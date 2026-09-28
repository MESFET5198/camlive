#!/usr/bin/env python3
import json
import sys

cams = json.load(open("/app/cameras.json"))
cid = sys.argv[1]
for c in cams:
    if c["id"] == cid:
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
            if isinstance(v, bool):
                print("%s=%s" % (ku, "1" if v else "0"))
            elif isinstance(v, (int, float)):
                print("%s=%s" % (ku, v))
            elif isinstance(v, str) and v and not any(ch in v for ch in " \t\n\"'$`"):
                print("%s=%s" % (ku, v))
        break
else:
    sys.exit(1)
