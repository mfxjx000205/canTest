"""录制与回放。

录制格式：

- ``jsonl``：每行一个帧的 JSON，测试平台最好解析，也便于 git diff
- ``csv``：Excel 可直接打开
- ``asc`` / ``blf``：CANoe / PCAN-View 等工具通用格式，交给 python-can 的 writer

回放按原始时间戳还原（可用 ``speed`` 缩放），也可用 ``gap_ms`` 强制固定间隔。
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import can

from .errors import BadRequest
from .frame import message_to_dict, parse_data
from .models import RecordInfo, ReplayRequest

__all__ = ["Recorder", "ReplayJob", "iter_frames", "dict_to_message", "default_record_path"]

RECORD_DIR = os.path.join(os.getcwd(), "recordings")

JSONL_FIELDS = (
    "timestamp", "id", "data", "dlc", "is_extended_id", "is_fd",
    "bitrate_switch", "is_remote_frame", "is_error_frame", "is_rx", "channel",
)


def default_record_path(session_id: str, fmt: str) -> str:
    os.makedirs(RECORD_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(RECORD_DIR, f"{stamp}-{session_id}.{fmt}")


def dict_to_message(d: Dict[str, Any]) -> can.Message:
    """把录制文件里的字典还原成 ``can.Message``。"""
    return can.Message(
        arbitration_id=int(d.get("id", 0)),
        data=parse_data(d.get("data", "")),
        is_extended_id=bool(d.get("is_extended_id", False)),
        is_fd=bool(d.get("is_fd", False)),
        bitrate_switch=bool(d.get("bitrate_switch", False)),
        is_remote_frame=bool(d.get("is_remote_frame", False)),
        is_error_frame=bool(d.get("is_error_frame", False)),
        timestamp=float(d.get("timestamp") or time.time()),
        check=False,
    )


def iter_frames(path: str) -> Iterator[can.Message]:
    """按文件类型读取录制文件，逐个产出消息。"""
    if not os.path.isfile(path):
        raise BadRequest(f"录制文件不存在：{path}")

    lower = path.lower()
    if lower.endswith(".jsonl") or lower.endswith(".ndjson"):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield dict_to_message(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        return

    try:
        reader = can.LogReader(path)
    except Exception as exc:  # noqa: BLE001
        raise BadRequest(f"无法解析录制文件 {path}: {exc}") from exc
    for msg in reader:
        yield msg


class Recorder:
    """单条总线的录制器。同一时刻只允许一个录制会话。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fh = None
        self._csv_writer: Optional[csv.DictWriter] = None
        self._can_writer = None
        self._active = False
        self._path: Optional[str] = None
        self._format: Optional[str] = None
        self._frames = 0
        self._bytes = 0
        self._started_at: Optional[float] = None

    # ---- 状态 ---------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    def info(self) -> RecordInfo:
        size = 0
        if self._path and os.path.isfile(self._path):
            try:
                size = os.path.getsize(self._path)
            except OSError:
                size = 0
        return RecordInfo(
            active=self._active,
            path=self._path,
            format=self._format,
            frames=self._frames,
            bytes=size,
            started_at=self._started_at,
        )

    # ---- 启停 ---------------------------------------------------------
    def start(
        self,
        fmt: str = "jsonl",
        path: Optional[str] = None,
        append: bool = False,
        session_id: str = "bus",
    ) -> RecordInfo:
        with self._lock:
            if self._active:
                raise BadRequest("已有录制在进行中，请先停止")

            fmt = fmt.lower()
            if fmt not in ("jsonl", "csv", "asc", "blf"):
                raise BadRequest(f"不支持的录制格式：{fmt}")

            target = path or default_record_path(session_id, fmt)
            target = os.path.abspath(target)
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)

            mode = "a" if append else "w"
            try:
                if fmt == "jsonl":
                    self._fh = open(target, mode, encoding="utf-8", newline="")
                elif fmt == "csv":
                    self._fh = open(target, mode, encoding="utf-8", newline="")
                    self._csv_writer = csv.DictWriter(self._fh, fieldnames=list(JSONL_FIELDS))
                    if not append or os.path.getsize(target) == 0:
                        self._csv_writer.writeheader()
                elif fmt == "asc":
                    self._can_writer = can.ASCWriter(target)
                elif fmt == "blf":
                    self._can_writer = can.BLFWriter(target)
            except OSError as exc:
                raise BadRequest(f"无法创建录制文件 {target}: {exc}") from exc

            self._path = target
            self._format = fmt
            self._frames = 0
            self._bytes = 0
            self._started_at = time.time()
            self._active = True
            return self.info()

    def write(self, msg: can.Message) -> None:
        if not self._active:
            return
        with self._lock:
            try:
                if self._can_writer is not None:
                    self._can_writer.on_message_received(msg)
                elif self._csv_writer is not None and self._fh is not None:
                    row = message_to_dict(msg)
                    row["data"] = row["data"]
                    self._csv_writer.writerow({k: row.get(k) for k in JSONL_FIELDS})
                    self._fh.flush()
                elif self._fh is not None:
                    self._fh.write(json.dumps(message_to_dict(msg), ensure_ascii=False) + "\n")
                    self._fh.flush()
                self._frames += 1
                self._bytes += len(msg.data or b"")
            except (OSError, ValueError):
                # 落盘失败不应中断 RX 线程，计数已经加了，记录在 info 里可见
                pass

    def stop(self) -> RecordInfo:
        with self._lock:
            if not self._active:
                return self.info()
            try:
                if self._can_writer is not None:
                    self._can_writer.stop()
                    self._can_writer = None
                if self._fh is not None:
                    self._fh.close()
                    self._fh = None
            except OSError:
                pass
            self._csv_writer = None
            self._active = False
            return self.info()


