"""来源注册表：能力矩阵、启停与顺序、未知 id、没 Key 的回退、统一口径的归一化。

外加一小节"接线测试"（`ui/quotes.py` 那四条门槛里最要紧的一条）：
**一个 Key 都没配时，两张表的现价/涨幅照样要能取到** ——
出厂顺序是 `["hithink", "public"]`（**同花顺是主源**），所以那条用例盯的是 `public`：
同花顺没 Key 时它接管，定位是**兜底**而不是主源。
2026-09-18 用户把主次换回来了：公开接口实测会限流（腾讯 fqkline 抓 700 只左右就开始
连续失败、新浪列表接口直接回 HTTP 456），而同花顺是正经 API、不会这么脆。

全部用例都不联网：来源取数函数被换成假函数、公开源的 HTTP 层被换成假 opener，
socket 层还有 conftest 的 `_block_network` 兜底（真外呼会直接失败，而不是悄悄打接口）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from laoa_trader.config import Config
from laoa_trader.data import eastmoney as em
from laoa_trader.data import hithink as hx
from laoa_trader.data import public_quotes as pq
from laoa_trader.data import sources

#: `tests/fixtures/public/` 里是 2026-09-17 真实抓下来的公开源响应（GBK 原文）。
#: 为什么用真实响应而不是手写样本：公开源最容易错的就是**单位归一**
#: （腾讯是"手/万元"、新浪是"股/元"）—— 手写的样本会跟着实现一起写错。
PUBLIC_FIXTURES = Path(__file__).parent / "fixtures" / "public"

@pytest.fixture(autouse=True)
def _clear_warning_memo():
    """清掉"未知来源 id 只告警一次"的去重表。

    为什么要清：那条日志是**进程内去重**的（热路径每 5 秒问一次，不能说一次刷一次），
    留着上一条用例留下的记录会让"该告警"的断言变成"看执行顺序"。
    """
    sources._warned_unknown.clear()
    yield


@pytest.fixture()
def qapp():
    """`QuoteService` 是 QObject：给一个 QApplication，跑完再排空事件队列。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def cfg_sources(*ids: str, **kwargs) -> Config:
    """造一个只改了 `data_sources` 的配置（其余字段全默认）。"""
    return Config(data_sources=list(ids), **kwargs)


def fake_public_opener(calls: list[str] | None = None):
    """公开源的假 HTTP 层：腾讯/新浪的 URL 各返回真实抓下来的响应（**绝不联网**）。

    契约与 `public_quotes.Opener` 一致（`opener(url, headers, timeout) -> bytes`）。
    没准备好的 URL 直接抛 `AssertionError` —— 让"测试打算用哪个接口"写清楚，
    而不是悄悄返回空串（那会变成"来源没数"而不是"测试写错了"）。
    """
    payloads = {
        "qt.gtimg.cn": (PUBLIC_FIXTURES / "tencent_three.txt").read_bytes(),
        "hq.sinajs.cn": (PUBLIC_FIXTURES / "sina_two.txt").read_bytes(),
    }
    seen: list[str] = [] if calls is None else calls

    def _get(url: str, headers: dict, timeout: float) -> bytes:
        seen.append(url)
        for key, payload in payloads.items():
            if key in url:
                return payload
        raise AssertionError(f"测试没准备这个 URL 的响应：{url}")

    _get.calls = seen          # type: ignore[attr-defined]
    return _get


def unified(symbol: str, **overrides) -> dict:
    """一条统一口径的行（东方财富那一路的形状）。"""
    row = {
        "symbol": symbol, "name": "样本", "last_price": 10.0, "prev_close": 9.5,
        "open": 9.6, "high": 10.2, "low": 9.4, "volume": 12300,
        "turnover": 123000.0, "pct": 5.26,
    }
    row.update(overrides)
    return row


# ── REGISTRY：能力矩阵与凭据 ──


def test_registry_has_all_builtin_sources() -> None:
    """内置来源一共三个（原名叫 `..._both_builtin_sources`，现在不止两个）。

    改名与新断言的理由：`public`（免 Key 公开源）是**没配同花顺 Key 时的兜底源**
    ——"不填 Key 也能看行情"这条产品承诺在注册表这一层就要成立，所以它的关键属性
    单独钉死（免 Key、名字写明"免 Key"、能力含快照）。
    ⚠️ 2026-09-18 用户把主源换回同花顺（公开接口实测会被限流），所以它的定位是
    **兜底**而不是主源 —— 这条用例盯的是"它确实兜得住"，不是"它是第一顺位"。
    """
    assert set(sources.REGISTRY) == {"public", "hithink", "eastmoney"}
    pub_info = sources.REGISTRY["public"]
    hx_info = sources.REGISTRY["hithink"]
    em_info = sources.REGISTRY["eastmoney"]
    assert hx_info.needs_key is True and hx_info.key_config == "hithink_api_key"
    assert pub_info.needs_key is False and pub_info.key_config is None
    assert em_info.needs_key is False and em_info.key_config is None
    assert hx_info.name == "同花顺金融数据服务（内置）"
    assert "免 Key" in pub_info.name and "免 Key" in em_info.name
    # 兜底源：能出实时快照（否则"没 Key 也能看行情"这句话根本没有来源支撑）
    assert sources.CAP_SNAPSHOT in pub_info.capabilities
    # ⚠️ 只许声明**已经接进链路**的能力。`public_quotes.daily()` 确实能取单只历史日K，
    # 但它没接进下载/日更链路，所以这里**必须**只有 snapshot：谁要是顺手把
    # CAP_DAILY_HISTORY 加回来，界面就会写着"历史日K"而实际取不到，
    # 用户只会认为"这个源坏了"—— 能力声明是给用户看的承诺，不是实现清单。
    assert pub_info.capabilities == frozenset({sources.CAP_SNAPSHOT})
    # note 是**界面直接显示**的那句话。2026-09-20 用户要求把它压成一句
    # （"只保留当前用哪个来源 / 怎么申请 Key 这类有用的，纯解释的长句全删"），
    # 所以这里钉的是新口径：一句话说清"它是兜底、免 Key、不用申请"，
    # 且**不再**出现能力清单、实测数字、限流故事、字段清单这些解释性文字。
    assert pub_info.note == "兜底源：免 Key，不用申请、不用填。"
    for jargon in ("实测", "限流", "字段", "data/public_market.py", "完整历史"):
        assert jargon not in pub_info.note, jargon


