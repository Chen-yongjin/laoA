"""自定义选股公式引擎：通达信/同花顺风格公式的**解析器 + 白名单向量化求值器**。

为什么要这个模块
----------------
内置的 5 条策略是"我们挑的"，而用户（尤其是要把程序分发出去的人）总想写自己的条件：
"5 日线上穿 + 量能放大 1.5 倍 + 近期有过涨停"这类话，用公式表达比改代码现实得多。
本模块只做**引擎核心**：语法 → AST → 求值。界面、配置文件、CLI 都不在这里碰，
所以它是 Qt 无关、可离线单测的（`tests/test_formula.py` 全程不联网）。

语言是通达信/同花顺风格的**明确子集**（不是全集，也不打算做全集）：::

    M5:=MA(C,5)                       { 中间变量，不输出；花括号是注释 }
    M10:=MA(C,10)
    C>M5 AND M10>REF(M10,1) AND V>MA(V,5)*1.5 AND 连板()>=2

最后一行必须是**选股条件**（返回 0/1 的表达式），其余行只能是 `X:=...` 赋值。
信号为 True 表示"当日收盘后选中"——与项目现有口径一致（选股看每只票最后一根 K 线）。

为什么**绝对不用** eval / exec
------------------------------
公式是**用户可编辑的文本**，而且这份程序**要分发给别人**。把 `eval` 接到用户输入上，
哪怕自认为只允许"表达式"，也等于交出了 `__import__("os").system(...)` 这类能力：
表达式里能写属性访问、能调任意对象，靠正则黑名单是拦不住的（历史上大量漏洞都是这么来的，
`__class__.__bases__` 之类能绕过一切字符过滤）。所以这里走完全不同的路：

    tokenizer（自己写）→ 递归下降解析成 AST → 白名单求值器
    （只认有限的节点类型 / 字段名 / 函数名，别的名字在**解析期**就报中文错）

整条链路上没有任何 `eval`/`exec`/`compile`/动态 `getattr`：用户输入最多变成
"一个我们自己定义的 AST 节点"。`_Evaluator.eval_node()` 末尾那句 raise 就是兜底——
真的出现不认识的节点，宁可报错也不执行。

为什么 NaN 要比成 False（而不是抛异常、也不是当成 0）
----------------------------------------------------
停牌、上市不足 N 日、窗口不够长、除以 0 —— 这些在真实数据里**每天都会出现**，
所以"缺值"是常态而不是异常：

* 抛异常 → 一只股票的缺值会让整轮选股崩掉（分发出去的程序不可接受）；
* 当成 0 → `V>MA(V,5)` 会在缺量日算成 `0>1000` = False（碰巧对），
  但 `C!=0` 会算成 True（**错**），停牌日被选进池子；
* 比成 False → 缺值**永远不产生信号**，语义是"数据不足就不选它"，这是唯一安全的默认。

所以内部约定是：**条件型值也是 0 / 1 / NaN 的浮点序列**（NaN = 缺值/未知），
沿着 AND/OR/NOT/COUNT/IF 一路传播，只在最后一步折成布尔（NaN → False）。
为什么要这么绕：如果比较直接吐布尔，`NOT` 会把缺值翻成 True
（`NOT (C>MA(C,5))` 在 MA 还没算出来的前 4 根上全为真），等于"数据不足"变成了买入信号 ——
而这是回测/选股里最贵的一种错。（见 `_logic_combine` / `_not` / `_count`。）

特别注意 IEEE 的坑：`NaN != 5` 在 float 语义下是 **True**。所以 `=`/`!=` 必须显式
把缺值掩成 NaN（见 `_num_cmp`），否则"停牌日不等于 5"会变成一条选股信号。
"""

from __future__ import annotations

import difflib
import math
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from laoa_trader.data.engine import HFQ_TABLE
from laoa_trader.log import get_logger

logger = get_logger(__name__)

# ── 硬限制（分发产品的防炸边界；全部给中文错误）──
#: 单条公式最大字符数
MAX_FORMULA_CHARS = 2000
#: 单条公式最大语句数（一行一条）
MAX_FORMULA_LINES = 60
#: AST 节点数上限（防止 `1+1+1+...` 这类把解析/求值拖垮）
MAX_AST_NODES = 2000
#: 表达式/括号最大嵌套深度。**必须限制**：Python 递归深度默认 1000，
#: 一串 `(((((...` 会直接 RecursionError（未捕获异常 = 违反"全部中文错误"）。
MAX_DEPTH = 64
#: 窗口参数 N 的合法区间。上限同时兜住 `HHV(C,10**9)` 这种"常数爆炸"
MAX_WINDOW = 5000
#: 数字字面量的绝对值上限（防止字面量溢出成 inf 参与比较）
MAX_LITERAL = 1e15


# ══════════════════════════════════════════════════════════════════════════
# 错误类型
# ══════════════════════════════════════════════════════════════════════════


class FormulaError(Exception):
    """公式错误：**结构化**（行号 / 列号 / 机器可读 code）+ 中文消息。

    为什么要结构化而不是一句 str：下一轮界面要拿它做两件事 ——
    ① 把光标定位到出错位置；② 按 code 分类（比如 `unknown_field` 可以顺手提示可用字段）。
    所以 `message` 里**不含**行号前缀（那是展示层的事），`__str__` 才拼上去。
    """

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        col: int | None = None,
        code: str = "syntax",
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.code = code
        self.hint = hint

    def __str__(self) -> str:  # noqa: D105 - 见类注释
        if self.line is None:
            head = ""
        elif self.col is None:
            head = f"第 {self.line} 行："
        else:
            head = f"第 {self.line} 行第 {self.col} 列："
        tail = f"（{self.hint}）" if self.hint else ""
        return f"{head}{self.message}{tail}"

    def __repr__(self) -> str:
        return f"FormulaError({str(self)!r}, code={self.code!r})"

    def to_dict(self) -> dict[str, Any]:
        """给界面用的字典（JSON 友好）。"""
        return {
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "code": self.code,
            "hint": self.hint,
            "text": str(self),
        }


class FormulaDataError(RuntimeError):
    """数据侧的问题（库不存在、字段长度不一致……）—— **不是**公式本身的错。

    与 `FormulaError` 分开：这样界面能区分"你公式写错了"（要改公式）
    和"本地数据不够/坏了"（要去下载数据），两者的下一步动作完全不同。
    """


# ══════════════════════════════════════════════════════════════════════════
# 字段表
# ══════════════════════════════════════════════════════════════════════════

#: 别名（大写）→ 规范字段名。字段大小写不敏感，短名长名都认。
FIELD_ALIASES: dict[str, str] = {
    "C": "C",
    "CLOSE": "C",
    "O": "OPEN",
    "OPEN": "OPEN",
    "H": "HIGH",
    "HIGH": "HIGH",
    "L": "LOW",
    "LOW": "LOW",
    "V": "VOL",
    "VOL": "VOL",
    "VOLUME": "VOL",
    "AMO": "AMOUNT",
    "AMOUNT": "AMOUNT",
    "PRE": "PRE_CLOSE",
    "PRE_CLOSE": "PRE_CLOSE",
    "DATE": "DATE",
    "INDUSTRY": "INDUSTRY",
    # 快照字段**不设英文别名**（只有中文名，见 `EXTRA_FIELDS`）：别名会占住那个名字，
    # 用户再写 `LTSZ:=流通市值` 就会被拒（"变量名与内置字段同名"）—— 一个平白无故的坑。
    # 中文名与 `量比` / `连板` / `涨停天数` 一致，而且编辑器右边的面板点一下就插入。
}

#: 规范字段名 → 静态类型（num = 数值序列，str = 字符串）
FIELD_KINDS: dict[str, str] = {
    "C": "num",
    "OPEN": "num",
    "HIGH": "num",
    "LOW": "num",
    "VOL": "num",
    "AMOUNT": "num",
    "PRE_CLOSE": "num",
    "DATE": "str",
    "INDUSTRY": "str",
}

#: 扩展字段注册表（规范名 → 静态类型）。
#:
#: 登记之后，解析器与求值器就认这个名字，取值走 `Series.extra`（同名字的一维数组）。
#: 目前有两类：
#:
#: 1. **快照字段**（只有"今天"这一个值，由调用方从实时快照塞进来）：
#:    `流通市值`（亿元）、`换手率`（%）—— 它们是用户点名要的两个选股条件，
#:    而日线里没有这两个数（同花顺的快照端点也不返回，见 `data/sources.py` 的
#:    `SUPPLEMENT_FIELDS`）。写法：把"今天"的值放在数组**最后一个位置**、
#:    其余填 NaN —— 于是 `流通市值>=10` 只在最后一根 K 线上成立，
#:    与"只看最后一根选股"的口径天然一致；取不到快照时整条都是 NaN，
#:    条件不成立（**宁可不出信号，也不拿旧值凑**）。
#: 2. **排除类标记**（0/1，**不需要联网**）：`ST`、`科创`、`北交所` ——
#:    它们完全由代码与名称推出来，在 `Series.__post_init__` 里自动补齐，
#:    所以任何入口（试算 / 选股 / 成绩单）都直接可用。
#:    用户写法：`ST=0 AND 科创=0 AND 北交所=0`（= 排除这三类，
#:    正是用户那条策略里的"排除ST 排除科创 排除北交所"）。
EXTRA_FIELDS: dict[str, str] = {
    "流通市值": "num",
    "换手率": "num",
    # ── 盘中快照口径（2026-09-23 加，用户："必须加进去啊"）──
    # 这四个与 `流通市值`/`换手率` 一样来自**实时快照**，但语义是"**现在这一刻**的盘面"
    # （用户在盘中点【运行】/【开始选股】时取当时的值），而不是日线最后一根的收盘值：
    #   `现价` = 最新价（元）；`现涨幅` = 当日涨跌幅（**百分数**，3.2 表示 +3.2%）；
    #   `现量比` = 量比（倍）；`现换手` = 换手率（%）。
    # ⚠️ 两个硬限制（界面的 tooltip 与文档都写着）：非交易时段取不到 → 条件不成立、
    # 一只都不出；快照只有"当下"这一个值 → **没有历史、不能回测**。
    "现价": "num",
    "现涨幅": "num",
    "现量比": "num",
    "现换手": "num",
    "热门行业": "num",
    "ST": "num",
    "科创": "num",
    "北交所": "num",
    "沪市": "num",
    "深市": "num",
    "创业板": "num",
}

#: 热门行业的"上榜窗口"（交易日）：`热门行业` 的值 = 最近这么多天里上过热门榜的次数。
#: 用户 2026-09-18 定的口径："最近 3 天上榜的" —— 只算当天会把一天脉冲当成热门，
#: 放宽到 3 天更稳（值域 0~3，写 `热门行业>=1` 就是"上过榜"）。
HOT_INDUSTRY_DAYS = 3

#: 排除类标记的规范名（自动补齐用；顺序即"用户最可能一起写"的顺序）
FLAG_FIELDS: tuple[str, ...] = ("ST", "科创", "北交所", "沪市", "深市", "创业板")

_NUM = "num"
_BOOL = "bool"
_STR = "str"

_KIND_LABEL = {_NUM: "数值", _BOOL: "条件（0/1）", _STR: "字符串"}


def is_st_name(name: Any) -> bool:
    """名称里带 `ST` 就算（`*ST` / `ST` / `SST` / `S佳通` 里的 `S` 不算）。

    A 股的风险警示股名称一定含 `ST`（`*ST` 也含），所以按名称判既准又不用额外数据；
    名称取自本地 `stock_basic`（选股流程本来就在读它）。
    """
    return "ST" in str(name or "").upper()


def is_star_market(symbol: Any) -> bool:
    """是不是**科创板**（`688` / `689` 开头）。

    为什么单列一个"科创"标记、而不跟创业板合并：用户要的是"排除科创"，
    而科创板的涨跌停是 20%、门槛与创业板也不同 —— 混在一起会让"排除科创"
    顺带把创业板也排掉（那不是他要的）。
    """
    code = str(symbol or "").strip()
    return code.startswith(("688", "689"))


def is_bse(symbol: Any) -> bool:
    """是不是**北交所**（`4` / `8` / `92` 号段）。

    号段依据（与 `data/public_sync.board_of` 同一套规则，实测 2026-09-17 全市场
    5564 只反推验证过）：北交所是 `43/83/87/88`（旧号段）与 `920`（新号段）。
    """
    code = str(symbol or "").strip()
    return code.startswith(("92", "8", "4"))


def is_sh(symbol: Any) -> bool:
    """是不是**沪市**（`6` 开头，含科创板；另有 `900` 沪 B 股）。

    口径说明：**科创板算沪市**（688/689 就是沪市的板），所以"排除沪市"会连科创一起排掉；
    想只排科创、留着其它沪市票，就写 `科创=0`（两个标记是独立的，互不蕴含）。
    """
    code = str(symbol or "").strip()
    return code.startswith("6") or code.startswith("900")


def is_sz(symbol: Any) -> bool:
    """是不是**深市**（`0` / `3` 开头，另有 `200` 深 B 股）。**创业板算深市**。"""
    code = str(symbol or "").strip()
    return code.startswith(("0", "3")) or code.startswith("200")


def is_chinext(symbol: Any) -> bool:
    """是不是**创业板**（`300` / `301` 开头）。"""
    return str(symbol or "").strip().startswith(("300", "301"))


def _all_fields() -> dict[str, str]:
    """别名 + 扩展字段（每次现算：扩展字段允许运行时登记）。"""
    out = dict(FIELD_ALIASES)
    for name in EXTRA_FIELDS:
        out.setdefault(name.upper(), name.upper())
    return out


def _field_kind(canonical: str) -> str:
    return EXTRA_FIELDS.get(canonical, FIELD_KINDS.get(canonical, _NUM))


# ══════════════════════════════════════════════════════════════════════════
# Tokenizer
# ══════════════════════════════════════════════════════════════════════════

#: 全角/中文标点 → 半角。为什么必须有：中文输入法下 `（`、`，`、`：`、
#: `“”` 是最常见的"明明看着一样却不通过"，而这跟"用户不会写公式"是两回事 ——
#: 1:1 替换不会改变列号，所以直接在这里归一化，用户永远看不到这类低级报错。
_FULLWIDTH = {
    "（": "(",
    "）": ")",
    "，": ",",
    "：": ":",
    "；": ";",
    "＞": ">",
    "＜": "<",
    "＝": "=",
    "＋": "+",
    "－": "-",   # U+FF0D
    "—": "-",   # U+2014 破折号（用户在中文里打出来的"减号"）
    "＊": "*",
    "／": "/",
    "！": "!",
    "％": "%",
    "．": ".",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
}

#: 双字符运算符（**必须先于单字符匹配**，否则 `>=` 会被拆成 `>` `=`）
_TWO_CHAR_OPS = (">=", "<=", "!=", "&&", "||", "<>")
#: 符号写法 → 规范写法。求值器只认规范名，所以归一化必须在**词法期**做一次，
#: 否则 `&&` 会一路以自身的形式流到解析器（表现为一句莫名其妙的"一行只能写一条语句"）。
_SYMBOL_ALIASES = {"&&": "AND", "||": "OR", "<>": "!="}
#: `<>` 是通达信/易语言的"不等于"，内部只有 `!=` —— 词法期就归一，
#: 解析器与求值器一处都不用改。

#: 通达信的输出样式修饰符（写在表达式后面、用逗号引出）：`MA5:MA(C,5),COLORRED;`
#: 它们只影响**画线外观**，对选股没有任何意义 —— 一律**吃掉、不报错**，
#: 但会记一条 note（`Formula.notes`），界面上告诉用户"样式修饰已被忽略"。
_STYLE_MODS: frozenset[str] = frozenset({
    "COLOR", "COLORRED", "COLORGREEN", "COLORBLUE", "COLORYELLOW", "COLORWHITE",
    "COLORBLACK", "COLORCYAN", "COLORMAGENTA", "COLORGRAY", "COLORLIGRAY",
    "COLORLIRED", "COLORLIGREEN", "COLORLIBLUE",
    "LINETHICK", "LINETHICK1", "LINETHICK2", "LINETHICK3", "LINETHICK4",
    "LINETHICK5", "LINETHICK6", "LINETHICK7",
    "STICK", "VOLSTICK", "COLORSTICK", "LINESTICK", "CROSSDOT", "CIRCLEDOT",
    "POINTDOT", "DOTLINE", "NODRAW", "NOAXIS", "PRECISION", "PRECIS",
    "SHIFT", "LAYER", "MOVE", "VAR", "ALIGN",
})

#: 画图类语句（整句跳过）：它们产出的是图形而不是序列，选股用不到。
#: 为什么"忽略"而不是"报错"：通达信的选股公式里常顺手带一句画线/标记，
#: 报错就等于"网上下来的公式一条都跑不了"；忽略它并在界面上说明，
#: 用户拿到的才是"能用的那部分条件"。
_DRAWING_FUNCS: frozenset[str] = frozenset({
    "DRAWICON", "DRAWTEXT", "DRAWNUMBER", "DRAWLINE", "DRAWKLINE", "DRAWGBK",
    "DRAWBAND", "DRAWNULL", "DRAWRECTREL", "DRAWSL", "DRAWCHANNEL",
    "POLYLINE", "PLOYLINE", "STICKLINE", "VERTLINE", "HORLINE", "PARTLINE",
    "DRAWTEXT_FIX", "DRAWICON_FIX", "DRAWNUMBER_FIX", "DRAWLINE_FIX",
    "SETTEXT", "DRAWBK", "FILLRGN", "RGB",
})
#: 单字符运算符（`=` 也是相等比较；`:=` 在赋值里单独处理）
_ONE_CHAR_OPS = ("+", "-", "*", "/", ">", "<", "=")
#: 比较运算符（"连续比较"拦截用）
_CMP_OPS = ("=", "!=", ">", "<", ">=", "<=")

#: 解出来的 token 种类
#: num 数字 / str 字符串 / ident 名字 / op 运算符 / lparen / rparen / comma / assign(:=) / colon(:)
_IDENT_START_EXTRA = "_"

