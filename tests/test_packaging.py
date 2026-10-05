"""打包配置的守卫用例：**注册机不许混进主程序产物**。

口径变过两次，都记在这里（免得下一个人以为哪一版是漏改）：
* 2026-09-20 用户：「注册机做成可执行文件。不随包分发。」→ 钉"主产物里不许出现 keygen"；
* 2026-09-21 用户：「直接把注册机打包到程序包里也可以的」→ CI 里刻意放一个（刺眼命名）；
* **2026-09-21 当天稍后又改回**：「下面的包不要带注册机了，我已经保存了」→
  包必须**干净**（没有注册机），作者要用时走手动 keygen job（产物挂 Release 的 `keygen.zip`）。

这条要求靠"文档里写一句"是拦不住的 —— 将来谁顺手把 `build/keygen.py` 或它的产物
放进 spec/DATAS，用户包里就带着签发算法，授权等于形同虚设。所以这里从四个层面钉住：

* 主 spec（`build/laoa_trader.spec`）里**不许出现** keygen；
* 注册机自己的 spec（`build/keygen.spec`）必须存在，且入口就是 `build/keygen.py`、
  产物名固定（`dist/keygen.exe`）；
* CI 的主 job 有"**产物里不许出现 keygen**"的检查（workflow 文本层面钉住，免得有人删掉那段），
  注册机则是**独立 job + 独立 artifact**，并且挂到同一个 Release 的 `keygen.zip`；
* 主程序的运行代码里**不许**导入注册机（注册机只给作者用）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_SPEC = ROOT / "build" / "laoa_trader.spec"
KEYGEN_SPEC = ROOT / "build" / "keygen.spec"
KEYGEN_PY = ROOT / "build" / "keygen.py"
WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"


def test_keygen_spec_exists_and_builds_the_keygen_entry() -> None:
    """注册机有自己的 spec：入口是 `build/keygen.py`，产物名是 `keygen`。"""
    text = KEYGEN_SPEC.read_text(encoding="utf-8")

    assert "keygen.py" in text
    assert re.search(r'name="keygen"', text), "产物名必须是 keygen（文档里写的是 dist/keygen.exe）"
    # 注册机是 onefile（主程序是 onedir）：这里用 EXE 直接收 binaries/datas，没有 COLLECT
    assert "COLLECT(" not in text
    assert KEYGEN_PY.is_file()


def test_main_spec_never_mentions_the_keygen() -> None:
    """**主程序 spec 里一个字都不许提到 keygen**（它带着签发密钥的算法）。"""
    text = MAIN_SPEC.read_text(encoding="utf-8")

    assert "keygen" not in text.lower(), "主 spec 里出现了 keygen —— 会把签发工具打进用户包里"


def test_main_spec_datas_does_not_include_the_build_directory() -> None:
    """主程序的随包数据不许来自 `build/`（注册机就在那里 —— 这是那条泄漏的源头）。"""
    text = MAIN_SPEC.read_text(encoding="utf-8")

    datas_block = text[text.index("DATAS = ["):text.index("a = Analysis(")]
    # 逐条看 `(源路径, 目标路径)`：源路径里不许出现 build/
    sources = re.findall(r"\(\s*str\(([^)]*)\)", datas_block)
    assert sources, "没解析出 DATAS 的条目（spec 结构变了？这条用例要跟着更新）"
    assert not any("build" in item for item in sources), \
        f"主 spec 的 DATAS 里出现了 build/ 下的东西：{sources}"


def test_application_code_never_imports_the_keygen() -> None:
    """主程序代码里不许导入注册机（它只给作者用，主程序没有任何地方需要它）。"""
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "keygen" in text:
            hits.append(str(path.relative_to(ROOT)))

    assert hits == [], f"这些源码引用了 keygen：{hits}"


def test_ci_packages_never_contain_the_keygen() -> None:
    """CI 打出来的**主程序包里必须没有注册机**（2026-09-21 主人："下面的包不要带注册机了"）。

    钉四件事：① 主 job 里有"产物里不许出现 keygen"的**检查**（不是只写文档）；
    ② 写包那一步不许再把注册机复制进去（`keygen_in_pkg` 那一步已删）；
    ③ 注册机仍是独立 job + 独立 artifact 名（`keygen`），主 artifact 名仍 `CaishenTrader`；
    ④ 主程序的 Release 附件里不许出现 keygen。
    """
    text = WORKFLOW.read_text(encoding="utf-8")

    # ① 主 job 的产物检查：出现 keygen 就报错
    assert "*注册机*" in text and "包里不该有注册机" in text, \
        "主 job 少了「产物里不许出现注册机」的检查"
    # ② 复制进包的那一步必须已经删掉（连同它那句刺眼文件名）
    assert "keygen_in_pkg" not in text, "「把注册机放进包」那一步应当已经删除"
    assert "注册机-作者专用-别分发给用户.exe" not in text, "包里不该再有注册机文件"
    # ③ 注册机自己的 job / artifact 仍在，主 artifact 名不变
    assert re.search(r"^\s{2}keygen:", text, re.M), "workflow 里没有独立的 keygen job"
    assert re.search(r"name:\s*keygen\b", text), "注册机 artifact 名必须是 keygen"
    assert re.search(r"name:\s*CaishenTrader\b", text), "主 artifact 名仍是 CaishenTrader"
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "contains(github.event.head_commit.modified, 'build/keygen.spec')" in text, \
        "改动 keygen.spec 时应当自动重发注册机"
    # ④ Release 附件里只有主程序 zip（注册机是**另一个 job** 传的独立附件）
    release_zip = re.search(r"files:\s*(.+)", text)
    assert release_zip is not None and "keygen" not in release_zip.group(1)


def test_keygen_is_published_to_the_release_not_only_as_an_artifact() -> None:
    """注册机要挂到**滚动 Release**（与主程序同一个页面），artifact 只作备用。

    为什么（主人 2026-09-21 实报「keygen 没跑通」）：注册机原来只走 artifact，
    而那一步在私有仓库上会因**存储额度已满**失败 —— 手动跑了 workflow 也拿不到东西。
    Release 附件与那份额度是两回事，所以这条用例把"注册机必须走 Release"钉住：
    以后谁把这一步删掉，主人就会再遇到一次"跑了却拿不到"。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    keygen_job = text[text.index("  keygen:"):]

    assert "gh release upload $tag keygen.zip --clobber" in keygen_job, \
        "注册机 job 里少了上传 Release 附件这一步（附件名 keygen.zip，主人从这里下）"
    assert re.search(r"id:\s*keygen_release", keygen_job), "这一步要有 id，才能记进 ci-log"
    assert "step_keygen_release=" in keygen_job, "注册机的 Release 结果要记进 ci-log（否则无法远程验证）"
    # artifact 那条保留但**不许**再影响判定（额度满了也不该让整轮红）
    assert re.search(r"id:\s*keygen_upload", keygen_job)
    upload_block = keygen_job[keygen_job.index("id: keygen_upload"):]
    assert "continue-on-error: true" in upload_block[:400], \
        "artifact 上传要标成 continue-on-error（额度问题不该让注册机拿不到）"


