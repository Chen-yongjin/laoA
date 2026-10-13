"""建主窗口的用例**共用的收尾**：一条路，按同一个顺序收干净。

为什么要有这个模块
------------------
"建一只 `MainWindow`"在测试里有十多处（`test_ui_smoke` / `test_titlebar` /
`test_pet_integration` / `test_license_ui` / `test_wording` / `test_wheel_guard` /
`test_scoring` / `test_alert_popup` / `test_gui_selftest`），每处都手写了一遍收尾 ——
而**手写就会漏**，实测漏掉的是同一件东西：`scores.stop()`（评分线程）。

漏收的后果不是"收尾不干净"这么轻：主窗口的 C++ 对象一旦被销毁，挂在它下面的
`ScoreService`、以及挂在服务下面的评分线程会被**连子带孙**一起析构；而线程还在跑的时候
被析构 = `qFatal` → `abort()`，也就是那个查了很久的症状：

* CI（Windows）：跑到 ~90% 进程直接没了、**没有任何用例失败记录**（`pytest.log` 停在半路）；
* 本机（Linux）：`Fatal Python error: Aborted`，faulthandler 的栈停在**评分线程**
  （`ui/scores.py in run`），主线程停在收尾那一行 `sendPostedEvents(..., DeferredDelete)`。

生产代码那边已经补上了"跑着的线程绝不许被析构"（`ui/threads.py` + `ui/scores.py` 的
`stop()`），这里是第二条：**每条用例都按同一个顺序收尾**，让"漏一个服务"不再可能 ——
以后新增线程/服务时，只改这一个地方。
"""

from __future__ import annotations

from typing import Any

#: 收尾时先停的定时器（都挂在主窗口上；不停的话窗口销毁后它们还在事件循环里触发）
_TIMER_ATTRS = ("_timer", "_market_timer", "_flash_timer", "_flash_stop_timer")
#: 收尾时要等的后台线程（概览取数 / 热力图取数）
_WORKER_ATTRS = ("_market_worker", "_heatmap_worker")


def wait_market(window: Any, qapp: Any, timeout_ms: int = 10_000) -> None:
    """等概览页那一轮**后台**取数结束，并把结果送到界面上。

    界面路径（启动 / 定时器 / 切页 /【立即刷新】）都跑在 QThread 里，而 `finished_ok`
    是排队信号 —— 不 wait + processEvents 就断言，会读到上一轮的值。
    """
    worker = getattr(window, "_market_worker", None)
    if worker is not None:
        try:
            worker.wait(timeout_ms)
        except RuntimeError:      # 底层对象已经销毁
            pass
    qapp.processEvents()


def close_top_level(widget: Any, qapp: Any) -> None:
    """收掉一只**独立的顶层控件**（浮窗 / 桌宠 / 单独建出来的页面）。

    与 `close_main_window` 同一个道理，只是对象小一号：这类控件**没有父窗口**，
    `close()` 只是隐藏、`deleteLater()` 只是排队 —— 不显式跑一次 DeferredDelete 就不会真删，
    于是每建一次就漏一只，一直挂在事件循环里（实测：连跑 6 条提醒用例攒下 6 只浮窗）。
    """
    if widget is None:
        return
    stop = getattr(widget, "shutdown", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住后面的收尾
            pass
    try:
        widget.close()
        widget.deleteLater()
    except RuntimeError:      # 底层对象已销毁
        pass

    from PySide6.QtCore import QEvent

    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)   # 真正执行删除
    qapp.processEvents()


def close_main_window(window: Any, qapp: Any, *, drain_timeout: float = 2.0) -> None:
    """把一只主窗口按**唯一那个顺序**收干净（可重复调用，任何一步失败都不挡后面的）。

    顺序是有讲究的，每一步都对应一次真实事故：

    1. **停定时器**：5 秒界面刷新 / 60 秒概览 / 闪烁那两只 —— 窗口还在销毁中时它们照样
       触发，会往半个身子已经拆掉的界面上打；
    2. **停服务**（`scheduler` / `quotes` / `scores`）：先让它们进入"不再收结果、
       不再起新线程"的状态，再谈等不等得到。**`scores` 这一条最要紧** ——
       评分线程比别的慢（每只票读日线 + 评估 17 项 × 2 条公式，还可能取一次快照），
       漏了它，窗口一销毁就是 `Fatal Python error: Aborted`；
    3. **等两条取数线程落地**（`_market_worker` / `_heatmap_worker`）；
    4. **`window.shutdown()`**：生产代码的退出路径（与托盘【退出】同一个方法）——
       桌宠 / 消息列表 / 浮窗 / 授权·关于窗口都是**没有父窗口**的顶层窗口，
       `deleteLater()` 收不掉它们（每建一次主窗口就留一只，孤儿越积越多，最终踩到已析构对象）；
    5. **`deleteLater()` + 显式跑 `DeferredDelete`**：`deleteLater()` 只是排队，
       不 `sendPostedEvents` 就不会真删 —— 窗口没真删，它的定时器与线程就都还在；
    6. **再等一次登记在册的工作线程**（`Worker.wait_all`）：它同时会**寄存**没等到的那几条
       （见 `ui/threads.py`），"等不到"绝不再等于"当它不存在"。
    """
    for name in _TIMER_ATTRS:
        timer = getattr(window, name, None)
        if timer is None:
            continue
        try:
            timer.stop()
        except RuntimeError:      # 底层对象已销毁
            pass

    for name in ("scheduler", "quotes", "scores", "signals"):
        service = getattr(window, name, None)
        stop = getattr(service, "stop", None)
        if not callable(stop):
            continue
        try:
            stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住后面的收尾
            pass

    wait_market(window, qapp)
    for name in _WORKER_ATTRS:
        worker = getattr(window, name, None)
        if worker is None:
            continue
        try:
            if worker.isRunning():
                worker.wait(3_000)
        except RuntimeError:      # 底层对象已销毁
            pass

    # 「关于软件」「状态详情」「提醒详情」是挂在窗口上的对话框，但 Qt 里对话框也是顶层窗口，
    # 可能比主窗口活得更久 —— 显式关掉，别赌"父窗口会带上它"
    for name in ("about_dialog", "status_dialog", "alert_detail_dialog", "signal_dialog"):
        dialog = getattr(window, name, None)
        if dialog is None:
            continue
        try:
            dialog.close()
        except RuntimeError:
            pass

    # 生产代码的退出路径：桌宠 / 消息列表 / 浮窗 / 各类对话框 / 后台线程都在这一个方法里
    try:
        window.shutdown()
    except Exception:  # noqa: BLE001
        pass
    tray = getattr(window, "tray", None)
    if tray is not None:
        try:
            tray.hide()
        except RuntimeError:
            pass
    try:
        window.close()
        window.deleteLater()
    except RuntimeError:
        pass

    from PySide6.QtCore import QEvent

    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)   # 真正执行删除
    qapp.processEvents()

    # 登记在册的工作线程：等一遍，等不到的那几条会被寄存（不会在跑着的时候被析构）
    try:
        from laoa_trader.ui import app as ui_app

        worker_cls = getattr(ui_app, "Worker", None)
        if worker_cls is not None:
            worker_cls.wait_all(drain_timeout)
    except Exception:  # noqa: BLE001 - 没装 Qt / 导入失败时安静跳过
        pass
