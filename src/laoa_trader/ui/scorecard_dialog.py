"""「策略成绩单」对话框：**按需**跑一遍历史成绩 —— 预检 + 后台 + 如实说明。

为什么是「按需打开的对话框」，而不是像旧版那样在「策略筛选」页常驻那两个按钮
----------------------------------------------------------------------------
2026-09 主人把旧版右下角的【看成绩单】【复制成绩单】从界面上移除了，理由是三条：
数据只有 6 个月（`history_years = 0.5` ≈ 120 个交易日），而成绩单自己有
250 个交易日 / 100 只票的**库级门槛**（`research/scorecard.py` 的
`MIN_DB_DAYS` / `MIN_DB_SYMBOLS`）—— 常驻在页面上只会永远显示「样本不足，
无法评估」，点一次还要扫全库几十秒。

2026-10-08 主人的决定是：**要一个入口**，但那三条顾虑必须一起解决。所以这里的做法是：

1. **按需**：入口是一个按钮 + 一个对话框（不占页面、不进状态栏）。不点就不读库，
   于是不存在「随手点一下、等二十秒」的意外；
2. **预检**：打开对话框就先问一句「这个库够不够谈成绩单」，判据**全部取自
   `research.scorecard.db_sufficiency()`** —— 门槛只能有一处定义，
   界面里再写一套 `MIN_DB_DAYS`，将来成绩单本体改了门槛，这里就会说反话。
   不够时把「现在有多少 / 需要多少 / 怎么补」写在最上面，而且**不用红色报错**：
   出厂就带 6 个月数据，命中这一条是**正常状态**，不是程序坏了（红色会让人以为出了故障）。
   **预检不通过也允许继续点** —— 有人就是想先看看自己那条策略长什么样，
   拦着他不如把「样本不足，不能当结论」如实标在结果上；
3. **后台**：计算一律在 `QThread` 里，**预检也一样**（`describe_db` 在大库上要
   `COUNT(DISTINCT)` 扫全表，主线程跑就是「打开对话框先卡十几秒」）。
   工作线程**只管算数**，进度与结果全部用信号回主线程 —— 线程里一个控件都不碰；
4. **如实说明**：算不出来就显示 `—` + 中文原因，样本不足就写「样本不足」。
   这个模块里没有一处「算不出数就填个 0」的兜底 —— 一个漂亮的假数比一片空白危险得多。

口径（与 `formulas.run_scorecard()` 的 docstring 逐条对齐）
--------------------------------------------------------
* 成交口径默认 `B`（D+1 收盘买 → D+2 收盘卖），口径定义取自
  `research/scorecard.py` 的 `CONVENTIONS`（这里不再抄一份）；
* **只算绝对收益，不算 α**：α 要全市场同期等权基准，那是
  `research.scorecard.evaluate_all()` 的活。这里回答的是「这条策略自己赚没赚」，
  文本里必须写清楚，否则用户会把两种数字混着比；
* T+1 是硬约束、一字板买不进要剔除 —— 这两条已经落在 `run_scorecard` 里，
  这里一个字都不重新实现。

为什么单独一个模块：`ui/formula_page.py` 已经 2890 行，而「盘中匹配」与
「历史成绩单」本来就是两件事。分开之后这一块能用 Qt 的 offscreen 平台**单独**建出来测
（`tests/test_scorecard_dialog.py`），不用把整个主窗口拉起来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from laoa_trader import formulas as formulas_lib
from laoa_trader.config import get_config
from laoa_trader.hints import BTN_DOWNLOAD_TEXT, TAB_SETTINGS_TEXT
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm

logger = get_logger(__name__)

try:  # Qt 缺失时不应该 import 就炸（与 `ui/formula_page.py` 同一个约定）
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QDialog,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QProgressBar,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
    )

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - 与 ui/app.py 同一个降级策略
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


#: 收尾时等线程退出的上限（与 `ui/formula_page.py` 的 `THREAD_JOIN_MS` 同一档）。
#: 关窗时**宁可多等这一下**，也不要放着一个还在跑的 `QThread` 被析构 ——
#: Qt 会直接 `QThread: Destroyed while thread is still running` 把进程带走。
THREAD_JOIN_MS = 5000

#: 结果表的列（顺序即显示顺序）。第一列是策略名，其余五列全是要么真数、要么 `—`。
RESULT_COLUMNS: tuple[str, ...] = ("策略", "样本数", "平均收益", "胜率", "t 值", "逐年")

#: 没有数时统一写它。**绝不写 0**：`0.00%` 会被读成「算出来是零」，
#: 而 `—` 的意思才是「这里没有数」（全项目的口径，见 README 的持仓那一节）。
DASH = "—"

#: 表头与按钮上的字**一律说「策略」不说「公式」**（2026-09-23 主人定的显示口径，
#: 判据在 `tests/test_wording.py`：用户看得见的文案里不许出现「公式」二字）。
#: 内部标识符照旧叫 formula / run_scorecard —— 那是数据与代码的名字，不许动。


def _research() -> Any:
    """惰性取 `research/scorecard.py`（与 `formulas._convention` 同一个做法）。

    为什么不在模块顶层 import：那个模块会拖进 pandas，而它是「可选的重活」——
    只在真的要看成绩单时才引入（`research/__init__.py` 的说明）。
    """
    from laoa_trader.research import scorecard as sc

    return sc


# ══════════════════════════════════════════════════════════════════════════
# 要做的事：一条策略 = 名字 + 正文
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ScorecardTarget:
    """要评估的一条策略。

    为什么要包一个类型而不是用 `(名字, 正文)` 元组：调用方**传参顺序传反**
    （正文当名字、名字当正文）在元组上完全看不出来，而这里的第一个受害者是
    编译报错里那句「第 1 行第 1 列」—— 排错要排半天。`_as_targets()` 两种都收，
    方便测试与调用方少写一层。
    """

    name: str
    text: str


def _as_targets(items: Sequence[Any] | None) -> list[ScorecardTarget]:
    """把 `ScorecardTarget` / `(名字, 正文)` 统一成 `ScorecardTarget` 列表。"""
    out: list[ScorecardTarget] = []
    for item in items or ():
        if isinstance(item, ScorecardTarget):
            out.append(item)
            continue
        try:
            name, text = item
        except (TypeError, ValueError):  # pragma: no cover - 调用方传错形状
            logger.warning(f"成绩单收到了看不懂的条目：{item!r}")
            continue
        out.append(ScorecardTarget(str(name), str(text)))
    return out


@dataclass
class ScorecardOutcome:
    """一条策略的结局：结果 / 中文原因 / 没算（取消）。

    为什么三样要挤在一个对象里而不是各发一个信号：界面每个表格行只有**一个**归宿，
    分成三个信号就得在槽里自己拼状态机（先 error 后 result 的乱序一旦出现，
    表格会显示最后到的那个，而它未必是真的）。这里由生产者一次定稿。
    """

    index: int
    name: str
    result: dict | None = None
    error: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """算出来了（**只有这一种情况能显示数字**）。"""
        return self.result is not None

    @property
    def pending(self) -> bool:
        """还没轮到它。"""
        return not self.ok and not self.error and not self.cancelled


class Cancelled(RuntimeError):
    """用户在计算过程中点了【取消】（或关窗）。

    单独一个异常类型：它必须在 `except Exception` 之前被接住、并且**不许**
    被当成「算不出来」写进结果文件 —— 「你取消了」和「它算错了」是两件事。
    只在本模块内部流转，不会冒到界面外。
    """


#: 取消的实现方式说明（写在最近的一次调用点旁边，这里只留一条备忘）：
#: `run_scorecard` 的 `progress_cb` 每 25 只票被调一次，我们在里面抛 `Cancelled`，
#: 让它从那一层的循环里直接冒出来。`load_series` 是带 `finally: conn.close()` 的生成器，
#: 栈展开时它被回收 → 连接照常关掉（不比自然跑完多留一个句柄）。


# ══════════════════════════════════════════════════════════════════════════
# 预检：这个库够不够谈成绩单（判据全部来自 research/scorecard.py）
# ══════════════════════════════════════════════════════════════════════════


def _span_text(info: dict) -> str:
    """库的跨度一句话（日期取不到就写 `—`，不编）。"""
    start = info.get("start") or DASH
    end = info.get("end") or DASH
    rows = int(info.get("rows") or 0)
    return f"{start} → {end}，{rows:,} 行"


def _span_years(info: dict) -> float | None:
    """样本跨度（年）；日期解析不出来就返回 None（**宁可不说，也不编一个数**）。"""
    try:
        start = date.fromisoformat(str(info.get("start")))
        end = date.fromisoformat(str(info.get("end")))
    except (TypeError, ValueError):
        return None
    return (end - start).days / 365.25


def _fix_text() -> str:
    """「怎么补数据」——按钮名从 `hints.py` 取（那里是全项目唯一一份**真的**按钮名）。"""
    return (
        f"怎么补：打开「{TAB_SETTINGS_TEXT} → 数据来源」，把下载年限那一栏"
        f"（history_years，默认 0.5 年 ≈ 6 个月）改成 10，再点【{BTN_DOWNLOAD_TEXT}】重新导一遍 —— "
        "成绩单要的是至少 1 年（250 个交易日）的日线。"
    )


def _plain(text: str) -> str:
    """把别处文案里的 `**加粗**` 标记去掉再显示。

    为什么要有这么一步：`db_sufficiency()` 的原文是给 Markdown 报告写的
    （里面写 `**全市场**`），而这里接的是一个**纯文本控件** —— 星号会原样显示出来，
    读起来像排版出错。只去星号，**一个字都不改**（数字、口径、下一步动作都留着）。
    """
    return str(text).replace("**", "")


def audit_text(*, ok: bool, reason: str, info: dict) -> str:
    """预检结论 → 用户读得懂的中文。

    为什么单独一个纯函数：这段文案是「界面说没说真话」的判据本身，
    不该和 Qt 控件纠缠在一起 —— 用例可以直接断言它的内容（不需要建窗口）。
    """
    sc = _research()
    need = f"{sc.MIN_DB_DAYS} 个交易日 / {sc.MIN_DB_SYMBOLS} 只票起"
    if not info.get("exists", True):
        return (f"⚠️ 本地库还没建起来（{info.get('path')}）：现在算不了成绩单。\n"
                f"先补数据：打开「{TAB_SETTINGS_TEXT} → 数据来源」点【{BTN_DOWNLOAD_TEXT}】。")
    if info.get("error"):
        # 表缺失/库损坏：说清是哪一步读不出来，别让用户以为「数据够了但算不出」
        return (f"⚠️ 本地库读不出行情表（{info['error']}）：现在算不了成绩单。\n"
                f"先在「{TAB_SETTINGS_TEXT} → 数据来源」点【{BTN_DOWNLOAD_TEXT}】把数据下下来。")
    days = int(info.get("days") or 0)
    symbols = int(info.get("symbols") or 0)
    if ok:
        text = (f"✅ 数据够用：库内 {days} 个交易日 / {symbols} 只票（{_span_text(info)}），"
                f"达到成绩单的门槛（{need}）。")
        years = _span_years(info)
        if years is not None and years < sc.SHORT_SAMPLE_YEARS:
            # 刚过门槛的库（250~1000 个交易日）逐年分组只有几个桶，
            # 「每年都为正」的说服力和 10 年样本不是一回事 —— 必须提前说一句
            text += (f"\n注意：样本跨度只有约 {years:.1f} 年（不到 "
                     f"{sc.SHORT_SAMPLE_YEARS:g} 年），下面「逐年」那一列只能当参考。")
        return text
    return (
        f"⚠️ 数据还不够，成绩单只能是「样本不足，不能当结论」\n"
        f"库里现在：{days} 个交易日 / {symbols} 只票（{_span_text(info)}）\n"
        f"成绩单至少要有：{need}（约 1 年）\n"
        f"{_fix_text()}\n"
        f"成绩单自己的判据：{_plain(reason)}\n"
        f"现在也可以点【开始计算】先看看 —— 结果会全程标注样本不足，别拿去当结论。"
    )


def precheck(db_path: str | Path) -> dict:
    """看一眼这个库够不够谈成绩单（**不含任何自定义门槛**）。

    Returns:
        {"ok": bool, "reason": str, "info": dict, "text": str}
        —— `info` 是 `scorecard.describe_db()` 的原样输出（行数/票数/交易日/跨度），
        `reason` 是 `db_sufficiency()` 的原话，`text` 是要显示给用户的那几句中文。
    """
    sc = _research()
    info = sc.describe_db(db_path)
    ok, reason = sc.db_sufficiency(info)
    return {"ok": bool(ok), "reason": reason, "info": info,
            "text": audit_text(ok=bool(ok), reason=reason, info=info)}


# ══════════════════════════════════════════════════════════════════════════
# 计算：逐条策略，一条一条产出（界面因此能边算边填）
# ══════════════════════════════════════════════════════════════════════════


def _error_text(exc: BaseException) -> str:
    """异常 → 一句中文原因（**绝不把 traceback 丢到界面上**）。

    数据问题与程序问题分开说：前者用户自己能解决（去下数据），
    后者得把日志发给作者 —— 两句话的下一步动作完全不同。
    """
    if isinstance(exc, fm.FormulaDataError):
        return f"算不出来：{_plain(exc)}"
    name = type(exc).__name__
    message = _plain(str(exc)).strip() or "没有更多信息"
    return (f"算不出来（{name}）：{message}\n"
            "这不是「库里没数据」那种问题，是程序没兜住 —— 日志里有完整堆栈，可以发给作者。")


def iter_outcomes(
    targets: Sequence[ScorecardTarget],
    db_path: str | Path,
    *,
    conv_key: str,
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[ScorecardOutcome]:
    """逐条策略算成绩单，**算完一条 yield 一条**。

    为什么是生成器而不是「算完返回整个列表」：每条策略都要扫一遍全库（几十秒量级），
    全算完再一起给，用户只能对着空白等；一条一条给，第 1 条算完就能看。

    取消有两处：每条策略**开始前**问一次 `should_cancel()`；
    计算**中途**靠 `progress_cb` 抛 `Cancelled`（见 `Cancelled` 的说明）。
    取消之后剩下的条目标成「没算」，已经算完的那些**留着** —— 它们是真的。
    """
    cancel = should_cancel or (lambda: False)
    for index, target in enumerate(targets):
        if cancel():
            yield ScorecardOutcome(index, target.name, cancelled=True)
            continue
        try:
            formula = fm.compile_formula(target.text, name=target.name)
        except fm.FormulaError as exc:
            # 引擎的中文错误里带行号列号，原样搬过来（界面一个字都不翻译）
            yield ScorecardOutcome(index, target.name, error=f"这条策略写错了：{_plain(exc)}")
            continue
        except Exception as exc:  # noqa: BLE001 - 编译期任何意外都只算这条失败
            yield ScorecardOutcome(index, target.name, error=_error_text(exc))
            continue
        try:
            result = formulas_lib.run_scorecard(
                formula, db_path, progress_cb=progress_cb, conv_key=conv_key,
            )
        except Cancelled:
            yield ScorecardOutcome(index, target.name, cancelled=True)
            continue
        except Exception as exc:  # noqa: BLE001 - 一条算不出来不该带走别的
            logger.warning(f"策略成绩单：{target.name} 算不出来：{exc}")
            yield ScorecardOutcome(index, target.name, error=_error_text(exc))
            continue
        yield ScorecardOutcome(index, target.name, result=result)


def _finite(value: Any) -> float | None:
    """能当数用的才算数；`None` / 非数字 / NaN / ±inf 一律算「没有这个数」。

    为什么必须专门拦 NaN：库里只要有一行缺了行情价（源里那一列是空，写进 SQLite
    就是 NULL，读出来是 NaN），`run_scorecard` 的样本里就会混进 NaN，平均值也跟着
    变成 NaN —— 一路显示出来是 `+nan%`：一个"看着像数、其实是垃圾"的东西。
    **空白比它诚实**。上游口径不归这个模块改（见交付说明里那条上游问题），
    但显示层必须挡住它：这个模块的规矩是"不许显示假的数"。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_pct(value: Any) -> str:
    """收益率 → `+1.23%`；没有数（含 NaN）就 `—`（**不写 0.00%**）。"""
    number = _finite(value)
    return DASH if number is None else f"{number * 100:+.2f}%"


