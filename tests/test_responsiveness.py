"""导入/刷新期间界面不卡：让出 GIL、进度节流、概况查询瘦身（全部离线、无 Qt）。

为什么单独一份用例
------------------
用户实报"下载数据时界面卡死"。三个叠加原因，各有各的验证方式：

1. **界面每 5 秒做全表扫描**：`data_summary()` 里两条重活（`COUNT(DISTINCT symbol)`
   要在几百万行的行情表上做全表去重）。这里钉住"symbols 不再走全表去重"
   （用 `set_trace_callback` 抓**实际执行的 SQL**，不是匹配源码字符串）；
2. **导入循环长时间占着 GIL**：读 Parquet / 清洗 / 逐批写库都是纯 CPU 的 Python/C 代码，
   不释放 GIL 时主线程连事件循环都转不动。这里钉住"导入循环按批调用 `_yield_gui()`"
   （次数与批次相称，而不是只调一次）；
3. **进度回调太密**：10 年库能发几千次，每次都跨线程排队 + 重绘。这里钉住节流门限
   （首末必发、百分比单调不减、总量被削掉一个数量级）。

界面层那两条（下载中跳过重活、概况 TTL 缓存）需要 QApplication，
放在 `tests/test_ui_smoke.py`（那边本来就有离屏 Qt 的脚手架）。
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import pytest

from laoa_trader import state
from laoa_trader.config import Config
from laoa_trader.data import hithink as hx
from laoa_trader.data import storage, sync
from tests.conftest import FakeClient


def _cfg(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    # `_write_dump` 造的行情从 2024-01-01 开始（写死的），而默认导入窗口已经是
    # **0.5 年（6 个月）**——不关掉窗口就会被过滤成空、整个文件红。
    # 这一组测的是"GIL/进度"，与导入窗口无关（窗口另有 test_history_window.py）。
    cfg.history_years = 0
    cfg.ensure_dirs()
    return cfg


def _write_dump(cfg, symbols: int, days: int) -> int:
    """写一个"多只股票 × 多天"的日K dump，返回总行数。"""
    base = date(2024, 1, 1)
    rows = []
    for i in range(symbols):
        symbol = f"{600000 + i:06d}"
        for d in range(days):
            day = (base + timedelta(days=d)).isoformat()
            rows.append({
                "thscode": f"{symbol}.SH",
                "date_ms": hx.date_to_ms(day),
                "open_price": 10.0, "high_price": 10.0, "low_price": 10.0,
                "close_price": 10.0 + d * 0.01, "volume": 1000.0, "turnover": 1e7,
            })
    pd.DataFrame(rows).to_parquet(cfg.dump_dir / "daily-k.parquet", index=False)
    pd.DataFrame([{
        "thscode": f"{600000:06d}.SH", "ex_date_ms": hx.date_to_ms(base.isoformat()),
        "dividend_per_share": 0.1, "per_share_bonus": 0.0,
        "allotment_ratio": 0.0, "allotment_price": 0.0,
    }]).to_parquet(cfg.dump_dir / "adjustment-factors.parquet", index=False)
    return len(rows)


# ── ① 数据概况：symbols 不再全表去重 ──


def test_data_summary_symbols_does_not_scan_the_daily_table(cfg) -> None:
    """`symbols` 取 `stock_basic` 的行数（O(1)），不再是行情表的全表去重。

    断言的是**实际执行的 SQL**（`set_trace_callback` 抓的），
    不是源码里的字符串 —— 后者太脆（注释里也有那串字）。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "甲", "银行"), ("600002", "乙", ""), ("600003", "丙", ""),
        ])
        # 故意只给其中两只写行情：旧实现会把 symbols 数成 2，新实现按"已知股票数"数成 3
        storage.write_daily_raw(conn, [
            ("600001", "2026-09-11", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            ("600002", "2026-09-11", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        ])
        executed: list[str] = []
        conn.set_trace_callback(executed.append)
        summary = storage.data_summary(conn)
        conn.set_trace_callback(None)

    assert summary["daily_rows"] == 2
    assert summary["symbols"] == 3              # 语义：库里已知的股票数（含没行情的）
    heavy = [sql for sql in executed
             if "distinct" in sql.lower() and "symbol" in sql.lower()]
    assert heavy == [], f"symbols 又走全表去重了：{heavy}"
    assert any("count(*)" in sql.lower() and "stock_basic" in sql.lower()
               for sql in executed), "symbols 应当取自 stock_basic 的行数"


def test_data_summary_stays_cheap_on_a_big_table(tmp_path) -> None:
    """20 万行行情表上：`data_summary()` 不再因为 symbols 而全表去重（量化门槛）。"""
    cfg = _cfg(tmp_path)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [(f"{600000 + i:06d}", f"样本{i}", "银行")
                                        for i in range(500)])
        base = date(2020, 1, 1)
        batch = []
        for i in range(500):
            symbol = f"{600000 + i:06d}"
            for d in range(400):
                batch.append((symbol, (base + timedelta(days=d)).isoformat(),
                              10.0, 10.0, 10.0, 10.0, 1e6, 1e7, 1.0))
        storage.write_daily_raw(conn, batch)
        started = time.perf_counter()
        for _ in range(10):
            storage.data_summary(conn)
        per_call = (time.perf_counter() - started) / 10 * 1000
    assert per_call < 50, f"20 万行库上每次 {per_call:.1f} ms，太慢了"


# ── ② 导入循环让出 GIL ──


def test_import_loop_yields_the_gil_periodically(tmp_path, monkeypatch) -> None:
    """导入循环按批让出 GIL：`_yield_gui()` 被调用**多次**（与批次相称），不是一次。

    阈值故意调小（每 100 行让一次、每 500 行一批），这样小样本 dump 也能验出
    "循环里真的在按批让出"；真实值见 `sync.YIELD_EVERY_ROWS`（20 万行）。
    """
    monkeypatch.setattr(sync, "YIELD_EVERY_ROWS", 100)
    monkeypatch.setattr(sync, "READ_BATCH_ROWS", 500)
    yields = {"n": 0}
    monkeypatch.setattr(sync, "_yield_gui",
                        lambda: yields.__setitem__("n", yields["n"] + 1))

    def run(symbols: int, days: int) -> int:
        tag = f"s{symbols}d{days}"
        cfg = _cfg(tmp_path / tag)
        rows = _write_dump(cfg, symbols=symbols, days=days)
        before = yields["n"]
        result = sync.download_history(cfg, client=FakeClient(), include_names=False)
        assert result.ok, result.error
        assert result.rows == rows
        return yields["n"] - before

    # **同样的行数、不同的股票只数**：
    # 读 Parquet 的让出只跟行数有关（两种跑法一样），而**写库循环**是按批（逐股）让出的 ——
    # 只数多则批多、让出多。所以"只数多 → 让出多"这条差异只能来自写库循环；
    # 把循环里的 `_yield_gui()` 删掉，两次跑法就会一样多（这条用例随即变红）。
    few_symbols = run(4, 500)      # 4 只 × 500 天 = 2000 行
    many_symbols = run(20, 100)    # 20 只 × 100 天 = 2000 行（同样 2000 行）
    assert few_symbols >= 4, f"一次导入只让出了 {few_symbols} 次，太少"
    assert many_symbols > few_symbols, (
        f"同样 2000 行，只数从 4 涨到 20，让出次数没涨（{few_symbols} -> {many_symbols}）"
        "：写库循环里没有按批让出"
    )


def test_yield_gui_is_a_real_gil_release(monkeypatch) -> None:
    """`_yield_gui()` 必须真的睡一下（`time.sleep` 才会释放 GIL；`pass` 不会）。

    这一条防的是"把让出写成空函数"这种假修：`time.sleep(0.001)` 会让出 GIL，
    而空实现只是白白占一行代码。
    """
    slept: list[float] = []
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: slept.append(float(seconds)))
    sync._yield_gui()
    assert slept and slept[0] > 0


