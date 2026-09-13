"""后台线程调度：每天 `run_at` 跑一次日更，交易时段内按间隔跑盘中提醒。

为什么要自己写线程调度（而不是用 Windows 任务计划）
--------------------------------------------------
1. 桌面版要"双击就能全自动"：装任务计划需要管理员权限、还要多一个配置入口；
2. 盘中提醒本来就是**进程内高频轮询**（每分钟一轮），任务计划做不了；
3. 线程模式在开发机（Linux）上也能跑，方便验证。

线程模型
--------
一个守护线程 `_loop`，每 1 秒醒一次，判断两件事：

- **日更**：本地时间到 `run_at`（默认 19:15）且当天交易日、当天还没跑过 → 跑
  「数据增量 → 策略 → 建池 → 推送」；
- **盘中**：在交易时段（09:30-11:30 / 13:00-15:00）且距上轮 ≥ `intraday_interval`
  秒 → 跑一轮盘中规则，命中的提醒并行推送三路通知。

所有异常都在线程内被捕获并记进 `status()["last_error"]` ——
后台线程一旦抛异常就会静默死掉，那是桌面程序最难查的 bug。
"""

from __future__ import annotations

import threading
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from laoa_trader import intraday, pool, state
from laoa_trader.config import Config, get_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 主循环的滴答间隔（秒）—— 够细才能"到点就触发"，又不会空转 CPU
TICK = 1.0

#: 数据闸门拦住时，同一原因的日志最多多久重复一次（秒）。
#: 调度每秒滴答一次，而"数据没下好"可能持续十几分钟 —— 不节流会把日志刷爆。
BLOCKED_LOG_INTERVAL = 300.0

#: 信号落库时每条策略保留的条数（与服务器版策略默认 top_n 一致）
SIGNAL_TOP_N = 30


#: 主跑时间的默认值（配置写坏时兜底；也是界面里的默认值）
DEFAULT_RUN_AT = "16:00"
#: 补跑时间默认值
DEFAULT_RUN_AT_FALLBACK = "19:15"
#: A 股收盘时间（用于"早于收盘"的提示）
MARKET_CLOSE = (15, 0)


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """严格解析 `HH:MM`；不合法返回 None（保存配置时要**拒绝**而不是猜）。"""
    raw = str(text or "").strip()
    if len(raw) != 5 or raw[2] != ":":
        return None
    hour, minute = raw[:2], raw[3:]
    if not (hour.isdigit() and minute.isdigit()):
        return None
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def parse_run_at(text: str) -> tuple[int, int]:
    """解析 "HH:MM"；格式不对时退回默认值（配置写错不该让程序跑不起来）。"""
    parsed = parse_hhmm(text)
    if parsed is not None:
        return parsed
    logger.warning(f"run_at 格式无法解析：{text!r}，改用默认 {DEFAULT_RUN_AT}")
    return parse_hhmm(DEFAULT_RUN_AT) or (16, 0)


def validate_run_times(primary: str, fallback: str = "") -> tuple[bool, list[str]]:
    """校验界面/CLI 传来的两个时间，返回 `(是否可保存, 提示列表)`。

    规则（需求）：
        - 格式必须是 `HH:MM`，否则**拒绝保存**；
        - 补跑时间必须**晚于**主跑时间，否则拒绝（逻辑矛盾：先补跑再主跑没意义）；
        - 主跑早于 15:00（A 股收盘）→ **允许保存但明确提示**（当天数据可能还没出来）。

    Returns:
        (ok, messages)。ok=False 时调用方**不要写配置**，把 messages 显示出来即可。
    """
    messages: list[str] = []
    primary_parsed = parse_hhmm(primary)
    if primary_parsed is None:
        messages.append(f"每天运行时间格式不对：{primary!r}（应写成 HH:MM，例如 16:00）")
    fallback_parsed = None
    if str(fallback or "").strip():
        fallback_parsed = parse_hhmm(fallback)
        if fallback_parsed is None:
            messages.append(f"补跑时间格式不对：{fallback!r}（应写成 HH:MM，例如 19:15）")
    if primary_parsed is None or (str(fallback or "").strip() and fallback_parsed is None):
        return False, messages
    if fallback_parsed is not None and fallback_parsed <= primary_parsed:
        messages.append(
            f"补跑时间（{fallback}）必须**晚于**主跑时间（{primary}），否则不会生效"
        )
        return False, messages
    if primary_parsed < MARKET_CLOSE:
        messages.append(
            f"⚠️ 主跑时间 {primary} 早于 A 股收盘（15:00）—— "
            "当天行情/涨停池可能还没出全，建议设成 16:00 之后"
        )
    return True, messages


