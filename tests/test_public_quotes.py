"""免 Key 公开源（`data/public_quotes.py`）测试：**离线**，用真实抓下来的响应做 fixture。

为什么必须有这些用例：这条源是**分发版的默认主源**（用户不填任何 Key 就靠它看行情），
所以它的两个最容易出错的地方必须钉死：

1. **单位**：腾讯是「手 / 万元」，新浪是「股 / 元」—— 同一张表里混了两种口径，
   用户看到的成交量会差 100 倍，而且"看起来还有数"。`test_units_*` 就是防这个的。
2. **缺数据不许编**：公开接口随时可能限流/改字段，取不到必须是 None（界面据此显示 `—`
   并退回本地收盘价），绝不能变成 0。

fixture 是 2026-09-17 真实抓下来的原文（腾讯 GBK、新浪 GBK，未做任何加工）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from laoa_trader.data import eastmoney as em
from laoa_trader.data import public_quotes as pq
from laoa_trader.data import sources

FIXTURES = Path(__file__).parent / "fixtures" / "public"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="gbk")


def _opener(responses: dict[str, bytes]):
    """假 HTTP 层：按 URL 关键字返回预设响应（没匹配到就抛，逼测试写清期望）。"""
    calls: list[str] = []

    def _get(url: str, headers: dict, timeout: float) -> bytes:
        calls.append(url)
        for key, payload in responses.items():
            if key in url:
                return payload
        raise AssertionError(f"测试没准备这个 URL 的响应：{url}")

    _get.calls = calls          # type: ignore[attr-defined]
    return _get


# ── 解析与单位 ──────────────────────────────────────────────────────────


def test_tencent_units_are_normalized_to_shares_and_yuan() -> None:
    """腾讯的「手」要 ×100 成股、「万元」要 ×1e4 成元（拿真实响应验）。"""
    rows = pq._parse_tencent(_fixture("tencent_three.txt"))

    maotai = rows["600519"]
    assert maotai["name"] == "贵州茅台"
    assert maotai["last_price"] == pytest.approx(1266.98)
    assert maotai["prev_close"] == pytest.approx(1258.0)
    # 17,554 手 → 1,755,400 股（少乘 100 就是最典型的那种"看起来有数"的错误）
    assert maotai["volume"] == pytest.approx(1_755_400)
    # [35] 里的成交额是元（精确值），不是 [37] 的万元
    assert maotai["turnover"] == pytest.approx(2_217_338_283, rel=1e-6)
    assert maotai["pct"] == pytest.approx(0.71)
    assert maotai["turnover_rate"] == pytest.approx(0.14)
    assert maotai["circ_mktcap"] == pytest.approx(15838.28)      # 亿元
    assert maotai["volume_ratio"] == pytest.approx(0.80)
    assert maotai["avg_price"] == pytest.approx(1263.17)
    assert maotai["provider"] == "tencent"
    assert set(rows) == {"600519", "000001", "300750"}


def test_sina_units_are_shares_and_yuan_without_conversion() -> None:
    """新浪本来就是「股 / 元」——**不能再乘 100**（这是同一个坑的另一半）。"""
    rows = pq._parse_sina(_fixture("sina_two.txt"))

    pingan = rows["000002"]
    assert pingan["name"]
    assert pingan["volume"] and pingan["volume"] > 1_000_000     # 明明就是股量级
    assert pingan["provider"] == "sina"
    # 新浪不给换手率/市值/量比：必须是 None（不是 0）
    for key in ("turnover_rate", "circ_mktcap", "total_mktcap", "volume_ratio", "pe"):
        assert pingan[key] is None, key
    # 涨跌幅由昨收现算（新浪不直接给）
    assert pingan["pct"] is not None


def test_missing_fields_stay_none_and_do_not_raise() -> None:
    """字段残缺/越界：取不到就是 None，绝不能让一只票把整批拖挂。"""
    broken = 'v_sh600519="1~贵州茅台~600519~10.00";'          # 只有 4 段
    assert pq._parse_tencent(broken) == {}

    short = 'v_sh600519="1~贵州茅台~600519~10.00~9.50~9.60~100~0~0~' + "~" * 40 + '";'
    rows = pq._parse_tencent(short)
    assert rows["600519"]["last_price"] == pytest.approx(10.0)
    assert rows["600519"]["turnover_rate"] is None


# ── 批量与兜底 ─────────────────────────────────────────────────────────


def test_batches_and_falls_back_to_sina_for_missing_symbols() -> None:
    """腾讯只回了 3 只中的 1 只 → 缺的用新浪补；两个来源的单位都被归一。"""
    opener = _opener({
        "qt.gtimg.cn": _fixture("tencent_three.txt").encode("gbk"),
        "hq.sinajs.cn": _fixture("sina_two.txt").encode("gbk"),
    })
    rows = pq.snapshot(["600519", "000001", "300750", "000002", "601318"],
                       opener=opener, pace=0)

    assert set(rows) == {"600519", "000001", "300750", "000002", "601318"}
    assert rows["600519"]["provider"] == "tencent"
    assert rows["000002"]["provider"] == "sina"
    # 新浪补进来的那只也走同一套归一（成交量是股量级、不是手量级）
    assert rows["000002"]["volume"] > 100_000
    assert any("qt.gtimg.cn" in u for u in opener.calls)
    assert any("hq.sinajs.cn" in u for u in opener.calls)


def test_symbols_are_split_into_batches_of_hundred() -> None:
    """超过 100 只必须分批（腾讯一次只吃 100 个代码）。"""
    codes = [f"6000{i:02d}" for i in range(250)]
    opener = _opener({"qt.gtimg.cn": b"", "hq.sinajs.cn": b""})
    pq.snapshot(codes, opener=opener, pace=0)

    tencent_calls = [u for u in opener.calls if "qt.gtimg.cn" in u]
    assert len(tencent_calls) == 3          # 100 + 100 + 50
    assert all(u.count(",") <= 100 for u in tencent_calls)


def test_network_failure_returns_partial_instead_of_raising() -> None:
    """腾讯挂了、新浪也没有 → 返回空字典，**不抛异常**（界面据此退回本地收盘价）。"""
    def _boom(url, headers, timeout):
        raise OSError("网络断了")

    assert pq.snapshot(["600519"], opener=_boom, pace=0) == {}
    assert pq.snapshot([], opener=_boom, pace=0) == {}
    assert pq.snapshot(None, opener=_boom, pace=0) == {}      # 全市场要调用方给代码表


def test_market_prefix_covers_all_boards() -> None:
    """沪深北（含 ETF）：前缀判错会直接取不到数据。"""
    assert pq.market_prefix("600519") == "sh"
    assert pq.market_prefix("688981") == "sh"
    assert pq.market_prefix("000001") == "sz"
    assert pq.market_prefix("300750") == "sz"
    assert pq.market_prefix("430047") == "bj"
    assert pq.market_prefix("832566") == "bj"
    assert pq.market_prefix("510300") == "sh"      # 沪市 ETF
    assert pq.market_prefix("159915") == "sz"      # 深市 ETF


# ── 统一口径：换手率 / 流通市值 两列不许在任何一路里被丢掉 ────────────────
#
# 2026-09-17 把 `turnover_rate`(%) 与 `circ_mktcap`(亿) 收进了 `sources.QUOTE_FIELDS`。
# 这两列**本来只有公开源有**（腾讯 `[38]`/`[44]`、新浪没有），收进来之后有两处会静默出错：
#   1. `sources.snapshot_map()` 是**按 `QUOTE_FIELDS` 逐键挑**的（`row.get(name)`）——
#      字段不在那份列表里就会被无声丢掉，界面看到的换手率会突然变成 `—`；
#   2. 各来源自己的换算：腾讯是亿、东财 `f21` 是**元**，差 1e8 倍。
# 这几个用例就钉这两件事。为什么放在本文件：这条契约横跨三个来源，
# 而本次改动里 `tests/test_sources.py` 已由另一处改动占用（不碰）。


def _tencent_rows() -> dict[str, dict]:
    return pq._parse_tencent(_fixture("tencent_three.txt"))


def test_snapshot_map_keeps_turnover_rate_and_circ_mktcap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开源（腾讯）的换手率/流通市值必须原样出现在统一口径里。"""
    monkeypatch.setattr(pq, "snapshot", lambda *a, **k: _tencent_rows())
    cfg = SimpleNamespace(data_sources=["public"])

    out = sources.snapshot_map(cfg, ["600519"])
    row = out["600519"]
    assert row["source"] == "public"
    assert set(sources.QUOTE_FIELDS) <= set(row)
    assert row["turnover_rate"] == pytest.approx(0.14)        # %
    assert row["circ_mktcap"] == pytest.approx(15838.28)      # 亿


