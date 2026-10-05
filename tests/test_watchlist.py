"""自选标的并入股票池：豁免热门行业、不占名额、去重、监控、上限、停用、自动补名称。

覆盖需求「自选标的并入股票池，一起实时监控」的全部验收点：
    1. 自选标的**豁免**热门行业过滤；
    2. 自选标的**不占**策略名额；
    3. 既是策略又是自选 → 池里**一行**、来源「策略+自选」；
    4. **策略池为空也必须盯自选**；
    5. `watchlist_max` 超限**提示**而非静默丢弃；
    6. 停用不进池/不监控，重新启用后恢复；
    7. 添加时自动补名称（库里查得到用库里的；查不到允许添加并提示）；
    8. 备注进入盘中提醒文案（`自选（龙头，成本 12.40）`）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader import intraday, pool
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine

from tests._toml import p
from tests.conftest import READY_THRESHOLDS


def _days(count: int = 40) -> list[str]:
    end = datetime(2026, 9, 11).date()
    out: list[str] = []
    cursor = end
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _enable_formulas(monkeypatch, tmp_path, cfg, formulas: dict[str, str]) -> None:
    """把公式目录指到临时目录、写好几条公式、并在配置里**勾上**它们。

    2026-09-18（用户要求）起**候选只来自勾选的公式** —— 内置策略退出了匹配链路，
    所以"想让池子里有票"这件事在测试里也必须走同一条路：写公式文件 → 勾上 → 建池。

    为什么不用打桩（monkeypatch `run_enabled_formulas`）：这条链路的重点正是
    "勾上的公式 → 候选 → 池子"，桩掉中间那一层，万一 `enabled_formulas` 与公式目录
    接错了（环境变量没生效、名字对不上），测试照样绿。小库上的价格是确定的
    （600001 = 3 元 / 600003 = 20 元起步），所以 `C<5` / `C>15` 这类条件选谁是**精确**的。
    """
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in formulas.items():
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setenv("LAOA_TRADER_FORMULAS", str(folder))
    cfg.enabled_formulas = list(formulas)


def _bars(symbol: str, closes: list[float], volumes: list[float]) -> list[tuple]:
    rows = []
    for i, day in enumerate(_days(len(closes))):
        close = float(closes[i])
        volume = float(volumes[i])
        rows.append((symbol, day, close, close, close, close, volume, volume * close))
    return rows


@pytest.fixture()
def wl_db(cfg):
    """合成库：两条策略候选（600001 低价股 / 600003 短期反转）+ 两只"只做自选用"的标的。

    600100（纺织，冷门行业）与 600200（钢铁）成交量极低，**不会被任何策略选中** ——
    这样"自选"与"策略"两条路径在测试里互不干扰，断言才准确。
    """
    storage.init_db(cfg.db_path)
    n = 40
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "低价样本", "银行"),        # 低价股策略会选中
            ("600003", "反转样本", "半导体"),      # 短期反转会选中
            ("600100", "冷门样本", "纺织"),        # 不在热门行业里（用来自选）
            ("600200", "自选二号", "钢铁"),
        ])
        storage.write_daily_raw(
            conn,
            _bars("600001", [3.0] * n, [2e7] * n)
            + _bars("600003", [20.0 * (0.988 ** i) for i in range(n)], [2e7] * n)
            # 自选专用标的：成交量极低（日均成交额 ≈ 80 万 < 3000 万），
            # 因此**任何策略都不会选中它们** —— 测试才能干净地只验证自选标的那条路径
            + _bars("600100", [8.0] * n, [1e5] * n)
            + _bars("600200", [12.0] * n, [1e5] * n),
        )
        storage.write_calendar(conn, days := _days(n))
        # 复权事件非空是"自检就绪"的硬条件之一
        storage.write_adjust_events(conn, [("600001", days[n // 2], 0.1, 0.0, 0.0, 0.0)])
        storage.write_limit_up_pool(conn, [
            (days[-1], "600003", "反转样本", 1, "首板", "09:35:00", "09:35:00",
             8e7, 0, "芯片", 5.0, 10.0, 1e9, 0, 1, "首板", 9e7, 12.0, 0, "hithink", "t"),
        ])
    return cfg


@pytest.fixture()
def engine(wl_db) -> DataEngine:
    return DataEngine(wl_db.db_path)


def _add(cfg, symbol: str, note: str = "", name: str | None = None, enabled: bool = True):
    with storage.connect(cfg.db_path) as conn:
        return storage.upsert_watchlist(conn, symbol, name=name, note=note, enabled=enabled)


# ── 存储层 ──


def test_watchlist_table_and_crud(wl_db) -> None:
    cfg = wl_db
    with storage.connect(cfg.db_path) as conn:
        assert storage.load_watchlist(conn) == []
        row = storage.upsert_watchlist(conn, "600100", name="冷门样本", note="龙头")
        assert row["symbol"] == "600100"
        assert row["enabled"] == 1
        assert row["added_at"]
        # 幂等：重复添加只更新，不产生第二行
        storage.upsert_watchlist(conn, "600100", note="龙头二号")
        rows = storage.load_watchlist(conn)
        assert len(rows) == 1
        assert rows[0]["note"] == "龙头二号"
        assert rows[0]["name"] == "冷门样本"         # 空名字不会冲掉已缓存名称
        # 停用 / 启用
        assert storage.set_watchlist_enabled(conn, "600100", False) is True
        assert storage.load_watchlist(conn, enabled_only=True) == []
        assert len(storage.load_watchlist(conn)) == 1        # 列表里还在
        storage.set_watchlist_enabled(conn, "600100", True)
        assert storage.watchlist_symbols(conn) == ["600100"]
        # 删除
        assert storage.remove_watchlist(conn, "600100") is True
        assert storage.remove_watchlist(conn, "600100") is False
        assert storage.load_watchlist(conn) == []


def test_watchlist_survives_reexisting_db_upgrade(cfg) -> None:
    """老库（没有 watchlist 表）打开时自动补建，不用删库重下数据。"""
    import sqlite3

    cfg.ensure_dirs()
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("CREATE TABLE stock_daily_raw (symbol TEXT, date TEXT)")   # 老库最小痕迹
        conn.commit()
    with storage.connect(cfg.db_path) as conn:      # 打开即应补齐全部表
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "watchlist" in tables
    assert "push_log" in tables


def test_data_summary_counts_enabled_watchlist(wl_db) -> None:
    _add(wl_db, "600100")
    _add(wl_db, "600200", enabled=False)
    with storage.connect(wl_db.db_path) as conn:
        assert storage.data_summary(conn)["watchlist"] == 1


# ── 1) 豁免热门行业过滤 ──


def test_watchlist_and_formula_rows_are_exempt_from_hot_industry_filter(
        engine, wl_db, tmp_path, monkeypatch) -> None:
    """自选标的与**公式候选**都不在热门行业里，也必须留在池子里。

    2026-09-18 起候选只来自勾选的公式：公式标的**故意不过**热门行业收敛
    （条件是用户自己写明的，再被他没看见的行业过滤删掉最莫名其妙），
    所以这条从"策略标的被过滤掉"改成了"公式标的同样豁免"。
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5"})   # 600001（银行）
    # 只把"半导体"当热门：600001 在银行、600100（自选）在纺织，两个都在冷门行业
    monkeypatch.setattr(pool, "hot_industries",
                        lambda db_path, **kw: {"半导体": {"score": 1.0, "limit_up": 1}})
    _add(wl_db, "600100", note="龙头")          # 纺织（冷门）
    rows = pool.build_pool(engine, wl_db, hot_only=True, save=False)
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols                  # 自选豁免热门过滤
    assert "600001" in symbols                  # 公式标的也豁免（2026-09-18 口径）
    cold = [r for r in rows if r["symbol"] == "600100"][0]
    assert cold["source"] == "自选"
    assert cold["note"] == "龙头"
    assert cold["strategy"] == ""               # 不挂公式名


