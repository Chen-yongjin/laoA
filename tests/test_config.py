"""配置读取：config.toml 优先、环境变量覆盖、默认值兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from laoa_trader import config as config_mod
from laoa_trader.config import Config, load_config

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


def test_pool_view_default_and_invalid_falls_back_to_cards(tmp_path: Path) -> None:
    """股票池视图偏好：默认卡片；写错（手改 TOML）一律当卡片，不让界面白屏。"""
    cfg = load_config(tmp_path / "none.toml", use_env=False)
    assert cfg.pool_view == "cards"

    path = _write(tmp_path, 'pool_view = "table"\n')
    assert load_config(path, use_env=False).pool_view == "table"

    # 大小写与空格也算合法写法（用户手改时很常见）
    path = _write(tmp_path, 'pool_view = " TABLE "\n')
    assert load_config(path, use_env=False).pool_view == "table"

    for bad in ("card", "卡片", "", "tableau"):
        path = _write(tmp_path, f'pool_view = "{bad}"\n')
        assert load_config(path, use_env=False).pool_view == "cards", bad



# ── 大盘概览（market_overview / market_indices / market_sentiment_indices /
#               market_breadth / market_overview_ttl）──


def test_market_overview_defaults() -> None:
    """默认三组：五个宽基 / 四个情绪 / 两个板块；宽度汇总关、TTL 55。"""
    cfg = load_config(use_env=False)
    assert cfg.market_overview is True
    assert cfg.market_indices == [
        "000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH",
    ]
    # 899050.BJ（北证50）**不存在**：混进 thscodes 会让整批快照失败，默认里不能有它
    assert "899050.BJ" not in cfg.market_indices
    assert cfg.market_sentiment_indices == [
        "883404.TI", "883958.TI", "883994.TI", "883418.TI",
    ]
    # 默认情绪组必须是这一组（顺序也是）
    assert "932000.TI" not in cfg.market_sentiment_indices
    assert cfg.market_sector_indices == ["881155.TI", "881157.TI"]   # 银行 / 证券
    # 全市场汇总（涨跌家数 + 北交所成交额）**默认开**：整页只显示沪深两市会显得空，
    # 代价是 6 个分页请求 —— 所以它有独立的 5 分钟缓存，不跟着每分钟的页面刷新跑
    assert cfg.market_breadth is True
    assert cfg.market_overview_ttl == 55


def test_market_overview_from_toml_with_comma_string(tmp_path: Path) -> None:
    """逗号分隔的字符串手写最省事（TOML 数组写起来啰嗦），两种写法都要认。"""
    path = _write(
        tmp_path,
        'market_indices = "000001.SH, 399006.SZ"\n'
        'market_sentiment_indices = "883958.TI=昨日连板，883409.TI"\n'
        'market_sector_indices = ["881155.TI", "881157.TI"]\n'
        "market_breadth = true\n"
        "market_overview_ttl = 90\n",
    )
    cfg = load_config(path, use_env=False)
    assert cfg.market_indices == ["000001.SH", "399006.SZ"]
    # 中文逗号也认；`代码=名称` 的自定义名原样保留（取数时解析）
    assert cfg.market_sentiment_indices == ["883958.TI=昨日连板", "883409.TI"]
    assert cfg.market_sector_indices == ["881155.TI", "881157.TI"]
    assert cfg.market_breadth is True
    assert cfg.market_overview_ttl == 90

    # 组配成空列表 = 这一组整行不显示（不是"回退默认"）
    path = _write(tmp_path, "market_sector_indices = []\n")
    assert load_config(path, use_env=False).market_sector_indices == []

    path = _write(tmp_path, 'market_indices = ["000300.SH"]\nmarket_overview = false\n')
    cfg2 = load_config(path, use_env=False)
    assert cfg2.market_indices == ["000300.SH"]
    assert cfg2.market_overview is False


def test_market_overview_invalid_values_fall_back(tmp_path: Path) -> None:
    """写错就回默认：**不要**因为配置写坏把概览关掉或变成每 5 秒打一轮接口。"""
    path = _write(
        tmp_path,
        'market_overview = "maybe"\n'          # 认不出来的真假值 → 回默认（开着）
        "market_breadth = 3\n"                 # 数字 → 按真值处理（3 = 开）
        'market_overview_ttl = "abc"\n'        # 乱码 → 回默认 55
        "market_indices = 123\n",              # 不是列表也不是字符串 → 回默认
    )
    cfg = load_config(path, use_env=False)
    assert cfg.market_overview is True
    assert cfg.market_breadth is True
    assert cfg.market_overview_ttl == 55
    assert cfg.market_indices == [
        "000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH",
    ]

    # TTL 写成 0/负数同样回默认：0 会让界面每 5 秒打一轮接口（配额与限流都吃不消）
    for bad in ("0", "-30"):
        path = _write(tmp_path, f"market_overview_ttl = {bad}\n")
        assert load_config(path, use_env=False).market_overview_ttl == 55, bad
    assert Config(market_overview_ttl=0).market_overview_ttl == 55


def test_market_overview_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量覆盖（临时换一组指数、或临时关掉概览，不必改文件）。"""
    monkeypatch.setenv("MARKET_OVERVIEW", "0")
    monkeypatch.setenv("MARKET_BREADTH", "yes")
    monkeypatch.setenv("MARKET_INDICES", "000300.SH,399006.SZ")
    monkeypatch.setenv("MARKET_SENTIMENT_INDICES", "883958.TI")
    monkeypatch.setenv("MARKET_SECTOR_INDICES", "881155.TI")
    monkeypatch.setenv("MARKET_OVERVIEW_TTL", "120")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.market_overview is False
    assert cfg.market_breadth is True
    assert cfg.market_indices == ["000300.SH", "399006.SZ"]
    assert cfg.market_sentiment_indices == ["883958.TI"]
    assert cfg.market_sector_indices == ["881155.TI"]
    assert cfg.market_overview_ttl == 120

    # 环境变量写错也一样回默认（不会把概览悄悄关掉）
    monkeypatch.setenv("MARKET_OVERVIEW", "maybe")
    monkeypatch.setenv("MARKET_OVERVIEW_TTL", "-1")
    cfg2 = load_config(tmp_path / "none.toml")
    assert cfg2.market_overview is True
    assert cfg2.market_overview_ttl == 55


