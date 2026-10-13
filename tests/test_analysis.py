"""决策信号骨架（`analysis.py`）的测试。

这一层要守住的三条纪律：

1. **结论集只有六个词**（进场信号成立 / 继续观察 / 建议剔除 / 持有 / 减仓 / 离场信号成立），
   不许出现「建议买入」「建议卖出」这类指令式措辞；
2. **没有大模型也照样出结论** —— 关掉附注那条线，判定结果一个字都不变；
3. 每条依据都能追到来源（策略名 / 字段名 / 网址），不出现"综合来看"这种空话。
"""

from __future__ import annotations

import pytest

from laoa_trader import analysis
from laoa_trader.analysis import (
    DROP, ENTER, EXIT, HOLD, TRIM, WATCH, Evidence, RuleSet, WebNote,
    attach_web, format_line, judge_pool, judge_position, with_rules,
)
from laoa_trader.scoring import ScoreResult


def make_score(total: float, *, advice: str = "中性") -> ScoreResult:
    """造一份评分结果（只给判定用得上的那几个字段）。"""
    return ScoreResult(symbol="600000", total=total, advice=advice,
                       as_of="2026-10-13", caliber="前复权", model_version="1.0.0")


# ————————————————————————— 自选池 —————————————————————————

def test_pool_enter_needs_both_score_and_strategy():
    """评分达标 **且** 当轮有策略命中，才给进场信号 —— 只满足一条就还是继续观察。"""
    score = make_score(78.0, advice="偏强")
    hit = judge_pool("600000", score=score, strategies=["公式·竞价策略"])
    assert hit.verdict == ENTER
    assert any("公式·竞价策略" in e.line() for e in hit.evidences)

    no_hit = judge_pool("600000", score=score, strategies=[])
    assert no_hit.verdict == WATCH


def test_pool_drop_needs_weak_score_and_no_recent_hit():
    """偏弱 + 长期没命中才建议剔除；刚命中过（哪怕分低）不剔除。"""
    weak = make_score(20.0, advice="弱势")
    assert judge_pool("600000", score=weak, strategies=[],
                      stale_days=45).verdict == DROP
    assert judge_pool("600000", score=weak, strategies=["公式·二板炸板"],
                      stale_days=2).verdict == WATCH


def test_pool_unknown_data_says_so_instead_of_guessing():
    """评分算不出来（本地没日线）时不许瞎给结论：继续观察 + 写明原因。"""
    bad = ScoreResult(symbol="600000", note="本地没有这只票的日线")
    signal = judge_pool("600000", score=bad)
    assert signal.verdict == WATCH
    assert any("日线" in n for n in signal.notes)
    assert WATCH in format_line(signal)


def test_pool_notes_uncalibrated_rules():
    """阈值没回测标定过就要在结论里说清楚，别让拍脑袋的默认值冒充回测结论。"""
    signal = judge_pool("600000", score=make_score(78.0), strategies=["公式·X"])
    assert signal.rules_version == analysis.DEFAULT_RULES.version
    assert any("尚未回测标定" in n for n in signal.notes)


# ————————————————————————— 持仓 —————————————————————————

def test_position_user_stop_wins_over_everything():
    """止损止盈是用户自己定的线，触发了就是离场信号，评分再高也不推翻。"""
    strong = make_score(90.0, advice="强势")
    assert judge_position("600000", shares=100, cost=10.0, price=9.0,
                          score=strong, stop_hit=True).verdict == EXIT
    assert judge_position("600000", shares=100, cost=10.0, price=12.0,
                          score=strong, take_hit=True).verdict == EXIT


def test_position_weak_score_needs_outflow_to_exit():
    """评分弱 + 资金净流出才喊离场；只看评分不动（回测要验的就是这条）。"""
    weak = make_score(25.0, advice="弱势")
    assert judge_position("600000", shares=100, score=weak,
                          main_net=-820.0).verdict == EXIT
    assert judge_position("600000", shares=100, score=weak,
                          main_net=300.0).verdict == HOLD
    assert judge_position("600000", shares=100, score=weak).verdict == HOLD


def test_position_trim_band_is_above_exit_band():
    """减仓那档比分更淡一点，两档阈值不会打架。"""
    mid = make_score(40.0, advice="偏弱")
    assert judge_position("600000", shares=100, score=mid,
                          main_net=-100.0).verdict == TRIM
    assert judge_position("600000", shares=100, score=mid,
                          main_net=100.0).verdict == HOLD


def test_position_reports_cost_and_pnl():
    """结论里要带上成本与盈亏，用户一眼知道这条说的是哪一笔。"""
    score = make_score(50.0)
    signal = judge_position("600000", shares=200, cost=10.0, price=11.0, score=score)
    assert any("成本 10.00" in e.line() and "+10.00%" in e.line() for e in signal.evidences)
    assert any("持有 200 股" in e.line() for e in signal.evidences)


# ————————————————————————— 大模型只做附注 —————————————————————————

def test_web_notes_only_append_and_never_change_the_verdict():
    """附注这条线**不许改结论** —— 这正是"大模型只是辅助"的落地检查。"""
    score = make_score(78.0, advice="偏强")
    bare = judge_pool("600000", score=score, strategies=["公式·X"])
    with_web = judge_pool("600000", score=score, strategies=["公式·X"])
    attach_web(with_web, [WebNote(title="公司公告：回购进展", summary="拟回购 1~2 亿元",
                                  url="https://example.com/a", date="2026-10-13",
                                  source="东财公告")])
    assert with_web.verdict == bare.verdict
    assert with_web.evidences[:-1] == bare.evidences
    assert any("回购" in e.line() and "example.com" in e.line() for e in with_web.evidences)
    assert with_web.web and with_web.web[0].title.startswith("公司公告")


