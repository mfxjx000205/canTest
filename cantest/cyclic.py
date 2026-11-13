"""周期发送。

Windows 上 ``time.sleep`` 的默认定时精度约 15 ms，对 1 kHz 级别的周期报文不够用。
这里用「粗睡 + 自旋」两段式等待：距目标时间超过 1.5 ms 时用 sleep 让出 CPU，
最后 1.5 ms 自旋对齐，实测抖动可以压到亚毫秒级。

每个任务跑在自己的线程里，互不影响；发送失败会累计错误，连续失败达到阈值自动停止，
避免总线断开后疯狂刷错误。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from .models import CyclicInfo, CyclicRequest, FrameSpec

__all__ = ["CyclicTask", "CyclicManager", "precise_sleep"]

_MAX_CONSECUTIVE_ERRORS = 20


def precise_sleep(duration_s: float) -> None:
    """高精度睡眠：粗睡 + 自旋。"""
    if duration_s <= 0:
        return
    deadline = time.perf_counter() + duration_s
    while True:
        remain = deadline - time.perf_counter()
        if remain <= 0:
            return
        if remain > 0.0015:
            time.sleep(remain - 0.001)
        # 最后 1.5 ms 自旋对齐，避免 sleep 粒度带来的抖动


class CyclicTask:
    """一个周期发送任务，支持多帧序列与有限次数。"""

    def __init__(
        self,
        frames: List[FrameSpec],
        period_ms: float,
        count: int,
        sender: Callable[[FrameSpec], None],
        name: Optional[str] = None,
        start_delay_ms: float = 0.0,
        task_id: Optional[str] = None,
    ) -> None:
        self.id = task_id or uuid.uuid4().hex[:8]
        self.name = name
        self.frames = frames
        self.period_ms = float(period_ms)
        self.count = int(count)
        self.start_delay_ms = float(start_delay_ms)

        self._sender = sender
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.sent_frames = 0
        self.sent_cycles = 0
        self.started_at = 0.0
        self.finished_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._jitter_ms = 0.0
        self._actual_period_ms = 0.0
        self._consecutive_errors = 0

    # ---- 生命周期 -----------------------------------------------------
    def start(self) -> "CyclicTask":
        self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name=f"cyclic-{self.id}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, wait: bool = True, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)
        if self.finished_at is None:
            self.finished_at = time.time()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 主循环 -------------------------------------------------------
    def _run(self) -> None:
        period_s = self.period_ms / 1000.0
        if self.start_delay_ms > 0:
            precise_sleep(self.start_delay_ms / 1000.0)

        t0 = time.perf_counter()
        cycle = 0
        prev_start = t0
        period_samples: List[float] = []

        while not self._stop_event.is_set():
            if self.count and cycle >= self.count:
                break

            target = t0 + cycle * period_s
            now = time.perf_counter()
            if target > now:
                precise_sleep(target - now)

            actual_start = time.perf_counter()
            if cycle > 0:
                period_samples.append(actual_start - prev_start)
                if len(period_samples) > 200:
                    period_samples.pop(0)
            prev_start = actual_start

            # 当前周期内依次发出所有帧
            for spec in self.frames:
                if self._stop_event.is_set():
                    break
                try:
                    self._sender(spec)
                    self.sent_frames += 1
                    self._consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 - 记录后继续
                    self._consecutive_errors += 1
                    self.last_error = str(exc)
                    if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        self._stop_event.set()
                        break

            cycle += 1
            self.sent_cycles = cycle

        if period_samples:
            avg = sum(period_samples) / len(period_samples)
            self._actual_period_ms = avg * 1000.0
            # 抖动取与目标周期的平均绝对偏差
            devs = [abs(s - period_s) for s in period_samples]
            self._jitter_ms = sum(devs) / len(devs) * 1000.0
        self.finished_at = time.time()

    # ---- 输出 ---------------------------------------------------------
    def info(self) -> CyclicInfo:
        end = self.finished_at or time.time()
        return CyclicInfo(
            id=self.id,
            name=self.name,
            period_ms=self.period_ms,
            count=self.count,
            sent_frames=self.sent_frames,
            sent_cycles=self.sent_cycles,
            running=self.running,
            started_at=self.started_at,
            elapsed_s=round(end - self.started_at, 3) if self.started_at else 0.0,
            actual_period_ms=round(self._actual_period_ms, 3),
            jitter_ms=round(self._jitter_ms, 3),
            last_error=self.last_error,
        )


class CyclicManager:
    """管理一条总线上的所有周期任务。"""

    def __init__(self, sender: Callable[[FrameSpec], None]) -> None:
        self._sender = sender
        self._tasks: Dict[str, CyclicTask] = {}
        self._lock = threading.Lock()

    def start(self, req: CyclicRequest) -> CyclicTask:
        task = CyclicTask(
            frames=req.frames,
            period_ms=req.period_ms,
            count=req.count,
            sender=self._sender,
            name=req.name,
            start_delay_ms=req.start_delay_ms,
        )
        with self._lock:
            # 清理已结束的任务，避免长期运行时无限增长
            self._tasks = {k: v for k, v in self._tasks.items() if v.running}
            self._tasks[task.id] = task
        task.start()
        return task

    def get(self, task_id: str) -> Optional[CyclicTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> List[CyclicTask]:
        with self._lock:
            return list(self._tasks.values())

    def stop(self, task_id: str) -> bool:
        """停止并从任务表里摘掉，返回是否确实停了一个在册任务。

        摘除是刻意的：``DELETE`` 之后再 ``GET`` 还能看到「已停止」的任务，
        平台会以为没删掉。需要回查已发帧数的话，停止接口的响应里已经带回了
        最终统计。
        """
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        task.stop()
        return True

    def stop_all(self) -> int:
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            task.stop(wait=True, timeout=1.0)
        return len(tasks)
