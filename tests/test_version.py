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


# ── 产品名 ──

#: 改名前的旧名：任何"用户看得见"的地方都不许再出现它。
#: 为什么要钉住：改名最容易漏（窗口标题改了、托盘/通知没改，或者推送标题还是旧名），
#: 而这种不一致只有用户自己发现得了。
LEGACY_NAME = "老牛选股法师"
#: 用户 2026-09-20 定的口径：**软件名 = 老牛选股**（窗口标题 / 托盘 / 任务栏 / exe / 打包目录），
#: 而通知、导出文件名、"助手"自称仍用 `ASSISTANT_NAME`（见 `ui/app.py`）。
NEW_NAME = "老牛选股"
ASSISTANT_NAME = "老牛选股助手"
#: 打包产物用的 ASCII 名（dist 目录 / exe / artifact；用户 2026-09-20 定）
ARTIFACT_NAME = "LaoniuTrader"


def test_user_visible_names_use_the_new_product_name() -> None:
    """窗口标题 / 托盘 / 推送标题 / 命令行 banner 全部用新名。

    （2026-09-18：Windows 系统通知那一整路删除，所以不再有 `windows.APP_ID`
    这一项要核 —— 用户原话："windows系统通知删除，太骚扰了，影响体验。"）
    """
    from laoa_trader import __main__ as cli
    from laoa_trader import scheduler
    from laoa_trader.ui import app as ui_app

    assert ui_app.APP_NAME == NEW_NAME
    assert getattr(ui_app, "ASSISTANT_NAME", "") == ASSISTANT_NAME
    assert LEGACY_NAME not in ui_app.APP_NAME
    assert NEW_NAME in cli.__doc__ or NEW_NAME in (cli.__doc__ or "")  # 帮助文本
    # 推送标题（飞书卡片 / 托盘 / 通知共用这一个标题）
    title = scheduler.pool_push_title("2026-09-14") if hasattr(scheduler, "pool_push_title") else None
    if title is None:                                  # 没有抽成函数就直接读源码里的字面量
        source = (ROOT / "src" / "laoa_trader" / "scheduler.py").read_text(encoding="utf-8")
        # 推送标题属于"助手"语气那一类（用户 2026-09-20：通知/推送用「老牛选股助手」）
        assert f"{ASSISTANT_NAME}-选股池" in source
        assert f"{LEGACY_NAME}-选股池" not in source
    else:
        assert NEW_NAME in title and LEGACY_NAME not in title


def test_repo_text_files_do_not_mention_the_legacy_name() -> None:
    """源码 / 配置示例 / 打包脚本 / README / CI 里都不许再有旧名。

    只扫**指定的用户可见文件**（不去 grep 整个仓库 —— 那样会把 tests 里刻意的旧名用例、
    git 历史、第三方文件都卷进来，变成一条爱误报的用例）。
    """
    targets = [
        "src/laoa_trader/ui/app.py",
        # （2026-09-18：`notify/windows.py` 随 Windows 系统通知整路删除，这里不再扫它）
        "src/laoa_trader/scheduler.py",
        "src/laoa_trader/__main__.py",
        "src/laoa_trader/__init__.py",
        "config.example.toml",
        "README.md",
        "build/build.bat",
        "build/laoa_trader.spec",
        ".github/workflows/build-windows.yml",
    ]
    offenders = [
        name for name in targets
        if LEGACY_NAME in (ROOT / name).read_text(encoding="utf-8")
    ]
    assert not offenders, f"这些文件里还留着旧名：{offenders}"


def test_executable_is_named_after_the_product() -> None:
    """产物名 = **LaoniuTrader**（用户 2026-09-20 定：文件夹/exe/压缩包都用 ASCII 名）。

    与界面里的中文名分开是刻意的：
    * **界面**（标题栏、托盘、关于页）用中文 `NEW_NAME` = 老牛选股 / 老牛选股助手；
    * **产物**（dist 目录、exe、artifact）用 ASCII `ARTIFACT_NAME` = LaoniuTrader ——
      ASCII 名在命令行、压缩包、别的机器上都不会有编码麻烦。
    """
    from laoa_trader.ui import app as ui_app

    spec = (ROOT / "build" / "laoa_trader.spec").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    assert f'name="{ARTIFACT_NAME}"' in spec
    assert f"dist/{ARTIFACT_NAME}/{ARTIFACT_NAME}.exe" in workflow
    # 界面里仍然是中文名（别为了改产物名把中文名一起换掉）
    assert ui_app.APP_NAME == NEW_NAME
