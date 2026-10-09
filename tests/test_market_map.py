"""板块热力图的数据层 + 布局算法（**纯函数**，不吃 Qt、不联网）。

为什么单独一份
--------------
2026-10-08 主人把「热门板块」从"上涨前五 / 下跌前五两张表"改成**板块热力图**，
新增了 `data/market_map.py`（快照 → 行业方块）与 `ui/heatmap.py` 的 `layout_blocks()`
（权重 → 矩形）。这两处是这次改动里**最容易算错**的地方：

- 面积给谁？——只有"拿得到流通市值"的票才进面积与加权涨跌幅，缺值的票**不能当成 0**
  （缺值就当 0，等于替它编了个权重，也让停牌股看起来像横盘）；
- 颜色怎么分档？——红涨绿跌、±10% 饱和，而 `None`（没数据）必须是**另一个颜色**；
- 布局会不会重叠/越界/在极端权重比下崩掉？——这类错误只能靠断言，肉眼看截图看不出来。

界面侧（方块画在哪、按钮接没接上、那行小字说了什么）在 `tests/test_ui_smoke.py`。
"""

from __future__ import annotations

import time
import types
from pathlib import Path

from laoa_trader.data import market_map, storage
from laoa_trader.ui.heatmap import GAP, layout_blocks


# ── 聚合：`build_blocks()` ──


def test_build_blocks_weights_the_change_by_circulating_market_cap() -> None:
    """`pct` = **市值加权**涨跌幅（不是等权平均）。

    300 亿的票 +10%、100 亿的票 -10% → 加权 = (300×10 + 100×(-10)) / 400 = **+5%**，
    等权平均会是 0% —— 这两个数在热力图上差一整档颜色，必须钉住是哪一个。
    """
    quotes = {
        "600001": {"pct": 10.0, "circ_mktcap": 300.0},
        "600002": {"pct": -10.0, "circ_mktcap": 100.0},
    }
    blocks = market_map.build_blocks(quotes, {"600001": "银行", "600002": "银行"})

    assert len(blocks) == 1
    block = blocks[0]
    assert block["name"] == "银行"
    assert block["pct"] == 5.0                  # 加权，不是 0.0（等权）
    assert block["mktcap"] == 400.0             # 面积 = 行业流通市值合计（亿）
    assert block["count"] == 2 and block["priced"] == 2


def test_build_blocks_ignores_stocks_without_a_market_cap() -> None:
    """没有市值（或市值 ≤0）的票：**既不进面积、也不进加权涨跌幅**。

    为什么不能"当成 0 顶上去"：那等于替这只票编了个权重，还会把加权涨幅往 0 拉 ——
    用户看到的是一个不存在的数。`count` 与 `priced` 分开记，就是为了让 tooltip
    能如实说"有 N 只没涨跌幅、未计入加权"（见 `ui/heatmap.py` 的 `tip_for`）。
    """
    quotes = {
        "600001": {"pct": 10.0, "circ_mktcap": 300.0},
        "600002": {"pct": -10.0, "circ_mktcap": 100.0},
        "600003": {"pct": 99.0},                       # 只有涨跌幅，没有市值
        "600004": {"pct": 99.0, "circ_mktcap": 0.0},   # 市值 0 也不算
        "600005": {"pct": 99.0, "circ_mktcap": -1.0},  # 市值是负的（脏数据）同样不算
    }
    industry = {code: "银行" for code in quotes}
    block = market_map.build_blocks(quotes, industry)[0]

    assert block["mktcap"] == 400.0             # 只有那两只真有市值的进了面积
    assert block["pct"] == 5.0                  # 三只 +99% 一点都没掺进来（拿它们当权重就是编数）
    assert block["count"] == 5                  # 成分股还是 5 只（市值缺失不影响"有几只票"）
    assert block["priced"] == 2                 # 参与加权的只有 2 只
    assert block["count"] != block["priced"]    # 界面据此在 tooltip 里说明差在哪


