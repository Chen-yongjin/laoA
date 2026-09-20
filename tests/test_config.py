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
    assert cfg.auto_run is False              # 用户拍板：不设定时运行（选股随时手动）
    assert cfg.intraday_interval == 60
    assert cfg.stop_loss == 0.05
    assert cfg.take_profit == 0.10
    assert (cfg.trade_capital, cfg.trade_position_pct, cfg.trade_max_positions) == (
        100000.0, 0.2, 5)
    assert cfg.notify_feishu is True and cfg.notify_tray is True
    # 2026-09-18：Windows 系统通知整路删除（用户："太骚扰了，影响体验"），
    # 所以 `notify_windows` 这个属性**不存在了** —— 老配置里还写着它也不会报错（见下一条用例）
    assert not hasattr(cfg, "notify_windows")
    assert cfg.hithink_api_key == ""


def test_reads_toml(tmp_path: Path) -> None:
    path = _write(tmp_path, f"""
hithink_api_key = "key-from-file"
feishu_app_id = "app"
feishu_app_secret = "secret"
feishu_chat_id = "chat"
notify_windows = false
notify_windows_sound = true
notify_windows_open_url = false
trade_capital = 50000
stop_loss = 0.08
run_at = "20:30"
data_dir = "{p(tmp_path / 'mydata')}"
""")
    cfg = load_config(path, use_env=False)
    assert cfg.hithink_api_key == "key-from-file"
    assert cfg.feishu_ready is True
    # 老配置里那三个已删除的键**一律忽略**、不报错（老配置文件要能继续用）
    assert not hasattr(cfg, "notify_windows")
    assert cfg.trade_capital == 50000.0
    assert cfg.stop_loss == 0.08
    assert cfg.run_at == "20:30"
    assert cfg.data_dir == tmp_path / "mydata"
    assert cfg.db_path == tmp_path / "mydata" / "trader.db"
    assert cfg.dump_dir == tmp_path / "mydata" / "dumps"
    assert cfg.source_path == path


