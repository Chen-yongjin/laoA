"""给人看的字：来源列里那个名字**不带任何前缀**，就是策略名本身。

口径变过两次，这里写清楚免得来回改：

* 2026-09-22 主人要求「把公式都改成策略吧 这样好看点」→ 来源列显示成 `策略·X`；
* 2026-09-23 主人要求「把公式名称中的策略两个字去掉，无意义」→ **前缀也去掉**，
  来源列就显示 `X`（`尾盘匹配策略`）；
* 同日主人又要求「为什么要+自选 什么策略跑出来的 直接记录策略名称 只有用户自己
  输入的才能算自选来源」→ **`+自选` 那个尾巴也不再显示**：来源列只回答"是哪条
  策略选出来的"，用户手工加进去、没有策略来源的票才写「自选」。

为什么单独一个模块：

* **只改显示，一个字节的数据都不改**。库里存的还是 `公式·放量上攻`
  （`stock_pool.strategy` / `watchlist.source_strategy`）、目录还是 `formulas/`、
  配置键还是 `enabled_formulas`、CLI 参数与代码标识符（`Formula` / `formula_group` /
  `formulas.py`）**全部原样** —— 升级不迁移数据、不回写历史值（"历史行不许被改写"
  是这个项目一直守着的口径）。
* 显示口径必须**只有一份**：库里的 `公式·X`、老内置策略的类名（`ReversalStrategy`）、
  以及更早版本写进备注的 `策略·X`，**三条路都要剥成同一个名字**，
  否则来源列会出现"有的带前缀、有的不带"。
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

#: **老版本**界面上用过的显示前缀（2026-09-22 那版写成 `策略·X`）。
#: 现在显示端一样要把它剥掉 —— 更早写进备注/来源列的 `策略·X` 还得能读成 `X`
#: （留着这个常量就是为了那一批老文本，别删）。
LEGACY_DISPLAY_PREFIX = "策略·"
#: 兼容旧名（外部若还引用它，含义同上：一个**要剥掉**的前缀，而不是要加上的前缀）。
DISPLAY_STRATEGY_PREFIX = LEGACY_DISPLAY_PREFIX

#: 界面上不再出现的那个词（用例扫界面文案时用的判据，见 `tests/test_wording.py`）。
OLD_WORD = "公式"

#: 界面上改用的词。
NEW_WORD = "策略"


def display_strategy(name: str) -> str:
    """策略名 → **显示用的**写法：剥掉前缀，只留名字本身。

        `公式·放量上攻` → `放量上攻`      （库里存的内部合成名）
        `策略·短期反转` → `短期反转`      （老版本显示过、写进过备注的写法）
        `尾盘匹配策略`  → `尾盘匹配策略`  （本来就干净）
        `我的策略一`    → `我的策略一`    （用户自己起的名字，一个字都不动）

    为什么只剥**前缀**、不做全文替换：用户自己起的名里可能真带"策略"或"公式"
    两个字（"我的策略一"），那是他的东西，不该被改字。
    """
    text = str(name or "")
    for prefix in (STORED_FORMULA_PREFIX, LEGACY_DISPLAY_PREFIX):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def display_words(text: str) -> str:
    """一整句话里的"公式" → "策略"（来源分类那种短标签用，如 `公式+自选` → `策略+自选`）。

    只用于**界面上的一小块标签**；库里存的值（`row["source"]`）不动，
    所以调用它的地方都是"要给人看之前"那一下。
    """
    return str(text or "").replace(OLD_WORD, NEW_WORD)


#: 老数据/老界面用过的"组合尾巴"：`X+自选`。2026-09-23 起来源列不再写它
#: （主人："为什么要+自选 什么策略跑出来的 直接记录策略名称 只有用户自己输入的才能算自选来源"）。
STORED_WATCH_SUFFIX = "+自选"


def strip_watch_suffix(text: str) -> str:
    """把老数据里的 `X+自选` 剥成 `X`（**只剥这个尾巴**，别的一律不动）。

    为什么要它：来源列在 09-21~09-23 之间会写成 `公式·X+自选` / `X+自选`，
    那些值可能已经存进 `source_label` 或写进了桌面导出文件；读回来时若不剥，
    同一只票就会一会儿带尾巴、一会儿不带。
    """
    return str(text or "").replace(STORED_WATCH_SUFFIX, "")


def display_source_label(text: str) -> str:
    """**来源列专用**的一条龙：剥内部前缀 + 剥老的 `+自选` 尾巴。"""
    return strip_watch_suffix(display_strategy(text))


__all__ = [
    "DISPLAY_STRATEGY_PREFIX",
    "LEGACY_DISPLAY_PREFIX",
    "NEW_WORD",
    "OLD_WORD",
    "STORED_FORMULA_PREFIX",
    "STORED_WATCH_SUFFIX",
    "display_source_label",
    "display_strategy",
    "strip_watch_suffix",
    "display_words",
]
