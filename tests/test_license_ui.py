"""授权界面：机器码展示、注册码输入与注册、锁「策略编辑」、到期提醒。

用户 2026-09-20 拍板的三条，在界面这一层各有对应：

* 「策略编辑锁住，点击提醒需要授权，请联系作者wx：q352162」
  → 未授权时点【策略编辑】**不打开编辑器**，弹出授权对话框（`test_editor_is_locked_*`）；
* 「免费运行7天，到期打开同样授权提醒。」
  → 到期时启动自动弹一次，而且**只弹一次**（`test_expired_launch_prompts_once`）；
* 「机器码下面加上注册码输入口和注册按键，点击可以注册。」
  → 对话框里机器码在注册码输入框**上面**，点【注册】当场生效（`test_dialog_*`）。

界面只读 `licensing.license_status()`，这些用例也照这个口径断言（不重复实现一套判定）。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from laoa_trader import clock, licensing as L

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过授权界面测试")

from laoa_trader.ui import app as ui_app  # noqa: E402


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定一份假指纹：机器码在测试里必须稳定（真硬件指纹会让断言跟着机器跑）。"""
    monkeypatch.setattr(L, "_fingerprint_parts", lambda: ["CPU-T", "BOARD-T", "DISK-T"])


@pytest.fixture()
def license_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """授权状态文件指到 tmp_path（**绝不碰开发机上的真授权**）。"""
    path = tmp_path / "license.json"
    monkeypatch.setattr(L, "state_path", lambda: path)
    return path


def _expire_trial(path: Path) -> None:
    """造"试用已到期"：首次运行放在 10 天前。"""
    today = clock.now_cn().date()
    path.write_text(json.dumps({
        "first_run": (today - datetime.timedelta(days=10)).strftime("%Y-%m-%d"),
        "max_seen": clock.today_cn(),
    }), encoding="utf-8")


def _teardown_window(win, qapp) -> None:
    """收尾：**与 `tests/test_ui_smoke.py` 的 window fixture 逐条对齐**。

    为什么必须一样严（这里踩过）：主窗口一建起来就在后台起 `_market_worker`（概览取数）
    与 `quotes`（快照）两条线程。只 `close()` 的话它们还在飞，收尾时往**已经关掉的日志流**
    里写日志（CI 日志里能看到 `I/O operation on closed file`），再往后就是随机顺序下的
    `Fatal Python error: Aborted` —— 与 2026-09-20 那次"孤儿桌宠"是同一种病：
    **窗口/线程比测试活得久**。
    """
    from PySide6.QtCore import QEvent

    for timer_name in ("_timer", "_market_timer", "_auction_timer", "_flash_timer"):
        timer = getattr(win, timer_name, None)
        if timer is not None:
            timer.stop()
    win.scheduler.stop()
    win.quotes.stop()
    # 等概览那条后台线程落地（它是最慢的一条；不等就是在赌它跑完前进程先退出）
    worker = getattr(win, "_market_worker", None)
    if worker is not None and worker.isRunning():
        worker.wait(3_000)
    win.shutdown()                 # 收桌宠 / 消息列表 / 浮窗 / 授权·关于窗口
    win.tray.hide()
    win.close()
    win.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture()
def window(cfg, qapp, fingerprint, license_file):
    """按"试用已到期"建一只主窗口（授权相关的用例都从这个状态出发）。"""
    assert ui_app.QT_AVAILABLE is True
    _expire_trial(license_file)
    win = ui_app.MainWindow(cfg)
    yield win
    _teardown_window(win, qapp)


# ══════════════════════════════════════════════════════════════════════════
# 授权对话框
# ══════════════════════════════════════════════════════════════════════════


def test_dialog_shows_machine_code_and_register_controls(window, qapp, cfg) -> None:
    """对话框里三样东西都在：机器码 / 注册码输入口 / 【注册】按钮（用户要求的位置）。"""
    window.on_open_license()
    qapp.processEvents()
    dialog = window.license_dialog

    assert dialog is not None
    # 机器码是**打开对话框时在后台线程里读**的（为了"点一下不卡"，见 licensing 那段注释），
    # 所以这里必须等它落地再断言 —— 不等就是个竞态：本机（Linux，快）常绿，
    # Windows runner 上偶发红（2026-09-21 就是这么红的）。
    _wait_machine(dialog, qapp)
    assert dialog.machine == L.machine_code()
    assert dialog.machine_label.text() == dialog.machine
    assert dialog.code_edit.placeholderText() == "XXXX-XXXX-XXXX-XXXX"
    assert dialog.btn_register.text() == "注册"
    # 联系方式是用户给定要发出去的那句（一个字都不能改）
    assert dialog.contact_label.text() == L.CONTACT_TEXT


