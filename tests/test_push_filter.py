"""推送范围：**池子里有什么就推什么**（那道"只推有边际的策略标的"的过滤已随引擎删除）。

⚠️ 2026-09-18（用户要求）：内置策略改成随包公式之后，`push_only_proven` 这条开关
（以及它背后的"证据 / 依赖开盘"整套标注，`pool.split_push_rows()` / `row_is_proven()` /
`strategy_evidence()` / `evidence_text()` / `OPEN_ONLY_TAG`）**整体删掉了** ——
它过滤的对象是那两条 `open_only` 的 Python 策略（正 α 只在"开盘买"口径下存在），
那两条策略已经不存在了，开关也就没有可过滤的东西。

所以这个文件现在只守三件事：
1. **默认全推**：勾上的公式选出来的票都进池、都进推送；
2. **没推**只可能是"同一天同一批内容已经推过"（幂等），`push_skipped_kind` 只剩 `duplicate`；
3. 那套被删掉的东西**真的没了**（模块里没有这些名字、配置里没有那个键）。
"""

from __future__ import annotations

import pytest

from laoa_trader import pool
from laoa_trader import scheduler
from laoa_trader.config import Config
from laoa_trader.data.engine import DataEngine

A_FORMULA = "短期反转"          # 桩里的三个"公式名"（对应小库里的三只票）
B_FORMULA = "地量后放量变盘"
C_FORMULA = "首板缩量整理"


def test_the_evidence_machinery_is_really_gone() -> None:
    """那套"证据 / 依赖开盘 / 推送过滤"的名字与配置键**一个都不该剩下**。

    留着半个（比如只删实现、留着字段）比全留着更糟：下次有人看到
    `push_only_proven` 会以为它还管用。
    """
    for name in ("split_push_rows", "row_is_proven", "strategy_evidence",
                 "evidence_text", "open_only_tooltip", "skipped_push_note",
                 "OPEN_ONLY_TAG"):
        assert not hasattr(pool, name), f"pool.{name} 应该已经删掉"
    assert not hasattr(Config(), "push_only_proven")
    assert not hasattr(Config(), "enabled_groups")
    assert not hasattr(Config(), "enabled_strategies")


# ── 端到端：推送里有没有它 ──


@pytest.fixture()
def daily_setup(cfg, db, monkeypatch):
    """把 `run_daily` 的外部依赖按住（同步/通知/热门行业）并捕获推送正文。"""
    captured: dict = {}

    def fake_notify_all(title, lines, kinds=None, cfg=None):
        captured["title"] = title
        captured["lines"] = list(lines)
        return {"tray": {"kind": "tray", "ok": True}}

    monkeypatch.setattr(scheduler.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify_all)
    monkeypatch.setattr("laoa_trader.pool.hot_industries", lambda db_path, **k: {})
    return captured


def _fake_formulas(*formula_names: str):
    """让「公式」组按给定名字各返回一只（名字与票一一对应）。

    2026-09-18（用户要求）：候选只来自**勾选的公式** —— `run_daily` 里没有
    "跑内置策略"这一步，所以桩打在 `formula_group.run_enabled_formulas` 上，
    返回的键是合成名 `公式·X`（传老策略类名会被建池当"非公式候选"丢掉）。
    """
    mapping = {A_FORMULA: ("600001", "浦发样本"),
               B_FORMULA: ("600002", "半导体甲"),
               C_FORMULA: ("600003", "半导体乙")}

    def fake_run(*_args, **_kwargs):
        from laoa_trader.strategy import formula_group

        run = formula_group.FormulaRun()
        for name in formula_names:
            symbol, label = mapping[name]
            run.picks[formula_group.formula_strategy_name(name)] = [
                {"symbol": symbol, "name": label, "reason": name},
            ]
        run.ran = list(formula_names)
        return run

    return fake_run


def test_run_daily_pushes_everything_by_default(cfg, db, daily_setup, monkeypatch) -> None:
    """**全推**：勾上的公式选出来的票都进池、都进推送。"""
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(A_FORMULA, B_FORMULA))
    report = scheduler.run_daily(cfg, DataEngine(db), notify=True)

    numbered = [line for line in daily_setup["lines"] if line[:1].isdigit()]
    assert any("600001" in line for line in numbered)
    assert any("600002" in line for line in numbered)
    assert report["pushed"] is True
    assert report["push_skipped_rows"] == []          # 键还在（老调用方读它），但恒为空
    assert report["push_skipped_kind"] is None


def test_push_skipped_kind_is_only_about_duplicates(cfg, db, daily_setup,
                                                    monkeypatch) -> None:
    """**没推只有一种原因**：同一天同一批内容已经推过（幂等） —— `duplicate`。"""
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(A_FORMULA, B_FORMULA))

    first = scheduler.run_daily(cfg, DataEngine(db), notify=True)
    assert first["pushed"] is True

    second = scheduler.run_daily(cfg, DataEngine(db), notify=True)

    assert second["pushed"] is False
    assert second["push_skipped_kind"] == "duplicate"
    assert "已推送过" in (second["push_skipped"] or "")
    assert second["push_skipped_rows"] == []


def test_all_formula_picks_are_still_pushed(cfg, db, daily_setup, monkeypatch) -> None:
    """一整批候选照推（老口径下"整批都被判成依赖开盘 → 一条都不推"那条路已经不存在）。"""
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(B_FORMULA))

    report = scheduler.run_daily(cfg, DataEngine(db), notify=True)

    assert report["pool"]
    assert report["pushed"] is True
    assert report["push_skipped_kind"] is None
    assert daily_setup.get("lines")


def test_pool_rows_carry_no_evidence_marker(cfg, db) -> None:
    """池子行里那两个证据字段一律为空（界面不再打「（依赖开盘）」标记）。"""
    pool.save_pool(db, [
        {"symbol": "600002", "name": "半导体甲", "score": 2.0,
         "strategy": "公式·地量后放量变盘", "strategies": "公式·地量后放量变盘",
         "reason": "地量后放量"},
        # 老库里的历史行（策略类名）也一样：不再有证据标记
        {"symbol": "600001", "name": "浦发样本", "score": 1.0,
         "strategy": "ReversalStrategy", "strategies": "ReversalStrategy",
         "reason": "短期反转"},
    ], "2026-09-11")

    rows = {r["symbol"]: r for r in pool.pool_table_rows(db)}
    for row in rows.values():
        assert row["evidence"] == ""
        assert row["evidence_text"] == ""
    # 老行的中文名照旧显示（历史数据的显示口径由 legacy.strategy_label 负责）
    assert rows["600001"]["source_label"] == "短期反转"
