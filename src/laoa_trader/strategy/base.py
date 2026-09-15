"""策略基类：定义所有入选策略的抽象接口。

移植自服务器版 `sequoia_x/strategy/base.py`，只去掉「飞书 webhook 路由」相关属性
（桌面版的通知是并行三路，不需要按策略路由到不同机器人）。
`display_name` / `group` / `target_horizon` 保留，用于界面展示与将来做收益跟踪。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from laoa_trader.data.engine import DataEngine

#: 证据强度：两套执行口径（D+1 开盘买 / D+1 尾盘买）都为正 —— "有边际"，正常推送
EVIDENCE_PROVEN = "proven"
#: 证据强度：正 α 只存在于"开盘买"口径（改成尾盘买就转负）—— **照常推送**，
#: 只是标的上会带「（依赖开盘）」标记、策略勾选框旁写着两套口径的数字（把判断权交给用户）
EVIDENCE_OPEN_ONLY = "open_only"

#: 全部取值（配置/测试/界面按它校验，避免各处写字符串）
EVIDENCE_KINDS: tuple[str, ...] = (EVIDENCE_PROVEN, EVIDENCE_OPEN_ONLY)


class BaseStrategy(ABC):
    """选股策略抽象基类。

    Attributes:
        display_name: 策略中文名（界面与推送卡片展示）。
        group: 档位（short / watch）—— 与服务器版一致，仅作说明用途。
        target_horizon: 目标持有期（交易日）。
        evidence: 证据强度（决定**默认推不推**，见 `pool.split_push_rows`）：
            `PROVEN` = 两套执行口径（D+1 开盘买 / D+1 尾盘买）都为正 → 正常推送；
            `OPEN_ONLY` = 正 α **只存在于"开盘买"口径**，改成尾盘买就转负 →
            进池、进推送（默认全推），但会带「（依赖开盘）」标记 + 一行口径数字 ——
            要不要跟它们是用户的判断；`push_only_proven = true` 时才会被过滤掉。
        evidence_note: 上面那条结论的中文说明（界面 tooltip / 日志里原样展示）。
    """

    display_name: str = ""
    #: 档位：ultra = 超短(T+3)；short = 短期(T+10)；watch = 观察（不单独推送）
    group: str = "watch"
    #: 该策略的目标持有期（交易日）
    target_horizon: int = 5
    #: 证据强度（取值见 `EVIDENCE_PROVEN` / `EVIDENCE_OPEN_ONLY`）
    evidence: str = EVIDENCE_PROVEN
    #: 证据说明（一句话，中文）
    evidence_note: str = ""

    def __init__(self, engine: DataEngine, settings: Any = None) -> None:
        """
        Args:
            engine: DataEngine 实例（读本地库）。
            settings: 配置对象（桌面版是 `config.Config`；这些策略暂不使用）。
        """
        self.engine = engine
        self.settings = settings

    @property
    def label(self) -> str:
        """展示用名称：优先中文名，未设置时退回类名。"""
        return self.display_name or type(self).__name__

    @abstractmethod
    def run(self) -> list[str]:
        """执行选股逻辑，返回选中的股票代码列表（无结果时返回空列表）。"""
        ...
