#!/usr/bin/env python3
"""把 cameras.json 中某个摄像头的配置输出为 shell 变量, 供 run_stream.sh eval。

所有值都经过 shlex.quote 转义: 配置可以在 WebUI 中修改, 不能让任意字符进入 eval。
"""
import json
import os
import re
import shlex
import sys

with open(os.environ.get("CAMLIVE_CAMS", "/app/cameras.json")) as f:
    cams = json.load(f)
cid = sys.argv[1]
for c in cams:
    if c.get("id") == cid:
        c = dict(c)
        c["DEV"] = c.get("device", "/dev/video0")
        c["W"] = int(c.get("width", 1280))
        c["H"] = int(c.get("height", 720))
        c["FPS"] = int(c.get("fps", 25))
        c["BITRATE"] = c.get("bitrate", "4M")
        c["BUFSIZE"] = c.get("bufsize", "8M")
        c["INPUT_FORMAT"] = c.get("input_format", "mjpeg")
        for k, v in c.items():
            ku = k.upper()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", ku):
                continue
            if isinstance(v, bool):
                v = "1" if v else "0"
            elif isinstance(v, (int, float)):
                v = str(v)
            elif not isinstance(v, str):
                continue
            print("%s=%s" % (ku, shlex.quote(v)))
        break
else:
    sys.exit(1)
