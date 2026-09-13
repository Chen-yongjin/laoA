"""三路通知：并行、互不影响、缺凭证优雅降级、配置可关。"""

from __future__ import annotations

import pytest

from laoa_trader.notify import KINDS, feishu, notify_all, summarize, tray, windows
from tests.conftest import FakeResponse, FakeSession


# ── 统一入口 ──


def test_notify_all_returns_per_channel_results(cfg) -> None:
    """三路都要有结果（缺凭证/平台不支持也是"有结果"，不是异常）。"""
    cfg.notify_tray = True
    results = notify_all("测试标题", ["第一行", "第二行"], cfg=cfg)
    assert set(results) == set(KINDS)
    for kind, res in results.items():
        assert res["kind"] == kind
        assert "ok" in res
    # 飞书没配凭证 → 跳过（不算失败）
    assert results["feishu"]["ok"] is True
    assert results["feishu"]["skipped"] is True
    assert "未配置" in results["feishu"]["detail"]


def test_notify_all_windows_degrades_on_non_windows(cfg) -> None:
    """非 Windows 平台必须优雅降级为"不支持"，而不是抛 ImportError。"""
    results = notify_all("标题", ["正文"], kinds=("windows",), cfg=cfg)
    if windows.SUPPORTED:  # pragma: no cover - 只在 Windows 上走这里
        assert "ok" in results["windows"]
    else:
        assert results["windows"]["ok"] is False
        assert results["windows"]["supported"] is False
        assert "不支持" in results["windows"]["detail"]


def test_notify_all_never_raises_when_channel_explodes(cfg, monkeypatch) -> None:
    """任一路失败（甚至抛异常）都不影响其它两路。"""
    def boom(*args, **kwargs):
        raise RuntimeError("飞书炸了")

    monkeypatch.setattr(feishu, "notify", boom)
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert results["feishu"]["ok"] is False
    assert "飞书炸了" in results["feishu"]["detail"]
    # 托盘不受影响
    assert results["tray"]["ok"] is True


def test_notify_all_respects_enabled_switches(cfg) -> None:
    cfg.notify_feishu = False
    cfg.notify_windows = False
    cfg.notify_tray = False
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert all(res["skipped"] is True for res in results.values())
    assert all(res["ok"] is True for res in results.values())


def test_notify_all_ignores_unknown_kinds(cfg) -> None:
    assert notify_all("标题", ["正文"], kinds=("telegram",), cfg=cfg) == {}


def test_notify_all_runs_channels_in_parallel(cfg, monkeypatch) -> None:
    """三路是**并行**的：慢的一路不该拖住其它路（这里用 0.3s 的假延迟验证）。"""
    import time

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return {"kind": "feishu", "ok": True, "detail": "慢"}

    monkeypatch.setattr(feishu, "notify", slow)
    monkeypatch.setattr(windows, "notify", slow)
    monkeypatch.setattr(tray, "notify", slow)
    start = time.monotonic()
    results = notify_all("标题", ["正文"], cfg=cfg)
    elapsed = time.monotonic() - start
    assert len(results) == 3
    assert elapsed < 0.85, f"三路并行应远快于串行的 0.9s，实际 {elapsed:.2f}s"


def test_summarize(cfg) -> None:
    results = notify_all("标题", ["正文"], cfg=cfg)
    text = summarize(results)
    assert "飞书" in text and "托盘" in text


# ── 飞书 ──


def test_feishu_skips_silently_without_credentials(cfg) -> None:
    cfg.feishu_app_id = cfg.feishu_app_secret = ""
    result = feishu.notify("标题", ["正文"], cfg=cfg)
    assert result["ok"] is True
    assert result["skipped"] is True


def test_feishu_card_payload_shape(cfg) -> None:
    """卡片结构：header(plain_text) + elements(div/lark_md)，与服务器版同款。"""
    card = feishu.build_card("📈 选股池", ["1. 甲（600001）", "2. 乙（600002）"])
    assert card["header"]["title"]["content"] == "📈 选股池"
    assert card["header"]["template"] == "blue"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert "600001" in card["elements"][0]["text"]["content"]


def test_feishu_sends_message_with_json_string_content(cfg) -> None:
    """im/v1/messages 的 content 必须是**JSON 字符串**（服务器版踩过的坑）。"""
    import json

    cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_chat_id = "app", "secret", "chat1"
    session = FakeSession({
        "/auth/v3/tenant_access_token/internal": FakeResponse(
            {"code": 0, "tenant_access_token": "t-123", "expire": 7200}),
        "/im/v1/messages": FakeResponse({"code": 0, "data": {"message_id": "m1"}}),
    })
    result = feishu.notify("标题", ["正文一", "正文二"], cfg=cfg, session=session)
    assert result["ok"] is True

    url, payload = [c for c in session.calls if "im/v1/messages" in c[0]][0]
    assert payload["receive_id"] == "chat1"
    assert payload["msg_type"] == "interactive"
    card = json.loads(payload["content"])          # 必须是字符串，且能解析成卡片
    assert card["header"]["title"]["content"] == "标题"


def test_feishu_reports_failure_without_raising(cfg) -> None:
    cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_chat_id = "app", "secret", "chat1"
    session = FakeSession({
        "/auth/v3/tenant_access_token/internal": FakeResponse(
            {"code": 0, "tenant_access_token": "t-123", "expire": 7200}),
        "/im/v1/messages": FakeResponse({"code": 9499, "msg": "无权限"}),
    })
    result = feishu.notify("标题", ["正文"], cfg=cfg, session=session)
    assert result["ok"] is False
    assert "9499" in result["detail"]