def test_build_blocks_counts_a_halted_stock_in_the_area_but_not_in_the_change() -> None:
    """停牌（有市值没涨跌幅）的票：**进面积，但不进加权涨跌幅的分母**。

    口径：`pct = 加权和 / 「有涨跌幅那些票」的市值合计` —— 300 亿 +10% 与 100 亿停牌
    算出来是 **+10%**（不是 7.5%）。把停牌票算进分母等于"按 0% 计进去"，
    那是替一只当天没交易的票编了一个"没动"的结论，与"缺值不当 0"相悖；
    它的市值照样进面积（面积回答"这块有多大"，与"今天涨没涨"是两件事），
    tooltip 里也写明了"1 只没有涨跌幅，未计入加权"。
    """
    quotes = {
        "600001": {"pct": 10.0, "circ_mktcap": 300.0},
        "600002": {"circ_mktcap": 100.0},               # 停牌：有市值、没涨跌幅
    }
    block = market_map.build_blocks(quotes, {"600001": "银行", "600002": "银行"})[0]

    assert block["mktcap"] == 400.0
    assert block["count"] == 2 and block["priced"] == 1
    assert block["pct"] == 10.0

    # 反例：只有停牌票的行业算不出涨跌幅 → None（画灰），不是 0（画横盘）
    halted = market_map.build_blocks(
        {"600002": {"circ_mktcap": 100.0}}, {"600002": "银行"})[0]
    assert halted["pct"] is None and halted["mktcap"] == 100.0


def test_build_blocks_puts_unknown_industry_into_its_own_block() -> None:
    """行业归属为空 → 归到「未分类」这一块（**不静默丢掉**）。

    丢掉的话少的是几十只票的市值，用户看到的面积分布就是错的，而且他无从知道 ——
    项目里"缺值不产生信号，但缺值必须被看见"是同一条纪律。
    """
    quotes = {
        "600001": {"pct": 1.0, "circ_mktcap": 10.0},
        "999999": {"pct": 2.0, "circ_mktcap": 20.0},     # 本地行业表里根本没有它
        "600002": {"pct": 3.0, "circ_mktcap": 30.0},     # 行业是空串（`industry_map` 会滤掉）
    }
    blocks = market_map.build_blocks(quotes, {"600001": "银行", "600002": ""})
    by_name = {b["name"]: b for b in blocks}

    assert market_map.UNKNOWN_INDUSTRY == "未分类"
    assert set(by_name) == {"银行", "未分类"}
    assert by_name["未分类"]["symbols"] == ["999999", "600002"]
    assert by_name["未分类"]["mktcap"] == 50.0
    assert sum(b["mktcap"] for b in blocks) == 60.0     # 一只都没丢


def test_build_blocks_keeps_none_pct_instead_of_zero() -> None:
    """一只票都算不出加权涨跌幅时 `pct` 是 **None**，不是 `0`。

    0% 在热力图上画的是近白（"真的没动"），None 画的是灰（"不知道"）——
    把停牌当成横盘就是往页面上写假信息。`block_color(None)` 与 `block_color(0.0)`
    必须能分开，这条是那一处的前提。
    """
    quotes = {
        "600001": {"circ_mktcap": 400.0},                # 有市值、没涨跌幅（停牌）
        "600002": {"pct": None, "circ_mktcap": 600.0},   # 字段在、值是 None
    }
    block = market_map.build_blocks(quotes, {"600001": "白酒", "600002": "白酒"})[0]

    assert block["pct"] is None
    assert block["pct"] != 0                             # 别被 `or 0` 之类的写法糊过去
    assert block["mktcap"] == 1000.0
    assert block["priced"] == 0

    # 一只都没有 → 连块都造不出来时不是 None 而是空列表（界面画"还没有数据"）
    assert market_map.build_blocks({}, {}) == []
    assert market_map.build_blocks({"600001": "不是字典"}, {}) == []


