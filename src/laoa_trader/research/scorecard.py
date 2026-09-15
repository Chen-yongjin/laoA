"""策略成绩单：用**可执行**的口径给每条策略算一张 α 成绩单（Qt 无关）。

为什么要有这个模块
------------------
用户投诉"策略选出的股票很垃圾"，而在此之前本项目能拿出来的证据只有一句
"某策略 T+3 α +0.10%"——既没说清口径，也没给出样本量、显著性、逐年稳定性。
缺的不是更多策略，而是**能证伪一个策略的成绩单**。这也是将来"自定义策略公式"
的评估底座：公式写完必须能立刻算出同样的成绩单，否则公式只是好看。

四条硬口径（改任何一条都会让结论变成"看着漂亮但不合法"）
-----------------------------------------------------
1. **A 股 T+1 结算：当天买入的股票当天不能卖。** 所以进场/出场被参数化成
   `entry=(offset, price)` / `exit=(offset, price)`（`price ∈ {open, close}`，
   `offset` 以交易日计、相对信号日 D），并强制：

       exit_offset > entry_offset ≥ 1

   `exit_offset == entry_offset`（= 当天买当天卖，T+0）在本模块里**直接抛异常**，
   因为那在 A 股不可执行，拿它当结论等于骗人（服务器版 `webapp/evaluate.py`
   用 `exit_idx = entry_idx + n - 1`，n=1 时会落在入场日当天 —— 本模块**刻意不用**
   那个偏移，宁可让持有期标签比服务器版"T+N"晚一天，也不出非法的口径）。
   非法口径如果混进结论，会凭空多出几个千分点的收益，比没有结论更糟。

2. **默认并列输出两套进场时点**（见 `CONVENTIONS`），因为"换个可执行的假设结论就没了"
   的假信号必须被暴露出来：

   | 口径 | 进场 | 出场 | 说明 |
   |---|---|---|---|
   | **A** | D+1 **开盘** | D+2 **收盘** | 旧口径（与 NAS 的历史结论对齐） |
   | **B** | D+1 **收盘** | D+2 **收盘** | **更贴近散户真实执行**：尾盘流动性好、不用抢开盘、避开开盘瞬时滑点 |
   | **C** | D+1 **收盘** | D+2 **开盘** | 最短隔夜：只吃一个跳空 |
   | A3/A5/A10、B3/B5/B10 | D+1 开盘 / 收盘 | D+3、D+5、D+10 收盘 | 对照档：看边际随持有期怎么衰减 |

   结论只看 **A 与 B 同一出场窗口下是否都为正**（`margin_verdicts`）：只有开盘口径为正的，
   标注"可能依赖开盘执行（抢开盘、滑点大、未必吃得进），谨慎对待"。

3. **α 的基准必须与逐笔窗口"同口径"**：进场用 D+1 收盘价时，"全市场同期等权收益"
   也必须是 **D+1 收盘 → 出场价**（而不是 D+1 开盘）。差一个开盘缺口会让 α 凭空多出/
   少掉零点几个百分点 —— 那正好是策略 α 的量级，会把噪声误读成"策略有效"。
   `market_map()` 与逐笔收益共用同一个 `Convention` 定义，就是为了钉死这一点。

4. **买不到的样本按口径区分剔除**（并分别计数，报告里都写出来）：

   - **开盘买**：进场日**开盘涨幅 ≥ 9.5%**（一字板）→ 挂不进去，计入 `dropped_limit_up`；
   - **尾盘买**：进场日**收盘涨停**（收盘相对前收 ≥ 9.5%）→ 也挂不进去，计入
     `dropped_limit_up_close`；
   - **卖出侧不做剔除**：跌停卖不出去是另一回事，本表不改写收益（只在报告里提示这个风险）。

5. **t 值按"信号日"聚合**：同一天选出的几十只股票同涨同跌、高度相关，
   把 3 万条信号当成 3 万个独立样本会让 t 值虚高十倍以上。正确做法是
   先算每个信号日的平均 α，再对这条**日度序列**做单样本 t 检验。
   （残留偏差：持有 N 天的收益在相邻信号日之间仍重叠，t 值依然偏乐观，
   更严格需 Newey-West 调整；这里只做保守提示，不伪装成精确检验。）

统计口径与服务器版 `sequoia_x/research/backtest.py` + `webapp/evaluate.py`
保持一致：α 的定义（个股 − 同期等权全市场）、t 值按信号日聚合、一字板剔除阈值 9.5%。
差别只在"进场价格可以是开盘也可以是收盘"以及"出场偏移 +1 天保证 T+1 合法"——
这两点都被参数化在 `Convention` 里，而不是散落在代码里。

数据来源
--------
只读本地库，**不联网**：

- 桌面版（默认）：SQLite 的 `stock_daily_hfq` **视图**（后复权价）、`stock_basic`、
  `trading_calendar`、`limit_up_pool`；
- 服务器版库（可选，仅用于交叉验证）：DuckDB 的 `stock_daily`（库里存的就是后复权价）、
  `stock_basic`、`limit_up_pool`。**以 read_only 打开，绝不写入**。

用法
----
    from laoa_trader.research import scorecard

    result = scorecard.evaluate("trader.db", scorecard.BUILTIN_SPECS[0])
    print(result["rows"][0]["avg_alpha_pct"], result["rows"][0]["t_stat"])

命令行走 `python -m laoa_trader --cli --scorecard`。
"""

from __future__ import annotations

import csv
import sqlite3
import tempfile
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev

import pandas as pd

# ── 常量（与服务器版对齐的部分都标注了） ──

#: 旧口径家族用的持有期（交易日）。由 `horizons=` 传入时，会被翻译成
#: "D+1 开盘买 → D+(N+1) 收盘卖" 的 `Convention`（与服务器版的历史结论对齐）。
#: **不含 0**：0 就是"D+1 开买、D+1 收卖"，在 A 股不可执行。
#: 默认走过的口径见 `CONVENTIONS`（含开盘买 / 尾盘买两套）。
DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5, 10)

#: 每个信号日取前 N 只（与服务器版 `backtest.TOP_N` 一致，便于横向比较）
TOP_N = 30

#: 进场日涨幅达到该值视为买不进（与服务器版一致）：
#: - 开盘买时看"开盘涨幅"（一字板）；
#: - 尾盘买时看"当日涨幅"（收盘涨停，一样挂不进去）。
LIMIT_UP_GAP = 0.095

#: 预热期（交易日）：250 日高点之类的因子需要完整窗口，否则早期信号是"残缺窗口"算出来的
BURN_IN = 250

#: 结论门槛：样本（笔）数低于它 → 判"样本不足"，不给结论
MIN_SAMPLES = 30

#: 结论门槛：至少要有 2 个信号日才谈得上 t 值（1 天的样本无法估计波动）
MIN_DAYS = 2

#: 全市场等权基准要求当日有效股票数下限；低于它视为"这不是一个市场"，
#: 不产出基准（服务器版用 100，这里放宽到 5，让离线小库/合成库也能算基准；
#: 真正的结论可信度由 MIN_SAMPLES/MIN_DAYS 把关）
MIN_MARKET_SYMBOLS = 5

#: **库级**结论门槛（比"笔数"更前置的一道闸）：库太小就不该给任何结论。
#: 理由：等权基准要的是"全市场"，几十只股票的平均收益不是市场；
#: 历史太短则连"是不是只有某一年有效"都无从谈起（分发包默认导入 5 年，够用）。
#: 低于门槛时报告里必须直说"样本不足，无法评估"，并列出库里的行数与日期范围。
MIN_DB_SYMBOLS = 100
MIN_DB_DAYS = 250

#: 行情表候选（按顺序探测）：
#: - `stock_daily_hfq` = 桌面版的**后复权视图**（原始价 × factor 实时算出）；
#: - `stock_daily`     = 服务器版库里存的**已经是后复权价**的那张表。
#: 两边的表名不同但数值口径相同（都是后复权），所以按存在性探测即可，
#: 不必让用户记"我这个库该用哪个表名"。
PRICE_TABLES: tuple[str, ...] = ("stock_daily_hfq", "stock_daily")

PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "turnover")


class IllegalHorizonError(ValueError):
    """持有期在 A 股不可执行（例如 0 = 当天买当天卖，T+0）。

    为什么单独定义异常而不是返回空结果：非法口径最危险的失败方式是
    "算出来一个数、还挺好看"。抛异常能让它一路冒泡到 CLI / 报告里，
    不可能被误当成结论。
    """


def check_horizon(horizon: int) -> int:
    """校验持有期合法性，返回 int（不合法直接抛 `IllegalHorizonError`）。

    A 股 T+1：当天买入当天不能卖 ⇒ 持有期至少 1 个交易日，
    即最早"D+1 开盘买、D+2 收盘卖"。`horizon <= 0` 对应的都是
    "买入日当天卖出"，本模块一律拒绝。
    """
    try:
        value = int(horizon)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 调用方传错类型
        raise IllegalHorizonError(f"持有期必须是整数：{horizon!r}") from exc
    if value < 1:
        raise IllegalHorizonError(
            f"持有期 T+{value} 不可执行：A 股 T+1 结算，当天买入当天不能卖"
            "（T+0 口径一律拒绝，否则成绩单会凭空变好）。"
            "合法的最短档是 T+1：D+1 开盘买入、D+2 收盘卖出。"
        )
    return value


def check_horizons(horizons: Iterable[int]) -> tuple[int, ...]:
    """批量校验持有期（保持传入顺序，去重）。"""
    out: list[int] = []
    for item in horizons:
        value = check_horizon(item)
        if value not in out:
            out.append(value)
    if not out:
        raise IllegalHorizonError("至少要给一个持有期（例如 (1, 3, 5, 10)）")
    return tuple(out)


# ── 执行口径（进场时点 × 进场价 × 出场时点 × 出场价） ──
#
# 为什么要参数化：同一个策略"开盘买"和"尾盘买"的成绩可以差很多 —— 开盘价经常是
# 情绪最高点（抢开盘的人替你抬轿子），尾盘价更接近散户真实成交。只给一套口径，
# 就等于把一个执行假设偷偷写进了结论里。这里把两个时点都跑出来并列展示，
# 让"换个可执行的假设结论就没了"的假信号自己暴露。