def test_watchlist_not_in_pool_when_disabled_by_config(engine, wl_db) -> None:
    """watchlist_in_pool=false → 自选只记录，不进池。"""
    _add(wl_db, "600100")
    wl_db.watchlist_in_pool = False
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    assert "600100" not in [r["symbol"] for r in rows]


# ── 2) 不占策略名额 ──


def test_watchlist_does_not_consume_strategy_quota(
        engine, wl_db, tmp_path, monkeypatch) -> None:
    """池子大小只限制**公式标的**：公式取满 1 只时，自选标的仍全部在池。

    （2026-09-18 起"策略标的"就是"公式标的" —— 候选只来自勾选的公式。）
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5"})   # 只选 600001
    for symbol in ("600100", "600200"):
        _add(wl_db, symbol)
    rows = pool.build_pool(engine, wl_db, size=1, hot_only=False, save=False)
    strategy_rows = [r for r in rows if r["strategy"]]
    watch_rows = [r for r in rows if not r["strategy"]]
    assert len(strategy_rows) == 1                       # size 只作用于公式标的
    assert {r["symbol"] for r in watch_rows} == {"600100", "600200"}
    assert len(rows) == 3


def test_strategy_rows_still_capped_by_size(engine, wl_db, tmp_path, monkeypatch) -> None:
    """没有自选标的时 size 仍然生效（4 只候选里只留 1 只）。

    2026-09-18 起候选来自勾选的公式：这里用 `C>0` 把库里 4 只都选出来，
    再让 `size=1` 砍到 1 只 —— 断言的是"池子大小对公式标的照样生效"。
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"全选": "C>0"})
    rows = pool.build_pool(engine, wl_db, size=1, hot_only=False, save=False)
    assert len(rows) == 1


