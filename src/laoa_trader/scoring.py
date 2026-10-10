"""个股评分：把"这只票现在怎么样"折成 0~100 的一个数 + **每一项为什么**。

主人 2026-10-11 的要求
----------------------
"做进界面，自选标的 / 持仓监控加一列评分，加入自动给出。"（构架见工作区
`新功能构架-评分与热力图.md` 第 2 节，这一版按那份构架实现。）

四条硬规矩（改任何一条都会让这个分数变得不能信）
------------------------------------------------
1. **可解释**：每一项都带一句人话理由（"站上 20 日线（12.34 > 12.01）+1"）——
   界面上鼠标停上去就能看全，不是甩给用户一个孤零零的 78 分。
2. **缺项不算 0**：拿不到数据的项标 `available=False`，总分按**已得分的权重重归一**，
   并把缺了哪些项写进 `missing`。把缺项当 0 分，等于给"没配 Key/没采到资金流"的用户
   判一个错的分 —— 那比不给分更糟。
3. **口径与选股一致**：序列一律走 `formulas.load_series()`（后复权 + 盘中拼今天那根），
   扩展字段走 `formulas.snapshot_extra()` / `fund_flow_extra()`。
   评分说金叉、选股说没有，是最难查的那种不一致。
4. **带模型版本**：`MODEL_VERSION` 改了项/权重/口径就 +0.01，评分结果里带着它 ——
   否则用户拿两周前的分和今天比，会得出错误结论。

为什么每一项都写成**公式**（而不是 Python 里的 if-else）
--------------------------------------------------------
构架里定的：能用公式表达的就不写第二套实现。好处有三：
* **口径天然一致**：用的是同一个引擎、同一份 K 线；
* **用户能在「策略筛选」页里看见并改**：每一项就是一条公式，不是黑盒；
* **测试直接复用 `test_formula` 那一套**：编译 + 逐位断言。
每项公式的返回值统一是 **-1 / 0 / +1**（看空 / 中性 / 看多），
评分层只做"加权平均 → 归一 → 0~100"，规则简单到不可能算错。

维度与权重
----------
`GROUPS` 里的权重来自材料 `源码分析/股票决策系统/综合功能/指标说明.md` 的默认权重
（趋势 > 动量 > 量价 > 资金 > 形态），同一维里各小项的权重按"材料里给的优先级 +
本项目的取数能力"定；个股级主力净额只有自选标的才采得到（`fund_flow_extra`），
采不到就是缺项，不是 0 分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from laoa_trader import formulas
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm

logger = get_logger(__name__)

#: 模型版本：**改了项、权重、口径就要 +0.01**（评分结果与界面上都带着它）
MODEL_VERSION = "1.0.0"

#: 维度的中文名与权重（材料里的默认权重：趋势 > 动量 > 量价 > 资金 > 形态）
GROUPS: dict[str, float] = {
    "趋势": 1.2,
    "动量": 1.0,
    "量价": 0.9,
    "资金": 0.8,
    "形态": 0.7,
}

#: 建议档位（**只描述强弱，不写成"买入/卖出"** —— 这个软件不是荐股工具，
#: 用"强势/偏弱"这种描述性说法，用户拿它当参考而不是当指令）
ADVICE_LEVELS: tuple[tuple[float, str], ...] = (
    (80.0, "强势"),
    (65.0, "偏强"),
    (45.0, "中性"),
    (30.0, "偏弱"),
    (0.0, "弱势"),
)


@dataclass(frozen=True)
class ItemSpec:
    """一个评分项：**一对布尔公式**（看多条件 / 看空条件）+ 展示用的名字与权重。

    为什么是"一对布尔"而不是一条返回 -1/0/+1 的公式：公式引擎是**筛选**引擎，
    策略最后一行必须是返回 0/1 的条件（写 `IF(...,1,-1)` 会被它拒绝，实测过）。
    拆成两个条件反而更好懂：每一项就是"什么算看多、什么算看空"，
    用户能在「策略筛选」页里逐条读、逐条改。
    """

    name: str
    group: str
    weight: float
    bull: str                # 看多条件
    bear: str                # 看空条件
    #: 理由里带上哪个数（`"close"` = 收盘价；其余是扩展字段名，如 `换手率`）。
    #: **只带原始数据**，不带需要再算一遍的指标值 —— 指标的结论已经由公式给了，
    #: 再自己算一套显示出来，两边口径一旦有差就是"理由与结论打架"（最难解释的 bug）。
    detail: str = ""
    #: 这一项**至少**要多少根 K 线（0 = 用引擎自己算出来的 `formula.min_history`）。
    #: 什么时候要手写：引擎那边 `hist_extra` 只能加常数，`ADX(14)` 报的是下界 15，
    #: 而它真正要 28 根（两级 Wilder 平滑）—— 不写这一档的话，
    #: "日线 20 根"的票会被算成"ADX 中性"，那是把数据不足当成结论。
    min_bars: int = 0
    #: 这一项缺数据时怎么解释（写进 `missing`）
    why_missing: str = "本地日线不足"


#: 全部评分项。公式里只用**引擎已有的函数** + 扩展字段；
#: `ADX/PDI/MDI/MFI` 是 2026-10-11 补进引擎的（还没有它们时那几项自动算缺项，
#: 见 `_compile`：编译失败 = 这项不可用，绝不让整只票算不出来）。
ITEMS: tuple[ItemSpec, ...] = (
    # ── 趋势 ──
    ItemSpec("均线多头排列", "趋势", 1.0,
             "MA(C,5)>MA(C,10) AND MA(C,10)>MA(C,20)",
             "MA(C,5)<MA(C,10) AND MA(C,10)<MA(C,20)", detail="close"),
    ItemSpec("站上 20 日线", "趋势", 0.8, "C>MA(C,20)", "C<MA(C,20)", detail="close"),
    ItemSpec("站上 60 日线", "趋势", 0.7, "C>MA(C,60)", "C<MA(C,60)", detail="close",
             why_missing="本地日线不足 60 根"),
    ItemSpec("EMA12 / EMA26", "趋势", 0.7,
             "EMA(C,12)>EMA(C,26)", "EMA(C,12)<EMA(C,26)", detail="close"),
    ItemSpec("ADX 趋势强度", "趋势", 1.2,
             "ADX(14)>=25 AND PDI(14)>MDI(14)", "ADX(14)>=25 AND PDI(14)<MDI(14)",
             min_bars=28, why_missing="引擎缺 ADX/PDI/MDI 或日线不足 28 根"),
    # ── 动量 ──
    ItemSpec("MACD 方向与柱状", "动量", 1.0,
             "DIF()>DEA() AND MACD()>REF(MACD(),1)",
             "DIF()<DEA() AND MACD()<REF(MACD(),1)", detail="close"),
    ItemSpec("RSI(14) 位置", "动量", 0.6, "RSI(C,14)<30", "RSI(C,14)>70"),
    ItemSpec("KDJ 的 J 值", "动量", 0.5, "J()<20", "J()>80",
             why_missing="日线不足 9 根"),
    # ── 量价 ──
    ItemSpec("放量方向", "量价", 0.8,
             "V>MA(V,20)*1.5 AND C>REF(C,1)", "V>MA(V,20)*1.5 AND C<REF(C,1)",
             detail="close", why_missing="本地日线不足 20 根"),
    ItemSpec("OBV 资金方向", "量价", 0.6, "OBV()>MA(OBV(),10)", "OBV()<MA(OBV(),10)",
             why_missing="本地日线不足 11 根"),
    # UB/LB 在这个引擎里是 `UB(X,N,P)`（X = 用哪条序列算，N 周期，P 倍数）
    ItemSpec("布林带位置", "量价", 0.5, "C<LB(C,20,2)", "C>UB(C,20,2)",
             detail="close", why_missing="本地日线不足 20 根"),
    ItemSpec("MFI(14) 资金强弱", "量价", 0.6, "MFI(14)<20", "MFI(14)>80",
             why_missing="引擎缺 MFI 或日线不足 15 根"),
    # ── 资金（个股级主力净额只有自选标的采得到 → 采不到算缺项，不是 0 分）──
    ItemSpec("换手活跃度", "资金", 0.5,
             "换手率>=3 AND 换手率<=15", "换手率>20", detail="换手率",
             why_missing="没取到换手率（要盘中快照）"),
    ItemSpec("主力资金净额", "资金", 0.7, "主力净额>0", "主力净额<0", detail="主力净额",
             why_missing="这只票还没采到资金流（自选标的在日更后才有）"),
    # ── 形态 ──
    ItemSpec("长下影 / 长上影", "形态", 0.8,
             "(MIN(O,C)-L)/(H-L+0.0001)>0.6 AND C>REF(C,1)",
             "(H-MAX(O,C))/(H-L+0.0001)>0.6 AND C<REF(C,1)", detail="close"),
    ItemSpec("三连阳 / 三连阴", "形态", 0.6,
             "UPNDAY(C,3) AND UPNDAY(V,3)", "DOWNNDAY(C,3)",
             why_missing="本地日线不足 4 根"),
    ItemSpec("阳包阴 / 阴包阳", "形态", 0.7,
             "C>O AND REF(C,1)<REF(O,1) AND C>REF(O,1) AND O<REF(C,1)",
             "C<O AND REF(C,1)>REF(O,1) AND C<REF(O,1) AND O>REF(C,1)",
             detail="close", why_missing="本地日线不足 2 根"),
)


@dataclass
class ScoreItem:
    """一项的结论。"""

    name: str
    group: str
    weight: float
    score: float            # -1 / 0 / +1（缺项时无意义）
    reason: str             # 一句人话
    available: bool = True


@dataclass
class ScoreGroup:
    """一个维度的结论（维度分同样是归一后的 0~100，便于画条形）。"""

    name: str
    weight: float
    score: float            # 0~100
    items: list[ScoreItem] = field(default_factory=list)


@dataclass
class ScoreResult:
    """一只票的评分结果（界面、CLI、导出都用它）。"""

    symbol: str
    total: float = 0.0                       # 0~100
    advice: str = ""                         # 强势/偏强/中性/偏弱/弱势
    groups: list[ScoreGroup] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    model_version: str = MODEL_VERSION
    as_of: str = ""                          # 评分用的是哪天的行情
    caliber: str = ""                        # 口径说明（与筛选那句同一个来源）
    note: str = ""                           # 算不出来时的原因（如"本地没有这只票的日线"）

    @property
    def ok(self) -> bool:
        return not self.note

    def tip(self) -> str:
        """鼠标停在这一格上看到的全文（维度 + 逐项理由 + 缺项说明）。"""
        if not self.ok:
            return self.note
        lines = [f"评分 {self.total:.0f}（{self.advice}） · 模型 v{self.model_version}",
                 f"行情日 {self.as_of} · {self.caliber}"]
        for group in self.groups:
            lines.append(f"\n【{group.name}】{group.score:.0f}")
            for item in group.items:
                mark = "" if item.available else "（缺）"
                lines.append(f"  · {item.reason}{mark}")
        if self.missing:
            lines.append("\n缺项（按已得分的权重重归一，没当 0 分算）："
                         + "、".join(self.missing))
        return "\n".join(lines)


def advice_for(total: float) -> str:
    """总分 → 一句话档位（只描述强弱，不是买卖建议）。"""
    for floor, text in ADVICE_LEVELS:
        if total >= floor:
            return text
    return ADVICE_LEVELS[-1][1]


#: 编译好的公式缓存：`{文本: Formula | None}`（None = 编译不过 → 这项不可用）。
#: 为什么缓存：一次要给几十只票打分，每只票 17 项 × 2 条公式，编译是纯浪费。
_COMPILED: dict[str, Any] = {}


def score_formulas() -> list[Any]:
    """把评分项编译成公式对象（给 `formulas.prepare_inputs` 判断"要用哪些字段"）。

    为什么把编译结果交给它：`prepare_inputs` 只看"公式里出现了哪些字段"来决定
    要不要取快照/资金流（没用到就不发请求）。评分项里有 `换手率` 与 `主力净额`，
    所以这里必须把真实公式给它 —— 传个空表就会"什么都不取"，那两项永远缺项。
    """
    out: list[Any] = []
    for spec in ITEMS:
        for text in (spec.bull, spec.bear):
            compiled = _compile(text)
            if compiled is not None:
                out.append(compiled)
    return out


def _compile(text: str) -> Any:
    """编译一条评分公式；**编译不过返回 None**（那一项按缺项处理，绝不抛出去）。

    为什么容错：评分项里用到的函数是引擎的公共资产（`ADX`/`MFI` 这类是后补进去的）。
    将来谁把某个函数删了、或用户在别的分支上跑，评分不该整只票算不出来 ——
    少一项、写明缺项，用户看到的仍然是一个能用的分。
    """
    if text in _COMPILED:
        return _COMPILED[text]
    try:
        compiled = fm.compile_formula(text, name="评分项")
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        logger.warning(f"评分项公式编译失败（这一项按缺项算）：{text} —— {exc}")
        compiled = None
    _COMPILED[text] = compiled
    return compiled


#: "扩展字段"（只有最后一个位置有值的那些）：评分项用到它们时，缺了就是缺项。
#: 判据直接复用 `formulas` 里那两份名单 —— 两边各写一份迟早对不上。
_EXTRA_FIELDS: frozenset[str] = frozenset(
    tuple(getattr(formulas, "SNAPSHOT_FIELDS", ())) + tuple(
        getattr(formulas, "FUND_FLOW_FIELDS", ()))
)


def _availability(series: Any, formula_obj: Any, spec: ItemSpec | None = None) -> str:
    """这一项算不算得出来：**空串 = 算得出来**，否则是"为什么算不出来"。

    为什么要单独判一遍，不能只看公式的求值结果：比较运算遇到 NaN 会得到 **False**
    （不是 NaN），于是"日线只有 5 根"时 `C>MA(C,20)` 老实返回 0 ——
    只看结果的话这一项会被当成"中性"（**等于把数据不足当成了真实结论**）。
    所以这里按引擎自己的两份元信息判：

    * `formula.min_history`：这条公式至少要多少根 K 线（引擎按函数参数算出来的）；
    * `formula.fields` 里有没有**扩展字段**（换手率/主力净额…）：那类字段"只有最后一个
      位置有值"，没取到时是 NaN —— 同样要判成缺项。
    """
    needed = max(int(getattr(formula_obj, "min_history", 0) or 0),
                 int(getattr(spec, "min_bars", 0) or 0))
    if len(getattr(series, "date", ()) or ()) < needed:
        return f"本地日线不足 {needed} 根"
    extra = getattr(series, "extra", None) or {}
    for name in getattr(formula_obj, "fields", ()) or ():
        if name not in _EXTRA_FIELDS:
            continue
        value = extra.get(name)
        missing = _last(value) if hasattr(value, "__len__") else _last_number(value)
        if missing is None:
            return f"没取到 {name}"
    return ""


def _last(value: Any) -> float | None:
    """取序列最后一个值；拿不到/非有限数返回 None。"""
    import math

    try:
        array = value if hasattr(value, "__len__") else None
        if array is None or len(array) == 0:
            return None
        number = float(array[-1])
    except Exception:  # noqa: BLE001
        return None
    return number if math.isfinite(number) else None


def _fmt(number: float) -> str:
    """理由里的数字：大数（成交额/资金）留一位小数，其余两位，整数不带小数点。"""
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    if abs(number) >= 100:
        return f"{number:.1f}"
    return f"{number:.2f}".rstrip("0").rstrip(".") if number % 1 else f"{number:.0f}"


def _detail_value(series: Any, key: str) -> float | None:
    """理由里要显示的那个数：`close` 取最后一根收盘，其余取扩展字段（换手率/主力净额）。"""
    if not key:
        return None
    if key == "close":
        return _last(series.close)
    value = (getattr(series, "extra", None) or {}).get(key)
    # 扩展字段是"铺成一条序列、只有最后一个位置有值"（见 `formulas.load_series`），
    # 所以先按序列取最后一个；万一给的是标量，也认（调用方手搓序列时方便）
    return _last(value) if hasattr(value, "__len__") else _last_number(value)


def _last_number(value: Any) -> float | None:
    """扩展字段是"只有一个数"的（见 `formulas.snapshot_extra`）：直接取它。"""
    import math

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _direction(score: float) -> str:
    if score > 0:
        return "看多"
    if score < 0:
        return "看空"
    return "中性"


def score_series(series: Any) -> ScoreResult:
    """给一只票的序列打分（**纯计算，不联网、不查库**）。"""
    result = ScoreResult(symbol=series.symbol,
                         as_of=str(series.date[-1]) if series.date else "")
    by_group: dict[str, list[ScoreItem]] = {}

    def unavailable(spec: ItemSpec) -> None:
        item = ScoreItem(spec.name, spec.group, spec.weight, 0.0,
                         f"{spec.name}：{spec.why_missing}", available=False)
        result.missing.append(spec.name)
        by_group.setdefault(spec.group, []).append(item)

    for spec in ITEMS:
        bull, bear = _compile(spec.bull), _compile(spec.bear)
        if bull is None or bear is None:
            unavailable(spec)
            continue
        # 数据够不够：见 `_availability`（比较运算遇 NaN 会返回 False，
        # 只看求值结果会把"数据不足"误判成"中性"）
        why = _availability(series, bull, spec) or _availability(series, bear, spec)
        if why:
            item = ScoreItem(spec.name, spec.group, spec.weight, 0.0,
                             f"{spec.name}：{why}", available=False)
            result.missing.append(spec.name)
            by_group.setdefault(spec.group, []).append(item)
            continue
        up, down = _last(bull.eval(series)), _last(bear.eval(series))
        if up is None or down is None:
            unavailable(spec)
            continue
        score = 1.0 if up else (-1.0 if down else 0.0)
        detail = ""
        shown = _detail_value(series, spec.detail)
        if shown is not None:
            label = "收盘" if spec.detail == "close" else spec.detail
            detail = f"（{label} {_fmt(shown)}）"
        reason = f"{spec.name}{detail}：{_direction(score)}"
        by_group.setdefault(spec.group, []).append(
            ScoreItem(spec.name, spec.group, spec.weight, score, reason))

    # ── 归一：只按**可用项**的权重算（缺项不进分母），再映射到 0~100 ──
    weighted = 0.0
    weight_sum = 0.0
    for group_name, group_weight in GROUPS.items():
        items = by_group.get(group_name) or []
        usable = [item for item in items if item.available]
        group_weighted = sum(item.score * item.weight for item in usable)
        group_weight_available = sum(item.weight for item in usable)
        if group_weight_available > 0:
            weighted += (group_weighted / group_weight_available) * group_weight
            weight_sum += group_weight
            group_score = (group_weighted / group_weight_available + 1) * 50
        else:
            group_score = 0.0
        result.groups.append(ScoreGroup(group_name, group_weight, group_score, items))

    if weight_sum <= 0:
        result.note = "本地没有这只票的日线数据，算不出评分"
        result.groups = []
        return result
    result.total = round((weighted / weight_sum + 1) * 50, 1)
    result.advice = advice_for(result.total)
    return result


def score_symbols(
    cfg: Any,
    db_path: Any,
    symbols: Sequence[str],
    *,
    prepared: formulas.Prepared | None = None,
) -> dict[str, ScoreResult]:
    """给一批票打分（读本地库 + 取快照/资金流扩展字段；**不写库、不改任何状态**）。

    Args:
        cfg: 配置（拿它去取快照与资金流的口径；None = 只用日线）。
        db_path: 本地库。
        symbols: 要评的代码（通常就是"界面上看得见的那几只"）。
        prepared: 已经准备好的输入（筛选刚跑完时可以复用，省一趟取数）。

    Returns:
        `{代码: ScoreResult}`；库里没有那只票时返回带 `note` 的结果（不是抛异常）。
    """
    wanted = [str(s) for s in symbols if str(s)]
    if not wanted:
        return {}
    # 取数这一趟**复用筛选那条路**（`prepare_inputs`）：快照、资金流、盘中那根 K 线、
    # 口径那句话都由它一处决定 —— 评分与选股必须同一份输入，否则会出现
    # "评分说金叉、选股说没有"这种最难查的不一致。它自己会按"公式用到哪些字段"
    # 决定要不要发请求（一条都不用到快照就不发）。
    if prepared is None:
        try:
            prepared = formulas.prepare_inputs(
                cfg, db_path, formulas=score_formulas(), symbols=wanted)
        except Exception as exc:  # noqa: BLE001 - 取不到快照/资金流也要能给出日线那几维
            logger.warning(f"评分准备输入失败（按日线口径算）：{exc}")
            prepared = formulas.Prepared()

    extra: dict[str, dict[str, float]] = dict(prepared.extra or {})
    caliber = prepared.caliber
    today = prepared.today
    live_bars = prepared.live_bars
    kline_day = prepared.kline_day

    out: dict[str, ScoreResult] = {}
    try:
        series_iter: Iterable[Any] = fm.load_series(
            db_path, wanted, extra=extra, today=today, live_bars=live_bars,
            kline_day=kline_day,
        )
    except Exception as exc:  # noqa: BLE001 - 库坏了也要给出可读结论
        logger.warning(f"评分读日线失败：{exc}")
        return {symbol: ScoreResult(symbol=symbol, note=f"读日线失败：{exc}") for symbol in wanted}

    for series in series_iter:
        result = score_series(series)
        result.caliber = caliber or result.caliber
        out[series.symbol] = result
    for symbol in wanted:
        out.setdefault(symbol, ScoreResult(symbol=symbol, note="本地没有这只票的日线数据"))
    return out


def cache_key(result: ScoreResult) -> tuple[str, str, str]:
    """缓存键：`(代码, 行情日, 模型版本)` —— 三者任一变了就该重算。"""
    return (result.symbol, result.as_of, result.model_version)


def format_line(result: ScoreResult) -> str:
    """一行文字（CLI 与日志用）：`600519 78 偏强（模型 v1.0.0）`。"""
    if not result.ok:
        return f"{result.symbol} —（{result.note}）"
    return f"{result.symbol} {result.total:.0f} {result.advice}（模型 v{result.model_version}）"
def score_symbols(
    cfg: Any,
    db_path: Any,
    symbols: Sequence[str],
    *,
    prepared: formulas.Prepared | None = None,
) -> dict[str, ScoreResult]:
    """给一批票打分（读本地库 + 取快照/资金流扩展字段；**不写库、不改任何状态**）。

    Args:
        cfg: 配置（拿它去取快照与资金流的口径；None = 只用日线）。
        db_path: 本地库。
        symbols: 要评的代码（通常就是"界面上看得见的那几只"）。
        prepared: 已经准备好的输入（筛选刚跑完时可以复用，省一趟取数）。

    Returns:
        `{代码: ScoreResult}`；库里没有那只票时返回带 `note` 的结果（不是抛异常）。
    """
    wanted = [str(s) for s in symbols if str(s)]
    if not wanted:
        return {}
    # 取数这一趟**复用筛选那条路**（`prepare_inputs`）：快照、资金流、盘中那根 K 线、
    # 口径那句话都由它一处决定 —— 评分与选股必须同一份输入，否则会出现
    # "评分说金叉、选股说没有"这种最难查的不一致。它自己会按"公式用到哪些字段"
    # 决定要不要发请求（一条都不用到快照就不发）。
    if prepared is None:
        try:
            prepared = formulas.prepare_inputs(
                cfg, db_path, formulas=score_formulas(), symbols=wanted)
        except Exception as exc:  # noqa: BLE001 - 取不到快照/资金流也要能给出日线那几维
            logger.warning(f"评分准备输入失败（按日线口径算）：{exc}")
            prepared = formulas.Prepared()

    extra: dict[str, dict[str, float]] = dict(prepared.extra or {})
    caliber = prepared.caliber
    today = prepared.today
    live_bars = prepared.live_bars
    kline_day = prepared.kline_day

    out: dict[str, ScoreResult] = {}
    try:
        series_iter: Iterable[Any] = fm.load_series(
            db_path, wanted, extra=extra, today=today, live_bars=live_bars,
            kline_day=kline_day,
        )
    except Exception as exc:  # noqa: BLE001 - 库坏了也要给出可读结论
        logger.warning(f"评分读日线失败：{exc}")
        return {symbol: ScoreResult(symbol=symbol, note=f"读日线失败：{exc}") for symbol in wanted}

    for series in series_iter:
        result = score_series(series)
        result.caliber = caliber or result.caliber
        out[series.symbol] = result
    for symbol in wanted:
        out.setdefault(symbol, ScoreResult(symbol=symbol, note="本地没有这只票的日线数据"))
    return out


def cache_key(result: ScoreResult) -> tuple[str, str, str]:
    """缓存键：`(代码, 行情日, 模型版本)` —— 三者任一变了就该重算。"""
    return (result.symbol, result.as_of, result.model_version)


def format_line(result: ScoreResult) -> str:
    """一行文字（CLI 与日志用）：`600519 78 偏强（模型 v1.0.0）`。"""
    if not result.ok:
        return f"{result.symbol} —（{result.note}）"
    return f"{result.symbol} {result.total:.0f} {result.advice}（模型 v{result.model_version}）"
