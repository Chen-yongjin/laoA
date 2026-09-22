"""公式库：公式文件的**存/取**、**试算**、**成绩单**，以及目录定位。

与 `strategy/formula.py` 的分工
-------------------------------
引擎那一份（`strategy/formula.py`，2300 行）只管"把文本变成能算的东西"：
词法、语法、白名单求值、`FormulaError`。它**刻意不碰磁盘、不碰界面、不碰配置**。

本模块是它的"产品外壳"：

* **目录定位** —— `formula_dir()` 同时支持源码运行与打包后的 exe（见下）；
* **保存/删除** —— 文件名安全化 + 引擎认的注释头（`# 名称:` / `# 说明:`）；
* **参与选股名单** —— `enabled_names()`：把 `config.toml` 里的 `enabled_formulas`
  收紧成"目录里真实存在且语法通过"的名字，**找不到/写错的忽略并记日志**；
* **运行 / 成绩单** —— 供界面上的【运行】（原【试算】）用（只读本地库，不联网）。

为什么"保存"要先做名称安全化
-----------------------------
公式名称是用户随手打的（"5日线上放量"），而它同时要当**文件名**。用户完全可能
打出 `涨/跌`（Windows 上直接建不出这个文件）、`A:B`（时间戳风格的冒号）、
结尾一个点（Windows 会把 `abc.` 悄悄变成 `abc`）—— 这些都不该表现成
"点了保存但列表里没有"。所以统一在这里把非法字符换成下划线、去掉首尾空格与结尾点，
**并且把安全化后的名字回显给用户**（界面填回名称框），用户看到的与磁盘上的一致。

为什么注释头由本模块拼、而**不**让用户写进编辑框
------------------------------------------------
引擎的公式文件格式是"文件开头连续的 `#` 行 = 名称/说明注释头，其余是公式体"。
如果让用户在编辑框里写 `# 名称: xxx`，他改一次名字就要记得改两处，改漏了就会出现
"列表里显示旧名字、文件里写着新名字"。所以界面只让用户填名称与正文，
注释头在这里拼；读取时引擎的 `_parse_formula_file()` 再把两者拆开。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Sequence

from laoa_trader.data.engine import HFQ_TABLE
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm

logger = get_logger(__name__)

#: 公式目录名（exe 同级 / 仓库根都是它）
FORMULA_DIR_NAME = "formulas"

#: 用户显式指定公式目录的环境变量（换机器、放共享盘、测试都靠它）
FORMULA_DIR_ENV = "LAOA_TRADER_FORMULAS"

#: Windows 文件名里**非法**的字符（换成下划线）。顺带把控制字符也挡掉：
#: 从别处复制来的公式名里偶尔夹着不可见字符，那种文件名在资源管理器里看着是空的。
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 文件名（= 公式名）的长度上限。Windows 全路径上限 260，公式名留 60 足够，
#: 而且过长的名字在列表/推送里也读不下去。
MAX_NAME_CHARS = 60

#: 说明的长度上限（注释头一行太长会让文件很难看）
MAX_DESC_CHARS = 200

#: 依赖本地涨停池的两个函数 —— 它们的"历史坑"要在界面与成绩单里讲清楚
LIMIT_UP_FUNCTIONS: tuple[str, ...] = ("连板", "涨停天数")

#: 用到 `连板()` / `涨停天数()` 时必须一起显示的中文提醒（**一句话，别吓人**）。
#:
#: 为什么必须有：这两个函数读的是本地 `limit_up_pool` 表，而那张表是**逐日同步攒出来的**
#: —— 用户第一次装好、只同步了最近几天时，历史日期一律读到 0，公式会"选不出票"或
#: "回测全是 0 信号"。这看起来像公式写错了，实际是数据没攒够，必须当场说清楚。
LIMIT_UP_HINT = (
    "注意：连板() / 涨停天数() 读的是本地涨停池（逐日同步攒的），"
    "早期日期会读到 0 —— 历史越早，信号越可能偏少。"
)

#: 用到 `FINANCE(...)` 时的提醒（**非阻断**：公式照跑，只是口径要讲清）。
#: 主人 2026-09-21 指定："你直接在程序后台把这个函数等同于流通市值就行了啊" ——
#: 于是它不再是"不支持"，但**必须**在【校验】里说一句，否则用户会以为它真是通达信那个股本。
FINANCE_HINT = (
    "注意：本程序把 FINANCE(...) 按「流通市值（亿元）」处理 —— 与通达信口径不同"
    "（通达信里它是流通股本/总股本这类财务项，本地没有财报数据）；"
    "拿它当股本用的公式结果会偏。"
)

#: 用到 `DYNAINFO(...)` 时的提醒（同样**非阻断**）：它被映射成量比，
#: 与通达信"取盘中动态行情某字段"完全不是一回事，必须说清。
DYNAINFO_HINT = (
    "注意：本程序把 DYNAINFO(...) 按「量比（倍）」处理 —— 与通达信口径不同"
    "（它本体是盘中动态行情，本地只有收盘后的日线）；"
    "写成 DYNAINFO(其它编号) 也一样按量比算。"
)

#: 默认的成交口径（成绩单用）。`B` = D+1 收盘买 → D+2 收盘卖：
#: 与 `research/scorecard.py` 的默认并列口径一致，也是散户真能执行的那一档。
DEFAULT_CONVENTION_KEY = "B"

#: 【运行】的结果**显示**最多列出多少只（名称（代码）格式太长，列满一屏就够了）。
#: 注意：它只影响界面显示（返回值里的 `shown`），**不影响 `hits`** ——
#: 【导出选股结果】写的是全量命中（见 `preview_hits` 的 Returns）。
PREVIEW_LIMIT = 20

#: 成绩单的进度回调类型（与本项目其它进度回调同一个签名：阶段 + 已完成 + 总数）
ProgressCb = Callable[[str, int, int], None]


# ══════════════════════════════════════════════════════════════════════════
# 目录定位
# ══════════════════════════════════════════════════════════════════════════


def repo_root() -> Path:
    """仓库根（`laoA/`）：本文件在 `laoA/src/laoa_trader/formulas.py`。"""
    return Path(__file__).resolve().parents[2]


def bundled_formula_dir() -> Path | None:
    """**随包分发**的示例公式目录（只读，找不到返回 None）。

    两种形态：
    * 打包后：PyInstaller 把 spec 里 `DATAS` 的 `formulas/` 解到 `_MEIPASS/formulas`；
    * 源码运行：就是仓库根的 `formulas/`。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        packed = Path(meipass) / FORMULA_DIR_NAME
        if packed.is_dir():
            return packed
    root = repo_root() / FORMULA_DIR_NAME
    return root if root.is_dir() else None


