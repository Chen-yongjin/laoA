"""数据来源注册表 —— 「系统设置 · 数据来源」这一组的**真相源**。

用户 2026-09-16 的要求原话："数据来源默认同花顺，可添加辅助来源……
可用的其它源，让用户自主添加，用他自己的 key。"

于是有了这个模块，它只回答四个问题（界面**不要自己拼**这些答案）：
    1. 系统里**有哪些**来源可用、每个来源要什么凭据（`REGISTRY` / `source_states`）；
    2. 用户**启用了**哪几个、按什么顺序（`active_sources`，读 `cfg.data_sources`）；
    3. 每个来源**能做什么**（`SourceInfo.capabilities`，只写实测确认过的，
       不写"应该有"的 —— 见 `eastmoney` 的模块头"已知风险"）；
    4. 现在**从哪个来源取行情**、取回来的数是什么口径（`snapshot_map`）。
       ⚠️ 默认顺序是 `["hithink", "public"]` —— **同花顺是主源**（正经 API、
       不会像公开接口那样限流：实测公开接口抓全市场历史时，腾讯 fqkline 到 700 只
       左右就开始连续失败——222 只失败、9 分钟没有一次成功——新浪列表接口直接回
       HTTP 456），免 Key 的公开源是**兜底**（没配 Key / Key 失效时才用）；
       顺序由 `cfg.data_sources` 决定，**谁在前面谁先取**。

为什么启停只用 `cfg.data_sources` 一个键表达
--------------------------------------------
不给每个来源再加 `xxx_enabled` 布尔键：两个地方说同一件事，就一定会出现
"列表里有、开关是关"这种自相矛盾的状态，用户看到的就是"我明明加了却不生效"。
**列表本身就是启停**（`["hithink", "eastmoney"]` = 两个都启用），
顺序 = 优先级（前一个失败或没配 Key，就落到下一个）。

单位与口径（**这是本模块最要紧的一件事**）
------------------------------------------
不同来源的原始字段口径**不一样**，`snapshot_map` 出口一律归一成同一套（`QUOTE_FIELDS`）：
    * `volume`  = **股**（东方财富 `f5`/kline 是**手**，模块内 ×100；同花顺本来就是股）；
    * `turnover` = **元**（两边一致）；
    * 价格 = **元**（东方财富带 `fltt=2` 的接口是元；不带是**分**，
      这是实测出来的坑，见 `eastmoney` 模块头）。
归一放在这里，界面与两张表就永远只认一套口径 —— 否则"哪个来源在用"
会静默改变表格里数字的量级，那是最难看懂的一类 bug。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from laoa_trader.data import eastmoney, hithink as hx, public_quotes
from laoa_trader.log import get_logger

logger = get_logger(__name__)

# ── 能力名（`SourceInfo.capabilities` 的取值，只认这三个）──

#: 实时快照（现价 / 涨跌幅 / 今开 / 最高 / 最低 / 成交量额）
CAP_SNAPSHOT = "snapshot"
#: 历史日K（日线序列，含复权口径）
CAP_DAILY_HISTORY = "daily_history"
#: 股票代码表（全市场代码 + 名称）
CAP_STOCK_LIST = "stock_list"

#: 全部合法能力（`capabilities` 里不许出现别的东西 —— 有测试钉住）
CAPABILITIES: tuple[str, ...] = (CAP_SNAPSHOT, CAP_DAILY_HISTORY, CAP_STOCK_LIST)

#: 能力 → 中文（界面那一行"能做什么"直接用它，不要各处自己写一遍）
CAPABILITY_LABELS: dict[str, str] = {
    CAP_SNAPSHOT: "实时快照",
    CAP_DAILY_HISTORY: "历史日K",
    CAP_STOCK_LIST: "股票代码表",
}

#: 能力 → **更短**的中文（界面那一行"提供：…"用它）。
#:
#: 为什么还要一份短的（2026-09-20 用户："压成一句…把罗列压缩到一行内、别换行成墙"）：
#: `CAPABILITY_LABELS` 是给 tooltip 与文档用的完整说法，界面那一行一长就会折成好几行、
#: 看着像一段说明文。同一件事只留一套**语义**（都从 capabilities 集合生成），
#: 只是呈现长度不同 —— 短标签漏了某个能力时，tooltip 里那份完整说法仍能对照。
CAPABILITY_SHORT_LABELS: dict[str, str] = {
    CAP_SNAPSHOT: "实时快照",
    CAP_DAILY_HISTORY: "日线",
    CAP_STOCK_LIST: "股票列表",
}

#: `snapshot_map()` 出口的**统一口径**字段（顺序即文档顺序）。
#: `source`（来源 id）与 `as_of`（取数时刻 unix 秒）由 `snapshot_map` 补上，
#: 所以不在这份列表里；`eastmoney.UNIFIED_KEYS` 必须与它一致（有测试钉住）。
#:
#: 单位（**每个来源自己换算好，出口只有这一套口径**）：
#:     `volume` = **股**（东财/腾讯的"手"已在各自模块里 ×100）；
#:     `turnover` = **元**；价格 = **元**；
#:     `pct` = **百分数**（0.71 = 0.71%）；
#:     `turnover_rate` = **百分数**（0.14 = 0.14%）——2026-09-17 新增；
#:     `circ_mktcap` = **亿**（东财 `f21` 是元，已在 `eastmoney._yi` 里 ÷1e8）——2026-09-17 新增。
#: 后两列为什么收进统一口径：公开源（腾讯/新浪）本来就有，界面早就在用；
#: 不收进来的话，`snapshot_map` 只按这份列表挑键，它们会被**静默丢掉**
#: （见 `snapshot_map` 里的 `row.get`），于是"换一个来源换手率就没了"。
QUOTE_FIELDS: tuple[str, ...] = (
    "symbol", "name", "last_price", "prev_close", "open",
    "high", "low", "volume", "turnover", "pct",
    "turnover_rate", "circ_mktcap",
    # 量比（倍）：2026-09-23 加 —— 公式里的「盘中快照」字段 `现量比` 要它。
    # 同花顺的快照端点**没有**这一项，所以它同时进了 `SUPPLEMENT_FIELDS`：
    # 主源给不出来时按字段往后找来源补（公开源/东方财富都提供）。
    "volume_ratio",
)


@dataclass(frozen=True)
class SourceInfo:
    """一个数据来源的"身份证"（给界面渲染，也给取数分派用）。

    Attributes:
        id: 写进 `cfg.data_sources` 的键（小写英文）。
        name: 中文展示名。
        needs_key: 是否必须用户自己配 Key。
        capabilities: 能做哪些事，取值见 `CAPABILITIES`。
        note: 一句话说明**实测到的事实**与**已知风险**（界面直接显示这句）。
        key_config: 该来源的 Key 存在哪个配置键（免 Key 的来源为 None）。
    """

    id: str
    name: str
    needs_key: bool
    capabilities: frozenset[str]
    note: str
    key_config: str | None = None


#: 内置来源。**键顺序 = 界面里"没启用的来源"的展示顺序**（启用的那几个按
#: `data_sources` 的顺序排，也就是真正的优先级）；2026-09-18 起同花顺是主源，所以排第一。
REGISTRY: dict[str, SourceInfo] = {
    "hithink": SourceInfo(
        id="hithink",
        name="同花顺金融数据服务（内置）",
        needs_key=True,
        capabilities=frozenset({CAP_SNAPSHOT, CAP_DAILY_HISTORY, CAP_STOCK_LIST}),
        # 2026-09-20（用户："只保留当前用哪个来源 / 怎么申请 Key 这类有用的，纯解释的长句全删"）：
        # 这里原来是一段长文（能力清单 + config.toml 键名 + 环境变量 + 行为细节），
        # 现在只留"它是主源、要 Key、去哪申请"，外加半句**会影响用户下一步动作**的后果
        # （没 Key 时筛选会被数据自检拒绝）—— 其余属于实现细节，界面上不教。
        note=(
            "主源：需要 Key，在 https://fuyao.aicubes.cn 申请后填在下面"
            "（没配 Key 时筛选会被数据自检拒绝）。"
        ),
        key_config="hithink_api_key",
    ),
    "public": SourceInfo(
        id="public",
        name="公开行情源（腾讯为主，免 Key）",
        needs_key=False,
        #: ⚠️ 只声明**已经接进界面**的能力：目前生效范围是「自选标的」「持仓监控」
        #: 两张表的 现价/涨幅/市值/换手（快照）。`public_quotes.daily()` 虽然能取单只
        #: 历史日K，但**还没接进下载/日更链路**，所以在界面上不声明它 ——
        #: 声明了却取不到，用户会以为"这个源坏了"（`eastmoney` 那条同理）。
        capabilities=frozenset({CAP_SNAPSHOT}),
        # 同上（2026-09-20）：原来那一大段（生效范围、能力边界、实测数字、限流故事、
        # 字段清单、单位差异）全部删除，只留一句**用户需要知道的那件事** ——
        # 它免 Key、不用申请。"没 Key 时筛选会被自检拒绝"这半句挪到了同花顺那一行
        # （那才是用户会去找 Key 的地方）。
        note="兜底源：免 Key，不用申请、不用填。",
        key_config=None,
    ),
    "eastmoney": SourceInfo(
        id="eastmoney",
        name="东方财富（公开接口，免 Key）",
        needs_key=False,
        capabilities=frozenset({CAP_SNAPSHOT, CAP_DAILY_HISTORY, CAP_STOCK_LIST}),
        # 2026-09-20（用户："按同一把尺子压成一句…把它现在那段压掉，只留结论；
        # 实现细节别放界面上"）：原来那一大段（实测条数、单位差异、限流细节、
        # 能力边界、klt 参数）全部删掉，只留"什么时候轮得到它、有什么风险"。
        note="兜底源之一：需要【添加来源】才会用到，接口未文档化、会被限流，只作备用。",
        key_config=None,
    ),
}


def _as_id_list(value: Any) -> list[str]:
    """`cfg.data_sources` → 规范的小写 id 列表（去空、去重、保持顺序）。

    为什么容错：用户手改 `config.toml` 时很常见把列表写成字符串
    （`data_sources = "hithink,eastmoney"`），`load_config` 那边已经会转成列表，
    但 `Config(...)` 直接构造出来的对象（测试、CLI 内部）可能还是字符串 ——
    这里再兜一次，免得"配置写错"表现成"来源无声消失"。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name and name not in out:
            out.append(name)
    return out