def test_every_snapshot_source_has_a_fetcher() -> None:
    """声明了快照能力的来源**必须**有取数实现（注册表与分派表不许漂移）。

    漂移的后果特别隐蔽：列表里能看到这个来源、能启用、界面一切正常，
    但取数时它被 `_SNAPSHOT_FETCHERS.get()` 静默跳过 —— 表现成"表格永远是空的"。
    反方向也要钉：**没**声明快照能力的来源不许挂在分派表上（那是"幽灵来源"，
    配置里没有任何 id 能指向它），分派表的键也不许是注册表之外的 id。
    """
    for info in sources.REGISTRY.values():
        if sources.CAP_SNAPSHOT in info.capabilities:
            assert info.id in sources._SNAPSHOT_FETCHERS, info.id
        else:
            assert info.id not in sources._SNAPSHOT_FETCHERS, info.id
    assert set(sources._SNAPSHOT_FETCHERS) <= set(sources.REGISTRY)


def test_capabilities_only_use_known_values() -> None:
    for info in sources.REGISTRY.values():
        assert info.capabilities <= set(sources.CAPABILITIES)
        assert info.capabilities, f"{info.id} 一个能力都没声明"


def test_eastmoney_does_not_claim_what_it_cannot_do() -> None:
    """不许假装能力：东方财富这里**没有**涨停池 / 复权因子 / 交易日历。

    而且 2026-09-20（用户："按同一把尺子压成一句，只留结论，实现细节别放界面上"）
    之后，它那句说明只剩"什么时候轮得到它 + 有什么风险"，实测数字/单位差异/
    能力边界/`klt` 参数这些实现细节**一句都不许再出现在界面上**。
    """
    em_info = sources.REGISTRY["eastmoney"]
    assert em_info.capabilities == frozenset({
        sources.CAP_SNAPSHOT, sources.CAP_DAILY_HISTORY, sources.CAP_STOCK_LIST,
    })
    assert em_info.note == ("兜底源之一：需要【添加来源】才会用到，"
                            "接口未文档化、会被限流，只作备用。")
    for jargon in ("涨停池", "复权因子", "交易日历", "klt", "手", "实测", "降级"):
        assert jargon not in em_info.note, jargon


def test_capability_labels_are_chinese_and_complete() -> None:
    assert sources.CAPABILITY_LABELS == {
        sources.CAP_SNAPSHOT: "实时快照",
        sources.CAP_DAILY_HISTORY: "历史日K",
        sources.CAP_STOCK_LIST: "股票代码表",
    }


def test_capability_brief_is_the_one_line_version_for_the_ui() -> None:
    """界面那一行的"提供：…"用**短**标签（2026-09-20 用户要求压成一行）。

    两份文案必须覆盖**同一批能力**（都从 `capabilities` 集合生成）—— 只长短不同，
    否则就会出现"短的那行漏了某个能力、用户以为换来源不会丢它"。
    """
    assert set(sources.CAPABILITY_SHORT_LABELS) == set(sources.CAPABILITY_LABELS)
    for info in sources.REGISTRY.values():
        assert sources.capabilities_brief(info).count("、") + 1 == len(info.capabilities)
        assert sources.capabilities_brief(info) == (
            "、".join(sources.CAPABILITY_SHORT_LABELS[cap] for cap in sources.CAPABILITIES
                      if cap in info.capabilities)
            or "（无）"
        )
    hx = sources.REGISTRY["hithink"]
    assert sources.capabilities_brief(hx) == "实时快照、日线、股票列表"
    # 一行放得下（界面那行还带"提供："与"（换来源会影响这些）"两个前后缀）
    assert len("提供：" + sources.capabilities_brief(hx) + "（换来源会影响这些）") <= 40


# ── active_sources：顺序、未知 id、各种写法 ──


def test_active_sources_follows_config_order() -> None:
    assert [i.id for i in sources.active_sources(cfg_sources("hithink"))] == ["hithink"]
    assert [i.id for i in sources.active_sources(cfg_sources("eastmoney", "hithink"))] == [
        "eastmoney", "hithink",
    ]


def test_active_sources_ignores_unknown_ids_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """未知 id 只记日志、不抛 —— 配置写错一个词不该让界面起不来。"""
    with caplog.at_level(logging.WARNING):
        out = sources.active_sources(cfg_sources("hithink", "csv", "东财", "eastmoney"))
    assert [i.id for i in out] == ["hithink", "eastmoney"]
    assert "csv" in caplog.text and "东财" in caplog.text


def test_active_sources_accepts_hand_written_forms() -> None:
    """用户手改 TOML 的常见写法都要认：字符串、中文逗号、大小写、重复项。"""
    assert [i.id for i in sources.active_sources(Config(data_sources="hithink,eastmoney"))] == [
        "hithink", "eastmoney",
    ]
    assert [i.id for i in sources.active_sources(Config(data_sources="Hithink，eastmoney "))] == [
        "hithink", "eastmoney",
    ]
    assert [i.id for i in sources.active_sources(Config(data_sources=["hithink", "hithink"]))] == [
        "hithink",
    ]
    assert sources.active_sources(Config(data_sources=[])) == []
    assert sources.active_sources(Config(data_sources=None)) == []
    assert sources.active_sources(Config(data_sources=["   "])) == []


