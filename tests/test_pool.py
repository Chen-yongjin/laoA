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
    lines = pool.format_pool_lines([
        {"name": "甲", "symbol": "600001", "strategies": "LowPriceStrategy,ReversalStrategy",
         "reason": "低价股"},
    ])
    assert lines == ["1. 甲（600001）LowPrice,Reversal｜低价股"]


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
