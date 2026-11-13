"""系统级接口：健康检查、后端与通道枚举。"""

from __future__ import annotations

import platform
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from .. import __version__
from ..backends import BACKEND_CAPS, available_backends, default_interface, list_channels, probe
from ..hub import FrameHub
from ..manager import BusManager
from ..models import BusStats
from .common import get_hub, get_manager, ok

router = APIRouter(tags=["system"])


@router.get("/health", summary="健康检查")
def health(hub: FrameHub = Depends(get_hub)) -> Dict[str, Any]:
    return ok(
        {
            "status": "ok",
            "version": __version__,
            "server_time": time.time(),
            "ws_subscribers": hub.total_subscribers(),
        }
    )


@router.get("/info", summary="运行环境与服务信息")
def info(manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    import can

    return ok(
        {
            "version": __version__,
            "python": platform.python_version(),
            "platform": platform.system(),
            "python_can": can.__version__,
            "default_interface": default_interface(),
            "open_buses": len(manager.list()),
            "capabilities": BACKEND_CAPS,
        }
    )


@router.get("/interfaces", summary="列出可用后端及其能力")
def interfaces() -> Dict[str, Any]:
    return ok(available_backends())


@router.get("/channels", summary="枚举通道")
def channels(
    interface: Optional[str] = Query(default=None, description="留空则用默认后端"),
) -> Dict[str, Any]:
    return ok({"interface": interface or default_interface(), "channels": list_channels(interface)})


@router.get("/probe", summary="硬件自检（DLL / 通道 / 驱动）")
def hardware_probe() -> Dict[str, Any]:
    return ok(probe())


@router.get("/stats", summary="所有会话的统计汇总")
def all_stats(manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for session in manager.list():
        stats: BusStats = session.snapshot_stats()
        items.append(
            {
                "id": session.id,
                "interface": session.interface,
                "channel": session.channel,
                "state": session.state(),
                "stats": stats.model_dump(),
            }
        )
    return ok({"count": len(items), "items": items})
