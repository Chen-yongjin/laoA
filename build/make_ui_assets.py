#!/usr/bin/env python3
"""生成界面皮肤用的素材：**极淡的拉丝金属纹理**（背景条平铺图）。

为什么把生成脚本放进仓库
------------------------
与图标（`build/make_icon.py`）同一个理由：素材这种东西"当时随手做一张"最容易失传，
以后要调色调/调密度没人知道那张 png 是怎么来的。所以把**画法写成代码**，产物由它生成：

    python build/make_ui_assets.py        # 需要 Pillow（只改素材时才要，运行时不需要）

产出（都在 `src/laoa_trader/assets/ui/`，随包分发）：
    brushed-metal.png       512×512，普通屏用
    brushed-metal@2x.png    1024×1024，高 DPI（150% 缩放）用

设计取舍
--------
- **对比度极低**（每个像素与基准色的差 ≤ 4/255）：这是一条"背景条"的底纹，
  用户不该"看见花纹"，只该觉得这地方不是一块死板的纯色。对比一高就会变成
  "花里胡哨的皮肤"，正是用户不想要的那种。
- **画的是"横向的细丝"而不是逐像素噪点**：每一行切成 2~8 像素的小段，每段整体亮一点
  或暗一点 —— 这才是"拉丝"的样子（逐像素噪点更像电视雪花）。纵向不做渐变，
  所以 `repeat-x` 平铺时不会有明显的横向接缝。顺带一个好处：小段让相邻像素大量重复，
  PNG 能压得很好（逐像素噪点的图压不动，一张要几百 KB）。
- **色相略偏冷**（B 比 R 高 2 左右）：银灰偏蓝是"金属感"的通用做法，也不会与
  界面里红涨绿跌的语义色打架。
- 尺寸取 512/1024 的 2 的幂：平铺时不会出现半像素的错位（tile 与 tile 之间对不齐）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 只有手动跑这个脚本时才会遇到
    print("需要 Pillow：python -m pip install Pillow", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "laoa_trader" / "assets" / "ui"

#: 基准色（与主题里的"分区标题底"同一族：浅银灰）
BASE = (242, 243, 245)
#: 每行整体明暗的浮动幅度（拉丝感主要来自这里）
ROW_SHIFT = 2
#: 每一小段（一根"丝"）的明暗幅度
STREAK = 2.5
#: 一根丝的长度（像素）：短了像噪点、长了像色带
RUN_MIN, RUN_MAX = 2, 8
#: 固定随机种子：同样的脚本永远产出同样的图（否则每次重跑都会改一版素材）
SEED = 20260914


def build(size: int) -> Image.Image:
    """画一张 `size × size` 的拉丝金属平铺图（横向细丝，对比度极低）。"""
    rng = random.Random(SEED + size)     # 尺寸进种子：同样的脚本永远产出同样的图
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        # 每一行整体亮一点/暗一点 —— 这是"拉丝"的第一层
        delta = rng.uniform(-ROW_SHIFT, ROW_SHIFT)
        x = 0
        while x < size:
            # 一根丝：这一小段整体再偏一点，走完换一个新偏值
            delta += rng.uniform(-STREAK, STREAK)
            delta = max(-STREAK * 2, min(STREAK * 2, delta))   # 别让差值越漂越远
            run = rng.randint(RUN_MIN, RUN_MAX)
            color = (_clamp(BASE[0] + delta), _clamp(BASE[1] + delta),
                     _clamp(BASE[2] + delta + 1))    # 略偏冷一点，银灰偏蓝才有金属感
            for step in range(run):
                if x + step >= size:
                    break
                pixels[x + step, y] = color
            x += run
    return image


def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in (("brushed-metal.png", 512), ("brushed-metal@2x.png", 1024)):
        path = OUT_DIR / name
        # 存成**调色板 PNG**：噪声图用真彩 PNG 几乎压不动（一张 400KB），
        # 而这张图的颜色本来就只有十几个灰度 —— 调色板一存只剩几十 KB，
        # 打包体积与加载速度都更好，画质在这点对比度下没有任何区别。
        build(size).convert("P", palette=Image.Palette.ADAPTIVE, colors=24).save(
            path, optimize=True
        )
        print(f"已生成 {path.relative_to(ROOT)}（{size}×{size}，"
              f"{path.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
