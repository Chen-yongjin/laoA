"""配置读取：`config.toml` 优先，环境变量覆盖。

为什么这样设计
--------------
服务器版用的是 pydantic-settings（环境变量 + `.env`）。桌面版**故意不引入 pydantic**：

1. 桌面版要能双击 exe 运行，配置来源必须是"看得见的文件"（`config.toml`），
   而不是藏在系统里的环境变量 —— 用户改了配置要能立刻看到改了什么；
2. 打包成 exe 后 `pip install pydantic-settings` 会多带一层依赖，
   而 Python 3.11 自带 `tomllib` 就能读 TOML，零依赖；
3. 但**运维习惯要继承**：同花顺 Key / 飞书凭证这些敏感项仍然支持环境变量覆盖
   （CI、临时调试、不想把 Key 写进磁盘文件的场景），命名与服务器版保持一致，
   两套程序可以共用同一份 `.env` 习惯。

优先级：**环境变量 > config.toml > 内置默认值**。

配置文件查找顺序（第一个存在的生效）：
    1. 显式传入的路径 / 环境变量 `LAOA_TRADER_CONFIG`
    2. 当前工作目录 `./config.toml`
    3. exe / 包所在目录的 `config.toml`（打包后就是 exe 旁边那个）
    4. 用户目录 `%APPDATA%\\LaoATrader\\config.toml`（Windows）或 `~/.config/laoa-trader/config.toml`
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: 默认数据目录（Windows 用 LOCALAPPDATA，其它平台退回 ~/.local/share）
DEFAULT_APP_NAME = "LaoATrader"

#: 支持的通知频道（顺序 = 界面与 --doctor 的展示顺序）
CHANNELS: tuple[str, ...] = ("windows", "feishu", "tray")

#: 股票池的两种视图（界面右上角【切换为卡片/表格】）；写错的值当 `cards`
POOL_VIEWS: tuple[str, ...] = ("cards", "table")


def default_data_dir() -> Path:
    """默认数据目录：Windows `%LOCALAPPDATA%\\LaoATrader\\data`，其它平台同构。"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / DEFAULT_APP_NAME / "data"
    # 非 Windows（开发和测试用）：保持同样的目录语义
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / DEFAULT_APP_NAME / "data"


def config_search_paths() -> list[Path]:
    """按优先级返回 config.toml 的候选路径。"""
    paths: list[Path] = []
    explicit = os.environ.get("LAOA_TRADER_CONFIG")
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path.cwd() / "config.toml")
    # 打包后 `sys.frozen` 为真，配置放在 exe 同级目录最直观
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).parent / "config.toml")
    else:
        paths.append(Path(__file__).resolve().parents[2] / "config.toml")
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / DEFAULT_APP_NAME / "config.toml")
    paths.append(Path.home() / ".config" / "laoa-trader" / "config.toml")
    return paths


def find_config_file() -> Path | None:
    """返回第一个存在的配置文件路径；都没有时返回 None（全部走默认值）。"""
    for path in config_search_paths():
        try:
            if path.is_file():
                return path
        except OSError:  # 权限/IO 问题不该让启动失败
            continue
    return None