def test_keygen_uses_the_same_algorithm_as_the_client() -> None:
    """注册机与客户端同源（同一把密钥、同一个函数）—— 两处漂移只在用户注册失败时暴露。"""
    from laoa_trader import licensing

    sys_path_added = False
    import importlib.util
    import sys

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
        sys_path_added = True
    try:
        spec = importlib.util.spec_from_file_location("laoa_keygen_guard", KEYGEN_PY)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        machine = licensing.machine_code()
        assert module.make_code(machine) == licensing.expected_code(machine)
    finally:
        if sys_path_added:
            sys.path.remove(str(ROOT / "src"))


@pytest.mark.parametrize("name", ["keygen.spec", "keygen.exe"])
def test_packaging_doc_mentions_the_keygen_output(name: str) -> None:
    r"""文档里要写清"怎么拿到注册机"（作者不看代码也知道该敲哪条命令）。

    两种路径分隔符都认：那是给 Windows 用的教程，里面写的是 `build\keygen.spec`，
    而 Linux/macOS 上写的是 `build/keygen.spec` —— 只认一种会让文档"明明写了却不通过"。
    """
    doc = (ROOT / "docs" / "打包教程.md").read_text(encoding="utf-8")

    assert f"build{os.sep}{name}" in doc or name in doc, f"打包教程里没提到 {name}"


