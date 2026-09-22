"""运行形态与"随包文件在哪"—— 全程序**只认这一处**。

为什么必须有这个模块
--------------------
2026-09-22 主人拍板：打包方式从 PyInstaller 换成 **Nuitka 编译**（把 Python 编成原生
exe，源码不能被解包还原），PyInstaller 只作备用。于是"我现在是哪种形态"从两种变成三种，
而**三种形态下"随包数据在哪"完全不同**：

| 形态 | 随包数据（图标 / 随包公式 / 示例配置） | 用户可写目录（config、formulas） |
|---|---|---|
| 源码运行 | 仓库里（`laoA/formulas/`、`src/laoa_trader/assets/`） | 仓库根 |
| PyInstaller onedir | `sys._MEIPASS`（解包目录） | exe 同级 |
| Nuitka standalone | **exe 同级**（Nuitka 把 data 放在产物目录里） | exe 同级 |

这三种判断原先散在 `assets.py` / `formulas.py` / `config.py` 里各写一份
（都只认 PyInstaller 的 `sys._MEIPASS`），换成 Nuitka 之后每一处都会静默用错路径 ——
症状是"程序能起来，但图标没了 / 随包公式列表空了 / 配置跑到别处去了"，而且
**开发机上（源码运行）永远看不出来**。所以这里收成一份，别处只调用。

Nuitka 是怎么认出来的
---------------------
Nuitka 编译后的**每个模块**里都会多一个 `__compiled__` 属性（源码运行时不存在），
所以 `"__compiled__" in globals()` 就是"我现在是被编译过的"最直接证据；
`sys.frozen` 也顺带认（Nuitka 与 PyInstaller 都可能设它），三条判据取并集，
免得依赖某一个版本的实现细节。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 允许测试注入 exe 路径（Nuitka / PyInstaller 产物里 `sys.executable` 就是那个 exe）
_EXE_OVERRIDE: list[Path] = []


def is_pyinstaller() -> bool:
    """是不是 PyInstaller 打出来的（它会把数据解到 `sys._MEIPASS`）。"""
    return bool(getattr(sys, "_MEIPASS", None))


def is_nuitka() -> bool:
    """是不是 Nuitka 编译出来的（模块里有 `__compiled__`）。"""
    return "__compiled__" in globals()


def is_frozen() -> bool:
    """是不是"打包/编译后的产物"（源码运行返回 False）。

    三项取并集的理由：PyInstaller 一定有 `_MEIPASS`；Nuitka 一定有 `__compiled__`；
    两者都可能设 `sys.frozen`。任何一个为真就按"产物"处理 —— 宁可多认，
    也别在某个版本上退回"当成源码运行"（那会让用户可写目录跑到程序安装目录里）。
    """
    return is_pyinstaller() or is_nuitka() or bool(getattr(sys, "frozen", False))


def kind() -> str:
    """运行形态：`pyinstaller` / `nuitka` / `source`（给 `--doctor` 与日志用）。"""
    if is_pyinstaller():
        return "pyinstaller"
    if is_nuitka():
        return "nuitka"
    if getattr(sys, "frozen", False):
        # 既不是 PyInstaller 也不是 Nuitka，但确实被冻结了（别的打包器 / 未来换工具）：
        # 按"产物"处理路径，名字如实写出来，排查时不会认错。
        return "frozen"
    return "source"


def _repo_root() -> Path:
    """仓库根（`laoA/`）：本文件在 `laoA/src/laoa_trader/runtime.py`。

    只在**源码运行**时用得到；产物运行时 `__file__` 指向解包/产物目录，
    算出来的"仓库根"没有意义（所以下面只在 source 分支调用它）。
    """
    return Path(__file__).resolve().parents[2]


def exe_dir() -> Path:
    """运行中的 exe 所在目录（源码运行时退化为仓库根）。

    用户可写的东西（`config.toml`、`formulas/`、日志）在产物形态下都放这里 ——
    "双击 exe 就看得见、能备份、能发给别人"是本项目一贯的取舍。
    """
    if _EXE_OVERRIDE:
        return _EXE_OVERRIDE[0]
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _repo_root()


def bundle_dir() -> Path:
    """**随包只读数据**所在目录（图标、随包公式、示例配置）。

    * PyInstaller：`sys._MEIPASS`（解包出来的临时目录）；
    * Nuitka：**exe 同级**（`--include-data-dir` 的落点就是产物目录）；
    * 源码运行：仓库根（`laoA/`）。
    """
    if is_pyinstaller():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return exe_dir()


def describe() -> str:
    """一行中文说明（`--doctor`、关于页、日志用）。"""
    labels = {
        "source": "源码运行",
        "pyinstaller": "PyInstaller 打包版",
        "nuitka": "Nuitka 编译版（原生 exe）",
        "frozen": "打包版",
    }
    return labels.get(kind(), kind())


def summary_lines() -> list[str]:
    """自检报告里的几行路径（`--doctor`）：出问题时用户直接复制这几行给我即可。"""
    return [
        f"运行形态    : {describe()}",
        f"程序位置    : {exe_dir()}",
        f"随包资源    : {bundle_dir()}",
    ]
