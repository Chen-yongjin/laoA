"""随时可跑：手动跑与定时跑**幂等**，CLI 覆盖生效，失败不崩。

覆盖需求「二、随时可以跑」与「一、选择贯穿全链路」：
- 同一天跑两次：`signal` / `stock_pool` 不产生重复行、不重复推同一批卡片；
- 手动跑（`run_daily`）与定时跑（`Scheduler._maybe_daily` → `run_daily_now`）走同一个函数；
- CLI `--groups` / `--strategies` 临时覆盖、`--once` / `--pool` 可用；
- 观察池（盘中提醒）只含启用组产生的标的。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader import intraday
from laoa_trader.intraday import now_shanghai  # noqa: E402
from laoa_trader import scheduler as sched
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import groups


@pytest.fixture()
def ready_db(cfg):
    """一份能跑出池子的行情库（低价股 + 反转各一条候选）。"""
    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()
    n = len(days)

    def bars(symbol, closes, volumes):
        out = []
        for i, day in enumerate(days):
            close = float(closes[i])
            volume = float(volumes[i])
            out.append((symbol, day, close, close, close, close, volume, volume * close))
        return out

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "低价样本", "银行"),
            ("600003", "反转样本", "半导体"),
        ])
        storage.write_daily_raw(conn, bars("600001", [3.0] * n, [2e7] * n)
                                + bars("600003", [20.0 * (0.988 ** i) for i in range(n)],
                                       [2e7] * n))
        storage.write_calendar(conn, days)
    return cfg


def _run(cfg, monkeypatch, **kwargs):
    """跑一次 run_daily（不推送，除非显式要求）。"""
    return sched.run_daily(cfg, DataEngine(cfg.db_path), **kwargs)


# ── 幂等 ──


def test_run_twice_is_idempotent_for_signals_and_pool(ready_db, monkeypatch) -> None:
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    first = _run(cfg, monkeypatch, notify=False, with_data=False)
    assert first["pool"], "第一次就该有池子"
    with storage.connect(cfg.db_path) as conn:
        signals_1 = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_1 = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]

    second = _run(cfg, monkeypatch, notify=False, with_data=False)
    with storage.connect(cfg.db_path) as conn:
        signals_2 = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_2 = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]

    assert signals_1 == signals_2 > 0      # 不产生重复信号行
    assert pool_1 == pool_2 > 0             # 不产生重复池子行
    assert first["pool"] == second["pool"]
    assert not second["errors"]


def test_run_twice_pushes_only_once(ready_db, monkeypatch) -> None:
    """同一天同一批内容只推一次（否则手动跑 + 定时跑会发两遍卡片）。"""
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    sent: list[str] = []

    def fake_notify_all(title, lines, kinds=None, cfg=None):
        sent.append(title)
        return {"tray": {"kind": "tray", "ok": True, "detail": "已投递"}}

    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify_all)

    first = _run(cfg, monkeypatch, notify=True, with_data=False)
    assert first["pushed"] is True
    assert len(sent) == 1

    second = _run(cfg, monkeypatch, notify=True, with_data=False)
    assert second["pushed"] is False
    assert second["push_skipped"]
    assert "已推送过" in second["push_skipped"]
    assert len(sent) == 1                    # 没有第二次推送

    # 池子内容变了（换一天/换策略）就应该允许再推
    third = _run(cfg, monkeypatch, notify=True, with_data=False,
                 selection=groups.resolve(["swing"], []))
    assert third["pushed"] is True
    assert len(sent) == 2


def test_manual_and_scheduled_share_one_pipeline(ready_db, monkeypatch) -> None:
    """界面手动按钮与 19:15 定时任务必须走**同一个函数**（口径一致才谈得上幂等）。"""
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    pushed: list[str] = []
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda title, lines, kinds=None, cfg=None:
                        pushed.append(title) or {"tray": {"ok": True}})

    scheduler = sched.Scheduler(cfg, DataEngine(cfg.db_path))
    manual = scheduler.run_daily_now(notify=True, with_data=False)
    assert manual["pushed"] is True
    assert scheduler.status()["last_daily"]["pushed"] is True

    # 定时点再触发一次：同一天不应该重复跑（也不重复推）
    scheduler._maybe_daily(now_shanghai() + timedelta(minutes=1))
    assert len(pushed) == 1


def test_push_log_records_fingerprint(ready_db, monkeypatch) -> None:
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda title, lines, kinds=None, cfg=None: {"tray": {"ok": True}})
    report = _run(cfg, monkeypatch, notify=True, with_data=False)
    assert report["pushed"] is True
    with storage.connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT day, kind, fingerprint FROM push_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "pool"
        assert rows[0]["day"] == report["data_date"]
        assert len(rows[0]["fingerprint"]) == 16
        assert storage.data_summary(conn)["pushed_days"] == 1


def test_pool_fingerprint_changes_with_content() -> None:
    a = sched.pool_fingerprint("标题", ["一行"])
    b = sched.pool_fingerprint("标题", ["另一行"])
    c = sched.pool_fingerprint("标题", ["一行"])
    assert a != b
    assert a == c


# ── 选择贯穿：池子 / 信号 / 观察池 ──


def test_selection_flows_to_pool_signals_and_watch(ready_db, monkeypatch) -> None:
    """禁用 swing 后：策略不跑、信号不落、池子不含该组标的、观察池也不盯它。"""
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    selection = groups.resolve(["short"], [])
    report = _run(cfg, monkeypatch, notify=False, with_data=False, selection=selection)
    assert report["pool"], "短线组应该有候选（短期反转）"
    assert all(row["strategy"] in set(selection.strategies) for row in report["pool"])
    assert "600001" not in [row["symbol"] for row in report["pool"]]

    with storage.connect(cfg.db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
    assert strategies and strategies <= set(selection.strategies)

    # 观察池：只盯启用组产生的标的
    targets, pool_symbols = intraday.watch_targets(cfg.db_path, selection=selection)
    assert targets
    assert all(sym != "600001" for sym in pool_symbols)
    assert "600001" not in targets


def test_watch_targets_filters_stale_pool_rows(cfg, monkeypatch) -> None:
    """库里存着上一轮（含被禁用组）的池子时，观察池也要按选择过滤掉。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600001", "name": "低价样本", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy", "score": 3.0},
            {"symbol": "600003", "name": "反转样本", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy", "score": 2.0},
        ], "2026-09-11")
    targets, pool_symbols = intraday.watch_targets(
        cfg.db_path, selection=groups.resolve(["short"], [])
    )
    assert pool_symbols == {"600003"}
    assert set(targets) == {"600003"}


