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
> 它们**不参与筛选、也不会混进当天的池子推送**，只走盘中提醒那几条通道。
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
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from laoa_trader.config import Config, get_config
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

#: "筛选完成"这一条消息的类型（**不是盘中提醒**：它由 `scheduler.run_daily()` 在
#: 建池成功之后写入）。用户 2026-09-18 要求"通知仿 QQ：盘中提醒与匹配推送都进同一个
#: 消息列表"，所以它住在同一张表（`intraday_alert`）里、只是 kind 不同。
KIND_POOL = "pool"

#: 「选股结果出来了」这条消息的正文（2026-10-05 主人："选股结果也不要播报，
#: 只提醒选股结果已出，请点击查看"）。它是一句**门铃**：不列票名、不报涨跌 ——
#: 要看哪几只在「策略筛选 → 本次筛选结果」里有一整张表。
POOL_DONE_TEXT = "选股结果已出，请点击查看"

KIND_LABELS = {
    KIND_POOL: "📈 选股结果已出",
    "stop_loss": "🛑 触及止损",
    "take_profit": "🎯 触及止盈",
    "break_ma5": "📉 跌破 5 日线",
    "limit_up_open": "🔓 涨停打开",
    "break_high": "🚀 放量突破20日高",
    "pullback_ma5_buy": "🎯 池内回踩买点",
    # 持仓做T 的**近似**提示：标签里直接写「近似」，用户在推送/浮窗/提醒页一眼能分辨
    # （它们与上面那些有历史数据支撑的规则不是一回事，见模块 docstring）
    "t_high": "🔁 近似·T高抛（反T：先卖后买）",
    "t_low": "🔁 近似·T低吸（正T：先买后卖）",
}

# 异动的 kind 按标签区分（同一只票同一天同一标签只推一次；标签变了可以再推一条）
for _tag_code in ANOMALY_TAGS:
    KIND_LABELS[f"anomaly_{_tag_code.lower()}"] = "⚡ 异动"


#: **语音播报的类型族**（设置页「语音播报内容」那一排勾选框就是它）。
#:
#: 为什么要"族"而不是一个个 kind：`anomaly_rapid_rally` 这类 kind 是按标签生成的（有 7 个），
#: 做T 又有两个。让用户在设置里勾十几个只有程序才认得的英文代号是折磨；
#: 勾"当日异动""做 T 提示"才是他脑子里的分类。
#:
#: 2026-10-05 主人："现在的语音播报有点乱，在设置里增加选项，可以自由选择要提醒的内容。"
#: 勾的是**念不念**，不影响消息列表与两张表的「提醒」列（那里始终是全的）。
VOICE_KIND_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("stop_loss", "触及止损", ("stop_loss",)),
    ("take_profit", "触及止盈", ("take_profit",)),
    ("break_ma5", "跌破 5 日线", ("break_ma5",)),
    ("limit_up_open", "涨停打开", ("limit_up_open",)),
    ("break_high", "放量突破 20 日高", ("break_high",)),
    ("pullback_ma5_buy", "池内回踩买点", ("pullback_ma5_buy",)),
    ("t", "做 T 提示（近似）", ("t_high", "t_low")),
    ("anomaly", "当日异动", tuple(f"anomaly_{code.lower()}" for code in ANOMALY_TAGS)),
    ("pool", "选股结果已出（默认不念）", (KIND_POOL,)),
)

#: **默认不念**的类型族（即使 `voice_kinds` 为空 = "全都念"）。
#:
#: 目前只有一条：`pool` —— 它要求的动作是"你自己点开看"，念出来只是「选股结果已出，
#: 请点击查看」，一个字的信息量都没有（2026-10-05 主人："选股结果也不要播报"）。
#: 用户真想让它念，在设置里勾上「选股结果已出」这一类就行（勾了就按勾的来）。
VOICE_OFF_BY_DEFAULT: frozenset[str] = frozenset({"pool"})

#: kind → 类型族（启动时展开一次）
_KIND_TO_GROUP: dict[str, str] = {
    kind: code for code, _label, members in VOICE_KIND_GROUPS for kind in members
}


def voice_kind_group(kind: Any) -> str:
    """一条提醒的 `kind` → 它属于哪个类型族（设置里勾的就是族）。

    认不出来的一律返回它自己：将来新加一种提醒，默认行为是"按自己的 kind 走"，
    既不会被误当成"没勾上"而念不出来，也不会被并进别的族里。
    """
    return _KIND_TO_GROUP.get(str(kind or "").strip(), str(kind or "").strip())


