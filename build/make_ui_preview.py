#!/usr/bin/env python3
"""把界面**离屏渲染成截图**（每个页签一张），供人肉眼验收。

为什么要这个脚本
----------------
界面改动的验收最终得靠眼睛，但 Agent 这边（以及 CI）只能跑测试：测试能证明"控件存在、
数值对、颜色是红的"，证明不了"排得好看、底部没被切、窄窗口不挤"。所以把窗口在离屏平台
下渲染出来存成 PNG，人打开图就能一眼判断 —— 也方便对比"银色主题 vs 系统默认"。

用法（需要 PySide6；只有看界面时才跑）：
    python build/make_ui_preview.py 出图目录
    python build/make_ui_preview.py 出图目录 --themes silver,system --sizes 960x900,1280x720

产出（平铺在一个目录里，文件名自带"主题 + 尺寸"，便于"银色 vs 原生"对照）：
    01-全市概览-silver-960x900.png     ← 默认页 + 银色（重点看这张）
                                         三块：宽基指数 / 情绪指数（含 3 个小条目）/ 热门板块
    01-全市概览-system-960x900.png     ← 同一页的系统默认主题（对照）
    01b-全市概览-页面-silver-960x900.png ← 只有页面（不含窗口边框，看排版细节）
    02-自选标的-… 03-持仓监控-… 04-策略筛选-… 05-系统设置-…、
    90-关于弹窗-…、91-状态详情-…
    1280x720 那一档同样一套（尺寸后缀不同）

设计取舍
--------
- **不联网**：把同花顺客户端换成一个假的（`PreviewClient`，数据取自 2026-09-14 收盘后的
  实测值），但**仍走真实的取数路径** —— 跑这个脚本不需要 API Key，也不会因为限流/断网
  出不了图；同时避免"手写 overview 漏键 → 截图里整块是空的"这种假象。
- **按"逻辑像素"出图**：`--sizes` 里的 960x900 就是用户那台 2160×1440@150% 的等效逻辑尺寸，
  用最苛刻的那档出图，排版问题才暴露得出来。
- 窗口尺寸用 `fit_window_geometry` 同一套规则（注入可用区域），保证"图里看到的"就是
  用户真机上会得到的布局。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))          # 复用 tests/ 里的造数工具（只读，不改测试）

#: 写死的行情快照（2026-09-14 收盘后的实测值），只为出图好看、且不联网
SAMPLE_ROWS: list[dict] = [
    # 宽基
    {"thscode": "000001.SH", "last_price": 3885.33, "price_change_ratio_pct": -0.07,
     "turnover": 779_280_000_000},
    {"thscode": "399001.SZ", "last_price": 13384.57, "price_change_ratio_pct": -0.64,
     "turnover": 849_890_000_000},
    {"thscode": "399006.SZ", "last_price": 3285.58, "price_change_ratio_pct": -1.10},
    {"thscode": "000688.SH", "last_price": 1528.27, "price_change_ratio_pct": -1.62},
    {"thscode": "000300.SH", "last_price": 4480.08, "price_change_ratio_pct": -0.67},
    # 情绪（同花顺板块指数）
    {"thscode": "883404.TI", "last_price": 885.529, "price_change_ratio_pct": -0.18},
    {"thscode": "883958.TI", "last_price": 6928.171, "price_change_ratio_pct": 3.53},
    {"thscode": "883994.TI", "last_price": 1551.879, "price_change_ratio_pct": 1.69},
    {"thscode": "883418.TI", "last_price": 2131.00, "price_change_ratio_pct": 0.95},
    # 板块（同花顺一级行业）
    {"thscode": "881155.TI", "last_price": 1408.771, "price_change_ratio_pct": 0.88},
    {"thscode": "881157.TI", "last_price": 1428.643, "price_change_ratio_pct": -0.27},
]


class PreviewClient:
    """假的同花顺客户端：让**真实的** `market.fetch_overview` 跑一遍（不联网）。

    为什么不直接手写一个 overview dict 喂给渲染函数：取数结果的结构是
    `fetch_overview` 定的（`configured_groups`、turnover 从指数里取、errors…），
    手写一份迟早会漏键 —— 第一版就漏了 `configured_groups`，于是三组指数**整组隐藏**，
    截图上那一大块是空的，看着像"界面坏了"。走真实路径就不会有这种假象。
    """

    def special_pool_total(self, path: str, day: str | None = None) -> int:
        from laoa_trader import market

        return {
            market.LIMIT_UP_PATH: 55,
            market.LIMIT_DOWN_PATH: 16,
            market.LIMIT_BREAK_PATH: 30,
        }.get(path, 0)

    def index_snapshot(self, thscodes: list[str]) -> dict:
        wanted = set(thscodes)
        return {"item": [r for r in SAMPLE_ROWS if r["thscode"] in wanted], "failed": []}

    def request(self, path: str, params: dict | None = None) -> dict:
        """全市场快照（分页）：给**与真机同一量级**的一整批数据。

        为什么不再只给 3 行：涨跌家数（3 个小条目之一）是"翻 6 页全市场快照"算出来的
        （`market.BREADTH_PAGE_SIZE` = 1000）。只给一页 3 行的话，截图上显示的是
        `涨 1 · 跌 1 · 平 1` —— 排版看不出问题，**但那个数字是假的**（用户会拿它当真）；
        给足 5571 行之后，页面上就是真的量级（涨 3126 · 跌 2224 · 平 221），
        与页脚那句"每 5 分钟更新"也对得上。
        """
        params = params or {}
        rows = _preview_breadth_rows()
        offset = int(params.get("offset") or 0)
        limit = int(params.get("limit") or 1000)
        return {"item": rows[offset:offset + limit], "total": len(rows)}


#: 全市场快照的**假样本**：涨 3126 / 跌 2224 / 平 221（合计 5571，与真机量级一致）。
#: 只在模块里造一次：分页会被调 6 次，每次都要同一批行，否则"翻页"会翻出重复或缺漏。
_BREADTH_ROWS: list[dict] | None = None


def _preview_breadth_rows() -> list[dict]:
    """按真实分布造全市场快照行（沪 2275 / 深 2850 / 北 446，北交所成交额合计 140 亿）。"""
    global _BREADTH_ROWS
    if _BREADTH_ROWS is not None:
        return _BREADTH_ROWS
    up, down, flat = 3126, 2224, 221
    total = up + down + flat
    bj_count = total // 13 + 1
    rows: list[dict] = []
    for i in range(total):
        if i < up:
            pct = 1.0
        elif i < up + down:
            pct = -0.8
        else:
            pct = 0.0
        # 北交所：每 13 只里 1 只（约 429 只），成交额摊平到合计 140.1 亿
        if i % 13 == 0:
            suffix, turnover = "BJ", 140.1e8 / bj_count
        elif i < 2275:
            suffix, turnover = "SH", 1.1e9
        else:
            suffix, turnover = "SZ", 1.6e9
        rows.append({
            "thscode": f"{600000 + i:06d}.{suffix}",
            "turnover": turnover,
            "volume": 1e6,
            "price_change_ratio_pct": pct,
        })
    _BREADTH_ROWS = rows
    return rows


def _use_preview_client() -> None:
    """把取数客户端换成假的（走真实取数路径，只是不出网）。"""
    from laoa_trader import market

    market.clear_cache()
    market._make_client = lambda cfg: PreviewClient()


#: 出图用的实时快照（写死的收盘前后实测值，只为"现价/涨幅/盈亏比例"三列有数可看）。
#: **不联网**：把 `ui.quotes` 的取数函数换成下面这个假的 ——
#: 它走的是与真实快照**同一个形状**（`{symbol: {price, pct, at}}`），
#: 所以截图里看到的表格布局与真机一致。
PREVIEW_QUOTES: dict[str, tuple[float, float]] = {
    "600001": (3.62, 1.97),
    "600002": (12.85, -0.62),
    "300750": (198.40, 2.31),
    "601398": (6.18, 0.16),
    "000001": (11.42, -1.04),
}


def _use_preview_quotes() -> None:
    """把实时快照换成假数据（离线出图，且现价列有真数而不是 `—`）。"""
    import time

    from laoa_trader.ui import quotes as quotes_mod

    def fake(_cfg, symbols, *, client=None):  # noqa: ANN001 - 与真实签名一致
        at = time.time()
        return {
            s: {"symbol": s, "price": PREVIEW_QUOTES[s][0], "pct": PREVIEW_QUOTES[s][1],
                "at": at}
            for s in symbols if s in PREVIEW_QUOTES
        }

    quotes_mod.fetch_snapshot_prices = fake


def _seed(cfg) -> None:
    """造一点像样的数据：股票池 / 自选标的 / 持仓 / 提醒 / **热门板块**都有内容。

    为什么连"热门板块"的数据也要造：概览页那一块是**从本地库算的**
    （当日涨停密度 + 近 5 日行业成分等权涨幅，`pool.hot_industries`）——
    库里没有 `limit_up_pool` 与足够的行业成分时，那一块就是一个 `—` 占位，
    截图看着像"界面坏了"。所以这里按真实口径造出 **14 个行业 / 每个行业若干只票**，
    让「热门板块」正好摆满前 12 行（与真机上的样子一致）。
    """
    from datetime import datetime, timedelta

    from laoa_trader.data import storage

    end = datetime(2026, 9, 11).date()
    days: list[str] = []
    cursor = end
    while len(days) < 40:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    days.sort()

    stocks = [("600001", "低价样本", "银行"), ("600002", "半导体甲", "半导体"),
              ("300750", "电池龙头", "电池"), ("601398", "大盘银行", "银行"),
              ("000001", "平安银行", "银行")]
    # 热门板块用的行业样本：14 个行业 × 6 只票 = 84 只（`hot_industries` 只管
    # "行业成分数"与"当日涨停家数"，给足样本才能算出像样的密度）
    hot_industries_sample = [
        "半导体", "软件服务", "电池", "医疗器械", "光伏设备", "航天航空",
        "证券", "银行", "汽车零部件", "化学制药", "通信设备", "食品饮料",
        "有色金属", "电子元件",
    ]
    industry_stocks: list[tuple[str, str, str]] = []
    for index, industry in enumerate(hot_industries_sample):
        for seat in range(6):
            symbol = f"{300100 + index * 10 + seat:06d}" if index % 2 else \
                f"{601000 + index * 10 + seat:06d}"
            industry_stocks.append((symbol, f"{industry}{seat + 1}", industry))

    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, stocks + industry_stocks)
        rows = []
        for i, (symbol, _n, _ind) in enumerate(stocks):
            base = 3.0 + i * 9
            for d_i, day in enumerate(days):
                price = base * (1 + 0.003 * d_i)
                rows.append((symbol, day, price, price, price, price,
                             2e7, 2e7 * price, 1.0))
        # 行业样本的走势各不相同（涨停密度与 5 日等权涨幅都因此有真数、有高有低）
        for i, (symbol, _n, _ind) in enumerate(industry_stocks):
            base = 8.0 + (i % 17) * 1.3
            drift = 0.004 if i % 3 == 0 else (-0.002 if i % 3 == 1 else 0.0007)
            for d_i, day in enumerate(days):
                price = base * (1 + drift * d_i)
                rows.append((symbol, day, price, price, price, price,
                             6e6, 6e6 * price, 1.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        storage.write_adjust_events(conn, [("600001", days[len(days) // 2],
                                            0.1, 0.0, 0.0, 0.0)])
        # 当日涨停池：每个行业 0~4 只（密度因此分档，热门板块的排序才看得出来）
        pool_rows = []
        for index, industry in enumerate(hot_industries_sample):
            count = (5 - index // 3) if index < 9 else max(0, 2 - index % 3)
            for seat in range(min(count, 6)):
                symbol = industry_stocks[index * 6 + seat][0]
                pool_rows.append((
                    days[-1], symbol, f"{industry}{seat + 1}", seat + 1, "首板",
                    f"09:{35 + seat:02d}:00", f"09:{40 + seat:02d}:00",
                    8e7, 0, industry, 5.0, 10.0, 1e9, 0, 1, "首板", 9e7,
                    12.0, 0, "hithink", "t",
                ))
        storage.write_limit_up_pool(conn, pool_rows)
        # 池子行的 `strategy` / `strategies` 必须是**真实存在的策略类名**
        # （`rules.STRATEGIES` 里的那些）：界面「来源」列显示的是 `策略·<中文名>`
        # （中文名由 `rules.strategy_label` 翻译），编一个不存在的类名会让截图里
        # 出现 `策略·VolatilitySqueeze` 这种内部叫法 —— 那是给用户看的列，不是调试面板。
        # `strategies` 与 `strategy` 保持同一批类名（建池时也是这么写的），
        # 否则界面会把它当成"同批还选中了另一条策略"，白多出一行。
        storage.save_pool(conn, [
            {"symbol": "600001", "name": "低价样本", "strategy": "LowPriceStrategy",
             "strategies": "LowPriceStrategy", "score": 0.812,
             "reason": "低价+缩量回踩"},
            {"symbol": "600002", "name": "半导体甲", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy,DryUpExpansionStrategy", "score": 0.664,
             "reason": "缩量回踩末端"},
            {"symbol": "300750", "name": "电池龙头", "strategy": "",
             "strategies": "", "score": None, "reason": "自选标的"},
        ], days[-1])
        storage.upsert_watchlist(conn, "300750", name="电池龙头", note="消息面")
        # 再加一只**只在自选、还没进池**的票：截图里要能看出"手工加的自选也在这张表里"
        storage.upsert_watchlist(conn, "601398", name="大盘银行", note="底仓")
        storage.upsert_position(conn, "600001", name="低价样本",
                                quantity=1000, avg_cost=3.05, note="试仓")
        storage.upsert_position(conn, "000001", name="平安银行", avg_cost=11.80,
                                note="突破前高")
        # 提醒写**今天**：表格的「提醒」列只显示当天的（写 days[-1] 会是一列 `—`，
        # 截图就看不出这一列长什么样了）
        from laoa_trader.intraday import now_shanghai

        storage.record_alerts(conn, [
            {"symbol": "600002", "name": "半导体甲", "kind": "break_high",
             "price": 12.85, "detail": "现价 12.85 突破 20 日高点 12.60，涨 2.0%"},
            {"symbol": "000001", "name": "平安银行", "kind": "stop_loss",
             "price": 11.42, "detail": "现价 11.42 ≤ 参考价 12.05 × 0.95"},
        ], now_shanghai().strftime("%Y-%m-%d"))


def _available(width: int, height: int):
    from PySide6.QtCore import QRect
    return QRect(0, 0, width, height)


def render(out_dir: Path, themes: list[str], sizes: list[tuple[int, int]]) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from laoa_trader import market
    from laoa_trader.config import Config
    from laoa_trader.data import storage
    from laoa_trader.ui import app as ui_app
    from laoa_trader.ui import theme as theme_mod

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="laoa-preview-"))
    cfg = Config(data_dir=tmp / "data", hithink_api_key="preview",
                 min_history_years=0, min_symbols=1, notify_channels=[])
    cfg.ensure_dirs()
    _seed(cfg)

    app = QApplication.instance() or QApplication([])
    _use_preview_client()          # 概览数据走真实取数路径（假客户端），不联网
    _use_preview_quotes()          # 现价/涨幅走假快照（真实取数路径，同样不联网）

    shots = 0
    for width, height in sizes:
        for theme_name in themes:
            # 主题跟着 **config** 走（窗口自己在 __init__ 里按 `cfg.ui_theme` 应用）——
            # 只调 `apply_theme(app, …)` 会被窗口构造时的那次覆盖掉，
            # 结果是"两种主题的图一模一样"（第一版就踩了这个坑）
            cfg.ui_theme = theme_name
            # 文件名里的尺寸后缀不能省：一档尺寸一套图，同目录平铺时靠它区分
            tag = f"{theme_name}-{width}x{height}"
            # 离屏平台的"真屏幕"是固定值，所以这里注入可用区域 → 出来的图就是
            # 用户那台机器（逻辑像素）上会得到的布局
            avail = _available(width, height)
            ui_app._available_geometry = lambda avail=avail: avail
            win = ui_app.MainWindow(cfg)
            win.show()
            app.processEvents()
            win.refresh_market_overview(force=True)    # 同步取一轮（假客户端，离线）
            win._tick()
            app.processEvents()

            tabs = [(i, win.tabs.tabText(i)) for i in range(win.tabs.count())]
            for index, title in tabs:
                win.tabs.setCurrentIndex(index)
                app.processEvents()
                QTimer.singleShot(0, lambda: None)
                app.processEvents()
                name = f"{index + 1:02d}-{title.replace(' ', '')}-{tag}.png"
                # 抓**整窗**（含顶部状态区与按钮行）—— 那两块正是这次整改的重点，
                # 只抓页面会看不到状态栏与页脚
                win.grab().save(str(out_dir / name))
                shots += 1
                print(f"  {name}")
                if title == ui_app.TAB_MARKET:
                    # 概览页再单独抓一张"只有页面"的（看排版细节时不用被窗口边框干扰）
                    page_name = f"{index + 1:02d}b-{title}-页面-{tag}.png"
                    win.market_page.grab().save(str(out_dir / page_name))
                    shots += 1
                    print(f"  {page_name}")

            # 「关于」与「状态详情」两个弹窗单独出图（它们也是用户会看到的界面）
            win.on_about()
            app.processEvents()
            if win.about_dialog is not None:
                win.about_dialog.grab().save(
                    str(out_dir / f"90-关于弹窗-{tag}.png"))
                shots += 1
                win.about_dialog.close()
            win.on_show_status_details()
            app.processEvents()
            if win.status_dialog is not None:
                win.status_dialog.grab().save(
                    str(out_dir / f"91-状态详情-{tag}.png"))
                shots += 1
                win.status_dialog.close()

            win._timer.stop()
            win._market_timer.stop()
            win.scheduler.stop()
            win.close()
            app.processEvents()

    print(f"共 {shots} 张 → {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="离屏导出界面截图（供肉眼验收）")
    parser.add_argument("out_dir", help="输出目录")
    parser.add_argument("--themes", default="silver,system",
                        help="逗号分隔：silver / system（默认两个都出，便于对比）")
    parser.add_argument("--sizes", default="960x900,1280x720",
                        help="逗号分隔的可用区域（逻辑像素），默认 960x900（用户那台 150%% 缩放）与 1280x720")
    args = parser.parse_args()
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    sizes = []
    for item in args.sizes.split(","):
        w, _, h = item.strip().partition("x")
        sizes.append((int(w), int(h)))
    return render(Path(args.out_dir).expanduser(), themes, sizes)


if __name__ == "__main__":
    raise SystemExit(main())