def _nearest_trading_day(db_path: str | None, day: str) -> str | None:
    """`day`（含）之后最近的交易日；没有日历/查不到时返回 None（调用方按自然日算）。"""
    if not db_path:
        return None
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            row = conn.execute(
                "SELECT MIN(date) FROM trading_calendar WHERE date >= ?", (day,)
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:  # noqa: BLE001 - 日历读不了就退化成自然日，不该影响界面
        return None


def next_run_info(
    cfg: Config | None = None,
    db_path: str | None = None,
    *,
    now: datetime | None = None,
    done_today: bool = False,
) -> dict:
    """计算"下次自动运行"是什么时候（状态栏与 --doctor 用）。

    Returns:
        {"kind": "primary"|"fallback"|"disabled", "at": "YYYY-MM-DD HH:MM",
         "label": "今天 16:00" / "明天 16:00" / "已关闭（每天自动运行）"}
    """
    cfg = cfg or get_config()
    now = now or datetime.now()
    if not getattr(cfg, "auto_run", True):
        return {"kind": "disabled", "at": None, "label": "已关闭（每天自动运行）"}

    primary = parse_run_at(cfg.run_at)
    fallback_text = str(getattr(cfg, "run_at_fallback", "") or "").strip()
    fallback = parse_hhmm(fallback_text) if fallback_text else None

    def _label(moment: datetime) -> str:
        days = (moment.date() - now.date()).days
        prefix = "今天" if days <= 0 else ("明天" if days == 1 else moment.strftime("%m-%d"))
        return f"{prefix} {moment.strftime('%H:%M')}"

    def _on_nearest_trading_day(day: str, hm: tuple[int, int]) -> datetime:
        """把 `day`（含）之后最近的交易日与 `hm` 组合成时间点。"""
        target_day = _nearest_trading_day(db_path, day) or day
        base = datetime.strptime(target_day, "%Y-%m-%d")
        return base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)

    today = now.strftime("%Y-%m-%d")
    if not done_today:
        primary_at = _on_nearest_trading_day(today, primary)
        if now < primary_at:
            return {"kind": "primary", "at": primary_at.strftime("%Y-%m-%d %H:%M"),
                    "label": _label(primary_at)}
        if fallback is not None:
            fallback_at = _on_nearest_trading_day(today, fallback)
            if now < fallback_at:
                return {"kind": "fallback", "at": fallback_at.strftime("%Y-%m-%d %H:%M"),
                        "label": _label(fallback_at) + "（主跑未成功时的补跑）"}

    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    primary_at = _on_nearest_trading_day(tomorrow, primary)
    return {"kind": "primary", "at": primary_at.strftime("%Y-%m-%d %H:%M"),
            "label": _label(primary_at)}


def pool_fingerprint(title: str, lines: list[str]) -> str:
    """推送内容指纹（用来判断"同一批卡片今天推过没有"）。"""
    import hashlib

    text = title + "\n" + "\n".join(lines)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


#: 数据闸门拦住时给用户看的中文下一步指引（界面与 CLI 共用同一句话）
DOWNLOAD_HINT = "请先下载：点界面上的【下载/更新历史数据】，或命令行运行 --download"


