"""公式库：公式文件的**存/取**、**试算**、**成绩单**，以及目录定位。

与 `strategy/formula.py` 的分工
-------------------------------
引擎那一份（`strategy/formula.py`，2300 行）只管"把文本变成能算的东西"：
词法、语法、白名单求值、`FormulaError`。它**刻意不碰磁盘、不碰界面、不碰配置**。

本模块是它的"产品外壳"：

* **目录定位** —— `formula_dir()` 同时支持源码运行与打包后的 exe（见下）；
* **保存/删除** —— 文件名安全化 + 引擎认的注释头（`# 名称:` / `# 说明:`）；
* **参与匹配名单** —— `enabled_names()`：把 `config.toml` 里的 `enabled_formulas`
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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from laoa_trader import market_regime, runtime
from laoa_trader.data.engine import HFQ_TABLE
from laoa_trader.log import get_logger
from laoa_trader.strategy import formula as fm

logger = get_logger(__name__)

#: 公式目录名（exe 同级 / 仓库根都是它）
FORMULA_DIR_NAME = "formulas"

#: 用户显式指定公式目录的环境变量（换机器、放共享盘、测试都靠它）
FORMULA_DIR_ENV = "LUWEIK_FORMULAS"

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
    "拿它当股本用的策略结果会偏。"
)

#: 用到 `INBLOCK(...)` 时的提醒（**非阻断**）：它被映射成"热门行业"，
#: 而两者语义其实不同（一个是"属于某板块"、一个是"上过几次热门榜"），必须说清。
INBLOCK_HINT = (
    "注意：本程序把 INBLOCK('板块') 按「热门行业（最近 3 日上榜次数 0~3）」处理，"
    "不做具体板块名匹配 —— 与通达信口径不同；写 INBLOCK('xx')>0 就是"
    "「上过热门榜」，想更严可以写 >1 或 >=2。"
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
#: 【导出匹配结果】写的是全量命中（见 `preview_hits` 的 Returns）。
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

    三种形态都由 `runtime.bundle_dir()` 回答：
    * PyInstaller：spec 的 `DATAS` 把 `formulas/` 解到 `_MEIPASS/formulas`；
    * Nuitka standalone：`--include-data-dir` 落在 **exe 同级的 `formulas/`**；
    * 源码运行：仓库根的 `formulas/`。

    ⚠️ 这里同时也是"Nuitka 换构时最容易漏掉"的一处：写死 `_MEIPASS` 的话，
    Nuitka 版**随包公式一个都找不到**（用户打开策略列表是空的、还没有任何报错）。
    """
    packaged = runtime.bundle_dir() / FORMULA_DIR_NAME
    if packaged.is_dir():
        return packaged
    root = repo_root() / FORMULA_DIR_NAME
    return root if root.is_dir() else None


#: 「这条随包公式播过种了没有」的记录文件（放在**公式目录里**，点开头所以不会被当成公式）。
#:
#: 为什么需要它（2026-09-18 起随包公式要能**补齐**）：新版本多带一条随包公式时，
#: 用户的目录里已经有自己存的公式了 —— 旧规则（"只在空目录复制"）会让那条新公式**永远不出现**；
#: 而直接"缺哪条补哪条"又会让**用户删掉的那条**每次启动都长回来。
#: 只有记下"播过哪些"，才分得清"还没给他"与"他不要"。
SEED_STATE_NAME = ".luweik-seeded.json"

#: 改名前的播种记录文件名（2026-09-30 产品改名时留的一行兼容）。
#: 为什么要认它：这份记录记的是"哪些随包公式已经给过、用户删掉的不许再补"。
#: 换了名字就当"没有记录"的话，所有随包公式会被判成"还没给他"→ **用户删掉的那条会长回来**，
#: 而这恰恰是这份记录存在的全部意义。所以旧名有、新名没有时，先沿用旧的。
LEGACY_SEED_STATE_NAMES: tuple[str, ...] = (".laoa-seeded.json",)

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
    for state_name in (SEED_STATE_NAME, *LEGACY_SEED_STATE_NAMES):
        path = target / state_name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.debug(f"读随包策略播种记录失败（当作没播过）：{exc}")
            return set()
        if not isinstance(data, list):
            return set()
        recorded = {str(item) for item in data}
        if state_name != SEED_STATE_NAME:
            # 老名字读到了：顺手落到新名字上（下一轮就不用再认旧名）
            _write_seed_state(target, recorded)
        return recorded
    return set()