def voice_allowed(kind: Any, cfg: Config | None = None) -> bool:
    """这一条要不要**念出来**（`voice_kinds` 为空 = 全都念）。

    只影响"念的那一句"（含桌宠气泡上那句话），不影响消息列表、图标闪烁、飞书/托盘推送 ——
    "少念几句"不该等于"少收几条提醒"。
    """
    cfg = cfg or get_config()
    chosen = {str(code).strip().lower() for code in (getattr(cfg, "voice_kinds", None) or [])
              if str(code).strip()}
    family = voice_kind_group(kind).lower()
    if not chosen:
        # 空 = "用户没筛过" → 全都念，**除了**那几类默认不念的（见 `VOICE_OFF_BY_DEFAULT`）
        return family not in VOICE_OFF_BY_DEFAULT
    return family in chosen


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
    """用官方交易日历判断是否开盘。

    判据顺序（改这里要小心：判错一次不是"少一条提醒"，而是**一整天全哑**，
    而且哑得没有任何提示 —— 用户看到的是"运行=是、交易时段=是、就是没提醒"）：

        1. 库里**有**这一天 → 交易日；
        2. 周末 → 不是（日历再怎么滞后也不会改变这一点）；
        3. 日历**已经越过**这一天（`MAX(date) > day`）→ 这一档日历是齐的，
           没有它就是真休市（调休/节假日）；
        4. 日历最新只到这一天或更早（`MAX(date) <= day`）→ **日历还没同步到今天**，
           按交易日处理。

    第 4 条是 2026-09-28 修的：老实现只认第 1、2 条（另外"表为空当交易日"），
    于是"昨晚 16:00 日更写了截至昨天的日历、今早开机还没同步"这个**每天都会发生**
    的情形被当成休市 —— 盘中提醒从开盘静默到当晚日更，用户只会说"盘中提醒无效"。
    代价：日历滞后的工作日内遇上"周中的节假日"，会按交易日跑一轮（数据是上一交易日的，
    价格阈值类规则仍然成立）；日历一旦覆盖到今天之后（默认的主源日历是近一年，
    含未来交易日），第 3 条会立刻接管，判断恢复精确。
    """
    day = day or now_shanghai().strftime("%Y-%m-%d")
    try:
        with storage.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM trading_calendar WHERE date = ?", (day,)
            ).fetchone()
            latest = conn.execute("SELECT MAX(date) FROM trading_calendar").fetchone()[0]
    except Exception:  # noqa: BLE001 - 库还没建好时按"交易日"处理，别把提醒整天关掉
        return True
    if row:
        return True
    try:
        weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
    except ValueError:
        weekday = 0                     # 日期本身不合法（调用方给的）：不据此判休市
    if weekday >= 5:
        return False
    if not latest:
        return True                     # 日历是空的：见上（同步失败时整天不跑更糟）
    if str(latest) > day:
        return False                    # 日历齐到"今天之后"，没有今天 = 真休市
    logger.info(f"交易日历只到 {latest}（还没到今天 {day}）：按交易日处理，"
                "免得盘中提醒从开盘静默到当晚日更")
    return True


# ── 观察池 ──


def _watch_label(note: str, cost: float | None) -> str:
    """自选标的的展示标签：`自选（龙头，成本 12.40）`。

    把备注与成本写进提醒里，是为了让收到卡片的人**一眼知道为什么盯它**：
    备注是用户自己写的理由（"龙头""消息面"），成本则直接对应止损止盈的基准。

    标签固定写「自选」：这条提醒本来就出自自选表，写「策略+自选」等于在一个
    自选名单里再解释一次"它也是自选"（2026-09-23 主人："为什么要+自选 什么策略
    跑出来的 直接记录策略名称 只有用户自己输入的才能算自选来源"）；是哪条策略
    选出来的，看「自选标的」表的来源列。
    """
    head = "自选"
    details = []
    if note:
        details.append(note)
    if cost:
        details.append(f"成本 {float(cost):.2f}")
    return f"{head}（{'，'.join(details)}）" if details else head


