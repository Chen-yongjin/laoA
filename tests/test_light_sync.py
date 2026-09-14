"""轻量数据同步（交易日历 / 行业归属 / 指数）：下载收尾补齐 + 指路必须指对按钮。

为什么单独一份用例
------------------
用户实报过："提示行业覆盖率 0%，要求大于等于 90%" —— 根因是**首次下载只写了行情和
股票名称，压根没有同步行业归属**（行业同步只在【刷新数据】/日更里跑）。于是自检判
"数据不可用"，而指路文案写的是"点【下载数据】/--download"，把用户引去重下 180MB。

这一份用例盯三件事：
1. `download_history` 收尾**真的会补**轻量数据，而且是在**写行情之后**（顺序错了行业写不进去）；
2. 轻量项失败**不影响行情结果**，但 `detail` 要说清"点【刷新数据】可重试"；
3. 自检/闸门的**指路指向正确的按钮**（轻量项 → 【刷新数据】，不是【下载数据】），
   并且 `ensure_ready` 会**自动补一次再复查**，不让用户为了一行业归属去重下 10 年。

全部离线：假客户端 + 本地 parquet（`_block_network` 在 socket 层封了外呼）。
"""

from __future__ import annotations

import pytest

from laoa_trader import hints
from laoa_trader.config import Config
from laoa_trader.data import hithink as hx
from laoa_trader.data import preflight, storage, sync
from tests.conftest import FakeClient, seed_ready_db
from tests.test_history_window import _daily_days, _write_daily, _write_events

# ── 假客户端：在 conftest 的 FakeClient 上补"轻量数据"那三件事 ──


class LightFakeClient(FakeClient):
    """假客户端 + 日历/行业/指数三件套的假数据（行业只覆盖行情里有的那只）。"""

    #: 行情 dump 里的股票（与 `_write_daily` 用的代码一致）
    SYMBOL = "600519"

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("trading_days", ["2026-09-10", "2026-09-11"])
        super().__init__(**kwargs)

    def ths_index_list(self, tag: str = "industry"):  # noqa: D102 - 见类注释
        self._check("ths_index_list", tag)
        return [
            {"thscode": "881155.TI", "name": "银行"},
            {"thscode": "881157.TI", "name": "证券"},
        ]

    def ths_constituents(self, thscode: str):  # noqa: D102
        self._check("ths_constituents", thscode)
        return [{"thscode": f"{self.SYMBOL}.SH", "name": "贵州样本"}]

    def index_historical(self, thscode, start, end, interval="1d"):  # noqa: D102
        self._check("index_historical", thscode)
        return []


class IndustryFailClient(LightFakeClient):
    """行业目录取不到（其它都正常）：用来验证"轻量项失败不影响整体 ok"。"""

    def ths_index_list(self, tag: str = "industry"):  # noqa: D102
        self._check("ths_index_list", tag)
        raise hx.HithinkError(5001, "测试假客户端：行业目录取不到")


def _light_cfg(tmp_path, **kwargs) -> Config:
    """临时配置 + 一份能跑通 `download_history` 的 dump。"""
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    cfg.ensure_dirs()
    days = _daily_days(1)                     # 一年的样本，够算复权与入库
    _write_daily(cfg.dump_dir / "daily-k.parquet", LightFakeClient.SYMBOL, days)
    _write_events(cfg.dump_dir / "adjustment-factors.parquet",
                  [(LightFakeClient.SYMBOL, days[-1], 0.1)])
    return cfg


def _industry_rows(cfg) -> list[tuple[str, str]]:
    with storage.connect(cfg.db_path) as conn:
        return [
            (r[0], r[1]) for r in conn.execute(
                "SELECT symbol, industry FROM stock_basic "
                "WHERE industry IS NOT NULL AND industry != ''"
            )
        ]


# ── ① 下载收尾会补轻量数据，而且顺序在"写行情 → 名称"之后 ──


