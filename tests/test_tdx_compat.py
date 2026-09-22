"""通达信（TDX）公式兼容层：**网上抄来的选股公式要能直接跑**。

用户原话（2026-09-21）：「我要的是通达信公式进来直接能跑」。

这一层做三件事，本文件逐件钉住：

1. **语法容错** —— 语句用 `;` 结尾、输出用 `:`、样式修饰 `,COLORRED`、画图语句
   （DRAWICON/DRAWTEXT/STICKLINE…）、全角标点、`<>` 表示"不等于"。
   这些在 TDX 的成品公式里到处都是，少认一样就是"整条公式报错"。
2. **函数补齐** —— KDJ/RSI/BOLL、EXIST/EVERY/BETWEEN、SMA/DMA/WMA、SLOPE/FORCAST…
   其中 `SMA` 是**加权递推**（不是 MA），这是最容易算错的一个，所以这里用
   "独立手写的递推"做交叉验证，而不是拿我们自己的实现自证。
3. **忽略要说出来** —— 画图语句被跳过、样式修饰被忽略，都必须写进
   `Formula.notes`，界面才能告诉用户"我那句画线没生效"。

测试里**不联网、不读库**：序列用 `make_series()` 手搓（与 `test_formula.py` 同一套）。
"""

from __future__ import annotations

import numpy as np
import pytest

from laoa_trader.strategy import formula as fm

from tests.test_formula import make_series


def _eval(text: str, series: fm.Series) -> np.ndarray:
    """编译并求值（返回最后一根 K 线的结果；布尔公式返回 True/False）。"""
    formula = fm.compile_formula(text)
    mask = formula.eval(series)
    return bool(mask[-1])


def _close_to(expr: str, expected: float, series: fm.Series, eps: float = 1e-6) -> bool:
    """断言"表达式在最后一根 K 线上的数值 ≈ expected"。

    为什么绕这一下：引擎对外只暴露布尔结果（`Formula.eval` 返回 0/1 掩码），
    没有"取值"的公开口子。与其为测试加一条旁路，不如把断言写成
    `R>=expected-eps AND R<=expected+eps` —— 效果一样，还不给引擎添新 API。
    """
    # 用定点小数写死（`.12f`）：`repr(1e-06)` 会给出科学计数法 `1e-06`，
    # 而公式语言的数字字面量**不支持**指数写法，那样会报"不认识的字符"。
    lo, hi = expected - eps, expected + eps
    return _eval(f"R:={expr}\nR>={lo:.12f} AND R<={hi:.12f}", series)


# ══════════════════════════════════════════════════════════════════════════
# 一、真实风格的 TDX 选股公式：能编译、能跑出结果（不报错）
# ══════════════════════════════════════════════════════════════════════════

#: 从网上抄来的典型写法（保留原有语法点：`:` 输出 / `;` 结尾 / COLORxxx / DRAWICON /
#: `<>` / SMA / EXIST / BETWEEN / 全角括号），只把"每只票的具体条件"留下。
TDX_SAMPLES: dict[str, str] = {
    "KDJ金叉": """RSV:=(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100;
K:SMA(RSV,3,1);
D:SMA(K,3,1);
J:3*K-2*D,COLORRED;
DRAWICON(CROSS(K,D),20,1);
CROSS(K,D) AND K<80;""",
    "MACD金叉": """DIF:EMA(C,12)-EMA(C,26),COLORRED;
DEA:EMA(DIF,9),COLORGREEN;
MACD:(DIF-DEA)*2,COLORSTICK;
DRAWICON(CROSS(DIF,DEA) AND DIF<0,0,1);
CROSS(DIF,DEA) AND C>MA(C,20);""",
    "RSI超卖反弹": """RSI6:=RSI(C,6);
RSI12:=RSI(C,12);
DRAWTEXT(CROSS(RSI6,RSI12),30,'金叉');
CROSS(RSI6,RSI12) AND RSI6<50;""",
    "布林突破": """MID:=BOLL(C,20);
UP:=UB(C,20,2);
LB2:=LB(C,20,2);
C>UP AND V>MA(V,5) AND CLOSE<>OPEN;""",
    "均线多头+量能": """MA5:MA(C,5),COLORRED;
MA10:MA(C,10);
MA20:MA(C,20),COLORGREEN,LINETHICK2;
MA60:MA(C,60);
DRAWKLINE(H,O,L,C);
MA5>MA10 AND MA10>MA20 AND EXIST(V>REF(V,1)*2,5) AND BETWEEN(C,3,50);""",
    "缩量回踩": """ZT:=EXIST(C/REF(C,1)>1.095,10);
SHRINK:=V<MA(V,5)*0.8;
PULL:=C>=MA(C,5)*0.98 AND C<REF(C,1);
SUMBARS(V,MA(V,20)*3)<20 AND ZT AND SHRINK AND PULL;""",
    "首板缩量整理": """PRE1:=REF(C,1)/REF(C,2)-1;
PRE2:=REF(C,2)/REF(C,3)-1;
TODAY:=C/REF(C,1)-1;
EVERY(C>MA(C,20),3) AND PRE1>=0.095 AND PRE2<0.095 AND TODAY<0.09 AND V<REF(V,1);""",
    "全角与不等于混写": """MA5:MA（C，5）;
A:=（C>MA5） AND （V<>REF（V，1））;
A AND UPNDAY（C，3） AND HHVBARS(H,20)>=3;""",
}


