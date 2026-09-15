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
import re
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

#: 集合竞价窗口（北京时间）：9:15–9:25。竞价是当天**第一份真实买卖盘**。
AUCTION_START = (9, 15)
AUCTION_END = (9, 25)
#: 9:25 之后到这里为止再拉一次：这时拿到的是竞价的**终态**（接口 phase=closed + final）
AUCTION_TAIL = (9, 30)
#: 一条推送/汇总里最多列几只竞价强度（配置 `auction_alert_max_items`，上限 10）
AUCTION_MAX_ALERTS = 5

#: "未匹配比"的两个阈值：`未匹配量 ÷ 竞价成交量` ≥ +0.5 买盘剩余占优、≤ -0.5 卖盘剩余占优。
#: 这是个**比例**（不像涨幅/量比那样随股票规模变化），所以固定成常量而不是配置项。
UNMATCHED_RATIO_STRONG = 0.5

#: 异动标签枚举（接口 `tag_name` 给的大写值）→ 中文短名。
#: 接口也可能直接给中文（"快速反弹"），两种都认 —— 见 `anomaly_tag_label`。
ANOMALY_TAGS: dict[str, str] = {
    "LIMIT_UP": "涨停",
    "LIMIT_DOWN": "跌停",
    "SHARP_RISE": "大幅上涨",
    "SHARP_FALL": "大幅下跌",
    "RAPID_RALLY": "快速反弹",
    "RAPID_DECLINE": "快速下跌",
}
#: 异动原因文本截断长度：推送要一行看得完，完整原文交易所接口随时能再取
ANOMALY_REASON_LIMIT = 120

KIND_LABELS = {
    "stop_loss": "🛑 触及止损",
    "take_profit": "🎯 触及止盈",
    "break_ma5": "📉 跌破 5 日线",
    "limit_up_open": "🔓 涨停打开",
    "break_high": "🚀 放量突破20日高",
    "first_board": "🔥 首板厚封单",
    "pullback_ma5_buy": "🎯 池内回踩买点",
    # 竞价强度：强/弱分开成两种 kind，去重键 (date, symbol, kind) 天然一天只推一次
    "auction_strong": "⚡ 竞价强度",
    "auction_weak": "⚡ 竞价走弱",
}

# 异动的 kind 按标签区分（同一只票同一天同一标签只推一次；标签变了可以再推一条）
for _tag_code in ANOMALY_TAGS:
    KIND_LABELS[f"anomaly_{_tag_code.lower()}"] = "⚡ 异动"


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
    engine: DataEngine,
    client: hx.HithinkClient,
    scan_market: bool = False,
    cfg: Config | None = None,
) -> list[dict]:
    """跑一轮规则，返回本轮命中的提醒（**未去重**）。

    两类"额外"提醒也在这里并进来（共用同一次循环，不再加定时器）：
    - **竞价强度**（只在 9:15–9:30 有数据，见 `auction_alerts`）；
    - **当日异动**（全市场一条请求 + 本地按自己的票过滤，见 `anomaly_alerts`）。
    """
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

    cfg = cfg or get_config()
    # 竞价强度：9:15–9:25 每分钟一次 + 9:25 后的终态一次（不在窗口内则一次请求都不发）
    alerts.extend(auction_alerts(client, db_path=engine.db_path, cfg=cfg))
    # 当日异动：全市场一条请求，本地只留自己的票
    alerts.extend(anomaly_alerts(client, db_path=engine.db_path, cfg=cfg))

    for kind, price, detail in evaluate_first_board(client):
        alerts.append({"symbol": "", "name": detail.split(" ")[0], "kind": kind,
                       "price": price, "detail": detail})
    # 涨停类提醒补"为什么涨停"（按需：只有真命中涨停打开/首板才去拉涨停池）
    enrich_limit_up_reasons(client, alerts)
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


# ── 集合竞价强度 / 当日异动 ──


