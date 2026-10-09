"""「弱市抬门槛」（`laoa_trader.market_regime`）的离线测试。

为什么要这一层（2026-10-08 主人定的口径）
---------------------------------------
大盘明显走弱的那几天，公式选出来的名单质量会掉：命中的票多半是"跌得比别人少"
而不是"有人在买"。于是弱市时只留"跑赢全市场"的票，并**在界面上说清名单为什么变短**。

这个文件钉五件事（每一件都对应一类最难查的错）：

1. **三个信号各自的偏强/偏弱/缺席**：结论、投票与 `facts` 文案 —— 缺席必须如实写在
   事实里，而不是悄悄记 0 票；
2. **不知道绝不等于弱市**：空库、只有一个交易日、库不存在时 `level = 未知`、
   `usable = False`、`score = None`，并且**一只票都不许被挡掉**；
3. **弱市真的筛**：跑赢基准的留下、跑输的挡掉，留下的 + 挡掉的 = 输入（守恒），
   被挡掉的票要能说清差在哪；
4. **强市/中性/关掉配置时行为与改动前完全一样**（一条票都不挡、一个字都不多说）；
5. **两条路一致**：同一条公式、同一份数据，`preview_hits` 与 `run_enabled_formulas`
   在弱市与强市下选出同一批票 —— 两处各写一套筛选就是这个项目里最难查的一类 bug。

数据全部是自己造的合成小库（`storage.init_db` + `write_daily_raw` + `write_calendar`
+ `write_index_daily` + `write_limit_up_pool`），全程不联网
（`tests/conftest.py` 已在 socket 层封死网络）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from laoa_trader import formulas as lib
from laoa_trader import market_regime, price_limits
from laoa_trader.config import Config, load_config
from laoa_trader.data import public_sync, storage
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group

from tests._toml import q
from tests.conftest import workdays_ending

#: 合成库的最后一个交易日与交易日总数。
#: **必须 > 20**：相对强弱的默认窗口是 20 个交易日（锚点 + 20 根 = 21 个交易日），
#: 不足 21 个交易日的库算不出基准，门槛会退回"什么都不挡"（那条路径另有专门用例）。
END = "2026-09-11"
COUNT = 30
DAYS = workdays_ending(END, COUNT)

#: 库里的 6 只票（都是主板代码：涨跌停幅度 10%，手算校验时不用切板块规则）
SYMBOLS = ("600001", "600002", "600003", "600004", "600005", "600006")

#: 上证指数在 `index_daily` 里的写法
SH = market_regime.INDEX_SYMBOL

#: 公式：`C > 0` 对合成库里 6 只票**全部命中** —— 这样"选出来几只"完全由门槛决定，
#: 断言的就是门槛本身（而不是又叠了一层公式条件）。
BODY = "C>0"

#: 固定的"收盘后"时刻：两条路都必须走**日 K 口径**，否则盘中实时那条路会去取快照
#: （测试里取不到、退回日 K），比对的就不是同一份输入了。
AFTER_CLOSE = datetime(2026, 9, 11, 20, 0)


# ══════════════════════════════════════════════════════════════════════════
# 造库：合成小库（不联网、结果确定）
# ══════════════════════════════════════════════════════════════════════════


def _series(symbol: str, base: float, drift: float, *, count: int = COUNT,
            last: str | None = None) -> list[float]:
    """`count` 根收盘价：逐日按 `drift` 复利，`last` 覆盖最后一根。

    `last='up'/'down'` 时用**项目自己的涨跌停规则**（`price_limits`）算出那一根 ——
    这两个数才是真涨停/真跌停的收盘价，涨停池与跌停家数才认它
    （拿 9.9% 冒充 10%，"跌停 0 家"这种假事实就会混进断言里）。
    """
    closes: list[float] = []
    price = base
    for _ in range(count):
        price = round(price * (1 + drift), 2)
        closes.append(price)
    if last in ("up", "down"):
        prev = closes[-2]
        target = (price_limits.limit_up_price(prev, symbol) if last == "up"
                  else price_limits.limit_down_price(prev, symbol))
        assert target is not None
        closes[-1] = target
    return closes


def _plan(drifts: dict[str, tuple[float, str | None]]) -> dict[str, tuple[str, list[float]]]:
    """{代码: (趋势, 最后一根)} → `{代码: (中文名, 收盘价序列)}`。"""
    return {
        symbol: (f"样本{i}", _series(symbol, 10.0 + i, drift, last=last))
        for i, (symbol, (drift, last)) in enumerate(drifts.items(), start=1)
    }


def _plan_strong() -> dict[str, tuple[str, list[float]]]:
    """强市：6 只全涨，其中 2 只最后一根是真涨停（广度 +1、涨跌停 +1）。"""
    return _plan({s: (0.005, "up" if i <= 2 else None)
                  for i, s in enumerate(SYMBOLS, start=1)})


def _plan_weak() -> dict[str, tuple[str, list[float]]]:
    """弱市：2 涨 4 跌，其中 1 只真涨停、2 只真跌停（广度 -1、涨跌停 -1）。

    两个信号都偏弱 ⇒ 判定为「弱」，这道门槛才会真的开始筛。
    领涨的两只（`600001` +10% 涨停、`600002` 微涨）与其余四只的 20 日收益
    拉得很开（一个 +47%、一个 +25%，第三名是负的），所以"该留哪两只"**不是临界判断**。
    """
    return _plan({
        "600001": (0.010, "up"),
        "600002": (0.005, None),
        "600003": (-0.010, None),
        "600004": (-0.015, None),
        "600005": (-0.020, "down"),
        "600006": (-0.025, "down"),
    })


def _plan_neutral() -> dict[str, tuple[str, list[float]]]:
    """中性：3 涨 3 跌（广度 0）、1 涨停 1 跌停（涨跌停 0）⇒ 结论「中」，门槛不生效。"""
    return _plan({
        "600001": (0.005, "up"),
        "600002": (0.005, None),
        "600003": (0.005, None),
        "600004": (-0.005, None),
        "600005": (-0.005, "down"),
        "600006": (-0.005, None),
    })


def _plan_index_tips() -> dict[str, tuple[str, list[float]]]:
    """广度偏弱、涨跌停偏强（只看这两个信号是「中」）—— 让**指数那一票**决定结论。"""
    return _plan({
        "600001": (0.005, "up"),
        "600002": (0.005, "up"),
        "600003": (-0.005, None),
        "600004": (-0.005, None),
        "600005": (-0.010, None),
        "600006": (-0.015, None),
    })


def _plan_tiny_weak() -> dict[str, tuple[str, list[float]]]:
    """只有 3 只票的弱市库：广度与涨跌停都偏弱，但**有效样本凑不出"全市场"基准**。"""
    return _plan({
        "600001": (0.005, "up"),
        "600002": (-0.010, "down"),
        "600003": (-0.015, "down"),
    })


def _db(tmp_path: Path, plan: dict[str, tuple[str, list[float]]], *,
        index: list[float] | None = None, pool: bool = True,
        days: list[str] | None = None, name: str = "trader.db") -> str:
    """把一份合成库写进临时目录并返回路径。

    Args:
        plan: `{代码: (中文名, 收盘价序列)}`（见 `_plan`）。
        index: 上证收盘价序列；None = 库里**没有**指数日线（免 Key 用户的形态）。
        pool: 是否写涨停池。True 时**按真实的涨停价**推断该写哪几只
            （最后一根收盘价等于涨停价 ⇒ 那天它就在涨停池里）；
            False = 涨停池没更新（那一天"涨停池是空的"，信号必须缺席）。
        days: 交易日列表（默认 30 个；造"只有一个交易日"的库时传切片）。
        name: 库文件名（同一个用例里造多个库时区分开）。
    """
    path = storage.init_db(tmp_path / name)
    days = list(days or DAYS)
    with storage.connect(path) as conn:
        storage.write_stock_basic(conn, [(s, n, "银行") for s, (n, _c) in plan.items()])
        rows = []
        for symbol, (_name, closes) in plan.items():
            for day, close in zip(days, closes):
                rows.append((symbol, day, round(close * 0.99, 2), round(close * 1.01, 2),
                             round(close * 0.98, 2), close, 1_000_000.0,
                             round(close * 1_000_000.0, 2)))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        if index is not None:
            storage.write_index_daily(conn, [
                (SH, day, c, c, c, c, 1e9, 1e11) for day, c in zip(days, index)
            ])
        if pool:
            ups = [(s, n) for s, (n, closes) in plan.items()
                   if len(closes) > 1
                   and public_sync.is_limit_up(closes[-1], closes[-2], s, n)]
            if ups:
                storage.write_limit_up_pool(conn, [
                    (days[-1], s, n, 1, "首板", "09:35:00", "09:35:00", 8e7, 0, "银行",
                     5.0, 10.0, 1e9, 0, 1, "首板", 9e7, 10.0, 0, "hithink", "t")
                    for s, n in ups
                ])
    return str(path)


def _picks(*symbols: str) -> list[dict]:
    """候选（形状与 `formula_group` 交给门槛的一模一样）。"""
    return [{"symbol": s, "name": f"样本{s[-1]}", "reason": "策略：门槛测试"}
            for s in symbols]


def _cfg(**overrides) -> Config:
    """出厂配置（门槛默认开）；要改哪几项就传进来。"""
    cfg = Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture()
def gate_cfg(cfg: Config, tmp_path: Path) -> Config:
    """带 config.toml 落点的配置（`enabled_formulas` 写回时需要它存在）。"""
    cfg.source_path = tmp_path / "config.toml"
    return cfg


# ══════════════════════════════════════════════════════════════════════════
# 1) 广度和涨跌停：偏强、偏弱、缺席
# ══════════════════════════════════════════════════════════════════════════


def test_breadth_and_limit_vote_strong_when_most_stocks_rise(tmp_path: Path) -> None:
    """广度偏强 + 涨跌停偏强 ⇒ 强市，事实里带具体家数。"""
    regime = market_regime.detect(_db(tmp_path, _plan_strong()))

    assert regime.votes == {"广度": 1, "涨跌停": 1}
    assert regime.facts[0] == f"上涨 6 / 下跌 0（{DAYS[-1]} 收盘）"
    assert "涨停 2 / 跌停 0" in regime.facts[1]
    assert regime.level == "强" and regime.usable is True and regime.score == 100
    assert regime.day == DAYS[-1]                 # 判定用的是库里最新行情日


def test_breadth_and_limit_vote_weak_when_most_stocks_fall(tmp_path: Path) -> None:
    """广度偏弱 + 涨跌停偏弱 ⇒ **弱市**（这道门槛唯一会生效的情况）。

    第 3 条事实是**指数缺席**的如实说明：库里没有 `index_daily`（免 Key 用户的形态），
    所以那一项不投票 —— 但它照样出现在事实清单里，用户才能看出"结论只由两个信号得出"。
    """
    regime = market_regime.detect(_db(tmp_path, _plan_weak()))

    assert regime.votes == {"广度": -1, "涨跌停": -1}
    assert regime.facts[0] == f"上涨 2 / 下跌 4（{DAYS[-1]} 收盘）"
    assert "涨停 1 / 跌停 2（跌停按有行业分类的票统计）" == regime.facts[1]
    assert "没有上证指数（sh.000001）" in regime.facts[2] and "不投票" in regime.facts[2]
    assert regime.level == "弱" and regime.usable is True and regime.score == 0
    assert regime.weak is True


def test_breadth_is_absent_when_nothing_moved(tmp_path: Path) -> None:
    """全部平盘 ⇒ 广度和涨跌停都**缺席**（不是"市场很中性"）。

    这条挡的是最难发现的一种读错：把"这库里压根没有真行情"读成"市场平静"，
    再拿去凑够两个信号判出一个结论。
    """
    plan = {s: (f"样本{i}", _series(s, 10.0 + i, 0.0))
            for i, s in enumerate(SYMBOLS, start=1)}
    regime = market_regime.detect(_db(tmp_path, plan, name="flat.db"))

    assert "广度" not in regime.votes and "涨跌停" not in regime.votes
    assert "既没有上涨也没有下跌" in regime.facts[0]
    assert regime.level == "未知" and regime.usable is False


def test_limit_signal_is_absent_when_the_pool_was_not_updated(tmp_path: Path) -> None:
    """涨停池空着 ≠ 市场平静：分不清"一只都没涨停"与"池子没更新"，只能缺席。

    顺带钉住铁律②：这里广度**明确投了 0 票**（3 涨 3 跌），可判信号只有 1 个 ——
    结论必须是「未知」。缺席的信号绝不能被当成 0 票来凑够两个信号。
    """
    regime = market_regime.detect(_db(tmp_path, _plan_neutral(), pool=False))

    assert regime.votes == {"广度": 0}
    assert "涨跌停" not in regime.votes
    assert "没有 2026-09-11 的涨停池数据" in regime.facts[1]
    assert "不投票" in regime.facts[1]
    assert regime.level == "未知" and regime.usable is False and regime.score is None


def test_detect_for_an_older_day_skips_the_limit_signal(tmp_path: Path) -> None:
    """判定一个**早于库里最新行情日**的日子：涨跌停那一票必须缺席。

    为什么：跌停家数复用 `pool.limit_down_industries()`，而它只按库里**最近两个**
    交易日算（它收到 `day` 但没用）—— 拿"涨停按旧日、跌停按最新两天"拼一个结论，
    等于把两天的盘面混在一起。这里连旧的涨停池数据都造出来了，专门盯这条边界。
    """
    db = _db(tmp_path, _plan_weak())
    with storage.connect(db) as conn:
        conn.execute("INSERT INTO limit_up_pool (date, symbol, name) VALUES (?, ?, ?)",
                     (DAYS[-2], "600001", "样本1"))
        conn.commit()

    regime = market_regime.detect(db, day=DAYS[-2])

    assert regime.day == DAYS[-2]
    assert "涨跌停" not in regime.votes
    assert "对不上" in regime.facts[1]
    assert regime.level == "未知"           # 只剩广度一票 ⇒ 不下结论


# ══════════════════════════════════════════════════════════════════════════
# 2) 指数：偏强、偏弱、缺席
# ══════════════════════════════════════════════════════════════════════════


def test_index_vote_decides_the_level(tmp_path: Path) -> None:
    """同一份行情，指数站上/跌破 MA20 ⇒ 强 / 弱：指数那一票真的能改结论。"""
    plan = _plan_index_tips()
    up = market_regime.detect(
        _db(tmp_path, plan, index=_series(SH, 3000.0, 0.002), name="up.db"))
    down = market_regime.detect(
        _db(tmp_path, plan, index=_series(SH, 3000.0, -0.002), name="down.db"))

    # 广度 -1、涨跌停 +1（只看这两票是"中"），指数那一票决定倒向哪边
    assert up.votes == {"广度": -1, "涨跌停": 1, "指数": 1}
    assert up.level == "强" and up.score == 67
    assert "高于 MA20" in up.facts[2] and up.facts[2].startswith("上证 ")
    assert down.votes == {"广度": -1, "涨跌停": 1, "指数": -1}
    assert down.level == "弱" and down.score == 33
    assert "低于 MA20" in down.facts[2]


def test_index_signal_is_absent_without_index_daily(tmp_path: Path) -> None:
    """免 Key 用户可能一条指数日线都没有：**缺席不投票**（既不是中性、更不是弱市）。"""
    regime = market_regime.detect(_db(tmp_path, _plan_index_tips()))

    assert "指数" not in regime.votes
    assert "没有上证指数（sh.000001）" in regime.facts[2]
    assert "不投票" in regime.facts[2]
    assert regime.level == "中" and regime.score == 50     # 广度 -1 与涨跌停 +1 各一半


def test_index_signal_is_absent_when_history_is_too_short(tmp_path: Path) -> None:
    """指数日线不足 20 根 ⇒ 同样缺席：MA20 算不出来就不是一个信号。"""
    db = _db(tmp_path, _plan_strong(), index=_series(SH, 3000.0, 0.002)[:5], name="i5.db")

    regime = market_regime.detect(db)

    assert "指数" not in regime.votes
    assert "上证指数只有 5 个交易日" in regime.facts[2]
    assert "不投票" in regime.facts[2]


# ══════════════════════════════════════════════════════════════════════════
# 3) 数据不足：未知 + 一只都不许挡（铁律①）
# ══════════════════════════════════════════════════════════════════════════


def test_empty_db_is_unknown_and_the_gate_blocks_nothing(tmp_path: Path) -> None:
    """空库：`未知` / `usable=False` / **没有 score**（不许用 50 冒充"不知道"）。"""
    db = str(storage.init_db(tmp_path / "empty.db"))

    regime = market_regime.detect(db)

    assert regime.level == "未知" and regime.usable is False and regime.score is None
    assert regime.votes == {}
    assert "没有任何日线行情" in regime.facts[0]

    picks = _picks(*SYMBOLS)
    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())
    assert outcome.kept == picks and outcome.dropped == []
    assert outcome.applied is False and outcome.note == ""


def test_single_trading_day_is_unknown_and_the_gate_blocks_nothing(tmp_path: Path) -> None:
    """只有一个交易日：三个信号全都算不出来 ⇒ 未知，且**一只都不挡**。"""
    plan = {s: (f"样本{i}", _series(s, 10.0 + i, 0.005, count=1))
            for i, s in enumerate(SYMBOLS, start=1)}
    db = _db(tmp_path, plan, days=DAYS[-1:], name="one.db")

    regime = market_regime.detect(db)

    assert regime.level == "未知" and regime.usable is False and regime.score is None
    assert regime.votes == {}
    assert "不足两个交易日" in regime.facts[0]

    outcome = market_regime.apply_gate(_picks(*SYMBOLS), db_path=db, day=None, cfg=_cfg())
    assert outcome.dropped == [] and outcome.applied is False and outcome.note == ""


def test_missing_db_is_unknown_and_creates_no_file(tmp_path: Path) -> None:
    """库不存在：如实说"未知"，而且**不许顺手建一个空库**。

    `storage.connect` 对不存在的文件是"连上就建"，凭空多出来的空 trader.db
    会被日更/界面认成"已经有数据"（`formulas._live_decision` 里记的是同一个坑）。
    """
    path = tmp_path / "nope.db"

    regime = market_regime.detect(path)

    assert regime.level == "未知" and regime.usable is False
    assert "本地还没有行情库" in regime.facts[0]
    assert not path.exists()
    assert market_regime.market_return(path) is None


# ══════════════════════════════════════════════════════════════════════════
# 4) 基准：全市场等权（与 scorecard 的 α 基准同口径）
# ══════════════════════════════════════════════════════════════════════════


def test_market_return_is_the_equal_weight_mean_of_the_window(tmp_path: Path) -> None:
    """基准 = 全市场每只票 N 日收益的**算术平均**（等权），用测试自己的 SQL 重算一遍。

    为什么要自己重算：拿被测实现的私有部分去校验被测实现，两边错成同一个样子也看不出来。
    """
    db = _db(tmp_path, _plan_weak())

    with sqlite3.connect(db) as conn:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_daily_hfq ORDER BY date DESC LIMIT 21")]
        base, latest = days[-1], days[0]
        rows = conn.execute(
            "SELECT d.symbol, d.close, p.close FROM stock_daily_hfq d "
            "JOIN stock_daily_hfq p ON p.symbol = d.symbol AND p.date = ? "
            "WHERE d.date = ?", (base, latest)).fetchall()
    expected = sum(close / prev - 1 for _symbol, close, prev in rows) / len(rows)

    assert market_regime.market_return(db, window=20) == pytest.approx(expected)
    assert set(market_regime.window_returns(db, window=20)) == set(SYMBOLS)
    # 那两天就是"库里最后 21 个交易日"的首尾（窗口 = 20 个交易日）
    assert (base, latest) == (DAYS[-21], DAYS[-1])


def test_suspended_stock_is_out_of_the_benchmark(tmp_path: Path) -> None:
    """锚点日停牌（那根 K 线不在库里）的票**不进基准** —— 按"有效样本"算。

    与 `research/scorecard.py` 的 `market_map` 是同一口径：基准按全市场**共同交易日**
    对齐，缺样本的行直接跳过（否则停牌股会把基准算成一个不存在的收益）。
    """
    db = _db(tmp_path, _plan_weak())
    anchor = DAYS[-21]
    with storage.connect(db) as conn:
        conn.execute("DELETE FROM stock_daily_raw WHERE symbol = ? AND date = ?",
                     ("600006", anchor))
        conn.commit()

    returns = market_regime.window_returns(db, window=20)

    assert returns is not None and "600006" not in returns and len(returns) == 5
    assert market_regime.market_return(db, window=20) == pytest.approx(
        sum(returns.values()) / 5)


def test_benchmark_needs_enough_stocks(tmp_path: Path) -> None:
    """有效样本少于 `MIN_BENCH_SYMBOLS`（5）只时**不产出基准**：几十只票的平均不是市场。"""
    plan = dict(list(_plan_weak().items())[:4])
    db = _db(tmp_path, plan, name="four.db")

    assert market_regime.window_returns(db, window=20) is not None   # 收益算得出来
    assert market_regime.market_return(db, window=20) is None        # 但不把它当"市场"


def test_benchmark_is_none_when_history_is_too_short(tmp_path: Path) -> None:
    """库里的交易日不足 `窗口 + 1` ⇒ 基准算不出来（**None，不是 0**）。

    为什么必须是 None 而不是 0：0 的意思是"全市场这段时间一动没动"，
    拿它当基准会让"相对强弱"退化成绝对涨幅 —— 那是另一个口径。
    """
    plan = {s: (n, closes[-10:]) for s, (n, closes) in _plan_weak().items()}
    db = _db(tmp_path, plan, days=DAYS[-10:], name="short.db")

    assert market_regime.market_return(db, window=20) is None
    # 窗口调小到能算得出来时，同一个库立刻就有基准（证明上面的 None 是"长度不够"）
    assert market_regime.market_return(db, window=9) is not None


# ══════════════════════════════════════════════════════════════════════════
# 5) 门槛本身：弱市筛、强市/中性不动
# ══════════════════════════════════════════════════════════════════════════


def test_weak_market_keeps_only_the_outperformers(tmp_path: Path) -> None:
    """弱市：跑赢全市场的留下、跑输的挡掉，两组票数与输入对得上（守恒）。"""
    db = _db(tmp_path, _plan_weak())
    picks = _picks(*SYMBOLS)

    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())

    assert outcome.applied is True
    assert outcome.regime.level == "弱"
    assert [p["symbol"] for p in outcome.kept] == ["600001", "600002"]
    assert sorted(p["symbol"] for p in outcome.dropped) == [
        "600003", "600004", "600005", "600006"]
    # 守恒：留下的 + 挡掉的 = 输入（一只都不许凭空消失或重复出现）
    assert len(outcome.kept) + len(outcome.dropped) == len(picks)
    assert sorted(p["symbol"] for p in outcome.kept + outcome.dropped) == sorted(
        p["symbol"] for p in picks)
    # 留下来的就是传进去的那些对象本身（建池那条路按它继续往下走，不复制）
    assert all(any(pick is original for original in picks) for pick in outcome.kept)
    # 挡掉的票要说清差在哪，且**原有字段不许被改动**（池子/推送还要用它）
    for pick in outcome.dropped:
        assert pick["rs_pct"] < 2.0
        assert "跑输全市场" in pick["why"] and "跑赢 2.0%" in pick["why"]
        assert pick["reason"] == "策略：门槛测试"
    # 留下来的票的相对强弱确实过线（与基准同口径：个股 − 同期全市场等权）
    returns = market_regime.window_returns(db, window=20)
    market = sum(returns.values()) / len(returns)
    for pick in outcome.kept:
        assert (returns[pick["symbol"]] - market) * 100 >= 2.0
    # 基准与个股强弱**共用同一份数据**（否则 α 里的两个数会来自不同口径）
    assert outcome.market_pct == pytest.approx(round(market * 100, 2))


def test_gate_note_explains_why_the_list_is_shorter(tmp_path: Path) -> None:
    """提示里必须写清：哪一天、什么状态、为什么、挡了几只、怎么关。"""
    db = _db(tmp_path, _plan_weak())

    outcome = market_regime.apply_gate(_picks(*SYMBOLS), db_path=db, day=None, cfg=_cfg())

    assert "今日大盘弱势" in outcome.note
    assert "上涨 2 / 下跌 4" in outcome.note          # 结论的根据（用户能核对）
    assert "涨停 1 / 跌停 2" in outcome.note
    assert "近 20 日跑赢全市场 2.0% 的票" in outcome.note
    assert "挡掉 4 只" in outcome.note
    assert "market_regime_gate = false" in outcome.note   # 怎么关掉这道门槛


def test_gate_note_shows_the_index_fact_when_available(tmp_path: Path) -> None:
    """有指数日线时，那句话里也要带上指数那一项（三个信号全偏弱 ⇒ 强度分 0）。"""
    db = _db(tmp_path, _plan_weak(), index=_series(SH, 3000.0, -0.002), name="wi.db")

    outcome = market_regime.apply_gate(_picks(*SYMBOLS), db_path=db, day=None, cfg=_cfg())

    assert outcome.regime.votes["指数"] == -1
    assert outcome.regime.score == 0
    assert "低于 MA20" in outcome.note


def test_gate_keeps_picks_it_cannot_judge(tmp_path: Path) -> None:
    """算不出相对强弱的票**保留**并计数：判据是"证明它跑赢"，不是"没证明就砍掉"。

    受害者永远是次新股与长期停牌股，而用户看不到任何原因 —— 所以宁可多留一只，
    也要把那句话写进提示里。
    """
    db = _db(tmp_path, _plan_weak())
    picks = _picks(*SYMBOLS) + [{"symbol": "688888", "name": "库外样本",
                                 "reason": "策略：门槛测试"}]

    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())

    assert [p["symbol"] for p in outcome.kept] == ["600001", "600002", "688888"]
    assert outcome.unknown == 1
    assert "历史不足" in outcome.note and "已保留" in outcome.note
    assert len(outcome.kept) + len(outcome.dropped) == len(picks)


def test_gate_threshold_moves_with_min_pct(tmp_path: Path) -> None:
    """门槛真的在起作用：0 只挡跑输的，50 连领涨的也挡掉（`min_pct` 是百分数）。"""
    db = _db(tmp_path, _plan_weak())
    picks = _picks(*SYMBOLS)

    zero = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg(), min_pct=0.0)
    assert [p["symbol"] for p in zero.kept] == ["600001", "600002"]

    strict = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg(), min_pct=50.0)
    assert strict.kept == []
    assert len(strict.dropped) == len(SYMBOLS)
    assert "挡掉 6 只" in strict.note and "跑赢全市场 50.0%" in strict.note


def test_gate_stays_silent_when_nothing_is_dropped(tmp_path: Path) -> None:
    """门槛生效但一只都没挡掉时**一个字都不多说**：名单没变短，就没有要解释的事。"""
    db = _db(tmp_path, _plan_weak())

    outcome = market_regime.apply_gate(_picks("600001", "600002"), db_path=db,
                                       day=None, cfg=_cfg())

    assert outcome.applied is True and outcome.dropped == []
    assert outcome.note == ""


@pytest.mark.parametrize("name,level", [("strong", "强"), ("neutral", "中")])
def test_strong_and_neutral_markets_change_nothing(
        tmp_path: Path, name: str, level: str) -> None:
    """强市/中性：**原样返回全部**、什么都不挡、提示里一个字都不多（回归）。"""
    plan = _plan_strong() if name == "strong" else _plan_neutral()
    db = _db(tmp_path, plan, name=f"{name}.db")
    picks = _picks(*SYMBOLS)

    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())

    assert outcome.regime.level == level
    assert outcome.kept == picks and outcome.dropped == []
    assert outcome.applied is False and outcome.note == ""


def test_gate_can_be_switched_off_and_needs_a_config(tmp_path: Path) -> None:
    """关掉配置（或压根没有配置）：一次判定都不做，行为与改动前完全一样。"""
    db = _db(tmp_path, _plan_weak())
    picks = _picks(*SYMBOLS)

    off = market_regime.apply_gate(picks, db_path=db, day=None,
                                   cfg=_cfg(market_regime_gate=False))
    assert off.kept == picks and off.dropped == [] and off.note == ""
    assert off.applied is False

    no_cfg = market_regime.apply_gate(picks, db_path=db, day=None, cfg=None)
    assert no_cfg.kept == picks and no_cfg.dropped == [] and no_cfg.note == ""

    # 配置文件里写成 false 时同样是关（这条走的是真正的 TOML 解析路径）
    path = tmp_path / "config.toml"
    path.write_text(f"data_dir = {q(tmp_path / 'data')}\n"
                    "market_regime_gate = false\n", encoding="utf-8")
    assert load_config(path, use_env=False).market_regime_gate is False


def test_weak_market_with_tiny_market_blocks_nothing(tmp_path: Path) -> None:
    """弱市但**有效样本凑不出基准**（只有 3 只票）：不挡任何票，并且如实说明。

    为什么必须说：用户看到的是"提示说弱市、名单却没变短"，不解释就等于这个功能坏了。
    """
    db = _db(tmp_path, _plan_tiny_weak(), name="tiny.db")

    regime = market_regime.detect(db)
    assert regime.level == "弱" and regime.usable is True     # 两个信号都偏弱

    picks = _picks("600001", "600002", "600003")
    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())

    assert outcome.applied is False and outcome.kept == picks and outcome.dropped == []
    assert "未启用" in outcome.note and "有效样本不足" in outcome.note


def test_weak_market_without_benchmark_history_blocks_nothing(tmp_path: Path) -> None:
    """弱市但库里不足 21 个交易日（算不出 20 日基准）：不挡任何票，并且如实说明。"""
    plan = {s: (n, closes[-10:]) for s, (n, closes) in _plan_weak().items()}
    db = _db(tmp_path, plan, days=DAYS[-10:], name="short.db")

    assert market_regime.detect(db).level == "弱"

    picks = _picks(*SYMBOLS)
    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=_cfg())

    assert outcome.applied is False and outcome.kept == picks and outcome.dropped == []
    assert "未启用" in outcome.note


def test_bad_config_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    """配置写错（负数/字符串/窗口 0/1/9999）：不崩，回退到出厂口径，且门槛照旧生效。"""
    path = tmp_path / "config.toml"
    path.write_text(f"data_dir = {q(tmp_path / 'data')}\n"
                    "market_regime_gate = true\n"
                    "market_regime_rs_min_pct = -3.0\n"
                    "market_regime_window = 0\n", encoding="utf-8")
    cfg = load_config(path, use_env=False)

    # 配置层照实读进来（它不该替业务判对错），回退发生在门槛这一层
    assert cfg.market_regime_rs_min_pct == -3.0 and cfg.market_regime_window == 0

    db = _db(tmp_path, _plan_weak())
    picks = _picks(*SYMBOLS)
    outcome = market_regime.apply_gate(picks, db_path=db, day=None, cfg=cfg)

    assert (outcome.min_pct, outcome.window) == (2.0, 20)      # 回退到出厂口径
    assert [p["symbol"] for p in outcome.kept] == ["600001", "600002"]

    # 手滑写成字符串：同样回退，且绝不许抛异常
    cfg.market_regime_rs_min_pct = "二"      # type: ignore[assignment]
    cfg.market_regime_window = "月"           # type: ignore[assignment]
    again = market_regime.apply_gate(picks, db_path=db, day=None, cfg=cfg)
    assert (again.min_pct, again.window) == (2.0, 20)
    assert [p["symbol"] for p in again.kept] == ["600001", "600002"]

    # 边界：0 是**合法**门槛（只留不跑输的）；窗口 1 不是一个窗口（回默认 20）；
    # 9999 是"尽可能长"（夹到 250）；5 原样用
    zero = market_regime.apply_gate(picks, db_path=db, day=None, cfg=Config(), min_pct=0)
    assert zero.min_pct == 0.0
    assert market_regime.apply_gate(picks, db_path=db, day=None, cfg=Config(),
                                    window=1).window == 20
    assert market_regime.apply_gate(picks, db_path=db, day=None, cfg=Config(),
                                    window=9999).window == 250
    assert market_regime.apply_gate(picks, db_path=db, day=None, cfg=Config(),
                                    window=5).window == 5


# ══════════════════════════════════════════════════════════════════════════
# 6) 两条路一致：试算（【运行】）与匹配（【开始筛选】）
# ══════════════════════════════════════════════════════════════════════════


def _formula_dir(tmp_path: Path, name: str = "门槛测试") -> Path:
    """把公式写进临时目录（走库自己的写文件口径，不手拼文本）。"""
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.txt").write_text(
        lib.formula_text(name, BODY, "弱市抬门槛用例"), encoding="utf-8")
    return folder


def test_preview_and_matching_agree_in_a_weak_market(
        tmp_path: Path, gate_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """**弱市**：同一条公式、同一份数据，【运行】与【开始筛选】选出同一批票。

    顺带钉住两件容易写错的事：
    * 名单为什么变短，两条路上是**同一句话**（都从 `apply_gate` 拿）；
    * 那句话进的是 `warnings` / `notes`，**不是 `errors`** —— 建池的"成功/失败"
      判据是"errors 是否为空"（`scheduler.Scheduler._report_succeeded`），
      塞进去会让一次正常完成的筛选被判成失败。
    """
    db = _db(tmp_path, _plan_weak())
    folder = _formula_dir(tmp_path)
    gate_cfg.enabled_formulas = ["门槛测试"]
    # 固定成"收盘后"：两条路都走日 K 口径（否则盘中那条路会去取快照、退回日 K）
    monkeypatch.setattr(lib, "caliber_now", lambda: AFTER_CLOSE)

    preview = lib.preview_hits(fm.compile_formula(BODY), db, cfg=gate_cfg, now=AFTER_CLOSE)
    run = formula_group.run_enabled_formulas(db, gate_cfg, directory=folder)

    preview_symbols = [hit["symbol"] for hit in preview["hits"]]
    picked = run.picks[formula_group.formula_strategy_name("门槛测试")]
    assert preview_symbols == [p["symbol"] for p in picked] == ["600001", "600002"]
    assert preview["count"] == len(picked) == 2

    gate_notes = [note for note in preview["notes"] if "大盘弱势" in note]
    assert len(gate_notes) == 1 and "挡掉 4 只" in gate_notes[0]
    assert gate_notes[0] in run.warnings          # 同一句话（同一个函数给的）
    assert run.errors == []                       # 告知绝不许混进 errors


def test_preview_and_matching_agree_in_a_strong_market(
        tmp_path: Path, gate_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """**强市**：两条路都出全部 6 只，且都不多说什么（行为与改动前一样）。"""
    db = _db(tmp_path, _plan_strong())
    folder = _formula_dir(tmp_path)
    gate_cfg.enabled_formulas = ["门槛测试"]
    monkeypatch.setattr(lib, "caliber_now", lambda: AFTER_CLOSE)

    preview = lib.preview_hits(fm.compile_formula(BODY), db, cfg=gate_cfg, now=AFTER_CLOSE)
    run = formula_group.run_enabled_formulas(db, gate_cfg, directory=folder)

    preview_symbols = [hit["symbol"] for hit in preview["hits"]]
    picked = run.picks[formula_group.formula_strategy_name("门槛测试")]
    assert preview_symbols == [p["symbol"] for p in picked] == list(SYMBOLS)
    assert not any("大盘" in note for note in preview["notes"]), preview["notes"]
    assert not any("大盘" in warning for warning in run.warnings), run.warnings
    assert run.errors == []


def test_matching_puts_the_gate_note_in_warnings_not_errors(
        tmp_path: Path, gate_cfg: Config) -> None:
    """匹配那条路的提示只进 `warnings`（`errors` 非空会被上层判成"这一轮失败"）。"""
    db = _db(tmp_path, _plan_weak())
    folder = _formula_dir(tmp_path)
    gate_cfg.enabled_formulas = ["门槛测试"]

    run = formula_group.run_enabled_formulas(db, gate_cfg, directory=folder)

    assert any("今日大盘弱势" in w for w in run.warnings), run.warnings
    assert run.errors == []