#: Python 关键字 / 内建名：出现即报错（用户不可能"想写"这些，只会是注入尝试或误贴代码）。
#:
#: ⚠️ 黑名单**不能包含任何合法字段名** —— 最初这里把 `OPEN`（Python 的 `open()`）也列了进去，
#: 结果 `OPEN>PRE_CLOSE` 会被当成"注入"拒绝，而 OPEN 明明是开盘价的合法长名。
#: 加条目时请先和 `FIELD_ALIASES` / `FUNCTIONS` 对一遍。
_FORBIDDEN_WORDS: dict[str, str] = {
    name: "策略只能写赋值与选股条件，不支持导入模块、定义函数/类、循环、异常处理"
    for name in (
        "IMPORT", "FROM", "DEF", "CLASS", "LAMBDA", "RETURN", "YIELD", "GLOBAL",
        "NONLOCAL", "DEL", "ASSERT", "RAISE", "TRY", "EXCEPT", "FINALLY", "WITH",
        "AS", "PASS", "BREAK", "CONTINUE", "WHILE", "FOR", "ELIF", "ELSE",
        "EXEC", "EVAL", "COMPILE", "GETATTR", "SETATTR", "VARS", "DIR",
        "INPUT", "PRINT", "SUBPROCESS", "SYSTEM", "POPEN", "OS", "SYS",
        "BUILTINS", "GLOBALS", "LOCALS", "STATICMETHOD", "PROPERTY", "SUPER",
    )
}
#: 会给出**更具体**提示的名字（比"未知字段"更能说明用户想干什么）
#: **已知、但本地数据上做不到**的通达信函数 → 一句人话（缺什么 + 能换成什么）。
#:
#: 为什么要与"未知函数"分开：这两类对用户意味着完全不同的事 ——
#:   * 未知函数 = "你打错字了"（提示里列可用函数清单就够了）；
#:   * 已知但不支持 = "这条公式的数据前提本地没有"，列一百个可用函数也没用，
#:     要告诉他缺什么、以及手上有什么能替代（否则他会一直以为是自己写错了）。
#: 所以这里的每条都要写清"缺什么数据"、能给替代写法的必须给。
#: 注：`FINANCE` **不在这里** —— 主人 2026-09-21 指定它"等同于流通市值"（见 `_impl_finance`）。
UNSUPPORTED_FUNCTIONS: dict[str, str] = {
    "WINNER": (
        "WINNER 需要筹码分布数据（每个价位的持仓成本），本地没有这份数据。"
        "近似的替代：用换手率与成交密集度自己写条件，例如 `换手率>5 AND V>MA(V,5)*1.5`。"
    ),
    "COST": (
        "COST 需要筹码分布数据（成本分布），本地没有。"
        "要「套牢盘/获利盘」这类判断只能自己用价格与均线近似，例如 `C<PRE*0.95`。"
    ),
}

#: 通达信的周期关键字（`C#WEEK` 这种写法里 `#` 后面那个词，大小写都认）
_TDX_PERIOD_WORDS = (
    "WEEK", "MONTH", "QUARTER", "YEAR", "DAY",
    "MIN1", "MIN5", "MIN15", "MIN30", "MIN60", "MIN",
)


def engine_stamp() -> str:
    """一句话的"引擎自报家门"：`（本程序 v1.1.0，公式引擎支持 70 个函数）`。

    用途只有一个：**用户贴报错原文时，我们能立刻分清"他打错了"还是"他在用旧包"**。
    旧包的可用函数清单是短清单（19 个那种），与这里报的数字一比就清楚，
    不必再来回问他版本。取不到版本号时只报函数个数（绝不让它抛异常 ——
    这句话本身只是给排查用的，不能成为新的报错来源）。
    """
    try:
        from laoa_trader import __version__ as version
    except Exception:  # noqa: BLE001 - 兜底：版本取不到就不写版本
        version = ""
    head = f"（本程序 v{version}，" if version else "（"
    return head + f"策略引擎支持 {len(SUPPORTED_FUNCTIONS)} 个函数）"


def _looks_like_tdx_period(text: str, index: int) -> bool:
    """`text[index]` 是 `#`，后面是不是跟着一个周期关键字（`#WEEK` / `#MIN5`…）。

    为什么要判词边界：本引擎的注释也是 `#` 开头（`# 说明: …`），
    不能把所有 `#` 都当跨周期 —— 只认后面紧跟周期词、且词后不是字母数字的情况。
    """
    rest = text[index + 1: index + 1 + 8].upper()
    for word in _TDX_PERIOD_WORDS:
        if rest.startswith(word):
            tail = rest[len(word): len(word) + 1]
            if tail == "" or not (tail.isalnum() or tail == "_"):
                return True
    return False


def _looks_like_formula_reference(value: str) -> bool:
    """字符串看起来像不像 `"公式名.指标"`（通达信引用别的公式的写法）。

    判据：恰好一个点、两边都是"名字"（字母/数字/下划线/中文），且整体不含空格与引号。
    这样 `"半导体"`（行业名）不会被误判，而 `"MACD.MACD"` / `"我的公式.输出1"` 会被认出来。
    """
    text = str(value or "")
    if text.count(".") != 1 or not text or " " in text:
        return False
    left, right = text.split(".", 1)
    if not left or not right:
        return False

    def ok(part: str) -> bool:
        return all(ch.isalnum() or ch == "_" or "\u4e00" <= ch <= "\u9fff" for ch in part)

    return ok(left) and ok(right)


_SPECIAL_WORDS: dict[str, str] = {
    "TRUE": "策略里没有 True/False，条件请写成比较式（例如 C>O）",
    "FALSE": "策略里没有 True/False，条件请写成比较式（例如 C>O）",
    "NONE": "策略里没有 None，缺值直接用字段本身即可（缺值不会产生信号）",
}


@dataclass(frozen=True, slots=True)
class _Token:
    """词法单元。`col` 是 1 起的列号（用户看到的列），全部按**归一化后**的字符算。"""

    kind: str
    value: str
    line: int
    col: int


def _is_ident_start(ch: str) -> bool:
    # 注意 `ch.isalpha()` 对汉字为 True（Python 的 Unicode 语义），
    # 所以"涨停天数"这类中文函数名天然可用
    return ch.isalpha() or ch in _IDENT_START_EXTRA


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in _IDENT_START_EXTRA


#: 数字字面量：支持 1 / 1.5 / .5 / 1e3（不支持 `1.2.3`，会在第二个点报错）
def _scan_number(text: str, i: int) -> tuple[str, int] | None:
    n = len(text)
    j = i
    seen_dot = False
    if text[j] == ".":
        seen_dot = True
        j += 1
    elif text[j].isdigit():
        while j < n and text[j].isdigit():
            j += 1
    else:
        return None
    if not seen_dot and j < n and text[j] == ".":
        j += 1
    while j < n and text[j].isdigit():
        j += 1
    if not any(c.isdigit() for c in text[i:j]):
        return None
    # 指数部分：`1e3` 要认，否则会被切成 `1` + 标识符 `e3`（错误信息会非常费解）
    if j < n and text[j] in "eE":
        k = j + 1
        if k < n and text[k] in "+-":
            k += 1
        if k < n and text[k].isdigit():
            while k < n and text[k].isdigit():
                k += 1
            j = k
    return text[i:j], j


def _positions(text: str) -> tuple[list[int], list[int]]:
    """预扫每个字符的 (行, 列)。

    为什么预扫：注释里可能有换行（`{...}` 可以跨行），逐段手工维护行列太容易错，
    而**错误信息的行列号就是用户唯一的定位线索**——错一格就指向别的字符。
    """
    lines: list[int] = []
    cols: list[int] = []
    ln, cl = 1, 1
    for ch in text:
        lines.append(ln)
        cols.append(cl)
        if ch == "\n":
            ln += 1
            cl = 1
        else:
            cl += 1
    return lines, cols


def _tokenize(text: str) -> list[_Token]:
    """把公式切成 token 列表（不产生 '换行' token：语句是按行切的，见 `_Parser`）。"""
    line_of, col_of = _positions(text)
    toks: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        raw = text[i]
        ch = _FULLWIDTH.get(raw, raw)

        if ch == "\n" or ch in " \t":
            i += 1
            continue

        # ── 注释 ──
        # `{...}` 是通达信写法；`//` 与 `#` 支持到行尾（`#` 同时被公式文件的注释头使用）
        if ch == "{":
            end = text.find("}", i)
            if end < 0:
                raise FormulaError(
                    "花括号注释 `{` 没有闭合",
                    line=line_of[i], col=col_of[i], code="comment",
                    hint="补一个 `}`；注释也可以整行删掉",
                )
            i = end + 1
            continue
        if ch == "#" and _looks_like_tdx_period(text, i):
            # 通达信的跨周期写法 `C#WEEK` / `C#MONTH`：`#` 在本引擎里是**注释**符号，
            # 若照注释处理，这一行会被整段吃掉 —— 用户看到的是"公式能编译、结果全不对"，
            # 那是最难查的一类问题。所以这里专门拦下来，说清"只按日线算"。
            raise FormulaError(
                "跨周期写法（`#WEEK` / `#MONTH` 这类）本引擎不支持：只按日线计算",
                line=line_of[i], col=col_of[i], code="period",
                hint="把周期条件改写成日线上的等价表达（例如「近 5 日」就写 COUNT(条件,5)）；"
                     "要真正的周线/月线数据得先做多周期引擎",
            )
        if ch == "#" or (ch == "/" and i + 1 < n and text[i + 1] == "/"):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue

        tok_line, tok_col = line_of[i], col_of[i]

        # ── 字符串（单双引号都认）──
        # 引号本身也按归一化后的字符比较：中文输入法打出的 `“ ”` 必须是合法的引号，
        # 否则 `INDUSTRY=“半导体”` 会以"字符串没有闭合"收场（非常费解）。
        # 但**内容原样保留**（不做全角归一）—— 行业名里真出现全角字符时不该被改写。
        if ch in ("'", '"'):
            j = i + 1
            buf: list[str] = []
            while j < n:
                if _FULLWIDTH.get(text[j], text[j]) == ch:
                    break
                if text[j] == "\n":
                    raise FormulaError(
                        "字符串没有闭合", line=tok_line, col=tok_col, code="string",
                    )
                buf.append(text[j])
                j += 1
            if j >= n:
                raise FormulaError(
                    "字符串没有闭合", line=tok_line, col=tok_col, code="string",
                )
            toks.append(_Token("str", "".join(buf), tok_line, tok_col))
            i = j + 1
            continue

        # ── 数字 ──
        if ch.isdigit() or ch == ".":
            scanned = _scan_number(text, i)
            if scanned is not None:
                literal, j = scanned
                toks.append(_Token("num", literal, tok_line, tok_col))
                i = j
                continue
            if ch == ".":
                raise FormulaError(
                    "不支持属性访问（`.`）",
                    line=tok_line, col=tok_col, code="forbidden",
                    hint="策略里没有对象属性；字段请直接写名字（如 CLOSE）",
                )

        # ── 名字（含中文）──
        if _is_ident_start(ch):
            j = i
            while j < n and _is_ident_char(_FULLWIDTH.get(text[j], text[j])):
                j += 1
            toks.append(_Token("ident", text[i:j], tok_line, tok_col))
            i = j
            continue

        # ── `:=` 赋值 ──
        if ch == ":":
            if i + 1 < n and text[i + 1] == "=":
                toks.append(_Token("assign", ":=", tok_line, tok_col))
                i += 2
            else:
                toks.append(_Token("colon", ":", tok_line, tok_col))
                i += 1
            continue

        # ── 双字符运算符 ──
        pair = text[i : i + 2]
        pair_norm = "".join(_FULLWIDTH.get(c, c) for c in pair)
        if pair_norm in _TWO_CHAR_OPS:
            # `&&` → AND、`||` → OR（`!=` 原样保留）：解析期只认规范名
            toks.append(
                _Token("op", _SYMBOL_ALIASES.get(pair_norm, pair_norm), tok_line, tok_col)
            )
            i += 2
            continue

        if ch == "!":
            toks.append(_Token("op", "NOT", tok_line, tok_col))
            i += 1
            continue
        if ch in _ONE_CHAR_OPS:
            toks.append(_Token("op", ch, tok_line, tok_col))
            i += 1
            continue
        if ch == "(":
            toks.append(_Token("lparen", "(", tok_line, tok_col))
            i += 1
            continue
        if ch == ")":
            toks.append(_Token("rparen", ")", tok_line, tok_col))
            i += 1
            continue
        if ch == ",":
            toks.append(_Token("comma", ",", tok_line, tok_col))
            i += 1
            continue
        if ch == "[":
            raise FormulaError(
                "不支持下标访问（`[`）",
                line=tok_line, col=tok_col, code="forbidden",
                hint="取历史值请用 REF(X,N)，例如 REF(C,1) 是昨收",
            )
        if ch == ";":
            raise FormulaError(
                "不认识的字符 `;`",
                line=tok_line, col=tok_col, code="syntax",
                hint="策略按行分隔语句，不需要分号",
            )
        if ch == "}":
            raise FormulaError(
                "多余的 `}`", line=tok_line, col=tok_col, code="comment",
                hint="注释必须写成 `{ ... }`",
            )

        raise FormulaError(
            f"不认识的字符 `{raw}`", line=tok_line, col=tok_col, code="syntax",
            hint="策略只支持 + - * / 比较运算、AND/OR/NOT、括号和已经列出的函数",
        )
    return toks


# ══════════════════════════════════════════════════════════════════════════
# AST 节点（全部是不可变的小对象；白名单求值器只认这几类）
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class _Lit:
    """常量（数字或字符串）。"""

    value: Any
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _FieldRef:
    """字段引用（`C` / `INDUSTRY` ...），name 是**规范名**。"""

    name: str
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _VarRef:
    """`:=` 定义的中间变量引用。"""

    name: str
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _Unary:
    op: str          # "-" | "NOT"
    operand: Any
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _Binary:
    op: str          # + - * / > < >= <= = != AND OR
    left: Any
    right: Any
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _InList:
    """`INDUSTRY IN ("半导体","软件服务")`。"""

    operand: Any
    values: tuple[str, ...]
    line: int
    col: int
    dtype: str = _BOOL


@dataclass(frozen=True, slots=True)
class _Call:
    name: str        # 规范名（大写）
    args: tuple[Any, ...]
    dtype: str
    line: int
    col: int


@dataclass(frozen=True, slots=True)
class _Statement:
    """一条语句：`name` 为 None 表示"裸表达式"（只允许出现在最后一行 = 选股条件）。"""

    name: str | None
    node: Any
    output: bool          # 用 `:` 声明的输出变量（供界面展示用）
    line: int
    col: int


# ══════════════════════════════════════════════════════════════════════════
# 向量化指标实现（纯 numpy，全部"窗口不足 → NaN"）
# ══════════════════════════════════════════════════════════════════════════


def _broadcast(value: Any, n: int) -> np.ndarray:
    """把标量/数组统一成长度 n 的 float64 数组。

    为什么允许标量：`MA(C,5)` 的第一个参数可能是 `5` 这样的常量，
    让 numpy 到处做广播太容易漏；统一在入口处展开，后面的实现只管一维数组。
    """
    arr = np.asarray(value, dtype="float64")
    if arr.ndim == 0:
        return np.full(n, float(arr), dtype="float64")
    if arr.ndim != 1 or arr.shape[0] != n:
        raise FormulaError(
            "序列长度不一致（内部错误：字段长度应等于交易日数）", code="shape",
        )
    return arr


def _roll(x: Any, window: int, reducer: Callable[[np.ndarray], np.ndarray], n: int) -> np.ndarray:
    """通用滚动窗口：**窗口不足 N 根 → NaN**（而不是拿"更短的窗口"凑一个数）。

    为什么坚持 min_periods=N：`MA(C,5)` 在第 3 根 K 线上没有"5 日均线"这回事。
    如果拿 3 根平均糊弄，用户会得到一个看起来正常、实际口径错误的信号 ——
    缺值(False) 只是"今天不选它"，算错却是"今天选错它"。前者可接受，后者不行。
    """
    x = _broadcast(x, n)
    out = np.full(n, np.nan, dtype="float64")
    if window < 1 or n < window:
        return out
    w = sliding_window_view(x, window)
    with np.errstate(all="ignore"):
        out[window - 1 :] = reducer(w)
    return out


def _roll_mean(x: Any, window: int, n: int) -> np.ndarray:
    # 窗口里只要有 NaN，mean 就是 NaN（numpy 的默认语义正好等于"缺值即缺值"）
    return _roll(x, window, lambda w: w.mean(axis=1), n)


def _roll_sum(x: Any, window: int, n: int) -> np.ndarray:
    return _roll(x, window, lambda w: w.sum(axis=1), n)


def _roll_max(x: Any, window: int, n: int) -> np.ndarray:
    return _roll(x, window, lambda w: w.max(axis=1), n)


def _roll_min(x: Any, window: int, n: int) -> np.ndarray:
    return _roll(x, window, lambda w: w.min(axis=1), n)


def _roll_std(x: Any, window: int, n: int) -> np.ndarray:
    # STD 用**样本标准差**（ddof=1），与通达信 STD 一致（总体标准差是 STDP）
    if window < 2:
        return np.full(n, np.nan, dtype="float64")
    return _roll(x, window, lambda w: w.std(axis=1, ddof=1), n)


def _ref(x: Any, offset: int, n: int) -> np.ndarray:
    """REF(X,N) = N 根 K 线之前的 X。落到序列之前的位置是 **NaN**（不是 0，也不是回绕）。"""
    x = _broadcast(x, n)
    if offset == 0:
        return x.copy()
    out = np.full(n, np.nan, dtype="float64")
    if offset < n:
        out[offset:] = x[: n - offset]
    return out


def _ema(x: Any, window: int, n: int) -> np.ndarray:
    """EMA：`EMA[i] = (2*X[i] + (N-1)*EMA[i-1]) / (N+1)`，首值取 X[0]（通达信口径）。

    手写循环而不是 pandas 的 `ewm`：缺值处要"沿用上一根、不更新"，这是本项目的约定
    （缺值不产生信号），而 `ewm` 的 skipna 语义与此不同，且 5000 根的循环开销可以忽略。
    """
    x = _broadcast(x, n)
    out = np.full(n, np.nan, dtype="float64")
    alpha = 2.0 / (window + 1.0)
    prev = np.nan
    for i in range(n):
        xi = x[i]
        if np.isnan(xi):
            out[i] = prev          # 缺值沿用上一根（prev 仍为 NaN 时就是 NaN）
            continue
        prev = xi if np.isnan(prev) else alpha * xi + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _count(cond: Any, window: int, n: int) -> np.ndarray:
    """COUNT(COND,N)：近 N 根里 COND 为真的次数。

    窗口里有**缺值**（不知道当天算不算）时整格返回 NaN，而不是"能数几根数几根" ——
    数少了的结论会变成"不满足条件"，那是**用缺数据下结论**，与全模块的口径冲突。
    """
    flags = _as_cond_float(cond, n)
    known = _roll_sum(np.where(np.isnan(flags), 0.0, 1.0), window, n)
    total = _roll_sum(np.nan_to_num(flags, nan=0.0), window, n)
    with np.errstate(all="ignore"):
        return np.where(known == float(window), total, np.nan)


def _cross(a: Any, b: Any, n: int) -> np.ndarray:
    """CROSS(A,B)：**这一根** A 上穿 B（`A[i]>B[i] 且 A[i-1]<=B[i-1]`）。

    注意"上穿"必须用到前一根：只写成 `A>B` 会连续多日成立（典型 bug：
    用户以为选的是"金叉那天"，实际选的是"金叉之后的每一天"）。
    第一根没有前值、以及任一侧缺值 → NaN（→ 最终为 False），不会产生金叉信号。
    """
    A = _broadcast(a, n)
    B = _broadcast(b, n)
    out = np.full(n, np.nan, dtype="float64")
    if n >= 2:
        missing = np.isnan(A) | np.isnan(B)
        with np.errstate(all="ignore"):
            crossed = (A[1:] > B[1:]) & (A[:-1] <= B[:-1])
        out[1:] = np.where(
            missing[1:] | missing[:-1], np.nan, crossed.astype("float64"),
        )
    return out


