"""提醒浮窗（QQ 式）+ 提示音 + 图标闪烁的测试。

为什么单独一个文件
------------------
这一块全是"**只有真把控件建出来才测得到**"的接线：浮窗的行文本、条数上限、
悬停不消失、点了弹详情、闪图标什么时候开始/什么时候停、`notify_popup = false`
会不会真的静音。用 Qt 的 `offscreen` 平台在无显示器环境里跑，
**不会真的响声音、也不会真的闪屏幕**（`winsound` 在非 Windows 上是静默的，
用例里还把 `sound.play` 换成了记录用的假函数）。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QSystemTrayIcon  # noqa: E402

from laoa_trader.data import storage  # noqa: E402
from laoa_trader.notify import sound  # noqa: E402
from laoa_trader.ui import alert_popup as popup_mod  # noqa: E402
from laoa_trader.ui.alert_popup import AlertPopup  # noqa: E402

DAY = "2026-01-05"


# ── 夹具 ──



@pytest.fixture(autouse=True)
def _no_live_quotes(monkeypatch: pytest.MonkeyPatch):
    """实时快照一律换成假的：界面测试不做任何真实外呼。

    为什么显式换掉（socket 层已经会拒）：交易时段里跑测试时，主窗口会按配置去取
    "现价"——被 socket 层拦下会变成一条**报错日志**，那是"报错"而不是"安静地不取"。
    换成记录用的假函数之后，这一路在测试里就是确定性的空数据，
    表格退回本地收盘价（带 `*` 标记），断言与几点钟跑测试无关。
    """
    from laoa_trader.ui import quotes as quotes_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        quotes_mod, "fetch_snapshot_prices",
        lambda cfg, symbols, *, client=None: calls.append(list(symbols)) or {},
    )
    return calls

@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _item(symbol: str = "600000", name: str = "样本股", kind: str = "break_high",
          price: float = 12.34, when: str = f"{DAY} 09:31:05") -> dict:
    """浮窗里的一条（主窗口 `_alert_items` 转出来的形状）。"""
    return {
        "date": DAY,
        "symbol": symbol,
        "target": f"{name}({symbol})",
        "name": name,
        "kind": kind,
        "kind_label": "🚀 放量突破20日高",
        "price": price,
        "price_text": f"{price:.2f}",
        "detail": f"现价 {price:.2f} 突破 20 日高点",
        "pushed_at": when,
        "time": when,
    }


@pytest.fixture()
def popup(qapp):
    """一个独立的浮窗（不起动画，免得断言位置时还在滑）。"""
    widget = AlertPopup(max_items=5, seconds=8, animate=False)
    yield widget
    widget.hide_popup()
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


@pytest.fixture()
def traces(monkeypatch) -> dict:
    """把"响声"换成记录用的假函数（**测试里绝不真的出声**）。"""
    called: list[str] = []
    monkeypatch.setattr(sound, "play", lambda alias=sound.DEFAULT_ALIAS: called.append(alias))
    return {"sound": called}


@pytest.fixture()
def win(cfg, qapp, monkeypatch):
    """主窗口（**只关心提醒**：自检/概览/调度都按住，不去碰网络与对话框）。"""
    from laoa_trader.ui import app as ui_app

    cfg.market_overview = False        # 概览页不取数（这些用例与它无关）
    cfg.auto_run = False
    cfg.auto_download_on_start = False
    cfg.intraday_anomaly = False
    # 空库自检的结论是"needs_full"，会弹首次向导；提醒用例不需要它
    monkeypatch.setattr(ui_app.MainWindow, "run_preflight", lambda self, *a, **k: None)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600000", "样本股", "银行"),
                                         ("000001", "平安银行", "银行")])
    window = ui_app.MainWindow(cfg)
    window.show()
    qapp.processEvents()
    yield window
    # 收尾：定时器/调度/顶层窗口都要收干净，否则会跨用例累积（与界面冒烟测试同一套做法）
    window._timer.stop()
    window._market_timer.stop()
    window._auction_timer.stop()
    window._flash_timer.stop()
    window.scheduler.stop()
    window.quotes.stop()               # 实时快照的工作线程也要收（不然后台还在飞）
    if window.alert_popup is not None:
        window.alert_popup.hide_popup()
        window.alert_popup.close()
    if window.alert_detail_dialog is not None:
        window.alert_detail_dialog.close()
    window.tray.hide()
    window.close()
    window.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _add_alerts(cfg, rows: list[dict]) -> None:
    with storage.connect(cfg.db_path) as conn:
        storage.record_alerts(conn, rows, DAY)


# ── 浮窗本身 ──


def test_row_text_is_name_and_code_like_the_push_text() -> None:
    """行文本 = `名称（代码）  类型  价格`（与推送、提醒列表同一个写法）。"""
    text = popup_mod.row_text(_item())
    assert "样本股(600000)" in text
    assert "🚀 放量突破20日高" in text
    assert "12.34" in text


def test_row_text_degrades_without_name_or_code() -> None:
    """两种退化都不能出现 `（None）`：没名字只留代码，连代码都没有才 `—`。"""
    assert popup_mod.row_text({"symbol": "600000", "kind_label": "🚀 放量突破20日高"}) \
        == "600000  🚀 放量突破20日高"
    assert popup_mod.row_text({}).startswith("—")


def test_popup_lists_items_with_count_title(popup) -> None:
    """三条提醒 → 三行 + 标题带条数。"""
    assert popup.show_alerts([_item("600000"), _item("000001", "平安银行")]) is True
    assert popup.isVisible()
    assert len(popup.rows) == 2
    assert popup.title_label.text() == "⚡ 盘中提醒 · 2 条"
    # 标的写法是**半角**括号 `名称(代码)`（与推送正文、两张表的列头同一套口径）
    assert all("(" in row.text() and ")" in row.text() for row in popup.rows)


def test_long_row_is_elided_but_tooltip_keeps_the_full_text(popup) -> None:
    """特别长的名字 + 类型 + 价格：行里中间省略，**完整文本留在 tooltip**（不能硬裁）。"""
    long_item = _item("688981", "某只名字特别长的半导体股票")
    popup.show_alerts([long_item])
    text = popup.rows[0].text()
    full = popup_mod.row_text(long_item)
    assert len(text) < len(full)
    assert "…" in text
    assert text.startswith("某只名字特别长")          # 开头保住（认得出哪只票）
    assert text.endswith("12.34")                     # 结尾保住（价格一定要看得见）
    assert popup.rows[0].toolTip() == full            # 悬停能看到全文


def test_popup_caps_items_at_max(popup) -> None:
    """最多列 `notify_popup_max_items` 条（新的在最上面）。"""
    popup.max_items = 2
    popup.show_alerts([_item("600000"), _item("000001", "平安银行"),
                       _item("600002", "半导体甲")])
    assert len(popup.rows) == 2
    assert "600000" in popup.rows[0].text()          # 第一条是最新的（调用方按新的在前传）
    assert popup.title_label.text() == "⚡ 盘中提醒 · 2 条"


def test_empty_alerts_do_not_popup(popup) -> None:
    """空列表 → 什么都不弹（闪一个空窗口会让人以为出错了）。"""
    assert popup.show_alerts([]) is False
    assert not popup.isVisible()
    assert popup.rows == []


def test_consecutive_alerts_merge_into_one_popup(popup) -> None:
    """连续到达的多条提醒合并进同一个浮窗，同一条不重复列。"""
    first = _item("600000")
    popup.show_alerts([first])
    assert len(popup.rows) == 1
    popup.show_alerts([_item("000001", "平安银行"), first])
    assert popup.isVisible()
    assert len(popup.rows) == 2                       # 不是两个窗口，也不是三行
    assert "000001" in popup.rows[0].text()
    assert "600000" in popup.rows[1].text()


def test_new_alert_after_hiding_does_not_bring_back_old_items(popup) -> None:
    """浮窗已经收起后再来一条：只显示**新来的**那条（旧的不再翻出来）。"""
    popup.show_alerts([_item("600000")])
    popup.hide_popup()
    popup.show_alerts([_item("000001", "平安银行")])
    assert len(popup.rows) == 1
    assert "000001" in popup.rows[0].text()


def test_merging_does_not_replay_the_slide(qapp) -> None:
    """已经有浮窗在屏幕上时，新提醒合并进来**不重新滑入**（否则每来一条都抖一下）。"""
    widget = AlertPopup(seconds=8, animate=True)
    widget.show_alerts([_item("600000")])
    widget.settle()
    assert widget.pos() == widget.target_pos()
    widget.show_alerts([_item("000001", "平安银行")])
    assert widget.pos() == widget.target_pos()      # 没有被打回屏幕右外侧
    assert len(widget.rows) == 2
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_popup_auto_hides_on_timeout(popup) -> None:
    """到点自己消失：显示时起计时，定时器到点就收起来。"""
    popup.show_alerts([_item()])
    assert popup.hide_timer.isActive()
    assert popup.hide_timer.remainingTime() > 0
    popup.hide_timer.timeout.emit()          # = 到点了（不真等 8 秒）
    assert not popup.isVisible()


def test_hover_pauses_auto_hide_and_leaving_resumes(popup) -> None:
    """鼠标停上去 → 不再自动消失；移开 → 重新开始计时。

    这一条是"用户正在看的时候窗口跑了"的防线：`enterEvent` 里必须停掉定时器。
    """
    popup.show_alerts([_item()])
    assert popup.hide_timer.isActive()
    popup.enterEvent(None)                   # 事件对象用不上（只看"进来了"这件事）
    assert popup.is_hovered() is True
    assert popup.hide_timer.isActive() is False
    popup.leaveEvent(None)
    assert popup.is_hovered() is False
    assert popup.hide_timer.isActive() is True


def test_clicking_a_row_emits_it_and_hides(popup) -> None:
    """点某一条 → 把那条交给主窗口（弹详情），浮窗收起。"""
    seen: list[dict] = []
    popup.item_clicked.connect(seen.append)
    popup.show_alerts([_item("600000"), _item("000001", "平安银行")])
    popup.rows[1].click()
    assert len(seen) == 1
    assert seen[0]["symbol"] == "000001"
    assert seen[0]["target"] == "平安银行(000001)"
    assert not popup.isVisible()


def test_close_and_view_all_buttons(popup) -> None:
    """【关闭】发 `closed`，【查看全部】发 `view_all_clicked`，两者都收起浮窗。"""
    closed: list[int] = []
    view_all: list[int] = []
    popup.closed.connect(lambda: closed.append(1))
    popup.view_all_clicked.connect(lambda: view_all.append(1))
    popup.show_alerts([_item()])
    popup.btn_all.click()
    assert view_all == [1] and not popup.isVisible()
    popup.show_alerts([_item()])
    popup.btn_close.click()
    assert closed == [1] and not popup.isVisible()


def test_target_position_is_bottom_right_of_available_area(qapp) -> None:
    """位置 = 屏幕**可用区域**（已扣掉任务栏）的右下角，留 12 像素余量。"""
    widget = AlertPopup(seconds=8, animate=False)
    widget.show_alerts([_item()])
    widget.settle()
    screen = widget._screen()
    assert screen is not None
    area = screen.availableGeometry()
    target = widget.target_pos()
    assert widget.pos() == target
    assert target.x() + widget.width() <= area.right() + 1
    assert target.y() + widget.height() <= area.bottom() + 1      # 不压任务栏
    assert target.x() >= area.left() and target.y() >= area.top()
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_slide_in_starts_off_screen_to_the_right(qapp) -> None:
    """滑入：显示的一瞬间在屏幕右外侧，动画把它拉到目标位置。"""
    widget = AlertPopup(seconds=8, animate=True)
    widget.show_alerts([_item()])
    assert widget.pos().x() > widget.target_pos().x()
    widget.settle()
    assert widget.pos() == widget.target_pos()
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


# ── 提示音（winsound，非 Windows 静默）──


def test_sound_is_silent_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert sound.available() is False
    assert sound.play() is False             # 什么都不做，也不炸


def test_sound_plays_system_alias_on_windows(monkeypatch) -> None:
    """Windows 上调 `winsound.PlaySound(别名, SND_ALIAS|SND_ASYNC)`（异步 = 不卡界面）。"""
    played: list[tuple] = []
    fake = types.ModuleType("winsound")
    fake.SND_ALIAS = 0x0001
    fake.SND_ASYNC = 0x0001
    fake.PlaySound = lambda name, flags: played.append((name, flags))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert sound.available() is True
    assert sound.play() is True
    assert played == [("SystemAsterisk", fake.SND_ALIAS | fake.SND_ASYNC)]


def test_sound_failure_never_raises(monkeypatch) -> None:
    """声卡/别名出问题只记日志：提醒该响的流程一步都不能因此中断。"""
    fake = types.ModuleType("winsound")
    fake.SND_ALIAS = 0x0001
    fake.SND_ASYNC = 0x0001

    def boom(*_a, **_k):
        raise RuntimeError("没有声卡")

    fake.PlaySound = boom
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert sound.play() is False


# ── 主窗口接线 ──


def test_new_alert_sounds_flashes_and_lands_in_the_message_list(
        win, cfg, qapp, traces, monkeypatch) -> None:
    """一条新提醒 → 响一声 + 图标开始闪 + **进「消息」列表**（浮窗默认不弹）。

    2026-09-18 用户改的口径（原话："通知仿照 QQ 桌面端，有消息软件图标闪烁，
    可以点开查看消息列表"）：消息**不再自己蹦出来**，而是闪图标 + 躺进列表里等他点开。
    """
    alerts: list[list] = []
    monkeypatch.setattr(win, "_start_alert_flash", lambda: alerts.append(["flash"]))
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    assert traces["sound"] == [sound.DEFAULT_ALIAS]
    assert alerts == [["flash"]]
    # 浮窗默认关 → 一条都没建
    assert win.alert_popup is None
    # 但消息进了列表，并且算**未读**（还没打开看）
    center = win.message_center
    assert center is not None and center.table.rowCount() >= 1
    assert center.unread_count() == 1
    row = center.messages()[0]
    assert row["target"] == "样本股(600000)"
    assert "🚀 放量突破20日高" in row["kind_label"]
    # 托盘菜单上的未读数也跟着变（QQ 的"消息(n)"）
    assert "（1）" in win.act_messages.text()


def test_popup_still_available_when_explicitly_switched_on(
        win, cfg, qapp, traces) -> None:
    """勾上 `notify_popup`（默认关）→ 浮窗照旧滑出来，消息**同时也进列表**。

    两条路互不影响：浮窗是"消息自己蹦出来给你看"，消息列表是"你自己点开看"。
    """
    cfg.notify_popup = True
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    assert win.alert_popup is not None and win.alert_popup.isVisible()
    # 名称+代码在**行文本**里（省略是从中间挖的，开头一定保得住）；
    # 类型那一段改看 **tooltip（全文）**：`text()` 是按可用宽度做中间省略后的结果，
    # 而省略位置取决于字体宽度 —— Windows 的微软雅黑比 Linux 那套宽，行文本会变成
    # `样本股(600000)  …突破20日高  12.34`，拿 text() 断言类型文案在 CI 上必红。
    row = win.alert_popup.rows[0]
    assert "样本股(600000)" in row.text()
    assert "🚀 放量突破20日高" in row.toolTip()


def test_alerts_already_in_db_do_not_popup_on_startup(cfg, qapp, monkeypatch, traces) -> None:
    """启动时库里已有的提醒**不弹**（只记账）：开机就被今天的提醒刷屏是骚扰。"""
    from laoa_trader.ui import app as ui_app

    monkeypatch.setattr(ui_app.MainWindow, "run_preflight", lambda self, *a, **k: None)
    cfg.market_overview = False
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [("600000", "样本股", "银行")])
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    window = ui_app.MainWindow(cfg)
    qapp.processEvents()
    assert window.alert_popup is None
    assert traces["sound"] == []
    assert (DAY, "600000", "break_high") in window._alert_seen
    # 同一条再对一次账也不能弹（去重键用的是 (日期, 标的, 类型)）
    window._tick()
    assert window.alert_popup is None
    window._timer.stop()
    window._market_timer.stop()
    window._auction_timer.stop()
    window.scheduler.stop()
    window.tray.hide()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_popup_off_only_means_no_popup(win, cfg, qapp, traces) -> None:
    """`notify_popup = false`（新默认）**只表示不弹浮窗**：响声、闪图标、消息列表照常。

    这条口径 2026-09-18 变过：以前"关浮窗"等于"别打扰我"（连声音和闪烁一起免了）。
    现在关浮窗只是"别自己蹦出来"，提醒本身仍然要让你知道 —— 闪图标 + 消息列表。
    """
    cfg.notify_popup = False
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    assert win.alert_popup is None                       # 不弹窗
    assert traces["sound"] == [sound.DEFAULT_ALIAS]      # 但响了一声
    assert win._flashing is True                         # 也闪了
    assert win.message_center.unread_count() == 1        # 消息躺在列表里等人点开
    win._stop_alert_flash()


def test_sound_off_only_mutes_the_sound(win, cfg, qapp, traces) -> None:
    """`notify_sound = false` 只关声音：闪图标与消息列表照常（静音开会场景）。"""
    cfg.notify_sound = False
    cfg.notify_popup = True                     # 浮窗这一路单独验（默认它是关的）
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    assert traces["sound"] == []
    assert win.alert_popup is not None and win.alert_popup.isVisible()
    assert win._flashing is True
    assert win.message_center.unread_count() == 1


def test_flash_starts_and_stops_with_icon_restored(win, cfg) -> None:
    """闪图标：开始闪 → 托盘图标在两张之间交替；停闪 → 恢复原图标。"""
    cfg.notify_flash_seconds = 6
    normal = win.tray.icon().cacheKey()
    win._start_alert_flash()
    assert win._flashing is True
    assert win._flash_timer.isActive()
    assert win.tray.icon().cacheKey() != normal          # 已经换成红点那张
    win._stop_alert_flash()
    assert win._flashing is False
    assert win._flash_timer.isActive() is False
    assert win.tray.icon().cacheKey() == normal          # 原图标回来了


def test_taskbar_alert_is_bounded_not_infinite(win, cfg, monkeypatch) -> None:
    """任务栏闪烁用**有限的毫秒数**（= 闪烁秒数），不是 `alert(w, 0)` 那种"闪到被激活"。

    为什么：QQ 式交互里用户点的是**消息窗口**，主窗口并不会被激活 ——
    用 `0` 的话任务栏会一直闪下去，而 Qt 没暴露 Win32 的 `FLASHW_STOP`，我们没法主动取消。
    所以闪一个有限的时长，到点自己停（托盘图标那边的闪烁照旧由 QTimer 管）。
    """
    calls: list[tuple] = []
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "alert", staticmethod(
        lambda widget, duration=0: calls.append((widget, duration))
    ))
    cfg.notify_flash_seconds = 7

    win._start_alert_flash()

    assert calls and calls[0][1] == 7000          # 7 秒，不是 0
    win._stop_alert_flash()


def test_flash_seconds_zero_does_not_flash(win, cfg) -> None:
    """`notify_flash_seconds = 0` = 不闪（只弹浮窗、只响声）。"""
    cfg.notify_flash_seconds = 0
    win._start_alert_flash()
    assert win._flashing is False
    assert win._flash_timer.isActive() is False


def test_stale_flash_timeout_does_not_stop_the_new_one(win, cfg) -> None:
    """两条提醒连着来时，第一条的"到点停"不能把第二条的闪烁掐掉。"""
    win._start_alert_flash()
    first_token = win._flash_token
    win._start_alert_flash()
    win._stop_alert_flash(first_token)                  # 旧的那次到点
    assert win._flashing is True                        # 新的还在闪
    win._stop_alert_flash(win._flash_token)
    assert win._flashing is False


def test_alert_icon_is_red_dotted_and_written_to_cache(win, cfg) -> None:
    """红点图标是**程序化画的**，并落一份到数据目录的 cache（不进仓库）。"""
    icon = win._alert_tray_icon()
    assert icon is not None
    assert (cfg.data_dir / "cache" / "tray-alert-32.png").is_file()


def test_row_click_opens_detail_with_push_text(win, cfg, qapp) -> None:
    """点浮窗里的某一条 → 弹详情（推送原文：原因 + 时间 + L2 条件单参数）。"""
    cfg.notify_popup = True                     # 浮窗默认关，这条专测浮窗那一路
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    win.alert_popup.rows[0].click()
    qapp.processEvents()
    assert win.alert_detail_dialog is not None and win.alert_detail_dialog.isVisible()
    text = win.alert_detail_box.toPlainText()
    assert "样本股(600000)" in text
    assert "时间：" in text
    assert "【条件单｜买入】" in text and "触发：价格 ≥" in text     # L2 参数与推送同一份
    assert not win.alert_popup.isVisible()


def test_view_all_switches_to_the_watch_pool_tab(win, cfg, qapp) -> None:
    """【查看全部】→ 显示主窗口并切到「自选标的」页。

    「盘中提醒」那一页已按用户要求取消，提醒现在住在两张表的「提醒」列里 ——
    而「自选标的」是池子的唯一入口（"哪几只票出了什么事"一眼一行）。
    """
    cfg.notify_popup = True                     # 浮窗默认关，这条专测浮窗那一路
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    win.alert_popup.btn_all.click()
    qapp.processEvents()
    assert win.isVisible()
    assert win.tabs.currentWidget() is win.watch_page
    assert win.tabs.tabText(win.tabs.currentIndex()) == "自选标的"


def test_tray_menu_has_messages_item(win, cfg, qapp) -> None:
    """托盘右键有【消息（N）】，点了打开消息列表（QQ 那样）。

    2026-09-18 之前这一项是【最近提醒】——它弹的是那扇滑出浮窗；现在改成开消息窗口，
    标题里的数字就是未读数。
    """
    menu = win.tray.contextMenu()
    texts = [action.text() for action in menu.actions()]
    assert "消息" in texts
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    assert win.act_messages.text() == "消息（1）"        # 未读数写在菜单项上
    win.act_messages.trigger()
    qapp.processEvents()
    assert win.message_center.isVisible()
    assert win.message_center.table.rowCount() == 1
    assert "样本股(600000)" in win.message_center.table.item(0, 1).text()
    # 打开即已读：未读数清零、图标停闪
    assert win.message_center.unread_count() == 0
    assert win.act_messages.text() == "消息"


def test_tray_left_click_opens_the_message_list(win, cfg, qapp) -> None:
    """左键单击托盘图标 → **打开「消息」窗口**（QQ 行为）；双击开主窗口。

    用户 2026-09-18 的要求就是这条交互："有消息软件图标闪烁，可以点开查看消息列表"。
    浮窗开着的人仍然先抬浮窗（他眼前已经有东西可看了）。
    """
    reason = QSystemTrayIcon.ActivationReason
    win.hide()
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    win.hide()
    win._on_tray_activated(reason.Trigger)
    assert win.message_center is not None and win.message_center.isVisible()
    assert not win.isVisible()                  # 不抢主窗口
    win.message_center.close()
    # 双击 → 主窗口（老行为不变）
    win._on_tray_activated(reason.DoubleClick)
    assert win.isVisible()


def test_tray_left_click_still_raises_an_open_popup(win, cfg, qapp) -> None:
    """浮窗开着时（`notify_popup`）左键先抬浮窗 —— 眼前已有东西可看，不该再开一扇窗。"""
    from PySide6.QtWidgets import QSystemTrayIcon as _Tray

    cfg.notify_popup = True
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()
    win.hide()
    win._on_tray_activated(_Tray.ActivationReason.Trigger)
    assert win.alert_popup.isVisible()
    assert win.message_center is None or not win.message_center.isVisible()


def test_recent_alerts_popup_lists_recent_rows(win, cfg, qapp) -> None:
    """【最近提醒】在没有任何新提醒时也从库里取最近几条来弹。"""
    _add_alerts(cfg, [
        {"symbol": "600000", "kind": "break_high", "price": 12.34, "detail": "突破"},
        {"symbol": "000001", "kind": "stop_loss", "price": 10.5, "detail": "跌破止损"},
    ])
    assert win.show_alert_popup(recent=True) is True
    qapp.processEvents()
    assert len(win.alert_popup.rows) == 2
    assert "平安银行(000001)" in win.alert_popup.rows[0].text()


def test_popup_shows_nothing_when_there_are_no_alerts(win, qapp) -> None:
    """库里一条提醒都没有时不弹空浮窗（只提示一句）。"""
    assert win.show_alert_popup(recent=True) is False
    assert win.alert_popup is None


def test_flash_and_sound_still_work_with_no_notify_channels(win, cfg, qapp, traces) -> None:
    """`notify_channels == []`（出厂默认）= **响声 + 闪图标 + 进消息列表**。

    用户拍板的新默认是"平时躺在任务栏，有消息就闪图标、点开看列表"：
    空频道列表只表示"不走系统弹窗/托盘气泡/飞书"，与本地这一套**互不相干** ——
    这条用例就是为了防止将来有人把"没勾任何频道"顺手写成"什么都不提醒"。
    """
    cfg.notify_channels = []
    _add_alerts(cfg, [{"symbol": "600000", "kind": "break_high", "price": 12.34,
                       "detail": "现价 12.34 突破 20 日高点"}])
    win._tick()
    qapp.processEvents()

    assert traces["sound"] == [sound.DEFAULT_ALIAS]          # 响了
    assert win._flashing is True                             # 闪了
    assert win._flash_timer.isActive() is True
    assert win.message_center.unread_count() == 1            # 消息进了列表
    win._stop_alert_flash()


def test_tray_menu_has_pause_intraday_item(win, qapp) -> None:
    """托盘的【暂停提醒】可勾选：窗口收在托盘里时，这是暂停提醒最顺手的入口。"""
    from laoa_trader.ui import app as ui_app

    texts = [a.text() for a in win.tray_menu.actions()]
    # 2026-09-18 起这一项叫【消息】（打开消息列表），不再是【最近提醒】（弹浮窗）
    assert "显示主窗口" in texts and "消息" in texts and "退出" in texts
    assert ui_app.BTN_START_TEXT in texts                    # 【开始匹配】也在托盘上
    act = next(a for a in win.tray_menu.actions() if a.text() == "暂停提醒")
    assert act.isCheckable() is True
    assert act.isChecked() is False
    act.trigger()
    qapp.processEvents()
    assert win.scheduler.status()["intraday_paused"] is True
    assert act.isChecked() is True
    assert win.btn_pause.text() == ui_app.BTN_RESUME_TEXT     # 设置页那个按钮同步了
