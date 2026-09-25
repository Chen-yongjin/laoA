"""东方财富公开行情接口客户端（**免 Key** 的第二数据源）。

为什么有这个东西
----------------
`docs/开发文档.md`（"辅助来源"）在 2026-09-16 拍板：
数据来源默认同花顺，**用户可以自己再加别的来源、用自己的 Key**。
对一个**没填同花顺 Key** 的用户来说，两张表（股票池 / 持仓监控）的「现价 / 涨幅」
不该是一片 `—` —— 东方财富这几个公开端点**不要 Key**，正好补上这个缺口。

实测记录（2026-09-16，本机真实联网；原始响应留在交付报告里）
-----------------------------------------------------------
1. **全市场快照** `GET push2.eastmoney.com/api/qt/clist/get`
   （`fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23`，`fltt=2`）→ `data.total = 5559`；
   实测样例 `{'f2': 40.18, 'f3': 45.58, 'f12': '688837', 'f14': 'N信诺维', 'f18': 27.6}`。
   **`fltt=2` 让价格直接是小数（元）**，不是分。
2. **单只快照** `GET push2.eastmoney.com/api/qt/stock/get?secid=1.600519`
   → `{'f43': 125973, ..., 'f58': '贵州茅台', 'f60': 127275, 'f170': -102}`。
   这里的价格是**整数分**（125973 = 1259.73）、涨跌幅是**整数百分之一**（-102 = -1.02%），
   与第 1 条的 `fltt=2` 口径**不同** —— 同一族端点、同样的业务含义、两种单位。
   ⚠️ 这是本文件里最容易写错的地方，所以口径在代码里是**显式参数**（见 `CALIBER_*`）。
3. **历史日K** `GET push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=1`
   → `data.klines` 每行 `日期,开,收,高,低,成交量(手),成交额(元),振幅%`；
   实测 77 行、`2026-09-16,1273.93,1259.73,1274.98,1254.10,24318,3066631576.00,1.64`。
   价格是小数（元）；`fqt`：0=不复权 / 1=前复权 / 2=后复权；`secid` 前缀 `1.`=沪市。
4. **批量快照** `GET push2.eastmoney.com/api/qt/ulist.np/get?secids=1.600519,0.000001,0.920819`
   字段名与 `clist` 完全一致，一次请求拿多只。**同一个端点的两种口径都实测过**：
   * **带 `fltt=2`**（本模块采用）→ 值就是元：
     `{'f2': 1258.0, 'f3': -1.16, 'f5': 26235, 'f6': 3307926407.0, 'f12': '600519',
     'f14': '贵州茅台', 'f15': 1274.98, 'f16': 1254.1, 'f17': 1273.93, 'f18': 1272.75}`、
     `{'f12': '000001', 'f2': 11.7, 'f3': -1.02}`、`{'f12': '920819', 'f2': 3.09, 'f3': -0.32}`；
     其中 `f17 = 1273.93` 与当日 kline 的开盘价 **一字不差**（模块头第 3 条），
     这两次请求互为交叉验证；
   * **不带 `fltt`** → 值是整数**分**：同一只茅台 `f2 = 125800`、`f3 = -116`、`f15 = 127498`，
     而同一刻 `stock/get` 给的 `f43 = 125800`（同一个值）→ 这两个写法是**分**口径，
     125800 分 = **1258.00 元**，与带 `fltt=2` 的 `f2 = 1258.0` 完全对齐。
   结论：**一律带 `fltt=2`**（少一种口径就少一类静默错价）；`stock/get` 那一路
   现在也支持 `fltt=2`，但本模块**故意保持它不带**（`snapshot_one` + `CALIBER_FEN`），
   好让"分口径"这条实测路径留在代码里、有测试守着 —— 万一哪天 `fltt` 被服务端忽略，
   出问题的只有一处而不是全部。
5. **成交量单位 = 手**（两条独立的算术核对，不是照文档抄的）：
   * 688837：`f6 ÷ (f5 × 100) = 1521904022 ÷ 36079200 = 42.2 元/股`，
     落在该行 `f16=39.12 ~ f15=48.66` 区间内；
   * 600519：`3307926407 ÷ 2623500 = 1261.0 元/股`，与同刻 `f2=1258.00` 齐平。
   若 `f5` 已经是"股"，这两个均价会差 100 倍、**两只都**会跑出当日区间
   （与 `intraday.intraday_vwap` 里对同花顺 `volume` 做的那道核对同一个思路）。

已知风险（如实写，不假装）
--------------------------
- 这是**公开但未文档化**的接口：字段含义靠实测反推，官方随时可能改字段、加风控或限流。
  所以本模块的函数**拿不到就返回空/部分结果 + 记日志**，不把网络异常抛给界面。
- 请求要带 `User-Agent`；分页之间做最小限流（`PAGE_PAUSE`）。
- **没有**实时涨停池 / 复权因子 / 交易日历：东方财富这三个端点不提供，
  `sources.REGISTRY` 里也如实不写（那是同花顺那一路的能力）。
- **拿不到行业**：`clist` 这里只取 `f12/f14`（代码 + 名称），行业要另取板块字段/接口，
  不在本次实测范围内 —— 不写没验证过的东西。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Iterable

logger = logging.getLogger("laoa_trader.data.eastmoney")

#: 行情推送域名（快照 / 批量快照）
PUSH2 = "https://push2.eastmoney.com"
#: 历史行情域名（日K）—— 与 PUSH2 **不是同一个域名**
PUSH2HIS = "https://push2his.eastmoney.com"

#: 请求头。`User-Agent` 不能省：实测（2026-09-16）不带 UA 时服务端在建连后直接断开
#: （`Empty reply from server`，curl 退出码 52），带上标准浏览器 UA 就正常返回 JSON。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
#: 行情页来源（更接近真实浏览器；实测不带也能过）
REFERER = "https://quote.eastmoney.com/"

#: 沪深A股的板块过滤串（实测 `data.total = 5559`，2026-09-16）。
#: 四段：深市主板 t:6 / 深市创业板 t:80 / 沪市主板 t:2 / 沪市科创板 t:23。
#: `+` 与 `,` 都是字面量：实测 urlencode 成 `%2B`/`%2C` 之后服务端照样认
#: （同一次请求 total 同样是 5559），所以这里就用最朴素的 urlencode。
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

#: `clist` / `ulist.np` 共用的字段口径（实测值，见模块头）：
#:   f12 代码 / f14 名称 / f2 现价 / f3 涨跌幅% / f4 涨跌额 / f5 成交量(**手**) /
#:   f6 成交额(**元**) / f15 最高 / f16 最低 / f17 今开 / f18 昨收 /
#:   f8 换手率(%) / f21 流通市值(**元**，见 `YUAN_TO_YI`)
#: `f8`/`f21` 是 2026-09-17 为了与公开源"统一口径"补要的两列（实测原始响应：
#:   `{"f2":1266.98,"f5":17554,"f6":2217338283.0,"f8":0.14,"f12":"600519",
#:     "f21":1583828386835}`
#: ）—— 请求里不写这两个字段名，服务端**就不会返回它们**，所以字段串必须一起改。
#: `f10` = **量比**（2026-09-23 加：盘中口径的公式要它；实测与腾讯 `[49]` 同一口径）
SNAPSHOT_FIELDS = "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f8,f10,f21"

#: 单只快照 `stock/get` 的字段（**分口径**，见模块头第 2 条）：
#:   f43 现价 / f44 最高 / f45 最低 / f46 今开 / f47 成交量(手) / f48 成交额(元) /
#:   f57 代码 / f58 名称 / f60 昨收 / f170 涨跌幅(×100)
STOCK_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170"

#: 日K 字段：fields1 是元信息、fields2 是每行的列顺序
KLINE_FIELDS1 = "f1,f2,f3,f4,f5"
#: `日期,开,收,高,低,成交量(手),成交额(元),振幅%`（实测 2026-09-16）
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"

#: 价格字段的口径。**同一个字段名 `f2`/`f43`，带不带 `fltt=2` 是两种单位**（模块头实测）：
#:   * `CALIBER_YUAN`：带 `fltt=2` → 值就是元（`clist`/`ulist.np` 实测 `f2=1258.0`）；
#:   * `CALIBER_FEN` ：不带 `fltt` → 值是**分**、涨跌幅是**百分之一**
#:     （`ulist.np` 实测 `f2=125800`、`stock/get` 实测 `f43=125800`、`f170=-102`）。
#: 为什么口径是**显式参数**而不是"自动判断"：涨跌幅 `f3` 是**比值**，
#: 整体放大/缩小 100 倍时它**完全不变**（(a−b)/b 与 (100a−100b)/(100b) 相等），
#: 所以没有任何内部一致性校验能区分"1258.00 元"与"125800 分"。
#: 口径只能由**请求参数**钉死（本模块一律带 `fltt=2`，`snapshot_one` 那条路例外且显式声明），
#: 再由测试把两种口径各钉一遍（`tests/test_eastmoney.py`）。
CALIBER_YUAN = "yuan"
CALIBER_FEN = "fen"

#: 默认超时（秒）。接口很快（实测 <1s），给 10 秒足够；
#: 超时短一点能让"这一拍失败 → 下一拍重试"更快发生。
DEFAULT_TIMEOUT = 10.0

#: 连续分页之间**最小**间隔（秒）。`snapshot_all()` 一次要打
#: `ceil(5559/200) = 28` 个分页请求（实测 total=5559），连着打会被风控盯上。
PAGE_PAUSE = 0.2

#: 分页硬上限（防服务端 `total` 异常时无限翻页）：40 × 200 = 8000 行 > 实测 5559 只
MAX_PAGES = 40

#: 批量快照一次最多带几个 `secids`（与同花顺那一路的 100 同一个数量级）
SECID_BATCH = 100

#: `adjust` 参数 → `fqt`（0=不复权 / 1=前复权 / 2=后复权）
FQT = {"none": 0, "raw": 0, "qfq": 1, "forward": 1, "hfq": 2, "backward": 2}

#: 手 → 股。`f5`（成交量）与日K 的成交量列都是**手**（模块头第 5 条两条算术核对）
LOTS_TO_SHARES = 100

#: 单股价格的**可信上界**（元）。A 股没有万元级股价：实测（2026-09-16）贵州茅台的
#: 收盘价是 **1259.73 元**（kline），沪深A股里它就在最贵的那一档 ——
#: 写 10000 是给"将来出现更贵的票"留足余量。
#: 它的用途只有一个：**挡住"分被当成元"**（1258.00 元误读成 125800 元）。
#: 说清它挡不住什么：反过来"元被当成分"（1258.00 → 12.58）**看不出**，
#: 低价股被误读也看不出（3.09 元 → 309 元仍在合理区间）。
#: 所以它只是"最后一道哨兵"，口径仍然必须由端点钉死（见 `CALIBER_*`）。
MAX_PLAUSIBLE_PRICE = 10000.0

#: 归一化后的统一口径（**与 `sources.QUOTE_FIELDS` 必须一致**，有测试钉住）。
#: 为什么不集中在 `sources` 里共享：`sources` 会 import 本模块，
#: 本模块再 import sources 就成环了。
#: 2026-09-17 追加 `turnover_rate`(%) 与 `circ_mktcap`(亿)：这两列原来是公开源
#: （腾讯/新浪）独有的，收进统一口径之后，界面不必再问"你这个来源有没有换手率"。
UNIFIED_KEYS: tuple[str, ...] = (
    "symbol", "name", "last_price", "prev_close", "open",
    "high", "low", "volume", "turnover", "pct",
    "turnover_rate", "circ_mktcap", "volume_ratio",
)

#: 元 → 亿（统一口径里市值一律是**亿**）。
#: 为什么非要换算：东财 `f21` 是**元**，而腾讯 `ltsz`/`zsz` 是**亿** —— 两个来源直接
#: 进同一张表，用户看到的市值会差 1e8 倍。实测依据（2026-09-17，同一时刻两路对拉）：
#:   * 东财 `ulist.np`：`600519` → `f21 = 1583828386835`（元）；
#:   * 腾讯 `qt.gtimg.cn`：`600519` → `[44] = 15838.28`（亿）；
#:   `1583828386835 ÷ 1e8 = 15838.28`，与腾讯**一字不差** → `f21` 的单位确实是元。
YUAN_TO_YI = 1e8

#: 注入式 opener 的签名：`opener(url, params, timeout) -> dict`（**已解析的 JSON**）。
#: 为什么契约是"解析后的 dict"而不是字节流：测试要的是"固定 JSON、完全离线"，
#: 直接返回 dict 就不必再造一层假 response；真实那一路由 `_urllib_json` 负责。
Opener = Callable[[str, dict[str, Any], float], dict]


class EastmoneyError(RuntimeError):
    """东方财富接口错误（HTTP/解析失败，或信封 `rc != 0`）。"""


def _urllib_json(url: str, params: dict[str, Any], timeout: float) -> dict:
    """真实取数：`urlencode` + `urllib.request`（**零第三方依赖**）。"""
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": REFERER, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception as exc:  # noqa: BLE001 - 统一成一种错误类型，上层只认它
        raise EastmoneyError(f"请求失败：{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
        raise EastmoneyError(f"响应不是 JSON：{raw[:120]!r}") from exc


def _get_json(
    url: str,
    params: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    opener: Opener | None = None,
) -> dict:
    """取一个 JSON 响应；`opener` 可注入（测试用固定 JSON，**完全不联网**）。

    Raises:
        EastmoneyError: 网络/解析失败，或信封 `rc != 0`。
    """
    data = opener(url, params, timeout) if opener is not None else _urllib_json(
        url, params, timeout
    )
    if not isinstance(data, dict):
        raise EastmoneyError(f"响应不是对象：{type(data).__name__}")
    rc = data.get("rc")
    # 信封判据：正常 `rc == 0`；`rc = 100`（实测 `1.830799` 这种不存在的 secid 组合）
    # 表示"参数对，但没有这个标的"—— 两种都不该被当成"有数据"。
    if rc not in (0, None):
        raise EastmoneyError(f"接口返回 rc={rc}")
    return data


def _num(value: Any) -> float | None:
    """转数字；`-`（停牌时真的会返回 `"-"`）与空值都返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _price(value: Any, caliber: str) -> float | None:
    """价格字段 → **元**（`CALIBER_FEN` 时 ÷100）。"""
    number = _num(value)
    if number is None:
        return None
    return number / 100.0 if caliber == CALIBER_FEN else number


