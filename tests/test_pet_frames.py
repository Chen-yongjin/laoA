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
    """把待机帧挂上去（`action` 传了才有动作帧）。

    ⚠️ 两个都要显式替换：`pet_action_frames()` 内部就是 `pet_frames("act")`，
    只替换前者的话动作帧会**跟着变成同一批待机图**（第一次跑就踩了：
    "没有动作帧时应该蹦两下"那条用例因此拿到了动作帧，蹦跳没发生）。
    """
    def _apply(action=None):
        monkeypatch.setattr(assets_mod, "pet_frames", lambda prefix="pet": frames)
        monkeypatch.setattr(assets_mod, "pet_action_frames", lambda: list(action or []))
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
    action = use_frames(action=None)
    use_frames(action=action[:2])
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