def _bad_numbers(result: dict) -> str:
    """这一行"算出来了但没有可用的数"→ 中文原因；正常返回空串。

    判据只看 NaN：`None` 是"样本不够、本来就没有这个数"（run_scorecard 的
    正常返回），而 NaN 是"算了个东西出来、只是它不是数" —— 后者必须说清，
    否则用户拿着一个 `—` 不知道该去补数据还是该重跑。
    """
    for key, label in (("avg", "平均收益"), ("t", "t 值"), ("win_rate", "胜率")):
        raw = result.get(key)
        if raw is None:
            continue
        if _finite(raw) is None:
            return (f"{label}不是一个数（样本里混进了缺失的行情价，算出来是 NaN）—— "
                    "这一行不能当结论：先在「系统设置 → 数据来源」补齐数据，再重跑一遍。")
    return ""


def _cell_text(outcome: ScorecardOutcome) -> dict:
    """一条结局 → 表格里那五格的字。**没有数就是 `—`**，不猜、不补零。"""
    if not outcome.ok:
        return {name: DASH for name in RESULT_COLUMNS[1:]}
    result = outcome.result or {}
    samples = _finite(result.get("samples"))
    win = _finite(result.get("win_rate"))
    t_stat = _finite(result.get("t"))
    years = result.get("by_year") or []
    return {
        "样本数": DASH if samples is None else str(int(samples)),
        "平均收益": _fmt_pct(result.get("avg")),
        "胜率": DASH if win is None else f"{win * 100:.1f}%",
        "t 值": DASH if t_stat is None else f"{t_stat:.2f}",
        "逐年": "；".join(
            f"{row.get('year')}：{_fmt_pct(row.get('avg'))}（{row.get('n')} 笔）"
            for row in years
        ) or DASH,
    }


