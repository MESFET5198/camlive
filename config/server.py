#!/usr/bin/env python3
import base64
import glob
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

CAMS_FILE = "/app/cameras.json"
WEBROOT = "/app"
LOGDIR = "/app/logs"

FMT_FIELDS = ("width", "height", "fps", "bitrate")
STREAMS = None  # StreamManager 实例, 启动时创建
PRINTERS = None  # PrinterMonitor 实例, 启动时创建
TIMELAPSE = None  # TimelapseManager 实例, 启动时创建
LIGHT_AUTOMATION = None  # LightAutomation 实例, 启动时创建

# 延时拍摄: 每4秒抓1帧, 合成30fps视频 => 2小时(7200s)压缩为1分钟(1800帧), 即120倍速
TIMELAPSE_ROOT = "/timelapse"
TIMELAPSE_FPS = 30
TIMELAPSE_INTERVAL = 4
LIGHT_API_URL = os.environ.get("LIGHT_API_URL", "http://127.0.0.1:8788").rstrip("/")
LIGHT_MAX_BRIGHTNESS = 191  # 75% of 255; enforced by camlive before forwarding

# 访问控制: 设置 CAMLIVE_PASSWORD 后所有页面/接口都需要 HTTP Basic 认证
AUTH_USER = os.environ.get("CAMLIVE_USER", "admin")
AUTH_PASSWORD = os.environ.get("CAMLIVE_PASSWORD", "")
# WebRTC 播放页地址模板; {host} 替换为浏览器访问的主机名, {id} 为摄像头 id
WEBRTC_URL = os.environ.get("WEBRTC_URL", "http://{host}:8889/{id}/")
# 只允许下载这些静态文件; cameras.json、源码、日志等一律 404
STATIC_ALLOW = re.compile(r"^/(?:index\.html|hls\.js|favicon\.ico|vendor/[\w.\-]+\.js|[\w\-]+\.m3u8|[\w\-]+\.ts)?$")