#: 「这条随包公式播过种了没有」的记录文件（放在**公式目录里**，点开头所以不会被当成公式）。
#:
#: 为什么需要它（2026-09-18 起随包公式要能**补齐**）：新版本多带一条随包公式时，
#: 用户的目录里已经有自己存的公式了 —— 旧规则（"只在空目录复制"）会让那条新公式**永远不出现**；
#: 而直接"缺哪条补哪条"又会让**用户删掉的那条**每次启动都长回来。
#: 只有记下"播过哪些"，才分得清"还没给他"与"他不要"。
SEED_STATE_NAME = ".laoa-seeded.json"

#: **退役的随包公式**：文件名 → 老版本随包内容的 sha256。
#:
#: 为什么需要它（2026-09-18）：用户要求删掉「连板回踩低吸」这条策略，而它在老版本里是
#: 随包公式 `涨停回踩低吸.txt` —— 只从仓库里删文件的话，**已经装过老版本的用户目录里
#: 那一份还在**，他升级后列表里照样留着这条，等于没删。所以启动时顺手清一次。
#:
#: 判据是**哈希逐字节一致**，不是"文件名一样就删"：用户可能把这条公式改成了自己的东西
#: （改阈值、加条件），那是他的劳动成果，删掉等于替他做主 —— 改过的就留着，
#: 同时记进 `SEED_STATE_NAME` 那份名单里（见 `_retire_bundled_formulas`）。
RETIRED_BUNDLED_FORMULAS: dict[str, str] = {
    "涨停回踩低吸.txt": "ca9f0533ce63c35cc62f05d035b7d51ace03fafd6de5bf9b0b58289b7c76cea6",
}