def test_active_sources_keeps_keyless_source_in_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 Key 的**启用**来源仍然在 active 列表里（界面要如实显示"没 Key"），
    真取数时才跳过它 —— 这是 `active_sources` 与 `usable_sources` 的分工。"""
    monkeypatch.setattr(hx, "available", lambda: False)
    assert [i.id for i in sources.active_sources(cfg_sources("hithink", "eastmoney"))] == [
        "hithink", "eastmoney",
    ]
    assert [i.id for i in sources.usable_sources(cfg_sources("hithink", "eastmoney"))] == [
        "eastmoney",
    ]


# ── 凭据判定 ──


def test_has_key_for_keyless_source_is_true() -> None:
    """免 Key 的来源 `has_key=True`（界面上那一列答的是"能不能用"，不是"填没填"）。"""
    cfg = cfg_sources("eastmoney")
    assert sources.has_key(cfg, sources.REGISTRY["eastmoney"]) is True
    assert sources.has_key(cfg, sources.REGISTRY["hithink"]) is False


def test_hithink_key_from_config_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 这里**故意不 patch `hx.available`**：要走的就是真实那条判据
    # （环境变量 → config.toml），patch 掉等于把被测的东西换掉。
    # 先把三个凭据环境变量清空，免得宿主环境里真有一个 Key 让断言失真。
    for name in ("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert sources.has_key(cfg_sources("hithink"), sources.REGISTRY["hithink"]) is False
    assert sources.has_key(cfg_sources("hithink", hithink_api_key="cfg-key"),
                           sources.REGISTRY["hithink"]) is True
    # 环境变量（`hx.available()` 覆盖的正是这条路）：没写进 config.toml 也算有 Key
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "env-key")
    assert sources.has_key(cfg_sources("hithink"), sources.REGISTRY["hithink"]) is True


# ── source_states：给界面渲染的那些行 ──


def test_source_states_lists_all_sources_with_enabled_first() -> None:
    """界面要能**添加来源**，所以没启用的也要列出来（这是与 active_sources 的关键差别）。

    期望值随注册表走：启用的是 `eastmoney` 排第一，其余按 `REGISTRY` 的定义顺序
    跟在后面 —— 现在是 `hithink`（主源，注册表里排第一）与 `public`（兜底）。
    """
    states = sources.source_states(cfg_sources("eastmoney"))
    # 没启用的排前面之后，剩下的按**注册表顺序**（2026-09-18 用户把主源换回同花顺，
    # 所以它在注册表里也排第一）
    assert [row["id"] for row in states] == ["eastmoney", "hithink", "public"]
    assert [row["enabled"] for row in states] == [True, False, False]
    assert set(states[0]) == {
        "id", "name", "enabled", "needs_key", "has_key",
        "capabilities_text", "capabilities_brief", "note", "key_config", "capabilities",
    }


def test_source_states_default_order_and_texts() -> None:
    """`source_states` 给界面渲染的那几行：能力文案、角色说明（note）都要如实。

    2026-09-18 用户把主次换回来（**同花顺是主源**、免 Key 公开源是兜底），所以
    `note` 这一栏也跟着改口 —— 而这正是**界面直接显示给用户看的那句话**：
    它要是还写着"公开源是主源"，用户就会按错误的理解去配 Key/排顺序。
    这条用例原来只断言 `note` 非空（等于没钉内容），这里补上两边的关键词。
    """
    states = {row["id"]: row for row in sources.source_states(cfg_sources("hithink"))}
    assert states["hithink"]["enabled"] is True
    assert states["hithink"]["has_key"] is False              # 没配 Key
    assert states["hithink"]["needs_key"] is True
    assert states["eastmoney"]["enabled"] is False
    assert states["eastmoney"]["has_key"] is True             # 免 Key = 可用
    assert states["eastmoney"]["capabilities_text"] == "实时快照、历史日K、股票代码表"
    assert states["eastmoney"]["key_config"] is None
    assert states["eastmoney"]["capabilities"] == (
        "daily_history", "snapshot", "stock_list",
    )
    # 免 Key 的公开源同理：没启用、但"可用"，能力文案**只列已接通的那一项**，
    # 与它声明的 capabilities 严格一致（多一个字都算过度承诺）
    assert states["public"]["enabled"] is False
    assert states["public"]["has_key"] is True
    assert states["public"]["key_config"] is None
    assert states["public"]["capabilities_text"] == "实时快照"
    assert states["public"]["capabilities"] == ("snapshot",)
    assert "免 Key" in states["public"]["name"]
    # 两边的角色说明必须是**新口径**（这一栏直接显示在界面上，写错就是在说假话）
    hx_note = states["hithink"]["note"]
    pub_note = states["public"]["note"]
    # 2026-09-20 起：这两句都压成了**一句话**（用户："只保留当前用哪个来源 / 怎么申请
    # Key 这类有用的，纯解释的长句全删"）。钉住的是"留下的是哪两件有用的事"：
    assert "主源" in hx_note and "申请" in hx_note             # 同花顺 = 主源 + 去哪申请
    assert "fuyao.aicubes.cn" in hx_note                        # 申请地址必须还在（可点）
    assert "筛选" in hx_note                                    # 没 Key 的后果：匹配被自检拒绝
    assert "config.toml" not in hx_note and "环境变量" not in hx_note   # 实现细节不上界面
    assert pub_note == "兜底源：免 Key，不用申请、不用填。"
    # 两句话都必须短（长文就是这次要清掉的东西）
    # 短到"一眼看完"：同花顺那句 65 字（主源 + 申请地址 + 没 Key 的后果），公开源 16 字
    assert len(hx_note) <= 80 and len(pub_note) <= 30


def test_source_states_capabilities_text_drops_missing_ones() -> None:
    info = sources.SourceInfo(
        id="x", name="假来源", needs_key=False,
        capabilities=frozenset({sources.CAP_SNAPSHOT}), note="",
    )
    assert sources.capabilities_text(info) == "实时快照"
    assert sources.capabilities_text(
        sources.SourceInfo(id="y", name="空来源", needs_key=False,
                           capabilities=frozenset(), note="")
    ) == "（无）"


# ── hithink_rows_to_map：同花顺口径（股 / 元，不换算）──


def test_hithink_rows_to_map_keeps_share_and_yuan_units() -> None:
    rows = [
        {"thscode": "600519.SH", "last_price": 1259.73, "price_change_ratio_pct": -1.02,
         "open_price": 1273.93, "prev_price": 1272.75, "high_price": 1274.98,
         "low_price": 1254.10, "volume": 24318, "turnover": 3066631576.0},
        {"thscode": "600000.SH", "last_price": 0},              # 停牌 → 丢掉
        {"thscode": "600001.XX", "last_price": 10.0},           # 认不出的后缀 → 跳过
        {"ticker": "000001", "last_price": 11.7},               # 只有 ticker 也能认
        {"last_price": 10.0},                                   # 没有代码 → 丢掉
    ]
    out = sources.hithink_rows_to_map(rows)
    assert set(out) == {"600519", "000001"}
    assert out["600519"] == {
        "symbol": "600519", "name": "",        # 同花顺快照不返回中文名
        "last_price": 1259.73, "prev_close": 1272.75, "open": 1273.93,
        "high": 1274.98, "low": 1254.10,
        "volume": 24318,                       # **股**，不换算
        "turnover": 3066631576.0,              # 元
        "pct": -1.02,
    }


# ── snapshot_map：挑来源、失败降级、统一口径 ──


def test_keyless_source_is_not_even_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 Key 的来源**连取数函数都不进**（凭据判断在分派之前，不是靠取数函数自己兜）。

    为什么要单测这一条：`_hithink_rows` 自己也会判 Key 并返回空 —— 两层判断都在，
    结果一样，所以"上面那层被删掉"在别的用例里看不出来。这里直接盯**分派**：
    没 Key 的同花顺一次都不该被派发出去。
    """
    attempts: list[str] = []
    monkeypatch.setattr(sources, "_SNAPSHOT_FETCHERS", {
        **sources._SNAPSHOT_FETCHERS,
        "hithink": lambda cfg, symbols: attempts.append("hithink") or {},
    })
    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(em, "snapshot",
                        lambda *a, **k: attempts.append("em") or [unified("600519")])
    out = sources.snapshot_map(cfg_sources("hithink", "eastmoney"), ["600519"])
    assert attempts == ["em"]                       # 同花顺没被派发，东方财富才是那个来源
    assert out["600519"]["source"] == "eastmoney"


