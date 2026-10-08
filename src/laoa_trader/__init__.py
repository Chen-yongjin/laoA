"""财神助手（Windows 单机版）。

自包含的 A 股匹配 + 盯盘 + 提醒桌面程序：本地 SQLite（原始价 + 复权因子 + 后复权视图）、
同花顺 REST 数据源、5 条入选策略、每日精选池、盘中规则提醒、三路并行通知。

用法：
    python -m laoa_trader          # 图形界面
    python -m laoa_trader --cli --once
"""

from laoa_trader.config import Config, get_config, load_config

__version__ = "1.5.0"
__all__ = ["Config", "get_config", "load_config", "__version__"]
