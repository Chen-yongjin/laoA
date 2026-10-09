"""涨跌停价的两条路（标量 / 数组）与通达信 `ZTPRICE` / `DTPRICE`。

为什么单独一个文件
------------------
`laoa_trader/price_limits.py` 是全项目**唯一**的"涨跌幅限制 + 取整口径"定义处：
日更（判涨停攒涨停池）走标量函数，公式引擎（逐票逐日的 numpy 数组）走数组函数。
两条实现并排放在一个文件里、由这里的用例**逐点比对**，是为了让"日更说涨停、
公式说没涨停"这种最难查的分歧不可能出现 —— 那种 bug 在界面上完全看不出来，
只会在筛选结果里悄悄少几只票。

通达信侧的口径（`ZTPRICE` / `DTPRICE`）单独钉住三件事：
第二个参数是**比例小数**（0.1 = 10%）而不是百分数；省略时按 10%；
北交所那条"涨停向下截断、跌停向上取整"的反向规则在数组路径里同样成立。
"""

from __future__ import annotations

import numpy as np
import pytest

from laoa_trader import price_limits as pl
from laoa_trader.data import public_sync as ps
from laoa_trader.strategy import formula as fm
from tests.test_formula import make_series


# ══════════════════════════════════════════════════════════════════════════
# 1) 标量与数组两条路必须给出同样的结果
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("board", ["main", "gem", "bj"])
def test_scalar_and_array_rounding_agree_on_a_price_grid(board: str) -> None:
    """价格网格上逐点比对两条路（含 0.005 / 0.015 这类半分钱边界）。"""
    prices = np.concatenate([
        np.arange(0.5, 120.0, 0.013),          # 普通区间
        np.arange(9.990, 10.060, 0.005),       # 半分钱边界
        np.array([0.05, 0.10, 49.12, 63.855, 1e4 + 0.005]),
    ])
    for up in (True, False):
        array_out = pl.round_array(prices, board, up=up)
        scalar_out = np.array([pl.round_scalar(float(x), board, up=up) for x in prices])
        assert np.allclose(array_out, scalar_out, atol=0, rtol=0), (
            f"{board} up={up} 两条路不一致："
            f"{prices[~np.isclose(array_out, scalar_out)][:5]}"
        )


def test_array_path_matches_the_established_scalar_rules() -> None:
    """数组路径的结果 == `public_sync.limit_up_price/limit_down_price`（同一套规则）。

    注意要比的是**乘完幅度之后**的结果：`round_array()` 只负责"取整到分"这一步，
    幅度（10%/20%/30%/5%）由调用方乘进去 —— 这里用 `limit_pct()` 拿到那个幅度，
    与生产代码取的是同一个来源。
    """
    for symbol, name in (("600519", "贵州茅台"), ("300750", "宁德时代"),
                         ("920000", "北交所样本"), ("600182", "S佳通")):
        board = pl.board_of(symbol)
        pct = ps.limit_pct(symbol, name) / 100
        for prev in (0.99, 2.0, 10.05, 49.12, 1234.56):
            up = pl.round_array(np.array([prev * (1 + pct)]), board, up=True)[0]
            down = pl.round_array(np.array([prev * (1 - pct)]), board, up=False)[0]
            assert up == ps.limit_up_price(prev, symbol, name)
            assert down == ps.limit_down_price(prev, symbol, name)


def test_north_exchange_rounding_goes_the_opposite_way() -> None:
    """北交所：涨停向下截断、跌停向上取整（实测口径，别改成统一四舍五入）。"""
    prev = 49.12
    assert ps.limit_up_price(prev, "920000") == 63.85     # 63.856 截断
    assert ps.limit_down_price(prev, "920000") == 34.39   # 34.384 进位
    # 主板同样的数字是四舍五入
    assert ps.limit_up_price(prev, "600519") == round(prev * 1.1 + 1e-9, 2)


# ══════════════════════════════════════════════════════════════════════════
# 2) 引擎里的 ZTPRICE / DTPRICE
# ══════════════════════════════════════════════════════════════════════════


def _limit_prices(symbol: str, name: str, prev: float, *,
                  ratio: float | None = None) -> tuple[float, float]:
    """用引擎内部实现算一对涨/跌停价（**走的就是公式求值那条路**）。"""
    series = make_series([prev * 0.99, prev], symbol=symbol, name=name)
    ev = fm._Evaluator(series)
    vals = [np.array([prev], dtype="float64")]
    if ratio is not None:
        vals.append(np.array([ratio], dtype="float64"))
    up = fm._limit_price(ev, vals, up=True)[0]
    down = fm._limit_price(ev, vals, up=False)[0]
    return float(up), float(down)


def test_ztprice_matches_hand_computed_limit_price() -> None:
    """`ZTPRICE(昨收, 0.1)` 就等于手算的 10% 涨停价（含四舍五入边界）。"""
    for prev in (3.01, 10.05, 11.055, 49.12, 596.1, 1234.56):
        up, _ = _limit_prices("600519", "贵州茅台", prev, ratio=0.1)
        assert up == round(prev * 1.1 + 1e-9, 2), prev


