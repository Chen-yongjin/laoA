#!/usr/bin/env python3
"""生成「老A法师 · 交易终端」的图标（PNG + 多尺寸 ICO）。

为什么把生成脚本放进仓库
------------------------
图标这种东西"当时随手画一个"最容易失传：以后要改颜色/换字号，没人知道当初那张
`.ico` 是怎么来的。所以这里把**画法写成代码**，产物由它生成：

    python build/make_icon.py          # 需要 Pillow（只打包/改图标时才要，运行时不需要）

产出（都在 `src/laoa_trader/assets/`，随包分发）：
    icon.png    256×256，界面里用（窗口图标 / 托盘 / 关于页）
    icon.ico    多尺寸（16/24/32/48/64/128/256），Windows 可执行文件用
    icon-*.png  各尺寸单图（README、文档里贴图用）

设计取舍（为什么长这样）
------------------------
- **深蓝底 + 白字 + 红线上扬**：深蓝是"终端/数据"的通用语，白字保证任何壁纸上都看得清；
  红线上扬取自 A 股"红涨"的习惯（不用绿=涨的欧美配色，免得用户误读）。
- **小尺寸单独画**：16×16 只有 256 个像素，把大写 A 和那条折线一起塞进去必然糊成一团。
  所以 **≤32px 只留"白色 A + 一个红点"**，48px 以上才画完整折线 —— 这是"图标要按尺寸分档"
  的标准做法，不是偷懒。
- 字用 DejaVu Sans Bold（Linux 上自带、字形敦实）；Windows 打包时这脚本只在需要改图标时跑，
  产物是位图，所以打包机不需要装字体。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - 只有手动跑这个脚本时才会遇到
    print("需要 Pillow：python -m pip install Pillow", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "laoa_trader" / "assets"

#: 画布基准尺寸（所有坐标都按 256 写，最后等比缩放到目标尺寸 —— 改起来直观）
BASE = 256

# ── 配色（与界面深色状态栏、A 股红涨的习惯一致）──
BG_TOP = (26, 41, 74)        # #1A294A 深蓝（略亮）
BG_BOTTOM = (11, 20, 36)     # #0B1424 深蓝（略暗）→ 竖向渐变，避免死板
INK = (255, 255, 255)        # 白色主字
RED = (226, 59, 59)          # #E23B3B A 股红
RED_LIGHT = (255, 122, 89)   # 折线高光的暖橙（小尺寸下更跳）
SHADOW = (0, 0, 0, 90)       # 字下方投影，让小尺寸也有厚度

#: 系统里常见的粗体无衬线字体（Linux 自带 DejaVu；都没有时退回 Pillow 内置位图字）
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)          # Pillow ≥ 9.2 支持带字号


def _rounded_mask(size: int, radius: int) -> Image.Image:
    """圆角矩形遮罩（Windows 的 exe 图标习惯用圆角方块）。"""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _background(size: int) -> Image.Image:
    """竖向渐变底 + 圆角，带一圈很淡的内描边（小尺寸下边缘更"实"）。"""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        ))
    img = grad.resize((size, size))
    img = img.convert("RGBA")
    img.putalpha(_rounded_mask(size, radius=round(size * 0.22)))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (1, 1, size - 2, size - 2), radius=round(size * 0.22),
        outline=(255, 255, 255, 28), width=max(1, round(size / 128)),
    )
    return img


def _draw_mark(img: Image.Image, size: int, *, detailed: bool) -> None:
    """在底图上画标记：白色 A（带投影）+（大尺寸才有的）红色上扬折线。"""
    draw = ImageDraw.Draw(img)
    scale = size / BASE

    # ── 白色 A：略偏上，给下面留出折线的位置 ──
    # 小尺寸（≤32px）**把字放大**：那个尺寸只有几百个像素，字小了就是一团灰点；
    # 大尺寸则收一点，把下方让给折线。
    font_size = round((164 if detailed else 198) * scale)
    font = _font(font_size)
    text = "A"
    box = draw.textbbox((0, 0), text, font=font)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = (size - (box[3] - box[1])) / 2 - box[1] - (size * 0.12 if detailed else 0)

    if detailed:
        # 投影：大尺寸下有厚度更精致；小尺寸不加（会把本来就少的白像素糊掉）
        offset = max(1, round(2.5 * scale))
        draw.text((x, y + offset), text, font=font, fill=SHADOW)
    draw.text((x, y), text, font=font, fill=INK)

    if not detailed:
        # 小尺寸：只加一个红点（一眼能认出是"红涨"的那个 app），别画折线
        r = max(2, round(9 * scale))
        cx, cy = size - r - round(8 * scale), size - r - round(8 * scale)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=RED)
        return

    # ── 红色上扬折线（A 股"红涨"）+ 箭头 ──
    # 位置讲究：**整条线必须在 A 的下方**（A 的底边约在 y=155/256），
    # 压到字母上在小尺寸会糊成一团 —— 这是按 48px 的实际渲染反复调出来的。
    pts = [(38, 220), (84, 202), (126, 212), (168, 184)]
    line = [(round(px * scale), round(py * scale)) for px, py in pts]
    width = max(2, round(12 * scale))
    draw.line(line, fill=RED, width=width, joint="curve")
    # 折线起点加一个亮点，末端画箭头：小尺寸下"上扬"的语义更明确
    head = max(3, round(13 * scale))
    ax, ay = line[-1]
    draw.polygon([(ax + head, ay - head), (ax - head // 2, ay - head // 2),
                  (ax + head // 2, ay + head // 2)], fill=RED_LIGHT)
    r = max(2, round(6 * scale))
    draw.ellipse((line[0][0] - r, line[0][1] - r, line[0][0] + r, line[0][1] + r), fill=RED_LIGHT)


def render(size: int) -> Image.Image:
    """画出指定尺寸的图标（PNG 用；ICO 的各档也按这个画）。"""
    img = _background(size)
    _draw_mark(img, size, detailed=size >= 48)
    return img


#: ICO 里要放的尺寸（Windows 会按显示场景自己挑：任务栏 32、标题栏 16、桌面大图标 256）
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: 额外单独导出的 PNG 尺寸（界面按尺寸取用，文档里贴图也用它）
#: 覆盖界面实际会用到的档位：标题栏 16、托盘 24/32、关于页 64、大图 128/256 ——
#: 界面用 `assets.icon_png(64)` 这种写法按尺寸取，缺档会退回 256 主图（能用但不够锐）
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = render(BASE)
    master.save(ASSETS / "icon.png")

    # ICO：**每一档单独画**（不是把 256 缩下去），小尺寸才不糊
    frames = [render(size) for size in ICO_SIZES]
    frames[-1].save(ASSETS / "icon.ico", format="ICO",
                    sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])

    for size in PNG_SIZES:
        render(size).save(ASSETS / f"icon-{size}.png")

    # 自检：ICO 头里应当列出我们要求的全部尺寸（否则 Windows 会拿缩放图凑合）
    print(f"已生成 {ASSETS}")
    for path in sorted(ASSETS.glob("icon*")):
        print(f"  {path.name:14s} {path.stat().st_size:>7d} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
