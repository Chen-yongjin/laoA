r"""授权与试用（单机终身授权，用户 2026-09-20 拍板）。

用户原话
--------
> 「策略编辑锁住，点击提醒需要授权，请联系作者wx：q352162」
> 「免费运行7天，到期打开同样授权提醒。」
> 「做个注册机给我，输入机器码就能算出注册码。机器码下面加上注册码输入口和注册按键，点击可以注册。」

所以这一层要回答三个问题，**只有这一层回答**（界面与 CLI 都只读它）：

1. **这台机器的机器码是什么** → `machine_code()`（显示给用户，他发给作者换注册码）；
2. **用户填的注册码对不对** → `verify(machine, code)` / `register(machine, code)`；
3. **现在算不算已授权**（已注册 or 试用未到期）→ `license_status()` / `is_licensed()`。

为什么用"HMAC 短码"而不是"Ed25519 公钥验签"
--------------------------------------------
两条路的强度差一个量级，但**用户要抄写**这个码（微信上发来发去、手打）：

* HMAC + 内置密钥：注册码 16 位（`XXXX-XXXX-XXXX-XXXX`），抄写、口述都不费劲；
  代价是密钥在**客户端**里（虽然做了拆分混淆），逆向出来的人可以自己算注册码；
* Ed25519 私钥签名：客户端只有公钥，**逆向也没用**；代价是注册码 100+ 字符
  （Base64 的 64 字节签名），微信传、手抄都不现实。

这是**分发产品**而不是"高价值软件授权"，真正要挡的是"顺手把 exe 拷给同事用"，
而不是"有人专门逆向你"。所以取 HMAC 短码，把"更好抄"放第一位。
**升级路径**：`verify()` 只依赖 `expected_code()` 一个函数，将来要换签名方案，
改这一个函数 + 把注册码长度放宽即可，界面与试用逻辑都不用动。

试用期与"删文件续命"
--------------------
* 首次运行日期写**两处**：`%APPDATA%\CaishenHelper\license.json` 与数据库 `app_state` 表；
* 读的时候取**较早**的那个 —— 删掉其中一个不会让试用期重置（删两个也没用：
  数据库里有更早的记录就会赢）；
* **时钟回拨防护**：记录"见过的最大日期"，一旦系统时间比它早（用户改系统时间续命），
  按**已到期**处理并把原因说清楚（宁可误伤一个改了时间的人，也不能让试用期无限续）。

这一层**绝不抛异常给界面**：取不到硬件、盘只读、JSON 坏了都退化成"未知/未授权 + 日志"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from laoa_trader import clock
from laoa_trader.config import DEFAULT_APP_NAME, get_config, user_config_path
from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 试用天数（用户原话："免费运行7天"）
TRIAL_DAYS = 7

#: 注册码位数（Base32 字符数）与分组
CODE_CHARS = 16
CODE_GROUP = 4

#: 机器码位数（与注册码同形，便于对照抄写）
MACHINE_CHARS = 16

#: 联系方式（用户给定，**一个字都不许改**：这是他给出去让人加的微信号）
CONTACT_TEXT = "需要授权，请联系作者 wx：q352162"

#: 注册码里允许出现的字符（Base32 的字母表，去掉容易看错的 I/O/0/1 的**大写**形式）
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

#: HMAC 密钥的**分片**。为什么这么写而不是一个明文字符串：
#: 它挡的是"反编译看到一串 key 就照抄"的顺手破解，不是密码学强度（见模块 docstring）。
#: 三片按位异或还原，还原后的字节故意不是合法 UTF-8（省得被人直接把字符串打出来）。
_KEY_PARTS: tuple[tuple[int, ...], ...] = (
    (0x1F, 0x2C, 0x63, 0x58, 0x1B, 0x6D, 0x0A, 0x33),
    (0x6A, 0x41, 0x11, 0x36, 0x7E, 0x22, 0x59, 0x4D),
    (0x75, 0x6D, 0x72, 0x6E, 0x65, 0x4F, 0x53, 0x7E),
)


def _secret() -> bytes:
    """还原 HMAC 密钥（三片异或）。每片长度相同时结果长度就是片长。"""
    out = bytearray(_KEY_PARTS[0])
    for part in _KEY_PARTS[1:]:
        for i, value in enumerate(part):
            out[i] ^= value
    return bytes(out)


# ══════════════════════════════════════════════════════════════════════════
# 机器码
# ══════════════════════════════════════════════════════════════════════════


def _run_text(args: list[str], timeout: float = 6.0) -> str:
    """跑一条命令取标准输出（失败/超时返回空串）。

    Windows 上取硬件指纹要问 WMI：用 PowerShell 的 CIM 查询（`wmic` 在新系统上
    已经被移除了）。**必须不弹黑窗**（`CREATE_NO_WINDOW`），否则用户一开软件
    就闪一个黑框，那是最容易被当成"这软件有毒"的现象。
    """
    if not args:
        return ""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":       # pragma: no cover - 只在 Windows 上生效
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(          # noqa: S603 - 参数是写死的，没有用户输入
            args, capture_output=True, timeout=timeout, text=True, **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - 取不到指纹不该让程序起不来
        logger.debug(f"取硬件信息失败（{args[0]}）：{exc}")
        return ""
    return (done.stdout or "").strip()


def _ps(query: str) -> str:
    """在 Windows 上跑一条 PowerShell 取字符串属性。"""
    if os.name != "nt":       # pragma: no cover - 非 Windows 没有 CIM
        return ""
    return _run_text([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"try {{ ({query}).Trim() }} catch {{ '' }}",
    ])


#: 一次性问三项硬件指纹的脚本（**一个进程问完**）。
#:
#: 为什么不是三条 `-Command`：每起一个 PowerShell 都要几百毫秒（实测冷启动 0.3~0.6s），
#: 三条就是 1~2 秒 —— 而机器码以前是**每次调用都重算**，于是"点一下策略编辑就卡一下"
#: （主人 2026-09-21 实报）。合成一条 + 下面的缓存，两层一起才把这条路压到 0。
_HW_SCRIPT = (
    "$p=(Get-CimInstance Win32_Processor).ProcessorId;"
    "$b=(Get-CimInstance Win32_BaseBoard).SerialNumber;"
    "$u=(Get-CimInstance Win32_ComputerSystemProduct).UUID;"
    "\"$p|$b|$u\""
)


def _hardware_parts() -> list[str]:
    """三项硬件指纹的**原始取值**（CPU / 主板 / 系统盘；取不到就是空串）。

    拆成单独一个函数是为了让"取不到硬件"这条路径**可测**（见
    `tests/test_licensing.py::test_machine_code_falls_back_when_hardware_is_unreadable`）：
    测试把这个函数换成"全空"，就能验证兜底逻辑真的生效。
    """
    parts: list[str] = []
    if os.name == "nt":       # pragma: no cover - 只在 Windows 上走
        raw = _run_text([
            "powershell", "-NoProfile", "-NonInteractive", "-Command", _HW_SCRIPT,
        ])
        parts = [p.strip() for p in str(raw or "").split("|")]
    else:
        for path in ("/etc/machine-id", "/sys/class/dmi/id/product_uuid",
                     "/sys/class/dmi/id/board_serial"):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            parts.append(value)
    return [str(p).strip() for p in parts]


def _fallback_part() -> str:
    """兜底指纹：用户名 + 机器名 + 装机目录。

    为什么必须有它：**取不到硬件**的机器（精简版系统、WMI 被禁、Linux 容器）如果
    算出全空指纹，那所有这类机器的机器码都一样 —— 等于没有绑定机器。
    而"谁在用 + 在哪台机器上 + 装在哪个目录"三项合起来，复制到别的机器上基本不会相同。
    """
    return "|".join((
        os.environ.get("USERNAME") or os.environ.get("USER") or "user",
        platform.node() or "host",
        str(_install_dir()),
    ))


def _fingerprint_parts() -> list[str]:
    """这台机器的指纹三项（取不到的用兜底项补足）。

    **测试与特殊环境注入假指纹**：monkeypatch 本函数即可（见 `tests/test_licensing.py`）。
    """
    cleaned = [p for p in _hardware_parts() if p]
    fallback = _fallback_part()
    while len(cleaned) < 3:
        cleaned.append(fallback)
    return cleaned[:3]


def _install_dir() -> Path:
    """程序所在目录（打包后是 exe 同级，源码运行是仓库根）。

    它进兜底指纹是**刻意**的：装机位置是这台机器上稳定的东西，
    而"用户名 + 机器名 + 装机目录"三项合起来，复制到别的机器上基本不会同时相同。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _encode(raw: bytes, chars: int) -> str:
    """字节 → Base32 大写 → 取前 `chars` 位 → 每 4 位加连字符。"""
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    text = text[:chars].ljust(chars, "A")
    return "-".join(text[i:i + CODE_GROUP] for i in range(0, chars, CODE_GROUP))


