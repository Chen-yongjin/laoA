"""板块行情（行业板块排行 + 主力净额）——免 Key，走**腾讯**排行接口。

为什么是腾讯，不是东方财富
--------------------------
2026-09-17 实测：东财 `push2.eastmoney.com/api/qt/clist/get`（**板块排行要用的那个端点**）
从本机直接被掐连接（`RemoteDisconnected`，curl 与 urllib 都一样）。同一时刻同一域名下的
`ulist.np/get`（批量个股快照）**侥幸还能用**（`eastmoney.py` 那一路就是靠它）——
但这恰恰说明它现在是"时通时不通"：把**整页板块行情**挂在这种端点上，
用户看到的就是"板块页一片空白，过一会儿又好了"。
腾讯的 `proxy.finance.qq.com` 排行接口同一天**免 Key、稳定可用**
（实测响应见 `tests/fixtures/sectors/rank_hy.json`，原样落盘，解析就是照着它写的）。
所以分发版这一路走腾讯 —— 与 `public_quotes.py` 是同一家（同一套容错口径，见下）。

接口与实测（2026-09-17，本机真实联网）
--------------------------------------
`GET https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank
     ?board_type=hy&sort_type=price&direct=down&offset=0&count=60`

* `board_type=hy` = 行业板块（腾讯自己的一级行业，**实测 `count=60` 只返回 31 个**：
  count 是"最多要几个"，不是"一定有这么多"）；
* 返回 `{"code":0,"data":{"rank_list":[{...}, ...]}}`；`code != 0` 或没有
  `rank_list` 一律当"没取到"（返回 `[]`），**不猜、不补**；
* **字段全是字符串**（`"0.29"` 不是 `0.29`），所以每个数都要过一遍数字解析；
* 一行实测样例（食品饮料，原文一字不改）：

      name=食品饮料  zdf=0.29（涨幅%）  hsl=1.34（换手率%）  lb=0.86（量比）
      zljlr=-58031.01（**主力净额，万元**）  zllr=541459.04  zllc=599490.04（流入/流出,万元）
      ltsz=35357.30（流通市值,亿）  zsz=36419.54（总市值,亿）
      zgb=62/122（涨/跌家数）  zdf_d5=-1.72  zdf_d20=-3.24
      lzg={code:sh601579, name:会稽山, zdf:5.37, zxj:29.85}（领涨股）

单位（**这个模块最容易错的一处**）
----------------------------------
腾讯给的主力净额是**万元**，本模块出口的 `main_net` 一律是**元**：

    -58031.01 万元 × 1e4 = -5.803101e8 元 ≈ **-5.80 亿**

（同时刻的"食品饮料 主力净流出 5.80 亿"就是它；少乘这 1e4 的话页面上会显示成
"-5.80 万"，量级差 10000 倍，而且**看起来还是个像样的数** —— 这正是最危险的一类错，
所以 `tests/test_sectors.py` 把这条单独钉住。）
其余：`pct`/`turnover_rate`/`pct_5d`/`pct_20d` 是**百分数原值**（0.29 = 0.29%），
`circ_mktcap`/`total_mktcap` 已经是**亿**（腾讯原值，不动）。

容错与缓存
----------
* **绝不抛异常、绝不编数**：网络失败/字段缺失/类型不对 → 返回 `[]` 或该项 `None`；
* 一次请求 60 行（实测 31 行）在页面上会被**多处**用到（板块表、涨幅榜、跌幅榜、
  概览页那一行），所以加 `CACHE_TTL` 秒的短缓存 —— 缓存省掉的不是"几毫秒"，
  而是**同一秒里对公开接口的重复请求**（公开接口上"少打一次"比"快一点"重要得多）。
  失败**不进缓存**（否则一次抖动会让板块页白 60 秒）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

# HTTP 层与数字解析**复用公开源那一套**：两路都是腾讯的公开接口，容错口径必须同源
# （`_num` 把空串/`-`/nan 都当"没有这个数"）—— 各写一份迟早会出现"两个模块两个口径"。
from laoa_trader.data.public_quotes import Opener, _num, _urllib_get
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 腾讯行业板块排行（`board_type=hy`）。为什么不用 `sort_type=zdf` 之类的其它排序：
#: 归一化之后**排序交给界面**（见模块头），这里只要一份尽可能全的行业清单，
#: `sort_type=price` 是实测可用的那个。
SECTOR_RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"

#: 默认要几个板块。实测（2026-09-17）腾讯一级行业只有 **31 个**，所以 60 只是"够用"，
#: 真要更多也不会被截断 —— 多要几个不影响"一行一个板块"的语义。
DEFAULT_COUNT = 60

#: 默认超时（秒）。板块排行实测 <1 秒，给 15 秒是给"网络抖动"留余量。
DEFAULT_TIMEOUT = 15.0

#: 短缓存 TTL（秒）。取 60 秒的理由：页面节奏是"每分钟刷新一次"（见
#: `ui/quotes.py` 的刷新间隔），而板块排行在一次刷新里会被多处调用
#: （板块表 + 涨幅/跌幅榜 + 概览），60 秒正好覆盖"一轮刷新"，
#: 又不会让用户看到明显过期的行情。
CACHE_TTL = 60.0

#: 出口契约的字段（**顺序即文档顺序**，界面按这些名字取值）。
#: 为什么不把 `code`（`pt01801120`）之类也带出来：界面只按名字识别板块，
#: 多一个键就多一处"两边名字不一样"的可能；真要板块代码，等它进了真实需求再加。
SECTOR_FIELDS: tuple[str, ...] = (
    "name", "pct", "main_net", "turnover_rate", "volume_ratio",
    "circ_mktcap", "total_mktcap", "up_count", "down_count",
    "leader_name", "leader_symbol", "pct_5d", "pct_20d",
)

#: 腾讯给的主力净额单位：**万元**。换成元要 ×1e4（实测 -58031.01 万元 = -5.80 亿）。
WAN_TO_YUAN = 1e4

#: 缓存：`count -> (写入时刻, 归一化后的行列表)`。
#: 为什么按 `count` 分键而不是只存一份：调用方可能一次要 30 个、一次要 60 个，
#: 用同一份缓存会让"要 60 个"的调用拿到 30 行、还看不出是缓存干的。
_cache: dict[int, tuple[float, list[dict]]] = {}

#: 时钟（测试可注入：验证 TTL 到期不需要真的 `sleep(60)`）。
_clock: Callable[[], float] = time.monotonic


def _int_or_none(value: Any) -> int | None:
    """`"62"` / `62` → 62；认不出 → None（**不填 0**：0 家上涨是真事，取不到是另一回事）。"""
    number = _num(value)
    return int(number) if number is not None else None


def _bare_symbol(value: Any) -> str | None:
    """`"sh601579"` / `"600519.SH"` → 裸 6 位代码（`"601579"`）；认不出 → None。

    为什么要剥前缀：项目里**本地代码一律是裸 6 位**（见 `sources.to_local_symbol`），
    领涨股要能直接拿去与本地库/行情源对上；留着 `sh601579` 就会出现
    "点进去查不到这只票"。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) == 6 else None


