"""界面层冒烟测试（**可选**：没装 PySide6 时整文件跳过）。

为什么值得写
------------
桌面版最容易出问题的地方不是算法，而是界面接线：按钮连到不存在的函数、
后台线程里直接碰控件（Qt 会随机崩）、状态栏算了不存在的字段……
这些只有真正把窗口创建出来才测得到。

做法：用 Qt 的 `offscreen` 平台插件在**无显示器**的环境（CI / Linux 开发机）里
把主窗口建起来、跑一轮刷新、点一遍按钮，然后正常退出。

前提：装了 PySide6 才会执行；否则 `pytest.skip`（Linux 裸环境下正常现象，见交付说明）。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from laoa_trader import intraday  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.intraday import now_shanghai  # noqa: E402
from laoa_trader.data.engine import DataEngine  # noqa: E402
from laoa_trader.notify import KINDS  # noqa: E402
from laoa_trader.strategy import rules as rules_mod  # noqa: E402
from laoa_trader.ui import app as ui_app  # noqa: E402
from tests._toml import p  # noqa: E402


@pytest.fixture()
def seeded(cfg):
    """给界面准备一点数据，让三个表都有内容可渲染。"""
    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 30:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600001", "低价样本", "银行"),
                                         ("600002", "半导体甲", "半导体")])
        rows = []
        for symbol, base in (("600001", 3.0), ("600002", 12.0)):
            for i, day in enumerate(days):
                price = base * (1 + 0.002 * i)
                rows.append((symbol, day, price, price, price, price,
                             2e7, 2e7 * price, 1.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        # 自检要求 adjust_event 非空（缺了后复权价会算错）
        storage.write_adjust_events(conn, [("600001", days[len(days) // 2],
                                            0.1, 0.0, 0.0, 0.0)])
        storage.write_limit_up_pool(conn, [
            (days[-1], "600002", "半导体甲", 1, "首板", "09:35:00", "09:35:00",
             8e7, 0, "芯片", 5.0, 10.0, 1e9, 0, 1, "首板", 9e7, 12.0, 0, "hithink", "t"),
        ])
        storage.save_pool(conn, [
            {"symbol": "600002", "name": "半导体甲", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy", "score": 3.0,
             "reason": "低价股·热门行业半导体"},
        ], days[-1])
        storage.upsert_position(conn, "600001", name="低价样本", quantity=1000,
                                avg_cost=3.0)
        storage.record_alerts(conn, [
            {"symbol": "600002", "kind": "break_high", "price": 12.5,
             "detail": "现价 12.50 突破 20 日高点"},
        ], days[-1])

    # 让**内存里的配置**与下面写进 config.toml 的 READY_THRESHOLDS 一致：
    # 2 只股票 / 30 个交易日远低于默认门槛（4000 只 / 4.5 年），不放开的话
    # 数据闸门会（正确地）拒绝选股，那测的就不是"按钮/定时"而是闸门本身了
    cfg.min_symbols = 1
    cfg.min_history_years = 0.0

    # 界面"保存设置"会把值写回这个文件；写一份带注释+未知键的，顺便验证不会丢
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(
        "# 用户自己的注释（保存设置后必须还在）\n"
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        'enabled_groups = ["short"]\n'      # 用户拍板后的默认策略集
        'enabled_strategies = []\n'
        'notify_channels = ["windows", "feishu", "tray"]\n'
        'notify_windows_sound = true\n'
        'notify_windows_open_url = true\n'
        'notify_tray_duration_ms = 8000\n'
        'feishu_on = true\n'
        'my_own_key = "别动我"      # 未知键\n',
        encoding="utf-8",
    )
    cfg.source_path = config_file
    return cfg


@pytest.fixture(autouse=True)
def _freeze_intraday_session(monkeypatch):
    """把"现在是不是交易时段"钉成"非交易时段"（**只影响这一份界面用例**）。

    为什么必须钉：这里有不少断言写的是"盘中提醒 未在时段"（标签渲染），
    而 `in_session` 是按**真实时钟**算的 —— 在交易时段内跑测试就会变成"时段中"，
    那条断言会因为"你几点跑的测试"而红，跟被测代码没关系。
    需要"时段中"的用例自己再 monkeypatch（见 `test_status_tags_…`）。
    """
    from laoa_trader import intraday

    monkeypatch.setattr(intraday, "in_session", lambda *a, **k: False)
    yield


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def window(seeded, qapp):
    from laoa_trader.ui import app as ui_app

    assert ui_app.QT_AVAILABLE is True
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    win._tick()
    qapp.processEvents()
    # 概览是**第一个页签**（启动就停在它上面），它会立刻在后台取一次数：
    # 先等它落地，免得它的结果跟用例自己的断言抢同一个 `market_overview`
    _wait_market(win, qapp)
    yield win
    # ── 收尾必须**彻底**：不然会跨用例累积 ──
    # 这堆控件每个都带定时器（5 秒界面刷新 / 60 秒概览），窗口没被真正销毁时它们会一直
    # 在事件循环里触发；用例一多，后面每个 `processEvents()` 都越来越慢 ——
    # 实测整套界面用例从 ~60 秒劣化到 9 分钟（每个用例 +9 秒）。
    # 所以要：停定时器 → 停调度线程 → 等概览后台线程落地 → 关闭窗口 → **强制处理
    # DeferredDelete**（`deleteLater()` 只是排队，不跑 `sendPostedEvents` 就不会真删）。
    win._timer.stop()
    win._market_timer.stop()
    win._auction_timer.stop()
    win._flash_timer.stop()
    win.scheduler.stop()
    win.quotes.stop()               # 实时快照的工作线程也要收（不然后台还在飞）
    _wait_market(win, qapp)
    worker = getattr(win, "_market_worker", None)
    if worker is not None and worker.isRunning():
        worker.wait(3_000)
    if win.about_dialog is not None:
        win.about_dialog.close()    # 「关于软件」窗口同理
    if win.status_dialog is not None:
        win.status_dialog.close()   # 「状态详情」窗口同理
    win.tray.hide()
    win.close()
    win.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)   # 真正执行删除
    qapp.processEvents()


def _layout_widgets(layout) -> list:
    """顶层布局（含其子布局）里直接挂着的控件。

    为什么要这么写：两个输入行曾经是 `layout.addLayout(pos_row)` 挂在**主窗口底部**的，
    这种"全局行"正是本辅助函数要抓的东西 —— 现在它们只应出现在各自页签里。
    """
    found: list = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None:
            found.append(widget)
        sub = item.layout()
        if sub is not None:
            found.extend(_layout_widgets(sub))
    return found


def _tab_page(window, title: str):
    """按页签标题取页面控件（页序变了也不会让断言找错地方）。"""
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    return window.tabs.widget(titles.index(title))


def _header_texts(table) -> list[str]:
    """表格的列头文字（断言"列就是这几列、顺序就是这个顺序"的唯一入口）。"""
    return [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]


def _row_cells(table, row: int) -> list[str]:
    """一行的全部单元格文字（空单元格按空串算，便于整体比对）。"""
    out = []
    for column in range(table.columnCount()):
        item = table.item(row, column)
        out.append(item.text() if item is not None else "")
    return out


def _symbols_of(table) -> list[str]:
    """表格里各行的代码（从 `UserRole` 取，与界面上显示的 `名称(代码)` 无关）。"""
    return [str(table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            for row in range(table.rowCount())]


def test_window_renders_all_panels(window, qapp) -> None:
    # **五个页签**，顺序就是用户给定的顺序（见 `docs/改版方案.md` 第二节）
    assert window.tabs.count() == 5
    assert [window.tabs.tabText(i) for i in range(5)] == list(ui_app.TAB_TITLES)
    # 2026-09-17：第一个页签从「全市概览」改回「大盘概览」（用户要求）——
    # 凡是按标题找页面的地方（截图脚本、`tabs.indexOf`、这条断言）都跟着改
    assert list(ui_app.TAB_TITLES) == ["大盘概览", "自选股池", "持仓监控", "策略选股",
                                       "系统设置"]
    assert ui_app.TAB_MARKET == "大盘概览"
    # 概览页是第一个页签（启动就停在它上面）：看盘第一眼要扫到
    assert window.tabs.widget(0) is window.market_page
    assert window.tabs.currentWidget() is window.market_page
    # 「策略选股」页里挂着公式编辑器（页面本身是"标题行 + 编辑器"的外壳）
    assert window.tabs.tabText(window.tabs.indexOf(_tab_page(window, "策略选股"))) == "策略选股"
    assert window.formula_page.parent() is _tab_page(window, "策略选股")

    # 两张表都填好了：池子 1 只（seeded 里 600002 是策略标的）、持仓 1 只
    assert window.pool_table.rowCount() == 1
    assert window.position_table.rowCount() == 1
    # 列就是用户给定的那几列（顺序也一致）
    assert _header_texts(window.pool_table) == list(ui_app.WATCH_HEADERS)
    assert _header_texts(window.position_table) == list(ui_app.POSITION_HEADERS)
    # 池子表格第一行：`名称(代码)` + 来源（seeded 里是 `低价股` 策略选的 → `策略·低价股`）
    assert window.pool_table.item(0, 0).text() == "半导体甲(600002)"
    # 列下标按新表头取（板块/来源两列因为新加的市值/换手往后挪了两格）
    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("板块")).text() == "半导体"
    assert window.pool_table.item(
        0, ui_app.WATCH_HEADERS.index("来源")).text() == "策略·低价股"
    # 表头右边那行小字：`共 N 只（策略 M · 自选 K）`
    assert window.pool_count_label.text() == "共 1 只（策略 1 · 自选 0）"
    assert window.tabs.currentWidget() is window.market_page      # 启动默认页
    # 表格要在**它自己那一页是当前页**时才谈得上可见（非当前页的控件 isVisible 恒 False）
    window.tabs.setCurrentWidget(window.watch_page)
    qapp.processEvents()
    assert window.pool_table.isVisible() is True


def test_title_area_is_one_row_and_progress_hides_when_idle(window, qapp) -> None:
    """标题区：软件名 · 运行状态 + 两个按钮，**没有那一排操作按钮**；进度条平时不显示。

    改版的核心一条就是这里：原来"三行状态 + 七个按钮"，同一件事说三遍，
    而且【下载数据】和【开始选股】并排 —— 用户点错是迟早的事。
    """
    # 标题区那一行里就是：软件名 / 分隔符 / "运行状态：" / 状态文本 / 【显示详情】【关于软件】
    assert window.app_title_label.text() == ui_app.APP_NAME
    assert window.btn_details.text() == ui_app.BTN_DETAILS_TEXT == "显示详情"
    assert window.btn_about.text() == ui_app.BTN_ABOUT_TEXT == "关于软件"
    # 按钮行与短标签都不在了（`STATUS_TAGS` 整个常量已删）
    assert not hasattr(window, "status_tags")
    assert not hasattr(window, "top_buttons_layout")
    # **细进度条只在任务运行时可见**
    assert window._job_running is False
    assert window.progress.isVisible() is False
    # 用一个"挡住的工作线程"把'任务在跑'这个状态钉住（不能靠 immediate 返回的 lambda：
    # 它可能在断言之前就跑完了，那样测出来的就是"运气"而不是行为）
    gate = threading.Event()
    window._run_worker(lambda: gate.wait(5.0), "测试任务")
    qapp.processEvents()
    assert window._job_running is True
    assert window.progress.isVisible() is True
    gate.set()
    window._worker.wait(3_000)
    window._on_worker_done("测试任务", None)
    qapp.processEvents()
    assert window._job_running is False
    assert window.progress.isVisible() is False


def test_status_line_shows_main_status_without_tags(window) -> None:
    """运行状态：一句话讲清"现在在干什么"；短标签的信息都有别处可看。"""
    text = window.status_label.fullText()
    # 正常状态那一句：数据就绪 + 更新到哪天 + 股票数（竖线一律不许出现）
    assert text.startswith("✅ 数据就绪 · ")
    assert "更新到 09-11" in text
    assert "｜" not in text
    assert "引擎：" not in text                        # 内部词只许出现在详情里

    # 细节进详情：行数 / 后台任务状态 / 日志路径 / 自检阈值都在 tooltip 里
    details = window.status_label.toolTip()
    assert "\n" in details                            # 多行
    assert "后台任务：" in details
    assert "本地行情行数：" in details
    assert "日志文件：" in details
    assert "自检阈值：" in details
    assert "最新数据日期：2026-09-11" in details
    # 定时运行已按用户要求整组移除：详情里不该再有"主跑/补跑/下次自动运行"
    assert "补跑" not in details
    assert "下次自动运行" not in details


# ── 顶部状态区：一行主状态 + 右侧短标签 + 【详情】弹窗 ──


def test_status_main_line_shows_exactly_one_thing(window, qapp) -> None:
    """主状态**一次只讲一件事**：四种优先级各来一条，且都不许串 `｜` / 出现内部词。

    用户原话"太啰嗦、很多词看不懂"：状态栏回答的应该是"现在要我注意什么"，
    所以下面四种情况各自只出一句话。
    """
    from laoa_trader import state
    from laoa_trader.data import preflight

    win = window

    def main_line() -> str:
        win._refresh_status()
        qapp.processEvents()
        text = win.status_label.fullText()
        assert "｜" not in text
        assert "引擎：" not in text
        return text

    # ① 瞬时消息（刚点过的动作 / 任务完成失败）—— 优先级最高
    win._set_status("✅ 下载数据完成：写入 10 行")
    assert main_line() == "✅ 下载数据完成：写入 10 行"

    # ② 正在下载：即使手头只有一条**旧的进度回传**，也显示会动的下载进度
    win._set_status("下载 daily-k 45%（81/181 MB）", progress=True)
    state.begin_download()
    try:
        win.progress.setRange(0, 100)
        win.progress.setValue(48)
        assert main_line().startswith("⏬ 正在下载历史数据 48%")
        # 但"重签 URL 继续下载"这种真正的提示不会被下载进度吞掉（它是给用户看的）
        win._on_worker_note("下载历史数据", "正在重签 URL 继续下载（第 2 次）")
        assert "正在重签 URL 继续下载" in main_line()
    finally:
        state.end_download()
        win.progress.setRange(0, 100)
        win.progress.setValue(0)

    # ③ 本地数据落后 / 不可用 → 一句"在哪个页点哪个按钮能解决"
    #    （按钮名与按钮上的字一模一样，页名也是；改版后【下载数据】住在「系统设置」）
    win._clear_status_message()
    ready_result = win.preflight_result
    win.preflight_result = {"status": preflight.NEEDS_INCREMENTAL, "stale_trading_days": 3}
    assert main_line() == ("⚠️ 本地数据落后 3 个交易日 · 在【系统设置】里点【下载数据】补齐")
    win.preflight_result = {"status": preflight.NEEDS_FULL, "reason": "行情表是空的"}
    assert main_line() == ("⚠️ 本地数据还没准备好 · 在【系统设置】里点【下载数据】下载历史数据")
    assert "行情表是空的" in win._status_details()      # 原始原因在详情里

    # ④ 一切正常 → 就一句"数据就绪"，股票数与日期都是短的
    win.preflight_result = ready_result
    assert main_line().startswith("✅ 数据就绪 · ")


def test_status_line_appends_intraday_state(window, qapp, monkeypatch) -> None:
    """「盘中提醒时段中 / 提醒已暂停」补在运行状态后面（**进行时**的事实，独立一格）。

    为什么要单独测：它是用户点名要出现在标题区里的那几种状态之一，
    而「盘中提醒」那一页已经取消 —— 提现暂停/时段状态的唯一入口就是这一句话。
    """
    win = window
    st = win.scheduler.status()
    win._clear_status_message()

    # 不在时段：**什么都不补**（开机就挂一句"未在时段"是噪音）
    monkeypatch.setattr(win.scheduler, "status", lambda *a, **k: {**st, "in_session": False,
                                                                "intraday_paused": False})
    win._refresh_status()
    qapp.processEvents()
    text = win.status_label.fullText()
    assert text.startswith("✅ 数据就绪 · ")
    assert "盘中提醒" not in text

    # 时段中 → 补一句
    monkeypatch.setattr(win.scheduler, "status", lambda *a, **k: {**st, "in_session": True,
                                                                "intraday_paused": False})
    win._refresh_status()
    qapp.processEvents()
    assert ui_app.INTRADAY_IN_SESSION in win.status_label.fullText()
    assert ui_app.INTRADAY_IN_SESSION == "盘中提醒时段中"

    # 已暂停 → 换成"提醒已暂停"
    monkeypatch.setattr(win.scheduler, "status", lambda *a, **k: {**st, "in_session": True,
                                                                "intraday_paused": True})
    win._refresh_status()
    qapp.processEvents()
    assert ui_app.INTRADAY_PAUSED in win.status_label.fullText()
    assert ui_app.INTRADAY_PAUSED == "提醒已暂停"

    # 三种取值本身的定义（界面/详情/测试共用这一份）
    assert win._intraday_text({"in_session": True}) == ui_app.INTRADAY_IN_SESSION
    assert win._intraday_text({"intraday_paused": True}) == ui_app.INTRADAY_PAUSED
    assert win._intraday_text({}) == ui_app.INTRADAY_OUT_SESSION == ""
    monkeypatch.undo()
    win._refresh_status()
    qapp.processEvents()


def test_status_area_never_shows_pipes_or_internal_terms(window, qapp, monkeypatch) -> None:
    """把"啰嗦"这件事钉死：任何状态下都不许再串 `｜`，也不许出现"引擎：在线"这类内部词。"""
    from laoa_trader import state

    win = window
    cases = {
        "正常": lambda: None,
        "瞬时消息": lambda: win._set_status("✅ 开始选股完成：池子 18 只"),
        "下载中": lambda: (state.begin_download(), win.progress.setValue(42)),
        "落后": lambda: setattr(
            win, "preflight_result",
            {"status": "needs_incremental", "stale_trading_days": 3},
        ),
    }
    try:
        for name, setup in cases.items():
            setup()
            win._refresh_status()
            qapp.processEvents()
            text = win.status_label.fullText()
            assert "｜" not in text, name
            assert "引擎" not in text, name
    finally:
        state.end_download()
        win.progress.setValue(0)
        win.preflight_result = None
        win._clear_status_message()
        monkeypatch.undo()
        win._refresh_status()
        qapp.processEvents()
        assert "｜" not in win.status_label.fullText()


def test_long_transient_message_is_elided_not_wrapped(window, qapp) -> None:
    """太长的瞬时消息：**省略成一行**（不换行），全文仍在 tooltip / 详情里，状态区不顶高。"""
    win = window
    win._refresh_status()
    qapp.processEvents()
    area_height = win.status_area.height()
    min_height = win.minimumSizeHint().height()

    long_text = "⚠️ 下载失败：" + "网络断开了，请检查网线或稍后再点【下载数据】；" * 6
    win._set_status(long_text)
    qapp.processEvents()

    assert win.status_label.fullText() == long_text      # 完整消息没丢
    shown = win.status_label.text()
    assert len(shown) < len(long_text)
    assert shown.endswith("…")                           # 省略号：看得出"这里被截了"
    assert "\n" not in shown                             # 没有换行
    # 一行的高度：状态区与整窗的最小高度都不变（底部不会被顶出屏幕）
    assert win.status_area.height() <= area_height + 2
    assert win.minimumSizeHint().height() <= min_height
    # 「只占一行」的判据必须与字体无关：Windows 上同一个控件实测高 30px，
    # 而 `fontMetrics().height()` 只有 13px（*2=26）——用像素比会把平台的
    # 字体/样式差异当成缺陷。这里改成两条结构判据：
    # ① 控件本身不换行；② 它没有把自己的行撑得比标题区还高。
    assert win.status_label.wordWrap() is False
    assert win.status_label.height() <= win.status_area.height()


def test_status_details_dialog_opens_and_copies(window, qapp) -> None:
    """【详情】弹窗：内容含行数 / 日志路径，可复制，而且与 tooltip 是**同一份文本**。"""
    from PySide6.QtWidgets import QApplication

    win = window
    assert win.status_dialog is None                      # 没点之前不开
    win.btn_details.click()
    qapp.processEvents()
    dialog = win.status_dialog
    assert dialog is not None and dialog.isVisible() is True
    assert dialog.windowTitle() == "状态详情"

    text = win.status_details_text.toPlainText()
    assert win.status_label.toolTip() == text             # tooltip 与弹窗同一份（口径不会分叉）
    assert text.startswith("状态详情")
    for expect in ("后台任务：", "本地行情行数：", "今日池子：1 只（含自选 0 只）",
                   "持仓浮动：", "盘中提醒：", "自检阈值：", "数据目录：", "日志文件："):
        assert expect in text, expect
    # 定时运行整组移除之后，详情里不该再留下"主跑/补跑/下次自动运行"这些字段
    assert "主跑时间：" not in text
    assert "下次自动运行：" not in text
    assert "laoa-trader.log" in text                      # 日志路径要能照着找到
    assert "10,283,203" not in text                       # 长串数字压成"万"
    assert win.status_details_text.isReadOnly() is True

    win.btn_copy_details.click()
    qapp.processEvents()
    assert "本地行情行数：" in QApplication.clipboard().text()
    assert "已复制状态详情" in win.status_label.fullText()
    dialog.close()


def test_buttons_are_short_with_full_tooltips(window, qapp) -> None:
    """每个按钮文字精简（2~4 字），完整说明进 tooltip；而且各自在**该在的那一页**。

    改版后按钮不再集中在一排：标题区只剩【显示详情】【关于软件】，
    数据那三个（下载/刷新/检查盘面）+【暂停提醒】在「系统设置」，
    【开始选股】在「策略选股」 —— 这条用例就是钉住"一处只管一件事"。
    """
    win = window
    top = [win.btn_details, win.btn_about]
    assert [b.text() for b in top] == ["显示详情", "关于软件"]
    data_row = [win.btn_download, win.btn_refresh, win.btn_check, win.btn_pause]
    assert [b.text() for b in data_row] == ["下载数据", "刷新数据", "检查盘面", "暂停提醒"]
    for button in [*top, *data_row, win.btn_run]:
        assert 2 <= len(button.text()) <= 4, button.text()
        assert len(button.toolTip()) >= 10, button.text()     # 完整说明在 tooltip 里
        assert button.toolTip() != button.text()

    # 按钮文字与"指路"文案同一份常量（不然用户按提示找不到按钮）
    from laoa_trader import hints
    assert (win.btn_download.text(), win.btn_details.text()) \
        == (ui_app.BTN_DOWNLOAD_TEXT, ui_app.BTN_DETAILS_TEXT)
    assert win.btn_run.text() == ui_app.BTN_START_TEXT == hints.BTN_RUN_TEXT == "开始选股"

    # **归属**：数据那三个在「系统设置」页、选股在「策略选股」页、两个全局按钮在标题区
    settings = _tab_page(win, ui_app.TAB_SETTINGS)
    formula_tab = _tab_page(win, ui_app.TAB_FORMULA)
    for button in data_row:
        assert settings.isAncestorOf(button), button.text()
    assert formula_tab.isAncestorOf(win.btn_run)
    for button in top:
        assert win.status_area.isAncestorOf(button), button.text()
    # 【显示详情】【关于软件】排在标题区那一行的最右（右对齐）
    qapp.processEvents()
    assert win.btn_details.x() > win.status_label.x()
    assert win.btn_about.x() > win.btn_details.x()
    assert win.btn_about.geometry().right() <= win.status_area.width() + 1

def test_status_line_is_fully_visible_at_960_logical_width(screen_window, qapp) -> None:
    """≈用户那台机器（逻辑可用 960×900 → 窗口 920×760）：运行状态**完整显示**，一行、不被切。

    运行状态是"省略不换行"的单行控件：宽度够就原样显示，不够才省略。
    这一档必须够 —— 用户看到的第一行字不能被截掉。
    """
    win = screen_window(960, 900)
    win._refresh_status()
    qapp.processEvents()
    assert (win.width(), win.height()) == (920, 760)

    shown = win.status_label.text()
    assert shown == win.status_label.fullText()          # 没被省略
    assert shown.startswith("✅ 数据就绪 · ")
    assert "…" not in shown
    # 一行的高度（没有折成两三行把标题区顶高）：判据与字体无关（见上一条用例的说明）
    assert win.status_label.wordWrap() is False
    assert win.status_label.height() <= win.status_area.height()
    assert "\n" not in shown
    # 软件名与两个按钮都在（没有被状态文本挤掉/换行）
    assert win.app_title_label.text() == ui_app.APP_NAME
    assert win.btn_details.isVisible() is True
    assert win.btn_about.isVisible() is True
    # 标题区整块在窗口内（没被下沿切掉）
    assert _bottom_of(win.status_area, win) <= win.height()


# ── 界面主题（皮肤）：银色 / 系统默认，一键切回 ──


def test_silver_theme_is_applied_when_the_window_opens(window, qapp) -> None:
    """默认皮肤是银色（金属感）：窗口一建起来，应用级样式表就已经是它了。"""
    from laoa_trader.ui import theme as theme_mod

    qss = qapp.styleSheet()
    assert theme_mod.current_theme() == "silver"
    assert window.cfg.ui_theme == "silver"
    assert "qlineargradient" in qss                       # 按钮/表头是渐变（金属感）
    assert "QPushButton#primaryAction" in qss             # 主操作按钮按 objectName 命中
    assert window.btn_run.objectName() == "primaryAction"
    assert "#f4f5f7" in qss                               # 窗口底：浅银灰
    # 拉丝纹理只铺在"背景条"上（状态区 / 页脚 / 表头），且是绝对路径
    assert window.status_area.objectName() == "statusArea"
    assert window.market_footer.objectName() == "marketFooter"
    assert "assets/ui/brushed-metal" in qss
    assert "background-repeat: repeat-x" in qss


def test_settings_theme_combo_switches_and_writes_back(window, qapp, seeded) -> None:
    """设置页「界面主题」下拉框：切换**立即生效** + 写回 config.toml（保留注释与未知键）。"""
    from laoa_trader.ui import theme as theme_mod

    box = window.theme_box
    assert [box.itemText(i) for i in range(box.count())] == ["银色（金属感）", "系统默认"]
    assert box.currentData() == "silver"
    silver_qss = qapp.styleSheet()
    assert silver_qss                                     # 银色：有样式表

    # 切到"系统默认" → 样式表被清空（安全绳：一键回到原生外观）
    box.setCurrentIndex(box.findData("system"))
    qapp.processEvents()
    assert qapp.styleSheet() == ""
    assert theme_mod.current_theme() == "system"
    assert window.cfg.ui_theme == "system"
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'ui_theme = "system"' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text        # 只改这一个键
    assert 'my_own_key = "别动我"' in text

    # 切回银色 → 样式表回来，界面照常刷新（换皮肤不许把界面换坏）
    box.setCurrentIndex(box.findData("silver"))
    qapp.processEvents()
    assert qapp.styleSheet() == silver_qss
    assert window.cfg.ui_theme == "silver"
    assert 'ui_theme = "silver"' in (seeded.data_dir / "config.toml").read_text(
        encoding="utf-8"
    )
    window._tick()
    qapp.processEvents()
    assert window.pool_table.rowCount() == 1              # 池子还在、刷新没出异常
    # 状态栏给出"已切换"的瞬时消息（主状态一次只讲一件事）
    assert "界面主题已切换为「银色（金属感）」" in window.status_label.fullText()


def test_window_builds_and_refreshes_in_every_theme(seeded, qapp) -> None:
    """两种主题各建一次窗口 + 来回切三次：都要能建起来、能刷新、能取数。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import theme as theme_mod

    market.clear_cache()
    try:
        for name in ("silver", "system", "silver"):
            seeded.ui_theme = name
            win = ui_app.MainWindow(seeded)
            win.show()
            qapp.processEvents()
            try:
                assert theme_mod.current_theme() == name
                assert bool(qapp.styleSheet()) is (name == "silver")
                win._tick()                                # 刷一轮：不许抛异常
                win._refresh_status()
                qapp.processEvents()
                assert win.pool_table.rowCount() == 1
                assert win.theme_box.currentData() == name  # 下拉框跟着配置走
            finally:
                win.scheduler.stop()
                win.tray.hide()
                win.deleteLater()
                qapp.processEvents()
    finally:
        market.clear_cache()


def test_illegal_theme_in_config_still_opens_window(seeded, qapp) -> None:
    """配置里写错主题（例如中文/少个字母）：按默认银色走，界面照常能用。"""
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import theme as theme_mod

    seeded.ui_theme = "银色金属"
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert theme_mod.current_theme() == "silver"
        assert qapp.styleSheet()                               # 仍然是银色皮肤
        assert win.theme_box.currentData() == "silver"         # 下拉框显示默认
        win._tick()
        qapp.processEvents()
        assert win.position_table.rowCount() == 1
    finally:
        win.scheduler.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()
        seeded.ui_theme = "silver"


