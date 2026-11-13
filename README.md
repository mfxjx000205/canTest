# CANtest

CAN / CAN FD 总线测试控制面。底层用 [python-can](https://python-can.readthedocs.io/) 驱动硬件，
对外暴露一套 HTTP + WebSocket 接口，供自动化测试平台直接调用——平台侧不需要装任何 CAN 驱动，
也不用管 socketCAN、PCAN、Kvaser 之间参数怎么不一样。

```
┌──────────────────┐   HTTP / WebSocket    ┌──────────────┐   驱动     ┌─────────┐
│  自动测试平台     │ ────────────────────▶ │   CANtest    │ ─────────▶ │ CAN 硬件 │
│ (pytest / 脚本)  │ ◀──────────────────── │  FastAPI 服务 │ ◀───────── │  / 总线  │
└──────────────────┘   {"ok": true, ...}   └──────────────┘   收发      └─────────┘
```

## 环境与硬件

| 项目 | 实际配置 |
| --- | --- |
| 操作系统 | Windows 10/11（Linux 亦可，走 socketCAN） |
| 分析仪 | PEAK **PCAN-USB Pro FD**（双通道 `PCAN_USBBUS1/2`） |
| 驱动 | PCAN-Basic 4.9.0.942，`PCANBasic.dll` 已在 System32 |
| 总线 | 1 Mbps 经典 CAN，控制器时钟 80 MHz，采样点 80% |
| python | 3.9+（实测 3.13） |

Linux 上把 `interface` 换成 `socketcan`、`channel` 换成 `can0` 即可，其余接口完全一致。

## 安装与启动

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"      # Linux: .venv/bin/pip

.venv\Scripts\python -m cantest probe      # 自检：驱动 DLL / 后端 / 通道
.venv\Scripts\python -m cantest serve      # 起服务，默认 127.0.0.1:8700
```

`probe` 的输出就是你排障的第一现场：

```
平台        : Windows  (Python 3.13.14)
python-can  : 4.6.1
默认后端    : pcan
驱动 DLL    :
  [OK]   PCANBasic.dll
  [MISS] canlib32.dll
通道        :
  pcan/PCAN_USBBUS1
  pcan/PCAN_USBBUS2
```

接口文档：服务起来后访问 <http://127.0.0.1:8700/docs>。

## 五分钟上手

```bash
# 1) 打开通道（只监听；listen_only=true 时不会产生 ACK，不干扰在役总线）
curl -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"pcan","channel":"PCAN_USBBUS1","bitrate":1000000,
       "f_clock_mhz":80,"sample_point":0.8,"listen_only":true}'
# → {"ok":true,"data":{"id":"pcan-usbbus1-1", ...}}

# 2) 看统计（帧率、总线负载、错误计数）
curl http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/stats

# 3) 消费式读 10 帧
curl -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/receive \
  -H "Content-Type: application/json" -d '{"count":10,"timeout_ms":1000}'

# 4) 发一帧并等响应，直接拿往返时延
curl -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/transact \
  -H "Content-Type: application/json" \
  -d '{"frame":{"id":"0x18FF50E5","data":"0102","is_extended_id":true},
       "expect":{"can_id":"0x18FF50E6","timeout_ms":200}}'
