"""总线生命周期与状态接口。"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends

from ..manager import BusManager
from ..models import BusConfig, FilterSpec
from .common import get_manager, ok

router = APIRouter(prefix="/buses", tags=["bus"])


@router.post("", summary="打开一条 CAN 通道")
def open_bus(config: BusConfig, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.open(config)
    return ok(session.info())


@router.get("", summary="列出所有已打开的会话")
def list_buses(manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    return ok([session.info() for session in manager.list()])


@router.get("/{bus_id}", summary="会话详情")
def get_bus(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    return ok(manager.get(bus_id).info())


@router.delete("/{bus_id}", summary="关闭会话")
def close_bus(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    manager.close(bus_id)
    return ok({"closed": bus_id})


@router.get("/{bus_id}/stats", summary="实时统计与总线负载")
def bus_stats(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok(
        {
            "id": session.id,
            "state": session.state(),
            "stats": session.snapshot_stats(),
            "cyclic_tasks": sum(1 for t in session.cyclic_tasks() if t.running),
            "recording": session.record_info(),
        }
    )


@router.post("/{bus_id}/reset", summary="从 Bus-Off / Error-Passive 恢复")
def reset_bus(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    ok_flag = session.reset()
    return ok({"reset": ok_flag, "state": session.state()})


@router.post("/{bus_id}/flush", summary="清空本地历史缓冲")
def flush_bus(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    dropped = session.flush()
    return ok({"dropped": dropped})


@router.get("/{bus_id}/filters", summary="读取当前过滤器")
def get_filters(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok(
        {
            "filters": session.get_filters(),
            "hw_filters_active": session.meta.get("hw_filters", False),
        }
    )


@router.put("/{bus_id}/filters", summary="设置过滤器（硬件过滤 + 软件兜底）")
def set_filters(
    bus_id: str,
    filters: List[FilterSpec] = Body(default_factory=list, embed=True),
    manager: BusManager = Depends(get_manager),
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    hw_ok = session.set_filters(filters)
    return ok({"filters": filters, "hw_filters_active": hw_ok})


@router.delete("/{bus_id}/filters", summary="清除所有过滤器")
def clear_filters(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    hw_ok = session.set_filters([])
    return ok({"filters": [], "hw_filters_active": hw_ok})


@router.get("/{bus_id}/capabilities", summary="该会话所走后端的能力")
def capabilities(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    return ok(manager.capabilities(bus_id))
