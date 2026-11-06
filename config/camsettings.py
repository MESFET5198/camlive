"""camlive 设置: 系统设置(settings.json) + 摄像头配置校验 + 设备枚举。

优先级: WebUI 保存的 settings.json > 环境变量(.env / compose) > 内置默认值。
settings.json 与 cameras.json 一样位于 /app, 不会被静态文件服务对外提供。
"""
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading

SETTINGS_FILE = os.environ.get("CAMLIVE_SETTINGS", "/app/settings.json")
_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": {}}

# 系统设置项: 键 -> (环境变量, 默认值, 说明)
SYSTEM_FIELDS = {
    "host_ip": ("HOST_IP", "", "本机局域网 IP(WebRTC 连接地址)"),
    "webrtc_url": ("WEBRTC_URL", "http://{host}:8889/{id}/", "WebRTC 播放页地址模板"),
    "light_api_url": ("LIGHT_API_URL", "http://127.0.0.1:8788", "补光灯 light_api 地址"),
    "auth_user": ("CAMLIVE_USER", "admin", "登录用户名"),
}


# ---------------------------------------------------------------- 系统设置
def load():
    """读取 settings.json(按 mtime 缓存), 失败返回 {}"""
    try:
        mt = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["mtime"] != mt:
            try:
                with open(SETTINGS_FILE) as f:
                    d = json.load(f)
                _CACHE["data"] = d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                _CACHE["data"] = {}
            _CACHE["mtime"] = mt
        return dict(_CACHE["data"])


def save(data):
    with _LOCK:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
        _CACHE["mtime"] = None


def get(key):
    """WebUI 设置 > 环境变量 > 默认值"""
    v = load().get(key)
    if v not in (None, ""):
        return v
    env, default, _ = SYSTEM_FIELDS[key]
    return os.environ.get(env) or default


def source(key):
    if load().get(key) not in (None, ""):
        return "webui"
    return "env" if os.environ.get(SYSTEM_FIELDS[key][0]) else "default"


# ---------------------------------------------------------------- 密码
def hash_password(pw):
    salt = secrets.token_hex(16)
    it = 200000
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), it).hex()
    return "pbkdf2_sha256$%d$%s$%s" % (it, salt, h)


def _check_hash(pw, stored):
    try:
        algo, it, salt, h = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(calc, h)
    except (ValueError, AttributeError):
        return False


def auth_mode():
    """返回 (是否启用, 来源)。WebUI 中设置过(包括清空)以 WebUI 为准, 否则看环境变量。"""
    s = load()
    if "auth_password_hash" in s:
        return bool(s["auth_password_hash"]), "webui"
    return bool(os.environ.get("CAMLIVE_PASSWORD")), "env"


def check_password(user, pw):
    if not hmac.compare_digest(user.encode(), str(get("auth_user")).encode()):
        return False
    s = load()
    if "auth_password_hash" in s:
        return bool(s["auth_password_hash"]) and _check_hash(pw, s["auth_password_hash"])
    env = os.environ.get("CAMLIVE_PASSWORD", "")
    return bool(env) and hmac.compare_digest(pw.encode(), env.encode())


def public_system():
    """给前端的系统设置(不含密码)"""
    enabled, src = auth_mode()
    out = {}
    for k, (env, default, label) in SYSTEM_FIELDS.items():
        out[k] = {"value": get(k), "saved": load().get(k, ""), "source": source(k),
                  "env": env, "default": default, "label": label}
    out["auth_enabled"] = enabled
    out["auth_source"] = src
    return out


URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(?::\d+)?(?:/[^\s\"'<>`]*)?$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,253}$")


def update_system(req):
    """校验并保存系统设置; 返回 (新设置, 错误信息)"""
    s = load()
    for k in SYSTEM_FIELDS:
        if k not in req:
            continue
        v = str(req[k] or "").strip()
        if v:
            if k == "host_ip" and not all(HOST_RE.match(x) for x in v.split(",")):
                return None, "局域网 IP 格式不正确"
            if k in ("webrtc_url", "light_api_url") and not URL_RE.match(v.replace("{host}", "h").replace("{id}", "i")):
                return None, "%s 必须是 http(s):// 开头的地址" % SYSTEM_FIELDS[k][2]
            if k == "webrtc_url" and "{id}" not in v:
                return None, "WebRTC 地址模板必须包含 {id}"
            if k == "auth_user" and not re.fullmatch(r"[\w.\-@]{1,32}", v):
                return None, "用户名只能包含字母、数字和 _ . - @"
            if k == "light_api_url":
                v = v.rstrip("/")
        s[k] = v
    if "auth_password" in req:
        pw = req["auth_password"]
        if pw is None or pw == "":
            s["auth_password_hash"] = ""          # 显式关闭密码
        elif not isinstance(pw, str) or len(pw) < 4 or len(pw) > 128:
            return None, "密码长度需为 4~128 位"
        else:
            s["auth_password_hash"] = hash_password(pw)
    if req.get("auth_reset_to_env"):
        s.pop("auth_password_hash", None)
    save(s)
    return s, None


# ---------------------------------------------------------------- 摄像头配置
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")
DEVICE_RE = re.compile(r"^(?:/dev/[\w/.:\-]+|https?://[^\s\"'<>`]+)$")
RATE_RE = re.compile(r"^\d{1,5}[KM]$")
CB_RE = re.compile(r"^[a-z]{2}=-?\d+(?:\.\d+)?(?::[a-z]{2}=-?\d+(?:\.\d+)?)*$")

BOOL_FIELDS = ("auto_name", "webrtc", "timelapse", "light_auto", "sleep", "sleep_no_viewer",
               "sharpen", "sharpen_until_next_print", "usb_reset")
INT_FIELDS = {"width": (160, 7680), "height": (120, 4320), "fps": (1, 120),
              "max_width": (0, 7680), "max_height": (0, 4320)}
ENUM_FIELDS = {"mode": ("h264", "mjpeg"), "input_format": ("mjpeg", "h264", "yuyv422"),
               "rotate": (0, 90, 180, 270), "printer_light": ("", "phrozen_arco")}
# 修改这些字段需要重启推流
STREAM_FIELDS = ("device", "mode", "input_format", "width", "height", "fps", "bitrate", "bufsize",
                 "rotate", "webrtc", "colorbalance", "sharpen")


def clean_camera(req, base=None):
    """校验 WebUI 提交的摄像头字段, 合并到 base(保留未知字段); 返回 (cam, 错误)"""
    cam = dict(base or {})
    for k, v in req.items():
        if k in ("id",):
            continue
        if k == "name":
            v = str(v or "").strip()
            if len(v) > 64:
                return None, "名称太长"
            cam[k] = v
        elif k == "device":
            v = str(v or "").strip()
            if not DEVICE_RE.match(v):
                return None, "设备必须是 /dev/... 或 http(s):// 地址"
            cam[k] = v
        elif k == "printer":
            v = str(v or "").strip().rstrip("/")
            if v and not v.startswith(("http://", "https://")):
                v = "http://" + v
            if v and not URL_RE.match(v):
                return None, "打印机地址格式不正确, 例如 http://192.168.1.50:7125"
            if v and not re.search(r":\d+$", v.split("//", 1)[1].split("/", 1)[0]):
                v += ":7125"    # Moonraker 默认端口
            cam[k] = v
        elif k in ("bitrate", "bufsize"):
            v = str(v or "").strip().upper()
            if v and not RATE_RE.match(v):
                return None, "%s 格式应为 4M 或 1500K" % k
            cam[k] = v
        elif k == "colorbalance":
            v = str(v or "").strip()
            if v and not CB_RE.match(v):
                return None, "颜色平衡格式应为 rs=0.1:bs=-0.05"
            cam[k] = v
        elif k == "usb_vendor":
            v = str(v or "").strip().lower()
            if v and not re.fullmatch(r"[0-9a-f]{4}", v):
                return None, "USB vendor 应为 4 位十六进制, 如 2e1a"
            cam[k] = v
        elif k in BOOL_FIELDS:
            cam[k] = bool(v)
        elif k in INT_FIELDS:
            lo, hi = INT_FIELDS[k]
            try:
                v = int(v or 0)
            except (TypeError, ValueError):
                return None, "%s 必须是整数" % k
            if not lo <= v <= hi:
                return None, "%s 范围 %d~%d" % (k, lo, hi)
            cam[k] = v
        elif k in ENUM_FIELDS:
            if k == "rotate":
                try:
                    v = int(v or 0)
                except (TypeError, ValueError):
                    pass
            if v is None:
                v = ""
            if v not in ENUM_FIELDS[k]:
                return None, "%s 取值不正确" % k
            cam[k] = v
        # 其他未知字段忽略(不允许从 WebUI 写入任意键)
    # 空字符串/0 表示"不设置", 从配置中去掉
    for k in list(cam):
        if cam[k] == "" or (k in ("max_width", "max_height", "rotate") and cam[k] == 0):
            del cam[k]
    if not cam.get("device"):
        return None, "必须填写设备"
    cam.setdefault("mode", "h264")
    cam.setdefault("width", 1280)
    cam.setdefault("height", 720)
    cam.setdefault("fps", 25)
    cam.setdefault("bitrate", "4M")
    if not cam.get("name"):
        cam["name"] = (base or {}).get("id") or "Camera"
    return cam, None


def list_devices():
    """枚举本机视频设备: [{path, name, stable}], by-id 路径重启后不会变"""
    out, seen = [], set()
    try:
        txt = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True,
                             text=True, timeout=5).stdout or ""
    except Exception:
        txt = ""
    name = None
    for line in txt.splitlines():
        if line and not line.startswith(("\t", " ")):
            name = line.rstrip(":").strip()
        elif line.strip().startswith("/dev/video"):
            p = line.strip()
            out.append({"path": p, "name": name or p, "stable": False})
            seen.add(p)
    for p in sorted(glob.glob("/dev/video*")):
        if p not in seen:
            out.append({"path": p, "name": p, "stable": False})
    names = {d["path"]: d["name"] for d in out}
    for p in sorted(glob.glob("/dev/v4l/by-id/*")):
        target = os.path.realpath(p)
        out.append({"path": p, "name": "%s → %s" % (names.get(target, os.path.basename(p)), target),
                    "stable": True})
    return out
