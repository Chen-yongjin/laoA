"""「软件授权」对话框：机器码 + 注册码输入 + 【注册】。

用户原话
--------
> 「做个注册机给我，输入机器码就能算出注册码。机器码下面加上注册码输入口和注册按键，点击可以注册。」
> 「策略编辑锁住，点击提醒需要授权，请联系作者wx：q352162」
> 「免费运行7天，到期打开同样授权提醒。」

所以这个对话框要在**一屏里**把用户需要的东西给全（他的用户会照着念给我听）：

1. **机器码**（可选中、可一键复制）—— 用户把它发我，我算出注册码发回去；
2. **注册码输入口 + 【注册】**（就在机器码下面，用户明确要求的位置）；
3. **联系方式**（`licensing.CONTACT_TEXT`，一个字都不改）；
4. 当前状态：试用还剩几天 / 未授权的原因。

为什么非模态（`show()` 而不是 `exec()`）
----------------------------------------
用户点【策略编辑】被拦下时，这个窗口是"提示 + 输入口"；他可能想先去别处看看机器码
再回来，也可能根本不注册（关掉继续用试用剩下的天数）。`exec()` 会把主窗口整个按住，
且自动化测试里会直接挂死（项目里【关于】就是这么处理的，理由一致）。
"""

from __future__ import annotations

import threading
from typing import Any

from laoa_trader import licensing
from laoa_trader.log import get_logger

logger = get_logger(__name__)

try:
    from PySide6.QtCore import Qt, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QVBoxLayout,
    )

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 与 ui/app.py 同一个降级策略
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)

#: 对话框标题（软件名不写死在这里：调用方把 `title` 传进来，避免与 `APP_NAME` 两处漂移）
DEFAULT_TITLE = "软件授权"

#: 机器码还没算出来时那一栏显示的占位（主人 2026-09-21："客户要注册的时候再去读"）
MACHINE_PLACEHOLDER = "正在读取…"

#: 顶部说明的两句话（按状态二选一）——**一眼看出"我该怎么办"**
INTRO_TRIAL = "本软件免费试用 {days} 天。把下面的机器码发给作者，可以换成永久授权。"
INTRO_EXPIRED = "免费试用已到期。把下面的机器码发给作者换取注册码，填进下面就能继续用。"

#: 注册码输入框的占位（形如 `XXXX-XXXX-XXXX-XXXX`）
CODE_PLACEHOLDER = "XXXX-XXXX-XXXX-XXXX"


