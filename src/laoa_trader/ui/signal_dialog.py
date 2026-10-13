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

from laoa_trader import analysis
from laoa_trader import pool as pool_mod
from laoa_trader.analysis import DISCLAIMER          # noqa: F401 - 界面与报告共用同一句（测试从这里取）
from laoa_trader.log import get_logger
from laoa_trader.ui import error_popup as error_popup_mod

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


if QT_AVAILABLE:

    class SignalDialog(QDialog):
        """结论窗。属性上刻意留着测试要用的引用（`table` / `detail` / `status_label` /
        `disclaimer` / `btn_copy` / `signals`），不要去爬控件层级。"""

        def __init__(self, parent: Any = None, *, title: str = "决策信号",
                     fallback_dir: Any = None) -> None:
            super().__init__(parent)
            self.signals: list[Any] = []
            #: 导出报告的标题（主窗口按"哪一页点的"设成 `…（自选标的）` / `…（持仓监控）`）——
            #: 文件离开这个窗口之后，只有这一行能说明它是从哪儿导出来的
            self.report_title: str = analysis.REPORT_TITLE
            #: 找不到桌面时的落点（主窗口给数据目录；不传就让 `pool.desktop_dir` 自己决定）
            self.fallback_dir = fallback_dir
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
            self.btn_export = QPushButton("导出到桌面")
            self.btn_export.setToolTip("把这一批结论导成一个文本文件")
            self.btn_export.clicked.connect(self.on_export)
            foot.addWidget(self.btn_export)
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

        # ── 导出 ──
        def report_text(self, **kwargs: Any) -> str:
            """这一批结论的**报告正文**（与落盘分开：正文是纯函数，测试直接断言它）。"""
            return analysis.report_text(self.signals, title=self.report_title, **kwargs)

        def on_export(self) -> None:
            """【导出到桌面】：把这批结论写成一个文本文件。

            两条口径（都照「策略筛选 → 导出筛选结果」那一套，别搞第二种）：

            * **导的是屏幕上这一份**（不重新分析）—— 重算会得到另一份结论，
              而他导出的必须是他刚看到的；
            * **落在桌面 `luweik` 文件夹里**（找不到桌面就退到数据目录），同一天覆盖同一个文件；
              成败都写在状态行里 —— "点了没反应"就等于"不知道导没导出去"。
            """
            if not self.signals:
                self.set_status("还没有结论可导出：先点【分析】")
                return
            text = self.report_text()
            if not text.strip():
                self.set_status("没有可导出的内容")
                return
            try:
                path = pool_mod.export_signal_file(text, fallback_dir=self.fallback_dir)
            except Exception as exc:  # noqa: BLE001 - 导出失败绝不能把这一窗带崩
                logger.warning(f"导出决策信号报告失败：{exc}")
                path = None
            if path is None:
                self.set_status("❌ 导出失败：文件没能写出去，详见日志")
                error_popup_mod.show_error(
                    self, "导出失败",
                    "决策信号报告没能写出去：桌面目录不存在、没有写权限，或文件正被占用。\n"
                    "下一步：确认桌面可写；仍不行请看【显示详情】里的日志。",
                )
                return
            self.set_status(f"已导出 {len(self.signals)} 只：{path}")

else:  # pragma: no cover - 没有 Qt 的环境（CLI）

    class SignalDialog:  # type: ignore[no-redef]
        """没有 Qt 时的空实现。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"没有可用的 Qt：{_QT_ERROR}")