class IllegalConventionError(ValueError):
    """进场/出场时点在 A 股不可执行（例如 D+1 开盘买、D+1 收盘卖 = T+0）。"""


#: 允许的成交价类型：开盘价 / 收盘价（本项目的库只有这两档，日线数据没有盘中价）
PRICE_KINDS: tuple[str, ...] = ("open", "close")


@dataclass(frozen=True)
class Convention:
    """一套执行口径：进场时点/价格 + 出场时点/价格。

    Attributes:
        key: 短键（`A` / `B` / `C` / `A3` / `T+5` …），结果表与 CSV 用它索引。
        label: 中文名（表格标题）。
        entry_offset / exit_offset: 相对**信号日 D** 的交易日偏移（1 = D+1）。
        entry_price / exit_price: `"open"` 或 `"close"`。
        note: 一句话说明（报告里带上）。
    """

    key: str
    label: str
    entry_offset: int
    entry_price: str
    exit_offset: int
    exit_price: str
    note: str = ""

    @property
    def holding(self) -> int:
        """持仓的交易日跨度（出场偏移 − 进场偏移）。"""
        return self.exit_offset - self.entry_offset

    @property
    def entry_desc(self) -> str:
        return f"D+{self.entry_offset}{'开盘' if self.entry_price == 'open' else '收盘'}"

    @property
    def exit_desc(self) -> str:
        return f"D+{self.exit_offset}{'开盘' if self.exit_price == 'open' else '收盘'}"

    @property
    def description(self) -> str:
        return f"{self.entry_desc}买 → {self.exit_desc}卖"

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "entry_offset": self.entry_offset,
            "entry_price": self.entry_price, "exit_offset": self.exit_offset,
            "exit_price": self.exit_price, "holding": self.holding,
            "entry": self.entry_desc, "exit": self.exit_desc, "note": self.note,
        }


def check_convention(conv: Convention) -> Convention:
    """校验口径合法性（T+1 是**硬约束**，不合法直接抛异常）。

    A 股 T+1：当天买入的股票当天不能卖 ⇒ 出场必须落在**比进场更晚的交易日**：
    `exit_offset > entry_offset ≥ 1`。所以：
    - `entry_offset < 1`（想在信号日当天买）不合法 —— 信号来自 D 收盘，最早只能 D+1；
    - `exit_offset == entry_offset`（D+1 开盘买、D+1 收盘卖，即 T+0）不合法；
    - `(D+1 收盘买 → D+2 开盘卖)` 合法（跨日），`(D+1 收盘 → D+2 收盘)` 也合法。
    """
    if conv.entry_price not in PRICE_KINDS or conv.exit_price not in PRICE_KINDS:
        raise IllegalConventionError(
            f"口径 {conv.key} 的成交价只能是 {PRICE_KINDS}，收到 "
            f"{conv.entry_price!r}/{conv.exit_price!r}"
        )
    if conv.entry_offset < 1:
        raise IllegalConventionError(
            f"口径 {conv.key} 不可执行：进场偏移 D+{conv.entry_offset} 早于 D+1 —— "
            "信号是 D 收盘后才算出来的，最早只能 D+1 进场"
        )
    if conv.exit_offset <= conv.entry_offset:
        raise IllegalConventionError(
            f"口径 {conv.key}（{conv.description}）不可执行：A 股 T+1 结算，"
            "当天买入当天不能卖（T+0 口径一律拒绝，否则成绩单会凭空变好）。"
            "合法的最短档是隔夜：如 D+1 收盘买 → D+2 开盘卖。"
        )
    return conv


#: **默认并列输出的口径**。A/B/C 的出场窗口相同（都到 D+2），只有进场价不同 ——
#: 这样才能干净地回答问题："这个 α 到底是选股能力，还是抢开盘抢出来的？"
CONVENTIONS: tuple[Convention, ...] = (
    Convention("A", "A 开盘买·隔日收盘卖", 1, "open", 2, "close",
               "旧口径：D+1 开盘买 → D+2 收盘卖（与 NAS 历史结论对齐）"),
    Convention("B", "B 尾盘买·隔日收盘卖", 1, "close", 2, "close",
               "尾盘买：D+1 收盘买 → D+2 收盘卖（更贴近散户真实执行）"),
    Convention("C", "C 尾盘买·隔夜开盘卖", 1, "close", 2, "open",
               "最短隔夜：D+1 收盘买 → D+2 开盘卖（只吃一个跳空）"),
    Convention("A3", "A3 开盘买·D+3 收盘卖", 1, "open", 3, "close"),
    Convention("B3", "B3 尾盘买·D+3 收盘卖", 1, "close", 3, "close"),
    Convention("A5", "A5 开盘买·D+5 收盘卖", 1, "open", 5, "close"),
    Convention("B5", "B5 尾盘买·D+5 收盘卖", 1, "close", 5, "close"),
    Convention("A10", "A10 开盘买·D+10 收盘卖", 1, "open", 10, "close"),
    Convention("B10", "B10 尾盘买·D+10 收盘卖", 1, "close", 10, "close"),
)

#: 结论只看这一对（同一出场窗口、不同进场价）：两套都为正才算"有边际"
PRIMARY_CONVENTION_PAIR: tuple[str, str] = ("A", "B")


def horizon_convention(horizon: int) -> Convention:
    """把旧口径的持有期翻译成 Convention：D+1 开盘买 → D+(N+1) 收盘卖。"""
    value = check_horizon(horizon)
    return Convention(
        f"T+{value}", f"T+{value}（D+1 开盘买 → D+{value + 1} 收盘卖）",
        1, "open", value + 1, "close", "由 horizons= 推出的旧口径",
    )


def as_conventions(items: Iterable[int | Convention]) -> tuple[Convention, ...]:
    """把 `int` / `Convention` 混着传的序列统一成校验过的 Convention 元组。

    为什么允许混着传：`horizons=(1,3,5,10)` 是旧 API（以及服务器版的口径），
    历史脚本与测试都用它；新代码可以传 `CONVENTIONS` 或自定的口径列表。
    """
    out: list[Convention] = []
    for item in items:
        conv = item if isinstance(item, Convention) else horizon_convention(item)
        checked = check_convention(conv)
        if checked.key not in [existing.key for existing in out]:
            out.append(checked)
    if not out:
        raise IllegalConventionError("至少要给一套执行口径（例如 CONVENTIONS）")
    return tuple(out)


# ── 数据源（SQLite 桌面版 / DuckDB 服务器版） ──


class ScorecardError(RuntimeError):
    """成绩单跑不下去（库不存在、表缺失、没装 duckdb 等），消息里带可执行的下一步。"""


def _is_sqlite(path: Path) -> bool:
    """按文件头判断是不是 SQLite 库（DuckDB 文件头不是这个）。"""
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _open_sqlite_readonly(db_path: str | Path) -> sqlite3.Connection:
    """只读打开桌面版 SQLite 库。

    **必须只读**：这个模块跑在用户机器上，一次误写就可能毁掉几小时的下载成果
    （`mode=ro` 下任何写入都会直接报错，而不是静默改库）。
    """
    path = Path(db_path)
    if not path.exists():
        raise ScorecardError(
            f"库不存在：{path}（先跑 `python -m laoa_trader --cli --download` 建库）"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _open_duckdb_readonly(db_path: str | Path):
    """只读打开服务器版 DuckDB 库（懒加载 duckdb，桌面版不依赖它）。

    两个细节都是踩过的坑：
    - `read_only=True`：DuckDB 在只读连接下**拒绝**任何写入/迁移，服务器版的库
      一个字节都不会被动（这是本模块唯一被允许碰服务器版数据的方式）；
    - DuckDB 会往 `~/.duckdb` 写扩展目录，桌面机上那个目录常常不可写，
      于是连接直接失败（报的还是"IO Error: Failed to create directory"），
      所以先确保 HOME 指向一个可写目录，并把临时目录也挪到系统临时区。
    """
    try:
        import duckdb  # noqa: PLC0415 - 懒加载：桌面版不装 duckdb 也能用本模块
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ScorecardError(
            "读取服务器版 DuckDB 库需要 duckdb 模块（pip install duckdb）。"
            "桌面版自己的库是 SQLite，不需要它。"
        ) from exc

    import os

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home or not os.access(home, os.W_OK):
        fallback = Path(tempfile.gettempdir()) / "laoa-duckdb-home"
        fallback.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(fallback)
    temp_dir = Path(tempfile.gettempdir()) / "laoa-duckdb-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=True,
                          config={"temp_directory": str(temp_dir)})


def _query(db_path: str | Path, sql: str, params: Sequence = ()) -> pd.DataFrame:
    """按库类型执行查询，统一返回 DataFrame（只读）。"""
    path = Path(db_path)
    if path.exists() and not _is_sqlite(path):
        conn = _open_duckdb_readonly(path)
        try:
            return conn.execute(sql, tuple(params)).df()
        finally:
            conn.close()
    with _open_sqlite_readonly(path) as conn:
        return pd.read_sql_query(sql, conn, params=tuple(params))


def price_table(db_path: str | Path) -> str:
    """探测该库用的是哪张行情表（桌面版视图 / 服务器版表），探测不到就报清楚。"""
    errors: list[str] = []
    for name in PRICE_TABLES:
        try:
            _query(db_path, f"SELECT 1 FROM {name} LIMIT 1")  # noqa: S608 - 常量表名
            return name
        except Exception as exc:  # noqa: BLE001 - 表不存在就试下一个
            errors.append(f"{name}({type(exc).__name__})")
    raise ScorecardError(
        f"库里找不到行情表（试过 {'、'.join(PRICE_TABLES)}）：{db_path}；"
        "先跑 `python -m laoa_trader --cli --download` 建库"
    )


