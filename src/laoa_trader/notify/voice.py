"""中文语音朗读（把提醒"喊出来"）：Windows 自带语音合成，**不引任何新依赖**。

用户原话（2026-09-18）："可不可以编写个机器人，直接中文语音提醒……有消息时大声喊出
消息内容。" —— 桌宠负责形象与气泡，这个模块只负责"出声"。

为什么走 PowerShell + `System.Speech`，而不是 pip 装一个 TTS 库
--------------------------------------------------------------
1. **分发成本**：这是给 Windows 单机版用户的 exe，用户机器上不会有 pip 环境。
   多一个依赖 = 打包体积 + 一个"到你机器上装不上"的失败点；Windows 自带的中文语音
   （Microsoft Huihui / Yaoyao 之类）在中文系统上**本来就有**，零安装。
2. **离线**：不联网、不调用任何云服务（提醒内容里有持仓与价格，不该往外发）。
3. **会失败的东西都不许影响主流程**：没有 PowerShell、没有中文音色、被用户静音、
   非 Windows —— 一律**静默降级**（只记日志），提醒照样进消息列表、桌宠照样冒气泡。

三条硬口径
----------
* **绝不阻塞界面**：`speak()` 只把文本丢进队列，真正的进程在**后台守护线程**里跑；
  多条消息**按顺序念、不重叠**（朗读本身要几秒，重叠会糊成一团）。
* **没有中文音色就不念**：拿英文音色念中文是怪腔怪调，比不念更糟。枚举
  `GetInstalledVoices()` 里 `Culture` 以 `zh` 开头的；一个都没有 → 不念，
  并且**只提示一次**"这台机器没有中文语音，已跳过朗读"（不刷屏、不报错）。
* **念之前先清洗**：提醒文本里带着 markdown 星号、竖线、emoji、URL 与括号里的
  英文代码，直接念会变成"星号星号""竖线"之类的噪音 —— 清洗后才像人在说话。

可测性：真正"起进程说话"那一步是模块级的 `_run()`；枚举音色是 `_list_voices_raw()`。
测试替换这两个即可，**不会真的发出声音**（离屏 CI 上也没有 Windows）。
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 朗读队列最多排几条：排太长会变成"消息早过时了还在念"，宁可只念最新的几条
MAX_QUEUE = 5

#: 枚举音色与朗读各自的最长等待（秒）。PowerShell 起一次约 0.3~1s；
#: 超过这个时间说明环境有问题，直接放弃这一条，别把队列堵住。
LIST_TIMEOUT = 10.0
SPEAK_TIMEOUT = 60.0

#: "没有中文音色"这句提示只发一次（用户看不懂的地方才提示，且不刷屏）
_prompted_missing_voice = False

#: 音色名单缓存（枚举一次要起一个进程，不该每条消息都做）
_voices: list[tuple[str, str]] | None = None
_voices_lock = threading.Lock()

#: 静音到什么时刻（`time.monotonic()`；0 = 没静音）。桌宠右键的【静音一小时】写它
_mute_until = 0.0
_mute_lock = threading.Lock()

_queue: "queue.Queue[str]" = queue.Queue(maxsize=MAX_QUEUE)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

#: 真正干活的函数（测试替换它 —— 替换之后**不会有任何进程被起起来**）
_runner: Callable[[list[str]], Any] | None = None

#: emoji 与装饰符号：念出来是"警告标志""红色圆点"之类的噪音，全部去掉
_NOISE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"      # 各类 emoji / 图标
    "\u2600-\u27BF"              # ☀✔✖ 等符号
    "\uFE0F\u200d"               # 变体选择符 / 零宽连接符
    "\u2b00-\u2bff"
    "]+"
)
_URL = re.compile(r"https?://\S+")
_MD = re.compile(r"[*`_#~]+")
_MULTI_SPACE = re.compile(r"\s+")


# ── 平台与音色 ────────────────────────────────────────────────────────


def available() -> bool:
    """这台机器**有可能**能念中文吗（只看平台与 PowerShell 在不在，不做枚举）。

    只做"值不值得往下试"的判断：真正的判据是枚举出来有没有中文音色（见 `voice_name()`）。
    """
    if not sys.platform.startswith("win"):
        return False
    return _powershell() is not None


def _powershell() -> str | None:
    """PowerShell 可执行文件名（Windows 上一定有一个；找不到就是环境有问题）。"""
    import shutil

    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _list_voices_raw() -> list[tuple[str, str, str]]:
    """枚举系统音色 → `[(名字, 区域, 性别)]`；失败返回空列表（**不抛异常**）。

    性别（`$i.Gender`，取值 `Male` / `Female` / `NotSet`）是 2026-09-18 加的：
    用户要求把"音色"改成**男声 / 女声**可选，而不是按中英文列一堆音色名。
    有些语音不报性别（`NotSet`）—— 那时按空串处理，挑选逻辑会退回"自动挑中文"。

    单独一层是为了让测试能替换掉（CI 上没有 Windows，也不该真去起进程）。
    """
    exe = _powershell()
    if exe is None:
        return []
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices()"
        " | ForEach-Object { $i=$_.VoiceInfo; \"$($i.Name)|$($i.Culture)|$($i.Gender)\" }"
    )
    try:
        done = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=LIST_TIMEOUT,
            creationflags=_no_window_flag(),
        )
    except Exception as exc:  # noqa: BLE001 - 环境问题一律降级，不往上抛
        logger.debug(f"枚举语音失败（当作没有可用音色）：{exc}")
        return []
    voices: list[tuple[str, str, str]] = []
    for line in (done.stdout or "").splitlines():
        name, _, rest = line.strip().partition("|")
        culture, _, gender = rest.partition("|")
        if name:
            voices.append((name, culture.strip(), _normalize_gender(gender)))
    return voices


def _normalize_gender(raw: Any) -> str:
    """SAPI 的 `Gender` → `"female"` / `"male"` / `""`（认不出来就是空串）。"""
    text = str(raw or "").strip().lower()
    if text.startswith("f"):
        return "female"
    if text.startswith("m"):
        return "male"
    return ""


def normalize_voices(voices: Any) -> list[tuple[str, str, str]]:
    """把音色名单统一成 `(名字, 区域, 性别)` 三元组（性别规范成 `female`/`male`/`""`）。

    规范化放在**进缓存这一步**、而不是只放在 PowerShell 解析那一层：测试替身直接喂
    `("Huihui", "zh-CN", "Female")` 这种原始值，两条路必须得到同一个结果 ——
    否则"真实环境挑得对、测试里看着也对"就成了假象。
    """
    out: list[tuple[str, str, str]] = []
    for item in (voices or []):
        parts = list(item) if isinstance(item, (list, tuple)) else [item]
        name = str(parts[0] if parts else "").strip()
        culture = str(parts[1] if len(parts) > 1 else "").strip()
        gender = _normalize_gender(parts[2] if len(parts) > 2 else "")
        if name:
            out.append((name, culture, gender))
    return out


def installed_voices(*, refresh: bool = False) -> list[tuple[str, str, str]]:
    """系统里装着的音色 → `[(名字, 区域, 性别)]`（缓存；`refresh=True` 重新枚举）。

    给"漂不挑得到男声/女声"用（用户 2026-09-18：音色改成男声/女声可选）。
    设置页不再逐个列音色名（那是上一版的做法，用户要求改掉）。
    """
    global _voices
    if not available():
        return []
    with _voices_lock:
        if _voices is None or refresh:
            _voices = normalize_voices(_list_voices_raw())
        return list(_voices)


def voice_label(name: str, culture: str = "") -> str:
    """下拉框里显示的一行：`微软慧慧（zh-CN）` 这种（没区域就只写名字）。"""
    name = str(name or "").strip()
    culture = str(culture or "").strip()
    return f"{name}（{culture}）" if culture else name


def voice_name() -> str | None:
    """挑一个中文音色名；没有中文音色返回 None（调用方据此**不念**）。

    优先 `zh-CN`，其次任何 `zh*`：中文系统上通常就一个，英文系统上可能挂着
    `zh-TW` 之类的（念简体文本仍然听得懂，比英文音色强得多）。
    """
    global _voices
    if not available():
        return None
    with _voices_lock:
        if _voices is None:
            _voices = normalize_voices(_list_voices_raw())
        voices = list(_voices)
    simplified = [v[0] for v in voices if v[1].lower().startswith("zh-cn")]
    if simplified:
        return simplified[0]
    chinese = [v[0] for v in voices if v[1].lower().startswith("zh")]
    if chinese:
        return chinese[0]
    if voices:
        logger.info("系统里没有中文语音，已跳过朗读（英文音色念中文会是怪腔怪调）")
    return None


def reset_cache() -> None:
    """清掉音色缓存（测试与"用户装了新语音"之后用；正常流程不需要）。"""
    global _voices
    with _voices_lock:
        _voices = None


def has_chinese_voice() -> bool:
    """这台机器能不能念中文（设置页用它把"语音"那一项说清楚）。"""
    return voice_name() is not None


# ── 静音 ─────────────────────────────────────────────────────────────


def mute_for(seconds: float) -> None:
    """静音一段时间（秒）。桌宠右键【静音一小时】用它 —— 只影响**朗读**，
    气泡与消息列表照常（用户要的是"别出声"，不是"别提醒"）。"""
    global _mute_until
    with _mute_lock:
        _mute_until = time.monotonic() + max(0.0, float(seconds))


def muted() -> bool:
    with _mute_lock:
        return time.monotonic() < _mute_until


def mute_remaining() -> float:
    with _mute_lock:
        return max(0.0, _mute_until - time.monotonic())


def unmute() -> None:
    global _mute_until
    with _mute_lock:
        _mute_until = 0.0


# ── 文本清洗 ─────────────────────────────────────────────────────────


def sanitize(text: Any) -> str:
    """把提醒文本洗成"像人说的话"：去掉 emoji / markdown / URL，压掉多余空白。

    例子：`🛑 触及止损 ｜ 贵州茅台(600519) 现价 1234.56 **已跌破** https://x/y`
    → `触及止损 贵州茅台(600519) 现价 1234.56 已跌破`
    """
    out = str(text or "")
    out = _URL.sub(" ", out)
    out = _NOISE.sub(" ", out)
    out = _MD.sub("", out)
    out = out.replace("|", "，").replace("｜", "，")
    # 中文标点两侧的空格要收掉：`触及止损 ， 茅台` 这种念出来会有奇怪的停顿
    out = re.sub(r"\s*([，,、。；;：:！!？?])\s*", r"\1", out)
    out = _MULTI_SPACE.sub(" ", out).strip()
    # 首尾的标点念出来不好听（"，"开头那种），顺手收掉
    return out.strip("，,、;；:：-— ")


#: 逐位朗读用的汉字
_CHINESE_DIGITS = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
                   "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
                   ".": "点", "-": "负"}

#: 数字（含可选的负号与小数部分）
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

#: 跟在这些字后面的数字是**数量**（成交量、家数、天数、百分比、倍数…），保持整读：
#: "五百零七家"比"五零七家"顺耳得多；而代码与价格要一位一位听才准。
_UNIT_AFTER = set("%％万亿手只家元股天倍条个点年")

#: 六位以上的纯整数按**代码**处理（A 股代码就是 6 位）
_CODE_MIN_DIGITS = 6


def digits_for_speech(text: Any) -> str:
    """把"该逐位念"的数字改成逐位（用户 2026-09-18 的原话："播报代码可以设置成一个
    一个读数字吗？现在直接是 6 万零 5 百一十九"）。

    规则（为什么这么分）：

    * **股票代码**（6 位及以上纯整数）→ 逐位：`600519` 整读会变成"六十万零五百一十九"，
      这正是用户实报的问题；
    * **价格 / 带小数的数**（`1234.56`）→ 逐位（含"点"）：听价格要一位一位才准；
    * **带单位或百分号的"数量"**（`500万股`、`37家`、`3.21%`、`2.5倍`）→ **保持整读**：
      这些按数量念才自然；
    * **1~5 位纯整数**（条数、天数）→ 保持整读（同上）。

    负数带"负"（`-3.2` → 负三点二）。这个变换只在**朗读**这一路做
    （消息列表、桌面导出、推送正文里仍然是 `600519` 原样）。
    """
    out = str(text or "")

    def _spell(number: str) -> str:
        return "".join(_CHINESE_DIGITS.get(ch, ch) for ch in number)

    pieces: list[str] = []
    cursor = 0
    for match in _NUMBER.finditer(out):
        raw = match.group(0)
        digits = raw.lstrip("-")
        after = out[match.end():match.end() + 1]
        amount = bool(after) and after in _UNIT_AFTER
        if amount:
            keep = True                      # 数量：整读
        elif "." in digits:
            keep = False                     # 价格/小数：逐位
        else:
            keep = len(digits) < _CODE_MIN_DIGITS   # 6 位以上当代码逐位，短整数整读
        if keep:
            continue
        pieces.append(out[cursor:match.start()])
        pieces.append(_spell(raw))
        cursor = match.end()
    if not pieces:
        return out
    pieces.append(out[cursor:])
    return "".join(pieces)


def prepare(text: Any, cfg: Any = None) -> str:
    """朗读前的最后一道加工：清洗 + （按设置）数字逐位。

    单独一层是为了让 `speak()` 与 `speak_now()` **走同一条路** —— 两条路的文本
    处理哪怕差一点点，用户就会听到"消息列表里那条念得对、试听念得不对"。
    """
    out = sanitize(text)
    if cfg is None or bool(getattr(cfg, "notify_voice_digits", True)):
        out = digits_for_speech(out)
    return out


def compose(target: str, kind_label: str, detail: str = "", price: Any = None) -> str:
    """拼一句要念的话：`名称(代码)，类型，说明 现价 x`。

    与消息列表里那一行**同源**（都来自同一条提醒行），但顺序按"听"的习惯排：
    先说谁、再说发生了什么、最后才是数字 —— 听的人前两个字就知道要不要抬头看屏幕。
    """
    parts = [sanitize(target), sanitize(kind_label)]
    if price not in (None, ""):
        parts.append(f"现价 {price}")
    body = sanitize(detail)
    if body:
        parts.append(body)
    return sanitize("，".join(p for p in parts if p))


# ── 朗读 ─────────────────────────────────────────────────────────────


#: 语速倍率的范围（界面给的就这个区间；1.0 = 正常）
RATE_MIN = 0.5
RATE_MAX = 2.0

#: 倍率 → SAPI Rate 的换算底数。SAPI 的 Rate（-10~10）在听感上**近似对数**：
#: 加减同一个数带来的"快慢变化"是相对量，所以用对数映射而不是线性。
#: 底数取 2.2 是实测口径：倍率 2.0 → +9、1.5 → +5、1.2 → +2、0.8 → -2、0.5 → -9，
#: 与"1.0 附近才自然、两端都很难听"的 SAPI 特性对得上（±10 几乎没法听，所以不贴边）。
_RATE_LOG_BASE = 2.2


def rate_to_sapi(multiplier: Any) -> int:
    """语速倍率（1.0 = 正常）→ Windows SAPI 的 Rate（-10~10）。"""
    import math

    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        return 0
    value = min(max(value, RATE_MIN), RATE_MAX)
    if value <= 0:
        return 0
    rate = round(10 * math.log(value) / math.log(_RATE_LOG_BASE))
    return int(min(max(rate, -10), 10))


def sapi_to_rate(rate: Any) -> float:
    """Windows SAPI 的 Rate（-10~10）→ 语速倍率（老配置反算用）。"""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return 1.0
    value = min(max(value, -10.0), 10.0)
    multiplier = _RATE_LOG_BASE ** (value / 10.0)
    return round(min(max(multiplier, RATE_MIN), RATE_MAX), 2)


def gender_label(gender: Any) -> str:
    """`"female"` / `"male"` → 界面上的中文（设置页说明行用它）。"""
    return {"female": "女声", "male": "男声"}.get(_normalize_gender(gender), "该音色")


def voices_of_gender(gender: str) -> list[tuple[str, str, str]]:
    """某种性别的音色（**中文优先排序**：`zh-CN` → 其它 `zh*` → 其它语言）。"""
    wanted = _normalize_gender(gender)
    if not wanted:
        return []
    same = [v for v in installed_voices() if v[2] == wanted]
    same.sort(key=lambda v: (0 if v[1].lower().startswith("zh-cn")
                             else 1 if v[1].lower().startswith("zh") else 2))
    return same


def resolve_gender_voice(gender: str) -> str | None:
    """按性别挑一个音色；挑不到返回 None（调用方回落到"自动挑中文"）。

    挑选规则（用户 2026-09-18 定的口径）：该性别的音色里 **`zh-CN` 优先，其次任何 `zh*`，
    再其次该性别的其它语言音色** —— 最后那一档只在"这台机器确实有中文音色"时才用得上
    （一个中文音色都没有的机器上，`speak()` 那条"没有中文音色就不念"的硬口径会拦住它，
    不会出现英文音色硬念中文的情况）。

    取不到性别（有些语音报 `NotSet`）时这里自然返回 None → 回落自动。
    """
    same = voices_of_gender(gender)
    if not same:
        logger.debug(f"没有性别为 {gender} 的音色，改用自动挑中文")
        return None
    chinese = [v for v in same if v[1].lower().startswith("zh")]
    if chinese:
        return chinese[0][0]
    # 该性别只有非中文音色：机器上还有中文音色的话，用它（用户明确点了这个性别）；
    # 一个中文音色都没有就直接回落自动 —— 自动那边会返回 None（不念）。
    return same[0][0] if voice_name() is not None else None


def configured_gender(cfg: Any = None) -> str:
    """配置里的音色选项 → `"female"` / `"male"` / `""`（自动）。

    老配置里存的是**音色完整名**（上一版的写法）—— 认不出来一律当自动，
    原因见 `config._normalize_voice_name`：界面已经不再列具体音色，
    留一个选不中的名字只会让"设置页显示自动、实际却用着某个音色"两处对不上。
    """
    raw = str(getattr(cfg, "notify_voice_name", "") or "").strip().lower()
    if raw in ("female", "女声"):
        return "female"
    if raw in ("male", "男声"):
        return "male"
    return ""


def chosen_voice(cfg: Any = None) -> str | None:
    """当前该用哪个音色（优先级从高到低）。

    1. **调用方已经解析好的具体音色名**（`speak_now(voice=...)` 合成的临时配置，见
       `_VoiceOverride.resolved_voice`）—— 设置页【试听】走的就是这条路：面板上选的是
       "男声"，界面先把它解析成一个具体音色名再传进来；
    2. 配置里的**性别**（男声/女声）→ 在该性别的音色里挑，挑不到就自动；
    3. **自动挑中文**。
    """
    explicit = getattr(cfg, "resolved_voice", None)
    if explicit:
        return str(explicit)
    gender = configured_gender(cfg)
    if gender:
        picked = resolve_gender_voice(gender)
        if picked:
            return picked
    return voice_name()


def speak(text: str, *, cfg: Any = None) -> bool:
    """把 `text` 排进朗读队列（**立刻返回，绝不阻塞**）。

    Returns:
        True = 已经排队（不代表真的念了）；False = 没排队（没开语音 / 没有中文音色 /
        正在静音 / 队满了）。调用方不需要为 False 做任何事 —— 气泡与消息列表照常。
    """
    if cfg is not None and not bool(getattr(cfg, "notify_voice", True)):
        return False
    if not str(text or "").strip():
        return False
    if muted():
        logger.debug("静音中，跳过朗读")
        return False
    if chosen_voice(cfg) is None:
        _prompt_missing_voice()
        return False
    body = prepare(text, cfg)
    if not body:
        return False
    # 队列里带上**这一刻的全部语音参数**（音色/音量/语速）：用户改完设置立刻生效，
    # 不用等队列里排着的几条念完（"改了没反应"是最容易被当成坏了的那种现象）
    item = {
        "text": body,
        "voice": chosen_voice(cfg),
        "volume": float(getattr(cfg, "notify_voice_volume", 0.9) or 0.9),
        "rate": float(getattr(cfg, "notify_voice_rate", 1.0) or 1.0),
    }
    try:
        _queue.put_nowait(item)
    except queue.Full:
        # 队满：丢掉**最旧**的一条再排新的（新消息永远比旧消息值得念）。
        # 塞回去的也是同一份 item（早先这里塞的是纯文本，音色/音量/语速就丢了 ——
        # 表现为"队满之后那几条用的是默认音色"，很隐蔽）
        try:
            _queue.get_nowait()
            _queue.put_nowait(item)
        except Exception:  # noqa: BLE001 - 丢不进去就算了，不影响任何事
            return False
    _ensure_worker()
    return True


def _prompt_missing_voice() -> None:
    """没有中文音色时只提示一次（放在消息列表/日志里，不弹窗打扰）。

    只在 **Windows** 上提示：开发机（Linux/macOS）本来就没有 `System.Speech`，
    在那种机器上刷一句"装个中文语音包"是误导。
    """
    global _prompted_missing_voice
    if _prompted_missing_voice or not sys.platform.startswith("win"):
        return
    _prompted_missing_voice = True
    logger.warning("这台机器没有中文语音（Windows 语音里没有 zh 音色），提醒不朗读；"
                   "装一个中文语音包即可（设置 → 时间和语言 → 语音）")


#: 「让朗读线程收工」的哨兵：队列里出现它，`_loop` 就返回。
#:
#: 为什么需要（2026-09-21，CI 三次红在同一个地方）：朗读线程原来是个"永远堵在
#: `_queue.get()`"的守护线程 —— 在 Windows 上它真的会起来（那里有 PowerShell），
#: 于是它可能活到 pytest 收尾之后：那时 `sys.stderr` 已关，它再写一句日志就是
#: `I/O operation on closed file`，再往后就是整个进程中止。守护线程"进程退出时自然结束"
#: 在正常情况下成立，但在"解释器已经开始收尾、Qt 也在拆对象"的窗口期里并不安全 ——
#: 所以给它一个**确定的收工信号**，由 `shutdown()` 在收尾时送进去。
_STOP = object()


def _ensure_worker() -> None:
    """起（或复用）朗读线程：守护线程；收尾由 `shutdown()` 显式叫停。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="voice-speak", daemon=True)
        _worker.start()


