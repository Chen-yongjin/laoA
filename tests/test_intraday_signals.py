"""当日异动 / 涨停原因 / 提醒标的文案 / 持仓做T 提示（全离线，假客户端）。

为什么单独一份用例
------------------
这几件事都是"用户明确点名要"的功能，而且各有各的坑：

1. **提醒里的标的要显示成 `名称（代码）`**（推送文本本来就是，界面原来只有代码）；
2. **涨停原因**：涨停池里早就有 `reason_type`（= 接口的 `limit_up_reason`）与
   `continue_day_text`，但界面从来没用过；
3. **异动提醒**：全市场一条请求 + 本地只留自己的票（池子/自选/持仓），
   同一只票同一天同一标签只推一次；
4. **持仓做T 的近似提示**（`t_high` / `t_low`，见文件末尾那一节）：只对**持仓股**发，
   说法必须落在"反T（先卖后买）"或"正T（先买后卖）"这两种**T+1 下合法**的操作上，
   而且必须写清"只提示部分仓位、可卖数量以券商为准"。

> **已下线**：原来的"竞价强度"（`auction_strong`/`auction_weak`、全市场竞价扫描）已于
> 2026-10-11 主人拍板整块删除 —— 竞价现在是一条普通随包策略（`formulas/竞价策略.txt`），
> 到点在【运行】里手动跑。所以本文件里再没有一条竞价用例；
> `intraday.py` 里也不该再有任何 `auction_*` 函数（有测试钉着，见下面那条）。

Qt 界面那几条（提醒表格、涨停行）在 `tests/test_ui_smoke.py`。
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
    """只实现这几件事的假客户端（异动/涨停池/快照），并记录调用。"""

    def __init__(self, anomalies=None, limit_up=None, fail=None) -> None:
        self.anomalies = list(anomalies or [])
        self.limit_up = list(limit_up or [])
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

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


# ── 客户端：接口的真实返回 ──


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


def test_intraday_module_has_no_auction_left(cfg) -> None:
    """**竞价那一整条链路已经从 `intraday.py` 删干净**（2026-10-11 主人拍板）。

    为什么要单独钉这一条：竞价扫描当年是"到点扫全市场 + 汇总推送"的一整套东西
    （常量、打分、过滤、取数、落库、推送），删的时候最容易留下半截 ——
    而留下半截的症状是"某天又被谁接回调度器里，开始每小时打 56 个请求"。
    所以这里不但断言模块里没有那几个函数，连名字都不许再出现。

    顺手钉住"异动照旧并进同一次循环"（它是同一段代码的邻居，不该被这次删除波及）。
    """
    _seed_targets(cfg)
    for name in ("auction_scan", "auction_scan_due", "auction_scan_pause", "auction_fields",
                 "auction_score", "auction_verdict", "auction_pass", "auction_detail",
                 "auction_card_text", "auction_scan_line", "auction_summary_message",
                 "fetch_auction", "auction_window", "auction_tail", "auction_fetch_window",
                 "market_symbols", "_push_auction_scan", "amount_text", "_auction_max_items",
                 "board_of", "board_label", "AUCTION_START", "AUCTION_END", "AUCTION_TAIL",
                 "AUCTION_SCAN_BATCH", "AUCTION_SCAN_PACE", "AUCTION_SCAN_STORE_LIMIT",
                 "UNMATCHED_RATIO_STRONG"):
        assert not hasattr(it, name), f"{name} 应该已经随竞价扫描一起删掉"

    client = FakeSignalClient(anomalies=[_anomaly("600001", "RAPID_RALLY")])
    engine = DataEngine(cfg.db_path)
    alerts = it.build_alerts(engine, client, cfg=cfg)
    kinds = {a["kind"] for a in alerts}
    assert kinds == {"anomaly_rapid_rally"}      # 异动照旧产出，且只有它
    assert "auction_strong" not in it.KIND_LABELS
    assert "auction_strong" not in it.ALERT_CELL_SHORT


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
def _enable_anomaly(cfg):
    """本文件测的就是异动/做T 这些功能本身：**显式打开**，不要依赖默认值。

    当日异动默认是**关**的（用户拍板"减少无用消息"，见 config.py 的注释）——
    不打开的话，测的是"关掉之后什么都不做"，等于没测。
    （竞价那一路原来自动打开过 `intraday_auction`，2026-10-11 随功能一起删了。）
    """
    cfg.intraday_anomaly = True


def test_intraday_anomaly_config_defaults_and_fallback() -> None:
    """当日异动的默认值与非法值回退（竞价那一组已随功能删除，见 `test_config.py`）。"""
    fresh = Config()
    # 当日异动**默认关**（用户拍板：减少无用消息）——能力都在，想要就打开
    assert fresh.intraday_anomaly is False
    assert fresh.anomaly_alert_tags == []

    bad = Config(anomaly_alert_tags=" rapid_rally ,, limit_down ")
    assert bad.anomaly_alert_tags == ["RAPID_RALLY", "LIMIT_DOWN"]


def test_intraday_anomaly_config_from_env(monkeypatch) -> None:
    """环境变量覆盖异动这一路；**竞价那几个旧环境变量现在什么都不做**。"""
    monkeypatch.setenv("INTRADAY_ANOMALY", "0")
    monkeypatch.setenv("ANOMALY_ALERT_TAGS", "limit_up,limit_down")
    # 老配置/老脚本里还留着这些名字：不该报错、也不该再影响任何字段
    monkeypatch.setenv("INTRADAY_AUCTION", "true")
    monkeypatch.setenv("AUCTION_MIN_PCT", "4.5")
    monkeypatch.setenv("AUCTION_SCAN_AT", "09:18, 09:24")
    monkeypatch.setenv("AUCTION_BOARDS", "chinext,star")
    cfg = config_mod.load_config(None, use_env=True)
    assert cfg.intraday_anomaly is False
    assert cfg.anomaly_alert_tags == ["LIMIT_UP", "LIMIT_DOWN"]
    for gone in ("intraday_auction", "auction_min_pct", "auction_scan_at", "auction_boards"):
        assert not hasattr(cfg, gone), f"{gone} 应该已经随竞价扫描一起删掉"


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
