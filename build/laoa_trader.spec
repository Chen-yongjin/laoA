# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows 单机版）。

用法（在 Windows 上）：
    build\\build.bat                       # 一键：建 venv → 装依赖 → 打包
    pyinstaller --noconfirm --clean build\\laoa_trader.spec

产出：`dist\\LaoniuTrader\\LaoniuTrader.exe`（**onedir** 目录版）。

为什么是 onedir 而不是 onefile
------------------------------
onefile 每次启动都要把几百 MB 依赖解压到临时目录：启动要 5~15 秒，
杀软（尤其国产安全软件）几乎必然误报。onedir 启动 1~2 秒，且便于增量更新。

为什么 --noconsole（windowed）
------------------------------
这是桌面程序，双击运行时不该弹黑框。副作用是**看不到 stdout** ——
所以 `log.py` 会同时把日志写进 `<data_dir>/logs/laoa-trader.log`，
出问题时让用户把那个文件发过来即可。

打包要点
--------
- `hiddenimports`：pandas / numpy / pyarrow 有大量**动态导入**（例如
  `pandas._libs.tslibs.*`、pyarrow 的压缩编解码器），PyInstaller 静态分析常常漏掉；
  `winotify` 是条件依赖（`sys_platform=='win32'`），也必须显式列上。
