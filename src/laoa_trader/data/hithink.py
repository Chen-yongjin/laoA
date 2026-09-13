"""同花顺金融数据服务（fuyao.aicubes.cn）REST 客户端。

**移植自服务器版 `sequoia_x/data/hithink_source.py`**，接口契约与错误分类逐条保持一致，
仅改动两处平台相关的地方（见文件末尾"与服务器版的差异"）。

接口契约要点（踩坑记录，原样保留）
----------------------------------
- 认证走 Header `X-api-key`，**不是** query 参数。
  凭据来源（本版）：环境变量 `HITHINK_FINANCE_API_KEY` → `config.toml` 的 `hithink_api_key`。
- 成功判据是**响应体** `code == 0`，HTTP 200 也可能是错误（`2003 Missing X-api-key`）；
  因此这里只认信封 `code`，不认状态码。
- `thscode` 必须带交易所后缀（`600519.SH` / `000001.SZ` / `830799.BJ`），
  同花顺板块指数是 `.TI`（如 `886042.TI`）；项目内部股票代码用**裸 6 位**，
  指数用 `sh.000300` / `ti.881101`，转换见 `to_thscode()` / `to_local_symbol()`。
- 时间参数分两套：毫秒时间戳（`start`/`end`/`date_ms`，Asia/Shanghai 零点）
  与 `YYYY-MM-DD` / `yyyyMMdd` 字符串，不可混用。
- 分页有两套模型：`limit/offset`（快照、代码表）与 `page/size`（涨停池），不要混用。
- 快照**不返回中文名**，名称要另取 `meta/tickers/list`。
- 全市场 dump 端点只返回**预签名 URL**（有效期约 5 分钟），要再 GET 一次下载 Parquet。
- 全市场历史**不要**用 `prices/historical` 逐只拉（5000+ 次请求），用 dump。

本模块只负责"取数并转成 Python 结构"，不写数据库；落库在 `data/storage.py` / `data/sync.py`。

与服务器版的差异（仅这两处，其余逐行一致）
------------------------------------------------
1. **凭据来源**：服务器版还会读 `~/.config/hithink-finance/credentials.env`（与官方 CLI 共用）；
   桌面版按需求只认"环境变量 → config.toml"，避免单机程序依赖隐藏的系统文件。
   `HithinkClient(api_key=...)` 显式传参的用法完全保留（测试注入用）。
2. **dump 默认落盘目录**：服务器版硬编码 `/tmp/...`，Windows 上不存在；
   改为系统临时目录 `tempfile.gettempdir()`。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

logger = logging.getLogger("laoa_trader.data.hithink")

BASE_URL = "https://fuyao.aicubes.cn/api"

#: 统一凭据环境变量（按优先级），与服务器版同名，两套程序可共用同一份配置
_KEY_ENV_VARS = ("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY")

#: 亚洲/上海时区：接口的毫秒时间戳都是"北京时间零点"
_TZ_SHANGHAI = timezone(timedelta(hours=8))

#: A 股交易所后缀推断（纯 6 位代码 → 交易所）
_EXCHANGE_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SH", ("60", "68", "90", "11", "13", "50", "51", "52", "56", "58")),  # 主板/科创/ETF
    ("SZ", ("00", "30", "20", "12", "15", "16", "18", "19")),              # 主板/创业板/ETF
    ("BJ", ("43", "83", "87", "88", "92", "82", "89")),                    # 北交所
)

#: 项目内部（baostock 风格）交易所前缀
_LOCAL_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

#: 可重试的错误码 —— 与官方 Python toolkit 保持一致（fuyao_client.RETRY_CODES）；
#: 其它业务码（含认证、参数、能力不支持）重试无意义，直接抛出。
_RETRY_CODES = frozenset({4001, 5001, 5002, 5003})


class HithinkError(RuntimeError):
    """接口业务错误（信封 `code != 0`）。"""

    def __init__(self, code: int, message: str, path: str = "") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"[{code}] {message}（{path}）" if path else f"[{code}] {message}")


class HithinkAuthError(HithinkError):
    """缺少/无效的 API Key（2002 / 2003 / 2004）。"""


class HithinkRateLimitError(HithinkError):
    """触发限流。"""


class HithinkNotReadyError(HithinkError):
    """数据尚未就绪（4040）。"""


# ── 凭据 ──


def _config_key() -> str | None:
    """从 `config.toml` 的 `hithink_api_key` 取 Key（延迟导入，避免循环依赖）。"""
    try:
        from laoa_trader.config import get_config

        return (get_config().hithink_api_key or "").strip() or None
    except Exception:  # noqa: BLE001 - 配置读不到时当作"没有 Key"，由调用方降级
        return None


def api_key() -> str | None:
    """统一凭据：环境变量优先，其次 config.toml；都缺则返回 None。

    Returns:
        Key 字符串，或 None（调用方据此判断"未配置"，例如 GUI 里提示去填 Key）。
    """
    for name in _KEY_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return _config_key()


def available() -> bool:
    """是否已配置凭据（**不代表**远端可用，线上必须实调一次才算）。"""
    return bool(api_key())


# ── 代码与时间转换 ──


def infer_exchange(ticker: str) -> str:
    """由纯 6 位代码推断交易所（SH/SZ/BJ）。"""
    for exchange, prefixes in _EXCHANGE_PREFIXES:
        if ticker.startswith(prefixes):
            return exchange
    return "SH"  # 兜底：6 开头之外的历史代码极少，交给服务端报错更安全


def to_thscode(symbol: str) -> str:
    """项目内部代码 → 同花顺 thscode。

    `sh.600519` → `600519.SH`；`600519` → `600519.SH`；已是 thscode 则原样返回。
    `.TI` 板块代码原样保留（如 `886042.TI`）。
    """
    raw = (symbol or "").strip()
    if not raw:
        raise ValueError("空代码")
    if "." not in raw:
        ticker = raw.zfill(6)
        if not (len(ticker) == 6 and ticker.isdigit()):
            # 别把拼错的代码发给服务端 —— 服务端只会回一个含糊的参数错误
            raise ValueError(f"无法识别的代码格式：{symbol}")
        return f"{ticker}.{infer_exchange(ticker)}"
    left, _, right = raw.partition(".")
    if left.upper() in ("SH", "SZ", "BJ", "TI"):  # sh.600519 / sh.000300 / ti.886042
        return f"{right}.{left.upper()}"
    if right.upper() in ("SH", "SZ", "BJ", "TI"):  # 已是 thscode
        return f"{left}.{right.upper()}"
    raise ValueError(f"无法识别的代码格式：{symbol}")


def to_local_symbol(thscode: str, asset_type: str = "a-share") -> str:
    """同花顺 thscode → 项目内部代码。

    **项目约定**（与服务器版一致）：`stock_daily_raw` 里的股票代码是**裸 6 位**（`600519`），
    只有指数/板块才带前缀（`sh.000300`、`ti.881101`）。

    Args:
        thscode: 同花顺代码，如 `600519.SH` / `886042.TI`。
        asset_type: `a-share`（股票，默认）或 `index`（指数/板块）。

    Returns:
        `600519`（股票）/ `sh.000300`、`ti.886042`（指数或板块）。
    """
    raw = (thscode or "").strip()
    if "." not in raw:
        ticker = raw.zfill(6)
        if asset_type == "index":
            return f"sh.{ticker}" if ticker.startswith(("000", "1")) else f"sz.{ticker}"
        return ticker
    ticker, _, suffix = raw.partition(".")
    suffix = suffix.upper()
    if suffix == "TI":  # 同花顺板块指数：用 ti. 前缀，避免与真实指数混淆
        return f"ti.{ticker}"
    prefix = _LOCAL_PREFIX.get(suffix)
    if prefix is None:
        raise ValueError(f"无法识别的 thscode：{thscode}")
    if asset_type == "index":
        return f"{prefix}.{ticker}"
    return ticker  # 股票：项目内部不带前缀


def date_to_ms(day: str) -> int:
    """`YYYY-MM-DD` / `YYYYMMDD` → 北京时间零点的毫秒时间戳。"""
    text = day.strip().replace("-", "")
    parsed = datetime.strptime(text, "%Y%m%d").replace(tzinfo=_TZ_SHANGHAI)
    return int(parsed.timestamp() * 1000)


def ms_to_date(ms: int | float | None) -> str:
    """毫秒时间戳 → `YYYY-MM-DD`（北京时间）。"""
    if ms is None:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=_TZ_SHANGHAI).strftime("%Y-%m-%d")


# ── 客户端 ──


class HithinkClient:
    """同花顺 REST 客户端。

    Args:
        api_key: 显式 Key；缺省走 `api_key()`（环境变量 → config.toml）。
        base_url: 接口根地址。
        timeout: 单次请求超时（秒）—— 桌面版最怕"卡住不动"，所以必须给超时。
        retries: 网络错误/限流/5xxx 的有界重试次数（指数退避）。
        pace: 相邻请求的最小间隔（秒）—— 官方要求控制请求节奏，别打高并发。
        session: 可注入的 requests.Session（测试用假会话）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        retries: int = 3,
        pace: float = 0.15,
        session: requests.Session | None = None,
    ) -> None:
        # 凭据解析：显式传参优先。
        #   api_key=None  → 走默认来源（环境变量 → config.toml），适合临时脚本；
        #   api_key=""    → **明确表示没有 Key**（直接报未配置，不再去别处找）。
        # 为什么要区分：调用方传进来的是一份"确定的配置"（例如界面/CLI 用 --config 指定的
        # 那份），如果它没有 Key，就不该悄悄回退到进程里另一个配置对象的 Key ——
        # 那会让人以为"改的配置没生效"，也让测试之间互相串味。
        key = (api_key.strip() if api_key is not None else (globals()["api_key"]() or "")).strip()
        if not key:
            raise HithinkAuthError(
                2003,
                "未配置 API Key：请设置环境变量 HITHINK_FINANCE_API_KEY，"
                "或在 config.toml 里填写 hithink_api_key"
                "（前往 https://fuyao.aicubes.cn/admin 获取）",
            )
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(int(retries), 0)
        self.pace = max(float(pace), 0.0)
        self.session = session or requests.Session()
        self._last_call = 0.0

    # -- 底层 --

    def _sleep_if_needed(self) -> None:
        if self.pace <= 0:
            return
        gap = time.monotonic() - self._last_call
        if gap < self.pace:
            time.sleep(self.pace - gap)

    def request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """发起一次请求并校验业务信封，返回 `data` 字段。

        Raises:
            HithinkAuthError: 2002/2003/2004（Key 缺失或失效，重试无意义）。
            HithinkNotReadyError: 4040（数据尚未就绪）。
            HithinkRateLimitError: 触发限流（可重试码已耗尽）。
            HithinkError: 其它业务错误码。
            requests.RequestException: 网络层错误（已耗尽重试）。
        """
        url = f"{self.base_url}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_exc: Exception | None = None

        for attempt in range(self.retries + 1):
            self._sleep_if_needed()
            try:
                resp = self.session.get(
                    url,
                    params=clean,
                    headers={"X-api-key": self.api_key, "Accept": "application/json"},
                    timeout=self.timeout,
                )
                self._last_call = time.monotonic()
                payload = resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.retries:
                    raise
                wait = 2**attempt
                logger.warning(f"同花顺请求异常（{path}）：{exc}，{wait}s 后重试")
                time.sleep(wait)
                continue
            except ValueError as exc:  # 非 JSON 响应
                last_exc = exc
                if attempt >= self.retries:
                    raise HithinkError(-1, f"响应不是合法 JSON：{exc}", path) from exc
                time.sleep(2**attempt)
                continue

            code = int(payload.get("code", -1))
            if code == 0:
                return payload.get("data") or {}

            message = str(payload.get("message") or "未知错误")
            if code in (2002, 2003, 2004):
                raise HithinkAuthError(code, message, path)
            if code == 4040:
                raise HithinkNotReadyError(code, message, path)
            if code in _RETRY_CODES:
                # 限流/瞬时服务端错误：有界指数退避（官方同样重试这四个码）
                if attempt < self.retries:
                    wait = 1.0 * (2**attempt)
                    logger.warning(f"同花顺返回 {code}（{path}）：{message}，{wait}s 后重试")
                    time.sleep(wait)
                    continue
                raise HithinkRateLimitError(code, message, path)
            raise HithinkError(code, message, path)

        raise HithinkError(-1, f"重试耗尽：{last_exc}", path)

    # -- 元信息 --

    def search(self, q: str, limit: int = 10, asset_type: str | None = "a-share") -> list[dict]:
        """按代码/名称检索标的（消歧为唯一 thscode）。"""
        data = self.request(
            "/meta/tickers/search", {"q": q, "limit": limit, "asset_type": asset_type}
        )
        return list(data.get("item") or [])

    def tickers(
        self,
        asset_type: str | None = "a-share",
        exchange: str = "SH,SZ,BJ",
        page_size: int = 10000,
        max_pages: int | None = None,
    ) -> list[dict]:
        """拉取代码表（含中文名），自动分页。

        代码表是**大结果**：调用方应落盘/写库，不要塞进日志或对话。
        """
        items: list[dict] = []
        offset = 0
        page = 0
        while True:
            data = self.request(
                "/meta/tickers/list",
                {
                    "exchange": exchange,
                    "asset_type": asset_type,
                    "limit": min(page_size, 10000),
                    "offset": offset,
                },
            )
            batch = list(data.get("item") or [])
            items.extend(batch)
            page += 1
            # 终止条件：本页不足 limit，或空页，或达到自设上限
            if len(batch) < min(page_size, 10000) or not batch:
                break
            if max_pages is not None and page >= max_pages:
                break
            offset += min(page_size, 10000)
        return items

    # -- 行情 --

    def snapshot(
        self,
        thscodes: list[str] | None = None,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> list[dict]:
        """行情快照：传 `thscodes` 走批量，否则全市场分页模式。

        注意：快照**不含中文名**；名称请用 `tickers()` 或本地 `stock_basic` 建立映射。
        """
        if thscodes:
            data = self.request(
                "/a-share/prices/snapshot", {"thscodes": ",".join(to_thscode(s) for s in thscodes)}
            )
            return list(data.get("item") or [])

        items: list[dict] = []
        offset = 0
        page = 0
        while True:
            data = self.request(
                "/a-share/prices/snapshot",
                {"limit": min(page_size, 10000), "offset": offset},
            )
            batch = list(data.get("item") or [])
            items.extend(batch)
            page += 1
            if len(batch) < min(page_size, 10000) or not batch:
                break
            if max_pages is not None and page >= max_pages:
                break
            offset += min(page_size, 10000)
        return items

    def historical(
        self,
        symbol: str,
        start: str,
        end: str,
        adjust: str = "forward",
        interval: str = "1d",
    ) -> list[dict]:
        """单只标的的历史日K（`start`/`end` 传 `YYYY-MM-DD`）。

        端点一次只接受一个 thscode；官方 toolkit 把单次窗口控制在 **5 年**以内
        （`_five_year_limit_ms`），这里按 5 年切片，避免踩到窗口上限的 `1003`。
        """
        items: list[dict] = []
        cursor = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
        while cursor <= last:
            chunk_end = min(cursor + timedelta(days=365 * 5 - 2), last)
            data = self.request(
                "/a-share/prices/historical",
                {
                    "thscode": to_thscode(symbol),
                    "interval": interval,
                    "start": date_to_ms(cursor.isoformat()),
                    "end": date_to_ms(chunk_end.isoformat()),
                    "adjust": adjust,
                },
            )
            items.extend(data.get("item") or [])
            cursor = chunk_end + timedelta(days=1)
        return items

    def adjustment_factors(self, symbol: str) -> list[dict]:
        """单只标的的复权事件流（分红/送股/配股）。"""
        data = self.request(
            "/a-share/corporate-actions/adjustment-factors", {"thscode": to_thscode(symbol)}
        )
        return list(data.get("item") or [])

    def index_historical(
        self, thscode: str, start: str, end: str, interval: str = "1d"
    ) -> list[dict]:
        """指数/板块历史 K 线（`thscode` 可为 `000300.SH` 或 `886042.TI`）。"""
        items: list[dict] = []
        cursor = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
        while cursor <= last:
            chunk_end = min(cursor + timedelta(days=365 * 5 - 2), last)
            data = self.request(
                "/a-share-index/prices/historical",
                {
                    "thscode": thscode,
                    "interval": interval,
                    "start": date_to_ms(cursor.isoformat()),
                    "end": date_to_ms(chunk_end.isoformat()),
                },
            )
            items.extend(data.get("item") or [])
            cursor = chunk_end + timedelta(days=1)
        return items

    def trading_days(self) -> list[str]:
        """近一年交易日（`YYYY-MM-DD` 升序）。"""
        data = self.request("/a-share/calendar/trading-days")
        days: list[str] = []
        for row in data.get("item") or []:
            raw = row.get("date")
            if raw:
                text = str(raw)
                days.append(f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text)
        return days

    # -- 估值与财务 --

    def valuations(self, symbols: list[str], chunk: int = 100) -> list[dict]:
        """批量估值快照（PE/PB/PS/PCF），每批最多 100 个代码。

        桌面版的日更流程**不用**估值（需求里明确"估值不需要"），
        保留此方法供将来做估值类因子。
        """
        items: list[dict] = []
        for i in range(0, len(symbols), chunk):
            batch = symbols[i : i + chunk]
            data = self.request(
                "/a-share/valuations/snapshot",
                {"thscodes": ",".join(to_thscode(s) for s in batch)},
            )
            items.extend(data.get("item") or [])
        return items

    # -- 特色数据 --

    def limit_up_pool(self, day: str | None = None, size: int = 200) -> list[dict]:
        """涨停股池（含连板数、封单额、涨停时间、涨停原因），自动翻页取全。

        注意：本端点用 `page/size` 分页（不是 limit/offset），默认 size=50 ——
        实测某日 50 条正好是默认页大小，**不翻页会漏掉一半涨停股**。
        `day=None` 时返回**当天实时**涨停池（盘中提醒用）。
        """
        items: list[dict] = []
        page = 1
        while True:
            data = self.request(
                "/a-share/special-data/limit-up-pool",
                {
                    "date_ms": date_to_ms(day) if day else None,
                    "page": page,
                    "size": min(max(size, 1), 200),
                },
            )
            batch = list(data.get("item") or [])
            items.extend(batch)
            pagination = data.get("pagination") or {}
            pages = int(pagination.get("pages") or 1)
            if page >= pages or not batch:
                break
            page += 1
        return items

    def limit_down_pool(self, day: str | None = None) -> list[dict]:
        """跌停股池。"""
        data = self.request(
            "/a-share/special-data/limit-down-pool",
            {"date_ms": date_to_ms(day) if day else None},
        )
        return list(data.get("item") or [])

    def limit_break_pool(self, day: str | None = None) -> list[dict]:
        """炸板股池。"""
        data = self.request(
            "/a-share/special-data/limit-break-pool",
            {"date_ms": date_to_ms(day) if day else None},
        )
        return list(data.get("item") or [])

    def limit_up_ladder(self) -> list[dict]:
        """近 30 个交易日的连板梯队矩阵。"""
        data = self.request("/a-share/special-data/limit-up-ladder")
        return list(data.get("item") or [])

    # -- 指数与板块 --

    def ths_index_list(self, tag: str = "industry") -> list[dict]:
        """同花顺指数目录（`industry` 行业 / `cn_concept` 概念 / `region` / `tszs`）。"""
        data = self.request("/a-share-index/catalog/ths-index-list", {"tag": tag})
        return list(data.get("item") or [])

    def ths_constituents(self, thscode: str) -> list[dict]:
        """板块或标准指数的当前成分股。"""
        data = self.request(
            "/a-share-index/constituents/ths-stock-list", {"thscode": thscode}
        )
        return list(data.get("item") or [])

    # -- 全市场 dump --

    def dump_url(self, tag: str = "daily-k-10d") -> tuple[str, str]:
        """取全市场 Parquet 的预签名下载 URL。

        Args:
            tag: `daily-k`（10 年全量）/ `daily-k-10d`（近 10 交易日）/ `adjustment-factors`。

        Returns:
            (预签名 URL, 过期时间字符串)。URL 有效期约 5 分钟，**拿到就下**，不要缓存。
        """
        data = self.request(f"/dump/market-dumps/{tag}/download-url")
        url = str(data.get("presigned_url") or "")
        if not url:
            raise HithinkError(-1, "dump 端点未返回 presigned_url", tag)
        return url, str(data.get("presigned_url_expires_at") or "")

    def download_dump(self, tag: str = "daily-k-10d", dest: Path | str | None = None) -> Path:
        """下载全市场 Parquet dump 到本地文件（流式，避免整包进内存）。

        大文件下载用更长的超时（默认 30s 对几百 MB 的 dump 太短），
        并且**只在拿到响应后才写文件** —— 失败时不会留下半截文件骗过"续传"判断。
        """
        url, expires = self.dump_url(tag)
        target = Path(dest) if dest else Path(tempfile.gettempdir()) / f"hithink-{tag}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"下载同花顺 dump：{tag} → {target}（URL 过期时间 {expires or '未知'}）")
        tmp = target.with_suffix(target.suffix + ".part")
        with self.session.get(url, stream=True, timeout=max(self.timeout, 120)) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
        # 原子替换：中途失败时 target 保持原样（续传逻辑据此判断"有没有下好"）
        tmp.replace(target)
        logger.info(f"下载完成：{target}（{target.stat().st_size / 1e6:.1f} MB）")
        return target

    def iter_dump(self, tag: str = "daily-k-10d", keep: bool = False):
        """下载并解析 dump 为 pandas DataFrame。

        Parquet 读取需要 pyarrow（pyproject 已声明；缺失时报错信息会提示）。
        """
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise HithinkError(-1, "需要 pandas 才能解析 dump") from exc

        path = self.download_dump(tag)
        try:
            frame = pd.read_parquet(path)
        except ImportError as exc:  # pragma: no cover
            raise HithinkError(
                -1, "读取 Parquet 需要 pyarrow：请执行 pip install pyarrow"
            ) from exc
        finally:
            if not keep:
                path.unlink(missing_ok=True)
        return frame


def default_client(**kwargs: Any) -> HithinkClient:
    """快捷构造（未配置 Key 时会抛 HithinkAuthError，调用方负责降级）。"""
    return HithinkClient(**kwargs)


def iter_daily_rows(frame: Any) -> Iterator[dict]:
    """把日K dump 的 DataFrame 转成项目内部行结构。

    Returns:
        每行 `{symbol, date, open, high, low, close, volume, turnover}`；
        `symbol` 已转成项目内部格式（裸 6 位），日期是 `YYYY-MM-DD`。
    """
    for row in frame.itertuples(index=False):
        thscode = getattr(row, "thscode", None)
        if not thscode:
            continue
        yield {
            "symbol": to_local_symbol(str(thscode)),
            "date": ms_to_date(getattr(row, "date_ms", None)),
            "open": float(getattr(row, "open_price", 0.0) or 0.0),
            "high": float(getattr(row, "high_price", 0.0) or 0.0),
            "low": float(getattr(row, "low_price", 0.0) or 0.0),
            "close": float(getattr(row, "close_price", 0.0) or 0.0),
            "volume": float(getattr(row, "volume", 0.0) or 0.0),
            "turnover": float(getattr(row, "turnover", 0.0) or 0.0),
        }


def iter_factor_rows(frame: Any) -> Iterator[dict]:
    """把复权事件 dump 的 DataFrame 转成项目内部行结构。"""
    for row in frame.itertuples(index=False):
        thscode = getattr(row, "thscode", None)
        if not thscode:
            continue
        yield {
            "symbol": to_local_symbol(str(thscode)),
            "ex_date": ms_to_date(getattr(row, "ex_date_ms", None)),
            "dividend_per_share": float(getattr(row, "dividend_per_share", 0.0) or 0.0),
            "per_share_bonus": float(getattr(row, "per_share_bonus", 0.0) or 0.0),
            "allotment_ratio": float(getattr(row, "allotment_ratio", 0.0) or 0.0),
            "allotment_price": float(getattr(row, "allotment_price", 0.0) or 0.0),
        }
