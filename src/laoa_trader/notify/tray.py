"""托盘通知（气泡 + 状态灯，供 UI 层消费）。

为什么单独一路
--------------
后台线程（调度器）需要"告诉界面有事发生"，但不能直接碰 Qt 控件 ——
Qt 控件只能在其所属线程里操作，跨线程调用会随机崩溃。

这里的做法：后台线程只把消息放进**线程安全的队列**，界面用 `QTimer`
定期 `drain()` 取走并用 `QSystemTrayIcon.showMessage()` 弹气泡。
这样托盘通知天然"离线可用"（界面没跑或最小化时消息不丢）。
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Any

from laoa_trader.log import get_logger

logger = get_logger(__name__)

MAX_QUEUE = 200
_queue: queue.Queue[dict] = queue.Queue(maxsize=MAX_QUEUE)
_lock = threading.Lock()

#: 界面注册的托盘图标（QSystemTrayIcon）。由 `ui/app.py` 调用 `bind()` 注入。
_tray: Any = None


def bind(tray: Any) -> None:
    """把 Qt 托盘图标交给本模块（界面启动时调用）。"""
    global _tray
    _tray = tray


def unbind() -> None:
    global _tray
    _tray = None


def notify(title: str, lines: list[str], cfg: Any = None) -> dict:
    """投递一条托盘气泡消息。

    队列满时丢掉最旧的一条（提醒宁可丢也不该阻塞调度线程）。
    """
    if cfg is not None and not getattr(cfg, "notify_tray", True):
        return {"kind": "tray", "ok": True, "skipped": True, "detail": "已在配置中关闭"}
    duration_ms = int(getattr(cfg, "notify_tray_duration_ms", 8000)) if cfg is not None else 8000
    message = {
        "title": title,
        "body": "\n".join(str(line) for line in lines[:10]),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": duration_ms,
    }
    try:
        _queue.put_nowait(message)
    except queue.Full:
        try:
            _queue.get_nowait()
        except queue.Empty:  # pragma: no cover - 竞态兜底
            pass
        try:
            _queue.put_nowait(message)
        except queue.Full:  # pragma: no cover
            logger.warning("托盘消息队列已满，丢弃本条")
            return {"kind": "tray", "ok": False, "detail": "队列已满"}

    # 界面已绑定托盘图标时**顺手弹一下**（仍在后台线程：Qt 的 showMessage 是
    # 跨线程安全的，真正跨线程不安全的是创建/销毁控件）
    if _tray is not None:
        _balloon(title, message["body"], message["duration_ms"])
    return {"kind": "tray", "ok": True,
            "detail": f"已投递到托盘（显示 {message['duration_ms']} ms）"}


def _balloon(title: str, body: str, duration_ms: int) -> None:
    """弹气泡：优先带图标与时长；宿主只接受两个参数时退回两参数调用。

    为什么要这么写：托盘对象是界面层注入的（可能是 QSystemTrayIcon，
    也可能是测试里的假对象），签名不完全相同 —— 多试一次比让通知静默丢失好。
    """
    if _tray is None:
        return
    try:
        _tray.showMessage(title, body, _icon(), int(duration_ms))
    except TypeError:
        try:
            _tray.showMessage(title, body)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"托盘气泡弹出失败（消息已入队）：{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"托盘气泡弹出失败（消息已入队）：{exc}")


def _icon():
    """气泡图标（拿不到 Qt 时返回 None，宿主自会处理）。"""
    try:
        from PySide6.QtWidgets import QSystemTrayIcon  # type: ignore[import-not-found]

        return QSystemTrayIcon.MessageIcon.Information
    except Exception:  # noqa: BLE001
        return None


def drain(limit: int = 20) -> list[dict]:
    """取走队列里的消息（界面 QTimer 定期调用）。"""
    out: list[dict] = []
    for _ in range(limit):
        try:
            out.append(_queue.get_nowait())
        except queue.Empty:
            break
    return out


def pending() -> int:
    """队列里还有多少条（测试/状态用）。"""
    return _queue.qsize()


def clear() -> None:
    """清空队列（测试用）。"""
    with _lock:
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
