"""CLI 自选与覆盖：`--groups` / `--strategies` / `--once` / `--pool` / `--list-groups`。

覆盖需求「一、4」与「二、1」的命令行部分：
- 临时覆盖**不改配置文件**（文件字节不变）；
- 两个都错时明确提示并且**不静默跑全量**；
- `--once` 会写 signal/stock_pool 且与定时跑口径一致（幂等）。

⚠️ 2026-09-18（用户要求）**口径变过**：候选只来自勾选的公式，
`enabled_groups` / `enabled_strategies`（以及 `--groups` / `--strategies` 这两个覆盖参数）
**不再影响选股结果** —— 它们只对 `--list-groups` / `--scorecard` 这些研究用入口有意义。
所以下面"只跑某组/某策略"的用例都改成了"勾一条公式 → 它决定选谁"，
并顺手钉住"覆盖参数不再把公式候选剔掉"。
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


def _enable_formulas(monkeypatch, tmp_path, cfg, config_path, formulas: dict[str, str]) -> None:
    """写公式文件 + 在 **config.toml** 里勾上它们（CLI 读的是文件，不是内存对象）。

    2026-09-18（用户要求）起候选只来自勾选的公式，所以走 CLI 的用例必须把
    `enabled_formulas` 写进配置文件、并把公式目录指过去（`LAOA_TRADER_FORMULAS`）。
    小库价格是确定的：600001 = 3.0 元、600003 ≈ 12.4 元 → `C<5` / `C>10` 各选一只。
    """
    from tests._toml import p as toml_path

    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in formulas.items():
        (folder / f"{name}.txt").write_text(
            f"# 名称: {name}\n# 说明: 测试用（{name}）\n{body}\n", encoding="utf-8"
        )
    monkeypatch.setenv("LAOA_TRADER_FORMULAS", str(folder))
    text = config_path.read_text(encoding="utf-8")
    names = ", ".join(f'"{n}"' for n in formulas)
    config_path.write_text(
        text + f"\nenabled_formulas = [{names}]\n", encoding="utf-8"
    )
    cfg.enabled_formulas = list(formulas)
    assert toml_path(folder).endswith("formulas")      # 顺手确认路径助手没被改坏


def test_list_groups_output(capsys) -> None:
    assert cli(["--cli", "--list-groups"]) == 0
    out = capsys.readouterr().out
    for key in ("ultra", "short", "swing"):
        assert key in out
    assert "T+2" in out and "T+3" in out and "T+10" in out
    assert "连板回踩低吸" in out and "低价股" in out
    assert "--groups" in out


def test_formula_decides_the_picks_and_group_override_is_inert(capsys, seeded,
                                                              tmp_path, monkeypatch) -> None:
    """候选由**勾选的公式**决定；`--groups` 覆盖不再把公式候选剔掉（2026-09-18 口径）。

    勾一条 `C>10`（小库里只有 600003 在 10 元以上），再传一个跟它毫无关系的
    `--groups swing`：票照旧进池、来源写 `公式·反转` —— 覆盖参数只对研究用入口有意义。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
    args = ["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]
    before = seeded["config"].read_text(encoding="utf-8")

    assert cli(args + ["--groups", "swing"]) == 0
    out = capsys.readouterr().out
    assert "反转样本(600003)" in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
        pool_strategies = {r[0] for r in conn.execute(
            "SELECT DISTINCT strategy FROM stock_pool")}
    assert strategies == {"公式·反转"}            # 信号表里就是这一轮跑的公式
    assert pool_strategies == {"公式·反转"}
    # CLI 覆盖是"临时的"：配置文件字节不变（`--groups` 只改内存里的 cfg）
    assert seeded["config"].read_text(encoding="utf-8") == before


def test_strategies_override_accepts_chinese_names(capsys, seeded,
                                                   tmp_path, monkeypatch) -> None:
    """`--strategies 短期反转` 中文名仍能解析（不报错），但**不再决定选谁**。

    2026-09-18 起候选只来自勾选的公式：这里勾的是 `C<5`（只有 600001），
    所以就算覆盖参数写着"短期反转"，选出来的也还是公式那一只。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"低价": "C<5"})
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--strategies", "短期反转"]) == 0
    out = capsys.readouterr().out
    assert "低价样本(600001)" in out
    assert "认不出" not in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
    assert strategies == {"公式·低价"}


def test_strategies_override_accepts_class_names(capsys, seeded,
                                                 tmp_path, monkeypatch) -> None:
    """类名写法同样能解析（不报错）；选谁仍由勾选的公式决定。"""
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"低价": "C<5"})
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--strategies", "LowPriceStrategy"]) == 0
    out = capsys.readouterr().out
    assert "认不出" not in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
    assert strategies == {"公式·低价"}


def test_groups_and_strategies_intersect_on_cli(capsys, seeded) -> None:
    """`--groups short --strategies 低价股` 交集为空：**不再拒绝运行**（2026-09-18 口径）。

    老口径下"交集为空"会让整轮选股被跳过（还报错退出 1）；新口径下这两个参数
    只管研究用入口，选股看的是 `enabled_formulas`，所以这里照常跑完 ——
    只是没有任何公式被勾上，于是"没有候选"（退出码 0，与"只盯自选股"同一件事）。
    """
    code = cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--groups", "short", "--strategies", "低价股"])
    out = capsys.readouterr().out
    assert code == 0
    assert "没有勾选任何公式" in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0


def test_unknown_group_name_warns(capsys, seeded) -> None:
    """组名拼错仍然当场拦住（覆盖参数的**解析**没变，变的只是"它不再决定选谁"）。"""
    code = cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"]),
                "--groups", "nope"])
    out = capsys.readouterr().out
    assert code == 1
    assert "未知策略组" in out


def test_config_groups_no_longer_decide_the_picks(capsys, seeded,
                                                  tmp_path, monkeypatch) -> None:
    """`enabled_groups` **不再决定候选**（2026-09-18）：决定权在 `enabled_formulas`。

    把配置里的组改成只剩 `short`（按老口径 600001 就该消失），但勾的公式是 `C<5`
    （只有 600001）—— 新口径下池子里照样是它。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"低价": "C<5"})
    seeded["config"].write_text(
        seeded["config"].read_text(encoding="utf-8").replace(
            'enabled_groups = ["ultra", "short", "swing"]',
            'enabled_groups = ["short"]'),
        encoding="utf-8",
    )
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]) == 0
    out = capsys.readouterr().out
    assert "本次按勾选的公式选股：低价" in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        pool_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stock_pool")}
    assert pool_symbols == {"600001"}


def test_pool_command_shows_the_source_column(capsys, seeded,
                                             tmp_path, monkeypatch) -> None:
    """`--pool` 打出池子表；2026-09-18 起来源列是 `公式·<公式名>`。"""
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]) == 0
    capsys.readouterr()
    assert cli(["--cli", "--pool", "--config", str(seeded["config"])]) == 0
    out = capsys.readouterr().out
    assert "股票池" in out
    assert "公式·反转" in out


def test_once_is_idempotent_via_cli(capsys, seeded, tmp_path, monkeypatch) -> None:
    """连续两次 `--once`：信号/池子行数不变（CLI 与定时任务同口径）。"""
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
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
