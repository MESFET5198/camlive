#!/usr/bin/env python3
"""每摄像头一个 MJPEG 采集/分发进程(由 server.py 启动)。

ffmpeg 输出 multipart MJPEG 到管道, 本进程解析出完整 JPEG 帧, 只保留最新一帧;
每个客户端一个线程, 等待新帧后发送。慢客户端只会丢自己的帧, 不会拖慢其他人。

本地协议(仅监听 127.0.0.1, server.py 代理给浏览器):
  客户端先发一行指令:
    STREAM\\n  -> 持续推送 multipart/x-mixed-replace (boundary=ffmpeg)
    SNAP\\n    -> 返回一张最新 JPEG 后关闭(延时摄影/快照用)

ffmpeg 退出时本进程也退出, 交给 server.py 的 watchdog 重启。

用法: mjpeg_server.py <camera_id> <device> <listen_port>
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

cid, dev, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(os.environ.get("CAMLIVE_CAMS", "/app/cameras.json")) as f:
    cam = next(c for c in json.load(f) if c["id"] == cid)
W, H, FPS = int(cam.get("width", 1280)), int(cam.get("height", 720)), int(cam.get("fps", 25))

if dev.startswith(("http://", "https://")):
    input_args = ["-f", "mpjpeg", "-i", dev]
else:
    # 输入格式跟随配置(例如 Insta360 只有 h264 才有高分辨率), 输出统一重编码为 MJPEG
    input_args = ["-f", "v4l2", "-input_format", cam.get("input_format", "mjpeg"),
                  "-video_size", "%dx%d" % (W, H), "-framerate", str(FPS),
                  "-i", dev]

ROTATE = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}
filters = []
rot = int(cam.get("rotate", 0)) % 360
if rot in ROTATE:
    filters.append(ROTATE[rot])
if cam.get("colorbalance"):
    filters.append("colorbalance=" + cam["colorbalance"])
output_args = ["-f", "mpjpeg", "-boundary_tag", "ffmpeg", "-q:v", str(cam.get("mjpeg_quality", 3))]
if filters:
    output_args += ["-vf", ",".join(filters)]
ff = subprocess.Popen(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + input_args + output_args + ["pipe:1"],
    stdout=subprocess.PIPE)

cond = threading.Condition()
frame = b""     # 最新一帧 JPEG
seq = 0         # 帧序号


def reader():
    """解析 ffmpeg mpjpeg 输出: --ffmpeg\\r\\nContent-type: ..\\r\\nContent-length: N\\r\\n\\r\\n<N bytes>"""
    global frame, seq
    buf = bytearray()
    out = ff.stdout
    while True:
        chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            hdr_end = buf.find(b"\r\n\r\n")
            if hdr_end < 0:
                if len(buf) > 65536:  # 不可能这么长的头, 丢弃垃圾数据
                    del buf[:-4]
                break
            length = None
            for line in bytes(buf[:hdr_end]).split(b"\r\n"):
                k, _, v = line.partition(b":")
                if k.strip().lower() == b"content-length":
                    try:
                        length = int(v.strip())
                    except ValueError:
                        pass
            if length is None:
                # 没有长度头: 退化为按下一个边界切分
                nxt = buf.find(b"--ffmpeg", hdr_end + 4)
                if nxt < 0:
                    break
                body = bytes(buf[hdr_end + 4:nxt]).rstrip(b"\r\n")
                del buf[:nxt]
            else:
                if len(buf) < hdr_end + 4 + length:
                    break
                body = bytes(buf[hdr_end + 4:hdr_end + 4 + length])
                del buf[:hdr_end + 4 + length]
            if body[:2] == b"\xff\xd8":
                with cond:
                    frame = body
                    seq += 1
                    cond.notify_all()
    print("[mjpeg] %s ffmpeg exited rc=%s" % (cid, ff.wait()), file=sys.stderr, flush=True)
    os._exit(1)


def part(jpg):
    return (b"--ffmpeg\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)) + jpg + b"\r\n"


def handle(c):
    try:
        c.settimeout(5)
        cmd = b""
        while not cmd.endswith(b"\n") and len(cmd) < 64:
            d = c.recv(64)
            if not d:
                break
            cmd += d
        cmd = cmd.strip().upper()
        c.settimeout(10)  # 发送超时: 卡住超过 10s 的客户端直接断开
        if cmd == b"SNAP":
            with cond:
                if not frame:
                    cond.wait(5)
                jpg = frame
            if jpg:
                c.sendall(jpg)
            return
        last = -1
        while True:
            with cond:
                if seq == last:
                    cond.wait(10)
                if seq == last:
                    continue
                jpg, last = frame, seq
            c.sendall(part(jpg))
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


threading.Thread(target=reader, daemon=True).start()
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
for _ in range(10):
    try:
        srv.bind(("127.0.0.1", port))
        break
    except OSError:
        time.sleep(0.5)  # 旧进程刚退出, 端口可能还没释放
srv.listen(16)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
