# 呼吸监测仪 · 详细方案（树莓派 + SHT4x + Web 仪表盘）

## 1. 目标
- 用 SHT4x 温湿度传感器检测呼吸（呼气/吸气）。
- 连续 20 秒无有效呼吸 → 报警（蜂鸣器，留到最后接入）。
- 提供 Web 交互界面：**实时**展示呼吸波形/频率/状态，**历史**展示呼吸频率曲线与呼吸暂停事件，支持 CSV 导出。
- 可参数化：呼吸阈值、暂停秒数、预警秒数。

## 2. 系统架构
```
[SHT4x 传感器] --I2C--> [树莓派 Zero / Zero 2 W]
                              │
        ┌─────────────────────┼──────────────────────┐
        │  Python 采集 + 检测服务（20Hz）              │
        │   1. 读湿度/温度                              │
        │   2. 基线法 + 迟滞 检测呼吸                    │
        │   3. 呼吸暂停计时（20s）/ 报警状态机            │
        └─────────────────────┬──────────────────────┘
                              │ 内存队列 + SQLite 落库
        ┌─────────────────────┼──────────────────────┐
        │  Flask + WebSocket 后端                      │
        │   WS   /ws          实时推送                 │
        │   REST /api/status /api/history /api/events │
        │        /api/config /api/export.csv          │
        └─────────────────────┬──────────────────────┘
                              │
                  [ 浏览器仪表盘 ]  ←  实时波形 / 历史 / 报警
```

**部署形态**：所有组件都在树莓派上跑，浏览器通过 `http://<树莓派>.local:8000` 访问；USB 直连电脑时，电脑浏览器访问该地址即可。

## 3. 硬件与接线（简要，详见「呼吸传感器_接线图.html」）
| SHT4x | 树莓派引脚 | 功能 |
|---|---|---|
| VIN | Pin 1 | 3.3V |
| SDA | Pin 3 | GPIO2 |
| SCL | Pin 5 | GPIO3 |
| GND | Pin 9 | 地 |

蜂鸣器（最后一步）：+ → Pin 11（GPIO17），− → Pin 6（GND）。LED（可选）：GPIO27 + 330Ω。

## 4. 软件分层

### 4.1 采集 / 检测服务（Python，已备好基础版）
- 复用 `sht4x_breath.py` 的 `BreathDetector`（慢速指数基线 + 迟滞），抗环境湿度漂移。
- 每条样本产出：`{t, humidity, signal, breath, apnea}`。
- 职责：写 SQLite + 投递到后端的内存队列供 WebSocket 推送。

### 4.2 后端（Flask + WebSocket，待实现）
| 端点 | 方法 | 说明 |
|---|---|---|
| `/ws` | WS | 每 50ms 推一条实时样本 JSON |
| `/api/status` | GET | 当前状态（呼吸频率/湿度/温度/apnea） |
| `/api/history?range=1h|24h` | GET | 历史聚合（呼吸频率曲线 + 事件） |
| `/api/events` | GET | 呼吸暂停事件列表 |
| `/api/config` | POST | 更新阈值/暂停秒数（写回检测服务） |
| `/api/export.csv` | GET | 导出原始数据 |

### 4.3 前端仪表盘（本次交付，`呼吸监测仪表盘.html`）
- 实时滚动波形（呼吸信号 + 阈值线 + 呼吸事件点）
- 大状态卡（正常/预警/报警 + 呼吸频率 + 呼吸动画）
- 统计瓦片：频率 / 湿度 / 温度 / 距上次呼吸 / 暂停计时 / 事件次数
- 历史图（1h / 24h 呼吸频率 + 暂停事件）+ 事件列表
- 参数面板：阈值、暂停秒数、预警秒数、图表窗口、演示/实时切换、CSV 导出
- 数据源抽象：`DemoSource`（仿真，现在用）/ `WebSocketSource`（接真实后端）

## 5. 数据模型（SQLite）
```sql
CREATE TABLE samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,        -- Unix 时间
  humidity REAL,
  signal REAL,             -- 去基线后的呼吸信号
  breath INTEGER,          -- 0/1 本采样是否呼气
  apnea INTEGER            -- 0/1 是否处于呼吸暂停
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts REAL,
  end_ts REAL,
  duration REAL            -- 暂停持续秒数
);
CREATE INDEX idx_samples_ts ON samples(ts);
```

## 6. 前端对接的数据协议（WS 消息 JSON）
```json
{ "t": 1736000000.0, "humidity": 45.2, "signal": 1.8,
  "breath": 1, "apnea": 0, "bpm": 15.2, "temp": 26.4 }
```

## 7. 里程碑
| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 硬件接通 + 冒烟测试 | ✅ `sht4x_read.py` |
| M2 | 采集 + 呼吸检测 | ✅ `sht4x_breath.py` |
| M3 | Flask 后端 + WS + SQLite | ⬜ 待做 |
| M4 | 前端仪表盘 | ✅ 本次交付（仿真数据） |
| M5 | 前后端联调（DemoSource → WebSocketSource） | ⬜ 待做 |
| M6 | 蜂鸣器报警 | ⬜ 最后 |
| M7 | systemd 开机自启 + 部署 | ⬜ 待做 |

## 8. 部署（M7 目标）
- `systemd` 服务托管「采集+检测+后端」，开机自启。
- 前端静态文件由 Flask 直接 serve（单文件 HTML 拷到 `templates/`）。
- USB 直连时树莓派走 `g_ether` 网卡，电脑浏览器访问 `http://<主机名>.local:8000`。

## 9. 风险与注意
- 呼吸检测非医疗级，仅演示/辅助。
- 阈值需实测标定（传感器离口鼻距离、环境湿度影响大）。
- 蜂鸣器电流大时需三极管驱动，勿直接用 GPIO 拉电流。
- 历史数据建议定期归档/清理，避免 SQLite 无限增长。
