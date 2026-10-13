"""决策信号的界面侧：**点一下才算、在后台算、算完出结论窗**（`QThread` 一层的薄壳）。

分工（用户 2026-10-13 定，详见 `docs/AI分析设计.md`）
----------------------------------------------------
* 判定逻辑在 `laoa_trader/analysis.py`（纯函数、不联网、可复现）；
* 这个模块只管**把事实从库里搬过去**：评分、当轮命中的策略、持仓成本、主力资金、
  自选状态 —— 搬完调 `judge_pool` / `judge_position`；
* 界面（`ui/signal_dialog.py`）只管画。

**大模型那条线还没接**（设计稿第 6 节）：它只会往结论里**挂附注**，不改结论。
所以这里的 `build_signals()` 现在一个网络请求都不发 —— 这一点是刻意的：
"关掉附注，结论一模一样"这条纪律，从第一天起就能被测试钉住。

为什么自己起线程而不是跟主界面那 5 秒的拍子走
--------------------------------------------
一次分析要读日线（算评分）、读资金流、查策略台账 —— 几十只票就是几十次库读，
放在界面线程里就是"点一下卡一下"。所以与评分同一个套路：**后台线程 + 信号回界面**，
并且线程**从创建起就登记**在 `ui/threads.py`（跑着的 QThread 绝不许被析构，
见那条 abort 事故的说明）。
"""

from __future__ import annotations

import sqlite3
import weakref
from typing import Any, Callable, Iterable, Sequence

from laoa_trader import analysis, pool as pool_mod, scoring, wording
from laoa_trader.analysis import Condition, KIND_FUND, KIND_POSITION, KIND_RULE, RuleSet
from laoa_trader.data import storage
from laoa_trader.log import get_logger
from laoa_trader.ui import threads as threads_mod

logger = get_logger(__name__)

#: 分析的两条路：自选池（还要不要留着观察）与持仓（持有/减仓/离场）。
KIND_POOL = "pool"
KIND_POSITION = "position"

try:  # Qt 缺失时不该 import 就炸（与其它 ui 模块同一个约定）
    from PySide6.QtCore import QObject, QThread, Signal

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)


# ══════════════════════════════════════════════════════════════════════════
# 搬事实 + 判定（**没有 Qt 也能跑**，测试直接调这里）
# ══════════════════════════════════════════════════════════════════════════