def describe_db(db_path: str | Path, panel: pd.DataFrame | None = None) -> dict:
    """看一眼这个库里到底有什么：后端、行数、股票数、日期范围、涨停池天数。

    为什么必须单独有一个函数：用户看到"策略不行"时，第一个要排除的可能
    恰恰是"库里根本没数据"。所以"样本不足"的结论里必须带上这些数字。

    Args:
        panel: 已经载入的行情面板。给了就用它统计行数/股票数/日期范围 ——
            大库（1000 万行）上 `COUNT(DISTINCT ...)` 要扫全表（实测数十秒到几分钟），
            而面板本来就要读，数字直接数出来更划算。
    """
    path = Path(db_path)
    info: dict = {
        "path": str(path), "exists": path.exists(), "backend": "—", "table": "",
        "rows": 0, "symbols": 0, "start": None, "end": None, "days": 0,
        "limit_up_days": 0, "calendar_days": 0, "error": "",
    }
    if not path.exists():
        info["error"] = "库文件不存在"
        return info
    info["backend"] = "sqlite" if _is_sqlite(path) else "duckdb"

    def _one(sql: str, default=0):
        try:
            frame = _query(path, sql)
        except Exception:  # noqa: BLE001 - 表缺失/为空都只是"没有数据"，不该炸
            return default
        if frame.empty or frame.iloc[0, 0] is None:
            return default
        return frame.iloc[0, 0]

    if panel is not None and not panel.empty:
        info["rows"] = int(len(panel))
        info["symbols"] = int(panel["symbol"].nunique())
        info["days"] = int(panel["date"].nunique())
        info["start"], info["end"] = str(panel["date"].min()), str(panel["date"].max())
        try:
            info["table"] = price_table(path)
        except ScorecardError:
            info["table"] = "—"
    else:
        try:
            table = price_table(path)
        except ScorecardError as exc:
            info["error"] = str(exc)
            return info
        info["table"] = table
        info["rows"] = int(_one(f"SELECT COUNT(*) AS n FROM {table}") or 0)
        info["symbols"] = int(_one(f"SELECT COUNT(DISTINCT symbol) AS n FROM {table}") or 0)
        info["days"] = int(_one(f"SELECT COUNT(DISTINCT date) AS n FROM {table}") or 0)
        frame = _query(path, f"SELECT MIN(date) AS a, MAX(date) AS b FROM {table}")
        if not frame.empty:
            info["start"], info["end"] = frame.iloc[0, 0], frame.iloc[0, 1]
    info["limit_up_days"] = int(
        _one("SELECT COUNT(DISTINCT date) AS n FROM limit_up_pool") or 0
    )
    info["calendar_days"] = int(_one("SELECT COUNT(*) AS n FROM trading_calendar") or 0)
    return info


def load_panel(
    db_path: str | Path,
    *,
    date_from: str | None = None,
    columns: Sequence[str] = PRICE_COLUMNS,
) -> pd.DataFrame:
    """载入长表行情面板（后复权），按 (symbol, date) 升序。

    Args:
        db_path: SQLite（桌面版）/ DuckDB（服务器版）库路径。**只读**。
        date_from: 只载入该日期及之后的数据（省内存，也避免无用的早期噪音）。
        columns: 需要的行情列。

    Returns:
        DataFrame[symbol, date, <columns>]，按 (symbol, date) 升序。
    """
    path = Path(db_path)
    if not path.exists():
        raise ScorecardError(f"库不存在：{path}")
    table = price_table(path)
    cols = ", ".join(columns)
    sql = f"SELECT symbol, date, {cols} FROM {table} WHERE 1=1"  # noqa: S608 - 表名/列名是常量
    params: list = []
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    sql += " ORDER BY symbol, date"
    frame = _query(path, sql, params)
    for col in columns:
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    # 0 / 负价会让收益率算出 inf：统一置 NaN，让它们自然缺席而不是污染统计
    for col in ("open", "close"):
        if col in frame:
            frame.loc[frame[col] <= 0, col] = float("nan")
    frame["date"] = frame["date"].astype(str)
    return frame


def load_risk_symbols(db_path: str | Path) -> set[str]:
    """ST/退市风险股（与实盘策略 `factors.load_risk_symbols` 同一判据）。

    已知偏差：用的是**当前**名称，历史上曾被 ST 的股票还原不出来
    （服务器版 `backtest.py` 里也是这么做的，两边一致）。
    """
    try:
        frame = _query(
            db_path, "SELECT symbol FROM stock_basic "
                     "WHERE name LIKE '%ST%' OR name LIKE '%退%'"
        )
    except Exception:  # noqa: BLE001 - 表缺失只意味着"没有可剔除的"
        return set()
    return {str(s) for s in frame["symbol"]} if not frame.empty else set()


def load_ladder(db_path: str | Path) -> dict[tuple[str, str], int]:
    """涨停池的 {(日期, 代码): 连板数}（连板回踩策略要用）。

    没有涨停池数据时返回空字典 —— 相关策略会选不出股票，
    而不是靠"涨幅 ≥9.5%"的近似去猜（两者含义不同：连板数只有权威数据能给）。
    """
    try:
        frame = _query(
            db_path,
            "SELECT date, symbol, high_days FROM limit_up_pool WHERE high_days IS NOT NULL",
        )
    except Exception:  # noqa: BLE001
        return {}
    if frame.empty:
        return {}
    return {
        (str(d), str(s)): int(n)
        for d, s, n in zip(frame["date"], frame["symbol"], frame["high_days"], strict=False)
        if n is not None
    }


# ── 信号定义（Spec）：内置策略与将来自定义公式共用同一种"信号函数" ──


@dataclass
class Spec:
    """一个被评估的信号定义。

    Attributes:
        key: 策略类名（与实盘 `signal.strategy` 一致）。
        label: 中文名。
        group: 所属策略组（ultra/short/swing）。
        build: **向量化**候选生成器：输入长表面板，输出 DataFrame[date, symbol, factor]。
        ascending: 因子排序方向（True = 取最小，如"跌得越多越优先"）。
        top_n: 每个信号日取前 N 只。
        note: 一句话说明（报告里带上）。
    """

    key: str
    label: str
    build: Callable[[pd.DataFrame], pd.DataFrame]
    ascending: bool = False
    top_n: int = TOP_N
    group: str = ""
    note: str = ""


#: 逐日信号函数：输入"某日的行情切片"，输出候选代码（顺序即优先级）。
#: 元素可以是 "600519"，也可以是 ("600519", 0.87) 表示排序因子。
SignalFunc = Callable[[pd.DataFrame], Iterable]


def spec_from_signal(
    key: str,
    label: str,
    func: SignalFunc,
    *,
    group: str = "",
    note: str = "",
    lookback: int = BURN_IN,
    top_n: int = TOP_N,
) -> Spec:
    """把"逐日信号函数"包成 Spec —— 自定义策略公式的入口。

    Args:
        func: `f(day_panel) -> 候选列表`。`day_panel` 是该股票池**截至当日**的
            长表切片（已按 symbol/date 升序，每只股票最多回看 `lookback` 行），
            所以滚动窗口类公式可以直接在上面算。
        lookback: 每日切片的回看行数（每只股票）。窗口越长越慢：
            这个包装是"逐日 × 全市场"的朴素循环，适合几百只股票的自定义公式；
            全市场 10 年的向量化扫描请直接用内置 Spec 的 build 写法。

    返回的 Spec 把候选顺序当作优先级（factor = 位置序号，升序取前 N）——
    与"输出候选 symbol 列表"的直觉一致。
    """

    def build(panel: pd.DataFrame) -> pd.DataFrame:
        rows: list[tuple[str, str, float]] = []
        if panel.empty:
            return pd.DataFrame(columns=["date", "symbol", "factor"])
        for date, day in panel.groupby("date", sort=True):
            window = day if lookback is None else pd.concat(
                [
                    group.tail(lookback)
                    for _, group in panel[panel["date"] <= date].groupby("symbol", sort=False)
                ],
                ignore_index=True,
            )
            for index, item in enumerate(func(window) or []):
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    symbol, factor = str(item[0]), float(item[1])
                else:
                    symbol, factor = str(item), float(index)
                rows.append((str(date), symbol, factor))
        return pd.DataFrame(rows, columns=["date", "symbol", "factor"])

    return Spec(key=key, label=label, build=build, ascending=True, top_n=top_n,
                group=group, note=note)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "symbol", "factor"])


def _out(panel: pd.DataFrame, mask: pd.Series, factor: pd.Series) -> pd.DataFrame:
    """把条件掩码 + 因子整理成候选表（只保留命中的行）。"""
    mask = mask.fillna(False).astype(bool)
    out = panel.loc[mask, ["date", "symbol"]].copy()
    out["factor"] = factor[mask]
    return out.dropna(subset=["factor"])


def _grouped(panel: pd.DataFrame):
    return panel.groupby("symbol", sort=False)


def _prepare(panel: pd.DataFrame) -> pd.DataFrame:
    """补齐内置策略要用的派生列（每只股票独立计算，与实盘口径一致）。"""
    work = panel.copy()
    grouped = _grouped(work)
    work["ret1"] = grouped["close"].transform(lambda s: s.pct_change())
    work["prev_close"] = grouped["close"].shift(1)
    work["prev_volume"] = grouped["volume"].shift(1)
    work["ma5"] = grouped["close"].transform(lambda s: s.rolling(5).mean())
    work["avg_turnover20"] = grouped["turnover"].transform(lambda s: s.rolling(20).mean())
    work["vol_ma20_prev"] = grouped["volume"].transform(
        lambda s: s.shift(1).rolling(20).mean()
    )
    for lag in (1, 2, 3):
        work[f"vol_lag{lag}"] = grouped["volume"].shift(lag)
    return work


def attach_ladder(panel: pd.DataFrame, ladder: dict[tuple[str, str], int]) -> pd.DataFrame:
    """把涨停池的连板数挂到面板的**前一行**（= 昨日涨停状态）。

    为什么要 shift：实盘的连板回踩策略看的是"**昨日**连板 ≥2、今日回踩"。
    回测里必须严格复现这个时点关系，否则用当日涨停数据选当日票 = 用未来信息。
    """
    work = panel.copy()
    if ladder:
        # 先按 (date, symbol) 取当日连板数，再整体按股票下移一行 = 昨日连板数
        keys = list(zip(work["date"], work["symbol"], strict=False))
        work["lu_days_today"] = [ladder.get(key, 0) for key in keys]
    else:
        work["lu_days_today"] = 0
    work["prev_lu_days"] = _grouped(work)["lu_days_today"].shift(1)
    return work


