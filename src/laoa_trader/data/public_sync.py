"""免 Key 的日更（腾讯全市场快照 + 新浪代码表）—— 让没 Key 的用户也能天天匹配。

为什么不能沿用 `sync.py` 那条路
------------------------------
`sync.py` 的日更建立在**同花顺 dump** 上（`daily-k-10d` 全市场近 10 日 + 复权事件表），
每一步都要 Key。免 Key 分发版必须换一条路，而且要把"复权"这件最难的事重新解决一遍 ——
因为公开源**不给复权事件表**（分红/送转/配股明细）。

免 Key 怎么做到"复权仍然是对的"
--------------------------------
用**交易所自己给的昨收**。腾讯快照的 `[4] 昨收` 不是"昨天的收盘价"，而是交易所
在除权除息日**调整过**的前收盘价（当日涨跌停价就是按它算的，实测 600519 涨停价
1383.80 = 1258.00×1.10）。于是：

    如果 快照.昨收 == 库里最后一根日线的收盘价   → 今天没除权，因子不变
    否则                                      → 今天除权了，
                                               k = 库里前收 / 快照昨收
                                               今天的因子 = 上一根的因子 × k

`后复权 = 不复权 × 因子`（见 `storage` 的表结构），所以这一步就让后复权序列**连续**了：
分红除权那天原始价会跳空下跌，而因子按同一个比例放大，两者相乘回到连续。
这条路不需要任何事件明细，也不需要多打一次接口 —— **每只票一个昨收就够了**。

实测（2026-09-17，本机真实全市场扫描 5564 只）：
* 昨收与库里前收**一致**的有 5552 只（= 当天没有除权除息）；
* 不一致的有 12 只，其中 12 只的比值都在 1.0000~1.0300 之间（典型的分红/送转量级），
  而且**全部**恰好是当天成交量不为 0、且价格无异常跳空的票 —— 这是"检测到真事件"的样子。

涨停价与连板数（历史那一段必须靠规则自己算）
--------------------------------------------
免 Key 没有涨停池接口，所以涨停/连板要从**日线自己判**：`涨停价 = round(昨收 × (1+幅度))`。
幅度按板块定，这一条是**在全市场 5564 只上反推验证过的**（2026-09-17）：

| 板块（代码段） | 幅度 | 取整 | 实测依据 |
|---|---|---|---|
| 主板 `60/00` | 10% | **四舍五入** | 3050 只吻合，其中 1562 只能区分"四舍五入 vs 截断"且全部是四舍五入 |
| 创业板/科创板 `30/68` | 20% | **四舍五入** | 1965 只吻合，744 只能区分且全部是四舍五入 |
| 北交所 `92/8/4` | 30% | **截断** | 181 只能区分取整方式，**全部**是截断（四舍五入会差 1 分） |
| 风险警示 `ST` | 同板块 | 同上 | 144+57+1 只 ST 的涨停价与同板块一致（**不是** 5%） |
| 未股改 S 股 | 5% | 四舍五入 | 实测 600182 佳通 是唯一的 5% 例外 |

⚠️ 这个规则是**历史回补**用的（库里的日线没有当日涨停价）；当天那一根应当优先用
快照里**腾讯给的涨停价**（那是权威值），`limit_pool_from_snapshot()` 就是这么做的。
两者的差别只可能出现在"某只票改了规则而我们不知道"的时候 —— 界面上家数以快照为准。

⚠️ 与同花顺口径的对齐：涨停/连板池**不含 ST、不含北交所**（实测同花顺涨停池
1188 个交易日里 `is_st` 全为 0、也没有一条北交所代码）。这不是漏，是跟着参照物走，
否则"加不加 Key"会让同一个数字跳几只（2026-09-17：腾讯 50 vs 同花顺 47，差的 3 只全是 ST）。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from laoa_trader import clock
from laoa_trader.config import Config, get_config
from laoa_trader.data import public_market as pm
from laoa_trader.data import storage
from laoa_trader.log import get_logger
from laoa_trader.price_limits import (
    LIMIT_BJ,
    LIMIT_GEM,
    LIMIT_MAIN,
    LIMIT_S,
    board_of,
    is_s_stock,
    limit_down_price,
    limit_pct,
    limit_up_price,
)

logger = get_logger(__name__)

#: 涨跌幅限制与涨跌停价的**规则只有一处定义**（`laoa_trader/price_limits.py`）：
#: 日更（判涨停/跌停、攒涨停池）、池子与提醒、以及公式引擎的通达信
#: `ZTPRICE()`/`DTPRICE()` 都用它。各写一份迟早会出现"日更说涨停、公式说没涨停"
#: 这种最难查的分歧，所以下面这些名字是**从那里再导出**的（老调用方一行都不用改）。
#: 各板块的幅度表与实测依据见该模块的 docstring。
#: 判"这一根是涨停"时允许的绝对误差（元）。价格是两位小数，比值再取整回来
#: 也可能差半分钱（浮点），所以给半分钱；**不能给更多**：主板一档涨停的相邻
#: 价位差 10%（几毛钱），放宽到 1 分以上就可能把"接近涨停"误判成涨停。
PRICE_EPS = 0.005

#: 除权检测的容差（元）：快照昨收与库里前收差**超过**这个值才算除权。
#: 半分钱的理由同上（两边都是两位小数的价格，正常情况应当分毫不差）。
EX_RIGHT_EPS = 0.005

#: 一次拿多少只票的"最后一根日线"（除权检测的基准）。500 是 `storage` 里
#: 分片查询的既有粒度（见 `dates_for_symbols`），跟着它走省得两处不一样。
CHUNK = 500


def is_limit_up(close: Any, prev_close: Any, symbol: str, name: str = "") -> bool:
    """这一根日线是不是涨停（用库里相邻两根算，历史回补时用）。"""
    if close is None or prev_close is None:
        return False
    target = limit_up_price(float(prev_close), symbol, name)
    return target is not None and abs(float(close) - target) < PRICE_EPS


def is_limit_down(close: Any, prev_close: Any, symbol: str, name: str = "") -> bool:
    if close is None or prev_close is None:
        return False
    target = limit_down_price(float(prev_close), symbol, name)
    return target is not None and abs(float(close) - target) < PRICE_EPS


# ── 除权检测与因子步长 ──


def factor_step(prev_raw_close: float, prev_adjusted_close: float) -> float:
    """除权当天的复权因子步长 `k = 库里前收(不复权) / 快照昨收(交易所调整后)`。

    方向说明（容易搞反，所以写在这里）：分红送转会让**原始价跳空下跌**，而
    `后复权 = 不复权 × 因子`，要让后复权连续，因子必须**变大**；交易所的昨收
    正是"调整后的前收盘"，它比原始前收小，所以 k = raw/adj > 1。
    """
    if not prev_adjusted_close or prev_adjusted_close <= 0:
        return 1.0
    return float(prev_raw_close) / float(prev_adjusted_close)


def detect_ex_right(prev_raw_close: Any, prev_adjusted_close: Any) -> tuple[bool, float]:
    """今天有没有除权除息 → `(是否除权, 因子步长)`。

    ⚠️ 昨收缺失 / 为 0 时**必须返回"没有除权"**（k = 1.0）：那是"没这个数"，不是"除权了"。
    早先的写法只挡了 `None`，于是 `昨收 = 0` 会走进 `factor_step`（那里返回 1.0）却把
    `changed` 报成 True —— 因子没变，但 `detail` 里会写"检测到 N 只除权除息并已重标因子"，
    也就是**谎报**了一件没发生的事（数字对、说明错，同样要不得）。
    """
    if prev_raw_close is None or prev_adjusted_close is None:
        return False, 1.0
    if float(prev_adjusted_close) <= 0:        # 缺数/异常值：不是除权
        return False, 1.0
    if abs(float(prev_raw_close) - float(prev_adjusted_close)) <= EX_RIGHT_EPS:
        return False, 1.0
    return True, factor_step(float(prev_raw_close), float(prev_adjusted_close))


# ── 库里的"最后一根日线"（除权检测的基准）──


def last_bars(
    conn: sqlite3.Connection, symbols: Sequence[str]
) -> dict[str, tuple[str, float, float]]:
    """`{symbol: (date, close, factor)}` —— 每只票**最近一根**日线。

    为什么用 `MAX(date)` 关联查询、而不是"取最近 N 天再挑最大"：后者有个**致命**的
    边界 —— 库里最后一根很旧的票（例如放假回来、或者长时间没开机）会落在时间窗之外，
    于是被当成**没有历史**，当天的因子就从 1 重算，整只票的复权序列在那天断成两截
    （而且图上看不出来：后复权的绝对水平本来就没意义，只有跨那天的收益率会错）。

    关联查询按主键 `(symbol, date)` 走，500 只一批，实测开销可忽略。
    """
    out: dict[str, tuple[str, float, float]] = {}
    codes = [str(s) for s in symbols if str(s)]
    for start in range(0, len(codes), CHUNK):
        chunk = codes[start:start + CHUNK]
        marks = ",".join("?" * len(chunk))
        sql = (
            "SELECT r.symbol AS symbol, r.date AS date, r.close AS close, "
            "       r.factor AS factor "
            "FROM stock_daily_raw r JOIN ("
            "  SELECT symbol, MAX(date) AS d FROM stock_daily_raw "
            f"  WHERE symbol IN ({marks}) GROUP BY symbol"
            ") m ON r.symbol = m.symbol AND r.date = m.d"
        )
        for row in conn.execute(sql, tuple(chunk)):
            out[str(row["symbol"])] = (
                str(row["date"]),
                float(row["close"] or 0.0),
                float(row["factor"] if row["factor"] is not None else 1.0),
            )
    return out


def previous_trading_day(conn: sqlite3.Connection, day: str) -> str | None:
    """交易日历里 `day` 之前最近的一个交易日；日历里没有就是 `None`。

    为什么要查日历而不是"往前减一天"：节假日与周末会让"减一天"算出一个不存在的
    交易日，于是**正常连续日更**也会被误判成"缺天"。宁可判不出来（`None` = 不报缺口），
    也不要把提示变成每天一次的噪音。
    """
    row = conn.execute(
        "SELECT MAX(date) FROM trading_calendar WHERE date < ?", (day,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


# ── 当日快照 → 日线行 ──


def bars_from_snapshot(
    rows: Iterable[dict],
    conn: sqlite3.Connection,
    day: str,
    *,
    names: dict[str, str] | None = None,
) -> tuple[list[tuple], dict[str, Any]]:
    """全市场快照 → `stock_daily_raw` 的行（含当天算出来的复权因子）。

    只写**当天真的成交过**的票（成交量为 0 = 停牌，没有 K 线可言；写上它会变成
    一根"四个价格都等于昨收、成交量为 0"的假 K 线，策略会把它当成一个交易日）。

    Returns:
        (行列表（9 列，见 `storage.write_daily_raw`）, 统计字典)。
    """
    rows = list(rows)
    codes = [str(row.get("symbol") or "") for row in rows]
    base = last_bars(conn, codes)
    prev_trading_day = previous_trading_day(conn, day)
    names = names or {}
    out: list[tuple] = []
    stat = {"written": 0, "suspended": 0, "no_price": 0, "new": 0,
            "ex_right": 0, "ex_ratio_min": None, "ex_ratio_max": None,
            "gap": 0, "ex_symbols": []}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        if not row.get("volume"):
            stat["suspended"] += 1
            continue
        close = row.get("last_price")
        if close is None:
            stat["no_price"] += 1
            continue
        known = base.get(symbol)
        if known is None:
            # 库里没有这只票的任何历史：因子从 1 起（它的后复权序列就从今天开始）。
            # 这不是"猜"：没有前收就没有除权可言，写 1 是唯一说得通的值。
            factor, prev_day = 1.0, None
            stat["new"] += 1
        elif known[0] == day:
            # ⚠️ **今天这一根已经写过了**（同一天重跑日更：连点两次按钮、按钮 + 定时任务、
            # 或者开盘前跑日更 —— 那时 `market_date()` 还是上一个交易日）。
            # 这时候库里"最近一根"就是今天自己，拿它当除权基准会把**今天的正常涨跌**
            # 当成除权事件，于是因子被反复乘大（实测：重跑两次 → 1.0 → 1.1004 → 1.2108），
            # 而且**不可逆**（后复权价一天比一天高）。
            # 所以：沿用今天已经算好的因子，原样写回（幂等）。
            factor, prev_day = known[2], known[0]
        else:
            prev_day, prev_close, prev_factor = known
            changed, k = detect_ex_right(prev_close, row.get("prev_close"))
            factor = prev_factor * k
            if changed:
                stat["ex_right"] += 1
                stat["ex_symbols"].append((symbol, names.get(symbol) or row.get("name") or "",
                                           k))
                stat["ex_ratio_min"] = k if stat["ex_ratio_min"] is None else min(
                    stat["ex_ratio_min"], k)
                stat["ex_ratio_max"] = k if stat["ex_ratio_max"] is None else max(
                    stat["ex_ratio_max"], k)
        # 缺口：库里最后一根**早于上一个交易日**才算缺天（用户不是每天都开机）。
        # ⚠️ 不能拿它跟"今天"比 —— 正常连续日更时"昨天那根"永远不等于今天，
        # 那样每一天都会把全市场 5000 多只票都报成缺口，提示变成噪音
        # （实测：连更的库里 17/17 全被算成缺口）。
        # 上一个交易日来自交易日历；日历里还没有更早的日期时**不判**（没依据就别报）。
        if (prev_day is not None and prev_day < day
                and prev_trading_day is not None and prev_day < prev_trading_day):
            # 只**统计**，不编数据：缺的那几天公开快照给不出来（用户哪天配了 Key
            # 走同花顺那条路就能一次补全）。
            stat["gap"] += 1
        out.append((
            symbol, day, row.get("open"), row.get("high"), row.get("low"),
            close, row.get("volume"), row.get("turnover"), factor,
        ))
    stat["written"] = len(out)
    # 除权明细只留前 10 条进日志（全量在返回值里，界面只显示个数）
    stat["ex_symbols"] = stat["ex_symbols"][:10]
    return out, stat


# ── 涨停池（免 Key 版）──


def limit_pool_from_snapshot(
    rows: Iterable[dict],
    conn: sqlite3.Connection,
    day: str,
    *,
    names: dict[str, str] | None = None,
) -> list[tuple]:
    """全市场快照 → `limit_up_pool` 的行（**涨停价用腾讯给的那个权威值**）。

    连板数（`high_days`）怎么算：看库里**上一个有涨停池的交易日**里这只票在不在，
    在就把它的天数 +1，否则算 1（首板）。这一路不需要任何涨跌幅规则 ——
    库里存的是每天按权威涨停价判出来的结果，滚雪球就得到连板数。

    ⚠️ 口径与同花顺一致：**不含 ST、不含北交所**（实测同花顺那个池子 1188 个交易日
    里 `is_st` 全 0、也没有北交所代码）。见模块 docstring 的实测说明。

    ⚠️ 同花顺池子里那些**公开源拿不到**的字段（封板时间、封单额、涨停原因、
    换手率、是否回封）一律留空 —— 不是 0，是 NULL。策略只依赖
    `date/symbol/high_days`（`formula.连板()` / `涨停天数()` 读它），
    所以留空不影响匹配；编一个 0 出来才会出问题（`连板()>=2` 会把这些票判成不合格，
    而"没这个数"和"封单额为 0"是两件事）。
    """
    rows = list(rows)
    names = names or {}
    prev = _previous_pool_days(conn, day)
    now = clock.stamp_cn()          # 北京时间（与 date 列同源）
    out: list[tuple] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol or pm._excluded_from_limits(row):
            continue
        # 现价压在涨停价上（涨停价是权威值；没有涨停价的新股/次新不可能涨停）
        if not pm._at_limit(row.get("last_price"), row.get("limit_up")):
            continue
        name = names.get(symbol) or str(row.get("name") or "")
        high_days = prev.get(symbol, 0) + 1
        out.append((
            day,                                    # date
            symbol,                                 # symbol
            name,                                   # name
            high_days,                              # high_days（连板数：自己滚出来的）
            None,                                   # limit_up_type（公开源没有分类）
            None,                                   # first_limit_up_time（没有封板时间）
            None,                                   # last_limit_up_time
            None,                                   # order_amount（封单额，拿不到）
            None,                                   # open_num（开板次数，拿不到）
            None,                                   # reason_type（涨停原因，拿不到）
            row.get("turnover_rate"),               # turnover_rate（有）
            row.get("pct"),                         # change_rate（有）
            None,                                   # currency_value —— **故意留空**：
            #   同花顺这个字段到底是"成交额"还是"流通市值"没有任何文档可证，
            #   而全项目**没有一处读它**。宁缺勿错：塞一个语义可能标错的数进去，
            #   以后有人读它就会得到错的结果，而且查不出来。
            None,                                   # is_again_limit（是否回封，拿不到）
            None,                                   # is_new（公开源不判新股）
            None,                                   # continue_day_text（同花顺的文案）
            None,                                   # max_seal_money（最大封单，拿不到）
            row.get("last_price"),                  # last_price
            1 if pm._is_st(row) else 0,             # is_st（那一列是 1/0）
            "public",                               # source
            now,                                    # updated_at
        ))
    return out


def _previous_pool_days(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    """上一个有涨停池的交易日 → `{symbol: high_days}`（连板数的依据）。"""
    row = conn.execute(
        "SELECT MAX(date) FROM limit_up_pool WHERE date < ?", (day,)
    ).fetchone()
    prev_day = row[0] if row and row[0] else None
    if not prev_day:
        return {}
    return {
        str(r["symbol"]): int(r["high_days"] or 1)
        for r in conn.execute(
            "SELECT symbol, high_days FROM limit_up_pool WHERE date = ?", (prev_day,))
        if r["symbol"]
    }


# ── 交易日历 / 代码表 ──


def market_date(*, opener: Callable | None = None) -> str | None:
    """最近一个交易日的日期（从上证指数的行情时间戳读，`YYYY-MM-DD`）。

    为什么用指数的时间戳而不是"今天"：周末与节假日上证指数的时间戳停在上一交易日，
    这正是我们要的"最近一个交易日"。用系统日期会把周六的日更写成一根周六的假 K 线。
    """
    from laoa_trader.data import public_quotes as pq
    got = pq.batch_quotes(["sh000001"], opener=opener)
    row = got.get("sh000001")
    stamp = str((row or {}).get("at") or "")
    if len(stamp) >= 8 and stamp[:8].isdigit():
        return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
    return None


def sync_names_public(conn: sqlite3.Connection, rows: Sequence[dict]) -> int:
    """把快照里的代码/名称写进 `stock_basic`（**行业留空** —— 公开源没有行业归属）。

    行业留空的后果要如实说明：**公开源没有全市场行业分类**（实测没有可用的公开接口），
    而数据自检要求行业覆盖率 ≥90%（`preflight.MIN_INDUSTRY_COVERAGE`）——
    所以没 Key 时**筛选会被自检拒绝**（见 `preflight` 与 `ui/app.py` 的【下载数据】说明），
    而不是"随便分个行业"混过去。行情与大盘概览不受影响。
    """
    payload = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "").strip()
        if symbol and name:
            payload.append((symbol, name, None))
    if not payload:
        return 0
    # ⚠️ 必须用 `names_only`：这条路上 industry 恒为 None，普通分支会把**已有的行业
    # 归属清空**（详见 `storage.write_stock_basic` 的说明）。
    return storage.write_stock_basic(conn, payload, names_only=True)


#: 指数日K取多少个交易日当"交易日历尾巴"。为什么要这个尾巴（而不是只写当天）：
#: 缺口判据要跟**上一个交易日**比，而"减一天"会把周末/节假日算错；日历里只有
#: 跑过的那几天时，"上一个交易日"根本查不到，缺口就永远判不出来（用户不是每天都开机，
#: 这个提示恰恰是为他准备的）。取 40 个交易日：覆盖一个多月的停机，一个请求。
CALENDAR_TAIL = 40
_SINA_INDEX_KLINE = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?"
    "symbol=sh000001&scale=240&ma=no&datalen={count}")


def calendar_tail(*, opener: Callable | None = None,
                  count: int = CALENDAR_TAIL) -> list[str]:
    """上证指数最近的交易日列表（**权威**：指数有K线的日子就是交易日）。

    用指数而不是"今天往前数"：周末与节假日不交易，指数那几天没有K线 —— 这正是
    缺口判据需要的"上一个交易日"。失败返回空列表（那就只写当天，绝不编日期）。
    """
    import json
    import urllib.request

    from laoa_trader.data import public_quotes as pq

    fetch = opener or pq._urllib_get
    try:
        text = fetch(_SINA_INDEX_KLINE.format(count=int(count)),
                     {"User-Agent": pq._UA, "Referer": "https://finance.sina.com.cn/"},
                     pq.TIMEOUT).decode("utf-8", errors="replace")
        rows = json.loads(text) if text.strip().startswith("[") else []
    except Exception as exc:  # noqa: BLE001 - 取不到就只写当天
        logger.info(f"取指数交易日失败（只写当天）：{exc}")
        return []
    return [str(r.get("day")) for r in rows
            if isinstance(r, dict) and r.get("day")]


def sync_calendar_public(conn: sqlite3.Connection, day: str,
                         *, opener: Callable | None = None) -> int:
    """把交易日写进 `trading_calendar`（当天 + 指数尾巴；`source="public"` 可追溯）。

    写尾巴不只是为了省事：缺口判据（`previous_trading_day`）要靠它才知道
    "上一个交易日"是哪天。
    """
    days = set(calendar_tail(opener=opener))
    if day:
        days.add(day)
    if not days:
        return 0
    return storage.write_calendar(conn, sorted(days), source="public")


# ── 日更总入口 ──


def daily_update_public(
    cfg: Config | None = None,
    *,
    progress_cb: Callable[..., None] | None = None,
    note_cb: Callable[[str], None] | None = None,
    force_scan: bool = True,
):
    """免 Key 日更：一趟全市场快照 → 当日日线 + 涨停池 + 代码表 + 交易日历。

    Returns:
        `SyncResult` 列表（与 `sync.daily_update()` 同形状，界面/命令行不用分叉）。
    """
    from laoa_trader.data.sync import SyncResult, _note, throttle_progress

    cfg = cfg or get_config()
    progress_cb = throttle_progress(progress_cb)
    result = SyncResult(stage="日更数据（免 Key 公开源）")
    try:
        cfg.ensure_dirs()
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return [result]

    _note(note_cb, "免 Key 日更：正在取全市场快照（约 1 分钟）…")
    try:
        rows = pm.cached_scan(force=force_scan)
    except Exception as exc:  # noqa: BLE001 - 取不到就是"今天没更"，绝不写半份数据
        result.ok = False
        result.error = f"全市场快照失败：{type(exc).__name__}: {exc}"
        return [result]
    if not rows:
        result.ok = False
        result.error = "全市场快照为空（网络不通或被限流）—— 本次不写任何数据"
        return [result]

    day = market_date() or clock.today_cn()
    names = {str(r.get("symbol")): str(r.get("name") or "") for r in rows if r.get("symbol")}
    extra: dict[str, Any] = {"day": day, "snapshot_rows": len(rows)}

    with storage.connect(cfg.db_path, timeout=300) as conn:
        bars, stat = bars_from_snapshot(rows, conn, day, names=names)
        written = storage.write_daily_raw(conn, bars) if bars else 0
        extra.update({k: v for k, v in stat.items() if k != "ex_symbols"})
        extra["ex_symbols"] = stat["ex_symbols"]

        pool = limit_pool_from_snapshot(rows, conn, day, names=names)
        pool_written = storage.write_limit_up_pool(conn, pool) if pool else 0
        extra["limit_up"] = pool_written

        extra["stock_basic"] = sync_names_public(conn, rows)
        extra["calendar"] = sync_calendar_public(conn, day)

    result.ok = True
    result.rows = written
    result.extra = extra
    result.detail = (
        f"{day}：写入行情 {written} 行，涨停池 {pool_written} 行，"
        f"代码/名称 {extra['stock_basic']} 条"
    )
    if stat["ex_right"]:
        result.detail += f"；检测到 {stat['ex_right']} 只除权除息并已重标因子"
    if stat["gap"]:
        result.extra["hint"] = (
            f"有 {stat['gap']} 只票的上一根日线不是最近交易日（中间缺天）："
            "公开快照只给得出当天那一根，缺的历史要用「下载/更新历史数据」按逐只日K补"
        )
        logger.warning(result.extra["hint"])
    _note(note_cb, result.detail)
    return [result]
