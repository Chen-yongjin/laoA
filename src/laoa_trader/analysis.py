"""决策信号（骨架）：把已有的事实拼成一句**可追溯的结论**。

用户 2026-10-13 定的口径
------------------------
> 「程序搭好架子，大模型只是辅助作用，可以从网上收集一些不是行情数据能读到的信息。」

所以这个模块的分工是死的：

* **判据、阈值、结论全在程序里**（本模块）。跑一遍不联网、不花钱、可复现 ——
  同一份数据同一版判据，结论必须一模一样。
* **大模型只做辅助**：把程序抓到的公告/新闻/行业消息读一遍，挑出跟这只票有关的，
  写成一句话（`WebNote`），挂到结论下面当附注。**它不改结论**，
  也不参与定阈值。没有 Key、断网、调用失败 ⇒ 结论照出，只是少那几行附注。
* 因此这里的每一条依据都是 `Evidence`（来源 + 数字 + 一句人话），
  界面点开就能追到是哪条策略、哪个字段、哪一天的数。

为什么阈值要单独收在 `RuleSet` 里
--------------------------------
用户要求「所有因子都要关注，最好能回测历史行情，给出最精准的建议」——
阈值最后必须由**回测标定**（哪条规则、多少样本、胜率与超额多少），
而不是在代码里散着写死。收成一个 `RuleSet` 之后：

* 回测跑出更优的阈值 → 只换 `RuleSet`（连同 `version` 一起换），判定逻辑不动；
* `calibrated=False` 时（现在就是）结论里会明说"阈值还没标定过"，
  免得把拍脑袋的默认值当成回测结论卖给用户。

措辞纪律（合规）
----------------
对外一律**进场信号 / 离场信号**，不写「建议买入」「建议卖出」这类指令句。
`VERDICTS` 里的六个词是唯一合法的结论集，界面与导出都从这里取。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

#: 判据版本：回测标定一次就升一位（与 `scoring.MODEL_VERSION` 同样的做法）。
SIGNAL_VERSION = "0.1.0"

#: 结论集 —— **唯一**合法的说法（自选池三条、持仓三条）。
ENTER = "进场信号成立"
WATCH = "继续观察"
DROP = "建议剔除"
HOLD = "持有"
TRIM = "减仓"
EXIT = "离场信号成立"

#: 自选池用的三条（池子里的票**默认就是观望状态**，分析回答的是"还值不值得留着看"）。
POOL_VERDICTS: tuple[str, ...] = (ENTER, WATCH, DROP)
#: 持仓用的三条。
POSITION_VERDICTS: tuple[str, ...] = (HOLD, TRIM, EXIT)
VERDICTS: tuple[str, ...] = POOL_VERDICTS + POSITION_VERDICTS

#: 依据的种类（界面按这个分组显示）。
KIND_RULE = "规则"
KIND_SCORE = "评分"
KIND_FUND = "资金"
KIND_POSITION = "持仓"
KIND_MARKET = "大盘"
KIND_WEB = "消息"


@dataclass(frozen=True)
class Evidence:
    """一条依据：**来源 + 数字 + 一句人话**。

    `source` 是"这条结论凭什么"的答案，必须能被用户自己核一遍：
    写策略名就写策略名（`公式·竞价策略`），写字段就写字段（`主力净额`），
    写网页就写网址。**不允许出现"综合来看"这种没来源的话**。
    """

    kind: str
    text: str
    source: str = ""

    def line(self) -> str:
        """一行显示文本。**来源与正文重复时不再重复一遍**：像"当轮被策略命中：尾盘超短策略
        （尾盘超短策略）"这种括号纯属噪音 —— 同一份内容说两遍，用户还得琢磨括号里是什么。"""
        if self.source and self.source not in self.text:
            return f"{self.text}（{self.source}）"
        return self.text


@dataclass(frozen=True)
class RuleSet:
    """判定阈值。**默认值是拍脑袋的临时值**（`calibrated=False`），等回测换掉。"""

    version: str = "provisional-0"
    #: 回测标定过没有。False 时结论里带一句提醒，界面上也照实说。
    calibrated: bool = False

    # —— 自选池 ——（评分是 0~100，见 `scoring.ADVICE_LEVELS`）
    #: 评分 ≥ 这条 **且** 当轮有已勾选策略命中 → 进场信号成立
    enter_score: float = 65.0
    #: 评分 ≤ 这条 **且** 长期没有策略命中 → 建议剔除
    drop_score: float = 30.0
    #: 距最近一次策略命中超过这么多交易日，算"长期没信号"
    stale_days: int = 20

    # —— 持仓 ——
    #: 评分 ≤ 这条 **且** 主力净流出 → 离场信号成立
    exit_score: float = 30.0
    #: 评分 ≤ 这条 **且** 主力净流出 → 减仓
    trim_score: float = 45.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "calibrated": self.calibrated,
            "enter_score": self.enter_score,
            "drop_score": self.drop_score,
            "stale_days": self.stale_days,
            "exit_score": self.exit_score,
            "trim_score": self.trim_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleSet":
        """从字典还原（回测把标定结果存下来就是这么存的）。**认不出的键直接忽略**。"""
        fields = {name for name in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in (data or {}).items() if k in fields})