@pytest.mark.parametrize("name", sorted(TDX_SAMPLES))
def test_real_world_tdx_formula_compiles_and_runs(name: str) -> None:
    """每条样例都要**能编译、能求值**（不报错就是通过 —— 有没有信号不影响这条）。"""
    text = TDX_SAMPLES[name]
    series = make_series(
        np.linspace(10.0, 12.0, 90) + np.sin(np.arange(90)) * 0.3,
        high=np.linspace(10.2, 12.3, 90),
        low=np.linspace(9.8, 11.7, 90),
        vol=np.linspace(1e6, 2e6, 90),
    )

    formula = fm.compile_formula(text)
    mask = formula.eval(series)               # 不抛异常即通过

    assert mask.shape == (90,)
    assert formula.min_history >= 1


def test_drawing_statements_are_reported_as_notes() -> None:
    """画图语句被忽略，但必须**说出来**（否则用户以为画线生效了）。"""
    text = "MA5:MA(C,5),COLORRED;\nDRAWICON(C>O,10,1);\nDRAWTEXT(C>O,20,'阳');\nC>MA5;"

    formula = fm.compile_formula(text)

    notes = " ".join(formula.notes)
    assert "画图语句" in notes
    assert "样式修饰" in notes and "COLORRED" in notes
    assert len(formula.statements) == 2       # 画图那两句没进语句表


def test_semicolon_and_colon_are_accepted() -> None:
    """`;` 当语句分隔、`:` 当输出：一整条公式写在一行里也要能跑。"""
    formula = fm.compile_formula("A:=MA(C,5); B:=MA(C,10); A>B;")

    assert len(formula.statements) == 3
    # `:=` 不是输出、`:` 才是；这条公式用的是 `:=`，所以没有输出线
    assert formula.outputs == ()


def test_unknown_but_harmless_style_suffix_is_ignored() -> None:
    """认不出的样式后缀（`SHIFT3` 之类）不该让整条公式失败。"""
    formula = fm.compile_formula("MA5:MA(C,5),SHIFT3;\nC>MA5;")

    assert len(formula.statements) == 2


# ══════════════════════════════════════════════════════════════════════════
# 二、新函数的正确性：用**独立实现**交叉验证（不拿自己自证）
# ══════════════════════════════════════════════════════════════════════════


def _series_for_funcs(n: int = 60) -> fm.Series:
    rng = np.random.default_rng(7)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    return make_series(
        close,
        open_=close - 0.05,
        high=close + 0.15,
        low=close - 0.15,
        vol=np.abs(rng.normal(1e6, 2e5, n)),
    )


