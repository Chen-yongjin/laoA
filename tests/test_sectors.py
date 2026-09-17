"""板块行情（`data/sectors.py`）测试：**完全离线**，用真实抓下来的响应做 fixture。

为什么必须有这些用例：板块页上最显眼的两个数就是「涨幅」与「主力净额」，
它们各自有一个**看起来完全正常**的错法：

1. **主力净额的单位**：腾讯给的是**万元**，出口必须是**元**。少乘 1e4 的话，
   `-5.80 亿` 会显示成 `-5.80 万` —— 数字量级差 10000 倍，而屏幕上仍然是个像样的负数，
   没有任何人会一眼看出来（`test_main_net_*` 就是钉这个的）。
2. **取不到就是取不到**：公开接口随时可能限流/改字段/回 HTML，
   这时必须返回 `[]` 或 `None`，绝不能"编一个 0"或"拿上一轮的数顶上"
   （缓存只缓存成功结果，见 `test_failures_are_not_cached`）。

fixture `fixtures/sectors/rank_hy.json` 是 2026-09-17 真实响应的**原始字节**
（腾讯 `proxy.finance.qq.com` 行业排行，31 行，未做任何加工）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laoa_trader.data import sectors

FIXTURES = Path(__file__).parent / "fixtures" / "sectors"


def _fixture_bytes() -> bytes:
    """原始响应字节（**不加任何加工**：解析逻辑要面对的就是这串东西）。"""
    return (FIXTURES / "rank_hy.json").read_bytes()


def _fixture_rows() -> list[dict]:
    return json.loads(_fixture_bytes().decode("utf-8"))["data"]["rank_list"]


class FakeOpener:
    """注入式 opener：按预置的响应序列回答，并记下每次请求的 URL。

    契约与 `public_quotes.Opener` 一致（`opener(url, headers, timeout) -> bytes`）。
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        self.calls.append(url)
        assert self.responses, f"多打了一个请求：{url}"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, bytes)
        return item


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后都清空模块级缓存。

    为什么是 autouse：缓存是**跨用例共享的模块状态**，上一个用例灌进去的 31 行会让
    下一个用例"看起来没发请求"——那种绿是假绿（它测的是缓存，不是它自己声明的行为）。
    """
    sectors.clear_cache()
    yield
    sectors.clear_cache()


# ── 出口契约（字段名一个都不能改）────────────────────────────────────────


def test_sector_fields_contract_is_frozen() -> None:
    """出口字段名与顺序就是界面在用的契约（改一个名字 = 界面取不到那一列）。"""
    assert sectors.SECTOR_FIELDS == (
        "name", "pct", "main_net", "turnover_rate", "volume_ratio",
        "circ_mktcap", "total_mktcap", "up_count", "down_count",
        "leader_name", "leader_symbol", "pct_5d", "pct_20d",
    )
    rows = sectors.parse_rank(_fixture_bytes())
    assert rows, "真实响应解析出来不该是空的"
    for row in rows:
        assert set(row) == set(sectors.SECTOR_FIELDS), row.get("name")


# ── 真实响应：字段与单位 ────────────────────────────────────────────────


def test_real_fixture_row_is_parsed_with_normalized_units() -> None:
    """拿实测那一行（食品饮料）把每个字段都钉一遍。"""
    rows = sectors.parse_rank(_fixture_bytes())
    assert len(rows) == 31                      # 腾讯一级行业实测 31 个（count 只是上限）

    food = next(row for row in rows if row["name"] == "食品饮料")
    assert food["pct"] == pytest.approx(0.29)                  # 百分数原值，不 ×100
    assert food["turnover_rate"] == pytest.approx(1.34)        # %
    assert food["volume_ratio"] == pytest.approx(0.86)
    assert food["circ_mktcap"] == pytest.approx(35357.30)      # 亿（腾讯原值）
    assert food["total_mktcap"] == pytest.approx(36419.54)     # 亿
    assert food["up_count"] == 62 and food["down_count"] == 122
    assert food["leader_name"] == "会稽山"
    assert food["leader_symbol"] == "601579"                   # `sh601579` → 裸 6 位
    assert food["pct_5d"] == pytest.approx(-1.72)
    assert food["pct_20d"] == pytest.approx(-3.24)


def test_main_net_is_yuan_not_wan() -> None:
    """主力净额：腾讯原文 `-58031.01`（万元）→ 出口 `-5.803101e8`（元）≈ **-5.80 亿**。

    少乘 1e4 是最危险的错法：页面照样显示一个负数，只是量级差了 10000 倍。
    所以这里既钉"等于多少"，也钉"绝不是原文那个数"。
    """
    raw = next(row for row in _fixture_rows() if row["name"] == "食品饮料")["zljlr"]
    assert float(raw) == pytest.approx(-58031.01)              # 上游原文（万元）

    food = next(row for row in sectors.parse_rank(_fixture_bytes())
                if row["name"] == "食品饮料")
    assert food["main_net"] == pytest.approx(-580310100.0)     # -58031.01 万元 = 元
    assert food["main_net"] / 1e8 == pytest.approx(-5.803101)  # 亿：-5.80 亿
    assert abs(food["main_net"]) > 1e7, "少了万元→元的换算（差 10000 倍）"


def test_every_fixture_row_gets_a_main_net_number() -> None:
    """31 行都要有主力净额（字段在响应里是齐的，被测出来为 None 就是解析断了）。"""
    rows = sectors.parse_rank(_fixture_bytes())
    assert all(row["main_net"] is not None for row in rows)


# ── 容错：残缺、脏数据、坏信封 ──────────────────────────────────────────


def test_broken_rows_yield_none_instead_of_raising() -> None:
    """字段缺失/认不出 → 对应项 None；**不抛异常、不填 0**。"""
    payload = {"code": 0, "data": {"rank_list": [
        {"name": "只有名字"},
        {"name": "脏数据", "zdf": "-", "hsl": "", "lb": "abc", "zljlr": "",
         "ltsz": None, "zsz": "不是数", "zgb": "62", "lzg": "会稽山"},
        {"name": "", "zdf": "1.23"},                 # 没有名字 → 整行丢掉
        "不是对象",                                    # 整行丢掉
        {"name": "带数字型值", "zdf": 1.23, "zljlr": 100.0, "zgb": 5,
         "lzg": {"name": "某股", "code": "600519.SH"}},
    ]}}
    rows = sectors.parse_rank(payload)
    assert [row["name"] for row in rows] == ["只有名字", "脏数据", "带数字型值"]

    empty = rows[0]
    for key in ("pct", "main_net", "turnover_rate", "volume_ratio", "circ_mktcap",
                "total_mktcap", "up_count", "down_count", "pct_5d", "pct_20d"):
        assert empty[key] is None, key
    assert empty["leader_name"] is None and empty["leader_symbol"] is None

    dirty = rows[1]
    assert dirty["main_net"] is None and dirty["circ_mktcap"] is None
    assert dirty["up_count"] is None and dirty["down_count"] is None   # "62" 没有 "/"
    assert dirty["leader_name"] is None and dirty["leader_symbol"] is None

    typed = rows[2]
    assert typed["pct"] == pytest.approx(1.23)                 # 数字型（非字符串）也认
    assert typed["main_net"] == pytest.approx(1e6)             # 100 万元 → 元
    assert typed["up_count"] is None                           # `zgb=5`（数字）→ 认不出
    assert typed["leader_symbol"] == "600519"                  # `600519.SH` → 裸 6 位


def test_leader_symbol_rejects_non_six_digit_codes() -> None:
    """领涨股代码剥出来不是 6 位就留 None（宁可不显示，也别显示一个错的代码）。"""
    assert sectors._bare_symbol("sh601579") == "601579"
    assert sectors._bare_symbol("601579.SH") == "601579"
    assert sectors._bare_symbol("12345") is None
    assert sectors._bare_symbol("") is None
    assert sectors._bare_symbol(None) is None


def test_bad_envelopes_return_empty_list() -> None:
    """响应不是 JSON / `code != 0` / 没有 `rank_list` → `[]`（当作没取到）。"""
    assert sectors.parse_rank(b"") == []
    assert sectors.parse_rank(b"<html>404</html>") == []
    assert sectors.parse_rank("v_pv_none_match") == []
    assert sectors.parse_rank({"code": 1, "data": {"rank_list": [{"name": "x"}]}}) == []
    assert sectors.parse_rank({"code": 0, "data": {}}) == []
    assert sectors.parse_rank({"code": 0, "data": {"rank_list": "不是列表"}}) == []
    assert sectors.parse_rank({"code": 0, "data": None}) == []
    assert sectors.parse_rank([{"name": "x"}]) == []
    assert sectors.parse_rank(None) == []


# ── 取数：注入 opener、绝不联网 ─────────────────────────────────────────


def test_fetch_uses_injected_opener_and_builds_the_rank_url() -> None:
    """注入 opener 时：只打一个请求，URL 参数（board_type/count/offset）都对。"""
    opener = FakeOpener(_fixture_bytes())
    rows = sectors.fetch_sector_rank(count=60, opener=opener)

    assert len(rows) == 31
    assert len(opener.calls) == 1
    url = opener.calls[0]
    assert url.startswith(sectors.SECTOR_RANK_URL)
    assert "board_type=hy" in url
    assert "count=60" in url and "offset=0" in url


def test_fetch_passes_the_requested_count() -> None:
    opener = FakeOpener(_fixture_bytes())
    sectors.fetch_sector_rank(count=17, opener=opener)
    assert "count=17" in opener.calls[0]


def test_fetch_returns_empty_on_network_failure() -> None:
    """网络失败 → `[]`，**不抛异常**（界面据此显示 `—`，而不是弹一个错误框）。"""
    opener = FakeOpener(OSError("网络断了"), OSError("网络断了"))
    assert sectors.fetch_sector_rank(opener=opener) == []
    assert sectors.fetch_sector_rank(opener=opener) == []       # 失败没被缓存，会真的重试


def test_fetch_returns_empty_when_server_says_nothing() -> None:
    """服务端返回 HTML / `code != 0` → `[]`（不要拿上一轮的数糊上去）。"""
    assert sectors.fetch_sector_rank(opener=FakeOpener(b"<html>bad gateway</html>")) == []
    assert sectors.fetch_sector_rank(
        opener=FakeOpener(json.dumps({"code": 1, "data": None}).encode())) == []


def test_count_below_one_makes_no_request() -> None:
    """`count<1` 是调用方的错，别拿去打公开接口（也不要抛给界面）。"""
    opener = FakeOpener()
    assert sectors.fetch_sector_rank(count=0, opener=opener) == []
    assert sectors.fetch_sector_rank(count=-3, opener=opener) == []
    assert sectors.fetch_sector_rank(count="不是数", opener=opener) == []
    assert opener.calls == []


# ── 缓存（TTL 60 秒：一轮刷新里多处调用，只该发一次请求）──────────────


def test_repeated_calls_within_ttl_hit_the_cache() -> None:
    """60 秒内重复取数只发**一次**请求（板块排行在页面上会被多处调用）。"""
    opener = FakeOpener(_fixture_bytes())
    first = sectors.fetch_sector_rank(opener=opener)
    second = sectors.fetch_sector_rank(opener=opener)

    assert len(opener.calls) == 1, "TTL 之内不该再发请求"
    assert first == second


def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """过了 TTL 必须重新取（**不能变成永久缓存**：那就永远不会刷新了）。"""
    now = [1000.0]
    monkeypatch.setattr(sectors, "_clock", lambda: now[0])
    opener = FakeOpener(_fixture_bytes(), _fixture_bytes())

    sectors.fetch_sector_rank(opener=opener)
    now[0] += sectors.CACHE_TTL + 0.01              # 刚好过期
    sectors.fetch_sector_rank(opener=opener)
    assert len(opener.calls) == 2

    # 再前进一点点，仍在新的 TTL 之内 → 不该再发
    now[0] += 1.0
    sectors.fetch_sector_rank(opener=opener)
    assert len(opener.calls) == 2


def test_cache_is_keyed_by_count() -> None:
    """不同的 `count` 不能互相串（要 60 个的调用不能拿到上次那 30 个的缓存）。"""
    opener = FakeOpener(_fixture_bytes(), _fixture_bytes())
    sectors.fetch_sector_rank(count=30, opener=opener)
    sectors.fetch_sector_rank(count=60, opener=opener)
    assert len(opener.calls) == 2
    assert "count=30" in opener.calls[0] and "count=60" in opener.calls[1]


def test_failures_are_not_cached() -> None:
    """失败**不进缓存**：一次抖动不该让板块页白 60 秒。"""
    opener = FakeOpener(OSError("断了"), _fixture_bytes())
    assert sectors.fetch_sector_rank(opener=opener) == []
    assert len(sectors.fetch_sector_rank(opener=opener)) == 31
    assert len(opener.calls) == 2


def test_cached_rows_are_copies() -> None:
    """返回的是**拷贝**：调用方就地加一列/排序不能把缓存改坏。"""
    opener = FakeOpener(_fixture_bytes())
    rows = sectors.fetch_sector_rank(opener=opener)
    rows[0]["name"] = "被改坏了"
    rows[0]["pct"] = 999.0
    rows.append({"name": "凭空多出来的一行"})

    again = sectors.fetch_sector_rank(opener=opener)
    assert again[0]["name"] == "食品饮料" and again[0]["pct"] == pytest.approx(0.29)
    assert len(again) == 31
    assert len(opener.calls) == 1
