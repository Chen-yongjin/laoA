"""数据同步：下载历史（含续传与进度）、日更增量、日历/行业/指数、失败返回结构化结果。

全部离线：用真实 Parquet 文件（pyarrow 写）+ 假客户端，不联网、不需要 Key。
"""

from __future__ import annotations

import pandas as pd
import pytest

from laoa_trader.data import hithink as hx
from laoa_trader.data import storage, sync
from tests.conftest import FakeClient


# ── 构造假 dump ──


def _write_daily_parquet(path, rows: list[tuple[str, str, float]]) -> None:
    """写一个和同花顺 daily-k dump 同结构的 Parquet。

    列名与官方一致：thscode / date_ms / open_price / high_price / low_price /
    close_price / volume / turnover。
    """
    frame = pd.DataFrame([
        {
            "thscode": thscode,
            "date_ms": hx.date_to_ms(day),
            "open_price": close,
            "high_price": close,
            "low_price": close,
            "close_price": close,
            "volume": 1000.0,
            "turnover": close * 1000,
        }
        for thscode, day, close in rows
    ])
    frame.to_parquet(path, index=False)


def _write_event_parquet(path, rows: list[tuple[str, str, float]]) -> None:
    """写一个和 adjustment-factors dump 同结构的 Parquet（第三个字段是每股分红）。"""
    frame = pd.DataFrame([
        {
            "thscode": thscode,
            "ex_date_ms": hx.date_to_ms(day),
            "dividend_per_share": dividend,
            "per_share_bonus": 0.0,
            "allotment_ratio": 0.0,
            "allotment_price": 0.0,
        }
        for thscode, day, dividend in rows
    ])
    frame.to_parquet(path, index=False)


@pytest.fixture()
def dumps(cfg):
    """预置好两个 dump。

    两处都放一份：
    - `<data_dir>/dumps/`：模拟"上次已下好"，用来测**复用/续传**；
    - `<data_dir>/dump_source/`：给 FakeClient 当"远端"，模拟 force 重下（日更每次强制重下）。
    """
    # 写/读 Parquet 需要 pyarrow（pyproject 已声明）。开发机上没装时，
    # 依赖本 fixture 的用例**整组跳过**，而不是报一堆误导性的失败。
    pytest.importorskip("pyarrow", reason="未安装 pyarrow，跳过 dump 相关用例")
    cfg.ensure_dirs()
    source = cfg.data_dir / "dump_source"
    source.mkdir(parents=True, exist_ok=True)
    days = [f"2026-01-{d:02d}" for d in range(1, 11)]
    daily_rows = [("600519.SH", day, 100.0 + i * 1.0) for i, day in enumerate(days)]
    event_rows = [("600519.SH", days[5], 1.0)]      # 第 6 日分红 1 元

    daily = cfg.dump_dir / "daily-k.parquet"
    events = cfg.dump_dir / "adjustment-factors.parquet"
    _write_daily_parquet(daily, daily_rows)
    _write_event_parquet(events, event_rows)
    _write_daily_parquet(source / "daily-k.parquet", daily_rows)
    _write_event_parquet(source / "adjustment-factors.parquet", event_rows)
    _write_daily_parquet(source / "daily-k-10d.parquet", daily_rows)
    return {
        "days": days,
        "daily": daily,
        "events": events,
        "source": source,
        "files": {
            "daily-k": source / "daily-k.parquet",
            "daily-k-10d": source / "daily-k-10d.parquet",
            "adjustment-factors": source / "adjustment-factors.parquet",
        },
    }


# ── download_history ──


def test_download_history_writes_rows_events_and_factors(cfg, dumps) -> None:
    stages: list[str] = []

    def progress(stage, done, total):
        stages.append(stage)

    result = sync.download_history(
        cfg, client=FakeClient(), progress_cb=progress, include_names=False
    )
    assert result.ok, result.error
    assert result.rows == 10
    assert result.extra["events"] == 1
    assert result.extra["factors"] == 1

    with storage.connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT date, close, factor FROM stock_daily_raw ORDER BY date"
        ).fetchall()
        hfq = conn.execute(
            "SELECT date, close FROM stock_daily_hfq ORDER BY date"
        ).fetchall()
        events = conn.execute("SELECT COUNT(*) FROM adjust_event").fetchone()[0]

    assert events == 1
    # 除权日之前 factor=1（最早的价格保持真实）
    assert rows[0]["factor"] == pytest.approx(1.0)
    assert rows[4]["factor"] == pytest.approx(1.0)
    # 除权日当天及之后：k = 前收 / (前收 − 1)
    prev_close = 100.0 + 4 * 1.0
    k = prev_close / (prev_close - 1.0)
    assert rows[5]["factor"] == pytest.approx(k)
    assert rows[9]["factor"] == pytest.approx(k)
    # 后复权价 = 原始价 × factor，序列在除权日连续
    assert hfq[4]["close"] == pytest.approx(104.0)
    assert hfq[5]["close"] == pytest.approx(105.0 * k)
    assert hfq[5]["close"] == pytest.approx(105.0 * prev_close / (prev_close - 1.0))
    # 进度回调被调用过，且阶段名是可读的中文
    assert stages and any("写入" in s or "下载" in s or "复权" in s for s in stages)


