"""「策略选取」勾的那几条 = 真正参与筛选的那几条（主人实报："没按我选的策略来"）。

主人 2026-10-12 的原话："策略选股里点选指定策略后，没有按照用户选取的策略进行
（不知道什么逻辑）。" 真机日志给出的证据是：

    16:12  策略筛选：首板缩量整理 5 只 → 策略标的合成完成：3 只
           → 股票池合成完成：8 只（策略 8 只 / 自选 0 只） → 股票池已写入 5 只

即 **这一轮只选出 3 只，结果页却写 8 只**。多出来的是并进池子的**自选标的行**
（它们带着"当初是哪条策略选的" `watchlist.source_strategy`，看着就像别的策略的票）。
主人这次给的口径是："**勾了哪条，就看到哪条的票**" —— 同一交易日内，
把"**当前没勾**的策略写进去的行"清掉，只留这一轮/当前勾选集合选出来的票，
**用户手工加的自选标的行必须保留**（`来源=自选` 那种）。

这一组用例把这条链逐环钉死：

1. 只勾一条 → 池子里只有这条策略的票（另一条的票一只都不许出现）；
2. **同一交易日两轮**：第一轮勾 A、第二轮只勾 B → A 的行被清掉、B 的票留下、
   手工加的自选行还在（票本身仍留在自选表里，用户数据不动）；
3. 上一轮留在**今天**池子里的、由别的策略写下去的行必须被清掉（含"这一轮一行都不用写"
   的那种轮次）；
4. 结果页说的是"**本次筛选**选出了哪几只"，并写清"池内共 M 只"差在哪；
5. 勾选 → `cfg.enabled_formulas` 与**落盘内容**立刻同步（界面写盘的就地改 cfg，
   【开始筛选】用的就是同一个对象，两者不可能错开）。

全程离线、只读本地合成库（`tests/conftest.py` 已在 socket 层封死网络）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from laoa_trader import pool as pool_mod
from laoa_trader import scheduler as sched
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import formula_group

#: 两条策略**各自唯一命中**的票（价格是构造出来的，命中集合是确定的）：
#: `低价` = `C<5` 选中 5 只低价票；`反转` = `C>10` 只选中那一只跌到 12.4 元的。
A_NAME, B_NAME = "低价", "反转"
LOW = ["600001", "600002", "600003", "600004", "600005"]      # 3.0 元 → 只有 `C<5` 命中
MID = "600006"                                                # 8.0 元 → 两条都不命中
HIGH = "600007"                                               # ≈12.4 元 → 只有 `C>10` 命中


def _days(n: int = 40) -> list[str]:
    end = datetime(2026, 9, 11).date()
    out: list[str] = []
    cursor = end
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    out.sort()
    return out


def _ready_db(cfg):
    """造一份能精确断言"哪条策略选了哪只票"的行情库。"""
    days = _days()
    n = len(days)
    plan = {
        **{symbol: (3.0, 0.0) for symbol in LOW},
        MID: (8.0, 0.0),
        HIGH: (20.0, -0.012),          # 每天跌 1.2% → 40 天后约 12.4 元
    }
    names = {symbol: f"低价样本{i}" for i, symbol in enumerate(LOW, start=1)}
    names[MID] = "中间样本"
    names[HIGH] = "反转样本"

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(
            conn, [(s, names[s], "银行" if s in LOW else "半导体") for s in plan]
        )
        rows = []
        for symbol, (base, drift) in plan.items():
            price = base
            for day in days:
                price *= (1 + drift)
                rows.append((symbol, day, price, price, price, price,
                             2e7, 2e7 * price))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
    assert n > 30
    return cfg


def _write_formulas(tmp_path, monkeypatch, cfg, formulas: dict[str, str]) -> None:
    """写策略文件并把「策略选取」勾成 `formulas` 的键（= `enabled_formulas`）。"""
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in formulas.items():
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setenv("LUWEIK_FORMULAS", str(folder))
    cfg.enabled_formulas = list(formulas)


def _run(cfg, monkeypatch):
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    return sched.run_daily(cfg, DataEngine(cfg.db_path), notify=False, with_data=False)


def _pool_rows(cfg, day):
    with storage.connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT symbol, strategy FROM stock_pool WHERE date = ?", (day,))]


# ══════════════════════════════════════════════════════════════════════════
# 1) 只勾一条 → 池子里只有这条策略的票
# ══════════════════════════════════════════════════════════════════════════


def test_only_the_checked_strategy_picks(cfg, tmp_path, monkeypatch) -> None:
    """两条策略都在目录里，只勾「低价」→ 池子里只有「低价」的票。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    cfg.enabled_formulas = [A_NAME]

    report = _run(cfg, monkeypatch)

    assert {r["symbol"] for r in report["pool"]} == set(LOW[:3])   # 每条策略最多 3 只
    assert {r["strategy"] for r in report["pool"]} == {"公式·低价"}
    assert report["formulas"]["ran"] == [A_NAME]                   # 另一条一次都没跑