def _num(value: Any) -> float | None:
    """宽松转 float（空/停牌/字段缺失一律 None）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hhmm(now: datetime) -> tuple[int, int]:
    return (now.hour, now.minute)


def auction_window(now: datetime | None = None) -> bool:
    """现在是不是**集合竞价进行中**（9:15–9:25）。"""
    now = now or now_shanghai()
    return AUCTION_START <= _hhmm(now) < AUCTION_END


def auction_tail(now: datetime | None = None) -> bool:
    """9:25–9:30：竞价已结束，这时候取到的 `phase=closed / data_status=final` 是**终态**。"""
    now = now or now_shanghai()
    return AUCTION_END <= _hhmm(now) < AUCTION_TAIL


def auction_fetch_window(now: datetime | None = None) -> bool:
    """该不该去取竞价数据：9:15–9:30（含 9:25 后取终态那段）。

    为什么把 9:25–9:30 也算进来：竞价终态（谁高开放量、谁低开走弱）是收盘前都在用的信息，
    而接口在 9:25 之后仍然返回 `phase=closed + data_status=final`，取的还是同一天的数。
    """
    return auction_window(now) or auction_tail(now)


def auction_fields(row: dict, name: str = "") -> dict:
    """竞价快照行 → 干净的展示字段（**负数/缺失的未匹配量一律当"没有"**）。

    接口**只给一个带符号的未匹配量**（没有买/卖两个数），实测 100 只里
    **>0 的 52 只、<0 的 45 只、恰好 -1 的 1 只**，所以要这么读：
    - `> 0` = **买盘剩余**（买强）；`< 0` = **卖盘剩余**（卖强）；两者都是真实力量对比；
    - **恰好 -1 当"缺失"**（茅台就是 -1）—— 那不是"卖压 1 手"；
    - 另外算一个"未匹配比" `未匹配量 ÷ 竞价成交量`，用于打分与展示。
    """
    unmatched = _num(row.get("auction_unmatched"))
    if unmatched == -1.0:
        unmatched = None                 # -1 是"未提供"，不是卖压 1 手
    volume = _num(row.get("auction_volume"))
    symbol = ""
    thscode = str(row.get("thscode") or "")
    if thscode:
        try:
            symbol = hx.to_local_symbol(thscode)
        except ValueError:
            symbol = ""
    amount = _num(row.get("auction_amount"))
    ratio = None
    if unmatched is not None and volume:
        ratio = unmatched / volume
    return {
        "symbol": symbol,
        "name": str(row.get("name") or name or symbol),
        "price": _num(row.get("auction_price")),
        "pct": _num(row.get("auction_pct")),
        "volume_ratio": _num(row.get("auction_volume_ratio")),
        "turnover_pct": _num(row.get("auction_turnover_pct")),
        "yesterday_ratio": _num(row.get("auction_yesterday_ratio_pct")),
        # 带符号的未匹配量（正=买盘剩余、负=卖盘剩余）；-1/缺失 → None
        "unmatched": unmatched,
        "volume": volume,
        "unmatched_ratio": ratio,
        "amount": amount,
        "pre_close": _num(row.get("pre_close_price")),
    }


def auction_card_text(fields: dict) -> str:
    """股票池卡片上那一行：`竞价 +3.2% 量比2.8 买盘剩余1200手`。

    - 拿不到涨跌幅 → 空串（整行不显示）；
    - 未匹配量缺失（-1）→ 只显示涨幅与量比；
    - 未匹配量为正 = 买盘剩余、为负 = 卖盘剩余（数值用绝对值 + 中文标注，别让用户猜符号）。
    """
    pct = fields.get("pct")
    if pct is None:
        return ""
    text = f"竞价 {pct:+.1f}%"
    if fields.get("volume_ratio") is not None:
        text += f" 量比{fields['volume_ratio']:.1f}"
    unmatched = fields.get("unmatched")
    if unmatched:
        side = "买盘剩余" if unmatched > 0 else "卖盘剩余"
        text += f" {side}{abs(unmatched):,.0f}手"
    return text


def auction_detail(fields: dict, score: int | None = None) -> str:
    """推送里的那句：原始数值 + 分数都写出来（用户要能看到"为什么给这个分"）。

    形如：`+3.21% 量比2.80 买盘剩余1,200手 成交额5,200万 未匹配比+0.62（分5）`。
    """
    parts = []
    if fields.get("pct") is not None:
        parts.append(f"{fields['pct']:+.2f}%")
    if fields.get("volume_ratio") is not None:
        parts.append(f"量比{fields['volume_ratio']:.2f}")
    unmatched = fields.get("unmatched")
    if unmatched:
        side = "买盘剩余" if unmatched > 0 else "卖盘剩余"
        parts.append(f"{side}{abs(unmatched):,.0f}手")
    if fields.get("amount") is not None:
        parts.append(f"成交额{fields['amount'] / 1e4:,.0f}万")
    if fields.get("unmatched_ratio") is not None:
        parts.append(f"未匹配比{fields['unmatched_ratio']:+.2f}")
    if fields.get("turnover_pct") is not None:
        parts.append(f"换手{fields['turnover_pct'] * 100:.2f}%")
    if fields.get("yesterday_ratio") is not None:
        parts.append(f"昨日量比{fields['yesterday_ratio']:.2f}")
    text = " ".join(parts) or "竞价数据不完整"
    return f"{text}（分{score}）" if score is not None else text


def auction_score(
    fields: dict, cfg: Config | None = None
) -> tuple[int | None, dict]:
    """给一只票的竞价**打分**（取代原来的"涨幅 ≥2% 且 量比 ≥2"那个 AND 判据）。

    为什么改成分级：实测 100 只里"涨幅≥2% **且** 量比≥2 **且** 成交额≥500万"是 **0 只** ——
    AND 判据太严，等于永远不提醒。改成分级之后，只要"高开或放量或买盘占优"就有分。

    计分表（越强分越高，负数表示低开/卖压）：
    - `+2` 竞价涨幅 ≥ `min_pct`（默认 +2.0%）；`+1` 涨幅 ≥ 它的一半（默认 +1.0%）
    - `+2` 量比 ≥ `min_ratio`（默认 2.0）；`+1` 量比 ≥ 它的 0.75（默认 1.5）
    - `+1` 未匹配比 ≥ +0.5（买盘剩余占优）；`-1` 未匹配比 ≤ -0.5（卖盘剩余占优）
    - `+1` 竞价成交额 ≥ `min_amount`（默认 500 万）
    - **低开按对称规则扣分**：涨幅 ≤ `-min_pct` 记 `-2`、≤ `-min_pct/2` 记 `-1`。
      为什么要补这一条：原口径只给"买盘那侧"加分（成交额那 1 分一定会拿），
      于是分数最低只能到 `0`，而"竞价弱"的门限是 `-2` —— **那条规则永远不可能触发**。
      补上对称的扣分之后，`≤ -2` 才真正表示"低开且卖盘占优"，与"低开/卖压"的说法一致。

    **成交额不到下限 → 返回 `(None, …)`：不参与评分**。竞价量太小时
    "未匹配量 ÷ 成交量"会爆表（实测某小盘股 +29.46），拿它判断强弱等于把噪音当信号。
    **正好等于门限也算命中**（判据用 `>=` / `<=`，与配置里写的数字一致）。

    Returns:
        `(分数 or None, 明细)`；明细里逐项写清哪一档命中、以及三个原始数值 ——
        界面与推送都要显示原始数（只给一个分，用户没法核对）。
    """
    cfg = cfg or get_config()
    try:
        min_pct = float(getattr(cfg, "auction_alert_min_pct", 2.0) or 2.0)
        min_ratio = float(getattr(cfg, "auction_alert_min_volume_ratio", 2.0) or 2.0)
        min_amount = float(getattr(cfg, "auction_alert_min_amount", 5e6) or 5e6)
    except (TypeError, ValueError):     # 配置被手改成怪值：按默认口径走，别抛
        min_pct, min_ratio, min_amount = 2.0, 2.0, 5e6

    breakdown: dict = {"reasons": []}
    amount = fields.get("amount")
    if amount is None or amount < min_amount:
        breakdown["skipped"] = "成交额不足"
        return None, breakdown                   # 成交额不达标：不参与评分

    score = 0
    pct = fields.get("pct")
    if pct is not None:
        if pct >= min_pct:
            score += 2
            breakdown["reasons"].append(f"高开{pct:+.2f}%")
        elif pct >= min_pct / 2:
            score += 1
            breakdown["reasons"].append(f"略高开{pct:+.2f}%")
        elif pct <= -min_pct:
            score -= 2                      # 低开：与"高开 +2"对称（见 docstring）
            breakdown["reasons"].append(f"低开{pct:+.2f}%")
        elif pct <= -min_pct / 2:
            score -= 1
            breakdown["reasons"].append(f"略低开{pct:+.2f}%")
    ratio = fields.get("volume_ratio")
    if ratio is not None:
        if ratio >= min_ratio:
            score += 2
            breakdown["reasons"].append(f"放量{ratio:.2f}")
        elif ratio >= min_ratio * 0.75:
            score += 1
            breakdown["reasons"].append(f"略放量{ratio:.2f}")
    unmatched_ratio = fields.get("unmatched_ratio")
    if unmatched_ratio is not None:
        if unmatched_ratio >= UNMATCHED_RATIO_STRONG:
            score += 1
            breakdown["reasons"].append("买盘占优")
        elif unmatched_ratio <= -UNMATCHED_RATIO_STRONG:
            score -= 1
            breakdown["reasons"].append("卖盘占优")
    score += 1                                   # 成交额已过门槛（上面提前返回了）
    breakdown["reasons"].append(f"成交额{amount / 1e4:,.0f}万")
    breakdown.update({"score": score, "pct": pct, "volume_ratio": ratio,
                      "unmatched_ratio": unmatched_ratio})
    return score, breakdown


def auction_verdict(score: int | None, cfg: Config | None = None) -> str:
    """分数 → `auction_strong` / `auction_weak` / 空串（不提示）。

    弱的分门限 = 强门限取负再放宽一档（默认 3 → **-2**）。
    注意：**弱只对持仓提示**（低开是"我已经买了"的风险），这一条在 `auction_alerts` 里判，
    因为这里拿不到持仓集合。
    """
    if score is None:
        return ""
    cfg = cfg or get_config()
    try:
        min_score = int(getattr(cfg, "auction_alert_min_score", 3) or 3)
    except (TypeError, ValueError):
        min_score = 3
    if score >= min_score:
        return "auction_strong"
    if score <= -max(1, min_score - 1):
        return "auction_weak"
    return ""


def anomaly_tag_label(raw: Any) -> tuple[str, str]:
    """`tag_name` → `(kind 后缀, 中文短名)`；枚举与中文两种写法都认。

    接口里 `tag_name` 既可能是枚举（`RAPID_RALLY`）也可能是中文（`快速反弹`），
    猜错会让"标签过滤"和"去重 kind"都失效，所以两种都认、认不出用原样文本当标签。
    """
    text = str(raw or "").strip()
    if not text:
        return "other", "异动"
    upper = text.upper()
    if upper in ANOMALY_TAGS:
        return upper.lower(), ANOMALY_TAGS[upper]
    for code, label in ANOMALY_TAGS.items():
        if text == label:
            return code.lower(), label
    slug = re.sub(r"[^0-9a-z_]+", "_", upper.lower()).strip("_")
    return (slug or "other"), text


def alert_universe(db_path: str, cfg: Config | None = None) -> dict[str, str]:
    """提醒关心的**全部标的**：股票池 + 自选 + 持仓 → `{symbol: 名称}`（去重）。

    为什么三类都要：竞价强度与异动只对"用户关心的票"有意义 ——
    池子是要买的、持仓是已经买了的、自选是盯着的；漏掉哪一类用户都会当成 bug。
    名称以 `stock_basic` 为准（池子/自选里存的是建池那天的名字，可能已经改名）。
    """
    from laoa_trader import pool as pool_mod

    cfg = cfg or get_config()
    symbols: dict[str, str] = {}
    for row in pool_mod.load_pool(db_path):
        symbol = str(row.get("symbol") or "")
        if symbol:
            symbols.setdefault(symbol, str(row.get("name") or ""))
    with storage.connect(db_path) as conn:
        for row in storage.load_watchlist(conn):
            symbol = str(row.get("symbol") or "")
            if symbol:
                symbols.setdefault(symbol, str(row.get("name") or ""))
        for symbol, row in storage.load_positions(conn).items():
            symbols.setdefault(str(symbol), str(row.get("name") or ""))
        wanted = list(symbols)
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            for symbol, name in conn.execute(
                f"SELECT symbol, name FROM stock_basic WHERE symbol IN ({placeholders})",
                wanted,
            ):
                if name:
                    symbols[str(symbol)] = str(name)
    return {s: n for s, n in symbols.items() if s}


def position_symbols(db_path: str) -> set[str]:
    """持仓代码集合：**"竞价弱"只对真持仓提示**（低开是"我已经买了"的风险，
    没买的票低开跟我没关系；池子里的票低开也不该在 9:20 就报警）。"""
    with storage.connect(db_path) as conn:
        return {str(symbol) for symbol in storage.load_positions(conn)}


def auction_alerts(
    client: hx.HithinkClient,
    db_path: str | None = None,
    cfg: Config | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """竞价强度提醒（按**分数**排强弱）；不在竞价窗口、或没开这个功能时返回空列表。

    口径（替换了早期那个"涨幅≥2% 且 量比≥2"的 AND 判据 —— 实测那种组合在 100 只里是 0 只）：
    - 每只票按 `auction_score()` 打分（高开/放量/买盘剩余/成交额各给分，卖盘剩余扣分）；
    - **分 ≥ `auction_alert_min_score`（默认 3）→ 竞价强**；
    - **分 ≤ -(门限-1)（默认 -2）→ 竞价弱**，而且**只对持仓提示**
      （低开是"我已经买了"的风险；没买的票低开与我无关）；
    - 成交额不到 `auction_alert_min_amount`（500 万）→ **不参与评分**；
    - 按分数（同分按 |涨幅|）排序，**最多 `auction_alert_max_items` 只（默认 5、上限 10）**。

    节奏由调用方定：`run_once` 每 `intraday_interval` 跑一轮，
    所以 9:15–9:25 之间是每分钟一次，9:25 之后那一轮拿到的是**终态**。
    失败只记日志（竞价数据缺一轮不影响其它提醒）。
    """
    cfg = cfg or get_config()
    if not bool(getattr(cfg, "intraday_auction", True)):
        return []
    if not auction_fetch_window(now):
        return []            # 不在 9:15–9:30：一次请求都不发（接口这时也没有竞价数据）
    db_path = db_path or cfg.db_path
    universe = alert_universe(db_path, cfg)
    if not universe:
        return []
    code_map: dict[str, str] = {}
    for symbol in universe:
        try:
            code_map[hx.to_thscode(symbol)] = symbol
        except ValueError:
            continue
    if not code_map:
        return []
    try:
        data = client.auction_snapshot(list(code_map))
    except Exception as exc:  # noqa: BLE001 - 竞价取不到不该影响其它提醒
        logger.warning(f"竞价快照获取失败（本轮跳过竞价提醒）：{exc}")
        return []
    phase = str(data.get("phase") or "")
    held = position_symbols(db_path)
    try:
        max_items = int(getattr(cfg, "auction_alert_max_items", AUCTION_MAX_ALERTS)
                        or AUCTION_MAX_ALERTS)
    except (TypeError, ValueError):
        max_items = AUCTION_MAX_ALERTS
    max_items = min(max(max_items, 1), 10)       # 上限 10：再多就成刷屏了
    hits: list[dict] = []
    for row in data.get("item") or []:
        symbol = code_map.get(str(row.get("thscode") or ""))
        if not symbol:
            continue
        fields = auction_fields(row, name=universe.get(symbol, ""))
        score, breakdown = auction_score(fields, cfg)
        kind = auction_verdict(score, cfg)
        if not kind:
            continue
        if kind == "auction_weak" and symbol not in held:
            continue                             # 弱只对持仓提示
        hits.append({
            "symbol": symbol,
            "name": fields["name"] or symbol,
            "kind": kind,
            "price": fields["price"],
            "score": score,
            "detail": auction_detail(fields, score),
            "_sort": (int(score if score is not None else 0),
                      abs(fields.get("pct") or 0.0)),
        })
    # 按分数排（同分按涨幅绝对值），只留前 N —— 提醒条数上限就是这里生效的
    hits.sort(key=lambda item: item["_sort"], reverse=True)
    for hit in hits:
        hit.pop("_sort", None)
    if hits:
        logger.info(f"竞价强度命中 {len(hits)} 只（phase={phase or '—'}，"
                    f"取前 {max_items} 只推送）")
    return hits[:max_items]


def fetch_auction(
    db_path: str,
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """取一次竞价快照 → `{symbol: 展示字段}`（界面卡片用；失败/非窗口返回空字典）。

    与 `auction_alerts` 共用 `auction_fields`，所以卡片上的数字与推送里的数字**一定一致**
    （同一个接口、同一套"负数当没有"的处理）。
    """
    cfg = cfg or get_config()
    if not bool(getattr(cfg, "intraday_auction", True)):
        return {}
    if not auction_fetch_window(now):
        return {}                        # 不在窗口里：不发请求
    universe = alert_universe(db_path, cfg)
    if not universe:
        return {}
    code_map: dict[str, str] = {}
    for symbol in universe:
        try:
            code_map[hx.to_thscode(symbol)] = symbol
        except ValueError:
            continue
    if not code_map:
        return {}
    client = client or hx.HithinkClient(
        api_key=getattr(cfg, "hithink_api_key", "") or None, pace=0.05
    )
    try:
        data = client.auction_snapshot(list(code_map))
    except Exception as exc:  # noqa: BLE001 - 取不到就不显示那一行，界面照常
        logger.info(f"竞价快照取数失败（卡片上不显示竞价行）：{exc}")
        return {}
    out: dict[str, dict] = {}
    for row in data.get("item") or []:
        symbol = code_map.get(str(row.get("thscode") or ""))
        if not symbol:
            continue
        fields = auction_fields(row, name=universe.get(symbol, ""))
        # 顺手把分数算好带上：界面要"原始数 + 分"一起显示（只给一个分用户没法核对）
        score, breakdown = auction_score(fields, cfg)
        fields["score"] = score
        fields["reasons"] = breakdown.get("reasons", [])
        out[symbol] = fields
    return out


def anomaly_alerts(
    client: hx.HithinkClient,
    db_path: str | None = None,
    cfg: Config | None = None,
) -> list[dict]:
    """当日异动里**只挑自己的票**（池子 / 自选 / 持仓）。

    为什么拉全市场再本地过滤：`anomaly-analysis-list` **一条请求**就给全市场异动，
    而逐只问（`anomaly-analysis-stock`）要几百次 —— 池子+自选+持仓几十只也够呛。
    接口失败只记日志：异动提醒缺一轮，不该影响止损止盈这些更重要的提醒。
    """
    cfg = cfg or get_config()
    if not bool(getattr(cfg, "intraday_anomaly", True)):
        return []
    db_path = db_path or cfg.db_path
    universe = alert_universe(db_path, cfg)
    if not universe:
        return []
    wanted_tags = [str(tag).strip().upper() for tag in (cfg.anomaly_alert_tags or [])]
    try:
        rows = client.anomaly_list(wanted_tags or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"异动列表获取失败（本轮跳过异动提醒）：{exc}")
        return []
    hits: list[dict] = []
    for row in rows or []:
        thscode = str(row.get("thscode") or "")
        if not thscode:
            continue
        try:
            symbol = hx.to_local_symbol(thscode)
        except ValueError:
            continue
        if symbol not in universe:
            continue                       # **只推自己的票**（池外异动一律不打扰）
        tag = str(row.get("tag_name") or "")
        suffix, label = anomaly_tag_label(tag)
        if wanted_tags and suffix.upper() not in wanted_tags and tag.upper() not in wanted_tags:
            continue                       # 配置里只关心某些标签
        reason = _clip_text(str(row.get("analysis_content") or ""), ANOMALY_REASON_LIMIT)
        detail = f"{label} · {reason}" if reason else label
        hits.append({
            "symbol": symbol,
            "name": str(row.get("stock_name") or universe.get(symbol) or symbol),
            "kind": f"anomaly_{suffix}",
            "price": None,
            "detail": detail,
        })
    if hits:
        logger.info(f"异动命中 {len(hits)} 只（全市场 {len(rows or [])} 条里属于自己票的）")
    return hits


def _clip_text(text: str, limit: int) -> str:
    """长文本截断（加省略号）：异动原因是整段新闻，推送一行不能太长。"""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: 需要补"涨停原因"的提醒类型（这几条本来只说"涨停打开/首板"，用户看不出为什么涨停）
LIMIT_UP_KINDS = ("limit_up_open", "first_board")


def enrich_limit_up_reasons(client: hx.HithinkClient, alerts: list[dict]) -> None:
    """给涨停类提醒补上"为什么涨停"（**按需取**：只有真命中才去拉涨停池）。

    为什么要按需：`limit_up_pool()` 会自动翻页（一天几十上百条），
    而绝大多数轮次根本没有涨停类提醒 —— 每轮都拉等于白花配额。
    原因取不到就保持原样（少一句话，不影响这条提醒本身的价值）。
    """
    targets = {
        alert["symbol"] for alert in alerts
        if alert.get("kind") in LIMIT_UP_KINDS and alert.get("symbol")
        # 只跳过"已经补过原因"的（详情里本来就有原因的首板提醒不用再补）；
        # 注意不能拿"详情里有'涨停'两个字"当判据 —— "涨停打开"本来就有这两个字
        and "涨停原因" not in str(alert.get("detail") or "")
    }
    if not targets:
        return
    try:
        rows = client.limit_up_pool()
    except Exception as exc:  # noqa: BLE001 - 补原因失败不影响提醒本身
        logger.info(f"涨停池取不到（本轮不给涨停类提醒补原因）：{exc}")
        return
    reasons: dict[str, str] = {}
    for row in rows or []:
        code = str(row.get("thscode") or row.get("ticker") or "")
        if not code:
            continue
        try:
            symbol = hx.to_local_symbol(code)
        except ValueError:
            continue
        reason = str(row.get("limit_up_reason") or "").strip()
        if reason:
            reasons.setdefault(symbol, reason)
    for alert in alerts:
        if alert.get("kind") in LIMIT_UP_KINDS and alert.get("symbol") in reasons:
            alert["detail"] = f"{alert.get('detail') or ''}，涨停原因：{reasons[alert['symbol']]}"


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
    # 竞价窗口（9:15–9:25）也要跑：那时还没到 9:30，但它正是"真实买卖盘"最有价值的十分钟。
    # 9:25–9:30 那一轮同样放行 —— 拿到的是竞价**终态**（接口 phase=closed/final）。
    auction_on = bool(getattr(cfg, "intraday_auction", True))
    in_window = result["in_session"] or (auction_on and auction_fetch_window())
    if not in_window and not ignore_session:
        logger.info("当前不在交易时段（含集合竞价窗口），跳过")
        return result

    if client is None and not hx.available():
        logger.warning("未配置同花顺 API Key，无法获取实时行情")
        result["error"] = "未配置 API Key"
        return result

    try:
        client = client or hx.HithinkClient(api_key=cfg.hithink_api_key or None, pace=0.05)
        alerts = build_alerts(engine, client, cfg=cfg)
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
