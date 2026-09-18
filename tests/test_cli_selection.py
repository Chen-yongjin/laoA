"""CLI 自选与覆盖：`--groups` / `--strategies` / `--once` / `--pool` / `--list-groups`。

覆盖需求「一、4」与「二、1」的命令行部分：
- 临时覆盖**不改配置文件**（文件字节不变）；
- `--groups ultra` 只跑 ultra 的策略、池子也只含该组标的；
- `--strategies 低价股` 用中文名也能选中；
- 两个都错时明确提示并且**不静默跑全量**；
- `--once` 会写 signal/stock_pool 且与定时跑口径一致（幂等）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from laoa_trader.__main__ import cli
from laoa_trader.data import storage

from tests._toml import p
from tests.conftest import READY_THRESHOLDS


@pytest.fixture()
def seeded(cfg, tmp_path):
    """一份能跑出候选的库 + 指向它的 config.toml（内容固定，便于比对是否被改动）。

    ⚠️ 别把这份 fixture 读成"没 Key 也能选股"（2026-09-18 用户核实后要求写准）：
    这份库是**手工造的就绪库** —— 复权事件与行业归属都是这里显式写进去的（只有同花顺那条
    dump 路给得到这两样），`hithink_api_key = ""` 只是为了让用例不发任何请求。
    真实情形是：**没 Key 时只能用免 Key 公开源，而公开源给不出这两样，选股永远过不了
    数据闸门**（见 `tests/test_public_sync.py` 里
    `test_public_source_data_can_never_pass_the_stock_picking_gate`）。
    本文件测的是"数据已经就绪之后，选股/覆盖参数本身对不对" —— 那是纯本地的事。
    """
    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()
    n = len(days)

    def bars(symbol, closes, volumes):
        out = []
        for i, day in enumerate(days):
            close = float(closes[i])
            volume = float(volumes[i])
            out.append((symbol, day, close, close, close, close, volume, volume * close))
        return out

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "低价样本", "银行"),
            ("600003", "反转样本", "半导体"),
        ])
        storage.write_daily_raw(
            conn,
            bars("600001", [3.0] * n, [2e7] * n)
            + bars("600003", [20.0 * (0.988 ** i) for i in range(n)], [2e7] * n),
        )
        storage.write_calendar(conn, days)
        # 复权事件非空是"自检就绪"的硬条件之一（缺了价格会算错）
        storage.write_adjust_events(conn, [("600001", days[len(days) // 2], 0.1, 0.0, 0.0, 0.0)])

    config = tmp_path / "config.toml"
    config.write_text(
        f'# 用户自己的注释\n'
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        'enabled_groups = ["ultra", "short", "swing"]\n'
        'enabled_strategies = []\n'
        "notify_channels = []          # 不推送\n"
        + READY_THRESHOLDS,
        encoding="utf-8",
    )
    return {"cfg": cfg, "config": config}


def test_list_groups_output(capsys) -> None:
    assert cli(["--cli", "--list-groups"]) == 0
    out = capsys.readouterr().out
    for key in ("ultra", "short", "swing"):
        assert key in out
    assert "T+2" in out and "T+3" in out and "T+10" in out
    assert "连板回踩低吸" in out and "低价股" in out
    assert "--groups" in out


def test_groups_override_runs_only_that_group(capsys, seeded) -> None:
    args = ["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]
    before = seeded["config"].read_text(encoding="utf-8")

    assert cli(args + ["--groups", "swing"]) == 0
    out = capsys.readouterr().out
    assert "波段·T+10" in out                     # 选择摘要
    assert "低价样本(600001)" in out            # 只跑了 swing
    # 池子里不该出现别的策略（低价股策略自己选中 600003 是合理的：
    # 它 12 元、跌幅没到跌停、流动性够，确实符合"低价股"的条件；
    # 所以这里断言"策略"而不是"股票"）
    assert "Reversal" not in out
    assert "DryUpExpansion" not in out and "FirstLimitUp" not in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
        pool_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stock_pool")}
    assert strategies == {"LowPriceStrategy"}
    assert "600001" in pool_symbols               # 低价股策略选中的标的进了池子
    # 池子里每一行都必须来自启用的策略（用 strategy 字段核对，与股票本身无关）
    with storage.connect(seeded["cfg"].db_path) as conn:
        pool_strategies = {r[0] for r in conn.execute(
            "SELECT DISTINCT strategy FROM stock_pool")}
    assert pool_strategies == {"LowPriceStrategy"}
    # CLI 覆盖是"临时的"：配置文件字节不变
    assert seeded["config"].read_text(encoding="utf-8") == before


def test_strategies_override_accepts_chinese_names(capsys, seeded) -> None:
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--strategies", "短期反转"]) == 0
    out = capsys.readouterr().out
    assert "反转样本(600003)" in out
    assert "低价样本" not in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
    assert strategies == {"ReversalStrategy"}


def test_strategies_override_accepts_class_names(capsys, seeded) -> None:
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--strategies", "LowPriceStrategy"]) == 0
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
    assert strategies == {"LowPriceStrategy"}


def test_groups_and_strategies_intersect_on_cli(capsys, seeded) -> None:
    """`--groups short --strategies 低价股` → 交集为空：不该跑，且要说清原因。"""
    code = cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--groups", "short", "--strategies", "低价股"])
    out = capsys.readouterr().out
    assert code == 1
    assert "没有启用任何策略" in out or "⚠️" in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0


def test_unknown_group_name_warns(capsys, seeded) -> None:
    code = cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--groups", "nope"])
    out = capsys.readouterr().out
    assert code == 1
    assert "未知策略组" in out


def test_config_selection_is_used_without_cli_flags(capsys, seeded) -> None:
    """不传 CLI 参数时按 config.toml 的 enabled_groups 走。"""
    seeded["config"].write_text(
        seeded["config"].read_text(encoding="utf-8").replace(
            'enabled_groups = ["ultra", "short", "swing"]',
            'enabled_groups = ["short"]'),
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]) == 0
    out = capsys.readouterr().out
    assert "短线·T+3" in out
    assert "低价样本" not in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        pool_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stock_pool")}
    assert pool_symbols == {"600003"}


def test_pool_command_shows_group_column(capsys, seeded) -> None:
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]) == 0
    capsys.readouterr()
    assert cli(["--cli", "--pool", "--config", str(seeded["config"])]) == 0
    out = capsys.readouterr().out
    assert "股票池" in out
    assert "波段·T+10" in out or "短线·T+3" in out


def test_once_is_idempotent_via_cli(capsys, seeded) -> None:
    """连续两次 `--once`：信号/池子行数不变（CLI 与定时任务同口径）。"""
    args = ["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]
    assert cli(args) == 0
    capsys.readouterr()
    with storage.connect(seeded["cfg"].db_path) as conn:
        first = (conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0])
    assert cli(args) == 0
    capsys.readouterr()
    with storage.connect(seeded["cfg"].db_path) as conn:
        second = (conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0])
    assert first == second
    assert first[0] > 0 and first[1] > 0
