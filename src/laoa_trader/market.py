"""大盘概览：涨跌停家数 + 沪深北成交额 + 宽基/情绪/板块三组指数。

这一层**刻意不碰界面**：`fetch_overview()` 只吃一份 `Config` 和一个"能取数的对象"
（真实 `HithinkClient` 或测试用的假客户端），把结果整理成一个普通 dict，
再由 `lines()` 统一排版成"一行一组、组名在前"的文本。「大盘概览」页与
`--cli --market` 用的都是这一份实现 —— 两处口径必须一致，
否则用户会看到"界面和命令行对不上"。

「大盘概览」页长这样（组在配置里为空时，那一行整行不显示）：

    涨停 55 · 跌停 16 · 炸板 30 ｜ 沪 7792亿 · 深 8499亿 · 北 140亿
    上涨 3126 · 下跌 2224 · 平盘 221
    宽基：上证 3885.33 -0.07% ｜ 深成 13384.57 -0.64% ｜ …
    情绪：同花顺情绪 885.53 -0.18% ｜ 昨日连板 6928.17 +3.53% ｜ …
    板块：银行 1408.77 +0.88% ｜ 证券 1428.64 -0.27%

三条硬性规矩（都是踩过坑才写下来的）
------------------------------------
1. **降级优先**：任何一路（涨跌停 / 指数三组 / 全市场汇总）失败，都只往 `errors` 里记一条，
   其余照常返回；**单个非法代码只影响它自己**（见 `hithink.index_snapshot` 的逐只重试），
   界面只少那一项。概览是"扫一眼"的参考信息，绝不能因为它把界面或命令搞挂。
2. **TTL 缓存与界面时钟解耦**：概览页自己每 60 秒刷一次，但取数有 TTL
   （`market_overview_ttl`，默认 55 秒），到点才真的打接口 —— 接口有配额、也怕限流；
   55 秒这个值特意略小于 60 秒，避免"每分钟刷一次却总差一点没到点"的边界抖动。
   读缓存时结果里 `stale=True`，界面据此在 tooltip 里说明"这是几分钟前的数"。
3. **全市场汇总单独一档 + 自己更长的 TTL**：涨跌家数与北交所成交额要翻 6 页全市场快照
   （5571 只 / 每页 1000），所以即使 `market_breadth` 默认开着，它也有独立的 5 分钟缓存
   （`_BREADTH_TTL`），**不跟着每分钟的页面刷新跑** —— 否则配额和限流都吃不消。

数据口径（照抄已用真实接口验证过的结论，别自己另找端点）
--------------------------------------------------------
- 涨跌停家数：`special-data/limit-up|limit-down|limit-break-pool?size=1` → `pagination.total`；
- 指数点位与涨跌幅：`a-share-index/prices/snapshot?thscodes=...`（**必须传代码**，
  不支持全市场；一个非法代码会让整批 1002，所以客户端里逐只重试）；
  三组（宽基 / 情绪 / 板块）是**同一个端点、同一批请求**，只是配置里分成三组便于分别换口径；
- 沪/深成交额：直接取 `000001.SH`（上证指数）与 `399001.SZ`（深证成指）的 `turnover`
  —— 这两个数就是沪深两市的成交额（深证综指 `399106.SZ` 报的数与深证成指完全相同）；
- 北交所成交额与涨跌家数：只能靠全市场快照分页按 `.BJ` 后缀汇总（所以慢一档、默认关）。
"""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import Any

from laoa_trader.data import hithink as hx
from laoa_trader.log import get_logger

logger = get_logger(__name__)

# ── 端点（与 hithink.BASE_URL 拼起来用；常量放这里，界面/CLI 都不该自己拼路径）──
LIMIT_UP_PATH = "/a-share/special-data/limit-up-pool"
LIMIT_DOWN_PATH = "/a-share/special-data/limit-down-pool"
LIMIT_BREAK_PATH = "/a-share/special-data/limit-break-pool"
INDEX_SNAPSHOT_PATH = "/a-share-index/prices/snapshot"
MARKET_SNAPSHOT_PATH = "/a-share/prices/snapshot"

