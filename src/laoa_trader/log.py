"""日志：统一格式，并同时写文件（桌面版的日志要能被用户找到）。

为什么不用 logging.basicConfig 了事
----------------------------------
桌面版是双击运行的：出问题时用户看不到控制台，必须有一份**能翻的日志文件**。
所以这里在 `data_dir/logs/laoa-trader.log` 里留一份（按大小轮转），
同时打到控制台（开发/CLI 模式用）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def setup_logging(data_dir: Path | str | None = None, level: int = logging.INFO) -> Path | None:
    """配置根 logger（幂等），返回日志文件路径（未写入文件时返回 None）。

    Args:
        data_dir: 数据目录；给了就写 `<data_dir>/logs/laoa-trader.log`。
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

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    log_path: Path | None = None
    if data_dir:
        try:
            folder = Path(data_dir) / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            log_path = folder / "laoa-trader.log"
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
