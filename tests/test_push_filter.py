"""推送过滤（`push_only_proven`）与策略"证据"标注的测试。

背景（用户拍板后的默认策略集）
------------------------------
默认只开 `short`（T+3 定位）；`ultra`（连板回踩：两套口径显著为负）与 `swing`
（T+10 波段）**停用但代码保留**。`short` 组里的两条策略（地量后放量变盘、首板缩量整理）
正 α 只存在于"开盘买"口径 —— 它们**是可勾选的内置策略，照常推送**（用户拍板：
判断权交给他，只把两套口径的数字摆在眼前，不替他静默过滤）。

这里盯的就是这两件事：
1. 标注能被代码读到（组停用原因、策略 `evidence` + 口径数字），并且**界面上看得见**；
2. **默认全推**；只有用户把 `push_only_proven` 打开时才"只推有边际的"，
   而且那时"为什么跳过"能从推送正文/日志/状态里看到。
"""

from __future__ import annotations

import logging

import pytest

from laoa_trader import config as config_mod
from laoa_trader import pool
from laoa_trader import scheduler
from laoa_trader.config import Config
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import groups as groups_mod
from laoa_trader.strategy import rules as rules_mod
from laoa_trader.strategy.base import EVIDENCE_OPEN_ONLY, EVIDENCE_PROVEN

OPEN_ONLY = "DryUpExpansionStrategy"        # 地量后放量变盘
OPEN_ONLY_2 = "FirstLimitUpStrategy"        # 首板缩量整理
PROVEN = "ReversalStrategy"                 # 短期反转（两套口径都为正）


# ── 默认策略集与标注 ──


def test_default_groups_is_short_only_everywhere() -> None:
    """默认策略集只有 `short`：config、groups、界面勾选都引用同一份事实。"""
    cfg = Config()
    assert cfg.enabled_groups == ["short"]
    assert groups_mod.default_group_keys() == ["short"]
    # 两处不能漂移：配置默认值必须等于 groups.py 里"默认启用"的那几组
    assert list(cfg.enabled_groups) == groups_mod.default_group_keys()


def test_disabled_groups_carry_the_reason() -> None:
    """停用的两组要带**中文原因**（引用成绩单数字），不是静默消失。"""
    reasons = groups_mod.disabled_groups()
    assert set(reasons) == {"ultra", "swing"}
    assert "−1.21%" in reasons["ultra"] and "−8.87" in reasons["ultra"]
    assert "0 年为正" in reasons["ultra"]
    assert "T+10" in reasons["swing"] and "T+3" in reasons["swing"]
    # 组本身仍然在（代码不删，用户以后可能自己开）
    assert set(groups_mod.GROUPS) == {"ultra", "short", "swing"}
    assert groups_mod.GROUPS["short"].disabled_reason == ""


def test_open_only_strategies_are_marked_with_evidence() -> None:
    """两条"依赖开盘"的策略带 `evidence = open_only` + 中文说明；其余是 `proven`。"""
    for name in (OPEN_ONLY, OPEN_ONLY_2):
        cls = rules_mod.STRATEGIES[name]
        assert cls.evidence == EVIDENCE_OPEN_ONLY
        assert "开盘买" in cls.evidence_note
        assert "尾盘买转负" in cls.evidence_note
        assert "未必吃得进" in cls.evidence_note
    # 两套口径的数字都要摆在眼前（用户据此自己判断，程序只负责给数据）
    assert "A +0.09%(t=2.31)" in rules_mod.STRATEGIES[OPEN_ONLY].evidence_note
    assert "−0.15%(t=−3.14)" in rules_mod.STRATEGIES[OPEN_ONLY].evidence_note
    assert "A +0.21%(t=1.92)" in rules_mod.STRATEGIES[OPEN_ONLY_2].evidence_note
    assert "−0.01%(t=−2.34)" in rules_mod.STRATEGIES[OPEN_ONLY_2].evidence_note
    # 短期反转两套口径都为正 → 正常推送
    assert rules_mod.STRATEGIES[PROVEN].evidence == EVIDENCE_PROVEN
    assert rules_mod.STRATEGIES["LadderPullbackStrategy"].evidence == EVIDENCE_PROVEN


