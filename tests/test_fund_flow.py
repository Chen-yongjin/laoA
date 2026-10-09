"""资金流：**只对自选标的采集**（东方财富公开接口），供公式里的主力资金字段。

覆盖四段，每段都钉着一条"写错就静默错"的地方：

1. **取数**（`eastmoney.fund_flow_history`）：列序、单位（净额是**元**、占比是**百分数**）、
   升序、失败只表现为空列表。列序不是照文档抄的 —— 用两条算术核对（大单+超大单 = 主力净额、
   五类净额相加 ≈ 0）反推出来的，见 `_parse_fund_flow` 的说明；
2. **落库**（`storage.write/load/latest_fund_flow`）：幂等、升序、天数上限、老库自动补表；
3. **采集**（`scheduler.sync_watchlist_fund_flow`）：名单 = **自选标的的全部**
   （含停用的），别的票一只都不问；自选为空时**一个请求都不发**；单只失败不影响其余；
   接进日更之后**绝不让日更失败**；
4. **公式**（`formulas.fund_flow_extra` + `formula.EXTRA_FIELDS`）：单位是**亿元**、
   `近5日主力净额` 是能取到的那些天之和、**只有自选标的才有值** —— 所以一条
   `主力净额>0` 的公式只会从自选标的里出票，空表时一只都不出且不报错。

一条都不联网：取数全部走注入式 `opener`（固定响应 = `tests/fixtures/eastmoney/fund_flow_600519.json`，
2026-10-08 真实抓的那一份），socket 层还有 conftest 的 `_block_network` 兜底。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from laoa_trader import config as config_mod
from laoa_trader import formulas as lib
from laoa_trader import pool
from laoa_trader import scheduler
from laoa_trader.data import eastmoney as em
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import formula as fm

#: 真实响应快照（2026-10-08 实测 `secid=1.600519&lmt=10`，见交付说明）
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eastmoney"

#: 盘后时刻（用它当"现在"：既不是交易时段、也远晚于合成库的最后一个交易日，
#: 于是口径稳定落在**日 K 线**上 —— 不会因为"测试跑在几点"而变成盘中实时口径）
AFTER_CLOSE = datetime(2026, 9, 11, 20, 0)


def fixture(name: str) -> dict:
    """读一份真实响应 JSON（文件名不含 `.json`）。"""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeOpener:
    """注入式 opener：按顺序吐出预置响应，并记下每次请求的 `(url, params)`。

    响应用完还被打 → 直接断言失败（这正是在测"没有多余的请求"）。
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, params: dict, timeout: float) -> dict:
        self.calls.append((url, dict(params)))
        assert self.responses, f"多打了一个请求：{url} {params}"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def secids(self) -> list[str]:
        return [str(params.get("secid")) for _url, params in self.calls]


def klines_response(*rows: str) -> dict:
    """造一个 `fflow/daykline` 形状的信封（`klines` 是**字符串数组**）。"""
    return {"rc": 0, "data": {"code": "600519", "market": 1, "name": "贵州茅台",
                              "klines": list(rows)}}


def fund_flow_response() -> dict:
    """真实那份响应（10 行，贵州茅台）。"""
    return fixture("fund_flow_600519")


def sample_net(main: float, *, pct: float = 3.5, day: str = "2026-10-08") -> str:
    """造一行资金流（只保证"大单 + 超大单 = 主力净额"这条自洽关系）。

    大单给 1/4、超大单给 3/4 —— 具体的拆分不重要，重要的是一条真实端点上也成立的关系：
    两列相加必须等于第 1 列，否则 `_parse_fund_flow` 的哨兵会报警（那是另一条用例在测）。
    """
    big, super_ = main * 0.25, main * 0.75
    return (f"{day},{main},{0.0},{0.0},{big},{super_},{pct},0.0,0.0,0.0,0.0,10.0,1.0,0.0,0.0")


# ── 1) 取数：列序与单位 ──


