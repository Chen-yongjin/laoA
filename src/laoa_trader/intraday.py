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

另有两条**只对自己持仓的票**发的"做T近似提示"（`t_high` / `t_low`，见下一节）——
它们与上面 6 条**不是一类东西**：上面 6 条是有历史数据可回测的规则，这两条没有。

> **已下线**：原来的"打板提醒"（kind `first_board`：实时涨停池里"首板 + 封单 ≥5000 万"）
> 已于本轮删除 —— 依赖**封单额**的打板信号没有可验证的边际（见策略成绩单那套口径），
> 用户也明确说"没有意义"。`limit_up_pool` 的同步、连板数与封单额字段**都还在**
> （池子要显示"涨停：2 连板 · 原因"，公式里的 `连板()` / `涨停天数()` 也依赖它），
> 只是不再用它产生提醒。

持仓做T 的近似提示（`t_high` / `t_low`）
---------------------------------------
| 类型 | 规则 | 触发 |
|---|---|---|
| 提示 | `t_high`（近似·T高抛） | 现价较昨收涨 ≥ `t_high_min_gain_pct`（2.0%）**且** 从今日最高回落 ≥ `t_high_pullback_pct`（1.5%）**且** 现价仍在分时均价上方 |
| 提示 | `t_low`（近似·T低吸） | 现价较昨收跌 ≥ `t_low_min_drop_pct`（2.0%）**且** 从今日最低反弹 ≥ `t_low_rebound_pct`（1.0%）**且** 未跌破（或刚收回）分时均价 |

**A 股 T+1 是这两条提示的地基**：当天买的票当天不能卖，所以持仓股一天只能做两种"T"——
**反T（先卖后买）**：卖部分**昨仓** → 当天更低时接回等量，成本降低、收盘股数不变；
**正T（先买后卖）**：用**可用现金**低吸 → 当天反弹后卖出**等量的昨仓**，收盘股数不变。
所以提示里绝不会出现"现在买/现在卖"这种裸指令，而且一定带「部分」——
程序不知道用户今天又买过多少、有多少被冻结，**能卖多少只有券商知道**。
`opened_at` 是今天的持仓**不发高抛（反T，要先卖）提示**（当天新建仓 T+1 不可卖），
但可以发低吸提示（正T 用的是现金，不卖出任何股票）。

> ⚠️ **这两条提示是近似的、且无法回测**：只有 60 秒一张的行情快照，没有分时/逐笔/Level-2，
> 更没有历史的分时数据来验证阈值 —— 这四个阈值是**手工设定的起点，不是拟合出来的**。
> 它们**不参与选股、也不会混进当天的池子推送**，只走盘中提醒那几条通道。
> 开关：`intraday_t`（**默认 false = 关**，用户拍板；想开就写 `intraday_t = true` 或环境变量 `INTRADAY_T=1`）。详见 README「持仓做T（近似提示）」。

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
import time
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from laoa_trader.config import (
    AUCTION_BOARD_LABELS,
    AUCTION_BOARDS,
    AUCTION_SCAN_GRACE_MIN,
    AUCTION_SCORE_RANGE,
    Config,
    get_config,
    split_scan_at,
)
from laoa_trader.data import hithink as hx
from laoa_trader import clock
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
#: 一条汇总里最多列几只（配置 `auction_alert_max_items`，**上限 50**）；
#: 配置读不出来时的兜底值（与 `Config` 的默认一致）
AUCTION_MAX_ALERTS = 10
#: 全市场扫描的**批大小**（接口上限 100）：5573 只 → 56 批
AUCTION_SCAN_BATCH = hx.AUCTION_BATCH
#: 批与批之间的间隔（秒）：接口按"每分钟多少次"限流，串行 + 0.3 秒间隔最稳。
#: 56 批 × (0.3 秒 + 请求耗时) ≈ 20~30 秒 —— 这就是"扫描在后台跑、不每分钟扫"的由来。
AUCTION_SCAN_PACE = 0.3
#: 全市场快照分页模式的页大小（拿不到本地代码表时的兜底取法）
AUCTION_SYMBOL_PAGE = 10000
#: 落库（以及详情里列出来）的命中条数上限：正常只有几十只，但把阈值调到极松时
#: 可能几百上千 —— 详情弹窗每 5 秒重建一次，几千行会把界面拖慢，所以留前 300 条，
#: 真实命中总数另存一列（`auction_scan.total`），界面上照样能说清"共命中多少只"
AUCTION_SCAN_STORE_LIMIT = 300

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

#: "选股完成"这一条消息的类型（**不是盘中提醒**：它由 `scheduler.run_daily()` 在
#: 建池成功之后写入）。用户 2026-09-18 要求"通知仿 QQ：盘中提醒与选股推送都进同一个
#: 消息列表"，所以它住在同一张表（`intraday_alert`）里、只是 kind 不同。
KIND_POOL = "pool"

