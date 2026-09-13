"""策略分组：按「持有期 + 证据强度」把 5 条入选策略分成 3 组，可自选跑哪几组。

为什么要分组
------------
5 条策略的持有期其实差一个量级，混在一起"每天推一堆"没有可执行性：

| 组 | 持有期 | 成员 | 依据（10 年样本） |
|---|---|---|---|
| `ultra` 超短·隔日 | T+2 | 连板回踩低吸 | T+2 α **+0.47%**(t=2.02)：唯一在**可执行的最短持有期**上 t≥2 的设计 |
| `short` 短线·T+3 | T+3 | 短期反转、地量后放量变盘、首板缩量整理 | T+2/T+3 t≈1.7~2.1 |
| `swing` 波段·T+10 | T+10 | 低价股 | T+3 +0.13%(t=3.50)、T+10 +0.32%(t=4.43)：四个持有期全显著为正 |

分组之后：想只做隔日就只开 `ultra`，想稳一点就只开 `swing` —— 池子、信号、
盘中提醒会**整条链路**都只围绕启用的组（见 `Selection`）。

**池子权重**放在这里（而不是 `pool.py`），是为了让"策略 → 权重 → 分组"
只有一个来源，避免两处维护导致漂移。权重沿用 10 年样本定的那套：
低价股 3、连板回踩 2、短期反转 2、地量放量 2、首板缩量 1。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from laoa_trader.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyGroup:
    """一个策略组。

    Attributes:
        key: 配置里用的短键（`ultra` / `short` / `swing`）。
        label: 界面展示名（中文）。
        horizon: 目标持有期（交易日）—— 与成员策略的 `target_horizon` 一致。
        members: ((策略类名, 池子权重), ...)，顺序即"该组内的默认优先级"。
        note: 一句话依据（界面上做提示用，取自 10 年样本结论）。
    """

    key: str
    label: str
    horizon: int
    members: tuple[tuple[str, int], ...]
    note: str = ""

    @property
    def strategies(self) -> tuple[str, ...]:
        """成员策略类名（保持定义顺序）。"""
        return tuple(name for name, _weight in self.members)

    def weight_of(self, class_name: str) -> int:
        """该策略在池子里的权重（不属本组时返回 0）。"""
        for name, weight in self.members:
            if name == class_name:
                return weight
        return 0


#: 三个组（顺序 = 界面与推送里的展示顺序：先超短、再短线、最后波段）
GROUPS: dict[str, StrategyGroup] = {
    "ultra": StrategyGroup(
        key="ultra",
        label="超短·隔日",
        horizon=2,
        members=(("LadderPullbackStrategy", 2),),
        note="连板回踩低吸：T+2 α +0.47%（t=2.02），唯一在可执行最短持有期上显著",
    ),
    "short": StrategyGroup(
        key="short",
        label="短线·T+3",
        horizon=3,
        members=(
            ("ReversalStrategy", 2),
            ("DryUpExpansionStrategy", 2),
            ("FirstLimitUpStrategy", 1),
        ),
        note="短期反转 / 地量后放量变盘 / 首板缩量整理：T+2~T+3 t≈1.7~2.1",
    ),
    "swing": StrategyGroup(
        key="swing",
        label="波段·T+10",
        horizon=10,
        members=(("LowPriceStrategy", 3),),
        note="低价股：T+3 +0.13%(t=3.50)、T+10 +0.32%(t=4.43)，四个持有期全显著",
    ),
}

#: 组的展示顺序
GROUP_ORDER: tuple[str, ...] = tuple(GROUPS)

#: 策略类名 → 组 key
_STRATEGY_TO_GROUP: dict[str, str] = {
    name: group.key for group in GROUPS.values() for name in group.strategies
}

#: 策略类名 → 池子权重（由 GROUPS 派生，供 pool.py 使用）
STRATEGY_WEIGHTS: dict[str, int] = {
    name: weight for group in GROUPS.values() for name, weight in group.members
}


def group_keys() -> list[str]:
    """全部组 key（按展示顺序）。"""
    return list(GROUP_ORDER)


def group_of(class_name: str) -> str | None:
    """策略类名 → 组 key（不在任何组里返回 None，例如将来新增但未归组）。"""
    return _STRATEGY_TO_GROUP.get(class_name)


def group_label(key: str) -> str:
    """组 key → 中文展示名（未知 key 原样返回，便于报错时看清是哪个）。"""
    group = GROUPS.get(key)
    return group.label if group else key


def group_horizon(key: str) -> int:
    group = GROUPS.get(key)
    return group.horizon if group else 0


def strategy_label_lookup() -> dict[str, str]:
    """{可输入的写法 → 策略类名}：类名、中文名都认（`--strategies 低价股` 与
    `--strategies LowPriceStrategy` 等价）。延迟导入 `rules` 以避免循环依赖。"""
    from laoa_trader.strategy import rules

    lookup: dict[str, str] = {}
    for class_name, cls in rules.STRATEGIES.items():
        lookup[class_name] = class_name
        lookup[class_name.lower()] = class_name
        label = cls.display_name or class_name
        lookup[label] = class_name
        # 再宽容一点：允许省略"低吸/整理"这类后缀后的中文名匹配
        lookup[label.replace("低吸", "").replace("整理", "")] = class_name
    return lookup


@dataclass
class Selection:
    """一次运行实际要跑哪些组 / 哪些策略。

    由 `resolve()` 从配置（或 CLI 临时覆盖）解析而来，然后**贯穿全链路**：
    跑策略、落信号、建池、盘中观察池都只看它。
    """

    groups: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    #: 是否来自"两组都没配"的默认全选
    default_all: bool = False
    #: 是否用户**明确**关闭了全部策略（例如 enabled_groups = ["none"]）——
    #: 这种"空"是故意的，调用方不该当成配置错误报警，只盯自选股即可
    explicit_off: bool = False

    @property
    def empty(self) -> bool:
        """没有任何可跑的策略（配置写错了、或名字全拼错）。"""
        return not self.strategies

    def includes(self, class_name: str) -> bool:
        """该策略是否在本次选择里（用于过滤候选/信号/观察池）。"""
        return class_name in self.strategies

    def label_of(self, class_name: str) -> str:
        """策略所属组的展示名（未知返回"—"）。"""
        key = group_of(class_name)
        return group_label(key) if key else "—"

    def describe(self) -> str:
        """一行中文摘要（CLI 打印与状态栏用）。"""
        if self.explicit_off:
            return "只盯自选股（策略已关闭）"
        if self.default_all:
            return f"全部策略组（{len(self.strategies)} 条策略）"
        labels = "、".join(group_label(k) for k in self.groups) or "（按策略名筛选）"
        return f"{labels}（{len(self.strategies)} 条策略）"

    def as_dict(self) -> dict:
        return {
            "groups": list(self.groups),
            "strategies": list(self.strategies),
            "warnings": list(self.warnings),
            "default_all": self.default_all,
            "explicit_off": self.explicit_off,
        }


def all_strategies() -> tuple[str, ...]:
    """全部策略类名（按组顺序展开）。"""
    return tuple(name for key in GROUP_ORDER for name in GROUPS[key].strategies)


def resolve(
    enabled_groups: Sequence[str] | None = None,
    enabled_strategies: Sequence[str] | None = None,
) -> Selection:
    """把「启用组」+「启用策略」解析成一份 Selection。

    规则（**两组都空 = 全选**，安全默认）：
        1. 都为空 → 全部组、全部策略；
        2. 只给组 → 这些组的全部成员；
        3. 只给策略 → 这些策略（组按策略自动推断）；
        4. 同时给 → 先取组的成员，再与策略列表**取交集**；
        5. 名字认不出来（拼错/已删除的策略）→ 记进 `warnings`，不静默忽略。

    Args:
        enabled_groups: 组 key（`ultra`/`short`/`swing`）。
        enabled_strategies: 策略名（类名或中文名都认）。

    Returns:
        Selection；若解析结果为空（例如名字全拼错），`selection.empty` 为真，
        调用方应当**跳过本次运行并提示原因**，而不是悄悄跑全量。
    """
    raw_groups = [str(g).strip() for g in (enabled_groups or []) if str(g).strip()]
    raw_strategies = [str(s).strip() for s in (enabled_strategies or []) if str(s).strip()]
    warnings: list[str] = []

    if not raw_groups and not raw_strategies:
        return Selection(groups=GROUP_ORDER, strategies=all_strategies(),
                         warnings=[], default_all=True)

    # 显式关闭全部策略：`enabled_groups = ["none"]`（或 off/无/关闭）→ 只盯自选股。
    # 为什么需要它：需求里"策略池为空也必须照常盯自选股"要有一种**明确的**表达方式，
    # 否则用户只能靠"把名字拼错"来关策略。
    OFF_ALIASES = {"none", "off", "no", "无", "关闭", "不要"}
    if raw_groups and all(g.lower() in OFF_ALIASES for g in raw_groups) \
            and not raw_strategies:
        return Selection(groups=(), strategies=(), warnings=[], explicit_off=True)
    raw_groups = [g for g in raw_groups if g.lower() not in OFF_ALIASES]

    # ── 组 ──
    chosen_groups: list[str] = []
    for key in raw_groups:
        normalized = key.lower()
        if normalized in GROUPS:
            if normalized not in chosen_groups:
                chosen_groups.append(normalized)
        else:
            warnings.append(f"未知策略组：{key}（可选：{'、'.join(GROUP_ORDER)}）")

    # 统一按**组定义顺序**（超短→短线→波段）排列，与用户输入顺序无关：
    # 这样界面/日志/推送里的顺序永远稳定，便于比对"今天和昨天是不是同一批"
    chosen_groups.sort(key=GROUP_ORDER.index)

    lookup = strategy_label_lookup()
    group_members: list[str] = []
    for key in chosen_groups:
        group_members.extend(GROUPS[key].strategies)

    # ── 策略 ──
    chosen_strategies: list[str] = []
    for raw in raw_strategies:
        class_name = lookup.get(raw) or lookup.get(raw.lower())
        if class_name is None:
            warnings.append(f"未知策略：{raw}（可用中文名或类名）")
            continue
        if class_name not in chosen_strategies:
            chosen_strategies.append(class_name)

    if chosen_groups and chosen_strategies:
        # 取交集：组决定范围、策略决定取舍（避免"组里的策略全跑"）
        final = [name for name in group_members if name in set(chosen_strategies)]
        dropped = [name for name in chosen_strategies if name not in set(group_members)]
        for name in dropped:
            warnings.append(
                f"策略 {name} 不在启用的组（{'、'.join(group_label(k) for k in chosen_groups)}）"
                "里，已忽略"
            )
    elif chosen_groups:
        final = list(group_members)
    else:
        final = list(chosen_strategies)

    # 组列表：显式给的组 ∪ 策略所在组（只给策略时也要能显示组别），同样按组顺序
    result_groups = list(chosen_groups)
    for name in final:
        key = group_of(name)
        if key and key not in result_groups:
            result_groups.append(key)
    result_groups.sort(key=GROUP_ORDER.index)

    if not final and not warnings:
        warnings.append("没有解析出任何策略")
    if not final:
        logger.warning("策略选择结果为空：" + "；".join(warnings))
    return Selection(groups=tuple(result_groups), strategies=tuple(final),
                     warnings=warnings, default_all=False)


def resolve_from_config(cfg) -> Selection:
    """从配置对象解析（config.toml 的 enabled_groups / enabled_strategies）。"""
    return resolve(
        getattr(cfg, "enabled_groups", None) or [],
        getattr(cfg, "enabled_strategies", None) or [],
    )


def weights_for(selection: Selection | None = None) -> dict[str, int]:
    """选择范围内 {策略类名: 权重}（Universe 为空时给全部）。"""
    if selection is None or selection.empty:
        return dict(STRATEGY_WEIGHTS)
    return {k: v for k, v in STRATEGY_WEIGHTS.items() if k in set(selection.strategies)}


def describe_groups() -> list[str]:
    """给 CLI/README 用的多行说明。"""
    lines = []
    for key in GROUP_ORDER:
        group = GROUPS[key]
        members = "、".join(
            f"{name.replace('Strategy', '')}(权重{w})" for name, w in group.members
        )
        lines.append(f"  {key:<6} {group.label:<10} T+{group.horizon:<3} {members}")
    return lines


def iter_selected(selection: Selection | None) -> Iterable[str]:
    """按组顺序遍历选择内的策略类名。"""
    if selection is None:
        return iter(all_strategies())
    return (name for name in all_strategies() if selection.includes(name))


__all__ = [
    "GROUPS",
    "GROUP_ORDER",
    "STRATEGY_WEIGHTS",
    "Selection",
    "StrategyGroup",
    "all_strategies",
    "describe_groups",
    "group_horizon",
    "group_keys",
    "group_label",
    "group_of",
    "iter_selected",
    "resolve",
    "resolve_from_config",
    "strategy_label_lookup",
    "weights_for",
]