def _dedupe(symbols: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    return [s for s in (str(x or "").strip() for x in symbols)
            if s and not (s in seen or seen.add(s))]


def _names(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, str]:
    """代码 → 名称（`stock_basic`；查不到就空着，不拿代码冒充名称）。"""
    if not symbols:
        return {}
    out: dict[str, str] = {}
    try:
        for chunk_start in range(0, len(symbols), 500):
            chunk = symbols[chunk_start:chunk_start + 500]
            sql = ("SELECT symbol, name FROM stock_basic WHERE symbol IN ("
                   + ", ".join("?" * len(chunk)) + ")")
            for row in conn.execute(sql, tuple(chunk)):
                out[str(row[0])] = str(row[1] or "")
    except sqlite3.Error as exc:  # 老库没这张表也不该让分析失败
        logger.debug(f"取股票名称失败：{exc}")
    return out


def _fund_net_wan(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, float]:
    """主力净额（**万元**）：库里存的是**元**（东财口径），这里换成万元好读。

    为什么用万元：单只票的资金流动辄几十万到几亿，用"元"写出来是一长串数字，
    用户读不出来；万元与界面别处（行业资金那类）同一个量纲。
    只对自选标的采集（`scheduler.sync_watchlist_fund_flow`），查不到就不给这一条依据。
    """
    out: dict[str, float] = {}
    try:
        for symbol, row in storage.latest_fund_flow(conn, symbols).items():
            value = row.get("main_net")
            if value is None:
                continue
            try:
                out[str(symbol)] = float(value) / 1e4
            except (TypeError, ValueError):
                continue
    except sqlite3.Error as exc:
        logger.debug(f"取资金流失败：{exc}")
    return out


def _collect_scores(
    cfg: Any, db_path: Any, symbols: Sequence[str],
    score_getter: Callable[[str], Any] | None,
) -> dict[str, Any]:
    """每只票的评分：**先问界面那份缓存，缺的现算**。

    为什么先问缓存：界面上那两张表的「评分」列就是它算的，同一个数不能有第二个来源
    （两套缓存 = 同一只票两个分）。缺的（用户还没等到后台算完就点了分析）在这里补算：
    分析本来就慢，多算几只不影响体验，而"结论里缺了评分"会直接改变判定结果。
    """
    out: dict[str, Any] = {}
    missing: list[str] = []
    for symbol in symbols:
        cached = None
        if score_getter is not None:
            try:
                cached = score_getter(symbol)
            except Exception as exc:  # noqa: BLE001 - 缓存坏了就当没有
                logger.debug(f"读评分缓存失败（{symbol}）：{exc}")
        if cached is None:
            missing.append(symbol)
        else:
            out[symbol] = cached
    if missing:
        try:
            out.update(scoring.score_symbols(cfg, db_path, missing))
        except Exception as exc:  # noqa: BLE001 - 算不出来就少一条依据，不挡结论
            logger.warning(f"分析时补算评分失败：{exc}")
    return out


def _pool_signals(
    cfg: Any, db_path: Any, symbols: Sequence[str],
    scores: dict[str, Any], rules: RuleSet,
) -> dict[str, Any]:
    """自选池那一路：进场信号成立 / 继续观察 / 建议剔除。"""
    picks: dict[str, str] = {}
    try:
        picks = pool_mod.round_pick_strategies(db_path)
    except Exception as exc:  # noqa: BLE001 - 台账读不到就按"没有命中"算（会在结论里说明）
        logger.debug(f"取这一轮的策略命中失败：{exc}")
    out: dict[str, Any] = {}
    with storage.connect(db_path) as conn:
        names = _names(conn, symbols)
        watch = storage.watchlist_map(conn)
        funds = _fund_net_wan(conn, symbols)
    for symbol in symbols:
        extra: list[Condition] = []
        if symbol in watch:
            extra.append(Condition(name="自选状态", actual="在自选表里（你自己加的）",
                                   met=None, source="自选标的", kind=KIND_RULE))
        if symbol in funds:
            extra.append(Condition(name="主力资金", actual=f"{funds[symbol]:+.0f} 万元",
                                   met=None, source="资金流", kind=KIND_FUND))
        picked = wording.display_strategy(str(picks.get(symbol) or ""))
        out[symbol] = analysis.judge_pool(
            symbol,
            name=names.get(symbol, ""),
            score=scores.get(symbol),
            strategies=[picked] if picked else [],
            extra=extra,
            rules=rules,
        )
    return out


def _position_signals(
    cfg: Any, db_path: Any, symbols: Sequence[str],
    scores: dict[str, Any], rules: RuleSet,
) -> dict[str, Any]:
    """持仓那一路：持有 / 减仓 / 离场信号成立。

    止损止盈线取用户自己在设置里填的比例（`stop_loss` / `take_profit`）——
    触发了就是离场信号，**程序不替用户改他自己定的线**。
    """
    stop = float(getattr(cfg, "stop_loss", 0.05) or 0.0)
    take = float(getattr(cfg, "take_profit", 0.10) or 0.0)
    out: dict[str, Any] = {}
    with storage.connect(db_path) as conn:
        names = _names(conn, symbols)
        positions = storage.load_positions(conn)
        closes = storage.latest_raw_closes(conn, symbols)
        funds = _fund_net_wan(conn, symbols)
    for symbol in symbols:
        row = positions.get(symbol) or {}
        try:
            cost = float(row.get("avg_cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            shares = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            shares = 0
        bar = closes.get(symbol) or {}
        try:
            price = float(bar.get("close")) if bar.get("close") is not None else None
        except (TypeError, ValueError):
            price = None
        stop_hit = take_hit = False
        if cost > 0 and price is not None:
            change = (price - cost) / cost
            stop_hit = stop > 0 and change <= -stop
            take_hit = take > 0 and change >= take
        extra: list[Condition] = []
        if price is not None and bar.get("date"):
            # 口径写在条件那一列：这是**本地收盘价**，不是实时快照（用户据此核对盈亏）
            extra.append(Condition(name="现价口径", actual=f"{bar['date']} 收盘价",
                                   met=None, source="本地日线", kind=KIND_POSITION))
        if symbol in funds:
            extra.append(Condition(name="主力资金", actual=f"{funds[symbol]:+.0f} 万元",
                                   met=None, source="资金流", kind=KIND_FUND))
        out[symbol] = analysis.judge_position(
            symbol,
            name=names.get(symbol, ""),
            shares=shares,
            cost=cost or None,
            price=price,
            score=scores.get(symbol),
            main_net=funds.get(symbol),
            stop_hit=stop_hit,
            take_hit=take_hit,
            stop_pct=stop,
            take_pct=take,
            extra=extra,
            rules=rules,
        )
    return out


def build_signals(
    cfg: Any, db_path: Any, symbols: Iterable[str], kind: str = KIND_POOL,
    *,
    score_getter: Callable[[str], Any] | None = None,
    rules: RuleSet = analysis.DEFAULT_RULES,
) -> dict[str, Any]:
    """给这几只票出结论（`{代码: Signal}`）。**只读库、不发网络请求**。

    Args:
        kind: `KIND_POOL` = 自选池那一路，`KIND_POSITION` = 持仓那一路。
            两路的结论集不同（"持有/减仓/离场"与"进场/观察/剔除"不能混），
            所以必须由调用方指明；认不出的 kind 按池子处理（保守：那是默认入口）。
        score_getter: 读界面评分的缓存（`ScoreService.result`）；不传就现算。
    """
    wanted = _dedupe(symbols)
    if not wanted:
        return {}
    scores = _collect_scores(cfg, db_path, wanted, score_getter)
    if kind == KIND_POSITION:
        return _position_signals(cfg, db_path, wanted, scores, rules)
    return _pool_signals(cfg, db_path, wanted, scores, rules)


# ══════════════════════════════════════════════════════════════════════════
# 后台线程 + 服务
# ══════════════════════════════════════════════════════════════════════════

if QT_AVAILABLE:

    class _SignalWorker(QThread):
        """算一批票的结论（只读库）。**故意不挂 C++ 父对象**，理由与 `ui/scores.py` 完全相同。"""

        done = Signal(dict, str)

        def __init__(self, cfg: Any, db_path: Any, symbols: Sequence[str], kind: str,
                     score_getter: Callable[[str], Any] | None = None,
                     service: Any = None, parent: Any = None) -> None:
            super().__init__(parent)
            threads_mod.keep(self)     # 从创建起就登记：跑着的时候永远有人拿着它
            self.cfg = cfg
            self.db_path = db_path
            self.symbols = [str(s) for s in symbols]
            self.kind = kind
            self.score_getter = score_getter
            #: 服务用**弱引用**记：直接拿着它会和服务形成引用环（服务 → 线程 → 服务），
            #: 那个环一旦被 gc 收掉，跑着的线程就跟着被析构 —— 同 `ui/scores.py` 那条 abort。
            self._service_ref = weakref.ref(service) if service is not None else None
            self._stopped_flag = False

        def _service_stopped(self) -> bool:
            service = self._service_ref() if self._service_ref is not None else None
            return bool(getattr(service, "_stopped", False))

        def stop(self) -> None:
            """由服务在收尾时置位：**置位后不发信号**（接收方可能已经拆了）。"""
            self._stopped_flag = True

        def run(self) -> None:  # noqa: D102 - QThread 接口
            if self._stopped_flag or self._service_stopped():
                return
            try:
                results = build_signals(self.cfg, self.db_path, self.symbols, self.kind,
                                        score_getter=self.score_getter)
            except Exception as exc:  # noqa: BLE001 - 分析失败不该影响界面
                logger.warning(f"后台分析失败：{exc}")
                results = {}
            if self._stopped_flag or self._service_stopped():
                logger.debug("分析线程已取消，丢弃这一批结果")
                return
            self.done.emit(results, self.kind)

    class SignalService(QObject):
        """分析服务：界面点一下 → 后台算 → `updated` 回界面（见 `ui/scores.py` 同一套路）。"""

        #: `(结果, kind)` —— 结果是一批 `{代码: Signal}`
        updated = Signal(dict, str)

        def __init__(self, cfg: Any, db_path: Any, parent: Any = None,
                     score_getter: Callable[[str], Any] | None = None) -> None:
            super().__init__(parent)
            self.cfg = cfg
            self.db_path = db_path
            self.score_getter = score_getter
            self._worker: Any = None
            self._stopped = False

        def analyze(self, symbols: Iterable[str], kind: str = KIND_POOL) -> bool:
            """发起一次分析；**同一时刻只跑一个线程**（在跑就返回 False，让界面如实说）。"""
            if self._stopped:
                return False
            wanted = _dedupe(symbols)
            if not wanted:
                return False
            if self._worker is not None and self._worker.isRunning():
                return False
            worker = _SignalWorker(self.cfg, self.db_path, wanted, kind,
                                   self.score_getter, self)
            worker.done.connect(self._on_done)
            self._worker = worker
            worker.start()
            return True

        @property
        def running(self) -> bool:
            return self._worker is not None and self._worker.isRunning()

        def stop(self) -> None:
            """收尾（与 `ScoreService.stop()` 同一个三步：先落旗 → 摘父对象 → 等/寄存）。"""
            self._stopped = True
            worker = self._worker
            if worker is None:
                return
            try:
                worker.stop()
                worker.setParent(None)
            except RuntimeError:      # 底层对象已经没了
                self._worker = None
                return
            if worker.isRunning():
                worker.wait(5_000)
            threads_mod.park(worker)
            self._worker = None

        def _on_done(self, results: dict, kind: str) -> None:
            if self._stopped:
                return
            self._worker = None
            try:
                if results:
                    self.updated.emit(dict(results), str(kind or KIND_POOL))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"通知界面分析结果失败：{exc}")

else:  # pragma: no cover - 没有 Qt 的环境（CLI）

    class SignalService:  # type: ignore[no-redef]
        """没有 Qt 时的空实现（CLI 直接调 `build_signals`）。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.cfg = args[0] if args else None
            self.db_path = args[1] if len(args) > 1 else None
            self._stopped = True

        def analyze(self, symbols: Iterable[str], kind: str = KIND_POOL) -> bool:
            return False

        @property
        def running(self) -> bool:
            return False

        def stop(self) -> None:
            return None
