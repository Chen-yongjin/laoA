"""股票池合成与热门行业过滤（移植自服务器版 pool.py，口径必须一致）。"""

from __future__ import annotations

import pytest

from laoa_trader import pool
from laoa_trader.data import storage


# ── 权重与配额 ──


def _picks(*symbols: str, name: str = "") -> list[dict]:
    return [{"symbol": s, "name": name or s, "reason": "测试"} for s in symbols]


def test_weights_match_server() -> None:
    """权重表必须与服务器版一致（不是拍脑袋，按 10 年样本 t 值定的）。"""
    assert pool.POOL_STRATEGIES == {
        "LowPriceStrategy": 3,
        "LadderPullbackStrategy": 2,
        "ReversalStrategy": 2,
        "DryUpExpansionStrategy": 2,
        "FirstLimitUpStrategy": 1,
    }
    assert pool.MAX_PER_STRATEGY == 3
    assert pool.DEFAULT_SIZE == 10


def test_score_is_weight_over_rank() -> None:
    """分数 = 策略权重 × (1/策略内排名)，与服务器版同款公式。"""
    built = pool.build_pool_from_picks({
        "LowPriceStrategy": _picks("A", "B"),
        "ReversalStrategy": _picks("C"),
    })
    scores = {row["symbol"]: row["score"] for row in built}
    assert scores["A"] == pytest.approx(3.0)      # 权重 3 × 1/1
    assert scores["B"] == pytest.approx(1.5)      # 权重 3 × 1/2
    assert scores["C"] == pytest.approx(2.0)      # 权重 2 × 1/1
    assert [row["symbol"] for row in built] == ["A", "C", "B"]   # 按分数降序


def test_max_per_strategy_is_enforced() -> None:
    """同一策略最多 3 只（避免一个策略占满 10 只）。"""
    built = pool.build_pool_from_picks({
        "LowPriceStrategy": _picks("A", "B", "C", "D", "E"),
    })
    assert [row["symbol"] for row in built] == ["A", "B", "C"]


def test_size_limit() -> None:
    built = pool.build_pool_from_picks({
        "LowPriceStrategy": _picks("A", "B", "C"),
        "ReversalStrategy": _picks("D", "E", "F"),
        "FirstLimitUpStrategy": _picks("G", "H", "I"),
    }, size=5)
    assert len(built) == 5


def test_symbol_picked_by_two_strategies_merges_and_scores_add_up() -> None:
    """多策略同时选中：分数相加、策略名合并、取第一个策略作为主策略。"""
    built = pool.build_pool_from_picks({
        "LowPriceStrategy": _picks("A"),
        "FirstLimitUpStrategy": _picks("A"),
    })
    assert len(built) == 1
    row = built[0]
    assert row["score"] == pytest.approx(4.0)                  # 3 + 1
    assert row["strategies"] == "LowPriceStrategy,FirstLimitUpStrategy"
    assert row["strategy"] == "LowPriceStrategy"


def test_unknown_strategy_gets_weight_one() -> None:
    """未登记的策略按权重 1 处理（不会因为缺配置而丢候选）。"""
    built = pool.build_pool_from_picks({"SomeNewStrategy": _picks("A")})
    assert built[0]["score"] == pytest.approx(1.0)


def test_reason_is_truncated_to_200_chars() -> None:
    long = "很长的理由" * 100
    built = pool.build_pool_from_picks({
        "LowPriceStrategy": [{"symbol": "A", "name": "A", "reason": long}],
    })
    assert len(built[0]["reason"]) <= 200


def test_format_pool_lines() -> None:
    """推送正文的标签是**策略中文名**（不是 `LowPrice` 这样的类名片段）。

    这条断言原来钉的是服务器版口径（`LowPrice,Reversal`）。改口径的理由：
    推送是给手机上的人看的一行字，而界面同一只票写的是「低价股」
    （`strategy_label()`）——一件事两个名字，用户没法拿它对照设置页。
    """
    lines = pool.format_pool_lines([
        {"name": "甲", "symbol": "600001", "strategies": "LowPriceStrategy,ReversalStrategy",
         "reason": "低价股"},
    ])
    assert lines == ["1. 甲(600001)低价股、短期反转｜低价股"]


