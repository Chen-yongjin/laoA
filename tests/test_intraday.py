"""盘中规则 + 条件单参数：交易时段、6 条规则、去重、整手与止损止盈。"""

from __future__ import annotations

from datetime import datetime

import pytest

from laoa_trader import intraday
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from tests.conftest import FakeClient


# ── 交易时段 ──


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 29, False), (9, 30, True), (10, 0, True), (11, 30, True),
        (11, 31, False), (12, 0, False), (12, 59, False), (13, 0, True),
        (14, 59, True), (15, 0, True), (15, 1, False), (20, 0, False),
    ],
)
def test_in_session_boundaries(hour: int, minute: int, expected: bool) -> None:
    assert intraday.in_session(datetime(2026, 9, 11, hour, minute)) is expected


def test_is_trading_day_without_calendar_assumes_trading(db) -> None:
    """日历没同步时按"交易日"处理 —— 否则一个同步失败就整天不提醒。"""
    with storage.connect(db) as conn:
        conn.execute("DELETE FROM trading_calendar")
        conn.commit()
    assert intraday.is_trading_day(db, "2026-09-11") is True


def test_is_trading_day_uses_calendar(db) -> None:
    with storage.connect(db) as conn:
        storage.write_calendar(conn, ["2026-09-10", "2026-09-11"])
    assert intraday.is_trading_day(db, "2026-09-11") is True
    assert intraday.is_trading_day(db, "2026-09-12") is False   # 周末


# ── 条件单参数（移植自 trade_plan.py）──


def test_round_lot_floors_to_hundred(cfg) -> None:
    assert intraday.round_lot(1234) == 1200
    assert intraday.round_lot(99.9) == 0
    assert intraday.round_lot(100) == 100


def test_position_size_uses_capital_and_pct(cfg) -> None:
    """股数 = 资金 × 单票仓位 ÷ 价格，向下取整到整手。"""
    cfg.trade_capital, cfg.trade_position_pct = 100000.0, 0.2
    # 20000 / 3.42 = 5847.95 → 5800 股
    assert intraday.position_size(3.42, cfg=cfg) == 5800
    assert intraday.position_size(3.42, capital=100000, pct=0.2, cfg=cfg) == 5800


def test_position_size_zero_for_bad_price(cfg) -> None:
    assert intraday.position_size(0, cfg=cfg) == 0
    assert intraday.position_size(-1, cfg=cfg) == 0


def test_plan_buy_trigger_limit_stop_target_and_lot(cfg) -> None:
    """触发价 → 委托价（含滑点）→ 整手数量 → 止损/止盈，与服务器版公式一致。"""
    plan = intraday.plan_buy("600519", "贵州样本", 10.0, reason="低价股", cfg=cfg)
    assert plan["trigger"] == 10.0
    assert plan["limit_price"] == pytest.approx(10.05)      # 10 × (1+0.005)
    assert plan["stop_loss"] == pytest.approx(9.5)          # -5%
    assert plan["take_profit"] == pytest.approx(11.0)       # +10%
    assert plan["quantity"] == 2000                         # 100000×0.2 ÷ 10 = 2000 股
    assert plan["quantity"] % intraday.LOT == 0             # 必须是整手
    assert plan["warnings"] == []
    assert "【条件单｜买入】600519 贵州样本" in plan["text"]
    assert "触发：价格 ≥ 10.00" in plan["text"]
    assert "委托：限价 10.05 × 2000 股" in plan["text"]
    assert "依据：低价股" in plan["text"]


def test_plan_buy_warns_when_capital_cannot_afford_one_lot(cfg) -> None:
    """买不起一手必须**明确告警**（而不是给一张 0 股的条件单）。"""
    cfg.trade_capital, cfg.trade_position_pct = 10000.0, 0.2   # 单票预算 2000 元
    plan = intraday.plan_buy("600519", "贵州样本", 50.0, cfg=cfg)
    assert plan["quantity"] == 0
    assert any("资金不足" in w for w in plan["warnings"])
    assert "买不起 1 手" in plan["text"]


def test_plan_buy_rounds_price_to_two_decimals(cfg) -> None:
    plan = intraday.plan_buy("600001", "甲", 3.4567, cfg=cfg)
    assert plan["trigger"] == 3.46
    assert plan["limit_price"] == pytest.approx(round(3.46 * 1.005, 2))


def test_plan_buy_warns_when_already_held(cfg) -> None:
    plan = intraday.plan_buy("600001", "甲", 3.0, held=1000, cfg=cfg)
    assert plan["quantity"] == 0
    assert any("已持有 1000 股" in w for w in plan["warnings"])