- `datas`：`config.example.toml` 要随包分发（首次运行向导让用户复制成 config.toml）。
- `excludes`：把开发期依赖（pytest / PySide6 的 QtWebEngine 等）排掉，能省几百 MB。
"""

import sys
from pathlib import Path

# spec 文件在 build/ 下，项目根是它的上一级
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = PROJECT_ROOT / "src"

# ── 隐藏导入：静态分析抓不到的动态导入 ──
HIDDEN = [
    # pandas / numpy 的动态子模块
    "pandas",
    "pandas._libs.tslibs.base",
    "pandas._libs.tslibs.conversion",
    "pandas._libs.tslibs.period",
    "pandas._libs.tslibs.timedeltas",
    "pandas._libs.tslibs.timestamps",
    "pandas._libs.tslibs.offsets",
    "pandas._libs.tslibs.parsing",
    "pandas._libs.tslibs.strptime",
    "pandas.io.formats.style",
    "numpy",
    "numpy.core._multiarray_umath",
    # Parquet 读取：pyarrow 及其压缩/编码后端都是动态加载的
    "pyarrow",
    "pyarrow.parquet",
    "pyarrow._parquet",
    "pyarrow.lib",
    "pyarrow.fs",
    "pyarrow.compute",
    "pyarrow.csv",
    "pyarrow.json",
    "pyarrow.dataset",
    "pyarrow._compute",
    "pyarrow._dataset",
    "pyarrow._fs",
    # 压缩编解码器（dump 里的 snappy/zstd 列）
    "snappy",
    "zstandard",
    "lz4.frame",
    "brotli",
    # 通知
    "winotify",
    "winrt",
    "winrt.windows.ui.notifications",
    # 网络
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
    "certifi",
    # 本项目
    "laoa_trader",
    "laoa_trader.config",
    "laoa_trader.log",
    "laoa_trader.pool",
    "laoa_trader.intraday",
    "laoa_trader.scheduler",
    "laoa_trader.data",
    "laoa_trader.data.engine",
    "laoa_trader.data.hithink",
    "laoa_trader.data.storage",
    "laoa_trader.data.sync",
    "laoa_trader.notify",
    "laoa_trader.notify.feishu",
    "laoa_trader.notify.tray",
    "laoa_trader.strategy",
    "laoa_trader.strategy.formula",
    "laoa_trader.strategy.formula_group",
    "laoa_trader.formulas",
    "laoa_trader.ui",
    "laoa_trader.ui.app",
    "laoa_trader.ui.formula_page",
    # 下面这些本来是**静态导入**（PyInstaller 的静态分析会自己跟进），列出来是"多一道保险"：
    # 打包版跑不起来是最难查的一类问题（本机全绿、exe 一开就 ImportError），
    # 代价只是清单长几行。新增模块时顺手加一条。
    "laoa_trader.assets",
    "laoa_trader.hints",
    "laoa_trader.market",
    "laoa_trader.state",
    "laoa_trader.data.preflight",
    "laoa_trader.data.sources",
    "laoa_trader.data.eastmoney",
    "laoa_trader.notify.sound",
    # 桌宠与中文朗读是 2026-09-18 新增的，**都是函数里懒导入**（静态分析看不到）：
    # 漏了它们就是"本机全绿、exe 一开桌宠不出来"这种最难查的问题。
    "laoa_trader.notify.voice",
    "laoa_trader.ui.desktop_pet",
    "laoa_trader.ui.message_center",
    "laoa_trader.research.scorecard",
    "laoa_trader.ui.alert_popup",
    "laoa_trader.ui.quotes",
    "laoa_trader.ui.theme",
    # PySide6 里被动态加载的插件模块
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

# ── 随包分发的数据文件（目标路径 → 源路径）──
DATAS = [
    (str(PROJECT_ROOT / "config.example.toml"), "."),
    (str(PROJECT_ROOT / "README.md"), "."),
    # 验收清单随包放：用户解压后**exe 旁边就有**"该点哪几下"，
    # 不用回头翻仓库（这是"拿到就能自己验"的最小代价）
    (str(PROJECT_ROOT / "快速验收.md"), "."),
    # 图标要打进包里：窗口 / 托盘 / 关于页都从 `laoa_trader/assets/` 取
    # （位置解析集中在 `laoa_trader/assets.py`，spec 这里只负责把文件放进去）
    (str(SRC / "laoa_trader" / "assets"), "laoa_trader/assets"),
    # 示例公式（随包分发）：解到 `_MEIPASS/formulas`，第一次打开「公式选股」页时
    # 由 `formulas.formula_dir()` 复制到 **exe 同级的 formulas/**（那才是用户自己的目录，
    # 可写、看得见、能备份）。少这一步的话，新用户打开那一页是个空列表，
    # 连【载入示例】都没得载。
    (str(PROJECT_ROOT / "formulas"), "formulas"),
]

#: exe 的图标：Windows 用它显示在任务栏 / 资源管理器 / 快捷方式上。
#: 多尺寸 ICO 由 `python build/make_icon.py` 生成；缺失时不阻塞打包（退化成默认图标，
#: 同时打印一句提示，免得"图标没换"变成一件要靠猜的事）。
ICON_PATH = SRC / "laoa_trader" / "assets" / "icon.ico"
if not ICON_PATH.exists():
    print(f"提示：没找到图标 {ICON_PATH}，本次打包用默认图标"
          f"（需要的话先跑 python build/make_icon.py）")


# ── 排除：省体积（这些包被打进去会多几百 MB）──
EXCLUDES = [
    "pytest",
    "PyInstaller",
    "matplotlib",
    "scipy",
    "IPython",
    "notebook",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "tkinter",
    "test",
    "unittest",
]


a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(SRC / "laoa_trader" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=DATAS,
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
    [],
    exclude_binaries=True,
    name="LaoniuTrader",   # exe 名（用户 2026-09-20 定：产物用 ASCII 名 LaoniuTrader）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩极易被杀软误报，关掉
    console=False,      # --noconsole：桌面程序不该弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,   # 见上面 ICON_PATH
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LaoniuTrader",   # 产出 dist/LaoniuTrader/LaoniuTrader.exe
)

if sys.platform != "win32":
    # 在 Linux/macOS 上只能打包出本平台的产物（PyInstaller 不支持交叉编译）。
    # 这里不报错，只是提醒：Windows 的 exe 必须在 Windows 上打。
    print(
        "提示：当前平台是 %s，PyInstaller 不能交叉编译 —— "
        "请把源码拷到 Windows 上执行 build\\build.bat 生成 exe。" % sys.platform
    )