def test_fund_flow_history_parses_real_response_in_ascending_order() -> None:
    """真实响应 → 升序的 10 行，字段与单位逐个钉住（列序是从算术核对反推的）。"""
    rows = em.fund_flow_history("600519", days=10, opener=FakeOpener(fund_flow_response()))

    assert len(rows) == 10
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)   # 升序
    latest = rows[-1]
    assert latest["date"] == "2026-10-08"
    # 单位：净额是**元**（−126323568.0 = −1.26 亿）、占比是**百分数**（−4.02 = −4.02%）
    assert latest["main_net"] == pytest.approx(-126323568.0)
    assert latest["main_net_pct"] == pytest.approx(-4.02)
    assert latest["super_net"] == pytest.approx(-24317584.0)
    assert latest["big_net"] == pytest.approx(-102005984.0)
    assert latest["close"] == pytest.approx(1255.79)
    assert latest["pct"] == pytest.approx(-0.22)
    # 占比若被当成"小数"读（−0.0402）就会全错：这里钉住量级
    assert all(abs(r["main_net_pct"]) > 0.1 for r in rows if r["main_net_pct"])


def test_fund_flow_history_column_order_matches_the_two_arithmetic_checks() -> None:
    """列序的**独立**核对：直接拿原始字符串算，不经过解析函数。

    两条关系（模块头实测记录第 6 条）：
      * 大单（第 5 列）+ 超大单（第 6 列）= 主力净额（第 2 列）；
      * 小单 + 中单 + 大单 + 超大单 ≈ 0（四舍五入的零头，量级差 1e7 倍以上）。
    单元写成别的顺序（比如把"小单"当主力）时，这两条**都不会**成立。
    """
    for line in fund_flow_response()["data"]["klines"]:
        parts = line.split(",")
        main, small, mid = float(parts[1]), float(parts[2]), float(parts[3])
        big, super_ = float(parts[4]), float(parts[5])
        assert main == pytest.approx(big + super_)
        total = small + mid + big + super_
        assert abs(total) < max(abs(main), 1.0) * 0.01


def test_fund_flow_history_reuses_the_existing_secid_rules() -> None:
    """`secid` 前缀**复用 `to_secid`**（沪 `1.` / 深 `0.` / 北交所也走 `0.`），并带上全部参数。"""
    opener = FakeOpener(klines_response(sample_net(1e8)),
                        klines_response(sample_net(-1e8)),
                        klines_response(sample_net(2e8)))
    for symbol in ("600519", "000001", "920819"):
        em.fund_flow_history(symbol, days=7, opener=opener)

    assert opener.secids == ["1.600519", "0.000001", "0.920819"]
    url, params = opener.calls[0]
    assert url == f"{em.PUSH2HIS}/api/qt/stock/fflow/daykline/get"
    assert params["fields1"] == em.FUND_FLOW_FIELDS1
    assert params["fields2"] == em.FUND_FLOW_FIELDS2
    assert params["klt"] == 101          # 日线
    assert params["lmt"] == 7            # days → lmt


def test_fund_flow_history_keeps_only_the_most_recent_days() -> None:
    """`days` 自己截断（服务端不一定认 `lmt`）：必须留下**最近**那几天，而不是头几天。"""
    all_dates = [line.split(",")[0] for line in fund_flow_response()["data"]["klines"]]
    rows = em.fund_flow_history("600519", days=3, opener=FakeOpener(fund_flow_response()))

    assert [r["date"] for r in rows] == all_dates[-3:]


def test_fund_flow_history_unknown_symbol_sends_no_request() -> None:
    """认不出 secid 的代码**直接跳过、一个请求都不发**（宁可不采，也不张冠李戴）。"""
    opener = FakeOpener()
    assert em.fund_flow_history("510300", opener=opener) == []   # 5 开头的 ETF：不猜交易所
    assert opener.calls == []


def test_fund_flow_history_bad_responses_are_empty_lists() -> None:
    """接口说没有这个标的 / 响应形状不对 → **空列表 + 不抛**（调用方据此跳过这一只）。"""
    bad = [
        {"rc": 100, "data": None},                       # 实测：不存在的 secid 组合
        {"rc": 0, "data": None},
        {"rc": 0, "data": {"klines": None}},
        {"rc": 0, "data": {"klines": "不是数组"}},
        {"rc": 0},                                       # 连 data 都没有
    ]
    for response in bad:
        assert em.fund_flow_history("600519", opener=FakeOpener(response)) == []


def test_fund_flow_history_timeout_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """**真实那一路**的超时 → 空列表（不是把异常抛给界面）。

    这条不走注入 opener，而是把 `urlopen` 换成抛 `TimeoutError` —— 因为它测的正是
    `_urllib_json` 把各种异常统一成 `EastmoneyError` 这一层（注入 opener 时那一层被绕过了）。
    """
    import urllib.request

    def boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert em.fund_flow_history("600519") == []


