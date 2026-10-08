"""板块热力图的数据层：**全市场快照 → 按行业聚合 → 可以直接画出来的方块**。

为什么单独一个模块（2026-10-08 主人："把板块涨跌那块改成板块热力图"）
------------------------------------------------------------------
大盘概览原来那一块是"上涨前五 / 下跌前五两张表"：只有 10 行，而且**看不见中间那一大片**
（横盘的、微涨微跌的板块根本不上榜）。热力图一眼看全 90 个行业，面积给权重、颜色给涨跌。

口径（主人 2026-10-08 拍板，别自己改）
------------------------------------
* **面积 = 流通市值**（行业级取"行业流通市值合计"）。查过云图自己的页面文案，它用的就是市值：
  「方块面积代表**市值权重**」「· 面积代表**流通市值**」——不是成交占比。
* **颜色 = 红涨绿跌**，±10% 之外饱和。云图那段文案反而写的是"红跌绿涨"（国际惯例），
  我们按 A 股习惯来，不跟它。
* **拿不到数据的块画灰并标 `—`，绝不当 0**：停牌的票没有涨跌幅，当成 0% 会被画成"横盘"，
  那是假信息（与项目里"缺值不产生信号"同一条纪律）。

代价与刷新节奏（这条决定它能不能常驻在概览页上）
----------------------------------------------
全市场快照走东财 clist 分页：`ceil(5559/200)` = **28 个请求**（这个数写在同一处的注释里）。
所以它**不能**跟着大盘概览那套 60 秒节拍走，只能：
**TTL 默认 180 秒 + 手动【刷新】+ 页面不可见时一次都不拉**（判断在界面层，见 `ui/app.py`）。
拉回来的结果顺手落一份磁盘缓存：下次开程序第一屏就有图，并如实显示"这是几点的快照"。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 行业归属为空的票归到这一块：**不静默丢掉** —— 少了几十只票的市值，
#: 用户看到的面积分布就是错的，而且他无从知道。
UNKNOWN_INDUSTRY = "未分类"

#: 颜色饱和阈值（%）：±10% 之外一律用最深的那档色。
#: 为什么饱和而不是线性拉满更大范围：A 股一天 ±10% 已是涨跌停，绝大多数票在 ±5% 以内，
#: 线性映射会让"今天其实普涨"和"今天其实普跌"看起来一样淡。
SATURATE_PCT = 10.0

#: 磁盘缓存文件名（放在 `<数据目录>/cache/` 下）
CACHE_NAME = "industry_heatmap.json"

#: 缓存超过这个秒数就标"数据旧了"（与界面上的 TTL 是两回事：
#: TTL 决定"要不要去拉"，这个决定"要不要在界面上提醒"）
STALE_AFTER = 900


# ── 行业归属 ──


def industry_map(db_path: str | Path) -> dict[str, str]:
    """`{symbol: 行业}`（读本地 `stock_basic.industry`，不联网）。

    为什么用本地表而不是再问一次接口：行业归属是**日频**数据（同花顺那条同步链会写），
    热力图每几分钟刷一次没必要每次都问；而且本地表在自检里要求覆盖率 ≥90%
    （见 `data/preflight.py`），够用。
    """
    import sqlite3

    out: dict[str, str] = {}
    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            rows = conn.execute(
                "SELECT symbol, industry FROM stock_basic "
                "WHERE industry IS NOT NULL AND industry != ''"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.info(f"读行业归属失败（热力图会只剩一个「{UNKNOWN_INDUSTRY}」块）：{exc}")
        return out
    for symbol, industry in rows:
        out[str(symbol)] = str(industry).strip()
    return out


# ── 聚合 ──


def _num(value: Any) -> float | None:
    """宽松转 float；认不出来返回 None（**不是 0**，见模块头那条纪律）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def build_blocks(
    quotes: dict[str, dict],
    industry: dict[str, str],
    *,
    names: dict[str, str] | None = None,
) -> list[dict]:
    """全市场快照 + 行业归属 → 行业方块列表（市值降序）。

    Args:
        quotes: `{symbol: 快照}`；只认统一口径的两个键 —— `pct`（涨跌幅，%）与
            `circ_mktcap`（流通市值，**亿**），它们由 `sources.QUOTE_FIELDS` 保证。
            不去猜各家接口的原始字段名：那是 `sources`/`eastmoney` 的活。
        industry: `{symbol: 行业}`（见 `industry_map`）。
        names: `{symbol: 名称}`（可选，只用于 tooltip 里的"行业内最大的那只"）。

    Returns:
        每块：`{name, count, priced, mktcap, pct, symbols, leader, leader_pct}`。

        * `mktcap` = 该行业内**能取到市值**的票的流通市值合计（亿）—— 面积用它；
        * `pct` = **市值加权**涨跌幅（只用同时有市值与涨跌幅的票，**分母也只算这些票的市值**）；
          一只都算不出来时 `None`（界面画灰标 `—`，**不写 0%**）；
        * `priced` 与 `count` 不一样时说明有票没涨跌幅（停牌），tooltip 要如实说。
    """
    names = names or {}
    buckets: dict[str, dict] = {}
    for symbol, quote in (quotes or {}).items():
        code = str(symbol or "").strip()
        if not code or not isinstance(quote, dict):
            continue
        label = industry.get(code) or UNKNOWN_INDUSTRY
        bucket = buckets.setdefault(label, {
            "name": label, "count": 0, "mktcap": 0.0, "weighted": 0.0, "priced_mktcap": 0.0,
            "priced": 0, "symbols": [], "leader": "", "leader_pct": None,
            "_leader_cap": -1.0,
        })
        bucket["count"] += 1
        bucket["symbols"].append(code)
        cap = _num(quote.get("circ_mktcap"))
        pct = _num(quote.get("pct"))
        if cap is not None and cap > 0:
            bucket["mktcap"] += cap
            if pct is not None:
                bucket["weighted"] += cap * pct
                bucket["priced_mktcap"] += cap
                bucket["priced"] += 1
            if cap > bucket["_leader_cap"]:
                bucket["_leader_cap"] = cap
                bucket["leader"] = names.get(code) or code
                bucket["leader_pct"] = pct
        # 没有市值的票既不进面积、也不进加权涨跌幅（否则等于替它编了个权重）

    blocks: list[dict] = []
    for bucket in buckets.values():
        weighted = bucket.pop("weighted")
        priced_cap = bucket.pop("priced_mktcap")
        bucket.pop("_leader_cap", None)
        # 分母是**有涨跌幅那些票**的市值合计，不是整个行业的市值 —— 这两者不一样：
        # 停牌票有市值但**没有涨跌幅**，把它算进分母等于"按 0% 计进去"（300 亿 +10%
        # 与 100 亿停牌会算出 +7.5%），那是在替一只没交易的票编一个"没动"的结论。
        # 与模块头那条纪律一致：缺值不当 0。它的市值照样进面积（面积是"有多大"，
        # 与"今天涨没涨"是两件事），tooltip 里也会写明"有几只没有涨跌幅，未计入加权"。
        bucket["pct"] = (weighted / priced_cap) if priced_cap else None
        blocks.append(bucket)
    # 市值降序：布局算法（squarify）也要大的在前，省得再排一遍
    blocks.sort(key=lambda b: (-(b["mktcap"] or 0.0), b["name"]))
    return blocks


