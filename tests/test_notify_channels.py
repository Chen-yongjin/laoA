"""通知频道自选：`notify_channels` 生效、各频道参数生效、频道间互不影响。

覆盖需求「三、通知方式自己设定」：
- `notify_channels` 决定发哪几个；**空列表 = 一个都不发**（只入库）；
- 只勾飞书时，托盘**根本不会被调用**（反之亦然）；
- 某频道抛错只记日志，其余频道照常；
- 飞书没配凭证时：明确提示「未配置飞书凭证，已跳过」，不报错、不影响其它频道；
- 每个频道的参数（气泡时长）真的传下去了；
- **已删除的 `windows` 频道**：老配置里还写着它时不报错，`channel_states()` 里如实说"该频道已删除"。
"""

from __future__ import annotations

import pytest

from laoa_trader.notify import KINDS, feishu, notify_all, summarize, summarizes_channels, tray


@pytest.fixture()
def spy(monkeypatch):
    """把两个频道都换成"记录调用"的假实现，用来断言**谁被调用了**。

    2026-09-18：Windows 系统通知整路删除，所以这里只剩飞书与托盘两个。
    """
    calls: dict[str, list] = {"feishu": [], "tray": []}
    results: dict[str, dict] = {}

    def make(name):
        def _fake(title, lines, cfg=None, **kwargs):
            calls[name].append({"title": title, "lines": lines, "cfg": cfg})
            return results.get(name) or {"kind": name, "ok": True, "detail": f"{name} 已发送"}
        return _fake

    monkeypatch.setattr(feishu, "notify", make("feishu"))
    monkeypatch.setattr(tray, "notify", make("tray"))
    return calls, results


# ── 频道选择 ──


def test_empty_channels_sends_nothing(spy, cfg) -> None:
    """空列表 = 只入库不推送：一个频道都不发，且结果显示"跳过"。"""
    calls, _ = spy
    cfg.notify_channels = []
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls == {"feishu": [], "tray": []}
    assert set(results) == set(KINDS)
    assert all(r["skipped"] for r in results.values())
    assert all("notify_channels" in r["detail"] for r in results.values())
    assert "不推送" in summarizes_channels(cfg)


