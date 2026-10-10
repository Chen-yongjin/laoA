"""测试公共设施：离线、不联网、不需要 API Key。

关键 fixture：

- `_block_network`（autouse）：**在 socket 层封死一切真实网络访问** ——
  任何漏网的 client 都会立刻报错，而不是悄悄打真实接口（消耗配额、被限流、结果不稳定）；
- `cfg`：临时数据目录的配置（不碰用户真实目录）；
- `db`：**合成行情库** —— 造出"能过策略条件"的数据，覆盖后复权、行业、涨停池、交易日历；
- `FakeClient` / `FakeSession`：假的同花顺客户端与假 HTTP 会话（记录调用、可编程返回/报错），
  用来测"网络失败也要返回结构化结果"这条硬性要求；
- `modal_calls`（+ autouse 的 `_never_block_on_modals`）：**把一切模态框变成"记一笔就返回"**
  —— 见下面的说明，这是"整套测试不会卡在弹窗上"的唯一保证。

为什么必须在 socket 层封：只靠"记得注入假 client"是不够的 —— 一次疏忽就会让
测试套件联网。封死之后，这个疏忽会变成一条**失败**，而不是一次静默的真实请求。
"""

from __future__ import annotations

import logging
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 保证 `pytest laoA/tests` 在未安装包时也能 import（与 pyproject 的 pythonpath 双保险）
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from laoa_trader.config import Config  # noqa: E402
from laoa_trader.data import hithink as hx  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.data.engine import DataEngine  # noqa: E402

#: 允许解析的主机名（本机回环不算联网）
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


class NetworkBlocked(RuntimeError):
    """测试里试图发起真实网络访问（IPv4/IPv6）。"""


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch):
    """每个测试都用一份干净的全局配置（无凭据、默认值）。

    为什么要这样：`save_settings()` / `set_config()` 会改进程级单例，
    如果不隔离，前一个测试保存的 "abc123" 会被后面的测试读到 ——
    表现为"测试之间互相串味"（实测就是这样炸过：某个用例突然去解析真实域名）。
    用 monkeypatch 设置，pytest 会在测试结束自动还原。
    """
    from laoa_trader import config as config_mod

    monkeypatch.setattr(config_mod, "_config", Config())
    yield




