# MAX30102 血氧 / 心率采集（Raspberry Pi Zero 2 W）

课程级 IoT 项目的第一步：用 MAX30102 反射式 PPG 传感器在树莓派上采集原始波形，
计算血氧饱和度（SpO2）、心率（HR）、灌注指数（PI）和信号质量（SQI），并落盘成 CSV。

> **这不是医疗器械。** MAX30102 的 datasheet 没有给出 SpO2 精度指标，红外 LED 是
> 880 nm（临床血氧仪常用 940 nm），默认曲线来自 Maxim AN6409 的经验拟合。
> 绝对读数可能有几个百分点的偏差，只能用于教学、趋势观察和算法实验。

## 快速开始（没有硬件也能跑）

```bash
cd spo2
PYTHONPATH=src python3 -m oxy.cli selftest          # 自检：已知 HR/SpO2 能否还原
PYTHONPATH=src python3 -m oxy.cli simulate --duration 60 --out data/sim.csv
PYTHONPATH=src python3 -m oxy.cli replay data/sim.csv
PYTHONPATH=src python3 scripts/plot_log.py data/sim.csv data/sim.png
```

`selftest` 会验证：仿真心率 72 bpm 恢复误差 ≤ 3 bpm，SpO2 97% 误差 ≤ 2%，
无手指时不得输出任何读数。

## 接线（Pi Zero 2 W 40-pin）

| MAX30102 模块 | 树莓派引脚 | 说明 |
|---|---|---|
| VIN | 3.3V (pin 1) | 模块自带 1.8V LDO 与电平转换 |
| GND | GND (pin 6) | 共地 |
| SDA | GPIO2 / pin 3 | I2C1 SDA |
| SCL | GPIO3 / pin 5 | I2C1 SCL |
| INT | GPIO17 / pin 11 | 可选，本项目用轮询不接也可 |

注意：Pi Zero 2 W 出厂**不带排针**，需要自己焊。I2C 默认关闭：

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
sudo apt install -y i2c-tools
i2cdetect -y 1        # 应看到 57
```

## 树莓派安装

不要在 Zero 2 W 上用 pip 编译 scipy（512 MB 内存经常 OOM）。用 apt 的二进制包 +
带 `--system-site-packages` 的 venv：

```bash
sudo apt install -y python3-numpy python3-scipy python3-smbus python3-yaml i2c-tools
python3 -m venv --system-site-packages .venv
.venv/bin/pip install smbus2 matplotlib
```

## 运行

```bash
# 先确认硬件（PYTHONPATH 指向 src/）
PYTHONPATH=src .venv/bin/python -m oxy.cli diag

# 连续采集，Ctrl-C 停止
PYTHONPATH=src .venv/bin/python -m oxy.cli monitor --out data/session.csv

# 采 5 分钟
PYTHONPATH=src .venv/bin/python -m oxy.cli monitor --out data/session.csv --duration 300
```

终端输出形如：

```
t=  12.0s  HR=72 bpm  SpO2=97.2%  PI=0.451%  SQI=0.86  red= 61234 ir=100233  [OK]
t=  13.0s  HR=-- bpm  SpO2=--%    PI=--%     SQI=0.31  red=   512 ir=   604  [NO FINGER]
```

文件：

- `data/session.csv`：1 Hz 汇总（HR / SpO2 / R / PI / SQI / 有效性）
- `data/session_raw.csv`：100 Hz 原始 `(red, ir)`，用于离线复算和报告作图

## 命令一览

| 命令 | 作用 |
|---|---|
| `oxy diag` | I2C 扫描 + PART_ID + 寄存器 + 温度 + FIFO 试读 |
| `oxy monitor` | 实时采集 |
| `oxy simulate` | 合成数据跑完整链路（无硬件） |
| `oxy replay RAW.csv` | 对原始 CSV 重新跑 DSP |
| `oxy selftest` | 无硬件自检（含憋气识别用例） |
| `oxy hold` | **PPG 单独憋气识别**（实时，输出 CSV + JSONL） |
| `oxy hold-sim` | 仿真验证憋气识别（无硬件） |
| `oxy web` | 网页界面：实时分数 + 空格打标（开始/结束憋气） |
| `oxy plot SUM.csv OUT.png` | 画 4 联图 |

## 憋气识别（仅 PPG）

```bash
# 先正常呼吸约 20 s 建立基线，再憋气
PYTHONPATH=src python3 -m oxy.cli hold --out data/hold.csv --jsonl data/hold.jsonl

# 无硬件验证：0-40s 正常，40-65s 憋气
PYTHONPATH=src python3 -m oxy.cli hold-sim --hold-start 40 --hold-dur 25
```

每秒输出一条 JSONL，字段 `state / score / confidence / features / drops`，可直接给
后续多设备加权融合使用。原理、打分公式与局限见 `docs/experiment.md` 第 11 节。

### 网页打标（推荐先用这个）

```bash
PYTHONPATH=src python3 -m oxy.cli web --port 8001
# 浏览器打开 http://<树莓派IP>:8001
```

页面上正常呼吸约 20 秒（`calibrating` → `normal`），按**空格**标记"开始憋气"，
松口后再按一次标记"恢复呼吸"。标签落到 `data/labels.csv`，同时记录当时的
state/score/特征。之后用真实标签评估延迟并挑阈值：

```bash
python3 scripts/evaluate_labels.py --labels data/labels.csv --timeline data/hold_web_*.csv
```

脚本会输出每段憋气的检出延迟、漏检、误报秒数，以及 `enter_score` 从 0.45 到 0.75
的扫描表，直接给出推荐阈值。没有硬件时可以加 `--simulate` 先看界面。

## 目录

```
config.yaml              所有可调参数（校验后才会生效）
src/oxy/driver.py        MAX30102 寄存器驱动
src/oxy/dsp.py           滤波 / R 比值 / SpO2 / HR / PI / SQI
src/oxy/resp.py          PPG 单独憋气识别（resp_mod / RSA / PWA）
src/oxy/monitor.py       采集循环 + CSV + JSONL
src/oxy/simulate.py      合成 PPG（含呼吸调制与憋气段）
src/oxy/cli.py           命令行入口
scripts/                 诊断 / 画图 / 标定
docs/experiment.md       实验方案、标定流程、局限与两周计划
```

详细实验方案见 [docs/experiment.md](docs/experiment.md)。