def test_supports_nested_section(tmp_path: Path) -> None:
    path = _write(tmp_path, """
title = "老牛选股助手"

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
        "notify_feishu", "notify_tray", "trade_capital",
        "trade_position_pct", "trade_max_positions", "intraday_interval",
        "stop_loss", "take_profit", "run_at",
    ):
        assert key in data, f"config.example.toml 缺少 {key}"
    assert data["run_at"] == "16:00"          # 主跑：收盘后
    assert data["run_at_fallback"] == "19:15"  # 补跑
    assert data["auto_run"] is False
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
    """自动运行三件套：开关 + 主跑 + 补跑（开关默认 **false**，两个时间键照旧）。"""
    cfg = load_config(tmp_path / "none.toml", use_env=False)
    assert (cfg.auto_run, cfg.run_at, cfg.run_at_fallback) == (False, "16:00", "19:15")

    path = _write(tmp_path, 'auto_run = true\nrun_at = "16:30"\nrun_at_fallback = "20:00"\n')
    cfg2 = load_config(path, use_env=False)
    assert cfg2.auto_run is True
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


# ── 提醒浮窗（QQ 式）相关的四个配置 + 闪烁秒数 ──


def test_notify_popup_defaults() -> None:
    """默认：**不弹浮窗**（用户 2026-09-18 改的口径）、浮窗参数仍是 8 秒/5 条、响声、闪 6 秒。

    为什么默认关：用户要的通知是 QQ 那种 —— 图标闪一下，他自己点托盘图标看「消息」列表
    （`ui/message_center.py`）；消息自己蹦出来反而不是他要的。
    浮窗那三个参数（秒数/条数）留着，因为勾上 `notify_popup` 就照旧生效。
    """
    cfg = load_config(use_env=False)
    assert cfg.notify_popup is False
    assert cfg.notify_popup_seconds == 8
    assert cfg.notify_popup_max_items == 5
    assert cfg.notify_sound is True
    assert cfg.notify_flash_seconds == 6


def test_notify_popup_invalid_values_fall_back(tmp_path: Path) -> None:
    """写错就回默认：秒数 0/负数/乱码 = 8 秒，条数越界夹到 1~10，闪烁负数 = 6 秒。

    注意 `notify_flash_seconds = 0` 是**合法**的（= 不闪），不该被改写成 6。
    """
    path = _write(tmp_path, """
notify_popup_seconds = 0
notify_popup_max_items = 99
notify_flash_seconds = -3
""")
    cfg = load_config(path, use_env=False)
    assert cfg.notify_popup_seconds == 8
    assert cfg.notify_popup_max_items == 10
    assert cfg.notify_flash_seconds == 6

    path2 = _write(tmp_path, """
notify_popup_seconds = "abc"
notify_popup_max_items = "abc"
notify_flash_seconds = 0
""")
    cfg2 = load_config(path2, use_env=False)
    assert cfg2.notify_popup_seconds == 8
    assert cfg2.notify_popup_max_items == 5
    assert cfg2.notify_flash_seconds == 0


def test_notify_popup_switches_written_wrong_stay_default(tmp_path: Path) -> None:
    """两个开关写错（`"maybe"`）→ **回到出厂值**（浮窗关、提示音开），不是把功能悄悄反过来。

    `notify_sound` 的默认仍是 True；`notify_popup` 的出厂值是 False（2026-09-18 起）。
    """
    path = _write(tmp_path, 'notify_popup = "maybe"\nnotify_sound = "maybe"\n')
    cfg = load_config(path, use_env=False)
    assert cfg.notify_popup is False
    assert cfg.notify_sound is True


def test_notify_popup_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量覆盖：开关认真假值，数字走同一套规范化（0 秒照样回默认）。"""
    monkeypatch.setenv("NOTIFY_POPUP", "0")
    monkeypatch.setenv("NOTIFY_SOUND", "off")
    monkeypatch.setenv("NOTIFY_POPUP_SECONDS", "12")
    monkeypatch.setenv("NOTIFY_POPUP_MAX_ITEMS", "9")
    monkeypatch.setenv("NOTIFY_FLASH_SECONDS", "3")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.notify_popup is False
    assert cfg.notify_sound is False
    assert cfg.notify_popup_seconds == 12
    assert cfg.notify_popup_max_items == 9
    assert cfg.notify_flash_seconds == 3

    # 环境变量写错也一样：开关回默认、秒数回默认
    monkeypatch.setenv("NOTIFY_POPUP", "maybe")
    monkeypatch.setenv("NOTIFY_POPUP_SECONDS", "0")
    cfg2 = load_config(tmp_path / "none.toml")
    assert cfg2.notify_popup is False           # 写错 → 回出厂值（2026-09-18 起是关）
    assert cfg2.notify_popup_seconds == 8


def test_example_config_documents_notify_popup() -> None:
    """config.example.toml 里要有这五个键，并写清"为什么不用系统通知"。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["notify_popup"] is False    # 默认关（2026-09-18：要的是 QQ 式"闪 + 消息列表"）
    assert data["notify_popup_seconds"] == 8
    assert data["notify_popup_max_items"] == 5
    assert data["notify_sound"] is True
    assert data["notify_flash_seconds"] == 6
    assert "点击行为不受" in text          # 为什么不走 Windows 系统通知
    assert "消息列表" in text               # 默认那条路是什么（点托盘图标看列表）


# ── 竞价扫描（全市场扫描 + 过滤规则）的那一组 ──


def test_auction_scan_defaults() -> None:
    """默认：关；9:20/9:25 各扫一次；涨幅 2%~9%；四板块全选；500 万；2 分；推 10 只。"""
    cfg = load_config(use_env=False)
    assert cfg.intraday_auction is False            # 口径确认之前**保持关闭**
    assert cfg.auction_scan_at == ["09:20", "09:25"]
    assert cfg.auction_min_pct == 2.0
    assert cfg.auction_max_pct == 9.0
    assert cfg.auction_boards == ["main", "chinext", "star", "bj"]
    assert cfg.auction_min_amount == 5e6
    assert cfg.auction_min_score == 2
    assert cfg.auction_min_volume_ratio == 2.0
    assert cfg.auction_alert_max_items == 10


def test_auction_scan_invalid_values_fall_back(tmp_path: Path) -> None:
    """写错就收拾干净：涨幅倒挂、板块写错、时刻写错、分数/条数越界。"""
    path = _write(tmp_path, """
auction_min_pct = 6.0
auction_max_pct = 5.0
auction_boards = ["chinext", "zzz"]
auction_scan_at = ["9:5", "25:00", "09:05"]
auction_min_score = 99
auction_alert_max_items = 999
""")
    cfg = load_config(path, use_env=False)
    assert cfg.auction_max_pct == 9.0               # 上限 ≤ 下限 → 回默认
    assert cfg.auction_min_pct == 6.0               # 下限本身合法，保留
    assert cfg.auction_boards == ["chinext"]        # 认不出的板块丢掉
    assert cfg.auction_scan_at == ["09:05"]         # 补零 + 去重 + 丢掉 25:00
    assert cfg.auction_min_score == 6               # 夹到满分（6）
    assert cfg.auction_alert_max_items == 50        # 夹到上限 50


def test_auction_scan_zero_or_negative_falls_back(tmp_path: Path) -> None:
    """阈值 0/负数会让"过滤"失真（什么都过）→ 回默认；板块空列表 = 不限制（全选）。"""
    path = _write(tmp_path, """
auction_min_pct = 0
auction_min_amount = -1
auction_max_pct = 0
auction_boards = []
auction_scan_at = "abc"
auction_min_score = 0
""")
    cfg = load_config(path, use_env=False)
    assert cfg.auction_min_pct == 2.0
    assert cfg.auction_min_amount == 5e6
    assert cfg.auction_max_pct == 9.0
    assert cfg.auction_boards == ["main", "chinext", "star", "bj"]
    assert cfg.auction_scan_at == ["09:20", "09:25"]
    assert cfg.auction_min_score == 1               # 0 → 夹到下限 1


def test_auction_scan_legacy_key_names_still_work(tmp_path: Path) -> None:
    """上一版的键名（`auction_alert_min_*`）继续认，但**新名优先**。"""
    path = _write(tmp_path, "auction_alert_min_pct = 3.5\nauction_alert_max_items = 7\n")
    cfg = load_config(path, use_env=False)
    assert cfg.auction_min_pct == 3.5
    assert cfg.auction_alert_max_items == 7

    both = _write(tmp_path, "auction_alert_min_pct = 3.5\nauction_min_pct = 4.5\n")
    assert load_config(both, use_env=False).auction_min_pct == 4.5


def test_auction_scan_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量：新名字都要能覆盖（含板块与扫描时刻这两个列表）。"""
    monkeypatch.setenv("AUCTION_MIN_PCT", "3.0")
    monkeypatch.setenv("AUCTION_MAX_PCT", "7.5")
    monkeypatch.setenv("AUCTION_MIN_AMOUNT", "3000000")
    monkeypatch.setenv("AUCTION_MIN_SCORE", "4")
    monkeypatch.setenv("AUCTION_ALERT_MAX_ITEMS", "20")
    monkeypatch.setenv("AUCTION_BOARDS", "chinext,star")
    monkeypatch.setenv("AUCTION_SCAN_AT", "09:19,09:26")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.auction_min_pct == 3.0 and cfg.auction_max_pct == 7.5
    assert cfg.auction_min_amount == 3e6
    assert cfg.auction_min_score == 4
    assert cfg.auction_alert_max_items == 20
    assert cfg.auction_boards == ["chinext", "star"]
    assert cfg.auction_scan_at == ["09:19", "09:26"]

    # 环境变量写错也一样回默认（0 秒那种写法不该让过滤失效）
    monkeypatch.setenv("AUCTION_MIN_PCT", "0")
    monkeypatch.setenv("AUCTION_SCAN_AT", "乱写")
    monkeypatch.setenv("AUCTION_BOARDS", "zzz")
    cfg2 = load_config(tmp_path / "none.toml")
    assert cfg2.auction_min_pct == 2.0
    assert cfg2.auction_scan_at == ["09:20", "09:25"]
    assert cfg2.auction_boards == ["main", "chinext", "star", "bj"]


def test_example_config_documents_auction_scan() -> None:
    """config.example.toml 里要能看到这一组，并写清"为什么只扫两次"和涨跌幅上下限的理由。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["intraday_auction"] is False
    assert data["auction_scan_at"] == ["09:20", "09:25"]
    assert data["auction_min_pct"] == 2.0
    assert data["auction_max_pct"] == 9.0
    assert data["auction_boards"] == ["main", "chinext", "star", "bj"]
    assert data["auction_min_amount"] == 5000000.0
    assert data["auction_min_score"] == 2
    assert data["auction_alert_max_items"] == 10
    assert "全市场" in text                          # 扫的是全市场，不是只看自己的票
    assert "配额" in text                            # 为什么不每分钟扫
    assert "买不进" in text                          # 涨幅上限的理由


# ── 持仓做T 的近似提示（开关 + 四个阈值）──


def test_position_t_switch_defaults_off_and_reads_toml(tmp_path: Path) -> None:
    """做T提示**默认关**（用户拍板：T策略默认关，阈值又没验证过）；开关与阈值都能从 config.toml 改。"""
    assert load_config(use_env=False).intraday_t is False
    assert load_config(use_env=False).t_high_min_gain_pct == 2.0

    path = _write(tmp_path, """
intraday_t = true
t_high_min_gain_pct = 3.0
t_high_pullback_pct = 2.0
t_low_min_drop_pct = 2.5
t_low_rebound_pct = 1.2
""")
    cfg = load_config(path, use_env=False)
    assert cfg.intraday_t is True
    assert cfg.t_high_min_gain_pct == 3.0
    assert cfg.t_high_pullback_pct == 2.0
    assert cfg.t_low_min_drop_pct == 2.5
    assert cfg.t_low_rebound_pct == 1.2


def test_position_t_switch_written_wrong_stays_default_off(tmp_path: Path) -> None:
    """开关写错（`"maybe"`）→ **回到默认（关）**。

    改成默认关之后，这一条和"写错按 False"结果一样了（这条断言是回归保护：
    就算有人把默认改回 true，也不该出现"写错 = 功能被打开"这种更坏的结果）。
    """
    path = _write(tmp_path, 'intraday_t = "maybe"\n')
    assert load_config(path, use_env=False).intraday_t is False
    # 但那四个阈值写坏（0/负数/乱码）→ 回各自的默认值（0 会让"涨过 0%"变成永远触发）
    path2 = _write(tmp_path, """
t_high_min_gain_pct = 0
t_high_pullback_pct = -1
t_low_min_drop_pct = "abc"
t_low_rebound_pct = 0.0
""")
    cfg = load_config(path2, use_env=False)
    assert (cfg.t_high_min_gain_pct, cfg.t_high_pullback_pct,
            cfg.t_low_min_drop_pct, cfg.t_low_rebound_pct) == (2.0, 1.5, 2.0, 1.0)


def test_position_t_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量：`INTRADAY_T` 打开、`T_*` 改阈值（写错也一样回默认）。"""
    monkeypatch.setenv("INTRADAY_T", "1")
    monkeypatch.setenv("T_HIGH_MIN_GAIN_PCT", "3.5")
    monkeypatch.setenv("T_LOW_REBOUND_PCT", "0.8")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.intraday_t is True
    assert cfg.t_high_min_gain_pct == 3.5
    assert cfg.t_low_rebound_pct == 0.8

    monkeypatch.setenv("INTRADAY_T", "maybe")          # 写错 → 回默认（关）
    monkeypatch.setenv("T_HIGH_MIN_GAIN_PCT", "0")
    cfg2 = load_config(tmp_path / "none.toml")
    assert cfg2.intraday_t is False
    assert cfg2.t_high_min_gain_pct == 2.0


def test_example_config_documents_position_t() -> None:
    """`config.example.toml` 要给全五个键，并写清"近似、不可回测、阈值是手工设定"。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["intraday_t"] is False                 # 用户拍板：T策略默认关
    assert data["t_high_min_gain_pct"] == 2.0
    assert data["t_high_pullback_pct"] == 1.5
    assert data["t_low_min_drop_pct"] == 2.0
    assert data["t_low_rebound_pct"] == 1.0
    assert "T+1" in text                               # 反T/正T 的前提要写出来
    assert "回测" in text and "近似" in text            # 说白了：近似、回测不了
    assert "券商" in text                               # 能卖多少只有券商知道


# ── 用户拍板后的默认策略集与 6 个月导入窗口 ──


def test_default_strategy_set_is_no_formula() -> None:
    """选股相关只剩一个键：`enabled_formulas`，**默认空 = 只盯自选股**。

    2026-09-18 之前这里断言的是"默认只开 short 组"（`enabled_groups`）；
    内置策略改成随包公式、策略组机制删掉之后，那两个键连字段都不存在了 ——
    所以"默认跑什么"这个问题现在只有一个答案：**什么都不跑，等用户勾公式**。
    """
    cfg = load_config(use_env=False)
    assert cfg.enabled_formulas == []
    assert not hasattr(cfg, "enabled_groups")
    assert not hasattr(cfg, "enabled_strategies")


def test_default_history_window_is_six_months() -> None:
    """默认导入 0.5 年（6 个月）、跨度门槛 0.4 年（必须 < 0.5，否则库永远判"不足"）。

    为什么是 0.5（用户拍板）：超短线（short 组持有期 T+3）不需要长历史 ——
    6 个月约 120 个交易日、约 50 万行，库更小、首次导入更快。
    """
    cfg = load_config(use_env=False)
    assert cfg.history_years == 0.5
    assert cfg.min_history_years == 0.4
    assert cfg.min_history_years < cfg.history_years
    assert cfg.history_warning() == ""              # 默认配置不矛盾


def test_default_switches_are_off() -> None:
    """出厂设置里"会自己动"的东西一律关掉（用户拍板）：
    不设定时运行（选股随时手动）、T策略默认关、当日异动默认关（减少无用消息）。
    """
    cfg = load_config(use_env=False)
    assert cfg.auto_run is False
    assert cfg.intraday_t is False
    assert cfg.intraday_anomaly is False


def test_default_notify_is_flash_plus_message_list() -> None:
    """默认只走「图标闪烁 + 消息列表」：`notify_channels` 空、`notify_popup` 关。

    为什么：系统弹窗/托盘气泡/飞书都由用户自己勾（出厂状态不该有"还会弹系统窗口"），
    而自绘浮窗也改成默认关了（用户 2026-09-18：他要的是 QQ 那种"图标闪、点开看列表"）。
    提醒本身仍然看得见：**图标闪 + 消息窗口 + 响声**，所以这三项仍是默认开。
    """
    cfg = load_config(use_env=False)
    assert cfg.notify_channels == []
    assert cfg.channels == []                       # 空列表 = 这三路一个都不发
    assert cfg.notify_popup is False
    assert cfg.notify_sound is True
    assert cfg.notify_flash_seconds == 6


def test_data_sources_default_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`data_sources`：默认 `["hithink", "public"]`（**同花顺是主源**），环境变量可覆盖。

    默认值随产品走，而且这个决定被用户来回拍过两次，所以这里把"为什么"写清楚：
    2026-09-17 一度把免 Key 的公开源放在第一位（"别人拿到程序不用先申请 Key"）；
    2026-09-18 用户改回来（原话："那还是不要换源吧 真晕 同花顺不会轻易限流"）——
    公开接口**实测会限流**：腾讯 fqkline 抓 700 只左右就开始连续失败、新浪列表接口
    直接回 HTTP 456，而同花顺是正经 API。顺序即优先级，所以这一行就是产品行为。
    环境变量这条路（`DATA_SOURCES`，逗号分隔、与 `notify_channels` 同一套 `_as_list`
    写法）**一字未改**，所以下面照旧钉着"环境变量真的接上了"。
    """
    cfg = load_config(tmp_path / "none.toml", use_env=False)
    # 2026-09-18 用户拍板：同花顺回到主源（正经 API、不会像公开接口那样限流），
    # 免 Key 的公开源降为兜底 —— 顺序即优先级，所以这一行就是产品行为
    assert cfg.data_sources == ["hithink", "public"]

    monkeypatch.setenv("DATA_SOURCES", "hithink,csv")
    cfg2 = load_config(tmp_path / "none.toml")
    assert cfg2.data_sources == ["hithink", "csv"]

    monkeypatch.setenv("DATA_SOURCES", "hithink，csv")   # 中文逗号也认
    assert load_config(tmp_path / "none.toml").data_sources == ["hithink", "csv"]

    # 顺序就是优先级：把公开源排到前面，它就是第一顺位（换来源不必改代码）
    monkeypatch.setenv("DATA_SOURCES", "public,hithink")
    assert load_config(tmp_path / "none.toml").data_sources == ["public", "hithink"]


def test_data_sources_in_example_config(tmp_path: Path) -> None:
    """`config.example.toml` 要带上这个键，且**与代码默认值一致**（两处漂移最容易出事）。

    比的是 `load_config(不存在的路径)` 而不是 `Config()`：走一遍真实的读取+归一，
    同时**不碰宿主机上可能存在的 config.toml**（`config_search_paths()` 会找
    仓库根目录与 `~/.config`，开发机上真有一份的话，断言就变成了"看环境"）。
    """
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["data_sources"] == ["hithink", "public"]   # 与代码默认值必须一致（主源=同花顺）
    assert data["data_sources"] == load_config(tmp_path / "none.toml",
                                              use_env=False).data_sources
    assert "同花顺" in text
    # 示例文件是**用户唯一能看到"这两个源是什么关系"的地方**，所以两句都要有：
    # 主源（同花顺）要 Key、以及兜底源（公开源）免 Key —— 2026-09-18 换了主次之后
    # 这两句的主语也跟着换了（原来"免 Key"是描述主源的）
    assert "Key" in text
    assert "免 Key" in text                  # 兜底源免 Key 这件事，示例里必须说清
    assert "兜底" in text                    # 定位也要写明，否则用户以为公开源还是主源


def test_default_history_window_is_pinned_in_example_config() -> None:
    """示例配置（会分发给用户）里的默认值必须与代码一致 —— 两处漂移是最容易出的事。"""
    import tomllib
    from pathlib import Path as P

    example = P(__file__).resolve().parents[1] / "config.example.toml"
    data = tomllib.loads(example.read_text(encoding="utf-8"))
    cfg = load_config(use_env=False)
    assert data["history_years"] == cfg.history_years == 0.5
    assert data["min_history_years"] == cfg.min_history_years == 0.4
    assert data["auto_run"] is cfg.auto_run is False
    assert data["intraday_t"] is cfg.intraday_t is False
    assert data["intraday_anomaly"] is cfg.intraday_anomaly is False
    assert data["notify_channels"] == cfg.notify_channels == []


def test_push_only_proven_key_is_gone() -> None:
    """`push_only_proven`（"只推有边际的策略标的"）已随策略引擎删掉 —— 字段不存在。

    老配置里写着它也不会报错：`load_config()` 按未知键忽略（见
    `test_config_writeback.py` 里那份样例配置，那两行退役键现在还在文件里）。
    """
    cfg = load_config(use_env=False)
    assert not hasattr(cfg, "push_only_proven")


def test_default_pet_and_voice_are_on() -> None:
    """桌宠与中文朗读**默认开**（用户 2026-09-18 点名要的），并且示例文件里教得会。

    为什么默认开：用户原话"像一个桌宠一样的，软件隐藏时停留在桌面，有消息时大声喊出
    消息内容" —— 这是他主动要的功能，出厂就该看得见。两个开关随时能关
    （关掉任何一个都不影响其余提醒：气泡/消息列表/图标闪烁各自独立）。
    """
    cfg = load_config(use_env=False)
    assert cfg.notify_pet is True
    assert cfg.notify_voice is True
    assert cfg.notify_voice_volume == 0.9        # 「大声喊」
    assert cfg.notify_voice_rate == 0            # 正常语速
    assert (cfg.pet_x, cfg.pet_y) == (0, 0)      # 0 = 还没拖过 → 默认右下角


def test_pet_and_voice_keys_are_validated(tmp_path: Path) -> None:
    """音量/语速/坐标写坏了都不许把程序带崩：越界夹取、乱码回默认。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "notify_voice_volume = 1.7\n"
        "notify_voice_rate = 99\n"
        "pet_x = -50\n"
        "pet_y = 12\n",
        encoding="utf-8",
    )
    cfg = load_config(path=path, use_env=False)
    assert cfg.notify_voice_volume == 1.0        # 夹到上限（"想更响一点"就按上限办）
    assert cfg.notify_voice_rate == 10
    assert cfg.pet_x == 0                        # 负数 = 屏幕外，当没记过
    assert cfg.pet_y == 12

    path.write_text('notify_voice_volume = "响"\nnotify_voice_rate = "快"\n',
                    encoding="utf-8")
    cfg = load_config(path=path, use_env=False)
    assert cfg.notify_voice_volume == 0.9 and cfg.notify_voice_rate == 0


def test_pet_and_voice_env_switches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量也能开关（与其它通知开关同一套写法）。"""
    monkeypatch.setenv("NOTIFY_PET", "false")
    monkeypatch.setenv("NOTIFY_VOICE", "0")
    cfg = load_config(use_env=True)
    assert cfg.notify_pet is False and cfg.notify_voice is False
