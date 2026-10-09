"""策略成绩单（回测）的离线测试：口径正确性 / T+1 合法性 / 一字板剔除 / 样本不足。

为什么这些用例必须存在
----------------------
成绩单的价值全在**口径**上：一个"算得出来、还挺好看"的非法口径比没有结论更危险。
所以这里不是测"函数跑得通"，而是逐条钉死：

1. **口径正确性**：5~6 天的小库 + 事先手算好的收益 → 断言 α、胜率、t 值、by_year
   全都能用纸笔复核（基准用固定值注入，避免"用被测代码验证被测代码"）；
2. **T+1 合法性**：`horizon=0`（D+1 开盘买、D+1 收盘卖）必须被拒绝，
   并且合法持有期的**卖出日必须严格晚于买入日**；
3. **一字板剔除**：买入日开盘 +10% 的样本必须被剔除、计数，且不参与任何统计；
4. **样本不足**：空库 / 单日库 / 太少样本 → 返回"样本不足 + 原因"，不崩、不瞎给结论；
5. **α 定义**：全市场普涨、被选中的票"跟着涨"时 α ≈ 0（否则就是把行情当成能力）。

全部离线：合成 SQLite 小库，不联网、不需要 API Key。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from laoa_trader.data import storage
from laoa_trader.research import scorecard as sc


# ── 造库工具 ──


def workdays(start: str, count: int) -> list[str]:
    """从 start 起的 count 个工作日（跳过周末），升序。"""
    day = date.fromisoformat(start)
    out: list[str] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def build_db(path: Path, days: list[str], series: dict[str, dict]) -> str:
    """写一个最小的行情库。

    Args:
        path: 库路径。
        days: 交易日（升序）。
        series: {代码: {"open": [...], "close": [...], "volume": [...], "turnover": [...]}}；
            `volume`/`turnover` 缺省给足量的默认值（过得了流动性门槛，又不影响收益计算）。

    Returns:
        库路径字符串。
    """
    storage.init_db(path)
    rows = []
    with storage.connect(path) as conn:
        storage.write_stock_basic(conn, [(symbol, f"样本{symbol}", "测试行业")
                                         for symbol in series])
        for symbol, item in series.items():
            opens = list(item["open"])
            closes = list(item["close"])
            volumes = list(item.get("volume") or [1e6] * len(days))
            turnovers = list(item.get("turnover") or [1e8] * len(days))
            for index, day in enumerate(days):
                high = max(opens[index], closes[index]) * 1.001
                low = min(opens[index], closes[index]) * 0.999
                rows.append((symbol, day, opens[index], high, low, closes[index],
                             volumes[index], turnovers[index]))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
    return str(path)


@pytest.fixture()
def six_day_db(tmp_path: Path) -> tuple[str, list[str]]:
    """6 个交易日 + 6 只票的库（后续用例各自覆盖价格路径）。"""
    days = workdays("2023-12-25", 6)
    series = {f"60000{i}": {"open": [10.0] * 6, "close": [10.0] * 6} for i in range(1, 7)}
    return build_db(tmp_path / "score.db", days, series), days


def fixed_signal(*symbols: str):
    """每天都选同一批票的信号函数（用于精确控制样本）。"""

    def signal(_day_panel: pd.DataFrame) -> list[str]:
        return list(symbols)

    return signal


# ── 1) T+1 合法性 ──


def test_horizon_zero_is_rejected_as_unexecutable():
    """T+0（D+1 开盘买、D+1 收盘卖）在 A 股不可执行，必须直接拒绝。"""
    for illegal in (0, -1, -5):
        with pytest.raises(sc.IllegalHorizonError) as excinfo:
            sc.check_horizon(illegal)
        message = str(excinfo.value)
        assert "不可执行" in message
        assert "T+1" in message          # 说明里要讲清"为什么"，用户才知道口径

    # DEFAULT_HORIZONS 本身也不许含 0（隔日超短是合法的最短档）
    assert 0 not in sc.DEFAULT_HORIZONS
    assert min(sc.DEFAULT_HORIZONS) == 1


def test_evaluate_rejects_illegal_horizon(six_day_db):
    """跑成绩单时给 0 也要炸 —— 不能"返回一行空结果"了事。"""
    db, _days = six_day_db
    with pytest.raises(sc.IllegalHorizonError):
        sc.evaluate(db, fixed_signal("600001"), horizons=(0,), burn_in=0)


def test_exit_is_strictly_after_entry(six_day_db):
    """持有 N 日 = 买入日 + N 个交易日；卖出日必须**严格晚于**买入日。"""
    db, days = six_day_db
    panel = sc.load_panel(db)
    picks = sc.build_picks(panel, sc.spec_from_signal("s", "s", fixed_signal("600001")),
                           top_n=5)
    # 基准的键是 (信号日, 口径 key)；旧口径 horizons=(1,3) 会被翻成 T+1 / T+3
    market = {(days[0], "T+1"): 0.0, (days[0], "T+3"): 0.0}
    outcomes = sc.compute_outcomes(panel, picks[picks["date"] == days[0]], market, (1, 3))
    record = outcomes["signals"][0]
    assert record["T+1"]["entry_date"] == days[1]        # D+1 开盘买
    assert record["T+1"]["exit_date"] == days[2]         # 持仓 1 日 → D+2 收盘卖
    assert record["T+3"]["exit_date"] == days[4]         # 持仓 3 日 → D+4 收盘卖
    for key, item in record.items():
        if key in ("signal_date", "symbol"):
            continue
        assert item["exit_date"] > item["entry_date"], f"{key} 当天买当天卖 = 非法"
    assert days.index(record["T+1"]["exit_date"]) - days.index(record["T+1"]["entry_date"]) == 1
    assert days.index(record["T+3"]["exit_date"]) - days.index(record["T+3"]["entry_date"]) == 3


# ── 2) 口径正确性：手算 α / 胜率 / t 值 / by_year ──


def test_alpha_win_rate_t_stat_and_by_year_are_hand_computable(tmp_path: Path):
    """每条信号日的窗口收益是 +2% / -1% / +3% / 0%，基准固定 +0.5%。

    手算：
        α = [1.5%, -1.5%, 2.5%, -0.5%] → 平均 +0.5%，胜率 2/4 = 50%
        日度均值 0.5%，样本标准差 1.8257% → t = 0.5 / (1.8257/√4) = 0.5477
        by_year: 2023 均值 0.0%（n=2）、2024 均值 +1.0%（n=2）
    """
    days = workdays("2023-12-28", 6)          # 12-28/29(2023) + 01-01~04(2024)
    # 每只票每天的开盘价都固定 10；只需要把"出场日收盘价"摆成想要的收益
    opens = [10.0] * 6
    closes = [10.0] * 6
    closes[2] = 10.2      # 信号日 day0 → 买入 day1 开盘 10 → 出场 day2 收盘 10.2 = +2%
    closes[3] = 9.9       # 信号日 day1 → 买入 day2 开盘 10 → 出场 day3 收盘 9.9 = -1%
    closes[4] = 10.3      # 信号日 day2 → 买入 day3 开盘 10 → 出场 day4 收盘 10.3 = +3%
    closes[5] = 10.0      # 信号日 day3 → 买入 day4 开盘 10 → 出场 day5 收盘 10.0 = 0%
    # 注意 day1 的收盘价也是"信号日 day1 的收盘价"，用作一字板判断，保持 10 即可
    db = build_db(tmp_path / "hand.db", days,
                  {"600001": {"open": opens, "close": closes},
                   "600002": {"open": [10.0] * 6, "close": [10.0] * 6}})

    market = {(day, "T+1"): 0.005 for day in days[:4]}   # 同期等权全市场 +0.5%（固定注入）
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1,), burn_in=0,
                         market=market, min_samples=1, min_market_symbols=1)
    row = result["rows"][0]

    assert row["n"] == 4
    assert row["days"] == 4
    assert row["avg_alpha_pct"] == pytest.approx(0.5, abs=1e-6)
    assert row["win_rate_pct"] == pytest.approx(50.0, abs=1e-6)
    assert row["t_stat"] == pytest.approx(0.5477, abs=1e-3)
    assert row["dropped_limit_up"] == 0
    assert row["start"] == days[0] and row["end"] == days[3]
    # 逐年（跨年）：2023 两天均值为 0，2024 两天均值为 +1%
    assert set(row["by_year"]) == {"2023", "2024"}
    assert row["by_year"]["2023"] == {"n": 2, "avg_alpha_pct": 0.0, "win_rate_pct": 50.0}
    assert row["by_year"]["2024"] == {"n": 2, "avg_alpha_pct": 1.0, "win_rate_pct": 50.0}
    # 绝对收益与同期市场也一并给出（用来判断"是不是只是行情好"）
    assert row["avg_ret_pct"] == pytest.approx(1.0, abs=1e-6)
    assert row["avg_market_pct"] == pytest.approx(0.5, abs=1e-6)


def test_t_stat_aggregates_by_signal_day_not_by_trade(tmp_path: Path):
    """同一天选 5 只票不算 5 个独立样本。

    第 1 个信号日 5 只票都 +1%，第 2 个信号日 5 只票都 -0.5%（基准 0）：
        按信号日聚合（正确）：日度均值 [1%, -0.5%] → t = 0.25/(1.0607/√2) = 0.3333
        按笔数聚合（错误、会虚高）：10 条 → t = 1.0
    本用例把两种算法钉开，谁把 t 改成按笔数算，这里立刻红。
    """
    days = workdays("2024-01-02", 4)
    symbols = [f"60000{i}" for i in range(1, 6)]
    series = {symbol: {"open": [10.0, 10.0, 10.0, 10.0],
                       "close": [10.0, 10.0, 10.1, 9.95]} for symbol in symbols}
    db = build_db(tmp_path / "daily.db", days, series)

    market = {(days[0], "T+1"): 0.0, (days[1], "T+1"): 0.0}
    result = sc.evaluate(db, fixed_signal(*symbols), horizons=(1,), burn_in=0,
                         market=market, min_samples=1, min_market_symbols=1)
    row = result["rows"][0]

    assert row["n"] == 10            # 10 笔
    assert row["days"] == 2          # 但只有 2 个信号日
    assert row["t_stat"] == pytest.approx(0.3333, abs=1e-3)
    assert row["t_stat"] != pytest.approx(1.0, abs=1e-2)   # 按笔数算的那个（错的）值


# ── 3) 一字板剔除 ──


def test_limit_up_open_is_dropped_and_excluded(tmp_path: Path):
    """买入日开盘 +10% 买不进：要计数、且**不参与统计**。

    做法：让被剔除的那只票在窗口里暴涨 +50%（如果它被算进去，n 和均价都会变形）。
    """
    days = workdays("2024-03-04", 4)
    series = {
        # 600001：D+1 开盘就到 +10%（一字板），之后一路涨（剔除与否一眼可辨）
        "600001": {"open": [10.0, 11.0, 13.0, 15.0], "close": [10.0, 13.0, 15.0, 16.5]},
        # 600002：正常能买到，窗口收益 +5%
        "600002": {"open": [10.0, 10.0, 10.0, 10.0], "close": [10.0, 10.0, 10.5, 10.5]},
    }
    db = build_db(tmp_path / "limit_up.db", days, series)

    def first_day_only(day_panel: pd.DataFrame) -> list[str]:
        # 只在第一个交易日匹配：否则第二天也会出信号，把"剔除 vs 没剔除"的区别冲掉
        return ["600001", "600002"] if day_panel["date"].max() == days[0] else []

    market = {(days[0], "T+1"): 0.0}
    result = sc.evaluate(db, first_day_only, horizons=(1,), burn_in=0,
                         market=market, min_samples=1, min_market_symbols=1)
    row = result["rows"][0]

    assert row["dropped_limit_up"] == 1        # 一字板那只被计数
    assert row["n"] == 1                       # 只剩能买到的那只
    assert row["avg_alpha_pct"] == pytest.approx(5.0, abs=1e-6)   # 不是 (5+50)/2
    assert row["win_rate_pct"] == pytest.approx(100.0, abs=1e-6)


# ── 4) α 的定义 ──


def test_following_the_market_gives_zero_alpha(tmp_path: Path):
    """全市场普涨 10%，被选中的票也普涨 10% → α 必须 ≈ 0。"""
    days = workdays("2024-05-06", 6)
    symbols = [f"60000{i}" for i in range(1, 7)]
    series = {symbol: {"open": [10.0] * 6, "close": [10.0, 10.0, 11.0, 11.0, 11.0, 11.0]}
              for symbol in symbols}
    db = build_db(tmp_path / "market.db", days, series)

    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1,), burn_in=0,
                         min_samples=1, min_market_symbols=1)
    row = result["rows"][0]
    assert row["n"] == 4
    assert row["avg_ret_pct"] == pytest.approx(10.0, abs=1e-6)    # 绝对收益 > 0（行情好）
    assert row["avg_alpha_pct"] == pytest.approx(0.0, abs=1e-6)   # 但 α = 0（没有匹配能力）


def test_market_benchmark_is_equal_weighted():
    """基准 = 同期全市场等权收益（不是沪深300，也不是中位数）。"""
    days = workdays("2024-06-03", 3)
    panel = pd.DataFrame([
        # A：窗口 +10%；B：窗口 0% → 等权基准 = 5%
        {"symbol": "A", "date": days[0], "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
        {"symbol": "B", "date": days[0], "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
        {"symbol": "A", "date": days[1], "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
        {"symbol": "B", "date": days[1], "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
        {"symbol": "A", "date": days[2], "open": 10.0, "close": 11.0,
         "high": 11.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
        {"symbol": "B", "date": days[2], "open": 10.0, "close": 10.0,
         "high": 10.1, "low": 9.9, "volume": 1e6, "turnover": 1e8},
    ])
    market = sc.market_map(panel, (1,), min_symbols=2)
    assert market[(days[0], "T+1")] == pytest.approx(0.05, abs=1e-9)
    # 样本太少的交易日不给基准（宁缺毋滥）
    assert sc.market_map(panel, (1,), min_symbols=5) == {}


# ── 5) 样本不足 ──


def test_empty_db_reports_insufficient_instead_of_crashing(tmp_path: Path):
    """空库：要给出"没有任何行情 + 原因"，而不是除零崩溃或瞎给结论。"""
    empty = storage.init_db(tmp_path / "empty.db")
    result = sc.evaluate(str(empty), fixed_signal("600001"), horizons=(1,), burn_in=0,
                         min_samples=1, min_market_symbols=1)
    assert result["sufficient"] is False
    assert "没有任何行情" in result["reason"]
    assert result["rows"][0]["n"] == 0
    assert result["rows"][0]["avg_alpha_pct"] is None
    assert result["rows"][0]["t_stat"] is None


def test_single_day_db_reports_insufficient(tmp_path: Path):
    """只有 1 个交易日：信号日之后没有行情 → 样本不足（且说明是"没有后续行情"）。"""
    days = workdays("2024-07-01", 1)
    db = build_db(tmp_path / "one.db", days,
                  {"600001": {"open": [10.0], "close": [10.0]}})
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1, 3), burn_in=0,
                         min_samples=1, min_market_symbols=1)
    assert result["sufficient"] is False
    assert "样本不足" in result["reason"]
    assert result["rows"][0]["n"] == 0


def test_too_few_samples_withholds_conclusion(tmp_path: Path):
    """样本量远低于门槛（30 笔）时：数字照算，但必须标"不下结论"。"""
    days = workdays("2024-08-05", 6)
    series = {"600001": {"open": [10.0] * 6, "close": [10.0, 10.0, 10.1, 10.1, 10.1, 10.1]}}
    db = build_db(tmp_path / "few.db", days, series)
    # min_samples 用默认的 30；库级门槛放低，专门验证"笔数不够就不下结论"这一条
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1,), burn_in=0,
                         min_market_symbols=1, min_db_symbols=1, min_db_days=1)
    row = result["rows"][0]
    assert row["n"] == 4
    assert row["sufficient"] is False
    assert result["sufficient"] is False
    assert "样本不足" in result["reason"]
    assert "30" in result["reason"]                 # 门槛数字要出现，方便用户判断差多少


def test_small_library_is_refused_by_db_level_gate(tmp_path: Path):
    """库太小（几十只股票 / 几十个交易日）时，即使算得出笔数也不给结论。

    为什么：等权基准要的是"全市场"，几十只股票的均值几乎就是策略自己
    （α 会机械地趋近 0）；这种库上跑出来的"结论"是噪声，必须直说。
    """
    days = workdays("2025-01-06", 40)
    series = {f"60000{i}": {"open": [3.0] * 40, "close": [3.0] * 40} for i in range(1, 9)}
    db = build_db(tmp_path / "small.db", days, series)
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1,), burn_in=0,
                         min_samples=1, min_market_symbols=1)

    assert result["sufficient"] is False
    assert result["db_sufficient"] is False
    assert "库太小" in result["reason"]
    assert "40 个交易日" in result["reason"]        # 库里有多少数据必须如实写出来
    assert "8 只" in result["reason"]


def test_missing_db_reports_clear_error(tmp_path: Path):
    """库不存在：明确报错 + 可执行的下一步，而不是 traceback 或空结论。"""
    with pytest.raises(sc.ScorecardError) as excinfo:
        sc.evaluate(str(tmp_path / "nope.db"), fixed_signal("600001"), horizons=(1,),
                    burn_in=0, min_samples=1)
    assert "库不存在" in str(excinfo.value)


def test_describe_db_reports_range_and_rows(six_day_db):
    """"样本不足"的结论必须带上库里的日期范围与行数（否则用户没法自查）。"""
    db, days = six_day_db
    info = sc.describe_db(db)
    assert info["backend"] == "sqlite"
    assert info["rows"] == 36 and info["symbols"] == 6 and info["days"] == 6
    assert info["start"] == days[0] and info["end"] == days[-1]


# ── 6) 内置策略的 Spec（与实盘条件对齐） ──


def test_builtin_strategy_specs_are_gone():
    """内置 5 条策略的 spec 已随策略引擎删掉（2026-09-18），评估对象必须显式给。

    这一点值得钉住：`evaluate_all()` 以前有个"不给 specs 就评内置 5 条"的默认值，
    内置策略删掉之后那个默认值会让"什么都没评却报成功"这种事发生 ——
    所以现在不给 specs 直接报错。
    """
    assert not hasattr(sc, "BUILTIN_SPECS")
    with pytest.raises(sc.ScorecardError, match="没有给出要评估的策略定义"):
        sc.evaluate_all("不存在的库.db")


def test_custom_spec_filters_and_ranks(tmp_path: Path):
    """自定义 spec 的老路照旧能用：`spec_from_signal` + 排序 + 取前 N。

    （原来这条测的是内置「低价股」spec，它随策略引擎删掉了；这里改成自己写一个
    等价的信号函数 —— 恰恰就是"用户想评估自己的筛选逻辑"时走的那条路。）
    """
    days = workdays("2024-09-02", 25)          # 需要 20 日均额
    series = {
        "600001": {"open": [2.0] * 25, "close": [2.0] * 25},
        "600002": {"open": [5.0] * 25, "close": [5.0] * 25},
        "600003": {"open": [1.5] * 25, "close": [1.5] * 25},      # 低于 2 元门槛
        "600004": {"open": [3.0] * 25, "close": [3.0] * 24 + [2.7]},   # 当日跌停
    }
    db = build_db(tmp_path / "lowprice.db", days, series)

    def cheap_after_liquidity(day_panel: pd.DataFrame):
        """当日收盘截面：≥2 元、且当日没跌停（>−9.5%），按价格升序（最便宜的在前）。"""
        work = day_panel.sort_values(["symbol", "date"])
        work["prev_close"] = work.groupby("symbol", sort=False)["close"].shift(1)
        today = work.groupby("symbol", sort=False).tail(1)
        picked = today[(today["close"] >= 2.0)
                       & (today["close"] / today["prev_close"] > 0.905)]
        picked = picked.sort_values("close")
        return [(row.symbol, float(row.close)) for row in picked.itertuples()]

    # 候选顺序即优先级（`spec_from_signal` 把位置当 factor、升序取前 N）
    spec = sc.spec_from_signal("低价近似", "低价近似", cheap_after_liquidity, top_n=30)
    panel = sc.load_panel(db)
    picks = sc.build_picks(panel, spec, top_n=30, risk_symbols=set())
    last_day = picks[picks["date"] == days[-1]]
    assert list(last_day["symbol"]) == ["600001", "600002"]      # 1.5 元被门槛挡掉


def test_attach_ladder_shifts_limit_up_to_the_previous_day(tmp_path: Path):
    """`attach_ladder()` 必须把涨停池数据挂到**前一行**（= 昨日状态），否则是未来函数。

    原来这条用例借"连板回踩"那个 spec 来验它；spec 删掉之后直接验这个函数本身，
    反而更准：它现在只服务**自定义公式的回测**（公式里写 `连板()`/`涨停天数()` 时，
    时点关系一样不能错）。
    """
    days = workdays("2024-10-08", 5)
    series = {"600001": {"open": [10.0] * 5, "close": [10.0] * 5}}
    db = build_db(tmp_path / "ladder.db", days, series)
    with storage.connect(db) as conn:
        storage.write_limit_up_pool(conn, [
            (days[-1], "600001", "样本600001", 3, "连板", "09:31:00", "09:35:00",
             8e7, 0, "测试", 5.0, 10.0, 1e9, 1, 0, "3连板", 9e7, 9.8, 0, "test", "t"),
        ])

    work = sc.attach_ladder(sc.load_panel(db), sc.load_ladder(db))

    today = work[work["date"] == days[-1]].iloc[0]
    yesterday = work[work["date"] == days[-2]].iloc[0]
    assert today["lu_days_today"] == 3          # 当日原始值
    assert today["prev_lu_days"] == 0           # 但"昨日连板"仍是 0（不能用当日数据）
    assert yesterday["prev_lu_days"] == 0       # 再往前一天也没有

    # 涨停池只有一天数据时，唯一会被"看到"的是它的**下一行**
    shifted = work[work["date"] > days[-1]]
    assert shifted.empty or shifted.iloc[0]["prev_lu_days"] == 0


def test_risk_symbols_are_excluded(tmp_path: Path):
    """ST/退市风险股在成绩单里也要剔除（与实盘一致）。"""
    days = workdays("2024-11-04", 25)
    series = {"600001": {"open": [2.0] * 25, "close": [2.0] * 25},
              "600002": {"open": [3.0] * 25, "close": [3.0] * 25}}
    db = build_db(tmp_path / "risk.db", days, series)
    with storage.connect(db) as conn:
        storage.write_stock_basic(conn, [("600002", "*ST样本", "测试行业")])
    assert sc.load_risk_symbols(db) == {"600002"}
    panel = sc.load_panel(db)
    spec = sc.spec_from_signal("全选", "全选", fixed_signal("600001", "600002"), top_n=30)
    picks = sc.build_picks(panel, spec, top_n=30, risk_symbols=sc.load_risk_symbols(db))
    assert set(picks["symbol"]) == {"600001"}          # *ST 那只被剔除


def test_callable_spec_is_accepted(six_day_db):
    """自定义公式可以直接传函数（成绩单是将来“自定义策略”的评估底座）。"""
    db, days = six_day_db
    target = days[-3]      # 倒数第三天：次日买入、再次日卖出，两段行情都齐全

    def formula(day_panel: pd.DataFrame) -> list[str]:
        # 逐日切片：切片的最后一天 == target 时才给候选（验证是按日调用、且最后一天有后续行情）
        return ["600001"] if day_panel["date"].max() == target else []

    result = sc.evaluate(db, formula, horizons=(1,), burn_in=0, min_samples=1,
                         min_market_symbols=1)
    assert result["key"] == "formula"
    assert result["rows"][0]["n"] == 1


# ── 7) 落盘（CSV / Markdown） ──


def test_export_writes_csv_and_markdown(six_day_db, tmp_path: Path):
    db, _days = six_day_db
    # 故意用默认门槛（30 笔）跑，好让报告里出现"样本不足"的说明
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1, 3), burn_in=0,
                         min_market_symbols=1)
    assert result["sufficient"] is False
    main_csv = sc.write_csv(sc.result_rows_for_export([result]),
                            tmp_path / "main.csv", fields=sc.RESULT_FIELDS)
    year_csv = sc.write_csv(sc.by_year_rows_for_export([result]),
                            tmp_path / "year.csv", fields=sc.BY_YEAR_FIELDS)
    markdown = sc.write_markdown([result], tmp_path / "report.md", title="测试成绩单")

    text = main_csv.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("strategy,label,group,convention")
    assert "dropped_limit_up_close" in text and "convention_label" in text
    assert "avg_alpha_pct" in text and "sufficient" in text
    # 逐年 CSV 至少要有表头（哪怕这一年没有样本）
    assert "year" in year_csv.read_text(encoding="utf-8-sig").splitlines()[0]
    report = markdown.read_text(encoding="utf-8")
    assert "# 测试成绩单" in report
    assert "T+0" in report and "一字板" in report        # 口径必须写进报告里
    assert "样本不足" in report                          # 没有结论时要明说


def test_empty_export_still_has_headers(tmp_path: Path):
    """一条结论都没有时也要写出正确的表头（Excel 打开不能是一片空白）。"""
    path = sc.write_csv([], tmp_path / "empty.csv", fields=sc.RESULT_FIELDS)
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("strategy,label")


def test_format_table_shows_limits_and_notes(six_day_db):
    """打印表格必须自带口径说明 —— 数字离开口径就会被当成结论。"""
    db, _days = six_day_db
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1,), burn_in=0,
                         min_samples=1, min_market_symbols=1)
    table = sc.format_table([result])
    assert "T+0" in table and "不可执行" in table
    assert "信号日" in table and "一字板" in table
    assert "D+1 开盘" in table


# ── 8) 执行口径（进场时点 × 进场价 × 出场时点 × 出场价） ──


def test_t0_convention_is_rejected():
    """"D+1 开盘买、D+1 收盘卖"= T+0，必须被拒绝（合法性硬约束）。"""
    illegal = sc.Convention("X", "T+0 演示", 1, "open", 1, "close")
    with pytest.raises(sc.IllegalConventionError) as excinfo:
        sc.check_convention(illegal)
    message = str(excinfo.value)
    assert "不可执行" in message and "T+1" in message

    # 进场早于 D+1 也不合法（信号是 D 收盘才有的）
    with pytest.raises(sc.IllegalConventionError):
        sc.check_convention(sc.Convention("Y", "D 当天买", 0, "close", 2, "close"))
    # 成交价写错也要炸（只有 open/close 两档）
    with pytest.raises(sc.IllegalConventionError):
        sc.check_convention(sc.Convention("Z", "均价买", 1, "vwap", 2, "close"))

    # 默认口径里每一套都必须合法（含尾盘买 / 隔夜卖）
    for conv in sc.CONVENTIONS:
        assert sc.check_convention(conv) is conv
        assert conv.exit_offset > conv.entry_offset >= 1
    # A/B/C 的出场窗口相同，只有进场价不同 —— 结论比较才有意义
    by_key = {conv.key: conv for conv in sc.CONVENTIONS}
    assert (by_key["A"].exit_offset, by_key["A"].exit_price) == (2, "close")
    assert (by_key["B"].exit_offset, by_key["B"].exit_price) == (2, "close")
    assert by_key["A"].entry_price == "open" and by_key["B"].entry_price == "close"
    assert (by_key["C"].entry_price, by_key["C"].exit_price) == ("close", "open")


def test_tail_entry_uses_close_price_to_close_price(tmp_path: Path):
    """B 口径 = D+1 收盘买 → D+2 收盘卖（手算断言，基准固定注入）。"""
    days = workdays("2024-01-02", 4)
    # 开盘价故意跳动：如果代码误用开盘价，结果会明显不同（这正是要钉死的点）
    series = {"600001": {"open": [10.0, 20.0, 30.0, 40.0],
                         "close": [10.0, 10.0, 10.2, 10.2]},
              "600002": {"open": [10.0] * 4, "close": [10.0] * 4}}
    db = build_db(tmp_path / "tail.db", days, series)
    conv = sc.Convention("B", "尾盘买", 1, "close", 2, "close")
    market = {(days[0], "B"): 0.005}      # 同口径基准固定 +0.5%
    result = sc.evaluate(db, fixed_signal("600001"), conventions=(conv,), burn_in=0,
                         market=market, min_samples=1, min_market_symbols=1)
    row = result["rows"][0]
    # D+1(01-03) 收盘 10.0 买 → D+2(01-04) 收盘 10.2 卖 = +2%；α = 2% − 0.5% = 1.5%
    # （只给第一个信号日注基准 → n=1，手算才对得上）
    assert row["n"] == 1
    assert row["avg_ret_pct"] == pytest.approx(2.0, abs=1e-6)
    assert row["avg_market_pct"] == pytest.approx(0.5, abs=1e-6)
    assert row["avg_alpha_pct"] == pytest.approx(1.5, abs=1e-6)
    assert row["entry"] == "D+1收盘" and row["exit"] == "D+2收盘"
    assert row["convention"] == "B"

    # 用开盘价（开盘是 20/30）算的话，收益会是另一番模样 —— 证明确实取的是收盘价
    conv_a = sc.Convention("A", "开盘买", 1, "open", 2, "close")
    result_a = sc.evaluate(db, fixed_signal("600001"), conventions=(conv_a,), burn_in=0,
                           market={(days[0], "A"): 0.005}, min_samples=1,
                           min_market_symbols=1)
    assert result_a["rows"][0]["avg_ret_pct"] != pytest.approx(2.0, abs=0.1)


def test_overnight_convention_buys_close_sells_next_open(tmp_path: Path):
    """C 口径 = D+1 收盘买 → D+2 开盘卖（只吃一个隔夜跳空）。"""
    days = workdays("2024-02-05", 4)
    series = {"600001": {"open": [10.0, 20.0, 21.0, 40.0],
                         "close": [10.0, 10.0, 10.2, 10.2]},
              "600002": {"open": [10.0] * 4, "close": [10.0] * 4}}
    db = build_db(tmp_path / "overnight.db", days, series)
    conv = sc.Convention("C", "隔夜卖", 1, "close", 2, "open")
    result = sc.evaluate(db, fixed_signal("600001"), conventions=(conv,), burn_in=0,
                         market={(days[0], "C"): 0.0}, min_samples=1, min_market_symbols=1)
    row = result["rows"][0]
    # D+1(02-06) 收盘 10.0 买 → D+2(02-07) 开盘 21.0 卖 = +110%
    assert row["n"] == 1
    assert row["avg_ret_pct"] == pytest.approx(110.0, abs=1e-6)
    assert row["exit"] == "D+2开盘" and row["entry"] == "D+1收盘"
    # 卖出日仍然严格晚于买入日（T+1 合法性）
    assert row["horizon"] == 1


def test_benchmark_must_use_the_same_entry_price():
    """α 的基准必须与逐笔**同口径**：尾盘买就用"D+1 收盘 → 出场"的等权收益。

    构造：A 票开盘价与收盘价给出两种完全不同的收益。若基准误用开盘口径，
    两套口径算出来的"市场收益"就会不一样，测试直接抓住。
    """
    days = workdays("2024-06-03", 3)
    rows = []
    for symbol, closes in (("A", [10.0, 10.0, 11.0]), ("B", [10.0, 10.0, 10.0]),
                           ("C", [10.0, 10.0, 10.0]), ("D", [10.0, 10.0, 10.0]),
                           ("E", [10.0, 10.0, 10.0])):
        for index, day in enumerate(days):
            # 开盘价统一比前收高 5%（小于 9.5% 才买得到；若基准误用开盘价，结果会完全不同）
            rows.append({"symbol": symbol, "date": day, "open": 10.5, "close": closes[index],
                         "high": 11.1, "low": 9.9, "volume": 1e6, "turnover": 1e8})
    panel = pd.DataFrame(rows)

    conv_a = sc.Convention("A", "开盘买", 1, "open", 2, "close")
    conv_b = sc.Convention("B", "尾盘买", 1, "close", 2, "close")
    market = sc.market_map(panel, (conv_a, conv_b), min_symbols=5)
    # A 票：A 口径 = 11/10.5−1 = +4.76%；B 口径 = 11/10−1 = +10%
    # 其余 4 只：A 口径 = 10/10.5−1 = −4.76%；B 口径 = 0%
    expect_a = ((11 / 10.5 - 1) + 4 * (10 / 10.5 - 1)) / 5
    expect_b = (11 / 10 - 1) / 5
    assert market[(days[0], "B")] == pytest.approx(expect_b, abs=1e-9)
    assert market[(days[0], "A")] == pytest.approx(expect_a, abs=1e-9)
    # 开盘口径的基准明显更低（被开盘缺口拖累）→ 两套基准绝不能混用
    assert expect_a < 0 < expect_b


def test_close_limit_up_is_dropped_only_for_tail_entry(tmp_path: Path):
    """尾盘买时"收盘涨停"也买不进：B 口径剔除并计入 dropped_limit_up_close；
    A 口径（开盘买）不受影响 —— 两套口径的剔除必须分开数。"""
    days = workdays("2024-03-04", 4)
    series = {
        # 600001：D+1 开盘只 +1%（开盘买得到），但 D+1 收盘 +10%（尾盘涨停买不到）
        "600001": {"open": [10.0, 10.1, 11.0, 11.0], "close": [10.0, 11.0, 11.2, 11.2]},
        # 600002：正常票（两套都买得到）
        "600002": {"open": [10.0] * 4, "close": [10.0, 10.0, 10.1, 10.1]},
    }
    db = build_db(tmp_path / "tail_limit.db", days, series)

    def first_day_only(day_panel: pd.DataFrame) -> list[str]:
        return ["600001", "600002"] if day_panel["date"].max() == days[0] else []

    conv_a = sc.Convention("A", "开盘买", 1, "open", 2, "close")
    conv_b = sc.Convention("B", "尾盘买", 1, "close", 2, "close")
    market = {(days[0], "A"): 0.0, (days[0], "B"): 0.0}
    result = sc.evaluate(db, first_day_only, conventions=(conv_a, conv_b), burn_in=0,
                         market=market, min_samples=1, min_days=1,
                         min_market_symbols=1, min_db_symbols=1, min_db_days=1)
    rows = {row["convention"]: row for row in result["rows"]}

    assert rows["A"]["dropped"] == 0 and rows["A"]["n"] == 2      # 开盘口径两只都买得到
    assert rows["A"]["dropped_limit_up"] == 0
    assert rows["A"]["dropped_limit_up_close"] == 1               # 但尾盘口径会剔掉 1 只
    assert rows["B"]["dropped"] == 1 and rows["B"]["n"] == 1      # 尾盘口径只剩正常那只
    assert rows["B"]["dropped_limit_up_close"] == 1
    assert rows["B"]["avg_ret_pct"] == pytest.approx(1.0, abs=1e-6)   # 只算了 600002


def test_verdict_flags_open_only_edge(tmp_path: Path):
    """只有开盘口径为正 → 必须标注"可能依赖开盘执行"，不能当成"策略有边际"。"""
    days = workdays("2024-04-01", 4)
    # 开盘买得到好处、尾盘买不到：D+1 开盘 10 → D+1 收盘 10.5 → D+2 收盘 10.2
    series = {"600001": {"open": [10.0, 10.0, 10.2, 10.2],
                         "close": [10.0, 10.5, 10.2, 10.2]},
              "600002": {"open": [10.0] * 4, "close": [10.0] * 4}}
    db = build_db(tmp_path / "openonly.db", days, series)
    conv_a = sc.Convention("A", "开盘买", 1, "open", 2, "close")
    conv_b = sc.Convention("B", "尾盘买", 1, "close", 2, "close")

    def first_day_only(day_panel: pd.DataFrame) -> list[str]:
        return ["600001"] if day_panel["date"].max() == days[0] else []

    market = {(days[0], "A"): 0.0, (days[0], "B"): 0.0}
    result = sc.evaluate(db, first_day_only, conventions=(conv_a, conv_b), burn_in=0,
                         market=market, min_samples=1, min_days=1,
                         min_market_symbols=1, min_db_symbols=1, min_db_days=1)
    rows = {row["convention"]: row for row in result["rows"]}
    # A：D+1 开盘 10.0 买 → D+2 收盘 10.2 卖 = +2.0%
    # B：D+1 收盘 10.5 买 → D+2 收盘 10.2 卖 = −2.86%
    assert rows["A"]["avg_alpha_pct"] == pytest.approx(2.0, abs=1e-6)
    assert rows["B"]["avg_alpha_pct"] == pytest.approx(10.2 / 10.5 * 100 - 100, abs=1e-3)
    verdict = sc.margin_verdicts([result])[0]
    assert "依赖开盘执行" in verdict["verdict"]
    assert verdict["a_alpha"] == pytest.approx(2.0, abs=1e-6)

    # 反过来：尾盘为正、开盘为负 → 另一种标注
    # D+1 开盘 10.4（缺口 +4%，买得到）→ D+1 收盘 10.2 → D+2 收盘 10.3
    series2 = {"600001": {"open": [10.0, 10.4, 10.3, 10.3],
                          "close": [10.0, 10.2, 10.3, 10.3]},
               "600002": {"open": [10.0] * 4, "close": [10.0] * 4}}
    db2 = build_db(tmp_path / "tailonly.db", days, series2)
    result2 = sc.evaluate(db2, first_day_only, conventions=(conv_a, conv_b), burn_in=0,
                          market=market, min_samples=1, min_days=1,
                          min_market_symbols=1, min_db_symbols=1, min_db_days=1)
    rows2 = {row["convention"]: row for row in result2["rows"]}
    assert rows2["B"]["avg_alpha_pct"] > 0 > rows2["A"]["avg_alpha_pct"]
    verdict2 = sc.margin_verdicts([result2])[0]
    assert "尾盘口径" in verdict2["verdict"]


def test_verdict_confirms_edge_when_both_positive(tmp_path: Path):
    """A、B 两套口径都为正 → 才算"有边际"。"""
    days = workdays("2024-05-06", 4)
    series = {"600001": {"open": [10.0, 10.0, 10.0, 10.0],
                         "close": [10.0, 10.0, 11.0, 11.0]},
              "600002": {"open": [10.0] * 4, "close": [10.0] * 4}}
    db = build_db(tmp_path / "both.db", days, series)
    conv_a = sc.Convention("A", "开盘买", 1, "open", 2, "close")
    conv_b = sc.Convention("B", "尾盘买", 1, "close", 2, "close")

    def first_day_only(day_panel: pd.DataFrame) -> list[str]:
        return ["600001"] if day_panel["date"].max() == days[0] else []

    market = {(days[0], "A"): 0.0, (days[0], "B"): 0.0}
    result = sc.evaluate(db, first_day_only, conventions=(conv_a, conv_b), burn_in=0,
                         market=market, min_samples=1, min_days=1,
                         min_market_symbols=1, min_db_symbols=1, min_db_days=1)
    rows = {row["convention"]: row for row in result["rows"]}
    assert rows["A"]["avg_alpha_pct"] > 0 and rows["B"]["avg_alpha_pct"] > 0
    assert "有边际" in sc.margin_verdicts([result])[0]["verdict"]


def test_default_conventions_include_a_b_c_and_controls():
    """默认口径集合必须包含用户要的 A/B/C 与对照档（不然 CLI 就不是"全跑"）。"""
    keys = [conv.key for conv in sc.CONVENTIONS]
    assert keys[:3] == ["A", "B", "C"]
    assert {"A3", "A5", "A10", "B3", "B5", "B10"} <= set(keys)
    assert sc.PRIMARY_CONVENTION_PAIR == ("A", "B")
    # 对照档：尾盘买（B 家族）出场偏移就是 D+3/D+5/D+10
    by_key = {conv.key: conv for conv in sc.CONVENTIONS}
    assert by_key["B10"].entry_price == "close" and by_key["B10"].exit_offset == 10
    assert by_key["A10"].entry_price == "open" and by_key["A10"].exit_offset == 10


def test_old_horizons_api_is_still_supported(six_day_db):
    """旧的 horizons 参数仍然能用（与 NAS 历史结论对齐的那套口径）。"""
    db, days = six_day_db
    result = sc.evaluate(db, fixed_signal("600001"), horizons=(1, 3), burn_in=0,
                         min_samples=1, min_market_symbols=1)
    assert result["conventions"] == ("T+1", "T+3")
    rows = {row["convention"]: row for row in result["rows"]}
    assert rows["T+1"]["entry"] == "D+1开盘" and rows["T+1"]["exit"] == "D+2收盘"
    assert rows["T+3"]["exit"] == "D+4收盘"
    assert rows["T+1"]["horizon"] == 1 and rows["T+3"]["horizon"] == 3


# ══════════════════════════════════════════════════════════════════════════
# 坏样本（NULL 价 → NaN）不许拖垮整条成绩单
# ══════════════════════════════════════════════════════════════════════════


def test_daily_t_survives_a_nan_sample() -> None:
    """一个 NaN 样本不许让 t 检验崩，也不许印出 `+nan%`。

    实测过的现场（2026-10-08）：库里某一行 `close` 是 NULL（源里缺那一列，
    `stock_daily_hfq` 视图里就是 NULL），那一笔收益算成 NaN；
    `statistics.stdev([nan, ...])` 直接抛
    `AttributeError: 'float' object has no attribute 'numerator'` —— 整条成绩单失败。
    只有一个信号日时更隐蔽：返回 `avg = nan`，报告里就印出 `+nan%`。
    """
    nan = float("nan")

    # 混着 NaN：坏样本被剔掉，剩下那条照常出结论
    t_stat, avg, days = sc.daily_t({"2026-01-01": [nan], "2026-01-02": [0.01]})
    assert days == 1 and avg == pytest.approx(0.01) and t_stat is None

    # 全是 NaN：当作"没有样本"，**不是 0、也不是 nan**
    t_stat, avg, days = sc.daily_t({"2026-01-01": [nan], "2026-01-02": [nan]})
    assert (t_stat, avg, days) == (None, None, 0)

    # 两天的好样本照旧算得出 t 值（改动没有把正常路径一起关掉）
    t_stat, avg, days = sc.daily_t({"2026-01-02": [0.01], "2026-01-03": [0.03]})
    assert days == 2 and avg == pytest.approx(0.02) and t_stat is not None


def test_finite_price_rejects_nan_none_and_zero() -> None:
    """`_finite_price()`：只有"有限的正数"才算价（0 与 NaN 都不行）。

    为什么不能用 `if not value`：NaN 在 Python 里是**真值**，`not nan` 是 False ——
    这正是那个 bug 的根因。
    """
    assert sc._finite_price(10.0) is True
    assert sc._finite_price("10.5") is True          # 从 sqlite 读出来可能是字符串
    assert sc._finite_price(None) is False
    assert sc._finite_price(float("nan")) is False
    assert sc._finite_price(float("inf")) is False
    assert sc._finite_price(0) is False
    assert sc._finite_price(-1) is False
    assert sc._finite_price(True) is False           # 布尔不是价
