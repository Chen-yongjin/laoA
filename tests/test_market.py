"""大盘概览（`laoa_trader/market.py`）：取数口径、页面排版、两级 TTL 缓存、逐路降级。

**全部离线**：一路用假客户端（`FakeMarketClient`）覆盖概览层的逻辑，
一路用**真实 `HithinkClient` + 假 `Session`** 钉住线上契约（`size=1`、
`thscodes` 批量、非法代码逐只重试）。`tests/conftest.py` 的 `_block_network`
在 socket 层封死了 IPv4/IPv6，任何漏网的真实请求都会变成一条失败。

这里核对的是**用户用真实接口验证过的口径**（数字照抄）：
今天 涨停 55 / 跌停 16 / 炸板 30；上证 3885.33 -0.07%（成交额 7792.8 亿）、
深成 13384.57 -0.64%（8498.9 亿）、创业板 3285.58 -1.10%、科创50 1528.27 -1.62%、
沪深300 4480.08 -0.67%；情绪 883404.TI 885.53 -0.18% / 883900.TI 3068.96 +3.21%。
"""

from __future__ import annotations

import pytest

from laoa_trader import config as config_mod
from laoa_trader import market
from laoa_trader.data import hithink as hx

from tests.conftest import FakeResponse, FakeSession
from tests._toml import p

#: 指数/情绪/板块快照的假数据（照抄用户用真实接口逐个验证过的当天数值）
SAMPLE_ROWS: list[dict] = [
    # 宽基
    {"thscode": "000001.SH", "last_price": 3885.33, "price_change_ratio_pct": -0.07,
     "turnover": 7792.4e8},
    {"thscode": "399001.SZ", "last_price": 13384.57, "price_change_ratio_pct": -0.64,
     "turnover": 8498.9e8},
    {"thscode": "399006.SZ", "last_price": 3285.58, "price_change_ratio_pct": -1.10},
    {"thscode": "000688.SH", "last_price": 1528.27, "price_change_ratio_pct": -1.62},
    {"thscode": "000300.SH", "last_price": 4480.08, "price_change_ratio_pct": -0.67},
    # 宽基里**必须有的那两只**（用户要求加上证50；中证2000 拿不到，换成中证1000，
    # 见 `market.WIDE_INDEX_EXTRA_CODES` 的说明）。数值是 2026-09-17 腾讯实测量的
    # 收盘点位，用来说明"这一行确实在页面上"（不是编的数）
    {"thscode": "000016.SH", "last_price": 2844.23, "price_change_ratio_pct": +0.42},
    {"thscode": "000852.SH", "last_price": 7548.82, "price_change_ratio_pct": -1.21},
    # 情绪
    {"thscode": "883404.TI", "last_price": 885.529, "price_change_ratio_pct": -0.18},
    {"thscode": "883958.TI", "last_price": 6928.171, "price_change_ratio_pct": +3.53},
    {"thscode": "883994.TI", "last_price": 1551.879, "price_change_ratio_pct": +1.69},
    {"thscode": "883418.TI", "last_price": 2131.00, "price_change_ratio_pct": +0.95},
    # 板块（同花顺一级行业）
    {"thscode": "881155.TI", "last_price": 1408.771, "price_change_ratio_pct": +0.88},
    {"thscode": "881157.TI", "last_price": 1428.643, "price_change_ratio_pct": -0.27},
]

#: 概览页应当长成的样子（用户给的样例；一行一组、组名在前，组内用 `｜` 分隔）。
#: 2026-09-17 起成交额是**沪 + 深 一个数**（7792.4 + 8498.9 = 16291.3 → 16291 亿），
#: 不再分成"沪 … 深 … 北 …"三格（用户要求；北交所那一格删掉）。
#: 这里按 `market_breadth = false` 渲染，所以涨跌家数是 `—`
#: （默认是开的，开着的版本见 `test_page_sample_with_breadth_on`）。
SAMPLE_LINES = [
    "涨停 55 · 跌停 16 · 炸板 30 ｜ 成交额 16291亿",
    "上涨 — · 下跌 — · 平盘 —",
    "宽基：上证 3885.33 -0.07% ｜ 深成 13384.57 -0.64% ｜ 创业板 3285.58 -1.10% ｜ "
    "科创50 1528.27 -1.62% ｜ 沪深300 4480.08 -0.67% ｜ 上证50 2844.23 +0.42% ｜ "
    "中证1000 7548.82 -1.21%",
    "情绪：同花顺情绪 885.53 -0.18% ｜ 昨日连板 6928.17 +3.53% ｜ "
    "昨日打首板 1551.88 +1.69% ｜ 微盘股 2131.00 +0.95%",
    "板块：银行 1408.77 +0.88% ｜ 证券 1428.64 -0.27%",
]


class FakeMarketClient:
    """假的同花顺客户端：只实现概览用到的那三个方法。

    为什么不直接用 conftest 的 `FakeClient`：那个是给 sync/intraday 用的
    （snapshot / limit_up_pool / download_dump）。概览要的是"池子家数 / 指数快照 /
    全市场分页"这套接口，这里按**真实客户端的签名与返回结构**重写一份，
    测出来的才是真实契约（尤其是 `index_snapshot` 的 `{"item", "failed"}` 结构）。
    """

    def __init__(
        self,
        *,
        totals: dict[str, int] | None = None,
        rows: list[dict] | None = None,
        index_fail: tuple[str, ...] = (),
        index_error: Exception | None = None,
        totals_error: tuple[str, ...] = (),
        pages: list[dict] | None = None,
        page_size: int = 1000,
        pages_error: Exception | None = None,
    ) -> None:
        self.totals = dict(totals or {})
        self.rows = list(rows or [])
        self.index_fail = set(index_fail)
        self.index_error = index_error
        self.totals_error = set(totals_error)
        self.pages = list(pages or [])
        self.page_size = page_size
        self.pages_error = pages_error
        self.calls: list[tuple[str, object]] = []

    # -- 概览用到的三个方法 --

    def special_pool_total(self, path: str, day: str | None = None) -> int:
        self.calls.append(("total", path))
        if path in self.totals_error:
            raise hx.HithinkError(5001, "测试假客户端：这一路取不到", path)
        return int(self.totals.get(path, 0))

    def index_snapshot(self, thscodes: list[str]) -> dict:
        self.calls.append(("index", tuple(thscodes)))
        if self.index_error is not None:
            raise self.index_error
        wanted = set(thscodes)
        return {
            "item": [dict(r) for r in self.rows if r["thscode"] in wanted],
            "failed": [c for c in thscodes if c in self.index_fail],
        }

    def request(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        offset = int(params.get("offset") or 0)
        self.calls.append(("request", (path, offset)))
        if self.pages_error is not None:
            raise self.pages_error
        page = offset // max(self.page_size, 1)
        return self.pages[page] if page < len(self.pages) else {"item": [], "total": 0}

    # -- 断言辅助 --

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)

    def offsets(self) -> list[int]:
        """全市场分页请求的 offset 序列（验证"逐页翻、没重复取"）。"""
        return [arg[1] for name, arg in self.calls if name == "request"]


