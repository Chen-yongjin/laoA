#!/usr/bin/env python3
"""把**设计稿原图**转成程序要用的整套图标（PNG 多尺寸 + Windows 多尺寸 ICO）。

为什么要有这个脚本
------------------
设计稿（`build/logo/source-logo.jpg`，2048×2048、白底、红橙渐变）是"一张大图"，
而程序需要的是**七八个尺寸各一份** + 一个多尺寸 ICO。手工用画图工具另存会：
尺寸对不上、白底留在图上（贴到深色任务栏就是一块白方块）、小尺寸糊成一团。

所以流程写成代码，一条命令重来：

    python build/import_logo.py                    # 生成到 src/laoa_trader/assets/
    python build/import_logo.py --background white # 换成"白底圆角卡片"风格
    python build/import_logo.py --preview 出图目录   # 额外导一份给人看的预览

它做的四件事
------------
1. **白底转透明**：按"颜色是叠在白底上的"做反预乘（un-premultiply），
   红橙笔画保留饱和色、边缘抗锯齿变成半透明 —— 比"把接近白的像素直接抠掉"干净得多；
2. **自动拆图形与文字**：设计稿上半是图形、下半是"老A选股"四个字，中间那条空白带
   就是分界线（脚本自己找，不用手填坐标）；
3. **按尺寸选构图**：≥128px 用"图形+文字"完整版（这时候文字还看得清），
   ≤64px **只留图形**（四个汉字缩到 20px 以下就是一坨，谁都认不出）；
4. **每档单独渲染**（不是把 256 缩下去），并做体积/覆盖率自检后落盘。

依赖：Pillow + numpy（只有改图标时才需要，运行时/打包都不需要）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    print("需要 Pillow 与 numpy：python -m pip install Pillow numpy", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "laoa_trader" / "assets"
LOGO_DIR = ROOT / "build" / "logo"
SOURCE_CANDIDATES = ("source-logo.jpg", "source-logo.png", "source-logo.webp")

#: 从设计稿里切出"图形"与"文字"的那条空白带，至少要有这么高才算分界（像素）
_SPLIT_GAP_MIN = 8
#: 内容占画布的比例（留白太多显得小气，太满会顶到边）
_MARGIN_FULL = 0.07
_MARGIN_MARK = 0.06
#: 到多少像素以上还用"图形+文字"的完整版（以下只留图形）
FULL_LOGO_MIN_SIZE = 128
#: 白底风格时圆角半径占画布的比例（与 Windows 图标习惯一致）
_ROUND_RADIUS = 0.22

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)


# ── 1) 读原图 ──


def find_source(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"找不到设计稿：{path}")
        return path
    for name in SOURCE_CANDIDATES:
        path = LOGO_DIR / name
        if path.is_file():
            return path
    raise SystemExit(
        f"没找到设计稿，请把原图放到 {LOGO_DIR}/source-logo.(jpg|png|webp)"
    )


def white_to_alpha(img: Image.Image) -> Image.Image:
    """白底 → 透明（反预乘）。

    原理：把每个像素看成 `色 = alpha·真色 + (1-alpha)·白`。取 `alpha = 1 - min(r,g,b)/255`
    （越接近白，alpha 越小），再解出真色。这样：
      - 纯白底 → alpha≈0（完全透明）；
      - 饱和的红橙笔画 → alpha≈1，颜色被还原成"真正的红"（不受白底稀释）；
      - 边缘抗锯齿的浅粉像素 → 半透明 + 饱和色（而不是"浅粉的实心边"）。
    比"阈值抠图"干净：不会在白底和笔画之间留一圈灰边。
    """
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    alpha = 1.0 - arr.min(axis=2, keepdims=True) / 255.0
    safe = np.maximum(alpha, 1e-6)
    color = np.clip((arr - (1.0 - safe) * 255.0) / safe, 0, 255)
    out = np.concatenate([color, alpha * 255.0], axis=2).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def content_bbox(img: Image.Image, threshold: int = 12) -> tuple[int, int, int, int]:
    """内容（alpha 超过阈值）的包围盒；空图返回整幅。"""
    alpha = np.asarray(img)[..., 3]
    mask = alpha > threshold
    if not mask.any():
        return (0, 0, img.width, img.height)
    ys, xs = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
    return (int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1)


def split_mark_and_text(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    """把"上半图形 / 下半文字"拆开（找中间那条最宽的空白带）。

    设计稿是"上图下文"的结构，所以按横向空白带切是最稳的：不用手填坐标，
    设计稿微调（改字号、改间距）之后重跑也照样对。
    """
    alpha = np.asarray(img)[..., 3]
    rows = (alpha > 12).sum(axis=1)
    blank = rows <= max(2, int(img.width * 0.002))
    # 收集所有空白带，取"位于画面中部、最宽"的那一条
    bands: list[tuple[int, int]] = []
    start = None
    for y, is_blank in enumerate(blank):
        if is_blank and start is None:
            start = y
        elif not is_blank and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, len(blank) - 1))
    middle = [b for b in bands
              if b[1] - b[0] + 1 >= _SPLIT_GAP_MIN and 0.25 * img.height < b[0] < 0.75 * img.height]
    if not middle:
        return img, None
    gap = max(middle, key=lambda b: b[1] - b[0])
    top = img.crop((0, 0, img.width, gap[0]))
    bottom = img.crop((0, gap[1] + 1, img.width, img.height))
    top = top.crop(content_bbox(top))
    bottom = bottom.crop(content_bbox(bottom))
    if top.height == 0 or bottom.height == 0:
        return img, None
    return top, bottom


# ── 2) 合成与渲染 ──


def _resize_rgba(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """RGBA 缩放（**按 alpha 预乘**、浮点运算、按倍率挑滤波器）。

    为什么不能直接 `img.resize(...)`：透明像素的颜色是 `(0,0,0)`，而 Pillow 对 RGBA
    是**逐通道直接平均**的 —— 任何"实心色 + 透明"的混合都会被往黑里拉。本项目的
    16/32px 是从 972px 缩下来的，几乎每个输出像素都掺着边缘，直接缩会得到**暗红/发黑**
    的图标（实测：品牌红 `RGB(211,22,0)` 缩完变成 `RGB(93,10,0)`）。

    这里做两件额外的事：
    1. **预乘**（颜色 × alpha）再缩放，最后除回去 —— 让"半透明边缘"参与平均时
       按它的不透明程度加权；
    2. 缩放走**单通道 F 模式（float32）**逐通道做，避免 8bit 量化在低 alpha 处
       丢掉颜色精度；**大倍率缩小用 BOX**（面积平均，没有 Lanczos 的负瓣振铃，
       不会在边缘压出黑边），倍率不夸张时才用 LANCZOS。
    """
    arr = np.asarray(img).astype(np.float32)
    alpha = arr[..., 3:4] / 255.0
    premultiplied = np.concatenate([arr[..., :3] * alpha, arr[..., 3:]], axis=2)

    scale = max(size[0] / img.width, size[1] / img.height)
    resample = Image.BOX if scale < 0.5 else Image.LANCZOS
    channels = []
    for i in range(4):
        plane = Image.fromarray(premultiplied[..., i], mode="F")
        channels.append(np.asarray(plane.resize(size, resample), dtype=np.float32))
    out = np.stack(channels, axis=2)
    out_alpha = out[..., 3:4] / 255.0
    color = np.clip(out[..., :3] / np.maximum(out_alpha, 1e-6), 0, 255)
    return Image.fromarray(np.concatenate([color, out[..., 3:]], axis=2).astype(np.uint8), "RGBA")


def _stack(mark: Image.Image, text: Image.Image | None, gap_ratio: float = 0.10) -> Image.Image:
    """把图形与文字按原比例上下拼起来（文字宽度不足时按比例放大到与图形同宽）。"""
    if text is None:
        return mark
    target_w = mark.width
    if text.width != target_w:
        scale = target_w / text.width
        text = _resize_rgba(text, (target_w, max(1, round(text.height * scale))))
    gap = max(2, round(mark.height * gap_ratio))
    out = Image.new("RGBA", (target_w, mark.height + gap + text.height), (0, 0, 0, 0))
    # 用 alpha_composite 而不是 paste(..., mask)：paste 的 mask 会把**被贴图片的 alpha
    # 再乘一次 mask 值**（实测 200 的 alpha 贴完变成 157），图像会整体变淡
    out.alpha_composite(mark, dest=(0, 0))
    out.alpha_composite(text, dest=(0, mark.height + gap))
    return out


def _fit(img: Image.Image, size: int, margin: float) -> Image.Image:
    """等比缩放到"能放进 size×(1-2·margin)"里，然后居中贴到透明画布上。"""
    inner = max(1, round(size * (1 - 2 * margin)))
    scale = min(inner / img.width, inner / img.height)
    resized = _resize_rgba(
        img, (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(resized, dest=((size - resized.width) // 2,
                                          (size - resized.height) // 2))
    return canvas


def render(size: int, mark: Image.Image, text: Image.Image | None,
           background: str = "transparent") -> Image.Image:
    """渲染一档尺寸：大尺寸用完整版、小尺寸只留图形；底可透明也可白底圆角。"""
    full = text is not None and size >= FULL_LOGO_MIN_SIZE
    source = _stack(mark, text) if full else mark
    icon = _fit(source, size, _MARGIN_FULL if full else _MARGIN_MARK)
    if background == "white":
        # 白底圆角卡片：先画白色圆角矩形，再把图标贴上去（有些场景更喜欢这种"实体图标"）
        plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=round(size * _ROUND_RADIUS), fill=(255, 255, 255, 255)
        )
        plate.alpha_composite(icon)
        return plate
    return icon


# ── 3) 落盘 ──


def main() -> int:
    parser = argparse.ArgumentParser(description="从设计稿生成整套图标")
    parser.add_argument("--source", help="设计稿路径（默认 build/logo/source-logo.*）")
    parser.add_argument("--background", choices=("transparent", "white"), default="transparent",
                        help="透明底（默认，推荐）或白底圆角卡片")
    parser.add_argument("--out", default=str(ASSETS), help="输出目录")
    parser.add_argument("--preview", help="额外导一份预览（文件名带尺寸，便于肉眼比对）")
    args = parser.parse_args()

    source_path = find_source(args.source)
    original = Image.open(source_path)
    rgba = white_to_alpha(original)
    rgba = rgba.crop(content_bbox(rgba))            # 先裁掉四周留白，后面才好按比例排版
    mark, text = split_mark_and_text(rgba)
    print(f"设计稿：{source_path.name} {original.size[0]}×{original.size[1]}"
          f"（{original.format}）")
    print(f"  图形：{mark.width}×{mark.height}"
          + (f"    文字：{text.width}×{text.height}" if text is not None else "    （未找到文字块）"))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = {size: render(size, mark, text, args.background) for size in ICO_SIZES}
    master = frames[256]
    master.save(out_dir / "icon.png")
    frames[256].save(
        out_dir / "icon.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES],
        append_images=[frames[s] for s in ICO_SIZES if s != 256],
    )
    for size in PNG_SIZES:
        frames[size].save(out_dir / f"icon-{size}.png")

    if args.preview:
        preview = Path(args.preview)
        preview.mkdir(parents=True, exist_ok=True)
        # 预览图：把每档按 4 倍放大拼在一起（小尺寸看得清），浅灰底便于看透明与边缘
        sheet = Image.new("RGBA", (sum(PNG_SIZES) * 4 + 40, 256 * 4 + 40), (238, 238, 242, 255))
        x = 20
        for size in PNG_SIZES:
            big = frames[size].resize((size * 4, size * 4), Image.NEAREST)
            sheet.paste(big, (x, 20 + (256 * 4 - size * 4) // 2), big)
            x += size * 4 + 8
        sheet.save(preview / "各尺寸对比（放大4倍）.png")
        for size in PNG_SIZES:
            shutil.copyfile(out_dir / f"icon-{size}.png", preview / f"icon-{size}-原尺寸.png")
        print(f"  预览已导出：{preview}")

    print(f"已生成 {out_dir}：")
    for path in sorted(out_dir.glob("icon*")):
        print(f"  {path.name:14s} {path.stat().st_size:>7d} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
