"""分发给别人时只导 5 年：导入按年过滤、复权事件全留、自检门槛联动。

覆盖需求「分发给别人时只下 5 年数据（默认）」的验收点：

1. `history_years=5` 时按日期过滤：跨 6 年的 dump 只写入最近 5 年；
2. 复权事件**不过滤**：窗口之前的除权事件仍参与因子计算（否则序列会差一个常数）；
3. 跨度 4.6 年 → `ready`；3 年 → `needs_full`（门槛是 4.5 年）；
4. `history_years=10` 时写满 10 年（回归保护）；
5. 配置矛盾（`min_history_years ≥ history_years`）→ 中文提示；
6. 摘要/`--doctor` 说清"实际导入了什么区间、多少行、多少年"。

全部离线：真实 Parquet（pyarrow 写）+ 假客户端，不联网。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from laoa_trader.config import Config, load_config
from laoa_trader.data import hithink as hx
from laoa_trader.data import preflight, storage, sync

from tests._toml import p
from tests.conftest import FakeClient, seed_ready_db, workdays_ending

# 本项目约定：缺 pyarrow 时整模块跳过，而不是让用例失败
pytest.importorskip("pyarrow")


def _write_daily(path, symbol: str, days: list[str], base: float = 10.0) -> None:
    """写一个和 daily-k dump 同结构的 Parquet。"""
    frame = pd.DataFrame([
        {
            "thscode": f"{symbol}.SH",
            "date_ms": hx.date_to_ms(day),
            "open_price": base + i * 0.01,
            "high_price": base + i * 0.01,
            "low_price": base + i * 0.01,
            "close_price": base + i * 0.01,
            "volume": 1000.0,
            "turnover": (base + i * 0.01) * 1000,
        }
        for i, day in enumerate(days)
    ])
    frame.to_parquet(path, index=False)


def _write_events(path, rows: list[tuple[str, str, float]]) -> None:
    frame = pd.DataFrame([
        {"thscode": f"{symbol}.SH", "ex_date_ms": hx.date_to_ms(day),
         "dividend_per_share": dividend, "per_share_bonus": 0.0,
         "allotment_ratio": 0.0, "allotment_price": 0.0}
        for symbol, day, dividend in rows
    ])
    frame.to_parquet(path, index=False)


def _daily_days(years: float, end: str | None = None, per_year: int = 4) -> list[str]:
    """构造跨越 `years` 年的交易日列表（每年若干个工作日，够算"跨度"即可）。"""
    today = datetime.strptime(end or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
    days: list[str] = []
    step = max(int(365 / per_year), 1)
    for i in range(int(years * per_year) + 1):
        days.append((today - timedelta(days=i * step)).strftime("%Y-%m-%d"))
    return sorted(dict.fromkeys(days))


@pytest.fixture()
def history_cfg(tmp_path, monkeypatch):
    """临时配置 + 预置 dump（调用方可覆盖 history_years）。"""
    for name in ("HISTORY_YEARS", "MIN_HISTORY_YEARS", "HITHINK_FINANCE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.ensure_dirs()
    return cfg


def _run_import(cfg, days: list[str], events=None, *, symbol: str = "600519"):
    """把 dump 放到缓存目录并跑一次 download_history（假客户端不会真下载）。"""
    _write_daily(cfg.dump_dir / "daily-k.parquet", symbol, days)
    _write_events(cfg.dump_dir / "adjustment-factors.parquet",
                  events or [(symbol, days[-1], 0.1)])
    return sync.download_history(cfg, client=FakeClient(), include_names=False)


# ── 1) 按年过滤 ──


def test_history_years_filters_old_rows(history_cfg) -> None:
    """跨 6 年的 dump：`history_years=5` 只写入最近 5 年，更早的行不入库。"""
    cfg = history_cfg
    cfg.history_years = 5
    days = _daily_days(6)                       # 覆盖 6 年
    result = _run_import(cfg, days)
    assert result.ok, result.error

    cutoff = (datetime.now() - timedelta(days=round(5 * 365.25))).strftime("%Y-%m-%d")
    with storage.connect(cfg.db_path) as conn:
        stored = [r[0] for r in conn.execute(
            "SELECT date FROM stock_daily_raw ORDER BY date")]
    assert stored, "应该有数据写入"
    assert min(stored) >= cutoff, f"写入了早于窗口的行：{min(stored)} < {cutoff}"
    assert any(day < cutoff for day in days)         # 造的数据里确实有更早的
    assert all(day >= cutoff for day in stored)
    # 摘要说清实际范围与过滤
    assert "本次导入 5 年" in result.detail
    assert "已按 history_years 过滤" in result.detail
    assert result.extra["history_years"] == 5
    assert result.extra["cutoff"] == cutoff
    assert result.extra["raw_rows"] == len(stored)
    assert result.extra["dump_rows"] > result.extra["raw_rows"]   # dump 原始更多


def test_history_years_ten_writes_full_range(history_cfg) -> None:
    """`history_years=10`（回归保护）：10 年的数据全部写入。"""
    cfg = history_cfg
    cfg.history_years = 10
    days = _daily_days(6)                       # 6 年 < 10 年 → 不该丢任何行
    result = _run_import(cfg, days)
    assert result.ok, result.error
    with storage.connect(cfg.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0]
    assert count == len(days)
    assert result.extra["history_years"] == 10
    assert result.extra["skipped"] == 0


def test_zero_history_years_imports_everything(history_cfg) -> None:
    """`history_years=0` = 不过滤（想导满 dump 里的全部）。"""
    cfg = history_cfg
    cfg.history_years = 0
    days = _daily_days(6)
    result = _run_import(cfg, days)
    assert result.ok
    with storage.connect(cfg.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0]
    assert count == len(days)
    assert result.extra["cutoff"] == ""


def test_load_raw_since_filters_before_pandas(history_cfg) -> None:
    """`load_raw(since=...)` 是过滤的关键实现：只回窗口内的行。"""
    cfg = history_cfg
    days = _daily_days(6)
    path = cfg.dump_dir / "daily-k.parquet"
    _write_daily(path, "600519", days)
    cutoff = (datetime.now() - timedelta(days=round(5 * 365.25))).strftime("%Y-%m-%d")

    full = sync.load_raw(path)
    cut = sync.load_raw(path, since=cutoff)
    assert len(full) == len(days)
    assert len(cut) < len(full)
    assert min(cut["date"]) >= cutoff


# ── 2) 复权事件不过滤 ──


def test_adjust_events_kept_outside_window(history_cfg) -> None:
    """窗口**之前**的除权事件仍要参与因子计算（否则序列会差一个常数）。

    做法：构造"除权日早于导入窗口"的事件，然后断言
    库里写进去的 factor 与"用全量数据算出来的 factor"完全一致。
    """
    cfg = history_cfg
    cfg.history_years = 5
    days = _daily_days(6)                       # 覆盖 6 年，最早几条在窗口之外
    # 除权日放在"窗口之外、但仍是 dump 里的第二天"：
    # 第一天没有前收（算不出因子），所以用第二天才检验得出"窗口外事件也被算进去"
    old_day = days[1]
    assert old_day < (datetime.now() - timedelta(days=round(5 * 365.25))
                      ).strftime("%Y-%m-%d")
    result = _run_import(cfg, days, events=[("600519", old_day, 0.5)])
    assert result.ok, result.error
    assert result.extra["events"] == 1

    # 事件表里必须保留这条（不管它在不在窗口内）
    with storage.connect(cfg.db_path) as conn:
        events = conn.execute(
            "SELECT symbol, ex_date FROM adjust_event ORDER BY ex_date").fetchall()
        stored = dict(conn.execute(
            "SELECT date, factor FROM stock_daily_raw ORDER BY date").fetchall())
    assert [ (e[0], e[1]) for e in events ] == [("600519", old_day)]

    # 用**全量**数据（不过滤）算一遍因子：两者必须一致
    raw_full = sync.load_raw(cfg.dump_dir / "daily-k.parquet")
    expected = sync.cumulative_factor(raw_full, sync.compute_adjust_factors(
        raw_full, sync.load_events(cfg.dump_dir / "adjustment-factors.parquet")))
    expected_map = dict(zip(expected["date"], expected["factor"], strict=False))
    for date, factor in stored.items():
        assert factor == pytest.approx(expected_map[date], rel=1e-12), date
    # 而且窗口内确实出现了 >1 的因子（说明窗口外那条事件起了作用）
    assert max(stored.values()) > 1.0


def test_events_not_filtered_by_date(history_cfg) -> None:
    """`load_events` 不做任何日期过滤（历史事件全留）。"""
    cfg = history_cfg
    days = _daily_days(6)
    path = cfg.dump_dir / "adjustment-factors.parquet"
    _write_events(path, [("600519", days[0], 0.3), ("600519", days[-1], 0.1)])
    events = sync.load_events(path)
    assert len(events) == 2
    assert set(events["ex_date"]) == {days[0], days[-1]}


# ── 3) 自检门槛联动 ──


def test_span_46_years_is_ready(tmp_path) -> None:
    """跨度 4.6 年 → ready（门槛 4.5）。"""
    cfg = Config(data_dir=tmp_path / "d1", hithink_api_key="")
    cfg.history_years, cfg.min_history_years, cfg.min_symbols = 5.0, 4.5, 1
    cfg.ensure_dirs()
    days = workdays_ending(datetime.now().strftime("%Y-%m-%d"), 1200)
    # 让最早一天距今 ~4.6 年
    start = (datetime.now() - timedelta(days=round(4.6 * 365.25))).strftime("%Y-%m-%d")
    days = [d for d in days if d >= start]
    seed_ready_db(cfg, days=len(days), trading_days=days)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.READY, result["reason"]
    assert result["span_years"] >= 4.5


def test_span_three_years_needs_full(tmp_path) -> None:
    """跨度 3 年 → needs_full（门槛 4.5）。"""
    cfg = Config(data_dir=tmp_path / "d2", hithink_api_key="")
    cfg.history_years, cfg.min_history_years, cfg.min_symbols = 5.0, 4.5, 1
    cfg.ensure_dirs()
    start = (datetime.now() - timedelta(days=round(3.0 * 365.25))).strftime("%Y-%m-%d")
    days = [d for d in workdays_ending(datetime.now().strftime("%Y-%m-%d"), 900)
            if d >= start]
    seed_ready_db(cfg, days=len(days), trading_days=days)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert "历史跨度" in result["reason"]
    assert "min_history_years" in result["reason"]      # 提示怎么改


# ── 4) 配置矛盾 ──


def test_config_linkage_defaults() -> None:
    cfg = Config()
    assert cfg.history_years == 3
    assert cfg.min_history_years == 2.5
    assert cfg.min_history_years < cfg.history_years
    assert cfg.history_warning() == ""


def test_config_linkage_warning_and_no_download(tmp_path) -> None:
    """`min_history_years ≥ history_years` → 中文提示，且**不下载**（下完照样判不足）。"""
    cfg = Config(data_dir=tmp_path / "d3", hithink_api_key="")
    cfg.history_years, cfg.min_history_years = 5.0, 9.0
    cfg.ensure_dirs()
    warning = cfg.history_warning()
    assert "配置矛盾" in warning
    assert "9" in warning and "5" in warning
    assert "4.5" in warning                       # 给出建议值

    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_FULL
    assert result["config_problem"] == warning

    proceed, _, results = preflight.ensure_ready(cfg, auto_download=True)
    assert proceed is False and results == []


def test_history_years_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HISTORY_YEARS", "10")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.history_years == 10
    monkeypatch.setenv("HISTORY_YEARS", "5")
    monkeypatch.setenv("MIN_HISTORY_YEARS", "4.5")
    cfg2 = load_config(tmp_path / "none.toml")
    assert (cfg2.history_years, cfg2.min_history_years) == (5, 4.5)
    assert cfg2.history_warning() == ""


# ── 5) 摘要与 doctor ──


def test_imported_range_helper(history_cfg) -> None:
    cfg = history_cfg
    cfg.history_years = 5
    days = _daily_days(6)
    _run_import(cfg, days)
    info = preflight.imported_range(cfg.db_path)
    assert info["rows"] > 0
    assert info["start"] and info["end"]
    assert info["span_years"] <= 5.1              # 过滤后不超过窗口


def test_doctor_shows_history_window(capsys, tmp_path, history_cfg) -> None:
    """`--doctor` 打印已导入区间 / 行数 / 覆盖年数 / history_years。"""
    from laoa_trader.__main__ import cli

    cfg = history_cfg
    cfg.history_years = 5
    cfg.min_history_years = 4.5
    cfg.min_symbols = 1
    cli_config = tmp_path / "config.toml"
    cli_config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n'
        "history_years = 5\nmin_history_years = 4.5\nmin_symbols = 1\n",
        encoding="utf-8",
    )
    days = workdays_ending(datetime.now().strftime("%Y-%m-%d"), 1200)
    start = (datetime.now() - timedelta(days=round(4.9 * 365.25))).strftime("%Y-%m-%d")
    days = [d for d in days if d >= start]
    seed_ready_db(cfg, days=len(days), trading_days=days)

    assert cli(["--cli", "--doctor", "--config", str(cli_config)]) == 0
    out = capsys.readouterr().out
    assert "已导入区间" in out
    assert "覆盖" in out and "年" in out
    assert "history_years: 5" in out
    assert f"({len(days)} 只股票)" in out or "只股票" in out


def test_doctor_reports_config_contradiction(capsys, tmp_path) -> None:
    from laoa_trader.__main__ import cli

    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{p(tmp_path / "d9")}"\n'
        "history_years = 5\nmin_history_years = 9\n",
        encoding="utf-8",
    )
    assert cli(["--cli", "--doctor", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "配置矛盾" in out
    assert "min_history_years" in out
