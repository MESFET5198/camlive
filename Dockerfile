# camlive: 通用 UVC 摄像头直播容器
# 构建: 默认从 docker.m.daocloud.io 拉基础镜像(国内快); 海外可用:
#   docker build --build-arg BASE_IMAGE=debian:bookworm-slim -t camlive:generic .
ARG BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim
FROM ${BASE_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg v4l-utils python3 ca-certificates \
        intel-media-va-driver libva2 libva-drm2 libigdgmm12 \
    && rm -rf /var/lib/apt/lists/*

# 程序文件 + 通用配置模板(运行时 /app 由宿主机挂载, 入口脚本补齐缺失文件)
# config/cameras.json 是作者本机配置, 用 config.example 的通用模板覆盖
COPY config/ /app.default/
COPY config.example/cameras.json /app.default/cameras.json
RUN rm -rf /app.default/logs && \
    chmod +x /app.default/entrypoint.sh /app.default/run_stream.sh \
    /app.default/cam_conf.py /app.default/cam_ids.py /app.default/mjpeg_server.py \
    /app.default/timelapse_snapshot.py

VOLUME ["/app"]
EXPOSE 8081
ENTRYPOINT ["/app.default/entrypoint.sh"]
