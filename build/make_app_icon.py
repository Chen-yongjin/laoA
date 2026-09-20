"""把用户给的桌宠图做成**应用图标**（窗口 / 托盘 / 任务栏 / exe 文件图标）。

为什么要单独一个脚本（用户 2026-09-20 要求"图标也改为桌宠2.png同款"）：
仓库里原来的 `build/make_icon.py` 依赖 **Pillow**，而运行依赖里刻意没有 Pillow
（`PySide6` 本来就是依赖，用它缩放就够了，见 `make_pet_asset.py` 的同一条理由）。
但 PyInstaller 的 `icon=` 只吃 `.ico`，而 Qt 不能写 ICO —— 所以这里：
  * 用 QImage 生成各尺寸 PNG（16/24/32/48/64/128/256）；
  * **自己拼一个 ICO 容器**：Vista 起 ICO 允许直接内嵌 PNG 负载
    （ICONDIR + N 个 ICONDIRENTRY + 每张 PNG 的原始字节；≥256 的尺寸在目录项里写 0）。

用法：
    python build/make_app_icon.py                 # 默认读 ../桌宠2.png
    python build/make_app_icon.py <源图路径>

产出（都写进 `src/laoa_trader/assets/`，随包分发）：
    icon.png / icon-16.png … icon-256.png / icon.ico
"""

from __future__ import annotations

import struct
import sys
from typing import Any
from pathlib import Path

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 无显示器也能生成（CI/本机都一样）

from PySide6.QtCore import Qt            # noqa: E402
from PySide6.QtGui import QImage         # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS = PROJECT_ROOT / "src" / "laoa_trader" / "assets"
DEFAULT_SOURCE = PROJECT_ROOT.parent / "桌宠2.png"     # 桌宠同款（透明底）
#: 生成哪些尺寸（Windows 会按需要挑；24 是小图标档，任务栏/资源管理器用 32/48/256）
SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
#: ICO 里装哪几个尺寸。**256 必须在**（用户 2026-09-20 实报"图标偏小、模糊"）：
#: 资源管理器与任务栏的大图标视图直接用 256 那一档，缺档时 Windows 会拿小的放大 → 糊。
#: 代价是 .ico 变大（256 那档的 PNG 负载 40 多 KB），`tests/test_assets.py` 的体积上限
#: 按新的实际大小调高了，**不是**为了过测试而砍图标。
ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
#: **小尺寸（≤ 这个值）先按"角色头部"裁剪再缩**：整张"财神娃娃骑牛"缩到 16 像素就是
#: 一坨看不清的东西（用户原话"图标偏小、模糊"）。裁到头部之后同一个 16 像素里
#: 主体占比大得多，任务栏那一档才认得出来。
SMALL_CROP_MAX = 32
#: 头部裁剪框：取主体 alpha 包围盒**上半部分**（人 + 元宝在牛的上方），再左右居中成方形。
#: 这个 0.58 是实测出来的比例：再小会把帽子切掉，再大又会把牛身一起吃进来变糊。
HEAD_CROP_RATIO = 0.58


def _subject_box(img: QImage) -> Any:
    """主体（角色）的 alpha 包围盒。**每张图都现算**：素材换一张，裁剪框自动跟着走。"""
    left, top, right, bottom = img.width(), img.height(), 0, 0
    for y in range(0, img.height(), 4):                 # 每 4 行/列采一次，够准且快
        for x in range(0, img.width(), 4):
            if (img.pixel(x, y) >> 24) & 0xFF > 24:
                left, top = min(left, x), min(top, y)
                right, bottom = max(right, x), max(bottom, y)
    if right <= left or bottom <= top:                  # 全透明（坏素材）：退回整张
        return (0, 0, img.width(), img.height())
    return (left, top, right - left, bottom - top)


