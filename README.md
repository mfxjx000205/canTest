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

先不接硬件，用 `virtual` 后端跑一遍——**能跑通说明装对了**，再换真机。

```bash
python -m cantest serve --port 8700
```

```bash
# 1) 开一个虚拟通道
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"virtual","channel":"demo"}'
# → {"ok":true,"data":{"id":"virtual-demo-1", ...}}

# 2) 发一帧（ID 写 0x 字符串即可）
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/virtual-demo-1/send \
  -H "Content-Type: application/json" \
  -d '{"id":"0x181","data":"1102"}'

# 3) 读回来（消费式；虚拟总线会广播回自己）
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/virtual-demo-1/receive \
  -H "Content-Type: application/json" -d '{"count":10,"timeout_ms":1000}'

# 4) 统计
curl -s http://127.0.0.1:8700/api/v1/buses/virtual-demo-1/stats
```

换成真机只改第 1 步的 `interface` / `channel`：

```bash
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"pcan","channel":"PCAN_USBBUS1","bitrate":1000000,
       "f_clock_mhz":80,"sample_point":0.8,"listen_only":true}'
# → {"ok":true,"data":{"id":"pcan-usbbus1-1", ...}}
```

`listen_only=true` 表示**不发 ACK、不发错误帧**，对在役总线只读。
后续用返回的 `data.id` 替换 URL 里的 `virtual-demo-1`。

真机上「发一帧等响应」用 `transact`，一次调用拿到往返时延：

```bash
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/transact \
  -H "Content-Type: application/json" \
  -d '{"frame":{"id":"0x601","data":"3E00"},
       "expect":{"can_id":"0x581","timeout_ms":200}}'
```

这需要总线上**真有 ECU 应答** `0x581`（UDS TesterPresent 的典型问答）；没有对端就会
`408 timeout`，那是断言在正常工作，不是接口坏了。

