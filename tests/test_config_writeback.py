"""设置写回 config.toml：**必须保留注释、未知键与表结构**。

覆盖需求「三、通知方式自己设定 · 4」与「四、5」：
用户在 config.toml 里写的注释、他自己加的键、将来版本的表，都不能因为
在界面上点了"保存设置"就被抹掉（那是 dict → TOML 全量重写的典型后果）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from laoa_trader.config import (
    Config,
    load_config,
    render_config_updates,
    save_settings,
    update_config_file,
)

SAMPLE = '''# 老A法师配置（这些注释必须活下来）
# 第二行注释      # 行尾也有注释

hithink_api_key = "abc123"       # 同花顺 Key
data_dir = "{data_dir}"

# ── 策略组 ──
enabled_groups = ["ultra", "short", "swing"]
enabled_strategies = []

# ── 通知 ──
notify_channels = [
  "windows",
  "feishu",
  "tray",
]                                # 多行数组，注释要留
notify_windows_sound = true
notify_tray_duration_ms = 8000

[my_own_section]                 # 用户自己的表
future_key = "keep me"           # 将来版本的键，不能丢

# 文件末尾注释
'''


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE.format(data_dir=tmp_path / "data"), encoding="utf-8")
    return path


def test_render_keeps_comments_and_unknown_keys(tmp_path: Path) -> None:
    text = SAMPLE.format(data_dir=tmp_path / "data")
    out = render_config_updates(text, {
        "notify_channels": ["windows"],
        "notify_windows_sound": False,
        "enabled_groups": ["swing"],
        "enabled_strategies": ["低价股"],
    })
    # 注释全在
    assert "# 老A法师配置（这些注释必须活下来）" in out
    assert "# 第二行注释      # 行尾也有注释" in out
    assert "# 同花顺 Key" in out
    assert "# ── 策略组 ──" in out
    assert "# 文件末尾注释" in out
    # 用户自己的表与未知键全在
    assert "[my_own_section]" in out
    assert 'future_key = "keep me"' in out
    # 多行数组被整体替换成单行（注释保留在同一行）
    assert 'notify_channels = ["windows"]' in out
    assert "# 多行数组，注释要留" in out
    assert '"feishu"' not in out.split("[my_own_section]")[0]
    # 未被修改的键原样（含对齐的空格）
    assert 'hithink_api_key = "abc123"       # 同花顺 Key' in out
    assert "notify_tray_duration_ms = 8000" in out


def test_render_appends_missing_keys(tmp_path: Path) -> None:
    text = SAMPLE.format(data_dir=tmp_path / "data")
    out = render_config_updates(text, {"notify_windows_open_url": False, "feishu_on": True})
    assert "notify_windows_open_url = false" in out
    assert "feishu_on = true" in out
    assert "以下由「设置」面板写入" in out
    # 追加不会破坏原有内容
    assert "[my_own_section]" in out
    assert out.startswith("# 老A法师配置")


def test_render_does_not_touch_keys_inside_tables(tmp_path: Path) -> None:
    """表里的同名键不能被顶层更新误伤；新增的顶层键必须插在**第一个表头之前**。

    为什么强调位置：TOML 没有"表结束"语法，追加到文件末尾会被解析成最后那张表
    里的键 —— 用户表被污染、而顶层根本读不到这个新键。
    """
    text = '[a]\nvalue = 1\n\n[b]\nnotify_channels = ["x"]\n'
    out = render_config_updates(text, {"notify_channels": ["windows"]})
    assert 'notify_channels = ["x"]' in out          # 表内的没动
    assert 'notify_channels = ["windows"]' in out    # 顶层新增了
    # 新键在第一个表头之前
    assert out.index('notify_channels = ["windows"]') < out.index("[a]")
    # 写回后能被解析，且顶层值正确（表里的值不受影响）
    import tomllib

    parsed = tomllib.loads(out)
    assert parsed["notify_channels"] == ["windows"]
    assert parsed["b"]["notify_channels"] == ["x"]


def test_quotes_and_backslashes_are_escaped(tmp_path: Path) -> None:
    out = render_config_updates("", {"a": 'C:\\path\\"x"', "b": True, "c": 1.5, "d": 3,
                                     "e": ["x", "y"]})
    assert 'a = "C:\\\\path\\\\\\"x\\""' in out
    assert "b = true" in out
    assert "c = 1.5" in out
    assert "d = 3" in out
    assert 'e = ["x", "y"]' in out


def test_update_config_file_roundtrip(sample_file: Path) -> None:
    """写回后必须仍然是**合法 TOML**，且新值能被 load_config 读到。"""
    update_config_file(sample_file, {
        "enabled_groups": ["ultra"],
        "notify_channels": ["tray"],
        "notify_tray_duration_ms": 3000,
        "feishu_on": False,
    })
    cfg = load_config(sample_file, use_env=False)
    assert cfg.enabled_groups == ["ultra"]
    assert cfg.notify_channels == ["tray"]
    assert cfg.notify_tray_duration_ms == 3000
    assert cfg.feishu_on is False
    # 未被更新的键仍然读得到原值
    assert cfg.hithink_api_key == "abc123"
    assert cfg.notify_windows_sound is True
    # 注释还在（逐字节再读一次原文）
    text = sample_file.read_text(encoding="utf-8")
    assert "# 老A法师配置（这些注释必须活下来）" in text
    assert 'future_key = "keep me"' in text


def test_update_is_idempotent(sample_file: Path) -> None:
    """连续保存两次，第二次不该产生任何差异（否则每次保存都在堆注释）。"""
    updates = {"notify_channels": ["windows"], "enabled_groups": ["short"]}
    update_config_file(sample_file, updates)
    first = sample_file.read_text(encoding="utf-8")
    update_config_file(sample_file, updates)
    assert sample_file.read_text(encoding="utf-8") == first


def test_update_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "new" / "config.toml"
    update_config_file(target, {"run_at": "20:00", "enabled_groups": ["ultra"]})
    assert target.is_file()
    cfg = load_config(target, use_env=False)
    assert cfg.run_at == "20:00"
    assert cfg.enabled_groups == ["ultra"]


def test_update_does_not_leave_temp_file(sample_file: Path) -> None:
    update_config_file(sample_file, {"run_at": "18:30"})
    assert not (sample_file.parent / "config.toml.tmp").exists()
    assert not list(sample_file.parent.glob("*.tmp"))


def test_save_settings_updates_memory_config(sample_file: Path) -> None:
    """保存后内存里的 cfg 立刻是新值（避免界面还在用旧设置）。"""
    cfg = load_config(sample_file, use_env=False)
    path, cfg = save_settings(cfg, {"notify_channels": ["windows"],
                                    "notify_windows_sound": False})
    assert path == sample_file
    assert cfg.notify_channels == ["windows"]
    assert cfg.notify_windows_sound is False
    assert cfg.source_path == sample_file
    # 磁盘上也确实是新值
    assert load_config(sample_file, use_env=False).notify_windows_sound is False


def test_save_settings_raises_oserror_on_unwritable(tmp_path: Path) -> None:
    """写不进去要抛 OSError（界面转成中文提示），而不是静默失败。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    cfg = Config(data_dir=tmp_path, source_path=blocker / "sub" / "config.toml")
    with pytest.raises(OSError):
        save_settings(cfg, {"notify_channels": []})
