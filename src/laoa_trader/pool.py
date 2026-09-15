"""每日精选股票池：**策略标的 + 自选股**，两类成员一起进池、一起盯。

池子成员有两类
--------------
1. **策略标的**：启用组 → 候选 → 热门行业过滤 → 权重打分 → 每策略≤3 → 取前 N（N=`size`）；
2. **自选股**（用户在界面/CLI 手动加的）：
   - **不受热门行业过滤**（自己选的，就是要盯）；
   - **不占策略名额**（`size` 只限制策略标的）；
   - 有自己的上限 `watchlist_max`（默认 20），超限会**提示**而不是静默丢弃；
   - 停用（`enabled=0`）的不进池、不监控，但保留在列表里。

去重：同一标的既是策略选中又是自选 → 池子里**只出现一行**，来源标成「策略+自选」。

原始说明（策略标的）

**移植自服务器版 `sequoia_x/pool.py`**（权重、单策略上限、热门行业过滤口径全部一致）。

为什么要有"池"
--------------
策略每天各推 30 只、五条策略加起来 150 只，**盯不过来也没法执行**。
这里把候选收敛成一个 10 只左右的小池子：

1. 只取**通过 10 年样本检验**的 5 条超短策略（见 `strategy/rules.py`）；
2. 按"证据强度"给权重（t 值越高、样本越长，权重越大）：
   低价股 3 / 连板回踩 2 / 短期反转 2 / 地量放量 2 / 首板缩量 1；
3. 每条策略先按自己的因子排序，再按权重折算成分数，**同一策略最多进 3 只**；
4. 只保留**热门行业**的候选（当日行业涨停密度 + 近 5 日行业成分等权涨幅，归一化后取前 12）；
5. 结果落库 `stock_pool`，盘中提醒直接盯这个池子。

> 这些策略的 α 都只有 0.1~0.5%，**远小于盘中波动**，所以池子只是"值得盯的清单"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from laoa_trader.config import get_config
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger
from laoa_trader.strategy import groups, rules

logger = get_logger(__name__)

#: 入选策略（类名 → 权重）。权重按 10 年样本的 t 值与样本长度定，不是拍脑袋。
#: **由 `strategy/groups.py` 派生**（策略 → 权重 → 分组只有一个来源，避免两处漂移）：
#:   低价股 3（四个持有期全显著）/ 连板回踩 2（T+2 t=2.02）/ 短期反转 2 / 地量放量 2 / 首板缩量 1
POOL_STRATEGIES: dict[str, int] = dict(groups.STRATEGY_WEIGHTS)

#: 单条策略在池子里的最大占比（避免一个策略占满 10 只）
MAX_PER_STRATEGY = 3

DEFAULT_SIZE = 10


def build_pool(
    engine: DataEngine,
    settings: Any = None,
    size: int = DEFAULT_SIZE,
    hot_only: bool = True,
    *,
    save: bool = True,
    day: str | None = None,
    picks: dict[str, list[dict]] | None = None,
    selection: groups.Selection | None = None,
    watchlist: list[dict] | None = None,
    report: dict | None = None,
) -> list[dict]:
    """跑入选策略并合成当日股票池。

    Args:
        engine: DataEngine。
        settings: 配置对象（透传）。
        size: 池子大小。
        hot_only: 是否只保留热门行业（用户要求：池子只选热门行业，数量少、盯得过来）。
        save: 是否落库 `stock_pool`。
        day: 池子日期，默认按库里最新行情日期。
        picks: 已算好的候选（`{策略类名: [...]}`）。**日更流程传它以避免重复跑策略** ——
            每条策略都要扫全市场面板，跑两遍会白白多花一倍时间。
        selection: 启用的组/策略；只从其中的候选合成池子（自选策略组的落点）。
        watchlist: 自选股行（`{symbol,name,note,enabled}`）；None 时按配置从库里读
            （`watchlist_in_pool=false` 表示"只记录不监控"，此时会跳过）。
        report: 可选的可变字典，用来接收"自选股超上限被截掉几只"之类的提示
            （返回类型保持 list，避免破坏现有调用方）。

    Returns:
        池子行列表（见 `build_pool_from_picks`）。
    """
    if picks is None:
        # 建池时把候选放宽：策略默认只取前 30，再叠加"仅热门行业"会剩不下几只，
        # 因此这里临时把 top_n 拉大到 200（只影响建池，不影响主流程的信号与推送）。
        run_kwargs: dict = {"top_n": 200, "names": engine.get_stock_names()}
        if selection is not None:
            run_kwargs["selection"] = selection
        picks_by_strategy, errors = rules.run_all(engine, settings, **run_kwargs)
        for msg in errors:
            logger.warning(f"池子策略执行失败：{msg}")
    else:
        picks_by_strategy = picks

    # 双保险：即使调用方传进来的候选里混了未启用的策略，也不让它进池子
    if selection is not None and not selection.empty:
        allowed = set(selection.strategies)
        dropped_strategies = [k for k in picks_by_strategy if k not in allowed]
        if dropped_strategies:
            logger.info(f"按选择过滤候选：剔除未启用策略 {dropped_strategies}")
        picks_by_strategy = {k: v for k, v in picks_by_strategy.items() if k in allowed}

    # 只保留热门行业的候选
    if hot_only:
        hot = hot_industries(engine.db_path)
        if hot:
            industry_of = engine.get_industry_map()
            kept: dict[str, list[dict]] = {}
            dropped = 0
            for strategy, candidates in picks_by_strategy.items():
                kept[strategy] = []
                for pick in candidates:
                    industry = industry_of.get(pick["symbol"])
                    if industry in hot:
                        kept[strategy].append(
                            {**pick, "reason": f"{pick['reason']}·热门行业{industry}"}
                        )
                    else:
                        dropped += 1
            picks_by_strategy = kept
            logger.info(
                f"热门行业过滤：保留 {sum(len(v) for v in kept.values())} 只、"
                f"剔除 {dropped} 只（热门行业：{'、'.join(list(hot)[:6])}）"
            )
        else:
            logger.warning("热门行业为空（缺行业/涨停/历史数据），本次不做过滤")

    pool = build_pool_from_picks(picks_by_strategy, size=size)
    logger.info(f"策略标的合成完成：{len(pool)} 只")

    # ── 自选股：独立上限、不占策略名额、不受热门行业过滤 ──
    pool = merge_watchlist(engine, pool, settings=settings, watchlist=watchlist,
                           report=report)
    logger.info(f"股票池合成完成：{len(pool)} 只"
                f"（策略 {sum(1 for r in pool if r.get('strategy'))} 只 / "
                f"自选 {sum(1 for r in pool if r.get('source') in ('自选', '策略+自选'))} 只）")
    if save and pool:
        day = day or engine.get_latest_data_date() or datetime.now().strftime("%Y-%m-%d")
        save_pool(engine.db_path, pool, day)
    return pool


def merge_watchlist(
    engine: DataEngine,
    pool: list[dict],
    *,
    settings: Any = None,
    watchlist: list[dict] | None = None,
    report: dict | None = None,
) -> list[dict]:
    """把自选股并进池子（去重、独立上限、来源标记）。

    - **去重**：既是策略选中又是自选 → 只留一行，`source` 标成「策略+自选」；
    - **独立上限**：`watchlist_max` 只截自选，超出的部分**提示**（`report["warnings"]`）；
    - **顺序**：策略标的（按分数降序）在前，纯自选（按加入时间）在后。
    """
    cfg = settings if settings is not None else get_config()
    rows = watchlist
    if rows is None:
        if not getattr(cfg, "watchlist_in_pool", True):
            # 配置成"只记录不监控"：记录照留，但不进池、不盯
            for row in pool:
                row.setdefault("source", "策略" if row.get("strategy") else "自选")
            if report is not None:
                report.setdefault("watchlist", 0)
            return pool
        from laoa_trader.data import storage

        with storage.connect(engine.db_path) as conn:
            rows = storage.load_watchlist(conn, enabled_only=True)

    enabled = [r for r in rows if r.get("enabled", 1)]
    limit = max(int(getattr(cfg, "watchlist_max", 20) or 0), 0)
    kept = enabled[:limit] if limit else []
    dropped = len(enabled) - len(kept)

    if dropped:
        message = (
            f"自选股 {len(enabled)} 只超过上限 watchlist_max={limit}，"
            f"本次只监控前 {limit} 只，另有 {dropped} 只未纳入"
            f"（可在 config.toml 提高 watchlist_max，或把暂时不看的停用）"
        )
        logger.warning(message)
        if report is not None:
            report.setdefault("warnings", []).append(message)
            report["watchlist_dropped"] = dropped
    if report is not None:
        report.setdefault("watchlist", len(kept))

    known = {row["symbol"] for row in pool}
    merged: list[dict] = []
    for row in pool:
        row.setdefault("source", "策略")
        merged.append(row)
    for entry in kept:
        symbol = entry["symbol"]
        note = (entry.get("note") or "").strip()
        if symbol in known:
            # 去重：策略标的与自选是同一只 → 只留一行
            for row in merged:
                if row["symbol"] == symbol:
                    row["source"] = "策略+自选"
                    row["note"] = note
                    row["watchlist"] = True
                    break
            continue
        merged.append({
            "symbol": symbol,
            "name": entry.get("name") or symbol,
            "strategy": "",
            "strategies": "",
            "score": 0.0,
            "reason": "自选" + (f"（{note}）" if note else ""),
            "source": "自选",
            "note": note,
            "watchlist": True,
        })
    return merged


def build_pool_from_picks(
    picks_by_strategy: dict[str, list[dict]], size: int = DEFAULT_SIZE
) -> list[dict]:
    """由"每条策略的候选（已按因子排序）"合成股票池。

    Args:
        picks_by_strategy: {策略类名: [{"symbol","name","reason"}...]}，顺序即该策略内的优先级。
        size: 池子大小。

    Returns:
        池子行列表（按分数降序，同策略不超过 MAX_PER_STRATEGY 只）。
    """
    merged: dict[str, dict] = {}
    for strategy, picks in picks_by_strategy.items():
        weight = POOL_STRATEGIES.get(strategy, 1)
        for rank, pick in enumerate(picks[: MAX_PER_STRATEGY * 3], start=1):
            symbol = pick["symbol"]
            # 策略内排名越靠前分越高；再乘策略权重
            score = weight * (1.0 / rank)
            entry = merged.setdefault(
                symbol,
                {"symbol": symbol, "name": pick.get("name") or symbol,
                 "score": 0.0, "strategies": [], "reasons": []},
            )
            entry["score"] += score
            if strategy not in entry["strategies"]:
                entry["strategies"].append(strategy)
                if pick.get("reason"):
                    entry["reasons"].append(pick["reason"])

    # 同策略最多 MAX_PER_STRATEGY 只：按分数从高到低填
    ordered = sorted(merged.values(), key=lambda e: -e["score"])
    counts: dict[str, int] = {}
    pool: list[dict] = []
    for entry in ordered:
        primary = entry["strategies"][0]
        if counts.get(primary, 0) >= MAX_PER_STRATEGY:
            continue
        counts[primary] = counts.get(primary, 0) + 1
        pool.append(
            {
                "symbol": entry["symbol"],
                "name": entry["name"],
                "strategy": primary,
                "strategies": ",".join(entry["strategies"]),
                "score": round(entry["score"], 4),
                "reason": "；".join(entry["reasons"])[:200],
            }
        )
        if len(pool) >= size:
            break
    return pool


def hot_industries(
    db_path: str,
    top: int = 12,
    momentum_window: int = 5,
    day: str | None = None,
) -> dict[str, dict]:
    """当前"热门行业"：**当日涨停密度** + **近 N 日行业成分等权涨幅**两个信号取并集。

    为什么用这两个（与服务器版同款理由）：
    - 涨停密度 = 行业内涨停家数 ÷ 行业内股票数 —— 直接反映资金当天在打哪个板块，
      **盘中就能算**（实时涨停池不断更新），响应最快；
    - 行业成分等权涨幅 —— 反映板块的中期强弱，避免只追一天的脉冲。
      服务器版注释里说明过：`index_daily` 里的行业指数要按名称对齐需要映射表，
      这里用"行业成分股等权涨幅"近似 —— 覆盖全、口径一致、且不依赖额外映射。

    Returns:
        {行业名: {"score": 综合分, "limit_up": 当日涨停家数, "density": 密度, "mom": 近N日涨幅}}
    """
    with storage.connect(db_path) as conn:
        # 行业成分数（分母）
        sizes: dict[str, int] = {}
        for industry, count in conn.execute(
            "SELECT industry, COUNT(*) FROM stock_basic "
            "WHERE industry IS NOT NULL AND industry != '' GROUP BY industry"
        ):
            sizes[industry] = count
        if day is None:
            latest = conn.execute("SELECT MAX(date) FROM limit_up_pool").fetchone()[0]
        else:
            latest = day
        if not latest:
            return {}
        limit_counts: dict[str, int] = {}
        for industry, count in conn.execute(
            "SELECT b.industry, COUNT(DISTINCT l.symbol) FROM limit_up_pool l "
            "JOIN stock_basic b ON b.symbol = l.symbol "
            "WHERE l.date = ? AND b.industry IS NOT NULL AND b.industry != '' "
            "GROUP BY b.industry",
            (latest,),
        ):
            limit_counts[industry] = count
        # 行业动量：用"行业成分股等权涨幅"近似（覆盖全、口径一致）
        bars = conn.execute(
            "SELECT b.industry, d.symbol, d.date, d.close FROM stock_daily_hfq d "
            "JOIN stock_basic b ON b.symbol = d.symbol "
            "WHERE d.date >= date(?, ?) AND b.industry IS NOT NULL AND b.industry != ''",
            (latest, f"-{momentum_window * 3} day"),
        ).fetchall()
    series: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for industry, symbol, date, close in bars:
        series.setdefault((industry, symbol), []).append((date, close))
    industry_returns: dict[str, list[float]] = {}
    momentum: dict[str, float] = {}
    for (industry, _symbol), points in series.items():
        points.sort()
        closes = [p[1] for p in points if p[1]]
        if len(closes) <= momentum_window:
            continue
        industry_returns.setdefault(industry, []).append(
            closes[-1] / closes[-1 - momentum_window] - 1
        )
    for industry, rets in industry_returns.items():
        momentum[industry] = sum(rets) / len(rets)

    # 打分：涨停密度排名 + 动量排名（都归一化到 0~1 后相加）
    def _norm(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        lo, hi = min(values.values()), max(values.values())
        if hi - lo < 1e-12:
            return {k: 0.5 for k in values}
        return {k: (v - lo) / (hi - lo) for k, v in values.items()}

    density = {
        ind: limit_counts.get(ind, 0) / max(sizes.get(ind, 1), 1) for ind in sizes
    }
    score = {}
    nd, nm = _norm(density), _norm(momentum)
    for industry in sizes:
        score[industry] = (nd.get(industry, 0) + nm.get(industry, 0)) / 2

    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:top]
    return {
        ind: {
            "score": round(sc, 4),
            "limit_up": limit_counts.get(ind, 0),
            "density": round(density.get(ind, 0.0), 4),
            "mom": round(momentum.get(ind, 0.0), 4),
        }
        for ind, sc in ranked
    }


def save_pool(db_path: str, pool: list[dict], day: str | None = None) -> int:
    """把股票池写入 `stock_pool`（幂等 upsert，同一天重复跑只会覆盖同代码的行）。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with storage.connect(db_path) as conn:
        written = storage.save_pool(conn, pool, day)
    logger.info(f"股票池已写入 {written} 只（{day}）")
    return written


