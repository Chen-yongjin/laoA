"""Nuitka 的入口脚本（`python -m nuitka build/launcher.py ...`）。

为什么要一个单独的小文件，而不是直接编译 `src/laoa_trader/__main__.py`：
1. Nuitka 的**主模块**就是它编译的那个脚本文件，用 `__main__.py` 当主模块会让
   产物里的模块名与包内的 `laoa_trader.__main__` 重名（`-m laoa_trader` 的入口语义
   也会跟着变），出问题时很难看出是谁在跑；
2. 产物里的可执行文件名默认取主模块名 —— 反正构建脚本最后会统一改名成
   `老牛选股`，这里只要保证"入口只有一个、语义明确"即可；
3. 入口保持三行：把 `sys.path` 指到 `src/`（源码运行时等价于 `PYTHONPATH=src`），
   调 `main()`，用它的返回码退出。

⚠️ 别在这里 import 任何 GUI 之外的重型模块（Nuitka 会跟着主模块的导入图走）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 源码布局：本文件在 laoA/build/launcher.py，包在 laoA/src/laoa_trader/
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from laoa_trader.__main__ import main  # noqa: E402 - 上一行必须先执行

if __name__ == "__main__":
    raise SystemExit(main())