# ── 分组（卡片一行一组，组名在前）──
#: 每组配一个配置键、一个行内组名、一个结果字典里的键。
#: 顺序 = 卡片显示顺序；**某组配置为空时整行不显示**（不留一个空的"情绪："）。
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("market_indices", "宽基", "indices"),
    ("market_sentiment_indices", "情绪", "sentiment"),
    ("market_sector_indices", "板块", "sector"),
)

#: 概览页的总行数：前两行固定（摘要 / 涨跌家数），之后每组一行。
#: 界面按它建 QLabel，测试按它断言（下标即行含义，某组为空时那一行隐藏）。
LINE_COUNT = 2 + len(GROUPS)

#: 底部那行小字的固定前缀（数据来源与刷新节奏，界面与命令行口径一致）
FOOTER_PREFIX = "数据来源：同花顺金融数据服务"

#: 沪/深成交额的取数口径：这两个指数的 turnover 就是两市成交额
SH_TURNOVER_CODE = "000001.SH"
SZ_TURNOVER_CODE = "399001.SZ"

# ── 排版分隔符（照需求给的样例）──
#: 摘要行里两段之间（涨跌停家数 ｜ 沪深北成交额）
_BLOCK_SEP = " ｜ "
#: 同一组里各条目之间（样例用的全角竖线，比逗号更不容易和数据混在一起）
_ITEM_SEP = " ｜ "

# ── 取数客户端的节奏 ──
#: 为什么比下载用的客户端更"急"：概览是在界面线程里同步取的（几行只读数据），
#: 断网/服务端不响应时必须尽快认输，否则用户点一下按钮就要等半分钟。
MARKET_TIMEOUT = 8.0
MARKET_RETRIES = 1
MARKET_PACE = 0.05

# ── 全市场汇总（market_breadth = true 时才跑）──
#: 每页 1000 条：5571 只 → 6 页
BREADTH_PAGE_SIZE = 1000
#: 页间间隔（秒）——官方要求控制请求节奏，连刷会被限流
BREADTH_PAGE_PAUSE = 0.3
#: 全市场汇总自己的 TTL（秒）：它是 6 个请求，不该跟着 55 秒的概览 TTL 跑
_BREADTH_TTL = 300.0

# ── A 股习惯配色：涨=红、跌=绿、平=默认色（**不要**欧美的绿涨红跌）──
COLOR_UP = "#d32f2f"
COLOR_DOWN = "#2e7d32"

#: 缺数据时的占位符（界面与命令行统一用它，免得两处一个用 "-" 一个用 "N/A"）
DASH = "—"

#: 代码 → 中文名（**本地小表**：目录接口 `catalog/ths-index-list` 一个 tag 就返回几千行，
#: 而概览每 55 秒刷一次 —— 为几个名字付几千行请求不值当）。
#: 表里没有的代码显示代码本身；想改成自己的口径，在配置里写 `代码=名称` 即可。
#: 注释里括号内是目录接口里的**官方名**，冒号前是卡片上显示的名字（短一些更耐看）。
INDEX_NAMES: dict[str, str] = {
    # 宽基（默认那五个）
    "000001.SH": "上证",            # 上证指数
    "399001.SZ": "深成",            # 深证成指
    "399006.SZ": "创业板",          # 创业板指
    "000688.SH": "科创50",          # 科创50
    "000300.SH": "沪深300",         # 沪深300
    # 情绪（同花顺板块指数 .TI）
    "883404.TI": "同花顺情绪",      # 同花顺情绪指数
    "883958.TI": "昨日连板",        # 昨日连板
    "883994.TI": "昨日打首板",      # 昨日打首板表现
    "883418.TI": "微盘股",          # 微盘股
    # 板块（同花顺一级行业指数，目录 tag=industry）
    "881155.TI": "银行",            # 银行
    "881157.TI": "证券",            # 证券
    # 备选口径（同一个端点、实测可用；配到任意一组都能显示，名称这里都有）
    "883900.TI": "昨日涨停表现",
    "883409.TI": "近期强势",
    "883918.TI": "昨日炸板股",
    "883408.TI": "近期新高",
    "883911.TI": "创历史新高",
    "883423.TI": "沪深主板昨日涨停",
    "883424.TI": "创业科创板昨日涨停",
    "883422.TI": "北交所昨日涨停表现",
}