def _counts(value: Any) -> tuple[int | None, int | None]:
    """`"62/122"` → `(62, 122)`（涨家数 / 跌家数）；格式不对 → `(None, None)`。"""
    head, _, tail = str(value or "").partition("/")
    if not tail:
        return None, None
    return _int_or_none(head), _int_or_none(tail)


def _leader(value: Any) -> tuple[str | None, str | None]:
    """`lzg` 那一格 → `(领涨股名, 裸 6 位代码)`；缺了就给两个 None。"""
    if not isinstance(value, dict):
        return None, None
    name = str(value.get("name") or "").strip() or None
    return name, _bare_symbol(value.get("code"))


def parse_rank(payload: Any) -> list[dict]:
    """腾讯排行响应 → **一行一个板块**的归一化列表。

    为什么把它单独拿出来（而不是塞在 `fetch_sector_rank` 里）：解析是这一路
    **唯一会算错单位**的地方，单独一个纯函数才能拿固定响应反复钉（测试**不联网**）。

    Args:
        payload: 响应体（`bytes`/`str`）或已经解析好的 `dict`（测试直接喂 dict 也行）。

    Returns:
        归一化行（字段见 `SECTOR_FIELDS`）；`code != 0`、没有 `rank_list`、
        响应不是 JSON、行不是对象 —— 一律返回 `[]` 或跳过那一行，**绝不抛异常**。
        **只保留 `name` 非空的行**：没有名字的板块在界面上无法识别，留着只会变成空行。
    """
    data: Any = payload
    text: str | None = None
    if isinstance(payload, (bytes, bytearray)):
        # GBK 不在这里猜：腾讯这个排行接口实测回的是 UTF-8（`errors="replace"` 兜一层，
        # 解码失败也只是个别汉字变问号，不会把整份数据丢掉）
        text = bytes(payload).decode("utf-8", "replace")
    elif isinstance(payload, str):
        text = payload
    if text is not None:
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - 上游偶尔回 HTML/空体，不能让它冒泡
            logger.info(f"板块排行响应不是 JSON（{len(text)} 字符）：{exc}")
            return []
    if not isinstance(data, dict):
        logger.info(f"板块排行响应不是对象：{type(data).__name__}")
        return []
    if data.get("code") not in (0, "0", None):
        # `code != 0` 是服务端明确说"这次没数据"（参数变了/被挡了），
        # 与"网络失败"一样处理：返回空，让界面显示 `—`，不要拿上次的数糊上去。
        logger.info(f"板块排行返回 code={data.get('code')!r}（当作没取到）")
        return []
    node = data.get("data")
    rows = node.get("rank_list") if isinstance(node, dict) else None
    if not isinstance(rows, list):
        logger.info("板块排行响应里没有 rank_list（当作没取到）")
        return []

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        up, down = _counts(row.get("zgb"))
        leader_name, leader_symbol = _leader(row.get("lzg"))
        main_net_wan = _num(row.get("zljlr"))
        out.append({
            "name": name,
            # 涨幅/换手率：腾讯给的就是百分数原值（0.29 = 0.29%），不再 ×100
            "pct": _num(row.get("zdf")),
            # **万元 → 元**（实测 -58031.01 万元 = -5.803101e8 元 ≈ -5.80 亿）
            "main_net": main_net_wan * WAN_TO_YUAN if main_net_wan is not None else None,
            "turnover_rate": _num(row.get("hsl")),
            "volume_ratio": _num(row.get("lb")),
            "circ_mktcap": _num(row.get("ltsz")),        # 已是亿
            "total_mktcap": _num(row.get("zsz")),        # 已是亿
            "up_count": up,
            "down_count": down,
            "leader_name": leader_name,
            "leader_symbol": leader_symbol,
            "pct_5d": _num(row.get("zdf_d5")),
            "pct_20d": _num(row.get("zdf_d20")),
        })
    return out


