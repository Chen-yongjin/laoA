"""决策信号界面侧（`ui/signals.py::build_signals`）的测试。

这一层要守住的：

1. **只读库、不发网络请求**（大模型那条线还没接，也不许在这一层偷偷接）；
2. 自选池与持仓**两路结论集不许串**（"离场信号成立"不该出现在自选池的结论里）；
3. 依据里必须带上**来源与口径**（哪个交易日、哪条策略、多少资金）；
4. 缺数据时照出结论（继续观察 / 持有），并在 `notes` 里说明缺什么 —— 不许瞎猜。
"""

from __future__ import annotations

import pytest

from laoa_trader import analysis
from laoa_trader.analysis import DROP, ENTER, EXIT, HOLD, TRIM, WATCH
from laoa_trader.data import storage
from laoa_trader.scoring import ScoreResult
from laoa_trader.ui import signals as signals_mod


def _score(total: float, symbol: str = "600001", advice: str = "中性") -> ScoreResult:
    return ScoreResult(symbol=symbol, total=total, advice=advice,
                       as_of="2026-09-11", caliber="前复权")


@pytest.fixture()
def fund_flow(db):
    """给 600001 写两条资金流（**元**，与东财口径一致）—— 最新一天是净流出。"""
    with storage.connect(db) as conn:
        storage.write_fund_flow(conn, [
            {"date": "2026-09-10", "symbol": "600001", "main_net": 5.0e7,
             "main_net_pct": 3.0, "super_net": 3.0e7, "big_net": 2.0e7,
             "close": 3.0, "pct": 1.0, "source": "eastmoney"},
            {"date": "2026-09-11", "symbol": "600001", "main_net": -8.2e6,
             "main_net_pct": -2.5, "super_net": -5.0e6, "big_net": -3.2e6,
             "close": 2.9, "pct": -1.2, "source": "eastmoney"},
        ])
    return db


def _getter(scores: dict):
    return lambda symbol: scores.get(symbol)


def test_pool_verdicts_follow_score_and_this_round_picks(db) -> None:
    """自选池：评分强 + 当轮被选中 → 进场信号；只剩弱分且没被选中 → 建议剔除。"""
    strong = signals_mod.build_signals(
        None, db, ["600001"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(82.0, advice="强势")}),
    )["600001"]
    assert strong.verdict == WATCH, "没有当轮命中记录时不该直接给进场信号"

    weak = signals_mod.build_signals(
        None, db, ["600001"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(21.0, advice="弱势")}),
    )["600001"]
    assert weak.verdict == DROP
    assert any("长期无信号" in n for n in weak.notes)


def test_pool_enter_uses_the_round_ledger_without_formula_prefix(db, monkeypatch) -> None:
    """当轮命中来自台账，且**显示时不带 `公式·` 前缀**（界面文案口径，见 wording.py）。"""
    monkeypatch.setattr(signals_mod.pool_mod, "round_pick_strategies",
                        lambda *a, **k: {"600001": "公式·竞价策略"})
    signal = signals_mod.build_signals(
        None, db, ["600001"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(80.0, advice="偏强")}),
    )["600001"]
    assert signal.verdict == ENTER
    joined = "\n".join(e.line() for e in signal.evidences)
    assert "竞价策略" in joined
    assert "公式·" not in joined


def test_pool_carries_watchlist_and_fund_evidence(fund_flow) -> None:
    """依据里要看得到"它在你自选里"与"主力净额多少万元"（用户能自己核）。"""
    with storage.connect(fund_flow) as conn:
        storage.upsert_watchlist(conn, "600001", name="浦发样本")
    signal = signals_mod.build_signals(
        None, fund_flow, ["600001"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(50.0)}),
    )["600001"]
    joined = "\n".join(e.line() for e in signal.evidences)
    assert "在自选表里" in joined
    assert "主力净额 -820 万元" in joined          # 元 → 万元，负号保留


