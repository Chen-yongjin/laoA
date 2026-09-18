"""「策略选股」页：**统一策略列表 + 傻瓜式公式编辑器 + 开始选股**。

用户原话
--------
> 「策略选股 -策略编辑（可直接兼容通达信成品公式）
>   点击打开策略编辑器(傻瓜式编辑器左右界面，左边编辑框，右边变量+运算符)
>   编辑完成命名(可添加备注)一下可以保存到公式列表。
>   -策略列表 （名称-备注-状态）
>   点击打开公式详情，可修改保存。右键菜单删除和启用/关闭。
>   -开始选股(选好的股发送消息，直接添加自选。)」

这一页就按这三块摆（`docs/改版方案.md` TAB 4）：

1. **策略列表**（上半）：**内置 5 条策略与自定义公式合成同一张表**，列固定为
   `名称 | 备注 | 状态`。内置在前、公式在后；备注列写的是**真实证据** ——
   内置取自 `strategy/rules.py` 的 `evidence`/`evidence_note` 与 `strategy/groups.py`
   的组结论 / `disabled_reason`，公式取自文件里的 `# 说明:` 注释头。
   **界面自己一个数字都不编**（见 `builtin_strategy_note`：它只搬字段，不做加法）。
   单击一行：内置 → 只读详情（条件说明 + 证据 + 当前状态，可复制）；
   公式 → 载入编辑器（可改可存）。右键：启用/关闭、删除（内置不可删）。
2. **策略编辑器**（下半，点【策略编辑】或点列表里的公式行才展开）：左边框、
   右边"点一下就插入"的按钮面板（变量 / 函数 / 运算符 / 排除 —— 排除组在**最下面**）。
   每个按钮的字是中文（点一下插入的仍是引擎认的语法），中文 tooltip 第一步写着"插入 XX"，
   `MA` 自动带括号、光标落在括号里，`AND` 自动补空格 —— 这些交互是**实测过的**，
   见下面 `insert_token` / `insert_operator` 的注释（别回退）。
   底部【校验】【运行】【导出选股结果】【保存】【另存为】【删除】，名称旁边多了**备注**
   （写进文件的 `# 说明:` 注释头）。

   **【运行】= 原来的【试算】**（用户 2026-09-18 要求"把试算直接改成运行"）：
   同一件事 —— 在当前库上跑一遍这条公式、只看每只票最后一根 K 线、不推送不写库。
   只改了界面上的两个字（用户说"试算"看不懂），**内部标识符仍叫 `preview_*`**
   （`btn_preview` / `on_preview` / `preview_worker` / `formulas.preview_hits`）：
   那些名字连着一批测试与文档里的引用，为一个文案做机械重命名只会制造噪音。
   看到 `preview` 就当"这条按钮"读。

   【导出选股结果】把**上一次【运行】的命中清单**（全量，不截断）写成桌面上的
   `老A选股助手-选股结果-<日期>.txt` —— 与 `scheduler.run_daily()` 建池时导出的
   是**同一个函数**（`pool.export_pick_file`），版式、来源列、现价列完全一致。
3. **【开始选股】**（右上角）：本页**只 emit `start_pick_requested()`** ——
   增量数据 → 跑策略与公式 → 建池 → 推送这一整套在主窗口（`ui/app.py`）里，
   这一页不碰它（也不联网）。

选股结果去哪了（用户 2026-09-17 的明确要求）
--------------------------------------------
用户原话：「策略选股只要显示策略，不显示选股结果，选股结果直接进自选股池，
可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」

所以这一页**只有策略列表**（`名称 | 备注 | 状态` + 【策略编辑】【开始选股】）：
旧版那块「本次选股结果」表 + 一句话结论 + 【全部加为自选】按钮**已经删掉**。
结果有**两个**去处在界面上看得见，一个都不会丢：

* **自选股池页**（主入口）：选出来的票就是 `stock_pool` 里的行，
  右键能【删除】、上面能【添加自选】—— 增删都在那一页做（用户原话"可以在股池再添加删除"）；
  这也是为什么这一页**不需要**再摆一张"结果表"：同一批票在一张表里看就够了。
* **桌面文件**：`scheduler.run_daily()` 在建池成功后调 `pool.export_pick_file()`，
  往桌面写一份 `老A选股助手-选股结果-<日期>.txt`（导出失败只记日志、不影响选股）。

`show_pick_result()` 这个方法**保留**（主窗口还在调它）：它现在是空实现，
只写日志、不画任何东西 —— 删掉它会让 `ui/app.py` 那边 `AttributeError`
（`app.py` 属于另一个改动方，这一页不替它做决定）。

为什么这里**没有**「成绩单」
----------------------------
旧版右下角有两个按钮（【看成绩单】【复制成绩单】）。改版把它们**从界面移除**：
数据只有 6 个月（`history_years = 0.5`），而成绩单自己有 250 个交易日的样本门槛
（`research/scorecard.py`）—— 放在界面上永远只会显示"样本不足，无法评估"，
点一次要扫全库几十秒。**能力没有删除**：`formulas.run_scorecard()` 保留，
CLI `--scorecard` 照旧（数据下到 ≥1 年时它才有意义）。

为什么单独一个模块而不是塞进 `ui/app.py`
--------------------------------------
`app.py` 已经 4300 行。这一页有 40 多个控件与自己的后台线程，塞进去会让
"主窗口接线"与"公式编辑"两件事互相干扰；分开之后，这一页可以用 Qt 的 offscreen
平台插件**单独**建出来测（`tests/test_formula_page.py` / `test_formula_lib.py`），
不用把整个主窗口拉起来。主窗口那边只留 `addTab` 与信号接线。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from laoa_trader import formulas as formulas_lib
from laoa_trader import pool as pool_mod
from laoa_trader.config import get_config
from laoa_trader.log import get_logger
from laoa_trader.strategy import base as base_mod
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group, groups
from laoa_trader.strategy import rules as rules_mod

logger = get_logger(__name__)

try:  # Qt 缺失时不应该 import 就炸（与 ui/app.py 同一个约定）
    from PySide6.QtCore import QEvent, Qt, QThread, Signal
    from PySide6.QtGui import QFont, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 与 ui/app.py 同一个降级策略
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


#: 右侧点选面板的固定宽度。为什么要"固定"：这一列的宽度会不停变化的话，
#: 按钮文字会被压成 `M A (`，而按钮本身是这一页的核心交互 —— 宁可让左边的编辑区窄一点。
PANEL_WIDTH = 300

#: 右侧按钮的高度（像素）。默认高度（约 26~30）下，四组 52 个按钮要把面板滚很久 ——
#: 用户 2026-09-18 要求"把按键大小都缩小"，于是高度降一档（30→22）、字号降一档。
BUTTON_HEIGHT = 22
#: 右侧那批按钮的 objectName（主题 QSS 用它单独给这批按钮设 padding / min-height，
#: 否则主题里通用的 `QPushButton { padding: 4px 12px; min-height: 20px }` 会把
#: `setFixedHeight(22)` 顶回 30px —— 实测过，别删）
PALETTE_BUTTON_OBJECT = "paletteButton"
#: 按钮字号相对默认字号降几档。只降一档是**故意的**：再小就费眼睛了
#: （这批按钮是给"刚上手的人"看的，宁可按钮矮一点、字要看得清）
BUTTON_FONT_DELTA = 1
#: 字号下限（再小就看不清了；某些平台默认字号本身就只有 8~9）
BUTTON_FONT_MIN = 7

#: 编辑区的最小高度（再小就看不见"最后一行是选股条件"这件事了）
EDITOR_MIN_HEIGHT = 130

#: `Tab` 插入的空格数。**不跳焦点**：写公式时按 Tab 是想缩进，
#: 跳到下一个控件会让用户正在打的字跑到别处（也被原始需求点到了）。
TAB_SPACES = "    "

#: 【运行】结果**显示**最多列几只（与 `formulas.PREVIEW_LIMIT` 一致；
#: 导出的桌面文件是**全量**，不按这个数截断）
PREVIEW_LIMIT = formulas_lib.PREVIEW_LIMIT

#: 提示区最多显示多少行（多行错误/很长的命中清单全塞进 QLabel 会把列表挤没；
#: 完整文本留在 `self.hint_text`，鼠标选中就能复制）
HINT_MAX_LINES = 14

#: 收尾时等后台线程真正退出的上限（毫秒）。见 `FormulaPage._finish_preview`：
#: 信号是排队投递的，在 `run()` 返回**之前**就可能已经到主线程了，
#: 这时候放掉最后一个引用会让 QThread 在"线程还在跑"时析构 —— Qt 直接崩进程。
THREAD_JOIN_MS = 3_000

# ── 统一策略列表的三类行 ──
ROW_BUILTIN = "builtin"      # 内置策略（写在 `strategy/rules.py` 里，只读、不可删）
ROW_FORMULA = "formula"      # 自定义公式（`formulas/` 目录里的文件，可改可删）
#: 「竞价策略」：**不是一个选股策略**，而是把设置页那个「竞价扫描」搬进这张表 ——
#: 勾上 = 每个交易日 9:20 / 9:25 各扫一次全市场并把强势股推给你（写回 `intraday_auction`）。
#: 用户 2026-09-18 的要求原话："不是公式，是把「竞价扫描」做成策略"。
#: 为什么它必须与选股策略**分开一类**：勾它不会往池子里加票、也不改变选股结果，
#: 混在一起会让"勾上 = 参与选股"这条规矩出现例外，而例外是用户最容易记错的东西。
ROW_AUCTION = "auction"
#: 这一行的 key（唯一；内置策略的 key 是类名，公式的 key 是公式名）
AUCTION_KEY = "auction"
AUCTION_NAME = "竞价策略"

#: 列表列头（用户给定，**一个字都不加**）
LIST_COLUMNS: tuple[str, ...] = ("名称", "备注", "状态")

#: 右键菜单项文案
MENU_ENABLE = "启用"
MENU_DISABLE = "关闭"
MENU_DELETE = "删除"
#: 内置策略的「删除」是**置灰**而不是藏起来 —— 藏起来用户会以为"程序坏了"，
#: 写上"不可删"他立刻明白为什么（见 `FormulaPage.row_menu`）。
MENU_DELETE_BUILTIN = "删除（内置策略不可删）"
#: 「竞价策略」那一行的删除项文案（它同样不可删，理由是"它是设置项，不是文件"）
MENU_DELETE_AUCTION = "删除（竞价策略是内置设置，不可删）"

#: `config.toml` 里"关掉全部策略"的写法（`groups.resolve()` 认得它 → `explicit_off`）。
#: 为什么必须有一个明确值：**两个键都空 = 全选**是安全默认，
#: 所以"所有勾都取消"不能用空列表表达，否则会反过来变成"全跑"。
OFF_GROUP_KEY = "none"

#: 备注（写进 `# 说明:` 注释头）的长度上限。为什么这里要拦：
#: 注释头是**一行**，超长的说明会把文件第一屏占满，用户用记事本打开公式时
#: 得先翻过大段文字才看得见公式本身（同一个上限库里有 `MAX_DESC_CHARS`）。
MAX_NOTE_CHARS = formulas_lib.MAX_DESC_CHARS

#: 顶部那行灰字说明（用户打开这一页先看到的东西）。
#: **必须写明结果去哪了**：这一页不再显示选股结果（用户要求），
#: 不写的话用户点完【开始选股】会以为"什么都没发生"。
PAGE_HINT = (
    "勾「状态」列 = 这条策略/公式参与选股（只有「竞价策略」那一行例外：它开关的是盘中"
    "竞价扫描，不参与选股）；单击一行看详情（公式会载入编辑器）；"
    "右键 = 启用 / 关闭 / 删除；点【开始选股】跑一轮 —— "
    "选出的票直接进「自选股池」（在那里增删），并同时导出一份到桌面的文本文件。"
)

#: 策略列表上方那句灰字
LIST_HINT = (
    "策略列表：内置 5 条固定在前（备注列是它的实测证据，只读）；"
    "中间那条「竞价策略」开关的是盘中竞价扫描（只做提示、不参与选股）；"
    "你自己的公式追加在后（备注来自公式文件的「# 说明:」）。"
)

#: 编辑器里那行灰字说明（小白第一眼看的就是它）
EDITOR_HINT = (
    "点右边的按钮就能插入；最后一行是选股条件。"
    "写完点【校验】→【运行】→【保存】；想留一份结果就点【导出选股结果】。"
)

#: 【载入示例】在没有示例文件时用的兜底公式（保证"小白第一步"一定走得通）
SAMPLE_NAME = "放量上攻"
SAMPLE_TEXT = "M5:=MA(C,5)\nV5:=MA(V,5)\nC>M5 AND C>O AND V>V5*1.5"

# ── 右侧面板的四个分组（变量 / 函数 / 运算符 / 排除）──
#
# 每一项：(插入的文本, tooltip, 参数个数, 按钮上的字)
#   * tooltip 一律写"是什么 + 一个例子"—— 小白不需要知道"简单移动平均"的定义，
#     他需要的是"我该写成什么样"；**tooltip 一律以插入的语法开头**（`MA(X,N) …`），
#     因为按钮上写的是中文，语法得在别处看得到（见下一条）；
#   * 第 4 项 = **按钮上的字**：用户 2026-09-18 要求"按键字符都翻译成中文，可以快速上手"
#     —— 按钮写「收盘价」「均线」「并且」，点一下插进去的仍是引擎认的 `C` / `MA(` / `AND`。
#     **为什么按钮字与插入文本必须分开**：公式语言是给引擎读的（ASCII），
#     而按钮是给刚上手的人读的 —— 让他先认识"成交量""上穿""并且"这些词，
#     点完看一眼编辑框，两边就自然对上了（tooltip 第一步就写着插入的是什么）。
#   * 第 3 项 = `None` → **原样插入**（变量、短运算符）；
#     `0`   → 插入 `NAME()` 并把光标放在括号**后面**（`连板()`、`涨停天数()` 这类
#             不带参数的函数，用户接着打 `>=2` 就行）；
#     `>0`  → 插入 `NAME()` 并把光标放在括号**里面**（接着打字就是第一个参数）。
# 为什么用 `None` 而不是"0 就不插括号"：0 和"无参函数"是两件事 ——
# 混成一个值的话 `连板` 会插成光秃秃的 `连板`（引擎会报"函数后面要跟括号"）。
VARIABLES: tuple[tuple[str, str, int | None, str], ...] = (
    ("C", "插入 C（收盘价，当天最后一笔成交价）。例：C>MA(C,5) 表示收盘站上 5 日线",
     None, "收盘价"),
    ("O", "插入 O（开盘价）。例：C>O 表示今天是阳线（收盘高于开盘）", None, "开盘价"),
    ("H", "插入 H（最高价）。例：H>REF(H,1) 表示今天创了新高", None, "最高价"),
    ("L", "插入 L（最低价）。例：L<REF(L,1) 表示今天创了新低", None, "最低价"),
    ("V", "插入 V（成交量，单位股）。例：V>MA(V,5)*1.5 表示比 5 日均量放大 1.5 倍",
     None, "成交量"),
    ("AMO", "插入 AMO（成交额，单位元）。例：AMO>100000000 表示当天成交额过亿",
     None, "成交额"),
    ("PRE", "插入 PRE（昨收价）。例：C/PRE>1.05 表示今天涨了 5% 以上", None, "昨收价"),
    ("INDUSTRY", "插入 INDUSTRY（所属行业名，要加引号）。例：INDUSTRY=\"半导体\"",
     None, "行业"),
    # ── 市值 / 换手：来自**实时快照**（日线里没有这两个数），所以只有"今天"这一个值。
    #    选股本来就看最后一根 K 线，直接写比较就行；取不到快照时条件不成立（不出票）。
    ("流通市值", "插入 流通市值（亿元，来自实时快照）。"
                "例：流通市值>=10 AND 流通市值<=300", None, "流通市值"),
    ("换手率", "插入 换手率（%，来自实时快照）。例：换手率>5", None, "换手率"),
    # ── 离线算出来的字段：不用联网、历史上也成立
    ("热门行业", "插入 热门行业：最近 3 个交易日里这只票的行业上过几次热门榜（0~3，"
                "口径与「大盘概览 → 热门板块」同一套）。例：热门行业>=1 表示只选上过榜的行业",
     None, "热门行业"),
)

FUNCTIONS: tuple[tuple[str, str, int | None, str], ...] = (
    ("MA", "插入 MA(X,N)：求 X 的 N 日平均值。例：MA(C,5) 是 5 日均价", 2, "均线"),
    ("EMA", "插入 EMA(X,N)：指数平均（越近的日子越重要）。例：EMA(C,12)", 2, "指数均线"),
    ("REF", "插入 REF(X,N)：引用 N 天前的值。例：REF(C,1) 是昨天收盘价", 2, "几天前的值"),
    ("HHV", "插入 HHV(X,N)：近 N 日的最高值。例：HHV(H,20) 是 20 日新高价", 2, "区间最高"),
    ("LLV", "插入 LLV(X,N)：近 N 日的最低值。例：LLV(L,20) 是 20 日最低价", 2, "区间最低"),
    ("SUM", "插入 SUM(X,N)：近 N 日的合计。例：SUM(V,5) 是近 5 日成交量合计", 2, "求和"),
    ("COUNT", "插入 COUNT(条件,N)：近 N 日里条件成立几次。例：COUNT(C>O,10)", 2, "数次数"),
    ("CROSS", "插入 CROSS(A,B)：金叉（昨天 A 不大于 B、今天 A 大于 B）。"
              "例：CROSS(C,MA(C,5))", 2, "上穿"),
    ("ABS", "插入 ABS(X)：绝对值。例：ABS(C-PRE) 是今天的涨跌金额", 1, "绝对值"),
    ("MAX", "插入 MAX(A,B)：取大的那个。例：MAX(C,O)", 2, "取大的"),
    ("MIN", "插入 MIN(A,B)：取小的那个。例：MIN(C,O)", 2, "取小的"),
    ("IF", "插入 IF(条件,A,B)：条件成立取 A、否则取 B。例：IF(C>O,C,O)", 3, "二选一"),
    ("STD", "插入 STD(X,N)：近 N 日的标准差（波动有多大）。例：STD(C,20)", 2, "波动大小"),
    ("BARSLAST", "插入 BARSLAST(条件)：距离上次条件成立过了几天。"
                 "例：BARSLAST(连板()>0)", 1, "距上次几天"),
    ("涨停天数", "插入 涨停天数()：今天是否涨停；涨停天数(10) 表示近 10 日涨停几次。"
                 "⚠️ 依赖本地涨停池，早期日期会读到 0", 0, "涨停天数"),
    ("连板", "插入 连板()：今天几连板（0 = 没涨停）。例：连板()>=2。"
             "⚠️ 依赖本地涨停池，早期日期会读到 0", 0, "连板"),
    ("量比", "插入 量比()：当天成交额 ÷ 前 5 日均额；也可以写 量比(10)。例：量比()>2",
     0, "量比"),
    ("DIF", "插入 DIF()：MACD 快线 = EMA(C,12)-EMA(C,26)。"
            "例：DIF()>DEA() 是金叉状态（要 26 根 K 线）", 0, "MACD快线"),
    ("DEA", "插入 DEA()：MACD 慢线 = EMA(DIF,9)。例：DIF()>DEA()（要 35 根 K 线）",
     0, "MACD慢线"),
    ("MACD", "插入 MACD()：柱状线 = (DIF-DEA)×2，正数是红柱。例：MACD()>0（要 35 根 K 线）",
     0, "MACD柱"),
)

#: 「排除」组：**按钮上写的是人话（非 X），点一下插入的是条件（`X=0`）**。
#: 2026-09-18（用户要求）：原来的 `ST` / `科创` / `北交所` 三个按钮"意思不明"——
#: 看到 `ST` 不知道点下去是要它还是不要它。现在按钮直接写"非 ST"，点一下插入 `ST=0`，
#: 一眼明白；同时补上「非沪市 / 非深市 / 非创业板」。
#:
#: 元素是四元组 `(插入的文本, 提示, 参数个数, 按钮上的字)`：前三个与其它组同形，
#: 第四个只有这一组用（其余组的按钮字就是插入的文本本身）。
EXCLUDES: tuple[tuple[str, str, int | None, str], ...] = (
    ("ST=0", "非 ST：排除 ST / *ST 等风险警示股。插入 ST=0", None, "非 ST"),
    ("北交所=0", "非北交所：排除北交所（4/8/92 号段）。插入 北交所=0", None, "非北交所"),
    ("科创=0", "非科创板：排除科创板（688/689）。插入 科创=0", None, "非科创板"),
    ("沪市=0", "非沪市：排除沪市（6 开头，含科创板与沪 B）。插入 沪市=0", None, "非沪市"),
    ("深市=0", "非深市：排除深市（0/3 开头，含创业板与深 B）。插入 深市=0", None, "非深市"),
    ("创业板=0", "非创业板：排除创业板（300/301）。插入 创业板=0", None, "非创业板"),
)

OPERATORS: tuple[tuple[str, str, int | None, str], ...] = (
    ("+", "插入 +（加）。例：MA(C,5)+MA(C,10)", None, "加"),
    ("-", "插入 -（减）。例：C-PRE 是今天的涨跌金额", None, "减"),
    ("*", "插入 *（乘）。例：V*1.5 表示按 1.5 倍算", None, "乘"),
    ("/", "插入 /（除）。例：AMO/100000000 是成交额（亿元）", None, "除"),
    (">", "插入 >（大于）。例：C>MA(C,5)", None, "大于"),
    ("<", "插入 <（小于）。例：C<MA(C,5)", None, "小于"),
    (">=", "插入 >=（大于等于）。例：C>=PRE*1.05", None, "大于等于"),
    ("<=", "插入 <=（小于等于）。例：C<=PRE*0.95", None, "小于等于"),
    ("=", "插入 =（等于，是**比较**不是赋值；赋值用 :=）。例：INDUSTRY=\"银行\"",
     None, "等于"),
    ("!=", "插入 !=（不等于）。例：INDUSTRY!=\"银行\"", None, "不等于"),
    ("AND", "插入 AND（并且）：两边都要成立（自动补空格）。"
            "例：C>MA(C,5) AND V>MA(V,5)", None, "并且"),
    ("OR", "插入 OR（或者）：任一边成立即可（自动补空格）。"
           "例：涨停天数(10)>=1 OR 连板()>=2", None, "或者"),
    ("NOT", "插入 NOT（取反，自动补空格）。"
            "例：NOT(C>MA(C,5)) 表示收盘没站上 5 日线", None, "取反"),
    ("(", "插入 (（左括号）：改变运算顺序。例：(C+O)/2 是当天中间价", None, "左括号"),
    (")", "插入 )（右括号）：与左括号配对", None, "右括号"),
)

#: 需要"前后补空格"的运算符（否则 `A>1` 之后点 AND 会粘成 `A>1AND B`）。
#: 为什么只对这三个补：`+`/`>` 这类短符号粘在一起不影响可读性，
#: 而 `AND`/`OR`/`NOT` 是**字母单词**，粘上就是"另一个词"，编辑器里一眼看不出来。
_SPACED_OPERATORS = ("AND", "OR", "NOT")


# ══════════════════════════════════════════════════════════════════════════
# 统一策略列表的"行"与它的备注/详情（**纯数据**，不依赖 Qt）
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class StrategyRow:
    """统一列表里的一行：内置策略与自定义公式**共用**同一个结构。

    为什么要合成一种行：列表要"合成同一张表"（用户给定），而两类行的交互又不同
    （内置只读、公式可改；内置不可删）。用 `kind` 区分、把差别写进数据里，
    表格与右键菜单就都只认这一种结构，不用到处 `isinstance`。
    """

    #: `ROW_BUILTIN` / `ROW_FORMULA`
    kind: str
    #: 内置 = 策略类名（`LowPriceStrategy`）；公式 = 公式名（= 文件名）
    key: str
    #: 展示名（内置 = 中文名）
    name: str
    #: 「备注」列的文本
    note: str
    #: 「备注」列的 tooltip（完整证据/错误全文）
    note_tip: str
    #: 是否参与选股（内置查 `enabled_groups`/`enabled_strategies`，公式查 `enabled_formulas`；
    #: 竞价策略查 `intraday_auction` —— 它不是选股策略，这条注释只说明"状态从哪来"）
    enabled: bool
    #: 内置策略的只读详情（条件说明 + 证据 + 当前状态）；公式行为空串
    detail: str = ""
    #: 公式行对应的 `FormulaSpec`；内置行为 None
    spec: Any = None

    @property
    def is_builtin(self) -> bool:
        return self.kind == ROW_BUILTIN

    @property
    def is_auction(self) -> bool:
        """是不是「竞价策略」那一行（内置的竞价扫描开关，**不参与选股**）。"""
        return self.kind == ROW_AUCTION

    @property
    def read_only(self) -> bool:
        """不是公式文件（内置策略 / 竞价策略）：单击只展开只读详情、不能删、不能改。"""
        return self.kind in (ROW_BUILTIN, ROW_AUCTION)


def builtin_order() -> list[str]:
    """内置策略的展示顺序：按组（超短 → 短线 → 波段），未归组的排在最后。

    与 `rules.run_all()` 的遍历顺序同一口径 —— 界面上看到的先后，
    就是"实际跑的先后 + 池子权重"的先后，用户不用记两套顺序。
    """
    order = list(groups.all_strategies())
    order += [name for name in rules_mod.STRATEGIES if name not in order]
    return order


def builtin_enabled(cfg: Any) -> set[str]:
    """当前**真正参与选股**的内置策略名。

    直接问 `groups.resolve_from_config()`，**不另算一套**：
    配置里 `enabled_groups` 与 `enabled_strategies` 谁空谁不空、两者取交集的规则，
    只有 `resolve()` 一个地方说得清；界面自己推一遍必然漂移
    （表现就是"列表里勾着、实际却没跑"，这种 bug 用户根本猜不到原因）。
    """
    return set(groups.resolve_from_config(cfg).strategies)


def _group_of_builtin(class_name: str) -> Any:
    key = groups.group_of(class_name)
    return groups.GROUPS.get(key) if key else None


def _group_note_tail(group: Any) -> str:
    """取组结论里"数字那一段"（`…：T+2~T+3 t≈1.7~2.1` → `T+2~T+3 t≈1.7~2.1`）。

    只做拆分、不做改写：组结论的写法是 `成员：结论`，逐条内置策略的备注里
    重复一遍成员名没有意义（那一行本来就写着是哪条策略）。
    """
    note = str(getattr(group, "note", "") or "")
    if not note:
        return ""
    for sep in ("：", ":"):
        if sep in note:
            return note.split(sep, 1)[1].strip()
    return note.strip()


def builtin_strategy_note(class_name: str) -> str:
    """内置策略「备注」列的那句话 —— **只搬真实字段，一个字都不编**。

    取材顺序（都是代码里已有的结论，界面不做任何"计算"）：

    1. 组**默认关闭**且写了原因 → `⛔ 默认关闭：<disabled_reason>`
       （例如"两套口径显著为负：B 口径 −1.21%(t=−5.36)…"）；
    2. 策略自己写了 `evidence_note`（正 α 只在"开盘买"口径存在的那两条）→ `⚠️ <note>`
       （含两套口径的真实数字）；
    3. 其余 = `evidence == EVIDENCE_PROVEN`，也就是 `base.py` 里写明的
       "两套执行口径（D+1 开盘买 / D+1 尾盘买）都为正" → 写 `两套口径都为正`，
       再把**所属组**的实测区间跟在后面，并标明它是**组**的数字（不是这条策略自己的）：
       `两套口径都为正 · 组实测 T+2~T+3 t≈1.7~2.1`。

    为什么第 3 条要写"组实测"三个字：逐条策略的历史数字代码里**没有**
    （只有组结论），把组的区间写成这条策略的成绩就是编数据。宁可多三个字。
    """
    cls = rules_mod.STRATEGIES.get(class_name)
    if cls is None:
        return ""
    group = _group_of_builtin(class_name)

    if group is not None and not group.enabled_by_default and group.disabled_reason:
        return "⛔ 默认关闭：" + group.disabled_reason

    note = str(getattr(cls, "evidence_note", "") or "").strip()
    if note:
        return "⚠️ " + note

    if str(getattr(cls, "evidence", "") or "") == base_mod.EVIDENCE_PROVEN:
        tail = _group_note_tail(group)
        return "两套口径都为正" + (f" · 组实测 {tail}" if tail else "")
    # 证据类型认不出来（将来新增取值）：**宁可什么都不说**，也不要猜
    return ""


def builtin_strategy_tip(class_name: str) -> str:
    """内置策略「备注」列的 tooltip：把来源与出处写清楚（用户不用翻代码/文档）。"""
    cls = rules_mod.STRATEGIES.get(class_name)
    group = _group_of_builtin(class_name)
    lines = [f"{rules_mod.strategy_label(class_name)}（{class_name}）"]
    if group is not None:
        weight = groups.STRATEGY_WEIGHTS.get(class_name, 0)
        lines.append(f"所属组：{group.label}（{group.key}）· T+{group.horizon} · 池子权重 {weight}")
        if group.note:
            lines.append("组结论：" + group.note)
        if group.disabled_reason:
            lines.append("⛔ 默认关闭：" + group.disabled_reason)
    note = str(getattr(cls, "evidence_note", "") or "") if cls is not None else ""
    if note:
        lines.append("证据：" + note)
    lines.append(
        "备注来自 strategy/rules.py 与 strategy/groups.py 里的字段（界面不另算数字）；"
        "内置策略只能启用/关闭，不能改也不能删"
    )
    return "\n".join(lines)


def builtin_strategy_detail(class_name: str, cfg: Any) -> str:
    """内置策略的**只读详情**（单击一行时展开）：条件说明 + 证据 + 当前状态。

    条件说明直接用策略类自己的 docstring（`strategy/rules.py` 里那份"规则：…"），
    **不在界面里重写一遍** —— 重写就会与代码漂移，而漂移之后的说明比没有说明更糟。
    """
    cls = rules_mod.STRATEGIES.get(class_name)
    group = _group_of_builtin(class_name)
    weight = groups.STRATEGY_WEIGHTS.get(class_name, 0)
    enabled = class_name in builtin_enabled(cfg)
    lines = [f"{rules_mod.strategy_label(class_name)}（{class_name}）"]
    if group is not None:
        lines.append(
            f"所属组：{group.label}（{group.key}）· 目标持有期 T+{group.horizon} · 池子权重 {weight}"
        )
    lines.append("当前状态：" + ("✅ 参与选股" if enabled else "☐ 未参与选股"))
    lines.append(
        "状态由 config.toml 的 enabled_groups / enabled_strategies 决定"
        f"（现在：enabled_groups={_cfg_list(cfg, 'enabled_groups')}、"
        f"enabled_strategies={_cfg_list(cfg, 'enabled_strategies')}）"
    )
    lines.append("")
    lines.append("── 条件说明（写在 strategy/rules.py 里，只读）──")
    doc = inspect.getdoc(cls) if cls is not None else None
    lines.append(doc or "（这条策略没有写说明）")
    lines.append("")
    lines.append("── 证据 ──")
    note = builtin_strategy_note(class_name)
    lines.append(note or "（这条策略没有写证据字段）")
    if group is not None and group.note:
        # 组结论单独一行：它是**这一组**的实测数字，与上面那条策略自己的证据不是一回事
        # （停用理由已经包含在 `builtin_strategy_note()` 里，这里不重复贴第二遍）
        lines.append("组结论：" + group.note)
    lines.append("")
    lines.append("内置策略写在代码里：只能启用/关闭，不能修改、也不能删除。")
    lines.append("想按自己的条件来，就照它写一条公式：【策略编辑】→ 写 →【保存】。")
    return "\n".join(lines)


def _cfg_list(cfg: Any, key: str) -> str:
    """配置里的列表按 **TOML 的写法**显示（`["short", "swing"]`）。

    为什么不写成 `[short、swing]` 那种好看的中文列表：这几句话的用途就是"你去看
    config.toml 的那一行"，写成能**照着抄**的形式最省事（用户改配置时不用想"那个顿号
    到底是什么"）。
    """
    raw = [str(v) for v in (getattr(cfg, key, None) or [])]
    return "[" + ", ".join(f'"{value}"' for value in raw) + "]"


# ── 「竞价策略」这一行（把设置页的「竞价扫描」搬进策略列表）──────────────
#
# 用户 2026-09-18 的要求："不是公式，是把「竞价扫描」做成策略"。
# 所以这一行**不是一个选股策略**：勾上它 = 开启竞价扫描（9:20 / 09:25 各扫一次全市场、
# 按下面这套口径打分推送），写回的是 `config.toml` 的 `intraday_auction`，
# 与 `enabled_groups` / `enabled_strategies` / `enabled_formulas` 都不相干。
#
# 口径里的数字**全部现读 `cfg`**（设置页那几个框写进去的就是它们），界面自己一个都不编：
# 下面那几个默认值只在 `cfg` 是测试替身、没有这些字段时兜底，与 `config.py` 的出厂值一致。

def auction_enabled(cfg: Any) -> bool:
    """竞价扫描开着没有（= 这一行的「状态」列）。"""
    return bool(getattr(cfg, "intraday_auction", False))


def _auction_settings(cfg: Any) -> dict[str, Any]:
    """这一行要显示的几个数字（读不到就给 `config.py` 的出厂值）。"""
    amount = float(getattr(cfg, "auction_min_amount", 5e6) or 0)
    return {
        # 扫描时刻：默认两个（09:20 盘中、09:25 竞价终态）
        "at": [str(t) for t in (getattr(cfg, "auction_scan_at", None) or ["09:20", "09:25"])],
        "min_pct": float(getattr(cfg, "auction_min_pct", 2.0) or 0.0),
        "max_pct": float(getattr(cfg, "auction_max_pct", 9.0) or 0.0),
        "ratio": float(getattr(cfg, "auction_min_volume_ratio", 2.0) or 0.0),
        "amount_wan": amount / 1e4,
        "score": int(getattr(cfg, "auction_min_score", 2) or 0),
        "items": int(getattr(cfg, "auction_alert_max_items", 10) or 0),
        "boards": [str(b) for b in (getattr(cfg, "auction_boards", None) or [])],
    }


def _auction_boards_text(cfg: Any) -> str:
    """板块那一条：全部勾着就写「全部板块」，否则把中文名列出来。

    中文名从 `config.AUCTION_BOARD_LABELS` 取（**推送、设置页、这里共用同一份**）——
    界面自己再抄一份中文名，迟早会出现"设置页写创业板、这里写创业"这种对不上的事。
    """
    from laoa_trader import config as config_mod

    on = set(_auction_settings(cfg)["boards"])
    labels = [label for key, label in config_mod.AUCTION_BOARD_LABELS.items() if key in on]
    if labels and len(labels) == len(config_mod.AUCTION_BOARD_LABELS):
        return "全部板块"
    return "、".join(labels) if labels else "（一个板块都没勾，扫不到票）"


def _auction_criteria(cfg: Any) -> list[str]:
    """口径那几行（行 tooltip 与详情共用，写一份免得两处说法不一致）。"""
    s = _auction_settings(cfg)
    return [
        "扫描时刻：" + " / ".join(s["at"]) + "（每天各一次，扫全市场）",
        f"竞价涨幅：{s['min_pct']:.1f}% ~ {s['max_pct']:.1f}%"
        "（≥上限的多半接近涨停，买不进，直接过滤）",
        f"竞价成交额：≥ {s['amount_wan']:.0f} 万",
        f"竞价量比：≥ {s['ratio']:.1f}（拿打分里「放量」那 2 分）",
        "板块：" + _auction_boards_text(cfg),
        f"打分门槛：≥ {s['score']}（满分 6 —— 涨幅 2 + 量比 2 + 买盘剩余 1 + 成交额 1），"
        f"一次最多推 {s['items']} 只",
    ]


def auction_note(cfg: Any) -> str:
    """「备注」列那句话：**先说清它不参与选股**，再说口径。

    为什么把"不参与选股"放在最前面：备注列是 Stretch 的，窗口一窄就会被省略号截掉 ——
    而这半句正是这一行最要紧的东西（用户看到"竞价策略"四个字，第一反应必然是
    "它也会给我选股吗"）。把结论写在能被看见的位置，比藏在末尾强。
    """
    s = _auction_settings(cfg)
    return (
        "只做盘中提示、不参与选股 ｜ "
        f"{' / '.join(s['at'])} 扫全市场：涨幅 {s['min_pct']:.1f}~{s['max_pct']:.1f}%、"
        f"量比≥{s['ratio']:.1f}、成交额≥{s['amount_wan']:.0f}万、打分≥{s['score']}"
        f" → 推前 {s['items']} 只"
    )


def auction_note_tip(cfg: Any) -> str:
    """「备注」列的 tooltip：口径全文 + 两条硬限制（不参与选股 / 无法回测）。"""
    lines = [
        f"{AUCTION_NAME}（{AUCTION_KEY}）：它不是选股策略，而是「系统设置 → 竞价扫描」"
        "那个功能的开关。",
        "",
        "每个交易日按下面这套口径扫全市场（约 5600 只，100 只一批，后台线程跑 20~30 秒）：",
    ]
    lines += ["· " + item for item in _auction_criteria(cfg)]
    lines += [
        "",
        "命中会进提醒（浮窗 + 托盘闪烁 + 提示音），9:25 那次推一条汇总；",
        "全部命中落库（`auction_scan` 表），详情弹窗里能看全市场结果。",
        "",
        "⚠️ 它**不参与选股**：勾上它不会往「自选股池」里加票，也不改变选股结果。",
        "⚠️ 竞价数据**没有历史**（接口只给当天），所以它**无法回测** —— "
        "当成「当日走势的早期提示」，别当买入信号。",
        "",
        "参数在「系统设置 → 竞价扫描」那一组里改（勾选状态与这里同步）。",
    ]
    return "\n".join(lines)


def auction_detail(cfg: Any) -> str:
    """单击这一行时的**只读详情**（口径 + 当前状态 + 去哪改参数）。"""
    enabled = auction_enabled(cfg)
    lines = [
        f"{AUCTION_NAME}（{AUCTION_KEY}）—— 内置的竞价扫描开关，只读",
        "",
        "当前状态：" + ("✅ 已开启（每天到点自动扫全市场）" if enabled else "☐ 未开启"),
        "写回的配置键：intraday_auction（勾「状态」列或右键【启用】即写回 config.toml）",
        "",
        "── 口径（数字就是设置页那几个框，界面不另算）──",
    ]
    lines += ["· " + item for item in _auction_criteria(cfg)]
    lines += [
        "",
        "── 它是什么、不是什么 ──",
        "· 是：**盘中提示**。9:15–9:25 的真实买卖盘是全市场唯一能看到「今天谁在抢」的数据；",
        "  9:25 那一枪拿到的是**竞价终态**，命中直接推到浮窗/托盘（点详情看全部命中）。",
        "· 不是：**选股策略**。它**不参与选股** —— 勾上不会往「自选股池」加票，"
        "也不会改变【开始选股】的结果；",
        "  要按自己的条件选股，用上面那 5 条内置策略或你自己写的公式。",
        "",
        "── 两条硬限制 ──",
        "1. 竞价数据**没有历史**（接口只给当天 stage=live/final，不接受日期）→ **无法回测**，",
        "   想验证只能实盘跑一段时间记录；",
        "2. 竞价指标与「当日后期涨停」的相关性只有 2 倍随机（本项目实测样本），",
        "   覆盖率低 —— 所以它只够当提示，不够当选股信号。",
        "",
        "参数怎么改：设置页「竞价扫描」那一组（涨幅上下限、成交额、量比、板块、打分、条数、扫描时刻）。",
    ]
    return "\n".join(lines)


def auction_row(cfg: Any) -> StrategyRow:
    """「竞价策略」那一行的数据（内置在前、公式在后，它排在两者之间）。"""
    return StrategyRow(
        kind=ROW_AUCTION,
        key=AUCTION_KEY,
        name=AUCTION_NAME,
        note=auction_note(cfg),
        note_tip=auction_note_tip(cfg),
        enabled=auction_enabled(cfg),
        detail=auction_detail(cfg),
    )


def formula_row_note(spec: Any, runtime_error: str = "") -> tuple[str, str]:
    """公式行「备注」列的 (文本, tooltip)。

    备注 = 公式文件里的 `# 说明:`（保存时界面写进去的），再叠加两类**必须让人看见**的问题：

    * 语法错（`⛔ 语法错：…`）—— 它勾上也跑不了（`formulas.enabled_names()` 会跳过它），
      所以不能只藏在提示里；完整错误（带行号列号）进 tooltip；
    * 上一次运行的运行期错误（`⚠️ 运行时出错：…`，`formula_group.last_status()`）。

    为什么把这两条放「备注」列而不是「状态」列：用户给定的列头只有
    `名称 | 备注 | 状态`，而「状态」列被"参与选股"这个勾占满了（一格一义），
    把错误塞进同一格会让"这一格到底能不能点"变得要猜。
    """
    note = str(getattr(spec, "description", "") or "").strip()
    tip = note
    if not spec.ok:
        full = str(spec.error_text or "").strip()
        note = (note + " ｜" if note else "") + "⛔ 语法错：" + _one_line(full, 60)
        tip = (tip + "\n\n" if tip else "") + "⛔ 这条公式现在编译不过：\n" + full
    if runtime_error:
        note = (note + " ｜" if note else "") + "⚠️ 运行时出错：" + _one_line(runtime_error, 40)
        tip = (tip + "\n\n" if tip else "") + "⚠️ 上一次选股时出错：\n" + str(runtime_error)
    return (note or "—"), tip


def build_strategy_rows(
    cfg: Any, specs: Sequence[Any], runtime: dict[str, str] | None = None
) -> list[StrategyRow]:
    """把「内置 5 条 + 目录里的公式」拼成统一列表的数据（内置在前，公式在后）。

    读取的**全是已有来源**：状态来自 `groups.resolve_from_config()` 与
    `cfg.enabled_formulas`，备注来自 `rules`/`groups` 的字段与公式文件的注释头。
    """
    runtime = runtime or {}
    enabled_builtin = builtin_enabled(cfg)
    known_formulas = set(str(n) for n in (getattr(cfg, "enabled_formulas", None) or []))

    rows: list[StrategyRow] = []
    for class_name in builtin_order():
        rows.append(
            StrategyRow(
                kind=ROW_BUILTIN,
                key=class_name,
                name=rules_mod.strategy_label(class_name),
                note=builtin_strategy_note(class_name) or "—",
                note_tip=builtin_strategy_tip(class_name),
                enabled=class_name in enabled_builtin,
                detail=builtin_strategy_detail(class_name, cfg),
            )
        )
    # 「竞价策略」排在**内置策略之后、公式之前**：它不是选股策略（勾它不往池子加票），
    # 但它是内置的（不可改不可删），所以不该混进"你自己的公式"那一段里（用户 2026-09-18 要求）。
    rows.append(auction_row(cfg))
    for spec in specs:
        note, tip = formula_row_note(spec, runtime.get(spec.name, ""))
        rows.append(
            StrategyRow(
                kind=ROW_FORMULA,
                key=spec.name,
                name=spec.name,
                note=note,
                note_tip=tip,
                enabled=spec.name in known_formulas,
                spec=spec,
            )
        )
    return rows


# ══════════════════════════════════════════════════════════════════════════
# 后台线程
# ══════════════════════════════════════════════════════════════════════════

if QT_AVAILABLE:

    class FormulaWorker(QThread):
        """这一页的后台线程（**【运行】用它**）。

        为什么必须后台跑：运行要逐只票读 K 线并在最后一根上跑公式（真实 3 年库实测
        3.7 秒，全市场 5000+ 只要 7~8 秒）。在主线程里跑就是"窗口未响应"——用户以为
        程序死了，其实是它正在算。这里是 Qt 里唯一安全的做法：工作线程只算数，
        结果通过信号回主线程再碰控件。

        `failed` 递的是**异常对象**而不是一句话：两种失败在界面上的说法不同
        （`FormulaDataError` = 数据问题，该去下载数据；其它 = 程序问题），
        工作线程不该替界面决定措辞 —— 主线程拿到类型才分得清
        （见 `FormulaPage._on_preview_failed`）。

        （旧版这里还兼跑"成绩单"，改版把成绩单的界面入口去掉了，
        这个线程只服务【运行】；`formulas.run_scorecard()` 仍在，CLI 照用。）
        """

        progress = Signal(str, int, int)
        finished_ok = Signal(object)
        failed = Signal(object)

        def __init__(self, fn: Callable[..., Any], *args: Any,
                     with_progress: bool = False, **kwargs: Any) -> None:
            super().__init__()
            self._fn = fn
            self._args = args
            self._kwargs = kwargs
            self._with_progress = with_progress

        def run(self) -> None:  # noqa: D102 - QThread 约定
            try:
                kwargs = dict(self._kwargs)
                if self._with_progress:
                    kwargs["progress_cb"] = self.progress.emit
                result = self._fn(*self._args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 后台异常必须回主线程说人话
                logger.exception("公式后台任务失败")
                self.failed.emit(exc)
            else:
                self.finished_ok.emit(result)


@dataclass
class RowMenu:
    """一行的右键菜单（**把菜单与两个动作一起递出来**，方便测试与接线）。

    `menu` 是真正要 `exec()` 的东西；`toggle`/`delete` 是那两个动作。
    单独包一层的原因：测试要在**不弹菜单**的前提下验证"这一行右键能做什么"
    （`exec()` 会阻塞事件循环，离屏测试里没人去点它）。
    """

    menu: Any
    toggle: Any
    delete: Any


if QT_AVAILABLE:

    class FormulaPage(QWidget):
        """「策略选股」页。

        属性里刻意留着测试与主窗口要用的引用（`name_edit` / `note_edit` / `editor` /
        `hint_label` / `table` / `rows` / `palette_buttons` / `preview_worker`），
        不要去爬控件层级 —— 这一页的控件多，按层级取值的测试一改布局就集体失效。
        """

        #: 【开始选股】被点 → **只举手**，真正的流程在主窗口（见 `on_start_pick`）
        start_pick_requested = Signal()

        def __init__(
            self,
            cfg: Any = None,
            parent: Any = None,
            *,
            status_cb: Callable[[str], None] | None = None,
            directory: Any = None,
        ) -> None:
            """建页。

            Args:
                cfg: 配置对象（默认全局单例）；公式目录、`enabled_*`、`watchlist_max`、
                    `db_path` 都读它。
                status_cb: 给主窗口显示一句话（标题区的运行状态），不传就只写日志。
                directory: 公式目录（默认 `formulas_lib.formula_dir()`；测试传 tmp_path）。
            """
            super().__init__(parent)
            self.cfg = cfg if cfg is not None else get_config()
            self.status_cb = status_cb
            self.directory = directory

            #: 目录里的公式（`FormulaSpec` 列表，顺序 = 文件名顺序）
            self.specs: list[Any] = []
            #: 统一列表的行（内置 + 公式，与表格行号一一对应）
            self.rows: list[StrategyRow] = []
            #: 右侧面板按钮：{token: QPushButton}（测试按 token 点，不爬布局）
            self.palette_buttons: dict[str, Any] = {}
            #: 【运行】（原【试算】，下同）的后台线程；跑完置回 None
            #: （测试就等这一条来判断"落地了"）
            self.preview_worker: FormulaWorker | None = None
            #: 【运行】按下按钮那一刻的公式快照（结果属于它，不属于编辑框里现在的内容）
            self.preview_formula: Any = None
            #: **上一次【运行】的结果**（【导出选股结果】导的就是它）：
            #: `{"name": 公式名, "date": 行情日, "hits": [{"symbol","name"}...], "count": N}`
            #: 或 None（还没跑过 / 跑失败了）。
            #:
            #: 为什么导出要用"上一次的结果"而不是重新跑一遍：重跑可能因为盘中快照、
            #: 数据更新而给出**与用户刚才看到的那份不一样**的清单 —— 他导出的必须是
            #: 他看过的。另外重跑一次要扫全库（实测 7~8 秒），点一下按钮等 8 秒不像话。
            #: 每次开始新一轮【运行】时先清空它：**绝不导出上一轮的陈结果**。
            self.last_run: dict | None = None
            self.hint_text: str = ""
            #: 内置策略行的勾选框（键 = 类名）与公式行的勾选框（键 = 公式名）。
            #: **两个字典**而不是一个：公式名与类名理论上可能撞（用户可以把公式
            #: 命名成 `LowPriceStrategy`），撞了之后"写失败要退回哪个勾"就会错。
            self._builtin_boxes: dict[str, Any] = {}
            self._row_boxes: dict[str, Any] = {}
            #: 「竞价策略」那一行的勾选框（只有一行，所以不放进上面两个字典）
            self._auction_box: Any = None
            #: 载入行时别把"选中变化"当成用户点击，也别让刷列表打开编辑器
            self._loading = False
            #: 当前展开的内置策略详情（刷列表后要跟着更新）
            self._detail_key = ""

            self._build_ui()
            self.reload()

        # ── 界面搭建 ──────────────────────────────────────────────────

        def _build_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 12)
            layout.setSpacing(8)

            # ── 顶部一行：【策略编辑】【开始选股】 + 一句灰字说明 ──
            #
            # 为什么操作按钮在这一页而不是标题区：改版方案的原则是"标题区不再放操作按钮，
            # 数据在「系统设置」、选股在「策略选股」" —— 点下去会发生什么，在这一页看得见。
            top = QHBoxLayout()
            self.btn_edit = QPushButton("策略编辑")
            self.btn_edit.setToolTip(
                "打开公式编辑器：左边写公式、右边点按钮插入（点列表里的公式行也会打开它）"
            )
            # 用 lambda 吞掉 `clicked` 带来的 checked 参数：直接接 `on_open_editor`
            # 的话那个 `False` 会被当成 spec 传进去（Qt 的经典坑，本文件里所有
            # 带参数的槽都这么接）
            self.btn_edit.clicked.connect(lambda _checked=False: self.on_open_editor())
            top.addWidget(self.btn_edit)

            self.btn_start_pick = QPushButton("开始选股")
            self.btn_start_pick.setObjectName("primaryAction")   # 主操作按钮（主题精确命中）
            self.btn_start_pick.setToolTip(
                "按上面勾选的策略与公式跑一轮：结果直接进「自选股池」"
                "（在那一页右键删除、或手工再添加），并同时往桌面导出一个结果文本文件，"
                "最后按「系统设置」里的通知方式发一条消息"
            )
            self.btn_start_pick.clicked.connect(self.on_start_pick)
            top.addWidget(self.btn_start_pick)

            self.page_hint = QLabel(PAGE_HINT)
            self.page_hint.setObjectName("statusTag")      # 小号灰字（与状态区同一档）
            self.page_hint.setWordWrap(True)
            top.addWidget(self.page_hint, 1)
            layout.addLayout(top)

            splitter = QSplitter(Qt.Orientation.Vertical)
            splitter.setChildrenCollapsible(False)
            splitter.addWidget(self._build_list_side())
            splitter.addWidget(self._build_bottom_stack())
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 1)
            self.splitter = splitter
            layout.addWidget(splitter, 1)

            # ── 提示区（多行、可复制）──
            #
            # 为什么放在**页面最下面**（而不是编辑器里）：校验/运行/保存/勾选的消息
            # 都要看得见 —— 用户把编辑器收起来之后，消息还留在屏幕上；
            # 而"提示区在编辑器里"时，收起来就什么都看不到了（用户会以为没反应）。
            self.hint_label = QLabel("")
            self.hint_label.setWordWrap(True)
            self.hint_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            self.hint_label.setMinimumHeight(40)
            self.hint_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(self.hint_label)

            # 编辑器 / 内置详情默认都收起来：用户给定的是"点击打开策略编辑器"
            self.bottom_stack.setVisible(False)

        def _build_list_side(self) -> Any:
            """上半：**只有策略列表**（名称 | 备注 | 状态）。

            为什么这里只剩一张表（用户原话："策略选股只要显示策略，不显示选股结果"）：
            旧版下半还挂着一块「本次选股结果」表 + 一句话结论 + 【全部加为自选】，
            而同一批票本来就有一张更该看的表 —— 「自选股池」页（结果就在那里，
            还能右键删、手工加）。摆两块显示同一批票只会让用户不确定"哪一块说了算"。
            """
            side = QWidget()
            layout = QVBoxLayout(side)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            self.list_hint = QLabel(LIST_HINT)
            self.list_hint.setObjectName("statusTag")
            self.list_hint.setWordWrap(True)
            layout.addWidget(self.list_hint)

            self.table = QTableWidget(0, len(LIST_COLUMNS))
            self.table.setHorizontalHeaderLabels(list(LIST_COLUMNS))
            self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self.on_context_menu)
            header = self.table.horizontalHeader()
            # 名称/状态窄一点（它们内容短），备注吃掉剩下的宽度（证据那句话最长）
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self._set_header_tooltip(0, "策略/公式的名字。内置策略的中文名来自 strategy/rules.py")
            self._set_header_tooltip(1, "证据或说明：内置策略写它的实测证据（代码里的字段，界面不编数字）；"
                                        "自定义公式写公式文件里的「# 说明:」")
            self._set_header_tooltip(2, "勾上 = 参与选股。内置策略写回 config.toml 的 "
                                        "enabled_groups + enabled_strategies；公式写回 enabled_formulas")
            self.table.itemSelectionChanged.connect(self.on_row_selected)
            layout.addWidget(self.table, 1)

            return side

        def _set_header_tooltip(self, column: int, text: str) -> None:
            """给表头写 tooltip（用户不用翻文档就知道这一列是什么口径）。"""
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(text)

        def _build_bottom_stack(self) -> Any:
            """下半：一叠两张 —— 公式编辑器 / 内置策略只读详情（同时只显示一张）。"""
            stack = QStackedWidget()
            stack.addWidget(self._build_editor_page())
            stack.addWidget(self._build_detail_page())
            self.bottom_stack = stack
            return stack

        def _build_editor_page(self) -> Any:
            """编辑器整页：一行说明 + 左右分栏（左边编辑、右边点选面板）。"""
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            head = QHBoxLayout()
            self.editor_hint = QLabel(EDITOR_HINT)
            self.editor_hint.setObjectName("statusTag")
            self.editor_hint.setWordWrap(True)
            head.addWidget(self.editor_hint, 1)
            self.btn_sample = QPushButton("载入示例")
            self.btn_sample.setToolTip("把一条能跑通的示例公式放进编辑框，照着改就行")
            self.btn_sample.clicked.connect(self.on_load_sample)
            head.addWidget(self.btn_sample)
            self.btn_close_editor = QPushButton("收起编辑器")
            self.btn_close_editor.setToolTip("收起这一块（公式已经保存的不会丢）")
            self.btn_close_editor.clicked.connect(self.on_close_panel)
            head.addWidget(self.btn_close_editor)
            layout.addLayout(head)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setChildrenCollapsible(False)
            splitter.addWidget(self._build_editor_side())
            splitter.addWidget(self._build_palette_side())
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 0)
            self.editor_splitter = splitter
            layout.addWidget(splitter, 1)
            self.editor_page = page
            return page

        def _build_detail_page(self) -> Any:
            """内置策略的只读详情页（**可选中复制** + 一个【复制】按钮）。"""
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            head = QHBoxLayout()
            self.detail_title = QLabel("")
            head.addWidget(self.detail_title, 1)
            self.btn_copy_detail = QPushButton("复制")
            self.btn_copy_detail.setToolTip("把这份详情复制到剪贴板（贴到记事本/群里都行）")
            self.btn_copy_detail.clicked.connect(self.on_copy_detail)
            head.addWidget(self.btn_copy_detail)
            self.btn_close_detail = QPushButton("关闭")
            self.btn_close_detail.setToolTip("收起详情")
            self.btn_close_detail.clicked.connect(self.on_close_panel)
            head.addWidget(self.btn_close_detail)
            layout.addLayout(head)

            # 只读 QPlainTextEdit 而不是 QLabel：内置策略的说明是**多段长文本**，
            # 只有文本控件才能整段选中复制（QLabel 只能一小段一小段选）
            self.detail_view = QPlainTextEdit()
            self.detail_view.setReadOnly(True)
            self.detail_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            layout.addWidget(self.detail_view, 1)
            self.detail_text = ""
            self.detail_page = page
            return page

        def _build_editor_side(self) -> Any:
            """左侧：名称 + 备注 + 编辑框 + 按钮行 + 进度条。"""
            side = QWidget()
            layout = QVBoxLayout(side)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)

            # ── 名称 + 备注 + 三个文件按钮 ──
            name_row = QHBoxLayout()
            name_row.addWidget(QLabel("公式名称："))
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("例如：5日线上放量")
            self.name_edit.setToolTip("这就是保存后的文件名，也是池子/推送里显示的名字")
            name_row.addWidget(self.name_edit, 1)

            self.btn_save = QPushButton("保存")
            self.btn_save.setObjectName("primaryAction")     # 主操作按钮（主题里精确命中）
            self.btn_save.setToolTip("把编辑框里的公式存到公式目录（重名会先问一句）")
            self.btn_save.clicked.connect(self.on_save)
            name_row.addWidget(self.btn_save)

            self.btn_save_as = QPushButton("另存为")
            self.btn_save_as.setToolTip("换一个名字再存一份（原来的那份不动）")
            self.btn_save_as.clicked.connect(self.on_save_as)
            name_row.addWidget(self.btn_save_as)

            self.btn_delete = QPushButton("删除")
            self.btn_delete.setToolTip("删掉当前这条公式文件（会先问一句；内置策略不能删）")
            self.btn_delete.clicked.connect(self.on_delete)
            name_row.addWidget(self.btn_delete)
            layout.addLayout(name_row)

            note_row = QHBoxLayout()
            note_row.addWidget(QLabel("备注："))
            self.note_edit = QLineEdit()
            self.note_edit.setPlaceholderText("这条公式是干什么的（可留空 —— 留空则自动填「用到的字段/函数」）")
            self.note_edit.setToolTip(
                "会写进公式文件的「# 说明:」注释头，列表的「备注」列显示的就是它；"
                f"最长 {MAX_NOTE_CHARS} 字，不能换行（注释头只有一行）"
            )
            note_row.addWidget(self.note_edit, 1)
            layout.addLayout(note_row)

            # ── 编辑框 ──
            self.editor = QPlainTextEdit()
            self.editor.setPlaceholderText(
                "在这里写公式，例如：\nM5:=MA(C,5)\nC>M5 AND V>MA(V,5)*1.5"
            )
            self.editor.setMinimumHeight(EDITOR_MIN_HEIGHT)
            self.editor.setTabChangesFocus(False)
            # 等宽字体：公式里的括号与逗号要能对齐，"哪一层括号"才看得出来
            font = QFont()
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setFamily("Consolas")
            font.setFixedPitch(True)
            self.editor.setFont(font)
            # Tab 插入空格而不是切焦点（见 `TAB_SPACES` 的说明）
            self.editor.installEventFilter(self)
            layout.addWidget(self.editor, 1)

            # ── 动作行 ──
            action_row = QHBoxLayout()
            self.btn_validate = QPushButton("校验")
            self.btn_validate.setToolTip("检查公式写得对不对；通过时会告诉你用到哪些字段/函数")
            self.btn_validate.clicked.connect(self.on_validate)
            action_row.addWidget(self.btn_validate)

            # 【运行】= 原来的【试算】（用户要求改这两个字："试算"看不懂）。
            # **属性名仍是 `btn_preview`**：它连着一批测试与文档里的引用，
            # 为一个文案做机械重命名只会制造噪音（见模块 docstring 的同一段说明）。
            self.btn_preview = QPushButton("运行：当前库能选出几只")
            self.btn_preview.setToolTip(
                "在当前库上跑一遍这条公式，看看最近一个交易日命中几只。\n"
                "不推送、不写库（结果不会进「自选股池」）"
            )
            self.btn_preview.clicked.connect(self.on_preview)
            action_row.addWidget(self.btn_preview)

            self.btn_export = QPushButton("导出选股结果")
            self.btn_export.setToolTip(
                "把上一次【运行】命中的全部股票写成一个文本文件，放在桌面上：\n"
                "老A选股助手-选股结果-<今天>.txt（同一天再导出会覆盖这一个文件）"
            )
            self.btn_export.clicked.connect(self.on_export)
            action_row.addWidget(self.btn_export)
            action_row.addStretch(1)
            layout.addLayout(action_row)

            # ── 进度条（只在运行时出现）──
            self.progress = QProgressBar()
            self.progress.setVisible(False)
            self.progress.setRange(0, 100)
            layout.addWidget(self.progress)
            return side

        def _build_palette_side(self) -> Any:
            """右侧：**点一下就输入**的四组按钮（变量 / 函数 / 运算符 / 排除）。

            用 `QGroupBox` 而不是 `QToolBox`：折叠起来之后，小白会以为"函数不见了"——
            四组一起看得见、函数多了就滚动，才是"所有东西都在右边"的本意。
            """
            panel = QWidget()
            panel.setFixedWidth(PANEL_WIDTH)
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.setSpacing(6)

            title = QLabel("点一下，就插到光标那里")
            title.setObjectName("statusTag")
            panel_layout.addWidget(title)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(0, 0, 0, 0)
            inner_layout.setSpacing(8)
            # 四组的**先后顺序**（用户 2026-09-18 要求"把排除区放在最下面"）：
            # 变量 / 函数 是写公式最常用的两块，运算符紧随其后，
            # 「排除」是选股末尾才补的那几个条件（不是每个人都用），所以排在最后。
            for name, items in (("变量", VARIABLES), ("函数", FUNCTIONS),
                                ("运算符", OPERATORS), ("排除", EXCLUDES)):
                inner_layout.addWidget(self._build_group(name, items))
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            panel_layout.addWidget(scroll, 1)
            self.palette_panel = panel
            return panel

        def _build_group(
            self,
            title: str,
            items: Sequence[
                tuple[str, str, int | None] | tuple[str, str, int | None, str]
            ],
        ) -> Any:
            """一组按钮（两列网格）。每个按钮一个中文 tooltip（是什么 + 一个例子）。

            元素是四元组 `(插入的文本, tooltip, 参数个数, 按钮上的字)`：前三项管
            "点下去发生什么"，第四项管"按钮上写什么"（中文），两者**故意分开** ——
            见上面 `VARIABLES` 的注释。

            按钮**做得比默认小**（用户 2026-09-18 要求"把按键大小都缩小"）：
            四组一共 52 个按钮，用默认高度时右侧面板要滚很久才看得全；
            字号也跟着降一档，中文两三个字在窄按钮里才不会被挤成 "…"。
            """
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setSpacing(3)
            grid.setContentsMargins(6, 4, 6, 4)
            for index, item in enumerate(items):
                token, tip, args = item[0], item[1], item[2]
                label = item[3] if len(item) > 3 else token
                button = QPushButton(label)
                button.setToolTip(tip)
                # objectName 让主题 QSS 能单独管这一批按钮（见 theme.py 的
                # `QPushButton#paletteButton`）：主题里 `QPushButton` 那条有
                # `padding: 4px 12px; min-height: 20px`，实测会把 setFixedHeight(22)
                # **顶回 30px** —— 只改代码不加这条 QSS 的话，"按钮缩小"只在没有样式表时生效
                button.setObjectName(PALETTE_BUTTON_OBJECT)
                button.setFixedHeight(BUTTON_HEIGHT)
                # 字号降一档（改的是**这个控件自己的**字体副本，不动全局主题字体）。
                # `pointSize() <= 0` 时说明字体是按像素设的 —— 那就不动它（免得算出负数）
                font = button.font()
                if font.pointSize() > 0:
                    font.setPointSize(max(BUTTON_FONT_MIN,
                                          font.pointSize() - BUTTON_FONT_DELTA))
                    button.setFont(font)
                elif font.pixelSize() > 0:
                    # 字号也可能按**像素**定义（那时 pointSize() 是 -1）：一样要降一档
                    font.setPixelSize(max(BUTTON_FONT_MIN,
                                          font.pixelSize() - BUTTON_FONT_DELTA))
                    button.setFont(font)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                # 运算符里的 `AND`/`OR`/`NOT` 走 `insert_operator`（要自动补空格），
                # 其余（变量、短运算符、函数）都走 `insert_token`。
                # 用默认参数把 token 绑进 lambda：循环变量在闭包里是**延迟求值**的，
                # 不绑的话所有按钮都会插入最后一个 token（经典坑）
                if args is None and token in _SPACED_OPERATORS:
                    button.clicked.connect(
                        lambda _checked=False, text=token: self.insert_operator(text)
                    )
                else:
                    button.clicked.connect(
                        lambda _checked=False, text=token, count=args: self.insert_token(
                            text, args=count
                        )
                    )
                grid.addWidget(button, index // 2, index % 2)
                self.palette_buttons[token] = button
            return box

        # ── 点选插入 ──────────────────────────────────────────────────

        def insert_token(self, token: str, args: int | None = None) -> None:
            """把 `token` 插到**当前光标处**（函数会带上 `()` 并把光标放进括号里）。

            `args=None` = 原样插入（变量、短运算符）；`args` 是数字 = 函数骨架，
            `0` 表示无参函数（光标落在 `)` 之后），`>0` 表示把光标放进括号里。

            **为什么必须在光标处插入，而不是追加到末尾**（这一页最容易做错的一处）：
            用户修改公式是"边想边改"的 —— 他点了【载入示例】或载入了一条已保存的公式，
            然后想改**中间那一行**（例如把 `MA(C,5)` 改成 `MA(C,10)`，或往第 2 行加个条件）。
            如果按钮把 token 追加到末尾，他会看到：公式语法通过、但选出来的票完全不对 ——
            而且**看不出哪里变了**（末尾多了一个 `AND ...`）。这种"静默改坏"比报错可怕得多。
            插入到光标处 + 焦点回到编辑框 + 光标停在括号里，才是"点一下就接着打字"的手感；
            追加到末尾则要用户自己再剪切粘贴一次，那这个面板就白做了。
            """
            cursor = self.editor.textCursor()
            if args is None:
                cursor.insertText(token)
            elif args > 0:
                # 骨架 `NAME()`，再把光标左移一格 → 落在括号里，接着打字就是第一个参数
                cursor.insertText(f"{token}()")
                cursor.movePosition(QTextCursor.MoveOperation.Left)
            else:
                # 无参函数：`NAME()`，光标停在 `)` 之后（用户接着打 `>=2` 之类）
                cursor.insertText(f"{token}()")
            self.editor.setTextCursor(cursor)
            # 焦点回到编辑框：用户是从右边面板点的按钮，不去抢回来的话
            # 他接着敲键盘就是在别的控件上输入（一次都输不进去）
            self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

        def insert_operator(self, operator: str) -> None:
            """插入运算符；`AND`/`OR`/`NOT` 前后自动补空格。

            为什么要补：`A>1` 后面直接粘 `AND` 会变成 `A>1AND B` —— 这是**语法错误**，
            而错误提示说的是"未知字段 A>1AND"之类，小白根本看不懂。宁可多一个空格。
            """
            if operator not in _SPACED_OPERATORS:
                self.insert_token(operator)
                return
            text = self.editor.toPlainText()
            position = self.editor.textCursor().position()
            before, after = text[:position], text[position:]
            lead = "" if (not before or before[-1].isspace()) else " "
            trail = "" if (after and after[0].isspace()) else " "
            self.insert_token(f"{lead}{operator}{trail}")

        def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt 命名
            """`Tab` 键插入空格（不跳焦点）。

            为什么：写多行公式时 Tab 是想缩进对齐，而 QPlainTextEdit 默认会插入制表符
            （在不同编辑器里宽度不一样，粘贴到别处就歪）；跳焦点更糟 ——
            用户正在打的公式会跑到名称框里。
            """
            if obj is self.editor and event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Tab:
                    cursor = self.editor.textCursor()
                    cursor.insertText(TAB_SPACES)
                    self.editor.setTextCursor(cursor)
                    return True
            return super().eventFilter(obj, event)

        # ── 策略列表：重建 / 单击 / 右键菜单 ──────────────────────────

        def reload(self) -> None:
            """重建统一策略列表（内置 + 公式）。

            主窗口在选股完成后会调它（`_on_pipeline_done`），所以这里**绝不碰编辑区**：
            用户可能正开着编辑器改公式，刷一次列表就把他的草稿换掉是不可接受的
            （旧版 `reload()` 会顺手选中第一行并载入编辑框 —— 在"列表在上、编辑器按需展开"
            的布局里那会变成"每次选完股，编辑器自己弹出来并顶掉我在写的东西"）。

            这里**也不再刷"本次结果"区**（那块界面已经按用户要求删掉，见模块 docstring
            「选股结果去哪了」）：结果在「自选股池」页与桌面文件里，不在这一页。
            """
            self.specs = formulas_lib.formula_files(self.directory)
            runtime = formula_group.last_status()
            keep = self.selected_key()
            self.rows = build_strategy_rows(self.cfg, self.specs, runtime)

            self._loading = True
            self.table.blockSignals(True)
            try:
                self._fill_table()
            finally:
                self.table.blockSignals(False)
                self._loading = False

            self._restore_selection(keep)
            self._update_list_hint()

        def _fill_table(self) -> None:
            """按 `self.rows` 铺表格（勾选框**先 setChecked 再接信号**）。"""
            self._builtin_boxes = {}
            self._row_boxes = {}
            self._auction_box = None
            self.table.setRowCount(len(self.rows))
            for index, row in enumerate(self.rows):
                name_item = QTableWidgetItem(row.name)
                if row.is_builtin:
                    name_item.setToolTip(f"内置策略（{row.key}）——只能启用/关闭")
                elif row.is_auction:
                    name_item.setToolTip(
                        "内置的「竞价扫描」开关（不是选股策略）——勾上 = 每个交易日到点"
                        "自动扫全市场并推送，写回 config.toml 的 intraday_auction"
                    )
                else:
                    name_item.setToolTip(f"公式文件：{getattr(row.spec, 'path', '')}")
                self.table.setItem(index, 0, name_item)

                note_item = QTableWidgetItem(row.note)
                if row.note_tip:
                    note_item.setToolTip(row.note_tip)
                self.table.setItem(index, 1, note_item)

                box = QCheckBox()
                box.setChecked(row.enabled)
                box.setToolTip(self._box_tooltip(row))
                if row.kind == ROW_FORMULA and row.spec is not None and not row.spec.ok:
                    # 语法错的公式勾上也跑不了（`formulas.enabled_names()` 会跳过它）：
                    # 勾选框置灰 + 说明原因，而不是"让用户勾上、然后什么都不发生"
                    box.setEnabled(False)
                # **先 setChecked 再接信号**：否则建表时就会触发一次"保存设置"
                box.stateChanged.connect(
                    lambda state, r=row: self.set_row_enabled(r.kind, r.key, bool(state))
                )
                holder = QWidget()
                holder_layout = QHBoxLayout(holder)
                holder_layout.setContentsMargins(0, 0, 0, 0)
                holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                holder_layout.addWidget(box)
                self.table.setCellWidget(index, 2, holder)

                if row.is_builtin:
                    self._builtin_boxes[row.key] = box
                elif row.is_auction:
                    self._auction_box = box
                else:
                    self._row_boxes[row.key] = box

        @staticmethod
        def _box_tooltip(row: StrategyRow) -> str:
            """勾选框的 tooltip：**勾上会发生什么、写回哪个键**，逐类说清。"""
            if row.is_builtin:
                return (
                    "勾上 = 这条内置策略参与选股（写回 config.toml 的 "
                    "enabled_groups 与 enabled_strategies 两个键）；取消 = 退出\n"
                    "内置策略只能启用/关闭，不能修改或删除"
                )
            if row.is_auction:
                return (
                    "勾上 = 开启竞价扫描（写回 config.toml 的 intraday_auction）："
                    "每个交易日 09:20 / 09:25 各扫一次全市场，强势股直接推到浮窗/托盘\n"
                    "它**不参与选股** —— 不会往「自选股池」加票，也不改变选股结果；"
                    "参数在「系统设置 → 竞价扫描」那一组里改"
                )
            return (
                "勾上 = 这条公式参与选股（写回 config.toml 的 enabled_formulas）；"
                "默认不勾 —— 你自己的公式要不要用，由你决定"
            )

        def _update_list_hint(self) -> None:
            """列表上方那句灰字：默认文案；配置里的策略名写错时**把原因说出来**。

            为什么要单独盯这一条：`enabled_groups` 与 `enabled_strategies` 同时非空时
            取的是**交集**，用户手改 config.toml 时很容易配出"交集为空"——
            表现是"列表里一个勾都没有、点选股什么都没跑"，而日志在他看不见的地方。
            """
            selection = groups.resolve_from_config(self.cfg)
            if selection.warnings:
                self.list_hint.setText(
                    "⚠️ config.toml 里的策略设置有认不出来的名字（这会让勾选与实跑不一致）："
                    + "；".join(selection.warnings)
                    + "　改完点【开始选股】前先看一眼勾选是否与预期一致。"
                )
                return
            self.list_hint.setText(LIST_HINT)

        def selected_row(self) -> StrategyRow | None:
            """当前选中的行（没有选中返回 None）。"""
            model = self.table.selectionModel()
            indexes = model.selectedRows() if model is not None else []
            index = indexes[0].row() if indexes else -1
            return self.rows[index] if 0 <= index < len(self.rows) else None

        def selected_key(self) -> str:
            row = self.selected_row()
            return row.key if row is not None else ""

        def selected_spec(self) -> Any:
            """当前选中行对应的 `FormulaSpec`（内置策略行 / 没选中的公式 → None）。"""
            row = self.selected_row()
            if row is not None and row.kind == ROW_FORMULA:
                return row.spec
            # 没有选中行时按名称框找（用户手打了名字、还没保存过的情况）
            name = self.current_name()
            for spec in self.specs:
                if spec.name == name:
                    return spec
            return None

        def row_of(self, key: str) -> StrategyRow | None:
            """按 key（类名 / 公式名）取行。"""
            for row in self.rows:
                if row.key == key:
                    return row
            return None

        def select_row(self, name: str) -> int | None:
            """按名称选中一行（**用户切行时才载入编辑区/展开详情**）。

            Returns:
                那一行的下标（保存后要 `scrollToItem` 滚过去）；没找到就是 `None`。
            """
            for index, row in enumerate(self.rows):
                if row.key == name:
                    self.table.selectRow(index)
                    return index
            return None

        def _restore_selection(self, key: str) -> None:
            """刷列表后把选中恢复到原来那一行（**不触发**"打开"动作）。"""
            if not key:
                return
            for index, row in enumerate(self.rows):
                if row.key == key:
                    self._loading = True
                    try:
                        self.table.selectRow(index)
                    finally:
                        self._loading = False
                    return

        def on_row_selected(self) -> None:
            """单击一行：内置策略 → **只读详情**；公式 → 载入编辑器（可改可存）。"""
            if self._loading:
                return
            row = self.selected_row()
            if row is None:
                return
            if row.read_only:
                # 内置策略与竞价策略都是**只读**的：单击展开详情，绝不载入编辑器
                # （竞价那一行没有公式可编辑 —— 它的"参数"在设置页）
                self.show_builtin_detail(row)
            else:
                self.on_open_editor(row.spec)

        def show_builtin_detail(self, row: StrategyRow) -> None:
            """展开某条**只读行**的详情（内置策略：条件 + 证据；竞价策略：口径 + 限制）。

            两类行共用这一个入口：它们都是"只能启用/关闭、不能改不能删"的行，
            差别只在文案与详情文本的来源（内置读 `rules.py`，竞价读设置里那几个数字）。
            """
            self._detail_key = row.key
            if row.is_auction:
                self.detail_title.setText(f"{row.name}（{row.key}）—— 内置的竞价扫描开关，只读")
                self.detail_text = row.detail or auction_detail(self.cfg)
                self.detail_view.setPlainText(self.detail_text)
                self.bottom_stack.setCurrentWidget(self.detail_page)
                self.bottom_stack.setVisible(True)
                self._set_hint(
                    f"「{row.name}」不是选股策略，是盘中提示的开关：勾「状态」列（或右键"
                    "【启用】）就开启竞价扫描，每个交易日到点自动扫全市场。\n"
                    "它不会往「自选股池」加票、也不改变选股结果；"
                    "涨幅 / 量比 / 成交额那些参数在「系统设置 → 竞价扫描」里改。"
                )
                return
            self.detail_title.setText(f"{row.name}（{row.key}）—— 内置策略，只读")
            self.detail_text = row.detail or builtin_strategy_detail(row.key, self.cfg)
            self.detail_view.setPlainText(self.detail_text)
            self.bottom_stack.setCurrentWidget(self.detail_page)
            self.bottom_stack.setVisible(True)
            self._set_hint(
                f"「{row.name}」是内置策略：只能启用/关闭（勾「状态」列，或右键【启用】/【关闭】），"
                "不能改也不能删。\n"
                "想按自己的条件来：点【策略编辑】照它写一条公式，保存后勾上「参与选股」。"
            )

        def on_context_menu(self, pos: Any) -> None:
            """右键某一行 → 弹菜单（【启用】↔【关闭】/【删除】）。"""
            index = self.table.indexAt(pos)
            if not index.isValid():
                return
            row_number = index.row()
            if not (0 <= row_number < len(self.rows)):
                return
            # 右键**先把这一行选中**（用户期望"菜单作用于我点的那一行"），
            # 但**不展开**编辑器/详情：`_loading` 让 `on_row_selected` 直接返回 ——
            # 否则右键一条公式就会顺手把编辑器顶出来，盖住用户正在看的东西。
            self._loading = True
            try:
                self.table.selectRow(row_number)
            finally:
                self._loading = False
            picked = self.row_menu(self.rows[row_number])
            self._show_menu(picked.menu, self.table.viewport().mapToGlobal(pos))

        def _show_menu(self, menu: Any, global_pos: Any) -> None:
            """真正弹菜单。**单独一个方法是为了让测试能拦住它** ——
            `exec()` 会阻塞事件循环，离屏测试里没有用户去点它，直接把测试挂死。
            """
            menu.exec(global_pos)

        def row_menu(self, row: StrategyRow) -> RowMenu:
            """这一行的右键菜单。

            三项规矩（用户逐条点到过）：
            1. 【启用】↔【关闭】**按当前状态只出现一个**（菜单里同时出现两个，用户会猜
               "哪个是现在的状态"）；名字写进 `objectName`，测试与调试一眼能看出是谁的菜单；
            2. 【删除】只对公式有效；内置策略的删除项**置灰并写明"内置策略不可删"**，
               而不是干脆不出现 —— 右键点开发现"什么都没有"时，用户的第一反应是
               "程序坏了"，写上理由他立刻明白；
            3. 菜单动作与勾选框走**同一个入口**（`set_row_enabled`），两处行为不可能不一致。
            """
            menu = QMenu(self.table)
            menu.setObjectName(f"rowMenu:{row.kind}:{row.key}")
            if row.is_auction:
                # 竞价那一行的"启用/关闭"开关的是**盘中扫描**，不是选股 ——
                # 菜单文案照实说，否则用户会以为勾上就能进池子
                if row.enabled:
                    toggle = menu.addAction(MENU_DISABLE)
                    toggle.setToolTip("关闭竞价扫描（下次开盘不再自动扫全市场、不再推提醒）")
                else:
                    toggle = menu.addAction(MENU_ENABLE)
                    toggle.setToolTip(
                        "开启竞价扫描：每个交易日 09:20 / 09:25 各扫一次全市场，"
                        "强势股推到浮窗/托盘（不参与选股）"
                    )
            elif row.enabled:
                toggle = menu.addAction(MENU_DISABLE)
                toggle.setToolTip(f"让「{row.name}」退出选股（下次【开始选股】不再跑它）")
            else:
                toggle = menu.addAction(MENU_ENABLE)
                toggle.setToolTip(f"让「{row.name}」参与选股（下次【开始选股】就会跑它）")
                if row.spec is not None and not row.spec.ok:
                    # 语法错的公式**勾上也跑不了**（`formulas.enabled_names()` 会跳过它），
                    # 所以"启用"这一项置灰并说明先修公式 —— 而不是让用户勾上之后
                    # 什么都看不到（那会变成"我勾了公式，池子里却没有"这种最难查的现象）
                    toggle.setEnabled(False)
                    toggle.setToolTip(
                        "这条公式现在编译不过，勾上也不会参与选股：先在编辑器里改好、"
                        "点【校验】通过，再右键启用"
                    )
            toggle.triggered.connect(
                lambda _checked=False, r=row: self.set_row_enabled(r.kind, r.key, not r.enabled)
            )

            delete = menu.addAction(MENU_DELETE)
            if row.is_auction:
                delete.setText(MENU_DELETE_AUCTION)
                delete.setEnabled(False)
                delete.setToolTip(
                    "竞价策略是内置的开关（参数在「系统设置 → 竞价扫描」那一组）："
                    "只能启用/关闭，不能删除"
                )
            elif row.is_builtin:
                delete.setText(MENU_DELETE_BUILTIN)
                delete.setEnabled(False)
                delete.setToolTip(
                    "内置策略写在代码里（strategy/rules.py）：只能启用/关闭，不能删除。"
                    "想按自己的条件来，就照它写一条公式"
                )
            else:
                delete.setToolTip(f"删掉公式文件「{row.key}」（会先问一句）")
                delete.triggered.connect(
                    lambda _checked=False, r=row: self.on_delete_formula(r.key)
                )
            return RowMenu(menu=menu, toggle=toggle, delete=delete)

        # ── 「参与选股」写回 config.toml ──────────────────────────────

        def set_row_enabled(self, kind: str, key: str, checked: bool) -> None:
            """统一的"启用/关闭"入口（勾选框与右键菜单都走这里 → 行为必然一致）。"""
            if kind == ROW_BUILTIN:
                self.on_toggle_builtin(key, checked)
            elif kind == ROW_AUCTION:
                self.on_toggle_auction(checked)
            else:
                self.on_toggle_enabled(key, checked)

        def on_toggle_builtin(self, class_name: str, checked: bool) -> None:
            """内置策略的「参与选股」 → 写回 `enabled_groups` + `enabled_strategies`。

            **为什么两个键都写**（这一页最容易踩的坑，实测过一次）：
            `groups.resolve()` 的规则是"两个键同时非空时取**交集**"，而
            `enabled_groups` 的出厂值是 `["short"]`。于是：

            * **只写 `enabled_strategies`**：用户勾上「低价股」（swing 组）时，
              交集 = `["LowPriceStrategy"] ∩ short 的成员` = **空** →
              `selection.empty` → 整轮选股被跳过。表现是"我明明勾了它，点选股却什么都没选"，
              而提示只说"没有可跑的策略"，用户根本猜不到是自己勾的那一下写法的问题；
            * **只写 `enabled_groups`**：勾一条就把**整组**带进来（`short` 组三条没法单独关掉一条），
              而用户给定的界面是按**条**勾选的。

            所以这里按"用户勾了哪些策略"**同时**算出两个键：
            组 = 勾上的策略所在的组，策略 = 勾上的那些 —— 交集恒等于用户勾的那一组。
            全部取消勾选时写 `enabled_groups = ["none"]`：那是 `resolve()` 里明确定义的
            "只盯自选股"（`explicit_off`）。**不能用空列表** —— 两个键都空在 `resolve()` 里
            是"全选"这个安全默认，写空会反过来变成"全部策略一起跑"。
            """
            from laoa_trader.config import save_settings

            wanted = builtin_enabled(self.cfg)
            if checked:
                wanted.add(class_name)
            else:
                wanted.discard(class_name)

            order = builtin_order()
            strategies = [name for name in order if name in wanted]
            # 认不出来的策略名（用户手写的、或将来新增的）**原样保留**：
            # 界面这一下改动的是"这 5 条内置策略"，不该顺手把用户写的东西删掉
            extras = [
                str(name) for name in (getattr(self.cfg, "enabled_strategies", None) or [])
                if str(name) not in order
            ]

            if strategies or extras:
                group_keys = [
                    key for key in groups.GROUP_ORDER
                    if any(groups.group_of(name) == key for name in strategies)
                ]
                selection = groups.resolve(group_keys, strategies + extras)
                if selection.empty:
                    self._sync_box(ROW_BUILTIN, class_name, not checked)
                    self._set_hint(
                        "❌ 这样勾完一条策略都不会跑：" + "；".join(selection.warnings)
                        + "\n（至少留一条勾着；一条都不想跑就把它们全取消 —— 那时只盯自选股）"
                    )
                    return
                updates: dict[str, Any] = {
                    "enabled_groups": group_keys,
                    "enabled_strategies": strategies + extras,
                }
            else:
                updates = {"enabled_groups": [OFF_GROUP_KEY], "enabled_strategies": []}

            try:
                path, self.cfg = save_settings(self.cfg, updates)
            except OSError as exc:
                # 写不进去（只读盘）：把勾选状态**退回去**，免得界面显示的与实际生效的不一致
                self._sync_box(ROW_BUILTIN, class_name, not checked)
                self._set_hint(
                    f"❌ 保存失败：{exc}（可手改 config.toml 的 enabled_groups / enabled_strategies）"
                )
                return

            self._sync_box(ROW_BUILTIN, class_name, checked)
            self._refresh_row_states()
            name = rules_mod.strategy_label(class_name)
            if checked:
                self._set_hint(
                    f"✅ 内置策略「{name}」已参与选股（写回 {path.name}）。\n"
                    f"enabled_groups={_cfg_list(self.cfg, 'enabled_groups')}　"
                    f"enabled_strategies={_cfg_list(self.cfg, 'enabled_strategies')}\n"
                    "两个键必须一起写：`groups.resolve()` 在两者都非空时取交集，"
                    "只写一个会出现「勾了却不跑」或「关不掉组里的一条」。"
                )
                self._toast(f"内置策略「{name}」已参与选股")
            elif not updates["enabled_strategies"]:
                self._set_hint(
                    "已把内置策略**全部关闭**（config.toml 写成 enabled_groups=[\"none\"]）。\n"
                    "下次【开始选股】只盯自选股 —— 想恢复就把某一条再勾上。"
                )
                self._toast("已关闭全部内置策略（只盯自选股）")
            else:
                self._set_hint(
                    f"内置策略「{name}」已退出选股（{path.name} 已更新："
                    f"enabled_groups={_cfg_list(self.cfg, 'enabled_groups')}）。"
                )
                self._toast(f"内置策略「{name}」已退出选股")

        def on_toggle_enabled(self, name: str, checked: bool) -> None:
            """公式的「参与选股」 → 写回 `enabled_formulas`（保留注释与未知键）。"""
            from laoa_trader.config import save_settings

            names = [str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or [])]
            if checked:
                if name not in names:
                    names.append(name)
            else:
                names = [n for n in names if n != name]
            try:
                path, self.cfg = save_settings(self.cfg, {"enabled_formulas": names})
            except OSError as exc:
                # 写不进去（只读盘）：把勾选状态**退回去**，免得界面显示的与实际生效的不一致
                self._sync_box(ROW_FORMULA, name, not checked)
                self._set_hint(f"❌ 保存失败：{exc}（可手改 config.toml 的 enabled_formulas）")
                return
            self._sync_box(ROW_FORMULA, name, checked)
            self._refresh_row_states()
            if checked:
                self._set_hint(
                    f"✅ 「{name}」已加入选股（写回 {path.name}）。\n"
                    "下次【开始选股】时它会作为「公式」组参与：进池的票来源会标成"
                    f"「公式·{name}」。"
                )
                self._toast(f"公式「{name}」已参与选股")
            else:
                self._set_hint(f"「{name}」已退出选股（{path.name} 里的 enabled_formulas 已更新）")
                self._toast(f"公式「{name}」已退出选股")

        def on_toggle_auction(self, checked: bool) -> None:
            """「竞价策略」的启用/关闭 → 写回 `intraday_auction`（**不是**选股开关）。

            为什么它写的是另一个键：这一行管的不是"选股时跑不跑"，而是"开盘后要不要
            自动扫全市场并推提醒"。`config.toml` 里那个键与设置页「竞价扫描」那一组的
            开关是同一个 —— 两处改的是同一件事，所以改完这里、那边也跟着变（反之亦然）。
            """
            from laoa_trader.config import save_settings

            try:
                path, self.cfg = save_settings(self.cfg, {"intraday_auction": bool(checked)})
            except OSError as exc:
                # 写不进去（只读盘）：把勾选状态退回去，免得界面显示与实际生效不一致
                self._sync_box(ROW_AUCTION, AUCTION_KEY, not checked)
                self._set_hint(f"❌ 保存失败：{exc}（可手改 config.toml 的 intraday_auction）")
                return
            self._sync_box(ROW_AUCTION, AUCTION_KEY, checked)
            self._refresh_row_states()
            at = " / ".join(_auction_settings(self.cfg)["at"])
            if checked:
                self._set_hint(
                    f"✅ 竞价策略已开启（写回 {path.name} 的 intraday_auction）："
                    f"每个交易日 {at} 各扫一次全市场，强势股直接推到浮窗 / 托盘。\n"
                    "⚠️ 它不参与选股：不会往「自选股池」加票，也不改变选股结果；"
                    "竞价数据没有历史，所以它只能当盘中提示、无法回测。\n"
                    "参数在「系统设置 → 竞价扫描」里改。"
                )
                self._toast("竞价策略已开启（每天到点自动扫全市场）")
            else:
                self._set_hint(
                    f"竞价策略已关闭（{path.name} 里的 intraday_auction=false）："
                    "开盘后不再自动扫描、不再推竞价提醒。"
                )
                self._toast("竞价策略已关闭")

        def _row_box(self, kind: str, key: str) -> Any:
            if kind == ROW_AUCTION:
                return self._auction_box
            folder = self._builtin_boxes if kind == ROW_BUILTIN else self._row_boxes
            return folder.get(key)

        def _sync_box(self, kind: str, key: str, checked: bool) -> None:
            """把某一行的勾选框同步成 `checked`（**屏蔽信号**，避免再触发一次写回）。"""
            box = self._row_box(kind, key)
            if box is None:
                return
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)

        def _refresh_row_states(self) -> None:
            """写回配置之后刷新**行数据**的勾选状态（详情里的"当前状态"也跟着变）。

            为什么不重新 `reload()`：那会把整张表的控件重建一遍，
            用户/测试手里那个 `QCheckBox` 对象会被销毁
            （PySide 之后再访问它直接抛 `RuntimeError: Internal C++ object already deleted`）。
            这里只换行数据 + 刷新正在显示的那份详情。

            内置与公式**都要刷**：右键菜单的文案是"按当前状态只出现【启用】或【关闭】"，
            行数据不跟着配置走的话，用户右键会看到与事实相反的菜单项
            （刚勾上它，菜单却说【启用】）。
            """
            enabled_builtin = builtin_enabled(self.cfg)
            enabled_formulas = set(
                str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or [])
            )
            auction_on = auction_enabled(self.cfg)

            def _enabled(row: StrategyRow) -> bool:
                if row.is_builtin:
                    return row.key in enabled_builtin
                if row.is_auction:
                    return auction_on
                return row.key in enabled_formulas

            def _detail(row: StrategyRow) -> str:
                if row.is_builtin:
                    return builtin_strategy_detail(row.key, self.cfg)
                if row.is_auction:
                    return auction_detail(self.cfg)
                return row.detail

            self.rows = [
                replace(row, enabled=_enabled(row), detail=_detail(row)) for row in self.rows
            ]
            if self._detail_key:
                row = self.row_of(self._detail_key)
                if row is not None and row.read_only:
                    self.detail_text = row.detail
                    self.detail_view.setPlainText(row.detail)

        # ── 编译器 / 校验 / 运行 ──────────────────────────────────────

        def on_open_editor(self, spec: Any = None) -> None:
            """打开公式编辑器（点【策略编辑】按钮，或点列表里的公式行）。"""
            if spec is not None:
                self._load_spec(spec)
            elif not self.editor.toPlainText().strip() and not self.name_edit.text().strip():
                # 头一次打开且编辑框是空的：给一句"下一步做什么"，不要让用户面对白板
                self._set_hint(
                    "照着示例改最快：点【载入示例】；右边按钮点一下就插到光标那里。"
                )
            self.bottom_stack.setCurrentWidget(self.editor_page)
            self.bottom_stack.setVisible(True)
            self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

        def on_close_panel(self) -> None:
            """收起下半块（编辑器 / 内置详情）。公式已经保存的不会丢。"""
            self.bottom_stack.setVisible(False)

        def _load_spec(self, spec: Any) -> None:
            """把一条公式载入编辑区（名称 + 备注 + 正文）。"""
            self._loading = True
            try:
                self.name_edit.setText(spec.name)
                self.note_edit.setText(getattr(spec, "description", "") or "")
                self.editor.setPlainText(spec.source)
            finally:
                self._loading = False
            if spec.ok and spec.formula is not None:
                text = f"已载入「{spec.name}」：{spec.formula.describe()}"
                hint = formulas_lib.limit_up_hint(spec.formula)
                if hint:
                    text += "\n⚠️ " + hint
                self._set_hint(text)
            elif not spec.ok:
                self._set_hint(f"❌ 这条公式现在有问题：{spec.error_text}\n"
                               "改完再点【校验】；点【保存】就会覆盖原文件。")
            else:
                self._set_hint(f"已载入「{spec.name}」")

        def compile_current(self, *, quiet: bool = False) -> Any:
            """编译编辑框里的公式；失败时把**中文原文**写进提示区并返回 None。"""
            text = self.editor.toPlainText()
            if not text.strip():
                if not quiet:
                    self._set_hint("❌ 公式还是空的：点右边的按钮就能插入，或者点【载入示例】")
                return None
            try:
                return fm.compile_formula(text, name=self.name_edit.text().strip())
            except fm.FormulaError as exc:
                if not quiet:
                    # 直接取引擎结构化错误里的 `text`（=「第 3 行第 12 列：未知函数 "MAA"
                    # （可用函数：…）」这种中文原文），界面**一个字都不翻译** ——
                    # 引擎已经把行号列号、近似建议、可用清单都写好了，再润色一遍只会
                    # 引入新的不一致。`to_dict()` 里的 line/col 留着给将来"把光标跳到
                    # 出错位置"用。
                    message = "❌ " + str(exc.to_dict()["text"])
                    self._set_hint(message)
                    # ⚠️ **同时弹到标题区的运行状态**（用户 2026-09-18 实报："可以再测试的
                    # 时候给个提示啊 公式有语法错误"）：提示区在**整个页面最下面**，
                    # 窗口小一点、或者用户正盯着上面那半屏时，那句话等于没写。
                    # 【校验】【运行】两条路都会走到这里，所以一句 toast 就够覆盖两处。
                    self._toast(message)
                return None

        def on_validate(self) -> None:
            """【校验】：显示用到的字段/函数/最少 K 线数；错就显示中文行号列号。"""
            formula = self.compile_current()
            if formula is None:
                return
            lines = [f"✅ 校验通过：{formula.describe()}"]
            if formula.outputs:
                lines.append("中间变量：" + "、".join(formula.outputs) + "（:= 只算不输出）")
            if formula.min_history > 0:
                lines.append(
                    f"提示：库里的历史不足 {formula.min_history} 根 K 线的票会自动跳过（缺值不产生信号）"
                )
            hint = formulas_lib.limit_up_hint(formula)
            if hint:
                lines.append("⚠️ " + hint)
            self._set_hint("\n".join(lines))

        def on_preview(self) -> None:
            """【运行】（按钮文案；内部标识符仍叫 preview）：当前库能选出几只。

            **后台线程 + 公式快照**：运行要逐只票读 K 线并在最后一根上跑公式，
            真实 3 年库实测 3.7 秒、全市场 5000+ 只要 7~8 秒 —— 在主线程里跑就是
            "窗口未响应"（用户已经为这件事抱怨过一次，那次是下载路径）。

            为什么先编译一次、再把**同一个公式对象**交给线程：线程跑的是用户按下按钮
            那一刻看到的公式。他在等待期间接着改编辑框（很常见：边等边琢磨条件），
            编辑框里的新内容不会把结果污染成"另一条公式的答案"。
            """
            formula = self.compile_current()
            if formula is None:
                return
            if self.preview_worker is not None and self.preview_worker.isRunning():
                # 双击 / 上一次还没跑完又点一次：不动正在跑的那次
                # （两个线程抢同一个库没有意义，只是白扫一遍）
                self._set_hint("运行还在跑，请稍候…（跑完会写在这里）")
                return
            self.preview_formula = formula
            # 新一轮开始 = 上一轮的导出依据作废：**绝不让用户导出陈结果**
            # （他刚点了【运行】，导出的必须是这一轮出来的清单）
            self.last_run = None
            self.btn_preview.setEnabled(False)
            # 先设成"不确定进度"：总共有多少只票要扫，得先走一遍库才知道。
            # 让进度条先动起来，比"停在 0% 七八秒"让人安心。
            self.progress.setRange(0, 0)
            self.progress.setFormat("正在运行…")
            self.progress.setVisible(True)
            self._set_hint("正在运行…（在后台跑，界面可以继续用）")
            # ⚠️ `cfg` 必须传进去：公式用到 `流通市值` / `换手率` 时要靠它去取那一趟
            # 实时快照（不用这两个字段的公式一个请求都不发，见 `preview_hits`）。
            # 早先漏传过一次，结果是"用到市值/换手的公式在界面上永远 0 只、
            # 而且连"取不到快照"这句提示都不出现"—— 用户只会以为公式写错了。
            worker = FormulaWorker(
                formulas_lib.preview_hits, formula, self.cfg.db_path,
                limit=PREVIEW_LIMIT, cfg=self.cfg,
            )
            self.preview_worker = worker
            worker.finished_ok.connect(self._on_preview_done)
            worker.failed.connect(self._on_preview_failed)
            worker.start()

        @staticmethod
        def _preview_text(formula: Any, result: dict) -> str:
            """把 `preview_hits` 的返回值拼成提示区那段中文。

            单独一个**纯函数**，是为了让"结果长什么样"与"它在哪个线程跑"解耦：
            后台化只该换线程，不该换文案（用户已经见过这几句话），
            所以格式化逻辑只此一份，谁调都是同一段文本。

            注：`hits` 现在是**全量**，显示按 `shown` 截断（见 `formulas.preview_hits`
            的 Returns）—— 界面一次列 60 只票会把提示区与列表一起挤没。
            """
            hits = list(result.get("hits") or [])
            shown = result.get("shown")
            if shown is None:      # 老/替身返回值里没有 `shown` 时：全列（宁可多列，不少列）
                shown = len(hits)
            hits = hits[:shown]
            if result["count"]:
                # 标的写法与全项目一致：**半角** `名称(代码)`（改版方案第四节），
                # 与「自选股池」表格、推送正文、错误信息里的写法逐字相同
                names = "、".join(
                    f"{hit['name']}({hit['symbol']})" for hit in hits
                )
                text = (f"最近交易日 {result['date']} 命中 {result['count']} 只：{names}")
                if result["count"] > shown:
                    text += f" …（只列前 {shown} 只）"
            else:
                text = (f"最近交易日 {result['date']}：没有命中"
                        f"（扫了 {result['scanned']} 只，{result['skipped']} 只因数据不足跳过）")
            hint = formulas_lib.limit_up_hint(formula)
            if hint:
                text += "\n⚠️ " + hint
            # 全局提示（例如"市值/换手取不到"）单独一行 —— 它跟"某只票算不出来"不是
            # 一回事：混进 `errors` 会被渲染成"1 只票算不出来"，票数是假的
            for note in result.get("notes") or []:
                text += "\n" + str(note)
            if result["errors"]:
                text += f"\n（{len(result['errors'])} 只票算不出来，已跳过：{result['errors'][0]}）"
            return text

        @staticmethod
        def _run_name(formula: Any) -> str:
            """这一轮结果挂在哪个名字下（桌面文件「来源」列的后半截）。

            优先用**公式自己的名字**（`compile_current()` 把名称框的内容带进了公式对象，
            所以这就是"按下【运行】那一刻的名字"），没有名字时写「未命名公式」——
            宁可写一个诚实的占位词，也不要在来源列里留一段空白。
            """
            return str(getattr(formula, "name", "") or "").strip() or "未命名公式"

        def _on_preview_done(self, result: Any) -> None:
            """运行回来了（回主线程执行）：先收起"正在跑"的样子，再写结果。"""
            formula = self.preview_formula
            self._finish_preview()
            if not isinstance(result, dict):
                self._set_hint("运行没有返回结果（请重试）")
                return
            # 记住这一轮，供【导出选股结果】使用：导出的必须是**用户刚看过的**那一份
            # （所以连行情日一起存下来，而不是导出时再查一次"最近交易日"——
            # 跑完到导出之间数据可能已经更新，那样文件里的日期会与提示区不一致）
            self.last_run = {
                "name": self._run_name(formula),
                "date": result.get("date"),
                "count": int(result.get("count") or 0),
                "hits": [dict(hit) for hit in (result.get("hits") or [])
                         if isinstance(hit, dict) and hit.get("symbol")],
            }
            self._set_hint(self._preview_text(formula, result))

        def on_export(self) -> None:
            """【导出选股结果】：把**上一次【运行】**的命中清单写成桌面上的文本文件。

            三条口径：

            * **导的是用户刚看过的那一份**（`self.last_run`），不重新跑一遍：
              重跑会得到另一份清单（盘中快照/新数据都会变），而他导出的必须是他看过的；
              再说重跑一次要扫全库 7~8 秒，点一下按钮等 8 秒不像话；
            * **全量**：提示区只列前 `PREVIEW_LIMIT` 只（一行放不下几十只），
              文件里是**全部**命中 —— 给用户的文件少几只，是最难被发现的那种错；
            * **成败都要看得见**：产物落在桌面（这一页之外），"没反应"就等于
              "不知道导没导出去"，所以提示区写清楚 + 标题区弹一句。

            用的函数与「开始选股」建池时导出的是**同一个**（`pool.export_pick_file`）：
            版式、来源列、现价列完全一致 —— 用户拿两个文件对比时不该看到两套格式。
            """
            run = self.last_run
            if run is None and self.preview_worker is not None and self.preview_worker.isRunning():
                # 正在跑：这时候说"还没运行过"是骗人的（他刚点过），
                # 而且这一轮的结果马上就有了 —— 让他等这一轮，别导出上一轮
                self._set_hint("运行还没跑完：等它跑完再点【导出选股结果】（导出的就是这一轮的命中）")
                return
            if not run:
                self._set_hint(
                    "还没有可导出的结果：先点【运行】看看能选出几只，再点【导出选股结果】"
                )
                return
            hits = [hit for hit in (run.get("hits") or []) if hit.get("symbol")]
            day_text = str(run.get("date") or "未知")
            if not hits:
                message = f"❌ 最近交易日 {day_text} 没有命中的股票，没有可导出的结果"
                self._set_hint(message)
                self._toast(message)
                return
            rows = [
                {
                    "symbol": str(hit["symbol"]),
                    "name": str(hit.get("name") or ""),
                    # 「来源」列与「自选股池」同一个词：公式选中 → `公式·<公式名>`
                    "strategy": groups.formula_strategy_name(str(run.get("name") or "")),
                }
                for hit in hits
            ]
            try:
                # `dest_dir` **不传**：由 `pool.desktop_dir()` 自己找桌面（Windows 上是
                # 用户真实的桌面，可能是 OneDrive 里那一个）；找不到时退回**数据目录**
                # （用户找得到的地方），而不是悄悄不导出。
                path = pool_mod.export_pick_file(
                    rows,
                    data_date=day_text,
                    db_path=self.cfg.db_path,
                    fallback_dir=Path(self.cfg.db_path).parent,
                )
            except Exception as exc:  # noqa: BLE001 - 导出失败绝不能把这一页带崩
                message = f"❌ 导出失败：{type(exc).__name__}: {exc}"
                self._set_hint(message)
                self._toast(message)
                return
            if path is None:
                # `export_pick_file` 把具体原因写进日志、只返回 None（它不许影响选股流程），
                # 所以这里给用户列出**可能**的原因与下一步，并让他去看日志
                message = ("❌ 导出失败：文件没能写出去（桌面目录不存在、没有写权限，"
                           "或者文件正被别的程序占用）—— 详见日志")
                self._set_hint(message)
                self._toast(message)
                return
            message = (f"✅ 已导出选股结果：{path.name}（{len(rows)} 只，行情日 {day_text}）"
                       f"\n文件位置：{path}")
            self._set_hint(message)
            self._toast(f"已导出选股结果：{path.name}")

        def _on_preview_failed(self, exc: Any) -> None:
            """运行失败：**数据问题**与**程序问题**分开说（下一步动作完全不同）。

            注：这里的两句话与改造前的同步版本**逐字一致**（只把"试算"这个词换成
            按钮上的"运行"）。`failed` 递过来的是异常对象（不是一句话），
            正是为了在这里用 `isinstance` 分清这两种情况 —— 工作线程不该替界面决定措辞。
            """
            self._finish_preview()
            self.last_run = None      # 这一轮没有结果 → 不给导出（宁可让他重跑）
            if isinstance(exc, fm.FormulaDataError):
                # 库不存在/读不出来：这不是公式写错了，说清楚下一步
                self._set_hint("❌ " + str(exc))
            else:
                self._set_hint(f"❌ 运行失败：{type(exc).__name__}: {exc}")

        def _finish_preview(self) -> None:
            """【运行】收尾：把按钮还回来、收掉进度条、放掉线程引用。

            为什么"等它真的退出"再放引用：`finished_ok`/`failed` 是**跨线程排队**投递的，
            在工作线程 `run()` 返回之前就可能已经排到主线程执行了；这时丢掉最后一个引用，
            QThread 对象会在"线程还没真正结束"时就析构 —— Qt 会
            `QThread: Destroyed while thread is still running` 直接把进程干掉。
            线程此刻已经在收尾，`wait()` 只等几毫秒，比留一堆线程对象在 `self` 上干净。
            """
            worker = self.preview_worker
            self.btn_preview.setEnabled(True)
            self.progress.setVisible(False)
            if worker is not None:
                if worker.isRunning():
                    worker.wait(THREAD_JOIN_MS)
                self.preview_worker = None

        # ── 保存 / 另存为 / 删除 ──────────────────────────────────────

        def _note_text(self) -> str | None:
            """备注 → 注释头那一行；空 = None（交给库自动生成"用到的字段/函数"）。

            **换行必须拍平**：`# 说明:` 是注释头的**一行**，写进去一个换行就会让
            后面那半行不再以 `#` 开头 —— 引擎读文件时把它当成公式正文，
            用户下次点开这条公式就会看到"未知字段"的报错（而他明明没改过公式）。
            这种坑在界面上完全看不出来，所以在入口拍平（`" ".join(split())`）。
            """
            raw = self.note_edit.text()
            note = " ".join(raw.split())
            if not note:
                return None
            return note[:MAX_NOTE_CHARS]

        def _note_problem(self) -> str:
            """备注合不合规（超长直接拒绝，**不静默截断**：用户要能自己决定删什么）。"""
            note = " ".join(self.note_edit.text().split())
            if len(note) > MAX_NOTE_CHARS:
                return (f"备注太长了（{len(note)} 字，最多 {MAX_NOTE_CHARS} 字）："
                        "它写在公式文件第一行的「# 说明:」注释头里，太长会把公式挤到看不见。")
            return ""

        def on_save(self) -> None:
            """【保存】：名称必填；重名先问一句再覆盖。"""
            raw = self.name_edit.text()
            problem = formulas_lib.name_error(raw)
            if problem:
                # 空名**只用提示区**、不弹窗：这是用户马上能自己改的问题，
                # 弹一个要点"确定"的框反而多一步（而覆盖是**不可逆**的，才必须拦一下）
                self._set_hint("❌ " + problem)
                self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                return
            problem = self._note_problem()
            if problem:
                self._set_hint("❌ " + problem)
                self.note_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                return
            name = formulas_lib.safe_name(raw)
            path = formulas_lib.formula_path(name, self.directory)
            if path.exists() and not self._confirm(f"公式「{name}」已存在，要覆盖它吗？\n"
                                                   f"（原来的内容会被替换，不可撤销）"):
                self._set_hint(f"已取消保存：公式「{name}」保持原样（没有被改动）")
                self._toast("已取消保存（原公式没有被改动）")
                return
            self._write(name)

        def on_save_as(self) -> None:
            """【另存为】：换个名字再存一份（原文件不动）。"""
            default = formulas_lib.safe_name(self.name_edit.text()) or "新公式"
            text, ok = QInputDialog.getText(self, "另存为", "新公式名称：", text=default)
            if not ok:
                return
            problem = formulas_lib.name_error(text)
            if problem:
                self._set_hint("❌ " + problem)
                return
            problem = self._note_problem()
            if problem:
                self._set_hint("❌ " + problem)
                return
            name = formulas_lib.safe_name(text)
            path = formulas_lib.formula_path(name, self.directory)
            if path.exists() and not self._confirm(f"公式「{name}」已存在，要覆盖它吗？"):
                self._set_hint(f"已取消另存为：公式「{name}」保持原样")
                self._toast("已取消另存为")
                return
            self._write(name)

        def on_delete(self) -> None:
            """【删除】按钮：删掉**当前**这条公式（先确认）。"""
            name = self.current_name()
            if not name:
                self._set_hint("❌ 请先在上面列表里选一条公式（或填上名称）——内置策略不能删")
                return
            self.on_delete_formula(name)

        def on_delete_formula(self, name: str) -> None:
            """删除一条公式文件（右键菜单与【删除】按钮共用，**先二次确认**）。"""
            if not name:
                return
            if not self._confirm(f"确定删除公式「{name}」吗？\n（公式文件会被删掉，不可撤销）"):
                self._set_hint(f"已取消删除：公式「{name}」还在")
                return
            try:
                deleted = formulas_lib.delete_formula(name, self.directory)
            except OSError as exc:
                self._set_hint(f"❌ 删除失败：{exc}（文件可能正被其它程序占用）")
                return
            if not deleted:
                self._set_hint(f"❌ 没找到公式「{name}」的文件")
                return
            if self.current_name() == name:
                # 删掉的正是编辑区里这条：把编辑区清空，免得用户以为"它还在、只是没保存"
                self._loading = True
                try:
                    self.name_edit.clear()
                    self.note_edit.clear()
                    self.editor.clear()
                finally:
                    self._loading = False
            self.reload()
            self._set_hint(f"🗑 已删除公式「{name}」（文件已从公式目录移除）")
            self._toast(f"公式「{name}」已删除")

        def _write(self, name: str) -> None:
            """真正落盘（名称已安全化、覆盖已确认）。"""
            body = self.editor.toPlainText()
            note = self._note_text()
            try:
                path = formulas_lib.save_formula(
                    name, body, description=note, directory=self.directory
                )
            except ValueError as exc:
                self._set_hint("❌ " + str(exc))
                self._toast("❌ 没保存：" + str(exc))
                return
            except OSError as exc:
                # 这里**必须弹 toast**：提示区在编辑器底部，窗口小就看不见，
                # 用户会以为"存上了"（2026-09-18 实报"保存了却不显示"最可能就是这样）。
                # 目录写的是**这一页实际用的**那个（可能是 `LAOA_TRADER_FORMULAS`
                # 或调用方传进来的），不是默认值 —— 打默认值会让人找错地方。
                folder = self.directory or formulas_lib.formula_dir()
                self._set_hint(f"❌ 没存上：{exc}（公式目录：{folder}）")
                self._toast("❌ 公式没存上（公式目录可能不可写）")
                return
            # 把安全化后的名字、最终写进文件的备注都回显：用户填 `涨/跌` 时看到的是
            # `涨_跌`，备注留空时看到的是自动生成的那句"用到的字段/函数" ——
            # 他看到的与磁盘上的一致（否则他会以为"我存的东西怎么不见了"）
            self.name_edit.setText(name)
            saved = next((s for s in formulas_lib.formula_files(self.directory)
                          if s.name == name), None)
            if saved is not None:
                self.note_edit.setText(saved.description or "")
            self.reload()
            # 2026-09-18（用户明确要求，两句原话）：
            #   「保存就自动显示在策略最下面，默认不勾选」→ 公式本来就追加在末尾
            #   （内置在前、公式在后，按文件名排序）；滚过去让它真的**看得见**，
            #   否则窗口小的时候它在视野外，用户会以为没保存上。**不自动勾选**：
            #   参不参与选股由用户自己决定，保存只管存。
            #   「成功不需要提示，失败再提示」→ 这里**一个字都不说**：列表最下面多出
            #   那一行就是成功的反馈；把"已保存"再讲一遍只是噪音。
            index = self.select_row(name)
            if index is not None:
                self.table.scrollToItem(self.table.item(index, 0))
            if saved is not None and not saved.ok:
                # **存下去了，但这条公式跑不了**（语法/字段错）—— 这算"有问题"，必须说：
                # 文件躺在列表里、勾选框是灰的，用户不看那把灰勾是不会知道原因的。
                # 一句话、带行列号（引擎给的就是中文），别写小作文。
                self._set_hint("❌ 公式有错，跑不了：" + (saved.error_text or "语法错误"))
            else:
                # 保存成功**一个字都不说**（用户要求"成功不需要提示"）：
                # 列表最下面多出的那一行就是反馈；上一次失败留下的红字顺手收掉。
                self._clear_hint()

        def on_load_sample(self) -> None:
            """【载入示例】：给小白一个**能跑通**的起点。"""
            self.bottom_stack.setCurrentWidget(self.editor_page)
            self.bottom_stack.setVisible(True)
            sample = None
            for spec in formulas_lib.formula_files(self.directory):
                if spec.ok and (sample is None or spec.name == SAMPLE_NAME):
                    sample = spec
                    if spec.name == SAMPLE_NAME:
                        break
            if sample is not None:
                self.name_edit.setText(sample.name)
                self.note_edit.setText(getattr(sample, "description", "") or "")
                self.editor.setPlainText(sample.source)
                self._set_hint(
                    f"已载入示例公式「{sample.name}」。\n"
                    "点【校验】看看它用到什么，点【运行】看它在你的库里能选出几只。"
                )
            else:
                self.name_edit.setText(SAMPLE_NAME)
                self.note_edit.clear()
                self.editor.setPlainText(SAMPLE_TEXT)
                self._set_hint(
                    "已载入内置示例公式（公式目录里还没有示例文件，这是兜底的那条）。\n"
                    "点【校验】→【运行】，再点【保存】就存到你的公式目录里了。"
                )
            self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

        # ── 选股结果（**这一页不再显示**）─────────────────────────────
        #
        # 用户 2026-09-17：「策略选股只要显示策略，不显示选股结果，选股结果直接进
        # 自选股池，可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」
        #
        # 所以旧的「本次选股结果」表、一句话结论、[全部加为自选] 按钮与它们的
        # 渲染/写库方法（`_refresh_result` / `_render_result` / `_load_result_from_db` /
        # `_result_row` / `on_add_all_to_watchlist`）**整体删掉**。结果的两个去处见模块
        # docstring 的「选股结果去哪了」：自选股池页（可右键删、可手工加）+ 桌面文件
        # （`pool.export_pick_file()`，由 `scheduler.run_daily` 在建池成功后调用）。

        def show_pick_result(self, rows: Any = None, *, data_date: str | None = None) -> None:
            """界面不再显示结果（用户要求），这个方法保留为**空实现**以免调用方崩；
            结果通过【自选股池】与桌面导出文件呈现。

            为什么留着一个什么都不做的方法（而不是删掉）：

            * `ui/app.py` 的 `_on_pipeline_done()` 还在调它
              （`getattr(page, "show_pick_result", None)` → 有就调）。删掉方法本身
              不会炸（那边用的是 `getattr`），但一旦哪天接线改成直接调用，
              这里就会变成 `AttributeError` **把结论显示那段带崩** ——
              一个空的兼容方法比"指望调用方老记得容错"可靠得多；
            * 参数原样收下（report 或行列表都行），签名不缩水：
              调用方不用为了这一页改代码（`app.py` 属于另一个改动方）。

            它现在做的事**只有写日志**：把"这一轮到底选出了几只"记进日志文件，
            方便事后对账（对完账就知道桌面文件里应该有哪几只）。

            Args:
                rows: `run_daily()` 的 report（dict，取 `pool`/`data_date`）或行列表；
                    两者都收下只是为了兼容，内容不会被展示。
                data_date: 行情日（report 里没有时用这个）。
            """
            picked: list[dict] = []
            if isinstance(rows, dict):
                data_date = data_date or rows.get("data_date")
                picked = [r for r in (rows.get("pool") or []) if isinstance(r, dict)]
            elif rows is not None:
                picked = [r for r in rows if isinstance(r, dict)]
            # 只数**选出来的**票（带来源策略的行），与建池那份清单同一口径
            selected = [r for r in picked if r.get("symbol")
                        and str(r.get("strategy") or r.get("strategies") or "")]
            logger.debug(
                "选股结果不在这张页面显示（用户要求）：本轮 %d 只（行情日 %s）——"
                "结果在「自选股池」页与桌面导出文件里",
                len(selected), data_date or "未知",
            )

        # ── 小工具 ────────────────────────────────────────────────────

        def current_name(self) -> str:
            """当前编辑中的公式名（名称框内容；已做文件名安全化）。"""
            return formulas_lib.safe_name(self.name_edit.text().strip())

        def _clear_hint(self) -> None:
            """清掉提示区（保存成功时用：上一次失败留下的那句话不该一直挂着）。"""
            self.hint_text = ""
            self.hint_label.setText("")

        def _set_hint(self, text: str) -> None:
            """写提示区（完整文本留一份给测试/复制）。"""
            self.hint_text = text
            lines = text.splitlines()
            if len(lines) > HINT_MAX_LINES:
                # 太长的提示（多行错误、命中清单）：提示区只显示前几行，完整文本在
                # `self.hint_text`（可选中复制），免得把下面的列表挤出屏幕
                shown = lines[:HINT_MAX_LINES]
                shown.append(f"…（还有 {len(lines) - HINT_MAX_LINES} 行没显示）")
                text = "\n".join(shown)
            self.hint_label.setText(text)

        def _toast(self, text: str) -> None:
            """一句话提示：优先交给主窗口显示在标题区的运行状态，没有就只写日志。"""
            logger.info(text)
            if callable(self.status_cb):
                try:
                    self.status_cb(text)
                except Exception:  # noqa: BLE001 - 回调出错不该影响这一页
                    logger.debug("公式页状态回调出错", exc_info=True)

        def on_start_pick(self) -> None:
            """【开始选股】：**只举手**（emit `start_pick_requested`）。

            为什么这一页不自己跑选股：整条流程（数据闸门 → 增量 → 策略 → 公式 → 建池 →
            推送 → 落库）都住在 `scheduler.run_daily()`，主窗口负责进度条与状态显示；
            这一页要是自己调一遍，就会出现两套流程、两套状态、两套错误处理
            （而且"这一页没跑数据闸门"会安静地跑出一个错的池子）。
            主窗口把这个信号接到 `on_run_pipeline`，跑完再调 `reload()` 刷列表。
            跑完之后**结果不在这一页**：进「自选股池」（在那一页增删）+ 导出一份到桌面
            （`scheduler.run_daily` 里建池成功后做的）—— 这句提示里得说出来，
            否则用户点完看不到任何结果会以为程序没反应。
            """
            self._toast("开始选股：正在按勾选的策略与公式跑一轮…"
                        "（结果直接进「自选股池」，并同时导出到桌面一个文本文件）")
            self.start_pick_requested.emit()

        def on_copy_detail(self) -> None:
            """【复制】：把内置策略详情放进剪贴板（贴到记事本/群里都行）。"""
            if not self.detail_text:
                self._toast("还没有可复制的详情：先在上面点一条内置策略")
                return
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(self.detail_text)
            self._toast("详情已复制到剪贴板")

        def _confirm(self, question: str) -> bool:
            """二次确认（覆盖/删除）。**可被 monkeypatch**（测试里模拟点"是"）。"""
            answer = QMessageBox.question(
                self,
                "确认",
                question,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes


def _one_line(text: Any, limit: int) -> str:
    """多行文本压成一行（表格里只放得下一行；全文在 tooltip 与提示区）。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "EDITOR_HINT",
    "FUNCTIONS",
    "FormulaPage",
    "FormulaWorker",
    "HINT_MAX_LINES",
    "LIST_COLUMNS",
    "LIST_HINT",
    "MAX_NOTE_CHARS",
    "MENU_DELETE",
    "MENU_DELETE_BUILTIN",
    "MENU_DISABLE",
    "MENU_ENABLE",
    "OFF_GROUP_KEY",
    "EXCLUDES",
    "OPERATORS",
    "PAGE_HINT",
    "PALETTE_BUTTON_OBJECT",
    "PANEL_WIDTH",
    "PREVIEW_LIMIT",
    "AUCTION_KEY",
    "AUCTION_NAME",
    "ROW_AUCTION",
    "ROW_BUILTIN",
    "ROW_FORMULA",
    "RowMenu",
    "SAMPLE_NAME",
    "SAMPLE_TEXT",
    "StrategyRow",
    "TAB_SPACES",
    "THREAD_JOIN_MS",
    "VARIABLES",
    "auction_detail",
    "auction_enabled",
    "auction_note",
    "auction_note_tip",
    "auction_row",
    "build_strategy_rows",
    "builtin_enabled",
    "builtin_order",
    "builtin_strategy_detail",
    "builtin_strategy_note",
    "builtin_strategy_tip",
    "formula_row_note",
]