def _read_seed_state(target: Path) -> set[str]:
    """读播种记录（读不出来就当没播过 —— 宁可多复制一份，也别让新公式不出现）。"""
    try:
        data = json.loads((target / SEED_STATE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        logger.debug(f"读随包公式播种记录失败（当作没播过）：{exc}")
        return set()
    if not isinstance(data, list):
        return set()
    return {str(name) for name in data}


def _write_seed_state(target: Path, seeded: set[str]) -> None:
    """写播种记录。写不进去只记日志：最坏结果是"下次启动再查一遍文件在不在"。"""
    try:
        (target / SEED_STATE_NAME).write_text(
            json.dumps(sorted(seeded), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"写随包公式播种记录失败（不影响使用）：{exc}")


def _retire_bundled_formulas(target: Path, seeded: set[str]) -> None:
    """清掉**退役的随包公式**（只删"用户没改过"的那些，见 `RETIRED_BUNDLED_FORMULAS`）。

    两个判断，缺一不可：

    1. **哈希与老版本随包的一致** → 那份文件是程序放进来的、用户一个字没动 → 删；
       哈希不一致 → 他改过 → **留着**，并且把他这份"认下来的"记进名单，以后都不再管它
       （否则下一个版本换了退役名单，又会拿旧哈希去比，比不中也不算错，但状态会乱）；
    2. **删完必须记进 `seeded`** —— 这是这次最容易做错的一步：
       `_seed_samples()` 的规则是"缺哪条补哪条"，只要那份文件**还在随包目录里**
       （老版本的 `_internal/formulas` 在用户原地升级时有可能还在），
       不记录就会被下一次启动**原样补回来** —— 表现就是"删了又长回来"，用户会以为程序坏了。
       记进名单 = 明确告诉补齐逻辑"这条我处理过了"。
    """
    for name, digest in RETIRED_BUNDLED_FORMULAS.items():
        if name in seeded:
            continue                      # 上一轮已经处理过（删过或认过）
        path = target / name
        if not path.is_file():
            seeded.add(name)              # 本来就没有：记下来，省得每轮都查一遍
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:            # 权限/占用：留着，下次启动再试
            logger.warning(f"读退役公式 {name} 失败（这次不处理）：{exc}")
            continue
        if hashlib.sha256(data).hexdigest() != digest:
            # 用户改过（或本来就是他自己写的同名文件）→ 那是他的东西，绝不删
            logger.info(f"退役公式 {name} 与随包版本不一致，按用户自己的公式保留")
            seeded.add(name)
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"删退役公式 {name} 失败（下次启动再试）：{exc}")
            continue
        seeded.add(name)
        logger.info(f"已清理退役的随包公式：{name}（它对应「连板回踩低吸」，用户要求删掉）")


def _seed_samples(target: Path, seeded: set[str]) -> None:
    """把随包公式**逐条补齐**到用户目录（缺哪条补哪条，**绝不覆盖**已有的文件）。

    三条规矩：

    1. **同名文件已存在 → 不碰**（用户自己存的、或改过的，永远是他的）；
    2. **播过种的记录里有、但目录里没了 → 也不补** —— 那多半是他**故意删的**，
       每次都长回来会让人以为程序坏了（见 `SEED_STATE_NAME` 的注释）；
    3. **新版本多带的公式**（记录里没有、目录里也没有）→ 复制进去，
       所以"内置策略"能随版本补齐，不用用户去别处找公式文本。

    为什么需要复制这一步：打包后随包公式在 `_internal/formulas`（解包目录，用户看不到、
    也不该往里写），而用户的公式放在 exe 同级的 `formulas/`。第一版不带这一步时，
    新用户打开界面看到的是一个**空列表**，连"载入示例"都没得载 —— 小白第一步就走不下去。

    `seeded` 由调用方（`_sync_bundled_formulas`）读进来、写完再落盘：退役清理与补齐
    必须共用同一份名单，否则两边各写各的，会把对方的记录覆盖掉。
    """
    source = bundled_formula_dir()
    if source is None or source == target:
        return
    bundled = [
        path for path in sorted(source.iterdir())
        if path.is_file() and path.suffix.lower() in fm.FORMULA_SUFFIXES
    ]
    if not bundled:
        return
    copied = 0
    for path in bundled:
        if path.name in seeded or path.name in RETIRED_BUNDLED_FORMULAS:
            # 已播过种 / 已经退役（老解包目录里可能还留着这个文件）：都不复制
            continue
        if (target / path.name).exists():
            # 用户已经有一份同名的：认下来（记进名单），以后他删了也不补
            seeded.add(path.name)
            continue
        try:
            shutil.copyfile(path, target / path.name)
        except OSError as exc:      # 权限/只读盘：示例没到位不该影响启动
            logger.warning(f"随包公式 {path.name} 复制失败：{exc}")
            continue
        seeded.add(path.name)
        copied += 1
    if copied:
        logger.info(f"已把 {copied} 条随包公式放进 {target}")


def _sync_bundled_formulas(target: Path) -> None:
    """随包公式的**一站式同步**：先退役旧的、再逐条补齐缺的，最后把名单落盘。

    为什么合成一个入口：这两件事共用同一份状态文件（`.laoa-seeded.json`），
    各自读一遍写一遍的话，后写的那次会把前一次刚记下的名字冲掉 ——
    退役名单就会"每轮重新判断"，补齐逻辑也会把退役文件当"还没给过他"补回来。

    源码运行 / 随包目录就是目标目录时**什么都不做**（`source == target`）：
    那种情况下"用户的公式目录"就是随包目录本身（开发时是仓库里的 `formulas/`），
    既没有"要补齐的随包公式"，也不该往仓库里写状态文件、更不该去删仓库里的文件。
    """
    source = bundled_formula_dir()
    if source is None or source == target:
        return
    seeded = _read_seed_state(target)
    _retire_bundled_formulas(target, seeded)
    _seed_samples(target, seeded)
    _write_seed_state(target, seeded)


def formula_dir() -> Path:
    """公式目录（**用户自己的公式存这里**），不存在就创建。

    查找顺序（与 `config.config_search_paths()` 同一个思路：打包后优先"看得见的位置"）：

    1. 环境变量 `LAOA_TRADER_FORMULAS`（换机器/放共享盘/测试用）；
    2. **打包后**：exe 同级目录下的 `formulas/` —— 用户双击 exe 就放在旁边，
       备份、发给别人、用记事本改都最直观；
    3. **源码运行**：仓库根 `laoA/formulas/`（就是仓库里那份，随包分发的也是它）。

    随包公式会**逐条补齐**进来：缺哪条补哪条、同名的绝不覆盖、用户删掉的不再补，
    退役的那几条（`RETIRED_BUNDLED_FORMULAS`）还会顺手清掉他没改过的那一份
    （见 `_sync_bundled_formulas`）—— 所以"内置公式"这件事就是"仓库里那个 `formulas/` 目录"。
    """
    override = (os.environ.get(FORMULA_DIR_ENV) or "").strip()
    if override:
        target = Path(override).expanduser()
    elif getattr(sys, "frozen", False):
        target = Path(sys.executable).resolve().parent / FORMULA_DIR_NAME
    else:
        target = repo_root() / FORMULA_DIR_NAME

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 只读盘 / 权限不足：**不抛异常**（界面照开），保存时再给中文错误
        logger.warning(f"公式目录建不出来：{target}（{exc}）")
        return target
    _sync_bundled_formulas(target)
    return target


def formula_files(directory: str | Path | None = None) -> list[fm.FormulaSpec]:
    """加载公式目录（默认 `formula_dir()`）里的全部公式，逐文件报错。"""
    return fm.load_formula_files(directory if directory is not None else formula_dir())


# ══════════════════════════════════════════════════════════════════════════
# 名称安全化 / 保存 / 删除
# ══════════════════════════════════════════════════════════════════════════


def safe_name(name: Any) -> str:
    """公式名 → 安全的文件名（去非法字符、去首尾空格与结尾点、超长截断）。

    **空名返回空串**（调用方据此拒绝保存，见 `name_error`）—— 这里不抛异常：
    "名字不能用"是用户马上能自己改的问题，走提示而不是异常。

    Example:
        >>> safe_name(' 5日线上/放量 ')
        '5日线上_放量'
    """
    text = "" if name is None else str(name)
    text = _ILLEGAL_NAME_CHARS.sub("_", text).strip()
    # Windows：结尾的点与空格会被**静默吃掉**（`abc.` → `abc`），先把它们去掉，
    # 免得"我存的名字"和"列表里的名字"看着不一样
    text = text.rstrip(". ")
    if len(text) > MAX_NAME_CHARS:
        text = text[:MAX_NAME_CHARS].rstrip(". ")
    return text


def name_error(name: Any) -> str:
    """名称能不能用：可用返回空串，否则返回中文原因（界面直接显示）。"""
    raw = "" if name is None else str(name)
    if not raw.strip():
        return "请先填公式名称（例如：5日线上放量）"
    if not safe_name(raw):
        return f"这个名字不能当文件名：{raw!r}（请换成中文或字母数字）"
    return ""


def formula_path(name: Any, directory: str | Path | None = None) -> Path:
    """公式名 → 文件路径（`.txt`；名字会被安全化）。"""
    folder = Path(directory) if directory is not None else formula_dir()
    return folder / f"{safe_name(name)}{fm.FORMULA_SUFFIXES[0]}"


def formula_text(name: str, body: str, description: str = "") -> str:
    """拼出公式文件的全文（注释头 + 公式体）—— 引擎的 `load_formula_files` 认这个格式。"""
    header = [f"# 名称: {name}"]
    if description:
        header.append(f"# 说明: {description}")
    lines = [*header, "", (body or "").strip("\n")]
    return "\n".join(lines).rstrip("\n") + "\n"


def describe_for_save(body: str, name: str = "") -> str:
    """给刚写完的公式自动生成一句说明（编译不过就给空串，不拦着用户存草稿）。

    为什么自动生成：界面上只有"名称"一个输入框，用户不该为了填一行说明再多打一遍
    字段与函数清单 —— 那正是 `Formula.describe()` 能算出来的东西。
    """
    try:
        formula = fm.compile_formula(body, name=name)
    except fm.FormulaError:
        return ""
    return formula.describe()[:MAX_DESC_CHARS]


def save_formula(
    name: str,
    body: str,
    *,
    description: str | None = None,
    directory: str | Path | None = None,
) -> Path:
    """把公式保存成文件（UTF-8，带引擎认的注释头）。

    Args:
        name: 公式名称（会做安全化；**空名拒绝**）。
        body: 公式正文（多行，最后一行是选股条件）。
        description: 说明；None = 用 `describe_for_save()` 自动生成。
        directory: 公式目录；None = `formula_dir()`。

    Returns:
        写入的文件路径。

    Raises:
        ValueError: 名称为空 / 安全化后为空。
        OSError: 写不进去（只读盘、权限），界面负责转成中文提示。
    """
    problem = name_error(name)
    if problem:
        raise ValueError(problem)
    clean = safe_name(name)
    if not clean:
        raise ValueError(f"这个名字不能当文件名：{name!r}")
    folder = Path(directory) if directory is not None else formula_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{clean}{fm.FORMULA_SUFFIXES[0]}"
    if description is None:
        description = describe_for_save(body, clean)
    text = formula_text(clean, body, description)
    # 先写临时文件再替换：中途失败不会把用户原来的公式截成半截
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logger.info(f"公式已保存：{path.name}（{len(body)} 字符）")
    return path


def delete_formula(name: str, directory: str | Path | None = None) -> bool:
    """删除公式文件；文件不存在返回 False（**不抛异常**）。"""
    path = formula_path(name, directory)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    logger.info(f"公式已删除：{path.name}")
    return True


# ══════════════════════════════════════════════════════════════════════════
# 参与选股名单（config.toml 的 enabled_formulas）
# ══════════════════════════════════════════════════════════════════════════


def enabled_names(cfg: Any = None, directory: str | Path | None = None) -> list[str]:
    """本次**真正参与选股**的公式名（按目录里的顺序）。

    三道收紧，缺一不可：

    1. 名字写在 `config.toml` 里、但 `formulas/` 里**没有这个文件** → 忽略 + 记日志
       （用户删了文件、或改名了；静默失败会让他以为"公式选股坏了"）；
    2. 文件有、但公式**语法错** → 忽略 + 记日志（坏公式不该拖垮其它公式）；
    3. 名字里的首尾空格、重复项 → 去掉。

    为什么"写成名字而不是文件路径"：用户眼里的公式就是它的名字，
    在界面上勾选、在配置里手写都该写名字；文件名的安全化规则改了他也不用动配置。
    """
    from laoa_trader.config import get_config

    cfg = cfg if cfg is not None else get_config()
    wanted = [str(n).strip() for n in (getattr(cfg, "enabled_formulas", None) or [])]
    wanted = [n for n in wanted if n]
    if not wanted:
        return []

    specs = formula_files(directory)
    by_name = {spec.name: spec for spec in specs}
    picked: list[str] = []
    for name in wanted:
        spec = by_name.get(name)
        if spec is None:
            logger.warning(
                f"公式选股：config.toml 里的 enabled_formulas 写着 {name!r}，"
                f"但公式目录里没有这条公式，已忽略"
            )
            continue
        if not spec.ok:
            logger.warning(f"公式选股：{name} 语法有错（{spec.error_text}），本次不参与")
            continue
        if name not in picked:
            picked.append(name)
    return picked


# ══════════════════════════════════════════════════════════════════════════
# 连板/涨停天数的历史坑
# ══════════════════════════════════════════════════════════════════════════


def tdx_compat_notes(formula: fm.Formula | None) -> list[str]:
    """公式里用到"口径与本项目不同"的通达信函数时，返回**非阻断**的中文提醒（可能多条）。

    为什么单独一个入口（而不塞进 `limit_up_hint`）：`limit_up_hint` 说的是"数据攒得够不够"，
    这里说的是"这个函数我们按另一个口径实现了" —— 两件事的下一步动作完全不同
    （前者等同步，后者是提醒他核对口径），混在一句话里谁也说不清。
    目前只有 `FINANCE` 一条；将来再加同类函数（例如有人要求 `DYNAINFO` 也硬映射）
    就往这里加，界面会把每一条单独列一行。
    """
    if formula is None:
        return []
    used = set(getattr(formula, "functions", ()) or ())
    notes: list[str] = []
    if "FINANCE" in used:
        notes.append(FINANCE_HINT)
    if "DYNAINFO" in used:
        notes.append(DYNAINFO_HINT)
    return notes


def limit_up_hint(formula: fm.Formula | None) -> str:
    """公式用到 `连板()`/`涨停天数()` 时返回中文提醒，否则空串。

    为什么按"函数名"判而不是按"结果对不对"判：数据攒没攒够是**环境**问题，
    跟公式写得对不对无关；用户需要在**校验通过的那一刻**就被告知，
    而不是等到试算结果偏少再回来怀疑公式。
    """
    if formula is None:
        return ""
    used = set(getattr(formula, "functions", ()) or ())
    return LIMIT_UP_HINT if used & set(LIMIT_UP_FUNCTIONS) else ""


# ══════════════════════════════════════════════════════════════════════════
# 试算：当前库能选出几只
# ══════════════════════════════════════════════════════════════════════════


def latest_trading_day(db_path: str | Path) -> str | None:
    """库里最新的行情日（空库返回 None）。"""
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), timeout=60)
    try:
        row = conn.execute(f"SELECT MAX(date) FROM {HFQ_TABLE}").fetchone()  # noqa: S608
    finally:
        conn.close()
    return str(row[0]) if row and row[0] else None