def copy_text(*, conv_key: str, audit: dict | None,
              outcomes: Sequence[ScorecardOutcome], db_path: str = "") -> str:
    """要贴到记事本 / 群里的纯文本：**口径 + 数据跨度 + 每条策略的成绩单正文**。

    为什么口径与跨度必须写进文本：这份文本会被**贴出去**，看到它的人（包括几天后的
    自己）不知道它是哪套成交口径、哪段数据算出来的 —— 少了这两行，
    一个「平均 +3%」可以来自 10 年样本，也可以来自 20 个交易日，
    而两者完全不是一回事。每条策略的正文直接取 `run_scorecard` 自己那段
    `text`（口径/样本/逐年/警告都在里面），不在这里重排一遍 ——
    重排就是第二个版式，迟早与界面上的说法对不上。
    """
    lines = [
        "📊 策略成绩单（luweik决策系统）",
        f"成交口径：{_convention_text(conv_key)}",
        "数据来源：本地库（只读、不联网）；下面是绝对收益，不算 α"
        "（α 要全市场同期基准，这个入口不算）。",
    ]
    info = (audit or {}).get("info") or {}
    if db_path:
        lines.append(f"本地库：{db_path}")
    if info:
        lines.append(
            f"数据跨度：{_span_text(info)}"
            f"（{info.get('days', DASH)} 个交易日 / {info.get('symbols', DASH)} 只票）"
        )
    if audit is not None and not audit.get("ok"):
        sc = _research()
        lines.append(
            f"⚠️ 样本不足，不能当结论：库内 {info.get('days', DASH)} 个交易日 / "
            f"{info.get('symbols', DASH)} 只票，成绩单至少要 "
            f"{sc.MIN_DB_DAYS} 个交易日 / {sc.MIN_DB_SYMBOLS} 只票。"
        )
    for outcome in outcomes:
        lines.append("─" * 28)
        if outcome.ok:
            body = _plain(str((outcome.result or {}).get("text") or "")).strip()
            lines.append(body or f"📊 策略成绩单：{outcome.name}（没有可读的正文）")
            # 上面那段正文是 `run_scorecard` 自己排的，里面可能写着 `+nan%`：
            # 紧接着再补一句说明，读到这份文本的人才知道那些 `+nan%` 是什么意思
            problem = _bad_numbers(outcome.result or {})
            if problem:
                lines.append(f"⚠️ 上面这一条不能用：{problem}")
        elif outcome.cancelled:
            lines.append(f"【{outcome.name}】没算完（已取消）")
        elif outcome.error:
            lines.append(f"【{outcome.name}】{outcome.error}")
        else:
            lines.append(f"【{outcome.name}】还没算")
    return "\n".join(lines)


