"""调度器数据闸门：**数据没就绪（或正在下载）时，绝不跑策略/建池/推通知**。

对应用户实际遇到的 bug（缺陷单第 3 项）：「下载历史数据没有显示进度，
但是已经运行弹出策略自动选股，选了一只股票了」——
根因是 `Scheduler._maybe_daily()` 直接 `run_daily_now()`，既没走自检、
也没有"正在下载"的互斥，于是在空库/半截数据上跑出了**看似正常但错误**的结果。

这一组的由来（用户实测的分发前严重 bug）
--------------------------------------
程序启动 → 自检 `needs_full` → 弹首次向导开始下载 ✓；
**同时**调度线程因为"今天已过主跑时间且今天没跑过"触发了日常选股 ✗
→ 在**只写了一半的库**上跑策略 → 选出 1 只 → 建池（还可能推了通知）。

两道闸门（`scheduler.data_gate` + `Scheduler._maybe_daily` / `_block_daily`）：

1. **正在下载**（`state.is_downloading()`）→ 跳过，且**不写"今天已完成"标记**；
2. **本地数据 `needs_full`** → 跳过，同样不写标记 —— 所以数据下好之后
   当天仍然会补跑一次（用户预期："下完就该自动选一次"）。

手动入口（界面【立即选股并建池】、CLI `--once`）走同一口径：**明确拒绝 + 指路**，
而不是"静默跑出个空池子"。

全部离线：合成小库 + 假客户端，不联网（另有 `conftest._block_network` 兜底）。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from laoa_trader import scheduler as sched
from laoa_trader import state
from laoa_trader.config import Config, load_config
from laoa_trader.data import preflight, storage
from laoa_trader.data.engine import DataEngine
from tests._toml import p
from tests.conftest import messages, seed_ready_db, workdays_ending


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _moment_after_run_at(hour: int = 16, minute: int = 30) -> datetime:
    """"今天已过主跑时间"的那个时刻（相对真实时钟算，避免用例随日期腐坏）。"""
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _clean_download_flag():
    """每个用例前后都确保"下载中"这面旗是干净的（它是进程级全局状态）。"""
    state.reset()
    yield
    state.reset()


def _ready_db(cfg: Config, days: int = 30) -> list[str]:
    """把库写成"自检 ready"（数据到最近工作日，门槛按小样本放低）。"""
    cfg.min_symbols = 1
    cfg.min_history_years = 0.0
    trading = workdays_ending(_today(), days)
    seed_ready_db(cfg, trading_days=trading)
    return trading


def _stale_db(cfg: Config, lag: int = 2, days: int = 30) -> list[str]:
    """数据落后 `lag` 个交易日：行情只到 `lag` 天前，**日历延伸到今天**。"""
    cfg.min_symbols = 1
    cfg.min_history_years = 0.0
    recent = workdays_ending(_today(), days)
    rows_days = recent[:-lag]
    seed_ready_db(cfg, trading_days=rows_days)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, recent)      # 日历往后补 → 判定"落后 lag 天"
    return rows_days


def _scheduler(cfg: Config, db: str, monkeypatch, *, succeed: bool = True):
    """造一个调度器，并把日更换成"记账"的假实现（真实流程另有用例覆盖）。"""
    obj = sched.Scheduler(cfg, DataEngine(db))
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    calls: list[str] = []

    def fake_run(**kwargs):
        calls.append("run_daily_now")
        report = {"errors": [] if succeed else ["模拟失败"], "pool": [], "picks": 0}
        obj.mark_daily_ran(report)
        return report

    monkeypatch.setattr(obj, "run_daily_now", fake_run)
    return obj, calls


# ── 1) needs_full：自动任务不跑、不写成功标记、日志说清原因 ──


def test_needs_full_blocks_scheduled_run(cfg, monkeypatch, log_records) -> None:
    """空库 + 已过主跑时间 → **一次都不跑**，并明确写出中文原因。"""
    storage.init_db(cfg.db_path)                     # 空库：必然 needs_full
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _scheduler(cfg, str(cfg.db_path), monkeypatch)

    s._maybe_daily(_moment_after_run_at())

    assert calls == []                               # 策略/建池一次都没跑
    assert s._last_daily_date is None                # **没写"今天已完成"标记**
    assert s._daily_failed_date is None              # 也不算"失败"（那是另一回事）
    text = messages(log_records)
    assert "本地数据不可用" in text and "跳过本次自动选股" in text
    assert "needs_full" not in text                  # 给用户看的是中文，不是状态码
    assert "复权事件" in text or "还没有数据库" in text or "空的" in text   # 说清具体原因
    assert "请先下载" in text                          # 日志里有下一步（不用翻文档猜）
    assert "【下载数据】" in text                      # 而且指名道姓是哪个按钮
    assert "【刷新数据】" in text                      # 轻量项（行业/日历）那条路也写清楚
    st = s.status()
    assert "数据不可用" in st["skipped_reason"]       # 状态栏能直接显示原因
    assert "跳过本次自动选股" in st["skipped_reason"]
    assert st["preflight_status"] == "needs_full"    # 自检结论也暴露出来
    assert st["daily_skipped_today"] is True         # 记下"今天还没跑"（不是失败、更不是已完成）
    assert st["last_daily_date"] is None             # 但**没有**被永久标记成今天已跑


def test_needs_full_can_still_run_after_data_is_ready(cfg, monkeypatch) -> None:
    """被拦下之后**当天**补好数据 → 下一个滴答就正常跑（拦截不是"今天放弃"）。"""
    storage.init_db(cfg.db_path)
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _scheduler(cfg, str(cfg.db_path), monkeypatch)

    s._maybe_daily(_moment_after_run_at())
    assert calls == []

    _ready_db(cfg)                                   # 模拟"下载完成了"
    assert s.status()["skipped_reason"] is not None     # 上一次的原因还留着（给用户看）
    s._maybe_daily(_moment_after_run_at(minute=31))
    assert calls == ["run_daily_now"]                 # 数据就绪后当天照样补跑
    assert s._last_daily_date == _today()
    assert s.status()["skipped_reason"] is None         # 跑成了 → 状态栏不再挂着"被拦住"


def test_blocked_log_is_throttled(cfg, monkeypatch, log_records) -> None:
    """调度每秒滴答一次：同一个原因不能把日志刷爆（节流 5 分钟）。"""
    storage.init_db(cfg.db_path)
    cfg.run_at, cfg.auto_run = "16:00", True
    s, _ = _scheduler(cfg, str(cfg.db_path), monkeypatch)
    for _ in range(50):
        s._maybe_daily(_moment_after_run_at())
    hits = [r for r in log_records if "跳过本次自动选股" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in hits]


# ── 2) 正在下载：跳过 ──


def test_downloading_blocks_scheduled_run(cfg, monkeypatch, log_records) -> None:
    """下载进行中 → 自动选股直接跳过（**即使数据本来就是 ready**），且不写标记。"""
    _ready_db(cfg)
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _scheduler(cfg, str(cfg.db_path), monkeypatch)

    state.begin_download()
    try:
        s._maybe_daily(_moment_after_run_at())
        assert calls == []
        assert s._last_daily_date is None
        assert "正在下载历史数据" in messages(log_records)
    finally:
        state.end_download()

    s._maybe_daily(_moment_after_run_at(minute=31))
    assert calls == ["run_daily_now"]                 # 下载结束 → 正常跑


def test_download_flag_is_raised_during_real_download(cfg, monkeypatch) -> None:
    """真下载（`sync.download_dump`）期间旗子必须飘着 —— 这是闸门的输入。"""
    from laoa_trader.data import hithink as hx
    from laoa_trader.data import sync as sync_mod

    cfg.ensure_dirs()
    seen: list[bool] = []

    class Recorder:
        def download_dump(self, tag, dest=None, **kwargs):
            seen.append(state.is_downloading())      # 下载中必须是 True
            kwargs["progress_cb"](10, 20)
            return dest

    # 免掉"必须是真 Parquet"这一步（那个逻辑另有专门用例），这里只验证旗子
    monkeypatch.setattr(hx, "parquet_ok", lambda *a, **k: True)
    assert state.is_downloading() is False
    sync_mod.download_dump(cfg, "daily-k", Recorder(), force=True)
    assert seen == [True]
    assert state.is_downloading() is False           # 结束后必须清旗


def test_download_flag_is_cleared_even_when_download_fails(cfg, monkeypatch) -> None:
    """下载抛异常时旗子也要清掉（否则调度会被**永远**拦住）。"""
    from laoa_trader.data import hithink as hx
    from laoa_trader.data import sync as sync_mod

    seen: list[bool] = []

    def boom(cfg_, tag, client, **kwargs):
        seen.append(state.is_downloading())
        raise hx.HithinkError(-1, "模拟下载失败")

    monkeypatch.setattr(sync_mod, "download_dump", boom)
    result = sync_mod.download_history(cfg, client=object(), include_names=False)
    assert result.ok is False
    assert seen == [True]                            # 失败发生在"下载中"
    assert state.is_downloading() is False           # 但旗子已清 → 不会永久拦住调度


# ── 3) ready：正常跑（回归保护）──


def test_ready_runs_normally(cfg, monkeypatch) -> None:
    """数据 ready → 定时任务照常执行（新增的闸门不能把正常路径也拦掉）。"""
    _ready_db(cfg)
    cfg.run_at, cfg.run_at_fallback, cfg.auto_run = "16:00", "19:15", True
    s, calls = _scheduler(cfg, str(cfg.db_path), monkeypatch)

    s._maybe_daily(_moment_after_run_at())
    assert calls == ["run_daily_now"]
    assert s._skipped_reason is None
    assert s.status()["preflight_status"] == "ready"
    assert s.status()["daily_skipped_today"] is False
    assert s._last_daily_date == _today()


# ── 4) needs_incremental：先增量、再选股 ──


def test_needs_incremental_gate_allows_run(cfg) -> None:
    """落后 2 个交易日 → 闸门仍放行（数据可用，只是不新鲜）。"""
    _stale_db(cfg, lag=2, days=20)
    result = preflight.check(cfg.db_path, cfg)
    assert result["status"] == preflight.NEEDS_INCREMENTAL
    assert result["stale_trading_days"] == 2, result

    gate = sched.data_gate(cfg, DataEngine(cfg.db_path))
    assert gate["ok"] is True
    assert gate["status"] == preflight.NEEDS_INCREMENTAL


@pytest.mark.parametrize("auto_download", [True, False])
def test_needs_incremental_syncs_before_strategies(cfg, monkeypatch,
                                                   auto_download) -> None:
    """`needs_incremental` → **先增量、再建池**（不管 auto_download_on_start 怎么配）。

    `run_daily(with_data=True)` 本身就是"先数据后策略"，所以这条顺序是产品的硬约定：
    落库的行情必须先补齐，池子才会按最新数据算。
    """
    auto_download_now = datetime.now()
    _stale_db(cfg, lag=1, days=20)
    cfg.auto_download_on_start = auto_download
    assert preflight.check(cfg.db_path, cfg)["status"] == preflight.NEEDS_INCREMENTAL

    order: list[str] = []
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(sched.sync, "daily_update",
                        lambda *a, **k: (order.append("增量"), [])[1])
    monkeypatch.setattr(sched.pool, "build_pool",
                        lambda *a, **k: (order.append("建池"), [])[1])
    obj = sched.Scheduler(cfg, DataEngine(cfg.db_path))
    obj._maybe_daily(auto_download_now.replace(hour=16, minute=30))

    assert order == ["增量", "建池"], order            # 增量必须排在策略/建池之前
    assert obj._last_daily_date is not None


def test_needs_incremental_without_auto_download_still_runs(cfg, monkeypatch,
                                                           log_records) -> None:
    """`needs_incremental` + `auto_download_on_start=false`：**照常跑**，但先增量、且日志说明。

    为什么这样定：落后几个交易日的数据**是可用的**（自检就把它当"能用但不新鲜"），
    所以不该像 `needs_full` 那样整轮跳过；而日更流程的第一步本来就是增量同步，
    于是"先补数据再选股"这个顺序天然成立。区别只在日志里说清
    "没有因为 auto_download_on_start=false 而额外去补数据"。
    """
    _stale_db(cfg, lag=1, days=20)
    cfg.auto_download_on_start = False
    assert preflight.check(cfg.db_path, cfg)["status"] == preflight.NEEDS_INCREMENTAL

    order: list[str] = []
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(sched.sync, "daily_update",
                        lambda *a, **k: (order.append("增量"), [])[1])
    monkeypatch.setattr(sched.pool, "build_pool",
                        lambda *a, **k: (order.append("建池"), [])[1])
    obj = sched.Scheduler(cfg, DataEngine(cfg.db_path))
    obj._maybe_daily(_moment_after_run_at())

    assert order == ["增量", "建池"], order
    assert obj._last_daily_date == _today()
    text = messages(log_records)
    assert "auto_download_on_start=false" in text      # 日志说明为什么没去额外补数据
    assert "先增量再选股" in text
    assert obj.status()["skipped_reason"] is None       # 这一轮不是"被跳过"


def test_needs_incremental_with_auto_download_logs_why(cfg, monkeypatch,
                                                      log_records) -> None:
    """`auto_download_on_start=true` 的同一场景：日志说法不同（说明会补数据）。"""
    _stale_db(cfg, lag=1, days=20)
    cfg.auto_download_on_start = True
    monkeypatch.setattr(sched.intraday, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(sched.sync, "daily_update", lambda *a, **k: [])
    monkeypatch.setattr(sched.pool, "build_pool", lambda *a, **k: [])
    obj = sched.Scheduler(cfg, DataEngine(cfg.db_path))
    obj._maybe_daily(_moment_after_run_at())

    text = messages(log_records)
    assert "auto_download_on_start=true" in text
    assert obj.status()["preflight_status"] == "needs_incremental"


# ── 5) 手动入口：明确拒绝 + 指路，且真的没跑策略 ──


def test_cli_once_refuses_on_needs_full(capsys, tmp_path, cfg, monkeypatch) -> None:
    """`--once` 在 needs_full 下：退出码 1 + 中文指路，**且真的没跑策略**。"""
    from laoa_trader.__main__ import cli

    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n'
        "min_history_years = 4.5\nmin_symbols = 4000\n",
        encoding="utf-8",
    )
    ran: list[str] = []
    monkeypatch.setattr(sched, "run_daily", lambda *a, **k: ran.append("run_daily"))

    code = cli(["--cli", "--once", "--no-notify", "--config", str(config)])
    out = capsys.readouterr().out
    assert code == 1
    assert ran == []                                   # 策略一次都没跑
    assert "本地没有可用的历史数据" in out
    assert "--download" in out                         # 给出可执行的下一步
    assert "不会跑策略" in out
    assert "Traceback" not in out


def test_cli_once_has_its_own_gate(capsys, tmp_path, cfg, monkeypatch) -> None:
    """`--once` 的第二道闸门：跑之前**再查一次**，不能只信启动时那一次自检。

    构造：第一次 `preflight.check`（启动自检）返回"就绪"，之后返回真实结论（needs_full）——
    对应"两次检查之间数据目录被清空/库被换掉"。策略绝不能跑。
    """
    from laoa_trader.__main__ import cli
    from laoa_trader.data import preflight as pf

    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n'
        "min_history_years = 0\nmin_symbols = 1\n",
        encoding="utf-8",
    )
    storage.init_db(cfg.db_path)                       # 空库 → 真实结论是 needs_full
    real_check = pf.check
    calls = {"n": 0}

    def flaky_check(db_path, cfg_=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:                            # 启动自检那一次：假装刚查过、就绪
            return {"status": "ready", "reason": "（模拟）刚检查过", "needs_download": "none"}
        return real_check(db_path, cfg_, **kwargs)

    monkeypatch.setattr(pf, "check", flaky_check)
    ran: list[str] = []
    monkeypatch.setattr(sched, "run_daily", lambda *a, **k: ran.append("run_daily"))

    code = cli(["--cli", "--once", "--no-notify", "--config", str(config)])
    out = capsys.readouterr().out
    assert calls["n"] >= 2, "`--once` 必须自己再查一次数据"
    assert code == 1
    assert ran == []
    assert "请先下载" in out
    assert "Traceback" not in out


# ── 闸门本身（界面/CLI/调度共用的纯函数）──


def test_gate_allows_ready(cfg) -> None:
    """ready → 放行，并把原始自检结果带出来（界面缓存/状态栏要用）。"""
    _ready_db(cfg)
    gate = sched.data_gate(cfg, DataEngine(cfg.db_path))
    assert gate["ok"] is True
    assert gate["status"] == preflight.READY
    assert gate["result"]["status"] == preflight.READY
    assert gate["message"]


def test_gate_blocks_and_explains_on_needs_full(cfg) -> None:
    """needs_full → 拦住，且 message 里有原因 + 下一步（界面直接显示这句话）。"""
    storage.init_db(cfg.db_path)
    gate = sched.data_gate(cfg, DataEngine(cfg.db_path))
    assert gate["ok"] is False
    assert gate["status"] == preflight.NEEDS_FULL
    assert "本地无可用历史数据" in gate["message"]
    assert "下载" in gate["message"]                    # 指路：怎么解决
    assert gate["reason"]


def test_gate_blocks_on_config_contradiction(cfg) -> None:
    """配置自相矛盾（min_history_years ≥ history_years）→ 拦住并说明是配置问题。"""
    _ready_db(cfg)
    cfg.history_years, cfg.min_history_years = 5.0, 9.0
    gate = sched.data_gate(cfg, DataEngine(cfg.db_path))
    assert gate["ok"] is False
    assert "min_history_years" in gate["reason"]


def test_gate_survives_broken_db(cfg) -> None:
    """库文件损坏时闸门也要给出结论（不能抛异常把界面/调度带崩）。"""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.db_path.write_bytes(b"this is not a sqlite file")
    gate = sched.data_gate(cfg, DataEngine(cfg.db_path))
    assert gate["ok"] is False
    assert gate["reason"]


def test_doctor_still_works_on_empty_db(capsys, tmp_path, cfg) -> None:
    """`--doctor` 是排障用的只读命令：空库也要能出报告（闸门不该影响它）。"""
    from laoa_trader.__main__ import cli

    storage.init_db(cfg.db_path)
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n',
        encoding="utf-8",
    )
    assert cli(["--cli", "--doctor", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "自检" in out and "Traceback" not in out


def test_default_thresholds_are_not_silently_relaxed(tmp_path) -> None:
    """回归保护：默认门槛必须还是"4000 只 / 4.5 年"（别为了过测试偷偷放松）。"""
    cfg = load_config(tmp_path / "nope.toml", use_env=False)
    assert cfg.min_symbols == 4000
    assert cfg.min_history_years == 4.5


def test_log_collector_fixture_works(log_records) -> None:
    """自检：`log_records` 真的抓得到 `laoa_trader` 的日志（否则上面的日志断言是假绿）。"""
    logging.getLogger("laoa_trader.demo").warning("中文原因测试")
    assert "中文原因测试" in messages(log_records)


def test_stale_helper_produces_expected_lag(cfg) -> None:
    """自检：`_stale_db` 造出来的库确实"落后 N 个交易日"（否则增量用例是假绿）。"""
    _stale_db(cfg, lag=3, days=20)
    assert preflight.check(cfg.db_path, cfg)["stale_trading_days"] == 3