完整的测试路径（含双通道自环验证发送）见 [怎么测：三条递进路径](#怎么测三条递进路径)。

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
| | GET | `/buses/{id}/stats` | 帧数、字节数、错误、帧率、总线负载（`0~1` 比例） |
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

**2.1 `expect` 默认只认「调用之后到达」的帧，这是故意的。**

`include_buffer` 默认 `false`。所以下面这种写法**一定超时**——不是接口坏了：

```bash
# 先发（帧立刻落到 B 的缓冲）
curl -s -X POST .../buses/A/send -d '{"id":"0x18FF50E5","data":"0102"}'
# 再等（等待器注册时，帧早就到了，默认语义认为「这是过去的事」）
curl -s -X POST .../buses/B/expect -d '{"can_id":"0x18FF50E5","timeout_ms":1000}'
# → 408 timeout，received 0/1
```

默认值**不能**改成「顺手查历史」：那样上一轮用例的残留帧会让这一轮断言假通过，而
「假通过」比「假失败」危险得多——测试平台的结论会直接错。要一次性验证，显式加一个：

| 写法 | 语义 | 该用在哪 |
| --- | --- | --- |
| 默认 | 只认调用之后到达的帧 | 自动化用例（防假通过） |
| `"include_buffer": true` | 在已有历史帧里找 | 手工验证、手工排查 |
| `"since_seq": <send 返回的 seq>` | 只认该水位之后 | 拆成两次调用的自动化用例 |
| 用 `transact` | 同一次调用内先记水位再发 | **首选** |

**3. 单位统一。** 接口里的 `timeout_ms`、`period_ms`、`gap_ms` 都是**毫秒**；
库内部的 `BusSession.expect(timeout_s=...)` 是**秒**。HTTP 层不做隐式换算猜测。

**4. 数据写法宽松。** `"0102"`、`"01 02"`、`"0x01,0x02"`、`[1,2]`、`b"\x01\x02"` 都等价；
返回体里同时给 `data`（连续 hex）和 `data_bytes`（整数数组），hex 便于人看，数组便于程序比对。

**5. 停止即摘除。** `DELETE /cyclic/{id}`、`DELETE /replay/{id}` 会把任务从表里移除，
最终统计在**停止接口的响应体**里返回（`finished` 字段）。`GET` 只列还活着的，不会挂着一条
「已停止」的任务误导你。

**6. 时间戳统一是墙钟（epoch 秒）。** 这条踩得很疼：PCAN 驱动给的接收时间戳是
**开机相对时间**（实测 `7667.5`，不是 epoch），而发送侧是 `time.time()`（`1.79e9`）。
两个时钟域混在一起，症状全是「看起来对、其实错」——`history()` 合并收发后
「先发后收」被读成「先收后发」、`/frames?since=<epoch>` 把接收帧全过滤掉、
录制回放的帧间隔失真。所以：

- 接收帧按**首帧标定恒定偏移**归一到墙钟，保留驱动侧亚毫秒的相对精度；
  偏移失配超过 5 秒（驱动重启、系统改时间）自动重新标定。
- 发送帧（含周期发送、**回放**）的时间戳一律是**实际发出时刻**。
  回放要还原的是帧间隔（来自文件里的原始时间戳），不是绝对时刻——
  沿用旧时间戳会让刚回放的帧在 `?since=` 里查不到。
- `bus_load` 是 `0~1` 的**比例**（`0.076` = 7.6%），不是百分数。

**7. 过滤不追溯历史。** `PUT /filters` 只影响**之后到达**的帧，缓冲区里已有的帧照旧在，
`receive` 会先把它们吐出来。想验证「过滤真的生效」，设完过滤先 `POST /flush` 再收，
否则会把设过滤之前的帧当成漏网的帧。硬件过滤与软件过滤是两层，硬件不支持时软件兜底。

**8. ID 写 `0x` 字符串最省事。** `"0x18FF50E5"`、`"18FF50E5"`、`419385573` 三种写法等价，
`id` / `can_id` / `id_mask` 都吃。规则按 Python 字面量习惯，避免「181 是几进制」的猜测：

| 写法 | 解释成 | 结果 |
| --- | --- | --- |
| `"0x181"` | 带前缀 → 十六进制 | 385 |
| `"181"` | 纯数字串 → 十进制 | 181 |
| `"18FF50E5"` | 含字母，只能是十六进制 | 0x18FF50E5 |
| `419385573` | 整数，原样用 | 0x18FF50E5 |

要写十六进制就带 `0x`——这条规则是为了让 `"181"` 不被误读成 385。

## 怎么测：三条递进路径

按风险从低到高，每步都能独立验证。**不要一上手就往在役总线上发帧。**

### 路径 A：不接硬件，先确认装对了（零风险，2 分钟）

`virtual` 后端在进程内模拟总线，两个会话挂同一个 `channel` 就能互通。

```bash
python -m pytest tests -q          # 期望 195 passed, 14 skipped
python -m cantest serve --port 8700
```

```bash
# 开两个会话，挂同一个虚拟通道
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" -d '{"interface":"virtual","channel":"demo"}'
# → data.id = "virtual-demo-1"
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" -d '{"interface":"virtual","channel":"demo"}'
# → data.id = "virtual-demo-2"

# 一号发，二号断言收到
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/virtual-demo-1/send \
  -H "Content-Type: application/json" \
  -d '{"id":"0x18FF50E5","data":"0102030405060708","is_extended_id":true}'

# include_buffer 必须带上：帧在等待器注册前就到了（详见语义 2.1）
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/virtual-demo-2/expect \
  -H "Content-Type: application/json" \
  -d '{"can_id":"0x18FF50E5","timeout_ms":1000,"include_buffer":true}'
# → {"ok":true,"data":{"matched":true,"count":1,...}}
```

实测输出：

```
[1] A 发一帧（ID 用 0x 字符串）
    200 {"ok":true,"data":{"id":419385573,"id_hex":"0x18FF50E5","data":"0102030405060708",
         "is_extended_id":true,"is_rx":false,"timestamp":1794539158.909701},"seq":0}

[2] B 断言收到（include_buffer=true）
    200 {"ok":true,"data":{"matched":true,"count":1,
         "frames":[{"id_hex":"0x18FF50E5","is_rx":true,"channel":"demo",...}]}}

[2'] 同一个请求去掉 include_buffer
    408 {"ok":false,"error":{"code":"timeout",
         "message":"期望帧未在 1000 ms 内到齐（0/1）"}}      ← 预期行为，不是 bug
```

**注意**：`virtual` 后端会把帧**广播回发送者自己**，所以你会看到 `tx_frames` 和
`rx_frames` 一起涨（实测发 8 帧后两边都是 8）。真实硬件不会这样，除非显式开
`receive_own_messages=true`。

### 路径 B：接真机，只监听（不产生 ACK，不干扰总线）

```bash
python -m cantest probe                        # 先自检：DLL / 通道 / 驱动
```

确认能看到 `PCAN_USBBUS1` / `PCAN_USBBUS2` 后再开通道：

```bash
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"pcan","channel":"PCAN_USBBUS1","bitrate":1000000,
       "f_clock_mhz":80,"sample_point":0.8,"listen_only":true}'
```

`listen_only=true` 意味着**不发 ACK、不发错误帧**，对在役总线是只读的。然后：

```bash
# 帧率 / 总线负载 / 错误计数
curl -s http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/stats

# 读 10 帧
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/receive \
  -H "Content-Type: application/json" -d '{"count":10,"timeout_ms":1000}'
```

**这一档能验证的**：位定时算得对不对（`timing` 字段）、能不能收到帧、帧率是否合理、
有没有错误帧、过滤器是否生效。
**验证不了的**：发送路径。只监听模式下发帧是空操作。

### 路径 C：双通道自环，验证发送（唯一安全的发帧方式）

用一根双绞线把 `PCAN_USBBUS1` 和 `PCAN_USBBUS2` 直连（`CAN_H`↔`CAN_H`、
`CAN_L`↔`CAN_L`，两端 120 Ω 终端），一个口发、另一个口收——**完全绕开在役总线**。

```bash
# 发送口
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"pcan","channel":"PCAN_USBBUS1","bitrate":1000000,"f_clock_mhz":80}'
# → pcan-usbbus1-1

# 接收口
curl -s -X POST http://127.0.0.1:8700/api/v1/buses \
  -H "Content-Type: application/json" \
  -d '{"interface":"pcan","channel":"PCAN_USBBUS2","bitrate":1000000,"f_clock_mhz":80}'
# → pcan-usbbus2-1

# 1 号口发
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus1-1/send \
  -H "Content-Type: application/json" -d '{"id":"0x181","data":"0102"}'

# 2 号口收——收到就说明位定时、发送、接收整条链路都对
curl -s -X POST http://127.0.0.1:8700/api/v1/buses/pcan-usbbus2-1/expect \
  -H "Content-Type: application/json" -d '{"can_id":"0x181","timeout_ms":1000}'
```

如果 2 号口收不到，按这个顺序查：终端电阻（两端各 120 Ω，不是一端）→ 两个口的
`bitrate` 是否一致 → `timing` 里的 `brp/tseg1/tseg2` 是否相同 → 线序是否反了。

跑通之后再开自动化测试：

```bash
set CANTEST_HW=1 && set CANTEST_HW_TX=1 && python -m pytest tests/test_hw_pcan.py -v
```

### 平台接入

测试平台把 CANtest 当外挂设备挂进来只需要两件事：

```python
from cantest.api.app import create_app
app = create_app()          # 或 app.mount("/cantest", create_app())
```

- 基址 `/api/v1`，健康检查 `GET /api/v1/health`（返回 `{"ok":true,"data":{"status":"ok"}}`）
- 所有响应统一 `{"ok":true,"data":...}` / `{"ok":false,"error":{"code":...}}`，
  平台侧按 `ok` 判成败、按 `error.code` 分类，不用去猜 HTTP 状态码

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
# 无硬件，195 例通过（真实硬件用例 14 例 skip），约 23 秒
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
$ python -m pytest tests -q                          → 195 passed, 14 skipped in 23.33s
$ CANTEST_HW=1 python -m pytest tests/test_hw_pcan.py -v
                                                     → 12 passed, 2 skipped in 4.34s
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
