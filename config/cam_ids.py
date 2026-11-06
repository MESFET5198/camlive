#!/usr/bin/env python3
import json
import os

for c in json.load(open(os.environ.get("CAMLIVE_CAMS", "/app/cameras.json"))):
    print(c["id"])
