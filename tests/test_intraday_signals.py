"""竞价强度 / 当日异动 / 涨停原因 / 提醒标的文案（全离线，假客户端）。

为什么单独一份用例
------------------
这四件事都是"用户明确点名要"的功能，而且各有各的坑：

1. **提醒里的标的要显示成 `名称（代码）`**（推送文本本来就是，界面原来只有代码）；
2. **涨停原因**：涨停池里早就有 `reason_type`（= 接口的 `limit_up_reason`）与 `continue_day_text`，
   但界面从来没用过；
3. **竞价强度**：接口的 `auction_unmatched` 实测会出现 **-1**（= 未提供），
   把它当"卖压"会把最强的票读成最弱的票 —— 这条必须钉死；另外"非竞价时段"是**正常状态**，
   不是错误（不推送、不显示 0）；
4. **异动提醒**：全市场一条请求 + 本地只留自己的票（池子/自选/持仓），
   同一只票同一天同一标签只推一次。

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
from tests.conftest import FakeResponse, FakeSession


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

    def auction_snapshot(self, thscodes, stage="live"):
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


def _auction_row(symbol: str, *, pct: float, ratio: float, unmatched: float = -1.0,
                 turnover: float = 0.0013, yesterday: float = 0.95,
                 name: str = "低价样本") -> dict:
    """一条竞价快照行（字段名照抄真实接口）。"""
    return {
        "thscode": hx.to_thscode(symbol), "ticker": symbol, "name": name,
        "auction_price": 12.5, "auction_pct": pct, "auction_volume": 158.0,
        "auction_amount": 2.02e7, "auction_unmatched": unmatched,
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
        (_scored(-1.2, 0.3, -0.7), -1, ""),      # 略低开 -1 + 卖盘 -1 + 1 = -1 → 不够弱
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
    cfg = Config(auction_alert_min_pct=5.0, auction_alert_min_volume_ratio=3.0,
                 auction_alert_min_score=5)
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


def test_auction_alerts_rank_and_cap(cfg) -> None:
    """按**分数**排序取前 N（默认 5、上限 10）：强的进提醒、弱的只对**持仓**推。"""
    _seed_targets(cfg, pool=[f"60000{i}" for i in range(1, 8)],
                  positions=["600002"])
    rows = [
        _auction_row("600001", pct=2.0, ratio=2.0, unmatched=1200.0),   # 6 分
        _auction_row("600002", pct=-2.5, ratio=0.3, unmatched=-900.0),  # 低开卖压 → -2 分
        _auction_row("600003", pct=2.0, ratio=0.5),        # 3 分（高开+成交额）
        _auction_row("600004", pct=1.2, ratio=1.6),        # 3 分（略高开+略放量+成交额）
        _auction_row("600005", pct=0.1, ratio=0.5),        # 1 分 → 不提示
        _auction_row("600006", pct=1.2, ratio=0.5),        # 2 分 → 不提示
        _auction_row("600007", pct=9.0, ratio=3.0, unmatched=2000.0),   # 6 分
    ]
    client = FakeSignalClient(auction_rows=rows)
    alerts = it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW)
    kinds = {a["symbol"]: a["kind"] for a in alerts}
    assert kinds["600001"] == "auction_strong"
    assert kinds["600007"] == "auction_strong"
    assert kinds["600002"] == "auction_weak"               # 持仓 → 弱也推
    assert "600003" in kinds or "600004" in kinds          # 3 分也是强（不用 AND）
    assert "600005" not in kinds and "600006" not in kinds
    # 分数高的排前面（600007 与 600001 都是 6 分，|涨幅| 大的在前）
    assert [a["symbol"] for a in alerts][:2] == ["600007", "600001"]
    # 分数与原始数值都要写进详情（用户要能核对"为什么给这个分"）
    assert "（分6）" in alerts[0]["detail"]
    assert "量比3.00" in alerts[0]["detail"]


def test_auction_weak_only_for_positions(cfg) -> None:
    """**弱提醒只对持仓推**：池子里但没买的票低开不报警。"""
    _seed_targets(cfg, pool=["600001", "600002"], positions=["600002"])
    rows = [
        _auction_row("600001", pct=-3.0, ratio=0.2, unmatched=-800.0),   # 池内没仓位
        _auction_row("600002", pct=-3.0, ratio=0.2, unmatched=-800.0),   # 持仓
    ]
    client = FakeSignalClient(auction_rows=rows)
    alerts = it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW)
    assert [a["symbol"] for a in alerts] == ["600002"]
    assert alerts[0]["kind"] == "auction_weak"


def test_auction_alerts_respects_max_items(cfg) -> None:
    """条数上限：默认 5 只，配置能改（**最大 10**，写大了会被夹住）。"""
    symbols = [f"60000{i}" for i in range(1, 9)]
    _seed_targets(cfg, pool=symbols)
    rows = [_auction_row(s, pct=5.0, ratio=5.0) for s in symbols]
    client = FakeSignalClient(auction_rows=rows)

    alerts = it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW)
    assert len(alerts) == 5                                # 默认 5
    cfg.auction_alert_max_items = 8
    assert len(it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg,
                                now=AUCTION_NOW)) == 8
    cfg.auction_alert_max_items = 99                       # 写超上限 → 夹到 10
    cfg.__post_init__()
    assert cfg.auction_alert_max_items == 10
    assert len(it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg,
                                now=AUCTION_NOW)) == 8     # 只有 8 只候选


def test_fetch_auction_carries_score_for_the_ui(cfg) -> None:
    """界面用的数据里带上分数与理由（卡片/详情要显示"原始数 + 分"）。"""
    _seed_targets(cfg)
    client = FakeSignalClient(auction_rows=[
        _auction_row("600001", pct=2.0, ratio=2.0, unmatched=1200.0)])
    snapshot = it.fetch_auction(cfg.db_path, cfg, client=client, now=AUCTION_NOW)
    assert snapshot["600001"]["score"] == 6
    assert "高开+2.00%" in snapshot["600001"]["reasons"]


def test_auction_alerts_skip_outside_the_window(cfg) -> None:
    """**非竞价时段不推送**：不在 9:15–9:30 时一次请求都不发。"""
    _seed_targets(cfg)
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=9.0, ratio=9.0)])
    assert it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=CLOSED_NOW) == []
    assert client.calls == []                            # 连请求都没发
    # 9:25 之后的终态窗口仍然取（拿到的是终态数据）
    assert it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=TAIL_NOW)
    assert [c[0] for c in client.calls] == ["auction"]


def test_auction_alerts_off_by_config(cfg) -> None:
    """`intraday_auction = false` → 不取、不推。"""
    _seed_targets(cfg)
    cfg.intraday_auction = False
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=9.0, ratio=9.0)])
    assert it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW) == []
    assert client.calls == []


def test_auction_alerts_batches_over_100_symbols(cfg) -> None:
    """池子+自选+持仓超过 100 只时**自动分批**（接口上限 100）。"""
    symbols = [f"{600000 + i:06d}" for i in range(130)]
    _seed_targets(cfg, pool=symbols)
    client = FakeSignalClient(auction_rows=[
        _auction_row(s, pct=3.0, ratio=3.0) for s in symbols[:3]
    ])
    alerts = it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW)
    # 分批发生在**客户端**里（见 `test_auction_snapshot_batches_over_the_limit`）：
    # 这里断言的是"130 只一次交给客户端、由它自己按 100 分批"，不重复实现一遍分批
    assert len(client.calls) == 1
    assert len(client.calls[0][1]) == 130
    assert len(alerts) == 3


def test_auction_alerts_cover_watchlist_and_positions(cfg) -> None:
    """竞价/异动看的票 = **池子 + 自选 + 持仓**（去重），不只是池子。"""
    _seed_targets(cfg, pool=["600001"], watch=["600002"], positions=["600003"])
    client = FakeSignalClient(auction_rows=[
        _auction_row("600002", pct=4.0, ratio=4.0),                     # 自选：强
        _auction_row("600003", pct=-4.0, ratio=0.2, unmatched=-900.0),  # 持仓：低开+卖盘 → 弱
    ])
    alerts = it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW)
    assert {a["symbol"] for a in alerts} == {"600002", "600003"}


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
    assert it.auction_alerts(client, db_path=cfg.db_path, cfg=cfg, now=AUCTION_NOW) == []


def test_build_alerts_includes_auction_and_anomaly(cfg, monkeypatch) -> None:
    """两者都并进同一次循环（不再加定时器）：`build_alerts` 一轮就能拿到。"""
    _seed_targets(cfg)
    client = FakeSignalClient(
        auction_rows=[_auction_row("600001", pct=3.5, ratio=3.0)],
        anomalies=[_anomaly("600001", "RAPID_RALLY")],
    )
    monkeypatch.setattr(it, "auction_fetch_window", lambda now=None: True)
    engine = DataEngine(cfg.db_path)
    alerts = it.build_alerts(engine, client, cfg=cfg)
    kinds = {a["kind"] for a in alerts}
    assert "auction_strong" in kinds
    assert "anomaly_rapid_rally" in kinds


def test_run_once_allows_the_auction_window(cfg, monkeypatch) -> None:
    """9:20（还没到 9:30）也要跑这一轮 —— 否则竞价提醒永远不会触发。"""
    _seed_targets(cfg)
    engine = DataEngine(cfg.db_path)
    monkeypatch.setattr(it, "in_session", lambda now=None: False)
    monkeypatch.setattr(it, "auction_fetch_window", lambda now=None: True)
    monkeypatch.setattr(it, "is_trading_day", lambda *a, **k: True)
    report = it.run_once(engine, cfg, dry_run=True,
                         client=FakeSignalClient(
                             auction_rows=[_auction_row("600001", pct=4.0, ratio=4.0)]))
    assert report["trading_day"] is True
    assert report["hits"] >= 1


# ── 涨停原因 ──


def test_first_board_alert_carries_the_reason() -> None:
    """首板提醒的 detail 里要带涨停原因（用户要"为什么涨"的答案）。"""
    client = FakeSignalClient(limit_up=[{
        "thscode": "600519.SH", "name": "贵州茅台", "continue_day_cnt": 1,
        "seal_money": 9e7, "last_price": 1281.0,
        "limit_up_reason": "半导体设备+业绩预增",
    }])
    hits = it.evaluate_first_board(client)
    assert hits and "半导体设备+业绩预增" in hits[0][2]


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

def test_auction_is_off_by_default_and_sends_no_request(cfg) -> None:
    """**竞价默认关闭**（口径待确认）：用默认配置跑，一次竞价请求都不发。

    这条钉的是"功能已就绪但不启用"这个状态本身 —— 用户明确要求先别把竞价接进默认流程。
    （本文件其余竞价用例用的是显式打开开关的 `cfg`，见上面的 `_enable_auction` fixture。）
    """
    _seed_targets(cfg)
    default_cfg = Config()
    assert default_cfg.intraday_auction is False
    client = FakeSignalClient(auction_rows=[_auction_row("600001", pct=9.0, ratio=9.0)])
    assert it.auction_alerts(client, db_path=cfg.db_path, cfg=default_cfg,
                             now=AUCTION_NOW) == []
    assert it.fetch_auction(cfg.db_path, default_cfg, client=client,
                            now=AUCTION_NOW) == {}
    assert client.calls == []                       # 一个请求都没发


def test_new_intraday_config_defaults_and_fallback(cfg) -> None:
    """5 个新配置项的默认值与非法值回退。"""
    fresh = Config()
    # 竞价**默认关**：功能已就绪，但口径与阈值待用户确认后才打开
    assert fresh.intraday_auction is False
    assert fresh.auction_alert_min_pct == 2.0
    assert fresh.auction_alert_min_volume_ratio == 2.0
    assert fresh.intraday_anomaly is True
    assert fresh.anomaly_alert_tags == []

    bad = Config(auction_alert_min_pct=0, auction_alert_min_volume_ratio=-1,
                 anomaly_alert_tags=" rapid_rally ,, limit_down ")
    assert bad.auction_alert_min_pct == 2.0
    assert bad.auction_alert_min_volume_ratio == 2.0
    assert bad.anomaly_alert_tags == ["RAPID_RALLY", "LIMIT_DOWN"]


def test_new_intraday_config_from_env(monkeypatch) -> None:
    """环境变量覆盖（界面上临时调试/分发时改口径用）。"""
    monkeypatch.setenv("INTRADAY_AUCTION", "false")
    monkeypatch.setenv("AUCTION_ALERT_MIN_PCT", "3.5")
    monkeypatch.setenv("INTRADAY_ANOMALY", "0")
    monkeypatch.setenv("ANOMALY_ALERT_TAGS", "limit_up,limit_down")
    cfg = config_mod.load_config(None, use_env=True)
    assert cfg.intraday_auction is False
    assert cfg.auction_alert_min_pct == 3.5
    assert cfg.intraday_anomaly is False
    assert cfg.anomaly_alert_tags == ["LIMIT_UP", "LIMIT_DOWN"]
