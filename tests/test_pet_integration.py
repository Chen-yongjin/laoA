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

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from laoa_trader.data import storage  # noqa: E402
from laoa_trader.ui import desktop_pet as pet_mod  # noqa: E402
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
    # 两条假音色（女/男各一条）：'按性别挑'这条链要靠它们验
    monkeypatch.setattr(voice, "_list_voices_raw",
                        lambda: [("Fake 中文 女", "zh-CN", "Female"),
                                 ("Fake 中文 男", "zh-CN", "Male")])
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


def test_pet_survives_the_window_being_minimised(window, qapp) -> None:
    """**最小化**主窗口 → 桌宠仍在（用户 2026-09-20 实报"主界面最小化后桌宠也消失"）。

    根因：桌宠原来带主窗口当 parent，而 Qt 在父窗口 `showMinimized()` 时会把子窗口
    一起藏起来。这条用例就是那个 bug 的看门人 —— 以后谁把 parent 传回去，它会立刻红。
    """
    assert window.pet is not None

    window.showMinimized()
    qapp.processEvents()

    assert window.isMinimized() is True
    assert window.pet.isVisible() is True, "主窗口最小化后桌宠跟着没了"
    # 桌宠自己**不带父窗口**（带了就会被 Qt 连带隐藏）
    assert window.pet.parent() is None

    window.showNormal()
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
    # 代码与价格**逐位**念（用户 2026-09-18："播报代码可以设置成一个一个读数字吗"）
    assert "六零零五一九" in _quiet_voice[0] and "止损提醒" in _quiet_voice[0]


def test_several_alerts_speak_only_the_newest(window, qapp, _quiet_voice) -> None:
    """一次来多条：默认只念最新那条（连着念五条会把人烦到关掉语音）。

    2026-10-05 起"还有 N 条"不再默认念（主人："语音播报有点乱"）—— 想听这句就在
    设置页「一句里念哪几样」里勾上「剩余条数」，见下一个用例。
    """
    rows = [
        {"date": "2026-09-18", "symbol": "600002", "kind": "break_ma5",
         "label": "跌破5日线", "detail": "现价 10.10", "price": 10.1},
        {"date": "2026-09-18", "symbol": "600001", "kind": "stop_loss",
         "label": "止损提醒", "detail": "现价 9.90", "price": 9.9},
    ]

    window._notify_alerts(rows)
    qapp.processEvents()

    text = window.pet.bubble.toolTip()
    assert "还有 1 条" not in text          # 默认不念条数
    # 只念最新那条：念的是列表第一条（跌破5日线），不是两条都念
    import time as _time
    _time.sleep(0.01)
    assert len(_quiet_voice) == 1 and "六零零零零二" in _quiet_voice[0]


def test_count_tail_and_detail_are_optional_fields(window, qapp, _quiet_voice) -> None:
    """勾上「剩余条数」/「说明」就念，取消就不念 —— 设置页那两组勾选框的落点。"""
    window.cfg.voice_fields = ["name", "code", "kind", "price", "detail", "extra"]
    rows = [
        {"date": "2026-09-18", "symbol": "600002", "kind": "break_ma5",
         "label": "跌破5日线", "detail": "现价 10.10", "price": 10.1},
        {"date": "2026-09-18", "symbol": "600001", "kind": "stop_loss",
         "label": "止损提醒", "detail": "现价 9.90", "price": 9.9},
    ]

    window._notify_alerts(rows)
    qapp.processEvents()

    assert "还有 1 条" in window.pet.bubble.toolTip()
    import time as _time
    _time.sleep(0.01)
    assert _quiet_voice and "还有 1 条" in _quiet_voice[-1]


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


