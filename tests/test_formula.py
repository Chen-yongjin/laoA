"""自定义策略公式引擎（`laoa_trader.strategy.formula`）的离线测试。

覆盖五块：

1. **正确性** —— 每个指标都用构造序列**手算核对**，而不是"跑通就算过"；
2. **错误路径** —— 未知字段/函数给近似建议、注入尝试、超长超深，全部中文 + 结构化；
3. **边界** —— 空序列 / 单日 / 全 NaN / 窗口越界，约定是"缺值永不产生信号"；
4. **公式文件加载** —— 注释头、中文名、BOM、单文件报错不影响整体；
5. **性能** —— 5000 根 K 线上一条复杂公式 < 50ms。

为什么非要"手算核对"：公式引擎的 bug 表现为"选出来的股票不对"，而用户那边
**拿不到任何报错**（公式照样跑出结果）。这类 bug 只能靠把指标数值算清楚来挡住。

全程不联网、不碰用户数据目录（`load_series` 用 tmp_path 里的合成库）。
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from laoa_trader.strategy import formula as fm

#: 仓库根（laoA/）—— 用来定位随包分发的示例公式目录
ROOT = Path(__file__).resolve().parents[1]
FORMULAS_DIR = ROOT / "formulas"

NAN = float("nan")


# ══════════════════════════════════════════════════════════════════════════
# 测试设施
# ══════════════════════════════════════════════════════════════════════════


def _dates(n: int) -> list[str]:
    """n 个连续日期的升序列表（用真实日历而不是 D0/D1，方便测 DATE 字段）。"""
    base = date(2026, 1, 1)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


def make_series(
    close,
    *,
    open_=None,
    high=None,
    low=None,
    vol=None,
    amount=None,
    industry: str = "半导体",
    symbol: str = "600001",
    name: str = "样本",
    limit_up_days=None,
    limit_up_cnt=None,
    dates=None,
) -> fm.Series:
    """造一只股票的序列：只给 close 也能用，其余按"同价、等量"补齐。"""
    c = np.asarray(close, dtype="float64")
    n = c.size

    def pick(value, default):
        arr = np.asarray(default() if value is None else value, dtype="float64")
        if arr.size != n:
            raise AssertionError(f"测试数据长度不一致：{arr.size} != {n}")
        return arr

    return fm.Series(
        symbol=symbol,
        name=name,
        industry=industry,
        date=_dates(n) if dates is None else list(dates),
        close=c,
        open=pick(open_, lambda: c),
        high=pick(high, lambda: c),
        low=pick(low, lambda: c),
        vol=pick(vol, lambda: np.full(n, 1e6)),
        amount=pick(amount, lambda: np.full(n, 1e8)),
        pre_close=pick(
            None,
            lambda: np.concatenate(([NAN], c[:-1])) if n else np.zeros(0),
        ),
        limit_up_days=pick(limit_up_days, lambda: np.zeros(n)),
        limit_up_cnt=pick(limit_up_cnt, lambda: np.zeros(n)),
    )


def sig(text: str, series: fm.Series) -> list[bool]:
    """编译并求值，返回 Python 布尔列表（方便逐位断言）。"""
    out = fm.compile_formula(text).eval(series)
    assert out.dtype == bool, "eval 必须返回布尔数组"
    assert out.shape == (len(series.date),)
    return out.tolist()


def raw(expr: str, series: fm.Series):
    """**白盒**：只求值一个表达式（不要求它是选股条件），用来核对指标数值。

    为什么需要它：公开 API 的最后一行必须是条件，所以 `MA(C,5)` 这类中间序列
    没法直接看出数值；而"MA 算得对不对"恰恰是最该被钉死的东西。
    """
    node = fm._Parser(expr).expr()
    return fm._Evaluator(series).eval_node(node)


def raw_num(expr: str, series: fm.Series) -> np.ndarray:
    return np.asarray(raw(expr, series), dtype="float64")


def compile_error(text: str) -> fm.FormulaError:
    """编译必须失败，且必须是 FormulaError（不是 ValueError/TypeError/崩栈）。"""
    with pytest.raises(fm.FormulaError) as excinfo:
        fm.compile_formula(text)
    return excinfo.value


# ══════════════════════════════════════════════════════════════════════════
# 一、正确性：手算核对
# ══════════════════════════════════════════════════════════════════════════


def test_ma5_values_and_close_above_ma5() -> None:
    """`C>MA(C,5)`：前 4 根窗口不足 → 不产生信号；第 5 根起 MA=3,4,5..."""
    s = make_series(range(1, 11))
    np.testing.assert_array_equal(
        raw_num("MA(C,5)", s), [NAN, NAN, NAN, NAN, 3, 4, 5, 6, 7, 8]
    )
    assert sig("C>MA(C,5)", s) == [False] * 4 + [True] * 6


def test_volume_above_volume_ma_times_1_5() -> None:
    """`V>MA(V,5)*1.5`：只有放量那根（量 200 vs 均量 120 → 200>180）成立。"""
    vols = [100, 100, 100, 100, 100, 200, 100, 100, 100, 100]
    s = make_series(range(1, 11), vol=vols)
    np.testing.assert_array_equal(
        raw_num("MA(V,5)", s), [NAN] * 4 + [100, 120, 120, 120, 120, 120]
    )
    assert sig("V>MA(V,5)*1.5", s) == [False] * 5 + [True] + [False] * 4


def test_user_example_formula_end_to_end() -> None:
    """用户原话里的那条公式：`C>MA(C,5) AND V>MA(V,5)*1.5`。

    两条各自成立的日子不同（前 6 根价格在涨但没放量、最后一天放量），
    所以整条公式只在**最后一天**成立 —— 这正是"多条件求交集"的语义。
    """
    vols = [100] * 5 + [100, 100, 100, 100, 300]
    s = make_series(range(1, 11), vol=vols)
    assert sig("C>MA(C,5) AND V>MA(V,5)*1.5", s) == [False] * 9 + [True]


def test_cross_is_real_golden_cross() -> None:
    """`CROSS` 是"**这一根**上穿"，不是"一直在上方"。

    序列 [1,2,3,2,1,2,3] 与 MA3 只在上穿一次（i=5）；
    i=6 虽然 C>MA3，但前一根已经在上方 → 不算金叉。
    这条用例专门盯着"把 CROSS 写成简单大于"的改动。
    """
    s = make_series([1, 2, 3, 2, 1, 2, 3])
    out = sig("CROSS(C,MA(C,3))", s)
    assert out == [False, False, False, False, False, True, False]
    assert sum(out) == 1, "整段序列只该有一次金叉"
    # 对照：简单大于会连续成立，二者必须不同
    assert sig("C>MA(C,3)", s) != out


def test_cross_first_bar_has_no_previous() -> None:
    """第一根没有前值 → 不产生金叉（而不是拿 NaN 比出 True 来）。"""
    s = make_series([5, 6, 7])
    assert sig("CROSS(C,5)", s)[0] is False


def test_hhv_llv_values_and_ratio() -> None:
    """`HHV(H,20)/LLV(L,20)<1.15`：窗口不足的 19 根都不算，之后横盘成立。"""
    s = make_series([10.0] * 25)
    assert sig("HHV(H,20)/LLV(L,20)<1.15", s) == [False] * 19 + [True] * 6
    narrow = make_series([10, 11, 12, 13, 14], high=[10, 11, 12, 13, 14], low=[8, 9, 10, 11, 12])
    np.testing.assert_array_equal(raw_num("HHV(H,3)", narrow), [NAN, NAN, 12, 13, 14])
    np.testing.assert_array_equal(raw_num("LLV(L,3)", narrow), [NAN, NAN, 8, 9, 10])


def test_count_limit_up_days_in_window() -> None:
    """`COUNT(涨停天数()>0,10)>=2`：近 10 日内涨停 ≥2 次。

    构造：[1,0,0,0,0,0,0,1,0,1] —— 到第 10 根（下标 9）刚好数到 3 次；
    前 9 根窗口不满 10 → 不产生信号。
    """
    flags = [1, 0, 0, 0, 0, 0, 0, 1, 0, 1]
    s = make_series(range(1, 11), limit_up_days=flags)
    expected = [False] * 9 + [True]
    assert sig("COUNT(涨停天数()>0,10)>=2", s) == expected
    # 等价写法：涨停天数(10) 就是 COUNT(涨停天数()>0,10)
    assert sig("涨停天数(10)>=2", s) == expected
    np.testing.assert_array_equal(
        raw_num("涨停天数()", s), np.asarray(flags, dtype="float64")
    )


def test_lianban_ge_2() -> None:
    """`连板()>=2`：直接用库里 limit_up_pool 的连板天数。"""
    counts = [0, 0, 1, 0, 0, 2, 0, 0, 3, 0]
    s = make_series(range(1, 11), limit_up_cnt=counts)
    assert sig("连板()>=2", s) == [
        False, False, False, False, False, True, False, False, True, False,
    ]


def test_if_max_min_abs_values() -> None:
    """IF / MAX / MIN / ABS 的逐位数值。"""
    s = make_series([10, 11, 12], open_=[12, 10, 11])
    np.testing.assert_array_equal(raw_num("IF(C>O,C,O)", s), [12, 11, 12])
    np.testing.assert_array_equal(raw_num("MAX(C,O)", s), [12, 11, 12])
    np.testing.assert_array_equal(raw_num("MIN(C,O)", s), [10, 10, 11])
    np.testing.assert_array_equal(raw_num("ABS(C-O)", s), [2, 1, 1])


def test_std_is_sample_std() -> None:
    """STD 用样本标准差（ddof=1）：[1,2,3] → 1.0。"""
    s = make_series([1.0, 2.0, 3.0, 10.0])
    got = raw_num("STD(C,3)", s)
    np.testing.assert_array_equal(got[:2], [NAN, NAN])
    assert got[2] == pytest.approx(1.0)
    assert got[3] == pytest.approx(float(np.std([2.0, 3.0, 10.0], ddof=1)))
    # 窗口 1 算不出标准差 → 全 NaN（而不是 0）
    assert np.isnan(raw_num("STD(C,1)", s)).all()


def test_sum_and_ref_values() -> None:
    s = make_series([1, 2, 3, 4, 5], vol=[10, 20, 30, 40, 50])
    np.testing.assert_array_equal(raw_num("SUM(V,3)", s), [NAN, NAN, 60, 90, 120])
    np.testing.assert_array_equal(raw_num("REF(C,1)", s), [NAN, 1, 2, 3, 4])
    np.testing.assert_array_equal(raw_num("REF(C,0)", s), [1, 2, 3, 4, 5])
    np.testing.assert_array_equal(raw_num("REF(C,4)", s), [NAN, NAN, NAN, NAN, 1])
    np.testing.assert_array_equal(raw_num("REF(C,99)", s), [NAN] * 5)


def test_ema_recursion() -> None:
    """EMA 是递推：alpha=2/(N+1)，首值取 X[0]（通达信口径）。"""
    s = make_series([1.0, 2.0, 3.0])
    got = raw_num("EMA(C,2)", s)
    alpha = 2 / 3
    assert got[0] == pytest.approx(1.0)
    assert got[1] == pytest.approx(alpha * 2 + (1 - alpha) * 1)
    assert got[2] == pytest.approx(alpha * 3 + (1 - alpha) * got[1])


def test_ema_skips_missing_bars() -> None:
    """缺值那一根沿用上一根的值（不更新），不会把 NaN 传染给后面。"""
    s = make_series([1.0, NAN, 3.0])
    got = raw_num("EMA(C,2)", s)
    assert got[1] == pytest.approx(1.0)
    assert got[2] == pytest.approx(2 / 3 * 3 + 1 / 3 * 1)


def test_barslast_values() -> None:
    """BARSLAST：当根为真 = 0，之后递增；从未为真 = NaN。"""
    s = make_series([10, 12, 11, 13, 12])
    # C>11 在 [10,12,11,13,12] 上是 [F,T,F,T,T] → 距上一次为真分别是 [nan,0,1,0,0]
    np.testing.assert_array_equal(
        raw_num("BARSLAST(C>11)", s), [NAN, 0, 1, 0, 0]
    )
    assert sig("BARSLAST(C>100)=0", s) == [False] * 5


def test_vol_ratio_uses_previous_5_days() -> None:
    """量比 = 当日成交额 ÷ 前 5 日均额（**不含当日**）。

    前 5 日各 1e8，第 6 日起各 3e8：
    第 6 根 = 3e8 / 1e8 = 3.0；第 7 根 = 3e8 / mean(1e8*4, 3e8) = 2.142857…；
    第 8 根 = 3e8 / mean(1e8*3, 3e8*2) = 1.666…；其后的分母被当日巨量自己抬起来 → <1.5。
    """
    amounts = [1e8] * 5 + [3e8] * 5
    s = make_series(range(1, 11), amount=amounts)
    got = raw_num("量比()", s)
    assert np.isnan(got[:5]).all(), "不足 5 个前值 → 不产生量比"
    assert got[5] == pytest.approx(3.0)
    assert got[6] == pytest.approx(3e8 / np.mean([1e8, 1e8, 1e8, 1e8, 3e8]))
    assert got[7] == pytest.approx(3e8 / np.mean([1e8, 1e8, 1e8, 3e8, 3e8]))
    assert got[8] < 1.5 and got[9] < 1.5
    assert sig("量比()>1.5", s) == [False] * 5 + [True, True, True, False, False]


def test_vol_ratio_window_argument() -> None:
    """量比(N) 可以自定义回看窗口（默认 5）。"""
    amounts = [1e8, 1e8, 5e8, 1e8]
    s = make_series(range(1, 5), amount=amounts)
    got = raw_num("量比(3)", s)
    assert np.isnan(got[:3]).all()
    assert got[3] == pytest.approx(1e8 / np.mean([1e8, 1e8, 5e8]))


def test_industry_equals_and_in() -> None:
    """INDUSTRY 支持 `=` / `!=` / `IN`（字符串只能这么比）。"""
    s = make_series([1, 2, 3], industry="半导体")
    assert sig('INDUSTRY="半导体"', s) == [True] * 3
    assert sig('INDUSTRY!="半导体"', s) == [False] * 3
    assert sig('INDUSTRY IN ("半导体","软件服务")', s) == [True] * 3
    assert sig('INDUSTRY IN ("白酒","软件服务")', s) == [False] * 3
    other = make_series([1, 2, 3], industry="白酒")
    assert sig('INDUSTRY="半导体"', other) == [False] * 3
    # 行业为空字符串的股票：只匹配空串，不会被 "IN" 误选
    blank = make_series([1, 2, 3], industry="")
    assert sig('INDUSTRY IN ("半导体")', blank) == [False] * 3
    assert sig('INDUSTRY=""', blank) == [True] * 3


def test_date_field_equality() -> None:
    """DATE 只参与 = / !=（供调试与"只看某一天"）。"""
    s = make_series([1, 2, 3])
    assert sig('DATE="2026-01-02"', s) == [False, True, False]
    assert sig('DATE!="2026-01-02"', s) == [True, False, True]


def test_multiline_variables_and_metadata() -> None:
    """多行 `:=` 中间变量：值正确，且 fields/functions 只统计真正用到的。"""
    text = "M5:=MA(C,5)\nM10:=MA(C,10)\nC>M5 AND M5>M10"
    s = make_series(range(1, 13))
    # MA(C,10) 要到第 10 根才有值 → 整条 AND 前 9 根都不成立
    assert sig(text, s) == [False] * 9 + [True] * 3

    f = fm.compile_formula(text)
    assert len(f.statements) == 3
    assert f.fields == ("C",)
    assert f.functions == ("MA",)
    assert f.outputs == ()
    assert f.min_history == 10


def test_colon_statement_marks_output() -> None:
    """`X : 表达式` 是"输出中间序列"的写法（本轮只记录，不渲染）。"""
    f = fm.compile_formula("M5 : MA(C,5)\nC>M5")
    assert f.outputs == ("M5",)
    assert sig("M5 : MA(C,5)\nC>M5", make_series(range(1, 11))) == [False] * 4 + [True] * 6


def test_variable_reassignment_allowed() -> None:
    """通达信允许重复赋值，这里也允许（最后写入的生效）。"""
    text = "T:=MA(C,3)\nT:=MA(C,5)\nC>T"
    s = make_series(range(1, 11))
    assert sig(text, s) == sig("C>MA(C,5)", s)


def test_logic_operators_word_and_symbol_equivalent() -> None:
    s = make_series([10, 12, 11, 13], open_=[9, 13, 12, 12])
    assert sig("C>O AND C>10", s) == sig("C>O && C>10", s)
    assert sig("C>O OR C>10", s) == sig("C>O || C>10", s)
    assert sig("NOT C>O", s) == sig("!(C>O)", s)
    assert sig("NOT C>O", s) == sig("! (C>O)", s)
    assert sig("not C>O", s) == sig("NOT C>O", s)
    # 语义核对：没有缺值时 NOT 就是取反
    positive = sig("C>O", s)
    assert sig("NOT C>O", s) == [not x for x in positive]


def test_case_insensitive_and_alias_equivalent() -> None:
    s = make_series(range(1, 11), vol=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    base = sig("C>MA(C,5) AND V>MA(V,5)", s)
    assert sig("c>ma(c,5) and v>ma(v,5)", s) == base
    assert sig("CLOSE>MA(CLOSE,5) AND VOL>MA(VOL,5)", s) == base
    assert sig("CLOSE>MA(CLOSE,5) AND VOLUME>MA(VOLUME,5)", s) == base
    # 其余别名
    assert sig("AMO>0", s) == sig("AMOUNT>0", s)
    assert sig("PRE>0", s) == [False] + [True] * 9      # 前收盘第 1 根没有
    assert sig("PRE_CLOSE>0", s) == sig("PRE>0", s)
    assert sig("O>0", s) == sig("OPEN>0", s)
    assert sig("H>0", s) == sig("HIGH>0", s)
    assert sig("L>0", s) == sig("LOW>0", s)


def test_open_and_close_use_their_own_columns() -> None:
    """`OPEN` 这类**长名**不能被安全黑名单误伤（曾经真的把 open() 关键字写进去过）。"""
    s = make_series([10, 11, 12], open_=[12, 9, 13])
    assert sig("OPEN>CLOSE", s) == [True, False, True]
    # 昨收：第 1 根没有（NaN → False），第 3 根 13>11 成立
    assert sig("OPEN>PRE_CLOSE", s) == [False, False, True]


def test_unary_minus_and_precedence() -> None:
    s = make_series([1, 2, 3, 4])
    np.testing.assert_array_equal(raw_num("-C", s), [-1, -2, -3, -4])
    np.testing.assert_array_equal(raw_num("-(C+1)", s), [-2, -3, -4, -5])
    # 乘法先于加法
    np.testing.assert_array_equal(raw_num("1+2*3", s), [7, 7, 7, 7])
    np.testing.assert_array_equal(raw_num("(1+2)*3", s), [9, 9, 9, 9])
    # 比较先于 AND
    assert sig("C>1 AND C<3 OR C>3.5", s) == [False, True, False, True]


def test_fullwidth_punctuation_is_accepted() -> None:
    """中文输入法的全角标点不该变成报错（这是最高频的"看起来一样却不通过"）。"""
    s = make_series([10, 11], open_=[9, 12])
    assert sig("C＞O AND （V＞0）", s) == [True, False]
    assert sig("INDUSTRY＝“半导体”", s) == [True, True]
    err = compile_error("C＞O，V＞0")
    assert "多余的逗号" in str(err)        # 全角逗号被归一成 `,`，报错要说得清楚


def test_comments_are_ignored() -> None:
    text = "{ 这是花括号注释 }\nM5:=MA(C,5)  # 行尾注释\n// 整行注释\nC>M5"
    s = make_series(range(1, 11))
    assert sig(text, s) == [False] * 4 + [True] * 6


def test_fields_and_functions_reported() -> None:
    f = fm.compile_formula(
        "M5:=MA(C,5)\nV5:=MA(V,5)\nC>M5 AND V>V5*1.5 AND 连板()>=1"
    )
    assert set(f.fields) == {"C", "VOL"}
    assert set(f.functions) == {"MA", "连板"}
    assert f.min_history == 5
    assert "字段：C、VOL" in f.describe()
    assert "连板" in f.describe()


def test_min_history_is_a_lower_bound() -> None:
    assert fm.compile_formula("C>MA(C,5)").min_history == 5
    assert fm.compile_formula("REF(C,3)>0").min_history == 4       # 还要多一根
    assert fm.compile_formula("量比()>1").min_history == 6         # 前 5 日 + 当日
    assert fm.compile_formula("C>MA(C,5) AND HHV(H,20)>0").min_history == 20
    assert fm.compile_formula("C>O").min_history == 1


def test_label_falls_back_to_last_line() -> None:
    f = fm.compile_formula("M5:=MA(C,5)\nC>M5")
    assert f.label == "C>M5"
    named = fm.compile_formula("C>MA(C,5)", name="我的公式")
    assert named.label == "我的公式"


# ══════════════════════════════════════════════════════════════════════════
# 二、错误路径：中文、可定位、结构化
# ══════════════════════════════════════════════════════════════════════════


def test_unknown_function_suggests_close_name() -> None:
    err = compile_error("C>MAA(C,5)")
    assert '未知函数 "MAA"' in str(err)
    assert '"MA"' in err.hint, "要给「是不是想写 MA」这类建议"
    assert "可用函数" in err.hint
    assert err.code == "unknown_function"
    assert (err.line, err.col) == (1, 3)


def test_unknown_field_suggests_close_name() -> None:
    err = compile_error("CLOES>1")
    assert '字段 "CLOES" 不存在' in str(err)
    assert "CLOSE" in err.hint
    assert err.code == "unknown_field"
    assert str(err).startswith("第 1 行第 1 列：")


def test_unknown_field_lists_defined_variables() -> None:
    err = compile_error("M5:=MA(C,5)\nM10:=M5*2\nC>M50")
    assert "已定义的变量" in err.hint
    assert "M5" in err.hint and "M10" in err.hint
    assert err.line == 3


def test_function_without_parentheses() -> None:
    err = compile_error("C>MA")
    assert '函数 "MA" 后面要跟括号' in str(err)
    assert "MA(C,5)" in err.hint


def test_unbalanced_parentheses() -> None:
    left = compile_error("C>MA(C,5")
    assert "括号没有闭合" in str(left)
    right = compile_error("C>MA(C,5))")
    assert "多余的" in str(right) or "一行只能写一条语句" in str(right)


def test_last_line_must_be_condition_numeric() -> None:
    err = compile_error("C")
    assert "最后一行必须是选股条件" in str(err)
    assert "数值序列" in str(err)
    assert err.code == "not_condition"
    err2 = compile_error("MA(C,5)")
    assert "最后一行必须是选股条件" in str(err2)


def test_last_line_must_be_condition_not_assignment() -> None:
    err = compile_error("M5:=MA(C,5)")
    assert "最后一行必须是选股条件" in str(err)
    assert err.code == "not_condition"


def test_middle_statement_must_be_assignment() -> None:
    """中间的裸表达式（想要"输出中间序列"却忘了写变量名）要明确指出怎么改。"""
    err = compile_error("C>0\nC>1")
    assert "中间语句必须是赋值" in str(err)
    assert ":=" in err.hint
    err2 = compile_error("MA(C,5)\nC>0")
    assert "中间语句必须是赋值" in str(err2)


def test_too_long_formula() -> None:
    err = compile_error("C>0 AND " + "C>0 AND " * 300 + "C>0")
    assert "公式太长" in str(err)
    assert err.code == "too_long"


def test_too_many_lines() -> None:
    text = "\n".join(f"A{i}:=1" for i in range(61)) + "\nC>0"
    err = compile_error(text)
    assert "行数太多" in str(err)
    assert err.code == "too_long"


def test_too_many_ast_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 节点上限：正常公式碰不到（2000 字符最多也就 2000 个节点），
    所以这里临时把上限压到 5 —— 用来证明这道闸门真的会拦，而不是摆设。"""
    monkeypatch.setattr(fm, "MAX_AST_NODES", 5)
    err = compile_error("C>O AND V>0 AND H>L")
    assert "太复杂" in str(err)
    assert err.code == "too_complex"


