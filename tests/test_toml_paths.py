r"""Windows 路径写进 config.toml：**必须转义**（CI 上 18 条失败的根因）。

根因链条（GitHub Actions build-windows 日志）
-------------------------------------------
    tmp_path = C:\Users\runneradmin\AppData\Local\Temp\pytest-123\test_x0
    → f'data_dir = "{tmp_path}"' 里的 \U 被 tomllib 当成转义序列
    → TOMLDecodeError: Invalid hex value (at line 1/2, column 17)
    → load_config 整体退回默认值 → 下游断言全部连锁失败

这里做三件事，缺一不可：

1. **转义后必须解析成功**（`config_error == ""`，且路径一字不差）；
2. **不转义时确实会坏** —— 否则第 1 条只是"怎么写过都能过"，证明不了任何东西；
   顺带钉住更阴的一种：`C:\temp` 里的 `\t` **不报错**，但路径被悄悄改成 `C:<TAB>emp`；
3. **静态扫描 `tests/`**：任何把路径插进 `data_dir = "…"` 的地方都必须包在
   `p()` / `q()` / `escape()` 里，防止以后又手写 f-string 把这个坑埋回来。

产品侧不背这个锅：`config._toml_value()` 本来就会转义（写回流程另见
`test_config_writeback.py`），坏的是**测试自己拼 TOML 文本**。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from laoa_trader.config import default_data_dir, load_config

from tests._toml import escape, p, q

TESTS_DIR = Path(__file__).resolve().parent

#: Windows 临时目录的真实长相（含 `\U`、`\t`、中文与空格）
WIN_PATH = r"C:\Users\runneradmin\AppData\Local\Temp\pytest-123\我的 数据"


def _write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── 1) 转义后：解析成功，路径一字不差 ──


def test_escaped_windows_path_parses(tmp_path: Path) -> None:
    """`data_dir = q(win_path)`：tomllib 能读懂，且值原样取回。"""
    path = _write_config(tmp_path / "config.toml", f"data_dir = {q(WIN_PATH)}\n")
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""                       # 没有"解析失败"告警
    assert str(cfg.data_dir) == WIN_PATH                # 反斜杠没被吃掉、也没变形


def test_escaped_path_with_p_inside_quotes(tmp_path: Path) -> None:
    """老写法（引号外加 `p(...)`）同样有效 —— 两种写法都留着用。"""
    path = _write_config(tmp_path / "config.toml", f'data_dir = "{p(WIN_PATH)}"\n')
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""
    assert str(cfg.data_dir) == WIN_PATH


def test_escape_handles_quotes_and_keeps_chinese(tmp_path: Path) -> None:
    """双引号要转义；中文/空格/单引号不受影响（转义过头会把路径改坏）。"""
    weird = r'D:\我 的"数据"\dump'
    path = _write_config(tmp_path / "config.toml", f"data_dir = {q(weird)}\n")
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""
    assert str(cfg.data_dir) == weird
    assert escape("600519") == "600519"
    assert escape("a'b c") == "a'b c"                   # 单引号无需转义
    assert escape("C:\\temp") == "C:\\\\temp"            # 只动反斜杠


# ── 2) 不转义：必须坏（证明上面那条测试有意义）──


def test_unescaped_windows_path_breaks_parsing(tmp_path: Path) -> None:
    r"""**不转义**时 `\U` 让整个 config.toml 解析失败并静默退回默认值。

    这正是 CI 上发生的事：配置没生效 → 数据目录回到默认值 → 找不到库 → 断言全崩。
    """
    raw = 'data_dir = "C:\\Users\\me\\data"\n'          # 写进文件后就是未转义的样子
    path = _write_config(tmp_path / "config.toml", raw)
    assert path.read_text(encoding="utf-8").count("\\") == 3   # 确实"没转义"

    cfg = load_config(path, use_env=False)
    assert "解析失败" in cfg.config_error
    assert "Invalid hex value" in cfg.config_error      # 与 CI 日志逐字一致
    # 关键后果：配置被丢掉、退回默认值（而不是报错让用户看见）
    assert cfg.data_dir == default_data_dir()
    assert str(cfg.data_dir) != r"C:\Users\me\data"


def test_unescaped_backslash_silently_corrupts_path(tmp_path: Path) -> None:
    r"""比报错更阴的一种：`\t` 不报错，但路径被悄悄改成 `C:<TAB>emp`。"""
    intended = r"C:\temp"
    path = _write_config(tmp_path / "bad" / "config.toml", 'data_dir = "C:\\temp"\n')
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""                       # 解析"成功"了
    assert str(cfg.data_dir) == "C:\temp"                # 但里面是真 TAB（\t 被解释了）
    assert str(cfg.data_dir) != intended                # ← 与用户以为的路径不同
    # 转义之后才是对的
    fixed = _write_config(tmp_path / "fixed" / "config.toml", f"data_dir = {q(intended)}\n")
    assert str(load_config(fixed, use_env=False).data_dir) == intended


def test_toml_literal_string_keeps_backslashes(tmp_path: Path) -> None:
    r"""单引号（TOML 字面量字符串）里反斜杠**本来就是字面量**，转义反而是错的。

    这条是给"修 Windows 路径"这个动作加的护栏：修的时候很容易顺手把
    `p()` 也套到单引号模板上 —— 那样 `C:\Users` 会变成 `C:\\Users`，
    不报错、但路径是错的（比解析失败更难发现）。
    """
    literal = "data_dir = '" + WIN_PATH + "'\n"
    path = _write_config(tmp_path / "lit" / "config.toml", literal)
    cfg = load_config(path, use_env=False)
    assert cfg.config_error == ""                       # 不转义也能解析
    assert str(cfg.data_dir) == WIN_PATH                # 反斜杠原样保留

    # 反过来：在单引号里多转一道 → 值里真的出现双反斜杠（错得很安静）
    over = "data_dir = '" + f"{p(WIN_PATH)}" + "'\n"
    over_path = _write_config(tmp_path / "over" / "config.toml", over)
    over_cfg = load_config(over_path, use_env=False)
    assert over_cfg.config_error == ""                  # 依然"解析成功"
    # 看 **TOML 解析出来的原始字符串**，不看 `str(cfg.data_dir)`：
    # Windows 上 `Path` 会把重复的反斜杠折叠掉（`C:\\Users` → `C:\Users`），
    # 这个错误在那边就"看不见"了 —— 但用户拿到的路径确实是错的。
    # （b7a5f31 的 Windows 构建就是这么挂的。）
    raw_value = tomllib.loads(over_path.read_text(encoding="utf-8"))["data_dir"]
    assert raw_value.count("\\") == 2 * WIN_PATH.count("\\")   # 每一道都变成了两道
    assert "\\\\" in raw_value
    assert raw_value != WIN_PATH


# ── 3) 静态扫描：不许再手写没转义的 TOML 路径 ──

#: 把路径插进 data_dir 的双引号值里（f-string 与 str.format 模板都长这样）
_INTERP = re.compile(r'data_dir\s*=\s*"\{([^{}]*)\}"')
#: `.format(...)` 里给 data_dir 的那个实参（拼开写，免得被自己扫出来）
_FORMAT_ARG = re.compile(
    r"\.format\(\s*data" + r"_dir\s*=([^)]*)"   # 拼开写：本文件也在扫描范围内
)
#: 路径被放进 TOML **字面量**字符串（单引号）里 —— 那里反斜杠本来就是字面量，
#: 所以**不许**再转义（转了就变成双反斜杠，路径静默出错，另一种难查）
_LITERAL = re.compile(r"data_dir\s*=\s*'\{([^{}]*)\}'")
#: 静态能认出来的"这个值已经处理过了"
_ESCAPED = re.compile(r"\b(p|q|escape|toml_str)\(")   # toml_str 是规范名，p/q 是短名
#: 显式豁免标记：**故意**写不转义的用例（例如"证明不转义确实会炸"的反面证据）。
#: 目的是让豁免看得见、可 grep，而不是让扫描器对整类写法睁一只眼闭一只眼。
_ALLOW_MARKER = "toml-guard: allow-unescaped"
#: 裸标识符：str.format / % 模板的占位符（由 `_FORMAT_ARG` 那条规则兜住）
_PLACEHOLDER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _scan_tests() -> list[str]:
    """扫 `tests/*.py`，返回"把路径插进 TOML 但没转义"的位置。"""
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ALLOW_MARKER in line:
                continue                       # 显式豁免（见 _ALLOW_MARKER 说明）
            for match in _INTERP.finditer(line):
                expr = match.group(1).strip()
                if _PLACEHOLDER.match(expr) or _ESCAPED.search(expr):
                    continue
                offenders.append(f"{path.name}:{lineno} 未转义：{line.strip()}")
            for match in _FORMAT_ARG.finditer(line):
                if not _ESCAPED.search(match.group(1)):
                    offenders.append(
                        f"{path.name}:{lineno} .format(…data_dir…) 未转义：{line.strip()}"
                    )
            for match in _LITERAL.finditer(line):
                if _ESCAPED.search(match.group(1)):
                    offenders.append(
                        f"{path.name}:{lineno} 单引号字面量里不该转义：{line.strip()}"
                    )
    return offenders


def test_no_unescaped_paths_written_into_toml() -> None:
    """扫描 `tests/*.py`：把路径写进 `data_dir = …` 必须走 `p()` / `q()`。"""
    offenders = _scan_tests()
    assert not offenders, (
        "把路径写进 config.toml 时要用 tests._toml 的 p()/q() 转义"
        "（Windows 的反斜杠会让 tomllib 解析失败 → 配置退回默认值 → 断言连锁失败）：\n"
        + "\n".join(offenders)
    )


def test_no_percent_template_for_data_dir() -> None:
    """printf 风格的占位符（`%` + `s`）也禁止再用来写 data_dir。

    为什么单独禁：`%` 的实参不在同一行，静态检查认不出它有没有转义 ——
    老代码里正是"三引号模板 + `% _p(...)`"那种写法。统一改成 f-string，
    才能被上面的规则盯住。
    """
    offenders = [
        f"{path.name}:{lineno}"
        for path in sorted(TESTS_DIR.glob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "%" + "s" in line and "data_dir" in line    # 拼开写：本文件也在扫描范围内
    ]
    assert not offenders, "请改用 f-string + tests._toml.p()：" + ", ".join(offenders)


def test_scanner_actually_bites(monkeypatch, tmp_path: Path) -> None:
    """反向验证扫描器有效：喂一段"没转义的写法"必须被点出来。

    没有这条，上面两个静态用例可能因为正则写错而**永远为真**（假绿）。
    注意下面这段"坏样本"是**拼出来的**：本文件自己也在被扫描范围内，
    所以不能让它以连续文本的形式出现（否则扫描器会先把本文件点出来）。
    """
    quote = '"'
    bad_content = "\n".join([
        "from pathlib import Path",
        "def test_x(tmp_path):",
        "    Path(" + quote + "c.toml" + quote + ").write_text(f'data_dir = "
        + quote + "{tmp_path.name}" + quote + "')",
        "    SAMPLE.format(data_" + "dir=tmp_path.name)",
        # 单引号字面量里多转一道 → 也要被点出来
        "    d = f\"data_dir = '" + "{p(tmp_path.name)}" + "'\"",
    ]) + "\n"

    fake = tmp_path / "tests"
    fake.mkdir()
    (fake / "test_bad.py").write_text(bad_content, encoding="utf-8")
    monkeypatch.setattr("tests.test_toml_paths.TESTS_DIR", fake)

    offenders = _scan_tests()
    assert len(offenders) == 3, offenders
    assert any("test_bad.py:3" in o and "未转义" in o for o in offenders), offenders
    assert any("test_bad.py:4" in o and "data_dir" in o for o in offenders), offenders
    assert any("test_bad.py:5" in o and "字面量" in o for o in offenders), offenders

    # 显式豁免标记：只放过那一行，别的地方照咬
    (fake / "test_bad.py").write_text(
        "def test_x(tmp_path):\n"
        "    a = f'data_dir = " + quote + "{tmp_path.name}" + quote + "'  "
        + "  # " + _ALLOW_MARKER + "\n",
        encoding="utf-8",
    )
    assert _scan_tests() == []

    # 换成转义写法后必须干净（扫描器不会"见谁都咬"）
    (fake / "test_bad.py").write_text(
        "from tests._toml import p\n"
        "def test_x(tmp_path):\n"
        "    a = f'data_dir = " + quote + "{p(tmp_path)}" + quote + "'\n"
        "    b = SAMPLE.format(data_" + "dir=p(tmp_path))\n",
        encoding="utf-8",
    )
    assert _scan_tests() == []
