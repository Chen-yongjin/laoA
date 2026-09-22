"""随包资源（图标）的位置解析 —— 全程序只认这一处。

为什么单独一个模块
------------------
"图标在哪"这件事有三种运行形态，各不一样：

1. **源码运行**（`PYTHONPATH=src python -m laoa_trader`）：就在本文件旁边 `assets/`；
2. **PyInstaller onedir**：`datas` 被解到 `_MEIPASS` 下，目录结构与源码一致
   （见 `build/laoa_trader.spec` 的 `DATAS`）；
3. **Nuitka standalone**（2026-09-22 起的默认构建方式）：`--include-data-dir` 把
   `assets/` 放在 **exe 同级**的 `laoa_trader/assets/`，于是第 1 条照样成立
   （`__file__` 指向产物目录里的那个路径）。

"我是哪种形态、随包数据在哪"由 `laoa_trader.runtime` 一处回答（见那个模块的说明）——
换成 Nuitka 时这里差点成为漏网之鱼：`_MEIPASS` 在 Nuitka 下不存在，
写死它就会"程序起来了、图标全没了"。

如果界面、托盘、打包脚本各写一份查找逻辑，改目录时就一定会漏掉一处（图标"在开发机上有、
到用户机器上没了"是这类代码的经典毛病）。所以集中在这里，并且**找不到就返回 None**，
让调用方优雅降级（没图标照样能跑）而不是抛异常。

图标本身是 `build/make_icon.py` 生成的（画法也留在仓库里，免得以后没人知道怎么改）。
"""

from __future__ import annotations

from pathlib import Path

from laoa_trader import runtime

#: 资源目录名（与本文件同级）
_ASSETS_NAME = "assets"


def assets_dir() -> Path:
    """资源目录（可能不存在 —— 调用方要自己判断）。

    查找顺序（两者都在就先用"本文件旁边"那个，它与源码目录结构一致）：
    1. 本文件旁边 `assets/`（源码运行、Nuitka 产物都命中）；
    2. `runtime.bundle_dir()/laoa_trader/assets/`（PyInstaller 的 `_MEIPASS` 落点）。
    """
    beside = Path(__file__).resolve().parent / _ASSETS_NAME
    if beside.is_dir():
        return beside
    packed = runtime.bundle_dir() / "laoa_trader" / _ASSETS_NAME
    if packed.is_dir():
        return packed
    return beside


def _first(names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = assets_dir() / name
        if path.is_file():
            return path
    return None


def icon_png(size: int | None = None) -> Path | None:
    """界面用的 PNG 图标路径（窗口 / 托盘 / 关于页）。

    Args:
        size: 指定尺寸时优先用 `icon-<size>.png`（小尺寸是单独画的，缩放会糊）；
            没有对应文件就退回主图 `icon.png`。

    Returns:
        文件路径；资源缺失时 `None`（调用方应优雅降级）。
    """
    if size:
        picked = _first((f"icon-{int(size)}.png", "icon.png"))
        return picked
    return _first(("icon.png",))


def icon_ico() -> Path | None:
    """Windows 可执行文件用的多尺寸 ICO 路径（打包脚本用）。"""
    return _first(("icon.ico",))


def pet_png() -> Path | None:
    """桌宠形象（用户 2026-09-18 给的图，缩到 512×512 后随包）路径；缺失返回 None。

    为什么单独一个函数、而不是让界面自己拼路径：与图标同一个理由 ——
    "素材在哪"只有这一处说了算（源码运行 / `pip install -e` / PyInstaller 解包
    三种形态各写一份必然漏一处）。**找不到时返回 None，桌宠要降级成自己画的小家伙**，
    而不是让桌面宠物把主界面拖死（它是锦上添花的东西）。

    原图是 8-bit RGB、没有 alpha（见 `build/make_pet_asset.py` 的说明），
    所以桌宠按**圆角卡片**呈现；将来换透明底素材时只换这个文件、不用改画法。
    """
    return _first(("pet.png",))


def ui_asset(name: str) -> Path | None:
    """界面素材（皮肤用的纹理图等）路径；**找不到返回 None**（界面必须优雅降级）。

    为什么不复用 `icon_png()`：图标是"按尺寸取一张"，界面素材是"按名字取一张"；
    混在一起以后一定会有人以为界面素材也分 16/32/48 档。
    素材放在 `assets/ui/` 下，由 `build/make_ui_assets.py` 生成 —— 与图标同一个理由：
    画法留在仓库里，改色调、改密度只要改脚本重跑一遍。

    注意：QSS 的 `url()` 要**绝对路径**（打包后相对路径会解析到别处），
    调用方拿到 Path 之后用 `as_posix()` 再拼进样式表。
    """
    if not name:
        return None
    path = assets_dir() / "ui" / name
    return path if path.is_file() else None
