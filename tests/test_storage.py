"""SQLite 存储层：建表幂等、upsert 不删数据、后复权视图、去重表。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from laoa_trader.data import storage


def test_init_db_creates_all_expected_tables(cfg) -> None:
    path = storage.init_db(cfg.db_path)
    with sqlite3.connect(path) as conn:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        views = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        }
    assert {
        "stock_daily_raw", "adjust_event", "stock_basic", "index_daily",
        "trading_calendar", "limit_up_pool", "stock_pool", "position", "signal",
        "intraday_alert",
    } <= names
    assert "stock_daily_hfq" in views


def test_init_db_is_idempotent(cfg) -> None:
    """重复初始化不能报错，也不能清空数据（用户可能反复点"下载"）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [("600001", "2026-01-01", 1, 1, 1, 10.0, 1, 1, 1.0)])
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0] == 1


def test_hfq_view_multiplies_by_factor(cfg) -> None:
    """后复权视图 = 原始价 × factor（与服务器版 stock_daily 数值等价）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            ("600001", "2026-01-01", 10.0, 11.0, 9.0, 10.0, 100.0, 1000.0, 2.0),
        ])
        row = conn.execute(
            "SELECT open, high, low, close, volume, turnover FROM stock_daily_hfq"
        ).fetchone()
    assert row["open"] == 20.0
    assert row["high"] == 22.0
    assert row["low"] == 18.0
    assert row["close"] == 20.0
    # 成交量/成交额不乘因子（与服务器版 apply_factors 一致）
    assert row["volume"] == 100.0
    assert row["turnover"] == 1000.0


def test_daily_raw_upsert_updates_in_place(cfg) -> None:
    """同一个 (symbol,date) 重复写入是 UPDATE，不产生重复行。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [("600001", "2026-01-01", 1, 1, 1, 10.0, 1, 1, 1.0)])
        storage.write_daily_raw(conn, [("600001", "2026-01-01", 1, 1, 1, 12.0, 1, 1, 3.0)])
        rows = conn.execute("SELECT close, factor FROM stock_daily_raw").fetchall()
    assert len(rows) == 1
    assert rows[0]["close"] == 12.0
    assert rows[0]["factor"] == 3.0


def test_existing_dates_by_symbol(cfg) -> None:
    """续传判断的依据：每只股票已入库的日期集合。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            ("600001", "2026-01-01", 1, 1, 1, 1, 1, 1, 1.0),
            ("600001", "2026-01-02", 1, 1, 1, 1, 1, 1, 1.0),
            ("600002", "2026-01-01", 1, 1, 1, 1, 1, 1, 1.0),
        ])
        got = storage.existing_dates_by_symbol(conn)
    assert got == {"600001": {"2026-01-01", "2026-01-02"}, "600002": {"2026-01-01"}}


def test_dates_for_symbols_matches_full_load(cfg) -> None:
    """分片查（续传主力，内存可控）必须与全量载入结果一致。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            ("600001", "2026-01-01", 1, 1, 1, 1, 1, 1, 1.0),
            ("600001", "2026-01-02", 1, 1, 1, 1, 1, 1, 1.0),
            ("600002", "2026-01-02", 1, 1, 1, 1, 1, 1, 1.0),
        ])
        full = storage.existing_dates_by_symbol(conn)
        partial = storage.dates_for_symbols(conn, ["600001", "600002", "999999"])
    assert full == {"600001": {"2026-01-01", "2026-01-02"}, "600002": {"2026-01-02"}}
    assert partial == full            # 没有数据的股票不出现在结果里


def test_dates_for_symbols_handles_more_than_sqlite_variable_limit(cfg) -> None:
    """SQLite 变量上限 999：一次传 1200 只股票不能炸（必须自动分片）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [("600001", "2026-01-01", 1, 1, 1, 1, 1, 1, 1.0)])
        got = storage.dates_for_symbols(conn, [f"{i:06d}" for i in range(1200)])
    assert got == {}


def test_save_pool_removes_stale_rows_of_same_day_only(cfg) -> None:
    """池子幂等：同一天重跑只清掉"这次不在池子里"的行，绝不动其它日期。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [{"symbol": "600001"}, {"symbol": "600002"}], "2026-09-11")
        storage.save_pool(conn, [{"symbol": "000001"}], "2026-09-10")
        storage.save_pool(conn, [{"symbol": "600003"}], "2026-09-11")
        same_day = {r[0] for r in conn.execute(
            "SELECT symbol FROM stock_pool WHERE date = '2026-09-11'")}
        other_day = {r[0] for r in conn.execute(
            "SELECT symbol FROM stock_pool WHERE date = '2026-09-10'")}
    assert same_day == {"600003"}
    assert other_day == {"000001"}   # 其它日期不受影响