def test_evidence_text_and_tooltip_for_the_ui() -> None:
    """界面标记：`（依赖开盘）` + 一句能看懂的解释（不只写在文档里）。"""
    assert pool.evidence_text(OPEN_ONLY) == "（依赖开盘）"
    assert pool.evidence_text(PROVEN) == ""
    assert pool.evidence_text("不存在的策略") == ""       # 认不出按 proven（照常推）
    tip = pool.open_only_tooltip("地量后放量变盘")
    assert "地量后放量变盘" in tip
    assert "策略照常推送" in tip and "push_only_proven" in tip   # 提示怎么收紧，不是默认收紧


def test_strategy_evidence_unknown_defaults_to_proven() -> None:
    """没标注的策略按"有边际"处理：少推了比多推了更难被发现。"""
    assert pool.strategy_evidence("") == EVIDENCE_PROVEN
    assert pool.strategy_evidence("SomeFutureStrategy") == EVIDENCE_PROVEN


# ── 哪些行算"有边际" ──


def test_row_is_proven_rules() -> None:
    assert pool.row_is_proven({"strategies": PROVEN}) is True
    assert pool.row_is_proven({"strategies": OPEN_ONLY}) is False
    assert pool.row_is_proven({"strategies": OPEN_ONLY_2}) is False
    # 多策略同时选中：只要有一条 proven，这个标的就值得推
    assert pool.row_is_proven({"strategies": f"{OPEN_ONLY},{PROVEN}"}) is True
    assert pool.row_is_proven({"strategies": f"{OPEN_ONLY},{OPEN_ONLY_2}"}) is False
    # 只有主策略字段（老数据）也要判对
    assert pool.row_is_proven({"strategy": OPEN_ONLY}) is False
    assert pool.row_is_proven({"strategy": PROVEN}) is True
    # 纯自选（没有策略）永远推：用户自己加的本来就要看
    assert pool.row_is_proven({"strategies": "", "strategy": ""}) is True


def test_split_push_rows_keeps_proven_and_self_selected() -> None:
    """**打开**开关时：proven、多策略里含 proven、纯自选都留下，只滤掉"纯依赖开盘"的。"""
    rows = [
        {"symbol": "600001", "name": "甲", "strategies": PROVEN},
        {"symbol": "600002", "name": "乙", "strategies": OPEN_ONLY},
        {"symbol": "600003", "name": "丙", "strategies": ""},            # 自选
        {"symbol": "600004", "name": "丁", "strategies": f"{OPEN_ONLY},{PROVEN}"},
    ]
    keep, skipped = pool.split_push_rows(rows, Config(push_only_proven=True))
    assert [r["symbol"] for r in keep] == ["600001", "600003", "600004"]
    assert [r["symbol"] for r in skipped] == ["600002"]
    assert "依赖开盘" in pool.skipped_push_note(skipped)
    assert "乙(600002)" in pool.skipped_push_note(skipped)
    assert pool.skipped_push_note([]) == ""


def test_split_push_rows_default_keeps_everything() -> None:
    """**默认全推**（`push_only_proven` 默认 false）：启用的策略选出来的一律推送。"""
    rows = [{"symbol": "600001", "strategies": OPEN_ONLY},
            {"symbol": "600002", "strategies": PROVEN},
            {"symbol": "600003", "strategies": ""}]
    assert Config().push_only_proven is False           # 默认就是"全推"
    keep, skipped = pool.split_push_rows(rows)          # 不传 cfg，走默认配置
    assert [r["symbol"] for r in keep] == ["600001", "600002", "600003"]
    assert skipped == []


def test_split_push_rows_filters_only_when_switched_on() -> None:
    """只有**主动打开** `push_only_proven` 时才收窄：依赖开盘的进池但不再推。"""
    rows = [{"symbol": "600001", "strategies": OPEN_ONLY},
            {"symbol": "600002", "strategies": PROVEN}]
    keep, skipped = pool.split_push_rows(rows, Config(push_only_proven=True))
    assert [r["symbol"] for r in keep] == ["600002"]
    assert [r["symbol"] for r in skipped] == ["600001"]


def test_push_only_proven_invalid_value_falls_back_to_default(tmp_path) -> None:
    """写错（`"maybe"`）→ 回到**当前默认**（全推），不是静默改成"只推有边际的"。"""
    path = tmp_path / "config.toml"
    path.write_text('push_only_proven = "maybe"\n', encoding="utf-8")
    assert config_mod.load_config(path, use_env=False).push_only_proven is False
    assert config_mod.load_config(use_env=False).push_only_proven is False


