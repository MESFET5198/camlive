# camlive — UVC 摄像头直播容器

双路 USB 摄像头 HLS/MJPEG 直播 + Klipper 打印机状态叠加 + 云台控制(支持时)。

## 快速开始

1. 加载镜像: `docker load -i camlive-generic.tar` (或自行 build)
2. 创建配置目录: `mkdir -p config` (可空, 首次启动自动填充默认配置)
3. 按你的机器修改 `compose.yml` 的 `devices` 段(摄像头节点数量不同)
4. 编辑 `config/cameras.json`:
   - `device`: 摄像头设备路径(用 `v4l2-ctl --list-devices` 查)
   - `name`: 显示名称(设 `auto_name: true` 则自动取设备名)
   - `printer`: Klipper/Moonraker 地址(可选, 删除则不显示打印状态)
   - `usb_vendor`: 摄像头 USB vendor(自愈重置用, 可删)
   - `max_width/max_height`: 可选分辨率上限
5. `docker compose up -d` → 打开 http://<机器IP>:8081

## 配置说明(config/cameras.json)

| 字段 | 说明 |
|------|------|
| id | 唯一标识, 也是流文件名(dji.m3u8) |
| device | 摄像头设备节点 /dev/videoX |
| mode | h264=HLS 流畅 / mjpeg=超低延迟直出 |
| width/height/fps/bitrate | 默认规格 |
| printer | Moonraker 地址, 叠加显示打印进度/ETA/温度 |
| auto_name | true=名称跟随实际设备 |
| usb_vendor/usb_reset | 掉线自动 USB 重置自愈(可选) |

## 特性

- HLS 1s 分片低延迟; MJPEG 模式 ~0.5s
- Intel 核显 VAAPI 硬编码(自动回退软编码, 兼容 AMD/ARM)
- 打印进度叠加: 状态/百分比/层数/预计剩余时间/热床喷头温度, 每 5s 刷新
- 摄像头掉线自愈: 重启进程 → 60s 后 USB 重置
- PTZ/变焦控制(支持云台的相机)
- 可选 STM32 USB HID 补光灯联动: 通过主机侧 `light_api.py` 读取状态并控制开关/亮度/RGB
- 页面右上角 ⚙ 展开/收起设置面板(防误触)

## STM32 补光灯联动

`camlive` 不直接打开 HID 设备，而是代理到同一主机上的 STM32 `light_api.py`（默认
`http://127.0.0.1:8788`）。页面只读取状态；摄像头配置中的 `light_auto: true` 会让关联打印机
处于 `printing` 或 `paused` 时开灯，进入完成、错误、取消或待机状态时关灯。状态轮询只在开关边沿
写一次 USB，桥接服务失败时保留重试，不把失败伪装成成功。

部署主机侧桥接服务时，参考 STM32 固件仓库的 `MONITOR_INTEGRATION.md`，并设置专用 udev 规则
允许服务用户访问 `1209:0001`。运行 `camlive` 时可用 `LIGHT_API_URL` 指定桥接地址；不要把未鉴权
的 `light_api.py` 绑定到公网或直接让浏览器跨域访问。

代理接口：`GET /api/light/health`、`GET /api/light/status`、`POST /api/light/power`、
`POST /api/light/brightness`、`POST /api/light/color`。

`camlive` 对全局亮度实施固定上限 75%（协议值 `191/255`）。请求更高值时会被压到 191，
响应会同时返回 `requested_brightness`、`applied_brightness` 和 `max_brightness`。该限制是软件策略，
不能替代实际电流、线材、接插件和温升验证。

若摄像头没有暴露硬件白平衡控制，可在摄像头配置中使用 `colorbalance` 让 ffmpeg 做软件颜色平衡；
它只影响视频流，不改变摄像头固件设置。

## 常见问题

- 画质差/卡: 调低分辨率或码率; 用 MJPEG 模式看延迟
- 摄像头不在列表: `v4l2-ctl --list-devices` 查节点, 改 cameras.json 的 device 并重启容器
- 重启容器后摄像头异常: 容器会自动 USB 重置自愈, 等待 ~1 分钟
- 无核显/AMD 机器: 删除 compose.yml 中 /dev/dri 行, 自动用软编码

## 分享给他人前注意

- `config/cameras.json` 是你的实际配置(设备路径/打印机 IP), 不要直接发给别人
- 别人只需 `mkdir config` 启动, 会自动拿到**通用模板**(config.example), 再按需编辑
- `usb_vendor/usb_reset` 默认关闭: 不配置就不会对未知设备做 USB 重置
- 镜像源: 海外构建失败时 `docker build --build-arg BASE_IMAGE=debian:bookworm-slim .`
