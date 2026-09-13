"""策略分组与选择：分组定义正确 + enabled_groups/enabled_strategies 真的生效。

覆盖需求「一、策略分成 3 个组，可自选」：
- 分组成员、持有期、权重与 README/表格一致；
- 只给组、只给策略、两个都给（取交集）、两个都空（全选）四种语义；
- 名字认不出来要**明确告警**，而不是静默跑全量（那等于选择失效）。
"""

from __future__ import annotations

import pytest

from laoa_trader import pool
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import groups, rules


# ── 分组定义 ──


def test_groups_membership_matches_spec() -> None:
    """三组成员必须与需求表格一致（组 key / 持有期 / 成员）。"""
    assert groups.GROUP_ORDER == ("ultra", "short", "swing")
    assert groups.GROUPS["ultra"].label == "超短·隔日"
    assert groups.GROUPS["ultra"].horizon == 2
    assert groups.GROUPS["ultra"].strategies == ("LadderPullbackStrategy",)

    assert groups.GROUPS["short"].label == "短线·T+3"
    assert groups.GROUPS["short"].horizon == 3
    assert set(groups.GROUPS["short"].strategies) == {
        "ReversalStrategy", "DryUpExpansionStrategy", "FirstLimitUpStrategy",
    }

    assert groups.GROUPS["swing"].label == "波段·T+10"
    assert groups.GROUPS["swing"].horizon == 10
    assert groups.GROUPS["swing"].strategies == ("LowPriceStrategy",)


def test_group_weights_match_server() -> None:
    """池子权重沿用 10 年样本定的那套，且与服务器版 POOL_STRATEGIES 一致。"""
    assert groups.STRATEGY_WEIGHTS == {
        "LowPriceStrategy": 3,
        "LadderPullbackStrategy": 2,
        "ReversalStrategy": 2,
        "DryUpExpansionStrategy": 2,
        "FirstLimitUpStrategy": 1,
    }
    # pool 里的权重表由 groups 派生（单一来源，不会漂移）
    assert pool.POOL_STRATEGIES == groups.STRATEGY_WEIGHTS


def test_every_strategy_belongs_to_exactly_one_group() -> None:
    """5 条策略都要归组，且不重复归组（漏掉/重复都会让"自选"出现幽灵行为）。"""
    grouped = [n for g in groups.GROUPS.values() for n in g.strategies]
    assert sorted(grouped) == sorted(rules.STRATEGIES)
    assert len(grouped) == len(set(grouped))
    for name in rules.STRATEGIES:
        assert groups.group_of(name) is not None
    assert groups.group_of("NotAStrategy") is None


def test_group_helpers() -> None:
    assert groups.group_label("ultra") == "超短·隔日"
    assert groups.group_label("unknown") == "unknown"
    assert groups.group_horizon("swing") == 10
    assert groups.group_horizon("unknown") == 0
    assert groups.group_keys() == ["ultra", "short", "swing"]
    assert groups.all_strategies()[0] == "LadderPullbackStrategy"
    assert len(groups.all_strategies()) == 5


def test_describe_groups_mentions_every_group() -> None:
    text = "\n".join(groups.describe_groups())
    for key in groups.GROUP_ORDER:
        assert key in text
    assert "T+2" in text and "T+3" in text and "T+10" in text


# ── 选择解析 ──


def test_both_empty_means_all() -> None:
    """两组都空 = 全选（安全默认：配置没写也不会一条都不跑）。"""
    selection = groups.resolve([], [])
    assert selection.default_all is True
    assert selection.empty is False
    assert set(selection.strategies) == set(rules.STRATEGIES)
    assert selection.groups == groups.GROUP_ORDER


def test_none_like_values_mean_all() -> None:
    """`resolve(None, None)` 与空列表等价（配置缺键时的路径）。"""
    assert groups.resolve(None, None).strategies == groups.resolve([], []).strategies


def test_groups_only() -> None:
    selection = groups.resolve(["ultra"], [])
    assert selection.strategies == ("LadderPullbackStrategy",)
    assert selection.groups == ("ultra",)
    assert selection.default_all is False


def test_multiple_groups_keeps_group_order() -> None:
    selection = groups.resolve(["swing", "short"], [])
    # 顺序按组定义顺序（超短→短线→波段），与传入顺序无关
    assert selection.strategies == (
        "ReversalStrategy", "DryUpExpansionStrategy", "FirstLimitUpStrategy",
        "LowPriceStrategy",
    )
    assert selection.groups == ("short", "swing")