def _write_seed_state(target: Path, seeded: set[str]) -> None:
    """写播种记录。写不进去只记日志：最坏结果是"下次启动再查一遍文件在不在"。"""
    try:
        (target / SEED_STATE_NAME).write_text(
            json.dumps(sorted(seeded), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"写随包策略播种记录失败（不影响使用）：{exc}")


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
            logger.warning(f"读退役策略 {name} 失败（这次不处理）：{exc}")
            continue
        if hashlib.sha256(data).hexdigest() != digest:
            # 用户改过（或本来就是他自己写的同名文件）→ 那是他的东西，绝不删
            logger.info(f"退役策略 {name} 与随包版本不一致，按用户自己的策略保留")
            seeded.add(name)
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"删退役策略 {name} 失败（下次启动再试）：{exc}")
            continue
        seeded.add(name)
        logger.info(f"已清理退役的随包策略：{name}（它对应「连板回踩低吸」，用户要求删掉）")


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
            logger.warning(f"随包策略 {path.name} 复制失败：{exc}")
            continue
        seeded.add(path.name)
        copied += 1
    if copied:
        logger.info(f"已把 {copied} 条随包策略放进 {target}")


def _sync_bundled_formulas(target: Path) -> None:
    """随包公式的**一站式同步**：先退役旧的、再逐条补齐缺的，最后把名单落盘。

    为什么合成一个入口：这两件事共用同一份状态文件（`.luweik-seeded.json`），
    各自读一遍写一遍的话，后写的那次会把前一次刚记下的名字冲掉 ——
    退役名单就会"每轮重新判断"，补齐逻辑也会把退役文件当"还没给过他"补回来。

    源码运行 / 随包目录就是目标目录时**什么都不做**（`source == target`）：
    那种情况下"用户的公式目录"就是随包目录本身（开发时是仓库里的 `formulas/`），
    既没有"要补齐的随包公式"，也不该往仓库里写状态文件、更不该去删仓库里的文件。

    同样"什么都不做"的还有 **Nuitka 编译版**（2026-09-22 起的主构建方式）：
    `--include-data-dir=formulas=formulas` 把随包策略放在 **exe 同级**，
    而用户目录也解析到那里 —— 两者本来就是同一个目录，于是用户直接看到那些策略
    （可改可删），不需要"复制一份给他"。**PyInstaller 版不一样**（随包那份在
    `_MEIPASS` 只读目录里），所以下面这段补齐/退役逻辑必须留着。
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

    1. 环境变量 `LUWEIK_FORMULAS`（换机器/放共享盘/测试用）；
    2. **打包后**：exe 同级目录下的 `formulas/` —— 用户双击 exe 就放在旁边，
       备份、发给别人、用记事本改都最直观；
    3. **源码运行**：仓库根 `laoA/formulas/`（就是仓库里那份，随包分发的也是它）。

    产物形态由 `runtime.is_frozen()` 判（PyInstaller 与 Nuitka 都算），
    不是只看 `sys.frozen` —— Nuitka 上那条判据不一定为真，漏判就会把用户公式
    写进"程序自己的目录"里（下次覆盖安装即丢）。

    随包公式会**逐条补齐**进来：缺哪条补哪条、同名的绝不覆盖、用户删掉的不再补，
    退役的那几条（`RETIRED_BUNDLED_FORMULAS`）还会顺手清掉他没改过的那一份
    （见 `_sync_bundled_formulas`）—— 所以"内置公式"这件事就是"仓库里那个 `formulas/` 目录"。
    """
    override = (os.environ.get(FORMULA_DIR_ENV) or "").strip()
    if override:
        target = Path(override).expanduser()
    elif runtime.is_frozen():
        # 产物形态（PyInstaller / Nuitka）：用户公式放 **exe 同级**（见 runtime.exe_dir）
        target = runtime.exe_dir() / FORMULA_DIR_NAME
    else:
        target = repo_root() / FORMULA_DIR_NAME

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 只读盘 / 权限不足：**不抛异常**（界面照开），保存时再给中文错误
        logger.warning(f"策略目录建不出来：{target}（{exc}）")
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
        return "请先填策略名称（例如：5日线上放量）"
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
        body: 公式正文（多行，最后一行是匹配条件）。
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
    logger.info(f"策略已保存：{path.name}（{len(body)} 字符）")
    return path


def delete_formula(name: str, directory: str | Path | None = None) -> bool:
    """删除公式文件；文件不存在返回 False（**不抛异常**）。"""
    path = formula_path(name, directory)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    logger.info(f"策略已删除：{path.name}")
    return True


# ══════════════════════════════════════════════════════════════════════════
# 参与匹配名单（config.toml 的 enabled_formulas）
# ══════════════════════════════════════════════════════════════════════════


