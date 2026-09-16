"""PySide6 主窗口。

保持简洁：能跑起来、四块信息（状态栏 / 股票池 / 持仓 / 盘中提醒）+ 三个按钮。

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
from laoa_trader.scheduler import Scheduler, data_gate, refresh_data, run_daily
from laoa_trader.strategy import rules as rules_mod
from laoa_trader.ui import theme as theme_mod

logger = get_logger(__name__)

#: 程序名 / 版权行 / 数据来源：「窗口标题」「关于」对话框、复制到剪贴板的版本信息
#: **共用这一份** —— 分发出去之后用户看到的版本信息必须处处一致，不能各写各的
APP_NAME = "老A选股助手"
COPYRIGHT_TEXT = "版权所有 © 2026 async-chen，保留所有权利。"
SOURCE_TEXT = ("数据来源：同花顺（fuyao.aicubes.cn）。"
               "本程序仅用于个人研究与学习，不构成任何投资建议。")

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
#: 高 760 而不是 720：现在多了一行 KPI 卡片区，720 在 150% 缩放的机器上正好压线。
WINDOW_PREFERRED_SIZE = (1120, 760)
#: 最小尺寸收到 760×540 —— 比"表格 9 列 + 概览三组条目"需要的宽度小得多，
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
#: 点位 / 涨跌幅两列的**轨道宽度取样串**：取"最宽的那种数字"量出列宽，
#: 让不同条目的两列落在同一条竖线上（`-88888.88` 覆盖负号 + 5 位整数 + 2 位小数，
#: 涨跌幅多一个 `%`）。取样串只用来量宽度，不当占位文本显示。
MARKET_VALUE_TRACK = "-88888.88"
MARKET_PCT_TRACK = "-888.88%"

#: 界面里唯一的三种字号层级：页面标题 +2、KPI 数值 +1 加粗、其余都是基准字号。
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

# ── 顶部状态区 ──
#: 瞬时消息（任务完成/失败、刚点过的动作）在状态栏停留多久（秒）。
#: 10 分钟：够用户看见，又不会永远盖住实时状态。
STATUS_MESSAGE_TTL = 600.0

#: 数据概况（`engine.summary()`）在界面层的缓存时长（秒）。
#: 为什么要缓存：它里面有全表 COUNT（行情表几百万行），而界面每 5 秒问一次 ——
#: 5 秒一轮地把整张表数一遍，用户看到的就是"卡"（下载/导入期间更明显）。
#: 30 秒足够：状态栏显示的是"有几只、多少行"这种量级信息，不需要秒级精确。
SUMMARY_TTL = 30.0
#: 右侧短标签的名字（顺序即显示顺序）。标签文本 = 名字 + 一个短值，
#: 每项 2~6 个字；没有值的项**整项隐藏**（例如没有持仓就不显示"持仓"）。
STATUS_TAGS: tuple[str, ...] = ("今日池子", "持仓", "下次选股", "盘中提醒")
#: 「盘中提醒」标签的三种取值（界面、测试、详情共用一份，不各写各的）
INTRADAY_PAUSED = "已暂停"
INTRADAY_IN_SESSION = "时段中"
INTRADAY_OUT_SESSION = "未在时段"
#: 顶部按钮文字（2~4 字）。
#: 前三个从 `laoa_trader.hints` 取 —— 非界面层（同步/自检/调度）的"指路"文案里
#: 也要引用这几个名字，写错就等于让用户去找一个不存在的按钮，所以只有一份定义。
BTN_PAUSE_TEXT = "暂停提醒"
BTN_RESUME_TEXT = "恢复提醒"
BTN_CHECK_TEXT = "检查盘面"
BTN_DETAILS_TEXT = "详情"
BTN_ABOUT_TEXT = "关于"

try:  # Qt 缺失时必须优雅降级（Linux 开发机、精简环境）
    from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import (
        QAction,
        QBrush,
        QColor,
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
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
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

    class MarketKpiCard(QFrame):
        """一个指标一张卡：标签小号灰字 + 数值大一号加粗。

        为什么不做成"一行长文本"：长文本靠自动换行折出来的行对不齐（数字有的在行尾、
        有的在行中），一眼就是"没排版"；一张卡一个指标之后卡片等宽等高、数值右对齐，
        扫一眼就能比大小，窄屏时也是卡片自己收缩而不是文字折行。

        属性：`name`（指标名）、`title_label`（小号灰标签）、`value_label`（大一号加粗数值）。
        取色只用 palette + 一条细边框，不引图片/图标（打包与 DPI 缩放才不会挑环境）。
        """

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self.setObjectName("marketKpiCard")
            self.setFrameShape(QFrame.Shape.StyledPanel)
            # 细边框圆角：与股票池卡片同一套做法（palette 取色，主题换了也不会撞色）
            self.setStyleSheet(
                "QFrame#marketKpiCard { border: 1px solid palette(mid);"
                " border-radius: 6px; }"
            )
            row = QHBoxLayout(self)
            row.setContentsMargins(10, 6, 10, 6)
            row.setSpacing(PAGE_SPACING)
            self.title_label = QLabel(name)
            # "小号灰字"靠**灰**与**不加粗**表达（字号层级只有三级，见 FONT_* 常量）
            self.title_label.setObjectName("marketKpiTitle")
            self.title_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
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
        """一个指数条目：`名称（灰） ｜ 点位 ｜ 涨跌幅`（后两列右对齐、**逐值**上色）。

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
            self.name_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
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

    class MarketGroupBox(QFrame):
        """一组指数 = 小标题 + 条目网格（**整组就是这一个控件**）。

        为什么要一整组一个控件：配置里没配这一组时，`setVisible(False)` 一下
        就能"连标题带网格"一起收掉，不会留一个空的"情绪："吊在那里。
        属性：`key`（结果字典里的键）、`grid`（QGridLayout）、`entries`（条目控件列表）、
        `placeholder_label`（配了这一组但没取到数时显示的 `—`）。
        """

        def __init__(self, label: str, key: str) -> None:
            super().__init__()
            self.label = label
            self.key = key
            self._codes: tuple = ()
            self._columns = 0
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

        def set_items(self, items: list[dict]) -> None:
            """按最新的条目列表更新网格：代码没变就只改文本/颜色，不重建控件。

            为什么不每次都重建：概览每分钟刷一次，重建会把焦点、选中与
            "鼠标正停在哪一项上"全部清掉；只在**代码列表真的变了**（换配置、加了一只）
            时重建，界面才稳。
            """
            codes = tuple(str(item.get("thscode") or "") for item in items)
            if codes != self._codes:
                self._codes = codes
                self.clear_entries()
                self.entries = [MarketEntry(item) for item in items]
                for column, entry in enumerate(self.entries):
                    self.grid.addWidget(entry, 0, column)
                self._columns = 0                 # 强制下一次重排列数
            for entry, item in zip(self.entries, items):
                entry.update_item(item)
            self.placeholder_label.setVisible(not items)

        def clear_entries(self) -> None:
            """把网格里的条目全部摘掉并销毁（换配置/换数据源时用）。"""
            for entry in self.entries:
                self.grid.removeWidget(entry)
                entry.setParent(None)
                entry.deleteLater()
            self.entries = []

        def set_columns(self, columns: int) -> None:
            """按列数把条目重排成网格（窄屏自动换行，绝不出现横向滚动）。"""
            if columns == self._columns:
                return
            self._columns = columns
            for index, entry in enumerate(self.entries):
                self.grid.removeWidget(entry)
                self.grid.addWidget(entry, index // columns, index % columns)
            for column in range(MARKET_MAX_COLUMNS):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)

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
            #: 状态栏的瞬时消息（任务完成/失败提示），与主状态分两层显示
            self._message = ""
            self._message_at = 0.0
            #: 这条消息是不是"进度回传"（下载中让位给会动的下载进度，见 `_compose_status`）
            self._message_is_progress = False
            #: 最近一次进度回传的"阶段 + 数量"（`下载 daily-k 81/181 MB`），补在下载进度后面
            self._progress_stage_text = ""
            #: 「状态详情」弹窗与它里面的文本（测试与"重复点详情"都要能拿到）
            self.status_dialog: Any = None
            self.status_details_text: Any = None
            self.status_details_cache = ""
            #: 池子表格的内容指纹（内容没变就不重建控件）
            self._pool_signature: tuple = ()
            #: 当前渲染出来的卡片（顺序与池子行一致）与当前视图（cards/table）
            self.pool_cards: list[Any] = []
            self._pool_view = "cards"
            #: 启动自检结果（三态）与首次向导
            self.preflight_result: dict | None = None
            self.wizard: Any = None
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
            #: 「关于」对话框（测试与"重复点关于"都要能拿到它）
            self.about_dialog: Any = None
            #: 「关于」里的图标标签（资源缺失时为 None）
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

            # 标题带版本号：用户报障第一句就是"我这是哪个版本"
            self.setWindowTitle(
                f"{APP_NAME} v{laoa_trader.__version__}（Windows 单机版 · 测试版）"
            )
            # 窗口图标用 256 那份（任务栏/Alt-Tab/标题栏都会取它）。
            # 拿不到图标就什么都不设，保持系统默认 —— 没有图标也要能用
            window_icon = _load_icon()
            if not window_icon.isNull():
                self.setWindowIcon(window_icon)
            self._apply_screen_geometry()
            self._build_ui()
            self._build_tray()
            self._start_scheduler()

            # 界面刷新定时器：状态栏 + 托盘消息 + 提醒列表
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

            # 顶部状态区：一行主状态 + 右侧短标签、按钮行、进度条（见 `_build_status_area`）
            layout.addWidget(self._build_status_area())

            # 四个表 + 五个页：池子 / 持仓 / 自选 / 提醒 / 设置
            self.tabs = QTabWidget()

            # 股票池页 = 【卡片/表格】切换按钮 + 空池提示 + 卡片视图 + 表格视图。
            # 两个视图**都留着**：卡片看得全，表格看得密，用户自己挑；切换只是 setVisible。
            self.pool_table = QTableWidget(0, 10)
            # 「来源」列把"策略组别"和"自选/策略+自选"合成一列（避免两列重复信息）；
            # 「涨停」列放在「备注」后面：今日涨停池里的连板数与涨停原因（不是涨停票就是空）
            self.pool_table.setHorizontalHeaderLabels(
                ["代码", "名称", "来源策略", "来源", "备注", "涨停", "热门行业", "分数",
                 "条件单参数", "复制"]
            )
            self._stretch(self.pool_table)
            # 概览放**第一个**（Qt 启动就显示第一个页签）：看盘第一眼要扫到，
            # 而且它是只读的一页，进来就能看，不需要先选股
            self.tabs.addTab(self._build_market_page(), "大盘概览")
            self.tabs.addTab(self._build_pool_page(), "股票池")

            self.position_table = QTableWidget(0, 7)
            # 「盈亏比例」紧跟在「成本」后面：成本和比例挨着看，"赚了几个点"一眼就出来
            # （以前这里没有这一列，比例只在状态栏/详情里）
            self.position_table.setHorizontalHeaderLabels(
                ["代码", "名称", "数量", "成本", "盈亏比例", "止损", "止盈"]
            )
            self._stretch(self.position_table)
            self.tabs.addTab(
                self._build_table_page(self._build_position_row(), self.position_table),
                "持仓",
            )

            self.alert_table = QTableWidget(0, 5)
            # 第 2 列是「标的」而不是「代码」：用户要求显示成 `名称（代码）`
            # （与推送文本同一格式），光看代码认不出是哪只票
            self.alert_table.setHorizontalHeaderLabels(["时间", "标的", "类型", "价格", "说明"])
            self._stretch(self.alert_table)
            self.watch_table = QTableWidget(0, 5)
            self.watch_table.setHorizontalHeaderLabels(
                ["代码", "名称", "备注", "状态", "是否已进池"]
            )
            self._stretch(self.watch_table)
            self.tabs.addTab(
                self._build_table_page(self._build_watch_row(), self.watch_table),
                "自选股",
            )

            self.tabs.addTab(self.alert_table, "盘中提醒")

            # 公式选股页：右边点按钮就能写公式（小白友好），左边编辑/校验/试算/保存。
            # 位置按用户点名的顺序：盘中提醒之后、设置之前。
            # `status_cb` 把这一页的"已保存/已加入选股"之类的一句话送到状态栏 ——
            # 这一页自己不弹提示框（不打断写公式），但用户在窗口任何位置都能看到结果
            self.formula_page = FormulaPage(self.cfg, status_cb=self._toast)
            self.tabs.addTab(self.formula_page, "公式选股")

            # 设置页：策略组自选 + 通知方式自选（都写回 config.toml，保留注释）
            self.tabs.addTab(self._build_settings_tab(), "设置")

            # stretch=1：多余的高度**全部给页签区**（页签里的表格/滚动区自己吸收高度变化），
            # 上面的状态栏、按钮行、进度条与页面内的页脚都拿固定高度。
            # 这样窗口高度变小的时候，是"列表少显示几行"，而不是"底部被切到屏幕外"。
            layout.addWidget(self.tabs, 1)

            self.setCentralWidget(central)

            # 启动时按配置决定股票池显示哪种视图（配置写错时 `Config` 已经归一成 cards）
            self._apply_pool_view(self.cfg.pool_view)

        def _build_status_area(self) -> Any:
            """顶部状态区：**一行主状态 + 右侧几个短标签**，下面是按钮行与进度条。

            为什么不再把 8 项用 `｜` 串成一大条：用户反馈"太啰嗦、很多词看不懂"。
            状态栏回答的是"**现在要我注意什么**"，只讲一件事；行数、股票数、补跑时间、
            引擎状态、日志路径这些"查得到就行"的东西全部挪进 tooltip 与【详情】弹窗
            （同一份多行文本，两处口径一致）。

            布局要点：
            - 主状态 `ElidedLabel`：单行、太长就省略（**不换行**），
              所以状态区高度恒定，永远不会把窗口顶高、把底部挤出屏幕；
            - 短标签：小号灰字、固定宽度、用布局间距分隔（**不用 `｜`**）；
            - 【详情】【关于】排在按钮行最右（页面级/全局操作放右边，符合习惯）。
            """
            area = QWidget()
            # 背景条（铺拉丝纹理的那一条）：顶部状态区 + 概览页页脚，见 theme.py
            area.setObjectName("statusArea")
            area_layout = QVBoxLayout(area)
            area_layout.setContentsMargins(0, 0, 0, 0)
            area_layout.setSpacing(6)

            # ── 第 1 行：主状态（左）+ 短标签（右）──
            row = QHBoxLayout()
            row.setSpacing(12)          # 标签之间也用间距分隔，不用竖线
            self.status_row_layout = row
            self.status_label = ElidedLabel("正在初始化…")
            # 主状态比正文大一号（层级只有三级：页面标题 +2 / 数值与主状态 +1 / 其余基准）
            self.status_label.setFont(
                _scaled_font(self.status_label.font(), FONT_VALUE_DELTA)
            )
            row.addWidget(self.status_label, 1)
            self.status_tags: dict[str, Any] = {}
            for name in STATUS_TAGS:
                tag = QLabel(name)
                tag.setObjectName("statusTag")                              # 小号灰字
                tag.setForegroundRole(QPalette.ColorRole.PlaceholderText)
                tag.setVisible(False)                                      # 没值就先不显示
                self.status_tags[name] = tag
                row.addWidget(tag, 0)
            area_layout.addLayout(row)

            # ── 第 2 行：按钮（文字 2~4 字，完整说明放 tooltip）──
            buttons = QHBoxLayout()
            buttons.setSpacing(PAGE_SPACING)
            self.top_buttons_layout = buttons
            self.btn_download = QPushButton(BTN_DOWNLOAD_TEXT)
            self.btn_download.setToolTip(
                "下载 / 更新历史数据：首次建库或补历史行情（可中断，下次接着传）"
            )
            self.btn_download.clicked.connect(self.on_download)
            buttons.addWidget(self.btn_download)

            # 【选股建池】：跑完整流程（增量数据 → 策略 → 建池 → 按通知设置推送）
            self.btn_run = QPushButton(BTN_RUN_TEXT)
            # 银色主题里的"主操作按钮"用 objectName 精确命中（见 theme.py 的
            # `QPushButton#primaryAction`）—— 不靠按钮顺序这种位置关系
            self.btn_run.setObjectName("primaryAction")
            self.btn_run.setToolTip(
                "立即选股并建池：增量数据 → 跑策略 → 建池 → 按「设置」里的通知方式推送"
            )
            self.btn_run.clicked.connect(self.on_run_pipeline)
            buttons.addWidget(self.btn_run)

            # 【刷新数据】：只补数据（行情/涨停池/日历/行业/指数），不选股、不推送
            self.btn_refresh = QPushButton(BTN_REFRESH_TEXT)
            self.btn_refresh.setToolTip(
                "只刷新数据：补行情 / 涨停池 / 交易日历 / 行业 / 指数，不选股、不推送"
            )
            self.btn_refresh.clicked.connect(self.on_refresh_data)
            buttons.addWidget(self.btn_refresh)

            self.btn_pause = QPushButton(BTN_PAUSE_TEXT)
            self.btn_pause.setToolTip("暂停盘中提醒（日更照跑）；再点一次恢复")
            self.btn_pause.clicked.connect(self.on_toggle_intraday)
            buttons.addWidget(self.btn_pause)

            self.btn_check = QPushButton(BTN_CHECK_TEXT)
            self.btn_check.setToolTip("立即检查盘面：按当前池子 / 持仓 / 自选跑一次盘中提醒")
            self.btn_check.clicked.connect(self.on_intraday_once)
            buttons.addWidget(self.btn_check)

            buttons.addStretch(1)
            # 【详情】与【关于】放最右：一个回答"这些数是什么"，一个放版本与版权
            self.btn_details = QPushButton(BTN_DETAILS_TEXT)
            self.btn_details.setToolTip(
                "状态详情：本地行数、股票数、补跑时间、日志文件、自检阈值（可复制）"
            )
            self.btn_details.clicked.connect(self.on_show_status_details)
            buttons.addWidget(self.btn_details)
            self.btn_about = QPushButton(BTN_ABOUT_TEXT)
            self.btn_about.setToolTip("版本号 / 作者 / 版权 / 数据来源（报障时先看这里）")
            self.btn_about.clicked.connect(self.on_about)
            buttons.addWidget(self.btn_about)
            area_layout.addLayout(buttons)

            # ── 第 3 行：进度条（下载/同步/选股时的进度）──
            self.progress = QProgressBar()
            self.progress.setValue(0)
            area_layout.addWidget(self.progress)
            self.status_area = area
            return area

        def _build_table_page(self, row: Any, table: Any) -> Any:
            """把"操作行 + 表格"装进同一个页签。

            为什么要拆页：这两行原来是挂在**主窗口底部**的，切到「设置」页也照样看得见，
            用户反馈"不知道在给哪一页录入"。输入行跟着它影响的表格走，视线不用来回跳。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            # 统一间距（只动边距与间距，不动结构、不动表格列）
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)
            layout.addLayout(row)        # 输入行在上：先填再点，符合操作顺序
            layout.addWidget(table, 1)   # 表格吃掉多余高度（窗口变矮时少显示几行，不被切掉）
            return page

        def _build_position_row(self) -> Any:
            """持仓操作行（放在持仓页顶部）。"""
            row = QHBoxLayout()
            self.pos_symbol = QLineEdit()
            self.pos_symbol.setPlaceholderText("代码（6 位）")
            # 回车 = 点【添加持仓】。录持仓是"填完就想确认"的动作，
            # 不该逼用户把鼠标挪到按钮上（手不离键盘更快，也不容易点错行）
            self.pos_symbol.returnPressed.connect(self.on_add_position)
            self.pos_qty = QLineEdit()
            self.pos_qty.setPlaceholderText("数量（股）")
            self.pos_cost = QLineEdit()
            self.pos_cost.setPlaceholderText("成本价")
            btn_add = QPushButton("添加持仓")
            btn_add.clicked.connect(self.on_add_position)
            btn_del = QPushButton("删除持仓")
            btn_del.clicked.connect(self.on_delete_position)
            for widget in (self.pos_symbol, self.pos_qty, self.pos_cost):
                row.addWidget(widget)
            row.addWidget(btn_add)
            row.addWidget(btn_del)
            row.addStretch(1)
            return row

        def _build_watch_row(self) -> Any:
            """自选股操作行（代码 / 备注 / 添加·删除·启用·停用，放在自选股页顶部）。"""
            row = QHBoxLayout()
            self.watch_symbol = QLineEdit()
            self.watch_symbol.setPlaceholderText("代码（6 位）")
            # 回车 = 点【加自选】：加自选常常是"看到一只就敲代码回车"的连击动作
            self.watch_symbol.returnPressed.connect(self.on_watch_add)
            self.watch_note = QLineEdit()
            self.watch_note.setPlaceholderText("备注（例如 龙头、消息面）")
            btn_watch_add = QPushButton("加自选")
            btn_watch_add.clicked.connect(self.on_watch_add)
            btn_watch_del = QPushButton("删除自选")
            btn_watch_del.clicked.connect(self.on_watch_remove)
            btn_watch_on = QPushButton("启用")
            btn_watch_on.clicked.connect(lambda: self.on_watch_toggle(True))
            btn_watch_off = QPushButton("停用")
            btn_watch_off.clicked.connect(lambda: self.on_watch_toggle(False))
            for widget in (self.watch_symbol, self.watch_note):
                row.addWidget(widget)
            for btn in (btn_watch_add, btn_watch_del, btn_watch_on, btn_watch_off):
                row.addWidget(btn)
            row.addStretch(1)
            return row

        # ── 大盘概览页（独立 tab；数据口径见 market.py）──

        #: KPI 区的排布：前两行各三张卡（涨跌停家数 / 涨跌家数），成交额那张**跨两行**贴在右侧。
        #: 常量写在这里而不是散在方法里：测试与将来调整都只改这一处。
        MARKET_KPI_ROWS = (("涨停", "跌停", "炸板"), ("上涨", "下跌", "平盘"))
        MARKET_KPI_AMOUNT = "成交额"

        def _build_market_page(self) -> Any:
            """「大盘概览」页：标题行 / KPI 卡片区 / 三组指数 / 单行页脚（含【立即刷新】）。

            为什么整页重排：原来一页是"五行自动换行的长文本"，折行位置随窗口宽度变，
            折出来的行头参差不齐，一眼就是"没排版"。现在改成**分区 + 网格**：
            KPI 一张卡一个指标（等宽等高、数值右对齐），三组指数各一个小标题 + 条目网格
            （每个指数一个条目控件，名称/点位/涨跌幅三列对齐），页脚只占一行。

            结构（也是测试的断言对象）：
                page ─┬─ 标题行（页面标题 + 右侧【立即刷新】）
                      ├─ market_scroll（可伸缩：窗口变矮时它自己滚动，页面底部那行不会被挤出去）
                      │    └─ KPI 网格 + 宽基/情绪/板块三组
                      ├─ market_hint（**只在出错时出现**，算"第二行"）
                      └─ market_footer（一行：数据来源小字 + 右对齐【立即刷新】）

            为什么内容区套滚动区：概览的条目数由配置决定（可能十几项），
            如果不给滚动区，窗口一矮就是"页面被切掉"；给滚动区之后，
            可伸缩的部分自己吸收高度变化，**页脚与窗口底部永远在屏幕内**。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            header = QHBoxLayout()
            header.setSpacing(PAGE_SPACING)
            self.market_title = QLabel("大盘概览")
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
            content_layout.addWidget(self._build_market_kpi_grid())
            #: `宽基/情绪/板块` → 整组容器（"整组隐藏"就靠它，见 MarketGroupBox）
            self.market_group_boxes: dict[str, Any] = {}
            for _, label, key in market.GROUPS:
                box = MarketGroupBox(label, key)
                self.market_group_boxes[label] = box
                content_layout.addWidget(box)
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
            #: 每个指数的条目控件（`thscode` / `name_label` / `value_label` / `pct_label`）
            self.market_entries: list[Any] = []
            self._market_columns = 0
            self._render_market_overview()   # 先画空骨架：第一秒就是"能看"的样子
            return page

        def _build_market_kpi_grid(self) -> Any:
            """KPI 区：**每个指标一个独立小控件**（等宽等高，用 QGridLayout 排）。

            为什么一个指标一个控件：原来"涨停 55 · 跌停 16 · 炸板 30"是一长串文本，
            窗口一窄就自动换行，折出来的行头和上一行的数字对不齐；拆成等宽卡片之后
            每张卡里是"小号灰标签 + 大一号加粗数值（右对齐）"，
            涨跌停三张、涨跌家数三张各自成行，成交额那张跨两行贴在右侧 —— 整齐且能比大小。
            """
            frame = QFrame()
            frame.setObjectName("marketKpiArea")
            grid = QGridLayout(frame)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(PAGE_SPACING)
            #: 指标名 → 卡片控件（`title_label` / `value_label` 都挂在卡片上）
            self.market_kpi_cards: dict[str, Any] = {}
            #: 指标名 → **数值标签**（断言文本最直接的一层）
            self.market_kpis: dict[str, Any] = {}
            for row_index, names in enumerate(self.MARKET_KPI_ROWS):
                for column, name in enumerate(names):
                    card = MarketKpiCard(name)
                    self.market_kpi_cards[name] = card
                    self.market_kpis[name] = card.value_label
                    grid.addWidget(card, row_index, column)
                    grid.setColumnStretch(column, 1)
            amount_column = len(self.MARKET_KPI_ROWS[0])
            amount = MarketKpiCard(self.MARKET_KPI_AMOUNT)
            self.market_kpi_cards[self.MARKET_KPI_AMOUNT] = amount
            self.market_kpis[self.MARKET_KPI_AMOUNT] = amount.value_label
            # 成交额**单独占一行、跨满所有列**（原来挤在第 4 列里跨两行）：
            # 它的文本是"沪 7793亿 · 深 8499亿 · 北 140亿"，在 Windows 的字体
            # （微软雅黑比 Linux 上那套宽）下第 4 列放不下、数值会被截断；
            # 跨整行之后不管字体多宽都够用，版式也仍然是"三列 + 一行"的规整样子。
            grid.addWidget(amount, len(self.MARKET_KPI_ROWS), 0, 1, amount_column)
            self.market_kpi_grid = grid
            return frame

        def _apply_market_columns(self) -> None:
            """按可用宽度决定每组每行放几个条目（`clamp(可用宽 // 260, 1, 5)`）。

            为什么要响应式列数：用户要求"不出现横向滚动条或被截断"。
            窗口变窄时列数自动从 5 降到 3/2/1，条目自己往下排（纵向滚动），
            而不是把第三列挤没或者让文字被裁掉。
            """
            scroll = getattr(self, "market_scroll", None)
            boxes = getattr(self, "market_group_boxes", None)
            if scroll is None or not boxes:
                return
            # 用**滚动区**的宽度而不是视口宽度：视口宽度会随着纵向滚动条出现而少十几像素，
            # 拿它算列数容易出现"滚动条一出现列数就掉一档"的抖动
            width = max(scroll.width(), self.market_page.width()) - 2 * PAGE_MARGINS[0]
            columns = max(1, min(MARKET_MAX_COLUMNS, width // MARKET_ENTRY_MIN_WIDTH))
            if columns == self._market_columns:
                return
            self._market_columns = columns
            for box in boxes.values():
                box.set_columns(columns)

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
            worker = Worker(market.fetch_overview, self.cfg, force=force)
            self._market_worker = worker
            worker.finished_ok.connect(self._on_market_overview_ready)
            worker.failed.connect(self._on_market_overview_failed)
            worker.start()

        def _on_market_overview_ready(self, overview: Any) -> None:
            """后台取回来了（回主线程执行）：记下结果并重画页面。"""
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
            """**同步**取一次大盘概览并刷到页面上（`force=True` 忽略 TTL 缓存）。

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
            try:
                overview = market.fetch_overview(self.cfg, client=client, force=force)
            except Exception as exc:  # noqa: BLE001 - market 层已"绝不抛"，这里再兜一层
                logger.warning(f"大盘概览取数异常：{type(exc).__name__}: {exc}")
                overview = None
            self.market_overview = overview
            self._render_market_overview()
            return overview

        def _render_market_overview(self) -> None:
            """把 `self.market_overview` 画到页面上（KPI 卡片 + 三组条目 + 单行来源页脚）。

            三件事：
            - **KPI**：一个指标一张卡的数值文本（`market.kpi_values()`，取数口径与命令行同一份）；
            - **三组条目**：每个指数一个条目控件，点位与涨跌幅**各按自己的涨跌上色**
              （涨=红、跌=绿、平/缺=默认色，色值只在 `market.py` 里定义一次）；
            - **空组**：配置里没这一组 → **整组连标题一起隐藏**，不留一个空的"情绪："。

            用 `setStyleSheet("color:…")` 上色而不是富文本 HTML：`text()` 里带标签会让断言
            变脆，而这里只改前景色，样式表是最直接的一层。
            """
            overview = self.market_overview
            values = market.kpi_values(overview)
            for name, label in self.market_kpis.items():
                label.setText(values.get(name, market.DASH))

            configured = set((overview or {}).get("configured_groups") or [])
            entries: list[Any] = []
            for _, group_label, key in market.GROUPS:
                box = self.market_group_boxes[group_label]
                # 没配这一组 → 整组隐藏（连标题），而不是留一个空标题在那吊着
                box.setVisible(key in configured)
                box.set_items(list((overview or {}).get(key) or []))
                if self._market_columns:
                    # `set_items` 重建条目后列数会归零：这里按当前列数重排一次，
                    # 否则新建出来的条目会全挤在同一行上（右边被裁掉）
                    box.set_columns(self._market_columns)
                entries.extend(box.entries)
            self.market_entries = entries

            self.market_as_of_label.setFullText(market.footer_text(overview))

            # 原因写进 tooltip（鼠标一停就能看到）与 hint（只在出错时出现，算"第二行"）
            detail = market.summary_text(overview)
            tooltip = ("大盘概览：" + detail) if detail else "大盘概览：暂无数据"
            for widget in [*self.market_kpi_cards.values(), *self.market_group_boxes.values()]:
                widget.setToolTip(tooltip)
            self.market_page.setToolTip(tooltip)
            errors = [str(e) for e in ((overview or {}).get("errors") or [])]
            if errors:
                text = "；".join(errors[:2]) + ("…" if len(errors) > 2 else "")
                self.market_hint.setText("⚠️ " + text)
                self.market_hint.setToolTip("；".join(errors))
            else:
                self.market_hint.setText("")
            self.market_hint.setVisible(bool(errors))

            # 数据到位之后按当前宽度再排一次列数（首屏宽度可能与建好时不同）
            self._apply_market_columns()

        def _build_pool_page(self) -> Any:
            """股票池页：切换按钮 + 空池提示 + 卡片视图（滚动区）+ 表格视图。

            为什么两种视图都留着：卡片把"来源/行业/备注/条件单参数"这些长短不一的文字
            完整显示出来，池子十几只时最好用；但表格一行一只、能一眼横扫，
            池子大或想比对分数时更顺手 —— 这是两种习惯，不该替用户二选一。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            # 统一间距（只动边距与间距：结构、卡片布局、表格列都不动）
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            view_row = QHBoxLayout()
            self.btn_pool_view = QPushButton("切换为表格")
            self.btn_pool_view.setToolTip("在「卡片」与「表格」两种股票池视图之间切换（会记住选择）")
            self.btn_pool_view.clicked.connect(self.on_toggle_pool_view)
            view_row.addWidget(self.btn_pool_view)
            view_row.addStretch(1)
            layout.addLayout(view_row)

            # 空池提示放在两个视图**之外**：这样切到表格视图时它照样显示/隐藏
            self.pool_empty_label = QLabel(
                "今日没有入选标的（收盘后自动选股，或点【选股建池】）"
            )
            self.pool_empty_label.setWordWrap(True)
            layout.addWidget(self.pool_empty_label)

            self.pool_scroll = QScrollArea()
            self.pool_scroll.setWidgetResizable(True)     # 卡片宽度跟着窗口走，不出现横向滚动
            self.pool_scroll.setFrameShape(QFrame.Shape.NoFrame)
            container = QWidget()
            self.pool_cards_layout = QVBoxLayout(container)
            self.pool_cards_layout.setSpacing(6)
            self.pool_cards_layout.addStretch(1)          # 卡片往上靠，不撑满整页
            self.pool_scroll.setWidget(container)
            layout.addWidget(self.pool_scroll, 1)     # 卡片区吃掉多余高度

            layout.addWidget(self.pool_table)
            return page

        def _build_pool_card(self, row: dict, price: float | None) -> Any:
            """一只股票一张卡片。

            卡片上的字段都挂成**同名属性**（symbol/name/label/.../copy_button）：
            测试按属性断言，将来做"点卡片看详情"也按属性取值，不用去爬控件层级。
            """
            card = QFrame()
            card.setObjectName("poolCard")
            card.setFrameShape(QFrame.Shape.StyledPanel)
            # 只用调色板取色 + 细边框圆角：不引入图片/图标等外部资源（打包分发才不挑环境）
            card.setStyleSheet(
                "QFrame#poolCard { border: 1px solid palette(mid); border-radius: 6px; }"
            )
            # 高度按内容自适（3 行文字 + 按钮），宽度随滚动区拉伸 —— 别每张卡占半屏
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

            card.symbol = str(row.get("symbol") or "")
            card.name = str(row.get("name") or "")
            card.label = str(row.get("label") or "")
            # 「（依赖开盘）」标记：卡片上也要看得见（正 α 只在开盘买口径下的提示）
            card.evidence_text = str(row.get("evidence_text") or "")
            card.label_with_evidence = f"{card.label}{card.evidence_text}"
            card.source_label = str(row.get("source_label") or "")
            card.note = str(row.get("note") or "")
            card.industry = str(row.get("industry") or "")
            card.score_text = _fmt_float(row.get("score"), 3)
            card.plan_text = self._plan_summary(row, price)

            layout = QVBoxLayout(card)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(2)

            # 第 1 行：代码 + 名称（加粗稍大，一眼能认出是哪只）｜右侧：来源策略 + 分数
            head = QHBoxLayout()
            title = QLabel(f"{card.symbol} {card.name}".strip())
            font = title.font()
            font.setBold(True)
            if font.pointSize() > 0:      # 字号为像素指定时（-1）不能加减，跳过加粗就够了
                font.setPointSize(font.pointSize() + 1)
            title.setFont(font)
            head.addWidget(title)
            head.addStretch(1)
            if card.label:
                label = QLabel(card.label_with_evidence)
                if card.evidence_text:
                    label.setToolTip(pool_mod.open_only_tooltip(card.label))
                head.addWidget(label)
            if row.get("score") is not None:
                # 没分数就不显示 —— 表格里那种"空单元格 + 一个 —"在卡片上很难看
                head.addWidget(QLabel(f"分数 {card.score_text}"))
            layout.addLayout(head)

            # 第 1.5 行：竞价强度（只在竞价窗口内有数据）+ 涨停信息（不是涨停票就没有）
            # 两行都放在最上面：竞价是当天最早的信号，涨停原因是"为什么涨"的答案
            card.auction_text = self._auction_text(str(row.get("symbol") or ""))
            card.auction_label = QLabel(card.auction_text)
            auction_font = card.auction_label.font()
            auction_font.setBold(True)
            card.auction_label.setFont(auction_font)
            card.auction_label.setVisible(bool(card.auction_text))
            layout.addWidget(card.auction_label)

            card.limit_up_text = pool_mod.limit_up_text(row)
            card.limit_up_label = QLabel(
                f"涨停：{card.limit_up_text}" if card.limit_up_text else ""
            )
            card.limit_up_label.setVisible(bool(card.limit_up_text))
            layout.addWidget(card.limit_up_label)

            # 第 2 行：来源 + 热门行业 + 备注（各段为空就整段不出现，不留空标签）
            meta = QHBoxLayout()
            if card.source_label:
                meta.addWidget(QLabel(card.source_label))
            if card.industry:
                meta.addWidget(QLabel(f"热门行业：{card.industry}"))
            if card.note:
                meta.addWidget(QLabel(f"备注：{card.note}"))
            meta.addStretch(1)
            layout.addLayout(meta)

            # 第 3 行：条件单参数 + 右下角【复制条件单】
            plan = QHBoxLayout()
            plan.addWidget(QLabel(card.plan_text))
            plan.addStretch(1)
            card.copy_button = QPushButton("复制条件单")
            card.copy_button.clicked.connect(
                lambda _=False, r=row, p=price: self._copy_plan(r, p)
            )
            plan.addWidget(card.copy_button)
            layout.addLayout(plan)

            # 卡片属性里留一份名称标签的引用：加粗/字号这类"看得见"的要求要测得到
            card.title_label = title
            return card

        def _build_settings_tab(self) -> Any:
            """设置页：策略组自选 + 通知方式自选 + 保存 + 发送测试提醒。

            为什么要做这一页：Windows 用户不该为了"只做隔日"或"关掉飞书"
            去手改 TOML。保存时**只就地改那几个键**，用户自己写的注释与
            未知键全部保留（见 `config.render_config_updates`）。

            为什么整页套一层滚动区（**只加容器，不动任何一项的顺序与层级**）：
            这一页控件最多（20 多个勾选框 + 若干输入框），它们的"最小高度"合起来有
            750 像素以上；页签的最小高度取所有页的最大值，于是**整窗的最小高度**
            被这一页顶到 887 像素 —— 1366×768 的笔记本（可用高约 680）上窗口缩不小，
            底边直接被屏幕切掉（用户反馈的"最下边看不见"）。套上滚动区之后，
            页面能跟着窗口收缩，够不到的那几项滚动一下就看到了。
            """
            from laoa_trader.strategy import groups as groups_mod

            inner = QWidget()
            layout = QVBoxLayout(inner)
            # 统一间距（只动边距与间距）
            layout.setContentsMargins(*PAGE_MARGINS)
            layout.setSpacing(PAGE_SPACING)

            # ── 界面主题（外观偏好：改完立即生效，不用重启）──
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
            layout.addLayout(theme_row)
            theme_hint = QLabel("换主题立即生效（不用重启），选择会写回 config.toml")
            theme_hint.setObjectName("statusTag")     # 小号灰字（与状态区同一个样式）
            layout.addWidget(theme_hint)

            # ── 自动运行（分发后每个人自己设；保存后立即生效，不用重启）──
            layout.addWidget(QLabel("自动运行（每天几点自动跑「数据增量 + 选股建池 + 通知」）"))
            run_row = QHBoxLayout()
            self.auto_run_box = QCheckBox("每天自动运行")
            self.auto_run_box.setChecked(bool(self.cfg.auto_run))
            self.auto_run_box.setToolTip("关掉则只在点【选股建池】时跑")
            run_row.addWidget(self.auto_run_box)
            run_row.addWidget(QLabel("主跑时间："))
            self.run_at_edit = QLineEdit(self.cfg.run_at)
            self.run_at_edit.setPlaceholderText("HH:MM，例如 16:00")
            # 宽度按**当前字体**量出来（"HH:MM" 五个字符 + 内边距），不写死像素：
            # 用户把 Windows 的"文本大小"调大之后，写死的 80 会把时间截掉一半
            self.run_at_edit.setFixedWidth(
                self.run_at_edit.fontMetrics().horizontalAdvance("00:00") + 24
            )
            run_row.addWidget(self.run_at_edit)
            run_row.addWidget(QLabel("补跑时间："))
            self.run_at_fallback_edit = QLineEdit(getattr(self.cfg, "run_at_fallback", ""))
            self.run_at_fallback_edit.setPlaceholderText("HH:MM，例如 19:15")
            self.run_at_fallback_edit.setFixedWidth(
                self.run_at_fallback_edit.fontMetrics().horizontalAdvance("00:00") + 24
            )
            run_row.addWidget(self.run_at_fallback_edit)
            run_row.addStretch(1)
            layout.addLayout(run_row)
            hint = QLabel(
                "主跑没成功时，到补跑时间再试一次（补跑必须晚于主跑）。"
                "建议主跑设在 16:00 之后 —— A 股 15:00 收盘，收盘后当天数据才齐。"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.save_run_button = QPushButton("保存自动运行设置")
            self.save_run_button.clicked.connect(self.on_save_run_at)
            layout.addWidget(self.save_run_button)
            self.run_hint = QLabel("")
            self.run_hint.setWordWrap(True)
            layout.addWidget(self.run_hint)
            layout.addWidget(QLabel("─" * 60))

            # ── 策略组 ──
            layout.addWidget(QLabel("策略组（决定跑哪些策略；改动保存后立即生效）"))
            self.group_boxes: dict[str, Any] = {}
            self.group_reason_labels: dict[str, Any] = {}
            for key in groups_mod.GROUP_ORDER:
                group = groups_mod.GROUPS[key]
                box = QCheckBox(
                    f"{group.label}（T+{group.horizon}）—— {group.note}"
                )
                box.setChecked(key in (self.cfg.enabled_groups or []))
                if group.disabled_reason:
                    # 停用的组：**默认不勾**，并把"为什么停用"直接写在勾选项的
                    # tooltip 与小字说明里 —— 用户不该为了搞清"这组怎么没了"去翻文档
                    box.setToolTip(f"默认停用：{group.disabled_reason}")
                    self.group_boxes[key] = box
                    layout.addWidget(box)
                    note = QLabel(f"　　⛔ 默认停用：{group.disabled_reason}")
                    note.setObjectName("statusTag")      # 小号灰字（与状态区同一档样式）
                    note.setWordWrap(True)
                    self.group_reason_labels[key] = note
                    layout.addWidget(note)
                    continue
                self.group_boxes[key] = box
                layout.addWidget(box)

            enabled_now = set(self.cfg.enabled_strategies or [])
            layout.addWidget(QLabel(
                "成员策略（勾选 = 只跑这些；全不勾 = 该组全选。当前仅对勾选生效）"
            ))
            self.strategy_boxes: dict[str, Any] = {}
            for key in groups_mod.GROUP_ORDER:
                group = groups_mod.GROUPS[key]
                for class_name, weight in group.members:
                    cls = rules_mod.STRATEGIES.get(class_name)
                    note = str(getattr(cls, "evidence_note", "") or "")
                    box = QCheckBox(
                        f"　{group.label} / {rules_mod.strategy_label(class_name)}"
                        f"（权重 {weight}）" + ("　⚠️ 依赖开盘" if note else "")
                    )
                    if note:
                        # 依赖开盘的策略：**可勾选、照常推送**，把两套口径的数字放在提示里
                        # —— 判断权交给用户，程序只负责把数据摆在眼前
                        box.setToolTip(note + "\n（标的照常推送；想只看「两套口径都为正」的"
                                       "标的，把 config.toml 的 push_only_proven 设成 true）")
                    box.setChecked(class_name in enabled_now)
                    self.strategy_boxes[class_name] = box
                    layout.addWidget(box)
            self.save_groups_button = QPushButton("保存策略组设置")
            self.save_groups_button.clicked.connect(self.on_save_groups)
            layout.addWidget(self.save_groups_button)

            # 自定义公式是**第四个组**（「公式」），成员由用户在「公式选股」页勾选，
            # 所以它不在这张勾选表里 —— 不写一句的话，用户会以为"公式不算策略组"
            formula_note = QLabel(
                "　　另：你自己写的公式是「公式」组（与上面三组并列），"
                "在【公式选股】页勾「参与选股」才生效，默认不参与。"
            )
            formula_note.setObjectName("statusTag")     # 小号灰字
            formula_note.setWordWrap(True)
            layout.addWidget(formula_note)

            layout.addWidget(QLabel("─" * 60))

            # ── 通知方式 ──
            layout.addWidget(QLabel(
                "通知方式（可任选；都不勾 = 只入库不推送。飞书需先填凭证）"
            ))
            chosen = {str(c).lower() for c in (self.cfg.notify_channels or [])}
            self.channel_boxes: dict[str, Any] = {}
            labels = {"windows": "Windows 原生弹窗", "feishu": "飞书卡片", "tray": "托盘气泡"}
            for name in KINDS:
                box = QCheckBox(labels.get(name, name))
                box.setChecked(name in chosen)
                self.channel_boxes[name] = box
                layout.addWidget(box)

            # windows 参数
            self.win_sound_box = QCheckBox("弹窗带提示音")
            self.win_sound_box.setChecked(bool(self.cfg.notify_windows_sound))
            self.win_url_box = QCheckBox("弹窗按钮点击打开雪球")
            self.win_url_box.setChecked(bool(self.cfg.notify_windows_open_url))
            layout.addWidget(self.win_sound_box)
            layout.addWidget(self.win_url_box)

            # 托盘参数
            tray_row = QHBoxLayout()
            tray_row.addWidget(QLabel("托盘气泡显示时长（毫秒）："))
            self.tray_duration = QSpinBox()
            self.tray_duration.setRange(1000, 60000)
            self.tray_duration.setSingleStep(1000)
            self.tray_duration.setValue(int(self.cfg.notify_tray_duration_ms))
            tray_row.addWidget(self.tray_duration)
            tray_row.addStretch(1)
            layout.addLayout(tray_row)

            # 飞书参数
            self.feishu_on_box = QCheckBox("启用飞书频道（feishu_on）")
            self.feishu_on_box.setChecked(bool(self.cfg.feishu_on))
            layout.addWidget(self.feishu_on_box)
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
            layout.addLayout(feishu_row)

            self.feishu_hint = QLabel("")
            self.feishu_hint.setWordWrap(True)
            layout.addWidget(self.feishu_hint)

            row = QHBoxLayout()
            self.save_notify_button = QPushButton("保存通知设置")
            self.save_notify_button.clicked.connect(self.on_save_notify)
            row.addWidget(self.save_notify_button)
            self.btn_test = QPushButton("发送测试提醒")
            self.btn_test.clicked.connect(self.on_test_notify)
            row.addWidget(self.btn_test)
            row.addStretch(1)
            layout.addLayout(row)

            layout.addWidget(QLabel("─" * 60))

            # ── 竞价扫描（全市场扫描 + 过滤规则）──
            #
            # 为什么把这一组放在界面上：这一版竞价是"**全市场扫一遍再按规则过滤**"，
            # 规则就是用户手里那几个数字（涨幅区间、板块、成交额、分数、条数、扫描时刻）——
            # 让他为了改一个 2.0% 去手改 TOML 是不合理的。
            layout.addWidget(QLabel(
                "竞价扫描（全市场，9:15–9:25 的真实买卖盘；默认关，勾上才取数）"
            ))
            self.auction_on_box = QCheckBox("启用竞价扫描（勾上后默认 09:20 / 09:25 各扫一次）")
            self.auction_on_box.setChecked(bool(getattr(self.cfg, "intraday_auction", False)))
            self.auction_on_box.setToolTip(
                "全市场约 5600 只、按 100 只一批 → 约 56 个请求/次（20~30 秒，后台线程跑）；\n"
                "所以只在下面两个时刻各扫一次，不是每分钟扫。"
            )
            layout.addWidget(self.auction_on_box)

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
            pct_row.addStretch(1)
            layout.addLayout(pct_row)

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
            layout.addLayout(board_row)

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
            layout.addLayout(score_row)

            self.auction_hint = QLabel("")
            self.auction_hint.setWordWrap(True)
            layout.addWidget(self.auction_hint)
            self.save_auction_button = QPushButton("保存竞价设置")
            self.save_auction_button.clicked.connect(self.on_save_auction)
            layout.addWidget(self.save_auction_button)

            layout.addStretch(1)
            self._refresh_channel_hints()
            self._refresh_run_hint()
            self._refresh_auction_hint()

            # 滚动区只是"外套"：里层控件、顺序、层级都与原来完全一致
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
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
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
            act_pool = QAction("立即选股并建池", self)
            act_pool.triggered.connect(self.on_run_pipeline)
            act_quit = QAction("退出", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_show)
            menu.addAction(act_recent)
            menu.addAction(act_pool)
            menu.addSeparator()
            menu.addAction(act_quit)
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
            - `needs_full` → 弹首次向导（填 Key/目录 → 开始下载 → 完成后自动跑一次选股建池）。

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
            """首次向导：说明原因 → 填/确认 API Key 与数据目录 → 开始下载 → 随后建池。

            非模态（show 而不是 exec）：用户可以先看看主界面，也不会卡住自动化测试。
            """
            from PySide6.QtWidgets import QDialog

            dialog = QDialog(self)
            dialog.setWindowTitle("首次运行 · 下载历史数据")
            dialog.resize(680, 420)
            layout = QVBoxLayout(dialog)
            # 导入年限（默认 3 年；0 表示不限制）
            years = float(getattr(self.cfg, "history_years", 3) or 0)
            layout.addWidget(QLabel("本地数据还不能用来选股："))
            reason = QLabel(f"· {result.get('reason')}")
            reason.setWordWrap(True)
            layout.addWidget(reason)
            layout.addWidget(QLabel(
                f"· 判据：需要 {self.cfg.min_history_years:g} 年历史（按 {years:g} 年导入）、"
                f"至少 {self.cfg.min_symbols} 只股票、有复权事件、行业覆盖 ≥90%、交易日历齐全"
            ))
            layout.addWidget(QLabel(
                f"点【开始下载】会拉取全市场日K 与复权事件，**导入最近 {years:g} 年**"
                f"（约 {int(round(years * 100))} 万行，预计 8~15 分钟；可中断，下次接着传）。"
                "\n想要 10 年（长样本回测）：把 config.toml 里的 history_years 改成 10 再下。"
            ))

            form = QHBoxLayout()
            form.addWidget(QLabel("同花顺 API Key："))
            self.wizard_key = QLineEdit(self.cfg.hithink_api_key)
            self.wizard_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.wizard_key.setPlaceholderText("在 fuyao.aicubes.cn/admin 获取")
            form.addWidget(self.wizard_key)
            layout.addLayout(form)

            dir_row = QHBoxLayout()
            dir_row.addWidget(QLabel("数据目录："))
            self.wizard_dir = QLineEdit(str(self.cfg.data_dir))
            dir_row.addWidget(self.wizard_dir)
            layout.addLayout(dir_row)

            self.wizard_progress = QProgressBar()
            layout.addWidget(self.wizard_progress)
            self.wizard_status = QLabel("尚未开始")
            self.wizard_status.setWordWrap(True)
            layout.addWidget(self.wizard_status)

            buttons = QHBoxLayout()
            self.wizard_start = QPushButton("开始下载")
            self.wizard_start.clicked.connect(self.on_wizard_start)
            buttons.addWidget(self.wizard_start)
            # 下载前 = "稍后再说"（关掉窗口，什么都不做）；
            # 下载中 = "取消下载"（协作式取消：已写入的数据保留，下次接着传）
            self.wizard_skip = QPushButton("稍后再说")
            self.wizard_skip.clicked.connect(self.on_wizard_cancel)
            buttons.addWidget(self.wizard_skip)
            buttons.addStretch(1)
            layout.addLayout(buttons)
            layout.addWidget(QLabel(
                "提示：下载完成后会自动跑一次选股建池；也可以之后点【选股建池】。"
            ))

            self.wizard = dialog
            dialog.show()          # 非模态：不阻塞界面
            dialog.raise_()

        def on_wizard_start(self) -> None:
            """向导里的【开始下载】：校验 Key → 保存配置 → 后台下载 → 完成后建池。"""
            from laoa_trader.config import save_settings

            key = self.wizard_key.text().strip()
            if not key:
                self.wizard_status.setText("❌ 请先填同花顺 API Key（没有 Key 无法下载数据）")
                return
            data_dir = self.wizard_dir.text().strip() or str(self.cfg.data_dir)
            try:
                save_settings(self.cfg, {"hithink_api_key": key, "data_dir": data_dir})
            except OSError as exc:
                self.wizard_status.setText(f"❌ 配置写入失败：{exc}（可手改 config.toml）")
                return
            self.engine = DataEngine(self.cfg.db_path)
            self.wizard_start.setEnabled(False)
            self._cancel_download = False
            self.wizard_skip.setText("取消下载")
            self.wizard_skip.setEnabled(True)
            self.wizard_status.setText("正在下载…（可点【取消下载】中断，下次接着传）")
            self._run_worker(
                lambda progress_cb, note_cb: sync.download_history(
                    self.cfg, progress_cb=progress_cb, note_cb=note_cb,
                    should_stop=lambda: self._cancel_download,
                ),
                "下载历史数据",
                with_progress=True, with_note=True,
            )

        def on_wizard_cancel(self) -> None:
            """向导按钮：没开始下载 → 关窗口；正在下载 → 协作式取消。"""
            if self.wizard_start.isEnabled():
                if self.wizard is not None:
                    self.wizard.close()
                return
            self._cancel_download = True
            self.wizard_skip.setEnabled(False)
            if self.wizard is not None:
                self.wizard_status.setText(
                    "正在取消…已下好的数据与已写入的行都会保留，下次点【开始下载】即续传。"
                )

        def _on_download_done(self, result: Any) -> None:
            """下载完成（成功/取消）后：向导收尾 + 自动跑一次选股建池。"""
            try:
                if self.wizard is not None:
                    self.wizard_start.setEnabled(True)
                    self.wizard_status.setText(
                        ("✅ " if getattr(result, "ok", False) else "⚠️ ")
                        + getattr(result, "message", str(result))
                    )
                    self.wizard_skip.setText("关闭")
                    if getattr(result, "ok", False):
                        self.wizard.close()
            except Exception:  # noqa: BLE001 - 向导可能已被关掉
                pass
            if getattr(result, "ok", False):
                # 数据变了 → 之前缓存的"needs_full"结论立刻作废，重算一次。
                # 这里**不**走 run_preflight()：那会在"还是不够用"时再弹一次向导，
                # 用户刚下完就被弹窗怼一脸不合适；只把结论写进状态栏。
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
                    return
                self._toast("下载完成，正在跑一次选股建池…")
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

            下载/导入期间**只刷"轻"的部分**（状态文案 + 托盘），
            重活（数据概况、四个表格）留到下载结束后一次性补齐 —— 见 `_heavy_refresh_allowed`。
            """
            try:
                heavy = self._heavy_refresh_allowed()
                # 「上一拍因为下载被跳过了，这一拍能刷了」→ 强制取一次新的数据概况，
                # 把下载/导入写进去的行数与只数立刻补上（其余时候由 30 秒 TTL 管）
                self._refresh_status(force_summary=heavy and self._heavy_paused)
                if heavy:
                    self._refresh_pool()
                    self._refresh_watchlist()
                    self._refresh_positions()
                    self._refresh_alerts()
                    if self._heavy_paused:
                        # 下载刚结束 → 完整刷一遍已经算过了（上面那些调用），这里只落标记
                        self._heavy_paused = False
                else:
                    # 记住"这次跳过了"：下载一结束的下一拍要把表格补上（不能一直空着）
                    self._heavy_paused = True
                # 概览**不在这里刷**：它有自己的 60 秒定时器与 55 秒 TTL
                # （见 `_market_tick`）—— 5 秒一轮会把配额刷掉
                # 新提醒的对账（响声 / 闪图标 / 浮窗）**不管轻重都做**：
                # 它只读最近 20 行，而且提醒不能因为"正在下载"就不响
                self._check_new_alerts()
                self._drain_tray()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"界面刷新异常：{exc}")

        def _refresh_status(self, *, force_summary: bool = False) -> None:
            """刷新整个状态区：主状态一句 + 右侧短标签 + tooltip 里的详情。

            三处要用同一批事实（池子几只、持仓多少），所以**只查一次库**再分发 ——
            5 秒一轮的刷新里查三遍同样的 SQL 是白费力气。
            数据概况走 TTL 缓存（下载期间直接用上一次的值），见 `_summary_cached`。
            """
            facts = self._status_facts(force_summary=force_summary)
            self.status_label.setText(self._compose_status(facts))
            self._refresh_status_tags(facts)
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
            """主状态：**只讲一件事**，按优先级取（用户要的"一行主状态"）。

            优先级：
            1. 瞬时消息（任务完成/失败、刚点过的动作，10 分钟内）；
            2. 正在下载 → `⏬ 正在下载历史数据 48%`；
            3. 今天该自动跑却被数据闸门拦下 / 上次出错 → 那句中文原因；
            4. 本地数据落后或不可用 → `⚠️ … · 点【下载数据】补齐`（逗号句，**不串 `｜`**）；
            5. 一切正常 → `✅ 数据就绪 · 5560 只 · 更新到 09-11`。

            为什么不再把 8 项串成一条：用户原话"太啰嗦、很多词看不懂"。
            行数 / 补跑时间 / 引擎状态 / 日志路径全部进 tooltip 与【详情】（`_status_details`）。
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
                return self._download_line(st)
            if message:
                return message
            reasons = [str(st.get("skipped_reason") or ""), str(st.get("last_error") or "")]
            if any(reasons):
                # 今天该自动跑却没跑 / 上次出错：这两句本来就是中文原因，直接说完
                return "⚠️ " + " · ".join(r for r in reasons if r)
            problem = self._data_problem_text()
            if problem:
                return problem
            return self._ready_text(facts)

        def _download_line(self, st: dict) -> str:
            """下载中的主状态：`⏬ 正在下载历史数据 48%`（带上当前阶段与 MB）。

            为什么要显式"正在下载"四个字：整件事可能十几分钟，屏幕上只挂一个百分比
            或者干脆什么都不显示，用户会以为程序卡死了（实测反馈过）。
            """
            value, maximum = self.progress.value(), self.progress.maximum()
            if maximum > 0:
                head = f"⏬ 正在下载历史数据 {value * 100 // maximum}%"
            else:
                head = "⏬ 正在下载历史数据…"
            if self._progress_stage_text:
                return f"{head} · {self._progress_stage_text}"
            return head

        def _data_problem_text(self) -> str:
            """数据不可用 / 落后时那一句："点哪个按钮能解决"；一切正常返回空串。

            为什么必须指路：用户看不懂"needs_full"这种内部说法，也不该去猜
            —— 直接告诉他按哪个按钮（按钮文字取自 `BTN_*` 常量，与按钮上的一模一样）。
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
                return f"⚠️ {result.get('reason') or hints.SYNC_LIGHT_HINT}"
            if status == preflight.NEEDS_INCREMENTAL:
                days = int(result.get("stale_trading_days") or 0)
                return f"⚠️ 本地数据落后 {days} 个交易日 · 点【{BTN_DOWNLOAD_TEXT}】补齐"
            return f"⚠️ 本地数据还没准备好 · 点【{BTN_DOWNLOAD_TEXT}】下载历史数据"

        def _ready_text(self, facts: dict) -> str:
            """正常状态：`✅ 数据就绪 · 5560 只 · 更新到 09-11`（比一行里塞八项好读）。"""
            summary = facts["summary"]
            latest = str(summary.get("latest_date") or "")
            symbols = int(summary.get("symbols") or 0)
            if latest:
                # 只要"月-日"：状态栏不是对账的地方，年份在同一天里没有信息量
                return f"✅ 数据就绪 · {symbols} 只 · 更新到 {latest[5:]}"
            return "✅ 数据就绪"

        def _intraday_text(self, st: dict) -> str:
            """盘中提醒的三种取值（`已暂停` / `时段中` / `未在时段`）。"""
            if st.get("intraday_paused"):
                return INTRADAY_PAUSED
            return INTRADAY_IN_SESSION if st.get("in_session") else INTRADAY_OUT_SESSION

        def _refresh_status_tags(self, facts: dict | None = None) -> None:
            """右侧短标签：每项 2~6 个字 + 一个短值；**没有值的项整项隐藏**。

            为什么不做成一句长文本：用户要的是"扫一眼知道今天有没有池子、持仓多少"，
            不是一串用竖线连起来的字段清单。
            """
            facts = facts if facts is not None else self._status_facts()
            st = facts["st"]
            next_run = (st.get("next_run") or {}).get("label") or ""
            tags = {
                "今日池子": f"今日池子 {facts['pool_count']}",
                "持仓": self._portfolio_tag(),
                # 自动运行关掉时不给"下次"（那会是一句永远不兑现的承诺）
                "下次选股": (f"下次选股 {next_run}" if st.get("auto_run", True)
                             else "自动运行 已关"),
                "盘中提醒": f"盘中提醒 {self._intraday_text(st)}",
            }
            for name, label in self.status_tags.items():
                text = tags.get(name, "")
                label.setText(text)
                label.setVisible(bool(text))       # 空 → 整项不显示（不是显示一个空壳）

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
                # 直接复用汇总推送那一行的写法（名称（代码）+ 板块 + 数值 + 分），
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

            这里才是"查得到就行"的东西：行数、股票数、补跑时间、后台任务状态、
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
            lines = [
                "状态详情",
                "────────────",
                f"后台任务：{'运行中' if st.get('running') else '未运行'}",
                f"最新数据日期：{latest}",
                f"本地股票数：{summary.get('symbols', 0)} 只",
                f"本地行情行数：{_short_number(summary.get('daily_rows'))} 行",
                f"今日池子：{facts['pool_count']} 只"
                f"（含自选 {summary.get('watchlist', 0)} 只）",
                f"持仓浮动：{self._floating_pnl(pnl)}",
                f"盘中提醒：{self._intraday_text(st)}",
                f"主跑时间：{st.get('run_at') or '—'}"
                f"（补跑 {st.get('run_at_fallback') or '未设置'}）",
                # 标签本身就会写"已关闭（每天自动运行）"，不再叠一句"（自动运行已关闭）"
                f"下次自动运行：{(st.get('next_run') or {}).get('label') or '—'}",
                f"数据自检：{self_check.get('status') or '尚未自检'}"
                + (f"（落后 {self_check.get('stale_trading_days')} 个交易日）"
                   if self_check.get("status") == preflight.NEEDS_INCREMENTAL else "")
                # 数据不可用时把**原始原因**留在这里（状态栏只给"点哪个按钮"）
                + (f" · {self_check.get('reason')}"
                   if self_check.get("status") == preflight.NEEDS_FULL
                   and self_check.get("reason") else ""),
                f"自检阈值：历史 ≥{self.cfg.min_history_years:g} 年 / "
                f"股票 ≥{self.cfg.min_symbols} 只 / 行业覆盖 ≥90% / 交易日历齐全",
                *self._auction_detail_lines(),
                f"数据目录：{self.cfg.data_dir}",
                f"日志文件：{log_path}",
            ]
            if st.get("skipped_reason"):
                lines.append(f"今天没自动跑：{st['skipped_reason']}")
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
            """
            try:
                with self.engine.connect() as conn:
                    positions = conn.execute(
                        "SELECT symbol, quantity, avg_cost FROM position WHERE quantity > 0"
                    ).fetchall()
                    if not positions:
                        return "无持仓", 0.0
                    latest = conn.execute(
                        "SELECT MAX(date) FROM stock_daily_hfq"
                    ).fetchone()[0]
                    prices = {
                        r[0]: r[1]
                        for r in conn.execute(
                            "SELECT symbol, close FROM stock_daily_hfq WHERE date = ?",
                            (latest,),
                        )
                    }
                total_cost = 0.0
                total_value = 0.0
                for symbol, qty, cost in positions:
                    price = prices.get(symbol)
                    if not price:
                        continue
                    total_cost += qty * (cost or 0)
                    total_value += qty * price
                if not total_cost:
                    return "无最新价", 0.0
                # 加权比例：按**成本**加权（每只股票的盈亏点数按投入摊平），
                # 不是把各只的比例简单平均 —— 后者会被小仓位的高波动带偏
                return "ok", (total_value - total_cost) / total_cost * 100
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"持仓浮动计算失败：{exc}")
                return "未知", 0.0

        def _portfolio_tag(self) -> str:
            """短标签 `持仓 +1.2%`；没有持仓（或没有最新价）→ 空串，整项不显示。"""
            state, pct = self._position_pnl()
            if state != "ok":
                return ""
            return f"持仓 {pct:+.1f}%"

        def _floating_pnl(self, values: tuple[str, float] | None = None) -> str:
            """持仓浮动的完整说法（**详情**用）：`+1.20%` / `无持仓` / `无最新价` / `—`。

            **只有比例、没有金额**：用户要求持仓不记盈亏金额（`元`这个字在这里不该出现）。
            """
            state, pct = values if values is not None else self._position_pnl()
            if state != "ok":
                return "—" if state == "未知" else state
            return f"{pct:+.2f}%"

        def _refresh_pool(self) -> None:
            """刷新股票池：**卡片与表格两套视图一起填**（看当前显示哪一个）。

            只在**内容真的变了**时重建：每行/每张卡都带一个"复制条件单"按钮，每 5 秒重建
            一次会不停销毁/创建控件（闪烁、丢焦点、白白吃 CPU）—— 桌面程序里这是很显眼的毛病。
            两套视图都填的好处：切换视图只是 setVisible，不重建、不丢滚动位置与选中状态
            （池子通常十几行，多画一份的代价可以忽略）。
            """
            rows = pool.pool_table_rows(self.cfg.db_path)
            # 签名里带上"涨停信息 + 竞价那一行"：竞价数据每分钟变一次、涨停池盘中会变，
            # 变了就要重建卡片（否则卡片上那两行会停在旧值）
            signature = tuple(
                (r["symbol"], r.get("score"), r.get("reason"), r.get("industry"),
                 r.get("source_label"), r.get("note"),
                 r.get("is_limit_up"), r.get("continue_day_text"), r.get("limit_up_reason"),
                 self._auction_text(r["symbol"]))
                for r in rows
            )
            if signature == self._pool_signature:
                return
            self._pool_signature = signature

            # 每只股票的最新价只查一次（表格与卡片共用，省掉一半数据库查询）
            prices = {r["symbol"]: self._last_price(r["symbol"]) for r in rows}

            # ── 表格视图 ──
            self.pool_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                price = prices[row["symbol"]]
                plan_text = self._plan_summary(row, price)
                self.pool_table.setItem(i, 0, QTableWidgetItem(str(row["symbol"])))
                self.pool_table.setItem(i, 1, QTableWidgetItem(str(row.get("name") or "")))
                # 策略列带"证据标记"：`（依赖开盘）` = 该策略的正 α 只在"开盘买"口径下
                # 存在（见 pool.evidence_text）—— 一眼能看出来，判断权交给用户
                label_text = str(row.get("label") or "")
                evidence = str(row.get("evidence_text") or "")
                item = QTableWidgetItem(f"{label_text}{evidence}")
                if evidence:
                    item.setToolTip(pool_mod.open_only_tooltip(label_text))
                self.pool_table.setItem(i, 2, item)
                self.pool_table.setItem(i, 3, QTableWidgetItem(str(row.get("source_label") or "—")))
                self.pool_table.setItem(i, 4, QTableWidgetItem(str(row.get("note") or "")))
                # 「涨停」列：`2 连板 · 半导体设备`（不在今日涨停池里就是空的）
                limit_up = pool_mod.limit_up_text(row)
                self.pool_table.setItem(i, 5, QTableWidgetItem(limit_up))
                self.pool_table.setItem(i, 6, QTableWidgetItem(str(row.get("industry") or "")))
                self.pool_table.setItem(i, 7, QTableWidgetItem(_fmt_float(row.get("score"), 3)))
                self.pool_table.setItem(i, 8, QTableWidgetItem(plan_text))
                btn = QPushButton("复制条件单")
                btn.clicked.connect(lambda _=False, r=row, p=price: self._copy_plan(r, p))
                self.pool_table.setCellWidget(i, 9, btn)

            # ── 卡片视图 ──
            for card in self.pool_cards:
                # 先脱离布局与父控件再删：否则 Qt 要等事件循环才真正销毁，
                # 中间这一小段时间里旧卡片还挂在 container 上（看着像"重影"）
                card.hide()
                self.pool_cards_layout.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
            self.pool_cards = []
            for i, row in enumerate(rows):
                card = self._build_pool_card(row, prices[row["symbol"]])
                color = self._auction_color(str(row["symbol"]))
                card.auction_label.setStyleSheet(f"color:{color}" if color else "")
                # 插在底部弹簧之前（弹簧始终在最后，卡片才不会散在页面中间）
                self.pool_cards_layout.insertWidget(i, card)
                self.pool_cards.append(card)

            # 空池提示：**两种视图下都要正确**（表格视图空着也一样要说明白为什么空）
            self.pool_empty_label.setVisible(not rows)

        def _apply_pool_view(self, view: str) -> None:
            """按 view（`cards` / `table`）显示对应视图，并把按钮文字改成"点了会发生什么"。

            按钮文字跟着**当前视图**走而不是固定一句"切换视图"：用户扫一眼就知道
            点下去会得到什么，不用先点一次试试（这也是原始需求里点名的行为）。
            """
            show_cards = str(view or "").strip().lower() != "table"   # 写错的值当卡片
            self._pool_view = "cards" if show_cards else "table"
            self.pool_scroll.setVisible(show_cards)
            self.pool_table.setVisible(not show_cards)
            self.btn_pool_view.setText("切换为表格" if show_cards else "切换为卡片")

        def on_toggle_pool_view(self) -> None:
            """【切换为表格/卡片】：换视图并**写回配置**（键 `pool_view`）。

            为什么写回：习惯用表格的人不该每次开程序都要再点一下；
            整条写回走 `_save_updates` → `config.save_settings`（只改这一个键、保留注释）。
            """
            target = "table" if self._pool_view == "cards" else "cards"
            self._apply_pool_view(target)
            self._save_updates(
                {"pool_view": target},
                "股票池视图已切换为" + ("表格" if target == "table" else "卡片"),
            )

        def _last_price(self, symbol: str) -> float | None:
            try:
                with self.engine.connect() as conn:
                    row = conn.execute(
                        "SELECT close FROM stock_daily_hfq WHERE symbol = ? "
                        "ORDER BY date DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()
                return float(row[0]) if row and row[0] else None
            except Exception:  # noqa: BLE001
                return None

        def _plan_summary(self, row: dict, price: float | None) -> str:
            if not price:
                return "缺最新价"
            try:
                plan = intraday.plan_buy(
                    row["symbol"], row.get("name") or "", price, cfg=self.cfg
                )
                return (
                    f"触发 {plan['trigger']:.2f}｜委托 {plan['limit_price']:.2f}"
                    f"｜{plan['quantity']} 股｜损 {plan['stop_loss']:.2f}｜盈 {plan['take_profit']:.2f}"
                )
            except Exception as exc:  # noqa: BLE001
                return f"参数计算失败：{exc}"

        def _copy_plan(self, row: dict, price: float | None) -> None:
            """把条件单文本写入剪贴板（一键复制）。"""
            if not price:
                self._toast("缺少最新价，无法生成条件单")
                return
            try:
                from laoa_trader.data import storage

                with self.engine.connect() as conn:
                    positions = storage.load_positions(conn)
                if row["symbol"] in positions:
                    pos = positions[row["symbol"]]
                    plan = intraday.plan_sell(
                        row["symbol"], row.get("name") or "", pos["avg_cost"],
                        price=price, qty=pos["quantity"], cfg=self.cfg,
                    )
                else:
                    plan = intraday.plan_buy(
                        row["symbol"], row.get("name") or "", price,
                        reason=row.get("reason") or "", positions=len(positions),
                        cfg=self.cfg,
                    )
                QApplication.clipboard().setText(plan["text"])
                self._toast(f"已复制 {row['symbol']} 的条件单参数")
            except Exception as exc:  # noqa: BLE001
                self._toast(f"复制失败：{exc}")

        def _refresh_positions(self) -> None:
            with self.engine.connect() as conn:
                from laoa_trader.data import storage

                positions = storage.load_positions(conn)
            rows = list(positions.values())
            self.position_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                cost = float(row.get("avg_cost") or 0)
                self.position_table.setItem(i, 0, QTableWidgetItem(str(row["symbol"])))
                self.position_table.setItem(i, 1, QTableWidgetItem(str(row.get("name") or "")))
                self.position_table.setItem(i, 2, QTableWidgetItem(str(row.get("quantity") or 0)))
                self.position_table.setItem(i, 3, QTableWidgetItem(_fmt_float(cost)))
                # 「盈亏比例」：该股最新价相对成本价的浮动（涨红跌绿，没价就 `—`）
                self.position_table.setItem(
                    i, 4, self._position_change_item(cost, self._last_price(str(row["symbol"])))
                )
                self.position_table.setItem(
                    i, 5, QTableWidgetItem(_fmt_float(cost * (1 - self.cfg.stop_loss)))
                )
                self.position_table.setItem(
                    i, 6, QTableWidgetItem(_fmt_float(cost * (1 + self.cfg.take_profit)))
                )

        @staticmethod
        def _position_change_item(cost: float, price: float | None) -> Any:
            """「盈亏比例」单元格：`+7.14%`（涨红跌绿），没有最新价就是 `—`。

            为什么缺价显示 `—` 而不是 `0.00%`：0.00% 是"平盘"这个**真实结论**，
            和"没取到价"完全不是一回事 —— 混在一起会让人以为持仓刚好打平。
            颜色沿用 `market.value_color()`（全程序"涨红跌绿"的唯一定义，
            平盘与缺价都返回空串 → 用默认前景色）。
            """
            item = QTableWidgetItem()
            if price is None or not cost:
                item.setText(market.DASH)
                return item
            pct = (float(price) - cost) / cost * 100
            item.setText(f"{pct:+.2f}%")
            color = market.value_color(pct)
            if color:
                # 用前景色而不是富文本：与表格其余部分同一套画法，排序/复制都不受影响
                item.setForeground(QBrush(QColor(color)))
            return item

        def _refresh_watchlist(self) -> None:
            """自选股列表：代码/名称/备注/状态/是否已进池。"""
            from laoa_trader.data import storage

            with self.engine.connect() as conn:
                rows = storage.load_watchlist(conn)
            in_pool = set(pool.pool_symbols(self.cfg.db_path))
            self.watch_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                enabled = int(row.get("enabled", 1)) == 1
                self.watch_table.setItem(i, 0, QTableWidgetItem(str(row["symbol"])))
                self.watch_table.setItem(i, 1, QTableWidgetItem(str(row.get("name") or "")))
                self.watch_table.setItem(i, 2, QTableWidgetItem(str(row.get("note") or "")))
                self.watch_table.setItem(i, 3, QTableWidgetItem("启用" if enabled else "已停用"))
                if not self.cfg.watchlist_in_pool:
                    state = "不监控（watchlist_in_pool=false）"
                else:
                    state = "已进池" if row["symbol"] in in_pool else "待下次建池"
                self.watch_table.setItem(i, 4, QTableWidgetItem(state))

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
                self._tick()
                self._select_watch_row(symbol)
            except Exception as exc:  # noqa: BLE001
                self._toast(f"加自选失败：{type(exc).__name__}: {exc}")

        def _selected_watch_symbol(self) -> str:
            """当前操作对象：表格选中的行优先，其次输入框里的代码。

            为什么要这样：点完【加自选】输入框会被清空，用户接着想【停用】刚才那只，
            如果只认输入框就会得到"先填代码"——所以选中行优先更符合直觉。
            """
            rows = self.watch_table.selectionModel().selectedRows()
            if rows:
                index = rows[0].row()
                item = self.watch_table.item(index, 0)
                if item is not None and item.text():
                    return item.text().strip()
            text = self.watch_symbol.text().strip()
            return text.zfill(6) if text else ""

        def _select_watch_row(self, symbol: str) -> None:
            """把表格选中行移到指定代码（加完自选就能直接接着操作它）。"""
            for i in range(self.watch_table.rowCount()):
                item = self.watch_table.item(i, 0)
                if item is not None and item.text() == symbol:
                    self.watch_table.selectRow(i)
                    return

        def on_watch_remove(self) -> None:
            self._invalidate_summary()
            from laoa_trader.data import storage

            symbol = self._selected_watch_symbol()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("先在自选股表里选中一行，或填 6 位代码再点删除")
                return
            with self.engine.connect() as conn:
                removed = storage.remove_watchlist(conn, symbol)
            self._toast(f"已删除自选 {symbol}" if removed else f"未找到自选 {symbol}")
            self._tick()

        def on_watch_toggle(self, enabled: bool) -> None:
            self._invalidate_summary()
            """启用/停用：停用后不进池、不监控，但仍留在列表里。"""
            from laoa_trader.data import storage

            symbol = self._selected_watch_symbol()
            if not (symbol.isdigit() and len(symbol) == 6):
                self._toast("先在自选股表里选中一行，或填 6 位代码再点启用/停用")
                return
            with self.engine.connect() as conn:
                changed = storage.set_watchlist_enabled(conn, symbol, enabled)
            if not changed:
                self._toast(f"未找到自选 {symbol}")
                return
            self._toast(f"已{'启用' if enabled else '停用'} {symbol}"
                        + ("" if enabled else "（不进池、不监控）"))
            self._tick()

        def _refresh_alerts(self) -> None:
            """最近的盘中提醒：第 2 列显示成 `名称（代码）`。

            名字从 `stock_basic` 查（`intraday_alert` 表里没有 name 列，也**不加列做迁移**）：
            一次查询拿全表映射，而不是每行查一次 —— 提醒列表最多 100 行，
            逐行查就是 100 次数据库往返（每 5 秒一轮）。
            """
            rows = intraday.alert_rows(self.cfg.db_path, limit=100)
            names = self._stock_names([str(r.get("symbol") or "") for r in rows])
            self.alert_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                self.alert_table.setItem(i, 0, QTableWidgetItem(str(row.get("pushed_at") or "")))
                self.alert_table.setItem(i, 1, QTableWidgetItem(self._alert_target_text(row, names)))
                self.alert_table.setItem(i, 2, QTableWidgetItem(str(row.get("label") or row.get("kind") or "")))
                self.alert_table.setItem(i, 3, QTableWidgetItem(_fmt_float(row.get("price"))))
                self.alert_table.setItem(i, 4, QTableWidgetItem(str(row.get("detail") or "")))

        def _auction_text(self, symbol: str) -> str:
            """卡片上竞价那一行：`竞价 +3.2% 量比 2.8`（没有数据/不在窗口 → 空串）。"""
            fields = self.auction_snapshot.get(symbol)
            return intraday.auction_card_text(fields) if fields else ""

        def _auction_color(self, symbol: str) -> str:
            """竞价那一行的颜色：涨红跌绿（复用 `market.value_color` 的唯一定义）。"""
            fields = self.auction_snapshot.get(symbol)
            return market.value_color((fields or {}).get("pct")) if fields else ""

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
            """`{symbol: 名称}`（一次查询）：代码 → 名字，供界面拼 `名称（代码）`。"""
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
            """提醒里那一列：`名称（代码）`。

            三种退化都要好看：没有名字 → 只显示代码；连代码都没有（老库里的历史
            提醒行、将来某类"全市场"提醒）→ 用说明里的名称，再不行才 `—`。
            **绝不显示 `（None）`**。
            """
            symbol = str(row.get("symbol") or "").strip()
            name = str(names.get(symbol) or "").strip()
            if symbol:
                return f"{name}（{symbol}）" if name else symbol
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
            """把库里的提醒行转成浮窗要的展示条目（`名称（代码）` 在这里拼好）。"""
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
            """【查看全部】/【最近提醒】：停闪 → 显示主窗口 → 切到「盘中提醒」页。"""
            self._stop_alert_flash()
            self._restore_window()
            index = self.tabs.indexOf(self.alert_table)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            self._refresh_alerts()

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

        def _run_worker(self, fn, label: str, with_progress: bool = False,
                        with_stage: bool = False, with_note: bool = False) -> None:
            # 明确设成"确定进度"的 0%（range=0..100）：下载最初几秒在取签名/建连，
            # 这期间进度条必须显示 0% 而不是"未开始/不确定"的样子
            self.progress.setRange(0, 100)
            self.progress.setFormat(f"{label} %p%")
            self.progress.setValue(0)
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
            """后台任务的一句话状态（下载重签/重试/完成）→ 状态栏 + 向导窗口。"""
            self._set_status(f"{label}：{text}")
            if self.wizard is not None and self.wizard.isVisible():
                try:
                    self.wizard_status.setText(text)
                except Exception:  # noqa: BLE001 - 向导可能已被关掉
                    pass

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
            self.progress.setFormat(label + " %p%")
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(min(done, max(total, 1)))
            # 首次向导里也有自己的进度条（下载时用户盯的是那个窗口）
            if self.wizard is not None and self.wizard.isVisible():
                try:
                    self.wizard_progress.setRange(0, max(total, 1))
                    self.wizard_progress.setValue(min(done, max(total, 1)))
                    if total >= 1_000_000:
                        self.wizard_progress.setFormat(label + " %p%")
                    self.wizard_status.setText(status)
                except Exception:  # noqa: BLE001 - 向导可能已被关掉
                    pass
            # 记下"阶段 + 数量"，下载中那行会把它补在百分比后面（例如 `· 81/181 MB`）
            self._progress_stage_text = (
                f"{stage} {done / 1e6:.0f}/{total / 1e6:.0f} MB" if total >= 1_000_000
                else (f"{stage} {done}/{total}" if total > 0 else f"{stage} {done}")
            )
            self._set_status(status, progress=True)

        def _on_worker_done(self, label: str, result: Any) -> None:
            self.progress.setValue(self.progress.maximum())
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
            self._set_status(f"❌ {label}失败：{msg.splitlines()[0]}")

        # ── 保存设置（写回 config.toml，保留注释与未知键）──

        def _save_updates(self, updates: dict, ok_text: str) -> None:
            """统一的保存流程：写文件 → 同步内存配置 → 刷新界面提示。"""
            from laoa_trader.config import save_settings

            try:
                path, self.cfg = save_settings(self.cfg, updates)
                self._toast(f"{ok_text}（已写入 {path.name}）")
            except OSError as exc:
                # 只读盘/权限问题：给中文原因，不弹 traceback
                self._toast(f"保存失败：{exc}（可手改 config.toml）")
                return
            except Exception as exc:  # noqa: BLE001
                self._toast(f"保存失败：{type(exc).__name__}: {exc}")
                return
            self._refresh_status()
            self._refresh_channel_hints()

        def on_save_run_at(self) -> None:
            """保存自动运行设置：**先校验**，不通过就拒绝写入并说明原因。"""
            from laoa_trader.scheduler import validate_run_times

            primary = self.run_at_edit.text().strip()
            fallback = self.run_at_fallback_edit.text().strip()
            ok, messages = validate_run_times(primary, fallback)
            if not ok:
                # 明确拒绝：不写配置，把原因显示出来（状态栏 + 设置页提示）
                self._toast("❌ " + "；".join(messages) + "（未写入配置）")
                self._refresh_run_hint("❌ " + "；".join(messages))
                return
            warning = next((m for m in messages if m.startswith("⚠️")), "")
            self._save_updates(
                {
                    "auto_run": self.auto_run_box.isChecked(),
                    "run_at": primary,
                    "run_at_fallback": fallback,
                },
                ("自动运行已保存：" + ("开" if self.auto_run_box.isChecked() else "关")
                 + f"，主跑 {primary}" + (f"，补跑 {fallback}" if fallback else ""))
                + (f"；{warning}" if warning else ""),
            )
            self._refresh_run_hint(warning)
            self._refresh_status()

        def _refresh_run_hint(self, extra: str = "") -> None:
            """把"下次自动运行"显示到设置页与状态栏（让人一眼看出设定生效了）。"""
            info = self.scheduler.status()["next_run"]
            text = f"下次自动运行：{info['label']}"
            if extra:
                text += "　｜　" + extra
            if hasattr(self, "run_hint"):
                self.run_hint.setText(text)
                self.run_hint.setWordWrap(True)

        def on_save_groups(self) -> None:
            """保存策略组选择：组勾选 + 策略勾选（全不勾 = 该组全选）。"""
            from laoa_trader.strategy import groups as groups_mod

            chosen_groups = [k for k, box in self.group_boxes.items() if box.isChecked()]
            chosen_strategies = [
                name for name, box in self.strategy_boxes.items() if box.isChecked()
            ]
            # 勾了策略但没勾它的组：自动把组也勾上，避免出现"选了却不跑"
            for name in chosen_strategies:
                key = groups_mod.group_of(name)
                if key and key not in chosen_groups:
                    chosen_groups.append(key)
            if not chosen_groups:
                self._toast("至少要勾一个策略组（或全部勾上 = 全跑）")
                return
            selection = groups_mod.resolve(chosen_groups, chosen_strategies)
            if selection.empty:
                self._toast("没有可跑的策略：" + "；".join(selection.warnings))
                return
            self._save_updates(
                {"enabled_groups": chosen_groups, "enabled_strategies": chosen_strategies},
                f"策略组已保存：{selection.describe()}",
            )

        def _panel_notify_updates(self) -> dict:
            """把通知设置面板的**当前**状态收集成 {键: 值}（保存与测试提醒共用）。"""
            return {
                "notify_channels": [
                    name for name, box in self.channel_boxes.items() if box.isChecked()
                ],
                "notify_windows_sound": self.win_sound_box.isChecked(),
                "notify_windows_open_url": self.win_url_box.isChecked(),
                "notify_tray_duration_ms": int(self.tray_duration.value()),
                "feishu_on": self.feishu_on_box.isChecked(),
                "feishu_app_id": self.feishu_app_id.text().strip(),
                "feishu_app_secret": self.feishu_secret.text().strip(),
                "feishu_chat_id": self.feishu_chat_id.text().strip(),
            }

        def on_save_notify(self) -> None:
            """保存通知方式：频道勾选 + 各频道参数。"""
            updates = self._panel_notify_updates()
            channels = updates["notify_channels"]
            if not channels:
                self._save_updates(updates, "通知已保存：不推送（只入库）")
                return
            if "feishu" in channels and not (updates["feishu_app_id"]
                                            and updates["feishu_app_secret"]):
                # 明确提示，但不阻止保存、不报错
                self._save_updates(
                    updates,
                    "通知已保存，但飞书未配置凭证 —— 发送时会自动跳过飞书，其余频道照常",
                )
                return
            self._save_updates(updates, "通知设置已保存")

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
                "auction_min_score": int(self.auction_score_box.value()),
                "auction_alert_max_items": int(self.auction_items_box.value()),
                "auction_boards": boards,
                "auction_scan_at": good or ["09:20", "09:25"],
                "_bad_scan_at": bad,          # 只给提示用，不写进配置文件
            }

        def on_save_auction(self) -> None:
            """保存竞价设置：**先校验**（涨幅上下限不能倒挂、时刻要能认出来），再写文件。"""
            updates = self._panel_auction_updates()
            bad = updates.pop("_bad_scan_at", [])
            lo, hi = updates["auction_min_pct"], updates["auction_max_pct"]
            if hi > 0 and hi <= lo:
                message = f"❌ 涨幅上限（{hi:g}%）必须大于下限（{lo:g}%）（未写入配置）"
                self._toast(message)
                self._refresh_auction_hint(message)
                return
            extra = f"；⚠️ 认不出的时刻已忽略：{'、'.join(bad)}" if bad else ""
            state = "开" if updates["intraday_auction"] else "关"
            self._save_updates(
                updates,
                f"竞价设置已保存：{state}；涨幅 {lo:g}%~{hi:g}%、"
                f"成交额 ≥ {updates['auction_min_amount'] / 1e4:,.0f} 万、"
                f"打分 ≥ {updates['auction_min_score']}、推前 "
                f"{updates['auction_alert_max_items']} 只、"
                f"扫描 {', '.join(updates['auction_scan_at'])}" + extra,
            )
            self._refresh_auction_hint(extra)
            self._refresh_status()

        def _refresh_auction_hint(self, extra: str = "") -> None:
            """竞价那一组的说明行：把"开关 + 上次扫描 + 请求量级"写清楚（看得出设定生效了）。"""
            if not hasattr(self, "auction_hint"):
                return
            lines = []
            if not bool(getattr(self.cfg, "intraday_auction", False)):
                lines.append("竞价扫描当前**关着**：一次请求都不发（勾上「启用竞价扫描」并保存即可开）")
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
            """【立即选股并建池】：先过**数据闸门** → 增量 → 策略 → 建池 → 推送。

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
            """数据不可用时拒绝手动选股：中文提示 + 把注意力引到下载按钮。"""
            text = f"⚠️ {message}"
            self._toast(text)
            self._set_status(text)
            logger.warning(f"已拒绝手动选股：{message}")
            try:
                # 引导：把焦点/视觉移回下载按钮（文字提示已经说清了要做什么）
                self.btn_download.setDefault(True)
                self.btn_download.setFocus()
            except Exception:  # noqa: BLE001 - 界面细节失败不影响"拒绝"本身
                pass

        def on_refresh_data(self) -> None:
            """【只刷新数据】：只跑增量同步，不选股、不推送。"""
            if self._busy():
                return
            self._run_worker(
                lambda progress_cb: refresh_data(self.cfg, self.engine,
                                                 progress_cb=progress_cb),
                "刷新数据",
                with_progress=True,
            )

        def on_download(self) -> None:
            if self._busy():
                return
            if not self.cfg.hithink_api_key:
                QMessageBox.warning(
                    self, "缺少 API Key",
                    "尚未配置同花顺 API Key。\n\n"
                    "请在 config.toml 里填写 hithink_api_key，"
                    "或设置环境变量 HITHINK_FINANCE_API_KEY 后重启本程序。",
                )
                return
            self._run_worker(
                lambda progress_cb, note_cb: sync.download_history(
                    self.cfg, progress_cb=progress_cb, note_cb=note_cb
                ),
                "下载/更新历史数据",
                with_progress=True, with_note=True,
            )

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
            # 按钮文字走 `BTN_*` 常量：状态区里"点【暂停提醒】"这类指路文案引用的就是它
            if self.scheduler.intraday_paused:
                self.scheduler.resume_intraday()
                self.btn_pause.setText(BTN_PAUSE_TEXT)
            else:
                self.scheduler.pause_intraday()
                self.btn_pause.setText(BTN_RESUME_TEXT)
            self._tick()

        # ── 持仓操作 ──

        def on_add_position(self) -> None:
            self._invalidate_summary()
            symbol = self.pos_symbol.text().strip()
            try:
                quantity = int(float(self.pos_qty.text().strip() or 0))
                cost = float(self.pos_cost.text().strip() or 0)
            except ValueError:
                self._toast("数量/成本价必须是数字")
                return
            if not symbol.isdigit() or len(symbol) != 6:
                self._toast("请填 6 位股票代码")
                return
            try:
                from laoa_trader.data import storage

                name = self.engine.get_stock_names([symbol]).get(symbol)
                with self.engine.connect() as conn:
                    storage.upsert_position(
                        conn, symbol, name=name, quantity=quantity, avg_cost=cost
                    )
                self._toast(f"已记录持仓 {symbol} {quantity} 股 @ {cost}")
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self._toast(f"写入持仓失败：{exc}")

        def on_delete_position(self) -> None:
            self._invalidate_summary()
            symbol, ok = QInputDialog.getText(self, "删除持仓", "要删除的股票代码：")
            if not ok or not symbol.strip():
                return
            try:
                from laoa_trader.data import storage

                with self.engine.connect() as conn:
                    removed = storage.delete_position(conn, symbol.strip())
                self._toast("已删除" if removed else "未找到该持仓")
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
