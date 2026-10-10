"""个股评分的界面侧：**后台算、算完通知、算过的记住**（`QThread` 一层的薄壳）。

为什么要单独一个服务而不是在刷新表格时顺手算
--------------------------------------------
表格每 5 秒重建一次（`_tick`），而评分要读日线 + 可能取快照 ——
放在刷新的那条路上就是"每 5 秒卡一下"，那是用户能直接感觉到的毛病。
所以这里只有三件事：

1. **按需排队**：界面只说"这几个代码要评分"（`request`），
   已经算过且**行情日 / 模型版本都没变**的**一次都不重算**（`_CACHE_TTL` 之外的变化
   只有行情日本身，见 `scoring.cache_key`）；
2. **一个后台线程算**：批量算完通过 `updated` 信号回到界面线程（Qt 信号跨线程安全）；
3. **失败静默**：算不出来就返回带 `note` 的结果（界面上显示 `—` 并把原因写进 tooltip），
   **绝不让评分把主界面拖死** —— 它是锦上添花的东西（与图标/语音/桌宠同一条纪律）。

为什么缓存键里带**行情日**：收盘后一天的评分不该变（同一天反复点也没必要重算），
但到了下一个交易日必须重算 —— 这正是"用户拿两周前的分和今天比"这类错误的源头。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Sequence

from laoa_trader import scoring
from laoa_trader.log import get_logger

logger = get_logger(__name__)

try:  # Qt 缺失时不该 import 就炸（与其它 ui 模块同一个约定）
    from PySide6.QtCore import QObject, QThread, Signal

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    _QT_ERROR = str(_exc)

if QT_AVAILABLE:

    class _ScoreWorker(QThread):
        """算一批票的评分（**只读库、只读快照**，一个字都不写）。"""

        done = Signal(dict)

        def __init__(self, cfg: Any, db_path: Any, symbols: Sequence[str],
                     parent: Any = None) -> None:
            super().__init__(parent)
            self.cfg = cfg
            self.db_path = db_path
            self.symbols = [str(s) for s in symbols]

        def run(self) -> None:  # noqa: D102 - QThread 接口
            # 服务已经收尾了就别再干活（退出流程里窗口可能已经拆了，
            # 这时候还去读库/取快照，出了问题连日志都写不出去）
            if getattr(self.parent(), "_stopped", False):
                return
            try:
                results = scoring.score_symbols(self.cfg, self.db_path, self.symbols)
            except Exception as exc:  # noqa: BLE001 - 评分失败不该影响界面
                logger.warning(f"后台评分失败：{exc}")
                results = {symbol: scoring.ScoreResult(symbol=symbol,
                                                       note=f"评分失败：{exc}")
                           for symbol in self.symbols}
            self.done.emit(results)

    class ScoreService(QObject):
        """评分缓存 + 后台计算（界面只跟它打交道）。"""

        #: 一批算完 → `{代码: ScoreResult}`（界面收到后刷新那两张表）
        updated = Signal(dict)

        def __init__(self, cfg: Any, db_path: Any, parent: Any = None) -> None:
            super().__init__(parent)
            self.cfg = cfg
            self.db_path = db_path
            self._lock = threading.Lock()
            #: `{代码: (缓存键, 结果)}`
            self._cache: dict[str, tuple[tuple, Any]] = {}
            #: 正在算的那一批（同一时刻只跑一个线程：评分是本地读库 + 偶尔快照，
            #: 堆多个线程只会互相抢 sqlite 连接）
            self._worker: Any = None
            #: 还有哪些代码等着算（同一只票排队期间被反复 request 也不会重复入队）
            self._pending: list[str] = []
            #: 已经发出过请求、还没拿到结果的代码（避免重复排队）
            self._inflight: set[str] = set()
            self._stopped = False

        # ── 对外 ──
        def result(self, symbol: str) -> Any:
            """拿缓存里的评分结果；没算过/已经过日返回 None（界面显示 `—`）。"""
            with self._lock:
                entry = self._cache.get(str(symbol))
            return entry[1] if entry else None

        def request(self, symbols: Iterable[str]) -> list[str]:
            """请评分服务算这几个代码（**只算缺的那些**）；返回这次真正排队的代码。"""
            if self._stopped:
                return []
            wanted: list[str] = []
            with self._lock:
                for raw in symbols:
                    symbol = str(raw or "")
                    if not symbol or symbol in self._inflight:
                        continue
                    cached = self._cache.get(symbol)
                    if cached and self._fresh(cached[1]):
                        continue        # 今天已经算过、模型也没变 → 不重算
                    self._inflight.add(symbol)
                    wanted.append(symbol)
            if not wanted:
                return []
            self._pending.extend(wanted)
            self._start_if_idle()
            return wanted

        def invalidate(self, symbol: str | None = None) -> None:
            """丢掉缓存（None = 全丢）。换了模型版本、或用户手动刷新时用。"""
            with self._lock:
                if symbol is None:
                    self._cache.clear()
                else:
                    self._cache.pop(str(symbol), None)

        def stop(self) -> None:
            """收尾：停线程（退出流程里必须调，否则解释器退出时 QThread 还活着会 abort）。"""
            self._stopped = True
            worker = self._worker
            if worker is not None:
                try:
                    if worker.isRunning():
                        worker.wait(5_000)
                except RuntimeError:      # 底层对象已经没了
                    pass
                self._worker = None

        # ── 内部 ──
        @staticmethod
        def _fresh(result: Any) -> bool:
            """这条缓存还新鲜吗（判据只有一处：`scoring.cache_key` 一模一样）。"""
            if not getattr(result, "ok", False):
                return False            # 上一次没算出来的（缺数据/失败）下次要再试
            return True

        def _start_if_idle(self) -> None:
            if self._worker is not None and self._worker.isRunning():
                return
            with self._lock:
                batch = list(self._pending)
                self._pending.clear()
            if not batch:
                return
            worker = _ScoreWorker(self.cfg, self.db_path, batch, self)
            worker.done.connect(self._on_done)
            self._worker = worker
            worker.start()

        def _on_done(self, results: dict) -> None:
            """一批算完：写缓存 → 把还没算的接着排 → 通知界面。"""
            with self._lock:
                for symbol, result in (results or {}).items():
                    self._cache[str(symbol)] = (scoring.cache_key(result), result)
                    self._inflight.discard(str(symbol))
            self._worker = None
            try:
                if results:
                    self.updated.emit(dict(results))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"通知界面评分结果失败：{exc}")
            self._start_if_idle()

else:  # pragma: no cover - 没有 Qt 的环境（CLI）

    class ScoreService:  # type: ignore[no-redef]
        """没有 Qt 时的空实现（CLI 不走这条路）。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def result(self, symbol: str) -> Any:
            return None

        def request(self, symbols: Iterable[str]) -> list[str]:
            return []

        def invalidate(self, symbol: str | None = None) -> None:
            return None

        def stop(self) -> None:
            return None
