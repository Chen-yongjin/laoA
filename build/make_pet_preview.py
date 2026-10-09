#!/usr/bin/env python3
"""把桌宠的形象渲染成一张**动图预览**（GIF），装软件之前就能看效果。

为什么要有这个脚本
------------------
主人 2026-10-10 要把桌宠换成动态形象。素材是**他出的**，所以两边都得有据可依：
- 他手里没有素材时，`--demo` 会用现在的 `assets/pet.png` **程序化生成一套 8 帧**的
  呼吸摆动帧，先看看"动起来的桌宠"是什么样、值不值得去做真素材；
- 他给了素材之后，直接跑本脚本就能看到"装进程序里会是什么样"（含缩放、透明边、
  帧率），不用先出一个 84MB 的包再装一遍。

怎么跑：
    QT_QPA_PLATFORM=offscreen python build/make_pet_preview.py            # 用现有素材
    QT_QPA_PLATFORM=offscreen python build/make_pet_preview.py --demo     # 程序化造帧
输出：`docs/桌宠动态预览/桌宠预览.gif`（动图）+ `桌宠预览-单帧.png`（首帧，便于在
不支持动图的查看器里看） + `帧-01.png…`（生成的帧，可当作素材模板）。

为什么用 Pillow 存 GIF：Qt 这边**写不了**动图（`QImageWriter` 的格式列表里没有 gif），
而 Pillow 是现成的（本机 12.3）。Pillow 只在这个脚本里用，不进程序依赖。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from PySide6.QtCore import QRectF, Qt                       # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication                   # noqa: E402

from laoa_trader import assets as assets_mod                 # noqa: E402
from laoa_trader.ui import desktop_pet as pet_mod            # noqa: E402

OUT_DIR = REPO / "docs" / "桌宠动态预览"

#: `--demo` 造几帧；帧间隔默认跟程序一致（`pet_mod.FRAME_MS`），
#: 这样预览的节奏与真机上看到的完全一样（主人要求"帧数不要过快"）
DEMO_FRAMES = 8
DEMO_MS = pet_mod.FRAME_MS
#: `--demo` 的幅度：上下浮动像素与缩放百分比。
#: 刻意很小 —— 桌宠是常驻桌面的，动作大了会抢注意力（主人要的是"活的"，不是"跳的"）
DEMO_BOB = 4.0
DEMO_SCALE = 0.03


def _make_demo_frames(count: int = DEMO_FRAMES) -> list[Path]:
    """用现在的静态图程序化生成一套帧（呼吸 + 轻微摆动），返回写好的 PNG 路径。"""
    source = QPixmap(str(assets_mod.pet_png() or ""))
    if source.isNull():
        raise SystemExit("没有 assets/pet.png，造不出演示帧")
    size = pet_mod.PET_SIZE
    out_dir = OUT_DIR / "帧"
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for index in range(count):
        phase = 2 * math.pi * index / count
        bob = DEMO_BOB * math.sin(phase)                  # 上下浮动
        scale = 1.0 + DEMO_SCALE * math.cos(phase)        # 一呼一吸
        canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            side = size * scale
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            target = QRectF((size - side) / 2.0, (size - side) / 2.0 + bob, side, side)
            painter.drawPixmap(target, source, QRectF(source.rect()))
        finally:
            painter.end()
        path = out_dir / f"pet-{index + 1:02d}.png"
        canvas.save(str(path), "PNG")
        made.append(path)
    return made


def _draw_state_sheet(states: dict[str, list[Path]]) -> Path | None:
    """把五种状态各取一帧拼成一张图（"素材都齐了没、每种长什么样"一眼看全）。"""
    from PySide6.QtGui import QFont

    labels = {"idle": "待机", "walk": "走路（平时活动）", "act": "有提醒", "think": "干活中", "sad": "亏了/止损"}
    picked = [(labels.get(key, key), value[0]) for key, value in states.items() if value]
    if not picked:
        return None
    side = 200
    gap = 10
    canvas = QImage(side * len(picked) + gap * (len(picked) + 1),
                    side + 40, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#111318"))
    painter = QPainter(canvas)
    try:
        font = QFont()
        font.setPointSizeF(font.pointSizeF() + 1.0)
        painter.setFont(font)
        painter.setPen(QColor("#e9e9ee"))
        for index, (text, path) in enumerate(picked):
            left = gap + index * (side + gap)
            painter.drawPixmap(left, gap, QPixmap(str(path)).scaled(
                side, side, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            painter.drawText(left, side + 30, text)
    finally:
        painter.end()
    path = OUT_DIR / "状态一览.png"
    canvas.save(str(path), "PNG")
    print(f"状态一览：{path.relative_to(REPO)}")
    return path


def _grab_frames(frames: list[Path], ms: int) -> tuple[list[QImage], QImage]:
    """把桌宠按帧抓成图片（走**真实控件**，所以看到的和装进程序里的一样）。"""
    from laoa_trader.ui import desktop_pet as pet_mod_local

    assets_mod.pet_frames = lambda prefix="pet": frames       # noqa: ARG005 - 脚本里就地替换
    assets_mod.pet_action_frames = lambda: []
    assets_mod.pet_gif = lambda: None
    pet = pet_mod_local.DesktopPet(None)
    pet.show()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    shots: list[QImage] = []
    try:
        # 只截"小家伙"那一块：窗口高度多出来的 `BUBBLE_HEIGHT` 是气泡位，
        # 没气泡时那条是全透明的（预览里留一条透明带会让人以为素材下面缺了一块）
        crop = (0, pet_mod.BUBBLE_HEIGHT, pet_mod.PET_SIZE, pet_mod.PET_SIZE)
        for index in range(len(frames)):
            pet._frame_index = index
            pet.update()
            if app is not None:
                app.processEvents()
            shots.append(pet.grab().toImage().copy(*crop))
    finally:
        pet.shutdown()
        pet.close()
    return shots, shots[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="桌宠动图预览")
    parser.add_argument("--demo", action="store_true",
                        help="用现在的静态图程序化造一套帧（没有素材时看效果用）")
    parser.add_argument("--no-sheet", action="store_true",
                        help="不出「五种状态一览」那张图")
    parser.add_argument("--ms", type=int, default=DEMO_MS,
                        help=f"帧间隔（毫秒），默认 {DEMO_MS}（与程序一致）")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication([])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    states: dict[str, list[Path]] = {}
    if args.demo:
        frames = _make_demo_frames()
        states["walk"] = frames
        print(f"演示帧：{len(frames)} 张 → {frames[0].parent.relative_to(REPO)}")
    else:
        for state in ("idle", "walk", "act", "think", "sad"):
            states[state] = assets_mod.pet_state_frames(state)
        # 动图用**走路那一套**（"平时在右下角活动"就是它）；没有就走待机
        frames = states.get("walk") or states.get("idle") or []
        if not frames:
            single = assets_mod.pet_png()
            if single is None:
                raise SystemExit("没有任何桌宠素材（assets/pet/ 或 pet.png 都没有）")
            print("只有单张素材 → 出静态预览；想看动态请先放序列帧或用 --demo")
            frames = [single]
        print("各状态帧数：" + "、".join(f"{k} {len(v)}" for k, v in states.items() if v))

    if states and not args.no_sheet:
        _draw_state_sheet(states)
    shots, first = _grab_frames(frames, args.ms)
    still = OUT_DIR / "桌宠预览-单帧.png"
    first.save(str(still), "PNG")

    if len(shots) < 2:
        print(f"只有一帧，出静态图：{still.relative_to(REPO)}")
        return 0

    try:
        from PIL import Image
    except ImportError:
        print("没装 Pillow，只能出静态图（动图需要 Pillow 编码 GIF）")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pet-preview-"))
    files: list[Path] = []
    for index, shot in enumerate(shots, start=1):
        path = tmp / f"{index:02d}.png"
        shot.save(str(path), "PNG")
        files.append(path)
    images = [Image.open(path).convert("RGBA") for path in files]
    # GIF 的透明只有 1 bit：先把"接近全透明"的像素压成透明色，边缘才不会糊成一圈白
    masks = []
    for image in images:
        alpha = image.getchannel("A").point(lambda value: 255 if value > 96 else 0)
        flat = Image.new("RGBA", image.size, (0, 0, 0, 0))
        flat.paste(image, (0, 0), alpha)
        masks.append(flat)
    gif = OUT_DIR / "桌宠预览.gif"
    masks[0].save(str(gif), save_all=True, append_images=masks[1:],
                  duration=args.ms, loop=0, disposal=2, transparency=0)
    print(f"动图：{gif.relative_to(REPO)}（{len(shots)} 帧 × {args.ms}ms）")
    print(f"单帧：{still.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