def light_request(path, method="GET", body=None, timeout=3):
    """访问可选的主机侧 STM32 HID REST 桥接，不直接操作 HID。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(LIGHT_API_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class LightAutomation:
    """按打印机状态边沿切换补光灯，避免轮询时重复写 USB。

    补光灯只有一盏, 但可能有多台打印机/摄像头开启 light_auto:
    任意一台在打印(或暂停)就开灯, 全部结束才关灯, 避免互相抢开关。"""

    ACTIVE_STATES = ("printing", "paused")

    def __init__(self):
        self.active = False
        self.wants = {}  # cid -> bool

    def update(self, cid, state):
        self.wants[cid] = state in self.ACTIVE_STATES
        want_on = any(self.wants.values())
        if want_on == self.active:
            return
        who = ",".join(k for k, v in self.wants.items() if v) or cid
        try:
            result = light_request("/api/light/power", "POST", {"on": want_on})
            self.active = want_on
            print("[light] %s 打印状态=%s, 自动%s灯 -> %s" % (
                who, state, "开" if want_on else "关", result.get("power")),
                flush=True)
        except Exception as e:
            # 不更新 self.active, 下一轮轮询会重试
            print("[light] %s 自动%s灯失败: %s" % (
                who, "开" if want_on else "关", e), flush=True)


PRINTER_QUERY = ("/printer/objects/query?print_stats&display_status&virtual_sdcard"
                 "&heater_bed&extruder&extruder1&extruder2&extruder3")
PRINTER_POLL = 3        # 秒
FINISHED_STATES = ("complete", "cancelled", "error")
PRINTER_FRESH = 15      # 缓存超过该时间视为离线


def http_json(url, method="GET", timeout=4):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class PrinterMonitor:
    """每台打印机(Moonraker 地址)一个轮询线程, 结果缓存供 API/延时摄影/休眠/补光灯共用。
    以前每个功能、每个浏览器标签各自去查 Moonraker, 现在统一 3s 一次。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}      # url -> {"ts", "data" | "error"}
        self.threads = {}
        self.meta = {}       # (url, filename) -> slicer metadata
        self.last_job = {}   # url -> {"filename", "end_time", "duration", "state"}
        self.prev_state = {}

    def ensure(self, url):
        if not url or url in self.threads:
            return
        t = threading.Thread(target=self._loop, args=(url,), daemon=True)
        self.threads[url] = t
        t.start()

    def poll_now(self, url):
        """同步查询一次(启动时用), 返回解析结果或 None"""
        self._poll(url)
        return self.get(url)

    def get(self, url):
        """返回最近一次成功的解析结果; 过期/失败返回 None"""
        self.ensure(url)
        with self.lock:
            c = self.cache.get(url)
        if not c or "data" not in c or time.time() - c["ts"] > PRINTER_FRESH:
            return None
        return c["data"]

    def error(self, url):
        with self.lock:
            c = self.cache.get(url) or {}
        return c.get("error") or "printer not polled yet"

    def state(self, url):
        d = self.get(url)
        return d["state"] if d else None

    def _loop(self, url):
        while True:
            started = time.monotonic()
            self._poll(url)
            time.sleep(max(0.5, PRINTER_POLL - (time.monotonic() - started)))

    def _poll(self, url):
        try:
            st = http_json(url + PRINTER_QUERY)["result"]["status"]
            data = self._parse(url, st)
            with self.lock:
                self.cache[url] = {"ts": time.time(), "data": data}
        except Exception as e:
            with self.lock:
                old = self.cache.get(url) or {}
                old = dict(old)
                old["error"] = str(e)
                self.cache[url] = old if "data" in old else {"ts": time.time(), "error": str(e)}

    def _slicer_meta(self, url, filename):
        """切片软件元数据(预估总时长), 每个文件只查一次"""
        if not filename:
            return {}
        key = (url, filename)
        if key not in self.meta:
            try:
                r = http_json(url + "/server/files/metadata?filename=" +
                              urllib.parse.quote(filename), timeout=4)
                self.meta[key] = r.get("result") or {}
            except Exception:
                self.meta[key] = {}
            if len(self.meta) > 64:
                self.meta.pop(next(iter(self.meta)))
        return self.meta[key]

    def _last_job(self, url):
        """打印结束后从 Moonraker 历史记录取实际完成时间"""
        try:
            r = http_json(url + "/server/history/list?limit=1&order=desc", timeout=4)
            job = (r.get("result") or {}).get("jobs", [None])[0] or {}
            if job.get("end_time"):
                return {"filename": job.get("filename") or "",
                        "end_time": int(job["end_time"]),
                        "duration": int(job.get("print_duration") or 0),
                        "state": job.get("status")}
        except Exception:
            pass
        return None

    def _parse(self, url, st):
        ps = st.get("print_stats") or {}
        ds = st.get("display_status") or {}
        vs = st.get("virtual_sdcard") or {}
        info = ps.get("info") or {}
        state = ps.get("state") or "unknown"
        filename = ps.get("filename") or ""
        now = time.time()

        # 进度: 优先 Klipper display_status; virtual_sdcard 的文件位置在部分配置下不是当前进度
        progress = ds.get("progress")
        if progress is None and vs.get("file_size"):
            progress = vs.get("file_position", 0) / float(vs["file_size"])
        if progress is None:
            tl, cl = info.get("total_layer"), info.get("current_layer")
            if tl and cl:
                progress = cl / float(tl)

        # 剩余时间: 文件进度线性外推(越打越准) 与 切片预估 按进度加权混合
        pd = ps.get("print_duration") or 0
        td = ps.get("total_duration")
        remaining, source = None, None
        meta = self._slicer_meta(url, filename) if state in ("printing", "paused") else {}
        slicer_total = meta.get("estimated_time")
        rem_file = pd / progress * (1 - progress) if progress and progress > 0.02 and pd > 0 else None
        rem_slicer = max(0.0, slicer_total - pd) if slicer_total else None
        if rem_file is not None and rem_slicer is not None:
            w = min(1.0, progress * 2)  # 前半程逐渐从切片预估过渡到实测外推
            remaining, source = w * rem_file + (1 - w) * rem_slicer, "mixed"
        elif rem_file is not None:
            remaining, source = rem_file, "file"
        elif rem_slicer is not None:
            remaining, source = rem_slicer, "slicer"
        elif td and td > pd:
            remaining, source = td - pd, "klipper"
        if state not in ("printing", "paused"):
            remaining, source = None, None

        # 打印结束: 记录完成时间(状态边沿时取历史记录, 取不到就用当前时间)
        prev = self.prev_state.get(url)
        self.prev_state[url] = state
        if state in ("complete", "cancelled", "error"):
            lj = self.last_job.get(url)
            # 刚结束, 或者 camlive 启动时打印机已处于结束状态: 查一次历史记录
            if not lj or prev in ("printing", "paused"):
                lj = self._last_job(url) or {"filename": filename, "end_time": int(now),
                                            "duration": int(pd), "state": state}
                self.last_job[url] = lj
        elif state in ("printing", "paused"):
            self.last_job.pop(url, None)

        bed = st.get("heater_bed") or {}
        # U1 有多个喷头：优先显示当前 active_pin 的喷头，
        # 不能固定读取基础 extruder（停泊时通常只有约 50°C）。
        ext = st.get("extruder") or {}
        for ext_name, ext_data in st.items():
            if ext_name.startswith("extruder") and isinstance(ext_data, dict) and ext_data.get("active_pin"):
                ext = ext_data
                break
        lj = self.last_job.get(url) or {}
        return {
            "state": state,
            "filename": filename,
            "progress": round(progress, 4) if progress is not None else None,
            "layer": info.get("current_layer"),
            "total_layer": info.get("total_layer"),
            "print_duration": int(pd) if pd else None,
            "remaining": int(remaining) if remaining is not None else None,
            "eta": int(now + remaining) if remaining is not None else None,
            "eta_source": source,
            "slicer_total": int(slicer_total) if slicer_total else None,
            "finished_at": lj.get("end_time"),
            "finished_duration": lj.get("duration"),
            "ext_temp": ext.get("temperature"),
            "ext_target": ext.get("target"),
            "bed_temp": bed.get("temperature"),
            "bed_target": bed.get("target"),
            "updated": int(now),
        }


def load_cams():
    try:
        with open(CAMS_FILE) as f:
            cams = json.load(f)
        if not isinstance(cams, list):
            return []
        return cams
    except (OSError, ValueError):
        return []


CAMS_LOCK = threading.Lock()


def save_cams(cams):
    """原子写入: 先写临时文件再 rename, 避免并发请求/断电写出半个 JSON"""
    with CAMS_LOCK:
        tmp = CAMS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cams, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CAMS_FILE)


def update_cam(cid, **fields):
    with CAMS_LOCK:
        cams = load_cams()
    for c in cams:
        if c.get("id") == cid:
            c.update(fields)
    save_cams(cams)


