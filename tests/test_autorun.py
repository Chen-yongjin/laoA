"""界面里设置"每天自动运行时间"：校验、立即生效、补跑、CLI 覆盖。

覆盖需求「界面里直接设置每天自动运行时间」的全部验收点：

1. 界面保存 → 写回 config（注释仍在）；非法格式 → 拒绝且 config 不变；
2. **改完不重启就生效**（注入 now，不 sleep）；
3. `auto_run=false` → 定时不触发，手动照常；
4. 补跑：主跑成功 → 不补跑；主跑失败 → 到补跑点再试；同一天不重复跑（成功标记）；
5. 主跑早于 15:00 → 允许保存但**明确提示**；
6. 补跑 ≤ 主跑 → 拒绝；
7. CLI `--run-at` / `--run-at-fallback` / `--no-auto-run` 临时覆盖（不改文件）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from laoa_trader import scheduler as sched
from laoa_trader.config import load_config, render_config_updates
from laoa_trader.data.engine import DataEngine
from laoa_trader.scheduler import (
    Scheduler,
    next_run_info,
    parse_hhmm,
    validate_run_times,
)

from tests._toml import p

# ── 解析与校验 ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [("16:00", (16, 0)), ("00:00", (0, 0)), ("23:59", (23, 59)), ("09:05", (9, 5))],
)
def test_parse_hhmm_ok(text: str, expected: tuple[int, int]) -> None:
    assert parse_hhmm(text) == expected


@pytest.mark.parametrize("text", ["", "1600", "16", "16:0", "16:000", "25:00", "16:60",
                                  "aa:bb", "16：00", None])
def test_parse_hhmm_rejects(text) -> None:
    """严格解析：这些都不算合法 HH:MM（保存时要拒绝，而不是猜）。"""
    assert parse_hhmm(text) is None


def test_parse_hhmm_tolerates_surrounding_spaces() -> None:
    """两侧空格容忍（用户从别处粘贴过来很常见），但格式本身仍要严格。"""
    assert parse_hhmm(" 16:00 ") == (16, 0)
    assert parse_hhmm("\t19:15") == (19, 15)


def test_validate_ok() -> None:
    assert validate_run_times("16:00", "19:15") == (True, [])


def test_validate_rejects_bad_format() -> None:
    ok, messages = validate_run_times("25:99", "19:15")
    assert ok is False
    assert any("格式不对" in m for m in messages)


def test_validate_rejects_bad_fallback_format() -> None:
    ok, messages = validate_run_times("16:00", "7:5")
    assert ok is False
    assert any("补跑时间格式不对" in m for m in messages)


def test_validate_rejects_fallback_not_after_primary() -> None:
    """补跑 ≤ 主跑 → 拒绝（否则"补跑"会先于主跑，逻辑矛盾）。"""
    ok, messages = validate_run_times("16:00", "16:00")
    assert ok is False
    assert any("必须**晚于**主跑时间" in m for m in messages)
    ok2, _ = validate_run_times("16:00", "15:59")
    assert ok2 is False
    # 只填主跑、不填补跑：允许（等于不做补跑）
    assert validate_run_times("16:00", "") == (True, [])


def test_validate_warns_before_market_close() -> None:
    """早于 15:00 收盘 → **允许保存**但给出提示。"""
    ok, messages = validate_run_times("09:30", "19:15")
    assert ok is True
    assert len(messages) == 1
    assert "早于 A 股收盘" in messages[0]
    assert "16:00 之后" in messages[0]
    # 14:59 也要提示；15:00 整不提示
    assert validate_run_times("14:59")[1]
    assert validate_run_times("15:00")[1] == []


# ── 下次自动运行时间（状态栏显示的内容） ──


def test_next_run_info_states() -> None:
    from laoa_trader.config import Config

    cfg = Config(run_at="16:00", run_at_fallback="19:15", auto_run=True)
    morning = datetime(2026, 9, 11, 9, 0)
    assert next_run_info(cfg, None, now=morning)["label"] == "今天 16:00"

    # 过了主跑、还没到补跑 → 显示补跑（主跑未成功时才会跑）
    after = datetime(2026, 9, 11, 17, 0)
    info = next_run_info(cfg, None, now=after)
    assert info["kind"] == "fallback"
    assert info["label"] == "今天 19:15（主跑未成功时的补跑）"

    # 过了补跑 → 明天主跑
    late = datetime(2026, 9, 11, 20, 0)
    assert next_run_info(cfg, None, now=late)["label"] == "明天 16:00"

    # 今天已经成功跑过 → 明天
    assert next_run_info(cfg, None, now=morning, done_today=True)["label"] == "明天 16:00"

    # 关掉自动运行 → 明确显示已关闭
    cfg.auto_run = False
    info = next_run_info(cfg, None, now=morning)
    assert info["kind"] == "disabled"
    assert "已关闭" in info["label"]


def test_next_run_info_uses_trading_calendar(cfg, db) -> None:
    """有交易日历时，"明天"要跳到**下一个交易日**（周末不会说"周六 16:00"）。

    `auto_run` 现在是**默认 false**，所以这里要显式打开：不开的话这个函数按设计
    返回"已关闭"（那种情况由 `test_next_run_info_states` 覆盖）。
    """
    from laoa_trader.data import storage

    cfg.auto_run = True
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, ["2026-09-11", "2026-09-14"])   # 周五 + 下周一
    friday_night = datetime(2026, 9, 11, 20, 0)
    info = next_run_info(cfg, cfg.db_path, now=friday_night)
    assert info["at"].startswith("2026-09-14")      # 跳过周末
    assert info["label"] == "09-14 16:00"


# ── 定时触发：主跑 / 补跑 / 成功标记 ──


def _sched(cfg, db, monkeypatch, *, succeed: bool = True):
    """造一个调度器，并把日更替身换成"成功/失败由参数决定"的假实现。"""
    sched_obj = Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    calls: list[datetime] = []

    def fake_run(**kwargs):
        calls.append(datetime.now())
        report = {"errors": [] if succeed else ["模拟失败：拉数据出错"],
                  "pool": [], "picks": 0}
        sched_obj.mark_daily_ran(report)
        return report

    monkeypatch.setattr(sched_obj, "run_daily_now", fake_run)
    return sched_obj, calls


def test_primary_run_fires_at_run_at(cfg, db, monkeypatch) -> None:
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _sched(cfg, db, monkeypatch)
    s._maybe_daily(datetime(2026, 9, 11, 15, 59))
    assert calls == []                       # 没到点
    s._maybe_daily(datetime(2026, 9, 11, 16, 0))
    assert len(calls) == 1                   # 到点就跑


def test_change_run_at_affects_next_round(cfg, db, monkeypatch) -> None:
    """把主跑时间改晚 → 同一时刻不再触发；改早 → 立刻触发（都不重启）。"""
    cfg.run_at_fallback, cfg.auto_run = "19:15", True
    cfg.run_at = "17:00"
    s, calls = _sched(cfg, db, monkeypatch)
    moment = datetime(2026, 9, 11, 16, 30)
    s._maybe_daily(moment)
    assert calls == []                       # 16:30 < 17:00，不跑

    cfg.run_at = "16:00"                     # 用户刚在界面改成 16:00（没重启）
    s._maybe_daily(moment)
    assert len(calls) == 1                   # 下一轮就按新时间跑


def test_auto_run_off_blocks_scheduled_but_allows_manual(cfg, db, monkeypatch) -> None:
    """关掉自动运行：定时不触发；手动按钮照常可用。"""
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", False
    s = Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    runs: list[dict] = []

    def fake_pipeline(*a, **kwargs):
        runs.append(kwargs)
        return {"errors": [], "pool": [], "picks": 0}

    monkeypatch.setattr(sched, "run_daily", fake_pipeline)     # 真正的日更流程替身

    s._maybe_daily(datetime(2026, 9, 11, 16, 0))
    s._maybe_daily(datetime(2026, 9, 11, 19, 15))
    assert runs == []                        # 自动触发被关掉

    report = s.run_daily_now(notify=False, with_data=False)
    assert report["errors"] == []            # 手动跑照常
    assert len(runs) == 1


def test_successful_primary_skips_fallback(cfg, db, monkeypatch) -> None:
    """主跑成功 → 到补跑时间也不补跑（成功标记拦住）。"""
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _sched(cfg, db, monkeypatch, succeed=True)
    s._maybe_daily(datetime(2026, 9, 11, 16, 0))
    assert len(calls) == 1
    s._maybe_daily(datetime(2026, 9, 11, 19, 15))     # 补跑点
    s._maybe_daily(datetime(2026, 9, 11, 20, 0))
    assert len(calls) == 1                            # 没有补跑


def test_failed_primary_triggers_fallback(cfg, db, monkeypatch) -> None:
    """主跑失败 → 到补跑时间再试一次；补跑成功后就收工。"""
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _sched(cfg, db, monkeypatch, succeed=False)
    s._maybe_daily(datetime(2026, 9, 11, 16, 0))
    assert len(calls) == 1                            # 主跑失败
    s._maybe_daily(datetime(2026, 9, 11, 18, 0))
    assert len(calls) == 1                            # 还没到补跑点，等着

    monkeypatch.setattr(s, "_report_succeeded", staticmethod(lambda report: True))
    s._maybe_daily(datetime(2026, 9, 11, 19, 15))     # 补跑点
    assert len(calls) == 2                            # 补跑发生
    s._maybe_daily(datetime(2026, 9, 11, 19, 30))
    assert len(calls) == 2                            # 补跑成功后不再重复


def test_failed_primary_without_fallback_retries(cfg, db, monkeypatch) -> None:
    """没配补跑时间：主跑失败后仍然会在下一轮重试（不至于当天彻底不动）。"""
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "", True
    s, calls = _sched(cfg, db, monkeypatch, succeed=False)
    s._maybe_daily(datetime(2026, 9, 11, 16, 0))
    s._maybe_daily(datetime(2026, 9, 11, 16, 1))
    assert len(calls) == 2


def test_late_start_runs_once(cfg, db, monkeypatch) -> None:
    """程序 20:00 才启动：当天没跑过 → 立刻补一次（主跑点已过）。"""
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _sched(cfg, db, monkeypatch)
    s._maybe_daily(datetime(2026, 9, 11, 20, 0))
    assert len(calls) == 1


def test_status_exposes_autorun_fields(cfg, db) -> None:
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:30", "20:00", True
    s = Scheduler(cfg, DataEngine(db))
    status = s.status(now=datetime(2026, 9, 11, 10, 0))
    assert status["auto_run"] is True
    assert status["run_at"] == "16:30"
    assert status["run_at_fallback"] == "20:00"
    assert status["next_run"]["label"] == "今天 16:30"
    assert status["daily_failed_today"] is False


# ── 写回配置：保留注释与未知键 ──


def test_render_keeps_comments_when_changing_run_time(tmp_path) -> None:
    """改运行时间走的是同一套"行级就地替换"，注释与未知键都要在。"""
    original = '''# 我的配置（注释要留着）
hithink_api_key = "abc"        # 同花顺 Key
run_at = "19:15"               # 每天几点跑

my_own_key = "别动我"
'''
    out = render_config_updates(original, {"run_at": "16:00", "auto_run": True,
                                           "run_at_fallback": "19:15"})
    # 值被替换、原注释保留（对齐空格会规范化成两个空格，内容不变）
    assert 'run_at = "16:00"  # 每天几点跑' in out
    assert 'hithink_api_key = "abc"        # 同花顺 Key' in out      # 没动的行原样保留
    assert "# 我的配置（注释要留着）" in out
    assert 'my_own_key = "别动我"' in out
    assert "auto_run = true" in out
    assert 'run_at_fallback = "19:15"' in out
    # 写回后仍然是合法 TOML，且值读得出来
    path = tmp_path / "config.toml"
    path.write_text(out, encoding="utf-8")
    cfg = load_config(path, use_env=False)
    assert (cfg.run_at, cfg.run_at_fallback, cfg.auto_run) == ("16:00", "19:15", True)


# ── CLI 临时覆盖 ──


@pytest.fixture()
def cli_config(tmp_path, cfg):
    path = tmp_path / "config.toml"
    path.write_text(
        f'# 用户注释\n'
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        'run_at = "16:00"\nrun_at_fallback = "19:15"\nauto_run = true\n',
        encoding="utf-8",
    )
    return path


def test_cli_run_at_override_is_temporary(capsys, cli_config) -> None:
    from laoa_trader.__main__ import cli
    from laoa_trader.intraday import now_shanghai

    before = cli_config.read_text(encoding="utf-8")
    # 用**程序自己的时钟**（北京时间）取"现在"：机器时区不一定是北京（CI 的 runner 是 UTC），
    # 拿本地时间去算"下次自动运行是今天还是明天"会在 UTC 机器上判错。
    started = now_shanghai()
    assert cli(["--cli", "--doctor", "--run-at", "16:30", "--config", str(cli_config)]) == 0
    finished = now_shanghai()
    out = capsys.readouterr().out
    assert "（临时）每天主跑时间：16:30" in out
    assert "每天 16:30（主跑）" in out
    # 「下次自动运行」是几点**取决于当前时刻**，而且有三种形态：
    #   16:30 之前 → 今天 16:30；16:30~19:15 之间（今天还没跑成功）→ 今天 19:15 补跑；
    #   19:15 之后 → 明天 16:30。
    # 这条断言以前写死"明天"，CI 在 UTC 上午跑就红；后来改成只按"今天/明天"算，
    # 又漏了**补跑分支**（16:30~19:15 之间会红）—— 两次都是与代码无关的假失败。
    # 现在直接调 `scheduler.next_run_info`（CLI 用的就是它）算出期望值：
    # 既不放过错误答案，也不依赖机器时区与跑用例的时刻。
    stub = SimpleNamespace(auto_run=True, run_at="16:30", run_at_fallback="19:15")
    allowed = {next_run_info(stub, None, now=moment)["label"] for moment in (started, finished)}
    assert any(f"下次自动运行: {label}" in out for label in allowed), (
        f"下次自动运行应是 {allowed} 之一，实际输出：{out!r}"
    )
    # 临时覆盖**不改配置文件**
    assert cli_config.read_text(encoding="utf-8") == before


def test_cli_no_auto_run(capsys, cli_config) -> None:
    from laoa_trader.__main__ import cli

    assert cli(["--cli", "--doctor", "--no-auto-run", "--config", str(cli_config)]) == 0
    out = capsys.readouterr().out
    assert "每天自动运行已关闭" in out
    assert "自动运行 **关闭**" in out
    assert "下次自动运行: 已关闭" in out


def test_cli_run_at_fallback_override_and_validation(capsys, cli_config) -> None:
    from laoa_trader.__main__ import cli

    assert cli(["--cli", "--doctor", "--run-at-fallback", "20:30",
                "--config", str(cli_config)]) == 0
    assert "（临时）补跑时间：20:30" in capsys.readouterr().out

    # 非法时间 → 明确报错 + 非零退出码（SystemExit(2)）
    with pytest.raises(SystemExit) as excinfo:
        cli(["--cli", "--doctor", "--run-at", "25:99", "--config", str(cli_config)])
    assert excinfo.value.code == 2
    assert "格式不对" in capsys.readouterr().out

    # 补跑 ≤ 主跑 → 同样拒绝
    with pytest.raises(SystemExit):
        cli(["--cli", "--doctor", "--run-at", "16:00", "--run-at-fallback", "15:00",
             "--config", str(cli_config)])
    assert "必须**晚于**主跑时间" in capsys.readouterr().out


def test_cli_doctor_shows_run_times_and_next(capsys, cli_config) -> None:
    from laoa_trader.__main__ import cli

    assert cli(["--cli", "--doctor", "--config", str(cli_config)]) == 0
    out = capsys.readouterr().out
    assert "定时        : 每天 16:00（主跑）；补跑 19:15；自动运行 开" in out
    assert "下次自动运行: " in out


def test_scheduler_uses_overridden_times_from_cli(cfg, db, monkeypatch) -> None:
    """CLI 覆盖写进内存 cfg 后，调度器读到的就是新时间（临时覆盖真的生效）。"""
    from laoa_trader.__main__ import _apply_run_time_override

    class Args:
        run_at = "17:45"
        run_at_fallback = None
        no_auto_run = False

    cfg.run_at, cfg.auto_run = "16:00", True
    _apply_run_time_override(cfg, Args())
    s, calls = _sched(cfg, db, monkeypatch)
    s._maybe_daily(datetime(2026, 9, 11, 17, 0))
    assert calls == []                       # 17:00 还没到 17:45
    s._maybe_daily(datetime(2026, 9, 11, 17, 45))
    assert len(calls) == 1