def test_dialog_register_with_wrong_code_says_why(window, qapp, cfg) -> None:
    """填错：就地给中文原因，**不关窗口**、也绝不写授权状态。"""
    window.on_open_license()
    dialog = window.license_dialog
    dialog.code_edit.setText("AAAA-BBBB-CCCC-DDDD")

    dialog.btn_register.click()
    qapp.processEvents()

    assert dialog.hint_label.text().startswith("❌")
    assert "不匹配" in dialog.hint_label.text()
    assert L.is_licensed(cfg) is False            # 试用本来已到期，不能被"点一下"变成已授权


def test_dialog_register_unlocks_immediately(window, qapp, cfg) -> None:
    """填对：当场生效（不用重启），状态行立刻变成"已注册"。"""
    window.on_open_license()
    dialog = window.license_dialog
    _wait_machine(dialog, qapp)
    dialog.code_edit.setText(L.expected_code(dialog.machine))

    dialog.btn_register.click()
    qapp.processEvents()

    assert dialog.hint_label.text().startswith("✅")
    assert L.is_licensed(cfg) is True
    assert "已注册" in dialog.status_label.text()


def test_dialog_copy_machine_puts_it_in_the_clipboard(window, qapp) -> None:
    """【复制机器码】：用户要把它发微信，手抄 16 位太容易错。"""
    from PySide6.QtGui import QGuiApplication

    window.on_open_license()
    dialog = window.license_dialog

    # 先等机器码读出来再点复制：那一栏是后台线程填的，机器码还没到手时复制到的是空串
    _wait_machine(dialog, qapp)
    dialog.btn_copy_machine.click()

    assert QGuiApplication.clipboard().text() == dialog.machine
    assert dialog.btn_copy_machine.text() == "已复制"      # 看得见又不打断


# ══════════════════════════════════════════════════════════════════════════
# 到期提醒（只弹一次）
# ══════════════════════════════════════════════════════════════════════════


def test_expired_launch_prompts_once(window, qapp) -> None:
    """到期启动自动弹一次授权提醒；再调一次不会重复弹。"""
    first = window.license_dialog
    assert first is not None                       # 启动就弹了
    assert window._license_prompted is True

    window.on_open_license()                       # 用户自己点开（复用同一个窗口）
    qapp.processEvents()
    assert window.license_dialog is first

    window._maybe_prompt_license()                 # 内部再检查一次
    assert window.license_dialog is first          # 没有堆出第二个


def test_trial_active_does_not_prompt_at_launch(cfg, qapp, fingerprint, license_file) -> None:
    """试用期内不打扰（用户没到期就弹提醒 = 骚扰）。"""
    win = ui_app.MainWindow(cfg)
    try:
        assert L.is_licensed(cfg) is True
        assert win.license_dialog is None
    finally:
        _teardown_window(win, qapp)


# ══════════════════════════════════════════════════════════════════════════
# 锁「策略编辑」
# ══════════════════════════════════════════════════════════════════════════


def test_editor_is_locked_and_points_to_the_dialog(window, qapp) -> None:
    """未授权：点【策略编辑】弹授权对话框、**不打开编辑器**，提示区写明原因与联系方式。"""
    page = window.formula_page

    reason = page.open_editor_guard()
    page.btn_edit.click()
    qapp.processEvents()

    assert reason != ""                            # 闸门拦下了
    assert window.license_dialog is not None       # 并且把授权窗口给了用户
    assert page.hint_text.startswith("🔒")
    assert L.CONTACT_TEXT in page.hint_text        # 微信就在提示里，用户不用猜去哪问
    assert page.bottom_stack.currentWidget() is page.editor_page   # 仍是默认页，但没显示
    assert page.bottom_stack.isVisibleTo(page) is False            # 关键：编辑器没被打开


def test_editor_unlocks_after_registering(window, qapp, cfg) -> None:
    """注册之后：闸门放行，编辑器正常打开（同一只窗口，不用重启）。"""
    page = window.formula_page
    window.on_open_license()
    dialog = window.license_dialog
    _wait_machine(dialog, qapp)
    dialog.code_edit.setText(L.expected_code(dialog.machine))
    dialog.btn_register.click()
    qapp.processEvents()

    assert page.open_editor_guard() == ""          # 放行
    assert "🔒" not in page.btn_edit.toolTip()     # tooltip 里的锁也撤了


def test_editor_tooltip_marks_the_lock(window) -> None:
    """未授权时按钮 tooltip 标明"需要授权"并给出联系方式（用户不用去别处找）。"""
    window._apply_license_lock()

    tip = window.formula_page.btn_edit.toolTip()
    assert "🔒" in tip and L.CONTACT_TEXT in tip


