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
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

from PySide6.QtCore import QEvent  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.data.engine import DataEngine  # noqa: E402
from laoa_trader.notify import KINDS  # noqa: E402
from laoa_trader.strategy import rules as rules_mod  # noqa: E402
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
        'enabled_groups = ["ultra", "short", "swing"]\n'
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
    win.scheduler.stop()
    _wait_market(win, qapp)
    worker = getattr(win, "_market_worker", None)
    if worker is not None and worker.isRunning():
        worker.wait(3_000)
    if win.wizard is not None:      # 首次向导（非模态）不要留给下一个用例
        win.wizard.close()
    if win.about_dialog is not None:
        win.about_dialog.close()    # 「关于」窗口同理
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

    为什么要这么写：原来的两个输入行是 `layout.addLayout(pos_row)` 挂在**主窗口底部**的，
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


def _card_of(window, symbol: str):
    for card in window.pool_cards:
        if card.symbol == symbol:
            return card
    raise AssertionError(f"卡片视图里没有 {symbol}")


def test_window_renders_all_panels(window, qapp) -> None:
    # 池子两套视图（卡片 / 表格）都填好了：切换视图只是 setVisible，不重建
    assert window.pool_table.rowCount() == 1
    assert len(window.pool_cards) == 1
    assert window.position_table.rowCount() == 1
    assert window.alert_table.rowCount() == 1
    # 六个页签：**大盘概览放第一个**（启动就停在它上面），股票池紧跟其后
    assert window.tabs.count() == 6
    assert [window.tabs.tabText(i) for i in range(6)] == [
        "大盘概览", "股票池", "持仓", "自选股", "盘中提醒", "设置",
    ]
    assert window.tabs.widget(0) is window.market_page
    assert window.tabs.currentWidget() is window.market_page      # 启动默认页
    # 池子两款视图的可见性要**在池子页是当前页**时才谈得上（先切过去）
    window.tabs.setCurrentWidget(_tab_page(window, "股票池"))
    qapp.processEvents()
    # 默认视图是卡片（"默认卡片"这条由 test_pool_view_toggle_writes_config 专门盯）
    assert window.pool_scroll.isVisible() is True
    assert window.pool_table.isVisible() is False
    window.on_toggle_pool_view()          # 切到表格视图，接着断言表格内容
    assert window.pool_table.isVisible() is True
    # 池子表格：代码/名称/来源策略/来源/备注/热门行业/分数/条件单参数/复制按钮
    assert window.pool_table.columnCount() == 9
    assert window.pool_table.item(0, 0).text() == "600002"
    assert window.pool_table.item(0, 1).text() == "半导体甲"
    assert window.pool_table.item(0, 2).text() == "低价股"
    # 来源列：组名 + 持有期（策略标的；"来源"与"组别"合成一列，不重复）
    assert window.pool_table.item(0, 3).text() == "波段·T+10（T+10）"
    assert window.pool_table.item(0, 4).text() == ""            # 备注（纯策略标的为空）
    assert window.pool_table.item(0, 5).text() == "半导体"
    assert window.pool_table.item(0, 7).text().startswith("触发 ")
    assert window.pool_table.cellWidget(0, 8).text() == "复制条件单"


def test_status_bar_shows_one_line_main_status_and_tags(window) -> None:
    """顶部状态区：主状态**只讲一件事**，细节（行数/补跑/引擎）挪进短标签与详情。"""
    text = window.status_label.fullText()
    # 正常状态那一句：数据就绪 + 股票数 + 更新到哪天（不再塞 8 项、不再用竖线）
    assert text.startswith("✅ 数据就绪 · ")
    assert "更新到 09-11" in text
    assert "｜" not in text
    assert "引擎：" not in text                        # 内部词只许出现在详情里

    # 右侧短标签：每项 2~6 个字 + 一个短值
    assert window.status_tags["今日池子"].text() == "今日池子 1"
    assert window.status_tags["持仓"].text().startswith("持仓 +")
    assert window.status_tags["下次选股"].text().startswith("下次选股 ")
    assert "16:00" in window.status_tags["下次选股"].text()      # 新默认：收盘后主跑
    assert window.status_tags["盘中提醒"].text() == "盘中提醒 未在时段"
    for name, tag in window.status_tags.items():
        assert "｜" not in tag.text(), name

    # 细节进详情：行数 / 补跑时间 / 后台任务状态 / 日志路径 / 自检阈值都在 tooltip 里
    details = window.status_label.toolTip()
    assert "\n" in details                            # 多行
    assert "后台任务：" in details
    assert "本地行情行数：" in details
    assert "补跑 19:15" in details
    assert "日志文件：" in details
    assert "自检阈值：" in details
    assert "最新数据日期：2026-09-11" in details


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

    # ③ 本地数据落后 / 不可用 → 一句"点哪个按钮能解决"（按钮名与按钮上的字一模一样）
    win._clear_status_message()
    ready_result = win.preflight_result
    win.preflight_result = {"status": preflight.NEEDS_INCREMENTAL, "stale_trading_days": 3}
    assert main_line() == "⚠️ 本地数据落后 3 个交易日 · 点【下载数据】补齐"
    win.preflight_result = {"status": preflight.NEEDS_FULL, "reason": "行情表是空的"}
    assert main_line() == "⚠️ 本地数据还没准备好 · 点【下载数据】下载历史数据"
    assert "行情表是空的" in win._status_details()      # 原始原因在详情里（状态栏只给"做什么"）

    # ④ 一切正常 → 就一句"数据就绪"，股票数与日期都是短的
    win.preflight_result = ready_result
    assert main_line().startswith("✅ 数据就绪 · ")


def test_status_main_line_shows_why_daily_was_skipped(window, qapp, monkeypatch) -> None:
    """今天该自动跑却被数据闸门拦下 / 上次出错 → 主状态直接说那句中文原因。"""
    win = window
    st = win.scheduler.status()
    monkeypatch.setattr(win.scheduler, "status", lambda *a, **k: {
        **st, "skipped_reason": "本地数据不可用：已跳过本次自动选股",
        "last_error": None,
    })
    win._clear_status_message()
    win._refresh_status()
    qapp.processEvents()
    text = win.status_label.fullText()
    assert text == "⚠️ 本地数据不可用：已跳过本次自动选股"
    assert "｜" not in text
    # 详情里也留着（用户想核对时查得到）
    assert "今天没自动跑：本地数据不可用：已跳过本次自动选股" in win._status_details()


def test_status_tags_show_short_values_and_hide_without_position(
    window, seeded, qapp, monkeypatch
) -> None:
    """短标签：2~6 个字 + 一个短值；**没有持仓就整项不显示**；盘中提醒三种取值。"""
    from laoa_trader.data import storage
    from laoa_trader.ui import app as ui_app

    win = window
    win._refresh_status()
    qapp.processEvents()
    assert win.status_tags["今日池子"].text() == "今日池子 1"
    assert win.status_tags["今日池子"].isVisible() is True
    # 有持仓 → "持仓 +x.x%"（seeded：成本 3.0、最新收盘 3.174 → +5.8%）
    assert win.status_tags["持仓"].text() == "持仓 +5.8%"
    assert win.status_tags["持仓"].isVisible() is True
    assert win.status_tags["下次选股"].text().startswith("下次选股 ")
    assert "｜" not in "".join(tag.text() for tag in win.status_tags.values())

    # 盘中提醒：未在时段（当前）/ 已暂停（点按钮后）
    assert win.status_tags["盘中提醒"].text() == "盘中提醒 未在时段"
    win.on_toggle_intraday()
    assert win.status_tags["盘中提醒"].text() == "盘中提醒 已暂停"
    win.on_toggle_intraday()
    # 三种取值本身（时段中 只能靠构造调度状态来触发）
    assert win._intraday_text({"in_session": True}) \
        == ui_app.INTRADAY_IN_SESSION == "时段中"
    assert win._intraday_text({"intraday_paused": True}) == ui_app.INTRADAY_PAUSED
    assert win._intraday_text({}) == ui_app.INTRADAY_OUT_SESSION == "未在时段"
    st = win.scheduler.status()
    monkeypatch.setattr(win.scheduler, "status", lambda *a, **k: {**st, "in_session": True})
    win._refresh_status()
    qapp.processEvents()
    assert win.status_tags["盘中提醒"].text() == "盘中提醒 时段中"

    # 没有持仓 → 整项不显示（不是显示成"持仓 —"）
    with storage.connect(seeded.db_path) as conn:
        storage.delete_position(conn, "600001")
    win._refresh_status()
    qapp.processEvents()
    assert win.status_tags["持仓"].text() == ""
    assert win.status_tags["持仓"].isVisible() is False
    assert win.status_tags["今日池子"].isVisible() is True     # 别的标签照常


def test_status_area_never_shows_pipes_or_internal_terms(window, qapp, monkeypatch) -> None:
    """把"啰嗦"这件事钉死：任何状态下都不许再串 `｜`，也不许出现"引擎：在线"这类内部词。"""
    from laoa_trader import state

    win = window
    st = win.scheduler.status()
    cases = {
        "正常": lambda: None,
        "瞬时消息": lambda: win._set_status("✅ 选股建池完成：池子 18 只"),
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
            for tag in win.status_tags.values():
                assert "｜" not in tag.text(), name
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
    assert win.status_label.height() \
        <= win.status_label.fontMetrics().height() * 2


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
                   "主跑时间：16:00（补跑 19:15）", "自检阈值：", "数据目录：", "日志文件："):
        assert expect in text, expect
    assert "laoa-trader.log" in text                      # 日志路径要能照着找到
    assert "10,283,203" not in text                       # 长串数字压成"万"
    assert win.status_details_text.isReadOnly() is True

    win.btn_copy_details.click()
    qapp.processEvents()
    assert "本地行情行数：" in QApplication.clipboard().text()
    assert "已复制状态详情" in win.status_label.fullText()
    dialog.close()