def _position_label(cost: float | None) -> str:
    """持仓票的标签：`持仓（成本 12.40）`。

    与 `_watch_label` 分开写是为了让「持仓」与「自选」在消息列表里一眼可分 ——
    同一只票既自选又持仓时，止损止盈要按**成本**算，标签必须说清用的是哪个口径。
    """
    try:
        cost = float(cost) if cost else None
    except (TypeError, ValueError):
        cost = None
    return f"持仓（成本 {cost:.2f}）" if cost else "持仓"


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
    2026-09-18 策略组机制删掉之后，匹配只产出公式标的，所以现在一律传 None ——
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
    """盘中观察目标 = **用户点名要盯的自选标的**（持仓由 `build_alerts` 另外补进来）。

    2026-10-05 主人："默认只监控持仓股票。" 所以这里**不再自动盯池子**：
    匹配出来的票只是候选，要不要盯由他在「自选标的」页逐只打开监控开关。

    Args:
        selection: **2026-09-18 起不再使用**（原来按启用的策略组窄化观察面）。
        days: 保留参数（老调用方传的"回看几天信号"），现在不再用它兜底。
        cfg: 配置。

    Returns:
        ({symbol: {...}}, 适用"池内买点"规则的符号集合) —— 第二个返回值就是观察面本身
        （用户点名要盯的票照给买点提示）。
    """
    from laoa_trader import pool as pool_mod

    allowed = None          # 见函数 docstring：按策略组窄化这条逻辑已经删掉

    cfg = cfg or get_config()
    targets: dict[str, dict] = {}
    # 已关闭监控的持仓：**从观察面里整体剔掉**，不管它是不是池内标的/自选
    # （优先级见 `monitor_off_symbols` 的说明）
    off = monitor_off_symbols(db_path)
    # 池子只用来认"这只是不是池内标的"（回踩买点要用它），**不再自动进观察面**。
    pool_rows = list(pool_mod.load_pool(db_path))
    pool_by_symbol = {str(row["symbol"]): row for row in pool_rows}
    pool_symbols: set[str] = set()

    # ── 观察面 = 用户明确打开监控的自选标的（+ 持仓，由 `build_alerts` 补）──
    #
    # 2026-10-05 主人："默认只监控持仓股票。" —— 在这之前，**池子里的每一行都盯**：
    # 跑一次筛选选出几十只，桌面上就几十只票一起响（他原话是"语音播报有点乱"）。
    # 现在改成分工明确：筛选结果只是"候选"，要不要盯由他在「自选标的」里逐只打开
    # （开关写进 `watchlist.enabled`，见 `storage.upsert_watchlist` 的说明）。
    # 默认为空 = 一只都不盯，这**不是**配置错误：新装的程序本来就该先安静地跑一轮匹配。
    if getattr(cfg, "watchlist_in_pool", True):
        with storage.connect(db_path) as conn:
            watch_entries = storage.load_watchlist(conn, enabled_only=True)
            positions = storage.load_positions(conn)
        for entry in watch_entries:
            symbol = entry["symbol"]
            note = (entry.get("note") or "").strip()
            held = positions.get(symbol)
            info = pool_by_symbol.get(symbol) or {}
            label = _watch_label(note, held["avg_cost"] if held else None)
            targets[symbol] = {
                "name": info.get("name") or entry.get("name") or symbol,
                "signal_date": info.get("date"),
                "label": label,
                "note": note,
                "watchlist": True,
                "source": "策略+自选" if info.get("strategy") else "自选",
                "strategy": info.get("strategy"),
                # 有持仓 → 用**持仓成本**做止损/止盈参考价；
                # 没有 → 留 None，交给 evaluate_sell_rules 回退到"前一交易日收盘"
                "close": float(held["avg_cost"]) if held and held.get("avg_cost") else None,
                "cost": float(held["avg_cost"]) if held and held.get("avg_cost") else None,
            }
            # 用户点名要盯的票，买点类规则（池内回踩买点）对它生效。
            # 2026-10-05 之前这里是"池子里的每一行自动进"，现在进observation面的只有
            # 用户打开监控的那些 —— 名单里既然是他自己挑的，买点提示就该照给。
            pool_symbols.add(symbol)
    # 观察面 = 用户点名要盯的自选标的（+ 持仓，由 `build_alerts` 补进来）。
    #
    # 2026-10-05 起**不再有"池子为空就退回近期推送信号"这条兜底**：它会把刚匹配出来的
    # 几十只票又变成提醒 —— 正是主人说的"乱"。要盯谁，就在「自选标的」里把那一行的
    # 监控开关打开（写进 `watchlist.enabled`）。
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
    """卖出/风控规则：返回 [(kind, price, detail)]。

    止损/止盈只需要一个有意义的**参考价**（持仓成本、或昨收），不需要本地历史；
    跌破 5 日线/涨停打开才要 `ctx`。所以 2026-09-28 把"没有 ctx 就整条不跑"改成
    "逐条看自己要什么" —— 持仓票（尤其本地没下过历史数据的）从此也能收到止损止盈提醒。
    """
    cfg = cfg or get_config()
    stop_pct, target_pct = cfg.stop_loss, cfg.take_profit
    last = snap.get("last_price")
    if not last:
        return []
    ctx = ctx or {}
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

    为什么单独一个函数：这条判据要在**两个地方**用同一份 ——
    盘中观察面（`watch_targets`：止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点/做T）、
    异动的标的宇宙（`alert_universe`）。
    抄两遍必然抄歪一处，而漏掉任何一处都会让右键那句【关闭监控】**变成半真的**：
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
    "这只票今天出过什么事"，止损/止盈/涨停打开/跌破 5 日线/放量突破/回踩买点/做T/异动
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
    stats: dict | None = None,
) -> list[dict]:
    """跑一轮规则，返回本轮命中的提醒（**未去重**）。

    当日异动也在这里并进来（共用同一次循环，不再加定时器）。

    Args:
        today: 北京时间的今天（`YYYY-MM-DD`）；做T提示要用它判断"是不是今天新建的仓"。
            默认按 `now_shanghai()` 取 —— 但 `run_once` 会把它的 `now` 传进来，
            这样测试注入一个固定时刻时，这里的时间也是同一个（不会一半注入一半真实）。
        stats: 出参（可省）：填 `watched`/`universe`/`held_extra`，供【检查盘面】那句
            结论说清"这一轮到底盯了几只" —— 否则"命中 0 条"和"一只都没盯"长得一样。
    """
    cfg = cfg or get_config()
    today = today or now_shanghai().strftime("%Y-%m-%d")
    pool, pool_symbols = watch_targets(engine.db_path)
    # 持仓一律查（不再挂在"做T"开关上）：**手上的票是最该提醒的那一类**，
    # 而"止损/止盈/跌破 5 日线"原来只在"池子/自选/近期信号"里跑 —— 于是
    # "我只把票记在持仓里"的用户整场交易收不到任何提醒（2026-09-28 主人实报"盘中提醒无效"）。
    held = held_positions(engine.db_path)
    symbols = list(pool)
    # 持仓**不一定在观察池里**（池子天天重建、用户手上的票却没变），所以要把
    # "不在池子里的持仓"补进这一轮的快照请求；只补几只，仍在同一个 100 只批次里，不额外发请求。
    off = monitor_off_symbols(engine.db_path)   # 「已关闭监控」的持仓照旧不盯
    extra = [s for s in held if s and s not in pool and s not in off]
    symbols += extra
    if stats is not None:
        stats.update({"watched": len(symbols), "universe": len(pool),
                      "held_extra": len(extra)})
    if not symbols:
        logger.info("股票池与近期信号都为空，无标的可盯")
    else:
        logger.info(f"本轮盯 {len(symbols)} 只（其中股票池 {len(pool_symbols)} 只"
                    + (f"，补进来的持仓 {len(extra)} 只" if extra else "") + "）")
    # 历史上下文：池内标的 + 补进来的持仓 —— 跌破 5 日线/涨停打开要用 MA5 与昨收。
    # 做T提示只用快照自己的字段，用不到这一份。
    ctx = history_context(engine.db_path, list(pool) + extra)

    alerts: list[dict] = []
    # 做T提示只看开关（默认关，用户拍板）；**持仓本身不挂在这个开关上** ——
    # 止损止盈那些卖出规则要照跑，否则"只把票记在持仓里"的用户零提醒（见上面）。
    t_on = bool(getattr(cfg, "intraday_t", False))
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
            if position is not None and t_on:
                # 做T提示：只有持仓股才跑（`position` 非 None 就等价于"在持仓表里"）
                t_base = str(position.get("name") or info.get("name") or symbol)
                for kind, price, detail in evaluate_t_rules(symbol, snap, position, today, cfg):
                    alerts.append({"symbol": symbol, "name": t_base, "kind": kind,
                                   "price": price, "detail": detail})
            if symbol not in pool:
                # 补进来的持仓（既不在池子里、也不是自选）：跑卖出/风控规则，
                # 参考价用**持仓成本** —— 成本才是止损止盈的基准，昨收不是。
                if position is None:
                    continue
                cost = float(position.get("avg_cost") or 0) or None
                base = str(position.get("name") or symbol)
                label = _position_label(cost)
                for kind, price, detail in evaluate_sell_rules(
                        symbol, snap, context, cost, cfg):
                    alerts.append({"symbol": symbol, "name": f"{base}·{label}",
                                   "kind": kind, "price": price, "detail": detail})
                continue
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
        # 标的写法 **半角括号**：与界面两张表的列头 `名称(代码)` 一致（开发文档）
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