def test_plan_buy_warns_when_positions_full(cfg) -> None:
    cfg.trade_max_positions = 5
    plan = intraday.plan_buy("600001", "甲", 3.0, positions=5, cfg=cfg)
    assert any("持仓已达上限 5 只" in w for w in plan["warnings"])
    assert plan["quantity"] > 0      # 只提示不加仓，不阻止算数量


def test_plan_sell_uses_real_cost(cfg) -> None:
    """卖出计划按**真实成本**算止损止盈（比用信号日收盘价准）。"""
    plan = intraday.plan_sell("600001", "甲", cost=10.0, price=10.4, qty=1000, cfg=cfg)
    assert plan["trigger"] == pytest.approx(9.5)          # 成本 -5%
    assert plan["take_profit"] == pytest.approx(11.0)     # 成本 +10%
    assert plan["limit_price"] == pytest.approx(round(9.5 * 0.995, 2))
    assert "浮动 +4.00%" in plan["text"]
    assert "× 1000 股" in plan["text"]


def test_plan_sell_without_cost_returns_warning(cfg) -> None:
    plan = intraday.plan_sell("600001", "甲", cost=0, cfg=cfg)
    assert plan["text"] == ""
    assert plan["warnings"] == ["缺少成本价，无法算止损止盈"]


def test_stop_loss_take_profit_come_from_config(cfg) -> None:
    cfg.stop_loss, cfg.take_profit = 0.08, 0.20
    buy = intraday.plan_buy("600001", "甲", 10.0, cfg=cfg)
    assert buy["stop_loss"] == pytest.approx(9.2)
    assert buy["take_profit"] == pytest.approx(12.0)
    hits = intraday.evaluate_sell_rules(
        "600001", {"last_price": 9.1}, {"prev_close": 10.0}, None, cfg=cfg
    )
    assert [h[0] for h in hits] == ["stop_loss"]


# ── 规则 ──


def test_evaluate_sell_rules_stop_loss_and_take_profit(cfg) -> None:
    ctx = {"prev_close": 10.0, "ma5": 9.0, "prev_prev_close": 9.0}
    stop = intraday.evaluate_sell_rules("600001", {"last_price": 9.4}, ctx, None, cfg=cfg)
    assert [h[0] for h in stop] == ["stop_loss"]
    take = intraday.evaluate_sell_rules("600001", {"last_price": 11.2}, ctx, None, cfg=cfg)
    assert [h[0] for h in take] == ["take_profit"]


def test_evaluate_sell_rules_uses_reference_close(cfg) -> None:
    """有池子/持仓参考价时优先用它（止损止盈对齐真实成本）。"""
    ctx = {"prev_close": 10.0, "ma5": 9.0}
    hits = intraday.evaluate_sell_rules(
        "600001", {"last_price": 9.0}, ctx, ref_close=10.0, cfg=cfg
    )
    assert [h[0] for h in hits] == ["stop_loss"]


def test_evaluate_sell_rules_break_ma5(cfg) -> None:
    """跌破 5 日线：只在"昨收还在 MA5 之上"时才算（否则是下跌途中的常态）。"""
    ctx = {"prev_close": 10.0, "ma5": 10.0}
    hits = intraday.evaluate_sell_rules("600001", {"last_price": 9.9}, ctx, None, cfg=cfg)
    assert "break_ma5" in [h[0] for h in hits]
    ctx_below = {"prev_close": 9.0, "ma5": 10.0}
    hits2 = intraday.evaluate_sell_rules("600001", {"last_price": 9.9}, ctx_below, None, cfg=cfg)
    assert "break_ma5" not in [h[0] for h in hits2]


def test_evaluate_sell_rules_limit_up_open(cfg) -> None:
    """昨日涨停、今日触及涨停价后打开。"""
    ctx = {"prev_close": 11.0, "prev_prev_close": 10.0, "ma5": 10.0}
    snap = {"last_price": 11.5, "high_price": 12.1}   # 11×1.095 = 12.045 触及后回落
    hits = intraday.evaluate_sell_rules("600001", snap, ctx, None, cfg=cfg)
    assert "limit_up_open" in [h[0] for h in hits]


def test_evaluate_buy_rules_breakout(cfg) -> None:
    ctx = {"high20": 10.0, "avg_turnover20": 1e8}
    snap = {"last_price": 10.4, "price_change_ratio_pct": 4.0, "turnover": 6e7}
    hits = intraday.evaluate_buy_rules("600001", snap, ctx)
    assert [h[0] for h in hits] == ["break_high"]


def test_evaluate_buy_rules_rejects_limit_up_chase(cfg) -> None:
    """涨幅超过 9%（接近涨停）不追 —— 避免在一字板上成交不了。"""
    ctx = {"high20": 10.0, "avg_turnover20": 1e8}
    snap = {"last_price": 11.0, "price_change_ratio_pct": 9.8, "turnover": 6e7}
    assert intraday.evaluate_buy_rules("600001", snap, ctx) == []