def test_only_tray_never_calls_feishu(spy, cfg) -> None:
    """只勾托盘时，飞书**根本不会被调用**（互不影响）。"""
    calls, _ = spy
    cfg.notify_channels = ["tray"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["tray"] and not calls["feishu"]
    assert results["feishu"]["skipped"] is True
    assert results["tray"]["ok"] is True


def test_only_feishu(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["feishu"]
    cfg.feishu_app_id = cfg.feishu_app_secret = "x"
    notify_all("标题", ["正文"], cfg=cfg)
    assert calls["feishu"] and not calls["tray"]


def test_unknown_channel_names_are_ignored(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["feishu", "telegram"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["feishu"]
    assert set(results) == set(KINDS)          # 未知名字不会变成"多发一路"


def test_result_order_is_stable_regardless_of_config_order(spy, cfg) -> None:
    """返回顺序固定为 KINDS 顺序（界面/日志显示稳定），配置顺序只决定"启用谁"。"""
    calls, _ = spy
    cfg.notify_channels = ["tray", "feishu"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert list(results) == list(KINDS)
    active = [name for name, res in results.items() if not res.get("skipped")]
    assert set(active) == {"tray", "feishu"}
    assert calls["tray"] and calls["feishu"]


def test_feishu_on_switch(spy, cfg) -> None:
    """`feishu_on = false` → 飞书被跳过（**根本不会被调用**），托盘照常。"""
    calls, _ = spy
    cfg.notify_channels = ["feishu", "tray"]
    cfg.feishu_on = False
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["feishu"] == []                     # 没被调用
    assert calls["tray"]                             # 托盘照常
    assert results["feishu"]["skipped"] is True
    assert "feishu_on" in results["feishu"]["detail"]


def test_legacy_single_channel_switch_still_works(spy, cfg) -> None:
    """老的 notify_feishu/notify_tray 单频道开关继续生效（兼容旧配置）。"""
    calls, _ = spy
    cfg.notify_channels = ["feishu", "tray"]
    cfg.notify_feishu = False
    cfg.feishu_app_id = cfg.feishu_app_secret = "x"
    notify_all("标题", ["正文"], cfg=cfg)
    assert not calls["feishu"]
    assert calls["tray"]


def test_channels_property_and_states(cfg) -> None:
    cfg.notify_channels = ["feishu", "tray"]
    assert cfg.channels == ["feishu", "tray"]
    cfg.notify_tray = False
    assert cfg.channels == ["feishu"]
    cfg.notify_channels = []
    assert cfg.channels == []
    cfg.notify_tray = True
    cfg.notify_channels = ["feishu", "tray"]
    states = cfg.channel_states()
    assert states["tray"] == "启用"
    assert "未配置飞书凭证" in states["feishu"]


def test_removed_windows_channel_is_reported_not_crashed(cfg) -> None:
    """老配置里还写着 `"windows"`（2026-09-18 已删除）→ **不报错**，而且如实说它没了。

    用户原话："windows系统通知删除，太骚扰了，影响体验。" —— 但别人的 config.toml
    里可能还留着这个词，加载不能炸、也不能让人以为它还在发。
    """
    cfg.notify_channels = ["windows", "tray"]

    assert cfg.channels == ["tray"]                     # 已删除的频道被忽略
    states = cfg.channel_states()
    assert "已删除" in states["windows"]
    assert states["tray"] == "启用"


# ── 缺凭证 / 互不影响 ──


def test_feishu_without_credentials_skips_quietly(cfg) -> None:
    """勾了飞书但没凭证：明确提示、ok=True、不影响其它频道。"""
    cfg.notify_channels = ["feishu", "tray"]
    cfg.feishu_app_id = cfg.feishu_app_secret = ""
    tray.clear()
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert results["feishu"]["skipped"] is True
    assert results["feishu"]["ok"] is True
    assert results["feishu"]["detail"] == "未配置飞书凭证，已跳过"
    # 托盘照常
    assert results["tray"]["ok"] is True
    assert tray.pending() == 1
    tray.clear()


def test_one_channel_failure_does_not_affect_others(cfg, monkeypatch) -> None:
    """飞书炸了 → 托盘照常（并行 + 各自 try）。"""
    cfg.notify_channels = ["feishu", "tray"]

    def boom(*a, **k):
        raise RuntimeError("飞书通道炸了")

    monkeypatch.setattr(feishu, "notify", boom)
    tray.clear()
    monkeypatch.setattr(tray, "notify",
                        lambda *a, **k: {"kind": "tray", "ok": True, "detail": "投递"})
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert results["feishu"]["ok"] is False
    assert "飞书通道炸了" in results["feishu"]["detail"]
    assert results["tray"]["ok"] is True


def test_every_channel_timeout_is_isolated(cfg, monkeypatch) -> None:
    """某频道卡死也不能拖住其它频道（各自超时）。"""
    import time

    cfg.notify_channels = ["feishu", "tray"]

    def slow(*a, **k):
        time.sleep(0.3)
        return {"kind": "feishu", "ok": True, "detail": "慢"}

    monkeypatch.setattr(feishu, "notify", slow)
    monkeypatch.setattr(tray, "notify",
                        lambda *a, **k: {"kind": "tray", "ok": True, "detail": "快"})
    start = time.monotonic()
    results = notify_all("标题", ["正文"], cfg=cfg)
    elapsed = time.monotonic() - start
    assert results["tray"]["ok"] is True
    assert elapsed < 0.6, f"两路应并行（串行会 ~0.3s+），实际 {elapsed:.2f}s"


# ── 各频道参数 ──


# ── Windows 通知那一整路的用例**全部删除**（2026-09-18）──
#
# 用户原话："windows系统通知删除，太骚扰了，影响体验。" —— `notify/windows.py`
# 连同 `winotify` 那套实现一起删了，这一段原来的 9 条用例（假 Toast 校验 duration 档位、
# 提示音/打开雪球参数透传、非 Windows 降级、配置关掉…）失去了被测对象，所以整体删除。
# **没有放宽任何断言**：留下的用例仍然钉着"频道选择、单频道开关、缺凭证跳过、
# 某路炸掉不影响其它路、托盘参数透传"这些真实行为。
#
# 顺手保留的教训（写在文档里）：`duration` 只能传 winotify 认的那两档 ——
# 当年这个真机 bug（默认 `"10s"` 被吞成"通知失败"）只在 Windows 上才复现。


def test_tray_duration_from_config(cfg) -> None:
    tray.clear()
    cfg.notify_channels = ["tray"]
    cfg.notify_tray_duration_ms = 12345
    result = tray.notify("标题", ["正文"], cfg=cfg)
    assert "12345" in result["detail"]
    assert tray.drain()[0]["duration_ms"] == 12345


def test_tray_duration_env_override(monkeypatch, tmp_path) -> None:
    """环境变量也能调（临时静音/延长不用改文件）。"""
    from laoa_trader.config import load_config

    monkeypatch.setenv("NOTIFY_TRAY_DURATION_MS", "2000")
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.notify_tray_duration_ms == 2000


def test_summarize_and_channel_hint(cfg) -> None:
    """一行摘要里写出**当前这两路**的状态（Windows 那一路已经删除，不再出现）。"""
    cfg.notify_channels = ["feishu", "tray"]
    cfg.feishu_app_id = cfg.feishu_app_secret = "x"
    text = summarizes_channels(cfg)
    assert "feishu" in text and "tray" in text and "windows" not in text
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert "飞书" in summarize(results) and "托盘" in summarize(results)
    assert "弹窗" not in summarize(results)          # 那条路没了