def _load_toml(path: Path | None) -> tuple[dict[str, Any], str]:
    """读取 TOML；文件损坏时**不抛异常**（GUI 不能因为一个坏文件起不来）。

    Returns:
        (解析出的数据, 错误说明)。错误说明会一路带到 `Config.config_error`，
        由 `--doctor` / 界面状态栏显示 —— 否则"配置写错了"会表现成
        "设置怎么都不生效"，用户根本猜不到原因（实测踩过：TOML 要求裸键是 ASCII，
        写中文键会解析失败并静默退回默认值）。
    """
    if path is None:
        return {}, ""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        return {}, f"config.toml 解析失败（已退回默认值）：{exc}"
    except OSError as exc:
        return {}, f"config.toml 读取失败（已退回默认值）：{exc}"
    # 允许 [laoa_trader] / [laoa] / [trader] 这类分区表，也允许平铺在顶层
    for section in ("laoa_trader", "laoa", "trader"):
        nested = data.get(section)
        if isinstance(nested, dict):
            merged = {k: v for k, v in data.items() if not isinstance(v, dict)}
            merged.update(nested)
            return merged, ""
    return {k: v for k, v in data.items() if not isinstance(v, dict)}, ""


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    value = _env_str(name)
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes", "on", "y", "是")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "是")
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class Config:
    """运行配置。

    字段与 `config.example.toml` 一一对应；新增字段时记得同步那个文件。
    """

    # ── 数据源 ──
    hithink_api_key: str = ""
    data_dir: Path = field(default_factory=default_data_dir)

    # ── 策略组（跑哪几组 / 哪几条策略）──
    #: 启用的策略组：ultra（超短·隔日 T+2）/ short（短线·T+3）/ swing（波段·T+10）
    enabled_groups: list[str] = field(
        default_factory=lambda: ["ultra", "short", "swing"]
    )
    #: 只跑列出的策略（类名或中文名都认）；**留空 = 该组全选**。
    #: 与 enabled_groups 同时非空时取交集；两者都空 = 全选（安全默认）。
    enabled_strategies: list[str] = field(default_factory=list)

    # ── 运行时自检（本地数据够不够用，三态判定；见 data/preflight.py）──
    #: 缺数据时是否自动下载：**增量自动**（1 次请求）；全量始终需要明确同意
    #: （界面向导点"开始下载"、CLI 加 --auto-download）
    auto_download_on_start: bool = True
    #: 首次下载**导入**多少年历史（同花顺 dump 固定 10 年，导入时按这个值过滤）。
    #: 默认 5 年：全市场约 500 万行，分发出去的库更小、首次下载更快；
    #: 想要 10 年（例如自己做长样本回测）把它改成 10。
    history_years: float = 5.0
    #: 历史跨度下限（年）——低于它就认为"历史不够，需要重新下载"。
    #: **必须小于** history_years，否则 5 年的库会被永远判成"不足"（见 history_warning()）
    min_history_years: float = 4.5
    #: 最新交易日股票数下限——低于它说明只下了一部分
    min_symbols: int = 4000
    #: `ready` 允许的最大落后交易日数（0 = 必须是最新交易日）
    max_stale_trading_days: int = 0

    # ── 自选股（手动加的标的：和策略标的并列进池，一起盯）──
    #: 自选股数量上限（超出的会在日志/界面提示，不静默丢弃）
    watchlist_max: int = 20
    #: 自选股是否并入股票池并参与盘中监控（false = 只记录不监控）
    watchlist_in_pool: bool = True

    # ── 通知 ──
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""
    receive_id_type: str = "chat_id"
    #: 要用的通知频道（三选任意组合，**空列表 = 只入库不推送**）
    notify_channels: list[str] = field(
        default_factory=lambda: ["windows", "feishu", "tray"]
    )
    #: 飞书频道总开关（与 notify_channels 同时生效；缺凭证时自动跳过）
    feishu_on: bool = True
    #: Windows 弹窗是否带提示音
    notify_windows_sound: bool = True
    #: Windows 弹窗点击是否打开雪球页面
    notify_windows_open_url: bool = True
    #: 托盘气泡显示时长（毫秒）
    notify_tray_duration_ms: int = 8000
    # ── 以下三个是"单频道开关"（老配置沿用；与 notify_channels 同时生效）──
    notify_feishu: bool = True
    notify_windows: bool = True
    notify_tray: bool = True

    # ── 交易参数（只用于算条件单，不涉及任何下单） ──
    trade_capital: float = 100000.0
    trade_position_pct: float = 0.2
    trade_max_positions: int = 5
    buy_slippage: float = 0.005
    sell_slippage: float = 0.005

    # ── 盘中 ──
    intraday_interval: int = 60
    stop_loss: float = 0.05
    take_profit: float = 0.10

    # ── 大文件下载（dump 几百 MB：断点续传 + 过期自动重签）──
    #: 单次下载失败后的最大尝试次数（每次都会重新签 URL 并从断点继续）
    download_max_attempts: int = 5
    #: 建连超时（秒）——网络不好时短一点，尽快重试
    download_connect_timeout: float = 15.0
    #: 读取超时（秒）——两个数据块之间的最大间隔，给流式下载留足余量
    download_read_timeout: float = 90.0

    # ── 定时（界面「设置 → 自动运行」可改；改完**立即生效**，不用重启）──
    #: 是否每天自动运行（关掉则只在你手动点按钮时跑）
    auto_run: bool = True
    #: 每天主跑时间（HH:MM）。默认 16:00 —— A 股 15:00 收盘，收盘后数据才齐
    run_at: str = "16:00"
    #: 补跑时间（HH:MM）：主跑**没成功**时到这个点再试一次
    run_at_fallback: str = "19:15"

    # ── 界面偏好（只影响"怎么显示"，不影响任何计算）──
    #: 股票池默认视图：`cards` = 卡片（默认，信息完整）/ `table` = 9 列表格（更密）
    pool_view: str = "cards"

    #: 记录配置实际来自哪个文件（状态栏展示用）
    source_path: Path | None = None
    #: 配置文件解析/读取失败的原因（空 = 一切正常）
    config_error: str = ""

    def __post_init__(self) -> None:
        """把界面偏好收紧到合法取值。

        为什么要在这里做：`pool_view` 是给界面看的，手改 config.toml 写错一个字母
        （"card" / "Cards" / 中文）不该表现成"股票池页打不开/空白"——
        统一按 `cards` 处理，用户看到的仍然是一个能用的界面。
        """
        value = str(self.pool_view or "").strip().lower()
        self.pool_view = value if value in POOL_VIEWS else "cards"

    # -- 派生属性 --

    @property
    def db_path(self) -> Path:
        """本地 SQLite 路径（目录不存在时由 storage 层创建）。"""
        return Path(self.data_dir) / "trader.db"

    @property
    def dump_dir(self) -> Path:
        """同花顺 dump 落盘目录（下载中断后可以复用已下好的 Parquet）。"""
        return Path(self.data_dir) / "dumps"

    @property
    def feishu_ready(self) -> bool:
        """飞书凭证是否配齐（缺任一则静默跳过该路通知）。"""
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def channels(self) -> list[str]:
        """本次实际要用的通知频道（按配置顺序，已剔除被关掉/写错的）。

        判定顺序（三者同时生效，任一为否就不发）：
            1. 在 `notify_channels` 列表里（空列表 = 一个都不发）；
            2. 该频道的单频道开关（`notify_feishu/windows/tray`）为真；
            3. 飞书还要 `feishu_on` 为真（凭证是否配齐在发送时再判，缺则"跳过"）。
        """
        enabled: list[str] = []
        for channel in self.notify_channels or []:
            name = str(channel).strip().lower()
            if name not in CHANNELS:
                continue          # 写错的名字忽略（不静默变成"全发"）
            if name == "feishu" and not self.feishu_on:
                continue
            if not getattr(self, f"notify_{name}", True):
                continue
            if name not in enabled:
                enabled.append(name)
        return enabled

    def channel_states(self) -> dict[str, str]:
        """每个频道的状态说明（通知设置面板与 --doctor 展示用）。

        Returns:
            {频道: "启用" / "未在 notify_channels 中" / "单频道开关关闭" / "feishu_on=false" / "未配置凭证"}
        """
        states: dict[str, str] = {}
        chosen = {str(c).strip().lower() for c in (self.notify_channels or [])}
        for name in CHANNELS:
            if name not in chosen:
                states[name] = "未在 notify_channels 中"
            elif name == "feishu" and not self.feishu_on:
                states[name] = "feishu_on = false"
            elif not getattr(self, f"notify_{name}", True):
                states[name] = f"notify_{name} = false"
            elif name == "feishu" and not self.feishu_ready:
                states[name] = "已启用，但未配置飞书凭证（发送时跳过）"
            else:
                states[name] = "启用"
        return states

    def config_warning(self) -> str:
        """配置有问题时返回中文提示（空串 = 没问题）。界面与 --doctor 都用它。"""
        return self.config_error

    def history_warning(self) -> str:
        """`history_years` / `min_history_years` 写矛盾时的中文提示（空串 = 没问题）。

        为什么必须联动：自检的 ready 判据是"跨度 ≥ min_history_years"。
        如果 min_history_years（默认 4.5）不小于 history_years（默认 5），
        那么**按配置导入的库永远达不到 ready**，用户会被反复催着重新下载 ——
        这个组合必须当场说清楚，不能让它表现成"数据老是缺"。
        """
        years = float(getattr(self, "history_years", 5) or 0)
        floor = float(getattr(self, "min_history_years", 4.5) or 0)
        if years <= 0:
            return f"history_years 必须大于 0（当前 {years:g}）"
        if floor >= years:
            return (
                f"配置矛盾：min_history_years（{floor:g}）必须**小于** history_years（{years:g}），"
                f"否则按 {years:g} 年导入的库永远达不到自检要求、会被反复要求重新下载。"
                f"建议 min_history_years 设为 {max(years - 0.5, 0.5):g}"
            )
        return ""

    def ensure_dirs(self) -> None:
        """建好数据目录（幂等）。

        Raises:
            OSError: 目录建不出来（只读盘、权限不足、盘符不存在）。
                调用方负责转成**看得懂的中文提示** —— 这类错误的 traceback 对用户毫无价值。
        """
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.dump_dir.mkdir(parents=True, exist_ok=True)

    def ensure_dirs_message(self) -> str:
        """尝试建目录；失败时返回中文提示，成功返回空串。"""
        try:
            self.ensure_dirs()
            return ""
        except OSError as exc:
            return (
                f"无法创建数据目录 {self.data_dir}：{exc}\n"
                "请在 config.toml 里把 data_dir 改成一个有写权限的目录"
                "（例如 D:\\LaoATrader\\data）。"
            )


