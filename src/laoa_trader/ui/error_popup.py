"""错误弹窗的**唯一出口**：`show_error()`。

主人 2026-10-11 的原话：「不需要那么多解释，提示错误时直接弹窗说明。」
所以从这一天起，界面上的**错误**走弹窗、**常驻的解释性文字**一律删掉：

- 用户主动操作失败（保存设置 / 加自选 / 记持仓 / 开始筛选 / 运行 / 下载 / 导出…）→ **每次都弹**；
- 后台与定时任务失败（下载、日更、盘中取数、快照、评分…）→ 同一类错误**每次运行只弹一次**
  （`once_key` 去重）——否则网线一拔就会每 5 秒糊一个框上来；
- 启动自检失败（本地没数据/数据不够）→ 启动后弹一次，正文里写清"下一步做什么"。

三条纪律（写在这里，别在别处另开一个弹窗）：

1. **全项目只有这一处弹错误框**。`QMessageBox` 的直接调用要么走这里，要么是
   "是/否确认"（删除、覆盖），后者是用户主动触发的选择，不属于"错误提示"。
2. **正文最多三行**，且必须包含"出了什么事 + 下一步做什么"。堆段落正是主人要砍掉的东西。
3. **测试里绝不允许真的弹出模态框**（模态框会卡到超时）：`tests/conftest.py` 里有一个
   autouse 夹具把 `QMessageBox.exec` 与四个静态入口全换成"记一笔就返回"，
   想断言"弹了什么"就用那个夹具记录的 `modal_calls`。

Qt 不可用时（无 PySide6 的精简环境）只写日志、绝不抛 —— 与 `ui/` 其它模块同一个约定。
"""

from __future__ import annotations

from typing import Any

from laoa_trader.log import get_logger

logger = get_logger(__name__)

try:      # pragma: no cover - Qt 缺失时走下面那条日志路
    from PySide6.QtWidgets import QApplication, QMessageBox

    QT_AVAILABLE = True
except Exception:  # noqa: BLE001 - 与 ui/app.py 同一个约定：import 不该炸
    QApplication = None                    # type: ignore[assignment]
    QMessageBox = None                     # type: ignore[assignment]
    QT_AVAILABLE = False

#: 弹窗上那个按钮的字（Qt 默认是英文 "OK"）
BUTTON_TEXT = "知道了"
#: 正文最多几行（主人："不要在弹窗里堆段落"）
MAX_LINES = 3

#: 已经弹过的 `once_key`（**进程级**：后台线程与主线程共用同一份"这类错误报过了"）。
#: 为什么不是每个窗口一份：同一类错误可能从调度器（后台线程）与界面（主线程）两条路来，
#: 各记一份就等于同一件事弹两次。
_once_seen: set[str] = set()


def seen_once_keys() -> set[str]:
    """已经弹过的 `once_key`（测试与排查用）。"""
    return set(_once_seen)


def reset_once_keys() -> None:
    """清空去重记录（测试用：同一个进程里跑多条用例时不互相污染）。"""
    _once_seen.clear()


def _clip(text: Any, *, max_lines: int = MAX_LINES) -> str:
    """正文收紧：一行一句、最多 `max_lines` 行（多的丢掉，完整原因在日志与详情里）。"""
    lines = [line.strip() for line in str(text or "").replace("\r", "").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return "未知错误（完整原因见日志）"
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    kept[-1] = kept[-1].rstrip("。；;") + "…（其余原因见日志）"
    return "\n".join(kept)


def _exec(box: Any) -> int:
    """真正弹出那个模态框。

    **单独一个函数是为了让测试能接管**：模态框会一直等用户点，在 CI 上就是"卡到超时"。
    测试把 `error_popup._exec` 换成"记一笔就返回"（见 `tests/conftest.py`），
    于是 `once_key` 去重、正文收敛这些**真逻辑**照样被执行，只是不真的弹出来。
    """
    return box.exec()


def show_error(parent: Any, title: str, text: str, *, once_key: str = "") -> bool:
    """弹一个"知道了"的错误框 → `是否真的弹了`（被 `once_key` 挡掉时返回 False）。

    Args:
        parent: 父窗口（模态挂在它上面；`None` = 独立窗口）。
        title: 标题，短，例如"下载失败"。
        text: 正文（最多三行）：**出了什么事 + 下一步做什么**。
        once_key: 非空 = 这一类错误"每次运行只弹一次"（后台/定时任务用）。
    """
    if once_key:
        if once_key in _once_seen:
            logger.info(f"【弹窗·已报过】{title}：{_clip(text, max_lines=1)}")
            return False
        _once_seen.add(once_key)
    body = _clip(text)
    logger.warning(f"【弹窗】{title}：{body.replace(chr(10), ' / ')}")
    if not QT_AVAILABLE or QApplication is None or QApplication.instance() is None:
        # 没有 Qt / 还没有 QApplication（CLI、或程序起得比界面还早）：
        # 日志已经写下了原因，这里**绝不**去构造 QWidget —— 那样只会崩或卡住
        return False
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(str(title or "出错了"))
    box.setText(body)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    ok = box.button(QMessageBox.StandardButton.Ok)
    if ok is not None:
        ok.setText(BUTTON_TEXT)
    _exec(box)
    return True