def test_fund_flow_history_skips_unparsable_lines() -> None:
    """单行坏掉只丢那一行（日期不合法 / 列数不够），其余照收。"""
    rows = em.fund_flow_history("600519", opener=FakeOpener(klines_response(
        sample_net(1e8, day="2026-10-08"),
        "2026-13-45,1,2,3,4,5,6,7,8,9,10,11,12,13,14",     # 日期不合法
        "2026-10-09,1,2,3",                                 # 列数不够
        sample_net(2e8, day="2026-10-12"),
    )))
    assert [r["date"] for r in rows] == ["2026-10-08", "2026-10-12"]


def test_fund_flow_history_keeps_a_row_whose_columns_disagree(log_records) -> None:
    """列序可疑时**只记日志、照样收下那一行**（丢行的症状比数值偏差更难查）。

    造一行"大单+超大单 ≠ 主力净额"（服务端改了列序的样子）：接口给的**主力净额**优先，
    但日志里必须留下一句话 —— 不然列序变了以后，用户拿到的是一串看着合理、其实张冠李戴的数。
    """
    bad = "2026-10-08,-126323568.0,-1.0,-1.0,1.0,1.0,-4.02,0,0,0,0,1255.79,-0.22,0,0"
    rows = em.fund_flow_history("600519", opener=FakeOpener(klines_response(bad)))

    assert len(rows) == 1                                     # 没有丢行
    assert rows[0]["main_net"] == pytest.approx(-126323568.0)  # 用接口给的那一列
    assert any("列序可疑" in record.getMessage() for record in log_records)


def test_main_net_row_units_and_missing_values() -> None:
    """当日主力净额（快照字段）：`f62` 是**元**、`f184` 是**百分数**；取不到是 None（不是 0）。"""
    got = em.main_net_row({"f12": "600519", "f62": 242336816.0, "f184": 9.53})
    assert got == {"main_net": pytest.approx(242336816.0),
                   "main_net_pct": pytest.approx(9.53)}
    # 批量端点没回这两个字段时 → None（编一个 0 出来会让 `主力净额<0` 的条件被误判成成立）
    assert em.main_net_row({"f12": "600519"}) == {"main_net": None, "main_net_pct": None}
    assert em.main_net_row({"f62": "-", "f184": ""}) == {"main_net": None, "main_net_pct": None}


def test_snapshot_requests_carry_main_net_but_the_unified_row_does_not() -> None:
    """`f62/f184` 进了快照请求，但**不进**统一行情口径（`UNIFIED_KEYS` 的键集是契约）。

    为什么不能并进 `normalize_row`：那个字典的键集与 `sources.QUOTE_FIELDS` 是同一份契约
    （`tests/test_eastmoney.py`、`tests/test_public_quotes.py` 都拿 `==` 钉着），
    而腾讯/新浪那两条来源根本给不出主力资金 —— 多两个键会让那条契约与每一个来源适配器
    都得跟着改。所以这两个数只在 `main_net_row()` 这个专门的出口上。
    """
    fields = em.SNAPSHOT_FIELDS.split(",")
    assert "f62" in fields and "f184" in fields

    opener = FakeOpener({"rc": 0, "data": {"total": 1, "diff": [
        row := {"f12": "600519", "f14": "贵州茅台", "f2": 1272.08, "f3": 1.3,
                "f62": 242336816.0, "f184": 9.53},
    ]}})
    rows = em.snapshot(["600519"], opener=opener)
    assert "f62" in opener.calls[0][1]["fields"].split(",")
    # 同一条原始行：`main_net_row` 拿得到当日主力资金（元 / 百分数）
    assert em.main_net_row(row)["main_net"] == pytest.approx(242336816.0)
    assert em.main_net_row(row)["main_net_pct"] == pytest.approx(9.53)
    # 归一化之后仍然是 `UNIFIED_KEYS` 那套键（一个都不多）
    assert set(rows[0]) == set(em.UNIFIED_KEYS)


# ── 2) 落库 ──


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    return storage.connect(storage.init_db(tmp_path / "flow.db"))