#: 需要**实时快照**的公式字段（只有今天这一个值，见 `formula.EXTRA_FIELDS`）
SNAPSHOT_FIELDS: tuple[str, ...] = ("流通市值", "换手率")

#: 需要**热门行业**（读库算，不联网）的字段名
HOT_FIELDS: tuple[str, ...] = ("热门行业",)

#: 「热门行业」上榜窗口（交易日）与取前几名 —— 与 `pool.hot_industries` 的默认 top 一致
HOT_WINDOW_DAYS = fm.HOT_INDUSTRY_DAYS
HOT_TOP = 12


def hot_industry_counts(db_path: str | Path, *, days: int = HOT_WINDOW_DAYS,
                        top: int = HOT_TOP) -> dict[str, int]:
    """最近 `days` 个交易日里各行业"上过几次热门榜" → `{行业名: 0~days}`。

    口径与「大盘概览 → 热门板块」那一套**完全同一份实现**（`pool.hot_industries`：
    当日涨停密度 + 行业成分等权涨幅打分取前 `top`），只是**按天各算一次再并集** ——
    用户 2026-09-18 定的口径是"最近 3 天上榜的"：只看当天会把一天的脉冲当热门。

    为什么按天算：涨停密度本来就是**逐日**的量（涨停池有历史），所以"某天上过榜"
    是可以如实算出来的；而动量那一项用的是"截至最新行情"的窗口（见 `hot_industries`
    的说明），所以严格说是"以今天为基准回溯 3 天的榜"。这一点写在文档里，不藏着。
    """
    from laoa_trader import pool      # 延迟导入：pool → formula_group → formulas，模块级会成环
    from laoa_trader.data import storage

    try:
        with storage.connect(db_path) as conn:
            dates = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT date FROM limit_up_pool ORDER BY date DESC LIMIT ?",
                (int(days),),
            ).fetchall()]
    except Exception as exc:  # noqa: BLE001 - 取不到就是"没有热门行业"
        logger.info(f"读交易日失败（热门行业用不了）：{exc}")
        return {}
    counts: dict[str, int] = {}
    for day in dates:
        try:
            names = pool.hot_industries(db_path, top=top, day=day)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"算 {day} 的热门行业失败：{exc}")
            continue
        for industry in names:
            counts[str(industry)] = counts.get(str(industry), 0) + 1
    return counts


