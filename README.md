# 树莓派 SHT4x 呼吸监测器（呼吸暂停报警）

用树莓派 + SHT4x 温湿度传感器检测呼吸，**连续 20 秒无呼吸即报警**。带 Web 仪表盘，实时展示呼吸波形，历史展示呼吸频率与暂停事件。

> 本设备仅作呼吸活动监测演示，**非医疗级诊断**。

## 硬件

- 树莓派（Zero 2 W / 3 / 4 / 5）
- SHT40 / SHT41 / SHT45 温湿度传感器（I2C，地址 `0x44`）
- （可选）有源蜂鸣器 + LED（报警）

## 接线

| SHT4x | 树莓派引脚 |
|---|---|
| VIN | Pin 1（3.3V） |
| SDA | Pin 3（GPIO2） |
| SCL | Pin 5（GPIO3） |
| GND | Pin 9 |

详见 `wiring-diagram.html`。

## 文件说明

| 文件 | 说明 |
|---|---|
| `sht4x_read.py` | I2C 冒烟测试 |
| `sht4x_breath.py` | 呼吸检测脚本（命令行，蜂鸣器留待最后接入） |
| `breath_server.py` | 后端：采集 + 检测 + WebSocket + REST + SQLite，托管仪表盘 |
| `dashboard-live.html` | 实时仪表盘（由后端 serve，部署时命名为 `index.html`） |
| `dashboard-demo.html` | 演示版仪表盘（带仿真数据，本地双击即可看效果） |
| `plan.md` | 方案 |
| `detailed-plan.md` | 详细方案（架构 / API / 数据模型 / 里程碑） |
| `wiring-diagram.html` | 接线图 |

## 快速开始

```bash
# 1. 安装依赖
sudo pip3 install --break-system-packages adafruit-circuitpython-sht4x flask flask-sock

# 2. 测试传感器
python3 -u sht4x_read.py

# 3. 启动后端（浏览器打开 http://<树莓派IP>:8000）
python3 -u breath_server.py
```

## 呼吸检测原理

呼气时湿度快速上升、吸气时回落。用慢速指数滑动平均估计「基线」，湿度偏离基线的幅度即「呼吸信号」；信号向上越过阈值（默认 **1.2 %RH**，需按传感器距离实测标定）记为一次呼吸；连续 20 秒无呼吸 → 报警。