def _pct(value: Any, caliber: str) -> float | None:
    """涨跌幅字段 → **百分数**（`CALIBER_FEN` 时 ÷100：`f170=-102` → −1.02%）。"""
    number = _num(value)
    if number is None:
        return None
    return number / 100.0 if caliber == CALIBER_FEN else number


def _yi(value: Any) -> float | None:
    """市值字段（`f21`，单位**元**）→ **亿**；取不到 → None（**不是 0**）。

    `fltt=2` 影响的是价格类字段，市值不受它影响：实测（2026-09-17）带 `fltt=2` 时
    `600519` 的 `f21` 仍是 `1583828386835`（元），÷1e8 = 15838.28 亿。
    """
    number = _num(value)
    return number / YUAN_TO_YI if number is not None else None


# ── 代码 → secid ──


#: 北交所号段（与 `hithink._EXCHANGE_PREFIXES` 的北交所段一致；920 是 2025 年启用的新号段）。
#: **实测**（2026-09-16）：
#:   * `0.920819`（颖泰生物）→ 真实行情 `{'f43': 309, 'f58': '颖泰生物', 'f60': 310,
#:     'f170': -32, 'f47': 38280}` → 3.09 元 / −0.32%；
#:   * `1.830799` → `{"rc":100,"data":null}` —— 同一只票换成 `1.` 前缀**拿不到**，
#:     说明北交所在东方财富走**深市那种 `0.`**，不是沪市的 `1.`；
#:   * `0.430047` / `0.830799` 前缀被认（返回 `诺思兰德(已切换)` / `艾融软件(已切换)`，
#:     `f43=0`）：老代码已切到 920 号段，所以**前缀对、只是没价** ——
#:     无价的行按"没有现价"丢掉（见 `normalize_row`），不把 0 当成真价。
_BJ_PREFIXES: tuple[str, ...] = ("43", "83", "87", "88", "92", "82", "89")

