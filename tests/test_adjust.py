"""复权计算：公式正确性、逐股累乘、后复权价、增量与全量一致。

这是整个数据层最关键的一组测试 —— 复权算错，所有因子和策略信号都是错的。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from laoa_trader.data import storage, sync


# ── 单事件公式 ──


def test_adjust_ratio_pure_dividend() -> None:
    """纯现金分红：k = 前收 / (前收 − 分红)。

    例：前收 10 元、每股分红 0.5 元 → k = 10 / 9.5 ≈ 1.05263。
    也就是"除权后价格 ×k"能还原到除权前的价格水平。
    """
    k = sync.adjust_ratio(prev_close=10.0, dividend=0.5)
    assert k == pytest.approx(10.0 / 9.5)


def test_adjust_ratio_bonus_shares() -> None:
    """10 送 10（每股送 1 股）：k = 前收 ×(1+1) / 前收 = 2。"""
    assert sync.adjust_ratio(prev_close=10.0, bonus=1.0) == pytest.approx(2.0)


def test_adjust_ratio_allotment() -> None:
    """配股：k = 前收 ×(1+配股比例) / (前收 + 配股比例×配股价)。

    前收 10、10 配 2（比例 0.2）、配股价 5 → k = 10×1.2 / (10+1) = 1.0909…
    """
    k = sync.adjust_ratio(prev_close=10.0, allotment_ratio=0.2, allotment_price=5.0)
    assert k == pytest.approx(12.0 / 11.0)


def test_adjust_ratio_combined_matches_server_formula() -> None:
    """组合情形，与服务器版 dump_import 的公式逐字对照：

        k = 前收 × (1 + 送股 + 配股) / (前收 − 现金分红 + 配股×配股价)
    """
    prev, dividend, bonus, ratio, price = 10.0, 0.3, 0.4, 0.2, 4.0
    expected = prev * (1 + bonus + ratio) / (prev - dividend + ratio * price)
    assert sync.adjust_ratio(prev, dividend, bonus, ratio, price) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("prev", "dividend"),
    [
        (10.0, 10.0),    # 分母 = 0
        (10.0, 20.0),    # 分母 < 0（数据异常，必须丢弃而不是算出负因子）
        (0.0, 1.0),      # 前收缺失
        (-1.0, 0.0),
    ],
)
def test_adjust_ratio_discards_invalid_events(prev: float, dividend: float) -> None:
    assert sync.adjust_ratio(prev, dividend) is None


# ── 逐股累乘 ──


def _raw_frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """构造 load_raw() 形状的行情表（symbol,date,open,high,low,close,volume,turnover）。"""
    data = []
    for symbol, date, close in rows:
        data.append({
            "symbol": symbol, "date": date, "open": close, "high": close,
            "low": close, "close": close, "volume": 1000.0, "turnover": close * 1000,
        })
    return pd.DataFrame(data)


def test_cumulative_factor_multiplies_per_symbol() -> None:
    """因子必须**逐股**累乘：A 的除权事件不能影响 B。"""
    raw = _raw_frame([
        ("600001", "2026-01-01", 10.0),
        ("600001", "2026-01-02", 10.0),
        ("600001", "2026-01-03", 10.0),
        ("600002", "2026-01-01", 20.0),
        ("600002", "2026-01-02", 20.0),
        ("600002", "2026-01-03", 20.0),
    ])
    factors = pd.DataFrame([
        ("600001", "2026-01-02", 2.0),   # 600001 在 01-02 除权，k=2
        ("600002", "2026-01-03", 1.5),   # 600002 在 01-03 除权，k=1.5
    ], columns=["symbol", "ex_date", "k"])

    out = sync.cumulative_factor(raw, factors).sort_values(["symbol", "date"])
    got = {
        (r.symbol, r.date): r.factor
        for r in out.itertuples(index=False)
    }
    assert got[("600001", "2026-01-01")] == pytest.approx(1.0)   # 除权前
    assert got[("600001", "2026-01-02")] == pytest.approx(2.0)   # 除权当日就生效
    assert got[("600001", "2026-01-03")] == pytest.approx(2.0)
    assert got[("600002", "2026-01-02")] == pytest.approx(1.0)
    assert got[("600002", "2026-01-03")] == pytest.approx(1.5)


def test_cumulative_factor_chains_multiple_events() -> None:
    """两次除权要连乘：k1×k2。"""
    raw = _raw_frame([
        ("600001", "2026-01-01", 10.0),
        ("600001", "2026-01-02", 10.0),
        ("600001", "2026-01-03", 10.0),
    ])
    factors = pd.DataFrame([
        ("600001", "2026-01-02", 2.0),
        ("600001", "2026-01-03", 1.5),
    ], columns=["symbol", "ex_date", "k"])
    out = sync.cumulative_factor(raw, factors).set_index("date")["factor"]
    assert out["2026-01-01"] == pytest.approx(1.0)
    assert out["2026-01-02"] == pytest.approx(2.0)
    assert out["2026-01-03"] == pytest.approx(3.0)


def test_cumulative_factor_sorts_unsorted_input() -> None:
    """dump 不保证按日期有序 —— 计算前必须排序，否则 searchsorted 全错。"""
    raw = _raw_frame([
        ("600001", "2026-01-03", 10.0),
        ("600001", "2026-01-01", 10.0),
        ("600001", "2026-01-02", 10.0),
    ])
    factors = pd.DataFrame([("600001", "2026-01-02", 2.0)],
                           columns=["symbol", "ex_date", "k"])
    out = sync.cumulative_factor(raw, factors).set_index("date")["factor"]
    assert out["2026-01-01"] == pytest.approx(1.0)
    assert out["2026-01-02"] == pytest.approx(2.0)
    assert out["2026-01-03"] == pytest.approx(2.0)


def test_apply_factors_produces_backward_adjusted_prices() -> None:
    """后复权价 = 原始价 × 因子；**最早的价格保持真实**（后复权的定义）。"""
    raw = _raw_frame([
        ("600001", "2026-01-01", 10.0),
        ("600001", "2026-01-02", 9.5),
        ("600001", "2026-01-03", 9.6),
    ])
    factors = pd.DataFrame([("600001", "2026-01-02", 10.0 / 9.5)],
                           columns=["symbol", "ex_date", "k"])
    out = sync.apply_factors(raw, factors).sort_values("date")
    assert out.iloc[0]["close"] == pytest.approx(10.0)
    # 除权日：9.5 × (10/9.5) = 10 —— 序列连续，不出现断崖
    assert out.iloc[1]["close"] == pytest.approx(10.0)
    assert out.iloc[2]["close"] == pytest.approx(9.6 * 10.0 / 9.5)


def test_compute_adjust_factors_uses_previous_trading_day_close() -> None:
    """前收取自"除权日之前最后一个交易日"（含停牌跳日的情形）。"""
    raw = _raw_frame([
        ("600001", "2026-01-01", 8.0),
        ("600001", "2026-01-02", 9.0),
        # 01-03 停牌，无 K 线
        ("600001", "2026-01-06", 9.0),
    ])
    events = pd.DataFrame(
        [("600001", "2026-01-06", 0.5, 0.0, 0.0, 0.0)],
        columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"],
    )
    factors = sync.compute_adjust_factors(raw, events)
    assert len(factors) == 1
    # 前收 = 01-02 的 9.0（不是 01-06 自己），k = 9 / 8.5
    assert factors.iloc[0]["k"] == pytest.approx(9.0 / 8.5)


def test_compute_adjust_factors_skips_unknown_symbols() -> None:
    """有事件、没行情（退市/久远）：跳过而不是报错。"""
    raw = _raw_frame([("600001", "2026-01-01", 10.0)])
    events = pd.DataFrame(
        [("999999", "2026-01-01", 0.5, 0.0, 0.0, 0.0)],
        columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"],
    )
    assert len(sync.compute_adjust_factors(raw, events)) == 0


def test_compute_adjust_factors_empty_events() -> None:
    raw = _raw_frame([("600001", "2026-01-01", 10.0)])
    out = sync.compute_adjust_factors(raw, pd.DataFrame(
        columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"]))
    assert list(out.columns) == ["symbol", "ex_date", "k"]
    assert len(out) == 0


# ── 增量因子与全量重算必须一致 ──


def test_incremental_factors_match_full_recompute(tmp_path, cfg) -> None:
    """日更的增量因子**必须**等于把全历史重算一遍的结果。

    这是增量算法的正确性判据：日更只拿最近 10 个交易日，靠库里已累计的
    factor 作为基数继续累乘；一旦基数或事件窗口处理错了，因子就会漂移，
    而漂移会静默污染所有策略信号（价格序列不连续）。
    """
    import pandas as pd

    days = [f"2026-01-{d:02d}" for d in range(1, 11)]
    symbol = "600001"

    # 全量：1~10 日的原始价 + 第 6 日一条分红事件
    full_rows = [(symbol, d, 10.0 + i * 0.1) for i, d in enumerate(days)]
    full_raw = _raw_frame(full_rows)
    events = pd.DataFrame(
        [(symbol, days[5], 0.5, 0.0, 0.0, 0.0)],
        columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                 "allotment_ratio", "allotment_price"],
    )
    full = sync.cumulative_factor(full_raw, sync.compute_adjust_factors(full_raw, events))
    full_factors = dict(zip(full["date"], full["factor"], strict=False))

    # 增量：库里先有 1~5 日（不带事件），再补 6~10 日
    path = storage.init_db(cfg.db_path)
    with storage.connect(path) as conn:
        storage.write_daily_raw(conn, [
            (symbol, days[i], 10.0 + i * 0.1, 10.0 + i * 0.1, 10.0 + i * 0.1,
             10.0 + i * 0.1, 1000.0, 1000.0, 1.0)
            for i in range(5)
        ])
        new_raw = full_raw[full_raw["date"].isin(days[5:])].reset_index(drop=True)
        adjusted, stale = sync._incremental_factors(conn, new_raw, events)

    increment = dict(zip(adjusted["date"], adjusted["factor"], strict=False))
    assert stale == 0
    # 1~5 日已在库里，增量只算 6~10 日；每一天的因子都必须与全量一致
    assert sorted(increment) == days[5:]
    for day in days[5:]:
        assert increment[day] == pytest.approx(full_factors[day], rel=1e-12), f"{day} 因子漂移"
    # 具体值：除权日当天因子就生效（k），之后保持不变
    prev_close = 10.0 + 4 * 0.1
    k = prev_close / (prev_close - 0.5)
    assert increment[days[5]] == pytest.approx(k)
    assert increment[days[6]] == pytest.approx(k)


def test_incremental_factors_without_events_keeps_base(tmp_path, cfg) -> None:
    """没有新除权事件时，新行沿用库里的基数（不能重置成 1.0）。"""
    days = [f"2026-02-{d:02d}" for d in range(1, 6)]
    symbol = "600001"
    path = storage.init_db(cfg.db_path)
    with storage.connect(path) as conn:
        # 库里已有数据，且历史累积因子是 2.0（说明过去发生过一次 10 送 10）
        storage.write_daily_raw(conn, [
            (symbol, days[0], 5.0, 5.0, 5.0, 5.0, 1000.0, 1000.0, 2.0),
        ])
        new_raw = _raw_frame([(symbol, days[1], 5.1), (symbol, days[2], 5.2)])
        adjusted, _stale = sync._incremental_factors(conn, new_raw, None)
    assert set(adjusted["factor"]) == {2.0}


def test_incremental_factors_reports_missed_events(tmp_path, cfg) -> None:
    """除权日落在"数据缺口"里（库内最后一天之后、本次窗口之前）要被统计出来。

    典型场景：日更中断几天后补数据，中间那天发生了一次分红 —— 基数没算它、
    新行也承载不了，必须提示用户重跑全量，否则那一段后复权价会不连续。
    """
    import pandas as pd

    path = storage.init_db(cfg.db_path)
    with storage.connect(path) as conn:
        storage.write_daily_raw(conn, [
            ("600001", "2026-03-10", 5.0, 5.0, 5.0, 5.0, 1.0, 1.0, 1.0),
        ])
        events = pd.DataFrame(
            [("600001", "2026-03-12", 0.1, 0.0, 0.0, 0.0)],   # 缺口里的除权日
            columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                     "allotment_ratio", "allotment_price"],
        )
        new_raw = _raw_frame([("600001", "2026-03-16", 5.1)])
        _adjusted, missed = sync._incremental_factors(conn, new_raw, events)
    assert missed == 1


def test_incremental_factors_ignores_already_applied_events(tmp_path, cfg) -> None:
    """早已算进基数的历史事件**不能**被反复报成问题（否则每天都会误报警告）。"""
    import pandas as pd

    path = storage.init_db(cfg.db_path)
    with storage.connect(path) as conn:
        storage.write_daily_raw(conn, [
            ("600001", "2026-03-10", 5.0, 5.0, 5.0, 5.0, 1.0, 1.0, 2.0),
        ])
        events = pd.DataFrame(
            [("600001", "2026-01-05", 0.1, 0.0, 0.0, 0.0)],
            columns=["symbol", "ex_date", "dividend_per_share", "per_share_bonus",
                     "allotment_ratio", "allotment_price"],
        )
        new_raw = _raw_frame([("600001", "2026-03-11", 5.1)])
        adjusted, missed = sync._incremental_factors(conn, new_raw, events)
    assert missed == 0
    assert adjusted["factor"].iloc[0] == pytest.approx(2.0)


def test_adjust_ratio_no_float_noise_for_zero_event() -> None:
    """没有任何分红送配的事件：k 恰好是 1（不能有浮点漂移）。"""
    k = sync.adjust_ratio(prev_close=3.33)
    assert k is not None and math.isclose(k, 1.0, rel_tol=0, abs_tol=0)