def test_build_blocks_sorts_by_market_cap_and_names_the_leader() -> None:
    """块按**市值降序**（布局算法也要大的在前），并给出"行业内最大的那只"供热力图 tooltip。

    顺序不是装饰：`layout_blocks()` 依赖"大的在前"才铺得整齐，界面也直接按这个顺序断言。
    """
    quotes = {
        "600003": {"pct": 1.0, "circ_mktcap": 50.0},      # 半导体里最小的
        "600002": {"pct": 6.0, "circ_mktcap": 500.0},     # 半导体里最大的
        "600001": {"pct": -1.0, "circ_mktcap": 400.0},    # 银行
        "300001": {"pct": 0.0, "circ_mktcap": 200.0},     # 白酒
    }
    industry = {"600002": "半导体", "600003": "半导体", "600001": "银行", "300001": "白酒"}
    names = {"600002": "半导体甲", "600003": "半导体丙", "600001": "浦发样本"}
    blocks = market_map.build_blocks(quotes, industry, names=names)

    assert [b["name"] for b in blocks] == ["半导体", "银行", "白酒"]
    assert [b["mktcap"] for b in blocks] == [550.0, 400.0, 200.0]
    semi = blocks[0]
    assert semi["leader"] == "半导体甲"                  # 市值最大的那只（用的是名称）
    assert semi["leader_pct"] == 6.0
    assert semi["symbols"] == ["600003", "600002"]       # 快照的键顺序，界面不依赖它
    # 没给名称时退回代码（tooltip 里宁可显示代码，也不要一个空白）
    assert market_map.build_blocks(
        {"600001": {"pct": 1.0, "circ_mktcap": 10.0}}, {"600001": "银行"}
    )[0]["leader"] == "600001"


# ── 颜色 ──


def test_block_color_is_red_up_green_down_and_grey_when_missing() -> None:
    """红涨绿跌、±10% 饱和；`None` 是灰，且**与 0% 的近白明显不同**。

    饱和（而不是线性拉到更大范围）：A 股一天 ±10% 已是涨跌停，绝大多数票在 ±5% 以内，
    线性映射会让"今天普涨"和"今天普跌"看起来一样淡。
    """
    deep_red, deep_green = market_map.block_color(10.0), market_map.block_color(-10.0)
    assert deep_red == (214, 48, 40)            # 涨：R 明显高于 G/B
    assert deep_green == (26, 145, 74)          # 跌：G 最高
    assert deep_red[0] > deep_red[1] and deep_red[0] > deep_red[2]
    assert deep_green[1] > deep_green[0] and deep_green[1] > deep_green[2]

    flat = market_map.block_color(0.0)
    assert flat == (238, 238, 238)              # 0% 近白：真的没动
    assert min(flat) >= 230

    grey = market_map.block_color(None)         # 没数据：灰
    assert grey == (158, 158, 158)
    assert grey != flat                         # "不知道" 与 "没动" 必须能一眼分开

    # 超出 ±10% 一律用最深那档（饱和），不会更红/更绿
    assert market_map.block_color(10.01) == deep_red
    assert market_map.block_color(555.0) == deep_red
    assert market_map.block_color(-555.0) == deep_green
    # 方向单调：涨得越多越红、跌得越多越绿。
    # ⚠️ 不能用"R 通道绝对值"来判断"更红" —— 近白底色 (238,238,238) 的红通道是 238，
    # 深红 (214,48,40) 的反而更低；"有多红"看的是 R 与 G 的**差**（绿同理看 G-R）。
    def redness(pct: float) -> int:
        r, g, _ = market_map.block_color(pct)
        return r - g

    def greenness(pct: float) -> int:
        r, g, _ = market_map.block_color(pct)
        return g - r

    assert redness(2.0) < redness(8.0) < redness(10.0)
    assert greenness(-2.0) < greenness(-8.0) < greenness(-10.0)
    assert redness(2.0) > 0 and greenness(-2.0) > 0       # 已经能看出方向，不是灰的