def report_text(report: dict | None) -> str:
    """把 `run_once` 的 report 翻成一句中文（【检查盘面】按钮与状态栏用）。

    为什么专门翻这一句：界面原来把它丢进了"筛选流程"那个分支，显示成
    `盘中检查完成： 池子 0 只` —— 点了按钮只看到"池子 0 只"，既不知道盯了几只，
    也不知道为什么一条都没提醒（不是交易日 / 不在交易时段 / 没配 API Key /
    全被当天去重挡了）。**"没有提醒"与"提醒跑不起来"是两件完全不同的事**，
    分不清就只能得到"盘中提醒无效"这一种结论。
    """
    report = report or {}
    if report.get("error"):
        return f"没跑成：{report['error']}"
    if report.get("skip"):
        return f"已跳过：{report['skip']}"
    watched = int(report.get("watched") or 0)
    hits = int(report.get("hits") or 0)
    fresh = int(report.get("fresh") or 0)
    bits = [f"盯了 {watched} 只"]
    if report.get("held_extra"):
        bits.append(f"其中补盯的持仓 {int(report['held_extra'])} 只")
    bits.append(f"命中 {hits} 条")
    if fresh:
        bits.append(f"新增提醒 {fresh} 条（已进消息列表）")
    elif hits:
        bits.append("都在今天提醒过了（同标的同类型当天只提醒一次）")
    else:
        bits.append("本轮没有触发条件的票")
    text = "，".join(bits)
    return text