def test_intraday_pause_button_toggles(window, qapp, monkeypatch) -> None:
    """【暂停提醒】：设置页按钮与托盘菜单项**两处同步**，运行状态也跟着变。"""
    assert window.btn_pause.text() == "暂停提醒"
    window.on_toggle_intraday()
    assert window.btn_pause.text() == "恢复提醒"
    assert window.scheduler.status()["intraday_paused"] is True
    assert window.act_pause.isChecked() is True          # 托盘那一项也勾上了
    # 运行状态补上"提醒已暂停"（三种取值之一）
    st = window.scheduler.status()
    monkeypatch.setattr(window.scheduler, "status",
                        lambda *a, **k: {**st, "in_session": True})
    window._refresh_status()
    qapp.processEvents()
    assert ui_app.INTRADAY_PAUSED in window.status_label.fullText()
    monkeypatch.undo()

    # 从**托盘菜单**点一次也能恢复（盯盘时窗口多半收在托盘里）
    assert any(a.text() == "暂停提醒" and a.isCheckable()
               for a in window.tray_menu.actions())
    next(a for a in window.tray_menu.actions() if a.text() == "暂停提醒").trigger()
    assert window.btn_pause.text() == "暂停提醒"
    assert window.scheduler.status()["intraday_paused"] is False
    assert window.act_pause.isChecked() is False


def test_add_position_writes_to_db(window, seeded) -> None:
    """【添加持仓】：只收 代码 + 成本价 + 备注 → 库里 `quantity = 0`、`monitor = 1`。"""
    window.pos_symbol.setText("600002")
    window.pos_cost.setText("11.5")
    window.pos_note.setText("突破前高")
    window.on_add_position()
    with storage.connect(seeded.db_path) as conn:
        positions = storage.load_positions(conn)
    assert positions["600002"]["avg_cost"] == 11.5
    assert positions["600002"]["quantity"] == 0          # 界面不再收数量（用户给定的字段）
    assert positions["600002"]["note"] == "突破前高"
    assert positions["600002"]["monitor"] == 1           # 新加的持仓默认在监控中
    assert window.position_table.rowCount() == 2
    assert window.position_table.item(1, 0).text() == "半导体甲(600002)"


def test_add_position_rejects_bad_input(window) -> None:
    window.pos_symbol.setText("abc")
    window.pos_cost.setText("1")
    window.on_add_position()
    assert "6 位股票代码" in window.status_label.fullText()

    window.pos_symbol.setText("600001")
    window.pos_cost.setText("不是数字")
    window.on_add_position()
    assert "成本价必须是数字" in window.status_label.fullText()

    # 成本价必填：没有成本就算不出盈亏比例与止损/止盈位
    window.pos_cost.setText("")
    window.on_add_position()
    assert "成本价必须是数字" in window.status_label.fullText()
    window.pos_cost.setText("0")
    window.on_add_position()
    assert "成本价要大于 0" in window.status_label.fullText()


def test_tray_messages_drain_into_balloon(window) -> None:
    from laoa_trader.notify import tray

    tray.clear()
    tray.notify("测试标题", ["一行字"], cfg=window.cfg)
    assert tray.pending() == 1
    window._drain_tray()
    assert tray.pending() == 0
    assert window._tray_notified == 1


def test_worker_reports_progress_and_failure(window, qapp) -> None:
    """后台线程的三种回调（进度/成功/失败）都要接得上，且失败不崩界面。"""
    from laoa_trader.data import sync

    result = sync.SyncResult(stage="下载", ok=True, rows=10, detail="写入 10 行")
    window._on_worker_done("下载", result)
    assert "写入 10 行" in window.status_label.fullText()

    window._on_worker_failed("下载", "RuntimeError: 断网了\n堆栈……")
    assert "下载失败" in window.status_label.fullText()
    assert "断网了" in window.status_label.fullText()

    window._on_progress("写入行情", 50, 100)
    assert window.progress.value() == 50
    assert window.progress.maximum() == 100


def test_close_event_minimizes_to_tray(window, qapp, monkeypatch) -> None:
    """关闭窗口只最小化到托盘（盯盘工具应常驻后台），**且不弹任何提示**。

    用户明确说不要"关掉窗口后程序还在跑"这条气泡：他是故意关窗的，
    当然知道程序还在跑。所以这里把托盘的气泡调用**记下来断言为空** ——
    只断言 `isHidden()` 的话，谁把那条提示加回去都不会有人发现。
    """
    bubbles: list[tuple] = []
    monkeypatch.setattr(window.tray, "showMessage",
                        lambda *args, **kwargs: bubbles.append(args))
    window.close()
    qapp.processEvents()
    assert window.isHidden() is True       # 行为保留：关窗 = 收进托盘
    assert bubbles == []                   # 但一句提示都不弹


def test_tray_menu_quit_still_exits(window, qapp, monkeypatch) -> None:
    """托盘右键的【退出】照旧能退出（用户仍然需要一条"真的退出"的路）。

    为什么值得单独盯着：关窗不再是退出（只隐藏），**托盘那一条就成了唯一的出口** ——
    它要是坏了，用户只能去任务管理器杀进程。
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    quit_calls: list[str] = []
    monkeypatch.setattr(QApplication, "quit", lambda *a, **k: quit_calls.append("quit"))
    stop_calls: list[str] = []
    monkeypatch.setattr(window.scheduler, "stop", lambda: stop_calls.append("stop"))

    menu = window.tray.contextMenu()
    assert menu is not None
    actions = {action.text(): action for action in menu.actions()}
    assert "退出" in actions
    actions["退出"].trigger()
    qapp.processEvents()

    assert quit_calls == ["quit"]          # 事件循环收到退出请求
    assert stop_calls == ["stop"]          # 调度线程先停（不留后台线程）
    assert QCoreApplication.instance() is not None


def test_test_notify_button_reports_results(window, qapp, monkeypatch) -> None:
    """「发送测试提醒」按钮：走后台线程并把三路结果汇报到状态栏。"""
    from laoa_trader.notify import notify_all as real_notify_all

    monkeypatch.setattr(
        "laoa_trader.notify.notify_all",
        lambda title, lines, kinds=("feishu", "windows", "tray"), cfg=None: {
            "feishu": {"kind": "feishu", "ok": True, "skipped": True, "detail": "未配置"},
            "windows": {"kind": "windows", "ok": False, "detail": "不支持"},
            "tray": {"kind": "tray", "ok": True, "detail": "已投递"},
        },
    )
    assert real_notify_all is not None
    window.on_test_notify()
    assert window._worker is not None
    window._worker.wait(10_000)          # 等后台完成
    qapp.processEvents()
    text = window.status_label.fullText()
    assert "飞书已跳过" in text
    assert "弹窗失败" in text
    assert "托盘成功" in text


def test_doctor_command_prints_report(cfg, capsys, tmp_path) -> None:
    """`--cli --doctor` 自检：路径/依赖/凭据/数据概况，排障第一步。"""
    from laoa_trader.__main__ import cli

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{p(cfg.data_dir)}"', encoding="utf-8")
    assert cli(["--cli", "--doctor", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "老A选股助手 —— 自检" in out
    assert "数据目录" in out and "数据库" in out
    assert "同花顺 Key" in out
    assert "行情行数" in out
    # 缺 Key 时要给出可执行的下一步提示
    assert "下载" in out or "API Key" in out


def test_settings_tab_widgets_reflect_config(window) -> None:
    """「系统设置」= **五组**（顺序固定），每组控件如实反映配置的当前值。

    五组由用户给定（`SETTINGS_GROUPS`）：数据来源 → 通知方式 → 竞价扫描 →
    **T策略** → 其他。第 4 组原来叫「持仓风险」，用户拍板改成 **T策略** 并把止损/止盈
    合并进来（原话：止盈止损比例给客户自己设置，在 T策略 中编辑）。
    策略组/成员策略的启停**不在这一页**（按规格移到「策略选股」的列表里）。
    """
    from laoa_trader import config as config_mod
    from laoa_trader.ui import app as ui_app

    assert list(window.settings_sections) == list(ui_app.SETTINGS_GROUPS) == [
        "数据来源", "通知方式", "竞价扫描", "T策略", "其他",
    ]
    for title, box in window.settings_sections.items():
        assert box.title_label.text() == title
        assert _tab_page(window, ui_app.TAB_SETTINGS).isAncestorOf(box) is True
    # 策略组的勾选**已经不在设置页**（公式/策略页那一边管），别两头都留着
    assert not hasattr(window, "group_boxes")
    assert not hasattr(window, "strategy_boxes")

    # 数据来源：**来源列表**。2026-09-18 用户拍板把主次换回来了 ——
    # 默认 `["hithink", "public"]`（同花顺是主源、免 Key 的公开源是兜底），
    # 所以列表里是两行、同花顺排第一（**列表顺序 = 优先级**）
    assert window.cfg.data_sources == ["hithink", "public"]   # 2026-09-18：同花顺回到主源
    assert "同花顺金融数据服务（内置）" in window.data_source_label.text()
    assert "hithink" in window.data_source_label.text()        # 把配置里的原值也写出来
    assert window.data_source_label.text().startswith("取数顺序")   # 顺序 = 优先级
    # 列表只画**已启用**的来源（这一份配置里的两个）；能力文案来自注册表
    assert list(window.source_rows) == [ui_app.BUILTIN_SOURCE, "public"]
    from laoa_trader.data import sources as sources_mod

    state = {s["id"]: s for s in sources_mod.source_states(window.cfg)}
    builtin = window.source_rows[ui_app.BUILTIN_SOURCE]
    assert builtin.name_label.text() == state["hithink"]["name"]
    # 排第一的那个 = 主来源（角色标记只给第一位）
    assert builtin.tag_label.text() == "主来源"
    # 公开源排在它后面 → 不给角色标记，而是按 Key 状态显示"免 Key"（它确实不用 Key）
    public = window.source_rows["public"]
    assert public.tag_label.text() == "免 Key"
    # 能力说明写在界面上（换来源会丢掉什么，用户必须看得见）——**文案来自真相源**
    assert builtin.capability_label.text() == \
        "提供：" + state["hithink"]["capabilities_text"]
    assert "实时快照" in builtin.capability_label.text()
    assert builtin.enabled_box.isChecked() is True
    assert builtin.enabled_box.isEnabled() is False            # 内置来源不能在界面上关
    # 非当前页签里的控件 `isVisible()` 恒为 False，所以这里看的是「有没有被显式藏起来」
    assert builtin.btn_delete.isHidden() is True               # 也不能删
    # **有 Key 输入框、也有【测试连接】**（2026-09-18 用户澄清："设置里让你不要配 KEY，
    # 但是你也要给个 key 的输入口啊" —— 那句"不保留自己的 KEY"说的是**程序里不许预置
    # 开发者自己的 Key**，不是不给用户填）。同一条规则仍然成立：**程序不预置任何 Key**
    assert builtin.key_edit is not None
    assert builtin.btn_test is not None
    assert builtin.btn_test.text() == "测试连接"
    # 输入框里显示的**只能是用户自己 config 里的那个值**（不是程序写死的）：
    # 这条夹具里是 "test-key"，所以框里就是 "test-key" —— 它证明"显示的是配置，
    # 不是某个内置常量"。而"程序不预置自己的 Key"由下一条钉
    assert builtin.key_edit.text() == window.cfg.hithink_api_key
    from laoa_trader.config import Config as _Config
    assert _Config().hithink_api_key == "", "出厂配置里不许预置任何 Key"
    # 输入框是**密码框**（Key 不该明晃晃挂在屏幕上）
    from PySide6.QtWidgets import QLineEdit
    assert builtin.key_edit.echoMode() == QLineEdit.EchoMode.Password
    # 申请地址那一行照样在（输入框 + 申请地址并存）
    assert "fuyao.aicubes.cn" in builtin.key_label.text()
    # 2026-09-18：用户把主源换回同花顺（公开接口实测会限流，同花顺不会轻易限流），
    # 所以这一行的标记从"备用源"改回"主来源" —— 标记与文案都由 `ui_app` 的常量给出，
    # 这里同时钉"常量本身"和"界面上渲染出来的那行字"，免得两处各改一半
    assert ui_app.BUILTIN_BACKUP_TAG == "主来源"
    assert builtin.key_notice_text == ui_app.BUILTIN_BACKUP_TEXT.format(
        url=ui_app.BUILTIN_KEY_URL)
    assert builtin.key_notice_text.startswith("主来源：同花顺金融数据服务（需要 Key，申请地址")
    assert "主来源：同花顺金融数据服务（需要 Key，申请地址" in builtin.key_label.text()
    assert "备用源" not in builtin.key_label.text()          # 旧口径的标记不许再出现
    assert f'href="{ui_app.BUILTIN_KEY_URL}"' in builtin.key_label.text()   # 地址可点开
    assert builtin.key_label.openExternalLinks() is True
    # 注册表里还有**没启用**的来源（东方财富）→ 进【添加来源】候选，不画进列表
    assert [s["id"] for s in window._addable_sources()] == ["eastmoney"]
    assert "东方财富" in window.source_add_hint.text()
    assert window.btn_add_source.text() == "添加来源"
    # 数据量：显示成"几个月"，可编辑的是 history_years
    assert window.history_years_box.value() == pytest.approx(window.cfg.history_years)
    assert "6 个月" in window.data_amount_label.text()
    assert window.data_dir_edit.isReadOnly() is True
    assert window.log_path_edit.isReadOnly() is True
    assert window.data_dir_edit.text() == str(window.cfg.data_dir)

    # 通知方式：四个勾选框 + 参数
    assert window.popup_box.isChecked() == bool(window.cfg.notify_popup)
    assert set(window.channel_boxes) == set(KINDS)
    chosen = {str(c).lower() for c in (window.cfg.notify_channels or [])}
    assert {n for n, box in window.channel_boxes.items() if box.isChecked()} == chosen
    assert window.sound_box.isChecked() == bool(window.cfg.notify_sound)
    assert window.flash_seconds_box.value() == int(window.cfg.notify_flash_seconds)
    assert window.popup_seconds_box.value() == int(window.cfg.notify_popup_seconds)
    assert window.popup_items_box.value() == int(window.cfg.notify_popup_max_items)
    assert window.win_sound_box.isChecked() == bool(window.cfg.notify_windows_sound)
    assert window.win_url_box.isChecked() == bool(window.cfg.notify_windows_open_url)
    assert window.tray_duration.value() == int(window.cfg.notify_tray_duration_ms)
    assert window.feishu_on_box.isChecked() == bool(window.cfg.feishu_on)
    assert window.feishu_app_id.text() == window.cfg.feishu_app_id

    # 竞价扫描：开关 + 8 个参数（沿用现有控件）
    assert window.auction_on_box.isChecked() == bool(window.cfg.intraday_auction)
    assert window.auction_min_pct_box.value() == pytest.approx(window.cfg.auction_min_pct)
    assert window.auction_max_pct_box.value() == pytest.approx(window.cfg.auction_max_pct)
    assert window.auction_amount_box.value() == pytest.approx(
        window.cfg.auction_min_amount / 1e4)
    assert window.auction_ratio_box.value() == pytest.approx(
        window.cfg.auction_min_volume_ratio)
    assert window.auction_score_box.value() == int(window.cfg.auction_min_score)
    assert window.auction_items_box.value() == int(window.cfg.auction_alert_max_items)
    assert set(window.auction_board_boxes) == set(config_mod.AUCTION_BOARDS)
    assert window.auction_scan_at_edit.text() == ", ".join(window.cfg.auction_scan_at)

    # T策略组：止损/止盈 + **T策略（默认关）+ 四个做T阈值**（这四个原来完全没有界面入口）
    assert window.stop_loss_box.value() == pytest.approx(abs(window.cfg.stop_loss) * 100)
    assert window.take_profit_box.value() == pytest.approx(abs(window.cfg.take_profit) * 100)
    assert window.intraday_t_box.isChecked() == bool(window.cfg.intraday_t)
    assert window.cfg.intraday_t is False                    # 用户拍板：默认关
    assert window.t_high_min_gain_box.value() == pytest.approx(
        window.cfg.t_high_min_gain_pct)
    assert window.t_high_pullback_box.value() == pytest.approx(
        window.cfg.t_high_pullback_pct)
    assert window.t_low_min_drop_box.value() == pytest.approx(window.cfg.t_low_min_drop_pct)
    assert window.t_low_rebound_box.value() == pytest.approx(window.cfg.t_low_rebound_pct)
    for box in (window.t_high_min_gain_box, window.t_high_pullback_box,
                window.t_low_min_drop_box, window.t_low_rebound_box):
        assert len(box.toolTip()) >= 10                       # 每个阈值都写清"跟谁比、比多少"
    # 这七个控件**确实在「T策略」组里**（止损/止盈是用户要求合并进来的，
    # 只断言"窗口上有这些控件"是不够的 —— 它们可能在别的组里）
    t_group = window.settings_sections[ui_app.SETTINGS_GROUPS[3]]
    assert t_group.title_label.text() == "T策略"
    for widget in (window.intraday_t_box, window.stop_loss_box, window.take_profit_box,
                   window.t_high_min_gain_box, window.t_high_pullback_box,
                   window.t_low_min_drop_box, window.t_low_rebound_box):
        assert t_group.isAncestorOf(widget) is True

    # 其他：主题 / 当日异动（默认关）/ 自选上限 / 是否进池
    assert window.theme_box.currentData() == window.cfg.ui_theme
    assert window.anomaly_box.isChecked() == bool(window.cfg.intraday_anomaly)
    assert window.cfg.intraday_anomaly is False              # 用户拍板：默认关
    assert window.watchlist_max_box.value() == int(window.cfg.watchlist_max)
    assert window.watchlist_in_pool_box.isChecked() == bool(window.cfg.watchlist_in_pool)
    # 底部就是那**一个**保存按钮
    assert window.save_settings_button.text() == "保存设置"
    assert len([b for b in window.settings_sections["其他"].findChildren(
        type(window.save_settings_button)) if b.text() == "保存设置"]) == 0  # 只在页脚

def test_watch_table_headers_and_source_column(window) -> None:
    """「自选股池」的列**就是用户给定的那 8 列**（顺序也一致），「来源」列区分策略与自选。

    2026-09-17 改版（用户要求）：在「涨幅」后面加「市值」「换手」两列，
    「提醒」列改成「监控开关」—— 所以列数从 6 变 8，`板块/来源` 的下标从 3/4 挪到 5/6。
    """
    assert window.pool_table.columnCount() == 8
    assert _header_texts(window.pool_table) == [
        "名称(代码)", "现价", "涨幅", "市值", "换手", "板块", "来源", "监控开关",
    ]
    assert window.pool_table.columnCount() == len(ui_app.WATCH_HEADERS)
    # seeded 里 600002 是 `低价股` 策略选中的（不是自选）：来源 = **哪条策略**
    # （用户要求这一列回答"是哪条策略选出来的"，而不是只写组别）
    assert window.pool_table.item(0, 0).text() == "半导体甲(600002)"
    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("来源")).text() \
        == "策略·低价股"
    # 组别与持有期挪进了行 tooltip（列数被用户定死，不能加列）
    tip = window.pool_table.item(0, 0).toolTip()
    assert "来源：策略·低价股" in tip
    assert "组别：波段·T+10（T+10）" in tip
    # 「备注」不再单独占一列（列数被用户定死）：它进了**整行的 tooltip**
    assert "备注" not in "".join(_header_texts(window.pool_table))


def test_pool_table_shows_watchlist_rows_not_in_pool(window, seeded, qapp) -> None:
    """手工加的自选**必须立刻出现**在这张表里（哪怕今天的池子还没重建）。

    这是最容易做错的一条：池子表格原来读的是 `stock_pool` 表（**建池那一刻**的快照），
    两次建池之间加的自选根本不在里面 —— 用户看到的是"我明明加了它，表里没有"。
    """
    assert window.pool_table.rowCount() == 1
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="龙头")
    window._invalidate_summary()
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    assert _symbols_of(window.pool_table) == ["600002", "600001"]   # 池内在前，自选在后
    row = window.pool_table.rowCount() - 1
    assert window.pool_table.item(row, 0).text() == "低价样本(600001)"
    assert window.pool_table.item(row, ui_app.WATCH_HEADERS.index("来源")).text() == "自选"
    # 板块来自 stock_basic（这一列的位置跟着新列往后挪了两格）
    assert window.pool_table.item(
        row, ui_app.WATCH_HEADERS.index("板块")).text() == "银行"
    # 表头那行小字：`共 2 只（策略 1 · 自选 1）`
    assert window.pool_count_label.text() == "共 2 只（策略 1 · 自选 1）"
    # 备注在整行的 tooltip 里（悬浮任何一格都能看到）
    for column in range(window.pool_table.columnCount()):
        assert "备注：龙头" in window.pool_table.item(row, column).toolTip()


def test_pool_row_hover_shows_note_and_monitor_state(window, seeded, qapp) -> None:
    """悬浮看备注 + 监控状态（表里没有「状态」列，这两件事只能靠 tooltip 说清）。"""
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="龙头")
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    row = _symbols_of(window.pool_table).index("600001")

    tip = window.pool_table.item(row, 0).toolTip()
    assert "备注：龙头" in tip
    assert "监控中" in tip
    # 停用之后：tooltip 里要说清"已停用"，来源列也要标出来（用户得看得见自己停过它）
    with storage.connect(seeded.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600001", False)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    assert "已停用" in window.pool_table.item(row, 0).toolTip()
    assert window.pool_table.item(
        row, ui_app.WATCH_HEADERS.index("来源")).text() == "自选（已停用）"
    # 「监控开关」那一格跟着变成"关闭"（与来源列的"已停用"说的是同一件事）
    assert window.pool_table.item(
        row, ui_app.WATCH_MONITOR_COLUMN).text() == ui_app.MONITOR_OFF_TEXT

    # 策略标的（不是自选）：说明它也在被盯，但**没有**"停用"这一说
    assert "策略选中的标的" in window.pool_table.item(0, 0).toolTip()


def test_pool_row_click_opens_xueqiu(window, qapp, monkeypatch) -> None:
    """单击「名称(代码)」→ 用浏览器打开雪球个股页（地址来自 `notify.windows` 的映射）。"""
    opened: list[str] = []
    monkeypatch.setattr(ui_app.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)

    window._on_table_cell_clicked(window.pool_table, 0, 0)
    assert opened == ["https://xueqiu.com/S/SH600002"]    # 6 开头 → 沪市（SH）
    # 点**别的列**不跳（用户只是想选行的时候不该被弹走）
    window._on_table_cell_clicked(window.pool_table, 0, 3)
    assert len(opened) == 1


def test_pool_row_click_does_not_open_for_invalid_code(window, qapp, monkeypatch) -> None:
    """代码不是合法 6 位数字时**什么都不做**（绝不拼一个 404 页出来）。"""
    from laoa_trader import pool as pool_mod

    opened: list[str] = []
    monkeypatch.setattr(ui_app.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: [
        {"symbol": "abc", "name": "坏代码", "strategy": "", "source": "自选",
         "source_label": "自选", "note": "", "industry": ""},
    ])
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    assert window.pool_table.item(0, 0).text() == "坏代码(abc)"
    window._on_table_cell_clicked(window.pool_table, 0, 0)
    assert opened == []


def test_pool_row_menu_actions(window, seeded, qapp, monkeypatch) -> None:
    """右键菜单：【删除】/【关闭监控】↔【打开监控】/【打开雪球】。

    策略标的（不是自选）没有"停用监控"这一说 → 那一项**灰掉**并给理由，
    而不是给一个点了没反应的菜单项。
    """
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="")
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = _symbols_of(window.pool_table)

    # 策略标的（600002）：删除可用、关闭监控灰掉、打开雪球可用
    menu = window._row_menu(window.pool_table, rows.index("600002"), "pool")
    actions = {a.text(): a for a in menu.actions() if a.text()}
    assert list(actions)[:2] == ["删除", "关闭监控"]
    assert actions["关闭监控"].isEnabled() is False
    assert "策略" in actions["关闭监控"].toolTip()
    assert actions["打开雪球"].isEnabled() is True

    # 自选（600001）：可以关闭监控；点了之后它变成"打开监控"
    menu = window._row_menu(window.pool_table, rows.index("600001"), "pool")
    toggle = next(a for a in menu.actions() if a.text() == "关闭监控")
    assert toggle.isEnabled() is True
    toggle.trigger()
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_map(conn)["600001"]["enabled"] == 0
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    menu = window._row_menu(window.pool_table, _symbols_of(window.pool_table).index("600001"),
                            "pool")
    assert any(a.text() == "打开监控" for a in menu.actions())
    assert "监控" in window.status_label.fullText() or "停用" in window.status_label.fullText()


def test_pool_row_menu_delete_removes_watchlist_then_pool_row(window, seeded, qapp) -> None:
    """【删除】：自选行删自选；纯策略行从**今日池子**里删掉（并说清下次会被重新评估）。"""
    from laoa_trader import pool as pool_mod

    window.on_pool_row_delete("600002")            # 纯策略行
    qapp.processEvents()
    assert "已从今日池子移除 600002" in window.status_label.fullText()
    assert "重新评估" in window.status_label.fullText()
    assert pool_mod.pool_symbols(seeded.db_path) == []

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
    window.on_pool_row_delete("600001")            # 自选行
    qapp.processEvents()
    assert "已删除自选 600001" in window.status_label.fullText()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_map(conn) == {}


def test_pool_table_not_rebuilt_when_signature_unchanged(window, qapp) -> None:
    """内容（含价格与提醒）没变就**不重建**：每 5 秒重建会丢选中行与滚动位置。"""
    item_before = window.pool_table.item(0, 0)
    window._refresh_pool_table()
    window._refresh_pool_table()
    assert window.pool_table.item(0, 0) is item_before


def test_pool_empty_label_visible_only_when_pool_empty(window, qapp, monkeypatch) -> None:
    """池子非空 → 提示收起；池子空 → 提示出现、表格清空（并说清"下一步点哪里"）。"""
    from laoa_trader import pool as pool_mod

    # 这一页不是当前页时里面的控件 isVisible 恒 False —— 先切过去再谈可见性
    window.tabs.setCurrentWidget(window.watch_page)
    qapp.processEvents()
    assert window.pool_empty_label.isVisible() is False      # seeded 的池子里有 1 只

    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: [])
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    assert window.pool_table.rowCount() == 0
    assert window.pool_empty_label.isVisible() is True
    assert "【开始选股】" in window.pool_empty_label.text()
    assert "自选" in window.pool_empty_label.text()
    assert window.pool_count_label.text() == "共 0 只（策略 0 · 自选 0）"


def test_pool_table_has_exact_headers_and_monitor_column(window, seeded, qapp) -> None:
    """「监控开关」列：格子里是 `开启`/`关闭`，**今天的提醒完整内容在 tooltip 里**。

    2026-09-17（用户要求）：原来那一列画的是提醒的短标签（`放量突破`），
    现在这一列画的是监控开关（可点切换）；提醒**一点没丢** ——
    短标签 + 整句话（含日期）都进了这一格的 tooltip。
    """
    from laoa_trader.intraday import now_shanghai

    today = now_shanghai().strftime("%Y-%m-%d")
    with storage.connect(seeded.db_path) as conn:
        storage.record_alerts(conn, [
            {"symbol": "600002", "kind": "break_high", "price": 12.85,
             "detail": "现价 12.85 突破 20 日高点 12.60"},
        ], today)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    item = window.pool_table.item(0, ui_app.WATCH_MONITOR_COLUMN)
    assert item.text() == ui_app.MONITOR_ON_TEXT          # 策略标的默认在监控中
    assert "放量突破" in item.toolTip()                    # 短标签（原「提醒」列的文字）
    assert "现价 12.85 突破 20 日高点 12.60" in item.toolTip()
    assert today in item.toolTip()
    # 表头文字写清这一列是什么（用户要求"改成监控开关"）
    assert _header_texts(window.pool_table)[ui_app.WATCH_MONITOR_COLUMN] == "监控开关"


# ── 「持仓监控」表 ──


def test_position_table_headers(window) -> None:
    """「持仓监控」的列**就是用户给定的那 10 列**：没有"数量"这一列。

    2026-09-17（用户要求）：加「市值」「换手」，「提醒」列改成「监控开关」。
    """
    assert window.position_table.columnCount() == 10
    assert _header_texts(window.position_table) == [
        "名称(代码)", "成本价", "现价", "涨幅", "市值", "换手",
        "盈亏比例", "止损位", "止盈位", "监控开关",
    ]
    assert window.position_table.columnCount() == len(ui_app.POSITION_HEADERS)
    assert "数量" not in "".join(_header_texts(window.position_table))


def test_position_row_shows_pnl_stop_and_target(window, seeded, qapp) -> None:
    """一行的全部单元格：成本 / 现价（本地收盘，**不带任何符号**）/ 涨幅 /
    市值 / 换手 / 盈亏比例 / 止损止盈 / 监控开关。

    seeded：600001 成本 3.0；本地最新**不复权**收盘 3.174、前收 3.1675…
    （库里的 post-adjust 视图会把它放大，所以这条用例同时钉住"不许用后复权价"）。
    2026-09-17（用户要求）：现价/涨幅**不再加 `*`**（用户把它误读成监控状态标记），
    区别改由 tooltip 说清。
    """
    from laoa_trader import market
    from laoa_trader.data import storage as st

    with st.connect(seeded.db_path) as conn:
        bar = st.latest_raw_closes(conn, ["600001"])["600001"]
    close, prev = bar["close"], bar["prev_close"]

    cells = _row_cells(window.position_table, 0)
    assert cells[0] == "低价样本(600001)"
    assert cells[1] == f"{3.0:.2f}"
    assert cells[2] == f"{close:.2f}"                       # **不带 `*`**（用户要求去掉）
    assert cells[3] == f"{(close - prev) / prev * 100:+.2f}%"
    assert "*" not in "".join(cells)                        # 整行都不许出现星号
    # 市值/换手：没有实时快照 → `—`（**不是 0**）
    assert cells[4] == market.DASH
    assert cells[5] == market.DASH
    assert cells[6] == f"{(close - 3.0) / 3.0 * 100:+.2f}%"  # 与「现价」同一个价
    assert cells[7] == f"{3.0 * (1 - seeded.stop_loss):.2f}"
    assert cells[8] == f"{3.0 * (1 + seeded.take_profit):.2f}"
    assert cells[9] == ui_app.MONITOR_ON_TEXT               # 默认在监控中
    # 「现价」的 tooltip 必须点明"这是本地最新收盘价（MM-DD），不是实时价"
    tip = window.position_table.item(0, 2).toolTip()
    assert "不是实时价" in tip
    assert "不复权" in tip
    assert f"（{bar['date'][5:]}）" in tip                  # MM-DD 口径


def test_position_pnl_uses_the_same_price_as_the_price_column(window, seeded, qapp) -> None:
    """有实时快照时：**现价与盈亏比例用同一个价**（这是要修掉的那个 bug 的验收点）。

    老代码的「盈亏比例」读 `stock_daily_hfq`（后复权）而现价是另一处算的 ——
    两列经常对不上，送股之后甚至差一倍。
    """
    window.quotes.apply(
        {"600001": {"symbol": "600001", "price": 4.0, "pct": 3.5, "at": time.time()}}
    )
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()

    cells = _row_cells(window.position_table, 0)
    assert cells[2] == "4.00"                               # 实时价：**不带** `*`
    assert cells[3] == "+3.50%"
    pnl = ui_app.POSITION_HEADERS.index("盈亏比例")
    assert cells[pnl] == f"{(4.0 - 3.0) / 3.0 * 100:+.2f}%"  # 用同一个 4.00 算出来的
    assert cells[pnl] == "+33.33%"
    assert "实时快照" in window.position_table.item(0, 2).toolTip()


def test_position_pnl_is_dash_when_there_is_no_price(window, seeded, qapp) -> None:
    """没有价（本地也没行情）→ `—`，**绝不显示 0.00%**（那是"刚好打平"这个真实结论）。"""
    from laoa_trader.data import storage as st

    window.pos_symbol.setText("000002")       # 库里没有它的行情
    window.pos_cost.setText("10")
    window.on_add_position()
    qapp.processEvents()

    row = _symbols_of(window.position_table).index("000002")
    cells = _row_cells(window.position_table, row)
    assert cells[2] == "—"
    assert cells[ui_app.POSITION_HEADERS.index("盈亏比例")] == "—"
    assert "0.00%" not in "".join(cells)


def test_position_row_menu_toggle_monitor(window, seeded, qapp) -> None:
    """右键：删持仓 / 关闭监控↔打开监控（关了就不再进做T提示）。"""
    from laoa_trader import intraday

    menu = window._row_menu(window.position_table, 0, "position")
    texts = [a.text() for a in menu.actions() if a.text()]
    assert texts == ["删除", "关闭监控", "打开雪球"]
    next(a for a in menu.actions() if a.text() == "关闭监控").trigger()
    qapp.processEvents()
    assert "关闭监控" in window.status_label.fullText()

    # `monitor = 0` → 做T的观察面里不再有它（这是"关闭监控"的**真实作用**）
    assert list(intraday.held_positions(seeded.db_path)) == []
    # 再打开 → 回来
    menu = window._row_menu(window.position_table, 0, "position")
    next(a for a in menu.actions() if a.text() == "打开监控").trigger()
    qapp.processEvents()
    assert list(intraday.held_positions(seeded.db_path)) == ["600001"]


def test_position_row_right_click_delete(window, seeded, qapp) -> None:
    """右键【删除】删掉这一行（不再弹输入框问代码）。"""
    menu = window._row_menu(window.position_table, 0, "position")
    next(a for a in menu.actions() if a.text() == "删除").trigger()
    qapp.processEvents()
    assert "已删除持仓 600001" in window.status_label.fullText()
    assert window.position_table.rowCount() == 0


def test_position_row_click_opens_xueqiu(window, qapp, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(ui_app.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    window._on_table_cell_clicked(window.position_table, 0, 0)
    assert opened == ["https://xueqiu.com/S/SH600001"]


def test_t_strategy_checkbox_is_bound_to_config(window, seeded, qapp) -> None:
    """「T策略」复选框：默认取配置、勾一下立刻写回 config.toml 的 `intraday_t`。"""
    assert window.t_strategy_box.isChecked() is False        # 默认关（用户拍板）
    window.t_strategy_box.setChecked(True)
    qapp.processEvents()
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert "intraday_t = true" in text
    assert window.cfg.intraday_t is True
    # 设置页那一组与它是**同一个键**：保存「T策略」组也能改它
    assert window.intraday_t_box.isChecked() is False        # 面板自己的初值来自打开时那份配置
    assert "T策略" in window.status_label.fullText()


def test_t_group_single_save_writes_ratios(window, seeded, qapp) -> None:
    """「系统设置 → T策略」那一组的单独保存：止损/止盈写回配置 + 立刻重算表格两列。

    止损/止盈原来单独一组叫「持仓风险」，用户拍板**合并进 T策略 组**（"止盈止损比例给客户
    自己设置，在 T策略 中编辑"）—— 所以这里同时钉住：两个比例与那一组的其它键
    （开关 + 四个阈值）走的是**同一个**保存入口。
    """
    window.stop_loss_box.setValue(7.5)
    window.take_profit_box.setValue(15.0)
    window.on_save_t_strategy()
    qapp.processEvents()
    assert window.cfg.stop_loss == pytest.approx(0.075)
    assert window.cfg.take_profit == pytest.approx(0.15)
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert "stop_loss = 0.075" in text
    assert "take_profit = 0.15" in text
    # 表格里的止损位/止盈位立刻跟上（3.0 × (1∓比例)）
    cells = _row_cells(window.position_table, 0)
    # 止损位/止盈位这两列的下标跟着新加的「市值」「换手」往后挪了两格
    assert cells[ui_app.POSITION_HEADERS.index("止损位")] == f"{3.0 * (1 - 0.075):.2f}"
    assert cells[ui_app.POSITION_HEADERS.index("止盈位")] == f"{3.0 * (1 + 0.15):.2f}"
    # 老名字 `on_save_risk` 是它的别名（老调用点不该炸），做的是同一件事
    window.stop_loss_box.setValue(6.0)
    window.on_save_risk()
    qapp.processEvents()
    assert window.cfg.stop_loss == pytest.approx(0.06)


# ── 输入行在各自页签里（不再挂在窗口底部）──


def test_input_rows_live_in_their_own_tabs(window) -> None:
    """持仓/自选的输入行在各自页签里，而且**不在**中央控件的顶层布局里（原来的全局行没了）。"""
    pos_page = _tab_page(window, ui_app.TAB_POSITION)
    watch_page = _tab_page(window, ui_app.TAB_WATCH)

    for widget in (window.pos_symbol, window.pos_cost, window.pos_note):
        assert pos_page.isAncestorOf(widget) is True
    for widget in (window.watch_symbol, window.watch_note):
        assert watch_page.isAncestorOf(widget) is True

    # 输入行排在表格**上方**（先填再点，视线不用来回跳）
    assert pos_page.layout().itemAt(1).layout().indexOf(window.pos_symbol) >= 0
    assert pos_page.layout().itemAt(2).widget() is window.position_table
    assert watch_page.layout().itemAt(1).layout().indexOf(window.watch_symbol) >= 0
    assert watch_page.layout().itemAt(2).widget() is window.pool_table

    # 全局行确实删掉了：中央控件顶层布局（含子布局）里不再挂着这些输入框
    top_level = _layout_widgets(window.centralWidget().layout())
    for widget in (window.pos_symbol, window.pos_cost, window.pos_note,
                   window.watch_symbol, window.watch_note):
        assert widget not in top_level


def test_watch_symbol_enter_adds_to_watchlist(window, seeded, qapp) -> None:
    """自选股输入框回车 = 点【添加自选】（加自选是"敲代码回车"的连击动作）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.watch_symbol.setText("600001")
    window.watch_note.setText("回车加的")
    QTest.keyClick(window.watch_symbol, Qt.Key.Key_Return)
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        rows = storage.load_watchlist(conn)
    assert rows[0]["symbol"] == "600001"
    assert rows[0]["name"] == "低价样本"
    assert rows[0]["note"] == "回车加的"
    # 加完立刻出现在「自选股池」表里（**不用等今晚重新建池**）
    assert _symbols_of(window.pool_table) == ["600002", "600001"]
    assert "已加自选：600001 低价样本" in window.status_label.fullText()


