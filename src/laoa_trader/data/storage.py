"""本地 SQLite 存储层：建表、幂等 upsert、后复权视图。

设计原则（继承服务器版的教训）
------------------------------
1. **只 upsert，不删**：所有写入都是 `INSERT ... ON CONFLICT DO UPDATE`。
   2026-09-11 那次事故的根因就是"按日期 DELETE 再整批 INSERT"——
   一旦中间失败，当天数据全没了。桌面版更要小心：用户重跑下载时
   绝不能把已经下好的 10 年数据删掉。
2. **原始价 + 复权因子分离**：`stock_daily_raw` 存**不复权**原始价 + `factor` 列，
   后复权价由 `stock_daily_hfq` **视图**实时算出。好处是复权事件更新后
   不需要重写几百万行价格，只改 factor（服务器版 dump_import 也是这个分层）。
3. **股票代码用裸 6 位**（`600519`），指数/板块带前缀（`sh.000300`、`ti.881101`）——
   与服务器版完全一致，方便两边对账。

表清单
------
    stock_daily_raw   不复权日线 + 后复权因子 factor
    stock_daily_hfq   **视图**：后复权价（open/high/low/close × factor）
    adjust_event      复权事件（分红/送股/配股）
    stock_basic       代码 → 名称 / 一级行业
    index_daily       指数与行业板块日线
    trading_calendar  官方交易日历
    limit_up_pool     涨停池（连板数/封单额/涨停时间/原因）
    stock_pool        每日精选股票池
    position          持仓台账
    signal            选股信号落库
    intraday_alert    盘中提醒去重表
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 进程内自增序号：保证**同一个时钟 tick 内**连续两次调用也拿到不同批次号
_BATCH_SEQ = itertools.count(1)


def _new_batch() -> str:
    """本次运行的批次号 —— **必须唯一，不能只靠时钟**。

    为什么不能用 `datetime.now().strftime("...%f")`：Windows 的系统时钟粒度约 15.6ms，
    在同一个 tick 里连续两次调用会拿到**一模一样**的批次号。而清理逻辑是
    "删掉同一天里 batch 不等于本次的行"，于是第一次留下的行会被当成"本次的行"
    留了下来 —— 同一天重复建池时池子里就残留上一批标的（CI 的 Windows runner 上
    这条用例 100% 复现；Linux 因为时钟精度高而侥幸通过，所以一直没暴露）。

    现在 = 进程号 + 进程内自增序号 + 随机后缀：跨 tick、跨多次调用、跨进程都不会撞。
    """
    return f"{os.getpid()}-{next(_BATCH_SEQ)}-{uuid.uuid4().hex[:8]}"

#: 建表 DDL（全部 IF NOT EXISTS，可反复执行）
SCHEMA: tuple[str, ...] = (
    # ── 原始行情（不复权）+ 后复权因子 ──
    """
    CREATE TABLE IF NOT EXISTS stock_daily_raw (
        symbol   TEXT NOT NULL,
        date     TEXT NOT NULL,
        open     REAL,
        high     REAL,
        low      REAL,
        close    REAL,
        volume   REAL,
        turnover REAL,
        factor   REAL NOT NULL DEFAULT 1.0,
        updated_at TEXT,
        PRIMARY KEY (symbol, date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_date ON stock_daily_raw (date);",
    # 后复权视图：下游（策略/回测）只认这一张，与服务器版 stock_daily 口径一致
    """
    CREATE VIEW IF NOT EXISTS stock_daily_hfq AS
    SELECT symbol,
           date,
           open  * COALESCE(factor, 1.0) AS open,
           high  * COALESCE(factor, 1.0) AS high,
           low   * COALESCE(factor, 1.0) AS low,
           close * COALESCE(factor, 1.0) AS close,
           volume,
           turnover
    FROM stock_daily_raw;
    """,
    # ── 复权事件 ──
    """
    CREATE TABLE IF NOT EXISTS adjust_event (
        symbol             TEXT NOT NULL,
        ex_date            TEXT NOT NULL,
        dividend_per_share REAL,
        per_share_bonus    REAL,
        allotment_ratio    REAL,
        allotment_price    REAL,
        source             TEXT,
        updated_at         TEXT,
        PRIMARY KEY (symbol, ex_date)
    );
    """,
    # ── 代码 → 名称 / 行业 ──
    """
    CREATE TABLE IF NOT EXISTS stock_basic (
        symbol     TEXT PRIMARY KEY,
        name       TEXT,
        industry   TEXT,
        updated_at TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_basic_industry ON stock_basic (industry);",
    # ── 指数 / 行业板块日线 ──
    """
    CREATE TABLE IF NOT EXISTS index_daily (
        symbol   TEXT NOT NULL,
        date     TEXT NOT NULL,
        open     REAL,
        high     REAL,
        low      REAL,
        close    REAL,
        volume   REAL,
        turnover REAL,
        PRIMARY KEY (symbol, date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_index_date ON index_daily (date);",
    # ── 交易日历 ──
    """
    CREATE TABLE IF NOT EXISTS trading_calendar (
        date       TEXT PRIMARY KEY,
        source     TEXT,
        updated_at TEXT
    );
    """,
    # ── 涨停池 ──
    """
    CREATE TABLE IF NOT EXISTS limit_up_pool (
        date          TEXT    NOT NULL,
        symbol        TEXT    NOT NULL,
        name          TEXT,
        high_days     INTEGER,
        limit_up_type TEXT,
        first_limit_up_time TEXT,
        last_limit_up_time  TEXT,
        order_amount  REAL,
        open_num      INTEGER,
        reason_type   TEXT,
        turnover_rate REAL,
        change_rate   REAL,
        currency_value REAL,
        is_again_limit INTEGER,
        is_new        INTEGER,
        continue_day_text TEXT,
        max_seal_money REAL,
        last_price    REAL,
        is_st         INTEGER,
        source        TEXT,
        updated_at    TEXT,
        PRIMARY KEY (date, symbol)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_limit_up_date ON limit_up_pool (date);",
    # ── 每日股票池 ──
    """
    CREATE TABLE IF NOT EXISTS stock_pool (
        date       TEXT NOT NULL,
        symbol     TEXT NOT NULL,
        name       TEXT,
        strategy   TEXT,
        strategies TEXT,
        score      REAL,
        reason     TEXT,
        created_at TEXT,
        batch      TEXT,
        PRIMARY KEY (date, symbol)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_stock_pool_date ON stock_pool (date);",
    # ── 持仓台账（只记录，不涉及任何下单）──
    """
    CREATE TABLE IF NOT EXISTS position (
        symbol     TEXT PRIMARY KEY,
        name       TEXT,
        quantity   INTEGER NOT NULL DEFAULT 0,
        avg_cost   REAL    NOT NULL DEFAULT 0,
        opened_at  TEXT,
        closed_at  TEXT,
        source     TEXT,
        note       TEXT,
        updated_at TEXT
    );
    """,
    # ── 选股信号 ──
    """
    CREATE TABLE IF NOT EXISTS signal (
        signal_date TEXT NOT NULL,
        strategy    TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        name        TEXT,
        close       REAL,
        score       REAL,
        reason      TEXT,
        created_at  TEXT,
        PRIMARY KEY (signal_date, strategy, symbol)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_signal_date ON signal (signal_date);",
    # ── 盘中提醒去重（同标的同类型当天只推一次）──
    """
    CREATE TABLE IF NOT EXISTS intraday_alert (
        date      TEXT NOT NULL,
        symbol    TEXT NOT NULL,
        kind      TEXT NOT NULL,
        price     REAL,
        detail    TEXT,
        pushed_at TEXT,
        PRIMARY KEY (date, symbol, kind)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_alert_date ON intraday_alert (date);",
    # ── 推送去重（手动跑与定时跑必须幂等：同一天同一批内容只推一次）──
    """
    CREATE TABLE IF NOT EXISTS push_log (
        day         TEXT NOT NULL,
        kind        TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        pushed_at   TEXT,
        PRIMARY KEY (day, kind, fingerprint)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_push_log_day ON push_log (day);",
    # ── 自选股（用户手动加的，和策略标的并列进池、一起盯）──
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol   TEXT PRIMARY KEY,
        name     TEXT,
        note     TEXT,
        enabled  INTEGER NOT NULL DEFAULT 1,
        added_at TEXT
    );
    """,
)

#: 期望存在的表（老库升级判定用；新增表时同步加到这里）
EXPECTED_TABLES: frozenset[str] = frozenset({
    "stock_daily_raw", "adjust_event", "stock_basic", "index_daily", "trading_calendar",
    "limit_up_pool", "stock_pool", "position", "signal", "intraday_alert", "push_log",
    "watchlist",
})


def connect(db_path: str | Path, *, timeout: float = 60.0) -> sqlite3.Connection:
    """打开连接：WAL 模式（读写不互锁，GUI 与后台线程可同时访问）。

    **库/表缺失时自动建好**：桌面程序可能在任何入口被打开（界面、CLI、调度线程），
    让每个入口自己记得建表太容易漏 —— 一次 sqlite_master 查询（微秒级）就能免掉
    "第一次运行报 no such table" 这类最尴尬的错误。
    """
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        existing = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # 缺任何一张表就整套补一遍（全是 IF NOT EXISTS，幂等）——
        # 这样**老库升级**（新增 push_log 这类表）不需要用户删库重下几十年数据
        if not EXPECTED_TABLES.issubset(existing):
            for ddl in SCHEMA:
                conn.execute(ddl)
            conn.commit()
    except sqlite3.Error as exc:  # pragma: no cover - 只读介质等极端情况
        logger.warning(f"初始化数据库结构失败：{exc}")
    return conn


def init_db(db_path: str | Path) -> Path:
    """建库建表（幂等），返回数据库路径。"""
    path = Path(db_path)
    with connect(path) as conn:
        for ddl in SCHEMA:
            conn.execute(ddl)
        conn.commit()
    return path


def upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    conflict: Sequence[str],
    update: Sequence[str] | None = None,
    ignore: bool = False,
    batch_size: int = 5000,
) -> int:
    """批量幂等写入（本模块所有写操作的唯一入口）。

    Args:
        table: 表名。
        columns: 要写入的列（顺序与 rows 一致）。
        rows: 行数据（可迭代，支持生成器 —— 全市场 dump 有几百万行，不能先全塞内存）。
        conflict: 冲突键列（一般是主键）。
        update: 冲突时要更新的列；None 表示"用除主键外的所有列"。
        ignore: True 时冲突直接跳过（用于"只记首次"的去重表）。
        batch_size: 每批提交的行数（大事务会让 GUI 卡住，分批提交更平滑）。

    Returns:
        实际提交的行数。
    """
    cols = list(columns)
    if ignore:
        sql = (
            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})"
        )
    else:
        sets = list(update) if update is not None else [
            c for c in cols if c not in set(conflict)
        ]
        assign = ", ".join(f"{c} = excluded.{c}" for c in sets)
        action = (
            f"DO UPDATE SET {assign}" if assign
            else "DO NOTHING"
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT({', '.join(conflict)}) {action}"
        )

    total = 0
    batch: list[Sequence[Any]] = []
    for row in rows:
        batch.append(tuple(row))
        if len(batch) >= batch_size:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    return total


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 行情 ──


def write_daily_raw(
    conn: sqlite3.Connection,
    rows: Iterable[Sequence[Any]],
    *,
    batch_size: int = 20000,
) -> int:
    """不复权日线 upsert（含 factor 列）。

    行顺序：symbol,date,open,high,low,close,volume,turnover[,factor]。
    只给 8 列时 factor 默认 1.0（没配到复权事件的股票本来就是 1）。
    """
    now = _now()

    def _payload():
        for r in rows:
            factor = r[8] if len(r) > 8 and r[8] is not None else 1.0
            yield (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], factor, now)

    return upsert(
        conn,
        "stock_daily_raw",
        ("symbol", "date", "open", "high", "low", "close", "volume", "turnover",
         "factor", "updated_at"),
        _payload(),
        conflict=("symbol", "date"),
        batch_size=batch_size,
    )


def write_index_daily(conn: sqlite3.Connection, rows: Iterable[Sequence[Any]]) -> int:
    """指数/板块日线 upsert。行顺序：symbol,date,o,h,l,c,vol,turnover。"""
    return upsert(
        conn,
        "index_daily",
        ("symbol", "date", "open", "high", "low", "close", "volume", "turnover"),
        rows,
        conflict=("symbol", "date"),
        batch_size=10000,
    )


def write_adjust_events(conn: sqlite3.Connection, rows: Iterable[Sequence[Any]]) -> int:
    """复权事件 upsert。行顺序：symbol,ex_date,dividend,bonus,allot_ratio,allot_price。"""
    now = _now()
    payload = (
        (r[0], r[1], r[2], r[3], r[4], r[5], "hithink", now) for r in rows
    )
    return upsert(
        conn,
        "adjust_event",
        ("symbol", "ex_date", "dividend_per_share", "per_share_bonus",
         "allotment_ratio", "allotment_price", "source", "updated_at"),
        payload,
        conflict=("symbol", "ex_date"),
        batch_size=10000,
    )


def write_stock_basic(
    conn: sqlite3.Connection,
    rows: Iterable[Sequence[Any]],
    *,
    industry_only: bool = False,
) -> int:
    """代码/名称/行业 upsert。行顺序：symbol,name,industry。

    Args:
        industry_only: True 时只更新行业（名称用 COALESCE 保留已有值）——
            行业同步拿到的名称字段偶尔为空，不能把已缓存的中文名冲掉。
    """
    now = _now()
    if industry_only:
        sql = (
            "INSERT INTO stock_basic (symbol, name, industry, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET industry = excluded.industry, "
            "name = COALESCE(stock_basic.name, excluded.name), "
            "updated_at = excluded.updated_at"
        )
        total = 0
        batch: list[tuple] = []
        for r in rows:
            batch.append((r[0], r[1], r[2], now))
            if len(batch) >= 5000:
                conn.executemany(sql, batch)
                conn.commit()
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
        return total
    payload = ((r[0], r[1], r[2], now) for r in rows)
    return upsert(
        conn,
        "stock_basic",
        ("symbol", "name", "industry", "updated_at"),
        payload,
        conflict=("symbol",),
    )


def write_calendar(conn: sqlite3.Connection, days: Iterable[str], source: str = "hithink") -> int:
    """交易日历 upsert。"""
    now = _now()
    return upsert(
        conn,
        "trading_calendar",
        ("date", "source", "updated_at"),
        ((d, source, now) for d in days),
        conflict=("date",),
    )


LIMIT_UP_COLUMNS: tuple[str, ...] = (
    "date", "symbol", "name", "high_days", "limit_up_type", "first_limit_up_time",
    "last_limit_up_time", "order_amount", "open_num", "reason_type", "turnover_rate",
    "change_rate", "currency_value", "is_again_limit", "is_new", "continue_day_text",
    "max_seal_money", "last_price", "is_st", "source", "updated_at",
)


def write_limit_up_pool(conn: sqlite3.Connection, rows: Iterable[Sequence[Any]]) -> int:
    """涨停池 upsert（行顺序见 LIMIT_UP_COLUMNS）。"""
    return upsert(
        conn,
        "limit_up_pool",
        LIMIT_UP_COLUMNS,
        rows,
        conflict=("date", "symbol"),
        batch_size=1000,
    )


# ── 股票池 ──


def save_pool(
    conn: sqlite3.Connection,
    pool: list[dict],
    day: str,
    *,
    batch: str | None = None,
) -> int:
    """写入当日股票池（幂等）。

    为什么不是"按日期 DELETE 再 INSERT"：那样一旦中间失败当天池子就空了。
    这里改用 `batch` 标记同一次运行：先 upsert 本次全部行（都打上同一个 batch），
    再删掉**同一天里 batch 不等于本次**的残留行 —— 只清理"上一次运行留下的、
    这次已经不在池子里的"标的，绝不动其它日期、也绝不出现"空窗期"。

    Returns:
        本次写入的池子行数。
    """
    if not pool:
        return 0
    day_batch = batch or _new_batch()
    now = _now()
    written = upsert(
        conn,
        "stock_pool",
        ("date", "symbol", "name", "strategy", "strategies", "score", "reason",
         "created_at", "batch"),
        (
            (day, r["symbol"], r.get("name"), r.get("strategy"), r.get("strategies"),
             r.get("score"), r.get("reason"), now, day_batch)
            for r in pool
        ),
        conflict=("date", "symbol"),
        batch_size=1000,
    )
    conn.execute(
        "DELETE FROM stock_pool WHERE date = ? AND batch IS NOT ?", (day, day_batch)
    )
    conn.commit()
    return written


def load_pool(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    """读取股票池：指定日期，或最近一次。"""
    if day is None:
        row = conn.execute("SELECT MAX(date) FROM stock_pool").fetchone()
        day = row[0] if row and row[0] else None
    if not day:
        return []
    rows = conn.execute(
        "SELECT * FROM stock_pool WHERE date = ? ORDER BY score DESC", (day,)
    ).fetchall()
    return [dict(r) for r in rows]


def pool_symbols(conn: sqlite3.Connection, day: str | None = None) -> list[str]:
    return [r["symbol"] for r in load_pool(conn, day)]


# ── 持仓 ──


def load_positions(conn: sqlite3.Connection, open_only: bool = True) -> dict[str, dict]:
    """读取持仓：{symbol: {...}}。"""
    sql = "SELECT * FROM position"
    if open_only:
        sql += " WHERE quantity > 0"
    return {r["symbol"]: dict(r) for r in conn.execute(sql).fetchall()}


def upsert_position(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    name: str | None = None,
    quantity: int = 0,
    avg_cost: float = 0.0,
    note: str = "",
) -> dict:
    """新增/修改一条持仓（GUI 的"添加持仓"直接调它）。"""
    now = _now()
    conn.execute(
        "INSERT INTO position (symbol, name, quantity, avg_cost, opened_at, source, note, "
        "updated_at) VALUES (?, ?, ?, ?, ?, 'manual', ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET name = COALESCE(excluded.name, position.name), "
        "quantity = excluded.quantity, avg_cost = excluded.avg_cost, note = excluded.note, "
        "closed_at = CASE WHEN excluded.quantity > 0 THEN NULL ELSE position.closed_at END, "
        "updated_at = excluded.updated_at",
        (symbol, name, int(quantity), float(avg_cost), now, note, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM position WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row)


def delete_position(conn: sqlite3.Connection, symbol: str) -> bool:
    """删除持仓（GUI 的"删除持仓"）。"""
    cur = conn.execute("DELETE FROM position WHERE symbol = ?", (symbol,))
    conn.commit()
    return bool(cur.rowcount)


# ── 信号 ──


def write_signals(conn: sqlite3.Connection, rows: Iterable[Sequence[Any]]) -> int:
    """信号 upsert。行顺序：signal_date,strategy,symbol,name,close,score,reason。"""
    now = _now()
    payload = (
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6], now) for r in rows
    )
    return upsert(
        conn,
        "signal",
        ("signal_date", "strategy", "symbol", "name", "close", "score", "reason",
         "created_at"),
        payload,
        conflict=("signal_date", "strategy", "symbol"),
        batch_size=2000,
    )


def recent_signal_symbols(
    conn: sqlite3.Connection,
    days: int = 10,
    allowed_strategies: set[str] | None = None,
) -> dict[str, dict]:
    """取最近 N 个交易日推送过的信号（作为卖出/风控观察池）。

    Args:
        days: 回看多少个信号日。
        allowed_strategies: 只保留这些策略产生的信号（自选策略组后的观察池过滤）；
            None = 不过滤。
    """
    dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT signal_date FROM signal ORDER BY signal_date DESC LIMIT ?",
            (days,),
        )
    ]
    if not dates:
        return {}
    marks = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT symbol, name, signal_date, close, strategy FROM signal "  # noqa: S608
        f"WHERE signal_date IN ({marks}) ORDER BY signal_date",
        tuple(dates),
    ).fetchall()
    out: dict[str, dict] = {}
    for symbol, name, signal_date, close, strategy in rows:
        if allowed_strategies is not None and strategy not in allowed_strategies:
            continue
        out[symbol] = {"name": name, "signal_date": signal_date, "close": close,
                       "strategy": strategy}
    return out


