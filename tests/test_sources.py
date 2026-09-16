"""来源注册表：能力矩阵、启停与顺序、未知 id、没 Key 的回退、统一口径的归一化。

外加一小节"接线测试"（`ui/quotes.py` 那四条门槛里最要紧的一条）：
**没填同花顺 Key、但启用了东方财富时，两张表的现价/涨幅照样要能取到**。

全部用例都不联网：来源取数函数被换成假函数，socket 层还有 conftest 的
`_block_network` 兜底（真外呼会直接失败，而不是悄悄打接口）。
"""

from __future__ import annotations

import logging
import time

import pytest

from laoa_trader.config import Config
from laoa_trader.data import eastmoney as em
from laoa_trader.data import hithink as hx
from laoa_trader.data import sources


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


def test_registry_has_both_builtin_sources() -> None:
    assert set(sources.REGISTRY) == {"hithink", "eastmoney"}
    hx_info = sources.REGISTRY["hithink"]
    em_info = sources.REGISTRY["eastmoney"]
    assert hx_info.needs_key is True and hx_info.key_config == "hithink_api_key"
    assert em_info.needs_key is False and em_info.key_config is None
    assert hx_info.name == "同花顺金融数据服务（内置）"
    assert "免 Key" in em_info.name


def test_capabilities_only_use_known_values() -> None:
    for info in sources.REGISTRY.values():
        assert info.capabilities <= set(sources.CAPABILITIES)
        assert info.capabilities, f"{info.id} 一个能力都没声明"


def test_eastmoney_does_not_claim_what_it_cannot_do() -> None:
    """不许假装能力：东方财富这里**没有**涨停池 / 复权因子 / 交易日历。"""
    em_info = sources.REGISTRY["eastmoney"]
    assert em_info.capabilities == frozenset({
        sources.CAP_SNAPSHOT, sources.CAP_DAILY_HISTORY, sources.CAP_STOCK_LIST,
    })
    for word in ("涨停池", "复权因子", "交易日历"):
        assert word in em_info.note            # 风险与短板写在明面上
    assert "未文档化" in em_info.note
    # 数值口径也如实写着（它们是实测出来的关键事实）
    assert "手" in em_info.note or "股" in em_info.note


def test_capability_labels_are_chinese_and_complete() -> None:
    assert sources.CAPABILITY_LABELS == {
        sources.CAP_SNAPSHOT: "实时快照",
        sources.CAP_DAILY_HISTORY: "历史日K",
        sources.CAP_STOCK_LIST: "股票代码表",
    }


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
    """界面要能**添加来源**，所以没启用的也要列出来（这是与 active_sources 的关键差别）。"""
    states = sources.source_states(cfg_sources("eastmoney"))
    assert [row["id"] for row in states] == ["eastmoney", "hithink"]
    assert states[0]["enabled"] is True and states[1]["enabled"] is False
    assert set(states[0]) == {
        "id", "name", "enabled", "needs_key", "has_key",
        "capabilities_text", "note", "key_config", "capabilities",
    }


def test_source_states_default_order_and_texts() -> None:
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
    assert states["hithink"]["note"]


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
    assert sources.snapshot_map(cfg_sources("hithink"), ["600519"]) == {}       # 没 Key
    assert sources.snapshot_map(cfg_sources(), ["600519"]) == {}                # 一个都没启用
    assert calls == []                                                          # 谁都没被叫


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


# ── 接线：`ui/quotes.py`（用户拍板的那条行为变化）──


def test_quotes_uses_eastmoney_when_hithink_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**主目标**：没填同花顺 Key、启用了东方财富 → 两张表照样有现价/涨幅。"""
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(em, "snapshot", lambda symbols, **k: [
        unified("600519", last_price=1258.0, pct=-1.16, volume=2623500,
                turnover=3307926407.0),
    ])
    cfg = cfg_sources("hithink", "eastmoney")
    out = q.fetch_snapshot_prices(cfg, ["600519"])
    assert set(out) == {"600519"}
    quote = out["600519"]
    # 界面（`app._price_cells`）读的就是这三个键
    assert quote["price"] == 1258.0 and quote["pct"] == -1.16
    assert isinstance(quote["at"], float) and quote["at"] > 0
    # 统一口径的字段一并带上（来源与取数时刻要能显示出来）
    assert quote["last_price"] == 1258.0 and quote["source"] == "eastmoney"
    assert quote["volume"] == 2623500


def test_quotes_sends_nothing_without_a_usable_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有可用来源 → 一次请求都不发、返回空（表格退回本地收盘价并标注）。"""
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    entered: list[str] = []
    # 盯**两层**：`quotes` 自己的门槛（连 `sources.snapshot_map` 都不进）
    # 与来源层的取数函数（`em.snapshot` 一次都不被叫）——
    # 只看其中一层的话，另一层被删掉是看不出来的
    monkeypatch.setattr(sources, "snapshot_map",
                        lambda *a, **k: entered.append("snapshot_map") or {})
    monkeypatch.setattr(em, "snapshot", lambda *a, **k: entered.append("em") or [])
    monkeypatch.setattr(em, "snapshot_all", lambda *a, **k: entered.append("em-all") or [])
    assert q.fetch_snapshot_prices(cfg_sources("hithink"), ["600519"]) == {}
    assert q.fetch_snapshot_prices(cfg_sources(), ["600519"]) == {}
    assert entered == []


def test_quotes_service_gate_follows_usable_sources(
    monkeypatch: pytest.MonkeyPatch, qapp
) -> None:
    """那四条门槛里"没有可用来源就不取"：没 Key 的 `["hithink"]` 不发，
    加了东方财富就发（交易时段内）。"""
    from laoa_trader import intraday, state
    from laoa_trader.ui import quotes as q

    monkeypatch.setattr(hx, "available", lambda: False)
    monkeypatch.setattr(state, "is_downloading", lambda: False)
    monkeypatch.setattr(intraday, "in_session", lambda now=None: True)
    provider = lambda: ["600519"]        # noqa: E731 - 一行的小闭包
    only_hx = q.QuoteService(cfg_sources("hithink"), provider)
    both = q.QuoteService(cfg_sources("hithink", "eastmoney"), provider)
    no_source = q.QuoteService(cfg_sources("eastmoney"), provider)
    try:
        assert only_hx.should_request() is False          # 没 Key 且没有别的来源
        assert both.should_request() is True              # 东方财富兜底
        assert no_source.should_request() is True         # 只要东方财富也行
        # 非交易时段 / 下载中 / 没有标的，三条门槛照旧
        monkeypatch.setattr(intraday, "in_session", lambda now=None: False)
        assert both.should_request() is False
        monkeypatch.setattr(intraday, "in_session", lambda now=None: True)
        monkeypatch.setattr(state, "is_downloading", lambda: True)
        assert both.should_request() is False
        monkeypatch.setattr(state, "is_downloading", lambda: False)
        empty = q.QuoteService(cfg_sources("eastmoney"), lambda: [])
        assert empty.should_request() is False
    finally:
        for service in (only_hx, both, no_source):
            service.stop()


def test_quotes_injected_client_path_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入假客户端的测试通路保留（离线、确定）：形状与生产一致。"""
    from tests.conftest import FakeClient
    from laoa_trader.ui import quotes as q

    client = FakeClient(snapshots=[
        {"ticker": "600001", "thscode": "600001.SH", "last_price": 4.0,
         "price_change_ratio_pct": 3.5, "volume": 1000, "turnover": 4000.0},
    ])
    out = q.fetch_snapshot_prices(cfg_sources(), ["600001"], client=client)
    assert out["600001"]["price"] == 4.0
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
