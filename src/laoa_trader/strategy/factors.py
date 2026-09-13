"""量价因子计算：作用在「长表」面板上的纯函数。

**整段移植自服务器版 `sequoia_x/strategy/factors.py`**，公式、窗口、阈值一字未改。
唯一改动是 `load_panel()` 读的表名：服务器版读 `stock_daily`（库里存的是后复权价），
桌面版读 `stock_daily_hfq` **视图**（原始价 × factor 实时算出的后复权价）——
两者数值完全一致，只是存储分层不同（见 `data/storage.py` 的说明）。

为什么单独抽一层：**回测与实盘必须使用同一套公式**，否则回测结论对实盘没有意义。
本模块的函数输入长表 DataFrame（列含 symbol/date/close...，按 symbol/date 升序），
输出与输入同索引的因子 Series；实盘取**最新交易日截面**做横截面排序选股。
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

#: 策略读取的行情表（后复权视图；与服务器版 `stock_daily` 数值等价）
PRICE_TABLE = "stock_daily_hfq"

# 默认载入的列
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "turnover")

# A 股涨停判定：主板 10%、创业板/科创板 20%。用 9.5% 作为保守阈值（区分"接近涨停"）
LIMIT_UP_THRESHOLD = 0.095


def load_panel(
    db_path: str,
    columns: tuple[str, ...] = PRICE_COLUMNS,
    symbols: list[str] | None = None,
    date_from: str | None = None,
) -> pd.DataFrame:
    """载入长表行情面板（后复权），按 (symbol, date) 升序。

    Args:
        db_path: SQLite 路径。
        columns: 需要的行情列。
        symbols: 只载入这些股票（为空则全部）。
        date_from: 只载入该日期之后的数据。

    Returns:
        DataFrame[symbol, date, <columns>]，已排序。
    """
    cols = ", ".join(columns)
    sql = f"SELECT symbol, date, {cols} FROM {PRICE_TABLE} WHERE 1=1"  # noqa: S608
    params: list = []
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    if symbols:
        placeholders = ",".join("?" * len(symbols))
        sql += f" AND symbol IN ({placeholders})"
        params.extend(symbols)
    sql += " ORDER BY symbol, date"

    with sqlite3.connect(db_path, timeout=60) as conn:
        df = pd.read_sql(sql, conn, params=tuple(params))
    return df


def add_columns(df: pd.DataFrame, symbol_col: str = "symbol") -> pd.DataFrame:
    """补齐派生列（每只股票独立计算）。

    Returns:
        df 的副本，附加 `ma5/ma20/ma60/ret1/avg_turnover20` 等常用列。
    """
    out = df.copy()
    grouped = out.groupby(symbol_col, sort=False)
    out["ma5"] = grouped["close"].transform(lambda s: s.rolling(5).mean())
    out["ma20"] = grouped["close"].transform(lambda s: s.rolling(20).mean())
    out["ma60"] = grouped["close"].transform(lambda s: s.rolling(60).mean())
    out["ret1"] = grouped["close"].transform(lambda s: s.pct_change())
    out["avg_turnover20"] = grouped["turnover"].transform(lambda s: s.rolling(20).mean())
    out["vol_ma20"] = grouped["volume"].transform(lambda s: s.rolling(20).mean())
    return out


# ── 因子：全部返回与 df 同索引的 Series ──


def momentum(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """过去 window 个交易日的收益率（短期反转用它的相反数）。"""
    return df.groupby("symbol", sort=False)["close"].transform(
        lambda s: s / s.shift(window) - 1
    )


def volatility(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """过去 window 日**日收益率**的标准差（低波动因子的原始值）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    tmp = df[["symbol"]].copy()
    tmp["_ret"] = ret
    return tmp.groupby("symbol", sort=False)["_ret"].transform(
        lambda s: s.rolling(window).std()
    )


def max_daily_return(df: pd.DataFrame, window: int = 21) -> pd.Series:
    """MAX 因子：过去 window 日内**单日最大涨幅**（彩票属性，越小越好）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    tmp = df[["symbol"]].copy()
    tmp["_ret"] = ret
    return tmp.groupby("symbol", sort=False)["_ret"].transform(
        lambda s: s.rolling(window).max()
    )


def volume_cv(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """量稳因子：过去 window 日成交量的变异系数（std/mean，越小越"稳"）。"""
    vol = df.groupby("symbol", sort=False)["volume"]
    mean = vol.transform(lambda s: s.rolling(window).mean())
    std = vol.transform(lambda s: s.rolling(window).std())
    return std / mean.replace(0, pd.NA)


def distance_to_high(df: pd.DataFrame, window: int = 250) -> pd.Series:
    """距区间最高价的比值：close / 过去 window 日最高价（越接近 1 越靠近高点）。"""
    highest = df.groupby("symbol", sort=False)["high"].transform(
        lambda s: s.rolling(window, min_periods=60).max()
    )
    return df["close"] / highest.replace(0, pd.NA)


def limit_up_flag(df: pd.DataFrame, threshold: float = LIMIT_UP_THRESHOLD) -> pd.Series:
    """当日是否涨停（用涨幅近似，未区分板块涨跌幅限制）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    return ret >= threshold


# ── 隔夜 / 日内拆分因子 ──
#
# A 股实证：把日收益拆成"隔夜"与"日内"两段后，两段的方向是**相反**的 ——
# 隔夜收益偏反转、日内收益偏动量。这类因子的好处是与现有策略的相关性低。


