"""桌宠（`ui/desktop_pet.py`）的测试。

用户 2026-09-18 的原话
----------------------
> 「像一个桌宠一样的，软件隐藏时停留在桌面，有消息时大声喊出消息内容。」
> （形象用他给的那张图，见 `assets/pet.png` 与 `build/make_pet_asset.py`）

这一页要钉住的是**行为**，不是画得好不好看：

* **与主窗口的可见性解耦** —— 用户最小化/隐藏主窗口时它还得在桌面上（这是原话里的
  核心要求，也是最容易做错的一处：一旦把桌宠挂成主窗口的子控件，主窗口一藏它就没）；
* 来消息时**冒气泡**（过长截断、全文进 tooltip）并**蹦两下**；
* 双击 → 打开消息列表、右键菜单项齐全（测试直接拿 `menu_actions()`，不弹菜单）；
* 拖动 → 发 `moved` 信号（**写配置是主窗口的事**，这里只负责"告诉它"）；
* 未读数写进 tooltip 与菜单文案。

用 Qt 的 `offscreen` 平台在无显示器环境里跑。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过桌宠测试")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from laoa_trader.notify import voice  # noqa: E402
from laoa_trader.ui import desktop_pet as pet_mod  # noqa: E402
from laoa_trader.ui.desktop_pet import DesktopPet  # noqa: E402


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def pet(qapp):
    widget = DesktopPet()
    yield widget
    widget.hide()
    widget.deleteLater()
    qapp.processEvents()


# ══════════════════════════════════════════════════════════════════════════
# 1) 与主窗口解耦：主窗口藏了，它还在
# ══════════════════════════════════════════════════════════════════════════


def test_pet_stays_when_the_main_window_is_hidden(qapp) -> None:
    """**这一条是用户原话的核心**：软件隐藏时桌宠必须还在桌面上。

    构造方式与 `ui/app.py` 一致（`DesktopPet(main_window)`）—— 带 parent **只是**
    为了"主窗口销毁时跟着销毁"，`Qt.Tool` 让它仍然是**独立的顶层窗口**，
    所以主窗口 `hide()`（用户点右上角关闭 = 收进托盘）之后它照样在桌面上。
    """
    host = QWidget()
    host.show()
    qapp.processEvents()
    pet = DesktopPet(host)
    pet.show()
    qapp.processEvents()

    assert pet.isWindow() is True            # 顶层窗口，不是嵌在宿主里的子控件
    assert pet.isVisible() is True

    host.hide()                              # 用户把主窗口收进托盘
    qapp.processEvents()

    assert pet.isVisible() is True, "主窗口一藏，桌宠就跟着没了"

    host.show()
    qapp.processEvents()
    pet.hide()
    pet.deleteLater()
    host.close()
    qapp.processEvents()


def test_pet_is_a_tool_window_that_stays_on_top(pet) -> None:
    """无边框 + 置顶 + `Tool`（不进任务栏）：桌宠不能像"第二个程序窗口"那样占任务栏。"""
    flags = pet.windowFlags()

    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool


# ══════════════════════════════════════════════════════════════════════════
# 2) 气泡：截断、全文、点掉
# ══════════════════════════════════════════════════════════════════════════


def test_bubble_shows_short_text_as_is(pet, qapp) -> None:
    pet.show()                      # 子控件的 isVisible 跟着父窗口：先让桌宠露脸
    qapp.processEvents()
    pet.show_bubble("贵州茅台(600519)，止损提醒")

    assert pet.bubble.isVisible() is True
    assert pet.bubble_text() == "贵州茅台(600519)，止损提醒"
    assert pet.bubble.toolTip() == "贵州茅台(600519)，止损提醒"


def test_long_text_is_clipped_in_the_bubble_but_full_in_tooltip(pet, qapp) -> None:
    """长文本：气泡里截断（桌宠是"看一眼"的东西），全文留在 tooltip。"""
    long_text = "贵州茅台(600519)，触及止损提醒，现价 1234.56，已跌破成本价百分之五，请尽快处理"

    pet.show()
    qapp.processEvents()
    pet.show_bubble(long_text)

    shown = pet.bubble_text()
    assert shown.endswith("…")
    assert len(shown) == pet_mod.BUBBLE_MAX_CHARS
    assert pet.bubble.toolTip() == long_text


def test_whitespace_is_squashed_in_the_bubble(pet, qapp) -> None:
    """多行/连续空格拍平：气泡是一行一句话，不换行成一片。"""
    pet.show()
    qapp.processEvents()
    pet.show_bubble("  触及止损\n\n  现价 1234.56  ")

    assert pet.bubble_text() == "触及止损 现价 1234.56"


def test_clicking_the_bubble_hides_it(pet, qapp) -> None:
    """点一下气泡 = "我看完了"（不用等它自己消失）。"""
    seen: list[int] = []
    pet.bubble_clicked.connect(lambda: seen.append(1))
    pet.show()
    qapp.processEvents()
    pet.show_bubble("触及止损")
    press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPoint(3, 3), QPoint(3, 3),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)

    pet.eventFilter(pet.bubble, press)
    qapp.processEvents()

    assert pet.bubble.isVisible() is False
    assert seen == [1]


# ══════════════════════════════════════════════════════════════════════════
# 3) 来消息：冒泡 + 蹦
# ══════════════════════════════════════════════════════════════════════════


def test_notify_shows_bubble_and_hops(pet, qapp) -> None:
    """`notify()` = 冒泡 + 蹦两下（声音归调用方，桌宠自己不出声）。"""
    pet.show()
    qapp.processEvents()
    before = pet.y()

    pet.notify("贵州茅台(600519)，触及止损")
    assert pet.bubble.isVisible() is True

    for _ in range(40):                 # 蹦是定时器驱动的，转几圈事件循环让它跑完
        qapp.processEvents()
        if not pet._hop_timer.isActive():
            break
        pet._hop_step()
    assert pet._hop_timer.isActive() is False       # 蹦完就停，不会一直动
    assert pet.y() == before                        # 蹦完回到原位（偶数次上下）


def test_notify_can_skip_hopping(pet, qapp) -> None:
    """静音那种"只是想给他看一眼"的场景可以不蹦（气泡照出）。"""
    pet.show()
    qapp.processEvents()
    pet.notify("已静音一小时", hop=False)

    assert pet.bubble.isVisible() is True
    assert pet._hop_timer.isActive() is False


# ══════════════════════════════════════════════════════════════════════════
# 4) 点与菜单
# ══════════════════════════════════════════════════════════════════════════


def test_double_click_emits_activated(pet) -> None:
    """双击 = 打开消息列表（`activated` 由主窗口接到"打开消息窗口"）。"""
    seen: list[int] = []
    pet.activated.connect(lambda: seen.append(1))
    event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPoint(5, 5), QPoint(5, 5),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)

    pet.mouseDoubleClickEvent(event)

    assert seen == [1]


def test_menu_has_the_four_actions(pet) -> None:
    """右键菜单四件事：消息（N）/ 试喊一条 / 静音一小时 / 藏起来。"""
    texts = [action.text() for action in pet.menu_actions()]

    assert texts[0] == "消息（0）"
    assert texts[1] == "试喊一条"
    assert texts[2] == "静音一小时"
    assert texts[3] == "藏起来"


def test_menu_shows_unread_count_and_cancel_mute(pet) -> None:
    """未读数与"静音还剩多少"都要写在菜单上（用户点开就知道现在是什么状态）。"""
    pet.set_unread(3)
    assert pet.menu_actions()[0].text() == "消息（3）"
    assert "未读 3 条" in pet.toolTip()

    voice.mute_for(3600)
    try:
        quiet = pet.menu_actions()[2].text()
        assert quiet.startswith("取消静音") and "分钟" in quiet
    finally:
        voice.unmute()


def test_menu_actions_emit_signals(pet) -> None:
    """菜单项不是摆设：触发时要发出对应信号（主窗口按信号干活）。"""
    fired: dict[str, list] = {"test": [], "mute": [], "hide": [], "open": []}
    pet.activated.connect(lambda: fired["open"].append(1))
    pet.test_requested.connect(lambda: fired["test"].append(1))
    pet.mute_requested.connect(lambda seconds: fired["mute"].append(seconds))
    pet.hide_requested.connect(lambda: fired["hide"].append(1))

    actions = pet.menu_actions()
    actions[0].trigger()
    actions[1].trigger()
    actions[2].trigger()
    actions[3].trigger()

    assert fired["open"] == [1]
    assert fired["test"] == [1]
    assert fired["mute"] and fired["mute"][0] > 0        # 默认是"静音一小时"
    assert fired["hide"] == [1]


# ══════════════════════════════════════════════════════════════════════════
# 5) 拖动：位置由主窗口写进配置
# ══════════════════════════════════════════════════════════════════════════


def test_dragging_emits_moved_with_the_new_position(pet) -> None:
    """松手才发信号（拖动过程中每像素都写盘是浪费）—— 这里钉住"发了、坐标对"。"""
    seen: list[tuple[int, int]] = []
    pet.moved.connect(lambda x, y: seen.append((x, y)))
    pet.move(120, 240)

    press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPoint(4, 4), QPoint(4, 4),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPoint(4, 4), QPoint(4, 4),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    pet.mousePressEvent(press)
    pet.mouseReleaseEvent(release)

    assert seen == [(120, 240)]


def test_release_without_press_does_not_emit(pet) -> None:
    """没按下的"松手"（别的控件漏过来的事件）不该写配置。"""
    seen: list[tuple[int, int]] = []
    pet.moved.connect(lambda x, y: seen.append((x, y)))
    release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPoint(1, 1), QPoint(1, 1),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)

    pet.mouseReleaseEvent(release)

    assert seen == []


# ══════════════════════════════════════════════════════════════════════════
# 6) 素材与降级
# ══════════════════════════════════════════════════════════════════════════


def test_pet_uses_the_shipped_asset(pet) -> None:
    """随包素材在（用户给的那张图）→ 桌宠用它。"""
    assert pet._pixmap is not None and pet._pixmap.isNull() is False


def _render_alpha(widget, point: tuple[int, int]) -> int:
    """把桌宠画到一张透明图上，取某一点的 alpha（0 = 那里什么都没画）。"""
    pixmap = QPixmap(widget.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap)
    return pixmap.toImage().pixelColor(*point).alpha()


def test_transparent_asset_makes_a_free_standing_character(pet, qapp) -> None:
    """**透明底素材 → 自由站立的角色**：不画卡片底、不画圆角边框（用户 2026-09-20 要的）。

    判据是素材自己有没有 alpha 通道；画出来的证据是**顶上那条金边**：
    卡片式会在 (40, 0) 这一带描一条 `#c8a24a` 的边，自由站立式那里是透明的
    （角色的轮廓就是它的形状，四周不该有一圈画出来的框）。
    """
    pet.show()
    qapp.processEvents()

    assert pet._transparent is True, "透明底素材应当被识别为可自由站立"
    assert _render_alpha(pet, (40, 0)) == 0, "透明素材上还是画了卡片边框"
    # 角色本体当然必须画出来：数一下"画了东西"的像素（不依赖角色具体长在哪儿）
    pixmap = QPixmap(pet.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    pet.render(pixmap)
    image = pixmap.toImage()
    painted = sum(
        1
        for y in range(0, image.height(), 3)
        for x in range(0, image.width(), 3)
        if image.pixelColor(x, y).alpha() > 0
    )
    assert painted > 100, "透明底角色一个像素都没画出来"


def test_opaque_asset_falls_back_to_the_rounded_card(qapp, tmp_path) -> None:
    """**没有 alpha 的素材 → 退回圆角卡片**（2026-09-18 那版带花纹的原图就是这种）。

    为什么保留这条回退：那种图四周是不透明花纹，硬抠会抠出一圈脏边，
    给它一块圆角底比"直接贴上去"好看。判据同一个（`hasAlphaChannel()`），
    换素材不用改代码 —— 这条用例就是钉住这件事。
    """
    opaque = tmp_path / "opaque.png"
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("#b8202a"))                 # 不透明红（模拟"带花纹背景"的素材）
    assert image.save(str(opaque), "PNG")

    widget = DesktopPet()
    try:
        widget._pixmap = QPixmap(str(opaque))
        widget._transparent = widget._pixmap.hasAlphaChannel()
        widget.show()
        qapp.processEvents()

        assert widget._transparent is False
        # 卡片式的证据：顶上那条金边画出来了
        assert _render_alpha(widget, (40, 0)) > 0
    finally:
        widget.hide()
        widget.deleteLater()
        qapp.processEvents()


def test_pet_falls_back_to_a_drawn_face_when_the_asset_is_missing(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """素材缺失（打包漏了/被删了）时**不能开不起来** —— 退化成手画的小家伙。"""
    monkeypatch.setattr(pet_mod.assets, "pet_png", lambda: None)

    widget = DesktopPet()
    try:
        assert widget._pixmap is None
        widget.resize(pet_mod.PET_SIZE, pet_mod.PET_SIZE)
        widget.show()                     # 画一次不该抛
        qapp.processEvents()
    finally:
        widget.hide()
        widget.deleteLater()
        qapp.processEvents()


def test_clip_text_keeps_short_text_untouched() -> None:
    assert pet_mod.clip_text("短的") == "短的"
    assert pet_mod.clip_text("x" * 100).endswith("…")
