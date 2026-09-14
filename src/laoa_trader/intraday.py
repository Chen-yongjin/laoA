"""盘中实时买卖点提醒 + 条件单参数（L2 半自动）。

本模块合并了服务器版的两个文件，逻辑逐行移植、阈值一字未改：

- `sequoia_x/intraday.py`：交易时段判断、观察池、**6 条盘中规则**、去重表；
- `sequoia_x/trade_plan.py`：`plan_buy` / `plan_sell` —— 触发价、委托价（含滑点）、
  **整手数量**、止损止盈，以及可直接抄进券商 App 的条件单文本。

本项目**只到 L2**：提醒 + 条件单参数，**不写任何真实下单代码**。
散户拿不到券商下单 API，但券商 App 自带"条件单"（触发价 → 委托价 → 数量），
这一步不需要任何权限，却能实现"到价自动执行"—— 这正是 L2 的价值。

6 条盘中规则
------------
| 类型 | 规则 | 触发 |
|---|---|---|
| 卖出/风控 | `stop_loss` | 现价 ≤ 参考价 ×(1−止损) |
| 卖出/风控 | `take_profit` | 现价 ≥ 参考价 ×(1+止盈) |
| 卖出/风控 | `break_ma5` | 昨收 ≥ MA5 且现价跌破 MA5×0.995 |
| 卖出/风控 | `limit_up_open` | 昨日涨停、今日触及涨停价后打开 |
| 买入 | `break_high` | 放量突破 20 日高点（涨 2%~9%，成交额 ≥ 日均 ×0.5） |
| 买入 | `pullback_ma5_buy` | 池内标的回踩 5 日线 ±1.5% 且盘中转强 |
| 打板 | `first_board` | 实时涨停池里"首板 + 封单 ≥5000 万" |

设计要点（继承服务器版）
------------------------
- **不刷屏**：`intraday_alert(date, symbol, kind)` 主键去重，同标的同类型当天只推一次；
- **只在交易时段跑**：以官方交易日历判断"今天是否开盘"，再卡 09:30-11:30 / 13:00-15:00；
- **可空跑**：`dry_run=True` 只打印不推送，便于验证；
- **失败不致命**：单轮异常只记日志，下一轮继续。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from laoa_trader.config import Config, get_config
from laoa_trader.data import hithink as hx
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 交易时段（本地时间，分钟数）
SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))

#: 盘中阈值（除止损/止盈来自配置外，其余与服务器版环境变量默认值一致）
BREAK_HIGH_WINDOW = int(os.environ.get("INTRADAY_BREAK_WINDOW", "20"))
MIN_BREAK_GAIN = float(os.environ.get("INTRADAY_MIN_BREAK_GAIN", "0.02"))
MAX_BREAK_GAIN = float(os.environ.get("INTRADAY_MAX_BREAK_GAIN", "0.09"))
FIRST_BOARD_SEAL = float(os.environ.get("INTRADAY_FIRST_BOARD_SEAL", "5e7"))
LOOKBACK_DAYS = int(os.environ.get("INTRADAY_LOOKBACK_DAYS", "10"))

#: A 股一手
LOT = 100

KIND_LABELS_POOL = {
    "pullback_ma5_buy": "🎯 池内回踩买点",
}

KIND_LABELS = {
    "stop_loss": "🛑 触及止损",
    "take_profit": "🎯 触及止盈",
    "break_ma5": "📉 跌破 5 日线",
    "limit_up_open": "🔓 涨停打开",
    "break_high": "🚀 放量突破20日高",
    "first_board": "🔥 首板厚封单",
    "pullback_ma5_buy": "🎯 池内回踩买点",
}


# ── 条件单参数（移植自 sequoia_x/trade_plan.py）──
#
# 服务器版从环境变量读这些参数；桌面版改为从 config.toml 读，
# **默认值与服务器版完全一致**（0.05 / 0.10 / 0.005）。


def stop_loss(cfg: Config | None = None) -> float:
    return (cfg or get_config()).stop_loss


def take_profit(cfg: Config | None = None) -> float:
    return (cfg or get_config()).take_profit


def round_lot(shares: float) -> int:
    """向下取整到 100 股（A 股一手）。"""
    return int(shares // LOT) * LOT


def position_size(
    price: float, capital: float | None = None, pct: float | None = None,
    cfg: Config | None = None,
) -> int:
    """按单票仓位占比算买入股数（整手）。

    资金买不起一手时返回 0（`plan_buy` 会据此给出明确告警，而不是给个 0 股的条件单）。
    """
    if not price or price <= 0:
        return 0
    cfg = cfg or get_config()
    capital = cfg.trade_capital if capital is None else capital
    pct = cfg.trade_position_pct if pct is None else pct
    return round_lot(capital * pct / price)


def plan_buy(
    symbol: str,
    name: str,
    price: float,
    reason: str = "",
    capital: float | None = None,
    held: int = 0,
    positions: int = 0,
    cfg: Config | None = None,
) -> dict:
    """生成买入计划（含可直接抄的条件单文本）。

    Args:
        price: 触发价（通常用提醒时点的现价）。
        held: 该标的已持有股数（>0 则提示不加仓）。
        positions: 当前持仓只数（达到上限则提示）。
    """
    cfg = cfg or get_config()
    stop_pct, target_pct = cfg.stop_loss, cfg.take_profit
    trigger = round(float(price), 2)
    limit_price = round(trigger * (1 + cfg.buy_slippage), 2)
    stop = round(trigger * (1 - stop_pct), 2)
    target = round(trigger * (1 + target_pct), 2)
    qty = 0 if held else position_size(trigger, capital, cfg=cfg)

    warnings = []
    if held:
        warnings.append(f"已持有 {held} 股，不建议加仓")
    if positions >= cfg.trade_max_positions:
        warnings.append(f"持仓已达上限 {cfg.trade_max_positions} 只，需先减仓")
    if qty == 0 and not held:
        warnings.append(
            f"资金不足：单票预算 {cfg.trade_capital * cfg.trade_position_pct:.0f} 元买不起 1 手"
        )

    lines = [
        f"【条件单｜买入】{symbol} {name}",
        f"触发：价格 ≥ {trigger:.2f}",
        f"委托：限价 {limit_price:.2f} × {qty or '—'} 股",
        f"止损：{stop:.2f}（-{stop_pct * 100:.0f}%）｜止盈：{target:.2f}（+{target_pct * 100:.0f}%）",
        "有效期：当日",
    ]
    if reason:
        lines.append(f"依据：{reason}")
    if warnings:
        lines.append("⚠️ " + "；".join(warnings))

    return {
        "symbol": symbol, "name": name, "side": "buy",
        "trigger": trigger, "limit_price": limit_price, "quantity": qty,
        "stop_loss": stop, "take_profit": target,
        "warnings": warnings, "text": "\n".join(lines),
    }


def plan_sell(
    symbol: str,
    name: str,
    cost: float,
    price: float | None = None,
    qty: int = 0,
    reason: str = "",
    cfg: Config | None = None,
) -> dict:
    """生成卖出计划：对**真实成本**算止损/止盈触发价（比信号日收盘准）。"""
    cfg = cfg or get_config()
    stop_pct, target_pct = cfg.stop_loss, cfg.take_profit
    cost = float(cost or 0)
    if cost <= 0:
        return {"symbol": symbol, "name": name, "side": "sell",
                "warnings": ["缺少成本价，无法算止损止盈"], "text": ""}
    stop = round(cost * (1 - stop_pct), 2)
    target = round(cost * (1 + target_pct), 2)
    last = round(float(price), 2) if price else None
    limit_price = round(stop * (1 - cfg.sell_slippage), 2)

    lines = [
        f"【条件单｜卖出】{symbol} {name}",
        f"止损：价格 ≤ {stop:.2f}（成本 {cost:.2f} × {1 - stop_pct:.2f}）",
        f"委托：限价 {limit_price:.2f} × {qty or '全部'} 股",
        f"止盈：价格 ≥ {target:.2f}（+{target_pct * 100:.0f}%）",
    ]
    if last is not None:
        pl = (last / cost - 1) * 100
        lines.append(f"当前 {last:.2f}（浮动 {pl:+.2f}%）")
    if reason:
        lines.append(f"依据：{reason}")
    return {
        "symbol": symbol, "name": name, "side": "sell", "cost": cost,
        "trigger": stop, "limit_price": limit_price, "take_profit": target,
        "quantity": qty, "warnings": [], "text": "\n".join(lines),
    }


# ── 交易时段与交易日 ──


#: A 股的一切"几点钟"都以**北京时间**为准
_TZ_SHANGHAI = timezone(timedelta(hours=8))


def now_shanghai(now: datetime | None = None) -> datetime:
    """取"北京时间的墙上时间"；传入的 `now` 原样返回（便于测试注入确定时刻）。

    为什么不用 `datetime.now()`：那台机器的时区不一定是北京 —— CI 的 Windows runner 就是
    UTC，当地 13:45 会被判成"交易时段中"（而北京此刻是 21:45，早已收盘）。交易时段、
    交易日、提醒时间戳这些**全部**要与机器时区解耦，否则换台机器/出国就错，而且错得很安静。

    **返回值是朴素的（不带 tzinfo）**：项目里所有时间比较（交易日历、run_at、补跑）
    都是"墙上时间"的朴素比较，塞一个 aware 的 datetime 进去会直接
    `TypeError: can't compare offset-naive and offset-aware`。所以这里把北京时间的
    墙上钟面值取出来、丢掉 tzinfo —— 语义就是"现在是北京几点"，与既有代码天然一致。
    """
    if now is not None:
        return now
    return datetime.now(_TZ_SHANGHAI).replace(tzinfo=None)


def in_session(now: datetime | None = None) -> bool:
    """当前是否在交易时段内（不判断是否交易日，见 is_trading_day）。"""
    moment = now_shanghai(now)
    minutes = moment.hour * 60 + moment.minute
    return any(start <= minutes <= end for start, end in SESSIONS)


def is_trading_day(db_path: str, day: str | None = None) -> bool:
    """用官方交易日历判断是否开盘（库里查不到就当交易日，避免日历没同步时整天不跑）。"""
    day = day or now_shanghai().strftime("%Y-%m-%d")
    try:
        with storage.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM trading_calendar WHERE date = ?", (day,)
            ).fetchone()
            has_calendar = conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    except Exception:  # noqa: BLE001 - 库还没建好时按"交易日"处理，别把提醒整天关掉
        return True
    if not has_calendar:
        return True
    return bool(row)


# ── 观察池 ──


def _watch_label(is_strategy: bool, note: str, cost: float | None) -> str:
    """自选股的展示标签：`自选（龙头，成本 12.40）` / `策略+自选（成本 12.40）`。

    把备注与成本写进提醒里，是为了让收到卡片的人**一眼知道为什么盯它**：
    备注是用户自己写的理由（"龙头""消息面"），成本则直接对应止损止盈的基准。
    """
    head = "策略+自选" if is_strategy else "自选"
    details = []
    if note:
        details.append(note)
    if cost:
        details.append(f"成本 {float(cost):.2f}")
    return f"{head}（{'，'.join(details)}）" if details else head


def _display_name(base: str, info: dict) -> str:
    """提醒里显示的标的名称（自选带标签，策略标的原样）。"""
    label = info.get("label")
    if label:
        return f"{base}（{label}）" if base and base not in label else str(base or label)
    return base


def recent_signal_symbols(
    db_path: str, days: int = LOOKBACK_DAYS, allowed_strategies: set[str] | None = None
) -> dict[str, dict]:
    """取最近 N 个交易日推送过的信号（作为卖出/风控观察池）。"""
    with storage.connect(db_path) as conn:
        return storage.recent_signal_symbols(conn, days, allowed_strategies)


def watch_targets(
    db_path: str,
    days: int = LOOKBACK_DAYS,
    selection: Any = None,
    cfg: Config | None = None,
) -> tuple[dict[str, dict], set[str]]:
    """盘中观察目标：**精选股票池优先**，近期推送信号兜底。

    Args:
        selection: 启用的组/策略；只盯**它们产生的**标的
            （自选策略组之后，盘中提醒不该再提示被关掉那组的股票）。
            None 时按配置解析（配置里两组都空 = 全选）。
        cfg: 配置（解析 selection 用）。

    Returns:
        ({symbol: {...}}, 池内符号集合)
    """
    from laoa_trader import pool as pool_mod
    from laoa_trader.strategy import groups as groups_mod

    if selection is None:
        selection = groups_mod.resolve_from_config(cfg or get_config())
    allowed = set(selection.strategies) if selection is not None else None

    def _kept(strategies_text: str) -> bool:
        """池子/信号里的策略串（逗号分隔）里**有任意一条在启用范围内**就保留。

        为什么看全部而不是只看主策略：一只股票可能同时被"低价股"和"首板缩量整理"
        选中，主策略字段只存了第一个 —— 只看主策略会把启用组里的标的误判成不可用。
        """
        if allowed is None:
            return True
        names = [x for x in str(strategies_text or "").split(",") if x]
        return any(name in allowed for name in names) if names else True

    cfg = cfg or get_config()
    targets: dict[str, dict] = {}
    pool_rows = [
        row for row in pool_mod.load_pool(db_path)
        if _kept(row.get("strategies") or row.get("strategy") or "")
    ]
    pool_symbols = {row["symbol"] for row in pool_rows}
    for row in pool_rows:
        targets[row["symbol"]] = {
            "name": row.get("name"), "signal_date": row.get("date"), "close": None,
            "source": "pool", "strategy": row.get("strategy"),
        }

    # ── 自选股：**无论策略池是否为空都要盯** ──
    # 为什么直接从 watchlist 表读、而不是只依赖池子：用户刚加的自选要立刻生效，
    # 不必等到今晚重新建池；策略被关掉（enabled_groups=["none"]）时也一样盯。
    if getattr(cfg, "watchlist_in_pool", True):
        with storage.connect(db_path) as conn:
            watch_entries = storage.load_watchlist(conn, enabled_only=True)
            positions = storage.load_positions(conn)
        for entry in watch_entries:
            symbol = entry["symbol"]
            note = (entry.get("note") or "").strip()
            held = positions.get(symbol)
            info = targets.get(symbol) or {}
            label = _watch_label(bool(info.get("strategy")), note,
                                 held["avg_cost"] if held else None)
            targets[symbol] = {
                **info,
                "name": info.get("name") or entry.get("name") or symbol,
                "label": label,
                "note": note,
                "watchlist": True,
                "source": "策略+自选" if info.get("strategy") else "自选",
                # 有持仓 → 用**持仓成本**做止损/止盈参考价；
                # 没有 → 留 None，交给 evaluate_sell_rules 回退到"前一交易日收盘"
                "close": float(held["avg_cost"]) if held and held.get("avg_cost") else None,
                "cost": float(held["avg_cost"]) if held and held.get("avg_cost") else None,
            }
            # 自选也要吃"池内回踩买点"这类买点规则
            pool_symbols.add(symbol)
    # 有股票池就**只盯池子**（池子是精选的热门行业标的，盯得过来、响应快）；
    # 池子为空时才退回"近期信号"（例如当晚选股还没跑）。
    if targets and os.environ.get("INTRADAY_POOL_ONLY", "1") != "0":
        return targets, pool_symbols
    for symbol, info in recent_signal_symbols(db_path, days, allowed).items():
        targets.setdefault(symbol, {**info, "source": "signal"})
    return targets, pool_symbols


def history_context(
    db_path: str, symbols: list[str], window: int = BREAK_HIGH_WINDOW
) -> dict:
    """取历史上下文：前 N 日最高价、MA5、MA20、20 日均量额（口径同服务器版）。"""
    if not symbols:
        return {}
    context: dict[str, dict] = {}
    with storage.connect(db_path, timeout=60) as conn:
        for i in range(0, len(symbols), 500):
            chunk = symbols[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT symbol, date, high, close, turnover FROM stock_daily_hfq "  # noqa: S608
                f"WHERE symbol IN ({marks}) ORDER BY symbol, date",
                tuple(chunk),
            ).fetchall()
            grouped: dict[str, list] = {}
            for symbol, date, high, close, turnover in rows:
                grouped.setdefault(symbol, []).append((date, high, close, turnover))
            for symbol, bars in grouped.items():
                closes = [b[2] for b in bars if b[2]]
                highs = [b[1] for b in bars if b[1]]
                turnovers = [b[3] for b in bars if b[3]]
                if len(closes) < 5:
                    continue
                context[symbol] = {
                    "prev_close": closes[-1],
                    "prev_prev_close": closes[-2] if len(closes) > 1 else None,
                    "high20": max(highs[-(window + 1) : -1]) if len(highs) > window else None,
                    "ma5": mean(closes[-5:]),
                    "ma20": mean(closes[-20:]) if len(closes) >= 20 else None,
                    "avg_turnover20": mean(turnovers[-20:]) if len(turnovers) >= 20 else None,
                    "last_date": bars[-1][0],
                }
    return context


# ── 规则 ──


def evaluate_sell_rules(
    symbol: str, snap: dict, ctx: dict, ref_close: float | None,
    cfg: Config | None = None,
) -> list[tuple[str, float, str]]:
    """卖出/风控规则：返回 [(kind, price, detail)]。"""
    cfg = cfg or get_config()
    stop_pct, target_pct = cfg.stop_loss, cfg.take_profit
    last = snap.get("last_price")
    if not last or not ctx:
        return []
    hits = []
    ref = ref_close or ctx.get("prev_close")
    if ref:
        if last <= ref * (1 - stop_pct):
            hits.append(("stop_loss", last,
                         f"现价 {last:.2f} ≤ 参考价 {ref:.2f} × {1 - stop_pct:.2f}"))
        elif last >= ref * (1 + target_pct):
            hits.append(("take_profit", last,
                         f"现价 {last:.2f} ≥ 参考价 {ref:.2f} × {1 + target_pct:.2f}"))
    ma5 = ctx.get("ma5")
    prev_close = ctx.get("prev_close")
    if ma5 and prev_close and prev_close >= ma5 and last < ma5 * 0.995:
        hits.append(("break_ma5", last, f"现价 {last:.2f} 跌破 5 日线 {ma5:.2f}"))
    prev_prev = ctx.get("prev_prev_close")
    if prev_close and prev_prev and prev_close >= prev_prev * 1.095:
        limit_price = prev_close * 1.095
        if (snap.get("high_price") or 0) >= limit_price and last < limit_price * 0.995:
            hits.append(("limit_up_open", last, f"昨日涨停，今日触及 {limit_price:.2f} 后打开"))
    return hits


def evaluate_buy_rules(symbol: str, snap: dict, ctx: dict) -> list[tuple[str, float, str]]:
    """买入规则：放量突破 20 日高点（且未涨停，避免追一字板）。"""
    last = snap.get("last_price")
    pct = snap.get("price_change_ratio_pct")
    if not last or not ctx or pct is None:
        return []
    gain = float(pct) / 100.0
    high20 = ctx.get("high20")
    avg_turnover = ctx.get("avg_turnover20")
    turnover = snap.get("turnover") or 0
    if not high20 or not avg_turnover:
        return []
    # 盘中成交额只有一天的一部分，用 0.5 倍日均额作为"放量"的宽松门槛
    if last > high20 and MIN_BREAK_GAIN <= gain <= MAX_BREAK_GAIN and turnover >= avg_turnover * 0.5:
        return [("break_high", last,
                 f"现价 {last:.2f} 突破 20 日高点 {high20:.2f}，涨 {gain * 100:.1f}%")]
    return []


def evaluate_pool_buy_rules(symbol: str, snap: dict, ctx: dict) -> list[tuple[str, float, str]]:
    """池内买点：回踩 5 日线附近且盘中转强（现价高于昨收）。

    与"放量突破 20 日高"互补：那个追突破，这个接回踩 —— 池子里的标的
    多半是超跌/低位股，回踩买点更符合它们的形态。
    """
    last = snap.get("last_price")
    if not last or not ctx:
        return []
    ma5 = ctx.get("ma5")
    prev_close = ctx.get("prev_close")
    if not ma5 or not prev_close:
        return []
    if abs(last / ma5 - 1) <= 0.015 and last > prev_close:
        return [("pullback_ma5_buy", last,
                 f"回踩 5 日线 {ma5:.2f}（现价 {last:.2f}）且盘中转强")]
    return []


def evaluate_first_board(client: hx.HithinkClient) -> list[tuple[str, float, str]]:
    """实时涨停池里的"首板 + 厚封单"（打板候选，含现价与封单额）。"""
    try:
        rows = client.limit_up_pool()
    except hx.HithinkError as exc:
        logger.warning(f"实时涨停池获取失败：{exc}")
        return []
    hits = []
    for row in rows:
        days = row.get("continue_day_cnt")
        seal = row.get("seal_money") or row.get("order_amount") or 0
        code = str(row.get("thscode") or row.get("ticker") or "")
        if not code or days != 1 or float(seal) < FIRST_BOARD_SEAL:
            continue
        price = float(row.get("last_price") or 0)
        reason = row.get("limit_up_reason") or ""
        hits.append(
            (
                "first_board",
                price,
                f"{row.get('name', '')} 首板，封单 {float(seal) / 1e8:.2f} 亿"
                + (f"，{reason}" if reason else ""),
            )
        )
    return hits


# ── 一轮执行 ──


def build_alerts(
    engine: DataEngine, client: hx.HithinkClient, scan_market: bool = False
) -> list[dict]:
    """跑一轮规则，返回本轮命中的提醒（**未去重**）。"""
    pool, pool_symbols = watch_targets(engine.db_path)
    symbols = list(pool)
    if not symbols:
        logger.info("股票池与近期信号都为空，无标的可盯")
    else:
        logger.info(f"本轮盯 {len(symbols)} 只（其中股票池 {len(pool_symbols)} 只）")
    ctx = history_context(engine.db_path, symbols)

    alerts: list[dict] = []
    for i in range(0, len(symbols), 100):
        batch = symbols[i : i + 100]
        try:
            snaps = client.snapshot(batch)
        except hx.HithinkError as exc:
            logger.warning(f"实时快照获取失败：{exc}")
            continue
        for snap in snaps:
            code = str(snap.get("ticker") or "")
            try:
                symbol = hx.to_local_symbol(str(snap.get("thscode") or code)) if code else ""
            except ValueError:
                symbol = ""
            if not symbol:
                continue
            context = ctx.get(symbol, {})
            info = pool.get(symbol) or {}
            ref = info.get("close")
            base = info.get("name") or symbol
            name = _display_name(base, info)
            for kind, price, detail in evaluate_sell_rules(symbol, snap, context, ref):
                alerts.append({"symbol": symbol, "name": name, "kind": kind,
                               "price": price, "detail": detail})
            for kind, price, detail in evaluate_buy_rules(symbol, snap, context):
                alerts.append({"symbol": symbol, "name": name, "kind": kind,
                               "price": price, "detail": detail})
            if symbol in pool_symbols:
                for kind, price, detail in evaluate_pool_buy_rules(symbol, snap, context):
                    alerts.append({"symbol": symbol, "name": name, "kind": kind,
                                   "price": price, "detail": detail})

    for kind, price, detail in evaluate_first_board(client):
        alerts.append({"symbol": "", "name": detail.split(" ")[0], "kind": kind,
                       "price": price, "detail": detail})
    return alerts


def record_alerts(db_path: str, alerts: list[dict], day: str) -> list[dict]:
    """把提醒写入去重表，返回**首次出现**的那些（同标的同类型当天只推一次）。"""
    with storage.connect(db_path) as conn:
        return storage.record_alerts(conn, alerts, day)


def format_message(
    alerts: list[dict], db_path: str | None = None, cfg: Config | None = None
) -> tuple[str, list[str]]:
    """把提醒整理成推送的标题与正文行。

    L2 半自动：**顺带给出可直接抄进券商条件单的参数**（触发价/委托价/数量/止损/止盈）。
    有持仓时用真实成本价算止损止盈；没有则按提醒时点价格推算。
    """
    cfg = cfg or get_config()
    positions: dict[str, dict] = {}
    if db_path:
        try:
            with storage.connect(db_path) as conn:
                positions = storage.load_positions(conn)
        except Exception:  # noqa: BLE001 - 台账缺失不影响提醒
            positions = {}

    stamp = now_shanghai().strftime("%H:%M")
    lines = []
    for alert in sorted(alerts, key=lambda a: a["kind"]):
        label = KIND_LABELS.get(alert["kind"], alert["kind"])
        lines.append(f"{label}｜{alert['name']}（{alert['symbol'] or '—'}）{alert['detail']}")
        symbol = alert.get("symbol") or ""
        if not symbol or not alert.get("price"):
            continue
        try:
            if symbol in positions:
                plan = plan_sell(
                    symbol, alert["name"], positions[symbol]["avg_cost"],
                    price=alert["price"], qty=positions[symbol]["quantity"],
                    reason=alert["kind"], cfg=cfg,
                )
            else:
                plan = plan_buy(
                    symbol, alert["name"], alert["price"], reason=alert["kind"],
                    held=0, positions=len(positions), cfg=cfg,
                )
            if plan.get("text"):
                lines.append("```\n" + plan["text"] + "\n```")
        except Exception:  # noqa: BLE001
            continue
    title = f"⚡ 盘中提醒 {stamp}"
    return title, lines


def run_once(
    engine: DataEngine,
    cfg: Config | None = None,
    *,
    dry_run: bool = False,
    ignore_session: bool = False,
    client: hx.HithinkClient | None = None,
    notifier: Any = None,
) -> dict:
    """跑一轮盘中提醒。

    Args:
        notifier: 通知函数 `(title, lines) -> dict`；默认走三路并行通知。

    Returns:
        {"trading_day": bool, "in_session": bool, "hits": n, "fresh": n, "pushed": bool, "error": str}
    """
    cfg = cfg or get_config()
    today = now_shanghai().strftime("%Y-%m-%d")
    result = {"trading_day": is_trading_day(engine.db_path, today),
              "in_session": in_session(), "hits": 0, "fresh": 0, "pushed": False, "error": ""}
    if not result["trading_day"]:
        logger.info("今天不是交易日，跳过盘中提醒")
        return result
    if not result["in_session"] and not ignore_session:
        logger.info("当前不在交易时段（09:30-11:30 / 13:00-15:00），跳过")
        return result

    if client is None and not hx.available():
        logger.warning("未配置同花顺 API Key，无法获取实时行情")
        result["error"] = "未配置 API Key"
        return result

    try:
        client = client or hx.HithinkClient(api_key=cfg.hithink_api_key or None, pace=0.05)
        alerts = build_alerts(engine, client)
        result["hits"] = len(alerts)
        fresh = record_alerts(engine.db_path, alerts, today)
        result["fresh"] = len(fresh)
        if not fresh:
            logger.info(f"本轮命中 {len(alerts)} 条，但都已推送过（或没有命中）")
            return result

        title, lines = format_message(fresh, db_path=engine.db_path, cfg=cfg)
        for line in lines:
            logger.info(f"【盘中】{line}")
        if dry_run or notifier is None:
            logger.info(f"[dry-run] 将推送 {len(fresh)} 条提醒：{title}")
            return result
        notifier(title, lines)
        result["pushed"] = True
    except Exception as exc:  # noqa: BLE001 - 单轮异常不该让提醒服务退出
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(f"盘中提醒本轮异常：{result['error']}")
    return result


def alert_rows(db_path: str, limit: int = 50) -> list[dict]:
    """界面用：最近的盘中提醒（附中文标签）。"""
    with storage.connect(db_path) as conn:
        rows = storage.load_recent_alerts(conn, limit)
    for row in rows:
        row["label"] = KIND_LABELS.get(row["kind"], row["kind"])
    return rows