def data_gate(
    cfg: Config | None = None,
    engine: DataEngine | None = None,
    *,
    today: str | None = None,
) -> dict:
    """**跑策略前的数据闸门**：本地数据到底能不能用来选股？

    这是"在不完整的数据上跑策略"这个 bug 的正解：策略、建池、通知都必须过这道门。
    判据直接复用 `preflight.check()`（纯本地、秒级、一次请求都不发）：

    - `ready` → 放行；
    - `needs_incremental` → **也放行**（数据可用，只是落后几天；`run_daily` 会先跑
      增量再选股，所以口径仍然是"先补数据再选股"）；
    - `needs_full`（空库/跨度不足/缺复权事件/落后超窗口…）→ **拦下**，给出原因与下一步。

    Args:
        cfg: 配置（阈值来源）。
        engine: 数据引擎（不传就用 `cfg.db_path`）。
        today: 覆盖"今天"（测试用）。

    Returns:
        ```python
        {"ok": bool,              # 能不能跑
         "status": "ready"|"needs_incremental"|"needs_full",
         "reason": "中文原因",
         "message": "可直接显示给用户的一句话",
         "result": {...}}         # 原始 preflight 结果
        ```
    """
    from laoa_trader.data import preflight

    cfg = cfg or get_config()
    db_path = engine.db_path if engine is not None else cfg.db_path
    try:
        result = preflight.check(db_path, cfg, today=today)
    except Exception as exc:  # noqa: BLE001 - 自检自己出错不能变成"崩界面"
        reason = f"数据自检失败（{type(exc).__name__}: {exc}）"
        return {"ok": False, "status": preflight.NEEDS_FULL, "reason": reason,
                "message": f"{reason}；{DOWNLOAD_HINT}", "result": {}}

    status = result.get("status")
    reason = result.get("reason") or ""
    if status == preflight.READY:
        return {"ok": True, "status": status, "reason": reason,
                "message": preflight.summary_line(result), "result": result}
    if status == preflight.NEEDS_INCREMENTAL:
        return {"ok": True, "status": status, "reason": reason,
                "message": preflight.summary_line(result), "result": result}
    return {
        "ok": False,
        "status": preflight.NEEDS_FULL,
        "reason": reason,
        "message": f"本地无可用历史数据（{reason}）；{DOWNLOAD_HINT}",
        "result": result,
    }