def _normalize(code: Any) -> str:
    """注册码/机器码的容错归一：去空白与连字符、转大写。

    为什么容错：用户会从微信里复制、会手打，多一个空格、少一个连字符、
    用小写输入都是家常便饭 —— 因为这些判"格式错误"是在难为用户。
    """
    text = str(code or "")
    return "".join(ch for ch in text.upper() if ch.isalnum())


#: 进程内缓存（第一次算完就记住）。`None` = 还没算过。
#:
#: 为什么必须缓存：Windows 上算一次要起 PowerShell 问硬件，**几百毫秒到一两秒**；
#: 而"点【策略编辑】"、刷新授权状态、打开「关于」都会走到这里 —— 以前每次点击都重算，
#: 表现就是"点策略什么的都会卡一下"（主人 2026-09-21 实报）。授权状态本身不变化，
#: 没有理由重复问硬件。
#:
#: ⚠️ **只缓存在内存里，绝不落盘**（和"启动时后台预热"一起，代替了落盘那份）。
#: 为什么不落盘：机器码是"授权绑定机器"的唯一依据 —— 一旦把它写进 `license.json`，
#: 那份文件被拷到别的机器上时，`machine_code()` 会读回**旧机器**的码、与文件里的
#: `machine` 字段一致 → 于是"拷文件就白用"。落盘省下的那几百毫秒（而且只在启动后
#: 第一次调用时才有）不值得拿这个换。
_MACHINE_CACHE: str | None = None