def test_save_pool_batch_is_unique_even_with_frozen_clock(cfg, monkeypatch) -> None:
    """**时钟被冻住**（Windows 上 15.6ms 粒度就是这个效果）时，批次号仍必须唯一。

    批次号原来取 `datetime.now().strftime("...%f")`，而清理逻辑是"删掉同一天里
    batch 不等于本次的行"。时钟一冻结（或同一 tick 内连调两次），第二次就会把第一次
    留下的行当成"本次的行"而清不掉 → 同一天重复建池会**残留上一批标的**
    （CI 的 Windows runner 上 100% 复现；Linux 时钟精度高，侥幸一直是绿的）。

    这条用例不依赖平台：冻住时钟后，修之前必红。
    """
    storage.init_db(cfg.db_path)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):        # type: ignore[override]
            return datetime(2026, 9, 13, 16, 0, 0, 123456)

    monkeypatch.setattr(storage, "datetime", _FrozenDatetime)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [{"symbol": "600001"}, {"symbol": "600002"}], "2026-09-11")
        storage.save_pool(conn, [{"symbol": "600003"}], "2026-09-11")
        rows = list(conn.execute(
            "SELECT symbol, batch FROM stock_pool WHERE date = '2026-09-11'"))
    assert {symbol for symbol, _batch in rows} == {"600003"}
    assert len({batch for _symbol, batch in rows}) == 1      # 只剩本次的批次


def test_new_batch_never_repeats() -> None:
    """批次号连取 1000 次不能有重复（不能依赖时钟分辨率）。"""
    assert len({storage._new_batch() for _ in range(1000)}) == 1000


def test_load_pool_defaults_to_latest_day(cfg) -> None:
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.save_pool(conn, [{"symbol": "600001", "name": "甲", "score": 1.0,
                                  "strategy": "LowPriceStrategy"}], "2026-09-10")
        storage.save_pool(conn, [{"symbol": "600002", "name": "乙", "score": 2.0,
                                  "strategy": "ReversalStrategy"}], "2026-09-11")
        rows = storage.load_pool(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600002"
    assert rows[0]["date"] == "2026-09-11"


# ── 提醒去重 ──


def test_record_alerts_dedupes_per_day_symbol_kind(cfg) -> None:
    """同一天、同标的、同类型只推一次；换日期或换类型仍可推。"""
    storage.init_db(cfg.db_path)
    alerts = [
        {"symbol": "600001", "kind": "stop_loss", "price": 9.5, "detail": "触及止损"},
        {"symbol": "600001", "kind": "stop_loss", "price": 9.4, "detail": "再次触及"},
        {"symbol": "600001", "kind": "take_profit", "price": 11.0, "detail": "触及止盈"},
    ]
    with storage.connect(cfg.db_path) as conn:
        fresh = storage.record_alerts(conn, alerts, "2026-09-11")
        again = storage.record_alerts(conn, alerts, "2026-09-11")
        next_day = storage.record_alerts(conn, alerts, "2026-09-12")
        rows = storage.load_recent_alerts(conn)
    assert len(fresh) == 2                       # 重复的 stop_loss 被去掉
    assert again == []                           # 当天再跑一轮：全部已推过
    assert len(next_day) == 2                    # 换一天重新提醒
    assert len(rows) == 4


def test_record_alerts_uses_name_when_symbol_missing(cfg) -> None:
    """首板提醒没有本地代码（来自实时涨停池），用中文名兜底做去重键。"""
    storage.init_db(cfg.db_path)
    alert = {"symbol": "", "name": "某某股份", "kind": "first_board", "price": 12.0,
             "detail": "首板，封单 1.00 亿"}
    with storage.connect(cfg.db_path) as conn:
        assert len(storage.record_alerts(conn, [alert], "2026-09-11")) == 1
        assert storage.record_alerts(conn, [alert], "2026-09-11") == []


# ── 持仓 ──


def test_position_upsert_and_delete(cfg) -> None:
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_position(conn, "600001", name="甲", quantity=1000, avg_cost=3.5)
        storage.upsert_position(conn, "600001", quantity=1500, avg_cost=3.6)
        positions = storage.load_positions(conn)
        assert positions["600001"]["quantity"] == 1500
        assert positions["600001"]["avg_cost"] == 3.6
        assert storage.delete_position(conn, "600001") is True
        assert storage.delete_position(conn, "600001") is False
        assert storage.load_positions(conn) == {}


def test_data_summary_shape(cfg) -> None:
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        summary = storage.data_summary(conn)
    assert set(summary) >= {
        "daily_rows", "symbols", "latest_date", "basic", "industries", "calendar",
        "limit_up_days", "pool", "positions",
    }
    assert summary["daily_rows"] == 0