def run_daily(
    cfg: Config | None = None,
    engine: DataEngine | None = None,
    *,
    notify: bool = True,
    progress_cb: sync.ProgressCb | None = None,
    selection: Any = None,
    with_data: bool = True,
    stage_cb: Any = None,
) -> dict:
    """跑一次完整的日更流程：数据增量（可选）→ 策略 → 建池 → 推送。

    **界面上的【立即选股并建池】与 19:15 的定时任务共用这一个函数** ——
    口径一致才谈得上幂等：信号按 (行情日, 策略, 代码) upsert、池子按
    (行情日, 代码) upsert、推送按内容指纹去重（同一天同一批内容只推一次）。

    Args:
        cfg: 配置。
        engine: DataEngine。
        notify: 是否推送（False = 只入库；CLI/界面的"不推送"试跑用）。
        progress_cb: 数据同步进度回调 `(stage, done, total)`。
        selection: 启用的组/策略；None 时按配置解析（`enabled_groups`/`enabled_strategies`）。
        with_data: 是否先跑一次数据增量（只想重算选股时传 False）。
        stage_cb: 阶段名回调（界面状态栏显示"跑策略/建池/推送通知"）。

    Returns:
        {"sync": [...], "picks": n, "signals": n, "pool": [...], "notify": {...},
         "errors": [...], "data_date":…, "selection": {...},
         "pushed": bool, "push_skipped": str|None}
    """
    cfg = cfg or get_config()
    engine = engine or DataEngine(cfg.db_path)
    from laoa_trader.strategy import groups as groups_mod

    selection = selection if selection is not None else groups_mod.resolve_from_config(cfg)
    report: dict[str, Any] = {
        "sync": [], "picks": 0, "signals": 0, "pool": [], "notify": {}, "errors": [],
        "data_date": None, "selection": selection.as_dict() if selection else {},
        "pushed": False, "push_skipped": None,
    }

    strategies_off = selection is not None and selection.empty
    if strategies_off:
        # 分两种情况，都不该"顺手跑全量"：
        #   1. 用户**明确**关掉策略（enabled_groups=["none"]）→ 这是正常用法：只盯自选股；
        #   2. 组名/策略名全拼错 → 是配置错误，必须把原因说出来。
        # 两种情况下**都要继续建池**：池子成员除了策略标的还有自选股，
        # 策略为空不代表没东西可盯（需求："即使策略池为空也必须照常盯自选股"）。
        if getattr(selection, "explicit_off", False):
            logger.info("策略已关闭（enabled_groups=[\"none\"]）：本次只处理自选股")
            report["strategies_off"] = True
        else:
            msg = "没有启用任何策略：" + "；".join(selection.warnings)
            report["errors"].append(msg)
            logger.warning(msg)

    def _stage(name: str) -> None:
        if stage_cb:
            try:
                stage_cb(name)
            except Exception:  # noqa: BLE001 - 界面回调出错不该影响流程
                pass

    # 1) 数据增量（没配 Key 时跳过，用本地已有数据选股）
    if with_data:
        _stage("数据增量")
        try:
            report["sync"] = sync.daily_update(cfg, progress_cb=progress_cb)
        except Exception as exc:  # noqa: BLE001 - 数据更新失败仍然要能跑策略
            report["errors"].append(f"数据更新：{type(exc).__name__}: {exc}")
            logger.warning(f"数据更新失败：{exc}")

    # 2) 策略 + 建池
    try:
        from laoa_trader.strategy import rules

        picks_all: dict[str, list[dict]] = {}
        if not strategies_off:
            _stage("跑策略")
            # 策略只跑**一遍**：候选放宽到 200 既能满足建池（叠加热门行业过滤后还要剩够 10 只），
            # 又能覆盖信号落库（取每条策略前 30 条，与服务器版口径一致）。
            # 跑两遍的话每条策略都要重扫全市场面板，纯浪费一倍时间。
            picks_all, errors = rules.run_all(engine, cfg, top_n=200, selection=selection)
            report["errors"].extend(errors)
            report["picks"] = sum(len(v) for v in picks_all.values())
            report["signals"] = rules.save_signals(
                engine, {k: v[:SIGNAL_TOP_N] for k, v in picks_all.items()}
            )
        _stage("建池")
        report["pool"] = pool.build_pool(
            engine, cfg, picks=picks_all, selection=selection, report=report
        )
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"选股建池：{type(exc).__name__}: {exc}")
        logger.exception("选股建池失败")

    report["data_date"] = engine.get_latest_data_date()
    pool_rows = report["pool"]
    if not pool_rows:
        logger.info("今日无股票池（非交易日 / 数据不足 / 所选策略组无候选 / 没有自选股），跳过推送")
        return report

    # 3) 推送（多频道并行；同一天同一批内容只推一次）
    if notify:
        from laoa_trader.data import storage
        from laoa_trader.notify import notify_all, summarize

        title = f"📈 老A法师-选股池 | {report['data_date']}"
        lines = pool.format_pool_lines(pool_rows)
        lines.extend(_pool_plan_lines(pool_rows, cfg))
        day = report["data_date"] or datetime.now().strftime("%Y-%m-%d")
        fingerprint = pool_fingerprint(title, lines)
        try:
            with storage.connect(cfg.db_path) as conn:
                first_time = storage.mark_pushed(conn, day, "pool", fingerprint)
        except Exception as exc:  # noqa: BLE001 - 去重表出错不该拦住通知
            logger.warning(f"推送去重检查失败（继续推送）：{exc}")
            first_time = True

        if not first_time:
            report["push_skipped"] = f"{day} 已推送过相同内容的池子（指纹 {fingerprint}）"
            logger.info("跳过推送：" + report["push_skipped"])
            return report

        _stage("推送通知")
        try:
            report["notify"] = notify_all(title, lines, cfg=cfg)
            report["pushed"] = True
            logger.info(f"池子推送：{summarize(report['notify'])}")
        except Exception as exc:  # noqa: BLE001 - 推送失败不影响落库
            report["errors"].append(f"通知：{type(exc).__name__}: {exc}")
    return report


def refresh_data(
    cfg: Config | None = None,
    engine: DataEngine | None = None,
    *,
    progress_cb: sync.ProgressCb | None = None,
) -> list[sync.SyncResult]:
    """"只刷新数据"：跑一遍增量同步（行情 + 涨停池 + 日历 + 行业 + 指数），不选股、不推送。

    界面上的【只刷新数据】按钮用它 —— 数据没好之前跑策略毫无意义，
    所以单独给一个"只补数据"的动作。

    Returns:
        各项 SyncResult 列表（调用方据此显示成功/失败原因）。
    """
    cfg = cfg or get_config()
    try:
        return sync.daily_update(cfg, progress_cb=progress_cb)
    except Exception as exc:  # noqa: BLE001 - 界面层不该看到异常
        logger.exception("数据刷新失败")
        return [sync.SyncResult(stage="数据刷新", ok=False,
                                error=f"{type(exc).__name__}: {exc}")]


