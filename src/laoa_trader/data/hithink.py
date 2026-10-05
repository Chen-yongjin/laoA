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

# ── dump 下载参数（dump 是几百 MB 的大文件，"一次 GET 写完"必然不可靠）──
#: 下载失败后的最大尝试次数（每次都会**重新签 URL** 并从断点继续）
DEFAULT_DOWNLOAD_ATTEMPTS = 5
#: 连接超时（秒）——建连慢通常是网络问题，短一点好尽快重试
DEFAULT_CONNECT_TIMEOUT = 15.0
#: 读取超时（秒）——两个数据块之间的最大间隔；几百 MB 的流给它宽松些
DEFAULT_READ_TIMEOUT = 90.0
#: 每次读取的块大小（1 MB）
DOWNLOAD_CHUNK = 1 << 20
#: 重试退避上限（秒）
_MAX_BACKOFF = 30.0

#: 集合竞价快照每批最多多少个代码（接口上限 100）
AUCTION_BATCH = 100


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


class DumpDownloadError(HithinkError):
    """dump 下载失败 —— **结构化**错误（已下载多少 / 卡在哪一步 / 建议怎么办）。

    为什么单独一个异常：几百 MB 的下载失败是**常态**（URL 5 分钟过期、家用宽带抖动、
    公司网络断流），用户需要知道的三件事是"下到哪了""为什么停的""现在该怎么办"，
    而不是一句 `ReadTimeout`。上层（sync）把它转成 `SyncResult` 显示在界面/CLI 上。
    """

    def __init__(
        self,
        tag: str,
        message: str,
        *,
        downloaded: int = 0,
        total: int = 0,
        attempts: int = 0,
        stage: str = "",
        suggestion: str = "",
    ) -> None:
        super().__init__(-1, message, tag)
        self.tag = tag
        self.downloaded = downloaded
        self.total = total
        self.attempts = attempts
        self.stage = stage
        self.suggestion = suggestion

    def as_dict(self) -> dict:
        """给界面/日志用的结构化字段。"""
        return {
            "tag": self.tag, "downloaded": self.downloaded, "total": self.total,
            "attempts": self.attempts, "stage": self.stage, "suggestion": self.suggestion,
            "message": str(self),
        }


class DownloadCancelled(HithinkError):
    """用户取消了下载（`.part` 保留，下次从断点继续）。"""


class _Retry(Exception):
    """内部信号：这次尝试失败，但**保留 .part**、重新签 URL 后继续。"""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


class _Restart(Exception):
    """内部信号：`.part` 不可信（Range 超界 / 校验不过），删掉从头下。"""


# ── 下载与校验的小工具 ──


def format_mb(size: int | float) -> str:
    """人类可读的大小（日志与错误信息里用）。"""
    value = float(size or 0)
    if value >= 1e9:
        return f"{value / 1e9:.2f} GB"
    if value >= 1e6:
        return f"{value / 1e6:.1f} MB"
    if value >= 1e3:
        return f"{value / 1e3:.0f} KB"
    return f"{value:.0f} B"


def parquet_rows(path: Path | str) -> int:
    """Parquet 行数（读不了返回 0）。"""
    try:
        import pyarrow.parquet as pq

        return int(pq.ParquetFile(path).metadata.num_rows)
    except Exception:  # noqa: BLE001 - 损坏/缺依赖都按"读不了"处理
        return 0


def parquet_ok(path: Path | str, min_rows: int = 1) -> bool:
    """文件是不是一个**可读且非空**的 Parquet（下载完整性校验）。

    没有 pyarrow 时退化成"文件大小 > 1KB"（此时本来也读不了 Parquet）。
    """
    target = Path(path)
    try:
        if target.stat().st_size <= 0:
            return False
    except OSError:
        return False
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return target.stat().st_size > 1024
    try:
        return int(pq.ParquetFile(target).metadata.num_rows) >= min_rows
    except Exception:  # noqa: BLE001 - 损坏/被截断
        return False


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _content_total(headers: dict, offset: int) -> int:
    """从响应头推断**文件总大小**。

    - `content-range: bytes 32768-180700000/180700001` → 取斜杠后的总数（206 时最准）；
    - `content-length`：206 时是"本次剩余字节"，要加上偏移；200 时就是总大小。

    （不能用 HEAD 拿大小：预签名 URL 只允许 GET，HEAD 会 403。）
    """
    content_range = str((headers or {}).get("content-range") or "")
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    length = str((headers or {}).get("content-length") or "")
    if length.isdigit():
        return offset + int(length)
    return 0