def test_download_history_is_resumable_and_idempotent(cfg, dumps) -> None:
    """**可中断续传**：第二次跑不重复写任何行（已入库的 (symbol,date) 全部跳过）。"""
    first = sync.download_history(cfg, client=FakeClient(), include_names=False)
    assert first.ok and first.rows == 10

    second = sync.download_history(cfg, client=FakeClient(), include_names=False)
    assert second.ok
    assert second.rows == 0
    assert second.extra["skipped"] == 10
    with storage.connect(cfg.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0]
    assert total == 10   # 没有重复行


def test_download_history_resumes_after_partial_write(cfg, dumps) -> None:
    """模拟"写到一半断了"：库里已有前 4 天，重跑只补后 6 天。"""
    days = dumps["days"]
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            ("600519", day, 100.0, 100.0, 100.0, 100.0, 1000.0, 1e5, 1.0)
            for day in days[:4]
        ])
    result = sync.download_history(cfg, client=FakeClient(), include_names=False)
    assert result.ok
    assert result.rows == 6            # 只补缺的 6 天
    assert result.extra["skipped"] == 4


def test_download_history_symbol_limit(cfg, dumps) -> None:
    """试跑模式：只处理前 N 只股票。"""
    result = sync.download_history(
        cfg, client=FakeClient(), include_names=False, symbol_limit=1
    )
    assert result.ok
    assert result.rows == 10


def test_download_history_without_key_returns_structured_failure(cfg) -> None:
    """没配 Key：返回结构化失败结果，**不抛异常**（界面层不该崩）。"""
    cfg.hithink_api_key = ""
    result = sync.download_history(cfg, include_names=False)
    assert result.ok is False
    assert "API Key" in result.error
    assert result.message.startswith("下载历史数据失败")


def test_download_history_returns_failure_on_api_error(cfg) -> None:
    """接口报错（Key 失效/网络故障）：同样是结构化失败，不抛异常。"""
    client = FakeClient(fail_with=hx.HithinkAuthError(2003, "模拟 Key 失效（测试假客户端）"))
    # 没有任何本地缓存 dump → 必然走到 client.download_dump → 抛错 → 转成失败结果
    result = sync.download_history(cfg, client=client, include_names=False)
    assert result.ok is False
    assert "2003" in result.error or "模拟 Key 失效" in result.error


def test_progress_callback_exception_does_not_break_download(cfg, dumps) -> None:
    """界面回调写错不该让下载失败（最常见的翻车点）。"""

    def broken(stage, done, total):
        raise RuntimeError("界面回调炸了")

    result = sync.download_history(cfg, client=FakeClient(), progress_cb=broken,
                                   include_names=False)
    assert result.ok
    assert result.rows == 10


# ── 日更 ──


def test_sync_daily_adds_only_new_rows(cfg, dumps, trading_days) -> None:
    """日更：先建全量，再用 daily-k-10d 补一天，验证只写新行且因子延续。"""
    sync.download_history(cfg, client=FakeClient(), include_names=False)

    # 新的 10 日 dump：包含老日期（应跳过）+ 一天新日期（应写入）
    days = dumps["days"] + ["2026-01-11"]
    _write_daily_parquet(dumps["files"]["daily-k-10d"], [
        ("600519.SH", day, 100.0 + i * 1.0) for i, day in enumerate(days)
    ])
    client = FakeClient(
        dump_files=dumps["files"],
        limit_up=[
            {"thscode": "600519.SH", "name": "贵州样本", "continue_day_cnt": 1,
             "seal_money": 8e7, "last_price": 110.0, "limit_up_reason": "白酒"},
        ],
    )
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, trading_days)

    result = sync.sync_daily(cfg, client=client, day="2026-01-11")
    assert result.ok, result.error
    assert result.rows == 1
    assert result.extra["skipped"] == 10
    assert result.extra["limit_up"] == 1

    with storage.connect(cfg.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0]
        latest = conn.execute(
            "SELECT factor FROM stock_daily_raw WHERE date = '2026-01-11'"
        ).fetchone()[0]
        limit = conn.execute("SELECT name, high_days FROM limit_up_pool").fetchone()
    assert total == 11
    # 延续除权后的因子（不是重置为 1.0）
    assert latest == pytest.approx(104.0 / 103.0, rel=1e-9)
    assert limit["name"] == "贵州样本"
    assert limit["high_days"] == 1


