"""数据访问门面：策略/建池/盘中提醒统一通过它读库。

为什么需要
----------
服务器版有 `DataEngine`（几十个方法：同步、回测、覆盖率自检……）。
桌面版把"同步/下载"交给 `data/sync.py`，这里只保留**策略真正用到的读接口**，
好处是策略代码可以**逐行照搬**服务器版（`self.engine.db_path` /
`self.engine.get_active_symbols()`），口径不会漂。

口径提示：这里所有行情读取都走 `stock_daily_hfq` **视图**（后复权），
等价于服务器版的 `stock_daily` 表 —— 因为桌面版把原始价和复权因子分开存了。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from laoa_trader.data import storage
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 最新交易日覆盖率下限（与服务器版一致）。
#: 低于它说明数据源当天大面积缺数（实测有天只有 206/5217 有数据），
#: 这时如果还按"最新一天"匹配，股票池会缩到几十只、毫无意义 —— 因此回退到上一个达标日。
COVERAGE_FLOOR = 0.5

#: 策略读取的行情表：后复权视图
HFQ_TABLE = "stock_daily_hfq"


class DataEngine:
    """本地库读接口（策略与提醒共用）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    # ── 连接 ──

    def connect(self, timeout: float = 60.0) -> sqlite3.Connection:
        """打开一个连接（库不存在时会建表 —— 空库也应能"跑一次"而不报错）。"""
        path = Path(self.db_path)
        if not path.exists():
            storage.init_db(path)
        return storage.connect(path, timeout=timeout)

    # ── 行情 ──

    def get_latest_data_date(self) -> str | None:
        """库中最新的行情日期；空库返回 None。"""
        with self.connect() as conn:
            return storage.latest_date(conn)

    def get_local_symbols(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {HFQ_TABLE}"  # noqa: S608 - 常量表名
            ).fetchall()
        return [row[0] for row in rows]

    def get_active_symbols(
        self, date: str | None = None, min_coverage: float = COVERAGE_FLOOR
    ) -> list[str]:
        """返回「最新交易日确实有行情」的股票池 —— 策略应当用它而不是全部本地股票。

        为什么需要：策略只用每只股票的**最后一根K线**匹配，若某只股票因同步失败或
        停牌导致最后一根K线停留在更早的日期，它仍会参与匹配，等于**用隔日数据匹配**。
        这同时也过滤掉了当日停牌的股票 —— 它们本来就无法交易。

        覆盖率回退：若最新交易日的覆盖率低于 min_coverage，则回退到覆盖率达标的
        上一个交易日，避免股票池缩水到几十只。
        """
        with self.connect() as conn:
            universe = conn.execute(
                f"SELECT COUNT(DISTINCT symbol) FROM {HFQ_TABLE}"  # noqa: S608
            ).fetchone()[0]
            if not universe:
                return []

            if date:
                target = date
            else:
                recent = conn.execute(
                    f"SELECT date, COUNT(DISTINCT symbol) AS n FROM {HFQ_TABLE} "  # noqa: S608
                    "GROUP BY date ORDER BY date DESC LIMIT 30"
                ).fetchall()
                target = recent[0][0] if recent else None
                for candidate, count in recent:
                    if count / universe >= min_coverage:
                        target = candidate
                        break

            if not target:
                return []
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {HFQ_TABLE} WHERE date = ? ORDER BY symbol",  # noqa: S608
                (target,),
            ).fetchall()
        return [row[0] for row in rows]

    def data_coverage(self, date: str | None = None) -> dict:
        """最新交易日的行情覆盖率（自检/状态栏用）。"""
        with self.connect() as conn:
            universe = conn.execute(
                f"SELECT COUNT(DISTINCT symbol) FROM {HFQ_TABLE}"  # noqa: S608
            ).fetchone()[0]
            target = date or conn.execute(
                f"SELECT MAX(date) FROM {HFQ_TABLE}"  # noqa: S608
            ).fetchone()[0]
            symbols = 0
            if target:
                symbols = conn.execute(
                    f"SELECT COUNT(DISTINCT symbol) FROM {HFQ_TABLE} WHERE date = ?",  # noqa: S608
                    (target,),
                ).fetchone()[0]
        return {
            "date": target,
            "symbols": symbols,
            "universe": universe,
            "missing": max(universe - symbols, 0),
            "rate": (symbols / universe) if universe else 0.0,
        }

    # ── 名称 ──

    def get_stock_names(self, symbols: list[str] | None = None) -> dict[str, str]:
        """本地股票名称（**只读本地表、不联网**，网络抖动不会让卡片里只剩代码）。"""
        with self.connect() as conn:
            if symbols:
                out: dict[str, str] = {}
                chunk = 500  # 规避 SQLite 的变量数上限（默认 999）
                for i in range(0, len(symbols), chunk):
                    part = symbols[i : i + chunk]
                    marks = ",".join("?" * len(part))
                    rows = conn.execute(
                        f"SELECT symbol, name FROM stock_basic "  # noqa: S608
                        f"WHERE symbol IN ({marks})",
                        tuple(part),
                    ).fetchall()
                    out.update({s: n for s, n in rows if n})
                return out
            rows = conn.execute(
                "SELECT symbol, name FROM stock_basic WHERE name IS NOT NULL"
            ).fetchall()
        return {s: n for s, n in rows}

    def get_industry_map(self) -> dict[str, str]:
        """{代码: 一级行业}（热门行业过滤用）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, industry FROM stock_basic "
                "WHERE industry IS NOT NULL AND industry != ''"
            ).fetchall()
        return {s: i for s, i in rows}

    # ── 概况 ──

    def summary(self) -> dict:
        with self.connect() as conn:
            return storage.data_summary(conn)