```

## 接口一览

所有业务接口挂在 `/api/v1` 下，响应统一包装：

```json
{"ok": true,  "data": {...}}
{"ok": false, "error": {"code": "timeout", "message": "...", "detail": {...}}, "path": "..."}
```

| 分类 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 系统 | GET | `/health` | 存活 + 版本 + WS 订阅数 |
| | GET | `/info` | 平台、python-can 版本、默认后端 |
| | GET | `/interfaces` | 各后端能力矩阵与可导入性 |
| | GET | `/channels?interface=pcan` | 枚举通道 |
| | GET | `/probe` | 硬件自检（等价 `cantest probe`） |
| | GET | `/stats` | 全局统计（会话数、总帧数） |
| 总线 | POST | `/buses` | 打开通道 |
| | GET | `/buses` / `/buses/{id}` | 列表 / 详情 |
| | DELETE | `/buses/{id}` | 关闭 |
| | GET | `/buses/{id}/stats` | 帧数、字节数、错误、帧率、总线负载 |
| | GET | `/buses/{id}/capabilities` | 该会话后端支持哪些能力 |
| | POST | `/buses/{id}/reset` | Bus-Off / Error-Passive 复位 |
| | POST | `/buses/{id}/flush` | 清本地历史缓冲 |
| 过滤 | GET/PUT/DELETE | `/buses/{id}/filters` | 硬件 + 软件双重过滤（ID + 掩码） |
| 收发 | POST | `/buses/{id}/send` | 发单帧 |
| | POST | `/buses/{id}/send/batch` | 批量发，可带帧间隔 |
| | POST | `/buses/{id}/receive` | **消费式**读帧 |
| | POST | `/buses/{id}/expect` | 等帧断言，**不消费**游标 |
| | POST | `/buses/{id}/transact` | 发送 + 等响应 + 往返时延 |
| | GET | `/buses/{id}/frames` | 历史帧（RX+TX 合并，可按 ID / 时间过滤） |
| 周期 | POST | `/buses/{id}/cyclic` | 起周期发送（多帧序列、有限次数） |
| | GET | `/buses/{id}/cyclic` | 列表（含实测周期与抖动） |
| | DELETE | `/buses/{id}/cyclic[/{task_id}]` | 停止，响应里带回最终统计 |
| 录制 | POST | `/buses/{id}/record/start` `/stop` | 录制到 jsonl / csv / asc / blf |
| | GET | `/buses/{id}/record` | 录制状态 |
| 回放 | POST | `/buses/{id}/replay` | 按原始时间戳或固定间隔回放 |
| | GET | `/buses/{id}/replay` | 任务列表 |
| | DELETE | `/buses/{id}/replay[/{job_id}]` | 停止 |
| 实时 | WS | `/ws/buses/{id}` | 帧流推送，可在 URL 上带过滤条件 |

## 必须知道的几个语义

这几条是踩过坑之后定下来的，用错会得到「看起来对、其实错」的测试结论。

**1. `receive` 消费游标，`expect` 不消费。**
`receive` 每读一次就前进，同一帧不会被读第二次；`expect` 只是旁路观察，不影响后续 `receive`。
断言场景用 `expect`，取数据场景用 `receive`，两者可以混用。

**2. 「先发后等」优先用 `transact`。**
分成 `/send` + `/expect` 两次 HTTP 调用，中间有时间窗口，响应帧可能在 `expect` 注册等待器之前就落地了。
`transact` 在同一个调用里先记水位再发帧，没有这个窗口。

跨调用确实要拆开时，用 `seq` 兜底：`/send` 的响应信封顶层带 `seq`（发送前的 RX 水位），
把它原样传给 `/expect` 的 `since_seq`，就只认这个水位之后到达的帧。

```json
// POST /send 的响应
{"ok": true, "data": {"id": 387, ...}, "seq": 1024}

// POST /expect 的请求
{"can_id": 387, "timeout_ms": 500, "since_seq": 1024}
```

**3. 单位统一。** 接口里的 `timeout_ms`、`period_ms`、`gap_ms` 都是**毫秒**；
库内部的 `BusSession.expect(timeout_s=...)` 是**秒**。HTTP 层不做隐式换算猜测。

**4. 数据写法宽松。** `"0102"`、`"01 02"`、`"0x01,0x02"`、`[1,2]`、`b"\x01\x02"` 都等价；
返回体里同时给 `data`（连续 hex）和 `data_bytes`（整数数组），hex 便于人看，数组便于程序比对。

**5. 停止即摘除。** `DELETE /cyclic/{id}`、`DELETE /replay/{id}` 会把任务从表里移除，
最终统计在**停止接口的响应体**里返回（`finished` 字段）。`GET` 只列还活着的，不会挂着一条
「已停止」的任务误导你。

## WebSocket 帧流

```
ws://127.0.0.1:8700/api/v1/ws/buses/{bus_id}?can_id=0x181&id_mask=0x7FF&data=1122
```

连接建立先收到 `hello`，之后每帧一条消息。三种 `type`：

```jsonc
// 1) 帧（字段收在 frame 里，和信封字段分开，靠 type 一个字段就能分发）
{"ok":true,"data":{"type":"frame","bus_id":"pcan-usbbus1-1",
  "frame":{"id":387,"id_hex":"0x181","data":"0102","data_bytes":[1,2],
           "dlc":2,"length":2,"is_extended_id":false,"is_fd":false,
           "is_rx":true,"timestamp":1763000000.123,"channel":"PCAN_USBBUS1"}}}