def test_top_buttons_are_short_with_full_tooltips(window, qapp) -> None:
    """顶部按钮文字精简到 2~4 字（1440 逻辑宽也不挤），完整说明进 tooltip。"""
    from laoa_trader.ui import app as ui_app

    win = window
    buttons = [win.btn_download, win.btn_run, win.btn_refresh, win.btn_pause,
               win.btn_check, win.btn_details, win.btn_about]
    assert [b.text() for b in buttons] == [
        "下载数据", "选股建池", "刷新数据", "暂停提醒", "检查盘面", "详情", "关于",
    ]
    for button in buttons:
        assert 2 <= len(button.text()) <= 4, button.text()
        assert len(button.toolTip()) >= 10, button.text()     # 完整说明在 tooltip 里
        assert button.toolTip() != button.text()
        assert win.top_buttons_layout.indexOf(button) >= 0    # 都在同一行里
    # 按钮文字与"指路"文案同一份常量（不然用户按提示找不到按钮）
    assert (win.btn_download.text(), win.btn_details.text()) \
        == (ui_app.BTN_DOWNLOAD_TEXT, ui_app.BTN_DETAILS_TEXT)
    # 【详情】【关于】排在这一行的最右（右对齐）
    qapp.processEvents()
    assert win.btn_details.x() > win.btn_check.x()
    assert win.btn_about.x() > win.btn_details.x()
    assert win.btn_about.geometry().right() <= win.status_area.width() + 1

def test_status_line_is_fully_visible_at_960_logical_width(screen_window, qapp) -> None:
    """≈用户那台机器（逻辑可用 960×900 → 窗口 920×760）：主状态**完整显示**，一行、不被切。

    主状态是"省略不换行"的单行控件：宽度够就原样显示，不够才省略。
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
    # 一行的高度（没有折成两三行把状态区顶高）
    assert win.status_label.height() \
        <= win.status_label.fontMetrics().height() * 2
    assert "\n" not in shown
    # 右侧短标签也都在（没有被挤掉/换行）
    for name in ("今日池子", "下次选股", "盘中提醒"):
        assert win.status_tags[name].isVisible() is True, name
        assert win.status_tags[name].text() != ""
    # 状态区整块在窗口内（没被下沿切掉）
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


def test_intraday_pause_button_toggles(window) -> None:
    assert window.btn_pause.text() == "暂停提醒"
    window.on_toggle_intraday()
    assert window.btn_pause.text() == "恢复提醒"
    assert window.scheduler.status()["intraday_paused"] is True
    # 短标签跟着变成"已暂停"（三种取值之一）
    assert window.status_tags["盘中提醒"].text() == "盘中提醒 已暂停"
    window.on_toggle_intraday()
    assert window.btn_pause.text() == "暂停提醒"
    assert window.scheduler.status()["intraday_paused"] is False
    assert window.status_tags["盘中提醒"].text() == "盘中提醒 未在时段"



def test_copy_plan_puts_text_on_clipboard(window, qapp) -> None:
    from PySide6.QtWidgets import QApplication

    window._copy_plan({"symbol": "600002", "name": "半导体甲", "reason": "测试"}, 12.5)
    qapp.processEvents()
    text = QApplication.clipboard().text()
    assert "【条件单｜买入】600002 半导体甲" in text
    assert "止损" in text and "止盈" in text


def test_add_position_writes_to_db(window, seeded) -> None:
    window.pos_symbol.setText("600002")
    window.pos_qty.setText("500")
    window.pos_cost.setText("11.5")
    window.on_add_position()
    with storage.connect(seeded.db_path) as conn:
        positions = storage.load_positions(conn)
    assert positions["600002"]["quantity"] == 500
    assert positions["600002"]["avg_cost"] == 11.5
    assert window.position_table.rowCount() == 2


def test_add_position_rejects_bad_input(window) -> None:
    window.pos_symbol.setText("abc")
    window.pos_qty.setText("10")
    window.pos_cost.setText("1")
    window.on_add_position()
    assert "6 位股票代码" in window.status_label.fullText()

    window.pos_symbol.setText("600001")
    window.pos_qty.setText("不是数字")
    window.on_add_position()
    assert "必须是数字" in window.status_label.fullText()


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


def test_close_event_minimizes_to_tray(window, qapp) -> None:
    """关闭窗口只最小化到托盘（盯盘工具应常驻后台）。"""
    window.close()
    qapp.processEvents()
    assert window.isHidden() is True


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
    """设置页要反映当前配置：三个组、五个策略、三个频道、各频道参数。"""
    from laoa_trader.strategy import groups as groups_mod

    assert set(window.group_boxes) == set(groups_mod.GROUP_ORDER)
    assert all(box.isChecked() for box in window.group_boxes.values())   # 默认全开
    assert set(window.strategy_boxes) == set(rules_mod.STRATEGIES)
    assert not any(box.isChecked() for box in window.strategy_boxes.values())  # 空=该组全选
    assert set(window.channel_boxes) == set(KINDS)
    assert all(box.isChecked() for box in window.channel_boxes.values())
    assert window.win_sound_box.isChecked() is True
    assert window.win_url_box.isChecked() is True
    assert window.tray_duration.value() == 8000
    assert window.feishu_on_box.isChecked() is True


def test_pool_table_has_source_column(window) -> None:
    """「来源」列合并了"策略组别"与"自选/策略+自选"，避免两列重复信息。"""
    assert window.pool_table.columnCount() == 9
    header = [window.pool_table.horizontalHeaderItem(i).text() for i in range(9)]
    assert header[3] == "来源"
    assert header[4] == "备注"
    assert window.pool_table.item(0, 3).text() == "波段·T+10（T+10）"


# ── 股票池：卡片视图（默认）与卡片/表格切换 ──


def test_pool_cards_render_row_fields(window) -> None:
    """卡片把池子行的字段暴露成属性（将来做"点卡片看详情"也按属性取值）。"""
    from PySide6.QtWidgets import QLabel

    card = window.pool_cards[0]
    assert card.symbol == "600002"
    assert card.name == "半导体甲"
    assert card.label == "低价股"
    assert card.source_label == "波段·T+10（T+10）"
    assert card.note == ""                    # 纯策略标的：没有备注
    assert card.industry == "半导体"
    assert card.score_text == "3.000"         # 分数是格式化后的字符串
    assert card.plan_text.startswith("触发 ")  # 条件单参数文本
    assert card.copy_button.text() == "复制条件单"

    # 文字确实画在卡片上（不是只存在属性里）
    texts = [label.text() for label in card.findChildren(QLabel)]
    assert any("600002" in t and "半导体甲" in t for t in texts)      # 第 1 行：代码 + 名称
    assert card.title_label.font().bold() is True                     # 名称加粗
    assert card.title_label.font().pointSize() >= window.font().pointSize()
    assert "低价股" in texts                                          # 右侧：来源策略
    assert "分数 3.000" in texts                                      # 右侧：分数
    assert "波段·T+10（T+10）" in texts                                # 第 2 行：来源
    assert any("热门行业：半导体" in t for t in texts)                 # 第 2 行：热门行业
    assert any(t.startswith("触发 ") for t in texts)                   # 第 3 行：条件单参数


def test_pool_card_is_compact_and_width_follows_window(pool_window, qapp) -> None:
    """卡片宽度跟着窗口走、高度紧凑（3 行文字 + 按钮，不该每张卡占半屏）。"""
    window = pool_window
    card = window.pool_cards[0]
    viewport_width = window.pool_scroll.viewport().width()
    assert card.width() >= viewport_width - 40     # 宽度铺满滚动区，右侧不留大片空白
    assert 40 <= card.height() <= 140              # 紧凑：3 行文字 + 一个按钮

    window.resize(max(window.width() - 400, 400), window.height())
    qapp.processEvents()
    assert card.width() < viewport_width           # 跟着窗口变窄（setWidgetResizable）
    assert card.width() >= window.pool_scroll.viewport().width() - 40


def test_pool_card_copy_button_copies_plan(window, qapp) -> None:
    """卡片右下角的【复制条件单】真的走 `_copy_plan`（剪贴板里得有真东西）。"""
    from PySide6.QtWidgets import QApplication

    QApplication.clipboard().setText("")      # 先清空：免得读到上一条用例的残留
    card = window.pool_cards[0]
    card.copy_button.click()
    qapp.processEvents()

    text = QApplication.clipboard().text()
    assert "【条件单｜买入】600002 半导体甲" in text
    assert "止损" in text and "止盈" in text
    assert "已复制 600002 的条件单参数" in window.status_label.fullText()


def test_pool_cards_not_rebuilt_when_signature_unchanged(window) -> None:
    """内容没变就**不重建**：每 5 秒重建一次会闪烁、丢焦点、白白吃 CPU。"""
    card_before = window.pool_cards[0]
    table_button_before = window.pool_table.cellWidget(0, 8)

    window._refresh_pool()
    window._refresh_pool()

    assert len(window.pool_cards) == 1
    assert window.pool_cards[0] is card_before          # 同一个控件对象
    assert id(window.pool_cards[0]) == id(card_before)  # 对象 id 也没变
    assert window.pool_table.cellWidget(0, 8) is table_button_before


def test_pool_cards_hide_empty_fields(window, qapp, monkeypatch) -> None:
    """没有分数/行业/备注时**不留空标签**（卡片上不该出现"备注："这种空壳）。"""
    from PySide6.QtWidgets import QLabel

    from laoa_trader import pool as pool_mod

    bare = {
        "symbol": "600002", "name": "半导体甲", "strategy": "LowPriceStrategy",
        "strategies": "LowPriceStrategy", "reason": "低价股·热门行业半导体",
        "label": "低价股", "source_label": "波段·T+10（T+10）",
        "industry": "", "note": "", "score": None,
    }
    monkeypatch.setattr(pool_mod, "pool_table_rows", lambda db_path, day=None: [bare])
    window._pool_signature = ()            # 强制重建
    window._refresh_pool()
    qapp.processEvents()

    card = window.pool_cards[0]
    assert card.score_text == "—"          # 没有分数：格式化结果就是"—"，但不显示
    assert card.industry == "" and card.note == ""
    texts = [label.text() for label in card.findChildren(QLabel)]
    assert all(t.strip() for t in texts), f"卡片上留了空标签：{texts}"
    assert not any(t.startswith("分数") for t in texts)
    assert not any("热门行业" in t for t in texts)
    assert not any("备注" in t for t in texts)
    assert card.plan_text.startswith("触发 ")     # 条件单参数照常算


def test_pool_empty_label_visible_only_when_pool_empty(pool_window, seeded, qapp,
                                                       monkeypatch) -> None:
    """池子非空 → 提示收起；池子空 → 提示出现、卡片列表清空（表格视图下也要正确）。"""
    from laoa_trader import pool as pool_mod

    window = pool_window
    assert window.pool_empty_label.isVisible() is False      # seeded 的池子里有 1 只
    assert window.pool_cards != []

    monkeypatch.setattr(pool_mod, "pool_table_rows", lambda db_path, day=None: [])
    window._refresh_pool()          # 签名从"有"变"无" → 重建
    qapp.processEvents()

    assert window.pool_cards == []
    assert window.pool_table.rowCount() == 0
    assert window.pool_empty_label.isVisible() is True
    assert "今日没有入选标的（收盘后自动选股，或点【选股建池】）" \
        in window.pool_empty_label.text()

    window.on_toggle_pool_view()    # 切到表格视图：提示照样得显示
    qapp.processEvents()
    assert window.pool_table.isVisible() is True
    assert window.pool_empty_label.isVisible() is True

    window.on_toggle_pool_view()    # 切回卡片
    qapp.processEvents()
    assert window.pool_empty_label.isVisible() is True


def test_pool_view_toggle_writes_config(pool_window, seeded, qapp) -> None:
    """默认卡片 → 点一下变表格且写回 pool_view=table → 再点回卡片并写回 cards。"""
    config_file = seeded.data_dir / "config.toml"
    window = pool_window

    assert window.cfg.pool_view == "cards"                  # 默认卡片
    assert window.pool_scroll.isVisible() is True
    assert window.pool_table.isVisible() is False
    assert window.btn_pool_view.text() == "切换为表格"       # 文字 = 点了会发生什么

    window.btn_pool_view.click()
    qapp.processEvents()
    assert window.pool_table.isVisible() is True
    assert window.pool_scroll.isVisible() is False
    assert window.btn_pool_view.text() == "切换为卡片"
    text = config_file.read_text(encoding="utf-8")
    assert 'pool_view = "table"' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text   # 只改这一个键
    assert 'my_own_key = "别动我"' in text
    assert window.cfg.pool_view == "table"

    window.btn_pool_view.click()
    qapp.processEvents()
    assert window.pool_scroll.isVisible() is True
    assert window.pool_table.isVisible() is False
    assert window.btn_pool_view.text() == "切换为表格"
    assert 'pool_view = "cards"' in config_file.read_text(encoding="utf-8")
    assert window.cfg.pool_view == "cards"


def test_pool_view_starts_from_config(seeded, qapp) -> None:
    """启动时按配置决定视图与按钮文字；配置写错（手改 TOML）当卡片，不白屏。"""
    from laoa_trader.ui import app as ui_app

    def open_window():
        win = ui_app.MainWindow(seeded)
        win.show()
        qapp.processEvents()
        # 启动默认停在第一个页签（大盘概览），池子那两款视图要先切过去才可见
        win.tabs.setCurrentWidget(_tab_page(win, "股票池"))
        qapp.processEvents()
        return win

    seeded.pool_view = "table"
    win = open_window()
    try:
        assert win.pool_table.isVisible() is True
        assert win.pool_scroll.isVisible() is False
        assert win.btn_pool_view.text() == "切换为卡片"
    finally:
        win.scheduler.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()

    seeded.pool_view = "手滑写错了"
    win = open_window()
    try:
        assert win.pool_scroll.isVisible() is True       # 非法值 → 卡片
        assert win.pool_table.isVisible() is False
        assert win.btn_pool_view.text() == "切换为表格"
    finally:
        win.scheduler.stop()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()


# ── 输入行在各自页签里（不再挂在窗口底部）──


def test_input_rows_live_in_their_own_tabs(window) -> None:
    """持仓/自选的输入行在各自页签里，而且**不在**中央控件的顶层布局里（原来的全局行没了）。"""
    from PySide6.QtWidgets import QPushButton

    pos_page = _tab_page(window, "持仓")
    watch_page = _tab_page(window, "自选股")

    for widget in (window.pos_symbol, window.pos_qty, window.pos_cost):
        assert pos_page.isAncestorOf(widget) is True
    for widget in (window.watch_symbol, window.watch_note):
        assert watch_page.isAncestorOf(widget) is True

    # 输入行排在表格**上方**（先填再点，视线不用来回跳）
    assert pos_page.layout().itemAt(0).layout().indexOf(window.pos_symbol) >= 0
    assert pos_page.layout().itemAt(1).widget() is window.position_table
    assert watch_page.layout().itemAt(0).layout().indexOf(window.watch_symbol) >= 0
    assert watch_page.layout().itemAt(1).widget() is window.watch_table

    # 全局行确实删掉了：中央控件顶层布局（含子布局）里不再挂着这些输入框
    top_level = _layout_widgets(window.centralWidget().layout())
    for widget in (window.pos_symbol, window.pos_qty, window.pos_cost,
                   window.watch_symbol, window.watch_note):
        assert widget not in top_level

    # 按钮文字没变（用户肌肉记忆、文档与截图都按这几个字找按钮）
    pos_texts = [b.text() for b in pos_page.findChildren(QPushButton)]
    assert "添加持仓" in pos_texts and "删除持仓" in pos_texts
    watch_texts = [b.text() for b in watch_page.findChildren(QPushButton)]
    for text in ("加自选", "删除自选", "启用", "停用"):
        assert text in watch_texts


def test_watch_symbol_enter_adds_to_watchlist(window, seeded, qapp) -> None:
    """自选股输入框回车 = 点【加自选】（加自选是"敲代码回车"的连击动作）。"""
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
    assert window.watch_table.rowCount() == 1
    assert "已加自选：600001 低价样本" in window.status_label.fullText()


def test_pos_symbol_enter_adds_position(window, seeded, qapp) -> None:
    """持仓输入框回车 = 点【添加持仓】。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.pos_symbol.setText("600002")
    window.pos_qty.setText("500")
    window.pos_cost.setText("11.5")
    QTest.keyClick(window.pos_symbol, Qt.Key.Key_Return)
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        positions = storage.load_positions(conn)
    assert positions["600002"]["quantity"] == 500
    assert positions["600002"]["avg_cost"] == 11.5
    assert window.position_table.rowCount() == 2