def test_snapshot_map_empty_symbols_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有要盯的票：一次请求都不发（空列表与 None 是两件事）。"""
    def boom(*args, **kwargs):
        raise AssertionError("不该发请求")

    monkeypatch.setattr(em, "snapshot", boom)
    monkeypatch.setattr(em, "snapshot_all", boom)
    assert sources.snapshot_map(cfg_sources("eastmoney"), []) == {}


def test_snapshot_map_without_any_usable_source_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个可用来源都没有 → `{}`，而且**一次请求都不发**（不抛）。"""
    monkeypatch.setattr(hx, "available", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: calls.append("em-snapshot") or [])
    monkeypatch.setattr(em, "snapshot_all", lambda *a, **k: calls.append("em-all") or [])
    monkeypatch.setattr(hx, "HithinkClient",
                        lambda *a, **k: calls.append("hx-client") or object())
    monkeypatch.setattr(pq, "_urllib_get",
                        lambda url, headers, timeout: calls.append("public") or b"")
    assert sources.snapshot_map(cfg_sources("hithink"), ["600519"]) == {}       # 没 Key
    assert sources.snapshot_map(cfg_sources(), ["600519"]) == {}                # 一个都没启用
    assert calls == []                                                          # 谁都没被叫


def test_snapshot_map_with_no_enabled_source_sends_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`data_sources = []`（用户把来源全删了）→ 没有可用来源、**一个字节都不发**。

    为什么要单独一条：`public` 是免 Key 的，**永远"可用"** ——
    "一个可用来源都没有"这个前提从 2026-09-17 起只能**显式构造**（把列表清空，
    或只留一个没配 Key 的同花顺）。这里同时盯住"注册层面为空"与"HTTP 层没被打"：
    只看其中一层的话，另一层悄悄绕过去是看不出来的。
    """
    monkeypatch.setattr(hx, "available", lambda: False)
    calls: list[str] = []
    opener = fake_public_opener(calls)
    monkeypatch.setattr(pq, "_urllib_get", opener)
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: calls.append("em-snapshot") or [])
    monkeypatch.setattr(em, "snapshot_all", lambda *a, **k: calls.append("em-all") or [])
    monkeypatch.setattr(hx, "HithinkClient",
                        lambda *a, **k: calls.append("hx-client") or object())
    cfg = cfg_sources()                                       # data_sources = []
    assert sources.active_sources(cfg) == []
    assert sources.usable_sources(cfg) == []                  # 一个能用的都没有
    assert sources.snapshot_map(cfg, ["600519"]) == {}
    assert sources.snapshot_map(cfg, None) == {}              # 全市场那条路同样不发
    assert calls == []                                        # 公开源/东财/同花顺都没被叫
    assert opener.calls == []


def test_snapshot_map_uses_the_public_source_without_any_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只启用 `public`、**一个 Key 都没配** → 照样拿到统一口径的行情（免 Key 兜底源）。

    用真实响应做假 HTTP 层：这条用例真正要保护的是"**没 Key 也有数**"，
    以及"公开源那一路的单位也已经归一成股/元"（两张表的口径不随来源变化）。
    """
    monkeypatch.setattr(hx, "available", lambda: False)
    opener = fake_public_opener()
    monkeypatch.setattr(pq, "_urllib_get", opener)
    out = sources.snapshot_map(cfg_sources("public"), ["600519", "000001"])

    assert set(out) == {"600519", "000001"}
    maotai = out["600519"]
    assert maotai["source"] == "public"
    assert maotai["name"] == "贵州茅台"
    assert maotai["last_price"] == pytest.approx(1266.98)
    assert maotai["prev_close"] == pytest.approx(1258.0)
    assert maotai["pct"] == pytest.approx(0.71)
    assert maotai["volume"] == pytest.approx(1_755_400)        # 股（腾讯的"手"已换算）
    assert maotai["turnover"] == pytest.approx(2_217_338_283, rel=1e-6)
    assert set(sources.QUOTE_FIELDS) <= set(maotai)
    assert abs(maotai["as_of"] - time.time()) < 5
    # 两只票一次批量请求（分批是来源自己的事，这里只确认没被拆成两次）
    assert len(opener.calls) == 1 and "qt.gtimg.cn" in opener.calls[0]


def test_snapshot_map_public_needs_a_symbol_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """`symbols=None`（要全市场）时公开源**不出数**：腾讯没有"给我全部"的开关。

    这是公开源已知的边界（全市场得由调用方给代码表），如实钉住 ——
    否则以后有人把 `None` 当成"随便给点"就有了想象空间。
    """
    monkeypatch.setattr(hx, "available", lambda: False)
    opener = fake_public_opener()
    monkeypatch.setattr(pq, "_urllib_get", opener)
    assert sources.snapshot_map(cfg_sources("public"), None) == {}
    assert opener.calls == []                                 # 连请求都没发