def load_pool(db_path: str, day: str | None = None) -> list[dict]:
    """读取股票池：指定日期，或最近一次（供盘中提醒与界面使用）。"""
    with storage.connect(db_path) as conn:
        return storage.load_pool(conn, day)


def pool_symbols(db_path: str, day: str | None = None) -> list[str]:
    with storage.connect(db_path) as conn:
        return storage.pool_symbols(conn, day)


def pool_changed(db_path: str, pool: list[dict], day: str | None = None) -> bool:
    """池子相对该日期已有记录是否变化（非交易日重复跑时用来避免重复推送）。"""
    existing = {row["symbol"] for row in load_pool(db_path, day)}
    current = {row["symbol"] for row in pool}
    return existing != current


def format_pool_lines(pool: list[dict]) -> list[str]:
    """把池子整理成推送正文行。

    策略标的保持与服务器版同款格式（`1. 名称（代码）LowPrice｜理由`）——
    自选标的没有策略名，改用「自选」+ 备注，避免出现空标签。
    """
    lines = []
    for i, row in enumerate(pool, start=1):
        tag = str(row.get("strategies") or "").replace("Strategy", "")
        note = (row.get("note") or "").strip()
        if not tag:
            tag = "自选" + (f"（{note}）" if note else "")
        elif row.get("source") == "策略+自选":
            # 既是策略选中又是自选：标出来，免得用户以为"我加的自选没生效"
            tag += "+自选" + (f"（{note}）" if note else "")
        lines.append(f"{i}. {row['name']}（{row['symbol']}）{tag}｜{row.get('reason') or ''}")
    return lines


