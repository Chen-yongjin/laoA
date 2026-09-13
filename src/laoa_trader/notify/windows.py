"""Windows 原生通知（winotify）。

为什么用 winotify
-----------------
桌面版的核心体验是"不用打开窗口就知道该动手了"。`winotify` 走的是
Windows 10/11 的原生 Toast（Action Center 里能看到历史），比自绘气泡醒目，
也不需要额外依赖。

优雅降级
--------
- 非 Windows 平台：`winotify` 装不上（pyproject 里就是 `sys_platform=='win32'` 条件依赖），
  这里导入失败**不是错误**，返回"不支持"即可 —— 另外两路通知照常工作；
- Windows 上但通知被系统关闭/失败：只记日志，返回失败原因，不抛异常。

点击跳转：Toast 挂一个"打开雪球"按钮（`launch`），点一下直接看该股 ——
不需要任何权限，纯 URL 跳转。
"""

from __future__ import annotations

import sys
from typing import Any

from laoa_trader.log import get_logger

logger = get_logger(__name__)

APP_ID = "老A法师 · 交易终端"
SUPPORTED = sys.platform == "win32"

#: winotify 的 `Notification(duration=…)` **只认这两个值**。
#: 真库源码里就是 `if duration not in ("short", "long"): raise ValueError("Duration is not
#: 'short' or 'long'")`。传别的（例如 `"10s"`）在真机上**每次都会抛异常**，而这里
#: 又把异常吞成"通知失败"的日志 —— 表现就是"Windows 通知一直没弹、也不报错"，极难查。
#: （CI 的 Windows 运行器就是这么把它抓出来的，见 tests/test_notify_channels.py。）
WINOTIFY_DURATIONS: tuple[str, ...] = ("short", "long")

#: 默认档位：`short` ≈ 7 秒，盯盘提醒够看清又不至于赖在屏幕上
DEFAULT_DURATION = "short"

#: 换算后多长算"长时间"（秒）：winotify 的 `long` ≈ 25 秒
_LONG_SECONDS = 20.0


def _to_seconds(text: str) -> float | None:
    """`"10s"` / `"8000ms"` / `"10"` → 秒数；认不出来返回 None。"""
    import re

    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(ms|毫秒|s|sec|secs|秒)?", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "s"
    return value / 1000.0 if unit in ("ms", "毫秒") else value


def normalize_duration(value: Any) -> str:
    """把各种"时长"写法归一化成 winotify 认的 `"short"` / `"long"`。

    为什么需要这一层：winotify **只接受这两个字符串**，其余一律
    `ValueError: Duration is not 'short' or 'long'`；而"时长"这事在人的脑子里、
    在配置里天然写成 `10s` / `8000ms` / `25`。所以**在进 winotify 之前**统一换算，
    非法值一律回到默认档位，绝不透传。

    规则：
        - `"short"` / `"long"`（大小写不敏感、允许空格）→ 原样；
        - `"8000ms"` / `"8000 毫秒"` → 按毫秒算；
        - `"10s"` / `"10"` / `10` / `10.5` → 按秒算；
        - 空串 / None / 认不出来的 → `short`；
        - 换算出**秒数 ≥ 20** → `long`，否则 `short`。
    """
    if value is None:
        return DEFAULT_DURATION
    text = str(value).strip().lower()
    if not text:
        return DEFAULT_DURATION
    if text in WINOTIFY_DURATIONS:
        return text
    seconds = _to_seconds(text)
    if seconds is None:
        return DEFAULT_DURATION
    return "long" if seconds >= _LONG_SECONDS else "short"


def to_xueqiu_code(code: str) -> str:
    """纯数字代码 → 雪球格式：6 开头→SH，4/8 开头→BJ，其余→SZ（与服务器版一致）。"""
    code = (code or "").strip()
    if code.startswith("6"):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def first_symbol(title: str, lines: list[str]) -> str:
    """从正文里挑出第一个可跳转的 6 位股票代码（标题里的数字不算）。"""
    import re

    for line in lines:
        match = re.search(r"\b(\d{6})\b", str(line))
        if match:
            return match.group(1)
    return ""


def toast_icon() -> str:
    """Toast 左上角的小图标：**必须是绝对路径**（winotify 会把它交给系统去读盘，
    相对路径在打包后的工作目录里会找不到）。

    找不到图标就返回空串 —— winotify 用默认图标，不影响弹窗。
    """
    try:
        from laoa_trader import assets

        path = assets.icon_png(size=48) or assets.icon_png()
    except Exception:  # noqa: BLE001 - 图标是"锦上添花"，绝不该拖垮通知
        return ""
    return str(path.resolve()) if path else ""


def notify(
    title: str,
    lines: list[str],
    cfg: Any = None,
    duration: Any = None,
) -> dict:
    """弹一条 Windows 原生通知。

    频道参数（config.toml）：
        - `notify_windows_sound`：是否带提示音（默认 True；开会/盯盘时可关掉）；
        - `notify_windows_open_url`：点击按钮是否打开雪球（默认 True）。

    Args:
        title: 通知标题。
        lines: 正文行（**最多取前 8 行**，多了系统会截断）。
        cfg: 配置。
        duration: 期望的展示时长。可以是 `"short"` / `"long"`，也可以是
            `"10s"` / `"8000ms"` / `25` 这类写法 —— 经 `normalize_duration()`
            换算成 winotify 认的档位（**它只认 short/long 两个值**）。

    Returns:
        {"kind": "windows", "ok": bool, "detail": str}
        非 Windows 平台返回 `ok=False, supported=False`（**调用方不应视为错误**）。
    """
    # 配置开关先判：用户主动关掉时报"已关闭"，比报"平台不支持"更准确
    if cfg is not None and not getattr(cfg, "notify_windows", True):
        return {"kind": "windows", "ok": True, "skipped": True, "detail": "已在配置中关闭"}
    if not SUPPORTED:
        return {"kind": "windows", "ok": False, "supported": False,
                "detail": "当前平台不支持 Windows 原生通知"}

    try:
        from winotify import Notification, audio  # type: ignore[import-not-found]
    except ImportError as exc:
        # 不是错误：非 Windows 或用户没装 winotify，静默降级
        logger.info(f"winotify 不可用，跳过 Windows 通知：{exc}")
        return {"kind": "windows", "ok": False, "supported": False,
                "detail": "未安装 winotify（非 Windows 平台正常现象）"}

    with_sound = bool(getattr(cfg, "notify_windows_sound", True)) if cfg is not None else True
    with_url = bool(getattr(cfg, "notify_windows_open_url", True)) if cfg is not None else True
    # 进 winotify 之前先归一化：它只认 "short"/"long"，别的值会直接抛异常
    shown_for = normalize_duration(duration)
    if str(duration).strip().lower() not in WINOTIFY_DURATIONS and duration is not None:
        logger.debug(f"通知时长 {duration!r} 已换算成 winotify 档位 {shown_for!r}")

    body = "\n".join(str(line) for line in lines[:8])  # Toast 正文太长会被系统截断
    try:
        toast = Notification(
            app_id=APP_ID, title=title, msg=body or "（无内容）", duration=shown_for,
            icon=toast_icon(),
        )
        symbol = first_symbol(title, lines)
        if with_url and symbol:
            toast.add_actions(
                label="打开雪球", launch=f"https://xueqiu.com/S/{to_xueqiu_code(symbol)}"
            )
        if with_sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
        detail = "已弹出通知" + ("" if with_sound else "（静音）")
        return {"kind": "windows", "ok": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - 弹窗失败不该影响其它频道
        logger.warning(f"Windows 通知失败：{exc}")
        return {"kind": "windows", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