# ── 盘中提醒去重 ──


def record_alerts(
    conn: sqlite3.Connection, alerts: list[dict], day: str
) -> list[dict]:
    """把提醒写入去重表，返回**首次出现**的那些（同标的同类型当天只推一次）。"""
    fresh: list[dict] = []
    now = _now()
    for alert in alerts:
        # 首板提醒没有本地代码，用中文名兜底做去重键
        key_symbol = alert.get("symbol") or alert.get("name") or ""
        cur = conn.execute(
            "INSERT OR IGNORE INTO intraday_alert (date, symbol, kind, price, detail, "
            "pushed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (day, key_symbol, alert["kind"], alert.get("price"), alert.get("detail"), now),
        )
        if cur.rowcount:
            fresh.append(alert)
    conn.commit()
    return fresh


def load_recent_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """最近提醒（GUI 的"盘中提醒"列表）。"""
    rows = conn.execute(
        "SELECT date, symbol, kind, price, detail, pushed_at FROM intraday_alert "
        "ORDER BY pushed_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── 自选股 ──


def load_watchlist(
    conn: sqlite3.Connection, enabled_only: bool = False
) -> list[dict]:
    """读取自选股（按加入时间升序 = 用户添加顺序）。

    Args:
        enabled_only: 只要启用的（进池/盯盘用这个）；False 时连停用的一起返回（列表展示用）。
    """
    sql = "SELECT * FROM watchlist"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY added_at, symbol"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def watchlist_symbols(conn: sqlite3.Connection, enabled_only: bool = True) -> list[str]:
    return [r["symbol"] for r in load_watchlist(conn, enabled_only)]


def watchlist_map(conn: sqlite3.Connection) -> dict[str, dict]:
    """{代码: 行}，供池子/提醒文案补来源与备注。"""
    return {r["symbol"]: r for r in load_watchlist(conn, enabled_only=False)}


def upsert_watchlist(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    name: str | None = None,
    note: str = "",
    enabled: bool = True,
) -> dict:
    """添加/更新一只自选股（幂等：同一代码重复添加只会更新名称与备注）。

    名称用 `COALESCE` 保护：添加时库里查得到就用库里的名字，
    但**不能**因为这次传了空名字就把已缓存的名字冲掉。
    """
    now = _now()
    conn.execute(
        "INSERT INTO watchlist (symbol, name, note, enabled, added_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "  name = COALESCE(excluded.name, watchlist.name), "
        "  note = CASE WHEN excluded.note != '' THEN excluded.note ELSE watchlist.note END, "
        "  enabled = excluded.enabled",
        (symbol, name, note, 1 if enabled else 0, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row)


def remove_watchlist(conn: sqlite3.Connection, symbol: str) -> bool:
    """删除自选股（彻底移出列表）。"""
    cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()
    return bool(cur.rowcount)


def set_watchlist_enabled(conn: sqlite3.Connection, symbol: str, enabled: bool) -> bool:
    """启用/停用：停用后不进池、不监控，但**保留在列表里**（随时能再打开）。"""
    cur = conn.execute(
        "UPDATE watchlist SET enabled = ? WHERE symbol = ?",
        (1 if enabled else 0, symbol),
    )
    conn.commit()
    return bool(cur.rowcount)


# ── 推送去重 ──


def mark_pushed(
    conn: sqlite3.Connection, day: str, kind: str, fingerprint: str
) -> bool:
    """记录"这一天、这一类、这份内容已经推过"，返回**是否首次**。

    为什么需要：手动【立即选股并建池】与 19:15 的定时任务可能同一天都跑，
    池子内容一样却推两遍 —— 用户会被同一批卡片刷屏。
    这里用内容指纹（`pipeline.pool_fingerprint()`）判重：
    池子没变就不重复推；变了（例如盘中补了数据）才再推一次。
    """
    now = _now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO push_log (day, kind, fingerprint, pushed_at) "
        "VALUES (?, ?, ?, ?)",
        (day, kind, fingerprint, now),
    )
    conn.commit()
    return bool(cur.rowcount)


def was_pushed(conn: sqlite3.Connection, day: str, kind: str, fingerprint: str) -> bool:
    """只查询不写入（界面显示"今天已推送过"用）。"""
    row = conn.execute(
        "SELECT 1 FROM push_log WHERE day = ? AND kind = ? AND fingerprint = ?",
        (day, kind, fingerprint),
    ).fetchone()
    return bool(row)


def pushed_days(conn: sqlite3.Connection, kind: str = "pool", limit: int = 10) -> list[str]:
    """最近推送过的日期（状态栏/自检用）。"""
    rows = conn.execute(
        "SELECT DISTINCT day FROM push_log WHERE kind = ? ORDER BY day DESC LIMIT ?",
        (kind, limit),
    ).fetchall()
    return [r[0] for r in rows]


# ── 查询辅助 ──


def latest_date(conn: sqlite3.Connection) -> str | None:
    """库里最新行情日期（状态栏"最新数据日期"）。"""
    row = conn.execute("SELECT MAX(date) FROM stock_daily_raw").fetchone()
    return row[0] if row and row[0] else None


def existing_daily_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """已入库的 (symbol, date) 集合 —— **续传**的依据。

    返回 set 而不是逐行查库：10 年全市场约 1000 万行，逐行 SELECT 会把
    下载过程拖成几个小时。内存占用约 1GB 量级（元组开销），可接受；
    调用方也可以分片使用 `existing_symbol_dates()`。
    """
    return {
        (r[0], r[1])
        for r in conn.execute("SELECT symbol, date FROM stock_daily_raw")
    }


def existing_dates_by_symbol(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """{symbol: {date...}}：按股票看已有哪些日期（增量续传用）。

    ⚠️ 全库载入：10 年全市场约 1000 万行时内存开销很大（GB 级）。
    全量下载请用 `dates_for_symbols()` 分片查。
    """
    out: dict[str, set[str]] = {}
    for symbol, date in conn.execute("SELECT symbol, date FROM stock_daily_raw"):
        out.setdefault(symbol, set()).add(date)
    return out


def dates_for_symbols(
    conn: sqlite3.Connection, symbols: Sequence[str]
) -> dict[str, set[str]]:
    """只查指定股票已入库的日期（**续传判断的主力**，内存可控）。

    为什么不用 `existing_dates_by_symbol` 一把梭：全量下载时那是 1000 万个
    (symbol,date) 元组，光集合开销就 1GB 左右，和行情表叠在一起在 8GB 的
    Windows 机器上会 OOM。这里按 500 只一批走主键索引，峰值只是几万行。

    Args:
        symbols: 股票代码（裸 6 位）。

    Returns:
        {symbol: {date, ...}}，没有数据的股票不会出现在结果里。
    """
    out: dict[str, set[str]] = {}
    chunk = 500  # 规避 SQLite 变量数上限（默认 999）
    for i in range(0, len(symbols), chunk):
        part = list(symbols[i : i + chunk])
        marks = ",".join("?" * len(part))
        for symbol, date in conn.execute(
            f"SELECT symbol, date FROM stock_daily_raw WHERE symbol IN ({marks})",  # noqa: S608
            tuple(part),
        ):
            out.setdefault(symbol, set()).add(date)
    return out


def data_summary(conn: sqlite3.Connection) -> dict:
    """数据概况（GUI 状态栏与 CLI 输出用）。"""
    def _count(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            return 0

    return {
        "daily_rows": _count("SELECT COUNT(*) FROM stock_daily_raw"),
        # 股票数用 `stock_basic` 的**表行数**（SQLite 对无 WHERE 的 COUNT(*) 走 O(1) 优化），
        # 而不是 `COUNT(DISTINCT symbol) FROM stock_daily_raw` —— 后者要在几百万行的行情表上
        # 做全表去重：实测 168 万行 61ms，5~10 年库（500~1000 万行）线性外推 200~400ms，
        # 而界面每 5 秒就会问一次（用户实报"下载时界面卡死"的元凶之一）。
        # 语义相同：每只股票在 stock_basic 里都有一行（下载/名称同步都会写）。
        "symbols": _count("SELECT COUNT(*) FROM stock_basic"),
        "latest_date": latest_date(conn),
        "basic": _count("SELECT COUNT(*) FROM stock_basic"),
        "industries": _count(
            "SELECT COUNT(DISTINCT industry) FROM stock_basic "
            "WHERE industry IS NOT NULL AND industry != ''"
        ),
        "calendar": _count("SELECT COUNT(*) FROM trading_calendar"),
        "limit_up_days": _count("SELECT COUNT(DISTINCT date) FROM limit_up_pool"),
        "pool": _count("SELECT COUNT(*) FROM stock_pool"),
        "positions": _count("SELECT COUNT(*) FROM position WHERE quantity > 0"),
        "pushed_days": _count("SELECT COUNT(DISTINCT day) FROM push_log WHERE kind = 'pool'"),
        "watchlist": _count("SELECT COUNT(*) FROM watchlist WHERE enabled = 1"),
    }