// 2) 保活：客户端发 "ping" → 回 pong
{"ok":true,"data":{"type":"pong","ts":1763000000.5}}

// 3) 会话被关闭
{"ok":true,"data":{"type":"bus_closed"}}
```

URL 上的过滤条件在**服务端**生效，无关帧根本不进网络，适合高频总线上只看关心的几个 ID。
会话不存在时先回一条 `{"ok":false,...}` 再以 `4404` 关闭。

## 位定时

CAN 控制器的位时间由时间份额组成：

```
一个位 = SYNC_SEG(1 TQ) + TSEG1 + TSEG2
TQ      = BRP / f_clock
bitrate = f_clock / (BRP × (1 + TSEG1 + TSEG2))
采样点  = (1 + TSEG1) / (1 + TSEG1 + TSEG2)
```

`f_clock=80 MHz` 下解出的常用组合（`cantest.timing.PRESETS_80MHZ`，开箱即用）：

| 仲裁段 | BRP | TSEG1 | TSEG2 | TQ 总数 | 采样点 |
| --- | --- | --- | --- | --- | --- |
| 1000 kbit/s | 1 | 63 | 16 | 80 | 80.0% |
| 800 kbit/s | 1 | 79 | 20 | 100 | 80.0% |
| 500 kbit/s | 1 | 127 | 32 | 160 | 80.0% |
| 250 kbit/s | 2 | 139 | 20 | 160 | 87.5% |
| 125 kbit/s | 4 | 139 | 20 | 160 | 87.5% |
| 100 kbit/s | 5 | 139 | 20 | 160 | 87.5% |
| 50 kbit/s | 10 | 139 | 20 | 160 | 87.5% |

| FD 数据段 | BRP | TSEG1 | TSEG2 | TQ 总数 | 采样点 |
| --- | --- | --- | --- | --- | --- |
| 5000 kbit/s | 1 | 11 | 4 | 16 | 75.0% |
| 4000 kbit/s | 1 | 14 | 5 | 20 | 75.0% |
| 2000 kbit/s | 1 | 31 | 8 | 40 | 80.0% |
| 1000 kbit/s | 2 | 31 | 8 | 40 | 80.0% |

`1 Mbps` 这组正好是 80 TQ、采样点 80%，符合 CiA 推荐值。求解器并非只查表：
任意波特率都会反解，且**要求整除**（不能整除就报错，而不是悄悄给一个偏了 2% 的波特率）。

**关于经典帧的实话**：PEAK 的经典 CAN 走驱动内置 BTR 表，由 `bitrate` 一步初始化，
我们算出的 `TSEG1=63` 只是**校验用**的参考值（响应里 `timing.source = "driver_table"` 标明了这点）；
只有 FD 模式才把我们算出的 `nom_* / data_*` 参数真正下发给驱动。不写清楚这点，
后面看到「配置说 80 TQ、驱动实际不是」会很难查。

## 后端能力矩阵

| 后端 | FD | 硬件过滤 | 只监听 | 错误帧 | 位定时 | 平台 |
| --- | --- | --- | --- | --- | --- | --- |
| `pcan` | ✓ | ✓ | ✓ | ✓ | ✓ | Windows / Linux |
| `socketcan` | ✓ | ✓ | 需 `ip link` 预设 | ✓ | ✓ | Linux |
| `kvaser` | ✓ | ✓ | ✓ | ✓ | ✗ | Windows / Linux |
| `vector` | ✓ | ✓ | ✓ | ✓ | ✗ | Windows |
| `virtual` | ✓ | ✗ | ✗ | ✗ | ✗ | 全平台（无硬件） |

`interface: "auto"` 时按平台和已装 DLL 推断：Windows 优先 PCAN → Kvaser → Vector → 虚拟；
Linux 走 socketCAN。平台侧可以先查 `/buses/{id}/capabilities` 再决定用例能不能跑。

## 错误码

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `bad_request` | 参数本身不合法（如 `data_mask` 不配 `data`、停止不存在的任务） |
| 404 | `bus_not_found` | 会话不存在或已关闭 |
| 408 | `timeout` | `expect` / `transact` 等不到帧（`detail` 里带已收到的帧） |
| 409 | `channel_in_use` | 同一物理通道被两个会话占用 |
| 422 | `validation_failed` | 结构校验失败（如 9 字节经典帧、`data_bitrate` 没开 FD） |
| 501 | `unsupported_feature` | 当前后端没这个能力（如虚拟总线不支持 `reset`） |
| 502 | `send_failed` | 驱动拒绝发送 |
| 503 | `bus_off` / `hardware_unavailable` | 总线 Bus-Off 且自动复位失败 / 打不开硬件 |

## 测试

```bash
# 无硬件，173 例通过（真实硬件用例 13 例 skip），约 25 秒
.venv\Scripts\python -m pytest tests -q