def test_push_tag_translates_but_keeps_custom_names() -> None:
    """标签翻译的三条边界：类名 → 中文、中文 → 原样（幂等）、`公式·x` → 原样。"""
    assert pool.push_tag({"strategies": "LowPriceStrategy"}) == "低价股"
    # 已经是中文名的老行（`enabled_strategies = ["低价股"]` 那类写法存下来的）
    assert pool.push_tag({"strategies": "低价股"}) == "低价股"
    assert pool.push_tag({"strategies": "低价股,ReversalStrategy"}) == "低价股、短期反转"
    # 用户自己起的公式名不能被翻译掉（认不出的名字原样返回）
    assert pool.push_tag({"strategies": "公式·放量上攻"}) == "公式·放量上攻"
    assert pool.push_tag({"strategies": ""}) == ""
    assert pool.push_tag({}) == ""


# ── 热门行业 ──


def test_hot_industries_ranks_by_limit_up_density(db) -> None:
    """当日涨停密度 + 近 5 日成分等权涨幅，归一化后取前 12 —— 半导体涨停最多应排第一。"""
    hot = pool.hot_industries(db)
    assert hot, "应该有热门行业"
    assert list(hot)[0] == "半导体"
    assert hot["半导体"]["limit_up"] == 2
    assert hot["半导体"]["score"] > hot["银行"]["score"] >= 0
    assert len(hot) <= 12
    # 房地产没有涨停、走势也不强，分数应最低
    assert hot["半导体"]["score"] >= hot["房地产"]["score"]


def test_hot_industries_empty_without_limit_up(db) -> None:
    """没有涨停池数据时返回空（调用方据此不做过滤）。"""
    from laoa_trader.data import storage

    with storage.connect(db) as conn:
        conn.execute("DELETE FROM limit_up_pool")
        conn.commit()
    assert pool.hot_industries(db) == {}


def test_hot_industries_respects_day(db) -> None:
    assert pool.hot_industries(db, day="2026-01-01") != {}   # 指定日期时不再查 MAX(date)
    # 该日没有涨停记录 → 分数只由动量决定，但仍会返回行业
    hot = pool.hot_industries(db, day="2026-01-01")
    assert all(v["limit_up"] == 0 for v in hot.values())


def test_build_pool_filters_non_hot_industries(engine, cfg, monkeypatch) -> None:
    """只保留热门行业的候选，并给理由打上"热门行业X"标记。"""
    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        return {
            "LowPriceStrategy": _picks("600002", "600001"),      # 半导体（热门）、银行
        }, []

    # 固定"热门行业"= 半导体（真实数据里只有 4 个行业，取前 12 会把它们全算热门）
    monkeypatch.setattr(
        pool, "hot_industries",
        lambda db_path, **kwargs: {"半导体": {"score": 1.0, "limit_up": 2,
                                            "density": 1.0, "mom": 0.05}},
    )
    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    built = pool.build_pool(engine, cfg, hot_only=True, save=False)
    assert [row["symbol"] for row in built] == ["600002"]
    assert "热门行业半导体" in built[0]["reason"]


def test_build_pool_without_hot_filter_keeps_all(engine, cfg, monkeypatch) -> None:
    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        return {"LowPriceStrategy": _picks("600002", "600001")}, []

    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    built = pool.build_pool(engine, cfg, hot_only=False, save=False)
    assert {row["symbol"] for row in built} == {"600002", "600001"}


def test_build_pool_saves_and_loads(engine, cfg, monkeypatch) -> None:
    """建池后应落库 `stock_pool`，`load_pool` 能读回来（界面与盘中提醒都靠它）。"""
    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        return {"LowPriceStrategy": _picks("600002")}, []

    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    built = pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    rows = pool.load_pool(cfg.db_path)
    assert len(built) == 1
    assert [r["symbol"] for r in rows] == ["600002"]
    assert pool.pool_symbols(cfg.db_path) == ["600002"]
    assert pool.pool_changed(cfg.db_path, built, "2026-09-11") is False
    assert pool.pool_changed(cfg.db_path, _picks("600099"), "2026-09-11") is True


def test_pool_table_rows_adds_industry_and_label(engine, cfg, monkeypatch) -> None:
    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        return {"LowPriceStrategy": _picks("600002", name="半导体甲")}, []

    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    rows = pool.pool_table_rows(cfg.db_path)
    assert rows[0]["industry"] == "半导体"
    assert rows[0]["label"] == "低价股"


def test_build_pool_bumps_top_n_for_wide_candidates(engine, cfg, monkeypatch) -> None:
    """建池时把候选放宽到 200（叠加热门行业过滤后还能剩够 10 只）。"""
    seen: dict = {}

    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        seen["top_n"] = top_n
        return {}, []

    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    pool.build_pool(engine, cfg, save=False)
    assert seen["top_n"] == 200