def _pool_plan_lines(pool_rows: list[dict], cfg: Config) -> list[str]:
    """给池子里的标的一并附上条件单参数（L2：提醒 + 参数，不含下单）。"""
    from laoa_trader import intraday as intraday_mod

    lines = ["", "**条件单参数（触发价 → 委托价 → 止损/止盈）**"]
    engine_price: dict[str, float] = {}
    try:
        with DataEngine(cfg.db_path).connect() as conn:
            rows = conn.execute(
                "SELECT symbol, close FROM stock_daily_hfq WHERE date = "
                "(SELECT MAX(date) FROM stock_daily_hfq)"
            ).fetchall()
            engine_price = {r[0]: r[1] for r in rows if r[1]}
    except Exception:  # noqa: BLE001 - 缺价格就跳过参数段
        return []

    for row in pool_rows[:10]:
        price = engine_price.get(row["symbol"])
        if not price:
            continue
        plan = intraday_mod.plan_buy(row["symbol"], row.get("name") or "", price, cfg=cfg)
        lines.append(
            f"{row['symbol']} {row.get('name') or ''}：触发 {plan['trigger']:.2f}｜"
            f"委托 {plan['limit_price']:.2f} × {plan['quantity']} 股｜"
            f"止损 {plan['stop_loss']:.2f}｜止盈 {plan['take_profit']:.2f}"
        )
    return lines if len(lines) > 2 else []