def test_too_deep_nesting() -> None:
    text = "C>" + "(" * 80 + "0" + ")" * 80
    err = compile_error(text)
    assert "嵌套太深" in str(err)
    assert err.code == "too_complex"


def test_python_injection_attempts_are_rejected() -> None:
    """注入尝试一律在**解析期**被拒（本引擎根本没有 eval，这里是第二道锁）。"""
    cases = {
        "import os": "import",
        "from os import path": "from",
        "def f(): return 1": "def",
        "class A: pass": "class",
        "lambda: 1": "lambda",
        "__class__": "__class__",
        "__import__": "__import__",
        "C.__class__": ".",
        "().x": "属性访问",
        "__class__.__bases__": "属性访问",   # `.` 在词法期就被拦下，`__class__` 根本到不了解析期
        "_private>0": "_private",
        "eval(C)": "eval",
        "exec(C)": "exec",
        "compile(C)": "compile",
    }
    for text, needle in cases.items():
        err = compile_error(text)
        assert needle in str(err), f"{text!r} 的报错里应点名 {needle}，实际：{err}"
        assert err.code in ("forbidden", "syntax"), err.code


def test_unsupported_syntax_gives_chinese_errors() -> None:
    cases = {
        "C[0]": "下标",
        '"a"+"b"': "字符串",
        "C>0;": "分号",
        "C&O": "不认识的字符",
        "C^2": "不认识的字符",
        "C%2": "不认识的字符",
        "1<C<5": "连续比较",
        "C.x": "属性访问",
        "C>0.5.5": "一行只能写一条语句",
    }
    for text, needle in cases.items():
        err = compile_error(text)
        assert needle in str(err), f"{text!r} 的报错里应有 {needle}，实际：{err}"


