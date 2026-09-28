"""竞价强度 / 当日异动 / 涨停原因 / 提醒标的文案（全离线，假客户端）。

为什么单独一份用例
------------------
这五件事都是"用户明确点名要"的功能，而且各有各的坑：

1. **提醒里的标的要显示成 `名称（代码）`**（推送文本本来就是，界面原来只有代码）；
2. **涨停原因**：涨停池里早就有 `reason_type`（= 接口的 `limit_up_reason`）与 `continue_day_text`，
   但界面从来没用过；
3. **竞价强度**：接口的 `auction_unmatched` 实测会出现 **-1**（= 未提供），
   把它当"卖压"会把最强的票读成最弱的票 —— 这条必须钉死；另外"非竞价时段"是**正常状态**，
   不是错误（不推送、不显示 0）；
4. **异动提醒**：全市场一条请求 + 本地只留自己的票（池子/自选/持仓），
   同一只票同一天同一标签只推一次；
5. **持仓做T 的近似提示**（`t_high` / `t_low`，见文件末尾那一节）：只对**持仓股**发，
   说法必须落在"反T（先卖后买）"或"正T（先买后卖）"这两种**T+1 下合法**的操作上，
   而且必须写清"只提示部分仓位、可卖数量以券商为准"。

Qt 界面那几条（提醒表格、卡片上的竞价/涨停行）在 `tests/test_ui_smoke.py`。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from laoa_trader import config as config_mod
from laoa_trader import intraday as it
from laoa_trader.config import Config
from laoa_trader.data import hithink as hx
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from tests.conftest import FakeClient, FakeResponse, FakeSession


class FakeSignalClient:
    """只实现这几件事的假客户端（竞价/异动/涨停池/快照），并记录调用。"""

    def __init__(self, auction_rows=None, anomalies=None, limit_up=None,
                 phase="closed", status="final", fail=None) -> None:
        self.auction_rows = list(auction_rows or [])
        self.anomalies = list(anomalies or [])
        self.limit_up = list(limit_up or [])
        self.phase = phase
        self.status = status
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    def auction_snapshot(self, thscodes, stage="live", chunk=100):
        self.calls.append(("auction", tuple(thscodes)))
        if self.fail:
            raise self.fail
        wanted = set(thscodes)
        return {"item": [r for r in self.auction_rows if r.get("thscode") in wanted],
                "failed": [], "phase": self.phase, "status": self.status,
                "timestamp": 0}

    def anomaly_list(self, tag_codes=None):
        self.calls.append(("anomaly", tuple(tag_codes or ())))
        if self.fail:
            raise self.fail
        return list(self.anomalies)

    def limit_up_pool(self, day=None, size=200):
        self.calls.append(("limit_up", day))
        if self.fail:
            raise self.fail
        return list(self.limit_up)

    def snapshot(self, thscodes=None, **kwargs):
        self.calls.append(("snapshot", tuple(thscodes or ())))
        return []

    def tickers(self, **kwargs):
        self.calls.append(("tickers", ()))
        return []


def _seed_targets(cfg, *, pool=("600001",), watch=(), positions=()) -> None:
    """把"自己的票"写进库（池子 + 自选 + 持仓）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600001", "低价样本", "银行"),
                                         ("600002", "半导体甲", "半导体"),
                                         ("600003", "白酒样本", "白酒")])
        if pool:
            storage.save_pool(conn, [
                {"symbol": s, "name": "样本", "strategy": "LowPriceStrategy",
                 "strategies": "LowPriceStrategy", "score": 1.0, "reason": "r"}
                for s in pool
            ], "2026-09-15")
        for symbol in watch:
            storage.upsert_watchlist(conn, symbol, name="自选样本")
        for symbol in positions:
            storage.upsert_position(conn, symbol, name="持仓样本", quantity=100,
                                    avg_cost=10.0)


def _seed_market(cfg, count: int = 0, symbols: list[str] | None = None) -> list[str]:
    """塞一批"全市场"代码进 `stock_basic`（竞价扫描的代码来源就是它）。

    默认造主板代码（600000 起）；调用方也可以点名要哪些代码（测板块时用）。
    """
    codes = list(symbols or [])
    if count:
        codes += [f"{600000 + i:06d}" for i in range(count)]
    codes = list(dict.fromkeys(codes))
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [(c, f"样本{c}", "行业") for c in codes])
    return codes


def _auction_row(symbol: str, *, pct: float, ratio: float, unmatched: float = -1.0,
                 turnover: float = 0.0013, yesterday: float = 0.95,
                 amount: float = 2.02e7, name: str = "低价样本") -> dict:
    """一条竞价快照行（字段名照抄真实接口）。"""
    return {
        "thscode": hx.to_thscode(symbol), "ticker": symbol, "name": name,
        "auction_price": 12.5, "auction_pct": pct, "auction_volume": 158.0,
        "auction_amount": amount, "auction_unmatched": unmatched,
        "auction_turnover_pct": turnover, "auction_yesterday_ratio_pct": yesterday,
        "auction_volume_ratio": ratio, "pre_close_price": 12.2,
        "open_price": 12.5, "last_price": 12.6, "float_market_cap": 1e10,
    }


AUCTION_NOW = datetime(2026, 9, 15, 9, 20)          # 竞价窗口内
TAIL_NOW = datetime(2026, 9, 15, 9, 27)             # 9:25 后（终态）
CLOSED_NOW = datetime(2026, 9, 15, 10, 30)          # 非竞价时段


# ── 客户端：竞价的真实返回 ──