# ── 断点续传的"还是不是同一代 dump"护栏 ──
#
# 为什么必须有：dump（`daily-k`）是**按天重新生成**的，而续传只看"本地 .part 有多少
# 字节"，`Range` 请求服务端**不校验内容**（它只按字节切）。于是"今天下到一半、明天点
# 继续"会把新版本的尾部贴到旧版本的头部后面，拼出一个**能读、但两天数据混在一起**的
# 文件 —— 对匹配程序来说这比直接报错危险得多。所以：
#   1. 首次响应时把远端的"身份"（ETag / Last-Modified / 总大小）记在 sidecar 里；
#   2. 每次续传请求回来后比对，**任一可得且不同** → 本地半截文件作废、从头下载；
#   3. 远端什么指纹都不给时，退回"跨天的半截文件一律作废"（dump 每天重生成）。

#: `.part` 的身份档案后缀（纯文本，用户可以直接打开看）
_PART_META_SUFFIX = ".meta"


def _remote_identity(headers: dict, offset: int) -> dict[str, str]:
    """从响应头提取远端文件的身份指纹（拿不到的字段留空串）。"""
    head = headers or {}
    total = _content_total(head, offset)
    return {
        "etag": str(head.get("etag") or "").strip(),
        "last_modified": str(head.get("last-modified") or "").strip(),
        "total": str(total) if total else "",
    }