def test_snapshot_map_prefers_hithink_over_public_when_a_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`["hithink", "public"]` 且配了 Key → **同花顺接管**，公开源一次都不打。

    这是"有 Key 的用户自动用同花顺"这条产品承诺的最小验证：
    顺序即优先级，第一个能出数的来源胜出，后面的来源连请求都不该发。
    """
    calls: list[str] = []
    public_calls: list[str] = []

    class FakeHx:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("hx")

        def snapshot(self, thscodes=None, **kwargs):
            calls.append("hx-snapshot")
            return [{"thscode": "600519.SH", "last_price": 1258.0,
                     "price_change_ratio_pct": -1.16, "volume": 2623500,
                     "turnover": 3307926407.0}]

    monkeypatch.setattr(hx, "HithinkClient", FakeHx)
    monkeypatch.setattr(pq, "_urllib_get",
                        lambda url, headers, timeout: public_calls.append(url) or b"")
    out = sources.snapshot_map(
        cfg_sources("hithink", "public", hithink_api_key="k"), ["600519"]
    )
    assert out["600519"]["source"] == "hithink"
    assert out["600519"]["last_price"] == 1258.0
    assert calls == ["hx", "hx-snapshot"]
    assert public_calls == []                                 # 公开源没被打


def test_snapshot_map_falls_through_when_the_public_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """免 Key 兜底源（公开源）挂了 → 只记日志，落到下一个来源，**绝不抛异常**。

    公开接口随时可能限流/改字段（2026-09-18 实测：腾讯 fqkline 抓 700 只左右开始连续
    失败、新浪列表接口回 HTTP 456），所以"兜底源失败"是常态而不是意外：
    配了 Key 的同花顺就该能接管（这条就是那份承诺的用例）。
    """
    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(em, "snapshot",
                        lambda symbols, **k: [unified("600519", last_price=11.0)])

    def _boom(url: str, headers: dict, timeout: float) -> bytes:
        raise OSError("腾讯接口连不上")

    # ① 网络层失败：`public_quotes` 自己吞掉 → 公开源返回空 → 换下一个来源
    monkeypatch.setattr(pq, "_urllib_get", _boom)
    out = sources.snapshot_map(cfg_sources("public", "eastmoney"), ["600519"])
    assert out["600519"]["source"] == "eastmoney"
    assert out["600519"]["last_price"] == 11.0

    # ② 公开源整路抛异常（不是"没数据"）：`snapshot_map` 也只记日志、继续换下一个
    monkeypatch.setattr(pq, "snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("公开源炸了")))
    out2 = sources.snapshot_map(cfg_sources("public", "eastmoney"), ["600519"])
    assert out2["600519"]["source"] == "eastmoney"

    # ③ 后面没有来源了 → 返回 `{}`（界面退回本地收盘价），不是抛出去
    assert sources.snapshot_map(cfg_sources("public"), ["600519"]) == {}


def test_snapshot_map_falls_back_to_eastmoney_without_hithink_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**没填同花顺 Key** → 用东方财富拿到现价（用户要的那条路）。"""
    monkeypatch.setattr(hx, "available", lambda: False)
    seen: list[list[str]] = []

    def fake_snapshot(symbols, **kwargs):
        seen.append(list(symbols))
        return [unified("600519"), unified("000001", last_price=11.7, pct=-1.02)]

    monkeypatch.setattr(em, "snapshot", fake_snapshot)
    out = sources.snapshot_map(cfg_sources("hithink", "eastmoney"), ["600519", "000001"])
    assert seen == [["600519", "000001"]]                    # 一次批量请求
    assert set(out) == {"600519", "000001"}
    assert out["600519"]["last_price"] == 10.0
    assert out["600519"]["source"] == "eastmoney"
    assert out["600519"]["volume"] == 12300                  # 已经是股（来源层换算过）
    assert abs(out["600519"]["as_of"] - time.time()) < 5


def test_snapshot_map_prefers_the_first_source_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同花顺优先：有 Key 时**不会**再去问东方财富（同一时刻只有一个来源在跑）。"""
    calls: list[str] = []

    class FakeHx:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("hx")

        def snapshot(self, thscodes=None, **kwargs):
            calls.append("hx-snapshot")
            return [{"thscode": "600519.SH", "last_price": 1258.0,
                     "price_change_ratio_pct": -1.16, "volume": 2623500,
                     "turnover": 3307926407.0}]

    monkeypatch.setattr(hx, "HithinkClient", FakeHx)
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: calls.append("em") or [])
    cfg = cfg_sources("hithink", "eastmoney", hithink_api_key="k")
    out = sources.snapshot_map(cfg, ["600519"])
    assert out["600519"]["source"] == "hithink"
    assert out["600519"]["last_price"] == 1258.0
    assert calls == ["hx", "hx-snapshot"]                    # 东方财富一次都没被叫


def test_snapshot_map_moves_on_when_the_first_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同花顺报错 → 只记日志、落到东方财富（顺序是配置顺序，不是并发抢答）。"""
    calls: list[str] = []

    class BoomHx:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("hx")

        def snapshot(self, *a, **k):
            raise RuntimeError("同花顺挂了")

    monkeypatch.setattr(hx, "HithinkClient", BoomHx)
    monkeypatch.setattr(em, "snapshot",
                        lambda *a, **k: calls.append("em") or [unified("600519")])
    out = sources.snapshot_map(
        cfg_sources("hithink", "eastmoney", hithink_api_key="k"), ["600519"]
    )
    assert calls == ["hx", "em"]
    assert out["600519"]["source"] == "eastmoney"


def test_snapshot_map_returns_empty_when_everything_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个来源都不可用 → 返回空字典（**绝不抛到界面**）。"""
    class BoomHx:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def snapshot(self, *a, **k):
            raise RuntimeError("同花顺挂了")

    monkeypatch.setattr(hx, "HithinkClient", BoomHx)
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: (_ for _ in ()).throw(
        em.EastmoneyError("东财也挂了")))
    cfg = cfg_sources("hithink", "eastmoney", hithink_api_key="k")
    assert sources.snapshot_map(cfg, ["600519"]) == {}
    # 单个票也要能扛住：来源返回空 → 同样是 {}
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: [])
    assert sources.snapshot_map(cfg, ["600519"]) == {}


def test_snapshot_map_only_returns_asked_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    """来源多给了别的票（比如全市场快照）→ 只要用户要的那些。"""
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: [
        unified("600519"), unified("000001"),
    ])
    out = sources.snapshot_map(cfg_sources("eastmoney"), ["600519"])
    assert set(out) == {"600519"}


def test_snapshot_map_whole_market_path_uses_snapshot_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`symbols=None` = 要全市场 → 东方财富那一路走分页（代价写在函数注释里）。"""
    calls: list[str] = []
    monkeypatch.setattr(em, "snapshot_all",
                        lambda **k: calls.append("all") or [unified("600519")])
    monkeypatch.setattr(em, "snapshot",
                        lambda *a, **k: calls.append("batch") or [])
    out = sources.snapshot_map(cfg_sources("eastmoney"))
    assert calls == ["all"]
    assert set(out) == {"600519"}


def test_snapshot_map_rows_carry_every_unified_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """统一口径的键一个都不能少（缺的填 None，而不是悄悄没有这个键）。"""
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: [
        {"symbol": "600519", "last_price": 1258.0},          # 只有两个字段的"穷"行
    ])
    out = sources.snapshot_map(cfg_sources("eastmoney"), ["600519"])
    row = out["600519"]
    assert set(sources.QUOTE_FIELDS) <= set(row)
    assert row["high"] is None and row["volume"] is None and row["name"] is None
    assert row["source"] == "eastmoney" and isinstance(row["as_of"], float)


def test_snapshot_map_reports_when_a_source_cannot_fetch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """认不出的来源 id 与"启用了但没实现"的情况都不该炸，且日志里说清楚。"""
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: [unified("600519")])
    with caplog.at_level(logging.WARNING):
        out = sources.snapshot_map(cfg_sources("csv", "eastmoney"), ["600519"])
    assert set(out) == {"600519"}
    assert "csv" in caplog.text


# ── 测试连接（给设置界面每一行留的钩子）──