def source_kind(row: dict, watch_entry: dict | None) -> str:
    """池子行的来源类型：`策略` / `自选` / `策略+自选`。

    自选状态由 `watchlist` 表现查（而不是存进 `stock_pool`）：
    这样用户今天加/停用自选，池子表格的"来源"立刻就对了，不用重跑建池，
    也不用给老库加列做迁移。
    """
    has_strategy = bool(row.get("strategy"))
    in_watch = watch_entry is not None and int(watch_entry.get("enabled", 1)) == 1
    if has_strategy and in_watch:
        return "策略+自选"
    if has_strategy:
        return "策略"
    if in_watch:
        return "自选"
    return "自选" if row.get("watchlist") else "策略"


def source_label(row: dict, watch_entry: dict | None) -> str:
    """界面「来源」列的文本：策略组名 / 自选 / 两者拼接。

    需求要求"别搞出两列重复信息"：所以组别与来源合成**一列**：
        策略标的      → `波段·T+10（T+10）`
        纯自选        → `自选`
        策略 + 自选   → `波段·T+10（T+10） + 自选`
    备注单独一列（自选备注是用户自己写的，值得单独看）。
    """
    strategy = row.get("strategy") or ""
    group_key = groups.group_of(strategy)
    parts: list[str] = []
    if strategy:
        label = groups.group_label(group_key) if group_key else rules.strategy_label(strategy)
        horizon = groups.group_horizon(group_key) if group_key else 0
        parts.append(f"{label}（T+{horizon}）" if horizon else label)
    if watch_entry is not None or row.get("watchlist"):
        parts.append("自选")
    return " + ".join(parts) if parts else "—"


