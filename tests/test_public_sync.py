"""免 Key 日更（`data/public_sync.py`）+ 日更入口分派（`sync.daily_update`）：**全离线**。

为什么这一批用例必须存在
-----------------------
这条链路是**没配同花顺 Key 时的日更兜底**（2026-09-18 用户把主源换回同花顺 ——
公开接口实测会限流：腾讯 fqkline 抓 700 只左右开始连续失败、新浪列表接口回 HTTP 456；
配了 Key 的机器走 `sync.py` 那条 dump 路，见下面 `daily_update()` 的分派用例）：
一趟腾讯全市场快照 → 当日日线 + 涨停池 + 代码表 + 交易日历。
它有三个"错了也不会报错、只会悄悄错下去"的死角，所以每一条都照着**实测结论**与
**真实夹具**钉：

1. **涨跌幅规则**（`limit_pct` / `limit_up_price` / `limit_down_price`）：取整方式与板块
   分段是**在全市场 5564 只上反推验证过的**（见 `public_sync` 的模块 docstring 表格）。
   差一分钱就会让"是不是涨停"判假，涨停池少几只，而且没有任何报错信号。
2. **复权因子**（`detect_ex_right` / `factor_step`）：公开源不给复权事件表，因子只能靠
   "快照昨收 vs 库里前收"推出来。方向搞反或容差放宽，污染的是**整条后复权序列** ——
   均线、涨幅、全部策略的输入，而库里看起来一切正常。
3. **"绝不写半份数据"**：快照为空/抛异常时，一个字节都不许落库；停牌票不许写成假 K 线；
   公开源拿不到的字段必须是 NULL 而不是 0（`连板()>=2` 会把 0 当成真实值）。

⚠️ 口径（2026-09-18 用户核实后要求写准，别让测试写出假承诺）：这条路让"没 Key 也能看到
当天行情"，但**选股仍然跑不起来，而且是设计如此** —— 数据闸门要求 `adjust_event` 非空与
行业覆盖 ≥90%，这两样只有同花顺那条路（用户自己的 Key）给得到。所以本文件里**不许**出现
"免 Key 攒够历史就能选股"这类说法；这条界线由
`test_public_source_data_can_never_pass_the_stock_picking_gate` 单独钉住。

夹具全是真的：`tests/fixtures/public_market/scan_rows.json` 是 2026-09-17 那一趟真实全市场
扫描（5564 只）里挑出来的 19 行，每一行都为某个坑存在（ST 涨停、北交所 920 段、停牌、
新股无涨停价哨兵、炸板、跌停）；`tencent_indices.txt` 是真实 HTTP 响应原文。
`tests/conftest.py` 的 `_block_network` 在 socket 层封死了外呼，所以这些用例不会碰真实接口
（凡是会取数的路径都用 monkeypatch 换成了夹具，包括日更总入口里的全市场扫描）。
"""

from __future__ import annotations

import json
import math
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any

import pytest

from laoa_trader.data import hithink as hx
from laoa_trader.data import public_market as pm
from laoa_trader.data import public_sync as ps
from laoa_trader.data import storage
from laoa_trader.data import sync
from tests.conftest import FakeClient

FIXTURES = Path(__file__).parent / "fixtures" / "public_market"

#: 快照写哪一天由用例自己给（`bars_from_snapshot(rows, conn, day)`），而"库里那根历史"
#: 与"今天"的**相对距离**正是被测语义：昨天 = 没缺天，20 天前 = 真的缺天。
#: 所以这些日期一律按 `today()` 算，不写字面量 —— 写死 2026-09-17 这类值，随着日历往后走
#: 它会从"昨天"变成"三天前"，用例的含义随之改变（红起来的原因看起来像被测代码坏了）。
TODAY = _date.today().isoformat()
YESTERDAY = (_date.today() - timedelta(days=1)).isoformat()
#: 20 天前：明显不是"昨天"，用它构造"真的缺天"。
STALE = (_date.today() - timedelta(days=20)).isoformat()


# ── 夹具与公共小工具 ────────────────────────────────────────────────────


def _rows() -> list[dict]:
    """真实扫描里挑出的 19 行（每个坑一行）。"""
    return json.loads((FIXTURES / "scan_rows.json").read_text(encoding="utf-8"))

def _days_ago(n: int) -> str:
    """`n` 个自然日之前的日期（造"上一个交易日"用；不引 pytest 的近似值）。"""
    from datetime import date, timedelta

    return (date.today() - timedelta(days=n)).isoformat()


def _by_symbol(symbol: str) -> dict:
    for row in _rows():
        if row["symbol"] == symbol:
            return row
    raise AssertionError(f"夹具里没有 {symbol}（夹具被改过？）")


def _writable(rows: list[dict]) -> list[dict]:
    """快照里**真的会变成一根 K 线**的那些行（与 `bars_from_snapshot` 的过滤条件一致）。

    过滤条件不许手写"19 减 2 等于 17"：过滤规则（有代码、成交量非 0、有现价）
    与产品实现是同一件事，写在两处早晚不一致 —— 这里从夹具算，夹具一改断言自动跟着走。
    """
    return [r for r in rows
            if r.get("symbol") and r.get("volume") and r.get("last_price") is not None]


def _suspended(rows: list[dict]) -> list[dict]:
    """停牌（成交量为 0）的那些行。"""
    return [r for r in rows if r.get("symbol") and not r.get("volume")]


def _at_limit_rows(rows: list[dict]) -> list[dict]:
    """压在涨停价上、且**不在**排除名单里的票 —— 涨停池的全部输入（口径见模块 docstring）。"""
    return [r for r in rows
            if pm._at_limit(r.get("last_price"), r.get("limit_up"))
            and not pm._excluded_from_limits(r)]


def _floor(value: float) -> float:
    """截断到分（与 `limit_up_price` 里北交所那一路同一种取整）。"""
    return math.floor(value * 100 + 1e-9) / 100


def _seed_bar(conn, symbol: str, day: str, close: float, *, factor: float = 1.0) -> None:
    """在库里造一根历史日线（只关心收盘价与因子：那正是除权检测的两个入参）。"""
    storage.write_daily_raw(
        conn,
        [(symbol, day, close, close, close, close, 1_000_000.0, close * 1_000_000.0, factor)],
    )


def _seed_calendar(conn, *days: str) -> None:
    """把交易日历写上 —— 缺口判据只认日历里的"上一个交易日"。

    为什么必须显式写：`previous_trading_day()` 查的是 `trading_calendar` 表，
    而且**日历里查不到就什么都不报**（宁可判不出来，也不要把提示变成每天一次的噪音）。
    所以凡是要断言 `gap` 的用例，都得先给日历一个"上一个交易日"。
    """
    assert days
    storage.write_calendar(conn, days, source="public")


def _pool_row(day: str, symbol: str, *, high_days: int | None, name: str = "样本") -> tuple:
    """造一条 21 列的涨停池行（列序必须与 `storage.LIMIT_UP_COLUMNS` 一致）。

    公开源拿不到的那 11 列一律 None —— 与 `public_sync.limit_pool_from_snapshot` 同形状，
    免得用一条"比产品实现更完整"的假行去测连板数（那样测的就不是真实情形了）。
    """
    row: list[Any] = [None] * len(storage.LIMIT_UP_COLUMNS)
    row[0], row[1], row[2], row[3] = day, symbol, name, high_days
    row[19], row[20] = "hithink", "测试造的行"
    return tuple(row)


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
                    *, seen: list[bool] | None = None) -> None:
    """把全市场扫描换成夹具数据。

    用例**绝不能**让它去真扫：一趟是 56 个请求 60 秒，而且在测试里被 `_block_network`
    封死 —— 那样测出来的会是"网络失败"这条与本次改版无关的路径。
    """
    def _scan(*, force: bool = False, **kwargs):
        if seen is not None:
            seen.append(bool(force))
        return list(rows)

    monkeypatch.setattr(pm, "cached_scan", _scan)


def _patch_market_date(monkeypatch: pytest.MonkeyPatch, day: str = TODAY) -> None:
    """把"最近交易日"固定下来：不固定的话日更会去读上证指数的时间戳（又一次外呼）。"""
    monkeypatch.setattr(ps, "market_date", lambda: day)


# 说明：**历史包方案已被用户否掉（2026-09-18）** —— 这里原先有一个
# `_disable_history_pack()`：当时的方案是"首次运行先装随程序带来的历史数据包"，那一步会在
# 取快照**之前**往库里写历史，所以每条日更用例都要把它显式关掉。用户随后否掉了这个方案
# （"那还是不要换源吧…同花顺不会轻易限流"）：同花顺仍是主源、完整历史走用户自己的 Key，
# `data/history_pack.py` 已删除 —— 于是那个开关（现在只是死代码）和它的 9 处调用一起删掉，
# 日更里不再有任何"先写历史"的前置动作。


def _settle_db(cfg) -> None:
    """把建库时遗留的连接**强制回收掉**，之后才取"一个字节都没写"的基准快照。

    为什么必须显式回收：`storage.init_db()` 里那条连接是**引用循环**
    （`sqlite3.Connection` 与它的 cursor 互指），只能等 GC 回收；它被回收时会做一次
    WAL checkpoint，把已经提交的 schema 从 `-wal` 合并进主文件 —— 于是"逐字节比较"
    会在一个和被测行为**毫无关系**的时刻变红（实测：只是 import 了另一个模块，
    主文件就从 4KB 变成 155KB、`-wal`/`-shm` 一起消失）。先收干净，比较才是在比数据。
    """
    import gc

    gc.collect()