def _as_list(value: Any, default: list[str]) -> list[str]:
    """把配置值转成字符串列表。

    兼容三种写法（用户手改 TOML 时很常见）：
        notify_channels = ["windows", "tray"]     # 标准
        notify_channels = "windows,tray"          # 手滑写成字符串
        notify_channels = []                      # 空 = 不启用（**不是**退回默认）
    """
    if value is None:
        return list(default)
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return list(default)
    return [str(item).strip() for item in items if str(item).strip()]


def _coerce(section: dict[str, Any], name: str) -> Any:
    """把 TOML 里的值转成目标字段的类型（TOML 没有布尔字符串这类问题，但手改容易写错）。"""
    defaults = Config()
    default = getattr(defaults, name)
    value = section.get(name)
    if value is None:
        return default
    if isinstance(default, list):
        return _as_list(value, default)
    if isinstance(default, bool):
        return _as_bool(value, default)
    if isinstance(default, float):
        return _as_float(value, default)
    if isinstance(default, int):
        return _as_int(value, default)
    return _as_str(value)


def load_config(path: Path | str | None = None, *, use_env: bool = True) -> Config:
    """读取配置：config.toml 打底，环境变量覆盖。

    Args:
        path: 显式配置文件路径；None 时按 `config_search_paths()` 自动查找。
        use_env: 是否允许环境变量覆盖（测试中可关掉，避免被宿主环境污染）。

    Returns:
        Config 实例（任何字段缺失/格式错都退回默认值，绝不抛异常）。
    """
    source = Path(path) if path is not None else find_config_file()
    if source is not None and not source.is_file():
        source = None
    data, config_error = _load_toml(source)

    kwargs: dict[str, Any] = {}
    for f in fields(Config):
        if f.name == "source_path":
            continue
        kwargs[f.name] = _coerce(data, f.name)

    raw_dir = data.get("data_dir")
    if raw_dir:
        # 支持 `%LOCALAPPDATA%` 这类 Windows 变量（TOML 里写死绝对路径很不友好）
        kwargs["data_dir"] = Path(os.path.expandvars(str(raw_dir))).expanduser()
    kwargs["source_path"] = source
    kwargs["config_error"] = config_error

    cfg = Config(**kwargs)

    if use_env:
        cfg = _apply_env(cfg)
    return cfg


