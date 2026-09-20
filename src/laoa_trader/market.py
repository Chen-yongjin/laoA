"""大盘概览：涨跌停家数 + 沪深成交额 + 宽基/情绪/板块三组指数。

这一层**刻意不碰界面**：`fetch_overview()` 只吃一份 `Config` 和一个"能取数的对象"
（真实 `HithinkClient` 或测试用的假客户端），把结果整理成一个普通 dict，
再由 `kpi_values()` / `entry_fields()` / `lines()` 统一排版成文本：
「大盘概览」页与 `--cli --market` 用的都是这一份实现 —— 两处口径必须一致，
否则用户会看到"界面和命令行对不上"。

「大盘概览」页长这样（组在配置里为空时，**整组连标题一起隐藏**）。
2026-09-17 改版（用户要求）：成交额不再分沪/深/北三格，而是**沪深相加一个数**，
并且与涨跌停/涨跌家数一起挪到页面**最上面**的「成交与情绪」块里排成一行
（那一块的标题与排列在 `ui/app.py`，这里是**数值口径**）：

    大盘概览
    成交与情绪  成交额 12991亿  涨停 55  跌停 16  炸板 30  上涨 3126  下跌 2224  平盘 221
    宽基指数    上证 3885.33 -0.07%   深成 13384.57 -0.64%   …
    情绪指数    同花顺情绪 885.53 -0.18%   昨日连板 6928.17 +3.53%   …
    热门板块    上涨前五 / 下跌前五 两张表（`sectors` 取数，见 `ui/app.py`）
    数据来源：公开行情接口（…）与同花顺金融数据服务 · 更新于 17:50（每分钟自动刷新） [立即刷新]

`--cli --market` 打印的还是"一行一组"的老样子（`lines()` 把同一批值按老口径拼起来），
所以这个模块里**取数与格式化只有一份**：界面拿"一个指标一个值"（`kpi_values()`）、
"一个指数三个字段"（`entry_fields()`），命令行拿拼好的整行。两处口径必须一致，
否则用户会看到"界面和命令行对不上"。

三条硬性规矩（都是踩过坑才写下来的）
------------------------------------
1. **降级优先**：任何一路（涨跌停 / 指数三组 / 全市场汇总）失败，都只往 `errors` 里记一条，
   其余照常返回；**单个非法代码只影响它自己**（见 `hithink.index_snapshot` 的逐只重试），
   界面只少那一项。概览是"扫一眼"的参考信息，绝不能因为它把界面或命令搞挂。
2. **TTL 缓存与界面时钟解耦**：概览页自己每 60 秒刷一次，但取数有 TTL
   （`market_overview_ttl`，默认 55 秒），到点才真的打接口 —— 接口有配额、也怕限流；
   55 秒这个值特意略小于 60 秒，避免"每分钟刷一次却总差一点没到点"的边界抖动。
   读缓存时结果里 `stale=True`，界面据此在 tooltip 里说明"这是几分钟前的数"。
3. **全市场汇总单独一档 + 自己更长的 TTL**：涨跌家数要翻 6 页全市场快照
   （5571 只 / 每页 1000），所以即使 `market_breadth` 默认开着，它也有独立的 5 分钟缓存
   （`_BREADTH_TTL`），**不跟着每分钟的页面刷新跑** —— 否则配额和限流都吃不消。
   （同一趟汇总里还顺手算了北交所成交额，但 2026-09-17 起界面不再显示它，
   见 `fetch_overview` 里的说明 —— 涨跌家数才是这一档**唯一**的显示项。）

数据口径（照抄已用真实接口验证过的结论，别自己另找端点）
--------------------------------------------------------
- 涨跌停家数：`special-data/limit-up|limit-down|limit-break-pool?size=1` → `pagination.total`；
- 指数点位与涨跌幅：`a-share-index/prices/snapshot?thscodes=...`（**必须传代码**，
  不支持全市场；一个非法代码会让整批 1002，所以客户端里逐只重试）；
  三组（宽基 / 情绪 / 板块）是**同一个端点、同一批请求**，只是配置里分成三组便于分别换口径；
- 沪深成交额：直接取 `000001.SH`（上证指数）与 `399001.SZ`（深证成指）的 `turnover`
  —— 这两个数就是沪深两市的成交额（深证综指 `399106.SZ` 报的数与深证成指完全相同）。
  界面与命令行显示的是**两者相加**（`turnover["total"]`，见 `kpi_values()`）；
- 北交所成交额：全市场快照分页按 `.BJ` 后缀汇总（所以慢一档）。**2026-09-17 起不再显示**：
  用户要求"成交额 = 沪 + 深，一个数；北交所删掉"，所以它只是分页汇总的副产品，
  留在 `turnover["bj"]` 里供 `has_data()` 与将来恢复用，界面上没有任何地方画它；
- 涨跌家数：同一趟全市场快照分页按涨跌幅分桶（`market_breadth` 关掉时不取）。
"""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import Any

