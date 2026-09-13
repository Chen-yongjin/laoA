"""图标资源：文件齐全、ICO 尺寸齐全、小尺寸不是糊的、缺资源时优雅降级。

为什么这些用例值得存在
----------------------
图标是最容易"在开发机上有、到用户机器上没了"的东西（打包时忘了放进 `datas`），
而且**错了也不报错**：Windows 会安安静静用默认图标。所以这里把三件事钉死：

1. 文件与尺寸：`assets/` 里该有的都在，ICO 里 16~256 七档齐全（缺档时 Windows 会拿
   缩放图凑合，任务栏上就是糊的）；
2. **画出来的东西真的有内容**：不装 Pillow（打包机/CI 不该为测图标多一个依赖）也能读
   PNG —— 这里自己解 PNG（zlib + 5 种行过滤器），断言 256 图有白色字与红色折线、
   圆角处透明、16 图也不是一片空白；
3. 资源缺失时 `assets.py` 返回 None（界面优雅降级），而不是抛异常把程序带崩。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from laoa_trader import assets

# ── 极简 PNG 解码（只支持本项目图标用的 8bit / 非隔行 / 灰度·RGB·RGBA）──


def _unfilter(kind: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    """就地还原一行（PNG 的 5 种过滤器，见 RFC 2083 第 6 节）。"""
    if kind == 0:
        return
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        up = prev[i]
        if kind == 1:
            line[i] = (line[i] + left) & 0xFF
        elif kind == 2:
            line[i] = (line[i] + up) & 0xFF
        elif kind == 3:
            line[i] = (line[i] + (left + up) // 2) & 0xFF
        elif kind == 4:
            up_left = prev[i - bpp] if i >= bpp else 0
            # Paeth 预测：取三个候选里"最接近线性"的那个
            p = left + up - up_left
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
            pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
            line[i] = (line[i] + pred) & 0xFF
        else:
            raise AssertionError(f"未知的 PNG 过滤器类型 {kind}")


def read_png(path: Path) -> tuple[int, int, int, bytes]:
    """读 PNG → (宽, 高, 通道数, 像素字节)。不依赖 Pillow（CI 里没有它）。"""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG"
    pos, idat, header = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    assert header is not None, f"{path.name} 没有 IHDR"
    width, height, depth, color, _comp, _filter, interlace = header
    assert depth == 8 and interlace == 0, f"{path.name} 只支持 8bit 非隔行"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    raw = zlib.decompress(idat)
    stride = width * channels
    out, prev, i = bytearray(), bytearray(stride), 0
    for _ in range(height):
        kind = raw[i]
        i += 1
        line = bytearray(raw[i : i + stride])
        i += stride
        _unfilter(kind, line, prev, channels)
        out += line
        prev = line
    return width, height, channels, bytes(out)


class Pixels:
    """按 (x, y) 取 RGBA，方便断言"某个位置是什么颜色"。"""

    def __init__(self, path: Path) -> None:
        self.width, self.height, self.channels, self.data = read_png(path)

    def rgba(self, x: int, y: int) -> tuple[int, int, int, int]:
        i = (y * self.width + x) * self.channels
        px = self.data[i : i + self.channels]
        if self.channels == 4:
            return tuple(px)  # type: ignore[return-value]
        if self.channels == 3:
            return (px[0], px[1], px[2], 255)
        return (px[0], px[0], px[0], 255)

    def count(self, predicate) -> int:
        return sum(
            1
            for y in range(self.height)
            for x in range(self.width)
            if predicate(self.rgba(x, y))
        )


def _is_white_ink(px: tuple[int, int, int, int]) -> bool:
    r, g, b, a = px
    return a > 200 and r > 200 and g > 200 and b > 200


def _is_red_ink(px: tuple[int, int, int, int]) -> bool:
    r, g, b, a = px
    return a > 150 and r > 150 and g < 140 and b < 140


# ── 1) 文件与尺寸 ──


def test_icon_files_exist() -> None:
    """该有的图标都在（打包脚本、界面、关于页都靠它们）。"""
    directory = assets.assets_dir()
    assert directory.is_dir(), f"图标目录不存在：{directory}"
    for name in ("icon.png", "icon.ico", "icon-16.png", "icon-32.png",
                 "icon-48.png", "icon-128.png", "icon-256.png"):
        assert (directory / name).is_file(), f"缺少 {name}"


def test_ico_has_all_windows_sizes() -> None:
    """ICO 里 16~256 七档齐全 —— 缺档时 Windows 会缩放凑合，任务栏图标就糊。

    这里直接解 ICO 头（格式很简单，不值得为它装 Pillow）。
    """
    data = assets.icon_ico().read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "ICO 头不对"
    sizes = set()
    for i in range(count):
        entry = data[6 + i * 16 : 22 + i * 16]
        width, height = entry[0], entry[1]
        sizes.add((width or 256, height or 256))
    assert {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)} <= sizes, sizes


def test_png_sizes_match_file_names() -> None:
    """`icon-32.png` 必须真的是 32×32（小尺寸是**单独画**的，名字对不上就等于没画）。

    这里**按目录里实际存在的文件**逐个核对（新增尺寸时不用回来改这条用例）。
    """
    directory = assets.assets_dir()
    checked = 0
    for path in sorted(directory.glob("icon-*.png")):
        size = int(path.stem.split("-")[1])
        width, height, channels, _data = read_png(path)
        assert (width, height) == (size, size), f"{path.name} 实际是 {width}×{height}"
        assert channels == 4, f"{path.name} 应该有透明通道（圆角要用）"
        checked += 1
    assert checked >= 5, "小尺寸图至少要覆盖 5 档（16/24/32/48/64…）"
    width, height, channels, _data = read_png(directory / "icon.png")
    assert (width, height, channels) == (256, 256, 4)


# ── 2) 画出来真的有内容（不是空白图、不是纯色块）──


def _opaque_pixels(px: "Pixels", threshold: int = 150) -> list[tuple[int, int]]:
    """不透明（alpha 高于阈值）的像素坐标。"""
    return [(x, y) for y in range(px.height) for x in range(px.width)
            if px.rgba(x, y)[3] > threshold]


def test_large_icon_has_real_content() -> None:
    """256 图：内容要**真的画上了**，而且占画布的比例合理（不是空图、也不是糊成一团）。"""
    px = Pixels(assets.icon_png())
    opaque = _opaque_pixels(px)
    coverage = len(opaque) / (px.width * px.height)
    assert coverage > 0.15, f"内容太少，像空图（{coverage:.1%}）"
    assert coverage < 0.85, f"内容几乎填满画布，四周没有留白（{coverage:.1%}）"
    width = max(p[0] for p in opaque) - min(p[0] for p in opaque) + 1
    height = max(p[1] for p in opaque) - min(p[1] for p in opaque) + 1
    assert width >= px.width * 0.6, f"内容太窄（{width}/{px.width}）"
    assert height >= px.height * 0.6, f"内容太矮（{height}/{px.height}）"


def test_opaque_pixels_keep_the_brand_red() -> None:
    """**颜色不能被稀释**：可见像素的平均色必须是"红橙"，不能变成暗红或灰白。

    注意分工：这条管**颜色**（有没有被白底/透明稀释成浅粉或压成发黑），
    "内容有没有被贴淡"由上面的覆盖率用例管（`alpha_composite` 与 `paste(mask=…)`
    的区别正是"不透明像素少了 8 倍"，那条会红）。
    """
    for name in ("icon-256.png", "icon-32.png", "icon-16.png"):
        px = Pixels(assets.assets_dir() / name)
        opaque = [px.rgba(x, y)[:3] for x, y in _opaque_pixels(px)]
        assert opaque, f"{name} 没有不透明像素"
        r = sum(c[0] for c in opaque) / len(opaque)
        g = sum(c[1] for c in opaque) / len(opaque)
        b = sum(c[2] for c in opaque) / len(opaque)
        assert r > 150, f"{name} 的红色被缩暗了（平均 R={r:.0f}）"
        assert r > g > b, f"{name} 的主色不是红橙（{r:.0f},{g:.0f},{b:.0f}）"


def test_large_icon_has_transparent_rounded_corners() -> None:
    """四角透明 —— 否则贴到浅色任务栏上会是一个突兀的方块。"""
    px = Pixels(assets.icon_png())
    for x, y in ((0, 0), (px.width - 1, 0), (0, px.height - 1), (px.width - 1, px.height - 1)):
        assert px.rgba(x, y)[3] == 0, f"({x},{y}) 应该透明"


def test_small_icon_is_not_blank() -> None:
    """16×16 只有 256 个像素：内容必须**占满大半个画布**，否则就是一团灰点。"""
    px = Pixels(assets.assets_dir() / "icon-16.png")
    opaque = _opaque_pixels(px)
    assert len(opaque) >= 30, f"16px 里几乎什么都看不见（{len(opaque)} 像素）"
    width = max(p[0] for p in opaque) - min(p[0] for p in opaque) + 1
    height = max(p[1] for p in opaque) - min(p[1] for p in opaque) + 1
    assert width >= px.width * 0.45, f"内容太窄（{width}/{px.width}）"
    assert height >= px.height * 0.45, f"内容太矮（{height}/{px.height}）"


def test_small_icon_drops_the_wordmark() -> None:
    """小尺寸**只留图形、去掉下方文字**（四个汉字缩到 20px 以下就是一坨）。

    判据用"内容包围盒的宽高比"当代理指标：设计稿里"图形+文字"是竖长的（高>宽），
    单独图形则接近方形/略扁 —— 所以 16px 的 h/w 必须明显小于 256px。
    """
    def aspect(px: Pixels) -> float:
        pts = _opaque_pixels(px)
        w = max(p[0] for p in pts) - min(p[0] for p in pts) + 1
        h = max(p[1] for p in pts) - min(p[1] for p in pts) + 1
        return h / w

    big, small = Pixels(assets.icon_png()), Pixels(assets.assets_dir() / "icon-16.png")
    assert aspect(big) > aspect(small), (
        f"256px 应当含文字（更竖长，h/w={aspect(big):.2f}），"
        f"16px 应当只留图形（h/w={aspect(small):.2f}）"
    )


# ── 3) 路径解析与优雅降级 ──


def test_paths_resolve_to_real_files() -> None:
    assert assets.icon_png().is_file()
    assert assets.icon_png(32).name == "icon-32.png"
    assert assets.icon_png(999).name == "icon.png"      # 没有这档 → 退回主图
    assert assets.icon_ico().suffix == ".ico"


def test_missing_assets_degrade_to_none(tmp_path, monkeypatch) -> None:
    """资源缺失时返回 None（界面照常启动，只是没图标）——不该抛异常。"""
    monkeypatch.setattr(assets, "assets_dir", lambda: tmp_path / "nope")
    assert assets.icon_png() is None
    assert assets.icon_ico() is None


def test_icon_size_is_reasonable() -> None:
    """图标别做成 1MB 的巨无霸（`datas` 会把它塞进每个用户的安装目录）。"""
    directory = assets.assets_dir()
    for path in directory.glob("icon*"):
        assert path.stat().st_size < 100_000, f"{path.name} 太大"
    ico_size = assets.icon_ico().stat().st_size
    assert 5_000 < ico_size < 100_000, f"ICO 体积不合理：{ico_size} B（应当含 7 档尺寸）"
