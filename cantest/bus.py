"""总线会话：一个会话 = 一条打开的 CAN 通道 + 一个 RX 线程 + 一组统计。

关键设计：

- **RX 线程独占 recv()**。python-can 的 ``recv`` 是阻塞调用，绝不能放进 FastAPI 的
  事件循环；所有帧先进环形缓冲，再由 WS/HTTP 侧按需消费。
- **过滤器双保险**。硬件过滤器交给驱动（省 CPU），同时保留软件过滤，保证
  「设了过滤就一定生效」，不受各后端能力差异影响。
- **断言用序号而不是时间戳**。发送方记录当时的帧序号，``expect`` 只认之后的帧，
  避免把缓冲里的旧报文误判成响应。
"""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import can

from .backends import open_bus
from .cyclic import CyclicManager, CyclicTask
from .errors import (
    BadRequest,
    BusOffError,
    OperationTimeout,
    SendFailed,
    UnsupportedFeature,
)
from .frame import FrameMatcher, frame_bits
from .models import (
    BusConfig,
    BusInfo,
    BusStats,
    CyclicRequest,
    FilterSpec,
    FrameSpec,
    RecordInfo,
    RecordRequest,
    ReplayRequest,
)
from .recorder import Recorder, ReplayJob

__all__ = ["BusSession", "FrameWaiter"]

# python-can 4.6 把 bus-off 状态从 ``BusState.OFF`` 改名为 ``BusState.ERROR``，
# 这里做一次兼容映射，避免锁死在某个小版本上。
_OFF_STATE = getattr(can.BusState, "OFF", None) or getattr(can.BusState, "ERROR", None)

_STATE_MAP = {
    can.BusState.ACTIVE: "active",
    can.BusState.PASSIVE: "error-passive",
}
if _OFF_STATE is not None:
    _STATE_MAP[_OFF_STATE] = "bus-off"


class FrameWaiter:
    """一次性等待器：喂进来的帧满足条件就置位事件。"""

    def __init__(self, matcher: FrameMatcher, needed: int, preset: Optional[List[can.Message]] = None):
        self.matcher = matcher
        self.needed = needed
        self.collected: List[can.Message] = list(preset or [])
        self.event = threading.Event()
        if len(self.collected) >= needed:
            self.event.set()

    def feed(self, msg: can.Message) -> None:
        if len(self.collected) >= self.needed:
            return
        if self.matcher.matches(msg):
            self.collected.append(msg)
            if len(self.collected) >= self.needed:
                self.event.set()


@dataclass
class _Stats:
    tx_frames: int = 0
    rx_frames: int = 0
    tx_bytes: int = 0
    rx_bytes: int = 0
    error_frames: int = 0
    bus_off_count: int = 0
    tx_errors: int = 0
    rx_errors: int = 0
    last_error: Optional[str] = None
    tx_ts: Deque[float] = field(default_factory=deque)
    rx_ts: Deque[float] = field(default_factory=deque)
    load_win: Deque[Tuple[float, int]] = field(default_factory=deque)

    def prune(self, cutoff: float) -> None:
        for dq in (self.tx_ts, self.rx_ts):
            while dq and dq[0] < cutoff:
                dq.popleft()
        while self.load_win and self.load_win[0][0] < cutoff:
            self.load_win.popleft()


