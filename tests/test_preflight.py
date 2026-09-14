"""运行时自检：三态判据、`ready` 不重复下载、CLI 闸门、首次向导。

覆盖需求「运行时自检：有 10 年库就不下载，没有就先下载再运行」的全部验收点。
全部离线：用合成库 + 假客户端，`conftest._block_network` 还会把 socket 封死。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader.config import Config
from laoa_trader.data import preflight, storage
from tests.conftest import seed_ready_db


def _trading_days(count: int, end: str = "2026-09-11") -> list[str]:
    """count 个工作日（跳过周末），升序。"""
    last = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[str] = []
    cursor = last
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _cfg(tmp_path, **kwargs) -> Config:
    """默认用"宽松阈值"（小样本库也能 ready）；需要验证默认值的用例另行断言。"""
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.min_history_years = kwargs.pop("min_history_years", 0.0)
    cfg.min_symbols = kwargs.pop("min_symbols", 1)
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    cfg.ensure_dirs()
    return cfg


# ── 默认阈值 ──


def test_default_thresholds_match_spec() -> None:
    """默认：导入 5 年、跨度门槛 4.5 年（否则 5 年的库会被永远判不足）。"""
    cfg = Config()
    assert cfg.history_years == 5
    assert cfg.min_history_years == 4.5
    assert cfg.min_history_years < cfg.history_years      # 联动约束
    assert cfg.history_warning() == ""
    assert cfg.min_symbols == 4000
    assert cfg.max_stale_trading_days == 0
    assert cfg.auto_download_on_start is True


def test_default_config_needs_full_without_history(tmp_path) -> None:
    """用**真实默认阈值**跑空库：必须是 needs_full（且不做任何下载）。"""
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert result["needs_download"] == preflight.DOWNLOAD_FULL
    assert "没有" in result["reason"] or "空" in result["reason"]


# ── 三态判据：needs_full 的每个分支 ──


def test_empty_db_needs_full(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    storage.init_db(cfg.db_path)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "空" in result["reason"]
    assert result["rows"] == 0


def test_missing_table_needs_full(tmp_path) -> None:
    """老库缺表（例如没有 watchlist/push_log）→ 先把表补上再判断，但内容仍不足。"""
    cfg = _cfg(tmp_path)
    import sqlite3

    with sqlite3.connect(cfg.db_path) as conn:      # 只建一张表，模拟残缺库
        conn.execute("CREATE TABLE stock_daily_raw (symbol TEXT, date TEXT)")
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    # connect() 会自动补建缺失的表，所以这里不会报"缺表"，而是报"空/数据不足"
    assert "缺表" not in result["reason"]


def test_short_history_needs_full(tmp_path) -> None:
    """跨度不足（这里用 30 天、门槛 9 年）→ needs_full。"""
    cfg = _cfg(tmp_path, min_history_years=9, history_years=10)
    seed_ready_db(cfg, days=30)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "历史跨度" in result["reason"]


def test_too_few_symbols_needs_full(tmp_path) -> None:
    cfg = _cfg(tmp_path, min_symbols=5000)
    seed_ready_db(cfg, days=30)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "只股票" in result["reason"]


def test_contradictory_history_settings_reported(tmp_path) -> None:
    """min_history_years ≥ history_years → 中文提示，且**不下载**（下完照样判不足）。"""
    cfg = _cfg(tmp_path, min_history_years=9, history_years=5)
    storage.init_db(cfg.db_path)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "配置矛盾" in result["reason"]
    assert "min_history_years" in result["reason"] and "history_years" in result["reason"]
    assert result.get("config_problem")

    proceed, _, results = preflight.ensure_ready(cfg, auto_download=True)
    assert proceed is False
    assert results == []          # 没有白下载


def test_missing_adjust_events_needs_full(tmp_path) -> None:
    """9.5 年历史但**缺复权事件** → needs_full（复权缺失会让价格算错，不能算就绪）。"""
    cfg = _cfg(tmp_path, min_history_years=0)
    seed_ready_db(cfg, days=400, with_events=False)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert result["has_adjust_events"] is False
    assert "复权事件" in result["reason"]


def test_low_industry_coverage_needs_full(tmp_path) -> None:
    """行业覆盖 25% → 判不可用，但**指路必须是"点【刷新数据】"**（不是重下历史）。

    为什么这条值得改口径：行业归属是目录类数据（1 次目录 + 约 90 次成分请求），
    点一下【刷新数据】几秒钟就补上；以前这里一律给 `needs_download = full`，
    界面/CLI 就照着"下载数据"指路 —— 用户真的被引去重下 180MB（实报 bug）。
    """
    cfg = _cfg(tmp_path)
    seed_ready_db(cfg, symbols=(
        ("600001", "甲", "银行"), ("600002", "乙", ""), ("600003", "丙", ""), ("600004", "丁", ""),
    ), days=30)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert result["industry_coverage"] == pytest.approx(0.25)
    assert "行业归属未同步" in result["reason"]
    # 关键：指路指向【刷新数据】，而且 needs_download 不再是 full
    assert "【刷新数据】" in result["reason"]
    assert preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT
    assert result["light_only"] is True
    assert preflight.summary_line(result).startswith("本地数据缺轻量项：")


def test_missing_calendar_needs_full(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    seed_ready_db(cfg, days=30)
    with storage.connect(cfg.db_path) as conn:
        conn.execute("DELETE FROM trading_calendar")
        conn.commit()
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "交易日历" in result["reason"]
    assert result["has_calendar"] is False


# ── 三态判据：ready / needs_incremental ──


def test_ready_when_everything_ok(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40, symbols=(
        ("600001", "甲", "银行"), ("600002", "乙", "半导体"), ("600003", "丙", "白酒"),
    ))
    result = preflight.check(cfg.db_path, cfg, today=trading[-1])
    assert result["status"] == preflight.READY
    assert result["needs_download"] == preflight.DOWNLOAD_NONE
    assert result["stale_trading_days"] == 0
    assert result["symbols"] == 3
    assert result["has_adjust_events"] is True
    assert result["industry_coverage"] == 1.0
    assert result["has_calendar"] is True
    assert result["span_years"] > 0
    assert result["latest_date"] == trading[-1]
    assert "就绪" in preflight.summary_line(result)


def test_stale_three_days_needs_incremental(tmp_path) -> None:
    """数据齐、落后 3 个交易日 → needs_incremental（增量 1 次请求就够）。"""
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40)
    later = preflight._trading_days_after(trading[-1], 3)   # 之后又开盘了 3 天
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, later)
    result = preflight.check(cfg.db_path, cfg, today=later[-1])
    assert result["status"] == preflight.NEEDS_INCREMENTAL
    assert result["needs_download"] == preflight.DOWNLOAD_INCREMENTAL
    assert result["stale_trading_days"] == 3
    assert "落后 3 个交易日" in result["reason"]
    assert "3 个交易日前" in preflight.summary_line(result)


def test_stale_thirty_days_needs_full(tmp_path) -> None:
    """**关键分支**：落后 30 个交易日 → needs_full（daily-k-10d 补不回来）。"""
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40)
    later = preflight._trading_days_after(trading[-1], 30)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, later)
    result = preflight.check(cfg.db_path, cfg, today=later[-1])
    assert result["status"] == preflight.NEEDS_FULL
    assert result["needs_download"] == preflight.DOWNLOAD_FULL
    assert result["stale_trading_days"] == 30
    assert "增量补不回来" in result["reason"]
    assert "10" in result["reason"]                  # 说明为什么（窗口只有 10 天）


@pytest.mark.parametrize("stale", [10, 11])
def test_incremental_window_boundary(tmp_path, stale: int) -> None:
    """边界：10 天还能增量，11 天就必须全量（增量 dump 只覆盖近 10 个交易日）。"""
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40)
    extra = preflight._trading_days_after(trading[-1], stale)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, extra)
    result = preflight.check(cfg.db_path, cfg, today=extra[-1])
    assert result["stale_trading_days"] == stale
    if stale <= preflight.INCREMENTAL_WINDOW_DAYS:
        assert result["status"] == preflight.NEEDS_INCREMENTAL
    else:
        assert result["status"] == preflight.NEEDS_FULL
        assert result["needs_download"] == preflight.DOWNLOAD_FULL


def test_max_stale_trading_days_tolerates_lag(tmp_path) -> None:
    """`max_stale_trading_days>0`：落后该范围内仍算 ready（用户接受略旧的数据）。"""
    cfg = _cfg(tmp_path, max_stale_trading_days=3)
    trading = seed_ready_db(cfg, days=40)
    lag3 = preflight._trading_days_after(trading[-1], 3)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, lag3)
    assert preflight.check(cfg.db_path, cfg, today=lag3[-1])["status"] == preflight.READY

    lag4 = preflight._trading_days_after(trading[-1], 4)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, lag4)
    result = preflight.check(cfg.db_path, cfg, today=lag4[-1])
    assert result["stale_trading_days"] == 4
    assert result["status"] == preflight.NEEDS_INCREMENTAL


def test_check_is_local_only(tmp_path, monkeypatch) -> None:
    """自检必须是纯本地：把 make_client 换成"一调用就报错"，自检仍然通过。"""
    cfg = _cfg(tmp_path)
    seed_ready_db(cfg, days=30)

    def boom(*a, **k):
        raise AssertionError("自检不该创建任何客户端/发任何请求")

    monkeypatch.setattr("laoa_trader.data.hithink.HithinkClient", boom)
    monkeypatch.setattr("laoa_trader.data.sync.make_client", boom)
    assert preflight.check(cfg.db_path, cfg)["status"] in (
        preflight.READY, preflight.NEEDS_INCREMENTAL, preflight.NEEDS_FULL)


# ── ensure_ready：自动补齐策略 ──


def test_ensure_ready_ready_sends_no_request(tmp_path, monkeypatch) -> None:
    """ready：一次 dump 请求都不发（这是需求的核心：有库就不下载）。"""
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40)
    calls: list[str] = []

    def fake_daily_update(*a, **k):
        calls.append("daily_update")
        return []

    def fake_download_history(*a, **k):
        calls.append("download_history")
        return None

    monkeypatch.setattr("laoa_trader.data.sync.daily_update", fake_daily_update)
    monkeypatch.setattr("laoa_trader.data.sync.download_history", fake_download_history)
    proceed, result, results = preflight.ensure_ready(cfg, auto_download=True, today=trading[-1])
    assert proceed is True
    assert result["status"] == preflight.READY
    assert calls == []                  # 什么都没调用
    assert results == []


def test_ensure_ready_incremental_auto(tmp_path, monkeypatch) -> None:
    """needs_incremental + auto_download_on_start → 自动跑一次增量。"""
    cfg = _cfg(tmp_path)
    trading = seed_ready_db(cfg, days=40)
    later = preflight._trading_days_after(trading[-1], 2)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, later)
    calls: list[str] = []
    monkeypatch.setattr("laoa_trader.data.sync.sync_daily",
                        lambda *a, **k: calls.append("sync_daily") or
                        __import__("laoa_trader.data.sync", fromlist=["x"]).SyncResult(
                            stage="日更数据", ok=True, rows=1, detail="增量 1 行"))
    proceed, result, results = preflight.ensure_ready(cfg, today=later[-1])
    assert proceed is True
    assert calls == ["sync_daily"]
    assert results and results[0].ok


def test_ensure_ready_incremental_respects_config_off(tmp_path, monkeypatch) -> None:
    """auto_download_on_start=false 且没有显式 auto_download → **不下载**，只提示。"""
    cfg = _cfg(tmp_path, auto_download_on_start=False)
    trading = seed_ready_db(cfg, days=40)
    later = preflight._trading_days_after(trading[-1], 2)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, later)
    monkeypatch.setattr("laoa_trader.data.sync.sync_daily",
                        lambda *a, **k: pytest.fail("不该自动下载"))
    proceed, result, results = preflight.ensure_ready(cfg, today=later[-1])
    assert proceed is True                      # 允许继续用（但结论基于旧数据）
    assert result["status"] == preflight.NEEDS_INCREMENTAL
    assert results == []


def test_ensure_ready_needs_full_requires_explicit_consent(tmp_path, monkeypatch) -> None:
    """needs_full 时不擅自全量下载（20 分钟的大动作必须用户同意）。"""
    cfg = _cfg(tmp_path, min_history_years=9, history_years=10)   # 空库必然 needs_full
    storage.init_db(cfg.db_path)
    monkeypatch.setattr("laoa_trader.data.sync.download_history",
                        lambda *a, **k: pytest.fail("没有明确同意就不该下载"))
    proceed, result, results = preflight.ensure_ready(cfg, auto_download=False)
    assert proceed is False
    assert result["status"] == preflight.NEEDS_FULL
    assert results == []


def test_ensure_ready_needs_full_with_consent_downloads(tmp_path, monkeypatch) -> None:
    """给了明确同意 → 先下载再重查自检。"""
    cfg = _cfg(tmp_path, min_history_years=9, history_years=10)
    storage.init_db(cfg.db_path)
    order: list[str] = []

    def fake_download(cfg_, **kwargs):
        order.append("download")
        seed_ready_db(cfg_, days=400)          # 模拟"下好了"
        from laoa_trader.data.sync import SyncResult

        return SyncResult(stage="下载历史数据", ok=True, rows=100, detail="写入 100 行")

    monkeypatch.setattr("laoa_trader.data.sync.download_history", fake_download)
    proceed, result, results = preflight.ensure_ready(cfg, auto_download=True)
    assert order == ["download"]
    assert proceed is True
    assert results and results[0].ok
    # 下载后重新自检：因为阈值要求 9 年而样本只有 1 年多，仍然是 needs_full（如实反映）
    assert result["status"] in (preflight.READY, preflight.NEEDS_FULL)


def test_ensure_ready_cancel_is_respected(tmp_path, monkeypatch) -> None:
    """取消回调要能传下去（界面向导的"取消下载"）。"""
    cfg = _cfg(tmp_path, min_history_years=9, history_years=10)
    storage.init_db(cfg.db_path)
    seen: dict = {}

    def fake_download(cfg_, progress_cb=None, should_stop=None, **kwargs):
        seen["should_stop"] = should_stop
        from laoa_trader.data.sync import SyncResult

        return SyncResult(stage="下载历史数据", ok=False, error="已取消")

    monkeypatch.setattr("laoa_trader.data.sync.download_history", fake_download)
    stopped = {"value": False}
    proceed, result, results = preflight.ensure_ready(
        cfg, auto_download=True, should_stop=lambda: stopped["value"]
    )
    assert callable(seen["should_stop"])
    assert seen["should_stop"]() is False
    stopped["value"] = True
    assert seen["should_stop"]() is True
    assert proceed is True                     # 取消不算致命：调用方据此停止即可
    assert results[0].ok is False


def test_check_never_raises_on_garbage_db(tmp_path) -> None:
    """库文件损坏（不是 SQLite）也不能抛异常 —— 自检本身必须稳。"""
    cfg = _cfg(tmp_path)
    cfg.db_path.write_bytes(b"this is not a sqlite file")
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert result["reason"]