def test_example_config_documents_market_overview() -> None:
    """config.example.toml 里要能看懂这张卡是什么、怎么换情绪指数。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    assert data["market_overview"] is True
    assert data["market_indices"] == [
        "000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH",
    ]
    assert data["market_sentiment_indices"] == [
        "883404.TI", "883958.TI", "883994.TI", "883418.TI",
    ]
    assert data["market_sector_indices"] == ["881155.TI", "881157.TI"]
    assert data["market_breadth"] is True        # 默认开，且要说明"每 5 分钟才更新"
    assert data["market_overview_ttl"] == 55

    text = example.read_text(encoding="utf-8")
    assert "给你自己换" in text                   # 情绪指数是留给用户换的
    assert "883958.TI" in text                   # 备选口径列出来
    assert "899050.BJ" in text and "不存在" in text   # 别写这个不存在的代码
    assert "6 页" in text                         # 全市场汇总要翻几页（为什么它慢一档）
    assert "每 5 分钟" in text                    # 页面上也会这么写：它明显比指数慢一档
    assert "每分钟刷一次" in text                  # 独立的 60 秒定时器（不是 5 秒那个）
    # 情绪组默认值要在示例里能看到（含微盘股），并且**不写任何"某代码取不到"的备注**：
    # 用户明确说过不需要这类备注，代码里直接给可用的代码。
    assert "883404.TI" in text and "883958.TI" in text
    assert "883994.TI" in text and "883418.TI" in text and "微盘股" in text
    assert "932000" not in text and "中证2000" not in text