#: 已经**告警过**的"未知来源 id 组合"（进程内去重）。
#: 为什么需要：`active_sources` 在热路径上（`ui/quotes.py` 每 5 秒问一次
#: `usable_sources`），配置里写错一个词就会变成"每 5 秒一条 warning"，
#: 一天下来把日志刷满 —— 同一个错说一次就够了，配置改过之后组合会变、自然会再说一次。
_warned_unknown: set[tuple[str, ...]] = set()


def active_sources(cfg: Any) -> list[SourceInfo]:
    """按 `cfg.data_sources` 的顺序返回**已实现且已启用**的来源。

    认不出的 id **忽略并记日志**（不抛）：配置文件写错一个词，
    用户该看到的是"这个来源我没实现"，而不是界面起不来。
    这里也**不检查 Key**：没配 Key 的启用来源照样返回（界面要如实显示
    "已启用但没 Key"），真正取数时它会自动跳过、落到下一个来源（见 `snapshot_map`）。
    """
    enabled = _as_id_list(getattr(cfg, "data_sources", None))
    out: list[SourceInfo] = []
    unknown: list[str] = []
    for name in enabled:
        info = REGISTRY.get(name)
        if info is None:
            unknown.append(name)
            continue
        out.append(info)
    if unknown and tuple(unknown) not in _warned_unknown:
        _warned_unknown.add(tuple(unknown))
        logger.warning(
            "data_sources 里有未实现的来源 id：%s（已忽略；可用的是 %s）",
            "、".join(unknown), "、".join(REGISTRY),
        )
    return out