def test_missing_web_line_is_stated_not_silent():
    """没取到消息（没配 Key / 没联网）要明说"不影响结论"，不能让用户以为消息是空的。"""
    signal = judge_pool("600000", score=make_score(50.0))
    if not signal.web:
        assert "未获取" in signal.tip()
        assert "不影响上面的结论" in signal.tip()


# ————————————————————————— 判据本身 —————————————————————————

def test_rule_set_roundtrip_and_calibration():
    """标定结果要能存下来再读回来（回测把阈值写进库就是走这条路）。"""
    tuned = with_rules(RuleSet(), version="backtest-1", calibrated=True,
                       enter_score=70.0, drop_score=25.0)
    again = RuleSet.from_dict(tuned.as_dict())
    assert again == tuned
    assert again.enter_score == 70.0
    signal = judge_pool("600000", score=make_score(78.0), strategies=["公式·X"], rules=again)
    assert signal.verdict == ENTER
    assert not any("尚未回测标定" in n for n in signal.notes)


def test_rule_set_ignores_unknown_keys():
    """旧库里的老键（删过的阈值）不能把还原搞崩。"""
    rules = RuleSet.from_dict({"version": "x", "enter_score": 60.0, "已经删掉的键": 1})
    assert rules.version == "x" and rules.enter_score == 60.0


def test_verdicts_never_use_buy_sell_instruction_wording():
    """合规底线：结论集里不许出现"买入/卖出"字样的指令句。"""
    for word in analysis.VERDICTS:
        assert "买入" not in word and "卖出" not in word and "建议买" not in word
    assert analysis.VERDICTS == (ENTER, WATCH, DROP, HOLD, TRIM, EXIT)


def test_evidence_requires_a_source():
    """没有来源的依据写出来不能带空的括号；来源与正文重复时不重复第二遍。"""
    assert Evidence("规则", "当轮被策略命中：公式·X").line() == "当轮被策略命中：公式·X"
    assert Evidence("资金", "主力净额 +300 万元", "资金流").line().endswith("（资金流）")
    # 策略那一条的来源就是策略名本身 —— 写两遍是噪音（离屏出图时看出来的）
    assert Evidence("规则", "当轮被策略命中：尾盘超短策略", "尾盘超短策略").line() == \
        "当轮被策略命中：尾盘超短策略"


@pytest.mark.parametrize("verdict", analysis.POOL_VERDICTS)
def test_pool_verdicts_are_within_the_allowed_set(verdict):
    """池子那一路只能落在三条里（顺手把常量本身钉住）。"""
    assert verdict in analysis.VERDICTS


# ————————————————————————— 导出用的报告正文 —————————————————————————

def test_report_text_lists_every_signal_with_evidence_and_disclaimer():
    """报告里每只票都要有结论、依据与免责；统计那一行按结论集的口径数。"""
    signals = [
        judge_pool("600001", name="浦发样本", score=make_score(78.0, advice="偏强"),
                   strategies=["尾盘超短策略"]),
        judge_pool("600002", name="半导体甲", score=make_score(50.0)),
    ]
    text = analysis.report_text(signals, day="2026-10-13", generated_at="2026-10-13 15:04")
    assert text.startswith(f"{analysis.REPORT_TITLE} · 2026-10-13")
    assert "共 2 只：进场信号成立 1 · 继续观察 1" in text
    assert "生成时间 2026-10-13 15:04（北京时间，手动分析）" in text
    assert "【进场信号成立】600001 浦发样本" in text
    assert "· 当轮被策略命中：尾盘超短策略" in text        # 依据带上了
    assert "消息：未获取" in text                          # 没接大模型那条线时如实说
    assert analysis.DISCLAIMER in text
    assert text.endswith("\n")


def test_report_text_says_uncalibrated_once_not_per_symbol():
    """阈值没标定只提醒**一次**（十只票重复十遍就是噪音）。"""
    signals = [judge_pool(f"60000{i}", score=make_score(50.0)) for i in range(3)]
    text = analysis.report_text(signals, day="2026-10-13", generated_at="x")
    assert text.count("尚未回测标定") == 1


def test_report_text_puts_web_notes_in_and_can_leave_them_out():
    """大模型的附注进报告（带日期与网址）；`include_web=False` 时连那一行都不提。"""
    signal = judge_pool("600001", score=make_score(50.0))
    attach_web(signal, [WebNote(title="公司公告：回购进展", summary="拟回购 1~2 亿元",
                                url="https://example.com/a", date="2026-10-13")])
    text = analysis.report_text([signal], day="d", generated_at="t")
    assert "消息 2026-10-13 公司公告：回购进展 https://example.com/a" in text
    bare = analysis.report_text([signal], day="d", generated_at="t", include_web=False)
    assert "消息" not in bare


def test_report_text_is_empty_when_there_is_nothing_to_export():
    """没有结论 → 空串（调用方据此说"还没有可导出的结论"，而不是导出一个空文件）。"""
    assert analysis.report_text([]) == ""
