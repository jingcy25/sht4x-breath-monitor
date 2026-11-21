# 树莓派 + SHT4x 呼吸监测 / 呼吸暂停报警器 —— 实施方案

## 1. 目标
- 用树莓派读取 SHT4x 温湿度传感器，检测呼吸（吸气/呼气）。
- 连续 **20 秒**未检测到有效呼吸 → 蜂鸣器 + LED **报警**。
- 恢复呼吸后**自动解除**报警。

## 2. 工作原理
- SHT4x 是 I2C 温湿度传感器，响应快（高精度单次测量约 8.3 ms），可做到每秒几十次采样。
- 把传感器放在口鼻附近（2~5 cm）：**呼气时湿度快速升高，吸气时回落**，湿度信号随呼吸周期性起伏。
- 软件用一个短滑动窗口计算湿度上升幅度；当湿度在极短时间内上升超过阈值（如 ≥ +0.3 %RH）判定为一次**「呼吸事件」**。
- 计时器记录「上一次呼吸」的时间戳；若 `当前时间 − 上次呼吸 > 20 s` → 触发报警。

> ⚠️ 这是「呼吸活动检测」，不是医疗级血氧/呼吸暂停诊断设备，结果仅作演示与辅助参考。

## 3. 硬件清单
| 名称 | 说明 |
|---|---|
| 树莓派 | 3B / 4B / 5 / Zero 2W 均可，40-pin GPIO 定义一致 |
| SHT40 / SHT41 / SHT45 模块 | I2C，3.3V（Adafruit / DFRobot 等兼容板） |
| 有源蜂鸣器 | 3.3V 有源（通电即响） |
| LED + 330Ω 电阻 | 报警指示灯（可选） |
| 杜邦线 | 公对母若干 |
| （可选）NPN 三极管 + 1kΩ 电阻 | 蜂鸣器驱动，避免直接用 GPIO 拉大电流 |

## 4. 接线表

### 4.1 SHT4x 传感器
| SHT4x 引脚 | 树莓派物理引脚 | 功能 |
|---|---|---|
| VIN / VDD | Pin 1 | 3.3V 电源 |
| GND | Pin 9（或任意 GND） | 地 |
| SCL | Pin 5 | GPIO3 (SCL) |
| SDA | Pin 3 | GPIO2 (SDA) |

> SHT4x 各模块丝印顺序可能不同（有的为 VIN/GND/SCL/SDA，有的为 VIN/SCL/SDA/GND），**一律以模块丝印为准**。

### 4.2 蜂鸣器（报警）
| 蜂鸣器 | 树莓派物理引脚 |
|---|---|
| +（信号） | Pin 11（GPIO17） |
| −（地） | Pin 6（GND） |

### 4.3 LED（报警指示灯，可选）
| LED | 树莓派物理引脚 |
|---|---|
| 阳极(+) → 330Ω 电阻 → GPIO | Pin 13（GPIO27） |
| 阴极(−) | Pin 14（GND） |

## 5. 软件步骤
1. 刷写 Raspberry Pi OS，启用 I2C：
   ```bash
   sudo raspi-config   # Interface Options → I2C → Enable → 重启
   ```
2. 安装依赖：
   ```bash
   sudo apt update && sudo apt install -y python3-pip
   sudo pip3 install adafruit-circuitpython-sht4x
   ```
3. 编写 Python 脚本（见下方伪代码）。
4. 运行并**标定阈值**（见第 7 节）。
5. 可选：用 systemd 实现开机自启。

## 6. 检测算法（核心伪代码）
```python
import time
import board
import busio
import digitalio
import adafruit_sht4x

# GPIO17 蜂鸣器 / GPIO27 LED
buzzer = digitalio.DigitalInOut(board.D17)
buzzer.direction = digitalio.Direction.OUTPUT
led = digitalio.DigitalInOut(board.D27)
led.direction = digitalio.Direction.OUTPUT

i2c = busio.I2C(board.SCL, board.SDA)
sensor = adafruit_sht4x.SHT4x(i2c)
sensor.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION  # 高精度、不加热

APNEA_SECONDS   = 20     # 呼吸暂停报警阈值（秒）
BREATH_THRESHOLD = 0.3   # 湿度突升阈值 (%RH)，需实测标定
WINDOW_SECONDS  = 0.6    # 滑动窗口

last_breath = time.monotonic()
window = []              # [(t, humidity)]

def alarm(on: bool):
    buzzer.value = on
    led.value = on

while True:
    t = time.monotonic()
    h = sensor.relative_humidity
    window.append((t, h))
    while window and t - window[0][0] > WINDOW_SECONDS:
        window.pop(0)

    # 窗口内湿度上升幅度 >= 阈值 => 视为一次呼吸
    if window:
        hmin = min(p[1] for p in window)
        hmax = max(p[1] for p in window)
        if hmax - hmin >= BREATH_THRESHOLD:
            last_breath = t

    # 超过 20 秒无呼吸 => 报警；恢复呼吸 => 自动解除
    alarm(t - last_breath > APNEA_SECONDS)

    time.sleep(0.05)   # 20 Hz 采样
```

> 进阶：呼气是「快速上升沿」，可改为检测短时差分 `h[t] - h[t-1] >= 阈值`，或加中值滤波、去基线漂移，抗干扰更稳。

## 7. 标定与注意事项
- **距离影响幅度**：传感器离口鼻越近信号越强，需实测确定 `BREATH_THRESHOLD`（先打印湿度曲线观察，再定阈值）。
- **误报/漏报来源**：环境湿度变化、说话、身体移动、空调/风扇气流都会干扰。
- **蜂鸣器电流**：有源蜂鸣器电流较大时用 NPN 三极管（如 2N2222 / S8050）+ 1kΩ 基极电阻驱动，避免 GPIO 过流。
- **报警解除**：恢复呼吸后应立即停止报警（代码已实现）；若需「长鸣/间歇鸣」可自行加 PWM 或延时循环。
- **电源**：SHT4x 用 3.3V，勿接 5V。

## 8. 后续可扩展
- 加一个按键做「静音/复位」。
- 记录呼吸频率（次/分钟）与湿度曲线到 CSV，用于复盘。
- 加 OLED 屏实时显示呼吸频率与状态。
- 联网（MQTT）推送到手机，做远程监护。