def _apply_env(cfg: Config) -> Config:
    """环境变量覆盖（命名与服务器版一致，敏感项不必落盘）。"""
    mapping: tuple[tuple[str, str], ...] = (
        ("HITHINK_FINANCE_API_KEY", "hithink_api_key"),
        ("FEISHU_APP_ID", "feishu_app_id"),
        ("FEISHU_APP_SECRET", "feishu_app_secret"),
        ("FEISHU_CHAT_ID", "feishu_chat_id"),
        ("FEISHU_RECEIVE_ID_TYPE", "receive_id_type"),
        ("WATCHLIST_MAX", "watchlist_max"),
        ("HISTORY_YEARS", "history_years"),
        ("MIN_HISTORY_YEARS", "min_history_years"),
        ("MIN_SYMBOLS", "min_symbols"),
        ("MAX_STALE_TRADING_DAYS", "max_stale_trading_days"),
        ("DOWNLOAD_MAX_ATTEMPTS", "download_max_attempts"),
        ("DOWNLOAD_CONNECT_TIMEOUT", "download_connect_timeout"),
        ("DOWNLOAD_READ_TIMEOUT", "download_read_timeout"),
        ("TRADE_CAPITAL", "trade_capital"),
        ("TRADE_POSITION_PCT", "trade_position_pct"),
        ("TRADE_MAX_POSITIONS", "trade_max_positions"),
        ("TRADE_BUY_SLIPPAGE", "buy_slippage"),
        ("TRADE_SELL_SLIPPAGE", "sell_slippage"),
        ("INTRADAY_INTERVAL", "intraday_interval"),
        ("INTRADAY_STOP_LOSS", "stop_loss"),
        ("INTRADAY_TAKE_PROFIT", "take_profit"),
        ("RUN_AT", "run_at"),
        ("LAOA_RUN_AT", "run_at"),          # 旧名，兼容早期配置
        ("RUN_AT_FALLBACK", "run_at_fallback"),
        ("DATA_DIR", "data_dir"),
    )
    for env_name, attr in mapping:
        raw = _env_str(env_name)
        if raw is None:
            continue
        default = getattr(cfg, attr)
        if isinstance(default, bool):
            setattr(cfg, attr, _as_bool(raw, default))
        elif isinstance(default, float) and not isinstance(default, bool):
            setattr(cfg, attr, _as_float(raw, default))
        elif isinstance(default, int):
            setattr(cfg, attr, _as_int(raw, default))
        elif isinstance(default, Path):
            setattr(cfg, attr, Path(os.path.expandvars(raw)).expanduser())
        else:
            setattr(cfg, attr, raw)

    env_list = (
        ("LAOA_ENABLED_GROUPS", "enabled_groups"),
        ("LAOA_ENABLED_STRATEGIES", "enabled_strategies"),
        ("NOTIFY_CHANNELS", "notify_channels"),
    )
    for env_name, attr in env_list:
        raw = _env_str(env_name)
        if raw is not None:
            setattr(cfg, attr, _as_list(raw, getattr(cfg, attr)))

    # 数值型频道参数（别混进布尔那一组："2000" 会被当成"不在真值表里 = False"）
    duration = _env_str("NOTIFY_TRAY_DURATION_MS")
    if duration is not None:
        cfg.notify_tray_duration_ms = _as_int(duration, cfg.notify_tray_duration_ms)

    for env_name, attr in (
        ("FEISHU_ON", "feishu_on"),
        ("WATCHLIST_IN_POOL", "watchlist_in_pool"),
        ("AUTO_DOWNLOAD_ON_START", "auto_download_on_start"),
        ("AUTO_RUN", "auto_run"),
        ("AUTO_DOWNLOAD_ON_START", "auto_download_on_start"),
        ("NOTIFY_WINDOWS_SOUND", "notify_windows_sound"),
        ("NOTIFY_WINDOWS_OPEN_URL", "notify_windows_open_url"),
        ("NOTIFY_FEISHU", "notify_feishu"),
        ("NOTIFY_WINDOWS", "notify_windows"),
        ("NOTIFY_TRAY", "notify_tray"),
    ):
        value = _env_bool(env_name)
        if value is not None:
            setattr(cfg, attr, value)
    return cfg