def test_auction_snapshot_parses_envelope_and_stage() -> None:
    """`auction_snapshot` 把 phase/status/item 原样取回，非竞价时段**不抛异常**。"""
    session = FakeSession({
        "auction/snapshot": FakeResponse({"code": 0, "data": {
            "timestamp": 1757900000000, "auction_phase": "closed",
            "data_status": "final", "total": 1,
            "item": [_auction_row("600519", pct=0.2379, ratio=1.186)],
        }}),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    data = client.auction_snapshot(["600519"])
    assert data["phase"] == "closed" and data["status"] == "final"
    assert data["item"][0]["auction_pct"] == 0.2379
    url, params = session.calls[0]
    assert "auction/snapshot" in url and params["stage"] == "live"
    assert params["thscodes"] == "600519.SH"


def test_auction_snapshot_batches_over_the_limit() -> None:
    """一次问 150 只 → 自动按 100 分批（接口上限就是 100）。"""
    session = FakeSession({"auction/snapshot": FakeResponse(
        {"code": 0, "data": {"auction_phase": "live", "data_status": "partial",
                             "item": []}})})
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    codes = [f"{600000 + i:06d}" for i in range(150)]
    client.auction_snapshot(codes)
    assert len(session.calls) == 2
    assert len(session.calls[0][1]["thscodes"].split(",")) == 100
    assert len(session.calls[1][1]["thscodes"].split(",")) == 50


def test_anomaly_list_passes_tag_codes_only_when_given() -> None:
    """`anomaly_list`：不传标签就不带 `tag_codes`（拿全量）。"""
    session = FakeSession({"anomaly-analysis-list": FakeResponse(
        {"code": 0, "data": {"item": [{"thscode": "600519.SH", "tag_name": "RAPID_RALLY",
                                       "analysis_content": "快速反弹", "stock_name": "贵州茅台"}]}})})
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    rows = client.anomaly_list()
    assert rows[0]["tag_name"] == "RAPID_RALLY"
    assert "tag_codes" not in session.calls[0][1]
    client.anomaly_list(["rapid_rally"])
    assert session.calls[1][1]["tag_codes"] == "RAPID_RALLY"


# ── 竞价：解析与判据 ──


def test_auction_unmatched_sign_semantics() -> None:
    """**关键**：`-1` 是"未提供"；其余负数 = **卖盘剩余**；正数 = **买盘剩余**。

    接口只给一个带符号的量（没有买/卖两个数），实测 100 只里 >0 的 52 只、<0 的 45 只、
    恰好 -1 的 1 只（茅台）—— 三种情况必须分得清。
    """
    missing = it.auction_fields(_auction_row("600519", pct=0.24, ratio=1.19,
                                             unmatched=-1.0))
    assert missing["unmatched"] is None and missing["unmatched_ratio"] is None
    assert "剩余" not in it.auction_card_text(missing)          # 只显示涨幅与量比
    assert "剩余" not in it.auction_detail(missing)

    buy = it.auction_fields(_auction_row("600519", pct=0.24, ratio=1.19,
                                        unmatched=1200.0))     # 竞价量 158 手 → 比 +7.6
    assert buy["unmatched"] == 1200.0
    assert "买盘剩余1,200手" in it.auction_card_text(buy)
    assert buy["unmatched_ratio"] > 0.5

    sell = it.auction_fields(_auction_row("600519", pct=-0.5, ratio=0.4,
                                         unmatched=-900.0))
    assert sell["unmatched"] == -900.0
    assert "卖盘剩余900手" in it.auction_card_text(sell)
    assert sell["unmatched_ratio"] < -0.5


def test_auction_card_text_and_color_semantics() -> None:
    """卡片那一行：`竞价 +3.2% 量比 2.8`；涨跌颜色复用 market 的语义色。"""
    from laoa_trader import market

    up = it.auction_fields(_auction_row("600519", pct=3.2, ratio=2.8))
    assert it.auction_card_text(up) == "竞价 +3.2% 量比2.8"
    assert market.value_color(up["pct"]) == market.COLOR_UP
    down = it.auction_fields(_auction_row("600519", pct=-3.2, ratio=0.5))
    assert it.auction_card_text(down) == "竞价 -3.2% 量比0.5"
    assert market.value_color(down["pct"]) == market.COLOR_DOWN
    # 没有涨跌幅（停牌/未提供）→ 整行不显示
    assert it.auction_card_text({"pct": None, "volume_ratio": 2.0}) == ""
    # 卡片那一行能把三件事一次说清（涨跌幅 / 量比 / 未匹配量的方向与大小）
    both = it.auction_fields(_auction_row("600519", pct=3.2, ratio=2.8, unmatched=1200.0))
    assert it.auction_card_text(both) == "竞价 +3.2% 量比2.8 买盘剩余1,200手"


def _scored(pct=None, ratio=None, unmatched_ratio=None, amount=6e6) -> dict:
    """构造"已经算好未匹配比"的字段（打分只看这几个数）。"""
    return {"pct": pct, "volume_ratio": ratio, "unmatched_ratio": unmatched_ratio,
            "amount": amount}


@pytest.mark.parametrize(
    "fields, expected_score, expected_verdict",
    [
        # 高开 2 + 放量 2 + 买盘占优 1 + 成交额 1 = 6（正好等于门限也算）
        (_scored(2.0, 2.0, 0.6), 6, "auction_strong"),
        (_scored(9.0, 5.0, 0.9), 6, "auction_strong"),
        # **正好 3 分算强**：高开 2 + （量比不够）+ 成交额 1
        (_scored(2.0, 0.5, None), 3, "auction_strong"),
        # 高开 2 + 成交额 1 = 3 → 也是强（**不用 AND**，这正是改成分级的原因）
        (_scored(2.0, 0.4, None), 3, "auction_strong"),
        # 略高开 1 + 略放量 1 + 成交额 1 = 3 → 强
        (_scored(1.2, 1.6, None), 3, "auction_strong"),
        # 只有成交额那 1 分 → 不提示
        (_scored(0.1, 0.5, None), 1, ""),
        # 平开但放量（2）+ 成交额（1）= 3 → 也算强：这正是"不用 AND"的好处
        (_scored(0.0, 2.0, None), 3, "auction_strong"),
        # 低开带卖压：低开 -2 + 卖盘 -1 + 成交额 +1 = -2 → 弱
        (_scored(-2.5, 0.3, -0.7), -2, "auction_weak"),
        # 略低开 -1 + 卖盘 -1 + 成交额 +1 = -1 → 弱门限 = -(门限-1) = -1（门限默认已改成 2）
        (_scored(-1.2, 0.3, -0.7), -1, "auction_weak"),
        # 平开 + 卖盘 -1 + 成交额 +1 = 0：不构成"弱"（没有低开）
        (_scored(0.0, 0.3, -0.7), 0, ""),
    ],
)
def test_auction_score_rubric(fields, expected_score, expected_verdict) -> None:
    """打分口径：**分级给分**（不是 AND），正好等于门限算强、低开带卖压算弱。"""
    score, breakdown = it.auction_score(fields, Config())
    assert score == expected_score, breakdown
    assert it.auction_verdict(score, Config()) == expected_verdict


def test_auction_score_requires_minimum_amount() -> None:
    """**成交额不到下限（默认 500 万）→ 不参与评分**（小盘股竞价量太小时比值会爆表）。"""
    rich = _scored(2.0, 2.0, 0.6, amount=5e6)          # 正好等于下限 → 参与
    score, _ = it.auction_score(rich, Config())
    assert score == 6 and it.auction_verdict(score, Config()) == "auction_strong"

    poor = _scored(2.0, 2.0, 0.6, amount=4_999_999.0)  # 差一块钱 → 不参与
    assert it.auction_score(poor, Config()) == (None, {"reasons": [], "skipped": "成交额不足"})
    assert it.auction_verdict(None, Config()) == ""
    assert it.auction_score(_scored(9.0, 9.0, 0.9, amount=None), Config())[0] is None


def test_auction_score_uses_configured_thresholds() -> None:
    """阈值全部走配置：把门槛调高，同样的票就不再算强。"""
    cfg = Config(auction_min_pct=5.0, auction_min_volume_ratio=3.0,
                 auction_min_score=5)
    score, _ = it.auction_score(_scored(5.0, 3.0, 0.6), cfg)
    assert score == 6 and it.auction_verdict(score, cfg) == "auction_strong"
    lower, _ = it.auction_score(_scored(4.9, 2.0, None), cfg)     # 1 + 0 + 1 = 2 → 不够
    assert lower == 2 and it.auction_verdict(lower, cfg) == ""


def test_auction_window_boundaries() -> None:
    """9:15–9:25 是竞价；9:25–9:30 取终态；其余时段一次请求都不发。"""
    assert it.auction_window(datetime(2026, 9, 15, 9, 15)) is True
    assert it.auction_window(datetime(2026, 9, 15, 9, 24)) is True
    assert it.auction_window(datetime(2026, 9, 15, 9, 25)) is False
    assert it.auction_tail(datetime(2026, 9, 15, 9, 25)) is True
    assert it.auction_tail(datetime(2026, 9, 15, 9, 30)) is False
    assert it.auction_fetch_window(datetime(2026, 9, 15, 9, 20)) is True
    assert it.auction_fetch_window(datetime(2026, 9, 15, 9, 27)) is True
    assert it.auction_fetch_window(datetime(2026, 9, 15, 10, 30)) is False


def test_auction_scan_covers_the_whole_market_not_just_own_symbols(cfg, monkeypatch) -> None:
    """**全市场扫描**：池子/自选/持仓之外的票一样进结果（这正是这一版改的东西）。"""
    _seed_targets(cfg, pool=["600001"], watch=["600002"], positions=["600003"])
    _seed_market(cfg, count=6)
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    client = FakeSignalClient(auction_rows=[
        _auction_row("600004", pct=4.0, ratio=4.0),      # 池外
        _auction_row("600005", pct=5.0, ratio=5.0),      # 池外
    ])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW, slot="09:25")
    assert {hit["symbol"] for hit in scan["hits"]} == {"600004", "600005"}
    assert scan["scanned"] == 6                          # 全市场（本地 stock_basic）都要扫


def test_auction_scan_batches_and_paces_requests(cfg, monkeypatch) -> None:
    """**节奏**：全市场按 100 只一批，批间 ≥0.3 秒，串行（不并发）。"""
    _seed_market(cfg, count=5573)
    pauses: list[float] = []
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: pauses.append(seconds))
    client = FakeSignalClient()
    it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW, slot="09:20")
    batches = [c for c in client.calls if c[0] == "auction"]
    assert len(batches) == 56                            # 5573 / 100 → 56 批
    assert all(len(c[1]) <= 100 for c in batches)
    assert len(pauses) == 55                             # 批与批之间等一次
    assert pauses and min(pauses) >= 0.3
    assert it.AUCTION_SCAN_PACE >= 0.3