def test_snapshot_map_fills_none_when_a_source_has_no_such_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源没有这一列（新浪/同花顺快照）→ 统一口径里是 `None`，而不是"没有这个键"。"""
    monkeypatch.setattr(pq, "snapshot", lambda *a, **k: pq._parse_sina(_fixture("sina_two.txt")))
    cfg = SimpleNamespace(data_sources=["public"])

    row = sources.snapshot_map(cfg, ["000002"])["000002"]
    assert "turnover_rate" in row and row["turnover_rate"] is None
    assert "circ_mktcap" in row and row["circ_mktcap"] is None


def test_eastmoney_row_fills_the_two_new_columns_in_unified_units() -> None:
    """东方财富那一路也要填这两列，且单位与公开源对齐（`f21` 元 → 亿）。

    这一行的原始字段是 **2026-09-17 实测**（`ulist.np`，`secids=1.600519`），
    与腾讯同刻的 `[38]=0.14`（换手率）、`[44]=15838.28`（流通市值,亿）**互相印证**：
    `1583828386835 ÷ 1e8 = 15838.28` —— 少除这 1e8，市值就会显示成 158 万亿。
    """
    raw = json.loads(json.dumps({
        "f2": 1266.98, "f3": 0.71, "f5": 17554, "f6": 2217338283.0,
        "f8": 0.14, "f12": "600519", "f14": "贵州茅台", "f15": 1274.0, "f16": 1240.0,
        "f17": 1250.0, "f18": 1258.0, "f21": 1583828386835,
    }))
    row = em.normalize_row(raw)

    assert row is not None
    assert set(row) == set(sources.QUOTE_FIELDS) == set(em.UNIFIED_KEYS)
    assert row["turnover_rate"] == pytest.approx(0.14)          # 百分数原值
    assert row["circ_mktcap"] == pytest.approx(15838.28)        # 元 → 亿
    assert row["volume"] == pytest.approx(1_755_400)            # 手 → 股（老的换算没坏）


def test_eastmoney_snapshot_asks_for_the_two_new_fields() -> None:
    """字段名要**请求**出来：`fields` 里不写 `f8/f21`，服务端就不会返回它们。"""
    assert "f8" in em.SNAPSHOT_FIELDS.split(",")
    assert "f21" in em.SNAPSHOT_FIELDS.split(",")


def test_eastmoney_missing_market_cap_is_none_not_zero() -> None:
    """`f21` 缺失/认不出 → `None`（绝不当成 0 亿）。"""
    raw = {"f2": 10.0, "f3": 1.0, "f5": 100, "f6": 100000.0, "f12": "600000", "f14": "浦发银行"}
    row = em.normalize_row(raw)
    assert row["circ_mktcap"] is None and row["turnover_rate"] is None
    assert row["volume"] == pytest.approx(10_000)
