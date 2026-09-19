"""内置策略 ↔ 随包公式：同一条规则在两条链路上必须选出**同一批票**。

为什么要这条测试
----------------
2026-09-18 起，5 条内置策略里活下来的 3 条（`ReversalStrategy` 短期反转、
`DryUpExpansionStrategy` 地量后放量变盘、`FirstLimitUpStrategy` 首板缩量整理）
从「写死在 `strategy/rules.py` 里的 Python」改成了「`formulas/` 下用户可改可删的公式」。

于是同一套选股规则有了**两条执行链路**：

* Python 链路：`rules.<Strategy>().run()` —— pandas 面板 + 排序取前 N；
* 公式链路：`formulas/*.txt` —— 公式引擎（numpy）→ `preview_hits` / `run_enabled_formulas`。

两条链路给用户的是**同一个答案**（"今天该盯哪几只"），所以它们一旦分叉，用户看到的
就是"勾了公式和没勾公式，池子里的票不一样"——而这是最难被发现的一类错：两边都不报错、
都有结果，只是结果不同。公式又是**用户会自己改**的东西，改完是不是还跟内置策略等价，
只能靠这条测试盯住。

怎么测
------
1. 造一个**足够长**的合成库：70 个交易日（`地量后放量变盘` 用到 `MA(REF(V,1),20)`，
   21 根 K 线才出得来第一个值）、18 只票；
2. 每条策略都有「应当命中」的样本，和「只差一个条件、所以不该命中」的对照样本
   （对照的写法见 `parity_db` 里逐个票的注释）；
3. 同一个库上分别跑两条链路，断言**选票集合相等**；
4. 还断言集合的**具体内容**（例如短期反转必须恰好选中 600101）——
   否则样本哪天被改坏了，"两边都空"也能让测试通过；
5. 最后刻意制造一个已知差异（把公式里的 `ST=0` 删掉），断言两边**不再相等**：
   证明这条测试真的有分辨能力，而不是一条永远通过的废测试。

已知、且本测试认可的差异
------------------------
* **排序取前 N 那一层做不到**：Python 每条策略都按自己的因子排序后取 `top_n=30`，
  而公式语言**没有排名/取前 N 的函数**（见 `formula.FUNCTIONS` 白名单，只有
  MA/REF/HHV/COUNT/CROSS… 这类逐票计算的函数）。所以公式返回的是**全量**命中，
  本测试只在"集合"上比、不比较顺序，并且把票数控制在 30 只以内
  （`MAX_PER_STRATEGY=30` 是 Python 侧的截断点；"每条公式最多进池 3 只"那道闸在
  `pool` / `formula_group.MAX_PER_FORMULA` 里，也不在公式里）。
  样本只造 18 只票，正是为了让 `top_n=30` 永远截不到东西 —— 一旦有人把票数加到 30 以上，
  这两条链路的差异就不再是公式的错，届时应该把 `top_n` 显式调大并在这里写清原因。
* **ST 的口径只对齐到"名称含 ST"**：Python 侧靠 `factors.exclude_risk()`，它排掉的是
  名称含 `ST` **或 `退`** 的票；公式侧靠 `ST=0`（`formula.is_st_name()`，只认 `ST`）。
  本库没有名称含 `退` 的样本，所以两边一致；真有退市整理股时，公式会把它选进来
  （想把口径收紧，就在公式里再加一条条件）。
* 这三条公式都**不依赖本地涨停池**（没用 `连板()` / `涨停天数()`），
  所以本测试不需要往 `limit_up_pool` 里造数据 —— 依赖涨停池的那条
  （连板回踩低吸）已经删掉了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from laoa_trader import formulas as lib
from laoa_trader.data import storage
from laoa_trader.data.engine import DataEngine
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group, groups, rules
from tests.conftest import workdays_ending

#: 随包公式目录（仓库根 `formulas/`；源码运行与打包前是同一份）
FORMULA_DIR = Path(__file__).resolve().parents[1] / "formulas"

#: 交易日数。必须 > 21：`地量后放量变盘` 用 `MA(REF(V,1),20)`，21 根才出第一个非缺值。
BAR_COUNT = 70

#: 最后一天（与其它用例同一个锚点，避免"周末"影响样本）
LAST_DAY = "2026-09-11"

#: 交易日序列（升序）
DAYS = workdays_ending(LAST_DAY, BAR_COUNT)

#: 公式名 → 对应的内置策略类名（正好是这次"改成随包公式"的三条）
PARITY: dict[str, str] = {
    "短期反转": "ReversalStrategy",
    "地量后放量变盘": "DryUpExpansionStrategy",
    "首板缩量整理": "FirstLimitUpStrategy",
}

#: 每条公式**应当恰好**选中哪些票（钉住样本，见模块头"怎么测"第 4 条）
EXPECTED: dict[str, set[str]] = {
    "短期反转": {"600101"},
    "地量后放量变盘": {"600201"},
    "首板缩量整理": {"600301"},
}

#: 名称为 ST 的对照票：Python 的 `exclude_risk()` 与公式的 `ST=0` 都该把它挡掉。
#: 它们的行情与"命中样本"**完全一样**，唯一的区别就是名字 —— 所以一旦两边少了一侧，
#: 结果里就会冒出这只票。
ST_SAMPLES: dict[str, str] = {
    "短期反转": "600106",
    "地量后放量变盘": "600208",
    "首板缩量整理": "600306",
}

#: 每个对照票"唯一的毛病"是公式里的哪一段：(公式名, 对照票, 原文, 放宽后的写法)。
#:
#: 为什么需要这张表：对照票只有在**别的条件都对、只差它这条**时才有证明力。
#: 一只票要是同时坏在两条条件上（写这份样本时就真踩过：1.5 元的样本因为"成交量×价格"
#: 顺带把日均成交额压到了 1300 万），那"公式把价格门槛整条删掉"它也照样不命中 ——
#: 于是测试一片绿，而公式里少了一条门槛没人知道。所以：**把这条条件放宽一点，
#: 它就该从"不命中"变"命中"**，逐条断言。ST 那一条由 `test_parity_can_actually_fail` 覆盖。
REJECTION_CASES: list[tuple[str, str, str, str]] = [
    # 短期反转：600102 是涨的（不满足 20 日跌 10%）、600104 成交额只有 1000 万
    ("短期反转", "600102", "RET20<=-0.10", "RET20<=0.10"),
    ("短期反转", "600104", "AMT20>=30000000", "AMT20>=1000000"),
    # 地量后放量：600202 前面没缩量、600203 今日收阴、600204 涨幅 9.5% 超上限、
    # 600205 只有 1.5 元、600206 日均额只有 1000 万
    ("地量后放量变盘", "600202", "VOLMA*0.7", "VOLMA*1.5"),
    ("地量后放量变盘", "600203", "C>O AND ", ""),
    ("地量后放量变盘", "600204", "RET1<=0.09", "RET1<=0.10"),
    ("地量后放量变盘", "600205", "C>=2", "C>=1"),
    ("地量后放量变盘", "600206", "AMT20>=30000000", "AMT20>=1000000"),
    # 首板缩量整理：600302 今日继续涨 9.5%、600303 昨日之前也涨了 10%（连板不是首板）、
    # 600304 今日放量、600305 收在昨收的 0.94 倍（破了 0.95）
    ("首板缩量整理", "600302", "RET1<0.09", "RET1<0.10"),
    ("首板缩量整理", "600303", "PREV_RET2<0.095", "PREV_RET2<0.11"),
    ("首板缩量整理", "600304", "SHRINK:=V<REF(V,1)", "SHRINK:=V>REF(V,1)*0"),
    ("首板缩量整理", "600305", "REF(C,1)*0.95", "REF(C,1)*0.90"),
]

#: 合成库里一共几只票。**必须 < 30**：超过之后 Python 侧的 `top_n=30` 会截断，
#: 两边天然不等（见模块头"已知差异"第 1 条）。
SYMBOL_COUNT = 18


# ══════════════════════════════════════════════════════════════════════════
# 合成小库
# ══════════════════════════════════════════════════════════════════════════


def _flat(value: float) -> list[float]:
    """70 天的常数序列。"""
    return [float(value)] * BAR_COUNT


def _bars(
    symbol: str,
    closes: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
    turnover: float | None = None,
) -> list[tuple]:
    """按收盘/成交量造行情行。

    * `open` 默认等于当日收盘（`close>open` 这种形态要显式传 `opens`）；
    * `turnover` 默认 `volume × close`（真实口径），要单独控制"日均成交额"时显式传；
    * 每行 8 列 ⇒ `write_daily_raw` 把 `factor` 填成 1.0（后复权价 = 原始价）。
    """
    rows = []
    for index, day in enumerate(DAYS):
        close = float(closes[index])
        volume = float(volumes[index])
        open_ = float(opens[index]) if opens is not None else close
        amount = float(turnover) if turnover is not None else volume * close
        rows.append((symbol, day, open_, max(open_, close), min(open_, close), close,
                     volume, amount))
    return rows


def _decline(rate: float = 0.008) -> list[float]:
    """每日跌 `rate` 的收盘序列（70 天累计约 -43%，20 日窗口远低于 -10%）。"""
    return [10.0 * ((1 - rate) ** i) for i in range(BAR_COUNT)]


@pytest.fixture()
def parity_db(tmp_path: Path) -> str:
    """18 只票 × 70 个交易日的合成库 —— 每只票只为"某一条策略的某一个条件"而存在。

    命名规则：`6001xx` 给短期反转、`6002xx` 给地量后放量变盘、`6003xx` 给首板缩量整理；
    每组的最后一只（`6001**6 / 6002**8 / 6003**6`）是同形态的 ST 票。
    """
    path = storage.init_db(tmp_path / "trader.db")
    rows: list[tuple] = []
    names: dict[str, tuple[str, str]] = {}

    def add(symbol: str, name: str, bars: list[tuple]) -> None:
        names[symbol] = (name, "银行")
        rows.extend(bars)

    # ── 短期反转（20 日跌幅 ≤-10%、20 日均额 ≥3000 万、今日未跌停、非 ST）──
    # 命中：一路阴跌（20 日约 -15%），成交额 2e7×10 元 ≈ 2 亿
    add("600101", "反转命中", _bars("600101", _decline(), _flat(2e7)))
    # 对照：20 日涨 5%（不满足"跌 10%"）
    add("600102", "反转对照上涨",
        _bars("600102", [10.0 * (1.0025 ** i) for i in range(BAR_COUNT)], _flat(2e7)))
    # 对照：跌得够，但**今日跌停**（-10%）：不接飞刀
    limit_down = _decline()
    limit_down[-1] = limit_down[-2] * 0.90
    add("600103", "反转对照跌停", _bars("600103", limit_down, _flat(2e7)))
    # 对照：跌得够，但日均成交额只有 1000 万（进得去出不来）
    add("600104", "反转对照缺流动性",
        _bars("600104", _decline(), _flat(2e7), turnover=1e7))
    # 对照：形态与 600101 完全相同，但名字含 ST → exclude_risk / ST=0 都要挡掉
    add("600106", "ST反转样本", _bars("600106", _decline(), _flat(2e7)))

    # ── 地量后放量变盘 ──
    def dryup(
        symbol: str,
        name: str,
        *,
        price: float = 10.0,
        quiet: bool = True,
        last_volume: float = 3e7,
        gain: float = 0.03,
        open_ratio: float = 1.0,
        turnover: float | None = None,
    ) -> None:
        """`quiet=True` ⇒ 前 3 个交易日缩量到 2e6（20 日均量约 8.8e6 的 0.23 倍）。

        今日量 3e7 = 均量的 3.4 倍（≥1.5 倍）；`gain` 是今日涨幅；
        `open_ratio` ≠1 时把今开抬高（做"收阴"的对照）。
        """
        closes = _flat(price)
        closes[-1] = price * (1 + gain)
        volumes = _flat(1e7)
        if quiet:
            for index in (BAR_COUNT - 4, BAR_COUNT - 3, BAR_COUNT - 2):
                volumes[index] = 2e6
        volumes[-1] = last_volume
        opens = _flat(price)
        if open_ratio != 1.0:
            opens[-1] = price * open_ratio
        add(symbol, name, _bars(symbol, closes, volumes, opens, turnover))

    # 命中：前 3 日地量 + 今日放量收阳涨 3%
    dryup("600201", "地量命中")
    # 对照：今日也放量了，但**前面不地量**（前 3 日量就是常态 1e7）
    dryup("600202", "地量对照未缩量", quiet=False)
    # 对照：地量 + 放量，但**今日收阴**（开 10.5 → 收 10.3）
    dryup("600203", "地量对照收阴", open_ratio=1.05)
    # 对照：地量 + 放量 + 收阳，但今日涨 9.5%（超过 9%，已不是"变盘"而是涨停）
    dryup("600204", "地量对照涨幅超标", gain=0.095)
    # 对照：形态全对，但股价 1.5 元（低于 2 元门槛）。
    # ⚠️ 必须显式给成交额：1.5 元 × 平常量算出来的日均额只有 1300 万，
    # 那样它就**同时**坏在"价太低"和"额不够"两条上，证明不了价格那一条被实现了
    # （`test_every_condition_is_really_implemented` 会盯住这件事）。
    dryup("600205", "地量对照低价", price=1.5, turnover=6e7)
    # 对照：形态全对，但日均成交额只有 1000 万
    dryup("600206", "地量对照缺额", turnover=1e7)
    # 对照：形态与 600201 相同，名字含 ST
    dryup("600208", "ST地量样本")

    # ── 首板缩量整理（昨日首板 + 今日缩量不破位）──
    def first_board(
        symbol: str,
        name: str,
        *,
        board_gain: float = 0.098,
        today_ratio: float = 0.97,
        prev_volume: float = 1e7,
        today_volume: float = 4e6,
        day_before_gain: float = 0.0,
    ) -> None:
        """`day_before_gain` = 再前一日（index -3）的涨幅，决定"是不是首板"。

        默认：再前一日 0%、昨日 +9.8%（首板）、今日 -3% 且缩量到 0.4 倍。
        """
        closes = _flat(10.0)
        closes[BAR_COUNT - 3] = 10.0 * (1 + day_before_gain)
        closes[BAR_COUNT - 2] = closes[BAR_COUNT - 3] * (1 + board_gain)
        closes[BAR_COUNT - 1] = closes[BAR_COUNT - 2] * today_ratio
        volumes = _flat(5e6)
        volumes[BAR_COUNT - 2] = prev_volume
        volumes[BAR_COUNT - 1] = today_volume
        add(symbol, name, _bars(symbol, closes, volumes))

    # 命中：昨日 +9.8%（首板）、今日 -3% 缩量收在昨收的 0.97 倍
    first_board("600301", "首板命中")
    # 对照：昨日首板，但今日**继续涨 9.5%**（不是"整理"）
    first_board("600302", "首板对照继续涨", today_ratio=1.095)
    # 对照：昨日也涨了 10% → 是**连板**不是首板
    first_board("600303", "首板对照二连板", day_before_gain=0.10)
    # 对照：形态全对，但今日**放量**（2e7 > 昨日 1e7）
    first_board("600304", "首板对照放量", today_volume=2e7)
    # 对照：今日缩量了，但收在昨收的 0.94 倍（跌破 0.95 下限）
    first_board("600305", "首板对照破位", today_ratio=0.94)
    # 对照：形态与 600301 相同，名字含 ST
    first_board("600306", "ST首板样本")

    assert len(names) == SYMBOL_COUNT, f"样本票数应为 {SYMBOL_COUNT}，实际 {len(names)}"
    assert SYMBOL_COUNT < 30, "票数必须 < 30，否则 Python 侧的 top_n 会截断（见模块头）"

    with storage.connect(path) as conn:
        storage.write_stock_basic(conn, [(s, n, ind) for s, (n, ind) in names.items()])
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, DAYS)
    return str(path)


@pytest.fixture()
def parity_formulas() -> dict[str, fm.Formula]:
    """编译好的三条随包公式（**从仓库的 `formulas/` 读**，不是把正文抄进测试）。

    为什么坚持读文件：这条测试要证明的正是"**随包文件**与 Python 策略等价"。
    把公式正文抄一份进测试，文件被改坏了测试反而照样通过。
    """
    specs = {spec.name: spec for spec in lib.formula_files(FORMULA_DIR)}
    out: dict[str, fm.Formula] = {}
    for name in PARITY:
        spec = specs.get(name)
        assert spec is not None, f"随包公式 {name}.txt 不在 {FORMULA_DIR} 里"
        assert spec.ok and spec.formula is not None, f"{name}.txt 编译失败：{spec.error_text}"
        out[name] = spec.formula
    return out


# ══════════════════════════════════════════════════════════════════════════
# 等价性
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("formula_name", sorted(PARITY))
def test_bundled_formula_matches_python_strategy(
    parity_db: str, parity_formulas: dict[str, fm.Formula], formula_name: str
) -> None:
    """`formulas/<名字>.txt` 选出的票 == `rules.<类名>().run()` 选出的票。

    两边跑的是同一个库、同一批票、同一根"最后一根 K 线"，所以集合必须**逐个元素相等**。
    """
    class_name = PARITY[formula_name]
    engine = DataEngine(parity_db)

    python_hits = set(rules.STRATEGIES[class_name](engine=engine).run())
    result = lib.preview_hits(parity_formulas[formula_name], parity_db)
    formula_hits = {hit["symbol"] for hit in result["hits"]}

    # 先钉住样本本身：万一样本被改坏（比如命中票不再命中），先在这里失败，
    # 而不是在一个"两边都空"的假通过里蒙混过去
    assert python_hits == EXPECTED[formula_name], (
        f"{class_name} 的样本跑偏了：期望 {EXPECTED[formula_name]}，实际 {python_hits}"
    )
    # 试算本身不该报错（报错会被吞进 errors/notes，命中数因此偏少也会"恰好两边相等"）
    assert result["errors"] == []
    assert result["skipped"] == 0          # 没有哪只票因为"数据不足/最后一根不是最新日"被跳过
    # ★ 核心断言：两条链路必须一模一样
    assert formula_hits == python_hits


@pytest.mark.parametrize("formula_name", sorted(PARITY))
def test_parity_can_actually_fail(
    parity_db: str, parity_formulas: dict[str, fm.Formula], formula_name: str
) -> None:
    """把公式里的 `ST=0` 删掉，两边就**必须不一致** —— 证明上一条测试抓得住分叉。

    为什么专门写这一步：等价性断言最怕变成"永远通过"的废测试（两边都空、断言写反、
    或者 fixture 坏了）。这里刻意制造一个**已知的**差异 —— 公式不再排除 ST 股，
    而 Python 侧照旧 `exclude_risk()` —— 如果这样都还"相等"，说明这条测试没有分辨能力。
    顺带把 `ST=0` ↔ `exclude_risk()` 的对应关系钉死：ST 对照票的行情与命中票完全一样，
    唯一的区别是名字。
    """
    formula = parity_formulas[formula_name]
    class_name = PARITY[formula_name]

    tampered = formula.text.replace(" AND ST=0", "")
    assert tampered != formula.text, "样本公式里没有 `AND ST=0`，改动没生效"
    mutated = fm.compile_formula(tampered, name=f"{formula_name}（去掉 ST=0）")

    python_hits = set(rules.STRATEGIES[class_name](engine=DataEngine(parity_db)).run())
    mutated_hits = {hit["symbol"] for hit in lib.preview_hits(mutated, parity_db)["hits"]}

    st_symbol = ST_SAMPLES[formula_name]
    assert mutated_hits != python_hits          # 差异必须被抓到
    assert st_symbol in mutated_hits            # 去掉 ST=0 之后 ST 对照票就进来了
    assert st_symbol not in python_hits         # 而 Python 侧永远排掉它


@pytest.mark.parametrize(
    ("formula_name", "symbol", "original", "relaxed"),
    REJECTION_CASES,
    ids=[f"{case[0]}-{case[1]}" for case in REJECTION_CASES],
)
def test_every_condition_is_really_implemented(
    parity_db: str,
    parity_formulas: dict[str, fm.Formula],
    formula_name: str,
    symbol: str,
    original: str,
    relaxed: str,
) -> None:
    """对照票只差**某一条**条件就该被挡掉：把那条放宽一点，它必须马上变成命中。

    这条测的是"公式真的实现了每一条门槛"，而不只是"和 Python 碰巧一样"：
    少写一条门槛时，只要 Python 侧也恰好不选它，等价性断言是看不出来的。
    所以这里逐条构造"放宽一点 → 该票出现"的对照（见 `REJECTION_CASES`）。
    """
    formula = parity_formulas[formula_name]
    assert original in formula.text, f"{formula_name} 的正文里找不到 {original!r}"
    relaxed_formula = fm.compile_formula(
        formula.text.replace(original, relaxed), name=f"{formula_name}（放宽一条）"
    )

    baseline = {hit["symbol"] for hit in lib.preview_hits(formula, parity_db)["hits"]}
    assert symbol not in baseline, f"{symbol} 本来就不该命中，样本已经跑偏"
    widened = {hit["symbol"] for hit in lib.preview_hits(relaxed_formula, parity_db)["hits"]}
    assert symbol in widened, (
        f"把 {original!r} 放宽成 {relaxed!r} 之后 {symbol} 仍然不命中 —— "
        f"说明它挡掉的原因不是这一条（对照样本失去了证明力）"
    )


def test_formula_group_path_selects_the_same_tickets(parity_db: str, cfg) -> None:
    """走**真正进池子**的那条路（`formula_group.run_enabled_formulas`）结果也一样。

    为什么还测一遍：`preview_hits` 是"试算"（界面上的按钮），而选股时走的是
    `run_enabled_formulas`（逐票扫一遍、按公式聚合成池子候选）。两者虽然共用
    `formula.py` 引擎，但入口不同 —— 只测试算的话，"选股链路"仍然可能分叉。
    `assert set(run.ran) == set(PARITY)` 也顺带保证三条公式文件都能正常载入。
    """
    cfg.enabled_formulas = list(PARITY)
    run = formula_group.run_enabled_formulas(parity_db, cfg, directory=FORMULA_DIR)

    assert run.errors == []
    assert set(run.ran) == set(PARITY)
    for formula_name, class_name in PARITY.items():
        key = groups.formula_strategy_name(formula_name)
        picked = {pick["symbol"] for pick in run.picks.get(key, [])}
        assert picked == EXPECTED[formula_name]
        assert picked == set(rules.STRATEGIES[class_name](engine=DataEngine(parity_db)).run())
    # 顺带确认：命中的票名来自本地 stock_basic（界面上要显示名字）
    for picks in run.picks.values():
        assert all(pick["name"] for pick in picks)