# ── ③ 进度节流 ──


def test_progress_throttle_cuts_the_signal_storm() -> None:
    """1000 次进度回调 → 发出的信号被削掉一个数量级；首末必发、百分比单调不减。"""
    sent: list[tuple[str, int, int]] = []
    callback = sync.throttle_progress(
        lambda stage, done, total: sent.append((stage, done, total))
    )
    total = 100_000
    for done in range(0, total + 1, 100):          # 1001 次，每次 0.1%
        callback("写入行情", done, total)

    assert sent[0][1] == 0                          # 首值一定发（否则像"没开始"）
    assert sent[-1][1] == total                     # 末值一定发（否则停在 99%）
    assert len(sent) <= 110, f"发了 {len(sent)} 次，节流没生效"
    assert len(sent) < 1001 / 5                     # 至少削掉 80%
    pcts = [done / tot for _, done, tot in sent]
    assert pcts == sorted(pcts)                     # 单调不减（进度条不会倒退）


def test_progress_throttle_always_sends_stage_switch_and_first_last() -> None:
    """阶段切换一定发（否则新阶段的进度条从头开始却不动）。"""
    sent: list[tuple[str, int, int]] = []
    callback = sync.throttle_progress(
        lambda stage, done, total: sent.append((stage, done, total))
    )
    callback("下载 daily-k", 0, 1000)
    callback("下载 daily-k", 1, 1000)               # 同阶段、同毫秒：被节流
    callback("写入行情", 1, 1000)                   # 换阶段：一定发
    callback("写入行情", 1000, 1000)                # 收尾：一定发
    assert [stage for stage, _, _ in sent] == ["下载 daily-k", "写入行情", "写入行情"]
    assert sent[-1][1] == 1000