def has_key(cfg: Any, info: SourceInfo) -> bool:
    """这个来源需要的凭据齐了吗（**免 Key 的来源恒为 True**）。

    为什么免 Key 也算"齐"：界面上这一列要回答的是"这个来源现在能不能用"。
    对东方财富来说"不需要 Key" 就是"可用"，画一个红叉反而会让用户以为要填东西。
    同花顺那一路认两种写法：`config.toml` 的 `hithink_api_key`，或环境变量
    （`hx.available()` 就是这两条的合并判断）。
    """
    if not info.needs_key:
        return True
    if info.key_config == "hithink_api_key":
        return bool(str(getattr(cfg, "hithink_api_key", "") or "").strip() or hx.available())
    return bool(str(getattr(cfg, info.key_config or "", "") or "").strip())


def capabilities_text(info: SourceInfo) -> str:
    """能力的**完整**中文一行（给 tooltip 与文档用；按 `CAPABILITIES` 顺序，认不出的原样列）。"""
    labels = [CAPABILITY_LABELS.get(cap, cap) for cap in CAPABILITIES
              if cap in info.capabilities]
    labels += sorted(cap for cap in info.capabilities if cap not in CAPABILITIES)
    return "、".join(labels) if labels else "（无）"


def capabilities_brief(info: SourceInfo) -> str:
    """能力的**短**中文一行（界面那个"提供：…"标签用它，保证一行放得下）。

    与 `capabilities_text` 是同一份 `capabilities` 集合的两种长度，语义不会漂移：
    短标签认不出来时退回完整标签（宁可长一点，也不显示一个内部英文键）。
    """
    labels = [CAPABILITY_SHORT_LABELS.get(cap, CAPABILITY_LABELS.get(cap, cap))
              for cap in CAPABILITIES if cap in info.capabilities]
    labels += sorted(cap for cap in info.capabilities if cap not in CAPABILITIES)
    return "、".join(labels) if labels else "（无）"