def limit_up_annotations(db_path: str, day: str | None = None) -> dict[str, dict]:
    """今日涨停池的"几连板 + 涨停原因" → `{symbol: {"continue_day_text", "reason"}}`。

    为什么单独查一次、而不是并进 `load_pool` 的 SQL：股票池是**建池那一刻**的行，
    涨停池是当天实时/收盘后更新的；两者按 (date, symbol) 左右拼一次就够了。
    一次查询建全量映射（而不是每行一次查询）也是必须的 —— 池子十几行就变成十几次查询。
    """
    with storage.connect(db_path) as conn:
        if day is None:
            row = conn.execute("SELECT MAX(date) FROM limit_up_pool").fetchone()
            day = row[0] if row else None
        if not day:
            return {}
        return {
            str(r[0]): {
                "continue_day_text": str(r[1] or ""),
                "reason": str(r[2] or ""),
            }
            for r in conn.execute(
                "SELECT symbol, continue_day_text, reason_type FROM limit_up_pool "
                "WHERE date = ?",
                (day,),
            )
        }


def limit_up_text(row: dict) -> str:
    """卡片/表格里那一行：`2 连板 · 半导体设备+业绩预增`。

    - 不是今日涨停票 → **空串**（调用方据此整行不显示）；
    - 没有原因就只显示连板数；没有连板数就只显示原因；两者都没有但确实在涨停池里 → `涨停`。
    """
    if not row.get("is_limit_up"):
        return ""
    parts = [str(row.get("continue_day_text") or "").strip(),
             str(row.get("limit_up_reason") or "").strip()]
    text = " · ".join(part for part in parts if part)
    return text or "涨停"


