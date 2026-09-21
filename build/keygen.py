# 触发说明（2026-09-21）：CI 里的 keygen job 只在提交改动过本文件或 keygen.spec 时才跑，
# 所以需要重新出注册机时，改这一行注释并推送即可（产物会挂到滚动 Release 的 keygen.zip）。
"""
（2026-09-21：本文件、keygen.spec、licensing.py 或 workflow 有改动时，CI 会自动重发注册机到 Release。）
注册机（**作者自己用的签发工具，不要分发给用户**）。

用法
----
::

    # 1) 命令行（我这边签发最快）
    python build/keygen.py 8F3K-2M7Q-XW4D-9PLA
    # → 注册码：XXXX-XXXX-XXXX-XXXX

    # 2) 小窗口（自己用鼠标点）
    python build/keygen.py

窗口里：输入机器码 → 点【生成注册码】→ 复制。机器码**大小写与连字符都容错**
（用户从微信里复制过来常带空格）。

密钥为什么从 `licensing` 取而不是这里再写一份
--------------------------------------------
注册码必须与客户端**用同一把密钥**算出来，否则用户拿到手的码是无效的。
两处各写一份密钥 = 迟早漂移，而且这种漂移**只有在用户注册失败时才被发现**。
所以这里只做界面，算法全部调 `licensing.expected_code()`。

**这个文件不要进打包产物**（`build/` 目录不在 spec 的打包范围内；
发布前确认整包产物里没有 `keygen.py`）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能直接 `python build/keygen.py` 跑：把仓库的 `src/` 放进 import 路径
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from laoa_trader import licensing  # noqa: E402 - 必须先把 src 放进路径


def make_code(machine: str) -> str:
    """机器码 → 注册码（容错：大小写、空格、连字符都不影响）。"""
    return licensing.expected_code(machine)


def main(argv: list[str]) -> int:
    machine = (argv[1] if len(argv) > 1 else "").strip()
    if machine:
        # 命令行模式：签发一行搞定，适合我这边直接把机器码粘进来
        print(f"机器码：{machine}")
        print(f"注册码：{make_code(machine)}")
        return 0
    return _gui()


def _gui() -> int:
    """小窗口：机器码输入 + 【生成注册码】+ 复制。"""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import (
            QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
        )
    except Exception as exc:  # noqa: BLE001 - 没装 Qt 时退回命令行用法
        print(f"没有可用的 Qt（{exc}）；请用命令行：python build/keygen.py <机器码>")
        return 2

    app = QApplication.instance() or QApplication([])
    win = QWidget()
    win.setWindowTitle("老牛选股 · 注册机（作者用）")
    win.setMinimumWidth(460)
    layout = QVBoxLayout(win)

    tip = QLabel("作者的签发工具，不要分发给用户。把用户发来的机器码粘进来即可。")
    tip.setWordWrap(True)
    layout.addWidget(tip)

    layout.addWidget(QLabel("机器码："))
    machine_edit = QLineEdit()
    machine_edit.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
    layout.addWidget(machine_edit)

    layout.addWidget(QLabel("注册码："))
    code_edit = QLineEdit()
    code_edit.setReadOnly(True)
    layout.addWidget(code_edit)

    row = QHBoxLayout()
    gen = QPushButton("生成注册码")
    gen.setObjectName("primaryAction")

    def _generate() -> None:
        raw = machine_edit.text().strip()
        if not raw:
            code_edit.setText("请先填机器码")
            return
        code_edit.setText(make_code(raw))

    gen.clicked.connect(_generate)
    row.addWidget(gen)

    def _copy() -> None:
        text = code_edit.text().strip()
        if text:
            QGuiApplication.clipboard().setText(text)
            code_edit.setToolTip("已复制")

    copy_btn = QPushButton("复制注册码")
    copy_btn.clicked.connect(_copy)
    row.addWidget(copy_btn)
    layout.addLayout(row)

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
