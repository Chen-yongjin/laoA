#!/usr/bin/env python3
"""给「财神助手」抓一套界面截图（**离屏渲染 + 全合成演示数据**）。

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

主界面那几张**不是干抓窗口**，而是合成到一张"桌面"画布上：主窗口贴左上、桌宠贴右下角
（见 `_desktop_canvas`）—— 主人 2026-09-23 的要求：「图片角落把桌宠也带上」，
要让图看起来是"桌宠站在桌面上陪着主窗口"，而不是只有 `09-桌宠提醒.png` 那张特写。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 离屏：无显示器也能抓图

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))                           # 借用 tests/ 里的假数据与假客户端

# 演示用的"家目录"：授权状态、配置文件都落在临时目录里 —— 截图脚本绝不该
# 往真实用户目录（`~/.config/caishen-helper`、`%APPDATA%\CaishenHelper`）写东西，
# 否则跑一次脚本就改了本机的授权/试用记账，那是实打实的副作用。
_DEMO_HOME = Path(tempfile.mkdtemp(prefix="laoa-shots-home-"))
os.environ["HOME"] = str(_DEMO_HOME)
os.environ["APPDATA"] = str(_DEMO_HOME / "AppData" / "Roaming")
os.environ["LOCALAPPDATA"] = str(_DEMO_HOME / "AppData" / "Local")
for _key in ("APPDATA", "LOCALAPPDATA"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import QRectF, Qt                    # noqa: E402
from PySide6.QtGui import (                              # noqa: E402
    QBrush, QColor, QImage, QLinearGradient, QPainter, QPixmap,
)
from PySide6.QtWidgets import (                          # noqa: E402
    QApplication, QGroupBox, QLabel, QScrollArea,
)

from laoa_trader.config import Config                    # noqa: E402
from laoa_trader.data import storage                     # noqa: E402

#: 统一窗口尺寸（整齐、且 1440×900 在常见屏幕上放得下）
WINDOW_SIZE = (1440, 900)
#: 截图输出目录
OUT_DIR = REPO / "docs" / "截图"

# ── 桌面画布（主窗口 + 角落里的桌宠）────────────────────────────────────
#
#: 桌面画布四周留白（像素）
CANVAS_MARGIN = 18
#: 主窗口与桌宠之间的横向间隙（像素）：**宁可画布宽一点，也不让桌宠压住窗口右边的列**
PET_GAP = 28
#: 桌面底色（浅灰渐变；纯色大块压缩后几乎不占 PNG 体积）
WALLPAPER_TOP = "#f4f6f9"
WALLPAPER_BOTTOM = "#dbe1e9"
#: 桌宠脚下那点影子的尺寸/透明度（让它像"站在桌面上"，不是"漂着"）
PET_SHADOW_SIZE = (112, 15)
PET_SHADOW_ALPHA = 34

# ── 演示数据 ──────────────────────────────────────────────────────────────
#
# ⚠️ 下面这些数字**全是编的，不是真实行情**（截图要发给客户，必须可重复、也不能
# 让人以为是当天的大盘）。但它们必须"看着像真在用的账户"：
#
#   主人 2026-09-23 的原话：「里面的自选标的都是乱七八糟的什么玩意，一看就知道不会炒股。
#   找小盘最近涨幅大的股票加进自选和持仓，匹配结果里面也搞好一点」
#
# 所以这一版的数据口径（改数据时按这张表对一遍，别凭感觉调）：
#   * **自选标的**：10 行，全是**真实存在的小盘股**（名称与代码配套，300/002/603/001 段）；
#     当日涨幅 +3%~+10%、流通市值 30~300 亿、换手 3%~15%、价格 5~60 元；
#     加入日期在过去 3~30 个交易日内，盈亏以盈利为主（−4%~+16%），来源显示的就是那条策略名
#     （2026-09-23 起不再带 `策略·` 前缀、也不拼 `+自选`），留一行显示「自选」。
#   * **持仓监控**：8 行，成本价与现价自洽（盈亏 = 现价/成本 − 1，由 `_align_position_costs`
#     在抓图前回写保证），盈亏 −5%~+20%，止损/止盈位按成本 ±8%/±15%（见 `main()` 里设的比例）。
#   * **匹配结果**：12 行，清一色 `尾盘匹配策略`（尾盘策略刚跑完一轮的样子）。
#   * **大盘概览**：像"略偏强的一天"（上涨 3120 / 下跌 2020 / 平盘 420，合计 5560、涨停 52、跌停 11、炸板 24、
#     两市成交 1.6 万亿），板块用真实板块名，且板块名与本地行业同名（否则涨停/跌停家数是 `—`）。
#
# 为什么用"真实股票名 + 编的行情"这个组合：名字配错（比如写"机器人龙头 600001"）散户一眼
# 就看出是假数据；而价格/市值/涨跌幅本来就每天在变，编成"小盘强势股"的量级反而最自然。

#: 全部演示标的：`(代码, 名称, 行业)`。**名称与代码必须是配套的真实标的**
#: （自选/持仓/匹配结果三张表共用这一份，图里才是"同一个人的同一个盘面"）。
#: 行业用真实板块名，且与「大盘概览 → 热门板块」那几个板块对得上。
DEMO_STOCKS: tuple[tuple[str, str, str], ...] = (
    ("002273", "水晶光电", "光学光电子"),
    ("300748", "金力永磁", "稀土永磁"),
    ("002472", "双环传动", "通用设备"),
    ("603005", "晶方科技", "半导体"),
    ("300101", "振芯科技", "军工电子"),
    ("002402", "和而泰", "消费电子"),
    ("300811", "铂科新材", "金属新材料"),
    ("002130", "沃尔核材", "电网设备"),
    ("300638", "广和通", "通信设备"),
    ("002979", "雷赛智能", "自动化设备"),
    # ── 下面这些只出现在「匹配结果」里（自选里挑了一部分加进去，其余是"看上了还没加"）──
    ("300913", "兆龙互连", "通信设备"),
    ("300593", "新雷能", "军工电子"),
    ("300458", "全志科技", "半导体"),
    ("002965", "祥鑫科技", "汽车零部件"),
    ("001309", "德明利", "半导体"),
    ("002335", "科华数据", "电源设备"),
    ("603297", "永新光学", "光学元件"),
    ("002378", "章源钨业", "小金属"),
    ("300496", "中科创达", "软件开发"),
    ("002444", "巨星科技", "通用设备"),
)

#: 实时快照（现价, 当日涨幅%, 换手率%, 流通市值亿）—— 注入到快照缓存里，
#: 两张表的「现价/涨幅/市值/换手」与匹配结果那几列都从这里取。
#: 量级按"小盘活跃股"编：市值 46~322 亿、换手 3%~14%、当日涨幅 +3%~+9.4%。
DEMO_QUOTES: dict[str, tuple[float, float, float, float]] = {
    "002273": (21.86, 6.42, 8.6, 268.0),
    "300748": (22.40, 5.18, 6.9, 292.0),
    "002472": (27.35, 7.63, 9.4, 235.0),
    "603005": (24.18, 4.35, 7.2, 158.0),
    "300101": (19.76, 8.21, 11.3, 112.0),
    "002402": (15.62, 3.86, 5.4, 143.0),
    "300811": (38.90, 9.12, 12.6, 98.0),
    "002130": (21.05, 5.77, 10.2, 264.0),
    "300638": (20.44, 4.98, 6.1, 156.0),
    "002979": (31.26, 6.05, 8.8, 96.0),
    "300913": (32.15, 7.85, 12.0, 74.0),
    "300593": (24.60, 6.71, 9.7, 105.0),
    "300458": (32.40, 4.62, 7.8, 208.0),
    "002965": (28.74, 5.35, 10.6, 118.0),
    "001309": (58.20, 3.18, 12.1, 132.0),
    "002335": (30.88, 3.42, 4.9, 187.0),
    "603297": (41.30, 6.88, 5.7, 46.0),
    "002378": (12.86, 9.35, 13.4, 132.0),
    "300496": (52.30, 4.85, 6.4, 241.0),
    "002444": (26.90, 5.10, 3.2, 322.0),
}

#: 自选标的：代码 → (**入库时的来源**, 距今几个交易日加的, 加入价系数, 监控开关)
#:
#: 来源写"库里那种数据形态"（`公式·X`，界面上显示成 `X` —— 不带前缀、不带 `+自选`）；
#: 留一行空来源，用来展示"手工加的自选"显示成「自选」。
#: 加入价 = 现价 × 系数（由 `_align_added_prices` 在抓图前对齐），
#: 所以 **盈亏 = 1/系数 − 1**：0.86 → +16.3%，1.04 → −3.8%。
DEMO_WATCH: dict[str, tuple[str, str, float, bool]] = {
    # 匹配结果那 12 只**都在自选里**（"选出来就加进去"是常规用法）——
    # 这样自选标的表里不会出现"加入日期/盈亏 = —"的空行
    "002273": ("公式·尾盘匹配策略", 3, 0.878, True),       # +13.9%
    "300748": ("公式·尾盘匹配策略", 5, 0.925, True),       # +8.1%
    "002472": ("公式·尾盘匹配策略", 6, 0.901, True),       # +11.0%
    "603005": ("公式·首板缩量整理", 8, 1.028, True),       # −2.7%
    "300101": ("公式·尾盘匹配策略", 10, 0.883, True),      # +13.2%
    "300811": ("公式·尾盘匹配策略", 12, 0.868, True),      # +15.2%
    "002130": ("公式·尾盘匹配策略", 14, 0.917, True),      # +9.1%
    "300638": ("公式·地量后放量变盘", 17, 0.952, False),   # +5.0%（这只关掉监控）
    "300913": ("公式·尾盘匹配策略", 19, 0.934, True),      # +7.1%
    "300593": ("公式·尾盘匹配策略", 21, 1.012, True),      # −1.2%
    "300458": ("公式·短期反转", 23, 0.906, True),          # +10.4%
    "002965": ("公式·尾盘匹配策略", 26, 1.036, True),      # −3.5%
    "002979": ("", 28, 1.018, True),                      # 手工加的 → 来源显示「自选」
}

#: 持仓监控：代码 → (成本价系数（成本 = 现价 × 系数）, 备注, 监控开关)
#:
#: 同样用系数：**盈亏 = 1/系数 − 1**（0.833 → +20.0%，1.053 → −5.0%）。
#: 止损位/止盈位由程序按 `cost × (1 ∓ 比例)` 算（比例在 `main()` 里设成 8% / 15%）。
DEMO_POSITIONS: dict[str, tuple[float, str, bool]] = {
    "300748": (0.885, "机器人主线，拿住", True),        # +13.0%
    "002472": (0.940, "", True),                       # +6.4%
    "300101": (0.833, "军工电子，已经止盈一半", True),  # +20.0%
    "002273": (1.053, "追高了，盯着止损", True),        # −5.0%
    "300811": (0.909, "", True),                       # +10.0%
    "002130": (0.968, "", False),                      # +3.3%（关掉监控）
    "603005": (1.024, "", False),                      # −2.3%（关掉监控）
    "300638": (0.943, "算力模组，中线", True),          # +6.0%
}

#: 演示用的板块热力图：`(行业名, 当日涨幅%, 行业流通市值合计亿)`（**全是编的**）。
#:
#: 四条硬要求（改数据时按这张单子对一遍）：
#:   ① 行业名用**真实行业名**（同花顺/腾讯那套叫法），别造"AI概念"这种没有出处的名字；
#:   ② 涨幅与市值要有**层次**：大到银行/白酒（近 10 万亿）小到几百亿的小行业，
#:      红绿都有、深浅都有 —— 热力图的价值就在"一眼看出钱压在哪、往哪边动"，
#:      一片均匀的浅色等于什么都没说；
#:   ③ 下面 **`DEMO_SECTORS` 里那 10 个板块必须在这里出现、且涨幅完全一致**：
#:      热力图的 tooltip 会显示"涨停家数 / 主力净额"（来自板块榜，见 `_patch_sector_rank`），
#:      同一个行业在两个数字打架（榜上 +3.42%、图上 -1% ）是最不能出的错；
#:   ④ 市值合计落在 A 股流通市值的量级（40~80 万亿，见 `_check_demo_heatmap`）。
DEMO_HEATMAP: tuple[tuple[str, float, float], ...] = (
    ("银行", -1.36, 98000), ("白酒", -2.74, 38000), ("半导体", 2.84, 41000),
    ("电池", 2.35, 26000), ("通信设备", 1.96, 23000), ("软件开发", 3.12, 21000),
    ("电力行业", -0.28, 19000), ("证券", -1.24, 17000), ("消费电子", 1.42, 16500),
    ("汽车零部件", 0.88, 15000), ("化学制药", -0.42, 13500), ("医疗器械", -0.65, 12000),
    ("石油行业", -0.74, 11500), ("电网设备", 1.55, 10500), ("煤炭", -1.82, 9500),
    ("保险", 0.35, 9000), ("有色金属", 1.18, 8500), ("化学制品", 0.76, 8100),
    ("家电行业", 0.92, 7200), ("房地产", -2.15, 7000), ("光学光电子", 0.85, 6800),
    ("通用设备", 1.32, 6000), ("食品饮料", -0.72, 5800), ("工程建设", -0.92, 5600),
    ("自动化设备", 2.05, 5500), ("专用设备", 1.08, 5200), ("计算机设备", 1.68, 4900),
    ("钢铁行业", -0.68, 4600), ("通信服务", -0.36, 4500), ("军工电子", 2.62, 4200),
    ("创新药", 1.28, 4100), ("农牧饲渔", 0.48, 3900), ("机器人", 3.42, 3600),
    ("航运港口", -0.92, 3400), ("铁路公路", 0.24, 3200), ("贵金属", 3.86, 3000),
    ("航空机场", -0.44, 2900), ("水泥建材", -1.12, 2800), ("物流行业", -0.58, 2600),
    ("航天装备", 1.86, 2600), ("固态电池", 2.86, 2400), ("装修建材", -0.86, 2400),
    ("商业百货", -0.36, 2100), ("CPO", 2.31, 1900), ("纺织服装", 0.28, 1900),
    ("旅游酒店", 0.66, 1800), ("塑料制品", 0.36, 1600), ("燃气", -0.32, 1500),
    ("造纸印刷", -0.55, 1400), ("橡胶制品", 0.42, 1200), ("液冷", 1.94, 1100),
    ("教育", 0.54, 900),
)

#: 每个行业拆成几只成分股（3~6 只）—— 热力图 tooltip 里"成分股：N 只"要有个像样的数。
#: 拆的时候**涨跌幅与行业一致**（同一行业里所有成分股同涨同跌）：这样"市值加权涨跌幅"
#: 一定等于表里那个数，不会出现"图上写 +2.84%、tooltip 里加权出来 +2.61%"这种自相矛盾。
DEMO_HEATMAP_SPLIT: tuple[float, ...] = (0.40, 0.25, 0.15, 0.10, 0.06, 0.04)


def _demo_heatmap_rows() -> tuple[list[tuple[str, str, str]], dict[str, dict]]:
    """演示热力图的成分股：`([(代码, 名称, 行业)], {代码: 快照})`。

    代码用 `9xxxxx` 段（不是真实 A 股号段）—— 万一这些行漏进某个界面，一眼能看出是演示数据；
    它们只写进临时库的 `stock_basic`（热力图靠它做行业归属），不参与涨跌停家数那套统计
    （没有日线，也就永远不会被数成涨停）。
    """
    rows: list[tuple[str, str, str]] = []
    quotes: dict[str, dict] = {}
    serial = 0
    for industry, pct, cap in DEMO_HEATMAP:
        count = 3 + (serial % 4)               # 3~6 只，反复跑结果一样（不用随机）
        parts = DEMO_HEATMAP_SPLIT[:count]
        scale = sum(parts)
        for index, part in enumerate(parts):
            serial += 1
            symbol = f"9{serial:05d}"
            name = f"{industry}成分{'甲乙丙丁戊己'[index]}"
            rows.append((symbol, name, industry))
            quotes[symbol] = {
                "symbol": symbol, "name": name, "last_price": 10.0 + index,
                "pct": pct, "circ_mktcap": round(cap * part / scale, 2),
                "turnover_rate": 2.0 + index, "volume_ratio": 1.0 + index * 0.1,
            }
    return rows, quotes


def _check_demo_heatmap() -> None:
    """演示热力图数据的自检（**数字编得不对就让截图脚本直接失败**，别把错图存下来）。

    查三件事：市值合计在 A 股量级、涨跌两边都有、`DEMO_SECTORS` 里那 10 个板块都在
    且涨幅一致（见 `DEMO_HEATMAP` 上头第 ③ 条）。
    """
    table = {name: pct for name, pct, _cap in DEMO_HEATMAP}
    total = sum(cap for _n, _p, cap in DEMO_HEATMAP)
    if not 40e4 <= total <= 80e4:
        raise SystemExit(f"演示热力图的流通市值合计 {total / 1e4:.1f} 万亿，"
                         f"不在 40~80 万亿这个量级（改 DEMO_HEATMAP）")
    if not any(p > 0 for p in table.values()) or not any(p < 0 for p in table.values()):
        raise SystemExit("演示热力图必须红绿都有（见 DEMO_HEATMAP 上头第 ② 条）")
    for name, pct, _net in DEMO_SECTORS:
        if name not in table:
            raise SystemExit(f"板块榜里的「{name}」没出现在 DEMO_HEATMAP 里"
                             f"（tooltip 的涨停家数会落空，见第 ③ 条）")
        if abs(table[name] - pct) > 1e-9:
            raise SystemExit(f"「{name}」两处涨幅不一致：热力图 {table[name]} / 板块榜 {pct}")


#: 演示用的板块榜（**注入**，不联网）：`(板块名, 当日涨幅%, 主力净额元)`
#:
#: 三条硬要求：
#:   ① 板块名用**真实板块名**（机器人/固态电池/CPO/液冷/创新药 这些散户天天听到的）；
#:   ② 板块名必须与下面 `DEMO_SECTOR_STOCKS` 里那些成分股的行业**同名** ——
#:      否则「涨停数量/跌停数量」两列全是 `—`（程序是按行业名去本地库里数家数的）；
#:   ③ 前五个是上涨板块、后五个是下跌板块（页面按涨幅正负分成"上涨前五/下跌前五"两张表）。
DEMO_SECTORS: tuple[tuple[str, float, float], ...] = (
    ("机器人", 3.42, 38.62e8), ("固态电池", 2.86, 26.41e8), ("CPO", 2.31, 31.07e8),
    ("液冷", 1.94, 15.83e8), ("创新药", 1.28, 9.24e8),
    ("白酒", -2.74, -32.16e8), ("房地产", -2.15, -27.48e8),
    ("煤炭", -1.82, -18.63e8), ("银行", -1.36, -21.35e8), ("航运港口", -0.92, -8.74e8),
)

#: 按板块造的"成分股"（**只用来喂板块的涨停/跌停家数，不会出现在任何一张图里**）：
#: 前 5 个板块里各放 1~3 只当日涨停的，后 5 个板块里各放 1~2 只跌停的。
#: 代码一律用主板 6 开头 —— 涨跌停就是 ±10%（项目里实测的板块规则；用创业板会变 20%，家数就对不上）。
DEMO_SECTOR_STOCKS: tuple[tuple[str, str, str, str], ...] = (
    # ── 上涨板块（涨停）──
    ("600101", "机器人成分甲", "机器人", "up"), ("600102", "机器人成分乙", "机器人", "up"),
    ("600103", "机器人成分丙", "机器人", "up"),
    ("600104", "固态电池成分甲", "固态电池", "up"),
    ("600105", "固态电池成分乙", "固态电池", "up"),
    ("600106", "CPO成分甲", "CPO", "up"), ("600107", "CPO成分乙", "CPO", "up"),
    ("600108", "液冷成分甲", "液冷", "up"),
    ("600109", "创新药成分甲", "创新药", "up"),
    # ── 下跌板块（跌停）──
    ("600201", "白酒成分甲", "白酒", "down"), ("600202", "白酒成分乙", "白酒", "down"),
    ("600203", "房地产成分甲", "房地产", "down"), ("600204", "房地产成分乙", "房地产", "down"),
    ("600205", "煤炭成分甲", "煤炭", "down"), ("600206", "煤炭成分乙", "煤炭", "down"),
    ("600207", "银行成分甲", "银行", "down"),
    ("600208", "航运港口成分甲", "航运港口", "down"),
)

#: 消息列表 / 桌宠气泡里那几条提醒 —— 用的都是上面自选/持仓里的票，
#: 文字与那张表的数字对得上（例：振芯科技确实在 +20%，所以它那条是"触及止盈位"）。
DEMO_ALERTS: tuple[tuple[str, str, str, str], ...] = (
    ("300101", "take_profit", "振芯科技 300101 触及止盈位 +20.0%，现价 19.76", "09:41"),
    ("002273", "stop_loss", "水晶光电 002273 已跌破成本价 23.02 的 −5%（止损提醒）", "09:52"),
    ("002472", "surge", "双环传动 002472 放量突破 20 日高点，量比 2.6", "10:06"),
    ("300811", "limit_up_open", "铂科新材 300811 涨停打开，现价 38.90（+9.12%）", "10:23"),
    ("002130", "pullback", "沃尔核材 002130 回踩 5 日线买点，缩量至 5 日均量 0.7 倍", "13:47"),
    ("300748", "break_ma5", "金力永磁 300748 跌破 5 日线（22.40），注意减仓", "14:12"),
)

#: 桌宠两处气泡的文案 —— 都用 `DEMO_ALERTS` 里那条提醒的票，不是另编一只：
#:   * 桌面合成图（01~07 的右下角）用「涨停打开」，票是自选/持仓里都有的铂科新材；
#:   * `09-桌宠提醒.png`（特写）用「止损提醒」，与持仓表里水晶光电 −5.04% 对得上。
#: 气泡在 180 像素宽里换行显示，太长会被截成"…"，所以这里都是短句。
PET_BUBBLE_CORNER = "铂科新材 300811 涨停打开"
PET_BUBBLE_CLOSEUP = "水晶光电 002273 止损提醒：已跌破 −5%"

#: 匹配结果那一屏的行（12 行，来源统一是尾盘策略 —— "刚跑完一轮"的样子）
DEMO_RESULT: tuple[str, ...] = (
    "002273", "300748", "002472", "603005", "300101", "300811",
    "002130", "300638", "300913", "300593", "300458", "002965",
)

#: 编辑器里那条公式（与随包的「尾盘匹配策略」同一条，主人 2026-09-23 定稿）
DEMO_FORMULA_NAME = "尾盘匹配策略"
DEMO_FORMULA_NOTE = "小盘尾盘强势股：市值 30~500 亿 + 换手 >3% + 放量 + 均线多头 + 近 10 日涨停"
DEMO_FORMULA_TEXT = (
    "ZC:=C/REF(C,1)-1\n"
    "J5:=MA(C,5)\n"
    "J10:=MA(C,10)\n"
    "J20:=MA(C,20)\n"
    "GAIN_OK:=ZC>=0.02 AND ZC<=0.07\n"
    "PRICE_OK:=C>=3 AND C<=50\n"
    "MKT_OK:=流通市值>=30 AND 流通市值<=500\n"
    "HSL_OK:=换手率>3\n"
    "VOL_OK:=V>REF(V,1)*1.3\n"
    "TREND_OK:=J5>J10 AND J10>J20 AND C>J5\n"
    "ZT_OK:=涨停天数(10)>=1\n"
    "HOT_OK:=热门行业>=1\n"
    "BOARD_OK:=ST=0 AND 科创=0 AND 北交所=0\n"
    "GAIN_OK AND PRICE_OK AND MKT_OK AND HSL_OK AND VOL_OK AND TREND_OK"
    " AND ZT_OK AND HOT_OK AND BOARD_OK"
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
            pct = DEMO_QUOTES[symbol][1] / 100.0        # 当日涨幅（与注入的快照同一个数）
            prev = last / (1 + pct)                     # 昨收：最后一根的涨幅 = 快照涨幅
            slope = 0.005 + 0.0012 * (index % 3)        # 60 个交易日涨 30%~45%，像"最近涨幅大的小盘股"
            start = prev / (1 + slope * max(last_i - 1, 1))
            for i, day in enumerate(days):
                price = last if i == last_i else start * (1 + slope * i)
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
        # 热力图的成分股也要进 `stock_basic`：行业归属（热力图的方块名）读的就是这张表，
        # 不入库的话整张图会缩成一个「未分类」的方块
        storage.write_stock_basic(conn, _demo_heatmap_rows()[0])
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
        # 再补两条**连板**（用板块里真涨停的那两只）：页面上的"连板/涨停天数"才有出处
        storage.write_limit_up_pool(conn, [
            (days[-1], "600101", "机器人成分甲", 2, "2连板", "09:31:00", "09:31:00",
             1.2e9, 0, "机器人", 5.0, 20.0, 1e9, 0, 2, "连板", 1.1e9, 11.0, 0, "hithink", "t"),
            (days[-1], "600104", "固态电池成分甲", 3, "3连板", "09:30:12", "09:30:12",
             1.6e9, 0, "固态电池", 5.0, 30.0, 1e9, 0, 3, "连板", 1.5e9, 11.0, 0, "hithink", "t"),
        ])
        for symbol, (source, back_days, ratio, monitor) in DEMO_WATCH.items():
            name = dict((s, n) for s, n, _ in DEMO_STOCKS)[symbol]
            added_price = round(DEMO_QUOTES[symbol][0] * ratio, 2)
            storage.upsert_watchlist(conn, symbol, name=name, enabled=monitor,
                                     price=added_price, source_strategy=source or None)
            # 加入日期 = 距今 back_days 个交易日（界面「加入日期」那一列读的就是 added_at）
            added_day = days[max(0, last_i - int(back_days))]
            conn.execute("UPDATE watchlist SET added_at = ? WHERE symbol = ?",
                         (f"{added_day} 09:35:00", symbol))
        for symbol, (_ratio, note, monitor) in DEMO_POSITIONS.items():
            name = dict((s, n) for s, n, _ in DEMO_STOCKS)[symbol]
            # 先按"演示现价 × 系数"写一份；建好窗口之后再按**表格真正显示的现价**精调
            # （见 `_align_position_costs`）—— 两处口径必须是同一个数
            cost = round(DEMO_QUOTES[symbol][0] * _ratio, 2)
            storage.upsert_position(conn, symbol, name=name, avg_cost=cost, note=note)
            # 监控开关（`position.monitor`；upsert 不收这个参数，直接改库）—— 图里要有开有关
            conn.execute("UPDATE position SET monitor = ? WHERE symbol = ?",
                         (1 if monitor else 0, symbol))
        # 当日池子：让「匹配结果」页有内容可显示（来源写 `公式·X` 的**数据**形态）
        storage.save_pool(conn, [
            {"symbol": symbol, "name": name,
             "strategy": "公式·尾盘匹配策略", "strategies": "公式·尾盘匹配策略",
             "score": round(6.0 - 0.2 * i, 1), "reason": industry}
            for i, (symbol, name, industry) in enumerate(
                (s, n, ind) for s, n, ind in DEMO_STOCKS if s in DEMO_RESULT
            )
        ], days[-1])
        storage.record_alerts(conn, [
            {"symbol": symbol, "kind": kind, "price": DEMO_QUOTES[symbol][0], "detail": detail}
            for symbol, kind, detail, _t in DEMO_ALERTS
        ], days[-1])
    _align_added_prices(conn)
    # 自检门槛：演示库只有 15 只票、60 个交易日，不放开的话数据闸门会（正确地）拦住匹配
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


def _demo_quote(symbol: str) -> dict:
    """一条演示快照（形状与 `ui.quotes.fetch_snapshot_prices` 的返回一致）。

    `at` / `as_of` 必须是**数字时间戳（epoch 秒）**：`quotes.is_fresh()` 用它判断
    "这条快照是不是今天的、够不够新"，字符串会被判成不新鲜 —— 界面上「现价」会退回
    本地收盘价（数值对得上、看不出来），而「市值/换手」直接显示 `—`。
    """
    import time

    price, pct, turnover, mktcap = DEMO_QUOTES.get(symbol, (10.0, 0.0, 1.0, 100.0))
    stamp = time.time()
    return {"symbol": symbol, "price": price, "pct": pct, "at": stamp, "as_of": stamp,
            "source": "演示数据", "turnover_rate": turnover, "circ_mktcap": mktcap}


def _block_network_fetches() -> None:
    """把"真去联网"的取数入口换掉（快照 / 板块榜 / 概览都换成固定数据）。

    为什么必须拦：这些图是要发出去的，必须**每次跑都一样**；真取一次行情，
    图里的价格、涨跌家数就跟着当天走，重跑一次就变了。

    ⚠️ 必须在**建主窗口之前**调用（`main()` 里的顺序不能改）：窗口一构造就可能起
    取数线程，那个线程用的是"当时那个取数函数"；等窗口建好再换，它已经把真实行情
    写进缓存了 —— 踩过一次，图上"水晶光电 21.86"变成了当天的真价 26.19，
    市值/换手/涨跌幅与两张表的盈亏全跟着变（图既不可重复，也不是要演示的盘面）。
    """
    from laoa_trader.data import sources
    from laoa_trader.ui import quotes as quotes_mod

    def _no_snapshot(_cfg, symbols, *_args, **_kwargs):
        return [{"symbol": str(s), **_demo_quote_fields(str(s))} for s in (symbols or [])]

    def _fake_snapshot(cfg, symbols, *_args, **_kwargs):
        # `ui.quotes.fetch_snapshot_prices` 的默认实现就是"调 sources.snapshot_map"，
        # 这里整条换掉：不管谁（定时器 / 手动刷新 / 构造时那一轮）来取，都只回演示数据
        return {str(s): _demo_quote(str(s)) for s in (symbols or [])}

    sources.snapshot_map = _no_snapshot                  # type: ignore[assignment]
    quotes_mod.fetch_snapshot_prices = _fake_snapshot    # type: ignore[assignment]

    # 热力图取的是**全市场带市值**的那条路（`sources.full_market_quotes`，生产上走东财分页）：
    # 这里换成演示的"全市场快照"。**必须拦**：不拦的话它真去发 28 个请求，
    # 而且图上会出现当天的真实涨跌（图就不可重复了）。
    stamp = time.time()
    demo_quotes = _demo_heatmap_rows()[1]
    sources.full_market_quotes = lambda _cfg: {          # type: ignore[assignment]
        symbol: {**quote, "source": "eastmoney", "as_of": stamp}
        for symbol, quote in demo_quotes.items()
    }


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
    """概览用假客户端：**数字按"略偏强的普通交易日"编**（不联网、每次跑都一样）。

    口径（与 `DEMO_STOCKS` 上头那段注释是一套，改的时候一起看）：
      * 涨跌家数：全市场 **5560 只**里 上涨 3120 / 下跌 2020 / 平盘 420（沪 2300 / 深 3000 / 北 260）；
      * 涨停 52 / 跌停 11 / 炸板 24（走 `totals`，页面直接显示这三个数）；
      * 沪深成交额 ≈ 1.6 万亿 —— 由下面**上证与深成两行的 turnover 相加**得出，别只改一处；
      * 宽基指数：上证 3xxx、深成 1xxxx、创业板 2xxx、科创50、上证50、中证2000，
        涨跌幅一律在 ±0.5% 以内（这个"略偏强"的日子里全绿是合理的）。
    """
    from tests.test_market import FakeMarketClient
    from laoa_trader import market

    # 涨跌幅的取值池：**故意各放几个接近 ±9.9% 的**，这样"涨停 52 家"看起来有出处
    up_values = (0.42, 1.86, 3.24, 6.15, 9.92, 0.78, 2.55, 4.31, 1.12, 7.42, 0.35, 5.08, 2.02)
    down_values = (-0.31, -1.24, -2.87, -5.63, -9.87, -0.72, -1.95, -3.44, -0.55, -7.15)

    # ⚠️ 家数必须与**真实市场量级**对得上：A 股全市场约 5560 只（本项目实测口径），
    # 所以三个数加起来要 ≈5560 —— 第一版只造了 4800 个样本、而且分页尺寸没和引擎对齐
    # （`market.BREADTH_PAGE_SIZE = 1000`，页面却给了 2400 一项），界面上只数到 2400 只，
    # 显示成"上涨 1300 / 下跌 1000"，主人一眼就看出假。
    # 现在的口径：上涨 3120 + 下跌 2020 + 平盘 420 = 5560（略偏强的普通一天），
    # 其中涨停 52 家算在上涨里、跌停 11 家算在下跌里（见下面 totals），不矛盾。
    up_n, down_n, flat_n = 3120, 2020, 420
    # 三个市场的代码段分开造，看着像真的（沪 ≈2300 / 深 ≈3000 / 北交所 ≈260）
    exchange_plan = (("SH", 2300, "600%03d.SH"), ("SZ", 3000, "00%04d.SZ"),
                     ("BJ", 260, "83%04d.BJ"))
    catalogue: list[str] = []
    for _ex, count, pattern in exchange_plan:
        catalogue.extend(pattern % i for i in range(1, count + 1))

    items: list[dict] = []
    for i, thscode in enumerate(catalogue):
        if i < up_n:
            pct = up_values[i % len(up_values)]
        elif i < up_n + down_n:
            pct = down_values[(i - up_n) % len(down_values)]
        else:
            pct = 0.0
        items.append({"thscode": thscode, "turnover": 6e7,
                      "volume": 8e4, "price_change_ratio_pct": pct})

    page_size = market.BREADTH_PAGE_SIZE
    pages = [{"item": items[i:i + page_size], "total": len(items)}
             for i in range(0, len(items), page_size)]

    rows = [
        # ── 宽基（点位是真实量级；涨跌幅 ±0.5% 以内）──
        {"thscode": "000001.SH", "last_price": 3892.44, "price_change_ratio_pct": 0.29,
         "turnover": 7642.6e8},
        {"thscode": "399001.SZ", "last_price": 13441.90, "price_change_ratio_pct": 0.21,
         "turnover": 8437.2e8},                        # ← 两行相加 ≈ 1.61 万亿（页面显示的成交额）
        {"thscode": "399006.SZ", "last_price": 3298.36, "price_change_ratio_pct": 0.38},
        {"thscode": "000688.SH", "last_price": 1531.62, "price_change_ratio_pct": 0.45},
        {"thscode": "000300.SH", "last_price": 4489.20, "price_change_ratio_pct": 0.24},
        {"thscode": "000016.SH", "last_price": 2846.90, "price_change_ratio_pct": 0.09},
        {"thscode": "000852.SH", "last_price": 7562.10, "price_change_ratio_pct": 0.42},
        # ── 情绪 ──
        {"thscode": "883404.TI", "last_price": 885.53, "price_change_ratio_pct": 0.36},
        {"thscode": "883958.TI", "last_price": 6928.17, "price_change_ratio_pct": 0.58},
        {"thscode": "883994.TI", "last_price": 1551.88, "price_change_ratio_pct": 0.44},
        {"thscode": "883418.TI", "last_price": 2131.00, "price_change_ratio_pct": 0.31},
        # ── 板块指数（这个块用的是注入的板块榜，这两行只是让"板块"那一组也有数）──
        {"thscode": "881155.TI", "last_price": 1408.77, "price_change_ratio_pct": -1.36},
        {"thscode": "881157.TI", "last_price": 1122.35, "price_change_ratio_pct": -0.48},
    ]
    return FakeMarketClient(
        totals={market.LIMIT_UP_PATH: 52, market.LIMIT_DOWN_PATH: 11,
                market.LIMIT_BREAK_PATH: 24},
        rows=rows, pages=pages,
        # ⚠️ 分页尺寸必须与引擎一致（`market.BREADTH_PAGE_SIZE`）：靠它把 offset 映射成页号，
        # 对不上就会"只数到第一页"，家数直接少一半。
        page_size=page_size,
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
    payload = {
        symbol: {"symbol": symbol, "price": price, "pct": pct, "at": stamp,
                 "as_of": stamp, "source": "演示数据",
                 "turnover_rate": turnover, "circ_mktcap": mktcap}
        for symbol, (price, pct, turnover, mktcap) in DEMO_QUOTES.items()
    }
    win.quotes.apply(payload)

    # ⚠️ **只 apply 一次是不够的**：界面每 5 秒会再刷一轮（`QuoteService.tick()`），
    # 那一轮会去联网取**真实行情**，把上面的演示数据整批覆盖掉 —— 实测过一次：
    # 演示里"水晶光电 21.86"在图上变成了当天真价 26.19，市值/换手/涨跌幅、
    # 以及两张表算出来的盈亏全跟着变（图既不可重复，也不是我们要演示的盘面）。
    # 所以这里把取数函数本身换成"只回演示数据"，之后的每一次刷新都拿到同一批数。
    def _fake_fetch(_cfg, symbols, *_args, **_kwargs):
        wanted = [str(s) for s in (symbols or [])]
        return {s: dict(payload[s]) for s in wanted if s in payload}

    win.quotes._fetch = _fake_fetch        # type: ignore[assignment]


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



def _dump_table(table, title: str) -> None:
    """把表格的真实单元格值打到 stdout（`LAOA_SHOTS_DEBUG=1` 时启用）。

    为什么要这一层：截图上"数字看着对不对"没法自动化断言，而**肉眼看图**又容易看错
    （离屏渲染的表格缩在小窗里，字很小）。这个开关让"数据对不对"变成可核对的文本输出，
    改完演示数据先跑一遍它，比盯着 PNG 猜快得多。
    """
    headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    print(f"--- {title}（{table.rowCount()} 行）---")
    print(" | ".join(headers))
    for r in range(table.rowCount()):
        cells: list[str] = []
        for c in range(table.columnCount()):
            item = table.item(r, c)
            if item is not None:
                cells.append(item.text().replace("\n", " "))
                continue
            holder = table.cellWidget(r, c)
            label = holder.findChild(QLabel) if holder is not None else None
            cells.append(label.text() if label is not None else "")
        print(" | ".join(cells))


def _grab(widget, name: str) -> Path:
    """存一张图（`grab()` 拿到的是渲染结果，离屏平台下同样有效）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    widget.grab().save(str(path), "PNG")
    return path