# ══════════════════════════════════════════════════════════════════════════
# 「关于」里的入口
# ══════════════════════════════════════════════════════════════════════════


def test_about_has_license_line_and_entry(window, qapp) -> None:
    """「关于」里能看到授权状态，并且有一个【授权…】按钮打开对话框。"""
    lines = window.about_lines()

    assert any(line.startswith("授权：") for line in lines)
    assert any("试用" in line or "未授权" in line or "已注册" in line for line in lines)

    window.on_about()
    qapp.processEvents()
    assert window.about_license_button.text() == "授权…"

    window.about_license_button.click()
    qapp.processEvents()
    assert window.license_dialog is not None


def test_keygen_matches_the_client_algorithm() -> None:
    """注册机与客户端**同一把密钥、同一个算法**（两处漂移只在用户注册失败时才被发现）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "laoa_keygen", Path(__file__).resolve().parents[1] / "build" / "keygen.py")
    keygen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keygen)

    machine = L.machine_code()
    assert keygen.make_code(machine) == L.expected_code(machine)
    assert L.verify(machine, keygen.make_code(machine))[0] is True


# ══════════════════════════════════════════════════════════════════════════
# 机器码只在"要注册的时候"才读（主人 2026-09-21 的明确口径）
# ══════════════════════════════════════════════════════════════════════════
#
# 原话：「不用每次都读机器码啊，客户要注册的时候再去读」。
# 所以：启动、点【策略编辑】、刷新设置/关于、保存设置 —— **一次都不许读硬件**；
# 只有打开授权对话框（或点【注册】）才读，而且同一进程里只读一次。


def _counting_machine(monkeypatch) -> dict:
    """把机器码查询换成计数器（不读硬件、不缓存）。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return ["CPU-1", "BOARD-1", "DISK-1"]

    monkeypatch.setattr(L, "_fingerprint_parts", counting)
    L.forget_cached_machine_code()
    return calls


def _wait_machine(dialog, qapp, timeout: float = 5.0) -> None:
    """等后台那条"读机器码"落地（对话框里那一栏从"正在读取…"变成真机器码）。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if dialog.machine:
            return
        time.sleep(0.01)
    raise AssertionError("机器码一直没读出来")


def test_routine_paths_never_read_the_machine_code(window, qapp, cfg, monkeypatch) -> None:
    """**没打开授权对话框之前，机器码函数一次都没被调用**（这条是主人这次的硬要求）。"""
    calls = _counting_machine(monkeypatch)

    # ① 启动时已经建过窗口（fixture 里建的）→ 先清一次计数，再把这些日常动作走一遍
    calls["n"] = 0
    window._maybe_prompt_license()                     # 启动那条检查
    window._apply_license_lock()                       # 刷新锁定状态（策略编辑的 tooltip）
    window.license_status_line()                       # 「关于」里那一行
    window.version_info_text()                         # 复制版本信息
    window.about_lines()
    window.on_save_settings()                          # 保存设置
    qapp.processEvents()
    page = window.formula_page
    if page is not None:
        guard = getattr(page, "open_editor_guard", None)
        if callable(guard):
            guard()                                    # 点【策略编辑】走的那条路
    qapp.processEvents()

    assert calls["n"] == 0, f"日常路径读了 {calls['n']} 次机器码（应当是 0 次）"


def test_opening_the_dialog_reads_it_exactly_once(window, qapp, monkeypatch) -> None:
    """打开授权对话框才读机器码，**且只读一次**（再开一次也不重读）。

    注意这个 fixture 是"试用已到期"：主窗口构造时就会自动弹一次授权对话框
    （那是**允许**读机器码的路径 —— 它就是在"要注册"的现场，而且在后台线程里读、界面不冻）。
    所以这里先把那只对话框丢掉、重新开一只，用来量"打开对话框"这一个动作读了几次。
    """
    window.license_dialog = None        # 丢掉启动时自动弹的那一只
    calls = _counting_machine(monkeypatch)

    window.on_open_license()
    qapp.processEvents()
    dialog = window.license_dialog

    # 界面先出来，机器码那一栏先写"正在读取…"（不能把界面冻住）
    assert dialog.machine_label.text() in ("正在读取…", dialog.machine)
    _wait_machine(dialog, qapp)

    assert calls["n"] == 1, f"读机器码 {calls['n']} 次（应当只有 1 次）"
    assert dialog.machine == L.machine_code()
    assert dialog.machine_label.text() == dialog.machine

    window.on_open_license()                           # 再开一次（复用同一个对话框）
    qapp.processEvents()
    _wait_machine(window.license_dialog, qapp)
    assert calls["n"] == 1, "第二次打开又读了一遍机器码"