def enabled_names(cfg: Any = None, directory: str | Path | None = None) -> list[str]:
    """本次**真正参与匹配**的公式名（按目录里的顺序）。

    三道收紧，缺一不可：

    1. 名字写在 `config.toml` 里、但 `formulas/` 里**没有这个文件** → 忽略 + 记日志
       （用户删了文件、或改名了；静默失败会让他以为"公式匹配坏了"）；
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
                f"策略匹配：config.toml 里的 enabled_formulas 写着 {name!r}，"
                f"但策略目录里没有这条策略，已忽略"
            )
            continue
        if not spec.ok:
            logger.warning(f"策略匹配：{name} 语法有错（{spec.error_text}），本次不参与")
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
    if "INBLOCK" in used:
        notes.append(INBLOCK_HINT)
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
#: 需要用**实时快照**才能算的公式字段（用到才去取一趟，见 `snapshot_extra`）。
#:
#: 分两类口径：
#:   * 收盘型：`流通市值` / `换手率` —— 日线里没有，快照给的是"今天"的值；
#:   * 盘中型（2026-09-23 加，用户："必须加进去啊"）：`现价` / `现涨幅` / `现量比` / `现换手`
#:     —— 语义是"**现在这一刻**的盘面"，用户在盘中点【运行】/【开始匹配】时按当时的快照算；
#:     非交易时段取不到（条件不成立、一只都不出），而且**没有历史、不能回测**。
SNAPSHOT_FIELDS: tuple[str, ...] = ("流通市值", "换手率", "现价", "现涨幅", "现量比", "现换手")

#: 公式字段名 → 快照字典（`sources.QUOTE_FIELDS`）里的键名。
#: 只有这一张表说了算：名字与键名对不上的症状是"字段永远是 NaN、一只都不出"，
#: 而界面上完全看不出原因（与 `_FuncSpec.uses_fields` 那个坑同一类）。
SNAPSHOT_FIELD_KEYS: dict[str, str] = {
    "流通市值": "circ_mktcap",
    "换手率": "turnover_rate",
    "现价": "last_price",
    "现涨幅": "pct",
    "现量比": "volume_ratio",
    "现换手": "turnover_rate",
}

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
    见 `data/sources.py` 的 `SUPPLEMENT_FIELDS`），而用户点名要拿它们匹配 ——
    所以按 `sources.snapshot_map()`（含"按字段从后面来源补齐"）取一趟，
    再铺成公式认的 `Series.extra`。

    Args:
        quotes: **已经取好的快照**（`_quote_rows` 的出口）。给进来就不再取一趟 ——
            实时口径（`prepare_inputs`）要拿同一份快照既拼 K 线又填扩展字段，
            取两次会看到两个时刻的盘面。`{}` 的语义是"取过了、什么都没有"，
            与 `None`（= 还没取）**不是一回事**。

    Returns:
        `(extra, note)`：`note` 是**取不到时给用户看的一句人话**（拿到了就是空串）——
        取不到就等于条件永远不成立（0 只），不说清用户会以为公式写错了。
    """
    codes = [str(c) for c in dict.fromkeys(symbols) if str(c)]
    if not codes:
        return {}, ""
    if quotes is None:
        quotes = _quote_rows(cfg, codes)
    extra: dict[str, dict[str, float]] = {}
    for symbol, row in (quotes or {}).items():
        values = {name: row.get(SNAPSHOT_FIELD_KEYS[name]) for name in SNAPSHOT_FIELDS}
        values = {k: float(v) for k, v in values.items() if v is not None}
        if values:
            extra[symbol] = values
    if extra:
        return extra, ""
    return {}, ("⚠️ 需要实时快照的字段（流通市值 / 换手率 / 现价 / 现涨幅 / 现量比 / 现换手）"
                "现在都取不到（没有行情快照），用到它们的条件一律不成立 —— 所以可能一只都选不出来。"
                "交易时段再试，或者在「系统设置 → 数据来源」里确认来源可用。"
                "（注意：`现价 / 现涨幅 / 现量比 / 现换手` 是**盘中口径**，只有盘中运行时才有值，"
                "而且没有历史、不能回测。）")