# ── 「关于」对话框（版本 / 版权 / 数据来源）──


def test_window_title_shows_version_and_about_button(window) -> None:
    import laoa_trader

    assert window.windowTitle() == (
        f"老A选股助手 v{laoa_trader.__version__}（Windows 单机版 · 测试版）"
    )
    assert window.btn_about.text() == "关于"


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
    assert "同花顺（fuyao.aicubes.cn）" in blob
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


def test_save_groups_writes_config_keeps_comments(window, seeded, qapp) -> None:
    """保存策略组：只有 swing 勾选 → 写回 config.toml，注释与未知键不能丢。"""
    window.group_boxes["ultra"].setChecked(False)
    window.group_boxes["short"].setChecked(False)
    window.on_save_groups()
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'enabled_groups = ["swing"]' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert "已写入 config.toml" in window.status_label.fullText()
    # 内存里的配置同步更新
    assert window.cfg.enabled_groups == ["swing"]


def test_save_groups_keeps_strategy_choices(window, seeded, qapp) -> None:
    window.strategy_boxes["LowPriceStrategy"].setChecked(True)
    window.group_boxes["ultra"].setChecked(False)
    window.group_boxes["short"].setChecked(False)
    window.on_save_groups()
    qapp.processEvents()
    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'enabled_strategies = ["LowPriceStrategy"]' in text
    assert window.cfg.enabled_strategies == ["LowPriceStrategy"]


def test_save_groups_requires_at_least_one_group(window, seeded) -> None:
    for box in window.group_boxes.values():
        box.setChecked(False)
    window.on_save_groups()
    assert "至少要勾一个策略组" in window.status_label.fullText()
    # 没有写坏配置文件
    assert 'enabled_groups = ["ultra", "short", "swing"]' in (
        seeded.data_dir / "config.toml").read_text(encoding="utf-8")


def test_save_notify_writes_channels_and_params(window, seeded, qapp) -> None:
    window.channel_boxes["feishu"].setChecked(False)
    window.win_sound_box.setChecked(False)
    window.tray_duration.setValue(3000)
    window.on_save_notify()
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'notify_channels = ["windows", "tray"]' in text
    assert "notify_windows_sound = false" in text
    assert "notify_tray_duration_ms = 3000" in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert window.cfg.notify_channels == ["windows", "tray"]


def test_save_notify_empty_channels_warns_but_saves(window, seeded, qapp) -> None:
    """一个频道都不勾 = 只入库不推送：允许保存，并在状态栏说清楚。"""
    for box in window.channel_boxes.values():
        box.setChecked(False)
    window.on_save_notify()
    qapp.processEvents()
    assert "不推送（只入库）" in window.status_label.fullText()
    assert 'notify_channels = []' in (seeded.data_dir / "config.toml").read_text(
        encoding="utf-8")


