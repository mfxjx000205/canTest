"""前端配套的端到端冒烟：按页面上按钮的顺序把接口走一遍。"""
import asyncio
import json
import time

import httpx

B = "http://127.0.0.1:8791/api/v1"


def call(method, path, body=None):
    r = httpx.request(method, B + path, json=body, timeout=15.0)
    d = r.json()
    if not d.get("ok"):
        raise SystemExit(f"FAIL {method} {path} -> {r.status_code} {json.dumps(d, ensure_ascii=False)}")
    return d


# ---- 1. 打开两条会话（同一 virtual 通道） ----
A = call("POST", "/buses", {"interface": "virtual", "channel": "ui-demo", "listen_only": False})["data"]
Bs = call("POST", "/buses", {"interface": "virtual", "channel": "ui-demo", "listen_only": False})["data"]
aid, bid = A["id"], Bs["id"]
print(f"1) 会话 opened: {aid} / {bid}")
print(f"   位定时: brp={A['timing'].get('brp')} tseg1={A['timing'].get('tseg1')} tseg2={A['timing'].get('tseg2')}")

# ---- 2. WebSocket 实时流 ----
async def ws_round(sends):
    import websockets
    url = f"ws://127.0.0.1:8791/api/v1/ws/buses/{bid}"
    got = []
    async with websockets.connect(url) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert hello["data"]["type"] == "hello", hello
        await asyncio.sleep(0.15)
        for f in sends:
            call("POST", f"/buses/{aid}/send", f)
        deadline = time.time() + 4
        while time.time() < deadline and len(got) < len(sends):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
            except asyncio.TimeoutError:
                break
            if msg["data"]["type"] == "frame":
                got.append(msg["data"]["frame"])
    return got


frames = asyncio.run(ws_round([
    {"id": "0x181", "data": "01020304"},
    {"id": "0x182", "data": "AABBCC", "is_extended_id": False},
]))
print(f"2) WS 收到 {len(frames)} 帧")
for f in frames:
    print(f"   {f['id_hex']} data={f['data']} rx={f['is_rx']} ext={f['is_extended_id']} dlc={f['dlc']}")

# ---- 3. 过滤器：只留 0x181 ----
call("POST", f"/buses/{bid}/flush")
r = call("PUT", f"/buses/{bid}/filters", {"filters": [{"can_id": "0x181", "id_mask": "0x7FF"}]})
print(f"3) 过滤器已设置，硬件生效={r['data']['hw_filters_active']}")
call("POST", f"/buses/{aid}/send", {"id": "0x181", "data": "11"})
call("POST", f"/buses/{aid}/send", {"id": "0x182", "data": "22"})
time.sleep(0.3)
got = call("POST", f"/buses/{bid}/receive", {"count": 50, "timeout_ms": 300})["data"]
ids = sorted({f["id_hex"] for f in got["frames"]})
print(f"   过滤后收到 {got['count']} 帧，ID={ids}")
assert ids in ([], ["0x181"]), f"过滤没生效，收到了 {ids}"

# ---- 4. 录制 ----
info = call("POST", f"/buses/{bid}/record/start", {"format": "jsonl"})["data"]
print(f"4) 录制开始 -> {info['path']}")
for i in range(5):
    call("POST", f"/buses/{aid}/send", {"id": "0x181", "data": f"0{i}1122334455"})
time.sleep(0.2)
stopped = call("POST", f"/buses/{bid}/record/stop")["data"]
print(f"   录制停止：{stopped['frames']} 帧 / {stopped['bytes']} 字节")

files = call("GET", "/recordings")["data"]
print(f"   录制列表 {files['count']} 个文件，最新 {files['files'][0]['name'] if files['files'] else '无'}")
assert files["count"] >= 1

# ---- 5. 回放 ----
target = stopped["path"]
job = call("POST", f"/buses/{bid}/replay", {"path": target, "speed": 100, "count": 1})["data"]
time.sleep(0.6)
jobs = call("GET", f"/buses/{bid}/replay")["data"]["jobs"]
print(f"5) 回放 {job['id']}：已发 {jobs[0]['sent'] if jobs else '?'} 帧，running={jobs[0]['running'] if jobs else '?'}")
call("DELETE", f"/buses/{bid}/replay")

# ---- 6. 事务：让 B 侧周期回 0x7DF，A 侧发 0x7E0 等应答 ----
call("POST", f"/buses/{bid}/cyclic", {"frames": [{"id": "0x7DF", "data": "0102030405060708"}], "period_ms": 100})
time.sleep(0.2)
t = call("POST", f"/buses/{aid}/transact", {
    "frame": {"id": "0x7E0", "data": "0201"},
    "expect": {"can_id": "0x7DF", "timeout_ms": 1000, "count": 3},
    "repeat": 1,
})
print(f"6) transact 收到 {t['data']['count']} 帧，往返 {t['data']['latency']}")
call("DELETE", f"/buses/{bid}/cyclic")

# ---- 7. 统计 & 收尾 ----
st = call("GET", f"/buses/{bid}/stats")["data"]
print(f"7) {bid} state={st['state']} rx={st['stats']['rx_frames']} tx={st['stats']['tx_frames']} "
      f"fps={st['stats']['rx_rate_fps']:.1f} load={st['stats']['bus_load']:.3f}")
call("DELETE", f"/buses/{aid}/filters")
for x in (aid, bid):
    call("DELETE", f"/buses/{x}")
print("8) 会话已全部关闭")
print("\n全部通过")