def pool_table_rows(db_path: str, day: str | None = None) -> list[dict]:
    """界面表格用：给每条池子记录补上行业、来源（组/自选）、自选备注与涨停信息。"""
    rows = load_pool(db_path, day)
    if not rows:
        return []
    limit_up = limit_up_annotations(db_path)
    with storage.connect(db_path) as conn:
        industries = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT symbol, industry FROM stock_basic WHERE industry IS NOT NULL"
            )
        }
        watch = storage.watchlist_map(conn)
    out = []
    for row in rows:
        strategy = row.get("strategy") or ""
        group_key = groups.group_of(strategy)
        entry = watch.get(row["symbol"])
        note = (entry or {}).get("note") or row.get("note") or ""
        out.append({
            **row,
            "industry": industries.get(row["symbol"], ""),
            "label": rules.strategy_label(strategy),
            # 组别（策略所属组）：推送文案与排序用它
            "group": group_key or "",
            "group_label": groups.group_label(group_key) if group_key else "—",
            "horizon": groups.group_horizon(group_key) if group_key else 0,
            # 来源：策略组名 / 自选 / 策略+自选（与组别合成一列）
            "source": source_kind(row, entry),
            "source_label": source_label(row, entry),
            "note": note,
            "watchlist_enabled": bool(entry and int(entry.get("enabled", 1)) == 1),
            # 今日涨停池里的信息（不在池里 → is_limit_up False，界面上整行不显示）
            "is_limit_up": row["symbol"] in limit_up,
            "continue_day_text": (limit_up.get(row["symbol"]) or {}).get("continue_day_text", ""),
            "limit_up_reason": (limit_up.get(row["symbol"]) or {}).get("reason", ""),
        })
    return out
