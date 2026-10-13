"""「决策信号」结论窗：**一屏把结论、依据、免责说完**。

用户 2026-10-13 要的形态是「自选/持仓加一列/一个入口，点一下出结论窗」。这一窗只做三件事：

1. **一张表**：代码 / 名称 / 结论 / 一句话 —— 一次分析几十只时先看这张表；
2. **下面那一块**：选中那一行的**全部依据**（`Signal.tip()`）——
   哪条策略命中的、哪个字段多少、哪天算的，用户自己能核一遍；
3. **底部一行**：免责 + 【复制】。

为什么不是"每只票弹一个框"：一次分析可能就是二三十只，弹窗会把用户淹掉；
而"一次一只"的入口（右键【分析】）也用**同一个窗**、只塞一行 —— 一处代码两种用法，
结论的排版与免责声明就永远不会两种说法。

**这一窗不联网、不算数**：数据是 `SignalService` 在后台线程里备好的（见 `ui/signals.py`），
窗口只管画。所以它可以在测试里直接建、直接填，不用等线程。
"""

from __future__ import annotations

from typing import Any, Sequence

from laoa_trader.log import get_logger

logger = get_logger(__name__)

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (
        QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
        QTableWidget, QTableWidgetItem, QPlainTextEdit, QVBoxLayout,
    )

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)

#: 表头（`SIGNAL_HEADERS` 这个名字与两张主表同一个约定，测试直接按它取列号）。
SIGNAL_HEADERS: tuple[str, ...] = ("代码", "名称", "结论", "一句话")

#: 底部免责声明：**只有一句短的**，也不许写成"建议买卖"。
DISCLAIMER = "本工具只做数据与形态的描述性统计，不构成投资建议。"


if QT_AVAILABLE:

    class SignalDialog(QDialog):
        """结论窗。属性上刻意留着测试要用的引用（`table` / `detail` / `status_label` /
        `disclaimer` / `btn_copy` / `signals`），不要去爬控件层级。"""

        def __init__(self, parent: Any = None, *, title: str = "决策信号") -> None:
            super().__init__(parent)
            self.signals: list[Any] = []
            self._build_ui(title)

        # ── 界面 ──
        def _build_ui(self, title: str) -> None:
            self.setWindowTitle(title)
            self.resize(760, 520)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 12)
            layout.setSpacing(8)

            self.status_label = QLabel("")
            self.status_label.setObjectName("statusTag")
            layout.addWidget(self.status_label)

            self.table = QTableWidget(0, len(SIGNAL_HEADERS))
            self.table.setHorizontalHeaderLabels(list(SIGNAL_HEADERS))
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            self.table.itemSelectionChanged.connect(self._on_selection)
            layout.addWidget(self.table, 2)

            self.detail = QPlainTextEdit()
            self.detail.setReadOnly(True)
            self.detail.setPlaceholderText("选中上面一行，这里显示这条结论的依据")
            layout.addWidget(self.detail, 3)

            foot = QHBoxLayout()
            self.disclaimer = QLabel(DISCLAIMER)
            self.disclaimer.setObjectName("statusTag")
            self.disclaimer.setWordWrap(True)
            foot.addWidget(self.disclaimer, 1)
            self.btn_copy = QPushButton("复制")
            self.btn_copy.setToolTip("复制当前这条结论的全部文字")
            self.btn_copy.clicked.connect(self.on_copy)
            foot.addWidget(self.btn_copy)
            self.btn_close = QPushButton("关闭")
            self.btn_close.clicked.connect(self.close)
            foot.addWidget(self.btn_close)
            layout.addLayout(foot)

        # ── 填内容 ──
        def set_status(self, text: str) -> None:
            self.status_label.setText(str(text or ""))

        def set_signals(self, signals: Sequence[Any]) -> None:
            """填一批结论（一批分析的结果）。**保留选中行**：同一只票重算后就地更新。"""
            keep = self.current_symbol()
            self.signals = list(signals or [])
            self.table.setRowCount(len(self.signals))
            for row, signal in enumerate(self.signals):
                values = (
                    str(getattr(signal, "symbol", "") or ""),
                    str(getattr(signal, "name", "") or ""),
                    str(getattr(signal, "verdict", "") or ""),
                    str(getattr(signal, "headline", "") or ""),
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole,
                                 str(getattr(signal, "symbol", "") or ""))
                    if column == 3:
                        item.setToolTip(text)
                    self.table.setItem(row, column, item)
            self._select(keep)
            if not self.signals:
                self.detail.setPlainText("")

        def _select(self, symbol: str) -> None:
            row = 0
            if symbol:
                for index, signal in enumerate(self.signals):
                    if str(getattr(signal, "symbol", "")) == symbol:
                        row = index
                        break
            if self.signals:
                self.table.selectRow(row)
                self._show(row)

        def current_symbol(self) -> str:
            row = self.table.currentRow()
            if row < 0 or row >= len(self.signals):
                return ""
            return str(getattr(self.signals[row], "symbol", "") or "")

        def current_tip(self) -> str:
            row = self.table.currentRow()
            if row < 0 or row >= len(self.signals):
                return ""
            return str(self.signals[row].tip())

        # ── 轮子 ──
        def _on_selection(self) -> None:
            self._show(self.table.currentRow())

        def _show(self, row: int) -> None:
            if row < 0 or row >= len(self.signals):
                self.detail.setPlainText("")
                return
            self.detail.setPlainText(str(self.signals[row].tip()))

        def on_copy(self) -> None:
            """把当前那条结论的全文放进剪贴板（没有可复制的就什么都不做）。"""
            text = self.current_tip()
            if not text:
                self.set_status("还没有结论可复制")
                return
            try:
                clipboard = QGuiApplication.clipboard()
            except Exception as exc:  # noqa: BLE001 - 剪贴板不可用不该崩
                logger.debug(f"取剪贴板失败：{exc}")
                return
            clipboard.setText(text)
            self.set_status("已复制到剪贴板")

else:  # pragma: no cover - 没有 Qt 的环境（CLI）

    class SignalDialog:  # type: ignore[no-redef]
        """没有 Qt 时的空实现。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"没有可用的 Qt：{_QT_ERROR}")