def all_symbols(db_path: str | Path) -> list[str]:
    """库里有行情的全部代码（试算/匹配要拿它去取快照）。读不出来就返回空列表。

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


# ══════════════════════════════════════════════════════════════════════════
# K 线口径：按时间自动在「日 K 线」与「盘中实时」之间切换
#
# 主人 2026-09-23 的原话（这就是本段全部的规格）：
#     "在软件内置规则里设定，开盘时间里运行的匹配，都是实时的，不是开盘时间，采用 K 线。"
# 同一天他又划掉了我加的那个开关："不需要加开关，按照我说的规则来" ——
# 所以这里是**无条件**的规则，没有配置键、界面上也没有勾选框（有用例钉着"关不掉"）。
#
# 也就是说：**这件事不该由用户写进策略、也不该由他决定开关**。他在盘中点【运行】/【开始匹配】，
# 用的就该是此刻的盘面；收盘之后（或周末、或没网）再用库里那根日 K。
# 于是策略里的 `C`、`C/REF(C,1)-1`、`量比()`、`C>MA(C,5)` 一个字都不用改，
# 就自动变成盘中口径 —— 这是"内置规则"与"再教用户写一条盘中策略"的区别。
#
# 三条判据必须同时成立才走实时（任何一条不成立都退回日 K，并**在结果里说清是哪一条**）：
#   1. 现在是**开盘时间**（9:30–11:30 / 13:00–15:00，北京时间）；
#   2. 今天是**交易日**（读库里 `trading_calendar`）；
#   3. 库里**还没有今天那一根**（当天已经跑过日更就直接用库里的）。
# 再加一条"数据条件"：**取得到实时快照**。取不到就退回日 K，并且**必须写明**
# —— 主人要留着这句："那是信息提示不是开关，用户要据此判断结果能不能用"。
# ══════════════════════════════════════════════════════════════════════════


def caliber_now() -> datetime:
    """这次匹配看到的「现在」（**北京时间**的墙上时间）。

    为什么单独一个函数而不是到处 `datetime.now()`：交易时段、交易日、口径文案全都挂在
    它上面，而测试要能钉死"现在是开盘时间"（`monkeypatch.setattr(lib, "caliber_now", …)`）。
    与 `intraday.now_shanghai()` / `clock.now_cn()` 是同一份实现（时区解耦的理由见那边）。
    """
    from laoa_trader import clock

    return clock.now_cn()


def _as_num(value: Any) -> float | None:
    """快照里的一项 → float；认不出（`None`/`-`/空串）返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None      # NaN 也算"没有这个数"


def _quote_rows(cfg: Any, codes: Sequence[str]) -> dict[str, dict]:
    """取一趟实时快照（含"主源给不了的字段往后补齐"），**绝不抛到调用方**。

    单独抽出来是因为实时口径要**一趟快照干两件事**：拼"今天"这根 K 线（价格/量）
    与填 `流通市值 / 换手率 / 现价 / 现涨幅 / 现量比 / 现换手`（扩展字段）。
    早先这两件事各取一次的话，同一轮匹配会看到两个时刻的盘面 —— 那是最没法解释的
    一类不一致（涨幅与 K 线对不上，用户只能怀疑程序坏了）。
    """
    from laoa_trader.data import sources

    wanted = [str(c) for c in dict.fromkeys(codes) if str(c)]
    if not wanted:
        return {}
    try:
        quotes = sources.snapshot_map(cfg, wanted)
    except Exception as exc:  # noqa: BLE001 - 取不到就是"没有实时数据"
        logger.info(f"取快照失败（市值/换手/盘中字段用不了）：{exc}")
        return {}
    try:
        # 同花顺的快照不返回量比/市值/换手 → 按字段从后面的来源（默认免 Key 公开源）补
        sources.supplement_map(cfg, quotes, wanted)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"补齐快照字段失败（市值/换手可能不全）：{exc}")
    return quotes


def live_bars_from_quotes(quotes: dict[str, dict] | None) -> dict[str, fm.LiveBar]:
    """快照 → `{代码: LiveBar}`（拼"今天"那根 K 线的原料）。

    只收**同时有现价与昨收**的票：缺一个就拼不出这根 K 线（涨幅的基准就是昨收），
    而**编一个**昨收出来会让"今天的涨幅"凭空多出一截。缺的票不接今天这根，
    照旧按日线算 —— 这是"少算一只"与"算错一只"之间的取舍，选少算。
    """
    out: dict[str, fm.LiveBar] = {}
    for symbol, row in (quotes or {}).items():
        if not isinstance(row, dict):
            continue
        prev = _as_num(row.get("prev_close"))
        last = _as_num(row.get("last_price"))
        if not prev or prev <= 0 or not last or last <= 0:
            continue
        out[str(symbol)] = fm.LiveBar(
            prev_close=prev,
            close=last,
            open=_as_num(row.get("open")),
            high=_as_num(row.get("high")),
            low=_as_num(row.get("low")),
            volume=_as_num(row.get("volume")),
            turnover=_as_num(row.get("turnover")),
        )
    return out