def test_save_notify_feishu_without_credentials_hints(window, seeded, qapp) -> None:
    """勾了飞书但没凭证：保存成功 + 明确提示"会自动跳过飞书"，不弹错误框。"""
    window.channel_boxes["feishu"].setChecked(True)
    window.feishu_app_id.setText("")
    window.feishu_secret.setText("")
    window.on_save_notify()
    qapp.processEvents()
    assert "飞书未配置凭证" in window.status_label.fullText()
    assert "winotify" not in window.status_label.fullText()   # 不是那句平台不支持的提示


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

    window.channel_boxes["feishu"].setChecked(False)
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


# ── 自选股面板 ──


def test_watchlist_panel_starts_empty(window) -> None:
    assert window.watch_table.rowCount() == 0
    assert [window.watch_table.horizontalHeaderItem(i).text() for i in range(5)] == [
        "代码", "名称", "备注", "状态", "是否已进池",
    ]


def test_watch_panel_add_autofills_name_and_notes(window, seeded, qapp) -> None:
    """加自选：名称从本地库自动补，备注写进去，表格立刻出现。"""
    window.watch_symbol.setText("600002")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()

    with storage.connect(seeded.db_path) as conn:
        rows = storage.load_watchlist(conn)
    assert rows[0]["symbol"] == "600002"
    assert rows[0]["name"] == "半导体甲"        # 自动补的名字
    assert rows[0]["note"] == "龙头"
    assert window.watch_table.rowCount() == 1
    assert window.watch_table.item(0, 1).text() == "半导体甲"
    assert window.watch_table.item(0, 2).text() == "龙头"
    assert window.watch_table.item(0, 3).text() == "启用"
    assert "已加自选：600002 半导体甲" in window.status_label.fullText()


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
    assert window.watch_table.item(0, 3).text() == "已停用"
    assert "不进池、不监控" in window.status_label.fullText()

    window.on_watch_toggle(True)
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["600001"]

    window.on_watch_remove()
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.load_watchlist(conn) == []
    assert window.watch_table.rowCount() == 0


def test_watch_panel_shows_monitoring_off(window, seeded, qapp) -> None:
    window.watch_symbol.setText("600001")
    window.on_watch_add()
    qapp.processEvents()
    window.cfg.watchlist_in_pool = False
    window._refresh_watchlist()
    qapp.processEvents()
    assert "不监控" in window.watch_table.item(0, 4).text()


def test_pool_table_shows_watchlist_source_and_note(window, seeded, qapp) -> None:
    """自选股进池后：来源能区分「自选」「策略+自选」，备注单独一列（卡片同样如此）。"""
    from laoa_trader import pool as pool_mod

    # 600002 是策略选中的（seed 的池子），把它也加为自选 → 来源「策略+自选」
    window.watch_symbol.setText("600002")
    window.watch_note.setText("龙头")
    window.on_watch_add()
    qapp.processEvents()
    window._refresh_pool()
    qapp.processEvents()

    assert window.pool_table.item(0, 3).text() == "波段·T+10（T+10） + 自选"
    assert window.pool_table.item(0, 4).text() == "龙头"
    # 卡片用的是同一份数据（两套视图不该各说各话）
    assert _card_of(window, "600002").source_label == "波段·T+10（T+10） + 自选"
    assert _card_of(window, "600002").note == "龙头"

    # 纯自选（不在策略候选里）→ 来源「自选」，且能进池
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600100", name="冷门样本", note="消息面")
    pool_mod.build_pool(DataEngine(seeded.db_path), window.cfg, hot_only=False,
                        save=True, day="2026-09-11")
    window._pool_signature = ()          # 强制重建控件
    window._refresh_pool()
    qapp.processEvents()
    rows = {window.pool_table.item(i, 0).text(): i
            for i in range(window.pool_table.rowCount())}
    assert "600100" in rows
    assert window.pool_table.item(rows["600100"], 3).text() == "自选"
    assert window.pool_table.item(rows["600100"], 4).text() == "消息面"
    assert _card_of(window, "600100").source_label == "自选"
    assert _card_of(window, "600100").note == "消息面"


def test_pool_table_signature_includes_note(window, seeded) -> None:
    """备注变了表格与卡片都要重画（否则用户改完备注界面不刷新）。"""
    from laoa_trader import pool as pool_mod

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600002", name="半导体甲", note="备注一")
    pool_mod.build_pool(DataEngine(seeded.db_path), window.cfg, hot_only=False,
                        save=True, day="2026-09-11")
    window._pool_signature = ()
    window._refresh_pool()

    def note_of(code: str) -> str:
        for i in range(window.pool_table.rowCount()):
            if window.pool_table.item(i, 0).text() == code:
                return window.pool_table.item(i, 4).text()
        raise AssertionError(f"池子里没有 {code}")

    assert note_of("600002") == "备注一"
    assert _card_of(window, "600002").note == "备注一"
    card_before = _card_of(window, "600002")

    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600002", note="备注二")
    window._refresh_pool()
    assert note_of("600002") == "备注二"
    assert _card_of(window, "600002").note == "备注二"
    assert _card_of(window, "600002") is not card_before   # 内容变了 → 卡片重建


def test_status_bar_shows_watchlist_count(window, seeded, qapp) -> None:
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
    window._tick()
    qapp.processEvents()
    # 状态栏只给"今日池子几只"；"含自选几只"是细节 → 进详情（用户反馈"状态栏太啰嗦"）
    assert window.status_tags["今日池子"].text() == "今日池子 1"
    assert "含自选 1 只" in window._status_details()


# ── 启动自检与首次向导 ──


@pytest.fixture()
def wizard_window(cfg, qapp, monkeypatch):
    """一个**空库**的主窗口：启动自检必然判 needs_full → 弹首次向导。"""
    from laoa_trader.ui import app as ui_app

    cfg.min_history_years = 9          # 空库无论如何都不可用
    # 向导里点"开始下载"会把 Key 写回配置文件；给一个真实的临时路径，
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
    win.scheduler.stop()
    if win.wizard is not None:
        win.wizard.close()
    win.deleteLater()
    qapp.processEvents()


def test_wizard_appears_when_needs_full(wizard_window) -> None:
    """空库启动 → 首次向导可见，说明原因与判据，并给出下载按钮。"""
    from laoa_trader.data import preflight

    win = wizard_window
    assert win.preflight_result["status"] == preflight.NEEDS_FULL
    assert win.wizard is not None
    assert win.wizard.isVisible() is True
    assert win.wizard_start.text() == "开始下载"
    assert "下载历史数据" in win.wizard.windowTitle()
    assert str(win.cfg.data_dir) in win.wizard_dir.text()
    assert "正在检查本地数据" not in win.status_label.fullText()   # 已给出结论


def test_wizard_start_triggers_download_and_progress(qapp, wizard_window, monkeypatch) -> None:
    """点【开始下载】→ 触发下载回调、进度更新不崩、完成后自动跑一次选股建池。"""
    import dataclasses

    from laoa_trader.data import sync as sync_mod
    from laoa_trader.ui import app as ui_app
    from tests.conftest import seed_ready_db

    seen: dict = {}
    pipeline_calls: list[str] = []

    def fake_download(cfg_, progress_cb=None, should_stop=None, **kwargs):
        seen["should_stop"] = should_stop
        if progress_cb:
            progress_cb("下载全市场日K（10 年）", 1, 2)
            progress_cb("写入行情", 2, 2)
        # "下载成功"在真实世界里意味着**数据已经可用**（自检会变 ready）。
        # 这里把那件事造出来：否则数据闸门会（正确地）拒绝接着跑选股建池 ——
        # 那是另一条用例（test_download_ok_but_still_not_ready_does_not_run_pipeline）。
        seed_ready_db(dataclasses.replace(cfg_, min_history_years=0.0, min_symbols=1))
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=42,
                                   detail="写入 42 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    monkeypatch.setattr(ui_app.MainWindow, "on_run_pipeline",
                        lambda self: pipeline_calls.append("pipeline"))

    win = wizard_window
    # 向导已经因为"严格门槛 + 空库"弹出来了（那是 fixture 的设定）。
    # 现在把窗口里的门槛调成小样本口径：模拟"下载完成后数据真的够用了"，
    # 否则闸门会（正确地）拒绝接着跑 —— 那条路径由
    # test_download_ok_but_still_not_ready_does_not_run_pipeline 覆盖。
    win.cfg.min_history_years, win.cfg.min_symbols = 0.0, 1
    win.wizard_key.setText("dummy-key")
    win.on_wizard_start()
    assert win._worker is not None
    win._worker.wait(30_000)
    qapp.processEvents()

    assert callable(seen["should_stop"])
    assert win.wizard_progress.value() == 2          # 进度条走到底
    assert "写入 42 行" in win.wizard_status.text()
    assert pipeline_calls == ["pipeline"]            # 下完自动跑一次选股建池
    assert win.cfg.hithink_api_key == "dummy-key"    # Key 写进了配置


def test_wizard_requires_api_key(wizard_window, qapp) -> None:
    """没填 Key 就点开始下载：明确提示、不启动后台任务。"""
    win = wizard_window
    win.wizard_key.setText("")
    win.on_wizard_start()
    assert "请先填同花顺 API Key" in win.wizard_status.text()
    assert win._worker is None


