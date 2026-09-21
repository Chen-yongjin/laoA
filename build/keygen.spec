# -*- mode: python ; coding: utf-8 -*-
r"""注册机的打包配置（**作者的签发工具，绝不随主程序分发**）。

用法（在 Windows 上，仓库根目录执行）：

    pyinstaller --noconfirm --clean build\keygen.spec

产出：`dist\keygen.exe`（**onefile**，双击即用，不用带一堆 `_internal`）。

为什么它是**单独一个 spec / 单独的产物**
----------------------------------------
`keygen.py` 里带着**签发密钥的算法**（从 `laoa_trader.licensing` 取）。谁能拿到这个 exe，
谁就能给任意机器码算出注册码 —— 等于把授权体系整个交出去。所以：

* **主程序 spec（`laoa_trader.spec`）里不许出现它**：`build/` 目录不在主包的 `DATAS` 里，
  主包的入口是 `laoa_trader/__main__.py`，与这里没有任何交集；
* **CI 里是单独的 job + 单独的 artifact 名（`keygen`）**：主 artifact 叫 `LaoniuTrader`
  （给用户），注册机 artifact 只有作者自己下载（见 `.github/workflows/build-windows.yml`）；
* `tests/test_packaging.py` 里有用例钉住"主 spec 不许提到 keygen" —— 将来谁顺手加进去会立刻红。

为什么 onefile 而主程序是 onedir
--------------------------------
主程序图"启动快 + 不被杀软误报"（几百 MB 依赖解压要 5~15 秒），所以用 onedir；
注册机的依赖只有 Qt 的一小部分、总共几十 MB，**一个文件最省事**（作者自己用，
不存在分发与升级的麻烦，启动多等一两秒无所谓）。

将来：加壳（用户 2026-09-20 说"等版本成熟再做"）
------------------------------------------------
加壳（VMProtect / Themida / Enigma 之类）能显著抬高逆向门槛，但会：
① 被杀软大面积误报（分发产品最怕这个）；② 让 exe 体积与启动时间都变差；
③ 每次改代码都要重新走一遍加壳流程。所以现在**不做**，等版本稳定、真的要防"专业逆向"时再上。
位置留在这里：加壳是**对产物**做的后处理（`dist\keygen.exe` 产出之后再处理），
不需要改这个 spec 里的任何一行 —— 这也是把这件"将来的事"写在这里的原因。
"""

import sys
from pathlib import Path

# spec 在 build/ 下，项目根是它的上一级（与 laoa_trader.spec 同一套定位方式）
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = PROJECT_ROOT / "src"

# 注册机只用到这几块：授权算法 + 它的依赖（config/clock/log）+ Qt 的窗口部件。
# 显式列出来是因为 `keygen.py` 是**脚本**（不在包里），PyInstaller 对它的
# 动态/间接导入分析没有对包那么可靠 —— 漏一个的后果是"打出来的 exe 一开就报缺模块"。
HIDDEN = [
    "laoa_trader",
    "laoa_trader.licensing",
    "laoa_trader.clock",
    "laoa_trader.config",
    "laoa_trader.log",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

# 注册机不需要的东西（打进去只会让 exe 更大、被杀软更爱误报）
EXCLUDES = [
    "pytest",
    "PyInstaller",
    "matplotlib",
    "scipy",
    "IPython",
    "notebook",
    "pandas",
    "numpy",
    "pyarrow",
    "requests",
    # 注意：**别把 sqlite3 排掉** —— `licensing` 里读"数据库那一份试用日期"用的是
    # `sqlite3`（经 storage.connect），排掉它等于给注册机埋一颗"某个分支一跑就 ImportError"
    # 的雷；而 sqlite3 本身就是标准库、体积可忽略。这条有用例守着（test_packaging.py）。
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "tkinter",
    "test",
    "unittest",
]

#: 图标用主程序那一份（同一个产品形象；没有就用默认图标并在控制台提一句）
ICON_PATH = SRC / "laoa_trader" / "assets" / "icon.ico"
if not ICON_PATH.exists():
    print(f"提示：没找到图标 {ICON_PATH}，本次打包用默认图标")

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(PROJECT_ROOT / "build" / "keygen.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],                      # 注册机不需要任何随包数据
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="keygen",                 # → dist\keygen.exe（ASCII 名：命令行里好敲、CI 上不会有编码问题）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX 极易被杀软误报（主程序也是关的）
    console=False,                 # 双击开窗口，不要黑框（无参数时它就是个小窗口）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)
