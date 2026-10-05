"""日志：统一格式，并同时写文件（桌面版的日志要能被用户找到）。

为什么不用 logging.basicConfig 了事
----------------------------------
桌面版是双击运行的：出问题时用户看不到控制台，必须有一份**能翻的日志文件**。
所以这里在 `data_dir/logs/caishen-helper.log` 里留一份（按大小轮转），
同时打到控制台（开发/CLI 模式用）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


class _SafeStreamHandler(logging.StreamHandler):
    """往控制台写日志的 handler：**流已经关了就当这条没写**，绝不抛异常。

    为什么需要它（2026-09-21，CI 三次红在同一个地方）：桌面版有一批后台线程
    （取数、调度、朗读……）。用例跑完、pytest 把 `sys.stderr` 关掉之后，只要还有一条
    线程活到那一刻并发出一句日志，`StreamHandler.emit()` 就会抛
    `ValueError: I/O operation on closed file` —— 它本身只是噪音，但它会把
    "线程活过了收尾"这件事暴露在日志里，还可能让 logging 自己再去处理异常时踩到同一个
    已关闭的流。收尾的**根因**在别处（线程必须确定性收干净，见 `ui/app.py` 的
    `MainWindow.shutdown()` 与 `tests/conftest.py` 的收尾夹具），这里只保证"日志这一层
    不会成为崩溃的放大器"。
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging 约定
        try:
            super().emit(record)
        except (ValueError, OSError):
            # 流已关闭 / 管道断开：丢掉这一条，什么都不做
            # （连 `handleError` 都不调用 —— 它默认又要往同一个流里写，正是我们要避开的）
            pass


def setup_logging(data_dir: Path | str | None = None, level: int = logging.INFO) -> Path | None:
    """配置根 logger（幂等），返回日志文件路径（未写入文件时返回 None）。

    Args:
        data_dir: 数据目录；给了就写 `<data_dir>/logs/caishen-helper.log`。
        level: 控制台级别。

    Returns:
        日志文件路径，或 None。
    """
    global _configured
    root = logging.getLogger("laoa_trader")
    if _configured:
        return getattr(root, "_laoa_log_path", None)

    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = _SafeStreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    log_path: Path | None = None
    if data_dir:
        try:
            folder = Path(data_dir) / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            log_path = folder / "caishen-helper.log"
            # 轮转：单文件 2MB × 3 份，桌面场景足够回溯最近几天
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(file_handler)
        except OSError:
            # 日志写不了不该让程序起不来（例如目录只读）
            log_path = None

    root._laoa_log_path = log_path  # type: ignore[attr-defined]
    _configured = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    """取带命名空间的 logger（与服务器版 `get_logger` 用法一致）。"""
    short = name.replace("laoa_trader.", "")
    return logging.getLogger(f"laoa_trader.{short}")


def log_file_path() -> Path | None:
    """当前日志文件路径（**只读、无副作用**）；还没写过文件时返回 None。

    为什么要单独开一个"只读"的口：状态详情里要告诉用户"日志在哪"，而
    `setup_logging()` 是**会建目录、建文件、挂 handler** 的（有副作用）——
    界面只是想把这个路径**显示**出来，不该顺手去改日志配置。
    """
    return getattr(logging.getLogger("laoa_trader"), "_laoa_log_path", None)
