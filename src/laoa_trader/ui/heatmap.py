"""板块热力图控件：**squarified treemap 自绘**（方块面积 = 流通市值，颜色 = 红涨绿跌）。

为什么要自绘而不是引图表库
--------------------------
1. 商用闭源产品不能随便用 QtCharts（GPL/商业双授权），而 PySide6 本体是 LGPL，自绘最干净；
2. 热力图要用的东西很少：矩形 + 颜色 + 文字 + hover —— 一个 `paintEvent` 就够；
3. **零新依赖**意味着 Nuitka 打包不变（现包 83MB 里没有 Chromium、没有额外图表库）。

布局算法单独一个纯函数（`layout_blocks`）：它只吃"权重 + 画布"，吐"一堆矩形"，
不碰 Qt、不碰数据源 —— 于是能在用例里直接断言"面积守恒、不重叠、极端权重不失衡"，
而不是靠肉眼看截图。这是这一块最容易写错、也最值得测的部分。

口径（主人 2026-10-08 拍板）见 `data/market_map.py` 的模块说明：面积=市值、颜色=红涨绿跌、
没有数据的块画灰标 `—`（**不当 0**）。
"""

from __future__ import annotations

from typing import Any, Sequence

from laoa_trader.data import market_map

#: 块与块之间的缝（像素）：太窄看着像一整块，太宽又浪费面积
GAP = 2
#: 小于这个尺寸的块就不写字了（写了也是一坨糊）
MIN_TEXT_W = 34
MIN_TEXT_H = 18


def layout_blocks(
    weights: Sequence[float], width: int, height: int,
) -> list[tuple[int, int, int, int]]:
    """`squarified treemap`：权重 → 一堆矩形（`(x, y, w, h)`）。

    为什么用 squarify 而不是"按行铺开"：等分格子会让长宽比失控（一条又长又窄的条子
    既画不下字、也看不出面积差），squarify 的目标就是让每块的**长宽比尽量接近 1**。

    算法（Bruls 等 2000 的经典写法，这里按"逐行填充"实现）：
    把权重从大到小排序，一行一行地填；每一行先按"当前剩余矩形的短边"算出候选宽度，
    只要新加入的块让**最差长宽比**变好就继续塞进这一行，变差就收行、换下一行。

    Args:
        weights: 每块的权重（必须 > 0；0 或负数会被当成 0 → 不给面积）。
        width/height: 画布尺寸（像素）。

    Returns:
        与 `weights` **一一对应**的矩形列表（顺序不变）。权重非正的块拿到 0 面积矩形
        `(0,0,0,0)`；**权重太小、摊到不足 1 像素的块也拿到 0 面积**（画不出来就是画不出来，
        给它 1 像素会挤掉邻居、让面积比例失真）。留白来自块与块之间的 `GAP`，
        实测利用率 92%~98%。
    """
    n = len(weights)
    if n == 0 or width <= 0 or height <= 0:
        return [(0, 0, 0, 0)] * n

    total = sum(float(w) for w in weights if float(w) > 0)
    rects: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * n
    if total <= 0:
        return rects

    # 画布按总面积缩放到"面积 = 权重"，这样内部全用整数像素也不会累计误差
    scale = (width * height) / total
    order = sorted(
        (i for i in range(n) if float(weights[i]) > 0),
        key=lambda i: -float(weights[i]),
    )
    areas = [float(weights[i]) * scale for i in order]

    x, y, w, h = 0, 0, int(width), int(height)
    index = 0
    while index < len(order) and w > 1 and h > 1:
        side = min(w, h)
        # 贪心攒一行：只要"最差长宽比"还在变好，就继续加
        row: list[float] = []
        best = None
        cursor = index
        while cursor < len(order):
            candidate = row + [areas[cursor]]
            worst = _worst_ratio(candidate, side)
            if best is not None and worst > best:
                break
            row = candidate
            best = worst
            cursor += 1
        row_sum = sum(row)
        # 厚度 = 这一行/列占多少像素，**垂直于铺开方向**：
        #   画布偏宽（w >= h）→ 从左边切一列（列宽 = 厚度），块从上下方向堆；
        #   画布偏高（h > w） → 从上边切一行（行高 = 厚度），块从左右方向铺。
        # 上下界：至少 1 像素（否则看不见）、不超过这一方向剩余的空间（`room`）——
        # 少了这个上界，权重极不均（比如"银行 9 万亿"对着一堆几百亿的小行业）时
        # 第一块的厚度会大于画布高度，整块图直接溢出到控件外面，看上去就是"图画坏了"。
        room = w if w >= h else h
        thickness = int(round(row_sum / side)) if side else 0
        thickness = max(1, min(thickness, room))
        offset = 0
        for area in row:
            length = int(round(area / thickness)) if thickness else 0
            length = max(1, min(length, side))
            if w >= h:      # 左边这一列：宽 = thickness，高 = length
                rects[order[index]] = (x, y + offset, max(0, thickness - GAP),
                                       max(0, length - GAP))
            else:           # 上边这一行：宽 = length，高 = thickness
                rects[order[index]] = (x + offset, y, max(0, length - GAP),
                                       max(0, thickness - GAP))
            offset += length
            index += 1
        if w >= h:
            x += thickness
            w -= thickness
        else:
            y += thickness
            h -= thickness
    return rects