def _head_crop(img: QImage) -> QImage:
    """裁出"角色头部"那一块（小尺寸图标专用，见 `SMALL_CROP_MAX` 的注释）。

    做法：先取主体包围盒，只保留**上半部分**（`HEAD_CROP_RATIO`），
    再以它为中心扩成一个正方形（正方形缩到 16×16 才不变形）。
    """
    x, y, w, h = _subject_box(img)
    side = int(max(w, h * HEAD_CROP_RATIO) * 1.12)      # 留一点余量，别贴着帽子边切
    cx = x + w // 2
    cy = y + int(h * HEAD_CROP_RATIO * 0.5)
    left = max(0, min(img.width() - side, cx - side // 2))
    top = max(0, min(img.height() - side, cy - side // 2))
    return img.copy(left, top, min(side, img.width() - left), min(side, img.height() - top))


def _render(source: Path, size: int, *, cropped: bool = False) -> QImage:
    """把源图等比缩放成 size×size 的 RGBA 图（透明底保留，四周留一点边）。

    Args:
        cropped: True 时**先裁到角色头部再缩**（小尺寸专用，见 `_head_crop`）。
    """
    img = QImage(str(source))
    if img.isNull():
        raise SystemExit(f"读不出源图：{source}")
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    if cropped:
        img = _head_crop(img)
    inner = max(1, int(size * 0.92))                    # 留 4% 边，免得贴边难看
    scaled = img.scaled(inner, inner, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    from PySide6.QtGui import QPainter
    painter = QPainter(canvas)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def _png_bytes(image: QImage) -> bytes:
    """QImage → PNG 字节。

    ⚠️ `QByteArray` 必须**先建好并一直持有引用**：写成 `QBuffer(QByteArray())` 时那个
    临时对象会被回收，而 QBuffer 还指着它 —— 实测直接段错误（faulthandler 指到这一行）。
    """
    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return bytes(data)


def _write_ico(path: Path, entries: list[bytes], sizes: list[int]) -> None:
    """手写 ICO：PNG 负载直接内嵌（Windows Vista+ 与 PyInstaller 都认）。"""
    count = len(entries)
    header = struct.pack("<HHH", 0, 1, count)           # reserved, type=icon, count
    offset = 6 + 16 * count
    dir_entries = b""
    payloads = b""
    for size, blob in zip(sizes, entries, strict=True):
        dim = 0 if size >= 256 else size                 # ≥256 在目录项里写 0
        dir_entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        payloads += blob
        offset += len(blob)
    path.write_bytes(header + dir_entries + payloads)


def main(argv: list[str]) -> int:
    source = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_SOURCE
    if not source.is_file():
        raise SystemExit(f"找不到源图：{source}")
    # QPainter（画布合成）需要一个应用实例；这里用 QApplication 而不是 QGuiApplication ——
    # 离屏平台下 QGuiApplication 会直接段错误（实测），与测试里那套一致
    QApplication.instance() or QApplication([])
    ASSETS.mkdir(parents=True, exist_ok=True)
    blobs: list[bytes] = []
    for size in SIZES:
        # 小尺寸走"裁剪版"（更好认）；同时存一张"整图缩"的对比图，便于肉眼比谁的辨识度高
        cropped = size <= SMALL_CROP_MAX
        image = _render(source, size, cropped=cropped)
        blob = _png_bytes(image)
        blobs.append(blob)
        (ASSETS / f"icon-{size}.png").write_bytes(blob)
        if cropped:
            (ASSETS / f"icon-{size}-full.png").write_bytes(_png_bytes(_render(source, size)))
        if size == 256:
            (ASSETS / "icon.png").write_bytes(blob)
    picked = [blob for size, blob in zip(SIZES, blobs, strict=True) if size in ICO_SIZES]
    _write_ico(ASSETS / "icon.ico", picked, list(ICO_SIZES))
    for size, blob in zip(SIZES, blobs, strict=True):
        print(f"  {size:>3}px  {len(blob):>6} B"
              + ("（裁剪版，另存 icon-%d-full.png 供对比）" % size if size <= SMALL_CROP_MAX else ""))
    print(f"{source.name} → {ASSETS}/icon.png + icon-*.png + icon.ico"
          f"（PNG {len(SIZES)} 档：{', '.join(str(s) for s in SIZES)}；"
          f"ICO {len(ICO_SIZES)} 档：{', '.join(str(s) for s in ICO_SIZES)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