def _only_daily(results: list) -> Any:
    """日更返回的 `SyncResult` 列表里**有且只有一条**，就是当日日更那条。

    为什么专门钉"只有一条"：日更返回的是**列表**（与 `sync.daily_update()` 同形状），
    界面上是逐条显示的。一旦哪天有人在前面塞进别的阶段（"装数据包"这类附加步骤），
    这个列表就会变长、界面多一行 —— 而每个调用方都以为 `results[0]` 是日更。
    这里按阶段名认，并要求恰好一条：形状一变就在这里炸，而不是让某条用例悄悄测错对象。

    （2026-09-18 用户否掉了"随包历史数据"方案：同花顺是主源、历史走用户自己的 Key，
    所以那种附加结果不会再出现 —— 但这条断言留着，它保护的是"日更只有一条结果"。）
    """
    daily = [r for r in results if "免 Key" in r.stage]
    assert len(daily) == 1, f"日更结果应当恰好一条，实际：{[r.stage for r in results]}"
    return daily[0]


@pytest.fixture()
def conn(cfg):
    """临时库（`cfg` 的数据目录在 tmp_path 下，不碰用户真实目录）的一条连接。"""
    storage.init_db(cfg.db_path)
    connection = storage.connect(cfg.db_path)
    yield connection
    connection.close()


# ════════════════════════════════════════════════════════════════════════
# A. 涨跌幅规则：板块 → 幅度 → 取整方式
# ════════════════════════════════════════════════════════════════════════


def test_board_of_covers_all_three_segments() -> None:
    """板块分段必须认下 `92/8/4`（北交所）与 `30/68`（创业板/科创板）。

    实测依据（2026-09-17 全市场 5564 只）：北交所有 **344 只**，既有 `920xxx` 新股号段，
    也有 `8xxxxx`/`4xxxxx` 老号段。把 8/4 当成主板，这 344 只的涨停价会全部按 10% 算错
    （实际 30%），而"少算"只会表现为"涨停池里没有它们"，不会报错。
    """
    assert ps.board_of("600519") == "main"
    assert ps.board_of("000504") == "main"
    assert ps.board_of("300750") == "gem"
    assert ps.board_of("688111") == "gem"
    assert ps.board_of("920000") == "bj"
    assert ps.board_of("830799") == "bj"
    assert ps.board_of("430047") == "bj"


def test_main_board_is_ten_percent_rounded_half_up() -> None:
    """主板（`60/00`）是 10%，取整方式为**四舍五入**。

    为什么必须钉取整方式：夹具里 `000504` 前收 8.27，腾讯给的涨停价是 **9.10**；
    截断会得 9.09 —— 差一分钱，`is_limit_up(9.10, 8.27)` 判假、涨停池少一只，
    而整个过程不报任何错。实测依据：全市场主板有 1562 只能区分两种取整，
    **全部**是四舍五入。
    """
    row = _by_symbol("000504")
    assert ps.limit_pct(row["symbol"], row["name"]) == ps.LIMIT_MAIN

    got = ps.limit_up_price(row["prev_close"], row["symbol"], row["name"])
    assert got == pytest.approx(row["limit_up"])          # 与腾讯的权威值分毫不差
    assert got > _floor(row["prev_close"] * 1.1)          # 截断会少一分钱 → 必须严格更大
    assert ps.limit_down_price(row["prev_close"], row["symbol"], row["name"]) == pytest.approx(
        row["limit_down"])


def test_gem_and_star_are_twenty_percent_rounded_half_up() -> None:
    """创业板/科创板（`30/68`）是 20%，同样四舍五入。

    实测依据（模块 docstring）：305.48 → 366.58（截断会得 366.57）；全市场 744 只能区分
    取整方式的创业板/科创板票**全部**是四舍五入。
    """
    assert ps.limit_pct("300750") == ps.LIMIT_GEM
    assert ps.limit_pct("688111") == ps.LIMIT_GEM
    raw = 305.48 * 1.2
    assert ps.limit_up_price(305.48, "300750") == pytest.approx(366.58)
    assert ps.limit_up_price(305.48, "300750") > _floor(raw)
    assert ps.limit_down_price(305.48, "300750") == pytest.approx(round(305.48 * 0.8 + 1e-9, 2))


def test_beijing_limit_up_truncates_instead_of_rounding() -> None:
    """北交所（`92/8/4`）是 30%，取整方式是**截断**（与沪深两市相反）。

    实测依据：49.12 × 1.3 = 63.856 → 腾讯给 **63.85**；四舍五入会得 63.86。
    全市场有 181 只北交所票能区分这两种取整，**全部**是截断 —— 这不是"代码风格"，
    是照抄交易所的规则。
    """
    assert ps.limit_pct("920000") == ps.LIMIT_BJ
    raw = 49.12 * 1.3
    got = ps.limit_up_price(49.12, "920000")
    assert got == pytest.approx(63.85)
    assert got == pytest.approx(_floor(raw))
    assert got != pytest.approx(round(raw + 1e-9, 2))     # 四舍五入的 63.86 是错的
    # 夹具里那只北交所票也一样：13.70 × 1.3 = 17.81（恰好整数，两种取整同值）
    row = _by_symbol("920000")
    assert ps.limit_up_price(row["prev_close"], row["symbol"], row["name"]) == pytest.approx(
        row["limit_up"])


def test_beijing_limit_down_rounds_up_so_it_never_breaks_the_band() -> None:
    """北交所跌停价**向上取整**：跌停价不能低于理论下限，与涨停价"不能高于理论上限、
    所以向下取整"互为镜像。

    实测依据（2026-09-17 全市场 5564 只）：北交所有 294 只票能区分"向上取整 vs 向下截断"，
    **全部**是向上取整（49.12 × 0.7 = 34.384 → 34.39）。搞成截断（34.38）会让跌停价
    低于交易所给的下限，`is_limit_down` 在真跌停那天判假。
    """
    raw = 49.12 * 0.7
    got = ps.limit_down_price(49.12, "920000")
    assert got == pytest.approx(math.ceil(raw * 100 - 1e-9) / 100)
    assert got == pytest.approx(34.39)
    assert got > _floor(raw)                              # 截断会得 34.38，低于下限
    # 夹具里两只北交所票的跌停价也必须分毫不差
    for symbol in ("920000", "920001"):
        row = _by_symbol(symbol)
        assert ps.limit_down_price(row["prev_close"], row["symbol"], row["name"]) == pytest.approx(
            row["limit_down"])


def test_st_does_not_change_the_band() -> None:
    """`ST`/`*ST` **不改变**涨跌幅（实测：002528 *ST英飞 前收 6.53、涨停价 7.18 = ×1.10，
    而不是很多人以为的 5%）。

    这条最容易按"常识"改错（"ST 不是 5% 吗"），而改错的后果是**全市场 200 多只 ST**
    的涨停判真判假整体错位。实测依据：全市场 204 只 ST 的涨停价全部与同板块规则一致。
    """
    row = _by_symbol("002528")
    assert "ST" in row["name"].upper()
    assert ps.limit_pct(row["symbol"], row["name"]) == ps.LIMIT_MAIN     # 是 10%，不是 5%
    computed = ps.limit_up_price(row["prev_close"], row["symbol"], row["name"])
    assert computed == pytest.approx(row["limit_up"])                    # 7.18 = 6.53 × 1.10
    # 按规则判它**确实**是涨停（只是进不了涨停池：口径排除 ST，见 D 节）
    assert ps.is_limit_up(row["last_price"], row["prev_close"], row["symbol"], row["name"]) is True
    # 北交所的 ST（若有）同样跟随板块：30%
    assert ps.limit_pct("920000", "*ST测试") == ps.LIMIT_BJ


def test_unreformed_s_share_uses_five_percent() -> None:
    """未股改 `S` 股是 **5%**（极少数例外），且要靠**名称**才能认出来。

    实测：`600182 佳通` 前收 12.68 → 涨停价 13.31 = ×1.05（不带名称按主板 10% 会算成 13.95）。
    所以 `limit_up_price` 的 `name` 参数不是装饰 —— 它的唯一用途就是这一条，
    漏传就会让这只票的涨停价错 6 毛钱。
    """
    assert ps.is_s_stock("S佳通") is True
    assert ps.limit_pct("600182", "S佳通") == ps.LIMIT_S
    assert ps.limit_up_price(12.68, "600182", "S佳通") == pytest.approx(13.31)
    assert ps.limit_down_price(12.68, "600182", "S佳通") == pytest.approx(12.05)
    # 不带名称 → 按主板 10%（说明"名称"这个入参真的被用了）
    assert ps.limit_up_price(12.68, "600182") == pytest.approx(round(12.68 * 1.1 + 1e-9, 2))


def test_is_s_stock_never_swallows_an_st_share() -> None:
    """`ST`/`*ST` **不是** `S` 股：判别顺序搞反会让 200 多只 ST 被打成 5% 幅度。

    名称判别的坑在于两个前缀都带 `S`：必须"以 S 开头且不以 ST 开头"才算未股改。
    """
    assert ps.is_s_stock("S佳通") is True
    assert ps.is_s_stock("ST英飞") is False
    assert ps.is_s_stock("*ST英飞") is False
    assert ps.is_s_stock("深物业A") is False
    assert ps.is_s_stock("") is False
    assert ps.is_s_stock(None) is False


def test_every_fixture_limit_price_reproduces_the_snapshot_value() -> None:
    """19 行里凡是腾讯给了涨跌停价的，规则都必须**分毫不差**地复现它（真夹具 = 真裁判）。

    这一条同时钉住"规则算得出来 ≠ 规则说了算"：`601091 N沈鼓` / `688801 C燧原-U` 是新股，
    腾讯给的涨停价是 **None**（新股当天没有涨跌幅限制），而规则照样能算出 4.83 / 572.40。
    当天那一根必须用快照的权威值（`limit_pool_from_snapshot` 就是这么做的），
    规则只用于**历史回补** —— 反过来覆盖就会凭空多出两只"涨停"。
    """
    rows = _rows()
    assert all(row["prev_close"] > 0 for row in rows)
    missing = 0
    for row in rows:
        computed_up = ps.limit_up_price(row["prev_close"], row["symbol"], row["name"])
        computed_down = ps.limit_down_price(row["prev_close"], row["symbol"], row["name"])
        assert computed_up is not None and computed_down is not None
        if row["limit_up"] is None:
            missing += 1
            assert row["limit_down"] is None
            assert pm._at_limit(row["last_price"], row["limit_up"]) is False
            continue
        assert computed_up == pytest.approx(row["limit_up"]), row["symbol"]
        assert computed_down == pytest.approx(row["limit_down"]), row["symbol"]
    assert missing >= 1          # 夹具里必须有"新股没有涨停价"的行，否则上面那半条没被测到


