"""自选股并入股票池：豁免热门行业、不占名额、去重、监控、上限、停用、自动补名称。

覆盖需求「自选股并入股票池，一起实时监控」的全部验收点：
    1. 自选股**豁免**热门行业过滤；
    2. 自选股**不占**策略名额；
    3. 既是策略又是自选 → 池里**一行**、来源「策略+自选」；
    4. **策略池为空也必须盯自选**；
    5. `watchlist_max` 超限**提示**而非静默丢弃；
    6. 停用不进池/不监控，重新启用后恢复；
    7. 添加时自动补名称（库里查得到用库里的；查不到允许添加并提示）；
    8. 备注进入盘中提醒文案（`自选（龙头，成本 12.40）`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader import intraday, pool
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import groups
from tests.conftest import READY_THRESHOLDS


def _days(count: int = 40) -> list[str]:
    end = datetime(2026, 9, 11).date()
    out: list[str] = []
    cursor = end
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _bars(symbol: str, closes: list[float], volumes: list[float]) -> list[tuple]:
    rows = []
    for i, day in enumerate(_days(len(closes))):
        close = float(closes[i])
        volume = float(volumes[i])
        rows.append((symbol, day, close, close, close, close, volume, volume * close))
    return rows


@pytest.fixture()
def wl_db(cfg):
    """合成库：两条策略候选（600001 低价股 / 600003 短期反转）+ 两只"只做自选用"的标的。

    600100（纺织，冷门行业）与 600200（钢铁）成交量极低，**不会被任何策略选中** ——
    这样"自选"与"策略"两条路径在测试里互不干扰，断言才准确。
    """
    storage.init_db(cfg.db_path)
    n = 40
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "低价样本", "银行"),        # 低价股策略会选中
            ("600003", "反转样本", "半导体"),      # 短期反转会选中
            ("600100", "冷门样本", "纺织"),        # 不在热门行业里（用来自选）
            ("600200", "自选二号", "钢铁"),
        ])
        storage.write_daily_raw(
            conn,
            _bars("600001", [3.0] * n, [2e7] * n)
            + _bars("600003", [20.0 * (0.988 ** i) for i in range(n)], [2e7] * n)
            # 自选专用标的：成交量极低（日均成交额 ≈ 80 万 < 3000 万），
            # 因此**任何策略都不会选中它们** —— 测试才能干净地只验证自选股那条路径
            + _bars("600100", [8.0] * n, [1e5] * n)
            + _bars("600200", [12.0] * n, [1e5] * n),
        )
        storage.write_calendar(conn, days := _days(n))
        # 复权事件非空是"自检就绪"的硬条件之一
        storage.write_adjust_events(conn, [("600001", days[n // 2], 0.1, 0.0, 0.0, 0.0)])
        storage.write_limit_up_pool(conn, [
            (days[-1], "600003", "反转样本", 1, "首板", "09:35:00", "09:35:00",
             8e7, 0, "芯片", 5.0, 10.0, 1e9, 0, 1, "首板", 9e7, 12.0, 0, "hithink", "t"),
        ])
    return cfg


@pytest.fixture()
def engine(wl_db) -> DataEngine:
    return DataEngine(wl_db.db_path)


def _add(cfg, symbol: str, note: str = "", name: str | None = None, enabled: bool = True):
    with storage.connect(cfg.db_path) as conn:
        return storage.upsert_watchlist(conn, symbol, name=name, note=note, enabled=enabled)


# ── 存储层 ──


def test_watchlist_table_and_crud(wl_db) -> None:
    cfg = wl_db
    with storage.connect(cfg.db_path) as conn:
        assert storage.load_watchlist(conn) == []
        row = storage.upsert_watchlist(conn, "600100", name="冷门样本", note="龙头")
        assert row["symbol"] == "600100"
        assert row["enabled"] == 1
        assert row["added_at"]
        # 幂等：重复添加只更新，不产生第二行
        storage.upsert_watchlist(conn, "600100", note="龙头二号")
        rows = storage.load_watchlist(conn)
        assert len(rows) == 1
        assert rows[0]["note"] == "龙头二号"
        assert rows[0]["name"] == "冷门样本"         # 空名字不会冲掉已缓存名称
        # 停用 / 启用
        assert storage.set_watchlist_enabled(conn, "600100", False) is True
        assert storage.load_watchlist(conn, enabled_only=True) == []
        assert len(storage.load_watchlist(conn)) == 1        # 列表里还在
        storage.set_watchlist_enabled(conn, "600100", True)
        assert storage.watchlist_symbols(conn) == ["600100"]
        # 删除
        assert storage.remove_watchlist(conn, "600100") is True
        assert storage.remove_watchlist(conn, "600100") is False
        assert storage.load_watchlist(conn) == []


def test_watchlist_survives_reexisting_db_upgrade(cfg) -> None:
    """老库（没有 watchlist 表）打开时自动补建，不用删库重下数据。"""
    import sqlite3

    cfg.ensure_dirs()
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("CREATE TABLE stock_daily_raw (symbol TEXT, date TEXT)")   # 老库最小痕迹
        conn.commit()
    with storage.connect(cfg.db_path) as conn:      # 打开即应补齐全部表
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "watchlist" in tables
    assert "push_log" in tables


def test_data_summary_counts_enabled_watchlist(wl_db) -> None:
    _add(wl_db, "600100")
    _add(wl_db, "600200", enabled=False)
    with storage.connect(wl_db.db_path) as conn:
        assert storage.data_summary(conn)["watchlist"] == 1


# ── 1) 豁免热门行业过滤 ──


def test_watchlist_exempt_from_hot_industry_filter(engine, wl_db, monkeypatch) -> None:
    """自选股不在任何热门行业里，也必须留在池子里（用户自己选的，就是要盯）。"""
    # 只把"半导体"当热门（策略候选里的 600001 在银行 → 会被过滤掉）
    monkeypatch.setattr(pool, "hot_industries",
                        lambda db_path, **kw: {"半导体": {"score": 1.0, "limit_up": 1}})
    _add(wl_db, "600100", note="龙头")          # 纺织（冷门）
    rows = pool.build_pool(engine, wl_db, hot_only=True, save=False)
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols                  # 自选豁免热门过滤
    assert "600001" not in symbols              # 策略标的照旧被过滤掉（银行不热门）
    cold = [r for r in rows if r["symbol"] == "600100"][0]
    assert cold["source"] == "自选"
    assert cold["note"] == "龙头"
    assert cold["strategy"] == ""               # 不挂策略名


def test_watchlist_not_in_pool_when_disabled_by_config(engine, wl_db) -> None:
    """watchlist_in_pool=false → 自选只记录，不进池。"""
    _add(wl_db, "600100")
    wl_db.watchlist_in_pool = False
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    assert "600100" not in [r["symbol"] for r in rows]


# ── 2) 不占策略名额 ──


def test_watchlist_does_not_consume_strategy_quota(engine, wl_db) -> None:
    """池子大小只限制策略标的：策略取满 1 只时，自选股仍全部在池。"""
    for symbol in ("600100", "600200"):
        _add(wl_db, symbol)
    rows = pool.build_pool(engine, wl_db, size=1, hot_only=False, save=False)
    strategy_rows = [r for r in rows if r["strategy"]]
    watch_rows = [r for r in rows if not r["strategy"]]
    assert len(strategy_rows) == 1                       # size 只作用于策略标的
    assert {r["symbol"] for r in watch_rows} == {"600100", "600200"}
    assert len(rows) == 3


def test_strategy_rows_still_capped_by_size(engine, wl_db) -> None:
    """没有自选股时，行为与原来完全一致（size 仍然生效）。"""
    rows = pool.build_pool(engine, wl_db, size=1, hot_only=False, save=False)
    assert len(rows) == 1


def test_strategy_rows_come_first(engine, wl_db) -> None:
    """顺序：策略标的（按分数降序）在前，纯自选在后。"""
    _add(wl_db, "600100")
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    assert rows[-1]["symbol"] == "600100"
    assert all(r["strategy"] for r in rows[:-1])


# ── 3) 去重 ──


def test_same_symbol_in_both_sources_appears_once(engine, wl_db) -> None:
    """既是策略选中又是自选 → 池里一行，来源「策略+自选」。"""
    # 600001 一定被低价股策略选中（3 元、流动性够）
    _add(wl_db, "600001", note="老朋友")
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    hits = [r for r in rows if r["symbol"] == "600001"]
    assert len(hits) == 1
    assert hits[0]["source"] == "策略+自选"
    assert hits[0]["note"] == "老朋友"
    assert hits[0]["strategy"] == "LowPriceStrategy"     # 策略信息保留


def test_pool_table_rows_source_labels(engine, wl_db) -> None:
    """界面/CLI 的「来源」列文本：组名（T+N）/ 自选 / 组名 + 自选。"""
    _add(wl_db, "600001", note="老朋友")     # 策略 + 自选
    _add(wl_db, "600100", note="龙头")       # 纯自选
    pool.build_pool(engine, wl_db, hot_only=False, save=True, day="2026-09-11")
    rows = {r["symbol"]: r for r in pool.pool_table_rows(wl_db.db_path)}
    assert rows["600001"]["source"] == "策略+自选"
    assert rows["600001"]["source_label"] == "波段·T+10（T+10） + 自选"
    assert rows["600001"]["note"] == "老朋友"
    assert rows["600100"]["source"] == "自选"
    assert rows["600100"]["source_label"] == "自选"
    assert rows["600100"]["note"] == "龙头"


def test_save_pool_persists_watchlist_rows(engine, wl_db) -> None:
    """自选股要真的落到 stock_pool 里（盘中提醒按池子盯）。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    pool.build_pool(engine, wl_db, hot_only=False, save=True, day="2026-09-11")
    stored = {r["symbol"]: r for r in pool.load_pool(wl_db.db_path)}
    assert "600100" in stored
    assert stored["600100"]["name"] == "冷门样本"
    assert stored["600100"]["strategy"] == ""


