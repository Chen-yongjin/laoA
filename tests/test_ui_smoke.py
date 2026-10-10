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
import re
import types
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
    # 数据闸门会（正确地）拒绝匹配，那测的就不是"按钮/定时"而是闸门本身了
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
        # 老配置里仍写着已删除的 Windows 频道键：**加载不报错**，回写时会被抹掉
        'notify_windows = false\n'
        'notify_windows_sound = true\n'
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
    win._flash_timer.stop()
    win.scheduler.stop()
    win.quotes.stop()               # 实时快照的工作线程也要收（不然后台还在飞）
    # 评分线程同理，而且它**更慢**（每只票要读日线 + 评估 17 项 × 2 条公式，
    # 参数里还可能去取一次快照）：收尾时不先停它，窗口已经拆了它还在飞，
    # 实测会让整个 pytest 进程在 teardown 阶段 `Fatal Python error: Aborted`
    win.scores.stop()
    _wait_market(win, qapp)
    worker = getattr(win, "_market_worker", None)
    if worker is not None and worker.isRunning():
        worker.wait(3_000)
    if win.about_dialog is not None:
        win.about_dialog.close()    # 「关于软件」窗口同理
    if win.status_dialog is not None:
        win.status_dialog.close()   # 「状态详情」窗口同理
    # ⚠️ **必须显式 shutdown**：桌宠/消息列表/浮窗都是**没有父窗口**的顶层窗口，
    # `win.deleteLater()` 收不掉它们 —— 每建一次主窗口就留一只桌宠，孤儿越积越多，
    # 最终某一只在事件循环里踩到已析构对象，pytest 中途 `Fatal Python error: Aborted`
    # （2026-09-20 实测：连建三次就留下三只）。这条收尾与生产代码的退出路径是同一个方法。
    win.shutdown()
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


def _settings_group_text(window, title: str) -> str:
    """某一组设置里**用户看得见的静态文字**（标签 + 按钮文字，不含输入框内容）。

    2026-10-11 主人要"数据来源那里只保留同花顺那栏，其它都精简掉"，
    所以断言"某句话不再出现在界面上"时得有个取证入口 ——
    逐控件找比断言"某个属性不存在"更硬：字被换个控件贴上去也会被抓到。
    """
    from PySide6.QtWidgets import QLabel, QAbstractButton

    box = window.settings_sections[title]
    parts = [w.text() for w in box.findChildren(QLabel)]
    parts += [w.text() for w in box.findChildren(QAbstractButton)]
    return "\n".join(parts)


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
    # **五个页签**，顺序就是用户给定的顺序（见 `docs/开发文档.md`）
    assert window.tabs.count() == 5
    assert [window.tabs.tabText(i) for i in range(5)] == list(ui_app.TAB_TITLES)
    # 2026-09-17：第一个页签从「全市概览」改回「大盘概览」（用户要求）——
    # 凡是按标题找页面的地方（截图脚本、`tabs.indexOf`、这条断言）都跟着改
    assert list(ui_app.TAB_TITLES) == ["大盘概览", "自选标的", "持仓监控", "策略筛选",
                                       "系统设置"]
    assert ui_app.TAB_MARKET == "大盘概览"
    # 概览页是第一个页签（启动就停在它上面）：看盘第一眼要扫到
    assert window.tabs.widget(0) is window.market_page
    assert window.tabs.currentWidget() is window.market_page
    # 「策略筛选」页里挂着公式编辑器（页面本身是"标题行 + 编辑器"的外壳）
    assert window.tabs.tabText(window.tabs.indexOf(_tab_page(window, "策略筛选"))) == "策略筛选"
    assert window.formula_page.parent() is _tab_page(window, "策略筛选")

    # 两张表都填好了：池子 1 只（seeded 里 600002 是策略标的）、持仓 1 只
    assert window.pool_table.rowCount() == 1
    assert window.position_table.rowCount() == 1
    # 列就是用户给定的那几列（顺序也一致）
    assert _header_texts(window.pool_table) == list(ui_app.WATCH_HEADERS)
    assert _header_texts(window.position_table) == list(ui_app.POSITION_HEADERS)
    # 池子表格第一行：`名称(代码)` + 来源（seeded 里是 `低价股` 策略选的 → `低价股`）
    assert window.pool_table.item(0, 0).text() == "半导体甲(600002)"
    # 列下标按新表头取（板块/来源两列因为新加的市值/换手往后挪了两格）
    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("板块")).text() == "半导体"
    assert window.pool_table.item(
        0, ui_app.WATCH_HEADERS.index("来源")).text() == "低价股"
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
    而且【下载数据】和【开始筛选】并排 —— 用户点错是迟早的事。
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
        "瞬时消息": lambda: win._set_status("✅ 开始筛选完成：池子 18 只"),
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
    from laoa_trader.log import LOG_NAME

    assert LOG_NAME in text                              # 日志路径要能照着找到
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
    【开始筛选】在「策略筛选」 —— 这条用例就是钉住"一处只管一件事"。
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
    assert win.btn_run.text() == ui_app.BTN_START_TEXT == hints.BTN_RUN_TEXT == "开始筛选"

    # **归属**：数据那三个在「系统设置」页、匹配在「策略筛选」页、两个全局按钮在标题区
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


def test_default_skin_is_applied_when_the_window_opens(window, qapp) -> None:
    """默认皮肤 = **午夜靛紫（深色）**（主人 2026-10-10 挑的）：窗口一建起来就已经是它了。

    其余几套皮肤仍然可用（下拉框里选），所以这里同时钉住"默认是哪一套"这件事 ——
    皮肤换了默认值，界面、下拉框、配置三处必须一致，否则用户看到的是
    "设置里写着别的、界面却是这一套"。
    """
    from laoa_trader.ui import theme as theme_mod

    qss = qapp.styleSheet()
    assert theme_mod.current_theme() == "indigo"
    assert window.cfg.ui_theme == "indigo"
    assert theme_mod.is_dark() is True                    # 深色底（热力图/自绘控件按它换色）
    assert "qlineargradient" in qss                       # 按钮/表头是渐变
    assert "QPushButton#primaryAction" in qss             # 主操作按钮按 objectName 命中
    assert window.btn_run.objectName() == "primaryAction"
    assert theme_mod.INDIGO_COLORS["window"] in qss       # 窗口底：近黑偏紫
    assert theme_mod.INDIGO_COLORS["tab_accent"] in qss   # 强调色进样式表
    # 深色底**不贴**那张浅色拉丝纹理（贴上去就是一块脏斑），背景条改用深色渐变
    assert window.status_area.objectName() == "statusArea"
    assert window.market_footer.objectName() == "marketFooter"
    assert "brushed-metal" not in qss
    assert "background-repeat: repeat-x" not in qss


