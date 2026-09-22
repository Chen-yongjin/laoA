"""运行形态与随包路径：源码 / PyInstaller / Nuitka 三种都要对（2026-09-22 换 Nuitka 时新增）。

为什么值得单独一个用例文件
--------------------------
打包方式从 PyInstaller 换成 Nuitka 之后，"随包数据在哪"从两种形态变成三种，
而**开发机上永远是源码运行** —— 路径写错的症状（图标没了、随包策略列表空了、
配置写到程序目录里）在开发机上一次都看不到，只会出现在用户那台机器上。
所以这里把三种形态**做成临时目录模拟出来**，逐条断言解析结果：

* 源码运行：仓库里那份；
* PyInstaller onedir：`sys._MEIPASS`（解包目录）；
* Nuitka standalone：**exe 同级**（`--include-data-dir` 的落点）。

模拟手法：改 `sys.executable` / 注入 `sys._MEIPASS` / 往 `runtime` 模块里塞
`__compiled__`（Nuitka 编译后每个模块都有它，源码运行时不存在），
再把 `assets.__file__` 指到假的产物目录里 —— 都是在真实产物里成立的等价条件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from laoa_trader import assets, config, formulas, runtime


@pytest.fixture()
def dist_layout(tmp_path: Path) -> Path:
    """造一个"Nuitka 产物目录"：exe + 随包数据（结构与 --include-data-* 一致）。"""
    dist = tmp_path / "LaoniuTrader"
    (dist / "laoa_trader" / "assets").mkdir(parents=True)
    (dist / "laoa_trader" / "assets" / "icon.png").write_bytes(b"png")
    (dist / "formulas").mkdir()
    (dist / "formulas" / "短期反转.txt").write_text("# 名称: 短期反转\n", encoding="utf-8")
    (dist / "老牛选股.exe").write_bytes(b"exe")
    (dist / "config.example.toml").write_text("# 示例\n", encoding="utf-8")
    return dist


def _pretend_nuitka(monkeypatch: pytest.MonkeyPatch, dist: Path) -> None:
    """把进程伪装成"Nuitka 编译出来的 exe 正在运行"。"""
    monkeypatch.setitem(runtime.__dict__, "__compiled__", object())
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(dist / "老牛选股.exe"))
    # Nuitka 里模块的 __file__ 指向产物目录内的路径（那一份不一定是真文件，
    # 但同级的 assets 目录是真的）—— 这行让 assets_dir() 的"旁边那个"分支也指向产物
    monkeypatch.setattr(assets, "__file__", str(dist / "laoa_trader" / "assets.py"))


def _pretend_pyinstaller(monkeypatch: pytest.MonkeyPatch, dist: Path, meipass: Path) -> None:
    """把进程伪装成"PyInstaller onedir 产物正在运行"（数据在 _MEIPASS 里）。"""
    monkeypatch.delitem(runtime.__dict__, "__compiled__", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(dist / "老牛选股.exe"))
    monkeypatch.setattr(assets, "__file__", str(meipass / "laoa_trader" / "assets.py"))


def test_source_mode_uses_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码运行（开发机 / 本机测试）：随包数据就是仓库里那份。"""
    monkeypatch.delitem(runtime.__dict__, "__compiled__", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert runtime.kind() == "source"
    assert runtime.is_frozen() is False
    assert runtime.exe_dir() == runtime._repo_root()
    assert runtime.bundle_dir() == runtime._repo_root()
    assert formulas.bundled_formula_dir() == runtime._repo_root() / "formulas"
    assert assets.assets_dir().is_dir()          # 仓库里那份 assets/
    assert "源码运行" in runtime.describe()


def test_nuitka_mode_resolves_beside_the_exe(
    dist_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nuitka 产物：形态认得出来，随包数据与用户可写目录都在 **exe 同级**。"""
    _pretend_nuitka(monkeypatch, dist_layout)

    assert runtime.kind() == "nuitka"
    assert runtime.is_frozen() is True
    assert runtime.exe_dir() == dist_layout
    assert runtime.bundle_dir() == dist_layout
    assert "Nuitka" in runtime.describe()

    # 随包策略：就在 exe 同级的 formulas/
    assert formulas.bundled_formula_dir() == dist_layout / "formulas"
    # 用户公式目录同样落在 exe 同级（双击 exe 就看得见、能备份）
    assert formulas.formula_dir() == dist_layout / "formulas"
    # 图标：产物里 laoa_trader/assets/
    assert assets.assets_dir() == dist_layout / "laoa_trader" / "assets"
    assert assets.icon_png() is not None


def test_pyinstaller_mode_still_works(
    dist_layout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyInstaller（备用打包路径）行为不变：数据在 `_MEIPASS`，用户目录在 exe 同级。

    这条是"换 Nuitka 别把老路踩坏"的保险：PyInstaller 的 spec 仍然留在仓库里，
    哪天 Nuitka 出问题要退回去，这条用例保证退回去也不会走错路径。
    """
    meipass = tmp_path / "_MEI12345"
    (meipass / "laoa_trader" / "assets").mkdir(parents=True)
    (meipass / "laoa_trader" / "assets" / "icon.png").write_bytes(b"png")
    (meipass / "formulas").mkdir()
    _pretend_pyinstaller(monkeypatch, dist_layout, meipass)

    assert runtime.kind() == "pyinstaller"
    assert runtime.bundle_dir() == meipass
    assert runtime.exe_dir() == dist_layout          # 用户可写目录仍跟着 exe
    assert formulas.bundled_formula_dir() == meipass / "formulas"
    assert formulas.formula_dir() == dist_layout / "formulas"
    assert assets.assets_dir() == meipass / "laoa_trader" / "assets"


def test_config_legacy_candidates_follow_the_exe(
    dist_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """老配置的迁移来源要跟着 exe 走（Nuitka 与 PyInstaller 都一样）。

    这条防的是"配置搬到 %APPDATA% 之后，产物形态下认不出老位置那份"：
    用户升级后飞书/Key 会突然变成"没配置"。
    """
    _pretend_nuitka(monkeypatch, dist_layout)

    candidates = config._legacy_config_candidates()
    assert dist_layout / "config.toml" in candidates


def test_doctor_and_version_lines_mention_the_build_kind(
    dist_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自检/版本这两处必须能说出"我是哪种构建"（报障时第一句就要这个答案）。"""
    _pretend_nuitka(monkeypatch, dist_layout)

    lines = runtime.summary_lines()
    assert any("Nuitka" in line for line in lines)
    assert any(str(dist_layout) in line for line in lines)
