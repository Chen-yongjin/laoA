#!/usr/bin/env python3
"""把**几套候选配色**各渲染一张主界面图，供主人挑一套（2026-10-10）。

为什么要有这个脚本
------------------
主人这次的要求是"界面做得更美观、更科技更高级一点"，同时说"配色先出三套图挑一挑"。
配色是**看着挑**的东西：Agent 这边讲一堆"深空蓝配青绿更有质感"没有意义，
把同一页、同一批数据、同样的排版在三套配色下各出一张图，一眼就分得出高下。
（这也是 `文档/开发文档` 里那条原则："看不出渲染结果时，不做更容易误解的事"。）

怎么跑：
    QT_QPA_PLATFORM=offscreen /tmp/laoa-test/bin/python build/make_palette_preview.py
    可选：--themes tech,obsidian,graphite --pages market,settings --size 1280x820
输出：`docs/配色预览/*.png`

三个取舍
--------
- **每套配色都重建一次窗口**：皮肤是控件出生时"穿上"的（有些颜色在造控件时就写进
  控件自己的样式表了），在同一扇窗口上热切皮肤会留下上一套的旧色 —— 那样出的图不准；
- **复用 `make_screenshots.py` 的演示数据与假取数**：绝不联网、图里没有任何真实数据，
  而且"演示库 + 假行情"只有一份实现（两个脚本各写一份迟早对不上）；
- 图**不含桌面画布与桌宠**：这次要挑的是窗口内的配色与标题栏，画布会把图撑大、
  缩小后反而看不清细节。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 离屏：无显示器也能出图

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "build"))                 # 复用截图脚本里的造数工具

# 演示用的"家目录"：授权、配置都落在临时目录 —— 出图脚本不该碰真实用户目录
_DEMO_HOME = Path(tempfile.mkdtemp(prefix="laoa-palette-home-"))
os.environ["HOME"] = str(_DEMO_HOME)
os.environ["APPDATA"] = str(_DEMO_HOME / "AppData" / "Roaming")
os.environ["LOCALAPPDATA"] = str(_DEMO_HOME / "AppData" / "Local")
for _key in ("APPDATA", "LOCALAPPDATA"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication                  # noqa: E402

import make_screenshots as shots                            # noqa: E402
from laoa_trader.ui import theme as theme_mod               # noqa: E402

#: 出图目录（与 `docs/截图/` 分开：那批是"产品图"，这批是"挑配色的对比图"）
OUT_DIR = REPO / "docs" / "配色预览"

#: 配色名 → 图里用的中文名（前缀 A/B/C 是为了排序与"我挑 B"这种说法）
PALETTE_TITLES: dict[str, str] = {
    "tech": "A-科技蓝（现在的默认）",
    "obsidian": "B-曜黑电光蓝",
    "graphite": "C-石墨紫罗兰",
}

#: 页签名 → 抓哪一页（页码与主窗口的页签顺序一致）
PAGE_INDEX: dict[str, int] = {"market": 0, "watch": 1, "settings": 4}
PAGE_TITLES: dict[str, str] = {"market": "大盘概览", "watch": "自选标的", "settings": "系统设置"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染候选配色的界面预览图")
    parser.add_argument("--themes", default=",".join(theme_mod.PREVIEW_THEMES),
                        help="要出图的配色名（逗号分隔，见 theme.PALETTES）")
    parser.add_argument("--pages", default="market,settings",
                        help="要出图的页面：market / watch / settings")
    parser.add_argument("--size", default="1280x860", help="窗口尺寸，例如 1280x860")
    return parser.parse_args()


def _render(app, cfg, theme_name: str, page: str, size: tuple[int, int]) -> Path:
    """按某套配色渲染某一页并存图（**每套配色重建一次窗口**，见模块说明）。"""
    from laoa_trader.ui import app as ui_app

    # 皮肤的判据是 `cfg.ui_theme`（主窗口构造时自己会应用一次），所以**先写配置再建窗口**：
    # 在同一扇窗口上热切皮肤会留下上一套的旧色（有些颜色是造控件时写进控件自己的样式表里的）
    cfg.ui_theme = theme_name
    theme_mod.apply_palette(app, theme_name)
    win = ui_app.MainWindow(cfg)
    win.resize(*size)
    win.show()
    app.processEvents()
    shots._after_window_built(win, app)
    # 概览页要**注入假的取数客户端**（否则它会去打真接口，图里就是一片占位符）
    win.refresh_market_overview(force=True, client=shots._fake_market_client())
    shots._wait_market(win, app)
    win.tabs.setCurrentIndex(PAGE_INDEX[page])
    app.processEvents()
    if page == "watch":
        shots._align_position_costs(win)
        app.processEvents()

    label = PALETTE_TITLES.get(theme_name, theme_name)
    name = f"{label}-{PAGE_TITLES[page]}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    win.grab().save(str(path), "PNG")

    # 收尾：主窗口带的线程/定时器/桌宠都要收干净（项目里踩过坑的地方，别省）
    try:
        win.shutdown()
    except Exception:                     # noqa: BLE001 - 出图脚本，收尾失败不影响已存的图
        pass
    win.close()
    app.processEvents()
    return path


def main() -> int:
    args = _parse_args()
    themes = [name.strip() for name in args.themes.split(",") if name.strip()]
    pages = [name.strip() for name in args.pages.split(",") if name.strip()]
    width, height = (int(part) for part in args.size.lower().split("x"))

    app = QApplication.instance() or QApplication([])

    shots._check_demo_heatmap()
    tmp = Path(tempfile.mkdtemp(prefix="laoa-palette-"))
    cfg = shots.Config(data_dir=tmp / "data")
    cfg.ensure_dirs()
    cfg.source_path = tmp / "config.toml"
    cfg.notify_channels = []              # 出图时绝不真发通知
    cfg.hithink_api_key = ""
    cfg.stop_loss = 0.08
    cfg.take_profit = 0.15
    shots._seed_demo_db(cfg)
    # 顺序要紧：**先**拦住联网取数，再建窗口（与截图脚本同一条规矩）
    shots._patch_sector_rank()
    shots._block_network_fetches()
    shots._pretend_windows_voices()

    saved: list[Path] = []
    for theme_name in themes:
        for page in pages:
            saved.append(_render(app, cfg, theme_name, page, (width, height)))
            print(f"  {saved[-1].relative_to(REPO)}  "
                  f"({saved[-1].stat().st_size // 1024} KB)")

    print(f"共 {len(saved)} 张 → {OUT_DIR.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