# ── 当日异动 ──


def _num(value: Any) -> float | None:
    """宽松转 float（空/停牌/字段缺失一律 None）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    为什么三类都要：当日异动只对"用户关心的票"有意义 ——
    池子是要买的、持仓是已经买了的、自选是盯着的；漏掉哪一类用户都会当成 bug。
    名称以 `stock_basic` 为准（池子/自选里存的是建池那天的名字，可能已经改名）。

    **已经关闭监控的持仓（`position.monitor = 0`）在这里被剔掉**：异动也属于
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
    """跑一轮盘中提醒。

    Args:
        notifier: 通知函数 `(title, lines) -> dict`；默认走三路并行通知。
        now: "现在"（测试用；默认取北京时间）。

    Returns:
        {"trading_day", "in_session", "hits", "fresh", "pushed", "error", "skip",
         "watched", "universe", "held_extra"}
    """
    cfg = cfg or get_config()
    now = now or now_shanghai()
    today = now.strftime("%Y-%m-%d")
    result = {"trading_day": is_trading_day(engine.db_path, today),
              "in_session": in_session(now), "hits": 0, "fresh": 0, "pushed": False,
              "error": "", "skip": "", "watched": 0, "universe": 0, "held_extra": 0}
    if not result["trading_day"]:
        result["skip"] = "今天不是交易日"
        logger.info("今天不是交易日，跳过盘中提醒")
        return result
    if not result["in_session"] and not ignore_session:
        result["skip"] = "现在不在交易时段（9:30–11:30 / 13:00–15:00）"
        logger.info("当前不在交易时段，跳过")
        return result

    if client is None and not hx.available():
        logger.warning("未配置同花顺 API Key，无法获取实时行情")
        result["error"] = "未配置同花顺 API Key（拿不到实时行情，盘中提醒无法运行）"
        return result

    try:
        client = client or hx.HithinkClient(api_key=cfg.hithink_api_key or None, pace=0.05)
        alerts = build_alerts(engine, client, cfg=cfg, today=today, stats=result)
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

    return result


def alert_rows(db_path: str, limit: int = 50) -> list[dict]:
    """界面用：最近的盘中提醒（附中文标签）。"""
    with storage.connect(db_path) as conn:
        rows = storage.load_recent_alerts(conn, limit)
    for row in rows:
        row["label"] = KIND_LABELS.get(row["kind"], row["kind"])
    return rows