def test_build_pool_direct_call_follows_the_checked_one(cfg, tmp_path, monkeypatch) -> None:
    """直接调 `build_pool`（定时任务/CLI 那条路）也只看勾的那几条。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    cfg.enabled_formulas = [B_NAME]

    rows = pool_mod.build_pool(DataEngine(cfg.db_path), cfg)

    assert [r["symbol"] for r in rows] == [HIGH]
    assert {r["strategy"] for r in rows} == {"公式·反转"}


# ══════════════════════════════════════════════════════════════════════════
# 2) 同一交易日两轮：勾了哪条就看到哪条（主人 2026-10-12 的口径）
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节的场景照真机日志摆：池子里除了"这一轮选出来的"，还并着**自选标的行** ——
# 其中一类是用户早先从结果页加进来的（带着"当初是哪条策略选的"，`source_strategy`），
# 另一类是纯手工加的（`来源=自选`，没有任何策略名）。


def _watch_pick(cfg, symbol: str, strategy: str, name: str = "") -> None:
    """上一轮从结果页点【加入自选】并且打开了监控的那只票。"""
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, symbol, name=name or symbol,
                                 source_strategy=strategy, enabled=True)


def _watch_manual(cfg, symbol: str, name: str = "") -> None:
    """用户**手工**加的自选标的（没有策略来源 → 「来源」列显示「自选」）。"""
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, symbol, name=name or symbol, enabled=True)


def test_source_column_follows_the_round_that_just_ran(
        cfg, tmp_path, monkeypatch) -> None:
    """同一交易日两轮：**「来源」列跟着这一轮走**（主人 2026-10-12："策略名当然改成新的"）。

    场景照真机日志摆：用户早先从 A 的结果页加了 5 只自选并开着监控，另外手工加了一只。
    * 第一轮只勾 A → 那 5 只的来源是 **A 现在的名字**（这一轮真选中了它）；
    * 第二轮只勾 B → 这 5 只这一轮**没被任何勾选的策略选中** → 来源写 **`自选`**
      （不再显示"当初是 A 选的"），而 B 选出来的那只由结果页说 B。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    # 上一轮（或更早）用户从 A 的结果页加了 5 只自选并开着监控；另外手工加了一只
    for symbol in LOW:
        _watch_pick(cfg, symbol, formula_group.formula_strategy_name(A_NAME))
    _watch_manual(cfg, MID)

    # ── 第一轮：只勾 A ──
    cfg.enabled_formulas = [A_NAME]
    first = _run(cfg, monkeypatch)
    day = first["data_date"]
    assert {r["strategy"] for r in first["pool"]} == {"公式·低价", ""}   # "" = 手工自选那只
    # 池子 = 这一轮选出来的（A 的 3 只，与自选去重后并成一行）+ 并进来的自选标的
    assert {r["symbol"] for r in first["pool"]} == set(LOW) | {MID}
    # 落库的是自选标的行（`save_picks=False`）：A 的票 + 手工那只
    assert {r["symbol"] for r in _pool_rows(cfg, day)} == set(LOW) | {MID}
    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    for symbol in LOW:
        assert page[symbol]["source_label"] == "低价"        # 这一轮 A 选中了它
    assert page[MID]["source_label"] == "自选"

    # ── 第二轮：只勾 B ──
    cfg.enabled_formulas = [B_NAME]
    second = _run(cfg, monkeypatch)

    # ① 池子（= 结果页/桌面文件读的那一份）里只剩 B 的票 + 自选标的行
    assert {r["symbol"] for r in second["pool"]} == set(LOW) | {MID, HIGH}
    assert {r["strategy"] for r in second["pool"] if r["symbol"] != HIGH} == {""}

    # ② 库里（今天这一批）没有「低价」的行了：这一轮写下去的自选行都是**没有策略名**的
    left = _pool_rows(cfg, day)
    assert all("低价" not in str(r["strategy"] or "") for r in left), left

    # ③ 「自选标的」页那一列：这 5 只这一轮没被选中 → `自选`（不再是 A 的名字）
    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    for symbol in LOW:
        assert page[symbol]["source_label"] == "自选", page[symbol]
        assert page[symbol]["strategy"] == ""
    assert page[MID]["source_label"] == "自选"

    # ④ 用户数据**一个都没少**：那 5 只票还在自选表里，库里那个历史字段也原样留着
    with storage.connect(cfg.db_path) as conn:
        entries = {r["symbol"]: r for r in storage.load_watchlist(conn)}
    assert set(LOW) | {MID} <= set(entries)
    assert entries[LOW[0]]["source_strategy"] == "公式·低价"     # 不动用户数据