#: 现在的默认判据（未标定）。
DEFAULT_RULES = RuleSet()


@dataclass(frozen=True)
class WebNote:
    """大模型那条线产出的**附注**（不是结论）。

    程序负责把原始文本抓下来（公告、新闻、行业消息 —— 这些行情接口里没有），
    大模型负责从里面挑出跟这只票相关的、写成一句话。挑不出来就一条都不给。
    """

    title: str
    summary: str = ""
    url: str = ""
    date: str = ""
    source: str = ""

    def evidence(self) -> Evidence:
        when = f"{self.date} " if self.date else ""
        origin = self.url or self.source
        return Evidence(KIND_WEB, f"{when}{self.title}：{self.summary}".rstrip("："), origin)


@dataclass
class Signal:
    """一只票的一份结论。"""

    symbol: str
    verdict: str
    headline: str = ""
    name: str = ""
    score: float | None = None
    as_of: str = ""
    evidences: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    web: list[WebNote] = field(default_factory=list)
    signal_version: str = SIGNAL_VERSION
    rules_version: str = DEFAULT_RULES.version

    def tip(self) -> str:
        """鼠标停在结论上看到的全文（与评分那一格同一个风格）。"""
        lines = [f"{self.symbol} {self.name}".strip(), f"【{self.verdict}】{self.headline}".rstrip("：")]
        if self.evidences:
            lines.append("")
            for item in self.evidences:
                lines.append(f"  · {item.line()}")
        if not self.web:
            lines.append("\n消息：未获取（未配置大模型或本次未联网；不影响上面的结论）")
        for note in self.notes:
            lines.append(f"\n注：{note}")
        return "\n".join(lines)


def _no_score_note(score: Any) -> str:
    if score is None:
        return "没有这只票的评分（数据不足），只按已有事实给结论。"
    note = getattr(score, "note", "")
    return note or "评分算不出来。"


def _score_evidence(score: Any) -> Evidence:
    total = float(getattr(score, "total", 0.0))
    advice = getattr(score, "advice", "") or ""
    return Evidence(
        KIND_SCORE,
        f"评分 {total:.0f}（{advice}）",
        f"scoring v{getattr(score, 'model_version', '')}",
    )


def judge_pool(
    symbol: str,
    *,
    name: str = "",
    score: Any = None,
    strategies: Sequence[str] = (),
    stale_days: int | None = None,
    rules: RuleSet = DEFAULT_RULES,
    extra: Iterable[Evidence] = (),
    as_of: str = "",
) -> Signal:
    """自选池里的一只票 → 进场信号成立 / 继续观察 / 建议剔除。

    `strategies` 是**当轮**选出了它的策略名（`pool.round_pick_strategies()` 的那份）；
    `stale_days` 是"距最近一次命中过了多少交易日"，**`None` = 查不到命中记录**
    （按"长期没有信号"算，与"刚命中过"是两回事）。

    「建议剔除」只针对**自选池**（不再值得占着观察位），不是清仓建议 ——
    这两件事在持仓那一路上分开算，别混。
    """
    evidences: list[Evidence] = []
    notes: list[str] = []

    scored = score is not None and bool(getattr(score, "ok", True))
    if scored:
        evidences.append(_score_evidence(score))
    else:
        notes.append(_no_score_note(score))
    for text in strategies:
        evidences.append(Evidence(KIND_RULE, f"当轮被策略命中：{text}", text))
    evidences.extend(extra)

    total = float(getattr(score, "total", 0.0)) if scored else None
    # "还新鲜"的两种证据：当轮命中（`strategies` 非空）、或查得到且没超期（`stale_days`）。
    # **查不到命中记录不算新鲜**：那正是"这只票已经很久没被任何策略选中"的样子。
    fresh = bool(strategies) or (stale_days is not None and stale_days <= rules.stale_days)
    if stale_days is not None:
        evidences.append(Evidence(KIND_RULE, f"距最近一次策略命中 {stale_days} 个交易日",
                                  f"阈值 {rules.stale_days} 日"))
    elif not strategies:
        notes.append("没有查到这只票被策略命中的记录（按长期无信号算）。")

    if total is not None and total >= rules.enter_score and strategies:
        verdict, headline = ENTER, "评分达标，且当轮有策略命中。"
    elif total is not None and total <= rules.drop_score and not fresh:
        verdict, headline = DROP, "评分偏弱，且长期没有策略命中。"
    elif total is None and not strategies:
        verdict, headline = WATCH, "数据不足，先继续观察。"
    else:
        verdict, headline = WATCH, "条件没凑齐，继续观察。"

    if not rules.calibrated:
        notes.append(f"判定阈值尚未回测标定（当前 {rules.version}），只作参考。")
    return Signal(
        symbol=symbol, name=name, verdict=verdict, headline=headline,
        score=total, as_of=as_of or getattr(score, "as_of", "") or "",
        evidences=evidences, notes=notes,
        rules_version=rules.version, signal_version=SIGNAL_VERSION,
    )