def test_auction_scan_progress_callback(cfg, monkeypatch) -> None:
    """扫描进度回调：界面/日志据此显示"扫到第几批"（56 批要 20~30 秒，不能一声不响）。"""
    _seed_market(cfg, count=250)
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    seen: list[tuple[int, int]] = []
    it.auction_scan(FakeSignalClient(), db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW,
                    slot="09:20", on_progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (1, 3) and seen[-1] == (3, 3)


def test_auction_scan_applies_every_filter(cfg, monkeypatch) -> None:
    """四条过滤规则逐条生效：涨幅下限 / 上限（一字板）/ 成交额 / 打分。"""
    cfg.auction_min_score = 2
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    _seed_market(cfg, count=5)
    client = FakeSignalClient(auction_rows=[
        _auction_row("600001", pct=1.5, ratio=5.0),      # 涨幅下限（+1.5% < 2.0%）
        _auction_row("600002", pct=9.9, ratio=5.0),      # 涨幅上限（一字板，买不进）
        _auction_row("600003", pct=4.0, ratio=5.0, amount=1e6),   # 成交额不到 500 万
        _auction_row("600004", pct=4.0, ratio=0.3, unmatched=None),  # 高开 2 + 成交额 1 = 3 分
        _auction_row("600005", pct=0.5, ratio=0.3, unmatched=None),  # 只有成交额 1 分 → 不够
    ])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW, slot="09:20")
    kept = {hit["symbol"] for hit in scan["hits"]}
    assert "600001" not in kept and scan["skipped"].get("涨幅下限") == 1
    assert "600002" not in kept and scan["skipped"].get("涨幅上限") == 1
    assert "600003" not in kept and scan["skipped"].get("成交额") == 1
    assert "600005" not in kept
    assert kept == {"600004"}


def test_auction_scan_ranks_by_score_and_caps_items(cfg, monkeypatch) -> None:
    """按**分数**排序、条数上限（默认 10、上限 50）—— 上限之外的只落库不推送。"""
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    symbols = [f"60000{i}" for i in range(1, 9)]
    _seed_market(cfg, symbols=symbols)
    # 全部都是满分 6 分（高开 2 + 放量 2 + 买盘 1 + 成交额 1），靠**涨幅**分名次
    rows = [_auction_row(s, pct=6.0 if s == "600003" else 4.0, ratio=4.0,
                         unmatched=2000.0) for s in symbols]
    client = FakeSignalClient(auction_rows=rows)
    cfg.auction_alert_max_items = 3
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW, slot="09:25")
    assert [h["symbol"] for h in scan["hits"]][0] == "600003"     # 分数最高的排第一
    assert len(scan["alerts"]) == 3
    assert [a["symbol"] for a in scan["alerts"]] == [h["symbol"] for h in scan["hits"][:3]]
    assert len(scan["hits"]) == 8                                  # 全部命中仍然都在
    assert scan["title"] == "⚡ 竞价扫描（9:25）｜共命中 8 只，推送前 3："


def test_auction_scan_summary_line_format(cfg, monkeypatch) -> None:
    """汇总文案：`名称（代码）` + 板块标签 + 原始数值 + 分数（用户要能核对）。"""
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    _seed_market(cfg, symbols=["300001", "688001"])
    client = FakeSignalClient(auction_rows=[
        _auction_row("300001", pct=5.21, ratio=3.2, unmatched=3400.0, amount=2.1e8,
                     name="创业板样本"),
        _auction_row("688001", pct=3.0, ratio=2.0, unmatched=-800.0, amount=6e7,
                     name="科创样本"),
    ])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW, slot="09:25")
    first = scan["lines"][0]
    assert first.startswith("1. 创业板样本(300001) 创业板 +5.21% 量比3.20 ")
    assert "成交额2.1亿" in first and "买盘剩余3,400手" in first and "（分6）" in first
    assert "卖盘剩余800手" in scan["lines"][1]
    assert "科创板" in scan["lines"][1]
    # 缺失的字段整段不出现（不写 `量比None` 这种垃圾）
    no_ratio = it.auction_scan_line({"name": "甲", "symbol": "600001", "board": "main",
                                     "pct": 3.0, "amount": 6e6, "score": 3})
    assert "量比" not in no_ratio and "剩余" not in no_ratio
    assert it.amount_text(2.1e8) == "2.1亿" and it.amount_text(6e7) == "6,000万"



def test_fetch_auction_carries_score_for_the_ui(cfg) -> None:
    """界面用的数据里带上分数与理由（卡片/详情要显示"原始数 + 分"）。"""
    _seed_targets(cfg)
    client = FakeSignalClient(auction_rows=[
        _auction_row("600001", pct=2.0, ratio=2.0, unmatched=1200.0)])
    snapshot = it.fetch_auction(cfg.db_path, cfg, client=client, now=AUCTION_NOW)
    assert snapshot["600001"]["score"] == 6
    assert "高开+2.00%" in snapshot["600001"]["reasons"]


def test_fetch_auction_returns_fields_by_symbol(cfg) -> None:
    """界面用的 `fetch_auction`：`{symbol: 展示字段}`，与非窗口时段返回空字典。"""
    _seed_targets(cfg)
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=2.5, ratio=3.0)])
    snapshot = it.fetch_auction(cfg.db_path, cfg, client=client, now=AUCTION_NOW)
    assert snapshot["600001"]["pct"] == 2.5
    assert it.auction_card_text(snapshot["600001"]) == "竞价 +2.5% 量比3.0"


# ── 异动 ──


def _anomaly(symbol: str, tag: str, content: str = "快速反弹",
             name: str = "低价样本") -> dict:
    return {"thscode": hx.to_thscode(symbol), "stock_name": name, "tag_name": tag,
            "analysis_content": content, "keyword_list": ["反弹"]}


