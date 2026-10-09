"""大盘状态（强/中/弱）与「弱市抬门槛」：**纯本地库，一个网络请求都不发**。

为什么要这一层（2026-10-08 主人定的口径）
----------------------------------------
大盘明显走弱的那几天，选出来的名单质量会掉 —— 不是公式坏了，而是"今天在涨的票"
本来就只有一成：同一条公式在弱势日里命中的 20 只，多半是"跌得比别人少"，
而不是"有人在买"。所以在**执行层**加一道宏观约束：弱市时只留下"跑赢全市场"的票
（参考同类系统的做法：弱势时把相对强弱门槛从 0% 提到 2%），
并且**在界面上说清为什么名单变短**（进 `run.warnings` / `preview["notes"]`）。

三条铁律（每条都对应一类最难查的错）
----------------------------------
1. **不知道 ≠ 弱市**：数据不足时 `level = 未知`、`usable = False`，**绝不当成弱市** ——
   那会在用户什么都没做错的情况下砍掉他今天的名单；
2. **信号缺席就是缺席**：缺席的信号**不投 0 票**。"0 票"的意思是"我看了，是中性"，
   与"我没看"是两件事；可判信号少于 `MIN_VOTING_SIGNALS` 个就不下结论；
3. **facts 里每一项都要能被用户核对**：带具体数字（`上涨 623 / 下跌 4312`），
   而不是「大盘偏弱」这种没有出处的形容词。

谁在用（为什么必须**共用同一份判断**）
------------------------------------
`strategy/formula_group.run_enabled_formulas()`（【开始筛选】那条路）与
`formulas.preview_hits()`（策略编辑页【运行】那条路）**调的是同一个 `apply_gate()`**。
两处各写一套筛选，迟早会出现"试算说 5 只、匹配只有 2 只"这种用户无法解释的差异 ——
这与 `formulas.prepare_inputs`（K 线口径）、`formula_group.confirm_days_of`
（连续确认）是同一个理由。

口径与边界（写清楚，别让用户猜）
------------------------------
* **只用本库**：不取指数快照、不看新闻、不发任何请求（离线/免 Key 都照常工作）；
* **基准与 `research/scorecard.py` 的 α 基准同口径**：同一份后复权价
  （`stock_daily_hfq`）、同一批**共同交易日**（按全市场日期对齐，停牌票自然缺席、
  按"有效样本"参与平均）、等权**算术平均**。唯一有意的差别：scorecard 的基准是
  回测口径，会剔掉"买不进"的一字板样本（那是可成交性要求）；这道门槛不剔 ——
  它问的是"这段时间谁比大盘强"，不是"能不能买到"；
* **窗口是"最近 N 个交易日"的收盘 → 收盘**，不做 D+1 错位（同上：这是筛选，
  不是一笔可成交的交易）；
* 全市场有效样本少于 `MIN_BENCH_SYMBOLS` 时**不产出基准**：几十只票的平均收益
  不是"市场"（与 `scorecard.MIN_MARKET_SYMBOLS` 同一个判断，数值也取同一个）；
* **盘中那一轮看的是"最后一根收盘"**：开盘时间里匹配用的是实时快照拼出的今天那一根
  （`formulas.prepare_inputs`），而三个信号读的是库里已经收盘的 K 线 ——
  今天的广度/涨跌停本来就不在本地（要算就得联网取全市场快照，而本模块一个请求都不发）。
  所以两条路都传 `day = prepared.kline_day`（最后一根收盘），
  事实里也**写明数字是哪一天的收盘**，不让用户以为那是此刻的盘面。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from laoa_trader.data import storage
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 上证指数的**库里写法**（`data/sync.py` 的映射表就是这个名字）。
#: 只认这一个：指数日线只有配了同花顺 Key 的日更才会写（见 `detect` 里的说明），
#: 换个名字去猜（`000001.SH` / `sh000001`）只会把"没有数据"变成"算出一个别的数"。
INDEX_SYMBOL = "sh.000001"

#: 指数均线长度：20 个交易日（约一个月）是"月线级"的趋势线，
#: 与公式里最常用的 `MA(C,20)` 同一档；短于它就只是在比昨天的涨跌。
MA_WINDOW = 20

#: 信号数下限：可判信号少于它就不下结论（"只看到一个信号就喊弱市"太容易误判）。
MIN_VOTING_SIGNALS = 2

#: 全市场等权基准要求当日有效样本数下限。为什么与 `scorecard.MIN_MARKET_SYMBOLS`
#: 取同一个数（5）：**同一个判断**（几十只票的平均不是市场），两处口径不一致的话
#: 会出现"成绩单认定这不是市场、门槛却拿它当基准"这种自相矛盾。
MIN_BENCH_SYMBOLS = 5

#: 门槛默认值（弱市时要求**跑赢全市场**的百分数、以及相对强弱的窗口）。
#: 默认值同时写在 `config.py` 的 `market_regime_rs_min_pct` / `market_regime_window`
#: 上，这里留一份是为了"配置读不出来时的回退"有唯一出处（见 `_min_pct_value`）。
DEFAULT_RS_MIN_PCT = 2.0
DEFAULT_WINDOW = 20

#: 窗口的合法区间：1 是"比昨天"，量不出"相对强弱"；250 是一个交易年的长度，
#: 再长在分发包的半年库里必然算不出来（`apply_gate` 会因此整道门槛不生效）。
#: **小于 2 的直接回退默认 20**（笔误不该悄悄变成另一套口径），大于 250 的夹到 250
#: （见 `_window_value`；与 `formula_group.confirm_days_of` 的夹紧是同一种做法）。
MIN_WINDOW = 2
MAX_WINDOW = 250

#: 浮点余量：收益率是百分数，恰好等于门槛时（rs = 2.0 与 min_pct = 2.0）
#: 不该因为 1e-15 的误差被判成"没达标" —— 恰好达标就是达标。
_EPS = 1e-9


@dataclass
class Regime:
    """大盘状态的一次判定结果（**纯数据**，不碰界面也不碰连接）。"""

    #: `强` / `中` / `弱` / `未知`；`未知` 的语义是"数据不够，不下结论"
    level: str = "未知"
    #: 强度刻度 0~100（三个信号全投偏强 = 100、全投偏弱 = 0、中性 = 50）。
    #: **`None` = 算不出来**（与 `level = 未知` 配套）：绝不用 50/中 冒充"不知道"。
    #: 注意它与 `level` 故意不严格对应：2 个信号里"1 弱 1 中性"按"过半"规则判**中**，
    #: 但强度分只有 25 —— 一个是**结论**，一个是**刻度**，两者都摆出来给用户看。
    score: int | None = None
    #: 判定用的行情日（库里最新的那个交易日；`detect(day=...)` 传了就是它）
    day: str | None = None
    #: **中文事实清单**（带具体数字，用户能自己核对）。缺席的信号也在这里如实写出来
    #: （"…这一项不投票"），因为它正是"结论为什么只由 N 个信号得出"的解释。
    facts: list[str] = field(default_factory=list)
    #: 数据够不够下结论（可判信号 ≥ `MIN_VOTING_SIGNALS`）
    usable: bool = False
    #: 各信号投的票：`{信号名: +1/0/-1}`，**缺席的信号不在里面**（不是 0）
    votes: dict[str, int] = field(default_factory=dict)

    @property
    def weak(self) -> bool:
        """是不是"弱市"（= 可以据此抬门槛的那一刻）。"""
        return self.usable and self.level == "弱"

    def summary(self) -> str:
        """一句话中文结论（界面/日志用；没数据时也如实说"未知"）。"""
        head = "；".join(self.facts) if self.facts else "没有可用的事实"
        return f"大盘{self.level}（{head}）"


# ══════════════════════════════════════════════════════════════════════════
# 信号一：广度（最新行情日上涨家数 vs 下跌家数）
# ══════════════════════════════════════════════════════════════════════════


def _recent_days(conn: sqlite3.Connection, *, upto: str | None = None,
                 limit: int | None = None) -> list[str]:
    """后复权行情里最近若干个交易日（**降序**，第一个就是最新行情日）。

    为什么用 `stock_daily_hfq`（视图）而不是 `stock_daily_raw`：两者日期集合
    完全一样（视图就是那张表加了因子），但下游全是按后复权价说话的 ——
    同一份表名能免掉"广度用原始价、相对强弱用后复权价"这种查不出来的不一致。
    """
    sql = "SELECT DISTINCT date FROM stock_daily_hfq WHERE date IS NOT NULL"
    args: list[Any] = []
    if upto:
        sql += " AND date <= ?"
        args.append(str(upto))
    sql += " ORDER BY date DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"  # noqa: S608 - limit 是整数，拼进去安全
    return [str(row[0]) for row in conn.execute(sql, args)]


def _breadth_vote(conn: sqlite3.Connection, days: Sequence[str]) -> tuple[int | None, str]:
    """广度信号：**最新行情日**上涨家数 vs 下跌家数 → `(票, 事实)`。

    为什么用"最后两个交易日的收盘价比一比"，而不是去算"20 日新高新低"：
    这一步只是要一个**大盘当天的方向**，两个交易日各 ~5500 行、一次聚合就够，
    而更复杂的口径在免 Key 的小库里经常算不出来（缺数据就整条信号作废）。
    平盘（收盘价相等）既不算涨也不算跌 —— 全部平盘（涨跌都为 0）时**不投票**：
    那说明这不是一份真实行情（合成/空壳库），不是"市场很中性"。
    """
    if len(days) < 2:
        return None, "广度：库里不足两个交易日的行情，这一项不投票"
    latest, previous = days[0], days[1]
    row = conn.execute(
        "SELECT SUM(CASE WHEN d.close > p.close THEN 1 ELSE 0 END),"
        "       SUM(CASE WHEN d.close < p.close THEN 1 ELSE 0 END) "
        "FROM stock_daily_hfq d "
        "JOIN stock_daily_hfq p ON p.symbol = d.symbol AND p.date = ? "
        "WHERE d.date = ? AND d.close IS NOT NULL AND p.close IS NOT NULL",
        (previous, latest),
    ).fetchone()
    up, down = int((row[0] if row else 0) or 0), int((row[1] if row else 0) or 0)
    if up + down == 0:
        return None, f"广度：{latest} 既没有上涨也没有下跌的票，这一项不投票"
    if up > down:
        vote = 1
    elif up < down:
        vote = -1
    else:
        vote = 0
    return vote, f"上涨 {up} / 下跌 {down}（{latest} 收盘）"


# ══════════════════════════════════════════════════════════════════════════
# 信号二：涨跌停家数
# ══════════════════════════════════════════════════════════════════════════


def _limit_vote(conn: sqlite3.Connection, db_path: Path, day: str,
                days: Sequence[str]) -> tuple[int | None, str]:
    """涨跌停信号：当日**涨停家数** vs **跌停家数** → `(票, 事实)`。

    `day` = 要判定的那一天；`days` = **库里真实的最新若干个交易日**（降序，不按 `day`
    过滤）—— 两者只有相等时这个信号才算得出来。

    跌停家数**复用 `pool.limit_down_industries()`**（只读调用，不改它）：
    判跌停的规则只许有一个真源（`price_limits.limit_down_price`，
    `pool.py` 那份就是照它算的），这里另写一套"close/prev−1 小于 −9.5%"之类的
    近似，迟早出现"界面说 37 家跌停、门槛说 12 家"。

    两条**已知口径**（不是 bug 就该照它说）：
    * 那个函数只统计**有行业归属**的票（它 JOIN 了 `stock_basic.industry`）——
      涨停家数（`limit_up_pool` 全量）与它严格来说不是一个口径，
      所以事实里把这句话写出来，用户才能核对；
    * 它收到的 `day` 参数**没有用**（按库里最近两个交易日算，见那里的说明）——
      因此**只有判定的日子就是库里最新行情日**时才敢用它的数：
      否则"涨停按 day、跌停按另两天"会把两个日子的盘面混成一个结论。
    """
    if len(days) < 2:
        return None, "涨跌停：库里不足两个交易日的行情，这一项不投票"
    newest = days[0]
    row = conn.execute(
        "SELECT COUNT(*) FROM limit_up_pool WHERE date = ?", (day,)
    ).fetchone()
    limit_up = int((row[0] if row else 0) or 0)
    if limit_up <= 0:
        # 涨停池是空的：**分不清"当天一只都没涨停"与"涨停池根本没更新"**
        # （免 Key 的日更、或那天的快照没取到都会这样）。宁可缺席，也不投 0 票 ——
        # 把"没有数据"读成"市场很平静"正是这道门槛最危险的失败方式。
        return None, f"涨跌停：没有 {day} 的涨停池数据，这一项不投票"
    if day != newest:
        return None, (f"涨跌停：跌停家数只能按库里最新交易日（{newest}）算，"
                      f"与判定的 {day} 对不上，这一项不投票")
    # 延迟导入：`pool.py` 会拉起整个池子/推送那一层，而本模块是"轻量纯本地库"
    # （行情页、试算、定时任务都会调它）—— 模块级导入会把那一大片顺带拉进来。
    from laoa_trader import pool

    # 上面已经确认库里有 ≥2 个交易日的行情，所以这里的空字典只有一个意思：
    # **一只跌停都没有**（那个函数返回 {} 的另一种情形是"不足两个交易日"，
    # 已经在 `_limit_vote` 开头排除掉了），可以放心当成 0 家。
    downs = pool.limit_down_industries(str(db_path))
    limit_down = sum(int(v) for v in downs.values())
    if limit_up > limit_down:
        vote = 1
    elif limit_up < limit_down:
        vote = -1
    else:
        vote = 0
    return vote, f"涨停 {limit_up} / 跌停 {limit_down}（跌停按有行业分类的票统计）"


# ══════════════════════════════════════════════════════════════════════════
# 信号三：上证指数与 MA20
# ══════════════════════════════════════════════════════════════════════════


def _index_vote(conn: sqlite3.Connection, day: str) -> tuple[int | None, str]:
    """指数信号：上证收盘价 vs 自己的 MA20 → `(票, 事实)`。

    ⚠️ **指数日线只有配了同花顺 Key 的日更才会写**（`data/sync.py` 的指数映射表）。
    免 Key 用户可能一条都没有 —— 那时这个信号**缺席**（不投票），
    绝不当成中性、更不许当成弱市：这正是铁律①与②的出处。
    """
    rows = conn.execute(
        "SELECT date, close FROM index_daily "
        "WHERE symbol = ? AND date <= ? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT ?",
        (INDEX_SYMBOL, str(day), MA_WINDOW),
    ).fetchall()
    if not rows:
        return None, (f"指数：库里没有上证指数（{INDEX_SYMBOL}）日线"
                      "（免 Key 的日更不写），这一项不投票")
    if len(rows) < MA_WINDOW:
        return None, (f"指数：上证指数只有 {len(rows)} 个交易日"
                      f"（不足 {MA_WINDOW} 根），这一项不投票")
    closes = [float(row[1]) for row in rows]
    index_day, close = str(rows[0][0]), closes[0]
    ma20 = sum(closes) / MA_WINDOW
    # 指数自己那一天的日期写进事实里：它与全市场最新行情日不一致时（指数没更新到
    # 同一天），用户一眼就能看出这个信号看的是哪一天，而不是以为它拿的是今天的盘。
    where = f"（{index_day}）" if index_day != day else ""
    if close > ma20:
        vote = 1
        text = f"上证{where} {close:.2f} 高于 MA20({ma20:.2f})"
    elif close < ma20:
        vote = -1
        text = f"上证{where} {close:.2f} 低于 MA20({ma20:.2f})"
    else:
        vote = 0
        text = f"上证{where} {close:.2f} 正好落在 MA20({ma20:.2f})"
    return vote, text


# ══════════════════════════════════════════════════════════════════════════
# 判定
# ══════════════════════════════════════════════════════════════════════════


def detect(db_path: str | Path, *, day: str | None = None) -> Regime:
    """从**本地库**算三个信号、各投一票，给出大盘强弱。

    三个信号（各自独立，缺席的不投票）：

    1. **广度**：最新行情日上涨家数 vs 下跌家数（见 `_breadth_vote`）；
    2. **涨跌停**：涨停家数（`limit_up_pool`）vs 跌停家数（`pool.limit_down_industries`）；
    3. **指数**：上证指数收盘价 vs MA20（缺指数数据则缺席，见 `_index_vote`）。

    综合判法（**为什么这么定**：两个信号就能形成"多数"，而"只看到一个信号就喊弱市"
    太容易误判 —— 单看广度，一个板块轮动日就会被读成弱势）：

    * 可判信号 **少于 `MIN_VOTING_SIGNALS`（2）个** → `level = 未知`、`usable = False`；
    * 可判信号里**过半偏弱** → `弱`；**过半偏强** → `强`；其余 → `中`。

    Args:
        db_path: 本地库（只读；库不存在时**不会**顺手建一个空库 —— 如实返回未知）。
        day: 要判定的行情日；None = 库里最新的那个交易日。

    Returns:
        `Regime`（含中文事实清单与各信号投票）。
    """
    path = Path(db_path)
    if not path.is_file():
        # 注意：`storage.connect` 对不存在的文件是"连上就建一个空库"，
        # 那会让这里凭空多出一个空的 trader.db（还可能被日更认成"已有数据"）。
        return Regime(day=day, facts=[f"本地还没有行情库（{path}），算不出大盘强弱"])
    try:
        with storage.connect(path) as conn:
            # `days_all` = 库里真实的交易日（**不按 day 过滤**）：涨跌停那一票要靠它
            # 判断"判定的日子是不是库里最新的那天"（跌停家数只能按最新两天算）
            days_all = _recent_days(conn)
            if not days_all:
                return Regime(day=day, facts=["本地库里没有任何日线行情，算不出大盘强弱"])
            days = [d for d in days_all if day is None or d <= str(day)]
            if not days:
                return Regime(day=day, facts=[
                    f"库里没有 {day} 及之前的行情（最早是 {days_all[-1]}），算不出大盘强弱"])
            latest = days[0]
            breadth, breadth_fact = _breadth_vote(conn, days)
            limit, limit_fact = _limit_vote(conn, path, latest, days_all)
            index, index_fact = _index_vote(conn, latest)
    except sqlite3.Error as exc:
        # 库损坏/被独占：如实说"读不出来"，绝不猜成弱市（铁律①）
        logger.warning(f"读大盘状态失败（本次按未知处理）：{exc}")
        return Regime(day=day, facts=[f"读本地行情库失败（{exc}），算不出大盘强弱"])

    votes: dict[str, int] = {}
    for name, vote in (("广度", breadth), ("涨跌停", limit), ("指数", index)):
        if vote is not None:
            votes[name] = int(vote)
    facts = [breadth_fact, limit_fact, index_fact]

    if len(votes) < MIN_VOTING_SIGNALS:
        # 铁律①：宁可说"不知道"。这里**不产出 score**（不能用 0/50 冒充一个结论）
        return Regime(level="未知", score=None, day=latest, facts=facts,
                      usable=False, votes=votes)

    bull = sum(1 for vote in votes.values() if vote > 0)
    bear = sum(1 for vote in votes.values() if vote < 0)
    if bear * 2 > len(votes):
        level = "弱"
    elif bull * 2 > len(votes):
        level = "强"
    else:
        level = "中"
    score = int(round(50 + 50 * sum(votes.values()) / len(votes)))
    return Regime(level=level, score=score, day=latest, facts=facts,
                  usable=True, votes=votes)


# ══════════════════════════════════════════════════════════════════════════
# 基准与门槛
# ══════════════════════════════════════════════════════════════════════════


def _window_value(window: Any, fallback: int = DEFAULT_WINDOW) -> int:
    """把"窗口"收敛成合法值（写错一律回退，**不抛异常**）。

    为什么夹紧而不是报错：这个值来自用户手写的 config.toml，写错时最糟的结果是
    "整条匹配链路抛异常 ⇒ 一只票都选不出来"。夹到能算的范围里，用户至少还能跑。

    为什么"小于 2"回**默认 20** 而不是夹到 2：0 / 1 / 负数都不是一个"窗口"
    （1 = 比昨天，量不出"相对强弱"），夹成 2 会把一个笔误悄悄变成另一套口径；
    回到默认 20 才是"写错 = 按出厂口径来"。上限方向相反：用户写 9999 明明是要
    "尽可能长"，夹到 `MAX_WINDOW`（一个交易年）比回到 20 更贴近他的本意。
    """
    try:
        value = int(window)
    except (TypeError, ValueError):
        return fallback
    if value < MIN_WINDOW:
        return fallback
    return min(value, MAX_WINDOW)


def _min_pct_value(min_pct: Any, fallback: float = DEFAULT_RS_MIN_PCT) -> float:
    """把"跑赢门槛（%）"收敛成合法值（字符串/负数 → 回退默认值）。

    为什么负数是错的而不是"放宽门槛"：门槛写成负数等于"只要不跑输大盘太多就留"
    ——那与这道门槛的动机（弱市只留明显更强的票）正好相反，几乎肯定是笔误
    （少写一个数字/多了个负号）。**0 是合法边界**（等价于"只留不跑输大盘的票"）。
    """
    try:
        value = float(min_pct)
    except (TypeError, ValueError):
        return fallback
    if value != value or value < 0:      # NaN / 负数
        return fallback
    return value


def gate_enabled(cfg: Any) -> bool:
    """用户有没有开这道门槛（配置写错/没有配置 → 当**没开**）。

    为什么写错当"关"：这是一道会**砍掉用户名单**的门槛，失败方向必须是"什么都别做"
    （与"未知不当弱市"是同一条思路）。
    """
    if cfg is None:
        return False
    return bool(getattr(cfg, "market_regime_gate", True))


def _settings(cfg: Any, min_pct: Any = None, window: Any = None) -> tuple[float, int]:
    """这轮要用的 `(跑赢门槛%, 窗口)`：显式参数优先，其次配置，写错回退默认值。

    单独一个函数是为了**两条路读同一份口径**（试算与匹配）：`min_pct` 一处按 2.0、
    另一处按配置值，就会变成"同一份数据、两个只数"，而用户没有任何办法解释。
    """
    if min_pct is None:
        min_pct = getattr(cfg, "market_regime_rs_min_pct", DEFAULT_RS_MIN_PCT)
    if window is None:
        window = getattr(cfg, "market_regime_window", DEFAULT_WINDOW)
    return _min_pct_value(min_pct), _window_value(window)


def window_returns(db_path: str | Path, *, day: str | None = None,
                   window: int = DEFAULT_WINDOW) -> dict[str, float] | None:
    """**全市场每只票**的 N 日收益 `{代码: 收益}`（小数，0.02 = 2%）。

    对齐口径与 `research/scorecard.market_map` 的 α 基准一致：**按全市场共同的
    交易日锚定**（锚点日没有 K 线的票 —— 停牌/新上市/退市 —— 自然缺席，
    由调用方按"有效样本"处理），同一份后复权价（`stock_daily_hfq`）。

    Returns:
        `{代码: 收益}`；库里不足 `window + 1` 个交易日、或一只都算不出来时返回 None
        （**None 而不是空字典**：调用方必须能区分"没有基准"与"基准是 0"）。
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    window = _window_value(window)
    try:
        with storage.connect(path) as conn:
            days = _recent_days(conn, upto=day, limit=window + 1)
            if len(days) < window + 1:
                return None
            latest, base = days[0], days[-1]
            rows = conn.execute(
                "SELECT d.symbol, d.close, p.close FROM stock_daily_hfq d "
                "JOIN stock_daily_hfq p ON p.symbol = d.symbol AND p.date = ? "
                "WHERE d.date = ? AND d.close > 0 AND p.close > 0",
                (base, latest),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(f"算全市场 {window} 日收益失败：{exc}")
        return None
    out: dict[str, float] = {}
    for symbol, close, prev in rows:
        out[str(symbol)] = float(close) / float(prev) - 1.0
    return out or None


def market_return(db_path: str | Path, *, day: str | None = None,
                  window: int = DEFAULT_WINDOW) -> float | None:
    """**全市场等权 N 日收益**（小数）—— 相对强弱的基准。

    **口径与 `research/scorecard.py` 的 α 基准一致**（α = 个股 − 同期全市场等权）：
    同一份后复权价、按全市场共同交易日锚定的同一个窗口、等权**算术平均**、
    停牌样本跳过。差别只有一处且是有意的：scorecard 的基准服务回测，会剔掉
    "买不进"的一字板样本（可成交性要求）；这道门槛是筛选，不剔 ——
    它问的是"这几天谁比大盘强"，不是"能不能买到"。

    Returns:
        等权收益（小数）；数据不足或有效样本少于 `MIN_BENCH_SYMBOLS` 时返回 None
        （**不产出"几十只票的平均"** —— 那不是市场）。
    """
    returns = window_returns(db_path, day=day, window=window)
    if not returns:
        return None
    if len(returns) < MIN_BENCH_SYMBOLS:
        return None
    return sum(returns.values()) / len(returns)


@dataclass
class GateOutcome:
    """`apply_gate` 的结果：留下的、被挡掉的，以及**给用户看的那句话**。"""

    #: 留下的候选（**就是传进来的那些 dict 本身**，没复制 ——
    #: 调用方可以按 `id()` 把它筛回各条公式的名单，见 `formula_group`）
    kept: list[dict] = field(default_factory=list)
    #: 被挡掉的候选（原 dict + `rs_pct` 相对强弱% + `why` 中文原因）
    dropped: list[dict] = field(default_factory=list)
    #: 这次判定出的大盘状态
    regime: Regime = field(default_factory=Regime)
    #: 这道门槛**真的生效过**吗（配置开 + 弱市 + 基准算得出来）
    applied: bool = False
    #: 生效时用的门槛（%）与窗口（交易日）
    min_pct: float = DEFAULT_RS_MIN_PCT
    window: int = DEFAULT_WINDOW
    #: 同期全市场等权收益（%，None = 算不出来）
    market_pct: float | None = None
    #: 历史不足、算不出相对强弱而**已保留**的票数（如实告诉用户，不静默砍）
    unknown: int = 0
    #: 进 `run.warnings` / `preview["notes"]` 的中文说明（**空串 = 什么都不用说**，
    #: 即强市/中性/未知、或这次一只票都没被挡掉 —— 那种情况下行为与改动前完全一样）
    note: str = ""

    def __iter__(self):
        """支持 `kept, dropped = apply_gate(...)` 这种写法（保持"输出两组票"的形状）。"""
        return iter((self.kept, self.dropped))

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def _gate_note(regime: Regime, *, dropped: int, unknown: int,
               min_pct: float, window: int) -> str:
    """"名单为什么变短了"的那句话（界面会**原样**显示，所以每项都要有数字）。"""
    facts = "；".join(regime.facts) if regime.facts else "没有可用的事实"
    text = (f"今日大盘{regime.level}势（{facts}）：已只保留近 {window} 日"
            f"跑赢全市场 {min_pct:.1f}% 的票，挡掉 {dropped} 只")
    if unknown:
        text += f"（另有 {unknown} 只因历史不足算不出相对强弱，已保留）"
    return text + "（配置 market_regime_gate = false 可关掉这道门槛）"


def apply_gate(
    picks: Iterable[dict],
    *,
    db_path: str | Path,
    day: str | None = None,
    cfg: Any = None,
    min_pct: Any = None,
    window: Any = None,
) -> GateOutcome:
    """**弱市抬门槛**：把候选按"近 N 日相对全市场的强弱"筛一遍（纯函数）。

    个股的相对强弱 = 该票近 N 日收益 − **同期全市场等权收益**（同一个窗口、
    同一份后复权价，见 `market_return` 的口径说明）；弱市时只留 `≥ min_pct%` 的票。

    这是【开始筛选】与【试算】**共用的唯一一份筛选实现**：两处分别写一遍，
    就会出现"试算说 5 只、匹配只有 2 只"这种用户无法解释的差异。

    Args:
        picks: 候选 `[{"symbol","name","reason"}...]`（缺 `symbol` 的条目原样保留：
            它到底该不该留不是这道门槛能判断的事）。
        db_path: 本地库（只读）。
        day: 判定用的行情日（两条路都传 `prepared.kline_day`）；None = 库里最新交易日。
        cfg: 配置（`market_regime_gate` / `market_regime_rs_min_pct` /
            `market_regime_window`）；**None = 没配置 ⇒ 门槛不生效** ——
            拿不到"用户想不想开"这件事时，宁可什么都不做。
        min_pct: 覆盖配置里的门槛（%，默认读配置、配置写错回退 2.0）。
        window: 覆盖配置里的窗口（交易日，小于 2 回默认 20、大于 250 夹到 250）。

    Returns:
        `GateOutcome`（可当 `(kept, dropped)` 解包）。**强市/中性/未知、关掉配置、
        或基准算不出来时，`kept` 就是全部输入、`dropped` 为空、`note` 为空串** ——
        行为与没有这道门槛时一模一样（这是回归用例钉住的口径）。
    """
    items = [pick for pick in picks if isinstance(pick, dict)]
    outcome = GateOutcome(kept=list(items), min_pct=_min_pct_value(min_pct),
                          window=_window_value(window))
    if not gate_enabled(cfg):
        # 没开（或没有配置）：不判定、不读库 —— 与改动前完全一样（一次库都不读）
        return outcome
    pct, span = _settings(cfg, min_pct, window)
    outcome.min_pct, outcome.window = pct, span
    regime = detect(db_path, day=day)
    outcome.regime = regime
    if not regime.weak:
        # 强市/中性/未知：**一条票都不许被挡掉**（铁律①）。
        # 未知时这里也不说话：名单没变短，没有需要解释的事。
        return outcome
    returns = window_returns(db_path, day=regime.day, window=span)
    if not returns or len(returns) < MIN_BENCH_SYMBOLS:
        # 弱市但算不出基准：**照旧不挡**（宁可少一道过滤，也不误伤用户的名单），
        # 但要如实说明 —— 否则用户会以为"这个功能坏了/没生效"。
        outcome.note = (f"今日大盘弱势（{'；'.join(regime.facts)}），但本地数据算不出"
                        f"全市场等权基准（有效样本不足 {MIN_BENCH_SYMBOLS} 只），"
                        "本次未启用「只留跑赢全市场」这道门槛")
        logger.warning(outcome.note)
        return outcome
    market = sum(returns.values()) / len(returns)
    outcome.market_pct = round(market * 100, 2)
    outcome.applied = True

    kept: list[dict] = []
    dropped: list[dict] = []
    for pick in items:
        symbol = str(pick.get("symbol") or "").strip()
        ret = returns.get(symbol)
        if not symbol or ret is None:
            # 算不出来（停牌/上市不满 N 天/代码不在库里）：**保留**并计数。
            # 判据是"证明它跑赢"，而不是"没证明就砍掉" —— 后者的受害者永远是
            # 新股与停牌股，而用户看不到任何原因。
            kept.append(pick)
            if symbol:
                outcome.unknown += 1
            continue
        strength = ret - market
        rs_pct = strength * 100
        if rs_pct >= pct - _EPS:
            kept.append(pick)
        else:
            blocked = dict(pick)
            blocked["rs_pct"] = round(rs_pct, 2)
            blocked["why"] = (f"近 {span} 日跑输全市场 {abs(rs_pct):.2f}%"
                              f"（门槛：跑赢 {pct:.1f}%）")
            dropped.append(blocked)
    outcome.kept, outcome.dropped = kept, dropped
    if dropped:
        # 只在**真挡掉了票**的时候说话：名单没变短时多说一句反而是噪音
        # （而且"名单没短、提示却说在筛选"会让用户怀疑这道门槛到底生效没有）。
        # 这里只负责把这句话**交出去**：两条路各自把它写进 `warnings` / `notes`
        # 并顺手记一行日志（见 `formula_group` 与 `formulas.preview_hits`）。
        outcome.note = _gate_note(regime, dropped=len(dropped), unknown=outcome.unknown,
                                 min_pct=pct, window=span)
    elif outcome.unknown:
        outcome.note = (f"今日大盘弱势（{'；'.join(regime.facts)}）：已按近 {span} 日"
                        f"跑赢全市场 {pct:.1f}% 筛选，本次没有票被挡掉"
                        f"（{outcome.unknown} 只因历史不足算不出相对强弱）")
    return outcome


__all__ = [
    "DEFAULT_RS_MIN_PCT",
    "DEFAULT_WINDOW",
    "GateOutcome",
    "INDEX_SYMBOL",
    "MA_WINDOW",
    "MAX_WINDOW",
    "MIN_BENCH_SYMBOLS",
    "MIN_VOTING_SIGNALS",
    "MIN_WINDOW",
    "Regime",
    "apply_gate",
    "detect",
    "gate_enabled",
    "market_return",
    "window_returns",
]
