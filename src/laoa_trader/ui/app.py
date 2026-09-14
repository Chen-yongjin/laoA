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
from typing import Any

import laoa_trader
from laoa_trader import assets, intraday, market, pool, state
from laoa_trader.config import Config, get_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger
from laoa_trader.notify import KINDS, summarize
from laoa_trader.scheduler import Scheduler, data_gate, refresh_data, run_daily
from laoa_trader.strategy import rules as rules_mod

logger = get_logger(__name__)

#: 程序名 / 版权行 / 数据来源：「窗口标题」「关于」对话框、复制到剪贴板的版本信息
#: **共用这一份** —— 分发出去之后用户看到的版本信息必须处处一致，不能各写各的
APP_NAME = "老A法师 · 交易终端"
COPYRIGHT_TEXT = "版权所有 © 2026 async-chen，保留所有权利。"
SOURCE_TEXT = ("数据来源：同花顺（fuyao.aicubes.cn）。"
               "本程序仅用于个人研究与学习，不构成任何投资建议。")

#: 关于页里图标的显示边长（资源只有 256/128/48/32/16 这几档，这里由 QPixmap 平滑缩放）
ABOUT_ICON_SIZE = 64

#: 「大盘概览」页自己的刷新周期（毫秒）—— 每分钟一次，与 5 秒的界面刷新解耦。
#: 取数另有 55 秒 TTL（`market_overview_ttl`），所以这一分钟里最多真打一次接口。
MARKET_REFRESH_MS = 60_000

try:  # Qt 缺失时必须优雅降级（Linux 开发机、精简环境）
    from PySide6.QtCore import Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QFrame,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressBar,
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

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 任何导入问题都降级为 CLI
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