def usable_sources(cfg: Any) -> list[SourceInfo]:
    """**现在真的能取数**的来源：已启用、已实现、凭据齐（或免 Key）。

    与 `active_sources` 的区别只有一条：这里把"启用了但没配 Key"的剔掉了。
    为什么要分成两个函数：界面要显示"你启用了同花顺但没填 Key"（`source_states`），
    而"该不该现在去发请求"必须用一个更严的判据（没 Key 就一次请求都不发）——
    `ui/quotes.py` 的 `should_request()` 用的就是这一个。
    """
    return [info for info in active_sources(cfg) if has_key(cfg, info)]


def source_states(cfg: Any) -> list[dict]:
    """给界面用的一行一个来源（**界面只渲染这个，不要自己拼**）。

    返回**全部**内置来源，而不是只有启用的那几个：设置页要能"添加来源"
    （用户原话"可用的其它源，让用户自主添加"）—— 只列已启用的，用户就没有东西可加。
    排序：**已启用的按 `data_sources` 的顺序在前**（顺序 = 实际优先级），
    没启用的按 `REGISTRY` 的定义顺序跟在后面。

    Returns:
        每项：`{id, name, enabled, needs_key, has_key, capabilities_text, note,
        key_config, capabilities}`（`capabilities` 是元组，界面要自己判断时用）。
    """
    enabled = _as_id_list(getattr(cfg, "data_sources", None))
    order = {name: index for index, name in enumerate(enabled)}
    infos = sorted(
        REGISTRY.values(),
        key=lambda info: (order.get(info.id, len(order)), list(REGISTRY).index(info.id)),
    )
    return [
        {
            "id": info.id,
            "name": info.name,
            "enabled": info.id in order,
            "needs_key": info.needs_key,
            "has_key": has_key(cfg, info),
            #: 完整说法（tooltip / 文档用）与**短**说法（界面那一行用）各给一份：
            #: 同一份 capabilities 集合的两种长度，界面拿短的那份才不会被折成墙
            "capabilities_text": capabilities_text(info),
            "capabilities_brief": capabilities_brief(info),
            "note": info.note,
            "key_config": info.key_config,
            "capabilities": tuple(sorted(info.capabilities)),
        }
        for info in infos
    ]


# ── 取数：同花顺那一路 ──


