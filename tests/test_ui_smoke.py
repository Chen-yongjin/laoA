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

from laoa_trader.data import storage  # noqa: E402
from laoa_trader.data.engine import DataEngine  # noqa: E402
from laoa_trader.notify import KINDS  # noqa: E402
from laoa_trader.strategy import rules as rules_mod  # noqa: E402


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

    # 界面"保存设置"会把值写回这个文件；写一份带注释+未知键的，顺便验证不会丢
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(
        "# 用户自己的注释（保存设置后必须还在）\n"
        f'data_dir = "{cfg.data_dir}"\n'
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
    yield win
    win.scheduler.stop()
    if win.wizard is not None:      # 首次向导（非模态）不要留给下一个用例
        win.wizard.close()
    win.tray.hide()
    win.deleteLater()
    qapp.processEvents()


def test_window_renders_all_panels(window) -> None:
    assert window.pool_table.rowCount() == 1
    assert window.position_table.rowCount() == 1
    assert window.alert_table.rowCount() == 1
    # 五个页签：股票池 / 持仓 / 自选股 / 盘中提醒 / 设置
    assert window.tabs.count() == 5
    assert [window.tabs.tabText(i) for i in range(5)] == [
        "股票池", "持仓", "自选股", "盘中提醒", "设置",
    ]
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


def test_status_bar_shows_engine_state(window) -> None:
    text = window.status_label.text()
    assert "引擎：在线" in text
    assert "最新数据日期：2026-09-11" in text
    assert "今日池子：1 只" in text
    assert "持仓浮动" in text
    assert "定时：16:00" in text                       # 新默认：收盘后主跑
    assert "补跑 19:15" in text
    assert "下次自动运行：" in text                    # 一眼看出设定生效了


def test_intraday_pause_button_toggles(window) -> None:
    assert window.btn_pause.text() == "暂停盘中提醒"
    window.on_toggle_intraday()
    assert window.btn_pause.text() == "恢复盘中提醒"
    assert window.scheduler.status()["intraday_paused"] is True
    window.on_toggle_intraday()
    assert window.btn_pause.text() == "暂停盘中提醒"
    assert window.scheduler.status()["intraday_paused"] is False


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
    assert "6 位股票代码" in window.status_label.text()

    window.pos_symbol.setText("600001")
    window.pos_qty.setText("不是数字")
    window.on_add_position()
    assert "必须是数字" in window.status_label.text()


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
    assert "写入 10 行" in window.status_label.text()

    window._on_worker_failed("下载", "RuntimeError: 断网了\n堆栈……")
    assert "下载失败" in window.status_label.text()
    assert "断网了" in window.status_label.text()

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
    text = window.status_label.text()
    assert "飞书已跳过" in text
    assert "弹窗失败" in text
    assert "托盘成功" in text


def test_doctor_command_prints_report(cfg, capsys, tmp_path) -> None:
    """`--cli --doctor` 自检：路径/依赖/凭据/数据概况，排障第一步。"""
    from laoa_trader.__main__ import cli

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{cfg.data_dir}"', encoding="utf-8")
    assert cli(["--cli", "--doctor", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "老A法师 · 交易终端 —— 自检" in out
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
    assert "已写入 config.toml" in window.status_label.text()
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
    assert "至少要勾一个策略组" in window.status_label.text()
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
    assert "不推送（只入库）" in window.status_label.text()
    assert 'notify_channels = []' in (seeded.data_dir / "config.toml").read_text(
        encoding="utf-8")


def test_save_notify_feishu_without_credentials_hints(window, seeded, qapp) -> None:
    """勾了飞书但没凭证：保存成功 + 明确提示"会自动跳过飞书"，不弹错误框。"""
    window.channel_boxes["feishu"].setChecked(True)
    window.feishu_app_id.setText("")
    window.feishu_secret.setText("")
    window.on_save_notify()
    qapp.processEvents()
    assert "飞书未配置凭证" in window.status_label.text()
    assert "winotify" not in window.status_label.text()   # 不是那句平台不支持的提示


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
    assert "测试通知" in window.status_label.text()
    assert "飞书已跳过" in window.status_label.text()
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
    assert "立即选股并建池完成" in window.status_label.text()
    assert "池子" in window.status_label.text()

    # 第二次点：幂等（行数不变），且不重复推送
    window.on_run_pipeline()
    window._worker.wait(60_000)
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == signals
        assert conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == pool_rows
    assert "未重复推送" in window.status_label.text()


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
    assert "刷新数据完成" in window.status_label.text()
    assert "写入 3 行" in window.status_label.text()


def test_refresh_data_failure_shows_reason_without_crash(window, qapp, monkeypatch) -> None:
    from laoa_trader import scheduler as sched

    monkeypatch.setattr(sched.sync, "daily_update",
                        lambda *a, **k: [sched.sync.SyncResult(stage="日更数据", ok=False,
                                                              error="模拟断网（测试假客户端）")])
    window.on_refresh_data()
    window._worker.wait(30_000)
    qapp.processEvents()
    assert "刷新数据完成" in window.status_label.text()
    assert "模拟断网" in window.status_label.text()


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
    assert "已加自选：600002 半导体甲" in window.status_label.text()


def test_watch_panel_add_unknown_symbol_warns_but_adds(window, seeded, qapp) -> None:
    window.watch_symbol.setText("601999")
    window.on_watch_add()
    qapp.processEvents()
    with storage.connect(seeded.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["601999"]
    assert "本地库没有它的名称" in window.status_label.text()


def test_watch_panel_rejects_bad_code(window, seeded) -> None:
    window.watch_symbol.setText("abc")
    window.on_watch_add()
    assert "6 位数字" in window.status_label.text()
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
    assert "不进池、不监控" in window.status_label.text()

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
    """自选股进池后：来源列能区分「自选」「策略+自选」，备注单独一列。"""
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


def test_pool_table_signature_includes_note(window, seeded) -> None:
    """备注变了表格要重画（否则用户改完备注界面不刷新）。"""
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
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600002", note="备注二")
    window._refresh_pool()
    assert note_of("600002") == "备注二"


def test_status_bar_shows_watchlist_count(window, seeded, qapp) -> None:
    with storage.connect(seeded.db_path) as conn:
        storage.upsert_watchlist(conn, "600001", name="低价样本")
    window._tick()
    qapp.processEvents()
    assert "含自选 1 只" in window.status_label.text()


# ── 启动自检与首次向导 ──


@pytest.fixture()
def wizard_window(cfg, qapp, monkeypatch):
    """一个**空库**的主窗口：启动自检必然判 needs_full → 弹首次向导。"""
    from laoa_trader.ui import app as ui_app

    cfg.min_history_years = 9          # 空库无论如何都不可用
    # 向导里点"开始下载"会把 Key 写回配置文件；给一个真实的临时路径，
    # 免得落到 ~/.config（沙箱里可能不可写、也不是本用例要测的东西）
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(f'data_dir = "{cfg.data_dir}"', encoding="utf-8")
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
    assert "正在检查本地数据" not in win.status_label.text()   # 已给出结论


def test_wizard_start_triggers_download_and_progress(qapp, wizard_window, monkeypatch) -> None:
    """点【开始下载】→ 触发下载回调、进度更新不崩、完成后自动跑一次选股建池。"""
    from laoa_trader.data import sync as sync_mod
    from laoa_trader.ui import app as ui_app

    seen: dict = {}
    pipeline_calls: list[str] = []

    def fake_download(cfg_, progress_cb=None, should_stop=None, **kwargs):
        seen["should_stop"] = should_stop
        if progress_cb:
            progress_cb("下载全市场日K（10 年）", 1, 2)
            progress_cb("写入行情", 2, 2)
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=42,
                                   detail="写入 42 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    monkeypatch.setattr(ui_app.MainWindow, "on_run_pipeline",
                        lambda self: pipeline_calls.append("pipeline"))

    win = wizard_window
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
    assert "本地数据就绪" in win.status_label.text()
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
    assert "落后" in win.status_label.text() or "落后" in win._message
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
    assert "下次自动运行：" in window.status_label.text()


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
    assert "自动运行已保存" in window.status_label.text()


def test_save_run_at_rejects_bad_format(window, seeded, qapp) -> None:
    """非法格式 → 拒绝保存，**config 不变**，给出中文提示。"""
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    window.run_at_edit.setText("25:99")
    window.on_save_run_at()
    qapp.processEvents()
    assert "格式不对" in window.status_label.text()
    assert "未写入配置" in window.status_label.text()
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before
    assert window.cfg.run_at == "16:00"                # 内存也没被改


def test_save_run_at_rejects_fallback_not_later(window, seeded, qapp) -> None:
    before = (seeded.data_dir / "config.toml").read_text(encoding="utf-8")
    window.run_at_edit.setText("16:00")
    window.run_at_fallback_edit.setText("15:30")
    window.on_save_run_at()
    qapp.processEvents()
    assert "必须**晚于**主跑时间" in window.status_label.text()
    assert (seeded.data_dir / "config.toml").read_text(encoding="utf-8") == before


def test_save_run_at_warns_before_market_close(window, seeded, qapp) -> None:
    """早于 15:00 收盘 → **保存成功**但明确提示。"""
    window.run_at_edit.setText("09:30")
    window.on_save_run_at()
    qapp.processEvents()
    text = window.status_label.text()
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
    assert "自动运行已关闭" in window.status_label.text()     # 状态栏说清楚


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