# ── 4) 策略池为空也必须盯自选 ──


def test_watch_targets_monitors_watchlist_with_empty_pool(wl_db) -> None:
    """策略池为空（库里没有任何池子）→ 依然盯自选股。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path)
    assert set(targets) == {"600100"}
    assert pool_symbols == {"600100"}          # 也在"池内买点"的适用范围内
    assert targets["600100"]["source"] == "自选"
    assert targets["600100"]["note"] == "龙头"


def test_watch_targets_ignores_watchlist_when_config_off(wl_db) -> None:
    """watchlist_in_pool=false → 自选只记录不监控。"""
    _add(wl_db, "600100")
    wl_db.watchlist_in_pool = False
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert targets == {} and pool_symbols == set()


def test_run_daily_with_strategies_off_still_pools_watchlist(wl_db, monkeypatch) -> None:
    """`enabled_groups = ["none"]`（策略全关）→ 池子里只有自选股，且不报配置错误。"""
    import laoa_trader.scheduler as sched

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"kind": "tray", "ok": True}})
    selection = groups.resolve(["none"], [])
    assert selection.explicit_off is True
    report = sched.run_daily(wl_db, DataEngine(wl_db.db_path), notify=True,
                             with_data=False, selection=selection)
    assert [r["symbol"] for r in report["pool"]] == ["600100"]
    assert report["picks"] == 0                       # 没跑策略
    assert report["errors"] == []                     # 这是正常用法，不是配置错误
    assert report["pushed"] is True                   # 该提醒还是提醒
    assert report["strategies_off"] is True


def test_run_daily_watchlist_only_mode_pushes_note(wl_db, monkeypatch) -> None:
    """只盯自选时推送文案要带备注（否则用户看不出为什么盯它）。"""
    import laoa_trader.scheduler as sched

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    captured: dict = {}

    def fake_notify(title, lines, kinds=None, cfg=None):
        captured["title"] = title
        captured["lines"] = lines
        return {"tray": {"ok": True}}

    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify)
    sched.run_daily(wl_db, DataEngine(wl_db.db_path), notify=True, with_data=False,
                    selection=groups.resolve(["none"], []))
    body = "\n".join(captured["lines"])
    assert "冷门样本（600100）自选（龙头）" in body


# ── 5) 上限 ──


def test_watchlist_max_caps_and_reports(engine, wl_db) -> None:
    """超过 watchlist_max：只监控前 N 只，并**明确提示**（不静默丢弃）。"""
    wl_db.watchlist_max = 2
    for symbol in ("600100", "600200", "600001", "600003"):
        _add(wl_db, symbol)
    report: dict = {}
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    watch_rows = [r for r in rows if r.get("source") in ("自选", "策略+自选")]
    assert len(watch_rows) == 2                       # 只纳入前 2 只
    assert report["watchlist"] == 2
    assert report["watchlist_dropped"] == 2
    assert any("超过上限" in w for w in report["warnings"])
    assert "watchlist_max" in report["warnings"][0]


def test_watchlist_max_zero_means_none(engine, wl_db) -> None:
    wl_db.watchlist_max = 0
    _add(wl_db, "600100")
    report: dict = {}
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    assert "600100" not in [r["symbol"] for r in rows]
    assert report["watchlist_dropped"] == 1


def test_watchlist_max_ok_when_not_exceeded(engine, wl_db) -> None:
    _add(wl_db, "600100")
    report: dict = {}
    pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    assert report.get("watchlist_dropped") is None
    assert report["watchlist"] == 1


# ── 6) 停用/启用 ──


def test_disabled_watchlist_not_in_pool_nor_watched(wl_db) -> None:
    _add(wl_db, "600100", name="冷门样本")
    _add(wl_db, "600200", name="自选二号")
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", False)
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols and "600200" not in symbols
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in targets and "600200" not in targets


def test_reenabled_watchlist_comes_back(wl_db) -> None:
    _add(wl_db, "600200", name="自选二号")
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", False)
    assert "600200" not in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", True)
    assert "600200" in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600200" in targets


# ── 7) 自动补名称 ──


def test_name_autofilled_from_local_db(engine, wl_db) -> None:
    name = engine.get_stock_names(["600100"]).get("600100")
    assert name == "冷门样本"
    row = _add(wl_db, "600100", note="龙头", name=name)
    assert row["name"] == "冷门样本"


def test_unknown_symbol_still_addable(wl_db) -> None:
    """库里查不到名称也允许添加（刚上市/改名），名称留空由后续同步补。"""
    row = _add(wl_db, "601999", note="新股")
    assert row["symbol"] == "601999"
    assert row["name"] is None
    assert row["note"] == "新股"
    assert "601999" in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]


# ── 8) 盘中提醒：参考价与备注 ──


def test_watchlist_reference_price_uses_position_cost(wl_db) -> None:
    """有持仓 → 用**持仓成本**算止损/止盈。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    with storage.connect(wl_db.db_path) as conn:
        storage.upsert_position(conn, "600100", name="冷门样本", quantity=1000,
                                avg_cost=12.40)
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    info = targets["600100"]
    assert info["cost"] == pytest.approx(12.40)
    assert info["close"] == pytest.approx(12.40)        # 作为止损/止盈参考价
    assert info["label"] == "自选（龙头，成本 12.40）"
    # 规则层：现价跌破成本 5% → 触发止损
    hits = intraday.evaluate_sell_rules(
        "600100", {"last_price": 11.7},
        {"prev_close": 8.0, "ma5": 8.0}, info["close"], cfg=wl_db,
    )
    assert [h[0] for h in hits] == ["stop_loss"]


