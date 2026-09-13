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


def notify(
    title: str,
    lines: list[str],
    cfg: Any = None,
    duration: str = "10s",
) -> dict:
    """弹一条 Windows 原生通知。

    频道参数（config.toml）：
        - `notify_windows_sound`：是否带提示音（默认 True；开会/盯盘时可关掉）；
        - `notify_windows_open_url`：点击按钮是否打开雪球（默认 True）。

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

    body = "\n".join(str(line) for line in lines[:8])  # Toast 正文太长会被系统截断
    try:
        toast = Notification(
            app_id=APP_ID, title=title, msg=body or "（无内容）", duration=duration
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
