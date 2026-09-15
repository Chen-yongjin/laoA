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
#: 数据/配置目录名（`%LOCALAPPDATA%\LaoATrader`）。**故意保持旧名**：它是老用户
#: 已经下好的历史数据库所在目录，改成新名字会让程序去空目录里找、逼用户重下 180MB。
#: 产品显示名是「老A选股助手」（见 `ui/app.py` 的 `APP_NAME`），两者不必一致。
DEFAULT_APP_NAME = "LaoATrader"

#: 支持的通知频道（顺序 = 界面与 --doctor 的展示顺序）
CHANNELS: tuple[str, ...] = ("windows", "feishu", "tray")

#: 股票池的两种视图（界面右上角【切换为卡片/表格】）；写错的值当 `cards`
POOL_VIEWS: tuple[str, ...] = ("cards", "table")

#: 界面主题：`silver` = 银色金属感（默认）/ `system` = 系统默认皮肤（安全绳）。
#: 为什么常量放在 config 里而不是 `ui/theme.py`：配置层要在**没有任何 Qt** 的环境下
#: 也能 import（CLI、服务器版共存、打包前的静态检查），而 theme.py 会用到 Qt。
#: 取值只有这一处，`theme.normalize_theme` 也读它，不会出现两份定义。
UI_THEMES: tuple[str, ...] = ("silver", "system")
DEFAULT_UI_THEME = "silver"

#: 大盘概览默认盯的宽基指数（同花顺代码）。实测这五个都能取到；
#: `899050.BJ`（北证50）**不存在**，混进去会让整批快照失败，别往这里加。
DEFAULT_MARKET_INDICES: tuple[str, ...] = (
    "000001.SH",    # 上证指数（它的成交额就是沪市成交额）
    "399001.SZ",    # 深证成指（它的成交额就是深市成交额）
    "399006.SZ",    # 创业板指
    "000688.SH",    # 科创50
    "000300.SH",    # 沪深300
)

#: 市场情绪指数默认值（这组是**给用户自己换的**：同一个端点，换个代码就换一套心情指标）。
DEFAULT_MARKET_SENTIMENT: tuple[str, ...] = (
    "883404.TI",    # 同花顺情绪指数
    "883958.TI",    # 昨日连板
    "883994.TI",    # 昨日打首板表现
    "883418.TI",    # 微盘股
)

#: 板块指数默认值（同花顺一级行业指数，目录 tag=industry）：银行、证券
DEFAULT_MARKET_SECTOR: tuple[str, ...] = ("881155.TI", "881157.TI")

#: 概览默认 TTL（秒）：界面 5 秒刷一次状态栏，但概览到点才真的打接口
DEFAULT_MARKET_OVERVIEW_TTL = 55

#: 竞价扫描的四个板块（**代码判定在 `intraday.board_of`**：`.BJ` 后缀最稳，
#: 没后缀时按前缀推 —— 43x/83x/87x/88x/92x 是北交所）。
#: 顺序即界面上的顺序（主板 → 创业板 → 科创板 → 北交所）。
AUCTION_BOARDS: tuple[str, ...] = ("main", "chinext", "star", "bj")
#: 板块 key → 中文短标签（推送、详情、设置页共用这一份）
AUCTION_BOARD_LABELS: dict[str, str] = {
    "main": "主板",
    "chinext": "创业板",
    "star": "科创板",
    "bj": "北交所",
}
#: 打分门限的取值范围（下限 1；上限 = **打分满分 6**：涨幅 2 + 量比 2 + 未匹配 1 + 成交额 1，
#: 写 8 也只会是"满分才推"，夹到 6 更贴近本意）
AUCTION_SCORE_RANGE: tuple[int, int] = (1, 6)
#: 推送条数的取值范围（全市场扫描，上限放宽到 50）
AUCTION_ITEMS_RANGE: tuple[int, int] = (1, 50)
#: 扫描宽限窗口（分钟）：调度器是 60 秒一拍、相位不固定，卡在 09:20:00 那一秒上不现实；
#: 到点后这段时间内跑一次就算这一档完成（而且**不会跨到下一档**，见 `intraday.auction_scan_due`）
AUCTION_SCAN_GRACE_MIN = 3