def test_push_only_proven_env_override(monkeypatch) -> None:
    """环境变量：真值表里的值才认，写错回默认（false）。"""
    monkeypatch.setenv("PUSH_ONLY_PROVEN", "1")
    assert config_mod.load_config(None, use_env=True).push_only_proven is True
    monkeypatch.setenv("PUSH_ONLY_PROVEN", "maybe")
    assert config_mod.load_config(None, use_env=True).push_only_proven is False


# ── 池子行上的标记（表格/卡片看得到）──


def test_pool_table_rows_mark_open_only(cfg, db) -> None:
    """池子行的 `evidence_text` 标记：界面据此在策略列/卡片上打「（依赖开盘）」。"""
    pool.save_pool(db, [
        {"symbol": "600002", "name": "半导体甲", "score": 2.0,
         "strategy": OPEN_ONLY, "strategies": OPEN_ONLY, "reason": "地量后放量"},
        {"symbol": "600001", "name": "浦发样本", "score": 1.0,
         "strategy": PROVEN, "strategies": PROVEN, "reason": "短期反转"},
    ], "2026-09-11")
    rows = {r["symbol"]: r for r in pool.pool_table_rows(db)}
    assert rows["600002"]["evidence_text"] == "（依赖开盘）"
    assert rows["600002"]["evidence"] == EVIDENCE_OPEN_ONLY
    assert rows["600001"]["evidence_text"] == ""


# ── 端到端：推送里有没有它 ──


@pytest.fixture()
def daily_setup(cfg, db, monkeypatch):
    """把 `run_daily` 的外部依赖按住（同步/通知/热门行业/选股结果）并捕获推送正文。"""
    captured: dict = {}

    def fake_notify_all(title, lines, kinds=None, cfg=None):
        captured["title"] = title
        captured["lines"] = list(lines)
        return {"tray": {"kind": "tray", "ok": True}}

    monkeypatch.setattr(scheduler.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify_all)
    monkeypatch.setattr("laoa_trader.pool.hot_industries", lambda db_path, **k: {})
    return captured


def _fake_formulas(*class_names: str):
    """让「公式」组按给定名字各返回一只（symbol 与名字一一对应）。

    2026-09-18（用户要求）：候选只来自**勾选的公式** —— `run_daily` 里已经没有
    "跑内置策略"这一步了，所以桩要打在 `formula_group.run_enabled_formulas` 上，
    返回的键是合成名 `公式·X`（传类名会被建池当"老策略类名"丢掉）。

    ⚠️ 顺带一个**口径后果**（本文件下面几条用例因此改了口径）：
    「依赖开盘」那个证据标记来自 `rules.STRATEGIES[类名].evidence`，而公式候选没有类名 ——
    所以 `strategy_evidence("公式·X")` 认不出它、按 `proven` 处理，
    `push_only_proven` 这个开关**对公式候选不再生效**。机制本身没坏：
    单元级用例（`test_row_is_proven_rules` / `test_split_push_rows_*` /
    `test_pool_table_rows_mark_open_only`）照旧守着它。
    """
    mapping = {OPEN_ONLY: ("600002", "半导体甲"), OPEN_ONLY_2: ("600003", "半导体乙"),
               PROVEN: ("600001", "浦发样本")}

    def fake_run(*_args, **_kwargs):
        from laoa_trader.strategy import formula_group, groups as groups_mod

        run = formula_group.FormulaRun()
        for name in class_names:
            symbol, label = mapping[name]
            run.picks[groups_mod.formula_strategy_name(name)] = [
                {"symbol": symbol, "name": label, "reason": name},
            ]
        run.ran = list(class_names)
        return run

    return fake_run


def test_run_daily_pushes_everything_by_default(cfg, db, daily_setup, monkeypatch) -> None:
    """**默认全推**：勾上的公式选出来的票都进池、都进推送（用户拍板）。"""
    assert cfg.push_only_proven is False
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(PROVEN, OPEN_ONLY))
    report = scheduler.run_daily(cfg, DataEngine(db), notify=True)
    numbered = [line for line in daily_setup["lines"] if line[:1].isdigit()]
    assert any("600001" in line for line in numbered)
    assert any("600002" in line for line in numbered)          # 另一条公式的也在推送里
    assert report["pushed"] is True
    assert report["push_skipped_rows"] == []
    assert "依赖开盘" not in "\n".join(daily_setup["lines"])   # 不作过滤，也就没有那句说明