def test_watch_targets_keeps_symbol_picked_by_both_groups(cfg) -> None:
    """一只股票被两条策略同时选中（跨组）时，只要有一条在启用范围内就保留。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600009", "name": "双策略", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy,ReversalStrategy", "score": 5.0},
        ], "2026-09-11")
    targets, pool_symbols = intraday.watch_targets(
        cfg.db_path, selection=groups.resolve(["short"], [])
    )
    assert pool_symbols == {"600009"}


def test_watch_targets_filters_recent_signals(cfg) -> None:
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_signals(conn, [
            ("2026-09-11", "LowPriceStrategy", "600001", "甲", 3.0, None, "低价股"),
            ("2026-09-11", "ReversalStrategy", "600003", "乙", 12.0, None, "短期反转"),
        ])
    targets, pool_symbols = intraday.watch_targets(
        cfg.db_path, selection=groups.resolve(["short"], [])
    )
    assert pool_symbols == set()             # 没有池子
    assert set(targets) == {"600003"}        # 信号兜底也要按选择过滤


def test_run_daily_refuses_when_selection_empty(ready_db, monkeypatch) -> None:
    """选择解析为空（名字全拼错）时：不跑、不落库，并明确说明原因。"""
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    report = _run(cfg, monkeypatch, notify=False, with_data=False,
                  selection=groups.resolve(["nope"], []))
    assert report["pool"] == []
    assert report["errors"] and "没有启用任何策略" in report["errors"][0]
    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0


def test_refresh_data_only_syncs(ready_db, monkeypatch) -> None:
    """【只刷新数据】：只跑同步，不选股、不落信号、不建池、不推送。"""
    cfg = ready_db
    calls: list[str] = []

    def fake_daily_update(cfg_, progress_cb=None):
        calls.append("sync")
        return [sched.sync.SyncResult(stage="日更数据", ok=True, rows=5,
                                      detail="写入 5 行")]

    monkeypatch.setattr(sched.sync, "daily_update", fake_daily_update)
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: pytest.fail("只刷新数据不该推送"))
    results = sched.refresh_data(cfg, DataEngine(cfg.db_path))
    assert calls == ["sync"]
    assert results and results[0].ok
    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == 0


def test_refresh_data_never_raises(cfg, monkeypatch) -> None:
    """同步层抛异常时也要返回结构化失败结果（界面不崩）。"""
    def boom(*a, **k):
        raise RuntimeError("断网了")

    monkeypatch.setattr(sched.sync, "daily_update", boom)
    results = sched.refresh_data(cfg, DataEngine(cfg.db_path))
    assert results and results[0].ok is False
    assert "断网了" in results[0].error


def test_run_daily_reports_stage_names(ready_db, monkeypatch) -> None:
    """界面状态栏靠 stage_cb 显示"跑策略/建池/推送通知"。"""
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"ok": True}})
    stages: list[str] = []
    _run(cfg, monkeypatch, notify=True, with_data=True, stage_cb=stages.append)
    assert stages[:1] == ["数据增量"]
    assert "跑策略" in stages and "建池" in stages and "推送通知" in stages


def test_broken_stage_callback_does_not_break_run(ready_db, monkeypatch) -> None:
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])

    def boom(_name):
        raise RuntimeError("界面回调炸了")

    report = _run(cfg, monkeypatch, notify=False, with_data=True, stage_cb=boom)
    assert report["pool"]