#: 深色主题下热力图的两端色（浅色主题那套见 `block_color` 里的字面量）。
#: 为什么必须分两档：浅色主题的横盘块是**近白**（#eeeeee），铺在深蓝底上就是一片发白的
#: 方块，"平静"反而变成全屏最亮的东西 —— 深色主题里横盘应当是**低调的深灰蓝**，
#: 涨跌两端才亮起来（与同花顺那种深色热力图一个观感）。
DARK_BASE = (32, 40, 56)          # 0%：低调的深灰蓝
DARK_UP = (232, 74, 84)           # +10% 及以上：亮红
DARK_DOWN = (46, 190, 120)        # -10% 及以下：亮绿
DARK_UNKNOWN = (90, 100, 118)     # 没数据：中性灰（比横盘块亮一点点，仍然分得开）


def block_color(pct: float | None, *, dark: bool = False) -> tuple[int, int, int]:
    """涨跌幅 → RGB（**红涨绿跌**，±`SATURATE_PCT` 饱和）。

    `None`（没数据）返回中性灰 —— 与"0%（横盘）"必须能分开：
    灰 = 不知道，0% = 真的没动。纯函数，用例直接打表核对。

    Args:
        pct: 涨跌幅（%），`None` = 没数据。
        dark: 深色主题（见 `theme.is_dark()`）。深色底换一套两端色与底色的**同构**配色：
            横盘是深灰蓝、涨跌两端是亮红亮绿 —— 判据完全一样，只是锚点不同，
            所以"0% 与 None 必须分得开"这条在任何主题下都成立。
    """
    if pct is None:
        return DARK_UNKNOWN if dark else (158, 158, 158)
    ratio = max(-1.0, min(1.0, float(pct) / SATURATE_PCT))
    if dark:
        base = DARK_BASE
        target, ratio = (DARK_UP, ratio) if ratio >= 0 else (DARK_DOWN, -ratio)
    else:
        base = (238, 238, 238)              # 0% 的近白
        if ratio >= 0:
            target, ratio = (214, 48, 40), ratio       # 红：涨
        else:
            target, ratio = (26, 145, 74), -ratio      # 绿：跌
    return tuple(int(round(base[i] + (target[i] - base[i]) * ratio)) for i in range(3))  # type: ignore[return-value]



