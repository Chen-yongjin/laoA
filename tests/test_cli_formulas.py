"""公式驱动的 CLI：`--once` / `--pool` + 老配置里的退役键不再影响匹配。

⚠️ 2026-09-18（用户要求）**口径变过，入口也变过**：候选只来自勾选的公式，
`enabled_groups` / `enabled_strategies` 两个配置键**删除**（老的 config.toml 里那两行
由 `load_config()` 当未知键忽略、回写时原样留着），
`--groups` / `--strategies` / `--list-groups` / `--scorecard` 四个命令行入口
**也一起删掉了**（它们服务的策略组机制与策略引擎已经不存在）。

所以这个文件现在只守三件事：
1. **勾一条公式 → 它决定选谁**（信号表与池子的来源都是 `公式·<名字>`）；
2. **老配置里的退役键不拦路、不被改坏**（文件字节不变）；
3. `--once` 幂等、`--pool` 能打出「来源」列。
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

    ⚠️ 别把这份 fixture 读成"没 Key 也能匹配"（2026-09-18 用户核实后要求写准）：
    这份库是**手工造的就绪库** —— 复权事件与行业归属都是这里显式写进去的（只有同花顺那条
    dump 路给得到这两样），`hithink_api_key = ""` 只是为了让用例不发任何请求。
    真实情形是：**没 Key 时只能用免 Key 公开源，而公开源给不出这两样，匹配永远过不了
    数据闸门**（见 `tests/test_public_sync.py` 里
    `test_public_source_data_can_never_pass_the_stock_picking_gate`）。
    本文件测的是"数据已经就绪之后，匹配/覆盖参数本身对不对" —— 那是纯本地的事。
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


def test_formula_decides_the_picks(capsys, seeded, tmp_path, monkeypatch) -> None:
    """候选由**勾选的公式**决定；配置里那两行退役键（`enabled_groups` 等）不参与。

    勾一条 `C>10`（小库里只有 600003 在 10 元以上）：票出现在这一轮的结果里、来源写
    `公式·反转`，信号表里也正是这一轮跑的公式；但**不再自动落进 stock_pool**
    （2026-09-21 起：要不要留下由用户在结果页面点【加入自选】）。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
    args = ["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]
    before = seeded["config"].read_text(encoding="utf-8")

    assert cli(args) == 0
    out = capsys.readouterr().out
    assert "反转样本(600003)" in out
    with storage.connect(seeded["cfg"].db_path) as conn:
        strategies = {r[0] for r in conn.execute("SELECT DISTINCT strategy FROM signal")}
        pool_strategies = {r[0] for r in conn.execute(
            "SELECT DISTINCT strategy FROM stock_pool")}
    assert strategies == {"公式·反转"}            # 信号表里就是这一轮跑的公式
    # 2026-09-21（主人要求"匹配结果不自动加入股池"）：`stock_pool` 里**不再有它**
    assert pool_strategies == set()
    # 老配置里的退役键**原样留着**（用户文件不被改坏），文件字节不变
    assert seeded["config"].read_text(encoding="utf-8") == before


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
    assert "本次按勾选的策略匹配：低价" in out
    # 候选由勾的公式决定：库里 signal 表记着这一轮的结果（stock_pool 不再自动落它）
    with storage.connect(seeded["cfg"].db_path) as conn:
        picked = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM signal")}
        pool_symbols = {r[0] for r in conn.execute("SELECT symbol FROM stock_pool")}
    assert picked == {"600001"}
    assert pool_symbols == set()


def test_pool_command_shows_the_source_column(capsys, seeded,
                                             tmp_path, monkeypatch) -> None:
    """`--pool` 打出池子表；来源列现在是 `自选`（匹配结果不再自动进池，见下）。

    2026-09-21（主人要求"匹配结果不自动加入股池"）：`stock_pool` 里只剩**自选** ——
    所以这条用例先加一只自选（这一只既是自选、又恰好被勾的公式选中 → 来源是
    `公式·反转+自选`），再断言 `--pool` 打得出来源列。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
    with storage.connect(seeded["cfg"].db_path) as conn:
        # 打开监控（2026-10-05 起：加自选默认不提醒、池子里的票默认不盯）
        storage.upsert_watchlist(conn, "600003", name="反转样本", enabled=True)
    assert cli(["--cli", "--once", "--no-notify", "--config", str(seeded["config"])]) == 0
    capsys.readouterr()
    assert cli(["--cli", "--pool", "--config", str(seeded["config"])]) == 0
    out = capsys.readouterr().out
    assert "股票池" in out
    assert "600003" in out
    assert "自选" in out                      # 来源列里写着它是自选


def test_once_is_idempotent_via_cli(capsys, seeded, tmp_path, monkeypatch) -> None:
    """连续两次 `--once`：信号行数不变、不产生重复行（CLI 与定时任务同口径）。

    2026-09-21 起"池子行"由自选决定，所以这里加一只自选来盯住"不重复落库"这件事。
    """
    _enable_formulas(monkeypatch, tmp_path, seeded["cfg"], seeded["config"], {"反转": "C>10"})
    with storage.connect(seeded["cfg"].db_path) as conn:
        # 打开监控（2026-10-05 起：加自选默认不提醒、池子里的票默认不盯）
        storage.upsert_watchlist(conn, "600003", name="反转样本", enabled=True)
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
    assert first[0] > 0 and first[1] == 1        # 信号有；池子里就是那一只自选