def test_strategy_rows_come_first(engine, wl_db, tmp_path, monkeypatch) -> None:
    """顺序：公式标的（按分数降序）在前，纯自选在后。

    （**必须勾一条公式**：不勾的话池子里只有自选标的，"前半段全是策略行"就变成
    空真 —— 那种测试看着绿，其实什么都没验。）
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5", "反转": "C>15"})
    _add(wl_db, "600100")
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    assert rows[-1]["symbol"] == "600100"
    assert rows[:-1], "应该至少有一行公式标的"
    assert all(r["strategy"] for r in rows[:-1])
    assert all(r["strategy"].startswith("公式·") for r in rows[:-1])


# ── 3) 去重 ──


def test_same_symbol_in_both_sources_appears_once(
        engine, wl_db, tmp_path, monkeypatch) -> None:
    """既是公式选中又是自选 → 池里**一行**（内部 `source` 记着两件事，显示只写公式名）。"""
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5"})   # 600001（3 元）
    _add(wl_db, "600001", note="老朋友")
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False)
    hits = [r for r in rows if r["symbol"] == "600001"]
    assert len(hits) == 1
    # `source` 仍是内部分档（公式 + 它在自选里这件事留着）；**显示**那一列只写公式名
    assert hits[0]["source"] == "公式+自选"
    assert pool.source_label(hits[0], {"enabled": 1}) == "低价"
    assert hits[0]["note"] == "老朋友"
    assert hits[0]["strategy"] == "公式·低价"            # 公式信息保留


def test_pool_table_rows_source_labels(engine, wl_db, tmp_path, monkeypatch) -> None:
    """界面/CLI 的「来源」列文本：**哪条公式**（存的是 `公式·低价`，显示 `低价`）/ 自选。

    2026-09-18 起候选只来自勾选的公式，所以这一列的主形态是公式名；
    `X`（内置策略中文名）仍在 `source_label()` 里支持（老池子行/老库还读得到），
    但新选出来的票不会再是它 —— 那条路已经退出匹配链路。
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5"})   # 600001（3 元）
    _add(wl_db, "600001", note="老朋友")     # 公式 + 自选
    _add(wl_db, "600100", note="龙头")       # 纯自选
    pool.build_pool(engine, wl_db, hot_only=False, save=True, day="2026-09-11")
    rows = {r["symbol"]: r for r in pool.pool_table_rows(wl_db.db_path)}
    assert rows["600001"]["source"] == "公式"          # 有策略来源就只报策略（2026-09-23 起）
    assert rows["600001"]["strategy"] == "公式·低价"
    assert rows["600001"]["source_label"] == "低价"        # 显示：不带前缀、也不带「+自选」
    assert rows["600001"]["strategy"] == "公式·低价"             # 数据：库里的值没被动过
    assert rows["600001"]["note"] == "老朋友"
    assert rows["600100"]["source"] == "自选"
    assert rows["600100"]["source_label"] == "自选"
    assert rows["600100"]["note"] == "龙头"
    # 一行 tooltip 的来源明细（界面用的就是这一份）
    # 2026-09-23 起**不再**拼「+自选」：600001 明明在自选里，来源那格也只写公式名
    #（主人：只有用户自己输入的才算自选来源）
    detail = pool.source_detail_lines(rows["600001"])
    assert detail[0] == "来源：低价"
    assert rows["600001"]["source"] == pool.source_kind(rows["600001"], {"enabled": 1})
    # 纯自选的行没有组别那一行（不留一个空壳）
    assert pool.source_detail_lines(rows["600100"]) == ["来源：自选"]


def test_save_pool_persists_watchlist_rows(engine, wl_db) -> None:
    """自选标的要真的落到 stock_pool 里（盘中提醒按池子盯）。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    pool.build_pool(engine, wl_db, hot_only=False, save=True, day="2026-09-11")
    stored = {r["symbol"]: r for r in pool.load_pool(wl_db.db_path)}
    assert "600100" in stored
    assert stored["600100"]["name"] == "冷门样本"
    assert stored["600100"]["strategy"] == ""


# ── 4) 策略池为空也必须盯自选 ──


def test_watch_targets_monitors_watchlist_with_empty_pool(wl_db) -> None:
    """策略池为空（库里没有任何池子）→ 依然盯自选标的。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path)
    assert set(targets) == {"600100"}
    assert pool_symbols == {"600100"}          # 也在"池内买点"的适用范围内
    assert targets["600100"]["source"] == "自选"
    assert targets["600100"]["note"] == "龙头"


def test_watch_targets_ignores_watchlist_when_config_off(wl_db) -> None:
    """watchlist_in_pool=false → 自选只记录不监控。"""
    _add(wl_db, "600100")
    wl_db.watchlist_in_pool = False
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert targets == {} and pool_symbols == set()


# ── 右键【关闭监控】的语义：这只票**不再产生任何盘中提醒** ──


def _hold(cfg, symbol: str, cost: float = 10.0, *, monitored: bool = True) -> None:
    """记一笔持仓，并把监控开关设成指定状态（界面右键【关闭监控】做的就是这件事）。

    显式把开关**设成**目标状态（而不是"只在关的时候设"）：重新打开监控时开关得回到 1，
    否则第二次断言会读到上一轮留下的 0。
    """
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_position(conn, symbol, name=None, avg_cost=cost, reopen=True)
        assert storage.set_position_monitor(conn, symbol, monitored) is True


