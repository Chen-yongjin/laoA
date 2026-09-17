"""免 Key 公开行情源（腾讯为主、新浪兜底、东财补齐）——分发版的**主源**。

为什么需要它
------------
单机版原来是"同花顺 fuyao 为主"，而它**必须自己申请 API Key**（去 fuyao.aicubes.cn 注册）。
做分发版时这是最大的门槛：别人拿到 exe，第一步就卡在"要先去申请一个 Key"。
所以分发版把**免 Key 的公开行情源**做成主源，同花顺降级为**备用/增强源**
（有 Key 的用户自动用它拿 10 年历史、竞价、估值、涨停池）。

本模块只做"取数 + 单位归一"，不管优先级与降级顺序 —— 那部分在 `sources.py`
（`data_sources` 的顺序即优先级），这样"谁是主源"是配置问题，不是代码问题。

数据来源与实测（2026-09-17，本机真实探测）
------------------------------------------
* **腾讯** `https://qt.gtimg.cn/q=sh600519,sz000001,...`（GBK，`~` 分隔 **88 个字段**）
  - 一次可带 100 只；**实测 600 只 / 6 批 / 4.2 秒 → 全市场 5562 只约 0.7 分钟**（串行）
  - 字段下标（逐个核对过，见 `_parse_tencent`）：
    `[1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]成交量(手) [30]时间 [32]涨跌幅%`
    `[33]最高 [34]最低 [35]现价/量/成交额(元) [38]换手率% [39]市盈率 [43]振幅`
    `[44]流通市值(亿) [45]总市值(亿) [46]市净率 [47]涨停价 [48]跌停价 [49]量比 [51]均价`
  - ⚠️ 腾讯是**免 Key 但非授权**的公开接口：随时可能改字段或限流。所以本模块
    **每一项都可能取不到**（取不到就留 None，绝不编数），并且 `sources.py` 还挂着
    新浪/东财两道兜底。
* **新浪** `https://hq.sinajs.cn/list=sh600519`（GBK；**必须带 `Referer: https://finance.sina.com.cn/`**）
  - 字段：`名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),...,日期,时间`
  - ⚠️ **单位与腾讯不同**：新浪的成交量是**股**、成交额是**元**；腾讯是**手**与**万元**。
    这正是本模块存在的意义之一 —— 单位在这里一次归一，上层永远不会拿到两种口径。
* **东方财富** `push2` 的 clist（原 `eastmoney.py`）：实测从本机**已被限流**
  （连续 `RemoteDisconnected`），所以只当第三道兜底，不当主路。

单位约定（与 `sources.QUOTE_FIELDS` 一致）
-----------------------------------------
    volume   = **股**（腾讯 ×100）
    turnover = **元**（腾讯优先用 `[35]` 里的精确值）
    last_price / prev_close / open / high / low = **元**
    另附（不在统一口径里，供选股条件与界面以后用）：
    `turnover_rate`(%)、`circ_mktcap`(亿)、`total_mktcap`(亿)、`pe`、`pb`、
    `volume_ratio`(量比)、`avg_price`(均价)、`limit_up`/`limit_down`(涨跌停价)、`at`(数据时刻)
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Sequence

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 腾讯一次能带的代码数（实测 100 只/批稳定；再大响应会很长）
TENCENT_BATCH = 100
#: 新浪一次能带的代码数
SINA_BATCH = 100
#: 批与批之间的最小间隔（秒）。公开接口上"快"没有意义，"被封"才是代价。
BATCH_PACE = 0.05

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIMEOUT = 15.0

#: 注入式 HTTP 层（测试用固定响应；生产用 urllib）—— 契约：`opener(url, headers, timeout) -> bytes`
Opener = Callable[[str, dict, float], bytes]


def _urllib_get(url: str, headers: dict, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 主机
        return response.read()


def market_prefix(symbol: str) -> str:
    """裸 6 位代码 → 腾讯/新浪的市场前缀：`sh` / `sz` / `bj`。

    为什么按前缀猜：A 股的代码段是交易所分配的（6/9=沪、0/3=深、4/8=北交所），
    我们本地库里存的也是裸 6 位（见 `sources.to_local_symbol` 的约定）。
    """
    code = str(symbol or "").strip()
    if not code:
        return ""
    head = code[0]
    if head in "69":
        return "sh"
    if head in "03":
        return "sz"
    if head in "48":
        return "bj"
    if head == "5":       # 沪市基金/ETF
        return "sh"
    if head in "12":      # 深市基金/ETF
        return "sz"
    return "sz"


def _num(value: Any) -> float | None:
    """字符串 → 数字；空白/`-`/认不出 → None（"没有这个数"，不是 0）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    return out if out == out else None        # 过滤 nan


