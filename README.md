# 树莓派 SHT4x 呼吸监测器（整合版）

> 分支：`feat/integration-camera-spo2`（基于 `feat/max30102-spo2`）
> 非医疗级诊断，仅课程演示 / 趋势观察。

## 这版做了什么

- **A 层（启停）**：FSR 在床 + VEML7700 光照 + 摄像头躺/坐，2-of-3；只有 `ASLEEP` 才使能 B；`AWAKE`/离床/如厕不报警。
- **B 层（20 s）**：SHT4x 单通道（FSR 不再参与 B）；事件时长存**总暂停**；加 1.5 s 不应期 + 5 s 高电平超时复位。
- **摄像头**：Teachable Machine **Pose**（Sit/Lie）浏览器端推理；Pi 只提供 MJPEG；`/camera` 页面打开时约 2 fps，POST 到 `/api/camera`；关闭页面则 `unknown`，A 层退回 FSR+光照。
- **SpO₂**：复用 `spo2/` 子项目，1 Hz 记录，随 session 启停；**不参与 20 s 判定**。
- **报警**：GPIO17 蜂鸣器 + GPIO27 LED（按同学接线），3 声/串，独立线程；`/api/test_alarm`。
- **FSR 阈值**：先给三档预设 `0.05 / 0.10 / 0.20 V`，实测后替换；支持极性反转（压力升高电压时勾选）。
- **部署**：本地 `chart.umd.min.js`、`static/pose_model`、`static/posenet`、`static/js` 全部离线；`index.html` 由后端托管。

## 硬件 / 接线

| 器件 | 接线 | 说明 |
|---|---|---|
| 口鼻 SHT4x | I2C1 0x44（Pin 1/3/5/9） | 主呼吸通道 |
| VEML7700 | I2C1 0x10 | 开/关灯、相机门控 |
| ADS1115 | I2C1 0x48（ADDR→GND） | FSR 单端 AIN0，PGA ±4.096 V（0–3.3 V 分压可直接接） |
| MAX30102 | I2C1 0x57 | SpO₂/HR，1 Hz 记录 |
| 蜂鸣器 | GPIO17（Pin 11） | NPN + 1 kΩ + 续流 |
| LED | GPIO27（Pin 13） | 330 Ω，按同学接线 |

> 环境 SHT4x 已删除；不再使用 i2c-3 overlay。所有 I2C 设备在 I2C1。

FSR 分压：3V3 — FSR — 节点 — 5 kΩ — GND（或反过来）。**先实测**空床/手压/身体压三档电压：

```bash
python3 - <<'PY'
from breath_sensors import ADS1115
adc = ADS1115(1)
for _ in range(20):
    print(round(adc.read_voltage(0)[0], 3))
PY
```

- 若压力升高电压 → 勾选“FSR 极性反转”。
- 三档预设只是占位；把实测值填进 `fsr_presets` 或 UI 下拉。
- 去抖 10 s、迟滞 0.03 V；`--fsr` 可改默认。

## 安装 / 运行

```bash
sudo apt update
sudo apt install -y python3-smbus python3-numpy python3-opencv i2c-tools
python3 -m pip install --break-system-packages flask flask-sock smbus2 RPi.GPIO

# SpO2 子项目依赖（见 spo2/README.md）
sudo apt install -y python3-scipy python3-yaml
python3 -m pip install --break-system-packages -r spo2/requirements.txt

# 启动（演示可把 sleep-onset 降到 60 s，正式用 300 s）
python3 -u breath_server.py --port 8000 --sleep-onset 300
```

systemd（可选，推荐）：

```bash
sudo cp systemd/breath-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now breath-monitor
sudo systemctl status breath-monitor --no-pager
```

打开：

- 仪表盘：`http://<pi>.local:8000/`
- 摄像头：`http://<pi>.local:8000/camera`（页面需保持打开，A 层才有相机票）

> 不要用 `g_ether`：OTG 口给 USB 摄像头；需要网络就用 Wi-Fi 或 hub 上的 USB-Ethernet。

## 摄像头模型（Teachable Machine Pose）

`static/pose_model/` 是 TM **Pose** 导出（`model.json` + `metadata.json` + `weights.bin`，标签 `Sit` / `Lie`）。
PoseNet 主干用本地 `static/posenet/mobilenet/float/075/`（MobileNetV1 0.75, stride 16）。
`static/js/teachablemachine-pose.min.js` 已把默认 Google 地址改为 `/static/posenet/mobilenet/float/075/`，离线可用。