from laoa_trader.data import hithink as hx
from laoa_trader.data import public_market
from laoa_trader.log import get_logger

logger = get_logger(__name__)

# ── 端点（与 hithink.BASE_URL 拼起来用；常量放这里，界面/CLI 都不该自己拼路径）──
LIMIT_UP_PATH = "/a-share/special-data/limit-up-pool"
LIMIT_DOWN_PATH = "/a-share/special-data/limit-down-pool"
LIMIT_BREAK_PATH = "/a-share/special-data/limit-break-pool"
INDEX_SNAPSHOT_PATH = "/a-share-index/prices/snapshot"
MARKET_SNAPSHOT_PATH = "/a-share/prices/snapshot"

# ── 分组（界面上每组一个小标题 + 条目网格）──
#: 每组配一个配置键、一个展示用组名、一个结果字典里的键。
#: 顺序 = 界面显示顺序；**某组配置为空时整组（连标题）隐藏**（不留一个空的"情绪："）。
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("market_indices", "宽基", "indices"),
    ("market_sentiment_indices", "情绪", "sentiment"),
    ("market_sector_indices", "板块", "sector"),
)

#: 概览的总行数：前两行固定（摘要 / 涨跌家数），之后每组一行。
#: `lines()` 按它出文本、`--cli --market` 与测试按它断言；
#: 界面**不再**按它建 5 个 QLabel —— 现在是"KPI 卡片 + 每组一个条目网格"。
LINE_COUNT = 2 + len(GROUPS)

#: 宽基那一组在结果字典里的键（`wide_specs()` 只对它有额外处理，见那里的说明）。
WIDE_GROUP_KEY = "indices"

#: 底部那行小字的固定前缀（数据来源与刷新节奏，界面与命令行口径一致）。
#:
#: 为什么不说死"同花顺"：概览取数的来源随用户的 `data_sources` 与"有没有配 Key"而变
#: （分发版的主源是**免 Key 的公开行情源**，同花顺是备用/增强）。写死一个名字，
#: 等于在用户面前说假话 —— 页脚是**唯一**告诉用户"这些数字哪来的"的地方。
FOOTER_PREFIX = "数据来源：同花顺金融数据服务（配置 Key 时）与公开行情接口（腾讯/新浪/东财）；非交易所授权行情"

#: 沪/深成交额的取数口径：这两个指数的 turnover 就是两市成交额
SH_TURNOVER_CODE = "000001.SH"
SZ_TURNOVER_CODE = "399001.SZ"

# ── 排版分隔符（只给 `lines()` 拼命令行文本用；界面是"一个数一个控件"，不拼长文本）──
#: 摘要行里两段之间（涨跌停家数 ｜ 沪深成交额）
_BLOCK_SEP = " ｜ "
#: 同一组里各条目之间（全角竖线，比逗号更不容易和数据混在一起）
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

# ── A 股习惯配色：涨=红、跌=绿、平/缺=默认色（**不要**欧美的绿涨红跌）──
#: 全仓**只有这一处**定义涨跌颜色：界面与测试都从这里取（`app.py` 里不许再写死色值，
#: 否则哪天调色调一半会出现"界面显示的和测试断言的"是两种红）。
COLOR_UP = "#d32f2f"
COLOR_DOWN = "#2e7d32"
#: 平盘/缺数据 = 不上色（用界面默认前景色）—— 空串比 None 好：调用方直接 f"color:{c}"
#: 前判一下真值就行，不用再区分两种"没有颜色"
COLOR_FLAT = ""

#: 缺数据时的占位符（界面与命令行统一用它，免得两处一个用 "-" 一个用 "N/A"）
DASH = "—"

