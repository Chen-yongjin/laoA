"""免 Key 的"同花顺客户端替身"+ 全市场扫描（大盘概览与日更共用这一份）。

为什么要有这个模块
------------------
`market.py` 的大盘概览是照着**同花顺客户端**的接口写的（`special_pool_total` /
`index_snapshot` / `request(MARKET_SNAPSHOT_PATH)` 三个方法）。分发版**没有 Key**，
于是概览整页变成 `—`。与其把 `market.py` 的取数逻辑分叉成"有 Key / 没 Key"两套
（那必然出现"界面和命令行对不上""两个来源的口径漂移"），不如在这里做一个**同花顺
客户端的替身**：同样的三个方法、同样的返回形状，`market.py` 一行不用改就能跑。

于是"没配 Key 时谁来兜底"这件事只落在**一个地方**：`fetch_overview` 选哪个客户端
（正常情况用同花顺；没 Key / Key 失效才落到这里）。

口径（全部是本机实测，不是推断；实测日期 2026-09-17）
----------------------------------------------------
* **代码表**：新浪 `Market_Center.getHQNodeData`（`node=hs_a`）→ 实测 **5564 只 / 55 页**，
  含北交所（`bj920000` 这种），每页 100 只。
* **全市场快照**：腾讯 `qt.gtimg.cn/q=`（100 只/批）→ 实测 **5564 只 / 56 个请求 / 60.5 秒**
  （本机网络 92 只/秒；用户家宽会明显更快）。
* **成交额自校验**：把这一趟扫出来的逐只 `[35]` 成交额按交易所相加，得到
  沪 **8685.26 亿**、深 **9543.21 亿**；而指数快照给的是 沪 8687.73 / 深 9543.61，
  其中"深证Ａ指"正好 **9543.21 亿**、"Ａ股指数"正好 **8685.27 亿** —— 两条路算出来的
  同一个数逐字节对上（差的那 2 亿是沪市 B 股），说明代码表没漏票、成交额字段没串位。
* **涨停家数**：按"现价 == 涨停价"判（腾讯的 `[47]` 涨停价是**按板块算好的**：
  实测 600519 → 昨收×1.10、300750/688111 → ×1.20、bj920000 → ×1.30）。
  实测这一趟扫出 50 只，而同花顺涨停池当天是 47 只 —— 差的 3 只**全是 `*ST`**
  （002528/002569/603838，腾讯给的涨停价就是昨收×1.10，现价正好压在涨停价上）。
  核对库里 1188 个交易日的同花顺涨停池：**`is_st` 全为 0、且不含任何北交所代码** ——
  也就是说同花顺这个"涨停家数"的口径本来就是**沪深两市、不含 ST**。
  为了让"加不加 Key"不至于让同一个数字跳 3 只，这里**对齐同花顺的口径**
  （见 `LIMIT_EXCLUDES_ST` / `LIMIT_EXCLUDES_BJ`），而不是自作主张换个口径。
* **炸板家数**：等价定义 = "最高价摸到涨停价、但现价低于涨停价"。
  涨停价是当日的硬顶（现价不可能超过它），所以"摸到过涨停"与"最高 == 涨停价"等价；
  再排除现价仍封在涨停价的（那是涨停不是炸板）。实测当天 23 只。
* **涨跌家数**：一天里**停牌**的票（腾讯回成交量 0、涨跌幅 0.00，实测 12 只）**不计入**
  —— 它们既没涨也没跌，算成"平盘"会凭空多出十几只平盘。这一条与同花顺
  "快照里没有涨跌幅的行跳过"是同一个意思。

⚠️ 这些是**免 Key 但未授权**的公开接口：随时可能改字段或限流。所以本模块的每一路
都只"少一项"，绝不编数；`market.py` 的逐路降级照旧生效。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Sequence

from laoa_trader.data import public_quotes as pq
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 新浪全 A 股列表（`node=hs_a` 含沪深北；每页 100 只是实测的稳定值）
SINA_LIST_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num={num}&sort=symbol&asc=1&node=hs_a"
)
#: 一页多少只（新浪实测 100 稳定）
LIST_PAGE = 100
#: 页间/批间间隔（秒）：公开接口上"快"没有意义，"被封"才是代价
LIST_PACE = 0.05
SCAN_PACE = 0.08

#: 快照缓存 TTL（秒）。**这是本模块最要紧的一个数**：一趟全市场扫描是 56 个请求，
#: 而概览页每 55 秒刷一次 —— 不缓存就等于"每 55 秒把全市场重扫一遍"，那是自己找死
#: （限流/封 IP）。所以扫描结果按 5 分钟缓存，与 `market._BREADTH_TTL` 同一个节拍：
#: 页面上"每分钟刷新"的只有指数与成交额（一个请求），涨跌停/涨跌家数走 5 分钟这一档，
#: 并且页脚把这件事写清楚（见 `market.footer_text`）。
SCAN_TTL = 300.0
#: 代码表缓存 TTL（秒）：一天也就变几只票，没必要每次都花 55 个请求去取。
LIST_TTL = 6 * 3600.0

#: 判"涨停/跌停"时允许的价格误差（元）。腾讯的价格是两位小数，直接比相等会因为
#: float 表示出现 7.180000000000001 这种尾巴；半分钱以内都算压在同一价位上。
_PRICE_EPS = 0.005

#: 哪些票**不计入**涨停/跌停家数（口径对齐同花顺，理由见模块 docstring 的实测）。
#: 这不是"漏了"，是"跟着参照物的口径走"：加了 Key 之后同一个数字不该跳。
LIMIT_EXCLUDES_ST = True
LIMIT_EXCLUDES_BJ = True


def _urllib_get(url: str, headers: dict, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 http(s) 主机
        return response.read()


def _label_of(symbol: str) -> str:
    """`920000` → `bj920000`（腾讯/新浪要的写法）；按 `public_quotes.market_prefix` 的规则。"""
    return f"{pq.market_prefix(symbol)}{symbol}"


# ── 代码表 ──


def list_symbols(*, opener: Callable | None = None, timeout: float = pq.TIMEOUT,
                 pace: float = LIST_PACE) -> list[str]:
    """全 A 股裸 6 位代码（**含北交所**）。

    为什么用新浪这个接口而不是腾讯：腾讯只认"你给我的这批代码"，没有"给我全部"的开关；
    新浪这个节点列表接口能给出**全市场**（实测 5564 只）。代码表拿到手之后，
    快照那一趟走腾讯（100 只/批、字段最全，含涨停价）。

    返回顺序：新浪按 symbol 升序，**原样保留**（调用方若需要稳定顺序，别在这里重排 ——
    分页汇总时"稳定"比"好看"重要）。
    """
    fetch = opener or _urllib_get
    out: list[str] = []
    seen: set[str] = set()
    page = 1
    while True:
        url = SINA_LIST_URL.format(page=page, num=LIST_PAGE)
        try:
            raw = fetch(url, {"User-Agent": pq._UA, "Referer": "https://finance.sina.com.cn/"},
                        timeout)
            text = raw.decode("gbk", errors="replace").strip()
            rows = json.loads(text) if text.startswith("[") else []
        except Exception as exc:  # noqa: BLE001 - 取不到就"少票"，绝不编一个代码出来
            logger.info(f"新浪代码表第 {page} 页失败：{exc}")
            break
        if not rows:
            break
        for row in rows:
            code = str((row or {}).get("symbol") or "").strip()
            code = code[2:] if len(code) > 6 else code       # `sh600519` → `600519`
            if len(code) == 6 and code.isdigit() and code not in seen:
                seen.add(code)
                out.append(code)
        if len(rows) < LIST_PAGE:
            break
        page += 1
        if pace:
            time.sleep(pace)
    if out:
        logger.info(f"免 Key 代码表：{len(out)} 只（{page} 页）")
    return out


# ── 单趟扫描（两个用途共用：概览的家数 + 日更的当日 K 线）──


def _clean(row: dict) -> dict:
    """腾讯的"哨兵值"清理：`-1`/`0` 在这里表示"没有这个概念"，不是价格。

    实测：`bj430047` 这种腾讯没有数据的票，涨停价/跌停价回 `-1`、最高价回 `0.00`，
    而现价却还是昨天的收盘价 —— 不清理的话，"最高价 0 == 涨停价 -1"这种组合
    会被算进炸板，凭空多出几只。
    """
    out = dict(row)
    for key in ("limit_up", "limit_down", "high", "low", "open", "avg_price"):
        value = out.get(key)
        if value is not None and value <= 0:
            out[key] = None
    out["exchange"] = _exchange_of(str(out.get("symbol") or ""))
    return out


def _exchange_of(symbol: str) -> str:
    """裸 6 位代码 → `SH` / `SZ` / `BJ`（认不出返回空串，不计入分桶）。"""
    return {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(pq.market_prefix(symbol), "")


def scan(symbols: Sequence[str] | None = None, *, opener: Callable | None = None,
         timeout: float = pq.TIMEOUT, pace: float = SCAN_PACE) -> list[dict]:
    """全市场（或指定代码）一趟快照 → 归一化行列表（含 开/高/低/现价/昨收/量/额/涨跌幅/涨跌停价）。

    Args:
        symbols: 裸 6 位代码；`None` = 全市场（自己先取代码表，多花 55 个请求）。
        opener: 注入的 HTTP 层（测试用）。
        pace: 批间间隔（秒）。

    Returns:
        行列表（**停牌但代码存在的票也在里面**，它们成交量为 0）；一只都没取到就是空列表。
    """
    codes = list(symbols) if symbols is not None else list_symbols(
        opener=opener, timeout=timeout)
    if not codes:
        return []
    rows = pq.snapshot(codes, opener=opener, timeout=timeout, pace=pace)
    out = [_clean(row) for row in rows.values()]
    # 顺序稳定（按代码）：概览的分页汇总与日更的写入都吃"顺序别乱跳"这件事
    out.sort(key=lambda row: str(row.get("symbol") or ""))
    return out


#: 模块级扫描缓存 —— **不是**优化，是"别把自己封了"：见 `SCAN_TTL` 的说明。
_SCAN: dict[str, Any] = {"symbols": None, "at": 0.0, "rows": []}
#: 代码表缓存（单独一档，TTL 更长）
_LIST: dict[str, Any] = {"at": 0.0, "symbols": []}


def clear_cache() -> None:
    """清空缓存（测试与"想立刻重扫一遍"时用）。"""
    _SCAN.update({"symbols": None, "at": 0.0, "rows": []})
    _LIST.update({"at": 0.0, "symbols": []})


def cached_scan(*, force: bool = False, opener: Callable | None = None,
                timeout: float = pq.TIMEOUT, symbols: Sequence[str] | None = None,
                ttl: float = SCAN_TTL) -> list[dict]:
    """带 5 分钟缓存的 `scan()`（概览的三路家数都从这里出数，因此只扫一趟）。"""
    key = "all" if symbols is None else ",".join(str(s) for s in symbols)
    if not force and _SCAN.get("symbols") == key:
        if time.monotonic() - float(_SCAN.get("at") or 0.0) < ttl:
            return list(_SCAN.get("rows") or [])
    rows = scan(symbols, opener=opener, timeout=timeout)
    if rows:                       # 空结果**不写缓存**：否则一次网络抖动会锁死 5 分钟
        _SCAN.update({"symbols": key, "at": time.monotonic(), "rows": list(rows)})
    return rows


def cached_symbols(*, force: bool = False, opener: Callable | None = None,
                   timeout: float = pq.TIMEOUT, ttl: float = LIST_TTL) -> list[str]:
    """带 6 小时缓存的代码表。"""
    if not force and _LIST.get("symbols"):
        if time.monotonic() - float(_LIST.get("at") or 0.0) < ttl:
            return list(_LIST["symbols"])
    codes = list_symbols(opener=opener, timeout=timeout)
    if codes:
        _LIST.update({"at": time.monotonic(), "symbols": list(codes)})
    return codes


# ── 家数统计 ──


def _is_st(row: dict) -> bool:
    """名称里带 `ST` 就算（`*ST`/`ST`/`SST` 都含这个子串；A 股没有别的名字带 ST）。"""
    return "ST" in str(row.get("name") or "").upper()


def _excluded_from_limits(row: dict) -> bool:
    """这一只该不该从"涨停/跌停家数"里剔掉（口径理由见模块 docstring）。"""
    if LIMIT_EXCLUDES_ST and _is_st(row):
        return True
    return bool(LIMIT_EXCLUDES_BJ and row.get("exchange") == "BJ")


def _at_limit(price: Any, limit: Any) -> bool:
    """现价是否**压在**涨/跌停价上（两位小数的价格，留半分钱误差）。"""
    if price is None or limit is None:
        return False
    return abs(float(price) - float(limit)) < _PRICE_EPS


def counts(rows: Iterable[dict]) -> dict:
    """一趟扫描 → `{up, down, break, adv, dec, flat, total, exchanges}`。

    - `up` / `down` / `break`：涨停 / 跌停 / 炸板家数（沪深、不含 ST，对齐同花顺口径）；
    - `adv` / `dec` / `flat`：上涨 / 下跌 / 平盘家数（**含 ST 与北交所**，停牌不计入）；
    - `exchanges`：`{"SH"|"SZ"|"BJ": {"count", "turnover", "volume"}}`（成交额单位 = 元）。
      这一栏是**自校验**用的：按交易所相加应当等于指数快照给的成交额（实测逐字节对上）。
    """
    up = down = break_ = 0
    adv = dec = flat = 0
    exchanges: dict[str, dict[str, float]] = {}
    for row in rows:
        bucket = exchanges.setdefault(
            str(row.get("exchange") or "") or "??",
            {"count": 0, "turnover": 0.0, "volume": 0.0},
        )
        if row.get("exchange"):
            bucket["count"] += 1
            bucket["turnover"] += float(row.get("turnover") or 0.0)
            bucket["volume"] += float(row.get("volume") or 0.0)

        if not _excluded_from_limits(row):
            if _at_limit(row.get("last_price"), row.get("limit_up")):
                up += 1
            elif _at_limit(row.get("last_price"), row.get("limit_down")):
                down += 1
            # 炸板：当天摸到过涨停价（涨停价是硬顶，所以"最高 == 涨停价"等价于"摸到过"），
            # 但现价已经不在涨停价上（否则它是涨停，不是炸板）
            elif (_at_limit(row.get("high"), row.get("limit_up"))
                  and row.get("last_price") is not None
                  and float(row["last_price"]) < float(row["limit_up"]) - _PRICE_EPS):
                break_ += 1

        # 停牌（成交量为 0）不算涨跌：它既没涨也没跌，算"平盘"会凭空多十几只
        if not row.get("volume"):
            continue
        pct = row.get("pct")
        if pct is None:
            continue
        if pct > 0:
            adv += 1
        elif pct < 0:
            dec += 1
        else:
            flat += 1
    return {
        "up": up, "down": down, "break": break_,
        "adv": adv, "dec": dec, "flat": flat,
        "total": len(list(rows)) if isinstance(rows, list) else adv + dec + flat,
        "exchanges": exchanges,
    }


# ── 同花顺客户端的替身 ──

#: 概览用到的三个"同花顺接口路径"里，我们只认这三个后缀（`market.py` 拼的是完整路径，
#: 这里按后缀认，避免两边各存一份完整路径导致漂移）。
_POOL_KEYS: dict[str, str] = {
    "limit-up-pool": "up",
    "limit-down-pool": "down",
    "limit-break-pool": "break",
}


class PublicMarketClient:
    """免 Key 的"同花顺客户端替身"：只实现 `market.py` 用到的那三个方法。

    为什么做成类而不是三个函数：`market.py` 是**按客户端写的**（构造一次、连着调几个
    方法），做成同形状的类，`fetch_overview` 就只需要换一个构造，不用分叉取数逻辑 ——
    分叉的代价是"界面走的是一套、命令行走的是另一套"，那正是这个项目反复踩过的坑。

    一个实例内部共享一趟扫描（`self._counts` 懒加载）：概览会先后问涨停、跌停、炸板
    三个数，**只扫一趟**。

    ⚠️ 这里**没有** `force` 参数，是故意的：概览页的「立即刷新」按钮走的是
    `fetch_overview(force=True)`，如果那个 force 能穿透到扫描层，"刷新"就变成
    "重扫全市场"——实测那是 **111 个请求 / 103 秒**（55 个代码表 + 56 个快照），
    而且概览每 55 秒就会重取一次。刷新按钮只刷新**便宜的那部分**（指数与成交额，
    一个请求）；家数走 5 分钟缓存，页脚把这件事写明了（`market.footer_text`）。
    """

    def __init__(self, *, opener: Callable | None = None, timeout: float = pq.TIMEOUT,
                 pace: float = SCAN_PACE) -> None:
        self._opener = opener
        self._timeout = timeout
        self._pace = pace
        self._counts: dict | None = None

    # 与 `hx.HithinkClient` 同名的属性：`market.py` 的报错文案会读它
    provider = "public"

    def _stat(self) -> dict:
        if self._counts is None:
            rows = cached_scan(opener=self._opener, timeout=self._timeout)
            if not rows:
                # ⚠️ 空结果**必须抛**，不能返回全 0：`counts([])` 会给出"涨停 0 家"，
                # 而 0 家涨停是一种真实状态 —— 把"断网/被限流"显示成"今天一只涨停都没有"
                # 是本项目定义的最严重的一类错误（编数）。上层 `_fetch_limits` 会把
                # 它变成"取不到"（界面显示 `—`）。
                raise RuntimeError("全市场扫描没有结果（网络不可用或被限流？）")
            self._counts = counts(rows)
        return self._counts

    def special_pool_total(self, path: str) -> int:
        """涨停 / 跌停 / 炸板家数（路径后缀 → 哪一个，见 `_POOL_KEYS`）。"""
        suffix = str(path or "").rstrip("/").rsplit("/", 1)[-1]
        key = _POOL_KEYS.get(suffix)
        if key is None:
            raise ValueError(f"免 Key 源没有这一个池子：{path}")
        value = self._stat().get(key)
        return int(value)

    def index_snapshot(self, codes: Sequence[str]) -> dict:
        """指数点位与涨跌幅（`000001.SH` 这种 thscode → 腾讯）。

        ⚠️ 这里**必须**按用户给的交易所前缀取，不能按代码首字符猜：`000001` 既是
        上证指数也是平安银行（深市），猜错就把指数点位显示成银行股价。
        """
        wanted: list[str] = []
        mapping: dict[str, str] = {}
        unsupported: list[str] = []
        for code in codes or []:
            thscode = str(code or "").strip()
            if not thscode:
                continue
            head, _, suffix = thscode.partition(".")
            suffix = suffix.upper()
            if suffix in ("SH", "SZ", "BJ"):
                label = f"{suffix.lower()}{head}"
            elif suffix == "TI":       # 同花顺板块指数：公开源没有对应标的
                # 不放进 `failed`：`failed` 在界面上是"你配的这个代码有问题"的意思，
                # 而这一类是"公开源**根本没有**这种标的"，两件事混在一起会让用户
                # 去改一个其实没写错的代码。这一组的说明由 `market._note_public_limits()`
                # 统一给一句（按组说，比逐只列 6 个代码清楚）。
                unsupported.append(thscode)
                continue
            else:
                label = _label_of(head)
            mapping[thscode] = label
            if label and label not in wanted:
                wanted.append(label)

        by_label = pq.batch_quotes(wanted, opener=self._opener,
                                   timeout=self._timeout, pace=self._pace) if wanted else {}
        items: list[dict] = []
        failed: list[str] = []
        for thscode, label in mapping.items():
            row = by_label.get(label)
            if not row or row.get("last_price") is None:
                failed.append(thscode)
                continue
            items.append({
                "thscode": thscode,
                "last_price": row.get("last_price"),
                "price_change_ratio_pct": row.get("pct"),
                "turnover": row.get("turnover"),
            })
        return {"item": items, "failed": failed, "unsupported": unsupported}

    def request(self, path: str, params: dict | None = None) -> dict:
        """全市场快照的**分页**（`market._fetch_breadth` 就是按 limit/offset 翻的）。

        数据来自同一趟 5 分钟缓存扫描，所以这里翻 6 页**不再发任何请求** ——
        这是把"免 Key 扫描"做成缓存的另一个好处：翻页免费。
        """
        suffix = str(path or "").rstrip("/").rsplit("/", 1)[-1]
        if "snapshot" not in suffix:
            raise ValueError(f"免 Key 源只提供全市场快照分页，不认这一路：{path}")
        params = params or {}
        try:
            limit = int(params.get("limit") or 0) or 1000
            offset = int(params.get("offset") or 0)
        except (TypeError, ValueError):
            limit, offset = 1000, 0
        rows = cached_scan(opener=self._opener, timeout=self._timeout)
        page = rows[offset:offset + limit]
        return {
            "total": len(rows),
            "item": [
                {
                    "thscode": f"{row.get('symbol')}.{row.get('exchange')}",
                    "turnover": row.get("turnover"),
                    "volume": row.get("volume"),
                    # ⚠️ 停牌（成交量为 0）这一栏必须是 **None**，不能给 0.00：
                    # `market._fetch_breadth` 对"没有涨跌幅"的行是**跳过**的（与同花顺
                    # 快照同一语义），给 0.00 会把 12 只停牌票算成"平盘"，于是同一页上
                    # `counts()` 算出平盘 157、界面却显示 169 —— 同一个概念两个数。
                    # 实测就是这么发现的（2026-09-17：157 vs 169，差额正好是 12 只停牌）。
                    "price_change_ratio_pct": (row.get("pct") if row.get("volume") else None),
                }
                for row in page
            ],
        }