def test_function_arity_errors() -> None:
    for text, needle in (
        ("MA(C)", "需要 2 个参数"),
        ("MA(C,5,6)", "需要 2 个参数"),
        ("MA()", "需要 2 个参数"),
        ("CROSS(C)", "需要 2 个参数"),
        ("IF(C>0,1)", "需要 3 个参数"),
        ("连板(5)", "需要 0 个参数"),
    ):
        err = compile_error(text + "\n")
        assert needle in str(err), f"{text!r} → {err}"


def test_window_argument_errors() -> None:
    for text, needle in (
        ("MA(C,0)", "1~5000"),
        ("MA(C,-1)", "1~5000"),
        ("MA(C,1.5)", "必须是整数"),
        ("MA(C,99999)", "1~5000"),
        ("REF(C,-1)", "0~5000"),
        ("HHV(H,0)", "1~5000"),
    ):
        err = compile_error(text + "\n")
        assert needle in str(err), f"{text!r} → {err}"


def test_window_argument_must_be_constant_at_runtime() -> None:
    """窗口写成序列（`MA(C,C)`）静态看着是数值，只能在求值期拦下。"""
    s = make_series([1, 2, 3, 4, 5])
    with pytest.raises(fm.FormulaError) as excinfo:
        fm.compile_formula("MA(C,C)>0").eval(s)
    assert "窗口参数" in str(excinfo.value)
    assert excinfo.value.code == "window"