def test_is_limit_up_is_true_only_on_the_exact_price() -> None:
    """涨停**判真**、差 1 分**判假** —— "接近涨停"绝不能被当成涨停。

    容差只有半分钱（`PRICE_EPS`）：主板一档涨停的相邻价位差 10%（几毛钱），
    放宽到 1 分以上就可能把"涨了 9.99%"当成涨停，涨停池与连板数一起错。
    """
    row = _by_symbol("000504")
    target = row["limit_up"]
    assert ps.PRICE_EPS < 0.01
    assert ps.is_limit_up(target, row["prev_close"], row["symbol"], row["name"]) is True
    assert ps.is_limit_up(target - 0.01, row["prev_close"], row["symbol"], row["name"]) is False
    assert ps.is_limit_up(target + 0.01, row["prev_close"], row["symbol"], row["name"]) is False
    # 半分钱以内仍算压在涨停价上（float 会把 7.18 变成 7.180000000000001）
    assert ps.is_limit_up(target + ps.PRICE_EPS / 2, row["prev_close"],
                          row["symbol"], row["name"]) is True


def test_fixture_rows_below_the_limit_are_never_judged_as_limit_up() -> None:
    """夹具里所有**收在涨停价之下**的票都不许被判成涨停（含停牌票与摸过涨停的炸板票）。

    期望集合从夹具算（"现价 < 涨停价"的行），不手写名单 —— 手写名单会漏掉
    `000572` 那种"最高摸到涨停价、收在下面"的炸板票，而它正是最容易误判的一类。
    """
    below = [r for r in _rows()
             if r.get("limit_up") is not None and r["last_price"] < r["limit_up"] - ps.PRICE_EPS]
    assert below                                   # 夹具里必须有这类行
    for row in below:
        assert ps.is_limit_up(row["last_price"], row["prev_close"],
                              row["symbol"], row["name"]) is False, row["symbol"]
    # 炸板那一只：最高价 == 涨停价（所以"摸到过"），但现价在下面 → 不是涨停
    touched = _by_symbol("000572")
    assert touched["high"] == touched["limit_up"]
    assert ps.is_limit_up(touched["high"], touched["prev_close"],
                          touched["symbol"], touched["name"]) is True   # 最高价确实压在涨停价


def test_is_limit_down_mirrors_the_same_rule() -> None:
    """跌停判定的方向与涨停镜像（向下 ×（1−幅度），取整同板块）。

    判错的后果和涨停一样隐蔽：跌停家数、以及"跌停不接"这类过滤会整体失效。
    """
    row = _by_symbol("002163")                     # 夹具里 7.86 == 跌停价
    assert row["last_price"] == pytest.approx(row["limit_down"])
    assert ps.is_limit_down(row["last_price"], row["prev_close"],
                            row["symbol"], row["name"]) is True
    assert ps.is_limit_down(row["last_price"] + 0.01, row["prev_close"],
                            row["symbol"], row["name"]) is False
    assert ps.is_limit_up(row["last_price"], row["prev_close"],
                          row["symbol"], row["name"]) is False
    # ST 跌停也一样判得出来（口径排除是**池子**的事，不是判定的事）
    st = _by_symbol("000056")
    assert st["last_price"] == pytest.approx(st["limit_down"])
    assert ps.is_limit_down(st["last_price"], st["prev_close"],
                            st["symbol"], st["name"]) is True


@pytest.mark.parametrize("prev_close", [None, 0, 0.0, -1.0])
def test_limit_prices_are_none_without_a_usable_prev_close(prev_close) -> None:
    """前收为空 / 0 / 负数 → **None**（"没有这个数"，不是 0）。

    返回 0 的后果不是"少一条数据"而是"编了一个数字"：界面上会显示"涨停价 0.00"，
    而 `is_limit_up` 的半分钱容差在 0 附近毫无意义（任何几毛钱的价格都远离它，
    但一旦有人把容差按比例理解就会全错）。None 才是唯一说得通的答案。
    """
    assert ps.limit_up_price(prev_close, "600519") is None
    assert ps.limit_down_price(prev_close, "600519") is None
    assert ps.is_limit_up(10.00, prev_close, "600519") is False
    assert ps.is_limit_down(10.00, prev_close, "600519") is False
    assert ps.is_limit_up(None, 10.00, "600519") is False
    assert ps.is_limit_down(None, 10.00, "600519") is False


# ════════════════════════════════════════════════════════════════════════
# B. 除权检测与因子步长
# ════════════════════════════════════════════════════════════════════════


def test_no_ex_right_when_the_snapshot_prev_close_matches_the_db() -> None:
    """快照昨收 == 库里前收 → 没除权，`k == 1.0`（因子一步都不动）。

    实测：2026-09-17 全市场 5564 只里 5552 只属于这一档。这一档必须"什么都不做" ——
    任何"顺手调一下"的想法都会在这一天毁掉 5552 只票的复权序列。
    """
    prev = _by_symbol("000504")["prev_close"]
    changed, k = ps.detect_ex_right(prev, prev)
    assert changed is False
    assert k == 1.0
    assert ps.factor_step(prev, prev) == 1.0


def test_ex_right_makes_the_factor_grow() -> None:
    """快照昨收比库里前收**小** → 除权，`k = 库前收 / 快照昨收 > 1`。

    方向是这个（容易搞反）：分红送转让**原始价跳空下跌**，而"后复权 = 不复权 × 因子"，
    要让后复权连续，因子必须**变大**。实测：12 只不一致票的比值都在 1.0000~1.0300。
    """
    changed, k = ps.detect_ex_right(10.00, 9.70)
    assert changed is True
    assert k > 1.0
    assert k == pytest.approx(10.00 / 9.70)
    assert ps.factor_step(10.00, 9.70) == pytest.approx(k)
    # 连续性：除权当天的后复权价（9.70 × k）正好回到除权前的原始价
    assert 9.70 * k == pytest.approx(10.00)


def test_a_three_percent_gap_is_an_ex_right() -> None:
    """差 3% 必须判成除权 —— 那是典型的分红/送转量级（实测 12 只不一致票的比值上限 1.0300）。

    判不出来的后果：当天的后复权价会比前一天**凭空低 3%**，所有以"后复权涨幅"为输入的
    策略都会看到一次假跌。
    """
    changed, k = ps.detect_ex_right(10.30, 10.00)
    assert changed is True
    assert k == pytest.approx(1.03)


def test_ex_right_tolerance_stays_within_half_a_cent() -> None:
    """差半分钱以内**不算**除权：两边都是两位小数的价格，正常情况应当分毫不差。

    容差放宽（比如放到 1 分或 1%）的代价是"隔日正常波动被当成除权"：
    因子被乘上一个无中生有的 k，而且**不可逆**（历史后复权价整体被缩放）。
    """
    assert ps.EX_RIGHT_EPS <= 0.005           # 半分钱，不能更宽
    assert ps.EX_RIGHT_EPS < 0.01
    assert ps.detect_ex_right(10.00, 10.00 - 0.004)[0] is False     # 差 4 厘：不算
    assert ps.detect_ex_right(10.00, 10.00 + 0.004)[0] is False
    assert ps.detect_ex_right(10.00, 10.00 - 0.006)[0] is True      # 差 6 厘：算


def test_missing_prev_close_never_touches_the_factor() -> None:
    """昨收为空 → 不算除权、`k = 1.0`；**不能因为"没数据"就乱改因子**。

    公开源偶尔会对个别票不给昨收。这时猜一个因子是不可逆的污染（乘上去就回不来），
    所以唯一的正确做法是"什么都不做"：`k` 保持 1.0，因子原样沿用。
    """
    assert ps.detect_ex_right(10.00, None) == (False, 1.0)
    assert ps.detect_ex_right(None, 10.00) == (False, 1.0)
    assert ps.detect_ex_right(None, None) == (False, 1.0)
    # 非正的"昨收"在公开源里等于"没这个数"：即使被判成除权，k 也必须保持 1.0
    for bad in (0, 0.0, -1.0):
        assert ps.detect_ex_right(10.00, bad)[1] == 1.0
    assert ps.factor_step(10.00, 0) == 1.0
    assert ps.factor_step(10.00, None) == 1.0
    assert ps.factor_step(10.00, -1.0) == 1.0


# ════════════════════════════════════════════════════════════════════════
# C. 当日 K 线落库（`bars_from_snapshot` / `last_bars` / `write_daily_raw`）
# ════════════════════════════════════════════════════════════════════════


def test_suspended_rows_never_become_a_fake_bar(cfg) -> None:
    """成交量为 0 = 停牌：**不写** K 线。

    写上它会变成一根"四个价格都等于昨收、成交量为 0"的假 K 线，策略会把它当成一个
    真实交易日：均线多一根、"上一根"错位、连板/回踩的窗口整体偏移一天。
    夹具里 `000016`/`002731` 就是两只停牌票（实测当天全市场 12 只）。
    """
    rows = _suspended(_rows())
    # 这一条同时是夹具完整性断言：夹具里"成交量为 0"的行必须就是那两只
    assert [r["symbol"] for r in rows] == ["000016", "002731"]

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        bars, stat = ps.bars_from_snapshot(rows, conn, TODAY)
        written = storage.write_daily_raw(conn, bars) if bars else 0
        total = conn.execute("SELECT COUNT(*) FROM stock_daily_raw").fetchone()[0]

    assert bars == []
    assert written == 0 and total == 0
    assert stat["suspended"] == len(rows)
    assert stat["written"] == 0


