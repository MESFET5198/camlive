#!/usr/bin/env python3
"""每摄像头一个 MJPEG 采集/分发进程(由 server.py 启动)。
ffmpeg 输出 multipart MJPEG 到管道, 转发给所有本地客户端;
server.py 通过 /preview/<id>/stream 代理给浏览器。
用法: mjpeg_server.py <camera_id> <device> <listen_port>
"""
import json
import socket
import subprocess
import sys
import threading

cid, dev, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
cams = json.load(open("/app/cameras.json"))
cam = next(c for c in cams if c["id"] == cid)
W, H, FPS = int(cam["width"]), int(cam["height"]), int(cam["fps"])

ff = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error",
     "-f", "v4l2", "-input_format", "mjpeg",
     "-video_size", "%dx%d" % (W, H), "-framerate", str(FPS),
     "-i", dev, "-f", "mpjpeg", "-q:v", "3", "pipe:1"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

clients = set()
lock = threading.Lock()
buf = b""
last_frame = b""
MARK = b"Content-Type: image/jpeg"


def reader():
    global buf, last_frame
    while True:
        chunk = ff.stdout.read(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 1_500_000:
            buf = buf[-1_500_000:]
        idx = buf.rfind(MARK)
        if idx >= 0:
            last_frame = buf[idx:]
        with lock:
            for c in list(clients):
                try:
                    c.sendall(chunk)
                except OSError:
                    clients.discard(c)
                    try:
                        c.close()
                    except OSError:
                        pass


def handle(c):
    with lock:
        if last_frame:
            try:
                c.sendall(last_frame)
            except OSError:
                c.close()
                return
        clients.add(c)
    try:
        while c.recv(1024):
            pass
    except OSError:
        pass
    finally:
        with lock:
            clients.discard(c)
        try:
            c.close()
        except OSError:
            pass


threading.Thread(target=reader, daemon=True).start()
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(8)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