def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """把整数夹进 `[low, high]`；写错（乱码/None）回默认。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)

#: 提醒浮窗 / 闪烁的默认值（`config.example.toml`、README 与界面提示都引用这一份）
DEFAULT_NOTIFY_POPUP_SECONDS = 8
DEFAULT_NOTIFY_POPUP_MAX_ITEMS = 5
DEFAULT_NOTIFY_FLASH_SECONDS = 6

#: 这些布尔键写错时**回到默认值**（而不是像其它布尔键那样按 False 处理）。
#: 为什么单独一群：概览是"看一眼"的辅助信息，把 `market_overview` 手滑写成
#: `"maybe"` 应该是"没生效、仍是默认开着"，而不是"功能被悄悄关掉了"。
#: 提醒浮窗与提示音同理 —— 写错一个词不该让"提醒"这个核心功能静默消失。
_STRICT_BOOL_FIELDS = frozenset(
    {"market_overview", "market_breadth", "notify_popup", "notify_sound"}
)

#: 严格的真值 / 假值（与 `_as_bool` 的真值表保持一致）
_TRUE_WORDS: tuple[str, ...] = ("1", "true", "yes", "on", "y", "是")
_FALSE_WORDS: tuple[str, ...] = ("0", "false", "no", "off", "n", "否")

#: 旧字段名 → 新字段名（竞价那一组改名：`auction_alert_min_*` → `auction_min_*`）。
#: 读 config.toml 时两者都认、**新名优先**（见 `load_config`）。
_LEGACY_FIELD_ALIASES: dict[str, str] = {
    "auction_alert_min_pct": "auction_min_pct",
    "auction_alert_min_volume_ratio": "auction_min_volume_ratio",
    "auction_alert_min_amount": "auction_min_amount",
    "auction_alert_min_score": "auction_min_score",
}


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


def _as_bool_strict(value: Any, default: bool) -> bool:
    """认得出来的真假值才转布尔，**认不出来就回到默认**（见 `_STRICT_BOOL_FIELDS`）。

    与 `_as_bool` 的区别只有一处：手滑写成 `"maybe"` 这类字符串时，
    `_as_bool` 会当 False（= 把功能关掉），这里回到默认值。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def _ttl_seconds(value: Any) -> int:
    """概览 TTL 收紧成**正整数秒**；非法值（0 / 负数 / 乱码）回到默认 55。

    为什么不让 0 表示"不缓存"：这个键的值是"多久打一次接口"，写 0 会让界面
    每 5 秒打一轮（配额和限流都吃不消）—— 这几乎不可能是本意，按默认值处理更安全。
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_MARKET_OVERVIEW_TTL
    return seconds if seconds > 0 else DEFAULT_MARKET_OVERVIEW_TTL


def split_scan_at(value: Any) -> tuple[list[str], list[str]]:
    """把"扫描时刻"收紧成规范写法：→ `(["09:20", "09:25"], ["09:70"])`。

    接受的写法：`"09:20"` / `"9:5"`（补零成 `09:05`）/ `"09:20,09:25"` /
    `[" 09:20 ", "09:25"]`。**认不出的项直接丢掉、不抛异常**（配置写错不该让程序起不来），
    但会把丢弃的原样返回 —— 设置页据此提示"哪一项没认出来"，而不是静默少扫一次。
    结果去重并按时间升序（扫描顺序 = 时间顺序）。
    """
    if isinstance(value, str):
        items: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return [], []
    good: list[str] = []
    bad: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        parts = text.split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            bad.append(text)
            continue
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            bad.append(text)
            continue
        stamp = f"{hour:02d}:{minute:02d}"
        if stamp not in good:
            good.append(stamp)
    return sorted(good), bad


def parse_scan_at(value: Any) -> list[str]:
    """只取合法的那部分（`config.auction_scan_at` 的规范化用）。"""
    return split_scan_at(value)[0]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """整数必须落在 `[minimum, maximum]` 内；写错或越界**一律回默认值**。

    为什么不把越界值夹到边界（`min(max(...))`）：`notify_popup_seconds` 写成 0
    或 -1 是"手滑"，夹成 1 秒等于"浮窗一闪就没了"，用户只会觉得功能坏了；
    回到默认 8 秒更符合本意。条数那类"多了也还能用"的参数才用夹取。
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if minimum <= number <= maximum else default


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
    # ── 自绘提醒浮窗（QQ 式：响声 + 图标闪烁 + 右下角弹出列表）──
    #: 是否弹自绘浮窗。为什么不用 Windows 原生 Toast 兜底：它的**点击行为不受我们控制**
    #: （点不开、看不到内容），等于没提醒 —— 详见 `ui/alert_popup.py` 的说明。
    notify_popup: bool = True
    #: 浮窗自动消失的秒数（鼠标停在浮窗上时不消失）；非法值回默认
    notify_popup_seconds: int = 8
    #: 浮窗最多列几条（1~10，与「竞价提醒条数」同一个上限口径）
    notify_popup_max_items: int = 5
    #: 提醒时是否响一声（Windows 系统提示音，只用标准库 `winsound`；非 Windows 静默跳过）
    notify_sound: bool = True
    #: 托盘 / 任务栏图标闪烁几秒（0 = 不闪）
    notify_flash_seconds: int = 6
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
    #: 集合竞价**全市场扫描**提醒（9:15–9:25 的真实买卖盘）：**默认关**。
    #: 代码（客户端 `auction_snapshot`、解析、打分、过滤、卡片那一行、设置页那一组、
    #: 详情里的"竞价扫描结果"）都已就绪并通过测试，但**口径与阈值还在与用户确认** ——
    #: 所以先关着：默认流程**一次竞价请求都不发**、界面不显示竞价行、不产生竞价提醒。
    #: 确认后把这一项改成 True（或到设置页勾上）即可启用，不用改代码。
    intraday_auction: bool = False
    #: 扫描时刻（HH:MM 列表，默认 9:20 与 9:25 各一次）。
    #: **为什么不每分钟扫**：全市场 5573 只按 100/批 = 56 个请求，9:15–9:25 每分钟一轮
    #: 就是 560 个请求 —— 配额会被打光、也容易触发限流。9:25 那次拿到的是**竞价终态**。
    auction_scan_at: list[str] = field(default_factory=lambda: ["09:20", "09:25"])
    #: 竞价涨幅**下限**（%）：低于它的直接过滤（用户点名的"竞价涨幅比例"）。默认 +2.0%。
    auction_min_pct: float = 2.0
    #: 竞价涨幅**上限**（%）：≥ 它的直接过滤 —— 一字板/接近涨停**买不进**，推了没意义。
    #: 依据：某日 25 只后期涨停里**没有一只**竞价涨幅 ≥9%，所以这条几乎零成本。
    auction_max_pct: float = 9.0
    #: 参与扫描的板块（多选）：取值见 `AUCTION_BOARDS`（主板/创业板/科创板/北交所）。
    #: 只勾科创 + 创业板 = "只看双创"。**一项都不勾 = 不限制（等于全选）**。
    auction_boards: list[str] = field(default_factory=lambda: list(AUCTION_BOARDS))
    #: 竞价成交额下限（元）：**不到这个数就不参与**（不是只扣分）。
    #: 为什么必须有这道门槛：竞价量太小时 `未匹配量 ÷ 成交量` 会爆表
    #: （实测某小盘股 +29.46），拿它判断强弱等于把噪音当信号。默认 500 万 ——
    #: 依据：全市场只有 **8.7%** 的股票过这条线（嫌严可以调到 300 万）。
    auction_min_amount: float = 5e6
    #: 竞价"打分"里的放量门槛（量比）：`auction_volume_ratio >= 这个值` 得 2 分，
    #: 达到它的 75% 得 1 分。默认 2.0（实测约 p90）。
    #: 注意：这是**打分**用的，不是硬过滤（量比低但高开很多的票照样能进）。
    auction_min_volume_ratio: float = 2.0
    #: 打分门限：`分 >= 这个值` → 命中（默认 **2**）。
    #: 为什么是 2 不是 3：真实全市场数据回测 —— 当日后期涨停覆盖率 24% vs 20%，
    #: 而池子里的推送量 0.44 vs 0.29 条/天 → **2 更划算**（多推一点、多覆盖一截）。
    #: 弱的分门限 = `-(这个值 - 1)`（低开/卖盘剩余占优）。
    auction_min_score: int = 2
    #: 一次扫描最多推几只（默认 10，**上限 50**）：全市场扫描命中面更宽，条数也放宽。
    auction_alert_max_items: int = 10

    #: 当日异动（涨停/跌停/大幅上涨下跌/快速反弹跳水）实时提醒：默认开。
    #: 数据源是**全市场一条请求**，再在本地按"自己的票"过滤（不逐只问）。
    intraday_anomaly: bool = True
    #: 只关心这些异动标签（空列表 = 全部）；取值见 `intraday.ANOMALY_TAGS`
    anomaly_alert_tags: list[str] = field(default_factory=list)

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
    #: 界面主题：`silver` = 银色金属感（默认）/ `system` = 系统默认皮肤。
    #: 设置页下拉框可切换，**改完立即生效**（不用重启）；写错一个字母就回默认。
    ui_theme: str = DEFAULT_UI_THEME

    # ── 大盘概览（启动默认页：涨跌停家数 / 成交额 / 涨跌家数 / 三组指数）──
    #: 总开关：关掉则概览**一个请求都不发**（卡片显示 `—` 并说明原因）
    market_overview: bool = True
    #: 宽基指数（同花顺代码；也接受 `000001.SH=我的上证` 自定义显示名）
    market_indices: list[str] = field(
        default_factory=lambda: list(DEFAULT_MARKET_INDICES)
    )
    #: 市场情绪指数（与宽基**同一个端点**，单独一组就是为了让你自己换口径）
    market_sentiment_indices: list[str] = field(
        default_factory=lambda: list(DEFAULT_MARKET_SENTIMENT)
    )
    #: 板块指数（同花顺一级行业指数，例如 881155.TI 银行 / 881157.TI 证券）
    market_sector_indices: list[str] = field(
        default_factory=lambda: list(DEFAULT_MARKET_SECTOR)
    )
    #: 全市场快照汇总（北交所成交额 + 涨跌家数）：**默认开** —— 涨跌家数是概览页里
    #: 最直观的一项，整页只显示沪深两市成交额会显得空。代价是 6 个分页请求，
    #: 所以它有独立的 5 分钟缓存（见 `market._BREADTH_TTL`），不跟着页面每分钟的刷新跑。
    market_breadth: bool = True
    #: 概览的 TTL 缓存（秒）：到点才真的打接口，不跟着 5 秒的界面定时器跑
    market_overview_ttl: int = DEFAULT_MARKET_OVERVIEW_TTL

    #: 记录配置实际来自哪个文件（状态栏展示用）
    source_path: Path | None = None
    #: 配置文件解析/读取失败的原因（空 = 一切正常）
    config_error: str = ""

    def __post_init__(self) -> None:
        """把界面偏好与概览参数收紧到合法取值。

        为什么要在这里做：`pool_view` 是给界面看的，手改 config.toml 写错一个字母
        （"card" / "Cards" / 中文）不该表现成"股票池页打不开/空白"——
        统一按 `cards` 处理，用户看到的仍然是一个能用的界面。
        概览的 TTL 同理：写 0/负数会让"到点才打接口"这条规矩失效，
        退回默认 55 秒，界面不会被自己的定时器打死。
        """
        value = str(self.pool_view or "").strip().lower()
        self.pool_view = value if value in POOL_VIEWS else "cards"
        # 主题同理：写错（"Silver " / "银色" / 少个字母）不该让界面起不来 —— 回默认
        theme = str(self.ui_theme or "").strip().lower()
        self.ui_theme = theme if theme in UI_THEMES else DEFAULT_UI_THEME
        # 竞价阈值：写 0/负数会让"打分"失真（任何票都算强）→ 回默认值
        for name, fallback in (("auction_min_pct", 2.0),
                               ("auction_min_volume_ratio", 2.0),
                               ("auction_min_amount", 5e6)):
            value = getattr(self, name, fallback)
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = fallback
            setattr(self, name, number if number > 0 else fallback)
        # 涨幅上限：必须**大于**下限（否则一条都留不下），非法/倒挂回默认 9.0；
        # 若下限本身就 ≥9（手写成 15），再把下限也拉回默认 —— 不能让两个值互相打架
        try:
            cap = float(self.auction_max_pct)
        except (TypeError, ValueError):
            cap = 9.0
        if cap <= self.auction_min_pct:
            cap = 9.0
        if cap <= self.auction_min_pct:
            self.auction_min_pct = 2.0
        self.auction_max_pct = cap
        # 打分门限夹在 1~6（满分就是 6）、条数夹在 1~50
        self.auction_min_score = _clamp_int(
            self.auction_min_score, *AUCTION_SCORE_RANGE, default=2
        )
        self.auction_alert_max_items = _clamp_int(
            self.auction_alert_max_items, *AUCTION_ITEMS_RANGE, default=10
        )
        # 板块多选：只留认得出的 key；**一个都没勾 = 不限制（全选）**，
        # 免得用户手滑把四个都取消之后看到"0 只命中"却不知道为什么
        picked = [str(b).strip().lower() for b in (self.auction_boards or [])]
        boards = [b for b in AUCTION_BOARDS if b in picked]
        self.auction_boards = boards or list(AUCTION_BOARDS)
        # 扫描时刻：解析成 `HH:MM`、去重、排序；一个都不合法 → 回默认（9:20/9:25）
        self.auction_scan_at = parse_scan_at(self.auction_scan_at) or ["09:20", "09:25"]
        # 提醒浮窗：秒数收紧在 1~120，"多少条"夹在 1~10（与竞价条数同一个口径）。
        # 秒数非法回默认、条数越界夹取 —— 两种处理不同是**故意的**：
        # 0 秒 = "浮窗一闪就没"，写这个值几乎不可能是本意；而 8 条只是"多列两行"，
        # 按 10 条办比丢回 5 条更贴近用户的手滑意图。
        self.notify_popup_seconds = _bounded_int(
            self.notify_popup_seconds, DEFAULT_NOTIFY_POPUP_SECONDS, 1, 120
        )
        try:
            self.notify_popup_max_items = min(max(int(self.notify_popup_max_items), 1), 10)
        except (TypeError, ValueError):
            self.notify_popup_max_items = DEFAULT_NOTIFY_POPUP_MAX_ITEMS
        # 闪烁 0 是**合法**的（= 不闪），所以下界从 0 起；负数/乱码回默认 6 秒
        self.notify_flash_seconds = _bounded_int(
            self.notify_flash_seconds, DEFAULT_NOTIFY_FLASH_SECONDS, 0, 120
        )
        # 异动标签统一成大写、去空（接口给的枚举是大写）
        tags = self.anomaly_alert_tags
        if isinstance(tags, str):
            tags = [x for x in tags.replace("，", ",").split(",")]
        self.anomaly_alert_tags = [
            str(tag).strip().upper() for tag in (tags or []) if str(tag).strip()
        ]
        self.market_overview_ttl = _ttl_seconds(self.market_overview_ttl)

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