def hithink_rows_to_map(rows: Any) -> dict[str, dict]:
    """同花顺原始快照行 → 统一口径 `{symbol: 字段 dict}`（**公开**，供注入假客户端复用）。

    为什么单独暴露出来：`ui/quotes.py` 有一条"注入客户端"的测试通路
    （`fetch_snapshot_prices(cfg, symbols, client=...)`），它拿到的就是这种原始行；
    把转换放在这里，全项目就**只有一处**知道"同花顺的行长什么样、单位是什么"。

    口径（实测记录见 `intraday.intraday_vwap`）：`volume` 单位是**股**、`turnover` 是**元**，
    两个都不需要换算（与东方财富的"手"不同）。
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        thscode = str(row.get("thscode") or row.get("ticker") or "").strip()
        if not thscode:
            continue
        try:
            symbol = hx.to_local_symbol(thscode)
        except ValueError:
            continue        # 认不出的代码（新后缀之类）跳过，不影响其余票
        last = _num(row.get("last_price"))
        if not last:
            # 停牌 / 新股未开盘（`last_price` 为 0 或 None）：丢掉 ——
            # "现价 0.00" 比"没有现价"更容易被当真
            continue
        out[symbol] = {
            "symbol": symbol,
            # 同花顺快照**不返回中文名**（项目里早有实测记录）：
            # 名称要靠本地 `stock_basic` 或 `meta/tickers/list`，这里如实留空
            "name": "",
            "last_price": last,
            "prev_close": _num(row.get("prev_price")),
            "open": _num(row.get("open_price")),
            "high": _num(row.get("high_price")),
            "low": _num(row.get("low_price")),
            "volume": _num(row.get("volume")),
            "turnover": _num(row.get("turnover")),
            "pct": _num(row.get("price_change_ratio_pct")),
        }
    return out


def _hithink_rows(cfg: Any, symbols: list[str] | None) -> dict[str, dict]:
    """同花顺快照 → 统一口径；**没有 Key 直接返回空**（一次请求都不发）。"""
    if not (str(getattr(cfg, "hithink_api_key", "") or "").strip() or hx.available()):
        logger.debug("同花顺：未配置 API Key，跳过（会落到下一个可用来源）")
        return {}
    client = hx.HithinkClient(
        api_key=str(getattr(cfg, "hithink_api_key", "") or "").strip() or None,
        pace=0.05,
    )
    if symbols is None:
        # 全市场：同花顺那边就是分页 `/a-share/prices/snapshot`（一次 100 只）
        rows = client.snapshot()
    else:
        rows = []
        for start in range(0, len(symbols), hx.AUCTION_BATCH):
            rows.extend(client.snapshot(symbols[start:start + hx.AUCTION_BATCH]))
    return hithink_rows_to_map(rows)


# ── 取数：公开源那一路（免 Key，兜底）──


def _public_rows(cfg: Any, symbols: list[str] | None) -> dict[str, dict]:
    """公开源快照（腾讯为主、新浪兜底）→ 统一口径。

    `symbols is None`（"要全市场"）时返回空：腾讯没有"给我全部"的开关，全市场得由
    调用方提供代码表（我们的 `stock_basic` 就是）。这样公开源不需要自己去查代码表，
    也就不会多出一个"作者静态 JSON"式的第三方依赖。
    """
    if symbols is None:
        logger.debug("公开源：未给代码表，跳过（全市场请由调用方提供 symbols）")
        return {}
    rows = public_quotes.snapshot(symbols)
    return {str(row["symbol"]): row for row in rows.values() if row.get("symbol")}


# ── 取数：东方财富那一路 ──


def _eastmoney_rows(cfg: Any, symbols: list[str] | None) -> dict[str, dict]:
    """东方财富快照 → 统一口径（`eastmoney` 已经做过手→股、分→元）。

    有明确代码时走**批量 secids**（一个请求拿几十只）；`symbols is None`
    （"我要全市场"）才走分页，代价是 ceil(5559/200)=28 个请求（实测 total=5559），
    所以这条只给"确实要全市场"的调用方用。
    """
    if symbols is None:
        rows = eastmoney.snapshot_all()
    else:
        rows = eastmoney.snapshot(symbols)
    return {
        str(row["symbol"]): row
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


#: 来源 id → 取数函数。**新增一个来源 = 往 REGISTRY 加一条 + 在这里注册一个函数**。
#: 不在这里的来源（用户写了个没实现的 id）在 `active_sources` 就被过滤掉了。
_SNAPSHOT_FETCHERS: dict[str, Callable[[Any, list[str] | None], dict[str, dict]]] = {
    "public": _public_rows,
    "hithink": _hithink_rows,
    "eastmoney": _eastmoney_rows,
}


#: 能**一次把全市场拉回来、并且带流通市值**的来源（2026-10-08 加，板块热力图要它）。
#: 现在只有东方财富（clist 分页，`ceil(5559/200)` = 28 个请求）：
#:   * 公开源出局 —— 腾讯/新浪没有"给我全部"的开关，全市场得由调用方给代码表；
#:   * 同花顺出局 —— 它也能翻页给全市场，但那个端点**不回市值**（见 `SUPPLEMENT_FIELDS`
#:     那段）。让它先命中的结果是**静默**画不出面积的一屏灰块，最难查的一类故障。
FULL_MARKET_MKTCAP_SOURCES: tuple[str, ...] = ("eastmoney",)


def _normalize(
    rows: dict[str, dict], wanted: list[str] | None, source_id: str, as_of: float,
) -> dict[str, dict]:
    """某来源的原始行 → 统一口径（`QUOTE_FIELDS` 全键都在，缺的就是 `None`）。

    `wanted is not None` 时只留用户要的那些：来源可能多给（全市场快照就是多给）。
    """
    out: dict[str, dict] = {}
    for symbol, row in (rows or {}).items():
        if wanted is not None and symbol not in wanted:
            continue
        item = {name: row.get(name) for name in QUOTE_FIELDS}
        item["symbol"] = symbol
        item["source"] = source_id
        item["as_of"] = as_of
        out[str(symbol)] = item
    return out


def _num(value: Any) -> float | None:
    """转数字；认不出返回 None（`-`、空串、None 都是"没有这个数"）。"""
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


def snapshot_map(cfg: Any, symbols: list[str] | None = None) -> dict[str, dict]:
    """取一轮实时快照 → `{symbol: 统一口径字典}`（**绝不抛到界面**）。

    行为（顺序即规则）：
        1. 按 `active_sources(cfg)` 的顺序，**一个来源一个来源地试**，
           不并发（同一时刻只有一个来源在跑：公开接口上并发只是更快地被限流，
           而且失败时也说不清是谁失败）；
        2. 只试**声明了 `snapshot` 能力**的来源；
        3. **第一个拿到非空结果的来源胜出**，整张表就由它供给 ——
           不逐只票去挑"谁先回答"：同一张表里两只票的价来自两个来源、
           时间戳还不一样，用户根本没法解释；
        4. 某个来源失败（网络/限流/没配 Key）→ 只记日志，继续下一个；
        5. 全都不行 → 返回 `{}`（界面退回本地收盘价并标注，不弹错误）。

    Args:
        cfg: 配置（`data_sources` 决定顺序与启停）。
        symbols: 要取的本地代码；**None = 不限**（要全市场，代价见 `_eastmoney_rows`）。

    Returns:
        每只票：`QUOTE_FIELDS` 的全部键 + `source`（来源 id）+ `as_of`（取数时刻 unix 秒）。
    """
    wanted: list[str] | None = None
    if symbols is not None:
        wanted = [s for s in dict.fromkeys(str(s).strip() for s in symbols) if s]
        if not wanted:
            return {}       # 没有要盯的票：一次请求都不发
    as_of = time.time()
    for info in active_sources(cfg):
        if CAP_SNAPSHOT not in info.capabilities:
            continue
        if not has_key(cfg, info):
            # 启用了但凭据不齐：**一次请求都不发**（发了也是 401），直接换下一个来源。
            # 界面那一行会显示"已启用，但没配 Key"（见 `source_states`）。
            logger.info(f"来源 {info.name} 没配凭据，跳过（换下一个来源试）")
            continue
        fetch = _SNAPSHOT_FETCHERS.get(info.id)
        if fetch is None:
            logger.info(f"来源 {info.id} 声明了快照能力但没有取数实现（跳过）")
            continue
        try:
            rows = fetch(cfg, wanted)
        except Exception as exc:  # noqa: BLE001 - 一个来源失败不该影响下一个
            logger.info(f"来源 {info.name} 取快照失败（换下一个来源试）：{exc}")
            continue
        if not rows:
            logger.info(f"来源 {info.name} 没拿到快照（换下一个来源试）")
            continue
        out = _normalize(rows, wanted, info.id, as_of)
        if out:
            logger.debug(f"实时快照来源：{info.id}（{len(out)} 只）")
            return out
    logger.info("没有可用来源取到快照（表格退回本地收盘价并标注）")
    return {}


#: 主源**可能给不出来**的字段。目前只有这两个，「自选标的」「持仓监控」两张表各占一列。
#:
#: 为什么会有"主源给不出来"这回事：同花顺 fuyao 的 `/a-share/prices/snapshot` 只回
#: `last_price / price_change / price_change_ratio_pct / open_price / high_price /
#: low_price / prev_price / volume / turnover` —— **没有换手率、没有市值**
#: （官方文档 `vendor/Financial-API/docs/api/endpoints-prices.md` 的"响应字段"表写得很死；
#: 全项目能拿到流通市值的只有竞价端点 `float_market_cap`，那要求正处竞价时段）。
#: 用户实报过："新加入的市值和换手，都没有数据" —— 因为他配了 Key、走的是同花顺主源。
#: `volume_ratio`（量比）同样是主源给不出来的：同花顺 snapshot 只回价量那几个字段。
SUPPLEMENT_FIELDS: tuple[str, ...] = ("turnover_rate", "circ_mktcap", "volume_ratio")


def supplement_map(
    cfg: Any, quotes: dict[str, dict], symbols: list[str], *, opener: Any = None
) -> dict[str, dict]:
    """按**字段**补齐主源给不了的项（价格仍由主源给，只补缺的那几项）。

    为什么是"字段级补齐"而不是直接换来源：同花顺的价格是最稳的一路（用户也明确要它当
    主源），为了两列去换掉整个快照不值得；而不补的话那两列永远是 `—`。
    所以：**先拿到谁的价格就用谁的**，缺的字段按 `data_sources` 的顺序**往后**找一个
    能给出来的来源（默认就是免 Key 的公开源：腾讯一次 100 只，够用且便宜）。

    规矩：
        * 只给**已经有报价**的票补字段（不给没有价格的票凭空造一条记录）；
        * 不给"主源自己"重复发请求（它已经回过一次了，缺就是缺）；
        * 补到的字段带上 `field_source`（那一项的来源 id），界面据此如实写出处 ——
          价格与这两项可能来自两个源，tooltip 里必须说得出这件事；
        * **绝不抛**：补不到就还是 `None`（界面显示 `—`，与"这个源不提供"同一回事）。
    """
    if not quotes:
        return quotes
    fields = SUPPLEMENT_FIELDS
    need = [
        symbol for symbol in dict.fromkeys(str(s) for s in symbols)
        if symbol in quotes and any(quotes[symbol].get(name) is None for name in fields)
    ]
    if not need:
        return quotes
    try:
        candidates = usable_sources(cfg)
    except Exception as exc:  # noqa: BLE001 - 来源注册表读不出来就不补
        logger.debug(f"补齐字段时读来源注册表失败（不补）：{exc}")
        return quotes
    for info in candidates:
        if CAP_SNAPSHOT not in info.capabilities:
            continue
        fetch = _SNAPSHOT_FETCHERS.get(info.id)
        if fetch is None:
            continue
        # 已经给过数据的那个来源跳过（它缺的字段不会再变出来）
        todo = [symbol for symbol in need if quotes[symbol].get("source") != info.id]
        if not todo:
            continue
        try:
            rows = fetch(cfg, todo)
        except Exception as exc:  # noqa: BLE001 - 补不到就算了
            logger.info(f"补齐字段：{info.name} 取数失败（继续试下一个）：{exc}")
            continue
        got = 0
        for symbol, row in (rows or {}).items():
            target = quotes.get(symbol)
            if target is None:
                continue
            for name in fields:
                if target.get(name) is None and row.get(name) is not None:
                    target[name] = row[name]
                    target.setdefault("field_source", {})[name] = info.id
                    got += 1
        if got:
            logger.debug(f"补齐字段：{info.name} 补上 {got} 项（{fields}）")
        need = [symbol for symbol in need
                if any(quotes[symbol].get(name) is None for name in fields)]
        if not need:
            break
    return quotes


def full_market_quotes(cfg: Any) -> dict[str, dict]:
    """**要流通市值**的全市场快照：挑"能一次给全市场、且带市值"的来源（**绝不抛**）。

    为什么不能直接用 `snapshot_map(cfg, None)`（2026-10-08，板块热力图）
    --------------------------------------------------------------
    `snapshot_map` 挑来源的判据只有"有没有快照能力"，所以**主源（同花顺）会先命中**，
    而它的快照不带 `circ_mktcap`（见 `SUPPLEMENT_FIELDS` 那段）→ 按市值分面积的
    热力图一块都算不出面积，而且**失败是静默的**：有涨跌幅、面积全是 0，
    用户只会觉得"热力图坏了"。这个函数把"字段要求"提前到挑来源这一步。

    Returns:
        `{symbol: 统一口径快照}`；**没有来源能给就是空字典**（不是异常）——
        调用方据此退到"本地代码表 + 逐字段补齐"（见 `ui/app.py` 的 `_heatmap_payload`），
        那条路慢一些（按代码表分批），但同样能拿到市值。
    """
    for info in usable_sources(cfg):
        if info.id not in FULL_MARKET_MKTCAP_SOURCES:
            continue
        if CAP_SNAPSHOT not in info.capabilities:
            continue
        fetch = _SNAPSHOT_FETCHERS.get(info.id)
        if fetch is None:
            continue
        try:
            rows = fetch(cfg, None)
        except Exception as exc:  # noqa: BLE001 - 这条来源失败不该让热力图整块消失
            logger.info(f"全市场快照：{info.name} 取数失败（继续看下一个）：{exc}")
            continue
        if not rows:
            logger.info(f"全市场快照：{info.name} 没拿到数据（继续看下一个）")
            continue
        out = _normalize(rows, None, info.id, time.time())
        if out:
            logger.debug(f"全市场快照来源：{info.id}（{len(out)} 只）")
            return out
    logger.info("没有来源能一次给出带市值的全市场快照（调用方退到按代码表取）")
    return {}


def probe_source(cfg: Any, source_id: str, symbol: str = "600519") -> dict:
    """**只测这一个来源**通不通（设置页那一行的【测试】按钮用；给界面留的钩子）。

    为什么单独一个函数：`snapshot_map` 是"按顺序挑第一个能用的"，它会**自动落到下一个**
    —— 拿它去测某一行，测的可能是别的来源（用户点"东方财富"却测出了同花顺的连通性）。
    这里不做任何回退：要测谁就只问谁。

    返回（**永远不抛**，界面直接显示 `text`）：
        `{"ok": bool, "text": str, "source": id, "symbol": symbol, "price": float|None}`
    """
    name = str(source_id or "").strip().lower()
    info = REGISTRY.get(name)
    if info is None:
        return {"ok": False, "text": f"没有这个来源：{source_id!r}（可用：{'、'.join(REGISTRY)}）",
                "source": name, "symbol": symbol, "price": None}
    if CAP_SNAPSHOT not in info.capabilities:
        return {"ok": False, "text": f"{info.name} 不支持实时快照（{capabilities_text(info)}）",
                "source": name, "symbol": symbol, "price": None}
    if not has_key(cfg, info):
        key_hint = f"请先在 config.toml 里填 {info.key_config}" if info.key_config else "缺少凭据"
        return {"ok": False, "text": f"{info.name} 还没配 Key：{key_hint}",
                "source": name, "symbol": symbol, "price": None}
    fetch = _SNAPSHOT_FETCHERS.get(name)
    if fetch is None:
        return {"ok": False, "text": f"{info.name} 声明了快照能力但还没有取数实现",
                "source": name, "symbol": symbol, "price": None}
    ticker = str(symbol or "").strip() or "600519"
    try:
        rows = fetch(cfg, [ticker])
    except Exception as exc:  # noqa: BLE001 - 测试连接绝不能把异常弹到界面
        logger.info(f"测试来源 {name} 失败：{exc}")
        return {"ok": False, "text": f"{info.name} 取数失败：{exc}",
                "source": name, "symbol": ticker, "price": None}
    item = rows.get(ticker) if isinstance(rows, dict) else None
    if not item or not item.get("last_price"):
        return {"ok": False,
                "text": f"{info.name} 通了但没拿到 {ticker} 的价（停牌/认不出的代码/限流？）",
                "source": name, "symbol": ticker, "price": None}
    pct = item.get("pct")
    tail = f"，涨跌 {float(pct):+.2f}%" if isinstance(pct, (int, float)) else ""
    return {"ok": True,
            "text": f"通：{info.name} 取到 {ticker} = {float(item['last_price']):.2f}{tail}",
            "source": name, "symbol": ticker, "price": float(item["last_price"])}


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_LABELS",
    "CAP_DAILY_HISTORY",
    "CAP_SNAPSHOT",
    "CAP_STOCK_LIST",
    "FULL_MARKET_MKTCAP_SOURCES",
    "QUOTE_FIELDS",
    "REGISTRY",
    "SourceInfo",
    "active_sources",
    "CAPABILITY_SHORT_LABELS",
    "capabilities_brief",
    "capabilities_text",
    "full_market_quotes",
    "has_key",
    "hithink_rows_to_map",
    "probe_source",
    "snapshot_map",
    "source_states",
    "supplement_map",
    "usable_sources",
]
