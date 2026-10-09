"""「连续 N 日确认」（`signal_confirm_days`）：把阈值型条件的**抖动**挡掉。

为什么要这一层（2026-10-08 主人定的口径）
---------------------------------------
`C > MA(C,20)` 这类条件在均线附近会今天命中、明天不命中，候选名单跟着抖 ——
用户看到的是"同一套策略，昨天选出 8 只、今天 2 只、明天又 7 只"，还分不清是行情变了
还是公式在抽搐。要求"最近 N+1 个交易日都命中"就能把这类抖动挡掉大半。

这个文件钉四件事：
1. `formula.confirmed()` 本身的口径（缺值不送分、历史不够不送分）；
2. **试算（`preview_hits`）与匹配（`run_enabled_formulas`）用同一份判断** ——
   两处结论不一致是这个项目里最难查的一类 bug；
3. 默认（0）**行为一点不变**（回归）；配置写错/超范围时夹紧到合法区间；
4. 名单变短时界面上能看出原因（来源里带「连续 N 日确认」、试算的提示里有一句话）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from laoa_trader import formulas as lib
from laoa_trader.data import storage
from laoa_trader.config import Config
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group

from tests.conftest import workdays_ending

#: 收盘价序列刻意做成"只在最后一根命中"与"一直命中"两种：
#:   * `600001` 一路涨 → `C>MA(C,3)` 每根都成立（**确认得住**）；
#:   * `600002` 最后一根才冒头（10,10,10,10,9,11 → 末根 11>10 成立、前一根 9<9.67 不成立）
#:     —— 这就是"抖一下"的典型形态，开了确认就该被挡掉。
_FLIP_CLOSES = (10.0, 10.0, 10.0, 10.0, 9.0, 11.0)
_STEADY_CLOSES = (10.0, 10.2, 10.4, 10.6, 10.8, 11.0)

#: 公式就用最朴素的一条：收盘价站上 3 日均线
BODY = "C>MA(C,3)"


@pytest.fixture()
def flip_db(tmp_path: Path) -> str:
    """6 个交易日的小库：一只"一直命中"、一只"最后一根才命中"。"""
    path = storage.init_db(tmp_path / "trader.db")
    days = workdays_ending("2026-09-11", len(_FLIP_CLOSES))
    with storage.connect(path) as conn:
        storage.write_stock_basic(conn, [("600001", "稳定样本", "银行"),
                                         ("600002", "抖动样本", "银行")])
        rows = []
        for symbol, closes in (("600001", _STEADY_CLOSES), ("600002", _FLIP_CLOSES)):
            for day, close in zip(days, closes):
                rows.append((symbol, day, close * 0.99, close * 1.01, close * 0.98,
                             close, 1_000_000.0, close * 1_000_000.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
    return str(path)


@pytest.fixture()
def confirm_cfg(cfg: Config, tmp_path: Path) -> Config:
    """带 config.toml 落点的配置（勾选公式要写回文件，必须有落点）。"""
    cfg.source_path = tmp_path / "config.toml"
    return cfg


def _formula_dir(tmp_path: Path, name: str = "抖动测试") -> Path:
    """把一条公式写进临时目录（走库自己的写文件口径，不手拼文本）。"""
    folder = tmp_path / "formulas"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.txt").write_text(
        lib.formula_text(name, BODY, "连续确认用例"), encoding="utf-8")
    return folder


# ══════════════════════════════════════════════════════════════════════════
# 1) 判断本身
# ══════════════════════════════════════════════════════════════════════════


def test_confirmed_zero_days_looks_at_the_last_bar_only() -> None:
    """`days=0`（默认）就是老口径：只看最后一根。"""
    assert fm.confirmed(np.array([False, False, False, True]), 0) is True
    assert fm.confirmed(np.array([True, True, True, False]), 0) is False
    # 空序列（新股/空库）：不产生信号，也不炸
    assert fm.confirmed(np.zeros(0, dtype=bool), 0) is False
    assert fm.confirmed(np.zeros(0, dtype=bool), 2) is False


def test_confirmed_requires_every_recent_bar() -> None:
    """要求最近 `days+1` 根**都**命中；中间断一根就不算确认。"""
    assert fm.confirmed(np.array([True, True, True]), 2) is True
    assert fm.confirmed(np.array([True, False, True]), 2) is False
    # 更早那根断掉不影响（只看窗口内）
    assert fm.confirmed(np.array([False, True, True, True]), 2) is True
    # 历史长度不够 `days+1` ⇒ 不算确认（宁可不出票，也不拿残缺窗口当"确认过"）
    assert fm.confirmed(np.array([True, True]), 2) is False


def test_confirmed_treats_missing_values_as_not_hit() -> None:
    """缺值算不命中。

    这条为什么重要：`Formula.eval` 把 NaN 折成 `False`（停牌、窗口不足），
    但**确认这一层自己也要经得起 NaN**（万一有人直接喂原始数组）——
    否则"数据不足"会被当成"一直在命中"，白送一个真信号。
    """
    assert fm.confirmed(np.array([np.nan, 1.0, 1.0]), 2) is False
    assert fm.confirmed(np.array([0.0, 1.0, 1.0]), 2) is False


def test_confirm_days_of_clamps_and_survives_bad_config() -> None:
    """配置写错不许崩、也不许变成"要求 100 天"：一律夹到 0~5。"""
    cfg = Config()
    assert formula_group.confirm_days_of(cfg) == 0            # 默认关
    cfg.signal_confirm_days = 3
    assert formula_group.confirm_days_of(cfg) == 3
    cfg.signal_confirm_days = -2
    assert formula_group.confirm_days_of(cfg) == 0
    cfg.signal_confirm_days = 99
    assert formula_group.confirm_days_of(cfg) == 5
    cfg.signal_confirm_days = "两"                              # type: ignore[assignment]
    assert formula_group.confirm_days_of(cfg) == 0


# ══════════════════════════════════════════════════════════════════════════
# 2) 试算与匹配：同一份判断
# ══════════════════════════════════════════════════════════════════════════


def test_preview_hits_default_keeps_the_old_behaviour(flip_db: str, confirm_cfg: Config) -> None:
    """默认（`signal_confirm_days = 0`）：抖一下的那只**照旧**被选中 —— 行为不变。"""
    formula = fm.compile_formula(BODY)
    after_close = datetime(2026, 9, 11, 20, 0)

    result = lib.preview_hits(formula, flip_db, cfg=confirm_cfg, now=after_close)

    assert [hit["symbol"] for hit in result["hits"]] == ["600001", "600002"]
    assert all("确认" not in note for note in result["notes"])


def test_preview_hits_drops_the_wobbler_when_confirmation_is_on(
    flip_db: str, confirm_cfg: Config,
) -> None:
    """开了 1 天确认：只保留"最近两根都命中"的那只，并在提示里说清原因。"""
    confirm_cfg.signal_confirm_days = 1
    formula = fm.compile_formula(BODY)
    after_close = datetime(2026, 9, 11, 20, 0)

    result = lib.preview_hits(formula, flip_db, cfg=confirm_cfg, now=after_close)

    assert [hit["symbol"] for hit in result["hits"]] == ["600001"]
    assert result["count"] == 1
    assert any("连续确认" in note and "2 个交易日" in note for note in result["notes"]), \
        result["notes"]
    assert any("signal_confirm_days" in note for note in result["notes"]), result["notes"]


def test_matching_and_preview_agree(flip_db: str, confirm_cfg: Config, tmp_path: Path) -> None:
    """**同一条公式、同一份数据，【运行】与【开始筛选】选出同一批票。**

    这是这一层最该钉的一条：两处各写一份判断，迟早出现"试算说 5 只、匹配只有 2 只"，
    而用户没有任何办法解释。
    """
    folder = _formula_dir(tmp_path)
    confirm_cfg.enabled_formulas = ["抖动测试"]
    confirm_cfg.signal_confirm_days = 1
    after_close = datetime(2026, 9, 11, 20, 0)

    preview = lib.preview_hits(fm.compile_formula(BODY), flip_db,
                               cfg=confirm_cfg, now=after_close)
    run = formula_group.run_enabled_formulas(flip_db, confirm_cfg, directory=folder)

    preview_symbols = [hit["symbol"] for hit in preview["hits"]]
    picked = run.picks[formula_group.formula_strategy_name("抖动测试")]
    assert preview_symbols == [pick["symbol"] for pick in picked] == ["600001"]


def test_matching_reason_says_why_the_list_is_shorter(
    flip_db: str, confirm_cfg: Config, tmp_path: Path,
) -> None:
    """名单变短时，来源那一列要能看出是**自己的确认设置**造成的。"""
    folder = _formula_dir(tmp_path)
    confirm_cfg.enabled_formulas = ["抖动测试"]
    confirm_cfg.signal_confirm_days = 1

    run = formula_group.run_enabled_formulas(flip_db, confirm_cfg, directory=folder)

    reason = run.picks[formula_group.formula_strategy_name("抖动测试")][0]["reason"]
    assert "连续 2 日确认" in reason

    # 默认（关闭确认）时，来源里不该多这句 —— 没开的东西不许写在界面上
    confirm_cfg.signal_confirm_days = 0
    run2 = formula_group.run_enabled_formulas(flip_db, confirm_cfg, directory=folder)
    reason2 = run2.picks[formula_group.formula_strategy_name("抖动测试")][0]["reason"]
    assert "确认" not in reason2
