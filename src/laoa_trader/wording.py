"""给人看的字：本程序里那套"自己写的选股规则"一律叫**策略**（不再叫"公式"）。

为什么单独一个模块（2026-09-22 主人要求：「把公式都改成策略吧 这样好看点」）：

* **只改显示，一个字节的数据都不改**。库里存的还是 `公式·放量上攻`
  （`stock_pool.strategy` / `watchlist.source_strategy`）、目录还是 `formulas/`、
  配置键还是 `enabled_formulas`、CLI 参数与代码标识符（`Formula` / `formula_group` /
  `formulas.py`）**全部原样** —— 升级不迁移数据、不回写历史值（"历史行不许被改写"
  是这个项目一直守着的口径）。
* 显示口径必须**只有一份**：`公式·X` 与内置策略的 `策略·短期反转` 长得不一样时，
  用户得学两套说法；现在两者都显示成 `策略·…`，来源列一眼看得懂。
* 老的合成前缀还有别的去处（推送正文、桌面导出、行 tooltip、结果表来源列），
  散着改迟早出现"一处改了、一处没改"，所以都从这里走。

⚠️ 判断"这一行是不是自定义策略"仍然看 `公式·` 前缀（`formula_group.is_formula_strategy`）：
那是**数据里的值**，不是给人看的字，不能跟着改。
"""

from __future__ import annotations

#: 库里/配置里用的合成名前缀（**内部值，不许改**）。
#: 与 `strategy.formula_group.FORMULA_PREFIX` 是同一个字面量 —— 这里再写一份是为了
#: 让本模块**不依赖任何内部模块**（谁都 import 得到，也不会绕出循环依赖）。
STORED_FORMULA_PREFIX = "公式·"

#: 界面上显示的前缀：与内置策略的 `策略·短期反转` 统一（`pool.STRATEGY_SOURCE_PREFIX`）。
DISPLAY_STRATEGY_PREFIX = "策略·"

#: 界面上不再出现的那个词（用例扫界面文案时用的判据，见 `tests/test_wording.py`）。
OLD_WORD = "公式"

#: 界面上改用的词。
NEW_WORD = "策略"


def display_strategy(name: str) -> str:
    """策略名 → **显示用的**写法：`公式·放量上攻` → `策略·放量上攻`，其余原样返回。

    为什么只换前缀、不做全文替换：用户自己起的名里可能真带"公式"两个字
    （"我的公式一"），那是他的东西，不该被改字。
    """
    text = str(name or "")
    if text.startswith(STORED_FORMULA_PREFIX):
        return DISPLAY_STRATEGY_PREFIX + text[len(STORED_FORMULA_PREFIX):]
    return text


def display_words(text: str) -> str:
    """一整句话里的"公式" → "策略"（来源分类那种短标签用，如 `公式+自选` → `策略+自选`）。

    只用于**界面上的一小块标签**；库里存的值（`row["source"]`）不动，
    所以调用它的地方都是"要给人看之前"那一下。
    """
    return str(text or "").replace(OLD_WORD, NEW_WORD)


__all__ = [
    "DISPLAY_STRATEGY_PREFIX",
    "NEW_WORD",
    "OLD_WORD",
    "STORED_FORMULA_PREFIX",
    "display_strategy",
    "display_words",
]
