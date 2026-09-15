"""运行时自检：本地数据到底能不能用？**三态判定**，纯本地、秒级、不联网。

为什么必须是三态
----------------
"有没有数据"这种二值判断会做出错误决定：

- 数据只落后 3 个交易日 → 跑一次 `daily-k-10d` 增量（**1 次请求**）就补齐了，
  却因为"库里已有数据"而不更新，策略就用 3 天前的价格选股；
- 数据落后 30 个交易日 → 增量 dump 只覆盖近 10 个交易日，**补不回来**
  （官方文档明确写了窗口，NAS 版也踩过这个坑），必须走 10 年全量。

所以判据分三态：

| 状态 | 判据 | 动作 |
|---|---|---|
| `ready` | 表齐全 + 跨度 ≥ `min_history_years` + 股票数 ≥ `min_symbols` + `adjust_event` 非空 + 行业覆盖 ≥ 90% + 日历非空 + 落后 ≤ `max_stale_trading_days` | **一次 dump 请求都不发** |
| `needs_incremental` | 以上都满足，但落后 1~10 个交易日 | 只跑 `daily-k-10d`（1 次请求） |
| `needs_full` | 缺表/空库/跨度不足/股票数不足/**缺复权事件**/行业覆盖低/缺日历/**落后 >10 个交易日** | 下载 10 年全量 |

为什么要查"复权事件非空"：`stock_daily_hfq` 视图是靠 `factor` 列乘出来的，而
factor 来自复权事件。事件表空了 → 所有价格都是"不复权"的，跨除权日的收益率会
凭空多出一截，**策略会选出完全错误的标的** —— 这种库不能算"就绪"。

为什么要查"行业覆盖 ≥90%"：热门行业过滤与行业分组都依赖 `stock_basic.industry`；
覆盖率低会让过滤失效（池子要么全空、要么全是漏网的），必须补。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from laoa_trader.config import Config, get_config
from laoa_trader import hints
from laoa_trader.data import storage
from laoa_trader.intraday import now_shanghai
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 三态
READY = "ready"
NEEDS_INCREMENTAL = "needs_incremental"
NEEDS_FULL = "needs_full"

#: 需要下载什么
DOWNLOAD_NONE = "none"
DOWNLOAD_INCREMENTAL = "incremental"
DOWNLOAD_FULL = "full"
#: **只缺轻量数据**（交易日历 / 行业归属 / 指数）：点一下【刷新数据】就能补上，
#: 完全不需要重下历史行情。为什么单独一个取值：以前这种情况一律给 `full`，
#: 界面/CLI 就照着"下载数据"指路 —— 用户实测被指去重下 180MB（实报 bug）。
DOWNLOAD_SYNC_LIGHT = "sync_light"

#: 行业覆盖率下限（低于它认为行业数据不可用）
MIN_INDUSTRY_COVERAGE = 0.9

#: 增量 dump（daily-k-10d）能覆盖的交易日数 —— **超过它增量补不回来，必须全量**
INCREMENTAL_WINDOW_DAYS = 10

#: 一年按多少天算（跨度用自然日近似，够判断"是不是 10 年库"）
DAYS_PER_YEAR = 365.25


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _scalar(conn, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _trading_days_after(day: str, count: int) -> list[str]:
    """`day` 之后的 count 个工作日（升序）—— 用来构造"数据落后 N 个交易日"。"""
    last = datetime.strptime(day, "%Y-%m-%d").date()
    out: list[str] = []
    cursor = last
    while len(out) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
    return out


def check(
    db_path: str | Path,
    cfg: Config | None = None,
    *,
    today: str | None = None,
) -> dict:
    """本地自检（**只读本地库，不发任何请求**），返回三态结论与各项指标。

    Args:
        db_path: SQLite 路径（不存在时按"空库"处理，会建库但不下载）。
        cfg: 配置（阈值来源：`min_history_years` / `min_symbols` /
            `max_stale_trading_days`）。
        today: 覆盖"今天"（测试用；默认取本机日期）。

    Returns:
        {
            "status": "ready" | "needs_incremental" | "needs_full",
            "reason": "中文原因",
            "stale_trading_days": 落后几个交易日,
            "rows": 行情行数, "symbols": 股票数, "span_years": 历史跨度(年),
            "coverage": 最新交易日行情覆盖率, "industry_coverage": 行业覆盖率,
            "has_adjust_events": bool, "has_calendar": bool,
            "latest_date": "YYYY-MM-DD" | None,
            "needs_download": "none" | "incremental" | "sync_light" | "full",
            "missing_tables": [...],
        }
    """
    cfg = cfg or get_config()
    min_years = float(getattr(cfg, "min_history_years", 2.5) or 0)
    # 配置写矛盾时**先说清楚**：否则 3 年的库会被永远判成"历史不足"，
    # 用户只会看到"又要重新下载"，根本猜不到是配置写错了
    linkage_problem = cfg.history_warning() if hasattr(cfg, "history_warning") else ""
    min_symbols = int(getattr(cfg, "min_symbols", 4000) or 0)
    max_stale = int(getattr(cfg, "max_stale_trading_days", 0) or 0)
    # "今天"必须与交易日历同一套时钟（北京时间）：本机日期在 UTC 机器上会差一天，
    # 落后的交易日天数就会算错（CI 上实测差 1 天 → 误判 needs_incremental / ready）
    today = today or now_shanghai().strftime("%Y-%m-%d")

    result: dict[str, Any] = {
        "status": NEEDS_FULL,
        "reason": "",
        "history_years": float(getattr(cfg, "history_years", 3) or 0),
        "min_history_years": min_years,
        "stale_trading_days": 0,
        "rows": 0,
        "symbols": 0,
        "span_years": 0.0,
        "coverage": 0.0,
        "industry_coverage": 0.0,
        "has_adjust_events": False,
        "has_calendar": False,
        "latest_date": None,
        "needs_download": DOWNLOAD_FULL,
        "missing_tables": [],
    }

    if linkage_problem:
        result["reason"] = linkage_problem
        result["config_problem"] = linkage_problem
        return result

    if not Path(db_path).exists():
        result["reason"] = "本地还没有数据库（首次运行）"
        result["missing_tables"] = sorted(storage.EXPECTED_TABLES)
        return result

    try:
        conn = storage.connect(db_path)
    except Exception as exc:  # noqa: BLE001 - 自检本身绝对不能抛（GUI 启动就走这条路）
        result["reason"] = (
            f"数据库文件无法读取（可能损坏或不是有效的 SQLite 文件）："
            f"{type(exc).__name__}: {exc}；建议删掉它重新下载"
        )
        return result
    try:
        tables = _table_names(conn)
        missing = sorted(storage.EXPECTED_TABLES - tables)
        result["missing_tables"] = missing
        if missing:
            result["reason"] = f"缺表：{'、'.join(missing)}"
            return result

        result["rows"] = int(_scalar(conn, "SELECT COUNT(*) FROM stock_daily_raw") or 0)
        if result["rows"] == 0:
            result["reason"] = "行情表是空的（还没下载过历史数据）"
            return result

        latest = _scalar(conn, "SELECT MAX(date) FROM stock_daily_raw")
        earliest = _scalar(conn, "SELECT MIN(date) FROM stock_daily_raw")
        result["latest_date"] = latest
        if earliest and latest:
            try:
                span_days = (datetime.strptime(latest, "%Y-%m-%d")
                             - datetime.strptime(earliest, "%Y-%m-%d")).days
                result["span_years"] = round(span_days / DAYS_PER_YEAR, 2)
            except ValueError:
                result["span_years"] = 0.0
        if result["span_years"] < min_years:
            result["reason"] = (
                f"历史跨度只有 {result['span_years']:.1f} 年（要求 ≥{min_years:g} 年），"
                f"最早 {earliest}；可调低 min_history_years，或把 history_years 调大后重下"
            )
            return result

        # 股票数取"最新交易日有行情的只数"：既是真实可交易范围，
        # 又能走 date 索引（全库 COUNT(DISTINCT symbol) 要扫上千万行，启动会卡）
        symbols = int(_scalar(
            conn, "SELECT COUNT(DISTINCT symbol) FROM stock_daily_raw WHERE date = ?",
            (latest,),
        ) or 0)
        result["symbols"] = symbols
        if symbols < min_symbols:
            result["reason"] = (
                f"最新交易日只有 {symbols} 只股票（要求 ≥{min_symbols}），"
                "可能只下了部分数据"
            )
            return result
        universe = int(_scalar(
            conn, "SELECT COUNT(DISTINCT symbol) FROM stock_daily_raw WHERE date = ?",
            (latest,),
        ) or 0)
        result["coverage"] = round(symbols / universe, 4) if universe else 0.0

        # 复权事件：空了 → 后复权价全是错的（详见模块开头说明）
        has_events = bool(_scalar(conn, "SELECT 1 FROM adjust_event LIMIT 1"))
        result["has_adjust_events"] = has_events
        if not has_events:
            result["reason"] = "缺少复权事件（分红/送股/配股），后复权价会算错，不能算就绪"
            return result

        # 行业覆盖率
        basic_total = int(_scalar(conn, "SELECT COUNT(*) FROM stock_basic") or 0)
        basic_industry = int(_scalar(
            conn, "SELECT COUNT(*) FROM stock_basic "
                  "WHERE industry IS NOT NULL AND industry != ''"
        ) or 0)
        industry_coverage = (basic_industry / basic_total) if basic_total else 0.0
        result["industry_coverage"] = round(industry_coverage, 4)
        if basic_total == 0 or industry_coverage < MIN_INDUSTRY_COVERAGE:
            # 这是"可以局部修复"的一种：行业归属是目录类数据，点【刷新数据】几秒钟就补上
            # （`sync_industry`：1 次目录 + 约 90 次成分请求），**不要**让用户重下 180MB 历史
            result["reason"] = (
                f"行业归属未同步（覆盖率 {industry_coverage:.0%}，要求 "
                f"≥{MIN_INDUSTRY_COVERAGE:.0%}）· {hints.SYNC_LIGHT_HINT}"
            )
            result["light_only"] = True
            result["needs_download"] = DOWNLOAD_SYNC_LIGHT
            return result

        # 交易日历：判断"落后几个交易日"必须靠它
        calendar_max = _scalar(conn, "SELECT MAX(date) FROM trading_calendar")
        result["has_calendar"] = bool(calendar_max)
        if not calendar_max:
            # 交易日历也是轻量项（1 个请求）：同样别指路去"重新下载"
            result["reason"] = (
                f"缺交易日历（无法判断数据是否新鲜）· {hints.SYNC_LIGHT_HINT}"
            )
            result["light_only"] = True
            result["needs_download"] = DOWNLOAD_SYNC_LIGHT
            return result

        # 落后几个交易日：日历里"晚于最新行情日、且不晚于今天"的天数
        stale = int(_scalar(
            conn,
            "SELECT COUNT(*) FROM trading_calendar WHERE date > ? AND date <= ?",
            (latest, today),
        ) or 0)
        result["stale_trading_days"] = stale

        if stale <= max_stale:
            result["status"] = READY
            result["needs_download"] = DOWNLOAD_NONE
            result["reason"] = f"本地数据就绪（最新 {latest}）"
            return result

        if stale <= INCREMENTAL_WINDOW_DAYS:
            result["status"] = NEEDS_INCREMENTAL
            result["needs_download"] = DOWNLOAD_INCREMENTAL
            result["reason"] = f"数据落后 {stale} 个交易日（最新 {latest}）"
            return result

        # 关键分支：落后超过增量窗口 → 增量补不回来，必须全量
        result["status"] = NEEDS_FULL
        result["needs_download"] = DOWNLOAD_FULL
        result["reason"] = (
            f"数据落后 {stale} 个交易日（最新 {latest}）—— "
            f"daily-k-10d 只覆盖近 {INCREMENTAL_WINDOW_DAYS} 个交易日，增量补不回来，需要全量重下"
        )
        return result
    finally:
        conn.close()


def imported_range(db_path: str | Path) -> dict:
    """库里实际导入的区间与行数（`--doctor` 与状态栏用）。"""
    if not Path(db_path).exists():
        return {"start": None, "end": None, "rows": 0, "span_years": 0.0}
    try:
        conn = storage.connect(db_path)
    except Exception:  # noqa: BLE001
        return {"start": None, "end": None, "rows": 0, "span_years": 0.0}
    try:
        start = _scalar(conn, "SELECT MIN(date) FROM stock_daily_raw")
        end = _scalar(conn, "SELECT MAX(date) FROM stock_daily_raw")
        rows = int(_scalar(conn, "SELECT COUNT(*) FROM stock_daily_raw") or 0)
    finally:
        conn.close()
    span = 0.0
    if start and end:
        try:
            span = round((datetime.strptime(end, "%Y-%m-%d")
                          - datetime.strptime(start, "%Y-%m-%d")).days / DAYS_PER_YEAR, 2)
        except ValueError:
            span = 0.0
    return {"start": start, "end": end, "rows": rows, "span_years": span}


def summary_line(result: dict) -> str:
    """一行中文结论（界面状态栏 / CLI 输出用）。"""
    status = result.get("status")
    if status == READY:
        return (f"本地数据就绪：{result.get('rows', 0):,} 行 / "
                f"最新 {result.get('latest_date')}")
    if needs_download(result) == DOWNLOAD_SYNC_LIGHT:
        # 行情是好的，只是缺轻量数据 —— 别说成"不可用"，更别说"要重新下载"
        return f"本地数据缺轻量项：{result.get('reason')}"
    if status == NEEDS_INCREMENTAL:
        return (f"本地数据落后 {result.get('stale_trading_days', 0)} 个交易日"
                f"（最新 {result.get('latest_date')}）——可增量更新，"
                f"当前结论基于 {result.get('stale_trading_days', 0)} 个交易日前的数据")
    return f"本地数据不可用：{result.get('reason') or '未知原因'}"


def needs_download(result: dict) -> str:
    """该下什么：`none` / `incremental` / `sync_light` / `full`。

    `sync_light` = 只缺轻量数据（点【刷新数据】即可），界面/CLI 的指路文案据此选按钮。
    """
    return result.get("needs_download") or DOWNLOAD_FULL


def ensure_ready(
    cfg: Config | None = None,
    *,
    auto_download: bool = False,
    today: str | None = None,
    progress_cb: Any = None,
    should_stop: Any = None,
    note_cb: Any = None,
) -> tuple[bool, dict, list]:
    """自检 + （按需）自动补数据，返回 `(是否可继续, 自检结果, 同步结果列表)`。

    自动补齐的规则（**保守**：宁可少下，不要擅自跑 20 分钟的全量）：

    - `ready` → 什么都不做，**一次请求都不发**；
    - `needs_incremental` → `auto_download` 或配置 `auto_download_on_start` 为真时
      跑一次增量（1 次请求）；
    - `sync_light`（只缺交易日历/行业归属/指数）→ **无条件**跑一次这三步再复查
      （轻量、幂等、不碰行情；把"为了一行业归属去重下 10 年"这条路彻底堵掉）；
    - `needs_full` → **只在 `auto_download` 显式为真时**才下载全量
      （全量是分钟级的大动作，必须由用户明确同意：界面点向导、CLI 加 `--auto-download`）。

    Args:
        auto_download: 调用方是否已明确同意下载（界面向导、CLI `--auto-download`）。
        progress_cb: 下载进度回调 `(stage, done, total)`。
        should_stop: 可调用对象，返回真表示用户取消了（用于界面取消按钮）。
        note_cb: 下载状态回调 `(一句话中文状态)`（"正在重签 URL 继续下载（第 2 次）"）。

    Returns:
        (proceed, result, sync_results)。`proceed=False` 时调用方应提示原因并停止，
        而不是"静默跑出空结果"。
    """
    cfg = cfg or get_config()
    result = check(cfg.db_path, cfg, today=today)
    logger.info(f"数据自检：{result['status']} —— {result['reason']}")
    if result.get("config_problem"):
        # 配置自相矛盾时**不要**下载：下完照样判不足，白等十几分钟
        logger.warning("配置矛盾，已跳过下载：" + result["config_problem"])
        return False, result, []
    sync_results: list = []
    wanted = needs_download(result)
    if wanted == DOWNLOAD_NONE:
        return True, result, sync_results

    from laoa_trader.data import sync as sync_mod

    if wanted == DOWNLOAD_SYNC_LIGHT:
        # 只缺轻量项（行业归属/日历/指数）：**自动补一次再复查**，不再问、也不再让用户去下载。
        # 理由：这三项是目录类数据（秒级~半分钟、幂等 upsert、不动行情），
        # 而"全量重下"是十几分钟的大动作 —— 为了一个行业归属去重下 10 年数据说不通。
        # 代价（约 90 个请求）与"用户点一次【刷新数据】"完全一样，所以这里不设门槛。
        logger.info("只缺轻量数据，自动同步：" + result["reason"])
        sync_results.extend(sync_mod.sync_light(cfg, progress_cb=progress_cb,
                                               note_cb=note_cb,
                                               should_stop=should_stop))
        return True, check(cfg.db_path, cfg, today=today), sync_results

    allow = bool(auto_download)
    if wanted == DOWNLOAD_INCREMENTAL:
        # 增量很便宜（1 次请求），配置允许时启动即自动补
        allow = allow or bool(getattr(cfg, "auto_download_on_start", True))
        if not allow:
            logger.info("落后不多，但 auto_download_on_start=false：只提示、不自动下载")
            return True, result, sync_results
        sync_results.append(sync_mod.sync_daily(cfg, progress_cb=progress_cb,
                                                note_cb=note_cb))
        return True, check(cfg.db_path, cfg, today=today), sync_results

    # needs_full
    if not allow:
        return False, result, sync_results
    sync_results.append(sync_mod.download_history(
        cfg, progress_cb=progress_cb, should_stop=should_stop, note_cb=note_cb
    ))
    return True, check(cfg.db_path, cfg, today=today), sync_results
