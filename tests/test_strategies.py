"""5 条入选策略：条件与阈值必须与服务器版一致（逐条用构造数据验证）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import factors, rules


def _days(count: int = 40) -> list[str]:
    """40 个工作日（不需要真实日历）。"""
    end = datetime(2026, 9, 11).date()
    out: list[str] = []
    cursor = end
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _bars(symbol: str, closes: list[float], volumes: list[float], opens=None):
    """按收盘价/成交量序列造行情行。open 默认取当日收盘（需要 close>open 的形态时显式传）。"""
    rows = []
    for i, day in enumerate(_days(len(closes))):
        close = float(closes[i])
        volume = float(volumes[i])
        open_ = float(opens[i]) if opens else close
        rows.append((symbol, day, open_, max(open_, close), min(open_, close), close,
                     volume, volume * close))
    return rows


def _flat(value: float, count: int) -> list[float]:
    return [value] * count


@pytest.fixture()
def strategy_db(cfg) -> str:
    """构造一组"刚好各触发一条策略"的行情。

    每只股票针对一条策略设计（详见各断言里的注释），同一条策略的候选按因子排序。
    """
    storage.init_db(cfg.db_path)
    n = 40
    rows: list[tuple] = []

    # 1) 低价股：股价 3 元、流动性充足、没跌停 → 应该是低价股策略的第一名
    rows += _bars("600001", _flat(3.0, n), _flat(2e7, n))
    # 2) 仅作对照的高价股（同条件但贵）
    rows += _bars("600002", _flat(100.0, n), _flat(2e7, n))
    # 3) 短期反转：连续阴跌，20 日跌幅约 -21%
    rows += _bars("600003", [20.0 * (0.988 ** i) for i in range(n)], _flat(2e7, n))
    # 4) 对照：一路上涨的股票不该被反转策略选中
    rows += _bars("600004", [10.0 * (1.005 ** i) for i in range(n)], _flat(2e7, n))
    # 5) 地量后放量：**前 3 个交易日**都缩量到 5e5，最后一日 2e7 放量收阳涨 ~4%
    quiet_closes = _flat(25.0, n)
    quiet_volumes = _flat(1e7, n)
    for i in (n - 4, n - 3, n - 2):                # 最近 3 个"已完成"的交易日
        quiet_volumes[i] = 5e5
    quiet_closes[-1] = 25.0 * 0.99 * 1.05
    quiet_volumes[-1] = 2e7
    quiet_opens = list(quiet_closes)
    quiet_opens[-1] = 25.0 * 0.99 * 0.97           # 收阳
    rows += _bars("600005", quiet_closes, quiet_volumes, quiet_opens)
    # 6) 首板缩量整理：前日 +10%、再前日 +1%、今日 -1% 且缩量到 0.5 倍
    first_closes = _flat(10.0, n)
    first_closes[-2] = 11.11
    first_closes[-1] = 11.0
    first_volumes = _flat(5e6, n)
    first_volumes[-2] = 1e7
    first_volumes[-1] = 5e6
    rows += _bars("600006", first_closes, first_volumes)
    # 7) 连板回踩：前日涨停（涨停池里记 2 连板）、今日缩量收阴、不破 5 日线
    ladder_closes = _flat(10.0, n)
    ladder_closes[-2] = 11.0
    ladder_closes[-1] = 10.5
    ladder_volumes = _flat(5e6, n)
    ladder_volumes[-2] = 1e7
    ladder_volumes[-1] = 5e6
    rows += _bars("600007", ladder_closes, ladder_volumes)
    # 8) ST 股票：条件与低价股一样，但必须被排除
    rows += _bars("600008", _flat(3.0, n), _flat(2e7, n))

    names = {
        "600001": ("低价样本", "银行"),
        "600002": ("高价样本", "白酒"),
        "600003": ("反转样本", "半导体"),
        "600004": ("上涨样本", "半导体"),
        "600005": ("地量样本", "半导体"),
        "600006": ("首板样本", "半导体"),
        "600007": ("连板样本", "银行"),
        "600008": ("ST样本", "银行"),
    }
    days = _days(n)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [(s, v[0], v[1]) for s, v in names.items()])
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        # 连板回踩依赖涨停池的连板数（high_days）
        storage.write_limit_up_pool(conn, [
            (days[-2], "600007", "连板样本", 2, "二连板", "09:31:00", "09:40:00",
             8e7, 0, "银行", 5.0, 10.0, 1e9, 1, 0, "2连板", 9e7, 11.0, 0, "hithink", "t"),
        ])
    return str(cfg.db_path)


@pytest.fixture()
def strategy_engine(strategy_db: str) -> DataEngine:
    return DataEngine(strategy_db)


# ── 逐条策略 ──


def test_low_price_selects_cheapest_first_and_excludes_st(strategy_engine) -> None:
    """低价股：按收盘价升序；ST/退市股必须被排除（涨跌幅 5%、有退市风险）。"""
    selected = rules.LowPriceStrategy(strategy_engine).run()
    assert "600001" in selected
    assert selected[0] == "600001"            # 3 元 < 10 元 < …（最低价排第一）
    assert "600008" not in selected           # ST 样本


def test_low_price_params_match_server() -> None:
    """参数必须与服务器版一字不差（不要"顺手优化"）。"""
    strategy = rules.LowPriceStrategy
    assert strategy.top_n == 30
    assert strategy.min_price == 2.0
    assert strategy.min_turnover == 3e7
    assert strategy.limit_down == -0.095
    assert strategy.display_name == "低价股"


def test_reversal_selects_deep_drop(strategy_engine) -> None:
    """短期反转：20 日跌幅 ≥10% 的入选；上涨的不入选。"""
    selected = rules.ReversalStrategy(strategy_engine).run()
    assert "600003" in selected               # 20 日 -21%
    assert "600004" not in selected           # 上涨的股票不该反转


def test_reversal_params_match_server() -> None:
    strategy = rules.ReversalStrategy
    assert (strategy.window, strategy.top_n) == (20, 30)
    assert strategy.min_drop == -0.10
    assert strategy.min_turnover == 3e7
    assert strategy.limit_down == -0.095


def test_dryup_expansion_selects_quiet_then_surge(strategy_engine) -> None:
    """地量后放量：前 3 日缩量 + 今日放量收阳（0~9%）的入选。"""
    selected = rules.DryUpExpansionStrategy(strategy_engine).run()
    assert "600005" in selected


def test_dryup_expansion_params_match_server() -> None:
    strategy = rules.DryUpExpansionStrategy
    assert (strategy.quiet_ratio, strategy.surge_ratio, strategy.max_gain) == (0.7, 1.5, 0.09)
    assert (strategy.min_price, strategy.min_turnover) == (2.0, 3e7)


def test_first_limit_up_selects_shrink_after_first_board(strategy_engine) -> None:
    """首板缩量整理：昨日首板（前日没涨停）、今日缩量不破位的入选。"""
    selected = rules.FirstLimitUpStrategy(strategy_engine).run()
    assert "600006" in selected


def test_first_limit_up_excludes_second_board(strategy_engine, strategy_db) -> None:
    """前一日也涨停（二连板）不算"首板"，必须排除。"""
    with storage.connect(strategy_db) as conn:
        # 把 600006 的再前一日也改成涨停（+9.7%）→ 变成二连板形态
        conn.execute(
            "UPDATE stock_daily_raw SET close = close * 1.097 WHERE symbol = '600006' "
            "AND date = (SELECT date FROM (SELECT DISTINCT date FROM stock_daily_raw "
            "ORDER BY date DESC LIMIT 1 OFFSET 2))"
        )
        conn.commit()
    selected = rules.FirstLimitUpStrategy(DataEngine(strategy_db)).run()
    assert "600006" not in selected


def test_first_limit_up_params_match_server() -> None:
    strategy = rules.FirstLimitUpStrategy
    assert (strategy.limit_up, strategy.max_today_gain, strategy.floor_ratio) == (0.095, 0.09, 0.95)


def test_ladder_pullback_selects_after_two_boards(strategy_engine) -> None:
    """连板回踩：昨日连板 ≥2 + 今日缩量收阴 + 不破 5 日线。"""
    selected = rules.LadderPullbackStrategy(strategy_engine).run()
    assert "600007" in selected


def test_ladder_pullback_needs_limit_up_pool(strategy_engine, strategy_db) -> None:
    """没有涨停池数据时直接跳过（而不是错选一堆股票）。"""
    with storage.connect(strategy_db) as conn:
        conn.execute("DELETE FROM limit_up_pool")
        conn.commit()
    assert rules.LadderPullbackStrategy(DataEngine(strategy_db)).run() == []


def test_ladder_pullback_params_match_server() -> None:
    strategy = rules.LadderPullbackStrategy
    assert (strategy.min_days, strategy.max_volume_ratio, strategy.ma5_floor) == (2, 0.9, 0.98)
    assert (strategy.min_turnover, strategy.limit_down) == (3e7, -0.095)


# ── 统一入口 ──


def test_run_all_returns_expected_shape(strategy_engine) -> None:
    """输出 `{策略类名: [{"symbol","name","reason"}...]}`，与建池的输入格式一致。"""
    picks, errors = rules.run_all(strategy_engine)
    assert errors == []
    assert set(picks) == set(rules.STRATEGIES)
    for class_name, items in picks.items():
        assert isinstance(items, list)
        for pick in items:
            assert set(pick) >= {"symbol", "name", "reason"}
            assert pick["name"]                       # 名称来自本地库
            assert pick["reason"] == rules.STRATEGY_LABELS[class_name]


def test_run_all_honours_top_n(strategy_engine) -> None:
    picks, _ = rules.run_all(strategy_engine, top_n=1)
    for items in picks.values():
        assert len(items) <= 1


def test_run_all_survives_a_broken_strategy(strategy_engine, monkeypatch) -> None:
    """单条策略失败不影响其它策略（服务器版同款行为）。"""

    class Boom(rules.LowPriceStrategy):
        def run(self):  # noqa: D102
            raise RuntimeError("策略炸了")

    monkeypatch.setitem(rules.STRATEGIES, "LowPriceStrategy", Boom)
    picks, errors = rules.run_all(strategy_engine)
    assert "LowPriceStrategy" not in picks
    assert any("策略炸了" in e for e in errors)
    assert "ReversalStrategy" in picks          # 其它策略照常出结果


def test_save_signals_writes_rows(strategy_engine, strategy_db) -> None:
    picks, _ = rules.run_all(strategy_engine)
    written = rules.save_signals(strategy_engine, picks, day="2026-09-11")
    assert written > 0
    with storage.connect(strategy_db) as conn:
        rows = conn.execute(
            "SELECT signal_date, strategy, symbol, name FROM signal"
        ).fetchall()
    assert rows and all(r["signal_date"] == "2026-09-11" for r in rows)
    assert {r["strategy"] for r in rows} <= set(rules.STRATEGIES)


def test_strategy_label_falls_back_to_class_name() -> None:
    assert rules.strategy_label("LowPriceStrategy") == "低价股"
    assert rules.strategy_label("SomeUnknownStrategy") == "SomeUnknown"


# ── 因子层 ──


def test_load_panel_reads_hfq_view(strategy_db) -> None:
    """策略读的是**后复权视图**（与服务器版 stock_daily 数值等价）。"""
    with storage.connect(strategy_db) as conn:
        conn.execute("UPDATE stock_daily_raw SET factor = 2.0 WHERE symbol = '600001'")
        conn.commit()
    panel = factors.load_panel(strategy_db, ("close",), symbols=["600001"])
    assert set(panel["close"]) == {6.0}          # 3.0 × 2.0


def test_load_risk_symbols_detects_st_and_delisting(strategy_db) -> None:
    risk = factors.load_risk_symbols(strategy_db)
    assert "600008" in risk


def test_exclude_risk_removes_rows(strategy_db) -> None:
    import pandas as pd

    frame = pd.DataFrame({"symbol": ["600001", "600008"], "close": [3.0, 3.0]})
    out = factors.exclude_risk(frame, factors.load_risk_symbols(strategy_db))
    assert list(out["symbol"]) == ["600001"]


def test_momentum_and_top_n_by() -> None:
    import pandas as pd

    frame = pd.DataFrame({
        "symbol": ["A"] * 3 + ["B"] * 3,
        "date": ["2026-01-01", "2026-01-02", "2026-01-03"] * 2,
        "close": [10.0, 9.0, 8.0, 10.0, 11.0, 12.0],
    })
    mom = factors.momentum(frame, 2)
    assert mom.iloc[2] == pytest.approx(-0.2)     # A: 8/10-1
    assert mom.iloc[5] == pytest.approx(0.2)      # B: 12/10-1
    snapshot = frame[frame["date"] == "2026-01-03"].copy()
    snapshot["factor"] = [8.0, 12.0]
    assert factors.top_n_by(snapshot, "factor", 1, ascending=True) == ["A"]
    assert factors.top_n_by(snapshot, "factor", 1, ascending=False) == ["B"]


def test_latest_snapshot_filters_inactive_symbols() -> None:
    import pandas as pd

    frame = pd.DataFrame({
        "symbol": ["A", "A", "B", "C"],
        "date": ["2026-01-01", "2026-01-02", "2026-01-01", "2026-01-02"],
        "close": [1.0, 1.1, 2.0, 3.0],
    })
    out = factors.latest_snapshot(frame, ["A", "C"])
    assert sorted(out["symbol"]) == ["A", "C"]     # B 的最后一根K线停在隔日 → 排除
