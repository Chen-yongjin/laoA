"""通知频道自选：`notify_channels` 生效、各频道参数生效、频道间互不影响。

覆盖需求「三、通知方式自己设定」：
- `notify_channels` 决定发哪几个；**空列表 = 一个都不发**（只入库）；
- 只勾 windows 时，飞书**根本不会被调用**；
- 某频道抛错只记日志，其余频道照常；
- 飞书没配凭证时：明确提示「未配置飞书凭证，已跳过」，不报错、不影响其它频道；
- 每个频道的参数（提示音/打开雪球/气泡时长）真的传下去了。
"""

from __future__ import annotations

import sys
import types
from typing import Any

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


#: 真 winotify 的校验逻辑（逐字抄自 `site-packages/winotify/__init__.py`）。
#: 我们的假实现**必须照抄**：否则 duration 写错时 Linux 上永远绿 —— 这正是
#: CI 的 Windows 运行器抓到、而本地一直没抓到的那个真机 bug。
WINOTIFY_DURATION_ERROR = "Duration is not 'short' or 'long'"


class _FakeToast:
    """记录参数的假 Toast；构造时**像真库那样**校验 duration。"""

    def __init__(
        self,
        app_id: str = "",
        title: str = "",
        msg: str = "",
        icon: str = "",
        duration: str = "short",
    ) -> None:
        if duration not in ("short", "long"):
            raise ValueError(WINOTIFY_DURATION_ERROR)
        self.app_id = app_id
        self.title = title
        self.msg = msg
        self.icon = icon
        self.duration = duration
        self.actions: list[tuple[str, str]] = []
        self.audio: tuple[Any, bool] | None = None
        self.shown = False

    def add_actions(self, label: str = "", launch: str = "") -> None:
        self.actions.append((label, launch))

    def set_audio(self, sound: Any = None, loop: bool = False) -> None:
        self.audio = (sound, loop)

    def show(self) -> None:
        self.shown = True


@pytest.fixture()
def fake_winotify(monkeypatch):
    """注入一个"照抄真库校验"的假 `winotify`，并把平台判定改成"就是 Windows"。

    为什么这么做：`windows.notify` 在非 Windows 上会**提前返回**"平台不支持"，
    真机那条路径在 Linux/CI 里一行都跑不到 —— 于是"duration 非法"这类问题
    只有用户在自己的 Windows 上才会遇到。这里把平台判定改掉 + 假库照抄校验，
    让那条路径**在 Linux 上也能真跑**。
    """
    toasts: list[_FakeToast] = []

    def _make(*args: Any, **kwargs: Any) -> _FakeToast:
        toast = _FakeToast(*args, **kwargs)
        toasts.append(toast)
        return toast

    module = types.ModuleType("winotify")
    module.Notification = _make                                   # type: ignore[attr-defined]
    module.audio = types.SimpleNamespace(Default="Default", IM="IM", Reminder="Reminder")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winotify", module)
    monkeypatch.setattr(windows, "SUPPORTED", True)
    return toasts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "short"),            # 不传 → 默认档
        ("", "short"),
        ("   ", "short"),
        ("short", "short"),
        ("LONG", "long"),           # 大小写不敏感
        (" long ", "long"),         # 前后空格容忍
        ("10s", "short"),           # 老代码里的默认值：换算成 short（绝不能再直接透传）
        ("10", "short"),
        (10, "short"),
        (10.5, "short"),
        ("8000ms", "short"),
        ("8000 毫秒", "short"),
        ("20s", "long"),            # ≥ 20 秒算长
        ("25s", "long"),
        (30, "long"),
        ("25000ms", "long"),
        ("随便什么", "short"),       # 认不出来 → 回到默认档，绝不透传非法值
        ("-5s", "short"),
        (object(), "short"),
    ],
)
def test_normalize_duration_only_yields_winotify_values(value: Any, expected: str) -> None:
    """任何输入都只能产出 winotify 认的两个值之一。"""
    got = windows.normalize_duration(value)
    assert got == expected
    assert got in windows.WINOTIFY_DURATIONS


def test_windows_default_call_actually_shows_toast(cfg, fake_winotify) -> None:
    """**真机 bug 回归**：默认调用必须真的弹出通知（老代码永远弹不出来）。

    老默认值 `duration="10s"` → winotify 抛 `ValueError` → 被 `except` 吞成
    "通知失败"日志：用户侧的表现是"Windows 通知一直没动静，也不报错"。
    """
    cfg.notify_channels = ["windows"]
    cfg.notify_windows_sound = False
    cfg.notify_windows_open_url = False
    result = windows.notify("标题", ["1. 甲（600519）"], cfg=cfg)

    assert result["ok"] is True, result
    assert len(fake_winotify) == 1
    toast = fake_winotify[0]
    assert toast.duration in windows.WINOTIFY_DURATIONS    # ← 非法值到不了这里
    assert toast.shown is True
    assert toast.title == "标题"
    assert toast.app_id == windows.APP_ID


