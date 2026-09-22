"""涨跌幅限制与涨跌停价：**全项目唯一的定义处**（板块判定 + 取整口径）。

为什么单独一个模块
------------------
同一套规则有三拨人要用：

1. 日更/回补（`data/public_sync.py`）—— 判"这一根是不是涨停/跌停"，用来攒涨停池；
2. 池子与提醒（`pool.py` 等）—— 判"这只票今天是不是跌停"；
3. **公式引擎**（`strategy/formula.py`）—— 通达信的 `ZTPRICE()` / `DTPRICE()`。

前两拨是"逐票标量"，第三拨是"numpy 数组（逐票逐日）"。如果各写一份，
迟早会出现"日更说涨停、公式说没涨停"这种最难查的分歧 —— 所以规则（板块号段、
涨跌幅、取整方式）全部收在这里，标量与数组两条路并排放、并由测试逐点比对。

实测依据（2026-09-17 全市场快照）
---------------------------------
* 主板 10%、创业板/科创板 20%、北交所 30%、未股改 S 股 5%；
* **北交所取整方向与别的板块相反**：涨停向下截断（49.12×1.3 = 63.856 → 63.85），
  跌停向上取整（49.12×0.7 = 34.384 → 34.39）—— 方向都朝"限制更紧的那一侧"；
* 其余板块四舍五入到分（这里是 `round(x, 2)` 的"四舍六入五成双"口径，
  与 `data/public_sync.py` 里历史实现逐字节一致，改了会让老库的涨停池对不上，
  所以**不要**换成 `floor(x+0.5)`）；
* `1e-9` 是给浮点误差留的余量（0.1+0.2 那种），不加会让 63.855 这类边界抖到另一侧。
"""

from __future__ import annotations

import math
from typing import Any

#: 各板块的涨跌幅限制（%）
LIMIT_MAIN = 10.0
LIMIT_GEM = 20.0        # 创业板/科创板
LIMIT_BJ = 30.0         # 北交所
LIMIT_S = 5.0           # 未股改 S 股（极少数）

#: 浮点余量：价格是两位小数，乘完再取整时会差 1e-15 那种量级
_EPS = 1e-9


def board_of(symbol: str) -> str:
    """裸 6 位代码 → 板块键（`bj` / `gem` / `main`）。"""
    code = str(symbol or "").strip()
    if code.startswith(("92", "8", "4")):
        return "bj"
    if code.startswith(("30", "68")):
        return "gem"
    return "main"


def is_s_stock(name: str) -> bool:
    """未股改 S 股（`S佳通` 这种）：名称以 `S` 开头但**不是** `ST`。"""
    text = str(name or "").strip().upper()
    return text.startswith("S") and not text.startswith("ST")


def limit_pct(symbol: str, name: str = "") -> float:
    """这只票的涨跌幅限制（%）。"""
    if is_s_stock(name):
        return LIMIT_S
    return {"bj": LIMIT_BJ, "gem": LIMIT_GEM, "main": LIMIT_MAIN}[board_of(symbol)]


def round_scalar(raw: float, board: str, *, up: bool) -> float:
    """把"理论价"取整到分（**标量**路径：日更、池子用）。"""
    if board == "bj":
        return (math.floor(raw * 100 + _EPS) if up else math.ceil(raw * 100 - _EPS)) / 100
    return round(raw + _EPS, 2)


def round_array(raw: Any, board: str, *, up: bool) -> Any:
    """把"理论价"取整到分（**数组**路径：公式引擎用）。

    与 `round_scalar()` 是同一套口径的两条实现（标量/数组），
    `tests/test_price_limits.py` 会在价格网格上逐点断言两者相等 —— 别只改一边。
    """
    import numpy as np      # 引擎侧本来就依赖 numpy；这里局部导入，标量调用方不必付代价

    values = np.asarray(raw, dtype="float64")
    if board == "bj":
        ticks = np.where(up, np.floor(values * 100 + _EPS), np.ceil(values * 100 - _EPS))
        return ticks / 100
    return np.round(values + _EPS, 2)


def limit_up_price(prev_close: float | None, symbol: str, name: str = "") -> float | None:
    """涨停价 = 前收 × (1+幅度)，按板块取整（北交所截断、其余四舍五入）。"""
    if prev_close is None or prev_close <= 0:
        return None
    raw = prev_close * (1 + limit_pct(symbol, name) / 100)
    return round_scalar(raw, board_of(symbol), up=True)


def limit_down_price(prev_close: float | None, symbol: str, name: str = "") -> float | None:
    """跌停价（北交所**向上**取整，方向与涨停相反）。"""
    if prev_close is None or prev_close <= 0:
        return None
    raw = prev_close * (1 - limit_pct(symbol, name) / 100)
    return round_scalar(raw, board_of(symbol), up=False)


def limit_price_ratio(prev_close: Any, ratio: Any, symbol: str, *, up: bool) -> Any:
    """`ZTPRICE/DTPRICE` 的共用实现：按**给定比例**（0.1 = 10%）算涨/跌停价。

    与 `limit_up_price()` 的区别只有一个：比例由调用方给（通达信公式里就是那个
    第二个参数，可以是常数也可以是表达式），不是按板块自动取。
    取整规则**仍然按板块**（北交所那条反向取整是真规则，跟比例从哪来无关）。

    返回 numpy 数组（公式引擎里 `prev_close` 本来就是 `REF(C,1)` 那种数组）；
    标量调用方请直接用 `limit_up_price()` / `limit_down_price()`。
    """
    import numpy as np

    prev = np.asarray(prev_close, dtype="float64")
    raw = prev * (1.0 + ratio) if up else prev * (1.0 - ratio)
    return round_array(raw, board_of(symbol), up=up)


__all__ = [
    "LIMIT_BJ",
    "LIMIT_GEM",
    "LIMIT_MAIN",
    "LIMIT_S",
    "board_of",
    "is_s_stock",
    "limit_down_price",
    "limit_pct",
    "limit_price_ratio",
    "limit_up_price",
    "round_array",
    "round_scalar",
]
