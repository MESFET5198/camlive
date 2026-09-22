# camlive: 通用 UVC 摄像头直播容器
# 构建: 默认从 docker.m.daocloud.io 拉基础镜像(国内快); 海外可用:
#   docker build --build-arg BASE_IMAGE=debian:bookworm-slim .
ARG BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim
FROM ${BASE_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg v4l-utils python3 ca-certificates \
        intel-media-va-driver libva2 libva-drm2 libigdgmm12 \
    && rm -rf /var/lib/apt/lists/*

# 默认配置模板(运行时代码挂载到 /app, 首次启动自动拷贝)
COPY config.example/ /app.default/
RUN chmod +x /app.default/entrypoint.sh /app.default/run_stream.sh \
    /app.default/cam_conf.py /app.default/cam_ids.py /app.default/mjpeg_server.py

VOLUME ["/app"]
EXPOSE 8081
ENTRYPOINT ["/app.default/entrypoint.sh"]
