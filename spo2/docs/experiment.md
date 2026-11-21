# 实验方案：MAX30102 血氧 / 心率测量

> 下文用 `oxy` 代指 `PYTHONPATH=src python3 -m oxy.cli`（在项目根目录执行）。
> 例如 `oxy diag` 实际命令是 `PYTHONPATH=src python3 -m oxy.cli diag`。

## 1. 系统组成

```
MAX30102 ──I2C1──> Raspberry Pi Zero 2 W ──> CSV（1 Hz 汇总 + 100 Hz 原始）
                        │
                        └─ 终端状态行 / PNG 图
```

采集与显示在同一进程内完成，不依赖网络，适合课程现场演示。

## 2. 接线与供电

| MAX30102 | Pi Zero 2 W | 备注 |
|---|---|---|
| VIN | 3.3V (pin 1) | 模块板载 LDO，逻辑电平 3.3V |
| GND | GND (pin 6) | |
| SDA | GPIO2 (pin 3) | 1.8 kΩ 板载上拉，模块再并 4.7 kΩ 仍可用 |
| SCL | GPIO3 (pin 5) | |
| INT | GPIO17 (pin 11) | 可选；本项目轮询 FIFO，不接也能跑 |

排查顺序：`i2cdetect -y 1` 有 `57` → `oxy diag` 能读到 `PART_ID = 0x15` →
FIFO 里有数据 → 手指放上去 `ir` 从几百跳到几万。

## 3. 采样参数与为什么

| 参数 | 取值 | 理由 |
|---|---|---|
| 模式 | SpO2（red + IR） | 需要两个波长才能算 R 比值 |
| 采样率 | 100 sps | 心率谐波到 ~5 Hz，100 sps 足够且 FIFO 压力小 |
| LED 脉宽 | 411 µs（18 bit） | 最高分辨率，暗电流影响最小 |
| ADC 量程 | 4096 nA | 手指反射光通常落在这个量程 |
| LED 电流 | 7.2 mA（0x24） | 先保守，PI 太低再加；太高会饱和 |
| FIFO | 不平均、不开 rollover、阈值 15 | 溢出可检测，避免静默丢样本 |

轮询间隔 0.2 s：100 sps 下每次约 20 个样本，FIFO 深度 32，留有 60% 余量。
`config.py` 会拒绝任何可能造成 FIFO 溢出的 `poll_interval_s`。

## 4. 算法

1. **带通** 0.5–5 Hz，4 阶 Butterworth，`sosfiltfilt` 零相位。
   低端去呼吸基线漂移，高端去工频与高频噪声。
2. **DC** 取窗内均值，**AC** 取滤波后 RMS。
3. **R = (AC_red/DC_red) / (AC_ir/DC_ir)**。
4. **SpO2 = −45.060·R² + 30.354·R + 94.845**（Maxim AN6409），钳位 70–100%。
5. **心率**：滤波后 IR 取反，`find_peaks`（最小间隔 0.33 s）→ RR 中位数 → bpm。
6. **PI** = 100·AC_ir/DC_ir，正常指尖约 0.1–2%。
7. **SQI**（0–1）= 0.35·PI 合理 + 0.30·节律规则 + 0.20·红/红外波形相关 + 0.15·未饱和。
   SQI < 0.55 时界面显示 `--`，CSV 记 `valid=0`。

窗口 5 s、1 s 更新一次。窗口越短越灵敏但 SpO2 越抖；5 s 是课程场景的折中。

## 5. 标定（可选，但报告里很加分）

没有受控低氧条件，只能覆盖 95–100% 的窄区间，因此**不要声称临床精度**，
把它写成"局部经验修正"即可。

1. 受试者静坐，你的设备夹食指，商用指夹血氧仪夹另一只手同侧中指。
2. 采 10 分钟，`oxy monitor` 记录；同时每隔 20–30 s 人工记录商用仪读数到
   `data/reference.csv`：

   ```csv
   timestamp_iso,ref_spo2
   2026-11-21T21:03:12+08:00,98
   2026-11-21T21:03:41+08:00,97
   ```

