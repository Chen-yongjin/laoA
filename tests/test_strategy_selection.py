"""「策略选取」勾的那几条 = 真正参与筛选的那几条（主人实报："没按我选的策略来"）。

主人原话（2026-10-12）："策略选股里点选指定策略后，没有按照用户选取的策略进行
（不知道什么逻辑）。" 这一组用例把这条链**从勾选框到池子**逐环钉死：

1. 只勾一条 → 池子里只有这条策略的票（另一条的票一只都不许出现），界面【开始筛选】
   与定时/CLI 走的那条路（`build_pool`）**同一个口径**；
2. **上一轮别的策略留在今天的池子里的行必须被清掉** —— 否则「自选标的」页（它读的
   就是 `stock_pool` 里那一天的行，`pool.pool_page_rows()`）会显示用户**没勾**的那条
   策略选出来的票，而用户勾的那条一只都没有（筛选结果不自动进股池，见
   `scheduler.run_daily` 的 `save_picks=False`）；
3. 勾选 → `cfg.enabled_formulas` 与**落盘内容**立刻同步（界面写盘的就地改 cfg，
   【开始筛选】用的就是同一个对象，两者不可能错开），并且"名字对不上"时
   `enabled_names()` 那句忽略的 warning 在**界面上也看得见**（列表上方那行提示）。

全程离线、只读本地合成库（`tests/conftest.py` 已在 socket 层封死网络）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from laoa_trader import pool as pool_mod
from laoa_trader import scheduler as sched
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine

#: 小库里两条策略**各自唯一命中**的票（与 `tests/test_pipeline.py` 同口径）：
#: 600001 = 3.0 元横盘 → 只有 `C<5` 命中；600003 ≈ 12.4 元 → 只有 `C>10` 命中。
LOW, HIGH = "600001", "600003"


def _ready_db(cfg):
    """造一份能精确断言"哪条策略选了哪只票"的行情库。"""
    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()
    n = len(days)

    def bars(symbol, closes, volumes):
        return [
            (symbol, day, float(closes[i]), float(closes[i]), float(closes[i]),
             float(closes[i]), float(volumes[i]), float(volumes[i]) * float(closes[i]))
            for i, day in enumerate(days)
        ]

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            (LOW, "低价样本", "银行"),
            (HIGH, "反转样本", "半导体"),
        ])
        storage.write_daily_raw(conn, bars(LOW, [3.0] * n, [2e7] * n)
                                + bars(HIGH, [20.0 * (0.988 ** i) for i in range(n)],
                                       [2e7] * n))
        storage.write_calendar(conn, days)
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
    _write_formulas(tmp_path, monkeypatch, cfg, {"低价": "C<5", "反转": "C>10"})
    cfg.enabled_formulas = ["低价"]

    report = _run(cfg, monkeypatch)

    assert [r["symbol"] for r in report["pool"]] == [LOW]
    assert {r["strategy"] for r in report["pool"]} == {"公式·低价"}
    # 另一条策略**一次都没跑**（候选里有它就说明"勾了却不生效"）
    assert report["formulas"]["ran"] == ["低价"]


def test_build_pool_direct_call_follows_the_checked_one(cfg, tmp_path, monkeypatch) -> None:
    """直接调 `build_pool`（定时任务/CLI 那条路）也只看勾的那几条。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {"低价": "C<5", "反转": "C>10"})
    cfg.enabled_formulas = ["反转"]

    rows = pool_mod.build_pool(DataEngine(cfg.db_path), cfg)

    assert [r["symbol"] for r in rows] == [HIGH]
    assert {r["strategy"] for r in rows} == {"公式·反转"}


# ══════════════════════════════════════════════════════════════════════════
# 2) 上一轮别的策略留在**今天池子里**的行必须被清掉
# ══════════════════════════════════════════════════════════════════════════


def test_previous_round_rows_of_another_strategy_are_cleared(
        cfg, tmp_path, monkeypatch) -> None:
    """上一轮别的策略在**今天**的池子里留下的行，这一轮必须清掉。

    这就是主人看到的现象：他只勾了「低价」，而「自选标的」页（读 `pool_page_rows()`）
    里却挂着 `公式·旧策略` 选出来的那只票 —— 那一行是**上一轮留下的残留**，
    而"同一批残留要清掉"这件事全靠 `storage.save_pool()` 里那句按 `batch` 的清理。

    为什么残留会留到今天：2026-09-21 起筛选结果不再自动进股池（`save_picks=False`），
    于是"这一轮一行都不用写"（没勾公式 / 自选都没开监控）成了**常态** ——
    那一轮以前连清理一起跳过，残留就永远躺在那儿。
    """
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {"低价": "C<5", "反转": "C>10"})

    # 先跑一轮把"今天"（行情日）定下来，再往今天的池子里放一行**别的策略**写的行 ——
    # 这就是主人看到的那一行：老版本把筛选结果写进过池子，或上一轮「反转」选的票
    # 被加进自选、后来又去掉了监控，而**那一轮之后再也没有东西要落库**，
    # 于是它就一直挂在今天的池子里
    first = _run(cfg, monkeypatch)
    day = first["data_date"]
    pool_mod.save_pool(cfg.db_path, [
        {"symbol": HIGH, "name": "反转样本", "strategy": "公式·旧策略",
         "strategies": "公式·旧策略", "score": 3.0, "reason": "上一轮留下的"},
    ], day)
    assert [r["symbol"] for r in _pool_rows(cfg, day)] == [HIGH]

    # 这一轮：只勾「低价」，而且这一轮**没有任何自选标的要落库**
    cfg.enabled_formulas = ["低价"]
    second = _run(cfg, monkeypatch)
    assert [r["symbol"] for r in second["pool"]] == [LOW]

    # ① 库里（今天这一批）只剩这一轮真正写下去的行 —— 旧策略那一行没了
    left = _pool_rows(cfg, day)
    assert all(r["strategy"] != "公式·旧策略" for r in left), left

    # ② 「自选标的」页看到的那一批里也不许再有它（这一页就是主人嘴里的"结果"）
    page = pool_mod.pool_page_rows(cfg.db_path)
    assert HIGH not in [r["symbol"] for r in page], page
    assert all(str(r.get("source_label") or "") != "旧策略" for r in page), page


def test_previous_round_row_is_cleared_even_when_the_pool_is_empty(
        cfg, tmp_path, monkeypatch) -> None:
    """这一轮池子完全是空的（一条都没勾、也没自选）时，残留照样要清。"""
    _ready_db(cfg)
    _write_formulas(tmp_path, monkeypatch, cfg, {"低价": "C<5"})
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
# 3) 勾选 → cfg 与落盘内容**立刻**同步（界面与实跑不可能错开）
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
