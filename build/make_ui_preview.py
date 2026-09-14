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

产出（每个尺寸 × 每个主题一套）：
    01-大盘概览-<主题>.png … 07-关于-<主题>.png

设计取舍
--------
- **不联网**：大盘数据用一份**写死的真实样例**（取自 2026-09-14 收盘后的实测值）直接喂给
  渲染函数 —— 跑这个脚本不该需要 API Key，也不该因为限流/断网出不了图。
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

#: 一份**真实的**大盘概览样例（2026-09-14 收盘后实测），只为出图好看、且不联网
SAMPLE_OVERVIEW = {
    "as_of": "2026-09-14 15:03",
    "limits": {"up": 55, "down": 16, "break": 30},
    "turnover": {"sh": 779_280_000_000, "sz": 849_890_000_000,
                 "bj": 14_010_000_000, "total": 1_643_030_000_000},
    "breadth": {"up": 3126, "down": 2224, "flat": 221, "total": 5571},
    "breadth_enabled": True,
    "stale": False,
    "failed": [],
    "errors": [],
    "indices": [
        {"thscode": "000001.SH", "name": "上证指数", "last": 3885.33, "change_pct": -0.07},
        {"thscode": "399001.SZ", "name": "深证成指", "last": 13384.57, "change_pct": -0.64},
        {"thscode": "399006.SZ", "name": "创业板指", "last": 3285.58, "change_pct": -1.10},
        {"thscode": "000688.SH", "name": "科创50", "last": 1528.27, "change_pct": -1.62},
        {"thscode": "000300.SH", "name": "沪深300", "last": 4480.08, "change_pct": -0.67},
    ],
    "sentiment": [
        {"thscode": "883404.TI", "name": "同花顺情绪", "last": 885.53, "change_pct": -0.18},
        {"thscode": "883958.TI", "name": "昨日连板", "last": 6928.17, "change_pct": 3.53},
        {"thscode": "883994.TI", "name": "昨日打首板", "last": 1551.88, "change_pct": 1.69},
        {"thscode": "883418.TI", "name": "微盘股", "last": 2131.00, "change_pct": 0.95},
    ],
    "sector": [
        {"thscode": "881155.TI", "name": "银行", "last": 1408.77, "change_pct": 0.88},
        {"thscode": "881157.TI", "name": "证券", "last": 1428.64, "change_pct": -0.27},
    ],
}


def _seed(cfg) -> None:
    """造一点像样的数据：股票池 / 自选股 / 持仓 / 提醒都有内容，截图才看得出排版。"""
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
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, stocks)
        rows = []
        for i, (symbol, _n, _ind) in enumerate(stocks):
            base = 3.0 + i * 9
            for d_i, day in enumerate(days):
                price = base * (1 + 0.003 * d_i)
                rows.append((symbol, day, price, price, price, price,
                             2e7, 2e7 * price, 1.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
        storage.write_adjust_events(conn, [("600001", days[len(days) // 2],
                                            0.1, 0.0, 0.0, 0.0)])
        storage.save_pool(conn, [
            {"symbol": "600001", "name": "低价样本", "strategy": "LowPriceStrategy",
             "strategies": "低价股", "score": 0.812, "reason": "低价+缩量回踩",
             "label": "波段·T+10（T+10）", "source_label": "波段·T+10（T+10）",
             "industry": "银行", "note": "龙头"},
            {"symbol": "600002", "name": "半导体甲", "strategy": "VolatilitySqueeze",
             "strategies": "高窄旗形整理", "score": 0.664, "reason": "旗形整理末端",
             "label": "波段·T+10（T+10）", "source_label": "策略",
             "industry": "半导体", "note": ""},
            {"symbol": "300750", "name": "电池龙头", "strategy": "",
             "strategies": "自选", "score": None, "reason": "自选股",
             "label": "自选", "source_label": "自选", "industry": "电池",
             "note": "消息面"},
        ], days[-1])
        storage.upsert_watchlist(conn, "300750", name="电池龙头", note="消息面")
        storage.upsert_position(conn, "600001", name="低价样本",
                                quantity=1000, avg_cost=3.05, note="试仓")
        storage.record_alerts(conn, [
            {"symbol": "600002", "name": "半导体甲", "kind": "放量上攻",
             "price": 12.8, "detail": "量比 2.4，触及日内高点"},
        ], days[-1])


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
    market.fetch_overview = lambda *a, **k: dict(SAMPLE_OVERVIEW)   # 不联网

    shots = 0
    for theme_name in themes:
        # 主题跟着 **config** 走（窗口自己在 __init__ 里按 `cfg.ui_theme` 应用）——
        # 只调 `apply_theme(app, …)` 会被窗口构造时的那次覆盖掉，
        # 结果是"两种主题的图一模一样"（第一版就踩了这个坑）
        cfg.ui_theme = theme_name
        for width, height in sizes:
            # 离屏平台的"真屏幕"是固定值，所以这里注入可用区域 → 出来的图就是
            # 用户那台机器（逻辑像素）上会得到的布局
            avail = _available(width, height)
            ui_app._available_geometry = lambda avail=avail: avail
            win = ui_app.MainWindow(cfg)
            win.show()
            app.processEvents()
            win._on_market_overview_ready(dict(SAMPLE_OVERVIEW))
            win._tick()
            app.processEvents()

            tabs = [(i, win.tabs.tabText(i)) for i in range(win.tabs.count())]
            for index, title in tabs:
                win.tabs.setCurrentIndex(index)
                app.processEvents()
                QTimer.singleShot(0, lambda: None)
                app.processEvents()
                name = f"{index + 1:02d}-{title.replace(' ', '')}-{theme_name}-{width}x{height}.png"
                win.grab().save(str(out_dir / name))
                shots += 1
                print(f"  {name}")

            # 「关于」与「状态详情」两个弹窗单独出图（它们也是用户会看到的界面）
            win.on_about()
            app.processEvents()
            if win.about_dialog is not None:
                win.about_dialog.grab().save(
                    str(out_dir / f"90-关于弹窗-{theme_name}-{width}x{height}.png"))
                shots += 1
                win.about_dialog.close()
            win.on_show_status_details()
            app.processEvents()
            if win.status_dialog is not None:
                win.status_dialog.grab().save(
                    str(out_dir / f"91-状态详情-{theme_name}-{width}x{height}.png"))
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