def test_checked_strategy_rows_survive_the_next_round(cfg, tmp_path, monkeypatch) -> None:
    """第二轮仍然勾着的那条策略的行**必须留着**（清的是"没勾的策略"，不是所有旧行）。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    _watch_pick(cfg, LOW[0], formula_group.formula_strategy_name(A_NAME))

    cfg.enabled_formulas = [A_NAME, B_NAME]
    first = _run(cfg, monkeypatch)
    day = first["data_date"]
    assert {r["symbol"] for r in _pool_rows(cfg, day)} == {LOW[0]}

    # 第二轮：A 仍然勾着、B 也勾上 → A 的那一行照旧在
    cfg.enabled_formulas = [A_NAME, B_NAME]
    second = _run(cfg, monkeypatch)

    assert LOW[0] in {r["symbol"] for r in second["pool"]}
    assert {r["symbol"] for r in _pool_rows(cfg, day)} == {LOW[0]}


# ══════════════════════════════════════════════════════════════════════════
# 2b) 「来源」列：**这一轮谁选中了它**（主人 2026-10-12："策略名当然改成新的"）
# ══════════════════════════════════════════════════════════════════════════
#
# 那一列的判据从"历史来源"（`watchlist.source_strategy`：当初是哪条策略选的）
# 换成了"**当前这一轮勾选集合里真的选中了它的那条**"，没有就是「自选」。
# "这一轮选出了什么"读 `signal` 台账（`pool.round_pick_strategies()`）—— 建池结果
# 不落库，台账是唯一记得住它的地方。


def test_source_column_shows_the_strategy_that_picked_it_this_round(
        cfg, tmp_path, monkeypatch) -> None:
    """① 先被 A 选中加进自选（旧来源 A）→ 这轮只勾 B 且 B 也选中它 → 显示 **B**。

    这只票**没开监控**（2026-10-05 起加自选默认不提醒）→ 它不在池子里，
    所以这一条只有"读台账"才答得对：池子行里根本没有它。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    # 当初是「低价」选出来加进自选的（库里记着旧来源），但**没开监控**
    with storage.connect(cfg.db_path) as conn:
        storage.upsert_watchlist(conn, HIGH, name="反转样本", enabled=False,
                                 source_strategy=formula_group.formula_strategy_name(A_NAME))

    cfg.enabled_formulas = [B_NAME]
    report = _run(cfg, monkeypatch)
    assert [r["symbol"] for r in report["pool"]] == [HIGH]       # 这一轮 B 选中了它
    assert report["pool"][0]["strategy"] == "公式·反转"

    row = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}[HIGH]
    assert row["source_label"] == "反转（已停用）"               # ← B **现在的名字**
    assert row["strategy"] == "公式·反转"
    # 库里那个历史字段一个字节都没动（口径改了也不动用户数据）
    with storage.connect(cfg.db_path) as conn:
        assert (storage.watchlist_map(conn)[HIGH]["source_strategy"]
                == "公式·低价")