def test_wizard_cancel_sets_cooperative_flag(wizard_window, monkeypatch, qapp) -> None:
    """下载中点【取消下载】：置协作式取消标志（已写入的数据保留，下次续传）。"""
    from laoa_trader.data import sync as sync_mod
    from laoa_trader.ui import app as ui_app

    def slow_download(cfg_, progress_cb=None, should_stop=None, **kwargs):
        # 模拟"下载中途被取消"：客户端在该检查点返回真时自己停止
        return sync_mod.SyncResult(
            stage="下载历史数据", ok=False,
            error="已取消" if (should_stop and should_stop()) else "还在跑",
        )

    monkeypatch.setattr(sync_mod, "download_history", slow_download)
    monkeypatch.setattr(ui_app.MainWindow, "on_run_pipeline", lambda self: None)
    win = wizard_window
    win.wizard_key.setText("dummy-key")
    win.on_wizard_start()
    assert win.wizard_skip.text() == "取消下载"
    win.on_wizard_cancel()                       # 下载中取消
    assert win._cancel_download is True
    assert "正在取消" in win.wizard_status.text()
    win._worker.wait(30_000)
    qapp.processEvents()
    assert "已取消" in win.wizard_status.text()
    assert win.wizard_start.isEnabled() is True  # 可以再点一次（续传）


def test_no_wizard_when_data_ready(seeded, qapp) -> None:
    """数据就绪时：不弹向导，状态栏打结论。"""
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
    assert win.wizard is None
    # 主状态那句就是"数据就绪"（不再把 summary_line 的"…行 / 最新 …"塞进状态栏）
    assert win.status_label.fullText().startswith("✅ 数据就绪 · ")
    assert "行" not in win.status_label.fullText()
    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()


def _lagged_cfg(cfg, lag: int, **kwargs):
    """造一个"最新行情日落后今天 lag 个交易日"的就绪库（日期必须相对今天算）。"""
    from datetime import datetime

    from laoa_trader.data import storage
    from tests.conftest import seed_ready_db, workdays_ending

    today = datetime.now().strftime("%Y-%m-%d")
    calendar = workdays_ending(today, 40 + lag)
    data_days = calendar[:-lag] if lag else calendar
    cfg.min_history_years = 0.0
    cfg.min_symbols = 1
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    seed_ready_db(cfg, days=len(data_days), trading_days=data_days)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, calendar)
    return cfg


def test_incremental_autodownload_on_start(cfg, qapp, monkeypatch) -> None:
    """落后几天 + auto_download_on_start=true → 启动时自动后台增量更新。"""
    from laoa_trader.ui import app as ui_app

    seeded = _lagged_cfg(cfg, 2, auto_download_on_start=True)
    calls: list[str] = []
    # 注意：ui/app.py 里 `refresh_data` 是 import 进来的直接引用，
    # 要 patch 它所在的命名空间（patch scheduler 里的同名函数不会生效）
    monkeypatch.setattr("laoa_trader.ui.app.refresh_data",
                        lambda *a, **k: calls.append("refresh") or [])
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    win.run_preflight()
    qapp.processEvents()
    if win._worker is not None:
        win._worker.wait(30_000)
    qapp.processEvents()
    assert calls == ["refresh"]                    # 自动跑了增量
    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()


def test_incremental_no_autodownload_when_config_off(cfg, qapp, monkeypatch) -> None:
    """auto_download_on_start=false → 只提示，不自动下载。"""
    from laoa_trader.ui import app as ui_app

    seeded = _lagged_cfg(cfg, 2, auto_download_on_start=False)
    monkeypatch.setattr("laoa_trader.ui.app.refresh_data",
                        lambda *a, **k: pytest.fail("配置关掉了就不该自动下载"))
    win = ui_app.MainWindow(seeded)
    win.show()
    qapp.processEvents()
    win.run_preflight()
    qapp.processEvents()
    assert win._worker is None                     # 没有后台任务
    assert "落后" in win.status_label.fullText() or "落后" in win._message
    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()


# ── 设置页「自动运行」──


def test_autorun_widgets_reflect_config(window) -> None:
    """设置页要显示当前设置：开关、主跑、补跑，以及"下次自动运行"。"""
    assert window.auto_run_box.isChecked() is True
    assert window.run_at_edit.text() == "16:00"
    assert window.run_at_fallback_edit.text() == "19:15"
    assert "下次自动运行" in window.run_hint.text()
    # 状态栏那一行只留短标签"下次选股 …"；完整说法在详情里
    assert window.status_tags["下次选股"].text().startswith("下次选股 ")
    assert "16:00" in window.status_tags["下次选股"].text()
    assert "下次自动运行：" in window._status_details()


def test_save_run_at_writes_config_and_keeps_comments(window, seeded, qapp) -> None:
    """界面保存运行时间 → 写回 config.toml，**用户注释与未知键仍在**。"""
    window.run_at_edit.setText("16:30")
    window.run_at_fallback_edit.setText("20:00")
    window.on_save_run_at()
    qapp.processEvents()

    text = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    assert 'run_at = "16:30"' in text
    assert 'run_at_fallback = "20:00"' in text
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert window.cfg.run_at == "16:30"                # 内存配置同步
    assert "自动运行已保存" in window.status_label.fullText()


def test_save_run_at_rejects_bad_format(window, seeded, qapp) -> None:
    """非法格式 → 拒绝保存，**config 不变**，给出中文提示。"""
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    window.run_at_edit.setText("25:99")
    window.on_save_run_at()
    qapp.processEvents()
    assert "格式不对" in window.status_label.fullText()
    assert "未写入配置" in window.status_label.fullText()
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before
    assert window.cfg.run_at == "16:00"                # 内存也没被改


def test_save_run_at_rejects_fallback_not_later(window, seeded, qapp) -> None:
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    window.run_at_edit.setText("16:00")
    window.run_at_fallback_edit.setText("15:30")
    window.on_save_run_at()
    qapp.processEvents()
    assert "必须**晚于**主跑时间" in window.status_label.fullText()
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before


def test_save_run_at_warns_before_market_close(window, seeded, qapp) -> None:
    """早于 15:00 收盘 → **保存成功**但明确提示。"""
    window.run_at_edit.setText("09:30")
    window.on_save_run_at()
    qapp.processEvents()
    text = window.status_label.fullText()
    assert "自动运行已保存" in text
    assert "早于 A 股收盘" in text
    assert "16:00 之后" in text
    assert 'run_at = "09:30"' in (seeded.data_dir / "config.toml").read_text(encoding="utf-8")