def test_probe_source_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """测"东方财富"这一行时**不许**偷偷去问同花顺（否则测的是别的来源）。"""
    calls: list[str] = []

    class FakeHx:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("hx")

        def snapshot(self, *a, **k):
            calls.append("hx-snapshot")
            return [{"thscode": "600519.SH", "last_price": 1258.0}]

    monkeypatch.setattr(hx, "HithinkClient", FakeHx)
    monkeypatch.setattr(em, "snapshot",
                        lambda *a, **k: calls.append("em") or [unified("600519")])
    cfg = cfg_sources("hithink", "eastmoney", hithink_api_key="k")
    out = sources.probe_source(cfg, "eastmoney")
    assert out["ok"] is True and out["source"] == "eastmoney"
    assert out["price"] == 10.0 and "东方财富" in out["text"]
    assert calls == ["em"]


def test_probe_source_reports_missing_key_and_unknown_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hx, "available", lambda: False)
    out = sources.probe_source(cfg_sources("hithink"), "hithink")
    assert out["ok"] is False and "hithink_api_key" in out["text"]
    unknown = sources.probe_source(cfg_sources("eastmoney"), "tushare")
    assert unknown["ok"] is False and "没有这个来源" in unknown["text"]


def test_probe_source_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: (_ for _ in ()).throw(
        em.EastmoneyError("被限流")))
    out = sources.probe_source(cfg_sources("eastmoney"), "eastmoney")
    assert out["ok"] is False and "被限流" in out["text"]
    # 通了但没这只票的价（停牌/限流）也要给出人话
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: [])
    empty = sources.probe_source(cfg_sources("eastmoney"), "eastmoney")
    assert empty["ok"] is False and "没拿到" in empty["text"]


def test_probe_source_tolerates_bad_symbol_input(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(em, "snapshot",
                        lambda symbols, **k: seen.append(list(symbols)) or [unified("600519")])
    assert sources.probe_source(cfg_sources("eastmoney"), "eastmoney", "")["ok"] is True
    assert seen == [["600519"]]                 # 空代码回落到默认的 600519


def test_probe_source_public_says_ok_without_any_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """**【测试】按钮对免 Key 兜底源也要给"通"**（用户没配任何 Key 时的第一行）。

    设置页那一行的意义就是"点一下看它到底通不通"；公开源不需要 Key，
    所以这里 `has_key` 必须为真、`ok` 必须为真 —— 否则用户会去找一个不存在的 Key。
    走到的是真解析（假 HTTP 层喂真实响应），单位也一并验了。
    """
    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(pq, "_urllib_get", fake_public_opener())
    out = sources.probe_source(cfg_sources("public"), "public")
    assert out["ok"] is True and out["source"] == "public"
    assert out["symbol"] == "600519" and out["price"] == pytest.approx(1266.98)
    assert "免 Key" in out["text"] and "1266.98" in out["text"]
    # 免 Key 的来源**不许**报"还没配 Key"
    assert "还没配 Key" not in out["text"]


# ── 接线：`ui/quotes.py`（用户拍板的那条行为变化）──


def test_quotes_uses_the_public_source_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**主目标**：一个 Key 都不配 → 两张表照样有现价/涨幅（落到免 Key 的公开源）。

    原名 `test_quotes_uses_eastmoney_when_hithink_key_is_missing`：**意图一字未改**
    （"绝不因为没 Key 就什么都不返回"），变的是"第一个免 Key 的来源"—— 出厂顺序是
    `["hithink", "public"]`，同花顺没 Key 时**跳过**它、落到 `public` 兜底。
    东方财富那条路仍在，只是排在公开源后面（下方第二个场景）。
    """
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(pq, "_urllib_get", fake_public_opener())   # 离线：真实响应做假 HTTP 层

    # ① 出厂默认顺序（`Config()` 就是 `["hithink", "public"]`；同花顺没 Key → 落到兜底）
    out = q.fetch_snapshot_prices(Config(), ["600519"])
    assert set(out) == {"600519"}                    # 没 Key 也**不是空**
    quote = out["600519"]
    # 界面（`app._price_cells`）读的就是这三个键
    assert quote["price"] == pytest.approx(1266.98)
    assert quote["pct"] == pytest.approx(0.71)
    assert isinstance(quote["at"], float) and quote["at"] > 0
    # 统一口径的字段一并带上（来源与取数时刻要能显示出来）
    assert quote["last_price"] == pytest.approx(1266.98)
    assert quote["source"] == "public"
    assert quote["volume"] == pytest.approx(1_755_400)          # 股（来源层已归一）

    # ② 东财排在公开源后面时的兜底也还在（同一条意图的另一种排法）
    monkeypatch.setattr(em, "snapshot", lambda symbols, **k: [
        unified("600519", last_price=1258.0, pct=-1.16, volume=2623500,
                turnover=3307926407.0),
    ])
    fallback = q.fetch_snapshot_prices(cfg_sources("hithink", "eastmoney"), ["600519"])
    assert fallback["600519"]["source"] == "eastmoney"
    assert fallback["600519"]["price"] == 1258.0
    assert fallback["600519"]["volume"] == 2623500


def test_quotes_sends_nothing_without_a_usable_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有可用来源 → 一次请求都不发、返回空（表格退回本地收盘价并标注）。

    `public` 是免 Key 的、**永远可用**，所以"一个可用来源都没有"这个前提现在
    只能**显式构造**：来源列表为空（用户全删了），或只留一个没配 Key 的同花顺。
    """
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    entered: list[str] = []
    # 盯**三层**：`quotes` 自己的门槛（连 `sources.snapshot_map` 都不进）、
    # 来源层的取数函数、以及免 Key 兜底源自己的 HTTP 层 ——
    # 只看其中一层的话，另一层被删掉（或绕过）是看不出来的
    monkeypatch.setattr(sources, "snapshot_map",
                        lambda *a, **k: entered.append("snapshot_map") or {})
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: entered.append("em") or [])
    monkeypatch.setattr(em, "snapshot_all", lambda *a, **k: entered.append("em-all") or [])
    monkeypatch.setattr(pq, "_urllib_get",
                        lambda url, headers, timeout: entered.append("public") or b"")
    none_enabled = cfg_sources()                                # 用户把来源全删了
    assert sources.usable_sources(none_enabled) == []
    assert q.fetch_snapshot_prices(none_enabled, ["600519"]) == {}
    # 只留一个没配 Key 的同花顺：同样一次请求都不发
    assert q.fetch_snapshot_prices(cfg_sources("hithink"), ["600519"]) == {}
    assert entered == []