def _flow_rows(*specs: tuple[str, str, float], pct: float = 4.56) -> list[dict]:
    """`(日期, 代码, 主力净额元)` → 写入用的行（其余列给一个自洽的值）。"""
    return [
        {"date": day, "symbol": symbol, "main_net": main,
         "main_net_pct": pct, "super_net": main * 0.75, "big_net": main * 0.25,
         "close": 10.0, "pct": 1.0}
        for day, symbol, main in specs
    ]


def test_write_fund_flow_is_idempotent(tmp_path: Path) -> None:
    """同一天同一只票重复写 = **更新那一行**，不新增（幂等），也不删别的行。"""
    conn = _open_db(tmp_path)
    rows = _flow_rows(("2026-10-08", "600519", 1.0e8), ("2026-09-30", "600519", 2.0e8))

    assert storage.write_fund_flow(conn, rows) == 2
    assert storage.write_fund_flow(conn, rows) == 2          # 再写一遍
    assert conn.execute("SELECT COUNT(*) FROM fund_flow").fetchone()[0] == 2

    changed = _flow_rows(("2026-10-08", "600519", 3.0e8), pct=9.99)
    storage.write_fund_flow(conn, changed)
    latest = storage.latest_fund_flow(conn, ["600519"])["600519"]
    assert latest["main_net"] == pytest.approx(3.0e8)
    assert latest["main_net_pct"] == pytest.approx(9.99)
    # 另一天那一行没被动过
    assert conn.execute("SELECT COUNT(*) FROM fund_flow").fetchone()[0] == 2


def test_write_fund_flow_drops_rows_without_symbol_or_date(tmp_path: Path) -> None:
    """缺代码 / 缺日期的行**直接丢掉**（写进一只没有代码的票比少写一行危险得多）。"""
    conn = _open_db(tmp_path)
    written = storage.write_fund_flow(conn, [
        {"date": "2026-10-08", "main_net": 1.0},                 # 没有 symbol
        {"symbol": "600519", "main_net": 1.0},                   # 没有 date
        {"date": "2026-10-08", "symbol": "600519", "main_net": 5.0},
    ])
    assert written == 1
    assert list(storage.latest_fund_flow(conn)) == ["600519"]


def test_load_fund_flow_is_ascending_and_respects_days(tmp_path: Path) -> None:
    """升序返回；`days` 只留**最近** N 条（倒序查、翻正返回）。"""
    conn = _open_db(tmp_path)
    storage.write_fund_flow(conn, _flow_rows(
        ("2026-09-29", "600519", 1.0), ("2026-09-30", "600519", 2.0),
        ("2026-10-08", "600519", 3.0), ("2026-10-09", "600519", 4.0),
    ))

    everything = storage.load_fund_flow(conn, ["600519"])["600519"]
    assert [r["date"] for r in everything] == [
        "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09",
    ]
    recent = storage.load_fund_flow(conn, ["600519"], days=2)["600519"]
    assert [r["date"] for r in recent] == ["2026-10-08", "2026-10-09"]
    assert [r["main_net"] for r in recent] == [3.0, 4.0]


def test_load_fund_flow_symbol_filter_and_empty_list(tmp_path: Path) -> None:
    """`symbols=None` = 表里全部；给了列表就只要那些；**空列表 = 一只都不要**。"""
    conn = _open_db(tmp_path)
    storage.write_fund_flow(conn, _flow_rows(
        ("2026-10-08", "600519", 1.0), ("2026-10-08", "000001", 2.0),
    ))

    assert sorted(storage.load_fund_flow(conn)) == ["000001", "600519"]
    assert list(storage.load_fund_flow(conn, ["000001"])) == ["000001"]
    assert storage.load_fund_flow(conn, []) == {}
    # 表里没有的代码不出现在结果里（不是"键存在但值是空列表"）
    assert storage.load_fund_flow(conn, ["999999"]) == {}


def test_latest_fund_flow_takes_the_newest_row(tmp_path: Path) -> None:
    """`latest_fund_flow` 取每只票最新那一天（tooltip 与「主力净额」字段都用它）。"""
    conn = _open_db(tmp_path)
    storage.write_fund_flow(conn, _flow_rows(
        ("2026-09-30", "600519", 1.0), ("2026-10-08", "600519", 2.0),
        ("2026-10-08", "000001", 3.0),
    ))

    latest = storage.latest_fund_flow(conn, ["600519"])
    assert latest["600519"]["date"] == "2026-10-08"
    assert latest["600519"]["main_net"] == pytest.approx(2.0)
    assert sorted(storage.latest_fund_flow(conn)) == ["000001", "600519"]
    assert storage.latest_fund_flow(conn, ["999999"]) == {}


