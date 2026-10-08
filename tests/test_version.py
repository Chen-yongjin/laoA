"""版本号只有一个真源：`laoa_trader.__version__`。

为什么值得单独测
----------------
分发出去的是一个 exe（用户看不到源码），他报障时说的是"【关于】里写着 v0.1.0"，
而安装包叫 `CaishenHelper-0.2.0` / 打包脚本读的是 `pyproject.toml` 的 `version`。
两处一旦漂移，就会出现"界面说这个版本、包是那个版本"的错位 ——
排查起来极其费劲（而且这种错位没人会主动发现），所以用一条用例钉死。
"""

from __future__ import annotations

import json
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


def test_package_names_are_renamed_everywhere() -> None:
    """改名（2026-09-30）最容易漏的地方：**压缩包名、站点下载文件名、包内容自检路径**。

    这三处分别落在 CI 工作流、站点脚本与清单、站点首页里。任何一处没跟上，用户下到的
    就是一个名字对不上的包 —— 打包本身不会报错，只能靠人发现，所以在这里钉死。
    """
    workflow = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    script = (ROOT / "网站" / "script.js").read_text(encoding="utf-8")
    index = (ROOT / "网站" / "index.html").read_text(encoding="utf-8")
    manifest = (ROOT / "网站" / "downloads" / "latest.json").read_text(encoding="utf-8")

    version = laoa_trader.__version__
    assert f"Copy-Item '{ARTIFACT_NAME}.zip' \"{ARTIFACT_NAME}-$ver.zip\"" in workflow
    assert f"{ARTIFACT_NAME}/财神助手\\.exe" in workflow          # 包内容自检认的是新路径
    assert f"downloads/{ARTIFACT_NAME}.zip" in script             # 站点的兜底链接
    assert f"downloads/{ARTIFACT_NAME}.zip" in index              # 首页两个下载按钮
    # 站点清单指向的必须是**downloads/ 里真实存在的那个包**（不一定是最新版本号）：
    # 新版本要等 CI 编完、包放进来之后才改这两行 —— 抢先把版本号改成还没编出来的那个，
    # 用户点下载只会看到「下载准备中」。所以这里钉的是「名字对得上、且文件确实在」。
    payload = json.loads(manifest)
    assert payload["file"].startswith(f"{ARTIFACT_NAME}-") and payload["file"].endswith(".zip")
    assert payload["version"] in payload["file"]
    _assert_manifest_points_at_a_real_package(payload)
    # 旧名字一个字都不许剩
    for text, label in ((workflow, "CI"), (script, "script.js"), (index, "index.html"),
                        (manifest, "latest.json")):
        assert "LaoniuTrader" not in text, f"{label} 里还留着旧包名"


def _assert_manifest_points_at_a_real_package(payload: dict) -> None:
    """站点清单里那个包**在本地得真的存在** —— 但 CI 上**必须放过**。

    为什么要分两种情况（2026-10-08 修：CI 连着两轮红在这一条）：
      * `网站/downloads/*.zip` 是 **gitignore 掉的**（80MB 构建产物不进 git，见 `.gitignore`
        里那条注释）。所以 CI 检出代码后那个目录里**一个 zip 都没有** ——
        在那里断言"文件存在"，等于要求把 80MB 的包提交进仓库，与设计相反。
      * 而在作者本机（发布时）那个目录里是**有包**的：这时清单指的必须是其中真实存在的那个，
        "改了版本号却没把包放进来"正是这条要拦的错 —— 用户点下载只会看到「下载准备中」。

    判据：`downloads/` 里**有没有 zip**。
      * 一个都没有 → 这是"没有包的检出"（CI / 刚 clone），只核对命名约定，不做存在性断言；
      * 有 ≥1 个 → 清单指的那个必须在里面（原来的强断言）。
    """
    downloads = ROOT / "网站" / "downloads"
    present = {path.name for path in downloads.glob("*.zip")}
    if not present:
        return
    assert payload["file"] in present, (
        f"站点清单指向的包不在 downloads/ 里：{payload['file']}（那里的包是："
        f"{'、'.join(sorted(present))}）—— 换版本时要把包一起放进去"
    )