def test_settings_theme_combo_switches_and_writes_back(window, qapp, seeded) -> None:
    """设置页「界面主题」下拉框：切换**立即生效** + 写回 config.toml（保留注释与未知键）。"""
    from laoa_trader.ui import theme as theme_mod

    box = window.theme_box
    # 八项：六套彩色皮肤（默认靛紫）+ 银色 + 系统默认 —— 主人要求"其它几套也放进包里能选"
    assert [box.itemText(i) for i in range(box.count())] == [
        "午夜靛紫（默认·深色）", "深海宝蓝（深色）", "石墨琥珀金（深色）",
        "暗夜玫红（深色）", "哑光雾蓝（深色·低饱和）", "浅色高级灰",
        "银色（金属感）", "系统默认"]
    assert box.currentData() == "indigo"                  # 默认是午夜靛紫
    default_qss = qapp.styleSheet()
    assert default_qss                                    # 默认皮肤：有样式表

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

    # 切回默认皮肤 → 样式表回来，界面照常刷新（换皮肤不许把界面换坏）
    box.setCurrentIndex(box.findData("indigo"))
    qapp.processEvents()
    assert qapp.styleSheet() == default_qss
    assert window.cfg.ui_theme == "indigo"
    assert 'ui_theme = "indigo"' in (seeded.data_dir / "config.toml").read_text(
        encoding="utf-8"
    )
    window._tick()
    qapp.processEvents()
    assert window.pool_table.rowCount() == 1              # 池子还在、刷新没出异常
    # 2026-09-18 起**不再**往标题区弹"已切换"（用户："软件操作的一些提醒都不需要"）：
    # 这句回显现在写在设置页那行小字里
    assert "界面主题已切换为「午夜靛紫（默认·深色）」" in window.save_settings_hint.text()
    assert "界面主题已切换" not in window.status_label.fullText()


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
    """配置里写错主题（例如中文/少个字母）：按默认皮肤走，界面照常能用。"""
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import theme as theme_mod

    seeded.ui_theme = "银色金属"
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert theme_mod.current_theme() == "indigo"
        assert qapp.styleSheet()                               # 仍然是默认皮肤
        assert win.theme_box.currentData() == "indigo"         # 下拉框显示默认
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
        lambda title, lines, kinds=("feishu", "tray"), cfg=None: {
            "feishu": {"kind": "feishu", "ok": True, "skipped": True, "detail": "未配置"},
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
    assert "托盘成功" in text
    assert "弹窗" not in text            # Windows 弹窗那一整路已删除（2026-09-18）


def test_doctor_command_prints_report(cfg, capsys, tmp_path) -> None:
    """`--cli --doctor` 自检：路径/依赖/凭据/数据概况，排障第一步。"""
    from laoa_trader.__main__ import cli

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{p(cfg.data_dir)}"', encoding="utf-8")
    assert cli(["--cli", "--doctor", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    # 自检报告的开头用**短自称**（手机上/剪贴板里那几行要短），界面标题才用全名
    assert f"{ui_app.ASSISTANT_NAME} —— 自检" in out
    assert "数据目录" in out and "数据库" in out
    assert "同花顺 Key" in out
    assert "行情行数" in out
    # 缺 Key 时要给出可执行的下一步提示
    assert "下载" in out or "API Key" in out


def test_settings_tab_widgets_reflect_config(window) -> None:
    """「系统设置」= **四组**（顺序固定），每组控件如实反映配置的当前值。

    四组由用户给定（`SETTINGS_GROUPS`）：数据来源 → 通知方式 → **T策略** → 其他。
    原来是五组，第 3 组是「竞价扫描」（开关 + 8 个阈值）—— 2026-10-11 主人
    "把设置里面的去掉"，整组删除（竞价改成一条可编辑的随包策略，见 `formulas/竞价策略.txt`）。
    T策略那一组是用户拍板的名字（原来叫「持仓风险」，用户要求把
    止损/止盈合并进来，原话：止盈止损比例给客户自己设置，在 T策略 中编辑）。
    策略组/成员策略的启停**不在这一页**（按规格移到「策略筛选」的列表里）。
    「盘中使用实时数据」也**不在这一页、且全程序没有这个开关**：那是内置规则，
    主人 2026-09-23 明确划掉（"不需要加开关，按照我说的规则来"）——
    见 `test_no_switch_can_disable_the_intraday_caliber`（`tests/test_formula_lib.py`）。
    """
    from laoa_trader.ui import app as ui_app

    assert list(window.settings_sections) == list(ui_app.SETTINGS_GROUPS) == [
        "数据来源", "通知方式", "T策略", "其他",
    ]
    # 「竞价扫描」那一组**整组没了**：控件、常量、说明行都不许剩下
    assert "竞价扫描" not in ui_app.SETTINGS_GROUPS
    for gone in ("auction_on_box", "auction_min_pct_box", "auction_max_pct_box",
                 "auction_amount_box", "auction_ratio_box", "auction_score_box",
                 "auction_items_box", "auction_board_boxes", "auction_scan_at_edit",
                 "auction_hint", "_panel_auction_updates", "on_save_auction",
                 "_build_settings_auction_group", "_refresh_auction_hint",
                 "_auction_timer", "_auction_worker", "request_auction", "_auction_tick",
                 "auction_snapshot"):
        assert not hasattr(window, gone), f"{gone} 应该已经随「竞价扫描」那一组删掉"
    assert not hasattr(ui_app, "AUCTION_REFRESH_MS")
    for title, box in window.settings_sections.items():
        assert box.title_label.text() == title
        assert _tab_page(window, ui_app.TAB_SETTINGS).isAncestorOf(box) is True
    # 策略组的勾选**已经不在设置页**（公式/策略页那一边管），别两头都留着
    assert not hasattr(window, "group_boxes")
    assert not hasattr(window, "strategy_boxes")

    # 数据来源：**界面上只剩同花顺那一行**（2026-10-11 主人："设置中，数据来源那里
    # 只保留同花顺那栏，可以填写同花顺KEY，其它都精简掉。"）。
    # 但 `config.data_sources` **照旧生效、照旧是默认两个** —— 功能没有退化，
    # 只是界面不再把它当"要用户配的东西"展示（公开源仍是免 Key 的内部兜底）
    assert window.cfg.data_sources == ["hithink", "public"]
    assert list(window.source_rows) == [ui_app.BUILTIN_SOURCE]
    from laoa_trader.data import sources as sources_mod

    state = {s["id"]: s for s in sources_mod.source_states(window.cfg)}
    builtin = window.source_rows[ui_app.BUILTIN_SOURCE]
    assert builtin.name_label.text() == state["hithink"]["name"]
    assert builtin.name_label.text() == ui_app.DATA_SOURCE_LABELS[ui_app.BUILTIN_SOURCE]
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
    # 说明那一行 = "需要 Key（申请地址 …）"：地址可点开、**只有一句话**
    assert builtin.key_notice_text == ui_app.BUILTIN_BACKUP_TEXT.format(
        url=ui_app.BUILTIN_KEY_URL)
    assert builtin.key_notice_text == "需要 Key（申请地址 https://fuyao.aicubes.cn）"
    assert "需要 Key" in builtin.key_label.text()
    assert f'href="{ui_app.BUILTIN_KEY_URL}"' in builtin.key_label.text()   # 地址可点开
    assert builtin.key_label.openExternalLinks() is True
    # 2026-10-11 精简掉的**整块东西**（来源列表 / 添加来源 / 删除 / 能力与角色标签）：
    # 控件与方法都必须真的不在，不然"精简"只是把字藏起来
    for gone in ("data_source_label", "source_list_layout", "btn_add_source",
                 "source_add_hint", "_source_states", "_fallback_source_state",
                 "_addable_sources", "_rebuild_source_rows", "_refresh_source_hint",
                 "_source_add_menu", "on_add_source_clicked", "on_add_source",
                 "on_remove_source", "_source_text", "_source_label"):
        assert not hasattr(window, gone), f"{gone} 应该随来源列表一起删掉"
    for gone in ("SOURCE_CAPABILITIES", "SOURCE_ADD_UNAVAILABLE_TEXT",
                 "BUILTIN_BACKUP_TAG"):
        assert not hasattr(ui_app, gone), f"{gone} 应该随来源列表一起删掉"
    # 那一行自己也不再挂角色/能力/开关/删除按钮（`SourceRow` 瘦成一行）
    for gone in ("tag_label", "capability_label", "note_label", "enabled_box",
                 "btn_delete"):
        assert not hasattr(builtin, gone), f"SourceRow.{gone} 应该删掉"
    # 界面上不再出现"添加/删除来源"这种操作文案（连提示行都没有了）
    group_text = _settings_group_text(window, "数据来源")
    for text in ("添加来源", "删除来源", "可以添加：", "免 Key", "列表顺序",
                 "当前来源（按优先级）", "主来源", "备用源", "config.toml",
                 "data_sources"):
        assert text not in group_text, text
    # 数据量：显示成"几个月"，可编辑的是 history_years
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
    assert window.tray_duration.value() == int(window.cfg.notify_tray_duration_ms)
    assert window.feishu_on_box.isChecked() == bool(window.cfg.feishu_on)
    assert window.feishu_app_id.text() == window.cfg.feishu_app_id

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
    # 只断言"窗口上有这些控件"是不够的 —— 它们可能在别的组里）。
    # ⚠️ 按**组名**取，不要按下标：组会增减（2026-09-23 一度在它前面插过「筛选口径」，
    # 当天又被主人划掉 —— "不需要加开关，按照我说的规则来"）
    t_group = window.settings_sections["T策略"]
    assert t_group.title_label.text() == "T策略"
    for widget in (window.intraday_t_box, window.stop_loss_box, window.take_profit_box,
                   window.t_high_min_gain_box, window.t_high_pullback_box,
                   window.t_low_min_drop_box, window.t_low_rebound_box):
        assert t_group.isAncestorOf(widget) is True
    # 「盘中使用实时数据」**没有**勾选框，也没有对应的配置属性：口径是内置规则
    # （主人 2026-09-23："不需要加开关，按照我说的规则来"）。键集合那一侧由
    # `SETTINGS_KEYS` 钉着（它不在里面），这里再钉"窗口上根本没有这个控件"。
    assert not hasattr(window, "caliber_live_box")
    assert not hasattr(window.cfg, "intraday_pick_live")

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
    """「自选标的」的列**就是用户给定的那几列**（顺序也一致），「来源」列区分策略与自选。

    2026-09-17 改版（用户要求）：在「涨幅」后面加「市值」「换手」两列，
    「提醒」列改成「监控开关」—— 所以列数从 6 变 8；
    2026-09-21（主人要求）：再加「加入日期」「盈亏」两列 → 10 列，
    「监控开关」仍在**最后一列**（点格子就能切换那一列的老位置没动）。
    """
    assert window.pool_table.columnCount() == 11      # 2026-10-11 加「评分」
    assert _header_texts(window.pool_table) == [
        "名称(代码)", "现价", "涨幅", "市值", "换手", "板块", "来源",
        "加入日期", "盈亏", "评分", "监控开关",
    ]
    assert window.pool_table.columnCount() == len(ui_app.WATCH_HEADERS)
    # seeded 里 600002 是 `低价股` 策略选中的（不是自选）：来源 = **哪条策略**
    # （用户要求这一列回答"是哪条策略选出来的"，而不是只写组别）
    assert window.pool_table.item(0, 0).text() == "半导体甲(600002)"
    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("来源")).text() \
        == "低价股"
    # 组别与持有期挪进了行 tooltip（列数被用户定死，不能加列）
    tip = window.pool_table.item(0, 0).toolTip()
    assert "来源：低价股" in tip
    assert "组别：" not in tip        # 2026-09-18 起没有"策略组"这个概念了
    # 「备注」不再单独占一列（列数被用户定死）：它进了**整行的 tooltip**
    assert "备注" not in "".join(_header_texts(window.pool_table))


def test_pool_table_shows_watchlist_rows_not_in_pool(window, seeded, qapp) -> None:
    """手工加的自选**必须立刻出现**在这张表里（哪怕今天的池子还没重建）。

    这是最容易做错的一条：池子表格原来读的是 `stock_pool` 表（**建池那一刻**的快照），
    两次建池之间加的自选根本不在里面 —— 用户看到的是"我明明加了它，表里没有"。
    """
    assert window.pool_table.rowCount() == 1
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="龙头", enabled=True)
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


def test_pool_row_tooltip_carries_fund_flow_only_for_watchlist(window, seeded, qapp) -> None:
    """「资金流」那一行只在**自选行**的 tooltip 里，且单位是**亿元**。

    2026-10-08：资金流**只对自选标的采集**（`scheduler.sync_watchlist_fund_flow`）——
    所以策略标的加这一行只会写"还没采集"，而它永远不会被采集（那是在骗人）。
    没有数据时也必须写出来（`—（还没采集，日更时会自动采）`），
    否则用户分不清"这只票没有资金流"与"程序还没采"。

    这张表**不加列**（10 列已经被主人嫌挤）：这一行只能住在 tooltip 里。
    """
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", enabled=True)
        storage.write_fund_flow(conn, [{
            "date": "2026-10-08", "symbol": "600001", "main_net": 123456789.0,
            "main_net_pct": 4.56, "super_net": 9e7, "big_net": 33456789.0,
            "close": 3.1, "pct": 1.0,
        }])
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    rows = {symbol: index for index, symbol in enumerate(_symbols_of(window.pool_table))}
    watch_tip = window.pool_table.item(rows["600001"], 0).toolTip()
    # 净额按**亿元**显示（库里是元：123456789 → +1.23 亿），占比是百分数原值
    assert "资金流：主力 +1.23 亿（+4.56%） 2026-10-08" in watch_tip
    # 策略标的（600002）**没有**这一行 —— 它不在采集名单里
    assert "资金流" not in window.pool_table.item(rows["600002"], 0).toolTip()
    # 表还是 10 列（用户嫌挤：不加列）
    assert window.pool_table.columnCount() == 11      # 2026-10-11 加「评分」


def test_watchlist_row_without_collected_flow_says_so(window, seeded, qapp) -> None:
    """自选但**还没采集** → 明说"日更时会自动采"（别让用户去猜是坏了还是没采）。"""
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", enabled=True)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    row = _symbols_of(window.pool_table).index("600001")
    assert "资金流：—（还没采集，日更时会自动采）" \
        in window.pool_table.item(row, 0).toolTip()


def test_pool_row_hover_shows_note_and_monitor_state(window, seeded, qapp) -> None:
    """悬浮看备注 + 监控状态（表里没有「状态」列，这两件事只能靠 tooltip 说清）。"""
    with storage.connect(seeded.db_path) as conn:
        # 打开监控（2026-10-05 起加自选默认不提醒；这里要验的是"开着"那一档）
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="龙头", enabled=True)
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

    2026-10-05（"默认只监控持仓股票"）：匹配选出的票默认**没有**在盯，所以那一项
    是可用的【打开监控】（点了就收下它、开始盯），不再是"灰掉的【关闭监控】"。
    """
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", note="", enabled=True)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = _symbols_of(window.pool_table)

    # 策略标的（600002）：删除可用、关闭监控灰掉、打开雪球可用
    menu = window._row_menu(window.pool_table, rows.index("600002"), "pool")
    actions = {a.text(): a for a in menu.actions() if a.text()}
    assert list(actions)[:2] == ["删除", "打开监控"]
    assert actions["打开监控"].isEnabled() is True      # 点了 = 收下它并开始盯
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
    assert any(a.text() == "打开监控" for a in menu.actions())     # 状态确实变了
    assert "监控" not in window.status_label.fullText()            # 但不再弹操作提示


def test_pool_row_menu_delete_removes_watchlist_then_pool_row(window, seeded, qapp) -> None:
    """【删除】：自选行删自选；纯策略行从**今日池子**里删掉（并说清下次会被重新评估）。"""
    from laoa_trader import pool as pool_mod

    window.on_pool_row_delete("600002")            # 纯策略行
    qapp.processEvents()
    # 事确实做了（这才是要断言的那一半）；成功提示不再弹
    assert pool_mod.pool_symbols(seeded.db_path) == []
    assert "已从今日池子移除" not in window.status_label.fullText()

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
    window.on_pool_row_delete("600001")            # 自选行
    qapp.processEvents()
    # 真删掉了（这才是要断言的那一半）；成功提示不再弹（2026-09-18）
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_map(conn) == {}
    assert "已删除自选" not in window.status_label.fullText()


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
    assert "【开始筛选】" in window.pool_empty_label.text()
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
    assert item.text() == ui_app.MONITOR_OFF_TEXT         # 匹配选出的票默认**不盯**
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
    assert window.position_table.columnCount() == 11  # 2026-10-11 加「评分」
    assert _header_texts(window.position_table) == [
        "名称(代码)", "成本价", "现价", "涨幅", "市值", "换手",
        "盈亏比例", "止损位", "止盈位", "评分", "监控开关",
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
    assert cells[ui_app.POSITION_MONITOR_COLUMN] == ui_app.MONITOR_ON_TEXT  # 默认在监控中
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
    # `monitor = 0` → 做T的观察面里不再有它（这是"关闭监控"的**真实作用**）；
    # 操作提示不再弹（2026-09-18）
    assert list(intraday.held_positions(seeded.db_path)) == []
    assert "关闭监控" not in window.status_label.fullText()
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
    assert window.position_table.rowCount() == 0                # 行没了 = 真删了
    assert "已删除持仓" not in window.status_label.fullText()   # 但不再弹提示


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
    # 回显进的是设置页那行小字（不再往标题区弹）
    assert "T策略" in window.save_settings_hint.text()
    assert "T策略" not in window.status_label.fullText()


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
    """自选标的输入框回车 = 点【添加自选】（加自选是"敲代码回车"的连击动作）。"""
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
    # 加完立刻出现在「自选标的」表里（**不用等今晚重新建池**）
    assert _symbols_of(window.pool_table) == ["600002", "600001"]
    # 成功不再弹提示（有名字、没超上限 → 没有任何警告要说）
    assert "已加自选" not in window.status_label.fullText()


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


# ── 需求 1/2/3 的界面部分：提醒标的文案、涨停原因、涨停那一行 ──


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
        "label": "短期反转", "source_label": "短期反转",
        "industry": "半导体", "note": "", "source": "策略",
        "is_limit_up": False, "continue_day_text": "", "limit_up_reason": "",
    }]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    # 来源 = 主策略；同批选中的其它策略进 tooltip（列宽只放得下一条）
    source_column = ui_app.WATCH_HEADERS.index("来源")
    assert window.pool_table.item(0, source_column).text() == "短期反转"
    tip = window.pool_table.item(0, 0).toolTip()
    assert "来源：短期反转" in tip
    assert "同批选中：地量后放量变盘" in tip        # 第二条策略没有从界面上消失
    # 2026-09-18 起没有"组别 / 持有期"（策略组机制删掉），也没有证据标记
    assert "组别：" not in tip and "T+3" not in tip
    assert "（依赖开盘）" not in tip
    assert "T+3" not in window.pool_table.item(0, source_column).text()


def test_pool_row_tooltip_shows_limit_up(pool_window, qapp, monkeypatch) -> None:
    """今日涨停（连板 + 原因）在**行的 tooltip** 里（原来占表的一列 + 卡片一行）。

    新表的列数被用户定死成 6 列，所以它的去处是 tooltip —— 信息不能因为去掉一列就丢掉。
    （同一行里原来还有"竞价 +3.2% 量比2.8"那一句；2026-10-11 竞价扫描整块下线、
    竞价改成一条普通策略之后，那一句也跟着删了 —— 下面顺带钉住它不会再冒出来。）
    """
    from laoa_trader import pool as pool_mod

    rows = [
        {"symbol": "600002", "name": "半导体甲", "strategy": "LowPriceStrategy",
         "strategies": "LowPriceStrategy", "score": 1.0, "reason": "r",
         "label": "低价股", "source_label": "低价股", "industry": "半导体",
         "note": "", "is_limit_up": True, "continue_day_text": "2 连板",
         "limit_up_reason": "半导体设备+业绩预增"},
        {"symbol": "600003", "name": "白酒样本", "strategy": "X", "strategies": "X",
         "score": 1.0, "reason": "r", "label": "低价股", "source_label": "低价股",
         "industry": "白酒", "note": "", "is_limit_up": False,
         "continue_day_text": "", "limit_up_reason": ""},
    ]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window._pool_signature = None          # 强制重画
    window._refresh_pool_table()
    qapp.processEvents()

    by_symbol = {str(window.pool_table.item(i, 0).data(Qt.ItemDataRole.UserRole)): i
                 for i in range(window.pool_table.rowCount())}
    tip = window.pool_table.item(by_symbol["600002"], 0).toolTip()
    assert "涨停：2 连板 · 半导体设备+业绩预增" in tip
    # 不是涨停票 → tooltip 里没有那一行（不留空壳）
    assert "涨停" not in window.pool_table.item(by_symbol["600003"], 0).toolTip()
    # 文案本身的实现仍由 pool 层提供（界面不自己拼一套）
    assert pool_mod.limit_up_text(rows[0]) == "2 连板 · 半导体设备+业绩预增"
    assert pool_mod.limit_up_text(rows[1]) == ""
    # 竞价那一行**整条不再出现**（它当年也是画在 tooltip 里的）
    assert "竞价" not in tip and "量比" not in tip
    # 状态详情里也不再提"竞价扫描"（原来那里会写"还没有结果 → 去设置页开"）
    details = window._status_details()
    assert "竞价" not in details


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

    from laoa_trader.ui import theme as theme_mod

    table = window.position_table
    assert table.columnCount() == 11
    pnl = ui_app.POSITION_HEADERS.index("盈亏比例")

    # seeded 里 600001：成本 3.0、本地最新收盘 3.174 → +5.80%（两位小数，精确断言）
    window._position_signature = None
    window._refresh_positions()
    qapp.processEvents()
    cell = table.item(0, pnl)
    assert cell.text() == "+5.80%"
    # 赚 → 红。色值**按当前主题取**（深色皮肤上是亮红，浅色皮肤上是 #d32f2f）——
    # 判据仍然只有一处（`market.value_color`），见 `theme.semantic()`。
    assert cell.foreground().color().name() == theme_mod.semantic("up")

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
    assert losing.foreground().color().name() == theme_mod.semantic("down")   # 亏 → 绿

    missing = table.item(by_symbol["600009"], pnl)
    assert missing.text() == ui_app.market.DASH                     # 不是 0.00%
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
    """≈用户那台（逻辑 960×900 → 窗口 920×760）：**11 列**持仓表铺满、不挤、也不顶大最小宽度。

    这一条是给"窗口能缩到 760 宽"的成果上保险：多一列如果让表格的最小宽度变大，
    窗口就又被顶出屏幕了（那正是用户最初抱怨的"最下边看不见"）。
    2026-09-17 加了两列（市值/换手）、2026-10-11 又加了「评分」—— 每次加列都要回来
    确认这条：多一列如果让最小宽度变大，窗口就又被顶出屏幕了。
    """
    win = screen_window(960, 900)
    win.tabs.setCurrentWidget(_tab_page(win, ui_app.TAB_POSITION))
    win._position_signature = None
    win._refresh_positions()
    qapp.processEvents()

    table = win.position_table
    # 2026-10-11 起 11 列：加了「评分」（窄列，只显示整数）
    assert table.columnCount() == 11
    widths = [table.columnWidth(i) for i in range(11)]
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
    from PySide6.QtWidgets import QLabel

    # 软件名只有一个真源（`ui/app.py` 的 APP_NAME，2026-10-08 起是「luweik决策系统」）
    assert window.windowTitle() == ui_app.APP_NAME == "luweik决策系统"
    assert "v" not in window.windowTitle()                 # 不再挂版本号
    assert window.app_title_label.text() == ui_app.APP_NAME
    assert window.btn_about.text() == ui_app.BTN_ABOUT_TEXT == "关于软件"
    # 【关于软件】里版本/版权齐全
    window.on_about()
    qapp.processEvents()
    blob = "\n".join(lb.text() for lb in window.about_dialog.findChildren(
        QLabel))
    assert f"版本：{laoa_trader.__version__}" in blob
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
    assert ui_app.APP_NAME in blob      # 软件名（只有 APP_NAME 一处定义）
    assert f"版本：{laoa_trader.__version__}" in blob
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
    # 2026-09-21 起多了「公式引擎」那一行：用户报障贴这份文本时，
    # 一行就能看出他跑的是哪个版本、引擎认识多少函数（旧包是短清单）。
    assert lines == [
        ui_app.APP_NAME,
        f"版本：{laoa_trader.__version__}",
        window.about_lines()[2],
        "版权所有 © 2026 async-chen，保留所有权利。",
    ]
    assert lines[2].startswith("策略引擎：") and "支持" in lines[2]
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
    """窗口图标是随包那套多档图标（不是空图标，也不是系统默认图标）。

    先清掉应用级图标：`QWidget.windowIcon()` 在窗口没设图标时会退回**应用**图标，
    不清掉的话这条断言就分不清"窗口自己设上了"和"蹭了应用图标"。
    """
    from PySide6.QtGui import QIcon

    qapp.setWindowIcon(QIcon())
    assert window.windowIcon().isNull() is False
    # 多档（含 256）—— 见 `test_application_icon_is_set` 里为什么不再只塞一张 256
    sizes = sorted(size.width() for size in window.windowIcon().availableSizes())
    assert sizes == [16, 24, 32, 48, 64, 128, 256], sizes


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
    # 图标现在是**多档 QIcon**（用户 2026-09-20 实报"图标偏小、模糊"）：
    # Windows 会按场景挑最合适的一档，而 16/24/32 那几档是"按角色头部裁过再缩"的，
    # 只塞一张 256 就等于那些裁剪版白做了。
    sizes = sorted(size.width() for size in qapp.windowIcon().availableSizes())
    assert sizes == [16, 24, 32, 48, 64, 128, 256], sizes


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


def test_save_notify_writes_channels_and_params(window, seeded, qapp) -> None:
    """通知那一组的保存：四个勾选框 + 声音/闪烁/浮窗参数一次写回。"""
    for name, box in window.channel_boxes.items():
        box.setChecked(name == "tray")
    window.popup_box.setChecked(False)
    window.sound_box.setChecked(False)
    window.flash_seconds_box.setValue(12)
    window.popup_seconds_box.setValue(20)
    window.popup_items_box.setValue(8)
    window.tray_duration.setValue(3000)
    window.on_save_notify()
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'notify_channels = ["tray"]' in text
    assert "notify_popup = false" in text
    assert "notify_sound = false" in text
    assert "notify_flash_seconds = 12" in text
    assert "notify_popup_seconds = 20" in text
    assert "notify_popup_max_items = 8" in text
    assert "notify_tray_duration_ms = 3000" in text
    # 已删除的 Windows 频道那几行：老配置里本来有，保存后**必须被抹掉**
    assert "notify_windows" not in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert window.cfg.notify_channels == ["tray"]
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
    # 成功保存**不再弹提示**：回显在设置页那行小字里（一句都不少）
    assert "只入库不推送" in window.save_settings_hint.text()
    assert "✅ 已保存" in window.save_settings_hint.text()
    assert "✅ 已保存" not in window.status_label.fullText()
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
    assert "✅ 已保存" in window.save_settings_hint.text()     # 回显（不再弹到标题区）
    assert "winotify" not in window.status_label.fullText()   # 也不是那句平台不支持的提示

# ── 「系统设置」：底部【保存设置】**一键**写回本页所有设置 ──

#: 一键保存应当写回的**全部**键（一个不多、一个不少）。
#: 少一个键 = "改了没生效"（最难查的那类抱怨）；多一个键 = 把不属于本页的东西悄悄改了。
SETTINGS_KEYS: frozenset[str] = frozenset({
    # 1) 数据来源
    #
    # ⚠️ 这里**没有 `data_sources`**，而且不许加（2026-10-11）：界面上的来源列表、
    # 【添加来源】、【删除】全删掉了，所以没有任何一个控件能改它 ——
    # 用户手写的顺序/来源名因此**一个字符都不会被动**（有逐字节比对的用例：
    # `test_saving_settings_never_touches_a_hand_written_data_sources`）。
    # 它照旧生效：`data/sources.py` 按它决定取数顺序与启停，公开源仍在链子里。
    "history_years",
    # 2) 通知方式
    "notify_popup", "notify_channels", "notify_sound", "notify_flash_seconds",
    "notify_popup_seconds", "notify_popup_max_items", "notify_tray_duration_ms", "feishu_on",
    "feishu_app_id", "feishu_app_secret", "feishu_chat_id",
    # 桌宠 + 中文朗读（2026-09-18 用户要的"机器人/桌宠喊出消息内容"）：
    # 这些键都在「通知方式」这一组里，一键保存就该把它们写回去
    # （`pet_x`/`pet_y` **不在**这里：那是拖动时单独写的，界面里没有对应控件）
    # `notify_voice_name` 是 2026-09-20 加的（用户："设置里桌宠声音可以自由改" →
    # 音色下拉框，空字符串 = 自动挑中文）
    # `notify_voice_digits` 已于 2026-09-21 删除（主人："价格逐位不需要有选项"）——
    # 读法固定，界面也没有对应控件
    # `notify_voice_rate` 已于 2026-09-21 删除（主人："把播报速度直接锁定 1.0 吧
    # 不要给选择了"）—— 语速固定 1.0，界面也没有控件
    "notify_pet", "notify_voice", "notify_voice_volume", "notify_voice_name",
    # 语音播报内容（2026-10-05 主人："语音播报有点乱，可以自由选择要提醒的内容"）：
    # 念哪些类型 / 一句里念哪几样 / 一次来多条怎么念
    "voice_kinds", "voice_fields", "voice_multi",
    # 3) T策略（原来这里是"3) 竞价扫描"那 9 个键 —— 2026-10-11 整组删掉了）
    "stop_loss", "take_profit", "intraday_t", "t_high_min_gain_pct",
    "t_high_pullback_pct", "t_low_min_drop_pct", "t_low_rebound_pct",
    # 4) 其他
    "ui_theme", "window_frame", "intraday_anomaly", "watchlist_max",
    "watchlist_in_pool",
    # 5) 数据来源那一行里**用户自己填的** Key（2026-09-18 起内置同花顺也有输入框了）
    "hithink_api_key",
})


def test_collect_settings_updates_covers_exactly_the_four_groups(window) -> None:
    """收集函数的键集合 = 四组控件的**全部**键（一键保存的"写哪些"就是它决定的）。

    **32 是现在的个数**（含 `hithink_api_key` —— 用户澄清"不要配 KEY"指的是
    **程序里不许预置自己的 Key**，不是不给填，
    所以内置同花顺那一行重新有了输入框，一键保存也就该把它写回去；
    出厂包里这个值始终是空串，程序从不写死它）。
    这个键**不是写死的**：收集函数按"界面上那一行自己的 `key_config`"收，
    也就是"界面上有没有输入框"是唯一判据（所以哪天再加一个要 Key 的来源，
    它的 Key 会自动进这份键集合 —— 见下一条断言）。
    `data_sources` 则**一个都不多收**：界面上已经没有能改它的控件了。
    """
    updates = window._collect_settings_updates()
    assert set(updates) == SETTINGS_KEYS
    # 界面管不着 `data_sources`：它既不在收集函数的字典里，也不在界面的任何控件上
    assert "data_sources" not in updates
    # 当前：37 个固定键（35 → 33：删掉 Windows 通知那一路时
    # `notify_windows_sound` / `notify_windows_open_url` 随之取消；
    # 33 → 37：加上桌宠与中文朗读的 4 个键；
    # 37 → 38：用户要求"桌宠声音可以自由改"，加上音色下拉的 `notify_voice_name`；
    # 39 → 38：2026-09-21 删掉"数字逐位念"开关 `notify_voice_digits`，
    # 主人原话："价格逐位不需要有选项，直接按我说的做就行了"；
    # 38 → 37：同一天删掉语速键 `notify_voice_rate`（"把播报速度直接锁定 1.0 吧"）。
    # 2026-09-23 一度加过 `intraday_pick_live`（「筛选口径」那一组），当天又被主人划掉
    # ——"不需要加开关，按照我说的规则来"，所以个数回到 37；
    # 37 → 40：2026-10-05 加上语音播报内容那三项（`voice_kinds` / `voice_fields` /
    # `voice_multi`）—— 主人："语音播报有点乱，可以自由选择要提醒的内容"；
    # 40 → 41：2026-10-10 加上窗口外观 `window_frame`（自绘标题栏 / 系统标题栏）；
    # 41 → 32：2026-10-11 删掉「竞价扫描」那一组（`intraday_auction` / `auction_scan_at` /
    # `auction_min_pct` / `auction_max_pct` / `auction_boards` / `auction_min_amount` /
    # `auction_min_volume_ratio` / `auction_min_score` / `auction_alert_max_items`
    # 一共 9 个键）—— 主人："把设置里面的去掉"）
    assert len(updates) == 32
    # 2026-09-18 起**必须收**它：内置同花顺那一行有输入框，一键保存就该把它写回去
    # （出厂值是空串，程序从不预置；"填了没保存"才是要防的那件事）
    assert "hithink_api_key" in updates
    # 「盘中匹配用实时数据」**不在键集合里**：内置规则没有开关，一键保存写不出去它
    assert "intraday_pick_live" not in updates
    # 竞价那 9 个键**一个都不在**（它们在 `Config` 上连字段都没了）
    assert not [k for k in updates if k.startswith("auction")], sorted(updates)
    assert "intraday_auction" not in updates
    # 收集规则本身（"界面上有输入框的每一行，按它自己的 key_config 收"）：
    # 界面上现在只有同花顺一行，所以这一份键集合里**只该有** `hithink_api_key`
    # 这一个 Key 类键 —— 多收一个就说明有哪一行没被精简掉
    row_key_fields = {
        str(getattr(row, "key_config", "") or "")
        for row in window.source_rows.values()
        if getattr(row, "key_edit", None) is not None
    }
    assert row_key_fields == {"hithink_api_key"}
    key_like = {k for k in updates if k.endswith("_api_key") or k.endswith("_token")}
    assert key_like == {"hithink_api_key"}, sorted(key_like)


def _change_every_settings_control(window) -> None:
    """把设置页**每一个**控件都改一遍（一键保存的验收要用；不留一个"没被覆盖"的键）。

    数据来源那一组现在只有两个控件：数据量（`history_years`）与同花顺那一行的
    Key 输入框（`hithink_api_key`）—— 两个都要改，改完一键保存就该都落盘。
    `data_sources` **没有控件**，所以这里也不改它（这正是"界面不动它"的由来）。
    """
    # 1) 数据来源
    window.history_years_box.setValue(1.5)
    window.source_rows[ui_app.BUILTIN_SOURCE].key_edit.setText("key-from-one-click")
    # 2) 通知方式（四个勾选框 + 参数）
    window.popup_box.setChecked(False)
    for name, box in window.channel_boxes.items():
        box.setChecked(name == "feishu")
    window.sound_box.setChecked(False)
    window.flash_seconds_box.setValue(9)
    window.popup_seconds_box.setValue(12)
    window.popup_items_box.setValue(3)
    window.tray_duration.setValue(4000)
    window.feishu_on_box.setChecked(True)
    window.feishu_app_id.setText("cli_test")
    window.feishu_secret.setText("secret_test")
    window.feishu_chat_id.setText("oc_test")
    # 3) T策略（开关 + 四个做T阈值 + 止损/止盈）
    window.stop_loss_box.setValue(6.0)
    window.take_profit_box.setValue(12.0)
    window.intraday_t_box.setChecked(True)
    window.t_high_min_gain_box.setValue(3.0)
    window.t_high_pullback_box.setValue(2.0)
    window.t_low_min_drop_box.setValue(2.5)
    window.t_low_rebound_box.setValue(1.5)
    # 6) 其他
    window.theme_box.setCurrentIndex(window.theme_box.findData("system"))
    window.anomaly_box.setChecked(True)
    window.watchlist_max_box.setValue(30)
    window.watchlist_in_pool_box.setChecked(False)


def test_save_settings_button_writes_every_control_in_one_click(window, seeded, qapp) -> None:
    """**一键保存的验收**：改遍每个控件 → 点一次【保存设置】→ 逐键断言落盘 + 回显。

    同时验证"保留用户自己的注释与未知键"（`config.update_config_file` 是**就地改写**，
    不是重写整个文件）—— 这是这一页敢一次写回 32 个键的前提。
    """
    # 输入框的初值**只能是**用户配置里的原值（界面不预置任何 Key）
    row = window.source_rows[ui_app.BUILTIN_SOURCE]
    assert row.key_edit.text() == window.cfg.hithink_api_key
    _change_every_settings_control(window)
    qapp.processEvents()
    config_file = seeded.data_dir / "config.toml"
    key_from_ui = row.key_edit.text()             # 上面刚改成 "key-from-one-click"
    assert key_from_ui != window.cfg.hithink_api_key

    window.save_settings_button.click()          # ← **一次点击**
    qapp.processEvents()
    text = config_file.read_text(encoding="utf-8")

    for line in (
        "history_years = 1.5",
        # 数据来源那一组现在还有一个控件：同花顺那一行的 Key 输入框（见下）
        f'hithink_api_key = "{key_from_ui}"',
        "notify_popup = false",
        'notify_channels = ["feishu"]',
        "notify_sound = false",
        "notify_flash_seconds = 9",
        "notify_popup_seconds = 12",
        "notify_popup_max_items = 3",

        "notify_tray_duration_ms = 4000",
        "feishu_on = true",
        'feishu_app_id = "cli_test"',
        'feishu_app_secret = "secret_test"',
        'feishu_chat_id = "oc_test"',
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
    # 项数就是固定键那份集合：内置同花顺那一行的 Key 也在里面
    # （用户 2026-09-18 要回了输入口，`hithink_api_key` 属于固定键）
    assert hint.startswith(f"✅ 已保存 {len(SETTINGS_KEYS)} 项（已写入 config.toml）")
    assert "生效：主题 系统默认；" in hint and "T策略 开" in hint
    # 那一行的 Key **会被写回**：改动来自界面那个输入框（"填了没保存"是要防的那件事）
    assert f'hithink_api_key = "{key_from_ui}"' in text
    assert window.cfg.hithink_api_key == key_from_ui
    # 而 `data_sources` **没有**被这次保存塞进文件（界面没有任何入口能改它；
    # 逐字节比对见 `test_saving_settings_never_touches_a_hand_written_data_sources`）
    assert "data_sources" not in text
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
    这正是"不能静默丢弃"要防的那件事。这里把两种典型填错都走一遍。
    （原来还有第三种"竞价涨幅上下限倒挂"；竞价那一组删掉之后，那条校验也不存在了。）
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

    # ③ 改回合法值 → 这一次真的写进去了（拒绝不是"卡住不动"）
    window.t_low_rebound_box.setValue(1.0)
    window.save_settings_button.click()
    qapp.processEvents()
    assert window.save_settings_hint.text().startswith("✅ 已保存")
    assert config_file.read_text(encoding="utf-8") != before


def test_formula_page_button_runs_the_pipeline_through_its_signal(
    seeded, qapp, monkeypatch
) -> None:
    """**点页面那个【开始筛选】按钮** → 走 `start_pick_requested` → 到 `on_run_pipeline`。

    这条是按契约收尾的验收（B2b 的页面只 emit 信号，流程在主窗口）：
    - 同一页**只有一个**【开始筛选】（主窗口不许再自带一个，两个同名按钮没法解释哪个真的跑）；
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
        # 这一页里的【开始筛选】按钮**只有一个**，而且就是页面自己那个
        formula_tab = _tab_page(win, ui_app.TAB_FORMULA)
        buttons = [b for b in formula_tab.findChildren(QPushButton)
                   if b.text() == "开始筛选"]
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


