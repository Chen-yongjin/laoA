"""桌宠动态形象（多帧序列 / GIF）：主人 2026-10-10 问「宠物形象可以换成动态吗」。

他选的是"换一套多帧形象（你出素材）"，所以这一份用例要钉住的是**素材怎么放就一定生效**，
以及"没有素材时不许把桌宠搞坏"：

1. 序列帧按**文件名里的数字**排序（`pet-2` 在 `pet-10` 前面 —— 按字符串排会得到
   `pet-1, pet-10, pet-2`，播出来的动作是乱的）；
2. 多帧时**只有可见才播**（桌宠常驻桌面，藏起来还每 120ms 重画一张 512×512 是白烧 CPU）；
3. 有 `act-*.png` 动作帧就播动作，没有才退回"蹦两下"；
4. 素材三种形态（序列帧 / GIF / 单张图）与"什么都没有"四种情况都要能开起来。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from laoa_trader import assets as assets_mod  # noqa: E402
from laoa_trader.ui import desktop_pet as pet_mod  # noqa: E402

#: 最小的合法 GIF（1×1、透明）：够让 QMovie 认它是一张图，且不依赖 Pillow
#: （测试环境不该为了造素材再装一个图像库）
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def frames(tmp_path):
    """造 3 帧纯色 PNG（第 N 帧是纯色 N，便于断言"现在画的是第几帧"）。"""
    made = []
    for index, color in enumerate(("#ff0000", "#00ff00", "#0000ff"), start=1):
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(color))
        path = tmp_path / f"pet-{index}.png"
        assert pixmap.save(str(path), "PNG") is True
        made.append(path)
    return made


@pytest.fixture()
def use_frames(monkeypatch, frames):
    """把某一批帧挂成桌宠的"素材"（`action` = 动作帧，`walk` = 走路帧）。

    ⚠️ 替换的是 **`pet_state_frames(state)`** 这一个入口：桌宠现在是按状态取帧的
    （`idle`/`walk`/`act`/`think`/`sad`），只替换 `pet_frames` 的话，
    `pet_state_frames` 会拿着同一个补丁去回答**所有**状态 ——
    "没有动作帧时应该蹦两下"那条用例因此拿到了动作帧，蹦跳压根没发生（实测踩过）。
    """
    def _apply(action=None, walk=None, think=None, sad=None):
        mapping = {
            "idle": list(frames),
            "walk": list(walk or []),
            "act": list(action or []),
            "think": list(think or []),
            "sad": list(sad or []),
        }
        monkeypatch.setattr(assets_mod, "pet_state_frames",
                            lambda state: mapping.get(str(state), []))
        monkeypatch.setattr(assets_mod, "pet_frames", lambda prefix="pet": [])
        monkeypatch.setattr(assets_mod, "pet_action_frames", lambda: list(action or []))
        monkeypatch.setattr(assets_mod, "pet_gif", lambda: None)
        monkeypatch.setattr(assets_mod, "pet_png", lambda: None)
        return frames

    return _apply


@pytest.fixture()
def quiet_pet(monkeypatch):
    """把三种素材来源都清空，用例各自按需挂上去（互不干扰）。"""
    monkeypatch.setattr(assets_mod, "pet_frames", lambda prefix="pet": [])
    monkeypatch.setattr(assets_mod, "pet_action_frames", lambda: [])
    monkeypatch.setattr(assets_mod, "pet_gif", lambda: None)
    monkeypatch.setattr(assets_mod, "pet_png", lambda: None)


# ── 1) 素材来源解析（assets 层，纯路径逻辑）──


def test_frames_are_sorted_by_number_not_by_name(tmp_path, monkeypatch) -> None:
    """`pet-2` 必须排在 `pet-10` 前面 —— 按字符串排会把动画播乱，而且很难看出是排序问题。"""
    folder = tmp_path / "pet"
    folder.mkdir()
    for name in ("pet-10.png", "pet-2.png", "pet-1.png", "pet-旧.png", "act-1.png"):
        (folder / name).write_bytes(b"x")
    monkeypatch.setattr(assets_mod, "assets_dir", lambda: tmp_path)
    assert [p.name for p in assets_mod.pet_frames()] == [
        "pet-1.png", "pet-2.png", "pet-10.png"]
    assert [p.name for p in assets_mod.pet_action_frames()] == ["act-1.png"]


def test_frames_lookup_is_empty_when_there_is_no_folder(tmp_path, monkeypatch) -> None:
    """没有 `assets/pet/` 目录时返回空列表（不是异常）—— 只有单张图的人不该受影响。"""
    monkeypatch.setattr(assets_mod, "assets_dir", lambda: tmp_path)
    assert assets_mod.pet_frames() == []
    assert assets_mod.pet_action_frames() == []
    assert assets_mod.pet_gif() is None


# ── 2) 桌宠按帧播放 ──


def test_sequence_frames_make_the_pet_animated(qapp, use_frames) -> None:
    """给了序列帧 → 会自己动：帧数、当前帧、定时器三样都要对。"""
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet.is_animated() is True
        assert pet.frame_count() == 3
        assert pet.current_frame_index() == 0
        assert pet._anim_timer.isActive() is False      # 还没 show：不许空转
        pet.show()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is True       # 显示出来才开始播
        pet._anim_step()
        assert pet.current_frame_index() == 1
        pet._anim_step()
        pet._anim_step()
        assert pet.current_frame_index() == 0           # 循环回到第一帧
        # 画出来的确实是"当前那一帧"的颜色
        shot = pet.grab().toImage()
        center = shot.pixelColor(int(pet.size_ * 0.5), int(pet.size_ * 0.5) + 30)
        assert center.isValid()
    finally:
        pet.shutdown()
        pet.close()


def test_animation_pauses_when_the_pet_is_hidden(qapp, use_frames) -> None:
    """藏起来就停：桌宠是常驻桌面的，不可见时还每 120ms 重画一张大图纯属白烧 CPU。"""
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is True
        pet.hide()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is False
        pet.show()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is True
    finally:
        pet.shutdown()
        pet.close()


def test_action_frames_play_once_and_then_return_to_idle(qapp, use_frames) -> None:
    """有 `act-*.png` 就播动作帧：播完自动回待机，**不再叠"蹦两下"**（会显乱）。"""
    use_frames(action=use_frames()[:2])
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        pet.notify("测试一条", hop=True)
        assert pet._action_left == 2                    # 动作帧有 2 帧就播 2 格
        assert pet._hop_timer.isActive() is False       # 不同时蹦跳
        pet._anim_step()
        assert pet._action_left == 1
        pet._anim_step()
        assert pet._action_left == 0
        assert pet.current_frame_index() == 0           # 回到待机第一帧
    finally:
        pet.shutdown()
        pet.close()


def test_without_action_frames_the_pet_still_hops(qapp, use_frames) -> None:
    """只有待机帧时，来消息照旧"蹦两下"（老行为不能丢）。"""
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        pet.notify("测试一条", hop=True)
        assert pet._hop_timer.isActive() is True
        assert pet._action_left == 0
    finally:
        pet.shutdown()
        pet.close()


# ── 3) GIF 与降级 ──


def test_gif_asset_is_played_by_qmovie(qapp, monkeypatch, tmp_path, quiet_pet) -> None:
    """给了 `pet.gif` 就走 QMovie（用户手里是 GIF 时不用先拆帧）。"""
    path = tmp_path / "pet.gif"
    path.write_bytes(TINY_GIF)
    monkeypatch.setattr(assets_mod, "pet_gif", lambda: path)
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet._movie is not None
        assert pet.is_animated() is True
        assert pet._pixmap is not None and pet._pixmap.isNull() is False
        pet.show()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is True
    finally:
        pet.shutdown()
        pet.close()


def test_sequence_frames_win_over_the_gif(qapp, monkeypatch, tmp_path,
                                          use_frames, quiet_pet) -> None:
    """两种素材都在时**用序列帧**：GIF 的透明只有 1 bit，边缘会有一圈白边。"""
    path = tmp_path / "pet.gif"
    path.write_bytes(TINY_GIF)
    monkeypatch.setattr(assets_mod, "pet_gif", lambda: path)
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet._movie is None
        assert pet.frame_count() == 3
    finally:
        pet.shutdown()
        pet.close()


def test_single_image_still_works_and_is_not_animated(qapp, quiet_pet, monkeypatch, tmp_path) -> None:
    """只有 `pet.png` 时行为与以前**完全一样**：静态、不启定时器、照样能画。"""
    path = tmp_path / "pet.png"
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#b8202a"))
    assert pixmap.save(str(path), "PNG") is True
    monkeypatch.setattr(assets_mod, "pet_png", lambda: path)
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet.is_animated() is False
        assert pet.frame_count() == 1
        assert pet._pixmap is not None
        pet.show()
        qapp.processEvents()
        assert pet._anim_timer.isActive() is False
    finally:
        pet.shutdown()
        pet.close()


def test_no_asset_at_all_still_paints_the_hand_drawn_face(qapp, quiet_pet) -> None:
    """一张素材都没有时照旧手画小圆脸（桌宠绝不能因为没素材就开不起来）。"""
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet.current_pixmap() is None
        assert pet.is_animated() is False
        pet.resize(pet_mod.PET_SIZE, pet_mod.PET_SIZE + pet_mod.BUBBLE_HEIGHT)
        pet.show()
        qapp.processEvents()
        assert pet.grab().toImage().isNull() is False
    finally:
        pet.shutdown()
        pet.close()


def test_broken_frame_file_is_skipped_not_fatal(qapp, monkeypatch, tmp_path) -> None:
    """坏帧文件只跳过它自己 —— 一张坏图不该让整只桌宠降级成手画的。"""
    good = tmp_path / "pet-1.png"
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor("#123456"))
    assert pixmap.save(str(good), "PNG") is True
    broken = tmp_path / "pet-2.png"
    broken.write_bytes(b"not a png")
    monkeypatch.setattr(assets_mod, "pet_frames", lambda prefix="pet": [good, broken])
    monkeypatch.setattr(assets_mod, "pet_action_frames", lambda: [])
    pet = pet_mod.DesktopPet(None)
    try:
        assert pet.frame_count() == 1              # 好的那张还在
        assert pet.current_pixmap() is not None
    finally:
        pet.shutdown()
        pet.close()


def test_pet_window_flags_are_unchanged_by_the_animation(qapp, use_frames) -> None:
    """动态化不许动那三条窗口属性（常驻桌面靠的就是它们）。"""
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        flags = pet.windowFlags()
        for flag in (Qt.WindowType.FramelessWindowHint,
                     Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.Tool):
            assert flags & flag
    finally:
        pet.shutdown()
        pet.close()


# ── 4) "平时在桌面右下角活动"与"帧数不要过快"（2026-10-10 主人的两条要求）──


def test_frame_interval_is_deliberately_slow() -> None:
    """**帧数不要过快**（主人原话）：帧间隔不许低于 150ms（约 6.7 帧/秒）。

    这条是"防回归"用的：走路的观感基本由这一个数决定，谁把它调回 100ms 以下，
    机器人就从"慢慢溜达"变成"抽搐"（实测过）。
    """
    assert pet_mod.FRAME_MS >= 150, f"帧间隔 {pet_mod.FRAME_MS}ms 太快了"
    assert pet_mod.WALK_STEP / (pet_mod.FRAME_MS / 1000.0) <= 30, "走路速度超过 30 像素/秒，太快"


def test_walking_moves_along_a_short_range_around_the_anchor(qapp, use_frames) -> None:
    """走动＝在**锚点附近左右晃**：水平挪、垂直不动、范围有上限。"""
    idle = use_frames(walk=use_frames())
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        anchor = (500, 400)
        pet.set_anchor(*anchor)
        assert pet.roam_offset() == 0

        pet._roam_dir = 1
        pet._roam_walk_left = 10
        for _ in range(10):
            pet._roam_step()
        assert pet.roam_offset() == 10 * pet_mod.WALK_STEP
        assert pet.x() == anchor[0] + 10 * pet_mod.WALK_STEP
        assert pet.y() == anchor[1], "走动只该在水平方向（右下角那一条上溜达）"
        assert idle is not None

        # 一直走也不会走出范围：到边界自动掉头
        pet._roam_walk_left = 500
        for _ in range(500):
            pet._roam_step()
        assert abs(pet.roam_offset()) <= pet_mod.ROAM_RANGE
        assert anchor[0] - pet_mod.ROAM_RANGE <= pet.x() <= anchor[0] + pet_mod.ROAM_RANGE
    finally:
        pet.shutdown()
        pet.close()


def test_walking_uses_the_walk_frames(qapp, use_frames) -> None:
    """走动时画的是**走路帧**，站着时画待机帧（两套素材各自管一段）。"""
    use_frames(walk=use_frames())
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        pet.set_anchor(100, 100)
        pet._roam_walk_left = 5
        pet._frame_index = 0
        assert pet.current_pixmap() is pet._walk_frames[1][0]
        pet._roam_walk_left = 0
        assert pet.current_pixmap() is pet._frames[0]
    finally:
        pet.shutdown()
        pet.close()


def test_mood_pauses_walking_and_uses_its_own_frames(qapp, use_frames) -> None:
    """情绪（think / sad）期间不走动，且画自己那一套帧。"""
    use_frames(think=[], sad=[])
    # think/sad 用另外两张图，便于断言"现在画的是哪一套"
    from PySide6.QtGui import QColor as _QColor

    def make(color):
        pixmap = QPixmap(32, 32)
        pixmap.fill(_QColor(color))
        return pixmap

    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        pet._states["think"] = [make("#ff00ff")]
        pet._states["sad"] = [make("#00ffff")]
        pet.set_anchor(200, 200)
        pet._roam_walk_left = 5
        before = pet.x()

        pet.set_mood("think")
        assert pet.mood() == "think"
        assert pet.current_pixmap() is pet._states["think"][0]
        assert pet._roam_walk_left == 0, "思考的时候不该还在踱步"
        for _ in range(5):
            pet._anim_step()
        assert pet.x() == before, "情绪期间窗口不该移动"

        pet.set_mood("止损")          # 认不出来的名字 → 回 idle（不许崩）
        assert pet.mood() == "idle"
        assert pet.current_pixmap() is pet._frames[0]     # 待机帧（不是路径列表）
    finally:
        pet.shutdown()
        pet.close()


def test_notify_carries_the_kind_into_the_mood(qapp, use_frames) -> None:
    """提醒类型决定表情：止损/跌破 → 难过；其它 → 平常心。"""
    use_frames()
    pet = pet_mod.DesktopPet(None)
    try:
        pet.show()
        qapp.processEvents()
        pet.notify("测试 600000 止损提醒", kind="止损提醒")
        assert pet.mood() == "sad"
        pet.notify("测试 600000 涨停打开", kind="涨停打开")
        assert pet.mood() == "idle"
        assert pet._is_bad_news("跌破成本价") is True
        assert pet._is_bad_news("") is False
        assert pet._is_bad_news("涨停打开") is False
    finally:
        pet.shutdown()
        pet.close()


def test_real_asset_pack_has_every_state() -> None:
    """真机上（随包素材）五种状态都要读得出来 —— 少一套 = "情绪"那部分静默失效。

    这条用的是**仓库里真实的** `assets/pet/`：主人给的 8 张立绘经
    `build/make_pet_frames.py` 生成之后就在那儿。
    """
    from laoa_trader import assets as real_assets

    counts = {state: len(real_assets.pet_state_frames(state))
              for state in ("idle", "walk", "act", "think", "sad")}
    assert counts["idle"] >= 1, counts
    assert counts["walk"] >= 2, counts          # 走路至少要两帧才叫循环
    assert counts["act"] >= 1, counts
    assert counts["think"] >= 1, counts
    assert counts["sad"] >= 1, counts