def test_text_color_switches_to_white_on_a_dark_background() -> None:
    """块内文字颜色跟着**底色深浅**走：深色底白字、浅色底深字、没数据用中灰。"""
    assert market_map.text_color(10.0) == (255, 255, 255)      # 深红底 → 白字
    assert market_map.text_color(-10.0) == (255, 255, 255)     # 深绿底 → 白字
    assert market_map.text_color(0.0) == (32, 32, 32)          # 近白底 → 深字
    assert market_map.text_color(1.0) == (32, 32, 32)          # 淡色底仍是深字
    assert market_map.text_color(None) == (60, 60, 60)         # 灰底 → 中灰字


# ── 汇总 / 缺值过滤 / 与板块榜合流 ──


def test_summarize_counts_each_kind_of_block() -> None:
    """那行小字用的汇总：块数、合计市值、涨/跌/平/无数据各几块。"""
    blocks = [
        {"name": "a", "mktcap": 100.0, "pct": 2.0},
        {"name": "b", "mktcap": 50.0, "pct": -3.0},
        {"name": "c", "mktcap": 25.0, "pct": 0.0},
        {"name": "d", "mktcap": 5.0, "pct": None},
    ]
    stat = market_map.summarize(blocks)
    assert (stat["blocks"], stat["mktcap"]) == (4, 180.0)
    assert (stat["up"], stat["down"], stat["flat"], stat["unknown"]) == (1, 1, 1, 1)
    assert market_map.summarize([]) == {
        "blocks": 0, "mktcap": 0.0, "up": 0, "down": 0, "flat": 0, "unknown": 0,
    }


def test_drawable_quotes_drops_rows_without_any_number() -> None:
    """两样都没有（涨跌幅与市值全空）的行直接滤掉：留着只会往「未分类」里塞零权重块。"""
    quotes = {
        "600001": {"pct": 1.0},
        "600002": {"circ_mktcap": 10.0},
        "600003": {},                                    # 两个字段都空 → 滤掉
        "600004": {"pct": None, "circ_mktcap": None},    # 显式 None → 也滤掉
        "600005": "不是字典",                             # 脏数据 → 滤掉
    }
    assert set(market_map.drawable_quotes(quotes)) == {"600001", "600002"}
    assert market_map.drawable_quotes({}) == {}
    assert market_map.drawable_quotes(None) == {}         # type: ignore[arg-type]


def test_industry_extras_maps_sector_rows_and_keeps_missing_values_as_none() -> None:
    """板块榜的三列 → `{行业名: {limit_up, limit_down, main_net}}`；取不到就是 `None`（不是 0）。

    热力图的面积与颜色走全市场快照，但"涨停家数 / 跌停家数 / 主力净额"只有板块榜有 ——
    两个来源在 tooltip 里合流（多一个请求都不发）。单位**在这一层不换算**：
    两个家数是只数，`main_net` 是元（tooltip 显示前自己 ÷1e8）。
    """
    rows = [
        {"name": "半导体", "limit_up": 3, "limit_down": 2, "main_net": 12.5e8},
        {"name": "银行", "limit_up": None, "limit_down": None, "main_net": None},
        {"name": "  "},                                   # 没名字的行直接跳过
        {"name": "医药", "limit_up": 0},                  # 0 是"真的没有涨停"，要留下
    ]
    extras = market_map.industry_extras(rows)

    assert set(extras) == {"半导体", "银行", "医药"}
    assert extras["半导体"] == {"limit_up": 3, "limit_down": 2, "main_net": 12.5e8}
    assert extras["银行"]["limit_up"] is None             # 取不到 → None（tooltip 不写这一行）
    assert extras["医药"] == {"limit_up": 0, "limit_down": None, "main_net": None}
    assert market_map.industry_extras([]) == {}
    assert market_map.industry_extras(None) == {}         # type: ignore[arg-type]