def test_download_history_syncs_light_data_after_quotes(tmp_path, monkeypatch) -> None:
    """**实报 bug 的正解**：下完历史行情后，行业归属要真的写进库，否则自检永远 0%。

    同时钉住顺序：写行情 → 股票名称 → 交易日历 → 行业归属 → 指数。
    `sync_industry` 只给"库里已有行情的股票"写行业，顺序反了整个映射会被丢掉 ——
    这条用例就是防它。
    """
    cfg = _light_cfg(tmp_path)
    order: list[str] = []

    real_write = sync.storage.write_daily_raw
    monkeypatch.setattr(sync.storage, "write_daily_raw",
                        lambda conn, rows: (order.append("写行情"),
                                            real_write(conn, rows))[1])
    for name in ("sync_stock_names", "sync_calendar", "sync_industry", "sync_index"):
        real = getattr(sync, name)

        def wrapper(cfg=None, client=None, _real=real, _name=name, **kwargs):
            order.append(_name)
            return _real(cfg, client=client, **kwargs)

        monkeypatch.setattr(sync, name, wrapper)

    client = LightFakeClient()
    result = sync.download_history(cfg, client=client, include_names=True)

    assert result.ok, result.error
    # 行业真的落库了（以前这里是空的 → 自检报"行业覆盖率 0%"）
    assert _industry_rows(cfg) == [(LightFakeClient.SYMBOL, "银行")]
    # 顺序：写行情先于名称，名称先于轻量三件套
    assert order.index("写行情") < order.index("sync_stock_names")
    assert order.index("sync_stock_names") < order.index("sync_calendar")
    assert order.index("sync_calendar") < order.index("sync_industry")
    assert order.index("sync_industry") < order.index("sync_index")
    # 轻量同步的结果记进 extra，便于排查
    assert result.extra["light"] == {"交易日历": True, "行业归属": True, "指数": True}
    # 下完之后自检应当就绪（行业覆盖不再是 0%）
    check = preflight.check(cfg.db_path, cfg)
    assert check["industry_coverage"] > 0
    assert check["status"] == preflight.READY


# ── ② 轻量项失败：整体仍 ok，但 detail 要指路 ──


def test_download_history_keeps_ok_when_industry_sync_fails(tmp_path) -> None:
    """行业同步失败**不算整体失败**（行情已经下好了），但 detail 要写清怎么补。"""
    cfg = _light_cfg(tmp_path)
    result = sync.download_history(cfg, client=IndustryFailClient(), include_names=False)

    assert result.ok is True                      # 不因为轻量项失败就判失败
    assert "行业归属" in result.detail
    assert "【刷新数据】" in result.detail         # 指路指向正确的按钮
    assert result.extra["light"]["行业归属"] is False
    assert result.extra["light"]["交易日历"] is True     # 其它项照常
    # 行业确实没写进去 → 自检会提示缺轻量项（而不是"要重下历史"）
    check = preflight.check(cfg.db_path, cfg)
    assert preflight.needs_download(check) == preflight.DOWNLOAD_SYNC_LIGHT
    assert "【刷新数据】" in check["reason"]


def test_download_history_cancel_stops_light_requests(tmp_path) -> None:
    """取消（`should_stop`）之后，轻量同步**一次请求都不发**。"""
    cfg = _light_cfg(tmp_path)
    client = LightFakeClient()
    results = sync.sync_light(cfg, client=client, should_stop=lambda: True)
    assert client.calls == []                     # 一个请求都没发
    assert all(not r.ok for r in results)
    assert "已取消" in results[0].error


def test_sync_light_stops_between_steps_when_cancelled(tmp_path) -> None:
    """跑到一半取消：已完成的步骤保留，后面的步骤不再发请求。"""
    cfg = _light_cfg(tmp_path)
    client = LightFakeClient()
    seen = {"n": 0}

    def should_stop() -> bool:
        seen["n"] += 1
        return seen["n"] > 1                      # 第一步（日历）之后取消

    results = sync.sync_light(cfg, client=client, should_stop=should_stop)

    assert results[0].stage == "交易日历" and results[0].ok is True
    assert [r.stage for r in results[1:]] == ["行业归属", "指数"]
    assert all(not r.ok for r in results[1:])
    kinds = [c[0] for c in client.calls]
    assert "trading_days" in kinds                # 日历那一步跑了
    assert "ths_index_list" not in kinds          # 取消之后**不再请求**行业
    assert "index_historical" not in kinds


# ── ③ 自检文案：轻量项要指路到【刷新数据】 ──


