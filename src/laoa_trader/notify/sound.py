"""提醒提示音：只用 Python 标准库 `winsound`，不引任何新依赖。

为什么单独一个模块
------------------
1. **声音是锦上添花，绝不能反过来影响提醒**：声卡被禁用、系统声音别名被改过、
   驱动报错 —— 这些都不该让"入库 / 浮窗 / 飞书"任何一路失败。所以这里
   任何异常都只在日志里留一行（`debug`），对外只返回 `True/False`，**不抛异常**。
2. **非 Windows 静默跳过**：开发机与 CI 跑在 Linux 上，`winsound` 模块根本不存在，
   import 就会 `ImportError`。所以 import 放在函数里，并且先用 `sys.platform` 挡一道
   —— 不能靠 `try: import winsound` 兜底（那会让"忘了装依赖"和"平台不对"混成一团）。
3. **只用系统别名，不打包 wav**：`SystemAsterisk` 这类别名由用户在
   「控制面板 → 声音」里自己指定，我们跟着用户的设置走，既符合习惯，
   也不用往安装包里塞音频文件、更不用担心版权。

为什么要 `SND_ASYNC`：同步播放会把调用它的线程按住几百毫秒。提醒是在
界面线程（用户刚点了按钮 / 定时器到点）里触发的，同步播放就是"点一下卡一下"。
"""

from __future__ import annotations

import sys

from laoa_trader.log import get_logger

logger = get_logger(__name__)

#: 默认提示音：系统"星号"提示音（比 `SystemHand`、`SystemExclamation` 温和，
#: 属于"有件事发生了"而不是"出错了"，符合盘中提醒的语义）
DEFAULT_ALIAS = "SystemAsterisk"


def available() -> bool:
    """当前平台能不能放提示音（非 Windows 恒为 False）。"""
    return sys.platform == "win32"


def play(alias: str = DEFAULT_ALIAS) -> bool:
    """放一声系统提示音。

    Args:
        alias: `winsound` 的系统声音别名（`SystemAsterisk` / `SystemExclamation` …）。

    Returns:
        真的调用了播放接口 → True；非 Windows、`winsound` 缺失、播放报错 → False。
        **无论哪种情况都不会抛异常**（声音坏了也得把提醒弹出来）。
    """
    if not available():
        # 静默跳过：Linux/macOS 上跑测试时这一步什么都不做（也就不会有声音）
        return False
    try:
        import winsound  # noqa: PLC0415 - 只在 Windows 上才导入
    except Exception as exc:  # noqa: BLE001 - 精简环境可能没有 winsound
        logger.debug(f"当前环境没有 winsound，跳过提示音：{exc}")
        return False
    try:
        winsound.PlaySound(str(alias), winsound.SND_ALIAS | winsound.SND_ASYNC)
        return True
    except Exception as exc:  # noqa: BLE001 - 别名不存在/驱动异常都不该冒泡
        logger.debug(f"提示音播放失败（不影响提醒）：{exc}")
        return False