def test_quotes_source_text_says_which_source_the_numbers_came_from() -> None:
    """快照的**出处**要能写成一句人话（价格一个来源、市值另一个来源补是常态）。

    图上写着"红涨绿跌"却不写出处，用户没法判断这块到底是哪个口径的数 ——
    所以那行小字里永远带着"来源 …"。取不到出处时返回空串（界面就不写这一句）。
    """
    quotes = {
        "600001": {"pct": 1.0, "circ_mktcap": 10.0, "source": "hithink",
                   "field_source": {"circ_mktcap": "public"}},
        "600002": {"pct": 2.0, "circ_mktcap": 20.0, "source": "hithink"},
        "600003": {"pct": 3.0, "circ_mktcap": 30.0, "source": "public"},
    }
    text = market_map.quotes_source_text(quotes)

    assert "同花顺" in text                    # 多数派（价格来源）当主语
    assert "市值由" in text and "公开行情源" in text   # 市值是另一个源补的，照样写出来
    # 只有一个来源时不必啰嗦"补"字
    assert "补" not in market_map.quotes_source_text(
        {"600001": {"pct": 1.0, "circ_mktcap": 10.0, "source": "public"}})
    # 快照里没有出处（老数据 / 来源没报）→ 空串，界面那一句就不出现
    assert market_map.quotes_source_text({"600001": {"pct": 1.0}}) == ""
    assert market_map.quotes_source_text({}) == ""
    assert market_map.quotes_source_text(None) == ""      # type: ignore[arg-type]


# ── 本地行业表 / 磁盘缓存 ──


def test_industry_map_reads_the_local_table(cfg) -> None:
    """行业归属读**本地表**（不联网、不多问一次接口），读不到就返回空（不抛）。"""
    storage.init_db(cfg.db_path)
    with storage.connect(cfg.db_path) as conn:
        storage.write_stock_basic(conn, [
            ("600001", "浦发样本", "银行"),
            ("600002", "半导体甲", "半导体"),
            ("600003", "空白行业", ""),                   # 空行业不进表（按"未分类"处理）
        ])
    assert market_map.industry_map(cfg.db_path) == {"600001": "银行", "600002": "半导体"}

    # 库坏了/表不存在：返回空字典 —— 热力图会只剩一个「未分类」块，但界面不能崩
    broken = Path(cfg.data_dir) / "broken.db"
    broken.write_text("这不是一个 sqlite 库", encoding="utf-8")
    assert market_map.industry_map(broken) == {}
    assert market_map.industry_map(cfg.data_dir / "根本不存在.db") == {}


def test_cache_roundtrip_and_age_text(cfg) -> None:
    """缓存落盘再读回来是同一份数据 + 同一个快照时间；坏了/没有就是 `([], 0)`。

    为什么要缓存：全市场快照要翻 28 个请求，开程序第一屏不可能干等它 ——
    先画上次的快照，并如实显示"这是几点钟的"（`age_text`）。
    """
    blocks = [{"name": "半导体", "mktcap": 800.0, "pct": 4.5, "count": 2, "priced": 2,
               "symbols": ["600002"], "leader": "半导体甲", "leader_pct": 6.0}]
    at = 1_790_000_000.0
    path = market_map.save_cache(cfg, blocks, at=at)

    assert path is not None and path.exists()
    assert market_map.cache_path(cfg) == Path(cfg.data_dir) / "cache" / market_map.CACHE_NAME
    loaded, loaded_at = market_map.load_cache(cfg)
    assert loaded == blocks and loaded_at == at

    # 没有缓存 / 缓存写坏了：安静地当作"还没有数据"
    empty = types.SimpleNamespace(data_dir=Path(cfg.data_dir) / "空的")
    assert market_map.load_cache(empty) == ([], 0.0)
    market_map.cache_path(cfg).write_text("{坏掉的 json", encoding="utf-8")
    assert market_map.load_cache(cfg) == ([], 0.0)

    # 快照时间 → 人话（界面、tooltip、放大窗口三处共用这一份实现）
    stamp, stale = market_map.age_text(
        1_790_000_000.0, now=1_790_000_000.0 + market_map.STALE_AFTER - 1
    )
    assert stamp == time.strftime("%m-%d %H:%M", time.localtime(1_790_000_000.0))
    assert stale is False                                # 还没过期
    assert market_map.age_text(1_790_000_000.0,
                               now=1_790_000_000.0 + market_map.STALE_AFTER + 1)[1] is True
    assert market_map.age_text(0.0) == ("还没有数据", True)