@dataclass
class Prepared:
    """一次匹配要交给公式引擎的**全部输入**（含"用哪套 K 线口径"的结论）。

    为什么打包成一个对象而不是让每个调用方自己拼：试算（`preview_hits`）与建池
    （`formula_group.run_enabled_formulas`）必须**完全同口径** —— 要是各写一份，
    迟早出现"点【运行】选出 3 只、点【开始匹配】选出 1 只"这种没法解释的差异。
    """
    #: 库里最新的行情日（K 线口径的"今天"）
    kline_day: str | None = None
    #: 实时口径的"今天"；**None = 本次没用实时**（用它当"要不要接一根"的开关）
    today: str | None = None
    #: `{代码: LiveBar}`：实时口径下要接到日线后面的那一根
    live_bars: dict[str, fm.LiveBar] = field(default_factory=dict)
    #: `{代码: {扩展字段: 值}}`（市值/换手/现价/现涨幅/现量比/现换手）
    extra: dict[str, dict[str, float]] = field(default_factory=dict)
    #: `{行业名: 上榜次数}`（热门行业）
    hot: dict[str, int] = field(default_factory=dict)
    #: **给用户看的一句口径说明**（界面上直接显示；永远非空）
    caliber: str = ""
    #: 影响"选不选得出来"的提示（典型：快照取不到 ⇒ 用到那些字段的条件一律不成立）。
    #: 建池那条路把它当**错误**（`FormulaRun.errors`）：它的意思是"这次的条件算不出来"，
    #: 而不是"程序跑得好好的" —— 也正因为如此，定时任务会因此安排一次补跑。
    notes: list[str] = field(default_factory=list)
    #: **告知**（不是错误）：典型是"盘中想用实时数据，但取不到快照 → 已退回日 K 线"。
    #: 为什么必须与 `notes` 分开：建池的"成功/失败"判据是"errors 是否为空"
    #: （`scheduler.Scheduler._report_succeeded`）—— 把一句"退回了日 K"塞进 errors，
    #: 会让一次**正常完成**的匹配被判成失败（表现：明明选出了票、推送也发了，
    #: 状态却写"未成功"，还要在补跑时间再跑一遍）。
    warnings: list[str] = field(default_factory=list)

    @property
    def display_day(self) -> str | None:
        """对外显示的"行情日"：实时口径下就是今天（否则用户会以为选的是昨天的盘）。"""
        return self.today or self.kline_day


def _live_decision(cfg: Any, db_path: str | Path, kline_day: str | None,
                   today: str, moment: datetime) -> tuple[bool, str]:
    """要不要走盘中实时 → `(走不走, 不走的理由)`（理由进那句口径说明）。

    ⚠️ 判据的顺序要紧：**先看库有没有数据**，再去看交易日历。`is_trading_day()` 会打开
    本地库（`storage.connect`），而 sqlite 对**不存在的文件**是"连上就建一个空库" ——
    于是"库还没下载"这件事会被后面 `load_series` 那句"本地数据库不存在"悄悄变成
    "库是空的、一只票都读不到"（错误信息对不上，还会凭空多出一个空 db 文件）。
    """
    if cfg is None:
        return False, "没有配置（拿不到数据来源）"
    # ⚠️ 这里**没有**"用不用实时"的开关可查，而且不许加（主人 2026-09-23：
    # "不需要加开关，按照我说的规则来"）：判据只有时间、交易日、库里的数据这三条。
    # 延迟导入：`intraday` 会拉起数据层一大片（hithink/engine/storage），
    # 而这个判据只有"真要跑匹配"时才用到；模块级导入会让 `import formulas`
    # 顺带把网络栈与数据库层拉进来（成绩单/策略列表页用不到它们）。
    from laoa_trader import intraday

    if not intraday.in_session(moment):
        return False, f"现在 {moment:%H:%M} 不是开盘时间"
    if kline_day is None:
        # 库不存在 / 库是空的：连"最近行情日"都没有，谈不上盘中口径
        return False, "本地还没有日线数据"
    if kline_day >= today:
        return False, f"库里已经有 {today} 这根 K 线了"
    if not intraday.is_trading_day(str(db_path), today):
        # 休市日（周末/节假日）在 9:30–15:00 之间也是"不在交易时段"的 —— 只看钟点会把
        # 上一个交易日的快照当成"今天的盘"，选出一批 pct=0 的假信号
        return False, "今天不在交易日历里（休市）"
    return True, ""