def _default_totals() -> dict[str, int]:
    return {
        market.LIMIT_UP_PATH: 55,
        market.LIMIT_DOWN_PATH: 16,
        market.LIMIT_BREAK_PATH: 30,
    }


def _client(**kwargs) -> FakeMarketClient:
    kwargs.setdefault("totals", _default_totals())
    kwargs.setdefault("rows", SAMPLE_ROWS)
    return FakeMarketClient(**kwargs)


@pytest.fixture(autouse=True)
def _fresh_cache():
    """每个用例都从空缓存开始：缓存是**模块级**的，不隔离会互相串味。"""
    market.clear_cache()
    yield
    market.clear_cache()


@pytest.fixture()
def mcfg(cfg):
    """概览配置：宽基五个 + 情绪四个 + 板块两个（与 config.py 的默认值一致），宽度汇总关。"""
    cfg.market_overview = True
    cfg.market_indices = list(config_mod.DEFAULT_MARKET_INDICES)
    cfg.market_sentiment_indices = list(config_mod.DEFAULT_MARKET_SENTIMENT)
    cfg.market_sector_indices = list(config_mod.DEFAULT_MARKET_SECTOR)
    cfg.market_breadth = False
    cfg.market_overview_ttl = 55
    return cfg


# ── 1) 线上契约：用真实客户端 + 假 Session 钉住请求形状 ──