def test_tray_menu_has_show_pet_and_clicking_it_brings_the_pet_back(window, qapp) -> None:
    """用户 2026-09-20 要求："加上显示桌宠" —— 托盘右键要能把藏起来的桌宠叫回来。

    这条盯三件事，缺一不可：
    ① 托盘菜单里有这一项（不然用户找不到）；
    ② 点了之后桌宠**真的**回到桌面上；
    ③ **语义与【藏起来】严格对齐** —— 那边写 `notify_pet=false` 并取消设置页勾选，
       这边就要写回 `true` 并勾上，否则会出现"桌宠回来了、设置页却显示没勾"
       这种两处对不上的状态（用户没法判断下次启动它会不会出来）。
    """
    from laoa_trader.ui import app as ui_app

    # 先按用户会走的那条路把它藏起来（右键桌宠 →【藏起来】）
    window.on_pet_hide()
    qapp.processEvents()
    assert window.pet.isVisible() is False
    assert window.cfg.notify_pet is False
    assert window.pet_box.isChecked() is False

    # ① 托盘菜单里有这一项，而且现在是"可点"的状态
    labels = [action.text() for action in window.tray_menu.actions()]
    assert ui_app.TRAY_SHOW_PET_TEXT in labels, labels
    assert window.act_pet.isEnabled() is True
    assert "中文念" in window.act_pet.toolTip()          # tooltip 要说清桌宠是干什么的

    # ② 点它
    window.act_pet.trigger()
    qapp.processEvents()

    assert window.pet is not None and window.pet.isVisible() is True, "点了【显示桌宠】它没回来"
    # ③ 三处状态必须一致：配置 / 设置页勾选框 / 托盘那一项
    assert window.cfg.notify_pet is True
    assert "notify_pet = true" in window.cfg.source_path.read_text(encoding="utf-8")
    assert window.pet_box.isChecked() is True
    assert "已回到桌面" in window.save_settings_hint.text()   # 被动提示告诉他能再藏起来


def test_tray_show_pet_item_is_greyed_out_while_the_pet_is_up(window, qapp) -> None:
    """桌宠已经在桌面上时，托盘那一项**置灰并改文案**（别让用户点了没反应以为坏了）。"""
    from laoa_trader.ui import app as ui_app

    assert window.pet.isVisible() is True                  # 启动就在桌面上
    window._refresh_tray_pet_action()

    assert window.act_pet.text() == ui_app.TRAY_PET_SHOWN_TEXT
    assert window.act_pet.isEnabled() is False
    assert "已经在桌面上了" in window.act_pet.toolTip()

    # 藏起来之后又变回可点（菜单反映的是"弹出来那一刻"的事实）
    window.on_pet_hide()
    qapp.processEvents()
    window._refresh_tray_pet_action()

    assert window.act_pet.text() == ui_app.TRAY_SHOW_PET_TEXT
    assert window.act_pet.isEnabled() is True


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


def test_voice_picker_marks_a_gender_this_machine_does_not_have(window, monkeypatch) -> None:
    """本机没有那个性别时，**选项上直接标出来**（用户 2026-09-21："两个声音都是女声，
    windows没有男声吗？"）。

    光靠下面那行灰字不够：他选"男声"时并不知道这台机器压根没装男声。标在选项上 +
    tooltip 里写清怎么装，比事后解释有效。选项**仍然可选**（装了语音包刷新后就正常，
    不让缓存把用户锁住）。
    """
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "installed_voices",
                        lambda refresh=False: [("Only Female", "zh-CN", "female")])
    monkeypatch.setattr(voice, "_voices", None, raising=False)

    window._fill_voice_names()

    box = window.voice_name_box
    assert box.itemText(1) == "女声"
    assert box.itemText(2) == "男声（本机没有）"
    assert "设置" in str(box.itemData(2, Qt.ItemDataRole.ToolTipRole))
    # 选了它照样能用（回落到自动），只是说明行会讲清楚原因
    box.setCurrentIndex(box.findData("male"))
    window._refresh_voice_hint()
    assert "没有男声音色" in window.voice_hint.text()


def test_voice_hint_lists_the_voices_it_found(window) -> None:
    """说明行要把**实测到的音色清单**摆出来（用户据此判断"到底装没装男声"）。"""
    window._refresh_voice_hint()

    hint = window.voice_hint.text()
    assert "本机语音：" in hint
    assert "Fake 中文 女（女声 · zh-CN）" in hint and "Fake 中文 男（男声 · zh-CN）" in hint


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


