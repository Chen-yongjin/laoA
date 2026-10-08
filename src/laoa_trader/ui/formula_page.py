"""「策略匹配」页：**统一策略列表 + 傻瓜式公式编辑器 + 开始匹配**。

用户原话
--------
> 「策略匹配 -策略编辑（可直接兼容通达信成品公式）
>   点击打开策略编辑器(傻瓜式编辑器左右界面，左边编辑框，右边变量+运算符)
>   编辑完成命名(可添加备注)一下可以保存到公式列表。
>   -策略列表 （名称-备注-状态）
>   点击打开公式详情，可修改保存。右键菜单删除和启用/关闭。
>   -开始匹配(选好的股发送消息，直接添加自选。)」

这一页就按这三块摆（`docs/开发文档.md` 1.2 节）：

1. **策略列表**（上半）：**随包公式与你自己的公式合成同一张表**，列固定为
   `策略名称 | 说明 | 策略选取`。最上面一行是「竞价策略」（唯一不是匹配策略的行，
   开关的是盘中竞价扫描），下面是公式；说明列取自公式文件里的 `# 说明:` 注释头。
   单击一行：公式 → 载入编辑器（可改可存）；竞价策略 → 只读详情。
   右键：启用/关闭、删除（公式可删，竞价策略那一行不可删）。
   2026-09-18（用户要求）：老版本这里先摆 5 条**内置策略**（写在 `strategy/rules.py`
   里的 Python 策略、只读不可删）；现在那 5 条改成了随包公式，列表里不再有内置行。
2. **策略编辑器**（下半，点【策略编辑】或点列表里的公式行才展开）：左边框、
   右边"点一下就插入"的按钮面板（变量 / 函数 / 运算符 / 排除 —— 排除组在**最下面**）。
   每个按钮的字是中文（点一下插入的仍是引擎认的语法），中文 tooltip 第一步写着"插入 XX"，
   `MA` 自动带括号、光标落在括号里，`AND` 自动补空格 —— 这些交互是**实测过的**，
   见下面 `insert_token` / `insert_operator` 的注释（别回退）。
   底部【校验】【运行】【导出匹配结果】【保存】【另存为】【删除】，名称旁边多了**备注**
   （写进文件的 `# 说明:` 注释头）。

   **【运行】= 原来的【试算】**（用户 2026-09-18 要求"把试算直接改成运行"）：
   同一件事 —— 在当前库上跑一遍这条公式、只看每只票最后一根 K 线、不推送不写库。
   只改了界面上的两个字（用户说"试算"看不懂），**内部标识符仍叫 `preview_*`**
   （`btn_preview` / `on_preview` / `preview_worker` / `formulas.preview_hits`）：
   那些名字连着一批测试与文档里的引用，为一个文案做机械重命名只会制造噪音。
   看到 `preview` 就当"这条按钮"读。

   【导出匹配结果】把**上一次【运行】的命中清单**（全量，不截断）写成桌面上的
   `luweik-匹配结果-<日期>.txt` —— 与 `scheduler.run_daily()` 建池时导出的
   是**同一个函数**（`pool.export_pick_file`），版式、来源列、现价列完全一致。
3. **【开始匹配】**（右上角）：本页**只 emit `start_pick_requested()`** ——
   增量数据 → 跑策略与公式 → 建池 → 推送这一整套在主窗口（`ui/app.py`）里，
   这一页不碰它（也不联网）。
4. **【成绩单】**（顶部那一排的最后）：按需打开 `ui/scorecard_dialog.py` 的对话框，
   把**勾选**的这几条策略在本地库上跑一遍历史成绩（预检 + 后台线程都在那个模块里）。

匹配结果去哪了（用户 2026-09-17 的明确要求）
--------------------------------------------
用户原话：「策略匹配只要显示策略，不显示匹配结果，匹配结果直接进自选标的，
可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」

所以这一页**只有策略列表**（`名称 | 备注 | 状态` + 【策略编辑】【开始匹配】【成绩单】）：
旧版那块「本次匹配结果」表 + 一句话结论 + 【全部加为自选】按钮**已经删掉**。
结果有**两个**去处在界面上看得见，一个都不会丢：

* **自选标的页**（主入口）：选出来的票就是 `stock_pool` 里的行，
  右键能【删除】、上面能【添加自选】—— 增删都在那一页做（用户原话"可以在股池再添加删除"）；
  这也是为什么这一页**不需要**再摆一张"结果表"：同一批票在一张表里看就够了。
* **桌面文件**：`scheduler.run_daily()` 在建池成功后调 `pool.export_pick_file()`，
  往桌面写一份 `luweik-匹配结果-<日期>.txt`（导出失败只记日志、不影响匹配）。

`show_pick_result()` 这个方法**保留**（主窗口还在调它）：它现在是空实现，
只写日志、不画任何东西 —— 删掉它会让 `ui/app.py` 那边 `AttributeError`
（`app.py` 属于另一个改动方，这一页不替它做决定）。

为什么这里**也有**「成绩单」，但只给一个**按需**入口
----------------------------------------------
旧版右下角常驻两个按钮（【看成绩单】【复制成绩单】）。2026-09 改版把它们**从界面上移除**：
数据只有 6 个月（`history_years = 0.5`），而成绩单自己有 250 个交易日 / 100 只票的
**库级门槛**（`research/scorecard.py`）—— 常驻在页面上永远只会显示"样本不足，无法评估"，
点一次还要扫全库几十秒。

2026-10-08 主人的决定是**要一个入口**。于是做法不是把面板搬回来，而是"按需 + 预检 + 后台"：
顶部一个【成绩单】按钮 → 打开 `ui/scorecard_dialog.py` 那个对话框。不点它就不读库，
也就不存在"随手点一下、等二十秒"的意外；对话框一打开先用本地库做一次预检
（判据**全部**来自 `research.scorecard.db_sufficiency()`，界面里不另写一套门槛），
库不够时最上面直接写清"现在有多少 / 需要多少 / 怎么补"，但**照旧允许继续点**
（有人就是想先看看自己那条策略长什么样），只在结果上如实标注"样本不足，不能当结论"；
计算与预检都在后台线程里跑，主线程不阻塞。

为什么按钮在这一页（而不是「系统设置」或菜单里）：成绩单评的就是**这一页列表里的策略**，
用户想到"这条策略到底行不行"时人就在这儿。它那个 tooltip 写明了"要扫全库、几十秒；
数据不足 1 年时结论不可用"。

为什么单独一个模块而不是塞进 `ui/app.py`
--------------------------------------
`app.py` 已经 4300 行。这一页有 40 多个控件与自己的后台线程，塞进去会让
"主窗口接线"与"公式编辑"两件事互相干扰；分开之后，这一页可以用 Qt 的 offscreen
平台插件**单独**建出来测（`tests/test_formula_page.py` / `test_formula_lib.py`），
不用把整个主窗口拉起来。主窗口那边只留 `addTab` 与信号接线。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from laoa_trader import formulas as formulas_lib
from laoa_trader import market as market_mod
from laoa_trader import pool as pool_mod
from laoa_trader.config import get_config
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group

logger = get_logger(__name__)

try:  # Qt 缺失时不应该 import 就炸（与 ui/app.py 同一个约定）
    from PySide6.QtCore import QEvent, QSize, Qt, QThread, Signal
    from PySide6.QtGui import QFont, QFontMetrics, QTextCursor
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
        QStyle,
        QStyleOptionButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
    # 「策略成绩单」对话框（按需打开，见模块 docstring 那一节）：放在这个 try 里面，
    # 与这一页同一个降级约定 —— 没有 Qt 就没有界面，那个模块也只需要在界面里存在。
    from laoa_trader.ui.scorecard_dialog import ScorecardDialog, ScorecardTarget

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 与 ui/app.py 同一个降级策略
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


#: 右侧点选面板的固定宽度。为什么要"固定"：这一列的宽度会不停变化的话，
#: 按钮文字会被压成 `M A (`，而按钮本身是这一页的核心交互 —— 宁可让左边的编辑区窄一点。
#:
#: 300 → 330（2026-09-18，用户要求"一行 4 列"）：4 列时每个按钮只有
#: `(宽度 - 滚动条 - 组框边距 - 列间距) / 4` 可用，实测最宽的那个标签
#: （「距上次几天」/「MACD快线」在 8pt 下 55px）加内边距需要 67px ——
#: 300 宽只能给到 66.8px（差一点点，字就会被截断），330 给到 74px 才稳。
PANEL_WIDTH = 330

#: 右侧面板**每行几个按钮**（用户 2026-09-18 要求一行 4 列）。
#: 52 个按钮按 2 列排要滚很久；4 列之后整块面板的高度大约减半，也更能一眼扫完。
PALETTE_COLUMNS = 4

#: 右侧按钮的高度（像素）。默认高度（约 26~30）下，四组 52 个按钮要把面板滚很久 ——
#: 用户 2026-09-18 要求"把按键大小都缩小"，于是高度降一档（30→22）；
#: 后来又要求"再缩小一下 + 一行 4 列"，于是再降到 20（字号仍只降一档，见下）。
BUTTON_HEIGHT = 20
#: 右侧那批按钮的 objectName（主题 QSS 用它单独给这批按钮设 padding / min-height，
#: 否则主题里通用的 `QPushButton { padding: 4px 12px; min-height: 20px }` 会把
#: `setFixedHeight(22)` 顶回 30px —— 实测过，别删）
PALETTE_BUTTON_OBJECT = "paletteButton"
#: 按钮字号相对默认字号降几档。只降一档是**故意的**：再小就费眼睛了
#: （这批按钮是给"刚上手的人"看的，宁可按钮矮一点、字要看得清）
BUTTON_FONT_DELTA = 1
#: 字号下限（再小就看不清了；某些平台默认字号本身就只有 8~9）
BUTTON_FONT_MIN = 7

#: 编辑区的最小高度（再小就看不见"最后一行是匹配条件"这件事了）
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

# ── 统一策略列表的两类行 ──
ROW_FORMULA = "formula"      # 公式（`formulas/` 目录里的文件，可改可删；随包的那几条也是它）
#: 「竞价策略」：**不是一个匹配策略**，而是把设置页那个「竞价扫描」搬进这张表 ——
#: 勾上 = 每个交易日 9:20 / 9:25 各扫一次全市场并把强势股推给你（写回 `intraday_auction`）。
#: 用户 2026-09-18 的要求原话："不是公式，是把「竞价扫描」做成策略"。
#: 为什么它必须与匹配策略**分开一类**：勾它不会往池子里加票、也不改变匹配结果，
#: 混在一起会让"勾上 = 参与匹配"这条规矩出现例外，而例外是用户最容易记错的东西。
ROW_AUCTION = "auction"
#: 这一行的 key（唯一；公式行的 key 是公式名）
AUCTION_KEY = "auction"
AUCTION_NAME = "竞价策略"

#: 列表列头（用户给定，**一个字都不加**）。
#: 2026-09-18 用户改过一次字：`名称 | 备注 | 状态` → `策略名称 | 说明 | 策略选取`
#: （"策略选取"这一列就是那个勾：勾上 = 这条策略参与匹配）。
LIST_COLUMNS: tuple[str, ...] = ("策略名称", "说明", "策略选取")

#: 右键菜单项文案
MENU_ENABLE = "启用"
MENU_DISABLE = "关闭"
MENU_DELETE = "删除"
#: 「竞价策略」那一行的删除项文案（它不可删，理由是"它是设置项，不是文件"）。
#: 注：老版本这里还有一条 `MENU_DELETE_BUILTIN`（内置策略不可删）——
#: 2026-09-18 起内置策略改成了随包公式，列表里没有"不可删的行"了（剩一条竞价策略），
#: 所以那条文案与 `OFF_GROUP_KEY`（"关掉全部策略"的写法）一起删掉。
MENU_DELETE_AUCTION = "删除（竞价策略是内置设置，不可删）"

#: 备注（写进 `# 说明:` 注释头）的长度上限。为什么这里要拦：
#: 注释头是**一行**，超长的说明会把文件第一屏占满，用户用记事本打开公式时
#: 得先翻过大段文字才看得见公式本身（同一个上限库里有 `MAX_DESC_CHARS`）。
MAX_NOTE_CHARS = formulas_lib.MAX_DESC_CHARS

#: 「本次匹配结果」表的列（用户 2026-09-18 要求把结果界面加回来）。
#: 来源列与「自选标的」那一列**同一个词**（`短期反转` / `放量上攻`，2026-09-23 起不带前缀）——
#: 两处说法不一样的话，用户没法拿它对账。
RESULT_COLUMNS: tuple[str, ...] = (
    "名称(代码)", "实时股价", "市值", "换手率", "来源", "加入自选",
)
#: 「加入自选」列的下标（不放数字字面量：列顺序改了也不会错位）
RESULT_ADD_COLUMN = RESULT_COLUMNS.index("加入自选")
#: 那一格的两个文字（已在自选里的行显示后者、且不可再点）
RESULT_ADD_TEXT = "加入自选"
RESULT_ADDED_TEXT = "已在自选"

#: 「加入自选」那一格：**文字每侧至少留这么多像素**（用户 2026-09-21 实报
#: "加入自选的框体有点小，字显示不全"）。
#: 为什么是"每侧留多少"而不是"按钮写死多少像素"：这一格的尺寸得按**当前字体**量
#: （Windows 125% 缩放下同一个字要宽 1.25 倍，写死的像素必然在某一档上切字）；
#: 量出来的文字宽度 + 这里的两侧留白 = 按钮的最小宽度，见 `_result_add_cell_size`。
#: 两侧留白的下限比主题 QSS 给的 12 像素略宽一点：主人要的就是"放宽一点"。
RESULT_ADD_SIDE_PADDING = 20
#: 上下同理（按钮高度不能小于"文字高度 + 上下留白"，否则字被上下切掉）
RESULT_ADD_V_PADDING = 8

#: 「本次匹配结果」表的 objectName。**只有一个用途**：让主题里那两条"多加一点内边距"
#: 的规则（`ui/theme.py` 的 `resultTable`）精确命中这一张表 —— 这一页的列宽是按内容算的
#: （`ResizeToContents`），内边距给得太紧时，换台机器（Windows 的微软雅黑）或系统缩放
#: 125% 就会出现"字被切掉半个"。别的表不在这条规则的射程内。
RESULT_TABLE_OBJECT = "resultTable"

#: 结果表每一列的口径（表头只有几个字，说明写这里）
RESULT_HEADER_TIPS: tuple[str, ...] = (
    "标的写法：名称(代码)",
    "实时股价：取实时快照；没有快照时用库里最近的收盘价并标注，都没有就显示 —",
    "流通市值，单位**亿**（取实时快照；取不到显示 —，不是 0）",
    "实时换手率，单位 %（取实时快照；取不到显示 —，不是 0）",
    "是哪条策略选出来的（与「自选标的」那一列同一个词）",
    "点这一格把这只票加进「自选标的」：之后它会一直留在池子里被盯盘，"
    "并记下加入时的价格用来算盈亏",
)

#: 结果页在**还没跑过匹配**时那句话
RESULT_HINT_IDLE = (
    "这里显示**本次匹配结果**（平时隐藏）：点右上角【开始匹配】跑一轮，跑完结果就出现在这里，"
    "可以【一键加入自选】或【导出结果到桌面】。"
)

#: 顶部那行灰字说明（用户打开这一页先看到的东西）。
#: **必须写明结果去哪了**：这一页不再显示匹配结果（用户要求），
#: 不写的话用户点完【开始匹配】会以为"什么都没发生"。
PAGE_HINT = (
    "勾「策略选取」列 = 这条策略参与匹配（只有「竞价策略」那一行例外：它开关的是盘中"
    "竞价扫描，不参与匹配）；单击一行看详情（策略会载入编辑器）；右键 = 启用 / 关闭 / 删除；"
    "点【开始匹配】跑一轮 —— 这一块会变成【本次匹配结果】，可以一键加入自选、导出到桌面，"
    "结果同时也会进「自选标的」并自动导出一份到桌面。"
)

#: 策略列表上方那句灰字。
#: 2026-09-18（用户要求）：这里原来写的是"内置 5 条固定在前（备注列是它的实测证据，只读）"；
#: 那 5 条内置策略改成了随包公式，所以现在列表就是"竞价策略 + 公式"两段。
LIST_HINT = (
    "策略列表：最上面那条「竞价策略」开关的是盘中竞价扫描（只做提示、不参与匹配）；"
    "下面全是策略 —— 随包预置的那几条与你自己写的一条待遇相同（都能改、能删、能勾选），"
    "「说明」列来自策略文件里的「# 说明:」。"
)

#: 编辑器里那行灰字说明（小白第一眼看的就是它）
EDITOR_HINT = (
    "点右边的按钮就能插入；最后一行是匹配条件。"
    "写完点【校验】→【运行】→【保存】；想留一份结果就点【导出匹配结果】。"
)

#: 【新策略】按钮上的字（旧名【载入示例】，2026-09-21 主人要求改名；行为不变）。
SAMPLE_BUTTON_TEXT = "新策略"

#: 【新策略】在没有示例文件时用的兜底公式（保证"小白第一步"一定走得通）
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
    #    匹配本来就看最后一根 K 线，直接写比较就行；取不到快照时条件不成立（不出票）。
    ("流通市值", "插入 流通市值（亿元，来自实时快照）。"
                "例：流通市值>=10 AND 流通市值<=300", None, "流通市值"),
    ("换手率", "插入 换手率（%，来自实时快照）。例：换手率>5", None, "换手率"),
    # ── 离线算出来的字段：不用联网、历史上也成立
    ("热门行业", "插入 热门行业：最近 3 个交易日里这只票的行业上过几次热门榜（0~3，"
                "口径与「大盘概览 → 热门板块」同一套）。例：热门行业>=1 表示只选上过榜的行业",
     None, "热门行业"),
    # ── 盘中快照口径（2026-09-23 加，用户："必须加进去啊"）──
    # 语义是"**现在这一刻**的盘面"：盘中点【运行】/【开始匹配】时取当时的实时快照，
    # 而不是日线最后一根（上一个收盘日）的值。所以：
    #   * 非交易时段取不到 → 条件不成立、一只都不出（tooltip 里必须写明，否则用户
    #     会以为公式写错了）；
    #   * 快照只有"当下"这一个值 → **没有历史、不能回测**，这也是必写的限制。
    ("现价", "插入 现价：**盘中**最新价（元），你在盘中点【运行】时取当时的值。"
             "例：现价<50。⚠️ 非交易时段取不到（条件不成立）；没有历史、不能回测", None, "现价"),
    ("现涨幅", "插入 现涨幅：**盘中**当日涨跌幅（%，3.2 表示 +3.2%）。"
               "例：现涨幅>=1 AND 现涨幅<=5。⚠️ 非交易时段取不到；没有历史、不能回测",
     None, "现涨幅"),
    ("现量比", "插入 现量比：**盘中**量比（倍）。例：现量比>5 表示当前放量到 5 倍以上。"
               "⚠️ 非交易时段取不到；没有历史、不能回测", None, "现量比"),
    ("现换手", "插入 现换手：**盘中**换手率（%）。例：现换手>=3 AND 现换手<=8。"
               "⚠️ 非交易时段取不到；没有历史、不能回测", None, "现换手"),
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
    # 空格（用户 2026-09-21 要求"最后加个空格按键，点击就输入空格"）：
    # 插进去的就是一个空格字符，写在最后是因为它不属于"运算符"的语法，只是排版用。
    # 放在这一组而不是别处的原因：与前几个一样是"往光标处塞一个字符"，
    # 而变量/函数那两组都是有语义的字段与调用。
    (" ", "插入一个空格（策略里空格只影响可读性，不影响计算）", None, "空格"),
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
    """统一列表里的一行：**公式**与「竞价策略」那一行共用同一个结构。

    为什么要合成一种行：列表是同一张表（用户给定），而两类行的交互不同
    （公式可改可删、能载入编辑器；竞价策略只读、开关的是盘中扫描）。
    用 `kind` 区分、把差别写进数据里，表格与右键菜单就都只认这一种结构。

    注（2026-09-18）：老版本这里还有第三类 `ROW_BUILTIN`（写在 `strategy/rules.py`
    里的 5 条 Python 策略）。用户要求把它们改成随包公式（可改可删）之后，
    列表里不再有"只读的内置策略行"，那一整条路（含 `builtin_*` 那几个函数）随之删掉。
    """

    #: `ROW_FORMULA` / `ROW_AUCTION`
    kind: str
    #: 公式 = 公式名（= 文件名）；竞价 = `AUCTION_KEY`
    key: str
    #: 展示名（公式 = 公式名）
    name: str
    #: 「说明」列的文本
    note: str
    #: 「说明」列的 tooltip（完整说明/错误全文）
    note_tip: str
    #: 是否参与匹配（公式查 `enabled_formulas`；竞价策略查 `intraday_auction` ——
    #: 它不是匹配策略，这条注释只说明"状态从哪来"）
    enabled: bool
    #: 只读行的详情（只有竞价策略那一行用）
    detail: str = ""
    #: 公式行对应的 `FormulaSpec`；竞价行为 None
    spec: Any = None

    @property
    def is_auction(self) -> bool:
        """是不是「竞价策略」那一行（内置的竞价扫描开关，**不参与匹配**）。"""
        return self.kind == ROW_AUCTION

    @property
    def read_only(self) -> bool:
        """不是公式文件（只剩竞价策略那一行）：单击只展开只读详情、不能删、不能改。"""
        return self.kind == ROW_AUCTION


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
# 所以这一行**不是一个匹配策略**：勾上它 = 开启竞价扫描（9:20 / 09:25 各扫一次全市场、
# 按下面这套口径打分推送），写回的是 `config.toml` 的 `intraday_auction`，
# 与 `enabled_formulas` 不相干（公式那一列写的是它）。
#
# 口径里的数字**全部现读 `cfg`**（设置页那几个框写进去的就是它们），界面自己一个都不编：
# 下面那几个默认值只在 `cfg` 是测试替身、没有这些字段时兜底，与 `config.py` 的出厂值一致。

def auction_enabled(cfg: Any) -> bool:
    """竞价扫描开着没有（= 这一行的「策略选取」列）。"""
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
    """「说明」列那句话：**先说清它不参与匹配**，再说口径。

    为什么把"不参与匹配"放在最前面：备注列是 Stretch 的，窗口一窄就会被省略号截掉 ——
    而这半句正是这一行最要紧的东西（用户看到"竞价策略"四个字，第一反应必然是
    "它也会给我匹配吗"）。把结论写在能被看见的位置，比藏在末尾强。
    """
    s = _auction_settings(cfg)
    return (
        "只做盘中提示、不参与匹配 ｜ "
        f"{' / '.join(s['at'])} 扫全市场：涨幅 {s['min_pct']:.1f}~{s['max_pct']:.1f}%、"
        f"量比≥{s['ratio']:.1f}、成交额≥{s['amount_wan']:.0f}万、打分≥{s['score']}"
        f" → 推前 {s['items']} 只"
    )


def auction_note_tip(cfg: Any) -> str:
    """「说明」列的 tooltip：口径全文 + 两条硬限制（不参与匹配 / 无法回测）。"""
    lines = [
        f"{AUCTION_NAME}（{AUCTION_KEY}）：它不是匹配策略，而是「系统设置 → 竞价扫描」"
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
        "⚠️ 它**不参与匹配**：勾上它不会往「自选标的」里加票，也不改变匹配结果。",
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
        "写回的配置键：intraday_auction（勾「策略选取」列或右键【启用】即写回 config.toml）",
        "",
        "── 口径（数字就是设置页那几个框，界面不另算）──",
    ]
    lines += ["· " + item for item in _auction_criteria(cfg)]
    lines += [
        "",
        "── 它是什么、不是什么 ──",
        "· 是：**盘中提示**。9:15–9:25 的真实买卖盘是全市场唯一能看到「今天谁在抢」的数据；",
        "  9:25 那一枪拿到的是**竞价终态**，命中直接推到浮窗/托盘（点详情看全部命中）。",
        "· 不是：**匹配策略**。它**不参与匹配** —— 勾上不会往「自选标的」加票，"
        "也不会改变【开始匹配】的结果；",
        "  要按自己的条件匹配，就勾上列表里那几条策略（随包的也在里面），或自己写一条。",
        "",
        "── 两条硬限制 ──",
        "1. 竞价数据**没有历史**（接口只给当天 stage=live/final，不接受日期）→ **无法回测**，",
        "   想验证只能实盘跑一段时间记录；",
        "2. 竞价指标与「当日后期涨停」的相关性只有 2 倍随机（本项目实测样本），",
        "   覆盖率低 —— 所以它只够当提示，不够当匹配信号。",
        "",
        "参数怎么改：设置页「竞价扫描」那一组（涨幅上下限、成交额、量比、板块、打分、条数、扫描时刻）。",
    ]
    return "\n".join(lines)


def auction_row(cfg: Any) -> StrategyRow:
    """「竞价策略」那一行的数据（它是列表的**第一行**，其余全是公式）。"""
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
    """公式行「说明」列的 (文本, tooltip)。

    备注 = 公式文件里的 `# 说明:`（保存时界面写进去的），再叠加两类**必须让人看见**的问题：

    * 语法错（`⛔ 语法错：…`）—— 它勾上也跑不了（`formulas.enabled_names()` 会跳过它），
      所以不能只藏在提示里；完整错误（带行号列号）进 tooltip；
    * 上一次运行的运行期错误（`⚠️ 运行时出错：…`，`formula_group.last_status()`）。

    为什么把这两条放「说明」列而不是「策略选取」列：列头只有三格，
    而「策略选取」列被"参与匹配"这个勾占满了（一格一义），
    把错误塞进同一格会让"这一格到底能不能点"变得要猜。
    """
    note = str(getattr(spec, "description", "") or "").strip()
    tip = note
    if not spec.ok:
        full = str(spec.error_text or "").strip()
        note = (note + " ｜" if note else "") + "⛔ 语法错：" + _one_line(full, 60)
        tip = (tip + "\n\n" if tip else "") + "⛔ 这条策略现在编译不过：\n" + full
    if runtime_error:
        note = (note + " ｜" if note else "") + "⚠️ 运行时出错：" + _one_line(runtime_error, 40)
        tip = (tip + "\n\n" if tip else "") + "⚠️ 上一次匹配时出错：\n" + str(runtime_error)
    return (note or "—"), tip


def build_strategy_rows(
    cfg: Any, specs: Sequence[Any], runtime: dict[str, str] | None = None
) -> list[StrategyRow]:
    """把「竞价策略那一行 + 目录里的公式」拼成统一列表的数据。

    读取的**全是已有来源**：状态来自 `cfg.enabled_formulas` 与 `cfg.intraday_auction`，
    说明来自公式文件的注释头与竞价的配置数值。

    为什么第一行是竞价策略（2026-09-18）：老版本这里先是 5 条内置策略、再是公式；
    用户把内置策略改成了随包公式，于是公式成了列表的主体，而「竞价策略」被放在**最前面
    一行**（它是唯一"不是匹配策略"的行，放最上面一眼就能看见，不会混进公式里）。
    """
    runtime = runtime or {}
    known_formulas = set(str(n) for n in (getattr(cfg, "enabled_formulas", None) or []))

    rows: list[StrategyRow] = [auction_row(cfg)]
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

        （旧版这里还兼跑"成绩单"。成绩单现在的入口是**按需打开的对话框**
        `ui/scorecard_dialog.py`，它有自己的线程 —— 那边要逐条回报进度，
        与本线程"一次返回一个 dict"不是一件事，所以两边各留一个，不硬合并。）
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
                logger.exception("策略后台任务失败")
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
        """「策略匹配」页。

        属性里刻意留着测试与主窗口要用的引用（`name_edit` / `note_edit` / `editor` /
        `hint_label` / `table` / `rows` / `palette_buttons` / `preview_worker`），
        不要去爬控件层级 —— 这一页的控件多，按层级取值的测试一改布局就集体失效。
        """

        #: 【开始匹配】被点 → **只举手**，真正的流程在主窗口（见 `on_start_pick`）
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
            #: 统一列表的行（竞价策略那一行 + 公式，与表格行号一一对应）
            self.rows: list[StrategyRow] = []
            #: 右侧面板按钮：{token: QPushButton}（测试按 token 点，不爬布局）
            self.palette_buttons: dict[str, Any] = {}
            #: 【运行】（原【试算】，下同）的后台线程；跑完置回 None
            #: （测试就等这一条来判断"落地了"）
            self.preview_worker: FormulaWorker | None = None
            #: 最近一次打开的【成绩单】对话框（它自己管自己的后台线程与预检；
            #: 这里留个引用只是为了别让 Qt 在 `exec()` 返回时把它立刻回收掉）
            self.scorecard_dialog: Any = None
            #: 【运行】按下按钮那一刻的公式快照（结果属于它，不属于编辑框里现在的内容）
            self.preview_formula: Any = None
            #: **上一次【运行】的结果**（【导出匹配结果】导的就是它）：
            #: `{"name": 公式名, "date": 行情日, "hits": [{"symbol","name"}...], "count": N}`
            #: 或 None（还没跑过 / 跑失败了）。
            #:
            #: 为什么导出要用"上一次的结果"而不是重新跑一遍：重跑可能因为盘中快照、
            #: 数据更新而给出**与用户刚才看到的那份不一样**的清单 —— 他导出的必须是
            #: 他看过的。另外重跑一次要扫全库（实测 7~8 秒），点一下按钮等 8 秒不像话。
            #: 每次开始新一轮【运行】时先清空它：**绝不导出上一轮的陈结果**。
            self.last_run: dict | None = None
            self.hint_text: str = ""
            #: 【策略编辑】的**闸门**（`ui/app.py` 挂上来的回调，见 `on_open_editor`）：
            #: 返回空串 = 放行；返回中文原因 = 已拦下（未授权时由主窗口弹授权对话框）。
            #: 为什么做成回调而不是页面自己判断授权：授权逻辑只允许有一处
            #: （`licensing`），这一页不该知道"授权"这件事怎么算。
            self.open_editor_guard: Any = None
            #: 公式行的勾选框（键 = 公式名）。竞价那一行单独存在 `_auction_box`
            #: （只有一行，不值当再开一个字典）。
            self._row_boxes: dict[str, Any] = {}
            #: 「竞价策略」那一行的勾选框（只有一行，所以不放进上面两个字典）
            self._auction_box: Any = None
            #: **本次匹配结果**（结果页那张表的行）：`[{"symbol","name","label"}...]`。
            #: 平时是空的；点【开始匹配】跑完由主窗口调 `show_pick_result()` 填进来。
            #: 取实时快照的函数（主窗口注入 `QuotesService.quote`；没注入时实时列显示 —）
            self.quotes_provider: Any = None
            self.result_rows: list[dict] = []
            #: 本次结果的行情日（导出文件与结论那句都用它）
            self.result_date: Any = None
            #: 载入行时别把"选中变化"当成用户点击，也别让刷列表打开编辑器
            self._loading = False
            #: 当前展开的**只读详情**（现在只有竞价那一行；刷列表后要跟着更新）
            self._detail_key = ""

            self._build_ui()
            self.reload()

        # ── 界面搭建 ──────────────────────────────────────────────────

        def _build_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 12)
            layout.setSpacing(8)

            # ── 顶部一行：【策略编辑】【开始匹配】 + 一句灰字说明 ──
            #
            # 为什么操作按钮在这一页而不是标题区：开发文档的原则是"标题区不再放操作按钮，
            # 数据在「系统设置」、匹配在「策略匹配」" —— 点下去会发生什么，在这一页看得见。
            top = QHBoxLayout()
            self.btn_edit = QPushButton("策略编辑")
            self.btn_edit.setToolTip(
                "打开策略编辑器：左边写策略、右边点按钮插入（点列表里的策略行也会打开它）"
            )
            # 用 lambda 吞掉 `clicked` 带来的 checked 参数：直接接 `on_open_editor`
            # 的话那个 `False` 会被当成 spec 传进去（Qt 的经典坑，本文件里所有
            # 带参数的槽都这么接）
            self.btn_edit.clicked.connect(lambda _checked=False: self.on_open_editor())
            top.addWidget(self.btn_edit)

            self.btn_start_pick = QPushButton("开始匹配")
            self.btn_start_pick.setObjectName("primaryAction")   # 主操作按钮（主题精确命中）
            self.btn_start_pick.setToolTip(
                "按上面勾选的策略跑一轮：结果直接进「自选标的」"
                "（在那一页右键删除、或手工再添加），并同时往桌面导出一个结果文本文件，"
                "最后按「系统设置」里的通知方式发一条消息"
            )
            self.btn_start_pick.clicked.connect(self.on_start_pick)
            top.addWidget(self.btn_start_pick)

            # 【成绩单】：**按需**入口（不常驻面板 —— 理由见模块 docstring）。
            # tooltip 必须把代价说在前面：它要扫全库（几十秒量级），而且出厂那 6 个月
            # 的数据**必然**得出"样本不足"—— 不说清楚，用户会以为程序算错了。
            self.btn_scorecard = QPushButton("成绩单")
            self.btn_scorecard.setToolTip(
                "按需算一遍历史成绩单：把上面勾选的策略在当前库里逐条回测，"
                "要扫全库，几十秒量级；数据不足 1 年（250 个交易日）时结论不可用 "
                "—— 打开后会先告诉你库里的数据够不够、不够怎么补"
            )
            self.btn_scorecard.clicked.connect(lambda _checked=False: self.on_scorecard())
            top.addWidget(self.btn_scorecard)

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

            # 编辑器 / 只读详情默认都收起来：用户给定的是"点击打开策略编辑器"
            self.bottom_stack.setVisible(False)

        def _build_list_side(self) -> Any:
            """上半：**只有策略列表**（名称 | 备注 | 状态）。

            为什么这里只剩一张表（用户原话："策略匹配只要显示策略，不显示匹配结果"）：
            旧版下半还挂着一块「本次匹配结果」表 + 一句话结论 + 【全部加为自选】，
            而同一批票本来就有一张更该看的表 —— 「自选标的」页（结果就在那里，
            还能右键删、手工加）。摆两块显示同一批票只会让用户不确定"哪一块说了算"。
            """
            # 2026-09-18（用户要求）：列表与"本次匹配结果"**同一个位置两块页面** ——
            # 平时显示策略列表；点【开始匹配】切到结果页，跑完就在那里看结果、
            # 一键加入自选、导出到桌面；点【返回策略列表】回来。
            # 为什么用 QStackedWidget 而不是把结果表藏在下面：用户明确说
            # "策略列表界面变为匹配结果界面（平时隐藏）" —— 同一个位置切换，不占两份高度。
            stack = QStackedWidget()
            self.list_page = self._build_list_page()
            self.result_page = self._build_result_page()
            stack.addWidget(self.list_page)
            stack.addWidget(self.result_page)
            stack.setCurrentWidget(self.list_page)
            self.list_stack = stack
            return stack

        def _build_list_page(self) -> Any:
            """策略列表页（上半的主视图）。"""
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
            self._set_header_tooltip(0, "策略的名字（就是策略文件的名字）")
            self._set_header_tooltip(1, "说明：来自策略文件里的「# 说明:」，或者在编辑器里填的备注")
            self._set_header_tooltip(2, "勾上 = 参与匹配（写回 config.toml 的 enabled_formulas）；"
                                        "竞价策略那一行例外：它开关的是盘中竞价扫描")
            self.table.itemSelectionChanged.connect(self.on_row_selected)
            layout.addWidget(self.table, 1)

            return side

        def _build_result_page(self) -> Any:
            """匹配结果页（**平时隐藏**；点【开始匹配】或跑完匹配才切过来）。

            用户 2026-09-18 的要求（推翻了 2026-09-17 的"这一页不显示结果"）：
            "匹配状态时，策略列表界面变为匹配结果界面（匹配结果界面平时隐藏），
            结果可以一键加入自选和导出。"

            所以这一页只有三样东西：一句结论（几只 / 行情日 / 有没有错）、
            结果表、三个按钮（加入自选 / 导出到桌面 / 返回策略列表）。
            它与「自选标的」页**不冲突**：匹配本来就会把票写进股池（那是最完整的表，
            能删能加），这里只是"刚跑完的这一批"的现场，方便立刻加自选或导出。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            self.result_hint = QLabel(RESULT_HINT_IDLE)
            self.result_hint.setObjectName("statusTag")
            self.result_hint.setWordWrap(True)
            self.result_hint.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(self.result_hint)

            self.result_table = QTableWidget(0, len(RESULT_COLUMNS))
            # objectName 是给主题 QSS 用的：只给**这一张表**多加一点单元格/表头内边距
            # （见 `ui/theme.py` 的 `resultTable` 规则）—— 别的表一律不动。
            self.result_table.setObjectName(RESULT_TABLE_OBJECT)
            self.result_table.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
            self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.result_table.setToolTip(
                "本次匹配选出来的票（按池子分数排序）。这张表是「刚跑完的这一批」的现场，"
                "完整的池子看「自选标的」页"
            )
            header = self.result_table.horizontalHeader()
            for column, mode in enumerate((
                QHeaderView.ResizeMode.ResizeToContents,      # 名称(代码)
                QHeaderView.ResizeMode.ResizeToContents,      # 实时股价
                QHeaderView.ResizeMode.ResizeToContents,      # 市值
                QHeaderView.ResizeMode.ResizeToContents,      # 换手率
                QHeaderView.ResizeMode.Stretch,               # 来源（最长，吃掉余宽）
                QHeaderView.ResizeMode.ResizeToContents,      # 加入自选（按钮）
            )):
                header.setSectionResizeMode(column, mode)
            # 每一列的意思写进表头 tooltip（列头只有几个字，放不下口径）
            for column, tip in enumerate(RESULT_HEADER_TIPS):
                item = self.result_table.horizontalHeaderItem(column)
                if item is not None:
                    item.setToolTip(tip)
            layout.addWidget(self.result_table, 1)

            row = QHBoxLayout()
            self.btn_add_all = QPushButton("一键加入自选")
            self.btn_add_all.setToolTip(
                "把这些票写进「自选标的」（只写本地库、不联网）：以后它们即使不进池也留在"
                "自选表里；已经在自选里的不会重复添加，也不会覆盖你写过的备注"
            )
            self.btn_add_all.clicked.connect(self.on_add_all_to_watchlist)
            row.addWidget(self.btn_add_all)

            self.btn_export_result = QPushButton("导出结果到桌面")
            self.btn_export_result.setToolTip(
                "把这张表里的票写成一个文本文件放到桌面："
                "luweik-匹配结果-<今天>.txt（与【开始匹配】自动导出的那份同一个文件名，"
                "同一天会覆盖它）"
            )
            self.btn_export_result.clicked.connect(self.on_export_result)
            row.addWidget(self.btn_export_result)

            self.btn_back_to_list = QPushButton("返回策略列表")
            self.btn_back_to_list.setToolTip("回到策略列表（结果还留着，下次匹配会刷新）")
            self.btn_back_to_list.clicked.connect(self.on_back_to_list)
            row.addWidget(self.btn_back_to_list)
            row.addStretch(1)
            layout.addLayout(row)
            return page

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
            # 2026-09-21（主人要求）：按钮文字从【载入示例】改成【新策略】——
            # **行为一个字都没变**（还是把一条能跑通的公式放进编辑框），
            # 只是"载入示例"这四个字让新用户以为是"看示例"而不是"开始写一条新的"。
            self.btn_sample = QPushButton(SAMPLE_BUTTON_TEXT)
            self.btn_sample.setToolTip("新建一条策略：把一条能跑通的策略放进编辑框，照着改就行")
            self.btn_sample.clicked.connect(self.on_load_sample)
            head.addWidget(self.btn_sample)
            self.btn_close_editor = QPushButton("收起编辑器")
            self.btn_close_editor.setToolTip("收起这一块（策略已经保存的不会丢）")
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
            name_row.addWidget(QLabel("策略名称："))
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("例如：5日线上放量")
            self.name_edit.setToolTip("这就是保存后的文件名，也是池子/推送里显示的名字")
            name_row.addWidget(self.name_edit, 1)

            self.btn_save = QPushButton("保存")
            self.btn_save.setObjectName("primaryAction")     # 主操作按钮（主题里精确命中）
            self.btn_save.setToolTip("把编辑框里的策略存到策略目录（重名会先问一句）")
            self.btn_save.clicked.connect(self.on_save)
            name_row.addWidget(self.btn_save)

            self.btn_save_as = QPushButton("另存为")
            self.btn_save_as.setToolTip("换一个名字再存一份（原来的那份不动）")
            self.btn_save_as.clicked.connect(self.on_save_as)
            name_row.addWidget(self.btn_save_as)

            self.btn_delete = QPushButton("删除")
            self.btn_delete.setToolTip("删掉当前这条策略文件（会先问一句；内置策略不能删）")
            self.btn_delete.clicked.connect(self.on_delete)
            name_row.addWidget(self.btn_delete)
            layout.addLayout(name_row)

            note_row = QHBoxLayout()
            note_row.addWidget(QLabel("备注："))
            self.note_edit = QLineEdit()
            self.note_edit.setPlaceholderText("这条策略是干什么的（可留空 —— 留空则自动填「用到的字段/函数」）")
            self.note_edit.setToolTip(
                "会写进策略文件的「# 说明:」注释头，列表的「说明」列显示的就是它；"
                f"最长 {MAX_NOTE_CHARS} 字，不能换行（注释头只有一行）"
            )
            note_row.addWidget(self.note_edit, 1)
            layout.addLayout(note_row)

            # ── 编辑框 ──
            self.editor = QPlainTextEdit()
            self.editor.setPlaceholderText(
                "在这里写策略，例如：\nM5:=MA(C,5)\nC>M5 AND V>MA(V,5)*1.5"
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
            self.btn_validate.setToolTip("检查策略写得对不对；通过时会告诉你用到哪些字段/函数")
            self.btn_validate.clicked.connect(self.on_validate)
            action_row.addWidget(self.btn_validate)

            # 【运行】= 原来的【试算】（用户要求改这两个字："试算"看不懂）。
            # **属性名仍是 `btn_preview`**：它连着一批测试与文档里的引用，
            # 为一个文案做机械重命名只会制造噪音（见模块 docstring 的同一段说明）。
            self.btn_preview = QPushButton("运行：当前库能选出几只")
            self.btn_preview.setToolTip(
                "在当前库上跑一遍这条策略，看看最近一个交易日命中几只。\n"
                "不推送、不写库（结果不会进「自选标的」）"
            )
            self.btn_preview.clicked.connect(self.on_preview)
            action_row.addWidget(self.btn_preview)

            self.btn_export = QPushButton("导出匹配结果")
            self.btn_export.setToolTip(
                "把上一次【运行】命中的全部股票写成一个文本文件，放在桌面上：\n"
                "luweik-匹配结果-<今天>.txt（同一天再导出会覆盖这一个文件）"
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
            # 「排除」是匹配末尾才补的那几个条件（不是每个人都用），所以排在最后。
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
            """一组按钮（`PALETTE_COLUMNS` 列网格，现在是一行 4 个）。
            每个按钮一个中文 tooltip（是什么 + 一个例子）。

            元素是四元组 `(插入的文本, tooltip, 参数个数, 按钮上的字)`：前三项管
            "点下去发生什么"，第四项管"按钮上写什么"（中文），两者**故意分开** ——
            见上面 `VARIABLES` 的注释。

            按钮**做得比默认小**（用户 2026-09-18 要求"把按键大小都缩小"、"一行 4 列"）：
            四组一共 52 个按钮，用默认大小/两列时右侧面板要滚很久才看得全；
            字号也跟着降一档 —— **不能再小**了，否则中文标签会被挤成 "…"
            （宽度够不够有测试按字体实际量出来，见 `test_formula_page`）。
            """
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setSpacing(3)
            # 组框内边距压到 4/3：4 列之后横向空间很紧，省下的都给按钮宽度
            grid.setContentsMargins(4, 3, 4, 3)
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
                grid.addWidget(button, index // PALETTE_COLUMNS, index % PALETTE_COLUMNS)
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

            主窗口在匹配完成后会调它（`_on_pipeline_done`），所以这里**绝不碰编辑区**：
            用户可能正开着编辑器改公式，刷一次列表就把他的草稿换掉是不可接受的
            （旧版 `reload()` 会顺手选中第一行并载入编辑框 —— 在"列表在上、编辑器按需展开"
            的布局里那会变成"每次选完股，编辑器自己弹出来并顶掉我在写的东西"）。

            这里**也不再刷"本次结果"区**（那块界面已经按用户要求删掉，见模块 docstring
            「匹配结果去哪了」）：结果在「自选标的」页与桌面文件里，不在这一页。
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
            self._row_boxes = {}
            self._auction_box = None
            self.table.setRowCount(len(self.rows))
            for index, row in enumerate(self.rows):
                name_item = QTableWidgetItem(row.name)
                if row.is_auction:
                    name_item.setToolTip(
                        "内置的「竞价扫描」开关（不是匹配策略）——勾上 = 每个交易日到点"
                        "自动扫全市场并推送，写回 config.toml 的 intraday_auction"
                    )
                else:
                    name_item.setToolTip(f"策略文件：{getattr(row.spec, 'path', '')}")
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

                if row.is_auction:
                    self._auction_box = box
                else:
                    self._row_boxes[row.key] = box

        @staticmethod
        def _box_tooltip(row: StrategyRow) -> str:
            """勾选框的 tooltip：**勾上会发生什么、写回哪个键**，逐类说清。

            2026-09-18 起只剩两类：公式（写 `enabled_formulas`）与竞价策略
            （写 `intraday_auction`）—— 老版本那条"内置策略写 enabled_groups +
            enabled_strategies"随内置行一起删掉了。
            """
            if row.is_auction:
                return (
                    "勾上 = 开启竞价扫描（写回 config.toml 的 intraday_auction）："
                    "每个交易日 09:20 / 09:25 各扫一次全市场，强势股直接推到浮窗/托盘\n"
                    "它**不参与匹配** —— 不会往「自选标的」加票，也不改变匹配结果；"
                    "参数在「系统设置 → 竞价扫描」那一组里改"
                )
            return (
                "勾上 = 这条策略参与匹配（写回 config.toml 的 enabled_formulas）；"
                "默认不勾 —— 你自己的策略要不要用，由你决定"
            )

        def _update_list_hint(self) -> None:
            """列表上方那句灰字：默认文案；勾了但**找不到文件/语法错**的公式在这里点名。

            为什么这条值得单独盯：`enabled_formulas` 里写了一个目录里没有的公式名
            （用户手改了 config.toml、或者把公式文件删了/改名了），
            表现是"列表里一个勾都没有、点匹配什么都没跑"，而日志在他看不见的地方。
            （老版本这里盯的是 `enabled_groups` / `enabled_strategies` 的"交集为空"；
            那两个键 2026-09-18 已退役，`formulas.enabled_names()` 会跳过认不出的名字。）
            """
            known = set(str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or []))
            files = {spec.name for spec in self.specs}
            missing = sorted(name for name in known if name not in files)
            if missing:
                self.list_hint.setText(
                    "⚠️ config.toml 里勾了这几条策略，但策略目录里找不到（这会让勾选与实跑"
                    "不一致）：" + "、".join(missing)
                    + "　改完点【开始匹配】前先看一眼勾选是否与预期一致。"
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
                self.show_auction_detail(row)
            else:
                self.on_open_editor(row.spec)

        def show_auction_detail(self, row: StrategyRow) -> None:
            """展开**只读行**的详情 —— 现在只有「竞价策略」那一行会走到这里。

            （2026-09-18 之前这个方法叫 `show_builtin_detail`，还要负责内置策略的
            "条件 + 证据"；内置策略改成随包公式之后，那种行不存在了。）
            """
            self._detail_key = row.key
            self.detail_title.setText(f"{row.name}（{row.key}）—— 内置的竞价扫描开关，只读")
            self.detail_text = row.detail or auction_detail(self.cfg)
            self.detail_view.setPlainText(self.detail_text)
            self.bottom_stack.setCurrentWidget(self.detail_page)
            self.bottom_stack.setVisible(True)
            self._set_hint(
                f"「{row.name}」不是匹配策略，是盘中提示的开关：勾「策略选取」列（或右键"
                "【启用】）就开启竞价扫描，每个交易日到点自动扫全市场。\n"
                "它不会往「自选标的」加票、也不改变匹配结果；"
                "涨幅 / 量比 / 成交额那些参数在「系统设置 → 竞价扫描」里改。"
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
                # 竞价那一行的"启用/关闭"开关的是**盘中扫描**，不是匹配 ——
                # 菜单文案照实说，否则用户会以为勾上就能进池子
                if row.enabled:
                    toggle = menu.addAction(MENU_DISABLE)
                    toggle.setToolTip("关闭竞价扫描（下次开盘不再自动扫全市场、不再推提醒）")
                else:
                    toggle = menu.addAction(MENU_ENABLE)
                    toggle.setToolTip(
                        "开启竞价扫描：每个交易日 09:20 / 09:25 各扫一次全市场，"
                        "强势股推到浮窗/托盘（不参与匹配）"
                    )
            elif row.enabled:
                toggle = menu.addAction(MENU_DISABLE)
                toggle.setToolTip(f"让「{row.name}」退出匹配（下次【开始匹配】不再跑它）")
            else:
                toggle = menu.addAction(MENU_ENABLE)
                toggle.setToolTip(f"让「{row.name}」参与匹配（下次【开始匹配】就会跑它）")
                if row.spec is not None and not row.spec.ok:
                    # 语法错的公式**勾上也跑不了**（`formulas.enabled_names()` 会跳过它），
                    # 所以"启用"这一项置灰并说明先修公式 —— 而不是让用户勾上之后
                    # 什么都看不到（那会变成"我勾了公式，池子里却没有"这种最难查的现象）
                    toggle.setEnabled(False)
                    toggle.setToolTip(
                        "这条策略现在编译不过，勾上也不会参与匹配：先在编辑器里改好、"
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
            else:
                delete.setToolTip(f"删掉策略文件「{row.key}」（会先问一句）")
                delete.triggered.connect(
                    lambda _checked=False, r=row: self.on_delete_formula(r.key)
                )
            return RowMenu(menu=menu, toggle=toggle, delete=delete)

        # ── 「参与匹配」写回 config.toml ──────────────────────────────

        def set_row_enabled(self, kind: str, key: str, checked: bool) -> None:
            """统一的"启用/关闭"入口（勾选框与右键菜单都走这里 → 行为必然一致）。"""
            if kind == ROW_AUCTION:
                self.on_toggle_auction(checked)
            else:
                self.on_toggle_enabled(key, checked)

        def on_toggle_enabled(self, name: str, checked: bool) -> None:
            """公式的「参与匹配」 → 写回 `enabled_formulas`（保留注释与未知键）。"""
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
                    f"✅ 「{name}」已加入匹配（写回 {path.name}）。\n"
                    "下次【开始匹配】时它会作为「策略」组参与：进池的票来源会标成"
                    f"「{name}」。"
                )
                # 成功**不弹提示**（2026-09-18 用户："软件操作的一些提醒都不需要"）；
                # 列表里那个勾 + 提示区那句话本身就是反馈
            else:
                self._set_hint(f"「{name}」已退出匹配（{path.name} 里的 enabled_formulas 已更新）")

        def on_toggle_auction(self, checked: bool) -> None:
            """「竞价策略」的启用/关闭 → 写回 `intraday_auction`（**不是**匹配开关）。

            为什么它写的是另一个键：这一行管的不是"匹配时跑不跑"，而是"开盘后要不要
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
                    "⚠️ 它不参与匹配：不会往「自选标的」加票，也不改变匹配结果；"
                    "竞价数据没有历史，所以它只能当盘中提示、无法回测。\n"
                    "参数在「系统设置 → 竞价扫描」里改。"
                )
            else:
                self._set_hint(
                    f"竞价策略已关闭（{path.name} 里的 intraday_auction=false）："
                    "开盘后不再自动扫描、不再推竞价提醒。"
                )

        def _row_box(self, kind: str, key: str) -> Any:
            if kind == ROW_AUCTION:
                return self._auction_box
            return self._row_boxes.get(key)

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

            公式与竞价那一行**都要刷**：右键菜单的文案是"按当前状态只出现【启用】或【关闭】"，
            行数据不跟着配置走的话，用户右键会看到与事实相反的菜单项
            （刚勾上它，菜单却说【启用】）。
            """
            enabled_formulas = set(
                str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or [])
            )
            auction_on = auction_enabled(self.cfg)

            def _enabled(row: StrategyRow) -> bool:
                if row.is_auction:
                    return auction_on
                return row.key in enabled_formulas

            def _detail(row: StrategyRow) -> str:
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
            """打开公式编辑器（点【策略编辑】按钮，或点列表里的公式行）。

            未授权时**不开编辑器**：交给主窗口挂上来的 `open_editor_guard`（它会弹授权
            对话框），这里只把那句原因写进提示区 —— 用户点了按钮必须有反应。
            """
            guard = getattr(self, "open_editor_guard", None)
            if callable(guard):
                try:
                    reason = guard()
                except Exception as exc:  # noqa: BLE001 - 闸门自己坏了不该锁死编辑器
                    logger.warning(f"策略编辑闸门出错（按放行处理）：{exc}")
                    reason = ""
                if reason:
                    self._set_hint("🔒 " + str(reason))
                    return
            if spec is not None:
                self._load_spec(spec)
            elif not self.editor.toPlainText().strip() and not self.name_edit.text().strip():
                # 头一次打开且编辑框是空的：给一句"下一步做什么"，不要让用户面对白板
                self._set_hint(
                    f"照着示例改最快：点【{SAMPLE_BUTTON_TEXT}】；"
                    "右边按钮点一下就插到光标那里。"
                )
            self.bottom_stack.setCurrentWidget(self.editor_page)
            self.bottom_stack.setVisible(True)
            self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

        def on_close_panel(self) -> None:
            """收起下半块（编辑器 / 只读详情）。公式已经保存的不会丢。"""
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
                self._set_hint(f"❌ 这条策略现在有问题：{spec.error_text}\n"
                               "改完再点【校验】；点【保存】就会覆盖原文件。")
            else:
                self._set_hint(f"已载入「{spec.name}」")

        def compile_current(self, *, quiet: bool = False) -> Any:
            """编译编辑框里的公式；失败时把**中文原文**写进提示区并返回 None。"""
            text = self.editor.toPlainText()
            if not text.strip():
                if not quiet:
                    self._set_hint(
                        f"❌ 策略还是空的：点右边的按钮就能插入，或者点【{SAMPLE_BUTTON_TEXT}】"
                    )
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
            # 口径与本项目不同的通达信函数（目前是 FINANCE → 流通市值）：
            # **非阻断**提醒，公式照跑，但必须说一句 —— 否则用户会以为它真是通达信那个股本。
            for note in formulas_lib.tdx_compat_notes(formula):
                lines.append("⚠️ " + str(note))
            # 通达信兼容层（2026-09-21）：编译时被**忽略**的东西要在这里说出来 ——
            # 用户从网上抄来的公式常带画图语句与样式修饰，不说明的话他会以为
            # "我那句画线生效了"，或者更糟：以为整条公式都跑到了。
            for note in getattr(formula, "notes", ()) or ():
                lines.append("ℹ️ " + str(note))
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
            # 「这次用的是哪套 K 线」**必须写在最前面**：同一份公式、同一个按钮，
            # 盘中（实时口径）与收盘后（日 K）选出来的票本来就会不一样，
            # 不写这一行用户只会以为程序不稳定（内置规则见 `formulas.prepare_inputs`）。
            caliber = str(result.get("caliber") or "")
            prefix = (caliber + "\n") if caliber else ""
            if result["count"]:
                # 标的写法与全项目一致：**半角** `名称(代码)`（开发文档），
                # 与「自选标的」表格、推送正文、错误信息里的写法逐字相同
                names = "、".join(
                    f"{hit['name']}({hit['symbol']})" for hit in hits
                )
                text = (f"最近交易日 {result['date']} 命中 {result['count']} 只：{names}")
                if result["count"] > shown:
                    text += f" …（只列前 {shown} 只）"
            else:
                text = (f"最近交易日 {result['date']}：没有命中"
                        f"（扫了 {result['scanned']} 只，{result['skipped']} 只因数据不足跳过）")
            text = prefix + text
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
            return str(getattr(formula, "name", "") or "").strip() or "未命名策略"

        def _on_preview_done(self, result: Any) -> None:
            """运行回来了（回主线程执行）：先收起"正在跑"的样子，再写结果。"""
            formula = self.preview_formula
            self._finish_preview()
            if not isinstance(result, dict):
                self._set_hint("运行没有返回结果（请重试）")
                return
            # 记住这一轮，供【导出匹配结果】使用：导出的必须是**用户刚看过的**那一份
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
            """【导出匹配结果】：把**上一次【运行】**的命中清单写成桌面上的文本文件。

            三条口径：

            * **导的是用户刚看过的那一份**（`self.last_run`），不重新跑一遍：
              重跑会得到另一份清单（盘中快照/新数据都会变），而他导出的必须是他看过的；
              再说重跑一次要扫全库 7~8 秒，点一下按钮等 8 秒不像话；
            * **全量**：提示区只列前 `PREVIEW_LIMIT` 只（一行放不下几十只），
              文件里是**全部**命中 —— 给用户的文件少几只，是最难被发现的那种错；
            * **成败都要看得见**：产物落在桌面（这一页之外），"没反应"就等于
              "不知道导没导出去"，所以提示区写清楚 + 标题区弹一句。

            用的函数与「开始匹配」建池时导出的是**同一个**（`pool.export_pick_file`）：
            版式、来源列、现价列完全一致 —— 用户拿两个文件对比时不该看到两套格式。
            """
            run = self.last_run
            if run is None and self.preview_worker is not None and self.preview_worker.isRunning():
                # 正在跑：这时候说"还没运行过"是骗人的（他刚点过），
                # 而且这一轮的结果马上就有了 —— 让他等这一轮，别导出上一轮
                self._set_hint("运行还没跑完：等它跑完再点【导出匹配结果】（导出的就是这一轮的命中）")
                return
            if not run:
                self._set_hint(
                    "还没有可导出的结果：先点【运行】看看能选出几只，再点【导出匹配结果】"
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
                    # 「来源」列与「自选标的」同一个词：公式选中 → `公式·<公式名>`
                    "strategy": formula_group.formula_strategy_name(
                        str(run.get("name") or "")
                    ),
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
                # `export_pick_file` 把具体原因写进日志、只返回 None（它不许影响匹配流程），
                # 所以这里给用户列出**可能**的原因与下一步，并让他去看日志
                message = ("❌ 导出失败：文件没能写出去（桌面目录不存在、没有写权限，"
                           "或者文件正被别的程序占用）—— 详见日志")
                self._set_hint(message)
                self._toast(message)
                return
            message = (f"✅ 已导出匹配结果：{path.name}（{len(rows)} 只，行情日 {day_text}）"
                       f"\n文件位置：{path}")
            self._set_hint(message)
            self._set_hint(message)

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
                        "它写在策略文件第一行的「# 说明:」注释头里，太长会把策略挤到看不见。")
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
            if path.exists() and not self._confirm(f"策略「{name}」已存在，要覆盖它吗？\n"
                                                   f"（原来的内容会被替换，不可撤销）"):
                self._set_hint(f"已取消保存：策略「{name}」保持原样（没有被改动）")
                return
            self._write(name)

        def on_save_as(self) -> None:
            """【另存为】：换个名字再存一份（原文件不动）。"""
            default = formulas_lib.safe_name(self.name_edit.text()) or "新策略"
            text, ok = QInputDialog.getText(self, "另存为", "新策略名称：", text=default)
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
            if path.exists() and not self._confirm(f"策略「{name}」已存在，要覆盖它吗？"):
                self._set_hint(f"已取消另存为：策略「{name}」保持原样")
                return
            self._write(name)

        def on_delete(self) -> None:
            """【删除】按钮：删掉**当前**这条公式（先确认）。"""
            name = self.current_name()
            if not name:
                self._set_hint("❌ 请先在上面列表里选一条策略（或填上名称）——内置策略不能删")
                return
            self.on_delete_formula(name)

        def on_delete_formula(self, name: str) -> None:
            """删除一条公式文件（右键菜单与【删除】按钮共用，**先二次确认**）。"""
            if not name:
                return
            if not self._confirm(f"确定删除策略「{name}」吗？\n（策略文件会被删掉，不可撤销）"):
                self._set_hint(f"已取消删除：策略「{name}」还在")
                return
            try:
                deleted = formulas_lib.delete_formula(name, self.directory)
            except OSError as exc:
                self._set_hint(f"❌ 删除失败：{exc}（文件可能正被其它程序占用）")
                return
            if not deleted:
                self._set_hint(f"❌ 没找到策略「{name}」的文件")
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
            self._set_hint(f"🗑 已删除策略「{name}」（文件已从策略目录移除）")

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
                self._set_hint(f"❌ 没存上：{exc}（策略目录：{folder}）")
                self._toast("❌ 策略没存上（策略目录可能不可写）")
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
            #   参不参与匹配由用户自己决定，保存只管存。
            #   「成功不需要提示，失败再提示」→ 这里**一个字都不说**：列表最下面多出
            #   那一行就是成功的反馈；把"已保存"再讲一遍只是噪音。
            index = self.select_row(name)
            if index is not None:
                self.table.scrollToItem(self.table.item(index, 0))
            if saved is not None and not saved.ok:
                # **存下去了，但这条公式跑不了**（语法/字段错）—— 这算"有问题"，必须说：
                # 文件躺在列表里、勾选框是灰的，用户不看那把灰勾是不会知道原因的。
                # 一句话、带行列号（引擎给的就是中文），别写小作文。
                self._set_hint("❌ 策略有错，跑不了：" + (saved.error_text or "语法错误"))
            else:
                # 保存成功**一个字都不说**（用户要求"成功不需要提示"）：
                # 列表最下面多出的那一行就是反馈；上一次失败留下的红字顺手收掉。
                self._clear_hint()

        def on_load_sample(self) -> None:
            """【新策略】（旧名【载入示例】）：给小白一个**能跑通**的起点。

            名字与行为分开：主人在 2026-09-21 只要求改按钮文字，所以
            "载入哪条公式、填哪些框、给什么提示"这些**一个字都没动**。
            """
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
                    f"已载入示例策略「{sample.name}」。\n"
                    "点【校验】看看它用到什么，点【运行】看它在你的库里能选出几只。"
                )
            else:
                self.name_edit.setText(SAMPLE_NAME)
                self.note_edit.clear()
                self.editor.setPlainText(SAMPLE_TEXT)
                self._set_hint(
                    "已载入内置示例策略（策略目录里还没有示例文件，这是兜底的那条）。\n"
                    "点【校验】→【运行】，再点【保存】就存到你的策略目录里了。"
                )
            self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

        # ── 匹配结果（**这一页不再显示**）─────────────────────────────
        #
        # 用户 2026-09-17：「策略匹配只要显示策略，不显示匹配结果，匹配结果直接进
        # 自选标的，可以在股池再添加删除。（也可以同时 output 一个文件到桌面）」
        #
        # 所以旧的「本次匹配结果」表、一句话结论、[全部加为自选] 按钮与它们的
        # 渲染/写库方法（`_refresh_result` / `_render_result` / `_load_result_from_db` /
        # `_result_row` / `on_add_all_to_watchlist`）**整体删掉**。结果的两个去处见模块
        # docstring 的「匹配结果去哪了」：自选标的页（可右键删、可手工加）+ 桌面文件
        # （`pool.export_pick_file()`，由 `scheduler.run_daily` 在建池成功后调用）。

        def show_pick_result(self, rows: Any = None, *, data_date: str | None = None) -> None:
            """把这一轮匹配的结果显示在**结果页**上（主窗口跑完 `run_daily` 会调它）。

            用户 2026-09-18 的要求（推翻了 2026-09-17 的"这一页不显示结果"）：
            "匹配状态时，策略列表界面变为匹配结果界面（匹配结果界面平时隐藏），
            结果可以一键加入自选和导出。"

            Args:
                rows: `run_daily()` 的 report（dict，取 `pool` / `data_date` / `errors`）
                    或**行列表**（老调用方的写法，照样收下）。
                data_date: 行情日（report 里没有时用这个）。
            """
            errors: list[str] = []
            picked: list[dict] = []
            caliber = ""
            warnings: list[str] = []
            if isinstance(rows, dict):
                data_date = data_date or rows.get("data_date")
                picked = [r for r in (rows.get("pool") or []) if isinstance(r, dict)]
                errors = [str(e) for e in (rows.get("errors") or [])]
                # 这一轮用的是哪套 K 线（盘中实时 / 日 K）：结果页上也要看得见，
                # 否则"同一份策略昨天选出 3 只、今天选出 7 只"没有解释
                formulas = rows.get("formulas")
                if isinstance(formulas, dict):
                    caliber = str(formulas.get("caliber") or "")
                    # 口径类的告知（如"盘中取不到快照，已退回日 K"）：它们**不是错误**
                    # （不在 `report["errors"]` 里），但用户得知道"这一轮为什么没用实时数据"
                    warnings = [str(w) for w in (formulas.get("warnings") or [])]
            elif rows is not None:
                picked = [r for r in rows if isinstance(r, dict)]
            self._fill_result(picked, data_date=data_date, errors=errors,
                              caliber=caliber, warnings=warnings)
            self.show_result_page()
            logger.info(
                "本次匹配结果已显示在「策略匹配」页：%d 只（行情日 %s）",
                len(self.result_rows), data_date or "未知",
            )

        def _result_label(self, row: dict) -> str:
            """结果表「来源」列：与「自选标的」那一列**同一个函数**（`pool.source_label`）。"""
            try:
                return pool_mod.source_label(row, None)
            except Exception:  # noqa: BLE001 - 来源算不出来不该让结果表画不出来
                logger.debug("算来源列失败", exc_info=True)
                return str(row.get("strategy") or row.get("strategies") or "—")

        def _fill_result(self, picked: list[dict], *, data_date: Any = None,
                         errors: list[str] | None = None,
                         caliber: str = "", warnings: list[str] | None = None) -> None:
            """把池子行铺进结果表，并写好那句结论。

            **只列"选出来的"票**（带来源的行）：`run_daily` 的池子里还会混进自选标的
            （那是"我自己加的"，不是"这次选出来的"），口径与建池那份清单一致。

            `caliber` 是这一轮用的 K 线口径（`formulas.prepare_inputs` 给的那句话）：
            它显示在结论下面，用户才知道"这次是按盘中实时算的，还是按收盘日 K 算的"。
            `warnings` 是口径类的**告知**（如"盘中取不到快照，已退回日 K"）：
            它们与 `errors` 分开（不是失败），但都要让人看见。
            """
            rows = [r for r in picked
                    if r.get("symbol") and str(r.get("strategy") or r.get("strategies") or "")]
            self.result_rows = [
                {"symbol": str(r["symbol"]), "name": str(r.get("name") or ""),
                 # `label` 是**给人看的那个词**（`公式·尾盘超短策略`），进编辑框上方的
                 # 「来源」列与备注（`匹配来源：X`）；`strategy` 是**引擎那份策略名**
                 # （与 `stock_pool.strategy` 同一种写法），加入自选时存进
                 # `watchlist.source_strategy` —— 股池那一列靠它才能显示成那条策略名
                 # （2026-09-21 主人实报"来源都变成自选了"就是少了这一份；
                 #  2026-09-23 起显示只写名字本身，见 `pool.source_label()`）。
                 "label": self._result_label(r),
                 "strategy": _first_strategy_name(r)}
                for r in rows
            ]
            self.result_date = data_date
            # 已经在自选里的票：那一格显示「已在自选」并置灰（不能重复加）
            in_watch = self._watchlist_symbols()
            self.result_table.setRowCount(len(self.result_rows))
            for index, row in enumerate(self.result_rows):
                label_text = f"{row['name']}({row['symbol']})"
                cells = (
                    label_text,
                    self._result_price_text(row["symbol"])[0],
                    self._result_quote_text(row["symbol"], "circ_mktcap", "亿"),
                    self._result_quote_text(row["symbol"], "turnover_rate", "%"),
                    row["label"],
                )
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    # `—` 那一格：tooltip 讲清"这一列是什么、为什么是 —"；
                    # 有值的格子：讲清"是哪只票的哪个数"
                    tip = (RESULT_HEADER_TIPS[column] if text == market_mod.DASH
                           else f"{label_text} · {RESULT_HEADER_TIPS[column]}")
                    item.setToolTip(tip)
                    self.result_table.setItem(index, column, item)
                self._set_result_add_cell(index, row["symbol"], already=row["symbol"] in in_watch)
            day = str(data_date or "未知")
            if self.result_rows:
                self.result_hint.setText(
                    f"本次匹配结果：共 {len(self.result_rows)} 只（行情日 {day}）。"
                    "结果不会自动进股池：要留下哪只就点它那一行的【加入自选】"
                    "（加进去之后记下加入价算盈亏；**默认不提醒**，要盯就去「自选标的」"
                    "打开那一行的监控开关），也可以【导出结果到桌面】。"
                )
            else:
                self.result_hint.setText(
                    f"本次匹配没有选到票（行情日 {day}）。"
                    "常见原因：勾选的策略没有命中、或数据还没更新；"
                    "可以点【返回策略列表】改一改再跑一轮。"
                )
            for message in (errors or []):
                # 出错的那几条（数据闸门拦住、策略出错…）直接说在结论下面 ——
                # 用户点完【开始匹配】最想知道的就是"为什么没结果"
                self.result_hint.setText(self.result_hint.text() + "\n❌ " + message)
            for warning in (warnings or []):
                # 口径类的告知（不是错误）：也写在结论下面，让"这一轮为什么没用实时数据"
                # 有地方看（它不在 `errors` 里，因为选了票、推送也发了，这一轮是成功的）
                self.result_hint.setText(self.result_hint.text() + "\n" + warning)
            if caliber:
                # 口径写在最后一行（它是"这一轮怎么算的"，不是错误也不是结论）
                self.result_hint.setText(self.result_hint.text() + "\n" + caliber)

        # ── 结果表：实时数据 + 每一行的【加入自选】 ──────────────────

        def set_quotes_provider(self, provider: Any) -> None:
            """注入"取实时快照"的函数（主窗口把 `QuotesService.quote` 接进来）。

            为什么用注入、而不是让这一页自己建一个快照服务：快照是**整个窗口一份缓存**
            （主窗口每 5 秒刷一次，自选标的/持仓两张表都在用）。这一页再建一个，
            就会有两套缓存、两个刷新节奏，同一个数在两张表里还可能不一样。
            没注入时（单测里直接建这一页）所有实时列显示 `—` —— 不编数。
            """
            self.quotes_provider = provider

        def _quote(self, symbol: str) -> dict:
            """取这一只的实时快照（没有 provider / 取不到 → 空字典）。"""
            provider = getattr(self, "quotes_provider", None)
            if not callable(provider):
                return {}
            try:
                data = provider(symbol)
            except Exception:  # noqa: BLE001 - 快照取不到只影响这几列
                logger.debug("取实时快照失败", exc_info=True)
                return {}
            return data if isinstance(data, dict) else {}

        def _watchlist_symbols(self) -> set[str]:
            """当前自选里的代码（决定那一格显示【加入自选】还是【已在自选】）。"""
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    rows = storage.load_watchlist(conn, enabled_only=False)
            except Exception:  # noqa: BLE001 - 读不出来就当没有自选（按钮仍可点，会再判一次）
                logger.debug("读自选表失败", exc_info=True)
                return set()
            return {str(row["symbol"]) for row in rows}

        def _last_close(self, symbol: str) -> float | None:
            """库里最近的**不复权收盘价**（没有实时快照时的兜底，界面会标 `*`）。"""
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    row = conn.execute(
                        "SELECT close FROM stock_daily_raw WHERE symbol = ? "
                        "ORDER BY date DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()
            except Exception:  # noqa: BLE001
                logger.debug("读本地收盘价失败", exc_info=True)
                return None
            if row and row[0]:
                return float(row[0])
            return None

        def _result_price_text(self, symbol: str) -> tuple[str, float | None]:
            """「实时股价」那一格：**实时快照优先**，没有就用本地最近收盘（标 `*`）。

            Returns:
                `(显示文字, 用于"加入时的价格"的数值)`；两样都没有时是 `("—", None)`。
            """
            price = self._quote(symbol).get("price")
            if price:
                return f"{float(price):.2f}", float(price)
            close = self._last_close(symbol)
            if close is not None:
                return f"{close:.2f}*", close
            return market_mod.DASH, None

        def _result_quote_text(self, symbol: str, key: str, unit: str) -> str:
            """「市值」「换手率」两格：取不到一律 `—`（**不显示 0** —— 0 是真实的值）。"""
            value = self._quote(symbol).get(key)
            if value is None:
                return market_mod.DASH
            return f"{float(value):.2f}{unit}"

        def _set_result_add_cell(self, index: int, symbol: str, *, already: bool) -> None:
            """「加入自选」那一格：没加过的给一个可点的按钮，加过的显示【已在自选】且置灰。"""
            if already:
                label = QLabel(RESULT_ADDED_TEXT)
                label.setObjectName("statusTag")
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setToolTip("这只票已经在「自选标的」里了，不用重复加")
                holder = label
            else:
                button = QPushButton(RESULT_ADD_TEXT)
                button.setToolTip(
                    "把这只票加进「自选标的」：之后它会一直留在池子里被盯盘，"
                    "并记下加入时的价格用来算盈亏（不重复添加、也不改你写过的备注）"
                )
                button.clicked.connect(
                    lambda _checked=False, sym=symbol, i=index: self.on_add_one_to_watchlist(
                        sym, i
                    )
                )
                holder = button
            # 尺寸按**当前字体现量**（用户 2026-09-21 实报"框体有点小，字显示不全"）：
            # ⚠️ 必须放在 `setCellWidget` **之后** —— 控件还没有父窗口时量到的是应用默认字体，
            # 而它真正的字体来自表格（可能被主题或字号设置改过）；实测 12pt 下差 16 像素，
            # 先量后放就会把两边的字各切掉一截（这就是那个坑）。
            # 只给 `setMinimumSize`、不 `setFixedSize`：宽字体/大字号下这一格要能自己长，
            # 列宽是 `ResizeToContents`，会跟着走。
            # 与其它表格一致：格子居中
            wrapper = QWidget()
            box = QHBoxLayout(wrapper)
            box.setContentsMargins(2, 0, 2, 0)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(holder)
            self.result_table.setCellWidget(index, RESULT_ADD_COLUMN, wrapper)
            cell_width, cell_height = _result_add_cell_size(holder)
            holder.setMinimumSize(cell_width, cell_height)
            # 行高也要够：默认行高 30 像素，而 15pt 时按钮自己就要 32 像素高 ——
            # 不给的话按钮上下被裁掉一条（同样是"字显示不全"的一种）。
            if self.result_table.rowHeight(index) < cell_height:
                self.result_table.setRowHeight(index, cell_height + 4)
            # 这一列的宽度是 `ResizeToContents` 算的，而它是在**放进去那一刻**算的；
            # 上面刚把最小尺寸调大，得让列宽重算一次 —— 否则按钮可能比列宽还宽，
            # 右边缘被列的边界裁掉（实测 10.5pt 时列宽 93、按钮 96，就是这么露出来的）。
            self.result_table.resizeColumnToContents(RESULT_ADD_COLUMN)

        def on_add_one_to_watchlist(self, symbol: str, index: int | None = None) -> None:
            """某一行点【加入自选】：把这一只写进 `watchlist`（**记下加入时的价格**）。

            与【一键加入自选】走同一条口径（不重复添加、不动用户备注、尊重上限），
            多做的一件事是**记住加入价** —— 「自选标的」的盈亏列要从那个价算起。
            """
            from laoa_trader.data import storage

            row = next((r for r in self.result_rows if r["symbol"] == symbol), None)
            if row is None:
                return
            name = row["name"] or symbol
            _, price = self._result_price_text(symbol)
            limit = max(int(getattr(self.cfg, "watchlist_max", 0) or 0), 0)
            try:
                with storage.connect(self.cfg.db_path) as conn:
                    existing = storage.load_watchlist(conn, enabled_only=False)
                    if any(str(r["symbol"]) == symbol for r in existing):
                        # 已经在自选里的票：**不重复添加、不动备注、不重新启用**，
                        # 但如果它的来源是空的（老数据、或当初是手工加的），
                        # 顺手把"这次是哪条公式选出来的"补上 —— 他只在这里点得到，
                        # 补的是他正要看的那条信息（见 `fill_watchlist_source`）。
                        storage.fill_watchlist_source(conn, symbol, row.get("strategy"))
                        if index is not None:
                            self._set_result_add_cell(index, symbol, already=True)
                        self._set_hint(f"「{name}」已经在自选里了（没有重复添加）")
                        return
                    enabled_count = sum(
                        1 for r in existing if int(r.get("enabled", 1)) == 1
                    )
                    if limit and enabled_count >= limit:
                        self._set_hint(
                            f"❌ 自选已达上限 {limit} 只，没有加进去"
                            "（去「自选标的」删几只，或把「系统设置」里的自选上限调大）"
                        )
                        return
                    storage.upsert_watchlist(
                        conn, symbol, name=name,
                        note=f"匹配来源：{row['label']}", price=price,
                        # 来源同时**结构化存一份**：股池那一列读它才能显示成那条策略名
                        # `尾盘超短策略`（2026-09-21 主人实报的"来源都变成自选了"）
                        source_strategy=row.get("strategy"),
                    )
            except Exception as exc:  # noqa: BLE001 - 库坏了要说人话，不让按钮把界面带走
                self._set_hint(f"❌ 加自选失败：{type(exc).__name__}: {exc}")
                return
            if index is not None:
                self._set_result_add_cell(index, symbol, already=True)
            price_text = f"（加入价 {price:.2f}）" if price else "（没有取到价格，盈亏先不显示）"
            self._set_hint(
                f"✅ 已把「{name}」加入「自选标的」{price_text}，盈亏从加入这天算起。\n"
                "默认**不提醒**（2026-10-05 起：默认只盯持仓股票）—— 要盯这一只，"
                "去「自选标的」点它那一行的「监控开关」。"
            )

        def show_result_page(self) -> None:
            """切到结果页（【开始匹配】按下时与跑完时都走这里）。"""
            if getattr(self, "list_stack", None) is not None:
                self.list_stack.setCurrentWidget(self.result_page)

        def show_list_page(self) -> None:
            """切回策略列表页（【返回策略列表】）。"""
            if getattr(self, "list_stack", None) is not None:
                self.list_stack.setCurrentWidget(self.list_page)

        def on_back_to_list(self) -> None:
            """【返回策略列表】：回到列表（结果留着，下次匹配刷新）。"""
            self.show_list_page()
            self._set_hint("已回到策略列表（本次结果还留着，点【开始匹配】会刷新它）")

        def on_add_all_to_watchlist(self) -> None:
            """【一键加入自选】：把本次结果的票写进 `watchlist`（**只写本地库，不联网**）。

            三条口径（与 `pool.merge_watchlist()` 一致，2026-09-18 从旧版结果区搬回来）：

            1. **不重复添加**：已经在自选表里的代码直接跳过（`upsert_watchlist` 本身幂等，
               但我们也不去动用户的备注 —— 他给自己那只票写过什么，不该被这一下改掉）；
            2. **尊重 `watchlist_max`**：上限只数**启用**的自选（与池子那边同一口径），
               满了就**明确说**"还有哪几只没加进去、怎么解决"，绝不静默丢；
            3. **保留来源**：新加的票在备注里写上"匹配来源：<策略/公式>"——
               池子重算后它们会离开池子，备注是这几只票"当初为什么在这"的唯一线索。
            """
            if not self.result_rows:
                self._set_hint("❌ 还没有匹配结果：先点【开始匹配】跑一轮")
                return
            from laoa_trader.data import storage

            limit = max(int(getattr(self.cfg, "watchlist_max", 0) or 0), 0)
            try:
                with storage.connect(self.cfg.db_path) as conn:
                    existing = storage.load_watchlist(conn, enabled_only=False)
                    enabled_count = sum(1 for row in existing if int(row.get("enabled", 1)) == 1)
                    known = {str(row["symbol"]) for row in existing}
                    fresh = [row for row in self.result_rows if row["symbol"] not in known]
                    room = max(limit - enabled_count, 0)
                    added: list[dict] = []
                    skipped: list[dict] = []
                    for row in fresh:
                        if len(added) >= room:
                            skipped.append(row)
                            continue
                        storage.upsert_watchlist(
                            conn, row["symbol"], name=row["name"],
                            note=f"匹配来源：{row['label']}",
                            # 来源也**结构化存一份**（股池那一列的 `X` 靠它）
                            source_strategy=row.get("strategy"),
                        )
                        added.append(row)
                    # 本来就在自选里的那几只：不重复添加、不动备注、不重新启用，
                    # 只把空着的来源补上（老数据里这一列是空的，他点这一次才有机会补）
                    for row in self.result_rows:
                        if str(row["symbol"]) in known:
                            storage.fill_watchlist_source(
                                conn, str(row["symbol"]), row.get("strategy")
                            )
            except Exception as exc:  # noqa: BLE001 - 库坏了要说人话，不让按钮把界面带走
                self._set_hint(f"❌ 加自选失败：{type(exc).__name__}: {exc}")
                return
            already = len(self.result_rows) - len(fresh)
            lines: list[str] = []
            if added:
                lines.append(
                    f"✅ 已加 {len(added)} 只进「自选标的」"
                    f"（现在共 {enabled_count + len(added)} 只自选，上限 {limit}）。"
                )
            if already:
                lines.append(f"另有 {already} 只本来就在自选里，没有重复添加、也没改你的备注。")
            if skipped:
                names = "、".join(f"{r['name']}({r['symbol']})" for r in skipped[:12])
                lines.append(
                    f"⚠️ 自选已达上限 {limit} 只，这 {len(skipped)} 只**没有**加进去：{names}"
                    "（去「自选标的」删几只，或把「系统设置」里的自选上限调大，再点一次）"
                )
            if not lines:
                lines.append("本次结果的票都已经在自选里了（没有重复添加）。")
            lines.append("在「自选标的」页能看到它们（右键可删）。")
            text = "\n".join(lines)
            self._set_hint(text)

        def on_export_result(self) -> None:
            """【导出结果到桌面】：把结果表里的票写成桌面文本文件。

            与编辑器里那个【导出匹配结果】、以及 `scheduler.run_daily()` 自动导出的
            是**同一个函数**（`pool.export_pick_file`）：版式、来源列、现价列完全一致，
            同一天的文件名也一样（互相覆盖，不会攒出一堆）。
            """
            if not self.result_rows:
                self._set_hint("❌ 还没有匹配结果：先点【开始匹配】跑一轮，再导出")
                return
            rows = [{"symbol": row["symbol"], "name": row["name"], "source_label": row["label"]}
                    for row in self.result_rows]
            try:
                path = pool_mod.export_pick_file(
                    rows,
                    data_date=str(self.result_date or "") or None,
                    db_path=self.cfg.db_path,
                    fallback_dir=Path(self.cfg.db_path).parent,
                )
            except Exception as exc:  # noqa: BLE001 - 导出失败绝不能把这一页带崩
                message = f"❌ 导出失败：{type(exc).__name__}: {exc}"
                self._set_hint(message)
                self._toast(message)
                return
            if path is None:
                message = ("❌ 导出失败：文件没能写出去（桌面目录不存在、没有写权限，"
                           "或者文件正被别的程序占用）—— 详见日志")
                self._set_hint(message)
                self._toast(message)
                return
            message = (f"✅ 已导出匹配结果：{path.name}（{len(rows)} 只）"
                       f"\n文件位置：{path}")
            self._set_hint(message)
            self._set_hint(message)

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
                    logger.debug("策略页状态回调出错", exc_info=True)

        def scorecard_targets(self) -> list[Any]:
            """当前**勾选**的策略 → 成绩单要评估的清单（名字 + 正文）。

            为什么只取勾选的（而不是"列表里全部"）：与【开始匹配】跑的是同一批，
            用户看到的"参与匹配的这几条"就是他以为会被评估的那几条。
            偷偷把没勾的也算一遍，除了多扫十几遍全库，还会让结果里冒出一堆
            他早就不用的策略。

            正文取 `spec.source`（= 文件里的策略体，不带 `# 名称:` 注释头）：
            带注释头喂给引擎会直接报"未知字段"，那是我们自己的格式问题，
            不该让用户看到。
            """
            targets: list[Any] = []
            for spec in self.specs:
                box = self._row_box(ROW_FORMULA, spec.name)
                if box is None or not box.isChecked():
                    continue
                targets.append(ScorecardTarget(name=spec.name, text=spec.source))
            return targets

        def on_scorecard(self) -> None:
            """【成绩单】：**按需**打开对话框（预检、计算、复制都在那个模块里）。

            这一页只做一件事：把勾选的策略名单交出去。不在这里算、也不在这里预检 ——
            同一份口径被两处实现，迟早会出现"界面说够、成绩单说不够"。
            模态（`exec()`）打开：成绩单是用户主动要看的临时窗口，跑完就关，
            不需要与主窗口并排操作。
            """
            dialog = ScorecardDialog(self, cfg=self.cfg, targets=self.scorecard_targets())
            self.scorecard_dialog = dialog
            dialog.exec()

        def on_start_pick(self) -> None:
            """【开始匹配】：**只举手**（emit `start_pick_requested`）。

            为什么这一页不自己跑匹配：整条流程（数据闸门 → 增量 → 策略 → 公式 → 建池 →
            推送 → 落库）都住在 `scheduler.run_daily()`，主窗口负责进度条与状态显示；
            这一页要是自己调一遍，就会出现两套流程、两套状态、两套错误处理
            （而且"这一页没跑数据闸门"会安静地跑出一个错的池子）。
            主窗口把这个信号接到 `on_run_pipeline`，跑完再调 `reload()` 刷列表。
            跑完之后**结果不在这一页**：进「自选标的」（在那一页增删）+ 导出一份到桌面
            （`scheduler.run_daily` 里建池成功后做的）—— 这句提示里得说出来，
            否则用户点完看不到任何结果会以为程序没反应。
            """
            # 切到结果页先摆一句"正在匹配…"：用户按下按钮之后**马上**要有个地方等着结果
            # （用户 2026-09-18 要求"匹配状态时，策略列表界面变为匹配结果界面"）。
            # 跑完主窗口会调 `show_pick_result()` 把真正的结果填进来。
            self.result_hint.setText("正在匹配…（跑完结果就出现在这里，不用再点别的）")
            self.result_table.setRowCount(0)
            self.show_result_page()
            self._toast("开始匹配：正在按勾选的策略跑一轮…"
                        "（结果会显示在这一页，并同时进「自选标的」+ 导出到桌面）")
            self.start_pick_requested.emit()

        def on_copy_detail(self) -> None:
            """【复制】：把内置策略详情放进剪贴板（贴到记事本/群里都行）。"""
            if not self.detail_text:
                self._toast("还没有可复制的详情：先在列表里点一行（策略或竞价策略）")
                return
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(self.detail_text)

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


def _first_strategy_name(row: dict) -> str:
    """结果行 → **引擎那份策略名**（`strategy` 优先，退回 `strategies` 的第一条）。

    与「来源」列那个**给人看的词**（`pool.source_label()`）刻意分开：
    存进自选表的是这一份（写法与 `stock_pool.strategy` 一致），
    将来显示时再走同一套 `source_label()`，股池表/推送/桌面文件才不会各说各话。
    """
    primary = str(row.get("strategy") or "").strip()
    if primary:
        return primary
    for name in str(row.get("strategies") or "").split(","):
        if name.strip():
            return name.strip()
    return ""


def _one_line(text: Any, limit: int) -> str:
    """多行文本压成一行（表格里只放得下一行；全文在 tooltip 与提示区）。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _result_add_cell_size(holder: Any) -> tuple[int, int]:
    """「加入自选」那一格**按当前字体量出来的**最小尺寸 `(宽, 高)`。

    为什么不能直接用 `sizeHint()`（这是用户实报"框体有点小，字显示不全"的根因）：
    `QPushButton` 的 `sizeHint()` 是**缓存**的，换字号之后不一定跟着长 ——
    Linux 实测：12pt 时"加入自选"的文字要 64 像素、算上按钮自己的左右内边距要 90，
    而那时 `sizeHint().width()` 还停在 80（10.5pt 时的值），照它排就把两边各切掉几个像素；
    高度同理（15pt 时按钮高 30，而文字自己就有 24 高 + 上下内边距）。
    所以这里每次都**现量**，并且只信"量出来的文字 + 留白"这一个下限：
    文字宽度取自 `QFontMetrics`，两侧留白见 `RESULT_ADD_SIDE_PADDING`，
    控件样式自己算出来的尺寸（QSS 的 padding/边框）作为另一个下限一起取大。
    """
    holder.ensurePolished()
    metrics = QFontMetrics(holder.font())
    # 两种文案都要放得下：没加过的显示「加入自选」、加过的显示「已在自选」，
    # 取较宽的那个，这样同一列的两种状态宽度一致（列宽不会来回跳）
    text_width = max(
        metrics.horizontalAdvance(RESULT_ADD_TEXT),
        metrics.horizontalAdvance(RESULT_ADDED_TEXT),
    )
    text_height = metrics.height()
    width = text_width + 2 * RESULT_ADD_SIDE_PADDING
    height = text_height + 2 * RESULT_ADD_V_PADDING
    if isinstance(holder, QPushButton):
        # 按钮还有自己的一圈内边距与边框：交给**控件自己的样式**算，别在这儿猜数字
        option = QStyleOptionButton()
        holder.initStyleOption(option)
        styled = holder.style().sizeFromContents(
            QStyle.ContentsType.CT_PushButton, option,
            QSize(text_width, text_height), holder,
        )
        width = max(width, styled.width())
        height = max(height, styled.height())
    return width, height


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
    "MENU_DISABLE",
    "MENU_ENABLE",
    "EXCLUDES",
    "OPERATORS",
    "PAGE_HINT",
    "PALETTE_BUTTON_OBJECT",
    "PALETTE_COLUMNS",
    "RESULT_ADD_COLUMN",
    "RESULT_ADD_SIDE_PADDING",
    "RESULT_ADD_V_PADDING",
    "RESULT_TABLE_OBJECT",
    "RESULT_ADD_TEXT",
    "RESULT_ADDED_TEXT",
    "RESULT_COLUMNS",
    "RESULT_HEADER_TIPS",
    "RESULT_HINT_IDLE",
    "PANEL_WIDTH",
    "PREVIEW_LIMIT",
    "AUCTION_KEY",
    "AUCTION_NAME",
    "ROW_AUCTION",
    "ROW_FORMULA",
    "RowMenu",
    "SAMPLE_BUTTON_TEXT",
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
    "formula_row_note",
]
