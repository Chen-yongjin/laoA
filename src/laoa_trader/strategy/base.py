"""策略基类：定义所有入选策略的抽象接口。

移植自服务器版 `sequoia_x/strategy/base.py`，只去掉「飞书 webhook 路由」相关属性
（桌面版的通知是并行三路，不需要按策略路由到不同机器人）。
`display_name` / `group` / `target_horizon` 保留，用于界面展示与将来做收益跟踪。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from laoa_trader.data.engine import DataEngine


class BaseStrategy(ABC):
    """选股策略抽象基类。

    Attributes:
        display_name: 策略中文名（界面与推送卡片展示）。
        group: 档位（short / watch）—— 与服务器版一致，仅作说明用途。
        target_horizon: 目标持有期（交易日）。
    """

    display_name: str = ""
    #: 档位：ultra = 超短(T+3)；short = 短期(T+10)；watch = 观察（不单独推送）
    group: str = "watch"
    #: 该策略的目标持有期（交易日）
    target_horizon: int = 5

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