def test_sma_is_the_weighted_recursion_not_a_simple_average() -> None:
    """`SMA(X,N,M)` 是 `Y=(M*X+(N-M)*Y_prev)/N` —— 与 `MA` **不同**（最常见的误用）。"""
    series = _series_for_funcs()
    x = series.close

    # 独立手写一遍递推（与引擎实现互不相干）
    prev = np.nan
    for xi in x:
        prev = xi if np.isnan(prev) else (1.0 * xi + (3 - 1) * prev) / 3.0

    assert _close_to("SMA(C,3,1)", prev, series, eps=abs(prev) * 1e-9 + 1e-9)
    assert abs(prev - float(np.mean(x[-3:]))) > 1e-6   # 与简单平均必须**不一样**


def test_ma_and_boll_match_pandas() -> None:
    series = _series_for_funcs()
    x = series.close

    assert _close_to("MA(C,5)", float(np.mean(x[-5:])), series)
    assert _close_to("BOLL(C,20)", float(np.mean(x[-20:])), series)
    assert _close_to("UB(C,20,2)", float(np.mean(x[-20:]) + 2 * np.std(x[-20:])), series)
    assert _close_to("LB(C,20,2)", float(np.mean(x[-20:]) - 2 * np.std(x[-20:])), series)


def test_rsi_matches_hand_written_recursion() -> None:
    series = _series_for_funcs()
    x = series.close

    diff = np.diff(x, prepend=np.nan)
    up, absd = np.maximum(diff, 0.0), np.abs(diff)

    def sma(arr: np.ndarray, n: int, m: int) -> np.ndarray:
        out = np.full(arr.size, np.nan)
        prev = np.nan
        for i, v in enumerate(arr):
            if np.isnan(v):
                out[i] = prev
                continue
            prev = v if np.isnan(prev) else (m * v + (n - m) * prev) / n
            out[i] = prev
        return out

    num, den = sma(up, 6, 1), sma(absd, 6, 1)
    with np.errstate(all="ignore"):
        want = num[-1] / den[-1] * 100.0
    assert _close_to("RSI(C,6)", float(want), series, eps=1e-6)


def test_kdj_chain_matches_hand_written_recursion() -> None:
    series = _series_for_funcs()

    rsv_series = np.array([_rsv_at(series, i) for i in range(series.close.size)])
    k_series = _sma_hand(rsv_series, 3, 1)
    d_series = _sma_hand(k_series, 3, 1)

    assert _close_to("RSV()", float(rsv_series[-1]), series)
    assert _close_to("K()", float(k_series[-1]), series)
    assert _close_to("D()", float(d_series[-1]), series)
    assert _close_to("J()", float(3 * k_series[-1] - 2 * d_series[-1]), series)


def _rsv_at(series: fm.Series, i: int) -> float:
    """第 i 根的 RSV（手算，独立于引擎实现）。"""
    if i < 8:
        return float("nan")
    hh = float(np.max(series.high[i - 8:i + 1]))
    ll = float(np.min(series.low[i - 8:i + 1]))
    if hh == ll:
        return float("nan")
    return (float(series.close[i]) - ll) / (hh - ll) * 100.0


def _sma_hand(arr: np.ndarray, n: int, m: int) -> np.ndarray:
    out = np.full(arr.size, np.nan)
    prev = np.nan
    for i, v in enumerate(arr):
        if np.isnan(v):
            out[i] = prev
            continue
        prev = v if np.isnan(prev) else (m * v + (n - m) * prev) / n
        out[i] = prev
    return out


def test_exist_every_and_count_agree_with_each_other() -> None:
    """EXIST/EVERY 与 COUNT 是同一件事的不同问法，三者必须自洽。"""
    series = _series_for_funcs()
    n = series.close.size

    c = series.close
    up = c > np.concatenate(([np.nan], c[:-1]))
    last5 = up[-5:]

    assert _eval("EXIST(C>REF(C,1),5)", series) is bool(np.any(last5))
    assert _eval("EVERY(C>REF(C,1),5)", series) is bool(np.all(last5))
    assert _close_to("COUNT(C>REF(C,1),5)", float(np.sum(last5)), series)