def clear_cache() -> None:
    """清空缓存（给测试与"用户手动刷新"用）。

    为什么需要它：模块级缓存跨测试用例是**共享状态**，上一个用例灌进去的数据会让
    下一个用例"看起来没发请求"，那种测试是假绿的。
    """
    _cache.clear()


def fetch_sector_rank(
    *,
    count: int = DEFAULT_COUNT,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """取腾讯行业板块排行 → 归一化列表（免 Key；**任何失败都只表现为"空"**）。

    Args:
        count: 最多要几个板块（腾讯那里 `count` 是上限，实测一级行业只有 31 个）。
            `< 1` 直接返回 `[]`（一次请求都不发）。
        opener: 注入的 HTTP 层（测试用固定响应，**绝不联网**）；契约与
            `public_quotes.Opener` 一致：`opener(url, headers, timeout) -> bytes`。
        timeout: 单次请求超时（秒）。

    Returns:
        每行一个板块，字段见 `SECTOR_FIELDS`；`pct`/`turnover_rate`/`pct_5d`/`pct_20d`
        是**百分数**，`main_net` 是**元**（腾讯原文是万元，这里已 ×1e4），
        `circ_mktcap`/`total_mktcap` 是**亿**。
        取不到时返回 `[]` —— **不抛异常、不编数、不返回上一轮的数**（缓存只缓存成功结果）。
    """
    try:
        wanted = int(count)
    except (TypeError, ValueError):
        logger.info(f"板块排行的 count 不是整数（{count!r}），当作没取到")
        return []
    if wanted < 1:
        return []

    cached = _cache.get(wanted)
    if cached is not None:
        stamp, rows = cached
        if _clock() - stamp <= CACHE_TTL:
            # 返回**每一行的浅拷贝**：调用方（界面）常常就地加一列/排序，
            # 直接给缓存里的那个 dict 会被改坏，下一个调用方拿到的是被改过的数据。
            return [dict(row) for row in rows]
        _cache.pop(wanted, None)        # 过期就清掉，免得留着占内存

    url = (f"{SECTOR_RANK_URL}?board_type=hy&sort_type=price&direct=down"
           f"&offset=0&count={wanted}")
    fetch = opener or _urllib_get
    try:
        raw = fetch(url, {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": "https://gu.qq.com/",
        }, timeout)
    except Exception as exc:  # noqa: BLE001 - 公开接口失败是常态，界面据此显示 `—`
        logger.info(f"板块排行请求失败（当作没取到）：{exc}")
        return []
    rows = parse_rank(raw)
    if not rows:
        # **失败不进缓存**：一次抖动不该让板块页白 60 秒（下一拍要能立刻重试）
        return []
    _cache[wanted] = (_clock(), rows)
    return [dict(row) for row in rows]


__all__ = [
    "CACHE_TTL",
    "DEFAULT_COUNT",
    "DEFAULT_TIMEOUT",
    "SECTOR_FIELDS",
    "SECTOR_RANK_URL",
    "WAN_TO_YUAN",
    "clear_cache",
    "fetch_sector_rank",
    "parse_rank",
]