def test_progress_throttle_has_a_time_rule() -> None:
    """百分比没变、但间隔到了 → 也要发一次（长阶段里"一直不动"看着像卡死）。"""
    sent: list[tuple[str, int, int]] = []
    callback = sync.throttle_progress(
        lambda stage, done, total: sent.append((stage, done, total)),
        min_interval=0.05,
    )
    callback("写入行情", 50, 1000)
    time.sleep(0.08)
    callback("写入行情", 50, 1000)
    assert len(sent) == 2


def test_download_history_throttles_the_caller_callback(tmp_path, monkeypatch) -> None:
    """真实下载路径也走节流：`download_history` 收到的回调被包了一层。"""
    cfg = _cfg(tmp_path)
    _write_dump(cfg, symbols=3, days=200)
    seen: list[tuple[str, int, int]] = []
    client = FakeClient()
    result = sync.download_history(
        cfg, client=client, include_names=False,
        progress_cb=lambda stage, done, total: seen.append((stage, done, total)),
    )
    assert result.ok, result.error
    stages = {stage for stage, _, _ in seen}
    assert stages, "一次进度都没发出来"
    # 节流之后同一阶段的次数远小于"每个批次一次"
    assert len(seen) <= 40, f"回调 {len(seen)} 次，没走节流"


def test_download_scope_flag_wraps_import(tmp_path) -> None:
    """导入期间 `state.is_downloading()` 为真（界面据此跳过重活）。"""
    cfg = _cfg(tmp_path)
    _write_dump(cfg, symbols=2, days=50)
    observed: list[bool] = []

    class Spy(FakeClient):
        def download_dump(self, tag="daily-k-10d", dest=None, **kwargs):
            observed.append(state.is_downloading())
            return super().download_dump(tag, dest=dest, **kwargs)

    result = sync.download_history(cfg, client=Spy(), include_names=False)
    assert result.ok, result.error
    assert observed == [True] * len(observed)      # 整个流程都在"下载中"
    assert state.is_downloading() is False         # 结束后旗子落下
