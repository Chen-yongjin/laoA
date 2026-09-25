"""自绘的盘中提醒浮窗（QQ 消息那种：右下角滑出来、响声、图标闪烁、点开看详情）。

为什么不用 Windows 原生 Toast
-----------------------------
实测（用户报障）：系统通知的**点击行为不受我们控制** —— 点不开、看不到内容，
在"专注助手 / 勿扰 / 通知设置"被关掉的机器上干脆一声不响。对盯盘工具来说，
"提醒出现了但点不开、看不到是哪只票、什么价"等于没提醒。
所以提醒这一路自己画一个窗口：位置、内容、点击后的行为**全在我们手里**。

设计要点
--------
- **一条提醒都不许丢**：连续到达的多条提醒**合并进同一个浮窗**（新的在最上面），
  绝不叠出一堆窗口把桌面铺满；
- **不打断用户**：`WA_ShowWithoutActivating` —— 弹出来不抢焦点（用户正在别的程序里
  打字时不会被顶掉），也不进任务栏（`Qt.Tool`）；
- **看得见就点得着**：点任意一条 → 交给主窗口弹详情（提醒原因 + 时间 + L2 条件单参数，
  就是推送里那份文本）；【查看全部】→ 主窗口的「盘中提醒」页；
- **不碍事**：到点自动消失，鼠标停上去就**不**消失（正在看的时候窗口跑了最招人烦），
  还可以直接拖到别处；
- **画法/文案只在这里**：主窗口只负责"给数据、接信号"，浮窗自己不查库、不发请求。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 浮窗宽度（像素）。**固定宽度**：几行提醒要能对齐着看，而且不能让某条提醒的
#: 说明文字特别长就把浮窗拉成一条横带（长文本在行里截断，完整内容看详情/tooltip）。
POPUP_WIDTH = 380

#: 与屏幕可用区域边缘留的余量（像素）：不贴任务栏、不贴屏幕边
POPUP_MARGIN = 12

#: 每一行文本的左右留白（外层 12×2 + 行内 8×2，见主题 QSS 里 `#alertPopup` 与行按钮）
#: + 一点保险：用来算"这一行放不放得下"，放不下就中间省略（见 `_fit`）
ROW_TEXT_PADDING = 46

#: 滑入动画时长（毫秒）：够看出来"是从右边滑进来的"，又不至于让人等
SLIDE_MS = 220

#: 标题前缀（与推送标题同一个说法：⚡ + 盘中提醒）
TITLE_PREFIX = "⚡ 盘中提醒"

#: 浮窗底部的操作提示（一句话说清"能点什么"）
FOOT_TEXT = "点任意一条看详情；鼠标停在这里不会自动消失"


def row_text(item: dict) -> str:
    """浮窗里一行提醒的文本：`名称(代码)  类型  价格`。

    与推送文本、主窗口两张表的「提醒」列**同一套写法**（**半角**括号 `名称(代码)`，
    见 `docs/开发文档.md`）：用户在三处看到的标的是同一个样子 ——
    不需要在脑子里做格式映射。
    """
    target = str(item.get("target") or item.get("symbol") or "").strip() or "—"
    kind = str(item.get("kind_label") or item.get("kind") or "").strip()
    price = str(item.get("price_text") or "").strip()
    return "  ".join(part for part in (target, kind, price) if part)


def _fit(row: QPushButton, text: str) -> str:
    """把行文本收进浮窗宽度：超了就**中间**省略（保住开头的名称/代码与结尾的价格）。

    手动量宽度是因为 `QPushButton` 不会自己省略 —— 文本超了直接被裁掉，
    用户看到的是"某只名字很长的股票（6889"，比省略号更像 bug。
    """
    available = max(60, POPUP_WIDTH - ROW_TEXT_PADDING)
    metrics = QFontMetrics(row.font())
    if metrics.horizontalAdvance(text) <= available:
        return text
    return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, available)


class AlertPopup(QFrame):
    """右下角滑入的提醒列表浮窗（顶层窗口，不占任务栏）。

    Signals:
        item_clicked(dict): 点了某一条（参数就是传进来的那一条 item，主窗口据此弹详情）。
        view_all_clicked(): 点了【查看全部】。
        closed(): 点了【关闭】（或认为"用户已经看到了"）。

    为什么用 `QFrame` 而不是 `QWidget`：QSS 里给 `background-color` 时，
    `QWidget` 的子类默认**不画**背景（要么加 `WA_StyledBackground`，要么继承 QFrame），
    用 QFrame 最省事，也符合卡片那几处的做法。
    """

    item_clicked = Signal(dict)
    view_all_clicked = Signal()
    closed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        max_items: int = 5,
        seconds: int = 8,
        animate: bool = True,
    ) -> None:
        super().__init__(parent)
        # 无边框 + 置顶 + Qt.Tool：Tool 不进任务栏（浮窗不是"一个程序窗口"）
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # 弹出来**不抢焦点**：用户可能在别的程序里打字，被一个提示窗顶掉输入是最烦的
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("alertPopup")
        self.setFixedWidth(POPUP_WIDTH)

        self.max_items = max(1, int(max_items))
        self.seconds = max(1, int(seconds))
        self.animate = bool(animate)

        #: 当前列出的提醒（新的在前面）；**合并**用的就是它
        self._items: list[dict] = []
        #: 每一行对应的按钮（重建行时替换；测试直接点它）
        self.rows: list[QPushButton] = []
        #: 鼠标是否停在浮窗上（停着就不自动消失）
        self._hovered = False
        #: 拖动状态与按下时的偏移
        self._dragging = False
        self._drag_offset = QPoint()
        #: 滑入动画的目标位置（`target_pos()` 算出来的右下角）
        self._target = QPoint()

        self._build()

        # 自动消失：单次定时器；鼠标进来就 stop、出去再 start（见 `enterEvent`）
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_popup)

        # 滑入动画（`QPropertyAnimation` 直接驱动窗口的 pos）
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ── 界面 ──

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self.title_label = QLabel(TITLE_PREFIX, self)
        self.title_label.setObjectName("alertPopupTitle")
        outer.addWidget(self.title_label)

        # 行容器：每次重建行（行数很少，重建比"复用+挪位"简单可靠）
        self._rows_host = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        outer.addWidget(self._rows_host)

        self.foot_label = QLabel(FOOT_TEXT, self)
        self.foot_label.setObjectName("alertPopupFoot")
        self.foot_label.setWordWrap(True)
        outer.addWidget(self.foot_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_all = QPushButton("查看全部", self)
        self.btn_all.setToolTip("打开主窗口的「盘中提醒」页（这里只列最近几条）")
        self.btn_all.clicked.connect(self._on_view_all)
        self.btn_close = QPushButton("关闭", self)
        self.btn_close.setToolTip("关掉这个浮窗（提醒已经记在「盘中提醒」页里，不会丢）")
        self.btn_close.clicked.connect(self._on_close)
        buttons.addWidget(self.btn_all)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_close)
        outer.addLayout(buttons)

    def _rebuild_rows(self) -> None:
        """按 `self._items` 重建行按钮（文本 = `名称(代码) 类型 价格`）。"""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        for alert in self._items:
            text = row_text(alert)
            row = QPushButton(self._rows_host)
            row.setObjectName("alertPopupRow")
            row.setFlat(True)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            # 行短了不好看、长了会被**硬裁**（按钮不会自己省略号化）：
            # 长名字 + 长类型 + 5 位价这种组合真会出现，所以自己按可用宽度做**中间省略**
            # （保住开头的名称和结尾的价格），完整文本仍然放在 tooltip 里
            row.setText(_fit(row, text))
            row.setToolTip(text)
            row.clicked.connect(lambda _=False, item=dict(alert): self._on_row_clicked(item))
            self._rows_layout.addWidget(row)
            self.rows.append(row)
        self.title_label.setText(
            f"{TITLE_PREFIX} · {len(self._items)} 条" if self._items else TITLE_PREFIX
        )

    # ── 数据 ──

    def items(self) -> list[dict]:
        """当前列出的提醒（副本，改它不会动浮窗）。"""
        return [dict(item) for item in self._items]

    @staticmethod
    def _key(item: dict) -> tuple:
        """同一条提醒的判据：日期 + 标的 + 类型（与库里去重键同一个口径）。"""
        return (
            str(item.get("date") or ""),
            str(item.get("symbol") or item.get("target") or ""),
            str(item.get("kind") or ""),
        )

    def show_alerts(self, items: list[dict]) -> bool:
        """把新提醒并进浮窗并弹出来（已有内容则**合并**，不新开窗口）。

        Args:
            items: 新的提醒（每条的 `target` 已由调用方拼好 `名称(代码)`）。

        Returns:
            真的弹了 → True；`items` 是空的 → False（**不弹空浮窗**）。
        """
        fresh = [dict(item) for item in (items or []) if item]
        if not fresh:
            # 没有新提醒就什么都不做：闪一个空窗口出来只会让人以为出错了
            return False
        if not self.isVisible():
            # 浮窗已经收起了（用户看过了 / 到点自动消失了）→ 这一次只显示**新来的**：
            # 把一小时前的那条又翻出来，用户会以为它是刚刚发生的
            self._items = []
        fresh_keys = {self._key(item) for item in fresh}
        # 合并：新的排最上面，旧的（不在新一批里的）接在后面；同一条只留一份
        merged = fresh + [item for item in self._items if self._key(item) not in fresh_keys]
        self._items = merged[: self.max_items]
        self._rebuild_rows()
        self.show_popup()
        return True

    def show_popup(self) -> None:
        """按当前内容显示（重新定位 + 滑入 + 起自动消失计时）。"""
        # 已经在屏幕上的（又来了一条提醒 → 合并进同一个浮窗）：**不重新滑入**，
        # 只就地变高/变矮并重新计时 —— 每来一条就滑一次，看着像在抖
        already_visible = self.isVisible()
        self.adjustSize()
        self._target = self.target_pos()
        if self.animate and not already_visible:
            # 滑入：**先落位再显示**。先 show 再 move 的话，窗口会有那么一帧出现在系统
            # 默认位置（左上角），既闪一下又可能"路过"鼠标把自动消失暂停掉
            # （实测在 offscreen 平台上就会）。
            start = QPoint(self._target.x() + self.width() + POPUP_MARGIN, self._target.y())
            self.move(start)
            self.show()
            self.raise_()
            self._slide.stop()
            self._slide.setStartValue(start)
            self._slide.setEndValue(self._target)
            self._slide.start()
        else:
            self._slide.stop()
            self.move(self._target)
            self.show()
            self.raise_()
        self._hovered = False
        self.start_hide_timer()

    def settle(self) -> None:
        """跳过动画立刻落到目标位置（测试与"用户手快点到"都用得上）。"""
        self._slide.stop()
        if not self._target.isNull():
            self.move(self._target)

    def target_pos(self) -> QPoint:
        """浮窗该在的位置：**鼠标所在屏幕**可用区域的右下角（已扣掉任务栏）。

        为什么用 `availableGeometry()` 而不是 `geometry()`：后者含任务栏与
        "贴边自动吸附"的边界，浮窗会正好压在任务栏上；多显示器时按鼠标位置
        选屏幕 —— 否则第二个屏上的用户永远看不到浮窗（跑到主屏去了）。
        """
        screen = self._screen()
        if screen is None:  # 极端环境（没有屏幕信息）：退回左上角，绝不崩
            return QPoint(POPUP_MARGIN, POPUP_MARGIN)
        area = screen.availableGeometry()
        x = area.right() - self.width() - POPUP_MARGIN + 1
        y = area.bottom() - self.height() - POPUP_MARGIN + 1
        return QPoint(max(area.left(), x), max(area.top(), y))

    @staticmethod
    def _screen() -> Any:
        """鼠标当前所在的屏幕（取不到就主屏，再取不到 None）。"""
        try:
            return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        except Exception as exc:  # noqa: BLE001 - 没有屏幕信息不该让提醒失败
            logger.debug(f"取屏幕失败（浮窗位置退回兜底）：{exc}")
            return None

    # ── 自动消失 ──

    def start_hide_timer(self) -> None:
        """起"到点自己消失"的计时；鼠标停在浮窗上时**不起**（别把正在看的窗口收走）。"""
        if self._hovered:
            return
        self.hide_timer.start(self.seconds * 1000)

    def is_hovered(self) -> bool:
        """鼠标是不是停在浮窗上（停着就不自动消失）。"""
        return self._hovered

    def hide_popup(self) -> None:
        """收起浮窗（不改 `self._items`：下次【最近提醒】还能看到刚才那几条）。"""
        self.hide_timer.stop()
        self._slide.stop()
        self.hide()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """鼠标进来 → 暂停自动消失（用户正在看）。"""
        self._hovered = True
        self.hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """鼠标离开 → 重新开始计时（不是立刻消失，还能回来接着看）。"""
        self._hovered = False
        self.start_hide_timer()
        super().leaveEvent(event)

    # ── 交互 ──

    def _on_row_clicked(self, item: dict) -> None:
        """点某一条 → 收起浮窗并请主窗口弹详情。

        为什么不留在原地：详情窗本身就带着完整信息（原因 + 时间 + L2 条件单参数），
        两个窗口叠着看反而乱；详情窗里也有【查看全部】，信息不会丢。
        """
        self.hide_popup()
        self.item_clicked.emit(item)

    def _on_view_all(self) -> None:
        self.hide_popup()
        self.view_all_clicked.emit()

    def _on_close(self) -> None:
        self.hide_popup()
        self.closed.emit()

    # 拖动：只有"没被行按钮盖住"的地方（标题栏、四周留白）能拖 —— 与 QQ 一致，
    # 也避免了"想点某一行的详情，结果把窗口拖走了"
    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        self._dragging = False
        super().mouseReleaseEvent(event)
