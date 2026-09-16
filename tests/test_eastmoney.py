"""东方财富客户端：单位口径（手→股、分→元）、分页、secid、日K 列序、失败降级。

这些用例**一条都不联网**：所有请求都走注入式 `opener`（返回固定 JSON），
socket 层还有 conftest 的 `_block_network` 兜底 —— 真联网会直接报错。
固定 JSON 里有两类：
  * **本地构造**的极小响应（把某一种口径/边界单独钉死，断言最锋利）；
  * **真实抓下来的响应**（`tests/fixtures/eastmoney/*.json`，2026-09-16 实测，
    带原始字段与数值）—— 用来钉住"我们解析的就是真接口的形状"，而不是我们自己想象的形状。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laoa_trader.data import eastmoney as em
from laoa_trader.data import sources

#: 真实响应快照（抓取脚本见交付报告；`clist`/`ulist`/`stock/get`/`kline`）
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eastmoney"


def fixture(name: str) -> dict:
    """读一份真实响应 JSON（文件名不含 `.json`）。"""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeOpener:
    """注入式 opener：按顺序吐出预置响应，并记下每次请求的 `(url, params)`。

    响应用完还被打 → 直接断言失败（这正是在测"没有多余的请求"）。
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict, timeout: float) -> dict:
        self.calls.append((url, dict(params)))
        assert self.responses, f"多打了一个请求：{url} {params}"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def rows_response(*rows: dict, total: int | None = None) -> dict:
    """造一个 `clist`/`ulist` 形状的信封。"""
    return {"rc": 0, "data": {"total": len(rows) if total is None else total,
                              "diff": list(rows)}}


# 真实响应里的两行（`clist_page1.json`，2026-09-16 实测）—— 单位用例的基准
REAL_688837 = {
    "f2": 40.18, "f3": 45.58, "f4": 12.58, "f5": 360792, "f6": 1521904022.0,
    "f12": "688837", "f14": "N信诺维", "f15": 48.66, "f16": 39.12,
    "f17": 47.57, "f18": 27.6,
}


# ── 注入式 opener：默认路径不联网、被注入时一次都不用 ──