# ── 内置的 5 条策略：条件、窗口、阈值逐条对齐 `strategy/rules.py` ──
#
# 为什么要在回测里**重写一遍**而不是直接调策略类：策略类是"取每只股票最后一根K线"
# 的实盘写法（`factors.latest_snapshot`），拿到历史某一天要改取数层；
# 这里把它改写成面板向量化形式，条件一字不改（与服务器版 `research/backtest.py`
# 的做法相同）。改条件就会让成绩单与实盘脱节 —— 那比没有成绩单更糟。


def _cand_low_price(panel: pd.DataFrame) -> pd.DataFrame:
    """低价股：绝对股价最低（≥2 元、日均成交额 ≥3000 万、非跌停）。"""
    work = _prepare(panel)
    mask = (
        (work["close"] >= 2.0)
        & (work["avg_turnover20"] >= 3e7)
        & (work["ret1"] > -0.095)
    )
    return _out(work, mask, work["close"])  # 升序：越便宜越优先


def _cand_ladder_pullback(panel: pd.DataFrame) -> pd.DataFrame:
    """连板回踩低吸：昨日连板 ≥2 + 今日缩量收阴 + 不破 5 日线。"""
    work = _prepare(panel)
    mask = (
        (work["prev_lu_days"] >= 2)
        & (work["close"] < work["prev_close"])
        & (work["volume"] < work["prev_volume"] * 0.9)
        & (work["close"] >= work["ma5"] * 0.98)
        & (work["avg_turnover20"] >= 3e7)
    )
    return _out(work, mask, work["prev_lu_days"])  # 降序：连板越高越优先


def _cand_reversal(panel: pd.DataFrame) -> pd.DataFrame:
    """短期反转：20 日跌幅 ≥10% + 流动性 + 排除当日跌停。"""
    work = _prepare(panel)
    work["mom20"] = _grouped(work)["close"].transform(lambda s: s / s.shift(20) - 1)
    mask = (
        (work["mom20"] <= -0.10)
        & (work["avg_turnover20"] >= 3e7)
        & (work["ret1"] > -0.095)
    )
    return _out(work, mask, work["mom20"])  # 升序：跌得越多越优先


def _cand_dryup_expansion(panel: pd.DataFrame) -> pd.DataFrame:
    """地量后放量变盘：前 3 日地量（<20 日均量 ×0.7）+ 今日放量 ≥×1.5 + 收阳涨 0~9%。"""
    work = _prepare(panel)
    base = (work["vol_lag1"] + work["vol_lag2"] + work["vol_lag3"]) / 3
    work["factor"] = work["volume"] / base.replace(0, float("nan"))
    mask = (
        (work["vol_lag1"] < work["vol_ma20_prev"] * 0.7)
        & (work["vol_lag2"] < work["vol_ma20_prev"] * 0.7)
        & (work["vol_lag3"] < work["vol_ma20_prev"] * 0.7)
        & (work["volume"] >= work["vol_ma20_prev"] * 1.5)
        & (work["close"] > work["open"])
        & (work["ret1"] > 0)
        & (work["ret1"] <= 0.09)
        & (work["close"] >= 2.0)
        & (work["avg_turnover20"] >= 3e7)
    )
    return _out(work, mask, work["factor"])  # 降序：放量越猛越优先


def _cand_first_limit_up(panel: pd.DataFrame) -> pd.DataFrame:
    """首板缩量整理：昨日**首次**涨停 + 今日缩量不破位。"""
    work = _prepare(panel)
    work["prev_ret1"] = _grouped(work)["ret1"].shift(1)
    work["prev_ret2"] = _grouped(work)["ret1"].shift(2)
    work["factor"] = work["volume"] / work["prev_volume"]
    mask = (
        (work["prev_ret1"] >= 0.095)
        & (work["prev_ret2"] < 0.095)
        & (work["ret1"] < 0.09)
        & (work["factor"] < 1.0)
        & (work["close"] >= work["prev_close"] * 0.95)
    )
    return _out(work, mask, work["factor"])  # 升序：缩量越明显越优先


#: 内置策略（顺序 = 组顺序，与界面/推送一致）
BUILTIN_SPECS: tuple[Spec, ...] = (
    Spec("LadderPullbackStrategy", "连板回踩低吸", _cand_ladder_pullback, ascending=False,
         group="ultra", note="T+1 隔日档唯一候选：昨日连板≥2 今日缩量回踩不破 5 日线"),
    Spec("ReversalStrategy", "短期反转", _cand_reversal, ascending=True,
         group="short", note="20 日跌幅≥10% 且非跌停"),
    Spec("DryUpExpansionStrategy", "地量后放量变盘", _cand_dryup_expansion, ascending=False,
         group="short", note="前 3 日地量后今日放量收阳"),
    Spec("FirstLimitUpStrategy", "首板缩量整理", _cand_first_limit_up, ascending=True,
         group="short", note="昨日首板 + 今日缩量不破位"),
    Spec("LowPriceStrategy", "低价股", _cand_low_price, ascending=True,
         group="swing", note="绝对股价最低（≥2 元且流动性达标）"),
)


def _coerce_spec(spec) -> Spec:
    """允许把"逐日信号函数"直接当 spec 传进来（自定义公式的用法）。"""
    if isinstance(spec, Spec):
        return spec
    if callable(spec):
        name = getattr(spec, "__name__", "custom")
        return spec_from_signal(name, name, spec)
    raise TypeError("spec 必须是 Spec 或 `f(day_panel) -> 候选列表` 的可调用对象")


# ── 候选 → 逐笔收益（入场、一字板、出场、α） ──


def build_picks(
    panel: pd.DataFrame,
    spec: Spec,
    *,
    top_n: int | None = None,
    risk_symbols: set[str] | None = None,
) -> pd.DataFrame:
    """生成某个信号定义在**每个交易日**的选股（取每天前 top_n 只）。

    Returns:
        DataFrame[date, symbol, factor]（已剔除 ST/退市风险股，已排序截断）。
    """
    if panel.empty:
        return _empty()
    candidates = spec.build(panel)
    if candidates is None or candidates.empty:
        return _empty()
    if risk_symbols:
        candidates = candidates[~candidates["symbol"].isin(risk_symbols)]
    candidates = candidates.dropna(subset=["factor"])
    limit = int(top_n if top_n is not None else spec.top_n)
    candidates = candidates.sort_values(
        ["date", "factor"], ascending=[True, bool(spec.ascending)]
    )
    return candidates.groupby("date", sort=True).head(limit)[["date", "symbol", "factor"]]


def market_map(
    panel: pd.DataFrame,
    conventions: Iterable[int | Convention],
    *,
    min_symbols: int = MIN_MARKET_SYMBOLS,
) -> dict[tuple[str, str], float]:
    """每个信号日、每套口径的「等权全市场收益」—— α 的基准。

    **基准必须与逐笔窗口同口径**（这是本模块最容易出错、也最容易被误读的一点）：
    逐笔用 D+1 收盘价进场，基准就必须是 **D+1 收盘 → 出场价**；如果逐笔用收盘、
    基准却用开盘，α 里会混进一个"开盘到收盘"的缺口 —— 那个缺口恰好是零点几个
    百分点的量级，与策略 α 同量级，会把噪声直接读成"策略有效"。

    因此这里和 `compute_outcomes` 共用同一份 `Convention` 定义：

        (信号日 D, 口径 K) 的基准 = 该日全市场每只股票按 K 的进场价/出场价算出的收益
        （买不进的样本按 K 的进场方式剔除：开盘买剔一字板、尾盘买剔收盘涨停），
        再对全部股票取算术平均。

    它代表"当天闭眼买一篮子平均股票"的收益：扣掉它，剩下的才是选股能力。
    """
    convs = as_conventions(conventions)
    if panel.empty:
        return {}
    wide_open = panel.pivot(index="date", columns="symbol", values="open").sort_index()
    wide_close = panel.pivot(index="date", columns="symbol", values="close").sort_index()
    dates = [str(d) for d in wide_close.index]
    result: dict[tuple[str, str], float] = {}
    for conv in convs:
        entry_wide = wide_open if conv.entry_price == "open" else wide_close
        exit_wide = wide_open if conv.exit_price == "open" else wide_close
        # shift(-k) = 当行之后的第 k 个交易日（k = 偏移量）
        entry = entry_wide.shift(-conv.entry_offset)
        exit_ = exit_wide.shift(-conv.exit_offset)
        # 进场日相对信号日收盘的涨幅：开盘买看"开盘涨幅"（一字板），
        # 尾盘买看"当日涨幅"（收盘涨停一样挂不进去）—— 与逐笔口径同一条判据。
        move = entry / wide_close - 1.0
        ret = (exit_ / entry - 1.0).mask(move >= LIMIT_UP_GAP)
        ret = ret.where(entry.notna() & exit_.notna())
        average = ret.mean(axis=1, skipna=True)
        counts = ret.notna().sum(axis=1)
        for pos, date in enumerate(dates):
            value = average.iloc[pos]
            if pd.isna(value) or int(counts.iloc[pos]) < min_symbols:
                continue
            result[(date, conv.key)] = float(value)
    return result