# 真实硬件，只监听（不发帧，安全）
set CANTEST_HW=1 && .venv\Scripts\python -m pytest tests/test_hw_pcan.py -v

# 额外验证发送路径——注意：会在总线上真发帧，别在役总线上跑
set CANTEST_HW=1 && set CANTEST_HW_TX=1 && .venv\Scripts\python -m pytest tests/test_hw_pcan.py -v
```

Linux 把 `.venv\Scripts\python` 换成 `.venv/bin/python`，`set X=1` 换成 `X=1` 前缀即可。

- `tests/test_timing.py`：位定时求解的不变式（`brp × tq_total` 必须等于一个位的时钟数）
- `tests/test_frame.py` / `test_models.py`：数据编解码、帧匹配、请求模型边界
- `tests/test_bus_virtual.py`：virtual 后端端到端（收发、expect、transact、周期、录制回放）
- `tests/test_api.py`：走真实 ASGI，把依赖注入、422 校验、异常→状态码映射一起验掉
- `tests/test_hw_pcan.py`：真实硬件，默认 skip

### 实测结果（PCAN-USB Pro FD / 1 Mbps / 80 MHz）

```
$ python -m pytest tests -q                          → 173 passed, 13 skipped in 25.54s
$ CANTEST_HW=1 python -m pytest tests/test_hw_pcan.py -v
                                                     → 11 passed, 2 skipped in 4.45s
```

监听链路上的真实数据：3 秒收到 **1489 帧**（496 fps），11 个唯一 ID，全部扩展帧、DLC 8，
错误帧 0、bus-off 0、`last_error` 空，状态 `ACTIVE`。**位定时算错是不可能收到这些帧的**，
所以「80 MHz / 1 Mbps 配置有效」这条由实测流量背书，而不是由配置回显背书。

## 安全提醒

现场那条总线上跑的是**活的 J1939 报文**（`0x18xx0Axx` 一族，源地址 0x19/0x1A/0x1E…）。
在役总线上随便发帧可能触发执行器动作或让 ECU 记故障码，因此：

- 首次接触一律 `listen_only: true`（不产生 ACK，纯旁听）；
- 发送类用例用 `CANTEST_HW_TX=1` 显式开启，默认跑不了；
- 需要真发帧做联调时，优先用独立的测试总线或双通道自环，不要在车上试。

## 代码结构

```
cantest/
├── timing.py      位定时求解（含 80 MHz 预置表）
├── frame.py       数据编解码、Message↔dict、帧匹配器、位长估算
├── models.py      pydantic 请求/响应模型（帧、过滤、配置、统计）
├── errors.py      统一错误类型 → HTTP 状态码
├── backends.py    后端探测、通道枚举、参数装配、打开总线
├── bus.py         BusSession：RX 线程 + 环形缓冲 + 统计 + 断言等待
├── cyclic.py      周期发送（粗睡 + 自旋，亚毫秒抖动）
├── recorder.py    录制 / 回放（jsonl、csv、asc、blf）
├── manager.py     多会话管理、通道占用检测
├── hub.py         WebSocket 帧广播（线程 → 事件循环）
├── api/           FastAPI 路由：system / bus / frame / async / ws
└── __main__.py    CLI：serve / probe
```

关键设计只有两条：**RX 线程独占阻塞 `recv()`**（绝不进事件循环），
以及**断言用序号而不是时间戳**（时间戳有精度和时钟漂移问题，序号是单调的）。
