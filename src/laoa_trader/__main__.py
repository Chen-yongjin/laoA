"""老A法师 · 交易终端（Windows 单机版）。

入口：`python -m laoa_trader`（无参数启动 GUI），或 `--cli` 走命令行：

    python -m laoa_trader --cli --download     # 下载 10 年历史数据（含进度）
    python -m laoa_trader --cli --once         # 跑一次：数据增量 + 选股 + 建池
    python -m laoa_trader --cli --pool         # 只看当前股票池
    python -m laoa_trader --cli --serve        # 常驻：定时日更 + 盘中提醒

界面不可用（例如没装 PySide6）时**自动降级为 CLI**，不会直接崩。
"""

from __future__ import annotations

import argparse
import sys

from laoa_trader import pool as pool_mod
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

    print("老A法师 · 交易终端 —— 自检")
    print("=" * 56)
    print(f"程序版本    : {laoa_trader.__version__}")
    print(f"Python      : {sys.version.split()[0]}（{platform.system()} {platform.release()}）")
    print(f"配置来源    : {cfg.source_path or '内置默认值（未找到 config.toml）'}")
    if cfg.config_warning():
        print(f"配置告警    : ⚠️ {cfg.config_warning()}")
    print(f"数据目录    : {cfg.data_dir}")
    print(f"数据库      : {cfg.db_path}"
          f"（{'已存在' if cfg.db_path.exists() else '尚未创建'}）")
    print(f"dump 目录   : {cfg.dump_dir}（{'已存在' if cfg.dump_dir.exists() else '尚未创建'}）")
    print(f"日志文件    : {cfg.data_dir / 'logs' / 'laoa-trader.log'}")
    print("-" * 56)
    print(f"同花顺 Key  : {_mask(cfg.hithink_api_key)}")
    print(f"飞书凭证    : AppID {_mask(cfg.feishu_app_id)} / Secret {_mask(cfg.feishu_app_secret)}"
          f" / 会话 {cfg.feishu_chat_id or '（自动发现）'}")
    print(f"通知开关    : 飞书 {cfg.notify_feishu}、Windows {cfg.notify_windows}、托盘 {cfg.notify_tray}")
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


def _apply_selection_override(cfg, args) -> None:
    """把 `--groups/--strategies` 临时写进**内存里的** cfg（配置文件不动）。

    语义（尽量不让人意外）：
        - 都不给 → 完全按 config.toml；
        - 只给 `--groups` → 这些组的全部成员（忽略配置里的 enabled_strategies）；
        - 只给 `--strategies` → 就这几条策略（组范围放宽成全组，否则会被配置里
          关掉的组"二次过滤"，用户会觉得"我明明指定了却不跑"）；
        - 两个都给 → 组定范围、策略定取舍（取交集）。
    """
    from laoa_trader.strategy import groups as groups_mod

    cli_groups = _split_list(args.groups)
    cli_strategies = _split_list(args.strategies)
    if not cli_groups and not cli_strategies:
        return
    cfg.enabled_groups = cli_groups or list(groups_mod.GROUP_ORDER)
    cfg.enabled_strategies = cli_strategies


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
                cfg, auto_download=True, progress_cb=_progress)
            for item in results:
                print(("✅ " if item.ok else "❌ ") + item.message)
            return None if ok else 1
        print(f"⚠️ {preflight.summary_line(result)}；"
              "如需现在补齐，请加 --auto-download（或把 auto_download_on_start 设为 true）。")
        return None

    # needs_full：没有 10 年库
    if not auto_download:
        print("")
        print(f"❌ {result['reason']}")
        print("本地没有可用的历史数据（或数据落后太多，增量补不回来）。请先运行：")
        print("    python -m laoa_trader --cli --download")
        print("（或在 config.toml 里填好 hithink_api_key 后加 --auto-download 自动下载）")
        return 1
    print("需要下载 10 年全量历史（约 10~20 分钟，可中断后重跑续传）…")
    ok, after, results = preflight.ensure_ready(
        cfg, auto_download=True, progress_cb=_progress)
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


def _print_groups() -> None:
    """打印策略组说明（怎么选、有哪些成员）。"""
    from laoa_trader.strategy import groups as groups_mod
    from laoa_trader.strategy import rules as rules_mod

    print("策略组（config.toml 的 enabled_groups 里填 key）：")
    for line in groups_mod.describe_groups():
        print(line)
    print()
    print("成员策略的中文名（enabled_strategies 里可以填中文名或类名）：")
    for key in groups_mod.GROUP_ORDER:
        group = groups_mod.GROUPS[key]
        names = "、".join(
            f"{rules_mod.strategy_label(n)}（权重{w}）" for n, w in group.members
        )
        print(f"  {group.label}：{names}")
        print(f"      依据：{group.note}")
    print()
    print("例：python -m laoa_trader --cli --groups ultra,short")
    print("    python -m laoa_trader --cli --strategies 低价股,连板回踩低吸 --once")


