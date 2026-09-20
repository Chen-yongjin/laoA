"""命令行入口：`--doctor` / `--pool` / `--once` / `--download` 都不能炸。

为什么值得单独测
----------------
桌面版出问题时用户最先能提供的就是"我在命令行敲了这条命令，出来这堆字"。
所以 CLI 必须做到：**任何异常都转成中文结论 + 非零退出码，绝不吐 traceback**；
`--doctor` 更要在数据目录不可写时照样把问题报出来（那正是它存在的意义）。

全部离线：无 Key 时走"结构化失败 + 用本地库继续选股"，不会联网
（同时受 `conftest._block_network` 保护）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from laoa_trader.__main__ import cli
from laoa_trader.data import storage

from tests._toml import p
from tests.conftest import READY_THRESHOLDS, seed_ready_db, workdays_ending


@pytest.fixture()
def config_file(tmp_path, cfg):
    """指向临时数据目录的 config.toml（含让合成小库能通过自检的宽松阈值）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        "notify_feishu = false\nnotify_windows = false\nnotify_tray = false\n"
        + READY_THRESHOLDS,
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture()
def ready_config(tmp_path, ready_cfg):
    """指向**已就绪**数据目录的 config.toml（自检 ready，可以直接跑正题）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{p(ready_cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        "notify_feishu = false\nnotify_windows = false\nnotify_tray = false\n"
        + READY_THRESHOLDS,
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture()
def blocked_config(tmp_path):
    """数据目录**建不出来**的配置：父路径是个文件（跨平台都必然失败）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我不是目录", encoding="utf-8")
    path = tmp_path / "blocked.toml"
    path.write_text(f'data_dir = "{p(blocker / "data")}"', encoding="utf-8")
    return str(path)


# ── --doctor ──


def test_doctor_reports_paths_deps_and_data(capsys, config_file) -> None:
    assert cli(["--cli", "--doctor", "--config", config_file]) == 0
    out = capsys.readouterr().out
    assert "老牛选股助手 —— 自检" in out
    for key in ("程序版本", "Python", "配置来源", "数据目录", "数据库", "同花顺 Key",
                "飞书凭证", "通知开关", "交易参数", "定时", "行情行数", "最新数据日期"):
        assert key in out, f"自检报告缺少「{key}」"
    assert "Traceback" not in out