def _barslast(cond: Any, n: int) -> np.ndarray:
    """BARSLAST(COND)：距离**上一次** COND 为真过了多少根（当根为真 = 0）；从未为真 = NaN。

    当根条件缺值 → 当根 NaN（"上一次是什么时候"这一刻仍然是不知道的）。
    """
    flags = _as_cond_float(cond, n)
    out = np.full(n, np.nan, dtype="float64")
    last = -1
    for i in range(n):
        current = flags[i]
        if current == 1.0:
            last = i
        if last >= 0 and not np.isnan(current):
            out[i] = i - last
    return out


def _vol_ratio(amount: Any, window: int, n: int) -> np.ndarray:
    """量比 = 当日成交额 / **不含当日**的前 N 日均额（默认 N=5）。

    为什么分母不含当日：含当日会把"当天放量"自己算进基准里，放量越猛分母越大，
    比值被自己的量稀释（当日 3 倍量时 `A/((A+4*avg)/5)` 远小于 3）。
    不足 N+1 根 → NaN → 不产生信号。
    """
    amt = _broadcast(amount, n)
    prev = _ref(amt, 1, n)
    base = _roll_mean(prev, window, n)
    with np.errstate(all="ignore"):
        ratio = amt / base
    return _nanify(ratio)


def _nanify(x: Any) -> Any:
    """把 ±inf 折成 NaN。

    `C/0` 在 IEEE 下是 inf：如果放过去，`C/V > 10` 可能在成交量为 0 的行上
    意外为真（inf > 10）。缺值就该是 NaN，NaN 才不产生信号。
    """
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else float("nan")
    arr = np.asarray(x, dtype="float64")
    return np.where(np.isfinite(arr), arr, np.nan)


def _as_float(value: Any) -> Any:
    """数值化（bool → 1.0/0.0）。返回 float 标量或 float64 数组。"""
    if isinstance(value, (bool, np.bool_)):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    return np.asarray(value, dtype="float64")


def _isnan(x: Any) -> Any:
    if isinstance(x, float):
        return math.isnan(x)
    return np.isnan(x)


def _as_cond_array(value: Any, n: int) -> np.ndarray:
    """最终折算：条件值 → 长度 n 的 bool 数组。**缺值一律 False**。

    这是"0/1/NaN 内部表示"的最后一道关口（见模块头的说明）：
    比较、CROSS 等返回的是 0/1 浮点 + NaN，这里才折成布尔给调用方。
    """
    if isinstance(value, str):
        raise FormulaError("条件里不能直接用字符串（请写成比较式）", code="type")
    arr = np.asarray(value)
    if arr.dtype == bool:
        if arr.ndim == 0:
            return np.full(n, bool(arr), dtype=bool)
        return arr
    if arr.dtype.kind in ("U", "S", "O"):
        raise FormulaError("条件里不能直接用字符串（请写成比较式）", code="type")
    floats = np.asarray(arr, dtype="float64")
    with np.errstate(all="ignore"):
        return np.where(np.isnan(floats), False, floats != 0)


def _as_cond_float(value: Any, n: int) -> np.ndarray:
    """条件值 → 长度 n 的 float64 序列，取值只有 0.0 / 1.0 / NaN（未知）。

    归一化到 0/1 是为了让 AND/OR/NOT 的实现是"数值运算 + 缺值掩码"这么简单的东西，
    而不是又一套三值逻辑的 if/else。
    """
    if isinstance(value, (bool, np.bool_)):
        return np.full(n, 1.0 if value else 0.0, dtype="float64")
    if isinstance(value, str):
        raise FormulaError("条件里不能直接用字符串（请写成比较式）", code="type")
    arr = np.asarray(value)
    if arr.dtype.kind in ("U", "S", "O"):
        raise FormulaError("条件里不能直接用字符串（请写成比较式）", code="type")
    floats = np.asarray(arr, dtype="float64")
    if floats.ndim == 0:
        return np.full(n, 0.0 if np.isnan(floats) else float(floats != 0), dtype="float64")
    with np.errstate(all="ignore"):
        return np.where(np.isnan(floats), np.nan, (floats != 0).astype("float64"))


def _logic_combine(op: str, a: Any, b: Any, n: int) -> np.ndarray:
    """AND / OR。**任一侧缺值 → 结果缺值**（悲观口径）。

    为什么"悲观"而不是 Kleene 三值逻辑的 `True OR 未知 = True`：
    选股公式的输出是"买不买"，让缺值一路保持"不知道"（最终 False）永远不会造出
    假信号；而 `True OR 未知 = True` 会让"数据不足"变成一条真的选股信号。
    宁可漏选，不可错选。
    """
    A = _as_cond_float(a, n)
    B = _as_cond_float(b, n)
    missing = np.isnan(A) | np.isnan(B)
    with np.errstate(all="ignore"):
        result = np.minimum(A, B) if op == "AND" else np.maximum(A, B)
    return np.where(missing, np.nan, result)


def _not(a: Any, n: int) -> np.ndarray:
    """NOT：缺值的**否定仍然是缺值**（否则 `NOT (C>MA(C,5))` 会在数据不足的前几根上全为真）。"""
    A = _as_cond_float(a, n)
    with np.errstate(all="ignore"):
        return np.where(np.isnan(A), np.nan, 1.0 - A)


def _num_cmp(op: str, a: Any, b: Any) -> Any:
    """数值比较 → 0.0 / 1.0 / NaN。**缺值给 NaN**（不是 False，见模块头）。

    `NaN != 5` 在 IEEE 语义下是 True，如果不显式抹掉，停牌日会满足 `C!=5`
    从而变成一条选股信号。所以这里统一：任一侧缺值 → NaN（既不算相等也不算不等）。
    """
    A = _as_float(a)
    B = _as_float(b)
    with np.errstate(all="ignore"):
        if op == ">":
            result = np.greater(A, B)
        elif op == "<":
            result = np.less(A, B)
        elif op == ">=":
            result = np.greater_equal(A, B)
        elif op == "<=":
            result = np.less_equal(A, B)
        elif op == "=":
            result = np.equal(A, B)
        else:  # !=
            result = np.not_equal(A, B)
        missing = np.logical_or(_isnan(A), _isnan(B))
        float_result = np.asarray(result, dtype="float64")
        return np.where(missing, np.nan, float_result)


def _as_object_array(value: Any, n: int) -> np.ndarray:
    if isinstance(value, str):
        return np.full(n, value, dtype=object)
    arr = np.asarray(value, dtype=object)
    if arr.ndim == 0:
        return np.full(n, arr.item(), dtype=object)
    return arr


def _str_cmp(op: str, a: Any, b: Any, n: int) -> np.ndarray:
    """字符串比较（只支持 `=` / `!=`；解析期已经拦下其它运算符）→ 0.0 / 1.0。"""
    A = _as_object_array(a, n)
    B = _as_object_array(b, n)
    result = np.equal(A, B) if op == "=" else np.not_equal(A, B)
    result = np.asarray(result, dtype=bool)
    if result.ndim == 0:
        return np.full(n, float(result), dtype="float64")
    return result.astype("float64")


# ══════════════════════════════════════════════════════════════════════════
# 函数白名单
# ══════════════════════════════════════════════════════════════════════════

#: 参数类型代码：num 数值 / cond 条件（数值也接受，非 0 即真）/ window 正整数常数 /
#: offset 非负整数常数（REF 允许 0）
_W = "window"
_OFF = "offset"
#: `any` = **不检查类型**。给"参数随便写都收"的兼容函数用（例如 `INBLOCK('板块')`：
#: 通达信那边要字符串，我们只按热门行业处理、参数根本不参与计算）——
#: 有了它，用户从网上抄来的写法（字符串 / 数字 / 不传）都能直接跑，不会卡在类型错误上。
_ANY = "any"


@dataclass(frozen=True, slots=True)
class _FuncSpec:
    """一个函数的静态签名 + 求值实现。"""

    min_args: int
    max_args: int
    result: str                        # num / bool / same（与第 2 个分支同类型，仅 IF）
    arg_kinds: tuple[str, ...]
    impl: Callable[..., Any]
    #: 窗口参数在参数里的位置（None = 这个函数不吃窗口）；用于估算最少需要多少根 K 线
    hist_arg: int | None = None
    #: 在窗口 N 之上还要多几根（REF(X,N) 需要 N+1 根）
    hist_extra: int = 0
    #: 不写参数时的隐含根数（`量比()` 内部固定看前 5 日，所以至少要 6 根）
    hist_default: int = 0
    #: 这个函数**内部用到**的字段（登记进 `Formula.fields`）。
    #: 为什么需要它：`preview_hits()` / 建池那条路是按 `formula.fields` 判断
    #: "要不要去取那一趟实时快照"的 —— 函数里偷偷用了字段却不登记，
    #: 表现就是"公式里明明写了市值条件，却永远取不到数、一只都不出"。
    uses_fields: tuple[str, ...] = ()


