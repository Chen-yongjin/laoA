"""决策信号（骨架）：先把**分析条件**逐条列出来，最后才给**分析结论**。

用户 2026-10-13 定的口径
------------------------
> 「程序搭好架子，大模型只是辅助作用，可以从网上收集一些不是行情数据能读到的信息。」
> 「措辞当然是要严谨一点，用离场信号和进场信号来表述更合理一点。」
> 「先列出分析条件，最后给出分析结论，那样才显得专业。」

所以这个模块的分工与版式是死的：

* **版式**：每只票一律「分析条件 → 消息 → 分析结论」；报告最后再给一节**结论汇总**。
  理由不只是好看：条件在前，用户能自己核"这条结论是怎么来的"；
  结论在后，它就是**推导的结果**而不是一句没来由的判断。
* **判据、阈值、结论全在程序里**（本模块）。跑一遍不联网、不花钱、可复现 ——
  同一份数据同一版判据，结论必须一模一样。
* **大模型只做辅助**：把程序抓到的公告/新闻/行业消息读一遍，挑出跟这只票有关的，
  写成一句话（`WebNote`），挂在条件与结论之间当附注。**它不改结论**，也不参与定阈值。
  没有 Key、断网、调用失败 ⇒ 结论照出，只是那一段写"未获取"。
* 每一条条件都是 `Condition`（条件名 + 门槛 + 实际值 + 满足/不满足/参考 + 数据来源），
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

#: 条件/数据的种类（哪条数字是从哪一路来的，报告里据此取舍）。
KIND_RULE = "规则"
KIND_SCORE = "评分"
KIND_FUND = "资金"
KIND_POSITION = "持仓"
KIND_MARKET = "大盘"

#: 免责声明（**唯一一份**：结论窗底部与导出的报告用的是同一句 —— 两处各写一份，
#: 改一次就会有一处落伍，而这句话是合规相关的那一句）。
DISCLAIMER = "本工具只做数据与形态的描述性统计，不构成投资建议。"

#: 报告第一行（导出文件的那一行；窗口标题另写）。
REPORT_TITLE = "luweik · 决策信号"

#: 判定结果的三态写法（条件那几行用）。
MET_TEXT = "满足"
UNMET_TEXT = "不满足"
REFERENCE_TEXT = "只作参考"


@dataclass(frozen=True)
class Condition:
    """**一条分析条件**：条件名 + 门槛 + 实际值 + 满足不满足 + 数据来源。

    为什么要结构化、而不是直接写成一串人话：用户要的是"先列条件、最后给结论"，
    而条件那一列必须**逐条能核**（`requirement` 是门槛、`actual` 是这只票的实际值、
    `source` 是这个值从哪来）。写成一串自由文本之后，谁也保证不了每只票都列全了。
    """

    name: str
    actual: str
    met: bool | None = None       # True 满足 / False 不满足 / None 只作参考（不参与判定）
    requirement: str = ""         # 门槛写法（如 `≥ 65`）；参考类条件留空
    source: str = ""              # 实际值从哪来（策略名 / 字段名 / 表名）
    kind: str = KIND_RULE

    def verdict_word(self) -> str:
        if self.met is None:
            return REFERENCE_TEXT
        return MET_TEXT if self.met else UNMET_TEXT

    def line(self, index: int = 0) -> str:
        """一行文本（条件表、报告正文共用同一份写法）。

        `source` 与正文重复时不再写一遍（`当轮策略命中：尾盘超短策略（尾盘超短策略）`
        那种括号是纯噪音，实测在出图时看出来的）。
        """
        head = f"{index}. {self.name}" if index else self.name
        if self.requirement:
            head = f"{head} {self.requirement}"
        body = f"：{self.actual}" if self.actual else ""
        tail = f"（{self.source}）" if self.source and self.source not in self.actual else ""
        return f"{head}{body}{tail} → {self.verdict_word()}" if self.met is not None else \
            f"{head}{body}{tail}（{self.verdict_word()}）"


@dataclass(frozen=True)
class WebNote:
    """大模型那条线产出的**消息附注**（不是结论，也不是判定条件）。

    程序负责把原始文本抓下来（公告、新闻、行业消息 —— 这些行情接口里没有），
    大模型负责从里面挑出跟这只票相关的、写成一句话。挑不出来就一条都不给。
    """

    title: str
    summary: str = ""
    url: str = ""
    date: str = ""
    source: str = ""

    def line(self) -> str:
        text = f"{self.date} {self.title}".strip() if self.date else self.title
        if self.summary:
            text = f"{text}，{self.summary}"
        if self.url:
            text = f"{text} {self.url}"
        return text


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


@dataclass
class Signal:
    """一只票的一份结论：**条件在前、结论在后**。"""

    symbol: str
    verdict: str
    headline: str = ""
    name: str = ""
    score: float | None = None
    as_of: str = ""
    #: 分析条件（顺序 = 判定时的检查顺序，报告与结论窗都按它列）
    conditions: list[Condition] = field(default_factory=list)
    #: 结论的限定说明（阈值未标定、数据不足…）—— 跟着结论走，不是条件
    notes: list[str] = field(default_factory=list)
    #: 大模型那条线的消息附注（可空；空 = 没取到，如实写"未获取"）
    web: list[WebNote] = field(default_factory=list)
    signal_version: str = SIGNAL_VERSION
    rules_version: str = DEFAULT_RULES.version

    def summary(self) -> str:
        """一句话（表格那一列、报告里的结论汇总用它）。"""
        return self.headline or self.verdict

    def condition_lines(self, *, numbered: bool = True) -> list[str]:
        return [c.line(index if numbered else 0)
                for index, c in enumerate(self.conditions, start=1)]

    def tip(self) -> str:
        """鼠标停在结论上 / 结论窗下半块看到的全文（**先条件、后结论**）。"""
        head = f"{self.symbol} {self.name}".strip()
        lines = [head]
        lines.append("\n【分析条件】")
        if self.conditions:
            for text in self.condition_lines():
                lines.append(f"  {text}")
        else:
            lines.append("  （这一路没有可列的条件）")
        lines.append("\n【消息】" + ("" if self.web else "未获取（未配置大模型或本次未联网；不影响下面的结论）"))
        for note in self.web:
            lines.append(f"  · {note.line()}")
        lines.append("\n【分析结论】")
        lines.append(f"  {self.verdict} —— {self.summary()}"
                     if self.summary() else f"  {self.verdict}")
        for note in self.notes:
            lines.append(f"  注：{note}")
        return "\n".join(lines)


def _score_actual(score: Any, scored: bool) -> str:
    if not scored:
        note = getattr(score, "note", "") if score is not None else ""
        return f"没有评分（{note}）" if note else "没有评分"
    return f"{float(getattr(score, 'total', 0.0)):.0f}（{getattr(score, 'advice', '') or ''}）"


def _uncalibrated_note(rules: RuleSet) -> str:
    return f"判定阈值尚未回测标定（当前 {rules.version}），只作参考。"


def judge_pool(
    symbol: str,
    *,
    name: str = "",
    score: Any = None,
    strategies: Sequence[str] = (),
    stale_days: int | None = None,
    rules: RuleSet = DEFAULT_RULES,
    extra: Iterable[Condition] = (),
    as_of: str = "",
) -> Signal:
    """自选池里的一只票 → 进场信号成立 / 继续观察 / 建议剔除。

    `strategies` 是**当轮**选出了它的策略名（`pool.round_pick_strategies()` 的那份）；
    `stale_days` 是"距最近一次命中过了多少交易日"，**`None` = 查不到命中记录**
    （按"长期没有信号"算，与"刚命中过"是两回事）。

    「建议剔除」只针对**自选池**（不再值得占着观察位），不是清仓建议 ——
    这两件事在持仓那一路上分开算，别混。
    """
    conditions: list[Condition] = []
    notes: list[str] = []
    scored = score is not None and bool(getattr(score, "ok", True))
    total = float(getattr(score, "total", 0.0)) if scored else None
    actual = _score_actual(score, scored)
    source = f"scoring v{getattr(score, 'model_version', '')}" if scored else ""

    # 条件 1/2：进场那一侧（评分门槛 + 当轮命中）—— 两条都满足才给进场信号
    enter_ok = bool(total is not None and total >= rules.enter_score)
    conditions.append(Condition(
        name="评分（进场门槛）", requirement=f"≥ {rules.enter_score:.0f}",
        actual=actual, met=enter_ok if scored else None, source=source, kind=KIND_SCORE))
    conditions.append(Condition(
        name="当轮策略命中", requirement="至少一条已勾选策略选中它",
        actual=("、".join(str(s) for s in strategies) if strategies else "没有命中记录"),
        met=bool(strategies),
        source=strategies[0] if len(strategies) == 1 else ""))

    # 条件 3/4：剔除那一侧（评分低 + 长期没信号）
    drop_ok = bool(total is not None and total <= rules.drop_score)
    conditions.append(Condition(
        name="评分（剔除门槛）", requirement=f"≤ {rules.drop_score:.0f}",
        actual=actual, met=drop_ok if scored else None, source=source, kind=KIND_SCORE))
    fresh = bool(strategies) or (stale_days is not None and stale_days <= rules.stale_days)
    conditions.append(Condition(
        name="策略命中新鲜度", requirement=f"最近 {rules.stale_days} 个交易日内有命中",
        actual=("查不到命中记录" if stale_days is None
                else f"距最近一次命中 {stale_days} 个交易日"),
        met=fresh))
    conditions.extend(extra)

    if scored and enter_ok and strategies:
        verdict, headline = ENTER, "评分达标，且当轮有策略命中。"
    elif scored and drop_ok and not fresh:
        verdict, headline = DROP, "评分偏弱，且长期没有策略命中。"
    elif not scored and not strategies:
        verdict, headline = WATCH, "数据不足，先继续观察。"
    else:
        verdict, headline = WATCH, "条件没凑齐，继续观察。"

    if not scored:
        notes.append(getattr(score, "note", "") if score is not None else "没有评分。")
    elif not strategies:
        notes.append("当轮没有策略命中；没能进场的直接原因就是这一条。")
    if not rules.calibrated:
        notes.append(_uncalibrated_note(rules))
    return Signal(
        symbol=symbol, name=name, verdict=verdict, headline=headline,
        score=total, as_of=as_of or getattr(score, "as_of", "") or "",
        conditions=conditions, notes=[n for n in notes if n],
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
    stop_pct: float | None = None,
    take_pct: float | None = None,
    rules: RuleSet = DEFAULT_RULES,
    extra: Iterable[Condition] = (),
    as_of: str = "",
) -> Signal:
    """持仓里的一只票 → 持有 / 减仓 / 离场信号成立。

    * 止损止盈（用户自己在设置里填的比例）**一旦触发就是离场信号** —— 那是用户自己定的线，
      程序不替他推翻；
    * 其余情况看评分 + 主力资金：评分弱 **且** 资金净流出才提减仓/离场，
      单看评分就喊"走"太容易被打脸（回测标定要验的就是这一条）。

    `main_net` 单位是**万元**（正 = 净流入）；`stop_pct` / `take_pct` 是用户设的比例
    （0.05 = 5%），只用来把门槛写进条件那一列。
    """
    conditions: list[Condition] = []
    notes: list[str] = []
    scored = score is not None and bool(getattr(score, "ok", True))
    total = float(getattr(score, "total", 0.0)) if scored else None
    actual = _score_actual(score, scored)
    source = f"scoring v{getattr(score, 'model_version', '')}" if scored else ""
    change = None
    if cost and price and cost > 0:
        change = (price - cost) / cost

    # 条件 1/2：用户自己定的止损止盈线（触发了就是离场，不看别的）
    stop_req = f"现价 ≤ 成本 ×(1−{stop_pct * 100:.1f}%)" if stop_pct else "现价跌破止损位"
    take_req = f"现价 ≥ 成本 ×(1+{take_pct * 100:.1f}%)" if take_pct else "现价突破止盈位"
    price_text = (f"成本 {cost:.2f} → 现价 {price:.2f}（{change * 100:+.2f}%）"
                  if change is not None else "没有成本价或现价")
    conditions.append(Condition(
        name="止损线（你自己设的）", requirement=stop_req, actual=price_text,
        met=bool(stop_hit), source="系统设置", kind=KIND_POSITION))
    conditions.append(Condition(
        name="止盈线（你自己设的）", requirement=take_req, actual=price_text,
        met=bool(take_hit), source="系统设置", kind=KIND_POSITION))

    # 条件 3/4：评分与资金（两条同时成立才算"弱且在被卖"）
    exit_ok = bool(total is not None and total <= rules.exit_score)
    trim_ok = bool(total is not None and total <= rules.trim_score)
    conditions.append(Condition(
        name="评分（离场门槛）", requirement=f"≤ {rules.exit_score:.0f}", actual=actual,
        met=exit_ok if scored else None, source=source, kind=KIND_SCORE))
    conditions.append(Condition(
        name="主力资金净流出", requirement="主力净额 < 0",
        actual=(f"{main_net:+.0f} 万元" if main_net is not None else "没有资金流数据"),
        met=(main_net < 0) if main_net is not None else None, source="资金流", kind=KIND_FUND))
    conditions.append(Condition(
        name="评分（减仓门槛）", requirement=f"≤ {rules.trim_score:.0f}", actual=actual,
        met=trim_ok if scored else None, source=source, kind=KIND_SCORE))
    if shares:
        conditions.append(Condition(name="持有数量", actual=f"{shares} 股", met=None,
                                    source="持仓表", kind=KIND_POSITION))
    conditions.extend(extra)

    outflow = main_net is not None and main_net < 0
    if stop_hit:
        verdict, headline = EXIT, "触发你设置的止损线。"
    elif take_hit:
        verdict, headline = EXIT, "触发你设置的止盈线。"
    elif exit_ok and outflow:
        verdict, headline = EXIT, "评分弱，且主力资金净流出。"
    elif trim_ok and outflow:
        verdict, headline = TRIM, "评分转弱，且主力资金净流出。"
    elif not scored and main_net is None:
        verdict, headline = HOLD, "没有评分与资金数据，暂按持有。"
    else:
        verdict, headline = HOLD, "没有触发离场条件。"

    if not scored:
        notes.append(getattr(score, "note", "") if score is not None else "没有评分。")
    if main_net is None:
        notes.append("没有这只票的资金流（只对自选标的采集），离场那两条判据用不上。")
    if not rules.calibrated:
        notes.append(_uncalibrated_note(rules))
    return Signal(
        symbol=symbol, name=name, verdict=verdict, headline=headline,
        score=total, as_of=as_of or getattr(score, "as_of", "") or "",
        conditions=conditions, notes=[n for n in notes if n],
        rules_version=rules.version, signal_version=SIGNAL_VERSION,
    )


def attach_web(signal: Signal, notes: Iterable[WebNote]) -> Signal:
    """把大模型那条线的消息附注挂到结论上（`Signal` 是可变对象，这里返回同一个实例）。

    附注**只进 `signal.web`**，不参与任何判定 —— 这样"关掉大模型，结论一模一样"
    这条纪律才成立（`tests/test_analysis.py` 钉着这一条）。
    """
    for note in notes or ():
        signal.web.append(note)
    return signal


def with_rules(rules: RuleSet, **changes: Any) -> RuleSet:
    """改几个阈值（回测标定后落库、或用户在设置里微调时用）。"""
    return replace(rules, **changes)


def format_line(signal: Signal) -> str:
    """一行文本（消息列表、导出、CLI 用同一份）。"""
    return f"{signal.symbol} {signal.name}｜{signal.verdict}｜{signal.summary()}".strip()


#: 报告里那句"阈值没标定"的提醒：与判定写进 `notes` 的那句是同一件事，
#: 报告里只在结论汇总里提一次（每只票下面再重复一遍，十只票就是十遍噪音）。
_UNCALIBRATED_HINT = "尚未回测标定"


def report_text(
    signals: Iterable[Signal],
    *,
    title: str = REPORT_TITLE,
    day: str | None = None,
    generated_at: str | None = None,
    disclaimer: str = DISCLAIMER,
    include_web: bool = True,
) -> str:
    """一批结论 → **导出用的纯文本报告**（纯函数：不碰磁盘、不联网、不读配置）。

    版式（用户 2026-10-13：「先列出分析条件，最后给出分析结论，那样才显得专业」）::

        luweik · 决策信号（自选标的） · 2026-10-13
        生成时间 …（北京时间，手动分析）
        判据版本：provisional-0

        一、分析条件
        600519 贵州茅台
          1. 评分（进场门槛） ≥ 65：78（偏强）（scoring v1.0.0） → 满足
          …

        二、分析结论
          进场信号成立：600519 贵州茅台 —— 评分达标，且当轮有策略命中。
          共 3 只：进场信号成立 1 · 继续观察 1 · 建议剔除 1
          ⚠️ 判定阈值尚未回测标定，结论只作参考。

        （本文件由程序自动生成；本工具只做数据与形态的描述性统计，不构成投资建议。）

    无结论时返回**空串**（调用方据此说"还没有可导出的结论"，而不是导出一个空文件）。
    """
    items = [s for s in (signals or []) if getattr(s, "symbol", "")]
    if not items:
        return ""
    if day is None or generated_at is None:
        try:                        # 时钟只在这里读一次；显式传入时完全可控（测试用）
            from laoa_trader import intraday

            now = intraday.now_shanghai()
            day = day or now.strftime("%Y-%m-%d")
            generated_at = generated_at or now.strftime("%Y-%m-%d %H:%M")
        except Exception:           # noqa: BLE001 - 取不到时间不该让报告出不来
            day = day or ""
            generated_at = generated_at or ""

    lines = [f"{title} · {day}".rstrip(" ·")]
    if generated_at:
        lines.append(f"生成时间 {generated_at}（北京时间，手动分析）")
    versions = sorted({s.rules_version for s in items if getattr(s, "rules_version", "")})
    if versions:
        lines.append(f"判据版本：{'、'.join(versions)}")

    # ── 一、分析条件 ──
    lines.append("")
    lines.append("一、分析条件")
    for signal in items:
        lines.append("")
        lines.append(f"{signal.symbol} {signal.name}".rstrip())
        for text in signal.condition_lines():
            lines.append(f"  {text}")
        if include_web:
            if signal.web:
                for note in signal.web:
                    lines.append(f"  · 消息 {note.line()}")
            else:
                lines.append("  · 消息：未获取（未配置大模型或本次未联网）")

    # ── 二、分析结论 ──
    lines.append("")
    lines.append("二、分析结论")
    for signal in items:
        lines.append(f"  {signal.verdict}：{signal.symbol} {signal.name}".rstrip()
                     + f" —— {signal.summary()}")
        for note in signal.notes:
            if _UNCALIBRATED_HINT in note:      # 汇总里提一次（见常量说明）
                continue
            lines.append(f"    注：{note}")
    counts: dict[str, int] = {}
    for signal in items:
        counts[signal.verdict] = counts.get(signal.verdict, 0) + 1
    lines.append("")
    lines.append("共 %d 只：%s" % (
        len(items),
        " · ".join(f"{word} {counts[word]}" for word in VERDICTS if word in counts),
    ))
    if any(any(_UNCALIBRATED_HINT in note for note in s.notes) for s in items):
        lines.append("⚠️ 判定阈值尚未回测标定，结论只作参考。")

    lines.append("")
    lines.append(f"（本文件由程序自动生成；{disclaimer}）")
    return "\n".join(lines) + "\n"