def test_save_pool_is_idempotent(db) -> None:
    rows = pool.build_pool_from_picks({"LowPriceStrategy": _picks("600002")})
    pool.save_pool(db, rows, "2026-09-11")
    pool.save_pool(db, rows, "2026-09-11")
    with storage.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0] == 1


# ── 「来源」列：**哪条策略**选出来的（不是组别）──
#
# 用户给定的口径（`docs/改版方案.md` 第八节第 5 条）：这一列要回答"是哪条策略"，
# 而不是"哪一组"；组别/持有期 + 其余策略名 + 证据标记全进行 tooltip（列数被定死成 6 列）。


def test_source_label_names_the_strategy_not_the_group() -> None:
    """四种来源各是什么文本：`策略·X` / `公式·X` / `自选` / `策略·X+自选`。"""
    builtin = {"strategy": "ReversalStrategy", "strategies": "ReversalStrategy"}
    formula = {"strategy": "公式·放量上攻", "strategies": "公式·放量上攻"}
    manual = {"strategy": "", "strategies": "", "watchlist": True}

    assert pool.source_label(builtin, None) == "策略·短期反转"
    # 第二个内置策略（另一组的）名字也要对：不能拿组名冒充
    assert pool.source_label({"strategy": "LowPriceStrategy",
                              "strategies": "LowPriceStrategy"}, None) == "策略·低价股"
    # 公式保持现状：合成名本身就是"哪条公式"
    assert pool.source_label(formula, None) == "公式·放量上攻"
    assert pool.source_label(manual, None) == "自选"
    # 两者都有 → 后面接 `+自选`（用户给定的写法，没有空格）
    assert pool.source_label(builtin, {"enabled": 1}) == "策略·短期反转+自选"
    # 组别**不再**出现在这一列里
    assert "T+3" not in pool.source_label(builtin, None)
    assert "短线" not in pool.source_label(builtin, None)


def test_strategy_names_falls_back_to_the_primary_column() -> None:
    """老库的行只有 `strategy`、`strategies` 是空的 → 来源与推送标签都不能退化成"自选"。

    `strategies` 是后加的列；缺了它就写"自选"，会把策略标的说成手工加的票 ——
    那是最难查的一类错（用户会以为策略没选到它）。
    """
    row = {"strategy": "ReversalStrategy", "strategies": ""}
    assert pool.strategy_names(row) == ["短期反转"]
    assert pool.push_tag(row) == "短期反转"
    assert pool.source_label(row, None) == "策略·短期反转"
    lines = pool.format_pool_lines([{**row, "name": "甲", "symbol": "600001",
                                     "reason": "短期反转"}])
    assert lines == ["1. 甲(600001)短期反转｜短期反转"]


def test_source_detail_lines_list_group_and_other_strategies() -> None:
    """tooltip 的来源明细：来源（含证据标记）/ 组别（含持有期）/ 同批选中的其它策略。"""
    row = {
        "strategy": "ReversalStrategy",
        "strategies": "ReversalStrategy,DryUpExpansionStrategy",
        "source_label": "策略·短期反转",
        "group_label": "短线·T+3", "horizon": 3,
        "evidence": "open_only", "evidence_text": "（依赖开盘）",
    }
    lines = pool.source_detail_lines(row)
    assert lines[0] == "来源：策略·短期反转（依赖开盘）"
    assert "组别：短线·T+3（T+3）" in lines
    # 主策略之外的那条策略名进 tooltip（列里写不下，但不能丢）
    assert "同批选中：地量后放量变盘" in lines
    # 「依赖开盘」的解释在最后一行，前缀是**主策略**的中文名（不是整串来源文本）
    assert lines[-1].startswith("策略·短期反转：正 α 只存在于")

    # 单策略 + 没有组别的行：只出"来源"一行（不留空壳）
    plain = {"strategy": "VolatilitySqueeze", "strategies": "VolatilitySqueeze",
             "source_label": "策略·VolatilitySqueeze", "group_label": "—", "horizon": 0,
             "evidence": "proven", "evidence_text": ""}
    assert pool.source_detail_lines(plain) == ["来源：策略·VolatilitySqueeze"]


