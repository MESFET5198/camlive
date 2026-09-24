#!/usr/bin/env python3
import os
import sys
import time
import urllib.request

url, out_dir, start_number, interval = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
os.makedirs(out_dir, exist_ok=True)
n = start_number
while True:
    started = time.monotonic()
    try:
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