def _coerce_value(name: str, value: Any) -> Any:
    """按字段 `name` 的类型收紧一个原始值（`_coerce` 与旧键名别名共用）。"""
    default = getattr(Config(), name)
    if value is None:
        return default
    if isinstance(default, list):
        return _as_list(value, default)
    if isinstance(default, bool):
        # 概览那几个开关"写错 = 回到默认"，其余布尔键沿用原语义（写错 = 假）
        return (
            _as_bool_strict(value, default)
            if name in _STRICT_BOOL_FIELDS
            else _as_bool(value, default)
        )
    if isinstance(default, float):
        return _as_float(value, default)
    if isinstance(default, int):
        return _as_int(value, default)
    return _as_str(value)


def _coerce(section: dict[str, Any], name: str) -> Any:
    """把 TOML 里的值转成目标字段的类型（TOML 没有布尔字符串这类问题，但手改容易写错）。"""
    return _coerce_value(name, section.get(name))


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

    # 旧键名（上一版的 `auction_alert_min_*`）继续认，但**新名优先**：
    # 改名不该让用户手写的 config.toml 静默失效（他只会看到"设置没生效"）
    for old_key, new_key in _LEGACY_FIELD_ALIASES.items():
        if new_key not in data and old_key in data:
            kwargs[new_key] = _coerce_value(new_key, data[old_key])

    raw_dir = data.get("data_dir")
    if raw_dir:
        # 支持 `%LOCALAPPDATA%` 这类 Windows 变量（TOML 里写死绝对路径很不友好）
        kwargs["data_dir"] = Path(os.path.expandvars(str(raw_dir))).expanduser()
    kwargs["source_path"] = source
    kwargs["config_error"] = config_error

    cfg = Config(**kwargs)

    if use_env:
        cfg = _apply_env(cfg)
    # 环境变量是**构造之后**才盖上去的：这里再跑一次归一，
    # 非法值才会退回默认（主题名、涨跌停阈值、异动标签大小写等都靠这一步）
    cfg.__post_init__()

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
        ("INTRADAY_AUCTION", "intraday_auction"),
        # 旧名（上一版的 `auction_alert_min_*`）继续认：**排在新名前面**，
        # 两个都设时后写的（新名）赢 —— 手写过的环境变量不该因为改名就失效
        ("AUCTION_ALERT_MIN_PCT", "auction_min_pct"),
        ("AUCTION_ALERT_MIN_VOLUME_RATIO", "auction_min_volume_ratio"),
        ("AUCTION_ALERT_MIN_AMOUNT", "auction_min_amount"),
        ("AUCTION_ALERT_MIN_SCORE", "auction_min_score"),
        ("AUCTION_ALERT_MAX_ITEMS", "auction_alert_max_items"),
        ("AUCTION_MIN_PCT", "auction_min_pct"),
        ("AUCTION_MAX_PCT", "auction_max_pct"),
        ("AUCTION_MIN_AMOUNT", "auction_min_amount"),
        ("AUCTION_MIN_SCORE", "auction_min_score"),
        ("AUCTION_MIN_VOLUME_RATIO", "auction_min_volume_ratio"),
        ("AUCTION_ALERT_MAX_ITEMS", "auction_alert_max_items"),
        ("INTRADAY_ANOMALY", "intraday_anomaly"),
        ("INTRADAY_STOP_LOSS", "stop_loss"),
        ("INTRADAY_TAKE_PROFIT", "take_profit"),
        ("UI_THEME", "ui_theme"),           # 界面主题（分发后可临时切回系统皮肤）
        ("NOTIFY_POPUP_SECONDS", "notify_popup_seconds"),   # 浮窗自动消失秒数
        ("NOTIFY_POPUP_MAX_ITEMS", "notify_popup_max_items"),  # 浮窗最多列几条
        ("NOTIFY_FLASH_SECONDS", "notify_flash_seconds"),   # 图标闪烁秒数
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
        ("AUCTION_SCAN_AT", "auction_scan_at"),
        ("AUCTION_BOARDS", "auction_boards"),
        ("MARKET_INDICES", "market_indices"),
        ("MARKET_SENTIMENT_INDICES", "market_sentiment_indices"),
        ("MARKET_SECTOR_INDICES", "market_sector_indices"),
        ("ANOMALY_ALERT_TAGS", "anomaly_alert_tags"),
    )
    for env_name, attr in env_list:
        raw = _env_str(env_name)
        if raw is not None:
            setattr(cfg, attr, _as_list(raw, getattr(cfg, attr)))

    # 数值型频道参数（别混进布尔那一组："2000" 会被当成"不在真值表里 = False"）
    duration = _env_str("NOTIFY_TRAY_DURATION_MS")
    if duration is not None:
        cfg.notify_tray_duration_ms = _as_int(duration, cfg.notify_tray_duration_ms)

    # 概览的 TTL 与两个开关：TTL 走"正整数秒"，开关走严格布尔（环境变量写错也回默认）
    ttl = _env_str("MARKET_OVERVIEW_TTL")
    if ttl is not None:
        cfg.market_overview_ttl = _ttl_seconds(ttl)
    for env_name, attr in (
        ("MARKET_OVERVIEW", "market_overview"),
        ("MARKET_BREADTH", "market_breadth"),
        # 浮窗与提示音两个开关也走"严格布尔"：环境变量里写 `NOTIFY_POPUP=maybe`
        # 应该是"没生效、仍是默认开着"，而不是把一个核心功能悄悄关掉
        ("NOTIFY_POPUP", "notify_popup"),
        ("NOTIFY_SOUND", "notify_sound"),
    ):
        raw = _env_str(env_name)
        if raw is not None:
            setattr(cfg, attr, _as_bool_strict(raw, getattr(cfg, attr)))

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

