"""每日精选股票池：**策略标的 + 自选股**，两类成员一起进池、一起盯。

池子成员有两类
--------------
1. **策略标的**：启用组 → 候选 → 热门行业过滤 → 权重打分 → 每策略≤3 → 取前 N（N=`size`）；
2. **自选股**（用户在界面/CLI 手动加的）：
   - **不受热门行业过滤**（自己选的，就是要盯）；
   - **不占策略名额**（`size` 只限制策略标的）；
   - 有自己的上限 `watchlist_max`（默认 20），超限会**提示**而不是静默丢弃；
   - 停用（`enabled=0`）的不进池、不监控，但保留在列表里。

去重：同一标的既是策略选中又是自选 → 池子里**只出现一行**，来源写**那条策略名**
（2026-09-23 主人："只有用户自己输入的才能算自选来源" —— 所以不再拼「+自选」；
"它在不在自选里"由行上的 `watchlist` 标记说话，内部 `source` 仍记着 `公式+自选`）。

原始说明（策略标的）

**移植自服务器版 `sequoia_x/pool.py`**（权重、单策略上限、热门行业过滤口径全部一致）。

为什么要有"池"
--------------
候选一次能出一百多只，**盯不过来也没法执行**。这里把候选收敛成一个 10 只左右的小池子：

1. **候选只来自"勾选的公式"**（2026-09-18 用户要求）：原来这里并的是 5 条写在代码里的
   Python 策略（`strategy/rules.py`），用户把它们整体改成了**随包公式**（可改可删），
   于是公式成了唯一来源 —— 随包的那几条与用户自己写的一条待遇完全相同：勾上才跑；
2. 每条公式先按自己的因子/条件排好，再按权重折算成分数，**同一条公式最多进 3 只**
   （`formula_group.MAX_PER_FORMULA` / `FORMULA_WEIGHT`）；
3. 只保留**热门行业**的候选（当日行业涨停密度 + 近 5 日行业成分等权涨幅，归一化后取前 12）；
   **公式标的不过这道收敛**（条件是用户自己写明的，再被他没看见的行业过滤删掉最莫名其妙）；
4. 自选股独立并入（**不占公式名额、不受热门行业过滤**）；
5. 结果落库 `stock_pool`，盘中提醒直接盯这个池子。

> 这些策略的 α 都只有 0.1~0.5%，**远小于盘中波动**，所以池子只是"值得盯的清单"。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from laoa_trader import clock
from laoa_trader import wording
from laoa_trader.config import get_config
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger
from laoa_trader import legacy
from laoa_trader.strategy import formula_group
from laoa_trader.strategy.formula_group import is_formula_strategy

logger = get_logger(__name__)

#: 单条策略/公式在池子里的最大占比（避免一条占满 10 只）
MAX_PER_STRATEGY = 3

DEFAULT_SIZE = 10


def weight_of(strategy: str) -> int:
    """池子权重：公式用 `formula_group.FORMULA_WEIGHT`，其它一律 1。

    为什么公式也要权重：候选合成是按"策略内排名 × 权重"打分的，权重为 0 的话
    用户勾了公式也进不了池子（表现为"我勾了它，怎么一只都没有"）。

    2026-09-18：内置策略都改成了随包公式，所以"不是公式"的策略名只可能出现在
    老数据/老调用方里 —— 给 1 而不是报错。
    """
    if is_formula_strategy(strategy):
        return formula_group.FORMULA_WEIGHT
    return 1


def build_pool(
    engine: DataEngine,
    settings: Any = None,
    size: int = DEFAULT_SIZE,
    hot_only: bool = True,
    *,
    save: bool = True,
    save_picks: bool = True,
    day: str | None = None,
    picks: dict[str, list[dict]] | None = None,
    selection: Any | None = None,
    watchlist: list[dict] | None = None,
    report: dict | None = None,
) -> list[dict]:
    """跑入选策略并合成当日股票池。

    除了内置策略，这里还会**自动并入 `enabled_formulas` 里勾选的自定义公式**
    （作为与 short 等并列的「公式」组）—— 界面【开始选股】、定时日更、CLI 三条路
    都走这个函数，所以放在这里就不可能有哪条路"忘了带公式"。默认（没勾公式时）
    一次库都不读，行为与以前完全一致。

    Args:
        engine: DataEngine。
        settings: 配置对象（透传）。
        size: 池子大小。
        hot_only: 是否只保留热门行业（用户要求：池子只选热门行业，数量少、盯得过来）。
            **自定义公式不参与这道收敛**（条件本身就是用户写明的，见下面的说明）。
        save: 是否落库 `stock_pool`。
        save_picks: **选股结果（公式选出来的票）要不要一起落库**。
            2026-09-21 主人要求"策略选股结果改成不自动加入股池" —— 所以【开始选股】
            这条路传 `False`：`stock_pool` 里只留**自选股**（用户自己加的、以及在结果
            页面点【加入自选】加进来的），选出来的票只在结果页面显示，要不要留下由用户点。
            为什么不是整条建池都不做：`stock_pool` 同时是**盘中监控的盯盘清单**
            （`intraday` 读它），而自选股必须继续被盯着 —— 见 `merge_watchlist()`。
        day: 池子日期，默认按库里最新行情日期。
        picks: 已算好的候选（`{"公式·X": [...]}`）。调用方已经算过就直接传，
            免得再跑一遍（`run_enabled_formulas` 要扫全库，跑两遍纯浪费）。
        selection: **2026-09-18 起不再用于选股，只为兼容老调用方保留这个参数**。
            用户把"内置策略"整体改成了随包公式（可改可删），"跑哪些策略"这件事
            现在只有一个答案：`config.toml` 里 `enabled_formulas` 勾了哪几条公式 ——
            它由 `formula_group.run_enabled_formulas()` 自己读，不需要外面传选择。
        watchlist: 自选股行（`{symbol,name,note,enabled}`）；None 时按配置从库里读
            （`watchlist_in_pool=false` 表示"只记录不监控"，此时会跳过）。
        report: 可选的可变字典，用来接收"自选股超上限被截掉几只"之类的提示
            （返回类型保持 list，避免破坏现有调用方）。**公式组的运行结果与错误
            也写进它**（`report["formulas"]` / `report["errors"]`）。

    Returns:
        池子行列表（见 `build_pool_from_picks`）。
    """
    if picks is None:
        # ⚠️ 2026-09-18（用户要求）：候选**只来自"勾选的公式"**，不再是"跑内置策略"。
        # 原来这里调已删掉的 `rules.run_all()` 跑那 5 条写在代码里的 Python 策略；用户把它们
        # 整体改成了随包公式（可改可删），于是 5 条策略退出选股链路 ——
        # 下面那段 `formula_group.run_enabled_formulas()` 成了**唯一**的候选来源，
        # 随包公式与用户自己写的一条待遇完全相同（勾上才跑）。
        picks_by_strategy = {}
    else:
        picks_by_strategy = picks

    # ── 「公式」组：用户自己在「公式选股」页勾的自定义公式 ──
    #
    # 为什么放在这里（而不是让调用方先合并好）：**建池是唯一必须并入公式的地方** ——
    # 界面上的【开始选股】、定时任务、CLI 三条路都会走到 `build_pool`，
    # 放在这里就不可能出现"某一条路忘了带公式"（那种 bug 极难发现：
    # 用户勾了公式，手动建池有、定时建池没有）。
    #
    # `enabled_formulas` 为空时 `run_enabled_formulas` **一次库都不读**，
    # 所以"没用公式的人"与以前完全一样（默认不参与）。
    picks_by_strategy = dict(picks_by_strategy)
    formula_run = formula_group.run_enabled_formulas(engine.db_path, settings)
    for key, rows in formula_run.picks.items():
        picks_by_strategy.setdefault(key, [])
        picks_by_strategy[key] = list(rows)
    if formula_run.errors:
        # 失败隔离：公式出错只进 errors（状态栏/日志能看到原因），**不影响建池**
        if report is not None:
            report.setdefault("errors", []).extend(formula_run.errors)
        for message in formula_run.errors:
            logger.warning(message)
    if report is not None:
        report["formulas"] = {
            "ran": list(formula_run.ran),
            "picks": {k: len(v) for k, v in formula_run.picks.items()},
            "status": dict(formula_run.status),
            # **完整候选行也要留一份**：`scheduler.run_daily()` 从这里取候选写 `signal`
            # 表（盘中风控观察池用）。只留计数的话那边就没东西可写 —— 而 `signal`
            # 表的口径没变（每条公式最多取 SIGNAL_TOP_N 只，由调用方截断）。
            "rows": {k: [dict(r) for r in v] for k, v in formula_run.picks.items()},
        }

    # 兜底：调用方可能传进来"不是公式"的候选（老代码、老配置留下的策略类名）。
    # 2026-09-18 起那 5 条内置策略退出选股链路，所以这类键一律丢掉 ——
    # 让它进池子只会让「来源」列出现"策略·X"这种界面上已经选不出来的东西，
    # 用户看着它却找不到对应的策略行（那是最难解释的一种现象）。
    stale = [k for k in picks_by_strategy if not is_formula_strategy(k)]
    if stale:
        logger.info(f"丢弃不是自定义策略的候选（内置策略已改成随包策略）：{stale}")
        picks_by_strategy = {
            k: v for k, v in picks_by_strategy.items() if is_formula_strategy(k)
        }

    # 只保留热门行业的候选
    if hot_only:
        hot = hot_industries(engine.db_path)
        if hot:
            industry_of = engine.get_industry_map()
            kept: dict[str, list[dict]] = {}
            dropped = 0
            for strategy, candidates in picks_by_strategy.items():
                if is_formula_strategy(strategy):
                    # 公式**不参与热门行业收敛**：条件是这个用户自己写明的
                    # （他要"低价+缩量"就只想要低价的那些），再被我们的"热门行业"
                    # 删掉一半，表现就是他勾了公式却几乎看不到票，而且完全猜不到原因。
                    kept[strategy] = list(candidates)
                    continue
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
    # 日志的两个数与表头 `pool_counts()` 同口径：**互斥**（策略行 + 纯自选行 = 总数），
    # 否则会出现"共 13 只（策略 12 · 自选 12）"这种看着像算错的行。
    total, n_strategy, n_watch = pool_counts(pool)
    logger.info(f"股票池合成完成：{total} 只（策略 {n_strategy} 只 / 自选 {n_watch} 只）")
    if save and pool:
        rows_to_save = pool if save_picks else [r for r in pool if r.get("watchlist")]
        if rows_to_save:
            day = (day or engine.get_latest_data_date()
                   or datetime.now().strftime("%Y-%m-%d"))
            save_pool(engine.db_path, rows_to_save, day)
        else:
            logger.info("这一轮没有自选股要落库（选股结果不再自动进池）")
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

    - **去重**：既是策略选中又是自选 → 只留一行，`source` 记成「公式+自选」
      （内部标记；**显示**只写那条策略名 —— 见 `source_label()`）；
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
        # 来源标成"策略"还是"公式"：自定义公式不能混进内置策略里（见 `source_kind`）
        base = "公式" if is_formula_strategy(row.get("strategy") or "") else "策略"
        row.setdefault("source", base)
        merged.append(row)
    for entry in kept:
        symbol = entry["symbol"]
        note = (entry.get("note") or "").strip()
        if symbol in known:
            # 去重：策略标的与自选是同一只 → 只留一行
            for row in merged:
                if row["symbol"] == symbol:
                    base = ("公式" if is_formula_strategy(row.get("strategy") or "")
                            else "策略")
                    row["source"] = f"{base}+自选"
                    row["note"] = note
                    row["watchlist"] = True
                    break
            continue
        merged.append({
            "symbol": symbol,
            "name": entry.get("name") or symbol,
            "score": 0.0,
            "reason": "自选" + (f"（{note}）" if note else ""),
            "note": note,
            "watchlist": True,
            # 从选股结果页加入自选的票带着"当初是哪条策略/公式选出来的"
            # （`watchlist.source_strategy`）—— 推送正文、桌面文件、盘中提醒都读这一份，
            # 所以这里必须把它带上，否则同一只票在股池表里写 `公式·X+自选`、
            # 在推送里写「自选」，两处对不上（2026-09-21 主人实报的那个 bug）。
            **watchlist_source_fields(entry),
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
        weight = weight_of(strategy)
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


def limit_down_industries(db_path: str, day: str | None = None) -> dict[str, int]:
    """当日**各行业跌停家数**（「大盘概览 → 下跌前五」那一列用）。

    为什么要有它：那两张表原来是同一套列头（`板块名称 | 涨停数量 | 涨幅 | 主力净额`），
    而"下跌前五"里放涨停家数是**说不通**的（主人 2026-09-21 指出）。跌停家数本地
    本来没有现成的：`limit_up_pool` 只有涨停池，跌停池没落库。

    口径：取库里**最近两个交易日**的**不复权**收盘价，按 `public_sync.is_limit_down()`
    判跌停（那个函数里的板块规则是本项目实测过的唯一一份：主板/创业板/科创板 10%/20%、
    北交所 30% 且**向上取整**、ST 同幅度），再按 `stock_basic.industry` 归组计数。

    Returns:
        `{行业名: 跌停家数}`；库里不足两个交易日（或读不出来）时返回 `{}` ——
        宁可让界面显示 `—`，也不要拿"全市场跌停数"冒充某个板块的数。
    """
    from laoa_trader.data import public_sync      # 跌停价规则只此一份，别在这里重写

    try:
        with storage.connect(db_path) as conn:
            days = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT date FROM stock_daily_raw ORDER BY date DESC LIMIT 2"
                )
            ]
            if len(days) < 2:
                return {}
            latest, previous = days[0], days[1]
            rows = conn.execute(
                "SELECT d.symbol, d.close, p.close, b.industry, b.name "
                "FROM stock_daily_raw d "
                "JOIN stock_daily_raw p ON p.symbol = d.symbol AND p.date = ? "
                "JOIN stock_basic b ON b.symbol = d.symbol "
                "WHERE d.date = ? AND b.industry IS NOT NULL AND b.industry != ''",
                (previous, latest),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 这一列取不到就显示 —，不影响别的块
        logger.warning(f"读跌停家数失败（这一列会显示 —）：{exc}")
        return {}
    counts: dict[str, int] = {}
    for symbol, close, prev_close, industry, name in rows:
        if public_sync.is_limit_down(close, prev_close, str(symbol), str(name or "")):
            key = str(industry)
            counts[key] = counts.get(key, 0) + 1
    return counts


def save_pool(db_path: str, pool: list[dict], day: str | None = None) -> int:
    """把股票池写入 `stock_pool`（幂等 upsert，同一天重复跑只会覆盖同代码的行）。"""
    # 用**北京日期**：池子是按"行情日"存的，机器在 UTC（NAS/Docker/CI）时
    # `datetime.now()` 会差 8 小时、跨零点就写成昨天 —— 界面上"今天的池子"会查不到。
    day = day or clock.today_cn()
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


def push_tag(row: dict) -> str:
    """推送正文里的标签：**策略中文名**（多条用「、」连接）；没有策略就是空串。

    这里**必须翻译**成中文，不能像服务器版那样把类名截断直接用
    （`.replace("Strategy", "")`）——那是服务器版的内部叫法，而推送是给**手机上的
    人**看的：用户收到的会是 `1. 平安银行(000001)LowPrice｜…`，
    而界面同一只票写的是「低价股」（`strategy_label()`）。同一件事两个名字，
    用户根本分不清是"哪条策略选的"，也没法拿它去对照「策略选股」列表。

    **与「来源」列的关系**：界面那一列写 `策略·低价股`（只写主策略，列宽只够一条），
    推送这一行把**所有**命中的策略名都列出来（`低价股、短期反转`）——
    手机上一行没有 tooltip，多写几个字比少一条信息好。两处的中文名**同一个来源**
    （都出自 `strategy_names()`），所以是"详细程度不同"，不是"两套说法"。

    兼容已有的库/配置：老行里的 `strategies` 可能已经是中文名（用户用
    `enabled_strategies = ["低价股"]` 这种写法存进去的），翻译函数对中文名
    原样返回，所以**重复调用是幂等的**；`strategies` 为空时退回 `strategy` 那条。
    """
    return "、".join(strategy_names(row))


def format_pool_lines(pool: list[dict]) -> list[str]:
    """把池子整理成推送正文行（`1. 名称(代码)策略中文名｜理由`）。

    自选标的没有策略名，改用「自选」+ 备注，避免出现空标签。
    标签的中文名来自 `push_tag()`（理由见那里：推送是给手机看的一行字）。

    括号是**半角**：`docs/改版方案.md` 第四节把全项目给人看的标的写法统一成
    `名称(代码)` —— 界面两张表的列头就是这么写的，推送正文再写全角就等于
    同一只票在两处长得不一样（用户要拿推送去对照表格里的那一行）。
    注意：**只改给人看的字符串**，CSV/JSON/库里的字段一个都没动。
    """
    lines = []
    for i, row in enumerate(pool, start=1):
        tag = push_tag(row)
        note = (row.get("note") or "").strip()
        if not tag:
            tag = "自选" + (f"（{note}）" if note else "")
        elif note:
            # 2026-09-23 主人："为什么要+自选 什么策略跑出来的 直接记录策略名称
            # 只有用户自己输入的才能算自选来源" → 推送正文与「自选股池」那一列、
            # 桌面导出文件必须**同一个词**（策略名本身），三个地方都不再拼「+自选」。
            # 备注照旧带上：它是用户自己写的理由，与"来源"无关。
            tag += f"（{note}）"
        lines.append(f"{i}. {row['name']}({row['symbol']}){tag}｜{row.get('reason') or ''}")
    return lines


# ══════════════════════════════════════════════════════════════════════════
# 桌面导出：选股结果除了进「自选股池」，也 output 一个文件到桌面
# ══════════════════════════════════════════════════════════════════════════
#
# 用户原话：「选股结果直接进自选股池……（也可以同时 output 一个文件到桌面）」
#
# 为什么放在 `pool.py`（而不是 `ui/` 或 `scheduler.py`）：
#   1. 这里的每一行都是**池子行 → 给人看的文本**，与 `format_pool_lines()`
#      （推送正文）是同一件事的两种排版；放一处才不会出现"两套写法"，
#      而且两份文本用的是同一批中文名（`strategy_label` / `source_label`）。
#   2. 界面【开始选股】、定时日更、CLI `--once` **三条路都经过 `run_daily` → 建池**，
#      导出挂在这个位置三条路就都有桌面文件；挂在界面上则只有点按钮那条路有。
#   3. 这一层不依赖 Qt、不联网、不读配置，可以单独测（`tests/test_desk_export.py`）。

#: 导出文件名（用户给定：`老牛选股助手-选股结果-2026-09-18.txt`）。
#: 同一天再跑一次会**覆盖同一个文件**：桌面不是归档目录，堆一串同名文件只会让人分不清。
EXPORT_NAME_PREFIX = "老牛选股助手-选股结果-"
EXPORT_NAME_SUFFIX = ".txt"

#: 文件名与正文里的日期写法（用户给定：`2026-09-18`）
EXPORT_DAY_FORMAT = "%Y-%m-%d"

#: 正文第一行（`老牛选股助手 · 选股结果 · 2026-09-18（行情日 2026-09-17）`）
EXPORT_TITLE = "老牛选股助手 · 选股结果"

#: 正文最后一行。**必须留着**：这份文件常被用户转发到群里，而里面的价格只是
#: 公开来源的快照 —— 不写清楚，看到的人会当它是交易所行情。
EXPORT_FOOTER = (
    "（本文件由程序自动生成；数据为公开来源的准实时快照，"
    "非交易所授权行情，不构成投资建议。）"
)

#: 桌面目录的候选写法：Windows 英文系统叫 `Desktop`、中文系统叫 `桌面`；
#: 后两条覆盖"桌面被 OneDrive 接管"那类机器。自己拼路径一定会猜错几台机器，
#: 所以 `_standard_desktop()` 还会**先**问 Qt/系统要一次答案（见那里的说明）。
#: 桌面上的**子目录**：导出文件落在 `桌面/老牛选股/` 里（2026-09-21 主人要求）。
#: 为什么要有这一层：以前直接扔在桌面根目录，用久了桌面上会散着一堆
#: `老牛选股助手-选股结果-*.txt`（每天一个），桌面本身就是用户摆东西的地方 ——
#: 收进一个以软件命名的文件夹里，找起来反而更快。
EXPORT_FOLDER_NAME = "老牛选股"

DESKTOP_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("Desktop",),
    ("桌面",),
    ("OneDrive", "Desktop"),
    ("OneDrive", "桌面"),
)


def _is_dir(path: Path) -> bool:
    """是不是一个**能进去的**目录（没权限/路径怪 → 当成没有，绝不抛异常）。

    为什么单独包一层：桌面目录是"猜"出来的，猜错的那台机器上可能是
    `C:\\Users\\别人\\Desktop` 这种读不动的路径 —— 个别 Windows 路径上
    `is_dir()` 会抛 `OSError`，而"导出到桌面"这件事**不该**把选股流程带走。
    """
    try:
        return path.is_dir()
    except OSError:
        return False


def _standard_desktop() -> Path | None:
    """问系统要"真正的桌面目录"（Windows 上认 OneDrive 重定向与中文名）。

    为什么要问系统：桌面被改成中文名、或被 OneDrive 搬到 `OneDrive\\桌面` 之后，
    `~/Desktop` 就不存在了 —— 自己拼路径会在**那台机器上**静默失败（用户的
    桌面就在眼前，程序却说"写不出去"）。`QStandardPaths` 走的是系统 API。

    `pool.py` 是后端模块（CLI、定时任务都会 import 它），所以这里**懒加载 + 全程兜底**：
    没装 PySide6 的机器不该因为"导出到桌面"这一件事而 import 就炸。
    """
    try:
        from PySide6.QtCore import QStandardPaths
    except Exception:  # noqa: BLE001 - 没有 Qt 就退回自己拼路径
        return None
    try:
        location = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation
        )
    except Exception:  # noqa: BLE001 - Qt 的枚举/版本差异不该影响导出
        return None
    return Path(location) if location else None


def desktop_dir(*, home: Any = None, fallback_dir: Any = None) -> Path | None:
    """找桌面目录：候选里**第一个存在的**；一个都没有就退回 `fallback_dir`。

    Args:
        home: 家目录（默认 `Path.home()`；测试注入 tmp_path，**不碰真桌面**）。
            注入了 home 就不再问系统要桌面目录 —— 那一路必须完全可控。
        fallback_dir: 桌面一个都找不到时的落点（调用方给**数据目录**）——
            "这台机器没有桌面"不该让结果凭空消失，至少留在用户找得到的数据目录里，
            日志里也会写明落在哪。

    Returns:
        目录 Path；连回退目录都没有时返回 None（调用方记日志、不导出）。
    """
    base = Path(home).expanduser() if home is not None else Path.home()
    candidates: list[Path] = []
    if home is None:
        # 系统给的答案排第一：它可能是 OneDrive 里的那一个，也可能是中文名那一个。
        # **注入 home 时（测试/特殊部署）不问系统** —— 那一路必须完全由调用方决定，
        # 否则"把家目录指到 tmp_path 免得写真人桌面"这件事会被系统答案绕过。
        standard = _standard_desktop()
        if standard is not None:
            candidates.append(standard)
    candidates.extend(base.joinpath(*parts) for parts in DESKTOP_SUBDIRS)
    for candidate in candidates:
        if _is_dir(candidate):
            return candidate
    # 回退目录**不要求存在**：由写文件那一步创建（数据目录可能还没建出来，
    # 但那是"至少能写"的地方，比"谁都不要"好）
    return Path(fallback_dir) if fallback_dir is not None else None


def latest_quotes(db_path: Any) -> dict[str, tuple[float, float | None]]:
    """`{代码: (最新价, 涨跌幅%)}` —— 给桌面文件那行"现价 +x.xx%"用。

    **读 `stock_daily_raw`（不复权）而不是 `stock_daily_hfq`（后复权视图）**：
    后复权价是给策略算因子用的口径，10 送 10 之后它能变成真实价的两倍 ——
    写进给人看的文件里就是"茅台 2600 元"这种假数字。用户手上的成交价是不复权价。

    涨跌幅按**库里最近两个交易日**的收盘价算；只有一天数据（或那天停牌没有前收）
    时返回 `None` —— 那时正文只写现价、不编一个百分比。
    """
    with storage.connect(db_path) as conn:
        days = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT date FROM stock_daily_raw ORDER BY date DESC LIMIT 2"
            )
        ]
        if not days:
            return {}
        rows = conn.execute(
            "SELECT symbol, date, close FROM stock_daily_raw WHERE date IN (?, ?)",
            (days[0], days[1] if len(days) > 1 else days[0]),
        ).fetchall()

    latest: dict[str, float] = {}
    previous: dict[str, float] = {}
    for symbol, day, close in rows:
        if close is None or not symbol:
            continue
        target = latest if day == days[0] else previous
        target[str(symbol)] = float(close)

    out: dict[str, tuple[float, float | None]] = {}
    for symbol, price in latest.items():
        before = previous.get(symbol)
        # 昨收为 0/缺失 → 不算涨跌幅（除零会把整份导出变成"没有文件"）
        pct = (price - before) / before * 100.0 if before else None
        out[symbol] = (price, pct)
    return out


def _quote_text(price: float, pct: float | None) -> str:
    """`现价 1266.98 +0.71%`（没有涨跌幅时只写现价 —— 宁可少一个数字，也不编一个）。"""
    text = f"现价 {price:.2f}"
    if pct is not None:
        text += f" {pct:+.2f}%"
    return text


def _export_source(row: dict) -> str:
    """桌面文件里的「来源」：与「自选股池」表格那一列**同一个词**。

    三条退路（越靠前越权威）：

    1. 行里已经算好的 `source_label`（`pool_table_rows()` 填的，即那条策略名）；
    2. 现算一次 `source_label(row, None)` —— `run_daily` 传进来的池子行只有
       `strategy/strategies/source/watchlist`，没有 `source_label`；
    3. 退回 `source`（`策略` / `公式` / `自选` / `公式+自选`；老数据可能带 `+自选`）。

    为什么留第 3 条：第 2 条在"纯自选行没有 `watchlist` 标记"时会给一个 `—`
    （`source_label` 的兜底值）—— 给人看的文件里写"来源：—"等于什么都没说。
    """
    label = wording.display_source_label(str(row.get("source_label") or "").strip())
    if not label:
        label = source_label(row, None)
    if not label or label == "—":
        # 第 3 条退路拿到的是内部短标签（`公式` / `公式+自选`），照样要说人话
        # （2026-09-22 起界面上不用"公式"这个词，见 `wording`）
        label = wording.strip_watch_suffix(
            wording.display_words(str(row.get("source") or "").strip())
        ) or "—"
    return label


def pick_export_text(
    pool_rows: list[dict],
    *,
    data_date: str | None = None,
    day: str | None = None,
    quotes: dict[str, tuple[float, float | None]] | None = None,
) -> str:
    """本次选股结果 → 桌面文件的**正文**（纯函数：不碰磁盘、不联网、不读配置）。

    版式（用户给定）：

        老牛选股助手 · 选股结果 · 2026-09-18（行情日 2026-09-17）
        共 N 只（策略 M · 自选 K）
        1. 贵州茅台(600519)  现价 1266.98 +0.71%  来源：策略·短期反转
        2. …
        （本文件由程序自动生成；……不构成投资建议。）

    三条口径：

    * **数量**用 `pool_counts()`（与「自选股池」表头同一个函数），所以"M 只策略 /
      K 只自选"与界面上那两个数是同一份算法，不会对不上；
    * **来源**优先用行里已经算好的 `source_label`（`pool_table_rows()` 填的，
      就是那条策略名），没有才算一次 —— 界面、推送、桌面文件同源；
    * **现价**来自 `quotes`（`latest_quotes()` 的结果或调用方注入），没有就不写这一段。

    隐私：正文里**只有选股结果本身**（代码、名称、价格、来源）—— 没有 Key、
    没有本地路径、没有系统信息。这份文件是要被用户转发出去的。
    """
    rows = [row for row in (pool_rows or []) if row.get("symbol")]
    if day is None:
        # 懒加载：`intraday` 自己会（在函数里）import `pool`，模块级互相 import
        # 会在别的入口顺序不同的情况下变成循环 —— 这里只需要"今天是哪天"。
        from laoa_trader import intraday as intraday_mod

        day = intraday_mod.now_shanghai().strftime(EXPORT_DAY_FORMAT)

    total, strategy, watch = pool_counts(rows)
    lines = [
        f"{EXPORT_TITLE} · {day}（行情日 {data_date or '未知'}）",
        f"共 {total} 只（策略 {strategy} · 自选 {watch}）",
    ]
    for index, row in enumerate(rows, start=1):
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "") or symbol
        source = _export_source(row)
        parts = [f"{index}. {name}({symbol})"]
        entry = (quotes or {}).get(symbol)
        if entry:
            try:
                price = float(entry[0])
                raw_pct = entry[1] if len(entry) > 1 else None
                pct = float(raw_pct) if raw_pct is not None else None
            except (TypeError, ValueError, IndexError):
                price, pct = 0.0, None      # 价格字段坏掉 → 这一行不写价格段
            if price > 0:
                parts.append(_quote_text(price, pct))
        parts.append(f"来源：{source}")
        # 两空格分隔：数字与中文之间留白，用户看一行就知道哪段是价格、哪段是来源
        lines.append("  ".join(parts))
    lines.append(EXPORT_FOOTER)
    return "\n".join(lines) + "\n"


def export_file_name(day: str) -> str:
    """文件名（`老牛选股助手-选股结果-2026-09-18.txt`，用户给定）。"""
    return f"{EXPORT_NAME_PREFIX}{day}{EXPORT_NAME_SUFFIX}"


def _write_export(path: Path, text: str) -> None:
    """真正落盘（单独一层：测试要能确定性地模拟"写盘失败"）。

    `utf-8-sig`（带 BOM）：这份文件是给 Windows 用户**双击打开**的，
    中文记事本/Excel 靠 BOM 认编码；没有它，老版本记事本会把中文显示成乱码。
    `newline="\\r\\n"`：Windows 桌面上双击就用记事本看，CRLF 在任何编辑器里都正常，
    而且**跨平台写出的字节是确定的**（测试才能逐字节钉住内容）。
    """
    path.write_text(text, encoding="utf-8-sig", newline="\r\n")


def export_pick_file(
    pool_rows: list[dict],
    *,
    data_date: str | None = None,
    day: str | None = None,
    dest_dir: Any = None,
    quotes: dict[str, tuple[float, float | None]] | None = None,
    db_path: Any = None,
    home: Any = None,
    fallback_dir: Any = None,
) -> Path | None:
    """把**本次选股结果**写成一个桌面上的纯文本文件；失败只记日志、返回 None。

    用户要求："结果直接进自选股池……也可以同时 output 一个文件到桌面"。
    所以这是**附赠**产物：它绝不能影响选股/建池/推送（调用方 `run_daily` 另有兜底
    try，这里自己也不再往外抛）。

    落点（2026-09-21 主人要求）：`桌面/老牛选股/老牛选股助手-选股结果-<日期>.txt` ——
    桌面根目录不再散着文件，都收进以软件命名的那个文件夹里；找不到桌面时退回数据目录，
    同样套一层 `老牛选股`。

    Args:
        pool_rows: 池子行（`build_pool()` 的返回值）。
        data_date: 行情日（写进标题括号里；来自 `run_daily` 的 `report["data_date"]`）。
        day: 文件名/标题里的日期（默认按**北京时间今天**）。
        dest_dir: 目标目录。**测试与特殊部署注入它**（不传就自己找桌面，见
            `desktop_dir()`）—— 测试绝不能往真桌面上写文件。
        quotes: `{代码: (现价, 涨跌幅%)}`；None 且给了 `db_path` 时按库算
            （`latest_quotes()`）。
        db_path: 行情库路径（只用来取现价）。
        home: 家目录（默认 `Path.home()`；测试注入，避免摸到真桌面）。
        fallback_dir: 找不到桌面时的落点（调用方给数据目录）。

    Returns:
        写出去的文件路径；**没有结果 / 找不到可写目录 / 写盘失败**都返回 None
        （原因全部写进日志，绝不静默）。
    """
    rows = [row for row in (pool_rows or []) if row.get("symbol")]
    if not rows:
        logger.info("本次没有选股结果，不导出桌面文件")
        return None
    try:
        if day is None:
            from laoa_trader import intraday as intraday_mod

            day = intraday_mod.now_shanghai().strftime(EXPORT_DAY_FORMAT)
        target = (
            Path(dest_dir).expanduser()
            if dest_dir is not None
            else desktop_dir(home=home, fallback_dir=fallback_dir)
        )
        if target is not None and dest_dir is None:
            # 只有"自己找桌面/回退目录"这条路上才套子目录：调用方**显式**给了
            # `dest_dir`（测试、定制部署）时不多加一层 —— 那时目录是调用方说了算。
            target = target / EXPORT_FOLDER_NAME
        if target is None:
            logger.warning(
                "找不到桌面目录、也没有可用的回退目录：本次选股结果没有导出"
                "（不影响选股与推送）"
            )
            return None
        target.mkdir(parents=True, exist_ok=True)      # 目录不在就建（回退目录常常还没建）
        if quotes is None and db_path is not None:
            try:
                quotes = latest_quotes(db_path)
            except Exception as exc:  # noqa: BLE001 - 取不到价格照样导出（只是少一列）
                logger.warning(f"读取最新价失败，桌面文件里不写现价：{exc}")
                quotes = {}
        path = target / export_file_name(day)
        _write_export(path, pick_export_text(rows, data_date=data_date,
                                            day=day, quotes=quotes))
        logger.info(f"选股结果已导出到 {path}")
        return path
    except Exception as exc:  # noqa: BLE001 - 导出失败绝不能把选股流程带走
        logger.warning(
            f"导出选股结果到桌面失败（不影响选股与推送）：{type(exc).__name__}: {exc}"
        )
        return None


#: **老版本**用过、现在只用来"剥掉"的前缀：2026-09-22 那版把来源列显示成 `策略·X`，
#: 2026-09-23 主人要求连前缀一起去掉。留着它是因为**老的备注文本**里可能写着
#: `选股来源：策略·X`（见 `watchlist_source_strategy()`），解析时要把这截剥掉。
STRATEGY_SOURCE_PREFIX = "策略·"

#: 老版本把选中它的那条策略**写在备注里**时用的前缀（`选股来源：公式·尾盘超短策略`）。
#: 现在来源有自己的列（`watchlist.source_strategy`），这个前缀只用来**救老数据**：
#: 认得出就显示出来，认不出就当没有（宁可显示「自选」，也不瞎猜）。
WATCH_SOURCE_NOTE_PREFIX = "选股来源："


def watchlist_source_strategy(entry: dict) -> str:
    """自选表的一行 → **当初是哪条策略/公式把它选进来的**（没有就返回空串）。

    两条来源，越靠前越权威：

    1. `watchlist.source_strategy` 这一列（2026-09-21 加）—— **加入那一刻**写下来的，
       用户后来怎么改备注都改不掉它。写法与 `stock_pool.strategy` 一致
       （`公式·尾盘超短策略` / 老内置策略的类名）。
    2. 备注里的 `选股来源：X` —— 老版本（来源列还不存在时）把来源写在备注里，
       这里认一次能把那一批老数据救回来。X 是**给人看的那个词**，所以：
       `公式·X` 原样就是策略名；`策略·X` 要去掉前缀（`strategy_label()` 认不出的名字
       原样返回，显示时前缀会被 `source_label()` 补回来）。

    为什么"从备注里解析"只是退路、不是主路：备注是**用户自己的字段**（"龙头""消息面"），
    他随时可以改掉、也可以把别处粘来的文字写进去 —— 拿它当权威数据源，
    迟早会出现"来源被用户改备注改没了"。所以新数据一律走那一列。
    """
    stored = str(entry.get("source_strategy") or "").strip()
    if stored:
        return stored
    note = str(entry.get("note") or "")
    index = note.find(WATCH_SOURCE_NOTE_PREFIX)
    if index < 0:
        return ""
    value = note[index + len(WATCH_SOURCE_NOTE_PREFIX):].strip()
    # 备注是自由文本：取到行尾/第一个分隔符为止（老版本的写法是"这一行只有它"）
    for stop in ("\n", "；", ";", "，"):
        cut = value.find(stop)
        if cut >= 0:
            value = value[:cut].strip()
    if not value or value == "—":       # 「没有值」的占位符（与 `_export_source` 同一个字面量）
        return ""
    if value.startswith(STRATEGY_SOURCE_PREFIX):
        value = value[len(STRATEGY_SOURCE_PREFIX):].strip()
    return value


def watchlist_source_fields(entry: dict) -> dict:
    """自选行 → 池子行里那几个**来源字段**（`strategy` / `strategies` / `source` /
    `source_label` / `label`），全部走与池子行**同一套函数**算出来。

    为什么要有这一层：自选表里存着"当初是哪条公式选出来的"，而界面上那一列、推送正文、
    桌面文件都要说**同一个词**。把这一行的 `strategy` 填成那条公式名之后，
    `source_label()` / `source_kind()` / `strategy_names()` 全都不用改 ——
    它们本来就是按 `strategy` 说话的（见 `source_label()` 的说明）。
    来源为空的纯手工自选行：`strategy` 仍是空串，显示就是「自选」（与以前一模一样）。

    `enabled=False` 的行照样显示，只是来源里点明状态：`自选（已停用）` /
    `X（已停用）`（用户要能看见自己停用过的票并把它打开）。
    """
    key = watchlist_source_strategy(entry)
    row = {"strategy": key, "strategies": key, "watchlist": True}
    # 「+自选」这一截说的是"它在不在自选表里"，与"启用/停用"无关 ——
    # 所以算 kind/label 时统一按"启用"那一份算（停用只影响后面补的那个尾巴）。
    # 为什么不直接把停用的 entry 传进去：`source_kind()` 会把停用的票判成"不是自选"，
    # 界面上那一行的说明就会变成"策略选中的标的"（停用状态反而看不见了）。
    in_watch = dict(entry or {})
    in_watch["enabled"] = 1
    fields = {
        "strategy": key,
        "strategies": key,
        "label": legacy.strategy_label(key) if key else "",
        "source": source_kind(row, in_watch),
        "source_label": source_label(row, in_watch),
    }
    if not int(entry.get("enabled", 1) or 0):
        fields["source_label"] = f"{fields['source_label']}（已停用）"
    return fields


def strategy_names(row: dict) -> list[str]:
    """这一行被哪些策略/公式选中 → **中文名列表**（按库里的顺序，去重）。

    为什么要有这一层（原来每个调用方各自 split/翻译）：
    - `strategies` 是后加的列，**老库/手写的池子行可能只有 `strategy`** ——
      那时推送正文会退化成"自选"，把策略标的写成自选是最难查的那类错；
    - 中文名只有一份来源（`legacy.strategy_label`：老数据的类名 → 中文名）：手机上、表格里、tooltip 里
      看到的必须是同一个词，否则用户没法拿它去对照「策略选股」列表。

    自定义公式的合成名（`公式·放量上攻`）`strategy_label()` 认不出来会原样返回 ——
    正是我们要的：用户自己起的名字不能被翻译掉。
    """
    raw = [n.strip() for n in str(row.get("strategies") or "").split(",") if n.strip()]
    if not raw:
        primary = str(row.get("strategy") or "").strip()
        raw = [primary] if primary else []
    out: list[str] = []
    for name in raw:
        label = legacy.strategy_label(name)
        if label and label not in out:
            out.append(label)
    return out


def primary_strategy_name(row: dict) -> str:
    """**主策略**的中文名（`strategy` 字段那条；没有就退回第一条）。

    "主策略"是建池时按分数定下来的那一条（`pool.build_pool` 写进 `strategy`），
    也是界面「来源」列显示的那一条 —— 列宽只够写一条，其余的在 tooltip 里（见
    `source_detail_lines`）。
    """
    primary = str(row.get("strategy") or "").strip()
    if primary:
        return legacy.strategy_label(primary)
    names = strategy_names(row)
    return names[0] if names else ""


def source_kind(row: dict, watch_entry: dict | None) -> str:
    """池子行的来源类型：`策略` / `公式` / `自选` / 组合。

    自选状态由 `watchlist` 表现查（而不是存进 `stock_pool`）：
    这样用户今天加/停用自选，池子表格的"来源"立刻就对了，不用重跑建池，
    也不用给老库加列做迁移。

    自定义公式单独标成 `公式`（而不是混进"策略"）：用户勾的公式是他自己的东西，
    与内置策略的边际证据无关，"这批票是哪来的"要一眼分得清。
    """
    has_strategy = bool(row.get("strategy"))
    if has_strategy:
        # 2026-09-23 主人："为什么要+自选 什么策略跑出来的 直接记录策略名称
        # 只有用户自己输入的才能算自选来源" → 有策略来源就只报策略，不再拼「+自选」，
        # 于是"组合档"整个消失（`策略+自选` 这个值不再产生）。
        return "公式" if is_formula_strategy(row.get("strategy") or "") else "策略"
    # 没有策略来源的行都是**用户自己加进去的** → 自选
    return "自选"


def source_label(row: dict, watch_entry: dict | None) -> str:
    """界面「来源」列 / CLI 那一列的文本：**是哪条策略选出来的**，没有策略来源才是「自选」。

    用户明确要求这一列回答"是哪条策略"，而不是只写组别（`波段·T+10（T+10）`
    回答不了"凭什么选它"）。所以：

        自定义策略标的 → `尾盘选股策略`（前缀已去掉，见下）
        老库里的内置策略行 → `短期反转`（`legacy.strategy_label` 翻中文名）
        纯手工自选       → `自选`

    ⚠️ **2026-09-23 起不再有「+自选」这个尾巴**（主人："为什么要+自选 什么策略跑出来的
    直接记录策略名称 只有用户自己输入的才能算自选来源"）：策略选出来的票即使同时
    也在自选表里，来源列也只写策略名 —— 它在不在自选里，看自选表和监控开关那一列。

    ⚠️ **2026-09-22 起自定义的那条也显示 `策略·`**（主人："把公式都改成策略吧 这样好看点"）：
    库里存的还是 `公式·放量上攻`（**历史值一个字节都没改**），只有显示时换前缀 ——
    换的地方是 `wording.display_strategy()`（`legacy.strategy_label()` 会调它），
    所以升级前后同一只票在界面上是同一个写法，不会一会儿 `公式·X` 一会儿 `策略·X`。

    **组别与持有期不再进这一列**：它们回答的是"这条策略属于哪一组"，
    而"来源"要回答的是"哪条策略"。两者都在行 tooltip 里（`source_detail_lines`）。
    一行被多条策略选中时这里只写**主策略**：列数被用户定死成 6 列，
    其余策略名同样收进 tooltip。

    与**推送正文**的关系（`format_pool_lines` 的 `push_tag`）：推送那一行仍然把
    **所有**命中的策略名都列出来（`低价股、短期反转`）—— 手机上一行没有 tooltip，
    多写几个字比少一条信息好；两处用的是**同一个中文名**（都出自 `strategy_label`），
    所以不存在"同一件事两套说法"，只是详细程度不同。
    """
    strategy = str(row.get("strategy") or "")
    parts: list[str] = []
    if strategy:
        if is_formula_strategy(strategy):
            # 显示时把前缀换成「策略·」（`wording` 只换前缀，不动用户自己起的名字）
            parts.append(wording.display_strategy(strategy))
        else:
            name = primary_strategy_name(row)
            # 2026-09-23 主人要求去掉前缀（"公式名称中的策略两个字去掉，无意义"）：
            # 这里就写名字本身；认不出中文名的老类名才退回一个中性的「策略」。
            parts.append(name or "策略")
    # 2026-09-23 起**不再拼「+自选」**：来源列只回答"是哪条策略选出来的"。
    # "它同时也在自选里"这个事实在别处表达（自选表自己那一行、监控开关那一列），
    # 在来源列重复一遍只会让人以为"自选"是来源之一。
    return parts[0] if parts else "自选"


def source_detail_lines(row: dict) -> list[str]:
    """一行的**来源明细**（行 tooltip 用）：哪条策略 / 哪个组 / 同批还被谁选中。

    列数被用户定死成 6 列，塞不进第二列策略名，但"这一行到底是谁选出来的"必须查得到：
    - `来源：公式·尾盘选股策略` —— 与「来源」列同一个文本；
    - `同批选中：放量上攻` —— 只在这一行被**多条**公式选中时出现（写公式名）。
    """
    lines: list[str] = []
    # 这一行可能是调用方**自己拼的行**（只带 `source_label` 没有 `strategy`）——
    # 照样把内部前缀换成「策略·」，否则同一只票在 tooltip 里写 `公式·X`、
    # 在「来源」列里写 `策略·X`（2026-09-22 起界面上不用"公式"这个词）
    label = wording.display_source_label(str(row.get("source_label") or "").strip()) or "—"
    lines.append(f"来源：{label}")
    group_label = str(row.get("group_label") or "")
    horizon = int(row.get("horizon") or 0)
    if group_label and group_label != "—":
        lines.append(f"组别：{group_label}" + (f"（T+{horizon}）" if horizon else ""))
    primary = primary_strategy_name(row)
    others = [name for name in strategy_names(row) if name != primary]
    if others:
        lines.append("同批选中：" + "、".join(others))
    return lines


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


def watchlist_only_rows(db_path: str, day: str | None = None) -> list[dict]:
    """**不在今日池子里**的自选股 → 与 `pool_table_rows` 同形状的行。

    为什么必须有这一层：`pool_table_rows` 读的是 `stock_pool` 表 —— 那是**建池那一刻**
    的快照，只包含"策略/公式选中的 + 当时已存在的自选"。用户在两次建池之间手工加的自选
    根本不在里面，而「自选股池」这一页按用户要求是"唯一入口"：**加了就必须看得见**，
    不能等到今晚重新建池才出现（"我明明加了它，界面上没有"是最容易被当成 bug 的行为）。

    返回的行补上 `source_label = "自选"`、`industry`、`note`，以及 `is_limit_up`
    （今日涨停池里的信息，与池内行同一套口径）—— 界面上那一行与策略选出来的行
    长得一样、能用同样的交互，用户不需要知道"这两行其实是两个来源"。
    """
    with storage.connect(db_path) as conn:
        watch = storage.load_watchlist(conn)
        industries = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT symbol, industry FROM stock_basic WHERE industry IS NOT NULL"
            )
        }
        names = {
            r[0]: r[1]
            for r in conn.execute("SELECT symbol, name FROM stock_basic")
        }
    in_pool = set(pool_symbols(db_path, day))
    limit_up = limit_up_annotations(db_path, day)
    out: list[dict] = []
    for entry in watch:
        symbol = str(entry.get("symbol") or "")
        if not symbol or symbol in in_pool:
            continue
        enabled = int(entry.get("enabled", 1)) == 1
        # 「来源」列（2026-09-21 修）：这一只是**从选股结果页加入自选**的，就显示
        # `公式·尾盘超短策略+自选` —— 以前这里写死成「自选」，于是"从选股列表加入的票
        # 到股池里全变成自选了"（主人实报）。纯手工加的票没有来源，仍是「自选」。
        source_fields = watchlist_source_fields(entry)
        out.append({
            "symbol": symbol,
            "name": str(entry.get("name") or names.get(symbol) or ""),
            "score": None,
            "reason": "自选股",
            "group": "",
            "group_label": "—",
            "horizon": 0,
            "is_formula": is_formula_strategy(source_fields["strategy"]),
            # 停用的自选**照样显示**（用户要能看见自己停用过的票并把它打开），
            # 所以来源里点明状态：`自选（已停用）`（见 `watchlist_source_fields`）
            "note": str(entry.get("note") or ""),
            "watchlist_enabled": enabled,
            "evidence": "",
            "evidence_text": "",
            "industry": industries.get(symbol, ""),
            "is_limit_up": symbol in limit_up,
            "continue_day_text": (limit_up.get(symbol) or {}).get("continue_day_text", ""),
            "limit_up_reason": (limit_up.get(symbol) or {}).get("reason", ""),
            **source_fields,
        })
    return out


def pool_page_rows(db_path: str, day: str | None = None) -> list[dict]:
    """「自选股池」页的**全部行**：精选池（策略/公式/当时已有的自选）+ 后来手工加的自选。

    顺序：池内行在前（沿用 `pool_table_rows` 的分数降序），后来手工加的自选按
    `watchlist` 表的顺序追加在后 —— 用户刚加的那只排在末尾，正好在视线落点上。
    """
    rows = pool_table_rows(db_path, day)
    known = {str(r.get("symbol") or "") for r in rows}
    rows.extend(r for r in watchlist_only_rows(db_path, day)
                if str(r.get("symbol") or "") not in known)
    return rows


def pool_counts(rows: list[dict]) -> tuple[int, int, int]:
    """`(共 N, 策略 M, 自选 K)`：「自选股池」表头那一行小字用。

    口径（2026-09-23 起与「来源」列对齐 —— 主人："只有用户自己输入的才能算自选来源"）：
    - **策略 M** = 有 `strategy` 的行（自定义策略与老内置策略都算，界面上靠「来源」列区分）；
    - **自选 K** = **没有**策略来源、由用户手工加进来的行；
    - 于是 **M + K 恒等于 N**（每只票要么是选出来的、要么是你自己加的）。
      改之前的口径是"K = 只要在自选表里就算"，与策略行重叠，会出现
      `共 13 只（策略 12 · 自选 12）` 这种两个分项加起来大于总数的写法。
    """
    total = len(rows)
    strategy = sum(1 for r in rows if str(r.get("strategy") or ""))
    watch = sum(
        1 for r in rows
        if not str(r.get("strategy") or "") and "自选" in str(r.get("source") or "")
    )
    return total, strategy, watch


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
        is_formula = is_formula_strategy(strategy)
        entry = watch.get(row["symbol"])
        note = (entry or {}).get("note") or row.get("note") or ""
        out.append({
            **row,
            "industry": industries.get(row["symbol"], ""),
            # 自定义公式的合成名（`公式·放量上攻`）本身就是"来源策略"该显示的东西
            # （`strategy_label` 认不出它会原样返回，正好是我们要的）
            "label": legacy.strategy_label(strategy),
            # 组别/持有期：**2026-09-18 起没有"策略组"了**（内置策略改成随包公式、
            # 组机制整体删掉），所以这两栏对所有行都是"没有"。字段**保留**是因为
            # 下单方的行结构（推送、表格、CLI 打印）都还在读它们。
            "group": "",
            "group_label": "—",
            "horizon": 0,
            # 是不是自定义公式（界面/推送想单独标一句时用；**不打证据标记**）
            "is_formula": is_formula,
            # 来源列：**哪条策略选出来的**（`策略·短期反转`）/ 公式名 / 自选 / 组合。
            # 组别与持有期改由 `group_label` / `horizon` 单独带着，进 tooltip
            "source": source_kind(row, entry),
            "source_label": source_label(row, entry),
            "note": note,
            "watchlist_enabled": bool(entry and int(entry.get("enabled", 1)) == 1),
            # 证据字段：**2026-09-18 起一律为空**。那套"我们自己的策略有没有边际"的
            # 证据只属于已删掉的 Python 策略；公式是用户自己的东西，不贴我们的结论。
            # 字段本身留着，是因为两张表的行结构与老数据都在用。
            "evidence": "",
            "evidence_text": "",
            # 今日涨停池里的信息（不在池里 → is_limit_up False，界面上整行不显示）
            "is_limit_up": row["symbol"] in limit_up,
            "continue_day_text": (limit_up.get(row["symbol"]) or {}).get("continue_day_text", ""),
            "limit_up_reason": (limit_up.get(row["symbol"]) or {}).get("reason", ""),
        })
    return out