def prepare_inputs(
    cfg: Any,
    db_path: str | Path,
    formulas: Sequence[fm.Formula] | None = None,
    symbols: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
) -> Prepared:
    """按"现在是不是开盘时间"准备这次匹配的全部输入（K 线口径 + 扩展字段）。

    Args:
        cfg: 配置（None = 没有配置：不联网、不取快照，纯日 K 口径）。
        db_path: 本地库。
        formulas: 这一轮要跑的公式。**它们的字段决定要不要取快照**：一条都不用到
            快照字段（市值/换手/现价/现涨幅/现量比/现换手）时，只有"盘中实时口径"
            会去取那一趟；否则**一个请求都不发**（与"没这个功能"完全一样）。
        symbols: 只关心这些代码（None = 库里全部）。
        now: 注入的"现在"（测试用；None = 真时间，见 `caliber_now`）。

    Returns:
        `Prepared`（口径结论 + 引擎要的全部输入）。
    """
    moment = caliber_now() if now is None else now
    today = moment.strftime("%Y-%m-%d")
    kline_day = latest_trading_day(db_path)
    want_snapshot = any(set(f.fields) & set(SNAPSHOT_FIELDS) for f in (formulas or ()))
    live, why = _live_decision(cfg, db_path, kline_day, today, moment)

    prepared = Prepared(kline_day=kline_day)
    quotes: dict[str, dict] = {}
    targets: list[str] = []
    if cfg is not None and (live or want_snapshot):
        # 取快照必须先知道"要哪些票"：库里给了代码就用它，没给就是全市场
        # （`load_series` 是逐只 yield 的生成器，拿不到代码表，见 `all_symbols`）
        targets = list(symbols) if symbols is not None else all_symbols(db_path)
        quotes = _quote_rows(cfg, targets)

    if live:
        prepared.live_bars = live_bars_from_quotes(quotes)
        if not prepared.live_bars:
            # 想实时却拿不到数据：退回日 K，并且**必须说清**（不然用户看到的是
            # "条件成立却一只都不出"，只能怀疑策略写错了）。
            # 走 `warnings` 而不是 `notes`：这是一句**告知**，这次匹配本身是正常完成的
            # （建池那边把 `notes` 当错误，会让一次成功的匹配被判成"未成功"并安排补跑）。
            live, why = False, "盘中取不到实时快照"
            prepared.warnings.append(
                "⚠️ 现在是开盘时间，本该按实时数据匹配，但取不到实时快照 → "
                "本次已退回日 K 线口径（收盘数据）。用到「现价 / 现涨幅 / 现量比 / 现换手」"
                "的条件不会成立；市值/换手也一样取不到。"
                "可稍后重试，或在「系统设置 → 数据来源」里确认来源与 Key 可用。"
            )
        else:
            prepared.today = today

    if want_snapshot and cfg is not None:
        # 快照已经取过就**直接复用**：同一轮里再取一次会看到另一个时刻的盘面，
        # 于是 K 线里的涨幅与 `现涨幅` 对不上（这类不一致用户只能怀疑程序坏了）。
        # 传 `quotes` 而不是 `quotes or None`：空字典的语义是"取过了、什么都没有"，
        # 换成 None 会让它再取一趟（正是要避免的那件事）。
        extra, note = snapshot_extra(cfg, list(targets), quotes=quotes)
        prepared.extra = extra
        if note:
            prepared.notes.append(note)

    prepared.hot = hot_industry_counts(db_path) if formulas and any(
        set(f.fields) & set(HOT_FIELDS) for f in formulas
    ) else {}

    if prepared.today:
        # ⚠️ 这句是**纯文本**（进 QLabel 的提示区、进匹配完成的结论），不是 markdown ——
        # 写 `**加粗**` 的话用户看到的就是两个星号（用户明确说过不喜欢这种星号）。
        prepared.caliber = (f"📊 本次口径：盘中实时（{moment:%H:%M}，"
                            f"用现价拼出今天 {today} 这根 K 线）")
    else:
        prepared.caliber = (f"📊 本次口径：日 K 线（最后一根 {kline_day or '无'}；"
                            f"{why}）")
    return prepared