def snapshot_extra(
    cfg: Any, symbols: Sequence[str], *, quotes: dict[str, dict] | None = None
) -> tuple[dict[str, dict[str, float]], str]:
    """给公式用的快照字段 → `({代码: {"流通市值": 亿, "换手率": %}}, 提示语)`。

    为什么要这一步：`流通市值` / `换手率` 日线里没有（同花顺的快照端点也不返回，
    见 `data/sources.py` 的 `SUPPLEMENT_FIELDS`），而用户点名要拿它们选股 ——
    所以按 `sources.snapshot_map()`（含"按字段从后面来源补齐"）取一趟，
    再铺成公式认的 `Series.extra`。

    Returns:
        `(extra, note)`：`note` 是**取不到时给用户看的一句人话**（拿到了就是空串）——
        取不到就等于条件永远不成立（0 只），不说清用户会以为公式写错了。
    """
    from laoa_trader.data import sources

    codes = [str(c) for c in dict.fromkeys(symbols) if str(c)]
    if not codes:
        return {}, ""
    if quotes is None:
        try:
            quotes = sources.snapshot_map(cfg, codes)
        except Exception as exc:  # noqa: BLE001 - 取不到就是没有这两个字段
            logger.info(f"取快照失败（市值/换手用不了）：{exc}")
            quotes = {}
        else:
            try:
                # 同花顺的快照不返回这两项 → 按字段从后面的来源（默认免 Key 公开源）补
                sources.supplement_map(cfg, quotes, codes)
            except Exception as exc:  # noqa: BLE001
                logger.info(f"补齐快照字段失败（市值/换手可能不全）：{exc}")
    extra: dict[str, dict[str, float]] = {}
    for symbol, row in (quotes or {}).items():
        values = {name: row.get("circ_mktcap" if name == "流通市值" else "turnover_rate")
                  for name in SNAPSHOT_FIELDS}
        values = {k: float(v) for k, v in values.items() if v is not None}
        if values:
            extra[symbol] = values
    if extra:
        return extra, ""
    return {}, ("⚠️ 市值/换手这两个数现在取不到（没有实时行情快照），"
                "用到它们的条件一律不成立 —— 所以可能一只都选不出来。"
                "交易时段再试，或者在「系统设置 → 数据来源」里确认来源可用。")