def _progress(stage: str, done: int, total: int) -> None:
    """CLI 进度：单行覆盖刷新（终端里比滚屏友好）。"""
    if total <= 0:
        print(f"\r{stage}…", end="", flush=True)
        return
    pct = done / total * 100
    print(f"\r{stage}：{done}/{total}（{pct:5.1f}%）", end="", flush=True)
    if done >= total:
        print()


def cli(argv: list[str] | None = None) -> int:
    """命令行模式。"""
    parser = argparse.ArgumentParser(
        prog="laoa_trader", description="老A法师 · 交易终端（命令行）"
    )
    parser.add_argument("--cli", action="store_true", help="强制命令行模式")
    parser.add_argument("--config", help="config.toml 路径")
    parser.add_argument("--download", action="store_true", help="下载/更新历史数据")
    parser.add_argument("--once", action="store_true", help="跑一次：数据增量 + 选股 + 建池")
    parser.add_argument("--pool", action="store_true", help="显示当前股票池")
    parser.add_argument(
        "--groups",
        help="临时只跑这些策略组（逗号分隔，覆盖 config.toml 的 enabled_groups）："
             "ultra,short,swing",
    )
    parser.add_argument(
        "--strategies",
        help="临时只跑这些策略（逗号分隔，中文名或类名都认，覆盖配置）："
             "低价股,连板回踩低吸",
    )
    parser.add_argument("--list-groups", action="store_true",
                        help="列出策略组与成员策略（含持有期与权重），然后退出")
    parser.add_argument(
        "--watchlist", nargs="+", metavar=("动作", "代码"),
        help="自选股管理：add 600519 / list / remove 600519 / enable 600519 / disable 600519"
             "（add 时名称自动从本地库补，可用 --note 写备注）",
    )
    parser.add_argument("--note", default="", help="配合 --watchlist add：备注（例如 龙头）")
    parser.add_argument("--serve", action="store_true", help="常驻：定时日更 + 盘中提醒")
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

    cfg = load_config(args.config) if args.config else get_config()
    _apply_selection_override(cfg, args)
    _apply_run_time_override(cfg, args)

    if args.list_groups:
        _print_groups()
        return 0

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

    if args.doctor:
        _doctor(cfg, startup_problem=problem)
        return 0

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
            print(f"  {i:>2}. {row['name']}（{row['symbol']}）"
                  f"{row.get('label') or ''}｜来源 {row.get('source_label') or '—'}｜"
                  f"{row.get('industry') or '—'}｜{row.get('reason') or ''}{note}")
        print("来源说明：策略=按策略组选出；自选=你自己加的；策略+自选=两者都有（只出现一行）")
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
        print("开始下载历史数据（首次约 10~20 分钟，可中断后重跑续传）…")
        result = sync.download_history(
            cfg, progress_cb=_progress, symbol_limit=args.limit
        )
        _print_result(result)
        return 0 if result.ok else 1

    if args.once:
        from laoa_trader.scheduler import run_daily

        from laoa_trader.strategy import groups as groups_mod

        selection = groups_mod.resolve_from_config(cfg)
        for warn in selection.warnings:
            print(f"⚠️ {warn}")
        if selection.empty and not getattr(selection, "explicit_off", False):
            print("没有启用任何策略（检查 enabled_groups / enabled_strategies 或用 --list-groups）")
            return 1
        if selection.explicit_off:
            print("策略已关闭（enabled_groups = [\"none\"]）：本次只处理自选股")
        else:
            print(f"启用的策略组：{selection.describe()}")

        report = run_daily(cfg, DataEngine(cfg.db_path), notify=not args.no_notify,
                           selection=selection)
        print(f"数据日期：{report['data_date']}")
        print(f"候选信号：{report['picks']} 条；写入信号 {report.get('signals', 0)} 行；"
              f"股票池：{len(report['pool'])} 只")
        for line in pool_mod.format_pool_lines(report["pool"]):
            print("  " + line)
        for err in report["errors"]:
            print(f"  ⚠️ {err}")
        if report.get("push_skipped"):
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
                  "--groups", "--strategies", "--list-groups", "--watchlist",
                  "--note", "--config", "--auto-download", "--force-download",
                  "--run-at", "--run-at-fallback", "--no-auto-run",
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
