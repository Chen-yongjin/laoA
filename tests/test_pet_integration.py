"""桌宠 + 中文朗读在**主窗口里**的接线测试（`ui/app.py`）。

用户 2026-09-18 的原话
----------------------
> 「可不可以编写个机器人，直接中文语音提醒。像一个桌宠一样的，软件隐藏时停留在桌面，
>   有消息时大声喊出消息内容。」
> 「不是实盘时间，无法测试消息模式。」

控件自己的行为在 `tests/test_desktop_pet.py` / `tests/test_notify_voice.py` 里测；
这里测的是**接线**，也就是用户真正会遇到的四件事：

1. 程序一打开桌宠就在桌面上；**主窗口收进托盘（hide）之后它还在**（原话里的核心要求）；
2. 来消息时桌宠冒泡、并且**念出来**（朗读那一步在这里被替换掉，CI 上不会真的发声）；
3. 双击桌宠 = 打开消息列表；未读数会同步到桌宠的菜单/tooltip 上；
4. 【试喊一条】在**非交易时段**也能把整条链路走一遍（响声 + 气泡 + 朗读 + 进消息列表），
   而且**不写库**（不污染真实提醒数据）；
   另外：静音、藏起来、拖动记位置、设置里刚勾/刚取消就立刻生效。
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过桌宠接线测试")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from laoa_trader.data import storage  # noqa: E402
from laoa_trader.notify import sound, voice  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def _quiet_voice(monkeypatch: pytest.MonkeyPatch):
    """**绝不真的发声**：朗读这一层全部换掉。

    这是在 CI（Linux）与开发机上都不出声音的前提；顺带把"能不能念"固定成"能" ——
    否则这里所有断言都会跟着"这台机器有没有中文音色"漂移。
    """
    spoken: list[str] = []
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "_list_voices_raw", lambda: [("Fake 中文", "zh-CN")])
    voice.reset_cache()
    voice.unmute()
    monkeypatch.setattr(voice, "run_command", lambda command: spoken.append(" ".join(command)))
    monkeypatch.setattr(sound, "play", lambda *a, **k: None)
    yield spoken
    voice.reset_cache()
    voice.unmute()


@pytest.fixture()
def window(cfg, qapp, tmp_path):
    """一个**真实的主窗口**（与 `test_ui_smoke.py` 同一个建法，收尾也照它收干净）。

    `cfg.source_path` 必须指到临时目录：这几个用例会真的**保存设置**，
    不指的话就会写到开发机真实的 `config.toml` 上去（测试污染用户配置是最糟的副作用）。
    """
    from laoa_trader.ui import app as ui_app

    storage.init_db(cfg.db_path)
    cfg.notify_pet = True
    cfg.notify_voice = True
    cfg.source_path = tmp_path / "config.toml"
    cfg.source_path.write_text("# 用户自己的注释\n", encoding="utf-8")
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()
    yield win
    # 收尾：定时器/线程/窗口都要收干净，否则跨用例累积（见 test_ui_smoke 的同一段注释）
    win._timer.stop()
    win._market_timer.stop()
    win._auction_timer.stop()
    win._flash_timer.stop()
    win.scheduler.stop()
    win.quotes.stop()
    if getattr(win, "message_center", None) is not None:
        win.message_center.close()
    if win.pet is not None:
        win.pet.hide()
        win.pet.deleteLater()
    win.tray.hide()
    win.close()
    win.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


# ══════════════════════════════════════════════════════════════════════════
# 1) 桌宠在不在、主窗口藏了还在不在
# ══════════════════════════════════════════════════════════════════════════


def test_pet_appears_at_startup_and_survives_the_window_being_hidden(window, qapp) -> None:
    """用户原话的核心：**软件隐藏时停留在桌面**。"""
    assert window.pet is not None, "启动就该有桌宠"
    assert window.pet.isVisible() is True

    window.hide()                     # 用户点右上角关闭 = 收进托盘
    qapp.processEvents()

    assert window.isVisible() is False
    assert window.pet.isVisible() is True, "主窗口收进托盘后桌宠跟着没了"

    window.show()
    qapp.processEvents()


def test_pet_is_not_created_when_switched_off(cfg, qapp, monkeypatch) -> None:
    """设置里关掉 `notify_pet` → 桌宠不出现（别在用户桌面上留个东西）。"""
    from laoa_trader.ui import app as ui_app

    storage.init_db(cfg.db_path)
    cfg.notify_pet = False
    win = ui_app.MainWindow(cfg)
    win.show()
    qapp.processEvents()
    try:
        assert win.pet is None or win.pet.isVisible() is False
    finally:
        win._timer.stop()
        win._market_timer.stop()
        win._auction_timer.stop()
        win._flash_timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        win.tray.hide()
        win.close()
        win.deleteLater()
        qapp.processEvents()


# ══════════════════════════════════════════════════════════════════════════
# 2) 来消息：冒泡 + 念出来（朗读被替换，断言"念的是什么"）
# ══════════════════════════════════════════════════════════════════════════


def test_alert_makes_the_pet_bubble_and_speaks(window, qapp, _quiet_voice) -> None:
    """一条提醒 → 气泡显示同一句话，并且这句话被送去朗读。"""
    rows = [{
        "date": "2026-09-18", "symbol": "600519", "kind": "stop_loss",
        "label": "止损提醒", "detail": "已跌破成本价", "price": 1234.56,
        "pushed_at": "2026-09-18 10:00:00",
    }]

    window._notify_alerts(rows)
    qapp.processEvents()

    assert window.pet.bubble.isVisible() is True
    shown = window.pet.bubble.toolTip()
    assert "600519" in shown and "止损提醒" in shown
    # 朗读走的是后台线程，等它把命令记下来（不是"睡一觉赌它跑了"）
    for _ in range(200):
        qapp.processEvents()
        if _quiet_voice:
            break
        import time as _time

        _time.sleep(0.01)
    assert _quiet_voice, "没有把这句话送去朗读"
    assert "600519" in _quiet_voice[0] and "止损提醒" in _quiet_voice[0]


def test_several_alerts_speak_only_the_newest_and_say_how_many_more(
        window, qapp, _quiet_voice) -> None:
    """一次来多条：只念最新那条 + "还有 N 条"（连着念五条会把人烦到关掉语音）。"""
    rows = [
        {"date": "2026-09-18", "symbol": "600002", "kind": "break_ma5",
         "label": "跌破5日线", "detail": "现价 10.10", "price": 10.1},
        {"date": "2026-09-18", "symbol": "600001", "kind": "stop_loss",
         "label": "止损提醒", "detail": "现价 9.90", "price": 9.9},
    ]

    window._notify_alerts(rows)
    qapp.processEvents()

    text = window.pet.bubble.toolTip()
    assert "还有 1 条" in text


def test_voice_switch_off_means_no_speaking(window, qapp, _quiet_voice) -> None:
    """设置里关掉朗读 → 不念（但气泡与消息列表照常）。"""
    window.cfg.notify_voice = False

    window._notify_alerts([{"date": "2026-09-18", "symbol": "600519", "kind": "stop_loss",
                            "label": "止损提醒", "detail": "现价 1234.56", "price": 1234.56}])
    qapp.processEvents()

    assert _quiet_voice == []
    assert window.pet.bubble.isVisible() is True


# ══════════════════════════════════════════════════════════════════════════
# 3) 双击 / 未读数
# ══════════════════════════════════════════════════════════════════════════


def test_double_clicking_the_pet_opens_the_message_list(window, qapp) -> None:
    """双击桌宠 = 打开「消息」列表（与托盘左键同一个回调）。"""
    window.pet.activated.emit()
    qapp.processEvents()

    assert window.message_center is not None
    assert window.message_center.isVisible() is True


def test_pet_unread_count_follows_the_message_list(window, qapp) -> None:
    """未读数三处同步：消息列表 → 托盘菜单 → 桌宠（只刷一处就会出现两个数）。"""
    window._ensure_message_center().add_messages(
        [{"date": "2026-09-18", "symbol": "600519", "kind": "stop_loss",
          "label": "止损提醒", "detail": "现价 1234.56", "price": 1234.56}]
    )
    window._refresh_message_badge()
    qapp.processEvents()

    assert window.pet.unread_count() == window.message_center.unread_count()
    assert "消息（1）" in [action.text() for action in window.pet.menu_actions()]


# ══════════════════════════════════════════════════════════════════════════
# 4) 【试喊一条】：非交易时段也能验，且不写库
# ══════════════════════════════════════════════════════════════════════════


def test_test_shout_runs_the_whole_chain_without_touching_the_database(
        window, qapp, _quiet_voice, cfg) -> None:
    """【试喊一条】= 气泡 + 响声 + 朗读 + 进消息列表；**库里一条提醒都不许留**。"""
    before = _alert_rows(cfg)

    window.on_pet_test()
    qapp.processEvents()
    for _ in range(200):
        qapp.processEvents()
        if _quiet_voice:
            break
        import time as _time

        _time.sleep(0.01)

    # 进列表了（标成"测试"，所以用户一眼知道这是他自己点出来的）
    items = window.message_center.messages()
    assert any(str(item.get("kind")) == "test" for item in items)
    assert "测试" in str(items[0].get("kind_label") or items[0].get("label") or "")
    # 冒泡了
    assert window.pet.bubble.isVisible() is True
    # 念了
    assert _quiet_voice, "试喊一条没有朗读"
    assert "测试" in _quiet_voice[0]
    # **不写库**：真实提醒数据一个字都没变
    assert _alert_rows(cfg) == before


def test_test_shout_explains_when_there_is_no_chinese_voice(
        window, qapp, monkeypatch) -> None:
    """没有中文音色：**不念，但把原因说清楚**（别让用户以为桌宠/语音坏了）。

    平台钉成 Windows：这一句提示分三种（关了语音 / 不是 Windows / 没有中文音色），
    这里要验的正是**第三种**，不钉平台的话在 Linux CI 上会走成第二种。
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(voice, "_list_voices_raw", lambda: [("English David", "en-US")])
    voice.reset_cache()

    window.on_pet_test()
    qapp.processEvents()

    assert window.pet.bubble.isVisible() is True          # 气泡照出
    assert "没有中文语音" in window.save_settings_hint.text()