def test_doctor_never_leaks_credentials(capsys, tmp_path) -> None:
    """Key 只显示前 4 位（排障要把输出贴给别人看，不能把 Key 一起送出去）。"""
    path = tmp_path / "with-key.toml"
    path.write_text(
        f'data_dir = "{p(tmp_path / "data")}"\nhithink_api_key = "SUPER-SECRET-KEY-123456"\n',
        encoding="utf-8",
    )
    assert cli(["--cli", "--doctor", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "SUPER-SECRET-KEY-123456" not in out
    assert "SUPE…56" in out


def test_broken_config_is_announced(capsys, tmp_path) -> None:
    """配置文件解析失败时，CLI 要明说（而不是安静地用默认值跑）。"""
    path = tmp_path / "broken.toml"
    path.write_text('data_dir = "/tmp/x"\n[中文表]\n', encoding="utf-8")
    assert cli(["--cli", "--doctor", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "配置告警" in out
    assert "解析失败" in out
    assert "Traceback" not in out


def test_doctor_survives_unwritable_data_dir(capsys, blocked_config) -> None:
    """数据目录不可写时，自检**照样出报告**（而不是甩一句错就退出）。"""
    assert cli(["--cli", "--doctor", "--config", blocked_config]) == 0
    out = capsys.readouterr().out
    assert "数据目录不可用" in out
    assert "数据概况    : 跳过" in out
    assert "Traceback" not in out


# ── --pool ──


def test_pool_on_empty_db_refuses_with_reason(capsys, config_file) -> None:
    """空库跑 `--pool`：明确报错退出（**不许静默跑出空结果**）。"""
    assert cli(["--cli", "--pool", "--config", config_file]) == 1
    out = capsys.readouterr().out
    assert "数据自检：needs_full" in out
    assert "本地没有可用的历史数据" in out
    assert "--download" in out          # 给出可执行的下一步
    assert "Traceback" not in out


def test_pool_prints_saved_pool(capsys, ready_config, ready_cfg) -> None:
    with storage.connect(ready_cfg.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600002", "name": "高价样本", "score": 3.0,
             "strategy": "LowPriceStrategy", "strategies": "LowPriceStrategy",
             "reason": "低价股·热门行业白酒"},
        ], "2026-09-11")
    assert cli(["--cli", "--pool", "--config", ready_config]) == 0
    out = capsys.readouterr().out
    assert "数据自检：ready" in out
    assert "股票池（1 只，2026-09-11）" in out
    assert "高价样本(600002)" in out


def test_pool_with_unwritable_dir_never_tracebacks(capsys, blocked_config) -> None:
    code = cli(["--cli", "--pool", "--config", blocked_config])
    out = capsys.readouterr().out
    assert code == 1
    assert "数据目录" in out
    assert "Traceback" not in out


# ── --once / --download ──


def test_once_without_key_runs_local_pipeline(capsys, ready_config, ready_cfg) -> None:
    """没配 Key 也能跑完整流程：数据同步失败→记录，选股建池照跑，不推送。"""
    assert cli(["--cli", "--once", "--no-notify", "--config", ready_config]) == 0
    out = capsys.readouterr().out
    assert "数据自检：ready" in out
    assert "数据日期：" in out
    assert "股票池" in out
    assert "Traceback" not in out


def test_download_without_key_is_structured_failure(capsys, config_file) -> None:
    """缺 Key：给出中文原因 + 非零退出码（不是 traceback）。"""
    assert cli(["--cli", "--download", "--config", config_file]) == 1
    out = capsys.readouterr().out
    assert "❌" in out
    assert "API Key" in out
    assert "Traceback" not in out


def test_writes_are_refused_when_data_dir_unwritable(capsys, blocked_config) -> None:
    """要写数据的命令（--download/--once）在目录不可写时应明确终止。"""
    assert cli(["--cli", "--once", "--config", blocked_config]) == 1
    out = capsys.readouterr().out
    assert "数据目录不可用，已终止" in out
    assert "Traceback" not in out


def test_help_and_no_args(capsys) -> None:
    """不带动作（或 --help）时打印帮助并正常退出，不能因为数据目录不可写就报错。"""
    assert cli(["--cli"]) == 0
    assert "老牛选股助手" in capsys.readouterr().out

    # --help 由 argparse 直接 SystemExit(0)，这是标准行为（退出码 0）
    with pytest.raises(SystemExit) as excinfo:
        cli(["--help"])
    assert excinfo.value.code == 0
    assert "--doctor" in capsys.readouterr().out


# ── 自选股命令行 ──


@pytest.fixture()
def watch_db(cfg):
    """带名称缓存的小库（**自检 ready**）：600519 查得到名字，601999 查不到。"""
    seed_ready_db(cfg, symbols=(("600519", "贵州样本", "白酒"),), days=30)
    return cfg


def test_watchlist_add_autofills_name(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    assert cli(["--cli", "--watchlist", "add", "600519", "--note", "龙头",
                "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "已加入自选股：600519 贵州样本" in out
    assert "龙头" in out
    with storage.connect(watch_db.db_path) as conn:
        rows = storage.load_watchlist(conn)
    assert rows[0]["name"] == "贵州样本"
    assert rows[0]["note"] == "龙头"


def test_watchlist_add_unknown_symbol_warns_but_adds(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    assert cli(["--cli", "--watchlist", "add", "601999", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "本地库里没有 601999 的名称" in out
    assert "仍按你给的信息添加" in out
    with storage.connect(watch_db.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["601999"]


def test_watchlist_add_warns_when_over_limit(capsys, watch_db, tmp_path) -> None:
    """超过 watchlist_max 要提示（不静默丢弃）。"""
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(watch_db.data_dir)}"\nwatchlist_max = 1\n' + READY_THRESHOLDS,
        encoding="utf-8")
    cli(["--cli", "--watchlist", "add", "600519", "--config", str(config)])
    capsys.readouterr()
    assert cli(["--cli", "--watchlist", "add", "601999", "--config", str(config)]) == 0
    assert "已超过上限 1" in capsys.readouterr().out


def test_watchlist_list_shows_status_and_pool(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    cli(["--cli", "--watchlist", "add", "600519", "--note", "龙头", "--config", str(config)])
    capsys.readouterr()
    assert cli(["--cli", "--watchlist", "list", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "自选股（1 只，上限 20）" in out
    assert "600519 贵州样本" in out
    assert "启用" in out and "未进池" in out
    assert "备注 龙头" in out


def test_watchlist_list_warns_when_monitoring_off(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(watch_db.data_dir)}"\nwatchlist_in_pool = false\n'
        + READY_THRESHOLDS, encoding="utf-8")
    cli(["--cli", "--watchlist", "add", "600519", "--config", str(config)])
    capsys.readouterr()
    assert cli(["--cli", "--watchlist", "list", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "不监控（watchlist_in_pool=false）" in out
    assert "只记录" in out


def test_watchlist_disable_enable_remove(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    cli(["--cli", "--watchlist", "add", "600519", "--config", str(config)])
    capsys.readouterr()

    assert cli(["--cli", "--watchlist", "disable", "600519", "--config", str(config)]) == 0
    assert "不进池、不监控" in capsys.readouterr().out
    with storage.connect(watch_db.db_path) as conn:
        assert storage.watchlist_symbols(conn) == []          # 停用 → 不启用
        assert len(storage.load_watchlist(conn)) == 1          # 但保留在列表里

    assert cli(["--cli", "--watchlist", "enable", "600519", "--config", str(config)]) == 0
    with storage.connect(watch_db.db_path) as conn:
        assert storage.watchlist_symbols(conn) == ["600519"]

    assert cli(["--cli", "--watchlist", "remove", "600519", "--config", str(config)]) == 0
    with storage.connect(watch_db.db_path) as conn:
        assert storage.load_watchlist(conn) == []
    # 再删一次：走"未找到"分支，退出码 1
    assert cli(["--cli", "--watchlist", "remove", "600519", "--config", str(config)]) == 1


def test_watchlist_unknown_action_and_missing_symbol(capsys, watch_db, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    assert cli(["--cli", "--watchlist", "wat", "--config", str(config)]) == 1
    assert "未知动作" in capsys.readouterr().out
    assert cli(["--cli", "--watchlist", "add", "--config", str(config)]) == 1
    assert "需要代码" in capsys.readouterr().out
    assert cli(["--cli", "--watchlist", "add", "abc", "--config", str(config)]) == 1
    assert "代码格式不对" in capsys.readouterr().out
    assert cli(["--cli", "--watchlist", "enable", "600519", "--config", str(config)]) == 1
    assert "未找到自选股" in capsys.readouterr().out


def test_pool_output_shows_source_and_note(capsys, watch_db, tmp_path) -> None:
    """`--pool` 要能看出来源与备注（策略 / 自选 / 策略+自选）。"""
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(watch_db.data_dir)}"\n' + READY_THRESHOLDS,
                      encoding="utf-8")
    with storage.connect(watch_db.db_path) as conn:
        storage.save_pool(conn, [
            {"symbol": "600519", "name": "贵州样本", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy", "score": 3.0, "reason": "低价股"},
            {"symbol": "601999", "name": "新股样本", "reason": "自选（龙头）"},
        ], "2026-09-11")
        storage.upsert_watchlist(conn, "601999", name="新股样本", note="龙头")
    assert cli(["--cli", "--pool", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    # 改版后「来源」回答的是**哪条策略**（`策略·低价股`），组别与持有期单独一栏 ——
    # 界面「来源」列也是这个口径，两处说的是同一件事
    assert "来源 策略·低价股" in out
    assert "组别 —" in out            # 2026-09-18 起没有"策略组"了（公式没有组别）
    assert "来源 自选" in out                        # 纯自选
    assert "组别 —" in out                           # 自选没有组别（不留空、写 `—`）
    assert "备注 龙头" in out                        # 备注
    assert "只出现一行" in out                       # 来源说明
    # 策略名**只在「来源」那一栏出现一次**：原来"名称后面紧跟策略名，来源栏再写组名"
    # 的口径已经合并，不能再退回"同一件事说两遍"（`｜低价股` 那个是**理由**字段，不算）
    assert "贵州样本(600519)｜来源 策略·低价股" in out
    assert out.count("来源 策略·低价股") == 1


# ── 运行时自检闸门 ──


def _ready_with_lag(cfg, lag: int, *, min_history_years: float = 0.0) -> str:
    """造一个"最新行情日落后今天 lag 个交易日"的就绪库，返回今天。

    注意必须**相对真实今天**构造：过期数据在"未来"的话，自检会认为没落后
    （日历里晚于最新行情、且不晚于今天的才计入落后天数）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    calendar = workdays_ending(today, 40 + lag)          # 含今天在内的交易日
    data_days = calendar[:-lag] if lag else calendar
    cfg.min_history_years = min_history_years
    cfg.min_symbols = 1
    seed_ready_db(cfg, days=len(data_days), trading_days=data_days)
    with storage.connect(cfg.db_path) as conn:
        storage.write_calendar(conn, calendar)           # 日历覆盖到"今天"
    return today


def _count_dump_calls(monkeypatch) -> list[str]:
    """把 CLI 内部创建的客户端换成"记账"的假客户端，返回调用记录列表。"""
    from laoa_trader.data import sync as sync_mod

    from tests.conftest import FakeClient

    client = FakeClient()
    monkeypatch.setattr(sync_mod, "make_client", lambda cfg=None, **kw: client)
    return client.calls


def test_once_on_ready_db_never_downloads(capsys, ready_config, monkeypatch) -> None:
    """**核心断言**：数据就绪时跑 `--once`，一次 dump 请求都不发。"""
    from laoa_trader.data import sync as sync_mod

    calls = _count_dump_calls(monkeypatch)
    monkeypatch.setattr(sync_mod, "daily_update", lambda *a, **k: [])
    assert cli(["--cli", "--once", "--no-notify", "--config", ready_config]) == 0
    out = capsys.readouterr().out
    assert "数据自检：ready" in out
    assert [c for c in calls if c[0] == "download_dump"] == []      # 零次下载
    assert calls == []                                              # 连客户端都没用上


def test_pool_on_ready_db_never_downloads(capsys, ready_config, monkeypatch) -> None:
    calls = _count_dump_calls(monkeypatch)
    assert cli(["--cli", "--pool", "--config", ready_config]) == 0
    assert [c for c in calls if c[0] == "download_dump"] == []


def test_download_on_ready_db_skips(capsys, ready_config, monkeypatch) -> None:
    """`--download` 在就绪时也不再重下（否则每次都会跑 20 分钟）。"""
    calls = _count_dump_calls(monkeypatch)
    assert cli(["--cli", "--download", "--config", ready_config]) == 0
    out = capsys.readouterr().out
    assert "无需重新下载" in out
    assert "--force-download" in out
    assert [c for c in calls if c[0] == "download_dump"] == []


def test_needs_full_without_auto_download_errors(capsys, config_file) -> None:
    """空库跑 `--once`：非零退出 + 中文原因 + 无 traceback（不许静默跑空结果）。"""
    code = cli(["--cli", "--once", "--no-notify", "--config", config_file])
    out = capsys.readouterr().out
    assert code == 1
    assert "数据自检：needs_full" in out
    assert "本地没有可用的历史数据" in out
    assert "--download" in out
    assert "Traceback" not in out


def test_needs_full_with_auto_download_downloads_first(capsys, config_file, cfg,
                                                       monkeypatch) -> None:
    """`--once --auto-download`：**先下载再继续**（用假客户端验证调用顺序）。"""
    from laoa_trader.data import sync as sync_mod

    from tests.conftest import FakeClient

    order: list[str] = []
    client = FakeClient()

    def fake_make_client(cfg_=None, **kw):
        order.append("make_client")
        return client

    def fake_download_history(cfg_, **kwargs):
        order.append("download_history")
        seed_ready_db(cfg_, days=40)          # 模拟下好了
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=10,
                                   detail="写入 10 行")

    monkeypatch.setattr(sync_mod, "make_client", fake_make_client)
    monkeypatch.setattr(sync_mod, "download_history", fake_download_history)
    monkeypatch.setattr(sync_mod, "daily_update", lambda *a, **k: [])
    assert cli(["--cli", "--once", "--no-notify", "--auto-download",
                "--config", config_file]) == 0
    out = capsys.readouterr().out
    assert order[0] == "download_history"     # 先下载
    assert "需要下载 10 年全量历史" in out
    assert "下载完成" in out
    assert "股票池" in out                     # 下载后继续跑了正题
    assert "Traceback" not in out


def test_needs_incremental_auto_updates_on_start(capsys, tmp_path, cfg, monkeypatch) -> None:
    """落后几天 + auto_download_on_start=true → 自动跑增量（1 次请求），然后继续。"""
    from laoa_trader.data import sync as sync_mod

    _ready_with_lag(cfg, 3)
    calls: list[str] = []
    monkeypatch.setattr(sync_mod, "sync_daily",
                        lambda *a, **k: calls.append("sync_daily") or
                        sync_mod.SyncResult(stage="日更数据", ok=True, rows=2,
                                            detail="写入 2 行"))
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n'
        "auto_download_on_start = true\nnotify_channels = []\n" + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "落后 3 个交易日" in out
    assert calls == ["sync_daily"]
    assert "数据地址" not in out
    assert "Traceback" not in out


def test_needs_incremental_respects_config_off(capsys, tmp_path, cfg, monkeypatch) -> None:
    """auto_download_on_start=false：只提示、不自动下载（但允许继续用旧数据）。"""
    from laoa_trader.data import sync as sync_mod

    _ready_with_lag(cfg, 2)
    monkeypatch.setattr(sync_mod, "sync_daily",
                        lambda *a, **k: pytest.fail("配置关掉了就不该自动下载"))
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = ""\n'
        "auto_download_on_start = false\nnotify_channels = []\n" + READY_THRESHOLDS,
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "落后 2 个交易日" in out
    assert "--auto-download" in out            # 告诉用户怎么现在补齐
    assert "基于 2 个交易日前的数据" in out     # 明确提醒结论的时效


def test_doctor_reports_preflight(capsys, ready_config) -> None:
    """`--doctor` 要打印三态结论与各项指标。"""
    assert cli(["--cli", "--doctor", "--config", ready_config]) == 0
    out = capsys.readouterr().out
    assert "数据自检    : ✅ ready" in out
    assert "行情行数  :" in out
    assert "历史跨度  :" in out
    assert "落后交易日:" in out
    assert "复权事件  :" in out
    assert "需要下载  : none" in out


def test_doctor_reports_needs_full(capsys, config_file) -> None:
    assert cli(["--cli", "--doctor", "--config", config_file]) == 0
    out = capsys.readouterr().out
    assert "数据自检    : ❌ needs_full" in out
    assert "需要下载  : full" in out