def judge_position(
    symbol: str,
    *,
    name: str = "",
    shares: int = 0,
    cost: float | None = None,
    price: float | None = None,
    score: Any = None,
    main_net: float | None = None,
    stop_hit: bool = False,
    take_hit: bool = False,
    rules: RuleSet = DEFAULT_RULES,
    extra: Iterable[Evidence] = (),
    as_of: str = "",
) -> Signal:
    """持仓里的一只票 → 持有 / 减仓 / 离场信号成立。

    * 止损止盈（用户自己在设置里填的）**一旦触发就是离场信号** —— 那是用户自己定的线，
      程序不替他推翻；
    * 其余情况看评分 + 主力资金：评分弱 **且** 资金净流出才提减仓/离场，
      单看评分就喊"走"太容易被打脸（回测标定要验的就是这一条）。

    `main_net` 单位是**万元**（与扩展字段 `主力净额` 同一口径，正=净流入）。
    """
    evidences: list[Evidence] = []
    notes: list[str] = []

    if shares:
        evidences.append(Evidence(KIND_POSITION, f"持有 {shares} 股", "持仓表"))
    if cost is not None and price is not None and cost > 0:
        pct = (price - cost) / cost * 100
        evidences.append(Evidence(KIND_POSITION,
                                  f"成本 {cost:.2f} → 现价 {price:.2f}（{pct:+.2f}%）", "持仓表"))
    scored = score is not None and bool(getattr(score, "ok", True))
    if scored:
        evidences.append(_score_evidence(score))
    else:
        notes.append(_no_score_note(score))
    if main_net is not None:
        evidences.append(Evidence(KIND_FUND, f"主力净额 {main_net:+.0f} 万元", "资金流"))
    evidences.extend(extra)

    total = float(getattr(score, "total", 0.0)) if scored else None
    outflow = main_net is not None and main_net < 0

    if stop_hit:
        verdict, headline = EXIT, "触发你设置的止损线。"
    elif take_hit:
        verdict, headline = EXIT, "触发你设置的止盈线。"
    elif total is not None and total <= rules.exit_score and outflow:
        verdict, headline = EXIT, "评分弱，且主力资金净流出。"
    elif total is not None and total <= rules.trim_score and outflow:
        verdict, headline = TRIM, "评分转弱，且主力资金净流出。"
    elif total is None and main_net is None:
        verdict, headline = HOLD, "没有评分与资金数据，暂按持有。"
    else:
        verdict, headline = HOLD, "没有触发离场条件。"

    if not rules.calibrated:
        notes.append(f"判定阈值尚未回测标定（当前 {rules.version}），只作参考。")
    return Signal(
        symbol=symbol, name=name, verdict=verdict, headline=headline,
        score=total, as_of=as_of or getattr(score, "as_of", "") or "",
        evidences=evidences, notes=notes,
        rules_version=rules.version, signal_version=SIGNAL_VERSION,
    )


def attach_web(signal: Signal, notes_: Iterable[WebNote]) -> Signal:
    """把大模型那条线的附注挂到结论上（`Signal` 是可变对象，这里返回同一个实例）。

    附注**只进 `signal.web`、也顺带进 `evidences` 供界面一屏显示**，
    但不参与任何判定 —— 这样"关掉大模型，结论一模一样"这条纪律才成立。
    """
    for note in notes_ or ():
        signal.web.append(note)
        signal.evidences.append(note.evidence())
    return signal


def with_rules(rules: RuleSet, **changes: Any) -> RuleSet:
    """改几个阈值（回测标定后落库、或用户在设置里微调时用）。"""
    return replace(rules, **changes)


def format_line(signal: Signal) -> str:
    """一行文本（消息列表、导出、CLI 用同一份）。"""
    return f"{signal.symbol} {signal.name}｜{signal.verdict}｜{signal.headline}".strip()
