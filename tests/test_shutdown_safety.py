"""收尾安全网：线程/日志在"用例结束之后"不许再制造麻烦。

为什么单独一个文件（2026-09-21，CI 三次死在同一个地方）
--------------------------------------------------------
CI（Windows）连着三次跑到 89~93% 就"没声音了"，**没有任何失败用例**，日志里只剩
后台线程往已关闭的流里写（`I/O operation on closed file`），随后整个进程中止。
根因是**线程活过了整场测试**，而它在 Windows 上才现形 —— 本机（Linux）没有 PowerShell，
朗读线程压根不会起来，所以本地怎么跑都是绿的。

这一组用例外加盯住三张网（根因在各自的产品代码里，这里只钉"网还在"）：
1. `voice.shutdown()`：给朗读线程送哨兵并等它退出（它是"永远堵在队列上"的守护线程）；
2. `Worker.wait_all()`：按**登记册**等所有后台工作线程落地，不依赖谁记得把线程挂到哪个属性；
3. 日志 handler 对"流已关闭"免疫：收尾阶段那一条日志不该变成崩溃的放大器。
"""

from __future__ import annotations

import logging
import os
import threading

import pytest

from laoa_trader import log as log_mod
from laoa_trader.notify import voice as voice_mod

@pytest.fixture()
def qapp():
    """离屏 QApplication（与 `test_formula_page.py` 里那个同款）。

    这些用例要建真正的 `QThread`，没有 QApplication 时 PySide6 会直接中止进程；
    这里单独建一个而不是共用别处的夹具：本文件要能在只跑它自己的时候也过。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过线程收尾测试")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


# ══════════════════════════════════════════════════════════════════════════
# 1) 朗读线程：收工信号
# ══════════════════════════════════════════════════════════════════════════


def test_voice_worker_is_stopped_by_shutdown() -> None:
    """`shutdown()` 之后那条朗读线程必须**真的退出了**（不是"下次再说"）。

    判据是 `threading.enumerate()` 里不再有 `voice-speak` —— 只看返回值不够：
    线程只要还活着，就有机会在解释器收尾时写日志/摸已析构的对象。
    """
    voice_mod.shutdown()                     # 先清干净，保证从"没有线程"开始
    voice_mod._ensure_worker()               # noqa: SLF001 - 这就是被测的那条线程
    names = [t.name for t in threading.enumerate()]
    assert "voice-speak" in names, "朗读线程没起来，这条用例就没意义了"

    assert voice_mod.shutdown() is True
    assert "voice-speak" not in [t.name for t in threading.enumerate()]


def test_voice_shutdown_is_idempotent() -> None:
    """没起过线程 / 重复调用都不许抛（它挂在所有退出路径上）。"""
    voice_mod.shutdown()
    assert voice_mod.shutdown() is True
    assert voice_mod.shutdown() is True


def test_voice_never_spawns_powershell_in_tests() -> None:
    """测试期间朗读这一路必须是"死的"：不起 PowerShell 进程、不枚举音色。

    这条钉的是 `tests/conftest.py` 里那个 autouse 夹具 —— 它一失效，Windows 的 CI
    就会重新起子进程与线程（而本机 Linux 永远发现不了）。
    """
    assert voice_mod._powershell() is None          # noqa: SLF001 - 夹具把它钉成 None
    assert voice_mod.available() is False
    assert voice_mod.installed_voices(refresh=True) == []


# ══════════════════════════════════════════════════════════════════════════
# 2) 工作线程登记册：不依赖"属性名写全了"
# ══════════════════════════════════════════════════════════════════════════


def test_worker_wait_all_covers_threads_not_held_by_the_window(qapp) -> None:
    """**没挂到窗口属性上**的工作线程也要被 `wait_all()` 等到 —— 这就是它存在的理由。

    历史上漏的正是这种：竞价取数那个线程当年不在 `shutdown()` 的
    等待名单里，于是它可能活过用例。登记册让"漏一个属性名"不再等于"漏一条线程"。
    """
    from laoa_trader.ui import app as ui_app

    worker_cls = ui_app.Worker
    started = threading.Event()

    def slow() -> str:
        started.set()
        threading.Event().wait(0.4)          # 故意比调用方先返回
        return "done"

    worker = worker_cls(slow)                # **没有任何父对象、也没被谁记住**
    worker.start()
    assert started.wait(5.0)
    assert worker.isRunning() is True

    left = worker_cls.wait_all(5.0)

    assert left == 0, "还有工作线程没落地"
    assert worker.isRunning() is False


def test_worker_wait_all_on_an_empty_registry_is_a_noop(qapp) -> None:
    """登记册空的时候也不许出事（它在每条用例的收尾都会跑一次）。"""
    from laoa_trader.ui import app as ui_app

    assert ui_app.Worker.wait_all(1.0) == 0


# ══════════════════════════════════════════════════════════════════════════
# 3) 日志：流关了就当这条没写
# ══════════════════════════════════════════════════════════════════════════


def test_log_handler_survives_a_closed_stream() -> None:
    """往**已关闭**的流写日志不许抛异常（收尾阶段那一条日志不该放大成崩溃）。

    ⚠️ 这里必须用 `os.devnull`，**不能写死 `/dev/null`**：Windows 上那个路径不存在，
    会以 `FileNotFoundError` 把 CI 直接弄红（2026-09-20 实测：CI 就是死在这一条上，
    而且是在 79% 处"停下来"的那种红，很容易被误当成又一次偶发崩溃）。
    """
    handler = log_mod._SafeStreamHandler(open(os.devnull, "w"))    # noqa: SIM115
    record = logging.LogRecord("laoa_trader.test", logging.INFO, __file__, 1, "x", (), None)
    handler.stream.close()                  # 模拟 pytest 收尾：流已关闭

    handler.emit(record)                    # 不许抛（普通 StreamHandler 会抛 ValueError）


def test_safe_handler_still_writes_when_the_stream_is_open(capsys) -> None:
    """正常情况仍要写出去 —— 这条防"为了不崩干脆什么都不写"的过度修法。"""
    handler = log_mod._SafeStreamHandler(__import__("sys").stderr)
    record = logging.LogRecord("laoa_trader.test", logging.INFO, __file__, 1, "写出来了吗", (), None)

    handler.emit(record)

    assert "写出来了吗" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════════
# 4) 跑着的线程绝不许被析构（寄存）—— 这就是那个"跑到 90% 无声中止"的根因
# ══════════════════════════════════════════════════════════════════════════


def _slow_thread_cls():
    """一个会跑一会儿的 `QThread`（0.6 秒足够"调用方已经走完、它还在跑"）。"""
    from PySide6.QtCore import QThread

    class Slow(QThread):
        def run(self) -> None:  # noqa: D102 - QThread 约定
            import time

            time.sleep(0.6)

    return Slow


def test_park_saves_a_thread_whose_parent_is_destroyed(qapp) -> None:
    """C++ 父对象被销毁时，**跑着的**线程必须被摘出来（否则 Qt 直接 `abort()`）。

    这条钉的是 2026-10-13 那个根因：`_ScoreWorker` 原来挂在 `ScoreService` 下面、
    服务又挂在主窗口下面 —— 窗口一 `deleteLater()` + 处理 DeferredDelete，整条父子链
    连子带孙一起析构，而评分线程还在读库/取快照，于是 Qt 走 `qFatal` → `abort()`：
    CI 上表现为"跑到 90% 进程没了、没有任何用例失败记录"，
    本机则是 `Fatal Python error: Aborted`、栈停在 `ui/scores.py in run`。

    用例本身能跑完就是断言（`abort()` 会把整个 pytest 进程带走）；
    另外验一下"寄存"确实把它从父子链里摘了出来、并且它跑完会被除名。
    """
    from PySide6.QtCore import QEvent, QObject

    from laoa_trader.ui import threads as threads_mod

    slow_cls = _slow_thread_cls()
    parent = QObject()
    thread = slow_cls(parent)                 # ← 就是"挂在别人下面"的那种
    thread.start()
    assert thread.isRunning() is True

    assert threads_mod.park(thread) is True    # 摘父 + 握住引用
    assert thread.parent() is None, "没有摘出 C++ 父子链，父对象一销毁它就被析构"
    del thread                                 # 连调用方那份引用也不要了（模拟 self._worker = None）

    parent.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()

    finished = threads_mod.drain(5.0)          # 它会自己跑完，跑完就被除名
    assert finished == 0, "寄存的线程没能在限期内落地"
    assert threads_mod.parked() == [], "跑完的线程还留在寄存名单里"


def test_keep_holds_a_thread_before_it_starts(qapp) -> None:
    """`keep()` 登记过的线程，**`start()` 之前不许被除名**（"在册"≠"在跑"）。

    为什么单列一条（2026-10-14 实测踩到）：修剪若写成"`isRunning()` 为假就除名"，
    刚建出来的线程会被**当场删掉登记** —— 于是"从创建到跑完都有人拿着它"这条保证在
    最危险的那段空档（`start()` 之后、`run()` 被调度之前）恰好是断的。实测表现：
    调用方在这几微秒里丢掉最后一份引用 → 跑着的 QThread 被析构 → `abort()`。
    """
    from laoa_trader.ui import threads as threads_mod

    thread = _slow_thread_cls()()
    assert threads_mod.keep(thread) is True
    assert thread in threads_mod.parked(), "还没 start() 就被除名了（那几微秒里没人拿着它）"
    assert thread not in threads_mod.running(), "它在册，但还没开跑"

    thread.start()
    assert thread in threads_mod.running()
    assert thread.wait(5_000) is True
    threads_mod.drain(1.0)                     # 跑完 → 除名（名单不会无限长）
    assert thread not in threads_mod.parked()


def test_park_ignores_a_thread_that_already_finished(qapp) -> None:
    """已经跑完的线程不必寄存（寄存名单不该无限长）。"""
    from laoa_trader.ui import threads as threads_mod

    slow_cls = _slow_thread_cls()
    thread = slow_cls()
    thread.start()
    assert thread.wait(5_000) is True

    assert threads_mod.park(thread) is False
    assert thread not in threads_mod.parked()


def test_worker_wait_all_parks_instead_of_dropping_the_reference(qapp) -> None:
    """`wait_all()` 等不到的线程要**寄存**，绝不能像原来那样"清空登记册"了事。

    原来 `wait_all()` 的最后一句是 `cls._live.clear()` —— 那正是"把最后一份引用丢掉"：
    线程没等到、引用又归零，PySide6 就会析构还在跑的 `QThread`，Qt 走 `qFatal` → `abort()`。
    现在改成寄存：等不到也**绝不让它被析构**。
    """
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import threads as threads_mod

    release = threading.Event()
    started = threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(10.0)
        return "done"

    worker = ui_app.Worker(blocked)
    worker.start()
    assert started.wait(5.0)

    left = ui_app.Worker.wait_all(0.2)          # 故意等不到
    assert left == 1, "这条用例的前提是「等不到」"
    assert worker.isRunning() is True
    assert worker in threads_mod.parked(), "等不到的线程没有被寄存（引用一归零就会被析构）"

    release.set()                               # 放它走
    assert threads_mod.drain(5.0) == 0
    assert threads_mod.parked() == []


def test_score_service_never_starts_new_work_after_stop(qapp, cfg) -> None:
    """`ScoreService.stop()` 之后：不再排队、不再起线程、也不再往界面发结果。

    为什么这三条都要钉：收尾途中只要还有一条路能起新线程，那条新线程就可能在"窗口正在
    被销毁"的那几毫秒里还活着 —— 而 QThread 在这时被析构就是 `abort()`。
    （原来 `_on_done()` 会无条件 `_start_if_idle()`，`_start_if_idle()` 也不看 `_stopped`。）
    """
    from laoa_trader.ui import scores as scores_mod
    from laoa_trader.ui import threads as threads_mod

    service = scores_mod.ScoreService(cfg, cfg.db_path)
    emitted: list = []
    service.updated.connect(lambda results: emitted.append(results))
    running_before = {id(t) for t in threads_mod.running()}

    service.stop()
    assert service.request(["600001"]) == [], "收尾之后仍在接受评分请求"
    assert service._pending == []                 # noqa: SLF001 - 内部队列就是被测对象

    # 收尾之后回来的那一批：一个字都不许写、一个信号都不许发、也不许再排队
    service._on_done({"600001": object()})        # noqa: SLF001 - 直接喂一条"迟到的结果"
    assert emitted == []
    assert service._pending == []                 # noqa: SLF001
    assert service._worker is None                # noqa: SLF001
    assert {id(t) for t in threads_mod.running()} - running_before == set(), \
        "收尾之后又起了新线程"


def test_score_worker_is_not_parented_to_the_service(qapp, cfg) -> None:
    """评分线程**不许挂 C++ 父对象** —— 它挂上去就等于"窗口一销毁它就被析构"。

    这是那个 abort 的第一半修法（第二半是 `stop()` 里的"摘父 + 等 + 寄存"）：
    只要它还挂在服务下面，服务随窗口一起销毁时，一条还在跑的 `QThread` 就会被析构。
    """
    from laoa_trader.ui import scores as scores_mod

    worker = scores_mod._ScoreWorker(cfg, cfg.db_path, ["600001"], None)   # noqa: SLF001
    assert worker.parent() is None
    worker.setParent(None)          # 与 `ScoreService.stop()` 里的那一步等价，不许抛


# ══════════════════════════════════════════════════════════════════════════
# 5) 退出闸：还有线程在跑时，宁可"直接结束进程"也不要 abort
# ══════════════════════════════════════════════════════════════════════════


def test_finish_exit_returns_the_code_when_nothing_is_running() -> None:
    """没有线程在跑时，退出闸**什么都不做**（正常退出路径一点代价都不该有）。"""
    from laoa_trader.ui import threads as threads_mod

    assert threads_mod.finish_exit(0) == 0
    assert threads_mod.finish_exit(3) == 3


def test_finish_exit_never_hard_exits_under_pytest(qapp, monkeypatch) -> None:
    """⚠️ 测试进程里 `finish_exit()` **绝不许**真的结束进程 —— 只返回退出码。

    为什么这条最要紧：后台线程还在跑时它会 `os._exit(code)`（那是 GUI 退出时的正解，
    见 `ui/threads.py`），但在 pytest 里那一声就会把**整套用例的汇总与退出码一起吞掉**：
    "全过"地消失、CI 反而看不出哪里出了问题。判据是 `threads.under_test()`。

    这条用例本身就是那道闸的证据：一旦 `under_test()` 那层被去掉，
    它会把整个 pytest 进程以退出码 7 结束（很难看，但那正是要避免的事）。
    """
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import threads as threads_mod

    release = threading.Event()
    started = threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(10.0)
        return "done"

    worker = ui_app.Worker(blocked)
    worker.start()
    assert started.wait(5.0)
    tried: list = []
    real_drain = threads_mod.drain
    monkeypatch.setattr(threads_mod, "drain",
                        lambda timeout=5.0: tried.append(timeout) or real_drain(0.1))
    try:
        assert threads_mod.under_test() is True
        assert threads_mod.finish_exit(7) == 7, "还在跑线程时也必须在测试里只返回退出码"
        assert tried == [], "测试进程里连「等一下」都不该做 —— 那道闸要在最前面"
    finally:
        release.set()
        assert real_drain(5.0) == 0