def _worst_ratio(row: Sequence[float], side: int) -> float:
    """这一行里最差的块长宽比（越小越好）；`side` 是当前剩余矩形的短边。"""
    if not row or side <= 0:
        return float("inf")
    row_sum = sum(row)
    thickness = row_sum / side
    if thickness <= 0:
        return float("inf")
    worst = 0.0
    for area in row:
        length = area / thickness
        if length <= 0:
            return float("inf")
        ratio = max(length / thickness, thickness / length)
        worst = max(worst, ratio)
    return worst


def build_heatmap_widget() -> Any:
    """建一个 `HeatmapWidget`（Qt 是延迟导入的：这个模块的纯函数部分要能被无 Qt 环境引用）。"""
    from PySide6.QtCore import QRect, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
    from PySide6.QtWidgets import QWidget

    class HeatmapWidget(QWidget):  # noqa: D101 - 见模块说明
        #: 双击某一块（放大窗口用它做"下钻"，概览页里没用）
        blockActivated = Signal(str)

        def __init__(self, parent: Any = None, *, compact: bool = False) -> None:
            super().__init__(parent)
            self.compact = compact          # 概览页里那个小的：字号更小、字更少
            self.blocks: list[dict] = []
            self.extras: dict[str, dict] = {}
            self._rects: list[tuple[int, int, int, int]] = []
            self.setMouseTracking(True)
            self.setMinimumHeight(120 if compact else 360)
            self.setToolTip("板块热力图：面积 = 行业流通市值合计，颜色 = 涨跌幅（红涨绿跌）；"
                            "鼠标停住看细节，双击放大")

        # ── 数据 ──

        def set_blocks(self, blocks: Sequence[dict],
                       extras: dict[str, dict] | None = None) -> None:
            self.blocks = [dict(b) for b in (blocks or [])]
            self.extras = dict(extras or {})
            self._relayout()
            self.update()

        def _relayout(self) -> None:
            weights = [float(b.get("mktcap") or 0.0) for b in self.blocks]
            self._rects = layout_blocks(weights, max(self.width(), 1), max(self.height(), 1))

        # ── 绘制 ──

        def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            super().resizeEvent(event)
            self._relayout()

        def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt 命名
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            if not self.blocks:
                painter.setPen(QPen(QColor(150, 150, 150)))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                                 "还没有数据 —— 点【刷新热力图】取一轮全市场快照")
                return
            font = QFont(self.font())
            font.setPointSize(max(7, font.pointSize() - (2 if self.compact else 0)))
            painter.setFont(font)
            metrics = QFontMetrics(font)
            for index, block in enumerate(self.blocks):
                if index >= len(self._rects):
                    break
                x, y, w, h = self._rects[index]
                if w <= 0 or h <= 0:
                    continue
                rect = QRect(x, y, w, h)
                pct = block.get("pct")
                rgb = market_map.block_color(pct)
                painter.fillRect(rect, QColor(*rgb))
                painter.setPen(QPen(QColor(255, 255, 255, 90)))
                painter.drawRect(rect)
                if w < MIN_TEXT_W or h < MIN_TEXT_H:
                    continue
                text_rgb = market_map.text_color(pct)
                painter.setPen(QPen(QColor(*text_rgb)))
                # 名字按**这块自己的宽度**截断（`elidedText`），而不是按固定字数 ——
                # 行业名 3~6 个字都有（"银行" / "汽车零部件"），固定字数要么浪费宽度、
                # 要么压到隔壁块上。宽度不够就"汽车零…"，一眼知道是哪类。
                room = max(rect.width() - 8, 1)
                name = metrics.elidedText(str(block.get("name") or ""),
                                          Qt.TextElideMode.ElideRight, room)
                painter.drawText(rect.adjusted(4, 2, -4, -2),
                                 Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, name)
                if h >= MIN_TEXT_H * 2:
                    pct_text = "—" if pct is None else f"{float(pct):+.2f}%"
                    painter.drawText(rect.adjusted(4, 2, -4, -4),
                                     Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                                     metrics.elidedText(pct_text, Qt.TextElideMode.ElideRight,
                                                        room))

        # ── 交互 ──

        def _block_at(self, pos: Any) -> dict | None:
            for index, (x, y, w, h) in enumerate(self._rects):
                if index >= len(self.blocks):
                    break
                if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                    return self.blocks[index]
            return None

        def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            block = self._block_at(event.position().toPoint())
            self.setToolTip(self.tip_for(block) if block else
                            "板块热力图：面积 = 行业流通市值合计，颜色 = 涨跌幅（红涨绿跌）")
            super().mouseMoveEvent(event)

        def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            block = self._block_at(event.position().toPoint())
            if block:
                self.blockActivated.emit(str(block.get("name") or ""))
            super().mouseDoubleClickEvent(event)

        def tip_for(self, block: dict) -> str:
            """一块的 tooltip：**每个数都说清口径**（用户要能自己核对，而不是相信一个颜色）。"""
            pct = block.get("pct")
            extra = self.extras.get(str(block.get("name") or ""), {})
            lines = [f"{block.get('name')}"]
            lines.append(f"涨跌幅（市值加权）：{'—' if pct is None else f'{float(pct):+.2f}%'}")
            lines.append(f"流通市值合计：{float(block.get('mktcap') or 0):.0f} 亿")
            count, priced = int(block.get("count") or 0), int(block.get("priced") or 0)
            lines.append(f"成分股：{count} 只"
                         + ("" if priced == count else f"（{count - priced} 只没有涨跌幅，未计入加权）"))
            if block.get("leader"):
                leader_pct = block.get("leader_pct")
                lines.append(f"最大的一只：{block['leader']}"
                             + ("" if leader_pct is None else f"（{float(leader_pct):+.2f}%）"))
            if extra.get("limit_up") is not None:
                lines.append(f"涨停家数：{int(extra['limit_up'])}")
            if extra.get("limit_down"):
                # 只在这个行业**真有跌停**时才写这一行：一片`跌停家数：0` 会把有用的那几行淹掉
                lines.append(f"跌停家数：{int(extra['limit_down'])}")
            if extra.get("main_net") is not None:
                # `sectors.fetch_sector_rank` 的 `main_net` 是**元**（腾讯原文万元已 ×1e4，
                # 见 `ui/app.py` 的 `sector_rank_tables`），这里必须 ÷1e8 才是"亿"
                lines.append(f"主力净额：{float(extra['main_net']) / 1e8:+.2f} 亿")
            lines.append("（面积 = 流通市值合计；颜色 = 涨跌幅，红涨绿跌）")
            return "\n".join(lines)

    return HeatmapWidget