def test_position_verdicts_use_cost_and_fund_flow(db) -> None:
    """持仓：评分弱 + 主力净流出 → 离场；资金没流出 → 保持持有（不因分低就喊走）。"""
    with storage.connect(db) as conn:
        storage.upsert_position(conn, "600003", name="半导体乙", quantity=200,
                                avg_cost=18.0, note="")
        storage.write_fund_flow(conn, [
            {"date": "2026-09-11", "symbol": "600003", "main_net": -3.0e7,
             "main_net_pct": -4.0, "super_net": -2.0e7, "big_net": -1.0e7,
             "close": 18.0, "pct": -2.0, "source": "eastmoney"},
        ])
    weak = signals_mod.build_signals(
        None, db, ["600003"], signals_mod.KIND_POSITION,
        score_getter=_getter({"600003": _score(22.0, symbol="600003", advice="弱势")}),
    )["600003"]
    assert weak.verdict == EXIT
    joined = "\n".join(e.line() for e in weak.evidences)
    assert "成本 18.00" in joined and "持有 200 股" in joined
    assert "主力净额 -3000 万元" in joined

    strong = signals_mod.build_signals(
        None, db, ["600003"], signals_mod.KIND_POSITION,
        score_getter=_getter({"600003": _score(70.0, symbol="600003", advice="偏强")}),
    )["600003"]
    assert strong.verdict == HOLD


def test_position_trims_when_the_score_is_only_slightly_weak(db) -> None:
    """减仓那档：45 分以下 + 资金流出（比离场那档轻）—— 两档不打架。"""
    with storage.connect(db) as conn:
        storage.upsert_position(conn, "600002", name="半导体甲", quantity=100,
                                avg_cost=12.4, note="")
        storage.write_fund_flow(conn, [
            {"date": "2026-09-11", "symbol": "600002", "main_net": -5.0e6,
             "main_net_pct": -1.0, "super_net": -3.0e6, "big_net": -2.0e6,
             "close": 12.0, "pct": -0.5, "source": "eastmoney"},
        ])
    signal = signals_mod.build_signals(
        None, db, ["600002"], signals_mod.KIND_POSITION,
        score_getter=_getter({"600002": _score(40.0, symbol="600002", advice="偏弱")}),
    )["600002"]
    assert signal.verdict == TRIM


def test_position_stop_line_beats_a_strong_score(db) -> None:
    """用户自己设的止损线一旦触发就是离场 —— 哪怕评分很高（程序不推翻用户的线）。"""
    cfg = type("Cfg", (), {"stop_loss": 0.05, "take_profit": 0.10})()
    with storage.connect(db) as conn:
        # 成本 10 元；小库最后一天收盘价远低于止损位（600001 一路阴跌到 3 元附近）
        storage.upsert_position(conn, "600001", name="浦发样本", quantity=100,
                                avg_cost=10.0, note="")
    signal = signals_mod.build_signals(
        cfg, db, ["600001"], signals_mod.KIND_POSITION,
        score_getter=_getter({"600001": _score(95.0, advice="强势")}),
    )["600001"]
    assert signal.verdict == EXIT
    assert any("止损 5.0%" in e.line() for e in signal.evidences)


def test_missing_score_still_produces_a_verdict(db) -> None:
    """评分算不出来也**照样出结论**（继续观察 / 持有），并说明缺什么。"""
    pool = signals_mod.build_signals(None, db, ["600001"], signals_mod.KIND_POOL,
                                     score_getter=_getter({}))
    assert pool["600001"].verdict == WATCH

    with storage.connect(db) as conn:
        storage.upsert_position(conn, "600001", name="浦发样本", quantity=0,
                                avg_cost=0.0, note="")
    pos = signals_mod.build_signals(None, db, ["600001"], signals_mod.KIND_POSITION,
                                    score_getter=_getter({}))
    assert pos["600001"].verdict == HOLD
    assert pos["600001"].notes, "缺数据必须在 notes 里说清楚"


def test_no_network_at_all(monkeypatch, db) -> None:
    """**这一层不许发任何网络请求**（大模型那条线接进来之后也只能挂附注）。"""
    import socket

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("分析不该联网")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    results = signals_mod.build_signals(
        None, db, ["600001", "600002"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(50.0), "600002": _score(50.0)}),
    )
    assert len(results) == 2