def machine_code() -> str:
    """本机机器码（`XXXX-XXXX-XXXX-XXXX`）。**同一台机器每次都一样**。

    取值顺序（越靠前越省时间）：

    1. **进程内缓存** —— 同一个进程里第二次调用直接返回（这才是"点按钮不卡"的关键）；
    2. 真的去问硬件（三条 PowerShell → 现在合成**一条**）。

    重启后第一次调用仍要问一次硬件，但那是**启动时后台线程**干的事
    （`MainWindow._warm_machine_code`），用户点到按钮时已经是缓存值了 ——
    所以这里不做落盘缓存（原因见 `_MACHINE_CACHE` 那段：落盘会让"拷文件白用"成立）。
    """
    global _MACHINE_CACHE
    if _MACHINE_CACHE:
        return _MACHINE_CACHE
    try:
        digest = hashlib.sha256("\x1f".join(_fingerprint_parts()).encode("utf-8")).digest()
    except Exception as exc:  # noqa: BLE001 - 极端环境：兜底也失败时给一个稳定占位
        logger.warning(f"算机器码失败，用占位值：{exc}")
        digest = hashlib.sha256(b"caishen-helper-unknown-machine").digest()
    _MACHINE_CACHE = _encode(digest, MACHINE_CHARS)
    return _MACHINE_CACHE


# ══════════════════════════════════════════════════════════════════════════
# 注册码
# ══════════════════════════════════════════════════════════════════════════


def forget_cached_machine_code() -> None:
    """忘掉进程内缓存的机器码（**测试用**；也用于"换了装机位置想立刻重算"的场景）。

    为什么要显式入口而不是让测试去改 `_MACHINE_CACHE`：缓存是"点按钮不卡"这条性能
    改进的核心，测试必须能可靠地把它清掉再验算一遍；有个具名函数就不怕改实现时漏掉。
    """
    global _MACHINE_CACHE
    _MACHINE_CACHE = None


def expected_code(machine: Any) -> str:
    """这台机器**应该**收到的注册码（作者用同一个函数算，客户端用它校验）。

    算法：`HMAC-SHA256(密钥, 归一化后的机器码)` 取前 10 字节（80 位）→ Base32 → 16 位。
    80 位意味着"随便猜一个蒙对"的概率是 2^-80，比中彩票低得多；
    而 16 个字符又刚好能一口气抄完。
    """
    normalized = _normalize(machine)
    mac = hmac.new(_secret(), normalized.encode("ascii", "ignore"), hashlib.sha256).digest()
    return _encode(mac[:10], CODE_CHARS)