def test_push_line_lists_all_strategies_while_the_column_shows_the_primary() -> None:
    """推送正文列**所有**命中的策略名，「来源」列只写**主策略** —— 两者不是"两套说法"。

    推送是手机上一行、没有 tooltip，所以它保留更详细的那一份（用户明确允许：
    "给推送链路单独保留一个更详细的文本"）；两处的中文名**同一个来源**
    （`strategy_names()` → `rules.strategy_label()`），详细程度不同而已。
    """
    row = {"name": "半导体甲", "symbol": "600002", "strategy": "ReversalStrategy",
           "strategies": "ReversalStrategy,DryUpExpansionStrategy", "reason": "缩量回踩",
           "source_label": "策略·短期反转"}
    assert pool.source_label(row, None) == "策略·短期反转"
    line = pool.format_pool_lines([row])[0]
    assert line == "1. 半导体甲(600002)短期反转、地量后放量变盘｜缩量回踩"
    # 推送标签里的每个中文名，都能在「策略选股」列表里找到（同一份翻译表）
    for name in pool.strategy_names(row):
        assert name in (pool.rules.strategy_label("ReversalStrategy"),
                        pool.rules.strategy_label("DryUpExpansionStrategy"))


# ── 选出来的票在「自选股池」里能删、能手工再加（用户要求）──
#
# 用户原话：「策略选股只要显示策略，不显示选股结果，选股结果直接进自选股池，
# **可以在股池再添加删除**」。
#
# 结果现在不再显示在「策略选股」页，入口收敛到「自选股池」那一张表：
# 右键【删除】走 `storage.delete_pool_symbol()`，手工再加走
# `storage.upsert_watchlist()`（界面上是【添加自选】/回车）。
# 界面那两步的接线在 `ui/app.py`（属于另一个改动方），这里钉住**后端这两个入口**
# 对"策略选中的票"真的有效 —— 界面怎么改都不至于连后端语义都变了。


def _saved_pool_with_one_pick(engine, cfg, monkeypatch) -> None:
    """建一个只有一只策略标的的池子并落库（600002 半导体甲）。"""
    def fake_run_all(engine_, settings=None, *, top_n=None, names=None):
        return {"ReversalStrategy": _picks("600002", name="半导体甲")}, []

    monkeypatch.setattr(pool.rules, "run_all", fake_run_all)
    built = pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    assert [row["symbol"] for row in built] == ["600002"]


def test_a_picked_symbol_can_be_deleted_from_the_pool_page(engine, cfg, monkeypatch) -> None:
    """右键【删除】的后端：`delete_pool_symbol` 之后「自选股池」不再显示这只票。"""
    _saved_pool_with_one_pick(engine, cfg, monkeypatch)
    assert [row["symbol"] for row in pool.pool_page_rows(cfg.db_path)] == ["600002"]

    with storage.connect(cfg.db_path) as conn:
        removed = storage.delete_pool_symbol(conn, "600002")

    assert removed == 1                                   # 真的删掉了一行
    assert pool.pool_page_rows(cfg.db_path) == []         # 池子页也随之空了
    assert pool.pool_symbols(cfg.db_path) == []


def test_a_manually_added_symbol_comes_back_into_the_pool_page(engine, cfg,
                                                               monkeypatch) -> None:
    """手工再加：`upsert_watchlist` 之后那只票**立刻**能在这张表里看见。

    为什么这条重要：池子按行情日重算，删掉一只策略标的之后用户想自己把它加回来
    （或加一只完全手工挑的票），界面上必须马上就有一行 —— 而不是"等今晚重新建池"。
    """
    _saved_pool_with_one_pick(engine, cfg, monkeypatch)
    with storage.connect(cfg.db_path) as conn:
        storage.delete_pool_symbol(conn, "600002")        # 先删掉（上一个用例的动作）
        storage.upsert_watchlist(conn, "600002", name="半导体甲", note="手工加的")

    rows = pool.pool_page_rows(cfg.db_path)

    assert [row["symbol"] for row in rows] == ["600002"]
    assert rows[0]["source_label"] == "自选"               # 来源标成自选（不是策略）
    assert rows[0]["note"] == "手工加的"                   # 用户写的备注留着
    assert rows[0]["strategy"] == ""                      # 不再是"策略选出来的"


def test_deleting_a_symbol_that_is_not_in_the_pool_is_a_no_op(cfg) -> None:
    """删一只不在池子里的票（界面点错、或另一处刚删过）→ 返回 0，不报错。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        assert storage.delete_pool_symbol(conn, "600002") == 0
    assert pool.pool_page_rows(cfg.db_path) == []
