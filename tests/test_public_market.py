"""免 Key 的"同花顺客户端替身"+ 全市场扫描（`data/public_market.py`）：**全离线**。

为什么这些用例必须存在
----------------------
这个模块是**没配同花顺 Key 时的兜底**（概览与全市场扫描都靠它；2026-09-18 用户把主源
换回同花顺 —— 公开接口实测会限流：腾讯 fqkline 抓 700 只左右开始连续失败、新浪列表接口
回 HTTP 456 —— 所以它的定位是"兜底"，不再是主源；有 Key 时概览走同花顺）。
它有两个"错了也不会报错、只会显示错数字"的地方，所以每一条都照着**真实响应**钉：

1. **口径**：涨停家数、涨跌家数、成交额。口径错了界面照样画得出来。
   本模块对齐的是同花顺的涨停池口径（**沪深、不含 ST**，理由与实测见模块 docstring），
   而涨跌家数**含** ST 与北交所 —— 两个口径不一样，必须有用例钉住，否则下一个人
   "顺手统一一下"就把其中一个改错了。
2. **"没取到"不等于 0**：断网/被限流时若返回 0，界面会显示"今天一只涨停都没有"。
   `test_empty_scan_raises_instead_of_reporting_zero_limits` 钉的就是它。

夹具全是真的：`scan_rows.json` 是 2026-09-17 那一趟真实全市场扫描里挑出来的
**19 行**（每一行都为某个坑存在：ST 涨停、北交所、停牌、新股无涨停价、腾讯的 -1 哨兵）。
`tests/conftest.py` 的 `_block_network` 在 socket 层封死了外呼，所以这些用例
不会、也不能碰真实接口。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from laoa_trader import market
from laoa_trader.data import public_market as pm
from laoa_trader.data import public_quotes as pq

FIXTURES = Path(__file__).parent / "fixtures" / "public_market"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_text(name: str, encoding: str = "gbk") -> str:
    return _fixture_bytes(name).decode(encoding, errors="replace")


def _scan_rows() -> list[dict]:
    """真实扫描里挑出的 19 行（每个坑一行）。"""
    return json.loads((FIXTURES / "scan_rows.json").read_text(encoding="utf-8"))


def _by_symbol(symbol: str) -> dict:
    for row in _scan_rows():
        if row["symbol"] == symbol:
            return row
    raise AssertionError(f"夹具里没有 {symbol}（夹具被改过？）")


def _opener(responses: dict[str, bytes]):
    """假 HTTP 层：按 URL 关键字返回预设响应；没匹配上就抛（逼测试写清期望）。"""
    calls: list[str] = []

    def _get(url: str, headers: dict, timeout: float) -> bytes:
        calls.append(url)
        for key, payload in responses.items():
            if key in url:
                return payload
        raise AssertionError(f"测试没准备这个 URL 的响应：{url}")

    _get.calls = calls          # type: ignore[attr-defined]
    return _get


@pytest.fixture(autouse=True)
def _clean_caches():
    """两个模块级缓存在用例之间必须清干净（否则"扫没扫过"会被上一条用例决定）。"""
    pm.clear_cache()
    market.clear_cache()
    yield
    pm.clear_cache()
    market.clear_cache()


# ── 市场前缀：920 号段是北交所，不是沪市 ─────────────────────────────────


def test_prefix_920_belongs_to_beijing_not_shanghai() -> None:
    """`920xxx` 是**北交所**新股号段；当沪市处理的代价是"344 只票静默消失"。

    实测（2026-09-17）：腾讯 `sh920000` 返回空、`bj920000` 才有数据；
    而 `900901`（沪市 B 股）确实是 `sh`。所以 9 开头必须按 `92` 再分一次。
    这条错误没有任何报错信号：取不到就是"这只票不在结果里"，表格上少 344 行而已。
    """
    assert pq.market_prefix("920000") == "bj"
    assert pq.market_prefix("920002") == "bj"
    assert pq.market_prefix("900901") == "sh"      # 沪市 B 股仍然是 sh
    assert pq.market_prefix("830799") == "bj"
    assert pq.market_prefix("430047") == "bj"
    assert pq.market_prefix("600519") == "sh"
    assert pq.market_prefix("000001") == "sz"      # 平安银行（指数走 `batch_quotes`，见下）


def test_scan_row_exchange_of_northbound_codes() -> None:
    """分桶也要按同一份规则：`920000` 归 `BJ`（概览的成交额/家数分桶读它）。"""
    assert pm._exchange_of("920000") == "BJ"
    assert pm._exchange_of("430047") == "BJ"
    assert pm._exchange_of("830799") == "BJ"
    assert pm._exchange_of("600519") == "SH"
    assert pm._exchange_of("300750") == "SZ"


# ── 代码表 ──────────────────────────────────────────────────────────────


def test_list_symbols_strips_prefixes_and_reads_pages() -> None:
    """新浪代码表：一页 100 只 → 拉下一页；返回的是**裸 6 位**代码（本项目内部口径）。"""
    page1 = _fixture_bytes("sina_list_page1.json")
    short_page = json.dumps(
        [{"symbol": "sh600519", "name": "贵州茅台"}, {"symbol": "bj920000", "name": "安徽凤凰"}]
    ).encode()
    opener = _opener({"page=1": page1, "page=2": short_page})

    codes = pm.list_symbols(opener=opener)

    # 期望值**从夹具算**：手写"100 + 2 = 102"当场错过一次（第二页那两只里有一只
    # 第一页本来就有，去重之后是 101）—— 这种"我以为的页数"最不该手写
    page1_codes = {row["symbol"][2:] for row in json.loads(page1.decode())}
    expected = page1_codes | {"600519", "920000"}
    assert set(codes) == expected and len(codes) == len(expected)
    assert codes[0] == "920000"                               # 新浪按 symbol 升序，bj 在前
    assert all(len(code) == 6 and code.isdigit() for code in codes)
    assert all(not code.startswith(("sh", "sz", "bj")) for code in codes)
    # 第一页满 100 → 必须去取第二页；第二页短 → 停（不多发一次无用请求）
    assert sum("page=1" in url for url in opener.calls) == 1
    assert sum("page=2" in url for url in opener.calls) == 1
    assert not any("page=3" in url for url in opener.calls)


def test_list_symbols_returns_empty_when_the_list_endpoint_fails() -> None:
    """代码表取不到 → 空列表（宁可"什么都没有"，也不许编一串代码出来）。"""

    def _boom(url: str, headers: dict, timeout: float) -> bytes:
        raise OSError("network down")

    assert pm.list_symbols(opener=_boom) == []


def test_scan_returns_empty_without_a_symbol_list() -> None:
    """**明确**不允许"不给代码表还去偷偷猜一个全市场"：代码表只有一个来源，不许暗箱。"""
    def _boom(url: str, headers: dict, timeout: float) -> bytes:
        raise AssertionError("给了空代码表就不该发任何请求")

    assert pm.scan([], opener=_boom) == []


# ── 哨兵值与归一化 ──────────────────────────────────────────────────────


def test_scan_cleans_tencent_sentinel_values() -> None:
    """腾讯对"没有这个概念的票"回 `-1`/`0`：必须当**没有**，不能当价格。

    实测 `bj430047`：涨停价/跌停价 `-1`、最高价 `0.00`，而现价还留着 8.17。
    不清理的话"最高 0 == 涨停价 -1"这类组合会被算进炸板。
    """
    opener = _opener({"qt.gtimg.cn": _fixture_bytes("tencent_sentinel.txt")})
    rows = {row["symbol"]: row for row in pm.scan(["430047", "600519"], opener=opener)}

    dead = rows["430047"]
    assert dead["last_price"] == pytest.approx(8.17)      # 现价留着（那是真数）
    assert dead["limit_up"] is None and dead["limit_down"] is None
    assert dead["high"] is None                            # 0 → None
    assert dead["exchange"] == "BJ"

    alive = rows["600519"]
    assert alive["limit_up"] == pytest.approx(1383.80)
    assert alive["limit_down"] == pytest.approx(1132.20)
    assert alive["high"] == pytest.approx(1267.60)


def test_scan_sorts_by_symbol_for_stable_pagination() -> None:
    """顺序必须稳定（按代码）：概览分页汇总与日更写入都怕"顺序乱跳"。"""
    payload = _fixture_text("tencent_indices.txt").encode("gbk")
    opener = _opener({"qt.gtimg.cn": payload})
    codes = ["600519", "000001", "300750"]
    rows = pm.scan(codes, opener=opener)
    assert [row["symbol"] for row in rows] == sorted(row["symbol"] for row in rows)


# ── 家数：两个口径（涨停类不含 ST/北交所；涨跌家数含） ──────────────────


def test_counts_follow_the_two_different_scopes() -> None:
    """涨停/跌停/炸板 **排除 ST 与北交所**；涨跌家数**含**它们。两个口径不一样。

    夹具里各坑都在：
    - `000504/000868/000920` 普通票压在涨停价上 → 3 只涨停；
    - `002528/002569` 是 **`*ST` 且压在涨停价上** → **不计**（同花顺涨停池实测不含 ST）；
    - `000056 *ST皇庭` 压在跌停价上 → 也不计（ST）；`002163 海南发展` → 1 只跌停；
    - `000572/000700` 最高价摸到涨停价但收在下面 → 2 只炸板；
    - `920000/920001` 北交所 → 不进涨停类统计（但进涨跌家数）；
    - `601091 N沈鼓`/`688801 C燧原-U` 新股次新**没有涨停价** → 不进（它没有"涨停"这回事）。
    """
    stat = pm.counts(_scan_rows())

    assert stat["up"] == 3
    assert stat["down"] == 1
    assert stat["break"] == 2
    # 涨跌家数：19 行里 2 只停牌不计，其余 17 只 → 12 涨 5 跌 0 平
    assert (stat["adv"], stat["dec"], stat["flat"]) == (12, 5, 0)
    assert stat["total"] == 19


def test_counts_never_lets_an_st_limit_up_into_the_number() -> None:
    """单独钉一遍"ST 涨停不计"：这是与同花顺 47 vs 腾讯 50 那 3 只差别的来源。"""
    st_up = [_by_symbol("002528"), _by_symbol("002569")]
    assert all("ST" in row["name"].upper() for row in st_up)
    assert all(pm._at_limit(row["last_price"], row["limit_up"]) for row in st_up)
    assert pm.counts(st_up)["up"] == 0            # 单独喂进去也必须是 0

    plain_up = [_by_symbol("000504"), _by_symbol("000868"), _by_symbol("000920")]
    assert pm.counts(plain_up)["up"] == 3


def test_suspended_stocks_are_not_counted_as_flat() -> None:
    """停牌（成交量为 0）不算涨跌：算成"平盘"会凭空多出十几只。

    实测 2026-09-17：全市场 12 只停牌，把它们算成平盘会显示 169 家平盘，
    而真实平盘是 157 家 —— 这个 12 的差额就是这么被抓出来的。
    """
    suspended = [_by_symbol("000016"), _by_symbol("002731")]
    assert all(not row["volume"] for row in suspended)
    stat = pm.counts(suspended)
    assert (stat["adv"], stat["dec"], stat["flat"]) == (0, 0, 0)


def test_broken_limit_requires_touching_the_limit_and_closing_below() -> None:
    """炸板的等价定义："最高价摸到涨停价"且"现价低于涨停价"。

    涨停价是当日的硬顶（现价不可能超过它），所以"摸到过涨停"就等于"最高 == 涨停价"；
    再排除掉收在涨停价上的（那是涨停）与根本没有涨停价的（新股）。
    """
    touched = _by_symbol("000572")
    assert touched["high"] == touched["limit_up"]
    assert touched["last_price"] < touched["limit_up"]
    assert pm.counts([touched])["break"] == 1

    # 收在涨停价上的（"最高 == 涨停价"也成立）不算炸板
    assert pm.counts([_by_symbol("000504")])["break"] == 0
    # 新股没有涨停价 → 不是炸板（哪怕最高价很高）
    newbie = _by_symbol("601091")
    assert newbie["limit_up"] is None
    assert pm.counts([newbie])["break"] == 0


def test_counts_buckets_turnover_by_exchange() -> None:
    """按交易所分桶相加 —— 这是**自校验**：实测沪 8685.27 亿、深 9543.21 亿，
    与指数口径（Ａ股指数 8685.27 / 深证Ａ指 9543.21）逐字节相同。"""
    stat = pm.counts(_scan_rows())
    assert set(stat["exchanges"]) == {"SH", "SZ", "BJ"}
    assert stat["exchanges"]["BJ"]["count"] == 2          # 夹具里两只北交所
    # 逐行核对分桶数：不手写"SH 应该有 4 只"这种数（我第一次就写错了），
    # 而是从夹具本身算出来比对 —— 夹具一改，这条断言自动跟着走
    rows = _scan_rows()
    for exchange in ("SH", "SZ", "BJ"):
        expected = sum(1 for row in rows if row["exchange"] == exchange)
        assert stat["exchanges"][exchange]["count"] == expected


# ── 客户端替身：三个方法与同花顺同形状 ─────────────────────────────────


def test_client_index_snapshot_never_guesses_the_exchange() -> None:
    """`000001.SH`（上证指数）与 `000001`（平安银行）**同码不同物**：不许靠首字符猜。

    这一条错了的后果特别刺眼：大盘概览上"A股指数"那一格会显示平安银行的股价。
    所以这里喂**两份**响应（`sh000001` 与 `sz000001`），断言取的是**指数**那一份。
    """
    index_payload = (
        'v_sh000001="1~上证指数~000001~3875.60~3891.60~3877.00~452886464~0~0~'
        + "~".join(["0"] * 22)
        + '~20260917161402~0~-0.41~3898.84~3866.89~3875.60/452886464/868773066850~'
        + "~".join(["0"] * 12)
        + '";'
    ).encode("gbk")
    stock_payload = (
        'v_sz000001="1~平安银行~000001~11.61~11.70~11.72~958000~0~0~'
        + "~".join(["0"] * 22)
        + '~20260917161402~0~-0.77~11.74~11.60~11.61/958000/1113350000~'
        + "~".join(["0"] * 12)
        + '";'
    ).encode("gbk")
    # 批量接口是**一次请求回多行**，所以这里合成一份响应；随后再断言
    # 请求串里两个带前缀的代码**都**在（前缀必须来自 thscode 的交易所后缀）
    opener = _opener({"qt.gtimg.cn": index_payload + stock_payload})

    result = pm.PublicMarketClient(opener=opener).index_snapshot(
        ["000001.SH", "000001.SZ"])

    assert "sh000001" in opener.calls[0]
    assert "sz000001" in opener.calls[0]      # 若靠首字符猜，这里只会有一个
    got = {item["thscode"]: item["last_price"] for item in result["item"]}
    assert got["000001.SH"] == pytest.approx(3875.60)     # 指数
    assert got["000001.SZ"] == pytest.approx(11.61)       # 平安银行
    assert result["failed"] == []


def test_client_index_snapshot_reads_real_tencent_indices() -> None:
    """用真实响应过一遍：宽基那 7 只全都能出点位与涨跌幅（用户要的就是这几只）。"""
    opener = _opener({"qt.gtimg.cn": _fixture_bytes("tencent_indices.txt")})
    result = pm.PublicMarketClient(opener=opener).index_snapshot([
        "000001.SH", "399001.SZ", "000300.SH", "000016.SH",
        "000688.SH", "000852.SH", "399006.SZ",
    ])
    by_code = {item["thscode"]: item for item in result["item"]}

    assert by_code["000001.SH"]["last_price"] == pytest.approx(3875.60)
    assert by_code["000001.SH"]["price_change_ratio_pct"] == pytest.approx(-0.41)
    assert by_code["000001.SH"]["turnover"] == pytest.approx(868773066850.0)
    assert by_code["000852.SH"]["last_price"] == pytest.approx(7548.82)
    assert len(result["item"]) == 7
    assert result["failed"] == []


def test_client_index_snapshot_reports_ti_codes_as_unsupported_not_failed() -> None:
    """同花顺板块指数（`.TI`）在公开源里**没有对应标的**：归 `unsupported`，不是 `failed`。

    为什么要分两个字段：`failed` 在界面上是"你配的这个代码有问题"（用户该去改配置），
    而 `.TI` 是"这一路根本没有这种标的"（改了也没用）。混在一起会让人去改一个没错的代码。
    """
    opener = _opener({"qt.gtimg.cn": _fixture_bytes("tencent_indices.txt")})
    result = pm.PublicMarketClient(opener=opener).index_snapshot(
        ["000001.SH", "883404.TI", "881155.TI"])

    assert [item["thscode"] for item in result["item"]] == ["000001.SH"]
    assert result["failed"] == []
    assert result["unsupported"] == ["883404.TI", "881155.TI"]


def test_client_special_pool_total_maps_the_three_paths() -> None:
    """三个池子路径 → 三个数（`market.py` 传的是完整路径，这里按后缀认，避免两处存路径）。"""
    pm._SCAN.update({"symbols": "all", "at": __import__("time").monotonic(),
                     "rows": _scan_rows()})
    client = pm.PublicMarketClient()
    assert client.special_pool_total("/a-share/special-data/limit-up-pool") == 3
    assert client.special_pool_total("/a-share/special-data/limit-down-pool") == 1
    assert client.special_pool_total("/a-share/special-data/limit-break-pool") == 2


def test_client_special_pool_total_rejects_an_unknown_pool() -> None:
    """不认识的路径**抛**，不猜：猜错的后果是把"连板池"的家数画成"涨停家数"。"""
    pm._SCAN.update({"symbols": "all", "at": __import__("time").monotonic(),
                     "rows": _scan_rows()})
    with pytest.raises(ValueError):
        pm.PublicMarketClient().special_pool_total("/a-share/special-data/zhaban-pool")


def test_empty_scan_raises_instead_of_reporting_zero_limits() -> None:
    """**扫描没结果时必须抛**，绝不能返回 0 —— "0 家涨停"是一种真实状态。

    这是本项目定义的最严重的一类错误（编数）：断网/被限流时界面若显示"涨停 0"，
    用户会当成"今天行情很差"。上层 `market._fetch_limits` 会把它变成"取不到"（`—`）。
    """

    def _boom(url: str, headers: dict, timeout: float) -> bytes:
        raise OSError("network down")

    client = pm.PublicMarketClient(opener=_boom)
    with pytest.raises(RuntimeError):
        client.special_pool_total("/a-share/special-data/limit-up-pool")


def _consistent_opener():
    """**自洽**的假响应：代码表里的票正好是快照里会返回的那两只。

    为什么必须自洽：`pq.snapshot()` 对"腾讯没返回的代码"会走新浪兜底，
    而 `scan()` 在**一只都没取到**时返回空列表（空结果不写缓存）—— 用不搭的两份夹具
    会得到"缓存永远不命中"的假象，然后你会去改其实没坏的产品代码。
    """
    page = json.dumps([{"symbol": "bj430047", "name": "诺思兰德"},
                       {"symbol": "sh600519", "name": "贵州茅台"}]).encode()
    return _opener({"page=1": page,
                    "qt.gtimg.cn": _fixture_bytes("tencent_sentinel.txt")})


def test_client_request_paginates_from_the_cache_without_new_requests() -> None:
    """分页汇总**不再发请求**（同一趟缓存的切片）：翻 6 页的代价是 0 个请求。"""
    opener = _consistent_opener()
    rows = pm.cached_scan(opener=opener)   # 先扫一趟（真扫是 111 个请求，这里 2 个）
    assert len(rows) == 2                  # 夹具自洽：两只票都拿到了
    before = len(opener.calls)

    client = pm.PublicMarketClient(opener=opener)
    page1 = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": 1, "offset": 0})
    page2 = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": 1, "offset": 1})

    assert len(opener.calls) == before                       # 翻页没花请求
    assert len(page1["item"]) == 1 and len(page2["item"]) == 1
    assert page1["total"] == page2["total"] == 2
    assert page1["item"][0]["thscode"] == "430047.BJ"         # `<代码>.<交易所>` 形状
    assert page2["item"][0]["thscode"] == "600519.SH"


def test_client_request_marks_suspended_rows_without_a_change_pct() -> None:
    """停牌那一栏给 **None**，不能给 0.00。

    给 0.00 的后果实测过：`market._fetch_breadth` 把 12 只停牌算成"平盘"，
    同一页上 `counts()` 算出 157、界面显示 169 —— 同一个概念两个数。
    """
    pm._SCAN.update({"symbols": "all", "at": __import__("time").monotonic(),
                     "rows": _scan_rows()})
    page = pm.PublicMarketClient().request(market.MARKET_SNAPSHOT_PATH,
                                          {"limit": 100, "offset": 0})
    by_code = {item["thscode"]: item for item in page["item"]}
    assert by_code["000016.SZ"]["price_change_ratio_pct"] is None      # 停牌
    assert by_code["002731.SZ"]["price_change_ratio_pct"] is None      # 停牌
    assert by_code["000504.SZ"]["price_change_ratio_pct"] == pytest.approx(10.04)
    # 停牌那两只的成交量是 0（上面那两条断言的**依据**就是这个 0）
    assert by_code["000016.SZ"]["volume"] == 0


def test_client_request_rejects_a_path_that_is_not_the_snapshot() -> None:
    """只实现全市场快照这一路；别的路径抛错，免得"静默返回错数据"。"""
    with pytest.raises(ValueError):
        pm.PublicMarketClient().request("/a-share/special-data/limit-up-pool", {})


# ── 缓存：别把自己封了 ─────────────────────────────────────────────────


def test_scan_cache_serves_the_second_call_without_scanning_again() -> None:
    """同一趟扫描在 TTL 内只跑一次 —— 这是"别把 IP 刷进黑名单"的唯一保障。"""
    opener = _consistent_opener()
    first = pm.cached_scan(opener=opener)
    calls_after_first = len(opener.calls)
    second = pm.cached_scan(opener=opener)

    assert first == second
    assert len(opener.calls) == calls_after_first      # 第二次一个请求都没发


def test_failed_scan_is_not_cached() -> None:
    """空结果**不写缓存**：否则一次网络抖动会把界面锁死 5 分钟（一直显示 `—`）。"""
    def _boom(url: str, headers: dict, timeout: float) -> bytes:
        raise OSError("down")

    assert pm.cached_scan(opener=_boom) == []
    assert pm._SCAN["rows"] == []


def test_cached_symbols_serves_the_list_within_ttl() -> None:
    """代码表缓存 6 小时：一天也就变几只票，没必要每次都花 55 个请求。"""
    opener = _opener({"page=1": _fixture_bytes("sina_list_page1.json")})
    first = pm.cached_symbols(opener=opener)
    calls = len(opener.calls)
    assert pm.cached_symbols(opener=opener) == first
    assert len(opener.calls) == calls
    assert len(first) == 100


def test_clear_cache_forgets_both_caches() -> None:
    """`clear_cache()` 两个缓存都要清（少清一个 = "我改了配置怎么没反应"）。"""
    pm._SCAN.update({"symbols": "all", "at": 1.0, "rows": _scan_rows()})
    pm._LIST.update({"at": 1.0, "symbols": ["600519"]})
    pm.clear_cache()
    assert pm._SCAN["rows"] == [] and pm._SCAN["symbols"] is None
    assert pm._LIST["symbols"] == []


# ── 与 `market.py` 的接线：没 Key 时概览页必须落到公开源 ────────────────


class _FakePublicClient:
    """替身客户端（与 `PublicMarketClient` 同形状），用来测 `market.py` 的接线。

    不直接用真的 `PublicMarketClient`：那个会去扫全市场（测试里被 socket 拦死），
    而这里要钉的是"**选了哪个客户端**"这件事 —— 两者的关注点不同。
    """

    def __init__(self, *, up: int = 47, down: int = 1, broke: int = 23,
                 breadth: list[tuple[str, float]] | None = None) -> None:
        self._up, self._down, self._broke = up, down, broke
        self._breadth = breadth or [
            ("600519.SH", 0.71), ("000001.SZ", -0.77), ("300750.SZ", 0.0),
        ]
        self.provider = "public"

    def special_pool_total(self, path: str) -> int:
        suffix = path.rsplit("/", 1)[-1]
        return {"limit-up-pool": self._up, "limit-down-pool": self._down,
                "limit-break-pool": self._broke}[suffix]

    def index_snapshot(self, codes: list[str]) -> dict:
        """按请求的代码逐只回（`.TI` 归 `unsupported`）—— 与真实替身同形状。

        只有 `000001.SH` / `399001.SZ` 带成交额（KPI 的"成交额 = 沪 + 深"读它们）；
        其余指数给一个固定点位，只为让"这一行在页面上"成立。
        """
        turnover = {"000001.SH": 868773066850.0, "399001.SZ": 954361203597.0}
        items, unsupported = [], []
        for code in codes:
            if str(code).endswith(".TI"):
                unsupported.append(code)
                continue
            items.append({
                "thscode": code,
                "last_price": 3875.60 if code == "000001.SH" else 1000.0,
                "price_change_ratio_pct": -0.41 if code == "000001.SH" else -0.20,
                "turnover": turnover.get(code),
            })
        return {"item": items, "failed": [], "unsupported": unsupported}

    def request(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        rows = [{"thscode": code, "turnover": 1e8, "volume": 1000,
                 "price_change_ratio_pct": pct} for code, pct in self._breadth]
        offset = int(params.get("offset") or 0)
        return {"total": len(rows), "item": rows[offset:offset + int(params.get("limit") or 1000)]}


@pytest.fixture()
def no_key(monkeypatch: pytest.MonkeyPatch):
    """把"没有同花顺 Key"这件事做成前置条件（构造客户端就抛，与真实行为一致）。"""
    def _raise(cfg=None, **kwargs):
        raise RuntimeError("没有配置 Key")
    monkeypatch.setattr(market, "_make_client", _raise)


def test_overview_falls_back_to_the_public_source_without_a_key(no_key, cfg,
                                                               monkeypatch) -> None:
    """**核心承诺**：没配 Key 时概览页不再是一整页 `—`，而是走免 Key 公开源**兜底**。

    （2026-09-18 用户把主源换回同花顺：有 Key 时这条路不会走；没 Key 时它是唯一的依靠。
    所以"落到公开源"这条行为没变，只是定位从"主源"变成"兜底"。）

    这里同时钉住来源标记（`price_source`）与页脚里"哪几项是 5 分钟一档"——
    数字是新的、说明是旧的，是这类改版最容易留下的假话。
    """
    monkeypatch.setattr(market, "_public_client", lambda: _FakePublicClient())
    monkeypatch.setattr(cfg, "market_breadth", True, raising=False)

    overview = market.fetch_overview(cfg, force=True)

    assert overview["price_source"] == "public"
    assert overview["pools_slow"] is True
    values = market.kpi_values(overview)
    assert values["涨停"] == "47" and values["跌停"] == "1" and values["炸板"] == "23"
    # 成交额 = 沪 + 深（指数那一批给的）
    assert values["成交额"] == "18231亿"
    assert values["上涨"] == "1" and values["下跌"] == "1" and values["平盘"] == "1"
    footer = market.footer_text(overview)
    assert "涨跌停家数" in footer and "涨跌家数" in footer and "每 5 分钟更新" in footer
    assert any("Key" in e for e in overview["errors"])
    assert market.has_data(overview) is True


def test_overview_uses_hithink_when_a_key_is_configured(cfg) -> None:
    """有 Key 时**走同花顺**（它才是主源；公开源只是没 Key 时的兜底，不抢班）。"""
    client = _FakeHithinkClient()
    overview = market.fetch_overview(cfg, client=client, force=True)
    assert overview["price_source"] == "hithink"
    assert overview["pools_slow"] is False
    assert "涨跌停家数" not in market.footer_text(overview)
    assert client.calls                      # 真的走了这个客户端


class _FakeHithinkClient:
    """最小的同花顺客户端替身（只为断言"选了它"）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def special_pool_total(self, path: str) -> int:
        self.calls.append(path)
        return 55

    def index_snapshot(self, codes: list[str]) -> dict:
        self.calls.append("index")
        return {"item": [{"thscode": "000001.SH", "last_price": 3885.33,
                          "price_change_ratio_pct": -0.07, "turnover": 7792.4e8}],
                "failed": []}

    def request(self, path: str, params: dict | None = None) -> dict:
        self.calls.append("request")
        return {"total": 0, "item": []}


