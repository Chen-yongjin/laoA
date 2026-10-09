"""老数据的显示口径 + `signal` 表写入 —— 与「策略引擎」无关的两件事。

为什么会有这个模块
------------------
2026-09-18 用户拍板：5 条「内置策略」（原先写在 `strategy/rules.py` 里、由 Python 算信号的
策略）整体改成**随包公式**，匹配只跑用户勾选的公式。于是那套策略引擎（`rules.py` /
`base.py` / `factors.py` / 策略组机制）连同 `research/scorecard.py` 里给它们做回测的那部分
**全部删掉了**。

但**老库里的数据不会跟着消失**：

* `stock_pool.strategy` 里存过 `LowPriceStrategy` 这类**类名**；
* `signal` 表也按类名写过行（盘中风控的观察池要读它）。

界面与推送读这些老数据时必须还能显示中文名 —— 否则用户会在「自选标的」的「来源」列里
看到 `LadderPullbackStrategy` 这种天书。所以这份"类名 → 中文名"的映射必须留着：
它服务的对象是**历史数据**，不是匹配逻辑（新选出来的票来源一律是 `公式·xxx` / `自选`）。

同理，`save_signals()` 只是往 `signal` 表写一批行（现在写的是**公式**的候选），
跟策略类没有半点关系，所以一起搬到这里，不再挂在已经删掉的 `rules.py` 上。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from laoa_trader import wording
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 老库里出现过的策略类名 → 中文名（**只用于显示历史数据**）。
#:
#: 五条都在，包括 2026-09-18 被删掉的「连板回踩低吸」与「低价股」——
#: 那两条已经不再参与筛选，但**老池子行里还留着它们的类名**，界面上照样要显示中文。
LEGACY_STRATEGY_LABELS: dict[str, str] = {
    "LowPriceStrategy": "低价股",
    "LadderPullbackStrategy": "连板回踩低吸",
    "ReversalStrategy": "短期反转",
    "DryUpExpansionStrategy": "地量后放量变盘",
    "FirstLimitUpStrategy": "首板缩量整理",
}


def strategy_label(class_name: str) -> str:
    """类名 / 合成名 → **界面上显示的那个词**（老数据的显示口径）。

    认不出的名字**原样返回**（只去掉 `Strategy` 后缀，与服务器版一致）——
    今天传进来的绝大多数是 `公式·xxx` 这种合成名或用户自己的名字，原样返回正是要的。

    2026-09-22 起多做一件事：显示前统一过一遍 `wording.display_strategy()` ——
    那一天主人要求"把公式都改成策略"（当时是**换成** `策略·` 前缀），2026-09-23 又要求
    「把公式名称中的策略两个字去掉，无意义」→ 从此这一层是**剥掉**前缀：
    自定义的 `公式·xxx` 与内置策略的 `短期反转` 在「来源」列里只写名字本身。
    **只动显示**，库里的值 `公式·xxx` 一个字没动。
    """
    label = LEGACY_STRATEGY_LABELS.get(class_name) or str(class_name or "").replace(
        "Strategy", ""
    )
    return wording.display_strategy(label)


def save_signals(
    engine: Any,
    picks: dict[str, list[dict]],
    day: str | None = None,
) -> int:
    """把这一轮选出来的票写入 `signal` 表（盘中风控观察池与将来做前向跟踪用）。

    Args:
        engine: `DataEngine`（要它的 `connect()` 与最新行情日）。
        picks: `{策略名: [{"symbol","name","reason"}...]}` —— 现在传进来的是
            公式的合成名（`公式·尾盘匹配策略`）与它们的候选。
        day: 信号日；默认按库里最新行情日。
    """
    from laoa_trader.data import storage

    day = day or engine.get_latest_data_date() or datetime.now().strftime("%Y-%m-%d")
    with engine.connect() as conn:
        rows = conn.execute(
            "SELECT symbol, close FROM stock_daily_hfq WHERE date = ?", (day,)
        ).fetchall()
        closes = {r[0]: r[1] for r in rows}
        payload = [
            (day, strategy, pick["symbol"], pick.get("name"), closes.get(pick["symbol"]),
             None, pick.get("reason"))
            for strategy, items in picks.items()
            for pick in items
        ]
        return storage.write_signals(conn, payload)


__all__ = [
    "LEGACY_STRATEGY_LABELS",
    "save_signals",
    "strategy_label",
]
