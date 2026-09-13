"""随包资源（图标）的位置解析 —— 全程序只认这一处。

为什么单独一个模块
------------------
"图标在哪"这件事有三种运行形态，各不一样：

1. **源码运行**（`PYTHONPATH=src python -m laoa_trader`）：就在本文件旁边 `assets/`；
2. **`pip install -e .` + 打包**：PyInstaller 把 `datas` 解到 `_MEIPASS` 下，
   目录结构与源码一致（见 `build/laoa_trader.spec` 的 `DATAS`）；
3. **onedir 产物**：`__file__` 指向解包后的包目录，第 1 条同样成立。

如果界面、托盘、打包脚本各写一份查找逻辑，改目录时就一定会漏掉一处（图标"在开发机上有、
到用户机器上没了"是这类代码的经典毛病）。所以集中在这里，并且**找不到就返回 None**，
让调用方优雅降级（没图标照样能跑）而不是抛异常。

图标本身是 `build/make_icon.py` 生成的（画法也留在仓库里，免得以后没人知道怎么改）。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 资源目录名（与本文件同级）
_ASSETS_NAME = "assets"


def assets_dir() -> Path:
    """资源目录（可能不存在 —— 调用方要自己判断）。"""
    beside = Path(__file__).resolve().parent / _ASSETS_NAME
    if beside.is_dir():
        return beside
    # onefile / 自定义 datas 目标路径时，PyInstaller 会把资源解到 _MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        packed = Path(meipass) / "laoa_trader" / _ASSETS_NAME
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