def test_injected_opener_means_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了 `opener` 就**绝不该**走 `_urllib_json`（这一条是"测试不联网"的地基）。"""

    def boom(*args, **kwargs):
        raise AssertionError("注入 opener 时不允许走真实 HTTP")

    monkeypatch.setattr(em, "_urllib_json", boom)
    out = em.snapshot(["600519"], opener=FakeOpener(rows_response(REAL_688837)))
    assert [row["symbol"] for row in out] == ["688837"]


def test_default_path_goes_through_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """不给 `opener` 时走 `_urllib_json`（把默认路径也钉住，免得两边都"以为对方在测"）。"""
    seen: list[str] = []

    def fake(url, params, timeout):
        seen.append(url)
        return fixture("ulist_batch")

    monkeypatch.setattr(em, "_urllib_json", fake)
    out = em.snapshot(["600519", "000001", "920819"])
    assert seen == [f"{em.PUSH2}/api/qt/ulist.np/get"]
    assert {row["symbol"] for row in out} == {"600519", "000001", "920819"}


# ── 代码 → secid ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "1.600519"),         # 沪市主板
        ("688837", "1.688837"),         # 科创板（6 开头）
        ("900901", "1.900901"),         # 沪B（9 开头）
        ("000001", "0.000001"),         # 深市主板
        ("300243", "0.300243"),         # 创业板（3 开头）
        ("sh.600519", "1.600519"),
        ("SH.600519", "1.600519"),
        ("600519.SH", "1.600519"),
        ("600519.sh", "1.600519"),
        ("sz.000001", "0.000001"),
        ("1.600519", "1.600519"),       # 已经是 secid：原样返回
        ("0.000001", "0.000001"),
        # 北交所：**实测**是 `0.`（`0.920819` 取到颖泰生物 3.09 元；`1.830799` 回 rc=100）
        ("920819", "0.920819"),
        ("920819.BJ", "0.920819"),
        ("bj.830799", "0.830799"),
        ("430047", "0.430047"),
        ("830799", "0.830799"),
        ("870204", "0.870204"),
        # 认不出来的一律空串（宁可不取价，也不猜一个别的标的回来）
        ("", ""),
        ("   ", ""),
        ("abc", ""),
        ("60051X", ""),
        ("1234567", ""),
        ("510300", ""),                 # ETF（5 开头）：本模块不猜
        ("159915", ""),                 # ETF（1 开头）：本模块不猜
        ("600519.XX", ""),              # 认不出的后缀
    ],
)
def test_to_secid(raw: str, expected: str) -> None:
    assert em.to_secid(raw) == expected


def test_to_secid_is_never_guessed_for_unknown_prefix() -> None:
    """`to_secid("")`/ETF 这类返回空串后，取数路径**一次请求都不发**。"""
    opener = FakeOpener()
    assert em.snapshot(["510300", "159915", "abc"], opener=opener) == []
    assert opener.calls == []
    assert em.daily("510300", "2026-06-01", "2026-09-16", opener=opener) == []
    assert opener.calls == []


# ── 归一化：手 → 股、元/分 两种口径 ──


def test_normalize_row_yuan_caliber_units() -> None:
    """`clist`+`fltt=2`：价格是元、成交量是**手**（×100 得股）。"""
    row = em.normalize_row(REAL_688837, em.CALIBER_YUAN)
    assert row is not None
    assert row["symbol"] == "688837"
    assert row["name"] == "N信诺维"
    assert row["last_price"] == 40.18          # 元，不除以 100
    assert row["prev_close"] == 27.6
    assert row["open"] == 47.57
    assert row["high"] == 48.66
    assert row["low"] == 39.12
    assert row["volume"] == 360792 * 100       # 手 → 股（实测 f5=360792 手）
    assert row["turnover"] == 1521904022.0     # 元，不动
    assert row["pct"] == 45.58                 # 百分数，不除以 100


def test_normalize_row_fen_caliber_divides_by_100() -> None:
    """`stock/get`（不带 fltt）：价格是**分**、涨跌幅是**百分之一**。"""
    payload = fixture("stock_get_600519")["data"]
    row = {
        "f12": payload["f57"], "f14": payload["f58"], "f2": payload["f43"],
        "f3": payload["f170"], "f5": payload["f47"], "f6": payload["f48"],
        "f15": payload["f44"], "f16": payload["f45"], "f17": payload["f46"],
        "f18": payload["f60"],
    }
    out = em.normalize_row(row, em.CALIBER_FEN)
    assert out is not None
    assert out["last_price"] == 1258.0          # 125800 分 = 1258.00 元
    assert out["prev_close"] == 1272.75
    assert out["open"] == 1273.93
    assert out["high"] == 1274.98
    assert out["low"] == 1254.10
    assert out["pct"] == -1.16                  # f170=-116 → −1.16%
    assert out["volume"] == 26235 * 100         # 手 → 股


def test_snapshot_one_parses_real_response_in_fen() -> None:
    """`snapshot_one` 走的是"不带 fltt"的单只端点 → 必须按分口径解析。"""
    out = em.snapshot_one("600519", opener=FakeOpener(fixture("stock_get_600519")))
    assert out is not None
    assert out["symbol"] == "600519"
    assert out["name"] == "贵州茅台"
    assert out["last_price"] == 1258.0
    assert out["volume"] == 2623500
    assert out["turnover"] == 3307926407.0


def test_two_calibers_agree_on_the_same_real_moment() -> None:
    """同一刻的两个端点（`ulist` 带 fltt=2 / `stock/get` 不带）必须给出**同一个价**。

    这是"分口径 ÷100"这条规则最硬的证据（两份响应都是 2026-09-16 实测抓下来的）：
    `ulist` 的 `f2 = 1258.0`（元）与 `stock/get` 的 `f43 = 125800`（分）是同一个数。
    """
    ulist = em.normalize_row(
        next(r for r in fixture("ulist_batch")["data"]["diff"] if r["f12"] == "600519"),
        em.CALIBER_YUAN,
    )
    stock = em.snapshot_one("600519", opener=FakeOpener(fixture("stock_get_600519")))
    assert ulist is not None and stock is not None
    assert stock["last_price"] * 100 == fixture("stock_get_600519")["data"]["f43"] == 125800
    assert ulist["last_price"] == stock["last_price"] == 1258.0
    # 涨跌幅同理：ulist 的 f3 已经是百分数（-1.16），stock/get 的 f170=-116 要 ÷100
    assert ulist["pct"] == stock["pct"] == -1.16
    # 成交量与成交额两边也一致（都是"手"与"元"）
    assert ulist["volume"] == stock["volume"] == 2623500
    assert ulist["turnover"] == stock["turnover"] == 3307926407.0


def test_normalize_row_drops_zero_or_missing_price() -> None:
    """停牌/未开盘（`f2` 为 0）与"已切换"的老代码（f43=0）都不该进表。"""
    assert em.normalize_row({"f12": "600519", "f2": 0}, em.CALIBER_YUAN) is None
    assert em.normalize_row({"f12": "600519", "f2": "-"}, em.CALIBER_YUAN) is None
    assert em.normalize_row({"f12": "600519"}, em.CALIBER_YUAN) is None
    # 没有代码的行（接口偶尔给空行）也丢掉
    assert em.normalize_row({"f2": 10.0}, em.CALIBER_YUAN) is None
    assert em.normalize_row({"f12": "abc", "f2": 10.0}, em.CALIBER_YUAN) is None
    # 老北交所代码：前缀对、f43=0 → 没有现价（实测 0.830799 = "艾融软件(已切换)"）
    assert em.snapshot_one("830799", opener=FakeOpener({"rc": 0, "data": {
        "f43": 0, "f44": 0, "f45": 0, "f46": 0, "f47": 0, "f48": 0.0,
        "f57": "830799", "f58": "艾融软件(已切换)", "f60": 3428, "f170": 0,
    }})) is None


def test_normalize_row_sentinel_drops_impossible_price() -> None:
    """口径写反（分被当成元）会得到 12 万元级的价 —— 哨兵把它丢掉，不许进表。

    说清这道哨兵的边界：它只在"值大得不可能"时生效；反过来（元被当成分）
    以及低价股被误读，它**看不出**——所以口径仍然由端点/参数钉死（见 `CALIBER_*`）。
    """
    row = em.normalize_row({"f12": "600519", "f2": 125800.0, "f15": 127498.0},
                           em.CALIBER_YUAN)
    assert row is None
    assert em.MAX_PLAUSIBLE_PRICE == 10000.0
    # 真正的高价股（茅台 1258.00 元）不受影响
    assert em.normalize_row({"f12": "600519", "f2": 1258.0}, em.CALIBER_YUAN)["last_price"] == 1258.0


def test_rows_to_map_keys_by_symbol() -> None:
    out = em.rows_to_map([REAL_688837, {"f12": "600519", "f2": 0}], em.CALIBER_YUAN)
    assert list(out) == ["688837"]


# ── 批量快照（ulist.np）──


def test_snapshot_uses_fltt2_and_batches_secids() -> None:
    """一次请求带一批 secids，**必须带 `fltt=2`**（不带就是分口径）。"""
    opener = FakeOpener(rows_response(), rows_response())
    out = em.snapshot([f"60{i:04d}" for i in range(150)], opener=opener, pause=0)
    assert out == []
    assert len(opener.calls) == 2                       # 150 只 / 每批 100 → 2 次
    url, params = opener.calls[0]
    assert url == f"{em.PUSH2}/api/qt/ulist.np/get"
    assert params["fltt"] == 2
    assert params["fields"] == em.SNAPSHOT_FIELDS
    assert len(params["secids"].split(",")) == 100
    assert len(opener.calls[1][1]["secids"].split(",")) == 50
    # secid 前缀按代码推出来（沪市 6 开头）
    assert params["secids"].startswith("1.600000")


def test_snapshot_parses_real_fixture_rows() -> None:
    """真实抓下来的批量响应：三只票都能解析，含北交所（`0.920819`）。"""
    out = em.snapshot(["600519", "000001", "920819"],
                      opener=FakeOpener(fixture("ulist_batch")))
    assert [row["symbol"] for row in out] == ["600519", "000001", "920819"]
    by_symbol = {row["symbol"]: row for row in out}
    assert by_symbol["600519"]["last_price"] == 1258.0
    assert by_symbol["600519"]["open"] == 1273.93        # 与当日 kline 开盘价一字不差
    assert by_symbol["000001"]["last_price"] == 11.7
    assert by_symbol["920819"]["last_price"] == 3.09     # 北交所：0. 前缀实测可用
    assert by_symbol["920819"]["pct"] == -0.32


def test_snapshot_keeps_going_when_one_batch_fails() -> None:
    """一批失败不影响别的批（只记日志），也不抛。"""
    opener = FakeOpener(em.EastmoneyError("boom"), rows_response(REAL_688837))
    out = em.snapshot([f"60{i:04d}" for i in range(150)], opener=opener, pause=0)
    assert [row["symbol"] for row in out] == ["688837"]


def test_snapshot_returns_empty_when_everything_fails() -> None:
    out = em.snapshot(["600519"], opener=FakeOpener(em.EastmoneyError("boom")))
    assert out == []


# ── 全市场分页（clist）──


def test_snapshot_all_paginates_and_dedupes() -> None:
    """两页拼起来、按代码去重、`total` 到了就停。"""
    page1 = rows_response(*[{"f12": f"60{i:04d}", "f2": 10.0 + i, "f5": i}
                            for i in range(3)], total=5)
    page2 = rows_response(*[{"f12": f"60{i:04d}", "f2": 10.0 + i, "f5": i}
                            for i in range(2, 5)], total=5)     # 600002 重复给一次
    opener = FakeOpener(page1, page2)
    out = em.snapshot_all(page_size=3, opener=opener, pause=0)
    assert [row["symbol"] for row in out] == ["600000", "600001", "600002", "600003", "600004"]
    assert len(opener.calls) == 2
    assert opener.calls[0][1]["pn"] == 1 and opener.calls[0][1]["pz"] == 3
    assert opener.calls[1][1]["pn"] == 2
    assert opener.calls[0][1]["fs"] == em.MARKET_FS
    assert opener.calls[0][1]["fltt"] == 2


def test_snapshot_all_stops_on_short_page() -> None:
    """服务端没给满一页 = 没有下一页（不必再多打一次）。"""
    opener = FakeOpener(rows_response({"f12": "600000", "f2": 10.0}))
    assert len(em.snapshot_all(page_size=200, opener=opener, pause=0)) == 1
    assert len(opener.calls) == 1


def test_snapshot_all_respects_max_pages_and_returns_partial_on_error() -> None:
    opener = FakeOpener(
        rows_response(*[{"f12": f"60{i:04d}", "f2": 10.0} for i in range(3)], total=999),
        em.EastmoneyError("第二页挂了"),
    )
    out = em.snapshot_all(page_size=3, opener=opener, pause=0)
    assert [row["symbol"] for row in out] == ["600000", "600001", "600002"]
    opener2 = FakeOpener(rows_response(*[{"f12": f"60{i:04d}", "f2": 10.0} for i in range(3)],
                                       total=999))
    assert len(em.snapshot_all(page_size=3, max_pages=1, opener=opener2, pause=0)) == 3
    assert len(opener2.calls) == 1


def test_snapshot_all_real_first_page() -> None:
    """真实抓下来的全市场首页：`total=5559`（2026-09-16 实测），行能解析。"""
    data = fixture("clist_page1")
    assert data["data"]["total"] == 5559
    opener = FakeOpener({"rc": 0, "data": {**data["data"], "total": 5}})
    out = em.snapshot_all(page_size=5, opener=opener, pause=0)
    assert out[0]["symbol"] == "688837" and out[0]["last_price"] == 40.18
    assert out[0]["volume"] == 360792 * 100


# ── 日K ──


def test_daily_column_order_and_units_from_real_fixture() -> None:
    """真实日K：列序是 `日期,开,收,高,低,成交量(手),成交额(元),振幅%`（实测）。"""
    out = em.daily("600519", "2026-06-01", "2026-09-16", opener=FakeOpener(fixture("kline_600519")))
    assert len(out) == 77                                  # 实测 77 行
    assert out[0] == {
        "date": "2026-06-01", "open": 1298.98, "close": 1281.58, "high": 1298.98,
        "low": 1273.29, "volume": 4384500, "turnover": 5741133268.00,
    }
    assert out[-1]["date"] == "2026-09-16"
    assert out[-1]["close"] == 1258.00
    assert out[-1]["high"] == 1274.98 and out[-1]["low"] == 1254.10
    assert out[-1]["volume"] == 26235 * 100                # 手 → 股
    assert out[-1]["turnover"] == 3307926407.00            # 元
    # 与同一刻的快照对得上（成交额与成交量两个字段完全一致）
    snap = em.snapshot(["600519"], opener=FakeOpener(fixture("ulist_batch")))[0]
    assert snap["turnover"] == out[-1]["turnover"]
    assert snap["volume"] == out[-1]["volume"]


def test_daily_real_fixture_first_and_last_two_rows() -> None:
    """前 2 行 / 后 2 行的原始字符串逐字核对（交付报告里那个实测对照的同一份数据）。"""
    klines = fixture("kline_600519")["data"]["klines"]
    assert klines[:2] == [
        "2026-06-01,1298.98,1281.58,1298.98,1273.29,43845,5741133268.00,1.98",
        "2026-06-02,1277.98,1279.20,1298.34,1272.98,36362,4769963104.00,1.98",
    ]
    assert klines[-2:] == [
        "2026-09-15,1281.00,1272.75,1284.50,1271.28,13762,1756915149.00,1.03",
        "2026-09-16,1273.93,1258.00,1274.98,1254.10,26235,3307926407.00,1.64",
    ]


@pytest.mark.parametrize(("adjust", "fqt"), [("qfq", 1), ("hfq", 2), ("none", 0), ("raw", 0)])
def test_daily_adjust_maps_to_fqt(adjust: str, fqt: int) -> None:
    opener = FakeOpener({"rc": 0, "data": {"klines": []}})
    em.daily("600519", "2026-06-01", "2026-09-16", adjust, opener=opener)
    url, params = opener.calls[0]
    assert url == f"{em.PUSH2HIS}/api/qt/stock/kline/get"       # 历史是另一个域名
    assert params["fqt"] == fqt
    assert params["klt"] == 101
    assert params["beg"] == "20260601" and params["end"] == "20260916"
    assert params["secid"] == "1.600519"


def test_daily_unknown_adjust_raises() -> None:
    """复权口径是**代码给的**，写错属于编程错误 → 直接抛（不要静默按不复权算）。"""
    with pytest.raises(ValueError):
        em.daily("600519", "2026-06-01", "2026-09-16", "后复权")


def test_daily_bad_rows_are_skipped() -> None:
    """列数不够 / 日期不是日期 的行跳过，不影响整段。"""
    opener = FakeOpener({"rc": 0, "data": {"klines": [
        "2026-06-01,1298.98,1281.58,1298.98,1273.29,43845,5741133268.00,1.98",
        "garbage",
        ",,,,,,",
        "not-a-date,1,2,3,4,5,6,7",
    ]}})
    out = em.daily("600519", "2026-06-01", "2026-09-16", opener=opener)
    assert [row["date"] for row in out] == ["2026-06-01"]


def test_daily_returns_empty_on_error_and_on_missing_payload() -> None:
    assert em.daily("600519", "2026-06-01", "2026-09-16",
                    opener=FakeOpener(em.EastmoneyError("boom"))) == []
    assert em.daily("600519", "2026-06-01", "2026-09-16",
                    opener=FakeOpener({"rc": 0, "data": None})) == []
    assert em.daily("600519", "2026-06-01", "2026-09-16",
                    opener=FakeOpener({"rc": 0, "data": {"klines": "不是列表"}})) == []


# ── 代码表 ──


def test_stock_list_pairs_code_and_name_without_industry() -> None:
    """代码表只有 `symbol` 与 `name` —— 这一路拿不到行业，就不返回行业字段。"""
    opener = FakeOpener(rows_response(
        {"f12": "600519", "f14": "贵州茅台"}, {"f12": "000001", "f14": "平安银行"},
        total=2,
    ))
    out = em.stock_list(opener=opener, page_size=2, pause=0)
    assert out == [{"symbol": "600519", "name": "贵州茅台"},
                   {"symbol": "000001", "name": "平安银行"}]
    assert set(out[0]) == {"symbol", "name"}
    assert opener.calls[0][1]["fields"] == "f12,f14"


def test_stock_list_stops_on_error() -> None:
    opener = FakeOpener(em.EastmoneyError("boom"))
    assert em.stock_list(opener=opener) == []


# ── 信封与异常 ──


def test_envelope_rc_nonzero_is_an_error() -> None:
    """`rc=100`（实测：`1.830799` 这种不存在的 secid 组合）= 没有这个标的 → 抛错。

    对 `snapshot_one` 来说它表现为 None（不抛给界面）。
    """
    with pytest.raises(em.EastmoneyError):
        em._get_json("http://x/y", {}, 1.0, opener=FakeOpener({"rc": 100, "data": None}))
    assert em.snapshot_one("600519", opener=FakeOpener({"rc": 100, "data": None})) is None


def test_non_dict_response_is_an_error() -> None:
    with pytest.raises(em.EastmoneyError):
        em._get_json("http://x/y", {}, 1.0, opener=FakeOpener(["不是对象"]))


def test_unified_keys_must_match_sources() -> None:
    """`eastmoney.UNIFIED_KEYS` 与 `sources.QUOTE_FIELDS` 是同一份契约（两处定义会漂）。"""
    assert em.UNIFIED_KEYS == sources.QUOTE_FIELDS
    assert em.LOTS_TO_SHARES == 100


def test_snapshot_map_of_eastmoney_rows_is_consumable_by_sources() -> None:
    """东方财富给的行丢进 `sources` 的归一化后，统一口径的键一个都不少。"""
    out = em.snapshot(["600519"], opener=FakeOpener(fixture("ulist_batch")))
    assert set(out[0]) == set(em.UNIFIED_KEYS)
    assert set(out[0]) == set(sources.QUOTE_FIELDS)