def test_unknown_symbols_are_dropped_and_duplicates_merged(db) -> None:
    """空代码/重复代码不进结果（重复会让同一只票在窗里出现两行）。"""
    results = signals_mod.build_signals(
        None, db, ["600001", "", "600001", "   "], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(50.0)}),
    )
    assert list(results) == ["600001"]


def test_kind_decides_which_verdict_set_is_used(db) -> None:
    """同一只票、同一份数据：池子那一路给池子的结论，持仓那一路给持仓的结论。"""
    with storage.connect(db) as conn:
        storage.upsert_position(conn, "600001", name="浦发样本", quantity=100,
                                avg_cost=3.0, note="")
    scores = _getter({"600001": _score(20.0, advice="弱势")})
    pool = signals_mod.build_signals(None, db, ["600001"], signals_mod.KIND_POOL,
                                     score_getter=scores)["600001"]
    pos = signals_mod.build_signals(None, db, ["600001"], signals_mod.KIND_POSITION,
                                    score_getter=scores)["600001"]
    assert pool.verdict in analysis.POOL_VERDICTS
    assert pos.verdict in analysis.POSITION_VERDICTS
    assert pool.verdict != pos.verdict or pool.verdict in (WATCH, HOLD)


# ── 导出：报告落盘（与筛选结果同一套落点规矩）──


def _one_signal(db) -> object:
    return signals_mod.build_signals(
        None, db, ["600001"], signals_mod.KIND_POOL,
        score_getter=_getter({"600001": _score(78.0, advice="偏强")}),
    )["600001"]


def test_export_writes_into_the_luweik_folder_on_the_desktop(tmp_path, db) -> None:
    """找到桌面就落在 `桌面/luweik/luweik-决策信号-<日期>.txt`（与筛选结果同一个文件夹）。"""
    from laoa_trader import analysis, pool

    (tmp_path / "Desktop").mkdir()
    text = analysis.report_text([_one_signal(db)], day="2026-10-13", generated_at="t")
    path = pool.export_signal_file(text, day="2026-10-13", home=tmp_path)
    assert path is not None
    assert path.parent == tmp_path / "Desktop" / pool.EXPORT_FOLDER_NAME
    assert path.name == "luweik-决策信号-2026-10-13.txt"
    raw = path.read_bytes()
    # Windows 用户双击就用记事本打开：带 BOM + CRLF（与筛选结果那份逐字节同一套口径）
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    written = path.read_text(encoding="utf-8-sig")
    assert written.startswith("luweik · 决策信号 · 2026-10-13")
    assert "【继续观察】600001" in written
    assert "不构成投资建议" in written


def test_export_falls_back_to_the_data_dir_when_there_is_no_desktop(tmp_path, db) -> None:
    """找不到桌面就退到给它的落点（数据目录那个 `luweik` 子目录），而不是悄悄不导出。"""
    from laoa_trader import analysis, pool

    text = analysis.report_text([_one_signal(db)], day="2026-10-13", generated_at="t")
    path = pool.export_signal_file(text, day="2026-10-13", home=tmp_path,
                                   fallback_dir=tmp_path / "data")
    assert path is not None
    assert path.parent == tmp_path / "data" / pool.EXPORT_FOLDER_NAME
    assert path.exists()


def test_export_same_day_overwrites_instead_of_piling_up(tmp_path, db) -> None:
    """同一天再导一次 = 覆盖同一个文件（桌面不是归档目录）。"""
    from laoa_trader import analysis, pool

    (tmp_path / "Desktop").mkdir()
    text = analysis.report_text([_one_signal(db)], day="2026-10-13", generated_at="t")
    first = pool.export_signal_file(text, day="2026-10-13", home=tmp_path)
    second = pool.export_signal_file(text + "（第二版）", day="2026-10-13", home=tmp_path)
    assert first == second
    assert "第二版" in first.read_text(encoding="utf-8-sig")
    assert len(list(first.parent.glob("*.txt"))) == 1


def test_export_refuses_empty_text(tmp_path, db) -> None:
    """没有内容就不写文件（返回 None，由界面说"还没有可导出的结论"）。"""
    from laoa_trader import pool

    assert pool.export_signal_file("   \n", day="2026-10-13", home=tmp_path) is None
    assert not (tmp_path / "Desktop").exists()