_NAME_CACHE = {}  # device -> (expire_ts, name)


def cam_display_name(cam):
    """auto_name 开启时, 名称跟随实际设备(Card type); 失败回退配置名。
    结果缓存 60s, 避免每次状态轮询都 fork v4l2-ctl。"""
    if not cam.get("auto_name"):
        return cam.get("name", cam["id"])
    hit = _NAME_CACHE.get(cam.get("device"))
    if hit and hit[0] > time.time():
        return hit[1]
    name = _probe_display_name(cam)
    _NAME_CACHE[cam.get("device")] = (time.time() + 60, name)
    return name


def _probe_display_name(cam):
    try:
        out = subprocess.run(["v4l2-ctl", "-d", cam["device"], "--info"],
                             capture_output=True, text=True, timeout=10).stdout or ""
        m = re.search(r"Card type\s*:\s*(.+)", out)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return cam.get("name", cam["id"])


def get_cam(cid):
    for c in load_cams():
        if c.get("id") == cid:
            return c
    return None


def stream_pid_file(cid):
    return "/tmp/stream-%s.pid" % cid


def is_online(cid):
    p = os.path.join(WEBROOT, cid + ".m3u8")
    try:
        return (time.time() - os.path.getmtime(p)) < 10
    except OSError:
        return False


def v4l2(dev, *args, check=False, timeout=15):
    return subprocess.run(["v4l2-ctl", "-d", dev] + list(args),
                          capture_output=True, text=True, check=check, timeout=timeout)


def list_formats(dev, cam=None):
    """枚举 MJPG 支持的分辨率/帧率 -> [{width,height,fps}]; cam.max_width/max_height 限制可选"""
    out = v4l2(dev, "--list-formats-ext").stdout or ""
    fmts, cur, res = [], None, None
    for line in out.splitlines():
        m = re.match(r"\s*\[\d+\]:\s*'(\w+)'", line)
        if m:
            cur = m.group(1)
            res = None
            continue
        m = re.match(r"\s*Size:\s*Discrete\s+(\d+)x(\d+)", line)
        if m:
            res = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.match(r"\s*Interval:\s*Discrete\s+[\d.]+s\s+\(([\d.]+) fps\)", line)
        if m and res and cur == "MJPG":
            fmts.append({"width": res[0], "height": res[1],
                         "fps": int(float(m.group(1)) + 0.5)})
    if cam:
        mw = int(cam.get("max_width") or 0)
        mh = int(cam.get("max_height") or 0)
        if mw or mh:
            fmts = [f for f in fmts
                    if (not mw or f["width"] <= mw) and (not mh or f["height"] <= mh)]
    out2, seen = [], set()
    for f in fmts:
        k = (f["width"], f["height"], f["fps"])
        if k not in seen:
            seen.add(k)
            out2.append(f)
    return out2


def list_controls(dev):
    """枚举全部 v4l2 控制项 -> {name: {type,value,min,max,step,inactive}}"""
    out = v4l2(dev, "-L").stdout or ""
    res = {}
    for line in out.splitlines():
        m = re.match(r"\s*([a-z_]+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)", line)
        if not m:
            continue
        name, ctype, rest = m.group(1), m.group(2), m.group(3)
        info = {}
        for k, v in re.findall(r"(\w+)=(\S+)", rest):
            info[k] = v
        try:
            value = int(info.get("value", info.get("default", "0")))
        except ValueError:
            value = info.get("value", info.get("default", 0))
        res[name] = {"type": ctype, "value": value,
                     "min": int(info.get("min", 0)),
                     "max": int(info.get("max", 1)),
                     "step": int(info.get("step", 1)),
                     "inactive": "inactive" in rest}
    return res


def get_ptz(dev, controls):
    names = [n for n in ("pan_absolute", "tilt_absolute", "zoom_absolute")
             if n in controls]
    if not names:
        return None
    out = v4l2(dev, "-C", ",".join(names)).stdout or ""
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = int(v.strip())
    return d


def set_control(dev, name, value, controls=None):
    """设置控制项; 对依赖自动模式的项先解除自动, 如对焦/白平衡/曝光"""
    controls = controls if controls is not None else list_controls(dev)
    if name == "focus_absolute" and controls.get("focus_automatic_continuous", {}).get("value") == 1:
        v4l2(dev, "-c", "focus_automatic_continuous=0", check=True)
    if name == "white_balance_temperature" and controls.get("white_balance_automatic", {}).get("value") == 1:
        v4l2(dev, "-c", "white_balance_automatic=0", check=True)
    if name == "exposure_time_absolute" and controls.get("auto_exposure", {}).get("value") != 1:
        v4l2(dev, "-c", "auto_exposure=1", check=True)
    v4l2(dev, "-c", "%s=%d" % (name, int(value)), check=True)


def kill_stream(cid):
    STREAMS.stop_cam(cid)


def usb_reset_by_vendor(vendor, device):
    """通过 authorized 开关强制重新枚举 USB 摄像头(部分固件切换模式需要)"""
    for d in glob.glob("/sys/bus/usb/devices/[0-9]*"):
        try:
            if open(os.path.join(d, "idVendor")).read().strip() == vendor:
                for v in ("0", "1"):
                    with open(os.path.join(d, "authorized"), "w") as f:
                        f.write(v)
                    time.sleep(1)
                for _ in range(30):
                    if os.path.exists(device):
                        break
                    time.sleep(0.5)
                time.sleep(2)
                return os.path.exists(device)
        except OSError:
            continue
    return False


