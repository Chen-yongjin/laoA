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

from laoa_trader import intraday, pool
from laoa_trader.config import Config, get_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger
from laoa_trader.notify import KINDS, summarize
from laoa_trader.scheduler import Scheduler, refresh_data, run_daily
from laoa_trader.strategy import rules as rules_mod

logger = get_logger(__name__)

try:  # Qt 缺失时必须优雅降级（Linux 开发机、精简环境）
    from PySide6.QtCore import QThread, QTimer, Signal
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
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

    class Worker(QThread):
        """通用工作线程：把可调用对象丢到后台跑，结果通过信号回主线程。

        为什么要这样：PySide6 里**只有主线程能碰控件**。下载 10 年数据要十几分钟，
        直接在按钮回调里跑会"窗口未响应"（Windows 还会弹"程序无响应"）。
        """

        progress = Signal(str, int, int)
        finished_ok = Signal(object)
        failed = Signal(str)

        stage = Signal(str)

        def __init__(self, fn, *args, with_progress: bool = False,
                     with_stage: bool = False, **kwargs) -> None:
            super().__init__()
            self._fn = fn
            self._args = args
            self._kwargs = kwargs
            self._with_progress = with_progress
            self._with_stage = with_stage

        def run(self) -> None:  # noqa: D102
            try:
                kwargs = dict(self._kwargs)
                if self._with_progress:
                    kwargs["progress_cb"] = self._emit_progress
                if self._with_stage:
                    kwargs["stage_cb"] = self.stage.emit
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
            self._tray_notified = 0
            #: 状态栏的瞬时消息（任务完成/失败提示），与实时状态分两层显示
            self._message = ""
            self._message_at = 0.0
            #: 池子表格的内容指纹（内容没变就不重建控件）
            self._pool_signature: tuple = ()
            #: 启动自检结果（三态）与首次向导
            self.preflight_result: dict | None = None
            self.wizard: Any = None
            self._cancel_download = False

            self.setWindowTitle("老A法师 · 交易终端（Windows 单机版）")
            self.resize(1120, 720)
            self._build_ui()
            self._build_tray()
            self._start_scheduler()

            # 界面刷新定时器：状态栏 + 托盘消息 + 提醒列表
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(5000)
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
            layout.addLayout(buttons)

            self.progress = QProgressBar()
            self.progress.setValue(0)
            layout.addWidget(self.progress)

            # 三个表：池子 / 持仓 / 提醒
            self.tabs = QTabWidget()
            self.pool_table = QTableWidget(0, 9)
            # 「来源」列把"策略组别"和"自选/策略+自选"合成一列（避免两列重复信息）
            self.pool_table.setHorizontalHeaderLabels(
                ["代码", "名称", "来源策略", "来源", "备注", "热门行业", "分数",
                 "条件单参数", "复制"]
            )
            self._stretch(self.pool_table)
            self.tabs.addTab(self.pool_table, "股票池")

            self.position_table = QTableWidget(0, 6)
            self.position_table.setHorizontalHeaderLabels(
                ["代码", "名称", "数量", "成本", "止损", "止盈"]
            )
            self._stretch(self.position_table)
            self.tabs.addTab(self.position_table, "持仓")

            self.alert_table = QTableWidget(0, 5)
            self.alert_table.setHorizontalHeaderLabels(["时间", "代码", "类型", "价格", "说明"])
            self._stretch(self.alert_table)
            self.watch_table = QTableWidget(0, 5)
            self.watch_table.setHorizontalHeaderLabels(
                ["代码", "名称", "备注", "状态", "是否已进池"]
            )
            self._stretch(self.watch_table)
            self.tabs.addTab(self.watch_table, "自选股")

            self.tabs.addTab(self.alert_table, "盘中提醒")

            # 设置页：策略组自选 + 通知方式自选（都写回 config.toml，保留注释）
            self.tabs.addTab(self._build_settings_tab(), "设置")

            layout.addWidget(self.tabs)

            # 持仓操作行
            pos_row = QHBoxLayout()
            self.pos_symbol = QLineEdit()
            self.pos_symbol.setPlaceholderText("代码（6 位）")
            self.pos_qty = QLineEdit()
            self.pos_qty.setPlaceholderText("数量（股）")
            self.pos_cost = QLineEdit()
            self.pos_cost.setPlaceholderText("成本价")
            btn_add = QPushButton("添加持仓")
            btn_add.clicked.connect(self.on_add_position)
            btn_del = QPushButton("删除持仓")
            btn_del.clicked.connect(self.on_delete_position)
            for widget in (self.pos_symbol, self.pos_qty, self.pos_cost):
                pos_row.addWidget(widget)
            pos_row.addWidget(btn_add)
            pos_row.addWidget(btn_del)
            pos_row.addStretch(1)
            layout.addLayout(pos_row)

            # 自选股：代码 / 备注 / 添加·删除·启用·停用（与策略标的并列进池，一起盯）
            watch_row = QHBoxLayout()
            self.watch_symbol = QLineEdit()
            self.watch_symbol.setPlaceholderText("代码（6 位）")
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
                watch_row.addWidget(widget)
            for btn in (btn_watch_add, btn_watch_del, btn_watch_on, btn_watch_off):
                watch_row.addWidget(btn)
            watch_row.addStretch(1)
            layout.addLayout(watch_row)

            self.setCentralWidget(central)

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
            """托盘图标：关闭窗口只最小化，不退出。"""
            icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
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
            self.setWindowIcon(icon)
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
                lambda progress_cb: sync.download_history(
                    self.cfg, progress_cb=progress_cb,
                    should_stop=lambda: self._cancel_download,
                ),
                "下载历史数据",
                with_progress=True,
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
            """刷新股票池表。

            只在**内容真的变了**时重建：每行都带一个"复制条件单"按钮，每 5 秒重建一次
            会不停销毁/创建控件（闪烁、丢焦点、白白吃 CPU）—— 桌面程序里这是很显眼的毛病。
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
            self.pool_table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                price = self._last_price(row["symbol"])
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
                        with_stage: bool = False) -> None:
            self.progress.setValue(0)
            self._set_status(f"{label}…")
            worker = Worker(fn, with_progress=with_progress, with_stage=with_stage)
            self._worker = worker
            worker.progress.connect(self._on_progress)
            worker.stage.connect(lambda name: self._set_status(f"{label}：{name}…"))
            worker.finished_ok.connect(lambda result: self._on_worker_done(label, result))
            worker.failed.connect(lambda msg: self._on_worker_failed(label, msg))
            worker.start()

        def _on_progress(self, stage: str, done: int, total: int) -> None:
            self.progress.setFormat(f"{stage} %p%")
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(min(done, max(total, 1)))
            # 首次向导里也有自己的进度条（下载时用户盯的是那个窗口）
            if self.wizard is not None and self.wizard.isVisible():
                try:
                    self.wizard_progress.setRange(0, max(total, 1))
                    self.wizard_progress.setValue(min(done, max(total, 1)))
                    self.wizard_status.setText(f"{stage}：{done}/{total}")
                except Exception:  # noqa: BLE001 - 向导可能已被关掉
                    pass
            self._set_status(f"{stage}：{done}/{total}")

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
            """【立即选股并建池】：增量数据 → 策略 → 建池 → 按通知设置推送。"""
            if self._busy():
                return

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
                lambda progress_cb: sync.download_history(
                    self.cfg, progress_cb=progress_cb
                ),
                "下载/更新历史数据",
                with_progress=True,
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
    window = MainWindow(cfg)
    window.show()
    return int(app.exec())


def main(cfg: Config | None = None) -> int:
    """兼容入口。"""
    return run_gui(cfg)


__all__ = ["QT_AVAILABLE", "MainWindow", "run_gui", "main"]
