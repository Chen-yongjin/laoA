#!/usr/bin/env python3
"""给「老牛选股」抓一套界面截图（**离屏渲染 + 全合成演示数据**）。

为什么要写成一个脚本而不是手工截屏：
    主人要拿这批图去分发（软件介绍里配图），所以它们必须
    ① **可重复**——界面一改、重新跑一次就能全部重抓，不用一张张手动来；
    ② **不含任何真实数据**——图里要给别人看，绝不能带用户真实的自选/持仓/Key/机器码。
    脚本因此在临时目录里现造一套"看着像真在用"的演示数据（常见股票、合理的价与涨跌、
    12 行左右的表），并把实时快照、大盘概览都换成**注入的假值**（这样跑脚本时
    不联网、不依赖当天行情，谁跑都得到同一批图）。

怎么跑：
    QT_QPA_PLATFORM=offscreen /tmp/laoa-verify/bin/python build/make_screenshots.py
    （在 laoA/ 目录下执行；输出到 docs/截图/）

注意：离屏平台下 `widget.grab()` 拿到的就是渲染结果，不需要真的有显示器；
但**必须先 show() + processEvents()**，否则拿到的是没排版的空白图。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 离屏：无显示器也能抓图

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))                           # 借用 tests/ 里的假数据与假客户端

# 演示用的"家目录"：授权状态、配置文件都落在临时目录里 —— 截图脚本绝不该
# 往真实用户目录（`~/.config/laoa-trader`、`%APPDATA%\LaoATrader`）写东西，
# 否则跑一次脚本就改了本机的授权/试用记账，那是实打实的副作用。
_DEMO_HOME = Path(tempfile.mkdtemp(prefix="laoa-shots-home-"))
os.environ["HOME"] = str(_DEMO_HOME)
os.environ["APPDATA"] = str(_DEMO_HOME / "AppData" / "Roaming")
os.environ["LOCALAPPDATA"] = str(_DEMO_HOME / "AppData" / "Local")
for _key in ("APPDATA", "LOCALAPPDATA"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QGroupBox, QScrollArea,
)

from laoa_trader.config import Config                    # noqa: E402
from laoa_trader.data import storage                     # noqa: E402

#: 统一窗口尺寸（整齐、且 1440×900 在常见屏幕上放得下）
WINDOW_SIZE = (1440, 900)
#: 截图输出目录
OUT_DIR = REPO / "docs" / "截图"

# ── 演示数据（**全是编的**，但长得像真在用的账户）────────────────────────
#
# 选这些名字是因为散户一眼认识；价格/涨跌幅/市值都按真实量级编（茅台一千多、
# 银行几块到十几块），免得图里出现"茅台 12 元"这种一眼假的组合。
DEMO_STOCKS: tuple[tuple[str, str, str], ...] = (
    ("600519", "贵州茅台", "白酒"),
    ("000001", "平安银行", "银行"),
    ("300750", "宁德时代", "电池"),
    ("002594", "比亚迪", "汽车整车"),
    ("601318", "中国平安", "保险"),
    ("000858", "五粮液", "白酒"),
    ("600036", "招商银行", "银行"),
    ("002415", "海康威视", "安防设备"),
    ("600276", "恒瑞医药", "化学制药"),
    ("601899", "紫金矿业", "贵金属"),
    ("300059", "东方财富", "证券"),
    ("002230", "科大讯飞", "软件开发"),
    ("600030", "中信证券", "证券"),
    ("000333", "美的集团", "家电行业"),
    ("601012", "隆基绿能", "光伏设备"),
)

#: 自选股池：代码 → (**入库时的来源**（与库里同一个写法：公式选中就是 `公式·X`）, 加入日期, 加入价系数, 监控开关)
#:
#: 加入价写成"现价的系数"而不是硬编一个数：现价一改（见 `DEMO_QUOTES`），
#: 盈亏仍然是合理的 ±8% 以内 —— 硬编价格迟早出现"五粮液 +41%"那种一眼假的数字。
DEMO_WATCH: dict[str, tuple[str, str, float, bool]] = {
    "600519": ("公式·尾盘超短", "2026-09-08", 0.976, True),
    "300750": ("公式·短期反转", "2026-09-09", 1.031, True),
    "002594": ("公式·尾盘超短", "2026-09-10", 0.958, True),
    "000858": ("公式·地量放量", "2026-09-11", 1.042, True),
    "002415": ("公式·首板缩量", "2026-09-14", 0.987, True),
    "601899": ("", "2026-09-15", 0.945, True),          # 手工加的 → 来源显示「自选」
    "300059": ("公式·尾盘超短", "2026-09-16", 1.018, True),
    "600030": ("公式·短期反转", "2026-09-17", 0.993, True),
    "000333": ("", "2026-09-18", 1.026, True),
    "601012": ("公式·地量放量", "2026-09-18", 0.972, True),
}

#: 持仓监控：代码 → (成本价系数（成本 = 现价 × 系数）, 备注)
#:
#: 与自选一样用系数而不是硬编成本价：持仓表的「现价」取自快照或本地收盘价
#: （哪个新用哪个），硬编成本价就会出现"宁德时代 +33%、紫金矿业 +75%"
#: 这种一眼不像普通账户的盈亏。
DEMO_POSITIONS: dict[str, tuple[float, str]] = {
    "600519": (1.045, "长线底仓"),
    "000001": (0.982, ""),
    "601318": (0.965, ""),
    "600036": (1.028, "分红再投"),
    "600276": (1.061, ""),
    "002230": (1.035, "看后续订单"),
    "300750": (0.947, ""),
    "601899": (0.913, ""),
}

#: 实时快照（现价, 涨跌幅%, 换手率%, 流通市值亿）—— 注入到快照缓存里，
#: 这样两张表的「现价/涨幅/市值/换手」有数（真跑会去联网取，截图不该依赖网络）。
DEMO_QUOTES: dict[str, tuple[float, float, float, float]] = {
    "600519": (1266.98, 0.71, 0.20, 15715.0),
    "000001": (11.86, 1.02, 0.44, 2270.0),
    "300750": (231.40, 2.35, 0.89, 12864.0),
    "002594": (108.75, 3.12, 1.42, 9860.0),
    "601318": (54.10, 0.86, 0.31, 9980.0),
    "000858": (142.30, 1.55, 0.62, 5520.0),
    "600036": (42.66, 0.94, 0.28, 8800.0),
    "002415": (30.95, -1.24, 0.75, 2860.0),
    "600276": (50.20, -0.58, 0.52, 3200.0),
    "601899": (20.15, 2.08, 1.16, 4140.0),
    "300059": (23.08, 3.42, 2.15, 3650.0),
    "002230": (47.90, -0.75, 1.05, 1110.0),
    "600030": (28.12, 1.66, 0.88, 2790.0),
    "000333": (73.15, 0.62, 0.35, 5010.0),
    "601012": (18.42, -1.86, 1.28, 1400.0),
}

#: 演示用的板块榜（**注入**，不联网）：`(板块名, 当日涨幅%, 主力净额元)`
#:
#: 为什么要自己定：板块榜来自腾讯行业接口，真取的话每次跑出来的板块与数字都不一样
#: （截图就不可重复了），而且它跟本地库的行业对不上 →「涨停数量/跌停数量」两列全 `—`。
#: 这里让"板块名"与下面按板块造的成分股行业**同名**，两列数字才落得下去。
DEMO_SECTORS: tuple[tuple[str, float, float], ...] = (
    ("传媒", 1.79, 29.78e8), ("计算机", 1.41, 28.53e8), ("家用电器", 1.24, -2.28e8),
    ("煤炭", 1.18, 4.21e8), ("综合", 1.14, 0.20e8),
    ("交通运输", -1.17, -7.27e8), ("钢铁", -0.95, -5.47e8),
    ("建筑材料", -0.92, -12.10e8), ("国防军工", -0.85, -18.37e8), ("通信", -0.70, -54.70e8),
)

#: 按板块造的"成分股"：前 5 个板块里各放 1~3 只当日**涨停**的（喂「涨停数量」），
#: 后 5 个板块里各放 1~3 只当日**跌停**的（喂「跌停数量」）。代码用主板 6 开头，
#: 涨跌停就是 ±10%（与项目里实测的板块规则一致；用创业板会变成 20%，数字对不上）。
DEMO_SECTOR_STOCKS: tuple[tuple[str, str, str, str], ...] = (
    ("600101", "传媒样本甲", "传媒", "up"), ("600102", "传媒样本乙", "传媒", "up"),
    ("600103", "传媒样本丙", "传媒", "up"),
    ("600104", "计算机甲", "计算机", "up"), ("600105", "计算机乙", "计算机", "up"),
    ("600106", "家电甲", "家用电器", "up"), ("600107", "煤炭甲", "煤炭", "up"),
    ("600108", "综合甲", "综合", "up"),
    ("600201", "交运甲", "交通运输", "down"), ("600202", "交运乙", "交通运输", "down"),
    ("600203", "钢铁甲", "钢铁", "down"), ("600204", "钢铁乙", "钢铁", "down"),
    ("600205", "建材甲", "建筑材料", "down"), ("600206", "军工甲", "国防军工", "down"),
    ("600207", "通信甲", "通信", "down"), ("600208", "通信乙", "通信", "down"),
)

#: 消息列表 / 桌宠气泡里那几条提醒（编的，但把项目的提醒类型都用上）
DEMO_ALERTS: tuple[tuple[str, str, str, str], ...] = (
    ("600519", "stop_loss", "现价 1266.98 已跌破成本价 1180.00 的 −5%（止损提醒）", "09:41"),
    ("300059", "limit_up_open", "东方财富 300059 涨停打开，现价 23.08（+3.42%）", "10:06"),
    ("002594", "surge", "比亚迪 002594 放量突破 20 日高点，量比 3.4", "10:52"),
    ("600276", "break_ma5", "恒瑞医药 600276 跌破 5 日线（50.20）", "11:15"),
    ("002415", "pullback", "海康威视 002415 回踩 5 日线买点，缩量至 5 日均量 0.7 倍", "13:47"),
    ("600519", "take_profit", "贵州茅台 600519 触及止盈位 +7.4%", "14:33"),
)


def _seed_demo_db(cfg: Config) -> list[str]:
    """在临时数据目录里造一套演示库（行情 + 复权事件 + 涨停池 + 自选 + 持仓 + 提醒）。"""
    days = _trading_days(60)
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, list(DEMO_STOCKS))
        rows = []
        last_i = len(days) - 1
        for index, (symbol, _name, _industry) in enumerate(DEMO_STOCKS):
            # 起点比现价低、最后一根正好落在演示现价上：曲线像涨上来的，
            # 而且「库里最新收盘价」与注入的快照价**同一个数**（图里不会出现两个价）
            last = DEMO_QUOTES[symbol][0]
            base = last / (1 + (0.0022 + 0.0005 * (index % 4)) * last_i)
            for i, day in enumerate(days):
                price = base * (1 + (0.0022 + 0.0005 * (index % 4)) * i)
                high = price * 1.018
                low = price * 0.985
                rows.append((symbol, day, price, high, low, price,
                             2.4e7, 2.4e7 * price, 1.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        # 自检要求复权事件非空（缺了后复权价会算错）
        storage.write_adjust_events(conn, [
            ("600519", days[len(days) // 2], 0.1, 0.0, 0.0, 0.0),
        ])
        # 按板块造的成分股：前 59 天都在 10.00，最后一根涨停(11.00)/跌停(9.00)
        # —— 「涨停数量」看涨停池、「跌停数量」看**日线按跌停价规则算**，
        # 两条路都要有数据，两列才不会全是 `—`
        storage.write_stock_basic(conn, [(sym, name, industry)
                                         for sym, name, industry, _d in DEMO_SECTOR_STOCKS])
        moves = []
        for index, (symbol, _name, _industry, direction) in enumerate(DEMO_SECTOR_STOCKS):
            last = 11.00 if direction == "up" else 9.00
            for i, day in enumerate(days):
                price = 10.00 if i < last_i else last
                high = max(price, 10.00) * 1.002 if direction == "up" else price * 1.002
                low = price * 0.998
                moves.append((symbol, day, price, high, low, price,
                              2.0e7, 2.0e7 * price, 1.0))
        storage.write_daily_raw(conn, moves)
        up_rows = []
        for symbol, name, industry, direction in DEMO_SECTOR_STOCKS:
            if direction != "up":
                continue
            up_rows.append((days[-1], symbol, name, 1, "首板", "09:35:00", "09:35:00",
                            6e7, 0, industry, 10.0, 10.0, 1e9, 0, 1, "首板",
                            6e7, 11.0, 0, "hithink", "t"))
        storage.write_limit_up_pool(conn, up_rows)
        storage.write_limit_up_pool(conn, [
            (days[-1], "300059", "东方财富", 1, "首板", "09:35:00", "09:35:00",
             9.8e8, 0, "证券", 5.0, 10.0, 1e9, 0, 1, "首板", 8.8e8, 22.3, 0, "hithink", "t"),
            (days[-2], "002594", "比亚迪", 2, "2连板", "09:31:00", "09:31:00",
             1.2e9, 0, "汽车整车", 5.0, 20.0, 1e9, 0, 2, "连板", 1.1e9, 104.0, 0, "hithink", "t"),
        ])
        for symbol, (source, day, ratio, monitor) in DEMO_WATCH.items():
            name = dict((s, n) for s, n, _ in DEMO_STOCKS)[symbol]
            added_price = round(DEMO_QUOTES[symbol][0] * ratio, 2)
            storage.upsert_watchlist(conn, symbol, name=name, enabled=monitor,
                                     price=added_price, source_strategy=source or None)
            if day:   # 加入日期：直接写库（界面读的就是这一列）
                conn.execute("UPDATE watchlist SET added_at = ? WHERE symbol = ?",
                             (f"{day} 09:35:00", symbol))
        for symbol, (_ratio, note) in DEMO_POSITIONS.items():
            name = dict((s, n) for s, n, _ in DEMO_STOCKS)[symbol]
            # 先按"演示现价 × 系数"写一份；建好窗口之后再按**表格真正显示的现价**精调
            # （见 `_align_position_costs`）—— 两处口径必须是同一个数
            cost = round(DEMO_QUOTES[symbol][0] * _ratio, 2)
            storage.upsert_position(conn, symbol, name=name, avg_cost=cost, note=note)
        # 当日池子：让「选股结果」页有内容可显示（来源写 `策略·X` 的**数据**形态）
        storage.save_pool(conn, [
            {"symbol": "300059", "name": "东方财富", "strategy": "公式·尾盘超短",
             "strategies": "公式·尾盘超短", "score": 6.0, "reason": "证券·热门行业证券"},
            {"symbol": "600519", "name": "贵州茅台", "strategy": "公式·尾盘超短",
             "strategies": "公式·尾盘超短", "score": 5.0, "reason": "白酒·热门行业白酒"},
            {"symbol": "002594", "name": "比亚迪", "strategy": "公式·短期反转",
             "strategies": "公式·短期反转", "score": 4.0, "reason": "汽车整车"},
        ], days[-1])
        storage.record_alerts(conn, [
            {"symbol": symbol, "kind": kind, "price": DEMO_QUOTES[symbol][0], "detail": detail}
            for symbol, kind, detail, _t in DEMO_ALERTS
        ], days[-1])
    _align_added_prices(conn)
    # 自检门槛：演示库只有 15 只票、60 个交易日，不放开的话数据闸门会（正确地）拦住选股
    cfg.min_symbols = 1
    cfg.min_history_years = 0.0
    return days


def _align_added_prices(conn) -> None:
    """把「加入价」对齐到**界面真正显示的现价**（库里最后一根收盘价）× 系数。

    为什么专门有这一步：`watchlist.added_price` 是"从加入那天算盈亏"的基准，
    而界面显示的现价可能来自快照、也可能来自库里的最新收盘价。两边若不是同一个数，
    截图里就会出现"五毛钱的票显示 −52%"这种一眼假的盈亏 —— 演示图必须自洽。
    """
    rows = conn.execute(
        "SELECT symbol, close FROM stock_daily_raw WHERE date = "
        "(SELECT MAX(date) FROM stock_daily_raw)"
    ).fetchall()
    last_close = {str(sym): float(close) for sym, close in rows if close}
    for symbol, (_source, _day, ratio, _monitor) in DEMO_WATCH.items():
        price = last_close.get(symbol)
        if price:
            conn.execute("UPDATE watchlist SET added_price = ? WHERE symbol = ?",
                         (round(price * ratio, 2), symbol))


def _trading_days(count: int) -> list[str]:
    """截至"今天"的 count 个工作日（升序，跳过周末）。"""
    from datetime import timedelta

    cursor = datetime(2026, 9, 21).date()
    out: list[str] = []
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(out)


def _block_network_fetches() -> None:
    """把所有"真去联网"的取数入口换掉（快照 / 板块榜 / 概览都换成固定数据）。

    为什么必须拦：这些图是要发出去的，必须**每次跑都一样**；真取一次行情，
    图里的价格、涨跌家数就跟着当天走，重跑一次就变了。
    """
    from laoa_trader.data import sources

    def _no_snapshot(_cfg, symbols, *_args, **_kwargs):
        return [{"symbol": str(s), **_demo_quote_fields(str(s))} for s in (symbols or [])]

    sources.snapshot_map = _no_snapshot        # type: ignore[assignment]


def _demo_quote_fields(symbol: str) -> dict:
    """把演示行情写成"快照行"的样子（与 `sources.snapshot_map` 的返回结构一致）。"""
    price, pct, turnover, mktcap = DEMO_QUOTES.get(symbol, (10.0, 0.0, 1.0, 100.0))
    return {"price": price, "pct": pct, "turnover_rate": turnover, "circ_mktcap": mktcap}


def _pretend_windows_voices() -> None:
    """把"系统语音"假装成一台装了中文语音的 Windows。

    为什么可以假装：这是**给 Windows 用户看的**产品介绍图，而这台机器是 Linux ——
    不假装的话，设置页那行会写"当前系统不是 Windows，语音朗读不可用"，
    与实际用户看到的样子不符（脚本注释里说明这一点，免得有人以为程序真这么显示）。
    """
    from laoa_trader.notify import voice

    voices = [
        {"name": "Microsoft Huihui Desktop", "culture": "zh-CN", "gender": "female"},
        {"name": "Microsoft Kangkang", "culture": "zh-CN", "gender": "male"},
    ]
    voice.voice_name = lambda *a, **k: "Microsoft Huihui Desktop"
    voice.installed_voices = lambda *a, **k: list(voices)
    voice.voices_summary = lambda *a, **k: "女声 Microsoft Huihui Desktop（zh-CN）、" \
                                            "男声 Microsoft Kangkang（zh-CN）"
    voice.resolve_gender_voice = lambda gender, *a, **k: (
        "Microsoft Kangkang" if str(gender) == "male" else "Microsoft Huihui Desktop"
    )
    voice.can_speak = lambda *a, **k: True


def _patch_sector_rank() -> None:
    """把板块榜换成固定数据（不联网、每次跑都一样，且与本地行业同名）。"""
    from laoa_trader.data import sectors

    def _fake_rank(*, count: int = 30, **_kwargs) -> list[dict]:
        return [
            {"name": name, "pct": pct, "main_net": net,
             "turnover_rate": 1.2, "pct_5d": pct * 2, "pct_20d": pct * 3}
            for name, pct, net in DEMO_SECTORS[: max(int(count), 0)]
        ]

    sectors.fetch_sector_rank = _fake_rank        # type: ignore[assignment]


def _fake_market_client():
    """概览用假客户端：概览层本身已被测试覆盖，这里只要"页面上有数"。"""
    from tests.test_market import SAMPLE_ROWS, FakeMarketClient
    from laoa_trader import market

    # 涨跌家数用**真实量级**（5000+ 只）：假客户端只回 3 行的话，页面上会出现
    # "上涨 1 · 下跌 1 · 平盘 1" —— 一眼就是坏的演示数据
    items: list[dict] = []
    for i in range(4800):
        if i % 8 == 0:
            pct = 0.0                      # 每 8 只里 1 只平盘
        else:
            pct = 1.6 if i % 5 else -1.2   # 涨多跌少，看起来像普涨的一天
        items.append({"thscode": f"{600000 + i}.SH", "turnover": 6e7,
                      "volume": 8e4, "price_change_ratio_pct": pct})
    pages = [{"item": items[:2400], "total": 4800},
             {"item": items[2400:], "total": 4800}]
    return FakeMarketClient(
        totals={market.LIMIT_UP_PATH: 58, market.LIMIT_DOWN_PATH: 14,
                market.LIMIT_BREAK_PATH: 27},
        rows=list(SAMPLE_ROWS), pages=pages,
    )


def _align_position_costs(win) -> None:
    """把持仓的「成本价」对齐到**表格里显示的现价**×系数。

    为什么必须在窗口建好、表填完之后做：持仓表的「现价」优先取实时快照、取不到退回
    本地收盘价 —— 只有表格自己知道这次用了哪个。硬编成本价就会出现"表里现价是 A、
    盈亏却按 B 算"的情况；读它显示的那个数再回写成本价，两张表与盈亏才自洽。
    """
    from laoa_trader.data import storage
    from laoa_trader.ui import app as ui_app

    price_column = ui_app.POSITION_HEADERS.index("现价")
    with storage.connect(win.cfg.db_path) as conn:
        for row in range(win.position_table.rowCount()):
            name_item = win.position_table.item(row, 0)
            price_item = win.position_table.item(row, price_column)
            if name_item is None or price_item is None:
                continue
            symbol = str(name_item.text()).split("(")[-1].rstrip(")")
            ratio = DEMO_POSITIONS.get(symbol, (None, ""))[0]
            try:
                price = float(str(price_item.text()).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if ratio:
                conn.execute("UPDATE position SET avg_cost = ? WHERE symbol = ?",
                             (round(price * ratio, 2), symbol))
    win._refresh_positions()


def _apply_fake_quotes(win) -> None:
    """把演示快照塞进缓存（真跑会联网取；截图不该依赖网络与当天行情）。"""
    import time

    # ⚠️ `at` / `as_of` 必须是**数字时间戳（epoch 秒）**：`quotes.is_fresh()` 用它判断
    # "这条快照是不是今天的、够不够新"，字符串会被判成不新鲜 —— 界面上「现价」会退回
    # 本地收盘价（数值对得上、看不出来），而「市值/换手」直接显示 `—`
    # （截图里就是两列空的，第一次跑正是这么暴露的）。
    stamp = time.time()
    win.quotes.apply({
        symbol: {"symbol": symbol, "price": price, "pct": pct, "at": stamp,
                 "as_of": stamp, "source": "演示数据",
                 "turnover_rate": turnover, "circ_mktcap": mktcap}
        for symbol, (price, pct, turnover, mktcap) in DEMO_QUOTES.items()
    })


def _wait_market(win, app) -> None:
    """等概览那段后台取数落地（否则抓到的是"正在加载"）。"""
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        if getattr(win, "_market_worker", None) is None:
            app.processEvents()
            return
        time.sleep(0.02)
    app.processEvents()


def _grab(widget, name: str) -> Path:
    """存一张图（`grab()` 拿到的是渲染结果，离屏平台下同样有效）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    widget.grab().save(str(path), "PNG")
    return path