- 推理在浏览器端（`tf.min.js` + TM pose），Pi 只负责 MJPEG；不存帧。
- 摄像头后台线程固定 **320×240@5 MJPG**（降低 USB 供电/带宽压力）；页面关闭 → `/api/camera` 不再更新 → 5 s 后 A 层视为 `unknown`。
- 若换模型，替换 `static/pose_model/` 三个文件即可；标签顺序会在页面显示。

## SpO₂（记录，不报警）

`spo2/` 是 MAX30102 子项目（驱动 / DSP / 仿真 / 自检 / 网页打标）。
`spo2_worker.py` 用子进程跑 `python -m oxy.cli monitor --out data/spo2_*.csv`，
每秒读 summary CSV 最新一行，写入 `latest` / `spo2_samples` 表。

- 随 A 层 `in_bed` 启停；离床停止。
- 不参与 20 s 判定；晨报/趋势可做事件后去饱和关联。
- 若没有硬件或 `spo2/` 不存在：静默降级为 `--`，不阻塞主服务。

## 关键接口

| 端点 | 说明 |
|---|---|
| `GET /` | 仪表盘 |
| `GET /camera` | 摄像头躺/坐页面 |
| `GET /video_feed` | Pi MJPEG |
| `POST /api/camera` | `{posture:"lying|sitting|unknown", confidence, t}` |
| `GET /api/status` | 最新状态（含 A/camera/SpO2） |
| `GET /api/history?range=1h|24h` | 呼吸频率 + 事件 |
| `GET /api/events` | 事件（总暂停时长） |
| `POST /api/config` | `th_up/apnea_sec/fsr_in_bed/fsr_invert` |
| `GET /api/selftest` | 一键硬件自检（各传感器读数 + ADC 四通道摆动） |
| `GET /api/export.csv` | 原始样本 |
| `GET /api/alarm_state` / `GET /api/test_alarm` | 报警状态/自检 |
| `WS /ws` | 50 ms 推送 |

## 硬件自检（接线/演示前必做）

```bash
curl -s http://127.0.0.1:8000/api/selftest | python3 -m json.tool
```

判读要点：

- `sht4x.ok=true` 且 `temp_c/rh_pct` 有值 → 口鼻 SHT4x 正常（待机也在读，2 s 一次，只显示不检测）。
- `veml7700.lux` < `lux_dark`（默认 10）→ 视为“暗”，**摄像头姿态票会被丢弃**；演示时开灯，让 lux > 10。
- `ads1115.verdict=floating` → AIN0 在无负载时摆动 > 0.2 V，说明 FSR 没真正接到 ADC（或分压缺下拉电阻），此时 FSR 阈值怎么调都没意义。
- `camera.ok=true` + `frame_age_s` < 3 → Pi 侧取流正常；页面没画面就是浏览器端的问题。

I²C 探测用 **SMBus quick write**（等价 `i2cdetect`）。注意不能用 `read_byte(0x44)` 判断 SHT4x 是否存在：SHT4x 在收到测量命令 `0xFD` 前会对裸读 NACK，会误报“未接”。启动时探测失败会自动重试，之后每 5 s 重试一次，因此**热插拔不需要重启服务**。

## P0 修复点

- 事件时长：`events.start_ts` 现在是**暂停起点**（上次呼吸），`duration = end - start`；不再少 20 s。
- 迟滞：`BreathDetector` 加 1.5 s 不应期 + 5 s 超时复位，避免卡在 `high` 漏呼吸。
- A 层：`a_state = standby/awake/asleep/gap`；B 只在 `asleep` 使能。
- FSR 不再做 B；1 Hz 只做在床/体动。
- LED 用 GPIO27，与同学接线一致。

## 已知限制

- 摄像头页面必须打开；关闭后 A 层无相机票（FSR+光照兜底）。
- 摄像头只在 `lux >= lux_dark`（默认 10）时计票；暗房间里姿态一律记 `unknown`。
- TM Pose 模型依赖 PoseNet 主干；Pi 侧不做推理，浏览器/手机需要能跑 WebGL/WASM。
- FSR 阈值/极性未实测；三档只是占位，务必先用 `/api/selftest` 确认 `verdict=stable` 再标定。
- SHT4x 单通道 B 仍受说话/翻身/离口鼻影响；A 门控 + 不应期 + 手动取消是必要冗余。
- SpO₂ 为教学级，绝对读数不作医疗依据。
- `spo2/` 子进程若退出（无手指/无硬件），UI 显示 `--`，不影响主服务。

## 分支

```bash
git checkout -b feat/integration-camera-spo2
# 把本目录文件放进去；或从本仓库直接推送
```

`main` 仍停在 11-21 的旧版；演示前请合并到 `main`/`dev`，不要用 `main` 启动。
