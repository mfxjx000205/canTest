"""高精度墙钟：给帧时间戳用，不再受 ``time.time()`` 的粒度限制。

为什么需要这个模块：

Windows 上 Python 3.10 及更早的 ``time.time()`` 走 ``GetSystemTimeAsFileTime()``，
分辨率 **15.625 ms**（``time.get_clock_info("time").resolution`` 会如实告诉你）。
3.11+ 换成了 ``GetSystemTimePreciseAsFileTime()``，分辨率 100 ns。

对本项目这不是无关痛痒的细节：帧时间戳直接决定两件事——
``GET /frames?since=`` 能不能按时间筛出帧、``history()`` 合并收发后的顺序对不对。
15.6 ms 的粒度下，1 Mbps 总线上的一整个报文突发会全部塌进同一个时间戳，
于是「先发后收」被读成「同刻」，顺序只能靠巧合。

实测（Windows + Python 3.10，连续 5000 次调用取不同值个数）：

    time           uniq=   1   min_step=      0 ns   GetSystemTimeAsFileTime
    perf_counter   uniq=3326   min_step=    100 ns   QueryPerformanceCounter
    monotonic      uniq=   1   min_step=      0 ns   GetTickCount64

也就是说 **3.10 的 ``perf_counter`` 依然是 100 ns**，只是它是一个单调计数器、
不是 epoch。所以这里做两件事：

1. 用 ``perf_counter`` 采样获得亚毫秒的**相对**精度；
2. 把它锚到 ``time.time()`` 上，得到既有 epoch 语义、又有高分辨率的墙钟。

锚点会周期性重校（默认 10 s 一次），防止长时间运行后两者漂移；系统改时间
也会在下次重校时被吸收。

关于单调性要说准确：**同一个锚定窗口内**返回值单调不减（由 ``perf_counter``
保证）；但跨窗口重校时返回的是新采样的 ``time.time()``，所以系统时钟被往回拨
的话，重锚后会出现一次向后的跳变。即本模块**不承诺**全局严格单调。
帧时间戳的顺序不依赖这一点——``bus.py`` 的 ``_stamp()`` 在锁内做了严格递增
钳位，无论底层时钟怎么跳，收发两侧入库的时间戳都保证严格递增。
"""

from __future__ import annotations

import time

__all__ = ["now", "resolution_ns", "is_coarse", "reset", "describe"]


# 重校间隔：间隔越短越贴合系统时间的跳变，越长则漂移影响的累积窗口越大。
# 10 s 对 CAN 测试足够——它远小于一次典型测试时长，又不会让锚定开销可见。
_RESYNC_INTERVAL_S = 10.0

# perf_counter 与 time.time() 的差值锚点
_anchor_perf = 0.0
_anchor_wall = 0.0
_ready = False


def reset() -> None:
    """丢弃锚点，下次 :func:`now` 重新采样（测试用）。"""
    global _ready
    _ready = False


def _calibrate() -> float:
    """重新锚定 perf_counter 与墙钟，返回本次的 offset。

    采样顺序有意为之：**先读 perf_counter，后读 time.time()**。反过来的话
    两次读之间墙钟可能刚好跨过一个 15.6 ms 的刻度跳变，会让 offset 带上
    最多一整格的系统误差；先记精确值、再取较粗的墙钟，误差就只剩墙钟自身
    的分辨率（它本来就是我们要绕开的那个量）。
    """
    global _anchor_perf, _anchor_wall, _ready
    perf = time.perf_counter()
    wall = time.time()
    _anchor_perf = perf
    _anchor_wall = wall
    _ready = True
    return wall - perf


def now() -> float:
    """当前墙钟时间（epoch 秒，浮点）。

    分辨率由底层的 ``perf_counter`` 决定，与 Python 版本无关：在 3.9/3.10 的
    Windows 上也是 100 ns 量级，而不是 ``time.time()`` 的 15.6 ms。

    同一锚定窗口内单调不减；跨窗口重校时若系统时钟被往回拨，会出现一次向后
    跳变（见模块 docstring）。要严格递增请用 ``bus.BusSession`` 里的钳位。
    """
    global _anchor_perf, _anchor_wall
    if not _ready:
        _calibrate()
        return _anchor_wall

    perf = time.perf_counter()
    # 距上次校准已过久 → 重校。判定放在取值之前，避免返回一个刚被判为过期的读数。
    if perf - _anchor_perf > _RESYNC_INTERVAL_S or perf < _anchor_perf:
        _calibrate()
        return _anchor_wall
    return _anchor_wall + (perf - _anchor_perf)


def _probe_min_step_ns(clock=time.perf_counter, samples: int = 2000) -> int:
    """测量某个时钟能做到的最小前进步长（纳秒），0 表示分辨不出来。"""
    prev = clock()
    best = 0
    for _ in range(samples):
        cur = clock()
        if cur > prev:
            step = cur - prev
            if best == 0 or step < best:
                best = step
            prev = cur
    return int(round(best * 1e9))


def resolution_ns() -> int:
    """本模块 :func:`now` 可达到的时间分辨率（纳秒），实测不为查表。"""
    step = _probe_min_step_ns(time.perf_counter)
    return step if step > 0 else 100


def is_coarse(threshold_ns: int = 1_000_000) -> bool:
    """``time.time()`` 本身是否粗到影响帧间隔测量（默认阈值 1 ms）。

    只用它做「要不要提示用户换 Python」的判断，不影响 :func:`now` 的精度。
    """
    info = time.get_clock_info("time")
    res_ns = int(round(info.resolution * 1e9))
    # resolution 字段为 0 的平台上退回实测
    if res_ns <= 0:
        res_ns = _probe_min_step_ns(time.time)
    return res_ns > threshold_ns


def describe() -> dict:
    """诊断信息，给 ``/info``、``probe`` 和启动脚本用。"""
    info = time.get_clock_info("time")
    raw_ns = int(round(info.resolution * 1e9))
    return {
        "wall_clock_impl": info.implementation,
        "wall_clock_resolution_ns": raw_ns,
        "effective_resolution_ns": resolution_ns(),
        "coarse": is_coarse(),
        "source": "perf_counter",
    }
