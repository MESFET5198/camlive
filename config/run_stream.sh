#!/bin/bash
# 用法: run_stream.sh <camera_id>   配置来自 /app/cameras.json
ID=$1
[ -z "$ID" ] && exit 1
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
eval "$(python3 /app/cam_conf.py "$ID")" || exit 1

# 网络 MJPEG 摄像头（例如打印机自带 Crowsnest 摄像头）直接转成 HLS。
# 这一路不经过 v4l2-ctl，也不需要本机 /dev/video 设备。
if [[ "$DEV" == http://* || "$DEV" == https://* ]]; then
  exec ffmpeg -hide_banner -loglevel warning \
    -f mpjpeg -i "$DEV" -vf "scale=${W}:${H}" -c:v libx264 -preset veryfast -tune zerolatency \
    -r "${FPS}" -fps_mode cfr -b:v "${BITRATE}" -maxrate "${BITRATE}" -bufsize "${BUFSIZE}" \
    -g "$((FPS*2))" -f hls -hls_time 2 -hls_list_size 4 -hls_flags delete_segments \
    "/app/${ID}.m3u8"
fi

# 先用 v4l2-ctl 强制设置 MJPG 分辨率，校验失败重试(摄像头切换分辨率时常有怪癖)
for i in 1 2 3 4 5 6; do
  v4l2-ctl -d "$DEV" --set-fmt-video=width=${W},height=${H},pixelformat=MJPG >/dev/null 2>&1
  CUR=$(v4l2-ctl -d "$DEV" --get-fmt-video 2>/dev/null | grep "Width/Height" | head -1 | grep -oE "[0-9]+/[0-9]+")
  if [ "$CUR" = "${W}/${H}" ]; then
    echo "[$ID] format ok: ${W}x${H}"
    break
  fi
  echo "[$ID] format retry $i: got $CUR, want ${W}/${H}"
  sleep 1
done

# 临时锐化(失焦视觉补救): cameras.json 里 sharpen=true 时启用
VF=""
if [ -n "${COLORBALANCE:-}" ]; then
  echo "[$ID] colorbalance enabled: ${COLORBALANCE}"
  VF="colorbalance=${COLORBALANCE},"
fi
if [ "${SHARPEN:-0}" = "1" ]; then
  echo "[$ID] sharpen enabled"
  VF="unsharp=5:5:0.8:5:5:0.0,"
fi

# 优先 Intel VAAPI 硬编(需要 /dev/dri 透传 + iHD 驱动)，失败自动回退 x264 软编
ENC=()
if [ -e /dev/dri/renderD128 ]; then
  ffmpeg -hide_banner -loglevel error -f lavfi -i nullsrc=s=320x240:d=0.1 \
    -vaapi_device /dev/dri/renderD128 -vf "hwupload,scale_vaapi=format=nv12" -c:v h264_vaapi \
    -f null - >/dev/null 2>&1 && \
    ENC=(-vaapi_device /dev/dri/renderD128 -vf "${VF}hwupload,scale_vaapi=format=nv12" -c:v h264_vaapi)
fi
if [ ${#ENC[@]} -eq 0 ]; then
  echo "[$ID] VAAPI unavailable, fallback to x264"
  if [ "${SHARPEN:-0}" = "1" ]; then
    ENC=(-c:v libx264 -preset veryfast -tune zerolatency -vf "${VF}unsharp=5:5:0.8:5:5:0.0")
  else
    if [ -n "${VF}" ]; then
      ENC=(-c:v libx264 -preset veryfast -tune zerolatency -vf "${VF%,}")
    else
      ENC=(-c:v libx264 -preset veryfast -tune zerolatency)
    fi
  fi
fi

OUT="[f=hls:hls_time=2:hls_list_size=4:hls_flags=delete_segments]/app/${ID}.m3u8"
if [ "${WEBRTC:-0}" = "1" ]; then
  OUT="${OUT}|[f=rtsp:rtsp_transport=tcp]rtsp://mediamtx:8554/${ID}"
  echo "[$ID] dual output enabled: HLS + RTSP/WebRTC"
fi

exec ffmpeg -hide_banner -loglevel warning \
  -f v4l2 -input_format mjpeg -video_size "${W}x${H}" -framerate "${FPS}" -i "$DEV" \
  "${ENC[@]}" \
  -r "${FPS}" -fps_mode cfr \
  -b:v "${BITRATE}" -maxrate "${BITRATE}" -bufsize "${BUFSIZE}" \
  -g "$((FPS*2))" \
  -map 0:v:0 -f tee "${OUT}"