#: 代码 → 中文名（**本地小表**：目录接口 `catalog/ths-index-list` 一个 tag 就返回几千行，
#: 而概览每 55 秒刷一次 —— 为几个名字付几千行请求不值当）。
#: 表里没有的代码显示代码本身；想改成自己的口径，在配置里写 `代码=名称` 即可。
#: 注释里括号内是目录接口里的**官方名**，冒号前是卡片上显示的名字（短一些更耐看）。
INDEX_NAMES: dict[str, str] = {
    # 宽基（默认那五个 + 用户 2026-09-17 要求补上的两只，见 WIDE_INDEX_EXTRA_CODES）
    "000001.SH": "上证",            # 上证指数
    "399001.SZ": "深成",            # 深证成指
    "399006.SZ": "创业板",          # 创业板指
    "000688.SH": "科创50",          # 科创50
    "000300.SH": "沪深300",         # 沪深300
    "000016.SH": "上证50",          # 上证50
    "000852.SH": "中证1000",        # 中证1000
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
    # 实测可用但**没有**进宽基默认列表的（用户原话里的"科创板"已经由 000688.SH 覆盖，
    # 见 `WIDE_INDEX_EXTRA_CODES` 的说明）：想用就在配置里写 `000680.SH`
    "000680.SH": "科创综指",        # 上证科创板综合指数（腾讯实测可返回）
}

#: 宽基组**必须有的两只**（用户 2026-09-17 说"要加上证50、中证2000、科创板"）。
#:
#: 为什么是这两只、为什么少了中证2000（**用户要的东西不许静默消失，所以写在这里**）：
#: - 上证50 `000016.SH`：2026-09-17 实测可返回（腾讯 `qt.gtimg.cn/q=sh000016`
#:   回的是 `1~上证50~000016~2844.23~…`），加上；
#: - 中证2000：**拿不到**。同一天实测腾讯 `sh932000` 与 `sz932000` **都**只回
#:   `v_pv_none_match="1"`（= 这个代码在腾讯那边没有对应标的），所以按任务书换成
#:   同一天实测可用的**中证1000 `000852.SH`**（腾讯回 `1~中证1000~000852~7548.82~…`）——
#:   它是小盘口径里最接近中证2000 的可用指数，而不是"随便换一个"；
#: - "科创板"：宽基列表里的 `000688.SH` **科创50** 就是它（保留）。同一天实测还有
#:   `000680`（科创综指，腾讯回 `1~科创综指~000680~1887.34~…`），但它既不是用户
#:   原话里的口径，也没有在同花顺指数快照端点（本项目实际取数用的那一个）上验证过，
#:   所以**不硬加** —— 想加的用户在 config.toml 的 `market_indices` 里写 `000680.SH` 即可
#:   （名字已经在 `INDEX_NAMES` 的"备选口径"里备着）。
WIDE_INDEX_EXTRA_CODES: tuple[str, ...] = ("000016.SH", "000852.SH")