def test_monitor_off_position_leaves_the_whole_observation_set(wl_db) -> None:
    """**重点**：持仓上关掉监控 → 它从观察面里整体消失，**哪怕它同时是自选**。

    优先级由用户拍板：某只票同时是策略标的或自选时，**以持仓上的监控开关为准** ——
    右键那一行菜单是对"这只票"最具体、最新的一次表态，而"它在池子里"是几天前跑策略
    留下的结果（池子每天重建）。这条用例两边都造出来，然后逐条断言。
    """
    _add(wl_db, "600100", name="冷门样本", note="龙头")      # 既是自选……
    _hold(wl_db, "600100", monitored=True)
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in targets and "600100" in pool_symbols   # 开着监控：照常盯

    _hold(wl_db, "600100", monitored=False)                   # 现在关掉它
    targets, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" not in targets, "关了监控的持仓不该再进观察面（自选身份也不例外）"
    assert "600100" not in pool_symbols
    # 做T那条路（持仓专属）也一起关掉
    assert intraday.held_positions(wl_db.db_path) == {}
    # 竞价与异动共用的"标的宇宙"同样剔掉它（这两路也属于"这只票的提醒"）
    assert "600100" not in intraday.alert_universe(wl_db.db_path, wl_db)

    # 打开监控 → 一切都回来（开关是双向的，不是一次性）
    _hold(wl_db, "600100", monitored=True)
    targets, _pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in targets
    assert "600100" in intraday.alert_universe(wl_db.db_path, wl_db)


def test_monitor_off_position_stays_in_pool_rows_but_not_targets(wl_db, engine) -> None:
    """关监控**不删池子行**（表里照旧看得到它），只是不再盯它。

    用户要的是"别盯它了"，不是"我没有这只票"（那是【删除】）—— 所以池子/表格不受影响，
    受影响的只有"观察面"。
    """
    _add(wl_db, "600100", name="冷门样本")
    pool.build_pool(engine, wl_db, hot_only=False, save=True, day="2026-09-11")
    _hold(wl_db, "600100", monitored=False)
    assert "600100" in {r["symbol"] for r in pool.load_pool(wl_db.db_path)}
    assert "600100" not in intraday.watch_targets(wl_db.db_path, cfg=wl_db)[0]


def test_monitor_off_read_failure_does_not_silence_everything(wl_db, monkeypatch) -> None:
    """读持仓表失败时**不做排除**（宁可多提醒一次，也不能因为读库失败把提醒全关掉）。"""
    def _boom(*_a, **_k):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(intraday.storage, "connect", _boom)
    assert intraday.monitor_off_symbols(wl_db.db_path) == set()


def test_run_daily_with_strategies_off_still_pools_watchlist(wl_db, monkeypatch) -> None:
    """一条公式都没勾（匹配只剩自选标的）→ 池子里只有自选标的，且不报错。

    老口径是 `enabled_groups = ["none"]`（策略全关）；那两个键 2026-09-18 已退役，
    现在决定"有没有公式标的"的只有 `enabled_formulas`（这里故意留空）。
    """
    import laoa_trader.scheduler as sched

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr("laoa_trader.notify.notify_all",
                        lambda *a, **k: {"tray": {"kind": "tray", "ok": True}})
    report = sched.run_daily(wl_db, DataEngine(wl_db.db_path), notify=True,
                             with_data=False)
    assert [r["symbol"] for r in report["pool"]] == ["600100"]
    assert report["picks"] == 0                       # 没跑策略
    assert report["errors"] == []                     # 这是正常用法，不是配置错误
    assert report["pushed"] is True                   # 该提醒还是提醒
    assert report["strategies_off"] is True


def test_run_daily_watchlist_only_mode_pushes_note(wl_db, monkeypatch) -> None:
    """只盯自选时推送文案要带备注（否则用户看不出为什么盯它）。"""
    import laoa_trader.scheduler as sched

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    captured: dict = {}

    def fake_notify(title, lines, kinds=None, cfg=None):
        captured["title"] = title
        captured["lines"] = lines
        return {"tray": {"ok": True}}

    monkeypatch.setattr("laoa_trader.notify.notify_all", fake_notify)
    sched.run_daily(wl_db, DataEngine(wl_db.db_path), notify=True, with_data=False)
    body = "\n".join(captured["lines"])
    assert "冷门样本(600100)自选（龙头）" in body


# ── 5) 上限 ──


def test_watchlist_max_caps_and_reports(engine, wl_db) -> None:
    """超过 watchlist_max：只监控前 N 只，并**明确提示**（不静默丢弃）。"""
    wl_db.watchlist_max = 2
    for symbol in ("600100", "600200", "600001", "600003"):
        _add(wl_db, symbol)
    report: dict = {}
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    watch_rows = [r for r in rows if r.get("source") in ("自选", "策略+自选")]
    assert len(watch_rows) == 2                       # 只纳入前 2 只
    assert report["watchlist"] == 2
    assert report["watchlist_dropped"] == 2
    assert any("超过上限" in w for w in report["warnings"])
    assert "watchlist_max" in report["warnings"][0]


def test_watchlist_max_zero_means_none(engine, wl_db) -> None:
    wl_db.watchlist_max = 0
    _add(wl_db, "600100")
    report: dict = {}
    rows = pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    assert "600100" not in [r["symbol"] for r in rows]
    assert report["watchlist_dropped"] == 1


def test_watchlist_max_ok_when_not_exceeded(engine, wl_db) -> None:
    _add(wl_db, "600100")
    report: dict = {}
    pool.build_pool(engine, wl_db, hot_only=False, save=False, report=report)
    assert report.get("watchlist_dropped") is None
    assert report["watchlist"] == 1


