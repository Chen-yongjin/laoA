"""版本号只有一个真源：`laoa_trader.__version__`。

为什么值得单独测
----------------
分发出去的是一个 exe（用户看不到源码），他报障时说的是"【关于】里写着 v0.1.0"，
而安装包叫 `CaishenHelper-0.2.0` / 打包脚本读的是 `pyproject.toml` 的 `version`。
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
#: 2026-09-30 主人把产品名改成「财神助手」（平台不许发带"选股/荐股"字样的东西），
#: 旧名就是那四个字，用例跟着改。
LEGACY_NAME = "老牛选股"
#: 2026-09-30 定的口径：**软件名 = 财神助手**（窗口标题 / 托盘 / 任务栏 / exe / 打包目录）。
#: 通知、导出文件名、"助手"自称以前另有一个带"助手"尾缀的别名，改名后合并成同一个名字。
NEW_NAME = "财神助手"
ASSISTANT_NAME = "财神助手"
#: 打包产物**目录/压缩包/artifact** 用的 ASCII 名（2026-09-30 随产品改名一起改）。
#: 改名前后都**没对外发过包**，所以不存在"旧下载链接失效"的问题；
#: 从今往后这个名字就是下载直链的一部分，别再动它。
ARTIFACT_NAME = "CaishenTrader"
#: 包里那个可执行文件的名字：**中文** —— 用户在资源管理器里双击的那一个，
#: 中文更直观；目录名保持 ASCII（命令行、下载链接、别的机器上都不会有编码麻烦）。
EXE_NAME = "财神助手"


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
        # 推送标题属于"助手"语气那一类（用户 2026-09-20：通知/推送用「财神助手」）
        assert f"{ASSISTANT_NAME}-标的池" in source
        assert f"{LEGACY_NAME}-标的池" not in source
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
    """**目录/压缩包**用 ASCII `ARTIFACT_NAME`，**exe** 用中文 `EXE_NAME`（主人 2026-09-21）。

    三层口径（别再混起来）：
    * **界面**（标题栏、托盘、关于页）= 中文 `NEW_NAME` / `ASSISTANT_NAME`；
    * **目录与压缩包/artifact** = ASCII `CaishenTrader` —— 命令行、下载链接、别的机器上
      都不会有编码麻烦；而且**改 zip 名会让已经发出去的下载链接失效**；
    * **exe** = 中文 `财神助手.exe` —— 用户在资源管理器里双击的就是它。
    """
    from laoa_trader.ui import app as ui_app

    spec = (ROOT / "build" / "laoa_trader.spec").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    assert f'name="{ARTIFACT_NAME}"' in spec                 # 目录名仍是 ASCII
    assert f'name="{EXE_NAME}"' in spec                      # exe 名是中文
    assert f"dist/{ARTIFACT_NAME}/{EXE_NAME}.exe" in workflow
    # 旧的 exe 名不许在 CI 里残留（漏一处就是"打包成功但检查找不到产物"）
    assert f"dist/{ARTIFACT_NAME}/{ARTIFACT_NAME}.exe" not in workflow
    # 压缩包与 artifact 名**没跟着变**（改了会让下载链接失效）
    assert f"{ARTIFACT_NAME}.zip" in workflow
    assert ui_app.APP_NAME == NEW_NAME