#: 交易所后缀 → secid 前缀（实测：沪 `1.`、深 `0.`、北交所也走 `0.`）
_SUFFIX_MARKET = {"SH": "1", "SZ": "0", "BJ": "0"}


def to_secid(symbol: str) -> str:
    """本地代码 → 东方财富 `secid`（`1.600519` / `0.000001` / `0.920819`）。

    接受的写法：`600519` / `sh.600519` / `600519.SH` / `1.600519`（已是 secid 原样返回）。

    **认不出来就返回空串**，调用方据此跳过这只票：宁可这只票没有现价，
    也不要拿一个猜出来的 secid 去换回**别的标的**的价格
    （"张冠李戴的价格"比"没有价格"危险得多）。
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    head, _, tail = raw.partition(".")
    if head in ("0", "1") and tail.isdigit() and len(tail) == 6:
        return f"{head}.{tail}"                          # 已经是 secid
    if tail in _SUFFIX_MARKET:                           # 600519.SH
        ticker, suffix = head.zfill(6), tail
    elif head in _SUFFIX_MARKET and tail.isdigit():      # sh.600519 / bj.920819
        ticker, suffix = tail.zfill(6), head
    elif "." not in raw:
        ticker, suffix = raw.zfill(6), ""
    else:
        return ""                                        # 认不出的后缀（新交易所之类）
    if not (len(ticker) == 6 and ticker.isdigit()):
        return ""
    if suffix:
        return f"{_SUFFIX_MARKET[suffix]}.{ticker}"
    if ticker.startswith(_BJ_PREFIXES):
        return f"0.{ticker}"                             # 北交所：实测走 0.（见上）
    if ticker.startswith(("6", "9")):
        return f"1.{ticker}"                             # 沪市（含科创板 68x、沪B 900x）
    if ticker.startswith(("0", "3")):
        return f"0.{ticker}"                             # 深市（含创业板 300x/301x）
    # 其余（1/2/5 开头的基金、ETF 之类）**不猜**：宁可没有价
    return ""


# ── 归一化（**单位统一**：股 / 元）──


def normalize_row(row: dict, caliber: str = CALIBER_YUAN) -> dict | None:
    """一行快照字段（`f2/f3/f5/…`）→ 统一口径字典；没有代码或没有价 → None。

    **单位换算**（写错就是静默错价，所以每一处都注明实测依据）：
      * `f5` 成交量是**手** → ×100 得**股**（模块头第 5 条的两条算术核对）；
      * `f6` 成交额本来就是**元**，不动；
      * `f21` 流通市值是**元** → ÷1e8 得**亿**（`_yi`，与腾讯的"亿"对齐）；
      * 价格与涨跌幅按 `caliber` 归一（见 `CALIBER_*`）。

    Args:
        row: 一行原始字段。
        caliber: `CALIBER_YUAN`（`clist` + `fltt=2`）或 `CALIBER_FEN`（`ulist`/`stock/get`）。
    """
    symbol = str(row.get("f12") or "").strip()
    if not symbol or not symbol.isdigit():
        return None
    last = _price(row.get("f2"), caliber)
    if last is None or last <= 0:
        # 停牌/未开盘（`f2` 为 0 或 `-`）与"已切换"的老代码（实测 f43=0）：
        # 一律丢掉 —— "现价 0.00"比"没有现价"更容易被当真
        return None
    if last > MAX_PLAUSIBLE_PRICE:
        # 口径写错时的哨兵（见 `MAX_PLAUSIBLE_PRICE` 的说明：它只挡一个方向）
        logger.warning(
            "东方财富快照价格离谱（%s：%s 元）—— 疑似“分/元”口径写反，这一行丢掉",
            symbol, last,
        )
        return None
    volume = _num(row.get("f5"))
    return {
        "symbol": symbol,
        "name": str(row.get("f14") or "").strip(),
        "last_price": last,
        "prev_close": _price(row.get("f18"), caliber),
        "open": _price(row.get("f17"), caliber),
        "high": _price(row.get("f15"), caliber),
        "low": _price(row.get("f16"), caliber),
        "volume": volume * LOTS_TO_SHARES if volume is not None else None,   # 手 → 股
        "turnover": _num(row.get("f6")),                                     # 元（不动）
        "pct": _pct(row.get("f3"), caliber),
        # `f8` 换手率本来就是百分数原值（实测 600519 `f8=0.14`，与腾讯 `[38]` 一致）；
        # `f21` 是**元** → ÷1e8 成亿（实测 1583828386835 → 15838.28 亿）
        "turnover_rate": _num(row.get("f8")),
        "circ_mktcap": _yi(row.get("f21")),
        # `f10` 量比（倍，无量纲）：与腾讯 `[49]` 同口径（实测同一只票两边一致）；
        # 它给"盘中口径"的公式字段用（见 `formulas.SNAPSHOT_FIELDS`）
        "volume_ratio": _num(row.get("f10")),
    }


def rows_to_map(rows: Iterable[dict], caliber: str = CALIBER_YUAN) -> dict[str, dict]:
    """归一化后的行列表 → `{symbol: row}`（同一代码只会有一条）。"""
    out: dict[str, dict] = {}
    for row in rows:
        item = normalize_row(row, caliber)
        if item is not None:
            out[item["symbol"]] = item
    return out


# ── 快照 ──


def snapshot_all(
    page_size: int = 200,
    max_pages: int | None = None,
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    pause: float = PAGE_PAUSE,
) -> list[dict]:
    """**全市场**快照（分页拉完），返回归一化后的行列表（股 / 元）。

    为什么有这条路：沪深A股实测 **5559 只**（2026-09-16），要"很多票"时
    逐只问就是几千个请求（滥用公开接口）；分页 `pz=200` 只要 **28 个请求**。
    代价（如实说）：28 个请求按 `PAGE_PAUSE=0.2` 限流 ≈ 5.6 秒；
    只要几十只票请走 `snapshot()`（批量 secids，**1 个请求**）。

    Args:
        page_size: 每页行数。
        max_pages: 最多几页（None = 翻到服务端说没有更多，硬上限 `MAX_PAGES`）。
        pause: 分页之间最小间隔（秒）；测试传 0（别为了测试真的睡）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    page_size = max(int(page_size), 1)
    limit = MAX_PAGES if max_pages is None else max(int(max_pages), 1)
    page = 1
    total: int | None = None
    while page <= limit:
        if page > 1 and pause > 0:
            time.sleep(pause)
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,            # 按涨跌幅降序（与网页上看到的一致，便于人工核对）
            "np": 1,
            "fltt": 2,          # **价格直接是元**，见模块头第 1 条
            "invt": 2,
            "fid": "f3",
            "fs": MARKET_FS,
            "fields": SNAPSHOT_FIELDS,
        }
        try:
            data = _get_json(f"{PUSH2}/api/qt/clist/get", params, timeout, opener)
        except EastmoneyError as exc:
            # 只记日志：已经拿到的页照样返回（部分数据 > 没有数据）
            logger.info(f"东方财富全市场快照第 {page} 页失败：{exc}")
            break
        payload = data.get("data")
        rows = payload.get("diff") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = normalize_row(row, CALIBER_YUAN)
            if item is not None and item["symbol"] not in seen:
                seen.add(item["symbol"])
                out.append(item)
        total = _int_or_none(payload.get("total"))
        if total is not None and len(out) >= total:
            break
        if len(rows) < page_size:
            break       # 没给满一页 = 没有下一页
        page += 1
    return out