def compute_outcomes(
    panel: pd.DataFrame,
    picks: pd.DataFrame,
    market: dict[tuple[str, str], float],
    conventions: Iterable[int | Convention],
) -> dict:
    """逐笔算出各套口径下的进场/出场与 α（一次遍历，多套口径）。

    Returns:
        {"signals": [逐笔 dict...], "drops": {口径: {"open": n, "close": m}},
         "dropped_limit_up": int, "dropped_limit_up_close": int, "no_entry": int}

        逐笔 dict：signal_date / symbol / {口径 key: {entry_date, exit_date, ret, alpha}}。

    关键实现点：
    - 进场行 = **该股票自身**在信号日之后的第一根K线（停牌股自然顺延，与服务器版
      `webapp/evaluate.py` 的 bisect 口径一致），再按口径的 offset 顺延；
    - 出场行 = 信号日之后第 `exit_offset` 根K线（`> entry_offset`，保证卖出更晚 ⇒ T+1 合法）；
    - 进场价 = 该行的开盘价或收盘价（由口径决定）；
    - 买不进 → 该口径下剔除并计数（**按进场方式分开记**）：
      · 开盘买：进场日开盘涨幅 ≥9.5%（一字板）→ `dropped_limit_up`
      · 尾盘买：进场日收盘涨停（收盘/前收 − 1 ≥9.5%）→ `dropped_limit_up_close`
    - 卖出侧不剔除（跌停卖不出去是另一回事，不改写收益）。
    """
    convs = as_conventions(conventions)
    dates_by_symbol: dict[str, list[str]] = {}
    opens_by_symbol: dict[str, list[float]] = {}
    closes_by_symbol: dict[str, list[float]] = {}
    for symbol, group in panel.groupby("symbol", sort=False):
        key = str(symbol)
        dates_by_symbol[key] = [str(d) for d in group["date"]]
        opens_by_symbol[key] = list(group["open"])
        closes_by_symbol[key] = list(group["close"])
    signal_closes: dict[tuple[str, str], float] = {}
    for date, symbol, close in zip(
        panel["date"], panel["symbol"], panel["close"], strict=False
    ):
        signal_closes[(str(date), str(symbol))] = close

    signals: list[dict] = []
    drops: dict[str, dict[str, int]] = {conv.key: {"open": 0, "close": 0} for conv in convs}
    no_entry = 0
    for date, symbol in zip(picks["date"], picks["symbol"], strict=False):
        date, symbol = str(date), str(symbol)
        rows_date = dates_by_symbol.get(symbol) or []
        base = bisect_right(rows_date, date)        # 信号日之后的第一个交易日（D+1）
        if base >= len(rows_date):
            no_entry += 1
            continue
        opens = opens_by_symbol[symbol]
        closes = closes_by_symbol[symbol]
        signal_close = signal_closes.get((date, symbol))
        record: dict = {"signal_date": date, "symbol": symbol}
        for conv in convs:
            entry_idx = base + (conv.entry_offset - 1)
            exit_idx = base + (conv.exit_offset - 1)
            if exit_idx >= len(rows_date) or entry_idx >= len(rows_date):
                continue
            entry_open, entry_close = opens[entry_idx], closes[entry_idx]
            exit_open, exit_close = opens[exit_idx], closes[exit_idx]
            entry_px = entry_open if conv.entry_price == "open" else entry_close
            exit_px = exit_open if conv.exit_price == "open" else exit_close
            # 两种"买不进"的判据都在**该口径的进场日**上算：
            #   开盘涨幅（一字板）与当日涨幅（收盘涨停）。
            gap_open = (
                entry_open / signal_close - 1.0
                if (signal_close and entry_open) else None
            )
            gap_close = (
                entry_close / signal_close - 1.0
                if (signal_close and entry_close) else None
            )
            if gap_open is not None and gap_open >= LIMIT_UP_GAP:
                drops[conv.key]["open"] += 1
            if gap_close is not None and gap_close >= LIMIT_UP_GAP:
                drops[conv.key]["close"] += 1
            move = gap_open if conv.entry_price == "open" else gap_close
            if move is not None and move >= LIMIT_UP_GAP:
                continue      # 这个口径下买不进 → 该口径不计这一笔
            if not entry_px or not exit_px:
                continue
            ret = exit_px / entry_px - 1.0
            benchmark = market.get((date, conv.key))
            record[conv.key] = {
                "entry_date": rows_date[entry_idx],
                "exit_date": rows_date[exit_idx],
                "ret": ret,
                # α = 个股区间收益 − **同口径**的同期全市场等权收益
                "alpha": (ret - benchmark) if benchmark is not None else None,
            }
        signals.append(record)

    return {
        "signals": signals,
        "drops": drops,
        "dropped_limit_up": sum(item["open"] for item in drops.values()),
        "dropped_limit_up_close": sum(item["close"] for item in drops.values()),
        "no_entry": no_entry,
    }


def daily_t(values_by_date: dict[str, list[float]]) -> tuple[float | None, float | None, int]:
    """按**信号日**聚合后的 (t 值, 日均值, 天数)。

    为什么必须按信号日聚合：同一天选出的几十只票同涨同跌，把 3 万条信号当
    3 万个独立样本，t 值会虚高十倍以上（标准差被低估、样本量被高估）。
    先算每日均值再做单样本 t 检验，才是"这个策略有没有稳定的选股能力"。

    残留偏差：持有 N 天的收益在相邻信号日之间仍重叠（自相关），t 值依然偏乐观。
    """
    daily = [mean(values) for values in values_by_date.values() if values]
    n = len(daily)
    if n < 2:
        return None, (daily[0] if daily else None), n
    avg = mean(daily)
    sd = stdev(daily)
    t_stat = (avg / (sd / (n ** 0.5))) if sd else None
    return t_stat, avg, n


def summarize(
    outcomes: dict,
    conventions: Iterable[int | Convention],
    *,
    min_samples: int = MIN_SAMPLES,
    min_days: int = MIN_DAYS,
) -> list[dict]:
    """把逐笔结果汇总成“一套口径一行”的成绩单行。

    行字段（一套口径一行；`by_year` 之外的都与需求里的字段名一致，多出来的字段是口径证据）：

        convention / horizon / n / avg_alpha_pct / win_rate_pct / t_stat / by_year /
        dropped_limit_up / dropped_limit_up_close / dropped / start / end
        + days（信号日数，t 值的样本量）/ avg_ret_pct（个股绝对收益）/
          avg_market_pct（同口径的同期等权市场）/ t_stat_ret（绝对收益的日度 t）/ sufficient

    剔除计数按口径区分（都在**该口径的进场日**上数）：
        dropped_limit_up       = 开盘涨幅 ≥9.5%（一字板，开盘买时用）
        dropped_limit_up_close = 收盘涨停（尾盘买时用）
        dropped                = 本口径实际剔除的笔数（按其进场价二选一）
    """
    rows: list[dict] = []
    signals = outcomes["signals"]
    drops = outcomes.get("drops") or {}
    for conv in as_conventions(conventions):
        usable = [
            (record, record[conv.key])
            for record in signals
            if conv.key in record and record[conv.key].get("alpha") is not None
        ]
        alphas = [item[1]["alpha"] for item in usable]
        rets = [item[1]["ret"] for item in usable]
        # 同期等权市场收益 = 个股收益 − α（逐笔反推，保证两者严丝合缝）
        markets = [item[1]["ret"] - item[1]["alpha"] for item in usable]

        by_date_alpha: dict[str, list[float]] = defaultdict(list)
        by_date_ret: dict[str, list[float]] = defaultdict(list)
        by_year: dict[str, dict] = {}
        for record, outcome in usable:
            date = str(record["signal_date"])
            by_date_alpha[date].append(outcome["alpha"])
            by_date_ret[date].append(outcome["ret"])
            bucket = by_year.setdefault(date[:4], {"n": 0, "_alpha_sum": 0.0, "_win": 0})
            bucket["n"] += 1
            bucket["_alpha_sum"] += outcome["alpha"]
            bucket["_win"] += 1 if outcome["alpha"] > 0 else 0

        year_rows: dict[str, dict] = {}
        for year, bucket in sorted(by_year.items()):
            year_rows[year] = {
                "n": bucket["n"],
                "avg_alpha_pct": round(bucket["_alpha_sum"] / bucket["n"] * 100, 4),
                "win_rate_pct": round(bucket["_win"] / bucket["n"] * 100, 2),
            }

        t_stat, daily_avg, days = daily_t(by_date_alpha)
        t_ret, _daily_ret_avg, _days_ret = daily_t(by_date_ret)
        dates = sorted(by_date_alpha)
        n = len(alphas)
        # 样本不足就不下结论：n 太少、或信号日不足 2 天（t 值无从谈起）
        sufficient = n >= min_samples and days >= min_days
        dropped_open = int((drops.get(conv.key) or {}).get("open", 0))
        dropped_close = int((drops.get(conv.key) or {}).get("close", 0))
        rows.append({
            "convention": conv.key,
            "convention_label": conv.label,
            "entry": conv.entry_desc,
            "exit": conv.exit_desc,
            "entry_price": conv.entry_price,
            "exit_price": conv.exit_price,
            "holding": conv.holding,
            "horizon": conv.holding,          # 兼容旧字段名（= 出场偏移 − 进场偏移）
            "n": n,
            "avg_alpha_pct": round(mean(alphas) * 100, 4) if alphas else None,
            "win_rate_pct": (
                round(sum(1 for a in alphas if a > 0) / n * 100, 2) if n else None
            ),
            "t_stat": round(t_stat, 4) if t_stat is not None else None,
            "by_year": year_rows,
            "dropped_limit_up": dropped_open,
            "dropped_limit_up_close": dropped_close,
            "dropped": dropped_open if conv.entry_price == "open" else dropped_close,
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
            # ── 以下为口径证据（多出来的字段，方便判断"是不是只是行情好"）──
            "days": days,
            "avg_ret_pct": round(mean(rets) * 100, 4) if rets else None,
            "avg_market_pct": round(mean(markets) * 100, 4) if markets else None,
            "t_stat_ret": round(t_ret, 4) if t_ret is not None else None,
            "sufficient": bool(sufficient),
            "min_samples": int(min_samples),
        })
    return rows