def all_symbols(db_path: str | Path) -> list[str]:
    """库里有行情的全部代码（试算/选股要拿它去取快照）。读不出来就返回空列表。

    为什么单独一个函数：`load_series()` 是**逐只 yield** 的生成器（内存友好），
    拿不到"一共有哪些代码"；而取快照必须先把代码表交出去。直接 `list(load_series())`
    会把全市场序列都读进内存 —— 那是几百 MB，正是这个模块一直在避免的事。
    """
    try:
        with sqlite3.connect(str(db_path), timeout=60) as conn:
            return [str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT symbol FROM {fm.HFQ_TABLE} ORDER BY symbol"  # noqa: S608
            ).fetchall()]
    except Exception as exc:  # noqa: BLE001 - 取不到就是没有这两个字段
        logger.info(f"读代码表失败（市值/换手用不了）：{exc}")
        return []


def preview_hits(
    formula: fm.Formula,
    db_path: str | Path,
    *,
    limit: int = PREVIEW_LIMIT,
    start: str | None = None,
    symbols: Sequence[str] | None = None,
    cfg: Any = None,
) -> dict:
    """在**当前本地库**上跑一遍公式，返回最新行情日的命中清单（只读）。

    口径与内置策略一致：只看**每只票最后一根 K 线**，命中即"当日收盘后选中"。
    最后一根 K 线早于全市场最新行情日的票会被跳过（停牌/退市：它的"最后一根"
    是旧的，拿它当"今天选中"是错的）。

    联网与否：公式里用到 `流通市值` / `换手率` 时才会取**一趟**实时快照
    （这两个数日线里没有，见 `snapshot_extra`）；不用它们的公式**一个请求都不发**。

    Returns:
        {"date": 行情日, "count": 命中数, "hits": [{"symbol","name"}...],
         "shown": 展示数, "scanned": 扫过的票数, "skipped": 数据不足的票数,
         "errors": [中文错误...], "notes": [全局提示...]}

        `hits` 是**全量**命中清单（按代码排序、不截断），`limit` 只决定 `shown`：
        界面按 `shown` 截断**显示**（提示区一行放不下 60 只票），而【导出选股结果】
        要写**完整**的一份 —— 给用户的文件里少几只，是最难被发现的那种错。
    """
    day = latest_trading_day(db_path)
    hits: list[dict] = []
    errors: list[str] = []
    #: **全局提示**（与"某只票算不出来"分开）：典型是"市值/换手现在取不到"这类
    #: 影响整次试算的话。为什么必须分开：界面把 `errors` 渲染成
    #: "（N 只票算不出来，已跳过：…）"—— 把一句全局提示混进去，用户看到的是
    #: "1 只票算不出来"，票数是假的、原因也被张冠李戴。
    notes: list[str] = []
    scanned = 0
    skipped = 0
    # 两类"额外字段"按需准备，**用到才做**：
    #   * 快照字段（流通市值/换手率）要联网，取一趟；
    #   * 热门行业读库就能算（不联网），算一次。
    # 两者互不依赖：公式只用热门行业时不该去取快照（也就一个请求都不发）。
    extra: dict[str, dict[str, float]] = {}
    hot: dict[str, int] = {}
    note = ""
    if cfg is not None and set(formula.fields) & set(SNAPSHOT_FIELDS):
        targets = list(symbols) if symbols is not None else None
        if targets is None:
            targets = all_symbols(db_path)
        extra, note = snapshot_extra(cfg, targets)
        if note:
            notes.append(note)
    if set(formula.fields) & set(HOT_FIELDS):
        hot = hot_industry_counts(db_path)
    for series in fm.load_series(db_path, symbols=symbols, start=start, extra=extra,
                                 hot_industries=hot):
        # 数据不够长：公式的滚动窗口一定全是缺值 ⇒ 不可能出信号，直接跳过（省时间）
        if len(series.date) < formula.min_history:
            skipped += 1
            continue
        if day is not None and series.date[-1] != day:
            skipped += 1
            continue
        scanned += 1
        try:
            mask = formula.eval(series)
        except (fm.FormulaError, fm.FormulaDataError) as exc:
            # 一只票算不出来不该让整次试算失败（与选股链路的隔离口径一致）
            # 标的写法统一成**半角** `名称(代码)`（见 docs/改版方案.md 第四节）
            errors.append(f"{series.name}({series.symbol})：{exc}")
            continue
        if bool(mask[-1]):
            hits.append({"symbol": series.symbol, "name": series.name})
    hits.sort(key=lambda hit: hit["symbol"])
    return {
        "date": day,
        "count": len(hits),
        # 全量（界面自己按 `shown` 截断显示，导出要全量）
        "hits": hits,
        "shown": min(len(hits), limit),
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "notes": notes,
    }


