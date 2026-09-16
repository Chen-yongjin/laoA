"""实时行情快照缓存：两张表（「自选股池」/「持仓监控」）要的**现价 / 涨幅**。

为什么要有这个模块
-----------------
1. 两张表的核心三列（现价 / 涨幅 / 盈亏比例）必须是**实时价**，而界面里原来
   只有一个 `_last_price()` —— 它读的是 `stock_daily_hfq.close`，是**后复权**收盘价。
   后复权价与用户手上的真实成本根本不是一套口径（老代码拿它算「盈亏比例」，
   10 送 10 之后会凭空显示 +100%），所以现价这一层必须与"本地日线"分开；
2. 盘中提醒那一路（`intraday.build_alerts`）**每 60 秒已经在拉同一份快照**
   （`HithinkClient.snapshot`），但它算完规则就把价格丢掉了 —— 界面要用同一份数据，
   要么复用它的结果（跨线程、跨模块耦合），要么自己再拉一次（配额翻倍）。
   这里选第三条路：**自己拉，但只在交易时段、每 60 秒、一次批量请求**，
   形状与 `build_alerts` 里那份完全一致（`{symbol: snapshot}`），
   失败就静默降级成"没有快照"（界面退回本地收盘价并**标注**，见 `app._price_cells`）。

数据来源是**可配的**（2026-09-16 用户拍板：默认同花顺，可自己加辅助来源）
--------------------------------------------------------------------------
本模块**不认任何具体来源**：取数一律走 `sources.snapshot_map(cfg, symbols)`
（`data/sources.py` 是来源的真相源：有哪些、用户启用了哪些、各自要什么凭据）。
由此带来两条行为变化：

* **"没配同花顺 Key 就不取" → 改成"没有可用来源就不取"**：东方财富是免 Key 的
   公开来源，用户没填同花顺 Key 但启用了东方财富时，两张表的现价/涨幅**照样能显示**。
   判据是 `sources.usable_sources(cfg)`（启用了 + 已实现 + 凭据齐或免 Key）。
* **单位由 `sources` 归一**（股 / 元）：本模块不再碰价格与成交量的换算；
   原来那个把同花顺 `thscode` 折成裸 6 位的 `snapshot_map(rows)` 也搬去了
   `sources`（新的 `sources.snapshot_map(cfg, symbols)` 同名同义，多做一件事：挑来源）；
   切批（100 只/次）也随之下沉到各来源自己的取数实现里。

设计取舍
--------
- **只在交易时段轮询**：复用 `intraday.SESSIONS` / `now_shanghai()`。收盘后价格不会变，
  继续每 60 秒打接口纯属浪费配额（同花顺是按次限流的，`build_alerts` 那一路也在用）；
- **绝不在下载期间跑**：`state.is_downloading()` 时跳过（导入线程正占着数据库，
  界面这一拍本来就只刷"轻"的部分，见 `app._tick`）；
- **界面线程一次都不阻塞**：取数在 `QThread` 里（`QuoteWorker`），回主线程再更新缓存；
- **没有可用来源就一次请求都不发**（`sources.usable_sources()`，见上）；
- **没有要盯的票也不发**（观察池 + 持仓 + 自选全空时）—— 这既是省配额，
  也让"空库"的测试环境天然不会外呼；
- **过期快照不许当实时价**：不是今天的快照一律视为没有（收盘后重启程序，
  昨天的价格被标成"实时"是最误导人的那种错）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from laoa_trader import intraday, state
from laoa_trader.config import Config
from laoa_trader.data import hithink as hx
from laoa_trader.data import sources
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 快照轮询周期（秒）：与盘中提醒同频（`intraday_interval` 也是 60）——
#: 两张表的现价与提醒那一行的价格必须是同一个时刻的，否则用户会看到"提醒说 12.50、
#: 表里写 12.40"这种自相矛盾（两边都"对"，但看着就是不对）。
REFRESH_SECONDS = 60.0

#: 交易时段内的快照**多久算过期**（秒）。正常 60 秒一刷，这里给 3 倍余量：
#: 连续 3 拍都失败（限流/断网）时，界面宁可退回"本地收盘价（带标注）"，
#: 也不该把一个几分钟前的价格继续当成实时价显示。
STALE_SECONDS = 180.0

#: 一次批量请求最多几只。**这个常数现在只是"一次请求的合理上限"的书面口径**：
#: 真正的切批在同花顺那一路（`hx.AUCTION_BATCH` = 100，接口单次上限）
#: 与东方财富那一路（`eastmoney.SECID_BATCH` = 100）各自的取数实现里。
BATCH = 100


def quote_of(item: dict, at: float | None = None) -> dict:
    """`sources` 的统一口径 → 界面用的快照字典（**两张表读的就是这个形状**）。

    为什么同一件事有两个价格键：界面（`app._price_cells`）读的是 `price`，
    而来源/存储那一层叫 `last_price` —— 两把钥匙都留着，是为了不为了改个名字
    去动 `app.py`（那是另一个人的文件），也让两边的字段一眼能对上。
    `at` 与 `as_of` 同理（`at` 是 `is_fresh` 读的时间戳）。

    Returns:
        `{"symbol", "price", "last_price", "pct", "at", "as_of", "source",
        "name", "prev_close", "open", "high", "low", "volume", "turnover"}`
    """
    moment = time.time() if at is None else float(at)
    price = item.get("last_price")
    stamp = item.get("as_of") or moment
    return {
        "symbol": str(item.get("symbol") or ""),
        "price": price,                  # 界面口径（`app._price_cells` 读这个）
        "last_price": price,             # 来源/存储口径（同一个数的两种叫法）
        "pct": item.get("pct"),
        "at": float(stamp),
        "as_of": float(stamp),
        "source": str(item.get("source") or ""),
        "name": item.get("name"),
        "prev_close": item.get("prev_close"),
        "open": item.get("open"),
        "high": item.get("high"),
        "low": item.get("low"),
        "volume": item.get("volume"),        # 股（由 `sources` 归一）
        "turnover": item.get("turnover"),    # 元
    }


#: A 股的一切"几点钟"都以**北京时间**为准（与 `intraday._TZ_SHANGHAI` 同一个口径）。
#: 为什么这里也要有一份：`time.localtime()` 用的是**本机时区**，
#: 而快照的 `at` 是 unix 时间戳 —— 在 UTC 的机器上（CI / 服务器）直接格式化
#: 会把北京时间 13:46 显示成 05:46，"取数时刻"这个信息就成了错的。
_TZ_SHANGHAI = timezone(timedelta(hours=8))


def snapshot_date(at: Any) -> str:
    """快照取数时刻所属的**北京日期**（`YYYY-MM-DD`）；认不出来返回空串。"""
    try:
        return datetime.fromtimestamp(float(at), _TZ_SHANGHAI).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def snapshot_time_text(at: Any) -> str:
    """快照取数时刻的**北京时间** `HH:MM:SS`（给 tooltip 用）；认不出来返回空串。"""
    try:
        return datetime.fromtimestamp(float(at), _TZ_SHANGHAI).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def is_fresh(quote: dict | None) -> bool:
    """这条快照现在还算"实时价"吗（见 `STALE_SECONDS` 与"必须是今天的"）。"""
    if not quote:
        return False
    at = quote.get("at")
    if not isinstance(at, (int, float)) or at <= 0:
        return False
    moment = intraday.now_shanghai()
    if snapshot_date(at) != moment.strftime("%Y-%m-%d"):
        return False                       # 昨天的快照 == 没有快照
    if intraday.in_session(moment) and (time.time() - at) > STALE_SECONDS:
        return False                       # 时段内连着几拍没取到 → 不装新鲜
    return True


def fetch_snapshot_prices(
    cfg: Config, symbols: list[str], *, client: Any = None
) -> dict[str, dict]:
    """**同步**取一轮快照 → `{symbol: quote}`；任何失败都返回 `{}`（绝不抛）。

    取数交给 `sources.snapshot_map`：它按 `cfg.data_sources` 的顺序挑**第一个能用的来源**
    （没填同花顺 Key 就落到东方财富，见模块说明），本模块只管
    "要不要取、取到的形状怎么给界面"。

    为什么"绝不抛"：这个函数跑在工作线程里，它失败只该表现为"表格退回本地收盘价"，
    不该让界面弹错误、更不该让工作线程把异常带出去（`QuoteWorker` 里也兜了一层）。

    Args:
        cfg: 配置（`data_sources` 决定用哪些来源、按什么顺序）。
        symbols: 要取的本地代码（裸 6 位）。
        client: 注入的**同花顺**客户端（测试用假客户端；给了它就只用它，
            不碰来源注册表）。这条通路是给"离线、确定性"的界面测试留的，
            归一化仍然走 `sources.hithink_rows_to_map` —— 测试看到的形状与生产一致。
    """
    wanted = [s for s in dict.fromkeys(str(s) for s in symbols) if s]
    if not wanted:
        return {}
    if client is not None:
        rows: list[dict] = []
        for start in range(0, len(wanted), hx.AUCTION_BATCH):
            rows.extend(client.snapshot(wanted[start:start + hx.AUCTION_BATCH]))
        return {
            symbol: quote_of(item)
            for symbol, item in sources.hithink_rows_to_map(rows).items()
        }
    if not sources.usable_sources(cfg):
        # 一个能用的来源都没有（都没启用 / 启用了但没配 Key）：**一次请求都不发**
        logger.debug("没有可用数据来源，不取实时快照")
        return {}
    try:
        items = sources.snapshot_map(cfg, wanted)
    except Exception as exc:  # noqa: BLE001 - 取数层任何异常都不该进到界面
        logger.info(f"实时快照取数失败（表格退回本地收盘价）：{exc}")
        return {}
    return {symbol: quote_of(item) for symbol, item in (items or {}).items()}


class QuoteWorker(QThread):
    """把一轮快照取数丢到后台线程跑（界面线程一次都不许被网络按住）。"""

    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., dict], *args: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self) -> None:  # noqa: D102 - QThread 约定
        try:
            self.ready.emit(self._fn(*self._args))
        except Exception as exc:  # noqa: BLE001 - 工作线程异常也必须回主线程
            logger.exception("实时快照线程异常")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class QuoteService(QObject):
    """快照缓存 + 轮询节奏（**没有自己的 QTimer**，由主窗口的 5 秒拍子驱动）。

    为什么不自己起一个 QTimer：主窗口已经有一个 5 秒定时器（测试的 teardown
    逐个盯着 `_timer` / `_market_timer` / `_auction_timer` / `_flash_timer`），
    再加一个就要在每个用例的收尾里多停一个 —— 让 `tick()` 自己按
    `REFRESH_SECONDS` 限流，定时器的数量不变，收尾也不用改。

    属性：
        cache: `{symbol: quote}`（quote 里带 `at` 时间戳，见 `quote_of`）
    """

    updated = Signal()

    def __init__(
        self,
        cfg: Config,
        symbols_provider: Callable[[], list[str]],
        parent: QObject | None = None,
        *,
        fetch: Callable[..., dict] | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._symbols_provider = symbols_provider
        self._fetch = fetch or fetch_snapshot_prices
        self.cache: dict[str, dict] = {}
        #: 上次**发起**取数的时刻（限流用）：请求失败也算发起过，
        #: 否则连续失败时会每 5 秒重试一次，把限流撞得更死
        self._requested_at = 0.0
        self._worker: QuoteWorker | None = None

    # ── 读 ──

    def quote(self, symbol: str) -> dict | None:
        """这只票**当前可用**的实时快照（过期/没有 → None）。"""
        quote = self.cache.get(str(symbol or ""))
        return quote if is_fresh(quote) else None

    def price(self, symbol: str) -> float | None:
        quote = self.quote(symbol)
        return float(quote["price"]) if quote else None

    def prices(self) -> dict[str, float]:
        return {s: float(q["price"]) for s, q in self.cache.items() if is_fresh(q)}

    # ── 节奏 ──

    def should_request(self, now: Any = None) -> bool:
        """现在该不该去取一轮快照（四条门槛，缺一个就一次请求都不发）。"""
        if state.is_downloading():
            return False                       # 下载/导入期间不抢数据库与配额
        # 之前这里是"没配同花顺 Key 就不取"；现在判据是"**有没有能用的来源**"——
        # 东方财富免 Key，用户没填同花顺 Key 时两张表照样该有现价（见模块说明）
        if not sources.usable_sources(self.cfg):
            return False
        if not intraday.in_session(now):
            return False                       # 非交易时段：价格不会变
        try:
            return bool(self._symbols_provider())
        except Exception as exc:  # noqa: BLE001 - 取不到票就当作"没有要盯的"
            logger.debug(f"取快照标的失败（本轮不取）：{exc}")
            return False

    def tick(self, now: Any = None) -> bool:
        """跟着主窗口的拍子走；真的发起了一轮取数就返回 True。"""
        if not self.should_request(now):
            return False
        if time.monotonic() - self._requested_at < REFRESH_SECONDS:
            return False
        self.request()
        return True

    def request(self) -> bool:
        """起一轮后台取数（上一轮还没回来就跳过这一轮）。"""
        if self._worker is not None and self._worker.isRunning():
            return False
        try:
            symbols = list(self._symbols_provider())
        except Exception:  # noqa: BLE001
            return False
        if not symbols:
            return False
        self._requested_at = time.monotonic()
        worker = QuoteWorker(self._fetch, self.cfg, symbols)
        self._worker = worker
        worker.ready.connect(self.apply)
        worker.failed.connect(self._on_failed)
        worker.start()
        return True

    def refresh_now(self, symbols: list[str] | None = None, client: Any = None) -> dict:
        """**同步**取一轮并写进缓存（测试用：注入假客户端，完全离线且即时可断言）。"""
        wanted = list(symbols) if symbols is not None else list(self._symbols_provider())
        # 取数函数的签名固定是 `(cfg, symbols, *, client=None)`（与
        # `fetch_snapshot_prices` 一致）—— 测试注入的假函数照这个签名写，就能直接复用
        quotes = self._fetch(self.cfg, wanted, client=client)
        self._requested_at = time.monotonic()
        self.apply(quotes)
        return dict(self.cache)

    def apply(self, quotes: Any) -> None:
        """把一轮结果并进缓存（`None` / 空 = 什么都不做，不把已有数据抹掉）。"""
        if not isinstance(quotes, dict) or not quotes:
            return
        self.cache.update(
            {str(s): dict(q) for s, q in quotes.items() if isinstance(q, dict)}
        )
        self.updated.emit()

    def _on_failed(self, message: str) -> None:
        """取数失败：只记日志（界面退回本地收盘价并标注，不弹任何提示）。"""
        logger.info(f"实时快照失败（表格退回本地收盘价）：{str(message).splitlines()[0]}")

    def stop(self) -> None:
        """停掉在飞的工作线程（窗口关闭 / 用例收尾）。"""
        worker, self._worker = self._worker, None
        if worker is not None and worker.isRunning():
            worker.wait(3_000)


__all__ = [
    "BATCH",
    "QuoteService",
    "QuoteWorker",
    "REFRESH_SECONDS",
    "STALE_SECONDS",
    "fetch_snapshot_prices",
    "is_fresh",
    "quote_of",
    "snapshot_date",
    "snapshot_time_text",
]