def evaluate(
    db_path: str | Path | None,
    spec,
    *,
    conventions: Iterable[int | Convention] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    top_n: int | None = None,
    date_from: str | None = None,
    burn_in: int = BURN_IN,
    panel: pd.DataFrame | None = None,
    market: dict[tuple[str, str], float] | None = None,
    min_samples: int = MIN_SAMPLES,
    min_days: int = MIN_DAYS,
    min_market_symbols: int = MIN_MARKET_SYMBOLS,
    min_db_symbols: int = MIN_DB_SYMBOLS,
    min_db_days: int = MIN_DB_DAYS,
    risk_symbols: set[str] | None = None,
    ladder: dict[tuple[str, str], int] | None = None,
    db_info: dict | None = None,
) -> dict:
    """给一个信号定义（Spec 或逐日信号函数）跑一份成绩单。

    Args:
        db_path: 本地库路径（SQLite 桌面版 / DuckDB 服务器版，只读）。
            `panel` 已给时可传 None（用于复用同一份面板跑多条策略，省 IO）。
        spec: `Spec`，或 `f(day_panel) -> 候选代码列表` 的可调用对象。
        conventions: 执行口径列表（`Convention` 或旧的 int 持有期混着传）。
            None 时用 `horizons` 推出旧口径家族（D+1 开盘买 → D+(N+1) 收盘卖）；
            要并列看开盘买/尾盘买，传 `CONVENTIONS`（CLI 的默认）。
        horizons: 旧的持有期参数（等价于"开盘买"那一套）。**T+0（0）会被拒绝**。
        top_n: 每个信号日取前 N 只；None = 用 spec 自己的默认值。
        date_from: 只评估该日期之后的信号。
        burn_in: 丢弃前 N 个交易日（让滚动窗口因子用满窗口，避免残缺窗口假信号）。
        panel / market / risk_symbols / ladder / db_info: 预先算好的公共输入，
            批量跑多条策略时传进来可避免重复读库与重复算基准。
            `market` 的键是 `(信号日, 口径 key)`，必须与 conventions 同口径。
        min_samples / min_days: 单条策略的结论门槛（不达标 → `sufficient=False`）。
        min_market_symbols: 等权基准要求当日有效股票数下限。
        min_db_symbols / min_db_days: **库级**门槛（库太小直接不给结论，见 `db_sufficiency`）。

    Returns:
        {
          key/label/group, rows: [每套口径一行（结构见 summarize）],
          conventions: (口径 key...), convention_defs: [{...}],
          n_signals, start, end, sufficient, db_sufficient, reason,
          dropped_limit_up, dropped_limit_up_close, db_info: {...}
        }
    """
    convs = as_conventions(conventions if conventions is not None else horizons)
    definition = _coerce_spec(spec)

    if panel is None:
        if db_path is None:
            raise ScorecardError("必须给 db_path 或 panel")
        panel = load_panel(db_path)
    if db_info is None and db_path is not None:
        # 把面板传进去：大库上 COUNT(DISTINCT ...) 要扫全表，而面板本来就读了
        db_info = describe_db(db_path, panel)
    db_info = db_info or {}

    if risk_symbols is None and db_path is not None:
        risk_symbols = load_risk_symbols(db_path)
    if ladder is None and db_path is not None:
        ladder = load_ladder(db_path)

    work = attach_ladder(panel, ladder or {})
    # 预热期：前 burn_in 个交易日丢弃（250 日高点等因子需要完整窗口）
    dates = sorted(work["date"].unique()) if not work.empty else []
    start = date_from
    if burn_in and len(dates) > burn_in:
        warm = str(dates[burn_in])
        start = max([value for value in (warm, date_from) if value], default=warm)
    elif date_from:
        start = date_from
    if start:
        work = work[work["date"] >= start]

    if market is None:
        # 基准与逐笔同口径（见 market_map 的说明）
        market = market_map(work, convs, min_symbols=min_market_symbols)

    picks = build_picks(work, definition, top_n=top_n, risk_symbols=risk_symbols)
    outcomes = compute_outcomes(work, picks, market, convs)
    rows = summarize(outcomes, convs, min_samples=min_samples, min_days=min_days)
    if not rows:
        raise IllegalConventionError("至少要给一套执行口径")

    ok_rows = [row for row in rows if row["sufficient"]]
    db_ok, db_reason = db_sufficiency(db_info, min_db_symbols, min_db_days)
    if ok_rows and db_ok:
        reason = ""
    else:
        reason = db_reason or _insufficient_reason(rows, outcomes, db_info)
    return {
        "key": definition.key,
        "label": definition.label,
        "group": definition.group,
        "note": definition.note,
        "horizons": tuple(dict.fromkeys(row["horizon"] for row in rows)),
        "conventions": tuple(conv.key for conv in convs),
        "convention_defs": [conv.as_dict() for conv in convs],
        "rows": rows,
        "n_signals": len(picks),
        "dropped_limit_up": outcomes["dropped_limit_up"],
        "dropped_limit_up_close": outcomes["dropped_limit_up_close"],
        "no_entry": outcomes["no_entry"],
        "start": min((row["start"] for row in rows if row["start"]), default=None),
        "end": max((row["end"] for row in rows if row["end"]), default=None),
        "sufficient": bool(ok_rows) and db_ok,
        "db_sufficient": db_ok,
        "reason": reason,
        "db_info": db_info,
    }


def db_sufficiency(
    db_info: dict,
    min_symbols: int = MIN_DB_SYMBOLS,
    min_days: int = MIN_DB_DAYS,
) -> tuple[bool, str]:
    """库级门槛：这个库够不够格谈"策略有没有 α"。

    为什么要在样本量之外**再加一道**：几十只股票的"等权全市场"其实是策略自己，
    α 会机械地趋近 0；历史上只有几十天则连"某一年的行情"都没覆盖。
    这种情况下算出来的数字不是"不显著"，而是**没有意义** —— 必须直说，
    并给出库里的行数/股票数/交易日数与日期范围，让用户知道差多少。
    """
    if not db_info:
        return True, ""            # 没有库信息（例如直接喂面板）时不拦，交给笔数门槛
    if not db_info.get("exists", True):
        return False, f"库不存在：{db_info.get('path')}"
    rows = int(db_info.get("rows") or 0)
    symbols = int(db_info.get("symbols") or 0)
    days = int(db_info.get("days") or 0)
    span = (f"库内 {rows:,} 行 / {symbols} 只 / {days} 个交易日"
            f"（{db_info.get('start') or '—'} → {db_info.get('end') or '—'}）")
    if not rows:
        return False, f"样本不足：库里没有任何行情；{span}"
    if symbols < min_symbols or days < min_days:
        return False, (
            f"样本不足：库太小（{symbols} 只 / {days} 个交易日，"
            f"低于门槛 {min_symbols} 只 / {min_days} 个交易日）—— "
            "等权基准要的是**全市场**，几十只股票的均值不是市场；历史太短也没法看逐年稳定性。"
            f"先把数据下够（`python -m laoa_trader --cli --download`，默认 5 年）；{span}"
        )
    return True, ""


def _insufficient_reason(rows: list[dict], outcomes: dict, db_info: dict) -> str:
    """说清“为什么没有结论”—— 空库、数据太短、还是一字板全剔除了。"""
    total = sum(row["n"] for row in rows)
    span = (f"库内 {db_info.get('rows', 0):,} 行 / {db_info.get('symbols')} 只 / "
            f"{db_info.get('days')} 个交易日"
            f"（{db_info.get('start') or '—'} → {db_info.get('end') or '—'}）")
    if not db_info.get("exists", True):
        return f"库不存在：{db_info.get('path')}"
    if not db_info.get("rows"):
        return (f"库里没有任何行情（{db_info.get('path')}）；"
                f"日期范围 {db_info.get('start') or '—'} → {db_info.get('end') or '—'}，"
                "先跑 `python -m laoa_trader --cli --download` 把数据下下来")
    if not outcomes["signals"] and outcomes["no_entry"]:
        return (f"样本不足：{outcomes['no_entry']} 笔信号**全部没有后续行情**"
                "（信号日之后没有下一个交易日，或持有期还没到期）—— "
                f"评估区间太短，至少要多出 {max(row['horizon'] for row in rows) + 1} 个交易日；{span}")
    if not outcomes["signals"]:
        return (f"样本不足：评估区间内一条信号都没选出来；{span}。"
                "先确认数据是否够长（因子窗口 20~60 日 + 预热期 250 日）")
    if total == 0:
        return (f"样本不足：样本全部不可成交或还没到期 —— 剔除一字板 "
                f"{outcomes['dropped_limit_up']} 笔、无后续行情 {outcomes['no_entry']} 笔；{span}")
    best = max(rows, key=lambda row: row["n"])
    return (f"样本不足：最多只有 {best['n']} 笔（T+{best['horizon']}）、"
            f"{best['days']} 个信号日，低于门槛 {best['min_samples']} 笔 / "
            f"{MIN_DAYS} 个信号日 —— 这个量级算出来的 α 没有统计意义，不下结论；{span}")


def evaluate_all(
    db_path: str | Path,
    specs: Sequence[Spec] | None = None,
    **kwargs,
) -> list[dict]:
    """对多条策略跑成绩单（面板/基准/风险名单只算一次，摊销到每条策略）。"""
    definitions = list(specs if specs is not None else BUILTIN_SPECS)
    # 口径：显式给 conventions 就用它，否则按旧参数 horizons 推（D+1 开盘买那一套）
    given = kwargs.pop("conventions", None)
    convs = as_conventions(given if given is not None
                           else kwargs.get("horizons", DEFAULT_HORIZONS))
    kwargs["conventions"] = convs
    kwargs.pop("horizons", None)

    panel = kwargs.pop("panel", None)
    if panel is None:
        panel = load_panel(db_path)
    db_info = kwargs.pop("db_info", None) or describe_db(db_path, panel)
    risk = kwargs.pop("risk_symbols", None)
    if risk is None:
        risk = load_risk_symbols(db_path)
    ladder = kwargs.pop("ladder", None)
    if ladder is None:
        ladder = load_ladder(db_path)

    # 基准与 burn-in 一起算：所有策略共用同一段评估区间，才能横向比较
    burn_in = kwargs.get("burn_in", BURN_IN)
    date_from = kwargs.get("date_from")
    work = attach_ladder(panel, ladder)
    dates = sorted(work["date"].unique()) if not work.empty else []
    start = date_from
    if burn_in and len(dates) > burn_in:
        warm = str(dates[burn_in])
        start = max([value for value in (warm, date_from) if value], default=warm)
    if start:
        work = work[work["date"] >= start]
    market = kwargs.pop("market", None)
    if market is None:
        market = market_map(work, convs,
                            min_symbols=kwargs.get("min_market_symbols", MIN_MARKET_SYMBOLS))

    results: list[dict] = []
    for definition in definitions:
        results.append(evaluate(
            None, definition, panel=work, market=market, risk_symbols=risk,
            ladder=ladder, db_info=db_info, **kwargs,
        ))
    return results