def test_anomaly_alerts_only_keep_own_symbols(cfg) -> None:
    """**只推自己的票**：池外的异动（哪怕涨停）一条都不产生。"""
    _seed_targets(cfg, pool=["600001"], watch=["600002"])
    client = FakeSignalClient(anomalies=[
        _anomaly("600001", "RAPID_RALLY"),
        _anomaly("600002", "LIMIT_UP", name="自选样本"),
        _anomaly("600999", "LIMIT_UP", name="池外的票"),      # 不在自己的票里
    ])
    alerts = it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg)
    assert {a["symbol"] for a in alerts} == {"600001", "600002"}
    assert alerts[0]["kind"] == "anomaly_rapid_rally"
    assert alerts[0]["detail"].startswith("快速反弹 · ")
    assert alerts[1]["kind"] == "anomaly_limit_up"


def test_anomaly_alerts_truncate_reason(cfg) -> None:
    """异动原因是整段新闻：截到 ~120 字（带省略号），别把推送撑爆。"""
    _seed_targets(cfg)
    long_text = "9时25分" + "种植业与林业板块下跌1.74%，" * 40
    client = FakeSignalClient(anomalies=[_anomaly("600001", "RAPID_DECLINE", long_text)])
    alerts = it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg)
    detail = alerts[0]["detail"]
    body = detail.split(" · ", 1)[1]
    assert len(body) <= it.ANOMALY_REASON_LIMIT
    assert body.endswith("…")
    assert detail.startswith("快速下跌 · ")


def test_anomaly_alerts_respects_tag_filter(cfg) -> None:
    """`anomaly_alert_tags` 只留配置里点名的标签（空 = 全部）。"""
    _seed_targets(cfg)
    rows = [_anomaly("600001", "RAPID_RALLY"), _anomaly("600001", "LIMIT_DOWN")]
    client = FakeSignalClient(anomalies=rows)
    assert len(it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg)) == 2
    cfg.anomaly_alert_tags = ["LIMIT_DOWN"]
    alerts = it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg)
    assert [a["kind"] for a in alerts] == ["anomaly_limit_down"]
    assert client.calls[-1][1] == ("LIMIT_DOWN",)         # 过滤条件也传给了接口


def test_anomaly_alerts_accept_chinese_tag_name(cfg) -> None:
    """接口的 `tag_name` 也可能直接是中文（"快速反弹"）：两种写法都要认。"""
    _seed_targets(cfg)
    client = FakeSignalClient(anomalies=[_anomaly("600001", "快速反弹")])
    alerts = it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg)
    assert alerts[0]["kind"] == "anomaly_rapid_rally"
    assert alerts[0]["detail"].startswith("快速反弹 · ")


def test_anomaly_alerts_off_and_empty_list(cfg) -> None:
    """关掉配置 → 不取；接口返回空 → 空列表、不报错。"""
    _seed_targets(cfg)
    client = FakeSignalClient(anomalies=[])
    assert it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg) == []
    cfg.intraday_anomaly = False
    assert it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg) == []


def test_anomaly_failure_does_not_break_other_alerts(cfg) -> None:
    """异动接口失败：只记日志，返回空列表（**不抛**），其它提醒照跑。"""
    _seed_targets(cfg)
    client = FakeSignalClient(fail=hx.HithinkError(5001, "测试假客户端：异动接口挂了"))
    assert it.anomaly_alerts(client, db_path=cfg.db_path, cfg=cfg) == []


def test_build_alerts_includes_anomaly_but_not_auction(cfg) -> None:
    """异动并进同一次循环；**竞价不在 `build_alerts` 里**（到点才扫，见 `run_once`）。

    为什么要把竞价摘出去：`build_alerts` 是"每分钟一拍"的路径，
    竞价扫描要是混进来就变成"每分钟扫一次全市场"（56 请求 × 10 分钟 = 560 请求）。
    """
    _seed_targets(cfg)
    client = FakeSignalClient(
        auction_rows=[_auction_row("600001", pct=3.5, ratio=3.0)],
        anomalies=[_anomaly("600001", "RAPID_RALLY")],
    )
    engine = DataEngine(cfg.db_path)
    alerts = it.build_alerts(engine, client, cfg=cfg)
    kinds = {a["kind"] for a in alerts}
    assert "anomaly_rapid_rally" in kinds
    assert "auction_strong" not in kinds
    assert [c for c in client.calls if c[0] == "auction"] == []     # 一次竞价请求都没发


def test_build_alerts_no_longer_emits_first_board(cfg) -> None:
    """**打板提醒已下线**：实时涨停池里就算有"首板 + 厚封单"，也不再产生任何提醒。

    为什么留着这条用例（而不是把相关用例删干净）：删掉一个功能之后，
    "它不会再回来"这件事必须由测试钉住 —— 否则哪天有人顺手把那段塞回
    `build_alerts`，用户会重新收到他已经明确说"没有意义"的提醒，而且没人会发现。

    顺带钉住两件事：① 标签表里也没有这个 kind（否则它会以"未知 kind = 原样显示
    first_board"的形式漏到推送里）；② 这一轮**不再为了打板去拉涨停池**
    （那条请求本身也是配额）。
    """
    _seed_targets(cfg)
    client = FakeSignalClient(
        limit_up=[{"thscode": "600519.SH", "name": "贵州茅台", "continue_day_cnt": 1,
                   "seal_money": 9e7, "last_price": 1281.0,
                   "limit_up_reason": "半导体设备+业绩预增"}],
        anomalies=[_anomaly("600001", "RAPID_RALLY")],
    )
    engine = DataEngine(cfg.db_path)

    alerts = it.build_alerts(engine, client, cfg=cfg)

    kinds = {a["kind"] for a in alerts}
    assert kinds, "其它提醒种类必须照常产出"
    assert "first_board" not in kinds
    assert kinds == {"anomaly_rapid_rally"}          # 这一轮该出的都出了
    assert "first_board" not in it.KIND_LABELS       # 标签表也清掉了
    assert not hasattr(it, "evaluate_first_board")   # 函数整个删掉，不是留着不用
    assert [c for c in client.calls if c[0] == "limit_up"] == []   # 不为打板拉涨停池


def test_run_once_scans_only_at_the_configured_slots(cfg, monkeypatch) -> None:
    """到点才扫：9:20 与 9:25 各一次；9:23（宽限窗口外）/9:40 一次请求都不发。"""
    _seed_market(cfg, count=120)
    engine = DataEngine(cfg.db_path)
    monkeypatch.setattr(it, "in_session", lambda now=None: False)
    monkeypatch.setattr(it, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=4.0, ratio=4.0)])
    sent: list[tuple[str, list[str]]] = []

    report = it.run_once(engine, cfg, client=client, notifier=lambda t, l: sent.append((t, l)),
                         now=datetime(2026, 9, 15, 9, 20, 5))
    assert report["auction"]["slot"] == "09:20"
    assert [c for c in client.calls if c[0] == "auction"]              # 扫了
    assert sent and sent[0][0].startswith("⚡ 竞价扫描（9:20）")

    # 同一档再跑一次（调度器每分钟一拍）→ 不再扫
    client.calls.clear()
    it.run_once(engine, cfg, client=client, notifier=lambda t, l: None,
                now=datetime(2026, 9, 15, 9, 21, 5))
    assert [c for c in client.calls if c[0] == "auction"] == []

    # 09:23（09:20 那档的宽限窗口已过，09:25 那档还没到）→ 不扫
    client.calls.clear()
    it.run_once(engine, cfg, client=client, notifier=lambda t, l: None,
                now=datetime(2026, 9, 15, 9, 23, 0), ignore_session=True)
    assert [c for c in client.calls if c[0] == "auction"] == []

    # 09:25 那一档（终态）→ 扫，并且汇总推送带"（9:25）"
    client.calls.clear()
    sent.clear()
    it.run_once(engine, cfg, client=client, notifier=lambda t, l: sent.append((t, l)),
                now=datetime(2026, 9, 15, 9, 25, 30))
    assert [c for c in client.calls if c[0] == "auction"]
    assert sent and sent[0][0].startswith("⚡ 竞价扫描（9:25）")


