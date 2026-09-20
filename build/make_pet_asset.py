#!/usr/bin/env python3
"""把用户给的桌宠原图缩成随包素材。

为什么把生成脚本放进仓库
------------------------
与图标（`build/make_icon.py`）和皮肤素材（`build/make_ui_assets.py`）同一个理由：
"当时随手缩一张"最容易失传，以后要换尺寸/换素材没人知道那张 png 是怎么来的。

    # 平时不用带参数（默认就是工作区里那张 桌宠2.png）
    python build/make_pet_asset.py [源图路径]

**换素材只有一句话的步骤**（用户 2026-09-20 换过一次，以后换还是这三下）：

    ① 把新的 PNG 放到 `laoA/桌宠2.png`（或改用源图路径参数）
    ② 跑一次本脚本：`python build/make_pet_asset.py`
    ③ 重新打包（CI 会跑 PyInstaller，`assets/` 整个目录随包）

也就是说**只需要重新生成这张 png**：窗口要不要卡片底、怎么摆、点什么，
都是看这张图**有没有 alpha 通道**自动决定的（见下面第 1 条），不用改代码。

产出（随包分发，PyInstaller 的 `DATAS` 会把整个 `laoa_trader/assets/` 打进包）：

    src/laoa_trader/assets/pet.png    512×512

两个必须知道的取舍
------------------
1. **有 alpha 就直接贴、没有 alpha 才画卡片底**：
   * 用户 2026-09-18 给的第一版是 1536×1536 的 **8-bit RGB（没有 alpha）**，
     画面是"财神娃娃骑牛 + 红金花纹 + 四周闪光"（右下角还有 AI 生成的水印）——
     那种图硬抠会抠出一圈脏边，所以当时按**圆角卡片**呈现；
   * 用户 2026-09-20 换成了 **RGBA（真有透明通道）、没有背景花纹** 的同款角色图 ——
     于是桌宠变成"**自由站立的角色**"：窗口透明、不画卡片底/边框/阴影，图片直接贴在桌面上。
   `ui/desktop_pet.py` 里判断的就是 `QImage.hasAlphaChannel()`，
   所以将来再换回没有 alpha 的图也会自动回到卡片式（不需要改一行代码）。
2. **不用 Pillow**：这个仓库的运行依赖里没有 Pillow（`build/make_ui_assets.py`
   才需要它，而且只为改素材时装），而 **PySide6 本来就是依赖**。
   缩放要的是"高质量重采样"，`QImage.scaled(..., SmoothTransformation)` 就够了，
   顺带避免"改素材还得装一个新库"。
3. **缩放保留 alpha**：`QImage` 读进来是什么格式就按它缩（`SmoothTransformation` 对
   RGBA 会连 alpha 一起平滑），存 PNG 时不会把透明通道丢掉 —— 丢掉的话桌宠立刻变成
   一块黑/白方块（这是最容易悄悄发生的一步，所以 `make_asset()` 里会**断言**结果带 alpha）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 工作区里那份**当前**原图（2026-09-20 换成透明底那版；换图只改这一处 + 重跑一次）
DEFAULT_SOURCE = PROJECT_ROOT.parent / "桌宠2.png"
TARGET = PROJECT_ROOT / "src" / "laoa_trader" / "assets" / "pet.png"

#: 随包尺寸：桌宠窗口里按约 180 像素高显示，512 足够 2x 屏（再大只是白白让 exe 变胖）
SIZE = 512


def make_asset(source: Path, target: Path = TARGET, size: int = SIZE) -> Path:
    """把 `source` 缩成 `size`×`size` 的 PNG 写到 `target`（保持长宽比，居中）。

    需要 Qt：`QImage` 的加载/缩放/保存**不需要**事件循环，但某些平台下
    `QGuiApplication` 缺失会让插件路径告警，所以这里显式建一个（offscreen 平台）。
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage

    app = QGuiApplication.instance() or QGuiApplication([])      # noqa: F841 - 见 docstring
    image = QImage(str(source))
    if image.isNull():
        raise SystemExit(f"读不出图片：{source}")
    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    transparent_in = image.hasAlphaChannel()
    # 源图有 alpha、结果却没有 → 透明通道在保存这一步丢了。桌宠会立刻变成一块方块，
    # 而这种问题在界面上看着"也像张图"，很容易被当成"素材就是这样"放过去 —— 直接拦住。
    if transparent_in and not scaled.hasAlphaChannel():
        raise SystemExit(
            f"缩放后 alpha 通道丢了（{source.name} 本来是透明的）："
            "桌宠会变成一块方块，请检查 Qt 的图片插件"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if not scaled.save(str(target), "PNG"):
        raise SystemExit(f"写不出图片：{target}")
    print(f"{source.name} {image.width()}×{image.height()}"
          f"（{'透明底' if transparent_in else '无 alpha'}）"
          f" → {target.relative_to(PROJECT_ROOT)} {scaled.width()}×{scaled.height()}")
    return target


def main(argv: list[str]) -> int:
    source = Path(argv[1]).expanduser() if len(argv) > 1 else DEFAULT_SOURCE
    if not source.is_file():
        print(f"找不到源图：{source}", file=sys.stderr)
        return 1
    make_asset(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
