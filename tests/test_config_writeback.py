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
    default_data_dir,
    load_config,
    render_config_updates,
    save_settings,
    update_config_file,
)

from tests._toml import p

#: 模板：`{data_dir}` 处必须传**已转义**的路径（`tests._toml.p()`），
#: 否则 Windows 的 `C:\Users\...` 会让 tomllib 解析失败、配置静默退回默认值。
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
    path.write_text(SAMPLE.format(data_dir=p(tmp_path / "data")), encoding="utf-8")
    return path


def test_render_keeps_comments_and_unknown_keys(tmp_path: Path) -> None:
    text = SAMPLE.format(data_dir=p(tmp_path / "data"))
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
    text = SAMPLE.format(data_dir=p(tmp_path / "data"))
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


def test_writeback_escapes_windows_data_dir(tmp_path: Path) -> None:
    r"""界面里把数据目录设成 Windows 路径：写回的文件必须还能被解析出来。

    这是一条**产品流程**（不是测试自己拼字符串）：`_toml_value()` 会转义反斜杠，
    所以 `C:\Users\me\LaoATrader\data` 写进 config.toml 后重新加载必须一字不差。
    如果哪天有人给 `_toml_value` "优化"掉转义，这里立刻红。
    """
    win = r"C:\Users\me\AppData\Local\LaoATrader\data"
    target = tmp_path / "config.toml"
    target.write_text(f'data_dir = "{p(tmp_path / "old")}"\n', encoding="utf-8")
    cfg = Config(data_dir=tmp_path, source_path=target)

    path, updated = save_settings(cfg, {"data_dir": win})
    assert path == target
    assert updated.data_dir == Path(win)                    # 内存里立刻生效

    text = target.read_text(encoding="utf-8")
    assert "\\\\" in text                                   # 文件里是转义过的
    reloaded = load_config(target, use_env=False)
    assert reloaded.config_error == ""                      # 没有"解析失败"
    assert str(reloaded.data_dir) == win                    # 值原样取回
    assert reloaded.data_dir == Path(win)

# ── Windows 路径的"前移"回归：在 Linux 上就能抓到 CI 上才暴露的这类 bug ──
#
# 背景：CI 的 build-windows 连续两轮红在"测试自己拼 config.toml 时没转义"上
# （`C:\Users\...` 里的 `\U` 被 tomllib 当成转义序列 → Invalid hex value →
# 配置整体退回默认值 → data_dir 变成默认目录 → 空库 → 后续断言连锁崩）。
# 本地 Linux 永远绿，因为 `tmp_path` 里没有反斜杠。下面这几条**显式用 Windows 路径**
# 走一遍真实的写回/渲染流程，把那个失败模式钉在 Linux 上。

#: 与 CI 上真实出现过的两种路径一致（GitHub 运行器的临时目录 / 仓库工作目录）
WIN_TEMP_CONFIG = r"C:\Users\runneradmin\AppData\Local\Temp\pytest-of-x\config.toml"
WIN_REPO_DATA = r"D:\a\laoA\laoA\data"


@pytest.mark.parametrize("win_path", [WIN_TEMP_CONFIG, WIN_REPO_DATA])
def test_render_config_updates_keeps_windows_path_readable(win_path: str) -> None:
    r"""`render_config_updates()` 写回 Windows 路径后，重新 `load_config` 必须成功。

    这条盯的是**产品代码**（`config._toml_value()` 的转义）：只要它被"优化"掉，
    这里立刻红 —— 而且是 `config_error` 为空 + 路径一字不差这种强断言。
    """
    text = SAMPLE.format(data_dir=p(win_path))
    out = render_config_updates(text, {"notify_channels": ["windows"]})

    assert 'data_dir = "' + win_path.replace("\\", "\\\\") + '"' in out   # 落盘形态是转义过的
    parsed = _load_text(out)
    assert parsed.config_error == ""                    # 没有"解析失败（已退回默认值）"
    assert str(parsed.data_dir) == win_path              # 一字不差（含反斜杠）
    assert "notify_channels" in out


@pytest.mark.parametrize("win_path", [WIN_TEMP_CONFIG, WIN_REPO_DATA])
def test_update_config_file_roundtrips_windows_path(tmp_path: Path, win_path: str) -> None:
    r"""真落盘再读回：`update_config_file()` → `load_config()` 往返一致。"""
    target = tmp_path / "config.toml"
    target.write_text(SAMPLE.format(data_dir=p(tmp_path)), encoding="utf-8")

    update_config_file(target, {"data_dir": win_path})
    cfg = load_config(target, use_env=False)

    assert cfg.config_error == ""
    assert str(cfg.data_dir) == win_path
    assert cfg.source_path == target
    # 注释与未知键照旧（写回没有因为转义而改坏文件结构）
    raw = target.read_text(encoding="utf-8")
    assert "# 老A法师配置（这些注释必须活下来）" in raw
    assert "[my_own_section]" in raw


def _load_text(text: str) -> Config:
    """把 TOML 文本落成临时文件再 `load_config`（`load_config` 只吃路径）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path, use_env=False)


def test_unescaped_windows_path_is_what_breaks_ci() -> None:
    r"""反面证据：**不**转义时配置会**静默**退回默认值（CI 那 13 条失败就是这么来的）。

    没有这条，上面那些"必须转义"的用例只是"怎么写过都能过"。
    这里用的就是 CI 日志里的那条路径（`\U` 开头 → `Invalid hex value`）。
    """
    # 下面这行是**故意不转义**的：静态扫描按行尾标记放过它，其余地方一律不许这么写
    text = SAMPLE.format(data_dir=WIN_TEMP_CONFIG)       # toml-guard: allow-unescaped

    cfg = _load_text(text)
    assert "解析失败" in cfg.config_error                # 配置整体解析失败
    assert "Invalid hex value" in cfg.config_error       # 与 CI 日志逐字一致
    assert cfg.data_dir == default_data_dir()            # **静默**退回默认值 → 空库 → 断言连锁崩
    assert str(cfg.data_dir) != WIN_TEMP_CONFIG


def test_unescaped_repo_path_also_breaks_config() -> None:
    r"""同一类问题的另一种长相：`D:\a\laoA\laoA\data` 里的 `\a` 是非法转义。

    断言只要求"解析失败 + 静默退回默认值"：具体报错文案随路径里第一个反斜杠的组合而变
    （`\U` → Invalid hex value，`\a` → Unescaped '\\' in a string），但**后果一样**。
    """
    text = SAMPLE.format(data_dir=WIN_REPO_DATA)         # toml-guard: allow-unescaped

    cfg = _load_text(text)
    assert "解析失败" in cfg.config_error
    assert cfg.config_error                          # 有明确的错误说明（不是静默吞掉）
    assert cfg.data_dir == default_data_dir()        # 退回默认目录 = 后面"空库/needs_full"的根因
    assert str(cfg.data_dir) != WIN_REPO_DATA