def test_pos_symbol_enter_adds_position(window, seeded, qapp) -> None:
    """持仓输入框回车 = 点【添加持仓】（数量已不在界面上，所以只填代码 + 成本价）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.pos_symbol.setText("600002")
    window.pos_cost.setText("11.5")
    QTest.keyClick(window.pos_symbol, Qt.Key.Key_Return)
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        positions = storage.load_positions(conn)
    assert positions["600002"]["avg_cost"] == 11.5
    assert positions["600002"]["quantity"] == 0
    assert window.position_table.rowCount() == 2


# ── 导入/下载期间界面不卡（用户实报"下载数据时界面卡死"）──


def test_tick_skips_heavy_work_while_downloading(window, qapp, monkeypatch) -> None:
    """下载/导入期间 `_tick()`：**不查数据概况**、也不重建表格；结束后立刻补齐一次。

    为什么这条最关键：`data_summary()` 里有全表 COUNT（几百万行），
    而导入线程正在同一张表上大批量写入 —— 每 5 秒按住界面几百毫秒，看着就是"卡死"。
    """
    from laoa_trader import state

    from laoa_trader.ui import app as ui_app

    win = window
    counters = {"summary": 0, "pool": 0}
    real_summary, real_pool = win.engine.summary, win._refresh_pool_table
    monkeypatch.setattr(win.engine, "summary",
                        lambda: (counters.__setitem__("summary", counters["summary"] + 1),
                                 real_summary())[1])
    monkeypatch.setattr(win, "_refresh_pool_table",
                        lambda: (counters.__setitem__("pool", counters["pool"] + 1),
                                 real_pool())[1])

    win._invalidate_summary()                      # 基础状态：缓存作废 → 这一拍会真查
    win._tick()
    qapp.processEvents()
    assert counters["summary"] >= 1                # 正常状态：会查
    before = dict(counters)                        # 之后只看**增量**（不受定时器影响）

    state.begin_download()
    try:
        for _ in range(3):
            # 每次都把"缓存时间"往前推，模拟"早就过了 30 秒"：
            # 这样"下载期间不查库"这条只能靠下载判断成立，
            # 而不是被 TTL 顺手挡住（否则去掉下载判断用例也不会红）
            win._summary_at -= (ui_app.SUMMARY_TTL + 1)
            win._tick()
            qapp.processEvents()
        assert counters["summary"] == before["summary"]   # 一次都没查（下载优先用缓存）
        assert counters["pool"] == before["pool"]         # 表格也没重建
        assert "正在下载" in win.status_label.fullText() or win.status_label.fullText()
    finally:
        state.end_download()

    win._tick()                                     # 下载结束后的第一拍：完整刷一遍
    qapp.processEvents()
    assert counters["summary"] > before["summary"]     # 又去查了（拿到新数字）
    assert counters["pool"] > before["pool"]           # 表格也补齐了


def test_summary_cache_ttl_and_invalidation(window, qapp, monkeypatch) -> None:
    """数据概况的 30 秒 TTL：期间复用缓存；下载期间即使过期也用缓存；显式作废立刻重取。"""
    from laoa_trader import state
    from laoa_trader.ui import app as ui_app

    win = window
    calls = {"n": 0}
    real = win.engine.summary
    monkeypatch.setattr(win.engine, "summary",
                        lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1])

    win._invalidate_summary()
    first = win._summary_cached()
    assert calls["n"] == 1
    assert win._summary_cached() is first            # 30 秒内：直接给缓存
    assert calls["n"] == 1

    win._summary_at -= (ui_app.SUMMARY_TTL + 1)      # 模拟过了 30 秒
    win._summary_cached()
    assert calls["n"] == 2                           # 过期了 → 重取

    state.begin_download()
    try:
        win._summary_at -= (ui_app.SUMMARY_TTL + 1)
        win._summary_cached()
        assert calls["n"] == 2                       # **下载期间不查**（导入正占着表）
    finally:
        state.end_download()

    win._invalidate_summary()                        # 用户刚加了自选/持仓
    win._summary_cached()
    assert calls["n"] == 3                           # 立刻重取，数字不会滞后 30 秒


def test_yield_gui_keeps_the_main_thread_responsive(qapp) -> None:
    """**让出 GIL 的机制验证**：`_yield_gui()` 之后主线程的定时器照样跳。

    做法：后台线程跑一段"单条 C 级调用占着 GIL"的活（`sum(range(N))` 不会在中间释放
    GIL，正是 pandas/pyarrow 那类长 C 调用的行为）；主线程 `processEvents` +
    QTimer 计时。让出的版本定时器照常触发，不让出的版本被饿死 ——
    这条用例能真的红/绿（去掉让出就会红）。
    """
    import threading

    from PySide6.QtCore import QTimer

    from laoa_trader.data import sync

    def count_ticks(with_yield: bool) -> int:
        ticks = {"n": 0}
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        timer.start()
        done = {"flag": False}

        def busy() -> None:
            for _ in range(12):
                sum(range(6_000_000))       # 单条 C 级调用，期间**不释放 GIL**
                if with_yield:
                    sync._yield_gui()       # 让出 1ms → 主线程能跑
            done["flag"] = True

        thread = threading.Thread(target=busy, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not done["flag"] and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.001)
        thread.join(10)
        timer.stop()
        return ticks["n"]

    starved = count_ticks(with_yield=False)
    alive = count_ticks(with_yield=True)
    assert alive >= 5, f"让出之后主线程仍只跳了 {alive} 次"
    assert alive > starved, f"让出版的定时器次数（{alive}）没有优于不让出的（{starved}）"


# ── 需求 1/2/3 的界面部分：提醒标的文案、涨停原因、竞价那一行 ──


def test_pool_source_column_shows_primary_strategy_and_tooltip_the_rest(
    pool_window, qapp, monkeypatch
) -> None:
    """「来源」列只写得下**主策略**，其余策略 + 组别 + 证据标记全在行 tooltip 里。

    「来源」列只有一格的宽度，所以"这一行到底被哪几条策略选中"只能进 tooltip ——
    工具提示里少一条，用户就少一个"要不要照这个信号动手"的依据。
    """
    from laoa_trader import pool as pool_mod

    rows = [{
        "symbol": "600002", "name": "半导体甲",
        # 两条策略同时选中它：主策略是"短期反转"（建池时按分数定的）
        "strategy": "ReversalStrategy",
        "strategies": "ReversalStrategy,DryUpExpansionStrategy",
        "score": 2.0, "reason": "缩量回踩",
        "label": "短期反转", "source_label": "策略·短期反转",
        "group": "short", "group_label": "短线·T+3", "horizon": 3,
        "industry": "半导体", "note": "", "source": "策略",
        "evidence": "open_only", "evidence_text": "（依赖开盘）",
        "is_limit_up": False, "continue_day_text": "", "limit_up_reason": "",
    }]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    # 来源 = 主策略（这一列的宽度只放得下它 + 组别等明细进 tooltip）
    source_column = ui_app.WATCH_HEADERS.index("来源")
    assert window.pool_table.item(0, source_column).text() == "策略·短期反转"
    tip = window.pool_table.item(0, 0).toolTip()
    assert "来源：策略·短期反转（依赖开盘）" in tip
    assert "组别：短线·T+3（T+3）" in tip
    assert "同批选中：地量后放量变盘" in tip        # 第二条策略没有从界面上消失
    assert "策略照常推送" in tip                    # 证据解释也在
    # 组别那一份不再塞进「来源」列（列里只有"哪条策略"）
    assert "T+3" not in window.pool_table.item(0, source_column).text()


def test_pool_row_tooltip_shows_limit_up_and_auction(pool_window, qapp,
                                                      monkeypatch) -> None:
    """今日涨停（连板 + 原因）与竞价那一行都在**行的 tooltip** 里。

    这两块原来占「股票池」表的一列和卡片上的一行；新表的列数被用户定死成 6 列，
    所以它们的去处是 tooltip —— 信息不能因为去掉一列就丢掉。
    """
    from laoa_trader import pool as pool_mod

    rows = [
        {"symbol": "600002", "name": "半导体甲", "strategy": "LowPriceStrategy",
         "strategies": "LowPriceStrategy", "score": 1.0, "reason": "r",
         "label": "低价股", "source_label": "策略·低价股", "industry": "半导体",
         "note": "", "is_limit_up": True, "continue_day_text": "2 连板",
         "limit_up_reason": "半导体设备+业绩预增"},
        {"symbol": "600003", "name": "白酒样本", "strategy": "X", "strategies": "X",
         "score": 1.0, "reason": "r", "label": "低价股", "source_label": "策略·低价股",
         "industry": "白酒", "note": "", "is_limit_up": False,
         "continue_day_text": "", "limit_up_reason": ""},
    ]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window.auction_snapshot = {
        "600002": {"symbol": "600002", "name": "半导体甲", "pct": 3.2, "volume_ratio": 2.8,
                   "turnover_pct": 0.13, "yesterday_ratio": 0.95, "unmatched": None,
                   "price": 12.5, "pre_close": 12.2},
    }
    window._pool_signature = None          # 强制重画
    window._refresh_pool_table()
    qapp.processEvents()

    by_symbol = {str(window.pool_table.item(i, 0).data(Qt.ItemDataRole.UserRole)): i
                 for i in range(window.pool_table.rowCount())}
    tip = window.pool_table.item(by_symbol["600002"], 0).toolTip()
    assert "涨停：2 连板 · 半导体设备+业绩预增" in tip
    assert "竞价：+3.2% 量比2.8" in tip
    # 不是涨停票 → tooltip 里没有那一行（不留空壳）
    assert "涨停" not in window.pool_table.item(by_symbol["600003"], 0).toolTip()
    # 文案本身的实现仍由 pool 层提供（界面不自己拼一套）
    assert pool_mod.limit_up_text(rows[0]) == "2 连板 · 半导体设备+业绩预增"
    assert pool_mod.limit_up_text(rows[1]) == ""
    # "看全部竞价"的位置：详情弹窗里列**全市场扫描结果**（没有扫描结果时说明去哪儿开）
    details = window._status_details()
    assert "竞价扫描" in details
    assert "设置页" in details


def test_status_points_to_refresh_when_only_light_data_missing(window, qapp) -> None:
    """只缺轻量项（行业归属/日历/指数）时，主状态指路【刷新数据】**而不是**【下载数据】。

    实报 bug：行业归属没同步被指路成"重新下载历史"，用户白等十几分钟重下 180MB。
    """
    from laoa_trader.data import preflight

    win = window
    ready = win.preflight_result
    win.preflight_result = {
        "status": preflight.NEEDS_FULL,
        "needs_download": preflight.DOWNLOAD_SYNC_LIGHT,
        "light_only": True,
        "reason": "行业归属未同步（覆盖率 0%，要求 ≥90%）· 点【刷新数据】补齐即可（不用重下历史行情）",
    }
    win._clear_status_message()
    win._refresh_status()
    qapp.processEvents()
    try:
        text = win.status_label.fullText()
        assert "【刷新数据】" in text
        assert "【下载数据】" not in text
        assert "｜" not in text
        # 详情里也留着同一句原因（用户想核对时看得到）
        assert "【刷新数据】" in win._status_details()
    finally:
        win.preflight_result = ready
        win._refresh_status()
        qapp.processEvents()


# ── 持仓盈亏：只有**比例**，没有金额；持仓页显示「盈亏比例」列 ──


def test_position_table_pnl_colors_and_dash(window, seeded, qapp) -> None:
    """「盈亏比例」：两位数百分比、涨红跌绿、没价的显示 `—`（且不上色）。"""
    from PySide6.QtCore import Qt

    from laoa_trader import market

    table = window.position_table
    assert table.columnCount() == 10
    pnl = ui_app.POSITION_HEADERS.index("盈亏比例")

    # seeded 里 600001：成本 3.0、本地最新收盘 3.174 → +5.80%（两位小数，精确断言）
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()
    cell = table.item(0, pnl)
    assert cell.text() == "+5.80%"
    assert cell.foreground().color().name() == market.COLOR_UP      # 赚 → 红

    # 再加两只：一只亏（成本 20 > 最新 12.696）、一只没有行情（不该显示 0.00%）
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=500,
                                avg_cost=20.0)
        storage.upsert_position(conn, "600009", name="没有行情的票", quantity=100,
                                avg_cost=10.0)
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()

    by_symbol = {_symbols_of(table)[i]: i for i in range(table.rowCount())}
    losing = table.item(by_symbol["600002"], pnl)
    assert losing.text() == "-36.52%"
    assert losing.foreground().color().name() == market.COLOR_DOWN  # 亏 → 绿

    missing = table.item(by_symbol["600009"], pnl)
    assert missing.text() == market.DASH                            # 不是 0.00%
    assert missing.data(Qt.ItemDataRole.ForegroundRole) is None     # 不上色（默认前景）
    # 止损位/止盈位按**成本**算
    cost = float(table.item(by_symbol["600001"], 1).text())
    stop = ui_app.POSITION_HEADERS.index("止损位")
    target = ui_app.POSITION_HEADERS.index("止盈位")
    assert float(table.item(by_symbol["600001"], stop).text()) \
        == pytest.approx(cost * (1 - seeded.stop_loss), abs=0.01)
    assert float(table.item(by_symbol["600001"], target).text()) \
        == pytest.approx(cost * (1 + seeded.take_profit), abs=0.01)


def test_position_pnl_never_shows_an_amount(window, qapp) -> None:
    """**用户明确要求**：持仓不记盈亏金额 —— 详情与所有显示文本里都不许出现「元」。"""
    win = window
    win._refresh_status()
    qapp.processEvents()

    details = win._status_details()
    line = next(ln for ln in details.splitlines() if ln.startswith("持仓浮动："))
    assert line == "持仓浮动：+5.80%"       # seeded：成本 3.0 / 最新 3.174
    assert "元" not in line
    for text in (win._floating_pnl(), win.status_label.fullText()):
        assert "元" not in text
    # 表格里也不许出现金额（只有比例）
    assert "元" not in "".join(_row_cells(win.position_table, 0))

    # 没有持仓时的三种文案也不带金额
    assert win._floating_pnl(("无持仓", 0.0)) == "无持仓"
    assert win._floating_pnl(("无最新价", 0.0)) == "无最新价"
    assert win._floating_pnl(("未知", 0.0)) == "—"


def test_position_pnl_ratio_is_weighted_by_cost(window, seeded, qapp) -> None:
    """组合比例按**成本**加权（不是把各只的比例简单平均）。"""
    with storage.connect(seeded.db_path) as conn:
        # 600001：3.0 → 3.174（+5.80%），成本 3000 元；600002：20 → 12.696（-36.52%），成本 100 元
        storage.upsert_position(conn, "600001", name="低价样本", quantity=1000,
                                avg_cost=3.0)
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=5,
                                avg_cost=20.0)
    window._position_signature = None
    window._refresh_positions()
    state, pct = window._position_pnl()
    assert state == "ok"
    # 加权：(3174 + 63.48 - 3100) / 3100 ≈ +4.43%（简单平均会是 -15.36%）
    assert pct == pytest.approx(4.43, abs=0.05)
    assert f"{pct:+.2f}%" == "+4.43%"


def test_position_table_columns_fit_at_960_logical_width(screen_window, qapp) -> None:
    """≈用户那台（逻辑 960×900 → 窗口 920×760）：**10 列**持仓表铺满、不挤、也不顶大最小宽度。

    这一条是给"窗口能缩到 760 宽"的成果上保险：多一列如果让表格的最小宽度变大，
    窗口就又被顶出屏幕了（那正是用户最初抱怨的"最下边看不见"）。
    2026-09-17 加了两列（市值/换手）之后这条更要紧 —— 所以列数跟着断言成 10。
    """
    win = screen_window(960, 900)
    win.tabs.setCurrentWidget(_tab_page(win, ui_app.TAB_POSITION))
    win._position_signature = None
    win._refresh_positions()
    qapp.processEvents()

    table = win.position_table
    assert table.columnCount() == 10
    widths = [table.columnWidth(i) for i in range(10)]
    assert all(width > 20 for width in widths), widths          # 每列都还看得清
    assert sum(widths) <= table.viewport().width() + 8          # Stretch：正好铺满
    assert table.horizontalScrollBar().isVisible() is False     # 不需要横向滚动
    assert win.minimumSizeHint().width() <= 960                 # 没把整窗最小宽度顶大
    assert _bottom_of(table, win) <= win.height()               # 表格没被窗口下沿切掉


# ── 持仓表的「提醒」列（做T 的**近似**提示也走这一列）──


def test_position_alert_column_shows_t_hint_with_full_tooltip(window, seeded, qapp) -> None:
    """「监控开关」列：`开启`/`关闭` + **今天最新一条提醒的整句话**在 tooltip 里。

    做T提示原来单独占一列（「今日T提示」），上一版并进「提醒」列；
    2026-09-17 用户要求把这一列改成监控开关，做T 那句话**照样一点没丢** ——
    它的短标签（`T高抛`）与整句话都在这一格的 tooltip 里。
    """
    today = now_shanghai().strftime("%Y-%m-%d")
    with storage.connect(seeded.db_path) as conn:
        # 第二只持仓：**没有**任何提醒（钉"空着要画 `—` 而不是空白"）
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=500, avg_cost=20.0)
        storage.record_alerts(conn, [
            {"symbol": "600001", "kind": "t_high", "price": 3.30,
             "detail": "现价 3.30（+2.6%），较今日最高 3.42 回落 3.5%，仍在均价 3.20 上方 → "
                       "可卖出【部分昨仓】（反T：先卖后买），回落后当天再买回等量；"
                       "只提示部分仓位，可卖数量以券商为准"},
        ], today)
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()

    table = window.position_table
    rows = {_symbols_of(table)[i]: i for i in range(table.rowCount())}
    cell = table.item(rows["600001"], ui_app.POSITION_MONITOR_COLUMN)
    assert cell.text() == ui_app.MONITOR_ON_TEXT        # 这一格现在是监控开关
    assert "T高抛" in cell.toolTip()                     # 原来的短标签（提醒列的文字）
    assert "反T：先卖后买" in cell.toolTip()              # 整句话在 tooltip 里
    assert "可卖数量以券商为准" in cell.toolTip()

    empty = table.item(rows["600002"], ui_app.POSITION_MONITOR_COLUMN)
    assert empty.text() == ui_app.MONITOR_ON_TEXT        # 没有提醒不代表没在监控
    assert "今天还没有这只票的盘中提醒" in empty.toolTip()


# ── 「关于」对话框（版本 / 版权 / 数据来源）──


def test_window_title_is_just_the_app_name(window, qapp) -> None:
    """标题栏**只剩软件名**（用户给定的布局：标题区 = 软件名 + 运行状态 + 两个按钮）。

    版本/版权不是消失了，而是从标题栏（1366 宽 + 125% 缩放下只剩省略号）挪到
    【关于软件】与【显示详情】两个正经去处 —— 两条都要真的能看到版本号。
    """
    import laoa_trader

    assert window.windowTitle() == ui_app.APP_NAME == "老A选股助手"
    assert "v" not in window.windowTitle()                 # 不再挂版本号
    assert window.app_title_label.text() == ui_app.APP_NAME
    assert window.btn_about.text() == ui_app.BTN_ABOUT_TEXT == "关于软件"
    # 【关于软件】里版本/版权齐全
    window.on_about()
    qapp.processEvents()
    blob = "\n".join(lb.text() for lb in window.about_dialog.findChildren(
        type(window.market_title)))
    assert f"版本：{laoa_trader.__version__}（测试版）" in blob
    assert "版权所有" in blob
    window.about_dialog.close()
    # 【显示详情】里也有版本号（报障时要贴的就是那段）
    assert f"v{laoa_trader.__version__}" in window.status_details_cache

def test_about_dialog_shows_version_and_copyright(window, qapp) -> None:
    """【关于】弹窗要能看到版本、作者、版权与数据来源（文字用 QLabel，不是链接）。"""
    import laoa_trader
    from PySide6.QtWidgets import QLabel, QPushButton

    window.on_about()
    qapp.processEvents()

    dialog = window.about_dialog
    assert dialog is not None
    assert dialog.isVisible() is True
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    blob = "\n".join(texts)
    assert "老A选股助手" in blob
    assert f"版本：{laoa_trader.__version__}（测试版）" in blob
    assert "作者 / 版权所有人：async-chen" in blob
    assert "版权所有 © 2026 async-chen，保留所有权利。" in blob
    # 数据来源那一行现在把**公开源写在前面**（2026-09-17：公开源是主源、同花顺是备用），
    # 并点明"非交易所授权行情" —— 这句是用户判断"这数据能不能当真"的依据
    assert "公开行情接口（腾讯/新浪/东财）" in blob
    assert "非交易所授权行情" in blob
    assert "准实时快照" in blob            # 新文案还点明了数据是"准实时"，不是实时授权行情
    assert "同花顺" in blob                # 备用/增强源也要提（有 Key 时它才接管）
    assert "不构成任何投资建议" in blob
    assert all("<a href" not in t for t in texts)          # 版本号不是富文本链接

    buttons = [b.text() for b in dialog.findChildren(QPushButton)]
    assert "复制版本信息" in buttons
    assert "关闭" in buttons


def test_about_copy_version_info_to_clipboard(window, qapp) -> None:
    """【复制版本信息】把 名称 / 版本 / 版权 三行写进剪贴板（报障时直接粘贴）。"""
    import laoa_trader
    from PySide6.QtWidgets import QApplication

    QApplication.clipboard().setText("")
    window.on_about()
    qapp.processEvents()
    window.about_copy_button.click()
    qapp.processEvents()

    lines = QApplication.clipboard().text().splitlines()
    assert lines == [
        "老A选股助手",
        f"版本：{laoa_trader.__version__}（测试版）",
        "版权所有 © 2026 async-chen，保留所有权利。",
    ]
    assert "已复制版本信息" in window.status_label.fullText()


def test_about_close_button_closes_dialog(window, qapp) -> None:
    from PySide6.QtWidgets import QPushButton

    window.on_about()
    qapp.processEvents()
    assert window.about_dialog.isVisible() is True

    close_btn = next(b for b in window.about_dialog.findChildren(QPushButton)
                     if b.text() == "关闭")
    close_btn.click()
    qapp.processEvents()
    assert window.about_dialog.isVisible() is False


# ── 图标接线（窗口 / 托盘 / 关于页）──


def test_window_icon_comes_from_assets(window, qapp) -> None:
    """窗口图标是随包那张 256 图（不是空图标，也不是系统默认图标）。

    先清掉应用级图标：`QWidget.windowIcon()` 在窗口没设图标时会退回**应用**图标，
    不清掉的话这条断言就分不清"窗口自己设上了"和"蹭了应用图标"。
    """
    from PySide6.QtGui import QIcon

    qapp.setWindowIcon(QIcon())
    assert window.windowIcon().isNull() is False
    assert [size.width() for size in window.windowIcon().availableSizes()] == [256]


def test_tray_icon_is_the_dedicated_small_png(window) -> None:
    """托盘是 16~32px 的场景：必须取专门画的 32 那份（拿 256 缩下去会糊）。"""
    assert window.tray.icon().isNull() is False
    assert [size.width() for size in window.tray.icon().availableSizes()] == [32]


def test_application_icon_is_set(qapp) -> None:
    """QApplication 也要设一份：不设的话 Windows 任务栏有时会显示 python 的默认图标。"""
    from PySide6.QtGui import QIcon

    from laoa_trader.ui import app as ui_app

    qapp.setWindowIcon(QIcon())
    ui_app._set_app_icon(qapp)
    assert qapp.windowIcon().isNull() is False
    assert [size.width() for size in qapp.windowIcon().availableSizes()] == [256]


def test_about_dialog_shows_icon(window, qapp) -> None:
    """「关于」里程序名下面有 64×64 图标（按 objectName 也找得到，方便报障截图对位置）。"""
    from PySide6.QtWidgets import QLabel

    window.on_about()
    qapp.processEvents()

    assert window.about_icon is not None
    assert window.about_icon.objectName() == "aboutIcon"
    assert window.about_dialog.findChild(QLabel, "aboutIcon") is window.about_icon
    pixmap = window.about_icon.pixmap()
    assert pixmap.isNull() is False
    assert (pixmap.width(), pixmap.height()) == (64, 64)


def test_window_opens_without_icon_assets(seeded, qapp, monkeypatch) -> None:
    """图标丢了也不能崩程序：`icon_png` 返回 None 时窗口照开、关于照弹、托盘照有图标。"""
    from PySide6.QtWidgets import QLabel

    from laoa_trader import assets
    from laoa_trader.ui import app as ui_app

    monkeypatch.setattr(assets, "icon_png", lambda size=None: None)
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert win.tray.icon().isNull() is False      # 退回系统标准图标，托盘不至于空着
        win.on_about()
        qapp.processEvents()
        assert win.about_dialog is not None
        assert win.about_icon is None                 # 没有图标就**不加**那个 QLabel，不留空位
        texts = [lb.text() for lb in win.about_dialog.findChildren(QLabel)]
        assert any(t.startswith("版本：") for t in texts)   # 正文不受图标影响
    finally:
        if win.about_dialog is not None:
            win.about_dialog.close()
        win.scheduler.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


def test_save_group_selection_writes_config_keeps_comments(window, seeded, qapp) -> None:
    """写回策略组：只跑 swing（默认是只勾 short）→ 写回 config.toml，注释与未知键不能丢。

    界面上没有这组勾选框了（按规格移到「策略选股」的列表里），但**写回能力**还在：
    `save_group_selection()` 就是那个入口（列表那边勾完调的也是它）。
    """
    assert window.save_group_selection(["swing"], []) is True
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'enabled_groups = ["swing"]' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert "已写入 config.toml" in window.status_label.fullText()
    # 内存里的配置同步更新
    assert window.cfg.enabled_groups == ["swing"]

def test_save_group_selection_keeps_strategy_choices(window, seeded, qapp) -> None:
    """勾了策略但没勾它的组 → 自动把组也带上（避免"选了却不跑"）。"""
    assert window.save_group_selection(["swing"], ["LowPriceStrategy"]) is True
    qapp.processEvents()
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'enabled_strategies = ["LowPriceStrategy"]' in text
    assert window.cfg.enabled_strategies == ["LowPriceStrategy"]
    # LowPriceStrategy 属于 swing 组：两个键要么都写、要么都不写
    assert window.cfg.enabled_groups == ["swing"]
    assert "swing" in text

def test_save_group_selection_requires_at_least_one_group(window, seeded) -> None:
    """一个组都不给 → **明确拒绝**（不许写出"哪一组都不跑"的配置）。"""
    assert window.save_group_selection([], []) is False
    assert "至少要勾一个策略组" in window.status_label.fullText()
    # 没有写坏配置文件（`seeded` 里写的就是默认策略集：只开 short）
    assert 'enabled_groups = ["short"]' in (
        seeded.data_dir / "config.toml").read_text(encoding="utf-8")

def test_save_notify_writes_channels_and_params(window, seeded, qapp) -> None:
    """通知那一组的保存：四个勾选框 + 声音/闪烁/浮窗参数一次写回。"""
    for name, box in window.channel_boxes.items():
        box.setChecked(name in ("windows", "tray"))
    window.popup_box.setChecked(False)
    window.sound_box.setChecked(False)
    window.flash_seconds_box.setValue(12)
    window.popup_seconds_box.setValue(20)
    window.popup_items_box.setValue(8)
    window.win_sound_box.setChecked(False)
    window.tray_duration.setValue(3000)
    window.on_save_notify()
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'notify_channels = ["windows", "tray"]' in text
    assert "notify_popup = false" in text
    assert "notify_sound = false" in text
    assert "notify_flash_seconds = 12" in text
    assert "notify_popup_seconds = 20" in text
    assert "notify_popup_max_items = 8" in text
    assert "notify_windows_sound = false" in text
    assert "notify_tray_duration_ms = 3000" in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert window.cfg.notify_channels == ["windows", "tray"]
    assert window.cfg.notify_popup is False
    assert window.cfg.notify_flash_seconds == 12
    assert window.cfg.notify_popup_seconds == 20
    assert window.cfg.notify_popup_max_items == 8

def test_save_notify_empty_channels_saves_and_says_so(window, seeded, qapp) -> None:
    """一个频道都不勾 = 只入库不推送：允许保存，并在回显里说清楚（不是失败）。"""
    for box in window.channel_boxes.values():
        box.setChecked(False)
    window.on_save_notify()
    qapp.processEvents()
    assert "只入库不推送" in window.status_label.fullText()
    assert "✅ 已保存" in window.status_label.fullText()
    assert 'notify_channels = []' in (seeded.data_dir / "config.toml").read_text(
        encoding="utf-8")

def test_save_notify_feishu_without_credentials_hints(window, seeded, qapp) -> None:
    """勾了飞书但没凭证：保存成功 + 面板上明确提示"会自动跳过飞书"，不弹错误框。"""
    window.channel_boxes["feishu"].setChecked(True)
    window.feishu_app_id.setText("")
    window.feishu_secret.setText("")
    window._refresh_channel_hints()
    assert "未配置凭证" in window.feishu_hint.text()
    window.on_save_notify()
    qapp.processEvents()
    assert "✅ 已保存" in window.status_label.fullText()
    assert "winotify" not in window.status_label.fullText()   # 不是那句平台不支持的提示

# ── 「系统设置」：底部【保存设置】**一键**写回本页所有设置 ──

#: 一键保存应当写回的**全部**键（一个不多、一个不少）。
#: 少一个键 = "改了没生效"（最难查的那类抱怨）；多一个键 = 把不属于本页的东西悄悄改了。
SETTINGS_KEYS: frozenset[str] = frozenset({
    # 1) 数据来源
    #
    # **没有 `hithink_api_key`**（2026-09-17 用户要求）：界面上不提供填 Key 的入口
    # （那一行只剩"主来源 + 申请地址"的说明；2026-09-18 用户把主源换回同花顺，
    # 角色标记随之从"备用源"改回"主来源"），所以一键保存
    # 也不该再写这个键。读取路径一个字没改（config.toml / 环境变量照旧生效）。
    "history_years",
    # 2) 通知方式
    "notify_popup", "notify_channels", "notify_sound", "notify_flash_seconds",
    "notify_popup_seconds", "notify_popup_max_items", "notify_windows_sound",
    "notify_windows_open_url", "notify_tray_duration_ms", "feishu_on",
    "feishu_app_id", "feishu_app_secret", "feishu_chat_id",
    # 3) 竞价扫描
    "intraday_auction", "auction_min_pct", "auction_max_pct", "auction_min_amount",
    "auction_min_volume_ratio", "auction_min_score", "auction_alert_max_items",
    "auction_boards", "auction_scan_at",
    # 4) T策略
    "stop_loss", "take_profit", "intraday_t", "t_high_min_gain_pct",
    "t_high_pullback_pct", "t_low_min_drop_pct", "t_low_rebound_pct",
    # 5) 其他
    "ui_theme", "intraday_anomaly", "watchlist_max", "watchlist_in_pool",
    # 6) 数据来源那一行里**用户自己填的** Key（2026-09-18 起内置同花顺也有输入框了）
    "hithink_api_key",
})


def test_collect_settings_updates_covers_exactly_the_five_groups(window) -> None:
    """收集函数的键集合 = 五组控件的**全部**键（一键保存的"写哪些"就是它决定的）。

    **35 是现在的个数**（2026-09-18：`hithink_api_key` 又回到了这份键集合里 ——
    用户澄清"不要配 KEY"指的是**程序里不许预置自己的 Key**，不是不给填，
    所以内置同花顺那一行重新有了输入框，一键保存也就该把它写回去；
    出厂包里这个值始终是空串，程序从不写死它）。
    在此之上，每个**已启用、需要 Key 且界面上真有输入框**的来源会按注册表给的
    `key_config` 多收一个键 —— 内置同花顺就是靠这条规则被收进来的
    （`test_source_list_key_field_enters_the_one_click_save`
    再用一个注册表测试替身把"将来再加要 token 的来源"那条分支钉住）。
    公开行情源是**免 Key** 的（它那行连输入框都没有），所以一个都不多收。
    """
    from laoa_trader.data import sources as sources_mod

    extra_key_fields = {
        state["key_config"] for state in sources_mod.source_states(window.cfg)
        if state["enabled"] and state["needs_key"] and state["key_config"]
        # 内置同花顺也会被收（它那行现在有输入框），所以这里用替身来源单独验这条分支，
        # 而"界面上有没有这个框"正是"要不要收这个键"的判据
        and state["id"] != ui_app.BUILTIN_SOURCE
    } - SETTINGS_KEYS
    updates = window._collect_settings_updates()
    assert set(updates) == SETTINGS_KEYS | extra_key_fields
    # 当前：35 个固定键（2026-09-18 起含 `hithink_api_key` —— 内置同花顺又有输入框了）
    assert len(updates) == 35
    # 2026-09-18 起**必须收**它：内置同花顺那一行有输入框，一键保存就该把它写回去
    # （出厂值是空串，程序从不预置；"填了没保存"才是要防的那件事）
    assert "hithink_api_key" in updates
    assert "_bad_scan_at" not in updates           # 内部提示字段不许进配置文件


def _change_every_settings_control(window) -> None:
    """把设置页**每一个**控件都改一遍（一键保存的验收要用；不留一个"没被覆盖"的键）。

    2026-09-17：这一组里**没有** Key 输入框了（同花顺那一行只有"主来源 + 申请地址"
    的说明；2026-09-18 用户把主源换回同花顺，角色标记随之改回"主来源"），所以这里也不改它 ——
    `_collect_settings_updates()` 的键集合里同样没有 `hithink_api_key`。
    """
    # 1) 数据来源
    window.history_years_box.setValue(1.5)
    # 2) 通知方式（四个勾选框 + 参数）
    window.popup_box.setChecked(False)
    for name, box in window.channel_boxes.items():
        box.setChecked(name == "feishu")
    window.sound_box.setChecked(False)
    window.flash_seconds_box.setValue(9)
    window.popup_seconds_box.setValue(12)
    window.popup_items_box.setValue(3)
    window.win_sound_box.setChecked(False)
    window.win_url_box.setChecked(False)
    window.tray_duration.setValue(4000)
    window.feishu_on_box.setChecked(True)
    window.feishu_app_id.setText("cli_test")
    window.feishu_secret.setText("secret_test")
    window.feishu_chat_id.setText("oc_test")
    # 3) 竞价扫描
    window.auction_on_box.setChecked(True)
    window.auction_min_pct_box.setValue(3.0)
    window.auction_max_pct_box.setValue(8.0)
    window.auction_amount_box.setValue(250.0)
    window.auction_ratio_box.setValue(2.5)
    window.auction_score_box.setValue(3)
    window.auction_items_box.setValue(7)
    for key, box in window.auction_board_boxes.items():
        box.setChecked(key in ("main", "star"))
    window.auction_scan_at_edit.setText("09:21, 09:24")
    # 4) T策略（开关 + 四个做T阈值 + 止损/止盈）
    window.stop_loss_box.setValue(6.0)
    window.take_profit_box.setValue(12.0)
    window.intraday_t_box.setChecked(True)
    window.t_high_min_gain_box.setValue(3.0)
    window.t_high_pullback_box.setValue(2.0)
    window.t_low_min_drop_box.setValue(2.5)
    window.t_low_rebound_box.setValue(1.5)
    # 5) 其他
    window.theme_box.setCurrentIndex(window.theme_box.findData("system"))
    window.anomaly_box.setChecked(True)
    window.watchlist_max_box.setValue(30)
    window.watchlist_in_pool_box.setChecked(False)


def test_save_settings_button_writes_every_control_in_one_click(window, seeded, qapp) -> None:
    """**一键保存的验收**：改遍每个控件 → 点一次【保存设置】→ 逐键断言落盘 + 回显。

    同时验证"保留用户自己的注释与未知键"（`config.update_config_file` 是**就地改写**，
    不是重写整个文件）—— 这是这一页敢一次写回 30 多个键的前提。
    """
    _change_every_settings_control(window)
    qapp.processEvents()
    config_file = seeded.data_dir / "config.toml"
    before_key = window.cfg.hithink_api_key       # 测试配置里是 "test-key"

    window.save_settings_button.click()          # ← **一次点击**
    qapp.processEvents()
    text = config_file.read_text(encoding="utf-8")

    for line in (
        # `hithink_api_key` **不在**这一批里（界面上没有它的入口了，见 SETTINGS_KEYS 的说明）
        "history_years = 1.5",
        "notify_popup = false",
        'notify_channels = ["feishu"]',
        "notify_sound = false",
        "notify_flash_seconds = 9",
        "notify_popup_seconds = 12",
        "notify_popup_max_items = 3",
        "notify_windows_sound = false",
        "notify_windows_open_url = false",
        "notify_tray_duration_ms = 4000",
        "feishu_on = true",
        'feishu_app_id = "cli_test"',
        'feishu_app_secret = "secret_test"',
        'feishu_chat_id = "oc_test"',
        "intraday_auction = true",
        "auction_min_pct = 3.0",
        "auction_max_pct = 8.0",
        "auction_min_amount = 2500000.0",
        "auction_min_volume_ratio = 2.5",
        "auction_min_score = 3",
        "auction_alert_max_items = 7",
        'auction_boards = ["main", "star"]',
        'auction_scan_at = ["09:21", "09:24"]',
        "stop_loss = 0.06",
        "take_profit = 0.12",
        "intraday_t = true",
        "t_high_min_gain_pct = 3.0",
        "t_high_pullback_pct = 2.0",
        "t_low_min_drop_pct = 2.5",
        "t_low_rebound_pct = 1.5",
        'ui_theme = "system"',
        "intraday_anomaly = true",
        "watchlist_max = 30",
        "watchlist_in_pool = false",
    ):
        assert line in text, line
    # 用户自己写的注释与未知键，一个字都不能丢
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    # 回显：写了几项、写到哪个文件、现在生效的是什么
    hint = window.save_settings_hint.text()
    # 项数就是固定键那份集合：内置同花顺那一行的 Key 现在也在里面
    # （用户 2026-09-18 要回了输入口，`hithink_api_key` 属于固定键）
    assert hint.startswith(f"✅ 已保存 {len(SETTINGS_KEYS)} 项（已写入 config.toml）")
    assert "生效：主题 系统默认；" in hint and "T策略 开" in hint
    # 那一行的 Key 现在**会被写回**：值就是输入框里显示的（= 用户 config 里的原值），
    # 所以内容不变、但**写这一下是有的**（"填了没保存"才是要防的那件事）
    assert f'hithink_api_key = "{before_key}"' in text
    assert window.cfg.hithink_api_key == before_key
    # 内存里的配置同步跟上（不用重启）
    assert window.cfg.history_years == 1.5
    assert window.cfg.notify_popup is False
    assert window.cfg.t_low_rebound_pct == 1.5
    assert window.cfg.watchlist_in_pool is False
    assert window.cfg.watchlist_max == 30
    # 主题换到"系统默认"要真的生效（不只是写进文件）
    assert qapp.styleSheet() == ""


def test_save_settings_refuses_and_names_the_offending_item(window, seeded, qapp) -> None:
    """任何一项校验不过 → **明确拒绝 + 点名哪一项**，而且**一个字都不写**。

    用户看到"保存失败"却不知道是哪一项，等于让他自己一项项试 ——
    这正是"不能静默丢弃"要防的那件事。这里把三种典型填错都走一遍。
    """
    config_file = seeded.data_dir / "config.toml"
    before = config_file.read_text(encoding="utf-8")

    # ① 止损填 0（控件允许填 0，由校验拒绝：0 会变成"任何下跌都算止损"）
    window.stop_loss_box.setValue(0.0)
    window.save_settings_button.click()
    qapp.processEvents()
    assert config_file.read_text(encoding="utf-8") == before
    assert window.save_settings_hint.text().startswith("❌ 未保存：")
    assert "止损比例" in window.save_settings_hint.text()
    assert "stop_loss" in window.save_settings_hint.text()

    # ② 四个做T阈值之一填 0（这四个原来连界面入口都没有，必须真的管住）
    window.stop_loss_box.setValue(5.0)
    window.t_low_rebound_box.setValue(0.0)
    window.save_settings_button.click()
    qapp.processEvents()
    assert config_file.read_text(encoding="utf-8") == before
    assert "做T·反弹幅度" in window.save_settings_hint.text()
    assert "t_low_rebound_pct" in window.save_settings_hint.text()

    # ③ 竞价涨幅上下限倒挂
    window.t_low_rebound_box.setValue(1.0)
    window.auction_min_pct_box.setValue(6.0)
    window.auction_max_pct_box.setValue(5.0)
    window.save_settings_button.click()
    qapp.processEvents()
    assert config_file.read_text(encoding="utf-8") == before
    assert "涨幅上限" in window.save_settings_hint.text()

    # 改回合法值 → 这一次真的写进去了（拒绝不是"卡住不动"）
    window.auction_min_pct_box.setValue(2.0)
    window.auction_max_pct_box.setValue(9.0)
    window.save_settings_button.click()
    qapp.processEvents()
    assert window.save_settings_hint.text().startswith("✅ 已保存")
    assert config_file.read_text(encoding="utf-8") != before


def test_formula_page_button_runs_the_pipeline_through_its_signal(
    seeded, qapp, monkeypatch
) -> None:
    """**点页面那个【开始选股】按钮** → 走 `start_pick_requested` → 到 `on_run_pipeline`。

    这条是按契约收尾的验收（B2b 的页面只 emit 信号，流程在主窗口）：
    - 同一页**只有一个**【开始选股】（主窗口不许再自带一个，两个同名按钮没法解释哪个真的跑）；
    - 按钮**点到流程入口只走一条路**：点一次只能跑一轮（信号 + 直连=两条路会跑两轮，
      这条断言就是防那个）；
    - `btn_run` 指向页面那个按钮（托盘菜单/主题/测试都按这个名字找它）。
    """
    from PySide6.QtWidgets import QPushButton

    called: list[int] = []
    monkeypatch.setattr(ui_app.MainWindow, "on_run_pipeline",
                        lambda self: called.append(1))
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        # 这一页里的【开始选股】按钮**只有一个**，而且就是页面自己那个
        formula_tab = _tab_page(win, ui_app.TAB_FORMULA)
        buttons = [b for b in formula_tab.findChildren(QPushButton)
                   if b.text() == "开始选股"]
        assert len(buttons) == 1, [b.text() for b in buttons]
        assert buttons[0] is win.formula_page.btn_start_pick
        assert win.btn_run is buttons[0]

        # **点那个真按钮**（不是直接 emit 信号）：一轮只跑一次
        buttons[0].click()
        qapp.processEvents()
        assert called == [1], called
        # 再点一次 → 又跑一轮（按钮不是"只能点一次"）
        buttons[0].click()
        qapp.processEvents()
        assert called == [1, 1]
        # 信号本身也接到同一个回调（页面内部两条路指向同一处）
        win.formula_page.start_pick_requested.emit()
        qapp.processEvents()
        assert called == [1, 1, 1]
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win._auction_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


def test_test_connection_probe_reports_every_outcome(window) -> None:
    """`probe_data_source()`：真打一次**最小请求**，把结果说成人话。

    为什么这条必须有：这段逻辑的价值全在"它真的去打了接口、而且每种失败都给出
    可执行的说法"。如果它只是弹一句"连接正常"，那就成了假能力 —— 所以这里把
    成功 / 没填 Key / Key 无效 / 服务端未就绪 / 网络异常五种结果逐一钉住。

    2026-09-17：界面上那个【测试连接】**按钮**被删掉了（用户要求把同花顺那一行改成
    "主来源 + 申请地址"，输入框没了按钮就没有被测对象），但**函数保留**：
    它是独立能力，测试直接调它（将来要恢复"测一下配置里的 Key 能不能用"，
    接一个按钮上来就行）。
    """
    from laoa_trader.data import hithink as hx

    class AuthClient:
        def special_pool_total(self, path, day=None):
            raise hx.HithinkAuthError(2002, "token invalid")

    class NotReadyClient:
        def special_pool_total(self, path, day=None):
            raise hx.HithinkNotReadyError(4040, "数据尚未就绪")

    class BoomClient:
        def special_pool_total(self, path, day=None):
            raise OSError("网络不通")

    from laoa_trader import market

    class RealOk:
        def special_pool_total(self, path, day=None):
            assert path == market.LIMIT_UP_PATH          # 就是那个最便宜的最小请求
            return 55

    text = ui_app.probe_data_source(window.cfg, api_key="k", client=RealOk())
    assert text.startswith("✅") and "55" in text
    assert "API Key" in ui_app.probe_data_source(window.cfg, api_key="")
    assert "Key 无效" in ui_app.probe_data_source(window.cfg, api_key="k",
                                                  client=AuthClient())
    assert "尚未就绪" in ui_app.probe_data_source(window.cfg, api_key="k",
                                                  client=NotReadyClient())
    assert "连接失败" in ui_app.probe_data_source(window.cfg, api_key="k",
                                                  client=BoomClient())


def test_test_connection_button_is_wired_and_never_dead(window, qapp) -> None:
    """那一行的【测试连接】**必须在页面上、而且真的接着东西**（点它不炸、有结论）。

    这条原来防的是相反的中间态（按钮还在但点了没反应）—— 2026-09-17 曾把按钮删掉；
    2026-09-18 用户要回输入口，按钮也就回来了。所以现在防的是同一种病的另一面：
    **按钮画出来了、但没有接上处理函数**（点下去什么都不发生，或者 AttributeError）。
    空 Key 时点它必须给一句"框是空的"（不发请求、不崩）。
    """
    from PySide6.QtWidgets import QPushButton

    builtin = window.source_rows[ui_app.BUILTIN_SOURCE]
    assert builtin.btn_test is not None
    page = _tab_page(window, ui_app.TAB_SETTINGS)
    buttons = [b.text() for b in page.findChildren(QPushButton) if "测试连接" in b.text()]
    assert buttons == ["测试连接"], "页面上应当只有一个【测试连接】（在来源行里）"

    # 空 Key → 明确告诉用户"先填"，绝不去发请求
    builtin.key_edit.setText("")
    window.on_test_source_key(ui_app.BUILTIN_SOURCE)
    qapp.processEvents()
    assert "空的" in window.key_hint.text() or "先" in window.key_hint.text()
    # 那一行的说明就是用户给定的那句话，且地址**可点开**
    assert builtin.key_label.openExternalLinks() is True
    assert ui_app.BUILTIN_KEY_URL in builtin.key_label.text()
    assert builtin.key_label.textInteractionFlags() & \
        ui_app.Qt.TextInteractionFlag.TextBrowserInteraction


def test_pick_result_is_no_longer_shown_on_the_formula_page(window, qapp) -> None:
    """选股跑完 → **不再把结果喂进「策略选股」页**（用户 2026-09-17 拍板：那一页只显示策略，
    结果直接进自选股池，另外导出一份桌面文件）。

    为什么这条留着（而不是删掉）：它盯的正是"别把那张结果表加回来"。
    旧版这一页下半挂着「本次选股结果」表 + 一句话结论 +【全部加为自选】，
    用户明确要求删掉；`FormulaPage.show_pick_result()` 保留成**空实现**
    （`app.py` 的 `_on_pipeline_done()` 还在调它），所以这条同时钉住两件事：
    ① 页面上确实没有结果表；② 调那个空方法不炸、也不改变页面。
    """
    report = {
        "data_date": "2026-09-11",
        "picks": 2,
        "pool": [
            {"symbol": "600002", "name": "半导体甲", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy,DryUpExpansionStrategy"},
            # 纯自选行（没有来源策略）→ 本来也不算"本次选股结果"
            {"symbol": "300750", "name": "电池龙头", "strategy": "", "strategies": ""},
        ],
    }
    window._on_pipeline_done("开始选股", report)      # 主窗口仍然调它：不许抛
    qapp.processEvents()

    page = window.formula_page
    # ① 结果表连骨头都不剩（属性查一遍；旧版是 result_box / result_table / result_rows）
    for stale in ("result_rows", "result_table", "result_box", "result_summary",
                  "result_date", "on_add_all_to_watchlist"):
        assert not hasattr(page, stale), stale
    # ② 空实现：收下参数、什么都不做、也不改变页面上的控件数
    from PySide6.QtWidgets import QLabel

    before = len(page.findChildren(QLabel))
    assert page.show_pick_result(report, data_date="2026-09-11") is None
    assert page.show_pick_result([{"symbol": "600002"}]) is None
    qapp.processEvents()
    assert len(page.findChildren(QLabel)) == before
    # 结论照旧进运行状态（用户照样知道"跑完了、选出了几只"）
    text = window.status_label.fullText()
    assert "开始选股完成" in text and "池子 2 只" in text

    # 主窗口是**容错调用**的：另一个模块没提供这个方法时不该让流程报错
    class NoResultPage:
        def reload(self):
            pass

    monkeypatch_page = NoResultPage()
    original = window.formula_page
    window.formula_page = monkeypatch_page
    try:
        window._on_pipeline_done("开始选股", report)     # 不抛异常
    finally:
        window.formula_page = original


def test_alert_texts_use_halfwidth_parens(window) -> None:
    """提醒相关的**所有用户可见文本**都是半角 `名称(代码)`：表格目标列 / 浮窗行 / 详情。

    全项目只有一种写法（`docs/改版方案.md` 第四节）：两处表格的列头早就写的是
    `名称(代码)`，推送正文、浮窗、详情再写全角就等于同一只票在屏幕上长得不一样。
    机器可读的格式（CSV/JSON/库字段）**一个都没动**。
    """
    from laoa_trader.ui import alert_popup as popup_mod

    # 用 seeded 库里**真的有**的代码：`_alert_items` 会去 stock_basic 查名字，
    # 查不到就只能显示代码（那是另一条降级路径，不是这里要测的）
    row = {"symbol": "600002", "name": "半导体甲", "kind": "stop_loss",
           "price": 12.5, "detail": "现价 12.50 ≤ 参考价 12.85 × 0.95",
           "pushed_at": "2026-09-16 10:31:00", "date": "2026-09-16"}
    target = window._alert_target_text(row, {"600002": "半导体甲"})
    assert target == "半导体甲(600002)"
    items = window._alert_items([dict(row)])
    assert items[0]["target"] == "半导体甲(600002)"
    line = popup_mod.row_text(items[0])
    assert line.startswith("半导体甲(600002)")
    detail = window._alert_detail_text(items[0])
    assert "半导体甲(600002)" in detail
    for text in (target, line, detail):
        assert "（600002）" not in text


def test_feishu_hint_shown_when_checked_without_credentials(window) -> None:
    window.feishu_app_id.setText("")
    window.feishu_secret.setText("")
    window.cfg.feishu_app_id = ""
    window.cfg.feishu_app_secret = ""
    window.channel_boxes["feishu"].setChecked(True)
    window._refresh_channel_hints()
    assert "未配置凭证" in window.feishu_hint.text()
    assert "其余频道照常" in window.feishu_hint.text()


def test_test_notify_uses_current_widgets_without_saving(window, qapp, monkeypatch,
                                                         seeded) -> None:
    """【发送测试提醒】用**面板当前勾选**（未保存也生效），且不改配置文件。"""
    seen: dict = {}

    def fake_notify_all(title, lines, kinds=None, cfg=None):
        seen["channels"] = list(cfg.notify_channels)
        seen["sound"] = cfg.notify_windows_sound
        return {"windows": {"kind": "windows", "ok": True, "detail": "弹了"},
                "feishu": {"kind": "feishu", "ok": True, "skipped": True,
                           "detail": "未配置飞书凭证，已跳过"},
                "tray": {"kind": "tray", "ok": True, "detail": "投递"}}

    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify_all)
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")

    # 面板当前勾选 = windows + tray（测试提醒用的是**面板当前值**，不用先保存）
    for name, box in window.channel_boxes.items():
        box.setChecked(name in ("windows", "tray"))
    window.win_sound_box.setChecked(False)
    window.on_test_notify()
    assert window._worker is not None
    window._worker.wait(10_000)
    qapp.processEvents()

    assert seen["channels"] == ["windows", "tray"]
    assert seen["sound"] is False
    assert "测试通知" in window.status_label.fullText()
    assert "飞书已跳过" in window.status_label.fullText()
    # 测试提醒只是"实发一条"，不该顺手改配置
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before


def test_run_pipeline_button_runs_in_background_and_is_idempotent(window, qapp,
                                                                  monkeypatch,
                                                                  seeded) -> None:
    """【立即选股并建池】：后台线程跑完整流程，写 signal/stock_pool，不崩界面。"""
    from laoa_trader import scheduler as sched

    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"kind": "tray", "ok": True}})
    # 这条测的是"按钮 → 后台 → 落库"的接线，不是默认策略集：小样本库（价格缓慢上涨）
    # 只满足「低价股」的条件（它属于默认停用的 swing 组），所以这里显式把那一组打开。
    # 「默认只开 short」本身由 settings 页与 config 的用例钉住。
    seeded.enabled_groups = ["swing"]

    window.on_run_pipeline()
    assert window._worker is not None
    window._worker.wait(60_000)
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        signals = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_rows = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
    assert signals > 0 and pool_rows > 0
    assert "立即选股并建池完成" in window.status_label.fullText()
    assert "池子" in window.status_label.fullText()

    # 第二次点：幂等（行数不变），且不重复推送
    window.on_run_pipeline()
    window._worker.wait(60_000)
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == signals
        assert conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == pool_rows
    assert "未重复推送" in window.status_label.fullText()


def test_refresh_data_button_only_syncs(window, qapp, monkeypatch, seeded) -> None:
    """【只刷新数据】：跑同步、不选股、不推送；同步失败也要有中文结论。"""
    from laoa_trader import scheduler as sched

    calls: list[str] = []
    monkeypatch.setattr(sched.sync, "daily_update",
                        lambda *a, **k: calls.append("sync") or [
                            sched.sync.SyncResult(stage="日更数据", ok=True, rows=3,
                                                  detail="写入 3 行")])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: pytest.fail("只刷新数据不该推送"))

    window.on_refresh_data()
    assert window._worker is not None
    window._worker.wait(30_000)
    qapp.processEvents()
    assert calls == ["sync"]
    assert "刷新数据完成" in window.status_label.fullText()
    assert "写入 3 行" in window.status_label.fullText()


def test_refresh_data_failure_shows_reason_without_crash(window, qapp, monkeypatch) -> None:
    from laoa_trader import scheduler as sched

    monkeypatch.setattr(sched.sync, "daily_update",
                        lambda *a, **k: [sched.sync.SyncResult(stage="日更数据", ok=False,
                                                              error="模拟断网（测试假客户端）")])
    window.on_refresh_data()
    window._worker.wait(30_000)
    qapp.processEvents()
    assert "刷新数据完成" in window.status_label.fullText()
    assert "模拟断网" in window.status_label.fullText()


# ── 自选股（添加 / 启停 / 删除）——表格就是「自选股池」那一张 ──


def test_watchlist_panel_starts_empty(window) -> None:
    """没加过自选时表格是空的（seeded 里只有一只策略标的 —— 它在表里，但不来自自选）。"""
    assert window.pool_table.rowCount() == 1
    assert window.pool_count_label.text() == "共 1 只（策略 1 · 自选 0）"


def test_watch_panel_add_autofills_name_and_notes(window, seeded, qapp) -> None:
    """加自选：名称从本地库自动补，备注写进去，表格立刻出现（来源标「自选」）。"""
    window.watch_symbol.setText("600001")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        rows = storage.load_watchlist(conn)
    assert rows[0]["symbol"] == "600001"
    assert rows[0]["name"] == "低价样本"        # 自动补的名字
    assert rows[0]["note"] == "龙头"
    row = _symbols_of(window.pool_table).index("600001")
    assert window.pool_table.item(row, 0).text() == "低价样本(600001)"
    assert "备注：龙头" in window.pool_table.item(row, 0).toolTip()
    assert window.pool_table.item(row, ui_app.WATCH_HEADERS.index("来源")).text() == "自选"
    # 加进来的自选默认在监控中 → 「监控开关」列是 `开启`
    assert window.pool_table.item(row, ui_app.WATCH_MONITOR_COLUMN).text() \
        == ui_app.MONITOR_ON_TEXT
    assert "已加自选：600001 低价样本" in window.status_label.fullText()


def test_watch_panel_add_unknown_symbol_warns_but_adds(window, seeded, qapp) -> None:
    window.watch_symbol.setText("601999")
    window.on_watch_add()
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["601999"]
    assert "本地库没有它的名称" in window.status_label.fullText()


def test_watch_panel_rejects_bad_code(window, seeded) -> None:
    window.watch_symbol.setText("abc")
    window.on_watch_add()
    assert "6 位数字" in window.status_label.fullText()
    with storage.connect(seeded.db_path) as conn:
        assert storage.load_watchlist(conn) == []


def test_watch_panel_toggle_and_remove(window, seeded, qapp) -> None:
    window.watch_symbol.setText("600001")
    window.watch_note.setText("老朋友")
    window.on_watch_add()
    qapp.processEvents()

    window.on_watch_toggle(False)
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_symbols(conn) == []           # 停了就不启用
        assert len(storage.load_watchlist(conn)) == 1           # 但还在列表里
    row = _symbols_of(window.pool_table).index("600001")
    assert window.pool_table.item(
        row, ui_app.WATCH_HEADERS.index("来源")).text() == "自选（已停用）"
    assert window.pool_table.item(
        row, ui_app.WATCH_MONITOR_COLUMN).text() == ui_app.MONITOR_OFF_TEXT
    assert "不进池、不监控" in window.status_label.fullText()

    window.on_watch_toggle(True)
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["600001"]

    window.on_watch_remove("600001")
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.load_watchlist(conn) == []
    assert "600001" not in _symbols_of(window.pool_table)


def test_pool_table_shows_watchlist_source_and_note(window, seeded, qapp) -> None:
    """自选股进池后：来源能区分「自选」「策略+自选」，备注在整行的 tooltip 里。"""
    from laoa_trader import pool as pool_mod

    # 600002 是策略选中的（seed 的池子），把它也加为自选 → 来源「…+ 自选」
    window.watch_symbol.setText("600002")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("来源")).text() \
        == "策略·低价股+自选"
    assert "备注：龙头" in window.pool_table.item(0, 0).toolTip()

    # 纯自选（不在策略候选里）→ 来源「自选」，而且**不用等建池**就能进这张表
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600100", name="冷门样本", note="消息面")
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = {_symbols_of(window.pool_table)[i]: i
            for i in range(window.pool_table.rowCount())}
    assert "600100" in rows
    assert window.pool_table.item(
        rows["600100"], ui_app.WATCH_HEADERS.index("来源")).text() == "自选"
    assert "备注：消息面" in window.pool_table.item(rows["600100"], 0).toolTip()

    # 建池之后它仍然只出现一次（不重复）
    pool_mod.build_pool(DataEngine(seeded.db_path), window.cfg, hot_only=False,
                        save=True, day="2026-09-11")
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    assert _symbols_of(window.pool_table).count("600100") == 1


def test_pool_table_signature_includes_note(window, seeded) -> None:
    """备注变了表格要重画（否则用户改完备注界面不刷新）。"""
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600002", name="半导体甲", note="备注一")
    window._pool_signature = None
    window._refresh_pool_table()
    assert "备注：备注一" in window.pool_table.item(0, 0).toolTip()

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600002", name="半导体甲", note="备注二")
    window._refresh_pool_table()          # 签名里带备注 → 必须重画
    assert "备注：备注二" in window.pool_table.item(0, 0).toolTip()

def test_pool_count_label_and_details_show_watchlist_count(window, seeded, qapp) -> None:
    """自选只数：**表头那一行小字**负责（`共 N 只（策略 M · 自选 K）`），详情里也有。"""
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
    window._pool_signature = None
    window._tick()
    # 数据概况在界面层有 30 秒 TTL 缓存（下载时不再每 5 秒全表 COUNT 一遍）：
    # 这个用例是**直接写库**的，所以显式要求取新值一次
    window._refresh_status(force_summary=True)
    qapp.processEvents()
    assert window.pool_count_label.text() == "共 2 只（策略 1 · 自选 1）"
    # 细节（"含自选几只"）在【显示详情】里
    assert "含自选 1 只" in window._status_details()


# ── 启动自检：数据不够用时**只在标题区写一行提示**（不再弹向导）──


@pytest.fixture()
def hint_window(cfg, qapp, monkeypatch):
    """一个**空库**的主窗口：启动自检必然判 needs_full → 标题区出现那一行提示。"""
    from laoa_trader.ui import app as ui_app

    cfg.min_history_years = 9          # 空库无论如何都不可用
    # 「数据来源」里填 Key 会写回配置文件；给一个真实的临时路径，
    # 免得落到 ~/.config（沙箱里可能不可写、也不是本用例要测的东西）
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(f'data_dir = "{p(cfg.data_dir)}"', encoding="utf-8")
    cfg.source_path = config_file
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()
    win.run_preflight()                # 显式触发（不依赖 singleShot 时序）
    qapp.processEvents()
    yield win
    win._timer.stop()
    win._market_timer.stop()
    win._auction_timer.stop()
    win.scheduler.stop()
    win.quotes.stop()
    win.tray.hide()
    win.deleteLater()
    qapp.processEvents()


def test_first_run_hint_replaces_the_wizard_dialog(hint_window, qapp) -> None:
    """空库启动 → 标题区**一行提示**（指路到「系统设置」），而不是弹一个向导窗口。"""
    from laoa_trader.data import preflight
    from PySide6.QtWidgets import QDialog

    win = hint_window
    assert win.preflight_result["status"] == preflight.NEEDS_FULL
    assert win.first_run_hint.isVisible() is True
    text = win.first_run_hint.fullText()
    assert "本地还没有行情数据" in text
    assert "系统设置" in text                       # 指路要指到**真的那一页**
    assert "下载数据" in text                       # 与按钮文字一字不差
    # 向导那一堆控件与属性都不在了（用户明确要求"不再弹模态向导"）
    assert not hasattr(win, "wizard")
    assert not hasattr(win, "wizard_start")
    # 而且没有**任何**新开的对话框（这一条要真的看窗口，不能只看属性）
    assert [w for w in qapp.topLevelWidgets()
            if isinstance(w, QDialog) and w.isVisible()] == []
    # 提示是**一行**（`ElidedLabel`：太长的原因省略掉，不换行顶高标题区）
    assert "\n" not in win.first_run_hint.text()
    assert "正在检查本地数据" not in win.status_label.fullText()   # 已给出结论


def test_first_run_hint_points_to_incremental_refresh(hint_window, qapp) -> None:
    """只缺轻量项（行业归属/日历/指数）时，提示指路【刷新数据】**而不是**【下载数据】。

    实报 bug：行业归属没同步被指路成"重新下载历史"，用户白等十几分钟重下 180MB。
    """
    from laoa_trader.data import preflight

    win = hint_window
    win.show_first_run_wizard({
        "status": preflight.NEEDS_FULL,
        "needs_download": preflight.DOWNLOAD_SYNC_LIGHT,
        "light_only": True,
        "reason": "行业归属未同步",
    })
    text = win.first_run_hint.fullText()
    assert "刷新数据" in text
    assert "下载数据" not in text
    assert "系统设置" in text

    # 落后几个交易日 → 也是"刷新"那条路
    win.show_first_run_wizard({"status": preflight.NEEDS_INCREMENTAL,
                               "stale_trading_days": 3})
    assert "落后 3 个交易日" in win.first_run_hint.fullText()
    assert "刷新数据" in win.first_run_hint.fullText()


def test_no_first_run_hint_when_data_ready(seeded, qapp) -> None:
    """数据就绪时：不显示那一行提示，运行状态打结论。"""
    from laoa_trader.data import preflight
    from laoa_trader.ui import app as ui_app

    seeded.min_history_years = 0.0
    seeded.min_symbols = 1
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    win.run_preflight()
    qapp.processEvents()
    assert win.preflight_result["status"] == preflight.READY
    assert win.first_run_hint.isVisible() is False
    # 运行状态那句就是"数据就绪"（不再把 summary_line 的"…行 / 最新 …"塞进去）
    assert win.status_label.fullText().startswith("✅ 数据就绪 · ")
    assert "行" not in win.status_label.fullText()
    win._timer.stop()
    win._market_timer.stop()
    win._auction_timer.stop()
    win.scheduler.stop()
    win.quotes.stop()
    win.tray.hide()
    win.deleteLater()
    qapp.processEvents()


def test_download_without_api_key_shows_inline_hint_not_dialog(window, qapp,
                                                              monkeypatch) -> None:
    """没配 Key 就点【下载数据】：**拒绝 + 不弹窗**，在「系统设置」里点亮提示 + 指路。

    为什么不做成弹窗：缺 Key 不是"要用户拍板"的事，而是"下一步该做什么" ——
    弹一个框只会拦住他，然后他还是得回同一个地方改配置。

    2026-09-18：填 Key 的入口就在**那一行的输入框**上（同花顺是主源、标记"主来源"），
    所以这句指路**先指输入框**，再说 `config.toml` / 环境变量那两条老路（三条都有效）。
    （中间有过一段"界面上没有输入框"的版本，已被用户否掉。）

    2026-09-18 追加：为什么这条用例还要断言"提示里写了没 Key 时**能用**什么" ——
    用户拍板"完整历史走用户自己的同花顺 Key、公开源退回兜底"之后，曾经试过"没 Key 时
    自动装随包历史包"，那个方案也被否掉了（`data/history_pack.py` 已删除）。于是
    "点了没反应"和"提示只说缺什么、不说还能干什么"都会让用户以为整个程序不可用 ——
    必须同时说清"能看行情与大盘概览"和"选股要 Key"，这两句是这条路径的全部信息量。
    """
    from PySide6.QtWidgets import QMessageBox

    win = window
    win.cfg.hithink_api_key = ""
    popped: list[str] = []
    # 万一还有谁把它弹出来，这条用例要**红**（不是静默通过）
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: popped.append("warning"))
    # 「系统设置」页不是当前页时，里面的控件 isVisible 恒为 False —— 先切过去
    win.tabs.setCurrentWidget(win.settings_page)
    qapp.processEvents()
    win.on_download()
    qapp.processEvents()

    assert popped == []
    assert win._worker is None                                  # 没起下载任务
    assert win.key_hint.isVisible() is True
    assert "API Key" in win.key_hint.text()
    # 指路指的是**配置文件**（界面上有输入框了）
    assert "config.toml" in win.key_hint.text()
    assert "hithink_api_key" in win.key_hint.text()
    assert "HITHINK_FINANCE_API_KEY" in win.key_hint.text()      # 环境变量那条路也写出来
    # **说清"没 Key 时能干什么、不能干什么"**（2026-09-18 用户拍板：历史走 Key、
    # 公开源退回兜底）——只说"缺 Key"会让人以为整个程序用不了
    assert "大盘概览" in win.key_hint.text()                     # 免 Key 兜底仍然可看
    assert "行情" in win.key_hint.text()
    assert "选股要 Key" in win.key_hint.text()                   # 但选股确实要它
    assert "自动" in win.key_hint.text()                         # 当天的行情仍会自动落库
    assert "系统设置" in win.status_label.fullText()
    # 在 config.toml 里写好 Key（老用户的做法）→ 提示收掉，再点就能下载
    config_file = win.cfg.source_path
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(
            'hithink_api_key = ""', 'hithink_api_key = "real-key"'),
        encoding="utf-8",
    )
    win.cfg.hithink_api_key = "real-key"
    win._run_worker = lambda *a, **k: None          # 只验"放行"，不真起下载线程
    win.on_download()
    qapp.processEvents()
    assert win.key_hint.isVisible() is False
    assert 'hithink_api_key = "real-key"' in config_file.read_text(encoding="utf-8")


# ── 「大盘概览」页（第一个页签）──


def _market_fake(**kwargs):
    """概览用假客户端：概览层已经单独测过，界面这边只关心"接线对了没有"。"""
    from laoa_trader import market
    from tests.test_market import FakeMarketClient, SAMPLE_ROWS

    kwargs.setdefault("totals", {
        market.LIMIT_UP_PATH: 55,
        market.LIMIT_DOWN_PATH: 16,
        market.LIMIT_BREAK_PATH: 30,
    })
    kwargs.setdefault("rows", SAMPLE_ROWS)
    kwargs.setdefault("pages", _market_pages())
    return FakeMarketClient(**kwargs)


def _market_pages() -> list[dict]:
    """一页全市场快照：一行代表一类（涨 / 跌 / 平盘 + 一只北交所）。

    真实口径是 5571 只 / 6 页，翻页循环与真实数字由 `tests/test_market.py` 覆盖；
    界面这边只验证"汇总结果接到了页面上"。假客户端的页大小是 1000，所以一页就结束。
    """
    return [{"item": [
        {"thscode": "600519.SH", "turnover": 1e11, "volume": 1e6,
         "price_change_ratio_pct": 1.0},          # 上涨
        {"thscode": "000001.SZ", "turnover": 4e11, "volume": 2e6,
         "price_change_ratio_pct": -0.5},         # 下跌
        {"thscode": "830799.BJ", "turnover": 140.1e8, "volume": 3e5,
         "price_change_ratio_pct": 0.0},          # 平盘 + 北交所成交额
    ], "total": 3}]


def _inject_market_client(monkeypatch, injected):
    """让窗口自己发起的取数（定时器 / 切页 / 【立即刷新】）也走假客户端。

    为什么用 patch 最底层取数函数、而不是给 MainWindow 加注入口：
    这几条路径的意义正是"界面自己决定什么时候取数"，注入点越少越接近真实行为。
    """
    from laoa_trader import market

    real = market.fetch_overview

    def fake(cfg, client=None, force=False):    # noqa: ANN001 - 与真实签名保持一致
        # 界面自己造客户端的路径会传 None：这里换成假客户端，测试才不会联网
        return real(cfg, client=client or injected, force=force)

    monkeypatch.setattr(market, "fetch_overview", fake)




def _wait_market(window, qapp, timeout_ms: int = 10_000) -> None:
    """等概览页那一轮**后台**取数结束，并把结果送到界面上。

    界面路径（启动 / 定时器 / 切页 / 【立即刷新】）都跑在 QThread 里，
    而 `finished_ok` 是排队信号 —— 不 wait + processEvents 就断言，会读到上一轮的值。
    """
    worker = getattr(window, "_market_worker", None)
    if worker is not None:
        worker.wait(timeout_ms)
    qapp.processEvents()


@pytest.fixture()
def market_window(window, qapp):
    """把窗口切到「大盘概览」页（并等切页触发的那一轮后台取数落地）。

    为什么需要：Qt 里非当前页签里的子控件 `isVisible()` 恒为 False，
    不切过去就断言不了"整行隐藏"这类行为（虽然概览是第一个页签、启动就在它上面，
    但别的用例可能先把页签切走了）。切页会触发一轮后台取数，这里等它落地并清一次缓存，
    让用例从干净状态开始。
    """
    from laoa_trader import market

    window.tabs.setCurrentWidget(window.market_page)
    qapp.processEvents()
    _wait_market(window, qapp)
    market.clear_cache()
    yield window


@pytest.fixture()
def pool_window(window, qapp):
    """把窗口切到「股票池」页（那几行控件要在当前页才谈得上可见性与几何尺寸）。"""
    window.tabs.setCurrentWidget(_tab_page(window, ui_app.TAB_WATCH))
    qapp.processEvents()
    yield window


def test_market_page_is_a_tab_of_its_own(window) -> None:
    """概览是**独立一页、第一个页签**，页面是"四块 + 单行页脚"，**没有 KPI 卡片排**。

    用户 2026-09-17 给定的布局（顺序也是用户给的）：
    **成交与情绪 → 宽基指数 → 情绪指数 → 热门板块**。
    第一块装的是原来那排 KPI 折成的小条目（成交额/涨跌停/涨跌家数），
    挪到了「宽基指数」**上面**（用户原话："把'成交额等元素'模块挪到「宽基指数」上面"）。
    这条用例同时是"别把卡片排加回来"的看门狗。
    """
    from PySide6.QtWidgets import QScrollArea, QWidget

    pool_page = _tab_page(window, ui_app.TAB_WATCH)
    assert window.tabs.widget(0) is window.market_page
    assert window.tabs.widget(1) is pool_page
    assert window.tabs.currentWidget() is window.market_page
    assert window.market_page is not pool_page
    assert pool_page.isAncestorOf(window.market_page) is False

    page = window.market_page
    # **四块**，顺序也钉住（用户给定：成交额与情绪在最上面，然后宽基、情绪、热门板块）
    assert list(window.market_sections) == list(ui_app.MARKET_SECTION_TITLES) == [
        ui_app.MARKET_SECTION_FLOW, ui_app.MARKET_SECTION_WIDE,
        ui_app.MARKET_SECTION_SENTIMENT, ui_app.MARKET_SECTION_HOT,
    ]
    # 用户原话"把'成交额等元素'模块挪到「宽基指数」上面"：第一条就是它
    assert ui_app.MARKET_SECTION_FLOW == "成交与情绪"
    assert ui_app.MARKET_SECTION_TITLES.index(ui_app.MARKET_SECTION_FLOW) \
        < ui_app.MARKET_SECTION_TITLES.index(ui_app.MARKET_SECTION_WIDE)
    for title, section in window.market_sections.items():
        assert section.title_label.text() == title
        assert page.isAncestorOf(section) is True
        assert page.isAncestorOf(section.title_label) is True
    # 内容区顶层**就是这四块**（多一块"KPI 区"会被这条抓出来）
    content = window.market_content.layout()
    assert [content.itemAt(i).widget() for i in range(4)] == [
        window.market_sections[t] for t in ui_app.MARKET_SECTION_TITLES
    ]
    assert content.itemAt(4).spacerItem() is not None      # 第 5 项是收尾的 stretch
    # 那排卡片连骨头都不该剩下（属性、objectName 都查一遍）
    assert not hasattr(window, "market_kpis")
    assert not hasattr(window, "market_kpi_cards")
    assert not hasattr(window, "market_kpi_grid")
    names = {widget.objectName() for widget in page.findChildren(QWidget)}
    assert "marketKpiCard" not in names
    assert "marketKpiArea" not in names

    # 7 个小条目**在「成交与情绪」块里**（最上面那一块；既不在情绪块里、也不另开一排）
    flow = window.market_sections[ui_app.MARKET_SECTION_FLOW]
    # 一条一个数（合成文本在 Windows 字体下会被自己的列宽截掉，
    # 见 `ui/app.py` 里 `MARKET_STAT_TITLES` 的注释与 CI 实测数字）
    assert list(flow.stats) == list(ui_app.MARKET_STAT_TITLES) == [
        ui_app.MARKET_STAT_AMOUNT,                       # 成交额 = 沪 + 深（一个数）
        ui_app.MARKET_STAT_LIMIT_UP, ui_app.MARKET_STAT_LIMIT_DOWN,
        ui_app.MARKET_STAT_BREAK, ui_app.MARKET_STAT_UP,
        ui_app.MARKET_STAT_DOWN, ui_app.MARKET_STAT_FLAT,
    ]
    assert len(ui_app.MARKET_STAT_TITLES) == 7     # 9 条 → 7 条（北交所删掉、沪+深合并）
    assert ui_app.MARKET_STAT_MAX_COLUMNS == 7     # 宽屏一行正好 7 个（用户要求）
    for name, item in flow.stats.items():
        assert item.title_label.text() == name
        assert flow.isAncestorOf(item) is True
        assert page.isAncestorOf(item) is True
        # 名称加粗（用户："所有名称显示不清楚，都加黑显示"）
        assert item.title_label.font().bold() is True, name
    for title, section in window.market_sections.items():
        if title != ui_app.MARKET_SECTION_FLOW:
            assert section.stats == {}, title              # 小条目只在成交与情绪块里
    # 情绪块**只剩指数条目**（用户要求：情绪指数块不再有小条目）
    assert window.market_sections[ui_app.MARKET_SECTION_SENTIMENT].stats == {}

    # 页脚 / 提示 / 【立即刷新】都在这一页里
    assert page.isAncestorOf(window.market_footer) is True
    assert page.isAncestorOf(window.market_as_of_label) is True
    assert page.isAncestorOf(window.market_hint) is True
    assert page.isAncestorOf(window.btn_market_refresh) is True
    assert window.btn_market_refresh.text() == "立即刷新"

    # 页面自上而下：标题行 / 可滚动的内容区 / 提示 / **最后一行**页脚
    layout = page.layout()
    assert layout.itemAt(0).layout().indexOf(window.market_title) >= 0
    assert layout.itemAt(1).widget() is window.market_scroll
    assert layout.itemAt(2).widget() is window.market_hint
    assert layout.itemAt(3).widget() is window.market_footer
    assert layout.itemAt(layout.count() - 1).widget() is window.market_footer

    # 内容区可滚动：窗口变矮时它自己吸收高度变化，页脚不会被顶出窗口
    assert isinstance(window.market_scroll, QScrollArea)
    assert window.market_scroll.widgetResizable() is True
    assert window.market_scroll.horizontalScrollBar().isVisible() is False

# ── 「数据来源」的来源列表（可添加的多个来源，各自用自己的 Key）──


def test_source_list_adds_eastmoney_without_a_fake_key_box(
    window, seeded, qapp
) -> None:
    """**【添加来源】→ 东方财富**：写回 `data_sources`、列表出现它那一行、
    **免 Key 的来源不给假输入框**，再删掉就回到原样。

    这条用的是**真实的第二个来源**（`data/sources.py` 的 `eastmoney`），不是测试替身：
    候选、名称、能力文案、风险说明全部来自注册表 —— 界面不自己维护第二份。
    """
    from laoa_trader.data import sources as sources_mod

    config_file = seeded.data_dir / "config.toml"
    assert window.cfg.data_sources == ["hithink", "public"]   # 2026-09-18：同花顺回到主源
    assert "eastmoney" in sources_mod.REGISTRY           # 后端已实现第二个来源

    # 【添加来源】的菜单里就是它（抽成方法之后测试不用去点会阻塞的 `exec`）
    menu = window._source_add_menu()
    assert [action.text() for action in menu.actions()] == ["东方财富（公开接口，免 Key）"]
    assert "未文档化" in menu.actions()[0].toolTip()      # 风险说明也在菜单里

    window.on_add_source("eastmoney")
    qapp.processEvents()
    assert window.cfg.data_sources == ["hithink", "public", "eastmoney"]
    assert 'data_sources = ["hithink", "public", "eastmoney"]' in config_file.read_text(
        encoding="utf-8")
    # 列表顺序 = 优先级：同花顺（主）→ 公开源（兜底）→ 用户新加的（追加在末尾）
    assert list(window.source_rows) == ["hithink", "public", "eastmoney"]
    row = window.source_rows["eastmoney"]
    assert row.name_label.text() == "东方财富（公开接口，免 Key）"
    assert row.tag_label.text() == "免 Key"
    # **免 Key 的来源不给假输入框**（用户会去找一个根本不存在的 Key）
    assert row.key_edit is None
    assert row.btn_test is None
    assert "免 Key" in row.key_label.text()
    # 风险说明如实显示（后端给的原文：公开但未文档化的接口，可能变更或限流）
    assert "未文档化" in row.note_label.text()
    assert "限流" in row.note_label.text()
    # 后端文案里的 markdown 强调标记在纯文本 QLabel 上会显示成字面星号 —— 去掉
    assert "**" not in row.note_label.text()
    assert "单位是手" in row.note_label.text()
    assert "**手**" in row.note_label.toolTip()          # 原文完整保留在 tooltip 里
    # 能力文案来自真相源，且**不许**出现它没有的能力（涨停池/复权因子这类）
    assert row.capability_label.text() == "提供：" + \
        sources_mod.capabilities_text(sources_mod.REGISTRY["eastmoney"])
    assert "涨停" not in row.capability_label.text()
    assert row.btn_delete.isHidden() is False             # 用户加的可以删
    assert window._source_add_menu() is None              # 没有别的可加了
    assert window.source_add_hint.text().startswith("没有可添加的来源了")

    # 删除 → 回到原来的两个来源
    row.btn_delete.click()
    qapp.processEvents()
    assert window.cfg.data_sources == ["hithink", "public"]   # 2026-09-18：同花顺回到主源
    assert list(window.source_rows) == ["hithink", "public"]   # 2026-09-18：同花顺回到主源
    assert 'data_sources = ["hithink", "public"]' in config_file.read_text(
        encoding="utf-8")
    # 内置那一条**删不掉**（历史日K 的 dump 只有它提供）
    window.on_remove_source("hithink")
    assert "hithink" in window.source_rows
    assert "不能删除" in window.status_label.fullText()


def test_source_list_key_field_enters_the_one_click_save(window, seeded, qapp,
                                                         monkeypatch) -> None:
    """**将来再加"需要 Key 的来源"时，它的 Key 自动进一键保存的键集合**。

    用一条**注册表里的测试替身**验证契约（公开源与东方财富都是免 Key 的，走不到这条分支）：
    `key_config` 指到哪个配置键，一键保存就写哪个键 —— 界面上的输入框与写回
    config.toml 的键一一对应，不存在"填了没保存"。
    这也顺带证明：同花顺不再被收进这份键集合，是**因为界面上没有它的输入框**，
    而不是因为代码里写死了一个"同花顺除外"的例外。
    """
    from laoa_trader.data import sources as sources_mod

    fake_key = "fake_source_token"
    monkeypatch.setitem(sources_mod.REGISTRY, "fakesrc", sources_mod.SourceInfo(
        id="fakesrc", name="测试来源（要 Key）", needs_key=True,
        capabilities=frozenset({sources_mod.CAP_DAILY_HISTORY}),
        note="测试替身：只用来验证「要 Key 的来源」这条分支。",
        key_config=fake_key,
    ))
    seeded.data_sources = ["hithink", "public", "fakesrc"]
    window._rebuild_source_rows()
    qapp.processEvents()
    row = window.source_rows["fakesrc"]
    assert row.key_edit is not None                       # 要 Key → 有输入框
    assert row.tag_label.text() == "未配 Key"
    # 同花顺那一行的输入口由它**自己的** `key_config` 决定（不是写死的特例）
    # 2026-09-18：内置同花顺那一行**有**输入框（用户要回了输入口）
    assert window.source_rows["hithink"].key_edit is not None
    row.key_edit.setText("token-abc")
    updates = window._collect_settings_updates()
    assert updates[fake_key] == "token-abc"               # 它的 Key 进了一键保存的键集合
    # 写盘路径也是通的（`fake_source_token` 不是 Config 的字段，但存进文件没问题）
    window.save_settings_button.click()
    qapp.processEvents()
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert f'{fake_key} = "token-abc"' in text
    # 35 个固定键（含 `hithink_api_key`）+ 这个测试替身来源的 Key = 36 项
    assert window.save_settings_hint.text().startswith("✅ 已保存 36 项")
    # 内置同花顺的 Key 也在这份键集合里（它的输入框和替身来源的走同一条规则）
    assert "hithink_api_key" in window._collect_settings_updates()


def test_source_list_falls_back_when_the_registry_is_unreadable(
    window, seeded, qapp, monkeypatch, caplog
) -> None:
    """`data/sources.py` 读不出来（还没写好 / 它自己抛异常）→ **退回内置那一行**，窗口照开。

    一张设置页绝不该把整个程序拖死：真出问题时用户连界面都进不去，什么都问不出来。
    兜底那一行必须**如实说明**为什么只有它（而不是默默只显示同花顺）。
    """
    from laoa_trader.data import sources as sources_mod
    from laoa_trader.ui import app as ui_app

    def boom(_cfg):
        raise RuntimeError("注册表炸了")

    monkeypatch.setattr(sources_mod, "source_states", boom)
    window._rebuild_source_rows()
    qapp.processEvents()
    assert list(window.source_rows) == [ui_app.BUILTIN_SOURCE]
    row = window.source_rows[ui_app.BUILTIN_SOURCE]
    assert "注册表暂时读不出来" in row.note_label.text()      # 如实说明，不是空白
    # 这一行照旧是"主来源 + 申请地址"（它不依赖注册表，是内置的）
    assert row.tag_label.text() == ui_app.BUILTIN_BACKUP_TAG
    # 输入框也在：它不依赖注册表（写死的就是 `hithink_api_key`），而且这一行**需要 Key**
    assert row.key_edit is not None
    assert "fuyao.aicubes.cn" in row.key_label.text()
    assert row.key_label.openExternalLinks() is True
    assert window._addable_sources() == []                     # 没有候选 → 不会画假条目
    assert window._source_add_menu() is None
    # 兜底那一行也有输入口，所以一键保存照样收 `hithink_api_key`
    #（注册表读不出来**不影响**这一点：这个键名是内置的，不靠注册表告诉它）
    assert "hithink_api_key" in window._collect_settings_updates()


def test_source_list_shows_unknown_names_truthfully(window, seeded, qapp) -> None:
    """配置里手写了认不出的来源名 → **照实显示**并注明没有实现，不假装认识它。

    改写死一行"主来源：同花顺"的话，用户改过 `data_sources` 之后界面就在说假话。
    """
    seeded.data_sources = ["hithink", "mystery"]
    window._rebuild_source_rows()
    qapp.processEvents()
    # **列表顺序 = 配置里写的顺序**（那是真实的取数优先级）：配置里同花顺写在前面，
    # 界面就照实画在前面 —— 界面只在"配置里漏写了它"时把它补在**最后**（备用位置）
    assert list(window.source_rows) == ["hithink", "mystery"]
    unknown = window.source_rows["mystery"]
    assert unknown.name_label.text() == "mystery"          # 键本身就当名字显示
    assert unknown.tag_label.text() == "未实现"
    # **没有它的 Key 输入这回事**：不画一个填不了东西的框（画了用户会去找 Key）
    assert unknown.key_edit is None
    assert "界面还没有这个来源的实现" in unknown.key_label.text()
    assert unknown.capability_label.text() == "提供：—"
    assert "mystery" in window.data_source_label.text()    # 配置原值也写出来


def test_settings_tab_is_scrollable_so_the_window_can_shrink(window, qapp) -> None:
    """设置页控件最多，**整页套了滚动区**，否则它会把"窗口最小高度"顶到屏幕外面去。

    为什么这条值得测：五组控件的"最小高度"合起来 1000+，而页签的最小高度取
    所有页的最大值 —— 不套滚动区时窗口最小高度会顶到屏幕外（1366×768 的笔记本上
    根本放不下，表现就是"一打开最下边看不见"）。这条盯的是那个根因别再加回来。
    """
    from PySide6.QtWidgets import QScrollArea

    page = _tab_page(window, ui_app.TAB_SETTINGS)
    scrolls = page.findChildren(QScrollArea)
    assert scrolls, "设置页需要一层滚动区（小屏才缩得下来）"
    assert scrolls[0].widgetResizable() is True
    # 五组都在滚动区里面（顺序 = 用户给定的顺序，滚动区只是外套）
    for title in ui_app.SETTINGS_GROUPS:
        assert scrolls[0].isAncestorOf(window.settings_sections[title]) is True, title
    assert scrolls[0].isAncestorOf(window.save_settings_button) is True
    assert scrolls[0].isAncestorOf(window.channel_boxes["tray"]) is True
    # 窗口的最小高度收得住（小于 1366×768 那档的可用高度）
    assert window.minimumSizeHint().height() < 600

def test_market_page_refreshes_right_after_startup(seeded, qapp, monkeypatch) -> None:
    """概览是**第一个页签**：窗口一起来就自动取一次，不干等 60 秒的定时器。

    取数在后台线程（启动时数据自检也在跑，两条都压主线程界面就"出来了却点不动"），
    所以这里等它落地再看页面。
    """
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    client = _market_fake()
    _inject_market_client(monkeypatch, client)      # 必须在建窗口**之前**注入
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()                            # singleShot(0) 在这里触发
    _wait_market(win, qapp)
    try:
        assert win.tabs.currentWidget() is win.market_page   # 第一屏就是概览
        assert client.calls                                  # 启动那一次确实去取了
        assert win.market_overview is not None
        stats = win.market_sections[ui_app.MARKET_SECTION_FLOW].stats
        assert stats[ui_app.MARKET_STAT_LIMIT_UP].value_label.text() == "55"
        assert stats[ui_app.MARKET_STAT_LIMIT_DOWN].value_label.text() == "16"
        assert stats[ui_app.MARKET_STAT_BREAK].value_label.text() == "30"
        # 成交额 = 沪 7792 + 深 8499 = **16291 亿**，一个数（北交所那 140 亿不再显示）
        assert stats[ui_app.MARKET_STAT_AMOUNT].value_label.text() == "16291亿"
        wide = win.market_sections[ui_app.MARKET_SECTION_WIDE]
        assert wide.entries[0].name_label.text() == "上证"
        # 「热门板块」跟着同一趟取回来了（seeded 库里有半导体涨停）
        hot = win.market_sections[ui_app.MARKET_SECTION_HOT]
        rows = hot.tables[ui_app.SECTOR_UP_TITLE].table
        assert rows.rowCount() >= 1
        assert rows.item(0, 0).text() == "半导体"        # 本地口径下涨幅最高的行业
        assert "更新于 —" not in win.market_as_of_label.fullText()   # 页脚已是真实取数时间
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win._auction_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()
        market.clear_cache()

#: 概览页两块**指数**应当长成的样子（顺序 = 配置顺序，数值照抄 `SAMPLE_ROWS` 的当日在值）。
#: 与 `tests/test_market.py` 的 `SAMPLE_LINES` 是同一批数字：界面与命令行口径必须一致。
#: 「热门板块」块摆的不是指数（它由 `pool.hot_industries` 算出来，断言在别处）。
MARKET_EXPECTED_ENTRIES: dict[str, list[tuple[str, str, str, str]]] = {
    # 宽基 = 配置里的五只 + **必有的两只**（上证50 / 中证1000，2026-09-17 用户要求；
    # 中证2000 腾讯实测拿不到，换成中证1000，见 `market.WIDE_INDEX_EXTRA_CODES`）
    "宽基指数": [
        ("000001.SH", "上证", "3885.33", "-0.07%"),
        ("399001.SZ", "深成", "13384.57", "-0.64%"),
        ("399006.SZ", "创业板", "3285.58", "-1.10%"),
        ("000688.SH", "科创50", "1528.27", "-1.62%"),
        ("000300.SH", "沪深300", "4480.08", "-0.67%"),
        ("000016.SH", "上证50", "2844.23", "+0.42%"),
        ("000852.SH", "中证1000", "7548.82", "-1.21%"),
    ],
    # 「情绪指数」块装的是**两组**同花顺板块指数：`market_sentiment_indices` 在前、
    # `market_sector_indices` 在后（沿用现有取数；用户给定的三块里只有这一块装它们）
    "情绪指数": [
        ("883404.TI", "同花顺情绪", "885.53", "-0.18%"),
        ("883958.TI", "昨日连板", "6928.17", "+3.53%"),
        ("883994.TI", "昨日打首板", "1551.88", "+1.69%"),
        ("883418.TI", "微盘股", "2131.00", "+0.95%"),
        ("881155.TI", "银行", "1408.77", "+0.88%"),
        ("881157.TI", "证券", "1428.64", "-0.27%"),
    ],
}


def _entry_fields(entry) -> tuple[str, str, str, str]:
    """一个条目控件的四段可断言文本：代码 / 名称 / 点位 / 涨跌幅。"""
    return (
        entry.thscode,
        entry.name_label.text(),
        entry.value_label.text(),
        entry.pct_label.text(),
    )


def _entry_colors(entry) -> tuple[str, str]:
    """一个条目的两个颜色（点位、涨跌幅）——逐值上色时这两个都由**自己**的涨跌决定。"""
    return entry.value_label.styleSheet(), entry.pct_label.styleSheet()


def _stat_texts(window) -> dict[str, str]:
    """7 个小条目的 `{名字: 数值}`（那一排 KPI 折进「成交与情绪」块之后的）。"""
    stats = window.market_sections[ui_app.MARKET_SECTION_FLOW].stats
    return {name: item.value_label.text() for name, item in stats.items()}


def _sector_table_rows(window, title: str) -> list[list[str]]:
    """「上涨前五 / 下跌前五」某一张表的全部单元格文字（含表头那一行不在此列）。"""
    table = window.market_sections[ui_app.MARKET_SECTION_HOT].tables[title].table
    return [
        [table.item(row, column).text() if table.item(row, column) is not None else ""
         for column in range(table.columnCount())]
        for row in range(table.rowCount())
    ]


def _sector_headers(window, title: str) -> list[str]:
    """某一张板块表的表头文字（用户要求"标上名称"的那四列）。"""
    table = window.market_sections[ui_app.MARKET_SECTION_HOT].tables[title].table
    return [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]


def test_market_page_renders_stats_entries_colors_and_footer(market_window, qapp) -> None:
    """7 个小条目 + 两块指数的每一项 + 逐值颜色 + 热门板块两张表 + 页脚，一次全钉住。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        assert market_window.market_overview is not None
        # 7 个小条目：与 `market.kpi_values()` 同源（取数口径与命令行共用一份）
        values = market.kpi_values(market_window.market_overview)
        assert (values["涨停"], values["跌停"], values["炸板"]) == ("55", "16", "30")
        # 成交额 = 沪 7792 + 深 8499 = 16291 亿（**一个数**，用户要求）
        assert values["成交额"] == "16291亿"
        assert (values["上涨"], values["下跌"], values["平盘"]) == ("1", "1", "1")
        stats = _stat_texts(market_window)
        assert stats == {
            "成交额": "16291亿",
            "涨停": "55", "跌停": "16", "炸板": "30",
            "上涨": "1", "下跌": "1", "平盘": "1",
        }
        assert list(stats) == list(ui_app.MARKET_STAT_TITLES)   # 显示顺序 = 常量顺序
        assert "北交所" not in stats and "140亿" not in "".join(stats.values())

        # 两块指数的每一项：一个指数一个控件，四项文本逐项对齐
        for title, expected in MARKET_EXPECTED_ENTRIES.items():
            section = market_window.market_sections[title]
            assert section.isVisible() is True
            assert section.placeholder_label.isVisible() is False   # 有数据就不要 `—` 占位
            assert [_entry_fields(entry) for entry in section.entries] == expected
        assert [entry.thscode for entry in market_window.market_entries] == [
            code for rows in MARKET_EXPECTED_ENTRIES.values() for code, *_ in rows
        ]
        # 名称一律**加粗**（用户："所有名称显示不清楚，都加黑显示"），数值不加粗
        for entry in market_window.market_entries:
            assert entry.name_label.font().bold() is True, entry.thscode
            assert entry.value_label.font().bold() is False, entry.thscode
            assert entry.pct_label.font().bold() is False, entry.thscode

        # 逐值上色：宽基里 5 只跌（绿）+ 上证50 涨（红）+ 中证1000 跌（绿），
        # 情绪块里 4 情绪 + 2 板块各自按自己的涨跌
        up, down = f"color:{market.COLOR_UP}", f"color:{market.COLOR_DOWN}"
        wide = market_window.market_sections[ui_app.MARKET_SECTION_WIDE].entries
        assert [_entry_colors(e) for e in wide] == [
            (down, down)] * 5 + [(up, up), (down, down)]
        sentiment = market_window.market_sections[ui_app.MARKET_SECTION_SENTIMENT].entries
        assert [_entry_colors(e) for e in sentiment] == (
            [(down, down)] + [(up, up)] * 3 + [(up, up), (down, down)]
        )
        assert market_window.market_hint.isVisible() is False       # 一切正常不留提示

        # 「热门板块」：**两张表**（上涨前五 / 下跌前五），表头就是用户给定的那四列
        for title in (ui_app.SECTOR_UP_TITLE, ui_app.SECTOR_DOWN_TITLE):
            assert _sector_headers(market_window, title) == list(ui_app.SECTOR_TABLE_HEADERS)
        assert list(market_window.market_sections[
            ui_app.MARKET_SECTION_HOT].tables) == [ui_app.SECTOR_UP_TITLE,
                                                   ui_app.SECTOR_DOWN_TITLE]
        # 测试环境里 `data/sectors.py` 的取数被 socket 层封死 → 退回**本地口径**：
        # 涨幅 = 近 5 日行业等权涨幅，主力净额 `—`（不是 0），页面上写清了口径
        hot_note = market_window.market_sections[ui_app.MARKET_SECTION_HOT].note_label
        assert hot_note.isVisible() is True
        assert "本地口径" in hot_note.fullText()
        assert "主力净额取不到" in hot_note.fullText()
        up_rows_local = _sector_table_rows(market_window, ui_app.SECTOR_UP_TITLE)
        assert [row[0] for row in up_rows_local] == ["半导体", "银行"]   # seeded 库的真实行业
        assert up_rows_local[0][1] == "1"            # 涨停家数口径来自 pool.hot_industries
        assert up_rows_local[1][1] == "0"
        assert all(row[3] == market.DASH for row in up_rows_local)      # 主力净额取不到 → —

        # 页脚：数据来源 + 取数时间 + 刷新节奏（breadth 开着要注明它慢一档）
        footer = market_window.market_as_of_label.fullText()
        assert footer.startswith(market.FOOTER_PREFIX + " · 更新于 ")
        assert "每分钟自动刷新" in footer
        # 成交额已改成"沪深合计"（与指数同节拍），所以这句只提涨跌家数
        assert "涨跌家数每 5 分钟更新" in footer
        assert "北交所" not in footer
        assert market_window.market_overview["as_of"] in footer

        # 全线上涨 → 点位与涨跌幅都变红（颜色跟着新数据走，不是建页面时定死的）
        up_rows = [
            {"thscode": "000001.SH", "last_price": 3900.0,
             "price_change_ratio_pct": 0.86, "turnover": 8e11},
            {"thscode": "399001.SZ", "last_price": 13400.0,
             "price_change_ratio_pct": 0.12, "turnover": 9e11},
        ]
        market_window.refresh_market_overview(force=True, client=_market_fake(rows=up_rows))
        qapp.processEvents()
        wide = market_window.market_sections[ui_app.MARKET_SECTION_WIDE].entries
        assert [e.value_label.text() for e in wide] == ["3900.00", "13400.00"]
        assert [e.pct_label.text() for e in wide] == ["+0.86%", "+0.12%"]
        assert [_entry_colors(e) for e in wide] == [(up, up)] * 2
    finally:
        market.clear_cache()