def test_fund_flow_table_is_added_to_existing_databases(tmp_path: Path) -> None:
    """**老库**（没有这张表）在 `connect()` 时自动补上 —— 不必删库重下行情。

    这一步的关键是 `EXPECTED_TABLES` 里必须写着 `fund_flow`：`connect()` 只有在
    "缺了某张期望的表"时才会整套跑一遍 `CREATE TABLE IF NOT EXISTS`，
    漏登记的话老库永远不会有这张表（症状是"资金流一次都写不进去"）。
    """
    path = storage.init_db(tmp_path / "old.db")
    conn = storage.connect(path)
    conn.execute("DROP TABLE fund_flow")
    conn.commit()
    assert "fund_flow" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    conn.close()

    assert "fund_flow" in storage.EXPECTED_TABLES
    again = storage.connect(path)
    columns = {r[1] for r in again.execute("PRAGMA table_info(fund_flow)")}
    assert columns == {
        "date", "symbol", "main_net", "main_net_pct", "super_net", "big_net",
        "close", "pct", "source", "updated_at",
    }
    # 主键是 (date, symbol)：同一天同一只票只能有一行
    keys = [r[1] for r in again.execute("PRAGMA table_info(fund_flow)") if r[5]]
    assert keys == ["date", "symbol"]


# ── 3) 采集：只对自选标的 ──


def _seed_watchlist(db: str, *symbols: str, disabled: tuple[str, ...] = ()) -> None:
    """把这几只票加进自选（`disabled` 里的那几只 enabled=0）。"""
    with storage.connect(db) as conn:
        for symbol in symbols:
            storage.upsert_watchlist(conn, symbol, name=symbol)
        for symbol in disabled:
            storage.set_watchlist_enabled(conn, symbol, False)


def test_sync_asks_only_watchlist_symbols(db: str, cfg) -> None:
    """库里 6 只票、自选 2 只 → **只对那 2 只**发请求，且都写进了库。

    这条是主人 2026-10-08 的原话（"只按照自选标的来采集资金流"）最直接的落点：
    多问一只票就是多打一次公开接口，而那只票的资金流永远不会被用到。
    """
    _seed_watchlist(db, "600001", "000001")
    opener = FakeOpener(fund_flow_response(), fund_flow_response())

    out = scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert out == {"symbols": 2, "written": 20, "failed": [], "skipped": None}
    assert sorted(opener.secids) == ["0.000001", "1.600001"]
    assert sorted(storage.latest_fund_flow(storage.connect(db))) == ["000001", "600001"]
    # 库里另外 4 只票（策略标的 / 全市场）一只都没有资金流数据
    assert "600002" not in storage.latest_fund_flow(storage.connect(db))


def test_sync_with_empty_watchlist_sends_nothing(db: str, cfg) -> None:
    """自选为空 → **一个请求都不发**（与"没票就不取数"同一条纪律）。"""
    opener = FakeOpener()          # 多打一个请求就会断言失败

    out = scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert opener.calls == []
    assert out["symbols"] == 0 and out["written"] == 0
    assert out["skipped"] == "自选标的为空"
    assert out["failed"] == []


def test_sync_collects_disabled_watchlist_rows_too(db: str, cfg) -> None:
    """**停用的自选也采**：那个开关管的是"要不要盯它"，不是"要不要它的数据"。

    按开关过滤会出现"把监控打开之后公式里的主力资金字段还是缺的"——
    而用户没有任何办法解释为什么（他不会想到字段取决于加自选那一刻有没有开监控）。
    """
    _seed_watchlist(db, "600001", "000001", disabled=("000001",))
    opener = FakeOpener(fund_flow_response(), fund_flow_response())

    out = scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert out["symbols"] == 2
    assert sorted(opener.secids) == ["0.000001", "1.600001"]
    assert sorted(storage.latest_fund_flow(storage.connect(db))) == ["000001", "600001"]