def _parse_tencent(payload: str) -> dict[str, dict]:
    """腾讯批量响应 → `{symbol: 归一化字典}`。

    响应形如：`v_sh600519="1~贵州茅台~600519~1266.98~1258.00~...";`（GBK 解码后）。
    字段靠**下标**取（接口不给字段名），所以每个下标都在模块 docstring 里对着实测值写清了；
    越界一律当"取不到"，不让任何一只票把整批拖挂。
    """
    out: dict[str, dict] = {}
    for line in payload.split(";"):
        line = line.strip()
        if not line.startswith("v_") or '"' not in line:
            continue
        body = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = body.split("~")
        # 只要求"核心几列在"（名称/代码/现价/昨收/今开/量）；其余靠 `at()` 取，
        # 取不到就是 None。**不要**写成"必须满 88/52 列才认" —— 上游哪天裁剪字段，
        # 那种写法会把整批数据全丢，而这恰恰是免 Key 主源最不能出的故障。
        if len(parts) < 6:
            continue

        def at(index: int) -> Any:
            return parts[index] if index < len(parts) else None

        symbol = str(at(2) or "").strip()
        if not symbol:
            continue
        volume_hand = _num(at(6))
        # [35] 是"现价/成交量(手)/成交额(元)"，这里的成交额比 [37]（万元）精确
        turnover_yuan = None
        chunk = str(at(35) or "").split("/")
        if len(chunk) >= 3:
            turnover_yuan = _num(chunk[2])
        if turnover_yuan is None:
            amount_wan = _num(at(37))
            turnover_yuan = amount_wan * 1e4 if amount_wan is not None else None
        out[symbol] = {
            "symbol": symbol,
            "name": str(at(1) or "").strip() or None,
            "last_price": _num(at(3)),
            "prev_close": _num(at(4)),
            "open": _num(at(5)),
            "high": _num(at(33)),
            "low": _num(at(34)),
            "volume": volume_hand * 100 if volume_hand is not None else None,
            "turnover": turnover_yuan,
            "pct": _num(at(32)),
            "turnover_rate": _num(at(38)),
            "pe": _num(at(39)),
            "amplitude": _num(at(43)),
            "circ_mktcap": _num(at(44)),          # 亿元
            "total_mktcap": _num(at(45)),         # 亿元
            "pb": _num(at(46)),
            "limit_up": _num(at(47)),
            "limit_down": _num(at(48)),
            "volume_ratio": _num(at(49)),
            "avg_price": _num(at(51)),
            "at": str(at(30) or "").strip() or None,
            "provider": "tencent",
        }
    return out


def _parse_sina(payload: str) -> dict[str, dict]:
    """新浪 `hq.sinajs.cn` 响应 → 归一化字典（**单位自己换**：股/元）。

    `var hq_str_sh600519="贵州茅台,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),...";`
    """
    out: dict[str, dict] = {}
    for line in payload.split(";"):
        line = line.strip()
        if not line.startswith("var hq_str_") or '"' not in line:
            continue
        symbol = line[len("var hq_str_"):].split("=", 1)[0].strip()[2:]      # 去掉 sh/sz/bj
        body = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = body.split(",")
        if len(parts) < 32 or not parts[0]:
            continue
        price = _num(parts[3])
        prev = _num(parts[2])
        pct = None
        if price is not None and prev:
            pct = (price / prev - 1) * 100
        out[symbol] = {
            "symbol": symbol,
            "name": parts[0].strip() or None,
            "last_price": price,
            "prev_close": prev,
            "open": _num(parts[1]),
            "high": _num(parts[4]),
            "low": _num(parts[5]),
            "volume": _num(parts[8]),            # 新浪本来就是股
            "turnover": _num(parts[9]),          # 新浪本来就是元
            "pct": pct,
            "turnover_rate": None, "pe": None, "amplitude": None,
            "circ_mktcap": None, "total_mktcap": None, "pb": None,
            "limit_up": None, "limit_down": None, "volume_ratio": None,
            "avg_price": None,
            "at": f"{parts[30]} {parts[31]}".strip() if len(parts) > 31 else None,
            "provider": "sina",
        }
    return out


