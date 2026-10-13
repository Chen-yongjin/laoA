"""luweik决策系统（Windows 专业版）。

入口：`python -m laoa_trader`（无参数启动 GUI），或 `--cli` 走命令行：

    python -m laoa_trader --cli --download     # 下载 10 年历史数据（含进度）
    python -m laoa_trader --cli --once         # 跑一次：数据增量 + 匹配 + 建池
    python -m laoa_trader --cli --pool         # 只看当前股票池
    python -m laoa_trader --cli --market       # 打印大盘概览那几行
    python -m laoa_trader --cli --market       # 只看大盘概览（与页面同一份：家数/成交额/涨跌家数/三组指数）
    python -m laoa_trader --cli --serve        # 常驻：定时日更 + 盘中提醒
    python -m laoa_trader --gui-selftest       # 图形界面冒烟自检（离屏；发版前/CI 用）

界面不可用（例如没装 PySide6）时**自动降级为 CLI**，不会直接崩。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from laoa_trader import pool as pool_mod
from laoa_trader import runtime
from laoa_trader.config import Config, default_data_dir, get_config, load_config
from laoa_trader.data import sync
from laoa_trader.data.engine import DataEngine
from laoa_trader.log import LOG_NAME, get_logger, setup_logging
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

    def __init__(self, *streams, flush_each: bool = False) -> None:
        self._streams = streams
        #: 每写一段就 flush 一次。`--gui-selftest` 用得上：那段代码要盯的恰恰是
        #: "界面建到一半整个进程没了"（Nuitka 段错误），缓冲里没落盘的内容会一起消失，
        #: 报告就成了空文件 —— 而那正是最需要它的时候。
        self._flush_each = flush_each

    def write(self, text: str) -> int:
        for stream in self._streams:
            try:
                stream.write(text)
                if self._flush_each:
                    stream.flush()
            except Exception:  # noqa: BLE001 - 其中一个流坏了不该影响另一个
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass


class _ReportLogHandler(logging.Handler):
    """把 `laoa_trader` 的日志**同时**写进自检报告（每一条都 flush）。

    为什么要它（2026-10-13 的 1.9.2 编译版实报"双击打不开"）：那次崩的是**段错误** ——
    没有 traceback、stdout 缓冲也可能一起没掉，唯一的下落不明的现场就是"日志停在哪一行"。
    所以自检报告里除了自己打印的几行，还要有**逐步的日志**：
    `ui/app.py::startup_step()` 写的 `启动步骤：…` 每一步都刷新一次，
    报告（以及 CI 的 smoke.log）里的**最后一行**就是崩溃点。

    只挂在自检期间（`_gui_selftest` 装上、跑完摘掉），不影响正常运行的日志配置。
    """

    def __init__(self, stream: Any) -> None:
        super().__init__()
        self._stream = stream
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()          # 崩了就崩在这一行之后 —— 必须立刻落盘
        except Exception:  # noqa: BLE001 - 报告写不进去不该影响程序
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

    print("luweik —— 自检")
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
    print(f"日志文件    : {Path(cfg.data_dir) / 'logs' / LOG_NAME}")
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
            print("\n提示：未配置同花顺 API Key，只能基于本地已有数据匹配，无法更新。")
    except Exception as exc:  # noqa: BLE001 - 自检本身不能崩
        print(f"数据概况读取失败：{type(exc).__name__}: {exc}")


#: GUI 自检报告的文件名。
#: **故意用 `.log` 而不是 `.txt`**：`--doctor` 那份叫 `logs/自检报告.txt`，而 CI 里是按
#: `logs/*.txt` 取"最新一份"来断言"运行形态 = Nuitka / 随包策略 / 资源目录"的 ——
#: 这份写得更晚，如果也叫 `.txt` 就会把 doctor 那份挤掉，让那几条断言莫名其妙地红。
GUI_SELFTEST_REPORT_NAME = "GUI自检报告.log"
#: 控件数下限：主窗口是五个页签 + 两张表格 + 一堆按钮，正常在 300 个以上。
#: 低于这个数说明界面**根本没搭出来**（自检不该在这种情况下报 OK）。
GUI_SELFTEST_MIN_WIDGETS = 50


def _dependency_versions() -> str:
    """自检报告里的"依赖版本"一行（PySide6/Qt/pandas/pyarrow/numpy）。

    为什么要打这一行（2026-10-13 的 1.9.2 事故里，这是最可疑的一条线索）：CI 每次构建都
    从头 `pip install -e ".[dev]"`，而 `PySide6>=6.6` **没有钉版本** —— 于是
    "1.9.1 能双击打开、1.9.2 一进界面就段错误"完全可能是**构建时 Qt 版本漂了**
    （同一份源码、不同的 Qt：Qt 自己的插件/DLL 与 Nuitka 的收集结果都会变）。
    把版本写进报告之后，两个版本的包/两份 smoke.log 放一起对比就能一眼看出差在哪。
    """
    parts: list[str] = []
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        parts.append(f"PySide6 {PySide6.__version__}（Qt {qVersion()}）")
    except Exception:  # noqa: BLE001 - 报告里少一行不该影响自检
        parts.append("PySide6 不可用")
    for name in ("pandas", "pyarrow", "numpy"):
        try:
            module = __import__(name)
            parts.append(f"{name} {getattr(module, '__version__', '?')}")
        except Exception:  # noqa: BLE001
            parts.append(f"{name} 缺失")
    return " / ".join(parts)


def _teardown_selftest_window(window: Any, app: Any) -> None:
    """自检收尾：定时器、后台线程、桌宠/消息列表/浮窗全部收掉（绝不抛、可重复调用）。

    为什么不能只是 `window.close()`：关窗在主窗口里等于"收进托盘"（`closeEvent` 是
    `ignore()` + `hide()`），定时器与后台线程照旧活着 —— 而自检完了就要退出进程，
    那时还活着的 `QThread` 会给出 `QThread: Destroyed while thread '' is still running`，
    在 CI 上表现为"自检崩了、却看不出是谁的锅"。
    所以走**生产代码的退出路径**（`MainWindow.shutdown()`，与托盘【退出】同一个方法），
    再补上测试里那套"显式处理 DeferredDelete"（`deleteLater()` 只排队，不跑
    `sendPostedEvents` 就不会真删）。
    """
    for name in ("_timer", "_market_timer", "_flash_timer", "_flash_stop_timer"):
        timer = getattr(window, name, None)
        if timer is None:
            continue
        try:
            timer.stop()
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住退出
            pass
    for step in (
        window.shutdown,
        lambda: getattr(window, "tray", None) and window.tray.hide(),
        window.close,
        window.deleteLater,
    ):
        try:
            step()
        except Exception:  # noqa: BLE001 - 收尾失败不该挡住退出
            pass
    if app is None:
        return
    try:
        app.processEvents()
        from PySide6.QtCore import QEvent

        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # 真正执行删除
        app.processEvents()
    except Exception:  # noqa: BLE001
        pass


def _gui_selftest(args) -> int:
    """`--gui-selftest`：**图形界面**的冒烟自检（编译产物发版前的守门人）。

    为什么必须有这一条（2026-10-13 主人实报）：1.9.2 双击打不开 ——
    `luweik决策系统.exe --cli --version` 一切正常，一进图形界面就是 Nuitka 段错误，
    而当时的 CI 冒烟只跑 `--version` / `--doctor`（**全程不碰 GUI**），
    于是"只在编译版、只在界面路径上出现"的崩溃一路绿灯发到了用户手里。

    走的是与用户双击**同一条路**：`QT_QPA_PLATFORM=offscreen`（在进程内自己设，
    不依赖外部环境）→ 建 `QApplication`（含 `setQuitOnLastWindowClosed` 与图标，
    与 `ui/app.py::run_gui` 一致）→ 建**真的** `MainWindow` → `show()` → 跑一次
    `_tick()` → `processEvents()` 几轮 → `shutdown()` 收尾 →
    打印一行 `GUI 自检 OK：窗口 N 个控件、版本 x.y.z` → 退出码 0。

    四条纪律：

    1. **绝不弹模态框**：关掉 `ui/error_popup.py` 那条唯一出口（自检里没人能点掉它，
       弹出来就是永远等下去 —— CI 上是超时，用户看到的是"卡住"）；
    2. **不联网**：概览页那 4~6 个（热力图 28 个）请求自检不需要 —— 断网/服务端不响应
       会让"界面冒烟"变成随机红，退出时还可能留下一条活线程；
    3. **不碰用户真实目录**：`--config` 指哪用哪；没给就用一个临时目录；
    4. **任何异常都吐 traceback 并返回非 0**：CI 靠退出码判红，不能"看起来跑完了"。

    Args:
        args: `cli()` 解析出来的参数（只用到 `config`）。

    Returns:
        进程退出码：0 = 界面建出来了；1 = 界面起不来/自检失败（traceback 已打印）；
        2 = 前提不成立（`--config` 指定了却没生效，见下面那段说明）。
    """
    # 离屏平台：CI runner（没有交互桌面）也能把真窗口建出来。
    # 用 `setdefault` 而不是硬覆盖：真机上排查"是不是平台插件的问题"时，
    # 我们自己指定 QT_QPA_PLATFORM 要能生效。
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    if args.config:
        cfg = load_config(args.config)
        # ⚠️ 必须查它到底认没认这个文件：`load_config` 读不出来（路径写错 / 带 BOM /
        # TOML 语法错）时会**静默退回默认值**，而默认数据目录正是**用户真实那一个** ——
        # 自检会往里建库、写日志。CI 就踩过它的孪生形态（config.toml 带 BOM → 第一个键名
        # 变成 `\ufeffdata_dir` → 被当成未知键忽略 → 报告写进了 %LOCALAPPDATA%）。
        # 与其"悄悄跑到别的目录上"再报个 OK（那正是 1.9.2 那种静默失败的路子），
        # 不如直接拒绝并说清哪个文件没起作用。
        why = ""
        if cfg.source_path is None:
            why = "文件不存在（或读不动）"
        elif cfg.config_warning():
            why = cfg.config_warning()
        elif Path(cfg.data_dir) == default_data_dir():
            why = (f"配置里的 data_dir 没生效，解析出来还是默认数据目录（{cfg.data_dir}）"
                   "—— 常见原因是文件带 BOM，或者压根没写 data_dir")
        if why:
            print(f"❌ GUI 自检拒绝运行：--config {args.config} 没能生效（{why}）。")
            print(f"   它现在指向默认数据目录 {cfg.data_dir} —— 那是**用户真实目录**，")
            print("   而自检会往里建库、写日志；自检宁可不跑，也不碰用户真实目录。")
            print("   请修好配置后重跑；或者不带 --config 跑（那样自动用临时数据目录）。")
            return 2
        source_text = str(cfg.source_path)
    else:
        # 没给 --config 时**绝不能**回落到用户真实的数据目录（自检会建库、写日志、
        # 界面上还可能回写配置）：造一个临时目录给它。
        # 跑完**不删**：Windows 上日志文件还开着（删不掉），而且真出问题时那个目录
        # 正好是排查材料 —— 把路径打出来，让用户自己决定留还是删。
        temp_dir = Path(tempfile.mkdtemp(prefix="luweik-gui-selftest-"))
        cfg = Config(data_dir=temp_dir)
        source_text = "内置默认值（没给 --config：数据目录用临时目录，不碰用户真实目录）"

    problem = cfg.ensure_dirs_message()
    # 日志先配上：自检里"界面建到一半就没了"时，日志是**唯一**能说明它走到哪一步的东西
    # （1.9.2 那次段错误的最后两行就是这么来的）。
    if not problem:
        setup_logging(cfg.data_dir)
    logger = get_logger("cli")

    report_path = Path(cfg.data_dir) / "logs" / GUI_SELFTEST_REPORT_NAME
    handle = None
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(report_path, "w", encoding="utf-8")
    except OSError as exc:      # 报告写不了不该让自检本身失败（CI 那边还能看退出码）
        print(f"（GUI 自检报告没能写入 {report_path}：{exc}）")

    original = sys.stdout
    mirror = None
    if handle is not None:
        # flush_each=True：见 `_Tee` 的说明（要盯的就是"建到一半进程没了"）
        sys.stdout = _Tee(original, handle, flush_each=True)
        # 日志也镜像进报告：崩在哪一步，看报告里 `启动步骤：…` 的最后一行
        mirror = _ReportLogHandler(handle)
        logging.getLogger("laoa_trader").addHandler(mirror)
    try:
        import laoa_trader

        print("luweik —— GUI 自检（离屏）")
        print("=" * 56)
        print(f"程序版本  : {laoa_trader.__version__}")
        print(f"运行形态  : {runtime.describe()}")
        print(f"平台插件  : QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM')}")
        print(f"依赖版本  : {_dependency_versions()}")   # Qt 版本漂了 = 1.9.2 事故的头号嫌疑
        print(f"配置来源  : {source_text}")
        print(f"数据目录  : {cfg.data_dir}")
        if problem:
            print(f"⚠️ {problem}")
        print("-" * 56)
        print("定位提示：下面日志里 `启动步骤：…` 的**最后一行**就是崩溃点"
              "（段错误没有 traceback，只能靠它）")
        logger.info(f"GUI 自检开始：数据目录 {cfg.data_dir}")
        try:
            return _gui_selftest_run(cfg, report_path)
        except BaseException:   # noqa: BLE001 - 自检的意义就是"崩了要让人看见"
            print("❌ GUI 自检失败：")
            # 两份都要：stderr 那份进 CI 的 `gui-selftest.err.txt`，
            # stdout 这份**同时进报告文件**（界面崩了时报告是唯一的现场）
            traceback.print_exc()
            print(traceback.format_exc())
            logger.error("GUI 自检失败", exc_info=True)
            return 1
    finally:
        if mirror is not None:
            logging.getLogger("laoa_trader").removeHandler(mirror)
        sys.stdout = original
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _gui_selftest_run(cfg, report_path: Path) -> int:
    """自检的正题：建真窗口 → 跑一拍 → 收尾。异常一路抛给 `_gui_selftest`（它负责非 0）。"""
    import laoa_trader
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import error_popup as error_popup_mod

    if not ui_app.QT_AVAILABLE:
        print("❌ 图形界面不可用（PySide6 没装 / 导入失败）—— GUI 自检无法进行。")
        return 1

    from PySide6.QtWidgets import QApplication, QWidget

    # ① 自检期间**一个模态框都不许弹**（见 `_gui_selftest` 的第 1 条纪律）。
    #    放在建窗口之前：`MainWindow.__init__` 里就会走到启动自检那条会弹框的路。
    previous_silent = error_popup_mod.set_silent(True)
    app = None
    window = None
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setQuitOnLastWindowClosed(False)   # 与 run_gui 一致：关窗只是收进托盘
        ui_app._set_app_icon(app)              # 与 run_gui 一致（图标=资源路径解析的探针）

        # 真主窗口：与用户双击时**同一段代码**（`ui/app.py::MainWindow.__init__`）——
        # 1.9.2 的段错误正是死在这里的后半段（屏幕几何 / `_build_ui` / 托盘 / 桌宠 / 定时器）。
        window = ui_app.MainWindow(cfg)

        # ② 不联网：概览页的取数只在"这一页真的在眼前"时才发（见 `MainWindow._market_page_visible`）。
        #    ⚠️ **必须在第一次 processEvents 之前**切页签：`__init__` 用
        #    `QTimer.singleShot(0, self._market_tick)` 排了启动那一轮概览取数，
        #    只要跑一次事件循环它就会发出去（4~6 个请求，还有热力图那 28 个）——
        #    实测第一次跑就是"打印 OK 之后进程 Aborted、退出码 134"（退出时那两条线程还在飞）。
        #    切走之后 `_market_tick` 自己就返回了，窗口照建照 show，五页照搭。
        watch_page = getattr(window, "watch_page", None)
        if watch_page is not None:
            window.tabs.setCurrentWidget(watch_page)

        window.show()
        app.processEvents()

        # ③ 真跑一拍：与用户双击后每 5 秒那一拍是**同一段代码**（含刷状态/两张表/快照/评分/提醒对账）
        window._tick()
        for _ in range(5):
            app.processEvents()

        count = len(window.findChildren(QWidget))
        if count < GUI_SELFTEST_MIN_WIDGETS:
            print(f"❌ GUI 自检失败：主窗口只建出 {count} 个控件"
                  f"（低于下限 {GUI_SELFTEST_MIN_WIDGETS}，界面没搭起来）")
            return 1
        print(f"GUI 自检 OK：窗口 {count} 个控件、版本 {laoa_trader.__version__}")
        print(f"报告文件  : {report_path}")
        return 0
    finally:
        # 先收窗口再还原弹窗开关：收尾过程中真出了问题也该**只记日志**，不许弹框
        if window is not None:
            _teardown_selftest_window(window, app)
        error_popup_mod.set_silent(previous_silent)


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


def _watchlist_command(cfg, values: list[str], note: str, *, remind: bool = False) -> int:
    """自选标的命令行：add / list / remove / enable / disable。

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
            print("自选标的为空。用 `--watchlist add 600519 --note 龙头` 添加。")
            return 0
        pool_symbols = set(pool_mod.pool_symbols(cfg.db_path))
        print(f"自选标的（{len(rows)} 只，上限 {cfg.watchlist_max}）：")
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
            # 默认**不提醒**（2026-10-05 主人的口径："默认只监控持仓股票"）；
            # 要在盘中盯它，用界面点那一行的监控开关（或直接改 watchlist.enabled）
            storage.upsert_watchlist(conn, symbol, name=name, note=note,
                                     enabled=True if remind else None)
            total = len(storage.load_watchlist(conn, enabled_only=True))
        print(f"✅ 已加入自选标的：{symbol} {name or ''}"
              + (f"（备注 {note}）" if note else "")
              + ("；已开始盯它" if remind else "；默认不提醒（加 --remind 才会盯）")
              + f"；当前盯着 {total} 只（上限 {cfg.watchlist_max}）")
        if total > cfg.watchlist_max:
            print(f"⚠️ 已超过上限 {cfg.watchlist_max}：盘中只会监控前 {cfg.watchlist_max} 只，"
                  "请在 config.toml 提高 watchlist_max，或停用暂时不看的。")
        return 0

    if action in ("remove", "del", "delete"):
        with storage.connect(cfg.db_path) as conn:
            removed = storage.remove_watchlist(conn, symbol)
        print("✅ 已从自选标的移除：" + symbol if removed else f"未找到自选标的 {symbol}")
        return 0 if removed else 1

    if action in ("enable", "disable"):
        enabled = action == "enable"
        with storage.connect(cfg.db_path) as conn:
            changed = storage.set_watchlist_enabled(conn, symbol, enabled)
        if not changed:
            print(f"未找到自选标的 {symbol}")
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
        prog="luweik", description="luweik（命令行）"
    )
    parser.add_argument("--cli", action="store_true", help="强制命令行模式")
    parser.add_argument("--config", help="config.toml 路径")
    parser.add_argument("--download", action="store_true", help="下载/更新历史数据")
    parser.add_argument("--once", action="store_true", help="跑一次：数据增量 + 匹配 + 建池")
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
    # 想按条件匹配就在界面上勾公式（或直接改 `formulas/` 里的公式文件）。
    parser.add_argument(
        "--watchlist", nargs="+", metavar=("动作", "代码"),
        help="自选标的管理：add 600519 / list / remove 600519 / enable 600519 / disable 600519"
             "（add 时名称自动从本地库补，可用 --note 写备注）",
    )
    parser.add_argument("--note", default="", help="配合 --watchlist add：备注（例如 龙头）")
    parser.add_argument(
        "--remind", action="store_true",
        help="配合 --watchlist add：立刻开始盯这只票（默认加进来**不提醒** ——"
             "2026-10-05 起默认只监控持仓股票）",
    )
    parser.add_argument("--serve", action="store_true", help="常驻：定时日更 + 盘中提醒")
    parser.add_argument(
        "--version", action="store_true",
        help="打印版本号与构建形态后退出（报障时先跑这个，一眼看出是不是旧包）",
    )
    parser.add_argument("--doctor", action="store_true",
                        help="自检：打印路径/依赖/凭据/数据概况，排障时先跑它")
    parser.add_argument(
        "--gui-selftest", action="store_true",
        help="图形界面冒烟自检：离屏（offscreen）建一次**真的**主窗口、跑一拍后退出。"
             "只跑 --version/--doctor 是碰不到界面的（1.9.2 的段错误就是这么漏过去的），"
             "发版前与 CI 用它守门；退出码非 0 即失败，"
             f"过程同时写进 <数据目录>/logs/{GUI_SELFTEST_REPORT_NAME}",
    )
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

    if args.gui_selftest:
        # 图形界面冒烟：**在加载配置之前**分支出去 —— 它自带配置与日志，
        # 而且绝不碰用户真实的数据目录（见 `_gui_selftest` 的说明）。
        #
        # 退出码过 `finish_exit()`：自检收尾之后若还有后台线程在跑（概览取数/评分那几条），
        # Qt 会在解释器收尾时把它们析构掉 → `abort()` → **退出码 134**，
        # 而 CI 的冒烟步骤读的就是这个退出码 —— "界面明明是好的"却报红。
        from laoa_trader.ui import threads as threads_mod

        return threads_mod.finish_exit(_gui_selftest(args))

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
        return _watchlist_command(cfg, args.watchlist, args.note, remind=args.remind)

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

        print(f"luweik {laoa_trader.__version__}")
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

        # 数据闸门（与界面【开始筛选】、调度线程同一口径）：
        # 没数据就跑公式 = 选出错的票，所以这里明确拒绝并返回非零退出码
        gate = data_gate(cfg, DataEngine(cfg.db_path))
        if not gate["ok"]:
            print(f"❌ {gate['message']}")
            return 1

        # ⚠️ 2026-09-18（用户要求）：**候选只来自勾选的公式** —— 那 5 条写在代码里的
        # Python 策略连同策略组机制一起删掉了（`--groups` / `--strategies` /
        # `--list-groups` / `--scorecard` 这些入口也随之删除）。
        # 所以这里不再因为"没有启用任何策略"而拒绝运行：一条公式都没勾 = 只盯自选标的，
        # 那是**默认的正常状态**。
        enabled = [str(n) for n in (getattr(cfg, "enabled_formulas", None) or [])]
        if enabled:
            print("本次按勾选的策略筛选：" + "、".join(enabled))
        else:
            print("没有勾选任何策略（enabled_formulas 为空）：本次只处理自选标的")

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
    # GUI 自检**先于真实配置加载**就分流：它自带临时数据目录与日志（见 `_gui_selftest`），
    # 而 `get_config()` 会去读用户的 config.toml、`setup_logging()` 会往用户的数据目录写日志 ——
    # "绝不碰用户真实目录"是这条命令的硬要求，所以连读都不该先读一遍。
    if "--gui-selftest" in argv:
        return cli(argv)
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
