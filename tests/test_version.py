"""版本号只有一个真源：`laoa_trader.__version__`。

为什么值得单独测
----------------
分发出去的是一个 exe（用户看不到源码），他报障时说的是"【关于】里写着 v0.1.0"，
而安装包叫 `LaoATrader-0.2.0` / 打包脚本读的是 `pyproject.toml` 的 `version`。
两处一旦漂移，就会出现"界面说这个版本、包是那个版本"的错位 ——
排查起来极其费劲（而且这种错位没人会主动发现），所以用一条用例钉死。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import laoa_trader

ROOT = Path(__file__).resolve().parents[1]


def _project_table() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_pyproject_version_matches_package_version() -> None:
    """`pyproject.toml` 的 version 与 `laoa_trader.__version__` 必须一字不差。"""
    assert _project_table()["version"] == laoa_trader.__version__


def test_pyproject_declares_author() -> None:
    """作者 / 版权所有人也要能查到（与「关于」对话框、README 的版权信息一致）。"""
    assert {"name": "async-chen"} in _project_table()["authors"]