# ── 缓存 ──
# 为什么用模块级缓存而不是挂在窗口上：`--cli --market`、界面、将来的定时提醒
# 都该共用同一份"最近一次结果"，同一进程里重复取数没有意义。
_CACHE: dict[str, Any] = {"signature": None, "at": 0.0, "overview": None}
#: 全市场汇总单独缓存（TTL 更长，见 `_BREADTH_TTL`）
_BREADTH_CACHE: dict[str, Any] = {"signature": None, "at": 0.0, "value": None}


def clear_cache() -> None:
    """清空缓存（测试与"改完配置想立刻看效果"时用）。"""
    _CACHE.update({"signature": None, "at": 0.0, "overview": None})
    _BREADTH_CACHE.update({"signature": None, "at": 0.0, "value": None})


def _signature(cfg: Any) -> tuple:
    """影响取数结果的配置指纹：变了就不该复用缓存（否则改了指数列表看不出变化）。"""
    return (
        *(
            tuple(str(x) for x in (getattr(cfg, key, None) or []))
            for key, _, _ in GROUPS
        ),
        bool(getattr(cfg, "market_breadth", False)),
    )


def _ttl(cfg: Any) -> float:
    """概览缓存的 TTL（秒）；配置写坏时退回 55（`Config` 里已归一，这里是双保险）。"""
    try:
        value = float(getattr(cfg, "market_overview_ttl", 55) or 0)
    except (TypeError, ValueError):
        return 55.0
    return value if value > 0 else 55.0