def test_market_colors_each_value_on_its_own_move(market_window, qapp) -> None:
    """**重点**：同一块里一涨一跌一平 → 每一项按自己的涨跌上色（不是"整块同色"）。

    这正是"一行一个 QLabel"做不到的事：情绪/板块经常同时有涨有跌，
    按块取色只能"要么全染红、要么全不染"，两种都在骗人。
    """
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        rows = [
            {"thscode": "883404.TI", "last_price": 900.0, "price_change_ratio_pct": 1.20},
            {"thscode": "883958.TI", "last_price": 6800.0, "price_change_ratio_pct": -0.80},
            {"thscode": "883994.TI", "last_price": 1551.0, "price_change_ratio_pct": 0.0},
        ]
        market_window.refresh_market_overview(force=True, client=_market_fake(rows=rows))
        qapp.processEvents()

        entries = market_window.market_sections[ui_app.MARKET_SECTION_SENTIMENT].entries
        by_code = {e.thscode: e for e in entries}
        up = by_code["883404.TI"]
        down = by_code["883958.TI"]
        flat = by_code["883994.TI"]

        assert (up.value_label.text(), up.pct_label.text()) == ("900.00", "+1.20%")
        assert _entry_colors(up) == (f"color:{market.COLOR_UP}",) * 2
        assert _entry_colors(down) == (f"color:{market.COLOR_DOWN}",) * 2
        # 平盘：两个 label 都没有 color 样式（用界面默认色，不硬塞一个颜色）
        assert _entry_colors(flat) == ("", "")
        assert flat.pct_label.text() == "+0.00%"
        # 同一块里既有红又有绿 —— 逐值上色才做得到
        assert {_entry_colors(up)[0], _entry_colors(down)[0]} == {
            f"color:{market.COLOR_UP}", f"color:{market.COLOR_DOWN}",
        }
        # 颜色只在 `market` 里定义一次，界面与测试都从那里取
        assert (market.COLOR_UP, market.COLOR_DOWN) == ("#d32f2f", "#2e7d32")
    finally:
        market.clear_cache()

