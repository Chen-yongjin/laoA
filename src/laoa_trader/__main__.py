"""老牛选股助手（Windows 单机版）。

入口：`python -m laoa_trader`（无参数启动 GUI），或 `--cli` 走命令行：

    python -m laoa_trader --cli --download     # 下载 10 年历史数据（含进度）
    python -m laoa_trader --cli --once         # 跑一次：数据增量 + 选股 + 建池
    python -m laoa_trader --cli --pool         # 只看当前股票池
    python -m laoa_trader --cli --market       # 打印大盘概览那几行
    python -m laoa_trader --cli --market       # 只看大盘概览（与页面同一份：家数/成交额/涨跌家数/三组指数）
    python -m laoa_trader --cli --serve        # 常驻：定时日更 + 盘中提醒

界面不可用（例如没装 PySide6）时**自动降级为 CLI**，不会直接崩。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from laoa_trader import pool as pool_mod
from laoa_trader import runtime
from laoa_trader.config import get_config, load_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import get_logger, setup_logging
from laoa_trader.scheduler import next_run_info, validate_run_times


def _print_result(result: sync.SyncResult) -> None:
    mark = "✅" if result.ok else "❌"
    print(f"{mark} {result.message}")


def _mask(secret: str) -> str:
    """脱敏显示凭证（排障时要看"配没配"，但不能把 Key 打到屏幕上/日志里）。"""
    if not secret:
        return "（未配置）"
    return f"{secret[:4]}…{secret[-2:]}（长度 {len(secret)}）" if len(secret) > 8 else "（已配置）"


class _Tee:
    """把 stdout 同时写到"控制台 + 文件"（自检报告落盘用）。

    为什么要它：Windows 的**桌面程序没有控制台**（`--noconsole` / Nuitka 的
    `--windows-console-mode=disable`），用户双击 exe 时看不到任何输出。
    而自检报告正是"报障时要给我的那段文字" —— 所以顺手写进日志目录一份，
    让用户直接发那个文件（CI 也是这样验证编译产物的：GUI 子系统的 exe，
    重定向 stdout 不一定拿得到内容，读文件最稳）。
    """

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            try:
                stream.write(text)
            except Exception:  # noqa: BLE001 - 其中一个流坏了不该影响另一个
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass


def _doctor(cfg, startup_problem: str = "") -> None:
    """自检报告：路径、配置来源、依赖、凭据、数据概况。

    为什么要这个命令：桌面版最常见的求助是"点了没反应/数据是空的"，
    而单机程序看不到控制台。先跑一次 --doctor 就能分清是"没配 Key"、
    "没下数据"、"数据目录不可写"还是"缺依赖"。

    Args:
        startup_problem: 数据目录创建失败的原因（空串表示没问题）。
            **自检命令不该因为目录不可写就直接退出** —— 那正是它要报出来的东西。
    """
    import platform

    import laoa_trader

    print("老牛选股助手 —— 自检")
    print("=" * 56)
    print(f"程序版本    : {laoa_trader.__version__}")
    # 运行形态与两条关键路径：换成 Nuitka 之后，"图标/随包公式找没找到"是最容易出问题的地方
    # （源码运行永远绿、编译版可能整个目录都没进去），所以自检里必须直接打出来。
    for line in runtime.summary_lines():
        print(line)
    try:
        from laoa_trader import assets as assets_mod
        from laoa_trader import formulas as formulas_mod

        print(f"资源目录    : {assets_mod.assets_dir()}")
        bundled = formulas_mod.bundled_formula_dir()
        print(f"随包策略    : {bundled if bundled else '（没找到：随包策略一个都不会出现）'}")
    except Exception as exc:  # noqa: BLE001 - 自检本身不该因为这里失败
        print(f"资源检查    : ⚠️ {type(exc).__name__}: {exc}")
    print(f"Python      : {sys.version.split()[0]}（{platform.system()} {platform.release()}）")
    print(f"配置来源    : {cfg.source_path or '内置默认值（未找到 config.toml）'}")
    if cfg.config_warning():
        print(f"配置告警    : ⚠️ {cfg.config_warning()}")
    print(f"数据目录    : {cfg.data_dir}")
    print(f"数据库      : {cfg.db_path}"
          f"（{'已存在' if cfg.db_path.exists() else '尚未创建'}）")
    print(f"dump 目录   : {cfg.dump_dir}（{'已存在' if cfg.dump_dir.exists() else '尚未创建'}）")
    # Path(...) 包一层：`data_dir` 可能还是界面传进来的字符串（save_settings 之前）
    print(f"日志文件    : {Path(cfg.data_dir) / 'logs' / 'laoa-trader.log'}")
    print("-" * 56)
    print(f"同花顺 Key  : {_mask(cfg.hithink_api_key)}")
    # 数据来源现在是一张**可添加的列表**（`data_sources`，顺序即取数优先级）：
    # 内置的同花顺要 Key，东方财富那个公开接口免 Key。只打 id 容易对不上号，
    # 所以连注册表里的中文名与"要不要 Key"一起打（注册表读不出来也不能让体检失败）。
    try:
        from laoa_trader.data import sources as sources_mod

        described = [f"{info.name}（{'要 Key' if info.needs_key else '免 Key'}）"
                     for info in sources_mod.active_sources(cfg)]
        source_text = "、".join(described) or "（空）"
        unknown = [s for s in cfg.data_sources if s not in sources_mod.REGISTRY]
        if unknown:
            source_text += f"；未实现：{'、'.join(unknown)}"
    except Exception:  # noqa: BLE001 - 体检不能因为注册表读不出来就失败
        source_text = "、".join(cfg.data_sources) or "（空）"
    print(f"数据来源    : {source_text}")
    print(f"飞书凭证    : AppID {_mask(cfg.feishu_app_id)} / Secret {_mask(cfg.feishu_app_secret)}"
          f" / 会话 {cfg.feishu_chat_id or '（自动发现）'}")
    # 2026-09-18：`notify_windows` 随"Windows 系统通知"整路删除，这行不再提它
    print(f"通知开关    : 飞书 {cfg.notify_feishu}、托盘 {cfg.notify_tray}")
    # `notify_channels` 是"实际发哪几路"的**总闸**，默认空 = 三路都不发（只走自绘浮窗）。
    # 只打三个单频道开关会让人误以为"现在是发着的"（默认已经改成空列表了），所以两行都打。
    chosen = "、".join(cfg.channels) or (
        f"无（只走自绘浮窗 notify_popup={cfg.notify_popup}）")
    print(f"通知频道    : notify_channels = {cfg.notify_channels}；实际发送 {chosen}")
    print(f"交易参数    : 资金 {cfg.trade_capital:.0f} 元、单票 {cfg.trade_position_pct:.0%}、"
          f"最多 {cfg.trade_max_positions} 只、止损 {cfg.stop_loss:.0%}、止盈 {cfg.take_profit:.0%}")
    _run_info = next_run_info(cfg, cfg.db_path)
    print(f"定时        : 每天 {cfg.run_at}（主跑）；补跑 {cfg.run_at_fallback or '未设置'}"
          f"；自动运行 {'开' if cfg.auto_run else '**关闭**'}；"
          f"盘中间隔 {cfg.intraday_interval}s")
    print(f"下次自动运行: {_run_info['label']}"
          + (f"（{_run_info['at']}）" if _run_info.get("at") else ""))
    print("-" * 56)

    # 依赖：缺哪个都会在具体功能上炸，提前说清楚
    for module, purpose in (
        ("pandas", "行情计算"), ("numpy", "行情计算"), ("pyarrow", "读同花顺 Parquet"),
        ("requests", "调用接口"), ("PySide6", "图形界面"),
        ("winotify", "Windows 原生通知（仅 Windows）"),
    ):
        try:
            __import__(module)
            mark = "✅"
        except Exception:  # noqa: BLE001
            mark = "❌"
        print(f"{mark} {module:<10} {purpose}")

    if startup_problem:
        print("⚠️ 数据目录不可用：")
        for line in startup_problem.splitlines():
            print(f"   {line}")
    print("-" * 56)
    if startup_problem:
        print("数据概况    : 跳过（数据目录不可用，先按上面的提示改 data_dir）")
        return

    # ── 运行时自检结论（三态）──
    from laoa_trader.data import preflight

    result = preflight.check(cfg.db_path, cfg)
    marks = {preflight.READY: "✅ ready", preflight.NEEDS_INCREMENTAL: "🟡 needs_incremental",
             preflight.NEEDS_FULL: "❌ needs_full"}
    print(f"数据自检    : {marks.get(result['status'], result['status'])} —— {result['reason']}")
    print(f"  行情行数  : {result['rows']:,}；股票数 {result['symbols']}"
          f"（阈值 {cfg.min_symbols}）")
    print(f"  历史跨度  : {result['span_years']:g} 年（阈值 {cfg.min_history_years:g}）"
          f"；最新 {result['latest_date']}")
    imported = preflight.imported_range(cfg.db_path)
    print(f"  已导入区间: {imported['start'] or '—'} → {imported['end'] or '—'}"
          f"；{imported['rows']:,} 行；覆盖 {imported['span_years']:g} 年")
    print(f"  history_years: {cfg.history_years:g}（导入年限，dump 固定 10 年、"
          f"按这个值过滤；改成 10 可导满 10 年）")
    print(f"  落后交易日: {result['stale_trading_days']}"
          f"（ready 允许 ≤{cfg.max_stale_trading_days}）")
    print(f"  复权事件  : {'有' if result['has_adjust_events'] else '**缺失**'}"
          f"；行业覆盖 {result['industry_coverage']:.0%}"
          f"；交易日历 {'有' if result['has_calendar'] else '**缺失**'}")
    print(f"  需要下载  : {result['needs_download']}"
          f"（auto_download_on_start={cfg.auto_download_on_start}）")
    if result["missing_tables"]:
        print(f"  缺表      : {'、'.join(result['missing_tables'])}")
    print("-" * 56)
    try:
        engine = DataEngine(cfg.db_path)
        summary = engine.summary()
        print(f"行情行数    : {summary['daily_rows']:,}（{summary['symbols']} 只股票）")
        print(f"最新数据日期: {summary['latest_date'] or '无（还没下载过历史数据）'}")
        print(f"行业 / 日历 : {summary['industries']} 个行业、{summary['calendar']} 个交易日")
        print(f"涨停池天数  : {summary['limit_up_days']}")
        print(f"股票池行数  : {summary['pool']}；当前持仓 {summary['positions']} 只")
        coverage = engine.data_coverage()
        print(f"最新日覆盖率: {coverage['rate']:.1%}"
              f"（{coverage['symbols']}/{coverage['universe']}）")
        if not summary["daily_rows"]:
            print("\n提示：先跑 `python -m laoa_trader --cli --download` 建库。")
        elif not cfg.hithink_api_key:
            print("\n提示：未配置同花顺 API Key，只能基于本地已有数据选股，无法更新。")
    except Exception as exc:  # noqa: BLE001 - 自检本身不能崩
        print(f"数据概况读取失败：{type(exc).__name__}: {exc}")


def _split_list(text: str | None) -> list[str]:
    """解析逗号分隔的 CLI 参数（中文逗号也认）。"""
    if not text:
        return []
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _preflight_gate(cfg, auto_download: bool) -> int | None:
    """跑自检并按需补数据。

    Returns:
        None 表示可以继续；整数表示该直接返回的退出码（**明确报错并退出**，
        绝不"静默跑出空结果"）。
    """
    from laoa_trader.data import preflight

    result = preflight.check(cfg.db_path, cfg)
    print(f"数据自检：{result['status']} —— {result['reason']}")
    if result["status"] == preflight.READY:
        return None
    if result["status"] == preflight.NEEDS_INCREMENTAL:
        if cfg.auto_download_on_start or auto_download:
            print(f"落后 {result['stale_trading_days']} 个交易日，先跑一次增量更新…")
            ok, after, results = preflight.ensure_ready(
                cfg, auto_download=True, progress_cb=_progress, note_cb=_note)
            print()
            for item in results:
                print(("✅ " if item.ok else "❌ ") + item.message)
            return None if ok else 1
        print(f"⚠️ {preflight.summary_line(result)}；"
              "如需现在补齐，请加 --auto-download（或把 auto_download_on_start 设为 true）。")
        return None

    # needs_full：没有 10 年库
    if not auto_download:
        print("")
        print(f"❌ 本地没有可用的历史数据：{result['reason']}")
        print("数据没下好之前**不会跑策略**（否则会选出错的票）。请先下载：")
        print("    python -m laoa_trader --cli --download        # 下载历史数据")
        print("或在 config.toml 里填好 hithink_api_key 后加 --auto-download 自动下载；")
        print("界面版点【下载数据】按钮同样可以（支持断点续传）。")
        return 1
    print("需要下载 10 年全量历史（约 10~20 分钟，有进度显示，可中断后重跑续传）…")
    ok, after, results = preflight.ensure_ready(
        cfg, auto_download=True, progress_cb=_progress, note_cb=_note)
    print()
    for item in results:
        print(("✅ " if item.ok else "❌ ") + item.message)
    if not ok or (results and not results[-1].ok):
        print("❌ 下载未完成，已停止（修好之后重跑即可续传）。")
        return 1
    print(f"✅ 下载完成：{preflight.summary_line(after)}")
    return None


def _watchlist_command(cfg, values: list[str], note: str) -> int:
    """自选股命令行：add / list / remove / enable / disable。

    名称**自动从本地库补**（`stock_basic`）；查不到也允许添加，只是提示一下 ——
    刚上市或有改名时库里可能还没有，不该因此拦住用户。
    """
    from laoa_trader.data import storage
    from laoa_trader.data.engine import DataEngine

    action = (values[0] or "").strip().lower()
    symbol = (values[1].strip() if len(values) > 1 else "")
    engine = DataEngine(cfg.db_path)
    known = ("add", "list", "remove", "del", "delete", "enable", "disable")
    if action not in known:
        print(f"未知动作：{action or '（空）'}（可用：add / list / remove / enable / disable）")
        return 1

    if action == "list":
        with storage.connect(cfg.db_path) as conn:
            rows = storage.load_watchlist(conn)
        if not rows:
            print("自选股为空。用 `--watchlist add 600519 --note 龙头` 添加。")
            return 0
        pool_symbols = set(pool_mod.pool_symbols(cfg.db_path))
        print(f"自选股（{len(rows)} 只，上限 {cfg.watchlist_max}）：")
        for row in rows:
            status = "启用" if int(row.get("enabled", 1)) == 1 else "已停用"
            if not cfg.watchlist_in_pool:
                in_pool = "不监控（watchlist_in_pool=false）"
            else:
                in_pool = "已进池" if row["symbol"] in pool_symbols else "未进池"
            note_text = f"｜备注 {row['note']}" if row.get("note") else ""
            print(f"  {row['symbol']} {row.get('name') or '（未知名称）'}"
                  f"｜{status}｜{in_pool}{note_text}")
        if not cfg.watchlist_in_pool:
            print("提示：watchlist_in_pool = false —— 自选只记录，不进池也不监控。")
        return 0

    if not symbol:
        print(f"--watchlist {action} 需要代码，例如：--watchlist {action} 600519")
        return 1
    symbol = symbol.zfill(6)

    if action == "add":
        if not (symbol.isdigit() and len(symbol) == 6):
            print(f"代码格式不对：{symbol}（应为 6 位数字）")
            return 1
        name = engine.get_stock_names([symbol]).get(symbol)
        if not name:
            print(f"⚠️ 本地库里没有 {symbol} 的名称（可能还没下载数据，或代码有误）——"
                  "仍按你给的信息添加，稍后同步数据会自动补上。")
        with storage.connect(cfg.db_path) as conn:
            storage.upsert_watchlist(conn, symbol, name=name, note=note, enabled=True)
            total = len(storage.load_watchlist(conn, enabled_only=True))
        print(f"✅ 已加入自选股：{symbol} {name or ''}"
              + (f"（备注 {note}）" if note else "")
              + f"；当前启用 {total} 只（上限 {cfg.watchlist_max}）")
        if total > cfg.watchlist_max:
            print(f"⚠️ 已超过上限 {cfg.watchlist_max}：盘中只会监控前 {cfg.watchlist_max} 只，"
                  "请在 config.toml 提高 watchlist_max，或停用暂时不看的。")
        return 0

    if action in ("remove", "del", "delete"):
        with storage.connect(cfg.db_path) as conn:
            removed = storage.remove_watchlist(conn, symbol)
        print("✅ 已从自选股移除：" + symbol if removed else f"未找到自选股 {symbol}")
        return 0 if removed else 1

    if action in ("enable", "disable"):
        enabled = action == "enable"
        with storage.connect(cfg.db_path) as conn:
            changed = storage.set_watchlist_enabled(conn, symbol, enabled)
        if not changed:
            print(f"未找到自选股 {symbol}")
            return 1
        print(f"✅ 已{'启用' if enabled else '停用'}：{symbol}"
              + ("" if enabled else "（停用后不进池、不监控，仍保留在列表里）"))
        return 0

    print(f"未知动作：{action}（可用：add / list / remove / enable / disable）")
    return 1


def _apply_run_time_override(cfg, args) -> None:
    """`--run-at` / `--run-at-fallback` / `--no-auto-run`：只改**内存里的**配置，不落盘。

    为什么要有：调试"改时间会不会按时跑"时不该顺手改掉人家的 config.toml
    （分发出去的机器上，运行时间是每个人自己设的）。
    """
    if getattr(args, "run_at", None):
        ok, messages = validate_run_times(args.run_at, args.run_at_fallback or
                                         cfg.run_at_fallback)
        for message in messages:
            print(("❌ " if not ok else "⚠️ ") + message)
        if not ok:
            raise SystemExit(2)
        cfg.run_at = args.run_at
        print(f"（临时）每天主跑时间：{args.run_at}")
    if getattr(args, "run_at_fallback", None):
        ok, messages = validate_run_times(args.run_at or cfg.run_at, args.run_at_fallback)
        for message in messages:
            print(("❌ " if not ok else "⚠️ ") + message)
        if not ok:
            raise SystemExit(2)
        cfg.run_at_fallback = args.run_at_fallback
        print(f"（临时）补跑时间：{args.run_at_fallback}")
    if getattr(args, "no_auto_run", False):
        cfg.auto_run = False
        print("（临时）每天自动运行已关闭：只在你手动点按钮/命令行跑时执行")


def _fmt_amount(value: int) -> str:
    """进度数值的显示：超过 1MB 就按 MB 显示（dump 下载是几百 MB，字节数没人看得懂）。"""
    if value >= 1_000_000:
        return f"{value / 1e6:.1f} MB"
    return f"{value:,}"


#: 进度打印节流：同一阶段至少间隔这么久、或至少跨这么多个百分点，才**新起一行**
#: 进度节流的门限：与同步层的 `sync.throttle_progress` 是**同一套语义**
#: （界面：「最多约 5 次/秒、或 1% 一发」；命令行：按秒/按 10% 起新行）。
#: 门限集中在这里，命令行与界面各取所需，避免两处各调一套参数、行为对不上。
PROGRESS_MIN_INTERVAL = 1.0
PROGRESS_STEP_PCT = 10
#: {阶段: (上次**成行**打印的时刻, 当时的百分比, 光标是否停在半行上)}
_PROGRESS_LAST: dict[str, tuple[float, int, bool]] = {}


def _progress_line(stage: str, done: int, total: int) -> str:
    """一行进度文本（不含换行）：带 MB、百分比与进度条。"""
    if total <= 0:
        return f"{stage}：{_fmt_amount(done)}"
    pct = done / total * 100
    bar = ""
    if total >= 1_000_000:                      # 大文件给一条进度条，180MB 才有"在动"的感觉
        filled = int(pct / 5)
        bar = " [" + "#" * filled + "." * (20 - filled) + "]"
    return (f"{stage}：{_fmt_amount(done)}/{_fmt_amount(total)}{bar}"
            f"（{pct:5.1f}%）")


def _progress(stage: str, done: int, total: int) -> None:
    """CLI 进度：**按秒或按 10%** 打一行（同一阶段内节流，不刷屏）。

    为什么要节流：dump 是 1 MB 一块下下来的，180 MB 就是 180 次回调；
    每次都打一行会把终端刷爆、有用的报错全被冲走。这里的规则是：

    - 距上次打印 ≥ `PROGRESS_MIN_INTERVAL` 秒，或百分比跨过 `PROGRESS_STEP_PCT` 的整数倍
      → **新起一行**（带换行，滚屏也看得见）；
    - 其余回调只在**同一行**上覆盖刷新（`\r`），保证"一直在动"；
    - 跑完（done ≥ total）一定收尾打一行 100%。
    """
    now = time.monotonic()
    pct = int(done / total * 100) if total > 0 else 0
    last_at, last_pct, inline = _PROGRESS_LAST.get(
        stage, (0.0, -PROGRESS_STEP_PCT, False)
    )
    finished = total > 0 and done >= total
    fresh_line = (
        finished
        or now - last_at >= PROGRESS_MIN_INTERVAL
        or pct // PROGRESS_STEP_PCT > last_pct // PROGRESS_STEP_PCT
    )
    line = _progress_line(stage, done, total)
    if fresh_line:
        if inline:
            print()                      # 先把"半行"结束掉，否则新行会接在它后面
        print(line, flush=True)
        _PROGRESS_LAST[stage] = (now, pct, False)
    else:
        print(f"\r{line}", end="", flush=True)
        _PROGRESS_LAST[stage] = (last_at, last_pct, True)


def _note(text: str) -> None:
    """下载状态（"正在重签 URL 继续下载（第 2 次）"）—— 另起一行，别被进度覆盖掉。"""
    print(f"  ↻ {text}", flush=True)


def _market_command(cfg) -> int:
    """`--market`：打印大盘概览页那几行后退出（与界面共用 `market.lines` 的同一份口径）。

    为什么要这个出口：界面上"看一眼大盘"得开窗口，而脚本、远程维护、以及
    "为什么卡片上全是 —"的排障，都需要一个纯文本的说法；退出码用它来决定
    "有没有取到东西"，所以取不到时必须是**非 0**，不能打印几行 `—` 就算成功。
    """
    from laoa_trader import market

    # 命令行总是取最新的：不吃界面/上一次运行留下的 TTL 缓存
    overview = market.fetch_overview(cfg, force=True)
    # 空串 = "这一组在配置里是空的"，界面会把那行隐藏，这里就别打空行
    for line in market.lines(overview):
        if line:
            print(line)
    if overview.get("as_of"):
        print(f"（取数时间 {overview['as_of']}"
              + ("；来自缓存" if overview.get("stale") else "") + "）")
    errors = [str(e) for e in (overview.get("errors") or [])]
    for message in errors:
        print(f"⚠️ {message}")
    if not market.has_data(overview):
        print("❌ 没取到任何数据：先按上面的原因处理"
              "（未配置同花顺 Key / 网络不通 / 被限流 / 代码写错）。")
        return 1
    return 0


def cli(argv: list[str] | None = None) -> int:
    """命令行模式。"""
    parser = argparse.ArgumentParser(
        prog="laoa_trader", description="老牛选股助手（命令行）"
    )
    parser.add_argument("--cli", action="store_true", help="强制命令行模式")
    parser.add_argument("--config", help="config.toml 路径")
    parser.add_argument("--download", action="store_true", help="下载/更新历史数据")
    parser.add_argument("--once", action="store_true", help="跑一次：数据增量 + 选股 + 建池")
    parser.add_argument("--pool", action="store_true", help="显示当前股票池")
    parser.add_argument(
        "--market", action="store_true",
        help="只看大盘概览：打印概览页那几行（涨跌停家数 + 沪深北成交额、涨跌家数、宽基/情绪/板块三组指数）后退出；"
             "取不到数据时退出码非 0 并说明原因",
    )
    # 2026-09-18：`--groups` / `--strategies` / `--list-groups` / `--scorecard`
    # （连同配套的 `--horizons` / `--out` / `--db` / `--top`）**整体删掉了** ——
    # 它们服务的对象是写在 `strategy/rules.py` 里的那 5 条 Python 策略与策略组机制，
    # 那两样东西随"内置策略改成随包公式"一起删除（见 legacy.py 的模块注释）。
    # 想按条件选股就在界面上勾公式（或直接改 `formulas/` 里的公式文件）。
    parser.add_argument(
        "--watchlist", nargs="+", metavar=("动作", "代码"),
        help="自选股管理：add 600519 / list / remove 600519 / enable 600519 / disable 600519"
             "（add 时名称自动从本地库补，可用 --note 写备注）",
    )
    parser.add_argument("--note", default="", help="配合 --watchlist add：备注（例如 龙头）")
    parser.add_argument("--serve", action="store_true", help="常驻：定时日更 + 盘中提醒")
    parser.add_argument(
        "--version", action="store_true",
        help="打印版本号与构建形态后退出（报障时先跑这个，一眼看出是不是旧包）",
    )
    parser.add_argument("--doctor", action="store_true",
                        help="自检：打印路径/依赖/凭据/数据概况，排障时先跑它")
    parser.add_argument("--no-notify", action="store_true", help="不推送通知（只落库）")
    parser.add_argument("--auto-download", action="store_true",
                        help="自检发现数据不可用时先自动下载再继续"
                             "（全量下载是分钟级动作，所以默认**不**自动做）")
    parser.add_argument("--force-download", action="store_true",
                        help="配合 --download：即使数据已就绪也强制重下")
    parser.add_argument("--run-at", metavar="HH:MM",
                        help="临时覆盖每天主跑时间（不改配置文件；例如 --run-at 16:30）")
    parser.add_argument("--run-at-fallback", metavar="HH:MM",
                        help="临时覆盖补跑时间（不改配置文件）")
    parser.add_argument("--no-auto-run", action="store_true",
                        help="临时关闭「每天自动运行」（只在你手动点/命令行跑时执行）")
    parser.add_argument("--interval", type=int, help="盘中轮询间隔（秒）")
    parser.add_argument("--limit", type=int, help="只处理前 N 只股票（试跑）")
    args = parser.parse_args(argv)
    _PROGRESS_LAST.clear()          # 每轮重新计数：同一阶段第二次跑也要有进度输出

    cfg = load_config(args.config) if args.config else get_config()
    _apply_run_time_override(cfg, args)

    if cfg.config_warning():
        print(f"⚠️ {cfg.config_warning()}")
    if cfg.history_warning():
        print(f"❌ {cfg.history_warning()}")

    problem = cfg.ensure_dirs_message()
    if problem:
        # 目录建不出来：自检与只读命令照常给出有用输出，写数据的命令才提前退出
        print(problem)
    setup_logging(None if problem else cfg.data_dir)
    logger = get_logger("cli")
    logger.info(f"数据目录：{cfg.data_dir}（配置来源：{cfg.source_path or '内置默认值'}）")

    if args.watchlist:
        return _watchlist_command(cfg, args.watchlist, args.note)

    # ── 进入正题前先自检（本地、秒级、不联网）──
    # 数据目录都建不出来时跳过自检：那种情况下真正的病根是目录/权限，
    # 报"没有历史数据"会把用户引到错的方向（下面的目录检查会给出正确提示）
    if not problem and any((args.once, args.pool, args.serve)):
        code = _preflight_gate(cfg, auto_download=args.auto_download)
        if code is not None:
            return code

    if getattr(args, "version", False):
        # 为什么单独给一个 --version（而不是让人翻关于页）：报障时最需要回答的两个问题是
        # "你装的是哪个版本"和"哪个构建形态"（PyInstaller 还是 Nuitka 编译版）。
        # CI 也用它做编译产物的冒烟检查（见 build/nuitka_build.py 与 workflow）。
        import laoa_trader

        from laoa_trader.strategy import formula as fm

        print(f"老牛选股助手 {laoa_trader.__version__}")
        print(f"构建形态：{runtime.describe()}")
        print(f"程序位置：{runtime.exe_dir()}")
        print(f"策略引擎：支持 {len(fm.SUPPORTED_FUNCTIONS)} 个函数")
        return 0

    if args.doctor:
        # 自检报告同时落盘一份（见 `_Tee` 的说明）：桌面版看不到控制台，
        # 而"出问题时把这段发我"是最省事的排查方式。
        report_path = Path(cfg.data_dir) / "logs" / "自检报告.txt"
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as handle:
                original = sys.stdout
                sys.stdout = _Tee(original, handle)
                try:
                    _doctor(cfg, startup_problem=problem)
                finally:
                    sys.stdout = original
            print(f"\n自检报告已写入：{report_path}")
        except OSError as exc:      # 目录不可写时不该让自检本身失败
            print(f"（自检报告没能写入 {report_path}：{exc}）")
            _doctor(cfg, startup_problem=problem)
        return 0

    if args.market:
        return _market_command(cfg)

    if args.pool:
        try:
            rows = pool_mod.pool_table_rows(cfg.db_path)
        except Exception as exc:  # noqa: BLE001 - 只读命令不该抛 traceback
            print(f"无法读取本地数据库（{cfg.db_path}）：{type(exc).__name__}: {exc}")
            return 1
        if not rows:
            print("股票池为空：先跑一次 `--once`（或先 `--download` 建库）")
            return 0
        print(f"股票池（{len(rows)} 只，{rows[0].get('date')}）：")
        for i, row in enumerate(rows, start=1):
            note = f"｜备注 {row['note']}" if row.get("note") else ""
            # 组别与持有期**单独一栏**：改版后「来源」列写的是"哪条策略"
            # （`策略·短期反转`），策略名与组名不再挤在同一格里 —— 命令行这边
            # 也不再重复打印策略名（那正是"同一件事说两遍"）。
            group = str(row.get("group_label") or "—")
            horizon = int(row.get("horizon") or 0)
            group_text = f"{group}（T+{horizon}）" if horizon and group != "—" else group
            # 标的写法与界面两张表一致：**半角**括号 `名称(代码)`（见 docs/开发文档.md）
            print(f"  {i:>2}. {row['name']}({row['symbol']})｜"
                  f"来源 {row.get('source_label') or '—'}｜组别 {group_text}｜"
                  f"{row.get('industry') or '—'}｜{row.get('reason') or ''}{note}")
        # 来源列的口径（2026-09-23 主人要求）：写「哪条策略选出来的」，
        # 前缀与「+自选」都已去掉；没有策略来源（用户自己加的）才写「自选」。
        print("来源说明：来源列写的是哪条策略选出来的（随包策略与你自写的都在这里，"
              "前缀已去掉）；手工加进自选、没有策略来源的票写「自选」")
        return 0

    # 只有"要写数据"的命令才因为目录不可用而终止；
    # 不带任何动作（或 --help）时照常打印帮助，退出码 0。
    if problem and (args.download or args.once or args.serve):
        print("数据目录不可用，已终止（请先按上面的提示修改 config.toml 的 data_dir）。")
        return 1

    if args.download:
        from laoa_trader.data import preflight

        local = preflight.check(cfg.db_path, cfg)
        print(f"数据自检：{local['status']} —— {local['reason']}")
        if local["status"] == preflight.READY and not args.force_download:
            # 需求：有 10 年库就不下载（避免把 20 分钟的重下变成"每次启动的默认动作"）
            print(f"✅ {preflight.summary_line(local)}；无需重新下载。"
                  "如确实要重下，请加 --force-download。")
            return 0
        print("开始下载历史数据（首次约 10~20 分钟，有进度显示，可中断后重跑续传）…")
        result = sync.download_history(
            cfg, progress_cb=_progress, symbol_limit=args.limit, note_cb=_note
        )
        print()
        _print_result(result)
        return 0 if result.ok else 1

    if args.once:
        from laoa_trader.scheduler import data_gate, run_daily

        # 数据闸门（与界面【开始选股】、调度线程同一口径）：
        # 没数据就跑公式 = 选出错的票，所以这里明确拒绝并返回非零退出码
        gate = data_gate(cfg, DataEngine(cfg.db_path))
        if not gate["ok"]:
            print(f"❌ {gate['message']}")
            return 1

        # ⚠️ 2026-09-18（用户要求）：**候选只来自勾选的公式** —— 那 5 条写在代码里的
        # Python 策略连同策略组机制一起删掉了（`--groups` / `--strategies` /
        # `--list-groups` / `--scorecard` 这些入口也随之删除）。
        # 所以这里不再因为"没有启用任何策略"而拒绝运行：一条公式都没勾 = 只盯自选股，
        # 那是**默认的正常状态**。
        enabled = [str(n) for n in (getattr(cfg, "enabled_formulas", None) or [])]
        if enabled:
            print("本次按勾选的策略选股：" + "、".join(enabled))
        else:
            print("没有勾选任何策略（enabled_formulas 为空）：本次只处理自选股")

        report = run_daily(cfg, DataEngine(cfg.db_path), notify=not args.no_notify)
        print(f"数据日期：{report['data_date']}")
        print(f"候选信号：{report['picks']} 条；写入信号 {report.get('signals', 0)} 行；"
              f"股票池：{len(report['pool'])} 只")
        for line in pool_mod.format_pool_lines(report["pool"]):
            print("  " + line)
        for err in report["errors"]:
            print(f"  ⚠️ {err}")
        if report.get("push_skipped"):
            # "没推"现在只有一种原因：同一天同一批内容已经推过（幂等）
            print(f"未推送：{report['push_skipped']}")
        if report.get("notify"):
            from laoa_trader.notify import summarize

            print(f"通知：{summarize(report['notify'])}")
        return 0

    if args.serve:
        from laoa_trader.scheduler import run_serve

        run_serve(cfg, interval=args.interval, dry_run=args.no_notify)
        return 0

    parser.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    """统一入口：无参数进 GUI，有 --cli / 参数进命令行。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = get_config()
    setup_logging(cfg.data_dir)

    wants_cli = "--cli" in argv or any(
        a in argv
        for a in ("--download", "--once", "--pool", "--serve", "--doctor",
                  "--watchlist", "--note", "--config", "--auto-download",
                  "--force-download", "--run-at", "--run-at-fallback",
                  "--no-auto-run", "--market", "--version",
                  "--help", "-h")
    )
    if wants_cli:
        return cli(argv)

    from laoa_trader.ui import app as ui_app

    if not ui_app.QT_AVAILABLE:
        # 界面不可用时自动降级：别让用户对着一个报错的 exe 发呆
        print("图形界面不可用（PySide6 未安装），已改为命令行模式。")
        print("可用的命令：--download / --once / --pool / --serve\n")
        return cli(["--cli"])
    try:
        return ui_app.run_gui(cfg)
    except Exception as exc:  # noqa: BLE001 - GUI 起不来时给出可执行的退路
        print(f"界面启动失败：{type(exc).__name__}: {exc}")
        print("可改用命令行：python -m laoa_trader --cli --once")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
