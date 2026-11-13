"""WebSocket 实时帧流。

    ws://127.0.0.1:8700/api/v1/ws/buses/{bus_id}?can_id=0x181&id_mask=0x7FF

订阅时可直接带 ID / 数据过滤条件，服务端在推流前就过滤掉无关帧，省带宽。
客户端发送 ``ping`` 会收到 ``pong``，用于保活探测。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..errors import CanTestError
from ..frame import FrameMatcher, parse_data
from ..hub import FrameHub
from ..manager import BusManager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/buses/{bus_id}")
async def ws_frames(
    websocket: WebSocket,
    bus_id: str,
    can_id: Optional[int] = Query(default=None, description="只看这个 ID"),
    id_mask: Optional[int] = Query(default=None, description="ID 掩码，用于位段匹配"),
    data: Optional[str] = Query(default=None, description="数据前缀（hex），如 1122"),
) -> None:
    await websocket.accept()

    manager: BusManager = websocket.app.state.manager
    hub: FrameHub = websocket.app.state.hub

    try:
        session = manager.get(bus_id)
    except CanTestError as exc:
        await websocket.send_json({"ok": False, "error": exc.to_payload()})
        await websocket.close(code=4404)
        return

    try:
        pattern = parse_data(data) if data else None
    except ValueError:
        await websocket.send_json(
            {"ok": False, "error": {"code": "bad_request", "message": "data 参数不是合法十六进制"}}
        )
        await websocket.close(code=4400)
        return

    matcher = (
        FrameMatcher(can_id=can_id, id_mask=id_mask, data=pattern)
        if (can_id is not None or pattern is not None)
        else None
    )
    queue = hub.subscribe(bus_id, matcher)

    await websocket.send_json(
        {
            "ok": True,
            "data": {
                "type": "hello",
                "bus_id": bus_id,
                "channel": session.channel,
                "state": session.state(),
                "filter": {"can_id": can_id, "id_mask": id_mask, "data": data},
                "server_time": time.time(),
            },
        }
    )

    async def sender() -> None:
        while True:
            if session.closed:
                await websocket.send_json({"ok": True, "data": {"type": "bus_closed"}})
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await websocket.send_json({"ok": True, "data": item})

    async def receiver() -> None:
        while True:
            text = await websocket.receive_text()
            if text.strip().lower() in ("ping", '{"type":"ping"}'):
                await websocket.send_json(
                    {"ok": True, "data": {"type": "pong", "ts": time.time()}}
                )

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        await asyncio.wait(
            {sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in (sender_task, receiver_task):
            task.cancel()
        for task in (sender_task, receiver_task):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        hub.unsubscribe(bus_id, queue)
        with contextlib.suppress(Exception):
            await websocket.close()