def test_pool_totals_use_size_one_and_pagination_total() -> None:
    """涨跌停家数：`size=1` + `pagination.total`（**不拉明细**，也不翻页）。"""
    session = FakeSession({
        "limit-up-pool": FakeResponse(
            {"code": 0, "data": {"item": [{"thscode": "600519.SH"}],
                                 "pagination": {"total": 55, "pages": 55}}}
        ),
        "limit-down-pool": FakeResponse(
            {"code": 0, "data": {"item": [], "pagination": {"total": 16}}}
        ),
        "limit-break-pool": FakeResponse(
            {"code": 0, "data": {"item": [], "pagination": {"total": 30}}}
        ),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)

    assert client.special_pool_total(market.LIMIT_UP_PATH) == 55
    assert client.special_pool_total(market.LIMIT_DOWN_PATH) == 16
    assert client.special_pool_total(market.LIMIT_BREAK_PATH) == 30

    assert len(session.calls) == 3                 # 每路只打一次（size=1 就没有"下一页"）
    for url, params in session.calls:
        assert "limit-up-pool" in url or "limit-down-pool" in url or \
            "limit-break-pool" in url
        assert params["size"] == 1                 # 契约：只取 1 条，total 才是全量家数
        assert "date_ms" not in params             # day=None → 交给服务器判"今日"

    # 指定交易日时带上 date_ms（北京时间零点的毫秒戳），口径与其它端点一致
    session.calls.clear()
    client.special_pool_total(market.LIMIT_UP_PATH, day="2026-09-11")
    assert session.calls[0][1]["date_ms"] == hx.date_to_ms("2026-09-11")


def test_pool_total_falls_back_when_pagination_missing() -> None:
    """契约变化（没有 pagination）时退回本页条数，而不是抛异常。"""
    session = FakeSession({
        "limit-up-pool": FakeResponse({"code": 0, "data": {"item": [{"a": 1}]}}),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    assert client.special_pool_total(market.LIMIT_UP_PATH) == 1


def test_index_snapshot_batches_codes_in_one_request() -> None:
    """指数快照：宽基与情绪**同一批**发出（一个请求），返回行原样交给上层。"""
    session = FakeSession({
        "/a-share-index/prices/snapshot": FakeResponse(
            {"code": 0, "data": {"item": [{"thscode": "000001.SH", "last_price": 3885.33}]}}
        ),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)

    result = client.index_snapshot(["000001.SH", "399001.SZ", "sh.000300", "399001.SZ"])
    assert len(session.calls) == 1
    assert session.calls[0][1]["thscodes"] == "000001.SH,399001.SZ,000300.SH"  # 去重且保序
    assert result["item"][0]["last_price"] == 3885.33
    assert result["failed"] == []


def test_invalid_code_makes_batch_fail_then_retries_one_by_one() -> None:
    """非法代码会让**整批** 1002 —— 所以必须逐只重试，保留能取到的那些。

    这是真实踩过的坑：把不存在的 `899050.BJ` 混进去，连上证指数都拿不到。
    """

    def route(params: dict):
        codes = str(params.get("thscodes") or "")
        if "," in codes or codes == "899050.BJ":
            return FakeResponse({"code": 1002, "message": "Unknown thscode"})
        return FakeResponse({"code": 0, "data": {"item": [
            {"thscode": codes, "last_price": 1.0, "price_change_ratio_pct": -0.5},
        ]}})

    session = FakeSession({"/a-share-index/prices/snapshot": route})
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)

    result = client.index_snapshot(["000001.SH", "899050.BJ", "399001.SZ"])
    assert [row["thscode"] for row in result["item"]] == ["000001.SH", "399001.SZ"]
    assert result["failed"] == ["899050.BJ"]       # 取不到的记在 failed 里，不静默丢
    assert len(session.calls) == 4                 # 1 次批量 + 3 次逐只


def test_index_snapshot_gives_up_immediately_on_auth_error() -> None:
    """Key 无效/数据未就绪跟"哪个代码"无关：不做 N 次逐只重试（那是白打）。"""
    session = FakeSession({
        "/a-share-index/prices/snapshot": FakeResponse(
            {"code": 2003, "message": "Missing X-api-key"}
        ),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    with pytest.raises(hx.HithinkAuthError):
        client.index_snapshot(["000001.SH", "399001.SZ"])
    assert len(session.calls) == 1


def test_index_snapshot_raises_batch_error_when_all_singles_fail() -> None:
    """批量失败且逐只也没一只成功 → 抛原始批量错误（比 N 个单只错误好定位）。"""
    session = FakeSession({
        "/a-share-index/prices/snapshot": FakeResponse(
            {"code": 1002, "message": "Unknown thscode"}
        ),
    })
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    with pytest.raises(hx.HithinkError) as excinfo:
        client.index_snapshot(["000001.SH"])
    assert excinfo.value.code == 1002
    assert len(session.calls) == 2                 # 批量 1 次 + 逐只 1 次


# ── 2) 概览层：解析与排版 ──


def test_overview_parses_limits_turnover_and_indices(mcfg) -> None:
    client = _client()
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert overview["limits"] == {"up": 55, "down": 16, "break": 30}
    # 沪/深成交额直接取"上证指数/深证成指"的 turnover（口径见 market.py 开头）
    assert overview["turnover"]["sh"] == pytest.approx(7792.4e8)
    assert overview["turnover"]["sz"] == pytest.approx(8498.9e8)
    assert overview["turnover"]["bj"] is None          # 全市场汇总关着
    assert overview["turnover"]["total"] == pytest.approx(7792.4e8 + 8498.9e8)
    assert overview["breadth"] is None
    assert overview["breadth_enabled"] is False
    assert overview["failed"] == []
    assert overview["errors"] == []
    assert overview["stale"] is False
    assert overview["as_of"]                              # "2026-09-14 15:03" 这类时间戳
    assert overview["configured_groups"] == ["indices", "sentiment", "sector"]

    names = [item["name"] for item in overview["indices"]]
    # 宽基 = 配置里的五只 + **必须有的两只**（`market.WIDE_INDEX_EXTRA_CODES`：
    # 上证50 / 中证1000，2026-09-17 用户要求加，中证2000 拿不到所以换成中证1000）
    assert names == ["上证", "深成", "创业板", "科创50", "沪深300",
                     "上证50", "中证1000"]
    first = overview["indices"][0]
    assert first["thscode"] == "000001.SH"
    assert first["last"] == pytest.approx(3885.33)
    assert first["change_pct"] == pytest.approx(-0.07)
    # 情绪组：同花顺情绪 / 昨日连板 / 昨日打首板 / 微盘股
    assert [item["name"] for item in overview["sentiment"]] == [
        "同花顺情绪", "昨日连板", "昨日打首板", "微盘股",
    ]
    # 板块组：同花顺一级行业指数（881155 银行 / 881157 证券）
    assert [item["name"] for item in overview["sector"]] == ["银行", "证券"]
    assert overview["sentiment"][0]["last"] == pytest.approx(885.529)
    # 三路取数：3 个池子 + **1 次**三组指数的批量（同一个端点，不分批）
    assert client.count("total") == 3
    assert client.count("index") == 1


def test_lines_match_the_agreed_sample(mcfg) -> None:
    """页面文本＝用户给的样例（一行一组、组名在前、组内 `｜` 分隔、A 股正负号）。"""
    overview = market.fetch_overview(mcfg, client=_client(), force=True)
    assert market.lines(overview) == SAMPLE_LINES
    assert len(market.lines(overview)) == market.LINE_COUNT == 5


def test_page_sample_with_breadth_on() -> None:
    """打开 `market_breadth`（现在是默认值）时：摘要行是"涨跌停 + **沪深合计成交额**"，
    第二行是涨跌家数（数字照抄用户实测的 `上涨 3126 · 下跌 2224 · 平盘 221`）。

    2026-09-17 改版（用户要求）：成交额 = 沪 + 深 **一个数**，
    **北交所那一格删掉** —— 所以这里同时钉住"三个数合成 16291 亿"与
    "`bj` 就算有值（140.1 亿）也不再出现在任何一行文本里"。
    页脚那句"每 5 分钟更新"只提涨跌家数：成交额已经和指数同一节拍（每分钟），
    再把它写进这句话就是错的。
    """
    overview = market._skeleton(configured=["indices", "sentiment", "sector"])
    overview.update({
        "limits": {"up": 55, "down": 16, "break": 30},
        # total 就是"沪 + 深"（真实路径在 `fetch_overview` 里算出来；这里手搓骨架
        # 所以要自己给全这一份，否则成交额会显示 —）
        "turnover": {"sh": 7792.4e8, "sz": 8498.9e8, "bj": 140.1e8,
                     "total": 7792.4e8 + 8498.9e8},
        "breadth": {"up": 3126, "down": 2224, "flat": 221},
        "breadth_enabled": True,
        "as_of": "2026-09-14 15:03",
    })
    lines = market.lines(overview)
    assert lines[0] == "涨停 55 · 跌停 16 · 炸板 30 ｜ 成交额 16291亿"
    assert "北" not in lines[0] and "140亿" not in lines[0]      # 北交所不再显示
    assert lines[1] == "上涨 3126 · 下跌 2224 · 平盘 221"
    assert lines[2:] == ["宽基：—", "情绪：—", "板块：—"]
    assert market.footer_text(overview) == (
        market.FOOTER_PREFIX + " · 更新于 2026-09-14 15:03"
        "（每分钟自动刷新；涨跌家数每 5 分钟更新）"
    )
    # `kpi_values()` 与命令行同源：界面那一行 7 个数的口径就是这一份
    values = market.kpi_values(overview)
    assert values["成交额"] == "16291亿"
    assert list(values) == ["成交额", "涨停", "跌停", "炸板", "上涨", "下跌", "平盘"]
    assert "北成交额" not in values and "沪成交额" not in values and "深成交额" not in values
    # 关掉 `market_breadth`：涨跌家数显示 —，且底部不再提"每 5 分钟"
    # （成交额仍然有数：它来自两只宽基指数，与全市场快照无关）
    overview["breadth_enabled"] = False
    overview["breadth"] = None
    overview["turnover"]["bj"] = None
    assert market.lines(overview)[0] == "涨停 55 · 跌停 16 · 炸板 30 ｜ 成交额 16291亿"
    assert market.lines(overview)[1] == "上涨 — · 下跌 — · 平盘 —"
    assert market.footer_text(overview).endswith("（每分钟自动刷新）")


def test_lines_show_dash_when_nothing_available() -> None:
    """什么都拿不到时：各组都是 `—`，绝不显示 0 或空白（0 家涨停是另一种意思）。

    注意：`configured_groups` 为空（没配）= 整行不显示，所以这里先按"都配了"喂数据。
    """
    assert market.lines(None)[0] == "涨停 — · 跌停 — · 炸板 — ｜ 成交额 —"
    assert market.lines(None)[1] == "上涨 — · 下跌 — · 平盘 —"
    assert market.lines(None)[2:] == ["", "", ""]      # 没配置的组整行不显示

    partial = {
        "limits": {"up": 55, "down": None, "break": None},
        "turnover": {"sh": 7792e8, "total": 7792e8},
        "configured_groups": ["indices", "sentiment", "sector"],
        "indices": [{"thscode": "000001.SH", "name": "上证", "last": 3885.33,
                     "change_pct": None}],
        "sentiment": [],
        "sector": [{"thscode": "881155.TI", "name": "银行", "last": None,
                    "change_pct": None}],
    }
    lines = market.lines(partial)
    # 只取到沪市那一边时合计就是那一边（2026-09-17 起成交额 = 沪 + 深，一个数）
    assert lines[0] == "涨停 55 · 跌停 — · 炸板 — ｜ 成交额 7792亿"
    assert lines[2] == "宽基：上证 3885.33 —"          # 缺涨跌幅 → 那一段是 —
    assert lines[3] == "情绪：—"                        # 配了但没数据 → 组名 + —
    assert lines[4] == "板块：银行 —"                   # 只有代码没有点位


def test_wide_group_always_carries_the_required_indexes(mcfg) -> None:
    """宽基组**必有的两只**（上证50 / 中证1000）：配置里没写也要带上，写了自己的名字也不覆盖。

    用户 2026-09-17 说"要加上证50、中证2000、科创板"：
    - 上证50 `000016.SH`：2026-09-17 实测可用（腾讯 `qt.gtimg.cn/q=sh000016` 回
      `1~上证50~000016~2844.23~…`），加上；
    - 中证2000：**拿不到**（同一天实测 `sh932000` / `sz932000` 都只回
      `v_pv_none_match="1"`），所以按任务书换成中证1000 `000852.SH`（实测可用）；
    - "科创板" = 列表里本来就有的 **科创50 `000688.SH`**（保留）。
    """
    assert market.WIDE_INDEX_EXTRA_CODES == ("000016.SH", "000852.SH")
    assert market.WIDE_INDEX_EXTRA_CODES[0] == "000016.SH"      # 上证50（用户点名要的）
    assert "000688.SH" in [c for c, _ in market.parse_codes(
        ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"])] \
        or market.INDEX_NAMES["000688.SH"] == "科创50"

    # 配置里只有五只 → 取到的是七只（两只补在**后面**，不打断用户自己的顺序）
    mcfg.market_indices = ["000001.SH", "399001.SZ", "399006.SZ",
                           "000688.SH", "000300.SH"]
    specs = market.wide_specs(mcfg)
    assert [code for code, _ in specs] == [
        "000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH",
        "000016.SH", "000852.SH",
    ]
    assert [name for _, name in specs][-2:] == ["上证50", "中证1000"]
    assert market.INDEX_NAMES["000016.SH"] == "上证50"
    assert market.INDEX_NAMES["000852.SH"] == "中证1000"

    # 用户自己写了名字/顺序 → 原样保留，只把缺的补在末尾
    mcfg.market_indices = ["000300.SH=我的沪深300", "000016.SH=我的上证50"]
    specs = market.wide_specs(mcfg)
    assert specs == [("000300.SH", "我的沪深300"), ("000016.SH", "我的上证50"),
                     ("000852.SH", "中证1000")]
    # 空列表仍然是"我不要这一块"（不补）—— 语义不变
    mcfg.market_indices = []
    assert market.wide_specs(mcfg) == []
    assert market._configured_group_keys(mcfg) == ["sentiment", "sector"]


def test_rejected_index_candidate_is_documented_not_silently_dropped() -> None:
    """**用户要的东西不许静默消失**：中证2000 拿不到这件事必须留在代码里、且可被断言。

    取不到就换一个（中证1000）是可以的，但"试过什么、为什么没有"必须写下来 ——
    否则下一个人（或用户自己）只会看到"我要的中证2000 呢？"，然后无从查起。
    这条同时钉住两件事：① 中证2000 的代码**不在**宽基列表里；
    ② 它的"为什么不在"写在 `REJECTED_INDEX_CANDIDATES` 里、并带上实测证据。
    """
    assert "932000" not in "".join(market.WIDE_INDEX_EXTRA_CODES)
    assert not any("932000" in code for code, _ in market.parse_codes(
        ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]))

    rejected = market.REJECTED_INDEX_CANDIDATES
    assert rejected, "被实测否掉的候选必须留档，不能悄悄消失"
    joined = "；".join(f"{k}={v}" for k, v in rejected.items())
    assert "中证2000" in joined                     # 用户原话里点名的那个
    assert "932000" in joined                       # 那个代码
    # 实测证据要写下来（这正是"试过什么"）：腾讯对这两个代码的返回
    assert "v_pv_none_match" in joined
    assert "sh932000" in joined and "sz932000" in joined
    # 换成了哪一个、为什么（中证1000 可用）
    assert "000852.SH" in joined and "中证1000" in joined


def test_footer_drops_the_beijing_amount_and_keeps_the_breadth_note() -> None:
    """页脚那行小字：**不再提北交所成交额**（它已经不显示了），涨跌家数照旧注明慢一档。"""
    overview = market._skeleton(configured=["indices"])
    overview.update({
        "turnover": {"sh": 1e12, "sz": 1e12, "bj": 1.4e10, "total": 2e12},
        "breadth_enabled": True, "as_of": "2026-09-17 14:30",
    })
    text = market.footer_text(overview)
    assert "北交所" not in text
    assert "涨跌家数每 5 分钟更新" in text
    assert market.footer_text({}) == market.FOOTER_PREFIX + " · 更新于 —（每分钟自动刷新）"


def test_footer_names_the_real_sources_and_the_disclaimer() -> None:
    """页脚那行小字要**如实**写清"数据哪来的"，并点明非交易所授权行情。

    为什么单列一条：其它用例都是拿 `market.FOOTER_PREFIX` 自己跟自己比（`… == 前缀 + …`），
    常量一改就跟着变，**钉不住内容**。而页脚是唯一告诉用户"这些数字哪来的"的地方 ——
    2026-09-17 主源从同花顺换成**免 Key 公开行情源**之后，这里再只写"同花顺"就是在说假话
    （概览取数实际走的是腾讯/新浪/东财那一路）。所以把内容钉在这里：
    公开行情接口、两个具体来源、同花顺（备用/增强）、以及"非交易所授权行情"这句免责。
    """
    prefix = market.FOOTER_PREFIX
    assert "公开行情接口" in prefix                      # 真实主源要写出来
    assert "腾讯" in prefix and "新浪" in prefix         # 具体到哪个接口，不含糊
    assert "同花顺" in prefix                            # 备用/增强源也要写明（有 Key 时用它）
    assert "非交易所授权行情" in prefix                  # 这句是用户判断"数据能不能当真"的依据
    assert "数据来源" in prefix
    # "多久更新一次"与来源同属页脚必须说清的两件事，一起钉住
    text = market.footer_text({"as_of": "2026-09-17 14:30"})
    assert text.startswith(prefix)
    assert "更新于 2026-09-17 14:30" in text and "每分钟自动刷新" in text


def test_empty_group_takes_no_line(mcfg) -> None:
    """某组配置为空 → **整行不显示**（不留一个空的"板块："）。"""
    mcfg.market_sector_indices = []
    overview = market.fetch_overview(mcfg, client=_client(), force=True)

    assert overview["configured_groups"] == ["indices", "sentiment"]
    lines = market.lines(overview)
    assert len(lines) == market.LINE_COUNT == 5
    assert lines[4] == ""                              # 板块那行是空串 → 界面隐藏
    assert not any("板块" in line for line in lines)
    assert "银行" not in "".join(lines)

    # 情绪组也清空 → 两行都不显示，宽基照常
    mcfg.market_sentiment_indices = []
    overview = market.fetch_overview(mcfg, client=_client(), force=True)
    lines = market.lines(overview)
    assert lines[3] == "" and lines[4] == ""
    assert lines[2].startswith("宽基：上证 ")


def test_config_can_rename_and_hint_missing_code(mcfg) -> None:
    """配置里能写 `代码=名称`；快照里没有的代码跳过并记进 failed。"""
    mcfg.market_indices = ["000001.SH=我的上证", "899050.BJ"]
    client = _client()
    overview = market.fetch_overview(mcfg, client=client, force=True)

    # 用户自己写的 `000001.SH=我的上证` **原样保留**（名字不被覆盖），
    # 而"必须有的两只"补在后面（`market.WIDE_INDEX_EXTRA_CODES`）
    assert [item["name"] for item in overview["indices"]] == [
        "我的上证", "上证50", "中证1000",
    ]
    assert overview["failed"] == ["899050.BJ"]
    assert "899050.BJ" in "".join(overview["errors"])
    lines = market.lines(overview)
    assert lines[2].startswith("宽基：我的上证 3885.33 -0.07%")   # 配置里的名字优先
    # 这一组配置里只有"我的上证"（+ 必有的两只），所以这一行就是这三项
    assert lines[2] == ("宽基：我的上证 3885.33 -0.07% ｜ 上证50 2844.23 +0.42% ｜ "
                        "中证1000 7548.82 -1.21%")
    assert "899050" not in lines[2]                      # 取不到的**跳过**，不占位置


def test_bad_code_only_drops_itself(mcfg) -> None:
    """**重点用例**：配置里混一个取不到的代码（这里用 `932000.TI`）——
    批量必然 1002（真实接口的行为），逐只重试后其余全部照常显示，界面上只少那一项。

    这条走**真实 `HithinkClient` + 假 Session**：测的是"契约 + 降级"整条链，
    而不是只测假客户端的编排。
    """

    def route(params: dict):
        codes = str(params.get("thscodes") or "")
        if "," in codes or codes == "932000.TI":
            # 真实接口就是这样：一个非法代码让**整批**返回 1002 Unknown thscode
            return FakeResponse({"code": 1002, "message": "Unknown thscode"})
        row = next((r for r in SAMPLE_ROWS if r["thscode"] == codes), None)
        return FakeResponse({"code": 0, "data": {"item": ([row] if row else [])}})

    session = FakeSession({"/a-share-index/prices/snapshot": route})
    client = hx.HithinkClient(api_key="k", session=session, pace=0, retries=0)
    mcfg.market_sentiment_indices = [
        "883404.TI", "883958.TI", "883994.TI", "932000.TI", "883418.TI",
    ]
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert overview["failed"] == ["932000.TI"]        # 只记这一个
    assert [item["name"] for item in overview["sentiment"]] == [
        "同花顺情绪", "昨日连板", "昨日打首板", "微盘股",
    ]
    assert [item["name"] for item in overview["indices"]] == [
        "上证", "深成", "创业板", "科创50", "沪深300", "上证50", "中证1000",
    ]
    lines = market.lines(overview)
    assert "932000" not in "".join(lines)             # 那一项不出现
    assert lines[2] == SAMPLE_LINES[2]                # 其它组一字不差
    assert lines[3] == SAMPLE_LINES[3]
    assert lines[4] == SAMPLE_LINES[4]                # 同组里其余的也都在
    assert any("932000.TI" in e for e in overview["errors"])   # 原因查得到


def test_turnover_is_rounded_to_yi(mcfg) -> None:
    """成交额按亿四舍五入（7792.8 + 140.1 = 7932.9 → 7933），是**一个数**。

    2026-09-17 起界面与命令行都只显示"沪 + 深"这一个数（用户要求），
    所以这里同时钉住"两段相加"和"只四舍五入一次"：
    先各自取整再相加（7793 + 140 = 7933）与先相加再取整（7932.9 → 7933）在这里刚好一样，
    但换一组数就会差 1 亿 —— 口径是**先相加、再四舍五入**（`turnover["total"]` 是原始元）。
    """
    rows = [
        {"thscode": "000001.SH", "last_price": 1.0, "price_change_ratio_pct": 0.0,
         "turnover": 7792.8e8},
        {"thscode": "399001.SZ", "last_price": 1.0, "price_change_ratio_pct": 0.0,
         "turnover": 140.1e8},
    ]
    overview = market.fetch_overview(mcfg, client=_client(rows=rows), force=True)
    assert overview["turnover"]["total"] == pytest.approx(7932.9e8)
    assert market.lines(overview)[0].endswith("成交额 7933亿")
    # 先各自取整再相加会是 7933 亿（7793 + 140）—— 两者相等，说明这条不是靠巧合钉住的
    assert market.kpi_values(overview)["成交额"] == "7933亿"


def test_value_color_follows_a_share_convention() -> None:
    """涨=红、跌=绿、平盘或缺数据=默认色 —— A 股习惯，不是欧美的绿涨红跌。

    逐**值**取色（不再是"整组同向才上色"）：情绪与板块那几组经常同时有涨有跌，
    按组取色只能"要么全染红、要么全不染"，两种都在骗人。
    """
    assert market.value_color(0.5) == market.COLOR_UP == "#d32f2f"
    assert market.value_color(3.21) == market.COLOR_UP
    assert market.value_color(-0.18) == market.COLOR_DOWN == "#2e7d32"
    assert market.value_color(-1.2) == market.COLOR_DOWN
    # 平盘（0.00%）既不算涨也不算跌；缺数据同样不上色
    assert market.value_color(0.0) == market.COLOR_FLAT == ""
    assert market.value_color(None) == ""
    assert market.value_color("") == ""
    assert market.value_color("不是数字") == ""


def test_kpi_values_and_entry_fields_are_the_single_source() -> None:
    """界面用的"一个指标一个值""一个指数三个字段"与命令行那几行**同源**。

    为什么钉这一条：界面改成"卡片 + 条目网格"之后，`lines()` 与界面各算各的话，
    同一个数在两处显示成不同样子（例如界面 `+0.07%`、命令行 `0.07%`）就没人发现。
    """
    overview = {
        "limits": {"up": 55, "down": 16, "break": 30},
        "turnover": {"sh": 7792.4e8, "sz": 8498.9e8, "bj": 140.1e8,
                     "total": 7792.4e8 + 8498.9e8},
        "breadth": {"up": 3126, "down": 2224, "flat": 221},
        "configured_groups": ["indices", "sentiment", "sector"],
        "indices": [{"thscode": "000001.SH", "name": "上证", "last": 3885.33,
                     "change_pct": -0.07}],
        "sentiment": [], "sector": [],
    }
    values = market.kpi_values(overview)
    # 键的顺序 = 界面上的显示顺序（宽屏一行 7 个）：**成交额在最前**（沪 + 深 一个数），
    # 然后涨停/跌停/炸板，最后涨跌家数。
    # 2026-09-17：`沪成交额` / `深成交额` / `北成交额` 三个键删掉了（用户要求合成一个数、
    # 北交所不再显示），所以键数从 9 变成 7。
    assert list(values) == ["成交额", "涨停", "跌停", "炸板", "上涨", "下跌", "平盘"]
    assert values["涨停"] == "55" and values["平盘"] == "221"
    assert values["成交额"] == "16291亿"          # 7792.4 + 8498.9 = 16291.3 → 16291
    assert "140亿" not in "".join(values.values())     # 北交所那 140 亿不再出现在任何一格

    name, value, pct = market.entry_fields(overview["indices"][0])
    assert (name, value, pct) == ("上证", "3885.33", "-0.07%")
    # 没点位就连涨跌幅一起显示 `—`（只报一半数据会让人以为接口坏了）
    assert market.entry_fields({"thscode": "X", "last": None,
                                "change_pct": 1.0}) == ("X", market.DASH, market.DASH)

    # 与 `lines()` 的口径一致：命令行那行就是这些值拼起来的
    lines = market.lines(overview)
    assert lines[0] == f"涨停 {values['涨停']} · 跌停 {values['跌停']} · " \
                       f"炸板 {values['炸板']} ｜ 成交额 {values['成交额']}"
    assert lines[1] == f"上涨 {values['上涨']} · 下跌 {values['下跌']} · 平盘 {values['平盘']}"
    assert lines[2] == "宽基：上证 3885.33 -0.07%"
    # 配了但没有数据的组 → `—`（不是空标题、也不是 0）
    assert lines[3] == "情绪：—"
    # 拿不到任何东西时也不抛：全部是 `—`
    assert market.kpi_values(None)["成交额"] == market.DASH
    assert market.entry_fields({"thscode": "X"}) == ("X", market.DASH, market.DASH)


# ── 3) TTL 缓存 ──


def test_second_call_within_ttl_uses_cache_and_marks_stale(mcfg) -> None:
    client = _client()
    first = market.fetch_overview(mcfg, client=client, force=True)
    calls_after_first = len(client.calls)

    second = market.fetch_overview(mcfg, client=client)
    assert len(client.calls) == calls_after_first   # 55 秒内**一次接口都没打**
    assert second["stale"] is True
    assert second["as_of"] == first["as_of"]        # 取数时间沿用第一次的
    assert second["limits"] == first["limits"]
    # 返回的是副本：改它不会污染缓存
    second["limits"]["up"] = 0
    assert market.fetch_overview(mcfg, client=client)["limits"]["up"] == 55


def test_expired_ttl_refetches(mcfg, monkeypatch) -> None:
    """TTL 到点就真的重打接口（用"把缓存时间戳往前拨"模拟 55 秒过去）。"""
    client = _client()
    market.fetch_overview(mcfg, client=client, force=True)
    calls = len(client.calls)
    monkeypatch.setattr(market, "_CACHE",
                        {**market._CACHE, "at": market._CACHE["at"] - 60})
    fresh = market.fetch_overview(mcfg, client=client)
    assert len(client.calls) == calls * 2
    assert fresh["stale"] is False


def test_force_always_refetches(mcfg) -> None:
    client = _client()
    market.fetch_overview(mcfg, client=client, force=True)
    calls = len(client.calls)
    forced = market.fetch_overview(mcfg, client=client, force=True)
    assert len(client.calls) == calls * 2
    assert forced["stale"] is False


def test_changing_indices_invalidates_cache(mcfg) -> None:
    """换了指数列表就该重新取：否则"改了配置没反应"会被当成 bug 报上来。"""
    client = _client()
    market.fetch_overview(mcfg, client=client, force=True)
    calls = len(client.calls)
    mcfg.market_indices = ["000300.SH"]
    overview = market.fetch_overview(mcfg, client=client)
    assert len(client.calls) == calls * 2
    # 换成只写沪深300 也一样：**必有的两只照样在**（`WIDE_INDEX_EXTRA_CODES`），
    # 而用户在配置里写的那一只排在最前面（顺序 = 配置顺序）
    assert [item["name"] for item in overview["indices"]] == [
        "沪深300", "上证50", "中证1000",
    ]


# ── 4) 降级：任何一路失败都不许拖垮整体 ──


def test_limits_failure_does_not_break_the_rest(mcfg) -> None:
    """跌停那一路抛异常 → 它显示 `—`，涨停/炸板/指数/情绪照常，原因进 errors。"""
    client = _client(totals_error=(market.LIMIT_DOWN_PATH,))
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert overview["limits"] == {"up": 55, "down": None, "break": 30}
    assert overview["errors"] and "跌停家数取不到" in overview["errors"][0]
    assert len(overview["indices"]) == 7           # 指数一路完全没受影响（5 只默认 + 2 只必有）
    assert len(overview["sentiment"]) == 4
    lines = market.lines(overview)
    assert lines[0].startswith("涨停 55 · 跌停 — · 炸板 30")
    assert lines[2] == SAMPLE_LINES[2]
    assert lines[3] == SAMPLE_LINES[3]


def test_indices_failure_does_not_break_limits(mcfg) -> None:
    """指数整批失败 → 指数/情绪两行是 `—`，涨跌停与成交额那行照常。"""
    client = _client(index_error=hx.HithinkError(5001, "测试假客户端：限流"))
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert overview["indices"] == [] and overview["sentiment"] == []
    assert overview["sector"] == []
    assert overview["turnover"]["sh"] is None
    assert any("指数/情绪/板块取不到" in e for e in overview["errors"])
    lines = market.lines(overview)
    assert lines[0].startswith("涨停 55 · 跌停 16 · 炸板 30")
    # 三组都配了但一个都没取到 → 组名 + —（不是整行不显示）
    assert lines[2:] == ["宽基：—", "情绪：—", "板块：—"]


def test_no_api_key_returns_skeleton_instead_of_raising(cfg) -> None:
    """没配 Key（真实客户端构造就失败）→ 返回骨架 + 中文原因，**绝不抛异常**。"""
    cfg.hithink_api_key = ""
    overview = market.fetch_overview(cfg, force=True)      # client=None → 走真实构造
    assert overview["limits"] == {"up": None, "down": None, "break": None}
    assert any("同花顺 Key" in e for e in overview["errors"])
    assert market.has_data(overview) is False


# ── 5) 全市场汇总（market_breadth）──


def _breadth_pages() -> list[dict]:
    """3 页小样本（配合 monkeypatch 把页大小改成 3）：沪/深/北各混一点。"""
    rows = [
        {"thscode": "600519.SH", "turnover": 1e11, "volume": 1e6,
         "price_change_ratio_pct": 1.0},
        {"thscode": "000001.SZ", "turnover": 4e11, "volume": 2e6,
         "price_change_ratio_pct": -0.5},
        {"thscode": "830799.BJ", "turnover": 1.4e10, "volume": 3e5,
         "price_change_ratio_pct": 2.0},
        {"thscode": "600520.SH", "turnover": 2e11, "volume": 1e6,
         "price_change_ratio_pct": -1.0},
        {"thscode": "000002.SZ", "turnover": 5e11, "volume": 2e6,
         "price_change_ratio_pct": 0.0},
        {"thscode": "831010.BJ", "turnover": 7.1e8, "volume": 1e5,
         "price_change_ratio_pct": -3.0},
        {"thscode": "600521.SH", "turnover": 3e11, "volume": 1e6,
         "price_change_ratio_pct": 0.5},
    ]
    return [
        {"item": rows[0:3], "total": 7},
        {"item": rows[3:6], "total": 7},
        {"item": rows[6:7], "total": 7},
    ]


def test_breadth_off_sends_no_full_market_request(mcfg) -> None:
    """默认关：**一个全市场分页请求都不发**，涨跌家数显示 `—`。"""
    mcfg.market_breadth = False
    client = _client()
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert client.count("request") == 0
    assert overview["breadth"] is None
    assert overview["turnover"]["bj"] is None
    # 关着 → 涨跌家数显示 —，且一个全市场分页请求都不发；
    # 成交额照常有数（它来自两只宽基指数，与全市场快照无关）
    assert market.lines(overview)[0].endswith("成交额 16291亿")
    assert market.lines(overview)[1] == "上涨 — · 下跌 — · 平盘 —"


def test_breadth_on_aggregates_pages_and_paces_them(mcfg, monkeypatch) -> None:
    """打开后：分页汇总出**涨跌家数**（与沪/深/北三市的分桶），页间**间隔 ≥0.3 秒**。

    北交所那一格仍然被算出来（`turnover["bj"]`、`exchanges["BJ"]`），
    但 2026-09-17 起**不再显示**，也**不进** `turnover["total"]`（用户要求删掉那一格）。
    """
    mcfg.market_breadth = True
    monkeypatch.setattr(market, "BREADTH_PAGE_SIZE", 3)
    sleeps: list[float] = []
    monkeypatch.setattr(market.time, "sleep", lambda seconds: sleeps.append(seconds))
    client = _client(pages=_breadth_pages(), page_size=3)

    overview = market.fetch_overview(mcfg, client=client, force=True)
    breadth = overview["breadth"]

    assert client.count("request") == 3               # 7 只 / 每页 3 → 3 页
    assert sleeps and all(gap >= 0.3 for gap in sleeps)
    assert client.offsets() == [0, 3, 6]              # 逐页往后翻，没重复取第一页
    assert breadth["up"] == 3 and breadth["down"] == 3 and breadth["flat"] == 1
    assert breadth["total"] == 7
    assert breadth["exchanges"]["SH"]["count"] == 3
    assert breadth["exchanges"]["BJ"]["turnover"] == pytest.approx(1.4e10 + 7.1e8)
    assert overview["turnover"]["bj"] == pytest.approx(1.4e10 + 7.1e8)
    # 合计**只算沪深**（北交所那 147 亿不进这个数）
    assert overview["turnover"]["total"] == pytest.approx(7792.4e8 + 8498.9e8)
    assert market.lines(overview)[0].endswith("成交额 16291亿")
    assert "147亿" not in market.lines(overview)[0]
    assert market.lines(overview)[1] == "上涨 3 · 下跌 3 · 平盘 1"


def test_breadth_has_its_own_longer_ttl(mcfg, monkeypatch) -> None:
    """概览缓存过期后，全市场汇总仍走它自己的 5 分钟缓存（不该再翻 6 页）。"""
    mcfg.market_breadth = True
    monkeypatch.setattr(market, "BREADTH_PAGE_SIZE", 3)
    client = _client(pages=_breadth_pages(), page_size=3)
    market.fetch_overview(mcfg, client=client, force=True)
    pages = client.count("request")

    # 把**概览**缓存的时刻往前拨（模拟 55 秒过去），汇总缓存保持新鲜
    monkeypatch.setattr(market, "_CACHE",
                        {**market._CACHE, "at": market._CACHE["at"] - 60})
    overview = market.fetch_overview(mcfg, client=client)
    assert client.count("request") == pages           # 一页都没重翻
    assert overview["breadth"]["total"] == 7          # 但仍来自那份缓存
    assert client.count("index") == 2                 # 指数那一路确实重取了


def test_breadth_failure_degrades_to_dash(mcfg) -> None:
    """全市场汇总失败：涨跌家数显示 `—`，其余照样有数。"""
    mcfg.market_breadth = True
    client = _client(
        pages_error=hx.HithinkError(5001, "测试假客户端：限流")
    )
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert overview["breadth"] is None
    assert overview["turnover"]["bj"] is None
    assert any("全市场汇总取不到" in e for e in overview["errors"])
    # 开着但没取到 → 显示 —（用户才知道是"没取到"而不是"没这个功能"）
    assert market.lines(overview)[0].endswith("成交额 16291亿")
    assert market.lines(overview)[1] == "上涨 — · 下跌 — · 平盘 —"
    assert len(overview["indices"]) == 7               # 5 只默认 + 上证50 + 中证1000


# ── 6) 总开关 ──


def test_master_switch_off_makes_no_request_at_all(mcfg) -> None:
    """`market_overview = false`：一个请求都不发，并说明"为什么是 —"。"""
    mcfg.market_overview = False
    client = _client()
    overview = market.fetch_overview(mcfg, client=client, force=True)

    assert client.calls == []
    assert market.has_data(overview) is False
    assert any("market_overview" in e for e in overview["errors"])
    # 组别是**读配置**得来的（不发请求），所以照样显示组名 + —：用户知道自己配的东西在哪
    assert market.lines(overview) == [
        "涨停 — · 跌停 — · 炸板 — ｜ 成交额 —",
        "上涨 — · 下跌 — · 平盘 —",
        "宽基：—", "情绪：—", "板块：—",
    ]


# ── 7) 命令行 --market ──


def _cli_config(tmp_path, cfg):
    """一份"没有 Key"的 config.toml（路径按 Windows 转义规则写）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n',
        encoding="utf-8",
    )
    return path


def test_cli_market_prints_the_card_lines_and_exits_zero(capsys, cfg, tmp_path,
                                                         monkeypatch) -> None:
    """`--market`：打印卡片那几行后退出 0（与界面共用 market.lines 的同一份文本）。"""
    from laoa_trader.__main__ import cli

    overview = market._skeleton()
    overview.update({"as_of": "2026-09-14 15:03", "configured_groups": ["indices"]})
    overview["limits"] = {"up": 55, "down": 16, "break": 30}
    overview["turnover"] = {"sh": 7792.4e8, "sz": 8498.9e8, "bj": None,
                            "total": 7792.4e8 + 8498.9e8}
    overview["indices"] = [{"thscode": "000001.SH", "name": "上证", "last": 3885.33,
                            "change_pct": -0.07, "turnover": 7792.4e8}]
    monkeypatch.setattr(market, "fetch_overview",
                        lambda *a, **k: overview)          # 离线：不碰网络

    code = cli(["--cli", "--market", "--config", str(_cli_config(tmp_path, cfg))])
    out = capsys.readouterr().out
    assert code == 0
    assert "涨停 55 · 跌停 16 · 炸板 30" in out
    assert "宽基：上证 3885.33 -0.07%" in out
    assert "上涨 — · 下跌 — · 平盘 —" in out        # 第二行是涨跌家数
    assert "2026-09-14 15:03" in out
    # 没配的组（情绪/板块）不打空行
    assert "情绪：" not in out and "板块：" not in out
    assert "\n\n" not in out


def test_cli_market_exits_nonzero_with_reason_when_nothing_available(
    capsys, cfg, tmp_path
) -> None:
    """拿不到数据：非 0 退出，并说清原因（这里是"没配 Key"）。"""
    from laoa_trader.__main__ import cli

    code = cli(["--cli", "--market", "--config", str(_cli_config(tmp_path, cfg))])
    out = capsys.readouterr().out
    assert code == 1
    assert "涨停 — · 跌停 — · 炸板 —" in out
    assert "同花顺 Key" in out                          # 原因看得见，不是干巴巴一个 1
    assert "没取到任何数据" in out


def test_main_routes_market_to_cli(monkeypatch, tmp_path, cfg) -> None:
    """`--market` 必须被当成命令行参数（否则会去开图形界面）。"""
    from laoa_trader import __main__ as main_mod

    seen: list[list[str]] = []
    monkeypatch.setattr(main_mod, "cli", lambda argv: seen.append(list(argv)) or 0)
    monkeypatch.setattr(main_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **k: None)
    assert main_mod.main(["--market"]) == 0
    assert seen == [["--market"]]
