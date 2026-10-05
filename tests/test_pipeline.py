"""随时可跑：手动跑与定时跑**幂等**，CLI 覆盖生效，失败不崩。

覆盖需求「二、随时可以跑」与「一、选择贯穿全链路」：
- 同一天跑两次：`signal` / `stock_pool` 不产生重复行、不重复推同一批卡片；
- 手动跑（`run_daily`）与定时跑（`Scheduler._maybe_daily` → `run_daily_now`）走同一个函数；
- CLI `--groups` / `--strategies` 临时覆盖、`--once` / `--pool` 可用；
- 观察池（盘中提醒）只含**勾选公式**产生的标的。

2026-09-18（用户要求）：候选**只来自勾选的公式** —— 5 条内置策略退出了匹配链路，
所以下面凡是要"池子里有票"的用例，都必须先勾上一条公式（`_enable_formulas`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader import intraday
from laoa_trader.intraday import now_shanghai  # noqa: E402
from laoa_trader import scheduler as sched
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine


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


def _enable_formulas(monkeypatch, tmp_path, cfg, formulas: dict[str, str]) -> None:
    """写几条公式文件、在配置里勾上它们（2026-09-18 起这是**唯一**的候选来源）。

    小库价格是确定的：600001 = 3.0 元（横盘）、600003 ≈ 12.4 元（每天 −1.2% 跌了 40 天），
    所以 `C<5` 只选 600001、`C>10` 只选 600003 —— 断言仍然精确，不靠"大概能选中"。
    """
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in formulas.items():
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setenv("LAOA_TRADER_FORMULAS", str(folder))
    cfg.enabled_formulas = list(formulas)


# ── 幂等 ──


def test_run_twice_is_idempotent_for_signals_and_watchlist_rows(
        ready_db, tmp_path, monkeypatch) -> None:
    """同一天跑两次：`signal` 不产生重复行；`stock_pool` 里也**只有自选那一行**。

    2026-09-21（主人要求"匹配结果不自动加入股池"）：选出来的票不再写进 `stock_pool`，
    所以"池子行不重复"这件事现在由**自选标的**那条路来验 —— 先加一只自选，再跑两轮，
    库里应当恰好一行（不是两行、也不是三行）。
    """
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5", "反转": "C>10"})
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    with storage.connect(cfg.db_path) as conn:
        # 用户自己加的自选，并**打开了监控**（2026-10-05 起：加自选默认不提醒）——
        # 只有"被盯着的"才会落进 stock_pool
        storage.upsert_watchlist(conn, "600001", name="甲样本", enabled=True)
    first = _run(cfg, monkeypatch, notify=False, with_data=False)
    assert first["pool"], "第一次就该有候选"
    with storage.connect(cfg.db_path) as conn:
        signals_1 = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_1 = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
        pool_rows = [r["symbol"] for r in conn.execute("SELECT symbol FROM stock_pool")]

    second = _run(cfg, monkeypatch, notify=False, with_data=False)
    with storage.connect(cfg.db_path) as conn:
        signals_2 = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
        pool_2 = conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]

    assert signals_1 == signals_2 > 0      # 不产生重复信号行
    assert pool_1 == pool_2 == 1            # 只落自选那一行，且不重复
    assert pool_rows == ["600001"]          # 选出来的票**没有**进池
    assert first["pool"] == second["pool"]
    assert not second["errors"]


def test_run_twice_pushes_only_once(ready_db, tmp_path, monkeypatch) -> None:
    """同一天同一批内容只推一次（否则手动跑 + 定时跑会发两遍卡片）。"""
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5", "反转": "C>10"})
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

    # 池子内容变了（改了勾选的公式）就应该允许再推
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5"})     # 只剩 600001
    third = _run(cfg, monkeypatch, notify=True, with_data=False)
    assert third["pushed"] is True
    assert len(sent) == 2


def test_manual_and_scheduled_share_one_pipeline(ready_db, tmp_path, monkeypatch) -> None:
    """界面手动按钮与 19:15 定时任务必须走**同一个函数**（口径一致才谈得上幂等）。"""
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5"})
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


def test_push_log_records_fingerprint(ready_db, tmp_path, monkeypatch) -> None:
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5"})
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


def test_enabled_formula_flows_to_pool_signals_and_watch(
        ready_db, tmp_path, monkeypatch) -> None:
    """勾上的公式要**贯穿全链路**：进池 → 落 signal → 进盘中观察池。

    这是 2026-09-18 新口径的端到端用例（取代原来那条"按策略组过滤"的）：
    "跑哪些策略"现在只有 `enabled_formulas` 一个答案，链路上任何一环漏掉它都会红。
    """
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"反转": "C>10"})   # 只选 600003
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    report = _run(cfg, monkeypatch, notify=False, with_data=False)
    assert [row["symbol"] for row in report["pool"]] == ["600003"]
    assert report["pool"][0]["strategy"] == "公式·反转"

    with storage.connect(cfg.db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
        stored = [r["symbol"] for r in conn.execute("SELECT symbol FROM stock_pool")]
    assert strategies == {"公式·反转"}          # 信号表里的正是这一轮跑的公式
    # ⚠️ 2026-09-21（主人要求）：**匹配结果不再自动进股池** —— 库里一行都不该有
    assert stored == [], "选出来的票不该自动写进 stock_pool"

    # 观察面：2026-10-05 起**池子里的票默认不盯**，也不用"近期信号"兜底 ——
    # 匹配只是候选，要盯哪只在「自选标的」里打开那一行的监控开关。
    targets, pool_symbols = intraday.watch_targets(cfg.db_path)
    assert set(targets) == set() and pool_symbols == set()

    # 用户在结果页点【加入自选】并打开监控之后（= 写进 watchlist 且 enabled=1），才被盯
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600003", name="丙样本", note="匹配来源：公式·反转",
                                 enabled=True)
    report2 = _run(cfg, monkeypatch, notify=False, with_data=False)
    assert [row["symbol"] for row in report2["pool"]] == ["600003"]
    with storage.connect(cfg.db_path) as conn:
        stored2 = [r["symbol"] for r in conn.execute("SELECT symbol FROM stock_pool")]
    assert stored2 == ["600003"]
    targets2, pool_symbols2 = intraday.watch_targets(cfg.db_path)
    assert pool_symbols2 == {"600003"} and set(targets2) == {"600003"}


def test_watch_targets_only_watches_what_the_user_opened(cfg) -> None:
    """库里存着上一轮的池子 → 那些票是**候选**，用户没打开监控就一只都不盯。

    2026-10-05 主人："默认只监控持仓股票。" 观察面只剩两处来源：持仓（`build_alerts`
    补进来）与他逐只打开监控的自选标的。历史类名（`LowPriceStrategy` 这种）一视同仁。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600001", "name": "低价样本", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy", "score": 3.0},
            {"symbol": "600003", "name": "反转样本", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy", "score": 2.0},
        ], "2026-09-11")
    targets, pool_symbols = intraday.watch_targets(cfg.db_path)
    assert targets == {} and pool_symbols == set()

    # 打开其中一只 → 只有它进观察面
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, "600003", name="反转样本", enabled=True)
    targets, pool_symbols = intraday.watch_targets(cfg.db_path)
    assert set(targets) == {"600003"} and pool_symbols == {"600003"}


