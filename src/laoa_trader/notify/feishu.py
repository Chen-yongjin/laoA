"""飞书推送（自建应用）。

移植自服务器版 `sequoia_x/notify/feishu_app.py`：**token 获取与 `im/v1/messages`
调用逐行一致**（含"content 必须是 JSON 字符串"这个坑），卡片构造按桌面版的内容
（标题 + 正文行）适配。

自建应用 vs 自定义机器人 Webhook：

| 方式 | 凭证 | 调用 |
|---|---|---|
| 自定义机器人 Webhook | 一个群一个 hook URL | 直接 POST 卡片 |
| 自建应用（本模块） | App ID + App Secret | 先换 tenant_access_token，再调 im/v1/messages |

桌面版选自建应用：单机程序里放一个"群机器人 hook URL"泄露面更大，
而 App ID/Secret 可以随时在飞书后台吊销。

凭证没配齐时**静默跳过**（返回 skipped，不算失败）—— 用户可能只想用 Windows 弹窗。
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from laoa_trader.config import Config, get_config
from laoa_trader.log import get_logger

logger = get_logger(__name__)

_BASE = "https://open.feishu.cn/open-apis"


def build_card(title: str, lines: list[str]) -> dict:
    """构造交互卡片（与服务器版同一套外层结构：header + div(lark_md)）。"""
    body = "\n".join(str(line) for line in lines) if lines else "（无内容）"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
        ],
    }


class FeishuAppNotifier:
    """基于自建应用凭证的飞书推送器。

    Args:
        cfg: 配置；缺省走全局配置。
        session: 可注入的 requests.Session（测试用假会话，不需要真网络）。
    """

    def __init__(self, cfg: Config | None = None, session: Any = None) -> None:
        cfg = cfg or get_config()
        self.cfg = cfg
        self.app_id = (cfg.feishu_app_id or "").strip()
        self.app_secret = (cfg.feishu_app_secret or "").strip()
        self.receive_id_type = (cfg.receive_id_type or "chat_id").strip()
        self.default_target = (cfg.feishu_chat_id or "").strip()
        self.session = session or requests.Session()
        self._token: str = ""
        self._token_expire_at: float = 0.0

    @property
    def ready(self) -> bool:
        """凭证是否配齐（不实调远端）。"""
        return bool(self.app_id and self.app_secret)

    # ── 凭证 ──

    def _get_token(self) -> str:
        """获取（带缓存的）tenant_access_token。"""
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token

        resp = self.session.post(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(
                f"获取 tenant_access_token 失败：code={data.get('code')} msg={data.get('msg')}"
            )
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200))
        logger.debug("tenant_access_token 已刷新")
        return self._token

    # ── 目标会话 ──

    def _resolve_target(self, token: str) -> str:
        """决定这条消息发到哪个会话：显式 chat_id 优先，否则自动取机器人所在的第一个群。"""
        if self.default_target:
            return self.default_target
        headers = {"Authorization": f"Bearer {token}"}
        chats: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, str | int] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.session.get(
                f"{_BASE}/im/v1/chats", headers=headers, params=params, timeout=10
            ).json()
            if data.get("code") != 0:
                raise RuntimeError(
                    f"获取机器人所在群失败：code={data.get('code')} msg={data.get('msg')}"
                )
            page = data.get("data", {})
            chats.extend(page.get("items", []))
            if not page.get("has_more"):
                break
            page_token = page.get("page_token", "")
        if not chats:
            raise RuntimeError(
                "机器人尚未加入任何群：请在飞书里把机器人拉进目标群，"
                "或显式设置 feishu_chat_id（配合 receive_id_type）"
            )
        return chats[0]["chat_id"]

    # ── 发送 ──

    def _post(self, payload: dict) -> tuple[int, dict]:
        token = self._get_token()
        target = self._resolve_target(token)
        resp = self.session.post(
            f"{_BASE}/im/v1/messages",
            params={"receive_id_type": self.receive_id_type},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=json.dumps({**payload, "receive_id": target}, ensure_ascii=False),
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data

    def send_card(self, title: str, lines: list[str]) -> dict:
        """发一张交互卡片。**不抛异常**，失败只记 ERROR 并返回结构化结果。"""
        if not self.ready:
            logger.info("未配置飞书凭证，跳过飞书推送")
            return {"kind": "feishu", "ok": True, "skipped": True,
                    "detail": "未配置飞书凭证，已跳过"}
        try:
            card = build_card(title, lines)
            # 注意载荷差异：Webhook 发的是 {"msg_type":..., "card":{...}}，
            # 而 im/v1/messages 的 content 只接受**内层卡片对象本身**的 JSON 字符串
            status, data = self._post({
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            })
            if status != 200 or data.get("code") != 0:
                msg = f"HTTP {status} code={data.get('code')} msg={data.get('msg')}"
                logger.error(f"飞书推送失败：{msg}")
                return {"kind": "feishu", "ok": False, "detail": msg}
            logger.info(f"飞书推送成功：{title}")
            return {"kind": "feishu", "ok": True, "detail": "已发送卡片"}
        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常：{exc}")
            return {"kind": "feishu", "ok": False, "detail": f"网络异常：{exc}"}
        except Exception as exc:  # noqa: BLE001 - 凭证/目标解析等异常同样不中断主流程
            logger.error(f"飞书推送异常：{exc}")
            return {"kind": "feishu", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    def send_text(self, title: str, lines: list[str]) -> dict:
        """发纯文本（运维告警用）。"""
        if not self.ready:
            return {"kind": "feishu", "ok": True, "skipped": True,
                    "detail": "未配置飞书凭证，已跳过"}
        try:
            status, data = self._post({
                "msg_type": "text",
                "content": json.dumps(
                    {"text": title + "\n" + "\n".join(lines)}, ensure_ascii=False
                ),
            })
            if status != 200 or data.get("code") != 0:
                msg = f"HTTP {status} code={data.get('code')} msg={data.get('msg')}"
                logger.error(f"飞书告警发送失败：{msg}")
                return {"kind": "feishu", "ok": False, "detail": msg}
            return {"kind": "feishu", "ok": True, "detail": "已发送文本"}
        except Exception as exc:  # noqa: BLE001
            logger.error(f"飞书告警异常：{exc}")
            return {"kind": "feishu", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def notify(
    title: str,
    lines: list[str],
    cfg: Config | None = None,
    session: Any = None,
) -> dict:
    """一路入口（供 `notify_all` 调用）：发一张卡片。"""
    return FeishuAppNotifier(cfg=cfg, session=session).send_card(title, lines)