# ══════════════════════════════════════════════════════════════════════════
# 设置里"桌宠声音可以自由改"（用户 2026-09-20 原话）
# ══════════════════════════════════════════════════════════════════════════
#
# 用户要的是"能自己挑声音"：音色下拉（第一项 = 自动挑中文）+【试听】按钮，
# 音量/语速保留。这里钉三件事：下拉里真的有系统音色、选了能存进配置并立刻生效、
# 【试听】按**面板当前**的参数念（不必先保存）。


def test_voice_picker_offers_genders_not_voice_names(window) -> None:
    """下拉框只给三项：自动（推荐）/ 女声 / 男声（用户："音色改成让用户可选男声和女声，
    而不是中英文" —— 所以这里**不再**逐条列音色名）。"""
    box = window.voice_name_box

    assert [box.itemData(i) for i in range(box.count())] == ["", "female", "male"]
    assert "自动" in box.itemText(0)
    # 这台机器两条假音色男/女都有 → 选项上不带"本机没有"的后缀
    assert box.itemText(1) == "女声" and box.itemText(2) == "男声"
    # 关掉再开也不会多出别的项（枚举失败/成功都不影响这三项）
    window._fill_voice_names()
    assert box.count() == 3


def test_voice_picker_choice_is_saved_and_used(window, qapp) -> None:
    """选「女声」→ 一键保存写回 `notify_voice_name`，并且下次念就用那个性别的音色。"""
    box = window.voice_name_box
    box.setCurrentIndex(box.findData("female"))

    window.on_save_settings()
    qapp.processEvents()

    assert window.cfg.notify_voice_name == "female"
    assert 'notify_voice_name = "female"' in window.cfg.source_path.read_text(encoding="utf-8")
    # 真挑出来的是那条女性音色（`_quiet_voice` 里喂的两条假音色之一）
    assert voice.chosen_voice(window.cfg) == "Fake 中文 女"


def test_try_listen_speaks_with_the_panel_values(window, qapp, _quiet_voice) -> None:
    """【试听】用面板上的音色/音量念一句样本（**语速锁定 1.0**），且**不写配置、不写库**。

    `_quiet_voice` 这个 fixture 把真正"起进程说话"的那一步换成了"把命令记下来"
    （见它的 docstring），所以这里可以**逐字断言**念的是什么、用哪个音色、音量语速是多少 ——
    而 CI 上不会发出任何声音。
    """
    import time

    spoken: list[str] = _quiet_voice
    window.voice_name_box.setCurrentIndex(window.voice_name_box.findData("male"))
    window.voice_volume_box.setValue(60)
    before = window.cfg.source_path.read_text(encoding="utf-8")

    window.on_try_voice()

    # 念的活交给后台线程（起进程要几百毫秒，放主线程会卡界面）——等它落地
    deadline = time.monotonic() + 5
    while not spoken and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert spoken, "【试听】没有把把朗读命令发出去"
    command = spoken[0]
    assert "SelectVoice('Fake 中文 男')" in command   # 下拉选的是「男声」→ 挑到男声那一条
    assert "$s.Volume = 60" in command                # 面板上的音量
    assert "$s.Rate = 0" in command                   # 语速锁定 1.0 → SAPI 0（不给选）
    assert window.cfg.source_path.read_text(encoding="utf-8") == before   # 试听不写配置
    assert window.cfg.notify_voice_name == ""         # 也没偷偷改内存里的配置


def test_settings_page_has_no_digits_switch(window) -> None:
    """设置页**不能再有**数字逐位念的勾选框（主人 2026-09-21："价格逐位不需要有选项"）。

    控件本身、以及任何指向它的属性都不该留着 —— 只删控件、把 `voice_digits_box`
    这类名字留在页面上，下一个人想"顺手再画出来"就只差几行（这与之前"结果区删干净"
    的做法是同一条纪律）。
    """
    for stale in ("voice_digits_box", "voice_digits_row"):
        assert not hasattr(window, stale), f"「{stale}」应当已经删掉"

    from PySide6.QtWidgets import QCheckBox

    texts = [box.text() for box in window.findChildren(QCheckBox)]
    assert all("逐位" not in text for text in texts), f"还有逐位相关的勾选框：{texts}"


