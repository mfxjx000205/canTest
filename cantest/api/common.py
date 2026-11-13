"""API 公共工具：响应信封、依赖注入、请求到匹配器的转换。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import Request

from ..bus import BusSession
from ..errors import BadRequest
from ..frame import FrameMatcher, parse_data
from ..hub import FrameHub
from ..manager import BusManager
from ..models import ExpectRequest

__all__ = ["ok", "get_manager", "get_hub", "get_session", "build_matcher"]


def ok(data: Any = None, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": True, "data": data}
    payload.update(extra)
    return payload


def get_manager(request: Request) -> BusManager:
    return request.app.state.manager


def get_hub(request: Request) -> FrameHub:
    return request.app.state.hub


def get_session(request: Request, bus_id: str) -> BusSession:
    return get_manager(request).get(bus_id)


def build_matcher(req: ExpectRequest) -> FrameMatcher:
    """把 API 的期望条件翻译成内部匹配器。"""
    data = parse_data(req.data) if req.data else None
    data_mask = parse_data(req.data_mask) if req.data_mask else None
    if data_mask is not None and data is None:
        raise BadRequest("data_mask 必须与 data 一起使用")
    if data is not None and data_mask is not None and len(data_mask) != len(data):
        raise BadRequest("data_mask 与 data 长度必须一致")
    return FrameMatcher(
        can_id=req.can_id,
        id_mask=req.id_mask,
        data=data,
        data_mask=data_mask,
        is_extended_id=req.is_extended_id,
        is_fd=req.is_fd,
    )