# ── 布局算法：`layout_blocks()`（纯函数，不吃 Qt）──


def _canvas_area(width: int, height: int) -> float:
    return float(width) * float(height)


def _area(rect: tuple[int, int, int, int]) -> float:
    return float(rect[2]) * float(rect[3])


def test_layout_blocks_keeps_area_proportional_to_weight() -> None:
    """**面积守恒**：各块面积之和 ≈ 画布面积（只少掉 GAP 那些缝），且面积比 ≈ 权重比。

    为什么这两条都要：只验"加起来差不多"会漏掉"面积顺序和权重顺序对不上"这种错 ——
    那正是热力图最不能错的一条（用户是靠**面积**读权重的）。
    """
    weights = [800.0, 400.0, 200.0, 80.0, 30.0]
    width, height = 900, 480
    rects = layout_blocks(weights, width, height)

    assert len(rects) == len(weights)                 # 与入参一一对应（顺序也不变）
    total = sum(_area(r) for r in rects)
    # 缝隙最多吃掉百分之几：下界的 0.95 是"缝很窄"这条设计意图的断言
    assert 0.95 * _canvas_area(width, height) <= total <= _canvas_area(width, height)

    weight_sum = sum(weights)
    for weight, rect in zip(weights, rects):
        expected = _area(rect) / _canvas_area(width, height)
        assert abs(expected - weight / weight_sum) <= 0.03, (weight, rect)
    # 面积顺序 = 权重顺序（大的块一定更大，不会"看着小其实重"）
    areas = [_area(r) for r in rects]
    assert areas == sorted(areas, reverse=True)


def test_layout_blocks_never_overlaps_and_stays_inside_the_canvas() -> None:
    """块与块**互不重叠**、每一块都在画布里面（越界会被裁掉一半，看着像少了个行业）。"""
    weights = [500.0, 400.0, 200.0, 80.0, 30.0, 10.0, 5.0]
    width, height = 640, 480
    rects = layout_blocks(weights, width, height)

    for x, y, w, h in rects:
        assert x >= 0 and y >= 0
        assert x + w <= width and y + h <= height
        assert w >= 0 and h >= 0

    boxes = [r for r in rects if _area(r) > 0]
    assert len(boxes) == len(weights)                 # 这组权重每块都该画得出来
    for i, (ax, ay, aw, ah) in enumerate(boxes):
        for bx, by, bw, bh in boxes[i + 1:]:
            assert ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay


def test_layout_blocks_gives_zero_area_only_to_non_positive_weights() -> None:
    """权重 ≤ 0 的块拿到 `(0,0,0,0)`（**不占面积也不占位置**），正权重的块照常画。"""
    rects = layout_blocks([0.0, -5.0, 3.0, 1.0], 400, 300)

    assert rects[0] == (0, 0, 0, 0)                   # 权重 0
    assert rects[1] == (0, 0, 0, 0)                   # 权重负数
    assert _area(rects[2]) > 0 and _area(rects[3]) > 0
    assert _area(rects[2]) > _area(rects[3])          # 3:1 的关系照样成立
    # 全非正 → 全是零面积（一像素都不画，也不除零）
    assert layout_blocks([0.0, 0.0], 400, 300) == [(0, 0, 0, 0)] * 2
    # 空列表 / 画布尺寸为 0：返回与入参等长的零矩形，**不抛也不死循环**
    assert layout_blocks([], 400, 300) == []
    assert layout_blocks([1.0, 2.0], 0, 0) == [(0, 0, 0, 0)] * 2
    assert layout_blocks([1.0, 2.0], -10, 50) == [(0, 0, 0, 0)] * 2