def test_all_writable_rows_land_in_the_database(cfg) -> None:
    """19 行夹具 → **17 根** K 线（2 只停牌不写），且统计口径自洽。

    "写了几行"是日更结果里给人看的那个数（`rows`），它必须等于库里真的多了几根 ——
    统计与写入各算一遍（`stat["written"]` 与 `len(bars)`）就是为了让两边对不上时立刻炸。
    """
    rows = _rows()
    expected = _writable(rows)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        bars, stat = ps.bars_from_snapshot(rows, conn, TODAY)
        written = storage.write_daily_raw(conn, bars)
        stored = conn.execute("SELECT COUNT(*) FROM stock_daily_raw WHERE date = ?",
                              (TODAY,)).fetchone()[0]

    assert len(bars) == len(expected) == written == stored == stat["written"]
    assert stat["suspended"] == len(rows) - len(expected)
    assert {row[0] for row in bars} == {r["symbol"] for r in expected}


def test_new_symbols_start_from_factor_one(cfg) -> None:
    """库里没有任何历史的票 → `factor = 1.0`。

    "没有前收就没有除权"：猜一个因子会让这只票的后复权序列**从一开始就是错的**，
    而它以后每一天都乘在这个错值上，越走越远。写 1.0 是唯一说得通的默认值。
    """
    rows = _rows()
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        bars, stat = ps.bars_from_snapshot(rows, conn, TODAY)

    assert len(bars) == len(_writable(rows))
    assert stat["new"] == len(bars)                 # 空库 → 全部算"新股"
    assert stat["ex_right"] == 0
    assert {row[8] for row in bars} == {1.0}
    assert stat["ex_ratio_min"] is None and stat["ex_ratio_max"] is None


def test_factor_is_inherited_when_the_prev_close_matches(cfg) -> None:
    """库里有历史且前收一致 → 因子**沿用上一根**（不是回到 1.0）。

    回到 1.0 等于"把以前累积的除权全部抹掉"：后复权序列在这一天断成两截，
    而且断点在图表上看起来只是一次普通的涨跌，查不出来。
    """
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], YESTERDAY, row["prev_close"], factor=1.5)
        bars, stat = ps.bars_from_snapshot([row], conn, TODAY)

    assert stat["ex_right"] == 0
    assert len(bars) == 1
    assert bars[0][8] == pytest.approx(1.5)


def test_factor_is_multiplied_by_k_on_an_ex_right_day(cfg) -> None:
    """库里有历史但前收不一致 → 除权 → 因子 = 上一根因子 × k，写出来就是 **1.03**。

    造的是真实场景（送转/分红量级）：把库里前收改成"快照昨收 × 1.03"，那 k = 1.03。
    不复权的收盘价照原样落库（因子是唯一的调整手段）——
    如果这里连收盘价也一起改，就成了"既复权又乘因子"，双重调整。
    """
    row = _by_symbol("000504")
    db_close = row["prev_close"] * 1.03
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], YESTERDAY, db_close, factor=1.0)
        bars, stat = ps.bars_from_snapshot([row], conn, TODAY,
                                          names={row["symbol"]: "改过的名字"})

    assert stat["ex_right"] == 1
    assert len(bars) == 1
    assert bars[0][8] == pytest.approx(1.03)                   # 因子 = 1.0 × 1.03
    assert bars[0][5] == pytest.approx(row["last_price"])       # 不复权价原样落库
    assert stat["ex_ratio_min"] == pytest.approx(1.03)
    assert stat["ex_ratio_max"] == pytest.approx(1.03)
    # 除权明细里的名字优先取 `names`（界面上要说清"是哪只票被重标了因子"）
    assert len(stat["ex_symbols"]) == 1
    assert stat["ex_symbols"][0][0] == row["symbol"]
    assert stat["ex_symbols"][0][1] == "改过的名字"
    assert stat["ex_symbols"][0][2] == pytest.approx(1.03)


def test_ex_right_factor_compounds_on_top_of_the_previous_factor(cfg) -> None:
    """除权因子是**乘**在上一根上，不是替换：上一根 1.5、k = 1.03 → 1.545。

    替换的后果是"以前除权的历史全部作废"，而这条票的后复权价会在除权日前后各错一截。
    """
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], YESTERDAY, row["prev_close"] * 1.03, factor=1.5)
        bars, _ = ps.bars_from_snapshot([row], conn, TODAY)

    assert bars[0][8] == pytest.approx(1.5 * 1.03)


def test_bars_land_in_the_storage_column_order(cfg) -> None:
    """9 列、列序必须与 `storage.write_daily_raw` 一致：
    `(symbol, date, open, high, low, close, volume, turnover, factor)`。

    列序写错是"数据看着有、其实全是错位"的典型 bug：所有 SQL 都能跑通、行数也对，
    但 K 线图会全线阴阳颠倒、成交额被当成成交量。所以这里既钉元组本身，也**写进库再读回来**
    按列名核对 —— 只断言元组是挡不住"写的时候又换了一次序"的。
    """
    row = _by_symbol("000011")          # 开/高/低/收四个值互不相同（8.40/9.03/8.28/8.93）
    assert len({row["open"], row["high"], row["low"], row["last_price"]}) == 4
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        bars, _ = ps.bars_from_snapshot([row], conn, TODAY)
        storage.write_daily_raw(conn, bars)
        got = conn.execute("SELECT * FROM stock_daily_raw WHERE symbol = ?",
                           (row["symbol"],)).fetchone()

    assert len(bars) == 1 and len(bars[0]) == 9
    assert tuple(bars[0]) == (row["symbol"], TODAY, row["open"], row["high"], row["low"],
                              row["last_price"], row["volume"], row["turnover"], 1.0)
    assert got["symbol"] == row["symbol"]
    assert got["date"] == TODAY
    assert got["open"] == pytest.approx(row["open"])
    assert got["high"] == pytest.approx(row["high"])
    assert got["low"] == pytest.approx(row["low"])
    assert got["close"] == pytest.approx(row["last_price"])
    assert got["volume"] == pytest.approx(row["volume"])
    assert got["turnover"] == pytest.approx(row["turnover"])
    assert got["factor"] == pytest.approx(1.0)
    assert got["updated_at"]                        # 写入时间不能为空（排查"哪次写坏的"）

    # 后复权视图（下游只认它）读出来的必须是"不复权 × 因子"
    view = conn.execute("SELECT close FROM stock_daily_hfq WHERE symbol = ? AND date = ?",
                        (row["symbol"], TODAY)).fetchone()
    assert view["close"] == pytest.approx(row["last_price"] * got["factor"])