def _why(exc: Exception) -> str:
    """异常 → 一句能照着排查的中文原因（用户看到的只有这一句，不能是裸 traceback）。"""
    if isinstance(exc, hx.HithinkAuthError):
        return f"未配置或无效的同花顺 Key（{exc}）"
    if isinstance(exc, hx.HithinkNotReadyError):
        return f"同花顺数据尚未就绪（{exc}）"
    if isinstance(exc, hx.HithinkRateLimitError):
        return f"同花顺限流（{exc}）"
    if isinstance(exc, hx.HithinkError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _note(errors: list[str], message: str) -> None:
    """记一条降级原因：既进结果（tooltip/CLI 显示），也写日志（事后能查）。"""
    errors.append(message)
    logger.warning(f"大盘概览降级：{message}")


def _make_client(cfg: Any) -> Any:
    """构造取数客户端。

    `api_key=... or None` 而不是直接传空串：空串在 `HithinkClient` 里表示
    "明确没有 Key"，而这里想保留"环境变量里配了 FUYAO_TOKEN/API_KEY"的临时调试用法
    （两种情况下"没有 Key"报的都是同一句话，不会让人猜）。
    """
    return hx.HithinkClient(
        api_key=getattr(cfg, "hithink_api_key", "") or None,
        timeout=MARKET_TIMEOUT,
        retries=MARKET_RETRIES,
        pace=MARKET_PACE,
    )


# ── 配置里的代码解析 ──


def parse_codes(specs: Any) -> list[tuple[str, str]]:
    """把配置里的代码列表解析成 `[(thscode, 显示名)]`。

    允许两种写法（用户手改 TOML 时很方便）：
        "000001.SH"                → 用本地映射表里的名字（"上证"）
        "000001.SH=我的上证"        → 用你自己写的名字
    认不出来的代码**不丢**：照样发给服务端，逐只重试时会失败并被记进 `failed`，
    这样用户能在概览上看到"这个代码有问题"，而不是"我配的东西怎么没了"。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if isinstance(specs, str):
        # 手写的配置对象可能直接塞了个字符串（走 load_config 时 `_as_list` 已经拆好了，
        # 这里防的是"直接构造 Config 的代码"，否则会把字符串按字符遍历，非常难查）
        specs = [item for item in specs.replace("，", ",").split(",") if item.strip()]
    for raw in specs or []:
        text = str(raw).strip()
        if not text:
            continue
        code, _, name = text.partition("=")
        code, name = code.strip(), name.strip()
        if not code:
            continue
        try:
            thscode = hx.to_thscode(code)
        except ValueError:
            thscode = code
        if thscode in seen:
            continue
        seen.add(thscode)
        out.append((thscode, name or INDEX_NAMES.get(thscode) or thscode))
    return out


# ── 各路取数（每一路都要能单独失败）──


def _fetch_limits(client: Any, errors: list[str]) -> dict:
    """涨停 / 跌停 / 炸板家数（3 个 size=1 的请求，每路独立降级）。"""
    limits: dict[str, Any] = {"up": None, "down": None, "break": None}
    for key, path, label in (
        ("up", LIMIT_UP_PATH, "涨停"),
        ("down", LIMIT_DOWN_PATH, "跌停"),
        ("break", LIMIT_BREAK_PATH, "炸板"),
    ):
        try:
            limits[key] = int(client.special_pool_total(path))
        except Exception as exc:  # noqa: BLE001 - 一路失败不能连累另外两路
            limits[key] = None
            _note(errors, f"{label}家数取不到：{_why(exc)}")
            if isinstance(exc, (hx.HithinkAuthError, hx.HithinkNotReadyError)):
                # Key 无效/数据未就绪：剩下两路必然一样报错，省掉两次无意义的请求
                _note(errors, f"{label}之后的池子数据跳过（同一原因）")
                break
    return limits


def _index_entries(
    rows: list[dict], specs: list[tuple[str, str]]
) -> list[dict]:
    """把快照行按配置顺序整理成展示用的条目；取不到的代码**跳过**（进 failed）。"""
    by_code = {str(row.get("thscode") or ""): row for row in rows}
    out: list[dict] = []
    for thscode, name in specs:
        row = by_code.get(thscode)
        if row is None:
            continue
        out.append(
            {
                "thscode": thscode,
                "name": name,
                "last": _to_float(row.get("last_price")),
                "change_pct": _to_float(row.get("price_change_ratio_pct")),
                "turnover": _to_float(row.get("turnover")),
            }
        )
    return out


def _to_float(value: Any) -> float | None:
    """宽松转 float；空/停牌/字段缺失一律 None（界面显示 `—`）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_breadth(client: Any, errors: list[str]) -> dict | None:
    """全市场快照分页汇总：沪/深/北的成交额、成交量与涨跌家数。

    Returns:
        `{"up","down","flat","total","exchanges": {"SH"|"SZ"|"BJ": {...}}}`；失败返回 None。
    """
    exchanges: dict[str, dict[str, Any]] = {}
    up = down = flat = 0
    offset = 0
    try:
        while True:
            data = client.request(
                MARKET_SNAPSHOT_PATH,
                {"limit": BREADTH_PAGE_SIZE, "offset": offset},
            )
            rows = list(data.get("item") or [])
            for row in rows:
                exchange = _exchange_of(str(row.get("thscode") or ""))
                if not exchange:
                    continue
                bucket = exchanges.setdefault(
                    exchange, {"count": 0, "turnover": 0.0, "volume": 0.0}
                )
                bucket["count"] += 1
                bucket["turnover"] += _to_float(row.get("turnover")) or 0.0
                bucket["volume"] += _to_float(row.get("volume")) or 0.0
                pct = _to_float(row.get("price_change_ratio_pct"))
                if pct is None:
                    continue            # 停牌/无涨跌幅：不计入涨跌家数
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
                else:
                    flat += 1
            offset += len(rows)
            total = int(_to_float(data.get("total")) or 0)
            if not rows or len(rows) < BREADTH_PAGE_SIZE or (total and offset >= total):
                break
            time.sleep(BREADTH_PAGE_PAUSE)   # 页间 ≥0.3 秒：并发硬刷会被限流
    except Exception as exc:  # noqa: BLE001 - 这一路失败只影响北交所那段
        _note(errors, f"全市场汇总取不到（北交所成交额与涨跌家数会缺）：{_why(exc)}")
        return None
    if not exchanges:
        _note(errors, "全市场汇总返回了空数据（北交所成交额与涨跌家数会缺）")
        return None
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "total": sum(int(b["count"]) for b in exchanges.values()),
        "exchanges": exchanges,
    }


