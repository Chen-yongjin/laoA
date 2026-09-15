"""竞价扫描（**全市场扫描 + 可配置过滤规则**）的测试。

为什么单独一个文件
------------------
这一版竞价的设计是"全市场扫一遍，再按用户的规则过滤"，所以要点有三块，
都不适合塞进 `test_intraday_signals.py`（那里是解析/打分/窗口）：

1. **过滤规则**逐条（涨幅下限/上限、板块多选、成交额、打分、条数）；
2. **节奏**（代码来源、100 只一批、批间 ≥0.3 秒、只在配置的时刻扫）；
3. **展示**（汇总推送文案、落库、详情弹窗那一节、设置页那一组能写回配置）。

全部离线：假客户端 + `auction_scan_pause` 不真睡（真睡 55×0.3 秒会让用例慢到没法用）。
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from laoa_trader import config as config_mod  # noqa: E402
from laoa_trader import intraday as it  # noqa: E402
from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.data.engine import DataEngine  # noqa: E402
from tests._toml import p  # noqa: E402

DAY = "2026-09-15"
MORNING = datetime(2026, 9, 15, 9, 20, 5)      # 09:20 那一档
CLOSE_SLOT = datetime(2026, 9, 15, 9, 25, 30)  # 09:25 那一档（终态）
BETWEEN = datetime(2026, 9, 15, 9, 23, 0)      # 两档之间（宽限窗口已过）


# ── 假客户端 ──


class FakeClient:
    """只做竞价扫描需要的事：竞价快照 + 代码表兜底，并记录每次调用。"""

    def __init__(self, rows=None, tickers=None, snapshot_rows=None) -> None:
        self.rows = list(rows or [])
        self.tickers_rows = list(tickers or [])
        self.snapshot_rows = list(snapshot_rows or [])
        self.calls: list[tuple[str, tuple]] = []
        self.gaps: list[float] = []

    def auction_snapshot(self, thscodes, stage="live", chunk=100):
        self.calls.append(("auction", tuple(thscodes)))
        wanted = set(thscodes)
        return {"item": [r for r in self.rows if r.get("thscode") in wanted],
                "failed": [], "phase": "live", "status": "final", "timestamp": 0}

    def tickers(self, **kwargs):
        self.calls.append(("tickers", ()))
        return list(self.tickers_rows)

    def snapshot(self, thscodes=None, **kwargs):
        self.calls.append(("snapshot", tuple(thscodes or ())))
        return list(self.snapshot_rows)

    def limit_up_pool(self, day=None, size=200):
        return []

    def anomaly_list(self, tag_codes=None):
        return []


def _row(symbol: str, *, pct: float, ratio: float = 3.0, amount: float = 2e7,
         unmatched: float = 1000.0, name: str = "样本") -> dict:
    """一条竞价快照行（字段名照抄真实接口）。"""
    thscode = symbol if "." in symbol else f"{symbol}." + {
        "6": "SH", "3": "SZ", "0": "SZ", "4": "BJ", "8": "BJ", "9": "BJ",
    }.get(symbol[0], "SH")
    return {
        "thscode": thscode, "name": name,
        "auction_price": 12.5, "auction_pct": pct, "auction_volume": 158.0,
        "auction_amount": amount, "auction_unmatched": unmatched,
        "auction_turnover_pct": 0.0013, "auction_yesterday_ratio_pct": 0.95,
        "auction_volume_ratio": ratio, "pre_close_price": 12.2,
    }


def _seed(cfg, symbols: list[str]) -> None:
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [(s, f"样本{s}", "行业") for s in symbols])


@pytest.fixture()
def cfg(tmp_path, monkeypatch) -> Config:
    """打开竞价扫描的临时配置（其余阈值走默认）。"""
    for name in ("INTRADAY_AUCTION", "AUCTION_MIN_PCT", "AUCTION_MAX_PCT",
                 "AUCTION_MIN_AMOUNT", "AUCTION_MIN_SCORE", "AUCTION_BOARDS",
                 "AUCTION_SCAN_AT", "AUCTION_ALERT_MAX_ITEMS"):
        monkeypatch.delenv(name, raising=False)
    config = Config(data_dir=tmp_path / "data", hithink_api_key="")
    config.intraday_auction = True
    config.ensure_dirs()
    return config


@pytest.fixture()
def nopause(monkeypatch):
    """把"批间等待"换成记录（**不真睡**）：节奏断言靠它，用例才跑得快。"""
    gaps: list[float] = []
    monkeypatch.setattr(it, "auction_scan_pause", lambda seconds: gaps.append(seconds))
    return gaps


# ── 板块判定 ──


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("600519", "main"), ("601318", "main"), ("000001", "main"),
        ("002371", "main"), ("003816", "main"),
        ("300750", "chinext"), ("301236", "chinext"),
        ("688981", "star"), ("689009", "star"),
        ("430047", "bj"), ("830799", "bj"), ("871981", "bj"), ("920002", "bj"),
        ("430047.BJ", "bj"), ("600519.SH", "main"), ("300750.SZ", "chinext"),
        ("688981.SH", "star"),
    ],
)
def test_board_of_by_prefix_and_suffix(symbol, expected) -> None:
    """板块判定：`.BJ` 后缀最稳（北交所前缀太多），没有后缀才按前缀推。"""
    assert it.board_of(symbol) == expected


def test_board_labels_are_chinese() -> None:
    assert it.board_label("chinext") == "创业板"
    assert it.board_label("star") == "科创板"
    assert it.board_label("bj") == "北交所"
    assert it.board_label("main") == "主板"


# ── 过滤规则（逐条 + 组合）──


def _fields(symbol: str, pct: float, amount: float = 2e7, ratio: float = 3.0) -> dict:
    return it.auction_fields(
        _row(symbol, pct=pct, amount=amount, ratio=ratio), name="样本"
    )


def test_filter_pct_lower_and_upper_bounds() -> None:
    """涨幅下限（默认 +2%）与上限（默认 9%）都要生效 —— 上限挡的是"买不进"的一字板。"""
    cfg = Config()
    assert it.auction_pass(_fields("600001", 2.0), cfg)[0] is True      # 正好等于下限 → 留
    assert it.auction_pass(_fields("600001", 1.99), cfg) == (False, "涨幅下限")
    assert it.auction_pass(_fields("600001", 8.99), cfg)[0] is True
    assert it.auction_pass(_fields("600001", 9.0), cfg) == (False, "涨幅上限")
    assert it.auction_pass(_fields("600001", 9.9), cfg) == (False, "涨幅上限")
    # 停牌/没有竞价数据（涨幅缺失）→ 不留
    assert it.auction_pass({"symbol": "600001", "pct": None, "amount": 2e7}, cfg) \
        == (False, "涨幅缺失")


def test_filter_amount_and_custom_bounds() -> None:
    """成交额下限默认 500 万；阈值全部走配置（改完立即生效）。"""
    cfg = Config()
    assert it.auction_pass(_fields("600001", 3.0, amount=5e6), cfg)[0] is True
    assert it.auction_pass(_fields("600001", 3.0, amount=4.9e6), cfg) == (False, "成交额")
    loose = Config(auction_min_pct=0.5, auction_max_pct=15.0, auction_min_amount=3e6)
    assert it.auction_pass(_fields("600001", 1.0, amount=3.5e6), loose)[0] is True


@pytest.mark.parametrize(
    "boards, symbol, expected",
    [
        (["chinext"], "300750", True),
        (["chinext"], "600519", False),
        (["chinext"], "688981", False),
        (["star"], "688981", True),
        (["star"], "300750", False),
        (["main"], "000001", True),
        (["main"], "300750", False),
        (["bj"], "430047", True),
        (["bj"], "600519", False),
        # 只勾科创 + 创业 = "只看双创"
        (["chinext", "star"], "300750", True),
        (["chinext", "star"], "688981", True),
        (["chinext", "star"], "600519", False),
        # 排除北交所
        (["main", "chinext", "star"], "430047", False),
        (["main", "chinext", "star"], "600519", True),
    ],
)
def test_filter_boards_each_and_in_combination(boards, symbol, expected) -> None:
    """板块多选：单勾、双勾（双创）、排除北交所都要按勾选来。"""
    cfg = Config(auction_boards=boards)
    assert it.auction_pass(_fields(symbol, 3.0), cfg)[0] is expected


def test_boards_empty_means_no_restriction() -> None:
    """一个都不勾 = 不限制（等于全选）—— 免得用户看到"0 只命中"却不知道为什么。"""
    cfg = Config(auction_boards=[])
    assert cfg.auction_boards == list(config_mod.AUCTION_BOARDS)
    assert it.auction_pass(_fields("430047", 3.0), cfg)[0] is True
    assert it.auction_pass(_fields("688981", 3.0), cfg)[0] is True


def test_scan_keeps_only_selected_boards(cfg, nopause) -> None:
    """扫描时板块过滤在**请求之前**生效：只勾创业板 → 请求里只有 300/301 的代码。"""
    symbols = ["600001", "600002", "300001", "300002", "688001", "430047"]
    _seed(cfg, symbols)
    cfg.auction_boards = ["chinext"]
    client = FakeClient(rows=[_row(s, pct=4.0) for s in symbols])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING, slot="09:20")
    codes = [c for name, c in client.calls if name == "auction"]
    asked = [code for batch in codes for code in batch]
    assert sorted(asked) == ["300001.SZ", "300002.SZ"]
    assert scan["scanned"] == 2
    assert {hit["symbol"] for hit in scan["hits"]} == {"300001", "300002"}


def test_scan_filters_one_li_ban_out(cfg, nopause) -> None:
    """一字板（涨幅 ≥ 上限）不进结果 —— 这是"买不进的不推"那条规则。"""
    _seed(cfg, ["600001", "600002"])
    client = FakeClient(rows=[_row("600001", pct=10.0), _row("600002", pct=4.0)])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING, slot="09:20")
    assert [hit["symbol"] for hit in scan["hits"]] == ["600002"]
    assert scan["skipped"].get("涨幅上限") == 1


def test_scan_score_threshold_and_max_items(cfg, nopause) -> None:
    """打分门限（默认 2）与条数上限（默认 10，上限 50）都要按配置走。"""
    symbols = [f"{600000 + i:06d}" for i in range(12)]
    _seed(cfg, symbols)
    rows = [_row(s, pct=3.0, ratio=3.0, unmatched=1000.0) for s in symbols]
    client = FakeClient(rows=rows)

    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING, slot="09:20")
    assert scan["kept"] == 12
    assert len(scan["alerts"]) == 10                      # 默认推 10 只

    cfg.auction_alert_max_items = 50                      # 上限 50
    cfg.__post_init__()
    assert cfg.auction_alert_max_items == 50
    assert len(it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING,
                               slot="09:20")["alerts"]) == 12   # 只有 12 只候选

    cfg.auction_alert_max_items = 999
    cfg.__post_init__()
    assert cfg.auction_alert_max_items == 50


# ── 扫描时刻 ──


def test_scan_due_only_at_configured_slots() -> None:
    """只在 `auction_scan_at` 到点时扫：默认 09:20 与 09:25 各一次。"""
    cfg = Config()
    assert cfg.auction_scan_at == ["09:20", "09:25"]
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 10), cfg, []) is None
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 20, 5), cfg, []) == "09:20"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 22, 59), cfg, []) == "09:20"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 25), cfg, []) == "09:25"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 27, 59), cfg, []) == "09:25"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 40), cfg, []) is None
    assert it.auction_scan_due(datetime(2026, 9, 15, 15, 0), cfg, []) is None


def test_scan_due_does_not_repeat_the_same_slot() -> None:
    """同一档扫过就不再扫（调度器每分钟一拍，不能变成每分钟扫一次全市场）。"""
    cfg = Config()
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 20, 5), cfg, ["09:20"]) is None
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 25, 5), cfg,
                               ["09:20"]) == "09:25"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 25, 5), cfg,
                               ["09:20", "09:25"]) is None


def test_scan_due_grace_window_does_not_cross_the_next_slot() -> None:
    """宽限窗口被下一档截断：09:23 之后不再补 09:20 那档（否则会与 09:25 挤在一起连发）。"""
    cfg = Config()
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 22, 59), cfg, []) == "09:20"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 23, 0), cfg, []) is None


def test_scan_due_with_custom_times() -> None:
    """扫描时刻可配（界面上就是那个输入框）；非法项被丢掉、认不出的回默认。"""
    cfg = Config(auction_scan_at=["09:18", "9:40", "09:18"])
    assert cfg.auction_scan_at == ["09:18", "09:40"]        # 去重 + 排序 + 补零
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 18, 10), cfg, []) == "09:18"
    assert it.auction_scan_due(datetime(2026, 9, 15, 9, 40, 10), cfg, []) == "09:40"
    assert Config(auction_scan_at=["25:00", "abc"]).auction_scan_at == ["09:20", "09:25"]
    good, bad = config_mod.split_scan_at("09:70, 09:20")
    assert good == ["09:20"] and bad == ["09:70"]


# ── 代码来源（全市场）──


def test_market_symbols_prefers_local_table(cfg) -> None:
    """优先本地 `stock_basic`：**零请求**（下载时已经写好了这张表）。"""
    _seed(cfg, ["600001", "300001"])
    client = FakeClient(tickers=[{"thscode": "600002.SH", "name": "接口里的"}])
    got = it.market_symbols(cfg.db_path, cfg, client)
    assert got == {"600001": "样本600001", "300001": "样本300001"}
    assert client.calls == []


def test_market_symbols_falls_back_to_tickers(cfg) -> None:
    """库还空时（首次运行没数据）退回 `meta/tickers/list`，并在日志里说明。"""
    storage.init_db(cfg.db_path)
    client = FakeClient(tickers=[{"thscode": "600002.SH", "name": "接口里的"},
                                 {"thscode": "300001.SZ", "name": "创业板样本"}])
    got = it.market_symbols(cfg.db_path, cfg, client)
    assert got == {"600002": "接口里的", "300001": "创业板样本"}
    assert [c[0] for c in client.calls] == ["tickers"]


def test_market_symbols_falls_back_to_snapshot(cfg) -> None:
    """代码表接口也不行时再退到全市场快照分页（**没有名称**，名称退回代码）。"""
    storage.init_db(cfg.db_path)
    client = FakeClient(tickers=[], snapshot_rows=[{"thscode": "600002.SH"}])
    got = it.market_symbols(cfg.db_path, cfg, client)
    assert got == {"600002": ""}
    assert [c[0] for c in client.calls] == ["tickers", "snapshot"]


def test_market_symbols_empty_without_source(cfg) -> None:
    """连兜底都拿不到 → 返回空字典（本次不扫，而不是崩掉）。"""
    storage.init_db(cfg.db_path)
    client = FakeClient(tickers=[], snapshot_rows=[])
    assert it.market_symbols(cfg.db_path, cfg, client) == {}


def test_scan_without_code_source_does_nothing(cfg, nopause) -> None:
    storage.init_db(cfg.db_path)
    client = FakeClient()
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING, slot="09:20")
    assert scan["hits"] == [] and scan["scanned"] == 0
    assert [c for c in client.calls if c[0] == "auction"] == []


# ── 节奏（批大小 / 批间等待）──


def test_scan_batches_by_100_and_paces_between_batches(cfg, nopause) -> None:
    """5573 只 → 56 批；批间等 `AUCTION_SCAN_PACE`（≥0.3 秒），串行不并发。"""
    _seed(cfg, [f"{600000 + i:06d}" for i in range(5573)])
    client = FakeClient(rows=[_row("600000", pct=4.0)])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=MORNING, slot="09:25")
    batches = [c for c in client.calls if c[0] == "auction"]
    assert len(batches) == 56
    assert [len(c[1]) for c in batches][:3] == [100, 100, 100]
    assert len(batches[-1][1]) == 73                      # 5573 = 55×100 + 73
    assert len(nopause) == 55                             # 批与批之间各等一次
    assert min(nopause) >= 0.3
    assert scan["scanned"] == 5573


def test_scan_one_batch_failure_does_not_stop_the_rest(cfg, monkeypatch, nopause) -> None:
    """某一批取不到 → 跳过它、其余批照常（竞价缺一只不该让整次扫描失败）。"""
    _seed(cfg, [f"{600000 + i:06d}" for i in range(250)])

    class Flaky(FakeClient):
        """第 2 批挂掉（第 1 批正常返回）。"""

        def auction_snapshot(self, thscodes, stage="live", chunk=100):
            if len(self.calls) == 1:
                self.calls.append(("auction", tuple(thscodes)))
                raise RuntimeError("这一批挂了")
            return super().auction_snapshot(thscodes, stage, chunk)

    # 命中票放在**第 1 批**（600000~600099），第 2 批挂掉不该影响它
    scan = it.auction_scan(Flaky(rows=[_row("600050", pct=4.0)]), db_path=cfg.db_path,
                           cfg=cfg, now=MORNING, slot="09:20")
    assert [hit["symbol"] for hit in scan["hits"]] == ["600050"]
    assert scan["scanned"] == 250                        # 250 只照常交给扫描（分 3 批）


# ── 落库与展示 ──


def test_auction_scan_round_trip_in_storage(cfg) -> None:
    """`auction_scan` 表：写入 → 读出（名次、板块、分数、★、真实总数）。"""
    storage.init_db(cfg.db_path)
    rows = [
        {"rank": 1, "symbol": "300001", "name": "创业板样本", "board": "chinext",
         "pct": 5.2, "volume_ratio": 3.2, "amount": 2.1e8, "unmatched": 3400.0,
         "score": 6, "pushed": True},
        {"rank": 2, "symbol": "600001", "name": "主板样本", "board": "main",
         "pct": 3.0, "volume_ratio": 2.0, "amount": 6e7, "unmatched": -800.0,
         "score": 4, "pushed": False},
    ]
    with storage.connect(cfg.db_path) as conn:
        assert storage.write_auction_scan(conn, DAY, "09:25", rows, total=37) == 2
        saved = storage.load_auction_scan(conn, DAY)
        assert [r["symbol"] for r in saved] == ["300001", "600001"]
        assert saved[0]["score"] == 6 and saved[0]["pushed"] == 1
        assert saved[0]["total"] == 37 and saved[0]["board"] == "chinext"
        assert storage.auction_scan_slots(conn, DAY) == ["09:25"]
        # 同一档重扫 → 覆盖（不是追加，"命中 37 只"不能变成 74 只）
        storage.write_auction_scan(conn, DAY, "09:25", rows[:1], total=40)
        assert len(storage.load_auction_scan(conn, DAY)) == 1
        assert storage.load_auction_scan(conn, DAY)[0]["total"] == 40
        assert storage.load_auction_scan(conn, "2000-01-01") == []


def test_scan_stores_all_hits_but_pushes_only_top(cfg, nopause) -> None:
    """落库留**全部命中**（上限 300 条）、推送只前 N 只，★ 也只在推送那几只上。"""
    symbols = [f"{600000 + i:06d}" for i in range(8)]
    _seed(cfg, symbols)
    cfg.auction_alert_max_items = 3
    client = FakeClient(rows=[_row(s, pct=4.0) for s in symbols])

    class Engine:
        db_path = cfg.db_path

    it._push_auction_scan(client, Engine(), cfg, DAY, "09:25", MORNING,
                          dry_run=False, notifier=lambda t, l: None)
    with storage.connect(cfg.db_path) as conn:
        saved = storage.load_auction_scan(conn, DAY)
    assert len(saved) == 8
    assert [r["pushed"] for r in saved] == [1, 1, 1, 0, 0, 0, 0, 0]


def test_summary_message_format(cfg, nopause) -> None:
    """汇总：标题"共命中 N 只，推送前 M"；正文按分数编号、带板块/数值/分数。"""
    _seed(cfg, ["300001", "688001", "600001"])
    client = FakeClient(rows=[
        _row("300001", pct=5.21, ratio=3.2, unmatched=3400.0, amount=2.1e8, name="创业板甲"),
        _row("688001", pct=4.0, ratio=2.0, unmatched=2000.0, amount=6e7, name="科创乙"),
        _row("600001", pct=3.0, ratio=1.0, unmatched=-900.0, amount=7e7, name="主板丙"),
    ])
    scan = it.auction_scan(client, db_path=cfg.db_path, cfg=cfg, now=CLOSE_SLOT, slot="09:25")
    assert scan["title"] == "⚡ 竞价扫描（9:25）｜共命中 3 只，推送前 3："
    assert scan["lines"][0].startswith("1. 创业板甲（300001） 创业板 +5.21% ")
    assert "（分6）" in scan["lines"][0]
    assert len(scan["lines"]) == 3
    assert scan["lines"][2].startswith("3. 主板丙（600001） 主板 ")
    assert "卖盘剩余900手" in scan["lines"][2]
    # 没有命中时标题仍然说得清（0 只），正文为空
    empty_title, empty_lines = it.auction_summary_message([], [], slot="09:20")
    assert empty_title == "⚡ 竞价扫描（9:20）｜0 只命中" and empty_lines == []


def test_scan_result_feeds_the_detail_dialog(cfg, nopause) -> None:
    """详情弹窗那一节：列全部命中、★ 标出已推送的、池内的票标「池内」。"""
    from laoa_trader.ui import app as ui_app
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    symbols = ["300001", "600001", "600002"]
    _seed(cfg, symbols)
    with storage.connect(cfg.db_path) as conn:
        # 让 600001 进池子（池内的票在结果里要标出来）
        storage.save_pool(conn, [{"symbol": "600001", "name": "主板甲",
                                  "strategy": "LowPriceStrategy",
                                  "strategies": "LowPriceStrategy", "score": 1.0,
                                  "reason": "r"}], DAY)
    client = FakeClient(rows=[
        _row("300001", pct=5.0, name="创业板甲"),
        _row("600001", pct=4.0, name="主板甲"),
        _row("600002", pct=3.0, name="主板乙"),
    ])
    cfg.auction_alert_max_items = 2
    it._push_auction_scan(client, DataEngine(cfg.db_path), cfg, DAY, "09:25", CLOSE_SLOT,
                          dry_run=False, notifier=lambda t, l: None)

    window = ui_app.MainWindow(cfg)
    try:
        window.cfg.market_overview = False
        lines = window._auction_detail_lines()
        text = "\n".join(lines)
        assert "竞价扫描（2026-09-15 09:25）共命中 3 只" in text
        assert "★ = 已推送前 2 只" in text
        assert "创业板甲（300001） 创业板 +5.00%" in text
        assert "主板甲（600001）" in text and "（池内）" in text
        stars = [line for line in lines[1:] if line.strip().startswith("★")]
        assert len(stars) == 2                          # 前 2 只标星
        assert "设置页" not in text                      # 有结果时不显示"去哪儿开"
        app.processEvents()
    finally:
        window._timer.stop()
        window._market_timer.stop()
        window._auction_timer.stop()
        window.scheduler.stop()
        window.tray.hide()
        window.close()
        window.deleteLater()
        app.processEvents()


def test_detail_dialog_explains_when_there_is_no_scan_yet(cfg) -> None:
    """还没有扫描结果时，详情里说清"默认什么时候扫、在哪儿开"。"""
    from laoa_trader.ui import app as ui_app
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    storage.init_db(cfg.db_path)
    window = ui_app.MainWindow(cfg)
    try:
        window.cfg.market_overview = False
        text = "\n".join(window._auction_detail_lines())
        assert "还没有结果" in text and "09:20" in text and "设置页" in text
    finally:
        window._timer.stop()
        window._market_timer.stop()
        window._auction_timer.stop()
        window.scheduler.stop()
        window.tray.hide()
        window.close()
        window.deleteLater()
        app.processEvents()


# ── 设置页（那一组输入要能写回 config.toml 并立即生效）──


@pytest.fixture()
def settings_window(cfg, tmp_path):
    """设置页用例：一份写好注释的 config.toml + 主窗口（其余后台动作按住）。"""
    from PySide6.QtWidgets import QApplication

    from laoa_trader.ui import app as ui_app

    app = QApplication.instance() or QApplication([])
    config_file = cfg.data_dir / "config.toml"
    config_file.write_text(
        "# 用户自己的注释（保存设置后必须还在）\n"
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        'my_own_key = "别动我"      # 未知键\n',
        encoding="utf-8",
    )
    cfg.source_path = config_file
    storage.init_db(cfg.db_path)
    window = ui_app.MainWindow(cfg)
    window.cfg.market_overview = False
    try:
        yield window, cfg, config_file
    finally:
        window._timer.stop()
        window._market_timer.stop()
        window._auction_timer.stop()
        window.scheduler.stop()
        window.tray.hide()
        window.close()
        window.deleteLater()
        app.processEvents()


def test_settings_group_reflects_current_config(settings_window) -> None:
    """进设置页时，那一组控件显示的是**当前配置**（不是写死的默认值）。"""
    window, cfg, _ = settings_window
    cfg.auction_min_pct = 3.5
    cfg.auction_max_pct = 8.0
    cfg.auction_min_score = 4
    cfg.auction_alert_max_items = 20
    cfg.auction_boards = ["star", "bj"]
    cfg.auction_scan_at = ["09:19", "09:26"]
    window2 = type(window)(cfg)          # 用新配置重建一次（页面照配置渲染）
    try:
        window2.cfg.market_overview = False
        assert window2.auction_min_pct_box.value() == 3.5
        assert window2.auction_max_pct_box.value() == 8.0
        assert window2.auction_score_box.value() == 4
        assert window2.auction_items_box.value() == 20
        assert window2.auction_board_boxes["star"].isChecked() is True
        assert window2.auction_board_boxes["bj"].isChecked() is True
        assert window2.auction_board_boxes["main"].isChecked() is False
        assert window2.auction_scan_at_edit.text() == "09:19, 09:26"
    finally:
        window2._timer.stop()
        window2._market_timer.stop()
        window2._auction_timer.stop()
        window2.scheduler.stop()
        window2.tray.hide()
        window2.close()
        window2.deleteLater()


def test_save_auction_writes_config_and_takes_effect(settings_window) -> None:
    """保存竞价设置：写回 config.toml（保留注释与未知键）+ 内存配置立即生效。"""
    window, cfg, config_file = settings_window
    window.auction_on_box.setChecked(True)
    window.auction_min_pct_box.setValue(2.5)
    window.auction_max_pct_box.setValue(8.5)
    window.auction_amount_box.setValue(300.0)          # 300 万
    window.auction_score_box.setValue(3)
    window.auction_items_box.setValue(15)
    window.auction_board_boxes["main"].setChecked(False)
    window.auction_board_boxes["bj"].setChecked(False)
    window.auction_scan_at_edit.setText("09:19, 09:26")
    window.on_save_auction()

    text = config_file.read_text(encoding="utf-8")
    assert "# 用户自己的注释（保存设置后必须还在）" in text
    assert 'my_own_key = "别动我"' in text
    assert "intraday_auction = true" in text
    assert "auction_min_pct = 2.5" in text
    assert "auction_max_pct = 8.5" in text
    assert "auction_min_amount = 3000000.0" in text
    assert "auction_min_score = 3" in text
    assert "auction_alert_max_items = 15" in text
    assert 'auction_boards = ["chinext", "star"]' in text
    assert 'auction_scan_at = ["09:19", "09:26"]' in text
    # 内存配置同步（不用重启）
    assert cfg.intraday_auction is True
    assert cfg.auction_min_pct == 2.5 and cfg.auction_max_pct == 8.5
    assert cfg.auction_min_amount == 3e6
    assert cfg.auction_boards == ["chinext", "star"]
    assert cfg.auction_scan_at == ["09:19", "09:26"]
    assert "竞价设置已保存" in window.status_label.fullText()
    assert "上次扫描" in window.auction_hint.text() or "还没有扫描结果" in window.auction_hint.text()


def test_save_auction_rejects_inverted_range(settings_window) -> None:
    """涨幅上限 ≤ 下限 → **拒绝写入**并说明原因（坏配置不该落盘）。"""
    window, cfg, config_file = settings_window
    before = config_file.read_text(encoding="utf-8")
    window.auction_min_pct_box.setValue(6.0)
    window.auction_max_pct_box.setValue(5.0)
    window.on_save_auction()
    assert config_file.read_text(encoding="utf-8") == before     # 一个字都没写
    assert "必须大于下限" in window.status_label.fullText()


def test_save_auction_keeps_all_boards_when_none_checked(settings_window) -> None:
    """一个板块都不勾 = 不限制：保存成"全选"，而不是空列表（空列表会让扫描 0 命中）。"""
    window, cfg, config_file = settings_window
    for box in window.auction_board_boxes.values():
        box.setChecked(False)
    window.on_save_auction()
    assert 'auction_boards = ["main", "chinext", "star", "bj"]' in \
        config_file.read_text(encoding="utf-8")
    assert cfg.auction_boards == ["main", "chinext", "star", "bj"]


def test_save_auction_ignores_bad_scan_times(settings_window) -> None:
    """扫描时刻写错：认不出的项丢掉（保留能认的），并在提示里说明。"""
    window, cfg, config_file = settings_window
    window.auction_scan_at_edit.setText("09:20, 25:99")
    window.on_save_auction()
    assert 'auction_scan_at = ["09:20"]' in config_file.read_text(encoding="utf-8")
    assert "认不出的时刻已忽略" in window.status_label.fullText()
    assert cfg.auction_scan_at == ["09:20"]


# ── 调度器：到点的扫描不能被"每分钟一拍"错过 ──


def test_scheduler_runs_a_due_scan_even_when_the_interval_has_not_elapsed(cfg, monkeypatch) -> None:
    """竞价扫描到点时**不受 `intraday_interval` 限制**（否则会正好错过渡口）。

    为什么必须有这一条：调度器是"每 TICK 看一下到没到盘子"，而常规提醒还有
    `intraday_interval`（默认 60 秒）这道间隔闸门。09:20 那一档只给 3 分钟宽限，
    上一拍恰好落在 09:22:30、下一拍就到了 09:23:30 —— 只按间隔判断就会**整档错过**。
    """
    from laoa_trader import scheduler as scheduler_mod
    from laoa_trader.data.engine import DataEngine

    calls: list[dict] = []
    sched = scheduler_mod.Scheduler(cfg, DataEngine(cfg.db_path))
    monkeypatch.setattr(it, "in_session", lambda now=None: False)      # 9:20 还没开盘
    monkeypatch.setattr(
        it, "run_once", lambda *a, **k: calls.append(k) or {"hits": 0, "fresh": 0}
    )
    sched._last_intraday_ts = __import__("time").time()                # 刚刚才跑过一轮
    sched._maybe_intraday(MORNING)
    assert len(calls) == 1                                            # 到点 → 照跑
    sched._maybe_intraday(BETWEEN)                                    # 不在任何一档 → 不跑
    assert len(calls) == 1


def test_scheduler_does_not_scan_outside_the_slots(cfg, monkeypatch) -> None:
    """不在扫描时刻、也不在交易时段 → 一轮都不跑（零请求）。"""
    from laoa_trader import scheduler as scheduler_mod
    from laoa_trader.data.engine import DataEngine

    calls: list[int] = []
    sched = scheduler_mod.Scheduler(cfg, DataEngine(cfg.db_path))
    monkeypatch.setattr(it, "in_session", lambda now=None: False)
    monkeypatch.setattr(it, "run_once", lambda *a, **k: calls.append(1) or {})
    sched._maybe_intraday(datetime(2026, 9, 15, 10, 30))
    sched._maybe_intraday(datetime(2026, 9, 15, 9, 10))
    assert calls == []