def test_overview_switches_to_public_when_the_key_is_rejected(cfg, monkeypatch) -> None:
    """Key **写错/过期**时也要能落到公开源。

    只认"凭据类"失败：网络不通换成公开源一样取不到，再扫一趟全市场（111 个请求）
    纯属白花 —— 所以这里也钉住"不认的失败不重试"。
    """
    class _AuthFail:
        def special_pool_total(self, path: str) -> int:
            raise market.hx.HithinkAuthError(2002, "Key 无效")

        def index_snapshot(self, codes: list[str]) -> dict:
            raise market.hx.HithinkAuthError(2002, "Key 无效")

        def request(self, path: str, params: dict | None = None) -> dict:
            raise market.hx.HithinkAuthError(2002, "Key 无效")

    used: list[str] = []

    def _public():
        used.append("public")
        return _FakePublicClient()

    monkeypatch.setattr(market, "_public_client", _public)
    overview = market.fetch_overview(cfg, client=_AuthFail(), force=True)

    assert used == ["public"]
    assert overview["price_source"] == "public"
    assert market.kpi_values(overview)["涨停"] == "47"
    assert any("凭据" in e for e in overview["errors"])


def test_overview_does_not_retry_on_a_plain_network_failure(cfg, monkeypatch) -> None:
    """网络类失败**不**改走公开源：换了也一样不通，只白花 111 个请求。"""
    class _NetFail:
        def special_pool_total(self, path: str) -> int:
            raise OSError("网络不可达")

        def index_snapshot(self, codes: list[str]) -> dict:
            raise OSError("网络不可达")

        def request(self, path: str, params: dict | None = None) -> dict:
            raise OSError("网络不可达")

    used: list[str] = []
    monkeypatch.setattr(market, "_public_client",
                        lambda: (used.append("public"), _FakePublicClient())[1])

    overview = market.fetch_overview(cfg, client=_NetFail(), force=True)

    assert used == []
    assert overview["price_source"] == "hithink"
    assert market.has_data(overview) is False


