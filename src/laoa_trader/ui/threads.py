"""还在跑的后台线程：**绝不允许在它跑着的时候被析构**（寄存 / 等着它落地）。

为什么必须有这个模块（2026-10-13 CI 与 2026-10-14 本机实测）
----------------------------------------------------------
PySide6/Qt 里，`QThread` 的 C++ 对象一旦在"线程还在跑"的时候被销毁，Qt 走的是
`qFatal` → `abort()`：**不是** Python 异常，`try/except` 抓不住、没有 traceback、
pytest 连失败摘要都来不及打印。症状就是本项目反复遇到的那一条：

* CI（Windows）：跑到 ~90% 进程直接没了、没有任何用例失败记录，`pytest.log` 停在半路；
* 本机（Linux）：`Fatal Python error: Aborted`，faulthandler 的栈停在**评分线程**
  （`ui/scores.py in run`），主线程停在**收尾那一行**。

而"什么时候会被析构"有两条路，两条都真的踩到过：

1. **C++ 父对象销毁**：`_ScoreWorker` 挂在 `ScoreService` 下面，而 `ScoreService` 又挂在
   主窗口下面 —— 窗口 `deleteLater()` + 处理 DeferredDelete 时，整条父子链连子带孙一起
   析构。评分线程此刻还在读库/取快照，于是 abort（本机已复现：把那一步的评分拖慢到
   超过 `stop()` 的等待上限，`tests/test_ui_smoke.py` 的收尾就稳定 134 退出）。
2. **Python 引用归零**：`self._worker = None`、`Worker._live.clear()` 之后没人再拿着它，
   PySide6 顺手把 C++ 对象删掉 —— 同样是"跑着的时候被析构"。

所以收尾的规矩只有一条：**先请求它停（`cancel()` / `_stopped` 标志）→ 再等 → 等不到就寄存**。
"寄存" = 把 C++ 父对象摘掉（`setParent(None)`，脱离销毁连锁）+ 留一个强引用（别让引用归零），
让它自己跑完；这期间它已经收了停的指令，最多做完手上这一轮就结束。

与"放宽断言"无关：这条只保证**进程不会静默死掉**，用例该红还是红。
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from typing import Any

#: 已寄存的线程（**强引用**：它们就是靠这个活着，直到自己跑完）
_PARKED: list[Any] = []
_LOCK = threading.Lock()

#: 退出时最多等多久（秒）：寄存的线程通常几毫秒到几秒就结束，这里给足但不无限等
_EXIT_WAIT_SECONDS = 10.0


def _alive(thread: Any) -> bool:
    """线程还在跑吗（底层 C++ 对象已销毁时返回 False，绝不抛）。"""
    try:
        return bool(thread.isRunning())
    except RuntimeError:      # 底层对象已析构
        return False
    except Exception:         # noqa: BLE001 - 任何异常都当作"它已经不在了"
        return False


def _object_alive(thread: Any) -> bool:
    """这个线程**对象**还在吗（与"在跑吗"不同：还没 `start()` 的线程也算在）。

    判据是"问它 `isRunning()` 会不会抛 `RuntimeError`" —— PySide6 在 C++ 对象已经析构时
    就是抛这个（而不是返回 False），这正是我们要区分的那两种"不活"。
    """
    try:
        thread.isRunning()
        return True
    except RuntimeError:      # 底层对象已析构
        return False
    except Exception:         # noqa: BLE001
        return False


def _gone(thread: Any) -> bool:
    """这条登记**可以除名**了吗。

    判据是 `isFinished()`（Qt：**跑完了才为真**；没 `start()` 过是假、正在跑也是假）——
    ⚠️ 这里**不能**用 `isRunning()` 的反面来判断：
      * 刚建出来、还没 `start()` 的线程 `isRunning()` 也是假 → 会被当成"跑完了"立刻除名，
        于是"从创建到跑完都有人拿着它"这条保证在 `start()` 之前就断了；
      * 而"已经 `start()`、但 `run()` 还没被调度"的那几微秒正是最危险的窗口
        （调用方此时丢掉最后一份引用，跑着的 QThread 就会被析构 → `qFatal` → `abort()`）。
    这是 2026-10-14 实测出来的：写成 `not isRunning()` 时，`keep()` 在 `__init__` 里
    登记完**当场就被自己修剪掉了**，等于没登记。
    """
    try:
        return bool(thread.isFinished())
    except RuntimeError:      # 底层 C++ 对象已经没了
        return True
    except Exception:         # noqa: BLE001 - 问不出来就当它还在册（宁可多留一会儿）
        return False


def _prune() -> None:
    """丢掉已经跑完的（**调用方持锁**）。判据见 `_gone()`。"""
    _PARKED[:] = [t for t in _PARKED if not _gone(t)]


def park(thread: Any) -> bool:
    """把一个**还在跑**的线程寄存起来，返回是否真的寄存了（已经在跑完的返回 False）。

    两件事一起做，缺一不可：

    * `setParent(None)`：从 C++ 父对象的销毁连锁里摘出来 —— 否则父对象（服务 → 主窗口）
      一销毁，这个跑着的 `QThread` 就跟着被析构，直接 `abort()`；
    * 记进 `_PARKED`：握住 Python 引用 —— 否则调用方那句 `self._worker = None` 一执行，
      PySide6 就把它删了，同样是 abort。

    典型用法（收尾里）：

        if worker.isRunning():
            worker.wait(3_000)
        park(worker)      # 还在跑就寄存；已经跑完则什么都不做
    """
    if thread is None:
        return False
    try:
        if not _alive(thread):
            return False
    except Exception:          # noqa: BLE001
        return False
    try:
        thread.setParent(None)   # 摘出销毁连锁（QObject 接口）
    except Exception:            # noqa: BLE001 - 摘不掉的（已析构）就没必要寄存了
        return False
    with _LOCK:
        _PARKED.append(thread)
        _prune()
    return True


def keep(thread: Any) -> bool:
    """登记一条后台线程：从"创建"到"跑完"之间，**永远有人拿着它**。

    与 `park()` 的区别只在"摘不摘父对象"：`keep()` 只握住引用，不动父子关系 ——
    因此在线程自己的 `__init__` 里调它就行（不依赖"调用方记得收尾"）。

    为什么必须有人拿着：`QThread` 的 Python 包装一旦引用归零（调用方 `self._worker = None`、
    服务的属性随窗口一起没了、或者 gc 收掉一个环），PySide6 就会析构 C++ 对象；
    而线程还在跑 → `qFatal` → `abort()`。

    登记时机刻意放在 `__init__`（而不是 `run()` 开头）：`start()` 之后到 `run()` 真正
    被调度之间还有一个空档，那时线程已经是"running"状态、却谁都还没登记 ——
    调用方在这几微秒里把引用丢掉，同样会 abort。跑完了的线程会在下一次
    `keep()` / `park()` / `drain()` 时被除名（不会无限堆着）。
    """
    if thread is None:
        return False
    if not _object_alive(thread):
        return False
    with _LOCK:
        if any(existing is thread for existing in _PARKED):
            return False
        _PARKED.append(thread)
        _prune()
    return True


def parked() -> list[Any]:
    """当前**在册**的线程快照（已跑完的会被除名；排查/测试用）。

    ⚠️ "在册"不等于"在跑"：刚 `start()`、`run()` 还没被调度的那几微秒，它在册但
    `isRunning()` 已经是真；而"建出来还没 `start()`"的也在册。要问"现在还在跑的有哪几条"
    用 `running()`。
    """
    with _LOCK:
        _prune()
        return list(_PARKED)


def running() -> list[Any]:
    """当前**正在跑**的后台线程（收尾检查用：收尾之后这里应当是空的）。"""
    return [t for t in parked() if _alive(t)]


def drain(timeout: float = 5.0) -> int:
    """等寄存的线程跑完，返回**没等到**的条数（0 = 全落地了）。

    按条分配超时（与 `ui/app.py::Worker.wait_all` 同一个理由）：几条线程各自卡在边界上时，
    "总共等 N 秒"会让最后那条连等的机会都没有。
    """
    wait_ms = int(max(timeout, 0.0) * 1000)
    left = 0
    with _LOCK:
        current = list(_PARKED)
    for thread in current:
        try:
            if _alive(thread):          # 没在跑的（建出来没 start / 已经跑完）不用等
                thread.wait(wait_ms)
            if _alive(thread):
                left += 1
        except RuntimeError:      # 已经析构了
            continue
        except Exception:         # noqa: BLE001 - 收尾失败不该挡住退出
            left += 1
    with _LOCK:
        _prune()
    return left


def _drain_at_exit() -> None:      # pragma: no cover - 只在实际退出时跑
    """进程退出前把寄存的线程等一等。

    为什么要它：解释器收尾会清掉模块全局变量，那份强引用一没，还在跑的 `QThread` 又会被
    析构 → 退出码变成 134（"程序看着正常跑完了，却报了个 abort"）。等一下就干净了；
    等不到的也不拦着退出（那时已经没别的办法，至少不会再把日志流写坏）。
    """
    try:
        drain(_EXIT_WAIT_SECONDS)
    except Exception:          # noqa: BLE001 - 退出路径绝不抛
        pass


#: 进程真的要退出时，给还活着的后台线程的**最后一点**时间（秒）。为什么不给足：
#: 同花顺那条取数的读超时是 90 秒（`data/hithink.py`），照它等就是"关个软件要等一分半"。
_EXIT_GRACE_SECONDS = 3.0


def _flush_logs() -> None:
    """把日志流刷出去（`os._exit()` 不会替我们刷）。"""
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:      # noqa: BLE001
                pass
        logging.shutdown()
    except Exception:              # noqa: BLE001 - 刷不出去也不该挡住退出
        pass


def under_test() -> bool:
    """现在是不是在 pytest 里跑（测试进程里**绝不允许**直接结束进程）。"""
    return "pytest" in sys.modules


def finish_exit(code: int) -> int:
    """GUI 退出码的**最后一道**：还有后台线程在跑就直接结束进程，别让它被析构。

    为什么需要（2026-10-14 实测）：Qt 在解释器收尾时会把这些 `QThread` 连着 C++ 对象一起
    析构，而"跑着的时候被析构"= `qFatal` → `abort()` —— 于是"关掉软件"变成退出码 134，
    Windows 上还会弹一次"程序已停止工作"（用户看到的就是"这软件怎么老是崩"）。
    实测：留一条睡 20 秒的 QThread 不再引用、进程退出 → `QThread: Destroyed while thread ''
    is still running` + 退出码 134；而先给几秒、再 `os._exit()` 就是干净的 0。

    这里的取舍是刻意的：**宁可命令式地结束进程，也不要一个崩溃式的退出码**。
    内存由系统回收，日志与缓冲先显式刷出去；剩下的"没来得及做的事"本来就只能等网络超时
    （90 秒），而那正是用户最不该被拖住的时候。

    ⚠️ 测试进程里**绝不**这么做：`os._exit()` 会连 pytest 的汇总与退出码一起吞掉
    （整套用例"全过"地消失）。所以 `under_test()` 为真时它**什么都不做**（连等待都不等）
    —— 那道判断刻意放在最前面，让它成为一条"不可绕过"的闸，而不是"等完 3 秒再看情况"。
    """
    if under_test():
        return int(code)
    try:
        left = drain(_EXIT_GRACE_SECONDS)
    except Exception:              # noqa: BLE001
        left = 0
    if not left:
        return int(code)
    try:
        logging.getLogger("laoa_trader").warning(
            f"退出时还有 {left} 条后台线程在跑（最多要等网络超时）—— 直接结束进程，"
            "免得它在解释器收尾时被析构（那会以 abort 收场）"
        )
    except Exception:              # noqa: BLE001
        pass
    _flush_logs()
    os._exit(int(code))
    return int(code)               # 走不到这里；只为让静态检查与读者都清楚它的"语义"


atexit.register(_drain_at_exit)

__all__ = ["drain", "finish_exit", "keep", "park", "parked", "running", "under_test"]