def test_watchlist_without_position_uses_prev_close(wl_db) -> None:
    """没有持仓 → 参考价留空，规则层回退到"前一交易日收盘"。"""
    _add(wl_db, "600100", name="冷门样本")
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    info = targets["600100"]
    assert info["close"] is None and info["cost"] is None
    assert info["label"] == "自选"
    ctx = intraday.history_context(wl_db.db_path, ["600100"])["600100"]
    hits = intraday.evaluate_sell_rules(
        "600100", {"last_price": ctx["prev_close"] * 0.9}, ctx, None, cfg=wl_db,
    )
    kinds = [h[0] for h in hits]
    assert "stop_loss" in kinds          # 参考价确实是"前一交易日收盘"
    # 跌 10% 时同时跌破 5 日线也是正常的（横盘股的 5 日线≈昨收）
    assert set(kinds) <= {"stop_loss", "break_ma5"}


def test_watchlist_alert_text_includes_note(wl_db) -> None:
    """盘中提醒文案带上备注（自选（龙头））。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")

    class Client:
        def snapshot(self, symbols=None, **kw):
            # 远低于参考价 → 触发止损
            return [{"ticker": "600100", "thscode": "600100.SH", "last_price": 5.0,
                     "price_change_ratio_pct": -40.0, "turnover": 1e9}]

        def limit_up_pool(self, day=None, size=200):
            return []

    alerts = intraday.build_alerts(DataEngine(wl_db.db_path), Client())
    assert alerts
    assert "自选（龙头）" in alerts[0]["name"]
    title, lines = intraday.format_message(alerts, db_path=wl_db.db_path, cfg=wl_db)
    assert "龙头" in "\n".join(lines)
    assert "🛑 触及止损" in "\n".join(lines)


def test_watchlist_gets_pool_buy_rules(wl_db) -> None:
    """自选股适用「池内回踩买点 / 放量突破20日高」两类买点规则。"""
    _add(wl_db, "600100", name="冷门样本")
    _, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in pool_symbols       # 池内买点（回踩 5 日线）
    ctx = intraday.history_context(wl_db.db_path, ["600100"])["600100"]
    snap = {"last_price": ctx["ma5"] * 1.005, "prev_close": ctx["prev_close"]}
    hits = intraday.evaluate_pool_buy_rules("600100", snap, ctx)
    assert [h[0] for h in hits] == ["pullback_ma5_buy"]


def test_watchlist_alerts_use_same_notify_channels(wl_db, monkeypatch) -> None:
    """自选股提醒走同一套 notify_channels（不搞特殊分支）。"""
    from laoa_trader.notify import feishu, notify_all, tray, windows

    _add(wl_db, "600100")
    wl_db.notify_channels = ["tray"]
    called: list[str] = []
    for name, module in (("windows", windows), ("feishu", feishu), ("tray", tray)):
        monkeypatch.setattr(
            module, "notify",
            lambda title, lines, cfg=None, _n=name, **kw: (
                called.append(_n) or {"kind": _n, "ok": True, "detail": "ok"}
            ),
        )
    results = notify_all("⚡ 盘中提醒", ["自选（龙头）｜…"], cfg=wl_db)
    assert called == ["tray"]                     # 只有配置里的频道真的被调用
    assert results["windows"]["skipped"] is True  # 其余显示"跳过"（不是失败）
    assert results["feishu"]["skipped"] is True
    assert results["tray"]["ok"] is True


# ── 与策略选择的关系 ──


def test_watchlist_kept_even_when_all_groups_disabled(wl_db) -> None:
    """策略组全关（选择为空）时，池子里仍要有自选股。"""
    _add(wl_db, "600100", name="冷门样本")
    selection = groups.resolve(["none"], [])
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=True,
                           save=False, picks={}, selection=selection)
    assert [r["symbol"] for r in rows] == ["600100"]


def test_watchlist_rows_survive_strategy_selection_filter(wl_db) -> None:
    """只启用某些组时，自选股不受影响（它不属于任何组）。"""
    _add(wl_db, "600100", name="冷门样本")
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=False,
                           save=False, selection=groups.resolve(["ultra"], []))
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols
    # 反例：只启用 ultra 时，低价股（swing）的标的不会进池
    assert "600001" not in symbols


# ── CLI / 推送文案：策略关闭与"策略+自选"标注 ──


def test_cli_once_with_strategies_off(wl_db, tmp_path, capsys, monkeypatch) -> None:
    """`--once` 在 enabled_groups=["none"] 时不该报错退出，而要只处理自选股。"""
    from laoa_trader.__main__ import cli
    import laoa_trader.scheduler as sched

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{wl_db.data_dir}"\nhithink_api_key = ""\n'
        'enabled_groups = ["none"]\nnotify_channels = []\n' + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "策略已关闭" in out
    assert "冷门样本（600100）" in out
    assert "没有启用任何策略" not in out


def test_cli_once_still_fails_on_typo_selection(wl_db, tmp_path, capsys, monkeypatch) -> None:
    """名字拼错还是配置错误：明确报错并返回非零。"""
    from laoa_trader.__main__ import cli
    import laoa_trader.scheduler as sched

    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{wl_db.data_dir}"\nhithink_api_key = ""\n'
        'enabled_groups = ["nope"]\nnotify_channels = []\n' + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 1
    assert "未知策略组" in capsys.readouterr().out


def test_push_lines_mark_strategy_plus_watchlist(wl_db) -> None:
    """推送正文：纯自选带「自选（备注）」，策略+自选带「+自选（备注）」。"""
    both = pool.format_pool_lines([{
        "name": "低价样本", "symbol": "600001", "strategies": "LowPriceStrategy",
        "source": "策略+自选", "note": "老朋友", "reason": "低价股",
    }])
    assert both == ["1. 低价样本（600001）LowPrice+自选（老朋友）｜低价股"]

    only_watch = pool.format_pool_lines([{
        "name": "冷门样本", "symbol": "600100", "strategies": "",
        "source": "自选", "note": "龙头", "reason": "自选（龙头）",
    }])
    assert only_watch == ["1. 冷门样本（600100）自选（龙头）｜自选（龙头）"]

    # 纯策略标的：格式与服务器版一致（不带任何自选字样）
    plain = pool.format_pool_lines([{
        "name": "半导体甲", "symbol": "600002", "strategies": "LowPriceStrategy",
        "source": "策略", "reason": "低价股",
    }])
    assert plain == ["1. 半导体甲（600002）LowPrice｜低价股"]