def test_cancelled_quote_worker_never_emits(qapp) -> None:
    """被取消的取数线程**一个信号都不许再发**（CI 那个"没有失败记录的中途崩溃"的堵口）。

    为什么钉这条：CI（Windows）上反复出现"跑到 ~93% 进程直接没了、没有任何用例失败记录"，
    faulthandler 抓到的现场就是 `QuoteWorker.run` 里的 `ready.emit(...)` ——
    接收方（已被销毁的控件）成了悬空指针。本机（Linux）复现不了 Windows 的 access
    violation，所以只能在源头把"取消之后不发信号"这条行为钉死：
    这里直接同步调 `run()`，模拟"取消之后那一轮才收工"。
    """
    from laoa_trader.ui import quotes as q

    got: list = []
    worker = q.QuoteWorker(lambda cfg, syms: {"600001": {"close": 1.0}}, "cfg", ["600001"])
    worker.ready.connect(got.append)

    worker.cancel()
    worker.run()

    assert got == [], "取消之后还发了 ready 信号 —— 顶层窗口销毁时这就是那个 access violation"
    assert worker.cancelled() is True


def test_running_quote_worker_without_cancel_still_emits(qapp) -> None:
    """反向：没被取消的那一轮**必须照常发结果**（否则就是"修崩了功能"）。"""
    from laoa_trader.ui import quotes as q

    got: list = []
    worker = q.QuoteWorker(lambda cfg, syms: {"600001": {"close": 1.0}}, "cfg", ["600001"])
    worker.ready.connect(got.append)

    worker.run()

    assert got == [{"600001": {"close": 1.0}}]


def test_stopping_the_service_cancels_the_inflight_worker(qapp) -> None:
    """`stop()` 不只是"等 3 秒" —— 它**先取消**，等不到也不会留下会发信号的线程。

    顺序为什么重要：真实环境一轮取数要几秒到几十秒，3 秒等不到是常态；只 wait 的话，
    线程会带着"发结果"的念头活到窗口销毁之后（CI 上就这么崩的）。
    """
    from laoa_trader.ui import quotes as q

    service = q.QuoteService(cfg_sources("public"), lambda: ["600001"])
    service._fetch = lambda cfg, syms, **kw: {}        # 不发网络请求
    assert service.request() is True
    worker = service._worker
    assert worker is not None

    service.stop()

    assert worker.cancelled() is True
    assert service._worker is None


def test_quotes_service_gate_follows_usable_sources(
    monkeypatch: pytest.MonkeyPatch, qapp
) -> None:
    """那四条门槛里"没有可用来源就不取"：没 Key 的 `["hithink"]` 不发，
    免 Key 的来源（`public` / `eastmoney`）只要在列表里就发（交易时段内）。"""
    from laoa_trader import intraday, state
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(state, "is_downloading", lambda: False)
    monkeypatch.setattr(intraday, "in_session", lambda now=None: True)
    provider = lambda: ["600519"]        # noqa: E731 - 一行的小闭包
    only_hx = q.QuoteService(cfg_sources("hithink"), provider)
    only_public = q.QuoteService(cfg_sources("public"), provider)
    both = q.QuoteService(cfg_sources("hithink", "public"), provider)
    em_only = q.QuoteService(cfg_sources("eastmoney"), provider)
    none = q.QuoteService(cfg_sources(), provider)
    empty_symbols = q.QuoteService(cfg_sources("public"), lambda: [])
    try:
        assert only_hx.should_request() is False          # 没 Key 且没有别的来源
        assert only_public.should_request() is True       # 免 Key 兜底源：不用配任何 Key
        assert both.should_request() is True              # 同花顺没 Key，公开源接管
        assert em_only.should_request() is True           # 只要东方财富也行
        assert none.should_request() is False             # 一个来源都没启用
        # 非交易时段：**当天还没成功取过就补一次**（2026-09-18 用户实报"自选标的/持仓监控里
        # 的市值和换手一直是空的" —— 那两列只有实时快照才给，而原来非交易时段一律不发请求，
        # 于是晚上打开软件时"现价有数（退回本地收盘价）、这两列却是 —"）。
        # 取到当天就收工：价格不会变，整天反复取纯属浪费配额。
        monkeypatch.setattr(intraday, "in_session", lambda now=None: False)
        assert both.should_request() is True, "收盘后当天还没取过 → 补一次"
        both._fetched_day = intraday.now_shanghai().strftime("%Y-%m-%d")
        assert both.should_request() is False, "今天已经取到过 → 不再取"
        both._fetched_day = None
        # 盘中照旧按节奏取（取过也不影响 should_request；限流在 tick 里管）
        monkeypatch.setattr(intraday, "in_session", lambda now=None: True)
        monkeypatch.setattr(state, "is_downloading", lambda: True)
        assert both.should_request() is False
        monkeypatch.setattr(state, "is_downloading", lambda: False)
        assert empty_symbols.should_request() is False
    finally:
        for service in (only_hx, only_public, both, em_only, none, empty_symbols):
            service.stop()