def test_market_footer_is_exactly_one_row(market_window, qapp) -> None:
    """页脚只能**一行**：左边数据来源、右边【立即刷新】；只有出错提示才占"第二行"。"""
    from PySide6.QtCore import QPoint
    from laoa_trader import market

    win = market_window
    market.clear_cache()
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        label = win.market_as_of_label
        button = win.btn_market_refresh
        footer = win.market_footer
        layout = win.market_footer_layout
        # 两个控件在**同一个**布局里（按钮没有另起一行）
        assert footer.layout() is layout
        assert layout.indexOf(label) >= 0
        assert layout.indexOf(button) >= 0
        # 纵向中心在同一条线上：按钮确实在第一行里
        label_middle = label.mapTo(win, QPoint(0, 0)).y() + label.height() / 2
        button_middle = button.mapTo(win, QPoint(0, 0)).y() + button.height() / 2
        assert abs(label_middle - button_middle) <= 4
        # 页脚高度就是"一行"的高度（多出一行的话这里会明显变高）
        assert footer.height() <= max(label.height(), button.height()) + 8
        # 左标签、右按钮，且都在页脚里（按钮没被挤出去）
        assert button.x() >= label.x() + label.width() - 1
        assert button.geometry().right() <= footer.width() + 1
        # 正常状态页面上只有这一行页脚（提示不占位）
        assert win.market_hint.isVisible() is False
    finally:
        market.clear_cache()