def _luminance(rgb: tuple[int, int, int]) -> float:
    """相对亮度（WCAG 口径，0~1）。给"深色底上该用深字还是亮字"做判据用。"""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_color(pct: float | None, *, dark: bool = False) -> tuple[int, int, int]:
    """块内文字颜色：**跟着底色走**（字与底糊在一起是最糟的观感）。

    浅色主题：块越红/越绿，底越深 → 字越白；横盘块近白 → 深字。
    深色主题正好反过来：底色本身是深的 → 横盘块用亮字，涨跌两端亮起来 → 用**深字**。
    """
    if pct is None:
        # 灰块（不知道）：浅色底用深灰字，深色底用亮字
        return (232, 238, 248) if dark else (60, 60, 60)
    if dark:
        # 深色主题**按底色明暗**决定字色，而不是按涨跌幅档位：
        # 5% 那一档的红/绿其实还是暗的（(132,57,70)），那时用深字就糊了；
        # 只有接近饱和（±8% 往上）的块才亮到该配深字。
        rgb = block_color(pct, dark=True)
        return (10, 18, 32) if _luminance(rgb) >= 0.20 else (232, 238, 248)
    strong = min(1.0, abs(float(pct)) / SATURATE_PCT) >= 0.45
    return (255, 255, 255) if strong else (32, 32, 32)


def summarize(blocks: Sequence[dict]) -> dict:
    """给界面那行小字用的汇总：几块、合计市值、涨/跌/平/无数据各几块。"""
    return {
        "blocks": len(blocks),
        "mktcap": sum(float(b.get("mktcap") or 0.0) for b in blocks),
        "up": sum(1 for b in blocks if (b.get("pct") or 0) > 0),
        "down": sum(1 for b in blocks if (b.get("pct") or 0) < 0),
        "flat": sum(1 for b in blocks if b.get("pct") == 0),
        "unknown": sum(1 for b in blocks if b.get("pct") is None),
    }


def _source_label(source_id: str) -> str:
    """来源 id → 人话（界面那行小字里放不下注册表里的全名，把括号里的注解去掉）。"""
    from laoa_trader.data import sources

    info = sources.REGISTRY.get(str(source_id or "").strip().lower())
    if info is None:
        return str(source_id or "未知来源")
    return re.sub(r"（[^）]*）", "", info.name).strip() or info.name