def test_toggle_auto_run_off_from_ui(window, seeded, qapp) -> None:
    window.auto_run_box.setChecked(False)
    window.on_save_run_at()
    qapp.processEvents()
    assert window.cfg.auto_run is False
    assert "auto_run = false" in (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    # 状态区说清楚：保存消息 + 短标签直接写"自动运行 已关"（详情里保留完整说法）
    assert "自动运行已保存：关" in window.status_label.fullText()
    assert window.status_tags["下次选股"].text() == "自动运行 已关"
    assert "下次自动运行：已关闭" in window._status_details()   # 详情里保留完整说法


def test_saved_time_takes_effect_without_restart(window, seeded, qapp, monkeypatch) -> None:
    """界面改完时间 → 调度器下一轮就按新时间判断（不重启）。"""
    from datetime import datetime

    from laoa_trader import scheduler as sched_mod

    monkeypatch.setattr(sched_mod.intraday, "is_trading_day", lambda *a, **k: True)
    runs: list[int] = []
    monkeypatch.setattr(window.scheduler, "run_daily_now", lambda **k: runs.append(1))

    moment = datetime(2026, 9, 11, 16, 30)
    window.run_at_edit.setText("17:00")
    window.on_save_run_at()
    qapp.processEvents()
    window.scheduler._maybe_daily(moment)
    assert runs == []                                  # 16:30 < 17:00：不跑

    window.run_at_edit.setText("16:00")                # 用户又改早了
    window.on_save_run_at()
    qapp.processEvents()
    window.scheduler._maybe_daily(moment)
    assert runs == [1]                                 # 下一轮就按新时间跑

# ── 下载进度可见（用户反馈："180MB 没有任何进度，像卡死"）──


def test_progress_shows_mb_and_percent_in_status_bar(window) -> None:
    """下载 dump 时：主进度条要走、状态栏要显示"MB + 百分比"。"""
    window._on_progress("下载 daily-k", 81_000_000, 180_700_000)

    assert window.progress.maximum() == 180_700_000
    assert window.progress.value() == 81_000_000        # 不是 -1（不是"不确定进度"）
    assert "MB" in window.progress.format()
    status = window.status_label.fullText()
    assert "下载 daily-k" in status
    assert "45%" in status or "44%" in status           # 81/180.7 ≈ 44.8%
    assert "81" in status and "181" in status or "180" in status


def test_progress_reaches_full_and_reports_downloading_flag(window, monkeypatch) -> None:
    """进度走到底 + 下载期间状态栏挂"正在下载"（下载中这面旗由 sync 层负责）。"""
    from laoa_trader import state

    total = 180_700_000
    for pct in (10, 55, 100):
        window._on_progress("下载 daily-k", total * pct // 100, total)
    assert window.progress.value() == total
    assert window.progress.maximum() == total
    assert "100" in window.progress.format() or window.progress.value() == total

    state.begin_download()
    try:
        window._tick()                                   # 定时刷新也要体现"在下载"
        assert "正在下载" in window.status_label.fullText()
    finally:
        state.end_download()
    window._tick()
    assert "正在下载" not in window.status_label.fullText()   # 结束后切回正常状态


def test_wizard_progress_bar_moves_during_download(wizard_window, qapp) -> None:
    """首次向导里那条进度条也要动，并显示 MB（用户盯的就是那个窗口）。

    用**真实的向导窗口**（不是手搓的控件替身）：下载 180 MB 时它必须
    走到 90/180 的位置、状态文字里有 MB 与百分比。
    """
    win = wizard_window
    assert win.wizard is not None and win.wizard.isVisible()

    win._on_progress("下载 daily-k", 90_000_000, 180_700_000)

    assert win.wizard_progress.maximum() == 180_700_000
    assert win.wizard_progress.value() == 90_000_000       # 不是 -1、也不是停在 0
    assert "MB" in win.wizard_progress.format()
    assert "下载 daily-k" in win.wizard_progress.format()
    text = win.wizard_status.text()
    assert "MB" in text and "%" in text                    # "…50%（90/181 MB）"
    assert "下载 daily-k" in text


def test_worker_note_updates_status(window) -> None:
    """下载状态（"正在重签 URL 继续下载（第 2 次）"）要显示出来，不被进度覆盖。"""
    window._on_worker_note("下载历史数据", "正在重签 URL 继续下载（第 2 次，从 87.0 MB 处接着下）")
    assert "正在重签 URL 继续下载（第 2 次" in window.status_label.fullText()


# ── 状态栏要能说清"今天为什么没自动跑"（用户不必翻日志）──


def test_status_bar_shows_why_daily_was_skipped(cfg, qapp, monkeypatch) -> None:
    """定时任务被数据闸门跳过 → 状态栏直接显示中文原因（`skipped_reason`）。"""
    from laoa_trader import scheduler as sched_mod
    from laoa_trader.ui import app as ui_app

    cfg.min_history_years, cfg.min_symbols = 4.5, 4000      # 空库 → needs_full
    monkeypatch.setattr(sched_mod.intraday, "is_trading_day", lambda *a, **k: True)
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()

    ran: list[str] = []
    monkeypatch.setattr(win.scheduler, "run_daily_now", lambda **k: ran.append("ran"))
    win.scheduler._maybe_daily(datetime.now().replace(hour=16, minute=30))
    # 主状态一次只讲一件事（见 test_status_bar_shows_one_line_main_status_and_tags）：
    # 先让出启动自检留下的那条更早的消息，看的才是"被闸门跳过"这件事本身
    win._message = ""
    win._tick()
    qapp.processEvents()

    assert ran == []                                        # 没跑
    st = win.scheduler.status()
    assert st["preflight_status"] == "needs_full"
    assert st["daily_skipped_today"] is True
    text = win.status_label.fullText()
    assert st["skipped_reason"] in text                     # 状态栏确实把它显示出来了
    assert "跳过本次自动选股" in text
    assert "｜" not in text

    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()


# ── 手动【立即选股并建池】的数据闸门 ──


def test_run_pipeline_refuses_when_data_not_ready(cfg, qapp, monkeypatch) -> None:
    """空库点【立即选股并建池】：**明确拒绝 + 指路**，绝不在不完整的数据上跑策略。"""
    from laoa_trader import scheduler as sched_mod
    from laoa_trader.ui import app as ui_app

    cfg.min_history_years, cfg.min_symbols = 4.5, 4000
    ran: list[str] = []
    monkeypatch.setattr(sched_mod, "run_daily", lambda *a, **k: ran.append("run_daily"))
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()

    win.on_run_pipeline()
    qapp.processEvents()

    assert ran == []                                   # 策略一次都没跑
    assert win._worker is None                          # 也没起后台任务
    text = win.status_label.fullText()
    assert "本地无可用历史数据" in text
    assert "下载" in text                               # 指路到下载按钮

    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()


def test_run_pipeline_refuses_while_downloading(window, qapp, monkeypatch) -> None:
    """正在下载时点【立即选股并建池】：同样拒绝（避免在不完整数据上跑）。"""
    from laoa_trader import state

    ran: list[str] = []
    monkeypatch.setattr(window.scheduler, "run_daily_now", lambda **k: ran.append(1))

    state.begin_download()
    try:
        window.on_run_pipeline()
        qapp.processEvents()
    finally:
        state.end_download()

    assert ran == []
    assert window._worker is None
    assert "正在下载历史数据" in window.status_label.fullText()


def test_wizard_download_done_runs_pipeline(cfg, qapp, monkeypatch) -> None:
    """首次下载完成 → 自检变 ready → **自动跑一次建池**（现有行为保留）。

    这条是端到端的"分发后第一次用"：空库 → needs_full → 向导下载 → 数据到位
    → 自动选股建池。装载的门槛按小样本放低（与其它小样本 fixture 同一约定），
    但"空库 = needs_full"这一条是真实的，所以起点确实会被闸门挡住 ——
    正好验证"下完能自己走过去"。
    """
    import dataclasses

    from laoa_trader import scheduler as sched_mod
    from laoa_trader.data import sync as sync_mod
    from laoa_trader.ui import app as ui_app
    from tests.conftest import seed_ready_db

    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(f'data_dir = "{p(cfg.data_dir)}"', encoding="utf-8")
    cfg.source_path = config_file

    def fake_download(cfg_, progress_cb=None, note_cb=None, should_stop=None, **kwargs):
        """模拟"下载完成"：把库写成 ready，并回报进度与状态（不联网）。"""
        if note_cb:
            note_cb("下载完成（180.7 MB，共 1 次尝试）")
        if progress_cb:
            progress_cb("下载 daily-k", 180_700_000, 180_700_000)
        seed_ready_db(dataclasses.replace(cfg_, min_history_years=0.0, min_symbols=1))
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=10, detail="写入 10 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    monkeypatch.setattr(sched_mod.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"kind": "tray", "ok": True}})

    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()
    win.run_preflight()
    qapp.processEvents()
    assert win.preflight_result["status"] == "needs_full"     # 空库
    assert win.wizard is not None                             # 弹了首次向导

    win.wizard_key.setText("dummy-key")
    win.on_wizard_start()
    assert win._worker is not None
    win._worker.wait(60_000)
    qapp.processEvents()
    # 下载完成 → 自动接着跑一次选股建池（此时闸门必须已经放行）
    for _ in range(3):
        if win._worker is not None and win._worker.isRunning():
            win._worker.wait(60_000)
        qapp.processEvents()

    with storage.connect(cfg.db_path) as conn:
        pool_rows = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
    assert pool_rows > 0, "下载完成后应当自动选出池子"
    status = win.status_label.fullText()
    assert "本地无可用历史数据" not in status
    assert win.preflight_result is not None                    # 缓存的是"下载后"的新结论
    assert win.preflight_result["status"] == "ready"           # 数据确实到位了

    win.scheduler.stop()
    win.deleteLater()
    qapp.processEvents()

def test_download_ok_but_still_not_ready_does_not_run_pipeline(qapp, wizard_window,
                                                               monkeypatch) -> None:
    """下载"成功"但数据仍不达标（例如行业覆盖不够）→ **不跑策略**，状态栏说清原因。

    闸门看的是"数据到底能不能用"，不是"上一动作成功没有"——
    否则半成品库照样会跑出错的池子。
    """
    from laoa_trader.data import sync as sync_mod
    from laoa_trader.ui import app as ui_app

    pipeline_calls: list[str] = []

    def fake_download(cfg_, progress_cb=None, note_cb=None, should_stop=None, **kwargs):
        # 故意**不**把库写成 ready：模拟"下到了文件但库仍然不可用"
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=0,
                                   detail="导入 0 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    monkeypatch.setattr(ui_app.MainWindow, "on_run_pipeline",
                        lambda self: pipeline_calls.append("pipeline"))

    win = wizard_window
    win.wizard_key.setText("dummy-key")
    win.on_wizard_start()
    assert win._worker is not None
    win._worker.wait(30_000)
    qapp.processEvents()

    assert pipeline_calls == []                      # 数据不可用 → 绝不跑策略
    assert "数据仍不可用" in win.status_label.fullText()
    assert win.preflight_result is not None
    assert win.preflight_result["status"] == "needs_full"


# ── 「大盘概览」页（独立 tab）──


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
    window.tabs.setCurrentWidget(_tab_page(window, "股票池"))
    qapp.processEvents()
    yield window


def test_market_page_is_a_tab_of_its_own(window) -> None:
    """概览是**独立一页、而且是第一个页签**，页面是"分区 + 网格"，不再是一团长文本。"""
    from PySide6.QtWidgets import QScrollArea

    pool_page = _tab_page(window, "股票池")
    assert window.tabs.widget(0) is window.market_page
    assert window.tabs.widget(1) is pool_page
    assert window.tabs.currentWidget() is window.market_page
    assert window.market_page is not pool_page
    assert pool_page.isAncestorOf(window.market_page) is False

    page = window.market_page
    # KPI 区：七个指标各一个**独立控件**（不是一个长字符串里的一段）
    assert set(window.market_kpis) == {
        "涨停", "跌停", "炸板", "成交额", "上涨", "下跌", "平盘",
    }
    assert len(set(window.market_kpis.values())) == 7        # 七个互不相同的数值标签
    for name, label in window.market_kpis.items():
        card = window.market_kpi_cards[name]
        assert card.value_label is label
        assert card.title_label.text() == name               # 卡片里带小号灰标签
        assert page.isAncestorOf(card) is True

    # 三组指数：各一个"整组容器"（`宽基/情绪/板块` → 用于整组隐藏）+ 组内网格
    assert set(window.market_group_boxes) == {"宽基", "情绪", "板块"}
    for label, box in window.market_group_boxes.items():
        assert box.title_label.text() == label
        assert page.isAncestorOf(box) is True
        assert page.isAncestorOf(box.title_label) is True

    # 页脚 / 提示 / 【立即刷新】都在这一页里
    assert page.isAncestorOf(window.market_footer) is True
    assert page.isAncestorOf(window.market_as_of_label) is True
    assert page.isAncestorOf(window.market_hint) is True
    assert page.isAncestorOf(window.btn_market_refresh) is True
    assert window.btn_market_refresh.text() == "立即刷新"

    # 页面自上而下：标题行 / 可滚动的内容区 / 出错提示 / **最后一行**页脚
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


def test_settings_tab_is_scrollable_so_the_window_can_shrink(window, qapp) -> None:
    """设置页控件最多，**整页套了滚动区**，否则它会把"窗口最小高度"顶到屏幕外面去。

    为什么这条值得测：设置页十几个勾选框的最小高度加起来 750+，而页签的最小高度取
    所有页的最大值 —— 不套滚动区时窗口最小高度是 887（1366×768 的笔记本上根本放不下，
    表现就是"一打开最下边看不见"）。这条盯的是那个根因别再加回来。
    """
    from PySide6.QtWidgets import QScrollArea

    page = _tab_page(window, "设置")
    scrolls = page.findChildren(QScrollArea)
    assert scrolls, "设置页需要一层滚动区（小屏才缩得下来）"
    assert scrolls[0].widgetResizable() is True
    assert scrolls[0].isAncestorOf(window.save_notify_button) is True
    # 控件还在、顺序没动（只是外面多了一层容器）
    assert page.isAncestorOf(window.group_boxes["ultra"]) is True
    assert page.isAncestorOf(window.channel_boxes["tray"]) is True
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
        assert win.market_kpis["涨停"].text() == "55"
        assert win.market_kpis["成交额"].text() == "沪 7792亿 · 深 8499亿 · 北 140亿"
        assert win.market_group_boxes["宽基"].entries[0].name_label.text() == "上证"
        assert "更新于 —" not in win.market_as_of_label.fullText()   # 页脚已是真实取数时间
    finally:
        win.scheduler.stop()
        if win.wizard is not None:
            win.wizard.close()
        win.tray.hide()
        win.deleteLater()
        qapp.processEvents()
        market.clear_cache()


#: 概览页三组条目应当长成的样子（顺序 = 配置顺序，数值照抄 `SAMPLE_ROWS` 的当日在值）。
#: 与 `tests/test_market.py` 的 `SAMPLE_LINES` 是同一批数字：界面与命令行口径必须一致。
MARKET_EXPECTED_ENTRIES: dict[str, list[tuple[str, str, str, str]]] = {
    "宽基": [
        ("000001.SH", "上证", "3885.33", "-0.07%"),
        ("399001.SZ", "深成", "13384.57", "-0.64%"),
        ("399006.SZ", "创业板", "3285.58", "-1.10%"),
        ("000688.SH", "科创50", "1528.27", "-1.62%"),
        ("000300.SH", "沪深300", "4480.08", "-0.67%"),
    ],
    "情绪": [
        ("883404.TI", "同花顺情绪", "885.53", "-0.18%"),
        ("883958.TI", "昨日连板", "6928.17", "+3.53%"),
        ("883994.TI", "昨日打首板", "1551.88", "+1.69%"),
        ("883418.TI", "微盘股", "2131.00", "+0.95%"),
    ],
    "板块": [
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


def test_market_page_renders_kpis_entries_colors_and_footer(market_window, qapp) -> None:
    """KPI 数值 + 三组每一项的名称/点位/涨跌幅 + 逐值颜色 + 页脚来源，一次全钉住。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        assert market_window.market_overview is not None
        # KPI：与 `market.kpi_values()` 同源（取数口径与命令行共用一份）
        values = market.kpi_values(market_window.market_overview)
        assert {name: label.text() for name, label in market_window.market_kpis.items()} \
            == values
        assert (values["涨停"], values["跌停"], values["炸板"]) == ("55", "16", "30")
        assert values["成交额"] == "沪 7792亿 · 深 8499亿 · 北 140亿"
        assert (values["上涨"], values["下跌"], values["平盘"]) == ("1", "1", "1")

        # 三组条目：一个指数一个控件，三项文本逐项对齐
        for label, expected in MARKET_EXPECTED_ENTRIES.items():
            box = market_window.market_group_boxes[label]
            assert box.isVisible() is True
            assert box.placeholder_label.isVisible() is False   # 有数据就不要 `—` 占位
            assert [_entry_fields(entry) for entry in box.entries] == expected
        assert [entry.thscode for entry in market_window.market_entries] == [
            code for rows in MARKET_EXPECTED_ENTRIES.values() for code, *_ in rows
        ]

        # 逐值上色：宽基全跌（绿）、情绪有涨有跌（红绿各自）、板块银行红证券绿
        up, down = f"color:{market.COLOR_UP}", f"color:{market.COLOR_DOWN}"
        assert [_entry_colors(e) for e in market_window.market_group_boxes["宽基"].entries] \
            == [(down, down)] * 5
        assert [_entry_colors(e) for e in market_window.market_group_boxes["情绪"].entries] \
            == [(down, down)] + [(up, up)] * 3
        assert [_entry_colors(e) for e in market_window.market_group_boxes["板块"].entries] \
            == [(up, up), (down, down)]
        assert market_window.market_hint.isVisible() is False       # 一切正常不留提示

        # 页脚：数据来源 + 取数时间 + 刷新节奏（breadth 开着要注明它慢一档）
        footer = market_window.market_as_of_label.fullText()
        assert footer.startswith("数据来源：同花顺金融数据服务 · 更新于 ")
        assert "每分钟自动刷新" in footer
        assert "涨跌家数与北交所成交额每 5 分钟更新" in footer
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
        wide = market_window.market_group_boxes["宽基"].entries
        assert [e.value_label.text() for e in wide] == ["3900.00", "13400.00"]
        assert [e.pct_label.text() for e in wide] == ["+0.86%", "+0.12%"]
        assert [_entry_colors(e) for e in wide] == [(up, up)] * 2
    finally:
        market.clear_cache()


def test_market_colors_each_value_on_its_own_move(market_window, qapp) -> None:
    """**重点**：同一组里一涨一跌一平 → 每一项按自己的涨跌上色（不再是"整组同色"）。

    这正是"一行一个 QLabel"做不到的事：情绪/板块经常同时有涨有跌，
    按组取色只能"要么全染红、要么全不染"，两种都在骗人。
    """
    from laoa_trader import market

    market.clear_cache()
    try:
        rows = [
            {"thscode": "883404.TI", "last_price": 900.0, "price_change_ratio_pct": 1.20},
            {"thscode": "883958.TI", "last_price": 6800.0, "price_change_ratio_pct": -0.80},
            {"thscode": "883994.TI", "last_price": 1551.0, "price_change_ratio_pct": 0.0},
        ]
        market_window.refresh_market_overview(force=True, client=_market_fake(rows=rows))
        qapp.processEvents()

        by_code = {e.thscode: e for e in market_window.market_group_boxes["情绪"].entries}
        up = by_code["883404.TI"]
        down = by_code["883958.TI"]
        flat = by_code["883994.TI"]

        assert (up.value_label.text(), up.pct_label.text()) == ("900.00", "+1.20%")
        assert _entry_colors(up) == (f"color:{market.COLOR_UP}",) * 2
        assert _entry_colors(down) == (f"color:{market.COLOR_DOWN}",) * 2
        # 平盘：两个 label 都没有 color 样式（用界面默认色，不硬塞一个颜色）
        assert _entry_colors(flat) == ("", "")
        assert flat.pct_label.text() == "+0.00%"
        # 同一组里既有红又有绿 —— 逐值上色才做得到
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


def test_market_page_hides_whole_group_when_config_is_empty(market_window, qapp) -> None:
    """某组配置为空 → 那一组**连标题一起隐藏**（不留一个空的"板块："）。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        market_window.cfg.market_sector_indices = []
        market_window.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()

        sector = market_window.market_group_boxes["板块"]
        assert sector.isVisible() is False
        assert sector.title_label.isVisible() is False      # 标题也跟着收掉，不留空标题
        assert sector.entries == []
        assert market_window.market_group_boxes["宽基"].isVisible() is True   # 其它组照常
        assert market_window.market_group_boxes["情绪"].isVisible() is True
        assert "银行" not in "".join(e.name_label.text() for e in market_window.market_entries)
        assert all(e.thscode != "881155.TI" for e in market_window.market_entries)
    finally:
        market.clear_cache()


def test_market_page_drops_only_the_bad_code(market_window, qapp) -> None:
    """**重点**：配置里混一个取不到的代码（这里用 932000.TI）→ 页面上**只少那一项**。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        market_window.cfg.market_sentiment_indices = [
            "883404.TI", "883958.TI", "883994.TI", "932000.TI", "883418.TI",
        ]
        market_window.refresh_market_overview(
            force=True, client=_market_fake(index_fail=("932000.TI",))
        )
        qapp.processEvents()

        sentiment = market_window.market_group_boxes["情绪"]
        assert [e.thscode for e in sentiment.entries] == [
            "883404.TI", "883958.TI", "883994.TI", "883418.TI",
        ]
        assert "932000" not in "".join(
            e.name_label.text() + e.value_label.text() for e in market_window.market_entries
        )
        assert "昨日连板" in [e.name_label.text() for e in sentiment.entries]
        # 其它组一字不动
        assert [_entry_fields(e) for e in market_window.market_group_boxes["宽基"].entries] \
            == MARKET_EXPECTED_ENTRIES["宽基"]
        assert market_window.market_overview["failed"] == ["932000.TI"]
        assert "932000.TI" in market_window.market_hint.text()
    finally:
        market.clear_cache()


def test_market_page_shows_dash_and_reason_without_data(market_window, qapp) -> None:
    """拿不到数据：该显示 `—` 的地方显示 `—`、原因写在页面上，界面照常可用。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        market_window.cfg.hithink_api_key = ""              # 没配 Key：真实路径直接降级
        market_window.refresh_market_overview(force=True)
        qapp.processEvents()

        assert market_window.market_overview is not None    # 拿不到 ≠ 抛异常，而是"有结构没数据"
        assert market.has_data(market_window.market_overview) is False
        assert {name: label.text() for name, label in market_window.market_kpis.items()} == {
            "涨停": market.DASH, "跌停": market.DASH, "炸板": market.DASH,
            "成交额": f"沪 {market.DASH} · 深 {market.DASH} · 北 {market.DASH}",
            "上涨": market.DASH, "下跌": market.DASH, "平盘": market.DASH,
        }
        # 配了的组还在（用户才知道自己配的东西在哪一行），只是没数据 → 一个 `—` 占位
        for label, box in market_window.market_group_boxes.items():
            assert box.isVisible() is True, label
            assert box.title_label.isVisible() is True
            assert box.entries == []
            assert box.placeholder_label.text() == market.DASH
            assert box.placeholder_label.isVisible() is True
        assert market_window.market_entries == []
        assert market_window.market_hint.isVisible() is True
        assert "同花顺 Key" in market_window.market_hint.text()
        assert "同花顺 Key" in market_window.market_page.toolTip()
        # 页脚那行小字照样有（时间是 —）
        assert "更新于 —" in market_window.market_as_of_label.fullText()

        market_window._tick()                                # 界面其它部分照常刷新
        qapp.processEvents()
        assert market_window.pool_table.rowCount() == 1
        assert market_window.status_tags["今日池子"].text() == "今日池子 1"
    finally:
        market.clear_cache()


def test_market_breadth_off_sends_no_page_request(market_window, qapp) -> None:
    """`market_breadth = false`：北交所与涨跌家数显示 `—`，且**一个分页请求都不发**。"""
    from laoa_trader import market

    market.clear_cache()
    try:
        market_window.cfg.market_breadth = False
        client = _market_fake()
        market_window.refresh_market_overview(force=True, client=client)
        qapp.processEvents()

        assert client.count("request") == 0
        assert market_window.market_kpis["成交额"].text().endswith("北 —")
        for name in ("上涨", "下跌", "平盘"):
            assert market_window.market_kpis[name].text() == market.DASH
        # 关掉时页脚就不该再提"每 5 分钟更新"
        assert "每 5 分钟" not in market_window.market_as_of_label.fullText()
    finally:
        market.clear_cache()


def test_market_page_font_hierarchy_and_alignment(market_window, qapp) -> None:
    """字号只有三级（页面标题 > 分区标题 = 正文），数值比标签大一号加粗，数字右对齐。

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

    title_font = win.market_title.font()
    group_font = win.market_group_boxes["宽基"].title_label.font()
    card = win.market_kpi_cards["涨停"]
    assert title_font.bold() is True
    assert title_font.pointSize() > group_font.pointSize()          # 页面标题最大
    assert card.value_label.font().bold() is True                   # 数值加粗
    # "大一号"：数值比它自己的标签大一号（标签保持正文号，只是变灰）
    assert card.value_label.font().pointSize() == card.title_label.font().pointSize() + 1
    assert card.value_label.alignment() & Qt.AlignmentFlag.AlignRight
    # 条目里点位与涨跌幅都右对齐；两列的列宽对所有条目都一样 → 数字落在同一条竖线上
    entries = win.market_group_boxes["宽基"].entries
    assert entries, "需要有数据才谈得上对齐"
    for entry in entries:
        assert entry.value_label.alignment() & Qt.AlignmentFlag.AlignRight
        assert entry.pct_label.alignment() & Qt.AlignmentFlag.AlignRight
    tracks = {
        (e.value_label.minimumWidth(), e.pct_label.minimumWidth()) for e in entries
    }
    assert len(tracks) == 1
    assert tracks.pop()[0] > 0


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

    market.clear_cache()
    try:
        market_window.cfg.market_overview = False
        client = _market_fake()
        market_window.refresh_market_overview(force=True, client=client)
        qapp.processEvents()
        assert client.calls == []
        # 配了的组照样显示（只是没数据，占位是 `—`）
        assert market_window.market_group_boxes["宽基"].isVisible() is True
        assert market_window.market_group_boxes["宽基"].placeholder_label.isVisible() is True
        assert market_window.market_kpis["涨停"].text() == market.DASH
        assert "market_overview" in market_window.market_hint.text()
    finally:
        market.clear_cache()


# ── 窗口尺寸：按屏幕可用区域算，最底部那一行不许被切掉 ──


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
        win.scheduler.stop()
        _wait_market(win, qapp)
        if win.wizard is not None:
            win.wizard.close()
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

        # 股票池页：9 列表格不许把窗口顶宽 —— 它自己在内部横向滚动
        win.tabs.setCurrentWidget(_tab_page(win, "股票池"))
        qapp.processEvents()
        assert win.pool_table.minimumSizeHint().width() <= win.pool_table.width()
        # 表格的横向滚动条是"需要时出现"（9 列放不下就自己滚，而不是把窗口顶宽）
        assert win.pool_table.horizontalScrollBarPolicy() \
            != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert win.minimumSizeHint().width() <= 760
        # 卡片视图是滚动区：宽度跟着视口走，不会横向溢出
        assert isinstance(win.pool_scroll, QAbstractScrollArea)
        assert win.pool_scroll.viewport().width() <= win.width()
    finally:
        market.clear_cache()


def test_kpi_grid_is_two_rows_of_three_plus_a_spanning_amount_card(
    market_window, qapp
) -> None:
    """KPI 排布：2 行 × 3 个 + 成交额**单独占一行跨满所有列**（不许挤成一行六个）。

    900 逻辑宽上下是最常见的实际宽度，六个块挤一行会又扁又难看；
    3+3 两组对齐、成交额横着占满一行，才是"整齐"的那一种。
    成交额原来是"第 4 列跨两行"，在 Windows 的字体下那一列放不下三个数字（实测被截断），
    改成跨满整行后与字体宽度无关。
    """
    from laoa_trader import market

    market.clear_cache()
    win = market_window
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        grid = win.market_kpi_grid
        expected = {
            "涨停": (0, 0, 1, 1), "跌停": (0, 1, 1, 1), "炸板": (0, 2, 1, 1),
            "上涨": (1, 0, 1, 1), "下跌": (1, 1, 1, 1), "平盘": (1, 2, 1, 1),
            "成交额": (2, 0, 1, 3),        # 第 3 行、跨满 3 列
        }
        for name, card in win.market_kpi_cards.items():
            index = grid.indexOf(card)
            assert index >= 0, name
            assert grid.getItemPosition(index) == expected[name], name
        # 同一个网格位置不会有两个卡片（"挤成一行六个"在这种情况下会被抓出来）
        positions = [
            grid.getItemPosition(grid.indexOf(card))[:2]
            for card in win.market_kpi_cards.values()
        ]
        assert len(set(positions)) == len(positions) == 7
    finally:
        market.clear_cache()


def test_overview_uses_three_columns_on_a_150pct_scaled_screen(
    screen_window, qapp
) -> None:
    """≈用户那台机器（逻辑可用 960×900 → 窗口 920×760）：概览页正好 **3 列**，整齐不挤不截断。

    3 列是那一档的主力形态（920 ÷ 260 ≈ 3）：三五只宽基排成 3+2、四项情绪排成 3+1，
    每一项的"名称/点位/涨跌幅"三列都要完整显示（不许被列宽截掉半个数字）。
    """
    from laoa_trader import market

    market.clear_cache()
    win = screen_window(960, 900)
    try:
        assert (win.width(), win.height()) == (920, 760)
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        assert win._market_columns == 3
        assert _grid_rows(win.market_group_boxes["宽基"]) == [
            [c for c, *_ in MARKET_EXPECTED_ENTRIES["宽基"]][:3],
            [c for c, *_ in MARKET_EXPECTED_ENTRIES["宽基"]][3:],
        ]
        assert [len(row) for row in _grid_rows(win.market_group_boxes["情绪"])] == [3, 1]
        assert [len(row) for row in _grid_rows(win.market_group_boxes["板块"])] == [2]

        # 每一项三个标签都完整显示：宽度不少于自己的"需要宽度"（截断/挤压会小于它）
        for entry in win.market_entries:
            assert entry.name_label.text() not in ("", market.DASH)
            assert entry.name_label.width() >= entry.name_label.sizeHint().width(), \
                entry.thscode
            for label in (entry.value_label, entry.pct_label):
                assert label.width() >= label.sizeHint().width(), entry.thscode
        # KPI 数值也不许被卡片的宽度截掉
        for name, card in win.market_kpi_cards.items():
            assert card.value_label.width() >= card.value_label.sizeHint().width(), name
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
    """窗口变窄 → 每组每行的条目数跟着降（4 → 3 → 5 列），且始终不出现横向溢出。"""
    from laoa_trader import market

    market.clear_cache()
    win = screen_window(1600, 1000)      # 窗口 = min(1120, 1560) = 1120 宽 → 4 列
    try:
        win.refresh_market_overview(force=True, client=_market_fake())
        qapp.processEvents()
        wide = win.market_group_boxes["宽基"]
        codes = [code for code, *_ in MARKET_EXPECTED_ENTRIES["宽基"]]
        assert win._market_columns == 4
        assert _grid_rows(wide) == [codes[:4], codes[4:]]

        # 缩到 800 宽（现在最小宽度是 760，缩得下去）：列数掉到 2，条目往下排
        win.resize(800, win.height())
        qapp.processEvents()
        assert win._market_columns == 2
        assert _grid_rows(wide) == [codes[:2], codes[2:4], codes[4:]]
        assert [c for row in _grid_rows(wide) for c in row] == codes
        assert win.market_scroll.horizontalScrollBar().isVisible() is False
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()

        # 再放大：列数涨到 5（一行装下五只宽基）
        win.resize(1500, win.height())
        qapp.processEvents()
        assert win._market_columns == 5
        assert _grid_rows(wide) == [codes]
        assert win.market_content.minimumSizeHint().width() \
            <= win.market_scroll.viewport().width()
    finally:
        market.clear_cache()