# ══════════════════════════════════════════════════════════════════════════
# 成绩单：这条公式历史上到底行不行
# ══════════════════════════════════════════════════════════════════════════


def _convention(key: str) -> Any:
    """按短键取成交口径（`research/scorecard.py` 那份定义，**不另写一套**）。"""
    from laoa_trader.research import scorecard as sc

    for conv in sc.CONVENTIONS:
        if conv.key == key:
            return conv
    return sc.CONVENTIONS[1] if len(sc.CONVENTIONS) > 1 else sc.CONVENTIONS[0]


def run_scorecard(
    formula: fm.Formula,
    db_path: str | Path,
    *,
    progress_cb: ProgressCb | None = None,
    conv_key: str = DEFAULT_CONVENTION_KEY,
    start: str | None = None,
    symbols: Sequence[str] | None = None,
) -> dict:
    """跑公式的历史成绩单（**逐只算，内存只占一只票**），返回结论 + 中文文本。

    口径与 `research/scorecard.py` 的 `compute_outcomes()` **逐行对齐**（进场/出场
    偏移、成交价、以及"一字板买不进就剔除"这条），默认 `B` 口径
    （D+1 收盘买 → D+2 收盘卖）。为什么不去调那个函数：它是面向"全市场 panel"
    的（一次把 10 年数据读进 pandas），而公式成绩单要能在**单机小库**上边跑边报进度 ——
    所以这里按同样的规则逐只走，内存占用与 `load_series()` 一致。

    与策略成绩单**故意不同**的一点：这里**不算 α**（超额收益需要全市场同期基准，
    那正是 `research/scorecard.py` 的活）。所以文本里写的是**绝对收益**，
    免得用户把两种数字混着比。

    Returns:
        {"formula","conv","conv_key","min_history","symbols","samples","days",
         "avg","win_rate","t","best","worst","by_year","dropped","errors",
         "hint","text"}
    """
    from laoa_trader.research import scorecard as sc

    conv = _convention(conv_key)
    per_day: dict[str, list[float]] = OrderedDict()
    by_year: dict[str, list[float]] = OrderedDict()
    samples = 0
    worst: float | None = None
    best: float | None = None
    dropped = 0
    errors: list[str] = []
    scanned = 0

    series_iter = fm.load_series(db_path, symbols=symbols, start=start)
    # 进度需要"总数"，而 `load_series` 是生成器（不知道总数）—— 先按库里的代码数
    # 报总步数：比"进度条永远停在 0%"好得多，且不额外读行情。
    total = _symbol_count(db_path, symbols)
    for series in series_iter:
        scanned += 1
        if progress_cb is not None and (scanned % 25 == 0 or scanned == total):
            progress_cb("公式成绩单", min(scanned, total or scanned), total or scanned)
        if len(series.date) < formula.min_history:
            continue
        try:
            mask = formula.eval(series)
        except (fm.FormulaError, fm.FormulaDataError) as exc:
            # 单只票的缺失值/坏数据只记一笔，不影响其它票（也不让成绩单整体失败）
            # 标的写法统一成**半角** `名称(代码)`（见 docs/改版方案.md 第四节）
            errors.append(f"{series.name}({series.symbol})：{exc}")
            continue
        dates = series.date
        for index in range(len(dates)):
            if not bool(mask[index]):
                continue
            trade = _forward_return(series, index, conv)
            if trade is None:
                dropped += 1
                continue
            ret, exit_date = trade
            samples += 1
            per_day.setdefault(dates[index], []).append(ret)
            by_year.setdefault(exit_date[:4], []).append(ret)
            best = ret if best is None or ret > best else best
            worst = ret if worst is None or ret < worst else worst

    t_stat, avg, days = sc.daily_t(per_day)
    wins = sum(1 for values in per_day.values() for value in values if value > 0)
    win_rate = (wins / samples) if samples else None
    result = {
        "formula": formula.label,
        "conv": conv.description,
        "conv_key": conv.key,
        "min_history": formula.min_history,
        "symbols": scanned,
        "samples": samples,
        "days": days,
        "avg": avg,
        "win_rate": win_rate,
        "t": t_stat,
        "best": best,
        "worst": worst,
        "dropped": dropped,
        "errors": errors,
        "by_year": [
            {"year": year, "n": len(values),
             "avg": sum(values) / len(values),
             "win": sum(1 for v in values if v > 0) / len(values)}
            for year, values in sorted(by_year.items())
        ],
        "hint": limit_up_hint(formula),
    }
    result["text"] = _scorecard_text(result)
    return result