def _canvas_size(pet_size: tuple[int, int]) -> tuple[int, int]:
    """桌面画布尺寸：宽 = 左边距 + 窗口 + 间隙 + 桌宠 + 右边距；高取窗口与桌宠里的较大者。"""
    width = CANVAS_MARGIN * 2 + WINDOW_SIZE[0] + PET_GAP + pet_size[0]
    height = CANVAS_MARGIN * 2 + max(WINDOW_SIZE[1], pet_size[1])
    return int(width), int(height)


def _desktop_canvas(win_pix: QPixmap, pet_pix: QPixmap) -> QPixmap:
    """把主窗口与桌宠合成到一张"桌面"画布上（窗口贴左上、桌宠贴右下角）。

    两个必须守住的点：

    ① **桌宠是透明底的**（`DesktopPet` 是 `WA_TranslucentBackground` 的独立顶层窗口，
       素材 `assets/pet.png` 自带透明通道），合成时**直接贴上去**就行 —— 千万别给它
       垫一块白底/圆角卡片，那会变成"桌面上多了一块白方块"（用户 2026-09-20 特意换成
       透明底素材，要摆脱的就是这个效果）。
    ② **桌宠整只都在画布内**（右下角留 `CANVAS_MARGIN` 边距、不与窗口重叠，
       中间还隔 `PET_GAP`）：窗口右边是表格的列和按钮，压上去就看不清了 ——
       所以画布宁可宽一点，也不要"桌宠挤在窗口上"。
    """
    width, height = _canvas_size((pet_pix.width(), pet_pix.height()))
    canvas = QPixmap(width, height)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    # 桌面底色（浅灰渐变）
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, QColor(WALLPAPER_TOP))
    gradient.setColorAt(1.0, QColor(WALLPAPER_BOTTOM))
    painter.fillRect(0, 0, width, height, QBrush(gradient))
    # 窗口投影：几层半透明圆角矩形（比真高斯模糊便宜太多，压缩后也几乎不涨体积）
    painter.setPen(Qt.PenStyle.NoPen)
    for spread, alpha in ((12, 14), (7, 22), (3, 30)):
        painter.setBrush(QColor(15, 23, 42, alpha))
        painter.drawRoundedRect(QRectF(
            CANVAS_MARGIN - spread, CANVAS_MARGIN - spread + 4,
            win_pix.width() + spread * 2, win_pix.height() + spread * 2,
        ), 16, 16)
    painter.drawPixmap(CANVAS_MARGIN, CANVAS_MARGIN, win_pix)

    pet_x = width - CANVAS_MARGIN - pet_pix.width()
    pet_y = height - CANVAS_MARGIN - pet_pix.height()
    # 脚下一点点影子（很淡；透明区域是"上面的像素"，不会因此变成方块）
    shadow_w, shadow_h = PET_SHADOW_SIZE
    painter.setBrush(QColor(15, 23, 42, PET_SHADOW_ALPHA))
    painter.drawEllipse(QRectF(
        pet_x + (pet_pix.width() - shadow_w) / 2, pet_y + pet_pix.height() - 20,
        shadow_w, shadow_h,
    ))
    painter.drawPixmap(pet_x, pet_y, pet_pix)
    painter.end()
    return canvas


