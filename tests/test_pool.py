"""股票池合成与热门行业过滤（移植自服务器版 pool.py，口径必须一致）。"""

from __future__ import annotations

import pytest

from laoa_trader import pool
from laoa_trader.data import storage


# ── 权重与配额 ──


def _picks(*symbols: str, name: str = "") -> list[dict]:
    return [{"symbol": s, "name": name or s, "reason": "测试"} for s in symbols]


def _enable_formulas(monkeypatch, tmp_path, cfg, formulas: dict[str, str]) -> None:
    """把公式目录指到临时目录、写好几条公式、并在配置里**勾上**它们。

    2026-09-18（用户要求）起**候选只来自勾选的公式** —— 5 条写在代码里的 Python 策略
    退出了选股链路，所以"池子里要有票"这件事在测试里也必须走同一条路：
    写公式文件 → 勾上 → 建池。这里用价格条件选票（小库价格是确定的）：

        600001 ≈ 1.85 元、600002 ≈ 12.5 元、600003 ≈ 18.7 元、300001 ≈ 26 元

    所以 `C<3` 只选 600001、`C>12 AND C<13` 只选 600002，断言仍然精确。
    """
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in formulas.items():
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setenv("LAOA_TRADER_FORMULAS", str(folder))
    cfg.enabled_formulas = list(formulas)


def test_weight_of_formula_and_unknown() -> None:
    """权重：公式用 `FORMULA_WEIGHT`，其它名字一律 1。

    2026-09-18 之前这里断言的是"5 条内置策略各自的权重表"（`pool.POOL_STRATEGIES`）；
    那批策略改成随包公式之后，**权重表连同策略引擎一起删掉了** ——
    现在池子里只可能有公式标的，所以"按名字查权重"只剩一条规则（公式 2、其它 1）。
    """
    from laoa_trader.strategy.formula_group import FORMULA_WEIGHT

    assert not hasattr(pool, "POOL_STRATEGIES"), "权重表应该已经删掉"
    assert pool.weight_of("公式·尾盘选股策略") == FORMULA_WEIGHT == 2
    assert pool.weight_of("SomeNewStrategy") == 1
    assert pool.weight_of("") == 1
    assert pool.MAX_PER_STRATEGY == 3
    assert pool.DEFAULT_SIZE == 10


def test_score_is_weight_over_rank() -> None:
    """分数 = 公式权重 × (1/公式内排名)（两条公式权重一样，所以只看排名）。"""
    built = pool.build_pool_from_picks({
        "公式·甲": _picks("A", "B"),
        "公式·乙": _picks("C"),
    })
    scores = {row["symbol"]: row["score"] for row in built}
    assert scores["A"] == pytest.approx(2.0)      # 权重 2 × 1/1
    assert scores["B"] == pytest.approx(1.0)      # 权重 2 × 1/2
    assert scores["C"] == pytest.approx(2.0)      # 权重 2 × 1/1
    assert [row["symbol"] for row in built] == ["A", "C", "B"]   # 按分数降序


def test_max_per_strategy_is_enforced() -> None:
    """同一条公式最多 3 只（避免一条公式占满 10 只）。"""
    built = pool.build_pool_from_picks({
        "公式·甲": _picks("A", "B", "C", "D", "E"),
    })
    assert [row["symbol"] for row in built] == ["A", "B", "C"]


def test_size_limit() -> None:
    built = pool.build_pool_from_picks({
        "公式·甲": _picks("A", "B", "C"),
        "公式·乙": _picks("D", "E", "F"),
        "公式·丙": _picks("G", "H", "I"),
    }, size=5)
    assert len(built) == 5


def test_symbol_picked_by_two_strategies_merges_and_scores_add_up() -> None:
    """多条公式同时选中：分数相加、公式名合并、取第一条作为主来源。"""
    built = pool.build_pool_from_picks({
        "公式·甲": _picks("A"),
        "公式·乙": _picks("A"),
    })
    assert len(built) == 1
    row = built[0]
    assert row["score"] == pytest.approx(4.0)                  # 2 + 2
    assert row["strategies"] == "公式·甲,公式·乙"
    assert row["strategy"] == "公式·甲"


def test_unknown_strategy_gets_weight_one() -> None:
    """未登记的策略按权重 1 处理（不会因为缺配置而丢候选）。"""
    built = pool.build_pool_from_picks({"SomeNewStrategy": _picks("A")})
    assert built[0]["score"] == pytest.approx(1.0)