class BusSession:
    """一条已打开的总线通道。"""

    def __init__(
        self,
        session_id: str,
        config: BusConfig,
        on_frame: Optional[Callable[[can.Message], None]] = None,
    ) -> None:
        self.id = session_id
        self.config = config
        self.opened_at = time.time()

        self._bus, self.meta = open_bus(config)
        self.interface: str = self.meta["interface"]
        self.channel: str = str(self.meta.get("channel"))

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._waiters: List[FrameWaiter] = []
        # _frames 只放 RX 帧：发送的帧如果混进来，receive() 会把自己刚发的帧
        # 当成对端响应，测试结论直接失真。TX 单独记在 _tx_log 里。
        self._frames: Deque[Tuple[int, can.Message]] = deque(maxlen=config.buffer_size)
        self._tx_log: Deque[can.Message] = deque(maxlen=config.buffer_size)
        self._seq = 0
        self._recv_cursor = 0
        self._stats = _Stats()
        self._filters: List[FilterSpec] = list(config.filters)
        self._hw_filters_active = False
        self._last_state: Optional[can.BusState] = None
        self._last_poll = 0.0
        # 驱动时间戳与系统墙钟的偏移，首帧标定（见 _normalize_ts）
        self._ts_offset: Optional[float] = None

        self._stop = threading.Event()
        self._on_frame_cb = on_frame
        self._recorder = Recorder()
        self._cyclic = CyclicManager(self._cyclic_send)
        self._replay: Dict[str, ReplayJob] = {}

        self._apply_filters(self._filters)

        self._rx_thread = threading.Thread(
            target=self._rx_loop, name=f"cantest-rx-{session_id}", daemon=True
        )
        self._rx_thread.start()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._stop.is_set()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._cyclic.stop_all()
        for job in list(self._replay.values()):
            job.stop()
        self._replay.clear()
        self._recorder.stop()
        try:
            self._bus.shutdown()
        except Exception:  # noqa: BLE001 - 关闭阶段不再抛错
            pass
        self._rx_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # RX
    # ------------------------------------------------------------------
    def _rx_loop(self) -> None:
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.2)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                consecutive_errors += 1
                with self._lock:
                    self._stats.rx_errors += 1
                    self._stats.last_error = str(exc)
                if consecutive_errors >= 50:
                    break
                time.sleep(0.01)
                continue

            consecutive_errors = 0
            if msg is None:
                self._poll_state()
                continue
            self._on_frame(msg)

    def _on_frame(self, msg: can.Message) -> None:
        if not self._pass_filters(msg):
            return

        now = time.time()
        # 进到这里的帧必然是收来的，显式打标。
        # 不能指望后端自己设：PCAN 会设，但 virtual 后端是把发送方的 Message
        # 对象直接广播出去，is_rx 还是 False，于是「统计说收到了、载荷说是我发的」。
        msg.is_rx = True
        self._normalize_ts(msg, now)
        with self._lock:
            self._seq += 1
            self._frames.append((self._seq, msg))
            if msg.is_error_frame:
                self._stats.error_frames += 1
                self._stats.last_error = self._describe_error(msg)
            else:
                self._stats.rx_frames += 1
                self._stats.rx_bytes += len(msg.data or b"")
                self._stats.rx_ts.append(now)
            self._stats.load_win.append((now, frame_bits(msg)))
            self._condition.notify_all()
            for waiter in self._waiters:
                waiter.feed(msg)

        if self._recorder.active:
            self._recorder.write(msg)
        if self._on_frame_cb is not None:
            try:
                self._on_frame_cb(msg)
            except Exception:  # noqa: BLE001 - 回调失败不能影响 RX
                pass

    # 驱动时间戳与墙钟偏差超过这个值就重新标定（驱动复位、系统改时间）
    _TS_RESYNC_S = 5.0

    def _normalize_ts(self, msg: can.Message, now: float) -> None:
        """把驱动给的接收时间戳统一到系统墙钟。

        为什么必须做：PCAN 这类驱动给的是**开机相对时间**（实测 7667 s），
        而发送帧用的是 ``time.time()``（1.79e9），两个时钟域混一起就会出连锁问题——
        ``history()`` 合并收发后排序全乱、``frames?since=`` 过滤把接收帧全丢掉、
        录制回放算出来的帧间隔毫无意义。

        这里按首帧标定一个恒定偏移：既保留驱动侧亚毫秒的相对精度（时间差仍来自硬件），
        又让绝对时刻能和墙钟对齐。偏移失配超过 5 秒视为标定失效，重新标定。
        """
        raw = getattr(msg, "timestamp", None)
        if not raw or raw <= 0:
            msg.timestamp = now
            return
        if self._ts_offset is None or abs(raw + self._ts_offset - now) > self._TS_RESYNC_S:
            self._ts_offset = now - raw
        msg.timestamp = raw + self._ts_offset

    @staticmethod
    def _describe_error(msg: can.Message) -> str:
        text = msg.data.decode("ascii", errors="replace") if msg.data else ""
        return text.strip() or "error frame"

    def _poll_state(self) -> None:
        now = time.time()
        if now - self._last_poll < 1.0:
            return
        self._last_poll = now
        try:
            state = self._bus.state
        except Exception:  # noqa: BLE001 - 部分后端不支持读状态
            return

        if _OFF_STATE is not None and state == _OFF_STATE and self._last_state != _OFF_STATE:
            with self._lock:
                self._stats.bus_off_count += 1
                self._stats.last_error = "bus-off"
            if self.config.auto_recover:
                self.reset(silent=True)
        self._last_state = state

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------
    def _pass_filters(self, msg: can.Message) -> bool:
        # 错误帧始终放行，否则总线诊断信息会被过滤器吃掉
        if msg.is_error_frame:
            return True
        if not self._filters:
            return True
        for spec in self._filters:
            if spec.is_extended_id is not None and msg.is_extended_id != spec.is_extended_id:
                continue
            if (msg.arbitration_id & spec.id_mask) == (spec.can_id & spec.id_mask):
                return True
        return False

    def _apply_filters(self, filters: List[FilterSpec]) -> bool:
        """设置过滤器：先尽力下发给硬件，再作为软件过滤兜底。"""
        self._filters = list(filters)
        try:
            if filters:
                self._bus.set_filters([f.to_socketcan() for f in filters])
            else:
                self._bus.set_filters(None)
            self._hw_filters_active = True
        except (NotImplementedError, can.CanError, ValueError, TypeError, AttributeError):
            self._hw_filters_active = False
        return self._hw_filters_active

    def set_filters(self, filters: List[FilterSpec]) -> bool:
        with self._lock:
            return self._apply_filters(filters)

    def get_filters(self) -> List[FilterSpec]:
        with self._lock:
            return list(self._filters)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send(self, spec: FrameSpec) -> can.Message:
        msg = spec.to_message()
        self.transmit(msg)
        return msg

    def transmit(self, msg: can.Message) -> None:
        """把构造好的 ``can.Message`` 发上总线，并记账。

        周期发送、回放都走这里，否则统计、历史、录制、WS 三处会出现口径不一致：
        比如回放直接调底层总线时，``tx_frames`` 不会增长，测试平台对上账就会发现
        「回放发了 2 帧但统计说 0 帧」。

        时间戳也在这里统一成**实际发出时刻**。回放的消息带着录制文件里的旧时间戳，
        如果原样放行，``GET /frames?since=`` 就查不到刚回放的帧——明明发了却看不见，
        是典型的「看起来对、其实错」。回放自己的帧间隔另有来源（读文件时的原始时间戳），
        不受这里影响。
        """
        try:
            self._bus.send(msg, timeout=0.2)
        except can.CanError as exc:
            with self._lock:
                self._stats.tx_errors += 1
                self._stats.last_error = str(exc)
            raise SendFailed(f"发送失败（ID {msg.arbitration_id:#x}）：{exc}") from exc

        now = time.time()
        msg.timestamp = now
        with self._lock:
            self._stats.tx_frames += 1
            self._stats.tx_bytes += len(msg.data or b"")
            self._stats.tx_ts.append(now)
            self._stats.load_win.append((now, frame_bits(msg)))
            # 存副本：发送记账记的是「我发出去的样子」。某些后端（virtual）会把这个
            # Message 对象直接广播出去，接收侧打 is_rx 标记时会改到同一个对象，
            # 于是 tx_log 里的帧变成「收到」的，收发记录串味。
            self._tx_log.append(copy.copy(msg))

        if self._recorder.active:
            self._recorder.write(msg)
        # 推送给 WS，让上位机能同时看到 TX / RX 两个方向
        if self._on_frame_cb is not None:
            try:
                self._on_frame_cb(msg)
            except Exception:  # noqa: BLE001
                pass

    def _cyclic_send(self, spec: FrameSpec) -> None:
        self.send(spec)

    def send_many(self, frames: List[FrameSpec], interval_ms: float = 0.0) -> int:
        sent = 0
        for idx, spec in enumerate(frames):
            if idx and interval_ms > 0:
                time.sleep(interval_ms / 1000.0)
            self.send(spec)
            sent += 1
        return sent

    # ------------------------------------------------------------------
    # 接收 / 等待 / 事务
    # ------------------------------------------------------------------
    def receive(
        self,
        count: int = 1,
        timeout_s: float = 1.0,
        matcher: Optional[FrameMatcher] = None,
    ) -> List[can.Message]:
        """消费式读取：只返回游标之后的新帧（仅 RX 方向）。"""
        deadline = time.time() + timeout_s
        out: List[can.Message] = []
        with self._condition:
            while len(out) < count:
                for seq, msg in self._pending_from_cursor():
                    if matcher is not None and not matcher.matches(msg):
                        continue
                    out.append(msg)
                    self._recv_cursor = seq
                    if len(out) >= count:
                        break
                if len(out) >= count:
                    break
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self._condition.wait(min(remain, 0.05))
            # 即使没凑满也把游标推到最新，避免下次重复返回同一批帧
            if self._frames:
                self._recv_cursor = max(self._recv_cursor, self._frames[-1][0])
        return out

    def _pending_from_cursor(self) -> List[Tuple[int, can.Message]]:
        """取出游标之后的帧。seq 连续，用首帧序号直接算出下标，避免全量扫描。"""
        if not self._frames:
            return []
        first_seq = self._frames[0][0]
        start = max(0, self._recv_cursor - first_seq + 1)
        if start >= len(self._frames):
            return []
        return list(islice(self._frames, start, None))

    def expect(
        self,
        matcher: FrameMatcher,
        timeout_s: float = 1.0,
        count: int = 1,
        since_seq: Optional[int] = None,
        include_buffer: bool = False,
    ) -> List[can.Message]:
        """等待满足条件的帧，不消费游标。

        ``since_seq`` 为空且 ``include_buffer`` 为 False 时，只认调用之后到达的帧。
        """
        with self._lock:
            if include_buffer or since_seq is not None:
                cutoff = since_seq if since_seq is not None else 0
                preset = [m for seq, m in self._frames if seq > cutoff and matcher.matches(m)]
            else:
                preset = []
            preset = preset[:count]
            remaining = count - len(preset)
            if remaining <= 0:
                return preset
            waiter = FrameWaiter(matcher, remaining, preset)
            self._waiters.append(waiter)

        try:
            if timeout_s <= 0:
                waiter.event.wait(0)
            else:
                waiter.event.wait(timeout_s)
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

        return waiter.collected

    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    def transact(
        self,
        spec: FrameSpec,
        matcher: FrameMatcher,
        timeout_s: float = 1.0,
        count: int = 1,
        repeat: int = 1,
    ) -> Tuple[List[can.Message], List[float]]:
        """发送一帧并在超时内等响应，返回 (响应帧, 每次往返毫秒)。"""
        responses: List[can.Message] = []
        latencies: List[float] = []
        for _ in range(max(1, repeat)):
            seq_before = self.current_seq()
            t0 = time.perf_counter()
            self.send(spec)
            frames = self.expect(matcher, timeout_s, count, since_seq=seq_before)
            if len(frames) < count:
                raise OperationTimeout(
                    f"等待响应超时（{timeout_s * 1000:.0f} ms），已收到 {len(frames)}/{count} 帧",
                    detail={"can_id": spec.id, "received": len(frames), "expected": count},
                )
            latencies.append((time.perf_counter() - t0) * 1000.0)
            responses.extend(frames)
        return responses, latencies

    # ------------------------------------------------------------------
    # 历史 / 状态
    # ------------------------------------------------------------------
    def history(
        self,
        limit: int = 200,
        matcher: Optional[FrameMatcher] = None,
        since: Optional[float] = None,
    ) -> List[can.Message]:
        """按时间顺序返回最近的帧（RX + TX 合并）。"""
        with self._lock:
            rx_tail = list(islice(self._frames, max(0, len(self._frames) - limit), None))
            tx_tail = list(islice(self._tx_log, max(0, len(self._tx_log) - limit), None))
        merged = [msg for _seq, msg in rx_tail] + tx_tail
        merged.sort(key=lambda m: m.timestamp or 0.0)

        out: List[can.Message] = []
        for msg in reversed(merged):
            if since is not None and (msg.timestamp or 0) < since:
                break
            if matcher is not None and not matcher.matches(msg):
                continue
            out.append(msg)
            if len(out) >= limit:
                break
        out.reverse()
        return out

    def state(self) -> str:
        try:
            return _STATE_MAP.get(self._bus.state, str(self._bus.state))
        except Exception:  # noqa: BLE001
            return "unknown"

    def snapshot_stats(self) -> BusStats:
        now = time.time()
        cutoff = now - 1.0
        with self._lock:
            self._stats.prune(cutoff)
            bits = sum(b for _t, b in self._stats.load_win)
            bitrate = self.config.bitrate or 1
            return BusStats(
                tx_frames=self._stats.tx_frames,
                rx_frames=self._stats.rx_frames,
                tx_bytes=self._stats.tx_bytes,
                rx_bytes=self._stats.rx_bytes,
                error_frames=self._stats.error_frames,
                bus_off_count=self._stats.bus_off_count,
                tx_errors=self._stats.tx_errors,
                rx_errors=self._stats.rx_errors,
                tx_rate_fps=round(float(len(self._stats.tx_ts)), 2),
                rx_rate_fps=round(float(len(self._stats.rx_ts)), 2),
                bus_load=round(min(1.0, bits / bitrate), 4),
                last_error=self._stats.last_error,
            )

    def info(self) -> BusInfo:
        return BusInfo(
            id=self.id,
            interface=self.interface,
            channel=self.channel,
            state=self.state(),
            opened_at=self.opened_at,
            uptime_s=round(time.time() - self.opened_at, 3),
            config=self.config,
            timing=self.meta.get("timing") or {},
            data_timing=self.meta.get("data_timing"),
            filters=self.get_filters(),
            stats=self.snapshot_stats(),
            hw_filters_active=self._hw_filters_active,
        )

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def reset(self, silent: bool = False) -> bool:
        """从 Bus-Off / Error-Passive 恢复。"""
        # 注意 VirtualBus 这类后端根本没有 ``reset`` 属性（连 NotImplementedError
        # 都不抛），所以先判能力再看异常，否则会被兜底成 503。
        reset_fn = getattr(self._bus, "reset", None)
        if not callable(reset_fn):
            if silent:
                return False
            raise UnsupportedFeature(f"后端 {self.interface} 不支持总线复位")
        try:
            reset_fn()
            self._last_state = can.BusState.ACTIVE
            return True
        except NotImplementedError as exc:
            # 虚拟总线等后端没有复位能力，明确回 501，不要伪装成内部错误
            if silent:
                return False
            raise UnsupportedFeature(f"后端 {self.interface} 不支持总线复位") from exc
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._stats.last_error = str(exc)
            if silent:
                return False
            raise BusOffError(f"总线复位失败：{exc}") from exc

    def flush(self) -> int:
        """清空本地历史缓冲，返回清掉的帧数。"""
        with self._lock:
            dropped = len(self._frames)
            self._frames.clear()
            self._tx_log.clear()
            self._recv_cursor = self._seq
        return dropped

    # ------------------------------------------------------------------
    # 录制 / 回放
    # ------------------------------------------------------------------
    def start_record(self, req: RecordRequest) -> RecordInfo:
        return self._recorder.start(req.format, req.path, req.append, self.id)

    def stop_record(self) -> RecordInfo:
        return self._recorder.stop()

    def record_info(self) -> RecordInfo:
        return self._recorder.info()

    def start_replay(self, req: ReplayRequest) -> ReplayJob:
        # 用 transmit 而不是裸总线：回放的帧同样要计入 tx_frames、写历史、
        # 被录制器抓到、推给 WS，否则平台对账时两边数字对不上。
        job = ReplayJob(self.transmit, req, name=f"replay-{len(self._replay) + 1}")
        with self._lock:
            self._replay[job.id] = job
        job.start()
        return job

    def stop_replay(self, job_id: Optional[str] = None) -> int:
        with self._lock:
            jobs = (
                [self._replay[job_id]]
                if job_id and job_id in self._replay
                else list(self._replay.values())
            )
        for job in jobs:
            job.stop()
        return len(jobs)

    def replay_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [job.info() for job in self._replay.values()]

    # ------------------------------------------------------------------
    # 周期发送
    # ------------------------------------------------------------------
    def start_cyclic(self, req: CyclicRequest) -> CyclicTask:
        return self._cyclic.start(req)

    def stop_cyclic(self, task_id: Optional[str] = None) -> int:
        if task_id is None:
            return self._cyclic.stop_all()
        return 1 if self._cyclic.stop(task_id) else 0

    def cyclic_tasks(self) -> List[CyclicTask]:
        return self._cyclic.list()

    def cyclic_task(self, task_id: str) -> Optional[CyclicTask]:
        return self._cyclic.get(task_id)