def test_hhvbars_and_upnday() -> None:
    series = _series_for_funcs()
    x = series.close

    assert _close_to("HHVBARS(H,20)", float(19 - int(np.argmax(series.high[-20:]))),
                     series, eps=1e-9)
    assert _close_to("LLVBARS(L,20)", float(19 - int(np.argmin(series.low[-20:]))),
                     series, eps=1e-9)
    assert _eval("UPNDAY(C,3)", series) is bool(
        all(x[k] > x[k - 1] for k in (x.size - 3, x.size - 2, x.size - 1))
    )
    assert _eval("DOWNNDAY(C,3)", series) is bool(
        all(x[k] < x[k - 1] for k in (x.size - 3, x.size - 2, x.size - 1))
    )


def test_slope_and_forcast_match_least_squares() -> None:
    series = _series_for_funcs()
    x = series.close

    t = np.arange(10, dtype="float64")
    k, b = np.polyfit(t, x[-10:], 1)

    assert _close_to("SLOPE(C,10)", float(k), series, eps=1e-9)
    assert _close_to("FORCAST(C,10)", float(k * 9 + b), series, eps=1e-6)


def test_between_and_range_accept_either_order() -> None:
    series = _series_for_funcs()
    c = series.close[-1]

    assert _eval(f"BETWEEN(C,{c - 1},{c + 1})", series) is True
    assert _eval(f"BETWEEN(C,{c + 1},{c - 1})", series) is True      # 大小顺序反过来也认
    assert _eval(f"BETWEEN(C,{c + 1},{c + 2})", series) is False
    assert _eval(f"RANGE(C,{c - 1},{c + 1})", series) is True


def test_math_helpers() -> None:
    series = _series_for_funcs()
    c = series.close[-1]

    assert _close_to("POW(C,2)", c * c, series, eps=1e-6)
    assert _close_to("SQRT(C)", float(np.sqrt(c)), series)
    assert _close_to("INTPART(C)", float(np.trunc(c)), series, eps=1e-9)
    assert _close_to("ROUND(C)", float(round(c)), series, eps=1e-9)
    assert _close_to("MOD(C,2)", c % 2, series, eps=1e-9)
    assert _eval("SIGN(C-PRE)>=0 OR SIGN(C-PRE)<0", series) is True


def test_unsupported_functions_say_so_in_chinese() -> None:
    """不支持的（财务/动态行情/筹码那类）要给人话 —— **而且不是"未知函数"那一套**。

    这条用例在主人实报「FINANCE 只给了一句干巴巴的未知函数」之后**加强了**：
    原来只断言"有中文"，现在要求错误码是 `unsupported_function`、提示里说清缺什么、
    FINANCE 还要给出「改用流通市值」这条可执行的路。详见下面第四节那组用例。
    """
    for text, need in (("DYNAINFO(3)>0", "日线"),
                       ("FINANCE(40)>0", "财务"),
                       ("WINNER(C)>0.5", "筹码"),
                       ("COST(50)>C", "筹码")):
        with pytest.raises(fm.FormulaError) as err:
            fm.compile_formula(text)
        assert err.value.code == "unsupported_function", text
        assert need in (err.value.hint or ""), text
        assert "未知函数" not in str(err.value), text
        assert any("\u4e00" <= ch <= "\u9fff" for ch in str(err.value))

    # 引用其它公式（TDX 的 `"公式名.指标"`）：本地没有那套公式库 → 专门的中文说明
    with pytest.raises(fm.FormulaError) as err:
        fm.compile_formula('"MACD.DIF">0')
    assert err.value.code == "formula_ref"
    assert "公式" in (err.value.hint or "")


def test_a_formula_with_only_drawing_statements_is_rejected() -> None:
    """整条公式只有画图语句 → 没有选股条件，必须报错（而不是"编译通过但什么都不做"）。"""
    with pytest.raises(fm.FormulaError):
        fm.compile_formula("DRAWICON(C>O,10,1);\nDRAWTEXT(C>O,20,'阳');")