def test_variable_name_cannot_shadow_field_or_function() -> None:
    err = compile_error("C:=1\nC>0")
    assert "与内置字段同名" in str(err)
    err2 = compile_error("MA:=1\nMA>0")
    assert "与内置函数同名" in str(err2)


def test_logic_requires_real_conditions() -> None:
    """条件位必须是比较式：`V AND C>O` 这种写法几乎总是用户写错了。"""
    for text, needle in (
        ("V AND C>O", "AND"),
        ("NOT C", "NOT"),
        ("COUNT(V,10)>0", "COUNT"),
        ("IF(C,1,0)>0", "IF"),
        ("BARSLAST(C)>0", "BARSLAST"),
    ):
        err = compile_error(text + "\n")
        assert "需要条件" in str(err), f"{text!r} → {err}"
        assert needle in str(err)


def test_string_operators_are_limited() -> None:
    for text in ('INDUSTRY>"a"', 'INDUSTRY=1', '1=INDUSTRY'):
        err = compile_error(text + "\n")
        assert err.code == "type" or "类型" in str(err) or "需要数值" in str(err)


def test_in_list_errors() -> None:
    assert "不能为空" in str(compile_error("INDUSTRY IN ()\n"))
    assert "只能是字符串" in str(compile_error("INDUSTRY IN (1)\n"))
    assert "括号列表" in str(compile_error('INDUSTRY IN "半导体"\n'))
    assert "只能用于字符串字段" in str(compile_error("C IN (1)\n"))


def test_empty_formula() -> None:
    for text in ("", "   ", "\n\n", "{ 只有注释 }"):
        err = compile_error(text)
        assert "公式是空的" in str(err)
        assert err.code == "empty"


def test_true_false_none_have_specific_hints() -> None:
    assert "True/False" in str(compile_error("C>0 AND TRUE"))
    assert "None" in str(compile_error("C>None"))


def test_error_is_structured_for_the_ui() -> None:
    err = compile_error("M5:=MA(C,5)\nC>M5 AND CLOES>1")
    payload = err.to_dict()
    assert payload["line"] == 2
    assert isinstance(payload["col"], int) and payload["col"] > 0
    assert payload["code"] == "unknown_field"
    assert payload["message"].startswith("字段")
    assert "第 2 行" in payload["text"]


def test_every_bad_input_raises_formula_error() -> None:
    """**硬要求**：任何垃圾输入都只能是 FormulaError，不能是别的异常（更不能崩栈）。"""
    bad_inputs = [
        "", "   ", "\n\n", "{",
        "MAA(C,5)>0", "CLOES>1", "C>MA(C,5", "C>0))", "(C>0",
        "C", "MA(C,5)", "V", "C+1", "M5:=MA(C,5)",
        "C>0\nD:=1", "import os", "from os import path", "def f()", "class A",
        "lambda x: x", "__class__", "__import__", "_x>0", "C.x", "C[0]", "C[0:1]",
        '"a"+"b"', "INDUSTRY+'a'", "C>0;", "C&O", "C|O", "C^2", "C%2", "C@O", "C$O",
        "C~O", "?", "1<C<5", "MA(C)", "MA(C,5,6)", "MA()", "CROSS(C)", "IF(C>0,1)",
        "MA(C,0)", "MA(C,-1)", "MA(C,1.5)", "MA(C,100000)", "REF(C,-1)",
        "C:=1\nC>0", "MA:=1\nMA>0", "MA", "NOT C", "C AND O", "COUNT(V,10)>0",
        "TRUE", "FALSE", "NONE", "INDUSTRY IN ()", "INDUSTRY IN (1,2)",
        "INDUSTRY IN \"a\"", "INDUSTRY>\"a\"", "C>" + "(" * 80 + "0" + ")" * 80,
        ";", "}", "{ 未闭合", '"未闭合', "C>0 AND " + "C>0 AND " * 300 + "C>0",
        "\n".join(f"A{i}:=1" for i in range(61)) + "\nC>0",
        "1" * 2100,
    ]
    crashes: list[str] = []
    for text in bad_inputs:
        try:
            fm.compile_formula(text)
            crashes.append(f"{text[:30]!r}: 竟然编译通过了（应该报错）")
        except fm.FormulaError:
            continue
        except Exception as exc:  # noqa: BLE001 - 就是要把"别的异常"抓出来
            crashes.append(f"{text[:30]!r}: {type(exc).__name__}: {exc}")
    assert crashes == [], "存在非 FormulaError 的失败：" + "\n".join(crashes)