def test_auction_scan_records_alerts_and_results(cfg, monkeypatch) -> None:
    """扫描结果：全部命中落 `auction_scan` 表，前 N 只另外登进提醒表（浮窗/列表看得到）。"""
    _seed_market(cfg, count=120)
    engine = DataEngine(cfg.db_path)
    monkeypatch.setattr(it, "in_session", lambda now=None: False)
    monkeypatch.setattr(it, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: None)
    cfg.auction_alert_max_items = 3
    rows = [_auction_row(f"{600000 + i:06d}", pct=4.0 + i * 0.1, ratio=4.0,
                         unmatched=2000.0) for i in range(5)]
    client = FakeSignalClient(auction_rows=rows)
    it.run_once(engine, cfg, client=client, notifier=lambda t, l: None,
                now=datetime(2026, 9, 15, 9, 25, 30))

    with storage.connect(cfg.db_path) as conn:
        saved = storage.load_auction_scan(conn, "2026-09-15")
    assert len(saved) == 5                                   # 全部命中都落库
    assert [r["pushed"] for r in saved] == [1, 1, 1, 0, 0]    # 前 3 只标 ★
    assert saved[0]["board"] == "main" and saved[0]["name"]
    alerts = [a for a in it.alert_rows(cfg.db_path, limit=20) if a["kind"] == "auction_strong"]
    assert len(alerts) == 3                                  # 提醒表只有前 N 只


# ── 涨停原因 ──


def test_enrich_limit_up_reasons_appends_reason() -> None:
    """`limit_up_open` 这类提醒补上原因；**没有命中就不去拉涨停池**（省配额）。"""
    alerts = [{"symbol": "600519", "name": "贵州茅台", "kind": "limit_up_open",
               "price": 1281.0, "detail": "涨停打开"}]
    client = FakeSignalClient(limit_up=[{
        "thscode": "600519.SH", "limit_up_reason": "半导体设备+业绩预增"}])
    it.enrich_limit_up_reasons(client, alerts)
    assert alerts[0]["detail"].endswith("，涨停原因：半导体设备+业绩预增")
    assert [c[0] for c in client.calls] == ["limit_up"]

    other = [{"symbol": "600519", "kind": "break_ma5", "detail": "跌破 5 日线"}]
    it.enrich_limit_up_reasons(FakeSignalClient(), other)
    assert other[0]["detail"] == "跌破 5 日线"          # 与涨停无关：原样


# ── 配置 ──


@pytest.fixture(autouse=True)
def _enable_auction(cfg):
    """本文件测的就是竞价/异动这些功能本身：**显式打开**，不要依赖默认值。

    竞价默认是关的（口径与阈值还在与用户确认，见 config.py 的注释）——
    所以这些用例必须自己开，否则测的是"关掉之后什么都不做"，等于没测。
    """
    cfg.intraday_auction = True
    cfg.intraday_anomaly = True