# ══════════════════════════════════════════════════════════════════════════
# 四、"已知但数据上做不到"的函数：要说清缺什么、能换成什么
#
# 主人实报：粘了一条用 FINANCE 的公式，只得到一句干巴巴的「未知函数 "FINANCE"」——
# 那会让人以为是自己打错了字，然后一直在函数名上较劲，而真正的原因是本地没有财务数据。
# 所以这几条不钉整句文案（文案会改），只钉"关键词必须在"。
# ══════════════════════════════════════════════════════════════════════════


def _error_of(text: str) -> fm.FormulaError:
    with pytest.raises(fm.FormulaError) as excinfo:
        fm.compile_formula(text)
    return excinfo.value


@pytest.mark.parametrize(
    ("text", "keywords"),
    (
        # FINANCE：财务/股本 —— 必须给出"用流通市值代替"这条路
        ("X:=FINANCE(7);\nX>0", ("财务", "股本", "流通市值")),
        # DYNAINFO：盘中动态行情 —— 必须说清只有日线，并给现价/量比的替代
        ("X:=DYNAINFO(3);\nX>0", ("动态行情", "日线", "量比")),
        # WINNER / COST：筹码分布
        ("X:=WINNER(C);\nX>0", ("筹码", "换手率")),
        ("X:=COST(50);\nX>0", ("筹码",)),
    ),
)
def test_known_but_unsupported_functions_have_a_real_explanation(
        text: str, keywords: tuple[str, ...]) -> None:
    """这些函数**不是**"未知函数"：要说明缺什么数据、并给出替代写法。"""
    error = _error_of(text)

    assert error.code == "unsupported_function", "应当与「未知函数」分开（错误码不同）"
    assert error.line == 1 and error.col is not None, "要有行列号，用户才能定位"
    blob = str(error) + " " + (error.hint or "")
    for word in keywords:
        assert word in blob, f"提示里缺少「{word}」：{blob!r}"
    # 而且**不许**退回那句干巴巴的"未知函数"（否则用户还是会去改函数名）
    assert "未知函数" not in str(error)


def test_unknown_function_still_lists_the_available_ones() -> None:
    """真正的"未知函数"照旧：列可用函数清单 + 近似建议（两类提示不能混）。"""
    error = _error_of("X:=MAA(C,5);\nX>0")

    assert error.code == "unknown_function"
    assert "未知函数" in str(error)
    assert "可用函数" in (error.hint or "")
    assert error.hint and "MA" in error.hint


@pytest.mark.parametrize(
    ("text", "keywords"),
    (
        ("X:=C#WEEK;\nX>0", ("跨周期", "日线")),
        ("X:=C#MONTH;\nX>0", ("跨周期", "日线")),
    ),
)
def test_cross_period_syntax_gets_its_own_message(text: str, keywords: tuple[str, ...]) -> None:
    """`C#WEEK` 这种跨周期写法必须**专门报错**，不能被当成注释整行吃掉。

    为什么这条特别重要：`#` 在本引擎里是注释符，若照注释处理，这一行会被静默丢掉 ——
    用户看到的是"公式能编译、结果就是不对"，那是所有问题里最难查的一种。
    """
    error = _error_of(text)

    assert error.code == "period"
    blob = str(error) + " " + (error.hint or "")
    for word in keywords:
        assert word in blob


def test_hash_comments_still_work() -> None:
    """反过来钉住：普通的 `#` 注释（公式文件的注释头就是它）照旧能用。"""
    formula = fm.compile_formula("# 说明: 注释行\nC>MA(C,5)")
    assert formula.min_history == 5


def test_formula_reference_gets_its_own_message() -> None:
    """`"MACD.MACD"`（通达信引用别的公式）要专门说明，而不是变成「字符串不能参与运算」。"""
    error = _error_of('X:="MACD.MACD";\nX>0')

    assert error.code == "formula_ref"
    blob = str(error) + " " + (error.hint or "")
    assert "公式" in blob and ("抄" in blob or "内置" in blob)


def test_an_industry_string_is_not_mistaken_for_a_formula_reference() -> None:
    """行业名这种字符串**不能**被误判成公式引用（`"半导体"` 没有点、判据要够严）。"""
    formula = fm.compile_formula('INDUSTRY="半导体"')
    assert formula is not None