def quotes_source_text(quotes: dict[str, dict]) -> str:
    """这批快照的**出处**（人话一行）；取不到就返回空串。

    为什么要说这件事：热力图这条路**价格与市值可能来自两个来源** —— 同花顺给价、
    公开源（腾讯）补市值（见 `sources.supplement_map`）。图上写着"红涨绿跌"却不写出处，
    用户没法判断"这块到底是哪个口径的数"。所以这里把两个来源都说出来。
    """
    price: dict[str, int] = {}
    fields: dict[str, int] = {}
    for quote in (quotes or {}).values():
        if not isinstance(quote, dict):
            continue
        main = str(quote.get("source") or "")
        if main:
            price[main] = price.get(main, 0) + 1
        for name, extra in (quote.get("field_source") or {}).items():
            if name in ("circ_mktcap", "turnover_rate") and extra:
                fields[str(extra)] = fields.get(str(extra), 0) + 1
    if not price:
        return ""
    main_id = max(price, key=lambda key: price[key])
    text = _source_label(main_id)
    others = [sid for sid in fields if sid != main_id]
    if others:
        text += "（市值由 " + "、".join(_source_label(sid) for sid in others) + " 补）"
    return text


# ── 磁盘缓存（跨次启动的第一屏） ──


def cache_path(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "cache" / CACHE_NAME


def save_cache(cfg: Any, blocks: Sequence[dict], *, at: float | None = None) -> Path | None:
    """落一份缓存（失败只记日志：热力图是"锦上添花"，不能因为它写不进去影响别的）。"""
    path = cache_path(cfg)
    payload = {"at": float(at if at is not None else time.time()),
               "blocks": [dict(b) for b in blocks]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path
    except OSError as exc:
        logger.debug(f"写热力图缓存失败（不影响使用）：{exc}")
        return None


def load_cache(cfg: Any) -> tuple[list[dict], float]:
    """读缓存 → `(blocks, 快照时间)`；没有/坏了就是 `([], 0)`。"""
    try:
        data = json.loads(cache_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], 0.0
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return [], 0.0
    return [b for b in blocks if isinstance(b, dict)], float(data.get("at") or 0.0)


def age_text(at: float, *, now: float | None = None) -> tuple[str, bool]:
    """快照时间 → `(「10:31」这种人话, 是否已过期)`。

    界面、tooltip、放大窗口三处都要说这句话，各处自己算迟早出现"一处说新、一处说旧"
    （这个项目上栽过一次同类的跟头，所以只留一个函数）。
    """
    if not at:
        return "还没有数据", True
    stamp = time.strftime("%m-%d %H:%M", time.localtime(at))
    return stamp, ((now if now is not None else time.time()) - at) > STALE_AFTER


def drawable_quotes(quotes: dict[str, dict]) -> dict[str, dict]:
    """只留"能画"的行：涨跌幅或流通市值至少有一项。

    滤掉的是"记录在、但两个字段都空"的（接口给了行、字段缺失），
    它们进聚合只会往 `未分类` 里塞一堆零权重块。
    """
    out: dict[str, dict] = {}
    for symbol, quote in (quotes or {}).items():
        if not isinstance(quote, dict):
            continue
        if _num(quote.get("pct")) is None and _num(quote.get("circ_mktcap")) is None:
            continue
        out[str(symbol)] = quote
    return out


def industry_extras(rows: Iterable[dict]) -> dict[str, dict]:
    """板块榜的行 → `{行业名: {limit_up, limit_down, main_net}}`，供热力图 tooltip 用。

    面积/颜色走全市场快照，但"涨停家数 / 跌停家数 / 主力净额"只有板块榜有
    （大盘概览**已经在拉**）—— 两个来源合起来用，多一个请求都不发。

    注意三行的**单位**跟着 `ui/app.py` 的 `sector_rank_tables` 走：两个家数是只数、
    `main_net` 是**元**（tooltip 显示前自己 ÷1e8 换成"亿"）。这里不换算：
    一个数在这一层改了单位，另一个调用方拿到的就不是他以为的那个量级。
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        name = str((row or {}).get("name") or "").strip()
        if not name:
            continue
        out[name] = {"limit_up": (row or {}).get("limit_up"),
                     "limit_down": (row or {}).get("limit_down"),
                     "main_net": (row or {}).get("main_net")}
    return out
