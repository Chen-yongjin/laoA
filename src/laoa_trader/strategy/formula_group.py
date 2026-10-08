"""「公式」组：把用户在界面上勾选的公式跑成**池子认的候选**。

为什么单独一个模块
------------------
`strategy/formula.py` 是引擎（只认文本 → 数组），`formulas.py` 是公式库
（文件、配置、试算、成绩单），而"把勾选的公式跑成一批候选、失败的那些只记日志"
这件事同时要碰 **配置 + 库 + 池子**，塞进任何一边都会让那一边变脏。
所以放这里，并且只暴露一个函数：`run_enabled_formulas()`。

两条硬规矩
----------
1. **默认不参与**：`config.toml` 里 `enabled_formulas` 为空时，本方**一次库都不读**
   （此时匹配只剩自选标的，有测试钉住）。
2. **失败隔离**：某条公式在运行时抛 `FormulaError` / `FormulaDataError`
   （典型例子：用了历史不完整的 `连板()`、或那只票的序列长度对不上），
   只做三件事 —— 记日志、把它记进 `status`（界面上标红给原因）、
   把它从本批候选里去掉。**其余公式照常出票，建池也不会失败。**

   为什么非要隔离：公式是**用户自己写的**，写错是常态而不是异常。一条写坏的公式
   让整轮匹配一起失败，等于"用户越敢试错，程序越不能用" ——
   与 `formula.py` 里"逐文件报错、坏的不会拖垮好的"是同一条思路，只是这次
   发生在**运行期**而不是解析期。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laoa_trader import formulas as lib
from laoa_trader import market_regime
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm

logger = get_logger(__name__)

#: 公式候选在池子里的权重。为什么是 2（而不是 1）：候选合成是按"策略内排名 × 权重"
#: 打分的，权重越高越不容易被别的公式挤到池子 10 只之外
#: （表现就是"我勾了公式，池子里却没有"）。给 2 是当年与「短期反转」同档的取值，
#: 现在所有候选都是公式，权重一视同仁 —— 但**不改它**：改了会让同一批候选的排序变化。
FORMULA_WEIGHT = 2

#: 同一条公式最多进池几只（与 `pool.MAX_PER_STRATEGY` 同一个口径，单独写一份是
#: 为了"公式多了之后想单独收紧"时不用动内置策略的常数）
MAX_PER_FORMULA = 3

# ── 运行态：上一次跑公式组的结果 ──
#
# 为什么用模块级字典（而不是落库/写文件）：它回答的是"刚刚这一轮里哪条公式出错了"，
# 页面刷新时拿来把那一行标红就够了；进程重启后历史错误没有意义（重新跑一次就有新的）。
# 与 `state.py` 的"下载中"旗子是同一个思路：**跨线程可见的少量运行态**，只此一处。
_lock = threading.Lock()
_last_status: dict[str, str] = {}


# ── 公式合成名（`放量上攻` → `公式·放量上攻`）──
#
# 这四个 helper 原先住在 `strategy/groups.py` 里（借"策略组"的地盘）：池子里的候选是
# 按"策略名"组织的，公式于是被合成成一个带前缀的策略名，来源列/排序/推送全都能复用
# 现有逻辑。2026-09-18 策略组机制整体删掉之后，它们**语义上的家**就是这里
# （跑公式的模块），所以搬了过来；`pool.py` / `intraday.py` / `scheduler.py` 都从这里 import。
FORMULA_PREFIX = "公式·"


def formula_strategy_name(formula_name: str) -> str:
    """公式名 → 池子/推送里的**合成策略名**（`放量上攻` → `公式·放量上攻`）。

    为什么不直接用公式名当策略名：来源列里出现一个裸名字，用户分不清"这是我写的公式"
    还是别的什么东西。带上 `公式·` 前缀，推送与卡片上一眼就能看出来源。
    """
    return f"{FORMULA_PREFIX}{str(formula_name or '').strip()}"


def is_formula_strategy(class_name: str) -> bool:
    """这个策略名是不是"自定义公式"（合成名以 `公式·` 开头）。"""
    return str(class_name or "").startswith(FORMULA_PREFIX)


def formula_name_of(class_name: str) -> str:
    """合成策略名 → 公式名（不是公式时原样返回）。"""
    name = str(class_name or "")
    return name[len(FORMULA_PREFIX):] if name.startswith(FORMULA_PREFIX) else name


def last_status() -> dict[str, str]:
    """上一次运行里 {公式名: 失败原因}（成功的不在里面）。"""
    with _lock:
        return dict(_last_status)


def reset_status() -> None:
    """清掉运行态（测试用；正常流程由 `run_enabled_formulas` 覆盖写入）。"""
    with _lock:
        _last_status.clear()


@dataclass
class FormulaRun:
    """一次「公式」组运行的结果。"""

    #: {合成策略名: [{"symbol","name","reason"}...]}（`公式·放量上攻` → 候选）
    picks: dict[str, list[dict]] = field(default_factory=dict)
    #: 中文错误清单（进 `report["errors"]` 与日志，**不阻断流程**）
    errors: list[str] = field(default_factory=list)
    #: {公式名: 失败原因}（界面标红；只在运行期真出错时才有条目）
    status: dict[str, str] = field(default_factory=dict)
    #: 本次真正参与运行的公式名（按目录顺序）
    ran: list[str] = field(default_factory=list)
    #: 扫过的票数（日志用）
    scanned: int = 0
    #: 这一轮用的 K 线口径（一句中文：盘中实时 / 日 K 线，见 `formulas.prepare_inputs`）。
    #: 界面上要显示出来 —— "同一份策略、同一个按钮，昨天选出的和今天选出的为什么不一样"
    #: 这个问题只有口径能解释。
    caliber: str = ""
    #: **告知**（不是错误）：典型是"盘中想用实时数据，但取不到快照 → 已退回日 K 线"。
    #: 与 `errors` 分开的理由：建池的"成功/失败"判据是"errors 是否为空"
    #: （`scheduler.Scheduler._report_succeeded`），把一句告知塞进 errors 会让一次
    #: **正常完成**的匹配被判成失败（票选出来了、推送也发了，状态却写"未成功"再补跑一遍）。
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.picks


def run_enabled_formulas(
    db_path: str | Path,
    cfg: Any = None,
    *,
    directory: str | Path | None = None,
) -> FormulaRun:
    """跑 `enabled_formulas` 里的公式，返回池子候选（**不写库**）。

    实现要点：
    * **一遍扫描**：`load_series()` 逐只产出，几条公式共用同一个 `Series`
      （每条公式单独读一遍库要多花几倍时间，而公式通常只有 1~3 条）；
    * 只看**每只票最后一根 K 线**，且该票的最后一根必须是全市场最新行情日
      （口径与内置策略、与界面【试算】完全一致）；
    * **K 线口径按时间自动切**：开盘时间里的那一轮用实时快照拼出"今天"这一根
      （见 `formulas.prepare_inputs`），所以【试算】与【开始匹配】的口径永远一致；
    * 数据长度不足 `min_history` 的票直接跳过（滚动窗口全是缺值 ⇒ 不可能出信号）；
    * **弱市抬门槛**（`market_regime`，2026-10-08）：大盘弱势时只留"近 N 日跑赢
      全市场"的票，挡掉多少只写在 `warnings` 里（界面原样显示）。这条判断与
      【运行】试算**共用同一个 `market_regime.apply_gate()`**；强市/中性/未知时
      它一只票都不挡（行为与改动前完全一样）。

    联网：只有"公式用到快照字段"或"此刻正走盘中实时口径"时才取**一趟**快照
    （见 `formulas.prepare_inputs`）；其余情况一个请求都不发。
    """
    result = FormulaRun()
    names = lib.enabled_names(cfg, directory)
    if not names:
        # 没勾任何公式：一次库都不读（这是默认状态，必须与"没这个功能"完全一样）
        with _lock:
            _last_status.clear()
        return result

    specs = {spec.name: spec for spec in lib.formula_files(directory)}
    active: list[fm.Formula] = []
    for name in names:
        spec = specs.get(name)
        if spec is None or not spec.ok or spec.formula is None:
            continue
        active.append(spec.formula)
        result.ran.append(name)
    if not active:
        return result

    failures: dict[str, int] = {}
    first_error: dict[str, str] = {}
    hits: dict[str, list[dict]] = {formula.label: [] for formula in active}
    #: 「连续 N 个交易日都命中才算选中」（`signal_confirm_days`，默认 0 = 不确认）。
    #: 为什么要这一层：阈值型条件在边界上会今天命中、明天不命中，候选名单跟着抖
    #: （见 `fm.confirmed` 的说明）。**试算那条路用的是同一个判断**，
    #: 否则会出现"【运行】说选出 5 只、【开始匹配】只有 2 只"这种无法解释的差异。
    confirm_days = confirm_days_of(cfg)

    # 输入（K 线口径 + 扩展字段）全部由 `lib.prepare_inputs` 一份逻辑给：
    # "开盘时间里跑的匹配都是实时的"这条规矩在**试算与建池两条路上必须是同一份实现** ——
    # 各写一份迟早会出现"点【运行】选出 3 只、点【开始匹配】选出 1 只"，而这种差异
    # 用户没有任何办法解释。快照只在"公式真用到那些字段"或"此刻正走盘中口径"时才取一趟；
    # 一条都不需要时**一个请求都不发**（与"没这个功能"完全一样）。
    prepared = lib.prepare_inputs(cfg, db_path, active)
    # 库里最新的行情日**由 prepare_inputs 一起给出**（它自己也要用它判"要不要接今天那根"）：
    # 这里不再查一次 —— 两次 MAX(date) 之间理论上还能变（数据一边更新一边匹配），
    # 于是"接没接今天那根"与"跳过哪些票"就可能按两个不同的日子判。
    day = prepared.kline_day
    result.caliber = prepared.caliber
    if prepared.caliber:
        logger.info(f"策略匹配 {prepared.caliber}")
    for note in prepared.notes:
        # 影响"选不选得出来"的提示（典型：快照取不到 ⇒ 用到那些字段的条件一律不成立）
        # 走 `errors`：界面会把它显示在状态栏/结果页上，不然用户看到的是
        # "条件成立却一只都不出"，只能怀疑策略写错了。
        result.errors.append(note)
        logger.warning(note)
    for warning in prepared.warnings:
        # 口径类的**告知**进 `warnings`，不进 `errors` —— 建池的"成功/失败"判据是
        # "errors 是否为空"（`scheduler.Scheduler._report_succeeded`）：一句"退回了日 K"
        # 会让一次正常完成、票也选出来了的匹配被判成失败（还要在补跑点再跑一遍）。
        result.warnings.append(warning)
        logger.warning(warning)

    try:
        series_iter = fm.load_series(
            db_path, extra=prepared.extra, hot_industries=prepared.hot,
            today=prepared.today, live_bars=prepared.live_bars,
            kline_day=prepared.kline_day,
        )
        # ⚠️ `load_series` 是**生成器**：函数体要到第一次 `next()` 才执行，
        # 所以"库不存在"这类错误是在下面这个 for 里抛出来的 —— try 必须包住整个循环
        # （只在 `fm.load_series(...)` 那一行外面的 try 拦不住任何东西，实测踩过）。
        for series in series_iter:
            result.scanned += 1
            if day is not None and series.date[-1] not in (day, prepared.today):
                # 停牌/退市的票：它的"最后一根"是旧的，拿它当"今天选中"是错的
                continue
            for formula in active:
                if len(series.date) < formula.min_history:
                    # 数据不够长：滚动窗口一定全是缺值 ⇒ 不可能出信号，直接跳过（省时间）
                    continue
                label = formula.label
                try:
                    mask = formula.eval(series)
                except (fm.FormulaError, fm.FormulaDataError) as exc:
                    # 逐票兜住：一只票算不出来（缺字段/长度不对）不该让这条公式整条作废，
                    # 更不该让别的公式与内置策略跟着一起失败
                    count = failures.get(label, 0) + 1
                    failures[label] = count
                    first_error.setdefault(label, str(exc))
                    if count == 1:
                        logger.warning(
                            f"策略 {label} 在 {series.name}({series.symbol}) 上算不出来：{exc}"
                        )
                    continue
                if fm.confirmed(mask, confirm_days):
                    hits[label].append({
                        "symbol": series.symbol,
                        "name": series.name,
                        # 「连续 N 日确认」写在来源里：用户看到名单变短时，
                        # 一眼就知道是自己的确认设置（而不是"公式坏了/数据没了"）
                        "reason": (f"策略：{label}" if confirm_days <= 0
                                   else f"策略：{label}（连续 {confirm_days + 1} 日确认）"),
                    })
    except fm.FormulaDataError as exc:
        # 库不存在 / 读不出来这类**环境**问题：说清楚原因就返回，绝不让整轮建池失败
        message = f"策略匹配：{exc}"
        logger.warning(message)
        result.errors.append(message)
        for name in result.ran:
            result.status[name] = str(exc)
        _remember(result.status)
        return result

    # ── 弱市抬门槛（`market_regime`）──
    # 大盘弱势时只留"近 N 日跑赢全市场"的票。**与试算那条路调的是同一个函数**
    # （`formulas.preview_hits`），所以【运行】与【开始匹配】在任何大盘状态下
    # 都给出同一批票 —— 两处各写一套筛选就会出现"试算说 5 只、匹配只有 2 只"
    # 这种用户无法解释的差异（本文件里 K 线口径、连续确认也都是这么处理的）。
    #
    # 为什么**一次判完所有公式的票**（而不是逐条公式各判一次）：
    #   ① 筛掉多少只、为什么筛，是**这一轮**的一句话 —— 逐条公式各说一遍会让
    #      界面上重复同一条消息，而"挡掉 N 只"还会变成"某一条公式的 N 只"；
    #   ② 判据只看代码（同一只票的相对强弱与它来自哪条公式无关），
    #      所以按代码筛回各条公式的名单与逐条筛的结果**完全一致**。
    #
    # 为什么必须在写进 `result.picks`（那里还有一次 `MAX_PER_FORMULA * 3` 截断）
    # **之前**筛：先截断再筛，被挡掉的票会把后面本该留下的票挤出名单，
    # 于是一轮匹配选出几只与【运行】试算的只数对不上。
    flat = [pick for picks in hits.values() for pick in picks]
    gate = market_regime.apply_gate(flat, db_path=db_path, day=day, cfg=cfg)
    blocked = {str(pick.get("symbol") or "") for pick in gate.dropped}
    if gate.note:
        # ⚠️ 进 `warnings`（告知），**绝不进 `errors`**：建池的"成功/失败"判据是
        # "errors 是否为空"（`scheduler.Scheduler._report_succeeded`）——
        # 把一句"弱市挡掉了 7 只"塞进 errors，会让一次**正常完成**的匹配被判成失败
        # （票选出来了、推送也发了，状态却写"未成功"再补跑一遍）。
        result.warnings.append(gate.note)
        logger.warning(gate.note)

    for formula in active:
        label = formula.label
        picks = hits.get(label) or []
        if failures.get(label):
            # 有票算不出来：**记下来并保留**剩下的结果（少选几只比整条消失更接近本意），
            # 但状态里必须有原因 —— 否则用户只会看到"命中变少了"，猜不到为什么
            reason = (f"{failures[label]} 只票算不出来：{first_error.get(label, '')}")
            result.status[label] = reason
            result.errors.append(f"策略 {label}：{reason}")
            logger.warning(f"策略 {label}：{reason}")
        if blocked:
            picks = [pick for pick in picks
                     if str(pick.get("symbol") or "") not in blocked]
        if not picks:
            continue
        # 公式内按代码排序（`load_series` 本身就是代码升序，这里显式排一次，
        # 免得以后换了数据源顺序，池子里的"公式内排名"跟着乱）
        picks.sort(key=lambda pick: pick["symbol"])
        result.picks[formula_strategy_name(label)] = picks[: MAX_PER_FORMULA * 3]

    _remember(result.status)
    if result.picks:
        logger.info(
            "策略匹配：" + "、".join(
                f"{formula_name_of(key)} {len(value)} 只"
                for key, value in result.picks.items()
            )
        )
    return result


def confirm_days_of(cfg: Any) -> int:
    """读「连续确认天数」配置（负数/写错一律当 0 = 不确认）。

    单独一个函数是为了**两条路读同一份口径**（`run_enabled_formulas` 与
    `formulas.preview_hits`）：写错配置时"试算按 0 算、匹配按 2 算"是最难查的那种不一致。
    """
    try:
        value = int(getattr(cfg, "signal_confirm_days", 0) or 0)
    except (TypeError, ValueError):
        return 0
    # 上限 5：再长就不是"反手抖"而是另一套策略了，而且会把候选压到几乎没有
    return max(0, min(value, 5))


def _remember(status: dict[str, str]) -> None:
    with _lock:
        _last_status.clear()
        _last_status.update(status)

__all__ = [
    "FORMULA_PREFIX",
    "FORMULA_WEIGHT",
    "MAX_PER_FORMULA",
    "FormulaRun",
    "confirm_days_of",
    "formula_name_of",
    "formula_strategy_name",
    "is_formula_strategy",
    "last_status",
    "reset_status",
    "run_enabled_formulas",
]