# ══════════════════════════════════════════════════════════════════════════
# spec 自身的一致性：注册机的 hiddenimports 必须真的存在、excludes 不许排掉它要用的东西
# ══════════════════════════════════════════════════════════════════════════

#: 注册机会用到的模块（`keygen.py` 自己 + 它 import 的那条链上的每个文件）
_KEYGEN_CHAIN = (
    "build/keygen.py",
    "src/laoa_trader/licensing.py",
    "src/laoa_trader/config.py",
    "src/laoa_trader/clock.py",
    "src/laoa_trader/log.py",
)


def _module_level_imports(path: Path) -> set[str]:
    """一个文件里**模块级**（含 `try:` 里的）导入的顶层模块名。

    为什么要挑模块级：`licensing._db_conn()` 里那种"函数内 import storage"是**懒加载**，
    打包时不会因为 excludes 出事（真出事也是在某个分支上）—— 而模块级的 import 一旦被
    `excludes` 排掉，exe 一开就报缺模块。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_keygen_spec_hiddenimports_all_exist() -> None:
    """`hiddenimports` 里写错一个名字 → 打出来的 exe 一开就报缺模块。逐条 import 验一遍。"""
    import importlib

    text = KEYGEN_SPEC.read_text(encoding="utf-8")
    block = text[text.index("HIDDEN = ["):text.index("EXCLUDES = [")]
    names = re.findall(r'"([\w\.]+)"', block)
    assert names, "没解析出 hiddenimports（spec 结构变了？）"

    for name in names:
        importlib.import_module(name)          # 不存在就 ImportError，用例红


def test_keygen_spec_does_not_exclude_what_it_needs() -> None:
    """`excludes` 排掉的东西，**不许**出现在注册机那条链的模块级导入里。

    这条拦的是很隐蔽的一类错：`excludes` 里顺手排掉 `sqlite3`（看起来"注册机用不到"），
    而 `licensing` 的某个分支真的会经 `storage` 用到它 —— exe 打出来本机看着没问题，
    用户那边一走到那个分支就崩。（我第一版就写错了，这条用例是那时候留下来的。）
    """
    text = KEYGEN_SPEC.read_text(encoding="utf-8")
    block = text[text.index("EXCLUDES = ["):text.index("ICON_PATH")]
    excluded = set(re.findall(r'"([\w\.]+)"', block))
    # `excludes` 里的写法两种都可能有：
    #   * 整包排除（`pandas`）—— 顶层名相同就不能用；
    #   * 只排某个子模块（`PySide6.QtWebEngineCore`）—— **不能**因此判定
    #     "PySide6 被排了"（它自己还要用 QtWidgets）。所以分两种比法。
    root_excluded = {name for name in excluded if "." not in name}
    full_excluded = excluded

    needed: set[str] = set()
    for rel in _KEYGEN_CHAIN:
        needed |= _module_level_imports(ROOT / rel)
    needed_roots = {name.split(".")[0] for name in needed}

    clash_roots = sorted(needed_roots & root_excluded)
    clash_full = sorted(needed & full_excluded)
    assert clash_roots == [], f"注册机链上要用却被整包排掉的模块：{clash_roots}"
    assert clash_full == [], f"注册机链上要用却被精确排掉的模块：{clash_full}"


def test_keygen_spec_paths_point_at_real_files() -> None:
    """spec 里写死的路径必须真的存在（入口脚本、图标）。"""
    text = KEYGEN_SPEC.read_text(encoding="utf-8")

    assert (ROOT / "build" / "keygen.py").is_file()
    assert 'PROJECT_ROOT / "build" / "keygen.py"' in text
    assert 'SRC / "laoa_trader" / "assets" / "icon.ico"' in text
