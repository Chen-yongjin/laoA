"""多频道并行通知：飞书 / 托盘气泡（可任选组合）。

2026-09-18：**Windows 原生通知整路删除**（用户原话："windows系统通知删除，太骚扰了，
影响体验"）—— 它的点击行为本来就不受我们控制，删掉之后提醒走"图标闪烁 + 「消息」列表"。

设计要点
--------
- **频道由配置决定**：`config.toml` 的 `notify_channels = ["feishu","tray"]`，
  **空列表 = 只入库不推送**。每个频道还有自己的开关与参数
  （`feishu_on`、`notify_tray_duration_ms`），老的 `notify_feishu/tray` 单频道开关继续生效。
  老配置里写着 `"windows"` 也不报错：`channels` 会把它当"不认识的频道"忽略掉，
  `channel_states()` 里显示成「该频道已删除」。
- **互相独立、互不影响**：用线程池并发发送，逐路收集结果。
  任一路抛异常（或超时）都不影响其它路 —— 飞书挂了弹窗照出，反之亦然。
- **缺凭证不算失败**：没配飞书凭证时该路返回 `skipped` 并给出中文原因
  （"未配置飞书凭证，已跳过"），`ok=True`，其余频道照常。
- **绝不抛异常**：`notify_all` 只返回每路的结果字典，调用方（GUI / 调度器）
  据此在状态栏提示 —— 通知失败不该让选股流程或界面崩掉。

返回格式（每个被"考虑过"的频道都有一条，便于界面说明"为什么没收到"）：

    {
        "feishu": {"kind": "feishu", "ok": True, "skipped": True, "detail": "未配置飞书凭证，已跳过"},
        "tray":   {"kind": "tray",   "ok": True, "detail": "已投递到托盘"},
    }
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from laoa_trader.config import CHANNELS, Config, get_config
from laoa_trader.log import get_logger
from laoa_trader.notify import feishu, tray

logger = get_logger(__name__)

#: 支持的通知频道（与 `config.CHANNELS` 同一个来源，避免两处漂移）
KINDS: tuple[str, ...] = CHANNELS

#: 单个频道的发送超时（秒）：网络卡住时不能把界面拖死
SEND_TIMEOUT = 20.0


def _skip_reason(kind: str, cfg: Config) -> str | None:
    """该频道这次为什么不该发（None = 该发）。"""
    chosen = {str(c).strip().lower() for c in (cfg.notify_channels or [])}
    if kind not in chosen:
        return "未在 notify_channels 中启用，已跳过"
    if kind == "feishu" and not cfg.feishu_on:
        return "feishu_on = false，已跳过"
    if not getattr(cfg, f"notify_{kind}", True):
        return f"notify_{kind} = false，已跳过"
    # 注意："未配置飞书凭证"由 feishu 频道自己判（它才知道自己缺什么），
    # 这样"某频道真的被调用并抛错"这类路径不会被提前 short-circuit 掉。
    return None


def _send_one(kind: str, title: str, lines: list[str], cfg: Config) -> dict:
    """发送一个频道（内部函数，异常在这里被吃掉）。"""
    try:
        reason = _skip_reason(kind, cfg)
        if reason:
            logger.info(f"{kind} 通知跳过：{reason}")
            return {"kind": kind, "ok": True, "skipped": True, "detail": reason}
        if kind == "feishu":
            return feishu.notify(title, lines, cfg)
        if kind == "tray":
            return tray.notify(title, lines, cfg)
        return {"kind": kind, "ok": True, "skipped": True,
                "detail": "未知频道，已跳过"}
    except Exception as exc:  # noqa: BLE001 - 任何一路炸掉都不影响其它路
        logger.warning(f"{kind} 通知异常：{exc}")
        return {"kind": kind, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def notify_all(
    title: str,
    lines: list[str],
    kinds: tuple[str, ...] | list[str] | None = None,
    cfg: Config | None = None,
) -> dict[str, dict]:
    """并行发送通知，返回每个频道的结果。

    Args:
        title: 标题（例如 "📈 老牛选股助手-选股播报 | 2026-09-13"）。
        lines: 正文行。
        kinds: 只考虑这些频道（默认按配置 `notify_channels`）。
            显式传入时也仍然受各频道自己的开关约束；未识别的名字忽略。
        cfg: 配置；缺省走全局配置。

    Returns:
        {频道名: 结果字典}。**永不抛异常**；`notify_channels = []` 时每个频道
        都是 `skipped`（等于"只入库不推送"）。
    """
    cfg = cfg or get_config()
    # 不传 kinds 时按配置的频道列表；传了就按传的（再过滤掉不认识的名字）
    requested = list(KINDS) if kinds is None else [
        str(k).strip().lower() for k in kinds if str(k).strip().lower() in KINDS
    ]
    if not requested:
        return {}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(requested), thread_name_prefix="notify") as pool:
        futures = {
            pool.submit(_send_one, kind, title, lines, cfg): kind for kind in requested
        }
        for future, kind in futures.items():
            try:
                results[kind] = future.result(timeout=SEND_TIMEOUT)
            except FutureTimeout:
                results[kind] = {"kind": kind, "ok": False, "detail": "发送超时"}
            except Exception as exc:  # noqa: BLE001
                results[kind] = {"kind": kind, "ok": False,
                                 "detail": f"{type(exc).__name__}: {exc}"}

    ok_count = sum(1 for r in results.values() if r.get("ok"))
    logger.info(
        "通知结果：" + "、".join(
            f"{k}={'成功' if v.get('ok') else '失败'}"
            + ("（跳过）" if v.get("skipped") else "")
            for k, v in results.items()
        )
    )
    if ok_count == 0:
        logger.warning(f"全部通知频道都失败：{results}")
    return results


def summarizes_channels(cfg: Config | None = None) -> str:
    """一行说明"现在会怎么通知"（设置面板/自检用）。"""
    cfg = cfg or get_config()
    states = cfg.channel_states()
    enabled = cfg.channels
    if not enabled:
        return "不推送（只入库）"
    parts = [f"{name}（{states[name]}）" for name in CHANNELS if name in enabled]
    return "、".join(parts)


def summarize(results: dict[str, dict]) -> str:
    """把结果整理成一行中文摘要（状态栏提示用）。"""
    labels = {"feishu": "飞书", "tray": "托盘"}
    parts = []
    for kind, res in results.items():
        name = labels.get(kind, kind)
        if res.get("skipped"):
            parts.append(f"{name}已跳过")
        elif res.get("ok"):
            parts.append(f"{name}成功")
        else:
            parts.append(f"{name}失败")
    return "、".join(parts) if parts else "未启用任何通知频道"


__all__ = ["KINDS", "notify_all", "summarize", "summarizes_channels",
           "feishu", "tray"]
