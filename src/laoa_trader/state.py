"""全局运行态：**有没有正在下载**（调度线程 / 界面 / CLI 共用同一面旗）。

为什么需要它
------------
下载全量历史是分钟级的动作，而调度线程每 `TICK`（1 秒）都在判断"到点了就跑日更"。
用户在首次向导里点【开始下载】的那一刻，如果调度线程正好刚到主跑时间、且今天还没跑过，
就会**在只写了一半的库上**跑策略 → 选出不该选的票 → 建池、甚至推通知。

实测过的现象（用户反馈）：启动 → 自检 `needs_full` → 弹向导开始下载 ✓，
**同时**调度线程触发了日常匹配 ✗ → 在不完整的数据上选出 1 只并建池。

这面旗就是那道闸门：`scheduler._maybe_daily` 看到它在飘就**跳过本次自动匹配**，
并且**不写"今天已完成"标记** —— 所以下载结束后当天还能正常补跑。

设计要点
--------
- 用 `threading.Event`：跨线程可见，且只关心"是不是在下载"这一个布尔量；
- **引用计数**而不是布尔量：`download_history` 内部还会调 `download_dump`，
  嵌套调用时只有最外层结束才算真的结束；
- 只在**入口函数**（`sync.download_history` / `sync.download_dump`）加旗，
  这样界面、CLI、自检补数据三条路都自动被覆盖。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()
_depth = 0
_event = threading.Event()


def begin_download() -> None:
    """进入下载区间（可重入：嵌套调用只加计数）。"""
    global _depth
    with _lock:
        _depth += 1
        _event.set()


def end_download() -> None:
    """离开下载区间；计数归零时清旗（允许不成对调用，不会变成负数）。"""
    global _depth
    with _lock:
        _depth = max(0, _depth - 1)
        if _depth == 0:
            _event.clear()


def is_downloading() -> bool:
    """当前是否有下载在进行（调度线程与界面读它）。"""
    return _event.is_set()


def depth() -> int:
    """当前嵌套层数（排障/测试用）。"""
    return _depth


@contextmanager
def download_scope() -> Iterator[None]:
    """`with download_scope():` —— 区间内 `is_downloading()` 为真（异常也保证清旗）。"""
    begin_download()
    try:
        yield
    finally:
        end_download()


def reset() -> None:
    """强制清旗（测试用；正常流程请走 `download_scope`）。"""
    global _depth
    with _lock:
        _depth = 0
        _event.clear()