def snapshot(
    symbols: Iterable[str],
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    batch: int = SECID_BATCH,
    pause: float = PAGE_PAUSE,
) -> list[dict]:
    """**指定代码**的快照（批量 `ulist.np`），返回归一化后的行列表（股 / 元）。

    为什么是批量端点而不是逐只 `stock/get`：两张表要盯的票是几十只，
    逐只是几十个请求（`stock/get` 一次只问一只）；`ulist.np/get` 实测一次能带多个
    `secids`（`1.600519,0.000001` → 一次拿到两只），字段名与 `clist` 完全一致。
    **1 个请求 vs 几十个请求**，对公开接口就是"能不能用"的差别。

    ⚠️ 必须带 `fltt=2`（实测：带它 `f2 = 1258.0` 元，不带它是 `f2 = 125800` 分）。
    本方法因此按 `CALIBER_YUAN` 解析 —— 与 `clist` 同一口径。

    代价：一次能带多少 secid 没有实测上限，按 `SECID_BATCH=100` 切批
    （超了多几个请求）；认不出 secid 的代码（`to_secid` 返回空）**直接跳过、不发请求**。
    """
    out: list[dict] = []
    seen: set[str] = set()
    secids: list[str] = []
    for symbol in symbols or []:
        secid = to_secid(symbol)
        if secid and secid not in secids:
            secids.append(secid)
    if not secids:
        return out
    size = max(int(batch), 1)
    for start in range(0, len(secids), size):
        if start and pause > 0:
            time.sleep(pause)
        chunk = secids[start:start + size]
        params = {
            "fltt": 2,          # **价格直接是元**（实测，见模块头第 4 条）
            "invt": 2,
            "np": 1,
            "secids": ",".join(chunk),
            "fields": SNAPSHOT_FIELDS,
        }
        try:
            data = _get_json(f"{PUSH2}/api/qt/ulist.np/get", params, timeout, opener)
        except EastmoneyError as exc:
            logger.info(f"东方财富批量快照失败（这批 {len(chunk)} 只跳过）：{exc}")
            continue
        payload = data.get("data")
        rows = payload.get("diff") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = normalize_row(row, CALIBER_YUAN)
            if item is not None and item["symbol"] not in seen:
                seen.add(item["symbol"])
                out.append(item)
    return out