def test_dtprice_matches_hand_computed_limit_price() -> None:
    for prev in (3.01, 10.05, 11.055, 49.12, 596.1, 1234.56):
        _, down = _limit_prices("600519", "贵州茅台", prev, ratio=0.1)
        assert down == round(prev * 0.9 + 1e-9, 2), prev


def test_ztprice_ratio_is_a_fraction_not_a_percentage() -> None:
    """第二个参数是 `0.2`（=20%），不是 `20` —— 与通达信一致。"""
    prev = 49.12
    up20, _ = _limit_prices("300750", "宁德时代", prev, ratio=0.2)
    assert up20 == ps.limit_up_price(prev, "300750")     # 创业板的 20%
    up10, _ = _limit_prices("300750", "宁德时代", prev, ratio=0.1)
    assert up10 == round(prev * 1.1 + 1e-9, 2)           # 给了 0.1 就是 10%，与板块无关


def test_ztprice_defaults_to_ten_percent_when_the_ratio_is_omitted() -> None:
    """省略第二个参数 = 按 10%（通达信老公式常这么写）。"""
    prev = 10.05
    up, down = _limit_prices("600519", "贵州茅台", prev)
    assert up == round(prev * 1.1 + 1e-9, 2)
    assert down == round(prev * 0.9 + 1e-9, 2)


def test_ztprice_keeps_the_north_exchange_rounding() -> None:
    """北交所：比例由公式给，**取整方向仍按板块**（0.3 → 向下截断 / 向上取整）。"""
    prev = 49.12
    up, down = _limit_prices("920000", "北交所样本", prev, ratio=0.3)
    assert up == 63.85 and down == 34.39


def test_ztprice_accepts_an_expression_ratio() -> None:
    """比例允许是表达式（`涨跌幅/100` 这种写法在真公式里很常见）。"""
    series = make_series([10.0, 10.5], symbol="600519", name="贵州茅台")
    ev = fm._Evaluator(series)
    prev = np.array([10.0, 10.0])
    ratio = np.array([0.05, 0.2])
    up = fm._limit_price(ev, [prev, ratio], up=True)
    assert up[0] == round(10.0 * 1.05 + 1e-9, 2)
    assert up[1] == round(10.0 * 1.2 + 1e-9, 2)


def test_ztprice_is_registered_and_listed() -> None:
    """两个函数要进白名单（否则粘进来还是"未知函数"）。"""
    assert "ZTPRICE" in fm.SUPPORTED_FUNCTIONS
    assert "DTPRICE" in fm.SUPPORTED_FUNCTIONS
    spec = fm.FUNCTIONS["ZTPRICE"]
    assert spec.min_args == 1 and spec.max_args == 2      # 第二个参数可省


# ══════════════════════════════════════════════════════════════════════════
# 3) 真·打板类公式：能编译、能跑、信号正确
# ══════════════════════════════════════════════════════════════════════════


def test_a_real_tdx_limit_up_formula_compiles_and_runs() -> None:
    """网上常见的一类打板公式（用 ZTPRICE 判"是否封在涨停价上"）能跑出信号。

    公式含义：今天的收盘价正好等于按昨收算出的涨停价 → 今天涨停。
    """
    text = (
        "ZTJ:=ZTPRICE(REF(C,1),0.1);\n"
        "DTJ:=DTPRICE(REF(C,1),0.1);\n"
        "涨停:=ABS(C-ZTJ)<0.005;\n"
        "跌停:=ABS(C-DTJ)<0.005;\n"
        "涨停"
    )
    formula = fm.compile_formula(text, name="打板测试")

    # 昨收 10.00 → 涨停价 11.00；今天收在 11.00 → 命中
    hit = make_series([10.0, 11.0], symbol="600519", name="贵州茅台")
    assert bool(formula.eval(hit)[-1]) is True

    # 今天只涨到 10.50 → 不命中
    miss = make_series([10.0, 10.5], symbol="600519", name="贵州茅台")
    assert bool(formula.eval(miss)[-1]) is False


def test_a_north_exchange_limit_up_formula_uses_the_truncated_price() -> None:
    """北交所的涨停价是**截断**出来的，收盘价等于它才算涨停（四舍五入的写法会漏判）。"""
    text = "ZTJ:=ZTPRICE(REF(C,1),0.3);\nABS(C-ZTJ)<0.005"
    formula = fm.compile_formula(text, name="北交所涨停")

    # 昨收 49.12 → 30% 涨停价 63.85（截断）；收在 63.85 应当命中
    hit = make_series([49.12, 63.85], symbol="920000", name="北交所样本")
    assert bool(formula.eval(hit)[-1]) is True
    # 四舍五入会得到 63.86：若引擎错用四舍五入，这条会命中（用它守住口径）
    over = make_series([49.12, 63.86], symbol="920000", name="北交所样本")
    assert bool(formula.eval(over)[-1]) is False