def test_download_history_chunks_large_symbol_sets(cfg, dumps) -> None:
    """股票数超过分片阈值（200）时，续传判断要走多批 —— 行数不能少、也不能重复写。"""
    days = dumps["days"][:5]
    rows = [
        (f"{600000 + i:06d}.SH", day, 10.0 + i * 0.1)
        for i in range(250)          # > 200，触发中途 flush
        for day in days
    ]
    _write_daily_parquet(dumps["files"]["daily-k"], rows)
    # 复权事件 dump 留一条（0 行的 dump 会被判为"不完整"，见 dump_is_usable）
    _write_event_parquet(dumps["files"]["adjustment-factors"],
                         [("600000.SH", days[2], 0.2)])

    # 清掉本地缓存，强制从（假）远端重新下载
    (cfg.dump_dir / "daily-k.parquet").unlink()
    (cfg.dump_dir / "adjustment-factors.parquet").unlink()
    client = FakeClient(dump_files=dumps["files"])

    first = sync.download_history(cfg, client=client, include_names=False)
    assert first.ok, first.error
    assert first.rows == 250 * len(days)
    with storage.connect(cfg.db_path) as conn:
        symbols = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily_raw"
        ).fetchone()[0]
    assert symbols == 250

    second = sync.download_history(cfg, client=FakeClient(), include_names=False)
    assert second.rows == 0
    assert second.extra["skipped"] == 250 * len(days)


def test_sync_daily_without_key_returns_failure(cfg) -> None:
    cfg.hithink_api_key = ""
    result = sync.sync_daily(cfg)
    assert result.ok is False
    assert result.error


def test_sync_daily_limit_up_failure_does_not_fail_daily(cfg, dumps) -> None:
    """涨停池失败不能影响行情写入（best-effort）。"""
    sync.download_history(cfg, client=FakeClient(), include_names=False)
    _write_daily_parquet(dumps["files"]["daily-k-10d"],
                         [("600519.SH", "2026-01-11", 111.0)])

    class LimitUpBoom(FakeClient):
        """行情正常、只有涨停池接口报错。"""

        def limit_up_pool(self, day=None, size=200):
            self.calls.append(("limit_up_pool", day))
            raise hx.HithinkError(5001, "模拟涨停池故障（测试假客户端）")

    client = LimitUpBoom(dump_files=dumps["files"])
    result = sync.sync_daily(cfg, client=client, day="2026-01-11", include_calendar=False)
    assert result.ok is True
    assert result.extra["limit_up_ok"] is False
    assert "涨停池同步失败" in result.detail


# ── 日历 / 行业 / 指数 ──


def test_sync_calendar(cfg) -> None:
    client = FakeClient(trading_days=["2026-09-10", "2026-09-11"])
    result = sync.sync_calendar(cfg, client=client)
    assert result.ok and result.rows == 2
    with storage.connect(cfg.db_path) as conn:
        days = [r[0] for r in conn.execute("SELECT date FROM trading_calendar ORDER BY date")]
    assert days == ["2026-09-10", "2026-09-11"]


def test_sync_calendar_empty_response_is_failure(cfg) -> None:
    result = sync.sync_calendar(cfg, client=FakeClient(trading_days=[]))
    assert result.ok is False


def test_sync_industry_keeps_stock_name_when_member_name_missing(cfg, db) -> None:
    """行业同步只覆盖 industry 列，不能把已缓存的中文名冲掉。"""

    class IndustryClient(FakeClient):
        def ths_index_list(self, tag="industry"):
            return [{"thscode": "881101.SH", "name": "半导体"},
                    {"thscode": "884001.SH", "name": "二级行业（应被忽略）"}]

        def ths_constituents(self, thscode):
            if thscode == "881101.SH":
                return [{"thscode": "600002.SH", "name": ""},
                        {"thscode": "600003.SH", "name": "半导体乙"}]
            return [{"thscode": "600001.SH", "name": "不该生效"}]

    result = sync.sync_industry(cfg, client=IndustryClient(), force=True)
    assert result.ok, result.error
    assert result.extra["industries"] == 1        # 只取 881* 一级行业
    with storage.connect(cfg.db_path) as conn:
        rows = dict(conn.execute("SELECT symbol, industry FROM stock_basic"))
        names = dict(conn.execute("SELECT symbol, name FROM stock_basic"))
    assert rows["600002"] == "半导体"
    assert rows["600003"] == "半导体"
    assert rows["600001"] == "银行"               # 二级行业没有覆盖它
    assert names["600002"] == "半导体甲"          # 名称保住了