def test_source_column_falls_back_to_self_selected(cfg, tmp_path, monkeypatch) -> None:
    """② 这轮**没有任何**勾选的策略选中它 → 显示 `自选`（不管库里那个旧名是什么）。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    # MID（8 元）哪条公式都不命中；A 只选 LOW*、B 只选 HIGH
    _watch_pick(cfg, MID, formula_group.formula_strategy_name(A_NAME))

    cfg.enabled_formulas = [A_NAME]
    _run(cfg, monkeypatch)

    row = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}[MID]
    assert row["source_label"] == "自选"
    assert row["strategy"] == ""


def test_source_column_with_a_deleted_strategy_name_in_old_data(
        cfg, tmp_path, monkeypatch) -> None:
    """③ 老数据：库里存着**已经删掉的那条策略**（`LowPriceStrategy`）→ 显示 `自选`、不许崩。

    那批内置策略 2026-09-18 就删掉了，用户现在根本选不出来它 —— 拿它当来源显示就是
    "我没勾的策略的票怎么在这儿"。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5"})
    for symbol in (LOW[0], MID):
        _watch_pick(cfg, symbol, "LowPriceStrategy")      # 已删除的内置策略名（老数据）

    cfg.enabled_formulas = [A_NAME]
    report = _run(cfg, monkeypatch)

    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    assert page[MID]["source_label"] == "自选"             # 不是「低价股」
    assert page[LOW[0]]["source_label"] == "低价"           # 这一轮 A 真选中了它 → 说 A
    # 池子行里也不该留下那个删掉的策略名（建池按"当前勾选集合"清）
    left = _pool_rows(cfg, report["data_date"])
    assert all(r["strategy"] != "LowPriceStrategy" for r in left), left


# ══════════════════════════════════════════════════════════════════════════
# 3) 上一轮别的策略留在**今天池子里**的行必须被清掉
# ══════════════════════════════════════════════════════════════════════════


