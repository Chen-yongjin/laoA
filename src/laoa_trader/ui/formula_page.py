"""「公式选股」页：**点按钮就能写公式**的编辑器（小白友好是硬要求）。

用户原话
--------
> 「所有变量和运算符都在界面右边，直接点击就能输入，输入公式名称就能保存，
>   这样小白都能轻松操作。」

所以这一页的每一处交互都是围绕"**不会写公式的人**"设计的：

1. 右边是"点一下就输入"的面板：变量 / 函数 / 运算符各一组，每个按钮都是一个大字
   token（`C`、`MA`、`AND`），tooltip 里写着"是什么 + 一个例子"；
2. 点按钮 = **在当前光标处插入**（见 `insert_token` 那段注释：为什么不能追加到末尾）；
   函数插入的是带括号的骨架、光标落在括号里，接着打字就是参数；
3. 左边是常规编辑区：名称 + 公式体 + 【校验】【试算】【看成绩单】【保存】；
4. 所有报错都是**中文原文**（直接取自 `FormulaError.to_dict()`），带行号列号，
   还会给"是不是想写 CLOSE"这类建议 —— 这一页不需要再翻译一遍错误。

为什么单独一个模块而不是塞进 `ui/app.py`
--------------------------------------
`app.py` 已经 3900 行。这一页有 40 多个控件与自己的后台线程，塞进去会让
"主窗口接线"与"公式编辑"两件事互相干扰；分开之后，这一页可以用 Qt 的 offscreen
平台插件**单独**建出来测（`tests/test_formula_lib.py` / `test_formula_page.py`），
不用把整个主窗口拉起来。主窗口那边只留一行 `addTab`。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from laoa_trader import formulas as formulas_lib
from laoa_trader.config import get_config
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group

logger = get_logger(__name__)

try:  # Qt 缺失时不应该 import 就炸（与 ui/app.py 同一个约定）
    from PySide6.QtCore import QEvent, Qt, QThread, Signal
    from PySide6.QtGui import QFont, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
        QFrame,
    )

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 与 ui/app.py 同一个降级策略
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


#: 右侧点选面板的固定宽度。为什么要"固定"：这一列的宽度会不停变化的话，
#: 按钮文字会被压成 `M A (`，而按钮本身是这一页的核心交互 —— 宁可让左边的编辑区窄一点。
PANEL_WIDTH = 300

#: 编辑区的最小高度（再小就看不见"最后一行是选股条件"这件事了）
EDITOR_MIN_HEIGHT = 130

#: `Tab` 插入的空格数。**不跳焦点**：写公式时按 Tab 是想缩进，
#: 跳到下一个控件会让用户正在打的字跑到别处（也被原始需求点到了）。
TAB_SPACES = "    "

#: 【试算】结果里最多列几只（与 `formulas.PREVIEW_LIMIT` 一致）
PREVIEW_LIMIT = formulas_lib.PREVIEW_LIMIT

#: 提示区最多显示多少行（成绩单很长，全塞进 QLabel 会把下面的列表挤没；
#: 完整文本留在 `self.scorecard_text`，用【复制成绩单】拿走）
HINT_MAX_LINES = 14

#: 收尾时等后台线程真正退出的上限（毫秒）。见 `FormulaPage._finish_preview`：
#: 信号是排队投递的，在 `run()` 返回**之前**就可能已经到主线程了，
#: 这时候放掉最后一个引用会让 QThread 在"线程还在跑"时析构 —— Qt 直接崩进程。
THREAD_JOIN_MS = 3_000

#: 顶部那两行灰字说明（小白第一眼看的就是它）
PAGE_HINT = (
    "点右边的按钮就能插入；最后一行是选股条件。"
    "写完点【校验】→【试算】→【保存】。"
)

#: 【载入示例】在没有示例文件时用的兜底公式（保证"小白第一步"一定走得通）
SAMPLE_NAME = "放量上攻"
SAMPLE_TEXT = "M5:=MA(C,5)\nV5:=MA(V,5)\nC>M5 AND C>O AND V>V5*1.5"

# ── 右侧面板的三个分组 ──
#
# 每一项：(插入的文本, tooltip, 参数个数)
#   * tooltip 一律写"是什么 + 一个例子"—— 小白不需要知道"简单移动平均"的定义，
#     他需要的是"我该写成什么样"；
#   * 第 3 项 = `None` → **原样插入**（变量、短运算符）；
#     `0`   → 插入 `NAME()` 并把光标放在括号**后面**（`连板()`、`涨停天数()` 这类
#             不带参数的函数，用户接着打 `>=2` 就行）；
#     `>0`  → 插入 `NAME()` 并把光标放在括号**里面**（接着打字就是第一个参数）。
# 为什么用 `None` 而不是"0 就不插括号"：0 和"无参函数"是两件事 ——
# 混成一个值的话 `连板` 会插成光秃秃的 `连板`（引擎会报"函数后面要跟括号"）。
VARIABLES: tuple[tuple[str, str, int | None], ...] = (
    ("C", "C 收盘价（当天最后一笔成交价）。例：C>MA(C,5) 表示收盘站上 5 日线", None),
    ("O", "O 开盘价。例：C>O 表示今天是阳线（收盘高于开盘）", None),
    ("H", "H 最高价。例：H>REF(H,1) 表示今天创了新高", None),
    ("L", "L 最低价。例：L<REF(L,1) 表示今天创了新低", None),
    ("V", "V 成交量（股）。例：V>MA(V,5)*1.5 表示比 5 日均量放大 1.5 倍", None),
    ("AMO", "AMO 成交额（元）。例：AMO>100000000 表示当天成交额过亿", None),
    ("PRE", "PRE 昨收价。例：C/PRE>1.05 表示今天涨了 5% 以上", None),
    ("INDUSTRY", "INDUSTRY 所属行业名（要加引号）。例：INDUSTRY=\"半导体\"", None),
)

FUNCTIONS: tuple[tuple[str, str, int | None], ...] = (
    ("MA", "MA(X,N) 求 X 的 N 日平均值。例：MA(C,5) 是 5 日均价", 2),
    ("EMA", "EMA(X,N) 指数平均（越近的越重要）。例：EMA(C,12)", 2),
    ("REF", "引用 N 天前的值。例：REF(C,1) 是昨天收盘价", 2),
    ("HHV", "HHV(X,N) 近 N 日的最高值。例：HHV(H,20) 是 20 日新高价", 2),
    ("LLV", "LLV(X,N) 近 N 日的最低值。例：LLV(L,20) 是 20 日最低价", 2),
    ("SUM", "SUM(X,N) 近 N 日的合计。例：SUM(V,5) 是近 5 日成交量合计", 2),
    ("COUNT", "COUNT(条件,N) 近 N 日里条件成立几次。例：COUNT(C>O,10)", 2),
    ("CROSS", "CROSS(A,B) 金叉：昨天 A 不大于 B、今天 A 大于 B。例：CROSS(C,MA(C,5))", 2),
    ("ABS", "ABS(X) 绝对值。例：ABS(C-PRE) 是今天的涨跌金额", 1),
    ("MAX", "MAX(A,B) 取大的那个。例：MAX(C,O)", 2),
    ("MIN", "MIN(A,B) 取小的那个。例：MIN(C,O)", 2),
    ("IF", "IF(条件,A,B) 条件成立取 A、否则取 B。例：IF(C>O,C,O)", 3),
    ("STD", "STD(X,N) 近 N 日的标准差（波动有多大）。例：STD(C,20)", 2),
    ("BARSLAST", "BARSLAST(条件) 距离上次条件成立过了几天。例：BARSLAST(连板()>0)", 1),
    ("涨停天数", "涨停天数() 今天是否涨停；涨停天数(10) 近 10 日涨停几次。"
                 "⚠️ 依赖本地涨停池，早期日期会读到 0", 0),
    ("连板", "连板() 今天几连板（0 = 没涨停）。例：连板()>=2。"
             "⚠️ 依赖本地涨停池，早期日期会读到 0", 0),
    ("量比", "量比() 当天成交额 ÷ 前 5 日均额；也可以写 量比(10)。例：量比()>2", 0),
)

OPERATORS: tuple[tuple[str, str, int | None], ...] = (
    ("+", "加。例：MA(C,5)+MA(C,10)", None),
    ("-", "减。例：C-PRE 是今天的涨跌金额", None),
    ("*", "乘。例：V*1.5 表示按 1.5 倍算", None),
    ("/", "除。例：AMO/100000000 是成交额（亿元）", None),
    (">", "大于。例：C>MA(C,5)", None),
    ("<", "小于。例：C<MA(C,5)", None),
    (">=", "大于等于。例：C>=PRE*1.05", None),
    ("<=", "小于等于。例：C<=PRE*0.95", None),
    ("=", "等于（**比较**，不是赋值；赋值用 :=）。例：INDUSTRY=\"银行\"", None),
    ("!=", "不等于。例：INDUSTRY!=\"银行\"", None),
    ("AND", "并且：两边都要成立（自动补空格）。例：C>MA(C,5) AND V>MA(V,5)", None),
    ("OR", "或者：任一边成立即可（自动补空格）。例：涨停天数(10)>=1 OR 连板()>=2", None),
    ("NOT", "取反（自动补空格）。例：NOT(C>MA(C,5)) 表示收盘没站上 5 日线", None),
    ("(", "左括号：改变运算顺序。例：(C+O)/2 是当天中间价", None),
    (")", "右括号：与左括号配对", None),
)

#: 需要"前后补空格"的运算符（否则 `A>1` 之后点 AND 会粘成 `A>1AND B`）。
#: 为什么只对这三个补：`+`/`>` 这类短符号粘在一起不影响可读性，
#: 而 `AND`/`OR`/`NOT` 是**字母单词**，粘上就是"另一个词"，编辑器里一眼看不出来。
_SPACED_OPERATORS = ("AND", "OR", "NOT")


class ScorecardWorker(QThread):
    """这一页的后台线程：**【看成绩单】与【试算】都用它**。

    为什么必须后台跑：成绩单要扫全库（10 年数据下是几千只 × 上千根 K 线），
    试算要逐只票读 K 线并在最后一根上跑公式（真实 3 年库实测 3.7 秒，全市场
    5000+ 只要 7~8 秒）。在主线程里跑就是"窗口未响应"——用户以为程序死了，
    其实是它正在算。这里是 Qt 里唯一安全的做法：工作线程只算数，
    结果通过信号回主线程再碰控件。

    `with_progress`：**不是每个被后台化的函数都收 `progress_cb`**
    （`preview_hits` 就只有 `limit`/`start`/`symbols`）。与其为了让 worker 统一
    而给库函数加一个假参数，不如让调用方声明"这次要不要进度回调"
    （与 `ui/app.py` 的 `Worker` 同一个约定）。

    `failed` 递的是**异常对象**而不是一句话：两种失败在界面上的说法不同
    （`FormulaDataError` = 数据问题，该去下载数据；其它 = 程序问题），
    工作线程不该替界面决定措辞 —— 主线程拿到类型才分得清
    （见 `FormulaPage._on_preview_failed`）。
    """

    progress = Signal(str, int, int)
    finished_ok = Signal(object)
    failed = Signal(object)

    def __init__(self, fn: Callable[..., Any], *args: Any,
                 with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._with_progress = with_progress

    def run(self) -> None:  # noqa: D102 - QThread 约定
        try:
            kwargs = dict(self._kwargs)
            if self._with_progress:
                kwargs["progress_cb"] = self.progress.emit
            result = self._fn(*self._args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 后台异常必须回主线程说人话
            logger.exception("公式后台任务失败")
            self.failed.emit(exc)
        else:
            self.finished_ok.emit(result)


class FormulaPage(QWidget):
    """「公式选股」页。

    属性里刻意留着测试与外部要用的引用（`name_edit` / `editor` / `hint_label` /
    `table` / `palette_buttons` / `scorecard_worker` / `preview_worker`），
    不要去爬控件层级 —— 这一页的控件多，按层级取值的测试一改布局就集体失效。
    """

    def __init__(
        self,
        cfg: Any = None,
        parent: Any = None,
        *,
        status_cb: Callable[[str], None] | None = None,
        directory: Any = None,
    ) -> None:
        """建页。

        Args:
            cfg: 配置对象（默认全局单例）；公式目录与 `enabled_formulas` 都读它。
            status_cb: 给主窗口显示一句话（状态栏），不传就只写日志。
            directory: 公式目录（默认 `formulas_lib.formula_dir()`；测试传 tmp_path）。
        """
        super().__init__(parent)
        self.cfg = cfg if cfg is not None else get_config()
        self.status_cb = status_cb
        self.directory = directory

        #: 当前列出的公式（`FormulaSpec` 列表，顺序 = 目录里的文件名顺序）
        self.specs: list[Any] = []
        #: 右侧面板按钮：{token: QPushButton}（测试按 token 点，不爬布局）
        self.palette_buttons: dict[str, Any] = {}
        #: 【看成绩单】的后台线程与结果
        self.scorecard_worker: ScorecardWorker | None = None
        self.scorecard_result: dict | None = None
        self.scorecard_text: str = ""
        #: 【试算】的后台线程；跑完置回 None（测试就等这一条来判断"落地了"）
        self.preview_worker: ScorecardWorker | None = None
        #: 【试算】按下按钮那一刻的公式快照（结果属于它，不属于编辑框里现在的内容）
        self.preview_formula: Any = None
        self.hint_text: str = ""
        self._loading = False          # 载入行时别把"选中变化"当成用户点击

        self._build_ui()
        self.reload()

    # ── 界面搭建 ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        # 顶部：一行灰字说明 + 右侧【载入示例】（小白的第一站）
        top = QHBoxLayout()
        self.page_hint = QLabel(PAGE_HINT)
        self.page_hint.setObjectName("statusTag")      # 小号灰字（与状态区同一档）
        self.page_hint.setWordWrap(True)
        top.addWidget(self.page_hint, 1)
        self.btn_sample = QPushButton("载入示例")
        self.btn_sample.setToolTip("把一条能跑通的示例公式放进编辑框，照着改就行")
        self.btn_sample.clicked.connect(self.on_load_sample)
        top.addWidget(self.btn_sample, 0)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_editor_side())
        splitter.addWidget(self._build_palette_side())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        self.splitter = splitter
        layout.addWidget(splitter, 1)

    def _build_editor_side(self) -> Any:
        """左侧：名称 + 编辑框 + 三个按钮 + 提示区 + 已保存公式列表。"""
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── 名称行 ──
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("公式名称："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：5日线上放量")
        self.name_edit.setToolTip("这就是保存后的文件名，也是池子/推送里显示的名字")
        name_row.addWidget(self.name_edit, 1)

        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("primaryAction")     # 主操作按钮（主题里精确命中）
        self.btn_save.setToolTip("把编辑框里的公式存到公式目录（重名会先问一句）")
        self.btn_save.clicked.connect(self.on_save)
        name_row.addWidget(self.btn_save)

        self.btn_save_as = QPushButton("另存为")
        self.btn_save_as.setToolTip("换一个名字再存一份（原来的那份不动）")
        self.btn_save_as.clicked.connect(self.on_save_as)
        name_row.addWidget(self.btn_save_as)

        self.btn_delete = QPushButton("删除")
        self.btn_delete.setToolTip("删掉当前这条公式文件（会先问一句）")
        self.btn_delete.clicked.connect(self.on_delete)
        name_row.addWidget(self.btn_delete)
        layout.addLayout(name_row)

        # ── 编辑框 ──
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "在这里写公式，例如：\nM5:=MA(C,5)\nC>M5 AND V>MA(V,5)*1.5"
        )
        self.editor.setMinimumHeight(EDITOR_MIN_HEIGHT)
        self.editor.setTabChangesFocus(False)
        # 等宽字体：公式里的括号与逗号要能对齐，"哪一层括号"才看得出来
        font = QFont()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("Consolas")
        font.setFixedPitch(True)
        self.editor.setFont(font)
        # Tab 插入空格而不是切焦点（见 `TAB_SPACES` 的说明）
        self.editor.installEventFilter(self)
        layout.addWidget(self.editor, 1)

        # ── 动作行 ──
        action_row = QHBoxLayout()
        self.btn_validate = QPushButton("校验")
        self.btn_validate.setToolTip("检查公式写得对不对；通过时会告诉你用到哪些字段/函数")
        self.btn_validate.clicked.connect(self.on_validate)
        action_row.addWidget(self.btn_validate)

        self.btn_preview = QPushButton("试算：当前库能选出几只")
        self.btn_preview.setToolTip("不推送、不写库，只看看最近一个交易日命中几只")
        self.btn_preview.clicked.connect(self.on_preview)
        action_row.addWidget(self.btn_preview)

        self.btn_scorecard = QPushButton("看成绩单")
        self.btn_scorecard.setToolTip("这条公式历史上行不行（后台计算，耗时长但不卡界面）")
        self.btn_scorecard.clicked.connect(self.on_scorecard)
        action_row.addWidget(self.btn_scorecard)

        self.btn_copy_scorecard = QPushButton("复制成绩单")
        self.btn_copy_scorecard.setToolTip("把上一次成绩单的完整文本复制到剪贴板")
        self.btn_copy_scorecard.clicked.connect(self.on_copy_scorecard)
        action_row.addWidget(self.btn_copy_scorecard)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        # ── 进度条（只在跑成绩单时出现）──
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        # ── 提示区（多行、可复制）──
        #
        # 为什么用 QLabel + 可选文本而不是弹窗：校验/试算的结果是"要照着改"的东西，
        # 弹窗点掉就没了；放在页面上可以边看边改，还能选中复制去搜索。
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.hint_label.setMinimumHeight(40)
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.hint_label)

        # ── 已保存公式列表 ──
        layout.addWidget(QLabel("已保存的公式（点一行就载入到上面改）"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "说明", "状态", "参与选股"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        layout.addWidget(self.table, 1)
        return side

    def _build_palette_side(self) -> Any:
        """右侧：**点一下就输入**的三组按钮（变量 / 函数 / 运算符）。

        用 `QGroupBox` 而不是 `QToolBox`：折叠起来之后，小白会以为"函数不见了"——
        三组一起看得见、函数多了就滚动，才是"所有东西都在右边"的本意。
        """
        panel = QWidget()
        panel.setFixedWidth(PANEL_WIDTH)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(6)

        title = QLabel("点一下，就插到光标那里")
        title.setObjectName("statusTag")
        panel_layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(8)
        for name, items in (("变量", VARIABLES), ("函数", FUNCTIONS), ("运算符", OPERATORS)):
            inner_layout.addWidget(self._build_group(name, items))
        inner_layout.addStretch(1)
        scroll.setWidget(inner)
        panel_layout.addWidget(scroll, 1)
        self.palette_panel = panel
        return panel

    def _build_group(self, title: str, items: Sequence[tuple[str, str, int | None]]) -> Any:
        """一组按钮（两列网格）。每个按钮一个中文 tooltip（是什么 + 一个例子）。"""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setSpacing(4)
        for index, (token, tip, args) in enumerate(items):
            button = QPushButton(token)
            button.setToolTip(tip)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            # 运算符里的 `AND`/`OR`/`NOT` 走 `insert_operator`（要自动补空格），
            # 其余（变量、短运算符、函数）都走 `insert_token`。
            # 用默认参数把 token 绑进 lambda：循环变量在闭包里是**延迟求值**的，
            # 不绑的话所有按钮都会插入最后一个 token（经典坑）
            if args is None and token in _SPACED_OPERATORS:
                button.clicked.connect(
                    lambda _checked=False, text=token: self.insert_operator(text)
                )
            else:
                button.clicked.connect(
                    lambda _checked=False, text=token, count=args: self.insert_token(
                        text, args=count
                    )
                )
            grid.addWidget(button, index // 2, index % 2)
            self.palette_buttons[token] = button
        return box

    # ── 点选插入 ──────────────────────────────────────────────────────

    def insert_token(self, token: str, args: int | None = None) -> None:
        """把 `token` 插到**当前光标处**（函数会带上 `()` 并把光标放进括号里）。

        `args=None` = 原样插入（变量、短运算符）；`args` 是数字 = 函数骨架，
        `0` 表示无参函数（光标落在 `)` 之后），`>0` 表示把光标放进括号里。

        **为什么必须在光标处插入，而不是追加到末尾**（这一页最容易做错的一处）：
        用户修改公式是"边想边改"的 —— 他点了【载入示例】或载入了一条已保存的公式，
        然后想改**中间那一行**（例如把 `MA(C,5)` 改成 `MA(C,10)`，或往第 2 行加个条件）。
        如果按钮把 token 追加到末尾，他会看到：公式语法通过、但选出来的票完全不对 ——
        而且**看不出哪里变了**（末尾多了一个 `AND ...`）。这种"静默改坏"比报错可怕得多。
        插入到光标处 + 焦点回到编辑框 + 光标停在括号里，才是"点一下就接着打字"的手感；
        追加到末尾则要用户自己再剪切粘贴一次，那这个面板就白做了。
        """
        cursor = self.editor.textCursor()
        if args is None:
            cursor.insertText(token)
        elif args > 0:
            # 骨架 `NAME()`，再把光标左移一格 → 落在括号里，接着打字就是第一个参数
            cursor.insertText(f"{token}()")
            cursor.movePosition(QTextCursor.MoveOperation.Left)
        else:
            # 无参函数：`NAME()`，光标停在 `)` 之后（用户接着打 `>=2` 之类）
            cursor.insertText(f"{token}()")
        self.editor.setTextCursor(cursor)
        # 焦点回到编辑框：用户是从右边面板点的按钮，不去抢回来的话
        # 他接着敲键盘就是在别的控件上输入（一次都输不进去）
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def insert_operator(self, operator: str) -> None:
        """插入运算符；`AND`/`OR`/`NOT` 前后自动补空格。

        为什么要补：`A>1` 后面直接粘 `AND` 会变成 `A>1AND B` —— 这是**语法错误**，
        而错误提示说的是"未知字段 A>1AND"之类，小白根本看不懂。宁可多一个空格。
        """
        if operator not in _SPACED_OPERATORS:
            self.insert_token(operator)
            return
        text = self.editor.toPlainText()
        position = self.editor.textCursor().position()
        before, after = text[:position], text[position:]
        lead = "" if (not before or before[-1].isspace()) else " "
        trail = "" if (after and after[0].isspace()) else " "
        self.insert_token(f"{lead}{operator}{trail}")

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt 命名
        """`Tab` 键插入空格（不跳焦点）。

        为什么：写多行公式时 Tab 是想缩进对齐，而 QPlainTextEdit 默认会插入制表符
        （在不同编辑器里宽度不一样，粘贴到别处就歪）；跳焦点更糟 ——
        用户正在打的公式会跑到名称框里。
        """
        if obj is self.editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab:
                cursor = self.editor.textCursor()
                cursor.insertText(TAB_SPACES)
                self.editor.setTextCursor(cursor)
                return True
        return super().eventFilter(obj, event)

    # ── 校验 / 试算 / 成绩单 ──────────────────────────────────────────

    def compile_current(self, *, quiet: bool = False) -> Any:
        """编译编辑框里的公式；失败时把**中文原文**写进提示区并返回 None。"""
        text = self.editor.toPlainText()
        if not text.strip():
            if not quiet:
                self._set_hint("❌ 公式还是空的：点右边的按钮就能插入，或者点【载入示例】")
            return None
        try:
            return fm.compile_formula(text, name=self.name_edit.text().strip())
        except fm.FormulaError as exc:
            if not quiet:
                # 直接取引擎结构化错误里的 `text`（=「第 3 行第 12 列：未知函数 "MAA"
                # （可用函数：…）」这种中文原文），界面**一个字都不翻译** ——
                # 引擎已经把行号列号、近似建议、可用清单都写好了，再润色一遍只会
                # 引入新的不一致。`to_dict()` 里的 line/col 留着给将来"把光标跳到
                # 出错位置"用。
                self._set_hint("❌ " + str(exc.to_dict()["text"]))
            return None

    def on_validate(self) -> None:
        """【校验】：显示用到的字段/函数/最少 K 线数；错就显示中文行号列号。"""
        formula = self.compile_current()
        if formula is None:
            return
        lines = [f"✅ 校验通过：{formula.describe()}"]
        if formula.outputs:
            lines.append("中间变量：" + "、".join(formula.outputs) + "（:= 只算不输出）")
        if formula.min_history > 0:
            lines.append(
                f"提示：库里的历史不足 {formula.min_history} 根 K 线的票会自动跳过（缺值不产生信号）"
            )
        hint = formulas_lib.limit_up_hint(formula)
        if hint:
            lines.append("⚠️ " + hint)
        self._set_hint("\n".join(lines))

    def on_preview(self) -> None:
        """【试算】：当前库的最近一个交易日能选出几只（名称（代码）格式）。

        **后台线程 + 公式快照**（这一页第二处必须后台化的地方，第一处是成绩单）：
        试算要逐只票读 K 线并在最后一根上跑公式，真实 3 年库实测 3.7 秒、
        全市场 5000+ 只要 7~8 秒 —— 在主线程里跑就是"窗口未响应"
        （用户已经为这件事抱怨过一次，那次是下载路径）。

        为什么先编译一次、再把**同一个公式对象**交给线程：线程跑的是用户按下按钮
        那一刻看到的公式。他在等待期间接着改编辑框（很常见：边等边琢磨条件），
        编辑框里的新内容不会把结果污染成"另一条公式的答案"。
        """
        formula = self.compile_current()
        if formula is None:
            return
        if self.preview_worker is not None and self.preview_worker.isRunning():
            # 双击 / 上一次还没跑完又点一次：不动正在跑的那次
            # （两个线程抢同一个库没有意义，只是白扫一遍）
            self._set_hint("试算还在跑，请稍候…（跑完会写在这里）")
            return
        self.preview_formula = formula
        self.btn_preview.setEnabled(False)
        # 先设成"不确定进度"：总共有多少只票要扫，得先走一遍库才知道。
        # 让进度条先动起来，比"停在 0% 七八秒"让人安心（与成绩单同一立场）。
        self.progress.setRange(0, 0)
        self.progress.setFormat("正在试算…")
        self.progress.setVisible(True)
        self._set_hint("正在试算…（在后台跑，界面可以继续用）")
        worker = ScorecardWorker(
            formulas_lib.preview_hits, formula, self.cfg.db_path, limit=PREVIEW_LIMIT
        )
        self.preview_worker = worker
        worker.finished_ok.connect(self._on_preview_done)
        worker.failed.connect(self._on_preview_failed)
        worker.start()

    @staticmethod
    def _preview_text(formula: Any, result: dict) -> str:
        """把 `preview_hits` 的返回值拼成提示区那段中文。

        单独一个**纯函数**，是为了让"结果长什么样"与"它在哪个线程跑"解耦：
        这次改造只是把计算挪到后台，文案一个字都不该变（用户已经见过这几句话），
        所以格式化逻辑只此一份，谁调都是同一段文本。
        """
        if result["count"]:
            names = "、".join(
                f"{hit['name']}（{hit['symbol']}）" for hit in result["hits"]
            )
            text = (f"最近交易日 {result['date']} 命中 {result['count']} 只：{names}")
            if result["count"] > result["shown"]:
                text += f" …（只列前 {result['shown']} 只）"
        else:
            text = (f"最近交易日 {result['date']}：没有命中"
                    f"（扫了 {result['scanned']} 只，{result['skipped']} 只因数据不足跳过）")
        hint = formulas_lib.limit_up_hint(formula)
        if hint:
            text += "\n⚠️ " + hint
        if result["errors"]:
            text += f"\n（{len(result['errors'])} 只票算不出来，已跳过：{result['errors'][0]}）"
        return text

    def _on_preview_done(self, result: Any) -> None:
        """试算回来了（回主线程执行）：先收起"正在跑"的样子，再写结果。"""
        formula = self.preview_formula
        self._finish_preview()
        if not isinstance(result, dict):
            self._set_hint("试算没有返回结果（请重试）")
            return
        self._set_hint(self._preview_text(formula, result))

    def _on_preview_failed(self, exc: Any) -> None:
        """试算失败：**数据问题**与**程序问题**分开说（下一步动作完全不同）。

        注：这里的两句话与改造前的同步版本**逐字一致**。`failed` 递过来的是异常
        对象（不是一句话），正是为了在这里用 `isinstance` 分清这两种情况 ——
        工作线程不该替界面决定措辞。
        """
        self._finish_preview()
        if isinstance(exc, fm.FormulaDataError):
            # 库不存在/读不出来：这不是公式写错了，说清楚下一步
            self._set_hint("❌ " + str(exc))
        else:
            self._set_hint(f"❌ 试算失败：{type(exc).__name__}: {exc}")

    def _finish_preview(self) -> None:
        """【试算】收尾：把按钮还回来、收掉进度条、放掉线程引用。

        为什么"等它真的退出"再放引用：`finished_ok`/`failed` 是**跨线程排队**投递的，
        在工作线程 `run()` 返回之前就可能已经排到主线程执行了；这时丢掉最后一个引用，
        QThread 对象会在"线程还没真正结束"时就析构 —— Qt 会
        `QThread: Destroyed while thread is still running` 直接把进程干掉。
        线程此刻已经在收尾，`wait()` 只等几毫秒，比留一堆线程对象在 `self` 上干净。
        """
        worker = self.preview_worker
        self.btn_preview.setEnabled(True)
        self.progress.setVisible(False)
        if worker is not None:
            if worker.isRunning():
                worker.wait(THREAD_JOIN_MS)
            self.preview_worker = None

    def on_scorecard(self) -> None:
        """【看成绩单】：后台线程跑历史成绩单（**不卡界面**）。"""
        formula = self.compile_current()
        if formula is None:
            return
        if self.scorecard_worker is not None and self.scorecard_worker.isRunning():
            self._set_hint("成绩单还在算，请稍候…")
            return
        if not self.scorecard_text:
            self._set_hint("正在后台算成绩单…（数据多的时候要几十秒，界面可以继续用）")
        self.scorecard_result = None
        self.progress.setVisible(True)
        # 先设成"不确定进度"：总分是多少要扫一遍库才知道，
        # 让进度条先动起来，比"停在 0% 几十秒"让人安心
        self.progress.setRange(0, 0)
        self.btn_scorecard.setEnabled(False)
        worker = ScorecardWorker(
            formulas_lib.run_scorecard,
            formula,
            self.cfg.db_path,
            with_progress=True,          # `run_scorecard` 收 `progress_cb`，试算不收
            conv_key=formulas_lib.DEFAULT_CONVENTION_KEY,
        )
        self.scorecard_worker = worker
        worker.progress.connect(self._on_scorecard_progress)
        worker.finished_ok.connect(self._on_scorecard_done)
        worker.failed.connect(self._on_scorecard_failed)
        worker.start()

    def _on_scorecard_progress(self, stage: str, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(min(done, max(total, 1)))
        self.progress.setFormat(f"{stage} {done}/{total}")

    def _on_scorecard_done(self, result: Any) -> None:
        self.btn_scorecard.setEnabled(True)
        self.progress.setVisible(False)
        if not isinstance(result, dict):
            self._set_hint("成绩单没有返回结果（请重试）")
            return
        self.scorecard_result = result
        self.scorecard_text = str(result.get("text") or "")
        self._set_hint(self.scorecard_text)

    def _on_scorecard_failed(self, exc: Any) -> None:
        """成绩单失败：把线程递过来的异常写成人话（这句与改造前逐字一致）。"""
        self.btn_scorecard.setEnabled(True)
        self.progress.setVisible(False)
        self._set_hint("❌ 成绩单算不出来：" + f"{type(exc).__name__}: {exc}")

    def on_copy_scorecard(self) -> None:
        """【复制成绩单】：把完整文本放进剪贴板（提示区只显示前几行）。"""
        if not self.scorecard_text:
            self._toast("还没有成绩单可复制：先点【看成绩单】")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.scorecard_text)
        self._toast("成绩单已复制到剪贴板")

    # ── 保存 / 删除 / 载入 ────────────────────────────────────────────

    def on_save(self) -> None:
        """【保存】：名称必填；重名先问一句再覆盖。"""
        raw = self.name_edit.text()
        problem = formulas_lib.name_error(raw)
        if problem:
            # 空名**只用提示区**、不弹窗：这是用户马上能自己改的问题，
            # 弹一个要点"确定"的框反而多一步（而覆盖是**不可逆**的，才必须拦一下）
            self._set_hint("❌ " + problem)
            self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        name = formulas_lib.safe_name(raw)
        path = formulas_lib.formula_path(name, self.directory)
        if path.exists() and not self._confirm(f"公式「{name}」已存在，要覆盖它吗？\n"
                                               f"（原来的内容会被替换，不可撤销）"):
            self._set_hint(f"已取消保存：公式「{name}」保持原样（没有被改动）")
            self._toast("已取消保存（原公式没有被改动）")
            return
        self._write(name)

    def on_save_as(self) -> None:
        """【另存为】：换个名字再存一份（原文件不动）。"""
        default = formulas_lib.safe_name(self.name_edit.text()) or "新公式"
        text, ok = QInputDialog.getText(self, "另存为", "新公式名称：", text=default)
        if not ok:
            return
        problem = formulas_lib.name_error(text)
        if problem:
            self._set_hint("❌ " + problem)
            return
        name = formulas_lib.safe_name(text)
        path = formulas_lib.formula_path(name, self.directory)
        if path.exists() and not self._confirm(f"公式「{name}」已存在，要覆盖它吗？"):
            self._set_hint(f"已取消另存为：公式「{name}」保持原样")
            self._toast("已取消另存为")
            return
        self._write(name)

    def on_delete(self) -> None:
        """【删除】：删掉当前公式（先确认）。"""
        name = self.current_name()
        if not name:
            self._set_hint("❌ 请先在下面的列表里选一条公式（或填上名称）")
            return
        if not self._confirm(f"确定删除公式「{name}」吗？\n（文件会被删掉，不可撤销）"):
            return
        try:
            deleted = formulas_lib.delete_formula(name, self.directory)
        except OSError as exc:
            self._set_hint(f"❌ 删除失败：{exc}（文件可能正被其它程序占用）")
            return
        if deleted:
            self.name_edit.clear()
            self.editor.clear()
            self.reload()
            self._set_hint(f"🗑 已删除公式「{name}」")
        else:
            self._set_hint(f"❌ 没找到公式「{name}」的文件")

    def _write(self, name: str) -> None:
        """真正落盘（名称已安全化、覆盖已确认）。"""
        body = self.editor.toPlainText()
        try:
            path = formulas_lib.save_formula(name, body, directory=self.directory)
        except ValueError as exc:
            self._set_hint("❌ " + str(exc))
            return
        except OSError as exc:
            self._set_hint(f"❌ 保存失败：{exc}（公式目录可能没有写权限，"
                           "可以把程序放到有写权限的目录）")
            return
        # 把安全化后的名字回显：用户填 `涨/跌` 时看到的是 `涨_跌`，
        # 与磁盘上的文件名一致（否则他会以为"我存的名字怎么不见了"）
        self.name_edit.setText(name)
        self.reload()
        self.select_row(name)
        self._set_hint(
            f"✅ 已保存「{name}」（{len(body)} 字符）\n"
            f"文件：{path}\n"
            "想让它参与每天的选股，就在下面那一行勾上「参与选股」。"
        )
        self._toast(f"公式「{name}」已保存")

    def on_load_sample(self) -> None:
        """【载入示例】：给小白一个**能跑通**的起点。"""
        sample = None
        for spec in formulas_lib.formula_files(self.directory):
            if spec.ok and (sample is None or spec.name == SAMPLE_NAME):
                sample = spec
                if spec.name == SAMPLE_NAME:
                    break
        if sample is not None:
            self.name_edit.setText(sample.name)
            self.editor.setPlainText(sample.source)
            self._set_hint(
                f"已载入示例公式「{sample.name}」。\n"
                "点【校验】看看它用到什么，点【试算】看它在你的库里能选出几只。"
            )
        else:
            self.name_edit.setText(SAMPLE_NAME)
            self.editor.setPlainText(SAMPLE_TEXT)
            self._set_hint(
                "已载入内置示例公式（公式目录里还没有示例文件，这是兜底的那条）。\n"
                "点【校验】→【试算】，再点【保存】就存到你的公式目录里了。"
            )
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

    # ── 已保存公式列表 ────────────────────────────────────────────────

    def reload(self) -> None:
        """重建"已保存公式"列表（名称 / 说明 / 状态 / 参与选股）。"""
        self.specs = formulas_lib.formula_files(self.directory)
        runtime = formula_group.last_status()
        enabled = set(str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or []))
        self.table.setRowCount(len(self.specs))
        self._row_boxes = {}
        for row, spec in enumerate(self.specs):
            name_item = QTableWidgetItem(spec.name)
            name_item.setToolTip(spec.path)
            self.table.setItem(row, 0, name_item)
            desc_item = QTableWidgetItem(spec.description or "—")
            if spec.description:
                desc_item.setToolTip(spec.description)
            self.table.setItem(row, 1, desc_item)

            status_item = QTableWidgetItem(self._status_text(spec, runtime))
            if not spec.ok:
                status_item.setToolTip(spec.error_text)
            elif spec.name in runtime:
                status_item.setToolTip(runtime[spec.name])
            self.table.setItem(row, 2, status_item)

            box = QCheckBox()
            box.setChecked(spec.name in enabled)
            box.setToolTip(
                "勾上 = 这条公式参与每天的选股（写回 config.toml 的 enabled_formulas）；"
                "默认不勾 —— 你自己的公式要不要用，由你决定"
            )
            # **先 setChecked 再接信号**：否则建表时就会触发一次"保存设置"
            box.stateChanged.connect(
                lambda state, n=spec.name: self.on_toggle_enabled(n, bool(state))
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(box)
            self.table.setCellWidget(row, 3, holder)
            self._row_boxes[spec.name] = box
        if self.specs:
            self.select_row(self.current_name() or self.specs[0].name)

    @staticmethod
    def _status_text(spec: Any, runtime: dict[str, str]) -> str:
        """状态列文本：✅ 校验通过 / ❌ 语法错 / ⚠️ 运行时报错（带原因摘要）。"""
        if not spec.ok:
            return "❌ " + _one_line(spec.error_text, 80)
        if spec.name in runtime:
            return "⚠️ 运行时出错：" + _one_line(runtime[spec.name], 60)
        return "✅ 校验通过"

    def current_name(self) -> str:
        """当前选中的公式名（列表选中优先，其次名称框）。"""
        name = self.name_edit.text().strip()
        return formulas_lib.safe_name(name)

    def select_row(self, name: str) -> None:
        """按名称选中一行（**用户切行时才载入编辑区**）。"""
        for row, spec in enumerate(self.specs):
            if spec.name == name:
                self.table.selectRow(row)
                return

    def selected_spec(self) -> Any:
        """当前选中的 `FormulaSpec`（没有就 None）。"""
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        index = rows[0].row() if rows else -1
        if 0 <= index < len(self.specs):
            return self.specs[index]
        # 没有选中行时按名称框找（用户手打了名字、还没保存过的情况）
        name = self.current_name()
        for spec in self.specs:
            if spec.name == name:
                return spec
        return None

    def on_row_selected(self) -> None:
        """列表选中某一行 → 把那条公式**载入编辑区**（可以直接改、再保存覆盖）。"""
        if self._loading:
            return
        spec = self.selected_spec()
        if spec is None:
            return
        self._loading = True
        try:
            self.name_edit.setText(spec.name)
            self.editor.setPlainText(spec.source)
            if spec.ok and spec.formula is not None:
                text = f"已载入「{spec.name}」：{spec.formula.describe()}"
                hint = formulas_lib.limit_up_hint(spec.formula)
                if hint:
                    text += "\n⚠️ " + hint
                self._set_hint(text)
            elif not spec.ok:
                self._set_hint(f"❌ 这条公式现在有问题：{spec.error_text}\n"
                               "改完再点【校验】；点【保存】就会覆盖原文件。")
            else:
                self._set_hint(f"已载入「{spec.name}」")
        finally:
            self._loading = False

    def on_toggle_enabled(self, name: str, checked: bool) -> None:
        """勾选「参与选股」→ **立刻写回 config.toml**（保留注释与未知键）。"""
        from laoa_trader.config import save_settings

        names = [str(n) for n in (getattr(self.cfg, "enabled_formulas", None) or [])]
        if checked:
            if name not in names:
                names.append(name)
        else:
            names = [n for n in names if n != name]
        try:
            path, self.cfg = save_settings(self.cfg, {"enabled_formulas": names})
        except OSError as exc:
            # 写不进去（只读盘）：把勾选状态**退回去**，免得界面显示的与实际生效的不一致
            box = getattr(self, "_row_boxes", {}).get(name)
            if box is not None:
                box.blockSignals(True)
                box.setChecked(not checked)
                box.blockSignals(False)
            self._set_hint(f"❌ 保存失败：{exc}（可手改 config.toml 的 enabled_formulas）")
            return
        if checked:
            self._set_hint(
                f"✅ 「{name}」已加入选股（写回 {path.name}）。\n"
                "下次【选股建池】时它会作为「公式」组参与：进池的票来源会标成"
                f"「公式·{name}」。"
            )
            self._toast(f"公式「{name}」已参与选股")
        else:
            self._set_hint(f"「{name}」已退出选股（{path.name} 里的 enabled_formulas 已更新）")
            self._toast(f"公式「{name}」已退出选股")

    # ── 小工具 ────────────────────────────────────────────────────────

    def _set_hint(self, text: str) -> None:
        """写提示区（完整文本留一份给测试/复制）。"""
        self.hint_text = text
        lines = text.splitlines()
        if len(lines) > HINT_MAX_LINES:
            # 成绩单很长：提示区只显示前几行，完整文本在【复制成绩单】里
            shown = lines[:HINT_MAX_LINES]
            shown.append(f"…（还有 {len(lines) - HINT_MAX_LINES} 行，点【复制成绩单】拿全文）")
            text = "\n".join(shown)
        self.hint_label.setText(text)

    def _toast(self, text: str) -> None:
        """一句话提示：优先交给主窗口显示在状态栏，没有就只写日志。"""
        logger.info(text)
        if callable(self.status_cb):
            try:
                self.status_cb(text)
            except Exception:  # noqa: BLE001 - 回调出错不该影响这一页
                logger.debug("公式页状态回调出错", exc_info=True)

    def _confirm(self, question: str) -> bool:
        """二次确认（覆盖/删除）。**可被 monkeypatch**（测试里模拟点"是"）。"""
        answer = QMessageBox.question(
            self,
            "确认",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


def _one_line(text: str, limit: int) -> str:
    """多行错误压成一行（列表里只放得下一行；全文在 tooltip 与提示区）。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "FUNCTIONS",
    "FormulaPage",
    "OPERATORS",
    "PAGE_HINT",
    "PANEL_WIDTH",
    "SAMPLE_NAME",
    "SAMPLE_TEXT",
    "ScorecardWorker",
    "TAB_SPACES",
    "VARIABLES",
]