def _convention_text(conv_key: str) -> str:
    """成交口径的中文名（定义取自 `research/scorecard.py` 的 `CONVENTIONS`）。

    与 `formulas._convention()` 同一个出处、同一个理由：口径是**被引用**的东西，
    复制一份到界面里，将来改口径就得记得改两处 —— 一定会漏。
    """
    sc = _research()
    for conv in sc.CONVENTIONS:
        if conv.key == conv_key:
            # 口径的中文名里本来就带着短键（`B 尾盘买·隔日收盘卖`），
            # 再拼一个上去会变成 `B B 尾盘买…`（看着像出错，实测过）
            label = conv.label if conv.label.startswith(conv.key) else f"{conv.key} {conv.label}"
            return f"{label}（{conv.description}）"
    return conv_key


# ══════════════════════════════════════════════════════════════════════════
# 后台线程：只管算数，一个控件都不碰
# ══════════════════════════════════════════════════════════════════════════

if QT_AVAILABLE:

    class PrecheckWorker(QThread):
        """预检线程（`describe_db` 在大库上要扫全表，不能挂在主线程上）。

        为什么不干脆同步跑：小库几十毫秒、大库几十秒 —— 同一个按钮在两种库上
        表现相差三个数量级，而用户只看到「窗口没响应」。放后台 + 一句
        「正在检查数据…」是唯一两种情况下都说得过去的做法。
        """

        finished_ok = Signal(object)
        failed = Signal(object)

        def __init__(self, db_path: str, parent: Any = None) -> None:
            super().__init__(parent)
            self._db_path = db_path

        def run(self) -> None:  # noqa: D102 - QThread 约定
            try:
                result = precheck(self._db_path)
            except Exception as exc:  # noqa: BLE001 - 后台异常必须回主线程说人话
                logger.exception("策略成绩单预检失败")
                self.failed.emit(exc)
            else:
                self.finished_ok.emit(result)

    class ScorecardWorker(QThread):
        """成绩单计算线程：**只算数，绝不碰控件**（进度与结果一律用信号回主线程）。

        与 `ui/formula_page.py` 的 `FormulaWorker` 长得像但**不共用**：那一页的线程
        只服务【运行】一次返回一个 dict，而这里要**逐条**回报（每条一个 `outcome`）。
        共用会逼着两边都写分支；而互相 import 会让「公式页 ↔ 成绩单对话框」
        变成双向依赖（formula_page 要用这个对话框）。
        """

        #: 阶段文案, 已完成, 总数（与全项目的进度回调同一签名，界面拿去画进度条）
        progress = Signal(str, int, int)
        #: 一条策略的结局（`ScorecardOutcome`）
        outcome = Signal(object)
        #: 全部跑完（含被取消的情况）
        finished_all = Signal()
        #: 整个流程意外挂掉（不是某一条算不出来）
        failed = Signal(object)

        def __init__(self, targets: Sequence[ScorecardTarget], db_path: str, *,
                     conv_key: str, parent: Any = None) -> None:
            super().__init__(parent)
            self._targets = list(targets)
            self._db_path = db_path
            self._conv_key = conv_key
            #: 取消标记：主线程置位、工作线程读（CPython 下 bool 读写本身就够安全，
            #: 这里不需要锁 —— 即使晚一轮读到，也只是多扫 25 只票）
            self._cancelled = False

        def cancel(self) -> None:
            """请求取消（**立即返回**，线程会在下一次进度回调时停下来）。"""
            self._cancelled = True

        @property
        def cancelled(self) -> bool:
            return self._cancelled

        def _progress(self, stage: str, done: int, total: int) -> None:
            """进度回调：既是「报进度」，也是**取消的检查点**（每 25 只票一次）。"""
            if self._cancelled:
                raise Cancelled()
            self.progress.emit(stage, done, total)

        def run(self) -> None:  # noqa: D102 - QThread 约定
            try:
                for item in iter_outcomes(
                    self._targets, self._db_path, conv_key=self._conv_key,
                    progress_cb=self._progress,
                    should_cancel=lambda: self._cancelled,
                ):
                    self.outcome.emit(item)
            except Exception as exc:  # noqa: BLE001 - 后台异常必须回主线程说人话
                logger.exception("策略成绩单后台任务失败")
                self.failed.emit(exc)
            else:
                self.finished_all.emit()