def test_previous_round_rows_of_another_strategy_are_cleared(
        cfg, tmp_path, monkeypatch) -> None:
    """库里今天那批行，来源是**当前没勾**的策略 → 这一轮必须清掉。

    为什么残留会留到今天：2026-09-21 起筛选结果不再自动进股池（`save_picks=False`），
    于是"这一轮一行都不用写"（没勾公式 / 自选都没开监控）成了**常态** ——
    那一轮以前连清理一起跳过，残留就永远躺在那儿。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})

    # 先跑一轮把"今天"（行情日）定下来，再往今天的池子里放一行**别的策略**写的行
    first = _run(cfg, monkeypatch)
    day = first["data_date"]
    pool_mod.save_pool(cfg.db_path, [
        {"symbol": HIGH, "name": "反转样本", "strategy": "公式·旧策略",
         "strategies": "公式·旧策略", "score": 3.0, "reason": "上一轮留下的"},
    ], day)
    assert [r["symbol"] for r in _pool_rows(cfg, day)] == [HIGH]

    # 这一轮：只勾「低价」，而且这一轮**没有任何自选标的要落库**
    cfg.enabled_formulas = [A_NAME]
    second = _run(cfg, monkeypatch)
    assert {r["symbol"] for r in second["pool"]} == set(LOW[:3])

    # ① 库里（今天这一批）只剩这一轮真正写下去的行 —— 旧策略那一行没了
    left = _pool_rows(cfg, day)
    assert all(r["strategy"] != "公式·旧策略" for r in left), left

    # ② 「自选标的」页看到的那一批里也不许再有它（这一页就是主人嘴里的"结果"）
    page = pool_mod.pool_page_rows(cfg.db_path)
    assert HIGH not in [r["symbol"] for r in page], page


def test_previous_round_row_is_cleared_even_when_the_pool_is_empty(
        cfg, tmp_path, monkeypatch) -> None:
    """这一轮池子完全是空的（一条都没勾、也没自选）时，残留照样要清。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5"})
    first = _run(cfg, monkeypatch)
    day = first["data_date"]
    pool_mod.save_pool(cfg.db_path, [
        {"symbol": HIGH, "name": "反转样本", "strategy": "公式·旧策略",
         "strategies": "公式·旧策略", "score": 3.0, "reason": "上一轮留下的"},
    ], day)

    cfg.enabled_formulas = []                     # 一条策略都不勾 = 只盯自选（默认状态）
    report = _run(cfg, monkeypatch)

    assert report["pool"] == []
    assert _pool_rows(cfg, day) == []


# ══════════════════════════════════════════════════════════════════════════
# 4) 结果页说"本次筛选选出了哪几只"，并写清与池子的大小差在哪
# ══════════════════════════════════════════════════════════════════════════


def test_result_page_counts_only_this_rounds_picks(cfg, tmp_path, monkeypatch) -> None:
    """结果页 = **这一轮选出来的**那几只；池子里并进来的自选标的**不能混进去**。

    真机日志里的数字（本轮 3 只 / 池 8 只）就是这么来的：池子是"选出来的票 + 自选标的"
    合起来的，只按"有没有 strategy"判会把自选标的也算成"本次筛选结果" ——
    现在结果页只认 `picked` 标记，并在结论里把"池内共 M 只"说清楚。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pytest

    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过结果页用例")
    from PySide6.QtWidgets import QApplication

    from laoa_trader.ui import formula_page as fp

    app = QApplication.instance() or QApplication([])
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    _watch_pick(cfg, LOW[0], formula_group.formula_strategy_name(A_NAME))
    _watch_manual(cfg, MID)

    cfg.enabled_formulas = [B_NAME]
    report = _run(cfg, monkeypatch)
    # 池子 = 这一轮选出来的（HIGH）+ 并进来的自选标的（LOW[0] 与手工那只）
    assert {r["symbol"] for r in report["pool"]} == {LOW[0], MID, HIGH}

    page = fp.FormulaPage(cfg, directory=tmp_path / "formulas")
    try:
        page.show_pick_result(report, data_date=report.get("data_date"))
        app.processEvents()

        assert [r["symbol"] for r in page.result_rows] == [HIGH]    # 只列选出来的
        hint = page.result_hint.text()
        assert "本次筛选结果：共 1 只" in hint
        assert "池内共 3 只" in hint and "自选标的" in hint          # 差在哪说清楚
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_result_page_falls_back_for_old_callers(page=None) -> None:
    """老调用方（report/行列表里没有 `picked` 标记）照旧按"有策略来源的行"列 —— 行为不变。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pytest

    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过结果页用例")
    from PySide6.QtWidgets import QApplication

    from laoa_trader.ui import formula_page as fp

    app = QApplication.instance() or QApplication([])
    widget = fp.FormulaPage.__new__(fp.FormulaPage)          # 只测这一个静态方法
    rows = [
        {"symbol": "600001", "strategy": "公式·甲"},
        {"symbol": "600009", "watchlist": True},              # 纯自选行
    ]
    assert [r["symbol"] for r in widget._pick_rows(rows)] == ["600001"]
    # 有标记时**只认标记**（池子里并进来的自选标的一律不算）
    marked = [{"symbol": "600001", "strategy": "公式·甲", "picked": True},
              {"symbol": "600009", "strategy": "公式·乙"}]
    assert [r["symbol"] for r in widget._pick_rows(marked)] == ["600001"]