_config: Config | None = None


def get_config(reload: bool = False) -> Config:
    """全局配置单例（GUI 与 CLI 共用）。"""
    global _config
    if _config is None or reload:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """注入配置（测试与 CLI 显式指定路径时用）。"""
    global _config
    _config = cfg


# ── 写回配置文件（**保留注释与未识别的键**）──
#
# 为什么不用 tomlkit 之类的库：桌面版不想为"改几个开关"多背一个依赖，
# 而且更关键的是**用户的 config.toml 是他自己写的**：里面有他加的注释、
# 备注、甚至我们还不认识的新键（比如将来版本、或他留的待办）。
# 一旦用 dict → TOML 全量重写，这些都会被抹掉。所以这里做**行级就地替换**：
# 只改动目标键那一行（含多行数组），其余字节原样保留。

def _toml_value(value: Any) -> str:
    """把 Python 值格式化成 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


#: 顶层 `key = value` 行（不含缩进里的表内键）
_KEY_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<rest>.*)$")


def _split_value_and_comment(rest: str) -> tuple[str, str]:
    """把 `值  # 注释` 拆成 (值, 注释)；字符串里的 `#` 不算注释。"""
    in_string = False
    escaped = False
    for i, ch in enumerate(rest):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if ch == "#" and not in_string:
            return rest[:i], rest[i:]
    return rest, ""


def _array_end_line(lines: list[str], start: int) -> int:
    """多行数组：返回数组闭合所在的行号（同一行闭合时返回 start）。"""
    depth = 0
    for i in range(start, len(lines)):
        line = lines[i]
        # 去掉注释与字符串，避免把注释里的括号算进去
        code, _comment = _split_value_and_comment(line)
        depth += code.count("[") - code.count("]")
        if depth <= 0:
            return i
    return len(lines) - 1


def render_config_updates(text: str, updates: dict[str, Any]) -> str:
    """在配置文本里就地更新若干**顶层键**，其余内容（注释/未知键/表）原样保留。

    Args:
        text: 原 config.toml 内容。
        updates: {键: 新值}；值会按 TOML 字面量格式化。

    Returns:
        新的配置文本。未出现的键会追加到文件末尾。
    """
    lines = text.splitlines()
    remaining = dict(updates)
    out: list[str] = []
    #: 当前处在哪个表里（"" = 顶层）。TOML 里表一旦开始就一直到文件结束或下一个表头，
    #: 所以必须**跟踪状态**，不能只看当前行是不是以 `[` 开头。
    section = ""
    first_table_at: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            if first_table_at is None:
                first_table_at = len(out)
        # 表内的键与顶层同名时**绝不能**被我们的更新误伤
        match = None if section or stripped.startswith("#") else _KEY_LINE.match(line)
        key = match.group("key") if match else None
        if key in remaining:
            value_text, comment = _split_value_and_comment(match.group("rest"))
            end = _array_end_line(lines, i) if "[" in value_text else i
            if end != i and not comment:
                # 多行数组的注释常常写在闭合那一行（`]  # 说明`），别把它丢了
                _code, tail_comment = _split_value_and_comment(lines[end])
                comment = tail_comment
            new_line = f"{key} = {_toml_value(remaining.pop(key))}"
            if comment:
                new_line += f"  {comment}"
            out.append(new_line)
            i = end + 1
            continue
        out.append(line)
        i += 1

    if remaining:
        block = ["# ── 以下由「设置」面板写入 ──"]
        block += [f"{key} = {_toml_value(value)}" for key, value in remaining.items()]
        if first_table_at is not None:
            # **必须插到第一个表头之前**：TOML 没有"表结束"语法，追加到文件末尾
            # 会被解析成最后那张表里的键（用户表的键就被污染了，顶层也读不到）
            if first_table_at > 0 and out[first_table_at - 1].strip():
                block.insert(0, "")
            out[first_table_at:first_table_at] = block + [""]
        else:
            if out and out[-1].strip():
                out.append("")
            out.extend(block)

    new_text = "\n".join(out)
    return new_text + ("\n" if text.endswith("\n") or new_text else "")


#: 值类型是 Path 的顶层键（`save_settings` 时把界面传来的字符串收紧成 Path）
_PATH_FIELDS = frozenset({"data_dir", "source_path"})


def update_config_file(
    path: Path | str | None,
    updates: dict[str, Any],
    *,
    create: bool = True,
) -> Path:
    """把若干顶层键写回 config.toml（保留原有注释、未知键与表结构）。

    Args:
        path: 配置文件路径；None 时用 `find_config_file()`，都没有则落到
            `~/.config/laoa-trader/config.toml`（保证"保存"总有地方可写）。
        updates: {键: 新值}。
        create: 文件不存在时是否创建。

    Returns:
        实际写入的路径。

    Raises:
        OSError: 写不进去（只读盘/权限），调用方负责转成中文提示。
    """
    target = Path(path) if path is not None else find_config_file()
    if target is None:
        target = Path.home() / ".config" / "laoa-trader" / "config.toml"

    original = ""
    if target.is_file():
        try:
            original = target.read_text(encoding="utf-8")
        except OSError:
            original = ""
    elif not create:
        raise FileNotFoundError(f"配置文件不存在：{target}")

    new_text = render_config_updates(original, updates)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换：中途失败不会把用户的配置截断成半截
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(target)
    return target


def save_settings(cfg: Config, updates: dict[str, Any]) -> tuple[Path, Config]:
    """写回设置并**同步内存里的配置对象**（界面保存后用）。

    写回后立刻把新值赋给传入的 cfg，避免"文件改了、界面还在用旧值"。

    Returns:
        (写入路径, 更新后的 cfg)
    """
    path = update_config_file(cfg.source_path, updates)
    for key, value in updates.items():
        if hasattr(cfg, key):
            # 路径类字段：界面传来的是 QLineEdit 的字符串，这里统一收紧成 Path ——
            # 否则 `cfg.data_dir / "logs"` 这种写法会 TypeError: unsupported operand
            # type(s) for /: 'str' and 'str'（首次运行向导保存数据目录后就会踩到）
            if key in _PATH_FIELDS and isinstance(value, str):
                value = Path(value)
            setattr(cfg, key, value)
    cfg.source_path = path
    set_config(cfg)
    return path, cfg

