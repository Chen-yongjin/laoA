"""编译产物的**图形界面**冒烟自检（`--gui-selftest`）—— 钉住它的三条契约。

为什么单独一个文件
------------------
2026-10-13 主人实报：编译出来的 1.9.2 双击打不开 —— `luweik决策系统.exe --cli --version`
一切正常（打印版本号），一进图形界面就 Nuitka 段错误；而当时的 CI 冒烟只跑 `--version` /
`--doctor`（**全程不碰 GUI**），所以"只在编译版、只在界面路径上出现"的崩溃一路绿灯发到了
用户手里。`--gui-selftest` 就是补这个窟窿的那条命令，这里钉住三件事：

1. 真把主窗口建出来、跑一拍之后**退出码 0**，并且报告文件里那行 OK 真的写了
   （CI 就是读那个文件判定"界面真的建出来了"的）；
2. 自检里**绝不弹模态框**（无人值守时一个框就是永久卡死：CI 上是超时，用户看到的是"卡住"）
   —— 判据是 `ui/error_popup.py::_exec` 这个**唯一接缝**有没有被碰过；
3. 界面建不起来时**退出码非 0**（CI 靠它变红，不能"看起来跑完了"）。

外加两条边界：不给 `--config` 时必须落到**临时目录**（绝不碰用户真实的数据目录/配置），
以及"静音"开关用完必须还原（它是进程级的，忘了还原等于把界面上所有错误提示变成只写日志）。

还有一条**定位链**：主窗口初始化每一步都要往日志里刷一行 `启动步骤：…`
（`ui/app.py::startup_step()`）—— 段错误没有 traceback，崩溃时"日志停在哪一行"就是唯一的现场
（1.9.2 那次现场只有"主题已应用 / 滚轮防误触"两行，谁也没法定位）。这条链子被删掉的话，
下一次再崩就又要靠猜，所以在这里钉住。

界面部分用 Qt 的 `offscreen` 平台在无显示器环境里跑（与 `test_ui_smoke.py` 同一套做法）。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from laoa_trader import __main__ as main_mod  # noqa: E402
from laoa_trader.__main__ import (  # noqa: E402
    GUI_SELFTEST_MIN_WIDGETS,
    GUI_SELFTEST_REPORT_NAME,
    cli,
)

from tests._toml import p  # noqa: E402


@pytest.fixture()
def selftest_config(tmp_path, cfg):
    """指向临时数据目录的 config.toml —— 与 CI 那一步写法一致（只写 data_dir）。"""
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{p(cfg.data_dir)}"\n', encoding="utf-8")
    return str(path)


def _report_path(data_dir) -> Path:
    """自检报告落在哪（CI 与用户报障读的都是这一份）。"""
    return Path(data_dir) / "logs" / GUI_SELFTEST_REPORT_NAME


# ── ① 建出真窗口并退出 0 ──


def test_gui_selftest_builds_a_real_window_and_exits_zero(capsys, cfg, selftest_config) -> None:
    """自检模式：真主窗口建出来、跑一拍、正常退出 —— 并留下那句 "GUI 自检 OK"。"""
    assert cli(["--gui-selftest", "--config", selftest_config]) == 0
    out = capsys.readouterr().out

    match = re.search(r"GUI 自检 OK：窗口 (\d+) 个控件、版本 (\S+)", out)
    assert match, f"自检没有打印那句 OK（输出如下）：\n{out}"
    # 控件数要像个真界面（界面没搭起来时不该报 OK）
    assert int(match.group(1)) >= GUI_SELFTEST_MIN_WIDGETS
    import laoa_trader

    assert match.group(2) == laoa_trader.__version__

    # CI 读的就是这份文件（GUI 子系统的 exe，重定向 stdout 不一定拿得到内容）
    report = _report_path(cfg.data_dir)
    assert report.is_file(), f"自检报告没写出来：{report}"
    text = report.read_text(encoding="utf-8")
    assert "GUI 自检 OK" in text
    assert "平台插件  : QT_QPA_PLATFORM=offscreen" in text       # 离屏是它自己在进程里设的
    assert "Traceback" not in text


# ── ② 绝不弹模态框 ──


def test_gui_selftest_never_pops_a_modal(monkeypatch, capsys, selftest_config, modal_calls) -> None:
    """自检期间**一次都不许**碰弹窗那条唯一接缝（碰了就是"没人能点掉的框"）。

    判据用 `error_popup._exec`：它是全项目唯一真正 `exec()` 的地方（见那个模块的说明），
    所以"有没有弹"这一件事只有它说了算。
    """
    from laoa_trader.ui import error_popup

    frames: list = []
    monkeypatch.setattr(error_popup, "_exec", lambda box: frames.append(box) or 0)

    assert cli(["--gui-selftest", "--config", selftest_config]) == 0
    capsys.readouterr()

    assert frames == [], "自检里弹出了模态框（CI 上就是卡到超时）"
    # conftest 的记录（`shown=True` = 真的走完了弹窗那一套）：一条都不许有
    assert not [call for call in modal_calls if call["shown"]], modal_calls
    # 开关是进程级的：用完必须还原，否则后面所有错误提示都变成只写日志
    assert error_popup.is_silent() is False


def test_silent_mode_is_log_only(monkeypatch, modal_calls) -> None:
    """`set_silent(True)` 的语义：只记日志、返回 False、**不碰** `_exec`。

    为什么要和上面那条分开：那条证明"走自检这条路时没弹"；这条证明"静音这个机制本身有效"
    —— 包括**对照组**（不静音时确实会去建框并调到唯一接缝），否则"没碰"可能只是因为
    压根没走到那条分支（没有 QApplication 时 `show_error` 本来就会提前返回）。
    """
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])      # 没有 app 时那条分支会提前返回

    from laoa_trader.ui import error_popup

    frames: list = []
    monkeypatch.setattr(error_popup, "_exec", lambda box: frames.append(box) or 0)

    # 对照组：不静音 → 真的建了框并调到唯一接缝
    assert error_popup.show_error(None, "对照", "这条应该弹出来") is True
    assert len(frames) == 1

    previous = error_popup.set_silent(True)
    try:
        assert error_popup.show_error(None, "静音", "这条只该进日志") is False
    finally:
        error_popup.set_silent(previous)

    assert len(frames) == 1, "静音模式下仍然弹了框"
    assert error_popup.is_silent() is False          # 还原了
    # 只记日志那条也要留下痕迹（否则"出错时什么都看不到"）
    assert [call for call in modal_calls if call["title"] == "静音"]


# ── ③ 失败必须非 0 ──


def test_gui_selftest_returns_nonzero_when_the_window_blows_up(
    monkeypatch, capsys, cfg, selftest_config
) -> None:
    """主窗口构造抛异常 → 打印 traceback 且退出码非 0（CI 靠这个变红）。"""
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import error_popup

    def _boom(self, cfg=None):
        raise RuntimeError("测试制造：主窗口构造炸了")

    monkeypatch.setattr(ui_app, "MainWindow", _boom)
    code = cli(["--gui-selftest", "--config", selftest_config])
    captured = capsys.readouterr()

    assert code != 0, "界面建不出来却报了成功 —— CI 就再也拦不住这类崩溃了"
    assert "GUI 自检失败" in captured.out
    assert "RuntimeError" in captured.out and "Traceback" in captured.out
    assert "Traceback" in captured.err                 # CI 的 gui-selftest.err.txt 那份
    # 现场要留在报告里（界面崩了的时候，报告是唯一一定写得出来的东西）
    report = _report_path(cfg.data_dir)
    assert report.is_file() and "GUI 自检失败" in report.read_text(encoding="utf-8")
    assert error_popup.is_silent() is False            # 失败路径也必须还原开关


def test_gui_selftest_refuses_when_the_given_config_is_not_honored(capsys, tmp_path) -> None:
    """`--config` 指定了却没生效 → **拒绝运行**（非 0），绝不许悄悄跑到默认数据目录上去。

    为什么这条要紧：`load_config` 读不出来时会**静默退回默认值**，而默认数据目录正是
    **用户真实那一个**（自检会往里建库、写日志）。CI 就踩过它的孪生形态：config.toml 带
    BOM 时第一个键名变成 `\\ufeffdata_dir`、被当成未知键忽略，数据目录于是变成了
    `%LOCALAPPDATA%\\LuweikDecision\\data` —— 报告写到了别处，还差点被当成"编译版退回了源码形态"。
    """
    from laoa_trader import config as config_mod

    real_default = config_mod.default_data_dir()
    existed_before = real_default.exists()

    # ① 带 BOM：TOML 解析失败 → 静默退回默认数据目录（上面那次事故的形态）
    bom = tmp_path / "bom.toml"
    bom.write_bytes('\ufeffdata_dir = "/tmp/不该被用到"\n'.encode("utf-8"))
    # ② 键名写错：文件读得进来，但 data_dir 没生效，照样落到默认数据目录
    no_dir = tmp_path / "nodata.toml"
    no_dir.write_text('hithink_api_key = ""\n', encoding="utf-8")
    # ③ 路径压根不存在
    for bad in (bom, no_dir, tmp_path / "没有这个文件.toml"):
        assert cli(["--gui-selftest", "--config", str(bad)]) != 0, f"没拦住：{bad}"

    out = capsys.readouterr().out
    assert "GUI 自检拒绝运行" in out
    assert "用户真实目录" in out

    # 真实（默认）数据目录自始至终没被碰过 —— 它才是这条守卫要保住的东西
    assert real_default.exists() == existed_before, \
        f"自检往默认数据目录里写了东西：{real_default}"


# ── ④ 不给 --config 时：临时目录，绝不碰用户真实目录 ──


def test_gui_selftest_without_config_never_touches_the_real_data_dir(
    monkeypatch, capsys
) -> None:
    """`--gui-selftest` 不带 `--config` 时用**临时数据目录**，连真实配置都不读。

    为什么必须这样：自检会建库、写日志，界面上还可能回写配置 —— 一条"冒烟命令"
    不该在用户的真实数据目录里留下任何东西（主人 2026-10-13 的硬要求）。
    """
    from laoa_trader import config as config_mod

    def _explode(*args, **kwargs):
        raise AssertionError("GUI 自检不许读用户的 config.toml / 真实数据目录")

    # `cli()` 里那条"没给 --config 就用 get_config()"的路必须**不被走到**
    monkeypatch.setattr(main_mod, "get_config", _explode)
    real_default = config_mod.default_data_dir()
    existed_before = real_default.exists()

    # 走**真正的入口**（`main()` 里那条"先分流再加载配置"的分支），不只是 cli()
    assert main_mod.main(["--gui-selftest"]) == 0
    out = capsys.readouterr().out

    match = re.search(r"数据目录  : (.+)\n", out)
    assert match, out
    data_dir = Path(match.group(1).strip())
    try:
        assert data_dir.is_dir()
        # 落在系统临时目录下（`mkdtemp` 就是在 `gettempdir()` 里建的），且带自检前缀
        assert data_dir.parent == Path(tempfile.gettempdir())
        assert data_dir.name.startswith("luweik-gui-selftest-")
        assert _report_path(data_dir).is_file()
        assert "GUI 自检 OK" in out
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    # 真实（默认）数据目录自始至终没被碰过
    assert real_default.exists() == existed_before, \
        f"自检往默认数据目录里写了东西：{real_default}"


# ── ⑤ 崩溃定位链：每一步都要留下 `启动步骤：…` ──


def test_every_startup_step_leaves_a_marker(log_records, cfg) -> None:
    """主窗口初始化每一步都写一行 `启动步骤：…`（段错误时**最后一行**就是崩溃点）。

    为什么这条要在测试里钉住（2026-10-13 的 1.9.2 实报）：编译版双击打不开，日志停在
    `界面主题已应用：indigo` + `已启用滚轮防误触…` 两行之后**一个字都没有** ——
    段错误不给 traceback，于是"它到底崩在屏幕几何、搭界面、托盘、桌宠还是调度器上"
    完全没法判断，只能靠一遍遍改代码重编再试。有了这串标记，CI 的 `smoke.log`
    与用户发回来的 `luweik.log` 尾巴就能直接指出崩在哪一步。

    这里顺带把"标记不能只在自检模式下写"钉住：它走的是**生产路径**（普通双击也写），
    否则用户那边崩了、我们手上那份日志还是什么都没有。
    """
    from PySide6.QtWidgets import QApplication

    from laoa_trader.__main__ import _teardown_selftest_window
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import error_popup

    app = QApplication.instance() or QApplication([])
    # 建窗口时会走到"启动自检失败"那条弹窗路 → 静音（这里不测弹窗，别让它卡住）
    previous_silent = error_popup.set_silent(True)
    window = None
    try:
        window = ui_app.MainWindow(cfg)
        window.show()
        app.processEvents()
    finally:
        error_popup.set_silent(previous_silent)
        if window is not None:
            _teardown_selftest_window(window, app)

    text = "\n".join(record.getMessage() for record in log_records)
    steps = [line.split("启动步骤：", 1)[1] for line in text.splitlines()
             if "启动步骤：" in line]
    assert steps, f"一条启动步骤标记都没有：\n{text}"
    # 顺序也要对（顺序错了说明标记插错了地方 —— 定位就会指到错误的一步）
    for expected in ("主题已应用", "滚轮防误触已装好", "屏幕几何 OK", "界面 OK", "托盘 OK",
                     "桌宠 OK", "调度器 OK", "授权检查 OK", "快照与评分服务 OK",
                     "定时器 OK", "首次 _tick OK", "主窗口构造完成"):
        assert any(step.startswith(expected) for step in steps), \
            f"启动步骤里缺「{expected}」：{steps}"
    assert steps[-1].startswith("主窗口构造完成"), f"最后一步应当是构造完成：{steps}"