def test_pick_result_is_shown_on_the_formula_page_again(window, qapp) -> None:
    """匹配跑完 → 结果**显示在「策略筛选」页的结果页上**（用户 2026-09-18 改的口径）。

    口径变过一次，两个日期都写在这里（免得下一个人以为哪一版是漏改）：
    2026-09-17 用户要求"这一页只显示策略，不显示结果"；2026-09-18 改成
    "匹配状态时，策略列表界面变为筛选结果界面（平时隐藏），结果可以一键加入自选和导出"。

    所以这条盯两件事：① 主窗口跑完 → 结果确实进了结果表、页面切到结果页；
    ② 主窗口对"没有这个方法的页面"仍然容错（`getattr` 老写法不许退化）。
    """
    report = {
        "data_date": "2026-09-11",
        "picks": 2,
        "pool": [
            {"symbol": "600002", "name": "半导体甲", "strategy": "公式·放量上攻",
             "strategies": "公式·放量上攻"},
            # 纯自选行（没有来源策略）→ 不算"本次筛选结果"
            {"symbol": "300750", "name": "电池龙头", "strategy": "", "strategies": ""},
        ],
    }
    window._on_pipeline_done("开始筛选", report)      # 主窗口调它：不许抛
    qapp.processEvents()

    page = window.formula_page
    assert [row["symbol"] for row in page.result_rows] == ["600002"]
    assert page.list_stack.currentWidget() is page.result_page      # 切到结果页
    assert page.result_table.rowCount() == 1

    # 结论照旧进运行状态（用户照样知道"跑完了、选出了几只"）
    text = window.status_label.fullText()
    assert "开始筛选完成" in text and "池子 2 只" in text

    # 主窗口是**容错调用**的：另一个模块没提供这个方法时不该让流程报错
    class NoResultPage:
        def reload(self):
            pass

    original = window.formula_page
    window.formula_page = NoResultPage()
    try:
        window._on_pipeline_done("开始筛选", report)     # 不抛异常
    finally:
        window.formula_page = original

