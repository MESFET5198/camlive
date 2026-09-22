#!/usr/bin/env python3
import json

for c in json.load(open("/app/cameras.json")):
    print(c["id"])
