"""桌宠：常驻桌面的一张小卡片，有消息时冒气泡 + 蹦两下（用户 2026-09-18 要求）。

用户原话
--------
> "可不可以编写个机器人，直接中文语音提醒。像一个桌宠一样的，软件隐藏时停留在桌面，
>   有消息时大声喊出消息内容。"

所以这一页只有三件事：

1. **软件隐藏时它还在**：无边框 + 置顶 + `Qt.Tool`（不进任务栏）的顶层窗口，
   **不挂在主窗口的可见性上** —— 主窗口最小化/隐藏，它照样在桌面上蹲着。
   构造时收一个 parent，**只是为了"主窗口销毁时跟着销毁"**，不是为了显示。
2. **有消息时冒气泡**：`名称(代码) + 类型 + 一句话`（过长截断、tooltip 给全文），
   同时**上下蹦两下**（QTimer 改 y 偏移，不做复杂动画 —— 桌宠只是提醒，不是玩具）。
3. **能点**：双击 = 打开「消息」列表；右键 = 【消息（N）】【试喊一条】
   【静音一小时/取消静音】【藏起来】。

形象
----
用户给的图（`assets/pet.png`，由 `build/make_pet_asset.py` 缩到 512×512 随包）。
**画法看素材有没有透明通道**，一个判据管两头：

* **有 alpha（现在这版）** → 「**自由站立的角色**」：窗口背景本来就是透明的
  （`WA_TranslucentBackground`），这里**不画**卡片底、圆角、边框、阴影，
  直接把图片贴上去（角色的轮廓就是它的形状）；
* **没有 alpha（2026-09-18 那版原图就是这种）** → 退回**圆角卡片**：
  那一版四周是不透明的红金花纹，硬抠会抠出一圈脏边，所以给它一块圆角底。

判据是 `QPixmap.hasAlphaChannel()`，在加载那一步算好存进 `self._transparent`。
用户 2026-09-20 换成透明底那版素材时**一行代码都没改**，靠的就是这个判据；
将来再换回不透明的图，也会自动回到卡片式。

素材缺失时**不能开不起来**：退化成 QPainter 自己画的小圆脸（本项目一贯的降级口径）。

与其它模块的关系
----------------
这一页**不查库、不发请求、不出声**：它只负责"显示 + 把用户的动作变成信号"。
声音归 `notify/voice.py`，消息列表归 `ui/message_center.py`，主窗口负责把三者接起来。
"""

from __future__ import annotations

from typing import Any

from laoa_trader import assets
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 桌宠的显示边长（像素）。用户给的图是方图，所以宽高同值
PET_SIZE = 180

#: 气泡高度（像素）：小家伙头顶那一块
BUBBLE_HEIGHT = 46

#: 卡片圆角（像素）
CARD_RADIUS = 18

#: 气泡最多显示多少字（长文本截断，全文进 tooltip —— 桌宠是"看一眼"的东西）
BUBBLE_MAX_CHARS = 34

#: 气泡自己消失的秒数（用户没点它的话）
BUBBLE_SECONDS = 8

#: 蹦一下的高度（像素）与次数
HOP_HEIGHT = 12
HOP_TIMES = 2

try:  # Qt 缺失时不该 import 就炸（与 ui/app.py 同一个约定）
    from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (
        QAction,
        QColor,
        QFont,
        QPainter,
        QPainterPath,
        QPixmap,
    )
    from PySide6.QtWidgets import QLabel, QMenu, QWidget

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


def clip_text(text: str, limit: int = BUBBLE_MAX_CHARS) -> str:
    """超长截断（桌宠冒的是"一句话"，全文留在 tooltip 里）。"""
    body = str(text or "")
    return body if len(body) <= limit else body[: max(1, limit - 1)] + "…"