def test_market_page_hides_whole_block_when_config_is_empty(market_window, qapp) -> None:
    """`market_indices` 没配 → 「宽基指数」块**连标题一起隐藏**（不留空的"宽基指数："）。

    「成交与情绪」块**始终显示**：它装的是 7 个小条目（成交额/涨跌停/涨跌家数），
    与"配没配指数"无关 —— 这也是它与旧版最大的区别（旧版整组隐藏后那一排就空了）。
    另外：`market_indices = []` 是"我不要这一块"的明确表态，所以这时候**不补**
    "必有的两只"（`market.WIDE_INDEX_EXTRA_CODES`）—— 见 `wide_specs()` 的说明。
    """
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.cfg.market_indices = []
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        wide = market_window.market_sections[ui_app.MARKET_SECTION_WIDE]
        assert wide.isVisible() is False
        assert wide.title_label.isVisible() is False     # 标题也跟着收掉，不留空标题
        assert wide.entries == []
        flow = market_window.market_sections[ui_app.MARKET_SECTION_FLOW]
        assert flow.isVisible() is True                  # 成交与情绪块照常（它装着 7 个小条目）
        sentiment = market_window.market_sections[ui_app.MARKET_SECTION_SENTIMENT]
        assert sentiment.isVisible() is True             # 情绪块照常（指数取到就显示）
        # 成交额是**从宽基条目里**取的（`market.py` 的口径：上证/深证两只指数的 turnover
        # 相加）—— 宽基没配时它必然是 `—`。这条断言把这个依赖关系钉住
        #（不是缺陷，是取数口径的必然结果；北交所那一格已经删掉了，不再受影响）
        assert flow.stats[ui_app.MARKET_STAT_AMOUNT].value_label.text() == market.DASH
        hot = market_window.market_sections[ui_app.MARKET_SECTION_HOT]
        assert hot.isVisible() is True                   # 热门板块不受指数配置影响
        assert "上证" not in "".join(e.name_label.text() for e in market_window.market_entries)
        assert all(e.thscode != "000001.SH" for e in market_window.market_entries)
    finally:
        market.clear_cache()

def test_market_page_drops_only_the_bad_code(market_window, qapp) -> None:
    """**重点**：配置里混一个取不到的代码（这里用 932000.TI）→ 页面上**只少那一项**。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.cfg.market_sentiment_indices = [
            "883404.TI", "883958.TI", "883994.TI", "932000.TI", "883418.TI",
        ]
        market_window.refresh_market_overview(
            force=True, client=_market_fake(index_fail=("932000.TI",))
        )
        qapp.processEvents()

        sentiment = market_window.market_sections[ui_app.MARKET_SECTION_SENTIMENT]
        assert [e.thscode for e in sentiment.entries][:4] == [
            "883404.TI", "883958.TI", "883994.TI", "883418.TI",
        ]
        assert "932000" not in "".join(
            e.name_label.text() + e.value_label.text() for e in market_window.market_entries
        )
        assert "昨日连板" in [e.name_label.text() for e in sentiment.entries]
        # 「宽基指数」块一字不动
        assert [_entry_fields(e) for e in
                market_window.market_sections[ui_app.MARKET_SECTION_WIDE].entries] \
            == MARKET_EXPECTED_ENTRIES[ui_app.MARKET_SECTION_WIDE]
        assert market_window.market_overview["failed"] == ["932000.TI"]
        assert "932000.TI" in market_window.market_hint.text()
    finally:
        market.clear_cache()

def test_market_page_shows_dash_and_reason_without_data(market_window, qapp) -> None:
    """拿不到数据：该显示 `—` 的地方显示 `—`、原因写在页面上，界面照常可用。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.cfg.hithink_api_key = ""              # 没配 Key：真实路径直接降级
        market_window.refresh_market_overview(force=True)
        qapp.processEvents()

        assert market_window.market_overview is not None    # 拿不到 ≠ 抛异常，而是"有结构没数据"
        assert market.has_data(market_window.market_overview) is False
        # 7 个小条目全是 `—`：成交额（沪+深）与涨跌停、涨跌家数一起降级
        assert _stat_texts(market_window) == {
            "成交额": market.DASH,
            "涨停": market.DASH, "跌停": market.DASH, "炸板": market.DASH,
            "上涨": market.DASH, "下跌": market.DASH, "平盘": market.DASH,
        }
        # 配了的两块指数还在（用户才知道自己配的东西在哪一行），只是没数据 → 一个 `—` 占位
        for title in (ui_app.MARKET_SECTION_WIDE, ui_app.MARKET_SECTION_SENTIMENT):
            section = market_window.market_sections[title]
            assert section.isVisible() is True, title
            assert section.title_label.isVisible() is True
            assert section.entries == []
            assert section.placeholder_label.text() == market.DASH
            assert section.placeholder_label.isVisible() is True
        assert market_window.market_entries == []
        # 「热门板块」的**本地兜底**（涨停家数 + 近 5 日行业等权涨幅）与有没有 Key /
        # 服务端通不通**无关** —— 概览全灭时它照样有内容，这正是它的价值
        hot = market_window.market_sections[ui_app.MARKET_SECTION_HOT]
        assert hot.isVisible() is True
        up_rows = _sector_table_rows(market_window, ui_app.SECTOR_UP_TITLE)
        assert [row[0] for row in up_rows] == ["半导体", "银行"]
        assert hot.placeholder_label.isVisible() is False
        assert market_window.market_hint.isVisible() is True
        assert "同花顺 Key" in market_window.market_hint.text()
        assert "同花顺 Key" in market_window.market_page.toolTip()
        # 页脚那行小字照样有（时间是 —）
        assert "更新于 —" in market_window.market_as_of_label.fullText()

        market_window._tick()                                # 界面其它部分照常刷新
        qapp.processEvents()
        assert market_window.pool_table.rowCount() == 1      # 池子照常显示（概览坏不影响它）
        assert market_window.pool_count_label.text() == "共 1 只（策略 1 · 自选 0）"
    finally:
        market.clear_cache()

