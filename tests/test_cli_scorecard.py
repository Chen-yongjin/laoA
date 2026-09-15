"""`--scorecard` 命令行：打印成绩单、落盘 CSV/Markdown、退出码语义。

为什么单独测 CLI：用户抱怨"策略选出的股票很垃圾"时，他手里能拿到的第一手证据
就是命令行输出。所以这个入口必须做到：

- **样本不足时退出码是 1 并且说明原因**（绝不能打印一张漂亮的表就退出 0）；
- **持有期写 0（T+0）时退出码是 2**（用法错误：T+0 在 A 股不可执行）；
- 有结论时退出码 0，并把 CSV（主表 + 逐年明细）与 Markdown 写到 `--out`。

全部离线：合成小库，不联网。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from laoa_trader.__main__ import cli
from laoa_trader.data import storage

from tests._toml import p


def _days(count: int, end: str = "2026-09-11") -> list[str]:
    cursor = date.fromisoformat(end)
    out: list[str] = []
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _seed_flat_market(db_path, symbols: int, days: int, base_price: float = 3.0) -> list[str]:
    """造一个"低价股策略能稳定出候选"的库：足够多的票、足够长的历史、价格 ≥2 元。

    为什么这样造：成绩单要走到"给出结论"（退出码 0），就必须有 ≥30 笔样本、
    ≥2 个信号日；低价股策略的条件最容易在小库上满足（股价≥2元、日均额≥3000万、非跌停）。
    """
    trading = _days(days)
    storage.init_db(db_path)
    with storage.connect(db_path) as conn:
        storage.write_stock_basic(conn, [
            (f"60{i:04d}", f"样本{i}", "测试行业") for i in range(1, symbols + 1)
        ])
        rows = []
        for i in range(1, symbols + 1):
            price = base_price + i * 0.05        # 价格各不相同 → 排序确定
            for day in trading:
                rows.append((f"60{i:04d}", day, price, price * 1.001, price * 0.999,
                             price, 1e6, 1e8))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, trading)
        storage.write_adjust_events(conn, [("600001", trading[0], 0.0, 0.0, 0.0, 0.0)])
    return trading


@pytest.fixture()
def scorecard_config(tmp_path, cfg):
    """指向临时数据目录的 config.toml。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{p(cfg.data_dir)}"\n'
        'hithink_api_key = ""\n'
        "notify_feishu = false\nnotify_windows = false\nnotify_tray = false\n",
        encoding="utf-8",
    )
    return str(path)


def test_scorecard_reports_insufficient_samples_with_exit_code_1(
    cfg, scorecard_config, tmp_path, capsys
):
    """样本太少：退出码 1 + 说清原因（并给出库里的日期范围与行数）。"""
    _seed_flat_market(cfg.db_path, symbols=6, days=6)
    out_dir = tmp_path / "成绩单"
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(out_dir)])
    text = capsys.readouterr().out
    assert code == 1
    assert "样本不足" in text
    assert "个交易日" in text                    # 库里有多少数据必须写出来
    assert "T+0" in text and "不可执行" in text   # 口径说明是表格的一部分
    # 该写的文件照样写（用户要能拿到"为什么没结论"的书面记录）
    assert list(out_dir.glob("*.csv"))
    assert list(out_dir.glob("*.md"))


def test_scorecard_gives_conclusion_with_exit_code_0(cfg, scorecard_config, tmp_path, capsys):
    """样本足够：退出码 0，**默认并列输出 A/B/C 三套口径**，并落盘 CSV + Markdown。"""
    # 库级门槛要求 ≥100 只 / ≥250 个交易日：这里造 110 只 × 300 天
    _seed_flat_market(cfg.db_path, symbols=110, days=300)
    out_dir = tmp_path / "成绩单"
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(out_dir)])
    text = capsys.readouterr().out
    assert code == 0, text
    # 三套主口径都在（开盘买 / 尾盘买 / 隔夜），且都带自己的进场出场说明
    assert "A 开盘买·隔日收盘卖" in text
    assert "B 尾盘买·隔日收盘卖" in text
    assert "C 尾盘买·隔夜开盘卖" in text
    assert "D+1开盘买 → D+2收盘卖" in text and "D+1收盘买 → D+2收盘卖" in text
    assert "结论：开盘买(A) vs 尾盘买(B)" in text
    assert "平均α" in text and "t值" in text and "正边际年" in text
    assert "剔开板" in text and "剔尾板" in text
    assert "已写出" in text

    main_csv = sorted(out_dir.glob("策略成绩单_*.csv"))
    verdict_csv = sorted(out_dir.glob("策略成绩单_*_结论.csv"))
    year_csv = sorted(out_dir.glob("策略成绩单_*_逐年明细.csv"))
    report = sorted(out_dir.glob("策略成绩单_*.md"))
    assert main_csv and verdict_csv and year_csv and report
    body = main_csv[0].read_text(encoding="utf-8-sig")
    assert "avg_alpha_pct" in body and "dropped_limit_up" in body
    assert "dropped_limit_up_close" in body and "convention" in body
    assert "低价股" in body                       # 低价股策略在这种库上一定有样本
    verdict_body = verdict_csv[0].read_text(encoding="utf-8-sig")
    assert "a_alpha_pct" in verdict_body and "verdict" in verdict_body
    assert "T+0" in report[0].read_text(encoding="utf-8")


