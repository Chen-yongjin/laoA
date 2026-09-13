"""通知频道自选：`notify_channels` 生效、各频道参数生效、频道间互不影响。

覆盖需求「三、通知方式自己设定」：
- `notify_channels` 决定发哪几个；**空列表 = 一个都不发**（只入库）；
- 只勾 windows 时，飞书**根本不会被调用**；
- 某频道抛错只记日志，其余频道照常；
- 飞书没配凭证时：明确提示「未配置飞书凭证，已跳过」，不报错、不影响其它频道；
- 每个频道的参数（提示音/打开雪球/气泡时长）真的传下去了。
"""

from __future__ import annotations

import pytest

from laoa_trader.notify import KINDS, feishu, notify_all, summarize, summarizes_channels, tray, windows


@pytest.fixture()
def spy(monkeypatch):
    """把三个频道都换成"记录调用"的假实现，用来断言**谁被调用了**。"""
    calls: dict[str, list] = {"windows": [], "feishu": [], "tray": []}
    results: dict[str, dict] = {}

    def make(name):
        def _fake(title, lines, cfg=None, **kwargs):
            calls[name].append({"title": title, "lines": lines, "cfg": cfg})
            return results.get(name) or {"kind": name, "ok": True, "detail": f"{name} 已发送"}
        return _fake

    monkeypatch.setattr(windows, "notify", make("windows"))
    monkeypatch.setattr(feishu, "notify", make("feishu"))
    monkeypatch.setattr(tray, "notify", make("tray"))
    return calls, results


# ── 频道选择 ──


def test_empty_channels_sends_nothing(spy, cfg) -> None:
    """空列表 = 只入库不推送：三个频道都不发，且结果显示"跳过"。"""
    calls, _ = spy
    cfg.notify_channels = []
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls == {"windows": [], "feishu": [], "tray": []}
    assert set(results) == set(KINDS)
    assert all(r["skipped"] for r in results.values())
    assert all("notify_channels" in r["detail"] for r in results.values())
    assert "不推送" in summarizes_channels(cfg)


def test_only_windows_never_calls_feishu(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["windows"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["windows"] and not calls["feishu"] and not calls["tray"]
    assert results["feishu"]["skipped"] is True
    assert results["windows"]["ok"] is True


def test_only_feishu(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["feishu"]
    cfg.feishu_app_id = cfg.feishu_app_secret = "x"
    notify_all("标题", ["正文"], cfg=cfg)
    assert calls["feishu"] and not calls["windows"] and not calls["tray"]


def test_unknown_channel_names_are_ignored(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["windows", "telegram"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["windows"]
    assert set(results) == set(KINDS)          # 未知名字不会变成"多发一路"


def test_result_order_is_stable_regardless_of_config_order(spy, cfg) -> None:
    """返回顺序固定为 KINDS 顺序（界面/日志显示稳定），配置顺序只决定"启用谁"。"""
    calls, _ = spy
    cfg.notify_channels = ["tray", "windows"]
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert list(results) == list(KINDS)
    active = [name for name, res in results.items() if not res.get("skipped")]
    assert set(active) == {"tray", "windows"}
    assert calls["tray"] and calls["windows"] and not calls["feishu"]


def test_feishu_on_switch(spy, cfg) -> None:
    calls, _ = spy
    cfg.notify_channels = ["windows", "feishu"]
    cfg.feishu_on = False
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert calls["windows"] and not calls["feishu"]
    assert results["feishu"]["skipped"] is True
    assert "feishu_on" in results["feishu"]["detail"]


def test_legacy_single_channel_switch_still_works(spy, cfg) -> None:
    """老的 notify_windows/notify_feishu 单频道开关继续生效（兼容旧配置）。"""
    calls, _ = spy
    cfg.notify_channels = ["windows", "tray"]
    cfg.notify_windows = False
    notify_all("标题", ["正文"], cfg=cfg)
    assert not calls["windows"]
    assert calls["tray"]


def test_channels_property_and_states(cfg) -> None:
    cfg.notify_channels = ["windows", "tray"]
    assert cfg.channels == ["windows", "tray"]
    cfg.notify_tray = False
    assert cfg.channels == ["windows"]
    cfg.notify_channels = []
    assert cfg.channels == []
    cfg.notify_channels = ["windows", "feishu"]
    cfg.notify_windows = True
    states = cfg.channel_states()
    assert states["windows"] == "启用"
    assert states["tray"] == "未在 notify_channels 中"
    assert "未配置飞书凭证" in states["feishu"]


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
    """飞书炸了 → 弹窗/托盘照常（并行 + 各自 try）。"""
    cfg.notify_channels = ["feishu", "windows", "tray"]

    def boom(*a, **k):
        raise RuntimeError("飞书通道炸了")

    monkeypatch.setattr(feishu, "notify", boom)
    monkeypatch.setattr(windows, "notify",
                        lambda *a, **k: {"kind": "windows", "ok": True, "detail": "弹了"})
    tray.clear()
    monkeypatch.setattr(tray, "notify",
                        lambda *a, **k: {"kind": "tray", "ok": True, "detail": "投递"})
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert results["feishu"]["ok"] is False
    assert "飞书通道炸了" in results["feishu"]["detail"]
    assert results["windows"]["ok"] is True
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


def test_windows_params_are_passed_down(cfg) -> None:
    """提示音 / 打开雪球 两个参数要真的传给 winotify 的那层实现。"""
    cfg.notify_channels = ["windows"]
    cfg.notify_windows_sound = False
    cfg.notify_windows_open_url = False
    result = windows.notify("标题", ["1. 甲（600519）"], cfg=cfg)
    if not windows.SUPPORTED:      # Linux：只能验证"没被配置挡下、走到了平台判定"
        assert result["supported"] is False
    else:                          # pragma: no cover - Windows
        assert result["ok"] is True
        assert "静音" in result["detail"]


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
    cfg.notify_channels = ["windows", "feishu"]
    cfg.feishu_app_id = cfg.feishu_app_secret = "x"
    text = summarizes_channels(cfg)
    assert "windows" in text and "feishu" in text
    results = notify_all("标题", ["正文"], cfg=cfg)
    assert "弹窗" in summarize(results)