def shutdown(timeout: float = 3.0) -> bool:
    """叫停朗读线程（幂等）：送哨兵 → 等它退出 → 清掉引用。

    什么时候调：`MainWindow.shutdown()`（所有退出路径都走它）与测试收尾夹具。
    返回值只表示"线程确实退了"，失败不抛异常（收尾失败不该挡住退出）。
    """
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is None:
        return True
    try:
        _queue.put_nowait(_STOP)
    except queue.Full:
        # 队列满：丢一条最旧的再塞哨兵（哨兵必须能进去，否则线程收不了工）
        try:
            _queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _queue.put_nowait(_STOP)
        except queue.Full:
            pass
    worker.join(timeout=timeout)
    return not worker.is_alive()


def _loop() -> None:
    """逐条念（**不重叠**）：真正慢的是起进程那一下，所以整段都在这里排队等完。"""
    while True:
        item = _queue.get()
        if item is _STOP:              # 收尾信号：干净退出（见 `shutdown()`）
            return
        try:
            if isinstance(item, dict):
                text, voice = item["text"], item["voice"]
                volume, rate = item["volume"], item["rate"]
            else:                      # 老格式（纯文本）：走默认参数
                text, voice, volume, rate = str(item), None, 0.9, 1.0
            if muted() or voice is None:
                continue
            run_command(_speak_command(text, voice=voice, volume=volume,
                                       rate=rate_to_sapi(rate)))
        except Exception as exc:  # noqa: BLE001 - 念不出来不许影响任何别的东西
            logger.debug(f"朗读失败（已忽略）：{exc}")
        finally:
            _queue.task_done()


