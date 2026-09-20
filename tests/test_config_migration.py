"""配置位置迁移的看门人：**老位置有配置、新位置空** 时，`load_config()` 必须读得到。

为什么单独一个文件（2026-09-20 用户实报"新版本同花顺 KEY 配置了仍提示没配"）：
上一版把配置从"exe 同级"搬到 `%APPDATA%\\LaoATrader\\config.toml`（为了覆盖安装不丢飞书
配置），并加了一条"老位置 → 新位置"的迁移。但**迁移函数里用了 `logger`，而 `config.py`
里从来没有定义过它** —— 于是只要走这条迁移路径就抛 `NameError`，而那次调用没有兜底，
异常一路冒到 `load_config()`（它的承诺是"绝不抛异常"）。**表现就是**：启动时读不到任何
配置 → 用户填过的同花顺 Key、飞书 app_id 全被判成"没配置"，于是界面一直提示"请配置
同花顺 Key"，而文件里明明有。

这条用例就是那个上线级 bug 的看门人：它走**真实入口**（`load_config()`），
而不是直接调迁移函数 —— 只有这样才能拦住"迁移里再写错一行"这类问题。
"""

from __future__ import annotations

import os
from pathlib import Path

from laoa_trader import config as config_mod

#: 老位置那份配置的内容（同花顺 Key + 飞书三件套：正是用户最怕丢的东西）
LEGACY_TOML = '''\
hithink_api_key = "OLD-KEY-FROM-LEGACY"
notify_channels = ["feishu"]
feishu_app_id = "cli_legacy"
feishu_app_secret = "secret_legacy"
feishu_chat_id = "chat_legacy"
'''


def _setup_dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """造出"老位置有配置、用户位置是空的"这套目录，并把环境指过去。

    Returns:
        `(老位置目录, 老配置文件, 用户位置配置文件)`
    """
    legacy_dir = tmp_path / "安装目录"          # 老版本：config.toml 就在 exe 边上
    legacy_dir.mkdir()
    legacy = legacy_dir / "config.toml"
    legacy.write_text(LEGACY_TOML, encoding="utf-8")

    appdata = tmp_path / "AppData"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))              # 用户位置（Windows 口径）
    monkeypatch.delenv("LAOA_TRADER_CONFIG", raising=False)  # 不显式指定路径
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    monkeypatch.chdir(legacy_dir)                            # 老位置之一就是"当前目录"
    return legacy_dir, legacy, config_mod.user_config_path()


def test_load_config_reads_the_legacy_file_and_never_raises(tmp_path, monkeypatch) -> None:
    """老配置会被认过来：Key 读得到、飞书也在，而且 `load_config()` 绝不抛异常。

    （这条就是那个 `NameError` 的直接复现：修之前它在 `find_config_file()` 里炸。）
    """
    _legacy_dir, legacy, user_config = _setup_dirs(tmp_path, monkeypatch)

    cfg = config_mod.load_config(use_env=False)          # ← 真实入口，异常会直接冒出来

    assert cfg.hithink_api_key == "OLD-KEY-FROM-LEGACY"
    assert cfg.feishu_app_id == "cli_legacy"
    assert cfg.feishu_app_secret == "secret_legacy"
    assert cfg.feishu_chat_id == "chat_legacy"
    assert cfg.source_path == user_config, "读的应当是新位置那一份（迁移之后）"


def test_migration_copies_instead_of_moving_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """迁移是**复制**（老文件留着当保险）、并且第二次启动不会重复搬。"""
    _legacy_dir, legacy, user_config = _setup_dirs(tmp_path, monkeypatch)

    config_mod.load_config(use_env=False)
    assert user_config.is_file(), "老配置没有被认到用户位置"
    assert legacy.is_file(), "老文件被搬走了（应当是复制）"

    first = user_config.read_text(encoding="utf-8")
    before_mtime = user_config.stat().st_mtime_ns
    config_mod.load_config(use_env=False)                # 再启动一次
    assert user_config.read_text(encoding="utf-8") == first
    assert user_config.stat().st_mtime_ns == before_mtime, "第二次启动又写了一遍"


def test_user_config_path_wins_over_the_legacy_copy(tmp_path, monkeypatch) -> None:
    """用户位置已经有配置时，**以它为准**（老那份不再覆盖用户后来填的值）。"""
    _legacy_dir, legacy, user_config = _setup_dirs(tmp_path, monkeypatch)
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text('hithink_api_key = "NEW-KEY-IN-USER-DIR"\n', encoding="utf-8")

    cfg = config_mod.load_config(use_env=False)

    assert cfg.hithink_api_key == "NEW-KEY-IN-USER-DIR"
    assert legacy.read_text(encoding="utf-8") == LEGACY_TOML, "老文件不该被动过"


def test_saving_writes_to_the_user_location(tmp_path, monkeypatch) -> None:
    """界面上填的 Key 保存到**用户位置**（覆盖安装碰不到它）。"""
    legacy_dir, _legacy, user_config = _setup_dirs(tmp_path, monkeypatch)
    cfg = config_mod.load_config(use_env=False)

    path, saved = config_mod.save_settings(cfg, {"hithink_api_key": "TYPED-BY-USER"})

    assert path == user_config
    assert saved.hithink_api_key == "TYPED-BY-USER"
    assert "TYPED-BY-USER" in user_config.read_text(encoding="utf-8")
    # 保存**不会**写到安装目录里去（那儿一升级就被覆盖）
    assert "TYPED-BY-USER" not in (legacy_dir / "config.toml").read_text(encoding="utf-8")


def test_config_module_has_a_logger() -> None:
    """`config.py` 里用到日志就必须有 logger —— 上一版漏了它才炸成那样。

    这条看着傻，但它拦的正是"新增迁移/兼容代码时顺手写 `logger.info(...)`"这一类
    在别处很常见、在这里却会**打断读配置**的动作（`config.py` 的加载链路上没有任何
    兜底能救它：那时连 `find_config_file()` 都还没返回）。
    """
    assert hasattr(config_mod, "logger")
    for name in ("info", "warning", "debug", "error"):
        assert callable(getattr(config_mod.logger, name))
    assert config_mod.logger.name.endswith("config")
