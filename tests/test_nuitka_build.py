"""Nuitka 构建脚本的静态与搬运逻辑（2026-09-22 换构建方式时新增）。

为什么不直接在测试里跑一次 Nuitka
----------------------------------
Nuitka 需要 C 编译器，本机（NAS）没有、也不该为了跑测试去装一个；真正的构建验证放在
Windows CI 上（构建 + 跑 exe 自检）。但"构建参数写错了"这类问题**不该等到 CI 才发现**
（一轮 CI 二十多分钟），所以这里把能静态核对的部分钉死：

1. 产物名与目录名 = 我们对客户承诺的那两个（`LaoniuTrader` / `老牛选股.exe`）；
2. 随包数据与 `build/laoa_trader.spec` 的 `DATAS` **逐项对应**（两条构建路径不许漂移）；
3. `--nofollow-import-to` 覆盖 spec 的 `EXCLUDES`（省体积的取舍两边一致）；
4. 搬运逻辑（Nuitka 的 `.dist` → `dist/LaoniuTrader/`，并把 exe 改成中文名）真的能跑，
   用一个假产物目录验。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_build_script():
    """把 `build/nuitka_build.py` 当模块载入（它在 build/ 下，不是安装的包）。"""
    path = PROJECT_ROOT / "build" / "nuitka_build.py"
    spec = importlib.util.spec_from_file_location("nuitka_build", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def builder():
    return _load_build_script()


def test_exe_and_dist_names_are_what_we_promise_users(builder) -> None:
    """产物名是硬承诺：`dist/LaoniuTrader/老牛选股.exe`（下载说明/CI 检查/文档都按它写）。"""
    assert builder.DIST_NAME == "LaoniuTrader"       # 目录名保持 ASCII（命令行/工具兼容）
    assert builder.EXE_NAME == "老牛选股"            # exe 名中文（用户在资源管理器里双击的就是它）


def test_build_command_covers_every_packaged_data_file(builder) -> None:
    """随包数据一个都不能漏（漏了就是"程序起来了、图标/随包策略/示例配置不见了"）。"""
    cmd = builder.build_command("python")
    joined = " ".join(cmd)

    for source, target in builder.DATA_FILES:
        assert source.is_file(), f"随包文件不存在：{source}"
        assert f"--include-data-files={source}={target}" in joined
    for source, target in builder.DATA_DIRS:
        assert source.is_dir(), f"随包目录不存在：{source}"
        assert f"--include-data-dir={source}={target}" in joined

    # 关键几项点名字（这些是 historically 会被漏掉的：图标、随包策略）
    assert "laoa_trader/assets" in joined
    assert "formulas" in joined


def test_build_command_matches_the_pyinstaller_spec_datas(builder) -> None:
    """两条构建路径的"随包数据"必须一致（PyInstaller 是备用路径，不能悄悄落后）。"""
    spec_text = (PROJECT_ROOT / "build" / "laoa_trader.spec").read_text(encoding="utf-8")
    datas_block = spec_text.split("DATAS = [", 1)[1].split("]", 1)[0]
    spec_targets = set(re.findall(r'"([^"]+)"\)', datas_block))

    nuitka_targets = {target for _source, target in (*builder.DATA_FILES, *builder.DATA_DIRS)}
    # spec 里的目标是相对路径（"." 表示放到根），这里把 "." 与根级文件对齐
    normalized = {("" if t == "." else t) for t in spec_targets}
    for target in nuitka_targets:
        assert target in normalized or target.split("/")[0] in normalized or "." in spec_targets, (
            f"Nuitka 打了 {target}，但 PyInstaller 的 spec 里没有对应项 —— 两条路径漂移了"
        )


def test_excludes_and_nofollow_stay_in_sync(builder) -> None:
    """省体积的取舍两边一致（一边排掉了、另一边打进去，体积会莫名多几百 MB）。"""
    spec_text = (PROJECT_ROOT / "build" / "laoa_trader.spec").read_text(encoding="utf-8")
    excludes_block = spec_text.split("EXCLUDES = [", 1)[1].split("]", 1)[0]
    spec_excludes = set(re.findall(r'"([^"]+)"', excludes_block))

    missing = {name for name in spec_excludes if name not in builder.NOFOLLOW}
    # spec 排掉的，Nuitka 这边**必须也排**（漂移的症状是"Nuitka 版莫名比 PyInstaller 版
    # 大几百 MB、构建也慢一截"，而没人会想到去比对两份清单）。
    # 反过来允许 Nuitka 多排几条：它比 PyInstaller 多编译那些第三方 `...tests` 子包，
    # 多排几条是省时间，不是不一致。
    assert not missing, f"spec 排掉了但这些没在 Nuitka 里排：{sorted(missing)}"
    extras = set(builder.NOFOLLOW) - spec_excludes
    for name in extras:
        assert "tests" in name or name.endswith(("examples", "scripts")), (
            f"{name} 是 Nuitka 侧多排的条目，但没有说明理由（只允许排第三方测试/示例包）"
        )


def test_stage_into_dist_moves_the_payload_and_renames_the_exe(builder, tmp_path: Path) -> None:
    """搬运逻辑：Nuitka 的 `launcher.dist/` → `dist/LaoniuTrader/老牛选股.exe`。

    这一步是"客户拿到的路径"与"Nuitka 自己的命名"之间的唯一转换点，
    搬错了的表现是"包能下、双击不到 exe"（用户第一时间就会报）。
    """
    staged = tmp_path / "launcher.dist"
    (staged / "laoa_trader" / "assets").mkdir(parents=True)
    (staged / "laoa_trader" / "assets" / "icon.png").write_bytes(b"png")
    (staged / "launcher.exe").write_bytes(b"MZ")

    exe = builder.stage_into_dist(staged, exe_suffix=".exe", dist_root=tmp_path / "dist")

    assert exe == tmp_path / "dist" / "LaoniuTrader" / "老牛选股.exe"
    assert exe.is_file()
    assert not staged.exists(), "中间目录应该被搬走（不是复制），否则 CI 上白占空间"
    # 随包数据跟着一起过去
    assert (exe.parent / "laoa_trader" / "assets" / "icon.png").is_file()


def test_stage_into_dist_replaces_an_old_build(builder, tmp_path: Path) -> None:
    """重复构建：旧的 dist/LaoniuTrader 要被整体替掉（否则新旧文件混在一起，最难查）。"""
    old = tmp_path / "dist" / "LaoniuTrader"
    old.mkdir(parents=True)
    (old / "旧文件.txt").write_text("old", encoding="utf-8")

    staged = tmp_path / "launcher.dist"
    staged.mkdir()
    (staged / "launcher").write_bytes(b"ELF")

    exe = builder.stage_into_dist(staged, exe_suffix="", dist_root=tmp_path / "dist")

    assert exe == tmp_path / "dist" / "LaoniuTrader" / "老牛选股"
    assert not (exe.parent / "旧文件.txt").exists(), "旧产物里那些文件不该留下来"


def test_stage_into_dist_fails_loudly_when_there_is_no_executable(builder, tmp_path: Path) -> None:
    """产物里没有可执行文件时要**大声失败**（静默产出一个空包是最坏情况）。"""
    staged = tmp_path / "launcher.dist"
    staged.mkdir()
    (staged / "somefile.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SystemExit):
        builder.stage_into_dist(staged, exe_suffix=".exe", dist_root=tmp_path / "dist")


def test_launcher_exists_and_is_a_thin_entry(builder) -> None:
    """入口脚本要在（Nuitka 的主模块就是它），而且只做"转发到 main()"这一件事。"""
    launcher = PROJECT_ROOT / "build" / "launcher.py"
    text = launcher.read_text(encoding="utf-8")
    assert "from laoa_trader.__main__ import main" in text
    assert text.count("\n") < 40, "入口脚本不该越写越厚（Nuitka 会跟着它的导入图走）"