# ══════════════════════════════════════════════════════════════════════════
# 5) 勾选 → cfg 与落盘内容**立刻**同步（界面与实跑不可能错开）
# ══════════════════════════════════════════════════════════════════════════


def test_toggle_keeps_cfg_and_config_file_in_sync(cfg, tmp_path, caplog) -> None:
    """在「策略选取」列勾一条：`cfg.enabled_formulas` 与 config.toml 立刻都是它。

    `ui/app.py` 的【开始筛选】用的是 `self.cfg`（不重新读盘），所以"界面上勾上去了、
    实跑却按旧配置"这件事只可能发生在"写盘时没就地改 cfg"上 —— 这条用例两头都钉。

    顺带钉住"名字对不上"那条路**看得见**：config.toml 里写着一条目录里没有的策略名时，
    `enabled_names()` 忽略它（日志里有一句 warning），而界面上列表上方那一行提示也写着
    是哪几条 —— 否则用户只会看到"我勾了它，却一只票都没有"。
    """
    import logging
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pytest

    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过勾选框用例")
    from PySide6.QtWidgets import QApplication

    from laoa_trader import formulas as lib
    from laoa_trader.ui import formula_page as fp

    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("甲", "乙"):
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\nC>0\n", encoding="utf-8")

    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("# 用户自己的注释（保存设置后必须还在）\n", encoding="utf-8")
    page = fp.FormulaPage(cfg, directory=folder)
    page.show()
    try:
        page._row_boxes["甲"].setChecked(True)
        app.processEvents()

        assert cfg.enabled_formulas == ["甲"]
        assert 'enabled_formulas = ["甲"]' in cfg.source_path.read_text(encoding="utf-8")
        assert "# 用户自己的注释（保存设置后必须还在）" in \
            cfg.source_path.read_text(encoding="utf-8")

        # 再勾第二条、然后取消第一条：两边都要跟着走（不是"只写第一次"）
        page._row_boxes["乙"].setChecked(True)
        app.processEvents()
        page._row_boxes["甲"].setChecked(False)
        app.processEvents()

        assert cfg.enabled_formulas == ["乙"]
        assert 'enabled_formulas = ["乙"]' in cfg.source_path.read_text(encoding="utf-8")
        assert page._row_boxes["甲"].isChecked() is False

        # 自定义文件与注释头名字不一样时，勾的是**文件里写的那个名字**（= 列表显示的名字），
        # `enabled_names()` 也就是按它匹配的 —— 两边同口径，不会"勾了却不生效"。
        (folder / "文件名.txt").write_text(
            "# 名称: 注释头名字\n# 说明: t\nC>0\n", encoding="utf-8")
        cfg.enabled_formulas = ["注释头名字"]
        assert lib.enabled_names(cfg, folder) == ["注释头名字"]
        with caplog.at_level(logging.WARNING):
            # 反过来按**文件名**去勾目录里没有的那个名字 → 忽略，并在界面上说清楚
            cfg.enabled_formulas = ["文件名"]
            assert lib.enabled_names(cfg, folder) == []
            page.reload()
            app.processEvents()
        assert "目录里没有这条策略" in caplog.text, caplog.text
        assert page.list_hint.isVisible() is True
        assert "文件名" in page.list_hint.text()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


# ══════════════════════════════════════════════════════════════════════════
# 6) 同一天跑两轮、勾的策略不同：「来源」只认**后一轮**
# ══════════════════════════════════════════════════════════════════════════