def test_layout_blocks_survives_extreme_weight_ratios() -> None:
    """1:1000 这种极端权重比**不崩**，也不把画布铺坏（小到画不出来的那块给零面积）。"""
    width, height = 800, 400
    rects = layout_blocks([1.0, 1000.0], width, height)

    assert len(rects) == 2
    small, big = rects[0], rects[1]
    assert _area(big) > 0
    # 千分之一的权重在 800×400 上不足 1 像素高 → 零面积（宁可不画，也不画一条假的长条）
    assert _area(small) == 0
    for x, y, w, h in rects:
        assert 0 <= x and 0 <= y and x + w <= width and y + h <= height

    # 反过来也一样（大权重的块在第一个）
    big_first = layout_blocks([1000.0, 1.0], width, height)
    assert _area(big_first[0]) > 0 and _area(big_first[1]) == 0

    # 单一权重：铺满整块（只差 GAP）
    only = layout_blocks([7.0], 400, 300)[0]
    assert _area(only) >= 0.95 * _canvas_area(400, 300)
    assert only[2] == 400 - GAP and only[3] == 300 - GAP


# ── 深色皮肤下的热力图配色（2026-10-08 起默认皮肤就是深色的）──
#
# 浅色主题的横盘块是**近白**；铺在深蓝底上，最平静的块反而变成全屏最亮的东西。
# 深色主题因此换一套**同构**配色（横盘=深灰蓝、涨跌两端=亮红亮绿），
# 判据完全一样、只是锚点不同 —— 下面把两件事钉住：两端色是真的换了、语义没变。


def test_dark_palette_keeps_the_semantics_but_moves_the_anchors() -> None:
    assert market_map.block_color(10.0, dark=True) == market_map.DARK_UP
    assert market_map.block_color(-10.0, dark=True) == market_map.DARK_DOWN
    assert market_map.block_color(0.0, dark=True) == market_map.DARK_BASE
    assert market_map.block_color(None, dark=True) == market_map.DARK_UNKNOWN
    # 涨的那一端仍然"更红"（R > G）、跌的那一端仍然"更绿"（G > R）
    up, down = market_map.block_color(6.0, dark=True), market_map.block_color(-6.0, dark=True)
    assert up[0] > up[1] and down[1] > down[0]
    # 底色是深的（这就是"深色主题"的意思），而两端比底色亮得多（否则看不见）
    assert sum(market_map.DARK_BASE) < 200
    assert sum(market_map.DARK_UP) > sum(market_map.DARK_BASE) + 200
    # 浅色那套一个字没动（默认参数必须与以前逐位一致）
    assert market_map.block_color(0.0) == (238, 238, 238)
    assert market_map.block_color(None) == (158, 158, 158)
    assert market_map.block_color(10.0) == (214, 48, 40)
    assert market_map.block_color(-10.0) == (26, 145, 74)


def test_dark_palette_separates_flat_from_missing_and_keeps_text_readable() -> None:
    """0%（真的没动）与 None（不知道）在深色底上**也必须**分得开，而且字要看得清。"""
    assert market_map.block_color(0.0, dark=True) != market_map.block_color(None, dark=True)

    def _lum(rgb):
        def _ch(value):
            c = value / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (_ch(v) for v in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def _contrast(fg, bg):
        hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for pct in (None, 0.0, 3.0, 5.0, 8.0, 10.0, -3.0, -5.0, -8.0, -10.0):
        bg = market_map.block_color(pct, dark=True)
        fg = market_map.text_color(pct, dark=True)
        assert fg != bg, pct
        assert _contrast(fg, bg) >= 4.3, (pct, fg, bg, round(_contrast(fg, bg), 2))

    # 深色底用**亮字**、亮底（接近饱和）用**深字** —— 这一条是"字和底糊在一起"的解药
    assert market_map.text_color(0.0, dark=True) == (232, 238, 248)
    assert market_map.text_color(10.0, dark=True) == (10, 18, 32)