3. 合并并拟合：

   ```bash
   python3 scripts/calibrate.py merge data/session.csv data/reference.csv \
       --out data/calibration_points.csv
   python3 scripts/calibrate.py fit data/calibration_points.csv \
       --out data/calibration.json
   ```

4. 报告 RMSE、bias、95% LoA（脚本会直接打印）。`oxy monitor` 下次启动会自动加载。

至少 30 个配对点、R 覆盖范围 ≥ 0.1 才勉强可谈；少于 10 个点仅作演示。

## 6. 验证与预期结果

| 项目 | 方法 | 预期 |
|---|---|---|
| 算法正确性 | `oxy selftest` | HR 误差 ≤3 bpm，SpO2 误差 ≤2% |
| 无手指拒绝 | 自检用例 | 0 个有效窗口 |
| 静息读数 | 与商用仪对比 | 偏差常在 ±2–4%，个体差异更大 |
| 运动伪差 | 手指轻动 | SQI 掉到阈值以下，输出 `--` 而非乱跳 |

**关于憋气**：30–45 s 憋气 SpO2 通常只降 0–2%，处在噪声量级，**不能作为
憋气检测的主判据**。这正是后续加麦克风 / 摄像头 / 温湿度的原因：靠"呼吸声消失 +
胸廓运动消失 + PPG 呼吸调制消失"来判事件，SpO2 只做辅助与事后佐证。

## 7. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `PART_ID` 不是 0x15 | 接线 / 供电 / I2C 未启用 | 查线，`i2cdetect -y 1` |
| `i2cdetect` 无 0x57 | SDA/SCL 反接、模块坏、没共地 | 换模块或杜邦线 |
| `ir` 只有几百 | 没有手指 / LED 电流太低 | 放手指，调 `led.current_ir` |
| `ir` 接近 262143 | LED 太亮饱和 | 降 `led.current_ir` |
| 频繁 FIFO overflow | Pi 负载高 / 轮询太慢 | 关桌面、降采样率、缩短轮询 |
| SpO2 乱跳 | 手指移动、按压力度变化、漏光 | 遮光、固定手指、看 SQI |

## 8. 报告可以怎么写（诚实版）

- 方法：ratio-of-ratios + 经验曲线 + 质量门控。
- 结果：与商用仪对比的 bias/LoA；SQI 门控前后的有效窗口比例；伪差条件下的表现。
- 讨论：880 nm vs 940 nm 的标定差异；未标定时绝对值的系统性偏差；
  5 s 窗口在灵敏度与稳定性之间的取舍；反射式与透射式的信噪比差异。
- 结论：可用于趋势与教学，不可用于诊断。

## 9. 两周计划

| 时间 | 任务 | 交付 |
|---|---|---|
| D1–D2 | 焊排针、开 I2C、`oxy diag` 通 | 寄存器截图 |
| D3–D4 | 静息采集 10 min，跑通 selftest / replay | 原始 CSV + 4 联图 |
| D5–D6 | 与商用血氧仪对比，做标定 | calibration.json + RMSE/LoA |
| D7–D8 | 抗干扰实验：移动、遮光、不同手指、不同人 | 对比表 |
| D9–D10 | 写报告 + 准备演示脚本 | 报告初稿 |
| D11–D14 | 接入麦克风 / 摄像头 / 温湿度，开始憋气事件检测 | 第二阶段原型 |

## 10. 第二阶段接口预留

`Monitor` 已把"采集 → 特征 → 输出"三段分开：

- 采集：任何实现 `read_fifo() -> (samples, overflowed)` 的对象都能替换进来；
- 特征：`dsp.analyze_window()` 是纯函数，可并列增加 `mic_features()`、`motion_features()`；
- 输出：CSV 表头集中在 `monitor.py` 顶部，加列不会影响其他模块。

后续只需新增一个 `MultiSourceMonitor`，复用同样的时钟与 CSV 写出逻辑。