def test_sync_one_failure_does_not_stop_the_others(db: str, cfg) -> None:
    """单只失败**不影响其余**：失败进 `failed`，后面的票照采、照写。

    按 secid 分别给响应（而不是按顺序）：这样断言与"自选表的排序"无关
    —— 排序本身不是这条用例要测的东西。
    """
    _seed_watchlist(db, "600001", "000001", "600002")
    calls: list[str] = []
    by_secid = {
        "1.600001": fund_flow_response(),                 # 正常
        "1.600002": {"rc": 0, "data": {"klines": []}},    # 接口给了空数组
        # 000001 不在表里 → 走下面那个 rc=100（实测：不存在的 secid 组合）
    }

    def opener(url: str, params: dict, timeout: float) -> dict:
        calls.append(str(params["secid"]))
        return by_secid.get(str(params["secid"]), {"rc": 100, "data": None})

    out = scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert out["symbols"] == 3
    assert sorted(calls) == ["0.000001", "1.600001", "1.600002"]
    assert len(out["failed"]) == 2
    assert any("000001" in reason for reason in out["failed"])
    assert any("600002" in reason for reason in out["failed"])
    # 成功的那一只照样写进了库（失败没有被当成"整批失败"）
    assert sorted(storage.latest_fund_flow(storage.connect(db))) == ["600001"]
    assert out["written"] == 10


def test_sync_is_off_when_the_config_says_so(db: str, cfg) -> None:
    """`fund_flow_enabled=false` → 一只都不采、一个请求都不发。"""
    _seed_watchlist(db, "600001")
    cfg.fund_flow_enabled = False
    opener = FakeOpener()

    out = scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert opener.calls == []
    assert out["symbols"] == 0 and out["written"] == 0
    assert out["skipped"] and "fund_flow_enabled" in out["skipped"]


@pytest.mark.parametrize("days, expected", [(7, 7), (0, 10), (-3, 10), (999, 60), ("x", 10)])
def test_sync_days_come_from_config_and_are_clamped(db: str, cfg, days, expected) -> None:
    """采集天数读配置键，并在**发请求之前**再夹一次（1~60）。

    配置层已经夹过一次，这里再夹是因为 `cfg` 对象可能在运行时被直接改
    （界面/测试）—— 而"采 999 天"一旦漏过去就是一个很长很长的请求。
    """
    _seed_watchlist(db, "600001")
    cfg.fund_flow_days = days
    opener = FakeOpener(fund_flow_response())

    scheduler.sync_watchlist_fund_flow(db, cfg, opener=opener, pace=0)

    assert opener.calls[0][1]["lmt"] == expected


def test_run_daily_collects_fund_flow_and_never_fails_on_it(db: str, cfg,
                                                            tmp_path: Path,
                                                            monkeypatch) -> None:
    """日更里接了采集，而且**采集失败绝不让日更失败**（与导出桌面文件同一条纪律）。

    做法：把采集换成"必抛"的实现 → 日更仍然跑完、`errors` 里**一个字都不许有**
    资金流（`errors` 是"这次日更成不成功"的判据，塞进去会让一次成功的筛选被判成失败
    并安排补跑），而失败原因在 `report["fund_flow"]` 里看得见。
    """
    seen: list[str] = []

    def boom(*args, **kwargs):
        seen.append("called")
        raise RuntimeError("接口全挂了")

    monkeypatch.setattr(scheduler, "sync_watchlist_fund_flow", boom)
    report = scheduler.run_daily(cfg, DataEngine(db), notify=False, with_data=False,
                                 export_dir=tmp_path)

    assert seen == ["called"]                       # 建池之后确实调了
    assert not any("资金流" in str(e) for e in report["errors"]), report["errors"]
    assert report["fund_flow"]["failed"] and "接口全挂了" in report["fund_flow"]["failed"][0]


def test_run_daily_reports_the_collection_summary(db: str, cfg, tmp_path: Path,
                                                  monkeypatch) -> None:
    """采集结果进 `report["fund_flow"]`（成功与否都看得见），且不是 `errors`。"""
    monkeypatch.setattr(scheduler, "sync_watchlist_fund_flow",
                        lambda *a, **k: {"symbols": 2, "written": 20,
                                         "failed": ["000001：没有取到资金流数据"],
                                         "skipped": None})
    report = scheduler.run_daily(cfg, DataEngine(db), notify=False, with_data=False,
                                 export_dir=tmp_path)

    assert report["fund_flow"]["written"] == 20
    assert report["errors"] == []