def test_public_path_tells_the_user_which_groups_have_no_public_equivalent(
        no_key, cfg, monkeypatch) -> None:
    """配了 `.TI` 板块指数却看到 `板块：—` 时，必须有一句话说明原因。

    否则用户的推断必然是"数据没刷新/程序坏了"，然后去改一个其实没写错的代码。
    """
    monkeypatch.setattr(market, "_public_client", lambda: _FakePublicClient())
    monkeypatch.setattr(cfg, "market_sentiment_indices", ["883404.TI"], raising=False)
    overview = market.fetch_overview(cfg, force=True)

    assert any("没有这几组的对应标的" in e for e in overview["errors"])
    assert "情绪" in "".join(overview["errors"])
    # `.TI` 不该被当成"你配错了代码"
    assert overview["failed"] == []


def test_no_data_means_no_timestamp(no_key, cfg, monkeypatch) -> None:
    """一项都没取到时页脚必须写"更新于 —"，不许写当前时间。

    `as_of` 是用户判断"这个数有多新"的唯一依据。明明一个数都没有，页脚却显示
    "更新于 09:13"，比空白更糟 —— 那是把"什么都没有"伪装成"刚取到的数据"。
    （这条是被 UI 用例抓出来的：改走免 Key 源之后，页脚突然有了时间戳。）
    """
    def _boom(url, headers, timeout):
        raise OSError("network down")

    monkeypatch.setattr(pm, "list_symbols", lambda **kw: [])
    monkeypatch.setattr(market, "_public_client",
                        lambda: pm.PublicMarketClient(opener=_boom))
    overview = market.fetch_overview(cfg, force=True)

    assert market.has_data(overview) is False
    assert overview["as_of"] == ""
    assert "更新于 —" in market.footer_text(overview)


def test_every_overview_return_path_carries_the_source_keys(cfg) -> None:
    """`price_source` / `pools_slow` 必须在**每一条**返回路径上都有。

    缺键的后果：界面读到 None 会把来源写成"同花顺"，而页面上的数字其实来自公开源 ——
    又一句假话。所以骨架里就得有这两个键（包括"总开关关掉"这条捷径）。
    """
    monkeypatch_off = SimpleNamespace(market_overview=False)
    skeleton = market._skeleton()
    assert "price_source" in skeleton and "pools_slow" in skeleton

    cfg2 = SimpleNamespace(market_overview=False, market_indices=["000001.SH"],
                           market_sentiment_indices=[], market_sector_indices=[],
                           market_breadth=False, market_overview_ttl=55)
    off = market.fetch_overview(cfg2, force=True)
    assert off["price_source"] == "" and off["pools_slow"] is False
