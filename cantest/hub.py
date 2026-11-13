"""WebSocket 帧广播。

RX 线程不是 asyncio 线程，不能直接碰 ``asyncio.Queue``，所以统一通过
``loop.call_soon_threadsafe`` 把投递动作调度回事件循环。

没有订阅者时直接丢弃，避免每帧都做一次字典转换。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import can

from .frame import FrameMatcher, message_to_dict

__all__ = ["FrameHub"]


class FrameHub:
    def __init__(self, max_queue: int = 2000) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: Dict[str, Set[Tuple[asyncio.Queue, Optional[FrameMatcher]]]] = {}
        self._lock = threading.Lock()
        self._max_queue = max_queue
        self._dropped = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---- 订阅 ---------------------------------------------------------
    def subscribe(
        self, bus_id: str, matcher: Optional[FrameMatcher] = None
    ) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.setdefault(bus_id, set()).add((queue, matcher))
        return queue

    def unsubscribe(self, bus_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            entries = self._subs.get(bus_id)
            if not entries:
                return
            for item in list(entries):
                if item[0] is queue:
                    entries.discard(item)
            if not entries:
                self._subs.pop(bus_id, None)

    def subscriber_count(self, bus_id: str) -> int:
        with self._lock:
            return len(self._subs.get(bus_id, ()))

    def total_subscribers(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._subs.values())

    # ---- 投递 ---------------------------------------------------------
    def publish_message(self, bus_id: str, msg: can.Message) -> None:
        """由 RX 线程调用。"""
        with self._lock:
            entries = list(self._subs.get(bus_id, ()))
        if not entries:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            # 帧字段收进 frame 子对象，和 type / bus_id 这些信封字段分开，
            # 客户端靠 type 一个字段就能分帧，帧内部字段也不会和信封撞名。
            payload = {
                "type": "frame",
                "bus_id": bus_id,
                "frame": message_to_dict(msg),
            }
        except Exception:  # noqa: BLE001
            return
        loop.call_soon_threadsafe(self._dispatch, entries, payload)

    def publish_event(self, bus_id: str, event: Dict[str, Any]) -> None:
        with self._lock:
            entries = list(self._subs.get(bus_id, ()))
        loop = self._loop
        if not entries or loop is None or loop.is_closed():
            return
        payload = dict(event)
        payload["bus_id"] = bus_id
        payload["type"] = payload.get("type", "event")
        loop.call_soon_threadsafe(self._dispatch, entries, payload)

    def _dispatch(
        self,
        entries: List[Tuple[asyncio.Queue, Optional[FrameMatcher]]],
        payload: Dict[str, Any],
    ) -> None:
        for queue, matcher in entries:
            if matcher is not None:
                # 订阅时带了过滤条件，用轻量字段比对即可
                if not _match_payload(matcher, payload):
                    continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._dropped += 1
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


def _match_payload(matcher: FrameMatcher, payload: Dict[str, Any]) -> bool:
    if payload.get("type") == "event":
        return True
    # 帧事件把字段收在 frame 里，事件本身是扁平的，两种都兼容
    frame: Dict[str, Any] = payload.get("frame") or payload
    if frame.get("is_error_frame") and not matcher.allow_error_frames:
        return False
    if matcher.can_id is not None:
        mask = matcher.id_mask
        if mask is None:
            mask = 0x1FFFFFFF if frame.get("is_extended_id") else 0x7FF
        if (frame.get("id", 0) & mask) != (matcher.can_id & mask):
            return False
    if matcher.data is not None:
        data = bytes(frame.get("data_bytes") or [])
        if len(data) < len(matcher.data):
            return False
        for idx, expected in enumerate(matcher.data):
            m = matcher.data_mask[idx] if matcher.data_mask and idx < len(matcher.data_mask) else 0xFF
            if (data[idx] & m) != (expected & m):
                return False
    return True