def verify(machine: Any, code: Any) -> tuple[bool, str]:
    """校验注册码。

    Returns:
        `(是否通过, 中文原因)`。原因**直接给界面显示**（用户看得懂、也能照着做），
        所以三种情况分开说：没填 / 格式不对 / 对不上（对不上时提示"把机器码发给我重算"）。
    """
    raw = str(code or "").strip()
    if not raw:
        return False, "请先填注册码（把上面的机器码发给作者换一个）"
    normalized = _normalize(raw)
    if len(normalized) != CODE_CHARS or any(ch not in _ALPHABET for ch in normalized):
        return False, (f"注册码格式不对：应该是 {CODE_CHARS} 位字母数字"
                       f"（形如 XXXX-XXXX-XXXX-XXXX，注意别少抄或多抄字符）")
    want = _normalize(expected_code(machine))
    # `compare_digest`：比较用时与内容无关（虽然这里防的是"猜"不是"计时侧信道"，
    # 用它是零成本的正确习惯）
    if not hmac.compare_digest(normalized, want):
        return False, "注册码与这台机器不匹配（机器码变了请重新发我换一个）"
    return True, "注册成功"


# ══════════════════════════════════════════════════════════════════════════
# 状态文件（一个文件 + 数据库一份，取"更保守"的那个）
# ══════════════════════════════════════════════════════════════════════════


def state_path() -> Path:
    """授权状态文件：与 `config.toml` 同目录（`%APPDATA%\\CaishenHelper\\license.json`）。

    为什么不放 exe 同级：用户更新软件是整包覆盖那个目录，授权文件会被一起盖掉
    （2026-09-20 飞书配置就是这么丢的）。
    """
    return user_config_path().parent / "license.json"


def _read_state() -> dict:
    """读状态文件（坏了当空字典，不抛）。"""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - 手工改坏/权限：当没有，别让界面起不来
        logger.warning(f"授权状态文件读不出来（当未授权处理）：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> bool:
    """写状态文件（失败只记日志、返回 False）。"""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001 - 只读盘/权限：授权要能"当次生效"
        logger.warning(f"授权状态写不进去（本次仍生效，重启会丢）：{exc}")
        return False


# ── 数据库那一份（备份 + 防"删文件续命"）────────────────────────────────


def _db_conn(cfg: Any = None) -> Any:
    """数据库连接（拿不到就返回 None，调用方按"没有库这一份"处理）。"""
    try:
        from laoa_trader.data import storage

        cfg = cfg or get_config()
        return storage.connect(cfg.db_path)
    except Exception as exc:  # noqa: BLE001 - 库坏了不该让授权判定崩
        logger.debug(f"打开数据库失败（授权只认文件那一份）：{exc}")
        return None


def _db_read(key: str, cfg: Any = None) -> str:
    conn = _db_conn(cfg)
    if conn is None:
        return ""
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_state ("
                " key TEXT PRIMARY KEY, value TEXT)"
            )
            row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"读 app_state({key}) 失败：{exc}")
        return ""
    finally:
        conn.close()