# ── 4) 公式：单位、缺口径、只有自选才有值 ──


def _last_dates(db: str, count: int) -> list[str]:
    """库里最近的 `count` 个交易日（升序）—— 合成库的最后一天是全市场行情日。"""
    with sqlite3.connect(db) as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_daily_raw ORDER BY date DESC LIMIT ?",
            (count,),
        )]
    return sorted(rows)


def _write_flow(db: str, symbol: str, amounts: list[float], *, pct: float = 4.56) -> None:
    """给这只票写最近几天的资金流（日期取库里的最后几天，倒着对上）。"""
    days = _last_dates(db, len(amounts))
    with storage.connect(db) as conn:
        storage.write_fund_flow(conn, [
            {"date": day, "symbol": symbol, "main_net": amount * 1e8,
             "main_net_pct": pct, "super_net": amount * 0.75e8,
             "big_net": amount * 0.25e8, "close": 10.0, "pct": 1.0}
            for day, amount in zip(days, amounts)
        ])


def test_fund_flow_extra_converts_yuan_to_yi(db: str) -> None:
    """**单位**：库里 123456789 **元** → 字段值是 **1.23456789 亿元**（差 1e8 倍的那一处）。"""
    _write_flow(db, "600001", [1.23456789], pct=18.02)

    extra = lib.fund_flow_extra(db)

    assert extra["600001"]["主力净额"] == pytest.approx(1.23456789)
    assert extra["600001"]["主力净占比"] == pytest.approx(18.02)      # 百分数原值
    assert extra["600001"]["近5日主力净额"] == pytest.approx(1.23456789)


def test_fund_flow_extra_sums_the_last_five_days(db: str) -> None:
    """`近5日主力净额` = 最近 5 个交易日之和；**不足 5 天就是能取到的那些天之和**。"""
    _write_flow(db, "600001", [1.0, -0.5, 2.0, 0.25, 0.75, 9.0])   # 6 天，只算最后 5 天
    extra = lib.fund_flow_extra(db)
    assert extra["600001"]["近5日主力净额"] == pytest.approx(-0.5 + 2.0 + 0.25 + 0.75 + 9.0)
    # 最新那一天的「主力净额」是 9.0（不是 6 天之和）
    assert extra["600001"]["主力净额"] == pytest.approx(9.0)

    _write_flow(db, "000001", [0.4, 0.6])                          # 只有 2 天
    short = lib.fund_flow_extra(db)
    assert short["000001"]["近5日主力净额"] == pytest.approx(1.0)   # 不拿 0 补成 5 天


def test_fund_flow_extra_is_empty_for_symbols_without_data(db: str) -> None:
    """表里没有的票**不出现在结果里**（字段缺值 = 条件不成立，绝不编 0）。"""
    _write_flow(db, "600001", [1.0])
    extra = lib.fund_flow_extra(db)
    assert list(extra) == ["600001"]


def test_fund_flow_yi_matches_the_data_layer() -> None:
    """两个"元 → 亿"的常数必须是同一个数（`formulas` 那份是写死的，不许漂）。"""
    assert lib.FUND_FLOW_YI == em.YUAN_TO_YI


def test_fund_flow_days_constants_agree_across_layers() -> None:
    """采集天数的默认值与区间在三个模块里是**同一份数**（配置层 / 取数层 / 采集层）。

    为什么钉这一条：配置层与采集层各收一次口径（写坏的值不该漏到请求里），
    但"两个地方各写一个数"迟早会漂 —— 漂的表现是"改了 `fund_flow_days` 没生效"
    或者"采了 1000 天"，两种都只在用户那边才看得出来。
    """
    assert config_mod.FUND_FLOW_DAYS_DEFAULT == em.FUND_FLOW_DAYS == 10
    assert config_mod.FUND_FLOW_DAYS_RANGE == (1, 60)
    # 采集层直接用配置层那两个常量（不在 scheduler 里再写一份）
    assert scheduler.FUND_FLOW_DAYS_DEFAULT is config_mod.FUND_FLOW_DAYS_DEFAULT
    assert scheduler.FUND_FLOW_DAYS_RANGE is config_mod.FUND_FLOW_DAYS_RANGE