def snapshot_one(
    symbol: str,
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict | None:
    """**单只**快照（`stock/get`），归一化后的行；拿不到返回 None。

    这一路**故意不带 `fltt`**，所以是**分口径**：实测同一刻 `stock/get` 的 `f43 = 125800`
    与 `ulist.np` 带 `fltt=2` 的 `f2 = 1258.0` 是**同一只票的同一个价**
    （模块头第 4 条）—— 125800 分 = 1258.00 元，`CALIBER_FEN` 的 ÷100 有据可依。
    有了 `snapshot()` 的批量路，这个方法主要留给"只问一只票"与人工核对。
    认不出的 secid 或 `rc=100`（没有这个标的）都返回 None，不抛。
    """
    secid = to_secid(symbol)
    if not secid:
        return None
    params = {"secid": secid, "invt": 2, "fields": STOCK_FIELDS}
    try:
        data = _get_json(f"{PUSH2}/api/qt/stock/get", params, timeout, opener)
    except EastmoneyError as exc:
        logger.info(f"东方财富单只快照失败（{symbol}）：{exc}")
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    # 把 `stock/get` 的字段名搬成 `clist` 的写法，共用同一套归一化（少一份重复口径）
    row = {
        "f12": str(payload.get("f57") or "").strip() or str(symbol).strip(),
        "f14": payload.get("f58"),
        "f2": payload.get("f43"),
        "f3": payload.get("f170"),
        "f5": payload.get("f47"),
        "f6": payload.get("f48"),
        "f15": payload.get("f44"),
        "f16": payload.get("f45"),
        "f17": payload.get("f46"),
        "f18": payload.get("f60"),
    }
    return normalize_row(row, CALIBER_FEN)


# ── 历史日K ──


def daily(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "qfq",
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = 10000,
) -> list[dict]:
    """单只标的的历史日K（`[{date, open, high, low, close, volume, turnover}]`）。

    **单位归一**：每行成交量是**手** → ×100 得股；成交额本来就是元；
    价格是小数（实测 `2026-06-01,1298.98,1281.58,...`），不需要 ÷100。

    Args:
        symbol: 本地代码（裸 6 位 / 带后缀 / secid 都认，见 `to_secid`）。
        start/end: `YYYY-MM-DD`（内部转成接口要的 `YYYYMMDD`）。
        adjust: `qfq`（前复权，默认）/ `hfq`（后复权）/ `none`（不复权）。
        limit: 单次最多取多少根（`lmt`）。实测口径：真正决定窗口的是 `beg`/`end`
            —— `lmt=30` 与 `lmt=100` 两次请求都返回 `2026-06-01 ~ 2026-09-16`
            同一段 77 行（2026-09-16 实测）；`lmt` 只是"顶多这么多根"的上限，
            所以这里默认给 10000（相当于不限）。

    Returns:
        按日期升序（接口本来就升序）。认不出的代码或空窗口返回 `[]`；
        网络/接口错误**只记日志**（上层该退回已有本地数据，不该崩）。
    """
    key = str(adjust or "").strip().lower()
    if key not in FQT:
        raise ValueError(f"不认识的复权口径：{adjust!r}（可选 {sorted(FQT)}）")
    secid = to_secid(symbol)
    if not secid:
        logger.info(f"东方财富：认不出 secid 的代码 {symbol!r}，跳过日K")
        return []
    params = {
        "secid": secid,
        "fields1": KLINE_FIELDS1,
        "fields2": KLINE_FIELDS2,
        "klt": 101,             # 101 = 日线
        "fqt": FQT[key],        # 0 不复权 / 1 前复权 / 2 后复权
        "beg": _compact_date(start),
        "end": _compact_date(end),
        "lmt": int(limit),
    }
    try:
        data = _get_json(f"{PUSH2HIS}/api/qt/stock/kline/get", params, timeout, opener)
    except EastmoneyError as exc:
        logger.info(f"东方财富日K取数失败（{symbol}）：{exc}")
        return []
    payload = data.get("data")
    klines = payload.get("klines") if isinstance(payload, dict) else None
    if not isinstance(klines, list):
        return []
    out: list[dict] = []
    for line in klines:
        row = _parse_kline(line)
        if row is not None:
            out.append(row)
    return out


def _parse_kline(line: Any) -> dict | None:
    """`"日期,开,收,高,低,成交量(手),成交额(元),振幅%"` → 统一口径字典。

    列顺序是**实测**的（模块头第 3 条）：第 2 列是**开**、第 3 列是**收**、第 4 列才是**高**
    —— 与"开高低收"的常见习惯不同，照习惯写会把"收"和"高"互换，
    而且回测照样跑得出来（只是结论全错）。
    """
    parts = str(line or "").split(",")
    if len(parts) < 7:
        return None
    day, op, close, high, low = (p.strip() for p in parts[:5])
    volume, turnover = parts[5].strip(), parts[6].strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    number = _num(volume)
    return {
        "date": day,
        "open": _num(op),
        "high": _num(high),
        "low": _num(low),
        "close": _num(close),
        "volume": number * LOTS_TO_SHARES if number is not None else None,   # 手 → 股
        "turnover": _num(turnover),                                          # 元（不动）
    }


def _compact_date(value: Any) -> str:
    """`YYYY-MM-DD` / `YYYYMMDD` → 接口要的 `YYYYMMDD`（认不出就原样回，让服务端报错）。"""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return text


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── 代码表 ──


def stock_list(
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    page_size: int = 500,
    max_pages: int | None = None,
    pause: float = PAGE_PAUSE,
) -> list[dict]:
    """全市场代码 + 名称（`[{"symbol", "name"}]`），复用 `clist` 的 `f12/f14`。

    **只能拿到代码与名称**：这次请求的字段集里**没有行业**（也没有上市日期、
    退市标记）。要行业得另取板块字段/接口，不在本次实测范围内 ——
    所以如实只返回这两列，不用"看起来像"的默认值糊弄调用方。
    """
    out: list[dict] = []
    seen: set[str] = set()
    limit = MAX_PAGES if max_pages is None else max(int(max_pages), 1)
    page_size = max(int(page_size), 1)
    page = 1
    while page <= limit:
        if page > 1 and pause > 0:
            time.sleep(pause)
        params = {
            "pn": page, "pz": page_size, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "fid": "f12", "fs": MARKET_FS,
            "fields": "f12,f14",
        }
        try:
            data = _get_json(f"{PUSH2}/api/qt/clist/get", params, timeout, opener)
        except EastmoneyError as exc:
            logger.info(f"东方财富代码表第 {page} 页失败：{exc}")
            break
        payload = data.get("data")
        rows = payload.get("diff") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("f12") or "").strip()
            if symbol and symbol.isdigit() and symbol not in seen:
                seen.add(symbol)
                out.append({"symbol": symbol, "name": str(row.get("f14") or "").strip()})
        total = _int_or_none(payload.get("total"))
        if total is not None and len(out) >= total:
            break
        if len(rows) < page_size:
            break
        page += 1
    return out


__all__ = [
    "CALIBER_FEN",
    "CALIBER_YUAN",
    "DEFAULT_TIMEOUT",
    "EastmoneyError",
    "FQT",
    "LOTS_TO_SHARES",
    "MARKET_FS",
    "MAX_PAGES",
    "MAX_PLAUSIBLE_PRICE",
    "PAGE_PAUSE",
    "PUSH2",
    "PUSH2HIS",
    "SECID_BATCH",
    "SNAPSHOT_FIELDS",
    "UNIFIED_KEYS",
    "YUAN_TO_YI",
    "daily",
    "normalize_row",
    "rows_to_map",
    "snapshot",
    "snapshot_all",
    "snapshot_one",
    "stock_list",
    "to_secid",
]