def test_feishu_token_failure_is_reported(cfg) -> None:
    cfg.feishu_app_id, cfg.feishu_app_secret = "app", "bad"
    session = FakeSession({
        "/auth/v3/tenant_access_token/internal": FakeResponse(
            {"code": 10003, "msg": "invalid app_secret"}),
    })
    result = feishu.notify("标题", ["正文"], cfg=cfg, session=session)
    assert result["ok"] is False
    assert "tenant_access_token" in result["detail"]


def test_feishu_token_is_cached(cfg) -> None:
    cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_chat_id = "app", "secret", "chat1"
    session = FakeSession({
        "/auth/v3/tenant_access_token/internal": FakeResponse(
            {"code": 0, "tenant_access_token": "t-123", "expire": 7200}),
        "/im/v1/messages": FakeResponse({"code": 0, "data": {}}),
    })
    notifier = feishu.FeishuAppNotifier(cfg=cfg, session=session)
    notifier.send_card("标题1", ["正文"])
    notifier.send_card("标题2", ["正文"])
    token_calls = [c for c in session.calls if "tenant_access_token" in c[0]]
    assert len(token_calls) == 1     # 第二次复用缓存，不再换 token


def test_feishu_discovers_chat_when_not_configured(cfg) -> None:
    cfg.feishu_app_id, cfg.feishu_app_secret, cfg.feishu_chat_id = "app", "secret", ""
    session = FakeSession({
        "/auth/v3/tenant_access_token/internal": FakeResponse(
            {"code": 0, "tenant_access_token": "t-1", "expire": 7200}),
        "/im/v1/chats": FakeResponse({"code": 0, "data": {
            "items": [{"chat_id": "auto-chat", "name": "选股群"}], "has_more": False}}),
        "/im/v1/messages": FakeResponse({"code": 0, "data": {}}),
    })
    result = feishu.notify("标题", ["正文"], cfg=cfg, session=session)
    assert result["ok"] is True
    assert session.calls[-1][1]["receive_id"] == "auto-chat"


# ── Windows 通知 ──


def test_windows_notify_returns_unsupported_off_platform() -> None:
    result = windows.notify("标题", ["正文"])
    if windows.SUPPORTED:  # pragma: no cover
        assert "ok" in result
    else:
        assert result["supported"] is False
        assert result["ok"] is False


def test_windows_notify_respects_config_switch(cfg) -> None:
    cfg.notify_windows = False
    result = windows.notify("标题", ["正文"], cfg=cfg)
    assert result["skipped"] is True


def test_xueqiu_code_mapping() -> None:
    assert windows.to_xueqiu_code("600519") == "SH600519"
    assert windows.to_xueqiu_code("000001") == "SZ000001"
    assert windows.to_xueqiu_code("830799") == "BJ830799"


def test_first_symbol_picks_from_lines() -> None:
    assert windows.first_symbol("标题", ["1. 甲（600519）低价股"]) == "600519"
    assert windows.first_symbol("标题", ["没有代码"]) == ""


# ── 托盘 ──


def test_tray_queues_and_drains(cfg) -> None:
    tray.clear()
    result = tray.notify("标题", ["正文一", "正文二"], cfg=cfg)
    assert result["ok"] is True
    messages = tray.drain()
    assert len(messages) == 1
    assert messages[0]["title"] == "标题"
    assert "正文一" in messages[0]["body"]
    assert tray.pending() == 0


def test_tray_drain_is_empty_when_nothing_queued() -> None:
    tray.clear()
    assert tray.drain() == []


def test_tray_respects_config_switch(cfg) -> None:
    tray.clear()
    cfg.notify_tray = False
    result = tray.notify("标题", ["正文"], cfg=cfg)
    assert result["skipped"] is True
    assert tray.drain() == []


def test_tray_queue_is_bounded(cfg) -> None:
    """队列满时丢最旧的，绝不阻塞调度线程。"""
    from laoa_trader.notify import tray as tray_mod

    tray.clear()
    for i in range(tray_mod.MAX_QUEUE + 20):
        tray_mod.notify(f"标题{i}", ["正文"], cfg=cfg)
    assert tray_mod.pending() <= tray_mod.MAX_QUEUE
    messages = tray_mod.drain(limit=tray_mod.MAX_QUEUE + 10)
    # 最新的一条一定还在（丢的是最旧的）
    assert messages[-1]["title"] == f"标题{tray_mod.MAX_QUEUE + 19}"
    tray.clear()


def test_tray_bind_never_raises_on_dummy_object(cfg) -> None:
    class Dummy:
        def showMessage(self, *args):   # noqa: N802 - 模拟 Qt 接口
            raise RuntimeError("托盘已销毁")

    tray.bind(Dummy())
    try:
        result = tray.notify("标题", ["正文"], cfg=cfg)   # 不该抛
        assert result["ok"] is True
    finally:
        tray.unbind()
        tray.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_every_channel_returns_dict_with_kind(kind: str, cfg) -> None:
    assert notify_all("标题", ["正文"], kinds=(kind,), cfg=cfg)[kind]["kind"] == kind