def test_a_fresh_install_shows_rate_1_0_and_the_pet_on(qapp, tmp_path) -> None:
    """**全新安装**（没有任何 config.toml）打开设置页：语速显示 1.0、桌宠勾上是勾着的。

    主人 2026-09-21 的两条默认值口径：
    ① 播报速度默认 1.0（= 正常语速；界面上不该出现 SAPI 的 0 / 1 那种原始值）；
    ② 桌宠默认开启（全新安装就该出现在桌面上）。
    """
    from laoa_trader.config import Config
    from laoa_trader.ui import app as ui_app

    # 全新配置：数据目录与 config.toml 都在临时目录里，什么都没写过
    fresh = Config(data_dir=tmp_path / "fresh-data")
    fresh.ensure_dirs()
    fresh.source_path = tmp_path / "config.toml"          # 不存在 = 全新安装
    storage.init_db(fresh.db_path)

    win = ui_app.MainWindow(fresh)
    win.show()
    qapp.processEvents()
    try:
        # 语速已于 2026-09-21 锁定 1.0 并去掉控件（主人："不要给选择了"）
        assert not hasattr(win, "voice_rate_box"), "设置页不该再有语速控件"
        assert win.pet_box.isChecked() is True, "全新安装桌宠应当默认勾上"
        assert win.pet is not None and win.pet.isVisible() is True, "全新安装桌宠就该在桌面上"
    finally:
        for name in ("_timer", "_market_timer", "_flash_timer"):
            timer = getattr(win, name, None)
            if timer is not None:
                timer.stop()
        win.scheduler.stop()
        win.quotes.stop()
        worker = getattr(win, "_market_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3_000)
        win.shutdown()
        win.tray.hide()
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_settings_page_has_no_rate_control(window) -> None:
    """设置页**不能有语速控件**（主人 2026-09-21："把播报速度直接锁定 1.0 吧 不要给选择了
    选错了感觉太怪了"）。

    音量仍然可调（主人没要求锁），所以这条只针对语速：控件、以及任何指向它的属性都不留。
    """
    assert not hasattr(window, "voice_rate_box"), "语速控件应当已经删掉"

    from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QSpinBox

    # 界面上再没有"语速"这两个字（标签、按钮文字、以及**任何 tooltip** 都不许有 ——
    # 2026-09-21 就漏过一处：试听按钮的 tooltip 还写着"按当前的音色/音量/语速念"，
    # 那条文字会让人去找一个已经删掉的控件）
    for widget in window.findChildren(QLabel) + window.findChildren(QPushButton):
        text = widget.text()
        tip = widget.toolTip()
        assert "语速" not in text, f"还有提到语速的可见文字：{text}"
        if "语速" in tip:
            # 允许"语速固定/锁定"这种**说明事实**的 tooltip，禁止"可以调语速"的口径
            assert "固定" in tip or "锁定" in tip, f"tooltip 还在说语速可调：{tip}"
    # 也没有带 "×" 后缀的倍率控件（那是原来语速框的特征）
    for box in window.findChildren(QDoubleSpinBox) + window.findChildren(QSpinBox):
        assert "×" not in box.suffix(), f"还有个倍率控件：{box.suffix()}"


def test_pet_gets_an_anchor_and_roams_there(window, qapp) -> None:
    """桌宠**会动起来**：主窗口摆好它之后必须给一个"落点"（锚点），否则它一动不动。

    为什么单拎一条：走动是在**锚点附近左右溜达**（见 `DesktopPet._roam_step`），
    而锚点只由两件事设置 —— 主窗口摆位、用户拖动。第一版就漏了"主窗口摆位"那一处，
    结果桌宠站得笔直（功能看着像没做）。这条把那个洞钉死。
    """
    pet = window._ensure_pet()
    assert pet is not None
    qapp.processEvents()
    anchor = pet._anchor
    assert anchor is not None, "主窗口摆好桌宠之后必须给它一个走动锚点"
    assert anchor == (pet.x(), pet.y())

    # 让它走一段：位置要在锚点附近变化，且**垂直方向不动**（右下角那一条上溜达）
    pet._roam_dir = 1
    pet._roam_walk_left = 5
    for _ in range(5):
        pet._roam_step()
    assert pet.roam_offset() != 0
    assert pet.y() == anchor[1]
    assert abs(pet.roam_offset()) <= pet_mod.ROAM_RANGE