def test_non_string_input() -> None:
    with pytest.raises(fm.FormulaError):
        fm.compile_formula(None)          # type: ignore[arg-type]
    with pytest.raises(fm.FormulaError):
        fm.compile_formula(b"C>0")        # type: ignore[arg-type]


def test_engine_source_has_no_eval_or_exec() -> None:
    """静态自检：引擎源码里**不存在** eval/exec/compile/__import__ 调用。

    为什么用 AST 而不是字符串搜索：字符串搜索会被 `def eval(self, ...)`、
    `formula.eval(...)` 这类正常代码干扰，只有语法树能准确回答"有没有调用内建 eval"。
    """
    tree = ast.parse(Path(fm.__file__).read_text(encoding="utf-8"))
    forbidden = {"eval", "exec", "compile", "__import__", "input", "globals",
                 "locals", "breakpoint"}
    # 逃逸出口：这些属性一旦出现，说明有人在往 Python 内部对象上爬
    escapes = {"__globals__", "__builtins__", "__class__", "__bases__",
               "__subclasses__", "__code__", "__mro__"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden:
                found.append(f"第 {node.lineno} 行调用了 {node.func.id}()")
        if isinstance(node, ast.Attribute) and node.attr in escapes:
            found.append(f"第 {node.lineno} 行访问了 .{node.attr}")
    assert found == [], "公式引擎里不允许出现动态执行：" + "；".join(found)


# ══════════════════════════════════════════════════════════════════════════
# 三、边界：空 / 单日 / 缺值 / 窗口越界
# ══════════════════════════════════════════════════════════════════════════


def test_empty_series_returns_empty_signal() -> None:
    s = make_series([])
    out = fm.compile_formula("C>MA(C,5)").eval(s)
    assert out.shape == (0,)
    assert out.dtype == bool


def test_single_bar_series() -> None:
    s = make_series([10.0])
    assert sig("C>MA(C,5)", s) == [False]
    assert sig("C>0", s) == [True]
    assert sig("REF(C,1)>0", s) == [False]
    assert sig("量比()>1", s) == [False]


def test_all_nan_series_never_raises() -> None:
    """全 NaN（长期停牌）必须安静地"什么都不选"，绝不抛异常。"""
    s = make_series([NAN] * 5, open_=[NAN] * 5, vol=[NAN] * 5)
    assert sig("C>MA(C,5)", s) == [False] * 5
    assert sig("C!=5", s) == [False] * 5
    assert sig("C=5", s) == [False] * 5
    assert sig("NOT C>MA(C,5)", s) == [False] * 5
    assert sig("C>MA(C,5) OR C>O", s) == [False] * 5
    assert sig("CROSS(C,MA(C,3))", s) == [False] * 5
    assert sig("COUNT(C>O,3)>=1", s) == [False] * 5
    assert sig("ABS(C-PRE_CLOSE)>0", s) == [False] * 5
    assert sig("量比()>1", s) == [False] * 5


def test_every_function_survives_missing_values() -> None:
    """把每个函数都拿缺值喂一遍：不许抛异常、不许返回 True。"""
    s = make_series(
        [NAN] * 3, open_=[NAN] * 3, high=[NAN] * 3, low=[NAN] * 3,
        vol=[NAN] * 3, amount=[NAN] * 3,
    )
    #: 表达式 → 每一格"是不是 NaN"的期望
    expectations = {
        "MA(C,2)": [True] * 3,
        "EMA(C,2)": [True] * 3,
        "REF(C,1)": [True] * 3,
        "HHV(H,2)": [True] * 3,
        "LLV(L,2)": [True] * 3,
        "SUM(V,2)": [True] * 3,
        "STD(C,2)": [True] * 3,
        "ABS(C)": [True] * 3,
        "MAX(C,O)": [True] * 3,
        "MIN(C,O)": [True] * 3,
        "BARSLAST(C>0)": [True] * 3,
        "量比()": [True] * 3,
        # 涨停池两列是 0/1（不是行情价，"缺值"在这里不存在）→ 0 而不是 NaN
        "涨停天数()": [False] * 3,
        "连板()": [False] * 3,
        # 滚动求和：窗口不足那一格是 NaN，其余是真实的 0
        "涨停天数(2)": [True, False, False],
    }
    for expr, want_nan in expectations.items():
        value = raw_num(expr, s)
        assert value.shape == (3,), expr
        assert np.isnan(value).tolist() == want_nan, f"{expr} → {value}"
        if not want_nan[0]:
            assert (value[~np.isnan(value)] == 0).all(), expr
    # 这些条件在全缺值序列上一律不成立（不抛异常、不产生信号）
    for text in ("MA(C,2)>0", "量比()>1", "涨停天数(2)>=1", "ABS(C)>0",
                 "BARSLAST(C>0)=0", "HHV(H,2)>LLV(L,2)"):
        assert sig(text, s) == [False] * 3, text


def test_nan_comparison_is_always_false() -> None:
    """缺值既不"大于"也不"不等于"任何东西（`NaN != 5` 的 IEEE 陷阱）。"""
    s = make_series([NAN, 5.0])
    assert sig("C>0", s) == [False, True]
    assert sig("C<0", s) == [False, False]
    assert sig("C>=5", s) == [False, True]
    assert sig("C=5", s) == [False, True]
    assert sig("C!=5", s) == [False, False], "缺值不能算「不等于 5」"
    assert sig("C!=0", s) == [False, True]


def test_not_does_not_resurrect_missing_data() -> None:
    """`NOT` 不能把"数据不足"翻成信号（回测里最贵的一类错）。"""
    s = make_series(range(1, 11))
    assert sig("NOT C>MA(C,5)", s) == [False] * 10
    # 与"取反"的直觉对比：只有数据齐了以后才可能为真
    s2 = make_series([10, 9, 8, 7, 6, 5, 4])
    assert sig("NOT C>MA(C,5)", s2) == [False] * 4 + [True, True, True]


def test_division_by_zero_is_nan_not_inf() -> None:
    """`C/V` 在成交量为 0 时是 NaN（不是 inf，否则 `C/V>10` 会意外为真）。"""
    s = make_series([1.0, 2.0], vol=[0.0, 1.0])
    got = raw_num("C/V", s)
    assert np.isnan(got[0])
    assert got[1] == pytest.approx(2.0)
    assert sig("C/V>10", s) == [False, False]


def test_ref_before_series_start_is_nan() -> None:
    s = make_series([1.0, 2.0, 3.0])
    np.testing.assert_array_equal(raw_num("REF(C,100)", s), [NAN] * 3)
    assert sig("REF(C,100)>0", s) == [False] * 3


def test_window_larger_than_series_is_nan() -> None:
    s = make_series([1.0, 2.0, 3.0])
    for expr in ("MA(C,10)", "HHV(H,10)", "LLV(L,10)", "SUM(V,10)", "STD(C,10)"):
        assert np.isnan(raw_num(expr, s)).all(), expr
    assert sig("COUNT(C>0,10)>=1", s) == [False] * 3
    assert sig("MA(C,10)>0", s) == [False] * 3


def test_nan_inside_window_poisons_result() -> None:
    """窗口里只要有一根是缺值，这一格就是缺值（不拿"更短的窗口"糊弄）。"""
    s = make_series([1.0, 2.0, NAN, 4.0, 5.0])
    got = raw_num("MA(C,3)", s)
    assert np.isnan(got[:2]).all()
    assert np.isnan(got[2]) and np.isnan(got[3]), "窗口含缺值 → NaN"
    assert got[4] == pytest.approx(float(np.mean([NAN, 4.0, 5.0]))) or np.isnan(got[4])
    # 第 5 根窗口是 [NaN,4,5] → 仍然缺值；第 6 根才干净
    s2 = make_series([1.0, 2.0, NAN, 4.0, 5.0, 6.0])
    got2 = raw_num("MA(C,3)", s2)
    assert np.isnan(got2[4])
    assert got2[5] == pytest.approx(float(np.mean([4.0, 5.0, 6.0])))


def test_nan_propagates_through_logic_and_count() -> None:
    """缺值沿 AND/OR/COUNT 传播：宁可漏选，不可错选。"""
    s = make_series(range(1, 11))
    # 前 4 根 MA(C,5) 是缺值 → 整条 AND 在这几根上不能为真
    assert sig("C>MA(C,5) AND C>0", s) == [False] * 4 + [True] * 6
    assert sig("C>MA(C,5) OR C>0", s) == [False] * 4 + [True] * 6
    # COUNT 的窗口里含缺值 → 整格缺值（不能"能数几根数几根"）：
    # 第 10 根的窗口里仍含 4 个缺值的 MA → 结论是"不知道"，不是"满足"
    assert sig("COUNT(C>MA(C,5),10)>=1", s) == [False] * 10
    # 条件本身没有缺值时，COUNT 就是老老实实数数
    assert sig("COUNT(C>0,10)>=10", s) == [False] * 9 + [True]
    assert sig("COUNT(C>0,3)=3", s) == [False, False, True, True, True] + [True] * 5


def test_eval_result_shape_and_dtype_for_pandas_like_input() -> None:
    """调用方传 numpy / list / pandas.Series 都能吃（Series 构造时统一转 arrays）。"""
    n = 6
    s = fm.Series(
        symbol="600001", name="样本", industry="半导体", date=_dates(n),
        close=list(range(1, n + 1)), open=list(range(1, n + 1)),
        high=[10.0] * n, low=[0.1] * n, vol=[1.0] * n, amount=[1.0] * n,
        pre_close=[NAN] + list(range(1, n)), limit_up_days=[0] * n,
        limit_up_cnt=[0] * n,
    )
    assert isinstance(s.close, np.ndarray)
    assert sig("C>MA(C,5)", s) == [False] * 4 + [True, True]


def test_series_length_mismatch_is_data_error() -> None:
    s = make_series([1.0, 2.0, 3.0])
    s.close = np.array([1.0])          # 模拟调用方拼错了数据
    with pytest.raises(fm.FormulaDataError) as excinfo:
        fm.compile_formula("C>0").eval(s)
    assert "长度必须一致" in str(excinfo.value)


def test_series_rejects_2d_arrays() -> None:
    with pytest.raises(fm.FormulaDataError):
        fm.Series(
            symbol="600001", name="", industry="", date=["2026-01-01"],
            close=np.zeros((1, 2)), open=[1.0], high=[1.0], low=[1.0],
            vol=[1.0], amount=[1.0], pre_close=[1.0],
            limit_up_days=[0.0], limit_up_cnt=[0.0],
        )


def test_formula_is_reusable_across_series_and_threadsafe() -> None:
    """同一个编译好的公式可以反复求值（每次求值都是独立上下文）。"""
    f = fm.compile_formula("C>MA(C,5)")
    a = make_series(range(1, 11))
    b = make_series(range(10, 0, -1))
    first = f.eval(a).tolist()
    assert first == [False] * 4 + [True] * 6
    # 下降序列上 C 永远低于 MA5 → 全 False。两个结果不同，正说明求值之间没有串味
    assert f.eval(b).tolist() == [False] * 10
    assert f.eval(a).tolist() == first


def test_extra_fields_extension_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """扩展点仍然成立：登记名字 + 在 `Series.extra` 里塞数组即可用。

    （2026-09-18 起这个扩展点真派上用场了：`流通市值`/`换手率`/`ST`/`科创`/`北交所`
    就是登记在这里的，见 `test_snapshot_fields_*` 与 `test_flag_fields_*`。）
    """
    monkeypatch.setitem(fm.EXTRA_FIELDS, "JJL", "num")
    s = make_series([1.0, 2.0, 3.0])
    s.extra["JJL"] = np.array([0.0, 5.0, 0.0])
    f = fm.compile_formula("JJL>1")
    assert f.fields == ("JJL",)
    assert f.eval(s).tolist() == [False, True, False]
    # 注册过、但这只票**没有值** → 全 NaN（条件不成立），**不报错**：
    # 字段名写错在编译期就拦住了，能走到这里的"缺值"只可能是某只票的数据问题，
    # 全市场几千只票各自报一次错会把结论刷成一片"本地数据里没有字段…"。
    assert f.eval(make_series([1.0, 2.0, 3.0])).tolist() == [False, False, False]


def test_unknown_field_still_rejected_without_registration() -> None:
    assert "不存在" in str(compile_error("JJL>1\n"))


# ══════════════════════════════════════════════════════════════════════════
# 四、公式文件加载
# ══════════════════════════════════════════════════════════════════════════


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_text(text, encoding=encoding)
    return path


def test_load_formula_files_with_header(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "# 名称: 放量上攻\n# 说明: 量价齐升\nC>MA(C,5) AND V>MA(V,5)*1.5\n")
    _write(tmp_path / "b.tvf", "MA5:=MA(C,5)\nC>MA5\n")
    specs = fm.load_formula_files(tmp_path)
    assert [s.name for s in specs] == ["放量上攻", "b"]     # 按文件名排序，名字来自注释头
    assert all(s.ok for s in specs)
    assert specs[0].description == "量价齐升"
    assert specs[0].formula is not None
    assert specs[0].formula.name == "放量上攻"
    assert specs[0].path.endswith("a.txt")
    # 没有注释头时退回文件名
    assert specs[1].formula is not None and specs[1].formula.name == "b"


def test_load_formula_files_ignores_other_extensions(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "C>MA(C,5)\n")
    _write(tmp_path / "notes.md", "# 名称: 不该被加载\nC>MA(C,5)\n")
    _write(tmp_path / "b.TVF", "C>MA(C,5)\n")           # 大写扩展名（Windows 上常见）
    specs = fm.load_formula_files(tmp_path)
    assert [Path(s.path).name for s in specs] == ["a.txt", "b.TVF"]


def test_load_formula_files_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert fm.load_formula_files(tmp_path / "不存在") == []


def test_load_formula_files_reports_each_error_separately(tmp_path: Path) -> None:
    """一条公式写错，不能连累同目录里其它公式（界面要能一次显示全部）。"""
    _write(tmp_path / "bad.txt", "# 名称: 坏公式\nC>MAA(C,5)\n")
    _write(tmp_path / "good.txt", "# 名称: 好公式\nC>MA(C,5)\n")
    _write(tmp_path / "bad2.tvf", "# 名称: 坏公式二\nC>0\nD:=1\n")
    specs = {s.name: s for s in fm.load_formula_files(tmp_path)}
    assert set(specs) == {"坏公式", "好公式", "坏公式二"}
    assert specs["好公式"].ok
    assert not specs["坏公式"].ok
    assert '未知函数 "MAA"' in specs["坏公式"].error_text
    assert specs["坏公式"].formula is None
    assert specs["坏公式"].error is not None and specs["坏公式"].error.line == 1
    assert not specs["坏公式二"].ok
    assert "中间语句必须是赋值" in specs["坏公式二"].error_text


def test_load_formula_files_handles_bom_and_crlf(tmp_path: Path) -> None:
    """Windows 记事本存的 UTF-8（带 BOM）+ CRLF 必须能读（否则第一条公式永远报错）。"""
    _write(tmp_path / "win.txt", "# 名称: 记事本保存的\r\nC>MA(C,5)\r\n", encoding="utf-8-sig")
    specs = fm.load_formula_files(tmp_path)
    assert len(specs) == 1 and specs[0].ok
    assert specs[0].name == "记事本保存的"
    assert specs[0].formula is not None
    assert specs[0].formula.text == "C>MA(C,5)"


def test_load_formula_files_fullwidth_colon_in_header(tmp_path: Path) -> None:
    """`# 名称：xxx`（全角冒号）也得认 —— 中文输入法默认打出来的就是这个。"""
    _write(tmp_path / "fw.txt", "# 名称：全角冒号\n# 说明：用输入法打的\nC>MA(C,5)\n")
    specs = fm.load_formula_files(tmp_path)
    assert specs[0].name == "全角冒号"
    assert specs[0].description == "用输入法打的"


def test_load_formula_files_multiline_description(tmp_path: Path) -> None:
    _write(tmp_path / "m.txt", "# 名称: 多行说明\n# 第一行\n# 第二行\nC>MA(C,5)\n")
    specs = fm.load_formula_files(tmp_path)
    assert specs[0].description == "第一行 第二行"


def test_load_formula_files_non_utf8_is_reported(tmp_path: Path) -> None:
    """GBK 编码的文件（国内用户另存为 ANSI 的默认结果）要报"不是 UTF-8"，而不是崩。"""
    (tmp_path / "gbk.txt").write_bytes("# 名称: 国标\nC>MA(C,5)\n".encode("gbk"))
    specs = fm.load_formula_files(tmp_path)
    assert len(specs) == 1
    assert not specs[0].ok
    assert "UTF-8" in specs[0].error_text


def test_shipped_formula_files_all_compile_and_evaluate() -> None:
    """随包分发的示例公式：全部能编译，且能在合成序列上跑出结果（不发散、不报错）。"""
    specs = fm.load_formula_files(FORMULAS_DIR)
    assert len(specs) >= 3, "formulas/ 里至少有 3 条示例公式"
    assert all(s.ok for s in specs), [s.error_text for s in specs if not s.ok]
    s = make_series(
        [10 + i * 0.1 for i in range(120)],
        open_=[10 + i * 0.1 - 0.05 for i in range(120)],
        high=[10 + i * 0.1 + 0.1 for i in range(120)],
        low=[10 + i * 0.1 - 0.1 for i in range(120)],
        vol=[1e6] * 119 + [5e6],
        amount=[1e8] * 120,
        limit_up_days=[1 if i in (100, 111) else 0 for i in range(120)],
        limit_up_cnt=[2 if i == 111 else 0 for i in range(120)],
    )
    for spec in specs:
        assert spec.name and spec.description, spec.path
        assert spec.formula is not None
        out = spec.formula.eval(s)
        assert out.shape == (120,)
        assert spec.formula.fields, f"{spec.name} 应该用到了字段"
    # "均线多头排列"需要 60 根 K 线 —— 元信息要如实反映
    by_name = {s.name: s for s in specs}
    assert by_name["均线多头排列"].formula.min_history == 60


# ══════════════════════════════════════════════════════════════════════════
# 五、数据接入：load_series（离线合成库）
# ══════════════════════════════════════════════════════════════════════════


def _make_db(tmp_path: Path) -> Path:
    """造一个只有 2 只股票、6 个交易日、带涨停池的小库（离线、可重复）。"""
    from laoa_trader.data import storage

    path = storage.init_db(tmp_path / "t.db")
    days = [f"2026-01-{d:02d}" for d in range(1, 7)]
    with storage.connect(path) as conn:
        storage.write_stock_basic(
            conn, [("600001", "样本甲", "半导体"), ("600002", "样本乙", "白酒")]
        )
        rows = []
        for symbol, base in (("600001", 10.0), ("600002", 20.0)):
            for i, day in enumerate(days):
                price = base + i
                rows.append((
                    symbol, day, price, price * 1.02, price * 0.98, price,
                    1e6 + i, 1e7 + i,
                ))
        storage.write_daily_raw(conn, rows)
        storage.write_limit_up_pool(conn, [
            (days[4], "600001", "样本甲", 2, "连板", "09:31:00", "09:45:00", 8e7, 1,
             "芯片", 5.0, 10.0, 1e9, 0, 0, "2连板", 7e7, 12.0, 0, "hithink", "t"),
            # high_days 为 NULL：仍在涨停池里，按 1 板算
            (days[5], "600002", "样本乙", None, "首板", "09:35:00", "09:35:00", 8e7, 0,
             "白酒", 5.0, 10.0, 1e9, 0, 1, "首板", 7e7, 20.0, 0, "hithink", "t"),
        ])
    return path


def test_load_series_basic_fields(tmp_path: Path) -> None:
    path = _make_db(tmp_path)
    series = list(fm.load_series(path))
    assert [s.symbol for s in series] == ["600001", "600002"]
    first = series[0]
    assert first.name == "样本甲"
    assert first.industry == "半导体"
    assert first.date == [f"2026-01-{d:02d}" for d in range(1, 7)]
    np.testing.assert_allclose(first.close, [10, 11, 12, 13, 14, 15])
    np.testing.assert_allclose(first.open, [10, 11, 12, 13, 14, 15])
    np.testing.assert_allclose(first.high, first.close * 1.02)
    np.testing.assert_allclose(first.vol, [1e6 + i for i in range(6)])
    np.testing.assert_allclose(first.amount, [1e7 + i for i in range(6)])
    # 昨收 = 上一根的后复权收盘（第一根没有 → NaN）
    np.testing.assert_array_equal(first.pre_close, [NAN, 10, 11, 12, 13, 14])


def test_load_series_limit_up_columns(tmp_path: Path) -> None:
    path = _make_db(tmp_path)
    a, b = list(fm.load_series(path))
    np.testing.assert_array_equal(a.limit_up_days, [0, 0, 0, 0, 1, 0])
    np.testing.assert_array_equal(a.limit_up_cnt, [0, 0, 0, 0, 2, 0])
    np.testing.assert_array_equal(b.limit_up_days, [0, 0, 0, 0, 0, 1])
    # high_days 为 NULL 时按 1 板算（它毕竟在涨停池里）
    np.testing.assert_array_equal(b.limit_up_cnt, [0, 0, 0, 0, 0, 1])


def test_load_series_symbols_and_start_filters(tmp_path: Path) -> None:
    path = _make_db(tmp_path)
    only = list(fm.load_series(path, symbols=["600002"]))
    assert [s.symbol for s in only] == ["600002"]
    trimmed = list(fm.load_series(path, symbols=["600001"], start="2026-01-04"))
    assert trimmed[0].date == ["2026-01-04", "2026-01-05", "2026-01-06"]
    assert list(fm.load_series(path, symbols=["999999"])) == []


def test_load_series_missing_db_is_data_error(tmp_path: Path) -> None:
    with pytest.raises(fm.FormulaDataError) as excinfo:
        list(fm.load_series(tmp_path / "没有这个库.db"))
    assert "不存在" in str(excinfo.value)


def test_load_series_end_to_end_selection(tmp_path: Path) -> None:
    """接线验证：从库里取数 → 跑公式 → 选出股票（离线，不联网）。"""
    path = _make_db(tmp_path)
    a, b = list(fm.load_series(path))
    # 600001 在第 5 天连板 2 天；600002 只在最后一天进涨停池
    np.testing.assert_array_equal(
        fm.compile_formula("连板()>=2").eval(a),
        [False, False, False, False, True, False],
    )
    np.testing.assert_array_equal(
        fm.compile_formula("涨停天数()>0").eval(b),
        [False, False, False, False, False, True],
    )
    # "当日收盘后选中"的口径：只看最后一根 K 线
    zt = fm.compile_formula("涨停天数()>0")
    assert [s.symbol for s in fm.load_series(path) if zt.eval(s)[-1]] == ["600002"]


# ══════════════════════════════════════════════════════════════════════════
# 六、性能
# ══════════════════════════════════════════════════════════════════════════

#: 一条"什么都有"的公式：18 条语句、5 个字段、11 个函数
COMPLEX_FORMULA = """M5:=MA(C,5)
M10:=MA(C,10)
M20:=MA(C,20)
M60:=MA(C,60)
V5:=MA(V,5)
HH:=HHV(H,20)
LL:=LLV(L,20)
R:=HH/LL
S:=STD(C,20)/M20
CNT:=COUNT(C>O,10)
DD:=CROSS(C,M5)
UP:=C>M5 AND M5>M10 AND M10>M20
BARS:=BARSLAST(连板()>=1)
LIM:=连板()>=1
ZT:=涨停天数(10)>=1
VR:=量比()
E:=EMA(C,12)
C>M5 AND V>V5*1.5 AND R<1.15 AND S<0.05 AND CNT>=3 AND UP AND (DD OR LIM OR ZT) AND VR>1
"""


def test_complex_formula_on_5000_bars_under_50ms() -> None:
    """5000 根 K 线上一条复杂公式 < 50ms。

    取 3 次里**最快**的一次：单次测量在 CI 上会被调度抖动污染（偶尔 3~5 倍），
    而这里要守的是"算法没有写出 O(n²)"，不是"这台机器此刻有多闲"。
    实测约 10ms（见 print 输出），阈值留了 5 倍余量。
    """
    rng = np.random.default_rng(7)
    n = 5000
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    s = fm.Series(
        symbol="600001", name="样本", industry="半导体", date=_dates(n),
        close=close, open=close * 0.99, high=close * 1.02, low=close * 0.98,
        vol=rng.uniform(1e6, 5e6, n), amount=rng.uniform(1e8, 5e8, n),
        pre_close=np.concatenate(([NAN], close[:-1])),
        limit_up_days=(rng.random(n) < 0.05).astype("float64"),
        limit_up_cnt=np.zeros(n),
    )
    formula = fm.compile_formula(COMPLEX_FORMULA)
    import time

    timings = []
    for _ in range(3):
        start = time.perf_counter()
        out = formula.eval(s)
        timings.append((time.perf_counter() - start) * 1000)
    best = min(timings)
    print(f"\n性能：5000 根 K 线求值 {[f'{t:.2f}ms' for t in timings]}（最快 {best:.2f}ms）")
    assert out.shape == (n,)
    assert best < 50.0, f"复杂公式在 5000 根 K 线上耗时 {best:.1f}ms，超过 50ms"


def test_compile_is_fast_enough_for_live_validation() -> None:
    """界面要"边打字边校验"，编译必须是毫秒级（这里只做量级断言，不做基准）。"""
    import time

    start = time.perf_counter()
    for _ in range(50):
        fm.compile_formula(COMPLEX_FORMULA)
    per_call_ms = (time.perf_counter() - start) / 50 * 1000
    print(f"编译一条 18 行公式：{per_call_ms:.2f}ms")
    assert per_call_ms < 20.0


# ══════════════════════════════════════════════════════════════════════════
# 五、快照字段与排除标记（2026-09-18 用户点名要的两件事）
# ══════════════════════════════════════════════════════════════════════════


def _series_with(symbol: str = "600000", name: str = "浦发银行", *, days: int = 3,
                 price: float = 10.0, **extra_fields: float) -> fm.Series:
    """造一条序列（可选把快照字段塞在**最后一根**上，与 `load_series` 的铺法一致）。"""
    extra = {
        key: np.concatenate([np.full(days - 1, np.nan), np.array([float(value)])])
        for key, value in extra_fields.items()
    }
    return fm.Series(
        symbol=symbol, name=name, industry="银行",
        date=[f"2026-09-0{i + 1}" for i in range(days)],
        close=np.full(days, price), open=np.full(days, price),
        high=np.full(days, price), low=np.full(days, price),
        vol=np.full(days, 100.0), amount=np.full(days, 1_000_000.0),
        pre_close=np.full(days, price), limit_up_days=np.zeros(days),
        limit_up_cnt=np.zeros(days), extra=extra,
    )


def test_snapshot_fields_are_registered_and_usable() -> None:
    """`流通市值` / `换手率` 是注册字段，能编译、能比较（单位：亿 / %）。"""
    f = fm.compile_formula("流通市值>=10 AND 流通市值<=300 AND 换手率>5")
    assert set(f.fields) == {"流通市值", "换手率"}
    assert bool(f.eval(_series_with(流通市值=50.0, 换手率=8.0))[-1]) is True
    assert bool(f.eval(_series_with(流通市值=5.0, 换手率=8.0))[-1]) is False


def test_snapshot_field_without_data_yields_no_signal() -> None:
    """拿不到快照（这只票没有值）→ **条件不成立**，既不报错也不出信号。

    为什么必须是"不成立"而不是"报错"：`流通市值`/`换手率` 只有实时快照才给，
    全市场总有一些票取不到；各自报一次错会把试算结论刷成一片错误信息，
    而真正的答案只是"它们今天不满足条件"。
    """
    f = fm.compile_formula("流通市值>=10")
    assert f.eval(_series_with())[-1] == False  # noqa: E712 - numpy bool 显式比较


def test_snapshot_field_is_only_known_on_the_last_bar() -> None:
    """这两个数**只有今天这一个值**：前面几根是 NaN，所以 `MA(流通市值,5)` 出不了信号。

    这条钉的是"不许拿今天的值倒推历史"—— 那会造出一条看起来很合理的假均线。
    """
    series = _series_with(days=6, 流通市值=50.0)
    f = fm.compile_formula("MA(流通市值,5)>10")
    assert not bool(f.eval(series)[-1])


def test_flag_fields_are_filled_automatically() -> None:
    """`ST` / `科创` / `北交所` **不用调用方准备**：由代码与名称自动补齐（不联网）。

    漏塞一次就会让用户写好的 `ST=0` 报"本地数据里没有字段 ST 的数据"——
    那是最莫名其妙的失败方式，所以在 `Series.__post_init__` 里就补上了。
    """
    assert _series_with("600519", "贵州茅台").extra["ST"][-1] == 0.0
    assert _series_with("600004", "*ST某某").extra["ST"][-1] == 1.0
    assert _series_with("688111", "金山办公").extra["科创"][-1] == 1.0
    assert _series_with("300750", "宁德时代").extra["科创"][-1] == 0.0   # 创业板不是科创
    assert _series_with("920000", "安徽凤凰").extra["北交所"][-1] == 1.0
    assert _series_with("430047", "诺思兰德").extra["北交所"][-1] == 1.0
    assert _series_with("600519", "贵州茅台").extra["北交所"][-1] == 0.0


def test_exclude_conditions_work_as_users_write_them() -> None:
    """用户那条策略里的"排除ST 排除科创 排除北交所" = `ST=0 AND 科创=0 AND 北交所=0`。"""
    f = fm.compile_formula("ST=0 AND 科创=0 AND 北交所=0")
    assert bool(f.eval(_series_with("600519", "贵州茅台"))[-1]) is True
    assert bool(f.eval(_series_with("600004", "*ST某某"))[-1]) is False
    assert bool(f.eval(_series_with("688111", "金山办公"))[-1]) is False
    assert bool(f.eval(_series_with("920000", "安徽凤凰"))[-1]) is False


def test_s_share_is_not_st() -> None:
    """未股改 `S` 股（`S佳通`）**不算 ST**：判据是名称含 `ST`，不是含 `S`。"""
    assert fm.is_st_name("S佳通") is False
    assert fm.is_st_name("*ST英飞") is True
    assert fm.is_st_name("ST某某") is True


def test_the_full_six_condition_strategy_compiles() -> None:
    """用户给的那 6 条条件**整条**能编译、能跑（这条是这次改动的验收）。"""
    f = fm.compile_formula(
        "LTSZ_OK:=流通市值>=10 AND 流通市值<=300\n"
        "HSL_OK:=换手率>5\n"
        "PRICE_OK:=C>=3 AND C<=50\n"
        "VOL_UP:=V>REF(V,1)*1.5\n"
        "LTSZ_OK AND HSL_OK AND PRICE_OK AND VOL_UP AND 量比()>=1.5 "
        "AND ST=0 AND 科创=0 AND 北交所=0"
    )
    ok = _series_with("600000", "浦发银行", days=7, 流通市值=50.0, 换手率=8.0)
    vols = np.full(7, 100.0); vols[-1] = 160.0
    amts = np.full(7, 1_000_000.0); amts[-1] = 3_000_000.0
    ok.vol, ok.amount = vols, amts
    assert bool(f.eval(ok)[-1]) is True