def test_the_three_fields_are_registered() -> None:
    """三个字段登记在 `EXTRA_FIELDS` 里（否则公式一写就报"未知字段"）。"""
    for name in ("主力净额", "主力净占比", "近5日主力净额"):
        assert fm.EXTRA_FIELDS[name] == "num"
        assert name in lib.FUND_FLOW_FIELDS
    assert lib.FUND_FLOW_WINDOW == 5


def test_prepare_inputs_injects_only_when_a_formula_uses_them(db: str, cfg) -> None:
    """用到才读（与快照同一条纪律）：不用这三个字段的公式，`extra` 里一个都没有。"""
    _write_flow(db, "600001", [1.23])

    used = lib.prepare_inputs(
        cfg, db, [fm.compile_formula("主力净额>0")], now=AFTER_CLOSE)
    assert used.extra["600001"]["主力净额"] == pytest.approx(1.23)

    unused = lib.prepare_inputs(cfg, db, [fm.compile_formula("C>0")], now=AFTER_CLOSE)
    assert unused.extra == {}


def test_series_carries_the_fund_flow_value_on_the_last_bar(db: str) -> None:
    """端到端：值铺在**最后一根** K 线上，前面是缺值（与快照字段同一套写法）。

    前面填 NaN 而不是"用今天的值倒推"，是为了让"历史上根本没有这个数"在序列里
    如实体现 —— 谁写了 `MA(主力净额,5)` 会得到全 NaN，而不是一条看着很合理的假均线。
    """
    _write_flow(db, "600001", [1.23])
    series = next(iter(fm.load_series(db, symbols=["600001"],
                                      extra=lib.fund_flow_extra(db))))

    values = series.extra["主力净额"]
    assert len(values) == len(series.date)
    assert values[-1] == pytest.approx(1.23)
    assert all(value != value for value in values[:-1])      # NaN


def test_formula_only_picks_tickets_that_have_fund_flow(db: str, cfg) -> None:
    """**一条 `主力净额>0` 的策略只会选出有资金流数据的票**（= 自选那几只）。

    合成库里 6 只票，只有 2 只写了资金流；其余 4 只字段缺值 → 条件不成立。
    这不是副作用，是"只对自选标的采集"这条口径的直接结果（见 `docs/兼容性.md` 第五节）。
    """
    _write_flow(db, "600001", [1.23])          # 净流入
    _write_flow(db, "000001", [-0.5])          # 净流出（有数据，但条件不成立）

    result = lib.preview_hits(fm.compile_formula("主力净额>0"), db, cfg=cfg,
                             now=AFTER_CLOSE)

    assert [hit["symbol"] for hit in result["hits"]] == ["600001"]
    assert result["errors"] == []
    # 别的票（没有资金流数据）一只都没被误选
    assert "600002" not in {hit["symbol"] for hit in result["hits"]}


def test_formula_selects_nothing_when_the_table_is_empty(db: str, cfg) -> None:
    """**空表**：公式一只都不出、而且**不报错**（缺值 = 条件不成立，不是异常）。"""
    result = lib.preview_hits(fm.compile_formula("主力净额>0 AND 近5日主力净额>0"),
                             db, cfg=cfg, now=AFTER_CLOSE)

    assert result["hits"] == []
    assert result["count"] == 0
    assert result["errors"] == []
    # 也不该给用户一段"取不到数据"的提示：读的是本地表，没有就是没有（不联网、不失败）
    assert not any("资金流" in note for note in result["notes"])


def test_pool_tooltip_line_units_and_empty_state() -> None:
    """行 tooltip 那一行的文案：`主力 +1.23 亿（+4.56%） 2026-10-08`；没有数据说清原因。"""
    line = pool.fund_flow_text({"date": "2026-10-08", "main_net": 123456789.0,
                                "main_net_pct": 4.56})
    assert line == "资金流：主力 +1.23 亿（+4.56%） 2026-10-08"

    assert pool.fund_flow_text(None) == "资金流：—（还没采集，日更时会自动采）"
    # 两个数都是空 → 如实说"没有数"，不写一个 0 出来
    assert pool.fund_flow_text({"date": "2026-10-08"}) == "资金流：—（这一天的数据是空的）"
    # 只有占比时不给一个编出来的净额
    assert pool.fund_flow_text({"date": "2026-10-08", "main_net_pct": -4.02}) == \
        "资金流：主力净占比 -4.02% 2026-10-08"