def _chunks(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def snapshot(
    symbols: Sequence[str] | None = None,
    *,
    opener: Opener | None = None,
    timeout: float = TIMEOUT,
    pace: float = BATCH_PACE,
) -> dict[str, dict]:
    """取一轮快照 → `{symbol: 归一化字典}`；**任何失败都只表现为"缺数据"**。

    Args:
        symbols: 要取的裸 6 位代码；`None` = 全市场（腾讯那边就是分页批量，
            实测全市场约 0.7 分钟，别在界面主线程里调）。
        opener: 注入的 HTTP 层（测试用）。
        pace: 批间间隔（秒）——对公开接口要客气一点。

    Returns:
        拿到的那些票；**拿不到的就不在字典里**（上层据此退回本地收盘价并标注）。
    """
    fetch = opener or _urllib_get
    wanted = list(dict.fromkeys(str(s).strip() for s in (symbols or []) if str(s).strip()))
    if symbols is not None and not wanted:
        return {}

    result: dict[str, dict] = {}
    if symbols is None:
        # 全市场：腾讯那边没有"给我全部"的开关，只能按代码表分批。调用方给 symbols
        # 更省事，所以这里明确要求调用方给（避免偷偷去查代码表、再多一次依赖）。
        logger.debug("公开源快照：未给代码表，返回空（全市场请由调用方提供 symbols）")
        return {}

    for batch in _chunks(wanted, TENCENT_BATCH):
        query = ",".join(f"{market_prefix(code)}{code}" for code in batch)
        url = f"https://qt.gtimg.cn/q={query}"
        try:
            raw = fetch(url, {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}, timeout)
            result.update(_parse_tencent(raw.decode("gbk", errors="replace")))
        except Exception as exc:  # noqa: BLE001 - 一批失败不该影响其它批
            logger.info(f"腾讯快照这一批失败（{len(batch)} 只）：{exc}")
        if pace:
            time.sleep(pace)

    # 缺的那些用新浪补一次（腾讯偶尔对个别代码不返回；新浪对沪深北都认）
    missing = [code for code in wanted if code not in result]
    for batch in _chunks(missing, SINA_BATCH):
        query = ",".join(f"{market_prefix(code)}{code}" for code in batch)
        url = f"https://hq.sinajs.cn/list={query}"
        try:
            raw = fetch(url, {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
                        timeout)
            result.update(_parse_sina(raw.decode("gbk", errors="replace")))
        except Exception as exc:  # noqa: BLE001 - 兜底失败就认了
            logger.info(f"新浪兜底这一批失败（{len(batch)} 只）：{exc}")
        if pace:
            time.sleep(pace)
    return result


def daily(symbol: str, start: str, end: str, *, adjust: str = "hfq",
          opener: Opener | None = None, timeout: float = TIMEOUT) -> list[dict]:
    """单只历史日K（腾讯 `fqkline`，免 Key）→ `[{date, open, high, low, close, volume, turnover}]`。

    ⚠️ 这是"逐只"接口：全市场要 5000+ 次请求，**只适合补少量票或按需拉长历史**；
    全市场历史走"包内自带的 6 个月 pack + 每日批量快照增量"（见 README 的免 Key 分发说明）。

    单位：腾讯返回的成交量是**手**、成交额是**万元** → 这里统一成**股**与**元**。
    """
    prefix = market_prefix(symbol)
    if not prefix:
        return []
    mode = {"hfq": "hfq", "qfq": "qfq", "none": ""}.get(str(adjust).lower(), "hfq")
    query = f"{prefix}{symbol},day,{start},{end},320,{mode}"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           + urllib.parse.urlencode({"param": query, "_var": "kline_day"}))
    fetch = opener or _urllib_get
    try:
        raw = fetch(url, {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}, timeout)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"腾讯日K失败 {symbol}：{exc}")
        return []
    text = raw.decode("utf-8", errors="replace")
    if "=" in text[:40]:            # `kline_day={...}`
        text = text.split("=", 1)[1]
    try:
        import json
        data = json.loads(text)
    except Exception:               # noqa: BLE001
        logger.info(f"腾讯日K解析失败 {symbol}")
        return []
    node = ((data.get("data") or {}).get(f"{prefix}{symbol}") or {})
    rows = node.get(f"{mode}day") or node.get("day") or []
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, list) or len(item) < 6:
            continue
        volume_hand = _num(item[5])
        turnover_wan = _num(item[6]) if len(item) > 6 else None
        out.append({
            "date": str(item[0]),
            "open": _num(item[1]),
            "close": _num(item[2]),
            "high": _num(item[3]),
            "low": _num(item[4]),
            "volume": volume_hand * 100 if volume_hand is not None else None,
            "turnover": turnover_wan * 1e4 if turnover_wan is not None else None,
        })
    return out