# ══════════════════════════════════════════════════════════════════════════
# 对话框
# ══════════════════════════════════════════════════════════════════════════

if QT_AVAILABLE:

    class ScorecardDialog(QDialog):
        """「策略成绩单」对话框。

        属性里刻意留着测试要用的引用（`audit_label` / `verdict_label` / `table` /
        `btn_run` / `btn_copy` / `btn_cancel` / `progress` / `progress_label` /
        `note_label` / `outcomes` / `worker` / `precheck_worker`），
        不要去爬控件层级 —— 摆一次布局就全失效（与 `FormulaPage` 同一个约定）。
        """

        def __init__(
            self,
            parent: Any = None,
            *,
            cfg: Any = None,
            targets: Sequence[Any] | None = None,
            conv_key: str | None = None,
            db_path: str | Path | None = None,
        ) -> None:
            """建对话框。**构造完就开始预检**（后台线程，不阻塞打开）。

            Args:
                cfg: 配置对象（默认全局单例）；库路径默认取它的 `db_path`。
                targets: 要评估的策略（`ScorecardTarget` 或 `(名字, 正文)`），
                    通常来自「策略筛选」页上**勾选**的那些行。
                conv_key: 成交口径短键（默认 `formulas.DEFAULT_CONVENTION_KEY`）。
                db_path: 覆盖库路径（测试用；不传就读 `cfg.db_path`）。
            """
            super().__init__(parent)
            self.cfg = cfg if cfg is not None else get_config()
            self.db_path = str(db_path or getattr(self.cfg, "db_path", "") or "")
            self.conv_key = str(conv_key or formulas_lib.DEFAULT_CONVENTION_KEY)
            self.targets = _as_targets(targets)
            #: 与 `targets` 一一对应的结局（先全部是「还没算」，算完一条填一条）
            self.outcomes: list[ScorecardOutcome] = [
                ScorecardOutcome(index, target.name)
                for index, target in enumerate(self.targets)
            ]
            #: 预检结论（`precheck()` 的原样返回）；还没回来时是 None
            self.audit: dict | None = None
            #: 计算线程（跑完置回 None；测试就等这一条判断「落地了」）
            self.worker: ScorecardWorker | None = None
            #: 预检线程（同上）
            self.precheck_worker: PrecheckWorker | None = None
            #: 点过【开始计算】没有（没点过时【复制】要如实说「还没算」）
            self.started = False

            self._build_ui()
            self._fill_table()
            self._start_precheck()

        # ── 界面 ──────────────────────────────────────────────────────

        def _build_ui(self) -> None:
            self.setWindowTitle("策略成绩单")
            self.resize(900, 620)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 12)
            layout.setSpacing(8)

            # ① 口径说明：**放在最上面**，因为用户读到第一个数字之前就该知道
            #    「这是哪套成交口径、数据从哪来、算的是绝对收益还是 α」。
            self.header_label = QLabel(self._header_text())
            self.header_label.setObjectName("statusTag")
            self.header_label.setWordWrap(True)
            self.header_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(self.header_label)

            # ② 预检
            audit_box = QGroupBox("① 这个库够不够算成绩单（打开时自动查一次）")
            audit_layout = QVBoxLayout(audit_box)
            self.audit_label = QLabel("正在检查数据…")
            self.audit_label.setWordWrap(True)
            self.audit_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            # 醒目靠**加粗 + 分组框**，不靠颜色：界面代码里不许手写色值
            # （见 `ui/theme.py` 的模块说明），而「样本不足」本来也不是报错红
            # —— 出厂 6 个月的数据必然命中它，那是正常状态。
            font = self.audit_label.font()
            font.setBold(True)
            self.audit_label.setFont(font)
            audit_layout.addWidget(self.audit_label)
            layout.addWidget(audit_box)

            # ③ 结果
            result_box = QGroupBox("② 结果")
            result_layout = QVBoxLayout(result_box)
            self.verdict_label = QLabel("")
            self.verdict_label.setWordWrap(True)
            self.verdict_label.setObjectName("statusTag")
            result_layout.addWidget(self.verdict_label)

            self.table = QTableWidget(0, len(RESULT_COLUMNS))
            self.table.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            header = self.table.horizontalHeader()
            # 列宽**必须先按内容量一遍**：默认（Interactive，100px）在"第一列 + 最后一列
            # 都要伸展"的配置下会把中间四列压到标题那么宽 —— 实测 `+1.00%` 被截成
            # `+1.0...`、`100.0%` 截成 `1...`。数字被截断比没有数字更坏：
            # 用户会念出半截数，还可能以为自己看错了。
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            # 策略名那一列吃掉剩余宽度（名字长短差别最大，按内容量反而会把表撑歪）
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setStretchLastSection(True)
            result_layout.addWidget(self.table, 1)

            self.note_label = QLabel("")
            self.note_label.setWordWrap(True)
            self.note_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            result_layout.addWidget(self.note_label)
            layout.addWidget(result_box, 1)

            # ④ 进度：**第几条策略 + 已扫多少只票**（用户在等，就得知道在等什么）
            run_row = QHBoxLayout()
            self.progress = QProgressBar()
            self.progress.setVisible(False)
            self.progress.setTextVisible(True)
            run_row.addWidget(self.progress, 1)
            self.progress_label = QLabel("")
            self.progress_label.setObjectName("statusTag")
            run_row.addWidget(self.progress_label)
            layout.addLayout(run_row)

            # ⑤ 底部按钮
            bottom = QHBoxLayout()
            self.btn_run = QPushButton("开始计算")
            self.btn_run.setObjectName("primaryAction")
            self.btn_run.setToolTip(
                "在当前本地库上把勾选的策略逐条算一遍成绩单：要扫全库，几十秒量级；"
                "数据不足 1 年时结论不可用（结果会如实标注）"
            )
            self.btn_run.clicked.connect(lambda _checked=False: self.on_run())
            bottom.addWidget(self.btn_run)

            self.btn_copy = QPushButton("复制")
            self.btn_copy.setToolTip(
                "把结果复制成纯文本（带成交口径与数据跨度），可以贴进记事本或群里"
            )
            self.btn_copy.clicked.connect(lambda _checked=False: self.on_copy())
            bottom.addWidget(self.btn_copy)

            bottom.addStretch(1)
            self.btn_cancel = QPushButton("取消计算")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setToolTip("停下正在跑的计算：已经算完的那几条会留着")
            self.btn_cancel.clicked.connect(lambda _checked=False: self.on_cancel())
            bottom.addWidget(self.btn_cancel)

            self.btn_close = QPushButton("关闭")
            self.btn_close.clicked.connect(lambda _checked=False: self.close())
            bottom.addWidget(self.btn_close)
            layout.addLayout(bottom)

            self.hint_label = QLabel("")
            self.hint_label.setWordWrap(True)
            self.hint_label.setObjectName("statusTag")
            layout.addWidget(self.hint_label)

        def _header_text(self) -> str:
            """顶部那行口径说明（**纯文本，不带星号**：提示区里写 `**加粗**` 会原样显示）。"""
            name = Path(self.db_path).name if self.db_path else DASH
            return (
                f"成交口径：{_convention_text(self.conv_key)}"
                "（与策略成绩单同一套规则：T+1 强制、一字板买不进就剔除）。\n"
                f"数据来自本地库 {name}（只读、不联网）；算的是绝对收益，"
                "不是 α —— α 要全市场同期等权基准，这个入口不算。\n"
                "每条策略都要扫一遍全库，所以它是按需打开的：不点【开始计算】不会读库。"
            )

        # ── 表格 ──────────────────────────────────────────────────────

        def _fill_table(self) -> None:
            """按 targets 铺好表格行（值先全是 `—`，算完一条填一条）。"""
            self.table.setRowCount(len(self.targets))
            for row, target in enumerate(self.targets):
                self._set_cell(row, RESULT_COLUMNS[0], target.name)
                for column in RESULT_COLUMNS[1:]:
                    self._set_cell(row, column, DASH)
            if not self.targets:
                self.note_label.setText(
                    "没有勾选任何策略：回到「策略筛选」列表，把要评估的策略勾上"
                    "（「竞价策略」那一行不是策略，不参与成绩单）。"
                )

        def _set_cell(self, row: int, column: str, text: str) -> None:
            index = list(RESULT_COLUMNS).index(column)
            item = self.table.item(row, index)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, index, item)
            item.setText(text)

        def _row_cell_text(self, row: int, column: str) -> str:
            """给用例用的读法（不爬控件层级）：表格里某一格的字。"""
            item = self.table.item(row, list(RESULT_COLUMNS).index(column))
            return "" if item is None else item.text()

        # ── 预检 ──────────────────────────────────────────────────────

        def _start_precheck(self) -> None:
            worker = PrecheckWorker(self.db_path)
            worker.finished_ok.connect(self._on_precheck_done)
            worker.failed.connect(self._on_precheck_failed)
            self.precheck_worker = worker
            worker.start()

        def _on_precheck_done(self, result: Any) -> None:
            self._finish_precheck()
            if not isinstance(result, dict):
                self.audit_label.setText("⚠️ 预检没有返回结果：可以照常点【开始计算】看结果。")
                return
            self.audit = result
            self.audit_label.setText(str(result.get("text") or ""))
            self._refresh_verdict()

        def _on_precheck_failed(self, exc: Any) -> None:
            self._finish_precheck()
            # 预检失败**不拦人**：算不算得出来由【开始计算】那一路说了算
            self.audit_label.setText(
                f"⚠️ 预检没跑成（{type(exc).__name__}：{exc}）："
                "可以照常点【开始计算】，每条策略算不出来时会写明原因。"
            )

        def _finish_precheck(self) -> None:
            worker = self.precheck_worker
            if worker is not None:
                if worker.isRunning():
                    worker.wait(THREAD_JOIN_MS)
                self.precheck_worker = None

        def _refresh_verdict(self) -> None:
            """结果区那句「这批数能不能当结论」——**预检不过就必须写在结果旁边**。

            为什么两处都写（预检区写了、结果区还要写）：用户很可能直接往下滚到表格，
            只看数字。数字旁边没有那句，他拿走的就只是一个漂亮的百分比。
            """
            if self.audit is None:
                self.verdict_label.setText("")
                return
            if self.audit.get("ok"):
                self.verdict_label.setText(
                    "预检通过：库的规模够谈成绩单（逐年那一列是否稳，另看每条下面的提示）。"
                )
            else:
                self.verdict_label.setText(
                    "⚠️ 预检没过：下面的数照算，但样本不足，不能当结论 —— "
                    "补够数据（见上面第 ① 栏）后再看一遍。"
                )

        # ── 计算 ──────────────────────────────────────────────────────

        def on_run(self) -> None:
            """【开始计算】：**后台跑**（主线程只画进度），绝不在这里算。"""
            if not self.targets:
                self._set_hint(
                    "没有勾选任何策略：回到「策略筛选」列表勾上要评估的，再来点【开始计算】。"
                )
                return
            if self.worker is not None and self.worker.isRunning():
                # 双击 / 上一次还没跑完又点一次：不动正在跑的那次（白扫一遍库没有意义）
                self._set_hint("还在算，请稍候…（算完会一条条填进上面的表）")
                return
            self.started = True
            self.outcomes = [
                ScorecardOutcome(index, target.name)
                for index, target in enumerate(self.targets)
            ]
            self._fill_table()
            self.note_label.setText("")
            self._refresh_verdict()
            self.btn_run.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            # 先设成"不确定进度"：一条策略要扫多少只票得走一遍库才知道，
            # 让进度条先动起来，比"停在 0% 二十秒"让人安心
            self.progress.setRange(0, 0)
            self.progress.setFormat("正在计算…")
            self.progress.setVisible(True)
            self.progress_label.setText(f"0/{len(self.targets)} 条策略")
            self._set_hint("正在计算：每条策略都要扫一遍全库（几十秒量级），界面可以继续用。")

            worker = ScorecardWorker(self.targets, self.db_path, conv_key=self.conv_key)
            worker.progress.connect(self._on_progress)
            worker.outcome.connect(self._on_outcome)
            worker.finished_all.connect(self._on_finished)
            worker.failed.connect(self._on_failed)
            self.worker = worker
            worker.start()

        def _on_progress(self, stage: str, done: int, total: int) -> None:
            """进度 → 进度条 + 「第几条策略 / 已扫多少只票」。"""
            if total > 0:
                self.progress.setRange(0, total)
                self.progress.setValue(done)
                self.progress.setFormat(f"{done}/{total} 只票")
            else:
                self.progress.setRange(0, 0)
                self.progress.setFormat("正在计算…")
            index = min(self._completed() + 1, len(self.targets))
            self.progress_label.setText(
                f"第 {index}/{len(self.targets)} 条策略 · 已扫 {done}/{total or '?'} 只票"
            )

        def _completed(self) -> int:
            """已经出结局的条数（`_on_progress` 靠它算"现在是第几条"）。"""
            return sum(1 for item in self.outcomes if not item.pending)

        def _on_outcome(self, outcome: Any) -> None:
            """一条策略算完了（回主线程执行）：填那一行，并记下它。"""
            if not isinstance(outcome, ScorecardOutcome):
                return
            index = outcome.index
            if 0 <= index < len(self.outcomes):
                self.outcomes[index] = outcome
            row = index
            if 0 <= row < self.table.rowCount():
                if outcome.ok:
                    for column, text in _cell_text(outcome).items():
                        self._set_cell(row, column, text)
                else:
                    for column in RESULT_COLUMNS[1:]:
                        self._set_cell(row, column, DASH)
            self._append_note(outcome)

        def _append_note(self, outcome: ScorecardOutcome) -> None:
            """算不出来的那几条（以及算出来但数不可用的），把**中文原因**写在表格下面。

            为什么不能用 tooltip 代替：用户不会去悬停一个自己没意识到有问题的格子。
            """
            if outcome.ok:
                problem = _bad_numbers(outcome.result or {})
                if problem:
                    self._add_note_line(f"⚠️ 【{outcome.name}】{problem}")
                return
            reason = outcome.error or ("没算（已取消）" if outcome.cancelled else "还没算")
            prefix = "" if outcome.cancelled else "⚠️ "
            self._add_note_line(f"{prefix}【{outcome.name}】{reason}")
            row = outcome.index
            if 0 <= row < self.table.rowCount():
                item = self.table.item(row, 0)
                if item is not None:
                    item.setToolTip(reason)

        def _add_note_line(self, line: str) -> None:
            lines = [value for value in self.note_label.text().split("\n") if value.strip()]
            lines.append(line)
            self.note_label.setText("\n".join(lines))

        def _on_finished(self) -> None:
            """全部跑完（含取消）：把界面收干净，并说清这一轮到底算出了几条。"""
            cancelled = self.worker is not None and self.worker.cancelled
            self._finish_worker()
            self.progress.setVisible(False)
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            done = [o for o in self.outcomes if o.ok]
            bad = [o for o in self.outcomes if o.error]
            parts = [f"共 {len(self.outcomes)} 条策略：算出来 {len(done)} 条"]
            if bad:
                parts.append(f"{len(bad)} 条算不出来（原因见下面）")
            if any(o.cancelled for o in self.outcomes):
                parts.append("其余的已取消，没算")
            self.progress_label.setText("；".join(parts))
            prefix = "已取消（算完的留着）：" if cancelled else "✅ 计算结束："
            self._set_hint(prefix + "；".join(parts) + "。想看文字版就点【复制】。")
            self._refresh_verdict()

        def _on_failed(self, exc: Any) -> None:
            """整个流程挂了（不是某一条算不出来）：如实说，并把每一行留成 `—`。"""
            self._finish_worker()
            self.progress.setVisible(False)
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            for row in range(self.table.rowCount()):
                for column in RESULT_COLUMNS[1:]:
                    self._set_cell(row, column, DASH)
            message = (f"❌ 成绩单没跑起来（{type(exc).__name__}：{exc}）—— "
                       "日志里有完整堆栈，可以发给作者。")
            self.note_label.setText(message)
            self._set_hint(message)
            self.progress_label.setText("没跑起来")

        def _finish_worker(self) -> None:
            """收尾：等线程**真的退出**再放引用。

            为什么必须等：`finished_all` / `failed` 是跨线程排队投递的，
            在工作线程 `run()` 返回之前就可能已经排到主线程执行了；这时丢掉最后一个
            引用，QThread 会在"线程还没结束"时析构 —— Qt 会直接
            `QThread: Destroyed while thread is still running` 把进程干掉。
            """
            worker = self.worker
            if worker is not None and worker.isRunning():
                worker.wait(THREAD_JOIN_MS)
            self.worker = None

        def on_cancel(self) -> None:
            """【取消计算】：请求停下（**已经算完的那几条留着**，它们是真的）。"""
            worker = self.worker
            if worker is None or not worker.isRunning():
                self._set_hint("现在没有在计算。")
                return
            worker.cancel()
            self.btn_cancel.setEnabled(False)
            self._set_hint("正在取消…（当前这条策略扫到下一段就停，已经算完的会留着）")

        # ── 复制 ──────────────────────────────────────────────────────

        def copy_text(self) -> str:
            """当前结果的纯文本（要贴出去的那一份）。"""
            return copy_text(conv_key=self.conv_key, audit=self.audit,
                             outcomes=self.outcomes, db_path=self.db_path)

        def on_copy(self) -> None:
            """【复制】：把纯文本放进剪贴板。

            三个"不给复制"的情形都**说清为什么**（而不是悄悄复制一份半成品）：
            还没算过 / 正在算（复制会缺几条，用户以为少算了策略）/ 被取消，
            后者照拷，但文本里每条都写着「没算完」。
            """
            if not self.started:
                self._set_hint("还没有成绩单可复制：先点【开始计算】。")
                return
            if self.worker is not None and self.worker.isRunning():
                self._set_hint(
                    "正在计算：等这一轮算完再复制（现在复制会缺几条策略，"
                    "看的人会以为那几条没算）。"
                )
                return
            text = self.copy_text()
            clipboard = QApplication.clipboard()
            try:
                if clipboard is not None:
                    clipboard.setText(text)
            except Exception as exc:  # noqa: BLE001 - 剪贴板取不到不该把窗口带崩
                self._set_hint(f"❌ 复制失败（{type(exc).__name__}：{exc}）："
                               "可以选中上面的文字手工复制。")
                return
            count = sum(1 for item in self.outcomes if item.ok)
            self._set_hint(
                f"✅ 已复制到剪贴板：{len(self.outcomes)} 条策略（其中 {count} 条算出结果），"
                "带成交口径与数据跨度，可以贴进记事本或群里。"
            )

        # ── 提示与关闭 ────────────────────────────────────────────────

        def _set_hint(self, text: str) -> None:
            self.hint_label.setText(text)

        def _stop_threads(self) -> bool:
            """关窗前把两个线程收干净；**收不干净就不许关**。

            为什么宁可拦住用户也不放行：窗口一销毁，还在跑的 `QThread` 会连同它的
            信号接收者一起没掉 —— 轻则 Qt 打一行警告，重则
            `QThread: Destroyed while thread is still running` 直接结束进程。
            「多等一句」和「程序崩掉」之间不需要权衡。
            """
            worker = self.worker
            if worker is not None and worker.isRunning():
                worker.cancel()
            ok = True
            for thread in (self.worker, self.precheck_worker):
                if thread is not None and thread.isRunning():
                    if not thread.wait(THREAD_JOIN_MS):
                        ok = False
            if ok:
                self.worker = None
                self.precheck_worker = None
            return ok

        def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            """点窗口右上角那个叉：收干净再放行；收不干净就**忽略这次关闭**。

            允许关闭时**交给 `QDialog.closeEvent` 去处理**（它会走 `reject()`）：
            `exec()` 那个嵌套事件循环只认 `done()`，自己 `event.accept()` 把窗口藏起来
            是不够的 —— 事件循环还在转，主窗口看起来就像卡死了。
            """
            if not self._stop_threads():
                event.ignore()
                self._set_hint("还在算（或正在检查数据）：等它停下来再关，"
                               "不然程序会崩。也可以先点【取消计算】。")
                return
            super().closeEvent(event)

        def done(self, result: int) -> None:  # noqa: D102 - Qt 约定
            if not self._stop_threads():
                self._set_hint("还在算（或正在检查数据）：等它停下来再关，"
                               "不然程序会崩。也可以先点【取消计算】。")
                return
            super().done(result)


__all__ = [
    "DASH",
    "RESULT_COLUMNS",
    "THREAD_JOIN_MS",
    "Cancelled",
    "PrecheckWorker",
    "ScorecardDialog",
    "ScorecardOutcome",
    "ScorecardTarget",
    "ScorecardWorker",
    "audit_text",
    "copy_text",
    "iter_outcomes",
    "precheck",
]
