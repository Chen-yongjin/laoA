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

    历史上漏的正是这种：竞价取数线程（`_auction_worker`）当时不在 `shutdown()` 的
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