def switch_config(cam, w, h, fps):
    STREAMS.stop_cam(cam["id"])
    time.sleep(1)
    if cam.get("usb_reset") and cam.get("usb_vendor"):
        usb_reset_by_vendor(cam["usb_vendor"], cam["device"])
    ok = False
    for i in range(5):
        v4l2(cam["device"],
             "--set-fmt-video=width=%d,height=%d,pixelformat=MJPG" % (w, h))
        out = v4l2(cam["device"], "--get-fmt-video").stdout or ""
        m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
        if m and int(m.group(1)) == w and int(m.group(2)) == h:
            ok = True
            break
        time.sleep(1)
    return ok


class StreamManager:
    """统一管理每个摄像头的流进程: h264 -> run_stream.sh(HLS), mjpeg -> mjpeg_server.py"""

    def __init__(self):
        self.procs = {}
        self.sleeping = {}
        self.sleep_reason = {}

    def mjpeg_port(self, cid):
        cam = get_cam(cid)
        cams = load_cams()
        idx = next((i for i, c in enumerate(cams) if c["id"] == cid), 0)
        return int(cam.get("mjpeg_port") or (9100 + idx)) if cam else 0

    def start_cam(self, cam):
        cid = cam["id"]
        self.stop_cam(cid)
        try:
            os.makedirs(LOGDIR, exist_ok=True)
            logf = open(os.path.join(LOGDIR, "cam-%s.log" % cid), "ab")
        except OSError:
            logf = None
        if cam.get("mode") == "mjpeg":
            cmd = ["python3", "/app/mjpeg_server.py", cid,
                   cam["device"], str(self.mjpeg_port(cid))]
        else:
            cmd = ["bash", "/app/run_stream.sh", cid]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=logf or subprocess.DEVNULL)
        self.procs[cid] = p
        return p

    def stop_cam(self, cid):
        p = self.procs.pop(cid, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def check(self):
        """崩溃自动重启; m3u8 超时自动升级: 重启进程 -> 60秒后 USB 重置。永不抛异常。"""
        try:
            self._check_impl()
        except Exception as e:
            print("watchdog error:", repr(e), flush=True)

    def _check_impl(self):
        if not hasattr(self, "stale_cnt"):
            self.stale_cnt = {}
        self.cycle = getattr(self, "cycle", 0) + 1
        for cid, p in list(self.procs.items()):
            cam = get_cam(cid)
            if not cam:
                continue
            if self.sleeping.get(cid):
                continue
            stale = False
            if cam.get("mode") != "mjpeg":
                p3 = os.path.join(WEBROOT, cid + ".m3u8")
                try:
                    stale = (time.time() - os.path.getmtime(p3)) > 15
                except OSError:
                    stale = True
            n = self.stale_cnt.get(cid, 0)
            if p.poll() is None and not stale:
                self.stale_cnt[cid] = 0
                continue
            n += 1
            self.stale_cnt[cid] = n
            print("[wd] %s unhealthy n=%d poll=%s stale=%s" % (cid, n, p.poll(), stale), flush=True)
            if n == 1 or (p.poll() is not None and n % 3 == 1 and n < 20):
                # 进程已退出(例如网络源暂时断开): 每 ~9s 重试一次, 源恢复后很快出画面
                self.start_cam(cam)
            if n >= 20:
                self.stale_cnt[cid] = 0
                if cam.get("usb_vendor"):
                    print("[wd] %s USB reset" % cid, flush=True)
                    usb_reset_by_vendor(cam.get("usb_vendor"), cam.get("device"))
                self.start_cam(cam)

        # 休眠模式: 每 10 轮(~30s)检查打印机状态
        if self.cycle % 5 == 0:
            self._sleep_check()
        for cam in load_cams():
            if not cam.get("printer"):
                continue
            # 延时拍摄: 每轮(3s)按打印机状态自动开始/停止
            if TIMELAPSE is not None and cam.get("timelapse"):
                TIMELAPSE.check(cam)
            # 补光灯联动: 与是否开启延时拍摄无关
            if LIGHT_AUTOMATION is not None and cam.get("light_auto"):
                st = PRINTERS.state(cam["printer"])
                if st is not None:
                    LIGHT_AUTOMATION.update(cam["id"], st)

    def _sleep_check(self):
        """打印机完成/出错 -> 停止推流(休眠); 重新打印 -> 自动唤醒"""
        for cam in load_cams():
            if not cam.get("sleep") or not cam.get("printer"):
                continue
            cid = cam["id"]
            st = PRINTERS.state(cam["printer"])
            if st is None:
                continue
            if self.sleeping.get(cid):
                if st in ("printing", "paused", "standby"):
                    self.sleeping[cid] = False
                    self.start_cam(cam)
                    print("[sleep] %s 打印机重新开始(%s), 唤醒推流" % (cid, st), flush=True)
                continue
            if st in FINISHED_STATES:
                # 打印完成: 仅在没有观众(页面关闭45s)时休眠, 避免正在观看时流被停掉
                if time.time() - LAST_SEEN[0] > 45:
                    self.stop_cam(cid)
                    self.sleeping[cid] = True
                    self.sleep_reason[cid] = "打印完成"
                    print("[sleep] %s 打印机%s且无观众, 停止推流(休眠)" % (cid, st), flush=True)
        # 临时锐化: 仅当打印机从"完成/空闲"转入打印(真正的新打印)时自动关闭
        if not hasattr(self, "printer_state"):
            self.printer_state = {}
        for cam in load_cams():
            if not cam.get("sharpen_until_next_print") or not cam.get("sharpen"):
                continue
            if not cam.get("printer"):
                continue
            cid = cam["id"]
            st = PRINTERS.state(cam["printer"])
            if st is None:
                continue
            prev = self.printer_state.get(cid)
            self.printer_state[cid] = st
            if (st == "printing" and prev in FINISHED_STATES + ("standby",)
                    and cam.get("sharpen")):
                update_cam(cid, sharpen=False)
                self.start_cam(get_cam(cid))
                print("[sharpen] %s 检测到新打印(由%s), 自动关闭锐化并重启流" % (cid, prev), flush=True)
        # 无观众休眠: 页面关闭 60s 后停止推流
        for cam in load_cams():
            if not cam.get("sleep_no_viewer"):
                continue
            cid = cam["id"]
            if self.sleeping.get(cid):
                continue
            if time.time() - LAST_SEEN[0] > 45:
                self.stop_cam(cid)
                self.sleeping[cid] = True
                self.sleep_reason[cid] = "无观众"
                print("[sleep] %s 无观众超过60s, 停止推流(休眠)" % cid, flush=True)

    def stop_all(self):
        for cid in list(self.procs):
            self.stop_cam(cid)


class TimelapseManager:
    """延时拍摄: 打印机开始打印时抓帧, 结束时合成视频存入 /timelapse"""

    def __init__(self):
        self.active = {}       # cid -> {proc, dir, started, filename}
        self.last_state = {}   # cid -> 上次打印机状态

    def _cmd(self, cid, d, start_number=None):
        cam = get_cam(cid) or {}
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-rw_timeout", "15000000",
                "-fflags", "+discardcorrupt",
                "-live_start_index", "-1",
                "-i", (cam.get("device") if str(cam.get("device", "")).startswith(("http://", "https://"))
                       else os.path.join(WEBROOT, cid + ".m3u8")),
                "-vf", "fps=1/%d" % TIMELAPSE_INTERVAL]
        if str(cam.get("device", "")).startswith(("http://", "https://")):
            args[args.index("-live_start_index"):args.index("-live_start_index") + 2] = []
            args.insert(args.index("-i"), "-f")
            args.insert(args.index("-i"), "mpjpeg")
        if start_number is not None:
            args += ["-start_number", str(start_number)]
        args += ["-f", "image2", "-q:v", "2",
                 os.path.join(d, "frame_%06d.jpg")]
        return args

    def _spawn(self, cid, d, start_number):
        os.makedirs(d, exist_ok=True)
        cam = get_cam(cid) or {}
        device = str(cam.get("device", ""))
        snapshot_url = None
        if device.startswith(("http://", "https://")):
            snapshot_url = device.replace("action=stream", "action=snapshot")
        elif cam.get("mode") == "mjpeg":
            # MJPEG 模式没有 HLS 清单, 直接向本机 mjpeg_server 要单帧
            snapshot_url = "tcp://127.0.0.1:%d" % STREAMS.mjpeg_port(cid)
        if snapshot_url:
            return subprocess.Popen(
                ["python3", "/app/timelapse_snapshot.py", snapshot_url, d,
                 str(start_number), str(TIMELAPSE_INTERVAL)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return subprocess.Popen(self._cmd(cid, d, start_number),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _count_frames(self, d):
        try:
            return len(glob.glob(os.path.join(d, "frame_*.jpg")))
        except Exception:
            return 0

    def start(self, cam):
        cid = cam["id"]
        if cid in self.active:
            return
        started = time.localtime()
        d = os.path.join(TIMELAPSE_ROOT, cid, time.strftime("%Y%m%d_%H%M%S", started))
        try:
            p = self._spawn(cid, d, 1)
        except OSError as e:
            print("[timelapse] %s 无法创建目录 %s: %s" % (cid, d, e), flush=True)
            return
        except Exception as e:
            print("[timelapse] %s 启动抓帧失败: %s" % (cid, e), flush=True)
            return
        self.active[cid] = {"proc": p, "dir": d, "started": started,
                            "filename": cam.get("_tl_filename", ""),
                            "spawned": time.time()}
        print("[timelapse] %s 打印机开始打印, 开始延时拍摄 -> %s" % (cid, d), flush=True)

    def resume(self, cam):
        """容器重启后若打印机仍在打印: 续用最新已有帧目录继续拍摄, 避免丢帧"""
        cid = cam["id"]
        if cid in self.active:
            return True
        base = os.path.join(TIMELAPSE_ROOT, cid)
        try:
            subs = sorted(glob.glob(os.path.join(base, "[0-9]" * 8 + "_" + "[0-9]" * 6)), reverse=True)
        except Exception:
            subs = []
        for d in subs:
            n = self._count_frames(d)
            if n <= 0:
                continue
            try:
                started = time.strptime(os.path.basename(d), "%Y%m%d_%H%M%S")
            except ValueError:
                started = time.localtime()
            try:
                p = self._spawn(cid, d, n + 1)
            except Exception as e:
                print("[timelapse] %s 续拍失败: %s" % (cid, e), flush=True)
                return False
            self.active[cid] = {"proc": p, "dir": d, "started": started,
                                "filename": cam.get("_tl_filename", ""),
                            "spawned": time.time()}
            print("[timelapse] %s 恢复续拍 %s (已有%d帧, 从%d继续)" % (cid, d, n, n + 1), flush=True)
            return True
        return False

    def stop(self, cam):
        cid = cam["id"]
        a = self.active.pop(cid, None)
        if not a:
            return
        p = a["proc"]
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[timelapse] %s 打印结束, 停止抓帧(%s)并合成视频" % (cid, a["dir"]), flush=True)
        threading.Thread(target=self._assemble, args=(cam, a), daemon=True).start()

    def _assemble(self, cam, a):
        cid = cam["id"]
        d = a["dir"]
        try:
            frames = sorted(glob.glob(os.path.join(d, "frame_*.jpg")))
        except Exception as e:
            print("[timelapse] %s 读取帧目录失败: %s" % (cid, e), flush=True)
            return
        if not frames:
            print("[timelapse] %s 无帧, 跳过合成" % cid, flush=True)
            return
        ts = time.strftime("%Y%m%d_%H%M%S", a["started"])
        name = "%s_%s" % (ts, cid)
        fname = re.sub(r"[^\w.\-]+", "_", a.get("filename") or "")
        if fname:
            name += "_" + fname.strip("_")
        out = os.path.join(TIMELAPSE_ROOT, cid, name + ".mp4")
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-framerate", str(TIMELAPSE_FPS),
                 "-pattern_type", "sequence", "-start_number", "1",
                 "-i", os.path.join(d, "frame_%06d.jpg"),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 out], timeout=1800)
            if r.returncode == 0:
                print("[timelapse] %s 完成: %s (%d帧, %.1fs视频)" %
                      (cid, out, len(frames), len(frames) / TIMELAPSE_FPS), flush=True)
            else:
                print("[timelapse] %s 合成失败 rc=%d" % (cid, r.returncode), flush=True)
        except Exception as e:
            print("[timelapse] %s 合成异常: %s" % (cid, e), flush=True)
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def watch(self):
        """抓帧进程异常退出后自动重启; 目录若被误删则自动重建, 从现有帧数继续编号"""
        for cid, a in list(self.active.items()):
            d = a["dir"]
            proc_alive = a["proc"].poll() is None
            if proc_alive:
                # HLS 清单切换时 ffmpeg 可能不退出但停止产出图片，
                # 仅检查进程状态无法发现这种卡死。
                frames = glob.glob(os.path.join(d, "frame_*.jpg"))
                newest = max((os.path.getmtime(x) for x in frames), default=0)
                if newest and time.time() - newest < 20:
                    continue
                # 刚启动/重启的进程还没来得及出第一帧(HLS 打开需要几秒), 不要立刻杀掉
                if time.time() - a.get("spawned", 0) < 30:
                    continue
                try:
                    a["proc"].terminate()
                    a["proc"].wait(3)
                except Exception:
                    try:
                        a["proc"].kill()
                    except Exception:
                        pass
            n = self._count_frames(d) + 1
            try:
                p = self._spawn(cid, d, n)
            except Exception as e:
                print("[timelapse] %s 重启抓帧失败: %s" % (cid, e), flush=True)
                continue
            a["proc"] = p
            a["spawned"] = time.time()
            print("[timelapse] %s 抓帧进程异常退出, 从帧 %d 重新开始" % (cid, n), flush=True)

    def check(self, cam):
        """依据打印机状态驱动开始/停止; 打印中/暂停 -> 拍摄, 完成/出错/取消 -> 收尾"""
        cid = cam["id"]
        d = PRINTERS.get(cam["printer"])
        if not d:
            return
        st, fname = d["state"], d["filename"]
        prev = self.last_state.get(cid)
        self.last_state[cid] = st
        cam["_tl_filename"] = fname
        if st in ("printing", "paused"):
            if prev not in ("printing", "paused"):
                self.start(cam)
        elif prev in ("printing", "paused"):
            self.stop(cam)
        self.watch()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEBROOT, **kw)

    def log_message(self, fmt, *args):
        # 每个 HLS 分片都打一行访问日志太吵, 只记录非 2xx/3xx
        if len(args) >= 2 and str(args[1])[:1] in ("2", "3"):
            return
        super().log_message(fmt, *args)

    def end_headers(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.endswith(".m3u8") or path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _authorized(self):
        if not AUTH_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                if hmac.compare_digest(user.encode(), AUTH_USER.encode()) and \
                        hmac.compare_digest(pw.encode(), AUTH_PASSWORD.encode()):
                    return True
            except Exception:
                pass
        body = b"authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="camlive", charset="UTF-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _static(self, path, head=False):
        if not STATIC_ALLOW.match(path):
            self.send_error(404)
            return
        if head:
            super().do_HEAD()
        else:
            super().do_GET()

    def do_HEAD(self):
        if not self._authorized():
            return
        self._static(urllib.parse.urlsplit(self.path).path, head=True)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cam_summary(self, cam):
        online = (STREAMS.procs.get(cam["id"]) is not None and
                  STREAMS.procs[cam["id"]].poll() is None) if cam.get("mode") == "mjpeg" else is_online(cam["id"])
        return {"id": cam["id"], "name": cam_display_name(cam),
                "device": cam["device"],
                "sleeping": STREAMS.sleeping.get(cam["id"], False),
                "sleep_reason": STREAMS.sleep_reason.get(cam["id"]),
                "mode": cam.get("mode", "h264"),
                "width": int(cam.get("width", 0)),
                "height": int(cam.get("height", 0)),
                "fps": int(cam.get("fps", 0)),
                "bitrate": cam.get("bitrate", ""),
                "stream": cam["id"] + ".m3u8",
                "webrtc": bool(cam.get("webrtc", False)),
                "printer": bool(cam.get("printer")),
                "online": online,
                "timelapse": bool(TIMELAPSE.active.get(cam["id"])) if TIMELAPSE else False}

    def do_GET(self):
        if not self._authorized():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/info":
            self._json({"webrtc_url": WEBRTC_URL, "auth": bool(AUTH_PASSWORD),
                        "light_max_brightness": LIGHT_MAX_BRIGHTNESS})
            return
        if path == "/api/light/health":
            try:
                self._json(light_request("/health"))
            except Exception as e:
                self._json({"ok": False, "available": False, "last_error": str(e)}, 503)
            return
        if path == "/api/light/status":
            try:
                self._json(light_request("/api/light/status"))
            except Exception as e:
                self._json({"ok": False, "available": False, "last_error": str(e)}, 503)
            return
        if path == "/api/cams" or re.match(r"^/api/(cam|printer)/", path):
            LAST_SEEN[0] = time.time()
        m = re.match(r"^/preview/([^/]+)/(stream|snapshot)$", path)
        if m:
            cid, kind = m.group(1), m.group(2)
            cam = get_cam(cid)
            if not cam:
                self._json({"error": "no such camera"}, 404)
                return
            if cam.get("mode") != "mjpeg":
                self._json({"error": "camera is not in mjpeg mode"}, 400)
                return
            try:
                s = socket.create_connection(("127.0.0.1", STREAMS.mjpeg_port(cid)), timeout=5)
                s.sendall(b"SNAP\n" if kind == "snapshot" else b"STREAM\n")
            except OSError:
                self._json({"error": "mjpeg stream down"}, 503)
                return
            if kind == "snapshot":
                try:
                    data = b""
                    while True:
                        chunk = s.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                finally:
                    s.close()
                if data[:2] != b"\xff\xd8":
                    self._json({"error": "no frame yet"}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            s.settimeout(30)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    data = s.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                s.close()
            return
        m = re.match(r"^/api/printer/([^/]+)$", path)
        if m:
            cam = get_cam(m.group(1))
            if not cam or not cam.get("printer"):
                self._json({"error": "no printer configured"}, 404)
                return
            d = PRINTERS.get(cam["printer"]) or PRINTERS.poll_now(cam["printer"])
            if d:
                self._json(d)
            else:
                self._json({"error": "printer unreachable: %s" % PRINTERS.error(cam["printer"])}, 502)
            return
        if path == "/api/cams":
            self._json([self._cam_summary(c) for c in load_cams()])
            return
        m = re.match(r"^/api/cam/([^/]+)$", path)
        if m:
            cam = get_cam(m.group(1))
            if not cam:
                self._json({"error": "no such camera"}, 404)
                return
            if str(cam.get("device", "")).startswith(("http://", "https://")):
                controls, formats = {}, []
            else:
                try:
                    controls = list_controls(cam["device"])
                    formats = list_formats(cam["device"], cam)
                except Exception as e:
                    self._json({"error": "device read failed: %s" % e}, 500)
                    return
            d = self._cam_summary(cam)
            d["controls"] = controls
            d["formats"] = formats
            d["ptz"] = get_ptz(cam["device"], controls) if controls else None
            self._json(d)
            return
        self._static(path)

    def do_POST(self):
        if not self._authorized():
            return
        m = re.match(r"^/api/light/(power|brightness|color)$", self.path)
        if m:
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                if m.group(1) == "brightness":
                    value = req.get("value")
                    if not isinstance(value, int) or isinstance(value, bool):
                        self._json({"ok": False, "error": "value must be an integer"}, 400)
                        return
                    req["value"] = max(0, min(LIGHT_MAX_BRIGHTNESS, value))
                result = light_request("/api/light/" + m.group(1), "POST", req)
                if m.group(1) == "brightness":
                    result["requested_brightness"] = value
                    result["applied_brightness"] = req["value"]
                    result["max_brightness"] = LIGHT_MAX_BRIGHTNESS
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "available": False, "error": str(e)}, 502)
            return
        m = re.match(r"^/api/printer/([^/]+)/cmd$", self.path)
        if m:
            cam = get_cam(m.group(1))
            if not cam or not cam.get("printer"):
                self._json({"error": "no printer configured"}, 404)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            cmd = req.get("cmd")
            if cmd not in ("pause", "resume", "cancel"):
                self._json({"error": "cmd must be pause/resume/cancel"}, 400)
                return
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        cam["printer"] + "/printer/" + cmd, method="POST"),
                        timeout=5) as r:
                    r.read()
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": "printer cmd failed: %s" % e}, 502)
            return
        m = re.match(r"^/api/cam/([^/]+)/wake$", self.path)
        if m:
            cam = get_cam(m.group(1))
            if not cam:
                self._json({"error": "no such camera"}, 404)
                return
            was_sleeping = STREAMS.sleeping.get(cam["id"], False)
            STREAMS.sleeping[cam["id"]] = False
            STREAMS.sleep_reason.pop(cam["id"], None)
            LAST_SEEN[0] = time.time()
            p = STREAMS.procs.get(cam["id"])
            # 仅在休眠或进程不在时才(重)启动; 否则每打开一次页面就会把所有流重启一遍
            restarted = was_sleeping or p is None or p.poll() is not None
            if restarted:
                STREAMS.start_cam(cam)
            self._json({"ok": True, "restarted": restarted})
            return
        m = re.match(r"^/api/cam/([^/]+)/(config|ptz|control|mode)$", self.path)
        if not m:
            self._json({"error": "not found"}, 404)
            return
        cid, action = m.group(1), m.group(2)
        cam = get_cam(cid)
        if not cam:
            self._json({"error": "no such camera"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        if str(cam.get("device", "")).startswith(("http://", "https://")):
            controls = {}
        else:
            try:
                controls = list_controls(cam["device"])
            except Exception as e:
                self._json({"error": "device read failed: %s" % e}, 500)
                return
        if action == "mode":
            mode = req.get("mode")
            if mode not in ("h264", "mjpeg"):
                self._json({"error": "mode must be h264 or mjpeg"}, 400)
                return
            update_cam(cid, mode=mode)
            STREAMS.start_cam(get_cam(cid))
            self._json({"ok": True, "mode": mode})
            return
        if action == "control":
            ctrl, value = req.get("ctrl"), req.get("value")
            c = controls.get(ctrl)
            if not c:
                self._json({"error": "unsupported control"}, 400)
                return
            try:
                value = int(value)
                if not (c["min"] <= value <= c["max"]):
                    raise ValueError
            except Exception:
                self._json({"error": "%s must be %d~%d" % (ctrl, c["min"], c["max"])}, 400)
                return
            try:
                set_control(cam["device"], ctrl, value, controls)
            except Exception as e:
                self._json({"error": "set failed: %s" % e}, 500)
                return
            self._json({"ok": True, "ctrl": ctrl, "value": value})
            return
        if action == "ptz":
            args = []
            for key, name in (("pan", "pan_absolute"),
                              ("tilt", "tilt_absolute"),
                              ("zoom", "zoom_absolute")):
                v = req.get(key)
                if v is None:
                    continue
                c = controls.get(name)
                if not c:
                    self._json({"error": "%s not supported" % name}, 400)
                    return
                try:
                    v = int(v)
                    if not (c["min"] <= v <= c["max"]):
                        raise ValueError
                except Exception:
                    self._json({"error": "%s must be %d~%d" % (name, c["min"], c["max"])}, 400)
                    return
                args.append((name, v))
            if not args:
                self._json({"error": "need pan/tilt/zoom"}, 400)
                return
            try:
                for name, v in args:
                    set_control(cam["device"], name, v, controls)
            except Exception as e:
                self._json({"error": "ptz failed: %s" % e}, 500)
                return
            self._json({"ok": True, "ptz": get_ptz(cam["device"], controls)})
            return
        # config: 切换分辨率/帧率/码率
        w = req.get("width")
        h = req.get("height")
        fps = req.get("fps")
        bitrate = req.get("bitrate")
        try:
            w, h, fps = int(w), int(h), int(fps)
        except (TypeError, ValueError):
            self._json({"error": "width/height/fps required"}, 400)
            return
        formats = list_formats(cam["device"], cam)
        if not any(f["width"] == w and f["height"] == h and f["fps"] == fps
                   for f in formats):
            self._json({"error": "unsupported resolution/fps", "choices": formats}, 400)
            return
        try:
            m2 = re.fullmatch(r"(\d+)([KM])", str(bitrate).strip().upper())
            br, br_unit = int(m2.group(1)), m2.group(2)
            if not ((br_unit == "M" and 1 <= br <= 16) or
                    (br_unit == "K" and 100 <= br <= 16000)):
                raise ValueError
            bitrate = "%d%s" % (br, br_unit)
        except Exception:
            self._json({"error": "bitrate must be e.g. 2M or 1500K"}, 400)
            return
        update_cam(cid, width=w, height=h, fps=fps, bitrate=bitrate)
        try:
            confirmed = switch_config(cam, w, h, fps)
            # switch_config 会停流并从 procs 移除, watchdog 不会再管它, 必须显式重启
            STREAMS.start_cam(get_cam(cid))
        except Exception as e:
            self._json({"error": "restart failed: %s" % e}, 500)
            return
        self._json({"ok": True, "width": w, "height": h, "fps": fps,
                    "bitrate": bitrate, "camera_confirmed": confirmed})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    STREAMS = StreamManager()
    PRINTERS = PrinterMonitor()
    TIMELAPSE = TimelapseManager()
    LIGHT_AUTOMATION = LightAutomation()
    LAST_SEEN = [time.time()]  # 页面心跳(任意观众)
    for cam in load_cams():
        STREAMS.start_cam(cam)
        # 容器重启时若打印机已在打印, 立即恢复延时拍摄(优先续拍已有帧, 否则新开)
        if cam.get("printer"):
            d = PRINTERS.poll_now(cam["printer"])  # 同时启动该打印机的轮询线程
            st = d["state"] if d else None
            if cam.get("timelapse") and st in ("printing", "paused"):
                cam["_tl_filename"] = d["filename"]
                if not TIMELAPSE.resume(cam):
                    TIMELAPSE.start(cam)
                TIMELAPSE.last_state[cam["id"]] = st

    def watchdog():
        while True:
            time.sleep(3)
            STREAMS.check()

    threading.Thread(target=watchdog, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
