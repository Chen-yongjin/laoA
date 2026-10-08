"""DataEngine（策略读库的门面）+ 调度器（后台线程、定时、暂停）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from laoa_trader import scheduler
from laoa_trader.data import storage
from laoa_trader.data.engine import COVERAGE_FLOOR, DataEngine


# ── DataEngine ──


def test_latest_data_date(db) -> None:
    assert DataEngine(db).get_latest_data_date() == "2026-09-11"


def test_get_local_symbols_and_industries(db) -> None:
    engine = DataEngine(db)
    assert "600001" in engine.get_local_symbols()
    assert engine.get_industry_map()["600002"] == "半导体"


def test_get_stock_names_local_only(db) -> None:
    names = DataEngine(db).get_stock_names()
    assert names["600001"] == "浦发样本"
    assert DataEngine(db).get_stock_names(["600001", "999999"]) == {"600001": "浦发样本"}


def test_get_active_symbols_returns_latest_day(db) -> None:
    symbols = DataEngine(db).get_active_symbols()
    assert "600001" in symbols
    assert len(symbols) == 6


def test_get_active_symbols_falls_back_when_coverage_low(db) -> None:
    """最新交易日只有极少数股票有数据（数据源故障）时，回退到覆盖率达标的上一交易日。"""
    with storage.connect(db) as conn:
        last = conn.execute("SELECT MAX(date) FROM stock_daily_raw").fetchone()[0]
        # 只留一只股票在最后一天 → 覆盖率 1/6 < COVERAGE_FLOOR，必须回退
        conn.execute("DELETE FROM stock_daily_raw WHERE date = ? AND symbol != '600001'",
                     (last,))
        conn.commit()
    symbols = DataEngine(db).get_active_symbols()
    assert len(symbols) == 6           # 回退到前一天（全员有行情）
    assert COVERAGE_FLOOR == 0.5


def test_get_active_symbols_explicit_date(db) -> None:
    with storage.connect(db) as conn:
        day = conn.execute(
            "SELECT date FROM stock_daily_raw GROUP BY date ORDER BY date LIMIT 1"
        ).fetchone()[0]
    symbols = DataEngine(db).get_active_symbols(day)
    assert symbols and all(s.startswith(("6", "0", "3")) for s in symbols)


def test_get_active_symbols_empty_db(cfg) -> None:
    assert DataEngine(cfg.db_path).get_active_symbols() == []


def test_data_coverage_and_summary(db) -> None:
    engine = DataEngine(db)
    coverage = engine.data_coverage()
    assert coverage["symbols"] == 6
    assert coverage["universe"] == 6
    assert coverage["rate"] == 1.0
    summary = engine.summary()
    assert summary["symbols"] == 6
    assert summary["latest_date"] == "2026-09-11"


def test_engine_survives_missing_database(cfg) -> None:
    """库文件不存在时自动建空库（不抛异常）—— 首次运行点"匹配"不该崩。"""
    engine = DataEngine(cfg.data_dir / "brand-new.db")
    assert engine.get_active_symbols() == []
    assert engine.get_latest_data_date() is None
    assert engine.summary()["daily_rows"] == 0


# ── 调度器 ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [("19:15", (19, 15)), ("00:00", (0, 0)), ("23:59", (23, 59)), ("16:00", (16, 0)),
     ("07:05", (7, 5)),
     # 解析不了就退回默认主跑时间（16:00）——配置写错不该让程序跑不起来
     ("bad", (16, 0)), ("25:00", (16, 0)), ("", (16, 0))],
)
def test_parse_run_at(text: str, expected: tuple[int, int]) -> None:
    assert scheduler.parse_run_at(text) == expected


def test_scheduler_start_stop_and_status(cfg, db) -> None:
    # `auto_run` 默认已经改成 false（用户拍板：不设定时运行）——这里显式打开，
    # 这条用例验的是"状态里如实报告开关与下次运行时间"，不是默认值本身。
    cfg.auto_run = True
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    assert sched.running is False
    assert sched.start() is True
    assert sched.start() is False          # 重复启动是幂等的
    assert sched.running is True
    status = sched.status()
    assert status["running"] is True
    assert status["run_at"] == "16:00"                 # 新默认：收盘后主跑
    assert status["run_at_fallback"] == "19:15"        # 补跑
    assert status["auto_run"] is True
    assert status["next_run"]["label"].startswith(("今天", "明天"))   # 状态栏要显示的
    assert status["intraday_paused"] is False
    assert status["last_error"] == ""
    assert sched.stop() is True
    assert sched.stop() is False           # 重复停止也是幂等的
    assert sched.running is False


def test_scheduler_pause_resume_intraday(cfg, db) -> None:
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    assert sched.intraday_paused is False
    sched.pause_intraday()
    assert sched.status()["intraday_paused"] is True
    sched.resume_intraday()
    assert sched.status()["intraday_paused"] is False


def test_scheduler_loop_never_dies_on_error(cfg, db, monkeypatch) -> None:
    """后台线程抛异常必须被吃掉（否则会静默死掉，是最难查的 bug）。"""
    sched = scheduler.Scheduler(cfg, DataEngine(db))

    def boom(*args, **kwargs):
        raise RuntimeError("内部炸了")

    monkeypatch.setattr(sched, "_maybe_intraday", boom)
    sched.start()
    import time

    time.sleep(0.2)
    assert sched.running is True            # 循环还在跑
    sched.stop()


def test_run_daily_pipeline_without_key_is_safe(cfg, db, monkeypatch) -> None:
    """没配 Key 也能跑完整流程（只用本地库匹配），不推送。"""
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    cfg.hithink_api_key = ""
    report = scheduler.run_daily(cfg, DataEngine(db), notify=False)
    assert report["data_date"] == "2026-09-11"
    assert isinstance(report["sync"], list)
    assert "pool" in report
    # 数据同步会失败（没 Key），但那是"记录下来的错误"，不是异常
    assert all(isinstance(r.error, str) for r in report["sync"])


def test_run_daily_notifies_pool_with_plan_params(cfg, db, monkeypatch) -> None:
    """有池子时应推送，且正文里带条件单参数（L2：提醒 + 参数）。"""
    from laoa_trader import notify as notify_mod

    captured: dict = {}

    def fake_notify_all(title, lines, kinds=notify_mod.KINDS, cfg=None):
        captured["title"] = title
        captured["lines"] = lines
        return {"feishu": {"ok": True, "skipped": True}, "windows": {"ok": True},
                "tray": {"ok": True}}

    monkeypatch.setattr(notify_mod, "notify_all", fake_notify_all)
    monkeypatch.setattr(scheduler.sync, "daily_update", lambda *a, **k: [])

    def fake_formulas(*_args, **_kwargs):
        # 2026-09-18（用户要求）：候选只来自**勾选的公式**，所以这里给的是
        # `公式·X` 这种合成键（不是策略类名）。键写错的话建池会把它当"老策略类名"丢掉。
        from laoa_trader.strategy import formula_group

        run = formula_group.FormulaRun()
        run.picks = {formula_group.formula_strategy_name("短期反转"): [
            {"symbol": "600001", "name": "低价样本", "reason": "短期反转"},
        ]}
        run.ran = ["短期反转"]
        return run

    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas",
                        fake_formulas)
    monkeypatch.setattr("laoa_trader.pool.hot_industries", lambda db_path, **k: {})

    report = scheduler.run_daily(cfg, DataEngine(db), notify=True)
    assert report["pool"]
    assert "luweik-标的池" in captured["title"]
    body = "\n".join(captured["lines"])
    assert "600001" in body
    assert "条件单参数" in body          # 附带触发价/委托价/止损止盈
    assert "止损" in body and "止盈" in body


def test_run_daily_survives_formula_crash(cfg, db, monkeypatch) -> None:
    """候选那一层（现在是「公式」组）整体崩掉时，流程仍返回结构化报告（界面据此提示）。

    2026-09-18 起"策略层"就是"公式层"（内置策略退出了匹配链路），所以这条从
    `rules.run_all` 崩溃改成 `formula_group.run_enabled_formulas` 崩溃 ——
    要保的东西没变：异常不许冒出去，必须变成 `report["errors"]` 里一句能看懂的中文。
    """
    monkeypatch.setattr(scheduler.sync, "daily_update", lambda *a, **k: [])

    def boom(*args, **kwargs):
        raise RuntimeError("公式全炸了")

    monkeypatch.setattr("laoa_trader.strategy.formula_group.run_enabled_formulas", boom)
    report = scheduler.run_daily(cfg, DataEngine(db), notify=False)
    # 错误里要能看出"哪一步炸了"（前缀是"匹配："）+ 原始异常（用户据此报障）
    assert any("匹配：" in e and "公式全炸了" in e for e in report["errors"])
    assert report["pool"] == []


def test_run_intraday_now_records_report(cfg, db, monkeypatch) -> None:
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(
        scheduler.intraday, "run_once",
        lambda *a, **k: {"trading_day": True, "in_session": True, "hits": 0,
                         "fresh": 0, "pushed": False, "error": ""},
    )
    report = sched.run_intraday_now()
    assert report["hits"] == 0
    assert sched.status()["last_intraday_at"] is not None


def test_scheduler_daily_fires_only_once_per_day(cfg, db, monkeypatch) -> None:
    """定时任务：当天到点后只触发一次（不能每秒重跑）。

    注意要显式打开 `auto_run`：默认已是 false，不开的话 `_maybe_daily()` 按设计
    什么都不做（那条行为由 test_autorun.py 的 `auto_run=false` 用例覆盖）。
    """
    cfg.auto_run = True
    calls: list[int] = []
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(scheduler.intraday, "is_trading_day", lambda *a, **k: True)

    def fake_run(**kwargs):
        calls.append(1)
        sched.mark_daily_ran({"errors": [], "pool": []})     # 模拟"跑成功"
        return {"errors": [], "pool": []}

    monkeypatch.setattr(sched, "run_daily_now", fake_run)
    sched._maybe_daily(datetime(2026, 9, 11, 16, 0))         # 到主跑点
    sched._maybe_daily(datetime(2026, 9, 11, 16, 0, 1))      # 下一秒：成功标记拦住
    assert len(calls) == 1
    # 第二天到点再触发一次
    sched._maybe_daily(datetime(2026, 9, 14, 16, 0))
    assert len(calls) == 2


def test_scheduler_skips_daily_before_run_at(cfg, db, monkeypatch) -> None:
    calls: list[int] = []
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(sched, "run_daily_now", lambda **k: calls.append(1))
    sched._maybe_daily(datetime(2026, 9, 11, 9, 0))          # 早于 16:00
    assert calls == []


def test_scheduler_skips_daily_on_non_trading_day(cfg, db, monkeypatch) -> None:
    cfg.auto_run = True               # 默认已关，这里要验"非交易日不跑"
    calls: list[int] = []
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(sched, "run_daily_now", lambda **k: calls.append(1))
    monkeypatch.setattr(scheduler.intraday, "is_trading_day", lambda *a, **k: False)
    sched._maybe_daily(datetime(2026, 9, 11, 20, 0))
    assert calls == []
    assert sched.status()["last_daily_date"] == "2026-09-11"


def test_scheduler_intraday_respects_pause_and_interval(cfg, db, monkeypatch) -> None:
    calls: list[int] = []
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    cfg.intraday_interval = 60
    monkeypatch.setattr(scheduler.intraday, "in_session", lambda now=None: True)
    monkeypatch.setattr(
        scheduler.intraday, "run_once",
        lambda *a, **k: calls.append(1) or {"hits": 0, "fresh": 0, "error": ""},
    )
    sched._maybe_intraday(datetime(2026, 9, 11, 10, 0))
    sched._maybe_intraday(datetime(2026, 9, 11, 10, 0, 1))   # 间隔不够，跳过
    assert len(calls) == 1

    sched.pause_intraday()
    sched._last_intraday_ts = 0
    sched._maybe_intraday(datetime(2026, 9, 11, 10, 5))
    assert len(calls) == 1                                   # 暂停后不再跑


def test_scheduler_intraday_outside_session(cfg, db, monkeypatch) -> None:
    calls: list[int] = []
    sched = scheduler.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(scheduler.intraday, "in_session", lambda now=None: False)
    monkeypatch.setattr(scheduler.intraday, "run_once",
                        lambda *a, **k: calls.append(1) or {})
    sched._maybe_intraday(datetime(2026, 9, 11, 20, 0))
    assert calls == []