def margin_verdicts(
    results: Sequence[dict],
    pair: tuple[str, str] = PRIMARY_CONVENTION_PAIR,
) -> list[dict]:
    """结论：**同一出场窗口下，开盘买（A）与尾盘买（B）是否都为正**。

    为什么必须两套一起看：只在一套口径上为正的策略，很可能只是"执行假设"的产物 ——
    开盘价常是当天情绪最高点（抢开盘的人替你抬轿子），尾盘价才是散户真能成交的价。
    反过来，只有尾盘口径为正的，说明这个边际"抢不到开盘才吃得到"，反而更可信。

    Returns:
        每策略一行：{key,label,group,a_alpha,a_t,b_alpha,b_t, verdict, detail}
    """
    first, second = pair
    out: list[dict] = []
    for result in results:
        rows = {row["convention"]: row for row in result["rows"]}
        one, two = rows.get(first), rows.get(second)
        label = result["label"]
        a_alpha = (one or {}).get("avg_alpha_pct")
        b_alpha = (two or {}).get("avg_alpha_pct")
        a_t = (one or {}).get("t_stat")
        b_t = (two or {}).get("t_stat")
        if not result["sufficient"]:
            verdict, detail = "样本不足，不下结论", result["reason"]
        elif a_alpha is None or b_alpha is None:
            verdict, detail = "无法比较（缺口径）", f"缺少口径 {first}/{second}"
        elif a_alpha > 0 and b_alpha > 0:
            verdict = f"✅ 有边际（{first}/{second} 两套口径都为正）"
            detail = f"{first} α {a_alpha:+.2f}%（t={a_t}）· {second} α {b_alpha:+.2f}%（t={b_t}）"
        elif a_alpha > 0:
            verdict = "⚠️ 可能依赖开盘执行（抢开盘、滑点大、未必吃得进），谨慎对待"
            detail = (f"{first} α {a_alpha:+.2f}%（t={a_t}）为正，但 {second} "
                      f"{b_alpha:+.2f}%（t={b_t}）不为正")
        elif b_alpha > 0:
            verdict = "⚠️ 只在尾盘口径为正：边际集中在尾盘/隔夜，开盘买吃不到"
            detail = (f"{second} α {b_alpha:+.2f}%（t={b_t}）为正，但 {first} "
                      f"{a_alpha:+.2f}%（t={a_t}）不为正")
        else:
            verdict = f"❌ 无边际（{first}/{second} 两套口径都为负）"
            detail = f"{first} α {a_alpha:+.2f}%（t={a_t}）· {second} α {b_alpha:+.2f}%（t={b_t}）"
        out.append({
            "key": result["key"], "label": label, "group": result["group"],
            "a_alpha": a_alpha, "a_t": a_t, "b_alpha": b_alpha, "b_t": b_t,
            "verdict": verdict, "detail": detail,
        })
    return out


# ── 报告输出（表格 / CSV / Markdown） ──


def _fmt(value, digits: int = 2, suffix: str = "", dash: str = "—",
         signed: bool = False) -> str:
    """数字格式化：None → dash；signed=True 时带正负号（α/t 值需要，胜率不需要）。"""
    if value is None:
        return dash
    return f"{value:+.{digits}f}{suffix}" if signed else f"{value:.{digits}f}{suffix}"


def positive_years(row: dict) -> tuple[int, int]:
    """(正 α 的年份数, 有样本的年份数) —— 用来回答"是不是只有某一年有效"。"""
    years = row.get("by_year") or {}
    return sum(1 for item in years.values() if item["avg_alpha_pct"] > 0), len(years)


def _year_flag(row: dict) -> str:
    good, total = positive_years(row)
    if not total:
        return "—"
    if good == total:
        return f"✅{good}/{total}"
    if good == 0:
        return f"❌0/{total}"
    return f"⚠️{good}/{total}"


def format_table(
    results: Sequence[dict],
    *,
    conventions: Iterable[int | Convention] | None = None,
) -> str:
    """打印用的成绩单（**一套口径一块**，块内按平均 α 降序），最后给出 A/B 结论。

    口径提示写在表头（T+1 合法性、两套进场价、买不进的两种剔除、α 同口径、t 值按信号日聚合）——
    成绩单最怕的就是"数字被当成结论"，所以口径必须跟数字一起出现。
    """
    lines: list[str] = []
    if conventions is not None:
        convs = as_conventions(conventions)
    elif results:
        keys = results[0]["conventions"]
        defs = {item["key"]: item for item in results[0].get("convention_defs", [])}
        convs = tuple(
            Convention(key, defs.get(key, {}).get("label", key),
                       defs.get(key, {}).get("entry_offset", 1),
                       defs.get(key, {}).get("entry_price", "open"),
                       defs.get(key, {}).get("exit_offset", 2),
                       defs.get(key, {}).get("exit_price", "close"))
            for key in keys
        )
    else:
        convs = as_conventions(DEFAULT_HORIZONS)
    lines.append("=" * 116)
    lines.append("策略成绩单（回测）：D 收盘选股 → D+1 进场 → 按口径持有 → 出场（卖出日严格晚于买入日）")
    lines.append("  · A = D+1【开盘】买（旧口径）· B = D+1【尾盘(收盘)】买（更贴近散户真实执行）"
                 "· C = D+1 尾盘买 → D+2 开盘卖（只吃隔夜）")
    lines.append("  · T+0（当日买当日卖）在本工具里被直接判定为**不可执行**，不参与任何统计")
    lines.append("  · α = 个股区间收益 − **同口径**的同期全市场等权收益"
                 "（开盘买就用开盘基准、尾盘买就用收盘基准，否则会混进一个开盘缺口）")
    lines.append("  · t 值按**信号日**聚合（同日多只票不是独立样本，按笔数算会虚高十倍以上）")
    lines.append("  · 买不进的一律剔除并计数：开盘买看“进场日开盘涨幅≥9.5%”（一字板 → 剔开板列），"
                 "尾盘买看“进场日收盘涨停”（→ 剔尾板列）")
    lines.append("  · 结论只看 A 与 B 在**同一出场窗口**下是否都为正 —— 只有一套为正，"
                 "说明这个 α 可能只是执行假设的产物")
    lines.append("=" * 116)

    if not results:
        lines.append("（没有可评估的策略）")
        return "\n".join(lines)

    for conv in convs:
        lines.append("")
        lines.append(f"── {conv.label}（{conv.description}，持仓 {conv.holding} 个交易日）")
        lines.append(f"{'策略':<16}{'组':<8}{'样本':>7}{'信号日':>7}{'平均α':>10}{'胜率':>8}"
                     f"{'t值':>8}{'正边际年':>10}{'剔开板':>8}{'剔尾板':>8}{'区间':>24}")
        rows = []
        for result in results:
            for row in result["rows"]:
                if row["convention"] == conv.key:
                    rows.append((result, row))
        rows.sort(key=lambda item: -(item[1]["avg_alpha_pct"]
                                     if item[1]["avg_alpha_pct"] is not None else -99))
        for result, row in rows:
            span = f"{row['start'] or '—'}~{row['end'] or '—'}"
            flag = "" if row["sufficient"] else " ⚠️"     # 表格里只标一下，原因写在表尾
            lines.append(
                f"{(result['label'] + flag):<16}{result['group'] or '—':<8}{row['n']:>7}"
                f"{row['days']:>7}{_fmt(row['avg_alpha_pct'], 2, '%', signed=True):>10}"
                f"{_fmt(row['win_rate_pct'], 1, '%', dash='—'):>8}"
                f"{_fmt(row['t_stat'], 2, signed=True):>8}{_year_flag(row):>10}"
                f"{row['dropped_limit_up']:>8}{row['dropped_limit_up_close']:>8}{span:>24}"
            )

    # ── 结论：A vs B（同一出场窗口、只差进场价）──
    verdicts = margin_verdicts(results)
    lines.append("")
    lines.append("── 结论：开盘买(A) vs 尾盘买(B)，同一出场窗口")
    lines.append(f"{'策略':<16}{'A α':>10}{'A t':>7}{'B α':>10}{'B t':>7}  结论（两套都为正方算有边际）")
    for item in verdicts:
        lines.append(
            f"{item['label']:<16}{_fmt(item['a_alpha'], 2, '%', signed=True):>10}"
            f"{_fmt(item['a_t'], 2, signed=True):>7}"
            f"{_fmt(item['b_alpha'], 2, '%', signed=True):>10}"
            f"{_fmt(item['b_t'], 2, signed=True):>7}  {item['verdict']}"
        )
    lines.append("")
    lines.append("说明：胜率 = α>0 的比例；“正边际年” ✅ = 每一年 α 都为正（最值得信），"
                 "⚠️ = 好坏参半，❌ = 年年为负。")
    lines.append("      剔开板 = 进场日开盘涨幅≥9.5%（开盘买用）；剔尾板 = 进场日收盘涨停（尾盘买用）。")
    lines.append("      卖出侧不做剔除：跌停卖不出去是真实风险，本表不改写收益（报告里单列提示）。")
    lines.append("      α 只有零点几个百分点时，扣掉往返成本（约 0.15%）后往往就没了 —— "
                 "本表未计成本，别把 +0.1% 当成能赚钱。")
    insufficient = [result for result in results if not result["sufficient"]]
    for result in insufficient:
        lines.append(f"  ⚠️ {result['label']}：{result['reason']}")
    db_info = results[0].get("db_info") or {}
    if db_info:
        lines.append(f"数据来源：{db_info.get('backend')} {db_info.get('path')}；"
                     f"{db_info.get('rows'):,} 行 / {db_info.get('symbols')} 只 / "
                     f"{db_info.get('days')} 个交易日（{db_info.get('start')} → {db_info.get('end')}）"
                     f"；涨停池 {db_info.get('limit_up_days')} 天")
    return "\n".join(lines)