class Scheduler:
    """后台调度器（GUI 与 CLI `--serve` 共用）。"""

    def __init__(self, cfg: Config | None = None, engine: DataEngine | None = None) -> None:
        self.cfg = cfg or get_config()
        self.engine = engine or DataEngine(self.cfg.db_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: 暂停盘中提醒（界面按钮 / 用户临时不想被打扰）
        self._intraday_paused = False
        #: 今天已经**成功**跑过（成功标记）—— 有了它就不再重复跑、补跑也不会触发
        self._last_daily_date: str | None = None
        #: 今天主跑失败过（到了补跑时间会再试一次）
        self._daily_failed_date: str | None = None
        self._last_intraday_ts: float = 0.0
        self._last_error: str = ""
        self._last_daily_report: dict = {}
        self._last_intraday_report: dict = {}
        #: 最近一次"为什么没自动跑"的中文原因（状态栏直接显示，None = 没被跳过）
        self._skipped_reason: str | None = None
        #: 最近一次数据自检的结论（ready / needs_incremental / needs_full）
        self._preflight_status: str | None = None
        #: 今天因为"数据没就绪/正在下载"被跳过的日期。
        #: 它**不是** `_daily_failed_date`：那个会让调度一直等到补跑点（例如 19:15）才重试，
        #: 而"数据下好了就该马上补跑"。所以单独记一份，只用于状态栏说明"今天还没跑"。
        self._skipped_date: str | None = None
        #: 闸门日志节流用（同一个原因 BLOCKED_LOG_INTERVAL 秒内只记一次）
        self._blocked_log_key: tuple[str, str] | None = None
        self._blocked_log_at: float = 0.0

    # ── 生命周期 ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """启动后台线程（重复调用是幂等的）。"""
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="laoa-scheduler", daemon=True)
        self._thread.start()
        hour, minute = parse_run_at(self.cfg.run_at)
        fallback = parse_hhmm(getattr(self.cfg, "run_at_fallback", "") or "")
        state = "开" if getattr(self.cfg, "auto_run", True) else "关（只手动跑）"
        logger.info(
            f"调度器已启动：自动运行【{state}】；主跑 {hour:02d}:{minute:02d}"
            + (f"，补跑 {fallback[0]:02d}:{fallback[1]:02d}" if fallback else "（未设补跑）")
            + f"；交易时段每 {self.cfg.intraday_interval}s 跑盘中提醒"
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """停止后台线程（幂等）。"""
        if not self.running:
            return False
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("调度器已停止")
        return True

    # ── 状态 ──

    def status(self, now: datetime | None = None) -> dict:
        """运行状态（界面状态栏与 CLI 输出用）。"""
        now = now or datetime.now()
        hour, minute = parse_run_at(self.cfg.run_at)
        fallback = parse_hhmm(getattr(self.cfg, "run_at_fallback", "") or "")
        today = now.strftime("%Y-%m-%d")
        return {
            "running": self.running,
            "auto_run": bool(getattr(self.cfg, "auto_run", True)),
            "run_at": f"{hour:02d}:{minute:02d}",
            "run_at_fallback": (
                f"{fallback[0]:02d}:{fallback[1]:02d}" if fallback else ""
            ),
            "next_run": next_run_info(
                self.cfg, self.engine.db_path, now=now,
                done_today=self._last_daily_date == today,
            ),
            "daily_failed_today": self._daily_failed_date == today,
            "last_daily_date": self._last_daily_date,
            "last_intraday_at": (
                datetime.fromtimestamp(self._last_intraday_ts).strftime("%H:%M:%S")
                if self._last_intraday_ts else None
            ),
            "intraday_paused": self._intraday_paused,
            "in_session": intraday.in_session(now),
            "trading_day": intraday.is_trading_day(self.engine.db_path),
            "last_error": self._last_error,
            "last_daily": self._last_daily_report,
            "last_intraday": self._last_intraday_report,
            # 数据闸门：界面状态栏据此显示"为什么今天没自动跑"（用户不必翻日志）
            "skipped_reason": self._skipped_reason,
            "preflight_status": self._preflight_status,
            "daily_skipped_today": self._skipped_date == today,
            "downloading": state.is_downloading(),
        }

    def pause_intraday(self) -> None:
        """暂停盘中提醒（日更照跑）。"""
        self._intraday_paused = True
        logger.info("盘中提醒已暂停")

    def resume_intraday(self) -> None:
        self._intraday_paused = False
        logger.info("盘中提醒已恢复")

    @property
    def intraday_paused(self) -> bool:
        return self._intraday_paused

    # ── 手动触发（界面按钮）──

    def run_daily_now(
        self,
        notify: bool = True,
        *,
        with_data: bool = True,
        selection: Any = None,
        progress_cb: sync.ProgressCb | None = None,
        stage_cb: Any = None,
    ) -> dict:
        """立刻跑一次（不受时间限制）：界面【立即选股并建池】与 CLI --once 用。"""
        report = run_daily(
            self.cfg, self.engine, notify=notify, with_data=with_data,
            selection=selection, progress_cb=progress_cb, stage_cb=stage_cb,
        )
        self.mark_daily_ran(report)
        return report

    def mark_daily_ran(self, report: dict | None = None) -> None:
        """记下"今天已经**成功**跑过"（成功标记）。

        手动跑与定时跑共用 `run_daily`，两边结果本来就幂等；这个标记的作用是：
        - 定时点到了不用再跑一遍（纯省时间）；
        - 有了成功标记就**不会触发补跑**（补跑只在主跑没成功时发生）。

        手动跑若失败（report 里有 errors），不写成功标记 —— 那样到补跑时间
        定时任务还会替你试一次，符合"主跑没成功就补跑"的语义。
        """
        if report is not None and not self._report_succeeded(report):
            self._daily_failed_date = datetime.now().strftime("%Y-%m-%d")
            self._last_daily_date = None
            logger.info("手动跑未成功：不写成功标记（定时补跑仍会尝试）")
            return
        self._last_daily_date = datetime.now().strftime("%Y-%m-%d")
        self._daily_failed_date = None
        if report is not None:
            self._last_daily_report = {
                "pool": len(report.get("pool") or []),
                "picks": report.get("picks", 0),
                "pushed": bool(report.get("pushed")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    def run_intraday_now(self, *, dry_run: bool = False, ignore_session: bool = True) -> dict:
        """立刻跑一轮盘中提醒（界面"立即检查"用）。"""
        report = intraday.run_once(
            self.engine, self.cfg, dry_run=dry_run, ignore_session=ignore_session
        )
        self._last_intraday_ts = time.time()
        self._last_intraday_report = report
        return report

    # ── 主循环 ──

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now()
                # 每轮都从 cfg 现读 run_at / auto_run —— 界面改完时间**下一轮就生效**，
                # 不需要重启（这是"分发后各人自定运行时间"能用的前提）
                self._maybe_daily(now)
                self._maybe_intraday(now)
            except Exception as exc:  # noqa: BLE001 - 后台线程绝不能静默死掉
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("调度循环异常（继续下一轮）")
            self._stop.wait(TICK)

    def _maybe_daily(self, now: datetime | None = None) -> None:
        """到点了就跑日更；主跑没成功时，到补跑时间再试一次。

        判定顺序（全部按**当前配置**现读，改完时间立即生效）：
            1. `auto_run=false` → 什么都不做（手动按钮照常可用）；
            2. 今天已经**成功**跑过（成功标记）→ 不重复跑；
            3. 非交易日 → 记一次就跳过（周末不重复试）；
            4. 未到主跑时间 → 等；
            5. 已过主跑、未到补跑：只有"今天主跑失败过"时才等到补跑点，否则就在主跑点跑；
            6. 已过补跑点：补跑（如果主跑失败过）或主跑（例如程序 20:00 才启动）；
            7. **数据闸门**（本轮新增，见 `data_gate`）：正在下载 → 跳过；
               数据 `needs_full` → 跳过。两种情况都**不写成功标记**，
               所以下载/补数据完成后，当天仍会正常补跑一次。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")

        if not getattr(self.cfg, "auto_run", True):
            return
        if self._last_daily_date == today:
            return
        if not intraday.is_trading_day(self.engine.db_path, today):
            self._last_daily_date = today      # 只记一次，避免每秒重试
            logger.info("今天不是交易日，跳过日更")
            return

        primary = parse_run_at(self.cfg.run_at)
        fallback_text = str(getattr(self.cfg, "run_at_fallback", "") or "").strip()
        fallback = parse_hhmm(fallback_text) if fallback_text else None
        now_hm = (now.hour, now.minute)

        if now_hm < primary:
            return
        failed_today = self._daily_failed_date == today
        is_fallback = failed_today and fallback is not None and now_hm >= fallback
        if failed_today and fallback is not None and now_hm < fallback:
            # 主跑失败、还没到补跑点：等着（补跑时间一到就会再试）
            return
        if failed_today and fallback is None:
            # 没配补跑时间：主跑失败就当今天没跑成，每隔一段时间重试一次没必要 ——
            # 直接放行（下一次循环还会进这里），但记日志便于排查
            logger.info("主跑失败且未配置补跑时间，将按下一轮循环重试")

        # ── 闸门一：正在下载就不选股（否则会在只写了一半的库上算策略）──
        if state.is_downloading():
            self._block_daily(
                "正在下载历史数据：已跳过本次自动选股；"
                "下载完成后当天仍会自动补跑一次（也可以点【立即选股并建池】）",
                now,
            )
            return

        # ── 闸门二：本地数据不可用就不选股 ──
        gate = data_gate(self.cfg, self.engine)
        self._preflight_status = gate["status"]
        if not gate["ok"]:
            self._block_daily(
                f"本地数据不可用（{gate['reason']}）：已跳过本次自动选股；"
                "请先下载历史数据（界面【下载/更新历史数据】或命令行 --download）",
                now,
            )
            return
        self._skipped_reason = None          # 这回能跑了 → 清掉"上次被跳过"的提示
        self._skipped_date = None
        if gate["status"] == "needs_incremental":
            # 数据可用、只是不新鲜：按现有配置走 —— 日更本身就是"先增量再选股"
            # （run_daily(with_data=True) 里第一步就是 daily_update）
            stale = gate["result"].get("stale_trading_days", 0)
            if getattr(self.cfg, "auto_download_on_start", True):
                logger.info(f"数据落后 {stale} 个交易日；先跑增量再选股（auto_download_on_start=true）")
            else:
                logger.info(
                    f"数据落后 {stale} 个交易日，但 auto_download_on_start=false："
                    "不额外补数据，仍按日更流程先增量再选股（结论基于最新可取到的数据）"
                )

        logger.info(
            ("到补跑时间，" if is_fallback else "到达定时时间，")
            + f"开始日更（主跑 {now.strftime('%H:%M')}）"
        )
        # 先占位：日更要跑几分钟，期间循环还会滴答；提前记下"今天跑过了"，
        # 保证当天只触发一次（即使 run_daily_now 抛异常也不会半小时后再跑一遍）
        report = self.run_daily_now()
        if self._report_succeeded(report):
            self._last_daily_date = today          # 成功标记
            self._daily_failed_date = None
        else:
            self._last_daily_date = None           # 没成功 → 允许补跑
            self._daily_failed_date = today
            logger.warning("本次日更未成功，将在补跑时间再试一次")

    def _block_daily(self, message: str, now: datetime) -> None:
        """记录"这一轮被闸门拦住了"：写一条中文日志（**节流**），**不写成功标记**。

        两个关键点：

        1. **不写"今天已完成"**：拦住的不是"今天不用跑了"，而是"现在还不能跑"。
           数据下好之后当天仍然要补跑一次（用户预期："下完就该自动选一次"）。
        2. 也**不写 `_daily_failed_date`**（即"今天主跑失败"）：那个标记会让调度
           一直等到补跑点（例如 19:15）才重试；而这里要的是"数据一就绪就补跑"。
           所以另外记一份 `_skipped_date`，只用来在状态栏说明"今天因为数据没就绪还没跑"。
        """
        self._skipped_reason = message
        self._skipped_date = now.strftime("%Y-%m-%d")
        key = (self._skipped_date, message)
        last_key, last_at = self._blocked_log_key, self._blocked_log_at
        if key == last_key and (time.monotonic() - last_at) < BLOCKED_LOG_INTERVAL:
            return                                   # 同一原因 5 分钟内只记一次
        self._blocked_log_key, self._blocked_log_at = key, time.monotonic()
        logger.warning(message + "；数据就绪后当天仍会补跑")


    @staticmethod
    def _report_succeeded(report: dict | None) -> bool:
        """这次日更算不算"成功"（决定要不要补跑）。

        判据：没有 errors，且不是"配置写错导致什么都没跑"。
        池子为空不算失败（可能当天没有候选）。
        """
        if not report:
            return False
        if report.get("errors"):
            return False
        if report.get("strategies_off"):
            return True
        return True

    def _maybe_intraday(self, now: datetime) -> None:
        if self._intraday_paused:
            return
        if not intraday.in_session(now):
            return
        interval = max(int(self.cfg.intraday_interval), 5)
        if time.time() - self._last_intraday_ts < interval:
            return
        self._last_intraday_ts = time.time()
        report = intraday.run_once(self.engine, self.cfg)
        self._last_intraday_report = report
        if report.get("error"):
            self._last_error = str(report["error"])


def run_serve(
    cfg: Config | None = None,
    interval: int | None = None,
    *,
    once: bool = False,
    dry_run: bool = False,
) -> None:
    """CLI 常驻模式：阻塞运行直到 Ctrl+C（`python -m laoa_trader --serve`）。"""
    cfg = cfg or get_config()
    if interval:
        cfg.intraday_interval = interval
    scheduler = Scheduler(cfg)
    if once:
        print("这一次的日更结果：")
        report = run_daily(cfg, scheduler.engine, notify=not dry_run)
        print(f"  数据日期：{report['data_date']}")
        print(f"  候选信号：{report['picks']} 条")
        print(f"  股票池：{len(report['pool'])} 只")
        for line in pool.format_pool_lines(report["pool"]):
            print("   " + line)
        for err in report["errors"]:
            print(f"  ⚠️ {err}")
        return
    scheduler.start()
    print(f"调度器已启动（run_at={cfg.run_at}，盘中间隔 {cfg.intraday_interval}s）。Ctrl+C 退出。")
    try:
        while True:
            time.sleep(60)
            st = scheduler.status()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] 运行={st['running']} "
                f"交易时段={st['in_session']} 盘中暂停={st['intraday_paused']} "
                f"最近错误={st['last_error'] or '无'}"
            )
    except KeyboardInterrupt:
        scheduler.stop()
        print("已退出")
