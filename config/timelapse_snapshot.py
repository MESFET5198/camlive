#!/usr/bin/env python3
import os
import socket
import sys
import time
import urllib.request

url, out_dir, start_number, interval = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])


def snap_tcp(url):
    """tcp://host:port -> 向 mjpeg_server.py 发 SNAP 取一帧"""
    host, _, port = url[len("tcp://"):].partition(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(b"SNAP\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data
            data += chunk


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
os.makedirs(out_dir, exist_ok=True)
n = start_number
while True:
    started = time.monotonic()
    try:
        if url.startswith("tcp://"):
            data = snap_tcp(url)
        else:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with opener.open(req, timeout=10) as r:
                data = r.read()
        if data[:2] != b"\xff\xd8":
            raise ValueError("snapshot is not JPEG")
        tmp = os.path.join(out_dir, ".frame_%06d.jpg.tmp" % n)
        final = os.path.join(out_dir, "frame_%06d.jpg" % n)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, final)
        n += 1
    except Exception:
        pass
    delay = interval - (time.monotonic() - started)
    if delay > 0:
        time.sleep(delay)