def _impl_ma(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _roll_mean(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_ema(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _ema(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_dif(ev: _Evaluator, node: _Call, vals: list) -> Any:  # noqa: ARG001
    """`DIF()` = `EMA(C,12) - EMA(C,26)`（MACD 的快线）。"""
    return _ema(ev.series.close, 12, ev.n) - _ema(ev.series.close, 26, ev.n)


def _impl_dea(ev: _Evaluator, node: _Call, vals: list) -> Any:  # noqa: ARG001
    """`DEA()` = `EMA(DIF,9)`（MACD 的慢线/信号线）。"""
    return _ema(_impl_dif(ev, node, vals), 9, ev.n)


def _impl_macd(ev: _Evaluator, node: _Call, vals: list) -> Any:  # noqa: ARG001
    """`MACD()` = `(DIF - DEA) * 2`（柱状线；正=红柱、负=绿柱，通达信口径）。"""
    return (_impl_dif(ev, node, vals) - _impl_dea(ev, node, vals)) * 2.0


def _impl_ref(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _ref(vals[0], ev.win(vals[1], node.args[1], minimum=0, code="offset"), ev.n)


def _impl_hhv(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _roll_max(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_llv(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _roll_min(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_sum(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _roll_sum(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_std(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _roll_std(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_count(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _count(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_cross(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _cross(vals[0], vals[1], ev.n)


def _impl_abs(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return np.abs(_as_float(vals[0]))


def _impl_max(ev: _Evaluator, node: _Call, vals: list) -> Any:
    # numpy 的 maximum/minimum 遇 NaN 传播 NaN（与"缺值即缺值"的全局约定一致）
    return np.maximum(_as_float(vals[0]), _as_float(vals[1]))


def _impl_min(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return np.minimum(_as_float(vals[0]), _as_float(vals[1]))


def _impl_if(ev: _Evaluator, node: _Call, vals: list) -> Any:
    """IF(COND,A,B)：条件缺值 → 结果也缺值（不偷偷取 B，那会把"不知道"变成结论）。"""
    cond = _as_cond_float(vals[0], ev.n)
    chosen = np.where(cond == 1.0, _as_float(vals[1]), _as_float(vals[2]))
    return np.where(np.isnan(cond), np.nan, chosen)


def _impl_barslast(ev: _Evaluator, node: _Call, vals: list) -> Any:
    return _barslast(vals[0], ev.n)


def _impl_limit_up_days(ev: _Evaluator, node: _Call, vals: list) -> Any:
    """涨停天数()：每根 K 线"是否涨停"（0/1，来自本地 limit_up_pool）。

    带参数 `涨停天数(N)` = 近 N 日涨停次数（等价 `COUNT(涨停天数()>0,N)`，但更省事）。
    两种形式都要：用户口语里"涨停天数"既可能指"今天是不是涨停"，也可能指"近10天几次"。
    """
    flags = ev.series.limit_up_days
    if not vals:
        return flags.copy()
    return _roll_sum(flags, ev.win(vals[0], node.args[0]), ev.n)


def _impl_limit_up_cnt(ev: _Evaluator, node: _Call, vals: list) -> Any:  # noqa: ARG001
    """连板()：当日连板天数（0 = 非涨停），来自 limit_up_pool.high_days。"""
    return ev.series.limit_up_cnt.copy()


#: `量比()` 的默认窗口（当日成交额 ÷ 前 N 日均额）。**只此一处定义**。
#: 为什么抽出来：`DYNAINFO` 要"等同量比"，两边必须以同一个窗口算 ——
#: 抽之前 DYNAINFO 误用了 spec 的 `hist_default=6`（那是"窗口 5 再多一根"的意思），
#: 结果两边算出来的量比不一样，而界面上完全看不出来。
VOL_RATIO_DEFAULT_WINDOW = 5


def _impl_vol_ratio(ev: _Evaluator, node: _Call, vals: list) -> Any:
    """量比()：当日成交额 ÷ 前 5 日均额（本项目自定义口径，见 `_vol_ratio`）。"""
    window = ev.win(vals[0], node.args[0]) if vals else VOL_RATIO_DEFAULT_WINDOW
    return _vol_ratio(ev.series.amount, window, ev.n)


#: 函数白名单：**只有这里的名字能被调用**（解析期查表，表外一律报错）
# ══════════════════════════════════════════════════════════════════════════
# 通达信（TDX）兼容层：补齐选股公式里高频出现的函数
#
# 为什么要有这一块：主人要求"通达信公式进来直接能跑"。下面这些函数在 TDX 的选股
# 公式里出现率极高（KDJ/RSI/BOLL、EXIST/EVERY/BETWEEN、SMA/DMA……），
# 缺一个就整条公式报错。实现口径**照 TDX 官方定义**，关键几处写在各自的注释里：
#   * `SMA(X,N,M)` 是**加权递推**，与 `MA` 不是一回事（这是最常见的误用）；
#   * 滚动窗口一律"窗口不足 → NaN"（与本模块一贯口径一致，缺值不产生信号）；
#   * 有状态函数（SMA/DMA/FILTER/VALUEWHEN/SUMBARS/OBV）用手写循环，
#     因为它们的定义本身就是逐根递推的，用向量化改写反而容易算错。
# ══════════════════════════════════════════════════════════════════════════


def _cond_window(cond: Any, window: int, n: int, mode: str) -> np.ndarray:
    """条件型窗口统计（EXIST / EVERY / UPNDAY / NDAY 共用）。

    `mode="any"` = 窗口内出现过；`"all"` = 窗口内全部成立。
    窗口里有**缺值**（不知道那天算不算）时整格 NaN —— 与本模块 `COUNT` 同一口径：
    缺数据不能下结论。
    """
    flags = _as_cond_float(cond, n)
    known = _roll_sum(np.where(np.isnan(flags), 0.0, 1.0), window, n)
    total = _roll_sum(np.nan_to_num(flags, nan=0.0), window, n)
    with np.errstate(all="ignore"):
        full = known == float(window)
        val = (total > 0.0) if mode == "any" else (total == float(window))
        return np.where(full, val.astype("float64"), np.nan)


def _impl_exist(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    return _cond_window(vals[0], ev.win(vals[1], node.args[1]), ev.n, "any")


def _impl_every(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    return _cond_window(vals[0], ev.win(vals[1], node.args[1]), ev.n, "all")


def _impl_between(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """BETWEEN(X,A,B)：X 在 A、B 之间（**谁大谁小都认**，照 TDX 的"介于两者之间"）。"""
    x, a, b = (_as_float(v) for v in vals[:3])
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    with np.errstate(all="ignore"):
        ok = (x >= lo) & (x <= hi)
    return np.where(np.isnan(x) | np.isnan(lo) | np.isnan(hi), np.nan, ok.astype("float64"))


def _impl_upnday(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """UPNDAY(X,N)：X 连续 N 根上涨（逐根比上一根高）。"""
    x = _broadcast(vals[0], ev.n)
    up = x > _ref(x, 1, ev.n)
    return _cond_window(up, ev.win(vals[1], node.args[1]), ev.n, "all")


def _impl_downnday(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    x = _broadcast(vals[0], ev.n)
    down = x < _ref(x, 1, ev.n)
    return _cond_window(down, ev.win(vals[1], node.args[1]), ev.n, "all")


def _impl_nday(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """NDAY(X,Y,N)：X 连续 N 根大于 Y。"""
    ge = _as_float(vals[0]) > _as_float(vals[1])
    return _cond_window(ge, ev.win(vals[2], node.args[2]), ev.n, "all")


def _impl_filter(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """FILTER(COND,N)：COND 成立后，**紧接着的 N-1 根不再输出**（信号去重）。

    逐根递推实现（TDX 的定义就是这个语义）；缺值当"不成立"处理。
    """
    flags = _as_cond_float(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    mute_until = -1
    for i in range(ev.n):
        f = flags[i]
        if np.isnan(f):
            out[i] = np.nan
            continue
        out[i] = 0.0
        if i <= mute_until:
            continue
        if f == 1.0:
            out[i] = 1.0
            mute_until = i + window - 1
    return out


def _impl_bars_since(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """BARSSINCE(COND)：COND 首次成立到现在的根数（之前是 NaN）。"""
    flags = _as_cond_float(vals[0], ev.n)
    out = np.full(ev.n, np.nan, dtype="float64")
    first = -1
    for i in range(ev.n):
        if first < 0 and flags[i] == 1.0:
            first = i
        if first >= 0:
            out[i] = float(i - first)
    return out


def _impl_barscount(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """BARSCOUNT(X)：到当前为止**有效**（非缺值）的根数。"""
    x = _broadcast(vals[0], ev.n)
    valid = ~np.isnan(x)
    return np.cumsum(valid).astype("float64")


def _impl_longcross(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """LONGCROSS(A,B,N)：前 N 根 A 都小于 B，本根 A 上穿 B（"长期压制后的金叉"）。"""
    a, b = _as_float(vals[0]), _as_float(vals[1])
    n_back = ev.win(vals[2], node.args[2])
    below = a < b
    kept = _cond_window(below, n_back, ev.n, "all")
    crossed = _cross(a, b, ev.n)
    with np.errstate(all="ignore"):
        return np.where(np.isnan(kept) | np.isnan(crossed), np.nan,
                        ((kept == 1.0) & (crossed == 1.0)).astype("float64"))


def _impl_valuewhen(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """VALUEWHEN(COND,X)：取**最近一次** COND 成立那根的 X（往后一直沿用）。

    用途很典型：`VALUEWHEN(CROSS(MA(C,5),MA(C,10)),C)` = "上次金叉时的价格"。
    在第一次成立之前是 NaN（那时候这个值还不存在，不能拿当前值凑）。
    """
    flags = _as_cond_float(vals[0], ev.n)
    x = _as_float(vals[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    last = np.nan
    for i in range(ev.n):
        if flags[i] == 1.0 and not np.isnan(x[i]):
            last = x[i]
        out[i] = last
    return out


def _impl_last(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """LAST(COND,A,B)：从 A 根前到 B 根前，COND 一直成立（A>B，B 常写 0）。"""
    flags = _as_cond_float(vals[0], ev.n)
    a = ev.win(vals[1], node.args[1], minimum=0, code="window")
    b = ev.win(vals[2], node.args[2], minimum=0, code="window")
    lo, hi = min(a, b), max(a, b)
    out = np.full(ev.n, np.nan, dtype="float64")
    for i in range(ev.n):
        if i - hi < 0:
            continue
        seg = flags[i - hi: i - lo + 1]
        if np.any(np.isnan(seg)):
            out[i] = np.nan
        else:
            out[i] = 1.0 if np.all(seg == 1.0) else 0.0
    return out


def _impl_hhvbars(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """HHVBARS(X,N)：近 N 根里最高值出现在**多少根之前**（0 = 就是本根）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    if window < 1 or ev.n < window:
        return out
    for i in range(window - 1, ev.n):
        seg = x[i - window + 1: i + 1]
        if np.all(np.isnan(seg)):
            continue
        out[i] = float(window - 1 - int(np.nanargmax(seg)))
    return out


def _impl_llvbars(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    if window < 1 or ev.n < window:
        return out
    for i in range(window - 1, ev.n):
        seg = x[i - window + 1: i + 1]
        if np.all(np.isnan(seg)):
            continue
        out[i] = float(window - 1 - int(np.nanargmin(seg)))
    return out


def _impl_sumbars(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """SUMBARS(X,N)：向前累加 X，达到 N 需要多少根（含当前根）。

    典型用法：`SUMBARS(V, MA(V,20)*5)` = "几天才凑够 5 日均量"。
    向后最多找 250 根（再远没意义，也避免长循环）；找不到就 NaN。
    """
    x = _broadcast(vals[0], ev.n)
    target = _as_float(vals[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    for i in range(ev.n):
        need = target[i]
        if np.isnan(need):
            continue
        acc = 0.0
        for j in range(i, max(-1, i - 250), -1):
            v = x[j]
            if np.isnan(v):
                break
            acc += v
            if acc >= need:
                out[i] = float(i - j)
                break
    return out


def _sma_tdx(x: Any, window: int, weight: int, n: int) -> np.ndarray:
    """TDX 的 `SMA(X,N,M)`：`Y = (M*X + (N-M)*Y_prev) / N`，首值 = 第一根有效 X。

    **它与 `MA`（简单平均）不是一回事**，这是粘通达信公式时最容易算错的一处：
    `SMA(C,3,1)` 是"权重 1/3 的指数式平滑"，不是"3 日均价"。
    """
    arr = _broadcast(x, n)
    m = float(weight)
    nn = float(window)
    out = np.full(n, np.nan, dtype="float64")
    prev = np.nan
    for i in range(n):
        xi = arr[i]
        if np.isnan(xi):
            out[i] = prev
            continue
        prev = xi if np.isnan(prev) else (m * xi + (nn - m) * prev) / nn
        out[i] = prev
    return out


def _impl_sma(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    # 第三个参数是权重 M（通达信里总是字面量），用 `ev.win` 取整数：它同时管住
    # "必须是正整数常量"这条检查，省得自己再写一遍（M 大于 N 属于写法错误，也一并报出来）。
    weight = ev.win(vals[2], node.args[2], minimum=1, code="weight")
    return _sma_tdx(vals[0], ev.win(vals[1], node.args[1]), weight, ev.n)


def _impl_dma(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """DMA(X,A)：动态移动平均 `Y = A*X + (1-A)*Y_prev`（A 可以是序列）。"""
    x, a = _as_float(vals[0]), _as_float(vals[1])
    out = np.full(ev.n, np.nan, dtype="float64")
    prev = np.nan
    for i in range(ev.n):
        if np.isnan(x[i]) or np.isnan(a[i]):
            out[i] = prev
            continue
        prev = x[i] if np.isnan(prev) else a[i] * x[i] + (1.0 - a[i]) * prev
        out[i] = prev
    return out


def _impl_wma(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """WMA(X,N)：加权平均，越近权重越大（权重 N、N-1、…、1）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    w = np.arange(1, window + 1, dtype="float64")
    total_w = float(w.sum())

    def reducer(win: np.ndarray) -> np.ndarray:
        return (win * w).sum(axis=1) / total_w

    return _roll(x, window, reducer, ev.n)


def _impl_var(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """VAR(X,N)：样本方差（除以 N-1，照 TDX 定义）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    if window < 2:
        return np.full(ev.n, np.nan, dtype="float64")
    return _roll(x, window, lambda w: np.nanvar(w, axis=1, ddof=1), ev.n)


def _impl_stdp(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """STDP(X,N)：总体标准差（除以 N）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    return _roll(x, window, lambda w: np.nanstd(w, axis=1, ddof=0), ev.n)


def _impl_avedev(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """AVEDEV(X,N)：平均绝对偏差（CCI 的分子要用它）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])

    def reducer(w: np.ndarray) -> np.ndarray:
        mean = np.nanmean(w, axis=1, keepdims=True)
        return np.nanmean(np.abs(w - mean), axis=1)

    return _roll(x, window, reducer, ev.n)


def _impl_slope(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """SLOPE(X,N)：N 根线性回归的斜率（每根 K 线变化多少）。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    t = np.arange(window, dtype="float64")
    t_mean = t.mean()
    denom = float(((t - t_mean) ** 2).sum())

    def reducer(w: np.ndarray) -> np.ndarray:
        return ((w - np.nanmean(w, axis=1, keepdims=True)) * (t - t_mean)).sum(axis=1) / denom

    return _roll(x, window, reducer, ev.n)


def _impl_forcast(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """FORCAST(X,N)：线性回归在**本根**的预测值（回归线延长到今天）。"""
    slope = _impl_slope(ev, node, vals)
    mean = _roll(vals[0], ev.win(vals[1], node.args[1]), lambda w: np.nanmean(w, axis=1), ev.n)
    window = ev.win(vals[1], node.args[1])
    # 回归线：y = mean + slope * (t - t_mean)，本根的 t = N-1，t_mean = (N-1)/2
    return mean + slope * ((window - 1) - (window - 1) / 2.0)


def _impl_rsi(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """RSI(X,N)：TDX 口径 `SMA(MAX(X-REF(X,1),0),N,1) / SMA(ABS(X-REF(X,1)),N,1) * 100`。"""
    x = _broadcast(vals[0], ev.n)
    window = ev.win(vals[1], node.args[1])
    diff = x - _ref(x, 1, ev.n)
    up = np.where(np.isnan(diff), np.nan, np.maximum(diff, 0.0))
    absd = np.abs(diff)
    num = _sma_tdx(up, window, 1, ev.n)
    den = _sma_tdx(absd, window, 1, ev.n)
    with np.errstate(all="ignore"):
        return np.where((den == 0.0) | np.isnan(den), np.nan, num / den * 100.0)


def _impl_boll(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """BOLL(X,N)：中轨 = MA(X,N)（通达信里上下轨是另两条线 UB/LB）。"""
    return _roll_mean(vals[0], ev.win(vals[1], node.args[1]), ev.n)


def _impl_ub(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """UB(X,N,P)：上轨 = 中轨 + P × 标准差。"""
    window = ev.win(vals[1], node.args[1])
    mult = float(_as_float(vals[2])[0]) if np.ndim(vals[2]) else float(vals[2])
    mid = _roll_mean(vals[0], window, ev.n)
    std = _roll(vals[0], window, lambda w: np.nanstd(w, axis=1, ddof=0), ev.n)
    return mid + mult * std


def _impl_lb(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    window = ev.win(vals[1], node.args[1])
    mult = float(_as_float(vals[2])[0]) if np.ndim(vals[2]) else float(vals[2])
    mid = _roll_mean(vals[0], window, ev.n)
    std = _roll(vals[0], window, lambda w: np.nanstd(w, axis=1, ddof=0), ev.n)
    return mid - mult * std


def _impl_rsv(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """RSV()：`(C-LLV(L,9))/(HHV(H,9)-LLV(L,9))*100`（KDJ 的第一步，默认 9 日）。"""
    high, low = ev.series.high, ev.series.low
    hh, ll = _roll_max(high, 9, ev.n), _roll_min(low, 9, ev.n)
    with np.errstate(all="ignore"):
        span = hh - ll
        return np.where(span == 0.0, np.nan, (ev.series.close - ll) / span * 100.0)


def _impl_k(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """K()：`SMA(RSV,3,1)`。"""
    return _sma_tdx(_impl_rsv(ev, node, vals), 3, 1, ev.n)


def _impl_d(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """D()：`SMA(K,3,1)`。"""
    return _sma_tdx(_impl_k(ev, node, vals), 3, 1, ev.n)


def _impl_j(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """J()：`3*K - 2*D`。"""
    return 3.0 * _impl_k(ev, node, vals) - 2.0 * _impl_d(ev, node, vals)


def _impl_obv(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """OBV()：能量潮（收盘涨就加成交量、跌就减），首根从 0 开始。"""
    close, vol = ev.series.close, ev.series.vol
    out = np.full(ev.n, np.nan, dtype="float64")
    acc = 0.0
    for i in range(ev.n):
        if i == 0:
            out[i] = 0.0
            continue
        if np.isnan(close[i]) or np.isnan(close[i - 1]) or np.isnan(vol[i]):
            out[i] = acc
            continue
        if close[i] > close[i - 1]:
            acc += vol[i]
        elif close[i] < close[i - 1]:
            acc -= vol[i]
        out[i] = acc
    return out


def _impl_wr(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """WR(N)：威廉指标 `100*(HHV(H,N)-C)/(HHV(H,N)-LLV(L,N))`（数值越小越强）。"""
    window = ev.win(vals[0], node.args[0]) if vals else 9
    high, low = ev.series.high, ev.series.low
    hh, ll = _roll_max(high, window, ev.n), _roll_min(low, window, ev.n)
    with np.errstate(all="ignore"):
        span = hh - ll
        return np.where(span == 0.0, np.nan, (hh - ev.series.close) / span * 100.0)


def _impl_cci(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    """CCI(N)：`(TP-MA(TP,N)) / (0.015*AVEDEV(TP,N))`，TP=(H+L+C)/3。"""
    window = ev.win(vals[0], node.args[0]) if vals else 14
    tp = (ev.series.high + ev.series.low + ev.series.close) / 3.0
    mean = _roll_mean(tp, window, ev.n)

    def reducer(w: np.ndarray) -> np.ndarray:
        m = np.nanmean(w, axis=1, keepdims=True)
        return np.nanmean(np.abs(w - m), axis=1)

    dev = _roll(tp, window, reducer, ev.n)
    with np.errstate(all="ignore"):
        return np.where(dev == 0.0, np.nan, (tp - mean) / (0.015 * dev))


def _num1(fn: Callable[[np.ndarray], np.ndarray]) -> Callable[..., Any]:
    """单参数数学函数的包装（POW/SQRT/LOG…）：保持缺值为缺值。"""

    def impl(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
        with np.errstate(all="ignore"):
            return fn(_as_float(vals[0]))

    return impl


def _num2(fn: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> Callable[..., Any]:
    def impl(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
        with np.errstate(all="ignore"):
            return fn(_as_float(vals[0]), _as_float(vals[1]))

    return impl


def _impl_codelike(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """`CODELIKE('300')`：代码是不是以该字符串开头（通达信语义），返回 0/1。

    注意这是**逐票**判定的：一条公式跑的是某一只票的整段序列，而代码不会变，
    所以整列是同一个值（常量数组）。判据用字符串前缀 —— 与通达信一致
    （它也是前缀匹配，不是板块判定）。
    """
    prefix = str(vals[0] if vals else "").strip()
    code = str(getattr(ev.series, "symbol", "") or "").strip()
    hit = bool(prefix) and code.startswith(prefix)
    return np.full(ev.n, 1.0 if hit else 0.0)


def _impl_nameinclude(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """`NAMEINCLUDE('ST')`：名称里是否**包含**该字符串（通达信语义），返回 0/1。

    同样逐票恒定；比较时两边都转大写（`st` 这种小写写法也认，中文不受影响）。
    """
    keyword = str(vals[0] if vals else "").strip()
    name = str(getattr(ev.series, "name", "") or "").strip()
    hit = bool(keyword) and keyword.upper() in name.upper()
    return np.full(ev.n, 1.0 if hit else 0.0)


def _impl_inblock(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """`INBLOCK('板块名')` → 等同本项目的 `热门行业`（最近 3 个交易日上榜次数，0~3）。

    **口径由主人 2026-09-21 指定**（原话是"这个也按热门行业写进去"）。

    与通达信的差别（必须让用户看到，界面上有一条非阻断提醒）：
    通达信的 `INBLOCK` 是"这只票**属不属于**某个板块"（0/1，而且板块名要真匹配）；
    我们手上只有"行业最近上过几次热门榜"这个**次数**，也不做板块名匹配 ——
    所以**参数一律忽略**，值就是 `热门行业`。

    为什么这样也够用：网上那类公式几乎都写成 `INBLOCK('xxx')>0`（"属于就选中"），
    而次数 >0 正好就是"上过热门榜"，语义天然对得上。
    """
    extra = getattr(ev.series, "extra", None) or {}
    value = extra.get("热门行业")
    if value is None:
        # 没算过热门榜（或库里缺涨停/行业数据）：整列缺值 ⇒ 条件不成立、不产生信号，
        # 与直接用 `热门行业` 写条件时的行为完全一致。
        return np.full(ev.n, np.nan)
    return _as_float(value)


def _impl_dynainfo(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """`DYNAINFO(...)` → 等同本项目的 `量比()`（倍）。

    **口径由主人 2026-09-21 指定**（原话是"也按等同写进去"）：
    通达信里 `DYNAINFO(17)` 就是量比、`DYNAINFO(3)` 是昨收之类的盘中动态行情字段，
    而本地只有收盘后的日线，没有那套盘中快照 —— 但写这类公式的人要的基本就是量比，
    所以直接给量比的值，比报"没有动态行情数据"有用。

    ⚠️ 与 `FINANCE` 同一套处理原则：
    - 参数怎么写都收下（数字/表达式/不传），一律返回同一个值 —— 不让它成为"跑不起来"的原因；
    - 口径差异由界面上的**非阻断提醒**（`formulas.tdx_compat_notes()`）说清；
    - 文档 `docs/通达信兼容性.md` 归到"支持（口径按本项目定义）"。
    """
    # **参数一律忽略**：通达信里那个参数是"取哪个动态字段"（17 = 量比、3 = 昨收…），
    # 而我们把这些字段统一映射到量比，所以写成什么数、写不写，结果都一样 ——
    # 这正是主人要的"参数随便写、都返回量比的值"。窗口固定 5 日，与 `量比()` 无参时同一口径。
    return _vol_ratio(ev.series.amount, VOL_RATIO_DEFAULT_WINDOW, ev.n)


def _impl_finance(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """`FINANCE(...)` → 等同本项目的 `流通市值`（单位：亿元）。

    **这是主人 2026-09-21 明确指定的口径**：「你直接在程序后台把这个函数等同于流通市值就行了啊。」
    原因：通达信里 `FINANCE(7)` 是流通股本、`FINANCE(40)` 是总股本之类的财报项，
    而本地**根本没有财务数据**；但用户写这类公式的**真实意图几乎都是"按市值筛"**，
    所以直接给一个能用的值，比报"本地没有财务数据"有用得多。

    ⚠️ 口径与通达信**不同**（股本 ≠ 市值，单位也从股变成亿元），所以：
    - 参数怎么写都收下（数字/表达式/不传），一律返回同一个值 —— 不让它成为"跑不起来"的原因；
    - 界面上有一条**非阻断提醒**（`formulas.tdx_compat_notes()`），说明这个差异；
    - 文档 `docs/通达信兼容性.md` 把它归到"支持（口径按本项目定义）"那一档。
    """
    extra = getattr(ev.series, "extra", None) or {}
    value = extra.get("流通市值")
    if value is None:
        # 没配 Key / 非交易时段取不到快照：整列缺值（条件不成立、不产生信号），
        # 与直接用 `流通市值` 写条件时的行为完全一致。
        return np.full(ev.n, np.nan)
    return _as_float(value)


def _limit_price(ev: "_Evaluator", vals: list, *, up: bool) -> Any:
    """`ZTPRICE` / `DTPRICE` 的共用实现（通达信内置）。

    口径（**与日更/池子用的是同一套规则**，见 `laoa_trader/price_limits.py`）：
    价格 = 前收 × (1 ± 比例)，再按**这只票所属板块**取整到分 ——
    北交所涨停向下截断、跌停向上取整，其余板块四舍五入。

    为什么比例默认 0.1：通达信里不写第二个参数时按主板 10% 算，写公式的人
    （尤其老公式）经常省掉它；给个 0.1 比报错更接近他们预期。
    比例是**小数**（0.1 = 10%），不是百分数 —— 与通达信一致。
    """
    from laoa_trader import price_limits as pl

    prev_close = _as_float(vals[0])
    ratio = 0.1 if len(vals) < 2 else vals[1]
    # 比例允许是表达式（TDX 里也常见 `ZTPRICE(REF(C,1), 涨跌幅/100)`）
    ratio_arr = _as_float(ratio) if not isinstance(ratio, (int, float)) else float(ratio)
    return pl.limit_price_ratio(
        prev_close, ratio_arr, str(getattr(ev.series, "symbol", "") or ""), up=up
    )


def _impl_ztprice(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    return _limit_price(ev, vals, up=True)


def _impl_dtprice(ev: "_Evaluator", node: "_Call", vals: list) -> Any:
    return _limit_price(ev, vals, up=False)


def _impl_round(ev: "_Evaluator", node: "_Call", vals: list) -> Any:  # noqa: ARG001
    """ROUND(X)：四舍五入到整数（TDX 的口径；要小数请用 `PRECISION` 之类画线属性，我们不支持）。"""
    return np.round(_as_float(vals[0]))


_impl_pow = _num2(np.power)
_impl_sqrt = _num1(np.sqrt)
_impl_log = _num1(np.log10)
_impl_ln = _num1(np.log)
_impl_exp = _num1(np.exp)
_impl_abs2 = _num1(np.abs)
_impl_sign = _num1(np.sign)
_impl_intpart = _num1(np.trunc)
_impl_ceiling = _num1(np.ceil)
_impl_floor = _num1(np.floor)
_impl_mod = _num2(np.mod)


FUNCTIONS: dict[str, _FuncSpec] = {
    "MA": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_ma, hist_arg=1),
    "EMA": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_ema, hist_arg=1),
    "REF": _FuncSpec(2, 2, _NUM, (_NUM, _OFF), _impl_ref, hist_arg=1, hist_extra=1),
    "HHV": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_hhv, hist_arg=1),
    "LLV": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_llv, hist_arg=1),
    "SUM": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_sum, hist_arg=1),
    "STD": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_std, hist_arg=1),
    "COUNT": _FuncSpec(2, 2, _NUM, ("cond", _W), _impl_count, hist_arg=1),
    "CROSS": _FuncSpec(2, 2, _BOOL, (_NUM, _NUM), _impl_cross),
    "ABS": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_abs),
    "MAX": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_max),
    "MIN": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_min),
    "IF": _FuncSpec(3, 3, "same", ("cond", _NUM, _NUM), _impl_if),
    "BARSLAST": _FuncSpec(1, 1, _NUM, ("cond",), _impl_barslast),
    # ── 本项目特有（数据来自本地库，不联网）──
    # MACD 三件套（0 参数，按收盘价算）。
    # `hist_default` 必须写足："最少 K 线"是这个函数**真需要的历史长度** ——
    # DIF 要 EMA26，DEA/MACD 还叠一层 EMA9（26+9=35）。写少了，试算/回测会拿
    # 只够 1 根的序列去算 EMA，得到一条"看着有值、其实没收敛"的曲线。
    "DIF": _FuncSpec(0, 0, _NUM, (), _impl_dif, hist_default=26),
    "DEA": _FuncSpec(0, 0, _NUM, (), _impl_dea, hist_default=35),
    "MACD": _FuncSpec(0, 0, _NUM, (), _impl_macd, hist_default=35),
    "涨停天数": _FuncSpec(0, 1, _NUM, (_W,), _impl_limit_up_days, hist_arg=0, hist_default=1),
    "连板": _FuncSpec(0, 0, _NUM, (), _impl_limit_up_cnt, hist_default=1),
    "量比": _FuncSpec(0, 1, _NUM, (_W,), _impl_vol_ratio, hist_arg=0,
                   hist_extra=1, hist_default=6),
    # ── 通达信（TDX）兼容层：选股公式里的高频函数 ──
    # 参数种类沿用本模块的 `_NUM`（数值序列）/`_W`（窗口，正整数）/`_OFF`（偏移，可为 0）/
    # `"cond"`（条件序列）；`hist_arg`/`hist_default` 决定"最少需要多少根 K 线"的估算 —— 
    # 写少了会让试算/回测拿不够长的序列去算，得到的曲线"看着有值、其实没收敛"。
    "SMA": _FuncSpec(3, 3, _NUM, (_NUM, _W, _NUM), _impl_sma, hist_default=20),
    "DMA": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_dma, hist_default=20),
    "WMA": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_wma, hist_arg=1),
    "EXPMA": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_ema, hist_arg=1),
    "EXIST": _FuncSpec(2, 2, _BOOL, ("cond", _W), _impl_exist, hist_arg=1),
    "EVERY": _FuncSpec(2, 2, _BOOL, ("cond", _W), _impl_every, hist_arg=1),
    "BETWEEN": _FuncSpec(3, 3, _BOOL, (_NUM, _NUM, _NUM), _impl_between),
    "RANGE": _FuncSpec(3, 3, _BOOL, (_NUM, _NUM, _NUM), _impl_between),
    "FILTER": _FuncSpec(2, 2, _BOOL, ("cond", _W), _impl_filter, hist_arg=1),
    "UPNDAY": _FuncSpec(2, 2, _BOOL, (_NUM, _W), _impl_upnday, hist_arg=1, hist_extra=1),
    "DOWNNDAY": _FuncSpec(2, 2, _BOOL, (_NUM, _W), _impl_downnday, hist_arg=1, hist_extra=1),
    "NDAY": _FuncSpec(3, 3, _BOOL, (_NUM, _NUM, _W), _impl_nday, hist_arg=2),
    "BARSCOUNT": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_barscount),
    "BARSSINCE": _FuncSpec(1, 1, _NUM, ("cond",), _impl_bars_since, hist_default=1),
    "LONGCROSS": _FuncSpec(3, 3, _BOOL, (_NUM, _NUM, _W), _impl_longcross, hist_arg=2,
                           hist_extra=1),
    # `VALUEWHEN` 的返回值就是 X 的类型（数值），所以用 `_NUM` 而不是 `"same"`：
    # `"same"` 那套是给 `IF` 这种"两边类型要一致"的函数用的，只有两个参数时会越界。
    "VALUEWHEN": _FuncSpec(2, 2, _NUM, ("cond", _NUM), _impl_valuewhen, hist_default=1),
    "LAST": _FuncSpec(3, 3, _BOOL, ("cond", _OFF, _OFF), _impl_last, hist_default=1),
    "HHVBARS": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_hhvbars, hist_arg=1),
    "LLVBARS": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_llvbars, hist_arg=1),
    "SUMBARS": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_sumbars, hist_default=1),
    "VAR": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_var, hist_arg=1),
    "STDP": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_stdp, hist_arg=1),
    "AVEDEV": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_avedev, hist_arg=1),
    "SLOPE": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_slope, hist_arg=1),
    "FORCAST": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_forcast, hist_arg=1),
    "RSI": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_rsi, hist_arg=1, hist_extra=1),
    "BOLL": _FuncSpec(2, 2, _NUM, (_NUM, _W), _impl_boll, hist_arg=1),
    "UB": _FuncSpec(3, 3, _NUM, (_NUM, _W, _NUM), _impl_ub, hist_arg=1),
    "LB": _FuncSpec(3, 3, _NUM, (_NUM, _W, _NUM), _impl_lb, hist_arg=1),
    "RSV": _FuncSpec(0, 0, _NUM, (), _impl_rsv, hist_default=9),
    "K": _FuncSpec(0, 0, _NUM, (), _impl_k, hist_default=15),
    "D": _FuncSpec(0, 0, _NUM, (), _impl_d, hist_default=18),
    "J": _FuncSpec(0, 0, _NUM, (), _impl_j, hist_default=18),
    "OBV": _FuncSpec(0, 0, _NUM, (), _impl_obv, hist_default=2),
    "WR": _FuncSpec(0, 1, _NUM, (_W,), _impl_wr, hist_arg=0, hist_default=9),
    "CCI": _FuncSpec(0, 1, _NUM, (_W,), _impl_cci, hist_arg=0, hist_default=14),
    "IFF": _FuncSpec(3, 3, "same", ("cond", _NUM, _NUM), _impl_if),
    "POW": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_pow),
    "SQRT": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_sqrt),
    "LOG": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_log),
    "LN": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_ln),
    "EXP": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_exp),
    "MOD": _FuncSpec(2, 2, _NUM, (_NUM, _NUM), _impl_mod),
    "INTPART": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_intpart),
    "ROUND": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_round),
    # 通达信内置：涨/跌停价（第二个参数 = 涨跌幅比例，0.1 = 10%，可省 → 按 10%）
    # 通达信 FINANCE(财务/股本)：本地没有财务数据，按主人指定的口径**等同流通市值**（亿元）
    # `uses_fields` 必须登记 流通市值：否则公式不会去取实时快照那一趟数据（永远取不到值）
    "FINANCE": _FuncSpec(0, 4, _NUM, (_NUM, _NUM), _impl_finance,
                         uses_fields=("流通市值",)),
    # 通达信 DYNAINFO(动态行情)：本地没有盘中快照，按主人指定的口径**等同 量比()**（倍）
    "DYNAINFO": _FuncSpec(0, 4, _NUM, (_NUM, _NUM), _impl_dynainfo,
                          hist_extra=1, hist_default=6),
    # 通达信 INBLOCK('板块名')：本地没有板块成分匹配，按主人指定的口径**等同 热门行业**（0~3）
    # `_ANY` = 参数随便写都收（字符串 / 数字 / 不传）；`uses_fields` 必须登记，
    # 否则"热门行业"不会被算、也不会进 Formula.fields（症状同上：永远取不到值）
    "INBLOCK": _FuncSpec(0, 2, _NUM, (_ANY, _ANY), _impl_inblock,
                         uses_fields=("热门行业",)),
    # 通达信 CODELIKE / NAMEINCLUDE：代码前缀、名称包含（都是逐票恒定的 0/1）。
    # `result=_BOOL` 很重要：它们要能直接进 `NOT(...)` / `OR` —— 声明成数值的话，
    # 逻辑运算会当场报"需要条件"（主人那段公式正是 `NOT(CODELIKE(...) OR ...)`）。
    "CODELIKE": _FuncSpec(1, 1, _BOOL, (_ANY,), _impl_codelike),
    "NAMEINCLUDE": _FuncSpec(1, 1, _BOOL, (_ANY,), _impl_nameinclude),
    "ZTPRICE": _FuncSpec(1, 2, _NUM, (_NUM, _NUM), _impl_ztprice),
    "DTPRICE": _FuncSpec(1, 2, _NUM, (_NUM, _NUM), _impl_dtprice),
    "CEILING": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_ceiling),
    "FLOOR": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_floor),
    "SIGN": _FuncSpec(1, 1, _NUM, (_NUM,), _impl_sign),
}

#: 展示给用户的"可用函数"清单（界面提示与错误提示共用，避免两处写法漂移）
SUPPORTED_FUNCTIONS: tuple[str, ...] = tuple(sorted(FUNCTIONS))

#: 关键字（大小写不敏感）；`&&`/`||`/`!` 在词法期已归一成这三个
_LOGIC_WORDS = ("AND", "OR", "NOT")


# ══════════════════════════════════════════════════════════════════════════
# 解析器（递归下降）
# ══════════════════════════════════════════════════════════════════════════


class _Parser:
    """把 token 流解析成 AST，**同时做静态类型检查**。

    为什么类型检查放在解析期（而不是等拿到数据再报错）：下一轮的界面要在用户
    敲字时实时校验，那时候**没有**股票数据；而且"最后一行是不是条件"这种判断
    本来就只依赖表达式结构，跟数据无关。
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = _tokenize(text)
        self.pos = 0
        self.nodes = 0
        self.vars: dict[str, str] = {}
        self.fields: list[str] = []
        self.funcs: list[str] = []
        self.outputs: list[str] = []
        self.min_history = 1
        self.depth = 0
        self.statements: list[_Statement] = []
        #: 编译期的"已知忽略项"（通达信兼容层：画图语句、样式修饰）。
        #: 界面【校验】会列给用户 —— 他得知道"我那句画线没生效"，
        #: 而不是以为整条公式都跑到了。
        self.notes: list[str] = []

    # ── token 流 ──

    def peek(self, k: int = 0) -> _Token:
        i = self.pos + k
        if i < len(self.toks):
            return self.toks[i]
        # EOF 沿用"最后一个 token 的下一列"，让报错位置落在公式末尾
        if self.toks:
            last = self.toks[-1]
            return _Token("eof", "", last.line, last.col + len(last.value))
        return _Token("eof", "", 1, 1)

    def next(self) -> _Token:
        tok = self.peek()
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def at_keyword(self, *words: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "ident" and tok.value.upper() in words

    def bump(self, tok: _Token) -> None:
        """AST 节点计数（超限即报错，防止超长表达式把解析/求值拖垮）。"""
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise FormulaError(
                f"策略太复杂（AST 节点超过 {MAX_AST_NODES}）",
                line=tok.line, col=tok.col, code="too_complex",
                hint="拆成多条 `:=` 中间变量，或者简化条件",
            )

    # ── 入口 ──

    def parse(self) -> tuple[list[_Statement], Any]:
        last_line = 0
        while self.pos < len(self.toks):
            start = self.peek()
            if start.line == last_line:
                raise FormulaError(
                    "一行只能写一条语句",
                    line=start.line, col=start.col, code="syntax",
                    hint="每条语句独占一行（`:=` 定义变量，最后一行写选股条件）",
                )
            if start.kind == "ident" and start.value.upper() in _DRAWING_FUNCS:
                # 通达信兼容层：画图语句整句跳过（选股用不到图形），只记一条 note。
                # 放在**最前面**判断：它的参数里可能有逗号/字符串，交给普通解析会报一堆假错。
                self._skip_line(start)
                last_line = self.toks[self.pos - 1].line
                continue
            if not self._is_assignment():
                node = self.expr()
                self._eat_style()          # `,COLORRED` 之类要**先**吃掉，否则会被当成多余 token
                if self.pos < len(self.toks):
                    self._raise_leftover(start, node)
                self.statements.append(_Statement(None, node, False, start.line, start.col))
            else:
                stmt = self._assignment()
                self._eat_style()
                self.statements.append(stmt)
            last_line = self.toks[self.pos - 1].line

        if not self.statements:
            raise FormulaError(
                "策略是空的", line=1, col=1, code="empty",
                hint="至少要写一行选股条件，例如 `C>MA(C,5)`",
            )
        if len(self.statements) > MAX_FORMULA_LINES:
            raise FormulaError(
                f"策略行数太多（{len(self.statements)} 行，上限 {MAX_FORMULA_LINES} 行）",
                line=self.statements[MAX_FORMULA_LINES].line, code="too_long",
            )
        condition = self._final_condition()
        return self.statements, condition

    def _skip_line(self, start: _Token) -> None:
        """跳过一整行（画图语句）并记一条 note。"""
        line = start.line
        while self.pos < len(self.toks) and self.toks[self.pos].line == line:
            self.pos += 1
        self._note(f"第 {line} 行的画图语句 {start.value}(…) 已忽略（画图对选股没有影响）")

    def _eat_style(self) -> None:
        """吃掉通达信的输出样式修饰：`,COLORRED` / `,LINETHICK2` / `,NODRAW` / `,SHIFT3` …"""
        eaten: list[str] = []
        while True:
            if self.peek().kind != "comma":
                break
            nxt = self.peek(1)
            if nxt.kind != "ident":
                break
            name = nxt.value.upper()
            if name in _STYLE_MODS or name.startswith(
                ("COLOR", "LINETHICK", "SHIFT", "PRECISION", "LAYER", "MOVE", "ALIGN")
            ):
                eaten.append(nxt.value)
                self.next()
                self.next()
                continue
            break
        if eaten:
            self._note("已忽略样式修饰：" + "、".join(dict.fromkeys(eaten)))

    def _note(self, text: str) -> None:
        """记一条"已知忽略项"（同一句只记一次，避免重复刷屏）。"""
        if text not in self.notes:
            self.notes.append(text)

    def _raise_leftover(self, start: _Token, node: Any) -> None:
        """裸表达式后面还剩 token。

        分两种情况给**不同**的错，因为用户要做的事完全不同：
        * 剩的东西在同一行 → 是"这行多写了东西"（多半是多余括号/逗号/两条语句挤一行）；
        * 剩的东西在后面的行 → 是"中间行没写变量名"（想输出中间序列却写成裸表达式）。
        """
        leftover = self.peek()
        if leftover.line != start.line:
            raise FormulaError(
                "中间语句必须是赋值",
                line=start.line, col=start.col, code="syntax",
                hint="中间行要写成 `X:=表达式`（例如 M5:=MA(C,5)），最后一行才是选股条件",
            )
        if leftover.kind == "rparen":
            raise FormulaError(
                "多余的 `)`", line=leftover.line, col=leftover.col, code="syntax",
                hint="括号没有对应的 `(`",
            )
        if leftover.kind == "comma":
            raise FormulaError(
                "多余的逗号", line=leftover.line, col=leftover.col, code="syntax",
                hint="策略按行分隔语句，行尾和中间都不需要逗号（参数之间才用）",
            )
        raise FormulaError(
            "一行只能写一条语句",
            line=leftover.line, col=leftover.col, code="syntax",
            hint="每条语句独占一行（`:=` 定义变量，最后一行写选股条件）",
        )

    def _final_condition(self) -> Any:
        """最后一条语句必须是**裸表达式**且是条件（返回 0/1）。"""
        last = self.statements[-1]
        if last.name is not None:
            # 这一段专门服务"从网上抄来的**片段**"（2026-09-21 主人就贴了这么一段：
            # 一个横跨 5 行的 `BAN := NOT(...)`，没有最后那行条件）。
            # 光说"最后一行必须是条件"他知道该做什么，但不知道**用哪个变量** ——
            # 所以直接把他刚定义的那个名字写进提示里，照抄一行就能跑。
            name = str(last.name)
            raise FormulaError(
                f"这条策略只定义了中间变量 {name}，没有写选股条件",
                line=last.line, col=last.col, code="not_condition",
                hint=f"在最后再加一行 `{name}`（或者别的条件，例如 `{name} AND C>MA(C,5)`）"
                     " —— 最后一行才是「选出哪些票」的条件；`X:=...` 只是定义中间变量",
            )
        if last.node.dtype != _BOOL:
            raise FormulaError(
                "策略最后一行必须是选股条件（返回 0/1 的表达式），"
                f"现在是{_KIND_LABEL[last.node.dtype]}序列",
                line=last.line, col=last.col, code="not_condition",
                hint="加一个比较，例如 `C>MA(C,5)`、`量比()>1.5`",
            )
        return last.node

    def _is_assignment(self) -> bool:
        tok = self.peek()
        if tok.kind != "ident":
            return False
        nxt = self.peek(1)
        return nxt.kind in ("assign", "colon")

    def _assignment(self) -> _Statement:
        name_tok = self.next()
        op_tok = self.next()
        self._check_name_usable(name_tok, as_variable=True)
        node = self.expr()
        upper = name_tok.value.upper()
        # 允许重复赋值（通达信语义），但**不允许**覆盖字段/函数名 ——
        # 那会让用户后面的 `C` 突然变成自己的变量，属于极难排查的坑
        self.vars[upper] = node.dtype
        if op_tok.kind == "colon" and upper not in self.outputs:
            self.outputs.append(upper)
        return _Statement(
            upper, node, op_tok.kind == "colon", name_tok.line, name_tok.col,
        )

    def _check_name_usable(self, tok: _Token, *, as_variable: bool) -> None:
        """变量名不能与字段/函数重名，也不能用 Python 关键字。"""
        name = tok.value
        upper = name.upper()
        self._check_forbidden(tok)
        if not upper.isidentifier() and not any("\u4e00" <= c <= "\u9fff" for c in upper):
            raise FormulaError(
                f'变量名 "{name}" 不合法', line=tok.line, col=tok.col, code="syntax",
            )
        if upper in _all_fields():
            raise FormulaError(
                f'变量名 "{name}" 与内置字段同名',
                line=tok.line, col=tok.col, code="syntax",
                hint=f"{upper} 已经是行情字段，请换一个名字（例如 MY{upper}）",
            )
        if upper in FUNCTIONS and not as_variable:
            raise FormulaError(
                f'变量名 "{name}" 与内置函数同名',
                line=tok.line, col=tok.col, code="syntax",
                hint="请换一个名字（例如 M5、A1）",
            )
        # ⚠️ **赋值时允许与函数同名**（2026-09-21，通达信兼容）：
        # 通达信公式里 `RSV:=...`、`K:=SMA(RSV,3,1)`、`D:=SMA(K,3,1)` 是标准写法，
        # 而 RSV/K/D/J 恰好也都是我们的函数名 —— 一律拒绝就等于"最经典的 KDJ 选股公式
        # 一条都跑不了"。字段名（C/O/H/L/V…）仍然禁止覆盖：那会让后面的 `C` 突然变成
        # 用户自己的变量，属于最难排查的一类坑。

    def _check_forbidden(self, tok: _Token) -> None:
        name = tok.value
        upper = name.upper()
        if name.startswith("_"):
            # `__class__` / `__import__` / `_x` 一律拒绝：公式没有任何理由访问
            # 以下划线开头的名字，而这些名字正是绕过白名单的常用入口
            raise FormulaError(
                f'策略里不允许使用 "{name}" 这类下划线名字',
                line=tok.line, col=tok.col, code="forbidden",
                hint="策略不能访问程序的内部对象，只能使用字段和上面列出的函数",
            )
        if upper in _SPECIAL_WORDS:
            raise FormulaError(
                _SPECIAL_WORDS[upper], line=tok.line, col=tok.col, code="forbidden",
            )
        if upper in _FORBIDDEN_WORDS:
            raise FormulaError(
                f'策略里不允许使用 "{name}"',
                line=tok.line, col=tok.col, code="forbidden",
                hint=_FORBIDDEN_WORDS[upper],
            )

    # ── 表达式（优先级从低到高）──

    def expr(self) -> Any:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            tok = self.peek()
            raise FormulaError(
                f"表达式嵌套太深（超过 {MAX_DEPTH} 层）",
                line=tok.line, col=tok.col, code="too_complex",
                hint="用 `:=` 把中间结果拆成几个变量",
            )
        try:
            return self.or_expr()
        finally:
            self.depth -= 1

    def or_expr(self) -> Any:
        node = self.and_expr()
        while True:
            tok = self.peek()
            if (tok.kind == "op" and tok.value == "OR") or self.at_keyword("OR"):
                self.next()
                right = self.and_expr()
                node = self._logic("OR", node, right, tok)
            else:
                return node

    def and_expr(self) -> Any:
        node = self.not_expr()
        while True:
            tok = self.peek()
            if (tok.kind == "op" and tok.value == "AND") or self.at_keyword("AND"):
                self.next()
                right = self.not_expr()
                node = self._logic("AND", node, right, tok)
            else:
                return node

    def not_expr(self) -> Any:
        tok = self.peek()
        if (tok.kind == "op" and tok.value == "NOT") or self.at_keyword("NOT"):
            self.next()
            operand = self.not_expr()
            self._require_cond(operand, tok, "NOT")
            self.bump(tok)
            return _Unary("NOT", operand, _BOOL, tok.line, tok.col)
        return self.comparison()

    def _logic(self, op: str, left: Any, right: Any, tok: _Token) -> Any:
        self._require_cond(left, tok, op)
        self._require_cond(right, tok, op)
        self.bump(tok)
        return _Binary(op, left, right, _BOOL, tok.line, tok.col)

    def _require_cond(self, node: Any, tok: _Token, what: str) -> None:
        """条件位必须是**条件型**表达式（比较 / CROSS / IN / AND OR NOT）。

        为什么要求这么严（通达信对"非 0 即真"更宽松）：`V AND C>O` 在宽松语义下
        等于"成交量非 0 且收阳"，几乎总是**用户写错了**（他想要的是放量）。宽松会让
        这种错误静默生效、结果看着还挺像那么回事；严格则当场告诉他该写 `V>0`。
        选股公式是"选出来的东西要真金白银买"的工具，宁可恶毒一点。
        """
        if node.dtype == _STR:
            raise FormulaError(
                f'运算符 "{what}" 需要条件（返回 0/1 的表达式），但两边有字符串',
                line=tok.line, col=tok.col, code="type",
                hint="字符串只能做 = / != 或 IN 比较；判断行业请写 INDUSTRY=\"半导体\"",
            )
        if node.dtype != _BOOL:
            raise FormulaError(
                f'运算符 "{what}" 需要条件（返回 0/1 的表达式），'
                f"现在是{_KIND_LABEL[node.dtype]}序列",
                line=tok.line, col=tok.col, code="type",
                hint="写成比较式，例如 C>O、V>MA(V,5)、连板()>=2",
            )

    def comparison(self) -> Any:
        left = self.additive()
        tok = self.peek()
        if self.at_keyword("IN"):
            return self._in_list(left, self.next())
        if tok.kind == "op" and tok.value in _CMP_OPS:
            self.next()
            right = self.additive()
            nxt = self.peek()
            if nxt.kind == "op" and nxt.value in _CMP_OPS:
                raise FormulaError(
                    "不支持连续比较（例如 1<C<5）",
                    line=nxt.line, col=nxt.col, code="syntax",
                    hint="请写成 `1<C AND C<5`",
                )
            return self._compare(tok, left, right)
        return left

    def _compare(self, tok: _Token, left: Any, right: Any) -> Any:
        if tok.value in ("=", "!="):
            # 字符串只能和字符串比；数值与条件可以互比（条件当 0/1，通达信同款宽松）
            if _STR in (left.dtype, right.dtype) and left.dtype != right.dtype:
                raise FormulaError(
                    f'运算符 "{tok.value}" 两边类型不一致',
                    line=tok.line, col=tok.col, code="type",
                    hint="字符串要和字符串比（例如 INDUSTRY=\"半导体\"）",
                )
        else:
            for side in (left, right):
                if side.dtype == _STR:
                    raise FormulaError(
                        f'运算符 "{tok.value}" 需要数值，但两边有字符串',
                        line=tok.line, col=tok.col, code="type",
                        hint="字符串只支持 = / != 和 IN（例如 INDUSTRY IN (\"半导体\")）",
                    )
        self.bump(tok)
        return _Binary(tok.value, left, right, _BOOL, tok.line, tok.col)

    def _in_list(self, operand: Any, tok: _Token) -> Any:
        if operand.dtype != _STR:
            raise FormulaError(
                "IN 只能用于字符串字段（如 INDUSTRY）",
                line=tok.line, col=tok.col, code="type",
                hint='写法：INDUSTRY IN ("半导体","软件服务")',
            )
        if self.peek().kind != "lparen":
            raise FormulaError(
                "IN 后面要跟一个括号列表",
                line=tok.line, col=tok.col, code="syntax",
                hint='写法：INDUSTRY IN ("半导体","软件服务")',
            )
        self.next()          # (
        values: list[str] = []
        if self.peek().kind == "rparen":
            raise FormulaError(
                "IN 的列表不能为空", line=tok.line, col=tok.col, code="syntax",
                hint='写法：INDUSTRY IN ("半导体","软件服务")',
            )
        while True:
            item = self.peek()
            if item.kind != "str":
                raise FormulaError(
                    "IN 列表里只能是字符串",
                    line=item.line, col=item.col, code="type",
                    hint='写法：INDUSTRY IN ("半导体","软件服务")',
                )
            values.append(self.next().value)
            sep = self.peek()
            if sep.kind == "comma":
                self.next()
                continue
            if sep.kind == "rparen":
                self.next()
                break
            raise FormulaError(
                "IN 列表没有闭合", line=sep.line, col=sep.col, code="syntax",
                hint="列表项之间用逗号分隔，最后补一个 `)`",
            )
        if not values:  # pragma: no cover - 空列表已在上面拦下
            raise FormulaError(
                "IN 的列表不能为空", line=tok.line, col=tok.col, code="syntax",
            )
        self.bump(tok)
        return _InList(operand, tuple(values), tok.line, tok.col)

    def additive(self) -> Any:
        node = self.multiplicative()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.value in ("+", "-"):
                self.next()
                right = self.multiplicative()
                node = self._arith(tok.value, node, right, tok)
            else:
                return node

    def multiplicative(self) -> Any:
        node = self.unary()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.value in ("*", "/"):
                self.next()
                right = self.unary()
                node = self._arith(tok.value, node, right, tok)
            else:
                return node

    def _arith(self, op: str, left: Any, right: Any, tok: _Token) -> Any:
        for side, label in ((left, "左边"), (right, "右边")):
            if side.dtype == _STR:
                raise FormulaError(
                    f'运算符 "{op}" 需要数值，但{label}是字符串',
                    line=tok.line, col=tok.col, code="type",
                    hint="策略不支持字符串拼接；字符串只能做 = / != 和 IN 比较",
                )
        self.bump(tok)
        return _Binary(op, left, right, _NUM, tok.line, tok.col)

    def unary(self) -> Any:
        tok = self.peek()
        if tok.kind == "op" and tok.value == "-":
            self.next()
            operand = self.unary()
            if operand.dtype == _STR:
                raise FormulaError(
                    '负号 "-" 只能用在数值上', line=tok.line, col=tok.col, code="type",
                )
            if isinstance(operand, _Lit):
                # 常量折叠：`-1` 就该是一个字面量 -1。这样 `MA(C,-1)`、`REF(C,-1)`
                # 能在**解析期**就报"窗口 N 非法"，而不是拖到求值期才暴露，
                # 界面上的实时校验也才拦得住。
                self.bump(tok)
                return _Lit(-float(operand.value), _NUM, tok.line, tok.col)
            self.bump(tok)
            return _Unary("-", operand, _NUM, tok.line, tok.col)
        return self.primary()

    def primary(self) -> Any:
        tok = self.peek()

        if tok.kind == "num":
            self.next()
            value = float(tok.value)
            if not math.isfinite(value) or abs(value) > MAX_LITERAL:
                raise FormulaError(
                    f"数字太大：{tok.value}",
                    line=tok.line, col=tok.col, code="syntax",
                    hint=f"字面量绝对值不能超过 {MAX_LITERAL:g}",
                )
            self.bump(tok)
            return _Lit(value, _NUM, tok.line, tok.col)

        if tok.kind == "str":
            self.next()
            self.bump(tok)
            # `"公式名.指标"`（通达信引用别的公式）在本引擎里只能是一个字符串常量，
            # 拿去当数值用时会以"字符串不能参与运算"收场 —— 那种提示会让人以为是引号写错了。
            # 这里提前认出来：形状是"名字.名字"，且出现在值的位置。
            value = str(tok.value or "")
            if _looks_like_formula_reference(value):
                raise FormulaError(
                    f'引用其它策略（"{value}"）本引擎不支持：本地没有共享的策略库',
                    line=tok.line, col=tok.col, code="formula_ref",
                    hint="把被引用那条策略的算式直接抄进来（例如它算的是 "
                         "`EMA(C,12)-EMA(C,26)`，就在这条策略里照样写一遍）；"
                         "要用现成的指标可以直接调 MACD / KDJ / RSI / BOLL 这些内置函数",
                )
            return _Lit(tok.value, _STR, tok.line, tok.col)

        if tok.kind == "lparen":
            self.next()
            node = self.expr()
            closing = self.peek()
            if closing.kind != "rparen":
                raise FormulaError(
                    "括号没有配对", line=tok.line, col=tok.col, code="syntax",
                    hint="补一个 `)`",
                )
            self.next()
            return node

        if tok.kind == "ident":
            self.next()                 # 先吃掉名字本身，后面只管括号与参数
            self._check_forbidden(tok)
            upper = tok.value.upper()
            # 名字已经吃掉，所以"后面是不是括号"看的是当前 token
            if self.peek().kind == "lparen":
                return self._call(tok, upper)
            if upper in FUNCTIONS:
                # 通达信兼容（2026-09-21）：用户**先赋值**过的名字优先当变量。
                # 典型：`RSV:=...` 然后又写 `SMA(RSV,3,1)` —— RSV 同时是我们的函数名，
                # 若在这里就报"函数后面要跟括号"，最经典的 KDJ 选股公式直接跑不了。
                # 只对"已经被赋值过"的名字放行，没赋值过的仍然按"忘写括号"提示。
                if upper not in self.vars:
                    spec = FUNCTIONS[upper]
                    example = _FUNCTION_EXAMPLE.get(upper, f"{upper}(...)")
                    raise FormulaError(
                        f'函数 "{upper}" 后面要跟括号',
                        line=tok.line, col=tok.col, code="syntax",
                        hint=f"例如 {example}"
                        + ("" if spec.min_args else f"；{upper} 也可以写成 {upper}()"),
                    )
            return self._field_or_var(tok, upper)

        if tok.kind == "rparen":
            raise FormulaError(
                "多余的 `)`", line=tok.line, col=tok.col, code="syntax",
            )
        if tok.kind == "comma":
            raise FormulaError(
                "这里多了一个逗号", line=tok.line, col=tok.col, code="syntax",
            )
        if tok.kind == "op":
            raise FormulaError(
                f'表达式不完整：运算符 "{tok.value}" 前面缺少数值或条件',
                line=tok.line, col=tok.col, code="syntax",
            )
        raise FormulaError(
            "表达式不完整", line=tok.line, col=tok.col, code="syntax",
        )

    def _field_or_var(self, tok: _Token, upper: str) -> Any:
        aliases = _all_fields()
        if upper in self.vars:
            self.bump(tok)
            return _VarRef(upper, self.vars[upper], tok.line, tok.col)
        if upper in aliases:
            canonical = aliases[upper]
            if canonical not in self.fields:
                self.fields.append(canonical)
            self.bump(tok)
            return _FieldRef(canonical, _field_kind(canonical), tok.line, tok.col)

        # 报错要能指导用户：先给"拼错了"的近似匹配，再给已定义的变量
        pool = sorted(set(aliases) | set(self.vars))
        near = difflib.get_close_matches(upper, pool, n=1, cutoff=0.6)
        hints: list[str] = []
        if near:
            hints.append(f'是不是想写 "{near[0]}"？')
        hints.append("可用字段：" + "、".join(sorted(set(aliases.values()))))
        if self.vars:
            hints.append("已定义的变量：" + "、".join(sorted(self.vars)))
        raise FormulaError(
            f'字段 "{tok.value}" 不存在',
            line=tok.line, col=tok.col, code="unknown_field", hint="".join(hints),
        )

    def _call(self, tok: _Token, upper: str) -> Any:
        spec = FUNCTIONS.get(upper)
        if spec is None and upper in UNSUPPORTED_FUNCTIONS:
            # 已知函数、但本地数据前提不满足：**不要混进"未知函数"那套提示
            # （列可用函数清单对这种情况毫无帮助 —— 缺的是数据，不是函数名）
            raise FormulaError(
                f'"{tok.value}" 这个函数本引擎认识，但本地数据算不出来',
                line=tok.line, col=tok.col, code="unsupported_function",
                hint=UNSUPPORTED_FUNCTIONS[upper],
            )
        if spec is None:
            near = difflib.get_close_matches(upper, list(FUNCTIONS), n=1, cutoff=0.55)
            hint = (f'是不是想写 "{near[0]}"？' if near else "") + (
                "可用函数：" + "、".join(SUPPORTED_FUNCTIONS)
            ) + engine_stamp()
            # 上面这句 `engine_stamp()` 是**排查用**的：用户把报错原文贴过来时，
            # 这句话直接告诉我们他跑的是哪个版本、引擎认识多少函数 ——
            # 2026-09-21 就靠这个才不用反复猜"你是不是还在用旧包"。
            # 版本号是懒加载的（`laoa_trader/__init__` 会 import config，
            # 引擎在导入期不依赖它，只能在这里按需取）。
            raise FormulaError(
                f'未知函数 "{tok.value}"',
                line=tok.line, col=tok.col, code="unknown_function", hint=hint,
            )

        # 函数**内部**用到的字段也要登记（例如 FINANCE → 流通市值）：
        # `Formula.fields` 是"要不要取实时快照"的唯一判据，漏登记就会静默取不到值。
        for name in getattr(spec, "uses_fields", ()) or ():
            if name not in self.fields:
                self.fields.append(name)

        self.next()                       # (
        args: list[Any] = []
        if self.peek().kind != "rparen":
            while True:
                args.append(self.expr())
                sep = self.peek()
                if sep.kind == "comma":
                    self.next()
                    continue
                break
        closing = self.peek()
        if closing.kind != "rparen":
            raise FormulaError(
                f'函数 "{upper}" 的括号没有闭合',
                line=tok.line, col=tok.col, code="syntax",
                hint=f"补一个 `)`（写法：{_FUNCTION_EXAMPLE.get(upper, upper + '(...)')}）",
            )
        self.next()

        if not (spec.min_args <= len(args) <= spec.max_args):
            want = (
                f"{spec.min_args} 个"
                if spec.min_args == spec.max_args
                else f"{spec.min_args}~{spec.max_args} 个"
            )
            raise FormulaError(
                f'函数 "{upper}" 需要 {want}参数，现在给了 {len(args)} 个',
                line=tok.line, col=tok.col, code="arity",
                hint=f"写法：{_FUNCTION_EXAMPLE.get(upper, upper + '(...)')}",
            )

        for index, (arg, want) in enumerate(zip(args, spec.arg_kinds)):
            self._check_arg(upper, index, arg, want)

        self._note_history(spec, args)
        if upper not in self.funcs:
            self.funcs.append(upper)

        if spec.result == "same":
            # IF(COND,A,B)：类型取 A/B，两者必须一致（否则 np.where 会静默造出字符串数组）
            if args[1].dtype != args[2].dtype:
                raise FormulaError(
                    f'函数 "{upper}" 的两个分支类型不一致',
                    line=tok.line, col=tok.col, code="type",
                    hint="IF 的第 2、3 个参数要么都是数值，要么都是条件",
                )
            dtype = args[1].dtype
        else:
            dtype = spec.result

        self.bump(tok)
        return _Call(upper, tuple(args), dtype, tok.line, tok.col)

    def _check_arg(self, name: str, index: int, arg: Any, want: str) -> None:
        if want == _ANY:
            return                       # 不检查：兼容函数刻意收下任何参数
        if want == "cond":
            # 与 _require_cond 同一套严格标准（COUNT/BARSLAST/IF 的条件位）
            if arg.dtype != _BOOL:
                raise FormulaError(
                    f'函数 "{name}" 的第 {index + 1} 个参数需要条件（返回 0/1 的表达式），'
                    f"现在是{_KIND_LABEL.get(arg.dtype, arg.dtype)}序列",
                    line=arg.line, col=arg.col, code="type",
                    hint="写成比较式，例如 COUNT(C>O,10)、BARSLAST(连板()>0)",
                )
            return
        if want == _NUM:
            if arg.dtype == _STR:
                raise FormulaError(
                    f'函数 "{name}" 的第 {index + 1} 个参数需要数值，但给的是字符串',
                    line=arg.line, col=arg.col, code="type",
                )
            return
        # window / offset：必须是正整数常数
        if arg.dtype != _NUM:
            raise FormulaError(
                f'函数 "{name}" 的第 {index + 1} 个参数（窗口 N）必须是数值常数',
                line=arg.line, col=arg.col, code="arity",
                hint="写成常数，例如 MA(C,5)",
            )
        if isinstance(arg, _Lit):
            self._check_window_literal(name, index, arg, want)

    def _check_window_literal(self, name: str, index: int, arg: _Lit, want: str) -> None:
        minimum = 0 if want == _OFF else 1
        value = float(arg.value)
        if value != math.floor(value):
            raise FormulaError(
                f'函数 "{name}" 的第 {index + 1} 个参数（窗口 N）必须是整数，现在是 {value:g}',
                line=arg.line, col=arg.col, code="arity",
            )
        k = int(value)
        if k < minimum or k > MAX_WINDOW:
            raise FormulaError(
                f'函数 "{name}" 的第 {index + 1} 个参数（窗口 N）'
                f"必须在 {minimum}~{MAX_WINDOW} 之间，现在是 {k}",
                line=arg.line, col=arg.col, code="window",
            )

    def _note_history(self, spec: _FuncSpec, args: list) -> None:
        """估算"至少需要多少根 K 线"（**下界**，给回测/界面预热用）。

        为什么是下界：动态窗口（`MA(C,MY_N)`、`MA(C,量比())`）静态算不出来，
        这里只统计常量窗口。回测/试算按这个值预取历史就够，取多了只是浪费。
        """
        if spec.hist_arg is None:
            self._note_window(max(spec.hist_default, 1))
            return
        if len(args) > spec.hist_arg:
            arg = args[spec.hist_arg]
            # 只有常量窗口能算；变量窗口（少见）不估
            if not isinstance(arg, _Lit) or not float(arg.value).is_integer():
                return
            self._note_window(max(int(arg.value) + spec.hist_extra, 1))
            return
        self._note_window(max(spec.hist_default, 1))

    def _note_window(self, bars: int) -> None:
        if bars > self.min_history:
            self.min_history = int(bars)


#: 每个函数的示例写法（错误提示里直接抄给用户）
_FUNCTION_EXAMPLE: dict[str, str] = {
    "MA": "MA(C,5)",
    "EMA": "EMA(C,12)",
    "REF": "REF(C,1)",
    "HHV": "HHV(H,20)",
    "LLV": "LLV(L,20)",
    "SUM": "SUM(V,5)",
    "STD": "STD(C,20)",
    "COUNT": "COUNT(C>O,10)",
    "CROSS": "CROSS(C,MA(C,5))",
    "ABS": "ABS(C-PRE_CLOSE)",
    "MAX": "MAX(C,O)",
    "MIN": "MIN(C,O)",
    "IF": "IF(C>O,C,O)",
    "BARSLAST": "BARSLAST(连板()>0)",
    "涨停天数": "涨停天数() 或 涨停天数(10)",
    "连板": "连板()",
    "量比": "量比() 或 量比(5)",
    "DIF": "DIF()",
    "DEA": "DEA()",
    "MACD": "MACD()",
}


# ══════════════════════════════════════════════════════════════════════════
# 求值器
# ══════════════════════════════════════════════════════════════════════════


def _validate_series(series: Series) -> int:
    """长度自检：所有数值序列必须与 `date` 等长。返回 n。"""
    n = len(series.date)
    for name in (
        "close", "open", "high", "low", "vol", "amount", "pre_close",
        "limit_up_days", "limit_up_cnt",
    ):
        arr = np.asarray(getattr(series, name))
        if arr.shape[0] != n:
            raise FormulaDataError(
                f"{series.symbol} 的 {name} 有 {arr.shape[0]} 个值，"
                f"但日期有 {n} 个 —— 序列长度必须一致"
            )
    for name, values in series.extra.items():
        arr = np.asarray(values, dtype="float64")
        if arr.ndim != 1 or arr.shape[0] != n:
            raise FormulaDataError(
                f"{series.symbol} 的扩展字段 {name} 必须是长度 {n} 的一维序列，"
                f"现在是 {arr.shape}"
            )
    return n


class _Evaluator:
    """白名单求值器：**只认** AST 里那几种节点，别的类型直接报错（绝不执行）。

    无状态污染：每次 `eval` 新建一个实例，所以同一个 `Formula` 可以在多个线程
    里并行跑（下一轮界面要按股票多线程试算）。
    """

    def __init__(self, series: Series) -> None:
        self.series = series
        self.n = _validate_series(series)
        self.env: dict[str, Any] = {}
        self._numeric = {
            "C": series.close,
            "OPEN": series.open,
            "HIGH": series.high,
            "LOW": series.low,
            "VOL": series.vol,
            "AMOUNT": series.amount,
            "PRE_CLOSE": series.pre_close,
        }
        self._strings: dict[str, Any] = {
            "DATE": list(series.date),
            "INDUSTRY": series.industry,
        }

    # ── 参数校验 ──

    def win(
        self, value: Any, node: Any, *, minimum: int = 1, code: str = "window",
    ) -> int:
        """求值期的窗口参数校验（字面量已在解析期查过；变量形式只能在这里拦）。"""
        arr = np.asarray(value)
        if arr.ndim != 0:
            raise FormulaError(
                "窗口参数 N 必须是常数，不能是序列",
                line=node.line, col=node.col, code=code,
                hint="如果 N 来自变量，请先写成常数，例如 N:=5",
            )
        raw = float(arr)
        if not math.isfinite(raw) or raw != math.floor(raw):
            raise FormulaError(
                f"窗口参数 N 必须是整数，现在是 {raw:g}",
                line=node.line, col=node.col, code=code,
            )
        k = int(raw)
        if k < minimum or k > MAX_WINDOW:
            raise FormulaError(
                f"窗口参数 N 必须在 {minimum}~{MAX_WINDOW} 之间，现在是 {k}",
                line=node.line, col=node.col, code=code,
            )
        return k

    # ── 求值 ──

    def run(self, formula: Formula) -> np.ndarray:
        if self.n == 0:
            # 空序列（新股/空库）直接返回空信号，**不走求值**：
            # 一堆滚动窗口在长度 0 上跑没有意义，也容易在某些 numpy 版本上告警
            return np.zeros(0, dtype=bool)
        result: Any = None
        for statement in formula.statements:
            value = self.eval_node(statement.node)
            if statement.name is None:
                result = value            # 只有最后一条是无名语句（解析期已保证）
            else:
                self.env[statement.name] = value
        if result is None:  # pragma: no cover - 解析期保证存在
            raise FormulaError("策略里没有选股条件（内部错误）", code="internal")
        return _as_cond_array(result, self.n)

    def eval_node(self, node: Any) -> Any:
        if isinstance(node, _Lit):
            return node.value
        if isinstance(node, _FieldRef):
            return self._field(node)
        if isinstance(node, _VarRef):
            if node.name not in self.env:
                raise FormulaError(
                    f'变量 "{node.name}" 在使用前没有赋值',
                    line=node.line, col=node.col, code="unknown_var",
                )
            return self.env[node.name]
        if isinstance(node, _Unary):
            return self._unary(node)
        if isinstance(node, _Binary):
            return self._binary(node)
        if isinstance(node, _Call):
            spec = FUNCTIONS[node.name]
            vals = [self.eval_node(arg) for arg in node.args]
            return spec.impl(self, node, vals)
        if isinstance(node, _InList):
            operand = self.eval_node(node.operand)
            values = set(node.values)
            arr = _as_object_array(operand, self.n)
            flags = np.array([v in values for v in arr], dtype="float64")
            return flags
        # 白名单兜底：不认识的节点**不执行**，报错收场
        raise FormulaError(
            "策略里有不支持的表达式（内部错误）", code="internal",
        )

    def _field(self, node: _FieldRef) -> Any:
        if node.dtype == _STR:
            if node.name in self._strings:
                return self._strings[node.name]
        else:
            if node.name in self._numeric:
                return self._numeric[node.name]
            extra = self.series.extra.get(node.name)
            if extra is not None:
                return np.asarray(extra, dtype="float64")
            if node.name in EXTRA_FIELDS:
                # 注册过的扩展字段**在这只票上没有值**（典型：`流通市值`/`换手率`
                # 只有实时快照才给，而这只票今天没取到）→ 返回**全 NaN**：
                # 用到它的条件不成立（不出信号），而不是报错。
                # 为什么不是报错：字段名写错在**编译期**就被拦住了（名字没注册就过不了），
                # 能走到这里的"缺值"只可能是一只票的**数据**问题 —— 全市场几千只票
                # 各自报一次错，会把试算的结论刷成一片"本地数据里没有字段…"，
                # 而真正的答案只是"它们今天不满足条件"。
                return np.full(self.n, np.nan, dtype="float64")
        raise FormulaDataError(
            f"本地数据里没有字段 {node.name} 的数据（{self.series.symbol}）"
        )

    def _unary(self, node: _Unary) -> Any:
        value = self.eval_node(node.operand)
        if node.op == "NOT":
            return _not(value, self.n)
        return _nanify(-_as_float(value))

    def _binary(self, node: _Binary) -> Any:
        op = node.op
        if op in ("AND", "OR"):
            left = _as_cond_float(self.eval_node(node.left), self.n)
            right = _as_cond_float(self.eval_node(node.right), self.n)
            return _logic_combine(op, left, right, self.n)

        left = self.eval_node(node.left)
        right = self.eval_node(node.right)

        if op in ("=", "!=") and (node.left.dtype == _STR or node.right.dtype == _STR):
            return _str_cmp(op, left, right, self.n)
        if op in (">", "<", ">=", "<=", "=", "!="):
            return _num_cmp(op, left, right)

        A = _as_float(left)
        B = _as_float(right)
        with np.errstate(all="ignore"):
            if op == "+":
                result = np.add(A, B)
            elif op == "-":
                result = np.subtract(A, B)
            elif op == "*":
                result = np.multiply(A, B)
            else:
                result = np.divide(A, B)
        return _nanify(result)


# ══════════════════════════════════════════════════════════════════════════
# 数据接口（与数据库解耦：求值器只认 Series）
# ══════════════════════════════════════════════════════════════════════════

#: 「今天」这一根的合成规则里的浮点余量：涨停价是两位小数，比大小要留一点余量
#: （与 `price_limits` 的 `_EPS`、`public_sync` 的 `PRICE_EPS` 同一个量级）。
LIVE_LIMIT_EPS = 0.005


@dataclass(frozen=True)
class LiveBar:
    """盘中「今天」这一根 K 线的**原始（不复权）**快照值 —— 实时口径的原料。

    为什么要有它：用户 2026-09-23 定的规矩是"在软件内置规则里设定：开盘时间里运行的
    选股都是实时的，不是开盘时间才采用 K 线"。日更要到收盘后才有今天这根，
    所以盘中必须**自己把今天这一根拼出来** —— 拼出来之后，用户已经写好的公式
    （`C`、`C/REF(C,1)-1`、`量比()`、`C>MA(C,5)`）不用改一个字就全变成盘中口径，
    这正是"内置规则"而不是"再写一条盘中公式"的意义。

    为什么存**原始价**、换算放到 `load_series` 里做：快照给的现价永远是不复权价
    （交易所口径），换到后复权要乘"库里最后一根的后复权收盘 ÷ 这里的昨收" ——
    那个比例只有同时看得见"库里的行"与"这里这几个数"的地方才算得出来
    （理由见 `_live_arrays`）。存原始价还有个好处：`pre_close` 也正好是
    涨停价的基准（`price_limits.limit_up_price` 要的就是不复权前收）。

    Attributes:
        prev_close: 快照里的**昨收**（交易所调整后的前收盘）。缺了它这根 K 线就
            拼不出来（也**不编**一个出来），见 `_live_arrays`。
        close: 现价；`open` / `high` / `low` 是今开 / 最高 / 最低（都是不复权、元）。
        volume: 成交量（**股**）；`turnover`: 成交额（**元**）。口径与库里的列一致
            （后复权只调价不调量，见 `storage` 里的视图定义），所以可以直接接上去。
    """

    prev_close: float
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    turnover: float | None = None


def _live_arrays(
    bar: LiveBar,
    symbol: str,
    name: str,
    dates: list[str],
    close: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    vol: np.ndarray,
    amount: np.ndarray,
    limit_days: np.ndarray,
    limit_cnt: np.ndarray,
    *,
    today: str,
    kline_day: str | None,
) -> tuple[list[str], np.ndarray, ...] | None:
    """在日线后面接上"今天"这一根（拼不出来就返回 None，**绝不编数**）。

    换算口径（这是本函数唯一容易搞错的地方，所以推导写全）：

        快照给的现价是不复权价，而库里那一列是**后复权**价（`后复权 = 不复权 × 因子`）。
        设库里最后一根的后复权收盘为 `hfq_last`、因子为 `f_last`，快照昨收为 `adj_prev`
        （交易所调整后的前收盘，除权日会被下调），于是"今天"的因子 `f_today = f_last × k`，
        其中除权步长 `k = 原始前收 ÷ adj_prev`（见 `public_sync.factor_step`）。
        而 `hfq_last = 原始前收 × f_last`，代进去：

            f_today = hfq_last ÷ adj_prev          ⇒     今天的后复权价 = 现价 × hfq_last ÷ adj_prev

        所以只需要一个比例 `hfq_last / adj_prev`：它同时含了历史因子与今天的除权步长。
        比出来的 `今天的后复权收盘 ÷ 昨天那根` 正好等于 `现价 ÷ 昨收` = 今天的真实涨幅，
        也就是说 `C/REF(C,1)-1` 会原样给出**盘中涨幅**（有测试钉住）。

    为什么只有在"库里最后一根正好是全市场最新行情日"时才接（`kline_day`）：
    停牌、退市、或用户好多天没开机的票，最后一根是**旧的**。给它接一根今天的，
    它就会混进"今天选中的票"里 —— 而它的今天这根是按一个断了好几天的缺口算出来的，
    涨跌幅是假的。这类票本来就该被"最后一根不是最新行情日"那条规则跳过。

    Returns:
        接上今天那一根之后的全部数组（顺序与入参一致）；不该接/接不了返回 None：
        库里已经有今天这一根、最后一根不是最新行情日、现价或昨收缺一个。
    """
    if not dates or kline_day is None:
        return None
    if dates[-1] >= today:
        # 库里已经有"今天"这一根了（日更跑过，或数据比今天还新）：**不重复接**，
        # 同一天出现两根会让 REF/MA 这些窗口函数全部错位。
        return None
    if dates[-1] != kline_day:
        return None                      # 停牌/数据缺天的票：不靠"接一根"混进今天的选股
    if not (bar.prev_close and bar.prev_close > 0 and bar.close and bar.close > 0):
        return None                      # 缺现价或缺昨收：拼不出来，退回日线
    factor = float(close[-1]) / float(bar.prev_close)
    if not math.isfinite(factor) or factor <= 0:
        return None

    def scaled(value: float | None, fallback: float) -> float:
        """把快照里的不复权价换成后复权价；缺了就退到 `fallback`（现价）。

        为什么缺最高/最低时用现价而不是 NaN：一根 K 线的最高价天然 ≥ 现价、
        最低价 ≤ 现价，用现价填是**自洽的最小假设**（"今天到目前为止没有更高的价"）；
        填 NaN 会让 `C<=H` 这类本来该成立的写法在盘中集体失效，用户完全看不出原因
        （来源没给最高价这件事，界面上没有任何地方能显示）。
        """
        raw = float(value) if value else fallback
        return raw * factor

    new_close = float(bar.close) * factor
    new_open = scaled(bar.open, float(bar.close))
    new_high = max(scaled(bar.high, float(bar.close)), new_close, new_open)
    new_low = min(scaled(bar.low, float(bar.close)), new_close, new_open)
    new_vol = float(bar.volume) if bar.volume is not None else math.nan
    new_amount = float(bar.turnover) if bar.turnover is not None else math.nan

    # 今天是不是涨停：用**不复权**的现价与昨收判（涨停价本来就是不复权口径）。
    # 连板数由"昨天那一根"滚上来（与日更里攒涨停池的规则一致：在就 +1，否则算 1）。
    from laoa_trader import price_limits as pl       # 延迟导入：与 ZTPRICE 的实现同一份规则

    target = pl.limit_up_price(float(bar.prev_close), symbol, name)
    limit_today = 1.0 if (target is not None
                          and float(bar.close) >= target - LIVE_LIMIT_EPS) else 0.0
    cnt_today = (float(limit_cnt[-1]) + 1.0 if limit_today else 0.0)

    return (
        [*dates, today],
        np.append(close, new_close),
        np.append(open_, new_open),
        np.append(high, new_high),
        np.append(low, new_low),
        np.append(vol, new_vol),
        np.append(amount, new_amount),
        np.append(limit_days, limit_today),
        np.append(limit_cnt, cnt_today),
    )


@dataclass
class Series:
    """单只股票的**时间升序**序列（公式求值的唯一输入）。

    为什么不直接传 DataFrame：公式引擎只关心"一维数组 + 几条元信息"，
    用 dataclass 表示能把引擎与数据库彻底解耦 —— 单测可以手搓序列（见
    `tests/test_formula.py`），下一轮界面也可以拿内存里的数据直接试算，
    不需要造一个临时库。

    Attributes:
        limit_up_days: 每日"是否涨停"（0/1），来自库里 `limit_up_pool`
            （行存在即为涨停；`high_days` 为空时也算涨停）。
        limit_up_cnt: 每日连板天数（0 表示非涨停），来自 `limit_up_pool.high_days`。
        extra: 扩展字段（**竞价字段的挂载点**，见 `EXTRA_FIELDS`）；键是规范字段名。
    """

    symbol: str
    name: str
    industry: str
    date: list[str]
    close: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    vol: np.ndarray
    amount: np.ndarray
    pre_close: np.ndarray
    limit_up_days: np.ndarray
    limit_up_cnt: np.ndarray
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把 list / pandas.Series 统一成 float64 一维数组（调用方少踩坑）。

        在这一步就校验维度，而不是等到求值：长度不一致是**数据问题**，
        越早暴露越好，而且错误信息能带上股票代码。
        """
        self.date = [str(d) for d in self.date]
        for name in (
            "close", "open", "high", "low", "vol", "amount", "pre_close",
            "limit_up_days", "limit_up_cnt",
        ):
            arr = np.asarray(getattr(self, name), dtype="float64")
            if arr.ndim != 1:
                raise FormulaDataError(
                    f"{self.symbol} 的 {name} 必须是**一维**时间序列，现在是 {arr.ndim} 维"
                )
            setattr(self, name, arr)
        if not isinstance(self.extra, dict):
            raise FormulaDataError("Series.extra 必须是 规范字段名 → 一维数组 的字典")
        # 「排除 ST / 科创板 / 北交所」这三个标记**不用调用方准备**：它们完全由
        # `symbol` 与 `name` 推出来（这两个本来就在 Series 上），所以在这里补齐 ——
        # 任何入口（试算 / 选股 / 成绩单）都直接可用，而且**不需要联网**。
        # 之所以做成"自动补齐"而不是"让调用方塞"：漏塞一次，用户写好的
        # `ST=0` 就会报"本地数据里没有字段 ST 的数据"——那是最莫名其妙的失败方式。
        n = len(self.date)
        for field_name, flag in zip(
            FLAG_FIELDS,
            (is_st_name(self.name), is_star_market(self.symbol), is_bse(self.symbol),
             is_sh(self.symbol), is_sz(self.symbol), is_chinext(self.symbol)),
            strict=True,
        ):
            if field_name not in self.extra:
                self.extra[field_name] = np.full(n, 1.0 if flag else 0.0)


def load_series(
    db_path: str | Path,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    extra: dict[str, dict[str, float]] | None = None,
    hot_industries: dict[str, int] | None = None,
    *,
    today: str | None = None,
    live_bars: dict[str, LiveBar] | None = None,
    kline_day: str | None = None,
) -> Iterator[Series]:
    """从本地库逐只产出 `Series`（**只读、不联网**；扩展字段由调用方给）。

    只读三处：`stock_daily_hfq`（后复权视图）、`stock_basic`（名称/行业）、
    `limit_up_pool`（涨停与连板）。**一个字都不写库**。

    为什么"逐只 yield"而不是一次查全表：10 年全市场约 1000 万行，
    一次性读进内存要几百 MB～1GB，而公式是**逐只算**的。逐只查询正好让
    内存占用保持在"一只股票"的量级（`(symbol, date)` 是主键，单只查询走索引）。

    Args:
        db_path: 本地 SQLite 路径。
        symbols: 只取这些代码；None = 库里全部（按代码升序）。
        start: 只要 >= 该日期（"2020-01-01"）的数据。
        extra: `{代码: {扩展字段: 值}}` —— 目前用于 `流通市值` / `换手率`
            （它们只有"今天"这一个值，见 `EXTRA_FIELDS`）。这里只把"某一个数"
            铺成一条序列：**最后一个位置**是今天、其余是 NaN。
            `ST` / `科创` / `北交所` 三个标记不用给（`Series` 自己会补齐）。
        hot_industries: `{行业名: 最近 N 天上榜次数}`（见 `formulas.hot_industry_counts`）。
            给了就给每只票填 `热门行业`（按它的 `stock_basic.industry` 查表），
            **同样是"只有今天这一个值"**（前面填 NaN）—— 它表达的是"当前状态"，
            不是一条历史序列；写成 `REF(热门行业,5)` 是没意义的（会得到缺值）。
        today: 「今天」的日期（`YYYY-MM-DD`）。给了就会在每只票的日线后面**接上今天
            这一根**（用 `live_bars` 里的实时快照拼），于是 `C` / `C/REF(C,1)-1` /
            `量比()` 全部变成盘中口径 —— 这是"开盘时间里跑的选股都是实时的，不是开盘
            时间才采用 K 线"那条规矩的实现点（见 `formulas.resolve_caliber`）。
            None（默认）= 完全按库里的日线走，与没有这个参数时**逐字一致**。
        live_bars: `{代码: LiveBar}`（实时快照的原始值）。缺某只票就不给它接今天这一根，
            它照旧按日线算。
        kline_day: 库里最新的行情日（`formulas.latest_trading_day`）。**必须给**：
            只有"最后一根正好是它"的票才接今天这一根 —— 否则停牌/缺天的票会靠
            "接一根"混进今天的选股，而且涨跌幅是假的（推导见 `_live_arrays`）。

    Yields:
        Series（时间升序）。没有数据的代码会被跳过。

    Raises:
        FormulaDataError: 库文件不存在。
    """
    path = Path(db_path)
    if not path.exists():
        raise FormulaDataError(f"本地数据库不存在：{path}（请先在界面点【下载数据】）")

    # 直接用 sqlite3：本模块不想依赖 DataEngine（那会把"读法"绑死在一处），
    # 表名复用 engine 的常量，保证与策略/回测读的是同一张后复权视图。
    conn = sqlite3.connect(str(path), timeout=60)
    try:
        if symbols is None:
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {HFQ_TABLE} ORDER BY symbol"  # noqa: S608
            ).fetchall()
            wanted = [r[0] for r in rows]
        else:
            wanted = [str(s) for s in symbols]

        for symbol in wanted:
            sql = (
                f"SELECT date, open, high, low, close, volume, turnover FROM {HFQ_TABLE} "  # noqa: S608
                "WHERE symbol = ?"
            )
            params: list[Any] = [symbol]
            if start:
                sql += " AND date >= ?"
                params.append(start)
            sql += " ORDER BY date"
            rows = conn.execute(sql, tuple(params)).fetchall()
            if not rows:
                continue

            basic = conn.execute(
                "SELECT name, industry FROM stock_basic WHERE symbol = ?", (symbol,)
            ).fetchone()
            name = (basic[0] if basic and basic[0] else symbol)
            industry = (basic[1] if basic and basic[1] else "")

            pool = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT date, high_days FROM limit_up_pool WHERE symbol = ?", (symbol,)
                ).fetchall()
            }

            dates = [str(r[0]) for r in rows]
            close = np.array([_num_or_nan(r[4]) for r in rows], dtype="float64")
            open_arr = np.array([_num_or_nan(r[1]) for r in rows], dtype="float64")
            high_arr = np.array([_num_or_nan(r[2]) for r in rows], dtype="float64")
            low_arr = np.array([_num_or_nan(r[3]) for r in rows], dtype="float64")
            vol_arr = np.array([_num_or_nan(r[5]) for r in rows], dtype="float64")
            amount_arr = np.array([_num_or_nan(r[6]) for r in rows], dtype="float64")
            limit_days = np.array(
                [1.0 if d in pool else 0.0 for d in dates], dtype="float64"
            )
            limit_cnt = np.array(
                [_board_count(pool.get(d)) if d in pool else 0.0 for d in dates],
                dtype="float64",
            )
            pre_close = np.full(len(dates), np.nan, dtype="float64")
            if len(dates) > 1:
                # 后复权口径下"昨收"就是昨日的后复权收盘价，直接平移一根即可
                pre_close[1:] = close[:-1]

            # 盘中实时口径：把"今天"这一根接上去（`today` 为 None 时这里整段是空操作，
            # 与没有这个功能时**逐字一致**）。
            # 接在扩展字段之前是**故意的**：扩展字段的语义是"只有最后那一根有值"，
            # 它按 `len(dates)` 铺；先铺后接的话那个值会落到"昨天"上 ——
            # 于是 `现价>MA(C,5)` 里的现价指的是昨天的价，而界面上完全看不出来。
            bar = (live_bars or {}).get(symbol)
            if today and bar is not None:
                patched = _live_arrays(
                    bar, symbol, name, dates, close, open_arr, high_arr, low_arr,
                    vol_arr, amount_arr, limit_days, limit_cnt,
                    today=today, kline_day=kline_day,
                )
                if patched is not None:
                    (dates, close, open_arr, high_arr, low_arr, vol_arr, amount_arr,
                     limit_days, limit_cnt) = patched
                    # 接上今天这一根之后，"昨天"跟着往后挪了一格：`pre_close` 也要补一格，
                    # 否则最后一根的"昨收"是 NaN（`REF(C,1)` 之外再用到昨收的都会缺值）
                    pre_close = np.append(pre_close, float(close[-2]))

            # 扩展字段：把"只有今天这一个数"的值铺成一条序列（末尾是今天、前面 NaN）。
            # 为什么这样铺：公式只看**最后一根** K 线选股，所以末尾那个值就是答案；
            # 前面填 NaN 而不是"用今天的值倒推"，是为了让"历史上根本没有这个数"
            # 这件事在序列里如实体现 —— 谁写了 `MA(流通市值,5)` 就会得到全 NaN
            # （不产生信号），而不是一条看起来很合理的假均线。
            fields = dict((extra or {}).get(symbol) or {})
            if hot_industries is not None:
                # 「热门行业」= 最近 N 天该行业上过几次热门榜（按票的行业查表）
                fields.setdefault("热门行业", float(hot_industries.get(industry, 0)))
            series_extra = {
                key: np.concatenate([
                    np.full(len(dates) - 1, np.nan, dtype="float64"),
                    np.array([float(value)], dtype="float64"),
                ])
                for key, value in fields.items()
                if value is not None
            }

            yield Series(
                symbol=symbol,
                name=name,
                industry=industry,
                extra=series_extra,
                date=dates,
                close=close,
                open=open_arr,
                high=high_arr,
                low=low_arr,
                vol=vol_arr,
                amount=amount_arr,
                pre_close=pre_close,
                limit_up_days=limit_days,
                limit_up_cnt=limit_cnt,
            )
    finally:
        conn.close()


def _num_or_nan(value: Any) -> float:
    return float("nan") if value is None else float(value)


def _board_count(high_days: Any) -> float:
    """涨停池里的连板数：`high_days` 为空时按 1 板算（它毕竟在涨停池里）。"""
    if high_days is None:
        return 1.0
    try:
        value = float(high_days)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0 else 1.0


# ══════════════════════════════════════════════════════════════════════════
# 公式对象 + 编译入口
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Formula:
    """编译好的公式。**不可变**，可以跨线程复用（每次 eval 都是独立上下文）。"""

    text: str
    statements: tuple[_Statement, ...]
    condition: Any
    fields: tuple[str, ...]
    functions: tuple[str, ...]
    outputs: tuple[str, ...]
    min_history: int
    name: str = ""
    description: str = ""
    source_path: str | None = None
    #: 编译时的"已知忽略项"（通达信兼容层）：画图语句、样式修饰。
    #: 界面【校验】把它们列出来，用户才知道"那句画线没生效"，而不是以为整条公式都跑了。
    notes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """展示名（没起名字时退回公式本身，界面不会显示空白）。"""
        return self.name or self.text.strip().splitlines()[-1].strip()

    def eval(self, series: Series) -> np.ndarray:
        """对单只股票的升序序列求值，返回同长度的 bool 数组。

        True = **当日收盘后选中**（与项目现有口径一致：策略用最后一根 K 线选股）。
        """
        try:
            return _Evaluator(series).run(self)
        except (FormulaError, FormulaDataError):
            raise
        except Exception as exc:      # noqa: BLE001 - 分发产品：宁可给中文错误也不要崩栈
            # 兜底：真的出了意料之外的 numpy/内部异常，也要变成可读的中文错误
            raise FormulaError(
                f"策略求值失败（{type(exc).__name__}: {exc}）", code="eval",
                hint="请检查策略里用到的字段与函数；数据不足的股票会被跳过",
            ) from exc

    def describe(self) -> str:
        """一句话说明这条公式用了什么（界面"这条公式用了什么"直接用）。"""
        parts = []
        if self.fields:
            parts.append("字段：" + "、".join(self.fields))
        if self.functions:
            parts.append("函数：" + "、".join(self.functions))
        parts.append(f"至少需要 {self.min_history} 根 K 线")
        return "；".join(parts)


def _semicolons_to_newlines(text: str) -> str:
    """把**引号外**的分号换成换行（通达信公式几乎每行都以 `;` 结尾）。

    为什么要做这一步：通达信、同花顺、以及网上抄来的公式，语句分隔符就是 `;`，
    甚至一整条公式可以写在一行里（`A:=1; B:=2; A>B;`）。我们内部只按换行分语句，
    所以在这里先归一化 —— 之后解析器一行一句的规则一条都不用改。
    引号内的分号（`INDUSTRY="银行;保险"` 这种）保持原样，不能动。
    代价：`;` 后面的内容会被算到下一行，报错行号对"分号在同一行"的写法会偏，
    但这对"分号结尾"的通行写法是**更准**的（内容本来就在下一行）。
    """
    if ";" not in text:
        return text
    out: list[str] = []
    quote = ""
    for ch in text:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        out.append("\n" if ch == ";" else ch)
    return "".join(out)

def compile_formula(
    text: str,
    *,
    name: str = "",
    description: str = "",
    source_path: str | None = None,
) -> Formula:
    """解析并校验公式。

    Args:
        text: 公式正文（多行，最后一行是选股条件）。
        name / description / source_path: 元信息（从公式文件加载时填，界面上显示）。

    Returns:
        Formula：可直接 `eval(series)`。

    Raises:
        FormulaError: 任何语法/类型/越界问题，**消息是中文且带行列号**，
            结构化字段见 `FormulaError`（界面用它定位光标）。

    Example:
        >>> f = compile_formula("M5:=MA(C,5)\\nC>M5 AND V>MA(V,5)*1.5")
        >>> f.fields, f.functions
        (('C', 'VOL'), ('MA',))
    """
    if not isinstance(text, str):
        raise FormulaError("策略必须是文本", code="type")
    # Windows 记事本存的文件带 BOM、换行是 \r\n —— 都要先归一化，否则第一行会莫名其妙报错
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cleaned = _semicolons_to_newlines(cleaned)     # 通达信写法：分号 = 语句分隔
    if len(cleaned) > MAX_FORMULA_CHARS:
        raise FormulaError(
            f"策略太长了（{len(cleaned)} 个字符，上限 {MAX_FORMULA_CHARS}）",
            code="too_long", hint="拆成几条策略，或者用 `:=` 减少重复书写",
        )

    parser = _Parser(cleaned)
    statements, condition = parser.parse()
    formula = Formula(
        text=cleaned,
        statements=tuple(statements),
        condition=condition,
        fields=tuple(parser.fields),
        functions=tuple(parser.funcs),
        outputs=tuple(parser.outputs),
        min_history=parser.min_history,
        name=name,
        description=description,
        source_path=source_path,
        notes=tuple(parser.notes),
    )
    logger.debug(
        f"策略编译通过：字段 {formula.fields}，函数 {formula.functions}，"
        f"{len(formula.statements)} 条语句"
    )
    return formula


# ══════════════════════════════════════════════════════════════════════════
# 公式文件加载（供"随包分发的预置公式"用）
# ══════════════════════════════════════════════════════════════════════════

#: 认的扩展名。`.tvf` = 通达信公式文件（用户从别处导出的公式常是这个后缀）
FORMULA_SUFFIXES = (".txt", ".tvf")

#: 注释头里认的键（中英文都认，用户从别处抄来的文件常常是英文键）
_NAME_KEYS = ("名称", "名字", "name", "title")
_DESC_KEYS = ("说明", "描述", "备注", "desc", "description", "note")


@dataclass(frozen=True)
class FormulaSpec:
    """一条"公式文件"的解析结果。

    为什么把错误**放进结果**而不是抛出去：一个目录里往往有十几条公式，
    其中一条写错不该让整个列表消失（下一轮的界面要一次显示全部，
    并且把出错的那条标红）。所以这里逐文件报错，`formula is None` + `error` 有值。
    """

    name: str
    description: str
    path: str
    source: str
    formula: Formula | None = None
    error: FormulaError | None = None

    @property
    def ok(self) -> bool:
        return self.formula is not None

    @property
    def error_text(self) -> str:
        return "" if self.error is None else str(self.error)


def _parse_formula_file(text: str) -> tuple[str, str, str]:
    """拆出 (名称, 说明, 公式正文)。

    注释头 = 文件**开头**连续的 `#` 行（可以夹空行）。支持全角冒号 ——
    中文用户用输入法打字时 `：` 是默认输出，为此报"格式错误"没有道理。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    name = ""
    desc_parts: list[str] = []
    body_start = 0
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            body_start = index + 1
            continue
        if not stripped.startswith("#"):
            body_start = index
            break
        header = stripped.lstrip("#").strip()
        body_start = index + 1
        if not header:
            continue
        key, sep, value = header.partition(":")
        if not sep:
            key, sep, value = header.partition("：")     # 全角冒号
        if not sep:
            desc_parts.append(header)
            continue
        key = key.strip().lower()
        value = value.strip()
        if key in _NAME_KEYS:
            name = value
        elif key in _DESC_KEYS:
            desc_parts.append(value)
        else:
            desc_parts.append(f"{key}: {value}" if value else key)
    body = "\n".join(lines[body_start:]).strip("\n")
    return name, " ".join(p for p in desc_parts if p), body


def load_formula_files(directory: str | Path) -> list[FormulaSpec]:
    """加载目录下的公式文件（`.txt` / `.tvf`，UTF-8）。

    约定：文件开头的 `#` 行是注释头，认 `# 名称: xxx` 与 `# 说明: xxx`；
    其余部分是公式体（最后一行必须是选股条件）。

    - 目录不存在 → 返回空列表（**不抛异常**：界面上没这个目录很正常）；
    - 单个文件语法错 → 只有它自己 `ok=False`，其余照常；
    - 文件名排序保证结果稳定（界面列表不会每次刷新都换顺序）。

    Args:
        directory: 公式目录。

    Returns:
        FormulaSpec 列表（含失败的条目，`ok=False`、`error_text` 是中文原因）。
    """
    folder = Path(directory)
    if not folder.is_dir():
        logger.debug(f"策略目录不存在：{folder}")
        return []

    paths = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in FORMULA_SUFFIXES),
        key=lambda p: p.name.lower(),
    )
    specs: list[FormulaSpec] = []
    for path in paths:
        # 用 utf-8-sig 读：Windows 记事本另存为 UTF-8 会带 BOM，带 BOM 时
        # 第一行开头会多一个不可见字符，导致"第一条公式永远报错"这种诡异现象
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            specs.append(FormulaSpec(
                name=path.stem, description="", path=str(path), source="",
                error=FormulaError(
                    f"文件不是 UTF-8 编码（{exc.reason}）", code="encoding",
                    hint="用记事本另存为 UTF-8 编码后重试",
                ),
            ))
            continue
        except OSError as exc:
            specs.append(FormulaSpec(
                name=path.stem, description="", path=str(path), source="",
                error=FormulaError(f"文件读不出来：{exc}", code="io"),
            ))
            continue

        name, description, body = _parse_formula_file(raw)
        name = name or path.stem
        try:
            formula = compile_formula(
                body, name=name, description=description, source_path=str(path),
            )
        except FormulaError as exc:
            # 逐文件报错：一条公式写错不影响别的公式被加载进来
            logger.warning(f"策略文件 {path.name} 解析失败：{exc}")
            specs.append(FormulaSpec(
                name=name, description=description, path=str(path), source=body,
                error=exc,
            ))
            continue
        specs.append(FormulaSpec(
            name=name, description=description, path=str(path), source=body,
            formula=formula,
        ))
    good = sum(1 for s in specs if s.ok)
    logger.info(f"策略目录 {folder}：载入 {good}/{len(specs)} 条策略")
    return specs


__all__ = [
    "EXTRA_FIELDS",
    "FIELD_ALIASES",
    "FUNCTIONS",
    "MAX_FORMULA_CHARS",
    "MAX_FORMULA_LINES",
    "MAX_WINDOW",
    "SUPPORTED_FUNCTIONS",
    "engine_stamp",
    "Formula",
    "FormulaDataError",
    "FormulaError",
    "FormulaSpec",
    "Series",
    "compile_formula",
    "load_formula_files",
    "load_series",
]