def test_test_shout_says_so_on_non_windows(window, qapp, monkeypatch) -> None:
    """非 Windows：同样不念，说清是"系统不支持"（不是"程序坏了"）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(voice, "available", lambda: False)
    voice.reset_cache()

    window.on_pet_test()
    qapp.processEvents()

    assert window.pet.bubble.isVisible() is True
    assert "不是 Windows" in window.save_settings_hint.text()


# ══════════════════════════════════════════════════════════════════════════
# 5) 静音 / 藏起来 / 拖动 / 设置保存
# ══════════════════════════════════════════════════════════════════════════


def test_mute_stops_speaking_but_keeps_bubbles(window, qapp, _quiet_voice) -> None:
    """静音只停朗读：提醒照进列表、桌宠照冒泡。"""
    window.on_pet_mute(3600)
    qapp.processEvents()

    window._notify_alerts([{"date": "2026-09-18", "symbol": "600519", "kind": "stop_loss",
                            "label": "止损提醒", "detail": "现价 1234.56", "price": 1234.56}])
    qapp.processEvents()

    assert _quiet_voice == []                       # 没念
    assert window.pet.bubble.isVisible() is True     # 气泡照出
    # 菜单变成"取消静音（还有 N 分钟）"
    assert window.pet.menu_actions()[2].text().startswith("取消静音")

    window.on_pet_mute(0)
    assert voice.muted() is False


def test_hiding_the_pet_writes_the_config_and_can_be_undone(window, qapp) -> None:
    """【藏起来】写进配置（下次启动也不再出来），重新勾上就回来。

    注：`save_settings()` 会返回一个**新的 Config 对象**并替换 `win.cfg`，
    所以这里读的是 `window.cfg`（拿用例自己那个 cfg 变量读会读到旧的，看着像"没生效"）。
    """
    window.on_pet_hide()
    qapp.processEvents()

    assert window.cfg.notify_pet is False
    assert window.pet.isVisible() is False
    assert "notify_pet = false" in window.cfg.source_path.read_text(encoding="utf-8")

    window.cfg.notify_pet = True                     # 用户在设置里重新勾上
    window._apply_pet_and_voice_settings()
    qapp.processEvents()
    assert window.pet.isVisible() is True


def test_pet_position_is_saved_after_dragging(window, qapp) -> None:
    """拖动 → 位置写进配置（下次启动回到这里）。"""
    window.on_pet_moved(333, 222)
    qapp.processEvents()

    assert (window.cfg.pet_x, window.cfg.pet_y) == (333, 222)
    text = window.cfg.source_path.read_text(encoding="utf-8")
    assert "pet_x = 333" in text and "pet_y = 222" in text


def test_saved_settings_apply_the_pet_switch_immediately(window, qapp) -> None:
    """设置里取消勾「桌宠」→ 保存后**立刻**消失（不用重启）。"""
    window.pet_box.setChecked(False)

    window.on_save_settings()
    qapp.processEvents()

    assert window.cfg.notify_pet is False
    assert window.pet.isVisible() is False

    window.pet_box.setChecked(True)
    window.on_save_settings()
    qapp.processEvents()
    assert window.pet.isVisible() is True


def test_voice_hint_says_which_voice_is_used(window, qapp) -> None:
    """设置页那一行说明要告诉用户"到底会不会念、用哪个音色"。"""
    assert "Fake 中文" in window.voice_hint.text()

    window.voice_box.setChecked(False)
    window._refresh_voice_hint()
    assert "已关闭" in window.voice_hint.text()


def _alert_rows(cfg) -> list[tuple]:
    """库里真实提醒行的一份快照（用来证明"试喊一条"没写库）。"""
    with storage.connect(cfg.db_path) as conn:
        return conn.execute(
            "SELECT date, symbol, kind, detail FROM intraday_alert ORDER BY date, symbol, kind"
        ).fetchall()