def preview_hits(
    formula: fm.Formula,
    db_path: str | Path,
    *,
    limit: int = PREVIEW_LIMIT,
    start: str | None = None,
    symbols: Sequence[str] | None = None,
    cfg: Any = None,
    now: datetime | None = None,
) -> dict:
    """在**当前本地库**上跑一遍公式，返回最新行情日的命中清单（只读）。

    口径与内置策略一致：只看**每只票最后一根 K 线**，命中即"当日收盘后选中"。
    最后一根 K 线早于全市场最新行情日的票会被跳过（停牌/退市：它的"最后一根"
    是旧的，拿它当"今天选中"是错的）。

    **K 线口径按时间自动切**（用户 2026-09-23 定的内置规则，见 `prepare_inputs`）：
    开盘时间（9:30–11:30 / 13:00–15:00 的交易日）里跑 → 用实时快照拼出"今天"这一根，
    `C` / `C/REF(C,1)-1` / `量比()` 于是都是**盘中口径**；不在开盘时间 → 用库里的日 K。
    取不到实时快照时退回日 K，并在 `notes` 里说清（`caliber` 那一行也写明是哪套口径）。

    联网与否：公式里用到 `流通市值` / `换手率` 等快照字段、或者此刻正走实时口径时，
    才取**一趟**实时快照（这两个数日线里没有，见 `snapshot_extra`）；
    收盘后不用这些字段的公式**一个请求都不发**。

    **弱市抬门槛**（`market_regime`，2026-10-08）：大盘弱势时只留"近 N 日跑赢全市场"
    的票（门槛 `market_regime_rs_min_pct`，窗口 `market_regime_window`），
    挡掉多少只、为什么挡，写在 `notes` 里（界面原样显示）。
    这条判断与建池那条路**共用同一个 `market_regime.apply_gate()`**；
    强市/中性/未知时它一只票都不挡（`notes` 里也不会多出一句）。

    Returns:
        {"date": 行情日（实时口径下就是今天）, "count": 命中数,
         "hits": [{"symbol","name"}...], "shown": 展示数,
         "scanned": 扫过的票数, "skipped": 数据不足的票数,
         "errors": [中文错误...], "notes": [全局提示...],
         "caliber": 这次用的 K 线口径（一句中文，界面直接显示）}

        `hits` 是**全量**命中清单（按代码排序、不截断），`limit` 只决定 `shown`：
        界面按 `shown` 截断**显示**（提示区一行放不下 60 只票），而【导出匹配结果】
        要写**完整**的一份 —— 给用户的文件里少几只，是最难被发现的那种错。
    """
    hits: list[dict] = []
    errors: list[str] = []
    #: **全局提示**（与"某只票算不出来"分开）：典型是"市值/换手现在取不到"这类
    #: 影响整次试算的话。为什么必须分开：界面把 `errors` 渲染成
    #: "（N 只票算不出来，已跳过：…）"—— 把一句全局提示混进去，用户看到的是
    #: "1 只票算不出来"，票数是假的、原因也被张冠李戴。
    notes: list[str] = []
    scanned = 0
    skipped = 0
    # 输入（K 线口径 + 扩展字段）全部由 `prepare_inputs` 一份逻辑给 —— 建池那条路
    # （`formula_group.run_enabled_formulas`）用的是**同一个函数**，所以
    # 【运行】与【开始匹配】不可能出现两套口径。
    prepared = prepare_inputs(cfg, db_path, [formula], symbols, now=now)
    day = prepared.kline_day
    # 「连续 N 日确认」与建池那条路**同一份口径**（`formula_group.confirm_days_of`）：
    # 试算按 0 算、匹配按 2 算的话，用户会看到"【运行】选出 5 只、【开始匹配】只有 2 只"，
    # 而且没有任何办法解释。确认生效时这里也要说一句，免得用户以为公式坏了。
    # 延迟导入：`formula_group` 在模块级 import 本模块（它是公式库的外壳），
    # 这里再顶层 import 会成环。
    from laoa_trader.strategy import formula_group as fg

    confirm_days = fg.confirm_days_of(cfg)
    if confirm_days > 0:
        notes.append(
            f"已开启「连续确认」：只有在最近 {confirm_days + 1} 个交易日都命中的票才算选中"
            "（配置键 signal_confirm_days；设 0 可关掉）"
        )
    # 试算这里两类提示都并进 `notes`（界面对它们一视同仁：都渲染成单独一行）——
    # 「建池」那条路才需要分开（那边 errors 是"成功/失败"的判据，见 `Prepared`）
    notes.extend(prepared.notes)
    notes.extend(prepared.warnings)
    for series in fm.load_series(
        db_path, symbols=symbols, start=start, extra=prepared.extra,
        hot_industries=prepared.hot,
        today=prepared.today, live_bars=prepared.live_bars, kline_day=prepared.kline_day,
    ):
        # 数据不够长：公式的滚动窗口一定全是缺值 ⇒ 不可能出信号，直接跳过（省时间）
        if len(series.date) < formula.min_history:
            skipped += 1
            continue
        # 最后一根必须是"全市场最新行情日"；实时口径下就是**今天那一根**
        # （没接上今天那根的票照旧按日线判，见 `load_series`）
        if day is not None and series.date[-1] not in (day, prepared.today):
            skipped += 1
            continue
        scanned += 1
        try:
            mask = formula.eval(series)
        except (fm.FormulaError, fm.FormulaDataError) as exc:
            # 一只票算不出来不该让整次试算失败（与匹配链路的隔离口径一致）
            # 标的写法统一成**半角** `名称(代码)`（见 docs/开发文档.md）
            errors.append(f"{series.name}({series.symbol})：{exc}")
            continue
        if fm.confirmed(mask, confirm_days):
            hits.append({"symbol": series.symbol, "name": series.name})
    hits.sort(key=lambda hit: hit["symbol"])
    # ── 弱市抬门槛（`market_regime`）──
    # 大盘弱势时只留"近 N 日跑赢全市场"的票。**与建池那条路调的是同一个函数**
    # （`formula_group.run_enabled_formulas`），所以【运行】与【开始匹配】
    # 在任何大盘状态下都会给出同一批票 —— 两处各写一套筛选，就会出现
    # "试算说 5 只、匹配只有 2 只"这种用户无法解释的差异。
    # 强市/中性/未知、或用户关掉 `market_regime_gate` 时：`kept` 就是原样的全部
    # （一条票都不许被挡掉），`note` 是空串（界面上一个字都不多）。
    gate = market_regime.apply_gate(hits, db_path=db_path, day=day, cfg=cfg)
    if gate.applied and gate.dropped:
        logger.info(f"弱市门槛：{len(hits)} 只 → {len(gate.kept)} 只")
    if gate.note:
        # **追加在最后**：`notes[0]` 历来是"本次口径"那句话（界面/测试按位置读它），
        # 弱市门槛的说明插到前面会把那句口径挤到第二行。
        # 另外它进的是 `notes`（告知），**不是 `errors`** —— `errors` 的意思是
        # "某只票算不出来"，把一句筛选说明混进去，用户看到的票数与原因都会被张冠李戴。
        notes.append(gate.note)
    hits = gate.kept
    return {
        # 实时口径下"行情日"就是**今天**：这一轮选的正是此刻的盘面，
        # 报成"最近交易日 2026-09-22"会让用户以为程序在拿昨天的收盘数据匹配
        "date": prepared.display_day,
        "count": len(hits),
        # 全量（界面自己按 `shown` 截断显示，导出要全量）
        "hits": hits,
        "shown": min(len(hits), limit),
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "notes": notes,
        "caliber": prepared.caliber,
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

    # ⚠️ 成绩单**永远只用库里的日 K**（不带 `today`/`live_bars`）：它算的是"这条公式
    # 在历史上行不行"。塞一根盘中的"今天"进去，等于拿一个还没有收盘结果的样本去算胜率
    # （而且同一份历史每次点都得到不同的数）。实时口径只属于【运行】/【开始匹配】那条路。
    series_iter = fm.load_series(db_path, symbols=symbols, start=start)
    # 进度需要"总数"，而 `load_series` 是生成器（不知道总数）—— 先按库里的代码数
    # 报总步数：比"进度条永远停在 0%"好得多，且不额外读行情。
    total = _symbol_count(db_path, symbols)
    for series in series_iter:
        scanned += 1
        if progress_cb is not None and (scanned % 25 == 0 or scanned == total):
            progress_cb("策略成绩单", min(scanned, total or scanned), total or scanned)
        if len(series.date) < formula.min_history:
            continue
        try:
            mask = formula.eval(series)
        except (fm.FormulaError, fm.FormulaDataError) as exc:
            # 单只票的缺失值/坏数据只记一笔，不影响其它票（也不让成绩单整体失败）
            # 标的写法统一成**半角** `名称(代码)`（见 docs/开发文档.md）
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
    # ⚠️ 用"有限的正数"判，不要用 `not entry_px`：NaN 是真值，`not nan` 是 False，
    # 挡不住它 —— 库里某一行行情价是 NULL 时（源里缺那一列），这一笔就会算成 NaN，
    # 一路传到最后让成绩单抛异常或印出 `+nan%`（2026-10-08 实测）。
    # 与 `research/scorecard.py` 用**同一个**判断（`_finite_price`），免得只有一边修。
    if not sc._finite_price(entry_px) or not sc._finite_price(exit_px):
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
        f"📊 策略成绩单：{result['formula']}",
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
        lines.append("⚠️ t 值不到 2：这条策略的收益和「随机选」很难区分开，别急着上真金白银。")
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
    "SNAPSHOT_FIELD_KEYS",
    "Prepared",
    "caliber_now",
    "hot_industry_counts",
    "live_bars_from_quotes",
    "prepare_inputs",
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
    "INBLOCK_HINT",
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