def _exchange_of(thscode: str) -> str:
    """`600519.SH` → `SH`；认不出来返回空串（不计入汇总，也不抛异常）。"""
    _, _, suffix = thscode.partition(".")
    suffix = suffix.upper()
    return suffix if suffix in ("SH", "SZ", "BJ") else ""


def _configured_group_keys(cfg: Any) -> list[str]:
    """哪几组在配置里配了代码（**纯读配置、不发请求**）—— 决定 `lines()` 出不出这一行。

    为什么要单独算：没配 Key / 总开关关掉时压根没去取数，但"用户配了哪几组"是配置里
    现成的信息 —— 那种情况下照样显示 `宽基：—`，用户才知道自己配的东西在哪一行，
    而不是卡片上凭空少几行（那看起来像"功能坏了"）。
    """
    return [key for source, _, key in GROUPS if parse_codes(getattr(cfg, source, None))]


def _fetch_groups(
    cfg: Any, client: Any, errors: list[str]
) -> tuple[dict[str, list[dict]], list[str]]:
    """取三组指数（宽基 / 情绪 / 板块）—— **同一个端点、同一批请求**，取回后按组切分。

    为什么三组要挤在一个请求里：它们本来就是同一个快照端点，分批发只是多花请求数
    （这几路每 55 秒就可能跑一次）；切分只按代码归属，互不影响。

    Returns:
        ({结果键: 条目列表}, 取不到的代码)。
    """
    specs = {key: parse_codes(getattr(cfg, source, None)) for source, _, key in GROUPS}

    codes: list[str] = []
    for _, _, key in GROUPS:
        for thscode, _name in specs[key]:
            if thscode not in codes:
                codes.append(thscode)
    if not codes:
        return {key: [] for key in specs}, []

    try:
        result = client.index_snapshot(codes)
    except Exception as exc:  # noqa: BLE001 - 指数整批失败：涨跌停与成交额那行照常显示
        _note(errors, f"指数/情绪/板块取不到：{_why(exc)}")
        return {key: [] for key in specs}, []

    rows = list((result or {}).get("item") or [])
    failed = [str(code) for code in ((result or {}).get("failed") or [])]
    # 服务端"请求成功但这只没返回"时也要算取不到：否则用户看到自己配的指数凭空少了
    # 一个，只会以为配置没生效（比报错更难查）
    returned = {str(row.get("thscode") or "") for row in rows}
    for thscode in codes:
        if thscode not in returned and thscode not in failed:
            failed.append(thscode)
    if failed:
        # 只记日志/提示，**不影响其它条目**：界面只少那一项（用户以后很可能填错代码）
        _note(errors, "以下代码取不到（已跳过，其余照常显示）：" + "、".join(failed))
    return {key: _index_entries(rows, specs[key]) for key in specs}, failed


# ── 汇总 ──