if QT_AVAILABLE:

    class DesktopPet(QWidget):
        """桌宠本体（见模块 docstring）。

        属性里刻意留着测试要用的引用（`bubble` / `menu_actions()`），
        不要去爬控件层级 —— 一改布局那些断言就集体失效（与 `FormulaPage` 同一个约定）。
        """

        #: 双击 → 打开「消息」列表
        activated = Signal()
        #: 右键【试喊一条】→ 走一遍真实链路（响声 + 气泡 + 朗读 + 进消息列表）
        test_requested = Signal()
        #: 右键【静音一小时】/【取消静音】→ 参数是秒数（0 = 取消）
        mute_requested = Signal(int)
        #: 右键【藏起来】→ 由主窗口决定"藏"这件事（它管配置与设置页那一栏的状态）
        hide_requested = Signal()
        #: 拖动结束 → (x, y)，主窗口负责写进 config.toml
        moved = Signal(int, int)
        #: 气泡被点掉
        bubble_clicked = Signal()

        def __init__(self, parent: Any = None, *, size: int = PET_SIZE) -> None:
            super().__init__(parent)
            # Tool + 置顶 + 无边框：不进任务栏、不被别的窗口盖住、没有标题栏
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setWindowTitle("老A的桌宠")
            self.size_ = int(size)
            self.setFixedSize(self.size_, self.size_ + BUBBLE_HEIGHT)
            #: 素材（没有素材时是 None → 手画降级，见 `_load_pet`）
            self._pixmap = self._load_pet()
            #: 素材**有没有透明通道** —— 决定它是「自由站立的角色」还是「圆角卡片」，
            #: 见 `paintEvent`。加载时判一次就够（素材不会中途变）。
            self._transparent = (
                self._pixmap is not None and self._pixmap.hasAlphaChannel()
            )
            self._unread = 0
            self._drag_from: Any = None
            self._hop_left = 0
            self._hop_dir = -1
            self._hop_timer = QTimer(self)
            self._hop_timer.timeout.connect(self._hop_step)
            self._bubble_timer = QTimer(self)
            self._bubble_timer.setSingleShot(True)
            self._bubble_timer.timeout.connect(self.hide_bubble)
            self._build_bubble()
            self.setToolTip("老A的桌宠：双击看消息，右键有菜单")

        # ── 素材 ─────────────────────────────────────────────────────

        @staticmethod
        def _load_pet() -> Any:
            """读桌宠素材（读不出来返回 None → 自己画一个）。"""
            path = assets.pet_png()
            if path is None:
                logger.info("桌宠素材缺失（assets/pet.png），改用手画的小家伙")
                return None
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                logger.warning(f"桌宠素材读不出来：{path}")
                return None
            return pixmap

        # ── 气泡 ─────────────────────────────────────────────────────

        def _build_bubble(self) -> None:
            """气泡是独立子控件（放在小家伙头顶），点一下收起来。"""
            self.bubble = QLabel("", self)
            self.bubble.setObjectName("petBubble")
            self.bubble.setWordWrap(True)
            self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.bubble.setGeometry(0, 0, self.size_, BUBBLE_HEIGHT)
            self.bubble.setStyleSheet(
                "QLabel#petBubble { background: rgba(255, 250, 240, 235);"
                " border: 2px solid #c8a24a; border-radius: 10px; padding: 4px 6px; }"
            )
            font = QFont(self.bubble.font())
            font.setPointSize(max(8, font.pointSize()))
            font.setBold(True)          # 气泡要"一眼看清"，加粗（名称加黑是用户的老要求）
            self.bubble.setFont(font)
            self.bubble.setVisible(False)
            self.bubble.setCursor(Qt.CursorShape.PointingHandCursor)
            self.bubble.installEventFilter(self)

        def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt 命名
            """气泡被点一下 → 收起来（"我看完了"）。

            为什么用事件过滤器而不是 `mousePressEvent = ...` 赋值：后者是给实例塞一个
            函数属性，Qt 不会走信号/事件那套流程（某些平台下不触发，还会让 `hasattr`
            之类的判断变得诡异）。过滤器是 Qt 认的正规做法。
            """
            if watched is getattr(self, "bubble", None) and \
                    event.type() == QEvent.Type.MouseButtonPress:
                self.hide_bubble()
                self.bubble_clicked.emit()
                return True
            return super().eventFilter(watched, event)

        def show_bubble(self, text: str, *, seconds: int = BUBBLE_SECONDS) -> None:
            """冒一个气泡（过长截断，全文进 tooltip）。"""
            body = " ".join(str(text or "").split())
            self.bubble.setToolTip(body)
            self.bubble.setText(clip_text(body))
            self.bubble.setVisible(True)
            self.bubble.raise_()
            self.update()
            self._bubble_timer.start(max(1, int(seconds)) * 1000)

        def hide_bubble(self) -> None:
            self.bubble.setVisible(False)
            self.update()

        def bubble_text(self) -> str:
            """气泡当前显示的文本（测试用；不含被截掉的部分）。"""
            return self.bubble.text()

        # ── 未读 ─────────────────────────────────────────────────────

        def set_unread(self, count: int) -> None:
            """未读数（tooltip 与右键菜单里那个 (N) 用）。"""
            self._unread = max(0, int(count))
            base = "老A的桌宠：双击看消息，右键有菜单"
            self.setToolTip(base if not self._unread else f"{base}（未读 {self._unread} 条）")

        def unread_count(self) -> int:
            return self._unread

        # ── 蹦两下 ───────────────────────────────────────────────────

        def hop(self, *, times: int = HOP_TIMES) -> None:
            """上下蹦两下（就是改 y 偏移，不做复杂动画）。"""
            self._hop_left = max(1, int(times)) * 2
            self._hop_dir = -1
            self._hop_timer.start(90)

        def _hop_step(self) -> None:
            self.move(self.x(), self.y() + self._hop_dir * max(1, HOP_HEIGHT // 2))
            self._hop_dir = -self._hop_dir
            self._hop_left -= 1
            if self._hop_left <= 0:
                self._hop_timer.stop()

        def notify(self, text: str, *, hop: bool = True) -> None:
            """来消息了：冒气泡 + 蹦两下（**声音由调用方负责** —— 这里只显示）。"""
            self.show_bubble(text)
            if hop:
                self.hop()

        # ── 右键菜单 ─────────────────────────────────────────────────

        def menu_actions(self) -> list[Any]:
            """右键菜单项（单独一层是为了让测试直接拿到它们，不用弹菜单）。"""
            from laoa_trader.notify import voice as voice_mod

            actions: list[Any] = []

            messages = QAction(f"消息（{self._unread}）", self)
            messages.setToolTip("打开消息列表（双击桌宠也可以）")
            messages.triggered.connect(lambda _checked=False: self.activated.emit())
            actions.append(messages)

            test = QAction("试喊一条", self)
            test.setToolTip("立刻走一遍真实链路：响声 + 气泡 + 朗读 + 进消息列表（随时可测）")
            test.triggered.connect(lambda _checked=False: self.test_requested.emit())
            actions.append(test)

            # 静音是"临时闭嘴"，随时能取消 —— 菜单上写的必须是**点下去会发生什么**
            if voice_mod.muted():
                left = int(voice_mod.mute_remaining() // 60) + 1
                quiet = QAction(f"取消静音（还有 {left} 分钟）", self)
                quiet.setToolTip("恢复朗读（气泡与消息列表本来就没停过）")
                quiet.triggered.connect(lambda _checked=False: self.mute_requested.emit(0))
            else:
                quiet = QAction("静音一小时", self)
                quiet.setToolTip("一小时内不朗读；气泡与消息列表照常")
                quiet.triggered.connect(lambda _checked=False: self.mute_requested.emit(3600))
            actions.append(quiet)

            hide = QAction("藏起来", self)
            hide.setToolTip("把桌宠藏起来（在「系统设置 → 通知方式」里可以再叫出来）")
            hide.triggered.connect(lambda _checked=False: self.hide_requested.emit())
            actions.append(hide)
            return actions

        def _show_menu(self, global_pos: Any) -> None:
            """真正弹菜单（单独一层：测试要能拦住它，`exec()` 会把离屏测试挂死）。"""
            menu = QMenu(self)
            for action in self.menu_actions():
                menu.addAction(action)
            menu.exec(global_pos)

        # ── 拖 / 点 / 右键 ───────────────────────────────────────────
        #
        # **命中判定按整张图的矩形**，不做"只点中角色轮廓"的逐像素测试。
        # 取舍（2026-09-20 写清）：透明底素材的四个角其实是空的，严格说点那里不算
        # "点到桌宠"；但逐像素命中要用 `QRegion(pixmap.mask())` 或自己查 alpha，
        # 代价是**拖动时抓着角色的边角反而拖不动**（用户会觉得"明明点在角色身上"），
        # 而收益只是"点空白角落不会误开消息列表"这种极小概率的事。
        # 桌宠是提醒用的，**好拖、好点**比"像素级精确"重要。

        def mousePressEvent(self, event: Any) -> None:      # noqa: N802 - Qt 命名
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, event: Any) -> None:       # noqa: N802
            if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_from)

        def mouseReleaseEvent(self, event: Any) -> None:    # noqa: N802
            """松手才写配置：拖动过程中每移动一像素都写盘是浪费。"""
            if self._drag_from is not None and event.button() == Qt.MouseButton.LeftButton:
                self._drag_from = None
                self.moved.emit(int(self.x()), int(self.y()))

        def mouseDoubleClickEvent(self, event: Any) -> None:   # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton:
                self.activated.emit()

        def contextMenuEvent(self, event: Any) -> None:     # noqa: N802
            self._show_menu(event.globalPos())

        # ── 画 ───────────────────────────────────────────────────────

        def paintEvent(self, _event: Any) -> None:          # noqa: N802
            """画小家伙。三种情况，按素材决定（见模块 docstring 的「形象」一节）：

            1. **素材有透明通道** → 自由站立的角色：直接把图贴上去，
               **不画卡片底/圆角/边框/阴影**（角色的轮廓就是它的形状）；
            2. **素材没有透明通道** → 圆角卡片式：那种图（第一版是"红金花纹 + 水印"）
               四周是不透明花纹，硬抠会抠出脏边，所以给它一块圆角底；
            3. **没有素材** → QPainter 手画一个小圆脸（绝不让桌宠开不起来）。

            坐标一律用 `QRectF`：气泡在位时整只往下让出 `BUBBLE_HEIGHT`，
            所以"有没有气泡"只影响 y，不影响任何判定（拖动/点击仍按整张图的矩形）。
            """
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            top = BUBBLE_HEIGHT if self.bubble.isVisible() else 0
            rect = QRectF(0, top, self.size_, self.size_)

            if self._pixmap is not None and self._transparent:
                # 透明底素材：什么都不垫，直接贴。卡片底与描边会把角色的透明区域
                # 涂成一块方块 —— 那正是用户 2026-09-20 换透明底素材要摆脱的效果。
                painter.drawPixmap(rect.toRect(), self._pixmap)
                painter.end()
                return

            path = QPainterPath()
            path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
            painter.setClipPath(path)
            if self._pixmap is not None:
                painter.drawPixmap(rect.toRect(), self._pixmap)
            else:
                self._paint_fallback(painter, rect)
            painter.setClipping(False)
            pen = painter.pen()
            pen.setColor(QColor("#c8a24a"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawPath(path)
            painter.end()

        @staticmethod
        def _paint_fallback(painter: Any, rect: Any) -> None:
            """素材缺失时手画的小家伙（红金配色与真图一致，不至于"桌面上多一块白板"）。"""
            painter.fillRect(rect, QColor("#b8202a"))
            painter.setPen(QColor("#ffd76a"))
            face = rect.adjusted(rect.width() * 0.2, rect.height() * 0.22,
                                 -rect.width() * 0.2, -rect.height() * 0.28)
            painter.drawEllipse(face)
            eye_y = face.top() + face.height() * 0.45
            for dx in (0.32, 0.68):
                painter.drawEllipse(
                    face.left() + face.width() * dx, eye_y,
                    face.width() * 0.1, face.height() * 0.12,
                )
            painter.drawArc(
                int(face.left() + face.width() * 0.3), int(eye_y),
                int(face.width() * 0.4), int(face.height() * 0.3), 200 * 16, 140 * 16
            )


__all__ = [
    "BUBBLE_HEIGHT",
    "BUBBLE_MAX_CHARS",
    "BUBBLE_SECONDS",
    "CARD_RADIUS",
    "DesktopPet",
    "HOP_HEIGHT",
    "HOP_TIMES",
    "PET_SIZE",
    "QT_AVAILABLE",
    "clip_text",
]