def test_source_column_ignores_the_previous_round_of_the_same_day(
        cfg, tmp_path, monkeypatch) -> None:
    """勾 A 跑一轮、再改勾 B 跑一轮（同一个行情日）→ 「来源」里不许再出现 A。

    主人 2026-10-13 实报：「我选的策略是尾盘匹配策略，他选出来的是首板缩量整理」。
    根因不在筛选（那一轮确实只跑了勾上的那条），而在**台账**：`signal` 表的冲突键是
    `(signal_date, strategy, symbol)`，同一交易日的几轮**各存各的行**；
    而盘中每跑一轮写的都是**同一个** signal_date（当晚日更之前行情日一直是上一个交易日）。
    「来源」列读台账时不认"最后一次写入的那一批"，就把上一轮那条没勾的策略显示成"这一轮选的"。

    不变式：一轮 = 一次 `storage.write_signals()` = 整批同一个 `run_batch`（显式批次号）。
    这里**故意不把时钟分开**（两轮在同一秒里跑完）—— 判据要是靠 `created_at` 猜，
    这条用例就会红。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})

    # ── 第一轮：勾 A（低价），跑完把其中一只加进自选（用户常这么干）──
    cfg.enabled_formulas = [A_NAME]
    first = _run(cfg, monkeypatch)
    assert first["formulas"]["ran"] == [A_NAME]
    _watch_pick(cfg, LOW[0], formula_group.formula_strategy_name(A_NAME))

    # ── 第二轮：改成只勾 B（反转），同一个行情日 ──
    cfg.enabled_formulas = [B_NAME]
    second = _run(cfg, monkeypatch)
    assert second["data_date"] == first["data_date"], "两轮必须在同一个行情日才复现得出来"
    assert second["formulas"]["ran"] == [B_NAME]

    # 台账里的"这一轮"只剩 B 的票 —— A 那一批**不许**再出现在来源里
    picks = pool_mod.round_pick_strategies(cfg.db_path)
    assert picks == {HIGH: formula_group.formula_strategy_name(B_NAME)}
    assert formula_group.formula_strategy_name(A_NAME) not in picks.values()

    # 「自选标的」页：上一轮 A 选中的那只票 → 来源写「自选」，不冒充这一轮的结果
    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    assert page[LOW[0]]["source_label"] == "自选"
    assert page[LOW[0]]["strategy"] == ""


def test_source_column_still_follows_the_latest_round_for_its_own_picks(
        cfg, tmp_path, monkeypatch) -> None:
    """反过来：**这一轮**选出来的票照旧写它的策略名（收紧台账不许把真话也一起丢了）。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    cfg.enabled_formulas = [A_NAME]
    _run(cfg, monkeypatch)
    _watch_pick(cfg, LOW[1], formula_group.formula_strategy_name(A_NAME))

    # 第二轮还是勾 A：它这一轮又选中了 LOW[1]（票在自选里，台账里也有它）
    _run(cfg, monkeypatch)

    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    assert page[LOW[1]]["source_label"] == "低价"
    assert page[LOW[1]]["strategy"] == formula_group.formula_strategy_name(A_NAME)