def test_evaluate_buy_rules_requires_volume(cfg) -> None:
    """成交额不足日均 0.5 倍不算"放量"。"""
    ctx = {"high20": 10.0, "avg_turnover20": 1e8}
    snap = {"last_price": 10.4, "price_change_ratio_pct": 4.0, "turnover": 1e7}
    assert intraday.evaluate_buy_rules("600001", snap, ctx) == []


def test_evaluate_pool_buy_rules_pullback(cfg) -> None:
    """池内回踩买点：现价贴近 MA5（±1.5%）且高于昨收。"""
    ctx = {"ma5": 10.0, "prev_close": 9.9}
    hits = intraday.evaluate_pool_buy_rules("600001", {"last_price": 10.1}, ctx)
    assert [h[0] for h in hits] == ["pullback_ma5_buy"]
    far = intraday.evaluate_pool_buy_rules("600001", {"last_price": 11.0}, ctx)
    assert far == []


def test_evaluate_first_board_filters_thin_seal(cfg) -> None:
    """首板 + 封单 ≥5000 万才算（封单太薄的容易被砸开）。"""
    client = FakeClient(limit_up=[
        {"thscode": "600001.SH", "name": "厚封单", "continue_day_cnt": 1,
         "seal_money": 8e7, "last_price": 10.0, "limit_up_reason": "芯片"},
        {"thscode": "600002.SH", "name": "薄封单", "continue_day_cnt": 1,
         "seal_money": 1e7, "last_price": 10.0},
        {"thscode": "600003.SH", "name": "二连板", "continue_day_cnt": 2,
         "seal_money": 9e7, "last_price": 10.0},
    ])
    hits = intraday.evaluate_first_board(client)
    assert len(hits) == 1
    assert hits[0][0] == "first_board"
    assert "厚封单" in hits[0][2]
    assert "封单 0.80 亿" in hits[0][2]
    assert "芯片" in hits[0][2]


def test_evaluate_first_board_swallows_api_error(cfg) -> None:
    from laoa_trader.data import hithink as hx

    assert intraday.evaluate_first_board(
        FakeClient(fail_with=hx.HithinkError(5001, "模拟接口错误（测试假客户端）"))
    ) == []


# ── 历史上下文 ──


def test_history_context_values(db) -> None:
    from laoa_trader import pool

    pool.save_pool(db, [{"symbol": "600002", "name": "半导体甲", "score": 1.0,
                         "strategy": "LowPriceStrategy"}], "2026-09-11")
    ctx = intraday.history_context(db, ["600002"])
    assert "600002" in ctx
    entry = ctx["600002"]
    assert entry["ma5"] and entry["ma20"] and entry["high20"]
    assert entry["ma5"] > entry["ma20"]           # 该股票是缓慢上涨的
    assert entry["avg_turnover20"]
    assert entry["prev_close"] > 0


def test_history_context_skips_symbols_without_enough_history(db) -> None:
    with storage.connect(db) as conn:
        storage.write_daily_raw(conn, [("999999", "2026-09-11", 1, 1, 1, 1, 1, 1, 1.0)])
    ctx = intraday.history_context(db, ["999999"])
    assert ctx == {}       # 少于 5 根 K 线不参与


# ── 观察池 ──


def test_watch_targets_prefers_pool(db) -> None:
    from laoa_trader import pool

    pool.save_pool(db, [{"symbol": "600002", "name": "半导体甲", "score": 1.0,
                         "strategy": "LowPriceStrategy"}], "2026-09-11")
    targets, symbols = intraday.watch_targets(db)
    assert set(targets) == {"600002"}
    assert symbols == {"600002"}
    assert targets["600002"]["source"] == "pool"


def test_watch_targets_falls_back_to_recent_signals(db, monkeypatch) -> None:
    """池子为空时退回"近期信号"，避免盘中无事可盯。"""
    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    with storage.connect(db) as conn:
        storage.write_signals(conn, [("2026-09-11", "LowPriceStrategy", "600003",
                                      "半导体乙", 18.0, None, "低价股")])
    targets, symbols = intraday.watch_targets(db)
    assert set(targets) == {"600003"}
    assert symbols == set()
    assert targets["600003"]["source"] == "signal"


# ── 一轮执行（含去重与推送）──


