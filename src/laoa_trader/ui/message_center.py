"""「消息」窗口：仿 QQ 桌面端的消息列表（用户 2026-09-18 要求）。

用户原话
--------
> 「我的想法是通知仿照 QQ 桌面端，有消息软件图标闪烁，可以点开查看消息列表。」

所以通知这一路的默认形态是**两件事**：① 托盘 / 任务栏图标闪；② 点托盘图标打开这张列表。
原来那扇"右下角滑出的浮窗"（`ui/alert_popup.py`）留着但**默认关闭**：
它按的是"消息自己蹦出来给你看"，而 QQ 的按的是"图标闪，你自己点开看"。

这个窗口做什么
--------------
- **一趟列表**：时间 | 标的 | 类型 | 说明（新的在最上面）；
- **未读看得见**：未读的行**加粗 + 前面带一个圆点**，标题上写着"未读 N 条"；
- **点一条看详情**：交给主窗口弹现有那个「提醒详情」（含 L2 条件单参数）——
  详情文本只有一份来源（`intraday.format_message`），这里不另写一套；
- **打开即已读**：窗口一显示就把未读清零并通知主窗口停闪（QQ 就是这个逻辑）；
- **一个列表收纳所有消息**：盘中提醒与"筛选完成"都在里面（都来自 `intraday_alert` 表）。

为什么数据由主窗口喂进来（而不是这个窗口自己去查库）
----------------------------------------------------
主窗口每 5 秒和库对一次账（`FormulaPage` 那一套也是同样的做法）：**跨线程只能靠库与信号**，
而这个窗口只负责画。数据来源唯一 = 列表里的东西必然与「提醒」列、与推送正文一致。

为什么"清空"只清视图、不删库
----------------------------
那些行还有别的用处：两张表的「提醒」列、盘中异动详情都读同一张表。
用户点【清空】要的是"把这一屏清干净"，不是"把历史删了"——
所以清空 = 记住一个时间点，比它旧的不再显示。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 列表列头（用户能一眼看懂的四列；"说明"列吃掉剩下的宽度）
COLUMNS: tuple[str, ...] = ("时间", "标的", "类型", "说明")

#: 未读行前面的记号。**用两个字符宽的写法**（圆点 + 全角空格）：
#: 这样已读/未读两行的文字起点仍然对齐，扫一眼就能看出哪几条没看过。
UNREAD_MARK = "● "
READ_MARK = "　　"

#: 窗口初始大小；用户可以随手拉（列表长了不用滚太久）
DEFAULT_SIZE = (760, 420)

#: 列表里最多保留多少条（再多的历史看两张表的「提醒」列/日志；这里只留最近这些）
MAX_ROWS = 200


def _key(item: dict) -> tuple:
    """消息的身份：日期 + 标的 + 类型（与库里 `intraday_alert` 的主键同一个口径）。

    去重、判读都靠它 —— 主窗口的"对账"也是这个键，两处必须一致，否则会出现
    "列表里两条、库里一条"这种对不上的情况。
    """
    return (
        str(item.get("date") or ""),
        str(item.get("symbol") or item.get("target") or ""),
        str(item.get("kind") or ""),
    )


def _row_text(item: dict) -> str:
    """「说明」列那一句话：优先正文，没有就用详情里的第一段。"""
    detail = str(item.get("detail") or "").strip()
    if detail:
        return detail
    return str(item.get("target") or item.get("symbol") or "—")


class MessageCenter(QWidget):
    """消息列表窗口（普通窗口：与 QQ 一样有自己的任务栏位置、可以最小化/拉伸）。

    Signals:
        opened(): 窗口显示出来了（主窗口据此清未读、停图标闪烁）。
        closed(): 窗口被关掉。
        item_clicked(dict): 点了某一条（主窗口弹「提醒详情」）。
        settings_requested(): 点了【通知设置】（主窗口切到「系统设置 → 通知方式」）。
    """

    opened = Signal()
    closed = Signal()
    item_clicked = Signal(dict)
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # **顶层窗口**（带 `parent` 只是为了跟着主窗口一起销毁、不泄漏）：
        # 不设这个标志的话，Qt 会把没有布局的子控件画在主窗口client区的左上角 ——
        # 那是"嵌在窗口里的一块"，不是 QQ 那种独立的消息窗口。
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("消息")
        self.resize(*DEFAULT_SIZE)
        self.setObjectName("messageCenter")

        #: 用户**此刻**是不是正看着这个窗口（未读的判据）。
        #: 为什么不问 `isVisible()`：新建的子控件在"父窗口可见"时就可能已经是 True，
        #: 于是"刚来的消息"会被当成"用户正在看"而标成已读。自己记这个状态才是确定的。
        self._viewing = False

        #: 列表里的消息（新的在最前面）
        self._items: list[dict] = []
        #: 未读消息的键（`_key`）；打开窗口就清空
        self._unread: set[tuple] = set()
        #: 【清空】之后这条时间戳（`pushed_at`）之前的不再显示（只是视图过滤）
        self._cleared_before: str = ""

        self._build()

    # ── 界面 ──

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        self.title_label = QLabel("消息", self)
        self.title_label.setObjectName("messageCenterTitle")
        outer.addWidget(self.title_label)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setToolTip(
            "点任意一条看详情（含条件单参数）；未读的带圆点、加粗，打开这个窗口就算看过了"
        )
        # 用 `cellClicked` 而不是 `itemSelectionChanged`：后者在重建表格时也会触发，
        # 会把"刷新一下"误当成"用户点了这条"（详情窗自己蹦出来）。
        self.table.cellClicked.connect(self._on_cell_clicked)
        outer.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_read_all = QPushButton("全部已读", self)
        self.btn_read_all.setToolTip("把未读标记清掉（消息本身还在，历史不会丢）")
        self.btn_read_all.clicked.connect(self.mark_all_read)
        buttons.addWidget(self.btn_read_all)

        self.btn_clear = QPushButton("清空", self)
        self.btn_clear.setToolTip(
            "把这一屏清干净（只是不显示了：历史还在库里，"
            "两张表的「提醒」列与推送记录都用它）"
        )
        self.btn_clear.clicked.connect(self.clear_view)
        buttons.addWidget(self.btn_clear)

        self.btn_settings = QPushButton("通知设置", self)
        self.btn_settings.setToolTip("去「系统设置 → 通知方式」改：提示音、闪烁秒数、飞书等")
        self.btn_settings.clicked.connect(lambda _=False: self.settings_requested.emit())
        buttons.addWidget(self.btn_settings)

        buttons.addStretch(1)
        self.btn_close = QPushButton("关闭", self)
        self.btn_close.setToolTip("关掉消息窗口（有新消息时会再闪图标）")
        self.btn_close.clicked.connect(self.close)
        buttons.addWidget(self.btn_close)
        outer.addLayout(buttons)

    # ── 数据 ──

    def messages(self) -> list[dict]:
        """当前列出的消息（副本）。"""
        return [dict(item) for item in self._items]

    def unread_count(self) -> int:
        """未读条数（托盘菜单、标题、提示都显示它）。"""
        return len(self._unread)

    def is_unread(self, item: dict) -> bool:
        """这一条是不是未读（测试直接问，不用去扒表格字体）。"""
        return _key(item) in self._unread

    def set_messages(self, items: list[dict], *, unread: bool = False) -> None:
        """**整批替换**列表内容（主窗口首次对账时用它把库里的历史灌进来）。

        Args:
            unread: 这些历史要不要算未读。默认不算 —— 启动时把昨天、今天早些时候
                的提醒全标成未读，只会让用户一开机就看到一堆红点。
        """
        self._items = [dict(item) for item in (items or [])][:MAX_ROWS]
        if unread:
            self._unread = {_key(item) for item in self._items}
        else:
            self._unread = set()
        self._refresh()

    def add_messages(self, items: list[dict]) -> int:
        """把新消息并进来（新的在最上面），返回真的加进去几条。

        窗口**开着**的时候新消息直接算已读（用户正看着这一屏，标成未读没有意义）；
        关着的时候才算未读 —— 这正是 QQ 的行为。
        """
        added = 0
        for item in items or []:
            row = dict(item)
            if any(_key(row) == _key(old) for old in self._items):
                continue
            self._items.insert(0, row)
            added += 1
            if not self._viewing:
                self._unread.add(_key(row))
        if added:
            del self._items[MAX_ROWS:]
            self._refresh()
        return added

    def mark_all_read(self) -> None:
        """全部标记为已读（【全部已读】与"窗口被打开"都走这里）。"""
        if not self._unread:
            return
        self._unread.clear()
        self._refresh()

    def clear_view(self) -> None:
        """清空视图：只留【清空】之后到的消息（库里一条都不删）。

        判据用 `pushed_at`（字符串比较就是时间比较：库里的写法是 `YYYY-MM-DD HH:MM:SS`）。
        """
        newest = max((str(item.get("time") or "") for item in self._items), default="")
        self._cleared_before = newest
        self._items = []
        self._unread.clear()
        self._refresh()

    def _visible_items(self) -> list[dict]:
        """要显示的那些（清空之后到的才算）。"""
        if not self._cleared_before:
            return list(self._items)
        return [
            item for item in self._items
            if str(item.get("time") or "") > self._cleared_before
        ]

    def _refresh(self) -> None:
        """按 `self._items` 重画表格（消息数量不大，整表重建最不容易出错）。"""
        rows = self._visible_items()
        self.table.setRowCount(len(rows))
        bold = QFont(self.font())
        bold.setBold(True)
        unread_count = 0
        for index, item in enumerate(rows):
            pending = self.is_unread(item)
            unread_count += 1 if pending else 0
            cells = (
                (UNREAD_MARK if pending else READ_MARK) + str(item.get("time") or "—"),
                str(item.get("target") or item.get("symbol") or "—"),
                str(item.get("kind_label") or item.get("kind") or ""),
                _row_text(item),
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if pending:
                    cell.setFont(bold)
                if column == 3:
                    # 说明可能很长：完整内容放 tooltip（列宽不够时 Qt 自己会截断）
                    cell.setToolTip(str(item.get("detail") or text))
                self.table.setItem(index, column, cell)
        self.table.resizeRowsToContents()
        total = len(self._items)
        if unread_count:
            self.title_label.setText(f"消息 · 未读 {unread_count} 条（共 {total} 条）")
        else:
            self.title_label.setText(f"消息（共 {total} 条）")

    # ── 事件 ──

    def _on_cell_clicked(self, row: int, _column: int) -> None:
        """点某一行：标已读 + 把那条交给主窗口弹详情。"""
        rows = self._visible_items()
        if not (0 <= row < len(rows)):
            return
        item = rows[row]
        key = _key(item)
        if key in self._unread:
            self._unread.discard(key)
            self._refresh()
        self.item_clicked.emit(dict(item))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """窗口显示 → **打开即已读** + 通知主窗口停闪（QQ 的逻辑）。"""
        super().showEvent(event)
        self._viewing = True
        self.mark_all_read()
        try:
            self.opened.emit()
        except Exception as exc:  # noqa: BLE001 - 信号槽出错不该影响窗口显示
            logger.debug(f"消息窗口 opened 信号失败：{exc}")

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """窗口收起来（关闭/最小化到后面）→ 之后来的消息重新算未读。"""
        self._viewing = False
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """窗口关闭 → 通知主窗口（之后来的新消息会重新开始闪图标）。"""
        self._viewing = False
        try:
            self.closed.emit()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"消息窗口 closed 信号失败：{exc}")
        super().closeEvent(event)