def test_strategies_only_accepts_chinese_and_class_names() -> None:
    """`--strategies 低价股,连板回踩低吸` 与类名写法等价。"""
    by_label = groups.resolve([], ["低价股", "连板回踩低吸"])
    by_class = groups.resolve([], ["LowPriceStrategy", "LadderPullbackStrategy"])
    assert by_label.strategies == by_class.strategies
    assert set(by_label.strategies) == {"LowPriceStrategy", "LadderPullbackStrategy"}
    # 只给策略时，组按策略自动推断（界面要显示组别）
    assert set(by_label.groups) == {"swing", "ultra"}


def test_groups_and_strategies_intersect() -> None:
    """两个都给：组定范围、策略定取舍。"""
    selection = groups.resolve(["short"], ["短期反转"])
    assert selection.strategies == ("ReversalStrategy",)
    # 不在启用组里的策略被忽略，并且**明确告警**
    selection2 = groups.resolve(["short"], ["低价股"])
    assert selection2.strategies == ()
    assert selection2.empty is True
    assert any("不在启用的组" in w for w in selection2.warnings)


def test_unknown_names_warn_and_do_not_fall_back_to_all() -> None:
    """名字全拼错时：告警 + 空选择（**绝不**悄悄退回"全跑"）。"""
    selection = groups.resolve(["ultraa"], ["低价股X"])
    assert selection.empty is True
    assert len(selection.warnings) == 2
    assert any("未知策略组" in w for w in selection.warnings)
    assert any("未知策略" in w for w in selection.warnings)


def test_partially_unknown_group_still_runs_known_one() -> None:
    selection = groups.resolve(["ultra", "typo"], [])
    assert selection.strategies == ("LadderPullbackStrategy",)
    assert any("未知策略组" in w for w in selection.warnings)


def test_group_key_case_insensitive_and_trimmed() -> None:
    selection = groups.resolve([" ULTRA ", ""], [])
    assert selection.strategies == ("LadderPullbackStrategy",)
    assert selection.warnings == []


def test_selection_helpers() -> None:
    selection = groups.resolve(["swing"], [])
    assert selection.includes("LowPriceStrategy") is True
    assert selection.includes("ReversalStrategy") is False
    assert selection.label_of("LowPriceStrategy") == "波段·T+10"
    assert selection.label_of("Nope") == "—"
    assert "波段·T+10" in selection.describe()
    assert selection.as_dict()["groups"] == ["swing"]


def test_weights_for_respects_selection() -> None:
    assert groups.weights_for(groups.resolve(["ultra"], [])) == {"LadderPullbackStrategy": 2}
    assert groups.weights_for(None) == groups.STRATEGY_WEIGHTS
    assert groups.weights_for(groups.resolve(["short", "swing"], [])) == {
        "ReversalStrategy": 2, "DryUpExpansionStrategy": 2,
        "FirstLimitUpStrategy": 1, "LowPriceStrategy": 3,
    }


def test_iter_selected_follows_group_order() -> None:
    selection = groups.resolve(["swing", "ultra"], [])
    assert list(groups.iter_selected(selection)) == [
        "LadderPullbackStrategy", "LowPriceStrategy",
    ]


def test_resolve_from_config(cfg) -> None:
    cfg.enabled_groups = ["ultra"]
    cfg.enabled_strategies = []
    assert groups.resolve_from_config(cfg).strategies == ("LadderPullbackStrategy",)
    cfg.enabled_groups = []
    cfg.enabled_strategies = ["首板缩量整理"]
    assert groups.resolve_from_config(cfg).strategies == ("FirstLimitUpStrategy",)
    cfg.enabled_groups = []
    cfg.enabled_strategies = []
    assert groups.resolve_from_config(cfg).default_all is True


# ── 策略层：只跑选中的策略 ──