KIND_LABELS = {
    KIND_POOL: "📈 选股完成",
    "stop_loss": "🛑 触及止损",
    "take_profit": "🎯 触及止盈",
    "break_ma5": "📉 跌破 5 日线",
    "limit_up_open": "🔓 涨停打开",
    "break_high": "🚀 放量突破20日高",
    "pullback_ma5_buy": "🎯 池内回踩买点",
    # 竞价强度：强/弱分开成两种 kind，去重键 (date, symbol, kind) 天然一天只推一次
    "auction_strong": "⚡ 竞价强度",
    "auction_weak": "⚡ 竞价走弱",
    # 持仓做T 的**近似**提示：标签里直接写「近似」，用户在推送/浮窗/提醒页一眼能分辨
    # （它们与上面那些有历史数据支撑的规则不是一回事，见模块 docstring）
    "t_high": "🔁 近似·T高抛（反T：先卖后买）",
    "t_low": "🔁 近似·T低吸（正T：先买后卖）",
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
    # 真正的实现搬到了 `clock.now_cn()`（那个模块存在的理由见它的 docstring）：
    # 这里保留这个函数名，是因为项目里到处都在用它（界面、调度、提醒……），
    # 改名只会制造一次没有收益的大范围改动。
    return clock.now_cn(now)


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


def _watch_label(note: str, cost: float | None) -> str:
    """自选股的展示标签：`自选（龙头，成本 12.40）`。

    把备注与成本写进提醒里，是为了让收到卡片的人**一眼知道为什么盯它**：
    备注是用户自己写的理由（"龙头""消息面"），成本则直接对应止损止盈的基准。

    标签固定写「自选」：这条提醒本来就出自自选表，写「策略+自选」等于在一个
    自选名单里再解释一次"它也是自选"（2026-09-23 主人："为什么要+自选 什么策略
    跑出来的 直接记录策略名称 只有用户自己输入的才能算自选来源"）；是哪条策略
    选出来的，看「自选股池」表的来源列。
    """
    head = "自选"
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
    """取最近 N 个交易日推送过的信号（作为卖出/风控观察池）。

    `allowed_strategies` 是**老调用方**留下的窄化参数（当年按启用的策略组过滤）；
    2026-09-18 策略组机制删掉之后，选股只产出公式标的，所以现在一律传 None ——
    参数保留只为兼容，传进来也照旧转给 storage（那是 SQL 层的过滤，没有副作用）。
    """
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
        selection: **2026-09-18 起不再使用**（原来按启用的策略组窄化观察面）。
            策略组机制已删；池子里的标的现在一律来自勾选的公式与自选股。
            None 时按配置解析（配置里两组都空 = 全选）。
        cfg: 配置（解析 selection 用）。

    Returns:
        ({symbol: {...}}, 池内符号集合)
    """
    from laoa_trader import pool as pool_mod

    allowed = None          # 见函数 docstring：按策略组窄化这条逻辑已经删掉

    cfg = cfg or get_config()
    targets: dict[str, dict] = {}
    # 已关闭监控的持仓：**从观察面里整体剔掉**，不管它是不是池内标的/自选
    # （优先级见 `monitor_off_symbols` 的说明）
    off = monitor_off_symbols(db_path)
    # 池子里的每一行都盯：当年这里按"启用的策略组"过滤，而策略组机制已经删掉，
    # 现在进池的只可能是勾选的公式标的与自选股 —— 没有"该不该盯"这一层了。
    pool_rows = list(pool_mod.load_pool(db_path))
    pool_symbols = {row["symbol"] for row in pool_rows}
    for row in pool_rows:
        targets[row["symbol"]] = {
            "name": row.get("name"), "signal_date": row.get("date"), "close": None,
            "source": "pool", "strategy": row.get("strategy"),
        }

    # ── 自选股：**无论策略池是否为空都要盯** ──
    # 为什么直接从 watchlist 表读、而不是只依赖池子：用户刚加的自选要立刻生效，
    # 不必等到今晚重新建池；一条公式都没勾（池子只剩自选）时也一样盯。
    if getattr(cfg, "watchlist_in_pool", True):
        with storage.connect(db_path) as conn:
            watch_entries = storage.load_watchlist(conn, enabled_only=True)
            positions = storage.load_positions(conn)
        for entry in watch_entries:
            symbol = entry["symbol"]
            note = (entry.get("note") or "").strip()
            held = positions.get(symbol)
            info = targets.get(symbol) or {}
            label = _watch_label(note, held["avg_cost"] if held else None)
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
        return _drop_unmonitored(targets, pool_symbols, off)
    for symbol, info in recent_signal_symbols(db_path, days, allowed).items():
        targets.setdefault(symbol, {**info, "source": "signal"})
    return _drop_unmonitored(targets, pool_symbols, off)


def _drop_unmonitored(
    targets: dict[str, dict], pool_symbols: set[str], off: set[str]
) -> tuple[dict[str, dict], set[str]]:
    """把"已关闭监控"的持仓从观察面（与池内标记）里剔掉。

    只在**有东西要剔**时才建新容器：这是每一轮都要走的路（调度器 60 秒一拍），
    空集合时原样返回，省掉两次全量拷贝。
    """
    if not off:
        return targets, pool_symbols
    return (
        {s: info for s, info in targets.items() if s not in off},
        {s for s in pool_symbols if s not in off},
    )


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
    if last > high20 and MIN_BREAK_GAIN <= gain <= MAX_BREAK_GAIN \
            and turnover >= avg_turnover * 0.5:
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


# ── 持仓做T 的近似提示（`t_high` / `t_low`）──
#
# 语义前提（A 股 **T+1**，这是整个功能的地基，改代码前先把这三行读一遍）：
#   当天买入的股票**当天不能卖**，所以"现在买、等会儿卖"的裸指令是**不合法**的。
#   一个持仓股当天能做的只有两件事：
#     反T（先卖后买）：卖掉**昨仓**的一部分 → 当天更低时买回等量 → 成本降低、收盘股数不变；
#     正T（先买后卖）：用**可用现金**低吸      → 当天反弹后卖出**等量的昨仓** → 收盘股数不变。
#   所以本模块发出的每一条都必须写成这两种之一，而且必须带「部分」——
#   程序既不知道用户今天又买过多少（那部分不能卖），也不知道有多少股被挂单冻结，
#   **能卖多少只有券商的持仓页说了算**。
#
# 数据来源与"近似"到什么程度（不要在这里许下做不到的承诺）：
#   只有每 60 秒一张的实时快照（`client.snapshot`），**没有分时、逐笔、Level-2**，
#   更没有历史分时数据可以回测 —— 所以这四个阈值是**手工设定的起点，不是拟合出来的**，
#   提示里的价格也只是一个瞬时快照值，具体成交价要用户自己用限价单去争取。
#   这些话在 README、模块 docstring、`KIND_LABELS` 的标签里都写了一遍（用户看得到的地方）。

#: 做T提示的两个 kind。与上面 6 条规则**故意分开**：标签里带「近似」，
#: 用户一眼能分辨"这是快照规则给的提示"还是"有历史数据支撑的信号"。
T_HIGH_KIND = "t_high"
T_LOW_KIND = "t_low"
T_KINDS: tuple[str, ...] = (T_HIGH_KIND, T_LOW_KIND)

#: 持仓页「今日T提示」列里的短文本（**整句话**放在 tooltip 里，见 `t_hint_tooltip`）
T_HINT_SHORT_LABELS: dict[str, str] = {
    T_HIGH_KIND: "高抛（近似）",
    T_LOW_KIND: "低吸（近似）",
}

#: 低吸判据里"站上均价"的容差：现价不比均价低过这个比例就算"已收回均价"。
#: 为什么给容差而不是硬要求 `现价 >= 均价`：60 秒一张的快照价格会在均价上下反复跳，
#: 严格判据会把"刚刚收回"的那一分钟读成"还在水下" —— 而那正是最该提示的一分钟。
#: 0.5% 与其它阈值一样是**手工设定**的（没有分时数据，无从拟合）。
T_LOW_VWAP_TOL = 0.005

#: 均价合理性校验的容差：算出来的分时均价必须落在当日 [最低, 最高] 区间里（留 0.5% 余量）。
T_VWAP_RANGE_TOL = 0.005


#: `opened_at` 的宽松写法：写入方（`storage.upsert_position` 的 `_now()`）给的是
#: `2026-09-16 09:31:00`；`2026/9/16`、`2026.09.16` 这两种是**防老库/手工导入**的假设
#: （没有实测样本，多认几种写法的代价只是几行正则）。
_OPENED_AT_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def _day_of(raw: Any) -> str:
    """把 `opened_at` 之类的文本取成 `YYYY-MM-DD`（取不出来返回空串）。

    为什么手写而不用 `datetime.fromisoformat`：这一列是自由文本（程序自己写的是
    `2026-09-16 09:31:00`，而老库/手工导入可能是空串或别的写法），
    而**解析失败绝不能抛** —— 抛出去就等于"持仓页的做T提示整个功能没了"。
    月/日越界（`2026-13-40`）也当"认不出来"：与其猜，不如让它走"建仓日未知"那条路。
    """
    match = _OPENED_AT_RE.match(str(raw or "").strip())
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def held_positions(db_path: str) -> dict[str, dict]:
    """做T提示的观察面：**有持仓的票** → `{symbol: position 行}`。

    为什么单独取一遍而不复用观察池（`watch_targets`）：观察池是"今天要买的票"
    （股票池 + 自选），**持仓完全可能不在里面**（池子天天重建，用户手上的票却没变）。
    只盯池子会出现"我明明持有它，做T提示却从来不提它"——那是这个功能最不能有的毛病。

    **"有持仓"的判据 = 未被显式平仓**（`storage.load_positions`），而不是 `quantity > 0`：
    改版后界面只记 代码 + 成本价 + 备注，新加的持仓 `quantity = 0` ——
    按老判据写，用户手工记的持仓**永远不会有做T提示**（加了却没反应）。
    界面上右键【关闭监控】把 `monitor` 置 0 的持仓在这里被排除：
    用户要表达的是"别盯它了"，而不是"我没有这只票"。

    老库不受影响：`monitor` 是后加的列、默认 1（见 `storage._ADDED_COLUMNS`），
    `quantity > 0` 的行也照旧在（见 `load_positions` 的说明）。
    """
    with storage.connect(db_path) as conn:
        positions = storage.load_positions(conn)
    return {s: row for s, row in positions.items() if int(row.get("monitor", 1) or 0)}


def monitor_off_symbols(db_path: str) -> set[str]:
    """已经**关闭监控**的持仓代码（`position.monitor = 0`）→ 这一轮一律不提醒。

    为什么单独一个函数：这条判据要在**三个地方**用同一份 ——
    盘中观察面（`watch_targets`：止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点/做T）、
    竞价与异动的标的宇宙（`alert_universe`）、竞价全市场扫描（`auction_scan`）。
    抄三遍必然抄歪一处，而漏掉任何一处都会让右键那句【关闭监控】**变成半真的**：
    用户关了它，做T不提示了，止损止盈照推 —— 那比没有这个开关更糟。

    **优先级（用户拍板）**：某只票同时是策略标的或自选时，**以持仓上的监控开关为准**。
    理由：右键那一行菜单是用户对"这只票"最具体、最新的一次表态，
    而"它在池子里"是几天前跑策略留下的结果；池子每天重建，表态不该被它覆盖。

    读不到持仓表时返回空集合（= 不做排除）：这是一个"少打扰"的开关，
    库读不出来时宁可多提醒一次，也不能因为读库失败把**全部**提醒关掉。
    """
    try:
        with storage.connect(db_path) as conn:
            positions = storage.load_positions(conn)
    except Exception as exc:  # noqa: BLE001 - 见上：读不到就不排除
        logger.debug(f"读持仓监控开关失败（本轮不做排除）：{exc}")
        return set()
    return {
        str(symbol)
        for symbol, row in positions.items()
        if not int(row.get("monitor", 1) or 0)
    }


def sellable_note(position: dict, today: str) -> tuple[bool, str]:
    """这只持仓今天**能不能卖**（T+1）→ `(可卖?, 要写进提示里的一句话)`。

    判据只有一条：`opened_at` 的**日期**是不是今天。
      - 是今天 → 今日新建仓，当天不可卖（反T 那一步根本做不到）；
      - 更早   → 可卖，没有附加说明；
      - 空/NULL/解析不出来 → **按"可卖"处理**，但提示里写明「建仓日未知，按可卖处理」。
        为什么不按"不可卖"处理：这一列老库可能根本没填，按不可卖会让高抛提示**永远不发**，
        用户看不到任何反应只会以为功能坏了；按可卖 + 明说，用户自己一眼就能判断。
        （与项目一贯的"宁可说清、不要静默"一致，见 `config.py` 的 `history_warning`。）
    """
    opened = _day_of(position.get("opened_at"))
    if not opened:
        return True, "建仓日未知，按可卖处理"
    if opened == today:
        return False, "今日新建仓，T+1 当天不可卖"
    return True, ""


def intraday_vwap(snap: dict) -> float | None:
    """分时均价 = **累计成交额 ÷ 累计成交量**（快照里没有均价字段，只能自己算）。

    为什么这么算：`/a-share/prices/snapshot` 的 `PriceSnapshotItem` 只有
    `last_price / open_price / high_price / low_price / prev_price / volume / turnover`
    （见数据源文档 `docs/api/endpoints-prices.md`），**既没有均价，也没有买一卖一**。
    文档里 `volume` 的单位是**股**、`turnover` 是成交额（元），相除就是元/股；
    同一份 `turnover` 早就被 `evaluate_buy_rules` 当"当日累计成交额"在用
    （拿它跟 20 日均额比），所以这两个字段是**当日累计值**。

    这个口径是**拿真实快照核对过的**（2026-09-16 上午，不是照文档猜的）：

    * 全市场 **5573 只**：`turnover ÷ volume` 落在当日 `[low, high]` 内的 **5550 只**、
      越界的 **0 只**（其余 23 只无量/无价，跳过）。若单位是"手"，均价会差 100 倍、
      **每一只**都会越界 —— 不可能是这个结果；
    * 同一批票隔 75 秒取两次：`volume` 与 `turnover` 都**单调增加**
      （茅台 1601022→1615722 股、20.21→20.39 亿元），增量隐含均价 1256.69 元
      正是当时的现价 —— 两个字段都是"当日累计"，不是"最后一分钟"。

    调用方仍然要走一道 `vwap_reasonable()`：那道校验现在防的是**数据故障**
    （停牌半天、high/low 与 volume/turnover 不同步）与将来的接口变更，
    而不是防单位换算 —— 见函数注释。
    """
    amount = _num(snap.get("turnover"))
    volume = _num(snap.get("volume"))
    if amount is None or volume is None or amount <= 0 or volume <= 0:
        return None
    return amount / volume


def vwap_reasonable(vwap: float | None, low: float | None, high: float | None) -> bool:
    """均价是否落在当日 [最低, 最高] 区间内（留 `T_VWAP_RANGE_TOL` 的余量）。

    这是一道**自我校验**，防的是两类事故：
    1. 快照的 high/low 与 volume/turnover **不是同一时刻的**（停牌半天、字段缺失、
       接口变更）—— 算出来的"均价"根本不代表当天的成交；
    2. 单位或口径变了的兜底（`volume` 单位若从"股"变成"手"，均价差 100 倍，一眼可见）。
       真实快照上这道校验**今天一只都不会拦**（2026-09-16 全市场 5573 只，0 只越界，
       见 `intraday_vwap()`），所以它不会误伤正常数据。
    判不过就把均价当"没有"：**宁可这一次不提，也不给一个错的数** ——
    提示里写着「均价 12.10 上方」，用户就会照着这个数下单。
    """
    if vwap is None or not low or not high or high <= 0 or low <= 0:
        return False
    return low * (1 - T_VWAP_RANGE_TOL) <= vwap <= high * (1 + T_VWAP_RANGE_TOL)


def _t_thresholds(cfg: Config | None = None) -> tuple[float, float, float, float]:
    """四个做T阈值：`(高抛涨幅, 高抛回落, 低吸跌幅, 低吸反弹)`。

    配置写坏/缺失一律回默认（`config.Config.__post_init__` 已经归一过一次，
    这里再防一层：调用方可能传一个"仿 Config"的简单对象过来）。
    """
    cfg = cfg or get_config()

    def one(name: str, fallback: float) -> float:
        try:
            value = float(getattr(cfg, name, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    return (one("t_high_min_gain_pct", 2.0), one("t_high_pullback_pct", 1.5),
            one("t_low_min_drop_pct", 2.0), one("t_low_rebound_pct", 1.0))


def evaluate_t_rules(
    symbol: str, snap: dict, position: dict, today: str, cfg: Config | None = None
) -> list[tuple[str, float, str]]:
    """持仓做T 的近似提示：返回 `[(kind, price, detail)]`（**未去重**）。

    判据（四个阈值见 `config.py` / `config.example.toml`，默认 2.0 / 1.5 / 2.0 / 1.0）：

    - `t_high`（高抛 = 反T 前半段）：现价较**昨收**涨 ≥ 2.0% **且** 较**今日最高**
      回落 ≥ 1.5% **且** 现价仍在分时均价上方（回落但没破位）；
    - `t_low`（低吸 = 正T，或反T 的后半段接回）：现价较昨收跌 ≥ 2.0% **且** 较
      **今日最低**反弹 ≥ 1.0% **且** 未跌破（或刚收回，容差 `T_LOW_VWAP_TOL`）均价。

    三条判据**缺一不可**（包括均价）：只有"冲高回落"或只有"跌深反弹"都不构成提示 ——
    "回落"本身可能就是趋势反转的第一步，"跌深"本身可能是继续跌。
    所以均价取不到（成交量/成交额缺失或越界）时**整条不发**，绝不用半个判据凑一条提示。

    Args:
        snap: 一张 60 秒快照（字段名照抄接口：`last_price` / `high_price` / `low_price` /
            `prev_price` / `price_change_ratio_pct` / `volume` / `turnover`）。
        position: 该标的的持仓行（**调用方已保证 `quantity > 0`**）。
        today: 北京时间的今天（`YYYY-MM-DD`，用 `now_shanghai()` 取，别用机器本地时间）。

    注意：**今日最高/最低不跨轮自己累积**，直接用快照自带的 `high_price` / `low_price`
    （接口给的就是当日累计高低点）。自己攒一份跨轮状态要多维护一个"跟实际不符"的来源
    （进程重启、跳过几轮、跑在别的机器上都会错），而快照里本来就有这个数。
    """
    cfg = cfg or get_config()
    last = _num(snap.get("last_price"))
    if last is None or last <= 0:
        return []
    high = _num(snap.get("high_price"))
    low = _num(snap.get("low_price"))
    prev = _num(snap.get("prev_price"))
    pct = _num(snap.get("price_change_ratio_pct"))
    if pct is None and prev and prev > 0:
        # 没有现成的涨跌幅就按快照的昨收算。**只用快照的昨收**：
        # 本地日线是后复权视图（`stock_daily_hfq` = 原始价 × factor），
        # 拿它跟实时价相除，对有过分红送股的票会算出离谱的涨跌幅。
        pct = (last / prev - 1) * 100
    if pct is None:
        return []                     # 昨收也拿不到：不发（不猜一个基准价）
    vwap = intraday_vwap(snap)
    if vwap is not None and not vwap_reasonable(vwap, low, high):
        vwap = None
    gain_min, pull_min, drop_min, rebound_min = _t_thresholds(cfg)
    sellable, sell_note = sellable_note(position, today)

    hits: list[tuple[str, float, str]] = []
    # ── 高抛：反T 的前半段（卖部分昨仓）──
    if sellable and high and high > 0:
        pullback = (high - last) / high * 100
        if pct >= gain_min and pullback >= pull_min and vwap is not None and last >= vwap:
            note = f"（{sell_note}）" if sell_note else ""
            hits.append((T_HIGH_KIND, last,
                         f"现价 {last:.2f}（{pct:+.1f}%），较今日最高 {high:.2f} "
                         f"回落 {pullback:.1f}%，仍在均价 {vwap:.2f} 上方 → "
                         f"可卖出【部分昨仓】（反T：先卖后买），回落后当天再买回等量；"
                         f"只提示部分仓位，可卖数量以券商为准{note}"))
    # ── 低吸：正T（先买后卖），也可能是在把前面高抛掉的仓位接回来 ──
    if low and low > 0:
        rebound = (last - low) / low * 100
        if pct <= -drop_min and rebound >= rebound_min and vwap is not None:
            gap = (last / vwap - 1) * 100
            if gap >= 0:
                vwap_text = f"已站上均价 {vwap:.2f}"
            elif gap >= -T_LOW_VWAP_TOL * 100:
                vwap_text = f"刚收回均价 {vwap:.2f}（仍低 {abs(gap):.1f}%）"
            else:
                vwap_text = ""
            if vwap_text:
                note = f"（{sell_note}）" if not sellable else ""
                hits.append((T_LOW_KIND, last,
                             f"现价 {last:.2f}（{pct:+.1f}%），较今日最低 {low:.2f} "
                             f"反弹 {rebound:.1f}%，{vwap_text} → 可用现金低吸"
                             f"（正T：先买后卖），当天反弹后卖出等量昨仓；"
                             f"只提示部分仓位，可卖数量以券商为准{note}"))
    return hits


def t_hints_today(db_path: str, day: str | None = None) -> dict[str, dict]:
    """今天已经发过的做T提示：`{symbol: 最新一条}`（持仓页「今日T提示」列用）。

    为什么从 `intraday_alert` 读、而不是在界面里拿快照现算：提示是**提醒服务那一轮**
    的结论（同一张快照、同一套阈值、同一条去重记录）。界面上再算一遍就有了两个真相，
    很容易出现"推送说高抛了、界面上却显示没有"这种自相矛盾；而且界面每 5 秒刷一次，
    现算等于要求界面也去取实时行情（多一份请求与失败路径）。
    """
    day = day or now_shanghai().strftime("%Y-%m-%d")
    with storage.connect(db_path) as conn:
        rows = storage.load_alerts_of_day(conn, day, T_KINDS)
    out: dict[str, dict] = {}
    for row in rows:                 # 升序遍历 → 后面的覆盖前面的 = 同一天里最新一条
        symbol = str(row.get("symbol") or "")
        if symbol:
            out[symbol] = row
    return out


def alerts_today_by_symbol(db_path: str, day: str | None = None) -> dict[str, dict]:
    """**今天**每只票最新一条提醒：`{symbol: 提醒行}`（两张表的「提醒」列用）。

    与 `t_hints_today()` 是同一个套路，但**不过滤 kind**：表格里的「提醒」列要回答的是
    "这只票今天出过什么事"，止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点/做T/竞价/异动
    都算 —— 只留做T那一类等于把最要紧的几条（触及止损！）藏起来。

    为什么从库里读、不在界面里现算：提醒是盘中服务那一轮的结论（同一张快照、同一套阈值、
    同一条去重记录）。界面上再算一遍就有两个真相，很容易"推送说了、表里没有"；
    而且界面每 5 秒刷一次，现算就等于要求界面也去取实时行情。

    升序遍历 + 后写覆盖前写 = 同一天里**最后**一条（`load_alerts_of_day` 已按 `pushed_at`
    升序返回，见它的说明），所以"同标的多条 → 取最新"不必自己比时间戳。
    """
    day = day or now_shanghai().strftime("%Y-%m-%d")
    with storage.connect(db_path) as conn:
        rows = storage.load_alerts_of_day(conn, day)
    out: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        # 顺带带上中文标签：浮窗/表格/详情显示的是同一个词，标签只在 `KIND_LABELS`
        # 定义一次，谁都不许自己拼一套
        out[symbol] = {**row, "label": KIND_LABELS.get(str(row.get("kind") or ""),
                                                       str(row.get("kind") or ""))}
    return out


#: 「提醒」列的**短标签**：表格里只能放 4~6 个字，而 `KIND_LABELS` 是给推送用的长句
#: （`🔁 近似·T高抛（反T：先卖后买）` 这种在单元格里会把宽度顶爆）。
#: 这里只压"显示"，不改 `KIND_LABELS` 本身 —— 推送、浮窗、详情用的仍是长句那一份。
ALERT_CELL_SHORT: dict[str, str] = {
    "stop_loss": "触及止损",
    "take_profit": "触及止盈",
    "break_ma5": "跌破5日线",
    "limit_up_open": "涨停打开",
    "break_high": "放量突破",
    "pullback_ma5_buy": "回踩买点",
    "auction_strong": "竞价强",
    "auction_weak": "竞价弱",
    "t_high": "T高抛",
    "t_low": "T低吸",
}


def alert_cell_text(row: dict | None) -> str:
    """「提醒」列的短文本（没提醒 → 空串，界面自己画 `—`）。"""
    if not row:
        return ""
    kind = str(row.get("kind") or "")
    if kind in ALERT_CELL_SHORT:
        return ALERT_CELL_SHORT[kind]
    if kind.startswith("anomaly_"):
        return "异动"
    # 认不出的 kind：用 `KIND_LABELS` 的中文（去掉 emoji 与空白）—— 宁可长一点，
    # 也不能给一个空单元格（"这里坏了"和"今天没事"看起来是一样的）
    return "".join(str(KIND_LABELS.get(kind, kind)).split()).lstrip("⚡🛑🎯📉🔓🚀🔁")


def alert_cell_tooltip(row: dict | None) -> str:
    """「提醒」列的 tooltip：**整句话**（触发数字与动作）+ 时间。"""
    if not row:
        return "今天还没有这只票的盘中提醒"
    label = KIND_LABELS.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
    return (f"{label}\n时间：{row.get('pushed_at') or '—'}\n"
            f"{row.get('detail') or ''}")


def t_hint_cell(row: dict | None) -> str:
    """「今日T提示」单元格的短文本（没提示过 → 空串，界面自己画 `—`）。"""
    kind = str((row or {}).get("kind") or "")
    return T_HINT_SHORT_LABELS.get(kind, "")


def t_hint_tooltip(row: dict | None) -> str:
    """「今日T提示」的 tooltip：**整句话**（数字 + 动作 + 免责说明）都在这儿。

    为什么要 tooltip：表格里只能放 4~5 个字的短标签，而用户真正要看的是
    "凭什么提示"（现价/涨幅/回落/均价）与"我该做什么"（反T 还是正T）——
    这两件事都在 `detail` 里，鼠标停上去就能看到完整那句。
    """
    if not row:
        return ("今天还没有做T提示：只有持仓股、且同时满足「冲高回落没破位」或"
                "「跌深反弹已收回均价」才会提示（近似提示，60 秒快照算的，无法回测）")
    label = KIND_LABELS.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
    return f"{label}\n时间：{row.get('pushed_at') or '—'}\n{row.get('detail') or ''}"


# ── 一轮执行 ──


def build_alerts(
    engine: DataEngine,
    client: hx.HithinkClient,
    scan_market: bool = False,
    cfg: Config | None = None,
    today: str | None = None,
) -> list[dict]:
    """跑一轮规则，返回本轮命中的提醒（**未去重**）。

    当日异动也在这里并进来（共用同一次循环，不再加定时器）。
    **竞价不在这里**：它是"全市场扫描 + 到点才扫 + 汇总推送"（见 `auction_scan`），
    由 `run_once` 按 `auction_scan_due` 单独驱动 —— 混进这一轮会让"每分钟一拍"
    变成"每分钟扫一次全市场"，配额会被打光。

    Args:
        today: 北京时间的今天（`YYYY-MM-DD`）；做T提示要用它判断"是不是今天新建的仓"。
            默认按 `now_shanghai()` 取 —— 但 `run_once` 会把它的 `now` 传进来，
            这样测试注入一个固定时刻时，这里的时间也是同一个（不会一半注入一半真实）。
    """
    cfg = cfg or get_config()
    today = today or now_shanghai().strftime("%Y-%m-%d")
    pool, pool_symbols = watch_targets(engine.db_path)
    # 做T提示只看**持仓**（quantity > 0）；关掉功能时连持仓都不查，一次库都不多读
    held = held_positions(engine.db_path) if bool(getattr(cfg, "intraday_t", True)) else {}
    symbols = list(pool)
    # 持仓**不一定在观察池里**（池子天天重建、用户手上的票却没变），所以要把
    # "不在池子里的持仓"补进这一轮的快照请求；只补几只，仍在同一个 100 只批次里，不额外发请求。
    # 补进来的标的一律**只跑做T规则**：止损止盈那几条的观察池口径是"池子/自选/近期信号"，
    # 顺手扩大它们的作用面等于悄悄改了另一个功能的行为，不在这次改动范围内。
    extra = [s for s in held if s and s not in pool]
    symbols += extra
    if not symbols:
        logger.info("股票池与近期信号都为空，无标的可盯")
    else:
        logger.info(f"本轮盯 {len(symbols)} 只（其中股票池 {len(pool_symbols)} 只"
                    + (f"，补进来的持仓 {len(extra)} 只" if extra else "") + "）")
    # 历史上下文只给池内标的算：补进来的持仓不需要（做T提示只用快照自己的字段，
    # 而且本地日线是**后复权**价，跟实时价根本不是一套口径，不能拿来算涨跌幅）
    ctx = history_context(engine.db_path, list(pool))

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
            position = held.get(symbol)
            if position is not None:
                # 做T提示：只有持仓股才跑（`position` 非 None 就等价于"在持仓表里"）
                t_base = str(position.get("name") or info.get("name") or symbol)
                for kind, price, detail in evaluate_t_rules(symbol, snap, position, today, cfg):
                    alerts.append({"symbol": symbol, "name": t_base, "kind": kind,
                                   "price": price, "detail": detail})
            if symbol not in pool:
                continue          # 补进来的持仓：除做T外不跑别的规则（见上面的注释）
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

    # 当日异动：全市场一条请求，本地只留自己的票
    alerts.extend(anomaly_alerts(client, db_path=engine.db_path, cfg=cfg))

    # 涨停类提醒补"为什么涨停"（按需：只有真命中"涨停打开"才去拉涨停池）
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
    例外：**做T提示（`t_high` / `t_low`）不给条件单** —— 它是同一天两次操作，
    一张条件单表达不了，硬塞一张还会把正T写成"卖出"（理由见循环里的注释）。
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
        # 标的写法 **半角括号**：与界面两张表的列头 `名称(代码)` 一致（改版方案第四节）
        lines.append(f"{label}｜{alert['name']}({alert['symbol'] or '—'}){alert['detail']}")
        symbol = alert.get("symbol") or ""
        if not symbol or not alert.get("price"):
            continue
        if alert.get("kind") in T_KINDS:
            # 做T提示**不生成条件单**：做T是同一天两次操作（先卖后买 / 先买后卖），
            # 一张条件单表达不了。而且持仓股会走 `plan_sell`，那会把"正T：先买后卖"
            # 也写成一张【条件单｜卖出】—— 方向都说错了，比不说更糟。
            # 该说的数字与动作已经在上面那句 detail 里（含"部分仓位"的边界）。
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


def board_of(symbol: str) -> str:
    """代码 → 板块 key（`main` / `chinext` / `star` / `bj`），认不出算主板。

    判定顺序（**`.BJ` 后缀最稳，优先用它**）：
    1. 带交易所后缀的（`430047.BJ` / `688981.SH`）→ 直接看后缀；
    2. 只有 6 位数字时按前缀推：创业板 `300/301`、科创板 `688/689`、
       北交所 `43x/83x/87x/88x/92x`（与 `hithink.infer_exchange` 同一套前缀，不另立一套）；
    3. 其余（沪 `60x`、深 `000/001/002/003`）→ 主板。

    为什么北交所要看后缀：北交所代码前缀很多（43/83/87/88/92），
    而且 `to_local_symbol` 把后缀**丢掉了**（项目内部是裸 6 位），
    所以从接口拿到 `thscode` 时先判后缀，比事后猜前缀可靠。
    """
    raw = str(symbol or "").strip()
    if not raw:
        return "main"
    if "." in raw:
        ticker, _, suffix = raw.partition(".")
        exchange = suffix.upper()
        if exchange == "BJ":
            return "bj"
        if exchange in ("SH", "SZ"):
            return _board_by_prefix(ticker.zfill(6))
        return _board_by_prefix(raw.split(".")[0].zfill(6))
    return _board_by_prefix(raw.zfill(6))


def _board_by_prefix(ticker: str) -> str:
    """纯 6 位代码的板块判定（`board_of` 的后半段）。"""
    if ticker.startswith(("300", "301")):
        return "chinext"
    if ticker.startswith(("688", "689")):
        return "star"
    try:
        if hx.infer_exchange(ticker) == "BJ":
            return "bj"
    except Exception as exc:  # noqa: BLE001 - 推不出来就当主板，绝不抛
        logger.debug(f"板块判定失败（按主板处理）：{exc}")
    return "main"


def board_label(board: str) -> str:
    """板块 key → 中文短标签（认不出原样返回，界面不会出现空白）。"""
    return AUCTION_BOARD_LABELS.get(str(board or ""), str(board or ""))


def market_symbols(
    db_path: str, cfg: Config | None = None, client: Any = None
) -> dict[str, str]:
    """全市场 `{symbol: 名称}`：**优先本地 `stock_basic`（零请求）**。

    竞价扫描针对全市场，需要 5500+ 个代码；本地库已经有这张表（下载/同步的收尾会写），
    所以正常情况下**一个请求都不花**。只有库还是空的（首次运行、还没下完数据）才退回：
    1. `meta/tickers/list`（含中文名，一次性拉全）；
    2. 全市场快照分页（**没有名称**，名称退回代码）。
    两条兜底都会写一行日志说明"为什么这里打了接口"，用户看不出所以然时能查得到。
    """
    try:
        with storage.connect(db_path) as conn:
            local = storage.load_stock_basic(conn)
    except Exception as exc:  # noqa: BLE001 - 库有问题就退回落后的兜底
        logger.info(f"本地股票表读取失败（改用接口取代码表）：{exc}")
        local = {}
    if local:
        return local
    if client is None:
        logger.info("本地还没有股票表（数据没下完），这次竞价扫描没有代码来源 —— 跳过")
        return {}
    try:
        rows = client.tickers()
        out = {}
        for row in rows or []:
            code = str(row.get("thscode") or row.get("ticker") or "")
            if not code:
                continue
            try:
                symbol = hx.to_local_symbol(code)
            except ValueError:
                continue
            out[symbol] = str(row.get("name") or row.get("stock_name") or "")
        if out:
            logger.info(f"本地股票表为空 → 用 meta/tickers/list 取到 {len(out)} 个代码")
            return out
    except Exception as exc:  # noqa: BLE001 - 退到下一条兜底
        logger.info(f"代码表接口取不到（改用全市场快照分页）：{exc}")
    try:
        rows = client.snapshot(page_size=AUCTION_SYMBOL_PAGE)
        out = {}
        for row in rows or []:
            code = str(row.get("thscode") or row.get("ticker") or "")
            if not code:
                continue
            try:
                symbol = hx.to_local_symbol(code)
            except ValueError:
                continue
            out[symbol] = ""
        if out:
            logger.info(f"本地股票表为空 → 用全市场快照分页取到 {len(out)} 个代码（没有中文名）")
        return out
    except Exception as exc:  # noqa: BLE001 - 两条兜底都失败：本次不扫
        logger.info(f"全市场快照也取不到（本次跳过竞价扫描）：{exc}")
        return {}


def auction_pass(fields: dict, cfg: Config | None = None) -> tuple[bool, str]:
    """一只票过不过**过滤规则**：→ `(是否留下, 没留下是因为哪条)`。

    四条硬规则（顺序即判定顺序，理由会写进日志，便于核对"为什么这只没进来"）：
    1. **板块**：不在 `auction_boards` 里的直接排除（只勾科创+创业 = 只看双创）；
    2. **涨幅下限** `auction_min_pct`：低开/微涨的不看（默认 +2%）；
    3. **涨幅上限** `auction_max_pct`：≥ 它的一律排除 —— 一字板/接近涨停**买不进**；
    4. **成交额下限** `auction_min_amount`：不到门槛不参与（`auction_score` 里还有一道同样的
       兜底，这里显式再判一次是为了**能说清"是因为成交额被滤掉的"**）。

    缺数据的处理：涨幅缺失（停牌/无竞价）→ 按"不留下"处理；成交额缺失同样不留。
    """
    cfg = cfg or get_config()
    boards = list(getattr(cfg, "auction_boards", None) or AUCTION_BOARDS)
    board = fields.get("board") or board_of(str(fields.get("symbol") or ""))
    if board not in boards:
        return False, "板块"
    pct = fields.get("pct")
    if pct is None:
        return False, "涨幅缺失"
    try:
        lo = float(getattr(cfg, "auction_min_pct", 2.0))
        hi = float(getattr(cfg, "auction_max_pct", 9.0))
        min_amount = float(getattr(cfg, "auction_min_amount", 5e6))
    except (TypeError, ValueError):
        lo, hi, min_amount = 2.0, 9.0, 5e6
    if pct < lo:
        return False, "涨幅下限"
    if hi > 0 and pct >= hi:
        # 上限拦的是"买不进"：一字板/接近涨停的票，推给用户也只是让他干看着
        return False, "涨幅上限"
    amount = fields.get("amount")
    if amount is None or amount < min_amount:
        return False, "成交额"
    return True, ""


def auction_scan_pause(seconds: float) -> None:
    """扫描时批与批之间的等待（**单独一个函数**：节奏是硬约束，得能被测到）。"""
    if seconds > 0:
        time.sleep(seconds)


def auction_scan_due(
    now: datetime | None = None, cfg: Config | None = None, scanned_slots: Any = None
) -> str | None:
    """现在该不该扫、扫的是哪一档：→ `"09:20"` / `None`。

    Args:
        scanned_slots: 今天**已经扫过**的时刻（`storage.auction_scan_slots` 的结果）。

    规则（`auction_scan_at` 默认 `["09:20", "09:25"]`）：
    - 到点后的 `AUCTION_SCAN_GRACE_MIN`（默认 3）分钟内算"这一档还没跑" —— 调度器是
      60 秒一拍、相位不固定，卡在 09:20:00 那一秒上不现实；
    - **不会跨档**：宽限窗口被下一档截断（09:20 那档最晚到 09:25 就作废），
      否则 09:20 没跑成会在 09:26 补一次，与 09:25 那档挤在一起连发两条；
    - 已经扫过的档（`scanned_slots` 里有）不再扫 —— 每分钟一轮的调度器只会触发一次。
    """
    cfg = cfg or get_config()
    slots, _bad = split_scan_at(getattr(cfg, "auction_scan_at", None) or [])
    if not slots:
        return None
    now = now or now_shanghai()
    done = {str(s) for s in (scanned_slots or [])}
    hhmm = _hhmm(now)
    for index, slot in enumerate(slots):
        hour, minute = (int(x) for x in slot.split(":"))
        start = hour * 60 + minute
        if hhmm[0] * 60 + hhmm[1] < start:
            break                      # 还没到这一档（后面的更晚，不用看了）
        end = start + max(1, int(AUCTION_SCAN_GRACE_MIN))
        if index + 1 < len(slots):
            nxt = slots[index + 1]
            end = min(end, int(nxt.split(":")[0]) * 60 + int(nxt.split(":")[1]))
        if start <= hhmm[0] * 60 + hhmm[1] < end and slot not in done:
            return slot
    return None


def auction_scan(
    client: hx.HithinkClient,
    db_path: str | None = None,
    cfg: Config | None = None,
    now: datetime | None = None,
    slot: str = "",
    on_progress: Any = None,
) -> dict:
    """**全市场**竞价扫描：取数 → 过滤 → 打分 → 排序 → 前 N 只。

    与上一版的区别（用户改的设计）：上一版只扫"自己的票"（池子/自选/持仓），
    这一版**扫全市场再按规则过滤**，所以能看到"池子外面的强势票"。

    节奏（硬约束，别改成每分钟扫）：
    - 代码来源优先本地 `stock_basic`（**零请求**，见 `market_symbols`）；
    - 竞价数据按 `AUCTION_SCAN_BATCH`（100）一批，**串行 + 每批之间等
      `AUCTION_SCAN_PACE`（0.3 秒）**；5573 只 ≈ 56 批 ≈ 20~30 秒；
    - 只在 `auction_scan_at` 到点时被调用一次（见 `auction_scan_due`），
      **不是每分钟**：56 请求 × 10 分钟 = 560 请求会把配额打光。

    Args:
        slot: 本次扫描的时刻标签（`"09:25"`，用于标题与落库）。
        on_progress: `(done, total) -> None`，可选（界面/日志的进度提示用）。

    Returns:
        `{"slot", "scanned", "kept", "hits"(全部命中，按分排序), "alerts"(前 N 只),
          "title", "lines", "skipped": {原因: 只数}}`；失败/没代码来源时 `hits` 为空。
    """
    cfg = cfg or get_config()
    db_path = db_path or str(cfg.db_path)
    result = {"slot": slot, "scanned": 0, "kept": 0, "hits": [], "alerts": [],
              "title": "", "lines": [], "skipped": {}}
    universe = market_symbols(db_path, cfg, client)
    if not universe:
        return result
    boards = list(getattr(cfg, "auction_boards", None) or AUCTION_BOARDS)
    code_map: dict[str, str] = {}
    for symbol in universe:
        if board_of(symbol) not in boards:
            continue
        try:
            code_map[hx.to_thscode(symbol)] = symbol
        except ValueError:
            continue
    if not code_map:
        return result
    codes = list(code_map)
    result["scanned"] = len(codes)
    batch = max(1, min(int(AUCTION_SCAN_BATCH), 100))
    chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    rows: list[dict] = []
    for index, chunk in enumerate(chunks, start=1):
        if index > 1:
            auction_scan_pause(AUCTION_SCAN_PACE)      # 批间 ≥0.3 秒，串行
        try:
            data = client.auction_snapshot(chunk, chunk=len(chunk))
        except Exception as exc:  # noqa: BLE001 - 一批失败不连累其它批
            logger.warning(f"竞价扫描第 {index}/{len(chunks)} 批失败（已跳过）：{exc}")
            continue
        rows.extend(data.get("item") or [])
        if on_progress is not None:
            try:
                on_progress(index, len(chunks))
            except Exception as exc:  # noqa: BLE001 - 进度回调不许影响扫描
                logger.debug(f"扫描进度回调失败：{exc}")
    logger.info(f"竞价扫描（{slot or '—'}）：{len(codes)} 只 / {len(chunks)} 批，"
                f"取回 {len(rows)} 条")
    skipped: dict[str, int] = {}
    hits: list[dict] = []
    # 已关闭监控的持仓：**这一次扫描照常取数，但不推它**。
    # 为什么不禁掉取数：取数是按 100 只一批的整批请求（剔掉一两只不省一个请求），
    # 而"不推"才是用户右键【关闭监控】时想要的那件事（见 `monitor_off_symbols`）。
    off = monitor_off_symbols(db_path)
    for row in rows:
        symbol = code_map.get(str(row.get("thscode") or ""))
        if not symbol:
            continue
        if symbol in off:
            skipped["已关闭监控"] = skipped.get("已关闭监控", 0) + 1
            continue
        fields = auction_fields(row, name=universe.get(symbol, ""))
        fields["board"] = board_of(str(row.get("thscode") or symbol))
        ok, reason = auction_pass(fields, cfg)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        score, breakdown = auction_score(fields, cfg)
        if score is None:                     # 成交额不过关（评分层的兜底）
            skipped["成交额"] = skipped.get("成交额", 0) + 1
            continue
        kind = auction_verdict(score, cfg)
        if kind != "auction_strong":
            skipped["分数"] = skipped.get("分数", 0) + 1
            continue
        hits.append({
            "symbol": symbol,
            "name": fields["name"] or symbol,
            "board": fields["board"],
            "kind": kind,
            "price": fields["price"],
            "pct": fields.get("pct"),
            "volume_ratio": fields.get("volume_ratio"),
            "amount": fields.get("amount"),
            "unmatched": fields.get("unmatched"),
            "score": score,
            "detail": auction_detail(fields, score),
            "_sort": (int(score), float(fields.get("pct") or 0.0)),
        })
    hits.sort(key=lambda item: item["_sort"], reverse=True)
    for hit in hits:
        hit.pop("_sort", None)
    for rank, hit in enumerate(hits, start=1):
        hit["rank"] = rank
    result["hits"] = hits
    result["skipped"] = skipped
    result["kept"] = len(hits)
    max_items = _auction_max_items(cfg)
    top = hits[:max_items]
    for hit in top:
        hit["pushed"] = True
    result["alerts"] = [
        {
            "symbol": hit["symbol"],
            "name": hit["name"],
            "kind": hit["kind"],
            "price": hit["price"],
            "detail": hit["detail"],
        }
        for hit in top
    ]
    title, lines = auction_summary_message(hits, top, slot=slot)
    result["title"], result["lines"] = title, lines
    if hits:
        logger.info(f"竞价扫描命中 {len(hits)} 只（推送前 {len(top)} 只）："
                    + "、".join(f"{h['name']}({h['symbol']})分{h['score']}" for h in top[:5])
                    + ("…" if len(top) > 5 else ""))
    else:
        logger.info(f"竞价扫描 0 只命中（{skipped or '无数据'}）")
    return result


def _auction_max_items(cfg: Config) -> int:
    """推送条数（配置写坏/超范围时按默认 10，绝不因为一个坏数字不推送）。"""
    try:
        return max(1, min(int(getattr(cfg, "auction_alert_max_items", AUCTION_MAX_ALERTS)), 50))
    except (TypeError, ValueError):
        return AUCTION_MAX_ALERTS


def amount_text(amount: Any) -> str:
    """成交额的口语写法：`2.1亿` / `5,200万`（用户看的是量级，不是精确到元）。"""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "—"
    if abs(value) >= 1e8:
        text = f"{value / 1e8:.1f}".rstrip("0").rstrip(".")
        return f"{text}亿"
    return f"{value / 1e4:,.0f}万"


def auction_scan_line(hit: dict) -> str:
    """汇总推送里的一行：`名称(代码) 创业板 +5.21% 量比3.20 成交额2.1亿 买盘剩余3,400手（分6）`。

    与卡片那一行（`auction_card_text`）**同一套写法**（`名称(代码)`、`买盘剩余/卖盘剩余`），
    再补上板块标签、成交额与分数 —— 用户要能一眼看出"哪只、哪个板、凭什么上榜"。
    缺的字段（量比/未匹配量取不到）**整段不显示**，不写 `量比None` 这种垃圾。
    """
    parts = [f"{hit.get('name')}({hit.get('symbol')})"]
    label = board_label(str(hit.get("board") or ""))
    if label:
        parts.append(label)
    pct = hit.get("pct")
    if pct is not None:
        parts.append(f"{float(pct):+.2f}%")
    ratio = hit.get("volume_ratio")
    if ratio is not None:
        parts.append(f"量比{float(ratio):.2f}")
    if hit.get("amount") is not None:
        parts.append(f"成交额{amount_text(hit.get('amount'))}")
    unmatched = hit.get("unmatched")
    if unmatched:
        side = "买盘剩余" if unmatched > 0 else "卖盘剩余"
        parts.append(f"{side}{abs(float(unmatched)):,.0f}手")
    text = " ".join(parts)
    score = hit.get("score")
    return f"{text}（分{score}）" if score is not None else text


def auction_summary_message(
    hits: list[dict], top: list[dict], slot: str = ""
) -> tuple[str, list[str]]:
    """竞价扫描的**汇总推送**：标题给"共命中几只、推前几只"，正文按分数编号列前 N 只。

    形如：
        ⚡ 竞价扫描（9:25）｜共命中 37 只，推送前 10：
        1. 名称（代码） 创业板 +5.21% 量比3.20 成交额2.1亿 买盘剩余3,400手（分6）

    为什么"共命中"与"推送前 N"分开写：用户要能看出**被截掉了多少**
    （只看 10 条不代表全市场只有 10 只强），详情里能看全部命中。
    """
    if not hits:
        return f"⚡ 竞价扫描（{_slot_text(slot)}）｜0 只命中", []
    title = (f"⚡ 竞价扫描（{_slot_text(slot)}）｜共命中 {len(hits)} 只，"
             f"推送前 {len(top)}：")
    lines = [f"{index}. {auction_scan_line(hit)}" for index, hit in enumerate(top, start=1)]
    return title, lines


def _slot_text(slot: str) -> str:
    """`09:25` → `9:25`（用户读的是"九点二十五"，不想要那个前导零）。"""
    text = str(slot or "").strip()
    if len(text) == 5 and text[0] == "0" and text[1].isdigit():
        return text[1:]
    return text or "—"


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
        min_pct = float(getattr(cfg, "auction_min_pct", 2.0) or 2.0)
        min_ratio = float(getattr(cfg, "auction_min_volume_ratio", 2.0) or 2.0)
        min_amount = float(getattr(cfg, "auction_min_amount", 5e6) or 5e6)
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

    弱的分门限 = 强门限取负再放宽一档（默认 2 → **-1**）。

    注意：全市场扫描里有**涨幅下限**（默认 +2%），低开的票在那一步就被滤掉了，
    所以扫描结果实际上只会出"强"；"弱"这一支仍然保留（`auction_score` 的对称扣分
    也是为它准备的），口径要改成"连低开一起提示"时不用重新设计。
    """
    if score is None:
        return ""
    cfg = cfg or get_config()
    try:
        min_score = int(getattr(cfg, "auction_min_score", 2) or 2)
        min_score = min(max(min_score, AUCTION_SCORE_RANGE[0]), AUCTION_SCORE_RANGE[1])
    except (TypeError, ValueError):
        min_score = 2
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

    **已经关闭监控的持仓（`position.monitor = 0`）在这里被剔掉**：竞价与异动也属于
    "这只票的盘中提醒"，右键【关闭监控】必须把它们一起管住 —— 否则用户看到的是
    "我关了监控，止损不推了，异动照推"，只会以为开关是坏的。
    优先级见 `monitor_off_symbols`：同时是策略标的/自选时，**以持仓的开关为准**。
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
    off = monitor_off_symbols(db_path)
    return {s: n for s, n in symbols.items() if s and s not in off}


def fetch_auction(
    db_path: str,
    cfg: Config | None = None,
    client: hx.HithinkClient | None = None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """取一次竞价快照 → `{symbol: 展示字段}`（界面卡片用；失败/非窗口返回空字典）。

    与竞价扫描（`auction_scan`）共用 `auction_fields` / `auction_score`，所以卡片上的数字
    与推送里的数字**一定一致**（同一个接口、同一套"负数当没有"的处理）。

    范围不同（这是**故意的**）：卡片这一行走 `alert_universe`，只取池子 + 自选 + 持仓
    （几十只 → 1 个请求，每分钟一次很便宜）；全市场扫描走 `market_symbols` + 56 个请求，
    只在配置的时刻跑（见 `auction_scan_due`）。
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


#: 需要补"涨停原因"的提醒类型（这条本来只说"涨停打开"，用户看不出为什么涨停）。
#: 原来还有 `first_board`（打板提醒）—— 那条提醒已下线（见模块 docstring），
#: 所以这里只剩一种；**保留成元组**是为了以后再加涨停类提醒时不用改调用处。
LIMIT_UP_KINDS = ("limit_up_open",)


def enrich_limit_up_reasons(client: hx.HithinkClient, alerts: list[dict]) -> None:
    """给涨停类提醒补上"为什么涨停"（**按需取**：只有真命中才去拉涨停池）。

    为什么要按需：`limit_up_pool()` 会自动翻页（一天几十上百条），
    而绝大多数轮次根本没有涨停类提醒 —— 每轮都拉等于白花配额。
    原因取不到就保持原样（少一句话，不影响这条提醒本身的价值）。
    """
    targets = {
        alert["symbol"] for alert in alerts
        if alert.get("kind") in LIMIT_UP_KINDS and alert.get("symbol")
        # 只跳过"已经补过原因"的（detail 里已经有"，涨停原因：…"就不用再补）；
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
    now: datetime | None = None,
) -> dict:
    """跑一轮盘中提醒（含**到点才跑的全市场竞价扫描**）。

    Args:
        notifier: 通知函数 `(title, lines) -> dict`；默认走三路并行通知。
        now: "现在"（测试用；默认取北京时间）。

    Returns:
        {"trading_day", "in_session", "hits", "fresh", "pushed", "error",
         "auction": {"slot", "scanned", "hits", "pushed"}}
    """
    cfg = cfg or get_config()
    now = now or now_shanghai()
    today = now.strftime("%Y-%m-%d")
    result = {"trading_day": is_trading_day(engine.db_path, today),
              "in_session": in_session(now), "hits": 0, "fresh": 0, "pushed": False,
              "error": "", "auction": {}}
    if not result["trading_day"]:
        logger.info("今天不是交易日，跳过盘中提醒（含竞价扫描）")
        return result
    # 竞价扫描是"到点才扫"（默认 09:20 / 09:25）：先算出这一轮到没到扫描时刻，
    # 它同时也是"现在虽然还没到 9:30，但这一轮必须跑"的理由之一。
    scan_slot = ""
    if bool(getattr(cfg, "intraday_auction", True)):
        try:
            with storage.connect(engine.db_path) as conn:
                scanned = storage.auction_scan_slots(conn, today)
            scan_slot = auction_scan_due(now, cfg, scanned) or ""
        except Exception as exc:  # noqa: BLE001 - 库有问题就不扫，别影响其它提醒
            logger.debug(f"竞价扫描到点判断失败（本轮不扫）：{exc}")
    in_window = result["in_session"] or bool(scan_slot)
    if not in_window and not ignore_session:
        logger.info("当前不在交易时段（也不在竞价扫描时刻），跳过")
        return result

    if client is None and not hx.available():
        logger.warning("未配置同花顺 API Key，无法获取实时行情")
        result["error"] = "未配置 API Key"
        return result

    try:
        client = client or hx.HithinkClient(api_key=cfg.hithink_api_key or None, pace=0.05)
        alerts = build_alerts(engine, client, cfg=cfg, today=today)
        result["hits"] = len(alerts)
        fresh = record_alerts(engine.db_path, alerts, today)
        result["fresh"] = len(fresh)
        if fresh and not (dry_run or notifier is None):
            title, lines = format_message(fresh, db_path=engine.db_path, cfg=cfg)
            for line in lines:
                logger.info(f"【盘中】{line}")
            notifier(title, lines)
            result["pushed"] = True
        elif fresh:
            title, _lines = format_message(fresh, db_path=engine.db_path, cfg=cfg)
            logger.info(f"[dry-run] 将推送 {len(fresh)} 条提醒：{title}")
        elif not fresh:
            logger.info(f"本轮命中 {len(alerts)} 条，但都已推送过（或没有命中）")
    except Exception as exc:  # noqa: BLE001 - 单轮异常不该让提醒服务退出
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(f"盘中提醒本轮异常：{result['error']}")

    # 竞价扫描放在**上那个 try 之外**：它是 20~30 秒的慢活，而且与上面互不相干 ——
    # 常规提醒出错（某只票取不到价、涨停池抽风）不该连带把竞价扫描也跳过，反之亦然。
    if scan_slot:
        try:
            result["auction"] = _push_auction_scan(
                client, engine, cfg, today, scan_slot, now,
                dry_run=dry_run, notifier=notifier,
            )
        except Exception as exc:  # noqa: BLE001
            result["auction"] = {"slot": scan_slot, "error": f"{type(exc).__name__}: {exc}"}
            logger.warning(f"竞价扫描本轮异常：{result['auction']['error']}")
    return result


def _push_auction_scan(
    client: hx.HithinkClient,
    engine: DataEngine,
    cfg: Config,
    today: str,
    slot: str,
    now: datetime,
    *,
    dry_run: bool = False,
    notifier: Any = None,
) -> dict:
    """跑一次全市场竞价扫描并推送汇总（返回结果摘要，供 `run_once` 汇报）。

    三件事的层次要分清：
    1. **落库**（`auction_scan` 表）存**全部命中**：界面"看全部"与复盘要用；
    2. **提醒表**（`intraday_alert`）只登前 N 只：浮窗/提醒列表不该被几十条刷屏
       （同标的同类型当天只登一次，所以 9:20 登过的 9:25 不会再登）；
    3. **推送**：一条汇总（`⚡ 竞价扫描（9:25）｜共命中 37 只，推送前 10：`），
       两次扫描各推一条 —— 9:25 那次是**终态**，值得再推一次。

    任何一步失败都只记日志：竞价扫描的毛病不该拖垮止损止盈那些更要紧的提醒。
    """
    def _progress(done: int, total: int) -> None:
        # 每 10 批记一行：56 批的扫描要跑 20~30 秒，日志里要能看出"它还在动"
        if done == 1 or done % 10 == 0 or done == total:
            logger.info(f"竞价扫描进度 {done}/{total} 批（{slot}）")

    try:
        scan = auction_scan(client, db_path=engine.db_path, cfg=cfg, now=now,
                            slot=slot, on_progress=_progress)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"竞价扫描失败（本轮不推竞价汇总）：{exc}")
        return {"slot": slot, "scanned": 0, "hits": 0, "pushed": False, "error": str(exc)}
    summary = {"slot": slot, "scanned": scan["scanned"], "kept": scan["kept"],
               "hits": len(scan["hits"]), "pushed": False, "error": ""}
    stored = scan["hits"][:AUCTION_SCAN_STORE_LIMIT]
    if len(stored) < len(scan["hits"]):
        logger.info(f"竞价扫描命中 {len(scan['hits'])} 只，落库与详情只留前 "
                    f"{len(stored)} 只（界面不需要几千行）")
    try:
        with storage.connect(engine.db_path) as conn:
            storage.write_auction_scan(conn, today, slot, stored,
                                       total=len(scan["hits"]),
                                       scanned_at=now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:  # noqa: BLE001 - 落库失败不影响推送
        logger.warning(f"竞价扫描结果落库失败：{exc}")
    fresh = []
    if scan["alerts"]:
        try:
            fresh = record_alerts(engine.db_path, scan["alerts"], today)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"竞价提醒登记录失败：{exc}")
    summary["fresh"] = len(fresh)
    if not scan["lines"]:
        return summary
    for line in scan["lines"]:
        logger.info(f"【竞价】{line}")
    if dry_run or notifier is None:
        logger.info(f"[dry-run] 将推送竞价汇总：{scan['title']}")
        return summary
    try:
        notifier(scan["title"], scan["lines"])
        summary["pushed"] = True
    except Exception as exc:  # noqa: BLE001 - 推送失败只记日志
        logger.warning(f"竞价汇总推送失败：{exc}")
        summary["error"] = str(exc)
    return summary


def alert_rows(db_path: str, limit: int = 50) -> list[dict]:
    """界面用：最近的盘中提醒（附中文标签）。"""
    with storage.connect(db_path) as conn:
        rows = storage.load_recent_alerts(conn, limit)
    for row in rows:
        row["label"] = KIND_LABELS.get(row["kind"], row["kind"])
    return rows
