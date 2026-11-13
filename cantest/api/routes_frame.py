"""收发与断言接口——自动测试平台调用最频繁的一组。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from ..errors import OperationTimeout
from ..frame import FrameMatcher
from ..manager import BusManager
from ..models import (
    BatchSendRequest,
    ExpectRequest,
    Frame,
    FrameSpec,
    ReceiveRequest,
    TransactRequest,
)
from .common import build_matcher, get_manager, ok

router = APIRouter(prefix="/buses", tags=["frame"])


def _frames(messages) -> list:
    return [Frame.from_message(msg) for msg in messages]


@router.post("/{bus_id}/send", summary="发送单帧")
def send_frame(
    bus_id: str, spec: FrameSpec, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    # 先记 RX 水位再发帧：响应体里带出去的 seq 可以作为 /expect 的 since_seq，
    # 这样「先发后等」不会因为跨两次 HTTP 调用漏掉中间到达的响应帧。
    seq_before = session.current_seq()
    msg = session.send(spec)
    return ok(Frame.from_message(msg), seq=seq_before)


@router.post("/{bus_id}/send/batch", summary="批量发送（可带帧间隔）")
def send_batch(
    bus_id: str, req: BatchSendRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    seq_before = session.current_seq()
    t0 = time.perf_counter()
    sent = session.send_many(req.frames, req.interval_ms)
    return ok(
        {
            "sent": sent,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
        },
        seq=seq_before,
    )


@router.post("/{bus_id}/receive", summary="消费式读取新帧")
def receive_frames(
    bus_id: str, req: ReceiveRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    matcher: Optional[FrameMatcher] = None
    if req.filter is not None:
        matcher = FrameMatcher(
            can_id=req.filter.can_id,
            id_mask=req.filter.id_mask,
            is_extended_id=req.filter.is_extended_id,
        )
    t0 = time.perf_counter()
    messages = session.receive(req.count, req.timeout_ms / 1000.0, matcher)
    return ok(
        {
            "frames": _frames(messages),
            "count": len(messages),
            "complete": len(messages) >= req.count,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
        }
    )


@router.post("/{bus_id}/expect", summary="等待满足条件的帧（断言）")
def expect_frame(
    bus_id: str, req: ExpectRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    matcher = build_matcher(req)
    t0 = time.perf_counter()
    messages = session.expect(
        matcher,
        req.timeout_ms / 1000.0,
        req.count,
        since_seq=req.since_seq,
        include_buffer=req.include_buffer,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    matched = len(messages) >= req.count
    if not matched and req.fail_fast:
        raise OperationTimeout(
            f"期望帧未在 {req.timeout_ms:.0f} ms 内到齐（{len(messages)}/{req.count}）",
            detail={
                "expected": {
                    "can_id": req.can_id,
                    "id_mask": req.id_mask,
                    "data": req.data,
                    "data_mask": req.data_mask,
                },
                "received": _frames(messages),
                "elapsed_ms": round(elapsed_ms, 3),
            },
        )
    return ok(
        {
            "matched": matched,
            "frames": _frames(messages),
            "count": len(messages),
            "elapsed_ms": round(elapsed_ms, 3),
        }
    )


@router.post("/{bus_id}/transact", summary="发送并等待响应，返回往返时延")
def transact(
    bus_id: str, req: TransactRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    matcher = build_matcher(req.expect)
    try:
        messages, latencies = session.transact(
            req.frame,
            matcher,
            req.expect.timeout_ms / 1000.0,
            req.expect.count,
            req.repeat,
        )
    except OperationTimeout as exc:
        exc.detail = dict(exc.detail or {})
        exc.detail["sent"] = req.frame.model_dump()
        raise

    return ok(
        {
            "sent": req.frame,
            "responses": _frames(messages),
            "count": len(messages),
            "latency_ms": [round(v, 3) for v in latencies],
            "latency": {
                "min": round(min(latencies), 3),
                "max": round(max(latencies), 3),
                "avg": round(sum(latencies) / len(latencies), 3),
            }
            if latencies
            else None,
        }
    )


@router.get("/{bus_id}/frames", summary="查询历史帧")
def frame_history(
    bus_id: str,
    limit: int = Query(default=200, ge=1, le=10000),
    since: Optional[float] = Query(default=None, description="Unix 时间戳，只返回其后的帧"),
    can_id: Optional[int] = Query(default=None),
    id_mask: Optional[int] = Query(default=None),
    is_extended_id: Optional[bool] = Query(default=None),
    manager: BusManager = Depends(get_manager),
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    matcher = None
    if can_id is not None:
        matcher = FrameMatcher(
            can_id=can_id, id_mask=id_mask, is_extended_id=is_extended_id
        )
    messages = session.history(limit, matcher, since)
    return ok({"frames": _frames(messages), "count": len(messages)})