def test_last_bars_picks_the_latest_bar_of_each_symbol(cfg) -> None:
    """`last_bars()` 取的是每只票**最近一根**（除权检测的基准）。

    取错（比如取成最早一根）会让"库里前收"永远对不上快照昨收 → 全市场被误判成除权，
    因子天天被乘。这里同时钉住"没历史的票不出现在结果里"（调用方靠 `None` 判新股）。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, "600001", STALE, 1.0, factor=1.1)
        _seed_bar(conn, "600001", YESTERDAY, 2.0, factor=1.2)
        _seed_bar(conn, "600002", YESTERDAY, 3.0, factor=1.3)
        got = ps.last_bars(conn, ["600001", "600002", "999999"])

    assert got["600001"] == (YESTERDAY, 2.0, 1.2)      # 最近那根，不是最早那根
    assert got["600002"] == (YESTERDAY, 3.0, 1.3)
    assert "999999" not in got


def test_last_bars_still_finds_a_very_old_bar(cfg) -> None:
    """库里最后一根**很旧**也必须找得到（它是这只票的除权基准）。

    为什么这里专门钉"很旧的也要找得到"：曾经按"最近 N 天"取基准（带回溯窗口），
    于是"放假回来 / 长时间没开机"的票落在窗口之外 → 被当成**没有历史** → 当天的因子从
    1.0 重算，整只票的复权序列在那天断成两截（图上看不出来：后复权的绝对水平本来就没意义，
    只有跨那天算出来的收益率会错）。所以基准查询不许带时间窗。
    """
    old = (_date.today() - timedelta(days=200)).isoformat()
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], old, row["prev_close"], factor=1.7)
        # 缺口判据是"早于**上一个交易日**"，所以日历里得有最近这几天
        # （真实运行时免 Key 日更会用指数日K把交易日尾巴灌进来，见 `calendar_tail`）
        _seed_calendar(conn, _days_ago(1), TODAY)
        got = ps.last_bars(conn, [row["symbol"]])
        bars, stat = ps.bars_from_snapshot([row], conn, TODAY)

    assert got[row["symbol"]][0] == old               # 没有时间窗：很旧也是基准
    assert bars[0][8] == pytest.approx(1.7)           # 因子沿用，不从 1.0 重来
    assert stat["new"] == 0
    assert stat["gap"] == 1                           # 缺天照旧被统计（"很旧"不等于"没历史"）


def test_last_bars_does_not_drop_symbols_across_chunks(cfg) -> None:
    """`last_bars` 按 500 只分片查（`CHUNK`）；跨分片不能丢票。

    丢一只 = 那一只被当成新股、因子从 1.0 重来，而这**不报错**（少的是"复权基准"，
    不是行数）。所以造 501 只（正好跨过一次分片边界）来钉。
    """
    symbols = [f"6{i:05d}" for i in range(501)]
    assert len(set(symbols)) == 501
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [(s, YESTERDAY, 1.0, 1.0, 1.0, 1.0, 1000.0, 1000.0, 1.0)
                                       for s in symbols])
        got = ps.last_bars(conn, symbols)

    assert len(got) == 501
    assert set(got) == set(symbols)


def test_missing_days_are_counted_but_never_invented(cfg) -> None:
    """库里最后一根离今天很远（用户几天没开机）→ 计入 `gap`，但**绝不编**出中间那几根。

    公开快照只给得出当天那一根，缺的历史要靠逐只日K补（界面上是"下载/更新历史数据"）。
    这里同时钉两件事：缺口被**统计**（好让上层提示用户去补），以及库里只多今天这一根
    （编 K 线是伪造数据，比"缺天"严重得多）。
    """
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], STALE, row["prev_close"], factor=1.0)
        _seed_calendar(conn, _days_ago(1), TODAY)
        bars, stat = ps.bars_from_snapshot([row], conn, TODAY)
        storage.write_daily_raw(conn, bars)
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM stock_daily_raw WHERE symbol = ? ORDER BY date",
            (row["symbol"],))]

    assert stat["gap"] == 1
    assert len(bars) == 1
    assert days == sorted([STALE, TODAY])           # 中间那些天一根都没被编出来


def test_fresh_bars_are_not_reported_as_a_gap(cfg) -> None:
    """库里**没有**这只票的历史时不计 gap（"新票"与"缺天"是两件事）。

    这两件事混在一起的话，用户第一次日更（全市场都是新票）就会看到
    "有 5000 多只票中间缺天，请去补历史"这种完全错误的提示。
    """
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        bars, stat = ps.bars_from_snapshot([row], conn, TODAY)

    assert len(bars) == 1
    assert stat["new"] == 1
    assert stat["gap"] == 0


# ════════════════════════════════════════════════════════════════════════
# D. 涨停池（`limit_pool_from_snapshot` / `_previous_pool_days`）
# ════════════════════════════════════════════════════════════════════════


def test_limit_pool_excludes_st_and_beijing(conn) -> None:
    """公开源涨停池要**对齐同花顺的口径：不含 ST、不含北交所**。

    实测依据（模块 docstring）：同花顺那个池子 1188 个交易日里 `is_st` 全为 0、
    也没有一条北交所代码；不跟着走的话"加不加 Key"会让同一个家数跳几只
    （2026-09-17：腾讯 50 vs 同花顺 47，差的 3 只全是 ST）。
    夹具里 000504/000868/000920 是普通涨停，002528/002569 是 `*ST` 且压在涨停价上。
    """
    rows = _rows()
    expected = _at_limit_rows(rows)
    pool = ps.limit_pool_from_snapshot(rows, conn, TODAY)

    assert {row[1] for row in pool} == {r["symbol"] for r in expected}
    assert len(pool) == len(expected) == 3
    # 被排除的那几只确实"够条件"（否则这条用例只证明了夹具里没人在涨停价上）
    at_limit = [r for r in rows if pm._at_limit(r.get("last_price"), r.get("limit_up"))]
    excluded_st = [r for r in at_limit if pm._excluded_from_limits(r)]
    assert {r["symbol"] for r in excluded_st} == {"002528", "002569"}
    assert all("ST" in r["name"].upper() for r in excluded_st)
    assert not any(row[1] in {r["symbol"] for r in excluded_st} for row in pool)

    # 北交所的排除也要真的生效：夹具里 920001 当天涨 9.09%（没压在涨停价上），
    # 所以这里把它的现价搬到涨停价上，造一个"北交所也涨停"的情形
    bj = dict(_by_symbol("920000"), last_price=_by_symbol("920000")["limit_up"])
    assert pm._excluded_from_limits(bj) is True
    assert ps.limit_pool_from_snapshot([bj], conn, TODAY) == []


def test_limit_pool_high_days_roll_over_from_the_previous_day(conn) -> None:
    """连板数：上一个有涨停池的交易日里它在 → 天数 +1；不在 → 1（首板）。

    公开源没有"连板数"这个字段，只能靠库里滚雪球。滚错一天，`连板()>=2` 这类公式与
    "连板回踩"策略的输入就全错了 —— 而它们在界面上都显示为一个数字，看不出对错。
    """
    rows = _rows()
    expected = sorted(r["symbol"] for r in _at_limit_rows(rows))
    assert len(expected) >= 2                    # 需要"有连板的"和"没连板的"各至少一只
    starred = expected[0]
    storage.write_limit_up_pool(conn, [_pool_row(YESTERDAY, starred, high_days=2)])

    pool = ps.limit_pool_from_snapshot(rows, conn, TODAY)
    days = {row[1]: row[3] for row in pool}

    assert days[starred] == 3                    # 昨天 2 连板 → 今天 3 连板
    assert {s: d for s, d in days.items() if s != starred} == {
        s: 1 for s in expected if s != starred}  # 其余都是首板


def test_limit_pool_high_days_survives_a_null_previous_row(conn) -> None:
    """上一交易日的 `high_days` 为空 → 今天算 2（`or 1` 兜底），不是崩、也不是 1。

    `None + 1` 会直接抛 TypeError 把整个日更打断；而老数据/别的源都可能留下空值。
    兜成 1（当成首板）则是把一只连板票降级成首板 —— 两个都不能接受，所以必须是 2。
    """
    row = _at_limit_rows(_rows())[0]
    storage.write_limit_up_pool(conn, [_pool_row(YESTERDAY, row["symbol"], high_days=None)])

    pool = ps.limit_pool_from_snapshot(_rows(), conn, TODAY)
    days = {r[1]: r[3] for r in pool}

    assert days[row["symbol"]] == 2


def test_limit_pool_rows_use_the_storage_column_order(conn) -> None:
    """21 列、列序与 `storage.LIMIT_UP_COLUMNS` 一致（写错列序几乎查不出来）。

    21 列里有 11 列公开源拿不到（NULL），错位之后界面上的"换手率"可能来自"封单额"，
    而两者都是数字、都不报错。所以这里既断言列数，也**写进库再读回来按列名核对**。
    """
    rows = _rows()
    pool = ps.limit_pool_from_snapshot(rows, conn, TODAY)
    written = storage.write_limit_up_pool(conn, pool)
    stored = {r["symbol"]: r for r in conn.execute(
        "SELECT * FROM limit_up_pool WHERE date = ?", (TODAY,))}

    assert written == len(pool) > 0
    assert all(len(row) == len(storage.LIMIT_UP_COLUMNS) == 21 for row in pool)
    for row in pool:
        src = _by_symbol(row[1])
        got = stored[row[1]]
        assert got["date"] == TODAY
        assert got["name"] == src["name"]
        assert got["high_days"] == 1
        assert got["turnover_rate"] == pytest.approx(src["turnover_rate"])
        assert got["change_rate"] == pytest.approx(src["pct"])
        assert got["last_price"] == pytest.approx(src["last_price"])
        assert got["source"] == "public"
        assert got["is_st"] == 0
        assert got["updated_at"]


def test_limit_pool_fields_the_public_source_cannot_give_are_null(conn) -> None:
    """公开源拿不到的字段必须是 **NULL**，不是 0。

    编一个 0 出来的后果很具体：`连板()>=2` 这类公式会把"没有这个数"当成
    "封单额为 0 / 开板次数为 0"，把票判成不合格 —— 而"没有数据"和"数据是 0"是两件事。
    特别是 `currency_value`：它的语义（成交额还是流通市值）**没有任何文档可证**，
    全项目也没有一处读它，所以故意留空；塞一个语义可能标错的数进去，
    以后有人读它就会得到错的结果，而且查不出来。
    """
    unavailable = ("limit_up_type", "first_limit_up_time", "last_limit_up_time",
                   "order_amount", "open_num", "reason_type", "currency_value",
                   "is_again_limit", "is_new", "continue_day_text", "max_seal_money")
    assert set(unavailable) <= set(storage.LIMIT_UP_COLUMNS)
    assert "currency_value" in unavailable          # 语义无文档可证 → 故意留空，不要改成成交额

    pool = ps.limit_pool_from_snapshot(_rows(), conn, TODAY)
    storage.write_limit_up_pool(conn, pool)
    index_of = {name: i for i, name in enumerate(storage.LIMIT_UP_COLUMNS)}

    assert pool
    for name in unavailable:
        assert all(row[index_of[name]] is None for row in pool), name
    stored = conn.execute("SELECT * FROM limit_up_pool WHERE date = ? LIMIT 1", (TODAY,)).fetchone()
    for name in unavailable:
        assert stored[name] is None, name
    # 反过来：拿得到的字段不许是 NULL（否则"留空"就成了"整行都是空"的借口）
    for name in ("date", "symbol", "name", "high_days", "turnover_rate", "change_rate",
                 "last_price", "is_st", "source", "updated_at"):
        assert stored[name] is not None, name


def test_limit_pool_is_st_column_is_an_integer_flag(conn, monkeypatch) -> None:
    """`is_st` 那一列是 **1/0**（与同花顺那一路一致），不是 True/False、也不是 None。

    对齐的是 SQL 层：库里 1188 个交易日的 `is_st` 全是 0/1，换成布尔或 None 会让既有查询
    悄悄改变含义。当前口径下 ST 根本进不了池子（见上一条用例），所以正常路径上只能是 0；
    这里把排除开关临时关掉，专门钉"1 这个取值真的会出现、而且是整数 1"。
    """
    pool = ps.limit_pool_from_snapshot(_rows(), conn, TODAY)
    normal = [row[18] for row in pool]
    assert normal and set(normal) == {0}
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in normal)

    monkeypatch.setattr(pm, "LIMIT_EXCLUDES_ST", False)
    st_rows = [r for r in _rows()
               if "ST" in r["name"].upper()
               and pm._at_limit(r.get("last_price"), r.get("limit_up"))]
    assert st_rows                                    # 夹具里必须真有"ST 且涨停"的行
    st_pool = ps.limit_pool_from_snapshot(st_rows, conn, TODAY)

    assert [(row[1], row[18]) for row in st_pool] == [(r["symbol"], 1) for r in st_rows]
    assert all(isinstance(row[18], int) and not isinstance(row[18], bool) for row in st_pool)


def test_previous_pool_days_uses_the_last_day_before_today(conn) -> None:
    """`_previous_pool_days()` 只看**今天之前**最近的那个有池子的日子。

    取错（比如取了今天或未来的行）会让连板数在同一份数据上跳：重跑一次日更，
    同一只票可能从 3 变 4 —— 幂等性被破坏，而且"昨天 2 连板"这个事实会被今天还没写完的
    自己的行覆盖。
    """
    assert ps._previous_pool_days(conn, TODAY) == {}      # 空库 → {}（首板）
    tomorrow = (_date.today() + timedelta(days=1)).isoformat()
    storage.write_limit_up_pool(conn, [
        _pool_row(STALE, "600001", high_days=5),
        _pool_row(YESTERDAY, "600002", high_days=2),
        _pool_row(TODAY, "600003", high_days=9),
        _pool_row(tomorrow, "600004", high_days=9),
    ])

    assert ps._previous_pool_days(conn, TODAY) == {"600002": 2}


# ════════════════════════════════════════════════════════════════════════
# E. 日更总入口（`daily_update_public`）
# ════════════════════════════════════════════════════════════════════════


def test_daily_update_public_writes_bars_pool_names_and_calendar(cfg, monkeypatch) -> None:
    """正常路径：一趟快照 → 当日日线 + 涨停池 + 代码表 + 交易日历，**四个都要落库**。

    期望值全部从夹具算（不手写"17 行行情 / 3 只涨停"）：这是"日更到底写了什么"的唯一凭据，
    而它同时是界面上那几行数字的来源 —— 少一项在界面上看不出来（比如涨停池没写，
    "连板回踩"当天就选不出票，用户只会觉得"今天没票"）。
    """
    rows = _rows()
    expected_bars = _writable(rows)
    expected_pool = _at_limit_rows(rows)
    expected_names = [r for r in rows if r.get("symbol") and str(r.get("name") or "").strip()]
    _patch_snapshot(monkeypatch, rows)
    _patch_market_date(monkeypatch)

    results = ps.daily_update_public(cfg)

    result = _only_daily(results)
    assert isinstance(result, sync.SyncResult)
    assert result.ok is True, result.error
    assert result.rows == len(expected_bars)
    assert result.error == ""
    assert "免 Key" in result.stage and "公开源" in result.stage
    assert result.detail.startswith(TODAY)

    extra = result.extra
    assert extra["day"] == TODAY
    assert extra["snapshot_rows"] == len(rows)
    for key in ("day", "limit_up", "stock_basic", "calendar", "written", "suspended",
                "new", "ex_right", "gap"):
        assert key in extra, key
    assert extra["written"] == result.rows
    assert extra["suspended"] == len(_suspended(rows))
    assert extra["limit_up"] == len(expected_pool)
    assert extra["stock_basic"] == len(expected_names)
    assert extra["calendar"] == 1
    assert "hint" not in extra                       # 空库里没有缺口，不该提示用户去补历史

    with storage.connect(cfg.db_path) as conn:
        bars = conn.execute("SELECT COUNT(*) FROM stock_daily_raw WHERE date = ?",
                            (TODAY,)).fetchone()[0]
        suspended_symbols = [r["symbol"] for r in _suspended(rows)]
        marks = ",".join("?" * len(suspended_symbols))
        suspended = conn.execute(
            f"SELECT COUNT(*) FROM stock_daily_raw WHERE symbol IN ({marks})",
            tuple(suspended_symbols)).fetchone()[0]
        pool = conn.execute("SELECT COUNT(*) FROM limit_up_pool WHERE date = ?",
                            (TODAY,)).fetchone()[0]
        basics = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
        calendar = conn.execute("SELECT source FROM trading_calendar WHERE date = ?",
                                (TODAY,)).fetchone()
        industries = conn.execute("SELECT COUNT(*) FROM stock_basic WHERE industry IS NOT NULL"
                                  ).fetchone()[0]

    assert bars == result.rows
    assert suspended == 0                            # 停牌票一根都没写（C 节的库级复核）
    assert pool == extra["limit_up"] > 0
    assert basics == extra["stock_basic"] == len(rows)
    assert calendar["source"] == "public"
    assert industries == 0                           # 公开源没有行业归属 → 一律 NULL


def test_daily_update_public_always_takes_a_fresh_snapshot(cfg, monkeypatch) -> None:
    """日更必须**强制重扫**（`force=True`）：5 分钟缓存里可能是上一次、甚至昨天的快照。

    拿缓存当"今天的 K 线"写进库，就是"数据看着有、其实是旧的"——而且它会把昨天的价格
    写到今天这一根上，之后所有策略都在错误的价格上跑。
    """
    seen: list[bool] = []
    _patch_snapshot(monkeypatch, _rows(), seen=seen)
    _patch_market_date(monkeypatch)

    ps.daily_update_public(cfg)

    assert seen == [True]


def test_daily_update_public_writes_nothing_on_an_empty_snapshot(cfg, monkeypatch) -> None:
    """快照为空（断网/被限流）→ **失败，且一个字节都不写**。

    "绝不写半份数据"：如果先写了代码表/日历、再发现行情为空，库里就出现
    "有日历、有名字、没有当天 K 线"的中间态 —— 选股会跑在半个市场上，
    而自检与界面都看不出哪里不对。
    """
    storage.init_db(cfg.db_path)
    _settle_db(cfg)                         # 先让建库那条连接被回收，基准快照才是"数据"
    before_bytes = cfg.db_path.read_bytes()
    before_files = sorted(p.name for p in cfg.data_dir.iterdir())
    _patch_snapshot(monkeypatch, [])
    _patch_market_date(monkeypatch)

    results = ps.daily_update_public(cfg)

    result = _only_daily(results)
    assert result.ok is False
    assert "不写任何数据" in result.error
    assert result.error and "空" in result.error
    assert result.rows == 0
    # 库文件与数据目录**逐字节**没变（比"行数为 0"更强：连 WAL/临时文件都不许出现）
    assert cfg.db_path.read_bytes() == before_bytes
    assert sorted(p.name for p in cfg.data_dir.iterdir()) == before_files
    with storage.connect(cfg.db_path) as conn:
        for table in ("stock_daily_raw", "limit_up_pool", "stock_basic", "trading_calendar"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_daily_update_public_writes_nothing_when_the_scan_raises(cfg, monkeypatch) -> None:
    """快照**抛异常**时同样不许写，且必须把异常类型写进错误里（不许吞成"成功 0 行"）。

    "日更完成（0 行）"是本项目定义的最严重的一类错误：用户会以为今天就是这样。
    这里同时钉住调用顺序 —— 快照失败就不该再去问"最近交易日"（那样只是白花一次请求）。
    """
    storage.init_db(cfg.db_path)
    _settle_db(cfg)                         # 同上：基准快照要在连接回收之后取
    before_bytes = cfg.db_path.read_bytes()

    def _scan_boom(**kwargs):
        raise OSError("网络不可达")

    def _date_boom():
        raise AssertionError("快照都取不到，不该再去问交易日")

    monkeypatch.setattr(pm, "cached_scan", _scan_boom)
    monkeypatch.setattr(ps, "market_date", _date_boom)

    results = ps.daily_update_public(cfg)

    result = _only_daily(results)
    assert result.ok is False
    assert "全市场快照失败" in result.error
    assert "OSError" in result.error
    assert cfg.db_path.read_bytes() == before_bytes


def test_daily_update_public_fails_before_any_request_when_the_data_dir_is_unusable(
        cfg, monkeypatch) -> None:
    """数据目录都建不出来时**一个请求都不该发**（先建目录、再取数）。

    反过来的代价是实打实的：取回来的全市场快照没地方落，白花 56 个请求 60 秒，
    还可能因为无谓的流量被限流；用户看到的是"跑了很久然后失败"。
    """
    def _boom_ensure_dirs():
        raise OSError("磁盘只读")

    def _bomb_scan(**kwargs):
        raise AssertionError("连目录都没建好就不该去取快照")

    monkeypatch.setattr(cfg, "ensure_dirs", _boom_ensure_dirs)
    monkeypatch.setattr(pm, "cached_scan", _bomb_scan)

    results = ps.daily_update_public(cfg)

    result = _only_daily(results)
    assert result.ok is False
    assert "OSError" in result.error
    assert result.rows == 0


def test_daily_update_public_warns_about_missing_days_with_a_way_out(cfg, monkeypatch) -> None:
    """缺天时必须在结果里给一句"缺的历史要用「下载/更新历史数据」按逐只日K补"。

    公开快照只给得出当天那一根：用户三天没开机就是三天缺口，程序编不出来，
    但**必须说**（`extra["hint"]`），否则用户以为数据是连续的，策略悄悄跑在缺口上。
    """
    row = _by_symbol("000504")
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        _seed_bar(conn, row["symbol"], STALE, row["prev_close"])
        # 日历里先有"上一个交易日"（否则判不出缺口 —— 那是刻意的：没依据就不报）
        _seed_calendar(conn, _days_ago(1), TODAY)
    _patch_snapshot(monkeypatch, [row])
    _patch_market_date(monkeypatch)

    result = _only_daily(ps.daily_update_public(cfg))

    assert result.ok is True
    assert result.rows == 1
    assert result.extra["gap"] == 1
    assert "缺" in result.extra["hint"]
    assert "历史" in result.extra["hint"]


def test_daily_update_public_tells_the_user_it_is_scanning(cfg, monkeypatch) -> None:
    """`note_cb` 的第一句话必须说清"在取全市场快照（约 1 分钟）"，最后一句是结果。

    免 Key 日更要跑一分钟左右（56 个请求）。没有这句话，用户看到的就是界面卡住，
    而"要不要等它跑完"的判断依据正是这句状态文案。
    """
    notes: list[str] = []
    _patch_snapshot(monkeypatch, _rows())
    _patch_market_date(monkeypatch)

    results = ps.daily_update_public(cfg, note_cb=notes.append)

    daily = _only_daily(results)
    assert notes
    assert "免 Key" in notes[0] and "全市场快照" in notes[0]
    assert notes[-1] == daily.detail
    assert daily.detail in notes


def test_calendar_sync_marks_the_public_source(cfg) -> None:
    """`sync_calendar_public` 要写 `trading_calendar` 且 `source='public'`。

    来源可追溯不是洁癖：免 Key 版的日历只有**一天**（快照给不出历史），有 Key 的是一年。
    排查"为什么日历只有一天"时必须分得清这一根是谁给的；写成默认的 `hithink`
    就会让人去查同花顺接口（那里其实是好的）。
    """
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        assert ps.sync_calendar_public(conn, TODAY) == 1
        got = conn.execute("SELECT date, source FROM trading_calendar").fetchone()
        assert got["date"] == TODAY
        assert got["source"] == "public"
        assert ps.sync_calendar_public(conn, "") == 0        # 没日期 → 不写
        assert conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 1


def test_name_sync_only_writes_rows_that_have_a_name(cfg) -> None:
    """代码表只写**有中文名**的票，行业一律留 NULL。

    - 名字为空 → 不写：`stock_basic.name` 是界面显示与推送正文的唯一来源，
      写一条空名字进去等于"这只票在界面上叫空白"；
    - 行业留 NULL：公开源没有行业归属，界面上「热门行业」那条策略会因此**如实**标出
      "没有行业表"，而不是"随便分个行业"（编一个行业会让筛选结果静默出错）。
    """
    storage.init_db(cfg.db_path)
    rows = [
        {"symbol": "600519", "name": "贵州茅台"},
        {"symbol": "000001", "name": "   "},          # 空白名 → 不写
        {"symbol": "", "name": "没有代码"},            # 没代码 → 不写
        {"symbol": "920000", "name": "安徽凤凰"},
    ]
    with storage.connect(cfg.db_path) as conn:
        assert ps.sync_names_public(conn, rows) == 2
        got = {r["symbol"]: r for r in conn.execute(
            "SELECT symbol, name, industry FROM stock_basic")}
        assert set(got) == {"600519", "920000"}
        assert got["920000"]["name"] == "安徽凤凰"
        assert all(r["industry"] is None for r in got.values())
        # 一条都没有 → 不碰库（不能让"没有名字"变成"清空 <code>stock_basic</code>"）
        assert ps.sync_names_public(conn, []) == 0
        assert conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0] == 2


def test_market_date_reads_the_real_index_timestamp() -> None:
    """`market_date()` 从**上证指数的时间戳**读"最近交易日"，不用系统日期。

    理由（模块注释）：周末与节假日上证指数的时间戳停在上一交易日，这正是我们要的；
    用系统日期会把周六的日更换写成一根周六的假 K 线。夹具是真实响应
    （`20260917161402` → 2026-09-17），所以这条断言是有据可查的。
    """
    raw = (FIXTURES / "tencent_indices.txt").read_bytes()
    calls: list[str] = []

    def _get(url: str, headers: dict, timeout: float) -> bytes:
        calls.append(url)
        return raw

    assert ps.market_date(opener=_get) == "2026-09-17"
    # 必须按**指数**代码取（`000001` 同时是平安银行，猜错就取到股价）
    assert calls and all("sh000001" in url for url in calls)


def test_market_date_returns_none_without_a_usable_timestamp() -> None:
    """时间戳缺失/格式不对 → **None**（调用方会退成"今天"，并在日志里说清楚）。

    编一个日期比返回 None 危险得多：日更会把 K 线写到错误的那一天，
    而那一天在库里看起来完全正常。
    """
    junk = 'v_sh000001="1~上证指数~000001~3875.60~3891.60~3877.00";'.encode("gbk")
    assert ps.market_date(opener=lambda url, headers, timeout: junk) is None
    assert ps.market_date(opener=lambda url, headers, timeout: b"") is None


# ════════════════════════════════════════════════════════════════════════
# F. `sync.daily_update()` 的分派（有 Key / 没 Key）
# ════════════════════════════════════════════════════════════════════════


def _no_hithink_key(monkeypatch: pytest.MonkeyPatch, message: str = "没有配置 Key") -> None:
    """让"没有同花顺 Key"成为前置条件：构造客户端就抛（与真实行为一致）。"""
    def _raise(cfg=None, **kwargs):
        raise hx.HithinkAuthError(2002, message)

    monkeypatch.setattr(sync, "make_client", _raise)


def test_daily_update_falls_back_to_the_public_source_without_a_key(cfg, monkeypatch) -> None:
    """没有同花顺 Key 时，日更改走免 Key 公开源**兜底**，并且真的写进了当天行情。

    这是"不填 Key 也能用"的命门：用户装上 exe 不填任何 Key，日更必须能落数据，否则
    库里永远是空的、连当天的行情都看不到。

    ⚠️ 但"能日更"**不等于**"能选股"（2026-09-18 用户核实后要求把这句话说准）：选股要过
    数据闸门，而闸门的两条硬条件（`adjust_event` 非空、行业覆盖 ≥90%）只有同花顺那条路
    满足得了 —— 所以这里**不许**写成"攒够历史就能选股"。这条界线由
    `test_public_source_data_can_never_pass_the_stock_picking_gate` 单独钉住。
    所以这里断言的不只是"走了哪条路"，而是"库里真的多了当天那一根"。
    """
    rows = _rows()
    _no_hithink_key(monkeypatch)
    _patch_snapshot(monkeypatch, rows)
    _patch_market_date(monkeypatch)

    results = sync.daily_update(cfg)

    result = _only_daily(results)
    assert result.ok is True, result.error
    assert "免 Key" in result.stage and "公开源" in result.stage
    assert result.rows == len(_writable(rows))
    assert "没有配置 Key" not in result.message

    with storage.connect(cfg.db_path) as conn:
        written = conn.execute("SELECT COUNT(*) FROM stock_daily_raw WHERE date = ?",
                              (TODAY,)).fetchone()[0]
        pool = conn.execute("SELECT COUNT(*) FROM limit_up_pool WHERE date = ?",
                            (TODAY,)).fetchone()[0]
        calendar = conn.execute("SELECT COUNT(*) FROM trading_calendar WHERE source = 'public'"
                                ).fetchone()[0]
    assert written == result.rows > 0
    assert pool > 0
    assert calendar == 1


def test_daily_update_without_a_key_is_not_a_plain_failure(cfg, monkeypatch) -> None:
    """旧行为（没 Key 就返回一条"日更失败"、数据一行不写）**必须已经不存在**。

    这是本次改版的核心：改版前 `daily_update()` 在构造客户端失败时直接 `return [失败]`，
    分发版用户于是永远没有数据。这条用例把旧行为的两个特征都钉死：
    结果是**成功**的，而且 `rows > 0`（旧行为下这两个都不可能成立）。
    """
    _no_hithink_key(monkeypatch, "未配置 Key")
    _patch_snapshot(monkeypatch, _rows())
    _patch_market_date(monkeypatch)

    results = sync.daily_update(cfg)

    result = _only_daily(results)
    assert result.ok is True, result.error
    assert result.error == ""
    assert result.rows > 0
    assert [r.error for r in results if not r.ok] == []
    assert "日更失败" not in result.message


def test_daily_update_keeps_the_hithink_path_when_a_key_client_is_injected(cfg, monkeypatch) -> None:
    """有 Key（注入了客户端）时**绝不碰公开源** —— 同花顺是主源，公开源只是兜底。

    2026-09-18 用户把主次换回同花顺之后，这条断言的份量更重了：有 Key 就该一路走
    同花顺（dump 全市场历史 + 涨停池），公开源只在"没 Key / Key 失效"时才被用到 ——
    要是两条路同时跑，同一份数据会被两个来源各写一遍（复权因子的基准还会互相打架）。
    分叉的代价这个项目已经踩过：界面走一套、命令行走另一套，两个来源的口径慢慢漂移。
    所以这里在公开源的入口上放一个炸弹，并断言同花顺那一路真的被走了（假客户端收到
    了 `download_dump` 调用）。
    """
    def _bomb(*args, **kwargs):
        raise AssertionError("有 Key 时不该碰公开源")

    monkeypatch.setattr(pm, "cached_scan", _bomb)
    monkeypatch.setattr(pm, "cached_symbols", _bomb)
    monkeypatch.setattr(ps, "market_date", _bomb)
    monkeypatch.setattr(ps, "daily_update_public", _bomb)

    client = FakeClient(fail_with=RuntimeError("测试假客户端：故意失败（不联网）"))
    results = sync.daily_update(cfg, client=client)

    assert results and results[0].stage == "日更数据"
    assert not any("免 Key" in r.stage for r in results)
    assert not any("公开源" in (r.stage + r.detail) for r in results)
    assert any(name == "download_dump" for name, _arg in client.calls)
    # 假客户端是"故意失败"的：这条用例测的是**路径**，不是成败
    assert all(isinstance(r, sync.SyncResult) for r in results)


def test_the_public_daily_update_is_exactly_one_scan(cfg, monkeypatch) -> None:
    """免 Key 日更的取数只有**一趟全市场快照**，任何别的地方都不许再外呼。

    这条不是"性能优化"的洁癖：公开源是**未授权**接口，一次多余的请求就是一次被限流的
    机会（全量扫描本身已经是 56 个请求）。所以把行情层的两个入口（`pq.snapshot` /
    `pq.batch_quotes`）换成炸弹、把"最近交易日"固定下来，断言它们一次都没被碰到 ——
    谁将来"顺手补一次指数"就会在这里当场变红。
    """
    def _bomb(*args, **kwargs):
        raise AssertionError("免 Key 日更不该调用行情层：取数只有那一趟全市场快照")

    scans: list[bool] = []
    _no_hithink_key(monkeypatch)
    _patch_snapshot(monkeypatch, _rows(), seen=scans)
    _patch_market_date(monkeypatch)
    monkeypatch.setattr(pm.pq, "snapshot", _bomb)
    monkeypatch.setattr(pm.pq, "batch_quotes", _bomb)

    result = _only_daily(sync.daily_update(cfg))

    assert result.ok is True, result.error
    assert scans == [True]                        # 有且只有一趟（且是强制重扫）
    assert result.rows == len(_writable(_rows()))


# ── 回归：四个"错得很安静"的坑（2026-09-18 修，逐条都有实测复现）──


def test_rerunning_the_same_day_does_not_multiply_the_factor_again(tmp_path) -> None:
    """**同一天重跑日更，复权因子不许再乘一次**（这个 bug 一旦发生就不可逆）。

    触发场景很日常：连点两次【刷新数据】、按钮与定时任务撞在一起、或者**开盘前**跑日更
    （那时 `market_date()` 还是上一个交易日，`day` 正好等于库里最后一根）。

    当时的实现拿"库里最近一根"当除权基准，而重跑时最近一根**就是今天自己** ——
    于是"今天的正常涨跌"被当成一次除权，因子被反复乘大：
    实测重跑两次 1.0 → 1.1004 → 1.2108（后复权价一天比一天高，而且改不回来）。
    这条用例钉的就是"再跑多少次都一样"。
    """
    import json
    from pathlib import Path

    import dataclasses
    from laoa_trader.config import Config
    from laoa_trader.data import storage

    rows = [r for r in json.loads(
        (Path(__file__).parent / "fixtures" / "public_market" / "scan_rows.json")
        .read_text(encoding="utf-8")) if r["symbol"] == "000504"]
    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            ("000504", "2026-09-16", 8.20, 8.30, 8.18, rows[0]["prev_close"],
             1e6, 1e7, 1.0)])
        results = []
        for _ in range(3):
            bars, stat = ps.bars_from_snapshot(rows, conn, "2026-09-17")
            storage.write_daily_raw(conn, bars)
            results.append((bars[0][8], stat["ex_right"]))

    factors = [factor for factor, _ in results]
    assert factors == [pytest.approx(1.0)] * 3, f"因子被反复乘大了：{factors}"
    assert all(ex == 0 for _, ex in results), "重跑不该报出任何除权除息"


def test_normal_daily_update_reports_no_gap(tmp_path) -> None:
    """**连续日更不该报缺口**（否则每天都会弹一句"5000 多只票中间缺天"）。

    缺口判据原来是"最后一根 != 今天"，而正常日更时最后一根永远是昨天 ——
    于是全市场每一只票每天都被算成缺口，提示变成噪音（实测 17/17）。
    现在跟**交易日历里的上一个交易日**比：库里那根 >= 上一个交易日，就不算缺。
    """
    import dataclasses
    import json
    from pathlib import Path

    from laoa_trader.config import Config
    from laoa_trader.data import storage

    rows = [r for r in json.loads(
        (Path(__file__).parent / "fixtures" / "public_market" / "scan_rows.json")
        .read_text(encoding="utf-8")) if r.get("volume") and r.get("last_price") is not None]
    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            (r["symbol"], "2026-09-16", r["open"], r["high"], r["low"],
             r["prev_close"], 1e6, 1e7, 1.0) for r in rows])
        storage.write_calendar(conn, ["2026-09-16", "2026-09-17"], source="test")
        bars, stat = ps.bars_from_snapshot(rows, conn, "2026-09-17")

    assert len(bars) == len(rows)
    assert stat["gap"] == 0, "昨天就在库里，不该被算成缺口"


def test_a_real_gap_is_still_reported(tmp_path) -> None:
    """真的缺天必须**报出来**（修上一个 bug 时最怕的就是顺手把提示也关掉）。

    库里最后一根停在 3 个交易日之前 → 每一只票都是缺口。
    """
    import dataclasses
    import json
    from pathlib import Path

    from laoa_trader.config import Config
    from laoa_trader.data import storage

    rows = [r for r in json.loads(
        (Path(__file__).parent / "fixtures" / "public_market" / "scan_rows.json")
        .read_text(encoding="utf-8")) if r.get("volume") and r.get("last_price") is not None]
    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_daily_raw(conn, [
            (r["symbol"], "2026-09-11", r["open"], r["high"], r["low"],
             r["prev_close"], 1e6, 1e7, 1.0) for r in rows])
        storage.write_calendar(conn, ["2026-09-11", "2026-09-14", "2026-09-15",
                                      "2026-09-16", "2026-09-17"], source="test")
        _bars, stat = ps.bars_from_snapshot(rows, conn, "2026-09-17")

    assert stat["gap"] == len(rows)


def test_name_sync_never_wipes_an_existing_industry(tmp_path) -> None:
    """免 Key 的日更只有"代码 + 名称"，**不许把已有的行业归属清空**。

    为什么这很危险：`write_stock_basic` 的普通分支会把 `industry=None` 一起 upsert，
    于是"曾经配过 Key、同步过行业"的用户一旦落到公开源日更，5000 多只的行业归属
    被静默清空，而 `pool.py` 的"只看热门行业"会跟着静默失效（那是一条 `if hot:`
    的直接跳过，界面上没有任何提示）。所以这条路必须走 `names_only`。
    """
    import dataclasses

    from laoa_trader.config import Config
    from laoa_trader.data import storage

    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600519", "贵州茅台", "白酒")])
        written = ps.sync_names_public(conn, [{"symbol": "600519", "name": "贵州茅台"}])
        row = conn.execute(
            "SELECT name, industry FROM stock_basic WHERE symbol='600519'").fetchone()

    assert written == 1
    assert row["name"] == "贵州茅台"
    assert row["industry"] == "白酒", "行业归属被清空了"


def test_missing_prev_close_is_not_an_ex_right_event() -> None:
    """昨收缺失 / 为 0 → **不是除权**（否则说明文字会谎报"已重标因子"）。

    因子在这些情况下本来就是 1.0（不变），但早先的实现把 `changed` 报成 True，
    于是日更的结论里会写"检测到 N 只除权除息并已重标因子" —— 数字对、说明错。
    """
    assert ps.detect_ex_right(10.0, 0.0) == (False, 1.0)
    assert ps.detect_ex_right(10.0, None) == (False, 1.0)
    assert ps.detect_ex_right(None, 10.0) == (False, 1.0)
    assert ps.detect_ex_right(10.0, 10.0) == (False, 1.0)
    # 真的除权（前收 10 元、交易所昨收 9.5 元）仍然要认出来
    changed, k = ps.detect_ex_right(10.0, 9.5)
    assert changed is True and k == pytest.approx(10.0 / 9.5)


def test_public_source_data_can_never_pass_the_stock_picking_gate(cfg, monkeypatch) -> None:
    """免 Key（只有公开源）**永远过不了数据闸门** —— "能日更"和"能选股"是两件事。

    为什么必须把这条钉死（2026-09-18 用户核实后要求）：日更成功（`ok is True`、写了几千行）
    太容易被读成"这台机器可以选股了"，于是注释、提示、文档里就会出现"没 Key 也能用，
    攒够历史就能选股"这种**做不到的承诺**。产品事实是：自检的就绪条件里有两条硬条件 ——
      - `adjust_event` 非空（`preflight.check`：缺了就是"后复权价会算错，不能算就绪"）；
      - 行业覆盖 ≥ `MIN_INDUSTRY_COVERAGE`（90%）。
    而 `storage.write_adjust_events()` 全项目**只**被 `sync.py` 那条同花顺 dump 路调用，
    行业归属（`sync_industry`）也只有那条路给得到 —— 公开源这两样都给不了。
    所以：没 Key 时行情 / 大盘概览 / 每日增量照常，**选股是设计上就跑不起来的**。

    这条用例同时是"假承诺"的守门人：谁把闸门放宽成"有行情就放行"，或者把公开源写成
    "攒够历史就能选股"，这里会当场变红（而不是等到用户配不出票来才发现）。
    """
    from laoa_trader.data import preflight
    from laoa_trader.scheduler import data_gate

    rows = _rows()
    _patch_snapshot(monkeypatch, rows)
    _patch_market_date(monkeypatch)

    result = _only_daily(ps.daily_update_public(cfg))
    assert result.ok is True, result.error
    assert result.rows > 0                                   # 当天行情真的写进去了
    assert result.extra["stock_basic"] > 0                   # 代码/名称也写了
    assert result.extra["calendar"] == 1                     # 交易日历也写了一天

    # 跨度 / 只数那两道门槛放低：否则第一个拦住它的是"历史跨度不足（要求 ≥0.4 年）"，
    # 就测不到真正说明问题的两条硬条件（复权事件、行业归属）
    cfg.min_history_years = 0.0
    cfg.min_symbols = 1

    with storage.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adjust_event").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM stock_basic WHERE industry IS NOT NULL AND industry != ''"
        ).fetchone()[0] == 0

    check = preflight.check(cfg.db_path, cfg)
    assert check["status"] == preflight.NEEDS_FULL
    assert check["has_adjust_events"] is False
    assert "复权事件" in check["reason"]          # 拒绝理由必须说清"为什么不能选股"

    gate = data_gate(cfg)
    assert gate["ok"] is False                   # 闸门真的拦下 → 策略一次都不会跑
    assert "复权事件" in gate["message"]

    # 再给它一次机会：手工补一条复权事件（**模拟同花顺那条 dump 路写进来的东西**，
    # 本机只有它会写 adjust_event）→ 仍然被拦，这次的理由换成行业归属
    with storage.connect(cfg.db_path) as conn:
        storage.write_adjust_events(conn, [("000504", TODAY, 0.0, 0.0, 0.0, 0.0)])
    check2 = preflight.check(cfg.db_path, cfg)
    assert check2["has_adjust_events"] is True   # 前置条件真的补上了（否则下面的断言不成立）
    assert check2["status"] == preflight.NEEDS_FULL
    assert "行业归属" in check2["reason"]
    gate2 = data_gate(cfg)
    assert gate2["ok"] is False
    assert "行业归属" in gate2["message"]