def _no_window_flag() -> int:
    """Windows 上"别弹黑窗"的标志；其它平台是 0。

    这是必须的：用 PowerShell 念一句话却闪一个黑框出来，比不念还难受。
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def _speak_command(text: str, *, voice: str | None = None,
                   volume: float = 0.9, rate: int = 0) -> list[str]:
    """拼出"念一句话"的 PowerShell 命令（**纯函数**，测试直接断言它）。

    PowerShell 单引号字符串里 `'` 要写成 `''`（否则用户的股票名里带个引号就把命令拼坏）。
    """
    exe = _powershell() or "powershell"
    safe = str(text).replace("'", "''")
    lines = ["Add-Type -AssemblyName System.Speech;",
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"]
    picked = voice or voice_name()
    if picked:
        lines.append(f"try {{ $s.SelectVoice('{picked.replace(chr(39), chr(39) * 2)}') }} catch {{}};")
    lines.append(f"$s.Volume = {max(0, min(100, int(round(volume * 100))))};")
    lines.append(f"$s.Rate = {max(-10, min(10, int(rate)))};")
    lines.append(f"$s.Speak('{safe}');")
    return [exe, "-NoProfile", "-NonInteractive", "-Command", " ".join(lines)]


def run_command(command: list[str]) -> None:
    """真正起进程念（**唯一会发声的一步**；测试替换 `_runner` 即可绕过）。

    每次都等它念完（`SPEAK_TIMEOUT` 兜底）：队列里的下一条要等这一条结束，
    否则两句话会叠在一起。
    """
    runner = _runner
    if runner is not None:
        runner(command)
        return
    subprocess.run(command, capture_output=True, text=True,
                   timeout=SPEAK_TIMEOUT, creationflags=_no_window_flag())


class _VoiceOverride:
    """`speak_now(voice=..., volume=..., rate=...)` 用的临时配置。

    语速这一项是**倍率**（与配置同一个量纲，1.0 = 正常）；换算成 SAPI 的整数
    只发生在真正拼命令那一步（`_speak_command`）。
    """

    def __init__(self, base: Any, *, voice: str | None, volume: float | None,
                 rate: float | None, digits: bool | None = None):
        self.notify_voice = bool(getattr(base, "notify_voice", True)) if base is not None else True
        #: `speak_now(voice=...)` 传进来的是**具体音色名**（界面已经把"男声/女声"解析过了），
        #: 所以它不能塞回 `notify_voice_name`（那个字段现在是"auto/female/male"枚举）——
        #: 塞回去会被当成认不出的老值 → 回落自动，用户就会听到"选男声却念女声"。
        self.resolved_voice = str(voice) if voice else None
        self.notify_voice_name = str(getattr(base, "notify_voice_name", "") or "")
        self.notify_voice_volume = (
            float(volume) if volume is not None
            else float(getattr(base, "notify_voice_volume", 0.9) or 0.9)
        )
        self.notify_voice_rate = (
            float(rate) if rate is not None
            else float(getattr(base, "notify_voice_rate", 1.0) or 1.0)
        )
        self.notify_voice_digits = (
            bool(digits) if digits is not None
            else bool(getattr(base, "notify_voice_digits", True))
        )


def _cfg_with_overrides(cfg: Any, *, voice: str | None, volume: float | None,
                        rate: float | None, digits: bool | None = None) -> Any:
    """把"这一次的语音参数"合成一份临时配置（None 的项沿用原来的 cfg）。"""
    return _VoiceOverride(cfg, voice=voice, volume=volume, rate=rate, digits=digits)


def can_speak(*, cfg: Any = None, force: bool = False) -> bool:
    """现在这一刻能不能念（**只做判断、不出声、不起进程**）。

    界面用它来决定"要不要起后台线程去念"以及"要不要解释为什么没声" ——
    真正念一句要几秒（起 PowerShell + 说完），放在主线程里就是"界面卡几秒"，
    而"能不能念"这个问题是**缓存过的**（音色名单只枚举一次），秒回。
    """
    if cfg is not None and not bool(getattr(cfg, "notify_voice", True)):
        return False
    if not force and muted():
        return False
    return chosen_voice(cfg) is not None


def speak_now(text: str, *, cfg: Any = None, force: bool = False,
              voice: str | None = None, volume: float | None = None,
              rate: float | None = None, digits: bool | None = None) -> bool:
    """同步念一句（桌宠右键【试喊一条】用它：用户点了按钮，要立刻听到）。

    只走语音这一条路，不进队列 —— 与 `speak()` 的"排队不阻塞"不同：
    这里**故意**让调用方自己决定要不要放到后台线程里（界面里就是放在后台线程里调它，
    因为这一句要念好几秒）。调用前可以先问 `can_speak()`，好把"没声"的原因说清楚。

    Args:
        force: 用户**主动**点的"试喊一条"传 True —— 静音的意思是"别被盘中提醒打扰"，
            不是"我点它也不许出声"。真正"没有中文音色"这条硬约束不受它影响。
        voice / volume / rate / digits: 覆盖配置里的音色、音量、语速（**倍率**，1.0 = 正常）
            与"数字逐位"（设置页那个【试听】按钮用它们试**面板上当前**的值，不必先保存）。
            传 None 就走配置/默认。
    """
    if voice or volume is not None or rate is not None or digits is not None:
        # 覆盖值走一个临时 cfg：`_speak_command` 只认 `notify_voice_name` 之类的属性，
        # 与其到处加参数，不如在这里合成一份"这次就用这套"的配置 —— 逻辑只有一套。
        cfg = _cfg_with_overrides(cfg, voice=voice, volume=volume, rate=rate, digits=digits)
    if cfg is not None and not bool(getattr(cfg, "notify_voice", True)):
        return False
    body = prepare(text, cfg)
    if not body:
        return False
    if not can_speak(cfg=cfg, force=force):
        _prompt_missing_voice()
        return False
    try:
        run_command(_speak_command(
            body,
            voice=chosen_voice(cfg),
            volume=float(getattr(cfg, "notify_voice_volume", 0.9) or 0.9),
            rate=rate_to_sapi(getattr(cfg, "notify_voice_rate", 1.0)),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"朗读失败（已忽略）：{exc}")
        return False
    return True


def test_text() -> str:
    """【试喊一条】用的演示文本（用户非交易时段也能验证"会不会念"）。"""
    return "试喊一条：贵州茅台 600519，触及止损，现价 1234.56，已跌破百分之五"


__all__ = [
    "MAX_QUEUE",
    "RATE_MAX",
    "RATE_MIN",
    "available",
    "configured_gender",
    "digits_for_speech",
    "gender_label",
    "normalize_voices",
    "prepare",
    "rate_to_sapi",
    "resolve_gender_voice",
    "sapi_to_rate",
    "voices_of_gender",
    "can_speak",
    "compose",
    "has_chinese_voice",
    "mute_for",
    "mute_remaining",
    "muted",
    "reset_cache",
    "run_command",
    "sanitize",
    "speak",
    "speak_now",
    "test_text",
    "unmute",
    "voice_name",
    "shutdown",
]
