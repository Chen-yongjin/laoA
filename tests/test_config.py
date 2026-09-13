"""配置读取：config.toml 优先、环境变量覆盖、默认值兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from laoa_trader import config as config_mod
from laoa_trader.config import load_config

from tests._toml import p


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults() -> None:
    cfg = load_config(use_env=False)
    assert cfg.run_at == "16:00"              # 新默认：收盘后主跑
    assert cfg.run_at_fallback == "19:15"     # 主跑没成功时的补跑
    assert cfg.auto_run is True
    assert cfg.intraday_interval == 60
    assert cfg.stop_loss == 0.05
    assert cfg.take_profit == 0.10
    assert (cfg.trade_capital, cfg.trade_position_pct, cfg.trade_max_positions) == (
        100000.0, 0.2, 5)
    assert cfg.notify_feishu is True and cfg.notify_windows is True and cfg.notify_tray is True
    assert cfg.hithink_api_key == ""


def test_reads_toml(tmp_path: Path) -> None:
    path = _write(tmp_path, f"""
hithink_api_key = "key-from-file"
feishu_app_id = "app"
feishu_app_secret = "secret"
feishu_chat_id = "chat"
notify_windows = false
trade_capital = 50000
stop_loss = 0.08
run_at = "20:30"
data_dir = "{p(tmp_path / 'mydata')}"
""")
    cfg = load_config(path, use_env=False)
    assert cfg.hithink_api_key == "key-from-file"
    assert cfg.feishu_ready is True
    assert cfg.notify_windows is False
    assert cfg.trade_capital == 50000.0
    assert cfg.stop_loss == 0.08
    assert cfg.run_at == "20:30"
    assert cfg.data_dir == tmp_path / "mydata"
    assert cfg.db_path == tmp_path / "mydata" / "trader.db"
    assert cfg.dump_dir == tmp_path / "mydata" / "dumps"
    assert cfg.source_path == path


def test_supports_nested_section(tmp_path: Path) -> None:
    path = _write(tmp_path, """
title = "老A法师"

[laoa_trader]
hithink_api_key = "nested-key"
run_at = "18:00"
""")
    cfg = load_config(path, use_env=False)
    assert cfg.hithink_api_key == "nested-key"
    assert cfg.run_at == "18:00"


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级：环境变量 > config.toml（敏感项不必落盘）。"""
    path = _write(tmp_path, 'hithink_api_key = "file-key"\ntrade_capital = 100000')
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "env-key")
    monkeypatch.setenv("TRADE_CAPITAL", "88888")
    monkeypatch.setenv("NOTIFY_TRAY", "false")
    monkeypatch.setenv("LAOA_RUN_AT", "21:00")
    cfg = load_config(path)
    assert cfg.hithink_api_key == "env-key"
    assert cfg.trade_capital == 88888.0
    assert cfg.notify_tray is False
    assert cfg.run_at == "21:00"


def test_broken_toml_falls_back_to_defaults(tmp_path: Path) -> None:
    """配置写坏了不能崩 —— 用默认值起来，让用户在界面里看到问题。"""
    path = _write(tmp_path, "this is not = = toml [[[")
    cfg = load_config(path, use_env=False)
    assert cfg.run_at == "16:00"
    assert cfg.source_path == path


def test_wrong_types_fall_back(tmp_path: Path) -> None:
    path = _write(tmp_path, """
trade_capital = "不是数字"
stop_loss = "abc"
notify_tray = "yes"
intraday_interval = "30"
""")
    cfg = load_config(path, use_env=False)
    assert cfg.trade_capital == 100000.0     # 非法值退回默认
    assert cfg.stop_loss == 0.05
    assert cfg.notify_tray is True           # "yes" 认作真
    assert cfg.intraday_interval == 30       # 数字字符串认作数字


def test_valid_toml_has_no_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, 'run_at = "20:00"\n')
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""
    assert cfg.config_warning() == ""
    assert cfg.run_at == "20:00"


def test_broken_toml_is_reported_not_silent(tmp_path: Path) -> None:
    """TOML 语法错必须**显式告警**（否则表现为"设置怎么都不生效"，用户查不到原因）。

    真实踩坑：TOML 的裸键/表名必须是 ASCII，写 `[中文表]` 会解析失败；
    老实现静默退回默认值，`--doctor` 还显示"配置来源 = 那个文件"，极具误导性。
    """
    path = _write(tmp_path, 'data_dir = "/tmp/should-not-apply"\n[中文表]\n')
    cfg = load_config(path, use_env=False)
    assert cfg.config_error
    assert "解析失败" in cfg.config_error
    assert "中文表" in cfg.config_error or "line" in cfg.config_error
    assert cfg.source_path == path
    assert str(cfg.data_dir) != "/tmp/should-not-apply"      # 确实退回了默认值
    assert cfg.config_warning() == cfg.config_error