# ── 6) 停用/启用 ──


def test_disabled_watchlist_not_in_pool_nor_watched(wl_db) -> None:
    _add(wl_db, "600100", name="冷门样本")
    _add(wl_db, "600200", name="自选二号")
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", False)
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols and "600200" not in symbols
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in targets and "600200" not in targets


def test_reenabled_watchlist_comes_back(wl_db) -> None:
    _add(wl_db, "600200", name="自选二号")
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", False)
    assert "600200" not in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600200", True)
    assert "600200" in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600200" in targets


# ── 7) 自动补名称 ──


def test_name_autofilled_from_local_db(engine, wl_db) -> None:
    name = engine.get_stock_names(["600100"]).get("600100")
    assert name == "冷门样本"
    row = _add(wl_db, "600100", note="龙头", name=name)
    assert row["name"] == "冷门样本"


def test_unknown_symbol_still_addable(wl_db) -> None:
    """库里查不到名称也允许添加（刚上市/改名），名称留空由后续同步补。"""
    row = _add(wl_db, "601999", note="新股")
    assert row["symbol"] == "601999"
    assert row["name"] is None
    assert row["note"] == "新股"
    assert "601999" in [r["symbol"] for r in pool.build_pool(
        DataEngine(wl_db.db_path), wl_db, hot_only=False, save=False)]


# ── 8) 盘中提醒：参考价与备注 ──