def main() -> int:
    app = QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp(prefix="laoa-shots-"))
    cfg = Config(data_dir=tmp / "data")
    cfg.ensure_dirs()
    cfg.source_path = tmp / "config.toml"
    cfg.notify_channels = []            # 截图时绝不真发通知
    cfg.notify_pet = True
    # **不许联网取行情**：截图必须可重复（同一批数据、同一批数字）。
    # 做法是**拦住取数调用**（见 `_block_network_fetches`），而不是清空 data_sources ——
    # 清空会让「系统设置 → 数据来源」显示"当前没有启用的来源、请把它加回去"，
    # 那种画面像装坏了，不能拿去当产品介绍图。
    cfg.hithink_api_key = ""
    days = _seed_demo_db(cfg)
    print(f"演示库就绪：{len(DEMO_STOCKS)} 只票 / {len(days)} 个交易日 → {cfg.db_path}")

    from laoa_trader.ui import app as ui_app

    win = ui_app.MainWindow(cfg)
    win.resize(*WINDOW_SIZE)
    win.show()
    app.processEvents()

    _patch_sector_rank()
    _block_network_fetches()
    _pretend_windows_voices()
    _apply_fake_quotes(win)
    for widget in (getattr(win, "voice_hint", None),):
        if widget is not None:
            win._refresh_voice_hint()
    win._refresh_pool_table()
    win._refresh_positions()
    app.processEvents()


    # ① 大盘概览（注入假客户端，避免联网）
    win.refresh_market_overview(force=True, client=_fake_market_client())
    _wait_market(win, app)
    saved = []
    for index, name in enumerate((
        "01-大盘概览", "02-自选股池", "03-持仓监控", "04-策略选股-列表", "05-系统设置",
    )):
        win.tabs.setCurrentIndex(index)
        app.processEvents()
        if index == 2:
            # 持仓表抓图**前一刻**才对齐成本价：这张表的「现价」会随刷新在
            # "快照价 / 本地收盘价"之间换来源，早点对齐会被后一次刷新冲掉 ——
            # 结果就是"现价 31.41、成本 18.40、盈亏 +70%"这种不像真账户的画面。
            _align_position_costs(win)
            app.processEvents()
        saved.append(_grab(win, name))

    # ② 策略编辑器（左边有内容、右边四组按钮面板）
    win.tabs.setCurrentIndex(3)          # 先切回「策略选股」——不切的话抓到的是上一屏
    app.processEvents()
    page = win.formula_page
    page.btn_edit.click()
    page.name_edit.setText("尾盘超短策略(T+1)")
    page.note_edit.setText("尾盘选强势股：5 日均线多头 + 当日涨 3~5% + 放量一倍")
    page.editor.setPlainText(
        "ZC:=C/REF(C,1)-1\n"
        "J5:=MA(C,5)\n"
        "J10:=MA(C,10)\n"
        "J20:=MA(C,20)\n"
        "PRICE_OK:=C>=3 AND C<=50\n"
        "MKT_OK:=流通市值>=30 AND 流通市值<=500\n"
        "GAIN_OK:=ZC>=0.03 AND ZC<=0.05\n"
        "VOL_OK:=V>REF(V,1)*2\n"
        "TREND_OK:=J5>J10 AND J10>J20 AND C>J5\n"
        "LB_OK:=量比()>1.5\n"
        "ZT_OK:=涨停天数(5)>=1\n"
        "BOARD_OK:=创业板=0 AND 科创=0 AND 北交所=0\n"
        "PRICE_OK AND MKT_OK AND GAIN_OK AND VOL_OK AND TREND_OK"
        " AND LB_OK AND ZT_OK AND BOARD_OK"
    )
    page.on_validate()
    app.processEvents()
    saved.append(_grab(win, "06-策略选股-编辑器"))

    # ③ 选股结果（6 列 + 每行【加入自选】）
    # 先把下面那块编辑器收起来：结果表在上一屏、编辑器的编辑框在下一屏，
    # 两者同时摊开会把结果表挤成半截（营销图里要一眼看清 6 列）
    page.on_close_panel()
    app.processEvents()
    page.show_pick_result({
        "data_date": days[-1],
        "pool": [
            {"symbol": s, "name": n, "strategy": "公式·尾盘超短"}
            for s, n, _ in DEMO_STOCKS[:4]
        ] + [
            {"symbol": s, "name": n, "strategy": "公式·短期反转"}
            for s, n, _ in DEMO_STOCKS[4:9]
        ],
    })
    app.processEvents()
    saved.append(_grab(win, "07-策略选股-选股结果"))

    # ④ 消息列表（QQ 那种，未读带圆点）
    from laoa_trader.ui.message_center import MessageCenter

    center = MessageCenter()
    center.set_messages([
        {"date": days[-1], "symbol": symbol, "kind": kind, "detail": detail,
         "price": DEMO_QUOTES[symbol][0], "pushed_at": f"{days[-1]} {clock}"}
        for symbol, kind, detail, clock in DEMO_ALERTS
    ], unread=True)
    center.show()
    app.processEvents()
    saved.append(_grab(center, "08-消息列表"))

    # ⑤ 桌宠（带气泡）
    from laoa_trader.ui.desktop_pet import DesktopPet

    pet = DesktopPet(None)
    pet.set_unread(3)
    # 气泡有长度上限（`desktop_pet.BUBBLE_MAX_CHARS = 34`），超了会被截成"…" ——
    # 演示图里用一句正好放得下的
    pet.show_bubble("贵州茅台 600519 止损提醒：已跌破 −5%")
    pet.show()
    app.processEvents()
    saved.append(_grab(pet, "09-桌宠提醒"))

    # ⑥ 授权窗口（**假机器码**，绝不出现真机码）
    from laoa_trader.ui.license_dialog import LicenseDialog

    # 状态按"刚装好的样子"给（试用还剩 7 天）：对话框里两处文字分别来自
    # 注入的 status 与 `licensing.status_text(cfg)`，两边**必须一致**，
    # 否则图上会出现"上面说试用还剩 7 天、下面说已到期"这种自相矛盾的画面。
    # `days_left=7` 正好等于 `TRIAL_DAYS`，与本机新建的试用记账一致。
    dialog = LicenseDialog(cfg, status={
        "licensed": True, "registered": False, "trial": True,
        "days_left": 7, "machine": "8F3K-2M7Q-XW4D-9PLA", "code": "", "reason": "",
    })
    # 拦掉"后台读真机器码"：那会把我们填的**假机器码**换成这台机器的真码（几百毫秒后生效，
    # 什么时候生效取决于机器快慢 —— 截图就成了不确定的东西）。演示图里必须是假码。
    dialog._start_machine_lookup = lambda: None        # type: ignore[method-assign]
    dialog.show()
    app.processEvents()
    saved.append(_grab(dialog, "10-授权窗口"))

    # 收尾：把窗口与线程收干净（这是项目里踩过坑的地方，别省）
    for widget in (pet, center, dialog):
        try:
            widget.close()
        except Exception:      # noqa: BLE001 - 截图脚本，收尾失败不影响已存的图
            pass
    try:
        win.shutdown()
    except Exception:          # noqa: BLE001
        pass
    app.processEvents()

    print("已生成：")
    for path in saved:
        print(f"  {path.relative_to(REPO)}  ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