def test_preflight_industry_missing_points_to_refresh_not_download(tmp_path) -> None:
    """**只缺行业归属**时：文案给【刷新数据】，绝不是"请重新下载历史"。"""
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    cfg.ensure_dirs()
    # 库是"就绪"的，只是 stock_basic.industry 全空（= 用户报的那个 0%）
    seed_ready_db(cfg, symbols=(("600001", "甲", ""), ("600002", "乙", "")), days=30)

    result = preflight.check(cfg.db_path, cfg)
    assert result["industry_coverage"] == 0.0
    assert "【刷新数据】" in result["reason"]
    assert "重新下载" not in result["reason"] and "全量重下" not in result["reason"]
    assert preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT
    assert result["light_only"] is True
    # 按钮名是真的（与界面上那颗按钮同一份定义）
    assert f"【{hints.BTN_REFRESH_TEXT}】" in result["reason"]


def test_preflight_missing_calendar_also_points_to_refresh(tmp_path) -> None:
    """缺交易日历同样属于轻量项：也指路【刷新数据】。"""
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    cfg.ensure_dirs()
    seed_ready_db(cfg, days=30)
    with storage.connect(cfg.db_path) as conn:
        conn.execute("DELETE FROM trading_calendar")
        conn.commit()

    result = preflight.check(cfg.db_path, cfg)
    assert preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT
    assert "【刷新数据】" in result["reason"]


# ── ④ ensure_ready：只缺轻量项时自动补一次再复查 ──


def test_ensure_ready_syncs_light_items_and_rechecks(tmp_path, monkeypatch) -> None:
    """**只缺行业** → 自动跑一次轻量同步并复查（补上了就放行），不去下载全量历史。"""
    cfg = Config(data_dir=tmp_path / "data", hithink_api_key="key")
    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    cfg.ensure_dirs()
    seed_ready_db(cfg, symbols=(("600001", "甲", ""), ("600002", "乙", "")), days=30)

    calls: list[str] = []

    def fake_sync_light(cfg=None, client=None, **kwargs):
        calls.append("sync_light")
        with storage.connect(cfg.db_path) as conn:      # 模拟"行业补上了"
            conn.execute("UPDATE stock_basic SET industry = '银行'")
            conn.commit()
        return [sync.SyncResult(stage="行业归属", ok=True, rows=2)]

    monkeypatch.setattr("laoa_trader.data.sync.sync_light", fake_sync_light)
    monkeypatch.setattr(
        "laoa_trader.data.sync.download_history",
        lambda *a, **k: pytest.fail("只缺轻量项时绝不该重新下载历史"),
    )

    proceed, after, results = preflight.ensure_ready(cfg)

    assert calls == ["sync_light"]                 # 真的去补了
    assert proceed is True
    assert after["status"] == preflight.READY      # 复查后放行
    assert [r.stage for r in results] == ["行业归属"]


def test_gate_auto_syncs_light_items_before_blocking(cfg, monkeypatch) -> None:
    """调度器的数据闸门：`auto_sync_light=True` 时先补再复查，补上就放行。"""
    from laoa_trader.scheduler import data_gate

    cfg.min_history_years, cfg.min_symbols = 0.0, 1
    seed_ready_db(cfg, symbols=(("600001", "甲", ""), ("600002", "乙", "")), days=30)

    calls: list[str] = []

    def fake_sync_light(cfg=None, client=None, **kwargs):
        calls.append("sync_light")
        with storage.connect(cfg.db_path) as conn:
            conn.execute("UPDATE stock_basic SET industry = '银行'")
            conn.commit()
        return [sync.SyncResult(stage="行业归属", ok=True, rows=2)]

    monkeypatch.setattr("laoa_trader.data.sync.sync_light", fake_sync_light)

    # 不开自动补：拦下，但**指路必须是【刷新数据】**
    blocked = data_gate(cfg)
    assert blocked["ok"] is False
    assert "【刷新数据】" in blocked["message"]
    assert calls == []

    # 开自动补：补一次 → 复查 → 放行
    opened = data_gate(cfg, auto_sync_light=True)
    assert calls == ["sync_light"]
    assert opened["ok"] is True