def test_reason_is_truncated_to_200_chars() -> None:
    long = "很长的理由" * 100
    built = pool.build_pool_from_picks({
        "公式·甲": [{"symbol": "A", "name": "A", "reason": long}],
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
    """标签翻译的三条边界：类名 → 中文、中文 → 原样（幂等）、自定义名只换前缀。

    2026-09-22 起自定义的那条也显示 `策略·`（主人："把公式都改成策略吧"）——
    库里存的仍是 `公式·放量上攻`，**只有显示换前缀**（见 `wording.display_strategy`）。
    """
    assert pool.push_tag({"strategies": "LowPriceStrategy"}) == "低价股"
    # 已经是中文名的老行（`enabled_strategies = ["低价股"]` 那类写法存下来的）
    assert pool.push_tag({"strategies": "低价股"}) == "低价股"
    assert pool.push_tag({"strategies": "低价股,ReversalStrategy"}) == "低价股、短期反转"
    # 用户自己起的名字不能被翻译掉（认不出的名字原样返回），只把内部前缀显示成「策略·」
    assert pool.push_tag({"strategies": "公式·放量上攻"}) == "策略·放量上攻"
    # 名字里真带"公式"两个字的那种：**一个字都不许改**（那是用户起的名）
    assert pool.push_tag({"strategies": "公式·我的公式一"}) == "策略·我的公式一"
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


def test_formula_rows_skip_the_hot_industry_filter(
        engine, cfg, tmp_path, monkeypatch) -> None:
    """公式候选**不过**热门行业收敛（理由里也不会出现"热门行业X"标记）。

    2026-09-18 起候选只可能来自公式，而公式标的从来就豁免这道过滤 ——
    条件是用户自己写明的，再被他没看见的行业过滤删掉一半，表现就是"我勾了公式
    却几乎看不到票"，而且完全猜不到原因。这条把新口径钉住（老口径"策略标的会被
    过滤掉"已经没有对应的代码路径了）。
    """
    # 勾两条公式：600002（半导体，热门）与 600001（银行，**不**热门）
    _enable_formulas(monkeypatch, tmp_path, cfg,
                     {"半导体甲": "C>12 AND C<13", "便宜货": "C<3"})
    # 固定"热门行业"= 半导体（真实数据里只有 4 个行业，取前 12 会把它们全算热门）
    monkeypatch.setattr(
        pool, "hot_industries",
        lambda db_path, **kwargs: {"半导体": {"score": 1.0, "limit_up": 2,
                                            "density": 1.0, "mom": 0.05}},
    )
    built = pool.build_pool(engine, cfg, hot_only=True, save=False)
    assert {row["symbol"] for row in built} == {"600001", "600002"}
    assert all("热门行业" not in row["reason"] for row in built)


def test_build_pool_without_hot_filter_keeps_all(engine, cfg, tmp_path, monkeypatch) -> None:
    """`hot_only=False`：勾上两条公式选出来的票全部进池。"""
    _enable_formulas(monkeypatch, tmp_path, cfg,
                     {"半导体甲": "C>12 AND C<13", "便宜货": "C<3"})
    built = pool.build_pool(engine, cfg, hot_only=False, save=False)
    assert {row["symbol"] for row in built} == {"600001", "600002"}
    assert all(row["strategy"].startswith("公式·") for row in built)


def test_build_pool_saves_and_loads(engine, cfg, tmp_path, monkeypatch) -> None:
    """建池后应落库 `stock_pool`，`load_pool` 能读回来（界面与盘中提醒都靠它）。

    候选来自一条真公式（`C>12 AND C<13` → 600002），所以这条同时验证了
    "勾上的公式 → 候选 → 落库 → 读回"这条完整链路。
    """
    _enable_formulas(monkeypatch, tmp_path, cfg, {"半导体甲": "C>12 AND C<13"})
    built = pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    rows = pool.load_pool(cfg.db_path)
    assert len(built) == 1
    assert [r["symbol"] for r in rows] == ["600002"]
    assert pool.pool_symbols(cfg.db_path) == ["600002"]
    assert pool.pool_changed(cfg.db_path, built, "2026-09-11") is False
    assert pool.pool_changed(cfg.db_path, _picks("600099"), "2026-09-11") is True


def test_pool_table_rows_adds_industry_and_label(
        engine, cfg, tmp_path, monkeypatch) -> None:
    """表格行要带行业与「来源」标签；2026-09-18 起标签是 `<策略前缀><策略名>`。

    2026-09-22（主人要求"把公式都改成策略"）：显示的**前缀是 `策略·`**，
    与内置策略的 `策略·短期反转` 统一；库里存的还是 `公式·半导体甲`。
    """
    _enable_formulas(monkeypatch, tmp_path, cfg, {"半导体甲": "C>12 AND C<13"})
    pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    rows = pool.pool_table_rows(cfg.db_path)
    assert rows[0]["symbol"] == "600002"
    assert rows[0]["industry"] == "半导体"
    assert rows[0]["label"] == "策略·半导体甲"
    # 库里那一份**没有被改写**（显示与存储分家，升级前后不影响历史数据）
    assert rows[0]["strategy"] == "公式·半导体甲"


def test_build_pool_never_runs_the_python_strategies(engine, cfg, tmp_path, monkeypatch) -> None:
    """**那套 Python 策略引擎已经整体删掉**（2026-09-18 用户要求：内置策略改成随包公式）。

    这条用两种方式钉住：
    ① 那些模块**导入不进来**（`strategy.rules` / `strategy.groups` / `strategy.base` /
       `strategy.factors` 全删了）—— 只要有人把它们加回来，这条立刻红；
    ② 建池在"没勾公式"时给出空池、而且**不许炸**（不再有 run_all 这条路）。
    """
    import importlib

    for name in ("laoa_trader.strategy.rules", "laoa_trader.strategy.groups",
                 "laoa_trader.strategy.base", "laoa_trader.strategy.factors"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)

    built = pool.build_pool(engine, cfg, save=False)     # 没勾公式 → 池子为空，但**不许炸**
    assert built == []


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
    """四种来源各是什么文本：`策略·X`（自定义与内置**现在是同一个写法**）/ `自选` / `策略·X+自选`。"""
    builtin = {"strategy": "ReversalStrategy", "strategies": "ReversalStrategy"}
    formula = {"strategy": "公式·放量上攻", "strategies": "公式·放量上攻"}
    manual = {"strategy": "", "strategies": "", "watchlist": True}

    assert pool.source_label(builtin, None) == "策略·短期反转"
    # 第二个内置策略（另一组的）名字也要对：不能拿组名冒充
    assert pool.source_label({"strategy": "LowPriceStrategy",
                              "strategies": "LowPriceStrategy"}, None) == "策略·低价股"
    # 自定义策略：库里的值还是 `公式·放量上攻`，**显示**成 `策略·放量上攻`
    # （2026-09-22 主人要求把"公式"改口成"策略"；见 `wording`）
    assert formula["strategy"] == "公式·放量上攻"          # 数据没被动过
    assert pool.source_label(formula, None) == "策略·放量上攻"
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


def test_source_detail_lines_show_source_and_the_other_formulas() -> None:
    """tooltip 的来源明细：`来源：…` + `同批选中：…`（多条公式同时选中时）。

    2026-09-18 起没有"组别 / 持有期 / 证据标记"这三样了 —— 它们都属于已删掉的
    策略引擎（策略组、`（依赖开盘）` 那套边际证据），公式标的本来也不带它们。
    """
    row = {
        "strategy": "公式·短期反转",
        "strategies": "公式·短期反转,公式·地量后放量变盘",
        "source_label": "公式·短期反转",
    }
    lines = pool.source_detail_lines(row)
    assert lines[0] == "来源：策略·短期反转"          # 这一行给的是算好的 `source_label`
    assert "同批选中：策略·地量后放量变盘" in lines    # 这一行现算（走 `strategy_label`）
    assert not any(line.startswith("组别：") for line in lines)

    # 只有一条公式的行：只出"来源"一行（不留空壳）
    plain = {"strategy": "公式·甲", "strategies": "公式·甲", "source_label": "策略·甲"}
    assert pool.source_detail_lines(plain) == ["来源：策略·甲"]

    # 老库里的内置策略行照旧显示中文名（历史数据的显示口径）
    old = {"strategy": "ReversalStrategy", "strategies": "ReversalStrategy",
           "source_label": "策略·短期反转"}
    assert pool.source_detail_lines(old) == ["来源：策略·短期反转"]


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
    from laoa_trader import legacy

    for name in pool.strategy_names(row):
        assert name in (legacy.strategy_label("ReversalStrategy"),
                        legacy.strategy_label("DryUpExpansionStrategy"))


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


def _saved_pool_with_one_pick(engine, cfg, tmp_path, monkeypatch) -> None:
    """建一个只有一只**公式标的**的池子并落库（600002 半导体甲）。

    2026-09-18 起候选只来自勾选的公式：这里勾一条 `C>12 AND C<13`
    （小库里只有 600002 落在 12~13 元之间），所以池子里就是它。
    """
    _enable_formulas(monkeypatch, tmp_path, cfg, {"半导体甲": "C>12 AND C<13"})
    built = pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")
    assert [row["symbol"] for row in built] == ["600002"]


def test_a_picked_symbol_can_be_deleted_from_the_pool_page(
        engine, cfg, tmp_path, monkeypatch) -> None:
    """右键【删除】的后端：`delete_pool_symbol` 之后「自选股池」不再显示这只票。"""
    _saved_pool_with_one_pick(engine, cfg, tmp_path, monkeypatch)
    assert [row["symbol"] for row in pool.pool_page_rows(cfg.db_path)] == ["600002"]

    with storage.connect(cfg.db_path) as conn:
        removed = storage.delete_pool_symbol(conn, "600002")

    assert removed == 1                                   # 真的删掉了一行
    assert pool.pool_page_rows(cfg.db_path) == []         # 池子页也随之空了
    assert pool.pool_symbols(cfg.db_path) == []


def test_a_manually_added_symbol_comes_back_into_the_pool_page(engine, cfg, tmp_path,
                                                               monkeypatch) -> None:
    """手工再加：`upsert_watchlist` 之后那只票**立刻**能在这张表里看见。

    为什么这条重要：池子按行情日重算，删掉一只策略标的之后用户想自己把它加回来
    （或加一只完全手工挑的票），界面上必须马上就有一行 —— 而不是"等今晚重新建池"。
    """
    _saved_pool_with_one_pick(engine, cfg, tmp_path, monkeypatch)
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


# ── 「选股候选只来自公式」这条口径的兜底（2026-09-18 从 test_groups.py 搬过来）──
#
# 原来这三条住在 `tests/test_groups.py`（那个文件随策略组机制一起删了）。
# 它们的断言对象其实是 **pool.py 的行为**，与"策略组"无关，所以搬到这里继续守着。


def test_selection_argument_does_not_filter_candidates(engine, cfg, monkeypatch) -> None:
    """`selection` 参数**已经不起作用**：池子只看勾了哪些公式。

    2026-09-18 之前"禁用 swing 组 → 低价股的标的被剔出池子"；策略组机制删掉之后，
    这个参数只剩签名兼容（老调用方可能还在传），传什么都不能影响结果。
    """
    from laoa_trader.strategy import formula_group

    run = formula_group.FormulaRun()
    run.picks = {formula_group.formula_strategy_name("测试公式"): [
        {"symbol": "600001", "name": "低价样本", "reason": "测试"},
    ]}
    run.ran = ["测试公式"]
    monkeypatch.setattr(formula_group, "run_enabled_formulas", lambda *a, **k: run)

    for selection in (None, object()):
        built = pool.build_pool(engine, cfg, hot_only=False, save=False,
                                selection=selection)
        assert [row["symbol"] for row in built] == ["600001"]
        assert built[0]["strategy"] == "公式·测试公式"


def test_pool_drops_candidates_that_are_not_formulas(engine, cfg) -> None:
    """调用方传进来的**非公式候选**（老策略类名）一律丢掉。

    那 5 条内置策略已经退出选股链路，界面上再也选不出它们 —— 让这种键进池子
    只会让「来源」列出现一个用户找不到对应行的"策略·X"。公式候选照常保留。
    """
    picks = {
        "LowPriceStrategy": [{"symbol": "600001", "name": "甲", "reason": "低价股"}],
        "公式·我的公式": [{"symbol": "600003", "name": "乙", "reason": "短期反转"}],
    }
    built = pool.build_pool(engine, cfg, hot_only=False, save=False, picks=picks)

    assert [row["symbol"] for row in built] == ["600003"]
    assert built[0]["strategy"] == "公式·我的公式"


def test_pool_rows_carry_no_group(engine, cfg, tmp_path, monkeypatch) -> None:
    """表格行**没有组别/持有期**（`group_label` 是「—」、`horizon` 是 0）。

    "组别 / T+N" 是已删掉的策略组机制的概念（公式没有组）——
    这两栏如实写"没有"，而不是借一个相近的组名糊上去。
    """
    _enable_formulas(monkeypatch, tmp_path, cfg, {"半导体甲": "C>12 AND C<13"})
    pool.build_pool(engine, cfg, hot_only=False, save=True, day="2026-09-11")

    rows = pool.pool_table_rows(cfg.db_path)
    assert rows
    for row in rows:
        assert row["group"] == ""
        assert row["group_label"] == "—"
        assert row["horizon"] == 0
        # 「来源」列照旧回答问题："是哪条策略选出来的"（显示成策略前缀）
        assert row["source_label"] == "策略·半导体甲"
