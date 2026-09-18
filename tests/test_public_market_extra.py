"""`data/public_market.py` 的补充边界（`tests/test_public_market.py` 之外的那几处）。

定位（2026-09-18）：这个模块是**没配同花顺 Key 时的兜底**（主源是同花顺；公开接口实测
会限流，所以用户把主次换了回来）。下面的用例测的都是"兜底这一路"的缓存与降级行为 ——
"没 Key 时落到公开源"这条行为没变，变的只是它的定位。

为什么不并进 `test_public_market.py`
-----------------------------------
那个文件钉的是**口径**（涨停类不含 ST/北交所、涨跌家数含、停牌不算平盘）与"没取到 ≠ 0"，
是整个模块的主干。这里补的是三类**缓存与降级**的边界：它们都在"数字看起来正常"的时候
出错，所以最容易被漏掉：

1. **缓存键**：扫描缓存按"要哪些代码"分键。命中错的键会让概览的"涨停家数"显示成
   3 只自选股里的涨停数 —— 一个看起来很正常的小数字。
2. **一趟扫描到底跑几次**：一个客户端实例内部只扫一趟、翻页免费。多扫一趟的代价是
   56 个请求（公开源是**未授权**接口，多打就是被限流/封 IP 的机会）。
3. **代码格式与参数类型**：配置里漏写 `.SH` 后缀、`limit/offset` 传了字符串时都要
   降级成"少一项"，不许升级成"整页取不到"（公开源的每一路都只许少一项）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from laoa_trader import market
from laoa_trader.data import public_market as pm

FIXTURES = Path(__file__).parent / "fixtures" / "public_market"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _rows() -> list[dict]:
    """真实扫描里挑出的 19 行（每个坑一行）。"""
    return json.loads((FIXTURES / "scan_rows.json").read_text(encoding="utf-8"))


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
    """模块级缓存在用例之间必须清干净，否则"扫没扫过"由上一条用例决定。"""
    pm.clear_cache()
    market.clear_cache()
    yield
    pm.clear_cache()
    market.clear_cache()


def test_scan_cache_never_serves_another_symbol_list() -> None:
    """扫描缓存的键里带**代码列表**：显式列表与"全市场"不能互相命中。

    命中错的后果很具体：盯盘/自选按 3 只票扫一趟之后，概览的"涨停家数"会变成
    "那 3 只里的涨停数"—— 一个看起来完全正常的小数字，既不报错也没有任何提示。
    """
    opener = _opener({"qt.gtimg.cn": _fixture_bytes("tencent_sentinel.txt")})
    codes = ["430047", "600519"]

    two = pm.cached_scan(symbols=codes, opener=opener)
    calls = len(opener.calls)
    assert len(two) == 2                                     # 夹具里这两只都有数据

    same = pm.cached_scan(symbols=codes, opener=opener)
    assert same == two
    assert len(opener.calls) == calls                        # 同一个键 → 命中缓存

    one = pm.cached_scan(symbols=["600519"], opener=opener)
    assert len(opener.calls) > calls                         # 换了键 → 必须重扫
    assert [row["symbol"] for row in one] == ["600519"]
    assert len(one) != len(two)                              # 不是"两份数据长得一样"


def test_one_client_instance_scans_the_market_only_once(monkeypatch) -> None:
    """一个 `PublicMarketClient` 内部**只扫一趟**：问三个家数 + 翻两页都不再扫。

    一趟全市场扫描是 56 个请求 / 60 秒。概览页每 55 秒刷一次、每次都问涨停/跌停/炸板
    三个数再翻 6 页 —— 每个问题各扫一趟就是 9 趟 500+ 个请求，那是自己把 IP 送进去。
    这里走**真的** `cached_scan`（只把最底层的 `scan()` 换成夹具），所以钉的是真缓存路径。
    """
    scans: list[object] = []

    def _fake_scan(symbols=None, **kwargs):
        scans.append(symbols)
        return _rows()

    monkeypatch.setattr(pm, "scan", _fake_scan)
    rows = _rows()
    expected = pm.counts(rows)
    client = pm.PublicMarketClient()

    assert client.special_pool_total("/a-share/special-data/limit-up-pool") == expected["up"]
    assert client.special_pool_total("/a-share/special-data/limit-down-pool") == expected["down"]
    assert client.special_pool_total("/a-share/special-data/limit-break-pool") == expected["break"]
    page1 = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": 1000, "offset": 0})
    page2 = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": 5, "offset": 5})

    assert len(scans) == 1                                   # 五问一扫描
    assert page1["total"] == page2["total"] == len(rows)
    assert len(page1["item"]) == len(rows)
    assert [item["thscode"] for item in page2["item"]] == [
        f"{row['symbol']}.{row['exchange']}" for row in rows[5:10]]


def test_index_snapshot_accepts_a_bare_code_without_an_exchange_suffix() -> None:
    """配置里漏写交易所后缀（`600519` 而不是 `600519.SH`）时按**代码段**推前缀去取。

    三组指数代码来自用户的 `config.toml`，格式写错是最常见的一类配置问题。
    这里既钉"真的去取了"（请求串里有 `sh600519`），也钉"取不到就进 `failed`"——
    `failed` 在界面上是"你配的这个代码有问题"，与"公开源根本没有这种标的"
    （`.TI`，走 `unsupported`）是两件事，混在一起会让用户去改一个没错的代码。
    """
    opener = _opener({"qt.gtimg.cn": _fixture_bytes("tencent_indices.txt")})
    result = pm.PublicMarketClient(opener=opener).index_snapshot(["600519", "000001.SH"])

    assert opener.calls and "sh600519" in opener.calls[0]     # 前缀是推出来的，这只没被丢掉
    assert [item["thscode"] for item in result["item"]] == ["000001.SH"]
    assert result["item"][0]["last_price"] == pytest.approx(3875.60)
    assert result["failed"] == ["600519"]                    # 夹具里没有它 → 如实报"取不到"
    assert result["unsupported"] == []


def test_request_falls_back_on_unusable_pagination_params() -> None:
    """`limit`/`offset` 传了垃圾值 → 退成默认（1000/0），不许抛。

    `request(path, params)` 是与同花顺客户端**同形状**的通用入口（`market._fetch_breadth`
    就是按 limit/offset 翻页的）。这一路的语义是"少显示一点"，所以任何一个参数写坏
    都只该降级，不该升级成"整页 `—`"——那会让一次配置手误看起来像数据源挂了。
    """
    pm._SCAN.update({"symbols": "all", "at": time.monotonic(), "rows": _rows()})
    client = pm.PublicMarketClient()

    page = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": "abc", "offset": None})
    assert page["total"] == len(_rows())
    assert len(page["item"]) == len(_rows())                 # limit 退成 1000 > 19

    zero = client.request(market.MARKET_SNAPSHOT_PATH, {"limit": 0, "offset": 0})
    assert len(zero["item"]) == len(_rows())                 # 0 也当成"没给"


def test_cached_symbols_force_refetches_the_list() -> None:
    """`cached_symbols(force=True)` 必须绕过 6 小时缓存。

    代码表 6 小时一档是"别每次都花 55 个请求"的取舍；没有 force 这条路，新股上市、
    改名、退市之后用户只能重启程序才能看到 —— 而他按的是"立即刷新"。
    """
    opener = _opener({"page=1": _fixture_bytes("sina_list_page1.json")})
    first = pm.cached_symbols(opener=opener)
    calls = len(opener.calls)
    assert first and len(first) == 100

    assert pm.cached_symbols(opener=opener) == first
    assert len(opener.calls) == calls                        # TTL 内不再取

    refreshed = pm.cached_symbols(force=True, opener=opener)
    assert len(opener.calls) > calls                         # force → 真的又取了一次
    assert refreshed == first