@pytest.mark.parametrize(
    ("value", "expected"), [("10s", "short"), ("25s", "long"), ("乱写", "short"), (None, "short")]
)
def test_windows_duration_argument_is_normalized(cfg, fake_winotify, value, expected) -> None:
    """调用方给什么时长写法都行，落到 winotify 的永远是合法档位。"""
    cfg.notify_channels = ["windows"]
    result = windows.notify("标题", ["正文"], cfg=cfg, duration=value)
    assert result["ok"] is True, result
    assert fake_winotify[0].duration == expected


def test_windows_params_are_passed_down(cfg, fake_winotify) -> None:
    """提示音 / 打开雪球 两个参数要真的传给 winotify 的那层实现。"""
    cfg.notify_channels = ["windows"]
    cfg.notify_windows_sound = False
    cfg.notify_windows_open_url = False
    result = windows.notify("标题", ["1. 甲（600519）"], cfg=cfg)
    assert result["ok"] is True
    assert "静音" in result["detail"]
    toast = fake_winotify[0]
    assert toast.audio is None            # 关掉提示音 → 不调 set_audio
    assert toast.actions == []            # 关掉跳转 → 不加按钮


def test_windows_open_url_button_points_to_xueqiu(cfg, fake_winotify) -> None:
    """开着跳转时：按钮指向该股雪球页，并且带提示音。"""
    cfg.notify_channels = ["windows"]
    cfg.notify_windows_sound = True
    cfg.notify_windows_open_url = True
    result = windows.notify("标题", ["1. 贵州茅台（600519）"], cfg=cfg)
    assert result["ok"] is True
    toast = fake_winotify[0]
    assert toast.actions == [("打开雪球", "https://xueqiu.com/S/SH600519")]
    assert toast.audio == ("Default", False)


def test_windows_body_keeps_first_eight_lines(cfg, fake_winotify) -> None:
    """正文只取前 8 行（多了系统会截断，不如自己截得干净）。"""
    cfg.notify_channels = ["windows"]
    lines = [f"{i}. 甲{i}（6005{19 + i:02d}）" for i in range(1, 13)]
    assert windows.notify("标题", lines, cfg=cfg)["ok"] is True
    body = fake_winotify[0].msg
    assert body.splitlines() == lines[:8]


def test_windows_empty_body_uses_placeholder(cfg, fake_winotify) -> None:
    """没有正文时不能弹一个空气泡（winotify 会报错）。"""
    cfg.notify_channels = ["windows"]
    assert windows.notify("标题", [], cfg=cfg)["ok"] is True
    assert fake_winotify[0].msg == "（无内容）"


def test_windows_failure_is_reported_not_raised(cfg, fake_winotify, monkeypatch) -> None:
    """弹窗失败只回报原因、不抛异常（否则会把整条推送链路带崩）。"""
    cfg.notify_channels = ["windows"]

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("系统把通知关了")

    monkeypatch.setattr(sys.modules["winotify"], "Notification", boom)
    result = windows.notify("标题", ["正文"], cfg=cfg)
    assert result["ok"] is False
    assert "系统把通知关了" in result["detail"]


def test_windows_disabled_by_config(cfg) -> None:
    """配置里关掉 → 报"已关闭"，而不是"平台不支持"（更准确）。"""
    cfg.notify_windows = False
    result = windows.notify("标题", ["正文"], cfg=cfg)
    assert result == {"kind": "windows", "ok": True, "skipped": True,
                      "detail": "已在配置中关闭"}


def test_windows_without_winotify_degrades(cfg, monkeypatch) -> None:
    """Windows 上没装 winotify：静默降级成"不支持"，不算错误。"""
    monkeypatch.setattr(windows, "SUPPORTED", True)
    monkeypatch.setitem(sys.modules, "winotify", None)      # import 时抛 ImportError
    result = windows.notify("标题", ["正文"], cfg=cfg)
    assert result["ok"] is False and result["supported"] is False


def test_windows_on_other_platform_is_not_an_error(cfg, monkeypatch) -> None:
    """非 Windows：`supported=False`，调用方不应视为失败。"""
    monkeypatch.setattr(windows, "SUPPORTED", False)
    result = windows.notify("标题", ["正文"], cfg=cfg)
    assert result["ok"] is False and result["supported"] is False
    assert "不支持" in result["detail"]


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