def _skeleton(
    errors: list[str] | None = None, configured: list[str] | None = None
) -> dict:
    """空结果骨架（关闭开关/一路都没取到时也返回**同一个结构**，调用方不用判空）。"""
    data = {
        "as_of": "",
        "limits": {"up": None, "down": None, "break": None},
        "turnover": {"sh": None, "sz": None, "bj": None, "total": None},
        "breadth": None,
        "breadth_enabled": False,
        #: 三组指数各自一行；空列表 = 这一组没有数据（配置为空的组在 `lines()` 里整行不显示）
        "failed": [],
        "errors": list(errors or []),
        "stale": False,
    }
    for _, _, key in GROUPS:
        data[key] = []
    #: 哪几组在配置里存在（决定 `lines()` 要不要出这一行）
    data["configured_groups"] = list(configured or [])
    return data


def fetch_overview(cfg: Any, client: Any = None, *, force: bool = False) -> dict:
    """取一次大盘概览（带 TTL 缓存与逐路降级，**永不抛异常**）。

    Args:
        cfg: 配置对象（用到 `market_overview` / 三个组别列表 /
            `market_breadth` / `market_overview_ttl`）。
        client: 取数对象；缺省按配置构造真实 `HithinkClient`（测试注入假客户端）。
        force: 忽略缓存强制重取（界面按钮与测试用；`--cli --market` 也用它）。

    Returns:
        见 `_skeleton()` 的键；取不到的部分是 None/空列表，原因在 `errors` 里。
        用缓存时 `stale` 为 True。
    """
    configured = _configured_group_keys(cfg)
    if not bool(getattr(cfg, "market_overview", True)):
        # 总开关关掉 = **一个请求都不发**（用户可能就是想省配额）
        return _skeleton(["大盘概览已关闭（market_overview = false）"], configured)

    signature = _signature(cfg)
    if not force:
        cached = _cached_overview(signature, _ttl(cfg))
        if cached is not None:
            return cached

    errors: list[str] = []
    if client is None:
        try:
            client = _make_client(cfg)
        except Exception as exc:  # noqa: BLE001 - 没 Key 是最常见的情况，照常返回骨架
            logger.info(f"大盘概览无法取数：{_why(exc)}")
            return _remember(signature, _skeleton([_why(exc)], configured))

    overview = _skeleton(configured=configured)
    overview["as_of"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    overview["limits"] = _fetch_limits(client, errors)
    groups, failed = _fetch_groups(cfg, client, errors)
    for _, _, key in GROUPS:
        overview[key] = groups[key]
    overview["failed"] = failed

    # 沪/深成交额直接从宽基条目里取（口径见模块开头）；北交所只能靠全市场分桶
    turnover = overview["turnover"]
    for row in overview["indices"]:
        if row["thscode"] == SH_TURNOVER_CODE:
            turnover["sh"] = row["turnover"]
        elif row["thscode"] == SZ_TURNOVER_CODE:
            turnover["sz"] = row["turnover"]

    breadth_enabled = bool(getattr(cfg, "market_breadth", False))
    overview["breadth_enabled"] = breadth_enabled
    if breadth_enabled:
        breadth = _fetch_breadth_cached(cfg, client, signature, force, errors)
        overview["breadth"] = breadth
        if breadth:
            turnover["bj"] = (
                breadth["exchanges"].get("BJ", {}).get("turnover")
            )
    available = [v for v in (turnover["sh"], turnover["sz"], turnover["bj"]) if v]
    turnover["total"] = sum(available) if available else None

    overview["errors"] = errors
    return _remember(signature, overview)


def _cached_overview(signature: tuple, ttl: float) -> dict | None:
    """TTL 内且配置没变 → 返回缓存副本（`stale=True`）；否则 None。"""
    overview = _CACHE.get("overview")
    if overview is None or _CACHE.get("signature") != signature:
        return None
    age = time.monotonic() - float(_CACHE.get("at") or 0.0)
    if age >= ttl:
        return None
    fresh = copy.deepcopy(overview)
    fresh["stale"] = True
    fresh["age"] = int(age)          # tooltip 里可以说"这是 N 秒前的数"
    return fresh


def _remember(signature: tuple, overview: dict) -> dict:
    """写入缓存并返回结果本身。

    失败的结果**也缓存**：否则断网时界面每 5 秒重试一次，白白拖着界面等超时。
    用户改了配置或想立刻重来，走 `force=True`（按钮/`--cli --market`）。
    """
    _CACHE.update(
        {"signature": signature, "at": time.monotonic(), "overview": overview}
    )
    return overview


def _fetch_breadth_cached(
    cfg: Any, client: Any, signature: tuple, force: bool, errors: list[str]
) -> dict | None:
    """全市场汇总的**长 TTL** 缓存：6 个分页请求不该每 55 秒重打一遍。"""
    if not force:
        value = _BREADTH_CACHE.get("value")
        if value is not None and _BREADTH_CACHE.get("signature") == signature:
            if time.monotonic() - float(_BREADTH_CACHE.get("at") or 0.0) < _BREADTH_TTL:
                return value
    value = _fetch_breadth(client, errors)
    if value is not None:
        _BREADTH_CACHE.update(
            {"signature": signature, "at": time.monotonic(), "value": value}
        )
    return value


# ── 排版（界面与命令行共用这一份）──


def _count_text(value: Any) -> str:
    """家数：拿不到就是 `—`（绝不显示 None 或者 0 —— 0 家涨停是一种真实状态）。"""
    if value is None:
        return DASH
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return DASH


def _amount_text(value: Any) -> str:
    """成交额（元）→ `7792亿`；拿不到就是 `—`。

    按"亿"**四舍五入到整数**：这几段是给人扫一眼的量级，多一位小数只会更难读。
    （样例里的 7792亿/8499亿/140亿 就是这个规则的产物：7792.8→7793、
    8498.9→8499、140.1→140 —— 用户那次手写的 7792亿 来自更早一次快照。）
    """
    number = _to_float(value)
    if number is None:
        return DASH
    return f"{number / 1e8:.0f}亿"


def _price_text(value: Any) -> str:
    number = _to_float(value)
    return DASH if number is None else f"{number:.2f}"


def _pct_text(value: Any) -> str:
    """涨跌幅：**带正负号**（`+3.21%` / `-0.07%`），A 股看盘的习惯写法。"""
    number = _to_float(value)
    return DASH if number is None else f"{number:+.2f}%"


def _entry_text(item: dict) -> str:
    """一个指数/情绪条目 → `上证 3885.33 -0.07%`（缺哪段就把哪段显示成 `—`）。"""
    name = str(item.get("name") or item.get("thscode") or DASH)
    if item.get("last") is None:
        return f"{name} {DASH}"
    if item.get("change_pct") is None:
        return f"{name} {_price_text(item.get('last'))} {DASH}"
    return f"{name} {_price_text(item.get('last'))} {_pct_text(item.get('change_pct'))}"


def lines(overview: dict | None) -> list[str]:
    """概览页文本（界面页面与 `--cli --market` 用同一份）。

    下标固定对应：
        [0] 摘要：`涨停 N · 跌停 N · 炸板 N ｜ 沪 N亿 · 深 N亿 · 北 N亿`
        [1] 涨跌家数：`上涨 N · 下跌 N · 平盘 N`
        [2] 宽基  [3] 情绪  [4] 板块   ← 顺序就是 `GROUPS` 的顺序

    **配置为空的那一组返回空串**（界面据此把那行整行隐藏，命令行跳过它）——
    不留一个空的"情绪："吊在那里。拼不出来的段位一律 `—`：宁可让用户看到"这里没数"，
    也不显示 0 或空白（0 家涨停和"没取到"是完全不同的两件事）。
    `market_breadth` 关掉时北交所与涨跌家数是 `—`（这两项只在打开时才取）。
    """
    data = overview or {}
    limits = data.get("limits") or {}
    turnover = data.get("turnover") or {}
    breadth = data.get("breadth") or {}

    summary = (
        f"涨停 {_count_text(limits.get('up'))} · "
        f"跌停 {_count_text(limits.get('down'))} · "
        f"炸板 {_count_text(limits.get('break'))}"
        + _BLOCK_SEP
        + f"沪 {_amount_text(turnover.get('sh'))} · "
        f"深 {_amount_text(turnover.get('sz'))} · "
        f"北 {_amount_text(turnover.get('bj'))}"
    )
    counts = (
        f"上涨 {_count_text(breadth.get('up'))} · "
        f"下跌 {_count_text(breadth.get('down'))} · "
        f"平盘 {_count_text(breadth.get('flat'))}"
    )

    configured = set(data.get("configured_groups") or [])
    out = [summary, counts]
    for _, label, key in GROUPS:
        if key not in configured:
            out.append("")            # 这一组没配 → 整行不显示
            continue
        items = data.get(key) or []
        out.append(
            f"{label}："
            + (_ITEM_SEP.join(_entry_text(item) for item in items) or DASH)
        )
    return out


def footer_text(overview: dict | None) -> str:
    """页面底部那行小字：数据来源 + 取数时间 + 刷新节奏（界面与命令行共用一份口径）。

    为什么把"涨跌家数与北交所成交额每 5 分钟更新"也写在这里：这两项（全市场快照）
    明显比指数慢一档，不写清楚会被当成"数据没刷新/坏了"。
    """
    data = overview or {}
    as_of = str(data.get("as_of") or "") or DASH
    tail = "每分钟自动刷新"
    if data.get("breadth_enabled"):
        tail += "；涨跌家数与北交所成交额每 5 分钟更新"
    return f"{FOOTER_PREFIX} · 更新于 {as_of}（{tail}）"


def _line_color(items: list[dict]) -> str:
    """一行里所有涨跌幅**同向**时才上色（涨红跌绿），否则用默认色。

    为什么按行而不是按数值上色：用户明确要求用 `QLabel.setStyleSheet("color:...")`，
    而一行是一个 QLabel、里面可能同时有涨有跌（真实行情里很常见）——
    那种情况下强行取一个颜色反而误导人，保持默认色最诚实。
    """
    values = [item.get("change_pct") for item in items]
    values = [v for v in values if v is not None]
    if not values:
        return ""
    if all(v > 0 for v in values):
        return COLOR_UP
    if all(v < 0 for v in values):
        return COLOR_DOWN
    return ""


def line_colors(overview: dict | None) -> list[str]:
    """与 `lines()` 一一对应的每行颜色（空串 = 用默认色）。

    前两行（摘要 / 涨跌家数）没有涨跌幅，恒为默认色；之后每组按组内涨跌幅是否同向决定。
    """
    data = overview or {}
    return ["", ""] + [_line_color(data.get(key) or []) for _, _, key in GROUPS]


def has_data(overview: dict | None) -> bool:
    """概览里到底有没有取到东西（`--cli --market` 用它决定退出码）。"""
    data = overview or {}
    limits = data.get("limits") or {}
    turnover = data.get("turnover") or {}
    if any(limits.get(key) is not None for key in ("up", "down", "break")):
        return True
    if any(turnover.get(key) for key in ("sh", "sz", "bj")):
        return True
    for _, _, key in GROUPS:
        if any(item.get("last") is not None for item in (data.get(key) or [])):
            return True
    return False


def summary_text(overview: dict | None) -> str:
    """一句话说明取数状态（tooltip / CLI 收尾用）：取数时间、是否来自缓存、失败原因。"""
    data = overview or {}
    bits: list[str] = []
    if data.get("as_of"):
        bits.append(
            ("（缓存）" if data.get("stale") else "") + f"取数时间 {data['as_of']}"
        )
    errors = [str(e) for e in (data.get("errors") or [])]
    if errors:
        bits.append("；".join(errors))
    return "｜".join(bits)