def test_quotes_injected_client_path_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入假客户端的测试通路保留（离线、确定）：形状与生产一致。

    这里**故意一个来源都不启用**（`cfg_sources()` = `[]`）：注入客户端这条路
    是完全离线、绕开来源注册表的，界面测试靠它拿确定的结果。
    """
    from tests.conftest import FakeClient
    from laoa_trader.ui import quotes as q

    client = FakeClient(snapshots=[
        {"ticker": "600001", "thscode": "600001.SH", "last_price": 4.0,
         "price_change_ratio_pct": 3.5, "volume": 1000, "turnover": 4000.0},
    ])
    cfg = cfg_sources()
    assert sources.usable_sources(cfg) == []        # 没有可用来源……
    out = q.fetch_snapshot_prices(cfg, ["600001"], client=client)
    assert out["600001"]["price"] == 4.0            # ……注入客户端照样出数
    assert out["600001"]["pct"] == 3.5
    assert out["600001"]["volume"] == 1000          # 股（同花顺口径不换算）
    assert out["600001"]["source"] == ""


def test_quote_of_shape_is_stable() -> None:
    """`quote_of` 的键是界面依赖的契约（app.py 读 `price` / `pct` / `at`）。"""
    from laoa_trader.ui import quotes as q

    quote = q.quote_of({"symbol": "600519", "last_price": 1258.0, "pct": -1.16,
                        "as_of": 1758000000.0, "source": "eastmoney", "volume": 100})
    assert quote["price"] == quote["last_price"] == 1258.0
    assert quote["at"] == quote["as_of"] == 1758000000.0
    assert quote["source"] == "eastmoney"
    assert quote["pct"] == -1.16
    # 没给 as_of 时用"现在"（缓存新鲜度判断要靠它）
    fresh = q.quote_of({"symbol": "600519", "last_price": 1.0})
    assert abs(fresh["at"] - time.time()) < 5


# ── 字段级补齐：主源给不出来的项（换手率 / 流通市值）──
#
# 为什么必须有这几条：用户实报「新加入的市值和换手，都没有数据」——
# 因为他配了 Key、走的是同花顺主源，而同花顺的 `/a-share/prices/snapshot`
# **根本不返回这两项**（官方文档的"响应字段"表里只有
# last_price/price_change/涨跌幅/开高低/昨收/量/额，见
# `vendor/Financial-API/docs/api/endpoints-prices.md`）。
# 于是这两列在**配了 Key 的情况下**永远是 `—`，而没配 Key（走免 Key 公开源）时反而是好的 ——
# 这种"越想用好反而越少"的错最容易被当成数据源的问题。


def test_hithink_mapping_carries_no_turnover_rate_or_market_cap() -> None:
    """钉住事实本身：同花顺快照的归一结果里**没有**这两项（不是我漏映射）。

    同花顺那个端点回的字段就这么多（官方文档列的十项），所以 `hithink_rows_to_map`
    没有它们不是 bug，而是"这个来源没有这个数"。补齐是 `supplement_map` 的事。
    """
    rows = [{
        "thscode": "600519.SH", "last_price": 1266.98, "prev_price": 1258.0,
        "open_price": 1257.98, "high_price": 1267.6, "low_price": 1254.0,
        "volume": 1_755_400, "turnover": 2_217_338_282.0,
        "price_change_ratio_pct": 0.71,
    }]
    mapped = sources.hithink_rows_to_map(rows)
    assert mapped["600519"]["last_price"] == 1266.98
    assert mapped["600519"].get("turnover_rate") is None
    assert mapped["600519"].get("circ_mktcap") is None


def test_supplement_fills_the_two_fields_from_the_next_source(monkeypatch) -> None:
    """主源（同花顺）缺这两项 → **从后面的来源按字段补上**，并标出是谁给的。

    这条是用户那条实报的回归：价格仍由同花顺给（它最稳），缺的两项由公开源补 ——
    两边的出处都要留下（`field_source`），tooltip 才能说清"价格谁给的、这两项谁给的"。
    """
    cfg = cfg_sources("hithink", "public")
    cfg.hithink_api_key = "test-key"
    quotes = {
        "600519": {**{name: None for name in sources.QUOTE_FIELDS},
                   "symbol": "600519", "last_price": 1266.98, "pct": 0.71,
                   "source": "hithink", "as_of": 1.0},
        "000001": {**{name: None for name in sources.QUOTE_FIELDS},
                   "symbol": "000001", "last_price": 11.61, "pct": -0.77,
                   "source": "hithink", "as_of": 1.0},
    }
    calls: list[tuple[str, list[str]]] = []

    def fake_public(_cfg, symbols):
        calls.append(("public", list(symbols)))
        return {
            "600519": {"turnover_rate": 0.42, "circ_mktcap": 15838.28},
            "000001": {"turnover_rate": 0.55, "circ_mktcap": 2253.9},
        }

    monkeypatch.setitem(sources._SNAPSHOT_FETCHERS, "public", fake_public)
    monkeypatch.setitem(sources._SNAPSHOT_FETCHERS, "hithink",
                        lambda _cfg, _symbols: pytest.fail("不该再打主源一次"))

    sources.supplement_map(cfg, quotes, ["600519", "000001"])

    assert quotes["600519"]["circ_mktcap"] == 15838.28
    assert quotes["600519"]["turnover_rate"] == 0.42
    assert quotes["000001"]["turnover_rate"] == 0.55
    # 出处：价格还是同花顺，这两项是公开源
    assert quotes["600519"]["source"] == "hithink"
    assert quotes["600519"]["field_source"] == {
        "turnover_rate": "public", "circ_mktcap": "public"}
    # 只发一趟、只问缺的那些票（不重复问主源、不整表重来）
    assert calls == [("public", ["600519", "000001"])]


def test_supplement_is_not_called_when_nothing_is_missing(monkeypatch) -> None:
    """主源本来就给全了（例如免 Key 公开源）→ **一次请求都不发**。"""
    cfg = cfg_sources("public")
    quotes = {"600519": {"symbol": "600519", "last_price": 1266.98,
                         "turnover_rate": 0.42, "circ_mktcap": 15838.28,
                         "source": "public", "as_of": 1.0}}
    monkeypatch.setitem(sources._SNAPSHOT_FETCHERS, "public",
                        lambda _cfg, _symbols: pytest.fail("不该发请求"))
    sources.supplement_map(cfg, quotes, ["600519"])
    assert quotes["600519"]["turnover_rate"] == 0.42
    assert "field_source" not in quotes["600519"]


def test_supplement_does_not_invent_a_quote_for_unknown_symbols(monkeypatch) -> None:
    """补字段**不许**给"没有报价"的票凭空造一条记录（那会变成"有市值没价格"的幽灵行）。"""
    cfg = cfg_sources("hithink", "public")
    cfg.hithink_api_key = "test-key"
    quotes = {"600519": {"symbol": "600519", "last_price": 1266.98,
                         "source": "hithink", "as_of": 1.0}}
    monkeypatch.setitem(sources._SNAPSHOT_FETCHERS, "public",
                        lambda _cfg, _symbols: {
                            "600519": {"turnover_rate": 0.42, "circ_mktcap": 15838.28},
                            "000001": {"turnover_rate": 0.55, "circ_mktcap": 2253.9},
                        })
    sources.supplement_map(cfg, quotes, ["600519"])
    assert set(quotes) == {"600519"}


def test_supplement_stays_quiet_when_the_second_source_fails(monkeypatch) -> None:
    """后面的来源挂了 → 只留下 `None`（界面画 `—`），**绝不抛、也不编数**。"""
    cfg = cfg_sources("hithink", "public")
    cfg.hithink_api_key = "test-key"
    quotes = {"600519": {"symbol": "600519", "last_price": 1266.98,
                         "source": "hithink", "as_of": 1.0}}

    def boom(_cfg, _symbols):
        raise OSError("network down")

    monkeypatch.setitem(sources._SNAPSHOT_FETCHERS, "public", boom)
    sources.supplement_map(cfg, quotes, ["600519"])
    assert quotes["600519"].get("circ_mktcap") is None


def test_usable_sources_without_the_public_source_leaves_the_fields_empty() -> None:
    """`data_sources` 里只有同花顺（用户明确不要别的源）→ 这两项就是 `None`。

    不偷偷去用用户没启用的来源：他要的是"只走同花顺"，那这两列显示 `—` 是**如实**的
    （tooltip 里也写明"来源不提供这一项"）。
    """
    cfg = cfg_sources("hithink")
    cfg.hithink_api_key = "test-key"
    quotes = {"600519": {"symbol": "600519", "last_price": 1266.98,
                         "source": "hithink", "as_of": 1.0}}
    sources.supplement_map(cfg, quotes, ["600519"])
    assert quotes["600519"].get("turnover_rate") is None
    assert quotes["600519"].get("circ_mktcap") is None