def _save_canvas(canvas: QPixmap, name: str) -> Path:
    """存桌面画布。

    存之前转成 **RGB888**（画布本身不透明）：带 alpha 通道的 PNG 体积更大，
    而我们不需要那条通道（透明的是桌宠那张图，已经合成完了）。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    image = canvas.toImage().convertToFormat(QImage.Format.Format_RGB888)
    image.save(str(path), "PNG")
    return path


def _grab_desktop(win, pet, name: str) -> Path:
    """抓「主窗口 + 角落桌宠」的桌面合成图（主界面那几张都用它）。"""
    return _save_canvas(_desktop_canvas(win.grab(), pet.grab()), name)


def main() -> int:
    app = QApplication.instance() or QApplication([])

    _check_demo_heatmap()
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
    # 止损 8% / 止盈 15%：持仓表那两列就是按这两个比例算的（成本 × (1 ∓ 比例)），
    # 编成"止损在成本下方 8%、止盈在上方 15%"，图里才是普通散户的设置
    cfg.stop_loss = 0.08
    cfg.take_profit = 0.15
    days = _seed_demo_db(cfg)
    print(f"演示库就绪：{len(DEMO_STOCKS)} 只票 / {len(days)} 个交易日 → {cfg.db_path}")

    # ⚠️ 顺序要紧：**先**拦住所有联网取数，再建窗口 —— 窗口构造时起的取数线程
    # 会用"当时那个取数函数"，晚一步就会把真实行情写进缓存（见 `_block_network_fetches`）
    _patch_sector_rank()
    _block_network_fetches()
    _pretend_windows_voices()

    from laoa_trader.ui import app as ui_app

    win = ui_app.MainWindow(cfg)
    win.resize(*WINDOW_SIZE)
    win.show()
    app.processEvents()

    _apply_fake_quotes(win)
    for widget in (getattr(win, "voice_hint", None),):
        if widget is not None:
            win._refresh_voice_hint()
    win._refresh_pool_table()
    win._refresh_positions()
    app.processEvents()

    # 桌宠（**先建出来**：主界面那几张要把它合成到右下角）。
    # 气泡有效期给足 —— 默认 8 秒，抓七八张图的时间足够它自己消失，
    # 那样后面几张图里桌宠就没气泡了（同一批图里必须长一个样）。
    from laoa_trader.ui.desktop_pet import DesktopPet

    pet = DesktopPet(None)
    pet.set_unread(3)
    pet.show_bubble(PET_BUBBLE_CORNER, seconds=3600)
    pet.show()
    app.processEvents()

    # ① 大盘概览（注入假客户端，避免联网）
    win.refresh_market_overview(force=True, client=_fake_market_client())
    _wait_market(win, app)
    saved = []
    for index, name in enumerate((
        "01-大盘概览", "02-自选标的", "03-持仓监控", "04-策略匹配-列表", "05-系统设置",
    )):
        win.tabs.setCurrentIndex(index)
        app.processEvents()
        if os.environ.get("LAOA_SHOTS_DEBUG"):
            if index == 1:
                _dump_table(win.pool_table, "自选标的")
            elif index == 2:
                _dump_table(win.position_table, "持仓监控")
        if index == 2:
            # 持仓表抓图**前一刻**才对齐成本价：这张表的「现价」会随刷新在
            # "快照价 / 本地收盘价"之间换来源，早点对齐会被后一次刷新冲掉 ——
            # 结果就是"现价 31.41、成本 18.40、盈亏 +70%"这种不像真账户的画面。
            _align_position_costs(win)
            app.processEvents()
        saved.append(_grab_desktop(win, pet, name))

    # ② 策略编辑器（左边有内容、右边四组按钮面板）
    win.tabs.setCurrentIndex(3)          # 先切回「策略匹配」——不切的话抓到的是上一屏
    app.processEvents()
    page = win.formula_page
    page.btn_edit.click()
    page.name_edit.setText(DEMO_FORMULA_NAME)
    page.note_edit.setText(DEMO_FORMULA_NOTE)
    page.editor.setPlainText(DEMO_FORMULA_TEXT)
    page.on_validate()
    app.processEvents()
    saved.append(_grab_desktop(win, pet, "06-策略匹配-编辑器"))

    # ③ 匹配结果（6 列 + 每行【加入自选】）
    # 先把下面那块编辑器收起来：结果表在上一屏、编辑器的编辑框在下一屏，
    # 两者同时摊开会把结果表挤成半截（营销图里要一眼看清 6 列）
    page.on_close_panel()
    app.processEvents()
    name_of = dict((symbol, name) for symbol, name, _ in DEMO_STOCKS)
    page.show_pick_result({
        "data_date": days[-1],
        "pool": [
            {"symbol": symbol, "name": name_of[symbol], "strategy": "公式·尾盘匹配策略"}
            for symbol in DEMO_RESULT
        ],
    })
    app.processEvents()
    if os.environ.get("LAOA_SHOTS_DEBUG"):
        _dump_table(page.result_table, "匹配结果")
    saved.append(_grab_desktop(win, pet, "07-策略匹配-匹配结果"))

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

    # ⑤ 桌宠特写（就是主界面那几张右下角那只，换成止损提醒那条一起拍）
    # 气泡有长度上限（`desktop_pet.BUBBLE_MAX_CHARS = 34`），超了会被截成"…" ——
    # 演示图里用一句正好放得下的
    pet.show_bubble(PET_BUBBLE_CLOSEUP, seconds=3600)
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