def result_rows_for_export(results: Sequence[dict]) -> list[dict]:
    """把结果摊平成 CSV 行（每策略 × 每套口径一行）。"""
    out: list[dict] = []
    for result in results:
        for row in result["rows"]:
            good, total = positive_years(row)
            out.append({
                "strategy": result["key"],
                "label": result["label"],
                "group": result["group"],
                "convention": row["convention"],
                "convention_label": row["convention_label"],
                "entry": row["entry"],
                "exit": row["exit"],
                "holding": row["holding"],
                "horizon": row["horizon"],
                "n": row["n"],
                "days": row["days"],
                "avg_alpha_pct": row["avg_alpha_pct"],
                "win_rate_pct": row["win_rate_pct"],
                "t_stat": row["t_stat"],
                "avg_ret_pct": row["avg_ret_pct"],
                "avg_market_pct": row["avg_market_pct"],
                "t_stat_ret": row["t_stat_ret"],
                "positive_years": good,
                "total_years": total,
                "dropped_limit_up": row["dropped_limit_up"],
                "dropped_limit_up_close": row["dropped_limit_up_close"],
                "dropped": row["dropped"],
                "start": row["start"],
                "end": row["end"],
                "sufficient": int(row["sufficient"]),
            })
    out.sort(key=lambda item: (item["convention"], -(item["avg_alpha_pct"] or -99)))
    return out


def verdict_rows_for_export(results: Sequence[dict]) -> list[dict]:
    """结论表（A vs B）的 CSV 行。"""
    return [
        {
            "strategy": item["key"], "label": item["label"], "group": item["group"],
            "a_alpha_pct": item["a_alpha"], "a_t": item["a_t"],
            "b_alpha_pct": item["b_alpha"], "b_t": item["b_t"],
            "verdict": item["verdict"], "detail": item["detail"],
        }
        for item in margin_verdicts(results)
    ]


def by_year_rows_for_export(results: Sequence[dict]) -> list[dict]:
    """逐年明细（每策略 × 每套口径 × 每年一行）—— 回答“是不是只有某一年有效”。"""
    out: list[dict] = []
    for result in results:
        for row in result["rows"]:
            for year, item in sorted((row.get("by_year") or {}).items()):
                out.append({
                    "strategy": result["key"],
                    "label": result["label"],
                    "group": result["group"],
                    "convention": row["convention"],
                    "entry": row["entry"],
                    "exit": row["exit"],
                    "horizon": row["horizon"],
                    "year": year,
                    "n": item["n"],
                    "avg_alpha_pct": item["avg_alpha_pct"],
                    "win_rate_pct": item["win_rate_pct"],
                    "positive": int(item["avg_alpha_pct"] > 0),
                })
    return out


#: CSV 列（主表 / 结论 / 逐年明细）—— 空结果时也要写出正确的表头，否则 Excel 打开是一片空白
RESULT_FIELDS: tuple[str, ...] = (
    "strategy", "label", "group", "convention", "convention_label", "entry", "exit",
    "holding", "horizon", "n", "days", "avg_alpha_pct", "win_rate_pct", "t_stat",
    "avg_ret_pct", "avg_market_pct", "t_stat_ret", "positive_years", "total_years",
    "dropped_limit_up", "dropped_limit_up_close", "dropped", "start", "end", "sufficient",
)
VERDICT_FIELDS: tuple[str, ...] = (
    "strategy", "label", "group", "a_alpha_pct", "a_t", "b_alpha_pct", "b_t",
    "verdict", "detail",
)
BY_YEAR_FIELDS: tuple[str, ...] = (
    "strategy", "label", "group", "convention", "entry", "exit", "horizon", "year",
    "n", "avg_alpha_pct", "win_rate_pct", "positive",
)


def write_csv(
    rows: Sequence[dict],
    path: str | Path,
    *,
    fields: Sequence[str] | None = None,
) -> Path:
    """写 CSV（UTF-8 + BOM：Windows 上 Excel 直接双击打开不会乱码）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    names = list(fields) if fields else (list(rows[0].keys()) if rows else [])
    with open(target, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return target


def write_markdown(results: Sequence[dict], path: str | Path, *, title: str = "策略成绩单") -> Path:
    """写 Markdown 报告（主表 + 逐年明细 + 口径说明 + 结论建议）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    db_info = (results[0].get("db_info") if results else {}) or {}
    if db_info:
        lines += [
            f"- 数据来源：`{db_info.get('backend')}` `{db_info.get('path')}`",
            f"- 数据规模：{db_info.get('rows'):,} 行 / {db_info.get('symbols')} 只 / "
            f"{db_info.get('days')} 个交易日（{db_info.get('start')} → {db_info.get('end')}）",
            f"- 涨停池：{db_info.get('limit_up_days')} 个交易日",
            "",
        ]
    lines += [
        "## 口径（先看这里再看数字）",
        "",
        "| 项 | 口径 |",
        "|---|---|",
        "| 进场 | 信号日 **D 的次日（D+1）**：口径 A 用**开盘价**、口径 B/C 用**收盘价（尾盘）** |",
        "| 出场 | 按口径：A/B 到 D+2 收盘，C 到 D+2 开盘，对照档到 D+3/D+5/D+10 收盘 |",
        "| T+0 | **不合法**（A 股 T+1 结算，当天买入当天不能卖），本表任何一行都不是 T+0 |",
        "| 买不进 | 开盘买看“进场日开盘涨幅 ≥9.5%”（一字板 → 剔开板）；尾盘买看“进场日收盘涨停”（→ 剔尾板）；都单独计数 |",
        "| 卖出侧 | **不做剔除**：跌停卖不出去是真实风险，本表不改写收益 |",
        "| α | 个股区间收益 − **同口径**的同期全市场等权收益（开盘买用开盘基准、尾盘买用收盘基准） |",
        "| t 值 | 先按**信号日**求平均，再对日度序列做 t 检验（不是按笔数） |",
        "| 成本 | **未计**交易成本与滑点（往返约 0.15%，α 在 0.1% 级别的策略要特别注意） |",
        "| 已知偏差 | 股票池是“当前仍在上市”的代码（幸存者偏差，收益偏乐观）；行业/ST 用当前名单 |",
        "",
        "**结论只看 A 与 B 在同一出场窗口下是否都为正**：只有一套为正，说明这个 α 可能只是"
        "执行假设的产物（开盘价常是当天情绪最高点，尾盘价才是散户真能成交的价）。",
        "",
    ]
    verdicts = margin_verdicts(results)
    lines += ["## 结论：开盘买(A) vs 尾盘买(B)", "",
              "| 策略 | A α | A t | B α | B t | 结论 |", "|---|---:|---:|---:|---:|---|"]
    for item in verdicts:
        lines.append(
            f"| {item['label']} | {_fmt(item['a_alpha'], 2, '%', signed=True)} | "
            f"{_fmt(item['a_t'], 2, signed=True)} | {_fmt(item['b_alpha'], 2, '%', signed=True)} | "
            f"{_fmt(item['b_t'], 2, signed=True)} | {item['verdict']} |"
        )
    lines.append("")

    conventions = results[0].get("convention_defs") if results else None
    if not conventions:
        conventions = [horizon_convention(n).as_dict() for n in DEFAULT_HORIZONS]
    for conv in conventions:
        lines += [
            f"## {conv['label']}（{conv['entry']}买 → {conv['exit']}卖，持仓 {conv['holding']} 个交易日）",
            "",
            "| 策略 | 组 | 样本 | 信号日 | 平均α | 胜率(α>0) | t值 | 正边际年 | 剔开板 | 剔尾板 | 区间 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        pairs = [(r, row) for r in results for row in r["rows"]
                 if row["convention"] == conv["key"]]
        pairs.sort(key=lambda item: -(item[1]["avg_alpha_pct"] if item[1]["avg_alpha_pct"]
                                      is not None else -99))
        for result, row in pairs:
            lines.append(
                f"| {result['label']}{'' if row['sufficient'] else ' ⚠️样本不足'} | "
                f"{result['group'] or '—'} | {row['n']} | {row['days']} | "
                f"{_fmt(row['avg_alpha_pct'], 2, '%', signed=True)} | "
                f"{_fmt(row['win_rate_pct'], 1, '%', dash='—')} | "
                f"{_fmt(row['t_stat'], 2, signed=True)} | {_year_flag(row)} | "
                f"{row['dropped_limit_up']} | {row['dropped_limit_up_close']} | "
                f"{row['start'] or '—'} ~ {row['end'] or '—'} |"
            )
        lines.append("")

    lines += ["## 逐年明细（判断“是不是只有某一年有效”）", "",
              "| 策略 | 口径 | 年份 | 样本 | 平均α | 胜率(α>0) |", "|---|---|---:|---:|---:|---:|"]
    for item in by_year_rows_for_export(results):
        lines.append(
            f"| {item['label']} | {item['convention']} | {item['year']} | {item['n']} | "
            f"{_fmt(item['avg_alpha_pct'], 2, '%', signed=True)} | {_fmt(item['win_rate_pct'], 1, '%')} |"
        )
    lines.append("")

    lines += ["## 结论提示", ""]
    for item in verdicts:
        lines.append(f"- **{item['label']}**：{item['verdict']} —— {item['detail']}")
    for result in results:
        if not result["sufficient"]:
            lines.append(f"- ⚠️ **{result['label']}**：{result['reason']}")
    lines.append("- 判断顺序：**A/B 两套口径都为正、且 t 值 ≥ 2、且逐年大多为正**，"
                 "α 还要能盖住 0.15% 的往返成本；只有一套为正 → 标注为执行依赖，谨慎对待。")
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