def test_zero_hit_strategy_says_why_when_snapshot_is_unavailable(
        cfg, tmp_path, monkeypatch) -> None:
    """勾了却一只都没出的策略：要留下"为什么是 0"（`zero_notes`），界面照原话显示。

    主人 2026-10-13 的现场就是这一条：`尾盘匹配策略` 要求流通市值 30~500 亿、换手率 >3%，
    而这两个数来自**实时快照** —— 非交易时段取不到 ⇒ 用到它们的条件一律不成立 ⇒ 0 只。
    同一轮里 `首板缩量整理` 只用本地日线，照常出 3 只，于是"我选的是 A，出来的是 B"。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {
        A_NAME: "C<5",                                    # 只用本地日线 → 照常出票
        "尾盘": "流通市值>=30 AND C>=3",                    # 要实时快照 → 这一轮必然 0 只
    })
    cfg.enabled_formulas = [A_NAME, "尾盘"]

    report = _run(cfg, monkeypatch)

    formulas = report["formulas"]
    assert formulas["picks"].get(formula_group.formula_strategy_name(A_NAME))
    assert formulas["picks"].get(formula_group.formula_strategy_name("尾盘"), 0) == 0
    note = formulas["zero_notes"].get(formula_group.formula_strategy_name("尾盘"), "")
    assert "流通市值" in note and "实时快照" in note
    assert formula_group.formula_strategy_name(A_NAME) not in formulas["zero_notes"]


# ══════════════════════════════════════════════════════════════════════════
# 7) 勾了几条就只跑几条：开跑前按 config.toml 校准（主人 2026-10-14 的硬要求）
# ══════════════════════════════════════════════════════════════════════════


def test_screening_follows_the_checked_ones_in_the_config_file(
        cfg, tmp_path, monkeypatch) -> None:
    """内存里那份与 `config.toml` 不一致时，**以文件为准**（用户勾的就是文件里那几条）。

    主人原话：「改成勾选几条就筛选几条，这是最基本的。」他的名字就在文件里、
    勾选框显示的也是文件里那份 —— 所以跑的那一份必须是它。两个 `Config` 对象、
    某次写盘没成功、别处改过这个键，都会让内存那份悄悄漂移；这里开跑前校准一次。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    # 文件里勾的是 B（反转），内存里那份还停在 A（低价）—— 模拟"漂移"
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text('enabled_formulas = ["%s"]\n' % B_NAME, encoding="utf-8")
    cfg.enabled_formulas = [A_NAME]
    # 校准的判据是 mtime：**只在"文件比我们记下的那一刻更新"时才以文件为准**
    # （无条件以文件为准会覆盖程序里主动设好的配置，那条路一次红掉 17 条用例）。
    cfg.source_mtime = cfg.source_path.stat().st_mtime - 10
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])

    report = sched.run_daily(cfg, DataEngine(cfg.db_path), notify=False, with_data=False)

    assert report["formulas"]["ran"] == [B_NAME]          # 只跑了文件里勾的那条
    assert {r["strategy"] for r in report["pool"]} == {formula_group.formula_strategy_name(B_NAME)}
    assert cfg.enabled_formulas == [B_NAME]                # 内存那份也被拉回一致


def test_screening_keeps_memory_when_the_config_file_is_broken(
        cfg, tmp_path, monkeypatch) -> None:
    """`config.toml` 读不出来时**保持内存那份**，绝不因为读不到文件就把策略清空。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5"})
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("this is not = = toml\n", encoding="utf-8")
    cfg.enabled_formulas = [A_NAME]
    cfg.source_mtime = cfg.source_path.stat().st_mtime - 10   # 文件被外面改过（但写坏了）
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])

    report = sched.run_daily(cfg, DataEngine(cfg.db_path), notify=False, with_data=False)

    assert report["formulas"]["ran"] == [A_NAME]
    assert cfg.enabled_formulas == [A_NAME]


def test_screening_does_not_touch_memory_when_the_file_was_not_touched(
        cfg, tmp_path, monkeypatch) -> None:
    """文件没被外面改过时，**内存那份说了算**（不许多此一举地"以文件为准"）。

    为什么单独钉：无条件以文件为准会覆盖"程序里主动设好的配置" —— 首次运行向导、
    CLI 的 `--config` 组合、以及一大堆用例都会直接给 `cfg` 赋值而文件不跟着变。
    第一版就是这么写的，CI 上一次红掉 17 条用例。判据是 mtime 有没有变。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {A_NAME: "C<5", B_NAME: "C>10"})
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text('enabled_formulas = ["%s"]\n' % B_NAME, encoding="utf-8")
    cfg.source_mtime = cfg.source_path.stat().st_mtime        # 与我们记下的那一刻一致
    cfg.enabled_formulas = [A_NAME]                            # 内存里是 A
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])

    report = sched.run_daily(cfg, DataEngine(cfg.db_path), notify=False, with_data=False)

    assert report["formulas"]["ran"] == [A_NAME]               # 跑的是内存里那份
    assert cfg.enabled_formulas == [A_NAME]