def test_market_breadth_off_shows_dash_and_says_why(market_window, qapp) -> None:
    """`market_breadth = false`：涨跌家数显示 `—`，**一个分页请求都不发**，并在页内说明原因。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.cfg.market_breadth = False
        client = _market_fake()
        market_window.refresh_market_overview(force=True, client=client)
        qapp.processEvents()

        assert client.count("request") == 0
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_UP] == market.DASH
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_DOWN] == market.DASH
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_FLAT] == market.DASH
        # 成交额与全市场快照无关（沪深两市来自两只宽基指数）→ 这一格照样有数
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_AMOUNT] == "16291亿"
        # 光看一个 `—` 用户猜不出为什么 —— 必须在页面上说清是"这个开关关着"
        assert market_window.market_hint.isVisible() is True
        assert "market_breadth" in market_window.market_hint.text()
        # 说的是具体哪几条（改版后是三个独立小条目：上涨/下跌/平盘）
        for title in ("上涨", "下跌", "平盘"):
            assert title in market_window.market_hint.text()
        assert "market_breadth" in market_window.market_sections[
            ui_app.MARKET_SECTION_FLOW
        ].stats[ui_app.MARKET_STAT_UP].value_label.toolTip()
        # 关掉时页脚就不该再提"每 5 分钟更新"
        assert "每 5 分钟" not in market_window.market_as_of_label.fullText()
    finally:
        market.clear_cache()

def test_market_page_font_hierarchy_and_alignment(market_window, qapp) -> None:
    """字号只有三级（页面标题 > 分区标题 = 正文），小条目数值比标签大一号加粗，数字右对齐。

    先注入假客户端刷一轮：**有数据才谈得上"数字竖着对齐"**（没数据时条目是 `—` 占位）。
    """
    from laoa_trader import market

    win = market_window
    market.clear_cache()
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        _assert_market_fonts_and_alignment(win)
    finally:
        market.clear_cache()

def _assert_market_fonts_and_alignment(win) -> None:
    """字号层级 / 加粗 / 右对齐 / 列宽一致 —— 从上面那条用例里拆出来只是为了让主用例短一点。"""
    from PySide6.QtCore import Qt

    from laoa_trader.ui import app as ui_app

    title_font = win.market_title.font()
    sections = win.market_sections
    group_font = sections[ui_app.MARKET_SECTION_WIDE].title_label.font()
    assert title_font.bold() is True
    assert title_font.pointSize() > group_font.pointSize()          # 页面标题最大
    from laoa_trader import market

    item = sections[ui_app.MARKET_SECTION_FLOW].stats[ui_app.MARKET_STAT_AMOUNT]
    assert item.value_label.text() != market.DASH                   # 有数才谈得上对齐
    assert item.value_label.font().bold() is True                   # 数值加粗（既有层级，没动）
    # "大一号"：数值比它自己的标签大一号（标签保持正文号，只是加粗）
    assert item.value_label.font().pointSize() == item.title_label.font().pointSize() + 1
    # **名称加粗**（用户："所有名称显示不清楚，都加黑显示"）：小条目的名字也得加粗
    assert item.title_label.font().bold() is True
    assert item.value_label.alignment() & Qt.AlignmentFlag.AlignRight
    # 条目里点位与涨跌幅都右对齐；两列的列宽对所有条目都一样 → 数字落在同一条竖线上
    entries = sections[ui_app.MARKET_SECTION_WIDE].entries
    assert entries, "需要有数据才谈得上对齐"
    for entry in entries:
        assert entry.value_label.alignment() & Qt.AlignmentFlag.AlignRight
        assert entry.pct_label.alignment() & Qt.AlignmentFlag.AlignRight
    tracks = {
        (e.value_label.minimumWidth(), e.pct_label.minimumWidth()) for e in entries
    }
    assert len(tracks) == 1
    assert tracks.pop()[0] > 0
    # 「热门板块」两张表：数值列右对齐、板块名称加粗
    hot = win.market_sections[ui_app.MARKET_SECTION_HOT]
    assert hot.tables, "两块表是固定的"
    for title, block in hot.tables.items():
        assert block.table.rowCount() >= 1, title          # 有数据才谈得上对齐
        for column in (1, 2, 3):                           # 涨停数量 / 涨幅 / 主力净额
            for row in range(block.table.rowCount()):
                item = block.table.item(row, column)
                assert item.textAlignment() & Qt.AlignmentFlag.AlignRight
        # 名称那一列**加粗**，数值列不加粗（用户明确要求别把数值也加粗）
        for row in range(block.table.rowCount()):
            assert block.table.item(row, 0).font().bold() is True
            for column in (1, 2, 3):
                assert block.table.item(row, column).font().bold() is False

def test_market_refresh_button_forces_refetch(market_window, qapp, monkeypatch) -> None:
    """【立即刷新】忽略 TTL 缓存，一定重打接口（与 `force=True` 同义）。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        client = _market_fake()
        _inject_market_client(monkeypatch, client)
        market_window.refresh_market_overview(force=True, client=client)
        calls = len(client.calls)
        assert calls > 0

        market_window.btn_market_refresh.click()          # 真点按钮（信号接线也要测到）
        _wait_market(market_window, qapp)                 # 界面路径在后台线程里取
        assert len(client.calls) == calls * 2
        assert market_window.market_overview["stale"] is False
    finally:
        market.clear_cache()


def test_market_timer_is_one_minute_and_only_when_page_visible(
    window, qapp, monkeypatch
) -> None:
    """每分钟刷一次，且**只在概览页可见时**才真刷（别的页面/最小化时一次都不打）。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        client = _market_fake()
        _inject_market_client(monkeypatch, client)
        assert window._market_timer.interval() == ui_app.MARKET_REFRESH_MS == 60_000

        # 当前在股票池页（概览是第一个页签）→ 定时器空转，一次接口都不打
        window.tabs.setCurrentIndex(1)
        window._market_tick()
        _wait_market(window, qapp)
        assert client.calls == []

        # 切回概览页 → 立刻刷（用户不该看到一分钟前的旧数）
        window.tabs.setCurrentIndex(0)
        _wait_market(window, qapp)
        calls = len(client.calls)
        assert calls > 0

        # 窗口隐藏（最小化到托盘）→ 定时器不刷
        window.hide()
        window._market_tick()
        _wait_market(window, qapp)
        assert len(client.calls) == calls
        window.show()
        qapp.processEvents()

        # 回到概览页且概览缓存过期 → 定时器这次真刷；
        # 但**全市场汇总走它自己的 5 分钟缓存**，不会跟着每分钟重翻页
        index_calls, page_calls = client.count("index"), client.count("request")
        market._CACHE["at"] = market._CACHE["at"] - 60      # 模拟 55 秒已过
        window._market_tick()
        _wait_market(window, qapp)
        assert client.count("index") == index_calls + 1
        assert client.count("request") == page_calls        # 一页都没重翻
    finally:
        market.clear_cache()


def test_switching_to_market_tab_refreshes_when_cache_expired(
    window, qapp, monkeypatch
) -> None:
    """切到概览页：缓存没过期只是重画（不重复打接口），过期了立刻取新的。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        client = _market_fake()
        _inject_market_client(monkeypatch, client)
        window.tabs.setCurrentIndex(0)              # 概览页（第一个页签）
        window.refresh_market_overview(force=True, client=client)
        calls = len(client.calls)

        window.tabs.setCurrentIndex(1)              # 走开到股票池
        window.tabs.setCurrentIndex(0)              # 再切回来：TTL 内，不该重复取
        _wait_market(window, qapp)
        assert len(client.calls) == calls
        assert window.market_overview["stale"] is True

        index_calls = client.count("index")
        market._CACHE["at"] = market._CACHE["at"] - 60      # 模拟 TTL 过期
        window.tabs.setCurrentIndex(1)
        window.tabs.setCurrentIndex(0)
        _wait_market(window, qapp)
        assert client.count("index") == index_calls + 1     # 指数这一路立刻取新的
        assert window.market_overview["stale"] is False
    finally:
        market.clear_cache()


def test_market_overview_off_shows_hint_and_makes_no_request(market_window, qapp) -> None:
    """总开关关掉：页面显示 `—` 并说明原因，且**一个请求都不发**。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        market_window.cfg.market_overview = False
        client = _market_fake()
        market_window.refresh_market_overview(force=True, client=client)
        qapp.processEvents()
        assert client.calls == []
        # 配了的两块照样显示（只是没数据，占位是 `—`）
        wide = market_window.market_sections[ui_app.MARKET_SECTION_WIDE]
        assert wide.isVisible() is True
        assert wide.placeholder_label.isVisible() is True
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_LIMIT_UP] \
            .startswith(market.DASH)
        assert _stat_texts(market_window)[ui_app.MARKET_STAT_AMOUNT] == market.DASH
        assert "market_overview" in market_window.market_hint.text()
    finally:
        market.clear_cache()

def test_market_layout_survives_wider_fonts(seeded, qapp, monkeypatch) -> None:
    """**换一台机器（字体更宽）也不能把数字挤掉** —— 这条是给 CI 那次红补的回归。

    为什么要有它：`test_overview_uses_three_columns_on_a_150pct_scaled_screen` 在 Linux
    全绿、到 Windows CI 上红了 5 条 —— 同一段中文在 Windows 字体下更宽（实测那一格需要
    338px，只分到 231px），于是合成文本被自己的列宽截掉、还把整页最小宽度顶到 714 > 视口 706。
    根因是**布局对字体宽度敏感**，而 Linux 上测不出来。2026-09-17 起名称还都**加粗**了
    （比原来更宽一点），这条回归更要紧。

    这里把应用字体放大（模拟更宽的字形）后重新检查三条硬要求：
    ① 小条目的数值不被自己的宽度截掉；② 页面最小宽度不超过视口（不出现横向滚动条）；
    ③ 四块的结构不变。字体是最容易被忽略的变量，所以用测试钉住它，而不是等 CI 再红一次。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFont

    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    original = qapp.font()
    wider = QFont(original.family(), original.pointSize() + 3)
    opened: list = []
    try:
        monkeypatch.setattr(ui_app, "_available_geometry", lambda: QRect(0, 0, 960, 900))
        qapp.setFont(wider)
        win = ui_app.MainWindow(seeded)
        win.show()
        qapp.processEvents()
        _wait_market(win, qapp)
        opened.append(win)
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        for section in win.market_sections.values():
            # 所有条目与小条目的三段文本都得放得下（截断会让 width < sizeHint）
            for entry in section.entries:
                # 指数条目是 名称/点位/涨跌幅 三段；热门板块行是 行业名/涨停数/密度/涨幅 四段
                labels = [entry.name_label]
                labels += [getattr(entry, attr) for attr in
                           ("value_label", "pct_label", "limit_up_label",
                            "density_label", "mom_label")
                           if hasattr(entry, attr)]
                for label in labels:
                    # 容 2px：Qt 的网格分配会四舍五入，1~2px 的差看不出区别；
                    # 真正要拦的是"差一截"（CI 那次是差 107px）。
                    assert label.width() >= label.sizeHint().width() - 2, \
                        (entry.name_label.text(), label.text())
            for name, item in section.stats.items():
                assert item.value_label.width() >= item.value_label.sizeHint().width(), name
                # 名称加粗之后更容易被挤 —— 它也得完整显示
                assert item.title_label.width() >= item.title_label.sizeHint().width() - 2, name
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()
        assert win.market_scroll.horizontalScrollBar().isVisible() is False
        assert win._market_columns >= 1
        assert list(win.market_sections) == list(ui_app.MARKET_SECTION_TITLES)
        assert all(section.isVisible() for section in win.market_sections.values())
    finally:
        qapp.setFont(original)
        market.clear_cache()


@pytest.fixture()
def screen_window(seeded, qapp, monkeypatch):
    """按**模拟的屏幕可用区域**开窗口。

    为什么要模拟：离屏平台报的"真屏幕"是 800×800，只测它测不出 1366×768 / 1024×600
    这两档笔记本屏幕上的行为 —— 而那正是用户反馈"一打开最下边看不见"的场景。
    这里把 `_available_geometry` 换成一个假的可用区域，走的还是真实代码路径。
    """
    from PySide6.QtCore import QRect

    from laoa_trader.ui import app as ui_app

    opened: list = []

    def _open(width: int, height: int):
        monkeypatch.setattr(
            ui_app, "_available_geometry", lambda: QRect(0, 0, width, height)
        )
        win = ui_app.MainWindow(seeded)
        win.show()
        qapp.processEvents()
        _wait_market(win, qapp)          # 启动那一轮后台取数先落地，别跟断言抢数
        opened.append(win)
        return win

    yield _open

    for win in opened:
        win._timer.stop()
        win._market_timer.stop()
        win._auction_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        _wait_market(win, qapp)
        win.tray.hide()
        win.deleteLater()
    qapp.processEvents()


def _bottom_of(widget, window) -> int:
    """控件换算到窗口坐标后的**底边** y（用来断言"没被窗口下沿切掉"）。"""
    from PySide6.QtCore import QPoint

    return widget.mapTo(window, QPoint(0, 0)).y() + widget.height()