def test_watchlist_reference_price_uses_position_cost(wl_db) -> None:
    """有持仓 → 用**持仓成本**算止损/止盈。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")
    with storage.connect(wl_db.db_path) as conn:
        storage.upsert_position(conn, "600100", name="冷门样本", quantity=1000,
                                avg_cost=12.40)
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    info = targets["600100"]
    assert info["cost"] == pytest.approx(12.40)
    assert info["close"] == pytest.approx(12.40)        # 作为止损/止盈参考价
    assert info["label"] == "自选（龙头，成本 12.40）"
    # 规则层：现价跌破成本 5% → 触发止损
    hits = intraday.evaluate_sell_rules(
        "600100", {"last_price": 11.7},
        {"prev_close": 8.0, "ma5": 8.0}, info["close"], cfg=wl_db,
    )
    assert [h[0] for h in hits] == ["stop_loss"]


def test_watchlist_without_position_uses_prev_close(wl_db) -> None:
    """没有持仓 → 参考价留空，规则层回退到"前一交易日收盘"。"""
    _add(wl_db, "600100", name="冷门样本")
    targets, _ = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    info = targets["600100"]
    assert info["close"] is None and info["cost"] is None
    assert info["label"] == "自选"
    ctx = intraday.history_context(wl_db.db_path, ["600100"])["600100"]
    hits = intraday.evaluate_sell_rules(
        "600100", {"last_price": ctx["prev_close"] * 0.9}, ctx, None, cfg=wl_db,
    )
    kinds = [h[0] for h in hits]
    assert "stop_loss" in kinds          # 参考价确实是"前一交易日收盘"
    # 跌 10% 时同时跌破 5 日线也是正常的（横盘股的 5 日线≈昨收）
    assert set(kinds) <= {"stop_loss", "break_ma5"}


def test_watchlist_alert_text_includes_note(wl_db) -> None:
    """盘中提醒文案带上备注（自选（龙头））。"""
    _add(wl_db, "600100", name="冷门样本", note="龙头")

    class Client:
        def snapshot(self, symbols=None, **kw):
            # 远低于参考价 → 触发止损
            return [{"ticker": "600100", "thscode": "600100.SH", "last_price": 5.0,
                     "price_change_ratio_pct": -40.0, "turnover": 1e9}]

        def limit_up_pool(self, day=None, size=200):
            return []

    alerts = intraday.build_alerts(DataEngine(wl_db.db_path), Client())
    assert alerts
    assert "自选（龙头）" in alerts[0]["name"]
    title, lines = intraday.format_message(alerts, db_path=wl_db.db_path, cfg=wl_db)
    assert "龙头" in "\n".join(lines)
    assert "🛑 触及止损" in "\n".join(lines)


def test_watchlist_gets_pool_buy_rules(wl_db) -> None:
    """自选标的适用「池内回踩买点 / 放量突破20日高」两类买点规则。"""
    _add(wl_db, "600100", name="冷门样本")
    _, pool_symbols = intraday.watch_targets(wl_db.db_path, cfg=wl_db)
    assert "600100" in pool_symbols       # 池内买点（回踩 5 日线）
    ctx = intraday.history_context(wl_db.db_path, ["600100"])["600100"]
    snap = {"last_price": ctx["ma5"] * 1.005, "prev_close": ctx["prev_close"]}
    hits = intraday.evaluate_pool_buy_rules("600100", snap, ctx)
    assert [h[0] for h in hits] == ["pullback_ma5_buy"]


def test_watchlist_alerts_use_same_notify_channels(wl_db, monkeypatch) -> None:
    """自选标的提醒走同一套 notify_channels（不搞特殊分支）。

    2026-09-18：Windows 通知那一路删除，这里只剩飞书与托盘两路。
    """
    from laoa_trader.notify import feishu, notify_all, tray

    _add(wl_db, "600100")
    wl_db.notify_channels = ["tray"]
    called: list[str] = []
    for name, module in (("feishu", feishu), ("tray", tray)):
        monkeypatch.setattr(
            module, "notify",
            lambda title, lines, cfg=None, _n=name, **kw: (
                called.append(_n) or {"kind": _n, "ok": True, "detail": "ok"}
            ),
        )
    results = notify_all("⚡ 盘中提醒", ["自选（龙头）｜…"], cfg=wl_db)
    assert called == ["tray"]                     # 只有配置里的频道真的被调用
    assert results["feishu"]["skipped"] is True   # 其余显示"跳过"（不是失败）
    assert results["tray"]["ok"] is True


# ── 与策略选择的关系 ──


def test_watchlist_kept_even_when_no_formula_is_enabled(wl_db) -> None:
    """一条公式都没勾（候选为空）时，池子里仍要有自选标的。"""
    _add(wl_db, "600100", name="冷门样本")
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=True,
                           save=False, picks={})
    assert [r["symbol"] for r in rows] == ["600100"]


def test_selection_argument_no_longer_filters_anything(
        wl_db, tmp_path, monkeypatch) -> None:
    """`selection` 参数**已经不起作用**（2026-09-18）：匹配只看勾了哪些公式。

    用户把内置策略整体改成随包公式之后，"跑哪些策略"没有第二套答案了 ——
    老调用方可能还在传 `selection`（签名里保留了它），但它不该把公式候选剔掉
    （那会变成"我勾了公式，池子里却没有"这种最难查的现象）。这条把新口径钉住。
    """
    _enable_formulas(monkeypatch, tmp_path, wl_db, {"低价": "C<5"})   # 600001
    _add(wl_db, "600100", name="冷门样本")
    rows = pool.build_pool(DataEngine(wl_db.db_path), wl_db, hot_only=False,
                           save=False, selection=object())
    symbols = [r["symbol"] for r in rows]
    assert "600100" in symbols          # 自选标的照旧不受影响
    assert "600001" in symbols          # 公式候选**也不会**被 selection 剔掉


# ── CLI / 推送文案：策略关闭与"策略+自选"标注 ──


def test_cli_once_without_formulas_pools_watchlist(wl_db, tmp_path, capsys, monkeypatch) -> None:
    """`--once` 没有任何公式时不该报错退出，而要只处理自选标的。

    2026-09-18（用户要求）：候选只来自勾选的公式 —— `enabled_groups=["none"]`
    （老口径的"策略全关"）已经不影响匹配了，真正决定"有没有公式标的"的是
    `enabled_formulas`。所以这条改成"一条公式都没勾"，行为与老口径一致：
    退出码 0、自选标的照常进池、状态栏能看出这一轮只有自选。
    """
    import laoa_trader.scheduler as sched
    from laoa_trader.__main__ import cli

    _add(wl_db, "600100", name="冷门样本", note="龙头")
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(wl_db.data_dir)}"\nhithink_api_key = ""\n'
        'enabled_groups = ["none"]\nnotify_channels = []\n' + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "没有勾选任何策略" in out
    assert "冷门样本(600100)" in out
    assert "没有启用任何策略" not in out


def test_cli_once_ignores_a_typo_in_the_config_groups(wl_db, tmp_path, capsys,
                                                     monkeypatch) -> None:
    """老配置里的退役键（组名拼错也一样）**不再拦路** —— 匹配只看勾了哪些公式。

    2026-09-18 口径：`enabled_groups` / `enabled_strategies` 已经从配置里删掉，
    `load_config()` 把它们当**未知键**忽略（用户文件里的其它内容一字不动）。
    所以配置文件里写了个不存在的组名，这轮匹配照跑（只盯自选标的），
    不该像老口径那样直接退出 1 —— 那会让人以为"匹配坏了"。
    """
    import laoa_trader.scheduler as sched
    from laoa_trader.__main__ import cli

    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(wl_db.data_dir)}"\nhithink_api_key = ""\n'
        'enabled_groups = ["nope"]\nnotify_channels = []\n' + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "没有勾选任何策略" in out
    assert "未知策略组" not in out          # 配置里的键不再解析，也就没有这条报错


def test_push_lines_mark_strategy_plus_watchlist(wl_db) -> None:
    """推送正文：纯自选带「自选（备注）」，策略选中的那只只写**策略名**（+备注）。

    用公式合成名当策略名（2026-09-18 起池子里只有公式标的与自选），
    中文名走 `legacy.strategy_label`：认不出的合成名原样显示 —— 正是要的。
    """
    both = pool.format_pool_lines([{
        "name": "低价样本", "symbol": "600001", "strategies": "公式·低价",
        "source": "公式+自选", "note": "老朋友", "reason": "低价",
    }])
    # 2026-09-23 起不写「+自选」：正文与股池表来源列、桌面文件同一个词
    assert both == ["1. 低价样本(600001)低价（老朋友）｜低价"]

    only_watch = pool.format_pool_lines([{
        "name": "冷门样本", "symbol": "600100", "strategies": "",
        "source": "自选", "note": "龙头", "reason": "自选（龙头）",
    }])
    assert only_watch == ["1. 冷门样本(600100)自选（龙头）｜自选（龙头）"]

    # 纯策略标的：格式与服务器版一致（不带任何自选字样）
    plain = pool.format_pool_lines([{
        "name": "半导体甲", "symbol": "600002", "strategies": "LowPriceStrategy",
        "source": "策略", "reason": "低价股",
    }])
    assert plain == ["1. 半导体甲(600002)低价股｜低价股"]


# ══════════════════════════════════════════════════════════════════════════
# 9) 自选表里的「来源」：从结果页加入自选的票要记得**当初是哪条公式选出来的**
#    （2026-09-21 主人实报："新版本从匹配列表加入自选的票到股池里的来源都变成自选了"）
# ══════════════════════════════════════════════════════════════════════════
#
# 根因：加入自选时只把来源写进了**备注**（`匹配来源：公式·X`），而「自选标的」那一列
# 读的是 `watchlist_only_rows()` —— 那里对不在池子里的自选行**写死**成「自选」。
# 结果：票只要不在今天的池子里（从结果页刚加的票就是这种），来源列永远显示「自选」。
#
# 修法：给自选表加一列 `source_strategy`（写法与 `stock_pool.strategy` 一致），
# 加入时写进去；显示时**照旧走 `source_label()` / `source_kind()`**（不另拼一套）。
# 老数据没有这一列：先从备注里的 `匹配来源：X` 认一次，认不出就当没有 → 显示「自选」。


def _watch_add(cfg, symbol: str, *, name: str = "", note: str = "",
               source_strategy: str = "") -> None:
    """加一只自选（模拟界面两条路：手工【添加自选】/ 结果页【加入自选】）。"""
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, symbol, name=name or None, note=note,
                                 source_strategy=source_strategy or None)


def _page_rows(cfg) -> dict[str, dict]:
    return {r["symbol"]: r for r in pool.pool_page_rows(cfg.db_path)}


def test_watchlist_source_is_persisted_and_shown_as_the_plain_strategy(wl_db) -> None:
    """从结果页加入自选的票：股池那一列写**策略名本身**（不是「自选」、也不带「+自选」）。

    2026-09-23 主人："为什么要+自选 什么策略跑出来的 直接记录策略名称
    只有用户自己输入的才能算自选来源" —— 所以哪怕它同时在自选表里，来源列也只写策略名。
    """
    _watch_add(wl_db, "600100", name="冷门样本",
               note="匹配来源：公式·尾盘超短策略",
               source_strategy="公式·尾盘超短策略")

    row = _page_rows(wl_db)["600100"]

    assert row["source_label"] == "尾盘超短策略"
    assert row["source"] == "公式"               # 与池子行同一套 `source_kind()`
    assert row["strategy"] == "公式·尾盘超短策略"
    # 行 tooltip 的来源明细也是同一个词（界面不自己拼一套）
    assert pool.source_detail_lines(row)[0] == "来源：尾盘超短策略"


def test_manually_added_watchlist_row_still_says_only_self_selected(wl_db) -> None:
    """**手工**加的票（没有来源）仍然只显示「自选」—— 不瞎猜、也不硬塞一个来源。"""
    _watch_add(wl_db, "600100", name="冷门样本", note="龙头")

    row = _page_rows(wl_db)["600100"]

    assert row["source_label"] == "自选"
    assert row["source"] == "自选"
    assert row["strategy"] == ""


def test_old_watchlist_row_recovers_the_source_from_the_note(wl_db) -> None:
    """老数据：来源写在备注里（`匹配来源：短期反转`）→ 认出来并原样显示策略名。

    这一条救的是"升级之前加的自选"：那时还没有 `source_strategy` 这一列。
    备注里的 `公式·` / `策略·` 前缀要去掉再当策略名用。
    """
    _watch_add(wl_db, "600200", name="自选二号", note="匹配来源：短期反转")

    row = _page_rows(wl_db)["600200"]

    assert row["strategy"] == "短期反转"             # 去掉前缀的那一份
    assert row["source_label"] == "短期反转"         # 显示：就是策略名本身
    assert row["source"] == "策略"


def test_old_watchlist_row_without_any_source_shows_self_selected(wl_db) -> None:
    """最老的那批数据：备注里也没有来源 → 显示「自选」，**不许崩、不许瞎猜**。

    备注是用户自己的字段（"龙头""消息面"），随便写的那些字不该被当成来源。
    """
    _watch_add(wl_db, "600100", name="冷门样本", note="龙头")

    assert _page_rows(wl_db)["600100"]["source_label"] == "自选"
    # 备注里出现了"来源"两个字但**不是**那个前缀 → 一样不当来源
    _watch_add(wl_db, "600200", name="自选二号", note="消息面来源不明")
    assert _page_rows(wl_db)["600200"]["source_label"] == "自选"


def test_disabled_row_keeps_both_the_source_and_the_disabled_mark(wl_db) -> None:
    """停用的自选照样显示：来源是策略名、状态用括号标出来 —— `尾盘超短策略（已停用）`。"""
    _watch_add(wl_db, "600100", name="冷门样本",
               source_strategy="公式·尾盘超短策略")
    with storage.connect(wl_db.db_path) as conn:
        storage.set_watchlist_enabled(conn, "600100", False)

    assert _page_rows(wl_db)["600100"]["source_label"] == "尾盘超短策略（已停用）"


def test_upsert_keeps_the_first_source_and_price(wl_db) -> None:
    """重复加入 **不改写**已经记下来的来源与加入价（"当初为什么在这"才有意义）。

    这一条与 `added_price` 是同一条口径（`COALESCE` 只补空值）。
    """
    _watch_add(wl_db, "600100", name="冷门样本", source_strategy="公式·A")
    with storage.connect(wl_db.db_path) as conn:
        storage.upsert_watchlist(conn, "600100", source_strategy="公式·B", price=9.9)
        row = storage.watchlist_map(conn)["600100"]

    assert row["source_strategy"] == "公式·A"


def test_fill_watchlist_source_only_fills_empty_and_touches_nothing_else(wl_db) -> None:
    """`fill_watchlist_source()`：只补空来源，**不重新启用、不动加入价/备注**。

    为什么需要它：用户对一个**已经在自选里**的票点【加入自选】时，界面说的是
    "没有重复添加、也没改你的备注"—— 那就不能顺手把他停用的票启用回来；
    但"这只是哪条公式选的"这条信息又该记下来（老数据里它是空的）。
    """
    with storage.connect(wl_db.db_path) as conn:
        storage.upsert_watchlist(conn, "600100", name="冷门样本", note="龙头",
                                 price=5.0, enabled=False)
        assert storage.fill_watchlist_source(conn, "600100", "公式·尾盘超短策略") is True
        row = storage.watchlist_map(conn)["600100"]

        assert row["source_strategy"] == "公式·尾盘超短策略"
        assert row["enabled"] == 0                       # 停用状态没被改
        assert row["note"] == "龙头"                     # 用户写的备注没被改
        assert float(row["added_price"]) == pytest.approx(5.0)

        # 已有来源 → 不再改写；不存在的票 → False（都不是错误）
        assert storage.fill_watchlist_source(conn, "600100", "公式·另一条") is False
        assert storage.watchlist_map(conn)["600100"]["source_strategy"] == "公式·尾盘超短策略"
        assert storage.fill_watchlist_source(conn, "999999", "公式·X") is False
        # 空来源 → 什么都不做（不会把已有的值清掉）
        assert storage.fill_watchlist_source(conn, "600100", "") is False


def test_old_database_gets_the_source_column_added(tmp_path) -> None:
    """老库升级：`connect()` 会把缺的列补上（与 `added_price` 同一条迁移路径）。

    判据不只看"列在不在"，还要看**老行读出来是什么**：老库补的列是 NULL →
    界面显示「自选」（`watchlist_source_strategy()` 返回空串），不是报错。

    造"升级前"的库用的是**旧版建表语句**（没有 `source_strategy` 那一列），
    而不是 `ALTER TABLE … DROP COLUMN` —— 后者要 SQLite 3.35+，而 Windows 上
    打包/CI 用的 SQLite 版本不由我们定（这条用例不该因为环境而红），
    而且"旧版建的库"本来就长这样，测得更真。
    """
    import sqlite3

    db = tmp_path / "old-trader.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE watchlist ("
            "  symbol TEXT PRIMARY KEY, name TEXT, note TEXT,"
            "  enabled INTEGER NOT NULL DEFAULT 1, added_at TEXT, added_price REAL"
            ")"
        )
        conn.execute(
            "INSERT INTO watchlist (symbol, name, note, enabled, added_at) "
            "VALUES ('600100', '冷门样本', '龙头', 1, '2026-09-01T09:30:00')"
        )
        conn.commit()

    with storage.connect(db) as conn:                          # 这一下 = 走迁移
        columns = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)")}
        rows = storage.load_watchlist(conn, enabled_only=False)

    assert "source_strategy" in columns
    assert rows[0]["source_strategy"] is None
    # 老数据（这一列是 NULL）在界面上显示「自选」—— 不崩、不瞎猜
    assert pool.watchlist_source_strategy(rows[0]) == ""
    assert pool.watchlist_source_fields(rows[0])["source_label"] == "自选"


def test_push_and_pool_table_use_the_same_source_word(wl_db) -> None:
    """推送正文与股池表**同一个词**：加了自选之后两边都还是那条公式名。"""
    _watch_add(wl_db, "600100", name="冷门样本", note="匹配来源：公式·尾盘超短策略",
               source_strategy="公式·尾盘超短策略")

    table_label = _page_rows(wl_db)["600100"]["source_label"]
    with storage.connect(wl_db.db_path) as conn:
        entries = storage.load_watchlist(conn)
    merged = pool.merge_watchlist(engine, [], settings=wl_db, watchlist=entries)
    lines = pool.format_pool_lines(merged)

    assert table_label == "尾盘超短策略"          # 在自选里也不加「+自选」
    assert lines and lines[0].startswith("1. 冷门样本(600100)尾盘超短策略")
    # 桌面文件那一列也是同一个词（`pick_export_text` 走 `_export_source`）
    text = pool.pick_export_text([{**merged[0], "source_label": table_label}])
    assert "来源：尾盘超短策略" in text
    assert "+自选" not in text
