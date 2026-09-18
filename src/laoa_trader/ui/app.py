"""PySide6 主窗口。

布局（改版后）：**一个标题区 + 五个页签**。

    标题区：老A选股助手 · 运行状态：<现在在干什么>            [显示详情] [关于软件]
            （下面是"没有数据"这一类**一句话提示**，以及只在任务运行时出现的细进度条）
    页签：大盘概览 / 自选股池 / 持仓监控 / 策略选股 / 系统设置

为什么要这样改（见 `docs/改版方案.md` 一、二）
--------------------------------------------
- 原来顶部是"三行状态 + 七个按钮"，同一件事说三遍，而按钮又分不清"数据"与"选股"；
  现在**标题区不做任何操作**：下载/刷新/检查在「系统设置」，选股在「策略选股」，
  一处只管一件事；
- 原来七个页签里有三个（股票池 / 自选股 / 盘中提醒）说的是同一批票，
  用户要在三张表之间来回对；现在合成「自选股池」一张表（`来源` 列区分策略与自选），
  提醒进表格的 `提醒` 列 + 浮窗 + 图标闪烁；
- **尽量不弹窗**：只有"必须让用户做决定"的才弹（当前只剩公式删除确认），
  "没有数据 / 没配 Key"这类**指路**的话写在标题区与设置页里
  （用户要的是"我该做什么"，不是"内部为什么"）。

设计取舍
--------
- **界面层只做展示与转发**：所有耗时操作（下载 10 年数据、跑策略、调用接口）
  都丢进 `QThread` 工作线程，界面层一律 `try/except` + 状态栏提示 ——
  网络抖动、Key 没配、数据没下，都不该让窗口崩掉或卡住；
- **关闭窗口最小化到托盘**：这类盯盘工具的大部分时间在后台运行；
- **PySide6 缺失时优雅降级**：`main()` 会退回 CLI（Linux 开发机常见），
  所以本模块顶部的 Qt 导入全部放在 `try` 里，`QT_AVAILABLE` 标记可用性。
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Any

import laoa_trader
from laoa_trader import assets, intraday, market, pool, state
from laoa_trader import pool as pool_mod
from laoa_trader import config
from laoa_trader.config import Config, get_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader import hints
from laoa_trader.hints import (
    BTN_DOWNLOAD_TEXT,
    BTN_REFRESH_TEXT,
    BTN_RUN_TEXT,
)
from laoa_trader.log import get_logger, log_file_path
from laoa_trader.notify import KINDS, sound, summarize
from laoa_trader.notify.windows import xueqiu_url
from laoa_trader.scheduler import Scheduler, data_gate, refresh_data, run_daily
from laoa_trader.strategy import rules as rules_mod
from laoa_trader.ui import quotes as quotes_mod
from laoa_trader.ui import theme as theme_mod

logger = get_logger(__name__)

#: 程序名 / 版权行 / 数据来源：「窗口标题」「关于软件」对话框、复制到剪贴板的版本信息
#: **共用这一份** —— 分发出去之后用户看到的版本信息必须处处一致，不能各写各的
APP_NAME = "老A选股助手"
COPYRIGHT_TEXT = "版权所有 © 2026 async-chen，保留所有权利。"
#: 数据来源声明（「关于软件」里那一行）。
#:
#: 为什么写两个来源、并点明"非交易所授权行情"：程序主源是**同花顺**（配了 Key 时），
#: 没配 Key / Key 失效时由**免 Key 的公开行情接口**（腾讯/新浪/东财）兜底。
#: 这些数据都是**准实时快照**，不是交易所授权行情 —— 分发出去以后这句话
#: 就是用户判断"这数据能不能当真"的依据。
#: 2026-09-18：顺序跟着主源调整（同花顺在前），与页脚 `market.FOOTER_PREFIX` 同一口径。
SOURCE_TEXT = ("数据来源：同花顺金融数据服务（配置 Key 时）与公开行情接口（腾讯/新浪/东财）。"
               "均为公开来源的准实时快照，非交易所授权行情。"
               "本程序仅用于个人研究与学习，不构成任何投资建议。")

# ── 五个页签的标题（顺序即显示顺序）──
#: 常量而不是散落的字面量：测试、截图脚本、托盘菜单跳转都要**按标题找页面**，
#: 写错一个字就是 `indexOf` 返回 -1、然后静默什么都不做（Qt 不会报错，最难查的一类）
#:
#: 2026-09-17：这个名字从「全市概览」改回「大盘概览」（用户要求）。
#: 改名的代价正好是上面那句话说的那种坑 —— 凡是按标题找页面的地方（`tabs.indexOf`、
#: 截图脚本、测试）都得跟着改，所以标题只有这一个常量，别处一律引用它。
TAB_MARKET = "大盘概览"
TAB_WATCH = "自选股池"
TAB_POSITION = "持仓监控"
TAB_FORMULA = "策略选股"
TAB_SETTINGS = "系统设置"
TAB_TITLES: tuple[str, ...] = (TAB_MARKET, TAB_WATCH, TAB_POSITION, TAB_FORMULA, TAB_SETTINGS)

#: 「自选股池」的列（顺序即界面顺序；测试直接断言这一份）。
#: 2026-09-17 改版（用户要求）：加「市值」「换手」两列（流通市值，单位亿；实时换手率%），
#: `提醒` 列改成「监控开关」（显示 开启/关闭，点一格就能切换，提醒内容进 tooltip）。
WATCH_HEADERS: tuple[str, ...] = (
    "名称(代码)", "现价", "涨幅", "市值", "换手", "板块", "来源", "监控开关",
)
#: 「持仓监控」的列。**没有"数量"**：用户给定的字段只有 代码 + 成本价 + 备注
#: （`quantity` 留在库里做兼容，见 `docs/改版方案.md` 第五节）。
#: 同样是 2026-09-17 加的两列 + 提醒改监控开关（与上面同一套口径，两张表要一致）。
POSITION_HEADERS: tuple[str, ...] = (
    "名称(代码)", "成本价", "现价", "涨幅", "市值", "换手",
    "盈亏比例", "止损位", "止盈位", "监控开关",
)
#: 「监控开关」列在两张表里的下标（点这一格切换监控；别处一律用它，不写死数字）。
WATCH_MONITOR_COLUMN = WATCH_HEADERS.index("监控开关")
POSITION_MONITOR_COLUMN = POSITION_HEADERS.index("监控开关")
#: 监控开关那一格的两个文字（用户给定：`开启` / `关闭`）。
MONITOR_ON_TEXT = "开启"
MONITOR_OFF_TEXT = "关闭"

#: 关于页里图标的显示边长（资源只有 256/128/48/32/16 这几档，这里由 QPixmap 平滑缩放）
ABOUT_ICON_SIZE = 64

#: 竞价强度的界面刷新周期（毫秒）：竞价窗口只有 9:15–9:30 这十几分钟，
#: 每分钟一次就够（卡片上那一行要新鲜，但不能每 5 秒打一次接口）。
AUCTION_REFRESH_MS = 60_000

#: 「大盘概览」页自己的刷新周期（毫秒）—— 每分钟一次，与 5 秒的界面刷新解耦。
#: 取数另有 55 秒 TTL（`market_overview_ttl`），所以这一分钟里最多真打一次接口。
MARKET_REFRESH_MS = 60_000

# ── 提醒浮窗 / 图标闪烁 ──
#: 托盘与任务栏图标闪烁的间隔（毫秒）：500ms 一闪（每秒两次）最像"有消息"，
#: 再快就晃眼、再慢就像没在闪
FLASH_INTERVAL_MS = 500
#: 红点图标落盘的文件名：存在**数据目录的 cache 下**，不进仓库、不进安装包
#: （图标本身是程序化画出来的，见 `_alert_tray_icon`）
TRAY_ALERT_ICON_NAME = "tray-alert-32.png"
#: 每次对账时最多看最近多少条提醒（判"是不是新提醒"用；5 秒一轮，不查全表）
ALERT_SCAN_LIMIT = 20
#: `_alert_seen` 的上限：长跑一整天也不让它无限涨（超了就按当前这批重新记账）
ALERT_SEEN_LIMIT = 1000

# ── 窗口尺寸 ──
#: 窗口**期望**尺寸（可用区域够大时就用它）与**最小**尺寸上限。
#: 这两个值只是"上限"：真正的尺寸按屏幕可用区域算（见 `fit_window_geometry`）。
#: 高 760 而不是 720：概览页有三块（宽基 / 情绪 / 热门板块），720 在 150% 缩放的机器上
#: 正好压线（"KPI 卡片整排"已经折进「情绪指数」块，这一档高度是留给三块的）。
WINDOW_PREFERRED_SIZE = (1120, 760)
#: 最小尺寸收到 760×540 —— 比"持仓表 8 列 + 概览两块指数条目"需要的宽度小得多，
#: 用户能把它拖小（内容区/表格自己滚动），而不是被一个虚高的最小尺寸顶出屏幕。
WINDOW_MIN_SIZE = (760, 540)
#: 与屏幕可用区域边缘留的余量（默认尺寸与最小尺寸同一档）。
#: 为什么要留：Windows 上任务栏、"贴边自动吸附"、非 100% 的 DPI 缩放都会让
#: "刚好等于可用高"的窗口贴边或压到任务栏上；留 40 像素，窗口四周才有呼吸空间。
WINDOW_MARGIN = 40
WINDOW_MIN_MARGIN = 40
#: 极端小屏兜底：屏幕窄到算出来是 0 或负数时，也不能开出一个点不动的窗口
WINDOW_FLOOR_SIZE = (400, 320)
WINDOW_MIN_FLOOR_SIZE = (360, 280)

#: 概览页里"每个指数条目"的参考宽度（像素）：每行放几个 = 可用宽 // 这个数。
#: 260 是"名称（5 个汉字）+ 点位（8 位数字）+ 涨跌幅"还能舒服排下的宽度。
MARKET_ENTRY_MIN_WIDTH = 260
#: 一行最多几个条目（再多就只有"稀"没有"密"了）
MARKET_MAX_COLUMNS = 5

#: 条目内各段之间、条目之间在算"最少要多宽"时要留的余量（像素）。
#: 这两条只影响**列数**（宁可少一列，也不让文字被裁），不影响任何布局约束。
MARKET_ITEM_GAP = 8
MARKET_ITEM_MARGIN = 12
#: 点位 / 涨跌幅两列的**轨道宽度取样串**：取"最宽的那种数字"量出列宽，
#: 让不同条目的两列落在同一条竖线上（`-88888.88` 覆盖负号 + 5 位整数 + 2 位小数，
#: 涨跌幅多一个 `%`）。取样串只用来量宽度，不当占位文本显示。
MARKET_VALUE_TRACK = "-88888.88"
MARKET_PCT_TRACK = "-888.88%"

# ── 「大盘概览」的四块（用户给定的布局：竖直排列，窄窗口自动换列）──
#: 四块的标题 = 界面上小标题的文字，**顺序就是页面上下顺序**。
#: 常量而不是字面量：测试、截图脚本都要按名字取整块（写错一个字就是 KeyError 或
#: 静默什么都不做），而且四块的顺序本身就是"看盘第一眼先看什么"的设计。
#:
#: 2026-09-17 改版（用户要求，顺序也是用户给的）：
#:   1. 「成交与情绪」**挪到最上面**（在「宽基指数」之前），装的是原来那排 KPI 的小条目；
#:   2. 「情绪指数」只剩指数条目（成交额/涨跌停/涨跌家数都搬去上面那一块了）；
#:   3. 「热门板块」改成"上涨前五 + 下跌前五"两张表。
MARKET_SECTION_FLOW = "成交与情绪"
MARKET_SECTION_WIDE = "宽基指数"
MARKET_SECTION_SENTIMENT = "情绪指数"
MARKET_SECTION_HOT = "热门板块"
MARKET_SECTION_TITLES: tuple[str, ...] = (
    MARKET_SECTION_FLOW, MARKET_SECTION_WIDE, MARKET_SECTION_SENTIMENT,
    MARKET_SECTION_HOT,
)
#: 原来占一整排的 KPI 卡片，现在折成「成交与情绪」块里的**小条目**（一行摆下）。
#:
#: 为什么是**一条一个数**而不是一句合成长文本：
#: 最初做成 3 条（`沪 7793亿 · 深 8499亿 · 北 140亿` 这种），在 Linux 字体下刚好，
#: 到 Windows 上就出事了 —— **同一段中文在 Windows 上更宽**（CI 实测：那一格需要
#: 338px、实际只分到 231px），合成文本被自己的列宽截掉半个数字，而且它撑大了整页的
#: 最小宽度（实测 714 > 视口 706，横向差 8px）。拆成"一个数一条"之后，每条都短，
#: 任何字体下都塞得下，也不再撑大最小宽度。
#:
#: 2026-09-17：成交额从**三格**（沪/深/北）合成**一格**（`成交额` = 沪 + 深，
#: 口径见 `market.kpi_values()`），北交所那一格**删掉**（用户要求）——
#: 于是 9 条变 7 条，宽屏一行正好摆 7 个。
MARKET_STAT_AMOUNT = "成交额"
MARKET_STAT_LIMIT_UP = "涨停"
MARKET_STAT_LIMIT_DOWN = "跌停"
MARKET_STAT_BREAK = "炸板"
MARKET_STAT_UP = "上涨"
MARKET_STAT_DOWN = "下跌"
MARKET_STAT_FLAT = "平盘"
MARKET_STAT_TITLES: tuple[str, ...] = (
    MARKET_STAT_AMOUNT,
    MARKET_STAT_LIMIT_UP, MARKET_STAT_LIMIT_DOWN, MARKET_STAT_BREAK,
    MARKET_STAT_UP, MARKET_STAT_DOWN, MARKET_STAT_FLAT,
)
#: 小条目最多铺几列：**7**（用户要求"宽屏一行 7 个"）。
#: 窄屏时沿用响应式列数机制往下走（见 `_apply_market_columns`）——
#: 小条目的列数单独算，因为它是 7 条，而指数条目最多 5 列（`MARKET_MAX_COLUMNS`）。
MARKET_STAT_MAX_COLUMNS = 7
#: 小条目还没建出来时的兜底宽度（实测值优先，见 `_market_stat_need()`）
MARKET_STAT_MIN_WIDTH = 118
#: 实测宽度之上再留的余量：字体在不同机器上会有 ±1~2px 的差别，
#: 卡得刚刚好会在换一台机器时变成"文字被截"（CI 上真踩过）。
MARKET_STAT_MARGIN = 4
#: 小条目网格的间距。**只此一处**：列数计算与 `stats_grid.setSpacing()` 都用它 ——
#: 两处各写一个字面量，改了间距忘了改列数计算就会"莫名其妙换行"。
MARKET_STAT_GRID_SPACING = 5
#: 小条目**框内**（名称与数值之间）的间距。与上面的网格间距同理：为了挤下 7 条而收紧，
#: 但不许小于 4 —— 再小两段文字就贴在一起，"看得清"比"排得下"更重要。
STAT_INNER_SPACING = 6
#: 「热门板块」取几个行业来算（`pool.hot_industries(top=...)`）。
#: 12 = 用户给定的口径（与 `pool.hot_industries` 的默认 top 一致）：两张表各取前 5，
#: 从这 12 个行业里挑，样本够宽（不会因为只取 5 个而漏掉真正涨得多的板块）。
MARKET_HOT_TOP = 12

# ── 「热门板块」：上涨前五 / 下跌前五 两张表（2026-09-17 用户要求）──
#: 两张表的列（用户给定的表头文字，**照抄**）：
#:     板块名称  涨停数量  涨幅  主力净额
#: 口径：「板块名称」来自 `data.sectors.fetch_sector_rank()`（取不到时退回本地
#: `pool.hot_industries()` 的行业名）；「涨停数量」永远来自 `pool.hot_industries()`
#: （与选股用的是同一套口径，不另起一套）；「涨幅」= 板块当天涨跌幅（%）；
#: 「主力净额」= 主力净流入，单位**亿**（正负号保留）。
SECTOR_TABLE_HEADERS: tuple[str, ...] = ("板块名称", "涨停数量", "涨幅", "主力净额")
#: 「上涨前五」/「下跌前五」的标题与各取几名（用户给定：前 5）。
SECTOR_UP_TITLE = "上涨前五"
SECTOR_DOWN_TITLE = "下跌前五"
SECTOR_TOP = 5
#: 「主力净额」的单位（元 → 亿）与显示位数：正负号保留、两位小数。
SECTOR_NET_UNIT = 1e8
#: 四个表头各自的 tooltip（"这一列什么意思"的说明书；列头只有四个字，写不下口径）。
SECTOR_HEADER_TIPS: tuple[str, ...] = (
    "板块名称：来自 `data/sectors.py` 的板块榜；取不到时退回本地"
    "`pool.hot_industries()` 的行业名（这时下面那行说明会写明是哪种口径）",
    "涨停数量：**当日该板块的涨停家数**（与选股用的是同一套口径：`pool.hot_industries`）。"
    "取不到本地涨停池数据时是 `—`",
    "涨幅：板块**当天涨跌幅**（%）；退回本地口径时它是**近 5 日**行业等权涨幅"
    "（那时表下方会写清）",
    "主力净额：主力资金净流入，单位**亿**（正=净流入、负=净流出）。"
    "只有 `data/sectors.py` 的板块榜提供这一项，取不到是 `—`（不是 0）",
)
#: 板块表"最少要多宽"的估算参数（只影响并列还是上下排，不影响任何布局约束）：
#: 每个单元格左右各留一点内边距，再给表框与竖向滚动条留一点。
SECTOR_CELL_PADDING = 24
SECTOR_TABLE_CHROME = 40

# ── 「系统设置」的五组（顺序 = 页面上下顺序）──
#: 五组各自一个带标题的块。**顺序**是用户给定的：先"数据从哪来"，
#: 再"消息怎么发"，然后是两个具体功能（竞价扫描 / T策略），最后是零碎的偏好。
#:
#: 第 4 组叫 **T策略**（用户拍板）：原来叫「持仓风险」，只装止损/止盈与四个做T阈值；
#: 现在**合并成一组** —— 用户原话"止盈止损比例给客户自己设置（在T策略中编辑）"。
#: 为什么合并合理：这四个阈值与那两个比例全是"这只票该怎么买卖"的数字，
#: 分散在两组里，用户改做T阈值时看不到止损比例、反之亦然，而它们经常要一起调。
SETTINGS_GROUPS: tuple[str, ...] = (
    "数据来源", "通知方式", "竞价扫描", "T策略", "其他",
)
# ── 「数据来源」：来源列表（**同花顺是主源，公开源是兜底**）──
#: 内置来源的键（它仍然读 `cfg.data_sources` 决定启停与优先级）。
BUILTIN_SOURCE = "hithink"
#: **读不到 `data/sources.py` 时**内置来源那一行用的能力文案（兜底，不是真相源）。
#: 正常路径的能力文案来自 `sources.source_states()` 的 `capabilities_text` ——
#: 界面**不另维护一份"谁有什么能力"的判断**，抄一份出来就一定会和第二来源对不上。
SOURCE_CAPABILITIES: dict[str, str] = {
    BUILTIN_SOURCE: "实时快照 · 历史日K · 股票代码表",
}
#: **没有可添加的来源**时那一行如实说明（用户点【添加来源】也会看到同一句）。
#: 什么时候会走到它：注册表里的来源都已经加进 `data_sources` 了，或者
#: `data.sources` 读不出来（那时列表里只有内置同花顺那一行 + 兜底说明）。
SOURCE_ADD_UNAVAILABLE_TEXT = (
    "没有可添加的来源了：注册表里已实现的来源都已经在下面的列表里"
    "（启停就是「在不在这个列表里」，想停用一个来源就点它那一行的【删除】）。"
    "如果这里本该还有别的来源，请查看日志里的「数据来源注册表」相关警告。"
)
#: 数据来源的显示名：键 → 中文。
#: 界面上**如实显示配置里的值**，认不出的键原样显示 + 注明"界面没有它的实现" ——
#: 改写死一行"主来源：同花顺"的话，用户手改过 `data_sources` 之后就与界面说的不一致了。
DATA_SOURCE_LABELS: dict[str, str] = {BUILTIN_SOURCE: "同花顺金融数据服务（内置）"}
#: 同花顺那一行显示标记 / 说明用的两种文案（用户 2026-09-17 给定）：
#: 那一行是"<标记>：同花顺金融数据服务（需要 Key，申请地址 …）"，地址做成可点的链接；
#: 它**在 Key 输入框下面**（2026-09-18：输入框按用户要求加回来了）。
#: 2026-09-18（用户拍板）：**同花顺回到主源**，公开源降为兜底 ——
#: 理由是公开接口实测会限流（腾讯 fqkline 抓 700 只左右开始连续失败、新浪列表接口
#: 回 456），而同花顺是正经 API。所以这一行的标记从"备用源"改回"主来源"，
#: 并且要**如实说明为什么值得为它申请一个 Key**：完整历史只有它给得到。
BUILTIN_BACKUP_TAG = "主来源"
BUILTIN_KEY_URL = "https://fuyao.aicubes.cn"
#: 那一行的原文（`{url}` 会被换成可点的 `<a href>`，见 `_backup_key_notice`）。
BUILTIN_BACKUP_TEXT = "主来源：同花顺金融数据服务（需要 Key，申请地址 {url}）"
#: `history_years` 那一行的中文口径：一年 ≈ 250 个交易日，0.5 年 ≈ 6 个月
#: （与 `config.history_years` 的默认值同源，用户给定：超短线不需要长历史）
MONTHS_PER_YEAR = 12

#: 界面里唯一的三种字号层级：页面标题 +2、小条目数值 +1 加粗、其余都是基准字号。
#: 为什么卡死在三种：字号一多，页面看着就"花"，用户要的是层级分明不是字号丰富。
FONT_TITLE_DELTA = 2
FONT_VALUE_DELTA = 1

# ── 统一间距（整窗只用这两组值，别有的地方 16 有的地方 4）──
#: 每个页面的内边距（左、上、右、下）：下方略小一点，视觉上不会觉得"下面空了一块"
PAGE_MARGINS = (14, 14, 14, 12)
#: 同一页里控件之间的间距
PAGE_SPACING = 8
#: 主窗口中央区的内边距（比页面再紧一点：外层套内层，太厚就浪费屏幕）
WINDOW_MARGINS = (12, 12, 12, 12)

# ── 标题区 ──
#: 瞬时消息（任务完成/失败、刚点过的动作）在运行状态里停留多久（秒）。
#: 10 分钟：够用户看见，又不会永远盖住实时状态。
STATUS_MESSAGE_TTL = 600.0
#: 细进度条的高度（像素）：它**只在任务运行时出现**，而且不显示百分比文字
#: （百分比在运行状态那句话里，见 `_download_line`）—— 两处都写数字是同一件事说两遍
PROGRESS_BAR_HEIGHT = 6

#: 数据概况（`engine.summary()`）在界面层的缓存时长（秒）。
#: 为什么要缓存：它里面有全表 COUNT（行情表几百万行），而界面每 5 秒问一次 ——
#: 5 秒一轮地把整张表数一遍，用户看到的就是"卡"（下载/导入期间更明显）。
#: 30 秒足够：状态区显示的是"有几只、多少行"这种量级信息，不需要秒级精确。
SUMMARY_TTL = 30.0
#: 「盘中提醒」在运行状态里的三种说法（界面、测试、详情共用一份，不各写各的）。
#: "未在时段"是**空串**：不在时段就不提这件事 —— 运行状态只讲"现在在干什么"，
#: 挂一句"未在时段"等于每次开机都提醒用户"现在没在盯盘"，那是噪音。
INTRADAY_PAUSED = "提醒已暂停"
INTRADAY_IN_SESSION = "盘中提醒时段中"
INTRADAY_OUT_SESSION = ""
#: 按钮文字（2~4 字）。
#: 前三个从 `laoa_trader.hints` 取 —— 非界面层（同步/自检/调度）的"指路"文案里
#: 也要引用这几个名字，写错就等于让用户去找一个不存在的按钮，所以只有一份定义。
BTN_PAUSE_TEXT = "暂停提醒"
BTN_RESUME_TEXT = "恢复提醒"
BTN_CHECK_TEXT = "检查盘面"
BTN_DETAILS_TEXT = "显示详情"
BTN_ABOUT_TEXT = "关于软件"
#: 【选股】按钮现在的名字（在「策略选股」页里）—— 定义在 `hints.BTN_RUN_TEXT`
BTN_START_TEXT = BTN_RUN_TEXT

try:  # Qt 缺失时必须优雅降级（Linux 开发机、精简环境）
    from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QAction,
        QBrush,
        QColor,
        QDesktopServices,
        QFont,
        QGuiApplication,
        QIcon,
        QPalette,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QProgressBar,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
    # 提醒浮窗：自己画的窗口（Windows 原生 Toast 的点击行为不受我们控制，见该模块说明）
    from laoa_trader.ui.alert_popup import AlertPopup
    # 公式编辑器（「公式选股」页）：单独一个模块 —— 主窗口这边只负责把它挂成页签
    from laoa_trader.ui.formula_page import FormulaPage

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 任何导入问题都降级为 CLI
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


def _fmt_float(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _short_number(value: Any) -> str:
    """大数字压成"万 / 亿"：`10,283,203` → `1028 万`。

    用户反馈"10,283,203 行"这种既长又看不懂 —— 状态详情里要的是**量级**，
    不是账目数字（真要精确到个位，库和日志里都查得到）。
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if abs(number) >= 1e8:
        text = f"{number / 1e8:.1f}"
        return text.rstrip("0").rstrip(".") + " 亿"
    if abs(number) >= 1e4:
        return f"{number / 1e4:.0f} 万"
    return f"{number:.0f}"


def _month_day(value: Any) -> str:
    """`2026-09-11` → `09-11`（表格 tooltip 里说明"这是哪天的收盘价"）。

    为什么只留月-日：tooltip 里那句是"这是本地最新收盘价（09-11），不是实时价"，
    带上年份反而让人以为这是一份历史档案；认不出来的值原样返回（**不编一个日期**），
    完全是空的时候给 `—`。
    """
    text = str(value or "").strip()
    if not text:
        return "—"
    parts = text.split("-")
    return "-".join(parts[1:]) if len(parts) == 3 else text


def plain_text(text: Any) -> str:
    """把后端文案里的 markdown 强调标记去掉（`**手**` → `手`），其余原样保留。

    为什么需要它：来源的 `note` 由 `data/sources.py` 给（**不在这里重写**），
    而那几行是为 README/日志写的、带 `**` 强调；而界面上显示它的是一个**纯文本**
    `QLabel` —— 不处理的话用户会看到字面的星号（`单位是**手**`），像排版坏了。
    只去掉这一种标记，不动任何字；原始文案仍然完整地放在 tooltip 里。
    """
    return str(text or "").replace("**", "")


def probe_data_source(cfg: Config, api_key: str = "", client: Any = None) -> str:
    """【测试连接】：真发一次**最小请求**，返回一句中文结论（给状态栏/托盘用）。

    为什么用一个"涨停池 size=1"来试：它是全项目最便宜的一个请求（服务端只返回
    一条 pagination 记录），而且与下载/实时行情走**同一个客户端、同一个 Key**——
    它通了，就说明"Key 有效、网络通、服务端认我们"；它不通，用户立刻知道要改什么。

    `client` 只为测试注入（假客户端），真实路径按 `api_key` 现造一个客户端。
    本函数**绝不抛异常**：任何异常都翻成中文结论（界面线程不该因为一次探测崩掉）。
    """
    from laoa_trader.data import hithink as hx

    key = str(api_key or "").strip()
    if not key:
        # 2026-09-18 改口：原来写"没有 Key 时下载历史数据与实时行情都用不了"→ 也不对。
        # 现在的事实：没 Key 时**行情与大盘概览照常**（免 Key 公开源兜底），
        # **完整历史与选股要 Key**（数据自检要求复权事件与行业归属，只有同花顺那条路给）。
        # 这里只说这三件事，不承诺"装历史包"之类已经不存在的路（历史包方案已被用户否掉）。
        return ("❌ 还没填 API Key（这是【测试连接】要用的那一个）："
                "不填也能看行情与大盘概览（免 Key 公开源）；"
                "但完整历史与**选股**要它 —— 自检要求复权事件与行业归属齐备")
    try:
        probe = client or hx.HithinkClient(api_key=key, timeout=8.0, retries=1)
        total = int(probe.special_pool_total(market.LIMIT_UP_PATH))
    except hx.HithinkAuthError as exc:
        return f"❌ Key 无效（服务端拒绝）：{exc}"
    except hx.HithinkNotReadyError as exc:
        return f"⚠️ Key 能用，但服务端数据尚未就绪：{exc}"
    except hx.HithinkRateLimitError as exc:
        return f"⚠️ 被同花顺限流（Key 本身没问题，等一会儿再试）：{exc}"
    except Exception as exc:  # noqa: BLE001 - 网络/解析/证书什么都翻成一句话
        return f"❌ 连接失败：{type(exc).__name__}: {exc}"
    return f"✅ 连接正常（这次取到当日涨停池 {total} 条记录）"


if QT_AVAILABLE:

    def _scaled_font(font: Any, delta: int = 0, *, bold: bool | None = None) -> Any:
        """在现成字体上做字号加减与加粗，返回**新对象**（不改调用方那份）。

        为什么要判断 `pointSize() > 0`：字号也可能是按像素设的（那时 `pointSize()` 是 -1），
        负数上再加加减减会得到"字号 0"，界面直接糊成一团。
        """
        out = QFont(font)
        if delta and out.pointSize() > 0:
            out.setPointSize(max(1, out.pointSize() + delta))
        if bold is not None:
            out.setBold(bold)
        return out

    def _bold_name_font(widget: Any) -> Any:
        """**名称**用的字体：基准字号 + 加粗（字号不加，只加粗）。

        用户 2026-09-17 原话："所有名称显示不清楚，都加黑显示。" 于是**所有名称**
        （指数条目名、小条目名、行业/板块名、两张表的「名称(代码)」列）都用它。

        为什么用 `QFont.setBold()` 而不是样式表 `font-weight: bold`（二者只能选一个）：
        - 同一个控件里，那两列**数值**要靠 `setStyleSheet("color:…")` **逐值上色**
          （见 `MarketEntry.update_item`）。样式表一旦挂到某个标签上，Qt 就按样式表解析
          该标签的字体属性，主题 QSS（`ui/theme.py` 的 `QTableView`/`QLabel` 规则）
          与 palette 取色的组合会变得难以预测；
        - `setBold()` 只改这一个属性，与主题、与逐值上色都不打架 —— 确定性更强。
        **数值不加粗**：用户说的是"名称要清楚"，加粗只给名称
        （小条目的数值本来就是"大一号 + 加粗"，那是既有层级，这次一个字都没动）。
        """
        return _scaled_font(widget.font(), 0, bold=True)

    def _available_geometry() -> Any:
        """主屏的**可用区域**（逻辑像素，已经扣掉任务栏）；拿不到屏幕信息时返回 None。

        为什么必须用 availableGeometry 而不是 geometry：1366×768 的笔记本上任务栏
        能占掉 40 像素左右，而 DPI 缩放 125% 时"逻辑可用高度"还要再减一截 ——
        照着整屏尺寸开窗，窗口底边正好被任务栏或屏幕下沿吃掉，
        用户看到的就是"软件一打开，最下边就看不见"。
        """
        try:
            screen = QGuiApplication.primaryScreen()
        except Exception as exc:  # noqa: BLE001 - 无显示环境/插件异常都要能降级
            logger.debug(f"取屏幕信息失败：{exc}")
            return None
        if screen is None:
            return None
        try:
            return screen.availableGeometry()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"取可用区域失败：{exc}")
            return None

    def fit_window_geometry(avail: Any = None) -> tuple[Any, Any, Any] | None:
        """按屏幕可用区域算 `(默认尺寸, 最小尺寸, 居中位置)`；拿不到屏幕信息时返回 None。

        规则（宽/高各自算，屏幕小就让位）：
        - 默认尺寸 = `min(1120, 可用宽 - 40) × min(760, 可用高 - 40)`；
        - 最小尺寸 = `min(760, 可用宽 - 40) × min(540, 可用高 - 40)`；
        - 位置 = 可用区域居中（别顶在左上角，也别让窗口一半在屏幕外）。

        **为什么是"按可用区域"而不是写死**：Windows 上真正决定窗口能看到多少的是
        *逻辑*像素 —— 2160×1440 的屏在 150% 缩放下只剩 1440×960 逻辑像素，
        再扣掉任务栏，可用高度只有 900 左右。写死 720 高（加标题栏）正好被切掉底边，
        用户看到的就是"打开软件最下边看不见"。按可用区域算，这台机器上会得到
        `min(1120, 1400) × min(760, 860) = 1120×760`，整窗都在屏幕内。

        **为什么最小尺寸也一起收**：窗口的最小尺寸如果大于可用高度，用户**缩不动**它 ——
        底边永远在屏幕外（`resize()` 会被 Qt 按最小尺寸顶回去），这是"最下边看不见"
        最隐蔽的那一半原因。收到 760×540 之后，即使用户把窗口拖到很小，
        内容区（概览的可滚动区、股票池的卡片区与表格）也都会自己滚动，
        不会有"表格 9 列顶出来的虚高最小宽度"把窗口撑出屏幕。
        最小尺寸还做了一次 `min(最小, 默认)` 的收敛：屏幕很窄时
        `min(760, 宽-40)` 会等于 `min(1120, 宽-40)`，那样窗口一开出来就是最小尺寸、
        用户一点都拖不大，也不合理。

        Args:
            avail: 可用区域（QRect）；缺省时自己去问主屏。测试注入一个假的 960×900
                （≈2160×1440 + 150% 缩放）也走这条路 —— 离屏平台的"真屏幕"是 800×800，
                只用它测不出真实机器上的行为。

        Returns:
            `(QSize 默认, QSize 最小, QPoint 位置)`；拿不到屏幕信息时 None（调用方退回固定尺寸）。
        """
        if avail is None:
            avail = _available_geometry()
        if avail is None:
            return None
        try:
            avail_width, avail_height = int(avail.width()), int(avail.height())
            avail_x, avail_y = int(avail.x()), int(avail.y())
        except Exception:  # noqa: BLE001 - 传进来的不是 QRect 就当拿不到
            return None
        if avail_width <= 0 or avail_height <= 0:
            return None

        width = min(WINDOW_PREFERRED_SIZE[0], avail_width - WINDOW_MARGIN)
        height = min(WINDOW_PREFERRED_SIZE[1], avail_height - WINDOW_MARGIN)
        min_width = min(WINDOW_MIN_SIZE[0], avail_width - WINDOW_MIN_MARGIN)
        min_height = min(WINDOW_MIN_SIZE[1], avail_height - WINDOW_MIN_MARGIN)
        width = max(width, WINDOW_FLOOR_SIZE[0])
        height = max(height, WINDOW_FLOOR_SIZE[1])
        min_width = max(min_width, WINDOW_MIN_FLOOR_SIZE[0])
        min_height = max(min_height, WINDOW_MIN_FLOOR_SIZE[1])
        min_width = min(min_width, width)
        min_height = min(min_height, height)

        x = avail_x + max(0, (avail_width - width) // 2)
        y = avail_y + max(0, (avail_height - height) // 2)
        return QSize(width, height), QSize(min_width, min_height), QPoint(x, y)

    class ElidedLabel(QLabel):
        """单行小字：宽度不够时**省略**（全文用 `fullText()` 取，鼠标停上去也能看全）。

        为什么不用 `setWordWrap(True)`：页脚必须只占一行（用户要求"数据说明和刷新
        改成一行或者两行显示"，正常状态就一行），一换行就等于把页脚变成了两行，
        刷新按钮还可能被挤到下一行去。省略号也比"半截字"更能让人看出"这里被截了"。

        横向 SizePolicy 用 `Ignored`：文本再长也不会把窗口的**最小宽度**顶大 ——
        最小宽度一旦被一行小字顶起来，小屏上就会横向溢出（用户看不到右侧内容）。
        """

        def __init__(self, text: str = "") -> None:
            super().__init__()
            self._full_text = ""
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.setFullText(text)

        def fullText(self) -> str:
            """完整文本（界面上显示的是它按当前宽度省略后的样子）。"""
            return self._full_text

        def setText(self, text: str) -> None:  # noqa: D102 - 与 setFullText 同义
            """外部只管给完整文本；**省略由本类按当前宽度自己算**。

            为什么要覆盖 `setText`：如果调用方直接走 QLabel 的 `setText`，
            本类记着的"完整文本"就与显示内容脱节了 —— 下一次 resize 会把
            之前设的文本**换成那句旧的完整文本**（真踩过：状态栏刷成"正在初始化…"）。
            """
            self.setFullText(text)

        def setFullText(self, text: str) -> None:
            """设置完整文本并立刻按当前宽度重算显示（tooltip 里放全文，方便核对）。"""
            self._full_text = str(text or "")
            self.setToolTip(self._full_text)
            self._apply_elide()

        def resizeEvent(self, event: Any) -> None:  # noqa: D102 - 见类注释
            super().resizeEvent(event)
            self._apply_elide()

        def _apply_elide(self) -> None:
            metrics = self.fontMetrics()
            width = max(0, self.width())
            text = self._full_text
            if width and metrics.horizontalAdvance(text) > width:
                text = metrics.elidedText(
                    self._full_text, Qt.TextElideMode.ElideRight, width
                )
            # QLabel.setText 对相同文本会直接返回，所以这里不会和 resize 打循环
            super().setText(text)

    class SourceRow(QFrame):
        """「数据来源」里的一行：**来源名 + 提供什么 + Key（或"免 Key"）+ 启用 + 删除**。

        用户要求数据来源做成"可添加的多个来源，各自用他自己的 Key"，所以这一行是
        列表里的一格，而不是把 Key 输入框散在页面上：

            ┌ 同花顺金融数据服务（内置）  [主来源]                            [✓] 启用 ┐
            │ 提供：实时快照、历史日K、股票代码表                                     │
            │ [••••••]（Key 输入框，默认空白）        [测试连接]                     │
            │ 主来源：同花顺金融数据服务（需要 Key，申请地址 fuyao.aicubes.cn）       │
            └────────────────────────────────────────────────────────────────────────┘
            ┌ 公开行情源（腾讯为主，免 Key）  [免 Key]                      [✓] 启用 ┐
            │ 提供：实时快照                                                         │
            │ 兜底源（没配同花顺 Key 时）：两张表的行情 + 大盘概览 + 每日增量；        │
            │ 实测会被限流；完整历史与选股要 Key（自检要求复权事件与行业归属）        │
            └────────────────────────────────────────────────────────────────────────┘

        三种行的差异全部由**构造参数**表达（不在类里 if 来源名）：
        - 内置行：`deletable=False` + 启用勾选框 `setEnabled(False)`（"在 data_sources 里就在用"）；
        - 用户添加的来源：可删、可停用，Key 落到它自己的配置键上；
        - 认不出的来源（用户手改过 `data_sources`）：只读展示 + 注明"界面没有它的实现"。

        "要不要填 Key"**完全听调用方的**（`needs_key`，来自 `data.sources.source_states`）：
        免 Key 的来源**不给输入框** —— 画一个填不了东西的框，比不画更糟
        （用户会去找一个根本不存在的 Key）。

        2026-09-17 → 2026-09-18：内置同花顺那一行**先**被改成"只有一行申请地址、没有输入框"，
        用户随后澄清了那句话的意思 —— **"不要配 KEY" = 程序里不许预置自己的 Key，
        不是不给用户填**（原话："设置里让你不要配 KEY，但是你也要给个 key 的输入口啊"）。
        所以现在是：**输入框（默认空白）+【测试连接】+ 下面一行可点开的申请地址**。
        程序侧一个字都没写死：`key_text` 只来自用户自己的 `config.toml`
        （`hithink_api_key`）/ 环境变量 `HITHINK_FINANCE_API_KEY`，出厂包里的这两个值都是空的。

        属性（测试与将来的第二来源都按这些名字取）：`source`（键）、`name_label`、
        `tag_label`、`capability_label`、`note_label`、`key_label`、`key_edit`（**免 Key 的
        来源才为 None**）、`enabled_box`、`btn_delete`、`btn_test`（免 Key 时为 None）、
        `key_notice_text`（内置行那句说明的纯文本）。
        """

        def __init__(
            self,
            source: str,
            *,
            name: str,
            capability: str = "",
            note: str = "",
            builtin: bool = False,
            implemented: bool = True,
            needs_key: bool = True,
            has_key: bool = False,
            key_text: str = "",
            key_placeholder: str = "API Key",
            key_config: str = "",
            key_echo_password: bool = True,
            tag: str = "",
            key_notice: str = "",
            key_notice_url: str = "",
        ) -> None:
            super().__init__()
            self.source = source
            self.builtin = builtin
            self.implemented = implemented
            self.needs_key = bool(needs_key)
            self.key_config = key_config
            self.setObjectName("sourceRow")
            self.setFrameShape(QFrame.Shape.StyledPanel)
            self.setStyleSheet(
                "QFrame#sourceRow { border: 1px solid palette(mid);"
                " border-radius: 6px; }"
            )
            outer = QVBoxLayout(self)
            outer.setContentsMargins(10, 6, 10, 6)
            outer.setSpacing(4)

            head = QHBoxLayout()
            head.setSpacing(PAGE_SPACING)
            self.name_label = QLabel(name)
            self.name_label.setFont(
                _scaled_font(self.name_label.font(), FONT_VALUE_DELTA, bold=True)
            )
            head.addWidget(self.name_label)
            # 标记：主来源 / 免 Key / 已配 Key / 未配 Key / 未实现 ——
            # 一眼看出这个来源的状态。调用方给了 `tag` 就用它（"主来源"那一行是按
            # `data_sources` 的顺序算出来的，类里判不出来，所以由调用方传进来）
            if not tag:
                if not implemented:
                    tag = "未实现"
                elif not self.needs_key:
                    tag = "免 Key"
                elif has_key:
                    tag = "已配 Key"
                else:
                    tag = "未配 Key"
            self.tag_label = QLabel(tag)
            self.tag_label.setObjectName("statusTag")      # 小号灰字
            self.tag_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            head.addWidget(self.tag_label)
            head.addStretch(1)
            self.enabled_box = QCheckBox("启用")
            self.enabled_box.setChecked(True)
            # 为什么这个勾选框点不动：启停只由 `data_sources` 一个键表达（见
            # `data/sources.py` 的模块头）——**列表里出现 = 已启用**，
            # 想停用它就用【删除】。画一个能点、点了却不改变任何东西的开关，
            # 就是在骗用户（他会以为关掉了）
            self.enabled_box.setEnabled(False)
            self.enabled_box.setToolTip(
                "内置的同花顺：在列表里 = 在 config.toml 的 data_sources 里 = 会用；"
                "列表顺序就是取数优先级（前一个不可用就落到下一个）。"
                "这里不能直接关 —— 想停用它就从 data_sources 里去掉"
                if builtin else
                "列表里出现 = 已启用（写在 config.toml 的 data_sources 里）；"
                "不想用它就点【删除】—— 这里不做第二个开关，免得两处说法不一致"
            )
            head.addWidget(self.enabled_box)
            self.btn_delete = QPushButton("删除")
            self.btn_delete.setToolTip(
                "把这个来源从列表里去掉（写回 config.toml 的 data_sources 的列表就是这个列表）"
            )
            self.btn_delete.setVisible(not builtin)
            head.addWidget(self.btn_delete)
            outer.addLayout(head)

            # 能力说明：**换来源会丢掉什么**，用户必须看得见（文案来自 `source_states`）
            self.capability_label = QLabel(
                plain_text("提供：" + capability) if capability else
                ("提供：—" if not implemented else "提供：（未知）")
            )
            self.capability_label.setObjectName("statusTag")
            self.capability_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            self.capability_label.setWordWrap(True)
            outer.addWidget(self.capability_label)

            # 一句话说明（含**已知风险**，例如东方财富是未文档化接口）：直接显示，
            # 不用点开、不用看文档 —— 用户要在这里就知道自己换了什么
            self.note_label = QLabel(plain_text(note))
            self.note_label.setObjectName("statusTag")
            self.note_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            self.note_label.setWordWrap(True)
            self.note_label.setVisible(bool(note))
            self.note_label.setToolTip(note or "")
            outer.addWidget(self.note_label)

            row = QHBoxLayout()
            row.setSpacing(PAGE_SPACING)
            self.key_edit: Any = None
            self.btn_test: Any = None
            #: 那一行说明的**纯文本**（界面显示的是带 `<a>` 的富文本，测试按这个属性断言文字）
            self.key_notice_text = str(key_notice or "")
            self.key_label = QLabel("")
            self.key_label.setObjectName("statusTag")
            self.key_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            self.key_label.setWordWrap(True)
            if self.needs_key:
                # 需要 Key 的来源 = **输入框 + 【测试连接】**
                # 2026-09-18（用户澄清）：内置同花顺这一行**也要有输入口** ——
                # 用户原话"设置里让你不要配 KEY，但是你也要给个 key 的输入口啊"：
                # 当初那句"只保留 key 的申请地址、不保留自己的 KEY"说的是
                # **程序里不许预置自己的（开发者的）Key**，不是"界面上不给填"。
                # 所以现在：输入框照给、默认**空白**（`key_text` 来自用户自己的 config.toml，
                # 程序从不写死任何 Key），下面再挂一行可点开的申请地址。
                self.key_label.setVisible(False)
                row.addWidget(self.key_label)
                self.key_edit = QLineEdit(key_text)
                if key_echo_password:
                    self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
                self.key_edit.setPlaceholderText(key_placeholder)
                row.addWidget(self.key_edit, 1)
                self.btn_test = QPushButton("测试连接")
                row.addWidget(self.btn_test)
            else:
                # **免 Key 的来源不给假输入框**：直接说清"不用填"
                self.key_label.setText("免 Key：这个来源不用申请、不用填（填了也没有用）")
                self.key_label.setVisible(True)
                row.addWidget(self.key_label, 1)
            outer.addLayout(row)

            if key_notice:
                # 申请地址那一行（内置同花顺）：做成**可点开**的链接，
                # 用户真的需要 Key 时一步就能到申请页，不用手抄地址。
                # 富文本而不是 HTML 转义拼接：整句是程序里写死的常量，不含用户输入。
                self.key_label.setText(
                    self.key_notice_text.replace(
                        key_notice_url,
                        f'<a href="{key_notice_url}">{key_notice_url}</a>',
                    ) if key_notice_url else self.key_notice_text
                )
                self.key_label.setTextFormat(Qt.TextFormat.RichText)
                self.key_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextBrowserInteraction
                )
                self.key_label.setOpenExternalLinks(True)
                self.key_label.setVisible(True)
                outer.addWidget(self.key_label)

            if not implemented:
                # 用户在 config.toml 里手写了别的来源名：**照实说**，不假装能配
                if self.key_edit is not None:
                    self.key_edit.setReadOnly(True)
                    self.key_edit.setPlaceholderText("（界面还没有这个来源的 Key 输入）")
                self.key_label.setText("界面还没有这个来源的实现：只如实显示，不会拿它取数")
                self.key_label.setVisible(True)
                self.setToolTip(
                    "这个来源名是你在 config.toml 的 data_sources 里写的，"
                    "但程序还没有它的实现 —— 界面只如实显示，不会拿它取数"
                )

    class MarketStatItem(QFrame):
        """一个小条目：标签（**加粗名称**）+ 数值大一号加粗（**横向一条**，不是一张大卡）。

        原来成交额/涨跌停/涨跌家数是**占满一整排**的 7 张 KPI 卡片；现在它们折进
        「成交与情绪」块的小条目（2026-09-17 起 7 条，宽屏一行摆下）。
        所以这里刻意做得**扁而小**：一条 22 像素左右高、跟着块内网格自动换列，
        不再单独占一排。

        为什么不做成"一行长文本"：长文本靠自动换行折出来的行对不齐（数字有的在行尾、
        有的在行中），一眼就是"没排版"；一个小条目一个数值，扫一眼就能比大小。

        2026-09-17（用户要求）：名称**加粗**（`_bold_name_font`），并且**不再用灰字** ——
        "灰 + 小号"正是用户说的"名称显示不清楚"；数值仍是"大一号 + 加粗"（既有层级，没动）。

        属性：`name`（条目名）、`title_label`（加粗名称）、`value_label`（大一号加粗数值）。
        取色只用 palette + 一条细边框，不引图片/图标（打包与 DPI 缩放才不会挑环境）。
        """

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self.setObjectName("marketStatItem")
            self.setFrameShape(QFrame.Shape.StyledPanel)
            # 细边框圆角：与三块之间的表盘同一套做法（palette 取色，主题换了也不会撞色）
            self.setStyleSheet(
                "QFrame#marketStatItem { border: 1px solid palette(mid);"
                " border-radius: 6px; }"
            )
            row = QHBoxLayout(self)
            # 内边距与内部间距**刻意收紧**（原 8/8）：用户要求这 7 条排成一排，
            # 在 920 逻辑宽下就差这十几个像素。收紧的是"框内的空白"，
            # 不是文字大小 —— 名称与数值的字号层级一个都没动。
            row.setContentsMargins(5, 2, 5, 2)
            row.setSpacing(STAT_INNER_SPACING)
            self.title_label = QLabel(name)
            # **名称：加粗 + 正文色**（不再用灰字，见类说明与 `_bold_name_font`）
            self.title_label.setObjectName("marketStatTitle")
            self.title_label.setFont(_bold_name_font(self.title_label))
            row.addWidget(self.title_label)
            row.addStretch(1)
            self.value_label = QLabel(market.DASH)
            self.value_label.setFont(
                _scaled_font(self.value_label.font(), FONT_VALUE_DELTA, bold=True)
            )
            self.value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            row.addWidget(self.value_label)

    class MarketEntry(QFrame):
        """一个指数条目：`名称（加粗） ｜ 点位 ｜ 涨跌幅`（后两列右对齐、**逐值**上色）。

        为什么要拆成三个 QLabel：用户要求"既然涨跌分颜色了，那情绪/板块的数字和涨跌幅
        也分一下颜色"。一行一个 QLabel 只有**一种**颜色，而情绪/板块经常同时有涨有跌，
        按行取色必然要么"全染红"要么"全不染"，两种都在骗人；拆成条目之后
        每一项按自己的涨跌上色（见 `market.value_color`）。

        数字竖着对齐的办法（两种里选一种 —— 选"固定列宽 + 右对齐"）：
        - 数值/涨跌幅两个标签都用"最宽数字串"（`MARKET_VALUE_TRACK` / `MARKET_PCT_TRACK`）
          量出来的宽度做 `setMinimumWidth`，再 `AlignRight`。列宽与字体无关，
          用户系统上装的是哪款中文字体都不影响对齐；
        - **没有**改用它 `QFont.setStyleHint(Monospace)`：那只是个"提示"，Windows 上
          Qt 未必真能找到等宽字体（中文界面下尤其如此），对齐会随字体漂移 ——
          固定列宽是确定性更强的做法。
        """

        def __init__(self, item: dict | None = None) -> None:
            super().__init__()
            self.setObjectName("marketEntry")
            self.thscode = ""
            self.name_label = QLabel(market.DASH)
            self.name_label.setObjectName("marketEntryName")
            # 指数名：**加粗 + 正文色**（用户："所有名称显示不清楚，都加黑显示"）。
            # 数值（点位/涨跌幅）不加粗 —— 它们靠"右对齐 + 逐值上色"表达，见类说明。
            self.name_label.setFont(_bold_name_font(self.name_label))
            self.value_label = QLabel(market.DASH)
            self.pct_label = QLabel(market.DASH)
            row = QHBoxLayout(self)
            row.setContentsMargins(8, 2, 8, 2)
            row.setSpacing(10)
            row.addWidget(self.name_label, 1)     # 名称吃掉多余宽度，两列数字始终靠右
            for label, track in (
                (self.value_label, MARKET_VALUE_TRACK),
                (self.pct_label, MARKET_PCT_TRACK),
            ):
                label.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                label.setMinimumWidth(label.fontMetrics().horizontalAdvance(track))
                row.addWidget(label, 0)
            if item is not None:
                self.update_item(item)

        def update_item(self, item: dict) -> None:
            """按一个条目 dict 更新三个文本与**逐值**颜色（点位与涨跌幅各自上色）。"""
            self.thscode = str(item.get("thscode") or "")
            name, value, pct = market.entry_fields(item)
            self.name_label.setText(name)
            self.value_label.setText(value)
            self.pct_label.setText(pct)
            color = market.value_color(item.get("change_pct"))
            # 用 setStyleSheet 上色而不是富文本 HTML：`text()` 里带标签会让断言变脆，
            # 而且颜色只有一个用途（前景色），样式表是最直接的一层
            style = f"color:{color}" if color else ""
            self.value_label.setStyleSheet(style)
            self.pct_label.setStyleSheet(style)

    class SectorTable(QFrame):
        """「上涨前五」/「下跌前五」里的一张表：小标题 + 四列。

        列就是用户给定的那四个字（`SECTOR_TABLE_HEADERS`）：
        `板块名称 | 涨停数量 | 涨幅 | 主力净额` —— 用户原话是"现在的数据都没写什么意思，
        改成以下表格标上名称"，所以**表头文字必须写清含义**，这也是这一块的设计核心。

        为什么用 `QTableWidget` 而不是继续用"一行一个 QFrame"：这两张表是**表**——
        列要对齐、要有表头、要能一眼看出"哪一列是什么"；QTableWidget 的列头就是
        那个"写清含义"的地方，而且它还自带列宽自适应（`Stretch`），窄窗口下自己
        横向滚动，不会把整页顶宽。

        口径（都在 tooltip 里写清，因为列头只有四个字）：
        - 板块名称：来自 `data.sectors.fetch_sector_rank()`；取不到时是本地
          `pool.hot_industries()` 的行业名（页面上会写明是哪种口径）；
        - 涨停数量：**当日该板块涨停家数**（`pool.hot_industries()`，与选股同一套口径）；
        - 涨幅：板块当天涨跌幅（%），按涨跌上色（`market.value_color`）；
        - 主力净额：主力净流入，**单位亿**（原始单位是元，这里 ÷1e8），正负号保留、
          同样按正负上色；取不到就是 `—`（**绝不显示 0**：0 是"刚好不流入不流出"）。
        """

        def __init__(self, title: str) -> None:
            super().__init__()
            self.title = title
            self.setObjectName("marketSectorTable")
            box = QVBoxLayout(self)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(4)
            self.title_label = QLabel(title)
            self.title_label.setObjectName("marketSectionTitle")   # 与分区标题同一档
            self.title_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            box.addWidget(self.title_label)
            self.table = QTableWidget(0, len(SECTOR_TABLE_HEADERS))
            self.table.setHorizontalHeaderLabels(list(SECTOR_TABLE_HEADERS))
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.setAlternatingRowColors(True)
            self.table.setShowGrid(True)
            # 表头那四个字就是"这一列什么意思"的说明书 —— 口径写进表头 tooltip
            for column, tip in enumerate(SECTOR_HEADER_TIPS):
                item = self.table.horizontalHeaderItem(column)
                if item is not None:
                    item.setToolTip(tip)
            box.addWidget(self.table)
            self.rows: list[dict] = []

        def minimum_need(self) -> int:
            """这张表"至少要多宽"（表头文字 + 单元格内边距 + 竖滚动条）。

            为什么要问它：两张表并列还是上下排，取决于"半屏塞不塞得下"——
            与其写死一个 600 像素的常量（换台机器字体更宽就不对了），
            不如按当前字体量一遍（与概览条目列数同一套思路，见 `_market_item_need`）。
            """
            metrics = self.table.fontMetrics()
            width = sum(metrics.horizontalAdvance(text) for text in SECTOR_TABLE_HEADERS)
            return int(width + SECTOR_CELL_PADDING * len(SECTOR_TABLE_HEADERS)
                       + SECTOR_TABLE_CHROME)

        def set_rows(self, rows: list[dict]) -> None:
            """填 5 行（`rows` 为空就画 0 行 + 表头，照样能看出这一列是什么）。"""
            self.rows = list(rows or [])
            self.table.setRowCount(len(self.rows))
            for index, row in enumerate(self.rows):
                self.table.setItem(index, 0, self._name_item(row))
                self.table.setItem(index, 1, self._limit_up_item(row))
                self.table.setItem(index, 2, self._pct_item(row))
                self.table.setItem(index, 3, self._net_item(row))

        def _name_item(self, row: dict) -> Any:
            """板块名称：**加粗**（用户要求所有名称加黑）——数值不加粗。"""
            item = QTableWidgetItem(str(row.get("name") or market.DASH))
            item.setFont(_bold_name_font(self.table))
            item.setToolTip(self._name_tip(row))
            return item

        @staticmethod
        def _name_tip(row: dict) -> str:
            bits = []
            if row.get("source_text"):
                bits.append(str(row["source_text"]))
            if row.get("mom") is not None and row.get("pct") is None:
                # 退回本地口径时，这一列装的其实是"近 5 日行业等权涨幅"
                bits.append("这一列现在是**近 5 日**行业等权涨幅（不是当日涨幅），"
                            "原因见「热门板块」下面那行说明")
            return "\n".join(bits)

        def _limit_up_item(self, row: dict) -> Any:
            value = row.get("limit_up")
            item = QTableWidgetItem(market.DASH if value is None else str(int(value)))
            item.setToolTip(SECTOR_HEADER_TIPS[1])
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            return item

        def _pct_item(self, row: dict) -> Any:
            value = row.get("pct")
            item = QTableWidgetItem(percent_value_text(value))
            color = market.value_color(value)
            if color:
                # 与两张主表同一套做法：改前景色，不用富文本（排序/复制都不受影响）
                item.setForeground(QBrush(QColor(color)))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            item.setToolTip(SECTOR_HEADER_TIPS[2])
            return item

        def _net_item(self, row: dict) -> Any:
            """主力净额：元 → **亿**（`+1.23亿` / `-0.45亿`）；取不到是 `—`。

            为什么单独写一个格式化而不是复用 `market._amount_text`：
            那个是"成交额"口径（四舍五入到整亿、没有正负号），
            而主力净额要的是**带符号的两位小数亿** —— 差一个符号，
            用户就会把"净流出"看成"净流入"。
            """
            value = row.get("main_net")
            if value is None:
                item = QTableWidgetItem(market.DASH)
                item.setToolTip(
                    "这一列取不到：主力净额要 `data/sectors.py` 的板块榜才提供，"
                    "现在这份数据里没有它（不是 0 —— 0 是「刚好不流入不流出」）"
                )
            else:
                number = float(value) / SECTOR_NET_UNIT
                item = QTableWidgetItem(f"{number:+.2f}亿")
                color = market.value_color(number)
                if color:
                    item.setForeground(QBrush(QColor(color)))
                item.setToolTip(f"{SECTOR_HEADER_TIPS[3]}\n原始值 {float(value):,.0f} 元")
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            return item

    class HotSectorsSection(QFrame):
        """「热门板块」块 = 标题 + **上涨前五 / 下跌前五 两张表**（2026-09-17 用户要求）。

        为什么改成两张表：用户原话是"把上涨前 5 和下跌前 5 都标出来。现在的数据都没写
        什么意思，改成以下表格标上名称" —— 也就是说这一块要回答两件事：
        **今天资金在抢谁、又在砸谁**，而且每一列的含义要写在表头上。
        旧版是"涨停密度前 12"的一列长串（只有上榜的、全是涨的），跌得最狠的板块
        在那套口径里根本不会出现（它按涨停密度排序）。

        窄屏排布：两张表**并列**（宽屏）或**上下排**（窄屏）—— 判据是
        "两张表的最小需要宽度之和塞不塞得下当前宽度"（`_relayout`），
        与概览条目列数同一套思路：宁可少排一列，也不让文字被裁。

        属性：`title_label`（块标题）、`tables`（`{标题: SectorTable}`）、
        `placeholder_label`（两张表都没数据时的 `—` 占位）、`note_label`（口径说明）。
        兼容旧接口：`entries` / `stats` 恒为空 —— 原来"一块 = 一个条目网格"，
        现在这一块是两张表，但外面遍历各块的代码（`_render_market_overview`）不必分叉。
        """

        def __init__(self, label: str,
                     titles: tuple[str, ...] = (SECTOR_UP_TITLE, SECTOR_DOWN_TITLE)) -> None:
            super().__init__()
            self.label = label
            self.keys: tuple[str, ...] = ()
            self.entries: list[Any] = []
            self.stats: dict[str, Any] = {}
            self._narrow: bool | None = None
            self._need = 0
            box = QVBoxLayout(self)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            self.title_label = QLabel(label)
            self.title_label.setObjectName("marketSectionTitle")
            self.title_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            box.addWidget(self.title_label)
            #: 两张表：**上涨前五** 与 **下跌前五**（顺序就是界面上的顺序）
            self.tables: dict[str, SectorTable] = {
                title: SectorTable(title) for title in titles
            }
            self.grid = QGridLayout()
            self.grid.setContentsMargins(0, 0, 0, 0)
            self.grid.setSpacing(MARKET_ITEM_GAP)
            box.addLayout(self.grid)
            for table in self.tables.values():
                self.grid.addWidget(table, 0, 0)
            self.placeholder_label = QLabel(market.DASH)
            self.placeholder_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            box.addWidget(self.placeholder_label)
            self.placeholder_label.setVisible(False)
            #: 页内那行说明（"表里的数是从哪来的"）：**只在有话说时出现**
            self.note_label = ElidedLabel("")
            self.note_label.setObjectName("statusTag")
            self.note_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            box.addWidget(self.note_label)
            self.note_label.setVisible(False)

        def set_rows(self, rows: dict[str, list[dict]], note: str = "") -> None:
            """填两张表：`{标题: 行列表}`；`note` 是"这些数从哪来"的那句话。"""
            for title, table in self.tables.items():
                table.set_rows(list((rows or {}).get(title) or []))
            empty = not any(table.rows for table in self.tables.values())
            self.placeholder_label.setVisible(empty)
            # 界面显示用 `plain_text`（与来源说明同一套：QLabel 是纯文本，
            # `**` 会显示成字面的星号）；tooltip 里保留原文
            self.note_label.setFullText(plain_text(note or ""))
            self.note_label.setToolTip(str(note or ""))
            self.note_label.setVisible(bool(note))
            self._need = 0
            self._relayout()

        def set_columns(self, columns: int, stat_columns: int | None = None) -> None:
            """列数变化时重排（这一块不看列数，只看自己的宽度 —— 见 `_relayout`）。"""
            self._relayout()

        def resizeEvent(self, event: Any) -> None:  # noqa: D102 - 见类说明
            super().resizeEvent(event)
            self._relayout()

        def _relayout(self) -> None:
            """按当前宽度决定两张表**并列**还是**上下排**（宽度不够就上下排）。"""
            if not self._need:
                self._need = sum(table.minimum_need() for table in self.tables.values())
            need = self._need + MARKET_ITEM_GAP
            width = max(self.width(), 0)
            available = width or 10_000          # 还没被布局量过宽度时：按够宽算
            narrow = available < need
            if narrow == self._narrow:
                return
            self._narrow = narrow
            for index, table in enumerate(self.tables.values()):
                self.grid.removeWidget(table)
                if narrow:
                    self.grid.addWidget(table, index, 0)
                else:
                    self.grid.addWidget(table, 0, index)
            for column in range(len(self.tables)):
                self.grid.setColumnStretch(column, 1 if (not narrow or column == 0) else 0)

    class MarketSection(QFrame):
        """一块 = 小标题 + 条目网格（**整块就是这一个控件**）。

        为什么要一整块一个控件：配置里没配这一组指数时，`setVisible(False)` 一下
        就能"连标题带网格"一起收掉，不会留一个空的"宽基指数："吊在那里。

        属性：
            `label`（界面上的小标题）、`keys`（数据来自哪几组，见 `market.GROUPS`）、
            `grid`（QGridLayout）、`entries`（条目控件列表）、`stats`（小条目名→控件，
            只有「成交与情绪」块有：`with_stats=True`）、
            `placeholder_label`（配了这一块但没取到数时的 `—`）。
        """

        def __init__(
            self,
            label: str,
            keys: tuple[str, ...],
            *,
            entry_factory: Any = None,
            with_stats: bool = False,
        ) -> None:
            super().__init__()
            self.label = label
            self.keys = tuple(keys)
            self.entry_factory = entry_factory or (
                lambda item: MarketEntry(item)
            )
            self._keys_seen: tuple = ()
            self._columns = 0
            self._stats_columns = 0
            box = QVBoxLayout(self)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            self.title_label = QLabel(label)
            self.title_label.setObjectName("marketSectionTitle")   # 分区标题：小号灰字
            self.title_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            box.addWidget(self.title_label)
            self.grid = QGridLayout()
            self.grid.setContentsMargins(0, 0, 0, 0)
            self.grid.setSpacing(6)
            box.addLayout(self.grid)
            self.entries: list[Any] = []
            self.placeholder_label = QLabel(market.DASH)
            self.placeholder_label.setForegroundRole(
                QPalette.ColorRole.PlaceholderText
            )
            box.addWidget(self.placeholder_label)
            self.placeholder_label.setVisible(False)
            #: 小条目（只有「成交与情绪」块有：成交额 / 涨停 / 跌停 / 炸板 / 上涨 / 下跌 / 平盘）
            self.stats: dict[str, Any] = {}
            self.stats_grid: Any = None
            if with_stats:
                self.stats_grid = QGridLayout()
                self.stats_grid.setContentsMargins(0, 0, 0, 0)
                self.stats_grid.setSpacing(MARKET_STAT_GRID_SPACING)
                for column, name in enumerate(MARKET_STAT_TITLES):
                    item = MarketStatItem(name)
                    self.stats[name] = item
                    self.stats_grid.addWidget(item, 0, column)
                    self.stats_grid.setColumnStretch(column, 1)
                box.addLayout(self.stats_grid)

        def set_items(self, items: list[dict]) -> None:
            """按最新的条目列表更新网格：**键没变就只改文本/颜色**，不重建控件。

            为什么不每次都重建：概览每分钟刷一次，重建会把焦点、选中与
            "鼠标正停在哪一项上"全部清掉；只在**条目集合真的变了**（换配置、加了一只）
            时重建，界面才稳。键取代码或行业名（两种条目都得有一个稳定的身份）。
            """
            keys = tuple(_market_item_key(item) for item in items)
            if keys != self._keys_seen:
                self._keys_seen = keys
                self.clear_entries()
                self.entries = [self.entry_factory(item) for item in items]
                for column, entry in enumerate(self.entries):
                    self.grid.addWidget(entry, 0, column)
                self._columns = 0                 # 强制下一次重排列数
            for entry, item in zip(self.entries, items):
                entry.update_item(item)
            # `—` 占位只在"**配了这一块**但没取到数"时出现：
            # 成交与情绪块本来就没有条目（它装的是小条目），给它画一个吊在那里的 `—`
            # 只会让人以为"这一块没数据"。
            self.placeholder_label.setVisible(not items and not self.stats)

        def set_stat(self, name: str, text: str, *, tooltip: str = "") -> None:
            """更新一个小条目的数值（块里没有这个条目时什么都不做）。"""
            item = self.stats.get(name)
            if item is None:
                return
            item.value_label.setText(text)
            if tooltip:
                item.setToolTip(tooltip)
                item.value_label.setToolTip(tooltip)
                item.title_label.setToolTip(tooltip)

        def clear_entries(self) -> None:
            """把网格里的条目全部摘掉并销毁（换配置/换数据源时用）。"""
            for entry in self.entries:
                self.grid.removeWidget(entry)
                entry.setParent(None)
                entry.deleteLater()
            self.entries = []

        def set_columns(self, columns: int, stat_columns: int | None = None) -> None:
            """按列数把**条目**与**小条目**各自重排成网格（窄屏自动换行，绝不横向滚动）。

            为什么两套列数分开传：条目最多 5 列（`MARKET_MAX_COLUMNS`）、
            小条目最多 7 列（`MARKET_STAT_MAX_COLUMNS`，用户要求"宽屏一行 7 个"），
            上限不同，所以由调用方（`_apply_market_columns`）按各自实测宽度算好传进来；
            不传时退回老的"小条目跟着条目列数走、最多 7"（老调用方与测试照样能用）。
            """
            if columns != self._columns:
                self._columns = columns
                for index, entry in enumerate(self.entries):
                    self.grid.removeWidget(entry)
                    self.grid.addWidget(entry, index // columns, index % columns)
                for column in range(MARKET_MAX_COLUMNS):
                    self.grid.setColumnStretch(column, 1 if column < columns else 0)
            if self.stats_grid is None:
                return
            wanted = min(columns if stat_columns is None else stat_columns,
                         MARKET_STAT_MAX_COLUMNS)
            if wanted == self._stats_columns:
                return
            self._stats_columns = wanted
            for index, name in enumerate(MARKET_STAT_TITLES):
                item = self.stats[name]
                self.stats_grid.removeWidget(item)
                self.stats_grid.addWidget(item, index // wanted, index % wanted)
            for column in range(MARKET_STAT_MAX_COLUMNS):
                # 等宽列：列数已经按"最宽那条"算过了（见 `_apply_market_columns`），
                # 所以等分之后每一列都不会小于最宽的那条 —— 这是**不会截字**的保证。
                # （试过"按各条需要比例分配"，字体一变反而会把宽的那条挤到 sizeHint 以下，
                #  宽字体用例当场红，所以退回等宽这套。）
                self.stats_grid.setColumnStretch(column, 1 if column < wanted else 0)

    def _market_item_key(item: dict) -> str:
        """条目的身份：指数的 `thscode` 或行业的 `industry`（重建控件的判据）。"""
        return str(item.get("thscode") or item.get("industry") or "")

    def _sector_number(value: Any) -> Any:
        """板块榜里的一个数 → float/int；空/非数字一律 None（界面画 `—`，**不是 0**）。

        为什么要单独一个宽松转换：板块榜的三个数（涨幅/主力净额/涨停家数）来自接口，
        停牌、缺字段、字段名对不上都会是空值 —— 把空值当 0 显示，用户会把
        "没取到"读成"确实是 0"（净额 0 = 刚好不流入不流出，是完全不同的结论）。
        """
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if isinstance(value, int) else number


    def sectors_module() -> Any:
        """防御式取 `laoa_trader.data.sectors`；**拿不到就返回 None**（调用方退回本地口径）。

        为什么必须 try/except 而不是顶部直接 import：这个模块是本次改版**另一个 agent
        正在写**的（接口契约见任务书），它可能还不存在、也可能写了一半语法错 ——
        而概览页是程序启动的**第一屏**，一个 import 失败等于整个窗口打不开。
        拿不到时退回"本地算"（`local_sector_tables`），页面上写明是哪种口径。
        """
        try:
            from laoa_trader.data import sectors as sectors_mod
        except Exception as exc:  # noqa: BLE001 - 缺模块/写坏/依赖缺失都算"拿不到"
            logger.info(f"板块榜模块不可用（退回本地口径）：{type(exc).__name__}: {exc}")
            return None
        if not callable(getattr(sectors_mod, "fetch_sector_rank", None)):
            logger.info("板块榜模块里没有 fetch_sector_rank（退回本地口径）")
            return None
        return sectors_mod


    def sector_rank_tables(
        rank: Any, industries: Any, *, shown: int = SECTOR_TOP
    ) -> tuple[list[dict], list[dict]]:
        """板块榜的行 + 本地涨停家数 → `(上涨前五, 下跌前五)`。

        口径（用户给定）：
        - **排序按涨幅**：上涨前五 = 涨幅降序前 5；下跌前五 = 涨幅升序前 5
          （涨幅 = 板块当天涨跌幅，来自 `sectors.fetch_sector_rank()`）；
        - **涨停数量用地本那一套**：`pool.hot_industries()` 的 `limit_up`
          （用户原话"涨停数量 = 该板块当日涨停家数；用它的口径，不要另起一套"），
          按**板块名**对齐；对不上的显示 `—`（宁可显示"没对上"，也不要编一个 0 出来）；
        - **主力净额**单位是元，界面上 ÷1e8 显示成"亿"（见 `SectorTable._net_item`）。
        """
        counts: dict[str, Any] = {}
        for name, record in (industries or {}).items():
            if str(name):
                counts[str(name)] = _sector_number((record or {}).get("limit_up"))
        rows: list[dict] = []
        for item in rank or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            pct = _sector_number(item.get("pct"))
            if pct is None:
                continue          # 没有涨幅就排不进"上涨/下跌前五"（不拿 0 顶替）
            rows.append({
                "name": name,
                "limit_up": counts.get(name),
                "pct": pct,
                "main_net": _sector_number(item.get("main_net")),
                "source_text": "板块榜（`data/sectors.py` 的 fetch_sector_rank）："
                               "涨幅=当天板块涨跌幅",
            })
        # 同涨幅按名字定序：**每次刷新顺序都一样**（否则两张表会自己跳来跳去）
        up = sorted(rows, key=lambda row: (-float(row["pct"]), row["name"]))[:shown]
        down = sorted(rows, key=lambda row: (float(row["pct"]), row["name"]))[:shown]
        return up, down


    def local_sector_tables(
        industries: Any, *, shown: int = SECTOR_TOP
    ) -> tuple[list[dict], list[dict]]:
        """取不到板块榜时的**本地兜底**：按本地口径排，主力净额一律 `—`。

        本地只有 `pool.hot_industries()` 给的三样东西：涨停家数、涨停密度、
        **近 5 日行业等权涨幅**（没有"当天板块涨跌幅"，也没有主力净额）。
        所以这里：
        - 涨幅那一列装的是**近 5 日等权涨幅**（`mom`），页面上会写明"不是当日涨幅" ——
          换个名字叫"涨幅"却不说明，等于拿一个别的口径冒充用户要的那个数；
        - 主力净额那一列是 `—`（取不到），**不显示 0**；
        - 涨停数量照旧用本地那一套口径（本来就是它算的）。
        """
        rows: list[dict] = []
        for name, record in (industries or {}).items():
            if not str(name):
                continue
            mom = _sector_number((record or {}).get("mom"))
            # `mom` 是**比例**（0.0321 = 3.21%），而 `pct` 这一列全项目统一是**百分数**
            # （板块榜给的就是百分数）—— 这里 ×100 换成同一口径，否则本地模式下
            # 涨幅会显示成 `+0.03%`（差 100 倍）。
            rows.append({
                "name": str(name),
                "limit_up": _sector_number((record or {}).get("limit_up")),
                "pct": None if mom is None else float(mom) * 100,
                "main_net": None,
                "mom": mom,
                "source_text": "本地口径：这一列是**近 5 日**行业等权涨幅（不是当日涨幅）",
            })
        ranked = [row for row in rows if row["pct"] is not None]
        up = sorted(ranked, key=lambda row: (-float(row["pct"]), row["name"]))[:shown]
        down = sorted(ranked, key=lambda row: (float(row["pct"]), row["name"]))[:shown]
        return up, down


    def sector_payload(cfg: Any, industries: Any) -> dict:
        """「热门板块」两张表的数据 + 那行说明（**在后台线程里调用**）。

        Returns:
            `{"up": [...], "down": [...], "source": "sectors"|"local", "note": "..."}`。
            `note` 就是页面上那行小字：**说明这些数是从哪来的、缺的那一列为什么缺** ——
            用户要的东西不许静默消失，取不到就得说清是"没取到"而不是"没有"。
        """
        sectors_mod = sectors_module()
        if sectors_mod is not None:
            try:
                rank = sectors_mod.fetch_sector_rank()
            except Exception as exc:  # noqa: BLE001 - 板块榜失败只影响这一块
                logger.info(f"板块榜取数失败（退回本地口径）：{type(exc).__name__}: {exc}")
                rank = None
            if rank:
                up, down = sector_rank_tables(rank, industries)
                if up or down:
                    unmatched = [
                        row["name"] for row in up + down if row["limit_up"] is None
                    ]
                    note = ("口径：涨幅 = 板块**当天**涨跌幅；涨停数量 = 当日该板块涨停家数"
                            "（本地涨停池，与选股同一套）；主力净额单位**亿**（正=净流入）。")
                    if unmatched:
                        note += ("这些板块名在本地行业表里没有对应行业，涨停数量显示 —："
                                 + "、".join(unmatched[:3])
                                 + ("…" if len(unmatched) > 3 else ""))
                    return {"up": up, "down": down, "source": "sectors", "note": note}
                reason = "板块榜取到了，但没有一行带涨幅（排不出前五）"
            else:
                reason = "板块榜数据取不到（`data/sectors.py` 这次没返回数据）"
        else:
            reason = "还没有可用的板块榜模块（`data/sectors.py` 未就绪）"
        up, down = local_sector_tables(industries)
        note = (f"{reason}：现在按**本地口径**显示 —— 涨幅 = 近 5 日行业等权涨幅"
                "（**不是**当日涨幅），主力净额取不到所以显示 —。"
                "等板块榜可用后（点【立即刷新】）这里会自动换成当日口径。")
        return {"up": up, "down": down, "source": "local", "note": note}


    # 原来的 `percent_text()` / `signed_percent_text()`（"比例 → 百分数"）随
    # `IndustryEntry`（旧的热门板块条目控件）一起删掉了：2026-09-17 起热门板块是两张表，
    # 表里的 `pct` 直接就是**百分数**（板块榜给的口径），只有一个格式化函数
    # （`percent_value_text`）。留着那两个没人调的转换函数，迟早有人拿它去格式化
    # "已经是百分数"的值 —— 那正好是 ×100 的那个量级错误。

    def percent_value_text(value: Any) -> str:
        """**已经是百分数**的值（3.21）→ `+3.21%`；取不到是 `—`。

        为什么不能直接用"比例 → 百分数"那种转换（原来那个 `signed_percent_text` 会 ×100）：
        板块榜里的 `pct` 本来就是**百分数**（腾讯原文 `zdf=0.29` 就是 0.29%），
        再乘一次 100 会显示成 `+29.00%` —— 量级差 100 倍，而且**看起来还是个像样的数**，
        是最危险的一类错（`data/sectors.py` 的模块头专门讲过同一件事）。
        所以两种口径各有一个函数，名字里就写着差在哪。
        """
        try:
            return f"{float(value):+.2f}%"
        except (TypeError, ValueError):
            return market.DASH

    def _load_icon(size: int | None = None) -> Any:
        """按尺寸取程序图标；资源缺失/读不出来时返回**空 QIcon**（不是异常）。

        为什么要包这一层：`assets.icon_png()` 在"图标没打进包 / 被误删"时返回 None，
        而 `QIcon(None)` 会抛异常 —— 图标是锦上添花，绝不该让窗口起不来。
        `size` 传了但那一档不存在时，`assets` 自己会退回主图（内部逻辑，见 assets.py）。
        """
        try:
            path = assets.icon_png(size) if size else assets.icon_png()
        except Exception as exc:  # noqa: BLE001 - 资源层任何毛病都不该影响开窗口
            logger.debug(f"图标定位失败：{exc}")
            return QIcon()
        if not path:
            return QIcon()
        try:
            return QIcon(str(path))
        except Exception as exc:  # noqa: BLE001 - 文件坏了也一样降级
            logger.debug(f"图标加载失败：{exc}")
            return QIcon()

    def _set_app_icon(app: Any) -> None:
        """给 QApplication 也设一份图标。

        只给窗口设是不够的：Windows 任务栏/Alt-Tab 在某些情况下取的是**应用**图标，
        不设就会退回 python.exe 的默认图标（用户一眼就能看出"这是拿 Python 跑的"）。
        """
        icon = _load_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)

    class Worker(QThread):
        """通用工作线程：把可调用对象丢到后台跑，结果通过信号回主线程。

        为什么要这样：PySide6 里**只有主线程能碰控件**。下载 10 年数据要十几分钟，
        直接在按钮回调里跑会"窗口未响应"（Windows 还会弹"程序无响应"）。
        """

        progress = Signal(str, int, int)
        finished_ok = Signal(object)
        failed = Signal(str)

        stage = Signal(str)
        #: 状态（"正在重签 URL 继续下载（第 2 次）"）—— 与进度分开：
        #: 进度条回答"还剩多少"，状态回答"此刻在干什么"
        note = Signal(str)

        def __init__(self, fn, *args, with_progress: bool = False,
                     with_stage: bool = False, with_note: bool = False,
                     **kwargs) -> None:
            super().__init__()
            self._fn = fn
            self._args = args
            self._kwargs = kwargs
            self._with_progress = with_progress
            self._with_stage = with_stage
            self._with_note = with_note

        def run(self) -> None:  # noqa: D102
            try:
                kwargs = dict(self._kwargs)
                if self._with_progress:
                    kwargs["progress_cb"] = self._emit_progress
                if self._with_stage:
                    kwargs["stage_cb"] = self.stage.emit
                if self._with_note:
                    kwargs["note_cb"] = self.note.emit
                result = self._fn(*self._args, **kwargs)
                self.finished_ok.emit(result)
            except Exception as exc:  # noqa: BLE001 - 工作线程异常也必须回主线程提示
                logger.exception("后台任务异常")
                self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

        def _emit_progress(self, stage: str, done: int, total: int) -> None:
            self.progress.emit(stage, int(done), int(total))

    class MainWindow(QMainWindow):
        """主窗口。"""

        def __init__(self, cfg: Config | None = None) -> None:
            super().__init__()
            self.cfg = cfg or get_config()
            # 界面主题（皮肤）：在搭界面**之前**应用 —— 控件一出生就带着皮肤，
            # 不会出现"先按原生画一遍、再被样式表刷一遍"的闪动。
            # 主题名非法/素材缺失都在 `theme` 层兜住（退回默认主题 / 纯色背景条）
            theme_mod.apply_theme(QApplication.instance(),
                                  getattr(self.cfg, "ui_theme", None))
            # 数据目录可能在只读盘/权限不足：界面照开，状态栏挂提示（别直接崩）
            self._startup_problem = self.cfg.ensure_dirs_message()
            if not self._startup_problem and self.cfg.config_warning():
                # 配置写错（例如 TOML 语法错）会静默退回默认值 —— 必须让用户看见
                self._startup_problem = self.cfg.config_warning()
            if self._startup_problem:
                logger.error(self._startup_problem)
            self.engine = DataEngine(self.cfg.db_path)
            self.scheduler = Scheduler(self.cfg, self.engine)
            self._worker: Worker | None = None
            #: 概览页自己的后台取数线程（与 `_worker` 分开：那个是"有任务在跑"的判据，
            #: 概览每分钟都取一次，混进去会让下载/选股被误判成"忙"
            self._market_worker: Worker | None = None
            self._tray_notified = 0
            #: 运行状态里的瞬时消息（任务完成/失败提示），与实时状态分两层显示
            self._message = ""
            self._message_at = 0.0
            #: 这条消息是不是"进度回传"（下载中让位给会动的下载进度，见 `_compose_status`）
            self._message_is_progress = False
            #: 最近一次进度回传的"阶段 + 数量"（`下载 daily-k 81/181 MB`），补在下载进度后面
            self._progress_stage_text = ""
            #: 当前有没有任务在跑（**细进度条只在它为真时可见**，见 `_set_progress_visible`）
            self._job_running = False
            #: 「状态详情」弹窗与它里面的文本（测试与"重复点详情"都要能拿到）
            self.status_dialog: Any = None
            self.status_details_text: Any = None
            self.status_details_cache = ""
            #: 池子表格的内容指纹（内容没变就不重建单元格控件）
            self._pool_signature: tuple | None = None
            #: 持仓表格的内容指纹（同上：不重建就不会丢选中行与滚动位置）
            self._position_signature: tuple | None = None
            #: 启动自检结果（三态）与"数据不足"那一行提示（**不再是模态向导**）
            self.preflight_result: dict | None = None
            #: 竞价强度快照：`{symbol: 展示字段}`（见 `request_auction` / `_auction_tick`）
            self.auction_snapshot: dict[str, dict] = {}
            self._auction_worker: Worker | None = None
            #: 数据概况的缓存与时间戳（见 `_summary_cached`）
            self._summary: dict | None = None
            self._summary_at = 0.0
            #: 上一次刷新是不是因为"正在下载"被跳过了（跳过了就要在结束后补一次）
            self._heavy_paused = False
            #: 最近一次的大盘概览（拿不到就是 None）——测试与"复制/追查原因"都从它取值
            self.market_overview: dict | None = None
            #: 最近一次的**热门行业**（`pool.hot_industries()` 的原样结果，键=行业名）。
            #: 与概览同一个节拍取回来（见 `_market_payload`）——「热门板块」那一块画的就是它
            self.market_industries: dict[str, dict] = {}
            #: 「关于软件」对话框（测试与"重复点关于"都要能拿到它）
            self.about_dialog: Any = None
            #: 「关于软件」里的图标标签（资源缺失时为 None）
            self.about_icon: Any = None
            #: 提醒浮窗（QQ 式）与「提醒详情」对话框；都是**用一次建一次、之后复用**
            self.alert_popup: Any = None
            self.alert_detail_dialog: Any = None
            self.alert_detail_box: Any = None
            #: 已经提醒过的键 `(日期, 标的, 类型)`；None = 还没跟库对过账
            #: （第一次只记账不弹：启动时把今天早上的提醒全弹一遍是骚扰）
            self._alert_seen: set[tuple] | None = None
            #: 图标闪烁：是否正在闪 / 当前亮的是哪一张 / 红点图标 / 闪烁代次
            self._flashing = False
            self._flash_on = False
            self._flash_icon: Any = None
            self._normal_tray_icon: Any = None
            self._flash_token = 0
            self._cancel_download = False

            # 标题栏**只写软件名**（用户给定的布局：标题区就是"软件名 + 运行状态 +
            # 显示详情 + 关于软件"）。原来挂的"v0.1.0（Windows 单机版 · 测试版）"
            # 在 1366 宽 + 125% 缩放的机器上只剩一串省略号，而版本/版权本来就有
            # 【关于软件】与【显示详情】两个正经去处（报障时要贴的是那里那份文本）。
            self.setWindowTitle(APP_NAME)
            # 窗口图标用 256 那份（任务栏/Alt-Tab/标题栏都会取它）。
            # 拿不到图标就什么都不设，保持系统默认 —— 没有图标也要能用
            window_icon = _load_icon()
            if not window_icon.isNull():
                self.setWindowIcon(window_icon)
            self._apply_screen_geometry()
            self._build_ui()
            self._build_tray()
            self._start_scheduler()

            # 实时行情快照缓存（两张表的现价/涨幅）：**没有自己的 QTimer**，
            # 跟着下面这个 5 秒拍子走、内部按 60 秒限流（见 `quotes.QuoteService.tick`）
            self.quotes = quotes_mod.QuoteService(
                self.cfg, self._quote_symbols, self
            )
            # 快照一到就重画两张表（不然要等下一个 5 秒拍子，用户会觉得"现价没更新"）
            self.quotes.updated.connect(self._on_quotes_updated)

            # 界面刷新定时器：运行状态 + 两张表 + 托盘消息 + 提醒对账
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(5000)

            # 「大盘概览」页**自己的**定时器：每分钟一次。
            # 为什么不搭上面那个 5 秒的顺风车：概览是 4~6 个接口请求，
            # 5 秒一轮等于每分钟打 48 次（配额与限流都吃不消）；而且这一页
            # 只在"用户正看着它"时才需要新鲜数（见 `_market_tick`）。
            self._market_timer = QTimer(self)
            self._market_timer.timeout.connect(self._market_tick)
            self._market_timer.start(MARKET_REFRESH_MS)
            # 竞价强度自己的定时器：每分钟一次；真正去取的前提是"在竞价窗口内"
            self._auction_timer = QTimer(self)
            self._auction_timer.timeout.connect(self._auction_tick)
            self._auction_timer.start(AUCTION_REFRESH_MS)
            # 提醒的"图标闪烁"定时器：有新提醒时开始交替托盘图标，到点（或用户点开
            # 浮窗/主窗口）立刻停 —— 见 `_start_alert_flash` / `_stop_alert_flash`
            self._flash_timer = QTimer(self)
            self._flash_timer.timeout.connect(self._flash_step)
            # 切到这一页时立刻刷一次（定时器是整分钟对齐的，切过来时可能差几十秒到点）
            self.tabs.currentChanged.connect(self._on_tab_changed)
            # 启动第一屏就是概览页（它是第一个页签）：起来后立刻取一次，
            # 别让用户对着 — 等满一分钟。放 singleShot(0) 里是**先让窗口画出来**，
            # 真正的请求在后台线程（见 request_market_overview）——
            # 启动时数据自检也在跑，两条都压在主线程上界面就会"出来了但点不动"
            QTimer.singleShot(0, self._market_tick)

            self._tick()
            if self._startup_problem:
                self._set_status("⚠️ " + self._startup_problem.replace("\n", " "))
            else:
                # 启动自检放在最后：窗口已经能显示，用户先看到界面再看到结论
                QTimer.singleShot(0, lambda: self.run_preflight(auto=True))

        # ── 界面搭建 ──

        def _apply_screen_geometry(self) -> None:
            """按屏幕可用区域定默认尺寸、收最小尺寸、把窗口居中（见 `fit_window_geometry`）。

            为什么不能写死 `resize(1120, 720)`：1366×768 的笔记本（外加任务栏与 125%
            DPI 缩放）逻辑可用高度只有 680 上下，写死 720 就是"一打开最下边就被切掉"。
            拿不到屏幕信息（极端环境）时才退回 `WINDOW_PREFERRED_SIZE`。
            """
            geometry = fit_window_geometry()
            if geometry is None:
                self.resize(*WINDOW_PREFERRED_SIZE)
                return
            size, minimum, position = geometry
            # 先设最小尺寸再 resize：反过来的话 Qt 会先按"布局的最小值"把窗口顶大，
            # 之后再 resize 也收不回来（这正是用户遇到的那个 bug 的机制）
            self.setMinimumSize(minimum)
            self.resize(size)
            self.move(position)

        def resizeEvent(self, event: Any) -> None:
            """窗口尺寸变了 → 重排概览页每个组的条目列数（5 列 → 3/2/1 列）。

            放在窗口这一层算的理由：概览页在页签里，宽度由窗口决定；
            在窗口这里算一次就够，页面不用自己猜可用宽度。窗口还没搭完时
            （`__init__` 里先 resize 再 `_build_ui`）直接跳过。
            """
            super().resizeEvent(event)
            if getattr(self, "market_scroll", None) is not None:
                self._apply_market_columns()

        def _build_ui(self) -> None:
            central = QWidget()
            layout = QVBoxLayout(central)
            # 统一间距：整窗只有这两组值（页面外边距 14/12、控件间距 8），
            # 不再出现"这里 16、那里 4"的参差
            layout.setContentsMargins(*WINDOW_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            # 标题区：软件名 · 运行状态 + 【显示详情】【关于软件】（见 `_build_title_area`）。
            # **这一行不放任何操作按钮** —— 下载/刷新在「系统设置」，选股在「策略选股」
            layout.addWidget(self._build_title_area())

            # 五个页签（顺序即用户给定的顺序，见 `TAB_TITLES`）
            self.tabs = QTabWidget()
            # 1) 大盘概览：只读的一页，看盘第一眼要扫到（Qt 启动就显示第一个页签）
            self.tabs.addTab(self._build_market_page(), TAB_MARKET)
            # 2) 自选股池：策略/公式选出来的 + 手工加的自选，**同一张表**（靠「来源」区分）
            self.watch_page = self._build_watch_pool_page()
            self.tabs.addTab(self.watch_page, TAB_WATCH)
            # 3) 持仓监控：只记 代码 + 成本价 + 备注（用户给定的字段）
            self.position_page = self._build_position_page()
            self.tabs.addTab(self.position_page, TAB_POSITION)

            # 4) 策略选股：**顶部一行【开始选股】+ 原来的公式编辑器**（编辑器原样挂过来，
            #    这一阶段只换"挂法"）
            self.formula_page = FormulaPage(self.cfg, status_cb=self._toast)
            # 【开始选股】按钮在「策略选股」页里（B2b 的 `FormulaPage` 提供），
            # 它通过 `start_pick_requested` 信号请主窗口跑选股 —— 主窗口这边**只连一次**：
            # 用 `getattr` 而不是直接取属性，是因为两个模块正在并行改，彼此不该因为
            # "对方还没落地"而 import 就崩（连不上时页面上少一个按钮，而不是程序起不来）。
            start_pick = getattr(self.formula_page, "start_pick_requested", None)
            if start_pick is not None:
                start_pick.connect(self.on_run_pipeline)
            else:
                logger.warning("FormulaPage 还没有 start_pick_requested 信号："
                               "「策略选股」页暂时没有【开始选股】入口")
            self.tabs.addTab(self._build_formula_tab(), TAB_FORMULA)

            # 5) 系统设置：数据来源（含【下载数据】/【刷新数据】/【检查盘面】）+ 其余各组
            self.settings_page = self._build_settings_tab()
            self.tabs.addTab(self.settings_page, TAB_SETTINGS)

            # stretch=1：多余的高度**全部给页签区**（页签里的表格/滚动区自己吸收高度变化），
            # 上面的标题区与页面内的页脚都拿固定高度。
            # 这样窗口高度变小的时候，是"列表少显示几行"，而不是"底部被切到屏幕外"。
            layout.addWidget(self.tabs, 1)

            self.setCentralWidget(central)

        def _build_title_area(self) -> Any:
            """标题区：一行「软件名 · 运行状态 + 两个按钮」，下面是一行提示与细进度条。

            为什么把原来的三行（主状态 + 右侧短标签 + 七个按钮）压成一行：
            用户原话"太啰嗦、很多词看不懂"，而那七个按钮里**三个属于数据、一个属于选股**，
            挤在一起谁也说不清点下去会发生什么。现在：

            - 运行状态（`status_label`）是**唯一**说"现在在干什么"的地方：
              正在下载 48% / 正在刷新 / 正在选股 / 正在检查数据 /
              数据就绪 · 更新到 09-11 · 5560 只 / 数据不足（一句话 + 指路到
              「系统设置」里的【下载数据】）；
            - 右侧那几个短标签（今日池子 / 持仓 / 下次选股 / 盘中提醒）**取消**：
              池子只数在「自选股池」的表头上（`共 12 只（策略 10 · 自选 4）`）、
              持仓在「持仓监控」里、"盘中提醒"进运行状态那一句、其余事实都在
              【显示详情】里 —— 一件事只说一次，标题区才留得住"现在在干什么"；
            - 【显示详情】【关于软件】放最右（全局操作靠右，符合习惯），4 个字说全；
            - **细进度条只在任务运行时出现**（`PROGRESS_BAR_HEIGHT` 像素、不显示百分比文字）：
              常驻一条灰条既占地方，又会被误读成"卡在 0%"。
            """
            area = QWidget()
            # 背景条（铺拉丝纹理的那一条）：标题区与概览页页脚，见 theme.py
            area.setObjectName("statusArea")
            outer = QVBoxLayout(area)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(4)

            row = QHBoxLayout()
            row.setSpacing(10)
            self.status_row_layout = row
            self.app_title_label = QLabel(APP_NAME)
            # 软件名比正文大两号加粗（层级只有三级：页面标题 +2 / 数值与状态 +1 / 其余）
            self.app_title_label.setFont(
                _scaled_font(self.app_title_label.font(), FONT_TITLE_DELTA, bold=True)
            )
            row.addWidget(self.app_title_label)
            ruler = QLabel("·")           # 分隔符：小号灰字，不抢视线
            ruler.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            row.addWidget(ruler)
            prefix = QLabel("运行状态：")
            prefix.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            row.addWidget(prefix)
            self.status_label = ElidedLabel("正在初始化…")
            # 状态文本比正文大一号
            self.status_label.setFont(
                _scaled_font(self.status_label.font(), FONT_VALUE_DELTA)
            )
            row.addWidget(self.status_label, 1)
            # 【显示详情】与【关于软件】放最右：一个回答"这些数是什么"，一个放版本与版权
            self.btn_details = QPushButton(BTN_DETAILS_TEXT)
            self.btn_details.setToolTip(
                "状态详情：本地行数、股票数、持仓浮动、日志文件、自检阈值（可复制）"
            )
            self.btn_details.clicked.connect(self.on_show_status_details)
            row.addWidget(self.btn_details)
            self.btn_about = QPushButton(BTN_ABOUT_TEXT)
            self.btn_about.setToolTip("版本号 / 作者 / 版权 / 数据来源（报障时先看这里）")
            self.btn_about.clicked.connect(self.on_about)
            row.addWidget(self.btn_about)
            outer.addLayout(row)

            # 「本地还没有行情数据 · 在【系统设置】里下载」这一类**一行提示**。
            # 为什么不做成弹窗：用户要的是"下一步点哪里"，不是被一个模态窗口拦住；
            # 提示挂在标题区会一直留着，直到数据下好（比弹一次就消失的对话框可靠）
            self.first_run_hint = ElidedLabel("")
            self.first_run_hint.setObjectName("firstRunHint")
            self.first_run_hint.setVisible(False)
            outer.addWidget(self.first_run_hint)

            # 细进度条：**只在任务运行时可见**。不显示文字（百分比在运行状态那句话里）
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
            self.progress.setFixedHeight(PROGRESS_BAR_HEIGHT)
            self.progress.setVisible(False)
            outer.addWidget(self.progress)

            self.status_area = area
            return area

        # ── 「自选股池」页 ──

        def _build_watch_pool_page(self) -> Any:
            """自选股池：**策略/公式选出来的 + 手工加的自选，同一张表**。

            为什么合成一张（原来分成「股票池」与「自选股」两页）：两页说的是同一批票
            （自选本来就会并进池子），用户要在两页之间来回对"我加的那只进了没有"；
            合成之后靠「来源」列区分，表头上再写一句 `共 N 只（策略 M · 自选 K）`。

            表里三列各有各的口径（都写在 `_refresh_pool_table` 里）：
            「现价/涨幅」= 实时快照优先、否则本地**不复权**收盘价并标 `*`；
            「板块」= `stock_basic.industry`；「提醒」= 今天该股最新一条盘中提醒。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            head = QHBoxLayout()
            head.setSpacing(PAGE_SPACING)
            title = QLabel("自选股池")
            title.setFont(_scaled_font(title.font(), FONT_TITLE_DELTA, bold=True))
            head.addWidget(title)
            # 表头右侧那一行小字：`共 12 只（策略 10 · 自选 4）`。
            # 为什么要两个数：M + K 可能大于 N（既是策略选中又是自选时只算一行），
            # 把两个数都写出来，用户才不会觉得"数字对不上"
            self.pool_count_label = QLabel("")
            self.pool_count_label.setObjectName("statusTag")    # 小号灰字
            self.pool_count_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            head.addStretch(1)
            head.addWidget(self.pool_count_label)
            layout.addLayout(head)

            layout.addLayout(self._build_watch_row())

            self.pool_table = QTableWidget(0, len(WATCH_HEADERS))
            self.pool_table.setHorizontalHeaderLabels(list(WATCH_HEADERS))
            self._stretch(self.pool_table)
            # 新加的三列各写一句"这一列什么意思"（表头只有两个字，写不下口径）
            self._set_header_tooltip(
                self.pool_table, WATCH_HEADERS.index("市值"),
                "流通市值**亿**（取实时快照；没有实时快照时显示 `—`，不是 0）"
            )
            self._set_header_tooltip(
                self.pool_table, WATCH_HEADERS.index("换手"),
                "实时换手率**%**（取实时快照；没有实时快照时显示 `—`，不是 0）"
            )
            self._set_header_tooltip(
                self.pool_table, WATCH_MONITOR_COLUMN,
                "监控开关：`开启` / `关闭`（**点这一格就能切换**，与右键【关闭监控】/"
                "【打开监控】是同一件事）。\n"
                "自选可以开关；策略/公式选中的票不是自选，没有「停用」这一说"
                "（点它会给一句指路的话）。\n"
                "鼠标停在这一格上可以看到**今天最新一条提醒**的完整内容。"
            )
            # 三件事统一：悬浮看备注、右键删除/开关监控、单击名称开雪球
            self._enable_row_interactions(self.pool_table, "pool")
            layout.addWidget(self.pool_table, 1)

            self.pool_empty_label = QLabel(
                "池子还是空的：点「策略选股」里的【开始选股】，或在上面填代码加自选"
            )
            self.pool_empty_label.setWordWrap(True)
            layout.addWidget(self.pool_empty_label)
            return page

        def _build_watch_row(self) -> Any:
            """自选股操作行：代码 + 备注 → 【添加自选】（回车即加）。

            删除 / 关闭监控 不在这里放按钮：它们现在在**右键菜单**里 ——
            行级操作跟着行走，用户右键哪一行就是操作哪一行，
            不会像原来那样"先在表格里选中、再点按钮、结果点错了行"。
            """
            row = QHBoxLayout()
            row.setSpacing(PAGE_SPACING)
            self.watch_symbol = QLineEdit()
            self.watch_symbol.setPlaceholderText("代码（6 位）")
            # 回车 = 点【添加自选】：加自选常常是"看到一只就敲代码回车"的连击动作
            self.watch_symbol.returnPressed.connect(self.on_watch_add)
            self.watch_note = QLineEdit()
            self.watch_note.setPlaceholderText("备注（例如 龙头、消息面）")
            # 备注里回车也等于添加：填完备注顺手回车，不该要求他再挪鼠标
            self.watch_note.returnPressed.connect(self.on_watch_add)
            self.btn_watch_add = QPushButton("添加自选")
            self.btn_watch_add.setObjectName("primaryAction")   # 主操作按钮（见 theme.py）
            self.btn_watch_add.setToolTip("加入自选并立刻出现在下面的表里（回车即加）")
            self.btn_watch_add.clicked.connect(self.on_watch_add)
            row.addWidget(self.watch_symbol, 1)
            row.addWidget(self.watch_note, 2)
            row.addWidget(self.btn_watch_add)
            row.addStretch(1)
            return row

        def _build_position_page(self) -> Any:
            """持仓监控：**代码 + 成本价 + 备注 → 【添加持仓】**，同一行右侧是 T策略 开关。

            为什么把 T策略 放在这一页：它就是"持仓相关的开关"，用户在这儿看持仓时
            一眼就能决定开不开；它**与「系统设置 → T策略」里的是同一个配置键**
            （`intraday_t`），两边写回同一处 `config.toml`，不存在两个真相。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            head = QHBoxLayout()
            head.setSpacing(PAGE_SPACING)
            title = QLabel("持仓监控")
            title.setFont(_scaled_font(title.font(), FONT_TITLE_DELTA, bold=True))
            head.addWidget(title)
            self.position_count_label = QLabel("")
            self.position_count_label.setObjectName("statusTag")
            self.position_count_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
            head.addStretch(1)
            head.addWidget(self.position_count_label)
            layout.addLayout(head)

            layout.addLayout(self._build_position_row())

            self.position_table = QTableWidget(0, len(POSITION_HEADERS))
            self.position_table.setHorizontalHeaderLabels(list(POSITION_HEADERS))
            self._stretch(self.position_table)
            self._enable_row_interactions(self.position_table, "position")
            # 列头 tooltip = 那一列的说明书（用户不用翻 README 就知道按什么口径算）
            self._set_header_tooltip(
                self.position_table, POSITION_HEADERS.index("盈亏比例"),
                "盈亏比例 =（现价 − 成本价）÷ 成本价。\n"
                "现价与左边那一列**是同一个价**：有实时快照就用实时价，没有就用本地最新"
                "收盘价（鼠标停在「现价」那一格上会写明这是哪天的收盘价）——"
                "绝不用后复权价冒充现价。\n"
                "取不到价时显示 `—`（不是 0.00%：那会被误读成「刚好打平」）。"
            )
            self._set_header_tooltip(
                self.position_table, POSITION_HEADERS.index("市值"),
                "流通市值**亿**（取实时快照；没有实时快照时显示 `—`，不是 0）"
            )
            self._set_header_tooltip(
                self.position_table, POSITION_HEADERS.index("换手"),
                "实时换手率**%**（取实时快照；没有实时快照时显示 `—`，不是 0）"
            )
            self._set_header_tooltip(
                self.position_table, POSITION_HEADERS.index("止损位"),
                "止损位 = 成本价 ×（1 − 止损比例）；比例在「系统设置 → T策略」里改"
            )
            self._set_header_tooltip(
                self.position_table, POSITION_HEADERS.index("止盈位"),
                "止盈位 = 成本价 ×（1 + 止盈比例）；比例在「系统设置 → T策略」里改"
            )
            self._set_header_tooltip(
                self.position_table, POSITION_MONITOR_COLUMN,
                "监控开关：`开启` / `关闭`（**点这一格就能切换**，与右键【关闭监控】/"
                "【打开监控】是同一件事）。\n"
                "关闭 = 这只票不再产生任何盘中提醒（止损/止盈/做T/竞价/异动）。\n"
                "鼠标停在这一格上可以看到**今天最新一条提醒**的完整内容"
                "（止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点/做T/竞价/异动）。"
            )
            layout.addWidget(self.position_table, 1)
            return page

        def _build_formula_tab(self) -> Any:
            """策略选股页 = **FormulaPage 自己那一整页**（它内部已有【策略编辑】【开始选股】）。

            改版后这一页的按钮与流程**都由 `FormulaPage` 起头**：它那个【开始选股】
            只 `emit start_pick_requested()`，主窗口把这个信号接到 `on_run_pipeline`
            （见 `_build_ui` 里的连接）—— 整条流程留在 `scheduler.run_daily()` 一处。

            主窗口这边**不自己再造一个【开始选股】**：同一页出现两个同名按钮，用户按哪个
            都可能，而"哪个才真的会跑"只能靠猜（这正是"一处只管一件事"要避免的）。
            它只做三件事：
            1. 连信号（`_build_ui`）；
            2. 把 `btn_run` 指向页面那个按钮 —— 托盘菜单、主题样式、测试都按这个名字找它，
               名字一改，那几处会**静默**失效（Qt 不报错，只是点了没反应）；
            3. **兜底**：两个模块并行改的中间态里，`FormulaPage` 可能还没有那个按钮或信号 ——
               那时自己补一行按钮，并且让它的点击**走同一条路**（有信号就 emit 信号，
               连信号都没有才直接连流程入口）。**绝不出现点了没反应的死按钮。**
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            # **左右不留边距**：里面的 FormulaPage 自己带着页面边距（PAGE_MARGINS），
            # 外层再加 14×2 会把整窗的**最小宽度**顶大 28 像素 ——
            # 公式页的最小宽度本来就大（右侧 41 个按钮的面板），
            # 多这 28 像素就足够让 1366×768 那档屏幕上的窗口缩不到位（实报过"最下边看不见"）。
            layout.setContentsMargins(0, PAGE_MARGINS[1], 0, PAGE_MARGINS[3])
            layout.setSpacing(0)

            signal = getattr(self.formula_page, "start_pick_requested", None)
            self.btn_run = getattr(self.formula_page, "btn_start_pick", None)
            if self.btn_run is not None and signal is None:
                # 页面有按钮、却没有那个信号（中间态）：直接连流程入口，
                # 否则用户点下去什么都不发生 —— 那是"死按钮"，比少一个按钮更糟
                logger.warning("FormulaPage 有【开始选股】按钮但没有 start_pick_requested "
                               "信号：暂时把点击直接接到选股流程")
                self.btn_run.clicked.connect(self.on_run_pipeline)
            if self.btn_run is None:
                row = QHBoxLayout()
                row.setContentsMargins(PAGE_MARGINS[0], 0, PAGE_MARGINS[2], 0)
                row.setSpacing(PAGE_SPACING)
                title = QLabel(TAB_FORMULA)
                title.setFont(_scaled_font(title.font(), FONT_TITLE_DELTA, bold=True))
                row.addWidget(title)
                # 【开始选股】：跑所有**启用**的策略 + 公式 → 结果直接进自选股池 → 发一条消息
                self.btn_run = QPushButton(BTN_START_TEXT)
                self.btn_run.setObjectName("primaryAction")
                self.btn_run.setToolTip(
                    "开始选股：增量数据 → 跑启用的策略与公式 → 结果直接进「自选股池」"
                    " → 按「系统设置」里的通知方式推送一条"
                )
                # **走信号**（有的话）：与页面自己的按钮同一条路 —— 一处逻辑、两个入口，
                # 就不会出现"两个按钮各连一套流程"这种迟早对不上的写法
                if signal is not None:
                    self.btn_run.clicked.connect(lambda _=False: signal.emit())
                else:
                    self.btn_run.clicked.connect(self.on_run_pipeline)
                row.addWidget(self.btn_run)
                # `ElidedLabel` 而不是 QLabel：这行说明很长，普通 QLabel 的**最小宽度**
                # 就是整句话的宽度 —— 它会一路把整窗的最小宽度顶上去（同上）
                hint = ElidedLabel("结果直接进「自选股池」；策略与公式的启停在下面的列表里勾")
                hint.setObjectName("statusTag")
                hint.setForegroundRole(QPalette.ColorRole.PlaceholderText)
                row.addWidget(hint, 1)
                layout.addLayout(row)
            layout.addWidget(self.formula_page, 1)
            return page

        @staticmethod
        def _set_header_tooltip(table: Any, column: int, text: str) -> None:
            """给某一列的**表头**写 tooltip（用户不用翻文档就知道这一列按什么口径算）。"""
            item = table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(text)

        def _enable_row_interactions(self, table: Any, kind: str) -> None:
            """两张表统一的行交互：悬浮看备注、右键菜单、单击名称开雪球。

            为什么做成一个方法而不是两处各写一遍：这几件事的**行为约定**（只有合法 6 位
            代码才跳转、右键点的是哪一行就操作哪一行）必须处处一致，抄两遍必然抄歪一处。
            """
            table.setMouseTracking(True)   # 没有它就没有 hover 事件（Qt 默认只在按住时跟踪）
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(
                lambda pos, t=table, k=kind: self._on_table_menu(t, pos, k)
            )
            table.cellClicked.connect(
                lambda row, column, t=table: self._on_table_cell_clicked(t, row, column)
            )

        def _build_position_row(self) -> Any:
            """持仓操作行：代码 + 成本价 + 备注 → 【添加持仓】，右侧是 T策略 开关。

            **不再收"数量"**（用户给定的字段只有代码 / 成本价 / 备注）：库里
            `position.quantity` 保留做兼容，新加的持仓写 0，界面上不出现
            （要算"该买多少股"是条件单那类功能的事，不属于这个"看一眼就知道该干什么"的工具）。
            """
            row = QHBoxLayout()
            row.setSpacing(PAGE_SPACING)
            self.pos_symbol = QLineEdit()
            self.pos_symbol.setPlaceholderText("代码（6 位）")
            # 回车 = 点【添加持仓】。录持仓是"填完就想确认"的动作，
            # 不该逼用户把鼠标挪到按钮上（手不离键盘更快，也不容易点错行）
            self.pos_symbol.returnPressed.connect(self.on_add_position)
            self.pos_cost = QLineEdit()
            self.pos_cost.setPlaceholderText("成本价")
            self.pos_cost.returnPressed.connect(self.on_add_position)
            self.pos_note = QLineEdit()
            self.pos_note.setPlaceholderText("备注（例如 试仓、突破前高）")
            self.pos_note.returnPressed.connect(self.on_add_position)
            self.btn_pos_add = QPushButton("添加持仓")
            self.btn_pos_add.setObjectName("primaryAction")
            self.btn_pos_add.setToolTip("记一笔持仓：代码 + 成本价（+ 备注），回车即加")
            self.btn_pos_add.clicked.connect(self.on_add_position)
            row.addWidget(self.pos_symbol, 1)
            row.addWidget(self.pos_cost, 1)
            row.addWidget(self.pos_note, 2)
            row.addWidget(self.btn_pos_add)

            # T策略开关：与「系统设置 → T策略」里的是**同一个配置键**（`intraday_t`），
            # 默认关（用户拍板）。放在添加按钮右边 = "这一页的开关"，与录持仓同一行不碍事
            self.t_strategy_box = QCheckBox("T策略")
            self.t_strategy_box.setChecked(bool(getattr(self.cfg, "intraday_t", False)))
            self.t_strategy_box.setToolTip(
                "持仓做T的**近似**提示（默认关）：\n"
                "只有 60 秒一张的行情快照算出的「高抛/低吸」提醒，无法回测；\n"
                "开关写在 config.toml 的 intraday_t，与「系统设置 → T策略」里的是同一个"
            )
            self.t_strategy_box.toggled.connect(self.on_toggle_t_strategy)
            row.addWidget(self.t_strategy_box)
            row.addStretch(1)
            return row

        # ── 大盘概览页（第一个页签；数据口径见 market.py）──

        def _build_market_page(self) -> Any:
            """「大盘概览」页：**四块**（成交与情绪 / 宽基指数 / 情绪指数 / 热门板块）+ 单行页脚。

            用户 2026-09-17 给定的布局（顺序也是用户给的）：竖直排列、窄窗口自动换列，
            整页**四块**：

            1. 「成交与情绪」：**最上面**那一块 —— 成交额（沪+深 一个数）+ 涨停/跌停/炸板 +
               上涨/下跌/平盘，**7 个小条目排成一行**（窄屏自动换行）；
            2. 「宽基指数」：上证 / 深成 / 创业板 / 科创50 / 沪深300 + 上证50 / 中证1000
               —— 点位 + 涨跌幅（各自按涨跌上色）；
            3. 「情绪指数」：同花顺情绪/板块指数，**只有指数条目**（小条目都搬去第 1 块了）；
            4. 「热门板块」：**上涨前五 + 下跌前五两张表**（板块名称/涨停数量/涨幅/主力净额）。

            为什么把这一块挪到最上面：用户原话是"把'成交额等元素'模块挪到「宽基指数」上面"——
            成交额与涨跌停/涨跌家数是"今天市场整体什么状态"的第一眼信息，
            指数点位是第二眼；顺序换了之后，第一行就把"钱和情绪"说完了。

            结构（也是测试的断言对象）：
                page ─┬─ 标题行（页面标题，按钮在页脚）
                      ├─ market_scroll（可伸缩：窗口变矮时它自己滚动，页面底部那行不会被挤出去）
                      │    └─ 成交与情绪块 + 宽基指数块 + 情绪指数块 + 热门板块块
                      ├─ market_hint（**只在有话说时出现**：取数失败 / 涨跌家数关掉 / 板块没数据）
                      └─ market_footer（一行：数据来源小字 + 右对齐【立即刷新】）

            为什么内容区套滚动区：概览的条目数由配置与行情决定（可能十几项），
            如果不给滚动区，窗口一矮就是"页面被切掉"；给滚动区之后，
            可伸缩的部分自己吸收高度变化，**页脚与窗口底部永远在屏幕内**。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            header = QHBoxLayout()
            header.setSpacing(PAGE_SPACING)
            self.market_title = QLabel(TAB_MARKET)
            # 页面标题 = 最大一级字号（层级：页面标题 > 分区标题 = 正文；数值比标签大一号）
            self.market_title.setFont(
                _scaled_font(self.market_title.font(), FONT_TITLE_DELTA, bold=True)
            )
            header.addWidget(self.market_title)
            header.addStretch(1)
            layout.addLayout(header)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)      # 内容宽度跟着窗口走
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            # 概览页永远不该出现横向滚动条（用户明确要求）：条目列数会跟着宽度收缩，
            # 真出现"横向拖着看"就说明列数算错了 —— 直接禁用，宁可纵向滚动
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            content = QWidget()
            content_layout = QVBoxLayout(content)
            content_layout.setContentsMargins(0, 0, 0, 0)
            content_layout.setSpacing(10)
            #: 块标题 → 整块控件（**顺序就是页面顺序**，见 `MARKET_SECTION_TITLES`）。
            #: 数据来源：成交与情绪块只吃 `market.kpi_values()` 的那 7 个数（没有指数条目）；
            #: 宽基块只吃 `market_indices`；情绪块吃 `market_sentiment_indices`
            #: + `market_sector_indices` 两组（同一类同花顺板块指数）；
            #: 热门板块块的数据来自 `data/sectors.py` 的板块榜 + `pool.hot_industries()`
            #: 的涨停家数（见 `sector_payload`）。
            self.market_sections: dict[str, Any] = {
                MARKET_SECTION_FLOW: MarketSection(
                    MARKET_SECTION_FLOW, (),
                    with_stats=True,
                ),
                MARKET_SECTION_WIDE: MarketSection(
                    MARKET_SECTION_WIDE, ("indices",),
                ),
                MARKET_SECTION_SENTIMENT: MarketSection(
                    MARKET_SECTION_SENTIMENT, ("sentiment", "sector"),
                ),
                MARKET_SECTION_HOT: HotSectorsSection(MARKET_SECTION_HOT),
            }
            for title in MARKET_SECTION_TITLES:
                content_layout.addWidget(self.market_sections[title])
            content_layout.addStretch(1)         # 条目少时靠上排，不散在页面中间
            scroll.setWidget(content)
            self.market_scroll = scroll
            self.market_content = content
            layout.addWidget(scroll, 1)

            # 取不到数据时的原因写在这里（正常时**隐藏**）：光看一排 `—` 用户猜不出为什么
            self.market_hint = QLabel("")
            self.market_hint.setWordWrap(True)
            self.market_hint.setVisible(False)
            layout.addWidget(self.market_hint)

            # 页脚：**一行**（左边数据来源小字、右边【立即刷新】），按钮不另起一行。
            # 做成独立控件（`market_footer`）是为了让"只有一行"这件事可断言：
            # 布局项数、以及按钮与标签的 y 中心是否在同一条线上。
            self.market_footer = QWidget()
            self.market_footer.setObjectName("marketFooter")   # 页脚也是一条背景条
            footer = QHBoxLayout(self.market_footer)
            footer.setContentsMargins(0, 0, 0, 0)
            footer.setSpacing(PAGE_SPACING)
            self.market_as_of_label = ElidedLabel(market.footer_text(None))
            # 可选中复制：这行常被贴到群里（宽度不够时显示成省略号，tooltip 里是全文）
            self.market_as_of_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            footer.addWidget(self.market_as_of_label, 1)   # 占满剩余宽度，按钮固定靠右
            self.btn_market_refresh = QPushButton("立即刷新")
            self.btn_market_refresh.setToolTip(
                "立刻重新取一次（平时每分钟自动刷新一次；本按钮会忽略缓存）"
            )
            self.btn_market_refresh.clicked.connect(self.on_market_refresh_clicked)
            footer.addWidget(self.btn_market_refresh, 0)
            self.market_footer_layout = footer
            layout.addWidget(self.market_footer, 0)

            self.market_page = page
            #: 每个指数条目的控件（`thscode` / `name_label` / `value_label` / `pct_label`）
            self.market_entries: list[Any] = []
            #: 「热门板块」两张表的数据（`sector_payload()` 的结果：up/down/source/note）
            self.market_sectors: dict[str, Any] = {
                "up": [], "down": [], "source": "", "note": "",
            }
            self._market_columns = 0
            self._market_stat_columns = 0
            self._render_market_overview()   # 先画空骨架：第一秒就是"能看"的样子
            return page

        def _market_item_need(self) -> int:
            """一行条目"至少要多宽"，按**当前字体实测**（不是写死的常量）。

            为什么要实测：写死 260 的算法在 Linux 上刚好，到 Windows 就出事 ——
            同一段中文在 Windows 的 CJK 回退字体下更宽（CI 实测：整页最小宽度
            1160 > 视口 866，横向就差 294px），而列数还按 260 算着 3 列，
            于是每一列都不够宽、文字被裁、整页出现横向滚动条。
            字体是唯一会变的变量，所以这里直接量 sizeHint：条目要多少就给多少，
            给不了就少放几列（窗口变窄 → 列数从 5 降到 3/2/1，条目往下排）。

            量的口径：各块里最宽的那个条目（指数条目是三段）。sizeHint 与当前宽度无关，
            所以这个值稳定、不会来回抖。
            """
            need = 0
            for section in (getattr(self, "market_sections", None) or {}).values():
                for entry in section.entries:
                    labels = [label for label in (
                        getattr(entry, "name_label", None),
                        getattr(entry, "value_label", None),
                        getattr(entry, "pct_label", None),
                    ) if label is not None]
                    if labels:
                        need = max(need, sum(l.sizeHint().width() for l in labels)
                                   + MARKET_ITEM_GAP * (len(labels) - 1))
            # 兜底：条目还没建出来（首屏空骨架）时用常量，免得算出 0 列
            return max(MARKET_ENTRY_MIN_WIDTH, need + MARKET_ITEM_MARGIN)

        def _market_stat_need(self) -> int:
            """一个小条目"至少要多宽"（实测：名称 + 数值 + 间隙）。

            与小条目**列数**单独算的理由：小条目是 7 条（用户要求"宽屏一行摆 7 个"），
            而指数条目最多 5 列（`MARKET_MAX_COLUMNS`）—— 两者上限不同，
            所以这里量的是小条目自己的宽度，与 `_market_item_need()` 分开。
            """
            need = 0
            for section in (getattr(self, "market_sections", None) or {}).values():
                for item in section.stats.values():
                    # 量**控件自己**的需要宽度（`sizeHint` 已经把内部的间距/边距算进去了）。
                    # 手工"名称宽 + 数值宽"会少算那几个像素，实测就差 2px：列宽 120 时
                    # 数值只分到 57px，而它需要 59px —— 文字被悄悄截掉一点点，正是最难发现的那种。
                    need = max(need, int(item.sizeHint().width()))
            # 兜底：还没建出来时用常量（免得算出 0 列）。**实测值优先**：
            # 用户要求"这 7 条排成一排"，而常量下限（原来写死 130）在小屏上正好会把
            # 第 7 条挤到第二行 —— 实测多少就是多少，宽度不够时再靠列数收缩。
            return (need + MARKET_STAT_MARGIN) if need else MARKET_STAT_MIN_WIDTH

        def _apply_market_columns(self) -> None:
            """按可用宽度与**实测条目宽度**决定每组每行放几个条目（小条目单独算）。

            为什么要响应式列数：用户要求"不出现横向滚动条或被截断"。
            窗口变窄（或换一台字体更宽的机器）→ 列数自动往下掉，
            条目自己往下排（纵向滚动），而不是把第三列挤没、或让文字被裁掉。
            小条目最多 7 列（宽屏一行 7 个）、指数条目最多 5 列 —— 两套上限分开算，
            但用的是同一份"按实测宽度收缩"的机制（用户要求"窄屏允许换行，
            沿用现有响应式列数机制"）。
            """
            scroll = getattr(self, "market_scroll", None)
            sections = getattr(self, "market_sections", None)
            if scroll is None or not sections:
                return
            # 用**滚动区**的宽度而不是视口宽度：视口宽度会随着纵向滚动条出现而少十几像素，
            # 拿它算列数容易出现"滚动条一出现列数就掉一档"的抖动
            width = max(scroll.width(), self.market_page.width()) - 2 * PAGE_MARGINS[0]
            columns = max(1, min(MARKET_MAX_COLUMNS,
                                 width // self._market_item_need()))
            # 小条目：把**间距**算进去，按"最宽那条"决定能放几列（等宽列，所以这样才不截字）。
            # 为什么这么在意这一行：用户要求"这 7 条排成一排"，而 7 列在 920 逻辑宽下
            # 只差几个像素 —— 条目的内边距/间距（见 `MarketStatItem` 与下面的间距常量）
            # 是**故意收紧**的，收过头会变成"文字被截"，放松一点就换行。两个都是缺陷，
            # 所以这几个像素被测试钉住了（`test_overview_columns_fit_the_screen_without_clipping`）。
            stat_need = self._market_stat_need()
            stat_columns = max(1, min(MARKET_STAT_MAX_COLUMNS,
                                      (width + MARKET_STAT_GRID_SPACING)
                                      // (stat_need + MARKET_STAT_GRID_SPACING)))
            if columns == self._market_columns \
                    and stat_columns == self._market_stat_columns:
                return
            self._market_columns = columns
            self._market_stat_columns = stat_columns
            for section in sections.values():
                section.set_columns(columns, stat_columns)

        def on_market_refresh_clicked(self) -> None:
            """【立即刷新】：**忽略 TTL 缓存**重取一次（用户手点的按钮就该立刻见效）。"""
            self.request_market_overview(force=True)

        def _market_page_visible(self) -> bool:
            """概览页此刻是不是真的"在用户眼前"（当前页 + 窗口没被最小化/隐藏）。

            为什么要判这么细：取数是 4~6 个请求，在别的页面上、或窗口缩到托盘里
            每分钟白刷一轮纯属浪费配额，也更容易撞上限流。
            """
            try:
                return bool(self.tabs.currentWidget() is self.market_page and self.isVisible())
            except Exception:  # noqa: BLE001 - 界面还没搭完时问这些也算"不可见"
                return False

        def _market_tick(self) -> None:
            """概览页自己的定时器（**每 60 秒**一次，与 5 秒的界面刷新解耦）。

            只有"页面在眼前"时才真去取；取不取由 `market` 层的 TTL（55 秒）决定，
            所以这一分钟里如果刚切过页、刚点过刷新，这里只是读缓存。
            启动那一次也走它（见 `__init__` 里的 `QTimer.singleShot(0, ...)`）。
            """
            if not self._market_page_visible():
                return
            self.request_market_overview()

        def _on_tab_changed(self, _index: int) -> None:
            """切到概览页时立刻刷一次（缓存没过期就只是重画缓存）。

            为什么必要：定时器是整分钟对齐的，用户切过来时可能正好差几十秒到点 ——
            看盘的人不该盯着"一分钟前的旧数"。顺便重排一次条目列数：
            非当前页签里的控件宽度不参与布局，切过来之后宽度才作数。
            """
            if self.tabs.currentWidget() is self.market_page:
                self._apply_market_columns()
                self.request_market_overview()

        def request_market_overview(self, force: bool = False) -> None:
            """界面路径取数：**丢到后台线程**，取回来再回主线程渲染。

            为什么必须后台（而不是在主线程里直接取）：
            - 启动时数据自检（`run_preflight`）也在主线程上跑，两件事叠在一起会让
              "窗口已经出来了却点不动"；
            - 断网/服务端不响应时每个请求要等满超时（4~6 个请求叠起来很可观），
              主线程被它按住就是整个界面卡住。
            这也和程序里其它联网动作（下载 / 只刷新数据 / 盘中检查）保持一致。

            同时只允许一轮在飞：上一轮还没回来就跳过这一轮（60 秒定时器与切页动作
            可能挨得很近，各起一个线程会把请求数凭空翻倍）。

            Args:
                force: 忽略 TTL 缓存（【立即刷新】用）。
            """
            if self._market_worker is not None and self._market_worker.isRunning():
                return
            worker = Worker(self._market_payload, force=force)
            self._market_worker = worker
            worker.finished_ok.connect(self._on_market_overview_ready)
            worker.failed.connect(self._on_market_overview_failed)
            worker.start()

        def _market_payload(self, force: bool = False, client: Any = None) -> dict:
            """**一趟取齐**「大盘概览」要的三份数据：行情概览 + 热门行业 + 板块榜两张表。

            为什么三件事一起取（而不是各开一个线程）：
            - 它们更新的是**同一页**、同一个刷新节拍（60 秒一次），分线程会出现
              "表头写 10:31、热门板块还是 10:30 的"这种半新半旧；
            - 热门行业是一次本地库查询（当日涨停密度 + 近 5 日行业等权涨幅），
              板块榜是一次网络取数（`sectors.fetch_sector_rank()`）——
              两件都能在后台线程里跑，但**都不能放主线程**：库里几百万行行情时那几条
              聚合查询够让界面顿一下，而板块榜断网时要等满超时（与"卡死"是同一类毛病）。
            """
            try:
                overview = market.fetch_overview(self.cfg, client=client, force=force)
            except Exception as exc:  # noqa: BLE001 - market 层已"绝不抛"，这里再兜一层
                logger.warning(f"大盘概览取数异常：{type(exc).__name__}: {exc}")
                overview = None
            industries: dict[str, dict] = {}
            try:
                industries = pool.hot_industries(self.cfg.db_path, top=MARKET_HOT_TOP)
            except Exception as exc:  # noqa: BLE001 - 热门板块取不到只让那一块空着
                logger.debug(f"取热门行业失败（热门板块空着）：{exc}")
            # 板块榜取不到就退回本地口径（`sector_payload` 里写清了原因，页面会显示）
            sectors_data = sector_payload(self.cfg, industries)
            return {
                "overview": overview,
                "industries": industries,
                "sectors": sectors_data,
            }

        def _on_market_overview_ready(self, payload: Any) -> None:
            """后台取回来了（回主线程执行）：记下结果并重画页面。

            兼容两种入参：`_market_payload()` 那个 `{overview, industries, sectors}`
            字典，以及**直接一份 overview**（老调用方/测试直接喂一份概览时不该炸）。
            """
            if isinstance(payload, dict) and "overview" in payload:
                overview = payload.get("overview")
                self.market_industries = payload.get("industries") or {}
                sectors_data = payload.get("sectors")
                if isinstance(sectors_data, dict):
                    self.market_sectors = sectors_data
            else:
                overview = payload
            self.market_overview = overview if isinstance(overview, dict) else None
            self._render_market_overview()

        def _on_market_overview_failed(self, message: str) -> None:
            """后台取数抛异常（`market` 层已"绝不抛"，这里是双保险）：原因写在页面上。"""
            first = str(message).splitlines()[0] if message else "未知错误"
            logger.warning(f"大盘概览取数失败：{first}")
            self.market_hint.setText(f"⚠️ 大盘概览取数失败：{first}")
            self.market_hint.setToolTip(str(message))
            self.market_hint.setVisible(True)

        def refresh_market_overview(
            self, force: bool = False, client: Any = None
        ) -> dict | None:
            """**同步**取一次大盘概览（含热门板块）并刷到页面上（`force=True` 忽略 TTL 缓存）。

            与 `request_market_overview()` 的分工：
            - 界面自己发起的取数（启动 / 定时器 / 切页 / 【立即刷新】）走**后台线程**那条，
              主线程一次都不阻塞；
            - 这个方法在调用者线程里同步跑完，给"已经拿着取数对象"的场景用：
              自动化测试（注入假客户端，保证完全离线且即时可断言）与将来可能的命令行复用。

            Args:
                force: 忽略缓存。
                client: 注入的取数对象（测试注入假客户端）。

            Returns:
                最近一次的概览 dict（同时写进 `self.market_overview`）；拿不到是 None。
            """
            payload = self._market_payload(force=force, client=client)
            self._on_market_overview_ready(payload)
            return self.market_overview

        def _render_market_overview(self) -> None:
            """把概览数据画到页面上（四块 + 单行来源页脚 + 页内提示）。

            五件事：
            - **成交与情绪块**（最上面那块）：7 个小条目（成交额 = 沪+深 / 涨停 / 跌停 /
              炸板 / 上涨 / 下跌 / 平盘），数值来自 `market.kpi_values()`
              （与 `--cli --market` 同一份口径）；**始终可见**（与"配没配指数"无关）；
            - **宽基 / 情绪两块指数**：每个指数一个条目控件，点位与涨跌幅**各按自己的
              涨跌上色**（涨=红、跌=绿、平/缺=默认色，色值只在 `market.py` 里定义一次）；
            - **热门板块**：上涨前五 / 下跌前五两张表（`self.market_sectors`，
              `sector_payload()` 的产物：板块榜 + 本地涨停家数）；
            - **空块**：`market_indices` 没配 → 宽基块**连标题一起隐藏**（不留空标题）；
              配了但取不到数 → 一个 `—` 占位；板块榜没数据 → 两张表空着 + 一行说明；
            - **页内提示**（`market_hint`）：取数失败的原因、以及"涨跌家数为什么是 `—`"
              （`market_breadth` 关着就不取全市场快照，`_market_hint_notes` 负责说清）。

            用 `setStyleSheet("color:…")` 上色而不是富文本 HTML：`text()` 里带标签会让断言
            变脆，而这里只改前景色，样式表是最直接的一层。
            """
            overview = self.market_overview
            values = market.kpi_values(overview)
            configured = set((overview or {}).get("configured_groups") or [])

            flow = self.market_sections[MARKET_SECTION_FLOW]
            # 这一块**始终可见**：它装的是"钱与情绪"，与配没配指数无关
            flow.setVisible(True)
            flow.set_items([])
            for name, text, tip in self._market_stat_rows(values, overview):
                flow.set_stat(name, text, tooltip=tip)

            wide = self.market_sections[MARKET_SECTION_WIDE]
            # 没配这一组 → 整块隐藏（连标题），而不是留一个空标题在那吊着
            wide.setVisible("indices" in configured)
            wide.set_items(list((overview or {}).get("indices") or []))

            sentiment = self.market_sections[MARKET_SECTION_SENTIMENT]
            # 情绪块**始终可见**：用户给定它是固定四块之一（配空了就只有标题 + `—`）
            sentiment.setVisible(True)
            sentiment_items: list[dict] = []
            for key in sentiment.keys:
                if key in configured:
                    sentiment_items.extend(list((overview or {}).get(key) or []))
            sentiment.set_items(sentiment_items)

            hot = self.market_sections[MARKET_SECTION_HOT]
            hot.setVisible(True)
            sector_data = self.market_sectors or {}
            hot.set_rows(
                {
                    SECTOR_UP_TITLE: list(sector_data.get("up") or []),
                    SECTOR_DOWN_TITLE: list(sector_data.get("down") or []),
                },
                note=str(sector_data.get("note") or ""),
            )

            entries: list[Any] = []
            for title in MARKET_SECTION_TITLES:
                section = self.market_sections[title]
                if self._market_columns:
                    # `set_items` 重建条目后列数会归零：这里按当前列数重排一次，
                    # 否则新建出来的条目会全挤在同一行上（右边被裁掉）
                    section.set_columns(self._market_columns, self._market_stat_columns)
                if title not in (MARKET_SECTION_HOT, MARKET_SECTION_FLOW):
                    entries.extend(section.entries)
            self.market_entries = entries

            self.market_as_of_label.setFullText(market.footer_text(overview))

            # 原因写进 tooltip（鼠标一停就能看到）与 hint（只在有话说时出现，算"第二行"）
            detail = market.summary_text(overview)
            tooltip = ("大盘概览：" + detail) if detail else "大盘概览：暂无数据"
            for title in MARKET_SECTION_TITLES:
                self.market_sections[title].setToolTip(tooltip)
            self.market_page.setToolTip(tooltip)

            notes = self._market_hint_notes(overview)
            if notes:
                text = "；".join(notes[:2]) + ("…" if len(notes) > 2 else "")
                self.market_hint.setText("⚠️ " + text)
                self.market_hint.setToolTip("；".join(notes))
            else:
                self.market_hint.setText("")
            self.market_hint.setVisible(bool(notes))

            # 数据到位之后按当前宽度再排一次列数（首屏宽度可能与建好时不同）
            self._apply_market_columns()

        def _market_stat_rows(
            self, values: dict, overview: Any
        ) -> list[tuple[str, str, str]]:
            """9 个小条目的 `(名字, 数值, tooltip)` —— **一条一个数**。

            为什么不做成"三句话"（原来那样）：合成文本在 Windows 字体下会被自己的列宽
            截掉半个数字（CI 实测 338px 的需要 vs 231px 的实际），见上面
            `MARKET_STAT_TITLES` 的注释。拆开之后每条都短，任何字体都塞得下。
            `values` 来自 `market.kpi_values()`（与 `--cli --market` 同一份口径），
            所以这里不自己算数、只排版。
            """
            breadth_on = bool((overview or {}).get("breadth_enabled"))
            amount_tip = (
                "成交额 = **沪市 + 深市**（两个数相加，一个数）："
                "取自上证指数与深证成指的成交额（单位元，显示成「亿」）。"
                "与指数同一个节拍（每分钟刷新）。\n"
                "北交所成交额**不再显示**（2026-09-17 用户要求删掉这一格）"
            )
            limits_tip = "当日涨停 / 跌停 / 炸板家数（同花顺涨停池、跌停池、炸板池）"
            breadth_tip = ("全市场上涨 / 下跌 / 平盘家数"
                           "（翻 6 页全市场快照算出来的，每 5 分钟更新一次）")
            if not breadth_on:
                breadth_tip = ("现在是 `—`：market_breadth 关着，程序不去翻全市场快照"
                               "（省配额）。想看到它就在 config.toml 里把 "
                               "market_breadth 设成 true")
            # 顺序 = 界面上的顺序（宽屏一行 7 个）：**成交额在最前**，
            # 后面是涨跌停三个、涨跌家数三个（用户要求"与成交额排成一行"）。
            return [
                (MARKET_STAT_AMOUNT, values["成交额"], amount_tip),
                (MARKET_STAT_LIMIT_UP, values["涨停"], limits_tip),
                (MARKET_STAT_LIMIT_DOWN, values["跌停"], limits_tip),
                (MARKET_STAT_BREAK, values["炸板"], limits_tip),
                (MARKET_STAT_UP, values["上涨"], breadth_tip),
                (MARKET_STAT_DOWN, values["下跌"], breadth_tip),
                (MARKET_STAT_FLAT, values["平盘"], breadth_tip),
            ]

        def _market_hint_notes(self, overview: Any) -> list[str]:
            """页内提示要说的每一句（空列表 = 一切正常，提示区隐藏）。

            几件事各有各的说法，**不能只把 errors 贴出来**：
            - 取数失败/关闭：`market` 层已经给了中文原因（含"market_overview 已关闭"）；
            - `market_breadth` 关着 → 涨跌家数必然是一排 `—`，不说清用户会以为坏了；
            - 热门板块为空 → 告诉他是"本地还没有涨停池数据"（点【刷新数据】能补），
               而不是让他以为这个块本来就不显示东西（"板块榜为什么是空的"由
               `sector_payload()` 的 note 写在块里，那句话说清了口径与缺失原因）。

            后两条**只在"真的取过一轮"之后才说**（`as_of` 是那一轮的取数时间）：
            窗口刚起来、后台那一路还没回来时，任何"为什么没有数"的说法都是猜的 ——
            那一秒页面上的 `—` 只是"还没取"，不是缺陷。
            """
            data = overview or {}
            notes = [str(e) for e in (data.get("errors") or [])]
            fetched = bool(data.get("as_of"))
            if fetched and bool(getattr(self.cfg, "market_overview", True)):
                if not data.get("breadth_enabled"):
                    notes.append(f"{MARKET_STAT_UP} / {MARKET_STAT_DOWN} / "
                                 f"{MARKET_STAT_FLAT} 显示 {market.DASH}：market_breadth "
                                 "关着，不取全市场快照（打开就能看到）")
                if not (self.market_industries or {}):
                    notes.append("热门板块暂无数据：本地还没有涨停池数据，"
                                 f"在【{TAB_SETTINGS}】里点【{BTN_REFRESH_TEXT}】补齐")
            return notes

        def _build_settings_tab(self) -> Any:
            """「系统设置」页：**五组 + 底部一个【保存设置】**（一键写回本页所有设置）。

            五组与顺序由用户给定（`SETTINGS_GROUPS`）：数据来源 → 通知方式 →
            竞价扫描 → T策略 → 其他（第 4 组用户拍板叫「T策略」：做T开关 + 四个阈值 +
            止损/止盈比例，见 `SETTINGS_GROUPS` 的说明）。

            为什么要"一键保存"（而不是每组一个保存按钮）：原来四组各有各的保存按钮，
            用户勾完通知、改完止损，得记住"这两个按钮都要点一遍" —— 漏点哪个都是
            "改了没生效"，而且四个按钮里到底哪个管哪些键，界面上根本看不出来。
            现在**页面底部一个【保存设置】把本页所有设置一次写回**，
            校验失败时明确拒绝并点名哪一项（见 `_collect_settings_updates`）。

            为什么整页套一层滚动区：这一页控件最多（五组、三十多个控件），
            它们的"最小高度"合起来有 1000 像素以上；页签的最小高度取所有页的最大值，
            于是**整窗的最小高度**被这一页顶到屏幕外 —— 1366×768 的笔记本
            （可用高约 680）上窗口缩不小，底边直接被屏幕切掉（用户反馈的"最下边看不见"）。
            套上滚动区之后，页面能跟着窗口收缩，够不到的那几项滚动一下就看到了。

            保存仍然复用 `_save_updates` → `config.save_settings`：
            **只就地改这几个键**，用户自己写的注释与未知键全部保留。
            """
            inner = QWidget()
            layout = QVBoxLayout(inner)
            # 统一间距（只动边距与间距）
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)
            #: 组名 → 整组控件（顺序 = `SETTINGS_GROUPS`，测试按组名取块）
            self.settings_sections: dict[str, Any] = {}

            self._build_settings_source_group(layout)
            self._build_settings_notify_group(layout)
            self._build_settings_auction_group(layout)
            self._build_settings_risk_group(layout)
            self._build_settings_misc_group(layout)

            # ── 底部：**一个**【保存设置】写回本页所有设置 + 生效回显 ──
            save_row = QHBoxLayout()
            save_row.setSpacing(PAGE_SPACING)
            self.save_settings_button = QPushButton("保存设置")
            self.save_settings_button.setObjectName("primaryAction")
            self.save_settings_button.setToolTip(
                "把本页五组设置**一次**写回 config.toml（你写的注释与未知键都会保留）；"
                "任何一项填得不对会明确拒绝并指出是哪一项"
            )
            self.save_settings_button.clicked.connect(self.on_save_settings)
            save_row.addWidget(self.save_settings_button)
            save_row.addStretch(1)
            layout.addLayout(save_row)
            self.save_settings_hint = QLabel("")
            self.save_settings_hint.setObjectName("statusTag")
            self.save_settings_hint.setWordWrap(True)
            layout.addWidget(self.save_settings_hint)

            layout.addStretch(1)
            self._refresh_channel_hints()
            self._refresh_auction_hint()

            # 滚动区只是"外套"：里层控件、顺序、层级都在各组里
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            # 横向不出滚动条：这一页的最低宽度（约 640）在 1024 宽的屏上也装得下，
            # 真出现横向条说明哪里出了问题，宁可让它纵向滚动
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(inner)
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(scroll, 1)
            return page

        def _settings_group(self, layout: Any, title: str, note: str = "") -> Any:
            """建一组（带标题的块）并挂到页面上 → 返回**给调用方填控件的布局**。

            做成一个方法而不是五处各写一遍：五组的标题字号、边距、说明行的样式与
            "块要能被整块取到"（`settings_sections`）这几件事必须处处一致 ——
            抄五遍必然抄歪一处，而歪掉的那一组看起来就像"没做完"。
            """
            box = QFrame()
            box.setObjectName("settingsGroup")
            box.setFrameShape(QFrame.Shape.StyledPanel)
            outer = QVBoxLayout(box)
            outer.setContentsMargins(10, 8, 10, 8)
            outer.setSpacing(6)
            title_label = QLabel(title)
            # 组标题 = 数值那一级（比页面标题小一号、比正文大一号、加粗）
            title_label.setFont(
                _scaled_font(title_label.font(), FONT_VALUE_DELTA, bold=True)
            )
            outer.addWidget(title_label)
            if note:
                note_label = QLabel(note)
                note_label.setObjectName("statusTag")      # 小号灰字
                note_label.setWordWrap(True)
                outer.addWidget(note_label)
            body = QVBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(4)
            outer.addLayout(body)
            box.title_label = title_label        # 让测试能直接断言组标题文字
            box.body_layout = body
            self.settings_sections[title] = box
            layout.addWidget(box)                # 挂上页面（不这么做整页就是空的）
            return body

        def _build_settings_source_group(self, layout: Any) -> None:
            """第 1 组「数据来源」：**来源列表**（**同花顺是主源，公开源是兜底**）。

            用户要求的是"可用的其它源，让用户自主添加，用他自己的 key"，所以这一组的主体
            是一张**来源列表**：每行是「来源名 + 提供什么 + Key（或"免 Key" / 备用的申请地址）
            + 启用 + 删除」。**列表顺序 = 取数优先级**，所以 2026-09-17 起
            **顺序就是优先级**：2026-09-18 起同花顺（要 Key）排第一，公开源紧随其后兜底。

            规则：
            - 列表只画**已启用**的来源（`data_sources` 里写着的那些，顺序即优先级），
              其余已实现的来源进【添加来源】菜单（点了才写回 `data_sources`）；
              候选来自注册表，界面**不自己维护一份"还有哪些来源"**；
            - 列表里如果出现注册表里没有的名字，那只可能是用户手改过 `data_sources`：
              那一行**照实显示**并注明"界面还没有它的实现"，不假装认识它；
            - 需要 Key 的来源给 Key 输入框（键由注册表的 `key_config` 指出），
              **免 Key 的来源不给输入框**（公开源那种）—— 画一个填不了东西的框
              比不画更糟，用户会去找一个根本不存在的 Key；
            - **内置同花顺那一行没有输入框**（2026-09-17 用户要求）：改成一行说明
              `主来源：同花顺金融数据服务（需要 Key，申请地址 …）`，地址**可点开**；
              它的 Key 仍然照旧从 config.toml / 环境变量读（见 `_collect_settings_updates`）。
            """
            body = self._settings_group(
                layout, "数据来源",
                # ⚠️ 这一行是**用户可见**的小字（QLabel 不解析 markdown），别写 `**加粗**`
                # —— 那会原样显示成两个星号（用户明确说过不喜欢这种星号）。
                "列表顺序 = 取数优先级：同花顺是主源（需要 Key，见它那一行的申请地址）；"
                "公开行情源是兜底（免 Key，实测会被限流，只在没配 Key / Key 失效时用）。"
                f"【{BTN_DOWNLOAD_TEXT}】【{BTN_REFRESH_TEXT}】【{BTN_CHECK_TEXT}】"
                f"【{BTN_PAUSE_TEXT}】也都在这一组里",
            )
            # 配置里的**原值**也写出来："界面说的"与"config.toml 里写的"必须对得上
            # （认不出的键照实显示，不假装认识）—— 老属性名 `data_source_label` 保留
            self.data_source_label = QLabel(self._source_text(self.cfg.data_sources))
            self.data_source_label.setObjectName("statusTag")      # 小号灰字
            self.data_source_label.setWordWrap(True)
            body.addWidget(self.data_source_label)

            #: 来源键 → 那一行的控件（测试与"填 Key / 删除"都按这个名字取）
            self.source_rows: dict[str, Any] = {}
            self.source_list_layout = QVBoxLayout()
            self.source_list_layout.setContentsMargins(0, 0, 0, 0)
            self.source_list_layout.setSpacing(PAGE_SPACING)
            body.addLayout(self.source_list_layout)

            add_row = QHBoxLayout()
            add_row.setSpacing(PAGE_SPACING)
            self.btn_add_source = QPushButton("添加来源")
            self.btn_add_source.setToolTip(
                "把另一个**已实现**的来源加进来（每个来源用它自己的 Key）"
            )
            # 有候选时弹一个菜单（选一个加进来），没有候选时给如实说明 ——
            # 两个来源都加完/注册表读不出来时，绝不画"点了没用的条目"
            self.btn_add_source.clicked.connect(self.on_add_source_clicked)
            add_row.addWidget(self.btn_add_source)
            add_row.addStretch(1)
            body.addLayout(add_row)
            self.source_add_hint = QLabel("")
            self.source_add_hint.setObjectName("statusTag")      # 小号灰字
            self.source_add_hint.setWordWrap(True)
            body.addWidget(self.source_add_hint)

            #: 来源列表（一行一个来源）。Key 输入框与【测试连接】**长在行上**
            #: （`SourceRow.key_edit` / `SourceRow.btn_test`），不是窗口级的
            #: `self.key_edit` —— 因为"来源可以有多个、各用各的 Key"。
            #: 谁按窗口级的老属性找控件会立刻 AttributeError，比"悄悄拿不到东西"好查。
            self._rebuild_source_rows()
            # 「没配 Key」的那句话写在这里（**不弹窗**）：用户点【下载数据】时，
            # 这一行会因为缺 Key 亮起来、焦点也落到输入框上 —— 指路比拦住他更有效
            self.key_hint = QLabel("")
            self.key_hint.setWordWrap(True)
            self.key_hint.setVisible(False)
            body.addWidget(self.key_hint)

            data_row = QHBoxLayout()
            data_row.setSpacing(PAGE_SPACING)
            self.btn_download = QPushButton(BTN_DOWNLOAD_TEXT)
            self.btn_download.setObjectName("primaryAction")
            self.btn_download.setToolTip(
                "下载 / 更新历史数据：首次建库或补历史行情（可中断，下次接着传）"
            )
            self.btn_download.clicked.connect(self.on_download)
            data_row.addWidget(self.btn_download)
            # 【刷新数据】：只补数据（行情/涨停池/日历/行业/指数），不选股、不推送
            self.btn_refresh = QPushButton(BTN_REFRESH_TEXT)
            self.btn_refresh.setToolTip(
                "只刷新数据：补行情 / 涨停池 / 交易日历 / 行业 / 指数，不选股、不推送"
            )
            self.btn_refresh.clicked.connect(self.on_refresh_data)
            data_row.addWidget(self.btn_refresh)
            # 【检查盘面】：立刻按池子/持仓/自选跑一轮盘中提醒
            self.btn_check = QPushButton(BTN_CHECK_TEXT)
            self.btn_check.setToolTip("立即检查盘面：按当前池子 / 持仓 / 自选跑一次盘中提醒")
            self.btn_check.clicked.connect(self.on_intraday_once)
            data_row.addWidget(self.btn_check)
            # 【暂停提醒】：暂停盘中提醒（选股照跑），再点一次恢复。
            # 它同时也在托盘右键菜单里（盯盘时窗口多半是收起来的）
            self.btn_pause = QPushButton(BTN_PAUSE_TEXT)
            self.btn_pause.setToolTip("暂停盘中提醒（选股照跑）；再点一次恢复。托盘右键里也有")
            self.btn_pause.clicked.connect(self.on_toggle_intraday)
            data_row.addWidget(self.btn_pause)
            data_row.addStretch(1)
            body.addLayout(data_row)

            # 数据量：显示成"下载 6 个月"，可编辑的是 `history_years`（年）
            amount_row = QHBoxLayout()
            amount_row.setSpacing(PAGE_SPACING)
            self.data_amount_label = QLabel(
                f"下载 {self._history_months_text()}（history_years = "
                f"{float(self.cfg.history_years):g}）"
            )
            amount_row.addWidget(self.data_amount_label)
            self.history_years_box = QDoubleSpinBox()
            self.history_years_box.setRange(0.0, 20.0)     # 允许填 0，由校验明确拒绝
            self.history_years_box.setSingleStep(0.5)
            self.history_years_box.setDecimals(1)
            self.history_years_box.setSuffix(" 年")
            self.history_years_box.setValue(float(self.cfg.history_years))
            self.history_years_box.setToolTip(
                "下载多久的历史行情（超短线用不到长历史，默认 0.5 年 = 6 个月）。\n"
                "改它只影响**下一次下载**的范围；必须大于 0，也要大于自检门槛 "
                f"min_history_years（{float(self.cfg.min_history_years):g}）"
            )
            self.history_years_box.valueChanged.connect(self._sync_data_amount_label)
            amount_row.addWidget(self.history_years_box)
            amount_row.addStretch(1)
            body.addLayout(amount_row)

            # 数据目录与日志路径：**只读、可复制**（报障时要贴的就是这两行）
            for label, attr, value in (
                ("数据目录：", "data_dir_edit", str(self.cfg.data_dir)),
                ("日志路径：", "log_path_edit", self._log_path_text()),
            ):
                path_row = QHBoxLayout()
                path_row.setSpacing(PAGE_SPACING)
                path_row.addWidget(QLabel(label))
                edit = QLineEdit(value)
                edit.setReadOnly(True)               # 只读：这里是"看/复制"，不是"改"
                edit.setToolTip("只读、可复制（选中后 Ctrl+C，或用右边的【复制路径】）")
                setattr(self, attr, edit)
                path_row.addWidget(edit, 1)
                body.addLayout(path_row)
            self.btn_copy_paths = QPushButton("复制路径")
            self.btn_copy_paths.setToolTip("把数据目录与日志路径一起复制到剪贴板")
            self.btn_copy_paths.clicked.connect(self.on_copy_paths)
            body.addWidget(self.btn_copy_paths)

        def _build_settings_notify_group(self, layout: Any) -> None:
            """第 2 组「通知方式」：四个勾选框 + 各频道的参数。

            四个勾选框（用户给定的顺序）：**QQ 浮动消息（默认开）** / 系统弹窗（windows）/
            托盘气泡（tray）/ 飞书推送（feishu + 凭证三栏）。
            为什么把"QQ 浮动消息"单列一个框（它其实不是 `notify_channels` 里的一员）：
            它就是这个程序自绘的那扇浮窗（`notify_popup`），用户嘴里的"QQ 消息模式"指的就是它；
            藏在别处（原来只在 config.toml 里）会让人找不到"我到底开了没开浮窗"。

            参数都摆在勾选框下面：提示音、托盘图标闪烁秒数、浮窗停留秒数、浮窗条数上限。
            这四项以前**只有配置文件里能改**，而它们正是"太吵/听不见/看不到"时先要动的东西。
            """
            body = self._settings_group(
                layout, "通知方式",
                "四个勾选框可任选；都不勾 = 只入库不推送（消息照样进两张表的「提醒」列）。"
                "飞书需先填凭证",
            )
            self.popup_box = QCheckBox("QQ 浮动消息（右下角滑出，默认开）")
            self.popup_box.setChecked(bool(getattr(self.cfg, "notify_popup", True)))
            self.popup_box.setToolTip(
                "收到提醒时在右下角滑出一扇浮窗（可点开看详情、可拖走、鼠标停着不消失）。\n"
                "它就是用户嘴里那种「QQ 消息」模式：响声 + 图标闪烁 + 浮窗三件事一起，"
                "任一件关掉都不影响另外两件"
            )
            body.addWidget(self.popup_box)

            chosen = {str(c).lower() for c in (self.cfg.notify_channels or [])}
            self.channel_boxes: dict[str, Any] = {}
            labels = {"windows": "系统弹窗（Windows 原生）", "feishu": "飞书推送（卡片消息）",
                      "tray": "托盘气泡（气泡提示）"}
            for name in KINDS:
                box = QCheckBox(labels.get(name, name))
                box.setChecked(name in chosen)
                self.channel_boxes[name] = box
                body.addWidget(box)

            # 声音 / 闪烁 / 浮窗参数（都是"太吵或看不见"时要动的旋钮）
            param_row = QHBoxLayout()
            param_row.setSpacing(PAGE_SPACING)
            self.sound_box = QCheckBox("提示音")
            self.sound_box.setChecked(bool(getattr(self.cfg, "notify_sound", True)))
            self.sound_box.setToolTip("收到提醒响一声（浮窗模式下同样生效）")
            param_row.addWidget(self.sound_box)
            param_row.addWidget(QLabel("　图标闪烁："))
            self.flash_seconds_box = QSpinBox()
            self.flash_seconds_box.setRange(0, 120)
            self.flash_seconds_box.setSuffix(" 秒")
            self.flash_seconds_box.setValue(int(getattr(self.cfg, "notify_flash_seconds", 6)))
            self.flash_seconds_box.setToolTip(
                "托盘图标闪多少秒（0 = 不闪）；浮窗可能被别的窗口盖住，闪图标是最不会被忽略的一路"
            )
            param_row.addWidget(self.flash_seconds_box)
            param_row.addWidget(QLabel("　浮窗停留："))
            self.popup_seconds_box = QSpinBox()
            self.popup_seconds_box.setRange(1, 120)
            self.popup_seconds_box.setSuffix(" 秒")
            self.popup_seconds_box.setValue(
                int(getattr(self.cfg, "notify_popup_seconds", 8))
            )
            self.popup_seconds_box.setToolTip("浮窗多久自动收起（鼠标停在上面时不收）")
            param_row.addWidget(self.popup_seconds_box)
            param_row.addWidget(QLabel("　浮窗最多："))
            self.popup_items_box = QSpinBox()
            self.popup_items_box.setRange(1, 10)
            self.popup_items_box.setSuffix(" 条")
            self.popup_items_box.setValue(
                int(getattr(self.cfg, "notify_popup_max_items", 5))
            )
            self.popup_items_box.setToolTip("浮窗里最多列几条（连续的提醒会合并进同一扇窗）")
            param_row.addWidget(self.popup_items_box)
            param_row.addStretch(1)
            body.addLayout(param_row)

            # windows 参数
            self.win_sound_box = QCheckBox("弹窗带提示音")
            self.win_sound_box.setChecked(bool(self.cfg.notify_windows_sound))
            self.win_url_box = QCheckBox("弹窗按钮点击打开雪球")
            self.win_url_box.setChecked(bool(self.cfg.notify_windows_open_url))
            body.addWidget(self.win_sound_box)
            body.addWidget(self.win_url_box)

            # 托盘参数
            tray_row = QHBoxLayout()
            tray_row.addWidget(QLabel("托盘气泡显示时长（毫秒）："))
            self.tray_duration = QSpinBox()
            self.tray_duration.setRange(1000, 60000)
            self.tray_duration.setSingleStep(1000)
            self.tray_duration.setValue(int(self.cfg.notify_tray_duration_ms))
            tray_row.addWidget(self.tray_duration)
            tray_row.addStretch(1)
            body.addLayout(tray_row)

            # 飞书参数
            self.feishu_on_box = QCheckBox("启用飞书频道（feishu_on）")
            self.feishu_on_box.setChecked(bool(self.cfg.feishu_on))
            body.addWidget(self.feishu_on_box)
            feishu_row = QHBoxLayout()
            self.feishu_app_id = QLineEdit(self.cfg.feishu_app_id)
            self.feishu_app_id.setPlaceholderText("飞书 App ID")
            self.feishu_secret = QLineEdit(self.cfg.feishu_app_secret)
            self.feishu_secret.setPlaceholderText("飞书 App Secret")
            self.feishu_secret.setEchoMode(QLineEdit.EchoMode.Password)
            self.feishu_chat_id = QLineEdit(self.cfg.feishu_chat_id)
            self.feishu_chat_id.setPlaceholderText("目标 chat_id（可留空自动发现）")
            for widget in (self.feishu_app_id, self.feishu_secret, self.feishu_chat_id):
                feishu_row.addWidget(widget)
            body.addLayout(feishu_row)

            self.feishu_hint = QLabel("")
            self.feishu_hint.setWordWrap(True)
            body.addWidget(self.feishu_hint)

            row = QHBoxLayout()
            # 【发送测试提醒】：按**面板当前勾选**实发一条（不用先保存）——
            # "通知到底通不通"是这一页唯一必须马上验证的事，去掉它用户就只能等下次选股
            self.btn_test = QPushButton("发送测试提醒")
            self.btn_test.setToolTip(
                "按上面**当前**的勾选与参数立刻发一条测试消息（配置文件不会被它改动）"
            )
            self.btn_test.clicked.connect(self.on_test_notify)
            row.addWidget(self.btn_test)
            row.addStretch(1)
            body.addLayout(row)

        def _build_settings_auction_group(self, layout: Any) -> None:
            """第 3 组「竞价扫描」：开关 + 8 个参数（沿用现有控件与 handler）。"""
            # 为什么把这一组放在界面上：这一版竞价是"**全市场扫一遍再按规则过滤**"，
            # 规则就是用户手里那几个数字（涨幅区间、板块、成交额、分数、条数、扫描时刻）——
            # 让他为了改一个 2.0% 去手改 TOML 是不合理的。
            body = self._settings_group(
                layout, "竞价扫描",
                "全市场 9:15–9:25 的真实买卖盘；默认关，勾上才取数",
            )
            self.auction_on_box = QCheckBox("启用竞价扫描（勾上后默认 09:20 / 09:25 各扫一次）")
            self.auction_on_box.setChecked(bool(getattr(self.cfg, "intraday_auction", False)))
            self.auction_on_box.setToolTip(
                "全市场约 5600 只、按 100 只一批 → 约 56 个请求/次（20~30 秒，后台线程跑）；\n"
                "所以只在下面两个时刻各扫一次，不是每分钟扫。"
            )
            body.addWidget(self.auction_on_box)


            pct_row = QHBoxLayout()
            pct_row.addWidget(QLabel("竞价涨幅："))
            self.auction_min_pct_box = QDoubleSpinBox()
            self.auction_min_pct_box.setRange(0.0, 20.0)
            self.auction_min_pct_box.setSingleStep(0.5)
            self.auction_min_pct_box.setDecimals(1)
            self.auction_min_pct_box.setSuffix(" %")
            self.auction_min_pct_box.setValue(float(getattr(self.cfg, "auction_min_pct", 2.0)))
            self.auction_min_pct_box.setToolTip("低于它的直接过滤（默认 +2.0%）")
            pct_row.addWidget(self.auction_min_pct_box)
            pct_row.addWidget(QLabel("~"))
            self.auction_max_pct_box = QDoubleSpinBox()
            self.auction_max_pct_box.setRange(0.0, 21.0)
            self.auction_max_pct_box.setSingleStep(0.5)
            self.auction_max_pct_box.setDecimals(1)
            self.auction_max_pct_box.setSuffix(" %")
            self.auction_max_pct_box.setValue(float(getattr(self.cfg, "auction_max_pct", 9.0)))
            self.auction_max_pct_box.setToolTip(
                "≥ 它的直接过滤：一字板/接近涨停**买不进**，推了也没用（默认 9.0%）"
            )
            pct_row.addWidget(self.auction_max_pct_box)
            pct_row.addWidget(QLabel("　成交额 ≥"))
            self.auction_amount_box = QDoubleSpinBox()
            self.auction_amount_box.setRange(0.0, 100000.0)
            self.auction_amount_box.setSingleStep(100.0)
            self.auction_amount_box.setDecimals(0)
            self.auction_amount_box.setSuffix(" 万")
            self.auction_amount_box.setValue(
                float(getattr(self.cfg, "auction_min_amount", 5e6)) / 1e4
            )
            self.auction_amount_box.setToolTip(
                "竞价成交额不到这个数不参与（挡掉小盘爆表噪声）。\n"
                "默认 500 万 —— 全市场只有 8.7% 的股票过线；嫌严可以调到 300 万"
            )
            pct_row.addWidget(self.auction_amount_box)
            # 量比门槛（`auction_min_volume_ratio`）：原来只有 config.toml 能改的
            # **第 8 个参数** —— 它决定打分里「放量」那 2 分给不给（满分 6），
            # 界面上却看不到，用户想放松一档只能去手改 TOML
            pct_row.addWidget(QLabel("　量比 ≥"))
            self.auction_ratio_box = QDoubleSpinBox()
            self.auction_ratio_box.setRange(0.0, 20.0)
            self.auction_ratio_box.setSingleStep(0.5)
            self.auction_ratio_box.setDecimals(1)
            self.auction_ratio_box.setSuffix(" 倍")
            self.auction_ratio_box.setValue(
                float(getattr(self.cfg, "auction_min_volume_ratio", 2.0))
            )
            self.auction_ratio_box.setToolTip(
                "竞价量比到不了这个数，打分里「放量」那 2 分就拿不到（默认 2.0 倍）"
            )
            pct_row.addWidget(self.auction_ratio_box)
            pct_row.addStretch(1)
            body.addLayout(pct_row)

            board_row = QHBoxLayout()
            board_row.addWidget(QLabel("板块（多选）："))
            self.auction_board_boxes: dict[str, Any] = {}
            chosen_boards = set(getattr(self.cfg, "auction_boards", None)
                                or config.AUCTION_BOARDS)
            for key in config.AUCTION_BOARDS:
                box = QCheckBox(config.AUCTION_BOARD_LABELS[key])
                box.setChecked(key in chosen_boards)
                box.setToolTip("一个都不勾 = 不限制（等于全选）")
                self.auction_board_boxes[key] = box
                board_row.addWidget(box)
            board_row.addStretch(1)
            body.addLayout(board_row)

            score_row = QHBoxLayout()
            score_row.addWidget(QLabel("打分 ≥"))
            self.auction_score_box = QSpinBox()
            self.auction_score_box.setRange(*config.AUCTION_SCORE_RANGE)
            self.auction_score_box.setValue(int(getattr(self.cfg, "auction_min_score", 2)))
            self.auction_score_box.setToolTip(
                "竞价强度打分（满分 6）：涨幅 2 + 量比 2 + 买盘剩余 1 + 成交额 1。\n"
                "默认 2 —— 真实数据上命中面（覆盖当日后期涨停 24% vs 20%）比 3 划算"
            )
            score_row.addWidget(self.auction_score_box)
            score_row.addWidget(QLabel("　推送条数 ≤"))
            self.auction_items_box = QSpinBox()
            self.auction_items_box.setRange(*config.AUCTION_ITEMS_RANGE)
            self.auction_items_box.setValue(
                int(getattr(self.cfg, "auction_alert_max_items", 10))
            )
            self.auction_items_box.setToolTip("按分数排序取前 N 只（1~50）")
            score_row.addWidget(self.auction_items_box)
            score_row.addWidget(QLabel("　扫描时刻："))
            self.auction_scan_at_edit = QLineEdit(
                ", ".join(getattr(self.cfg, "auction_scan_at", None) or ["09:20", "09:25"])
            )
            self.auction_scan_at_edit.setPlaceholderText("09:20, 09:25")
            self.auction_scan_at_edit.setToolTip(
                "逗号分隔的时刻（HH:MM）。9:25 那次拿到的是**竞价终态**。\n"
                "为什么不填就每分钟扫：56 个请求 × 10 分钟会把配额打光"
            )
            score_row.addWidget(self.auction_scan_at_edit)
            score_row.addStretch(1)
            body.addLayout(score_row)

            self.auction_hint = QLabel("")
            self.auction_hint.setWordWrap(True)
            body.addWidget(self.auction_hint)

        def _build_settings_risk_group(self, layout: Any) -> None:
            """第 4 组「T策略」：T策略总开关 + **四个做T阈值** + **止盈/止损比例**。

            为什么补这一组：`stop_loss` / `take_profit` 这两个比例原来**界面上完全没有入口**
            （只能手改 config.toml），而「持仓监控」里的止损位/止盈位两列就是按它们算的 ——
            用户在界面上看得到结果却改不了参数，说不通。

            四个做T阈值（`t_high_min_gain_pct` / `t_high_pullback_pct` /
            `t_low_min_drop_pct` / `t_low_rebound_pct`）**原来也完全没有界面入口**：
            而它们决定"什么算冲高回落、什么算跌深反弹"，也就是提示多不多、灵不灵。
            现在摆在 T策略开关下面，并各自写清"跟谁比、比多少"。
            """
            body = self._settings_group(
                layout, "T策略",
                "做T提示的开关与四个阈值；止盈/止损比例也在这里设（用户拍板：在 T策略 里编辑）"
                "—— 止损位/止盈位 = 成本价 ×（1 ∓ 比例）",
            )
            risk_row = QHBoxLayout()
            risk_row.addWidget(QLabel("止损比例："))
            self.stop_loss_box = QDoubleSpinBox()
            # 下界刻意放到 0：**允许填 0，然后由【保存设置】明确拒绝**（"填 0 = 不想止损"
            # 这种误解必须在保存那一刻说清楚，而不是被控件悄悄夹成 0.5）
            self.stop_loss_box.setRange(0.0, 50.0)
            self.stop_loss_box.setSingleStep(0.5)
            self.stop_loss_box.setDecimals(1)
            self.stop_loss_box.setSuffix(" %")
            self.stop_loss_box.setValue(abs(float(self.cfg.stop_loss)) * 100)
            self.stop_loss_box.setToolTip("跌到「成本 ×（1 − 这个比例）」提示止损（默认 5%）")
            risk_row.addWidget(self.stop_loss_box)
            risk_row.addWidget(QLabel("　止盈比例："))
            self.take_profit_box = QDoubleSpinBox()
            self.take_profit_box.setRange(0.0, 200.0)
            self.take_profit_box.setSingleStep(0.5)
            self.take_profit_box.setDecimals(1)
            self.take_profit_box.setSuffix(" %")
            self.take_profit_box.setValue(abs(float(self.cfg.take_profit)) * 100)
            self.take_profit_box.setToolTip("涨到「成本 ×（1 + 这个比例）」提示止盈（默认 10%）")
            risk_row.addWidget(self.take_profit_box)
            risk_row.addStretch(1)
            body.addLayout(risk_row)
            # T策略：与「持仓监控」页那一行的复选框是**同一个配置键**（`intraday_t`）
            self.intraday_t_box = QCheckBox("T策略（持仓做T的近似提示，默认关）")
            self.intraday_t_box.setChecked(bool(getattr(self.cfg, "intraday_t", False)))
            self.intraday_t_box.setToolTip(
                "只有 60 秒一张的行情快照算出的「高抛/低吸」提示，**无法回测**；"
                "四个阈值就是下面这四个数字（config.toml 的 t_* 项）"
            )
            body.addWidget(self.intraday_t_box)

            # ── 四个做T阈值（各有各的"跟谁比"）──
            t_row = QHBoxLayout()
            t_row.setSpacing(PAGE_SPACING)
            t_box = QHBoxLayout()
            t_box.addWidget(QLabel("　冲高：相对昨收涨 ≥"))
            self.t_high_min_gain_box = QDoubleSpinBox()
            self.t_high_min_gain_box.setRange(0.0, 20.0)
            self.t_high_min_gain_box.setSingleStep(0.5)
            self.t_high_min_gain_box.setDecimals(1)
            self.t_high_min_gain_box.setSuffix(" %")
            self.t_high_min_gain_box.setValue(float(self.cfg.t_high_min_gain_pct))
            self.t_high_min_gain_box.setToolTip(
                "t_high_min_gain_pct：现价相对**昨收**至少涨这么多才算「冲高」（默认 2.0%）"
            )
            t_box.addWidget(self.t_high_min_gain_box)
            t_row.addLayout(t_box)
            t_box2 = QHBoxLayout()
            t_box2.addWidget(QLabel("　回落：从今日最高回 ≥"))
            self.t_high_pullback_box = QDoubleSpinBox()
            self.t_high_pullback_box.setRange(0.0, 20.0)
            self.t_high_pullback_box.setSingleStep(0.5)
            self.t_high_pullback_box.setDecimals(1)
            self.t_high_pullback_box.setSuffix(" %")
            self.t_high_pullback_box.setValue(float(self.cfg.t_high_pullback_pct))
            self.t_high_pullback_box.setToolTip(
                "t_high_pullback_pct：从**今日最高**回落这么多才算「见顶回落」（默认 1.5%）"
            )
            t_box2.addWidget(self.t_high_pullback_box)
            t_row.addLayout(t_box2)
            t_row.addStretch(1)
            body.addLayout(t_row)

            t_row2 = QHBoxLayout()
            t_row2.setSpacing(PAGE_SPACING)
            t_box3 = QHBoxLayout()
            t_box3.addWidget(QLabel("　杀跌：相对昨收跌 ≥"))
            self.t_low_min_drop_box = QDoubleSpinBox()
            self.t_low_min_drop_box.setRange(0.0, 20.0)
            self.t_low_min_drop_box.setSingleStep(0.5)
            self.t_low_min_drop_box.setDecimals(1)
            self.t_low_min_drop_box.setSuffix(" %")
            self.t_low_min_drop_box.setValue(float(self.cfg.t_low_min_drop_pct))
            self.t_low_min_drop_box.setToolTip(
                "t_low_min_drop_pct：现价相对**昨收**至少跌这么多才算「杀跌」（默认 2.0%）"
            )
            t_box3.addWidget(self.t_low_min_drop_box)
            t_row2.addLayout(t_box3)
            t_box4 = QHBoxLayout()
            t_box4.addWidget(QLabel("　止跌：从今日最低反弹 ≥"))
            self.t_low_rebound_box = QDoubleSpinBox()
            self.t_low_rebound_box.setRange(0.0, 20.0)
            self.t_low_rebound_box.setSingleStep(0.5)
            self.t_low_rebound_box.setDecimals(1)
            self.t_low_rebound_box.setSuffix(" %")
            self.t_low_rebound_box.setValue(float(self.cfg.t_low_rebound_pct))
            self.t_low_rebound_box.setToolTip(
                "t_low_rebound_pct：从**今日最低**反弹这么多才算「止跌回稳」（默认 1.0%）"
            )
            t_box4.addWidget(self.t_low_rebound_box)
            t_row2.addLayout(t_box4)
            t_row2.addStretch(1)
            body.addLayout(t_row2)
            t_note = QLabel(
                "　这四个阈值只管做T提示（默认关）；填 0 会被【保存设置】拒绝 ——"
                "0 等于「任何一点波动都算」，那种提示只会刷屏"
            )
            t_note.setObjectName("statusTag")
            t_note.setWordWrap(True)
            body.addWidget(t_note)

        def _build_settings_misc_group(self, layout: Any) -> None:
            """第 5 组「其他」：界面主题 + 当日异动提醒 + 自选股上限与是否进池。

            为什么"当日异动提醒"在这里：它是一条**独立于止损止盈**的消息源
            （全市场异动里挑自己的票），默认关（用户拍板：减少无用消息）——
            想开的人得找得到开关，不想开的人不该被它吵。
            """
            body = self._settings_group(
                layout, "其他", "零碎的偏好，改完点下面的【保存设置】")
            theme_row = QHBoxLayout()
            theme_row.addWidget(QLabel("界面主题："))
            self.theme_box = QComboBox()
            self.theme_box.setToolTip(
                "银色 = 金属感皮肤；系统默认 = 回到 Windows 原生外观（随时可切回）"
            )
            for name in config.UI_THEMES:
                self.theme_box.addItem(theme_mod.theme_label(name), name)
            self.theme_box.setCurrentIndex(
                max(0, self.theme_box.findData(
                    theme_mod.normalize_theme(self.cfg.ui_theme)))
            )
            # 先设好当前值**再**接信号：否则建页面时就会触发一次"保存设置"
            self.theme_box.currentIndexChanged.connect(self.on_theme_changed)
            theme_row.addWidget(self.theme_box)
            theme_row.addStretch(1)
            body.addLayout(theme_row)
            theme_hint = QLabel("换主题立即生效（不用重启），选择也会写回 config.toml")
            theme_hint.setObjectName("statusTag")     # 小号灰字（与状态区同一个样式）
            body.addWidget(theme_hint)

            self.anomaly_box = QCheckBox("当日异动提醒（涨停/跌停/大幅波动…，默认关）")
            self.anomaly_box.setChecked(bool(getattr(self.cfg, "intraday_anomaly", False)))
            self.anomaly_box.setToolTip(
                "全市场异动里**只挑自己的票**（池子/自选/持仓）提醒，一次请求；"
                "默认关（减少无用消息）。右键【关闭监控】的持仓不参与"
            )
            body.addWidget(self.anomaly_box)

            watch_row = QHBoxLayout()
            watch_row.setSpacing(PAGE_SPACING)
            watch_row.addWidget(QLabel("自选股上限："))
            self.watchlist_max_box = QSpinBox()
            self.watchlist_max_box.setRange(1, 500)
            self.watchlist_max_box.setSuffix(" 只")
            self.watchlist_max_box.setValue(int(getattr(self.cfg, "watchlist_max", 20)))
            self.watchlist_max_box.setToolTip(
                "超过上限时**明确提示**（不静默丢弃）：盘中只监控前 N 只自选"
            )
            watch_row.addWidget(self.watchlist_max_box)
            self.watchlist_in_pool_box = QCheckBox("自选股进池（一起盯、一起算盈亏提示）")
            self.watchlist_in_pool_box.setChecked(
                bool(getattr(self.cfg, "watchlist_in_pool", True))
            )
            self.watchlist_in_pool_box.setToolTip(
                "关掉之后自选股只显示在「自选股池」表里，不进盘中观察面（不产生提醒）"
            )
            watch_row.addWidget(self.watchlist_in_pool_box)
            watch_row.addStretch(1)
            body.addLayout(watch_row)

        def on_theme_changed(self, index: int = -1) -> None:
            """下拉框换主题：**立即生效**（不用重启）并写回 config.toml。

            为什么先应用再保存：换皮肤是"立刻想看效果"的操作，不能等写盘成功；
            写盘失败（只读盘/权限）时 `_save_updates` 会给出中文原因，皮肤已经换好了。
            """
            name = self.theme_box.itemData(index) if index >= 0 else None
            if name is None:
                name = self.theme_box.currentData()
            applied = theme_mod.apply_theme(QApplication.instance(), name)
            self._save_updates(
                {"ui_theme": applied},
                f"界面主题已切换为「{theme_mod.theme_label(applied)}」",
            )

        def _refresh_channel_hints(self) -> None:
            """把"飞书没配凭证"这类提示显示出来（勾了才提示，不弹错误框）。

            按**面板当前**的勾选与输入框内容判断，而不是按已保存的配置 ——
            用户刚把 App ID 填进去还没保存时，也该立刻看到提示消失。
            """
            import dataclasses

            current = dataclasses.replace(self.cfg, **self._panel_notify_updates())
            states = current.channel_states()
            parts = [f"{name}：{states[name]}" for name in KINDS]
            line = "｜".join(parts)
            if self.channel_boxes.get("feishu") is not None and \
                    self.channel_boxes["feishu"].isChecked() and not current.feishu_ready:
                line = "⚠️ 已勾选飞书但未配置凭证 —— 发送时会提示「未配置飞书凭证，已跳过」，" \
                       "其余频道照常。" + line
            self.feishu_hint.setText(line)

        @staticmethod
        def _stretch(table: Any) -> None:
            """两张表的列宽策略：**名称列按内容给够，其余列平分剩下的宽度**。

            2026-09-17：列变多了（自选股池 8 列、持仓监控 10 列），如果所有列都平分，
            760 宽的窗口下每列只有 64 像素 —— `名称(代码)` 会被省略成"低价样本(60…"，
            而名字正是用户最需要看清的那一列（他刚说过"所有名称显示不清楚"）。
            所以名称列改成 `ResizeToContents`（按内容给够，不会被挤），
            其余列仍然是 `Stretch` 平分 —— 这是**同一个策略的一处收口调整**，
            不是另起一套：宽窗口下两种模式看起来完全一样，只有窄窗口才看得出区别。
            `min_section` 是每列的下限，防止极窄时数字列被压到看不见（表格自己横向滚动）。
            """
            header = table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.Stretch)
            # 名称那一列按内容给宽（其余列 Stretch 平分剩余宽度）
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectRows)
            # 隔行浅底（主题里 `alternate-background-color` 靠这个开关才生效）：
            # 一行一只股票时，隔行底色比网格线更不容易看串行
            table.setAlternatingRowColors(True)

        def _build_tray(self) -> None:
            """托盘图标：关闭窗口只最小化，不退出。

            托盘是 16~32px 的场景，所以**专门取 32 那一档**（小尺寸是单独画的，
            拿 256 缩下去会糊）；`assets` 内部在缺这一档时会退回主图。
            两样都没有才退回系统标准图标 —— 托盘图标空着就没法右键退出了。
            """
            icon = _load_icon(32)
            if icon.isNull():
                icon = self.style().standardIcon(
                    self.style().StandardPixmap.SP_ComputerIcon
                )
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("老A选股助手")
            # 闪烁要交替两张图，得先记住"正常的那张"（见 `_start_alert_flash`）
            self._normal_tray_icon = icon
            menu = QMenu()
            act_show = QAction("显示主窗口", self)
            act_show.triggered.connect(self._restore_window)
            act_recent = QAction("最近提醒", self)
            act_recent.setToolTip("把最近几条提醒再弹一次（像 QQ 那样点开就能看）")
            act_recent.triggered.connect(self.on_show_recent_alerts)
            # 【开始选股】：与「策略选股」页那个按钮**同一个回调**（一处逻辑两处入口）
            act_pool = QAction(BTN_START_TEXT, self)
            act_pool.setToolTip("跑启用的策略与公式，结果直接进「自选股池」")
            act_pool.triggered.connect(self.on_run_pipeline)
            # 【暂停提醒】：盯盘时窗口多半收在托盘里，这个开关必须在托盘上够得着。
            # 用可勾选项（`setCheckable`）而不是"点了就切换文字"：勾选框一眼能看出当前状态
            act_pause = QAction(BTN_PAUSE_TEXT, self)
            act_pause.setCheckable(True)
            act_pause.setToolTip("暂停盘中提醒（选股照跑）；窗口收在托盘里时从这里开关最方便")
            act_pause.triggered.connect(self.on_toggle_intraday)
            self.act_pause = act_pause
            act_quit = QAction("退出", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_show)
            menu.addAction(act_recent)
            menu.addAction(act_pool)
            menu.addSeparator()
            menu.addAction(act_pause)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray_menu = menu
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()
            # 这里**不再**用托盘图标去覆盖窗口图标：托盘那份是 32px，
            # 拿去当窗口图标在任务栏/Alt-Tab 上会明显发虚（窗口图标在 __init__ 里设过 256 的）
            try:
                from laoa_trader.notify import tray as tray_mod

                tray_mod.bind(self.tray)  # 后台线程的消息由界面弹出
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"托盘绑定失败：{exc}")

        # ── 运行时自检（本地、秒级、不联网）──

        def run_preflight(self, auto: bool = False, force: bool = False) -> None:
            """启动时自检：本地数据够不够用？按三态分别处理。

            - `ready` → 状态栏打结论，**一次请求都不发**；
            - `needs_incremental` → 提示落后几天；`auto_download_on_start=true` 时后台自动补；
            - `needs_full` → 标题区挂一行提示（填 Key → 点【下载数据】；改版后**不再弹向导**）。

            Args:
                auto: 是否由启动定时器触发（仅用于日志语义）。
                force: 已经检查过时是否强制重查。默认**幂等**：重复调用直接返回，
                    避免"自动检查一次 + 手动再点一次"把增量跑两遍、或弹出两个向导。
            """
            from laoa_trader.data import preflight

            if not force and self.preflight_result is not None:
                return
            # 这条是"进度"性质的消息：自检出结论之后要撤掉，
            # 否则状态栏会在十分钟里一直挂着"正在检查本地数据…"（用户会以为卡住了）
            self._set_status("正在检查本地数据…", progress=True)
            QApplication.processEvents()
            try:
                result = preflight.check(self.cfg.db_path, self.cfg)
            except Exception as exc:  # noqa: BLE001 - 自检失败不该让界面起不来
                self._set_status(f"⚠️ 数据自检失败：{type(exc).__name__}: {exc}")
                return
            self.preflight_result = result
            status = result.get("status")
            if status != preflight.READY and \
                    preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT:
                # **只缺轻量项**：行业归属/日历/指数是目录类数据，点【刷新数据】几秒就好。
                # 这里直接后台补一次（不弹"下载历史数据"的向导）—— 用户实报过
                # 被这个提示引去重下 180MB。补完再复查一次，把结论写进状态栏。
                self._toast(f"⚠️ {result.get('reason')}")
                self._run_worker(
                    lambda note_cb=None, progress_cb=None: preflight.ensure_ready(
                        self.cfg, note_cb=note_cb, progress_cb=progress_cb,
                    ),
                    "补齐轻量数据",
                    with_progress=True, with_note=True,
                )
                return
            if status == preflight.READY:
                # 自检出结论了 → 撤掉"正在检查本地数据…"那条进度消息，
                # 让主状态显示 `✅ 数据就绪 · 5560 只 · 更新到 09-11`。
                # 也**不再**把 summary_line（"…10,283,203 行 / 最新 …"）塞进状态栏 ——
                # 行数与阈值在【详情】里（用户反馈"很多词看不懂"指的就是这种）。
                self._clear_status_message()
                self._refresh_status()
                return
            if status == preflight.NEEDS_INCREMENTAL:
                self._refresh_status()
                if self.cfg.auto_download_on_start:
                    self._toast(f"⚠️ {preflight.summary_line(result)}；正在后台增量更新…")
                    self.on_refresh_data()
                else:
                    self._toast(f"⚠️ {preflight.summary_line(result)}；"
                                "点【刷新数据】可立即增量更新")
                return
            # needs_full：弹首次向导。状态栏**不贴那句原始原因**（"行情表是空的…"），
            # 而是给一句"点哪个按钮"的话（`_data_problem_text`）；原因在向导窗口与详情里 ——
            # 用户要的是"我该做什么"，不是"内部为什么"（原话：很多词看不懂）
            self._clear_status_message()
            self._refresh_status()
            self.show_first_run_wizard(result)

        def show_first_run_wizard(self, result: dict) -> None:
            """数据不够用时**只写一行提示**（原来是弹一个模态向导，用户要求改成提示）。

            为什么取消那个窗口（用户拍板）：它问的是"填 Key / 选目录 / 开始下载"，
            而这三件事在「系统设置」里都有（Key 输入框、【下载数据】按钮）——
            再弹一个窗口只是把同一件事说第二遍，还会挡住主界面。
            现在标题区常年挂着这一行（数据下好之前一直在），用户随时看得到"该做什么"，
            也不会被拦在外面。

            `self.first_run_hint` 是**单行**控件（`ElidedLabel`）：太长的原因会被省略号截断，
            全文仍在 tooltip 与【显示详情】里。
            """
            if self.first_run_hint is None:
                return
            self.first_run_hint.setFullText(self._first_run_hint_text(result))
            self.first_run_hint.setVisible(True)

        def _first_run_hint_text(self, result: dict) -> str:
            """「本地还没有行情数据 · 在【系统设置】里点【下载数据】」这一句。

            指路必须指到**真的按钮**上（按钮文字取自 `BTN_*` 常量，与界面上的一字不差）：
            改版后【下载数据】搬进了「系统设置」，提示里就得写"在【系统设置】里"，
            否则用户会满窗找一个不存在的按钮（这正是 `hints.py` 存在的理由）。
            """
            from laoa_trader.data import preflight

            result = result or {}
            reason = str(result.get("reason") or "").strip()
            if result.get("status") == preflight.NEEDS_INCREMENTAL:
                days = int(result.get("stale_trading_days") or 0)
                return (f"本地数据落后 {days} 个交易日 · 在【{TAB_SETTINGS}】里点"
                        f"【{BTN_REFRESH_TEXT}】补齐")
            if preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT:
                # 只缺轻量项（交易日历/行业归属/指数）：指路"刷新"而不是"重下历史"，
                # 否则用户会白等十几分钟重下 180MB（实报过的 bug）
                return (f"本地数据还差 {hints.LIGHT_ITEM_NAMES} · 在【{TAB_SETTINGS}】里点"
                        f"【{BTN_REFRESH_TEXT}】补齐（几秒就好）")
            tail = f"（{reason}）" if reason else ""
            return (f"本地还没有行情数据{tail} · 在【{TAB_SETTINGS}】里填好 API Key，"
                    f"再点【{BTN_DOWNLOAD_TEXT}】下载（可中断，下次接着传）")

        def hide_first_run_hint(self) -> None:
            """数据就绪之后把那一行提示收掉（它说的是一件**已经不存在**的事）。"""
            if self.first_run_hint is not None:
                self.first_run_hint.setFullText("")
                self.first_run_hint.setVisible(False)

        def _on_download_done(self, result: Any) -> None:
            """下载完成（成功/取消）后：收掉"数据不足"提示 + 数据够了就自动跑一次选股。

            下载过程中，标题区那一行提示（`first_run_hint`）一直说着"还没有行情数据"；
            下完之后它就该消失（否则用户会看着一句过期的话以为没下成功）。
            """
            if getattr(result, "ok", False):
                # 数据变了 → 之前缓存的"needs_full"结论立刻作废，重算一次。
                # 这里**不**走 run_preflight()：那会在"还是不够用"时再摆一次提示，
                # 用户刚下完就被一堆提示怼一脸不合适；只把结论写进运行状态。
                # 轻量项（行业归属/日历/指数）缺了就**先自动补一次**再复查：
                # 刚下完几年行情的用户，不该因为少一个行业归属被告知"数据仍不可用"
                gate = data_gate(self.cfg, self.engine, auto_sync_light=True)
                self.preflight_result = gate.get("result") or None
                self._set_status(
                    ("✅ " if gate["ok"] else "⚠️ ") + gate["message"]
                )
                if not gate["ok"]:
                    # 下完了还不够（例如跨度/主体缺失）：说清原因，别硬跑
                    self._toast(f"⚠️ 数据仍不可用：{gate['reason']}")
                    # 提示行留着（数据确实还不能用），但要说清**还差什么**
                    self.show_first_run_wizard(
                        gate.get("result") or {"reason": gate.get("reason") or ""}
                    )
                    return
                self.hide_first_run_hint()
                self._toast(f"下载完成，正在跑一次【{BTN_START_TEXT}】…")
                self.on_run_pipeline()

        def _start_scheduler(self) -> None:
            try:
                self.scheduler.start()
            except Exception as exc:  # noqa: BLE001 - 调度起不来也要能用界面
                self._set_status(f"⚠️ 调度器启动失败：{exc}")

        # ── 状态栏与刷新 ──

        def _set_status(self, text: str, *, progress: bool = False) -> None:
            """显示一条**瞬时消息**（任务完成/失败、刚点过的动作），见 `_compose_status`。

            为什么要分两层：定时器每 5 秒重写一次主状态；如果消息和实时状态共用同一个
            Label，任务完成/失败的提示会在零点几秒内被刷掉 —— 用户根本看不到。

            Args:
                progress: 这条消息是不是"进度回传"（`_on_progress`）。下载进行中时
                    进度回传会让位给"⏬ 正在下载历史数据 48%"那一行（否则屏幕上会挂着
                    一条**不会动**的旧进度，看起来像卡死）；而"重签 URL 继续下载"
                    这类真正的提示仍然优先显示。
            """
            self._message = text
            self._message_at = time.monotonic()
            self._message_is_progress = progress
            self.status_label.setText(self._compose_status())

        def _clear_status_message(self) -> None:
            """撤掉当前那条瞬时消息（例：自检已经出结论，"正在检查…"就该撤掉）。

            为什么要显式撤：瞬时消息的优先级最高、能存活 10 分钟 ——
            一条"正在做某事"的消息在事情做完之后还挂着，用户只会以为卡住了。
            """
            self._message = ""
            self._message_at = 0.0
            self._message_is_progress = False

        def _heavy_refresh_allowed(self) -> bool:
            """现在允许做"重"刷新吗（查库 + 重建表格）？

            **下载/导入期间一律不允许**：导入线程正在同一张表上大批量写入，
            而这几块每 5 秒要跑 11 条 COUNT/聚合（其中两条是全表）、还要按行查最新价
            （N+1）。这会儿界面被按住几百毫秒到几秒，用户看到的就是"卡死"（实报）。
            进度条与状态文案不靠这个（进度有回调在推），所以"不刷"期间界面并不瞎。
            """
            return not state.is_downloading()

        def _tick(self) -> None:
            """每 5 秒刷新一次（全部包在 try 里：界面刷新绝不崩）。

            下载/导入期间**只刷"轻"的部分**（运行状态 + 托盘），
            重活（数据概况、两张表）留到下载结束后一次性补齐 —— 见 `_heavy_refresh_allowed`。

            实时快照（`self.quotes.tick()`）**不管轻重都问一次**：它自己会判断
            "是否在交易时段 / 是否在下载 / 有没有要盯的票"（见 `QuoteService.should_request`），
            而且它只是**发起**一轮后台取数，不会把主线程按住。
            """
            try:
                heavy = self._heavy_refresh_allowed()
                # 「上一拍因为下载被跳过了，这一拍能刷了」→ 强制取一次新的数据概况，
                # 把下载/导入写进去的行数与只数立刻补上（其余时候由 30 秒 TTL 管）
                self._refresh_status(force_summary=heavy and self._heavy_paused)
                if heavy:
                    self._refresh_pool_table()
                    self._refresh_positions()
                    if self._heavy_paused:
                        # 下载刚结束 → 完整刷一遍已经算过了（上面那些调用），这里只落标记
                        self._heavy_paused = False
                else:
                    # 记住"这次跳过了"：下载一结束的下一拍要把表格补上（不能一直空着）
                    self._heavy_paused = True
                # 概览**不在这里刷**：它有自己的 60 秒定时器与 55 秒 TTL
                # （见 `_market_tick`）—— 5 秒一轮会把配额刷掉
                # 实时快照同理：这里只是"拍一下"，真正取不取由 `QuoteService` 自己判
                self.quotes.tick()
                # 新提醒的对账（响声 / 闪图标 / 浮窗）**不管轻重都做**：
                # 它只读最近 20 行，而且提醒不能因为"正在下载"就不响
                self._check_new_alerts()
                self._drain_tray()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"界面刷新异常：{exc}")

        def _refresh_status(self, *, force_summary: bool = False) -> None:
            """刷新运行状态那一句 + tooltip 里的详情。

            两处要用同一批事实（池子几只、持仓多少），所以**只查一次库**再分发 ——
            5 秒一轮里查两遍同样的 SQL 是白费力气。
            数据概况走 TTL 缓存（下载期间直接用上一次的值），见 `_summary_cached`。
            """
            facts = self._status_facts(force_summary=force_summary)
            self.status_label.setText(self._compose_status(facts))
            details = self._status_details(facts)
            self.status_label.setToolTip(details)
            self.status_details_cache = details      # 【详情】弹窗打开时直接用这一份

        def _invalidate_summary(self) -> None:
            """让"数据概况"缓存作废（数据刚被改过 → 下一次刷新取新值）。

            为什么必须显式作废：概况有 30 秒 TTL（为了下载/导入时不卡），
            但用户**刚**加了自选/持仓、或刚跑完一次同步时，状态栏上的数字就该立刻跟上 ——
            不然那 30 秒里显示的是旧数，看起来像"点了没生效"。
            """
            self._summary = None
            self._summary_at = 0.0

        def _summary_cached(self, *, force: bool = False) -> dict:
            """取"数据概况"（**带 TTL 缓存**；下载期间一律用上一次的值）。

            为什么不直接在每次刷新时 `engine.summary()`：它里面有全表 COUNT
            （行情表几百万行），而状态区每 5 秒刷一次 —— 那等于让界面陪着导入线程
            一起按住数据库（用户实报"下载时界面卡死"）。
            缓存放在**界面层**而不是 `storage`：CLI/调度器要的是实时值，
            界面要的只是"别每 5 秒把整张表数一遍"，这是展示需求不是数据需求。
            """
            downloading = state.is_downloading()
            now = time.monotonic()
            if self._summary is not None:
                # **下载优先**：下载/导入期间一律用上一次的值，`force` 也不行 ——
                # 导入线程正占着同一张表，这会儿去数几百万行就是把界面按住（实报的"卡死"）
                if downloading:
                    return self._summary
                if not force and now - self._summary_at < SUMMARY_TTL:
                    return self._summary
            try:
                value = self.engine.summary()
            except Exception as exc:  # noqa: BLE001 - 取不到就用上一次（宁旧勿崩）
                logger.debug(f"取数据概况失败：{exc}")
                value = self._summary or {}
            self._summary = value
            self._summary_at = now
            return value

        def _status_facts(self, *, force_summary: bool = False) -> dict:
            """状态区一次刷新要用的全部事实（`_compose_status` / 标签 / 详情共用）。"""
            try:
                st = self.scheduler.status()
            except Exception as exc:  # noqa: BLE001 - 状态区绝不能因为取状态而崩
                logger.debug(f"取调度状态失败：{exc}")
                st = {}
            summary = self._summary_cached(force=force_summary)
            try:
                pool_count = len(pool.load_pool(self.cfg.db_path))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取池子只数失败：{exc}")
                pool_count = 0
            return {
                "st": st,
                "summary": summary,
                "pool_count": pool_count,
                "pnl": self._position_pnl(),
            }

        def _compose_status(self, facts: dict | None = None) -> str:
            """运行状态那一句：**只讲一件事**，按优先级取。

            优先级（用户给定的口径）：
            1. 瞬时消息（任务完成/失败、刚点过的动作，10 分钟内）；
            2. 正在下载 → `⏬ 正在下载历史数据 48%（写入行情 62/285 万行）`；
            3. 本地数据落后或不可用 → `⚠️ … · 在【系统设置】里点【下载数据】`；
            4. 一切正常 → `✅ 数据就绪 · 更新到 09-11 · 5560 只`。

            最后再补一句"盘中提醒"的状态（时段中 / 已暂停）—— 它是**进行时**的事实，
            不属于上面任何一档；不在时段时**什么都不补**（见 `INTRADAY_OUT_SESSION`）。
            """
            facts = facts if facts is not None else self._status_facts()
            st = facts["st"]
            fresh = bool(self._message) and (
                time.monotonic() - self._message_at < STATUS_MESSAGE_TTL
            )
            message = self._message if fresh else ""
            downloading = bool(st.get("downloading"))
            # 下载中且手头只有"进度回传"这类消息 → 用一行会动的下载进度；
            # 其它消息（任务完成/失败、重签 URL 的提示）仍然优先，不被吞掉
            if downloading and (not message or self._message_is_progress):
                return self._with_intraday(self._download_line(st), st)
            if message:
                return self._with_intraday(message, st)
            problem = self._data_problem_text()
            if problem:
                return self._with_intraday(problem, st)
            return self._with_intraday(self._ready_text(facts), st)

        def _with_intraday(self, text: str, st: dict) -> str:
            """把"盘中提醒时段中 / 已暂停"补在运行状态后面（没在时段就不补）。"""
            note = self._intraday_text(st)
            return f"{text} · {note}" if note else text

        def _download_line(self, st: dict) -> str:
            """下载中的运行状态：`⏬ 正在下载历史数据 48% · 写入行情 62/285 万行`。

            为什么必须显式"正在下载"四个字 + 百分比 + 阶段：整件事可能十几分钟，
            屏幕上只挂一个百分比（或者干脆什么都不显示），用户会以为程序卡死了（实测反馈）。
            """
            value, maximum = self.progress.value(), self.progress.maximum()
            if maximum > 0:
                head = f"⏬ 正在下载历史数据 {value * 100 // maximum}%"
            else:
                head = "⏬ 正在下载历史数据…"
            if self._progress_stage_text:
                return f"{head}（{self._progress_stage_text}）"
            return head

        def _data_problem_text(self) -> str:
            """数据不可用 / 落后时那一句："点哪个按钮能解决"；一切正常返回空串。

            为什么必须指路、而且要点名**在哪个页**：用户看不懂"needs_full"这种内部说法，
            也不该去猜；改版后【下载数据】【刷新数据】搬进了「系统设置」，
            不写"在【系统设置】里"就等于让用户满窗找一个不存在的按钮
            （这正是 `hints.py` 存在的理由）。
            """
            from laoa_trader.data import preflight

            result = self.preflight_result
            if not result:
                return ""      # 还没自检过：不瞎报，交给"数据就绪"那句
            status = result.get("status")
            if status == preflight.READY:
                return ""
            # 先看"该做什么"（needs_download），再看"有多严重"（status）：
            # 只缺轻量项（交易日历/行业归属/指数）时点【刷新数据】几秒就好，
            # 指路去"下载数据"会让人白等十几分钟重下 180MB（实报 bug）
            if preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT:
                return (f"⚠️ {result.get('reason') or hints.SYNC_LIGHT_HINT}"
                        f" · 在【{TAB_SETTINGS}】里点【{BTN_REFRESH_TEXT}】")
            if status == preflight.NEEDS_INCREMENTAL:
                days = int(result.get("stale_trading_days") or 0)
                return (f"⚠️ 本地数据落后 {days} 个交易日 · 在【{TAB_SETTINGS}】里点"
                        f"【{BTN_DOWNLOAD_TEXT}】补齐")
            return (f"⚠️ 本地数据还没准备好 · 在【{TAB_SETTINGS}】里点"
                    f"【{BTN_DOWNLOAD_TEXT}】下载历史数据")

        def _ready_text(self, facts: dict) -> str:
            """正常状态：`✅ 数据就绪 · 更新到 09-11 · 5560 只`。

            先说"更新到哪天"再说"多少只"：看盘的人先关心"数据新不新"，
            股数是核对用的（顺序与用户给的示例一致）。
            """
            summary = facts["summary"]
            latest = str(summary.get("latest_date") or "")
            symbols = int(summary.get("symbols") or 0)
            if latest:
                # 只要"月-日"：运行状态不是对账的地方，年份在同一天里没有信息量
                return f"✅ 数据就绪 · 更新到 {latest[5:]} · {symbols} 只"
            return "✅ 数据就绪"

        def _intraday_text(self, st: dict) -> str:
            """盘中提醒的三种说法（`时段中` / `已暂停` / 空串=不在时段就不提）。"""
            if st.get("intraday_paused"):
                return INTRADAY_PAUSED
            return INTRADAY_IN_SESSION if st.get("in_session") else INTRADAY_OUT_SESSION


        def _auction_detail_lines(self) -> list[str]:
            """状态详情里的**竞价扫描结果**（全市场扫描的**全部命中**，按分排序）。

            为什么要单独存一张表（`auction_scan`）而不是只靠推送文本：
            推送只列前 N 只，而用户要能"看全部竞价" —— 详情弹窗就是那个地方
            （可滚动、可复制）。每行给**原始数值 + 板块 + 分**（只给一个分没法核对），
            ★ = 已经在推送里发过的那几只，`（池内）` = 这只同时在自己的池子/自选/持仓里。
            """
            from laoa_trader.data import storage as storage_mod

            try:
                with storage_mod.connect(self.cfg.db_path) as conn:
                    rows = storage_mod.load_auction_scan(conn)
            except Exception as exc:  # noqa: BLE001 - 读不到就不显示这一节
                logger.debug(f"读竞价扫描结果失败：{exc}")
                return []
            if not rows:
                return ["竞价扫描：还没有结果（默认 09:20 / 09:25 各扫一次全市场；"
                        "开关与阈值在设置页「竞价扫描」那一组）"]
            slot = str(rows[0].get("slot") or "")
            day = str(rows[0].get("day") or "")
            total = int(rows[0].get("total") or len(rows))
            top = int(sum(1 for row in rows if row.get("pushed")))
            in_pool = set(self._pool_symbols())
            shown = f"，这里列前 {len(rows)} 只" if total > len(rows) else ""
            lines = [f"竞价扫描（{day} {slot}）共命中 {total} 只{shown}，"
                     f"★ = 已推送前 {top} 只"]
            for row in rows:
                mark = "★" if row.get("pushed") else "·"
                inside = "（池内）" if str(row.get("symbol")) in in_pool else ""
                # 直接复用汇总推送那一行的写法（名称(代码) + 板块 + 数值 + 分），
                # 只在行尾补一个「（池内）」—— 自己再拼一遍格式，迟早和推送里的不一致
                lines.append(f"  {mark} {intraday.auction_scan_line(row)}{inside}")
            return lines

        def _pool_symbols(self) -> list[str]:
            """当前盯的代码（池子 + 自选）：竞价结果里标"池内"用。

            取不到就返回空列表（这只是锦上添花，绝不能因为它让详情打不开）。
            """
            try:
                return list(intraday.watch_targets(self.cfg.db_path)[0])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取池子代码失败（竞价结果不标池内）：{exc}")
                return []

        def _status_details(self, facts: dict | None = None) -> str:
            """状态详情：多行中文说明，tooltip 与【详情】弹窗**共用这一份**。

            这里才是"查得到就行"的东西：行数、股票数、持仓浮动、后台任务状态、
            日志路径、自检阈值 —— 用户想核对/报障时用得上，平时不占地方。
            """
            from laoa_trader.data import preflight

            facts = facts if facts is not None else self._status_facts()
            st, summary, pnl = facts["st"], facts["summary"], facts["pnl"]
            latest = summary.get("latest_date") or "无"
            # 数据目录可能是 str（直接构造 Config 时很常见），统一成 Path 再拼
            log_path = log_file_path() or (
                Path(self.cfg.data_dir) / "logs" / "laoa-trader.log"
            )
            self_check = self.preflight_result or {}
            intraday_state = self._intraday_text(st) or "不在交易时段"
            lines = [
                "状态详情",
                "────────────",
                # 版本/版权只是标题栏里不再挂了，**不是没了**：报障时要贴的就是这几行
                # （【关于软件】里那份是同一份文案，见 `about_lines`）
                f"{APP_NAME} v{laoa_trader.__version__}（测试版）",
                f"后台任务：{'运行中' if st.get('running') else '未运行'}",
                f"最新数据日期：{latest}",
                f"本地股票数：{summary.get('symbols', 0)} 只",
                f"本地行情行数：{_short_number(summary.get('daily_rows'))} 行",
                f"今日池子：{facts['pool_count']} 只"
                f"（含自选 {summary.get('watchlist', 0)} 只）",
                f"持仓浮动：{self._floating_pnl(pnl)}",
                f"盘中提醒：{intraday_state}",
                # 定时运行已按用户要求整组移除，这里不再有"主跑/补跑/下次自动运行"
                # （状态栏里那句"今天该自动跑却没跑"也随之删除：它描述的事已经不存在了）
                f"数据自检：{self_check.get('status') or '尚未自检'}"
                + (f"（落后 {self_check.get('stale_trading_days')} 个交易日）"
                   if self_check.get("status") == preflight.NEEDS_INCREMENTAL else "")
                # 数据不可用时把**原始原因**留在这里（运行状态只给"点哪个按钮"）
                + (f" · {self_check.get('reason')}"
                   if self_check.get("status") == preflight.NEEDS_FULL
                   and self_check.get("reason") else ""),
                f"自检阈值：历史 ≥{self.cfg.min_history_years:g} 年 / "
                f"股票 ≥{self.cfg.min_symbols} 只 / 行业覆盖 ≥90% / 交易日历齐全",
                *self._auction_detail_lines(),
                f"数据目录：{self.cfg.data_dir}",
                f"日志文件：{log_path}",
            ]
            if st.get("last_error"):
                lines.append(f"上次出错：{st['last_error']}")
            return "\n".join(lines)

        def on_show_status_details(self) -> None:
            """【详情】：弹出状态详情（可复制），把"看不懂的词"变成查得到的东西。

            非模态（`show` 而不是 `exec`）：用户可以先看别的，也不会卡住自动化测试。
            """
            if self.status_dialog is None:
                dialog = QDialog(self)
                dialog.setWindowTitle("状态详情")
                # 尺寸跟着主窗口走（别在小屏上开出一个比主窗口还大的对话框）
                dialog.resize(min(560, max(360, self.width() - 80)),
                              min(420, max(260, self.height() - 160)))
                layout = QVBoxLayout(dialog)
                box = QPlainTextEdit()
                box.setReadOnly(True)          # 只读但**可选中复制**（报障时整段贴过来）
                box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                self.status_details_text = box
                layout.addWidget(box, 1)
                row = QHBoxLayout()
                self.btn_copy_details = QPushButton("复制详情")
                self.btn_copy_details.setToolTip("把这段文字复制到剪贴板")
                self.btn_copy_details.clicked.connect(self.on_copy_status_details)
                row.addWidget(self.btn_copy_details)
                row.addStretch(1)
                self.btn_close_details = QPushButton("关闭")
                self.btn_close_details.clicked.connect(dialog.close)
                row.addWidget(self.btn_close_details)
                layout.addLayout(row)
                self.status_dialog = dialog
            details = self._status_details()
            self.status_details_cache = details
            self.status_details_text.setPlainText(details)
            self.status_dialog.show()
            self.status_dialog.raise_()

        def on_copy_status_details(self) -> None:
            """把状态详情写进剪贴板（与弹窗里显示的完全是同一份文本）。"""
            text = self.status_details_text.toPlainText() or self._status_details()
            QApplication.clipboard().setText(text)
            self._set_status("已复制状态详情")

        def _position_pnl(self) -> tuple[str, float]:
            """持仓浮动 `(状态, 涨跌比例%)`；状态是中文短句：ok / 无持仓 / 无最新价 / 未知。

            为什么返回中文状态而不是枚举：调用方（状态栏、详情）拿到的就是能显示的东西，
            界面层不用再各自判一遍（也就不会出现"两处对同一件事说法不同"）。

            为什么**只给比例、不给金额**：用户明确要求"不要记录盈亏金额，只记录盈亏比例" ——
            "这一手赚了几个点"才是决策信息，金额只跟仓位大小有关（同样的 7% ，
            一万块和一百万块的绝对数完全不同），还要多一处口径去核对。
            内部为了算**加权**比例仍然要过一遍市值与成本，但金额不出这个方法。

            **口径**：现价与「持仓监控」表里那一列**完全同一个价**（实时快照优先，
            没有就本地不复权收盘价）—— 老代码这里读 `stock_daily_hfq`（后复权），
            与真实成本不是一套口径，是这次要修掉的 bug。
            """
            from laoa_trader.data import storage

            try:
                with self.engine.connect() as conn:
                    positions = storage.load_positions(conn)
                if not positions:
                    return "无持仓", 0.0
                symbols = list(positions)
                # **同一套现价口径**：实时快照优先，没有就用本地**不复权**收盘价
                # （老代码在这里读的是 `stock_daily_hfq`：后复权价对着真实成本算出来的
                #  比例，在送股/分红之后会离谱地偏大 —— 这次统一到 `_price_cells` 那一份）
                local = self._local_closes(symbols)
                total_cost = 0.0
                total_value = 0.0
                for symbol, row in positions.items():
                    price = self.quotes.price(symbol) or (local.get(symbol) or {}).get("close")
                    cost = float(row.get("avg_cost") or 0)
                    if not price or not cost:
                        continue
                    # 数量只在"加权"里用一次（界面不显示、也不再要求用户填）：
                    # 没填数量的持仓按**等权 1 手**参与加权，避免 0 权重让它彻底不出现在浮动里。
                    # 为什么等权而不是排除它：用户只记了代码+成本时，"这只票赚了几个点"
                    # 仍然是有效信息，只是没法按仓位加权 —— 等权是这里能做到的最合理的近似。
                    qty = float(row.get("quantity") or 0) or 1.0
                    total_cost += qty * cost
                    total_value += qty * float(price)
                if not total_cost:
                    return "无最新价", 0.0
                # 加权比例：按**成本**加权（每只股票的盈亏点数按投入摊平），
                # 不是把各只的比例简单平均 —— 后者会被小仓位的高波动带偏
                return "ok", (total_value - total_cost) / total_cost * 100
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"持仓浮动计算失败：{exc}")
                return "未知", 0.0

        def _floating_pnl(self, values: tuple[str, float] | None = None) -> str:
            """持仓浮动的完整说法（**详情**用）：`+1.20%` / `无持仓` / `无最新价` / `—`。

            **只有比例、没有金额**：用户要求持仓不记盈亏金额（`元`这个字在这里不该出现）。
            """
            state, pct = values if values is not None else self._position_pnl()
            if state != "ok":
                return "—" if state == "未知" else state
            return f"{pct:+.2f}%"

        # ── 实时行情快照（两张表的「现价 / 涨幅」）──

        def _quote_symbols(self) -> list[str]:
            """现在要盯的代码：池子 + 自选 + 持仓（实时快照只取这些）。

            为什么**不**用 `intraday.watch_targets()`：那个函数会解析策略组、查近期信号，
            是"盘中盯盘"那一轮的完整口径；而这里每 5 秒被问一次（限流判断用），
            只需要"有哪些代码"，三条轻查询就够了。

            取不到就返回空列表 —— 空列表 = 一次请求都不发（见 `QuoteService.should_request`），
            这正是我们要的降级：库还没建好时不该去外呼。
            """
            from laoa_trader.data import storage

            out: list[str] = []
            try:
                out.extend(pool.pool_symbols(self.cfg.db_path))
            except Exception as exc:  # noqa: BLE001 - 取不到就不取实时价（表格有本地兜底）
                logger.debug(f"取池内代码失败（本轮不取实时价）：{exc}")
            try:
                with storage.connect(self.cfg.db_path) as conn:
                    out.extend(storage.watchlist_symbols(conn, enabled_only=True))
                    out.extend(storage.load_positions(conn).keys())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取自选/持仓代码失败：{exc}")
            return [str(s) for s in dict.fromkeys(out) if s]

        def _on_quotes_updated(self) -> None:
            """实时快照回来了（主线程）：立刻重画两张表，不等下一个 5 秒拍子。"""
            try:
                self._refresh_pool_table()
                self._refresh_positions()
            except Exception as exc:  # noqa: BLE001 - 重画失败不该影响后续刷新
                logger.debug(f"快照到达后刷新表格失败：{exc}")

        def _local_closes(self, symbols: list[str]) -> dict[str, dict]:
            """本地最近两根**不复权**日线收盘（实时快照缺失时的兜底，见 `storage`）。

            为什么是"不复权"：现价这一列要与用户手上的**真实成本**直接比 ——
            后复权价在送股/分红之后会整体缩放（10 送 10 就是两倍），
            拿它算「盈亏比例」正是这次要修掉的 bug（见 `docs/改版方案.md` 第五节）。

            **只查没有实时快照的那些票**：交易时段里绝大多数票都有实时价，
            为它们去查本地日线（每只票一次索引范围扫描）纯属白做 ——
            留着这个兜底只是为了收盘后 / 断网 / 没配 Key 时那一列还有数可显示。
            """
            from laoa_trader.data import storage

            wanted = [s for s in dict.fromkeys(symbols) if s and not self.quotes.quote(s)]
            if not wanted:
                return {}
            try:
                with storage.connect(self.cfg.db_path) as conn:
                    return storage.latest_raw_closes(conn, wanted)
            except Exception as exc:  # noqa: BLE001 - 查不到就是"没有兜底价"，画 `—`
                logger.debug(f"取本地收盘价失败：{exc}")
                return {}

        def _price_cells(self, symbol: str, local: dict[str, dict]) -> tuple[Any, Any, float | None]:
            """「现价」「涨幅」两个单元格 + 给「盈亏比例」用的那个价格。

            口径（用户给定的优先级）：
            1. **有实时快照**（60 秒一取，且只在本交易时段取）→ 现价 / 涨幅都取它，
               tooltip 写明取数时刻；
            2. 否则用**本地最新收盘价**，tooltip 里写清"这是本地最新收盘价（MM-DD），
               **不是实时价**"；
            3. 两者都没有 → `—`（不画 0.00）。

            2026-09-17（用户要求）：**去掉 `*` 号**。原来本地收盘价会写成 `12.34*`，
            本意是"这不是实时价"，但用户把它当成了**监控状态标记**（以为带 `*` 表示
            这只票没在监控）。现在单元格里**不加任何符号**，改由 tooltip 说清
            （"本地最新收盘价（09-11），不是实时价"）—— 信息一点没少，
            只是不再用一个会被误读的符号表达。

            **返回的价格与「现价」列是同一个数**：调用方（「盈亏比例」）必须用它，
            不许自己再查一次价 —— 两列显示两个价，是用户最难理解的那种不一致。
            """
            quote = self.quotes.quote(symbol)
            pct: float | None = None
            if quote:
                price = float(quote["price"])
                raw_pct = quote.get("pct")
                pct = float(raw_pct) if raw_pct is not None else None
                price_item = QTableWidgetItem(f"{price:.2f}")
                # 取数时刻按**北京时间**写（`snapshot_time_text` 与 `intraday` 同一口径）：
                # 用 `time.localtime` 在 UTC 的机器上会把 13:46 显示成 05:46
                at_text = quotes_mod.snapshot_time_text(quote.get("at"))
                price_item.setToolTip(
                    f"实时快照 {price:.2f}" + (f"（{at_text}）" if at_text else "")
                )
                pct_item = QTableWidgetItem(f"{pct:+.2f}%" if pct is not None else market.DASH)
            else:
                bar = local.get(symbol) or {}
                close = bar.get("close")
                if close:
                    price = float(close)
                    prev = bar.get("prev_close")
                    pct = ((price - prev) / prev * 100) if prev else None
                    # **不带 `*`**（用户要求）：本地价与实时价长得一样，
                    # 区别写在 tooltip 里 —— 符号容易被误读成"监控状态"，
                    # 而且这两年表格里最容易被问的就是"这个星号什么意思"。
                    price_item = QTableWidgetItem(f"{price:.2f}")
                    tip = (f"这是本地最新收盘价（{_month_day(bar.get('date'))}），"
                           "**不是实时价**\n"
                           f"（{price:.2f}，不复权 —— 与实时价同一口径，才能和成本价直接比）")
                    price_item.setToolTip(tip)
                    pct_item = QTableWidgetItem(
                        f"{pct:+.2f}%" if pct is not None else market.DASH
                    )
                    pct_item.setToolTip(tip)
                else:
                    price = None
                    price_item = QTableWidgetItem(market.DASH)
                    price_item.setToolTip("本地还没有这只票的行情数据（在「系统设置」里下载）")
                    pct_item = QTableWidgetItem(market.DASH)
                    pct_item.setToolTip(price_item.toolTip())
            if pct is not None:
                color = market.value_color(pct)
                if color:
                    # 用前景色而不是富文本：与表格其余部分同一套画法，排序/复制都不受影响
                    pct_item.setForeground(QBrush(QColor(color)))
            return price_item, pct_item, price

        def _snapshot_cells(self, symbol: str) -> tuple[Any, Any]:
            """「市值」「换手」两个单元格（**取不到一律 `—`，绝不显示 0**）。

            口径（用户给定）：
            - 市值 = **流通市值**，单位**亿**，取快照里的 `circ_mktcap`；
            - 换手 = **实时换手率 %**，取快照里的 `turnover_rate`；
            - 快照没有 / 这两个字段没取到 → `—`：0 亿市值、0% 换手都是**真实存在的值**，
              拿 0 冒充"没取到"，用户会把"数据缺了"读成"这只票没人交易"。

            2026-09-18（用户要求）：tooltip **不再写出处**（原来会写"·来源 公开行情接口
            （腾讯/新浪）"这类标注），表头那段"为什么可能是别的来源给的"说明也去掉了 ——
            用户原话"把数据来源说明去掉，不需要"。所以这里只说**数值与时刻**，
            以及"为什么是 `—`"这半句（那半句有必要：不写清，用户会以为程序算错了，
            而不是"现在没有实时快照"）。

            ⚠️ 数据层仍然记着"哪个字段是谁给的"（`sources.supplement_map` 的
            `field_source`，见那里的说明）—— 那是排查用的，**不要再往界面上加回来**。
            """
            quote = self.quotes.quote(symbol) or {}
            at_text = quotes_mod.snapshot_time_text(quote.get("at"))
            when = f"（快照 {at_text}）" if at_text else ""
            missing = "现在没有这只票的实时快照，这一项显示 —（不是 0）"
            cap = quote.get("circ_mktcap")
            if cap is None:
                cap_item = QTableWidgetItem(market.DASH)
                cap_item.setToolTip("流通市值：" + missing)
            else:
                cap_item = QTableWidgetItem(f"{float(cap):.2f}亿")
                cap_item.setToolTip(
                    f"流通市值 {float(cap):,.2f} 亿{when}"
                    "\n（单位亿；取的是快照里的流通市值，不是总市值）"
                )
            turn = quote.get("turnover_rate")
            if turn is None:
                turn_item = QTableWidgetItem(market.DASH)
                turn_item.setToolTip("换手率：" + missing)
            else:
                turn_item = QTableWidgetItem(f"{float(turn):.2f}%")
                turn_item.setToolTip(f"实时换手率 {float(turn):.2f}%{when}")
            for item in (cap_item, turn_item):
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            return cap_item, turn_item

        def _monitor_cell(
            self, symbol: str, on: bool, alert: Any, *, togglable: bool = True,
            why: str = "",
        ) -> Any:
            """「监控开关」那一格：文字是 `开启` / `关闭`，tooltip 里带**今日最新一条提醒**。

            2026-09-17（用户要求）：原来的「提醒」列改成「监控开关」——
            - 单元格文字 = 监控状态（`开启` / `关闭`），**点一下就能切换**
              （见 `_on_table_cell_clicked`；右键菜单里那两项保持同步，两处调的是同一个方法）；
            - **原来的提醒内容一点没丢**：短标签 + 那一整句（含时间）都进 tooltip
              （`intraday.alert_cell_text` / `alert_cell_tooltip`），
              鼠标停在格子上就能看到 —— 列只有一格宽，写不下整句话。
            """
            item = QTableWidgetItem(MONITOR_ON_TEXT if on else MONITOR_OFF_TEXT)
            item.setData(Qt.ItemDataRole.UserRole, symbol)
            label = intraday.alert_cell_text(alert)
            bits = [
                f"监控：{'已开启' if on else '已关闭'}"
                + ("（点这一格就能切换）" if togglable else "（这一行不能在这里切换）"),
            ]
            if why:
                bits.append(why)
            if label:
                bits.append(f"今日最新一条提醒：{label}")
            # 整句话（含时间）**一律**用 `intraday.alert_cell_tooltip`：没有提醒时它给的是
            # "今天还没有这只票的盘中提醒" —— 自己再写一句近义的话，两处说法迟早不一致
            bits.append(intraday.alert_cell_tooltip(alert))
            # 整行的 tooltip（备注 / 来源明细 / 涨停原因 / 竞价）由调用方用
            # `_attach_row_tooltip` 并进来 —— 这里不自己拼，否则同一段会写两遍
            item.setToolTip("\n".join(bits))
            if not on:
                # 关了监控的那一格灰掉：与"名称变灰"同一套表达（一眼看出一行是停用的）
                item.setForeground(QBrush(QColor(Qt.GlobalColor.gray)))
            return item

        @staticmethod
        def _tag_item(text: str, symbol: str, tooltip: str = "") -> Any:
            """带 `symbol` 的单元格：右键菜单、单击跳转、悬浮备注都靠这个隐藏字段认行。

            为什么不靠"取单元格文本再解析代码"：`名称(代码)` 是**显示格式**，
            将来改一个字（换括号样式、加后缀）就会把跳转悄悄弄坏；
            `UserRole` 里的代码是结构化的，显示怎么变都不影响。
            """
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, symbol)
            if tooltip:
                item.setToolTip(tooltip)
            return item

        def _row_tooltip(self, row: dict, monitor_off: bool = False) -> str:
            """整行通用的 tooltip：**备注 + 监控状态 + 竞价那一行**。

            备注是用户自己写的（"龙头""消息面"），它是"为什么盯这只票"的答案；
            监控状态必须写进来，是因为表里**没有**「状态」列了（列数被用户定死成 6/8 列），
            而"这只票到底还在不在监控"是必须能看出来的事实；
            竞价那一行（`竞价 +3.2% 量比 2.8`）原来画在卡片上，卡片视图取消后
            它的去处就是这里 —— 竞价只在 9:15–9:25 有数据，为它单开一列等于
            95% 的时间空着。

            Args:
                monitor_off: 这只票的**持仓**被右键关掉了监控（`position.monitor = 0`）。
                    这时它虽然在池子里/是自选，也**不会产生任何盘中提醒** ——
                    必须以持仓上的开关为准（见 `intraday.monitor_off_symbols`），
                    而这件事在表里看不出来（表格只有 6 列），只能写在这儿。
            """
            note = str(row.get("note") or "").strip()
            source = str(row.get("source") or "")
            if "自选" in source:
                state = "监控中（盘中提醒会盯它）" if row.get("watchlist_enabled", True) \
                    else "已停用：不进池、不监控（右键可重新打开监控）"
            elif str(row.get("strategy") or ""):
                state = "监控中（策略选中的标的都在这张表里一起盯）"
            else:
                state = ""
            if monitor_off:
                state = ("已关闭监控（持仓上右键关的）：这只票不再产生任何盘中提醒"
                         "（止损/止盈/做T/竞价/异动），也不进观察面 —— "
                         "它在池子/自选里的身份不影响这条（持仓的开关优先）")
            auction = self._auction_text(str(row.get("symbol") or ""))
            # 今日涨停池的信息（连板数 + 涨停原因）：原来占「股票池」表的一列 + 卡片一行，
            # 而新表的列数被用户定死成 6 列 —— 它的去处是这一行的 tooltip
            # （"为什么涨停"是超短最有用的那句话，不能因为去掉一列就丢掉）
            limit_up = pool_mod.limit_up_text(row)
            # 「来源」列只写得下**主策略**（列数被用户定死成 6 列），所以这一行的来源明细
            # 全在 tooltip 里：哪条策略（`策略·短期反转`）、哪个组（含持有期）、
            # 同批还被哪些策略/公式选中、以及「（依赖开盘）」那段证据解释 ——
            # 全部由 `pool.source_detail_lines()` 给出（界面不自己拼一套，见那里的说明）。
            parts = [
                f"备注：{note}" if note else "",
                state,
                *pool_mod.source_detail_lines(row),
                f"涨停：{limit_up}" if limit_up else "",
                f"竞价：{auction[3:]}" if auction else "",
            ]
            return "\n".join(p for p in parts if p)

        def _attach_row_tooltip(self, item: Any, row_tip: str) -> None:
            """把"这一行"的 tooltip 并进单元格自己的 tooltip（两段都要保留）。

            为什么每一格都挂：用户悬浮的位置在哪都该看到那句备注 ——
            只有名称格带 tooltip 的话，鼠标停在「现价」上什么都不会发生，
            用户就会以为"这行没备注"。
            """
            if not row_tip:
                return
            existing = item.toolTip()
            item.setToolTip(f"{existing}\n{row_tip}" if existing else row_tip)

        # ── 「自选股池」页 ──

        def _refresh_pool(self) -> None:
            """刷新「自选股池」表（老名字保留：调度/测试里叫惯了，实现见 `_refresh_pool_table`）。"""
            self._refresh_pool_table()

        def _refresh_pool_table(self) -> None:
            """填「自选股池」：名称/现价/涨幅/市值/换手/板块/来源/监控开关。

            行从 `pool.pool_page_rows()` 来（池内行 + **不在池里的自选**，见那个函数的说明）。

            2026-09-17 改版（用户要求）：加「市值」「换手」两列（取自实时快照，
            取不到 `—`），「提醒」列改成「监控开关」（开启/关闭 + 点一下切换 +
            tooltip 里保留原来的提醒内容）。

            为什么还留着"内容指纹"（`_pool_signature`）：每 5 秒重建一次整张表，
            会把用户正在看的选中行与滚动位置一起清掉（桌面程序里很显眼的毛病）。
            指纹里带上价格、市值换手与监控开关那一格（含 tooltip），
            所以价格一变、提醒一到、开关一拨，表就会重画。
            """
            rows = pool.pool_page_rows(self.cfg.db_path)
            symbols = [str(r.get("symbol") or "") for r in rows]
            local = self._local_closes(symbols)
            alerts = self._alerts_today()
            names = self._stock_names(symbols)
            # 自选表一次查出来：每一行的监控开关状态要用它（原来右键菜单每条查一次，
            # 现在表格每行都要画这一格，逐行开连接就太浪费了）
            watchlist = self._watchlist_map()
            # 哪些票的**持仓**被右键关掉了监控（一次查询，见 `intraday.monitor_off_symbols`）：
            # 这一行即使是策略标的或自选，也不会产生任何盘中提醒 —— 要写进 tooltip，
            # 否则用户会以为"表里有它、提醒却从来不响"是坏了
            monitor_off = intraday.monitor_off_symbols(self.cfg.db_path)
            for row in rows:
                if not row.get("name"):
                    # 池子行里存的名称是**建池那一刻**的（新股/改名之后会不一致），
                    # 库里没有才退回行里那个
                    row["name"] = names.get(str(row.get("symbol") or ""), "")

            cells: list[tuple[Any, ...]] = []
            for row in rows:
                symbol = str(row.get("symbol") or "")
                price_item, pct_item, _price = self._price_cells(symbol, local)
                cap_item, turn_item = self._snapshot_cells(symbol)
                alert = alerts.get(symbol)
                row_tip = self._row_tooltip(row, monitor_off=symbol in monitor_off)
                # 名称(代码)：**半角括号**，与 `docs/改版方案.md` 第四节的口径一致
                name_item = self._tag_item(
                    f"{row.get('name') or ''}({symbol})".strip(), symbol, row_tip
                )
                # 「名称(代码)」这一列**加粗**（用户："所有名称显示不清楚，都加黑显示"）
                name_item.setFont(_bold_name_font(self.pool_table))
                industry_item = self._tag_item(str(row.get("industry") or market.DASH), symbol)
                source_item = self._tag_item(str(row.get("source_label") or market.DASH),
                                             symbol)
                # 「监控开关」：自选行可切换；**策略/公式选中的票不是自选**，
                # 没有"停用"这一说（与右键菜单里那项灰掉是同一个判据）
                entry = watchlist.get(symbol)
                if entry is None:
                    monitor_item = self._monitor_cell(
                        symbol, True, alert, togglable=False,
                        why="这只票是策略/公式选中的（不是自选）：要停止盯它在"
                            "「策略选股」里关掉对应策略，或右键【删除】这一行",
                    )
                else:
                    monitor_item = self._monitor_cell(
                        symbol, bool(int(entry.get("enabled", 1) or 0)), alert,
                    )
                for item in (price_item, pct_item, cap_item, turn_item, industry_item,
                             source_item, monitor_item):
                    item.setData(Qt.ItemDataRole.UserRole, symbol)
                    self._attach_row_tooltip(item, row_tip)
                cells.append((name_item, price_item, pct_item, cap_item, turn_item,
                              industry_item, source_item, monitor_item))

            signature = tuple(
                (str(r.get("symbol") or ""), r.get("name"), r.get("source_label"),
                 r.get("industry"), r.get("note"), r.get("watchlist_enabled"),
                 c[1].text(), c[2].text(), c[3].text(), c[4].text(),
                 c[WATCH_MONITOR_COLUMN].text(), c[WATCH_MONITOR_COLUMN].toolTip())
                for r, c in zip(rows, cells)
            )
            if signature == self._pool_signature:
                return
            self._pool_signature = signature

            self.pool_table.setRowCount(len(cells))
            for i, row_cells in enumerate(cells):
                for column, item in enumerate(row_cells):
                    self.pool_table.setItem(i, column, item)

            total, strategy, watch = pool.pool_counts(rows)
            self.pool_count_label.setText(f"共 {total} 只（策略 {strategy} · 自选 {watch}）")
            self.pool_empty_label.setVisible(not rows)

        def _watchlist_map(self) -> dict[str, dict]:
            """自选表全量（`{symbol: 行}`）—— 「监控开关」那一格要按它判能不能切换。

            一次查整张表：这一列每一行都要用（原来只有右键菜单用，逐行查一次还行，
            现在每 5 秒刷表，逐行开连接就太浪费了）。读不到就返回空字典 ——
            这时所有行都会按"策略标的"画（不开、也不假装能切）。
            """
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    return dict(storage.watchlist_map(conn))
            except Exception as exc:  # noqa: BLE001 - 读不到就不显示开关状态
                logger.debug(f"取自选表失败（监控开关按策略行画）：{exc}")
                return {}

        def _alerts_today(self) -> dict[str, dict]:
            """今天每只票最新一条提醒（两张表共用一次查询）。"""
            try:
                return intraday.alerts_today_by_symbol(self.cfg.db_path)
            except Exception as exc:  # noqa: BLE001 - 读不到提醒就是"今天没事"，界面照常
                logger.debug(f"取今日提醒失败：{exc}")
                return {}

        def _symbol_at(self, table: Any, row: int) -> str:
            """这一行的代码（从任意一格的 `UserRole` 取；空行返回空串）。"""
            if row < 0 or row >= table.rowCount():
                return ""
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    symbol = item.data(Qt.ItemDataRole.UserRole)
                    if symbol:
                        return str(symbol)
            return ""

        def _on_table_cell_clicked(self, table: Any, row: int, column: int) -> None:
            """单击「名称(代码)」→ 打开雪球；单击「监控开关」→ **切换这一行的监控**。

            两个动作都只认**自己那一列**：整张表随便点一下就跳浏览器（或改了监控）
            会很烦人 —— 用户只是想选一行的时候就被弹走/被改了状态。

            2026-09-17（用户要求）：监控开关"点一下就能切换"。两种情况：
            - **持仓表**：`position.monitor` 取反（与右键【关闭监控】调的是同一个方法
              `on_toggle_position_monitor`，两处不会各说各话）；
            - **自选股池**：只有**自选**行能切（`watchlist.enabled`）；策略/公式选中的票
              不是自选，没有"停用"这一说 —— 点它不静默失败，而是给一句指路的话
              （与右键菜单里那一项灰掉 + tooltip 说明是同一个判据）。
            """
            symbol = self._symbol_at(table, row)
            if not symbol:
                return
            if column == WATCH_MONITOR_COLUMN and table is self.pool_table:
                entry = self._watchlist_map().get(symbol)
                if entry is None:
                    self._toast(
                        "这只票是策略/公式选中的（不是自选）：不能在这里单独关掉监控 ——"
                        "在「策略选股」里关掉对应策略，或右键【删除】这一行"
                    )
                    return
                self.on_watch_toggle(not bool(int(entry.get("enabled", 1) or 0)), symbol)
                return
            if column == POSITION_MONITOR_COLUMN and table is self.position_table:
                self.on_toggle_position_monitor(symbol, not self._position_monitored(symbol))
                return
            if column != 0:
                return
            url = xueqiu_url(symbol)
            if not url:
                return
            opened = QDesktopServices.openUrl(QUrl(url))
            logger.info(f"打开雪球个股页：{symbol} → {url}（ok={bool(opened)}）")

        def _on_table_menu(self, table: Any, pos: Any, kind: str) -> None:
            """右键菜单：把 `_row_menu` 建好的菜单弹在鼠标位置（Qt 回调入口）。

            `exec` 要**全局坐标**（Qt6 起不再接受局部坐标，传局部会弹到屏幕角落）。
            """
            item = table.itemAt(pos)
            if item is None:
                return
            menu = self._row_menu(table, item.row(), kind)
            if menu is None:
                return
            menu.exec(table.viewport().mapToGlobal(pos))

        def _row_menu(self, table: Any, row: int, kind: str) -> Any:
            """给一行建右键菜单：**【删除】/【关闭监控】↔【打开监控】/【打开雪球】**。

            行级操作跟着行走：右键点的是哪一行就操作哪一行，不需要"先在表格里选中
            再点顶部按钮"（那正是原来最容易点错行的做法）。

            单独抽成一个方法（而不是塞在 `_on_table_menu` 里直接 `exec`）有两个理由：
            `exec` 会**阻塞**在事件循环里（自动化测试点不动它），
            而菜单内容（有哪些项、灰不灰、点了调谁）正是要断言的东西 ——
            分开之后测试可以直接把菜单建出来逐项检查。
            """
            symbol = self._symbol_at(table, row)
            if not symbol:
                return None
            menu = QMenu(table)
            act_delete = QAction("删除", menu)
            if kind == "pool":
                act_delete.triggered.connect(
                    lambda _=False, s=symbol: self.on_pool_row_delete(s)
                )
                enabled = self._pool_row_watch_enabled(symbol)
                if enabled is None:
                    # 策略/公式选中的票不是自选，没有"停用监控"这一说 ——
                    # 灰掉并把理由写在 tooltip 里（比一个点了没反应的菜单项好）
                    act_toggle = QAction("关闭监控", menu)
                    act_toggle.setEnabled(False)
                    act_toggle.setToolTip(
                        "这只票是策略/公式选中的（不是自选）。要停止盯它，"
                        "去「策略选股」关掉对应策略，或直接【删除】这一行"
                    )
                else:
                    act_toggle = QAction("关闭监控" if enabled else "打开监控", menu)
                    act_toggle.triggered.connect(
                        lambda _=False, s=symbol, e=enabled: self.on_watch_toggle(not e, s)
                    )
            else:
                act_delete.triggered.connect(
                    lambda _=False, s=symbol: self.on_delete_position(s)
                )
                monitored = self._position_monitored(symbol)
                act_toggle = QAction("关闭监控" if monitored else "打开监控", menu)
                act_toggle.setToolTip(
                    "关闭监控 = 这只票**不再产生任何盘中提醒**（止损/止盈/做T/竞价/异动），"
                    "在池子/自选那一侧也不再进观察面；它仍然留在表里，随时可以打开"
                )
                act_toggle.triggered.connect(
                    lambda _=False, s=symbol, m=monitored:
                    self.on_toggle_position_monitor(s, not m)
                )
            menu.addAction(act_delete)
            menu.addAction(act_toggle)
            menu.addSeparator()
            act_open = QAction("打开雪球", menu)
            act_open.setEnabled(bool(xueqiu_url(symbol)))
            act_open.triggered.connect(lambda _=False, s=symbol: self._open_xueqiu(s))
            menu.addAction(act_open)
            return menu

        def _open_xueqiu(self, symbol: str) -> None:
            """打开雪球个股页（右键菜单那一项；单击名称走 `_on_table_cell_clicked`）。"""
            url = xueqiu_url(symbol)
            if url:
                QDesktopServices.openUrl(QUrl(url))

        def _pool_row_watch_enabled(self, symbol: str) -> bool | None:
            """这一行的自选开关状态；不在自选表里返回 None（= 策略/公式选中的行）。"""
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    entry = storage.watchlist_map(conn).get(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取自选状态失败：{exc}")
                return None
            if not entry:
                return None
            return int(entry.get("enabled", 1)) == 1

        def _position_monitored(self, symbol: str) -> bool:
            """这条持仓还在监控中吗（`position.monitor`，默认 1 = 监控）。"""
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    row = storage.load_positions(conn, open_only=False).get(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取持仓监控状态失败：{exc}")
                return True
            return bool(int((row or {}).get("monitor", 1) or 0))

        def on_pool_row_delete(self, symbol: str) -> None:
            """右键【删除】一行池内标的。

            两种情形分开办（**不能只删自选**）：
            - 它在自选表里 → 删自选。若它同时是策略选中的票，这一行会以"策略标的"的样子
              继续留着 —— 那正是用户想要的（"不想要的自选去掉，策略选的还在"）；
            - 它不在自选表里（纯策略/公式选中）→ 从**今日池子**里删掉这一行，
              下次【开始选股】会重新评估（提示里明说这句，否则用户以为删不干净）。
            """
            from laoa_trader.data import storage

            self._invalidate_summary()
            try:
                with storage.connect(self.cfg.db_path) as conn:
                    removed_watch = storage.remove_watchlist(conn, symbol)
                    removed_pool = 0 if removed_watch else storage.delete_pool_symbol(
                        conn, symbol
                    )
            except Exception as exc:  # noqa: BLE001
                self._toast(f"删除失败：{type(exc).__name__}: {exc}")
                return
            if removed_watch:
                self._toast(f"已删除自选 {symbol}")
            elif removed_pool:
                self._toast(f"已从今日池子移除 {symbol}（下次【开始选股】会重新评估）")
            else:
                self._toast(f"没找到 {symbol} 的池子行")
            self._pool_signature = None
            self._tick()

        # ── 「持仓监控」页 ──

        def _refresh_positions(self) -> None:
            """填「持仓监控」：名称(代码)/成本价/现价/涨幅/市值/换手/盈亏比例/止损位/止盈位/监控开关。

            三个关键口径：
            - **现价与盈亏比例用的是同一个价**（`_price_cells` 的返回值）—— 老代码的
              「盈亏比例」用后复权收盘价、而"现价"在别处算，两列经常对不上
              （送股之后甚至差一倍），那是这次要修的 bug；
            - 止损位/止盈位 = `成本 × (1 ∓ 比例)`，比例来自「系统设置 → T策略」；
            - 缺价一律 `—`（**不是 0.00%**：0.00% 是"刚好打平"这个真实结论，
              与"没取到价"完全是两回事）。
            """
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    positions = storage.load_positions(conn)
            except Exception as exc:  # noqa: BLE001 - 库坏了也要让这一页画出来
                logger.debug(f"读持仓失败：{exc}")
                positions = {}
            rows = list(positions.values())
            symbols = [str(r.get("symbol") or "") for r in rows]
            local = self._local_closes(symbols)
            alerts = self._alerts_today()
            # 复选框状态跟着配置走（两边是同一个键，用户可能在设置页改过）。
            # 这里必须**屏蔽信号**：这只是把配置"镜像"到控件上，不是用户操作 ——
            # 不屏蔽的话，一键保存之后这次同步会**再触发一次写盘**，还会用
            # "T策略已开"那条提示把"✅ 已保存 N 项"的回显顶掉（实测踩到过）。
            blocked = self.t_strategy_box.blockSignals(True)
            self.t_strategy_box.setChecked(bool(getattr(self.cfg, "intraday_t", False)))
            self.t_strategy_box.blockSignals(blocked)

            cells: list[tuple[Any, ...]] = []
            for row in rows:
                symbol = str(row.get("symbol") or "")
                cost = float(row.get("avg_cost") or 0)
                price_item, pct_item, price = self._price_cells(symbol, local)
                cap_item, turn_item = self._snapshot_cells(symbol)
                monitored = bool(int(row.get("monitor", 1) or 0))
                note = str(row.get("note") or "").strip()
                row_tip = "\n".join(p for p in (
                    f"备注：{note}" if note else "",
                    "监控中（盘中提醒会盯它）" if monitored
                    else "已关闭监控：不再产生任何盘中提醒（止损/止盈/做T/竞价/异动），"
                         "也不进池子/自选的观察面（右键或点这一格可打开）",
                ) if p)
                name_item = self._tag_item(
                    f"{row.get('name') or ''}({symbol})".strip(), symbol, row_tip
                )
                # 「名称(代码)」这一列**加粗**（用户："所有名称显示不清楚，都加黑显示"）
                name_item.setFont(_bold_name_font(self.position_table))
                if not monitored:
                    # 关了监控的行用灰字：一眼能看出来（监控开关那一格也是灰的）
                    name_item.setForeground(QBrush(QColor(Qt.GlobalColor.gray)))
                # 盈亏比例：与「现价」用的是同一个价格；缺价 → `—`
                if price is None or not cost:
                    profit_item = QTableWidgetItem(market.DASH)
                    profit_item.setToolTip("没有现价或没填成本价，算不出盈亏比例")
                else:
                    profit = (price - cost) / cost * 100
                    profit_item = QTableWidgetItem(f"{profit:+.2f}%")
                    color = market.value_color(profit)
                    if color:
                        profit_item.setForeground(QBrush(QColor(color)))
                    profit_item.setToolTip(
                        f"（现价 {price:.2f} − 成本 {cost:.2f}）÷ 成本 {cost:.2f}"
                        f" = {profit:+.2f}%"
                    )
                alert = alerts.get(symbol)
                monitor_item = self._monitor_cell(
                    symbol, monitored, alert,
                    why="点这一格 = 关闭 / 打开监控；右键菜单里的两项是同一件事",
                )
                stop_item = QTableWidgetItem(
                    _fmt_float(cost * (1 - self.cfg.stop_loss)) if cost else market.DASH
                )
                target_item = QTableWidgetItem(
                    _fmt_float(cost * (1 + self.cfg.take_profit)) if cost else market.DASH
                )
                for item in (cost_item := self._tag_item(
                        _fmt_float(cost) if cost else market.DASH, symbol),
                        price_item, pct_item, cap_item, turn_item, profit_item,
                        stop_item, target_item, monitor_item):
                    item.setData(Qt.ItemDataRole.UserRole, symbol)
                    self._attach_row_tooltip(item, row_tip)
                cells.append((name_item, cost_item, price_item, pct_item, cap_item,
                              turn_item, profit_item, stop_item, target_item,
                              monitor_item))

            signature = tuple(
                (str(r.get("symbol") or ""), r.get("name"), r.get("avg_cost"), r.get("note"),
                 r.get("monitor"), c[2].text(), c[3].text(), c[4].text(), c[5].text(),
                 c[6].text(),
                 c[POSITION_MONITOR_COLUMN].text(),
                 c[POSITION_MONITOR_COLUMN].toolTip())
                for r, c in zip(rows, cells)
            )
            if signature == self._position_signature:
                return
            self._position_signature = signature
            self.position_table.setRowCount(len(cells))
            for i, row_cells in enumerate(cells):
                for column, item in enumerate(row_cells):
                    self.position_table.setItem(i, column, item)
            self.position_count_label.setText(
                f"共 {len(rows)} 只" if rows else "还没有持仓：在上面填代码 + 成本价记一笔"
            )

        def on_toggle_t_strategy(self, checked: bool) -> None:
            """「T策略」开关（持仓监控页那一行）→ 写回 `intraday_t`（与系统设置同一个键）。"""
            self._save_updates(
                {"intraday_t": bool(checked)},
                "T策略（做T近似提示）已" + ("开" if checked else "关"),
            )

        def on_toggle_position_monitor(self, symbol: str, enabled: bool) -> None:
            """右键：打开/关闭这只**持仓**的监控（`position.monitor`）。

            "关闭监控"的真实作用（用户要求"名副其实"）：这只票
            - **不再产生任何盘中提醒** —— 止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点
              （`intraday.watch_targets` 的观察面）、做T（`intraday.held_positions`）、
              竞价（`intraday.alert_universe` / 全市场扫描）、当日异动（`alert_universe`）
              四个入口都按同一个判据排除；
            - **在池子/自选那一侧也不进观察面**。

            **优先级**：这只票同时是策略标的或自选时，**以持仓上的监控开关为准** ——
            见 `intraday.monitor_off_symbols` 里的理由（右键是"对这只票"最新最具体的表态，
            而"它在池子里"是几天前跑策略的结果，池子每天重建）。

            持仓仍然留在表里、仍然显示盈亏（灰字 + tooltip 写明已关闭监控）：
            用户想表达的是"别盯它了"，而不是"我没有这只票"（那是【删除】）。
            """
            from laoa_trader.data import storage

            try:
                with storage.connect(self.cfg.db_path) as conn:
                    changed = storage.set_position_monitor(conn, symbol, enabled)
            except Exception as exc:  # noqa: BLE001
                self._toast(f"开关监控失败：{exc}")
                return
            if not changed:
                self._toast(f"没找到持仓 {symbol}")
                return
            self._toast(f"{symbol} 已{'打开' if enabled else '关闭'}监控"
                        + ("" if enabled else "（不再产生任何盘中提醒：止损/止盈/做T/竞价/异动）"))
            self._position_signature = None
            self._tick()

        # ── 自选股的增删与启停（右键菜单与输入行共用同一批方法）──

        def on_watch_add(self) -> None:
            """加自选：名称自动从本地库补；查不到允许添加但提示。"""
            self._invalidate_summary()
            from laoa_trader.data import storage

            symbol = self.watch_symbol.text().strip().zfill(6)
            note = self.watch_note.text().strip()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("自选股代码要填 6 位数字")
                return
            try:
                name = self.engine.get_stock_names([symbol]).get(symbol)
                hint = "（本地库没有它的名称，已按代码添加）" if not name else ""
                with self.engine.connect() as conn:
                    storage.upsert_watchlist(conn, symbol, name=name, note=note, enabled=True)
                    enabled_count = len(storage.load_watchlist(conn, enabled_only=True))
                text = f"已加自选：{symbol} {name or ''}{hint}"
                if enabled_count > self.cfg.watchlist_max:
                    text += (f"；⚠️ 自选 {enabled_count} 只已超过上限 "
                             f"{self.cfg.watchlist_max}，盘中只监控前 {self.cfg.watchlist_max} 只")
                self._toast(text)
                self.watch_symbol.clear()
                self.watch_note.clear()
                self._pool_signature = None
                self._tick()
                self._select_watch_row(symbol)
            except Exception as exc:  # noqa: BLE001
                self._toast(f"加自选失败：{type(exc).__name__}: {exc}")

        def _selected_watch_symbol(self) -> str:
            """当前操作对象：表格选中的行优先，其次输入框里的代码。

            为什么这样：点完【添加自选】输入框会被清空，用户接着想删掉刚才那只，
            如果只认输入框就会得到"先填代码"——所以选中行优先更符合直觉。
            """
            rows = self.pool_table.selectionModel().selectedRows()
            if rows:
                symbol = self._symbol_at(self.pool_table, rows[0].row())
                if symbol:
                    return symbol
            text = self.watch_symbol.text().strip()
            return text.zfill(6) if text else ""

        def _select_watch_row(self, symbol: str) -> None:
            """把表格选中行移到指定代码（加完自选就能直接接着操作它）。"""
            for i in range(self.pool_table.rowCount()):
                if self._symbol_at(self.pool_table, i) == symbol:
                    self.pool_table.selectRow(i)
                    return

        def on_watch_remove(self, symbol: str = "") -> None:
            """删自选：只删 `watchlist` 表里那一行，别的什么都不动。

            与右键菜单的关系：菜单里的【删除】由 `on_pool_row_delete` 分发 ——
            **在自选表里的行**走这条路径（删自选），**纯策略/公式选中的行**走
            "从今日池子移除"那条（它本来就不是自选，删自选删不掉它）。
            这个方法负责"没给代码时自己去哪儿找"（当前选中的行 → 输入框）。
            """
            self._invalidate_summary()
            from laoa_trader.data import storage

            symbol = symbol or self._selected_watch_symbol()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("先点一行自选，或填 6 位代码再删除")
                return
            with self.engine.connect() as conn:
                removed = storage.remove_watchlist(conn, symbol)
            self._toast(f"已删除自选 {symbol}" if removed else f"未找到自选 {symbol}")
            self._pool_signature = None
            self._tick()

        def on_watch_toggle(self, enabled: bool, symbol: str = "") -> None:
            """启用/停用自选：停用后不进池、不监控，但仍留在列表里（看得见才能再打开）。"""
            self._invalidate_summary()
            from laoa_trader.data import storage

            symbol = symbol or self._selected_watch_symbol()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("先点一行自选，或填 6 位代码再启用/停用")
                return
            with self.engine.connect() as conn:
                changed = storage.set_watchlist_enabled(conn, symbol, enabled)
            if not changed:
                self._toast(f"未找到自选 {symbol}")
                return
            self._toast(f"已{'启用' if enabled else '停用'} {symbol}"
                        + ("" if enabled else "（不进池、不监控）"))
            self._pool_signature = None
            self._tick()

        def _auction_text(self, symbol: str) -> str:
            """这只票的竞价强度那一行：`竞价 +3.2% 量比 2.8`（没有数据/不在窗口 → 空串）。

            原来它画在股票池卡片上；卡片视图按用户要求取消之后，它进了行的 tooltip
            （见 `_row_tooltip`）—— 竞价只在 9:15–9:25 有数据，为它单开一列
            等于 95% 的时间空着一格。
            """
            fields = self.auction_snapshot.get(symbol)
            return intraday.auction_card_text(fields) if fields else ""


        def _auction_tick(self) -> None:
            """竞价窗口内每分钟取一次（不在窗口里、或没开这个功能时一次请求都不发）。"""
            if not bool(getattr(self.cfg, "intraday_auction", True)):
                return
            if not intraday.auction_fetch_window():
                return
            self.request_auction()

        def request_auction(self) -> None:
            """后台取一次竞价快照 → 刷新卡片上那一行（失败只记日志，界面照常）。"""
            if self._auction_worker is not None and self._auction_worker.isRunning():
                return
            worker = Worker(intraday.fetch_auction, self.cfg.db_path, self.cfg)
            self._auction_worker = worker
            worker.finished_ok.connect(self._on_auction_ready)
            worker.failed.connect(
                lambda msg: logger.info(f"竞价取数失败（卡片不显示竞价行）：{msg.splitlines()[0]}")
            )
            worker.start()

        def _on_auction_ready(self, snapshot: Any) -> None:
            """竞价数据回来了（主线程）：换成新数据并按最新内容重建卡片。"""
            self.auction_snapshot = snapshot if isinstance(snapshot, dict) else {}
            self._refresh_pool()

        def _stock_names(self, symbols: list[str]) -> dict[str, str]:
            """`{symbol: 名称}`（一次查询）：代码 → 名字，供界面拼 `名称(代码)`。"""
            wanted = [s for s in dict.fromkeys(symbols) if s]
            if not wanted:
                return {}
            try:
                return self.engine.get_stock_names(wanted)
            except Exception as exc:  # noqa: BLE001 - 取不到名字就退回显示代码
                logger.debug(f"取股票名称失败：{exc}")
                return {}

        @staticmethod
        def _alert_target_text(row: dict, names: dict[str, str]) -> str:
            """提醒里那一列：`名称(代码)`。

            三种退化都要好看：没有名字 → 只显示代码；连代码都没有（老库里的历史
            提醒行、将来某类"全市场"提醒）→ 用说明里的名称，再不行才 `—`。
            **绝不显示 `（None）`**。
            """
            symbol = str(row.get("symbol") or "").strip()
            name = str(names.get(symbol) or "").strip()
            if symbol:
                return f"{name}({symbol})" if name else symbol
            detail = str(row.get("detail") or "").strip()
            return detail.split(" ")[0] if detail else "—"

        def _drain_tray(self) -> None:
            """把后台线程投递的托盘消息弹出来（跨线程只能这样传递）。"""
            from laoa_trader.notify import tray as tray_mod

            for message in tray_mod.drain(limit=5):
                self.tray.showMessage(message["title"], message["body"])
                self._tray_notified += 1

        # ── 提醒：响声 + 图标闪烁 + QQ 式浮窗 ──
        #
        # 为什么要自绘浮窗：Windows 原生 Toast 的**点击行为不受我们控制**
        # （点不开、看不到内容），盯盘提醒"出现了却点不开"等于没提醒。
        # 详细取舍见 `ui/alert_popup.py` 的模块说明。

        @staticmethod
        def _alert_key(row: dict) -> tuple:
            """一条提醒的判据：`(日期, 标的, 类型)` —— 与库里的去重键同一个口径。"""
            return (
                str(row.get("date") or ""),
                str(row.get("symbol") or ""),
                str(row.get("kind") or ""),
            )

        def _check_new_alerts(self) -> None:
            """和库对一次账：有**新**提醒就响声 + 闪图标 + 弹浮窗。

            为什么从库里读、而不是等通知回调：盘中提醒是调度线程（后台 QThread）
            跑出来的，跨线程直接碰 Qt 控件会随机崩溃；库是两条路径都认的同一份事实，
            界面每 5 秒对一次账最稳（顺带覆盖了"手动点【检查盘面】"那条路）。
            """
            try:
                rows = intraday.alert_rows(self.cfg.db_path, limit=ALERT_SCAN_LIMIT)
            except Exception as exc:  # noqa: BLE001 - 读不到就是这轮不弹，界面照常
                logger.debug(f"读提醒失败（本轮不弹浮窗）：{exc}")
                return
            keys = [self._alert_key(row) for row in rows]
            if self._alert_seen is None:
                # 第一次只记账不弹：启动时把今天早上的提醒全弹一遍是骚扰
                self._alert_seen = set(keys)
                return
            fresh = [row for row, key in zip(rows, keys) if key not in self._alert_seen]
            self._alert_seen.update(keys)
            if len(self._alert_seen) > ALERT_SEEN_LIMIT:
                # 长跑一整天也不让它无限涨（只留当前这批，旧的自然已经弹过）
                self._alert_seen = set(keys)
            if fresh:
                # 库里按时间倒序（新的在前），浮窗也按这个顺序显示
                self._notify_alerts(fresh)

        def _notify_alerts(self, alerts: list[dict]) -> None:
            """新提醒到了：响声 → 图标闪烁 → 弹浮窗（三步各自独立，一步失败不影响其它）。"""
            if not alerts:
                return
            if not bool(getattr(self.cfg, "notify_popup", True)):
                # 浮窗关掉 = 用户说"别打扰我"：连声音和闪烁一起免了，只入库
                # （要看就去「盘中提醒」页 / 托盘【最近提醒】）
                return
            if bool(getattr(self.cfg, "notify_sound", True)):
                sound.play()
            self._start_alert_flash()
            self.show_alert_popup(alerts)

        def _alert_items(self, rows: list[dict]) -> list[dict]:
            """把库里的提醒行转成浮窗要的展示条目（`名称(代码)` 在这里拼好）。"""
            names = self._stock_names([str(row.get("symbol") or "") for row in rows])
            items = []
            for row in rows:
                item = dict(row)
                item["target"] = self._alert_target_text(row, names)
                item["name"] = str(names.get(str(row.get("symbol") or "")) or "")
                item["kind_label"] = str(row.get("label") or row.get("kind") or "")
                item["price_text"] = _fmt_float(row.get("price")) if row.get("price") else ""
                item["time"] = str(row.get("pushed_at") or "")
                items.append(item)
            return items

        def show_alert_popup(self, alerts: list[dict] | None = None, *,
                             recent: bool = False) -> bool:
            """弹（或更新）提醒浮窗。

            Args:
                alerts: 要显示的提醒行；`None` = 从库里取最近的几条。
                recent: 是不是"用户主动看最近提醒"（托盘【最近提醒】）——
                    这种就算 `notify_popup = false` 也照弹（是用户自己点名要看的）。

            Returns:
                真的弹出来了 → True（没有提醒可弹 → False）。
            """
            if not recent and not bool(getattr(self.cfg, "notify_popup", True)):
                return False
            if alerts is None:
                try:
                    alerts = intraday.alert_rows(
                        self.cfg.db_path, limit=self._popup_max_items()
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"读最近提醒失败：{exc}")
                    alerts = []
                recent = True
            items = self._alert_items([dict(row) for row in alerts])
            if not items:
                if recent:
                    self._toast("最近还没有盘中提醒")
                return False
            return bool(self._ensure_popup().show_alerts(items))

        def _popup_max_items(self) -> int:
            """浮窗最多列几条（配置写坏时按默认 5 条，绝不因为一个坏数字不弹窗）。"""
            try:
                return max(1, int(getattr(self.cfg, "notify_popup_max_items", 5)))
            except (TypeError, ValueError):
                return 5

        def _ensure_popup(self) -> Any:
            """浮窗只建一次（顶层窗口建一次就够了），参数**每次都从配置同步**。

            为什么每次都同步：设置页改完 `notify_popup_seconds` 之后不该等到
            重启才生效（配置对象就是唯一事实来源）。
            """
            if self.alert_popup is None:
                popup = AlertPopup()
                popup.item_clicked.connect(self.on_alert_item_clicked)
                popup.view_all_clicked.connect(self.on_show_all_alerts)
                popup.closed.connect(self._stop_alert_flash)
                self.alert_popup = popup
            self.alert_popup.max_items = self._popup_max_items()
            try:
                seconds = int(getattr(self.cfg, "notify_popup_seconds", 8))
            except (TypeError, ValueError):
                seconds = 8
            self.alert_popup.seconds = max(1, seconds)
            return self.alert_popup

        def on_alert_item_clicked(self, item: dict) -> None:
            """点了浮窗里的某一条 → 停闪 + 弹这条的详情。"""
            self._stop_alert_flash()
            self.show_alert_detail(item)

        def on_show_all_alerts(self, _checked: bool = False) -> None:
            """【查看全部】/【最近提醒】：停闪 → 显示主窗口 → 切到「自选股池」页。

            为什么跳这一页（原来是「盘中提醒」页，那一页已按用户要求取消）：
            提醒现在住在两张表的「提醒」列里，而「自选股池」是**池子的唯一入口** ——
            用户点【查看全部】想看的是"哪几只票出了什么事"，那一页正好一行一只。
            """
            self._stop_alert_flash()
            self._restore_window()
            index = self.tabs.indexOf(self.watch_page)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            self._pool_signature = None           # 强制重画一次（提醒可能刚写进库）
            self._refresh_pool_table()

        def on_show_recent_alerts(self, _checked: bool = False) -> None:
            """托盘菜单【最近提醒】：没有浮窗就按最近的提醒现弹一个。"""
            popup = self.alert_popup
            if popup is not None and popup.isVisible():
                # 已经挂着一个（可能用户正看着）→ 抬起来就行，别重算一遍
                popup.raise_()
                return
            self.show_alert_popup(recent=True)

        def show_alert_detail(self, item: dict) -> None:
            """提醒详情（可复制）：提醒原因 + 时间 + L2 条件单参数。

            非模态（`show` 而不是 `exec`）：用户可以先看别的，也不会卡住自动化测试
            —— 与「状态详情」同一个取舍。
            """
            if self.alert_detail_dialog is None:
                dialog = QDialog(self)
                dialog.setWindowTitle("提醒详情")
                # 尺寸跟着主窗口走（别在小屏上开出一个比主窗口还大的对话框）
                dialog.resize(min(560, max(360, self.width() - 80)),
                              min(420, max(260, self.height() - 160)))
                layout = QVBoxLayout(dialog)
                box = QPlainTextEdit()
                box.setReadOnly(True)          # 只读但**可选中复制**
                box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                self.alert_detail_box = box
                layout.addWidget(box, 1)
                row = QHBoxLayout()
                self.btn_copy_alert_detail = QPushButton("复制条件单参数")
                self.btn_copy_alert_detail.setToolTip(
                    "把这段文字复制到剪贴板（可直接抄进券商条件单）"
                )
                self.btn_copy_alert_detail.clicked.connect(self.on_copy_alert_detail)
                row.addWidget(self.btn_copy_alert_detail)
                self.btn_all_alerts = QPushButton("查看全部")
                self.btn_all_alerts.setToolTip("打开主窗口的「盘中提醒」页")
                self.btn_all_alerts.clicked.connect(self.on_show_all_alerts)
                row.addWidget(self.btn_all_alerts)
                row.addStretch(1)
                self.btn_close_alert_detail = QPushButton("关闭")
                self.btn_close_alert_detail.clicked.connect(dialog.close)
                row.addWidget(self.btn_close_alert_detail)
                layout.addLayout(row)
                self.alert_detail_dialog = dialog
            self.alert_detail_box.setPlainText(self._alert_detail_text(item))
            self.alert_detail_dialog.show()
            self.alert_detail_dialog.raise_()

        def _alert_detail_text(self, item: dict) -> str:
            """详情文本 = 浮窗那一行 + 时间 + **推送原文**（含 L2 条件单参数）。

            推送原文直接调 `intraday.format_message` 生成 —— 与真正推给用户的那份
            **同源**，不另写一套（否则"浮窗里看到的参数"和"推送里的参数"迟早不一致）。
            """
            target = str(item.get("target") or item.get("symbol") or "—")
            head = "　".join(
                part for part in (
                    target,
                    str(item.get("kind_label") or item.get("kind") or ""),
                    str(item.get("price_text") or ""),
                ) if part
            )
            alert = {
                "kind": str(item.get("kind") or ""),
                "name": str(item.get("name") or ""),
                "symbol": str(item.get("symbol") or ""),
                "detail": str(item.get("detail") or ""),
                "price": item.get("price"),
            }
            pushed = ""
            try:
                _title, lines = intraday.format_message([alert], self.cfg.db_path, self.cfg)
                # 推送文本里那两行是 Markdown 代码块（飞书要这样），详情窗去掉围栏
                pushed = "\n".join(lines).replace("```\n", "").replace("\n```", "").strip()
            except Exception as exc:  # noqa: BLE001 - 算不出条件单也要把原因显示出来
                logger.debug(f"生成提醒详情失败（退回简版）：{exc}")
            parts = [head, f"时间：{item.get('time') or '—'}", ""]
            if pushed:
                head_line, *_rest = pushed.split("\n")
                parts.append(head_line)
                if _rest:
                    parts.extend([""] + _rest)
            else:
                parts.append(f"说明：{item.get('detail') or '—'}")
            return "\n".join(parts).strip()

        def on_copy_alert_detail(self, _checked: bool = False) -> None:
            """把详情（含条件单参数）写进剪贴板 —— 与弹窗里显示的是同一份文本。"""
            text = ""
            if self.alert_detail_box is not None:
                text = self.alert_detail_box.toPlainText()
            if not text:
                self._toast("没有可复制的内容")
                return
            QApplication.clipboard().setText(text)
            self._toast("已复制提醒详情（含条件单参数）")

        # ── 图标闪烁 ──

        def _start_alert_flash(self) -> None:
            """托盘 + 任务栏图标闪 `notify_flash_seconds` 秒（用户点开就立刻停）。

            为什么要闪：浮窗可能被别的窗口盖住、声音可能被静音，
            而托盘图标一直在用户眼皮底下 —— 闪它是最不会被忽略的一路。
            """
            try:
                seconds = int(getattr(self.cfg, "notify_flash_seconds", 6))
            except (TypeError, ValueError):
                seconds = 6
            if seconds <= 0 or self.tray is None:
                return
            self._normal_tray_icon = self._normal_tray_icon or self.tray.icon()
            self._flash_icon = self._alert_tray_icon()
            if self._flash_icon is None:
                return  # 连红点图标都画不出来：不闪（浮窗和声音照常）
            self._flash_token += 1
            token = self._flash_token
            self._flashing = True
            self._flash_step()          # 立刻亮一次，不然要等半秒才看得出
            self._flash_timer.start(FLASH_INTERVAL_MS)
            # 代次放在闭包里：上一条提醒的"到点停闪"不能把这一条刚起的闪烁掐掉
            QTimer.singleShot(seconds * 1000, lambda: self._stop_alert_flash(token))
            try:
                # 任务栏按钮闪烁（Windows 上就是任务栏图标闪）——0 = 一直闪到窗口被激活
                QApplication.alert(self, 0)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"任务栏提醒失败（不影响浮窗与托盘闪烁）：{exc}")

        def _flash_step(self) -> None:
            """一闪：正常图标 ↔ 红点图标交替。"""
            if self.tray is None or self._flash_icon is None or self._normal_tray_icon is None:
                return
            self._flash_on = not self._flash_on
            self.tray.setIcon(self._flash_icon if self._flash_on else self._normal_tray_icon)

        def _stop_alert_flash(self, token: int | None = None) -> None:
            """停止闪烁并恢复原图标（用户已经看到了 / 到点了）。

            Args:
                token: 哪一次闪烁的"到点停"；与当前代次不符就忽略
                    （否则连续两条提醒时，第一条的定时器会把第二条的闪烁掐掉）。
            """
            if token is not None and token != self._flash_token:
                return
            if self._flash_timer is not None:
                self._flash_timer.stop()
            self._flashing = False
            self._flash_on = False
            if self.tray is not None and self._normal_tray_icon is not None:
                self.tray.setIcon(self._normal_tray_icon)

        def _alert_tray_icon(self) -> Any:
            """带红点的托盘图标：**程序化画**在现成的 32px 图标右上角。

            为什么不预置一张红点 png：图标本身会随版本迭代（`build/make_icon.py` 画的），
            预置的那张很快就会和主图不一致；现画永远跟着主图走。
            画好的那张会存进**数据目录的 cache**（方便排查），不进仓库/安装包。
            """
            if self._flash_icon is not None:
                return self._flash_icon
            try:
                from PySide6.QtGui import QPainter

                base = assets.icon_png(32)
                pixmap = QPixmap(str(base)) if base else self.tray.icon().pixmap(32, 32)
                if pixmap.isNull():
                    return None
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setBrush(QBrush(QColor(market.COLOR_UP)))   # 涨红 = 提醒色
                painter.setPen(Qt.PenStyle.NoPen)
                size = max(8, pixmap.width() // 3)
                # 画在右上角，并稍微内缩：托盘图标边缘常被系统裁掉几像素
                painter.drawEllipse(pixmap.width() - size - 1, 1, size, size)
                painter.end()
                self._write_alert_icon(pixmap)
                self._flash_icon = QIcon(pixmap)
            except Exception as exc:  # noqa: BLE001 - 画不出来就不闪，浮窗照弹
                logger.debug(f"红点图标生成失败（不闪图标）：{exc}")
                return None
            return self._flash_icon

        def _write_alert_icon(self, pixmap: Any) -> None:
            """红点图标落一份到数据目录的 cache（**不进仓库**）。"""
            try:
                cache = Path(self.cfg.data_dir) / "cache"
                cache.mkdir(parents=True, exist_ok=True)
                pixmap.save(str(cache / TRAY_ALERT_ICON_NAME), "PNG")
            except Exception as exc:  # noqa: BLE001 - 存不下不影响闪烁
                logger.debug(f"红点图标落盘失败：{exc}")

        def _toast(self, text: str) -> None:
            """轻提示：状态栏 + 托盘气泡（不用模态弹窗打断操作）。"""
            self._set_status(text)
            try:
                self.tray.showMessage("老A选股助手", text)
            except Exception:  # noqa: BLE001
                pass

        # ── 按钮回调 ──

        def _busy(self) -> bool:
            """是否有后台任务在跑（避免重复点击开两个下载）。"""
            if self._worker is not None and self._worker.isRunning():
                self._toast("有任务正在运行，请稍候…")
                return True
            return False

        def _set_progress_visible(self, visible: bool) -> None:
            """显示/隐藏标题区那条**细进度条**（只有任务在跑时才显示）。

            为什么不做成常驻：一条恒在的灰条既占地方，又会被误读成"卡在 0%"；
            任务跑完还留着更糟 —— 用户会以为还有事情没做完。
            """
            self._job_running = bool(visible)
            self.progress.setVisible(bool(visible))

        def _run_worker(self, fn, label: str, with_progress: bool = False,
                        with_stage: bool = False, with_note: bool = False) -> None:
            # 明确设成"确定进度"的 0%（range=0..100）：下载最初几秒在取签名/建连，
            # 这期间进度条必须显示 0% 而不是"未开始/不确定"的样子
            self.progress.setRange(0, 100)
            self.progress.setFormat(f"{label} %p%")
            self.progress.setValue(0)
            self._set_progress_visible(True)     # 任务开始 → 细进度条出现
            self._set_status(f"{label}…")
            worker = Worker(fn, with_progress=with_progress, with_stage=with_stage,
                            with_note=with_note)
            self._worker = worker
            worker.progress.connect(self._on_progress)
            worker.stage.connect(lambda name: self._set_status(f"{label}：{name}…"))
            worker.note.connect(lambda text: self._on_worker_note(label, text))
            worker.finished_ok.connect(lambda result: self._on_worker_done(label, result))
            worker.failed.connect(lambda msg: self._on_worker_failed(label, msg))
            worker.start()

        def _on_worker_note(self, label: str, text: str) -> None:
            """后台任务的一句话状态（下载重签/重试/完成）→ 运行状态那一句。"""
            self._set_status(f"{label}：{text}")

        @staticmethod
        def _progress_texts(stage: str, done: int, total: int) -> tuple[str, str]:
            """(进度条上的文字, 状态栏文字)。

            下载 dump 时数值是**字节**（几百 MB）：不显示 MB 与百分比的话，
            180 MB / 十几分钟的过程看起来就像卡死了（用户实测反馈）。
            """
            if total >= 1_000_000:
                pct = (done / total * 100) if total else 0.0
                label = f"{stage}｜{done / 1e6:.1f}/{total / 1e6:.1f} MB"
                status = (f"{stage} {pct:.0f}%"
                          f"（{done / 1e6:.0f}/{total / 1e6:.0f} MB）")
                return label, status
            if total > 0:
                return f"{stage}｜{done}/{total}", f"{stage}：{done}/{total}"
            return f"{stage}｜{done}", f"{stage}：{done}"

        def _on_progress(self, stage: str, done: int, total: int) -> None:
            label, status = self._progress_texts(stage, done, total)
            # 进度条本身**不显示文字**（细条放不下，而且百分比在运行状态那句话里），
            # 但 `format` 仍然设着：将来真要显示时不必再改一遍。
            self.progress.setFormat(label + " %p%")
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(min(done, max(total, 1)))
            self._set_progress_visible(True)     # 有进度回传 = 一定有任务在跑
            # 记下"阶段 + 数量"，下载中那行会把它补在百分比后面（例如 `· 81/181 MB`）
            self._progress_stage_text = (
                f"{stage} {done / 1e6:.0f}/{total / 1e6:.0f} MB" if total >= 1_000_000
                else (f"{stage} {done}/{total}" if total > 0 else f"{stage} {done}")
            )
            self._set_status(status, progress=True)

        def _on_worker_done(self, label: str, result: Any) -> None:
            self.progress.setValue(self.progress.maximum())
            self._set_progress_visible(False)    # 任务结束 → 细进度条收起来
            # 后台任务可能改了数据（下载/同步/建池）：让概况缓存作废，状态栏立刻跟上
            self._invalidate_summary()
            # 下载/导入刚结束：下一次刷新要**完整**刷一遍（见 `_tick` 的下载跳过逻辑）
            self._heavy_paused = True
            if isinstance(result, sync.SyncResult):
                self._toast(f"{label}完成：{result.message}")
                if label == "下载历史数据":
                    self._on_download_done(result)
            elif isinstance(result, list) and result and hasattr(result[0], "message"):
                self._toast(f"{label}完成：" + "；".join(r.message for r in result))
            elif isinstance(result, dict) and result and set(result) <= set(KINDS):
                # 三路通知的结果字典：逐路报成功/失败，缺凭证的显示"跳过"
                self._toast(f"{label}：{summarize(result)}")
            elif isinstance(result, dict) and "pool" in result:
                self._on_pipeline_done(label, result)
            elif isinstance(result, dict):
                self._toast(f"{label}完成：{result.get('data_date') or ''}"
                            f" 池子 {len(result.get('pool') or [])} 只")
            else:
                self._toast(f"{label}完成")
            self._tick()

        def _on_pipeline_done(self, label: str, report: dict) -> None:
            """把选股流程的 report 翻成一句中文结论（含失败原因，不弹窗打断）。"""
            pool_count = len(report.get("pool") or [])
            data_date = report.get("data_date") or "无行情日"
            bits = [f"行情日 {data_date}", f"信号 {report.get('picks', 0)} 条",
                    f"池子 {pool_count} 只"]
            formulas = report.get("formulas") or {}
            if formulas.get("ran"):
                # 「公式」组的运行结果也要说出来：不然用户勾了公式却"什么都看不到"
                picked = sum((formulas.get("picks") or {}).values())
                bits.append(f"公式 {len(formulas['ran'])} 条选出 {picked} 只")
            # 公式的运行期错误（某只票算不出来之类）会显示在公式列表里（标红），
            # 顺手刷一次那张表，用户切过去就能看到原因
            page = getattr(self, "formula_page", None)
            if page is not None:
                try:
                    page.reload()
                except Exception:  # noqa: BLE001 - 刷新列表失败不该影响结论显示
                    logger.debug("公式列表刷新失败", exc_info=True)
                # 把**这一轮**的结果直接喂给「策略选股」页的「本次选股结果」区
                # （不喂它也能从 `stock_pool` 兜底读最近一次建池，但那是"库里那一份"：
                # 遇到"没建成池 / 同一天跑了两次"时，直接给 report 才是刚跑完的这一批）。
                # 用 getattr 容错：另一个模块可能还没提供这个方法，不该因此让流程报错。
                show_result = getattr(page, "show_pick_result", None)
                if callable(show_result) and isinstance(report, dict):
                    try:
                        show_result(report, data_date=report.get("data_date"))
                    except Exception:  # noqa: BLE001 - 结果区画不出来不影响结论与提醒
                        logger.debug("填充本次选股结果失败", exc_info=True)
            if report.get("signals"):
                bits.append(f"写入信号 {report['signals']} 行")
            if report.get("pushed"):
                from laoa_trader.notify import summarize as _sum

                bits.append("通知：" + _sum(report.get("notify") or {}))
            elif report.get("push_skipped"):
                # 两种"没推"要分清：① 同一天同一批内容已经推过（幂等，正常现象）；
                # ② 今天命中的全是「依赖开盘」的策略（默认不推）—— 后者必须把原因说出来，
                #    否则用户会以为"策略今天没选到票"
                if report.get("push_skipped_kind") == "filtered":
                    bits.append("未推送：" + str(report["push_skipped"]))
                else:
                    bits.append("未重复推送")
            if report.get("push_note"):
                skipped = report.get("push_skipped_rows") or []
                bits.append(f"已跳过 {len(skipped)} 只依赖开盘的标的（进池但没推送，"
                            "因为 push_only_proven 开着）")
            errors = report.get("errors") or []
            text = f"{label}完成：" + "，".join(bits)
            if errors:
                text += "；⚠️ " + errors[0]
            if not pool_count and not errors:
                text += "（今日没有候选：非交易日 / 数据不足 / 所选策略组无信号）"
            self._toast(text)

        def _on_worker_failed(self, label: str, msg: str) -> None:
            logger.error(f"{label}失败：{msg}")
            self.progress.setValue(0)
            self._set_progress_visible(False)    # 失败也算"任务结束"：进度条收起来
            self._set_status(f"❌ {label}失败：{msg.splitlines()[0]}")

        # ── 保存设置（写回 config.toml，保留注释与未知键）──

        # ── 「数据来源」的来源列表 ──
        #
        # **真相源是 `data/sources.py`**：有哪些来源、各自要什么凭据、能做什么、
        # 有什么已知风险，全由 `source_states(cfg)` 给（界面不另写一份能力文案 ——
        # 抄一份出来，第二个来源落地时两边一定会不一致）。
        # 列表只画**已启用**的来源（`data_sources` 里写着的那些，顺序即优先级），
        # 其余的实现好的来源进【添加来源】菜单，用户点了才加。

        def _source_states(self) -> tuple[list[dict], str]:
            """要画的来源行 + "取不到真相源"时的原因（原因非空 = 走了兜底）。

            **防御性导入**：`data.sources` 还没写好、或者它自己抛异常时，
            退回"只有内置同花顺一行 + 我自己那句如实说明"，窗口照常打开 ——
            一张设置页绝不该把整个程序拖死（用户报障时连界面都进不去就什么都问不出来）。
            """
            try:
                from laoa_trader.data import sources as sources_mod

                states = list(sources_mod.source_states(self.cfg))
            except Exception as exc:  # noqa: BLE001 - 注册表读不出来不该拦住界面
                logger.warning(f"读数据来源注册表失败（退回内置来源那一行）：{exc}")
                return [self._fallback_source_state()], f"{type(exc).__name__}: {exc}"
            rows = [state for state in states if state.get("enabled")]
            # 内置同花顺**永远在列表里**（它提供历史日K 的 dump 与实时快照），但
            # **排在最后**：列表顺序 = 取数优先级，而 2026-09-17 起
            # "同花顺是主源、公开源兜底"（用户 2026-09-18 拍板）—— 插在第一位就等于告诉用户
            # "同花顺最优先"，与 config.py 里 `data_sources = ["public", "hithink"]`
            # 的默认顺序正好相反。配置里漏写它时也补在这儿（并如实说明"配置里没写它"）。
            if not any(str(r.get("id")) == BUILTIN_SOURCE for r in rows):
                rows.append(self._fallback_source_state(
                    configured=BUILTIN_SOURCE in [
                        str(x) for x in (self.cfg.data_sources or [])
                    ]
                ))
            # 认不出的名字（用户手改过 `data_sources`）在注册表里根本没有 ——
            # 照实补一行只读的，别让"我明明写了它"变成"列表里凭空少了一个"
            known = {str(state.get("id")) for state in states}
            for name in (self.cfg.data_sources or []):
                name = str(name)
                if name and name not in known:
                    rows.append({
                        "id": name, "name": name, "enabled": True, "needs_key": False,
                        "has_key": False, "capabilities_text": "",
                        "note": "这个来源名是你在 config.toml 的 data_sources 里写的，"
                                "但程序还没有它的实现 —— 界面只如实显示，不会拿它取数",
                        "key_config": None, "unknown": True,
                    })
            return rows, ""

        def _fallback_source_state(self, *, configured: bool = True) -> dict:
            """读不到注册表时那一行：内置同花顺（能力文案用本地常量，如实标注原因）。"""
            note = ("数据来源注册表暂时读不出来（见日志）：这里只显示内置的同花顺。"
                    "它提供 历史日K · 股票列表 · 实时快照。")
            if not configured:
                note = ("config.toml 的 data_sources 里没有写内置的 hithink —— "
                        "程序不会用它取数，请把它加回去。" + note)
            return {
                "id": BUILTIN_SOURCE, "name": DATA_SOURCE_LABELS[BUILTIN_SOURCE],
                "enabled": True, "needs_key": True, "has_key": bool(self.cfg.hithink_api_key),
                "capabilities_text": SOURCE_CAPABILITIES.get(BUILTIN_SOURCE, ""),
                "note": note, "key_config": "hithink_api_key", "fallback": True,
            }

        def on_test_source_key(self, source: str) -> None:
            """【测试连接】：拿**那个来源输入框里的 Key**真发一次最小请求，结论写回提示行。

            为什么不做成弹窗：结论只有一句话（能用 / 不能用 + 下一步），
            弹窗会拦住用户；写进「数据来源」那一组的提示行里，他抬头就能看到。
            为什么"真发一次请求"而不是只做格式校验：Key 失效、被限流、服务端没就绪
            这几种情况的**表现一模一样**（都是取不到数），只有真打一次才分得清。
            """
            if self._busy():
                return
            row = (self.source_rows or {}).get(str(source))
            if row is None or row.key_edit is None:
                return
            key = row.key_edit.text().strip()
            if not key:
                self._show_key_hint(
                    "❌ 这个输入框是空的：先把 Key 填进去再点【测试连接】；"
                    f"申请地址见下面那行（{BUILTIN_KEY_URL}）"
                )
                self._toast("❌ Key 是空的 · 见【系统设置】那一行的说明")
                return
            self._toast("正在测试这个 Key…")
            QApplication.processEvents()
            try:
                text = probe_data_source(self.cfg, api_key=key)
            except Exception as exc:  # noqa: BLE001 - 探测本身不许把界面搞崩
                text = f"❌ 测试失败：{type(exc).__name__}: {exc}"
            self._show_key_hint(text)
            self._toast(text.splitlines()[0])

        def _source_key_text(self, key_field: str) -> str:
            """这个来源当前存着的 Key（没有对应配置键时返回空串）。"""
            if not key_field:
                return ""
            return str(getattr(self.cfg, key_field, "") or "")

        def _addable_sources(self) -> list[dict]:
            """【添加来源】的候选：**已实现但还没启用**的来源（来自注册表）。"""
            try:
                from laoa_trader.data import sources as sources_mod

                states = list(sources_mod.source_states(self.cfg))
            except Exception as exc:  # noqa: BLE001 - 读不到就没有可加的
                logger.debug(f"读数据来源注册表失败（添加来源无候选）：{exc}")
                return []
            return [state for state in states if not state.get("enabled")]

        def _rebuild_source_rows(self) -> None:
            """按 `source_states(cfg)` 重画来源列表（一行一个来源）。"""
            layout = getattr(self, "source_list_layout", None)
            if layout is None:
                return
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.source_rows = {}
            rows, fallback_reason = self._source_states()
            self._source_fallback_reason = fallback_reason
            #: 配置里写着的来源名（顺序 = 优先级）—— 标记怎么给按**配置**判，
            #: 不按"这一轮画出来几行"判：注册表读不出来时只画得出同花顺一行，
            #: 若按行数判就会给同花顺贴上"主来源"，而配置里明明还有公开源。
            listed = [str(x).strip() for x in (self.cfg.data_sources or []) if str(x).strip()]
            #: 配置里的第一个 = 实际优先级最高的那个（标记就按它判，不写死来源名）
            main_id = listed[0].lower() if listed else ""
            for index, state in enumerate(rows):
                source = str(state.get("id") or "")
                builtin = source == BUILTIN_SOURCE
                unknown = bool(state.get("unknown"))
                key_config = str(state.get("key_config") or "")
                # 标记**完全按配置顺序判定**（列表顺序 = 优先级），不写死任何一个来源名：
                # 用户手改 `data_sources` 之后，界面说的必须还是配置里的事实。
                # 2026-09-18 起默认顺序是 ["hithink", "public"]，所以同花顺显示"主来源"；
                # 公开源**有意不贴"兜底"**，而是让行自己显示"免 Key"（那是用户能据此判断
                # "能不能用"的状态，比再贴一个角色词有用）；哪天用户把公开源排到第一，
                # "主来源"会自动跟着挪过去。
                # 只有**排在第一位**的那个给角色标记（"主来源"）；其余留给行自己按
                # Key 状态显示（"免 Key" / "已配 Key" / "未配 Key"）—— 那三个状态都是
                # 用户能动手解决或能据此判断"能不能用"的，比再贴一个"兜底"有用。
                # "谁是兜底"由**顺序**（列表顺序 = 优先级）与每行的 note 说明表达，
                # 而 note 里已经写着"没配同花顺 Key 时的兜底源"。
                if unknown:
                    tag = "未实现"
                elif source.lower() == main_id:
                    tag = "主来源"
                else:
                    tag = ""
                row = SourceRow(
                    source,
                    name=str(state.get("name") or source),
                    capability=str(state.get("capabilities_text") or ""),
                    note=str(state.get("note") or ""),
                    builtin=builtin,
                    implemented=not unknown,
                    needs_key=bool(state.get("needs_key", True)),
                    has_key=bool(state.get("has_key")),
                    key_text=self._source_key_text(key_config),
                    key_config=key_config,
                    key_placeholder=("在 fuyao.aicubes.cn/admin 获取"
                                     if builtin else "这个来源的 Key / Token"),
                    tag=tag,
                    # 内置同花顺那一行：**输入框 + 【测试连接】照给**（2026-09-18 用户澄清），
                    # 下面再挂一行"申请地址"（`key_notice` 那行文案）。
                    # 程序里**不预置任何 Key**：输入框的初值就是用户自己 config.toml 里的值。
                    key_notice=(BUILTIN_BACKUP_TEXT.format(url=BUILTIN_KEY_URL)
                                if builtin else ""),
                    key_notice_url=BUILTIN_KEY_URL if builtin else "",
                )
                if row.key_edit is not None and key_config:
                    row.key_edit.setToolTip(
                        f"「{state.get('name')}」用它自己的 Key（写回 config.toml 的 "
                        f"{key_config}）；留空 = 没配，就走后面的来源兜底"
                    )
                if not builtin:
                    row.btn_delete.clicked.connect(
                        lambda _=False, src=source: self.on_remove_source(src)
                    )
                if row.btn_test is not None:
                    row.btn_test.clicked.connect(
                        lambda _=False, src=source: self.on_test_source_key(src)
                    )
                layout.addWidget(row)
                self.source_rows[source] = row
            if BUILTIN_SOURCE not in listed:
                # 配置里没写内置来源：**照实说明**（不静默补上，用户要知道取数会失败）
                logger.warning(f"data_sources 里没有内置的 {BUILTIN_SOURCE}：{listed}")
            label = getattr(self, "data_source_label", None)
            if label is not None:
                # 列表与「配置里的原值」那一行必须同步：只改一处的话，用户加了来源之后
                # 上面那行还写着旧的 data_sources，看着像"没生效"
                label.setText(self._source_text(self.cfg.data_sources))
            self._refresh_source_hint()

        def _refresh_source_hint(self) -> None:
            """【添加来源】下面那句说明：还能加什么（或为什么现在没得加）。"""
            hint = getattr(self, "source_add_hint", None)
            if hint is None:
                return
            candidates = self._addable_sources()
            if not candidates:
                # 两个来源都加完了（或者注册表读不出来）→ 如实说明，不画假条目
                hint.setText(SOURCE_ADD_UNAVAILABLE_TEXT)
                return
            parts = []
            for state in candidates:
                cap = str(state.get("capabilities_text") or "")
                extra = (f"（免 Key，提供 {cap}）" if not state.get("needs_key")
                         else f"（要用你自己的 Key，提供 {cap}）")
                parts.append(f"{state.get('name')}{extra}")
            hint.setText("可以添加：" + "；".join(parts)
                         + "　—— 添加后写回 config.toml 的 data_sources，"
                           "顺序就是取数的优先级（前一个不可用就落到下一个）")

        def _source_add_menu(self) -> Any:
            """【添加来源】的菜单（候选来自注册表；没有候选就返回 None）。

            抽成一个方法（而不是在点击回调里直接 `exec`）：`exec` 会阻塞在事件循环里，
            自动化测试点不动它 —— 而"菜单里有哪些项、点了写什么"正是要断言的东西。
            """
            candidates = self._addable_sources()
            if not candidates:
                return None
            menu = QMenu(self.settings_page)
            for state in candidates:
                action = QAction(str(state.get("name") or state.get("id")), menu)
                cap = str(state.get("capabilities_text") or "")
                action.setToolTip(
                    f"提供 {cap}；" + (str(state.get("note") or ""))
                )
                source_id = str(state.get("id") or "")
                action.triggered.connect(
                    lambda _=False, sid=source_id: self.on_add_source(sid)
                )
                menu.addAction(action)
            return menu

        def on_add_source_clicked(self) -> None:
            """【添加来源】按钮：菜单里选一个（没得选就如实说明，不加条目）。"""
            menu = self._source_add_menu()
            if menu is None:
                self._refresh_source_hint()
                self._toast(SOURCE_ADD_UNAVAILABLE_TEXT)
                return
            menu.exec(self.btn_add_source.mapToGlobal(
                self.btn_add_source.rect().bottomLeft()))

        def on_add_source(self, source: str = "") -> None:
            """把某个**已实现**的来源加进 `data_sources`（现有键，走 `save_settings`）。

            为什么只写 `data_sources` 这一个键：启停就是"在不在这个列表里"
            （见 `data/sources.py` 的模块头）—— 再加一个 `xxx_enabled` 就会出现
            "列表里有、开关是关"这种自相矛盾、用户看不懂的状态。
            """
            source = str(source or "")
            candidates = {str(s.get("id")): s for s in self._addable_sources()}
            if source not in candidates:
                self._refresh_source_hint()
                self._toast(SOURCE_ADD_UNAVAILABLE_TEXT)
                return
            sources = [str(x) for x in (self.cfg.data_sources or []) if str(x).strip()]
            if source in sources:
                return
            sources.append(source)
            self._save_updates({"data_sources": sources},
                               f"已添加来源：{candidates[source].get('name') or source}")
            self._rebuild_source_rows()

        def on_remove_source(self, source: str) -> None:
            """删掉一个用户添加的来源（内置的那个删不掉）。"""
            if source == BUILTIN_SOURCE:
                self._toast("内置主来源不能删除（实时快照只有它提供）")
                return
            sources = [str(x) for x in (self.cfg.data_sources or []) if str(x) != source]
            self._save_updates({"data_sources": sources}, f"已删除来源：{source}")
            self._rebuild_source_rows()

        def _source_text(self, sources: Any) -> str:
            """「数据来源 → 取数顺序」那一行：**照 `cfg.data_sources` 如实显示**。

            为什么不写"主来源：同花顺"：主来源取决于 `data_sources` 的**顺序**
            （2026-09-17 起默认是 `["public", "hithink"]`，公开源在前），写死一个名字，
            用户改过 `data_sources` 之后界面就在说假话。所以这行给的是**取数顺序**本身。

            认不出的键**原样显示**并注明"界面没有它的实现"，而不是干脆不显示
            （那用户就不知道程序到底打算用哪个来源）。
            """
            names = [str(x) for x in (sources or []) if str(x).strip()]
            if not names:
                return ("取数顺序：（`data_sources` 是空的 —— 程序不会取任何行情数据，"
                        "请填上 `public`（免 Key）或 `hithink`）")
            shown = []
            for name in names:
                label = self._source_label(name)
                shown.append(f"{label}" if label else f"{name}（界面没有它的实现，"
                                                       f"只如实显示）")
            return (f"取数顺序（前一个不可用就落到下一个）：{'、'.join(shown)}"
                    f"　·　config.toml: "
                    f"data_sources = [{', '.join(repr(n) for n in names)}]")

        @staticmethod
        def _source_label(name: str) -> str:
            """来源键 → 中文显示名（**先问注册表**，读不到才退回本地小表）。

            为什么要问注册表：注册表（`data/sources.py`）才是"有哪些来源、各自叫什么"
            的真相源；本地小表（`DATA_SOURCE_LABELS`）只是**读不到它时的兜底**。
            原来只查本地小表，于是新增的公开源在"取数顺序"那一行会显示成
            "public（界面没有它的实现）"—— 明明是主源，却写着"没有实现"，是假话。
            """
            key = str(name or "").lower()
            label = DATA_SOURCE_LABELS.get(key)
            if label:
                return label
            try:
                from laoa_trader.data import sources as sources_mod

                info = getattr(sources_mod, "REGISTRY", {}).get(key)
            except Exception as exc:  # noqa: BLE001 - 读不到就用"认不出"的表达
                logger.debug(f"读数据来源注册表失败（显示名退回本地小表）：{exc}")
                return ""
            return str(getattr(info, "name", "") or "") if info is not None else ""

        def _history_months_text(self) -> str:
            """`history_years` → `6 个月`（用户看的是"几个月"，配置里存的是"年"）。"""
            try:
                months = float(self.cfg.history_years) * MONTHS_PER_YEAR
            except (TypeError, ValueError):
                return "—"
            return f"{months:g} 个月"

        def _sync_data_amount_label(self) -> None:
            """数据量那一行的说明跟着输入框走（改了值马上看到"那是几个月"）。"""
            months = float(self.history_years_box.value()) * MONTHS_PER_YEAR
            self.data_amount_label.setText(
                f"下载 {months:g} 个月（history_years = "
                f"{float(self.history_years_box.value()):g}）"
            )

        @staticmethod
        def _log_path_text() -> str:
            """日志文件的真实路径（与【显示详情】里那一份同一口径）。"""
            return str(log_file_path() or "（还没有日志文件）")

        def on_copy_paths(self) -> None:
            """把数据目录与日志路径一起复制到剪贴板（报障时要贴的就是这两行）。"""
            text = f"数据目录：{self.data_dir_edit.text()}\n日志路径：{self.log_path_edit.text()}"
            QApplication.clipboard().setText(text)
            self._toast("已复制数据目录与日志路径")

        def _save_updates(self, updates: dict, ok_text: str) -> Any:
            """统一的保存流程：写文件 → 同步内存配置 → 刷新界面提示。

            Returns:
                写入的路径（`Path`）；写失败返回 None（调用方据此决定要不要回显"已保存"）。
            """
            from laoa_trader.config import save_settings

            try:
                path, self.cfg = save_settings(self.cfg, updates)
                self._toast(f"{ok_text}（已写入 {path.name}）")
            except OSError as exc:
                # 只读盘/权限问题：给中文原因，不弹 traceback
                self._toast(f"保存失败：{exc}（可手改 config.toml）")
                return None
            except Exception as exc:  # noqa: BLE001
                self._toast(f"保存失败：{type(exc).__name__}: {exc}")
                return None
            self._refresh_status()
            self._refresh_channel_hints()
            self._refresh_auction_hint()
            return path

        def save_group_selection(self, groups: list[str], strategies: list[str]) -> bool:
            """写回"跑哪些策略组/哪几条策略"（`enabled_groups` + `enabled_strategies`）。

            为什么界面上**没有**这个入口了：用户给定的布局把"策略组/策略的启停"
            从设置页移到了「策略选股」的列表里（一处只管一件事）—— 那个列表由
            `FormulaPage` 提供，用户在那里右键【启用】/【关闭】。
            这个方法保留成**一个明确的能力入口**（`config.toml` 的这两个键仍要有人写）：
            校验（至少要有一组、组与策略不能互相矛盾）与写回都还在这里，
            不然"选了却不跑"这种毛病会重新冒出来。

            Returns:
                真的写回了 → True；被拒绝（没有可跑的策略）→ False。
            """
            from laoa_trader.strategy import groups as groups_mod

            chosen_groups = list(groups)
            # 勾了策略但没勾它的组：自动把组也勾上，避免出现"选了却不跑"
            for name in strategies:
                key = groups_mod.group_of(name)
                if key and key not in chosen_groups:
                    chosen_groups.append(key)
            if not chosen_groups:
                self._toast("至少要勾一个策略组（或全部勾上 = 全跑）")
                return False
            selection = groups_mod.resolve(chosen_groups, strategies)
            if selection.empty:
                self._toast("没有可跑的策略：" + "；".join(selection.warnings))
                return False
            return bool(self._save_updates(
                {"enabled_groups": chosen_groups, "enabled_strategies": list(strategies)},
                f"策略组已保存：{selection.describe()}",
            ))

        def on_save_groups(self) -> None:
            """兼容旧入口：按**当前配置**里的策略组原样再写一遍（等价于"没改动"）。

            改版后设置页里不再有策略组勾选框（入口在「策略选股」的列表里，
            见 `save_group_selection`）。这个方法留着是为了让老的调用点
            （脚本/测试/将来可能的快捷键）不会因为找不到方法而炸：
            它做的是**幂等**的重写，不改变任何选择。
            """
            self.save_group_selection(list(self.cfg.enabled_groups or []),
                                      list(self.cfg.enabled_strategies or []))

        def _panel_notify_updates(self) -> dict:
            """把通知设置面板的**当前**状态收集成 {键: 值}（一键保存与测试提醒共用）。"""
            return {
                "notify_popup": self.popup_box.isChecked(),
                "notify_channels": [
                    name for name, box in self.channel_boxes.items() if box.isChecked()
                ],
                "notify_sound": self.sound_box.isChecked(),
                "notify_flash_seconds": int(self.flash_seconds_box.value()),
                "notify_popup_seconds": int(self.popup_seconds_box.value()),
                "notify_popup_max_items": int(self.popup_items_box.value()),
                "notify_windows_sound": self.win_sound_box.isChecked(),
                "notify_windows_open_url": self.win_url_box.isChecked(),
                "notify_tray_duration_ms": int(self.tray_duration.value()),
                "feishu_on": self.feishu_on_box.isChecked(),
                "feishu_app_id": self.feishu_app_id.text().strip(),
                "feishu_app_secret": self.feishu_secret.text().strip(),
                "feishu_chat_id": self.feishu_chat_id.text().strip(),
            }

        def on_save_notify(self) -> None:
            """只保存通知那一组（一键保存里也走同一份收集函数）。"""
            self._toast(self._save_settings(
                self._panel_notify_updates(), extra="；" + self._settings_effect_text()
            ))

        # ── 竞价扫描设置（全市场扫描 + 过滤规则）──

        def _panel_auction_updates(self) -> dict:
            """把"竞价扫描"面板的当前状态收集成 {键: 值}（保存用）。"""
            boards = [key for key, box in self.auction_board_boxes.items() if box.isChecked()]
            if not boards:
                # 一个都不勾 = 不限制：显式写成全部，别让用户看到"保存了空列表 = 什么都不扫"
                boards = list(config.AUCTION_BOARDS)
            good, bad = config.split_scan_at(self.auction_scan_at_edit.text())
            return {
                "intraday_auction": self.auction_on_box.isChecked(),
                "auction_min_pct": float(self.auction_min_pct_box.value()),
                "auction_max_pct": float(self.auction_max_pct_box.value()),
                "auction_min_amount": float(self.auction_amount_box.value()) * 1e4,
                "auction_min_volume_ratio": float(self.auction_ratio_box.value()),
                "auction_min_score": int(self.auction_score_box.value()),
                "auction_alert_max_items": int(self.auction_items_box.value()),
                "auction_boards": boards,
                "auction_scan_at": good or ["09:20", "09:25"],
                "_bad_scan_at": bad,          # 只给提示用，不写进配置文件
            }

        def on_save_auction(self) -> None:
            """只保存竞价那一组：**先校验**（涨幅上下限不能倒挂），再写文件。

            回显里把这一组的几个关键参数再念一遍（与一键保存的通用回显不同）：
            竞价参数之间是**互相牵制**的（涨幅区间、成交额、分数、条数），
            用户改完最想知道的是"现在这套规则到底是什么"。
            """
            updates = self._panel_auction_updates()
            bad = updates.pop("_bad_scan_at", [])
            errors = self._validate_updates(updates)
            if errors:
                message = "❌ 未保存：" + "；".join(errors)
                self._toast(message)
                self._set_settings_hint(message)
                self._refresh_auction_hint(message)
                return
            extra = (
                f"；竞价设置已保存：{'开' if updates['intraday_auction'] else '关'}；"
                f"涨幅 {updates['auction_min_pct']:g}%~{updates['auction_max_pct']:g}%、"
                f"成交额 ≥ {updates['auction_min_amount'] / 1e4:,.0f} 万、"
                f"量比 ≥ {updates['auction_min_volume_ratio']:g}、"
                f"打分 ≥ {updates['auction_min_score']}、"
                f"推前 {updates['auction_alert_max_items']} 只、"
                f"扫描 {', '.join(updates['auction_scan_at'])}"
                + (f"；⚠️ 认不出的时刻已忽略：{'、'.join(bad)}" if bad else "")
            )
            message = self._save_settings(updates, extra=extra)
            self._toast(message)
            self._refresh_auction_hint(extra)
            self._refresh_status()

        def _collect_settings_updates(self) -> dict:
            """本页**所有**控件的当前值 → `{配置键: 值}`（一键保存的唯一收集入口）。

            为什么要"一个收集函数"而不是五组各弹一个 dict：一键保存必须写**同一个键集合** ——
            测试直接断言这个集合（多一个键、少一个键都是 bug：少一个键就是"改了没生效"，
            多一个键就是把不属于本页的东西悄悄改了）。
            """
            updates: dict[str, Any] = {
                # 1) 数据来源
                #
                # 数据来源那一组的**固定键**：数据量（`history_years`）。
                # ⚠️ `hithink_api_key` **不在这份字典里**，但它**照样会被写回** ——
                # 它由本函数末尾那段"按界面上真正存在的行收集"的循环收（那一行的 Key 输入框
                # 就是它的入口）。为什么分两处写：需要 Key 的来源是**可添加的多个**
                # （`data_sources` + 注册表的 `key_config`），那份名单是活的，
                # 所以"要不要收这个键"只认**界面上有没有那个输入框**这一条判据。
                "history_years": float(self.history_years_box.value()),
                # 2) 通知方式（`popup_box` 单独一栏，其余三路来自 channel_boxes）
                **self._panel_notify_updates(),
                # 3) 竞价扫描（`_bad_scan_at` 只给提示用，不进配置文件）
                **{k: v for k, v in self._panel_auction_updates().items()
                   if k != "_bad_scan_at"},
                # 4) T策略（止损/止盈在配置里存**小数**，见 `on_save_t_strategy`）
                "stop_loss": abs(float(self.stop_loss_box.value())) / 100.0,
                "take_profit": abs(float(self.take_profit_box.value())) / 100.0,
                "intraday_t": self.intraday_t_box.isChecked(),
                "t_high_min_gain_pct": float(self.t_high_min_gain_box.value()),
                "t_high_pullback_pct": float(self.t_high_pullback_box.value()),
                "t_low_min_drop_pct": float(self.t_low_min_drop_box.value()),
                "t_low_rebound_pct": float(self.t_low_rebound_box.value()),
                # 5) 其他
                "ui_theme": str(self.theme_box.currentData() or self.cfg.ui_theme),
                "intraday_anomaly": self.anomaly_box.isChecked(),
                "watchlist_max": int(self.watchlist_max_box.value()),
                "watchlist_in_pool": self.watchlist_in_pool_box.isChecked(),
            }
            # 每个**需要 Key 的已启用来源**各写各的 Key（`key_config` 由注册表给）：
            # 将来再加"要 token 的来源"时，它的 Key 会自动进这一份键集合 ——
            # 界面上的输入框与写回配置的键**一一对应**，不存在"填了没保存"。
            # 内置同花顺那一行现在**也有输入框**（2026-09-18 用户澄清："要给我一个
            # key 的输入口"），所以 `hithink_api_key` 也由这一份循环收进来 ——
            # "界面上有没有这个框"仍然是"要不要收这个键"的唯一判据，不另写一份名单。
            # ⚠️ 按**界面上真正存在的行**收集，而不是按注册表的来源列表收集：
            # 输入框长在行上，行才是"界面上有没有这个框"的唯一真相。
            # 早先按注册表收集时有个隐蔽的漏：注册表读不出来时（`source_states` 抛异常）
            # 内置同花顺那一行**照样画着输入框**，但收集时被跳过 —— 用户填了 Key、
            # 点了保存、什么都没写进去（"填了没保存"正是这里最该防的事）。
            # 这个坑是 `test_source_list_falls_back_when_the_registry_is_unreadable` 抓出来的。
            for source, row in (self.source_rows or {}).items():
                field = str(getattr(row, "key_config", "") or "")
                if not field or field in updates or row.key_edit is None:
                    continue
                updates[field] = row.key_edit.text().strip()
            return updates

        @staticmethod
        def _validate_updates(updates: dict) -> list[str]:
            """校验一组待写入的值 → 中文错误列表（空列表 = 可以写）。

            **每一项都要点名**：用户看到"保存失败"却不知道是哪一项，等于让他自己一项项试
            （这正是"不能静默丢弃"的反面）。数值口径与控件的单位一致：
            止损/止盈与四个做T阈值在**面板上是百分数**（5 = 5%），而传进来的
            `stop_loss` / `take_profit` 已经是小数 —— 所以这里按小数比 0。

            只校验**这一批里真的有**的键：各组自己的保存按钮只写本组那几个键，
            不做这个判断的话，"只保存竞价设置"会被"止损为 0"这种**并不存在**的问题拒掉。
            """
            errors: list[str] = []

            def _bad(key: str) -> bool:
                """这一批里有这个键、而且它的值不大于 0。"""
                return key in updates and float(updates.get(key) or 0) <= 0

            for key, label in (
                ("stop_loss", "止损比例"),
                ("take_profit", "止盈比例"),
                ("history_years", "下载年限"),
                ("t_high_min_gain_pct", "做T·冲高幅度"),
                ("t_high_pullback_pct", "做T·回落幅度"),
                ("t_low_min_drop_pct", "做T·杀跌幅度"),
                ("t_low_rebound_pct", "做T·反弹幅度"),
            ):
                if _bad(key):
                    hint = ("大于 0（0 年等于不下任何历史数据）" if key == "history_years"
                            else "大于 0%（填 0 等于「任何一点波动都算」，那种提示只会刷屏）")
                    errors.append(f"{label}（{key}）必须{hint}")
            lo = float(updates.get("auction_min_pct") or 0)
            hi = float(updates.get("auction_max_pct") or 0)
            if "auction_min_pct" in updates and "auction_max_pct" in updates \
                    and hi > 0 and hi <= lo:
                errors.append(f"竞价涨幅上限（{hi:g}%）必须大于下限（{lo:g}%）")
            return errors

        def _save_settings(self, updates: dict, *, extra: str = "") -> str:
            """写一组设置并返回**给用户看的那句话**（一键保存与各组保存共用）。

            校验失败 → 一个字都不写，返回"❌ 未保存：哪一项不对"。
            """
            errors = self._validate_updates(updates)
            if errors:
                message = "❌ 未保存：" + "；".join(errors)
                self._set_settings_hint(message)
                return message
            path = self._save_updates(updates, f"已保存 {len(updates)} 项")
            if path is None:
                message = "❌ 保存失败（原因见运行状态那一行；也可手改 config.toml）"
                self._set_settings_hint(message)
                return message
            message = f"✅ 已保存 {len(updates)} 项（已写入 {path.name}）{extra}"
            self._set_settings_hint(message)
            return message

        def _set_settings_hint(self, text: str) -> None:
            """把保存结果回显在按钮下面（用户不必去翻运行状态那一行）。"""
            if getattr(self, "save_settings_hint", None) is not None:
                self.save_settings_hint.setText(text)
                self.save_settings_hint.setToolTip(text)

        def on_save_settings(self) -> None:
            """底部【保存设置】：**一键写回本页所有设置**。

            顺序是刻意的：先收集（只读控件）→ 校验（有错就一个字都不写、并点名哪一项）
            → 一次写盘 → 回显"保存了几项 + 现在生效的是什么"。
            为什么要回显"生效状态"：写盘成功不等于**生效**（例如改了通知方式但浮窗关着），
            把生效的那几条摆出来，用户才不用去别处核对。
            """
            updates = self._collect_settings_updates()
            message = self._save_settings(updates, extra="；" + self._settings_effect_text())
            self._toast(message)
            # 保存后按新值立刻重算界面：止损位/止盈位两列、竞价说明行、通知提示
            self._position_signature = None
            self._refresh_positions()
            self._refresh_auction_hint()
            self._refresh_channel_hints()
            self._refresh_status()

        def _settings_effect_text(self) -> str:
            """一句话说清"现在生效的是什么"（保存回显的后半句）。"""
            channels = [name for name, box in self.channel_boxes.items() if box.isChecked()]
            notify = "QQ 浮窗" if self.popup_box.isChecked() else "无浮窗"
            if channels:
                notify += " + " + "、".join(
                    {"windows": "系统弹窗", "feishu": "飞书", "tray": "托盘气泡"}.get(
                        name, name)
                    for name in channels
                )
            else:
                notify += "（其它频道都没勾 = 只入库不推送）"
            return (
                f"生效：主题 {theme_mod.theme_label(str(self.theme_box.currentData()))}；"
                f"通知 {notify}；"
                f"竞价扫描 {'开' if self.auction_on_box.isChecked() else '关'}；"
                # 止损/止盈就在「T策略」组里（用户拍板），回显时一并念出来
                f"T策略 {'开' if self.intraday_t_box.isChecked() else '关'}"
                f"（止损 −{float(self.stop_loss_box.value()):g}% / "
                f"止盈 +{float(self.take_profit_box.value()):g}%）；"
                f"当日异动 {'开' if self.anomaly_box.isChecked() else '关'}；"
                f"自选上限 {int(self.watchlist_max_box.value())} 只"
            )

        def _refresh_auction_hint(self, extra: str = "") -> None:
            """竞价那一组的说明行：把"开关 + 上次扫描 + 请求量级"写清楚（看得出设定生效了）。"""
            if not hasattr(self, "auction_hint"):
                return
            lines = []
            if not bool(getattr(self.cfg, "intraday_auction", False)):
                lines.append("竞价扫描当前关着：一次请求都不发（勾上「启用竞价扫描」并保存即可开）")
            else:
                slots = ", ".join(getattr(self.cfg, "auction_scan_at", None) or [])
                lines.append(f"已启用：每个交易日 {slots} 各扫一次全市场"
                             "（约 56 个请求/次，后台线程跑，界面不会卡）")
            last = self._last_auction_scan_text()
            if last:
                lines.append(last)
            if extra:
                lines.append(extra)
            self.auction_hint.setText("　｜　".join(lines))

        def _last_auction_scan_text(self) -> str:
            """上一次扫描的结论（读 `auction_scan` 表）：`上次扫描 09:25：命中 37 只`。"""
            from laoa_trader.data import storage as storage_mod

            try:
                with storage_mod.connect(self.cfg.db_path) as conn:
                    rows = storage_mod.load_auction_scan(conn)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"读上次竞价扫描失败：{exc}")
                return ""
            if not rows:
                return "还没有扫描结果"
            day = str(rows[0].get("day") or "")
            slot = str(rows[0].get("slot") or "")
            total = int(rows[0].get("total") or len(rows))
            pushed = sum(1 for row in rows if row.get("pushed"))
            return f"上次扫描 {day} {slot}：命中 {total} 只（推送 {pushed} 只）"

        # ── 手动跑（与定时任务共用同一套流程，保证幂等）──

        def on_run_pipeline(self) -> None:
            """【开始选股】：先过**数据闸门** → 增量 → 策略 → 建池 → 推送。

            闸门是必须的：数据没下好就点这个按钮，跑出来的池子是错的
            （在只写了一半的库上跑策略），所以这里**明确拒绝并指路**，
            而不是"静默跑出个空池子"骗用户。
            """
            if self._busy():
                return
            if state.is_downloading():
                self._refuse_pipeline("正在下载历史数据，请等下载完成后再选股")
                return
            gate = data_gate(self.cfg, self.engine)
            if not gate["ok"]:
                self._refuse_pipeline(gate["message"])
                return
            if gate.get("result"):
                self.preflight_result = gate["result"]  # 顺手把缓存的结论刷新

            def _job(progress_cb, stage_cb):
                report = run_daily(
                    self.cfg, self.engine, notify=True,
                    progress_cb=progress_cb, stage_cb=stage_cb,
                )
                # 记下"今天跑过了"：定时点就不必再跑一遍（纯省时间）
                self.scheduler.mark_daily_ran(report)
                return report

            self._run_worker(_job, "立即选股并建池",
                             with_progress=True, with_stage=True)

        def _refuse_pipeline(self, message: str) -> None:
            """数据不可用时拒绝选股：中文提示 + 把注意力引到「系统设置」里的下载按钮。

            为什么要把页面切过去：【下载数据】现在住在「系统设置」，只写一句提示
            用户还得自己找那一页；直接切过去 + 焦点落到按钮上，动作是连续的
            （这一页本来就是"数据"那一组所在的地方）。
            """
            text = f"⚠️ {message}"
            self._toast(text)
            self._set_status(text)
            logger.warning(f"已拒绝手动选股：{message}")
            try:
                index = self.tabs.indexOf(self.settings_page)
                if index >= 0:
                    self.tabs.setCurrentIndex(index)
                self.btn_download.setDefault(True)
                self.btn_download.setFocus()
            except Exception:  # noqa: BLE001 - 界面细节失败不影响"拒绝"本身
                pass

        def on_refresh_data(self) -> None:
            """【{BTN_REFRESH_TEXT}】：只跑增量同步，不选股、不推送。"""
            if self._busy():
                return
            self._run_worker(
                lambda progress_cb: refresh_data(self.cfg, self.engine,
                                                 progress_cb=progress_cb),
                "刷新数据",
                with_progress=True,
            )

        def on_download(self) -> None:
            """【下载数据】：没配 Key 时**不弹窗**，只在「系统设置」里把那句话点亮。

            为什么把原来那个 `QMessageBox` 换成页内提示（用户要求"尽量减少弹窗"）：
            缺 Key 不是"必须让用户做决定"的事，而是"下一步该做什么" ——
            弹窗只是拦住他去点确定，然后他还是得去同一个地方改配置。

            2026-09-17 起界面上**没有**填 Key 的入口（那一行只剩
            "申请地址"的说明），所以这句话改成指路 **config.toml / 环境变量** ——
            读数路径没变（`Config.hithink_api_key` 与环境变量 HITHINK_FINANCE_API_KEY
            照旧生效），只是写的地方从界面回到了配置文件。
            """
            if self._busy():
                return
            if not self.cfg.hithink_api_key:
                # 2026-09-18（用户拍板）：**历史数据走用户自己的同花顺 Key**，一次拿到
                # 多年历史；公开源那一路退回"兜底"—— 没 Key 时它照样把**当天**的
                # 行情/涨停池写进库（见 `public_sync.daily_update_public`）。
                # ⚠️ 别写成"历史一天天攒起来就够了"：**攒够了也选不了股** ——
                # 数据自检（`preflight.check`）要求"复权事件非空 + 行业覆盖 ≥90%"，
                # 而这两样只有同花顺那条路会写（`storage.write_adjust_events` 全项目
                # 只被 `sync.py` 那条 dump 路调用）。这是**设计如此**，不是待修项：
                # 缺事件就等于拿"算错的复权价"去选票。所以这句话要说得让人
                # 一眼明白"没 Key 能看什么、不能做什么"，而不是让人以为等几天就行。
                self._show_key_hint(
                    "❌ 完整历史数据要同花顺 API Key（每个用户自己申请一个）："
                    "在**上面那一行的 Key 输入框**里填上、点【保存设置】就生效（不用重启）；"
                    "也可以手改 config.toml 的 hithink_api_key，"
                    "或设环境变量 HITHINK_FINANCE_API_KEY"
                    f"（申请地址见上面那行的 {BUILTIN_KEY_URL}）。"
                    "没 Key 时能用的：实时行情（自选股池/持仓监控）与大盘概览 —— "
                    "每天也会自动把当天的行情与涨停池写进库；"
                    "但**选股要 Key**：数据自检要求「复权事件」与「行业归属」齐备"
                    "（这两样只有同花顺那条路给得到），缺了就拒绝选股"
                    "（就是不让程序拿算错的复权价去选票）"
                )
                self._toast("❌ 选股要同花顺 Key · 没 Key 时行情与大盘概览可用 · 见【系统设置】")
                return
            self._hide_key_hint()
            self._run_worker(
                lambda progress_cb, note_cb: sync.download_history(
                    self.cfg, progress_cb=progress_cb, note_cb=note_cb
                ),
                "下载/更新历史数据",
                with_progress=True, with_note=True,
            )

        def _show_key_hint(self, text: str) -> None:
            """在「系统设置 → 数据来源」里点亮一句话。

            2026-09-17：界面上没有 Key 输入框了，所以这里**不再把焦点给输入框**
            （原来那句"焦点落到输入框上"是给"填 Key"用的），改为把焦点给来源列表那一行 ——
            用户顺着看下去就是"主来源：…申请地址 …"那行说明；写 Key 的地方在 config.toml。
            """
            try:
                self.key_hint.setText(text)
                self.key_hint.setVisible(True)
                row = (self.source_rows or {}).get(BUILTIN_SOURCE)
                if row is not None:
                    row.setFocus()
            except Exception as exc:  # noqa: BLE001 - 提示失败不影响"拒绝下载"本身
                logger.debug(f"显示 Key 提示失败：{exc}")

        def _hide_key_hint(self) -> None:
            try:
                self.key_hint.setText("")
                self.key_hint.setVisible(False)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"隐藏 Key 提示失败：{exc}")

        # 历史（免得后人以为这里少了个函数）：
        #   2026-09-17 内置同花顺那一行改成"来源标记 + Key 申请地址"，随之删掉了
        #   `on_save_api_key()` 与【测试连接】—— 输入框没了，它们就没有被测对象；
        #   2026-09-18 用户澄清"**不要配 KEY**"指的是**程序里不许预置自己的 Key**、
        #   不是不给填，于是那一行又把输入框加回来了，【测试连接】也随之恢复
        #   （现在是 `on_test_source_key()`，按来源取那一行的输入框，见它上面）。
        # Key 的读写三条路都在：
        #   * 读：`Config.hithink_api_key` / 环境变量 `HITHINK_FINANCE_API_KEY`；
        #   * 写：界面上（【保存设置】/【一键保存】会把它写回 config.toml）或手改文件；
        #   * 验：【测试连接】→ `probe_data_source()`（独立函数，测试直接调它）。

        def on_test_notify(self) -> None:
            """一键测试通知（走后台线程：飞书是网络调用，别卡住界面）。

            用**面板当前勾选与参数**实发一条（不要求先保存）—— 边调边试更顺手；
            配置文件不会被这个按钮改动。
            """
            if self._busy():
                return
            import dataclasses

            from laoa_trader.notify import notify_all

            test_cfg = dataclasses.replace(self.cfg, **self._panel_notify_updates())
            lines = [
                "这是一条测试提醒。",
                "收到它说明通知链路正常。",
                "频道与参数取自「设置」页当前勾选（无需先保存）。",
            ]
            self._run_worker(
                lambda: notify_all("🧪 老A选股助手 · 测试提醒", lines, cfg=test_cfg),
                "测试通知",
            )

        def on_intraday_once(self) -> None:
            if self._busy():
                return
            self._run_worker(
                lambda: self.scheduler.run_intraday_now(ignore_session=True),
                "盘中检查",
            )

        def on_toggle_intraday(self) -> None:
            """【暂停提醒】/【恢复提醒】：托盘菜单与设置页共用这一份逻辑。

            两处都要跟着走（按钮文字 + 托盘菜单的可勾选状态），否则用户从托盘关掉、
            回到设置页却看到按钮还写着"暂停提醒"，就会以为没生效。
            """
            # 按钮文字走 `BTN_*` 常量：运行状态里"点【暂停提醒】"这类指路文案引用的就是它
            paused = not self.scheduler.intraday_paused
            if paused:
                self.scheduler.pause_intraday()
            else:
                self.scheduler.resume_intraday()
            self._sync_pause_widgets(paused)
            self._tick()

        def _sync_pause_widgets(self, paused: bool) -> None:
            """把"已暂停"这个状态同步到设置页按钮与托盘菜单项（两处只有一个真相）。"""
            try:
                self.btn_pause.setText(BTN_RESUME_TEXT if paused else BTN_PAUSE_TEXT)
                action = getattr(self, "act_pause", None)
                if action is not None:
                    action.setChecked(paused)
            except Exception as exc:  # noqa: BLE001 - 界面细节失败不影响暂停本身
                logger.debug(f"同步暂停状态失败：{exc}")

        def on_save_t_strategy(self) -> None:
            """只保存「T策略」那一组：开关 + 四个做T阈值 + 止损/止盈比例。

            界面上填的是**百分数**（5 = 5%），配置里存的是**小数**（0.05）——
            与 `intraday.stop_loss()` 的口径一致；这里与 `_collect_settings_updates`
            是同一套换算，别处不许再换一次。
            一键保存走的是同一个收集函数（`_collect_settings_updates`），所以两条路的键
            与口径**必然一致**（这也是一键保存敢一次写回 30 多个键的前提）。
            """
            updates = {
                key: self._collect_settings_updates()[key]
                for key in ("stop_loss", "take_profit", "intraday_t",
                            "t_high_min_gain_pct", "t_high_pullback_pct",
                            "t_low_min_drop_pct", "t_low_rebound_pct")
            }
            message = self._save_settings(
                updates,
                extra=f"；止损 −{updates['stop_loss'] * 100:g}% / "
                      f"止盈 +{updates['take_profit'] * 100:g}%；"
                      f"T策略{'开' if updates['intraday_t'] else '关'}",
            )
            self._toast(message)
            # 「持仓监控」表的止损位/止盈位两列立刻按新比例重算（指纹清掉才会重画）
            self._position_signature = None
            self._refresh_positions()

        def on_save_risk(self) -> None:
            """兼容旧名字：等价于 `on_save_t_strategy()`（那一组以前叫「持仓风险」）。

            留着是为了让老调用点（脚本/测试）不会因为找不到方法而炸；
            它做的是同一件事 —— 保存 `intraday_t` + 四个阈值 + 止损/止盈。
            """
            self.on_save_t_strategy()

        # ── 持仓操作 ──

        def on_add_position(self) -> None:
            """【添加持仓】：**只记 代码 + 成本价 + 备注**（用户给定的字段）。

            数量不再收集（界面也不显示）。库里 `position.quantity` 保留做兼容：
            这里写 `quantity=0`，并靠 `reopen=True` 把"已平仓"标记清掉 ——
            用户手填一行就是"我现在持有它"。成本价必填（没有成本就算不出盈亏比例与
            止损止盈位，那一行除了代码什么都显示不了）。
            """
            self._invalidate_summary()
            symbol = self.pos_symbol.text().strip()
            note = self.pos_note.text().strip()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("请填 6 位股票代码")
                return
            try:
                cost = float(self.pos_cost.text().strip())
            except ValueError:
                self._toast("成本价必须是数字（从券商那里抄成交均价）")
                return
            if cost <= 0:
                self._toast("成本价要大于 0")
                return
            try:
                from laoa_trader.data import storage

                name = self.engine.get_stock_names([symbol]).get(symbol)
                with self.engine.connect() as conn:
                    storage.upsert_position(
                        conn, symbol, name=name, quantity=0, avg_cost=cost,
                        note=note, reopen=True,
                    )
                self._toast(f"已记录持仓 {symbol} {name or ''} @ {cost:.2f}")
                self.pos_symbol.clear()
                self.pos_cost.clear()
                self.pos_note.clear()
                self._position_signature = None
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self._toast(f"写入持仓失败：{exc}")

        def on_delete_position(self, symbol: str = "") -> None:
            """删持仓：右键菜单传代码；手动调用时不传，用表格里选中的那一行。

            为什么**不再**弹 `QInputDialog` 问代码：用户已经在表格里指着那一行了，
            再让他把代码敲一遍是多余的一步（而且敲错了就删错票）。
            """
            self._invalidate_summary()
            from laoa_trader.data import storage

            symbol = symbol.strip() if symbol else ""
            if not symbol:
                rows = self.position_table.selectionModel().selectedRows()
                symbol = self._symbol_at(self.position_table, rows[0].row()) if rows else ""
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("先在持仓表里点一行（或右键那一行）再删除")
                return
            try:
                with self.engine.connect() as conn:
                    removed = storage.delete_position(conn, symbol)
                self._toast(f"已删除持仓 {symbol}" if removed else f"未找到持仓 {symbol}")
                self._position_signature = None
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self._toast(f"删除失败：{exc}")

        # ── 关于（版本 / 版权 / 数据来源）──

        def about_lines(self) -> list[str]:
            """「关于」对话框的正文（显示与"复制版本信息"共用这一份文案）。

            版本号**现场取** `laoa_trader.__version__`：发版只改一处，
            不会出现"界面写着旧版本号、安装包是新版本"这种查半天的错位。
            """
            return [
                APP_NAME,
                f"版本：{laoa_trader.__version__}（测试版）",
                "作者 / 版权所有人：async-chen",
                COPYRIGHT_TEXT,
                SOURCE_TEXT,
            ]

        def version_info_text(self) -> str:
            """报障时要贴给作者的三行：名称 / 版本 / 版权。"""
            lines = self.about_lines()
            return "\n".join((lines[0], lines[1], lines[3]))

        @staticmethod
        def _about_icon_label() -> Any:
            """「关于」对话框里的 64×64 图标；**拿不到图标就返回 None**（调用方不加这个控件）。

            图标只有 256/128/48/32/16 这几档：256 那份是"详细版"（白 A + 红色上扬折线），
            这里平滑缩到 64 显示（`icon_png(64)` 会先找 `icon-64.png`，没有就退回主图）。
            """
            try:
                path = assets.icon_png(ABOUT_ICON_SIZE)
            except Exception as exc:  # noqa: BLE001 - 资源层毛病不该让"关于"打不开
                logger.debug(f"关于页图标定位失败：{exc}")
                return None
            if not path:
                return None
            pixmap = QPixmap(str(path))
            if pixmap.isNull():          # 文件在但读不出来（截断/权限）：同样不放图标
                return None
            label = QLabel()
            label.setObjectName("aboutIcon")     # 名字留着：测试与将来排障按名字找
            label.setPixmap(pixmap.scaled(
                ABOUT_ICON_SIZE, ABOUT_ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            return label

        def on_about(self) -> None:
            """【关于】：图标 + 版本号 + 版权 + 数据来源，外加一键复制（用户不必自己敲版本）。"""
            if self.about_dialog is not None:
                # 连点两次不该堆出一摞窗口
                self.about_dialog.close()
            dialog = QDialog(self)
            dialog.setWindowTitle("关于 " + APP_NAME)
            layout = QVBoxLayout(dialog)
            lines = self.about_lines()
            for i, text in enumerate(lines):
                label = QLabel(text)          # 纯文本标签：版本号不需要做成链接
                label.setWordWrap(True)
                if i == 0:                    # 第一行是程序名，当标题用
                    font = label.font()
                    font.setBold(True)
                    label.setFont(font)
                layout.addWidget(label)
                if i == 0:
                    # 图标紧跟在程序名下面；没有图标就整段不加（不留一块空白）
                    self.about_icon = self._about_icon_label()
                    if self.about_icon is not None:
                        layout.addWidget(self.about_icon)

            buttons = QHBoxLayout()
            copy_btn = QPushButton("复制版本信息")
            copy_btn.clicked.connect(self.on_copy_version_info)
            buttons.addWidget(copy_btn)
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dialog.accept)
            buttons.addWidget(close_btn)
            buttons.addStretch(1)
            layout.addLayout(buttons)

            self.about_dialog = dialog
            self.about_copy_button = copy_btn
            dialog.show()        # 非模态：不挡住主窗口，也不会卡住自动化测试
            dialog.raise_()

        def on_copy_version_info(self) -> None:
            """把版本信息写进剪贴板（报障时粘贴，比自己照着对话框敲准得多）。"""
            QGuiApplication.clipboard().setText(self.version_info_text())
            self._toast("已复制版本信息")

        # ── 窗口/托盘 ──

        def _restore_window(self) -> None:
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _on_tray_activated(self, reason: Any) -> None:
            """托盘被点：左键单击 → 看"刚刚冒出来的提醒"，双击 → 直接开主窗口。

            单击的语义按用户习惯来：**浮窗还挂在屏幕上就先把浮窗抬起来**
            （他多半是想看刚才那条提醒），没有浮窗才开主窗口。
            两种都先停掉图标闪烁 —— 用户已经做出反应了，再闪就是吵。
            """
            reason_value = getattr(reason, "value", reason)
            trigger = QSystemTrayIcon.ActivationReason.Trigger.value
            double = QSystemTrayIcon.ActivationReason.DoubleClick.value
            if reason_value not in (trigger, double):
                return
            popup = self.alert_popup
            if reason_value == trigger and popup is not None and popup.isVisible():
                self._stop_alert_flash()
                popup.raise_()
                return
            self._stop_alert_flash()
            self._restore_window()

        def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            """主窗口被激活（用户点开了它）→ 立刻停止闪烁。"""
            try:
                if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()):
                    self._stop_alert_flash()
            except Exception as exc:  # noqa: BLE001 - 停闪失败不该影响窗口事件
                logger.debug(f"处理激活事件失败：{exc}")
            super().changeEvent(event)

        def _quit(self) -> None:
            try:
                self.scheduler.stop()
            except Exception:  # noqa: BLE001
                pass
            # 浮窗是**没有父窗口的顶层窗口**，不主动收掉会在退出后留一张空壳在屏幕上
            if self.alert_popup is not None:
                self.alert_popup.hide_popup()
            self.tray.hide()
            QApplication.quit()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
            """关闭窗口 = **隐藏到托盘**（盯盘工具要常驻后台），关窗时**不弹任何提示**。

            为什么 `event.ignore()` + `hide()` 而不是真的退出：盘中要一直盯，
            误点关闭就把一整天的提醒都丢了。所以关窗只是把窗口收起来，
            定时器、调度、托盘全都继续跑；想真正退出就用**托盘右键 →【退出】**。

            为什么**不再**弹那条"程序还在托盘里跑"的提示气泡（用户明确说不需要）：
            用户是**故意**关窗的，他当然知道程序还在跑 —— 再弹一次只是噪音，
            而且每次关窗都弹（没有"只弹一次"的说法，那反而是另一种猜谜）。
            顺带一个好处：关窗不再产生任何跨窗口的副作用，行为更好预测。
            """
            event.ignore()
            self.hide()


def run_gui(cfg: Config | None = None) -> int:
    """启动 GUI，返回进程退出码。"""
    if not QT_AVAILABLE:
        print("PySide6 不可用，无法启动图形界面。")
        print(f"原因：{_QT_ERROR}")
        print("可以改用命令行：python -m laoa_trader --cli --once")
        return 2
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关窗只是最小化到托盘
    _set_app_icon(app)                    # 任务栏/Alt-Tab 取的是应用图标，不设会退回 python 默认图标
    window = MainWindow(cfg)
    window.show()
    return int(app.exec())


def main(cfg: Config | None = None) -> int:
    """兼容入口。"""
    return run_gui(cfg)


__all__ = ["QT_AVAILABLE", "MainWindow", "run_gui", "main"]