def test_fit_window_geometry_rules() -> None:
    """尺寸规则：`min(1120, 可用宽-40) × min(760, 可用高-40)`、最小尺寸收到 760×540、按可用区域居中。"""
    from PySide6.QtCore import QRect

    from laoa_trader.ui import app as ui_app

    # 用户那台：2160×1440 物理分辨率 + 150% 缩放 → 逻辑 1440×960，扣掉任务栏约 900
    size, minimum, pos = ui_app.fit_window_geometry(QRect(0, 0, 1440, 900))
    assert (size.width(), size.height()) == (1120, 760)
    assert (minimum.width(), minimum.height()) == (760, 540)
    assert (pos.x(), pos.y()) == ((1440 - 1120) // 2, (900 - 760) // 2)
    assert size.height() <= 900 and minimum.height() <= 900        # 整窗在可用区域里

    # 逻辑宽度只有 960（更苛刻的那种 DPI 组合）：默认尺寸跟着让位，最小尺寸仍然 760×540
    size, minimum, pos = ui_app.fit_window_geometry(QRect(0, 0, 960, 900))
    assert (size.width(), size.height()) == (920, 760)
    assert (minimum.width(), minimum.height()) == (760, 540)
    assert (pos.x(), pos.y()) == ((960 - 920) // 2, (900 - 760) // 2)
    assert minimum.width() <= size.width() and minimum.height() <= size.height()

    # 常见笔记本档
    size, minimum, _ = ui_app.fit_window_geometry(QRect(0, 0, 1280, 720))
    assert (size.width(), size.height()) == (1120, 680)
    assert minimum.width() <= size.width() and minimum.height() <= size.height()

    # 极窄的屏上"最小"与"默认"会撞到一起：必须收敛成 `最小 <= 默认`，
    # 否则窗口一开出来就是最小尺寸、还一点都拖不大
    size, minimum, _ = ui_app.fit_window_geometry(QRect(0, 0, 800, 600))
    assert (size.width(), size.height()) == (760, 560)
    assert minimum.width() <= size.width() and minimum.height() <= size.height()

    # 多显示器：可用区域不在原点时，居中要相对**那块**屏幕算
    _, _, pos = ui_app.fit_window_geometry(QRect(1920, 0, 1440, 900))
    assert pos.x() == 1920 + (1440 - 1120) // 2
    # 传进来的不是可用区域（拿不到屏幕时上层可能传了奇怪的东西）→ 返回 None，不抛
    assert ui_app.fit_window_geometry("不是矩形") is None


@pytest.mark.parametrize(
    "avail",
    [
        (960, 900),     # ≈2160×1440 + 150% 缩放（逻辑 1440×960，扣任务栏 ~900）—— 用户那台
        (1280, 720),    # 另一档常见笔记本（也是一堆 150% 缩放机器缩放后的逻辑尺寸）
    ],
)
def test_window_fits_the_screen_and_keeps_the_bottom_row(
    screen_window, qapp, avail
) -> None:
    """**核心回归**：窗口不许比屏幕可用区域大，最底部那一行必须留在窗口内。

    这两档就是用户真实会遇到的尺寸：150% 缩放把逻辑可用区域压到 900 高左右，
    写死 `resize(1120, 720)`（再加标题栏）正好把底边顶出屏幕 ——
    用户看到的症状就是"一打开，最下边看不见"。
    其中"`minimumSizeHint` 不超出可用区域"这条最关键：最小尺寸一旦超标，
    `resize()` 会被 Qt 顶回去，窗口就永远缩不进屏幕里。
    """
    width, height = avail
    win = screen_window(width, height)
    qapp.processEvents()

    # 1) 尺寸按可用区域算，且不超过它（写死 1120×720 的实现会在这里红）
    assert win.width() == min(1120, width - 40)
    assert win.height() == min(760, height - 40)
    assert win.width() <= width and win.height() <= height
    # 2) 最小尺寸也收住了：否则用户拖不动窗口，底边永远在屏幕外
    assert win.minimumSizeHint().width() <= width
    assert win.minimumSizeHint().height() <= height
    assert win.minimumSize().width() <= win.width()
    assert win.minimumSize().height() <= win.height()
    # 3) 居中
    assert abs(win.x() - (width - win.width()) // 2) <= 2
    assert abs(win.y() - (height - win.height()) // 2) <= 2
    # 4) 最下面的东西（页脚 / 进度条 / 页签区 / 出错提示）都在窗口里
    for widget in (win.status_label, win.progress, win.tabs, win.market_hint,
                   win.market_footer):
        assert _bottom_of(widget, win) <= win.height(), widget
    # 5) 横向不溢出：内容最小宽度放得进视口，也不出现横向滚动条
    assert win.market_scroll.horizontalScrollBar().isVisible() is False
    assert win.market_content.minimumSizeHint().width() \
        <= win.market_scroll.viewport().width()


def test_window_falls_back_to_the_default_size_without_screen(seeded, qapp,
                                                              monkeypatch) -> None:
    """拿不到屏幕信息（极端环境）→ 退回 1120×760，而不是开出一个 0×0 的窗口。"""
    from laoa_trader.ui import app as ui_app

    monkeypatch.setattr(ui_app, "_available_geometry", lambda: None)
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert ui_app.WINDOW_PREFERRED_SIZE == (1120, 760)
        assert (win.width(), win.height()) == (1120, 760)
    finally:
        win.scheduler.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


def test_window_stays_usable_when_dragged_down_to_the_minimum(
    screen_window, qapp
) -> None:
    """**用户那台机器上的关键一条**：把窗口拖到 800×560 / 760×540 也还能用。

    为什么要单独测：最小尺寸如果被"股票池表格 9 列"或"概览三组条目"顶大，
    窗口就缩不进去；缩进去之后又可能出现"底部被切 / 横向溢出"。
    这里两件事一起钉：最小宽度确实能到 760、缩小后页脚还在窗口里、
    概览内容与股票池表格都不会把窗口顶宽（表格自己横向滚动）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractScrollArea

    from laoa_trader import market

    market.clear_cache()
    win = screen_window(960, 900)          # ≈ 用户那台（150% 缩放）
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        assert win.minimumSize().width() <= 760        # 真的能拖到 760 宽
        assert win.minimumSize().height() <= 540

        for size in ((800, 560), (760, 540)):
            win.resize(*size)
            qapp.processEvents()
            assert (win.width(), win.height()) == size       # 没被最小尺寸顶回去
            # 最底部那一行（页脚）与进度条都还在窗口里
            for widget in (win.progress, win.tabs, win.market_footer):
                assert _bottom_of(widget, win) <= win.height(), (size, widget)
            # 概览页不横向溢出（内容最小宽度放得进视口，滚动条关着也看得全）
            assert win.market_content.minimumSizeHint().width() \
                <= win.market_scroll.viewport().width(), size
            assert win.market_scroll.horizontalScrollBar().isVisible() is False

        # 自选股池页：6 列表格不许把窗口顶宽 —— 它自己在内部横向滚动
        win.tabs.setCurrentWidget(_tab_page(win, ui_app.TAB_WATCH))
        qapp.processEvents()
        assert win.pool_table.minimumSizeHint().width() <= win.pool_table.width()
        # 表格的横向滚动条是"需要时出现"（放不下就自己滚，而不是把窗口顶宽）
        assert win.pool_table.horizontalScrollBarPolicy() \
            != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert isinstance(win.pool_table, QAbstractScrollArea)
        assert win.pool_table.viewport().width() <= win.width()
        assert win.minimumSizeHint().width() <= 760
        # 持仓监控页：8 列表格同理
        win.tabs.setCurrentWidget(_tab_page(win, ui_app.TAB_POSITION))
        qapp.processEvents()
        assert win.position_table.minimumSizeHint().width() <= win.position_table.width()
        assert win.minimumSizeHint().width() <= 760
    finally:
        market.clear_cache()


def test_sector_rank_tables_are_sorted_by_change_pct_with_local_limit_ups() -> None:
    """`ui_app.sector_rank_tables()`：**按涨幅**排上涨前五 / 下跌前五，
    涨停数量用**本地那一套口径**（`pool.hot_industries` 的 `limit_up`），主力净额单位是元。

    2026-09-17（用户要求）：热门板块改成两张表 —— 上涨前五按涨幅降序、下跌前五按涨幅升序，
    各 5 行；涨停数量与选股用的是同一套口径（不另起一套）。
    """
    from laoa_trader.ui import app as ui_app

    rank = [
        {"name": "半导体", "pct": 3.21, "main_net": 1.23e9},
        {"name": "银行", "pct": 0.55, "main_net": -4.5e8},
        {"name": "白酒", "pct": -2.40, "main_net": -9.9e8},
        {"name": "煤炭", "pct": -1.05, "main_net": -3.3e8},
        {"name": "地产", "pct": -3.60, "main_net": -1.4e9},
        {"name": "医药", "pct": 0.20, "main_net": 5.0e7},
        {"name": "证券", "pct": 1.10, "main_net": 2.0e8},
    ]
    industries = {
        "半导体": {"limit_up": 5, "density": 0.10, "mom": 0.03},
        "银行": {"limit_up": 0, "density": 0.0, "mom": -0.01},
        "白酒": {"limit_up": 2, "density": 0.2, "mom": 0.01},
    }
    up, down = ui_app.sector_rank_tables(rank, industries)
    # 上涨前五 = 涨幅降序前 5（各 5 行）
    assert [row["name"] for row in up] == ["半导体", "证券", "银行", "医药", "煤炭"]
    assert [row["pct"] for row in up] == [3.21, 1.10, 0.55, 0.20, -1.05]
    # 下跌前五 = 涨幅升序前 5
    assert [row["name"] for row in down] == ["地产", "白酒", "煤炭", "医药", "银行"]
    assert [row["pct"] for row in down] == [-3.60, -2.40, -1.05, 0.20, 0.55]
    assert len(up) == len(down) == ui_app.SECTOR_TOP == 5
    # 涨停数量 = 本地口径（不在本地行业表里的板块 → None，界面画 `—`，**不是 0**）
    assert [row["limit_up"] for row in up] == [5, None, 0, None, None]
    # 主力净额是**元**（界面 ÷1e8 显示成"亿"），原样带着符号
    assert up[0]["main_net"] == 1.23e9
    assert up[0]["main_net"] / ui_app.SECTOR_NET_UNIT == pytest.approx(12.30)

    # 没有涨幅的行排不进前五（不拿 0 顶替）；同涨幅按名字定序 → 顺序稳定
    rank2 = [{"name": "乙", "pct": None}, {"name": "甲", "pct": 1.0},
             {"name": "丙", "pct": 1.0}]
    up2, _ = ui_app.sector_rank_tables(rank2, {})
    assert [row["name"] for row in up2] == ["丙", "甲"]      # 稳定序：同名次按名字排
    # 空输入不炸（取不到板块榜 / 本地库还没建好时就是这个入参）
    assert ui_app.sector_rank_tables(None, None) == ([], [])
    assert ui_app.sector_rank_tables([], {}) == ([], [])


def test_local_sector_tables_fall_back_and_say_so() -> None:
    """取不到板块榜时的**本地兜底**：涨幅 = 近 5 日等权涨幅（**比例 → 百分数**），
    主力净额一律 None（界面 `—`）。用户要的东西不许静默消失 —— 缺哪一项都要看得见。"""
    from laoa_trader.ui import app as ui_app

    industries = {
        "半导体": {"limit_up": 5, "density": 0.10, "mom": 0.0321},
        "银行": {"limit_up": 0, "density": 0.0, "mom": -0.0105},
        "煤炭": {"limit_up": 1, "density": 0.05, "mom": None},     # 缺动量 → 排不进前五
    }
    up, down = ui_app.local_sector_tables(industries)
    assert [row["name"] for row in up] == ["半导体", "银行"]
    # `mom` 是比例（0.0321 = 3.21%），这一列统一成**百分数**：×100 只做一次
    assert up[0]["pct"] == pytest.approx(3.21)
    assert [row["name"] for row in down] == ["银行", "半导体"]
    assert all(row["main_net"] is None for row in up + down)
    assert all(row["mom"] is not None for row in up + down)
    assert ui_app.local_sector_tables({}) == ([], [])
    # 页内说明由 `sector_payload()` 拼（下一节那条用例逐句钉住）


def test_sector_payload_uses_the_sector_module_when_available(monkeypatch) -> None:
    """有 `data/sectors.py` 时走板块榜；取数失败/模块缺失时退回本地口径**并在页面上说明**。

    这里用一个**假的 sectors 模块**（真实契约：`fetch_sector_rank()` → list[dict]）：
    另一条路径（模块缺失 / 它抛异常 / 返回空）也都走一遍。
    """
    from laoa_trader.ui import app as ui_app

    class FakeSectors:
        def __init__(self, rows=None, boom=False):
            self.rows = rows if rows is not None else [
                {"name": "半导体", "pct": 3.21, "main_net": 1.23e9},
                {"name": "银行", "pct": -0.55, "main_net": -4.5e8},
            ]
            self.boom = boom

        def fetch_sector_rank(self, *args, **kwargs):
            if self.boom:
                raise RuntimeError("板块榜炸了")
            return self.rows

    industries = {"半导体": {"limit_up": 5, "mom": 0.03}}

    monkeypatch.setattr(ui_app, "sectors_module", lambda: FakeSectors())
    payload = ui_app.sector_payload(object(), industries)
    assert payload["source"] == "sectors"
    # 表里只有两行数据 → 两张表都装着这两行（上涨前五按涨幅降序、下跌前五升序）
    assert [row["name"] for row in payload["up"]] == ["半导体", "银行"]
    assert [row["name"] for row in payload["down"]] == ["银行", "半导体"]
    assert "当天" in payload["note"] and "亿" in payload["note"]
    assert payload["up"][0]["limit_up"] == 5            # 涨停数量来自本地口径

    # 模块缺失 → 本地兜底 + 写明原因
    monkeypatch.setattr(ui_app, "sectors_module", lambda: None)
    payload = ui_app.sector_payload(object(), industries)
    assert payload["source"] == "local"
    assert "未就绪" in payload["note"]
    assert "近 5 日" in payload["note"] and "主力净额取不到" in payload["note"]
    assert payload["up"][0]["main_net"] is None

    # 取数抛异常 → 也是本地兜底（原因写清是"取不到"）
    monkeypatch.setattr(ui_app, "sectors_module", lambda: FakeSectors(boom=True))
    payload = ui_app.sector_payload(object(), industries)
    assert payload["source"] == "local"
    assert "取不到" in payload["note"]

    # 返回空数据 → 同样兜底
    monkeypatch.setattr(ui_app, "sectors_module", lambda: FakeSectors(rows=[]))
    payload = ui_app.sector_payload(object(), industries)
    assert payload["source"] == "local"
    assert "没返回数据" in payload["note"]


def test_sectors_module_import_is_defensive(monkeypatch) -> None:
    """`sectors_module()`：拿不到板块榜模块时返回 None（**不抛**）—— 概览页是第一屏，
    一个 import 失败不能让整个窗口打不开。

    怎么模拟"模块拿不到"：把 `laoa_trader.data.sectors` 从父包里摘掉、并往 `sys.modules`
    里塞一个 `None`（Python 对"`sys.modules` 里是 None"的子模块会抛 `ImportError`）——
    与"模块文件还不存在 / 写坏了"是同一条代码路径（`except Exception` → 返回 None）。
    """
    import sys

    import laoa_trader.data as data_pkg

    from laoa_trader.ui import app as ui_app

    assert ui_app.sectors_module() is not None          # 正常情况下当然拿得到
    monkeypatch.delattr(data_pkg, "sectors", raising=False)
    monkeypatch.setitem(sys.modules, "laoa_trader.data.sectors", None)
    assert ui_app.sectors_module() is None


def test_market_hot_block_is_two_real_tables_with_clear_headers(market_window, qapp) -> None:
    """「热门板块」块 = **上涨前五 / 下跌前五两张表**，每张表的表头就是用户给定的四列。

    用户原话："把上涨前 5 和下跌前 5 都标出来。现在的数据都没写什么意思，
    改成以下表格标上名称。" —— 所以这条重点钉**表头文字**与**两张表都在**。
    口径：涨停数量来自"选股用的那一套"（`pool.hot_industries`）。
    """
    from laoa_trader import market, pool
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    win = market_window
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        section = win.market_sections[ui_app.MARKET_SECTION_HOT]
        assert section.isVisible() is True
        assert section.title_label.text() == ui_app.MARKET_SECTION_HOT
        assert list(section.tables) == [ui_app.SECTOR_UP_TITLE, ui_app.SECTOR_DOWN_TITLE]
        assert (ui_app.SECTOR_UP_TITLE, ui_app.SECTOR_DOWN_TITLE) == ("上涨前五", "下跌前五")
        for title, block in section.tables.items():
            # 表头就是用户给的那四个字（"现在的数据都没写什么意思" → 现在写清了）
            assert _sector_headers(win, title) == ["板块名称", "涨停数量", "涨幅", "主力净额"]
            assert block.title_label.text() == title
            # 每张表各 5 行（本次数据只有 2 个行业 → 就 2 行；上限是 5）
            assert 0 < block.table.rowCount() <= ui_app.SECTOR_TOP == 5
            # 四列的表头 tooltip 都写清了口径（列头只有四个字，放不下解释）
            for column in range(4):
                tip = block.table.horizontalHeaderItem(column).toolTip()
                assert len(tip) >= 10, (title, column)
        # 涨停数量用的是**本地那一套**（`pool.hot_industries`），不是另起一套
        expected = pool.hot_industries(win.cfg.db_path, top=ui_app.MARKET_HOT_TOP)
        up_rows = _sector_table_rows(win, ui_app.SECTOR_UP_TITLE)
        for row in up_rows:
            assert row[0] in expected
            assert row[1] == str(expected[row[0]]["limit_up"])
        # 本地口径下涨幅 = 近 5 日等权涨幅（比例 → 百分数），页内说明写清了这一点
        assert up_rows[0][2] == f"{expected[up_rows[0][0]]['mom'] * 100:+.2f}%"
    finally:
        market.clear_cache()


def test_market_hot_block_explains_itself_when_local_data_is_missing(
    market_window, qapp, monkeypatch
) -> None:
    """本地没有涨停池数据 + 板块榜也取不到 → 两张表空着 + **页面上写出怎么补**。

    不能只留一个空块：用户会以为"这一块本来就不显示东西"，而实际是数据还没下。
    """
    from laoa_trader import market, pool
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        monkeypatch.setattr(pool, "hot_industries", lambda *a, **k: {})
        monkeypatch.setattr(ui_app, "sectors_module", lambda: None)
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        section = market_window.market_sections[ui_app.MARKET_SECTION_HOT]
        assert all(block.table.rowCount() == 0 for block in section.tables.values())
        assert section.placeholder_label.isVisible() is True
        assert section.placeholder_label.text() == market.DASH
        # 表头仍然在（空表也要看得出这四列是什么）
        assert _sector_headers(market_window, ui_app.SECTOR_UP_TITLE) \
            == list(ui_app.SECTOR_TABLE_HEADERS)
        assert market_window.market_hint.isVisible() is True
        assert "热门板块" in market_window.market_hint.text()
        assert "刷新数据" in market_window.market_hint.text()
    finally:
        market.clear_cache()


def test_overview_columns_fit_the_screen_without_clipping(
    screen_window, qapp
) -> None:
    """≈用户那台机器（逻辑可用 960×900 → 窗口 920×760）：概览页**条目不被截、也不横向溢出**。

    为什么不写死"正好 3 列"：列数现在是**按当前字体实测**算出来的
    （`_market_item_need()`）—— 同一段中文在 Windows 的 CJK 回退字体下更宽，
    写死 3 列就会"每列都不够宽、文字被裁、整页出现横向滚动条"（CI 实测过：
    整页最小宽度 1160 > 视口 866）。所以这里钉的是**不变的那条要求**：
    每个数字都完整显示、内容不比视口宽、网格与报出来的列数一致。
    """
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    win = screen_window(960, 900)
    try:
        assert (win.width(), win.height()) == (920, 760)
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        columns = win._market_columns
        assert 1 <= columns <= ui_app.MARKET_MAX_COLUMNS
        # 列数就得是"按实测宽度算出来"的那个值（不是拍脑袋的常量）
        avail = max(win.market_scroll.width(), win.market_page.width()) \
            - 2 * ui_app.PAGE_MARGINS[0]
        assert columns == max(1, min(ui_app.MARKET_MAX_COLUMNS,
                                     avail // win._market_item_need()))
        codes = [c for c, *_ in MARKET_EXPECTED_ENTRIES[ui_app.MARKET_SECTION_WIDE]]
        rows = _grid_rows(win.market_sections[ui_app.MARKET_SECTION_WIDE])
        assert [c for row in rows for c in row] == codes          # 一个都没少
        assert all(len(row) <= columns for row in rows)           # 每行不超过列数
        assert len(rows) == -(-len(codes) // columns)             # 行数正好排满
        sentiment_rows = _grid_rows(win.market_sections[ui_app.MARKET_SECTION_SENTIMENT])
        assert all(len(row) <= columns for row in sentiment_rows)
        # 7 个小条目最多 7 列（宽屏一行 7 个），窄屏按算出来的列数往下排
        assert 1 <= win._market_stat_columns <= ui_app.MARKET_STAT_MAX_COLUMNS == 7
        # 内容不比视口宽 → 不会出现横向滚动条
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()
        assert win.market_scroll.horizontalScrollBar().isVisible() is False

        # 每一项三个标签都完整显示：宽度不少于自己的"需要宽度"（截断/挤压会小于它）
        for entry in win.market_entries:
            assert entry.name_label.text() not in ("", market.DASH)
            assert entry.name_label.width() >= entry.name_label.sizeHint().width(), \
                entry.thscode
            for label in (entry.value_label, entry.pct_label):
                assert label.width() >= label.sizeHint().width(), entry.thscode
        # 小条目的名称与数值都不许被自己的宽度截掉
        for name, item in win.market_sections[
                ui_app.MARKET_SECTION_FLOW].stats.items():
            assert item.value_label.width() >= item.value_label.sizeHint().width(), name
            assert item.title_label.width() >= item.title_label.sizeHint().width(), name
        # 热门板块两张表的每一格文字都不许被自己的列宽截掉
        for title, block in win.market_sections[ui_app.MARKET_SECTION_HOT].tables.items():
            for row in range(block.table.rowCount()):
                for column in range(block.table.columnCount()):
                    item = block.table.item(row, column)
                    need = block.table.fontMetrics().horizontalAdvance(item.text())
                    assert block.table.columnWidth(column) >= need, (title, row, column)
        # 一行页脚照样成立
        assert win.market_footer.height() \
            <= max(win.market_as_of_label.height(), win.btn_market_refresh.height()) + 8
    finally:
        market.clear_cache()

def _grid_rows(box) -> list[list[str]]:
    """按网格位置把条目排成"行"（每行是若干 thscode）—— 用来断言响应式列数。"""
    placed: dict[int, list[tuple[int, str]]] = {}
    for entry in box.entries:
        row, column, _, _ = box.grid.getItemPosition(box.grid.indexOf(entry))
        placed.setdefault(row, []).append((column, entry.thscode))
    return [[code for _, code in sorted(cells)] for _, cells in sorted(placed.items())]


def test_market_entry_columns_shrink_with_the_window(screen_window, qapp) -> None:
    """窗口变窄 → 每块每行的条目数跟着降（4 → 2 → 5 列），且始终不出现横向溢出。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    win = screen_window(1600, 1000)      # 窗口 = min(1120, 1560) = 1120 宽 → 4 列
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        wide = win.market_sections[ui_app.MARKET_SECTION_WIDE]
        codes = [code for code, *_ in MARKET_EXPECTED_ENTRIES[ui_app.MARKET_SECTION_WIDE]]
        wide_columns = win._market_columns

        # 缩到 800 宽（现在最小宽度是 760，缩得下去）：列数只会掉、不会涨，条目往下排
        win.resize(800, win.height())
        qapp.processEvents()
        assert win._market_columns <= wide_columns
        assert [c for row in _grid_rows(wide) for c in row] == codes
        assert win.market_scroll.horizontalScrollBar().isVisible() is False
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()

        # 再放大：列数涨回去（窗口越宽，一行放得下越多条目）
        win.resize(1500, win.height())
        qapp.processEvents()
        assert win._market_columns >= wide_columns
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()
        # 小条目按**自己的列数**排（与指数条目那套分开算）：宽屏一行 7 个，
        # 窗口一窄就往下排 —— 位置得与算出来的列数一致，且 7 条一个都不少
        stats = win.market_sections[ui_app.MARKET_SECTION_FLOW].stats
        grid = win.market_sections[ui_app.MARKET_SECTION_FLOW].stats_grid
        stat_columns = win._market_stat_columns
        positions = [grid.getItemPosition(grid.indexOf(item))[:2]
                     for item in stats.values()]
        assert positions == [(index // stat_columns, index % stat_columns)
                            for index in range(len(stats))]
        assert len(stats) == 7
    finally:
        market.clear_cache()

def test_pool_row_tooltip_marks_open_only_strategy(pool_window, qapp, monkeypatch) -> None:
    """「依赖开盘」的标的：行的 tooltip 里带 `（依赖开盘）` 与那段解释（不是只写在文档里）。

    这个标记原来画在「来源策略」列与卡片上；新表的「来源」列给的是**组名**，
    所以具体策略名与证据标记一起收进 tooltip —— 证据不能从界面上消失。
    """
    from laoa_trader import pool as pool_mod

    rows = [
        {"symbol": "600002", "name": "半导体甲", "strategy": "DryUpExpansionStrategy",
         "strategies": "DryUpExpansionStrategy", "score": 2.0, "reason": "地量后放量",
         "label": "地量后放量变盘", "source_label": "策略·地量后放量变盘",
         "industry": "半导体", "note": "", "group": "short", "group_label": "短线·T+3",
         "horizon": 3, "source": "策略", "evidence": "open_only",
         "evidence_text": "（依赖开盘）",
         "is_limit_up": False, "continue_day_text": "", "limit_up_reason": ""},
        {"symbol": "600001", "name": "浦发样本", "strategy": "ReversalStrategy",
         "strategies": "ReversalStrategy", "score": 1.0, "reason": "短期反转",
         "label": "短期反转", "source_label": "策略·短期反转", "industry": "银行",
         "note": "", "group": "short", "group_label": "短线·T+3",
         "horizon": 3, "source": "策略", "evidence": "proven", "evidence_text": "",
         "is_limit_up": False, "continue_day_text": "", "limit_up_reason": ""},
    ]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    rows_by_symbol = {_symbols_of(window.pool_table)[i]: i
                      for i in range(window.pool_table.rowCount())}
    tip = window.pool_table.item(rows_by_symbol["600002"], 0).toolTip()
    assert "来源：策略·地量后放量变盘（依赖开盘）" in tip
    assert "组别：短线·T+3（T+3）" in tip
    assert "策略照常推送" in tip and "push_only_proven" in tip   # 提示是"可以收紧"，不是"已经收紧"
    # 有边际证据的那只**不带**标记（tip 里仍然写策略名，供用户核对是哪条策略选的）
    plain = window.pool_table.item(rows_by_symbol["600001"], 0).toolTip()
    assert "来源：策略·短期反转" in plain
    assert "依赖开盘" not in plain


# ── 两张表「提醒」列的数据源（`intraday.alerts_today_by_symbol`）──
#
# 为什么放在界面测试文件里：它是这两列的**唯一数据源**（`KIND_LABELS` 的短标签、
# tooltip 的整句话都从它出来），换掉它 = 两列的语义就变了。


# ── 两张表新增的「市值」「换手」列 / 「监控开关」列 / 去掉 `*` ──
#
# 为什么这几条放在界面测试里：这三个改动都是**用户直接提的界面问题**
# （2026-09-17）："两张表加市值换手"、"提醒列改成监控开关、点一下就能切换"、
# "把 `*` 号去掉"（用户把它误当成监控状态标记）。断言的就是他在界面上看到的东西。


def _inject_snapshot(window, symbol: str, **extra) -> None:
    """给某只票塞一份**实时快照**（含市值/换手两项），绕开网络。"""
    quote = {"symbol": symbol, "price": 3.20, "pct": 0.63, "at": time.time(),
             "source": "public"}
    quote.update(extra)
    window.quotes.apply({symbol: quote})


def test_tables_show_circ_mktcap_and_turnover_rate(window, seeded, qapp) -> None:
    """两张表的「市值」「换手」：取快照里的**流通市值（亿）**与**实时换手率（%）**；
    没有快照时是 `—`（**绝不显示 0** —— 0 亿市值/0% 换手是真实存在的值）。"""
    from laoa_trader import market

    # 600001 只是持仓；把它也加成自选，池子表里才有它这一行
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="")
    window._pool_signature = None
    cap_col = ui_app.WATCH_HEADERS.index("市值")
    turn_col = ui_app.WATCH_HEADERS.index("换手")
    # 持仓表的两列同名，但下标不同（前面多了成本价那一列）
    p_cap = ui_app.POSITION_HEADERS.index("市值")
    p_turn = ui_app.POSITION_HEADERS.index("换手")
    assert (cap_col, turn_col) == (3, 4)
    assert (p_cap, p_turn) == (4, 5)

    # ① 没有实时快照 → 两列都是 `—`（不是 0），tooltip 说清"为什么是 —"
    window._pool_signature = None
    window._position_signature = None
    window._refresh_pool_table()
    window._refresh_positions()
    qapp.processEvents()
    row = _symbols_of(window.pool_table).index("600001")
    cells = _row_cells(window.pool_table, row)
    assert cells[cap_col] == market.DASH and cells[turn_col] == market.DASH
    assert "不是 0" in window.pool_table.item(row, cap_col).toolTip()
    pcells = _row_cells(window.position_table, 0)
    assert pcells[p_cap] == market.DASH and pcells[p_turn] == market.DASH

    # ② 有快照（含这两项）→ 照实显示：市值单位亿、换手带 %
    _inject_snapshot(window, "600001", circ_mktcap=456.78, turnover_rate=1.23)
    window._pool_signature = None
    window._position_signature = None
    window._refresh_pool_table()
    window._refresh_positions()
    qapp.processEvents()
    assert window.pool_table.item(row, cap_col).text() == "456.78亿"
    assert window.pool_table.item(row, turn_col).text() == "1.23%"
    assert window.position_table.item(0, p_cap).text() == "456.78亿"
    assert window.position_table.item(0, p_turn).text() == "1.23%"
    # 表头写了口径（"亿"/"%"），tooltip 还点明"是流通市值，不是总市值"
    assert "流通市值" in window.pool_table.horizontalHeaderItem(cap_col).toolTip()
    assert "换手率" in window.pool_table.horizontalHeaderItem(turn_col).toolTip()
    assert "不是总市值" in window.position_table.item(0, p_cap).toolTip()


def test_tables_snapshot_columns_are_dash_not_zero_when_only_one_is_missing(
    window, seeded, qapp
) -> None:
    """快照在、但**没给这两项**（来源不提供 / 契约还没接上）→ 还是 `—`，不是 0。

    这是最容易被写错的一处：`float(None)` 会炸，`float(0)` 会显示成 0.00亿/0.00%，
    而"0 亿市值"会被用户读成"这只票没人要"。
    """
    from laoa_trader import market

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="")
    cap_col = ui_app.WATCH_HEADERS.index("市值")
    _inject_snapshot(window, "600001")          # 只有价与涨幅，没有市值/换手
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    row = _symbols_of(window.pool_table).index("600001")
    assert window.pool_table.item(row, cap_col).text() == market.DASH
    assert window.pool_table.item(
        row, ui_app.WATCH_HEADERS.index("换手")).text() == market.DASH
    assert "0.00亿" not in "".join(_row_cells(window.pool_table, row))


def test_monitor_column_click_toggles_position_monitor(window, seeded, qapp) -> None:
    """「监控开关」列：**点一下就能切换**（与右键菜单调的是同一个方法）。"""
    from laoa_trader import intraday
    from laoa_trader.data import storage as st

    column = ui_app.POSITION_MONITOR_COLUMN
    table = window.position_table
    window.tabs.setCurrentWidget(_tab_page(window, ui_app.TAB_POSITION))
    qapp.processEvents()
    assert table.item(0, column).text() == ui_app.MONITOR_ON_TEXT

    window._on_table_cell_clicked(table, 0, column)          # ← 单击那一格
    qapp.processEvents()
    assert table.item(0, column).text() == ui_app.MONITOR_OFF_TEXT
    with st.connect(seeded.db_path) as conn:
        assert int(st.load_positions(conn, open_only=False)["600001"]["monitor"]) == 0
    # 真实作用：不再进做T/盘中提醒的观察面（与右键【关闭监控】完全一致）
    assert list(intraday.held_positions(seeded.db_path)) == []
    assert "关闭监控" in window.status_label.fullText()

    window._on_table_cell_clicked(table, 0, column)          # 再点一次 → 打开
    qapp.processEvents()
    assert table.item(0, column).text() == ui_app.MONITOR_ON_TEXT
    assert list(intraday.held_positions(seeded.db_path)) == ["600001"]
    # 关掉的那一格会变灰（一眼看得出这一行是停用的）
    window._on_table_cell_clicked(table, 0, column)
    qapp.processEvents()
    from PySide6.QtGui import QColor

    assert table.item(0, column).foreground().color().name() \
        == QColor(Qt.GlobalColor.gray).name()        # 关掉的那一格变灰


def test_monitor_column_click_toggles_watchlist_and_explains_for_strategy_rows(
    window, seeded, qapp
) -> None:
    """自选行点一下就能开关监控；**策略/公式选中的票不是自选**，点了给一句指路的话
    （不静默失败 —— 与右键菜单里那一项灰掉 + tooltip 说明是同一个判据）。"""
    from laoa_trader.data import storage as st

    column = ui_app.WATCH_MONITOR_COLUMN
    table = window.pool_table
    with st.connect(seeded.db_path) as conn:
        st.upsert_watchlist(conn, "600001", name="低价样本", note="")
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = {_symbols_of(table)[i]: i for i in range(table.rowCount())}
    assert set(rows) == {"600002", "600001"}

    # 策略标的（600002）：不能在这里关 → 文字仍是 `开启`，点一下给指路的话
    assert table.item(rows["600002"], column).text() == ui_app.MONITOR_ON_TEXT
    window._on_table_cell_clicked(table, rows["600002"], column)
    qapp.processEvents()
    assert table.item(rows["600002"], column).text() == ui_app.MONITOR_ON_TEXT
    assert "不能在这里单独关掉监控" in window.status_label.fullText()
    assert "策略" in window.status_label.fullText()

    # 自选（600001）：点一下 → `关闭`，写回 watchlist.enabled = 0
    window._on_table_cell_clicked(table, rows["600001"], column)
    qapp.processEvents()
    with st.connect(seeded.db_path) as conn:
        assert st.watchlist_map(conn)["600001"]["enabled"] == 0
    assert table.item(rows["600001"], column).text() == ui_app.MONITOR_OFF_TEXT
    # 再点回来
    window._on_table_cell_clicked(table, rows["600001"], column)
    qapp.processEvents()
    with st.connect(seeded.db_path) as conn:
        assert st.watchlist_map(conn)["600001"]["enabled"] == 1


def test_monitor_column_keeps_the_alert_content_in_the_tooltip(window, seeded, qapp) -> None:
    """「提醒」列改成监控开关之后，**提醒内容一点没丢**：整句话都在这一格的 tooltip 里。"""
    from laoa_trader.intraday import now_shanghai

    today = now_shanghai().strftime("%Y-%m-%d")
    with storage.connect(seeded.db_path) as conn:
        storage.record_alerts(conn, [
            {"symbol": "600001", "kind": "stop_loss", "price": 2.90,
             "detail": "现价 2.90 ≤ 参考价 3.05 × 0.95"},
        ], today)
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()
    cell = window.position_table.item(0, ui_app.POSITION_MONITOR_COLUMN)
    tip = cell.toolTip()
    assert "监控：已开启" in tip
    assert "触及止损" in tip                      # 原来「提醒」列的短标签
    assert "现价 2.90 ≤ 参考价 3.05 × 0.95" in tip  # 整句话
    assert "时间：" in tip                         # 连时间都在
    assert "点这一格就能切换" in tip               # 用户怎么用这一格，也写在 tooltip 里


def test_price_columns_have_no_star_and_say_local_close_in_the_tooltip(
    window, seeded, qapp
) -> None:
    """本地收盘价**不再加 `*`**（用户把它误读成监控状态标记），改由 tooltip 说清
    "这是本地最新收盘价（MM-DD），不是实时价"。"""
    from laoa_trader.data import storage as st

    with st.connect(seeded.db_path) as conn:
        bar = st.latest_raw_closes(conn, ["600001"])["600001"]
        st.upsert_watchlist(conn, "600001", name="低价样本", note="")
    mmdd = bar["date"][5:]
    window._pool_signature = None
    window._position_signature = None
    window._refresh_pool_table()
    window._refresh_positions()
    qapp.processEvents()
    # 两张表各取 600001 那一行（池子表里自选排在池内行之后）
    rows = {
        window.pool_table: _symbols_of(window.pool_table).index("600001"),
        window.position_table: _symbols_of(window.position_table).index("600001"),
    }
    for table, row in rows.items():
        headers = ([table.horizontalHeaderItem(c).text()
                    for c in range(table.columnCount())])
        price_col = headers.index("现价")            # 两张表的「现价」下标不同（列不一样）
        cells = _row_cells(table, row)
        assert not any("*" in text for text in cells), cells
        tip = table.item(row, price_col).toolTip()
        assert "不是实时价" in tip
        assert mmdd in tip
        assert f"{bar['close']:.2f}" in tip
        # 现价那一格就是本地收盘价本身（没有多一个字符）
        assert table.item(row, price_col).text() == f"{bar['close']:.2f}"


def test_quote_timestamp_text_is_beijing_time() -> None:
    """快照的"取数时刻"按**北京时间**格式化（与机器时区无关）。

    为什么这条要单独测：`time.localtime()` 用的是本机时区 —— 在 UTC 的 CI/服务器上
    会把北京时间 13:46 显示成 05:46，tooltip 里那句"取数时刻"直接就是错的。
    """
    from datetime import datetime, timedelta, timezone

    from laoa_trader.ui import quotes as quotes_mod

    # 北京时间 2026-09-16 13:46:58 = UTC 05:46:58
    at = datetime(2026, 9, 16, 13, 46, 58,
                  tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert quotes_mod.snapshot_time_text(at) == "13:46:58"
    assert quotes_mod.snapshot_date(at) == "2026-09-16"
    # 认不出来的值：给空串（调用方据此不写那一行），不是抛异常
    assert quotes_mod.snapshot_time_text(None) == ""
    assert quotes_mod.snapshot_time_text("昨天") == ""
    assert quotes_mod.snapshot_date("昨天") == ""
    # 过期判定：不是今天的快照一律不算"实时价"。
    # **这两个时间戳必须相对"现在"取**：写死日期的话，这条用例只在写它的那一天是绿的
    assert quotes_mod.is_fresh(None) is False
    assert quotes_mod.is_fresh({"price": 1.0}) is False           # 没有 at
    assert quotes_mod.is_fresh({"price": 1.0, "at": time.time()}) is True
    yesterday = time.time() - 24 * 3600
    assert quotes_mod.snapshot_date(yesterday) != quotes_mod.snapshot_date(time.time())
    assert quotes_mod.is_fresh({"price": 1.0, "at": yesterday}) is False


def test_alerts_today_by_symbol_takes_the_latest_of_the_day(cfg) -> None:
    """同一只票今天多条提醒 → **取最新那条**（表格里只放得下一格）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.record_alerts(conn, [
            {"symbol": "600001", "kind": "break_high", "price": 3.1,
             "detail": "现价 3.10 突破 20 日高点"},
        ], "2026-09-15")
        storage.record_alerts(conn, [
            {"symbol": "600001", "kind": "stop_loss", "price": 2.9,
             "detail": "现价 2.90 ≤ 参考价 3.05 × 0.95"},
        ], "2026-09-15")
    got = intraday.alerts_today_by_symbol(cfg.db_path, "2026-09-15")
    assert set(got) == {"600001"}
    assert got["600001"]["kind"] == "stop_loss"          # 后写的（更晚的）赢
    assert got["600001"]["detail"].startswith("现价 2.90")
    assert got["600001"]["label"] == "🛑 触及止损"        # 顺带带上中文标签
    assert intraday.alert_cell_text(got["600001"]) == "触及止损"
    assert "现价 2.90" in intraday.alert_cell_tooltip(got["600001"])
    assert "时间：" in intraday.alert_cell_tooltip(got["600001"])


def test_alerts_today_by_symbol_ignores_other_days(cfg) -> None:
    """**昨天**的提醒不许出现在今天的表格里（跨天会把旧结论显示成新的）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.record_alerts(conn, [
            {"symbol": "600001", "kind": "stop_loss", "price": 2.9, "detail": "昨天的事"},
        ], "2026-09-14")
    assert intraday.alerts_today_by_symbol(cfg.db_path, "2026-09-15") == {}
    # 到了昨天那一天它当然在（口径是"那一天"，不是"最近"）
    assert "600001" in intraday.alerts_today_by_symbol(cfg.db_path, "2026-09-14")


def test_alerts_today_by_symbol_empty_day_is_empty_dict(cfg) -> None:
    """一条提醒都没有的日子 → 空字典（表格画 `—`，不是报错）。"""
    storage.init_db(cfg.db_path)
    assert intraday.alerts_today_by_symbol(cfg.db_path, "2026-09-15") == {}
    assert intraday.alert_cell_text(None) == ""
    assert "今天还没有这只票的盘中提醒" in intraday.alert_cell_tooltip(None)


# ── 持仓存储的兼容（界面不再收"数量"）──


def test_load_positions_keeps_old_rows_and_new_zero_quantity_rows(cfg) -> None:
    """三种行：老行（有数量）与新行（数量 0）都要读得出来，**显式平仓的不读**。

    改版后界面只记 代码 + 成本价 + 备注，新行 `quantity = 0`；老判据 `quantity > 0`
    会让手工加的持仓**永远不出现** —— 那是"加了却没反应"这类最难查的 bug。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        # ① 老库里的行（有数量）
        storage.upsert_position(conn, "600001", name="老行", quantity=1000, avg_cost=3.0)
        # ② 界面新加的行（没有数量）
        storage.upsert_position(conn, "600002", name="新行", quantity=0, avg_cost=12.0,
                                note="手工记的")
        got = storage.load_positions(conn)
        assert set(got) == {"600001", "600002"}
        assert got["600001"]["quantity"] == 1000
        assert got["600002"]["quantity"] == 0
        assert got["600002"]["monitor"] == 1                 # 默认在监控中

        # ③ 显式平仓的行（quantity = 0 且 closed_at 有值）不读
        conn.execute("UPDATE position SET closed_at = '2026-09-15 15:00:00' "
                     "WHERE symbol = '600002'")
        conn.commit()
        assert set(storage.load_positions(conn)) == {"600001"}
        assert set(storage.load_positions(conn, open_only=False)) == {"600001", "600002"}

        # ④ 老行即便带着 closed_at 也照读（不能动不动丢失老库的行）
        conn.execute("UPDATE position SET closed_at = '2026-09-15 15:00:00' "
                     "WHERE symbol = '600001'")
        conn.commit()
        assert set(storage.load_positions(conn)) == {"600001"}


def test_upsert_position_reopen_clears_closed_mark(cfg) -> None:
    """界面重新添加一只"以前平掉过"的票 → 平仓标记被清掉（否则新行不显示）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_position(conn, "600001", name="甲", quantity=0, avg_cost=3.0)
        conn.execute("UPDATE position SET closed_at = '2026-09-15 15:00:00' "
                     "WHERE symbol = '600001'")
        conn.commit()
        assert storage.load_positions(conn) == {}
        storage.upsert_position(conn, "600001", quantity=0, avg_cost=3.2, reopen=True)
        assert set(storage.load_positions(conn)) == {"600001"}


def test_set_position_monitor_toggles_and_filters_t_observation(cfg) -> None:
    """【关闭监控】置 `monitor = 0`：持仓还在，但不再进做T的观察面。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_position(conn, "600001", name="甲", quantity=0, avg_cost=3.0)
    assert list(intraday.held_positions(cfg.db_path)) == ["600001"]
    with storage.connect(cfg.db_path) as conn:
        assert storage.set_position_monitor(conn, "600001", False) is True
        assert storage.set_position_monitor(conn, "600999", False) is False
    assert intraday.held_positions(cfg.db_path) == {}
    assert set(storage.load_positions(storage.connect(cfg.db_path))) == {"600001"}


def test_pipeline_status_explains_the_push_filter(window, qapp) -> None:
    """状态栏要能说明"跳过几只、为什么"（用户不该以为策略今天没选到票）。"""
    window._on_pipeline_done("开始选股", {
        "picked": 3, "data_date": "2026-09-11",
        "pool": [{"symbol": "600001"}], "picks": 2, "signals": 2,
        "pushed": True, "notify": {"tray": {"kind": "tray", "ok": True}},
        "push_note": "另有 2 只只由「依赖开盘」的策略选出（正 α 只在开盘买口径下存在）",
        "push_skipped_rows": [{"symbol": "600002"}, {"symbol": "600003"}],
    })
    qapp.processEvents()
    text = window.status_label.fullText()
    assert "已跳过 2 只依赖开盘的标的" in text

    # 全部被过滤（压根没推）：状态栏要直接给出原因，而不是那句"未重复推送"
    window._on_pipeline_done("开始选股", {
        "pool": [{"symbol": "600002"}], "picks": 1, "signals": 1, "pushed": False,
        "push_skipped": "另有 1 只只由「依赖开盘」的策略选出（正 α 只在开盘买口径下存在）",
        "push_skipped_kind": "filtered", "push_skipped_rows": [{"symbol": "600002"}],
    })
    qapp.processEvents()
    assert "依赖开盘" in window.status_label.fullText()
    assert "未重复推送" not in window.status_label.fullText()
