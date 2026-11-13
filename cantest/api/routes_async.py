"""后台任务接口：周期发送、录制、回放。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..errors import BadRequest
from ..manager import BusManager
from ..models import CyclicRequest, RecordRequest, ReplayRequest
from .common import get_manager, ok

router = APIRouter(prefix="/buses", tags=["background"])


# --------------------------------------------------------------------------
# 周期发送
# --------------------------------------------------------------------------
@router.post("/{bus_id}/cyclic", summary="启动周期发送")
def start_cyclic(
    bus_id: str, req: CyclicRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    task = session.start_cyclic(req)
    return ok(task.info())


@router.get("/{bus_id}/cyclic", summary="列出周期任务")
def list_cyclic(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    tasks = [task.info() for task in session.cyclic_tasks()]
    return ok({"count": len(tasks), "tasks": tasks})


@router.delete("/{bus_id}/cyclic", summary="停止全部周期任务")
def stop_all_cyclic(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    stopped = session.stop_cyclic(None)
    return ok({"stopped": stopped})


@router.delete("/{bus_id}/cyclic/{task_id}", summary="停止指定周期任务")
def stop_cyclic(
    bus_id: str, task_id: str, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    task = session.cyclic_task(task_id)
    if task is None:
        raise BadRequest(f"周期任务 {task_id!r} 不存在")
    session.stop_cyclic(task_id)
    # 任务停止后即从任务表摘除，最终统计在这次响应里带走，
    # 平台不需要靠「列表里还挂着一条已停止的任务」来读结果。
    return ok({"stopped": task_id, "finished": task.info()})


# --------------------------------------------------------------------------
# 录制
# --------------------------------------------------------------------------
@router.post("/{bus_id}/record/start", summary="开始录制")
def start_record(
    bus_id: str, req: RecordRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok(session.start_record(req))


@router.post("/{bus_id}/record/stop", summary="停止录制")
def stop_record(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok(session.stop_record())


@router.get("/{bus_id}/record", summary="录制状态")
def record_info(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok(session.record_info())


# --------------------------------------------------------------------------
# 回放
# --------------------------------------------------------------------------
@router.post("/{bus_id}/replay", summary="回放录制文件")
def start_replay(
    bus_id: str, req: ReplayRequest, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    job = session.start_replay(req)
    return ok(job.info())


@router.get("/{bus_id}/replay", summary="回放任务列表")
def list_replay(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    jobs = session.replay_jobs()
    return ok({"count": len(jobs), "jobs": jobs})


@router.delete("/{bus_id}/replay", summary="停止全部回放")
def stop_all_replay(bus_id: str, manager: BusManager = Depends(get_manager)) -> Dict[str, Any]:
    session = manager.get(bus_id)
    return ok({"stopped": session.stop_replay(None)})


@router.delete("/{bus_id}/replay/{job_id}", summary="停止指定回放")
def stop_replay(
    bus_id: str, job_id: str, manager: BusManager = Depends(get_manager)
) -> Dict[str, Any]:
    session = manager.get(bus_id)
    stopped = session.stop_replay(job_id)
    if not stopped:
        raise BadRequest(f"回放任务 {job_id!r} 不存在")
    return ok({"stopped": job_id})