def _symbol_count(db_path: str | Path, symbols: Sequence[str] | None) -> int:
    if symbols is not None:
        return len(list(symbols))
    path = Path(db_path)
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path), timeout=60)
    try:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT symbol) FROM {HFQ_TABLE}"  # noqa: S608
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else 0


def _forward_return(series: fm.Series, index: int, conv: Any) -> tuple[float, str] | None:
    """信号日 `index` 在给定口径下的 (收益率, 出场日期)；不可成交返回 None。

    规则与 `scorecard.compute_outcomes()` 一致：
    - 进场行 = 该股票自身在信号日之后的第 `entry_offset` 根 K 线（停牌自然顺延）；
    - **一字板买不进**（进场价相对信号日收盘涨幅 ≥ `LIMIT_UP_GAP`）→ 这一笔不计；
    - 出场行越界（还没走到那天）→ 不计。
    """
    from laoa_trader.research import scorecard as sc

    base = index + 1                       # D+1 = 信号日之后的第一根 K 线
    entry_idx = base + (conv.entry_offset - 1)
    exit_idx = base + (conv.exit_offset - 1)
    if exit_idx >= len(series.date):
        return None
    signal_close = float(series.close[index]) if series.close[index] else None
    if signal_close is None:
        return None
    opens = series.open
    closes = series.close
    entry_px = float(opens[entry_idx] if conv.entry_price == "open" else closes[entry_idx])
    exit_px = float(opens[exit_idx] if conv.exit_price == "open" else closes[exit_idx])
    if not entry_px or not exit_px:
        return None
    move = (
        (entry_px / signal_close - 1.0)
        if conv.entry_price == "open"
        else (float(closes[entry_idx]) / signal_close - 1.0)
    )
    if move >= sc.LIMIT_UP_GAP:
        return None
    return (exit_px / entry_px - 1.0), series.date[exit_idx]


def _scorecard_text(result: dict) -> str:
    """把成绩单结果排版成**能直接读的中文多行文本**（界面提示区与复制都用它）。"""
    def pct(value: Any) -> str:
        return "—" if value is None else f"{value * 100:+.2f}%"

    lines = [
        f"📊 公式成绩单：{result['formula']}",
        f"口径：{result['conv']}（与策略成绩单同一套规则；**绝对收益**，这里不算 α）",
        f"样本：{result['samples']} 笔 / {result['days']} 个交易日"
        f"（扫了 {result['symbols']} 只，买不进剔除 {result['dropped']} 笔）",
        "平均收益：" + pct(result["avg"]) + "　胜率："
        + ("—" if result["win_rate"] is None else f"{result['win_rate'] * 100:.1f}%")
        + "　t 值：" + ("—" if result["t"] is None else f"{result['t']:.2f}"),
        f"最好：{pct(result['best'])}　最差：{pct(result['worst'])}",
    ]
    if result["by_year"]:
        lines.append("按年：")
        for row in result["by_year"]:
            lines.append(
                f"  {row['year']}：{row['n']} 笔，平均 {row['avg'] * 100:+.2f}%，"
                f"胜率 {row['win'] * 100:.0f}%"
            )
    if result["samples"] < 30:
        lines.append("⚠️ 样本太少（不到 30 笔），结论只能当参考 —— 多攒些数据再跑一次。")
    elif result["t"] is not None and abs(result["t"]) < 2:
        lines.append("⚠️ t 值不到 2：这条公式的收益和「随机选」很难区分开，别急着上真金白银。")
    if result["hint"]:
        # 用到连板()/涨停天数()：历史越早数据越可能缺 —— 必须写在成绩单里
        lines.append("⚠️ " + result["hint"])
    if result["errors"]:
        lines.append(f"（另有 {len(result['errors'])} 只票算不出来，已跳过："
                     f"{result['errors'][0]}）")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_CONVENTION_KEY",
    "FORMULA_DIR_ENV",
    "FORMULA_DIR_NAME",
    "LIMIT_UP_FUNCTIONS",
    "LIMIT_UP_HINT",
    "MAX_NAME_CHARS",
    "PREVIEW_LIMIT",
    "RETIRED_BUNDLED_FORMULAS",
    "SEED_STATE_NAME",
    "HOT_FIELDS",
    "HOT_WINDOW_DAYS",
    "SNAPSHOT_FIELDS",
    "hot_industry_counts",
    "all_symbols",
    "snapshot_extra",
    "bundled_formula_dir",
    "delete_formula",
    "describe_for_save",
    "enabled_names",
    "formula_dir",
    "formula_files",
    "formula_path",
    "formula_text",
    "latest_trading_day",
    "DYNAINFO_HINT",
    "FINANCE_HINT",
    "limit_up_hint",
    "tdx_compat_notes",
    "name_error",
    "preview_hits",
    "repo_root",
    "run_scorecard",
    "safe_name",
    "save_formula",
]
