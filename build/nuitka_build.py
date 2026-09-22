"""Nuitka 构建脚本（2026-09-22 起的主构建方式）。

用法（在 Windows 上；Linux 上也会构建出 Linux 版，用来验证参数是对的）：

    python build/nuitka_build.py                # 构建并把产物摆成 dist/LaoniuTrader/老牛选股.exe
    python build/nuitka_build.py --keep-going   # 出错时保留中间目录（排查用）

为什么从 PyInstaller 换成 Nuitka
--------------------------------
主人 2026-09-22 拍板：「用 Nuitka/Cython 把程序编译一遍，不再加其它壳」。
PyInstaller 只是把 `.pyc` 打进包里，用解包工具几分钟就能还原出源码；
Nuitka 把 Python 编译成 C 再编成原生 exe，逆向门槛高一个量级，而且**不需要**
再叠一层商业壳（那些壳会明显抬高杀软误报率）。`build/laoa_trader.spec` 与
workflow 里的 PyInstaller 步骤**都保留**，作为备用路径（Nuitka 哪个版本翻车时可以退回去）。

产物形态与 PyInstaller 版**逐项对齐**（这是硬要求，否则客户下载说明、CI 检查、
Release 附件全要跟着改）：
* 目录：`dist/LaoniuTrader/`
* 可执行文件：`老牛选股.exe`（Windows）/ `老牛选股`（其它平台）
* 随包数据（`config.example.toml`、`README.md`、`快速验收.md`、
  `laoa_trader/assets/`、`formulas/`）放在**产物目录里**，
  位置与 `build/laoa_trader.spec` 的 `DATAS` 一一对应 ——
  路径解析统一走 `laoa_trader.runtime`，别在这里另发明一套。

为什么先构建到临时目录、再搬到 `dist/`
--------------------------------------
Nuitka 的产物目录名由主模块名决定（`launcher.dist` 这种），而我们要求目录名是
`LaoniuTrader`、exe 名是中文 `老牛选股`。与其跟 Nuitka 的命名规则较劲，
不如构建完做一次确定性搬运：搬运逻辑集中在本脚本，CI 与本地行为一致。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
#: 构建中间目录（每次重建；`--keep-going` 时保留下来看日志）
STAGING = PROJECT_ROOT / "build" / "nuitka-out"
#: 最终产物目录名（**ASCII**：解压/命令行/别的工具里中文名容易乱码）
DIST_NAME = "LaoniuTrader"
#: 可执行文件名（中文：用户在资源管理器里双击的那一个）
EXE_NAME = "老牛选股"

#: 随包数据：与 `build/laoa_trader.spec` 的 DATAS 一一对应（源 → 产物内的相对路径）
DATA_FILES: tuple[tuple[Path, str], ...] = (
    (PROJECT_ROOT / "config.example.toml", "config.example.toml"),
    (PROJECT_ROOT / "README.md", "README.md"),
    (PROJECT_ROOT / "快速验收.md", "快速验收.md"),
)
DATA_DIRS: tuple[tuple[Path, str], ...] = (
    (SRC / "laoa_trader" / "assets", "laoa_trader/assets"),
    # 随包策略（示例/内置公式）：解到产物目录的 formulas/，首次运行由
    # `formulas.formula_dir()` 复制/补齐到 **exe 同级的** formulas/（那就是用户目录）
    (PROJECT_ROOT / "formulas", "formulas"),
)

#: 不用编进去的重型/无关依赖（与 spec 的 EXCLUDES 同口径，省时间也省体积）
NOFOLLOW = (
    "pytest",
    "PyInstaller",
    "matplotlib",
    "scipy",
    "IPython",
    "notebook",
    "tkinter",
    "test",              # 标准库的 test 包（与 spec 的 excludes 对齐）
    "unittest",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
)

ICON_ICO = SRC / "laoa_trader" / "assets" / "icon.ico"


def build_command(python: str) -> list[str]:
    """拼出 Nuitka 命令行（单独一个函数，便于测试与人工复核参数）。"""
    cmd: list[str] = [
        python, "-m", "nuitka",
        str(PROJECT_ROOT / "build" / "launcher.py"),
        "--standalone",
        "--assume-yes-for-downloads",      # CI 上要它自动下载 depends/编译器组件
        "--enable-plugin=pyside6",         # Qt 插件与 DLL 由官方插件负责
        "--include-package=laoa_trader",   # 本项目全部模块（含懒导入那几个）
        f"--output-dir={STAGING}",
        # 版本信息：属性里看得出来这是哪个产品哪个版本（也方便用户报障）
        "--product-name=老牛选股",
        "--product-version=1.1.0",
        "--file-description=老牛选股助手（行情软件辅助工具）",
        # 编译期优化：去掉断言与 docstring 相关的开销；`__doc__` 我们**要**保留
        # （策略编辑器的帮助文案、函数的"是什么"提示都读它），所以不加 --python-flag=-OO
        "--python-flag=-O",
    ]
    for source, target in DATA_FILES:
        cmd.append(f"--include-data-files={source}={target}")
    for source, target in DATA_DIRS:
        cmd.append(f"--include-data-dir={source}={target}")
    for name in NOFOLLOW:
        cmd.append(f"--nofollow-import-to={name}")
    if sys.platform == "win32":
        # 桌面程序：不要黑框
        cmd.append("--windows-console-mode=disable")
        if ICON_ICO.is_file():
            cmd.append(f"--windows-icon-from-ico={ICON_ICO}")
        cmd.append(f"--output-filename={EXE_NAME}.exe")
    else:
        cmd.append(f"--output-filename={EXE_NAME}")
    return cmd


def _staged_dist_dir() -> Path | None:
    """Nuitka 产出的 `<主模块>.dist` 目录（名字随主模块，别写死）。"""
    candidates = [p for p in STAGING.glob("*.dist") if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def stage_into_dist(dist_dir: Path, *, exe_suffix: str,
                    dist_root: Path | None = None) -> Path:
    """把 Nuitka 的产物目录搬到 `dist/LaoniuTrader/`，并把 exe 改成中文名。

    为什么这一步不能省：Nuitka 用主模块名（`launcher.exe`）命名产物，
    而我们对客户承诺的是 `LaoniuTrader/老牛选股.exe`（下载说明、快捷方式、
    文档里的路径都按它写）。搬运比跟 Nuitka 的命名规则较劲可靠。
    """
    target = (dist_root or (PROJECT_ROOT / "dist")) / DIST_NAME
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dist_dir), str(target))

    # 改名：产物里的可执行文件可能叫 launcher.exe（或带 .bin 后缀）
    exe_target_name = f"{EXE_NAME}{exe_suffix}"
    current = target / exe_target_name
    if not current.is_file():
        found = [p for p in target.iterdir()
                 if p.is_file() and (p.suffix == exe_suffix or p.suffix == "")]
        if not found:
            raise SystemExit(f"❌ 产物目录里找不到可执行文件：{target}")
        found[0].rename(current)
    return current


def _dir_size_mb(path: Path) -> float:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 Nuitka 构建「老牛选股」")
    parser.add_argument("--keep-going", action="store_true",
                        help="构建失败时保留中间目录（排查用）")
    parser.add_argument("--print-command", action="store_true",
                        help="只打印将要执行的 Nuitka 命令（不构建）")
    args = parser.parse_args(argv)

    cmd = build_command(sys.executable)
    if args.print_command:
        print(" ".join(cmd))
        return 0

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("用 Nuitka 构建（编译成原生 exe；比 PyInstaller 慢很多，属正常）")
    print("=" * 70)
    started = time.monotonic()
    # 直接继承 stdin/stdout/stderr：Nuitka 的输出很长，CI 里要能完整看到失败原因
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False,
                            env={**os.environ, "PYTHONUTF8": "1"})
    if result.returncode != 0:
        print(f"❌ Nuitka 构建失败（退出码 {result.returncode}）")
        if not args.keep_going:
            shutil.rmtree(STAGING, ignore_errors=True)
        return result.returncode

    dist_dir = _staged_dist_dir()
    if dist_dir is None:
        print(f"❌ 没找到 Nuitka 的产物目录（{STAGING}/*.dist）")
        return 1
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    exe = stage_into_dist(dist_dir, exe_suffix=exe_suffix)
    shutil.rmtree(STAGING, ignore_errors=True)

    minutes = (time.monotonic() - started) / 60
    print("-" * 70)
    print(f"✅ 构建完成，用时 {minutes:.1f} 分钟")
    print(f"   可执行文件：{exe}")
    print(f"   产物目录  ：{exe.parent}（{_dir_size_mb(exe.parent):.0f} MB）")
    print("   下一步：跑一次自检确认编译版路径都对 ——")
    print(f"     \"{exe}\" --doctor     # 报告同时写进 <数据目录>/logs/自检报告.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