def _read_meta(path: Path) -> dict[str, str]:
    """读 sidecar（不存在/读不了都返回空字典，绝不因此挡住下载）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def _write_meta(path: Path, ident: dict[str, str], tag: str) -> None:
    """记下这一代远端的身份（写不了只记日志，不影响下载）。"""
    lines = [
        f"tag={tag}",
        f"at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        *[f"{key}={value}" for key, value in ident.items()],
    ]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - 档案写不了不该毁掉下载
        logger.debug(f"写 {path.name} 失败（忽略）：{exc}")


def _stale_part_reason(part: Path) -> str:
    """半截文件是不是"上一代 dump"留下的？（跨天一律作废）

    这是远端一个指纹都不给时的兜底判据：dump 每天重新生成，跨天的半截文件
    **必然**来自上一代，接着下就是在拼两个版本。
    """
    try:
        mtime = datetime.fromtimestamp(part.stat().st_mtime)
    except OSError:
        return ""
    if mtime.date() == datetime.now().date():
        return ""
    return (
        f"本地半截文件是 {mtime:%Y-%m-%d %H:%M} 留下的，而 dump 每天重新生成，"
        "接着下会拼出两个版本的混合文件，已作废改为从头下载"
    )


def _identity_mismatch(expected: dict[str, str], current: dict[str, str]) -> str:
    """两代远端身份不一致 → 中文原因；无法判断或一致 → 空串。"""
    for key, label in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        old, new = expected.get(key, ""), current.get(key, "")
        if old and new and old != new:
            return f"远端文件已更新（{label} 由 {old} 变成 {new}）"
    old_total, new_total = expected.get("total", ""), current.get("total", "")
    if old_total and new_total and old_total != new_total:
        return (
            f"远端文件已更新（总大小 {_mb_text(old_total)} → {_mb_text(new_total)}）"
        )
    return ""


def _mb_text(text: str) -> str:
    """`"189000000"` → `"189.0 MB"`（数字认不出来就原样返回）。"""
    try:
        return format_mb(int(text))
    except (TypeError, ValueError):
        return str(text)



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

    def special_pool_total(self, path: str, day: str | None = None) -> int:
        """涨跌停/炸板池的**家数**（只取总数，不拉明细）。

        为什么单独写一个：`size=1` 时信封里的 `pagination.total` 就是全量家数，
        而 `limit_up_pool()` 会为了明细自动翻页到几十上百条 —— 大盘概览只要一个数字，
        把整页明细拉回来纯粹浪费配额（这几路是每 55 秒就可能跑一次的）。

        Args:
            path: 池子端点，见 `laoa_trader.market.LIMIT_UP_PATH` 等常量。
            day: `YYYY-MM-DD` 指定交易日；None = 服务器今日（盘中就是实时值）。

        Returns:
            家数；服务端没给 `pagination` 时退回"本页条数"（契约变化也不至于抛异常）。
        """
        data = self.request(
            path, {"size": 1, "date_ms": date_to_ms(day) if day else None}
        )
        pagination = data.get("pagination") or {}
        try:
            return int(pagination.get("total"))
        except (TypeError, ValueError):
            return len(data.get("item") or [])

    def index_snapshot(self, thscodes: list[str]) -> dict:
        """指数/板块的实时点位与涨跌幅（一次批量取回）。

        ⚠️ 契约要点（实测踩过）：`thscodes` 里**只要有一个不存在的代码，整批都返回
        1002 Unknown thscode** —— 连上证指数都一起拿不到。而代码是用户写在配置里的，
        写错一个不该让整张大盘概览变空，所以这里批量失败后**逐只重试**：
        保留能取到的，取不到的放进 `failed`。

        Args:
            thscodes: 同花顺代码（`000001.SH` / `399006.SZ` / `883404.TI`），
                也接受项目内部写法（`sh.000001` / 裸 6 位）。

        Returns:
            `{"item": [快照行...], "failed": [取不到的代码...]}`；
            `item` 的行就是服务端原样返回的字段（`thscode` / `last_price` /
            `price_change_ratio_pct` / `turnover` ...）。

        Raises:
            HithinkError: 批量失败**且**逐只重试也一只都没取到（Key 无效 / 限流 /
                服务端故障）—— 此时抛出原始的批量错误（比 N 个单只错误更好定位）。
        """
        wanted: list[str] = []
        failed: list[str] = []
        for raw in thscodes or []:
            text = str(raw).strip()
            if not text:
                continue
            try:
                code = to_thscode(text)
            except ValueError:
                # 连格式都不对（例如填了个中文名）：不发请求，直接记成"取不到"
                failed.append(text)
                continue
            if code not in wanted:      # 同一个代码配两遍只请求一次（顺序按配置来）
                wanted.append(code)
        if not wanted:
            return {"item": [], "failed": failed}

        try:
            data = self.request(
                "/a-share-index/prices/snapshot", {"thscodes": ",".join(wanted)}
            )
            return {"item": list(data.get("item") or []), "failed": failed}
        except (HithinkAuthError, HithinkNotReadyError):
            # 与"哪个代码"无关（Key 无效 / 数据未就绪）：逐只重试也是白打，直接抛
            raise
        except Exception as exc:
            batch_error = exc
            logger.warning(
                f"指数批量快照失败（{exc}），改为逐只重试：{'、'.join(wanted)}"
            )

        items: list[dict] = []
        for code in wanted:
            try:
                data = self.request(
                    "/a-share-index/prices/snapshot", {"thscodes": code}
                )
            except Exception as exc:  # noqa: BLE001 - 单只失败必须让其余照常
                logger.info(f"指数 {code} 取数失败（已跳过）：{exc}")
                failed.append(code)
                continue
            batch = list(data.get("item") or [])
            if batch:
                items.extend(batch)
            else:
                failed.append(code)     # 请求成功但没这一只：同样按"取不到"处理
        if not items:
            raise batch_error
        return {"item": items, "failed": failed}

    # -- 集合竞价 / 异动 --

    def auction_snapshot(self, thscodes: list[str], stage: str = "live",
                         chunk: int = AUCTION_BATCH) -> dict:
        """集合竞价快照（9:15–9:25 的**真实买卖盘**）。

        契约要点（照抄实测，别猜）：
        - 端点 `a-share/auction/snapshot`，参数 `thscodes`（1–100 个）与 `stage`；
        - 返回 `data` 里有 `auction_phase` / `data_status` / `total` / `item`；
          `auction_phase="closed"` + `data_status="final"` 表示**竞价已结束、这是终态**；
          非竞价时段是 `closed`/`not_ready` —— 那是**正常状态**（"现在没有竞价数据"），
          不是错误，所以这里**不抛异常**，把 phase/status 原样交回给调用方判断；
        - `item` 字段：`auction_price / auction_pct / auction_volume(手) / auction_amount /
          auction_unmatched / auction_turnover_pct / auction_yesterday_ratio_pct /
          auction_volume_ratio / pre_close_price / open_price / last_price`。
          ⚠️ `auction_unmatched` 实测会出现 **-1**（茅台就是 -1）= "未提供"，
          调用方（`intraday.auction_fields`）必须把负数当"没有"，绝不能读成"卖压 1 手"。

        Args:
            thscodes: 同花顺代码（也接受 `sh.600519` / 裸 6 位），**自动按 100 分批**。
            stage: `live`（默认，竞价进行中/当天快照）。
            chunk: 每批代码数（接口上限 100）。

        Returns:
            `{"item": [行...], "failed": [...], "phase": "…", "status": "…",
              "timestamp": 服务端时间戳}`；一批都没成功时 `item` 为空、`failed` 有值。
        """
        wanted: list[str] = []
        failed: list[str] = []
        for raw in thscodes or []:
            text = str(raw).strip()
            if not text:
                continue
            try:
                code = to_thscode(text)
            except ValueError:
                failed.append(text)
                continue
            if code not in wanted:
                wanted.append(code)
        if not wanted:
            return {"item": [], "failed": failed, "phase": "", "status": "", "timestamp": 0}

        items: list[dict] = []
        phase = status = ""
        timestamp = 0
        size = max(1, min(int(chunk or AUCTION_BATCH), AUCTION_BATCH))
        for i in range(0, len(wanted), size):
            batch = wanted[i : i + size]
            try:
                data = self.request(
                    "/a-share/auction/snapshot",
                    {"thscodes": ",".join(batch), "stage": stage},
                )
            except (HithinkAuthError, HithinkNotReadyError):
                raise                      # 与代码无关（Key/就绪度）：整批都没意义
            except Exception as exc:  # noqa: BLE001 - 一批失败不连累其它批
                logger.warning(f"竞价快照这一批取不到（已跳过）：{exc}")
                failed.extend(batch)
                continue
            items.extend(data.get("item") or [])
            phase = str(data.get("auction_phase") or phase)
            status = str(data.get("data_status") or status)
            timestamp = int(data.get("timestamp") or timestamp or 0)
        return {"item": items, "failed": failed, "phase": phase,
                "status": status, "timestamp": timestamp}

    def anomaly_list(self, tag_codes: list[str] | None = None) -> list[dict]:
        """当日全市场异动（涨停/跌停/大幅上涨下跌/快速反弹跳水）—— **一条请求**。

        为什么不做"逐只查异动"：那是几百次请求（池子+自选+持仓逐只问），
        而这个端点一次就把当日全市场异动给全了，本地按自己的票过滤即可
        （见 `intraday.anomaly_alerts`）。

        Args:
            tag_codes: 只看这些标签（空/None = 全部）。

        Returns:
            `item` 行：`{stock_name, analysis_content(异动原因长文本), keyword_list,
            thscode, tag_name}`。
        """
        params: dict[str, Any] = {}
        codes = [str(c).strip().upper() for c in (tag_codes or []) if str(c).strip()]
        if codes:
            params["tag_codes"] = ",".join(codes)
        data = self.request("/a-share/special-data/anomaly-analysis-list", params)
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

    def download_dump(
        self,
        tag: str = "daily-k-10d",
        dest: Path | str | None = None,
        *,
        progress_cb: Any = None,
        max_attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        verify: bool = True,
        should_stop: Any = None,
        note_cb: Any = None,
    ) -> Path:
        """下载全市场 Parquet dump 到本地文件（**分块 + 断点续传 + 过期自动重签**）。

        为什么要这么麻烦：dump 有几百 MB（`daily-k` 约 180 MB），而预签名 URL
        **只允许 GET、有效期约 5 分钟**（HEAD 会 403）。一次 `requests.get(stream=True)`
        写完的做法，遇到"下载中途 URL 过期 / 家用宽带抖动 / 公司网络断流"就整轮失败，
        而且下次还得从 0 开始 —— 这正是用户"下载不了 10 年数据"的原因。

        现在的做法：

        1. 写 `<目标>.part`，**已下载字节数就是下次的 `Range` 起点**；
        2. 每次尝试都**重新签一次 URL**（过期问题自然解决）并从断点续传；
        3. 有界重试 + 指数退避（默认 5 次），日志写清"从第 X MB 继续，第 N 次尝试"；
        4. 连接超时与读取超时分开设（读取更宽松，见常量）；
        5. 传输完成后校验确实是可读的 Parquet（行数 > 0），**通过后才原子替换**成正式文件名；
        6. 失败时抛 `DumpDownloadError`（结构化：已下载多少 / 卡在哪一步 / 建议），
           上层转成 `SyncResult` 显示，不用裸异常糊用户一脸。

        Args:
            tag: `daily-k` / `daily-k-10d` / `adjustment-factors`。
            dest: 目标文件；缺省放系统临时目录。
            progress_cb: `(已下载字节, 总字节)` 回调（总大小未知时等于已下载）。
            max_attempts: 最大尝试次数（每次都会重新签 URL）。
            connect_timeout / read_timeout: 建连与读取超时（秒）。
            verify: 是否做完 Parquet 完整性校验。
            should_stop: 可选回调，返回真表示用户取消（`.part` 保留、下次继续）。
            note_cb: 可选回调 `(一句话中文状态)` —— 用于界面/命令行显示
                "正在重签 URL 继续下载（第 2 次）"这类**状态**（与进度分开：
                进度是百分比，状态是"此刻在干什么"）。

        Returns:
            下载好的文件路径。

        Raises:
            DumpDownloadError: 重试耗尽（含已下载字节数与建议）。
        """
        target = Path(dest) if dest else Path(tempfile.gettempdir()) / f"hithink-{tag}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        meta_path = Path(str(part) + _PART_META_SUFFIX)
        attempts = max(int(max_attempts), 1)

        downloaded = _file_size(part)
        total = 0
        stage, reason = "准备", ""
        if downloaded:
            logger.info(f"{tag}：发现未下完的 .part（{format_mb(downloaded)}），将从中断处继续")
            _note(note_cb, f"{tag}：发现未下完的文件（{format_mb(downloaded)}），从断点继续")
            # 兜底护栏：跨天的半截文件必然是上一代 dump（每天重新生成），一律作废
            stale = _stale_part_reason(part)
            if stale:
                logger.warning(f"{tag}：{stale}")
                _note(note_cb, f"{tag}：{stale}")
                part.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                downloaded = 0

        for attempt in range(1, attempts + 1):
            # **每次尝试前都以 .part 的真实大小为准**：上一次尝试可能已经写进去一部分
            # （断流前收到的字节），不能还用上一轮的旧值 —— 否则重试会从 0 重下。
            downloaded = _file_size(part)
            try:
                logger.info(
                    f"下载 dump {tag}：第 {attempt}/{attempts} 次尝试，"
                    f"从 {format_mb(downloaded)} 处继续（目标 {target.name}）"
                )
                if attempt > 1:
                    _note(
                        note_cb,
                        f"正在重签 URL 继续下载（第 {attempt} 次，"
                        f"从 {format_mb(downloaded)} 处接着下）",
                    )
                else:
                    _note(note_cb, f"正在下载 {tag}（取签名 URL 并开始传输）")
                downloaded, total = self._download_once(
                    tag, part, downloaded, progress_cb, connect_timeout, read_timeout,
                    should_stop, meta_path,
                )
            except DownloadCancelled:
                logger.info(f"{tag}：用户取消下载（已下载 {format_mb(_file_size(part))} 已保留）")
                _note(note_cb, f"已取消下载（已下 {format_mb(_file_size(part))}，下次接着传）")
                raise
            except _Restart as exc:
                # .part 不可信（远端说 Range 超界 / 服务器忽略了 Range / 远端文件换了
                # 一代）→ 丢掉从头来，这属于"清理残留"，不该让用户看到莫名其妙的报错
                logger.warning(f"{tag}：{exc}，改为从头下载（已下载的 {format_mb(downloaded)} 作废）")
                _note(note_cb, f"{tag}：{exc}，改为从头下载")
                part.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                downloaded, total, stage, reason = 0, 0, "清理残留", str(exc)
            except _Retry as exc:
                stage, reason = exc.stage, exc.reason
                downloaded = _file_size(part)
                logger.warning(
                    f"{tag}：第 {attempt}/{attempts} 次未成功（{exc.stage}：{exc.reason}），"
                    f"已下载 {format_mb(downloaded)}；退避后重签 URL 从断点继续"
                )
                _note(
                    note_cb,
                    f"第 {attempt}/{attempts} 次未成功（{exc.stage}：{exc.reason}），"
                    f"已下 {format_mb(downloaded)}；稍后自动重签 URL 继续",
                )
            else:
                if verify and not parquet_ok(part):
                    # 大小够了但内容不可读（半截/损坏）→ 删掉重来，避免"看起来下好了"
                    size = _file_size(part)
                    part.unlink(missing_ok=True)
                    downloaded, total = 0, 0
                    stage, reason = "完整性校验", f"文件 {format_mb(size)} 但不可读（行数 0）"
                    logger.warning(f"{tag}：{reason}，已丢弃并重下")
                    _note(note_cb, f"{tag}：{reason}，已丢弃并重下")
                else:
                    part.replace(target)
                    meta_path.unlink(missing_ok=True)   # 下好了就不需要身份档案了
                    logger.info(
                        f"下载完成：{target}（{format_mb(_file_size(target))}，"
                        f"共 {attempt} 次尝试）"
                    )
                    _note(
                        note_cb,
                        f"{tag} 下载完成（{format_mb(_file_size(target))}，"
                        f"共 {attempt} 次尝试）",
                    )
                    if progress_cb:
                        _call_progress(progress_cb, _file_size(target), _file_size(target))
                    return target

            if attempt < attempts:
                wait = min(2.0 ** (attempt - 1), _MAX_BACKOFF)
                time.sleep(wait)

        message = (
            f"dump {tag} 下载失败：已下载 {format_mb(downloaded)}"
            + (f" / {format_mb(total)}" if total else "")
            + f"，{attempts} 次尝试后放弃（卡在{stage}：{reason}）"
        )
        suggestion = (
            "检查网络后**重新运行本程序即可从断点继续**（不会从头下载）；"
            "若反复失败，可先把网络调到更稳的环境（或在能下载的机器上下好 "
            f"{tag}.parquet 后拷进数据目录的 dumps 子目录）"
        )
        logger.error(f"{message}；建议：{suggestion}")
        raise DumpDownloadError(
            tag, message, downloaded=downloaded, total=total, attempts=attempts,
            stage=stage, suggestion=suggestion,
        )

    def _download_once(
        self,
        tag: str,
        part: Path,
        downloaded: int,
        progress_cb: Any,
        connect_timeout: float,
        read_timeout: float,
        should_stop: Any = None,
        meta_path: Path | None = None,
    ) -> tuple[int, int]:
        """一次尝试：重签 URL → 从 `downloaded` 处 Range 续传 → 写完当前响应。

        Args:
            meta_path: 身份档案（sidecar）路径。首次（`downloaded == 0`）把远端的
                ETag / Last-Modified / 总大小记进去；续传时比对，**不是同一代就作废重下**
                （dump 每天重新生成，而 Range 请求服务端不校验内容）。

        Returns:
            (已下载字节, 文件总大小)；总大小未知时为 0。

        Raises:
            _Retry: 可重试（URL 过期/网络/校验前的传输中断），`.part` 保留。
            _Restart: `.part` 不可信（Range 超界 / 服务器忽略 Range / 远端换了一代），
                应把 `.part` 与身份档案一起删掉重来。
        """
        url, expires = self.dump_url(tag)      # **每次尝试都重新签**：URL 只活 ~5 分钟
        # `Accept-Encoding: identity`：别让中间代理对响应做 gzip —— 那样"收到的字节数"
        # 会与 Content-Length 对不上，续传会算出错误的断点并误判成"没下完"。
        headers = {"Accept-Encoding": "identity"}
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        try:
            resp = self.session.get(
                url, headers=headers, stream=True,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.RequestException as exc:
            raise _Retry("建连", f"{type(exc).__name__}: {exc}") from exc

        with resp:
            status = int(getattr(resp, "status_code", 200) or 200)
            if status in (401, 403):
                # 预签名 URL 过期/被拒：重新签一次就好（下一次尝试会拿到新 URL）
                raise _Retry("取签名", f"URL 已过期或被拒（HTTP {status}，过期时间 {expires or '未知'}）")
            if status == 416:
                raise _Restart(f"Range 超出远端文件（本地 .part 有 {format_mb(downloaded)}）")
            if status not in (200, 206):
                raise _Retry("传输", f"HTTP {status}")
            if status == 200 and downloaded:
                # 服务器忽略了 Range：如果还按"追加"写就会把文件写坏，必须从头写
                raise _Restart("服务器未按 Range 返回（HTTP 200）")

            resp_headers = getattr(resp, "headers", None) or {}
            ident = _remote_identity(resp_headers, downloaded)
            if downloaded:
                # 续传：先确认"还是同一代 dump"，否则拼出来的文件是两天的混合物
                mismatch = _identity_mismatch(
                    _read_meta(meta_path) if meta_path is not None else {}, ident
                )
                if mismatch:
                    raise _Restart(
                        f"{mismatch}，本地已下载的 {format_mb(downloaded)} 与它不同源"
                    )
            elif meta_path is not None:
                _write_meta(meta_path, ident, tag)

            total = _content_total(resp_headers, downloaded)
            written = downloaded
            _call_progress(progress_cb, written, total or written)
            try:
                with open(part, "ab" if downloaded else "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        _call_progress(progress_cb, written, total or written)
                        if _stop_requested(should_stop):
                            raise DownloadCancelled(
                                -1, f"已取消（已下载 {format_mb(written)}，下次从断点继续）", tag
                            )
            except _Retry:
                raise
            except (requests.RequestException, OSError) as exc:
                # 传输中断：**保留 .part**，下次从 written 处继续
                raise _Retry("传输", f"{type(exc).__name__}: {exc}") from exc

            if total and written != total:
                raise _Retry("传输", f"只下到 {format_mb(written)} / {format_mb(total)}（连接被中断）")
            return written, total

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


def _stop_requested(should_stop: Any) -> bool:
    """用户是否要求取消？（回调出错按"不取消"处理，别让界面小毛病打断下载）"""
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:  # noqa: BLE001
        return False


def _call_progress(progress_cb: Any, done: int, total: int) -> None:
    """调用进度回调；回调本身出错**不能**影响下载（界面回调最容易踩到）。"""
    if progress_cb is None:
        return
    try:
        progress_cb(int(done), int(total))
    except Exception:  # noqa: BLE001
        logger.debug("下载进度回调异常，已忽略", exc_info=True)


def _note(note_cb: Any, text: str) -> None:
    """调用状态回调（"正在重签 URL 继续下载（第 2 次）"这类）；回调出错不影响下载。"""
    if note_cb is None:
        return
    try:
        note_cb(str(text))
    except Exception:  # noqa: BLE001
        logger.debug("下载状态回调异常，已忽略", exc_info=True)


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
