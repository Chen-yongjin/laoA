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


def test_second_round_keeps_only_the_newly_checked_strategy(
        cfg, tmp_path, monkeypatch) -> None:
    """第一轮勾 A、第二轮只勾 B → 池子里只剩 B 的票 + 手工自选行，A 的行被清掉。

    这就是主人真机日志里的那个现象：他第二轮只勾了一条，结果页/「自选标的」里却还有
    **别的策略的票**（那些行带着"当初是哪条策略选的"）。
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

    # ── 第二轮：只勾 B ──
    cfg.enabled_formulas = [B_NAME]
    second = _run(cfg, monkeypatch)

    # ① 池子（= 结果页/桌面文件读的那一份）里只剩 B 的票 + 手工自选行
    assert {r["symbol"] for r in second["pool"]} == {HIGH, MID}
    assert {r["strategy"] for r in second["pool"]} == {"公式·反转", ""}

    # ② 库里（今天这一批）也没有「低价」的行了，手工自选那一行还在
    left = _pool_rows(cfg, day)
    assert {r["symbol"] for r in left} == {MID}, left
    assert all("低价" not in str(r["strategy"] or "") for r in left), left

    # ③ 那 5 只在「自选标的」页里变成**不在池子里的自选行**（`reason` 就是这句），
    #    不再是"这次筛选的池子行"；票本身照旧看得见、照旧被盯。
    #    ⚠️ 它们的「来源」列**照旧**写"当初是哪条策略选的"（`低价`）—— 那是主人
    #    2026-09-21 明确要的口径（见 `test_add_to_watchlist_keeps_the_pick_source_in_the_pool_page`）。
    #    也就是说：`来源`列回答"这票当初怎么来的"，而"是不是本次筛选结果"看的是
    #    它有没有进**这一轮**的池子（上面 ① ② 两条断言）。
    page = {r["symbol"]: r for r in pool_mod.pool_page_rows(cfg.db_path)}
    for symbol in LOW:
        assert page[symbol]["reason"] == "自选标的", page[symbol]
        assert page[symbol]["source_label"] == "低价"
    # B 选出来的那只**不在这一页**：2026-09-21 起筛选结果不自动进股池，它只在
    # 【开始筛选】的结果页 / 桌面文件 / `signal` 表里（上面 `second["pool"]` 已断言）。
    assert HIGH not in page

    # ④ 用户数据**一个都没少**：那 5 只票还在自选表里（只是不再算"这次选出来的"）
    with storage.connect(cfg.db_path) as conn:
        symbols = {r["symbol"] for r in storage.load_watchlist(conn)}
    assert set(LOW) | {MID} <= symbols


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
    """结果页 = **这一轮选出来的**那几只；池子里多出来的自选标的**不能混进去**。

    真机日志里的数字（本轮 3 只 / 池 8 只）就是这么来的：自选标的行也带着 `strategy`
    （"当初是哪条策略选的"），只按"有没有 strategy"判会把它们算成"本次筛选结果"。
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
    assert {r["symbol"] for r in report["pool"]} == {HIGH, MID}    # 池子：选出来的 + 自选

    page = fp.FormulaPage(cfg, directory=tmp_path / "formulas")
    try:
        page.show_pick_result(report, data_date=report.get("data_date"))
        app.processEvents()

        assert [r["symbol"] for r in page.result_rows] == [HIGH]    # 只列选出来的
        hint = page.result_hint.text()
        assert "本次筛选结果：共 1 只" in hint
        assert "池内共 2 只" in hint and "自选标的" in hint          # 差在哪说清楚
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
