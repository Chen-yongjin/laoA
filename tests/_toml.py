r'''测试专用小工具：把**路径**安全地写进 TOML 字符串。

为什么需要它
------------
Windows 上 pytest 的临时目录形如 ``C:\Users\runneradmin\AppData\Local\Temp\...``。
反斜杠直接塞进 TOML 的**基本字符串**（``"..."``）里会被当成转义序列：

- ``\U`` 开头 → ``tomllib.TOMLDecodeError: Invalid hex value (at line 1, column 17)``
  → ``load_config`` 整体解析失败、**静默退回默认值** → 后面所有断言连锁失败。
  （GitHub Actions 的 build-windows 上 18 条失败全是这一个根因。）
- ``\t`` / ``\n`` 开头 → **不报错，但路径被悄悄改掉**（``C:\temp`` 会变成 ``C:<TAB>emp``），
  比报错更难查。

产品侧没有这个问题：``config._toml_value()`` 本来就会转义并加引号。
这里补的是**测试侧** —— 测试自己拼 TOML 文本时也得转义。

> 注意：本文件的 docstring 必须是 **raw 字符串**（``r"""``），否则上面那个
> ``C:\Users`` 会让 Python 自己在导入时抛 ``SyntaxError: truncated \UXXXXXXXX escape`` ——
> 同一个坑的第二种踩法。

用法
----
    from tests._toml import p, q

    f'data_dir = "{p(cfg.data_dir)}"\n'      # 放进已有的双引号里
    f'data_dir = {q(cfg.data_dir)}\n'        # 连引号一起给
    SAMPLE.format(data_dir=p(tmp_path))      # str.format 模板同样要先转义

``tests/test_toml_paths.py`` 里有一个静态检查用例，专门盯着"又有人手写没转义的
``data_dir = "…"``"这件事。
'''

from __future__ import annotations

__all__ = ["escape", "p", "q", "toml_str"]


def escape(value: object) -> str:
    r"""转义 TOML 基本字符串（`"..."`）的内容（不带引号）。

    只处理两件在路径里真会发生的事：

    - 反斜杠 → 双反斜杠（Windows 路径的关键，见模块 docstring）；
    - 双引号 → `\"`（少见，但会直接截断字符串）。

    其余字符原样保留：中文、空格、单引号在基本字符串里都合法。
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


#: 全仓**唯一**一份转义实现。`p` / `toml_str` 是同一个东西的两个名字：
#: `p` 短、调用点好看；`toml_str` 名字自带说明，也方便从 `tests.conftest` 统一 import。
toml_str = escape


def p(value: object) -> str:
    """转义后**不带引号** —— 放进已有的 `"..."` 里用（`toml_str` 的短名字）。"""
    return escape(value)


def q(value: object) -> str:
    """转义后**带引号** —— 直接当 TOML 值用，省得自己数引号。"""
    return f'"{escape(value)}"'