def test_auction_is_off_by_default_and_sends_no_request(cfg, monkeypatch) -> None:
    """**竞价默认关闭**（口径待确认）：用默认配置跑一整轮，一次竞价请求都不发。

    这条钉的是"功能已就绪但不启用"这个状态本身 —— 用户明确要求先别把竞价接进默认流程。
    （本文件其余竞价用例用的是显式打开开关的 `cfg`，见上面的 `_enable_auction` fixture。）
    """
    _seed_market(cfg, count=120)
    default_cfg = Config()
    default_cfg.data_dir = cfg.data_dir
    assert default_cfg.intraday_auction is False
    engine = DataEngine(cfg.db_path)
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=9.0, ratio=9.0)])
    monkeypatch.setattr(it, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(it, "in_session", lambda now=None: False)
    report = it.run_once(engine, default_cfg, client=client,
                         notifier=lambda t, l: None,
                         now=datetime(2026, 9, 15, 9, 20, 5))
    assert report["auction"] == {}                  # 连"到点判断"都不做
    assert it.fetch_auction(cfg.db_path, default_cfg, client=client,
                            now=AUCTION_NOW) == {}
    assert [c for c in client.calls if c[0] == "auction"] == []


def test_new_intraday_config_defaults_and_fallback(cfg) -> None:
    """竞价那一组的默认值与非法值回退（其余的见 `test_config.py`）。"""
    fresh = Config()
    # 竞价**默认关**：功能已就绪，但口径与阈值待用户确认后才打开
    assert fresh.intraday_auction is False
    assert fresh.auction_min_pct == 2.0
    assert fresh.auction_min_volume_ratio == 2.0
    # 当日异动**默认关**（用户拍板：减少无用消息）——能力都在，想要就打开
    assert fresh.intraday_anomaly is False
    assert fresh.anomaly_alert_tags == []

    bad = Config(auction_min_pct=0, auction_min_volume_ratio=-1,
                 anomaly_alert_tags=" rapid_rally ,, limit_down ")
    assert bad.auction_min_pct == 2.0
    assert bad.auction_min_volume_ratio == 2.0
    assert bad.anomaly_alert_tags == ["RAPID_RALLY", "LIMIT_DOWN"]


def test_new_intraday_config_from_env(monkeypatch) -> None:
    """环境变量覆盖（界面上临时调试/分发时改口径用）；**旧名也继续认**。"""
    monkeypatch.setenv("INTRADAY_AUCTION", "false")
    monkeypatch.setenv("AUCTION_ALERT_MIN_PCT", "3.5")      # 旧名
    monkeypatch.setenv("AUCTION_MIN_PCT", "4.5")            # 新名同时给了 → 新名赢
    monkeypatch.setenv("AUCTION_MIN_SCORE", "4")
    monkeypatch.setenv("AUCTION_SCAN_AT", "09:18, 09:24")
    monkeypatch.setenv("AUCTION_BOARDS", "chinext,star")
    monkeypatch.setenv("INTRADAY_ANOMALY", "0")
    monkeypatch.setenv("ANOMALY_ALERT_TAGS", "limit_up,limit_down")
    cfg = config_mod.load_config(None, use_env=True)
    assert cfg.intraday_auction is False
    assert cfg.auction_min_pct == 4.5
    assert cfg.auction_min_score == 4
    assert cfg.auction_scan_at == ["09:18", "09:24"]
    assert cfg.auction_boards == ["chinext", "star"]
    assert cfg.intraday_anomaly is False
    assert cfg.anomaly_alert_tags == ["LIMIT_UP", "LIMIT_DOWN"]


# ── ⑤ 持仓做T 的近似提示（`t_high` / `t_low`）──
#
# 地基是 A 股 **T+1**：当天买的票当天不能卖。所以这里每条断言都要能回答两件事 ——
#   1. 这条提示说的是**反T（先卖后买）**还是**正T（先买后卖）**？
#   2. 有没有写清"只提示部分仓位、可卖数量以券商为准"？
#      （程序不知道用户今天又买过多少、有多少被挂单冻结，能卖多少只有券商知道。）
# 四个阈值默认 2.0 / 1.5 / 2.0 / 1.0（%），都是**手工设定的起点**：
# 只有 60 秒快照、没有分时/逐笔数据，回测不了（README「持仓做T（近似提示）」有说明）。

T_DAY = "2026-09-16"          # 固定的"今天"（北京时间）；CI 跑在 UTC，绝不能用真实日期
T_NOW = datetime(2026, 9, 16, 10, 30)


def _t_snap(symbol="600001", *, last, high, low, prev, vwap=None, volume=1e6, pct=None):
    """一张 60 秒快照：字段名照抄接口，`turnover = 均价 × 成交量`（两者都是当日累计值）。"""
    amount = (vwap if vwap is not None else last) * volume
    return {
        "ticker": symbol, "thscode": hx.to_thscode(symbol),
        "last_price": last, "high_price": high, "low_price": low, "prev_price": prev,
        "price_change_ratio_pct": pct if pct is not None else (last / prev - 1) * 100,
        "volume": volume, "turnover": amount,
    }


def _t_pos(cfg, symbol="600001", *, opened_at="2026-01-05 09:31:00",
           quantity=1000, cost=10.0, name="持仓样本"):
    """写一条持仓并返回它（`opened_at` 显式指定：T+1 的判据全看这一列）。

    `storage.upsert_position` 会把 `opened_at` 写成"现在"，所以再用一条 UPDATE 覆盖掉 ——
    测"昨日建仓 / 今日建仓 / 建仓日未知"这三种情况必须能精确控制它。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_position(conn, symbol, name=name, quantity=quantity, avg_cost=cost)
        conn.execute("UPDATE position SET opened_at = ? WHERE symbol = ?", (opened_at, symbol))
        conn.commit()
    with storage.connect(cfg.db_path) as conn:
        return storage.load_positions(conn)[symbol]


def _t_day(cfg, days=(T_DAY,)) -> None:
    """把这些天写进交易日历（真实路径：`is_trading_day` 查的就是这张表）。"""
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, list(days))


# ── 高抛（反T 前半段）──


def test_t_high_fires_with_all_the_numbers(cfg) -> None:
    """高抛：涨 2.8% + 从今日最高回落 3.4% + 仍在均价上方 → 提示"卖部分昨仓"。"""
    pos = _t_pos(cfg)
    snap = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    hits = it.evaluate_t_rules("600001", snap, pos, T_DAY, cfg)
    assert [h[0] for h in hits] == ["t_high"]
    kind, price, detail = hits[0]
    assert kind == "t_high" and price == 12.34
    # 触发它的数字必须都在句子里（用户要能自己核对，而不是相信一个结论）
    assert "现价 12.34（+2.8%）" in detail
    assert "今日最高 12.78 回落 3.4%" in detail
    assert "仍在均价 12.10 上方" in detail
    # 说法必须是**反T**（先卖后买）+ 部分仓位 + 可卖数量未知
    assert "反T：先卖后买" in detail and "部分昨仓" in detail
    assert "可卖数量以券商为准" in detail and "部分仓位" in detail
    # 标签里带「近似」：用户一眼能分辨它不是"有历史数据支撑的信号"
    assert "近似" in it.KIND_LABELS["t_high"]


def test_t_high_boundary_fires_at_threshold_and_not_just_below(cfg) -> None:
    """三条判据**正好在门槛上**要发；任何一条差一点点就不发。

    为什么这里敢钉"正好等于"：
    - 涨幅直接用快照给的 `price_change_ratio_pct`（传 2.0 就是 2.0，不经过任何除法）；
    - 回落那一对价格是**实测过浮点精确**的（`(25.00−24.625)/25.00×100 == 1.5` 恰好成立）。
    顺带记一个踩过的坑：`(10.10−10.00)/10.00×100` 在浮点里是 `0.9999999999999963`，
    所以"正好 1%"这类断言**不能**随手拿两个两位小数去写。
    """
    pos = _t_pos(cfg)
    on = _t_snap(last=24.625, high=25.0, low=23.9, prev=24.14, vwap=24.0, pct=2.0)
    assert [h[0] for h in it.evaluate_t_rules("600001", on, pos, T_DAY, cfg)] == ["t_high"]
    # ① 涨幅差 0.01 个点
    weak_gain = _t_snap(last=24.625, high=25.0, low=23.9, prev=24.14, vwap=24.0, pct=1.99)
    assert it.evaluate_t_rules("600001", weak_gain, pos, T_DAY, cfg) == []
    # ② 回落只有 1.48%（门槛 1.5%）
    weak_pull = _t_snap(last=24.63, high=25.0, low=23.9, prev=24.14, vwap=24.0, pct=2.0)
    assert it.evaluate_t_rules("600001", weak_pull, pos, T_DAY, cfg) == []
    # ③ 现价跌破均价（回落但破了位）
    below_vwap = _t_snap(last=24.625, high=25.0, low=23.9, prev=24.14, vwap=24.7, pct=2.0)
    assert it.evaluate_t_rules("600001", below_vwap, pos, T_DAY, cfg) == []


def test_t_high_skipped_for_position_opened_today(cfg) -> None:
    """**今日新建仓 T+1 不可卖** → 不发高抛提示（这是整个功能最容易做错的一条）。"""
    pos = _t_pos(cfg, opened_at=f"{T_DAY} 09:31:00")
    assert it.sellable_note(pos, T_DAY) == (False, "今日新建仓，T+1 当天不可卖")
    snap = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    assert it.evaluate_t_rules("600001", snap, pos, T_DAY, cfg) == []


def test_sellable_note_unknown_opened_at_is_sellable_with_a_note(cfg) -> None:
    """`opened_at` 空/NULL（老库、手工导入）→ 按"可卖"处理，但**必须在提示里写明**。

    为什么不按"不可卖"处理：这一列可能是空的，按不可卖会让高抛提示永远不发，
    用户看不到任何反应只会以为功能坏了；按可卖 + 明说，用户自己一眼能判断。
    """
    pos = _t_pos(cfg, opened_at=None)
    assert it.sellable_note(pos, T_DAY) == (True, "建仓日未知，按可卖处理")
    snap = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    hits = it.evaluate_t_rules("600001", snap, pos, T_DAY, cfg)
    assert [h[0] for h in hits] == ["t_high"]
    assert "建仓日未知，按可卖处理" in hits[0][2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-16 09:31:00", "2026-09-16"),
        ("2026/9/16", "2026-09-16"),          # 单位数月/日也认（别当成"未知"）
        ("2026.09.16", "2026-09-16"),
        ("2026-09-15", "2026-09-15"),
        ("", ""), (None, ""), ("乱写", ""), ("2026-13-40", ""),
    ],
)
def test_day_of_parses_legacy_opened_at(raw, expected) -> None:
    """`opened_at` 是自由文本（老库/手工导入），解析要宽松、**绝不抛异常**。"""
    assert it._day_of(raw) == expected


# ── 低吸（正T，或反T 的后半段接回）──


def test_t_low_fires_and_says_it_is_a_positive_t(cfg) -> None:
    """低吸：跌 2.4% + 从今日最低反弹 1.5% + 已站上均价 → 提示"用现金低吸（正T）"。"""
    pos = _t_pos(cfg)
    snap = _t_snap(last=9.76, high=10.0, low=9.62, prev=10.0, vwap=9.70, pct=-2.4)
    hits = it.evaluate_t_rules("600001", snap, pos, T_DAY, cfg)
    assert [h[0] for h in hits] == ["t_low"]
    detail = hits[0][2]
    assert "现价 9.76（-2.4%）" in detail
    assert "今日最低 9.62 反弹 1.5%" in detail
    assert "已站上均价 9.70" in detail
    assert "正T：先买后卖" in detail and "可用现金低吸" in detail
    assert "可卖数量以券商为准" in detail
    assert "近似" in it.KIND_LABELS["t_low"]


def test_t_low_still_fires_for_position_opened_today_and_says_why(cfg) -> None:
    """正T 用**现金**买入、不卖任何股票，所以今日新建仓照样提示 —— 但要说明卖要等明天。"""
    pos = _t_pos(cfg, opened_at=f"{T_DAY} 09:31:00")
    snap = _t_snap(last=9.76, high=10.0, low=9.62, prev=10.0, vwap=9.70, pct=-2.4)
    hits = it.evaluate_t_rules("600001", snap, pos, T_DAY, cfg)
    assert [h[0] for h in hits] == ["t_low"]
    assert "今日新建仓，T+1 当天不可卖" in hits[0][2]


def test_t_low_boundary_and_vwap_tolerance(cfg) -> None:
    """低吸的三条判据 + "刚收回均价"的 0.5% 容差（`T_LOW_VWAP_TOL`）。"""
    pos = _t_pos(cfg)
    # 正好在门槛上：跌幅 -2.0%（快照直接给）、反弹 1.0%（浮点精确的一对价格 12.5 → 12.625）
    on = _t_snap(last=12.625, high=13.0, low=12.5, prev=12.883, vwap=12.60, pct=-2.0)
    assert [h[0] for h in it.evaluate_t_rules("600001", on, pos, T_DAY, cfg)] == ["t_low"]
    # 跌幅差 0.01 个点
    assert it.evaluate_t_rules(
        "600001", _t_snap(last=12.625, high=13.0, low=12.5, prev=12.883, vwap=12.60,
                          pct=-1.99), pos, T_DAY, cfg) == []
    # 反弹只有 0.96%（门槛 1.0%）
    assert it.evaluate_t_rules(
        "600001", _t_snap(last=12.62, high=13.0, low=12.5, prev=12.883, vwap=12.60,
                          pct=-2.0), pos, T_DAY, cfg) == []
    # 仍压在均价下方 0.59%（超过 0.5% 容差）→ "没收回"，不发
    assert it.evaluate_t_rules(
        "600001", _t_snap(last=12.625, high=13.0, low=12.5, prev=12.883, vwap=12.70,
                          pct=-2.0), pos, T_DAY, cfg) == []
    # 刚收回均价（低 0.28%，在容差内）→ 发，并且句子里写明"仍低多少"
    recovered = _t_snap(last=12.625, high=13.0, low=12.5, prev=12.883, vwap=12.66, pct=-2.0)
    hits = it.evaluate_t_rules("600001", recovered, pos, T_DAY, cfg)
    assert [h[0] for h in hits] == ["t_low"]
    assert "刚收回均价 12.66" in hits[0][2] and "仍低 0.3%" in hits[0][2]


# ── 缺字段 / 口径不对：宁可少提示，不给错数 ──


def test_t_rules_degrade_gracefully_when_fields_are_missing(cfg) -> None:
    """缺字段一律"不发"，**一条都不许抛异常**（快照字段缺失是常态，不是异常）。"""
    pos = _t_pos(cfg)
    # 只有一个空字典 / 只有现价（连昨收都没有 → 算不出涨跌幅）
    assert it.evaluate_t_rules("600001", {}, pos, T_DAY, cfg) == []
    assert it.evaluate_t_rules("600001", {"last_price": 12.34}, pos, T_DAY, cfg) == []
    assert it.evaluate_t_rules("600001", {"last_price": 0}, pos, T_DAY, cfg) == []
    # 没有成交量/成交额 → 算不出均价 → 三条判据缺一条，整条不发
    no_volume = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10,
                        volume=1e6, pct=2.83)
    no_volume["volume"] = 0
    no_volume["turnover"] = 0
    assert it.evaluate_t_rules("600001", no_volume, pos, T_DAY, cfg) == []
    # 没有现成的涨跌幅、但有昨收 → 用快照的昨收自己算（照样能发）
    computed = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    computed.pop("price_change_ratio_pct")
    assert [h[0] for h in it.evaluate_t_rules("600001", computed, pos, T_DAY, cfg)] == ["t_high"]


def test_t_vwap_out_of_today_range_is_dropped(cfg) -> None:
    """均价必须落在当日 [最低, 最高] 内 —— 这是防"单位/口径不对"的自我校验。

    真实风险：接口文档写 `volume` 单位是**股**，但若它其实是**手**，均价会差 100 倍。
    这种时候**整条不发**（宁可不说，也不给一个错的数：提示里写着「均价 12.10 上方」，
    用户就会照着这个数下单）。
    """
    assert it.vwap_reasonable(12.10, 12.05, 12.78) is True
    assert it.vwap_reasonable(1210.0, 12.05, 12.78) is False      # 单位差 100 倍
    assert it.vwap_reasonable(11.0, 12.05, 12.78) is False        # 低于当日最低
    assert it.vwap_reasonable(None, 12.05, 12.78) is False
    assert it.vwap_reasonable(12.10, None, None) is False

    pos = _t_pos(cfg)
    wrong_unit = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    wrong_unit["volume"] = 1e4                                    # 成交额不变、量小 100 倍
    assert it.evaluate_t_rules("600001", wrong_unit, pos, T_DAY, cfg) == []
    # 反过来：量给大了 100 倍 → 均价 0.12（远低于当日最低）。
    # 这一条**必须单独有**：均价偏低时后面的 `现价 >= 均价` 判据会"恰好通过"，
    # 只有这道区间校验拦得住它 —— 上面那条（均价偏高）是那条判据顺手拦住的，
    # 不能算成校验的功劳（实测：把区间校验删掉，只有下面这条会红）。
    too_low = _t_snap(last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83)
    too_low["volume"] = 1e8
    assert it.evaluate_t_rules("600001", too_low, pos, T_DAY, cfg) == []


# ── 整条链路（run_once）：去重、配置开关、以及"持仓不在池子里" ──


def test_run_once_pushes_t_hint_once_per_day(cfg, monkeypatch) -> None:
    """整条链路：快照 → 提醒落库 → 推送文本；**同标的同 kind 当天只推一次**。

    去重复用既有的 `intraday_alert(date, symbol, kind)` 主键（一天一条），
    换一天（次日）会重新发 —— 所以这里连"次日再发一次"一起钉住。
    """
    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    cfg.intraday_t = True          # 做T**默认关**（用户拍板）；这条验的是它的整条链路
    # 成本刻意取 12.00：现价 12.34 够触发做T高抛，但**够不到止盈**（12.00 × 1.10 = 13.20）
    # —— 这条用例要单独钉住做T那一条链路，不希望被同时命中的止盈搅进来
    _t_pos(cfg, cost=12.0)
    _t_day(cfg, (T_DAY, "2026-09-17"))
    client = FakeClient(snapshots=[_t_snap(last=12.34, high=12.78, low=12.05, prev=12.0,
                                           vwap=12.10, pct=2.83)])
    sent: list[tuple[str, list[str]]] = []
    engine = DataEngine(cfg.db_path)

    first = it.run_once(engine, cfg, ignore_session=True, client=client,
                        notifier=lambda t, l: sent.append((t, l)), now=T_NOW)
    assert first["hits"] == 1 and first["fresh"] == 1 and first["pushed"] is True
    body = "\n".join(sent[0][1])
    assert "近似·T高抛（反T：先卖后买）" in body
    assert "现价 12.34" in body and "可卖数量以券商为准" in body
    # 做T提示**不生成条件单**：一张条件单表达不了"先卖后买"，
    # 而且持仓股会走 `plan_sell`，会把方向写成"卖出"（比不说更糟）
    assert "条件单" not in body

    # 同一张快照再来两轮：命中还在，但不再推（去重键含日期）
    second = it.run_once(engine, cfg, ignore_session=True, client=client,
                         notifier=lambda t, l: sent.append((t, l)),
                         now=datetime(2026, 9, 16, 10, 31))
    assert second["hits"] == 1 and second["fresh"] == 0 and second["pushed"] is False
    third = it.run_once(engine, cfg, ignore_session=True, client=client,
                        notifier=lambda t, l: sent.append((t, l)),
                        now=datetime(2026, 9, 16, 14, 59))
    assert third["fresh"] == 0
    assert len(sent) == 1

    # 次日：新的交易日，同一只票同样的形态 → 重新发（一天一条，不是"一辈子一条"）
    next_day = it.run_once(engine, cfg, ignore_session=True, client=client,
                           notifier=lambda t, l: sent.append((t, l)),
                           now=datetime(2026, 9, 17, 10, 30))
    assert next_day["fresh"] == 1 and next_day["pushed"] is True
    assert len(sent) == 2


def test_run_once_watches_held_symbols_outside_the_pool(cfg, monkeypatch) -> None:
    """持仓**不在股票池里也要盯** —— 池子天天重建，用户手上的票却不会跟着变。

    这条钉的是"补进快照请求"这件事本身：如果只盯池子，持仓票连快照都不会去取
    （下面的 `asked` 断言就是这个），做T提示就成了"只有池子里的持仓才提示"。
    """
    from laoa_trader import pool as pool_mod

    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    cfg.intraday_t = True          # 做T**默认关**（用户拍板）；这条验的是"持仓票也要盯"
    _t_pos(cfg, "600009", name="在手票")
    pool_mod.save_pool(cfg.db_path, [{"symbol": "600001", "name": "池内票", "score": 1.0,
                                      "strategy": "ReversalStrategy"}], T_DAY)
    _t_day(cfg)
    client = FakeClient(snapshots=[
        _t_snap("600009", last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83),
        _t_snap("600001", last=10.0, high=10.1, low=9.9, prev=10.0, vwap=10.0, pct=0.0),
    ])
    it.run_once(DataEngine(cfg.db_path), cfg, ignore_session=True, client=client,
                notifier=lambda t, l: None, now=T_NOW)

    rows = it.alert_rows(cfg.db_path, limit=20)
    t_rows = [r for r in rows if r["kind"] in it.T_KINDS]
    assert [r["symbol"] for r in t_rows] == ["600009"]
    assert t_rows[0]["label"] == it.KIND_LABELS["t_high"]
    asked = [c[1] for c in client.calls if c[0] == "snapshot"]
    assert "600009" in asked[0]                 # 持仓确实被放进了快照请求
    # 补进来的持仓**也跑卖出/风控规则**（2026-09-28 主人报"盘中提醒无效"）：原来它们
    # 只跑做T，而做T默认关 —— "只把票记在持仓里"的用户就整场交易零提醒。
    # 这里成本 10.00、现价 12.34 → 止盈。
    sells = [r for r in rows if r["symbol"] == "600009" and r["kind"] not in it.T_KINDS]
    assert [r["kind"] for r in sells] == ["take_profit"]


def test_intraday_t_off_emits_nothing_and_asks_nothing(cfg, monkeypatch) -> None:
    """`intraday_t = false`：一条做T提示都不发；但持仓的止损止盈**照跑**。

    2026-09-28 改的：老实现把"持仓票"整条观察面挂在做T开关上（关掉做T连持仓都不查），
    于是主人报"盘中提醒无效" —— 他手上的票记在持仓里，止损/止盈一条都没有。
    现在做T开关只管做T，卖出/风控规则独立于它。
    """
    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    cfg.intraday_t = False
    _t_pos(cfg, "600009")          # 成本 10.00
    _t_day(cfg)
    client = FakeClient(snapshots=[
        _t_snap("600009", last=12.34, high=12.78, low=12.05, prev=12.0, vwap=12.10, pct=2.83),
    ])
    result = it.run_once(DataEngine(cfg.db_path), cfg, ignore_session=True, client=client,
                         notifier=lambda t, l: None, now=T_NOW)
    rows = it.alert_rows(cfg.db_path, limit=20)
    assert [r for r in rows if r["kind"] in it.T_KINDS] == []      # 做T提示一条都没有
    assert [r["kind"] for r in rows] == ["take_profit"]            # 止盈照发（10.00 → 12.34）
    assert result["fresh"] == 1 and result["pushed"] is True


def test_t_hints_never_enter_the_daily_pool_push(cfg, monkeypatch) -> None:
    """做T提示只走**盘中提醒**通道：不写 `push_log`（池子推送去重表）、不动股票池。"""
    monkeypatch.setenv("INTRADAY_POOL_ONLY", "1")
    _t_pos(cfg)
    _t_day(cfg)
    client = FakeClient(snapshots=[_t_snap(last=12.34, high=12.78, low=12.05, prev=12.0,
                                           vwap=12.10, pct=2.83)])
    it.run_once(DataEngine(cfg.db_path), cfg, ignore_session=True, client=client,
                notifier=lambda t, l: None, now=T_NOW)
    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_log").fetchone()[0] == 0
        assert storage.load_pool(conn) == []


# ── 持仓页「今日T提示」列的数据源 ──


def test_t_hints_today_returns_latest_and_ignores_other_kinds_and_days(cfg) -> None:
    """`t_hints_today` 只给"今天 + 做T kind"的最新一条（持仓页那一列的数据源）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.record_alerts(conn, [{"symbol": "600001", "kind": "t_high", "price": 12.3,
                                      "detail": "第一条"}], T_DAY)
        storage.record_alerts(conn, [{"symbol": "600001", "kind": "t_low", "price": 9.7,
                                      "detail": "第二条"}], T_DAY)
        storage.record_alerts(conn, [{"symbol": "600002", "kind": "stop_loss", "price": 9.0,
                                      "detail": "别的 kind"}], T_DAY)
        storage.record_alerts(conn, [{"symbol": "600003", "kind": "t_high", "price": 1.0,
                                      "detail": "昨天的"}], "2026-09-15")

    hints = it.t_hints_today(cfg.db_path, T_DAY)
    assert set(hints) == {"600001"}                      # 别的 kind、别的日期都不算
    assert hints["600001"]["detail"] == "第二条"          # 同一天里最新一条
    assert it.t_hint_cell(hints["600001"]) == "低吸（近似）"
    tooltip = it.t_hint_tooltip(hints["600001"])
    assert "第二条" in tooltip and "近似·T低吸（正T：先买后卖）" in tooltip
    # 今天没有提示：单元格空串（界面自己画 `—`），tooltip 说明"什么时候才会有提示"
    assert it.t_hint_cell(None) == ""
    assert "今天还没有做T提示" in it.t_hint_tooltip(None)