#: 试过但**实测拿不到**的候选（代码 → 为什么没用它）。
#:
#: 为什么要把这件事写成**数据**而不是只写在注释里：用户点名要的东西不许静默消失 ——
#: 注释会被下一次重构顺手删掉，而这份字典有测试钉着（见 `tests/test_market.py` 的
#: `test_rejected_index_candidate_is_documented_not_silently_dropped`），
#: 谁想删就得先想清楚"用户要的中证2000 到底去哪儿了"。
REJECTED_INDEX_CANDIDATES: dict[str, str] = {
    "sh932000": "中证2000：2026-09-17 实测腾讯只回 v_pv_none_match=\"1\"（无对应标的），拿不到",
    "sz932000": "同上（换深市前缀也一样拿不到）",
    "000852.SH": "中证1000 —— **换用了它**（同一天实测腾讯可返回）："
                  "它是小盘口径里最接近中证2000 的可用指数，见 WIDE_INDEX_EXTRA_CODES",
    "000680.SH": "科创综指：实测腾讯可返回，但用户原话里的「科创板」已由 000688.SH 科创50 覆盖，"
                 "而且它没有在同花顺指数快照端点上验证过，所以**不硬加**"
                 "（想用就在配置里写 000680.SH，名字已在 INDEX_NAMES 里）",
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


def wide_specs(cfg: Any) -> list[tuple[str, str]]:
    """宽基组最终要显示的条目 = **配置里的宽基 + 必须有的那两只**（`WIDE_INDEX_EXTRA_CODES`）。

    为什么要在这里"补"，而不是只改配置默认值（`config.DEFAULT_MARKET_INDICES`）：
    用户这次要求"宽基指数加三项"，而宽基的默认列表住在 `config.py`，
    **`tests/test_config.py` 正钉着那五只**（该文件不在本次改动范围内，不能改它的期望值），
    所以把"必须有"这件事落在取数层：只要用户在 `market_indices` 里配了宽基（非空），
    页面就一定带上上证50 与中证1000；用户在配置里写了自己的名字（`代码=名字`）时，
    配置里已有的代码**原样保留**（不覆盖、不重排），只把缺的补在末尾。

    边界（刻意保留的语义）：`market_indices = []`（空）仍然是"不显示这一块" ——
    空列表是"我不要这一块"的明确表态，这时候补两只上去等于不听用户的话
    （`test_market_page_hides_whole_block_when_config_is_empty` 钉的就是它）。
    """
    configured = parse_codes(getattr(cfg, "market_indices", None))
    if not configured:
        return []
    merged = list(configured)
    have = {thscode for thscode, _name in merged}
    for code in WIDE_INDEX_EXTRA_CODES:
        if code not in have:
            merged.append((code, INDEX_NAMES.get(code) or code))
    return merged


def _group_specs(cfg: Any) -> dict[str, list[tuple[str, str]]]:
    """三组指数各自要取的代码（宽基那一组过一道 `wide_specs()`）。"""
    return {
        key: (wide_specs(cfg) if key == WIDE_GROUP_KEY
              else parse_codes(getattr(cfg, source, None)))
        for source, _label, key in GROUPS
    }


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
    """全市场快照分页汇总：沪/深/北的成交额、成交量与涨跌家数（按交易所分桶）。

    成交额那一栏里现在只有涨跌家数会被显示（北交所成交额 2026-09-17 起不显示了），
    但三个交易所都照常分桶 —— 分页循环反正要跑一遍，多存两个数不花请求。

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
    except Exception as exc:  # noqa: BLE001 - 这一路失败只影响涨跌家数那几格
        _note(errors, f"全市场汇总取不到（涨跌家数会缺）：{_why(exc)}")
        return None
    if not exchanges:
        _note(errors, "全市场汇总返回了空数据（涨跌家数会缺）")
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
    return [key for key, specs in _group_specs(cfg).items() if specs]


def _fetch_groups(
    cfg: Any, client: Any, errors: list[str]
) -> tuple[dict[str, list[dict]], list[str]]:
    """取三组指数（宽基 / 情绪 / 板块）—— **同一个端点、同一批请求**，取回后按组切分。

    为什么三组要挤在一个请求里：它们本来就是同一个快照端点，分批发只是多花请求数
    （这几路每 55 秒就可能跑一次）；切分只按代码归属，互不影响。

    Returns:
        ({结果键: 条目列表}, 取不到的代码)。
    """
    specs = _group_specs(cfg)

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
    # 来源**明确不认**的代码（免 Key 源没有同花顺板块指数 `.TI` 这种标的）不当失败：
    # 它没写错，是这一路根本没有这种标的 —— 混进 `failed` 会让用户去改一个没错的代码。
    unsupported = {str(code) for code in ((result or {}).get("unsupported") or [])}
    # 服务端"请求成功但这只没返回"时也要算取不到：否则用户看到自己配的指数凭空少了
    # 一个，只会以为配置没生效（比报错更难查）
    returned = {str(row.get("thscode") or "") for row in rows}
    for thscode in codes:
        if thscode in unsupported:
            continue
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
        #: 这一趟是谁供的数（`"public"` = 免 Key 公开源；`"hithink"` = 同花顺）。
        #: 放在骨架里而不是只在 `fetch_overview` 里加：**每一条返回路径都要有这两个键**，
        #: 否则界面读到 None 会在"没 Key 直连骨架"那条路上把来源写错。
        "price_source": "",
        #: 涨跌停家数是不是 5 分钟一档（免 Key 路径是；同花顺那三个池子是 55 秒一档）
        "pools_slow": False,
    }
    for _, _, key in GROUPS:
        data[key] = []
    #: 哪几组在配置里存在（决定 `lines()` 要不要出这一行）
    data["configured_groups"] = list(configured or [])
    return data


def _public_client() -> Any:
    """免 Key 的"同花顺客户端替身"（腾讯/新浪；见 `data/public_market.py`）。

    这里**唯一**一处"没 Key 时换谁取数"的决定点：换成同一个形状的客户端之后，
    `_collect_overview` 一行都不用分叉 —— 否则就会出现"界面走一套、命令行走另一套"，
    而这个项目已经在"界面和命令行对不上"上踩过一次了。

    注意这里**不传** `force`：那个开关属于"概览要不要重取"，而扫描层有自己的 5 分钟
    缓存（见 `public_market.SCAN_TTL`），穿透下去会变成"每次刷新重扫全市场"。
    """
    return public_market.PublicMarketClient(
        timeout=MARKET_TIMEOUT, pace=public_market.SCAN_PACE,
    )


def _is_auth_problem(errors: list[str]) -> bool:
    """失败原因是不是"同花顺凭据不可用"这一类（可以改走免 Key 源的那种）。

    只看这一种：网络不通、服务端 500 之类换成公开源也一样拿不到，
    那种情况再扫一趟全市场只是白花 56 个请求。
    """
    text = "｜".join(str(e) for e in errors)
    return ("Key" in text) or ("未配置" in text) or ("无效" in text) or ("未就绪" in text)


def _note_public_limits(cfg: Any, errors: list[str]) -> None:
    """免 Key 路径下，把"哪几组没有公开等价物"如实说出来（不是报错，是口径说明）。

    为什么要写在 `errors` 里（界面 tooltip/命令行都会显示）：用户配了同花顺的板块指数
    （`881155.TI` 这种）却看到 `板块：—`，"数据没刷新/程序坏了"是最自然的推测 ——
    必须有个地方说明白"这一组公开源没有对应标的，配 Key 才有"。
    """
    missing: list[str] = []
    for source_attr, label, _key in GROUPS:
        specs = parse_codes(getattr(cfg, source_attr, None))
        if specs and all(str(code).upper().endswith(".TI") for code, _ in specs):
            missing.append(label)
    if missing:
        _note(errors, "免 Key 公开源没有这几组的对应标的（配同花顺 Key 才会显示）："
                      + "、".join(missing))


def _collect_overview(cfg: Any, client: Any, errors: list[str], *, signature: tuple,
                      force: bool) -> dict:
    """真正取一趟概览（三路各自降级）。`fetch_overview` 只管"用哪个客户端"。"""
    overview = _skeleton(configured=_configured_group_keys(cfg))
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
            # 北交所成交额：**算出来但不再显示**（2026-09-17 用户要求删掉这一格）。
            # 留着它是因为它只是同一趟分页汇总的副产品（不额外花请求），
            # 而且 `has_data()` 与将来"想恢复这一格"时还要用；界面/命令行都不画它。
            turnover["bj"] = (
                breadth["exchanges"].get("BJ", {}).get("turnover")
            )
    #: 显示用的成交额 = **沪 + 深**（用户 2026-09-17 要求合成一个数，不再分成三格）。
    #: 只取到一边时（例如用户把宽基里的深证删了）合计就是那一边的数 ——
    #: 这比显示 `—` 更贴近事实；两边都没有才是 None（界面画 `—`）。
    #: `kpi_values()` 的"成交额"读的就是这个键，界面与命令行因此不会分叉。
    available = [v for v in (turnover["sh"], turnover["sz"]) if v]
    turnover["total"] = sum(available) if available else None

    if not has_data(overview):
        # 一项都没取到 → **不写"取数时间"**。`as_of` 是给用户判断"这个数有多新"用的：
        # 明明什么都没有、页脚却显示"更新于 09:13"，比空白更糟 —— 它让人以为这是
        # 刚取到的数据。（页脚本来就支持 `—` 这个写法，见 `footer_text`。）
        overview["as_of"] = ""
    return overview


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

    2026-09-17（免 Key 分发版）：**没配同花顺 Key 时不再返回空骨架**，而是改走免 Key
    的公开源（腾讯/新浪）。这样"装上就能看大盘"这条承诺在概览页成立；
    页脚与 tooltip 里的数据来源、以及哪几项是 5 分钟一档，都跟着一起变（见
    `footer_text`），不会出现"数字是新的、说明是旧的"。
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
    public_path = False
    if client is None:
        try:
            client = _make_client(cfg)
        except Exception as exc:  # noqa: BLE001 - 没 Key 是最常见的情况：改走公开源
            logger.info(f"大盘概览：无同花顺凭据（{_why(exc)}），改走免 Key 公开源")
            errors.append("未配置同花顺 Key：大盘概览走免 Key 公开行情源（腾讯/新浪）")
            client = _public_client()
            public_path = True

    overview = _collect_overview(cfg, client, errors, signature=signature, force=force)

    # 凭据在"构造时没报错、请求时才报错"的情形（Key 写错/过期/服务端未就绪）也要能落到
    # 公开源：否则用户看到的就是一整页 `—` 加一句"Key 无效"，而他明明可以免 Key 看盘。
    if not public_path and not has_data(overview) and _is_auth_problem(errors):
        logger.info("大盘概览：同花顺凭据不可用，改走免 Key 公开源重取一次")
        retry_errors: list[str] = [
            "同花顺凭据不可用，本次已改走免 Key 公开行情源（腾讯/新浪）："
            + "；".join(errors)
        ]
        client = _public_client()
        overview = _collect_overview(cfg, client, retry_errors,
                                     signature=signature, force=force)
        errors = retry_errors
        public_path = True

    #: 这一趟是谁供的数（页脚文案、tooltip、测试都读它）。不写进结果的话，
    #: 用户没法知道"我加了 Key 到底有没有生效"，我们也没法在测试里钉住这件事。
    overview["price_source"] = "public" if public_path else "hithink"
    #: 免 Key 路径下 涨停/跌停/炸板 与 涨跌家数 **都是同一趟 5 分钟缓存的扫描**，
    #: 所以页脚要改口说"这几项 5 分钟更新"（`footer_text`），否则就是在骗人。
    overview["pools_slow"] = bool(public_path)
    if public_path:
        _note_public_limits(cfg, errors)
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


def entry_fields(item: dict) -> tuple[str, str, str]:
    """一个指数条目 → `(名称, 点位文本, 涨跌幅文本)`（缺哪段哪段是 `—`）。

    为什么拆成三个字段而不是直接给一行文本：界面上每一个指数是一个**独立控件**，
    里面"名称（灰）/ 点位（右对齐）/ 涨跌幅（右对齐）"三列各是一个 QLabel ——
    点位与涨跌幅要**各自**按自己的涨跌上色（见 `value_color()`），
    一行文本做不到这件事（一个 QLabel 只有一种颜色）。
    """
    name = str(item.get("name") or item.get("thscode") or DASH)
    if item.get("last") is None:
        # 没点位就不报涨跌幅：只报一个"涨跌幅"而点位是 `—`，会让人以为拿到的是一半数据
        return name, DASH, DASH
    return name, _price_text(item.get("last")), _pct_text(item.get("change_pct"))


def _entry_text(item: dict) -> str:
    """一个指数/情绪条目 → `上证 3885.33 -0.07%`（缺哪段就把哪段显示成 `—`）。

    实现上就是 `entry_fields()` 三段的拼接 —— 命令行（`lines()`）与界面
    （"名称｜点位｜涨跌幅"三个 QLabel）共用同一份字段口径。
    """
    name, value, pct = entry_fields(item)
    if value == DASH:
        return f"{name} {DASH}"
    return f"{name} {value} {pct}"


def kpi_values(overview: dict | None) -> dict[str, str]:
    """KPI 区的**每一个**数值（界面一张卡一个数；`lines()` 再把它们拼成一行）。

    为什么要有这一层：界面上"涨停 55"是**一张卡片**里的两个控件（小号灰标签 + 大一号加粗数值），
    不是一长串文本里的一段；长文本靠自动换行折出来的行对不齐，一眼就不专业。
    键的顺序就是界面上的显示顺序（成交额一张，涨停/跌停/炸板 一组，涨跌家数 一组）。

    2026-09-17 改版（用户要求）：
    - `成交额` = **沪 + 深**（`turnover["total"]`），**一个数**；原来的
      `沪成交额` / `深成交额` / `北成交额` 三个键与"沪 … 深 … 北 …"那句话一起删掉；
    - 北交所成交额**不再显示**（数据还在 `turnover["bj"]` 里，见 `fetch_overview`）。
    键数因此从 9 降到 7，界面一行正好摆 7 个小条目。
    """
    data = overview or {}
    limits = data.get("limits") or {}
    turnover = data.get("turnover") or {}
    breadth = data.get("breadth") or {}
    return {
        # 成交额放在最前：这一行是"钱 + 情绪"，先看钱（用户把这一块叫「成交与情绪」）
        "成交额": _amount_text(turnover.get("total")),
        "涨停": _count_text(limits.get("up")),
        "跌停": _count_text(limits.get("down")),
        "炸板": _count_text(limits.get("break")),
        "上涨": _count_text(breadth.get("up")),
        "下跌": _count_text(breadth.get("down")),
        "平盘": _count_text(breadth.get("flat")),
    }


def lines(overview: dict | None) -> list[str]:
    """概览页文本（`--cli --market` 用；界面用的是 `kpi_values()` / `entry_fields()`）。

    下标固定对应：
        [0] 摘要：`涨停 N · 跌停 N · 炸板 N ｜ 成交额 N亿`
        [1] 涨跌家数：`上涨 N · 下跌 N · 平盘 N`
        [2] 宽基  [3] 情绪  [4] 板块   ← 顺序就是 `GROUPS` 的顺序

    成交额是**沪 + 深 一个数**（2026-09-17 用户要求），所以摘要行里不再有"沪 … 深 … 北 …"。
    **配置为空的那一组返回空串**（命令行跳过它；界面上是整组连标题一起隐藏）——
    不留一个空的"情绪："吊在那里。拼不出来的段位一律 `—`：宁可让用户看到"这里没数"，
    也不显示 0 或空白（0 家涨停和"没取到"是完全不同的两件事）。
    `market_breadth` 关掉时涨跌家数是 `—`（它只在打开时才取）。
    """
    data = overview or {}
    values = kpi_values(overview)
    summary = (
        f"涨停 {values['涨停']} · 跌停 {values['跌停']} · 炸板 {values['炸板']}"
        + _BLOCK_SEP
        + f"成交额 {values['成交额']}"
    )
    counts = (
        f"上涨 {values['上涨']} · 下跌 {values['下跌']} · 平盘 {values['平盘']}"
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

    为什么把"涨跌家数每 5 分钟更新"也写在这里：这一项（全市场快照）明显比指数慢一档，
    不写清楚会被当成"数据没刷新/坏了"。
    2026-09-17 起这句话里**不再提成交额**：界面上的成交额已经是"沪+深"（取自两只宽基指数
    的 turnover，与指数同一个节拍、每分钟刷新），跟 5 分钟一档的全市场快照没关系了 ——
    把它留在这句话里就是错的。
    """
    data = overview or {}
    as_of = str(data.get("as_of") or "") or DASH
    tail = "每分钟自动刷新"
    # 哪几项是"慢一档"的，必须**逐项说清**：免 Key 路径下 涨跌停家数 与 涨跌家数
    # 都是同一趟 5 分钟缓存的全市场扫描（56 个请求，不可能每分钟重扫一遍，
    # 那既吃不消也会被限流）；同花顺路径下 涨跌停家数 是 3 个池子请求、跟着 55 秒走。
    slow: list[str] = []
    if data.get("pools_slow"):
        slow.append("涨跌停家数")
    if data.get("breadth_enabled"):
        slow.append("涨跌家数")
    if slow:
        tail += f"；{'、'.join(slow)}每 5 分钟更新"
    return f"{FOOTER_PREFIX} · 更新于 {as_of}（{tail}）"


def value_color(change_pct: Any) -> str:
    """**单个**涨跌幅 → 颜色（`""` = 用界面默认色）。

    为什么按值而不是按组：用户明确要求"既然涨跌分颜色了，那情绪/板块的数字和涨跌幅
    也分一下颜色"。情绪与板块那几组**经常同时有涨有跌**，而一个"整组一行"的控件
    只有一种颜色 —— 按组取色必然出现"要么全都染成红、要么全都不染"，
    两种情况都在骗人。界面改成"每个指数一个条目控件"之后，点位与涨跌幅各自
    按自己的涨跌上色：涨=红、跌=绿、平盘与没取到=默认色。
    """
    number = _to_float(change_pct)
    if number is None or number == 0:
        # 平盘（0.00%）与"没取到"（None）都不上色：A 股看盘里 0 既不算涨也不算跌
        return COLOR_FLAT
    return COLOR_UP if number > 0 else COLOR_DOWN


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

# ── 雪球个股页地址（**与通知无关**：界面右键"打开雪球"用它）──
#
# 2026-09-18：Windows 通知那一整路被删掉了（用户原话："windows系统通知删除，
# 太骚扰了，影响体验"），但这两个函数还活着 —— 它们的另一个用户是主窗口里
# "右键某只票 → 打开雪球"（`ui/app.py`）。删 `notify/windows.py` 时顺手搬到这里，
# 免得 `ui/app.py` 为了一个纯字符串拼装去 import 一个通知模块。


def to_xueqiu_code(code: str) -> str:
    """纯数字代码 → 雪球格式：6 开头→SH，4/8 开头→BJ，其余→SZ（与服务器版一致）。"""
    code = (code or "").strip()
    if code.startswith("6"):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def xueqiu_url(symbol: str) -> str:
    """纯数字代码 → 雪球个股页地址；**不是 6 位数字就返回空串**。

    为什么单独一个函数（而不是各处自己拼 `f"https://xueqiu.com/S/{...}"`）：
    界面里"右键表格里的名称(代码) → 打开个股页"用它，将来还会有别的地方要用；
    各拼一遍的话，改域名（雪球换过路径）必然漏掉一处。

    **校验放在这里**：界面上点的是表格单元格，而单元格里可能是空行/一串说明文字
    （理论上不该有，但"点一下没反应"远好过"打开一个 `xueqiu.com/S/SZ` 的 404 页"），
    所以调用方可以直接用空串判断"这一行不可跳转"。
    """
    code = (symbol or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return ""
    return f"https://xueqiu.com/S/{to_xueqiu_code(code)}"


def first_symbol(title: str, lines: list[str]) -> str:
    """从正文里挑出第一个可跳转的 6 位股票代码（标题里的数字不算）。"""
    import re

    for line in lines:
        match = re.search(r"\b(\d{6})\b", str(line))
        if match:
            return match.group(1)
    return ""


def toast_icon() -> str:
    """Toast 左上角的小图标：**必须是绝对路径**（winotify 会把它交给系统去读盘，
    相对路径在打包后的工作目录里会找不到）。

    找不到图标就返回空串 —— winotify 用默认图标，不影响弹窗。
    """
    try:
        from laoa_trader import assets

        path = assets.icon_png(size=48) or assets.icon_png()
    except Exception:  # noqa: BLE001 - 图标是"锦上添花"，绝不该拖垮通知
        return ""
    return str(path.resolve()) if path else ""


def notify(
    title: str,
    lines: list[str],
    cfg: Any = None,
    duration: Any = None,
) -> dict:
    """弹一条 Windows 原生通知。

    频道参数（config.toml）：
        - `notify_windows_sound`：是否带提示音（默认 True；开会/盯盘时可关掉）；
        - `notify_windows_open_url`：点击按钮是否打开雪球（默认 True）。

    Args:
        title: 通知标题。
        lines: 正文行（**最多取前 8 行**，多了系统会截断）。
        cfg: 配置。
        duration: 期望的展示时长。可以是 `"short"` / `"long"`，也可以是
            `"10s"` / `"8000ms"` / `25` 这类写法 —— 经 `normalize_duration()`
            换算成 winotify 认的档位（**它只认 short/long 两个值**）。

    Returns:
        {"kind": "windows", "ok": bool, "detail": str}
        非 Windows 平台返回 `ok=False, supported=False`（**调用方不应视为错误**）。
    """
    # 配置开关先判：用户主动关掉时报"已关闭"，比报"平台不支持"更准确
    if cfg is not None and not getattr(cfg, "notify_windows", True):
        return {"kind": "windows", "ok": True, "skipped": True, "detail": "已在配置中关闭"}
    if not SUPPORTED:
        return {"kind": "windows", "ok": False, "supported": False,
                "detail": "当前平台不支持 Windows 原生通知"}

    try:
        from winotify import Notification, audio  # type: ignore[import-not-found]
    except ImportError as exc:
        # 不是错误：非 Windows 或用户没装 winotify，静默降级
        logger.info(f"winotify 不可用，跳过 Windows 通知：{exc}")
        return {"kind": "windows", "ok": False, "supported": False,
                "detail": "未安装 winotify（非 Windows 平台正常现象）"}

    with_sound = bool(getattr(cfg, "notify_windows_sound", True)) if cfg is not None else True
    with_url = bool(getattr(cfg, "notify_windows_open_url", True)) if cfg is not None else True
    # 进 winotify 之前先归一化：它只认 "short"/"long"，别的值会直接抛异常
    shown_for = normalize_duration(duration)
    if str(duration).strip().lower() not in WINOTIFY_DURATIONS and duration is not None:
        logger.debug(f"通知时长 {duration!r} 已换算成 winotify 档位 {shown_for!r}")

    body = "\n".join(str(line) for line in lines[:8])  # Toast 正文太长会被系统截断
    try:
        toast = Notification(
            app_id=APP_ID, title=title, msg=body or "（无内容）", duration=shown_for,
            icon=toast_icon(),
        )
        symbol = first_symbol(title, lines)
        if with_url and symbol:
            toast.add_actions(
                label="打开雪球", launch=f"https://xueqiu.com/S/{to_xueqiu_code(symbol)}"
            )
        if with_sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
        detail = "已弹出通知" + ("" if with_sound else "（静音）")
        return {"kind": "windows", "ok": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - 弹窗失败不该影响其它频道
        logger.warning(f"Windows 通知失败：{exc}")
        return {"kind": "windows", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
