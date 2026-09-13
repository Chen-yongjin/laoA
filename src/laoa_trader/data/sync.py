"""数据下载与更新。

三条路径
--------
1. `download_history()`：**首次建库**。下载同花顺全市场 10 年日K dump（`daily-k`）
   + 复权事件 dump（`adjustment-factors`），本地算后复权因子，写入
   `stock_daily_raw`（原始价 + factor）与 `adjust_event`。**可中断续传**：
   已在库里的 `(symbol, date)` 直接跳过，中断后重跑只补缺口。
2. `sync_daily()`：**日更**。用 `daily-k-10d`（近 10 个交易日全市场）+ 当日涨停池，
   不需要估值。
3. `sync_calendar()` / `sync_industry()` / `sync_index()`：交易日历、一级行业归属、指数日线。

复权公式（与服务器版 `dump_import.py` 逐字一致）
----------------------------------------------
除权日理论除权价 = (前收 - 现金分红 + 配股比例×配股价) / (1 + 送股比例 + 配股比例)
为保持序列连续，除权日及之后的复权因子累乘 k：

    k = 前收 × (1 + 送股 + 配股) / (前收 - 现金分红 + 配股×配股价)

于是 `后复权价_t = 原始价_t × ∏_{除权日 ≤ t} k`（最早的价格保持真实）。
本项目把它存成 `factor` 列，由 `stock_daily_hfq` 视图实时算后复权价 ——
数值与服务器版写进 `stock_daily` 的后复权价**完全一致**。

错误处理约定
------------
所有对外函数**都不抛异常**（网络抖动不该让 GUI 崩），一律返回 `SyncResult`
（成功/失败原因、写入行数、附加信息）。调用方（GUI / CLI / scheduler）
只看 `result.ok` 与 `result.message`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from laoa_trader import state
from laoa_trader.config import Config, get_config
from laoa_trader.data import hithink as hx
from laoa_trader.data import storage
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 进度回调签名：progress_cb(stage, done, total)
ProgressCb = Callable[[str, int, int], None]

#: 状态回调签名：note_cb(一句话中文状态)
NoteCb = Callable[[str], None]

#: 默认 dump 目录
DEFAULT_DUMP_DIR = "dumps"

#: 需要同步的指数（内部代码 → 同花顺 thscode）：沪深300 是基准，其余用于市场状态
DEFAULT_INDICES: dict[str, str] = {
    "sh.000300": "000300.SH",  # 沪深300
    "sh.000001": "000001.SH",  # 上证指数
    "sz.399001": "399001.SZ",  # 深证成指
    "sz.399006": "399006.SZ",  # 创业板指
}

#: 同花顺 `daily-k` dump **固定**覆盖约 10 年（端点没有"只要 5 年"的选项，
#: 所以只能整个下载下来、导入时按 `history_years` 过滤）
DUMP_SPAN_YEARS = 10

#: 首次导入的默认年限（可被 config 的 `history_years` 覆盖）。
#: 默认 5 年：全市场约 500 万行，分发包更小、首次下载更快；
#: 要做长样本回测就把 `history_years` 改成 10（库会大一倍、内存峰值也更高）。
DEFAULT_HISTORY_YEARS = 5

#: 兼容旧名（历史上这个常量表示"dump 覆盖 10 年"）
HISTORY_YEARS = DUMP_SPAN_YEARS

#: 一年按多少天算（与 preflight 保持一致）
DAYS_PER_YEAR = 365.25


class _Cancelled(Exception):
    """内部信号：用户在下载过程中点了取消（已写入的数据保留，下次续传）。"""


@dataclass
class SyncResult:
    """结构化同步结果（成功/失败原因 + 行数），绝不抛到界面层。"""

    stage: str
    ok: bool = True
    rows: int = 0
    detail: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        if self.ok:
            return self.detail or f"{self.stage}完成（{self.rows} 行）"
        return f"{self.stage}失败：{self.error}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "ok": self.ok, "rows": self.rows,
            "detail": self.detail, "error": self.error, **self.extra,
        }

    def __bool__(self) -> bool:  # 便于 `if result:`
        return self.ok


def _note(note_cb: NoteCb | None, text: str) -> None:
    """状态回调（"正在重签 URL 继续下载（第 2 次）"这类）——回调出错不该影响下载。"""
    if note_cb is None:
        return
    try:
        note_cb(str(text))
    except Exception:  # noqa: BLE001 - 显示层的问题不能弄崩下载
        pass


def _notify(progress_cb: ProgressCb | None, stage: str, done: int, total: int) -> None:
    """调用进度回调；回调本身出错**不能**影响下载（UI 回调最容易踩到）。"""
    if progress_cb is None:
        return
    try:
        progress_cb(stage, int(done), int(total))
    except Exception:  # noqa: BLE001
        logger.debug("进度回调异常，已忽略", exc_info=True)


def make_client(cfg: Config | None = None, **kwargs: Any) -> hx.HithinkClient:
    """按配置构造客户端（Key 从环境变量 → config.toml）。

    Raises:
        hx.HithinkAuthError: 未配置 Key。调用方应捕获并转成 SyncResult。
    """
    cfg = cfg or get_config()
    # 注意传的是**字符串本身**（可能是 ""）：显式配置说了没有 Key，就不要再去
    # 环境变量/别的配置对象里找（见 HithinkClient.__init__ 的说明）
    return hx.HithinkClient(api_key=cfg.hithink_api_key, **kwargs)


# ── 复权计算（移植自 sequoia_x/data/dump_import.py）──


def adjust_ratio(
    prev_close: float,
    dividend: float = 0.0,
    bonus: float = 0.0,
    allotment_ratio: float = 0.0,
    allotment_price: float = 0.0,
) -> float | None:
    """单个复权事件的累乘因子 k（后复权）。

    k = 前收 × (1 + 送股 + 配股) / (前收 - 现金分红 + 配股×配股价)

    为什么单独抽成一个函数：服务器版把它内联在 `compute_adjust_factors` 的循环里，
    这里抽出来是为了让**单元测试能直接验证这条公式**（测试不依赖 pandas 大表）。
    计算本身一字未改，仍然是"分母 ≤ 0 或结果非有限/非正 → 丢弃该事件"。

    Returns:
        k，或 None（该事件不可用，应跳过）。
    """
    import math

    prev = float(prev_close or 0.0)
    if not math.isfinite(prev) or prev <= 0:
        return None
    d = float(dividend or 0.0)
    s = float(bonus or 0.0)
    r = float(allotment_ratio or 0.0)
    p = float(allotment_price or 0.0)
    denominator = prev - d + r * p
    if denominator <= 0:
        return None
    k = prev * (1.0 + s + r) / denominator
    if not math.isfinite(k) or k <= 0:
        return None
    return k


#: 日K dump 里需要的列（列裁剪本身就能省一大半内存）
RAW_COLUMNS: tuple[str, ...] = (
    "thscode", "date_ms", "open_price", "high_price", "low_price", "close_price",
    "volume", "turnover",
)


#: 内部列名 → dump 里的列名
_RAW_SOURCE_COLUMNS: dict[str, str] = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
    "volume": "volume",
    "turnover": "turnover",
}

#: 默认要的行情列
DEFAULT_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "turnover")


def load_raw(
    path: Path,
    since: str | None = None,
    columns: tuple[str, ...] | None = None,
) -> Any:
    """读取日K dump，转成项目内部结构（裸 6 位代码 + YYYY-MM-DD）。

    Args:
        since: 只要 `date >= since`（`YYYY-MM-DD`）的行；None = 全部。
            **先在 Arrow 层按列 + 日期过滤，再转 pandas** ——
            10 年全市场 1028 万行直接进 pandas 峰值约 2GB，先过滤能省掉大部分，
            分发给别人（默认只导入 5 年）时尤其明显。
        columns: 只要这些行情列（默认 OHLCV+成交额）。
            算复权因子只需要 `close`，这里传 `("close",)` 能把内存再压一大截。

    Returns:
        DataFrame[symbol, date, *columns]
    """
    import pandas as pd

    wanted = tuple(columns) if columns else DEFAULT_PRICE_COLUMNS
    read_columns = ["thscode", "date_ms"] + [_RAW_SOURCE_COLUMNS[c] for c in wanted]
    cutoff_ms = hx.date_to_ms(since) if since else None

    frame = None
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(
            path,
            columns=read_columns,
            filters=[("date_ms", ">=", cutoff_ms)] if cutoff_ms is not None else None,
        )
        frame = table.to_pandas()
        del table
    except ImportError:  # pragma: no cover - 没有 pyarrow 时退回 pandas（更慢更吃内存）
        frame = pd.read_parquet(path, columns=read_columns)
        if cutoff_ms is not None:
            frame = frame[frame["date_ms"] >= cutoff_ms].reset_index(drop=True)
    if frame is None:  # pragma: no cover
        frame = pd.read_parquet(path, columns=read_columns)

    frame["symbol"] = frame["thscode"].map(lambda t: hx.to_local_symbol(str(t)))
    frame["date"] = frame["date_ms"].map(hx.ms_to_date)
    frame = frame.rename(columns={v: k for k, v in _RAW_SOURCE_COLUMNS.items()})
    return frame[["symbol", "date", *wanted]]


def load_events(path: Path) -> Any:
    """读取复权事件 dump。"""
    import pandas as pd

    frame = pd.read_parquet(path)
    frame["symbol"] = frame["thscode"].map(lambda t: hx.to_local_symbol(str(t)))
    frame["ex_date"] = frame["ex_date_ms"].map(hx.ms_to_date)
    for col in ("dividend_per_share", "per_share_bonus", "allotment_ratio", "allotment_price"):
        if col not in frame.columns:
            frame[col] = 0.0
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    return frame[
        ["symbol", "ex_date", "dividend_per_share", "per_share_bonus", "allotment_ratio",
         "allotment_price"]
    ]


def compute_adjust_factors(raw: Any, events: Any) -> Any:
    """由复权事件算出每只股票在每个除权日的累计复权因子 k（后复权用）。

    移植自 `sequoia_x/data/dump_import.py::compute_adjust_factors`，逐行一致。

    Returns:
        DataFrame[symbol, ex_date, k]
    """
    import numpy as np
    import pandas as pd

    if events is None or len(events) == 0:
        return pd.DataFrame(columns=["symbol", "ex_date", "k"])

    # 每只股票的前收：用 (symbol, date) 排序后的 searchsorted 找"除权日之前最后一个交易日"
    raw_sorted = raw.sort_values(["symbol", "date"])
    closes = raw_sorted.set_index(["symbol", "date"])["close"]

    rows = []
    for symbol, group in events.groupby("symbol", sort=False):
        try:
            series = closes.loc[symbol]
        except KeyError:
            continue  # 事件有、行情没有（退市/久远），跳过
        dates = series.index.to_numpy()
        values = series.to_numpy()
        for event in group.sort_values("ex_date").itertuples(index=False):
            pos = int(np.searchsorted(dates, event.ex_date, side="left")) - 1
            if pos < 0 or not np.isfinite(values[pos]) or values[pos] <= 0:
                continue
            k = adjust_ratio(
                float(values[pos]),
                event.dividend_per_share,
                event.per_share_bonus,
                event.allotment_ratio,
                event.allotment_price,
            )
            if k is None:
                continue
            rows.append((symbol, event.ex_date, k))
    return pd.DataFrame(rows, columns=["symbol", "ex_date", "k"])


def cumulative_factor(raw: Any, factors: Any) -> Any:
    """给原始行情表加上 `factor` 列（后复权累乘因子，逐股累乘）。

    这是服务器版 `apply_factors` 里"算因子"的那一半（numpy searchsorted + cumprod），
    原样抽出：桌面版把因子存进库、由视图乘出后复权价，所以这里**不乘价格**。

    排序**必须**保留（服务器版 apply_factors 开头就是 `sort_values(["symbol","date"])`）：
    下面的 searchsorted 要求每只股票的日期升序。
    """
    import numpy as np

    frame = raw.sort_values(["symbol", "date"]).copy()
    frame["factor"] = 1.0
    if factors is None or len(factors) == 0:
        return frame

    factors = factors.sort_values(["symbol", "ex_date"])
    for symbol, group in factors.groupby("symbol", sort=False):
        mask = frame["symbol"] == symbol
        if not mask.any():
            continue
        dates = frame.loc[mask, "date"].to_numpy()
        ex_dates = group["ex_date"].to_numpy()
        ks = group["k"].to_numpy()
        # 每个交易日：累乘所有 ex_date <= date 的 k
        idx = np.searchsorted(ex_dates, dates, side="right")
        cumulative = np.cumprod(ks)
        frame.loc[mask, "factor"] = np.where(idx > 0, cumulative[np.maximum(idx - 1, 0)], 1.0)
    return frame


def apply_factors(raw: Any, factors: Any) -> Any:
    """把累计因子作用到原始价上，得到后复权价（最早的价格保持真实）。

    移植自服务器版同名函数（保留 `factor` 列，价格同时乘上因子）。
    """
    frame = cumulative_factor(raw, factors)
    for col in ("open", "high", "low", "close"):
        frame[col] = frame[col] * frame["factor"]
    return frame


# ── dump 下载与解析 ──


def dump_summary(path: Path) -> dict:
    """用 Parquet 元数据报告规模（不加载数据，秒级）。"""
    try:
        import pyarrow.parquet as pq

        meta = pq.ParquetFile(path).metadata
        return {"rows": meta.num_rows, "row_groups": meta.num_row_groups,
                "columns": meta.num_columns}
    except Exception as exc:  # noqa: BLE001 - 只看规模，失败不该中断
        import os

        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        return {"rows": 0, "row_groups": 0, "columns": 0, "error": str(exc), "bytes": size}


def dump_is_usable(path: Path, *, min_rows: int = 1) -> bool:
    """已存在的 dump 是否可用（下载中断会留下残缺文件，必须校验）。

    为什么要校验：dump 有几百 MB，下载中途断线时文件仍然存在，
    直接复用会得到"看起来成功、其实只有一半股票"的库 —— 这比报错更危险。
    """
    return hx.parquet_ok(path, min_rows=min_rows)


def download_dump(
    cfg: Config,
    tag: str,
    client: hx.HithinkClient,
    *,
    force: bool = False,
    progress_cb: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
    note_cb: NoteCb | None = None,
) -> Path:
    """下载（或复用）一个 dump 到 `<data_dir>/dumps/<tag>.parquet`。

    复用已下好的文件是为了**可中断续传**：几亿字节的重下代价太高。
    真正下载交给客户端的 `download_dump`（分块 + 断点续传 + URL 过期自动重签），
    这里只负责：选目标路径、把字节进度翻译成 `(stage, done, total)` 给界面、
    以及下载后再校验一次。

    Args:
        progress_cb: `(stage, done, total)`，`done/total` 是**字节**（显示层格式化成 MB）。
        note_cb: 一句话状态回调（"正在重签 URL 继续下载（第 2 次）"这类），
            给界面/命令行的"状态"而不是"进度"。

    Raises:
        hx.DumpDownloadError: 重试耗尽（含已下载字节数与建议）。
    """
    cfg.ensure_dirs()
    target = cfg.dump_dir / f"{tag}.parquet"
    if not force and dump_is_usable(target):
        logger.info(f"复用已下载的 dump：{target}（{dump_summary(target)}）")
        _note(note_cb, f"{tag}：复用已下好的 dump（{dump_summary(target)}）")
        return target

    def _bytes_progress(done: int, total: int) -> None:
        # 界面上的进度条吃 (stage, done, total)：stage 只写"在干什么"，
        # 字节数交给显示层格式化成 MB（否则同一条信息会重复两遍）
        if progress_cb is None:
            return
        progress_cb(f"下载 {tag}", int(done), int(total or done))

    with state.download_scope():
        client.download_dump(
            tag,
            dest=target,
            progress_cb=_bytes_progress,
            note_cb=note_cb,
            max_attempts=int(getattr(cfg, "download_max_attempts", 5) or 5),
            connect_timeout=float(getattr(cfg, "download_connect_timeout", 15) or 15),
            read_timeout=float(getattr(cfg, "download_read_timeout", 90) or 90),
            should_stop=should_stop,
        )
    if not dump_is_usable(target):
        raise hx.HithinkError(-1, f"下载的 dump 不可用（文件损坏或被截断）：{target}", tag)
    return target


# ── 主流程：下载历史 ──


def download_history(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    progress_cb: ProgressCb | None = None,
    *,
    symbol_limit: int | None = None,
    force_download: bool = False,
    include_names: bool = True,
    should_stop: Callable[[], bool] | None = None,
    note_cb: NoteCb | None = None,
) -> SyncResult:
    """下载 10 年全市场历史 + 复权事件，算后复权因子并入库（可中断续传）。

    Args:
        cfg: 配置（默认全局）。
        client: 注入的客户端（测试用假客户端）。
        progress_cb: `(stage, done, total)` 进度回调，供界面显示进度条。
        symbol_limit: 只处理前 N 只股票（试跑/自检用）。
        force_download: 忽略已下好的 dump，强制重下。
        include_names: 顺带拉一次股票名称表（建池要显示中文名）。
        should_stop: 协作式取消回调（界面向导的"取消"按钮）。返回真时在**阶段边界**
            停止并返回失败结果 —— 已下好的 dump 与已写入的行都会保留，
            下次点"开始下载"就是**续传**，不会从头再来。
        note_cb: 状态回调（"正在重签 URL 继续下载（第 2 次）"这类中文状态）。

    Returns:
        SyncResult（`rows` = 本次写入的行情行数，`extra` 含跳过行数等）。

    Note:
        整个下载期间 `laoa_trader.state.is_downloading()` 为真 —— 调度线程据此
        **跳过自动选股**，避免在只写了一半的库上跑策略（见 `state` 模块说明）。
    """
    cfg = cfg or get_config()
    result = SyncResult(stage="下载历史数据")
    with state.download_scope():
        return _download_history_inner(
            cfg, client, progress_cb, result,
            symbol_limit=symbol_limit, force_download=force_download,
            include_names=include_names, should_stop=should_stop, note_cb=note_cb,
        )


def _download_history_inner(
    cfg: Config,
    client: hx.HithinkClient | None,
    progress_cb: ProgressCb | None,
    result: SyncResult,
    *,
    symbol_limit: int | None = None,
    force_download: bool = False,
    include_names: bool = True,
    should_stop: Callable[[], bool] | None = None,
    note_cb: NoteCb | None = None,
) -> SyncResult:
    """`download_history` 的实现体（拆出来只为让"下载中"这面旗包住整个流程）。"""
    try:
        cfg.ensure_dirs()
        client = client or make_client(cfg)
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - 任何意外都不该崩界面
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        import pandas as pd  # noqa: F401 - 尽早暴露"缺 pandas"这种环境问题
    except ImportError as exc:
        result.ok = False
        result.error = f"缺少 pandas：{exc}"
        return result

    def _cancelled() -> bool:
        """用户点了取消？（只在阶段边界/批次边界检查，保证不会留下半个文件）"""
        return bool(should_stop and should_stop())

    try:
        if _cancelled():
            result.ok = False
            result.error = "已取消（已下好的 dump 与已写入的数据都保留，下次继续即可）"
            return result

        # 1) 下载两个 dump（各带预签名 URL，拿到就下）
        _note(note_cb, "正在下载全市场日K（约 180 MB，可中断续传）…")
        raw_path = download_dump(cfg, "daily-k", client, force=force_download,
                                 progress_cb=progress_cb, should_stop=should_stop,
                                 note_cb=note_cb)
        _note(note_cb, "正在下载复权事件…")
        event_path = download_dump(cfg, "adjustment-factors", client, force=force_download,
                                   progress_cb=progress_cb, should_stop=should_stop,
                                   note_cb=note_cb)

        # 2) 解析（**导入按 history_years 过滤**；dump 本身仍是整个 10 年，没法只下 5 年）
        years = float(getattr(cfg, "history_years", DEFAULT_HISTORY_YEARS) or 0)
        cutoff = ""
        if years > 0:
            cutoff = (datetime.now() - timedelta(days=round(years * DAYS_PER_YEAR))
                      ).strftime("%Y-%m-%d")
        _notify(progress_cb, f"解析日K dump（保留 {years:g} 年）", 0, 1)
        raw = load_raw(raw_path, since=cutoff or None)
        raw_total = int(dump_summary(raw_path).get("rows") or 0)
        events = load_events(event_path)
        if len(raw) == 0:
            result.ok = False
            result.error = (
                "日K dump 为空（远端数据未就绪？）"
                if not cutoff else
                f"按 history_years={years:g} 过滤后没有数据（起点 {cutoff}）："
                "确认 dump 里有这个区间，或把 config.toml 的 history_years 调大"
            )
            return result
        if symbol_limit:
            keep = sorted(raw["symbol"].unique())[: int(symbol_limit)]
            raw = raw[raw["symbol"].isin(keep)]
            events = events[events["symbol"].isin(keep)]
            logger.info(f"试跑模式：只处理 {len(keep)} 只股票、{len(raw)} 行")
        _notify(progress_cb, f"解析日K dump（保留 {years:g} 年）", 1, 1)

        # 3) 算复权因子
        #
        # **关键**：因子必须用**全量历史**算，不能只用窗口内的行 ——
        # 除权日的"前收"取自除权日之前最后一个交易日；窗口起点的除权事件，
        # 前收落在窗口之外。只用窗口内数据算，那些事件会被整条丢掉，
        # 于是整段序列相对真实后复权价差一个常数（收益率不受影响，但价格全错）。
        #
        # 省内存的做法：算因子只需要 (symbol, date, close) 三列 + 全量复权事件，
        # 所以单独读一份"瘦"数据算完就释放，窗口内的 8 列数据另读一份。
        _notify(progress_cb, "计算后复权因子", 0, 1)
        factor_input = load_raw(raw_path, columns=("close",))
        events_all = load_events(event_path)
        factors = compute_adjust_factors(factor_input, events_all)
        del factor_input
        adjusted = cumulative_factor(raw, factors)
        # 全市场 10 年约 1000 万行 × float64 ≈ 1GB，两份同时在内存里会翻倍；
        # 算完因子后窗口内的原始表也没用了，显式释放（Windows 上内存吃紧时很关键）
        del raw
        _notify(progress_cb, "计算后复权因子", 1, 1)
        logger.info(f"复权事件 {len(events)} 条 → 有效因子 {len(factors)} 条")

        # 4) 落库（复权事件全量 upsert；行情按 (symbol,date) 跳过已入库的）
        with storage.connect(cfg.db_path, timeout=300) as conn:
            storage.write_adjust_events(conn, events.itertuples(index=False, name=None))

            total = len(adjusted)
            done = 0
            written = 0
            skipped = 0
            buffer: list[tuple] = []
            # 5000 只股票逐只回调会把界面刷爆，这里按"每 0.5% 或每 5 万行"汇报一次
            step = max(total // 200, 5000)
            last_report = 0
            # 续传判断分片进行：一次只查 200 只股票已在库的日期（见 storage.dates_for_symbols）
            pending: list[tuple[str, Any]] = []

            def _flush(pending: list[tuple[str, Any]]) -> None:
                nonlocal written, skipped, done, last_report
                if not pending:
                    return
                have_map = storage.dates_for_symbols(conn, [s for s, _ in pending])
                for symbol, group in pending:
                    have = have_map.get(symbol)
                    sym_skipped = 0
                    if have:
                        mask = ~group["date"].isin(have)
                        sym_skipped = int((~mask).sum())
                        group = group[mask]
                    skipped += sym_skipped
                    done += len(group) + sym_skipped
                    if len(group):
                        buffer.extend(
                            group[["symbol", "date", "open", "high", "low", "close", "volume",
                                   "turnover", "factor"]].itertuples(index=False, name=None)
                        )
                    if len(buffer) >= 50_000:
                        written += storage.write_daily_raw(conn, buffer)
                        buffer.clear()
                        if _cancelled():
                            raise _Cancelled()
                    if done - last_report >= step:
                        _notify(progress_cb, "写入行情", done, total)
                        last_report = done
                pending.clear()

            for symbol, group in adjusted.groupby("symbol", sort=False):
                pending.append((symbol, group))
                if len(pending) >= 200:
                    _flush(pending)
            _flush(pending)
            if buffer:
                written += storage.write_daily_raw(conn, buffer)
            _notify(progress_cb, "写入行情", total, total)

        result.rows = written
        start_date = min(adjusted["date"]) if total else ""
        end_date = max(adjusted["date"]) if total else ""
        result.extra = {
            "skipped": skipped,
            "raw_rows": total,                  # 过滤后参与导入的行数
            "dump_rows": raw_total,             # dump 原始行数（约 1028 万）
            "history_years": years,
            "cutoff": cutoff,
            "start": start_date,
            "end": end_date,
            "events": int(len(events)),
            "factors": int(len(factors)),
        }
        if cutoff:
            result.detail = (
                f"本次导入 {years:g} 年：{start_date} → {end_date}，共 {total:,} 行"
                f"（原始 dump {raw_total:,} 行，已按 history_years 过滤）；"
                f"写入 {written} 行、跳过已入库 {skipped} 行；"
                f"复权事件 {len(events)} 条（**全部保留**，含窗口之前的除权）"
                f" → 有效因子 {len(factors)} 条"
            )
        else:
            result.detail = (
                f"写入行情 {written} 行、跳过已入库 {skipped} 行；"
                f"复权事件 {len(events)} 条 → 有效因子 {len(factors)} 条"
            )

        # 5) 顺带更新股票名称（失败不算整体失败：名字只影响展示）
        if include_names:
            name_result = sync_stock_names(cfg, client=client)
            result.extra["names"] = name_result.rows
            if not name_result.ok:
                logger.warning(f"股票名称同步失败（不影响行情）：{name_result.message}")
        return result

    except (hx.DownloadCancelled, _Cancelled):
        result.ok = False
        result.error = "已取消（已下好的 dump 与已写入的数据都保留，下次继续即可续传）"
        logger.info("下载历史数据：用户取消")
    except hx.HithinkError as exc:
        result.ok = False
        result.error = f"接口错误 {exc}"
        logger.warning(f"下载历史数据失败：{exc}")
    except MemoryError:
        result.ok = False
        result.error = "内存不足：历史数据较大，请关闭其它程序后重试（已入库的部分不会丢失）"
    except Exception as exc:  # noqa: BLE001 - 兜底：绝不抛到界面层
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("下载历史数据异常")
    return result


# ── 增量日更 ──


def _base_factors(conn: sqlite3.Connection) -> dict[str, tuple[str, float]]:
    """每只股票**库里最新一行**的 (日期, factor) —— 增量计算的起点。

    为什么需要：后复权因子是"逐股累乘"的。日更只拿到最近 10 个交易日，
    不能从头重算，必须用库里已累计的 factor 作为基数继续累乘。
    """
    rows = conn.execute(
        "SELECT r.symbol, r.date, r.factor FROM stock_daily_raw r "
        "JOIN (SELECT symbol, MAX(date) AS d FROM stock_daily_raw GROUP BY symbol) m "
        "  ON m.symbol = r.symbol AND m.d = r.date"
    ).fetchall()
    return {r[0]: (r[1], float(r[2] if r[2] is not None else 1.0)) for r in rows}


def _recent_closes(conn: sqlite3.Connection, since: str) -> dict[str, list[tuple[str, float]]]:
    """[since, ∞) 的收盘价序列（按股票分组，日期升序）—— 用于确定除权日的"前收"。"""
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol, date, close in conn.execute(
        "SELECT symbol, date, close FROM stock_daily_raw WHERE date >= ? "
        "ORDER BY symbol, date",
        (since,),
    ):
        out.setdefault(symbol, []).append((date, float(close or 0.0)))
    return out


def _incremental_factors(
    conn: sqlite3.Connection,
    new_raw: Any,
    events: Any,
) -> tuple[Any, int]:
    """给增量行算 factor（在库内基数上继续累乘），返回 (带 factor 的 frame, 漏算事件数)。

    算法：
        1. 取每只股票库内最新一行的 factor 作为基数（新股票基数 = 1.0）；
        2. 对窗口内的复权事件，用"库内近段收盘价 + 本次新行"拼出序列找前收，算 k；
        3. 新行 factor = 基数 × ∏(ex_date ≤ 该行的 k)。

    "漏算事件"（`missed`）：除权日落在 **(库内最后一天, 本次窗口第一天)** 之间的事件。
    这一段既不在基数里（基数只覆盖到库内最后一天），又没有对应的新行可承载因子 ——
    典型场景是日更中断了几天后补数据，中间那天发生了一次分红。
    这时**不擅自重写历史**（会动到几十万行），只把数量报出来，提示重跑全量下载。
    早于库内最后一天的事件则视为"已经算进基数"，不计入（否则每天都会误报）。
    """
    import numpy as np
    import pandas as pd

    base = _base_factors(conn)
    if new_raw is None or len(new_raw) == 0:
        frame = new_raw.copy() if new_raw is not None else pd.DataFrame()
        if len(frame):
            frame["factor"] = 1.0
        return frame, 0

    window_start = str(new_raw["date"].min())
    # 前收要往前多取一段：除权日正好是窗口第一天时，前收落在库里
    since = (datetime.strptime(window_start, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")
    prior = _recent_closes(conn, since)

    events_by_symbol: dict[str, Any] = {}
    missed = 0
    if events is not None and len(events):
        for symbol, group in events.groupby("symbol", sort=False):
            last = base.get(symbol)
            if last is not None:
                # 落在"库内最后一天之后、本次窗口之前"的事件：基数没算它、新行也承载不了
                missed += int(((group["ex_date"] > last[0]) & (group["ex_date"] < window_start)).sum())
            recent = group[group["ex_date"] >= window_start]
            if len(recent):
                events_by_symbol[symbol] = recent.sort_values("ex_date")

    frame = new_raw.copy()
    frame["factor"] = 1.0
    for symbol, group in frame.groupby("symbol", sort=False):
        base_factor = base.get(symbol, (None, 1.0))[1]
        symbol_events = events_by_symbol.get(symbol)
        if symbol_events is None or len(symbol_events) == 0:
            frame.loc[group.index, "factor"] = base_factor
            continue

        # 合并序列：库内已有的近期收盘 + 本次新行（新行覆盖同日的旧值）
        merged: dict[str, float] = dict(prior.get(symbol, []))
        for date, close in zip(group["date"], group["close"], strict=False):
            merged[str(date)] = float(close or 0.0)
        dates = np.array(sorted(merged))
        closes = np.array([merged[d] for d in dates])

        ks: list[float] = []
        ex_dates: list[str] = []
        for event in symbol_events.itertuples(index=False):
            pos = int(np.searchsorted(dates, event.ex_date, side="left")) - 1
            if pos < 0 or not np.isfinite(closes[pos]) or closes[pos] <= 0:
                continue
            k = adjust_ratio(
                float(closes[pos]),
                event.dividend_per_share,
                event.per_share_bonus,
                event.allotment_ratio,
                event.allotment_price,
            )
            if k is None:
                continue
            ks.append(k)
            ex_dates.append(event.ex_date)

        row_dates = group["date"].to_numpy()
        if not ks:
            frame.loc[group.index, "factor"] = base_factor
            continue
        idx = np.searchsorted(np.array(ex_dates), row_dates, side="right")
        cumulative = np.cumprod(np.array(ks))
        frame.loc[group.index, "factor"] = base_factor * np.where(
            idx > 0, cumulative[np.maximum(idx - 1, 0)], 1.0
        )
    return frame, missed


def sync_daily(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    progress_cb: ProgressCb | None = None,
    *,
    day: str | None = None,
    include_calendar: bool = True,
    note_cb: NoteCb | None = None,
) -> SyncResult:
    """日更：`daily-k-10d` 增量行情 + 当日涨停池（不做估值）。

    Args:
        day: 涨停池日期（默认最近交易日/今天）。
        include_calendar: 顺带刷新交易日历（1 次请求，保证"今天是否开盘"准确）。

    Returns:
        SyncResult（`rows` = 写入的行情行数，`extra` 含涨停池行数等）。
    """
    cfg = cfg or get_config()
    result = SyncResult(stage="日更数据")
    try:
        cfg.ensure_dirs()
        client = client or make_client(cfg)
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        # 1) 近 10 交易日全市场日K（体量小，每次强制重下，避免拿到过期的缓存）
        raw_path = download_dump(cfg, "daily-k-10d", client, force=True,
                                 progress_cb=progress_cb, note_cb=note_cb)
        raw = load_raw(raw_path)
        if len(raw) == 0:
            result.ok = False
            result.error = "daily-k-10d dump 为空"
            return result

        event_path = download_dump(cfg, "adjustment-factors", client, force=True,
                                   progress_cb=progress_cb)
        events = load_events(event_path)

        with storage.connect(cfg.db_path, timeout=300) as conn:
            # 只查"本次 dump 里出现过的股票"已有哪些日期（10 日窗口 ≈ 5 万行，不整库载入）
            existing = storage.dates_for_symbols(
                conn, raw["symbol"].unique().tolist()
            )
            # 2) 过滤出真正的新行（续传/幂等：已入库的 (symbol,date) 跳过）
            keep_index = []
            for i, (symbol, date) in enumerate(zip(raw["symbol"], raw["date"], strict=False)):
                if date not in existing.get(symbol, ()):
                    keep_index.append(i)
            new_raw = raw.iloc[keep_index] if keep_index else raw.iloc[0:0]
            skipped = len(raw) - len(new_raw)
            logger.info(f"日更：需写入 {len(new_raw)} 行（跳过已入库 {skipped} 行）")

            written = 0
            missed = 0
            if len(new_raw):
                _notify(progress_cb, "计算增量复权因子", 0, 1)
                adjusted, missed = _incremental_factors(conn, new_raw, events)
                _notify(progress_cb, "计算增量复权因子", 1, 1)
                rows = adjusted[["symbol", "date", "open", "high", "low", "close", "volume",
                                 "turnover", "factor"]].itertuples(index=False, name=None)
                written = storage.write_daily_raw(conn, rows)
                _notify(progress_cb, "写入行情", written, len(new_raw))

            # 3) 涨停池（策略/热门行业都依赖它）
            _notify(progress_cb, "同步涨停池", 0, 1)
            limit_day = day or storage.latest_date(conn) or datetime.now().strftime("%Y-%m-%d")
            try:
                limit_rows = sync_limit_up_pool(cfg, client=client, day=limit_day, conn=conn)
                limit_ok, limit_n, limit_err = limit_rows.ok, limit_rows.rows, limit_rows.error
            except Exception as exc:  # noqa: BLE001 - 涨停池失败不影响行情
                limit_ok, limit_n, limit_err = False, 0, str(exc)
            _notify(progress_cb, "同步涨停池", 1, 1)

        result.rows = written
        result.extra = {
            "skipped": skipped, "missed_events": missed,
            "limit_up": limit_n, "limit_up_day": limit_day, "limit_up_ok": limit_ok,
        }
        result.detail = f"写入行情 {written} 行（跳过 {skipped} 行），涨停池 {limit_n} 行（{limit_day}）"
        if missed:
            # 历史因子可能需要重算：只提醒，不擅自重写历史（那会动到几百万行）
            result.extra["hint"] = (
                f"有 {missed} 条复权事件的除权日落在数据缺口里（未被算进复权因子），"
                "建议重跑一次「下载/更新历史数据」做全量重算"
            )
            logger.warning(result.extra["hint"])
        if not limit_ok:
            result.detail += f"；涨停池同步失败（{limit_err}）"

        if include_calendar:
            cal = sync_calendar(cfg, client=client)
            result.extra["calendar"] = cal.rows
            if not cal.ok:
                logger.warning(f"交易日历同步失败：{cal.message}")
        return result

    except hx.HithinkError as exc:
        result.ok = False
        result.error = f"接口错误 {exc}"
        logger.warning(f"日更失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("日更异常")
    return result


# ── 交易日历 / 涨停池 ──


def sync_calendar(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
) -> SyncResult:
    """官方交易日历（近一年）落库。"""
    cfg = cfg or get_config()
    result = SyncResult(stage="交易日历")
    try:
        client = client or make_client(cfg)
        days = client.trading_days()
        if not days:
            result.ok = False
            result.error = "交易日历返回空"
            return result
        with storage.connect(cfg.db_path) as conn:
            result.rows = storage.write_calendar(conn, days)
        result.detail = f"{result.rows} 个交易日（{days[0]} → {days[-1]}）"
        return result
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"交易日历同步失败：{result.error}")
    return result


def _pick(row: dict, *names: str):
    """按顺序取第一个非空字段（官方/公开接口字段名不同）。"""
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    result = _as_float(value)
    return int(result) if result is not None else None


def _time_text(value) -> str | None:
    """涨停时间：接口给的是毫秒时间戳或 HH:MM:SS 文本，统一成文本。"""
    if value is None or value == "":
        return None
    text = str(value)
    if text.isdigit() and len(text) >= 10:
        return hx.ms_to_date(int(text))
    return text


def limit_up_row(day: str, r: dict) -> tuple | None:
    """把一条涨停池记录转成 `limit_up_pool` 的行（字段别名兼容，逐条移植）。

    Returns:
        行元组（顺序见 `storage.LIMIT_UP_COLUMNS`），或 None（无代码，跳过）。
    """
    code = _pick(r, "thscode", "ticker", "code")
    if not code:
        return None
    symbol = hx.to_local_symbol(str(code)) if "." in str(code) else str(code).zfill(6)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        day,
        symbol,
        r.get("name"),
        _as_int(_pick(r, "continue_day_cnt", "high_days")),
        r.get("limit_up_type"),
        _time_text(_pick(r, "first_limit_up_time", "limit_up_time")),
        _time_text(_pick(r, "last_limit_up_time", "limit_up_time")),
        _as_float(_pick(r, "seal_money", "order_amount")),
        _as_int(r.get("open_num")),
        _pick(r, "limit_up_reason", "reason_type"),
        _as_float(r.get("turnover_rate")),
        _as_float(_pick(r, "price_change_ratio_pct", "change_rate")),
        _as_float(r.get("currency_value")),
        _as_int(r.get("is_again_limit")),
        1 if r.get("is_new") in (True, 1, "1") else _as_int(r.get("is_new")),
        r.get("continue_day_text"),
        _as_float(r.get("max_seal_money")),
        _as_float(_pick(r, "last_price", "latest")),
        1 if r.get("is_st") is True else _as_int(r.get("is_st")),
        "hithink",
        now,
    )


def sync_limit_up_pool(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    day: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> SyncResult:
    """某交易日涨停池落库（连板数/封单额/涨停时间/原因）。"""
    cfg = cfg or get_config()
    result = SyncResult(stage="涨停池")
    try:
        client = client or make_client(cfg)
        day = day or datetime.now().strftime("%Y-%m-%d")
        rows = client.limit_up_pool(day)
        payload = [r for r in (limit_up_row(day, row) for row in rows) if r]
        if not payload:
            result.detail = f"{day} 无涨停股（非交易日或数据未就绪）"
            return result
        if conn is not None:
            result.rows = storage.write_limit_up_pool(conn, payload)
        else:
            with storage.connect(cfg.db_path) as own:
                result.rows = storage.write_limit_up_pool(own, payload)
        result.detail = f"{day} 涨停池 {result.rows} 行"
        return result
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"涨停池同步失败：{result.error}")
    return result


# ── 名称 / 行业 / 指数 ──


def sync_stock_names(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
) -> SyncResult:
    """拉取全市场代码表，写入 `stock_basic(symbol, name)`。

    名称只用于展示（池子、推送卡片），所以失败**不影响**数据流程。
    """
    cfg = cfg or get_config()
    result = SyncResult(stage="股票名称")
    try:
        client = client or make_client(cfg)
        items = client.tickers()
        rows = []
        for item in items:
            code = str(item.get("thscode") or "")
            if not code:
                continue
            try:
                symbol = hx.to_local_symbol(code)
            except ValueError:
                continue
            rows.append((symbol, str(item.get("name") or "").strip() or None, None))
        if not rows:
            result.ok = False
            result.error = "代码表返回空"
            return result
        with storage.connect(cfg.db_path) as conn:
            result.rows = storage.write_stock_basic(conn, rows, industry_only=True)
        result.detail = f"名称 {result.rows} 只"
        return result
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"股票名称同步失败：{result.error}")
    return result


def industry_map_is_fresh(
    db_path: str | Path, days: int = 7, min_coverage: float = 0.9
) -> bool:
    """行业映射是否足够新：**覆盖率 ≥90% 且 7 天内更新过**。

    只看时间是不够的：覆盖率不足的映射"更新时间"也很新，会把
    "行业字段大面积缺失"误判成"刚更新过"。
    """
    try:
        with storage.connect(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(updated_at), COUNT(*) FROM stock_basic "
                "WHERE industry IS NOT NULL AND industry != ''"
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily_raw"
            ).fetchone()[0]
    except sqlite3.Error:
        return False
    if not row or not row[0] or not row[1] or not total:
        return False
    if row[1] / total < min_coverage:
        return False
    try:
        last = datetime.fromisoformat(str(row[0]))
    except ValueError:
        return False
    return (datetime.now() - last).days < days


def sync_industry(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    *,
    force: bool = False,
) -> SyncResult:
    """同花顺行业目录 + 成分 → `stock_basic.industry`（只取 881* 一级行业）。

    为什么要挑 881 开头：目录里混了三个层级（881xxx 一级约 90 个、884xxx 二三级）。
    `industry` 是粗分类（热门行业过滤用它做分组），320 个细分类会造成
    "每个行业只有几只股票"的碎片化 —— 与服务器版口径一致，只取一级行业。
    """
    cfg = cfg or get_config()
    result = SyncResult(stage="行业归属")
    try:
        if not force and industry_map_is_fresh(cfg.db_path):
            result.detail = "行业映射仍在有效期内（<7 天且覆盖率 ≥90%），跳过"
            result.extra["skipped"] = True
            return result
        client = client or make_client(cfg)
        boards = client.ths_index_list("industry")
        if not boards:
            result.ok = False
            result.error = "同花顺行业目录为空"
            return result
        level1 = [b for b in boards if str(b.get("thscode") or "").startswith("881")]
        if not level1:
            level1 = boards  # 目录结构变化时退回全量，避免直接失效

        mapping: dict[str, tuple[str, str]] = {}
        failed = 0
        for board in level1:
            code = str(board.get("thscode") or "")
            name = str(board.get("name") or "").strip()
            if not code or not name:
                continue
            try:
                members = client.ths_constituents(code)
            except hx.HithinkError as exc:
                failed += 1
                logger.warning(f"行业 {name}（{code}）成分获取失败：{exc}")
                continue
            for row in members:
                thscode = str(row.get("thscode") or "")
                if not thscode:
                    continue
                try:
                    local = hx.to_local_symbol(thscode)
                except ValueError:
                    continue
                # 一只股票在多个一级行业时保留第一个（一级行业基本互斥）
                mapping.setdefault(local, (name, str(row.get("name") or "").strip()))

        if not mapping:
            result.ok = False
            result.error = "行业成分为空，未更新映射"
            return result

        with storage.connect(cfg.db_path) as conn:
            known = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily_raw")}
            rows = [
                (sym, stock_name or None, ind)
                for sym, (ind, stock_name) in mapping.items()
                if sym in known
            ]
            result.rows = storage.write_stock_basic(conn, rows, industry_only=True)
        result.extra = {"industries": len(level1), "mapped": result.rows, "failed": failed}
        result.detail = (
            f"一级行业 {len(level1)} 个 → 覆盖 {result.rows} 只有行情的股票"
            + (f"（{failed} 个板块取数失败）" if failed else "")
        )
        return result
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"行业归属同步失败：{result.error}")
    return result


def sync_index(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    *,
    indices: dict[str, str] | None = None,
    start: str = "2024-01-01",
) -> SyncResult:
    """指数日线落库（默认沪深300/上证/深成/创业板），增量续传。"""
    cfg = cfg or get_config()
    result = SyncResult(stage="指数日线")
    try:
        client = client or make_client(cfg)
        targets = indices or DEFAULT_INDICES
        end = datetime.now().strftime("%Y-%m-%d")
        written = 0
        failed = 0
        with storage.connect(cfg.db_path) as conn:
            for local, thscode in targets.items():
                # 增量：从库里已有的最后一天开始，避免每次全量重拉
                last = conn.execute(
                    "SELECT MAX(date) FROM index_daily WHERE symbol = ?", (local,)
                ).fetchone()[0]
                begin = last or start
                try:
                    bars = client.index_historical(thscode, begin, end)
                except hx.HithinkError as exc:
                    failed += 1
                    logger.warning(f"指数 {thscode} 取数失败：{exc}")
                    continue
                payload = [
                    (local, hx.ms_to_date(b.get("date_ms")), _as_float(b.get("open_price")),
                     _as_float(b.get("high_price")), _as_float(b.get("low_price")),
                     _as_float(b.get("close_price")), _as_float(b.get("volume")),
                     _as_float(b.get("turnover")))
                    for b in bars
                    if b.get("date_ms")
                ]
                if payload:
                    written += storage.write_index_daily(conn, payload)
        result.rows = written
        result.extra = {"failed": failed}
        result.detail = f"指数 {len(targets)} 个 → {written} 行"
        if failed and not written:
            # 全部指数都没拿到：这不是"成功但 0 行"，要在状态栏/结果里如实报错
            result.ok = False
            result.error = f"{failed} 个指数全部取数失败"
        return result
    except hx.HithinkError as exc:
        result.ok = False
        result.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"指数同步失败：{result.error}")
    return result


def sync_index_boards(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    *,
    start: str = "2026-01-01",
) -> SyncResult:
    """同花顺**一级行业指数**日线（881* 板块，落 `index_daily`，symbol 形如 `ti.881101`）。

    可选：热门行业过滤当前用的是"行业成分股等权涨幅"（见 pool.py），
    不需要行业指数也能算；这里提供接口是为了将来切到真正的行业指数动量。
    """
    cfg = cfg or get_config()
    result = SyncResult(stage="行业指数")
    try:
        client = client or make_client(cfg)
        end = datetime.now().strftime("%Y-%m-%d")
        boards = [
            b for b in client.ths_index_list("industry")
            if str(b.get("thscode") or "").startswith("881")
        ]
        written, failed = 0, 0
        with storage.connect(cfg.db_path) as conn:
            for board in boards:
                thscode = str(board.get("thscode") or "")
                local = f"ti.{thscode.split('.')[0]}"
                last = conn.execute(
                    "SELECT MAX(date) FROM index_daily WHERE symbol = ?", (local,)
                ).fetchone()[0]
                try:
                    bars = client.index_historical(thscode, last or start, end)
                except hx.HithinkError as exc:
                    failed += 1
                    logger.warning(f"行业指数 {thscode} 取数失败：{exc}")
                    continue
                rows = [
                    (local, hx.ms_to_date(b["date_ms"]), b.get("open_price"),
                     b.get("high_price"), b.get("low_price"), b.get("close_price"),
                     b.get("volume"), b.get("turnover"))
                    for b in bars if b.get("date_ms")
                ]
                if rows:
                    written += storage.write_index_daily(conn, rows)
        result.rows = written
        result.extra = {"boards": len(boards), "failed": failed}
        result.detail = f"{len(boards)} 个板块 → {written} 行"
        return result
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def daily_update(
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    progress_cb: ProgressCb | None = None,
) -> list[SyncResult]:
    """每天开机/定时跑的一组同步（逐项独立，某项失败不影响其它项）。

    Returns:
        各项的 SyncResult 列表。
    """
    cfg = cfg or get_config()
    results: list[SyncResult] = []
    try:
        client = client or make_client(cfg)
    except Exception as exc:  # noqa: BLE001 - 没 Key 时全部标记失败，界面照常可用
        failed = SyncResult(stage="日更数据", ok=False, error=str(exc))
        return [failed]

    results.append(sync_daily(cfg, client=client, progress_cb=progress_cb))
    for fn in (sync_calendar, sync_industry, sync_index):
        try:
            results.append(fn(cfg, client=client))
        except Exception as exc:  # noqa: BLE001
            results.append(SyncResult(stage=fn.__name__, ok=False,
                                      error=f"{type(exc).__name__}: {exc}"))
    return results


def iter_rows(frame: Any) -> Iterator[tuple]:
    """DataFrame → 行元组（供 storage 写入；避免调用方各自拼列顺序）。"""
    cols = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
    for row in frame[cols].itertuples(index=False, name=None):
        yield row