class ReplayJob:
    """后台回放任务。

    ``sender`` 是「把消息发上总线」的回调，而不是裸 ``can.BusABC``：回放要复用
    总线会话的发帧路径（统计、历史、录制、WS 推送都在那里面）。
    """

    def __init__(
        self,
        sender: Callable[[can.Message], None],
        req: ReplayRequest,
        name: Optional[str] = None,
    ) -> None:
        self.id = name or f"replay-{int(time.time() * 1000) % 100000}"
        self.req = req
        self.sent = 0
        self.error: Optional[str] = None
        self.running = False
        self.started_at = 0.0
        self.finished_at: Optional[float] = None
        self._send = sender
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "ReplayJob":
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"replay-{self.id}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.finished_at is None:
            self.finished_at = time.time()

    def _run(self) -> None:
        self.running = True
        try:
            for _ in range(self.req.count):
                if self._stop.is_set():
                    break
                prev_ts: Optional[float] = None
                for msg in iter_frames(self.req.path):
                    if self._stop.is_set():
                        break
                    if self.req.gap_ms is not None:
                        if self.sent:
                            time.sleep(self.req.gap_ms / 1000.0)
                    elif prev_ts is not None:
                        delta = (msg.timestamp - prev_ts) / max(self.req.speed, 1e-6)
                        if 0 < delta < 10:
                            time.sleep(delta)
                    prev_ts = msg.timestamp
                    try:
                        self._send(msg)
                        self.sent += 1
                    except Exception as exc:  # noqa: BLE001 - 单帧失败不影响后续
                        self.error = str(exc)
                if not self.req.loop:
                    break
        except Exception as exc:  # noqa: BLE001
            # 文件不存在 / 格式坏了都在这里收口，任务状态里能查到原因，
            # 而不是只在 stderr 留一段 traceback。
            self.error = str(exc)
        finally:
            self.running = False
            self.finished_at = time.time()

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.req.path,
            "sent": self.sent,
            "running": self.running,
            "speed": self.req.speed,
            "gap_ms": self.req.gap_ms,
            "loop": self.req.loop,
            "started_at": self.started_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 3)
            if self.started_at
            else 0.0,
            "error": self.error,
        }
