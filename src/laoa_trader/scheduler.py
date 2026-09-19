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
from pathlib import Path
from typing import Any

from laoa_trader import intraday, pool, state
from laoa_trader.config import Config, get_config
from laoa_trader.data import sync
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader import hints
from laoa_trader.data import preflight
from laoa_trader.hints import LIGHT_ITEM_NAMES as DL_HINT_LIGHT_ITEMS
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
    now = now or intraday.now_shanghai()
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


#: 数据闸门拦住时给用户看的中文下一步指引（界面与 CLI 共用同一句话，见 `hints` 模块）。
#: 后面那句是**轻量项**的指路：缺交易日历/行业归属/指数时点【刷新数据】就够了，
#: 别去重下历史（用户实报过被指错路）
DOWNLOAD_HINT = f"{hints.DOWNLOAD_HINT}；缺{DL_HINT_LIGHT_ITEMS}用【{hints.BTN_REFRESH_TEXT}】"


def data_gate(
    cfg: Config | None = None,
    engine: DataEngine | None = None,
    *,
    today: str | None = None,
    auto_sync_light: bool = False,
    note_cb: Any = None,
) -> dict:
    """**跑策略前的数据闸门**：本地数据到底能不能用来选股？

    这是"在不完整的数据上跑策略"这个 bug 的正解：策略、建池、通知都必须过这道门。
    判据直接复用 `preflight.check()`（纯本地、秒级、一次请求都不发）：

    - `ready` → 放行；
    - `needs_incremental` → **也放行**（数据可用，只是落后几天；`run_daily` 会先跑
      增量再选股，所以口径仍然是"先补数据再选股"）；
    - `needs_full`（空库/跨度不足/缺复权事件/落后超窗口…）→ **拦下**，给出原因与下一步；
    - **只缺轻量项**（交易日历/行业归属/指数）→ 默认也拦下（但指路指向【刷新数据】，
      文案里不再是"重新下载"）；`auto_sync_light=True` 时**先自动补一次再复查**，
      补上了就放行 —— 别让用户为了一个行业归属去重下 10 年数据。

    Args:
        cfg: 配置（阈值来源）。
        engine: 数据引擎（不传就用 `cfg.db_path`）。
        today: 覆盖"今天"（测试用）。
        auto_sync_light: 只缺轻量项时自动同步（**会发约 90 个请求**，所以默认关；
            调度器的定时/自动跑传 True，界面上的手动按钮按需自己点【刷新数据】）。
        note_cb: 自动同步时的状态回调（"正在同步行业归属…"）。

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

    if preflight.needs_download(result) == preflight.DOWNLOAD_SYNC_LIGHT:
        if auto_sync_light:
            # 先补轻量项再复查：补上了就当 ready 放行（选股不需要"行业归属完好"之外的东西）
            logger.info(f"只缺轻量数据，先自动同步再复查：{reason}")
            try:
                sync.sync_light(cfg, note_cb=note_cb)
            except Exception as exc:  # noqa: BLE001 - 补不上就按原来的原因拦下
                logger.warning(f"轻量数据自动同步失败：{type(exc).__name__}: {exc}")
            retry = preflight.check(db_path, cfg, today=today)
            if retry.get("status") == preflight.READY:
                return {"ok": True, "status": retry["status"],
                        "reason": retry.get("reason") or "",
                        "message": preflight.summary_line(retry), "result": retry}
            result, reason = retry, retry.get("reason") or reason
        return {
            "ok": False,
            "status": preflight.NEEDS_FULL,
            "reason": reason,
            "message": f"本地数据缺轻量项（{reason}）",
            "result": result,
        }

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
    export_dir: Any = None,
) -> dict:
    """跑一次完整的日更流程：数据增量（可选）→ 策略 → 建池 → 导出桌面文件 → 推送。

    **界面上的【开始选股】（「策略选股」页，或托盘菜单里的同名项）与 CLI `--once`
    共用这一个函数** —— 按钮名取自 `hints.BTN_RUN_TEXT`，别处不许自己写一个
    （改版后那个按钮叫【开始选股】，老文档里的【选股建池】已经不存在了）。
    口径一致才谈得上幂等：信号按 (行情日, 策略, 代码) upsert、池子按
    (行情日, 代码) upsert、推送按内容指纹去重（同一天同一批内容只推一次）。

    Args:
        cfg: 配置。
        engine: DataEngine。
        notify: 是否推送（False = 只入库；CLI/界面的"不推送"试跑用）。
        progress_cb: 数据同步进度回调 `(stage, done, total)`。
        selection: **2026-09-18 起不再用于选股，只为兼容老调用方保留这个参数**。
            内置策略已整体改成随包公式（可改可删），"跑哪些策略"由 `config.toml` 的
            `enabled_formulas` 决定（建池里读它），外面传什么都不影响结果。
        with_data: 是否先跑一次数据增量（只想重算选股时传 False）。
        stage_cb: 阶段名回调（界面状态栏显示"跑策略/建池/推送通知"；
            导出成功时还会收到一句 `结果已导出到 <路径>` —— 用户要求写成桌面文件就
            在状态栏里说一声）。
        export_dir: 桌面文件的落点。None（默认）= 自己找桌面
            （`pool.desktop_dir()`：系统桌面 → `Desktop`/`桌面`/OneDrive 桌面 → 数据目录兜底）。
            **测试与特殊部署注入它** —— 传了就不会往真人桌面上写文件。

    Returns:
        {"sync": [...], "picks": n, "signals": n, "pool": [...], "notify": {...},
         "errors": [...], "data_date":…, "selection": {}(恒为空，只为兼容),
         "pushed": bool, "push_skipped": str|None,
         "export_path": str|None（导出成功时是文件路径）}
    """
    cfg = cfg or get_config()
    engine = engine or DataEngine(cfg.db_path)

    # ⚠️ 2026-09-18（用户要求）：**不再解析 `enabled_groups` / `enabled_strategies`**。
    # 「内置策略」（写在 `strategy/rules.py` 里的 5 条 Python 策略）整体改成了**随包公式**
    # （可改可删），所以"跑哪些策略"这件事现在只有一个答案：`config.toml` 里
    # `enabled_formulas` 勾了哪几条公式 —— 建池里读它（`formula_group`），
    # 这里不需要也不该再替它做一次选择。
    #
    # `selection` 参数**只为兼容老调用方保留**（CLI/测试还在传），不再参与选股。
    report: dict[str, Any] = {
        "sync": [], "picks": 0, "signals": 0, "pool": [], "notify": {}, "errors": [],
        "data_date": None, "selection": {},
        "pushed": False, "push_skipped": None,
        # 桌面导出：成功时是文件路径，失败/跳过时是 None（失败原因进 errors，不静默）
        "export_path": None,
        # 推送过滤（`push_only_proven`）：被跳过的标的与中文原因 —— 状态栏/日志/报告都从这里取
        "push_skipped_rows": [], "push_note": None, "push_skipped_kind": None,
    }

    # 一条公式都没勾 = **只盯自选股**。这是**默认状态、不是错误**（公式默认一条都不勾），
    # 所以不写 `errors`，只置 `strategies_off` 让调度器/界面知道"这轮没有公式标的"。
    # 池子照常建：成员还有自选股 —— "即使策略池为空也必须照常盯自选股"这条口径没变。
    enabled_formulas = [str(n) for n in (getattr(cfg, "enabled_formulas", None) or [])]
    strategies_off = not enabled_formulas
    if strategies_off:
        logger.info("没有勾选任何公式：本次只处理自选股"
                    "（在「策略选股」页勾上公式即可参与选股）")
        report["strategies_off"] = True

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

    # 2) 建池（候选由 `build_pool()` 里的「公式」组现算 —— 这是唯一的候选来源）
    try:
        _stage("建池")
        report["pool"] = pool.build_pool(engine, cfg, report=report)

        # 信号落库（`signal` 表，供盘中风控观察池）：用**这一轮真正跑的公式**的候选。
        # 候选行由建池写进 `report["formulas"]["rows"]`（它在建池里已经算过一遍，
        # 这里只是取出来截断入库，不重跑）。
        # 一条公式都没勾时这里是空的 —— 与"没有候选"是同一件事，不必特殊处理。
        formula_rows = (report.get("formulas") or {}).get("rows") or {}
        picks_all = {key: list(rows) for key, rows in formula_rows.items()}
        report["picks"] = sum(len(v) for v in picks_all.values())
        if picks_all:
            # `save_signals()` 只是"把候选写进 signal 表"的写库函数（与策略实现无关），
            # 所以继续借它用；`rules` 这个模块本身已经不参与选股了。
            from laoa_trader.strategy import rules as rules_mod

            report["signals"] = rules_mod.save_signals(
                engine, {k: v[:SIGNAL_TOP_N] for k, v in picks_all.items()}
            )
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"选股：{type(exc).__name__}: {exc}")
        logger.exception("选股失败")

    report["data_date"] = engine.get_latest_data_date()
    pool_rows = report["pool"]
    if not pool_rows:
        logger.info("今日无股票池（非交易日 / 数据不足 / 所选策略组无候选 / 没有自选股），跳过推送")
        return report

    # 2.5) 桌面导出：用户要求「选股结果直接进自选股池，**也可以同时** output 一个文件到桌面」。
    # 为什么挂在**建池成功之后**、推送之前：
    #   * 池子已经落库（`build_pool(save=True)`），导出的就是用户马上要在界面上看到的那一份；
    #   * 推送那一段有好几个提前 return（内容没变不重复推、全是"依赖开盘"的标的就整批不推），
    #     导出要是挂在推送之后，这些情况下桌面就**没有文件**——而用户要的是"选完股就有"。
    _stage("导出结果")
    try:
        exported = pool.export_pick_file(
            pool_rows,
            data_date=report["data_date"],
            dest_dir=export_dir,
            db_path=cfg.db_path,
            # 桌面找不到时退回**数据目录**（用户找得到的地方），而不是悄悄不导出
            fallback_dir=Path(cfg.db_path).parent,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：导出绝不能把选股/推送一起带走
        # 内层 `export_pick_file` 自己已经兜过一层，这里是"万一它被换掉/被 monkeypatch
        # 成会抛的实现"时的最后一道 —— 测试用 monkeypatch 抛异常正是打这一条。
        report["errors"].append(f"导出桌面文件：{type(exc).__name__}: {exc}")
        logger.warning(f"导出选股结果失败（不影响选股与推送）：{exc}")
    else:
        if exported is not None:
            report["export_path"] = str(exported)
            # 状态栏里也说一句（用户要求："写出去了就在状态栏/日志里说一句
            # 结果已导出到 <路径>"）。`stage_cb` 是这一层唯一能碰到状态栏的口子
            # （主窗口把它接到 `_set_status`），所以这里把整句话当"阶段"发出去 ——
            # 紧接着的"推送通知"阶段可能把它顶掉，所以路径同时留在
            # `report["export_path"]` 里，调用方想再显示一次随时能取。
            _stage(f"结果已导出到 {exported}")
        else:
            # 进 errors：界面状态栏会把第一条错误说出来。用户被承诺过"桌面上会有个文件"，
            # 没写出来就必须让他知道，而不是只写进他自己不会去看的日志。
            report["errors"].append("导出桌面文件：没有写成功（原因见日志）")

    # 3) 推送（多频道并行；同一天同一批内容只推一次）
    if notify:
        from laoa_trader.data import storage
        from laoa_trader.notify import notify_all, summarize

        # 推送范围：**默认全推**；只有用户主动打开 `push_only_proven` 才收窄成
        # "只推有边际的策略标的"。那时 `open_only` 那两条策略（地量后放量变盘 /
        # 首板缩量整理：正 α 只在"开盘买"口径下存在，尾盘买就转负）的标的会被跳过 ——
        # 但它们**照常进池、照常显示在表格/卡片上**，而且跳过的原因会写进推送/日志/状态栏。
        push_rows, skipped_rows = pool.split_push_rows(pool_rows, cfg)
        report["push_skipped_rows"] = [
            {"symbol": r.get("symbol"), "name": r.get("name"),
             "strategies": r.get("strategies") or r.get("strategy") or ""}
            for r in skipped_rows
        ]
        if skipped_rows:
            report["push_note"] = pool.skipped_push_note(skipped_rows)
            logger.info("推送过滤：" + report["push_note"])
        if not push_rows:
            # 今天命中的全是"依赖开盘"的标的 → **不推**，但要把原因说清楚
            report["push_skipped"] = report["push_note"] or "今天没有可推送的标的"
            report["push_skipped_kind"] = "filtered"
            logger.info("跳过推送：" + report["push_skipped"])
            return report

        title = f"📈 老A选股助手-选股池 | {report['data_date']}"
        # 「公式」组：用户自己的公式选出来的票，**标题里点一下名** ——
        # 收到推送的人一眼就知道"这批票里有我自己写的公式"，不用翻到正文才看出来
        # （正文每一行的标签也写着 `公式·名字`）。名字多时只列前两条：
        # 标题不该比正文还长。
        picked_formulas = list((report.get("formulas") or {}).get("picks") or {})
        if picked_formulas:
            # 懒加载：`groups` 只在"标题里给公式点名"这一处用得到（把合成名
            # `公式·放量上攻` 还原成用户起的名字），没必要为它拖慢模块导入。
            from laoa_trader.strategy import groups as groups_mod

            names = "、".join(
                groups_mod.formula_name_of(key) for key in picked_formulas[:2]
            )
            more = f" 等 {len(picked_formulas)} 条" if len(picked_formulas) > 2 else ""
            title += f"｜公式：{names}{more}"
        lines = pool.format_pool_lines(push_rows)
        lines.extend(_pool_plan_lines(push_rows, cfg))
        if skipped_rows:
            # 正文里也留一句：用户收到推送时就知道"今天还有几只没推、为什么"
            lines.append(report["push_note"])
        day = report["data_date"] or intraday.now_shanghai().strftime("%Y-%m-%d")
        fingerprint = pool_fingerprint(title, lines)
        try:
            with storage.connect(cfg.db_path) as conn:
                first_time = storage.mark_pushed(conn, day, "pool", fingerprint)
        except Exception as exc:  # noqa: BLE001 - 去重表出错不该拦住通知
            logger.warning(f"推送去重检查失败（继续推送）：{exc}")
            first_time = True

        if not first_time:
            report["push_skipped"] = f"{day} 已推送过相同内容的池子（指纹 {fingerprint}）"
            report["push_skipped_kind"] = "duplicate"
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
    """只刷新数据：跑一遍增量同步（行情 + 涨停池 + 日历 + 行业 + 指数），不选股、不推送。

    界面上的【刷新数据】按钮用它（`hints.BTN_REFRESH_TEXT`）—— 数据没好之前跑策略毫无意义，
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
        now = now or intraday.now_shanghai()
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
        """立刻跑一次（不受时间限制）：界面【开始选股】与 CLI `--once` 用。

        `selection` 与 `run_daily()` 一样**只为兼容老调用方保留**（2026-09-18 起
        不再参与选股：候选只来自 `config.toml` 里 `enabled_formulas` 勾的公式）。
        """
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
            self._daily_failed_date = intraday.now_shanghai().strftime("%Y-%m-%d")
            self._last_daily_date = None
            logger.info("手动跑未成功：不写成功标记（定时补跑仍会尝试）")
            return
        self._last_daily_date = intraday.now_shanghai().strftime("%Y-%m-%d")
        self._daily_failed_date = None
        if report is not None:
            self._last_daily_report = {
                "pool": len(report.get("pool") or []),
                "picks": report.get("picks", 0),
                "pushed": bool(report.get("pushed")),
                "at": intraday.now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
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
                now = intraday.now_shanghai()
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
        now = now or intraday.now_shanghai()
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
                "下载完成后当天仍会自动补跑一次（也可以点【开始选股】）",
                now,
            )
            return

        # ── 闸门二：本地数据不可用就不选股 ──
        # auto_sync_light=True：**只缺轻量项**（交易日历/行业归属/指数）时先自动补一次再复查。
        # 定时任务跑在后台，不该因为"行业归属没同步"就跳过今天，更不该等用户去点按钮
        # （补的是目录类数据：约 90 个请求、幂等、不动行情）。
        gate = data_gate(self.cfg, self.engine, auto_sync_light=True)
        self._preflight_status = gate["status"]
        if not gate["ok"]:
            # 默认给"下载历史数据 + 轻量项用【刷新数据】"这一整句（见模块顶部的 DOWNLOAD_HINT）
            hint = DOWNLOAD_HINT
            if preflight.needs_download(gate.get("result") or {}) == preflight.DOWNLOAD_SYNC_LIGHT:
                hint = hints.SYNC_LIGHT_HINT        # 缺轻量项：指路【刷新数据】，不是重下历史
            self._block_daily(
                f"本地数据不可用（{gate['reason']}）：已跳过本次自动选股；{hint}",
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
        """交易时段内按间隔跑盘中提醒；**竞价扫描时刻即使还没开盘也要跑一轮**。

        为什么把"竞价扫描到点"单独放行：9:20 / 9:25 都还没到 09:30（不在交易时段里），
        只按 `in_session` 判断的话**竞价扫描永远不会触发** —— 而它恰恰是开盘前最有用的那一步。
        代价是每分钟只多一次"到没到点"的本地判断（查一次 `auction_scan` 表），零请求。
        """
        if self._intraday_paused:
            return
        scan_due = False
        if bool(getattr(self.cfg, "intraday_auction", False)):
            try:
                with storage.connect(self.engine.db_path) as conn:
                    scanned = storage.auction_scan_slots(
                        conn, intraday.now_shanghai(now).strftime("%Y-%m-%d")
                    )
                scan_due = bool(intraday.auction_scan_due(now, self.cfg, scanned))
            except Exception as exc:  # noqa: BLE001 - 到点判断失败就当没到点
                logger.debug(f"竞价扫描到点判断失败：{exc}")
        if not intraday.in_session(now) and not scan_due:
            return
        interval = max(int(self.cfg.intraday_interval), 5)
        # 到点的竞价扫描**不受轮询间隔限制**：09:20/09:25 是硬时刻，
        # 而"每分钟一拍 + 宽限 3 分钟"两条叠加起来有可能正好错过（例如上一拍在 09:22:30、
        # 下一拍就到 09:23:30，那一档已经作废）—— 宁可这一刻多跑一轮常规提醒，也别漏扫。
        if time.time() - self._last_intraday_ts < interval and not scan_due:
            return
        self._last_intraday_ts = time.time()
        report = intraday.run_once(self.engine, self.cfg, now=now)
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