def _fmt_float(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


if QT_AVAILABLE:

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
            #: 状态栏的瞬时消息（任务完成/失败提示），与实时状态分两层显示
            self._message = ""
            self._message_at = 0.0
            #: 池子表格的内容指纹（内容没变就不重建控件）
            self._pool_signature: tuple = ()
            #: 当前渲染出来的卡片（顺序与池子行一致）与当前视图（cards/table）
            self.pool_cards: list[Any] = []
            self._pool_view = "cards"
            #: 启动自检结果（三态）与首次向导
            self.preflight_result: dict | None = None
            self.wizard: Any = None
            #: 最近一次的大盘概览（拿不到就是 None）——测试与"复制/追查原因"都从它取值
            self.market_overview: dict | None = None
            #: 「关于」对话框（测试与"重复点关于"都要能拿到它）
            self.about_dialog: Any = None
            #: 「关于」里的图标标签（资源缺失时为 None）
            self.about_icon: Any = None
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
            self.resize(1120, 720)
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

        def _build_ui(self) -> None:
            central = QWidget()
            layout = QVBoxLayout(central)

            # 顶部状态栏
            self.status_label = QLabel("正在初始化…")
            self.status_label.setWordWrap(True)
            layout.addWidget(self.status_label)

            # 按钮行
            buttons = QHBoxLayout()
            self.btn_download = QPushButton("下载/更新历史数据")
            self.btn_download.clicked.connect(self.on_download)
            buttons.addWidget(self.btn_download)

            # 【立即选股并建池】：跑完整流程（增量数据 → 策略 → 建池 → 按通知设置推送）
            self.btn_run = QPushButton("立即选股并建池")
            self.btn_run.clicked.connect(self.on_run_pipeline)
            buttons.addWidget(self.btn_run)

            # 【只刷新数据】：只补数据（行情/涨停池/日历/行业/指数），不选股、不推送
            self.btn_refresh = QPushButton("只刷新数据")
            self.btn_refresh.clicked.connect(self.on_refresh_data)
            buttons.addWidget(self.btn_refresh)

            self.btn_pause = QPushButton("暂停盘中提醒")
            self.btn_pause.clicked.connect(self.on_toggle_intraday)
            buttons.addWidget(self.btn_pause)

            self.btn_check = QPushButton("立即检查盘面")
            self.btn_check.clicked.connect(self.on_intraday_once)
            buttons.addWidget(self.btn_check)

            buttons.addStretch(1)
            # 【关于】放最右：版本号 / 版权 / 数据来源都在里面（报障时用户第一句话就是版本）
            self.btn_about = QPushButton("关于")
            self.btn_about.clicked.connect(self.on_about)
            buttons.addWidget(self.btn_about)
            layout.addLayout(buttons)

            self.progress = QProgressBar()
            self.progress.setValue(0)
            layout.addWidget(self.progress)

            # 四个表 + 五个页：池子 / 持仓 / 自选 / 提醒 / 设置
            self.tabs = QTabWidget()

            # 股票池页 = 【卡片/表格】切换按钮 + 空池提示 + 卡片视图 + 表格视图。
            # 两个视图**都留着**：卡片看得全，表格看得密，用户自己挑；切换只是 setVisible。
            self.pool_table = QTableWidget(0, 9)
            # 「来源」列把"策略组别"和"自选/策略+自选"合成一列（避免两列重复信息）
            self.pool_table.setHorizontalHeaderLabels(
                ["代码", "名称", "来源策略", "来源", "备注", "热门行业", "分数",
                 "条件单参数", "复制"]
            )
            self._stretch(self.pool_table)
            # 概览放**第一个**（Qt 启动就显示第一个页签）：看盘第一眼要扫到，
            # 而且它是只读的一页，进来就能看，不需要先选股
            self.tabs.addTab(self._build_market_page(), "大盘概览")
            self.tabs.addTab(self._build_pool_page(), "股票池")

            self.position_table = QTableWidget(0, 6)
            self.position_table.setHorizontalHeaderLabels(
                ["代码", "名称", "数量", "成本", "止损", "止盈"]
            )
            self._stretch(self.position_table)
            self.tabs.addTab(
                self._build_table_page(self._build_position_row(), self.position_table),
                "持仓",
            )

            self.alert_table = QTableWidget(0, 5)
            self.alert_table.setHorizontalHeaderLabels(["时间", "代码", "类型", "价格", "说明"])
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

            # 设置页：策略组自选 + 通知方式自选（都写回 config.toml，保留注释）
            self.tabs.addTab(self._build_settings_tab(), "设置")

            layout.addWidget(self.tabs)

            self.setCentralWidget(central)

            # 启动时按配置决定股票池显示哪种视图（配置写错时 `Config` 已经归一成 cards）
            self._apply_pool_view(self.cfg.pool_view)

        def _build_table_page(self, row: Any, table: Any) -> Any:
            """把"操作行 + 表格"装进同一个页签。

            为什么要拆页：这两行原来是挂在**主窗口底部**的，切到「设置」页也照样看得见，
            用户反馈"不知道在给哪一页录入"。输入行跟着它影响的表格走，视线不用来回跳。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.addLayout(row)        # 输入行在上：先填再点，符合操作顺序
            layout.addWidget(table)
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

        def _build_market_page(self) -> Any:
            """「大盘概览」页：摘要 / 涨跌家数 / 三组指数 / 底部来源与【立即刷新】。

            为什么单独成页而不是塞进股票池页顶部：这是"看盘第一眼要扫到"的东西，
            独立一页才能给足留白与字号（摘要行加粗放大、组与组之间留空），
            股票池页也就不会被这几行挤掉高度。

            为什么一行一个 QLabel：用户明确要求"涨=红、跌=绿"用 `QLabel.setStyleSheet`
            上色 —— 一行一个控件才谈得上"整行同向时才上色"，也才能做到
            "某组没配就整行不显示"。`market_labels` 的下标含义见 `market.lines()`。
            """
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 12)
            layout.setSpacing(10)          # 整页而不是一行：留白放宽一点

            title = QLabel("大盘概览")
            font = title.font()
            font.setBold(True)
            if font.pointSize() > 0:
                font.setPointSize(font.pointSize() + 2)
            title.setFont(font)
            layout.addWidget(title)

            #: 摘要 / 涨跌家数 / 宽基 / 情绪 / 板块（顺序与 market.lines() 一致）
            self.market_labels: list[Any] = []
            for index in range(market.LINE_COUNT):
                label = QLabel(market.DASH)
                label.setWordWrap(True)
                # 可选中复制：这几行数常被贴到群里或笔记里
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                label_font = label.font()
                if index == 0:
                    # 摘要行是"一眼看大盘"的那行，加大一号并加粗
                    if label_font.pointSize() > 0:
                        label_font.setPointSize(label_font.pointSize() + 3)
                    label_font.setBold(True)
                elif index == 1:
                    if label_font.pointSize() > 0:
                        label_font.setPointSize(label_font.pointSize() + 1)
                label.setFont(label_font)
                layout.addWidget(label)
                self.market_labels.append(label)

            # 取不到数据时的原因写在这里（正常时隐藏）：光看一排 — 用户猜不出为什么
            self.market_hint = QLabel("")
            self.market_hint.setWordWrap(True)
            self.market_hint.setVisible(False)
            layout.addWidget(self.market_hint)

            layout.addStretch(1)

            footer = QHBoxLayout()
            self.market_as_of_label = QLabel(market.footer_text(None))
            self.market_as_of_label.setWordWrap(True)
            self.market_as_of_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            footer.addWidget(self.market_as_of_label)
            footer.addStretch(1)
            self.btn_market_refresh = QPushButton("立即刷新")
            self.btn_market_refresh.setToolTip(
                "立刻重新取一次（平时每分钟自动刷新一次；本按钮会忽略缓存）"
            )
            self.btn_market_refresh.clicked.connect(self.on_market_refresh_clicked)
            footer.addWidget(self.btn_market_refresh)
            layout.addLayout(footer)

            self.market_page = page
            self._render_market_overview()   # 先画空骨架：第一秒就是"能看"的样子
            return page

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
            看盘的人不该盯着"一分钟前的旧数"。
            """
            if self.tabs.currentWidget() is self.market_page:
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
            """把 `self.market_overview` 画到页面上（五行文本 + 按行上色 + 来源与原因）。

            涨跌按 **A 股习惯**配色（涨=红、跌=绿、平=默认色）；一行里所有涨跌幅同向才
            上色，有涨有跌就用默认色 —— 一个 QLabel 只有一种颜色，
            硬取其中一个值去上色反而会误导人。

            `lines()` 里空串表示"这一组在配置里是空的"→ **整行隐藏**，不留一个空的"情绪："。
            """
            overview = self.market_overview
            texts = market.lines(overview)
            colors = market.line_colors(overview)
            for label, text, color in zip(self.market_labels, texts, colors):
                label.setText(text)
                label.setStyleSheet(f"color:{color}" if color else "")
                label.setVisible(bool(text))     # 组没配 → 那一行整行不显示

            self.market_as_of_label.setText(market.footer_text(overview))

            # 原因写进 tooltip（鼠标一停就能看到）与 hint（当前两句话，太长就省略）
            detail = market.summary_text(overview)
            tooltip = ("大盘概览：" + detail) if detail else "大盘概览：暂无数据"
            for label in [*self.market_labels, self.market_as_of_label, self.market_page]:
                label.setToolTip(tooltip)
            errors = [str(e) for e in ((overview or {}).get("errors") or [])]
            if errors:
                text = "；".join(errors[:2]) + ("…" if len(errors) > 2 else "")
                self.market_hint.setText("⚠️ " + text)
                self.market_hint.setToolTip("；".join(errors))
            else:
                self.market_hint.setText("")
            self.market_hint.setVisible(bool(errors))

        def _build_pool_page(self) -> Any:
            """股票池页：切换按钮 + 空池提示 + 卡片视图（滚动区）+ 表格视图。

            为什么两种视图都留着：卡片把"来源/行业/备注/条件单参数"这些长短不一的文字
            完整显示出来，池子十几只时最好用；但表格一行一只、能一眼横扫，
            池子大或想比对分数时更顺手 —— 这是两种习惯，不该替用户二选一。
            """
            page = QWidget()
            layout = QVBoxLayout(page)

            view_row = QHBoxLayout()
            self.btn_pool_view = QPushButton("切换为表格")
            self.btn_pool_view.setToolTip("在「卡片」与「表格」两种股票池视图之间切换（会记住选择）")
            self.btn_pool_view.clicked.connect(self.on_toggle_pool_view)
            view_row.addWidget(self.btn_pool_view)
            view_row.addStretch(1)
            layout.addLayout(view_row)

            # 空池提示放在两个视图**之外**：这样切到表格视图时它照样显示/隐藏
            self.pool_empty_label = QLabel(
                "今日没有入选标的（收盘后自动选股，或点【立即选股并建池】）"
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
            layout.addWidget(self.pool_scroll)

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
                head.addWidget(QLabel(card.label))
            if row.get("score") is not None:
                # 没分数就不显示 —— 表格里那种"空单元格 + 一个 —"在卡片上很难看
                head.addWidget(QLabel(f"分数 {card.score_text}"))
            layout.addLayout(head)

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
            """
            from laoa_trader.strategy import groups as groups_mod

            page = QWidget()
            layout = QVBoxLayout(page)

            # ── 自动运行（分发后每个人自己设；保存后立即生效，不用重启）──
            layout.addWidget(QLabel("自动运行（每天几点自动跑「数据增量 + 选股建池 + 通知」）"))
            run_row = QHBoxLayout()
            self.auto_run_box = QCheckBox("每天自动运行")
            self.auto_run_box.setChecked(bool(self.cfg.auto_run))
            self.auto_run_box.setToolTip("关掉则只在点【立即选股并建池】时跑")
            run_row.addWidget(self.auto_run_box)
            run_row.addWidget(QLabel("主跑时间："))
            self.run_at_edit = QLineEdit(self.cfg.run_at)
            self.run_at_edit.setPlaceholderText("HH:MM，例如 16:00")
            self.run_at_edit.setFixedWidth(80)
            run_row.addWidget(self.run_at_edit)
            run_row.addWidget(QLabel("补跑时间："))
            self.run_at_fallback_edit = QLineEdit(getattr(self.cfg, "run_at_fallback", ""))
            self.run_at_fallback_edit.setPlaceholderText("HH:MM，例如 19:15")
            self.run_at_fallback_edit.setFixedWidth(80)
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
            for key in groups_mod.GROUP_ORDER:
                group = groups_mod.GROUPS[key]
                box = QCheckBox(
                    f"{group.label}（T+{group.horizon}）—— {group.note}"
                )
                box.setChecked(key in (self.cfg.enabled_groups or []))
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
                    box = QCheckBox(
                        f"　{group.label} / {rules_mod.strategy_label(class_name)}"
                        f"（权重 {weight}）"
                    )
                    box.setChecked(class_name in enabled_now)
                    self.strategy_boxes[class_name] = box
                    layout.addWidget(box)
            self.save_groups_button = QPushButton("保存策略组设置")
            self.save_groups_button.clicked.connect(self.on_save_groups)
            layout.addWidget(self.save_groups_button)

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

            layout.addStretch(1)
            self._refresh_channel_hints()
            self._refresh_run_hint()
            return page

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
            self.tray.setToolTip("老A法师 · 交易终端")
            menu = QMenu()
            act_show = QAction("显示主窗口", self)
            act_show.triggered.connect(self._restore_window)
            act_pool = QAction("立即选股并建池", self)
            act_pool.triggered.connect(self.on_run_pipeline)
            act_quit = QAction("退出", self)
            act_quit.triggered.connect(self._quit)
            menu.addAction(act_show)
            menu.addAction(act_pool)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda reason: self._restore_window()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
            )
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
            self._set_status("正在检查本地数据…")
            QApplication.processEvents()
            try:
                result = preflight.check(self.cfg.db_path, self.cfg)
            except Exception as exc:  # noqa: BLE001 - 自检失败不该让界面起不来
                self._set_status(f"⚠️ 数据自检失败：{type(exc).__name__}: {exc}")
                return
            self.preflight_result = result
            status = result.get("status")
            if status == preflight.READY:
                self._set_status("✅ " + preflight.summary_line(result))
                self._refresh_status()
                return
            if status == preflight.NEEDS_INCREMENTAL:
                self._refresh_status()
                if self.cfg.auto_download_on_start:
                    self._toast(f"⚠️ {preflight.summary_line(result)}；正在后台增量更新…")
                    self.on_refresh_data()
                else:
                    self._toast(f"⚠️ {preflight.summary_line(result)}；"
                                "点【只刷新数据】可立即增量更新")
                return
            # needs_full：弹首次向导
            self._set_status(f"⚠️ {result.get('reason')}")
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
            # 导入年限（默认 5 年；0 表示不限制）
            years = float(getattr(self.cfg, "history_years", 5) or 0)
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
                "提示：下载完成后会自动跑一次选股建池；也可以之后点【立即选股并建池】。"
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
                gate = data_gate(self.cfg, self.engine)
                self.preflight_result = gate.get("result") or None
                self._set_status(
                    ("✅ " if gate["ok"] else "⚠️ ") + gate["message"]
                )
                if not gate["ok"]:
                    # 下完了还不够（例如跨度/行业覆盖仍不达标）：说清原因，别硬跑
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

        def _set_status(self, text: str) -> None:
            """显示一条**瞬时消息**（不覆盖实时状态，见 `_compose_status`）。

            为什么要分两层：定时器每 5 秒重写一次状态栏；如果消息和实时状态共用同一个
            Label，任务完成/失败的提示会在零点几秒内被刷掉 —— 用户根本看不到。
            """
            self._message = text
            self._message_at = time.monotonic()
            self.status_label.setText(self._compose_status())

        def _tick(self) -> None:
            """每 5 秒刷新一次（全部包在 try 里：界面刷新失败绝不崩）。"""
            try:
                self.status_label.setText(self._compose_status())
                self._refresh_pool()
                self._refresh_watchlist()
                self._refresh_positions()
                self._refresh_alerts()
                # 概览**不在这里刷**：它有自己的 60 秒定时器与 55 秒 TTL
                # （见 `_market_tick`）—— 5 秒一轮会把配额刷掉
                self._drain_tray()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"界面刷新异常：{exc}")

        def _refresh_status(self) -> None:
            self.status_label.setText(self._compose_status())

        def _compose_status(self) -> str:
            """状态栏 = （可选的瞬时消息）+ 实时状态。"""
            summary = self.engine.summary()
            st = self.scheduler.status()
            pool_count = len(pool.load_pool(self.cfg.db_path))
            live = (
                f"引擎：{'在线' if st['running'] else '已停止'}"
                f"｜最新数据日期：{summary.get('latest_date') or '无'}"
                f"（{summary.get('symbols', 0)} 只 / {summary.get('daily_rows', 0)} 行）"
                f"｜今日池子：{pool_count} 只（含自选 {summary.get('watchlist', 0)} 只）"
                f"｜持仓浮动：{self._floating_pnl()}"
                f"｜盘中提醒："
                f"{'已暂停' if st['intraday_paused'] else ('交易时段中' if st['in_session'] else '非交易时段')}"
                f"｜定时：{st['run_at']}"
                + (f"（补跑 {st['run_at_fallback']}）" if st.get("run_at_fallback") else "")
                + f"｜下次自动运行：{st['next_run']['label']}"
                + ("" if st.get("auto_run") else "（自动运行已关闭）")
                # 正在下载：一直挂着这个提示（进度回调用 5 秒一次的定时器刷不出来，
                # 而且"下载中"这件事要盖过其它状态，否则用户以为卡死了）
                + ("｜⏬ 正在下载历史数据…" if st.get("downloading") else "")
                # 因为数据没就绪而没自动跑：说清原因（否则用户以为定时坏了）
                + (f"｜⚠️ {st['skipped_reason']}" if st.get("skipped_reason") else "")
                + (f"｜⚠️ {st['last_error']}" if st.get("last_error") else "")
            )
            # 瞬时消息保留 10 分钟（够用户看见），之后自然消失
            if self._message and time.monotonic() - self._message_at < 600:
                return f"{self._message}｜{live}"
            return live

        def _floating_pnl(self) -> str:
            """持仓浮动盈亏（用库里最新收盘价；缺价则显示 —）。"""
            try:
                with self.engine.connect() as conn:
                    positions = conn.execute(
                        "SELECT symbol, quantity, avg_cost FROM position WHERE quantity > 0"
                    ).fetchall()
                    if not positions:
                        return "无持仓"
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
                    return "无最新价"
                pnl = total_value - total_cost
                return f"{pnl:+.0f} 元（{pnl / total_cost * 100:+.2f}%）"
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"浮动盈亏计算失败：{exc}")
                return "—"

        def _refresh_pool(self) -> None:
            """刷新股票池：**卡片与表格两套视图一起填**（看当前显示哪一个）。

            只在**内容真的变了**时重建：每行/每张卡都带一个"复制条件单"按钮，每 5 秒重建
            一次会不停销毁/创建控件（闪烁、丢焦点、白白吃 CPU）—— 桌面程序里这是很显眼的毛病。
            两套视图都填的好处：切换视图只是 setVisible，不重建、不丢滚动位置与选中状态
            （池子通常十几行，多画一份的代价可以忽略）。
            """
            rows = pool.pool_table_rows(self.cfg.db_path)
            signature = tuple(
                (r["symbol"], r.get("score"), r.get("reason"), r.get("industry"),
                 r.get("source_label"), r.get("note"))
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
                self.pool_table.setItem(i, 2, QTableWidgetItem(str(row.get("label") or "")))
                self.pool_table.setItem(i, 3, QTableWidgetItem(str(row.get("source_label") or "—")))
                self.pool_table.setItem(i, 4, QTableWidgetItem(str(row.get("note") or "")))
                self.pool_table.setItem(i, 5, QTableWidgetItem(str(row.get("industry") or "")))
                self.pool_table.setItem(i, 6, QTableWidgetItem(_fmt_float(row.get("score"), 3)))
                self.pool_table.setItem(i, 7, QTableWidgetItem(plan_text))
                btn = QPushButton("复制条件单")
                btn.clicked.connect(lambda _=False, r=row, p=price: self._copy_plan(r, p))
                self.pool_table.setCellWidget(i, 8, btn)

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
                self.position_table.setItem(
                    i, 4, QTableWidgetItem(_fmt_float(cost * (1 - self.cfg.stop_loss)))
                )
                self.position_table.setItem(
                    i, 5, QTableWidgetItem(_fmt_float(cost * (1 + self.cfg.take_profit)))
                )

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
            rows = intraday.alert_rows(self.cfg.db_path, limit=100)
            self.alert_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                self.alert_table.setItem(i, 0, QTableWidgetItem(str(row.get("pushed_at") or "")))
                self.alert_table.setItem(i, 1, QTableWidgetItem(str(row.get("symbol") or "")))
                self.alert_table.setItem(i, 2, QTableWidgetItem(str(row.get("label") or row.get("kind") or "")))
                self.alert_table.setItem(i, 3, QTableWidgetItem(_fmt_float(row.get("price"))))
                self.alert_table.setItem(i, 4, QTableWidgetItem(str(row.get("detail") or "")))

        def _drain_tray(self) -> None:
            """把后台线程投递的托盘消息弹出来（跨线程只能这样传递）。"""
            from laoa_trader.notify import tray as tray_mod

            for message in tray_mod.drain(limit=5):
                self.tray.showMessage(message["title"], message["body"])
                self._tray_notified += 1

        def _toast(self, text: str) -> None:
            """轻提示：状态栏 + 托盘气泡（不用模态弹窗打断操作）。"""
            self._set_status(text)
            try:
                self.tray.showMessage("老A法师", text)
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
            self._set_status(status)

        def _on_worker_done(self, label: str, result: Any) -> None:
            self.progress.setValue(self.progress.maximum())
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
            if report.get("signals"):
                bits.append(f"写入信号 {report['signals']} 行")
            if report.get("pushed"):
                from laoa_trader.notify import summarize as _sum

                bits.append("通知：" + _sum(report.get("notify") or {}))
            elif report.get("push_skipped"):
                bits.append("未重复推送")
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
                lambda: notify_all("🧪 老A法师 · 测试提醒", lines, cfg=test_cfg),
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
            if self.scheduler.intraday_paused:
                self.scheduler.resume_intraday()
                self.btn_pause.setText("暂停盘中提醒")
            else:
                self.scheduler.pause_intraday()
                self.btn_pause.setText("恢复盘中提醒")
            self._tick()

        # ── 持仓操作 ──

        def on_add_position(self) -> None:
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

        def _quit(self) -> None:
            try:
                self.scheduler.stop()
            except Exception:  # noqa: BLE001
                pass
            self.tray.hide()
            QApplication.quit()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
            """关闭窗口 = 最小化到托盘（盯盘工具应常驻后台）。"""
            event.ignore()
            self.hide()
            try:
                self.tray.showMessage(
                    "老A法师仍在后台运行",
                    "已最小化到托盘；右键托盘图标可退出。",
                )
            except Exception:  # noqa: BLE001
                pass


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