def test_scorecard_accepts_horizons_for_the_old_convention(cfg, scorecard_config,
                                                          tmp_path, capsys):
    """`--horizons 1,3` 仍走旧口径（D+1 开盘买），便于与 NAS 的历史结论对齐。"""
    _seed_flat_market(cfg.db_path, symbols=110, days=300)
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(tmp_path / "out"), "--horizons", "1,3"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "T+1（D+1 开盘买 → D+2 收盘卖）" in text
    assert "A 开盘买·隔日收盘卖" not in text      # 旧口径家族里没有 A/B/C 这些键


def test_scorecard_rejects_t0_horizon(cfg, scorecard_config, tmp_path, capsys):
    """`--horizons 0` 是 T+0（当天买当天卖），退出码 2 并说明"为什么不可执行"。"""
    _seed_flat_market(cfg.db_path, symbols=6, days=6)
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(tmp_path / "out"), "--horizons", "0"])
    text = capsys.readouterr().out
    assert code == 2
    assert "不可执行" in text and "T+1" in text
    assert "合法示例" in text


def test_scorecard_handles_missing_db(cfg, scorecard_config, tmp_path, capsys):
    """指向一个不存在的库：退出码 1 + 明确原因（不吐 traceback）。"""
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "out")])
    text = capsys.readouterr().out
    assert code == 1
    assert "无法评估" in text and "库不存在" in text


def test_scorecard_can_be_limited_to_one_strategy(cfg, scorecard_config, tmp_path, capsys):
    """`--strategies 低价股` 只评估这一条（评估底座也要能单点用）。"""
    _seed_flat_market(cfg.db_path, symbols=110, days=300)
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(tmp_path / "out"), "--strategies", "低价股",
                "--horizons", "1"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "只评估" in text
    assert "低价股" in text
    assert "连板回踩低吸" not in text.split("── T+1")[-1]


def test_scorecard_warns_about_a_short_sample() -> None:
    """3 年库（约 730 个交易日）要提示"逐年稳定性仅供参考"；长样本不提示。

    为什么必须提示：分发包默认只导入 3 年，逐年分组只剩 3 个桶 ——
    「每一年都为正 ✅」的说服力和 10 年样本不是一回事，用户据此决定策略去留时得知道。
    """
    from laoa_trader.research import scorecard as sc

    short = {"days": 730, "start": "2023-01-03", "end": "2025-12-31"}
    note = sc.sample_note(short)
    assert "3 年样本较短，逐年稳定性仅供参考" in note
    assert "2.9 年" in note or "2.8 年" in note          # 用交易日折算，不是日历跨度
    assert "history_years" in note                        # 想要长样本怎么改也说了

    assert sc.sample_note({"days": 2500}) == ""           # 10 年样本不提示
    assert sc.sample_note({}) == ""                       # 没数据不硬凑
    assert sc.sample_note({"days": 0}) == ""


def test_short_sample_note_lands_in_the_report(cfg, scorecard_config, tmp_path,
                                               capsys) -> None:
    """3 年库跑成绩单：控制台与 Markdown 报告里都要有那句提示。"""
    _seed_flat_market(cfg.db_path, symbols=110, days=300)     # 300 个交易日 ≈ 1.2 年
    out_dir = tmp_path / "成绩单"
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(out_dir), "--groups", "swing"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "逐年稳定性仅供参考" in text
    report = sorted(out_dir.glob("策略成绩单_*.md"))[0].read_text(encoding="utf-8")
    assert "逐年稳定性仅供参考" in report


def test_scorecard_defaults_to_all_strategies(cfg, scorecard_config, tmp_path,
                                              capsys) -> None:
    """成绩单**默认评全部策略**（含已停用的组），只有显式 `--groups/--strategies` 才收窄。

    为什么不跟着选股开关走：默认策略集只开 `short`，跟着走就会只评 3 条 ——
    而成绩单正是"决定谁该留"的依据，被停用那两组的数字恰恰是最需要看到的。
    """
    _seed_flat_market(cfg.db_path, symbols=110, days=300)
    code = cli(["--cli", "--config", scorecard_config, "--scorecard",
                "--out", str(tmp_path / "out")])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "全部策略" in text and "不受选股开关限制" in text
    assert "只评估" not in text
    assert "连板回踩低吸" in text                     # 停用组（ultra）也在评估范围内