def _grouped_series(df: pd.DataFrame, values: pd.Series) -> pd.core.groupby.SeriesGroupBy:
    tmp = df[["symbol"]].copy()
    tmp["_v"] = values
    return tmp.groupby("symbol", sort=False)["_v"]


def overnight_return(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """过去 window 日累计**隔夜**收益（今开 / 昨收 − 1 的累计）。"""
    prev_close = _grouped_series(df, df["close"]).shift(1)
    ratio = (df["open"] / prev_close).where(prev_close > 0)
    log_ret = pd.Series(np.log(ratio.to_numpy()), index=df.index)
    total = _grouped_series(df, log_ret).transform(lambda s: s.rolling(window).sum())
    return pd.Series(np.expm1(total.to_numpy()), index=df.index)


def intraday_return(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """过去 window 日累计**日内**收益（今收 / 今开 − 1 的累计）。"""
    ratio = (df["close"] / df["open"]).where(df["open"] > 0)
    log_ret = pd.Series(np.log(ratio.to_numpy()), index=df.index)
    total = _grouped_series(df, log_ret).transform(lambda s: s.rolling(window).sum())
    return pd.Series(np.expm1(total.to_numpy()), index=df.index)


def idio_volatility(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """特质波动率（IVOL）：剔除当日全市场收益后的残差标准差（按 beta=1 简化）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    market = ret.groupby(df["date"]).transform("mean")
    resid = ret - market
    return _grouped_series(df, resid).transform(lambda s: s.rolling(window).std())


def skewness(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """日收益偏度（负偏度溢价：偏度越低未来收益越高）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    return _grouped_series(df, ret).transform(lambda s: s.rolling(window).skew())


def amihud_illiquidity(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """Amihud 非流动性：|日收益| / 成交额（元），取 window 日均值后放缩 1e8。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    illiq = ret.abs() / df["turnover"].where(df["turnover"] > 0)
    scaled = illiq * 1e8
    return _grouped_series(df, scaled).transform(lambda s: s.rolling(window).mean())


def rsi(df: pd.DataFrame, window: int = 6) -> pd.Series:
    """RSI（用简单均值近似 Wilder 平滑，短线足够）。"""
    close = df.groupby("symbol", sort=False)["close"]
    delta = close.transform(lambda s: s.diff())
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _grouped_series(df, gain).transform(lambda s: s.rolling(window).mean())
    avg_loss = _grouped_series(df, loss).transform(lambda s: s.rolling(window).mean())
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    out = 100 - 100 / (1 + rs)
    # avg_loss == 0 且 avg_gain > 0 → 一直涨 → RSI 100
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return out


def closing_strength(df: pd.DataFrame) -> pd.Series:
    """收盘强度：(收盘 − 最低) / (最高 − 最低)，1 = 收在当日最高。"""
    span = df["high"] - df["low"]
    return ((df["close"] - df["low"]) / span.where(span > 0)).clip(0, 1)


def consecutive_down_days(df: pd.DataFrame) -> pd.Series:
    """连续下跌天数（收阴/下跌即 +1，一旦上涨归零）。"""
    ret = df.groupby("symbol", sort=False)["close"].transform(lambda s: s.pct_change())
    down = (ret < 0).astype(float)

    def _streak(s: pd.Series) -> pd.Series:
        # 连续段计数：x != x.shift() 时重置
        blocks = (s != s.shift()).cumsum()
        return s.groupby(blocks).cumsum()

    return _grouped_series(df, down).transform(_streak)


def latest_snapshot(df: pd.DataFrame, symbols: list[str] | None = None) -> pd.DataFrame:
    """取每只股票**最新一行**构成截面。

    Args:
        df: 含 symbol/date 及因子列的面板。
        symbols: 只保留这些股票（通常传 `engine.get_active_symbols()`，
            保证不会用到最后一根K线停留在隔日的陈旧数据）。

    Returns:
        截面 DataFrame（每只股票一行）。
    """
    latest = df[df["date"] == df["date"].max()]
    if symbols is not None:
        latest = latest[latest["symbol"].isin(set(symbols))]
    return latest.copy()


def top_n_by(
    snapshot: pd.DataFrame, column: str, n: int = 30, ascending: bool = True
) -> list[str]:
    """按因子列取前 n 只（ascending=True 取最小，用于反转/低价等）。"""
    valid = snapshot.dropna(subset=[column])
    if valid.empty:
        return []
    return valid.nsmallest(n, column)["symbol"].tolist() if ascending else (
        valid.nlargest(n, column)["symbol"].tolist()
    )


def load_risk_symbols(db_path: str) -> set[str]:
    """风险警示股（ST/*ST/退市整理）的代码集合。

    做法来自名称匹配（`stock_basic.name` 含 "ST" 或 "退"）。
    这类股票涨跌幅限制 5%、有退市风险，量化策略通常直接排除。
    """
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            rows = conn.execute(
                "SELECT symbol FROM stock_basic WHERE name LIKE '%ST%' OR name LIKE '%退%'"
            ).fetchall()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def exclude_risk(snapshot: pd.DataFrame, risk_symbols: set[str]) -> pd.DataFrame:
    """剔除 ST/退市风险股。"""
    if not risk_symbols or snapshot.empty:
        return snapshot
    return snapshot[~snapshot["symbol"].isin(risk_symbols)]