if QT_AVAILABLE:

    class LicenseDialog(QDialog):
        """授权对话框（非模态）。"""

        #: 注册成功 → 调用方（主窗口）据此解锁界面
        registered = Signal()
        #: 机器码算好了（**在后台线程里算的**，见 `_start_machine_lookup`）→ 回主线程填界面
        _machine_ready = Signal(str)

        def __init__(self, cfg: Any = None, parent: Any = None, *,
                     title: str = DEFAULT_TITLE, status: dict | None = None) -> None:
            super().__init__(parent)
            self.cfg = cfg
            self.setWindowTitle(title)
            self.setMinimumWidth(520)

            # ⚠️ 这里**不读机器码**（主人 2026-09-21："不用每次都读机器码啊，客户要注册的
            # 时候再去读"）。读一次要起 PowerShell 问硬件（几百毫秒），而打开对话框本身
            # 不该卡；所以先把界面显示出来，机器码那一栏写"正在读取…"，
            # 后台线程算完再填上（同一进程里只算一次，`licensing.machine_code` 自己缓存）。
            # 校验"这份注册码是不是本机的"也放在拿到机器码之后做。
            self.status = status if isinstance(status, dict) else licensing.license_status(cfg)
            self.machine = str(self.status.get("machine") or "")
            self._machine_ready.connect(self._on_machine_ready)

            layout = QVBoxLayout(self)

            # ── 当前状态 + 一句"下一步做什么" ──
            self.status_label = QLabel(self._status_text())
            self.status_label.setWordWrap(True)
            font = self.status_label.font()
            font.setBold(True)
            self.status_label.setFont(font)
            layout.addWidget(self.status_label)

            self.intro_label = QLabel(self._intro_text())
            self.intro_label.setWordWrap(True)
            layout.addWidget(self.intro_label)

            # ── 机器码（可选中 + 一键复制）──
            layout.addWidget(QLabel("机器码（把它发给作者）："))
            machine_row = QHBoxLayout()
            self.machine_label = QLabel(self.machine or MACHINE_PLACEHOLDER)
            self.machine_label.setObjectName("licenseMachine")
            self.machine_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            machine_font = self.machine_label.font()
            machine_font.setPointSize(max(1, machine_font.pointSize() + 2))
            machine_font.setBold(True)
            self.machine_label.setFont(machine_font)
            machine_row.addWidget(self.machine_label, 1)
            self.btn_copy_machine = QPushButton("复制机器码")
            self.btn_copy_machine.clicked.connect(self.on_copy_machine)
            machine_row.addWidget(self.btn_copy_machine)
            layout.addLayout(machine_row)

            # ── 注册码输入 + 注册（用户要求就在机器码下面）──
            layout.addWidget(QLabel("注册码（作者给你的那一串）："))
            code_row = QHBoxLayout()
            self.code_edit = QLineEdit()
            self.code_edit.setPlaceholderText(CODE_PLACEHOLDER)
            self.code_edit.setToolTip(
                "形如 XXXX-XXXX-XXXX-XXXX；大小写、有没有连字符都认"
            )
            self.code_edit.returnPressed.connect(self.on_register)
            code_row.addWidget(self.code_edit, 1)
            self.btn_register = QPushButton("注册")
            self.btn_register.setObjectName("primaryAction")
            self.btn_register.clicked.connect(self.on_register)
            code_row.addWidget(self.btn_register)
            layout.addLayout(code_row)

            # ── 结果提示（注册失败的中文原因 / 成功那句）──
            self.hint_label = QLabel("")
            self.hint_label.setWordWrap(True)
            self.hint_label.setObjectName("statusTag")
            layout.addWidget(self.hint_label)

            # ── 联系方式（用户给定的原文，**一个字都不改**）──
            self.contact_label = QLabel(licensing.CONTACT_TEXT)
            self.contact_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(self.contact_label)

            bottom = QHBoxLayout()
            bottom.addStretch(1)
            self.btn_close = QPushButton("关闭")
            self.btn_close.clicked.connect(self.close)
            bottom.addWidget(self.btn_close)
            layout.addLayout(bottom)

        # ── 文案 ──────────────────────────────────────────────────────

        def _status_text(self) -> str:
            """当前状态那一行（与「关于」里的说法同源：`licensing.status_text`）。"""
            return licensing.status_text(self.cfg)

        def _intro_text(self) -> str:
            """顶部那句"下一步做什么"：试用中 / 已到期两种措辞。"""
            if self.status.get("licensed"):
                days = int(self.status.get("days_left") or licensing.TRIAL_DAYS)
                return INTRO_TRIAL.format(days=days)
            return INTRO_EXPIRED

        # ── 动作 ──────────────────────────────────────────────────────

        def showEvent(self, event: Any) -> None:
            """一显示就开始在后台读机器码（用户看到的先是"正在读取…"）。"""
            super().showEvent(event)
            self._start_machine_lookup()

        def _start_machine_lookup(self) -> None:
            """在**后台线程**里算机器码（算完通过信号回主线程填界面）。

            为什么必须后台：Windows 上算一次要起 PowerShell 问硬件（几百毫秒到一两秒），
            放主线程里就是"打开授权对话框时顿一下"。界面先显示出来、这一栏写
            "正在读取…"，算完再填 —— 用户看到的是"在读"，不是"卡住了"。
            """
            if self.machine:
                return                       # 已经有值（调用方传进来的状态里带了机器码）
            try:
                threading.Thread(
                    target=self._lookup_machine, daemon=True, name="license-machine"
                ).start()
            except Exception as exc:  # noqa: BLE001 - 起不了线程就退回同步算（宁可顿一下）
                logger.debug(f"机器码查询线程没起来，改为同步：{exc}")
                self._on_machine_ready(licensing.machine_code())

        def _lookup_machine(self) -> None:
            """线程侧：算机器码 + 顺便校验一次"这份授权是不是本机的"。"""
            try:
                machine = licensing.machine_code()
            except Exception as exc:  # noqa: BLE001 - 算不出来也要给个说法
                logger.warning(f"机器码算不出来：{exc}")
                self._machine_ready.emit("")
                return
            # 拿到机器码之后才校验注册码与本机是否匹配（`verify_machine=True`）：
            # 这正是"要注册的时候才读机器码"的落点 —— 平时那条路一次都不读。
            try:
                self.status = licensing.license_status(self.cfg, verify_machine=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"校验授权状态失败：{exc}")
            self._machine_ready.emit(machine)

        @Slot(str)
        def _on_machine_ready(self, machine: str) -> None:
            """主线程：把机器码填进那一栏（并把校验后的状态刷新一次）。"""
            self.machine = str(machine or "")
            if self.machine:
                self.machine_label.setText(self.machine)
                self.btn_copy_machine.setEnabled(True)
            else:
                self.machine_label.setText("（没读出来，可点【重新读取】）")
            try:
                self.status_label.setText(self._status_text())
                self.intro_label.setText(self._intro_text())
            except Exception as exc:  # noqa: BLE001 - 只是文案，出错别带崩对话框
                logger.debug(f"刷新授权说明失败：{exc}")

        def on_copy_machine(self) -> None:
            """复制机器码（用户要发微信，手抄 16 位很容易错）。"""
            QGuiApplication.clipboard().setText(self.machine)
            # 成功不弹提示（用户 2026-09-18 的口径：操作确认不打扰）——
            # 就地换一下按钮文字，看得见又不打断
            self.btn_copy_machine.setText("已复制")
            self.hint_label.setText("机器码已复制，发给作者即可。")

        def on_register(self) -> None:
            """【注册】：校验 → 写授权状态 → 成功就发信号（主窗口据此解锁）。"""
            ok, message = licensing.register(self.machine, self.code_edit.text(), self.cfg)
            self.hint_label.setText(("✅ " if ok else "❌ ") + message)
            if not ok:
                self.code_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                return
            # 成功：刷新状态显示（"已注册"），再通知外面
            self.status = licensing.license_status(self.cfg)
            self.status_label.setText(self._status_text())
            self.intro_label.setText(self._intro_text())
            self.registered.emit()

        def refresh(self) -> None:
            """重新读一次授权状态（外面改过状态文件时用）。"""
            self.status = licensing.license_status(self.cfg)
            self.status_label.setText(self._status_text())
            self.intro_label.setText(self._intro_text())


__all__ = [
    "CODE_PLACEHOLDER",
    "DEFAULT_TITLE",
    "INTRO_EXPIRED",
    "INTRO_TRIAL",
    "LicenseDialog",
    "QT_AVAILABLE",
]