def test_quoted_chinese_keys_are_valid_toml(tmp_path: Path) -> None:
    """用户如果想用中文键，加上引号就是合法 TOML（此时不该有告警）。"""
    path = _write(tmp_path, 'run_at = "09:30"\n\n["我自己的表"]\n"未知键" = "别动我"\n')
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""
    assert cfg.run_at == "09:30"


def test_explicit_path_that_does_not_exist(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.toml", use_env=False)
    assert cfg.run_at == "16:00"
    assert cfg.source_path is None


def test_default_data_dir_uses_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 默认数据目录 `%LOCALAPPDATA%\\LaoATrader\\data`。"""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert config_mod.default_data_dir() == Path(
        r"C:\Users\me\AppData\Local") / "LaoATrader" / "data"


def test_default_data_dir_without_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert config_mod.default_data_dir() == Path("/tmp/xdg/LaoATrader/data")


def test_search_paths_include_env_pointer(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "custom.toml"
    target.write_text('run_at = "07:00"', encoding="utf-8")
    monkeypatch.setenv("LAOA_TRADER_CONFIG", str(target))
    cfg = load_config(use_env=False)
    assert cfg.source_path == target
    assert cfg.run_at == "07:00"


def test_data_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "envdata"))
    cfg = load_config(use_env=True)
    assert cfg.data_dir == tmp_path / "envdata"


def test_ensure_dirs_creates_data_and_dump(cfg) -> None:
    cfg.ensure_dirs()
    assert Path(cfg.data_dir).is_dir()
    assert cfg.dump_dir.is_dir()


def test_feishu_ready_requires_both_credentials(cfg) -> None:
    cfg.feishu_app_id = "app"
    cfg.feishu_app_secret = ""
    assert cfg.feishu_ready is False
    cfg.feishu_app_secret = "secret"
    assert cfg.feishu_ready is True


def test_get_config_singleton_and_set(cfg, monkeypatch) -> None:
    monkeypatch.setattr(config_mod, "_config", None)
    first = config_mod.get_config()
    assert config_mod.get_config() is first
    config_mod.set_config(cfg)
    assert config_mod.get_config() is cfg


def test_config_example_file_is_valid() -> None:
    """`config.example.toml` 必须能被解析（用户复制的第一站）。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    assert example.is_file()
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    for key in (
        "hithink_api_key", "feishu_app_id", "feishu_app_secret", "feishu_chat_id",
        "notify_feishu", "notify_windows", "notify_tray", "trade_capital",
        "trade_position_pct", "trade_max_positions", "intraday_interval",
        "stop_loss", "take_profit", "run_at",
    ):
        assert key in data, f"config.example.toml 缺少 {key}"
    assert data["run_at"] == "16:00"          # 主跑：收盘后
    assert data["run_at_fallback"] == "19:15"  # 补跑
    assert data["auto_run"] is True
    # 示例文件里不能带真实凭证
    assert data["hithink_api_key"] == ""
    assert data["feishu_app_secret"] == ""


def test_watchlist_defaults() -> None:
    cfg = load_config(use_env=False)
    assert cfg.watchlist_max == 20
    assert cfg.watchlist_in_pool is True


def test_watchlist_from_toml(tmp_path: Path) -> None:
    path = _write(tmp_path, "watchlist_max = 5\nwatchlist_in_pool = false\n")
    cfg = load_config(path, use_env=False)
    assert cfg.watchlist_max == 5
    assert cfg.watchlist_in_pool is False


def test_watchlist_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHLIST_MAX", "7")
    monkeypatch.setenv("WATCHLIST_IN_POOL", "0")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.watchlist_max == 7
    assert cfg.watchlist_in_pool is False


def test_example_config_documents_watchlist() -> None:
    """config.example.toml 里要能看懂怎么用自选股（用户第一站）。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    assert data["watchlist_max"] == 20
    assert data["watchlist_in_pool"] is True
    text = example.read_text(encoding="utf-8")
    assert "--watchlist add" in text          # 教怎么加
    assert "自选股" in text


def test_autorun_defaults_and_toml(tmp_path: Path) -> None:
    """自动运行三件套：开关 + 主跑 + 补跑。"""
    cfg = load_config(tmp_path / "none.toml", use_env=False)
    assert (cfg.auto_run, cfg.run_at, cfg.run_at_fallback) == (True, "16:00", "19:15")

    path = _write(tmp_path, 'auto_run = false\nrun_at = "16:30"\nrun_at_fallback = "20:00"\n')
    cfg2 = load_config(path, use_env=False)
    assert cfg2.auto_run is False
    assert cfg2.run_at == "16:30"
    assert cfg2.run_at_fallback == "20:00"


def test_autorun_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量覆盖：AUTO_RUN / RUN_AT / RUN_AT_FALLBACK。"""
    monkeypatch.setenv("AUTO_RUN", "0")
    monkeypatch.setenv("RUN_AT", "17:45")
    monkeypatch.setenv("RUN_AT_FALLBACK", "21:00")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.auto_run is False
    assert cfg.run_at == "17:45"
    assert cfg.run_at_fallback == "21:00"