def test_sync_industry_is_skipped_when_fresh(cfg, db) -> None:
    """覆盖率达标且 7 天内更新过 → 跳过（避免每次启动都拉 90 个板块）。"""
    with storage.connect(cfg.db_path) as conn:
        conn.execute(
            "UPDATE stock_basic SET updated_at = datetime('now','localtime') "
            "WHERE industry IS NOT NULL"
        )
        conn.commit()
    # 造一个会在被调用时抛错的客户端：跳过意味着根本不该调它
    client = FakeClient(fail_with=AssertionError("不该调用"))
    result = sync.sync_industry(cfg, client=client)
    assert result.ok and result.extra.get("skipped") is True


def test_sync_index_writes_bars(cfg) -> None:
    class IndexClient(FakeClient):
        def index_historical(self, thscode, start, end, interval="1d"):
            return [{"date_ms": hx.date_to_ms("2026-09-11"), "open_price": 1.0,
                     "high_price": 2.0, "low_price": 0.5, "close_price": 1.5,
                     "volume": 10.0, "turnover": 100.0}]

    result = sync.sync_index(cfg, client=IndexClient())
    assert result.ok and result.rows == len(sync.DEFAULT_INDICES)
    with storage.connect(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT close FROM index_daily WHERE symbol = 'sh.000300'"
        ).fetchone()
    assert row[0] == 1.5


def test_sync_failures_are_never_raised(cfg) -> None:
    """所有对外同步函数都必须把异常转成 SyncResult（硬性要求）。

    注意：这里抛的是**假客户端合成的**错误（消息里带"模拟…"字样），
    日志里出现"限流/5001"不代表真的打了同花顺接口 —— 测试套件的网络在
    socket 层就被封死了（见 tests/test_offline.py）。
    """
    boom = FakeClient(fail_with=hx.HithinkRateLimitError(5001, "模拟限流（测试假客户端）"))
    for fn in (sync.sync_calendar, sync.sync_industry, sync.sync_index,
               sync.sync_stock_names, sync.sync_limit_up_pool):
        result = fn(cfg, client=boom)
        assert isinstance(result, sync.SyncResult)
        assert result.ok is False
        assert result.error


def test_sync_limit_up_pool_field_aliases(cfg) -> None:
    """官方 REST 与网页接口字段名不同，别名要都能吃下。"""
    client = FakeClient(limit_up=[{
        "thscode": "600519.SH", "name": "贵州样本", "continue_day_cnt": 2,
        "seal_money": 1.5e8, "first_limit_up_time": 1757550600000,
        "limit_up_reason": "白酒", "is_new": True, "last_price": 1680.0,
    }])
    result = sync.sync_limit_up_pool(cfg, client=client, day="2026-09-11")
    assert result.ok and result.rows == 1
    with storage.connect(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM limit_up_pool").fetchone()
    assert row["symbol"] == "600519"
    assert row["high_days"] == 2
    assert row["order_amount"] == 1.5e8          # seal_money → order_amount
    assert row["reason_type"] == "白酒"          # limit_up_reason → reason_type
    assert row["is_new"] == 1
    assert row["last_price"] == 1680.0


def test_sync_limit_up_pool_accepts_web_field_names(cfg) -> None:
    client = FakeClient(limit_up=[{
        "ticker": "000001", "name": "平安样本", "high_days": 1,
        "order_amount": 5e7, "reason_type": "银行",
    }])
    result = sync.sync_limit_up_pool(cfg, client=client, day="2026-09-11")
    assert result.ok and result.rows == 1
    with storage.connect(cfg.db_path) as conn:
        row = conn.execute("SELECT symbol, high_days FROM limit_up_pool").fetchone()
    assert row["symbol"] == "000001" and row["high_days"] == 1


def test_daily_update_collects_results(cfg) -> None:
    """日常入口：逐项独立，返回每项结果（没 Key 时也只返回失败项，不抛异常）。"""
    cfg.hithink_api_key = ""
    results = sync.daily_update(cfg)
    assert results and all(isinstance(r, sync.SyncResult) for r in results)
    assert all(r.ok is False for r in results)