@pytest.fixture()
def full_db(cfg):
    """造一份"每条策略都有候选"的行情（沿用 test_strategies 的思路，精简版）。"""
    from datetime import datetime, timedelta

    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()
    n = len(days)

    def bars(symbol, closes, volumes, opens=None):
        out = []
        for i, day in enumerate(days):
            close = float(closes[i])
            volume = float(volumes[i])
            open_ = float(opens[i]) if opens else close
            out.append((symbol, day, open_, max(open_, close), min(open_, close), close,
                        volume, volume * close))
        return out

    rows: list[tuple] = []
    rows += bars("600001", [3.0] * n, [2e7] * n)                       # 低价股
    rows += bars("600003", [20.0 * (0.988 ** i) for i in range(n)], [2e7] * n)   # 反转
    rows += bars("600002", [100.0] * n, [2e7] * n)                     # 对照
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "低价样本", "银行"),
            ("600002", "高价样本", "白酒"),
            ("600003", "反转样本", "半导体"),
        ])
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
    return str(cfg.db_path)


def test_run_all_only_runs_selected_strategies(full_db) -> None:
    engine = DataEngine(full_db)
    picks, errors = rules.run_all(engine, selection=groups.resolve(["swing"], []))
    assert errors == []
    assert set(picks) == {"LowPriceStrategy"}
    assert "600001" in [p["symbol"] for p in picks["LowPriceStrategy"]]

    picks_short, _ = rules.run_all(engine, selection=groups.resolve(["short"], []))
    assert set(picks_short) == {
        "ReversalStrategy", "DryUpExpansionStrategy", "FirstLimitUpStrategy",
    }
    assert "600003" in [p["symbol"] for p in picks_short["ReversalStrategy"]]


def test_run_all_without_selection_runs_everything(full_db) -> None:
    picks, _ = rules.run_all(DataEngine(full_db))
    assert set(picks) == set(rules.STRATEGIES)


def test_run_all_selection_order_follows_groups(full_db) -> None:
    picks, _ = rules.run_all(DataEngine(full_db), selection=groups.resolve([], []))
    assert list(picks) == [
        "LadderPullbackStrategy", "ReversalStrategy", "DryUpExpansionStrategy",
        "FirstLimitUpStrategy", "LowPriceStrategy",
    ]


def test_signals_only_contain_selected_strategies(full_db) -> None:
    engine = DataEngine(full_db)
    selection = groups.resolve(["short"], [])
    picks, _ = rules.run_all(engine, selection=selection)
    rules.save_signals(engine, picks, day="2026-09-11")
    with storage.connect(full_db) as conn:
        rows = conn.execute("SELECT DISTINCT strategy FROM signal").fetchall()
    names = {r[0] for r in rows}
    assert names
    assert names <= set(selection.strategies)
    assert "LowPriceStrategy" not in names      # 被禁用的组没有落信号


def test_pool_excludes_disabled_group_symbols(full_db, cfg) -> None:
    """建池只从启用组的候选里合成：禁用 swing 后低价股（600001）不该进池子。"""
    engine = DataEngine(full_db)
    pool_on = pool.build_pool(engine, cfg, hot_only=False, save=False,
                              selection=groups.resolve([], []))
    assert "600001" in [row["symbol"] for row in pool_on]

    pool_off = pool.build_pool(engine, cfg, hot_only=False, save=False,
                               selection=groups.resolve(["short"], []))
    symbols = [row["symbol"] for row in pool_off]
    assert "600001" not in symbols
    assert all(row["strategy"] in set(groups.resolve(["short"], []).strategies)
               for row in pool_off)


def test_pool_filters_injected_picks_by_selection(full_db, cfg) -> None:
    """即使调用方传进来的候选混了未启用策略，也不能进池子（双保险）。"""
    engine = DataEngine(full_db)
    picks = {
        "LowPriceStrategy": [{"symbol": "600001", "name": "甲", "reason": "低价股"}],
        "ReversalStrategy": [{"symbol": "600003", "name": "乙", "reason": "短期反转"}],
    }
    built = pool.build_pool(engine, cfg, hot_only=False, save=False, picks=picks,
                            selection=groups.resolve(["short"], []))
    assert [row["symbol"] for row in built] == ["600003"]


def test_pool_rows_carry_group_label(full_db, cfg) -> None:
    engine = DataEngine(full_db)
    pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11",
                    selection=groups.resolve([], []))
    rows = pool.pool_table_rows(cfg.db_path)
    assert rows
    for row in rows:
        assert row["group"] in groups.GROUP_ORDER
        assert row["group_label"] == groups.group_label(row["group"])
        assert row["horizon"] == groups.group_horizon(row["group"])
    by_symbol = {r["symbol"]: r for r in rows}
    if "600001" in by_symbol:
        assert by_symbol["600001"]["group_label"] == "波段·T+10"
        assert by_symbol["600001"]["horizon"] == 10
