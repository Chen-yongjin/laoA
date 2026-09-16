"""「公式」组：把用户在界面上勾选的公式跑成**池子认的候选**。

为什么单独一个模块
------------------
`strategy/formula.py` 是引擎（只认文本 → 数组），`formulas.py` 是公式库
（文件、配置、试算、成绩单），而"把勾选的公式跑成一批候选、失败的那些只记日志"
这件事同时要碰 **配置 + 库 + 池子**，塞进任何一边都会让那一边变脏。
所以放这里，并且只暴露一个函数：`run_enabled_formulas()`。

两条硬规矩
----------
1. **默认不参与**：`config.toml` 里 `enabled_formulas` 为空时，本方**一次库都不读**，
   内置策略的行为与以前完全一致（有测试钉住）。
2. **失败隔离**：某条公式在运行时抛 `FormulaError` / `FormulaDataError`
   （典型例子：用了历史不完整的 `连板()`、或那只票的序列长度对不上），
   只做三件事 —— 记日志、把它记进 `status`（界面上标红给原因）、
   把它从本批候选里去掉。**其余公式与内置策略照常出票，建池也不会失败。**

   为什么非要隔离：公式是**用户自己写的**，写错是常态而不是异常。一条写坏的公式
   让整轮选股（连同 5 条内置策略）一起失败，等于"用户越敢试错，程序越不能用" ——
   与 `formula.py` 里"逐文件报错、坏的不会拖垮好的"是同一条思路，只是这次
   发生在**运行期**而不是解析期。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laoa_trader import formulas as lib
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import groups

logger = get_logger(__name__)

#: 公式候选在池子里的权重（`pool.POOL_STRATEGIES` 查不到时用的兜底值就是 1，
#: 这里显式给 2）。为什么是 2：用户**自己勾的**公式要是权重 1，很容易被内置策略
#: 按分数挤到 10 只之外（表现为"我勾了公式，池子里却没有"）；给到与"短期反转"
#: 同档的 2 既看得见，又不会盖过内置策略。
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

    @property
    def empty(self) -> bool:
        return not self.picks


def run_enabled_formulas(
    db_path: str | Path,
    cfg: Any = None,
    *,
    directory: str | Path | None = None,
) -> FormulaRun:
    """跑 `enabled_formulas` 里的公式，返回池子候选（**不写库、不联网**）。

    实现要点：
    * **一遍扫描**：`load_series()` 逐只产出，几条公式共用同一个 `Series`
      （每条公式单独读一遍库要多花几倍时间，而公式通常只有 1~3 条）；
    * 只看**每只票最后一根 K 线**，且该票的最后一根必须是全市场最新行情日
      （口径与内置策略、与界面【试算】完全一致）；
    * 数据长度不足 `min_history` 的票直接跳过（滚动窗口全是缺值 ⇒ 不可能出信号）。
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

    day = lib.latest_trading_day(db_path)
    failures: dict[str, int] = {}
    first_error: dict[str, str] = {}
    hits: dict[str, list[dict]] = {formula.label: [] for formula in active}

    try:
        series_iter = fm.load_series(db_path)
        # ⚠️ `load_series` 是**生成器**：函数体要到第一次 `next()` 才执行，
        # 所以"库不存在"这类错误是在下面这个 for 里抛出来的 —— try 必须包住整个循环
        # （只在 `fm.load_series(...)` 那一行外面的 try 拦不住任何东西，实测踩过）。
        for series in series_iter:
            result.scanned += 1
            if day is not None and series.date[-1] != day:
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
                            f"公式 {label} 在 {series.name}({series.symbol}) 上算不出来：{exc}"
                        )
                    continue
                if bool(mask[-1]):
                    hits[label].append({
                        "symbol": series.symbol,
                        "name": series.name,
                        "reason": f"公式：{label}",
                    })
    except fm.FormulaDataError as exc:
        # 库不存在 / 读不出来这类**环境**问题：说清楚原因就返回，绝不让整轮建池失败
        message = f"公式选股：{exc}"
        logger.warning(message)
        result.errors.append(message)
        for name in result.ran:
            result.status[name] = str(exc)
        _remember(result.status)
        return result

    for formula in active:
        label = formula.label
        picks = hits.get(label) or []
        if failures.get(label):
            # 有票算不出来：**记下来并保留**剩下的结果（少选几只比整条消失更接近本意），
            # 但状态里必须有原因 —— 否则用户只会看到"命中变少了"，猜不到为什么
            reason = (f"{failures[label]} 只票算不出来：{first_error.get(label, '')}")
            result.status[label] = reason
            result.errors.append(f"公式 {label}：{reason}")
            logger.warning(f"公式 {label}：{reason}")
        if not picks:
            continue
        # 公式内按代码排序（`load_series` 本身就是代码升序，这里显式排一次，
        # 免得以后换了数据源顺序，池子里的"公式内排名"跟着乱）
        picks.sort(key=lambda pick: pick["symbol"])
        result.picks[groups.formula_strategy_name(label)] = picks[: MAX_PER_FORMULA * 3]

    _remember(result.status)
    if result.picks:
        logger.info(
            "公式选股：" + "、".join(
                f"{groups.formula_name_of(key)} {len(value)} 只"
                for key, value in result.picks.items()
            )
        )
    return result


def _remember(status: dict[str, str]) -> None:
    with _lock:
        _last_status.clear()
        _last_status.update(status)


__all__ = [
    "FORMULA_WEIGHT",
    "MAX_PER_FORMULA",
    "FormulaRun",
    "last_status",
    "reset_status",
    "run_enabled_formulas",
]