def test_push_only_proven_is_inert_for_formula_picks(cfg, db, daily_setup,
                                                    monkeypatch, caplog) -> None:
    """打开 `push_only_proven` 也**照推**：公式候选没有「依赖开盘」标记（2026-09-18 口径）。

    ⚠️ 这条是**口径变了**，不是把断言放松：那个标记存在
    `rules.STRATEGIES[类名].evidence` 里，而候选现在只来自公式（没有类名）→
    `strategy_evidence()` 按 `proven` 处理，于是这只票不再被过滤。
    想恢复"按证据收窄推送"，得先给公式一种能带标记的写法（例如公式文件的说明里写
    `# 依赖开盘`）—— 那是一件独立的事，见本轮报告。机制本身仍有单元级用例守着
    （`test_row_is_proven_rules` / `test_split_push_rows_filters_only_when_switched_on`）。
    """
    cfg.push_only_proven = True
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(PROVEN, OPEN_ONLY))
    with caplog.at_level(logging.INFO, logger="laoa_trader.scheduler"):
        report = scheduler.run_daily(cfg, DataEngine(db), notify=True)

    pooled = {row["symbol"] for row in report["pool"]}
    assert {"600001", "600002"} <= pooled                      # 两只都进池了
    numbered = [line for line in daily_setup["lines"] if line[:1].isdigit()]
    assert any("600001" in line for line in numbered)
    assert any("600002" in line for line in numbered)          # ← 不再被过滤掉
    assert report["pushed"] is True
    assert report["push_skipped_rows"] == []
    assert not [r for r in caplog.records if "推送过滤" in r.message]


def test_push_only_proven_switch_does_not_change_formula_pushes(cfg, db, daily_setup,
                                                               monkeypatch) -> None:
    """开关 true → false 对公式候选没有区别（都全推）：它俩都不走"按证据过滤"那条路。

    老口径下 true 会收窄、false 会放开（`test_split_push_rows_*` 仍然守着那套规则）；
    公式候选没有证据标记，所以两种取值下推送内容一致 —— 这条把差别钉清楚。
    """
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(PROVEN, OPEN_ONLY))

    cfg.push_only_proven = True
    report_on = scheduler.run_daily(cfg, DataEngine(db), notify=True)
    body_on = "\n".join(daily_setup["lines"])
    assert "600001" in body_on and "600002" in body_on
    assert report_on["push_skipped_rows"] == []

    # 再改成 false 跑一次：**内容没变**，所以被"同一天同一批内容只推一次"的指纹挡住 ——
    # 这与证据过滤无关（`push_skipped_rows` 仍然是空的），正好把两件事分开：
    # "开关不影响公式候选的推送内容"（上面那条）+ "同一批内容不会推两遍"（这条）。
    cfg.push_only_proven = False
    report_off = scheduler.run_daily(cfg, DataEngine(db), notify=True)
    assert report_off["pushed"] is False
    assert "已推送过" in (report_off["push_skipped"] or "")
    assert report_off["push_skipped_rows"] == []


def test_all_formula_picks_are_still_pushed(cfg, db, daily_setup,
                                            monkeypatch, caplog) -> None:
    """一批候选"全带某个标记"的极端情形：新口径下照推（`push_skipped_kind` 保持 None）。

    老口径：整批都被判成"依赖开盘" → 一条都不推、`push_skipped_kind == "filtered"`。
    新口径：公式候选没有标记 → 没有可过滤的东西，推送照发。
    真正的过滤逻辑（`split_push_rows`）仍由单元用例守着；这条只钉"run_daily 这一步"。
    """
    cfg.push_only_proven = True
    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        _fake_formulas(OPEN_ONLY))
    with caplog.at_level(logging.INFO, logger="laoa_trader.scheduler"):
        report = scheduler.run_daily(cfg, DataEngine(db), notify=True)

    assert report["pool"]                                       # 池子照常有内容
    assert report["pushed"] is True
    assert report["push_skipped_kind"] is None
    assert daily_setup.get("lines")                             # 真的推了
    assert not [r for r in caplog.records if "跳过推送" in r.message]


def test_list_groups_shows_the_disabled_reasons() -> None:
    """`--list-groups` 的输出里能看到停用标记与原因（CLI 用户也不用翻文档）。"""
    lines = groups_mod.describe_groups()
    text = "\n".join(lines)
    assert "⛔" in text
    assert "显著为负" in text and "−8.87" in text
    assert "T+10 波段" in text
    assert "默认启用的组：short" in text
