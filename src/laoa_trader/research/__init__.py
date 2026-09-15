"""研究层：策略成绩单（回测）—— 回答"某条策略到底行不行"。

与界面无关（Qt 无关），只依赖 pandas + 本地库；
命令行入口：`python -m laoa_trader --cli --scorecard`。

**不在这里 import scorecard**：它是可选的重活（会读几百万行行情），
让调用方显式 `from laoa_trader.research import scorecard` 才引入，
避免"打开界面"就顺带把它拉进来。
"""

__all__: list[str] = []