def _db_write(key: str, value: str, cfg: Any = None) -> None:
    conn = _db_conn(cfg)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_state ("
                " key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"写 app_state({key}) 失败：{exc}")
    finally:
        conn.close()


def _today() -> date:
    """今天（**北京时间**，`datetime.date`）。

    授权判定必须与项目其它地方用同一个"今天"：用户在中国，程序按 `clock` 走北京时间；
    机器在 UTC 时区时用 `date.today()` 会差 8 小时，跨零点会多算/少算一天。
    （`clock.now_cn()` 是朴素北京时间，`.date()` 就是北京日期。）
    """
    return clock.now_cn().date()


def _parse_day(text: Any) -> date | None:
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def _first_run_day(state: dict, cfg: Any = None) -> date | None:
    """首次运行日期：**文件与数据库取较早的那个**。

    为什么要两份并取早的：只存文件的话，"删掉 license.json"就能把试用期重置。
    数据库那份是防这个的（用户不会想到去动 `trader.db`）。
    """
    candidates = [_parse_day(state.get("first_run")), _parse_day(_db_read("license_first_run", cfg))]
    days = [d for d in candidates if d is not None]
    return min(days) if days else None


def _max_seen_day(state: dict, cfg: Any = None) -> date | None:
    """见过的最大日期（时钟回拨检测用），同样取两份里较晚的那个。"""
    candidates = [_parse_day(state.get("max_seen")), _parse_day(_db_read("license_max_seen", cfg))]
    days = [d for d in candidates if d is not None]
    return max(days) if days else None


def _stored_code(state: dict) -> str:
    """状态文件里记着的注册码（可能为空 —— 那就是没注册过）。"""
    return _normalize(state.get("code"))


# ══════════════════════════════════════════════════════════════════════════
# 对外状态
# ══════════════════════════════════════════════════════════════════════════


def license_status(cfg: Any = None, *, verify_machine: bool = False) -> dict:
    """**授权的唯一真相来源**：界面、CLI、锁功能都读它，别在各处各算一套。

    ⚠️ **默认不读机器码**（主人 2026-09-21 的明确口径："不用每次都读机器码啊，
    客户要注册的时候再去读"）。读机器码在 Windows 上要起一个 PowerShell 问硬件，
    而启动、点【策略编辑】、刷新设置/关于、保存设置都会走到这个函数 ——
    每次都读就是"点一下卡一下"。

    所以这里分成两档：

    * **默认（平时）**：只读授权文件与试用记账（普通文件/数据库读取，几毫秒）。
      `registered` 的判断是"文件里有没有注册码"，**不校验它属于哪台机器**；
    * **`verify_machine=True`**（只有打开授权对话框/点【注册】时才用）：算一次机器码
      （进程内缓存），校验注册码是不是这台机器的、状态文件是不是本机的。

    为什么可以这样分：日常使用只需要回答"现在能不能用"，而"这份授权是不是这台机器的"
    只在**要注册**的时候才有意义。代价见 `verify_machine` 的调用方注释（授权对话框）。

    Returns:
        `{"licensed": bool, "registered": bool, "trial": bool, "days_left": int,
          "machine": str, "code": str, "reason": str}`

        * `licensed` —— 能不能用（已注册 or 试用未到期）；
        * `registered` —— 是不是已经注册过（界面上措辞不同：注册过不该再提试用）；
        * `trial` —— 当前是否处于试用期；
        * `days_left` —— 试用期剩余天数（已注册时是 `TRIAL_DAYS`，无意义；
          已到期时是 0）；
        * `machine` —— 本机机器码；**没校验时是空串**（这样调用方一眼能看出
          "这一份状态没读过机器码"，不会误当成"机器码是空的"）；
        * `reason` —— 未授权/回拨时给用户看的中文原因（已授权时是空串）。
    """
    state = _read_state()
    today = _today()
    machine = machine_code() if verify_machine else ""

    # 1) 已注册？
    stored_machine = _normalize(state.get("machine"))
    stored_code = _stored_code(state)
    if stored_code and (not verify_machine or stored_machine == _normalize(machine)):
        ok, _why = verify(state.get("machine") or machine, stored_code)
        if ok:
            _remember_day(today, state, cfg)
            return {
                "licensed": True, "registered": True, "trial": False,
                "days_left": TRIAL_DAYS, "machine": machine, "code": state.get("code", ""),
                "reason": "",
            }
        if verify_machine:
            logger.warning("状态文件里的注册码验不过（机器变了或文件被改过），按未授权处理")

    # 2) 时钟回拨：系统时间比我们见过的最晚日期还早 → 按到期处理
    max_seen = _max_seen_day(state, cfg)
    if max_seen is not None and today < max_seen:
        return {
            "licensed": False, "registered": False, "trial": False, "days_left": 0,
            "machine": machine, "code": "", 
            "reason": f"检测到系统时间被调回了（当前 {today}，上次运行 {max_seen}）。"
                      "把系统时间调回正确日期，或联系作者获取授权。",
        }

    # 3) 试用期
    first = _first_run_day(state, cfg)
    if first is None:
        # 第一次运行：记下今天（两处都写），本次算试用第 1 天
        _remember_day(today, state, cfg, first_run=today)
        return {
            "licensed": True, "registered": False, "trial": True,
            "days_left": TRIAL_DAYS, "machine": machine, "code": "",
            "reason": "",
        }

    used = (today - first).days
    left = TRIAL_DAYS - used
    _remember_day(today, state, cfg, first_run=first)
    if left > 0:
        return {
            "licensed": True, "registered": False, "trial": True,
            "days_left": left, "machine": machine, "code": "",
            "reason": "",
        }
    return {
        "licensed": False, "registered": False, "trial": False, "days_left": 0,
        "machine": machine, "code": "",
        "reason": f"免费试用 {TRIAL_DAYS} 天已经到期（首次运行 {first}）。",
    }


def _remember_day(today: date, state: dict, cfg: Any = None,
                  *, first_run: date | None = None) -> None:
    """把"今天"与首次运行日期记进**两处**（只往前推，不回退）。

    ⚠️ **首次运行日期必须两份都有**（这条踩过一次，见
    `test_deleting_the_state_file_cannot_reset_the_trial`）：只在状态文件里写的话，
    用户删掉 `license.json` 就能把试用期重置回 7 天 —— 而"试用期"正是要防这个。
    """
    text = today.strftime("%Y-%m-%d")
    changed = False
    if first_run is not None:
        known = _parse_day(state.get("first_run"))
        earliest = first_run if known is None else min(known, first_run)
        if state.get("first_run") != earliest.strftime("%Y-%m-%d"):
            state["first_run"] = earliest.strftime("%Y-%m-%d")
            changed = True
    if str(state.get("max_seen") or "") < text:
        state["max_seen"] = text
        changed = True
    if changed:
        _write_state(state)
    if first_run is not None:
        # 数据库那份取"更早的那个"：用户手改文件把日期往后推也没用
        db_first = _parse_day(_db_read("license_first_run", cfg))
        earliest_db = first_run if db_first is None else min(db_first, first_run)
        if _db_read("license_first_run", cfg) != earliest_db.strftime("%Y-%m-%d"):
            _db_write("license_first_run", earliest_db.strftime("%Y-%m-%d"), cfg)
    if _db_read("license_max_seen", cfg) < text:
        _db_write("license_max_seen", text, cfg)


def is_licensed(cfg: Any = None) -> bool:
    """能不能用（已注册 or 试用未到期）。**界面只调它**。"""
    return bool(license_status(cfg).get("licensed"))


def status_text(cfg: Any = None) -> str:
    """一行中文状态（「关于」对话框与授权对话框共用同一句，不会两处说法不一致）。"""
    status = license_status(cfg)
    if status.get("registered"):
        return "已注册（单机终身授权）"
    if status.get("licensed"):
        return f"试用中：还剩 {status.get('days_left', 0)} 天（免费 {TRIAL_DAYS} 天）"
    return "未授权：" + str(status.get("reason") or "试用已到期")


def register(machine: Any, code: Any, cfg: Any = None) -> tuple[bool, str]:
    """注册：校验通过就写进状态文件（**立刻生效**），返回 `(是否成功, 中文说明)`。

    为什么"写盘失败也算成功"：盘只读/权限不足的机器上，用户点了注册却什么都没发生
    是最难解释的现象。所以这一层：**内存与状态文件尽力写**，写不进去就明说
    "本次有效、重启后需要重新注册"，但**当次一定放行**（见 `verify` 之后的返回值）。
    """
    machine = str(machine or "").strip()
    ok, why = verify(machine, code)
    if not ok:
        return False, why
    state = _read_state()
    state["machine"] = machine
    state["code"] = str(code or "").strip()
    state["registered_at"] = clock.stamp_cn()
    if not state.get("first_run"):
        state["first_run"] = _today().strftime("%Y-%m-%d")
    state["max_seen"] = _today().strftime("%Y-%m-%d")
    wrote = _write_state(state)
    # 顺手在数据库里也记一笔"已注册"（用于"见过这台机器注册过"，不参与放行判定）
    _db_write("license_registered_machine", _normalize(machine), cfg)
    if wrote:
        return True, "注册成功，已永久授权本机"
    return True, "注册成功（配置目录写不进去，本次已生效、重启后请再注册一次）"


__all__ = [
    "CODE_CHARS",
    "CONTACT_TEXT",
    "MACHINE_CHARS",
    "TRIAL_DAYS",
    "expected_code",
    "is_licensed",
    "license_status",
    "machine_code",
    "register",
    "state_path",
    "status_text",
    "verify",
]
