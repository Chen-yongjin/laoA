"""策略层：入选策略（低价股 / 连板回踩 / 短期反转 / 地量放量 / 首板缩量）与因子函数。"""

from laoa_trader.strategy import factors, rules
from laoa_trader.strategy.base import BaseStrategy

__all__ = ["BaseStrategy", "factors", "rules"]