def test_watch_targets_keeps_symbol_picked_by_many_formulas(cfg) -> None:
    """一只股票被多条公式同时选中时，只出现一次（目标表按代码去重）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600009", "name": "双策略", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy,ReversalStrategy", "score": 5.0},
        ], "2026-09-11")
        storage.upsert_watchlist(conn, "600009", name="双策略", enabled=True)
    targets, pool_symbols = intraday.watch_targets(cfg.db_path)
    assert pool_symbols == {"600009"}


def test_recent_signals_no_longer_produce_watch_targets(cfg) -> None:
    """**不再**"没有池子就退回最近推送过的信号"：那条兜底会把选出来的票又变成提醒。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_signals(conn, [
            ("2026-09-11", "LowPriceStrategy", "600001", "甲", 3.0, None, "低价股"),
            ("2026-09-11", "ReversalStrategy", "600003", "乙", 12.0, None, "短期反转"),
        ])
    targets, pool_symbols = intraday.watch_targets(cfg.db_path)
    assert targets == {} and pool_symbols == set()


def test_run_daily_without_any_formula_is_normal_not_an_error(
        ready_db, monkeypatch) -> None:
    """**一条公式都没勾 = 只盯自选标的**：不报错、不落信号、照常往下走。

    2026-09-18 起公式默认一条都不勾，所以这是**最常见的正常状态**（不是配置错误）——
    老版本的"没有启用任何策略"报错口径（selection 解析为空 → 写 errors）已经作废。
    """
    cfg = ready_db
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    report = _run(cfg, monkeypatch, notify=False, with_data=False)
    assert report["pool"] == []
    assert report["strategies_off"] is True     # 调度器/界面靠它知道"这轮没有公式标的"
    assert not [e for e in report["errors"] if "没有启用" in e or "公式" in e]
    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == 0


def test_refresh_data_only_syncs(ready_db, monkeypatch) -> None:
    """【只刷新数据】：只跑同步，不匹配、不落信号、不建池、不推送。"""
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


def test_run_daily_reports_stage_names(ready_db, tmp_path, monkeypatch) -> None:
    """界面状态栏靠 stage_cb 显示阶段名："数据增量 / 建池 / 推送通知"。

    （2026-09-18 起**没有"跑策略"这一档**了：候选由建池里的「公式」组现算，
    所以阶段名少了它 —— 断言跟着改。）
    """
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5"})
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"ok": True}})
    stages: list[str] = []
    _run(cfg, monkeypatch, notify=True, with_data=True, stage_cb=stages.append)
    assert stages[:1] == ["数据增量"]
    assert "建池" in stages and "推送通知" in stages
    assert "跑策略" not in stages        # 内置策略退出匹配链路后不再有这一档


def test_broken_stage_callback_does_not_break_run(ready_db, tmp_path, monkeypatch) -> None:
    cfg = ready_db
    _enable_formulas(monkeypatch, tmp_path, cfg, {"低价": "C<5"})
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])

    def boom(_name):
        raise RuntimeError("界面回调炸了")

    report = _run(cfg, monkeypatch, notify=False, with_data=True, stage_cb=boom)
    assert report["pool"]
