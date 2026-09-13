"""5 条入选策略的信号计算。

**逐条移植自服务器版**（`sequoia_x/strategy/{low_price,ladder_pullback,reversal,
dryup_expansion,first_limit_up}.py`），**条件、窗口、阈值一字未改**：

| 类名 | 中文名 | 关键条件 | 10 年样本 T+2/T+3 α |
|---|---|---|---|
| LowPriceStrategy | 低价股 | 绝对股价最低（≥2 元、日均成交额 ≥3000 万、非跌停、非 ST） | T+3 +0.13% (t=3.50) |
| LadderPullbackStrategy | 连板回踩低吸 | 昨日连板 ≥2 + 今日缩量收阴 + 不破 5 日线 | **T+2 +0.47% (t=2.02)** |
| ReversalStrategy | 短期反转 | 20 日跌幅 ≥10% + 流动性 + 排除跌停 | T+3 +0.08% (t=1.94) |
| DryUpExpansionStrategy | 地量后放量变盘 | 前 3 日地量（<20 日均量 ×0.7）+ 今日放量 ≥×1.5 + 收阳涨 0~9% | T+3 +0.11% |
| FirstLimitUpStrategy | 首板缩量整理 | 昨日首板（前日 <9.5%）+ 今日缩量 <昨日 + 收盘 ≥昨收 ×0.95 | T+3 +0.10% |

> 这些 α 都只有 0.1~0.5%，**远小于盘中波动**：策略只是"值得盯的清单"，
> 不是"买了就涨"的保证。实际执行靠盘中止损/止盈纪律（见 intraday.py）。

输入是本地库（后复权视图），输出 `{策略类名: [{"symbol","name","reason"}...]}`，
每条策略内部**按自己的因子排序**，`top_n` 可配。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger
from laoa_trader.strategy import factors
from laoa_trader.strategy.base import BaseStrategy

logger = get_logger(__name__)


class LowPriceStrategy(BaseStrategy):
    """低价股：绝对股价越低越优先（≥2 元）。

    证据：低价股因子在 A 股长期存在（散户偏好低价股、名义价格幻觉），
    但极可能与"小市值"重合；10 年样本里它是唯一四个持有期都显著为正的策略。
    """

    display_name: str = "低价股"
    group: str = "short"
    target_horizon: int = 10

    top_n: int = 30
    min_price: float = 2.0
    min_turnover: float = 3e7
    limit_down: float = -0.095

    def run(self) -> list[str]:
        panel = factors.load_panel(self.engine.db_path, ("close", "turnover"))
        if panel.empty:
            return []

        panel["factor"] = panel["close"]
        grouped = panel.groupby("symbol", sort=False)
        panel["ret1"] = grouped["close"].transform(lambda s: s.pct_change())
        panel["avg_turnover"] = grouped["turnover"].transform(lambda s: s.rolling(20).mean())

        snapshot = factors.latest_snapshot(panel, self.engine.get_active_symbols())
        snapshot = factors.exclude_risk(
            snapshot, factors.load_risk_symbols(self.engine.db_path)
        )
        snapshot = snapshot.dropna(subset=["factor", "avg_turnover", "ret1"])
        snapshot = snapshot[
            (snapshot["factor"] >= self.min_price)
            & (snapshot["avg_turnover"] >= self.min_turnover)
            & (snapshot["ret1"] > self.limit_down)
        ]
        selected = factors.top_n_by(snapshot, "factor", self.top_n, ascending=True)
        logger.info(f"低价股：候选 {len(snapshot)} 只，选出 {len(selected)} 只")
        return selected


class LadderPullbackStrategy(BaseStrategy):
    """连板回踩低吸（超短，T+2）：昨日连板 ≥2 + 今日缩量收阴 + 不破 5 日线。

    证据：T+2 α +0.47%（t=2.02）—— "打板族"里唯一在可执行的最短持有期上 t≥2 的设计，
    但不稳定（更长持有期转负），因此只进股票池、不单独推送。
    """

    display_name: str = "连板回踩低吸"
    group: str = "watch"
    target_horizon: int = 3

    top_n: int = 30
    min_days: int = 2
    max_volume_ratio: float = 0.9
    ma5_floor: float = 0.98
    min_turnover: float = 3e7
    limit_down: float = -0.095

    def run(self) -> list[str]:
        panel = factors.load_panel(self.engine.db_path, ("close", "volume", "turnover"))
        if panel.empty:
            return []
        with sqlite3.connect(self.engine.db_path, timeout=60) as conn:
            rows = conn.execute(
                "SELECT date, symbol, high_days FROM limit_up_pool WHERE high_days IS NOT NULL"
            ).fetchall()
        ladder = {(d, s): n for d, s, n in rows}
        if not ladder:
            logger.info("连板回踩低吸：无涨停池数据，跳过")
            return []

        grouped = panel.groupby("symbol", sort=False)
        panel["prev_close"] = grouped["close"].shift(1)
        panel["prev_volume"] = grouped["volume"].shift(1)
        panel["ma5"] = grouped["close"].transform(lambda s: s.rolling(5).mean())
        panel["avg_turnover"] = grouped["turnover"].transform(lambda s: s.rolling(20).mean())
        panel["prev_days"] = [
            ladder.get((prev, sym), 0)
            for prev, sym in zip(panel["date"].shift(1), panel["symbol"], strict=False)
        ]
        snapshot = factors.latest_snapshot(panel, self.engine.get_active_symbols())
        snapshot = factors.exclude_risk(snapshot, factors.load_risk_symbols(self.engine.db_path))
        snapshot = snapshot.dropna(subset=["prev_close", "prev_volume", "ma5", "avg_turnover"])
        snapshot = snapshot[
            (snapshot["prev_days"] >= self.min_days)
            & (snapshot["close"] < snapshot["prev_close"])
            & (snapshot["volume"] < snapshot["prev_volume"] * self.max_volume_ratio)
            & (snapshot["close"] >= snapshot["ma5"] * self.ma5_floor)
            & (snapshot["avg_turnover"] >= self.min_turnover)
        ].copy()
        snapshot["factor"] = snapshot["prev_days"]
        selected = factors.top_n_by(snapshot, "factor", self.top_n, ascending=False)
        logger.info(f"连板回踩低吸：候选 {len(snapshot)} 只，选出 {len(selected)} 只")
        return selected


class ReversalStrategy(BaseStrategy):
    """短期反转：买入过去一个月跌幅最大的股票。

    规则：20 日收益率 ≤ -10%（确实超跌）→ 按 20 日收益升序取前 30 →
    要求日均成交额 ≥ 3000 万（能进能出）→ 排除当日跌停（不接飞刀）。
    """

    display_name: str = "短期反转"
    group: str = "watch"
    target_horizon: int = 10

    window: int = 20            # 回看窗口（交易日）
    top_n: int = 30             # 取前 N
    min_drop: float = -0.10     # 窗口内至少跌 10%
    min_turnover: float = 3e7   # 日均成交额下限（元）
    limit_down: float = -0.095  # 当日跌幅达到该值视为跌停，剔除

    def run(self) -> list[str]:
        panel = factors.load_panel(self.engine.db_path, ("close", "turnover"))
        if panel.empty:
            return []

        panel["factor"] = factors.momentum(panel, self.window)
        panel["ret1"] = factors.momentum(panel, 1)
        panel["avg_turnover"] = panel.groupby("symbol", sort=False)["turnover"].transform(
            lambda s: s.rolling(self.window).mean()
        )

        snapshot = factors.latest_snapshot(panel, self.engine.get_active_symbols())
        # 排除 ST/退市风险股（涨跌幅 5%、有退市风险，不适合放进选股池）
        snapshot = factors.exclude_risk(
            snapshot, factors.load_risk_symbols(self.engine.db_path)
        )
        snapshot = snapshot.dropna(subset=["factor", "avg_turnover", "ret1"])
        snapshot = snapshot[
            (snapshot["factor"] <= self.min_drop)
            & (snapshot["avg_turnover"] >= self.min_turnover)
            & (snapshot["ret1"] > self.limit_down)
        ]
        selected = factors.top_n_by(snapshot, "factor", self.top_n, ascending=True)
        logger.info(f"短期反转：候选 {len(snapshot)} 只，选出 {len(selected)} 只")
        return selected


class DryUpExpansionStrategy(BaseStrategy):
    """地量后放量变盘：缩量后突然放量上涨者优先。

    规则：前 3 个交易日成交量均 < 前 20 日均量 ×0.7（地量）；今日成交量 ≥ 前 20 日均量 ×1.5
    （放量）；今日收阳且涨幅 0~9%（已涨停则不追）；股价 ≥2 元；20 日均成交额 ≥3000 万。
    按「今日量 ÷ 前 3 日均量」降序取前 30。
    """

    display_name: str = "地量后放量变盘"
    group: str = "watch"
    target_horizon: int = 3

    top_n: int = 30
    quiet_ratio: float = 0.7
    surge_ratio: float = 1.5
    max_gain: float = 0.09
    min_price: float = 2.0
    min_turnover: float = 3e7

    def _snapshot(self, panel):
        """剔除 ST/退市风险股后取最新截面（回测路径同样不含 ST，两边一致）。"""
        snapshot = factors.latest_snapshot(panel, self.engine.get_active_symbols())
        return factors.exclude_risk(snapshot, factors.load_risk_symbols(self.engine.db_path))

    def run(self) -> list[str]:
        panel = factors.load_panel(self.engine.db_path, ("open", "close", "volume", "turnover"))
        if panel.empty:
            return []

        grouped = panel.groupby("symbol", sort=False)
        panel["ret1"] = grouped["close"].transform(lambda s: s.pct_change())
        panel["vol_ma20_prev"] = grouped["volume"].transform(
            lambda s: s.shift(1).rolling(20).mean()
        )
        panel["avg_turnover"] = grouped["turnover"].transform(lambda s: s.rolling(20).mean())
        for lag in (1, 2, 3):
            panel[f"vol_lag{lag}"] = grouped["volume"].shift(lag)

        base = (panel["vol_lag1"] + panel["vol_lag2"] + panel["vol_lag3"]) / 3
        panel["factor"] = panel["volume"] / base.replace(0, float("nan"))

        snapshot = self._snapshot(panel).dropna(
            subset=["factor", "vol_ma20_prev", "avg_turnover", "ret1", "vol_lag3"]
        )
        snapshot = snapshot[
            (snapshot["vol_lag1"] < snapshot["vol_ma20_prev"] * self.quiet_ratio)
            & (snapshot["vol_lag2"] < snapshot["vol_ma20_prev"] * self.quiet_ratio)
            & (snapshot["vol_lag3"] < snapshot["vol_ma20_prev"] * self.quiet_ratio)
            & (snapshot["volume"] >= snapshot["vol_ma20_prev"] * self.surge_ratio)
            & (snapshot["close"] > snapshot["open"])
            & (snapshot["ret1"] > 0)
            & (snapshot["ret1"] <= self.max_gain)
            & (snapshot["close"] >= self.min_price)
            & (snapshot["avg_turnover"] >= self.min_turnover)
        ]
        selected = factors.top_n_by(snapshot, "factor", self.top_n, ascending=False)
        logger.info(f"地量后放量变盘：候选 {len(snapshot)} 只，选出 {len(selected)} 只")
        return selected


class FirstLimitUpStrategy(BaseStrategy):
    """首板缩量整理：昨日**首次**涨停、今日缩量不破位。

    规则：昨日涨幅 ≥9.5% 且前一日 <9.5%；今日涨幅 <9%（没继续涨停）；
    今日成交量 < 昨日；收盘 ≥ 昨日收盘 × 0.95。按"缩量幅度"升序取前 30。
    """

    display_name: str = "首板缩量整理"
    group: str = "watch"
    target_horizon: int = 3

    top_n: int = 30
    limit_up: float = 0.095
    #: 今日最高涨幅上限（继续涨停就不是"整理"了）
    max_today_gain: float = 0.09
    #: 今日收盘相对昨日收盘的下限
    floor_ratio: float = 0.95

    def run(self) -> list[str]:
        panel = factors.load_panel(self.engine.db_path, ("close", "volume"))
        if panel.empty:
            return []

        grouped = panel.groupby("symbol", sort=False)
        panel["ret1"] = grouped["close"].transform(lambda s: s.pct_change())
        panel["prev_ret1"] = grouped["ret1"].shift(1)
        panel["prev_ret2"] = grouped["ret1"].shift(2)
        panel["prev_close"] = grouped["close"].shift(1)
        panel["prev_volume"] = grouped["volume"].shift(1)

        panel["factor"] = panel["volume"] / panel["prev_volume"]
        snapshot = factors.latest_snapshot(panel, self.engine.get_active_symbols())
        snapshot = factors.exclude_risk(
            snapshot, factors.load_risk_symbols(self.engine.db_path)
        )
        snapshot = snapshot.dropna(subset=["factor", "ret1", "prev_ret1", "prev_close"])
        snapshot = snapshot[
            (snapshot["prev_ret1"] >= self.limit_up)
            & (snapshot["prev_ret2"] < self.limit_up)
            & (snapshot["ret1"] < self.max_today_gain)
            & (snapshot["factor"] < 1.0)
            & (snapshot["close"] >= snapshot["prev_close"] * self.floor_ratio)
        ]
        selected = factors.top_n_by(snapshot, "factor", self.top_n, ascending=True)
        logger.info(f"首板缩量整理：候选 {len(snapshot)} 只，选出 {len(selected)} 只")
        return selected


#: 入选策略（类名 → 类）。键必须与 `pool.POOL_STRATEGIES` 的键一致（权重按类名查）。
STRATEGIES: dict[str, type[BaseStrategy]] = {
    "LowPriceStrategy": LowPriceStrategy,
    "LadderPullbackStrategy": LadderPullbackStrategy,
    "ReversalStrategy": ReversalStrategy,
    "DryUpExpansionStrategy": DryUpExpansionStrategy,
    "FirstLimitUpStrategy": FirstLimitUpStrategy,
}

#: 中文名（界面/推送展示）
STRATEGY_LABELS: dict[str, str] = {
    name: (cls.display_name or name) for name, cls in STRATEGIES.items()
}


def strategy_label(class_name: str) -> str:
    """类名 → 中文名（找不到时去掉 Strategy 后缀，与服务器版 format_pool_lines 一致）。"""
    return STRATEGY_LABELS.get(class_name) or class_name.replace("Strategy", "")


def run_all(
    engine: DataEngine,
    settings: Any = None,
    *,
    top_n: int | None = None,
    names: dict[str, str] | None = None,
    selection: Any = None,
) -> tuple[dict[str, list[dict]], list[str]]:
    """跑选中的策略（默认全部 5 条）。

    Args:
        engine: DataEngine。
        settings: 配置对象（透传给策略，当前策略不使用）。
        top_n: 覆盖每条策略的取数上限；None 时用策略类自己的默认值（30）。
        names: {代码: 名称}；None 时从本地 `stock_basic` 读。
        selection: `groups.Selection` —— 只跑其中的策略（组级/策略级自选靠它生效）；
            None 表示全跑。

    Returns:
        (picks, errors)：
        - picks: `{策略类名: [{"symbol","name","reason"}...]}`（顺序即优先级），
          与服务器版 `pool.build_pool` 的输入格式完全一致；
        - errors: 失败的策略说明（单条策略失败不影响其它策略 —— 服务器版同款行为）。
    """
    if names is None:
        names = engine.get_stock_names()

    # 按**组顺序**遍历（超短 → 短线 → 波段），让输出顺序稳定、便于比对
    from laoa_trader.strategy import groups as groups_mod

    wanted = None if selection is None else set(selection.strategies)
    picks: dict[str, list[dict]] = {}
    errors: list[str] = []
    ordered = [n for n in groups_mod.all_strategies() if n in STRATEGIES]
    ordered += [n for n in STRATEGIES if n not in ordered]    # 未归组的策略排在最后

    for class_name in ordered:
        if wanted is not None and class_name not in wanted:
            continue          # 未启用的策略连跑都不跑：省时间，也不会落信号
        cls = STRATEGIES[class_name]
        try:
            strategy = cls(engine=engine, settings=settings)
            if top_n is not None and hasattr(strategy, "top_n"):
                strategy.top_n = int(top_n)
            symbols = strategy.run()
        except Exception as exc:  # noqa: BLE001 - 单条策略失败不该拖垮整体
            logger.warning(f"策略 {class_name} 执行失败：{type(exc).__name__}: {exc}")
            errors.append(f"{class_name}: {type(exc).__name__}: {exc}")
            continue
        picks[class_name] = [
            {"symbol": sym, "name": names.get(sym, sym), "reason": strategy.label}
            for sym in symbols
        ]
        logger.info(f"策略 {strategy.label}：{len(symbols)} 只")
    if selection is not None and getattr(selection, "warnings", None):
        logger.warning("策略选择提示：" + "；".join(selection.warnings))
    return picks, errors


def save_signals(
    engine: DataEngine,
    picks: dict[str, list[dict]],
    day: str | None = None,
) -> int:
    """把选股结果写入 `signal` 表（供盘中风控观察池与将来做前向跟踪）。"""
    from laoa_trader.data import storage

    day = day or engine.get_latest_data_date() or datetime.now().strftime("%Y-%m-%d")
    closes: dict[str, float] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, close FROM stock_daily_hfq WHERE date = ?", (day,)
        ).fetchall()
        closes = {r[0]: r[1] for r in rows}
        payload = [
            (day, class_name, pick["symbol"], pick.get("name"), closes.get(pick["symbol"]),
             None, pick.get("reason"))
            for class_name, items in picks.items()
            for pick in items
        ]
        return storage.write_signals(conn, payload)


__all__ = [
    "STRATEGIES",
    "STRATEGY_LABELS",
    "DryUpExpansionStrategy",
    "FirstLimitUpStrategy",
    "LadderPullbackStrategy",
    "LowPriceStrategy",
    "ReversalStrategy",
    "run_all",
    "save_signals",
    "strategy_label",
]