def _install_network_block() -> dict:
    """把 socket 层的连接与域名解析换成"立刻抛 `NetworkBlocked`"。

    抽成函数是为了让"按用例"与"整场"两处**共用同一份实现** —— 两份实现迟早会漂移，
    而漂移的表现是"某条路径悄悄真的联网了"，那正是最不该发生的事。
    """
    real = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
    }

    def _refuse(address) -> None:
        raise NetworkBlocked(
            f"测试不允许联网，但检测到对 {address!r} 的连接尝试；"
            "请把该调用改成注入假 client / 假 Session（见 tests/conftest.py）"
        )

    def connect(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse(address)
        return real["connect"](self, address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse(address)
        return real["connect_ex"](self, address, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        _refuse(address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            raise NetworkBlocked(f"测试不允许做域名解析，但检测到 {host!r}")
        return real["getaddrinfo"](host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    return real


def _restore_network(real: dict) -> None:
    socket.socket.connect = real["connect"]
    socket.socket.connect_ex = real["connect_ex"]
    socket.create_connection = real["create_connection"]
    socket.getaddrinfo = real["getaddrinfo"]


@pytest.fixture(scope="session", autouse=True)
def _block_network_for_the_whole_session():
    """**整场**封网（不只是每个用例期间）—— 这条是防进程崩，不是防"测试联网"。

    为什么必须整场：界面用例会起后台 `QThread` 取数，而 `_block_network` 是**按用例**
    打桩的，用例一结束就撤销。漏网的线程于是拿着**未打桩**的 `socket.getaddrinfo`
    去做真实 DNS —— 而 `QuoteService.stop()` 只等 3 秒，窗口一销毁，Qt 就
    `abort()`（实测：整套用例跑到一半以 `Fatal Python error: Aborted` 结束，
    崩溃线程栈正停在 `socket.py … getaddrinfo`）。
    整场封住之后，这种线程只会拿到一个**立刻抛出**的 `NetworkBlocked`
    （产品代码本来就捕获它），既不会真联网、也不会挂住线程。
    """
    real = _install_network_block()
    _restore_network_on_exit = real
    try:
        yield
    finally:
        _restore_network(_restore_network_on_exit)


@pytest.fixture(scope="session", autouse=True)
def _drain_threads_at_session_end():
    """整场收尾：把还活着的后台线程**等到落地**，并把没落地的名字打到日志里。

    为什么要有这一条（2026-09-21）：CI（Windows）三次死在"跑到 89~93% 就没声音了、
    没有任何失败用例"，症状是后台线程在 `sys.stderr` 已关之后还在写日志 ——
    也就是**线程活过了整场测试**。线程漏收的根因已分别修掉（`MainWindow.shutdown()`
    现在会走 `Worker.wait_all()` 兜底、并给朗读线程送哨兵），
    这里再兜最后一道：如果还有线程没落地，**把它的名字打出来**，下次一眼就能定位是谁
    （上一次就是靠"日志里只剩 I/O operation on closed file"猜了好几轮）。
    """
    yield
    import threading

    try:
        from laoa_trader.notify import voice as voice_mod

        voice_mod.shutdown()
    except Exception:  # noqa: BLE001 - 收尾失败不该影响测试结论
        pass
    main = threading.main_thread()
    deadline = __import__("time").monotonic() + 5.0
    while __import__("time").monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t is not main and t.is_alive()
                 and not t.daemon]
        if not alive:
            return
        __import__("time").sleep(0.1)
    leftover = [f"{t.name}(daemon={t.daemon})" for t in threading.enumerate()
                if t is not main and t.is_alive()]
    if leftover:
        # ⚠️ 这里**不能走 logging**：整场收尾时 pytest 已经把它的 handler/流收掉了，
        # 再从这儿写日志就会得到 I/O operation on closed file（2026-09-21 实测：
        # CI 的 stderr 里正是这条 —— 它把「还有线程活着」这个真问题掩盖成了「日志在报错」）。
        # 直接写**原始 stderr**，并自己兜异常：诊断失败绝不能影响测试结论。
        try:
            import sys as _sys

            stream = getattr(_sys, "__stderr__", None)
            if stream is not None:
                stream.write(
                    "[tests] 整场收尾时仍有线程存活："
                    + "、".join(leftover)
                    + "（若 CI 再出现跑到半路中止，先看这一行）\n"
                )
                stream.flush()
        except Exception:  # noqa: BLE001 - 诊断失败不影响测试结论
            pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch):
    """封死 IPv4/IPv6 的连接与域名解析；放行 AF_UNIX（Qt/DBus 等本地 IPC 需要）。

    AF_UNIX 特意放行：Qt 的托盘/D-Bus 在本机走 unix socket，那不是"联网"；
    而 TCP/UDP 一律拒绝 —— 单元测试不该有任何真实外呼。
    实现与"整场封网"共用（见 `_install_network_block`）。
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _refuse(address) -> None:
        raise NetworkBlocked(
            f"测试不允许联网，但检测到对 {address!r} 的连接尝试；"
            "请把该调用改成注入假 client / 假 Session（见 tests/conftest.py）"
        )

    def connect(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse(address)
        return real_connect(self, address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse(address)
        return real_connect_ex(self, address, *args, **kwargs)

    def create_connection(address, *args, **kwargs):  # 替代 socket.create_connection
        _refuse(address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            raise NetworkBlocked(f"测试不允许做域名解析，但检测到 {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield


#: 一次测试里"本该弹出来的模态框"被记在这里（`modal_calls` 夹具暴露给用例断言）。
_MODAL_CALLS: list[dict] = []


@pytest.fixture(scope="session", autouse=True)
def _install_modal_guard():
    """**整场**把模态框变成"记一笔就返回"：这是"测试不会卡在弹窗上"的唯一保证。

    为什么必须是 **session 作用域**（这一点踩过坑）：一开始写成 function 作用域的
    autouse 夹具，结果 `window` 这类用例自己的夹具**收尾时**（`qapp.processEvents()`
    里投递的后台失败信号 → `_show_error`）补丁已经被撤掉了 —— 真的弹出一个模态框，
    整套用例就卡在 teardown 上直到超时（实测：卡在 `QMessageBox.exec()`）。
    session 作用域 + 手动 `MonkeyPatch` 之后，补丁从头到尾都在。

    两层：
    1. `ui.error_popup._exec`（唯一真正 `exec()` 的地方）换成"记一笔就返回" ——
       `show_error` 自己的逻辑（`once_key` 去重、正文收敛）照常执行；
    2. `QMessageBox` 的四个静态入口与 `exec` 也换掉，作为**最后一道网**：
       哪天有人新写了别处的弹窗、或者用例忘了打桩，也只会多一条记录，不会卡死
       （`question` 返回 Yes：子标题里的二次确认默认"确认"）。
    """
    from laoa_trader.ui import error_popup as error_popup_mod

    mp = pytest.MonkeyPatch()
    mp.setattr(error_popup_mod, "_exec", lambda box: 0)
    try:
        from PySide6.QtWidgets import QMessageBox
    except Exception:  # noqa: BLE001 - 没装 Qt 的用例本来也不会弹
        QMessageBox = None
    if QMessageBox is not None:
        mp.setattr(QMessageBox, "exec", lambda self: 0, raising=False)
        for name in ("warning", "critical", "information"):
            mp.setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0), raising=False)
        mp.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
            raising=False,
        )
    yield
    mp.undo()


def pytest_runtest_setup(item) -> None:      # noqa: ARG001 - pytest 钩子签名
    """每个用例**开始之前**清空弹窗记录 + 清 `once_key` 去重。

    为什么是钩子而不是 autouse 夹具（这一处踩过坑）：pytest 里 autouse 夹具**排在
    用例自己要的夹具之后**建立 —— 写成夹具时，"建窗口的夹具在 setup 阶段弹的那一次"
    会被随后的清零抹掉（实测：断言"启动自检弹了一次"永远看到空记录）。钩子在**任何夹具
    之前**跑，正好是"这条用例从头开始记"的位置。

    不清去重的话，第二条用例会因为"这类错误已经报过"而一条都弹不出来 ——
    表现是"用例时而红时而绿"，最难查。
    """
    from laoa_trader.ui import error_popup as error_popup_mod

    _MODAL_CALLS.clear()
    error_popup_mod.reset_once_keys()


@pytest.fixture(autouse=True)
def _record_modal_calls(monkeypatch: pytest.MonkeyPatch):
    """把 `ui.error_popup.show_error` 包一层：照常执行，只多记一笔"本该弹什么"。

    记录字段（`modal_calls` 夹具给用例）：
        `{"title", "text", "once_key", "shown"}` —— `shown=False` 表示被 `once_key`
        去重挡掉了（"同一类后台错误第二次不再弹"就断言这个）。
    """
    from laoa_trader.ui import error_popup as error_popup_mod

    real = error_popup_mod.show_error

    def _spy(parent, title, text, *, once_key="", **kw):
        shown = real(parent, title, text, once_key=once_key, **kw)
        _MODAL_CALLS.append({
            "title": str(title),
            "text": str(text),                              # 传进去的原文
            "shown_text": error_popup_mod._clip(text),      # **真正显示的那份**（≤3 行）
            "once_key": str(once_key),
            "shown": bool(shown),
        })
        return shown

    monkeypatch.setattr(error_popup_mod, "show_error", _spy)
    yield


@pytest.fixture()
def modal_calls():
    """本次测试里"本该弹出的模态框"记录：`[{title, text, once_key, shown}, …]`。"""
    return _MODAL_CALLS


@pytest.fixture()
def log_records():
    """收集 `laoa_trader` 日志（用来断言"日志里必须说清中文原因"）。

    为什么不直接用 pytest 的 `caplog`：`log.setup_logging()` 把 `laoa_trader`
    这个 logger 的 `propagate` 设成了 False（桌面版要自己写文件），
    记录不会冒泡到 root，`caplog` 就抓不到。这里直接挂一个 handler 上去。
    """
    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            records.append(record)

    logger = logging.getLogger("laoa_trader")
    handler = _Collect(level=logging.DEBUG)
    # logger 自身的级别也要放开：`log.setup_logging()` 未必在本用例里跑过，
    # 默认只有 WARNING 能过 —— 那样 INFO 级别的"为什么这样跑"就断言不到了
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def messages(records) -> str:
    """把收集到的日志拼成一段文本（断言用）。"""
    return "\n".join(r.getMessage() for r in records)


# ── 测试自己拼 TOML 时的路径转义：**全仓唯一一份实现** ──
# 为什么放在这里 re-export：写测试的人第一反应就是 `from tests.conftest import ...`，
# 而历史上正因为"每个文件各写一份 _p()"，CI 上连续漏了 5 个文件没转义
# （Windows 的 `C:\Users\...` 会让 tomllib 报 Invalid hex value，配置静默退回默认值）。
# 现在实现只有 `tests/_toml.py` 一份，这里只是把它摆到最顺手的入口上。
from tests._toml import escape, p, q, toml_str  # noqa: E402,F401
#                     ↑ 这四个就是给别的测试文件 import 用的（re-export），不是本文件自己要用


#: 让"小样本合成库"能通过自检的宽松阈值（真实默认值另有用例断言）
READY_THRESHOLDS = (
    "min_history_years = 0\n"
    "min_symbols = 1\n"
    "max_stale_trading_days = 0\n"
)


def workdays_ending(day: str, count: int) -> list[str]:
    """截至 `day`（含）的 count 个工作日，升序（跳过周末）。"""
    last = datetime.strptime(day, "%Y-%m-%d").date()
    out: list[str] = []
    cursor = last
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def seed_ready_db(
    cfg: Config,
    symbols: tuple[tuple[str, str, str], ...] = (
        ("600001", "低价样本", "银行"),
        ("600002", "高价样本", "白酒"),
    ),
    days: int = 30,
    *,
    with_events: bool = True,
    trading_days: list[str] | None = None,
) -> list[str]:
    """把库写成**自检判定为 ready** 的状态，返回交易日列表。

    自检要求：行情非空 + 跨度达标 + 股票数达标 + **有复权事件** + 行业覆盖 ≥90%
    + 交易日历非空 + 不落后。小样本库靠 `READY_THRESHOLDS` 放低跨度/股票数门槛，
    其余各项都要如实造出来，否则测出来的就是"自检失败"而不是被测功能。

    Args:
        with_events: 是否写复权事件（False 用来构造"缺复权事件 → needs_full"）。
    """
    storage.init_db(cfg.db_path)
    trading = list(trading_days) if trading_days else _trading_days(days)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [(s, n, ind) for s, n, ind in symbols])
        rows = []
        for i, (symbol, _name, _ind) in enumerate(symbols):
            for d_i, day in enumerate(trading):
                close = 10.0 + i + d_i * 0.01
                rows.append((symbol, day, close, close, close, close, 2e7, 2e7 * close, 1.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, trading)
        if with_events:
            storage.write_adjust_events(conn, [
                (symbols[0][0], trading[len(trading) // 2], 0.1, 0.0, 0.0, 0.0),
            ])
    return trading


@pytest.fixture()
def ready_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """一份**已就绪**的库（自检 ready），CLI 用例直接拿它跑正题。"""
    for name in ("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY",
                 "MIN_HISTORY_YEARS", "MIN_SYMBOLS", "MAX_STALE_TRADING_DAYS",
                 "AUTO_DOWNLOAD_ON_START"):
        monkeypatch.delenv(name, raising=False)
    config = Config(data_dir=tmp_path / "data", hithink_api_key="")
    config.min_history_years = 0.0
    config.min_symbols = 1
    config.ensure_dirs()
    seed_ready_db(config, days=40)
    return config


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """临时配置：数据目录在 tmp_path 下，三路通知全关（测试不发真实通知）。"""
    # 清掉可能存在的宿主环境变量，避免"环境变量覆盖"把测试搞乱
    for name in (
        "HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY",
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID",
        "NOTIFY_FEISHU", "NOTIFY_WINDOWS", "NOTIFY_TRAY",
        "TRADE_CAPITAL", "TRADE_POSITION_PCT", "TRADE_MAX_POSITIONS",
        "INTRADAY_STOP_LOSS", "INTRADAY_TAKE_PROFIT", "INTRADAY_POOL_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = Config(data_dir=tmp_path / "data", hithink_api_key="test-key")
    # 三路频道**显式**清空：默认值现在是 `[]`（用户拍板：默认只走自绘浮窗），
    # 这里再写一遍是因为这是"测试绝不发真实通知"这条保证的输入 ——
    # 显式写死之后，就算将来默认值又改回三路，测试也不会突然真的往外发。
    # 想验发送的用例自己 `cfg.notify_channels = list(KINDS)`（见 test_notify.py）。
    config.notify_channels = []
    config.ensure_dirs()
    return config


def _trading_days(count: int, end: str | None = None) -> list[str]:
    """生成 count 个"工作日"日期（跳过周末，不查真实日历）。"""
    end_date = datetime.strptime(end or "2026-09-11", "%Y-%m-%d").date()
    days: list[str] = []
    cursor = end_date
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(days)


@pytest.fixture()
def trading_days() -> list[str]:
    return _trading_days(40)


@pytest.fixture()
def db(cfg: Config, trading_days: list[str]) -> str:
    """合成一个可用的行情库。

    数据设计（刻意让 5 条策略都能出候选）：
    - 6 只股票分属 3 个行业；半导体 2 只当日涨停（用来验证"热门行业"排序）；
    - 价格走势：`600001` 一路阴跌（满足短期反转）、`000001` 低位横盘（满足低价股）；
    - 成交量：`300001` 前 3 日地量、最后一日放量（满足地量后放量）。
    """
    path = storage.init_db(cfg.db_path)
    symbols: dict[str, tuple[str, str, float]] = {
        "600001": ("浦发样本", "银行", 3.0),
        "600002": ("半导体甲", "半导体", 12.0),
        "600003": ("半导体乙", "半导体", 18.0),
        "000001": ("平安样本", "银行", 6.0),
        "300001": ("创业样本", "白酒", 25.0),
        "000002": ("地产样本", "房地产", 9.0),
    }
    with storage.connect(path) as conn:
        storage.write_stock_basic(
            conn,
            [(s, meta[0], meta[1]) for s, meta in symbols.items()],
        )
        symbols_first = next(iter(symbols))
        rows = []
        for i, (symbol, (_, _, base)) in enumerate(symbols.items()):
            price = base
            for d_i, day in enumerate(trading_days):
                # 每只股票不同的走势：600001 缓慢阴跌（-1.2%/日），其余微涨
                drift = -0.012 if symbol == "600001" else 0.001
                price = price * (1 + drift)
                volume = 1_000_000.0
                volume *= 1.0 + (i * 0.1)
                if symbol == "300001":
                    # 前 3 日地量、最后一日 3 倍放量
                    if d_i >= len(trading_days) - 4:
                        volume = 200_000.0 if d_i < len(trading_days) - 1 else 3_000_000.0
                rows.append((
                    symbol, day, price * 0.99, price * 1.01, price * 0.98, price,
                    volume, volume * price,
                ))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, trading_days)
        last = trading_days[-1]
        storage.write_limit_up_pool(conn, [
            (last, "600002", "半导体甲", 1, "首板", "09:35:00", "09:35:00",
             8e7, 0, "芯片", 5.0, 10.0, 1e9, 0, 1, "首板", 9e7, 12.0, 0, "hithink", "t"),
            (last, "600003", "半导体乙", 2, "二连板", "09:31:00", "09:45:00",
             6e7, 1, "芯片", 6.0, 10.0, 2e9, 1, 0, "2连板", 7e7, 18.0, 0, "hithink", "t"),
            (last, "600001", "浦发样本", 1, "首板", "10:10:00", "10:20:00",
             3e7, 0, "银行", 1.0, 10.0, 5e8, 0, 1, "首板", 3e7, 3.0, 0, "hithink", "t"),
        ])
        # 让合成小库也能通过"运行时自检"（否则调度/界面会因为数据闸门而拒绝跑策略）：
        # 补一条**零效果**复权事件 —— 只为满足"复权事件非空"这一条判据。
        # 0 送股/0 配股/0 现金分红 ⇒ 因子恒等于 1，**不改变任何策略输入**
        # （用真实的送配股事件会整体缩放后复权价，可能影响其它用例的筛选结果）。
        storage.write_adjust_events(conn, [(symbols_first, trading_days[0], 0.0, 0.0, 0.0, 0.0)])
    # 小样本库（6 只股票 / 40 个交易日）远低于默认门槛 4000 只、4.5 年 ——
    # 放低门槛，让它按"真实小库"参与后续流程；其余判据（行业覆盖/复权事件/日历）
    # 都是**如实造出来**的，不是为了绕过检查
    cfg.min_symbols = 1
    cfg.min_history_years = 0.0
    return str(path)


@pytest.fixture()
def engine(db: str) -> DataEngine:
    return DataEngine(db)


class FakeResponse:
    """最小可用的响应对象（requests.Response 的替身）。"""

    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise hx.requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """假的 requests.Session：按 URL 关键词返回预置响应，并记录调用。"""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    def _resolve(self, url: str, params: dict | None):
        for key, value in self.routes.items():
            if key in url:
                if callable(value):
                    return value(params or {})
                return value
        return FakeResponse({"code": 0, "data": {"item": []}})

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        return self._resolve(url, params)

    def post(self, url, json=None, data=None, headers=None, timeout=None, **kwargs):
        # 飞书接口用 data=<JSON 字符串> 提交（不是 json=），测试里要能看到真实载荷
        payload = json
        if payload is None and isinstance(data, (str, bytes)):
            try:
                payload = __import__("json").loads(data)
            except ValueError:
                payload = {"_raw": data}
        self.calls.append((url, dict(payload or {})))
        return self._resolve(url, payload)


class FakeClient:
    """假的同花顺客户端（用于 sync / intraday 的离线测试）。

    **它抛出的所有错误都是合成的**（消息里带"测试假客户端"字样），
    日志里看到 "4001/5001/限流" 之类的字样是测试在模拟服务端错误，
    不是真的打到了同花顺接口 —— socket 层已被 `_block_network` 封死。

    可编程：
    - `snapshots`：snapshot() 返回的行情快照；
    - `limit_up`：limit_up_pool() 返回的涨停池；
    - `dump_files`：{tag: 本地 parquet 路径} —— download_dump() 直接"复制"过来，
      这样既模拟了真实下载（含 force 重下路径），又不需要联网；
    - `fail_with`：任何调用都抛这个异常（测"失败要返回结构化结果"）。
    """

    def __init__(
        self,
        snapshots: list[dict] | None = None,
        limit_up: list[dict] | None = None,
        fail_with: Exception | None = None,
        trading_days: list[str] | None = None,
        dump_files: dict[str, Path] | None = None,
    ) -> None:
        self.snapshots = snapshots or []
        self.limit_up = limit_up or []
        self.fail_with = fail_with
        self.trading = trading_days
        self.dump_files = dump_files or {}
        self.calls: list[tuple[str, object]] = []

    def _check(self, name: str, arg=None):
        self.calls.append((name, arg))
        if self.fail_with is not None:
            raise self.fail_with

    def snapshot(self, thscodes=None, **kwargs):
        self._check("snapshot", thscodes)
        return [s for s in self.snapshots if not thscodes or s.get("ticker") in thscodes]

    def limit_up_pool(self, day=None, size=200):
        self._check("limit_up_pool", day)
        return self.limit_up

    def trading_days(self):
        self._check("trading_days")
        return self.trading or []

    def ths_index_list(self, tag="industry"):
        self._check("ths_index_list", tag)
        return []

    def ths_constituents(self, thscode):
        self._check("ths_constituents", thscode)
        return []

    def index_historical(self, thscode, start, end, interval="1d"):
        self._check("index_historical", thscode)
        return []

    def tickers(self, **kwargs):
        self._check("tickers")
        return []

    def valuations(self, symbols, chunk=100):
        self._check("valuations", symbols)
        return []

    def download_dump(self, tag="daily-k-10d", dest=None, **kwargs):
        """模拟下载：把预置的 parquet 复制过去。

        `**kwargs` 吸收真实实现的那堆参数（progress_cb / max_attempts / 超时 /
        should_stop）—— 测试只关心"文件到位"，但签名必须兼容，否则会掩盖真实调用。
        """
        self._check("download_dump", tag)
        source = self.dump_files.get(tag)
        if source is None:
            raise AssertionError(
                f"测试未注入 {tag} 的 dump 文件（请给 FakeClient 传 dump_files）"
            )
        import shutil

        # 没给 dest 时落到系统临时目录（**别写死 /tmp**：Windows 上 Path("/tmp")
        # 是"当前盘符根目录下的 \\tmp"，行为不一样；真实实现也用 gettempdir）
        target = Path(dest) if dest else Path(tempfile.gettempdir()) / f"{tag}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        progress_cb = kwargs.get("progress_cb")
        if callable(progress_cb):
            size = target.stat().st_size
            progress_cb(size // 2, size)
            progress_cb(size, size)
        return target


@pytest.fixture()
def sqlite_conn(db: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_license_state(tmp_path_factory, monkeypatch):
    r"""每个用例都用一份**干净的授权状态**（默认：刚装上、试用第 1 天）。

    为什么必须隔离：授权状态文件在 `%APPDATA%\LuweikDecision\license.json`（开发机上
    也可能真的存在）。不隔离的话，**测试行为会跟着开发机/CI 机器上那份文件变**：
    那里写着"试用到期"时，每个建 `MainWindow` 的用例都会多弹一个授权窗口，
    定时器与窗口数跟着变（这类差异最难的是一开始就看不出来 —— 本机全绿、CI 上偶发崩）。
    用 monkeypatch 指到 tmp_path，pytest 结束自动还原。
    """
    from laoa_trader import clock, licensing as licensing_mod

    # ⚠️ 用 `tmp_path_factory`（会话临时根下的独立目录）而不是 `tmp_path`：
    # 后者是本用例的"工作目录"，有些用例会断言"这个目录里现在只有哪几个文件"
    # （桌面导出、公式保存各有一条），往里塞东西会把它们弄红 —— 实测踩过。
    path = tmp_path_factory.mktemp("license-state") / "license.json"
    today = clock.today_cn()
    path.write_text('{"first_run": "%s", "max_seen": "%s"}' % (today, today),
                    encoding="utf-8")
    # 注意：这里刻意**只**隔离授权状态文件。数据库那份（`app_state` 表）跟着各用例
    # 自己的 `cfg.db_path` 走 —— 它们是 tmp 目录里的独立库，不会互相串。
    monkeypatch.setattr(licensing_mod, "state_path", lambda: path)
    yield


@pytest.fixture(autouse=True)
def _never_touch_the_real_user_config(tmp_path, monkeypatch):
    """整场不许碰**这台机器上真实的**用户配置（`~/.config/luweik-decision/config.toml`）。

    为什么必须（2026-10-08 实测踩到）：界面里有几处**自动保存** —— 勾选策略
    （`formula_page` 存 `enabled_formulas`）、拖桌宠记坐标（`pet_x/pet_y`）、
    《系统设置》的【保存设置】—— 它们最终都走 `config.update_config_file(cfg.source_path)`。
    而用例里的 `Config(...)` 大多是 `source_path=None`，于是**回落到真实用户位置**：
    跑一遍界面用例，家目录里就多出一份 `config.toml`（里面写着测试用的
    `hithink_api_key = "test-key"`），紧接着 `test_config.py::test_defaults`
    因为读到那份文件而红 —— 测试与开发机的状态就这么串起来了。
    在**离线/CI** 机器上它还会顺手改掉人家真实在用的配置（那份文件是用户自己填的 Key）。

    做法有两道，都在这一条夹具里：
      ① 把 `LUWEIK_CONFIG` 指到本用例 tmp 目录下一个**不存在的**路径 ——
         "谁都没配过" 的语义没变（读不到就是默认值），但 `find_config_file()`
         找的就是这里；**用用例级 `tmp_path` 而不是会话级**：否则前一个用例写下的
         内容会串给后一个（那正是上面那条红）；
      ② 包一层 `config.update_config_file`：`path=None`（或恰好等于真实用户位置）时
         把落点改成 tmp 里那个文件。**这一道才是真正兜住的** —— `None` 在函数内部
         还会回落到 `user_config_path()`，光设环境变量拦不住（实测：设了之后
         `update_config_file(None, …)` 照样写进家目录）。
    收尾再加一道**断言**：这一条用例之前真实位置不存在、跑完却存在了 → 直接判失败。
    这类"悄悄改了宿主机状态"的 bug 只有在它大声报错时才可能被修掉。

    注意：不 patch `config.user_config_path` 本身 —— `test_config_migration.py` 那几条
    正是靠它（以及里面的老目录迁移）来验行为的，动了它就把那些用例的输入改了。
    """
    from laoa_trader import config as config_mod

    real = config_mod.user_config_path()
    existed_before = real.exists()
    isolated = tmp_path / "user-config.toml"
    original = config_mod.update_config_file

    def _guarded(path=None, updates=None, *, create=True):
        target = isolated if path is None else Path(path)
        if target == real:
            target = isolated
        return original(target, updates or {}, create=create)

    monkeypatch.setenv("LUWEIK_CONFIG", str(isolated))
    monkeypatch.setattr(config_mod, "update_config_file", _guarded)
    # 默认**数据目录**也一起收到 tmp 里（`%LOCALAPPDATA%\LuweikDecision\data` /
    # `$XDG_DATA_HOME/LuweikDecision/data`）：跑 `--doctor` 的用例最后会把
    # `自检报告.txt` 落到那个目录（见 `__main__` 的 `_Tee`），
    # 在 CI 上那份报告还会被归档步骤抓去当"编译产物自检报告"——
    # 2026-10-08 实测：ci-log 里那份写着「运行形态：源码运行」，看日志的人
    # 会以为编译版退回了源码形态（真正那份在 `ci-smoke-data/logs/` 下）。
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    # APPDATA 同理（`user_config_path()` 的 Windows 分支 + 老目录迁移的落点）：
    # 用例要验迁移路径时自己 `monkeypatch.setenv`，会覆盖这一条（见 test_config_migration.py）。
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    yield
    if not existed_before and real.exists():
        # 不删它（删掉就把证据毁了），直接判失败并告诉人文件在哪
        pytest.fail(f"这条用例把**真实用户配置**写出来了：{real}"
                    "（界面里的自动保存必须落在 tmp 目录，见本夹具说明）")


@pytest.fixture(autouse=True)
def _never_speak_for_real(monkeypatch: pytest.MonkeyPatch):
    """整场把**朗读**这一路钉成"不发声、不起进程、不起线程"。

    为什么必须（2026-09-21，CI 三次红在同一个地方）：`voice` 模块在 **Windows** 上
    真的可用（那里有 PowerShell），于是它会：① 起一个 `powershell` 子进程枚举音色；
    ② 起一条**永远堵在队列上**的守护线程等朗读。Linux 上没有 PowerShell，所以这两个
    副作用在本机（以及之前的本地全量）**永远看不到** —— 而 CI 是 Windows，于是那条
    线程可能活到 pytest 收尾之后：那时 `sys.stderr` 已经关了，它再写一句日志就是
    `I/O operation on closed file`，再往后整个进程中止（日志停在半路、没有任何用例失败记录）。

    生产代码那边也做了两道（`MainWindow.shutdown()` 会 `voice.shutdown()` 送哨兵、
    `log.py` 的 handler 对"流已关闭"免疫），这里是最外面一道：**测试根本不发声**。
    要验"念的是什么"的用例自己 monkeypatch `run_command`/`_list_voices_raw` 即可。
    """
    try:
        from laoa_trader.notify import voice as voice_mod
    except Exception:      # noqa: BLE001 - 极小依赖环境
        return
    monkeypatch.setattr(voice_mod, "run_command", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(voice_mod, "_list_voices_raw",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(voice_mod, "_powershell", lambda: None, raising=False)
    voice_mod.reset_cache()
    yield
    # 收尾：把朗读线程叫停（送哨兵 + join），不让它跨用例存活
    try:
        voice_mod.shutdown()
    except Exception:      # noqa: BLE001 - 收尾失败不该把用例带崩
        pass


@pytest.fixture(autouse=True)
def _close_orphan_top_level_windows():
    """每个用例收尾：把**没有父窗口**的顶层窗口（桌宠 / 消息列表 / 浮窗）显式关掉。

    为什么需要兜底：这几只都是无父窗口的顶层窗口，`deleteLater()` 收不掉它们 ——
    用例里建了主窗口却忘了收（或只 `close()` 了主窗口）时，它们会留在事件循环里，
    定时器继续跑、窗口继续重绘，某一次就会踩到已销毁的 C++ 对象，让**整个 pytest
    进程中途 Aborted**（实测：连建三次主窗口 → 三只桌宠残留 → 下一次全量崩在 25% 处）。
    生产代码的退出路径（`MainWindow.shutdown`）负责正常收尾，这里只是测试侧的安全网。
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:      # noqa: BLE001 - 没装 Qt 的极简环境
        return
    app = QApplication.instance()
    if app is None:
        return
    # ── 先收**主窗口**：它是所有后台线程的源头 ──
    # `MainWindow.shutdown()` 现在会把 `quotes` / `scheduler` / `_market_worker` / `_worker`
    # 四条线程与桌宠/消息列表/浮窗/对话框一起收掉（见它的 docstring）。为什么要在这层兜底：
    # 测试里只要有一条用例建了主窗口又漏了收尾，那条线程就会活到**下一个用例甚至解释器退出**,
    # 在 Windows 上表现为随机顺序下跑到中途 `Fatal Python error: Aborted`（CI 实测：
    # 两次都停在 93% 附近、没有任何用例失败记录，日志里只剩"往已关闭的日志流里写"）。
    # 让每条用例自己记得收是不可靠的，所以这里统一兜一层。
    try:
        from laoa_trader.ui import app as ui_app

        window_cls = getattr(ui_app, "MainWindow", None)
        if window_cls is not None:
            for widget in list(QApplication.topLevelWidgets()):
                if isinstance(widget, window_cls):
                    try:
                        widget.shutdown()
                    except Exception:  # noqa: BLE001 - 兜底收尾失败不该把用例带崩
                        pass
    except Exception:      # noqa: BLE001 - 没装 Qt / 导入失败时安静跳过
        pass
    for widget in list(QApplication.topLevelWidgets()):
        name = type(widget).__name__
        if name not in ("DesktopPet", "MessageCenter", "AlertPopup", "LicenseDialog"):
            continue
        try:
            shutdown = getattr(widget, "shutdown", None)
            if callable(shutdown):
                shutdown()
            widget.close()
            widget.deleteLater()
        except Exception:  # noqa: BLE001 - 兜底收尾失败不该把用例带崩
            pass
    app.processEvents()
    # ── 最后一道：**所有**登记在册的工作线程都必须落地 ──
    # 为什么不能只靠上面那几个属性名：漏一个名字就等于漏一条线程，而漏掉的后果是
    # CI 上"跑到半路中止、没有任何失败记录"（`Worker._live` 的注释里写了同源事故）。
    try:
        from laoa_trader.ui import app as ui_app

        worker_cls = getattr(ui_app, "Worker", None)
        if worker_cls is not None:
            worker_cls.wait_all(1.0)
    except Exception:      # noqa: BLE001 - 没装 Qt / 导入失败时安静跳过
        pass


@pytest.fixture(autouse=True)
def _no_real_desktop_export(tmp_path, monkeypatch):
    """测试期间**绝不往真桌面写文件**（筛选结果导出会落在桌面）。

    为什么必须有：`scheduler.run_daily()` 建池成功后会往**桌面**导出一份结果文本
    （用户要求）。测试里大量调用 `run_daily`（pipeline / cli / scheduler_gate …），
    它们多数不传 `export_dir` —— 在开发者本机（尤其 Windows CI 的 runner，那里
    `~/Desktop` 是真实存在的目录）就会真的在桌面上落一个文件，而且**同名同日互相覆盖**。
    这种副作用不会让测试变红，所以最容易被忽略：直到有一天发现"桌面上怎么多了个文件"。

    这里把桌面目录固定到临时目录：显式传 `export_dir` 的用例不受影响（它们本来就注入），
    没有显式注入的那些则写进 `tmp_path/desktop-export`，跑完即随 tmp_path 消失。
    """
    try:
        from laoa_trader import pool
    except Exception:      # noqa: BLE001 - 极小依赖环境下没有这个模块就跳过
        return
    target = tmp_path / "desktop-export"
    real_export = getattr(pool, "export_pick_file", None)
    if real_export is None:      # 老版本没有这个函数（或将来改名了）
        return

    def _guarded(*args, **kwargs):
        # 只拦"调用方没指定目录"的那种：显式传了 dest_dir 的用例保持真实行为，
        # 也就不影响 `desktop_dir()` 自己的解析用例（它们直接调那个函数，没被替换）。
        kwargs.setdefault("dest_dir", target)
        return real_export(*args, **kwargs)

    monkeypatch.setattr(pool, "export_pick_file", _guarded, raising=False)