def test_run_once_records_and_dedupes(db, cfg, monkeypatch) -> None:
    """跑一轮：命中的提醒落去重表；同一轮再跑不再重复推送。"""
    import laoa_trader.pool as pool_mod

    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    pool_mod.save_pool(db, [{"symbol": "600002", "name": "半导体甲", "score": 1.0,
                             "strategy": "LowPriceStrategy", "date": "2026-09-11"}],
                       "2026-09-11")
    with storage.connect(db) as conn:
        storage.write_calendar(conn, [datetime.now().strftime("%Y-%m-%d")])

    engine = DataEngine(db)
    client = FakeClient(snapshots=[{
        "ticker": "600002", "thscode": "600002.SH", "last_price": 1.0,   # 远低于昨收 → 止损
        "price_change_ratio_pct": -90.0, "turnover": 1e9,
    }])
    sent: list[tuple[str, list[str]]] = []

    first = intraday.run_once(
        engine, cfg, ignore_session=True, client=client,
        notifier=lambda title, lines: sent.append((title, lines)),
    )
    assert first["hits"] >= 1
    assert first["fresh"] >= 1
    assert first["pushed"] is True
    assert sent and "盘中提醒" in sent[0][0]
    # 提醒文本里带上可抄的条件单参数
    assert any("条件单" in line for line in sent[0][1])

    second = intraday.run_once(
        engine, cfg, ignore_session=True, client=client,
        notifier=lambda title, lines: sent.append((title, lines)),
    )
    assert second["hits"] >= 1
    assert second["fresh"] == 0        # 去重生效
    assert second["pushed"] is False
    assert len(sent) == 1


def test_run_once_outside_session_is_noop(db, cfg) -> None:
    engine = DataEngine(db)
    result = intraday.run_once(engine, cfg, client=FakeClient(), ignore_session=False)
    # 只有在非交易时段才会跳过；若测试恰好在交易时段运行，则不应断言
    if not intraday.in_session():
        assert result["hits"] == 0 and result["pushed"] is False


def test_run_once_dry_run_does_not_notify(db, cfg, monkeypatch) -> None:
    import laoa_trader.pool as pool_mod

    pool_mod.save_pool(db, [{"symbol": "600002", "name": "半导体甲", "score": 1.0,
                             "strategy": "LowPriceStrategy"}], "2026-09-11")
    with storage.connect(db) as conn:
        storage.write_calendar(conn, [datetime.now().strftime("%Y-%m-%d")])
    client = FakeClient(snapshots=[{
        "ticker": "600002", "thscode": "600002.SH", "last_price": 1.0,
        "price_change_ratio_pct": -90.0, "turnover": 1e9,
    }])
    sent = []
    result = intraday.run_once(
        DataEngine(db), cfg, dry_run=True, ignore_session=True, client=client,
        notifier=lambda t, lines: sent.append(t),
    )
    assert result["pushed"] is False
    assert sent == []


def test_run_once_without_key_reports_error(db, cfg, monkeypatch) -> None:
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    cfg.hithink_api_key = ""
    with storage.connect(db) as conn:
        storage.write_calendar(conn, [datetime.now().strftime("%Y-%m-%d")])
    result = intraday.run_once(DataEngine(db), cfg, ignore_session=True)
    assert result["error"]
    assert result["pushed"] is False


def test_run_once_api_failure_does_not_raise(db, cfg) -> None:
    """单轮异常只记结果，不抛给调用方（盘中断一轮不该让服务退出）。"""
    from laoa_trader.data import hithink as hx

    with storage.connect(db) as conn:
        storage.write_calendar(conn, [datetime.now().strftime("%Y-%m-%d")])
    result = intraday.run_once(
        DataEngine(db), cfg, ignore_session=True,
        client=FakeClient(fail_with=hx.HithinkError(5001, "模拟接口错误（测试假客户端）")),
    )
    assert result["pushed"] is False


def test_format_message_uses_position_cost(db, cfg) -> None:
    """有持仓时用真实成本算止损止盈（卖出计划而不是买入计划）。"""
    with storage.connect(db) as conn:
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=1000,
                                avg_cost=20.0)
    title, lines = intraday.format_message(
        [{"symbol": "600002", "name": "半导体甲", "kind": "stop_loss",
          "price": 18.0, "detail": "现价 18.00 ≤ 参考价 19.00"}],
        db_path=db, cfg=cfg,
    )
    body = "\n".join(lines)
    assert "🛑 触及止损" in body
    assert "【条件单｜卖出】" in body
    assert "19.00" in body          # 20 × 0.95


def test_alert_rows_adds_labels(db, cfg) -> None:
    with storage.connect(db) as conn:
        storage.record_alerts(
            conn,
            [{"symbol": "600002", "kind": "break_high", "price": 12.0, "detail": "突破"}],
            "2026-09-11",
        )
    rows = intraday.alert_rows(db)
    assert rows[0]["label"] == "🚀 放量突破20日高"