def test_alert_texts_use_halfwidth_parens(window) -> None:
    """提醒相关的**所有用户可见文本**都是半角 `名称(代码)`：表格目标列 / 浮窗行 / 详情。

    全项目只有一种写法（`docs/开发文档.md`）：两处表格的列头早就写的是
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
        seen["sound"] = cfg.notify_sound           # Windows 那条路删了，改成看提示音开关
        return {"feishu": {"kind": "feishu", "ok": True, "skipped": True,
                           "detail": "未配置飞书凭证，已跳过"},
                "tray": {"kind": "tray", "ok": True, "detail": "投递"}}

    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify_all)
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")

    # 面板当前勾选 = tray（测试提醒用的是**面板当前值**，不用先保存）
    for name, box in window.channel_boxes.items():
        box.setChecked(name == "tray")
    window.sound_box.setChecked(False)
    window.on_test_notify()
    assert window._worker is not None
    window._worker.wait(10_000)
    qapp.processEvents()

    assert seen["channels"] == ["tray"]
    assert seen["sound"] is False
    assert "测试通知" in window.status_label.fullText()
    assert "飞书已跳过" in window.status_label.fullText()
    # 测试提醒只是"实发一条"，不该顺手改配置
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before


def test_run_pipeline_button_runs_in_background_and_is_idempotent(window, qapp,
                                                                  monkeypatch,
                                                                  seeded) -> None:
    """【立即筛选并建池】：后台线程跑完整流程，写 signal/stock_pool，不崩界面。"""
    from laoa_trader import scheduler as sched

    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"kind": "tray", "ok": True}})
    # 这条测的是"按钮 → 后台 → 落库"的接线，不测候选从哪来。
    # 2026-09-18（用户要求）起候选**只来自勾选的公式**（内置策略退出匹配链路），
    # 所以这里写一条最简单的公式（`C>0`，小库里两只票都命中）并勾上它 ——
    # 老写法（`enabled_groups = ["swing"]`）已经不会让任何票进池了。
    formula_dir = seeded.data_dir / "formulas"
    formula_dir.mkdir(parents=True, exist_ok=True)
    (formula_dir / "界面测试公式.txt").write_text(
        "# 名称: 界面测试公式\n# 说明: 界面用例专用\nC>0\n", encoding="utf-8"
    )
    monkeypatch.setenv("LUWEIK_FORMULAS", str(formula_dir))
    seeded.enabled_formulas = ["界面测试公式"]

    window.on_run_pipeline()
    assert window._worker is not None
    window._worker.wait(60_000)
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        signals = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_rows = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
    assert signals > 0 and pool_rows > 0
    assert "立即筛选并建池完成" in window.status_label.fullText()
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
    """【只刷新数据】：跑同步、不筛选、不推送；同步失败也要有中文结论。"""
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


# ── 自选标的（添加 / 启停 / 删除）——表格就是「自选标的」那一张 ──


def test_watchlist_panel_starts_empty(window) -> None:
    """没加过自选时表格是空的（seeded 里只有一只策略标的 —— 它在表里，但不来自自选）。"""
    assert window.pool_table.rowCount() == 1
    assert window.pool_count_label.text() == "共 1 只（策略 1 · 自选 0）"


def test_watch_panel_add_autofills_name_and_notes(window, seeded, qapp) -> None:
    """加自选：名称从本地库自动补、备注写进去、表格立刻出现。

    2026-10-05 起**加进来默认不提醒**（主人："默认只监控持仓股票"），所以这一行的
    监控开关是 `关闭`、来源列带「（已停用）」尾巴；要盯它得点开那一格。
    """
    window.watch_symbol.setText("600001")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        rows = storage.load_watchlist(conn)
    assert rows[0]["symbol"] == "600001"
    assert rows[0]["name"] == "低价样本"        # 自动补的名字
    assert rows[0]["enabled"] == 0                 # 默认不提醒（2026-10-05）
    assert rows[0]["note"] == "龙头"
    row = _symbols_of(window.pool_table).index("600001")
    assert window.pool_table.item(row, 0).text() == "低价样本(600001)"
    assert "备注：龙头" in window.pool_table.item(row, 0).toolTip()
    assert window.pool_table.item(row, ui_app.WATCH_HEADERS.index("来源")).text() \
        == "自选（已停用）"
    # 默认**不提醒** → 「监控开关」列是 `关闭`
    assert window.pool_table.item(row, ui_app.WATCH_MONITOR_COLUMN).text() \
        == ui_app.MONITOR_OFF_TEXT
    assert "已加自选" not in window.status_label.fullText()     # 成功不弹提示


def test_watch_panel_add_unknown_symbol_warns_but_adds(window, seeded, qapp) -> None:
    window.watch_symbol.setText("601999")
    window.on_watch_add()
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        rows = storage.load_watchlist(conn, enabled_only=False)
    assert [r["symbol"] for r in rows] == ["601999"]
    assert rows[0]["enabled"] == 0          # 默认不提醒（2026-10-05）
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
    assert "不进池、不监控" not in window.status_label.fullText()   # 操作提示不再弹

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
    """自选标的进池后：来源写**哪条策略选出来的**（没有策略来源才是「自选」）。"""
    from laoa_trader import pool as pool_mod

    # 600002 是策略选中的（seed 的池子），把它也加为自选 → 来源仍写那条策略名
    window.watch_symbol.setText("600002")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    assert window.pool_table.item(0, ui_app.WATCH_HEADERS.index("来源")).text() \
        == "低价股"
    assert "备注：龙头" in window.pool_table.item(0, 0).toolTip()

    # 纯自选（不在策略候选里）→ 来源「自选」，而且**不用等建池**就能进这张表
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600100", name="冷门样本", note="消息面", enabled=True)
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
        # 打开监控：概况里的"含自选 N 只"数的是**盯着**的那些（2026-10-05 起）
        storage.upsert_watchlist(conn, "600001", name="低价样本", enabled=True)
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
    """一个**空库**的主窗口：启动自检必然判 needs_full → **弹一次**"数据不能用"的窗。

    2026-10-11 之前这里断言的是标题区那一行常驻提示；主人要求"提示错误时直接弹窗说明"、
    常驻的首启引导行删掉之后，它改成断言那个弹窗（见 `test_startup_preflight_pops_once`）。
    """
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
    win.scheduler.stop()
    win.quotes.stop()
    win.tray.hide()
    win.deleteLater()
    qapp.processEvents()


#: 删掉的那些"常驻解释性小字"里出现过的**原话**（一个都不许再出现在界面上）。
#: 逐条对着主人 2026-10-11 的要求来："宁可删干净，别改写成短句留在那儿"。
_DELETED_EXPLANATION_PHRASES: tuple[str, ...] = (
    "本地还没有行情数据",                                  # 首启引导行 first_run_hint
    "market_breadth 关着，不取全市场快照",                 # 概览页 market_hint（口径解释）
    "本地还没有涨停池数据，在【系统设置】里点【刷新数据】补齐",
    "列表顺序 = 取数优先级",                               # 四组标题下的说明
    "都不勾 = 只入库不推送",
    "零碎的偏好，改完点下面的",
    "这四个阈值只管做T提示",
    "换主题立即生效（不用重启）",
    "它管的是窗口本身，不是一层皮肤",
    "默认：提醒来时闪托盘",
    "结果直接进「自选标的」；策略的启停",
    "策略列表：全是策略",                                  # LIST_HINT
    "点右边的按钮就能插入；最后一行是筛选条件",            # EDITOR_HINT
    "勾「策略选取」列 = 这条策略参与筛选",                 # PAGE_HINT
    "口径：面积 = 行业流通市值合计",                       # 热力图那行小字里的口径段
    # 2026-10-11（主人："数据来源那里只保留同花顺那栏…其它都精简掉"）：
    # 【添加来源】/【删除】/能力标签/角色标记那一套整块删掉，这几句原话不许回来
    "添加来源",
    "可以添加：",
    "没有可添加的来源了",
    "这个来源名是你在 config.toml 的 data_sources 里写的",
    "当前来源（按优先级）",
    "主来源",
    "备用源",
    "免 Key",
)


def test_no_resident_explanation_labels_left(window, qapp) -> None:
    """**常驻的解释性小字已经删干净**（主人 2026-10-11："宁可删干净"）。

    这条是"别偷偷长回来"的看门人：把窗口里**所有** `statusTag` 标签的实际文字扫一遍，
    对照上面那张"删掉的原话"清单 —— 一句都不许出现。留下的那十几个标签只用来说明
    **状态 / 结果 / 错误与原因 / 指路**（例如"共 12 只（策略 10 · 自选 4）"、
    "将使用系统语音「X」朗读"、"还没取过热力图数据 —— 点【刷新热力图】…"），
    主人的保留清单里明确要留着它们。
    """
    from PySide6.QtWidgets import QLabel

    tags = [w for w in window.findChildren(QLabel) if w.objectName() == "statusTag"]
    assert tags, "一个 statusTag 都没扫到，这条判据就没意义了"
    joined = "\n".join(w.text() for w in tags)
    for phrase in _DELETED_EXPLANATION_PHRASES:
        assert phrase not in joined, f"删掉的解释性小字又回来了：{phrase}"
    # 删掉的那几个"整行/整组"的东西也一起钉住（属性、参数、常量）
    for gone in ("first_run_hint", "market_hint", "_market_hint_notes",
                 "_first_run_hint_text", "show_first_run_wizard", "hide_first_run_hint"):
        assert not hasattr(window, gone), f"{gone} 应该已经删掉"
    import inspect

    params = inspect.signature(type(window)._settings_group).parameters
    assert "note" not in params, "四组标题下的那条说明参数应该已经删掉"
    # QLabel 不解析 markdown：任何标签上都不许留 `**`
    for w in tags:
        assert "**" not in w.text(), w.text()[:60]


def test_tooltips_are_one_short_sentence(window, qapp) -> None:
    """tooltip **只留一句短的**：没有多行、也没有超长的（主人 2026-10-11 的硬要求）。

    判据是"长度"而不是"内容像不像解释"：**> 45 字**一律算超长（那条规矩落地时是把
    多行/超长的整段删掉、只留一句）。想加内容就加进日志或【显示详情】，别塞进 tooltip。
    """
    from PySide6.QtWidgets import QAbstractSpinBox, QCheckBox, QComboBox, QLabel, QPushButton

    widgets = (window.findChildren(QLabel) + window.findChildren(QPushButton)
               + window.findChildren(QCheckBox) + window.findChildren(QComboBox)
               + window.findChildren(QAbstractSpinBox))
    def allowed(tip: str) -> bool:
        """允许"比一句短话长"的三类 —— 主人的保留清单里就是它们（逐条写清理由）。"""
        # ① 状态标签上的 tooltip = 【显示详情】的**全文**（主人："完整原因照旧进日志与【显示详情】"）
        if tip.startswith("状态详情"):
            return True
        # ② 公式面板按钮 = 字段说明（单位 + 例子）：主人明确要求这些单位写在按钮上，
        #    有测试钉着（`test_formula_page.py` 断言"亿元 / 百分数 / 例：REF(C,1)"）
        if tip.startswith("插入 "):
            return True
        # ③ 概览页脚 = "数据从哪来"，一句状态（页面上没有第二处写它）
        if "非交易所授权行情" in tip:
            return True
        # ④ 涨跌家数那一格 = "为什么显示 —"的**原因**（主人：错误与原因要保留）
        if "market_breadth 关着" in tip:
            return True
        # ⑤ 同花顺那一行的 Key 说明 = **指路**（"去哪申请、填哪儿"），主人保留清单里点名要留；
        #    有测试钉着申请地址必须写在界面上
        if "fuyao.aicubes.cn" in tip:
            return True
        return False

    offenders = []
    for w in widgets:
        tip = w.toolTip() or ""
        if not tip or allowed(tip):
            continue
        if "\n" in tip or len(tip) > 45:
            offenders.append((type(w).__name__, (getattr(w, "text", lambda: "")() or "")[:16], tip[:60]))
    assert not offenders, offenders[:6]


def test_startup_preflight_pops_once(hint_window, qapp, modal_calls) -> None:

    """**启动自检失败 → 弹一次窗**（主人 2026-10-11："提示错误时直接弹窗说明"）。

    这一条替代了原来那行常驻提示（"本地还没有行情数据 · 在【系统设置】里点【下载数据】"）：
    正文里必须同时有**出了什么事**（本地还没有行情数据）与**下一步做什么**
    （在【系统设置】里点【下载数据】）；再次自检**不再弹**（`once_key` 去重）。
    """
    from laoa_trader.data import preflight

    win = hint_window
    assert win.preflight_result["status"] == preflight.NEEDS_FULL
    # 只看"启动自检"这一类（空库启动时热力图也会失败一次，那是另一类后台错误）
    shots = [c for c in modal_calls if c["once_key"] == "startup-preflight"]
    assert len(shots) == 1, [(c["once_key"], c["title"]) for c in modal_calls]
    assert shots[0]["shown"] is True
    assert "本地" in shots[0]["title"]
    # 正文必须同时有"出了什么事"与"下一步做什么"（口径细节不进弹窗）
    assert "本地" in shots[0]["shown_text"] and "还没" in shots[0]["shown_text"]
    assert "系统设置" in shots[0]["shown_text"]
    assert "下载数据" in shots[0]["shown_text"]
    assert len(shots[0]["shown_text"].splitlines()) <= 3    # 不堆段落

    # 再跑一次自检：同一类错误**不再弹**（否则每点一次【刷新数据】就糊一个框）。
    # ⚠️ 要 `force=True`：`run_preflight` 是**幂等**的（已经查过就直接返回），
    # 不带 force 时它压根不会再走到弹窗那一行 —— 那测的就不是"去重"而是"没执行"。
    win.run_preflight(force=True)
    qapp.processEvents()
    again = [c for c in modal_calls if c["once_key"] == "startup-preflight"]
    assert len(again) == 2 and again[1]["shown"] is False, modal_calls


def test_user_action_failure_pops_every_time(window, qapp, modal_calls,
                                             monkeypatch) -> None:
    """**用户主动操作失败 → 每次都弹**：保存设置写盘失败、加自选失败各一条。

    判据是"不带 `once_key`"（`shown` 一直是 True）—— 他刚按下的那一下必须得到回应，
    哪怕同一类错误这一轮已经报过一次。
    """
    from laoa_trader import config as config_mod

    # ① 【保存设置】写盘失败（只读盘 / 权限）
    def boom(*_a, **_k):
        raise OSError("只读文件系统")

    monkeypatch.setattr(config_mod, "save_settings", boom)
    window.save_settings_button.click()
    qapp.processEvents()
    saves = [c for c in modal_calls if "保存" in c["title"]]
    assert saves and saves[-1]["shown"] is True, modal_calls
    assert saves[-1]["once_key"] == ""                       # 用户操作：不去重
    assert "下一步" in saves[-1]["shown_text"]

    # 再点一次 → **又弹一个**（用户操作与后台任务的区别就在这里）
    before = len(saves)
    window.save_settings_button.click()
    qapp.processEvents()
    saves = [c for c in modal_calls if "保存" in c["title"]]
    assert len(saves) == before + 1 and saves[-1]["shown"] is True

    # ② 【添加自选】失败
    from laoa_trader.data import storage

    def boom2(*_a, **_k):
        raise RuntimeError("库被占用")

    monkeypatch.setattr(storage, "upsert_watchlist", boom2)
    window.watch_symbol.setText("600519")
    window.on_watch_add()
    qapp.processEvents()
    added = [c for c in modal_calls if "加自选" in c["title"]]
    assert added and added[-1]["shown"] is True and added[-1]["once_key"] == ""
    assert "下一步" in added[-1]["shown_text"]
    # 状态栏仍然只写一句短的（弹窗只补充原因与下一步）
    assert "加自选失败" in window.status_label.fullText()


def test_background_failure_pops_only_once_per_run(window, qapp, modal_calls) -> None:
    """**同一类后台错误每次运行只弹一次**（`once_key` 去重）：网线一断不会每 5 秒一个框。

    走真实路径：`_on_worker_failed`（下载/刷新/筛选那些后台任务都用它）。
    """
    window._on_worker_failed("下载数据", "网络断开了")
    qapp.processEvents()
    window._on_worker_failed("下载数据", "网络断开了")
    qapp.processEvents()

    pops = [c for c in modal_calls if c["once_key"] == "worker:下载数据"]
    assert len(pops) == 2, modal_calls
    assert pops[0]["shown"] is True and pops[1]["shown"] is False
    assert "下一步" in pops[0]["shown_text"]
    assert len(pops[0]["shown_text"].splitlines()) <= 3
    # 另一类后台任务**照旧能弹**（去重是按"这一类"，不是"弹过一次就再也不弹"）
    window._on_worker_failed("刷新数据", "同花顺没响应")
    qapp.processEvents()
    other = [c for c in modal_calls if c["once_key"] == "worker:刷新数据"]
    assert other and other[0]["shown"] is True


def test_popup_texts_are_short_and_say_the_next_step(window, qapp, modal_calls) -> None:
    """弹窗正文的硬约束：**≤3 行、且必须带"下一步"**（主人："不要在弹窗里堆段落"）。"""
    from laoa_trader.ui import error_popup

    window._show_error("测试", "第一行\n第二行\n第三行\n第四行")
    qapp.processEvents()
    last = modal_calls[-1]
    assert len(last["shown_text"].splitlines()) <= error_popup.MAX_LINES
    assert last["shown_text"].endswith("…（其余原因见日志）")   # 多的丢掉，不堆段落


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
    必须同时说清"能看行情与大盘概览"和"匹配要 Key"，这两句是这条路径的全部信息量。
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
    assert "筛选要 Key" in win.key_hint.text()                   # 但匹配确实要它
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

    # 页脚 / 【立即刷新】都在这一页里
    # （2026-10-11 主人："常驻的'数据不足/去设置里下载'那种提示行…也删掉" ——
    #  原来夹在滚动区与页脚之间的 `market_hint` 已整行删除，取数失败改由弹窗说）
    assert not hasattr(window, "market_hint"), "那一行常驻提示应该已经删掉"
    assert page.isAncestorOf(window.market_footer) is True
    assert page.isAncestorOf(window.market_as_of_label) is True
    assert page.isAncestorOf(window.btn_market_refresh) is True
    assert window.btn_market_refresh.text() == "立即刷新"

    # 页面自上而下：可滚动的内容区 / 提示 / **最后一行**页脚。
    # 2026-09-21（主人要求）：最上面那行大字「大盘概览」**已删除** —— 与页签文字重复，
    # 字号预算让给了四个分区标题（见 `MARKET_SECTION_TITLE_DELTA`）。
    assert not hasattr(window, "market_title"), "那行大字标题应该已经删掉"
    layout = page.layout()
    assert layout.itemAt(0).widget() is window.market_scroll
    assert layout.itemAt(1).widget() is window.market_footer
    assert layout.itemAt(layout.count() - 1).widget() is window.market_footer

    # 内容区可滚动：窗口变矮时它自己吸收高度变化，页脚不会被顶出窗口
    assert isinstance(window.market_scroll, QScrollArea)
    assert window.market_scroll.widgetResizable() is True
    assert window.market_scroll.horizontalScrollBar().isVisible() is False

# ── 「数据来源」：**界面上只剩同花顺那一行**（2026-10-11 主人精简）──
#
# 主人原话："设置中，数据来源那里只保留同花顺那栏，可以填写同花顺KEY，其它都精简掉。"
# 这一组因此**没有任何"来源列表"**了：没有【添加来源】、没有【删除】、没有能力标签、
# 没有"列表顺序 = 优先级"的说明行。下面这几条钉的是**精简之后仍然成立的两件事**：
#   1. 界面**一个字都不改** `config.data_sources`（用户手写的顺序/来源名原样保留）；
#   2. 公开行情源**照旧是免 Key 的内部兜底**（同花顺没配 Key 时行情还得靠它）——
#      精简的是界面，不是功能。


def test_source_group_shows_only_hithink_and_keeps_the_public_fallback(
    window, qapp
) -> None:
    """界面上只有同花顺一行；但**公开源仍在链子里**（免 Key 兜底，功能没退化）。

    为什么这条必须有：这次改动最大的风险不是"少画了几个控件"，
    而是有人顺手把公开源从 `data_sources` 或注册表里也删了 —— 那一刀下去，
    没配 Key 的用户就**一点行情都取不到**（正是当初引入公开源要解决的问题）。
    """
    from laoa_trader.data import sources as sources_mod

    # ① 界面：只有一行（同花顺），带 Key 输入框
    assert list(window.source_rows) == [ui_app.BUILTIN_SOURCE]
    assert window.source_rows[ui_app.BUILTIN_SOURCE].key_edit is not None
    page = _tab_page(window, ui_app.TAB_SETTINGS)
    texts = [w.text() for w in page.findChildren(ui_app.QPushButton)]
    assert "添加来源" not in texts and "删除" not in texts
    # ② 后端：配置里那两个来源照旧、公开源照旧"现在就能用"（免 Key）
    assert window.cfg.data_sources == ["hithink", "public"]
    usable = [info.id for info in sources_mod.usable_sources(window.cfg)]
    assert "public" in usable
    # ③ 而且它真的在链子末尾等着（同花顺没 Key 时会被用到）
    assert [info.id for info in sources_mod.active_sources(window.cfg)] == [
        "hithink", "public",
    ]
    # 界面上的文字里不许再出现"添加 / 删除来源"这类操作（连提示行都没了）
    assert "添加来源" not in _settings_group_text(window, "数据来源")


def test_hithink_key_field_enters_the_one_click_save(window, seeded, qapp) -> None:
    """同花顺那一行的 Key 输入框**一键保存就会写回** `hithink_api_key`。

    这一行的输入口是用户唯一要动手的地方（2026-09-18 用户要回来的输入口），
    所以"填了没保存"是最该防的问题：键名由那一行自己的 `key_config` 给
    （不是代码里写死的特例），收集函数按"界面上有没有这个框"判。
    """
    row = window.source_rows[ui_app.BUILTIN_SOURCE]
    row.key_edit.setText("key-from-ui")
    updates = window._collect_settings_updates()
    assert updates["hithink_api_key"] == "key-from-ui"
    window.save_settings_button.click()
    qapp.processEvents()
    assert window.save_settings_hint.text().startswith("✅ 已保存")
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'hithink_api_key = "key-from-ui"' in text


def test_saving_settings_never_touches_a_hand_written_data_sources(
    window, seeded, qapp
) -> None:
    """**保存设置不动 `data_sources`** —— 逐字节比对那一行（用户手写的也不动）。

    为什么用"逐字节"而不是"值相等"：界面这次删掉了所有改来源的入口，
    这条用例就是那个结论的**取证**。值相等还不够 —— 顺手把用户写的注释抹掉
    （`_toml_value` 重新格式化数组、丢掉行尾注释）同样是他不想要的改动。

    所以这里特意写一行**带注释、带自定义顺序**的 `data_sources`，
    然后点一次【保存设置】：那一行必须一个字符都不变。
    """
    config_file = seeded.data_dir / "config.toml"
    hand_written = 'data_sources = ["public", "hithink"]   # 主人自己排的顺序\n'
    config_file.write_text(
        config_file.read_text(encoding="utf-8") + hand_written, encoding="utf-8"
    )
    before = config_file.read_text(encoding="utf-8")
    assert hand_written in before

    # 改一个**别的**键（证明这次保存真的写了东西 —— 否则"没动"没有意义）
    window.source_rows[ui_app.BUILTIN_SOURCE].key_edit.setText("k2")
    window.save_settings_button.click()
    qapp.processEvents()
    assert window.save_settings_hint.text().startswith("✅ 已保存")

    after = config_file.read_text(encoding="utf-8")
    assert after != before                                   # 确实写盘了
    assert hand_written in after                             # 但那一行一个字符没变
    assert after.count("data_sources") == before.count("data_sources")
    assert 'hithink_api_key = "k2"' in after                 # 该写的还是写了
    # 收集函数里根本没有这个键（"界面会不会改它"的唯一入口就是它）
    assert "data_sources" not in window._collect_settings_updates()


def test_window_opens_with_a_public_only_config_and_leaves_it_alone(
    seeded, qapp
) -> None:
    """老配置里**只有公开源**（`data_sources = ["public"]`）→ 窗口照开、照不改它。

    这类配置是真会存在的（用户在早先的版本里删掉过同花顺那一行）。界面精简之后
    必须做到两件事：窗口正常打开（不是"配置里没写同花顺就崩"），
    并且保存设置**不把它悄悄加回来** —— 用户没要求过的事，界面不该替他改。
    同花顺那一行的输入框照旧在（他随时可以填 Key，但填不填由他）。
    """
    seeded.data_sources = ["public"]
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert list(win.source_rows) == [ui_app.BUILTIN_SOURCE]
        assert win.source_rows[ui_app.BUILTIN_SOURCE].key_edit is not None
        config_file = seeded.data_dir / "config.toml"
        config_file.write_text(
            config_file.read_text(encoding="utf-8")
            + 'data_sources = ["public"]\n', encoding="utf-8")
        win.save_settings_button.click()
        qapp.processEvents()
        assert 'data_sources = ["public"]' in config_file.read_text(encoding="utf-8")
        assert seeded.data_sources == ["public"]             # 内存里那份也没被动过
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.scores.stop()
        win.shutdown()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


def test_source_group_builds_without_the_source_registry(
    seeded, qapp, monkeypatch, caplog
) -> None:
    """`data/sources.py` 读不出来（还没写好 / 它自己抛异常）→ **窗口照开、那一行照画**。

    一条设置页绝不该把整个程序拖死（真出问题时用户连界面都进不去，什么都问不出来）。
    2026-10-11 精简之后这条更简单了：那一行**是内置的**（名字、申请地址、
    要写的配置键全是本地常量），所以注册表读不出来也不影响它 ——
    但这条用例留着，因为它同时钉住"**Key 还是要能填、能存**"。
    """
    from laoa_trader.data import sources as sources_mod

    def boom(_cfg):
        raise RuntimeError("注册表炸了")

    monkeypatch.setattr(sources_mod, "source_states", boom)
    monkeypatch.setattr(sources_mod, "REGISTRY", {})
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    try:
        assert list(win.source_rows) == [ui_app.BUILTIN_SOURCE]
        row = win.source_rows[ui_app.BUILTIN_SOURCE]
        assert row.key_edit is not None                      # 输入口不依赖注册表
        assert "fuyao.aicubes.cn" in row.key_label.text()     # 申请地址也不依赖它
        assert row.key_label.openExternalLinks() is True
        row.key_edit.setText("k-without-registry")
        # 一键保存照样收这个键（键名是内置的，不靠注册表告诉它）
        assert win._collect_settings_updates()["hithink_api_key"] == "k-without-registry"
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.scores.stop()
        win.shutdown()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


def test_settings_tab_is_scrollable_so_the_window_can_shrink(window, qapp) -> None:
    """设置页控件最多，**整页套了滚动区**，否则它会把"窗口最小高度"顶到屏幕外面去。

    为什么这条值得测：四组控件的"最小高度"合起来 1000+，而页签的最小高度取
    所有页的最大值 —— 不套滚动区时窗口最小高度会顶到屏幕外（1366×768 的笔记本上
    根本放不下，表现就是"一打开最下边看不见"）。这条盯的是那个根因别再加回来。
    """
    from PySide6.QtWidgets import QScrollArea

    page = _tab_page(window, ui_app.TAB_SETTINGS)
    scrolls = page.findChildren(QScrollArea)
    assert scrolls, "设置页需要一层滚动区（小屏才缩得下来）"
    assert scrolls[0].widgetResizable() is True
    # 四组都在滚动区里面（顺序 = 用户给定的顺序，滚动区只是外套）
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
        # 「板块热力图」**不跟着概览那一趟**：它有自己的数据源与节拍（TTL 180 秒、
        # 只在自己那条节拍上拉，见 `_maybe_heatmap`），而这里封了网 → 那一轮必然失败。
        # 要钉的是"它如实说清自己什么状态"，而不是留一个空块让人猜。
        _wait_heatmap(win, qapp)
        hot = _heatmap_section(win)
        assert hot.title_label.text() == ui_app.MARKET_SECTION_HOT == "板块热力图"
        assert hot.isVisible() is True
        assert win.market_heatmap == []                      # 离线 → 没有数据
        assert hot.note_label.isVisible() is True
        # 没有数据时那行小字**必须说清是"没取到"还是"还没取过"**（两种说法都验一遍）
        assert "取数失败" in win._heatmap_note() and "口径：" not in win._heatmap_note()
        assert win.market_heatmap_error
        win.market_heatmap_error = ""
        assert "还没取过热力图数据" in win._heatmap_note()
        # 一有数据（这里直接喂构造好的 blocks）：面积最大的那一块就是半导体
        # —— seeded 库里有半导体涨停，热力图与概览对得上
        _feed_heatmap(win)
        assert [b["name"] for b in hot.widget.blocks][0] == "半导体"
        assert "更新于 —" not in win.market_as_of_label.fullText()   # 页脚已是真实取数时间
    finally:
        win._timer.stop()
        win._market_timer.stop()
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


def _heatmap_section(window):
    """「板块热力图」那一块（2026-10-08 起它取代了原来的上涨前五 / 下跌前五两张表）。"""
    return window.market_sections[ui_app.MARKET_SECTION_HOT]


def _heatmap_blocks() -> list[dict]:
    """一份**构造好的**热力图数据（`data/market_map.build_blocks()` 的真实产物）。

    为什么不从界面取数路径拿：测试环境 socket 被封死（见 `tests/conftest.py` 的
    `_block_network`），全市场快照必然取不到，而这里要验的是"**有数据时**热力图怎么画"。
    所以按真实契约造数据：面积 = 行业流通市值合计（亿）、颜色 = 市值加权涨跌幅。

    三个行业刻意各有代表性：半导体（涨、两块票、市值最大）、银行（跌）、白酒（平）；
    市值降序 = 半导体 800 > 银行 400 > 白酒 200（面积顺序就是它）。
    """
    from laoa_trader.data import market_map

    quotes = {
        "600002": {"pct": 6.0, "circ_mktcap": 500.0},      # 半导体甲：行业内市值最大
        "600003": {"pct": 2.0, "circ_mktcap": 300.0},      # 半导体乙
        "600001": {"pct": -1.0, "circ_mktcap": 400.0},     # 浦发样本
        "300001": {"pct": 0.0, "circ_mktcap": 200.0},      # 创业样本
    }
    industry = {"600002": "半导体", "600003": "半导体",
                "600001": "银行", "300001": "白酒"}
    names = {"600002": "半导体甲", "600003": "半导体乙",
             "600001": "浦发样本", "300001": "创业样本"}
    return market_map.build_blocks(quotes, industry, names=names)


def _feed_heatmap(window, blocks=None, *, at=None) -> list[dict]:
    """把构造好的 blocks 喂进「板块热力图」并重画（**走界面路径**：`market_heatmap` + 重画）。

    顺手把 `market_heatmap_error` / `market_heatmap_source` 清掉 —— 那正是
    `_on_heatmap_ready()` 取数成功时做的事：离线环境里后台那一轮必然失败，
    不清的话那行小字会同时挂着"最近一次取数失败"，口径与快照时间的断言就被搅浑了。
    """
    data = _heatmap_blocks() if blocks is None else blocks
    window.market_heatmap = list(data)
    window.market_heatmap_at = time.time() if at is None else at
    window.market_heatmap_error = ""
    window.market_heatmap_source = ""
    window._render_market_overview()
    return data


def _wait_heatmap(window, qapp, timeout_ms: int = 10_000) -> None:
    """等热力图那一轮**后台**取数落地（它有自己的节拍与线程，见 `_maybe_heatmap`）。

    与 `_wait_market` 一样是"等信号送进界面再断言"。测试环境封了网，所以它**必然**
    以"取数失败"收场 —— 这里等的是"失败已经如实写进界面"，不是"取到了数据"。
    """
    worker = getattr(window, "_heatmap_worker", None)
    if worker is not None:
        worker.wait(timeout_ms)
    qapp.processEvents()


def _heatmap_tip(window, block: dict) -> str:
    """某一块的 tooltip（每个数都该说清口径，见 `ui/heatmap.py` 的 `tip_for`）。"""
    return _heatmap_section(window).widget.tip_for(block)


def test_market_page_renders_stats_entries_colors_and_footer(market_window, qapp) -> None:
    """7 个小条目 + 两块指数的每一项 + 逐值颜色 + 板块热力图 + 页脚，一次全钉住。"""
    from laoa_trader import market
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import theme as theme_mod

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
        up = f"color:{theme_mod.semantic('up')}"
        down = f"color:{theme_mod.semantic('down')}"
        wide = market_window.market_sections[ui_app.MARKET_SECTION_WIDE].entries
        assert [_entry_colors(e) for e in wide] == [
            (down, down)] * 5 + [(up, up), (down, down)]
        sentiment = market_window.market_sections[ui_app.MARKET_SECTION_SENTIMENT].entries
        assert [_entry_colors(e) for e in sentiment] == (
            [(down, down)] + [(up, up)] * 3 + [(up, up), (down, down)]
        )
        assert not hasattr(market_window, "market_hint")            # 那行提示已删

        # 「板块热力图」：没有表头了，它把"每一列/每一档是什么意思"写进了 block 的 tooltip。
        # 原来那四列（板块名称 / 涨停数量 / 涨幅 / 主力净额）现在各有对应的说法，
        # 逐条钉住：涨跌幅是**市值加权**、面积是**流通市值合计**、涨跌家数仍来自本地那一套。
        _wait_heatmap(market_window, qapp)      # 离线那一轮先落地，免得跟断言抢状态
        hot = _heatmap_section(market_window)
        assert hot.isVisible() is True
        blocks = _feed_heatmap(market_window)
        # **面积顺序 = 市值降序**（用户是靠面积读权重的：大的必须更大）
        assert [b["name"] for b in blocks] == ["半导体", "银行", "白酒"]
        assert [b["mktcap"] for b in blocks] == [800.0, 400.0, 200.0]
        assert [b["name"] for b in hot.widget.blocks] == ["半导体", "银行", "白酒"]
        areas = [w * h for _, _, w, h in hot.widget._rects]
        assert areas == sorted(areas, reverse=True) and areas[0] > 0
        # **颜色 = 红涨绿跌**（半导体 +4.5% 红、银行 -1% 绿、白酒 0% 近白）
        from laoa_trader.data import market_map

        semi, bank, white = blocks
        assert semi["pct"] == 4.5 and bank["pct"] == -1.0 and white["pct"] == 0.0
        assert market_map.block_color(semi["pct"])[0] > market_map.block_color(semi["pct"])[1]
        assert market_map.block_color(bank["pct"])[1] > market_map.block_color(bank["pct"])[0]
        assert market_map.block_color(white["pct"]) == (238, 238, 238)
        # tooltip 里每个数都说清口径（原来"表头那四个字放不下解释"的问题，heatmap 里
        # 靠这一行行文字说清；用户在块上停一下就自己核对得了）
        semi_tip = _heatmap_tip(market_window, semi)
        assert "涨跌幅（市值加权）：+4.50%" in semi_tip
        assert "流通市值合计：800 亿" in semi_tip
        assert "面积 = 流通市值合计；颜色 = 涨跌幅，红涨绿跌" in semi_tip
        assert "最大的一只：半导体甲（+6.00%）" in semi_tip
        # 一张图不分"涨跌两张表"了：涨跌幅的方向由**颜色**说（同一个 tooltip 里），
        # 跌停家数只在真有跌停时才写一行 —— 这里本地没有跌停池数据，就一个字都不写
        # （一片"跌停家数：0"会把有用的那几行淹掉，编一个 0 更是假信息）
        assert "跌停家数" not in semi_tip
        # 涨停家数仍然来自**本地那一套**（`pool.hot_industries`，与原来那一列同源）
        assert "涨停家数：1" in semi_tip
        assert "涨停家数：0" in _heatmap_tip(market_window, bank)
        # 主力净额取不到（测试环境板块榜被封）→ tooltip 里**一个字都不写**，绝不编一个 0
        assert "主力净额" not in semi_tip
        # 那行小字（`note_label`）：口径 + 快照时间 + **缺什么**都写在上面。
        # 板块榜那一趟在测试环境里必然取不到（socket 封死）→ 退回**本地口径**：
        # 涨停家数还有（本地涨停池，与筛选同一套），缺的只有主力净额 ——
        # 这行小字就是原来那句"本地口径 / 主力净额取不到"的接任者，必须说准是**哪一项**。
        assert market_window.market_sectors["source"] == "local"
        hot_note = hot.note_label
        assert hot_note.isVisible() is True
        note_text = hot_note.fullText()
        # 2026-10-11 主人："把繁琐的说明文字删掉" —— 原来这句开头还有一段口径
        # （"面积 = 行业流通市值合计，颜色 = 市值加权涨跌幅（红涨绿跌，±10% 饱和）"），
        # 整段删掉：标题与图例里已经写着，这里只留**状态与原因**
        assert "面积 = 行业流通市值合计" not in note_text
        assert "颜色 = 市值加权涨跌幅" not in note_text
        assert re.search(r"快照 \d{2}-\d{2} \d{2}:\d{2}", note_text), note_text
        assert "3 个行业：涨 1 · 跌 1 · 平 1" in note_text
        assert "板块榜这一轮没取到：tooltip 里没有主力净额" in note_text
        assert "涨停家数用的是本地口径" in note_text

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
    from laoa_trader.ui import theme as theme_mod
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
        assert _entry_colors(up) == (f"color:{theme_mod.semantic('up')}",) * 2
        assert _entry_colors(down) == (f"color:{theme_mod.semantic('down')}",) * 2
        # 平盘：两个 label 都没有 color 样式（用界面默认色，不硬塞一个颜色）
        assert _entry_colors(flat) == ("", "")
        assert flat.pct_label.text() == "+0.00%"
        # 同一块里既有红又有绿 —— 逐值上色才做得到
        assert {_entry_colors(up)[0], _entry_colors(down)[0]} == {
            f"color:{theme_mod.semantic('up')}", f"color:{theme_mod.semantic('down')}",
        }
        # 语义色的**判据**只在 `market` 里定义一次；色值按主题给（见 theme.semantic）——
        # 浅色皮肤仍然是全项目核过的那两档，深色皮肤换成亮一档
        assert (market.COLOR_UP, market.COLOR_DOWN) == ("#d32f2f", "#2e7d32")
        theme_mod.apply_theme(None, "silver")
        assert theme_mod.semantic("up") == market.COLOR_UP
        assert theme_mod.semantic("down") == market.COLOR_DOWN
        theme_mod.apply_theme(None, "indigo")
        assert theme_mod.semantic("up") == theme_mod.DARK_SEMANTIC["up"]
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
        # 正常状态页面上只有这一行页脚（那行常驻提示已经删掉）
        assert not hasattr(win, "market_hint")
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
        hot = _heatmap_section(market_window)
        assert hot.isVisible() is True                   # 板块热力图不受指数配置影响
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
        # 掉的那只不再写进常驻提示行（那一行已删）；它由状态详情的概览那句如实带出
        assert "932000.TI" not in market_window._status_details()
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
        # 「板块热力图」的取数**不走同花顺 Key**：全市场快照走公开源、行业归属读本地表
        # （`market_map.industry_map`）—— 概览全灭时它照样画得出内容，这正是它的价值。
        # 这里没有 Key、socket 又被封死，那就用"本地行业表 + 一份快照"喂给它，
        # 钉住"画出来的就是 seeded 库里的真实行业"（半导体 / 银行），而不是靠联网。
        from laoa_trader.data import market_map

        hot = _heatmap_section(market_window)
        assert hot.isVisible() is True
        industry = market_map.industry_map(market_window.cfg.db_path)
        assert industry == {"600001": "银行", "600002": "半导体"}    # 本地表，没联网
        quotes = {"600002": {"pct": 6.0, "circ_mktcap": 500.0},
                  "600001": {"pct": -1.0, "circ_mktcap": 400.0}}
        blocks = market_map.build_blocks(quotes, industry)
        assert [b["name"] for b in blocks] == ["半导体", "银行"]     # 面积降序
        _feed_heatmap(market_window, blocks)
        assert [b["name"] for b in hot.widget.blocks] == ["半导体", "银行"]
        # 有数据时那行小字说的是口径与快照时间，不再说"还没取过"（热力图没有 `—` 占位那套：
        # 空态是画布上的一句话，见 `HeatmapWidget.paintEvent`）
        assert "还没取过热力图数据" not in hot.note_label.fullText()
        assert "个行业：涨" in hot.note_label.fullText()        # 状态（块数与涨跌家数）还在
        assert not hasattr(hot, "placeholder_label")
        # 常驻提示行已删；"没配 Key、现在走公开源"这条指路仍在页面 tooltip 里
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
        # 光看一个 `—` 用户猜不出为什么 —— 原因写在**数值格自己的 tooltip** 里
        # （常驻的那行解释文字已按主人要求删除，所以这里改从 tooltip 断言）
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

    sections = win.market_sections
    # 2026-09-21：页面里不再有"页标题"那一层，**四个分区标题升到最大一档**（黑体大字）
    group_font = sections[ui_app.MARKET_SECTION_WIDE].title_label.font()
    assert group_font.bold() is True
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
    # 「板块热力图」取代了原来那两张表：没有"名称列 / 数值列"可对齐了，
    # 而用户对表格提的那条要求（"所有名称显示不清楚，都加黑显示"）落在这里的等价物是
    # **可读性 = 字色与底色的对比**：块内文字颜色必须跟着底色深浅切换
    # （深色底白字、浅色底深字，见 `market_map.text_color`），否则字和底糊成一片。
    from laoa_trader.data import market_map

    hot = _heatmap_section(win)
    blocks = _feed_heatmap(win)
    assert hot.widget.blocks, "有数据才谈得上画得下"          # 原来那条"两块表非空"
    assert len(hot.widget._rects) == len(blocks)             # 每块都排到了地方
    for block, (_, _, width, height) in zip(hot.widget.blocks, hot.widget._rects):
        assert width > 0 and height > 0, block["name"]
        bg = market_map.block_color(block["pct"])
        fg = market_map.text_color(block["pct"])
        assert fg != bg, block["name"]                       # 字和底同色 = 看不见
        assert (fg == (255, 255, 255)) == (abs(block["pct"]) >= 4.5), block["name"]
    # 排版顺序也是一层"层级"：块标题 → 热力图 → 那行口径小字（说明在图下面，不挤标题里）
    box = hot.layout()
    assert box.indexOf(hot.widget) < box.indexOf(hot.note_label)

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
        assert "market_overview" in market_window.market_page.toolTip()
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
    for widget in (win.status_label, win.progress, win.tabs, win.market_footer):
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

        # 自选标的页：6 列表格不许把窗口顶宽 —— 它自己在内部横向滚动
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
    各 5 行；涨停数量与匹配用的是同一套口径（不另起一套）。
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


def test_the_unmatched_note_checks_the_column_each_table_actually_shows(monkeypatch) -> None:
    """页脚那句"哪些板块没对上"要按**每张表显示的那一列**判，不能两张表都查涨停家数。

    起因（2026-09-23 抓分发截图时发现的真 bug）：原来 `unmatched` 只查 `limit_up`，
    而"下跌前五"显示的是**跌停家数** —— 下跌那几个板块本来就没有涨停，于是页脚永远挂着
    "这些板块名在本地行业表里没有对应行业，数量列显示 —：交通运输、钢铁…"，
    **可它们的数字明明显示了**。这行小字是给用户解释"为什么有空列"的，说错比不说更糟。
    """
    from laoa_trader.ui import app as ui_app

    # 11 个板块：涨的 6 个 + 跌的 5 个 —— 这样两张表**不重叠**，
    # "上涨表看涨停家数、下跌表看跌停家数"这件事才验得出来
    up_names = [f"涨{i}" for i in range(6)]
    down_names = [f"跌{i}" for i in range(5)]
    rank = (
        [{"name": name, "pct": 3.0 - 0.1 * i, "main_net": 1e8} for i, name in enumerate(up_names)]
        + [{"name": name, "pct": -1.0 - 0.1 * i, "main_net": -1e8}
           for i, name in enumerate(down_names)]
    )

    class FakeSectors:
        def fetch_sector_rank(self, *args, **kwargs):
            return rank

    monkeypatch.setattr(ui_app, "sectors_module", lambda: FakeSectors())
    # 传一个带 `db_path` 的假配置：`sector_payload` 会拿它去算跌停家数
    cfg = types.SimpleNamespace(db_path="演示.db")
    industries = {name: {"limit_up": 3, "mom": 0.03} for name in up_names}
    down_counts = {name: 2 for name in down_names}

    monkeypatch.setattr(ui_app.pool, "limit_down_industries",
                        lambda *a, **k: down_counts, raising=False)
    payload = ui_app.sector_payload(cfg, industries)
    assert [row["name"] for row in payload["up"]] == up_names[:5]
    # 下跌前五按涨幅**升序**（跌得最狠的在最上面）
    assert [row["name"] for row in payload["down"]] == list(reversed(down_names))
    assert payload["up"][0]["limit_up"] == 3
    assert payload["down"][0]["limit_down"] == 2
    # 两列都有数 → 页脚**不许**再说"没对应行业"
    assert "没有对应行业" not in payload["note"]

    # 反过来：真有一列对不上（本地没有跌停家数）→ 这句话要出现，并且点名是哪个板块
    monkeypatch.setattr(ui_app.pool, "limit_down_industries",
                        lambda *a, **k: {}, raising=False)
    payload = ui_app.sector_payload(cfg, industries)
    assert "没有对应行业" in payload["note"] and down_names[-1] in payload["note"]
    assert payload["up"][0]["limit_up"] == 3          # 上涨那一列照样有数


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


def test_market_hot_block_is_a_real_heatmap_with_a_clear_caliber(market_window, qapp) -> None:
    """「板块热力图」块 = **一块自绘热力图**，每个数都说清口径（2026-10-08 主人要求换掉两张表）。

    为什么不再用"表格"：上涨前五 / 下跌前五两张表只有 10 行，**中间那一大片看不见**
    （横盘、微涨微跌的行业根本不上榜）；热力图一眼看全所有行业：面积给权重、颜色给涨跌。

    这条替代的是原来那条"两张真表 + 表头文字"的用例，重点钉三件事：
    **它是真的热力图控件**（不是又一张表）、**面积顺序 = 市值降序**、
    **每个数都在 tooltip 里写清了口径**（原来"表头只有四个字放不下解释"的那个问题，
    在这里是靠 tooltip 解决的）。口径：涨停家数仍来自本地那一套（`pool.hot_industries`）。
    """
    from laoa_trader import market, pool
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    win = market_window
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        _wait_heatmap(win, qapp)
        section = _heatmap_section(win)
        assert section.isVisible() is True
        assert section.title_label.text() == ui_app.MARKET_SECTION_HOT == "板块热力图"
        # 真的是自绘热力图控件（不是"又一张表"）：块列表 / tooltip 都在，
        # 而原来那两张表的入口（`tables` / 四列表头 / 那两个标题）**整个删掉了**
        assert not hasattr(section, "tables")
        for gone in ("SECTOR_UP_TITLE", "SECTOR_DOWN_TITLE", "SECTOR_TABLE_HEADERS",
                     "SECTOR_TABLE_HEADERS_DOWN", "SECTOR_HEADER_TIPS",
                     "SECTOR_COUNT_KEY_UP", "SECTOR_COUNT_KEY_DOWN"):
            assert not hasattr(ui_app, gone), f"两张表的残留常量没删干净：{gone}"
        assert type(section.widget).__name__ == "HeatmapWidget"
        assert callable(section.widget.set_blocks) and callable(section.widget.tip_for)
        assert section.widget.blocks == []                # 离线还没取到（它不是概览那一趟的东西）
        assert section.widget.extras                      # 但 extras 已就位（来自概览那趟的板块榜）
        # 三个按钮都在（刷新 / 放大 / 打开大盘云图），文字就是界面上的那几档
        assert (section.btn_refresh.text(), section.btn_zoom.text(),
                section.btn_site.text()) == ("刷新热力图", "放大", "打开大盘云图")
        assert ui_app.HEATMAP_TTL_SECONDS == 180          # 它自己的节拍（不跟概览那 60 秒）
        assert ui_app.HEATMAP_MIN_HEIGHT >= 200           # 概览页里那块的落地高度
        assert ui_app.MARKET_HEATMAP_SITE.startswith("https://")

        blocks = _feed_heatmap(win)
        # **面积顺序 = 市值降序**（每块的面积就是它的行业流通市值合计）
        assert [b["mktcap"] for b in blocks] == sorted(
            (b["mktcap"] for b in blocks), reverse=True)
        assert [b["name"] for b in section.widget.blocks] == [b["name"] for b in blocks]
        # 每个数都在 tooltip 里写清了口径 —— 逐个说法钉住（谁能自己核对，就不用信一个颜色）
        tip = section.widget.tip_for(blocks[0])
        assert "涨跌幅（市值加权）" in tip          # 不是等权、不是算术平均
        assert "流通市值合计" in tip                # 面积的口径
        assert "成分股：2 只" in tip                # 有几只票
        assert "最大的一只" in tip                  # 行业内最大的那只（跟名字一起给）
        assert tip.rstrip().endswith("（面积 = 流通市值合计；颜色 = 涨跌幅，红涨绿跌）")
        # 涨停家数用的是**本地那一套**（`pool.hot_industries`），不是另起一套
        expected = pool.hot_industries(win.cfg.db_path, top=ui_app.MARKET_HOT_TOP)
        extras = win._heatmap_extras()
        assert extras, "seeded 库里有涨停，板块榜这一趟该有内容"
        for name, record in extras.items():
            assert name in expected                       # 行业名对得上本地那一套
            assert record["limit_up"] == expected[name]["limit_up"]
        assert extras["半导体"]["limit_up"] == 1
        assert "涨停家数：1" in section.widget.tip_for(
            next(b for b in blocks if b["name"] == "半导体"))
        # 取不到的那一列（主力净额，测试环境板块榜被封）**一个字都不写**，不编 0
        assert all(record["main_net"] is None for record in extras.values())
        assert "主力净额" not in tip
    finally:
        market.clear_cache()


def test_market_hot_block_explains_itself_when_the_sector_rank_is_missing(
    market_window, qapp, monkeypatch
) -> None:
    """本地没有涨停池数据 + 板块榜也取不到 → 热力图照画 + **页面上写出怎么补**。

    不能只留一个空块：用户会以为"这一块本来就不显示东西"，而实际是数据还没下。
    热力图的面积与颜色走全市场快照（与板块榜无关），所以**它照样画得出来**；
    缺的那一项（主力净额，以及没有本地涨停池时的涨停家数）要在那行小字里点明，
    而不是让人以为"没涨停"。
    """
    from laoa_trader import market, pool
    from laoa_trader.ui import app as ui_app

    market.clear_cache()
    try:
        monkeypatch.setattr(pool, "hot_industries", lambda *a, **k: {})
        monkeypatch.setattr(ui_app, "sectors_module", lambda: None)
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        _wait_heatmap(market_window, qapp)
        section = _heatmap_section(market_window)
        # 板块榜这一轮是空的 → 供热力图 tooltip 用的 extras 也必然是空的
        assert (market_window.market_sectors or {}).get("up") in (None, [])
        assert market_window._heatmap_extras() == {}
        blocks = _feed_heatmap(market_window)
        # 热力图照样有内容（面积/颜色不依赖板块榜），只是 tooltip 里少那两项
        assert [b["name"] for b in section.widget.blocks] == [b["name"] for b in blocks]
        tip = section.widget.tip_for(blocks[0])
        assert "涨停家数" not in tip and "主力净额" not in tip
        # 而那行小字必须**说清为什么少**（"没取到" ≠ "没有涨停"），口径那几句照旧在。
        # 措辞按分支不同：退回**本地口径**时涨停家数还在（本地涨停池），缺的只有主力净额；
        # 两个分支的共用说法是"板块榜这一轮没取到"，缺的那一项一定点名。
        note = section.note_label.fullText()
        assert "板块榜这一轮没取到" in note
        assert "主力净额" in note
        assert "个行业：涨" in note                              # 状态照旧，口径那几句已删
        assert tip.rstrip().endswith("（面积 = 流通市值合计；颜色 = 涨跌幅，红涨绿跌）")
        # 那行常驻提示（"本地还没有涨停池数据，点【刷新数据】补齐"）已按主人要求删除；
        # "为什么没有涨停家数"仍写在热力图下面那行小字里（状态与原因，主人要求保留）
        assert not hasattr(market_window, "market_hint")
        assert "板块榜这一轮没取到" in market_window._heatmap_note()
    finally:
        market.clear_cache()


def test_heatmap_note_states_its_caliber_and_the_snapshot_time(market_window, qapp) -> None:
    """那行小字（`note_label`）三种状态各说各的话 —— **口径、快照时间、缺什么**。

    它是这一块唯一的文字说明（原来两张表靠四条表头 + 一行说明），所以三种状态都要说清：
    ① 还没取过 → 指路【刷新热力图】；② 取数失败 → 写出失败原因；③ 有数据 → 口径 + 快照
    MM-DD HH:MM；板块榜这一轮没取到时再补一句（否则用户会以为"没有涨停"）。
    """
    from laoa_trader import market

    market.clear_cache()
    win = market_window
    try:
        _wait_heatmap(win, qapp)
        hot = _heatmap_section(win)
        # ① 还没取过（离线建窗口时后台那一轮还没落地 / 缓存也没有）
        win.market_heatmap, win.market_heatmap_at, win.market_heatmap_error = [], 0.0, ""
        note = win._heatmap_note()
        assert "还没取过热力图数据" in note and "刷新热力图" in note
        assert "口径：" not in note
        # ② 取数失败：原因是**原样写出来**的（不是一句"加载失败"）
        win.market_heatmap_error = "RuntimeError: 全市场快照没取到（网络/限流/来源都不可用）"
        assert win._heatmap_note().startswith("⚠️ 取数失败：")
        assert "全市场快照没取到" in win._heatmap_note()
        # ③ 有数据：口径 + 分档 + 快照时间 + 涨跌平各几块
        at = time.time() - 60
        blocks = _feed_heatmap(win, at=at)
        note = win._heatmap_note()
        assert "3 个行业：涨 1 · 跌 1 · 平 1" in note
        # 口径那两句已按主人要求删除（"本地口径"那几个字是另一回事，别误伤）
        assert "面积 = 行业流通市值合计" not in note and "颜色 = 市值加权涨跌幅" not in note
        assert re.search(r"快照 \d{2}-\d{2} \d{2}:\d{2}", note), note
        assert "已过期" not in note                      # 一分钟前的快照还不算旧
        # 快照时间取的是**真实时间**（相差一分钟 → 显示的分钟数就该是那个）
        assert time.strftime("%m-%d %H:%M", time.localtime(at)) in note
        # 那行小字显示的就是 `_heatmap_note()` 的产物（一处生成，页面小字 / 放大窗口 /
        # tooltip 三处共用，不许各算各的）
        assert hot.note_label.fullText() == note
        assert hot.note_label.toolTip() == note
        # ④ 板块榜这一轮退回了**本地口径** → 说清缺的是哪一项（涨停家数还在，缺的是主力净额）
        win.market_sectors = {"up": [], "down": [], "source": "local", "note": "x"}
        win._render_market_overview()
        note = win._heatmap_note()
        assert "板块榜这一轮没取到" in note and "主力净额" in note
        assert "涨停家数用的是本地口径" in note
        # ④b 板块榜整个没取到（连本地那份都没有）→ 那一句话要升级：两项都别指望
        win.market_sectors = {"up": [], "down": [], "source": "sectors", "note": "x"}
        win._render_market_overview()
        assert "板块榜这一轮没取到：鼠标停在块上看不到涨停家数与主力净额" \
            in win._heatmap_note()
        # ⑤ 板块榜回来了 → 那句话收掉（说错比不说更糟）
        win.market_sectors = {"up": [{"name": "半导体", "limit_up": 1, "main_net": None}],
                              "source": "sectors"}
        win._render_market_overview()
        assert "板块榜这一轮没取到" not in win._heatmap_note()
        assert [b["name"] for b in hot.widget.blocks] == [b["name"] for b in blocks]
        # ⑥ 快照的**出处**也要写出来（价格与市值可能来自两个来源，见 `quotes_source_text`）
        win.market_heatmap_source = "同花顺金融数据服务（市值由 公开行情源 补）"
        win._render_market_overview()
        assert "来源 同花顺金融数据服务（市值由 公开行情源 补）" in win._heatmap_note()
        assert hot.note_label.fullText() == win._heatmap_note()   # 小字跟着一起重画
    finally:
        market.clear_cache()


def test_heatmap_tip_shows_sector_rank_extras_with_units(market_window, qapp) -> None:
    """tooltip 里"板块榜那三项"的**单位与显示条件**：涨停家数 / 跌停家数 / 主力净额（亿）。

    这三项只有板块榜有（面积与颜色走全市场快照），而板块榜在测试环境里必然取不到，
    所以这里按控件自己的接口喂一份 extras（`hot.set_blocks(blocks, extras, note=…)`）——
    要钉的是"合流之后每个数的单位对不对、没有的行不写"，不是"有没有联网取到"。
    单位：两个家数是**只数**；`main_net` 在数据层是**元**，tooltip 显示前 ÷1e8 换成"亿"
    （`sector_rank_tables` 的口径），差 1e8 倍是最容易犯又最看不出来的错。
    """
    from laoa_trader import market

    market.clear_cache()
    try:
        hot = _heatmap_section(market_window)
        blocks = _heatmap_blocks()
        extras = {
            "半导体": {"limit_up": 3, "limit_down": 2, "main_net": 12.5e8},
            "银行": {"limit_up": 0, "limit_down": None, "main_net": None},
        }
        hot.set_blocks(blocks, extras, note="口径：面积 = 行业流通市值合计")
        semi_tip = hot.widget.tip_for(hot.widget.blocks[0])
        assert "涨停家数：3" in semi_tip
        assert "跌停家数：2" in semi_tip
        assert "主力净额：+12.50 亿" in semi_tip        # 12.5e8 元 = 12.5 亿（不是 1250000000 亿）
        bank_tip = hot.widget.tip_for(hot.widget.blocks[1])
        # 0 家涨停要写（"真的没有"是有用的信息）；跌停 0 / 主力净额取不到就不写这一行
        assert "涨停家数：0" in bank_tip
        assert "跌停家数" not in bank_tip and "主力净额" not in bank_tip
        # 口径那行小字照旧由调用方给（这一条不关心它）
        assert hot.note_label.fullText() == "口径：面积 = 行业流通市值合计"
        # 板块榜那几项**不进面积、也不进颜色**：面积还是市值、颜色还是涨跌幅
        assert [b["mktcap"] for b in hot.widget.blocks] == [800.0, 400.0, 200.0]
        assert blocks[0]["pct"] == 4.5
    finally:
        market.clear_cache()


def test_heatmap_zoom_opens_a_window_with_its_own_chart(market_window, qapp) -> None:
    """【放大】→ 独立窗口里有一张热力图（同一份数据、同一套口径，只是画布大）。"""
    from laoa_trader import market

    market.clear_cache()
    win = market_window
    try:
        _wait_heatmap(win, qapp)
        blocks = _feed_heatmap(win)
        window = win.show_heatmap_window()
        qapp.processEvents()

        assert window is not None and window.isVisible() is True
        assert type(window._chart).__name__ == "HeatmapWidget"
        assert window._chart.compact is False               # 大图：不截断行业名
        assert window._chart.minimumHeight() > 0
        assert [b["name"] for b in window._chart.blocks] == [b["name"] for b in blocks]
        assert window._chart.extras                          # 涨停家数等 extras 一起带过来
        assert "个行业：涨" in window._note.text()
        assert "板块热力图" in window.windowTitle()
        # 同一个窗口不重复建（连点两次不该堆出一摞）
        assert win.show_heatmap_window() is window
        assert sum(1 for w in qapp.topLevelWidgets() if w is window) == 1
        # 窗口里也有【刷新】与【打开大盘云图】两个入口（与概览页同一套方法）
        from PySide6.QtWidgets import QPushButton

        texts = [b.text() for b in window.findChildren(QPushButton)]
        assert "刷新" in texts and "打开大盘云图" in texts
        window.close()
        qapp.processEvents()
        assert window.isVisible() is False
    finally:
        market.clear_cache()


def test_heatmap_zoom_and_refresh_buttons_are_wired(market_window, qapp) -> None:
    """【放大】/【刷新热力图】真的接上了（点按钮，不是调方法）——离线时如实报失败。

    【刷新热力图】那条尤其值得点一遍：它的意义是"忽略 TTL 立刻重拉一轮"，而测试环境
    封了网 → 这一轮**必然失败**。要钉的就是"失败被如实写到那行小字上、界面照常可用"，
    而不是静默什么都不发生（用户点了没反应，是最难查的一类问题）。
    """
    from laoa_trader import market

    market.clear_cache()
    win = market_window
    try:
        _wait_heatmap(win, qapp)
        hot = _heatmap_section(win)
        # 【放大】按钮 = `show_heatmap_window`
        assert win._heatmap_window is None
        hot.btn_zoom.click()
        qapp.processEvents()
        assert win._heatmap_window is not None
        assert win._heatmap_window.isVisible() is True
        assert win._heatmap_window._chart is not None        # 窗口里确实有图
        win._heatmap_window.close()
        qapp.processEvents()

        # 【刷新热力图】按钮 = `on_refresh_heatmap`（忽略 TTL 真去取一轮）
        win.market_heatmap_error = ""
        win.market_heatmap, win.market_heatmap_at = [], 0.0
        win._heatmap_ts = 0.0
        hot.btn_refresh.click()
        assert time.time() - win._heatmap_ts < 5, "点了刷新就该立刻起一轮取数"
        _wait_heatmap(win, qapp)
        assert win.market_heatmap_error, "离线取数必然失败，且必须如实记下来"
        assert "取数失败" in win._heatmap_note()
        assert hot.note_label.isVisible() is True
        # 失败不影响界面其余部分：概览页照常能用
        win._tick()
        qapp.processEvents()
        assert win.pool_table.rowCount() == 1
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
        # 「板块热力图」没有"列宽"这回事了，窄屏上的等价要求是：
        # ① 块本身不比视口宽（不横向溢出）；② 每一块的矩形都落在自己的画布内
        # （越界就会被裁掉一半，看着像少了个行业）；③ 每块都大到写得下名字。
        from laoa_trader.ui import heatmap as heatmap_mod

        hot = _heatmap_section(win)
        _wait_heatmap(win, qapp)
        blocks = _feed_heatmap(win)
        canvas_w, canvas_h = hot.widget.width(), hot.widget.height()
        assert canvas_w <= win.market_scroll.viewport().width()
        assert hot.widget.minimumHeight() == ui_app.HEATMAP_MIN_HEIGHT >= 240
        assert len(hot.widget._rects) == len(blocks)
        for _, _, width, height in hot.widget._rects:
            assert 0 < width <= canvas_w and 0 < height <= canvas_h
        readable = [rect for rect in hot.widget._rects
                    if rect[2] >= heatmap_mod.MIN_TEXT_W and rect[3] >= heatmap_mod.MIN_TEXT_H]
        assert len(readable) == len(blocks), "有数据时每一块都该写得下名字"
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

def test_pool_row_tooltip_has_no_evidence_marker(pool_window, qapp, monkeypatch) -> None:
    """行的 tooltip 里**不再有**「（依赖开盘）」或"策略照常推送"那段解释。

    那个标记来自 `rules.STRATEGIES[类名].evidence`（2026-09-18 随策略引擎删掉）。
    老库里的历史行（带着类名）照旧显示中文名与来源，但不再有证据标记 ——
    它现在是"我们自己的策略有没有边际"的旧说法，公式标的本来就不该被贴我们的结论。
    """
    from laoa_trader import pool as pool_mod

    rows = [
        {"symbol": "600002", "name": "半导体甲", "strategy": "DryUpExpansionStrategy",
         "strategies": "DryUpExpansionStrategy", "score": 2.0, "reason": "地量后放量",
         "label": "地量后放量变盘", "source_label": "地量后放量变盘",
         "industry": "半导体", "note": "", "source": "策略",
         "is_limit_up": False, "continue_day_text": "", "limit_up_reason": ""},
    ]
    monkeypatch.setattr(pool_mod, "pool_page_rows", lambda db_path, day=None: rows)
    window = pool_window
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()

    tip = window.pool_table.item(0, 0).toolTip()
    assert "来源：地量后放量变盘" in tip      # 历史行的中文名照旧
    for gone in ("依赖开盘", "push_only_proven", "策略照常推送", "组别："):
        assert gone not in tip, f"tooltip 里还留着「{gone}」"



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
    # 单元格 tooltip 现在是**一句短的**（值 + 快照时间）；"不是总市值"那句解释已按主人要求删除，
    # 表头 tooltip 里仍旧写着这一列是**流通市值**（口径没丢，只是不再啰嗦）
    assert window.position_table.item(0, p_cap).toolTip().startswith("流通市值 456.78 亿")


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
    # 真实作用：不再进做T/盘中提醒的观察面（与右键【关闭监控】完全一致）；
    # 操作提示本身不再弹（2026-09-18）
    assert list(intraday.held_positions(seeded.db_path)) == []
    assert "关闭监控" not in window.status_label.fullText()

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


def test_watch_table_shows_added_date_and_pnl_from_the_added_price(
    window, seeded, qapp
) -> None:
    """「加入日期」+「盈亏」两列（2026-09-21 主人要求："盈亏从加入股池那天算"）。

    判据：加入价写 5.00，盈亏 =（本地最新收盘价 − 5.00）÷ 5.00（期望值由测试自己从库里
    读收盘价算，不写死数字 —— 换一套 `seeded` 数据也不会假红）；
    日期那一格是加入那天（`added_at` 的日期部分）；**老数据（没有加入价）显示 `—`**。
    """
    from laoa_trader.data import storage as st
    from laoa_trader.ui import theme as theme_mod

    table = window.pool_table
    with st.connect(seeded.db_path) as conn:
        st.upsert_watchlist(conn, "600001", name="低价样本", price=5.0)
        st.upsert_watchlist(conn, "600003", name="老数据样本")     # 没给价 → added_price 为空
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = {_symbols_of(table)[i]: i for i in range(table.rowCount())}
    added_col = ui_app.WATCH_ADDED_COLUMN
    pnl_col = ui_app.WATCH_PNL_COLUMN

    # 加入日期：`added_at` 是北京时间的时间戳，这一格只要日期（形如 2026-09-21）
    added_text = table.item(rows["600001"], added_col).text()
    assert len(added_text) == 10 and added_text[4] == "-" and added_text[7] == "-"
    # 盈亏：按加入价 5.00 与本地最新收盘价算（期望值现算，不写死）
    with st.connect(seeded.db_path) as conn:
        close = conn.execute(
            "SELECT close FROM stock_daily_raw WHERE symbol = '600001' "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()[0]
    expected = (float(close) - 5.0) / 5.0 * 100
    pnl_text = table.item(rows["600001"], pnl_col).text()
    assert pnl_text.endswith("%")
    assert abs(float(pnl_text.rstrip("%")) - expected) < 0.01
    assert "加入价 5.00" in table.item(rows["600001"], pnl_col).toolTip()
    # 颜色沿用红涨绿跌：这只票是亏的 → 绿
    assert table.item(rows["600001"], pnl_col).foreground().color().name() \
        == ui_app.QColor(theme_mod.semantic("down")).name()

    # 老数据：两格都是 `—`（不拿今天顶替、也不假装 0%）
    assert table.item(rows["600003"], pnl_col).text() == ui_app.market.DASH
    assert "算不出盈亏" in table.item(rows["600003"], pnl_col).toolTip()


def test_monitor_column_click_toggles_watchlist_and_explains_for_strategy_rows(
    window, seeded, qapp
) -> None:
    """自选行点一下就能开关监控；**匹配选出来的票默认不盯**，点了就是"收下它、开始盯"。

    2026-10-05 主人："默认只监控持仓股票。" —— 所以策略行的默认文字改成 `关闭`，
    点一下会把它写进自选表并打开监控（以前这里给的是"不能在这里单独关掉监控"，
    因为那时候池子里的每一行都自动盯着）。"""
    from laoa_trader.data import storage as st

    column = ui_app.WATCH_MONITOR_COLUMN
    table = window.pool_table
    with st.connect(seeded.db_path) as conn:
        st.upsert_watchlist(conn, "600001", name="低价样本", note="", enabled=True)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    rows = {_symbols_of(table)[i]: i for i in range(table.rowCount())}
    assert set(rows) == {"600002", "600001"}

    # 匹配选出的票（600002）：默认 `关闭`；点一下就收进来盯着
    assert table.item(rows["600002"], column).text() == ui_app.MONITOR_OFF_TEXT
    window._on_table_cell_clicked(table, rows["600002"], column)
    qapp.processEvents()
    with st.connect(seeded.db_path) as conn:
        assert st.watchlist_map(conn)["600002"]["enabled"] == 1
    assert table.item(rows["600002"], column).text() == ui_app.MONITOR_ON_TEXT

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


def test_pipeline_status_explains_why_nothing_was_pushed(window, qapp) -> None:
    """状态栏要能说明"为什么没推"—— 现在只有一种原因：**同一批内容今天已经推过**。

    （2026-09-18 之前还有"命中的全是依赖开盘的策略 → 一条都不推"那一路，
    它随 Python 策略引擎一起删掉了；老报告里带着 `push_skipped_kind="filtered"`
    也不该让界面出错 —— 那种报告只可能来自老版本，这里顺手钉住容错。）
    """
    window._on_pipeline_done("开始筛选", {
        "data_date": "2026-09-11",
        "pool": [{"symbol": "600001"}], "picks": 2, "signals": 2,
        "pushed": False,
        "push_skipped": "2026-09-11 已推送过相同内容的池子（指纹 abc）",
        "push_skipped_kind": "duplicate",
    })
    qapp.processEvents()
    text = window.status_label.fullText()
    assert "未重复推送" in text

    # 老版本报告里的 filtered 原因：界面照旧把它显示出来（不崩、不吞）
    window._on_pipeline_done("开始筛选", {
        "pool": [{"symbol": "600002"}], "picks": 1, "signals": 1, "pushed": False,
        "push_skipped": "（老版本）另有 1 只只由「依赖开盘」的策略选出",
        "push_skipped_kind": "filtered",
    })
    qapp.processEvents()
    # `filtered` 这个 kind 已经不会产生（策略引擎删了），界面把它当"未重复推送"显示 ——
    # 关键是**不许崩、不许静默**，真正的原因那句还在 push_skipped 里
    assert "未重复推送" in window.status_label.fullText()


def test_deleting_a_watchlist_row_that_is_also_in_todays_pool_removes_it_now(
    window, seeded, qapp,
) -> None:
    """**自选行同时也躺在今日池子里**时，删了要立刻从表里消失（2026-10-08 主人实报的 bug）。

    为什么会这样：自选标的会被并进 `stock_pool`（盘中监控按它盯），所以"删自选"之后
    池子里那一行还在 —— 界面读的正是池子行，于是**点了删除、那一行纹丝不动**，
    用户看到的就是"删除不生效"。原来那条用例造的场景是"自选不在池子里"
    （`seeded` 库的池子只有 600002），刚好绕开了这个组合。

    判据：① 自选表里没了；② **今日池子里也没了**；③ 表里那一行当场消失。
    """
    from laoa_trader import pool as pool_mod

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", enabled=True)
    # 造出"自选已经被并进今日池子"的真实状态（与建池后的库一模一样：
    # 自选行的 `strategy` 是空的，来源因此显示「自选」）
    with storage.connect(seeded.db_path) as conn:
        row = conn.execute("SELECT MAX(date) FROM stock_pool").fetchone()
    day = row[0] if row and row[0] else "2026-09-11"
    pool_mod.save_pool(seeded.db_path, [{
        "symbol": "600001", "name": "低价样本", "strategy": "", "strategies": "",
        "score": None, "reason": "自选标的",
    }], day=day)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    assert "600001" in _symbols_of(window.pool_table)

    window.on_pool_row_delete("600001")
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_map(conn) == {}                       # ① 自选删掉了
    assert "600001" not in pool_mod.pool_symbols(seeded.db_path)       # ② 今日池子也删掉了
    assert "600001" not in _symbols_of(window.pool_table)              # ③ 表里当场就没了


def test_deleting_a_symbol_added_from_the_result_page_removes_it_now(
    window, seeded, qapp,
) -> None:
    """从筛选结果页点【加入自选】的票：删了也要**当场消失**（2026-10-08 实报的那个 bug 的另一半）。

    这一类带着"当初是哪条策略选出来的"（`watchlist.source_strategy`），所以池子行里
    **也有策略名**。按"有没有策略名"判"今天被没被选中"会把它当成策略标的留下来，
    于是删除又变成没反应 —— 判据只能是"这一行是不是自选来源的行"。
    """
    from laoa_trader import pool as pool_mod

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本", enabled=True,
                                 source_strategy="公式·尾盘匹配策略")
    row = None
    with storage.connect(seeded.db_path) as conn:
        row = conn.execute("SELECT MAX(date) FROM stock_pool").fetchone()
    day = row[0] if row and row[0] else "2026-09-11"
    pool_mod.save_pool(seeded.db_path, [{
        "symbol": "600001", "name": "低价样本", "strategy": "公式·尾盘匹配策略",
        "strategies": "公式·尾盘匹配策略", "score": 0.5, "reason": "自选",
    }], day=day)
    window._pool_signature = None
    window._refresh_pool_table()
    qapp.processEvents()
    assert "600001" in _symbols_of(window.pool_table)

    window.on_pool_row_delete("600001")
    qapp.processEvents()

    assert "600001" not in _symbols_of(window.pool_table)
    assert "600001" not in pool_mod.pool_symbols(seeded.db_path)


# ── 按钮**不许被拉长铺满**（主人 2026-10-11 实报的那一类回归）──
#
# 症状与根因（主人原话："说明内容去掉以后，策略选股页的 3 个按钮都拉长铺满了"）：
# 一行里原来右侧挂着一句灰字说明，**多出来的宽度被它吃掉了**；界面文字瘦身把说明删掉
# 之后，那一行没有任何东西再去吃多余宽度，于是宽度摊到剩下的控件上 —— 按钮被拉成
# 等宽、铺满整行（实测：`策略编辑` 宽 385 / 自然宽 80；`复制路径` 宽 674 / 自然宽 74）。
#
# 所以判据只有一个：**渲染出来的宽度不许明显超过它的自然宽度**
# （`width() <= sizeHint().width() + 12`）—— 只断言"代码里有 addStretch"是拦不住回归的
# （把 `addStretch(1)` 删掉、那条断言照样绿）。


def _stretched_buttons(container) -> list[tuple[str, int, int]]:
    """容器里**被拉长**的按钮：`(文字, 实际宽, 自然宽)`（空列表 = 都保持自然宽度）。"""
    from PySide6.QtWidgets import QPushButton

    out: list[tuple[str, int, int]] = []
    for button in container.findChildren(QPushButton):
        if not button.isVisible():
            continue                      # 没在当前页签上的控件量不到宽度
        natural = button.sizeHint().width()
        if natural <= 0:
            continue
        if button.width() > natural + 12:
            out.append((button.text(), button.width(), natural))
    return out


def test_settings_rows_do_not_stretch_their_buttons(window, qapp) -> None:
    """「系统设置」各组里的按钮**保持自然宽度**（含【复制路径】那一个）。

    2026-10-11 修的：`复制路径` 原来直接挂在竖直布局上（`body.addWidget(btn)`），
    于是它撑满整行 —— 实测宽 674、自然宽只有 74，与主人报的"按钮拉长铺满"同一种病。
    现在它在自己的行里、行尾有 `addStretch(1)`。
    """
    settings_page = _tab_page(window, ui_app.TAB_SETTINGS)
    window.tabs.setCurrentWidget(settings_page)     # 非当前页签量不到宽度
    window.resize(1280, 820)
    qapp.processEvents()

    assert window.btn_copy_paths.isVisible() is True
    assert window.btn_copy_paths.width() <= window.btn_copy_paths.sizeHint().width() + 12
    assert _stretched_buttons(settings_page) == [], _stretched_buttons(settings_page)


def test_no_visible_button_is_stretched_in_any_tab(window, qapp) -> None:
    """**通用护栏**：每一页（含「策略筛选」的两个子页）里都不许有被拉长的按钮。

    逐个页签量渲染宽度，而不是去 grep `addStretch`：这条会在"删掉某行末尾的说明文字"
    或者"删掉 `addStretch(1)`"时立刻变红，正是主人这次实报的那类回归。
    窗口先调到 1280 宽 —— 行里得有剩余宽度，才有"被拉长"这回事。
    """
    window.resize(1280, 820)
    qapp.processEvents()
    problems: list[str] = []
    for i in range(window.tabs.count()):
        window.tabs.setCurrentIndex(i)
        qapp.processEvents()
        window._tick()
        qapp.processEvents()
        for text, width, natural in _stretched_buttons(window.tabs.widget(i)):
            problems.append(f"{window.tabs.tabText(i)}：{text!r} 宽={width} 自然={natural}")
    # 页内的两个子页（策略列表 / 筛选结果）也要各量一遍
    formula_page = window.formula_page
    for attr in ("list_stack", "bottom_stack"):
        stack = getattr(formula_page, attr, None)
        if stack is None:
            continue
        for i in range(stack.count()):
            stack.setCurrentIndex(i)
            qapp.processEvents()
            for text, width, natural in _stretched_buttons(stack.widget(i)):
                problems.append(f"{attr}[{i}]：{text!r} 宽={width} 自然={natural}")
    assert problems == [], problems


def test_other_windows_do_not_stretch_their_buttons(window, seeded, qapp) -> None:
    """**对话框与顶层窗口**里的按钮同样不许被拉长（它们的排法与页签不同）。

    覆盖：消息中心 / 提醒浮窗 / 桌宠 / 授权对话框 / 成绩单对话框 —— 这几张都不在页签里，
    上面那条通用护栏看不到它们，而它们清一色是"左边几个按钮 + 右边【关闭】"的排法：
    中间的 `addStretch(1)` 一旦被删掉，整排按钮就会一起被拉宽（正是要防的那类回归）。
    """
    from laoa_trader.ui.alert_popup import AlertPopup
    from laoa_trader.ui.desktop_pet import DesktopPet
    from laoa_trader.ui.license_dialog import LicenseDialog
    from laoa_trader.ui.message_center import MessageCenter
    from laoa_trader.ui.scorecard_dialog import ScorecardDialog

    problems: list[str] = []
    made: list = []
    try:
        center = MessageCenter(window)
        center.set_messages([{"symbol": "600001", "name": "低价样本", "kind": "选股结果",
                              "detail": "测试", "at": "2026-09-11 09:35:00"}])
        made.append(("消息中心", center))
        made.append(("浮窗", AlertPopup()))
        made.append(("桌宠", DesktopPet()))
        made.append(("授权对话框", LicenseDialog(None, None)))
        made.append(("成绩单", ScorecardDialog(None, cfg=window.cfg,
                                              targets=[("样本策略", "C>0")],
                                              db_path=seeded.db_path)))
        for name, widget in made:
            widget.show()
            qapp.processEvents()
            window.resize(1280, 820)
            for text, width, natural in _stretched_buttons(widget):
                problems.append(f"{name}：{text!r} 宽={width} 自然={natural}")
    finally:
        # 收尾要**等线程真的退出**（授权窗口在读机器码、成绩单在预检）：留着在飞的
        # `QThread` 会打到已销毁的控件上，后面的用例会成倍变慢
        for _name, widget in made:
            for method in ("shutdown", "_stop_threads"):
                stop = getattr(widget, method, None)
                if callable(stop):
                    stop()
            widget.close()
            widget.deleteLater()
        qapp.processEvents()
    assert problems == [], problems


def test_formula_tab_fallback_run_button_does_not_stretch(
    seeded, qapp, monkeypatch
) -> None:
    """兜底那条【开始筛选】也不许被拉长（它那一行的说明文字同样被删掉过）。

    `MainWindow._build_formula_tab` 里有一段**兜底分支**：页面没有 `btn_start_pick`
    时（中间的半成品状态 / 将来页面改版）主窗口自己建一个按钮。那一行原来右侧挂着一句
    灰字说明，2026-10-11 删掉了 —— 删完就必须补 `addStretch(1)`，否则这个按钮会撑满整行。

    正常路径上这段分支不执行（页面自己有按钮），所以只能**造一个没有那个按钮的页面**
    才量得到它；这也正是它容易被漏掉的原因。
    """
    from PySide6.QtWidgets import QPushButton

    class _PageWithoutRunButton(ui_app.FormulaPage):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.btn_start_pick = None          # 模拟"页面没提供按钮"的中间态

    monkeypatch.setattr(ui_app, "FormulaPage", _PageWithoutRunButton)
    win = ui_app.MainWindow(seeded)
    win.resize(1280, 820)
    win.show()
    qapp.processEvents()
    try:
        page = _tab_page(win, ui_app.TAB_FORMULA)
        win.tabs.setCurrentWidget(page)
        qapp.processEvents()
        button = win.btn_run
        assert isinstance(button, QPushButton)
        assert button is not None
        assert button.isVisible() is True
        assert button.width() <= button.sizeHint().width() + 12, (
            f"兜底的【开始筛选】被拉长了：宽 {button.width()} / 自然 "
            f"{button.sizeHint().width()}"
        )
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.scores.stop()
        win.shutdown()
        win.tray.hide()
        win.close()
        win.deleteLater()
        qapp.processEvents()

