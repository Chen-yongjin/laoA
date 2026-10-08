"""桌面导出：匹配结果那份额外的纯文本文件（用户要求"也可以同时 output 一个文件到桌面"）。

这一组钉住四件事：

1. **文件长什么样**：文件名带日期，正文逐行的版式（标题/数量/每一只票/页脚提醒），
   字符编码与换行符也要对（这份文件是给 Windows 用户**双击打开**的）；
2. **落在哪里**：桌面优先（`Desktop` → `桌面` → OneDrive 里的桌面），
   一个都没有才退回**数据目录** —— 绝不因为"桌面被 OneDrive 重定向 / 改成中文名"
   就把结果丢掉；
3. **价格口径**：现价读**不复权**的 `stock_daily_raw`（后复权价写进给人看的文件里
   会变成"茅台 2600 元"这种假数字）；没有价格就**不写那一段**，不编数字；
4. **不许出事**：写盘失败、目录建不出来、没有可写目录 —— 一律只记日志 + 返回 None，
   绝不抛异常（导出是附赠产物，不能把匹配流程带走；调用方那一层另有用例，见
   `tests/test_scheduler_gate.py`）。

**测试一律注入 `dest_dir`/`home`（家目录用 tmp_path 造）**：
`pool.export_pick_file()` 不传 `dest_dir` 时会去找**真桌面** ——
测试要是走那条路，就会往跑测试的人桌面上丢文件（丢过一次就再也不敢随手跑测试了）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from laoa_trader import intraday, pool
from tests.conftest import messages


def _pool_rows() -> list[dict]:
    """三条池子行：一条内置策略选的、一条纯自选（手工加的）、一条自定义公式选的。

    字段与 `pool.pool_table_rows()` / `build_pool()` 的返回值一致
    （`strategy`/`strategies`/`source`/`source_label`）—— 桌面文件读的就是
    界面上同一份行数据，用例也照那个形状造。
    """
    return [
        {"symbol": "600519", "name": "贵州茅台", "strategy": "ReversalStrategy",
         "strategies": "ReversalStrategy", "source": "策略",
         "source_label": "短期反转"},
        {"symbol": "000001", "name": "平安银行", "strategy": "", "strategies": "",
         "source": "自选", "source_label": "自选", "watchlist": True},
        {"symbol": "600002", "name": "半导体甲", "strategy": "公式·放量上攻",
         "strategies": "公式·放量上攻", "source": "公式",
         "source_label": "公式·放量上攻"},
    ]


# ── 1) 文件名与正文版式 ──


def test_file_name_carries_the_date_and_content_is_the_agreed_layout(tmp_path) -> None:
    """文件名带日期；正文 = 标题 + 数量 + 逐只清单 + 页脚（用户给定的版式）。"""
    path = pool.export_pick_file(
        _pool_rows(),
        data_date="2026-09-17",
        day="2026-09-18",
        dest_dir=tmp_path,
        quotes={"600519": (1266.98, 0.71)},     # 注入行情：用例不碰库、不联网
    )

    assert path == tmp_path / "luweik-匹配结果-2026-09-18.txt"
    assert path is not None and path.exists()
    assert path.read_text(encoding="utf-8-sig").splitlines() == [
        "luweik · 匹配结果 · 2026-09-18（行情日 2026-09-17）",
        # M = 有来源策略的行（内置 + 公式），K = 自选 —— 与「自选标的」表头同一口径
        "共 3 只（策略 2 · 自选 1）",
        "1. 贵州茅台(600519)  现价 1266.98 +0.71%  来源：短期反转",
        "2. 平安银行(000001)  来源：自选",
        "3. 半导体甲(600002)  来源：放量上攻",
        pool.EXPORT_FOOTER,
    ]


def test_the_file_is_utf8_with_bom_and_crlf(tmp_path) -> None:
    """编码/换行要适合 Windows 双击打开：UTF-8 **带 BOM** + CRLF。

    没有 BOM 时老版本记事本会把中文显示成乱码；CRLF 是 Windows 编辑器的原生换行
    （同时也是"写出去的字节是确定的"这条保证，测试才能逐字节钉住内容）。
    """
    path = pool.export_pick_file(_pool_rows(), dest_dir=tmp_path, day="2026-09-18")

    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")                 # UTF-8 BOM
    assert b"\r\n" in data                                  # 行尾是 CRLF
    assert b"\r\r\n" not in data                            # 别把 CRLF 又翻译一遍
    assert "贵州茅台" in data.decode("utf-8-sig")            # 中文没乱


def test_file_name_defaults_to_today_in_beijing_time(tmp_path, monkeypatch) -> None:
    """不传 `day` 时用**北京时间今天**（部署在别的时区的机器也不会写成昨天）。"""
    monkeypatch.setattr(intraday, "now_shanghai",
                        lambda *a, **k: datetime(2026, 9, 18, 9, 30))

    path = pool.export_pick_file(_pool_rows(), dest_dir=tmp_path)

    assert path is not None and path.name == "luweik-匹配结果-2026-09-18.txt"


def test_no_tooltip_or_reason_noise_but_source_label_is_kept(tmp_path) -> None:
    """来源优先用行里算好的 `source_label`；行里没有时才算一次（组合来源要留住）。

    `短期反转+自选` 这种"既是选出来的、又是自选"的标记是用户判断该不该动手的依据
    （改动方案里它也是「来源」列的写法），桌面文件必须与界面说同一个词。
    `run_daily` 传进来的池子行就是"没有 `source_label`"那种（只有 `strategy` +
    `watchlist` 标记），所以第 2 行专门走这条路。
    """
    rows = [
        {"symbol": "600002", "name": "半导体甲", "strategy": "ReversalStrategy",
         "strategies": "ReversalStrategy", "source": "策略+自选",
         "source_label": "短期反转+自选"},
        {"symbol": "300001", "name": "创业样本", "strategy": "ReversalStrategy",
         "strategies": "ReversalStrategy", "source": "策略+自选",
         "watchlist": True},                    # 没有 source_label → 现算
    ]

    text = pool.pick_export_text(rows, data_date="2026-09-17", day="2026-09-18")

    assert "1. 半导体甲(600002)  来源：短期反转" in text
    assert "2. 创业样本(300001)  来源：短期反转" in text


def test_source_never_prints_a_placeholder_dash(tmp_path) -> None:
    """连 `watchlist` 标记都没有的纯自选行：来源写 `自选`（**不是** `—`）。

    给人看的文件里写"来源：—"等于什么都没说；`source` 字段里本来就有答案。
    """
    rows = [{"symbol": "000001", "name": "平安银行", "strategy": "", "strategies": "",
             "source": "自选"}]

    text = pool.pick_export_text(rows, day="2026-09-18")

    assert "1. 平安银行(000001)  来源：自选" in text
    assert "来源：—" not in text


def test_no_pool_rows_writes_nothing(tmp_path, log_records) -> None:
    """没有结果就不导出（不写空文件，也不抛异常）。"""
    assert pool.export_pick_file([], dest_dir=tmp_path, day="2026-09-18") is None
    assert pool.export_pick_file(None, dest_dir=tmp_path, day="2026-09-18") is None
    assert list(tmp_path.iterdir()) == []                    # 一个文件都没留下
    assert "不导出桌面文件" in messages(log_records)


def test_rows_without_symbol_are_dropped(tmp_path) -> None:
    """没有代码的行不算数（老库/手改过的行里可能混着空代码）。"""
    rows = [{"name": "没有代码的行"},
            {"symbol": "600519", "name": "贵州茅台", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy", "source": "策略",
             "source_label": "短期反转"}]

    path = pool.export_pick_file(rows, dest_dir=tmp_path, day="2026-09-18")

    text = path.read_text(encoding="utf-8-sig")
    assert "共 1 只（策略 1 · 自选 0）" in text
    assert "没有代码的行" not in text


# ── 2) 落在哪里：桌面优先、数据目录兜底 ──


def test_desktop_directory_is_used_when_it_exists(tmp_path) -> None:
    """`~/Desktop` 存在就用它（而不是数据目录）。"""
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    fallback = tmp_path / "data"
    fallback.mkdir()

    assert pool.desktop_dir(home=home, fallback_dir=fallback) == home / "Desktop"

    path = pool.export_pick_file(_pool_rows(), dest_dir=None, home=home,
                                 fallback_dir=fallback, day="2026-09-18")
    # 2026-09-21（主人要求）：桌面根目录不再散着文件，收进 `桌面/luweik/` 里
    assert pool.EXPORT_FOLDER_NAME == "luweik"
    assert path == (home / "Desktop" / pool.EXPORT_FOLDER_NAME
                    / "luweik-匹配结果-2026-09-18.txt")
    assert not list(fallback.iterdir())                      # 没有重复写进数据目录


def test_chinese_desktop_name_is_found(tmp_path) -> None:
    """中文系统的桌面叫「桌面」—— 只按 `Desktop` 找会在中文 Windows 上"写不出去"。"""
    home = tmp_path / "home"
    (home / "桌面").mkdir(parents=True)

    assert pool.desktop_dir(home=home) == home / "桌面"
    path = pool.export_pick_file(_pool_rows(), dest_dir=None, home=home,
                                 day="2026-09-18")
    assert path == (home / "桌面" / pool.EXPORT_FOLDER_NAME
                    / "luweik-匹配结果-2026-09-18.txt")


def test_onedrive_desktop_is_found(tmp_path) -> None:
    """桌面被 OneDrive 接管（`~/OneDrive/Desktop`）时也要找得到。"""
    home = tmp_path / "home"
    (home / "OneDrive" / "Desktop").mkdir(parents=True)

    assert pool.desktop_dir(home=home) == home / "OneDrive" / "Desktop"


def test_falls_back_to_the_data_dir_when_no_desktop_exists(tmp_path) -> None:
    """一个桌面都没有 → 退回数据目录（**并且把目录建出来**），文件照样有。"""
    home = tmp_path / "home"
    home.mkdir()
    fallback = tmp_path / "data" / "导出"           # 故意还不存在

    path = pool.export_pick_file(_pool_rows(), dest_dir=None, home=home,
                                 fallback_dir=fallback, day="2026-09-18")

    # 回退目录里也套一层「luweik」（口径与桌面那条一致：文件永远收在一个文件夹里）
    assert path == (fallback / pool.EXPORT_FOLDER_NAME
                    / "luweik-匹配结果-2026-09-18.txt")
    assert path.exists()


def test_no_desktop_and_no_fallback_says_so_and_returns_none(tmp_path, log_records) -> None:
    """连回退目录都没有 → 返回 None 并在日志里说清（不抛异常、不静默）。"""
    home = tmp_path / "home"
    home.mkdir()

    assert pool.desktop_dir(home=home) is None
    assert pool.export_pick_file(_pool_rows(), dest_dir=None, home=home,
                                 fallback_dir=None, day="2026-09-18") is None
    text = messages(log_records)
    assert "找不到桌面目录" in text
    assert "不影响匹配与推送" in text


def test_missing_dest_dir_is_created(tmp_path) -> None:
    """调用方给的目录不存在就建（`--once` 之类的无人值守入口不会先去建目录）。"""
    target = tmp_path / "还没建" / "更深一层"

    path = pool.export_pick_file(_pool_rows(), dest_dir=target, day="2026-09-18")

    assert path is not None and path.parent == target and target.is_dir()


# ── 3) 现价与涨跌幅的口径 ──


def test_latest_quotes_reads_unadjusted_prices(db) -> None:
    """现价读 `stock_daily_raw`（不复权）；涨跌幅 = 最近两个交易日收盘价之比。

    `db` 是合成小库：600001 每天 −1.2%，600002 每天 +0.1%
    （见 `tests/conftest.py` 的 `db` fixture）。
    """
    quotes = pool.latest_quotes(db)

    assert set(quotes) == {"600001", "600002", "600003", "000001", "300001", "000002"}
    price, pct = quotes["600001"]
    assert price > 0
    assert pct == pytest.approx(-1.2, abs=0.05)             # 阴跌那只
    assert quotes["600002"][1] == pytest.approx(0.1, abs=0.05)


def test_export_uses_the_db_for_prices_when_quotes_are_not_injected(db, tmp_path) -> None:
    """不注入行情时按库算现价（`run_daily` 走的就是这条路：只传 `db_path`）。"""
    rows = [{"symbol": "600002", "name": "半导体甲", "strategy": "ReversalStrategy",
             "strategies": "ReversalStrategy", "source_label": "短期反转"}]

    path = pool.export_pick_file(rows, dest_dir=tmp_path, db_path=db, day="2026-09-18")

    line = path.read_text(encoding="utf-8-sig").splitlines()[2]
    price, pct = pool.latest_quotes(db)["600002"]
    assert f"现价 {price:.2f} {pct:+.2f}%" in line


def test_no_price_means_no_price_segment(tmp_path) -> None:
    """库里没有这只票的价格 → 那一行**不写**现价（宁可少一个数字，也不编一个）。"""
    row = {"symbol": "999999", "name": "查不到价格的票", "strategy": "ReversalStrategy",
           "strategies": "ReversalStrategy", "source_label": "短期反转"}

    path = pool.export_pick_file([row], dest_dir=tmp_path, quotes={}, day="2026-09-18")

    line = path.read_text(encoding="utf-8-sig").splitlines()[2]
    assert line == "1. 查不到价格的票(999999)  来源：短期反转"
    assert "现价" not in line


def test_price_without_previous_close_omits_only_the_percentage(tmp_path) -> None:
    """只有一天数据（没有昨收）→ 写现价、不写涨跌幅（除零会把整份导出变成"没文件"）。"""
    text = pool.pick_export_text(
        [{"symbol": "600519", "name": "贵州茅台", "source_label": "短期反转"}],
        day="2026-09-18", quotes={"600519": (1266.98, None)},
    )

    assert "现价 1266.98  来源：短期反转" in text
    assert "%" not in text


def test_broken_quote_rows_do_not_break_the_file(tmp_path) -> None:
    """价格字段是脏数据（字符串/空元组）时只丢价格段，文件照常写出来。"""
    text = pool.pick_export_text(
        [{"symbol": "600519", "name": "贵州茅台", "source_label": "短期反转"},
         {"symbol": "600002", "name": "半导体甲", "source_label": "短期反转"}],
        day="2026-09-18", quotes={"600519": ("不是数字", 1.0), "600002": ()},
    )

    assert "现价" not in text
    assert "1. 贵州茅台(600519)  来源：短期反转" in text


# ── 4) 失败只记日志 ──


def test_write_failure_is_only_logged(tmp_path, monkeypatch, log_records) -> None:
    """写盘失败（磁盘满/权限/文件被占用）→ 返回 None + 日志说清，**不抛异常**。"""
    def boom(path: Path, text: str) -> None:
        raise OSError("磁盘满了（模拟）")

    monkeypatch.setattr(pool, "_write_export", boom)

    assert pool.export_pick_file(_pool_rows(), dest_dir=tmp_path,
                                 day="2026-09-18") is None
    text = messages(log_records)
    assert "导出匹配结果到桌面失败" in text
    assert "OSError" in text and "磁盘满了（模拟）" in text
    assert "不影响匹配与推送" in text


def test_dest_dir_being_a_file_is_only_logged(tmp_path, log_records) -> None:
    """`dest_dir` 其实是个文件（用户把路径配错了）→ 同样只记日志。

    这条走的是**真实的文件系统错误**（`mkdir` 必然失败），不靠 monkeypatch，
    所以它还能证明"目录建不出来"这条路真的是被兜住的。
    """
    occupied = tmp_path / "桌面"
    occupied.write_text("我不是目录", encoding="utf-8")

    assert pool.export_pick_file(_pool_rows(), dest_dir=occupied,
                                 day="2026-09-18") is None
    assert "导出匹配结果到桌面失败" in messages(log_records)
    assert occupied.read_text(encoding="utf-8") == "我不是目录"      # 没被破坏


def test_broken_db_for_prices_does_not_stop_the_export(tmp_path, log_records) -> None:
    """行情库坏了（文件不是 sqlite）→ 取不到价格，但文件**照样**写出来。

    库不存在时 `storage.connect()` 会建一个空库、`latest_quotes` 返回 `{}`（不算失败）；
    真正会抛的是"文件存在但不是 sqlite"这种 —— 那也必须只记日志。
    """
    broken = tmp_path / "坏库.db"
    broken.write_bytes("这不是一个 sqlite 文件".encode("utf-8"))

    path = pool.export_pick_file(_pool_rows(), dest_dir=tmp_path,
                                 db_path=broken, day="2026-09-18")

    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "1. 贵州茅台(600519)  来源：短期反转" in text
    assert "现价" not in text                                # 没有价格就不写那一段
    assert "读取最新价失败" in messages(log_records)


def test_missing_db_just_means_no_prices(tmp_path) -> None:
    """库文件还不存在（第一次跑、数据目录是新的）→ 建出来、照常导出，只是没有现价。"""
    path = pool.export_pick_file(_pool_rows(), dest_dir=tmp_path,
                                 db_path=tmp_path / "还没建的库.db", day="2026-09-18")

    assert path is not None and path.exists()
    assert "现价" not in path.read_text(encoding="utf-8-sig")


def test_export_carries_no_private_information(tmp_path) -> None:
    """隐私：正文里只有匹配结果本身 —— 没有本地路径、没有 Key/Token。

    这份文件是用户要往外发的（贴群里、发给朋友），把本机路径或凭据写进去
    等于替他泄露环境信息。
    """
    rows = [{**_pool_rows()[0], "note": "我自己的备注"}]
    path = pool.export_pick_file(rows, dest_dir=tmp_path, day="2026-09-18")

    text = path.read_text(encoding="utf-8-sig")
    lowered = text.lower()
    assert str(tmp_path) not in text
    # 正文里连一个路径分隔符都不该有（Unix 的 `/` 与 Windows 的 `\`）
    assert "/" not in text and "\\" not in text
    for secret in ("api_key", "apikey", "token", "secret", "webhook", "password"):
        assert secret not in lowered


def test_legacy_export_folder_is_moved_to_the_new_name(tmp_path) -> None:
    """改名后的第一次导出：老的 `桌面\\财神助手\\` 整个搬到 `桌面\\luweik\\`（历史文件不丢）。

    为什么要有这一步：目录名跟着产品名走，改名之后老用户那几个月攒下来的
    `*-匹配结果-*.txt` 会留在一个再也不会被写入的旧文件夹里 —— 文件没丢，但用户
    再也看不到、程序也不会再往里写，等于凭空少了一段历史。（2026-10-08 又一次改名时加。）
    """
    home = tmp_path / "home"
    desktop = home / "Desktop"
    legacy = desktop / "财神助手"
    legacy.mkdir(parents=True)
    (legacy / "旧名-匹配结果-2026-09-01.txt").write_text("历史", encoding="utf-8")

    path = pool.export_pick_file(_pool_rows(), dest_dir=None, home=home,
                                 day="2026-09-18")

    assert path.parent == desktop / pool.EXPORT_FOLDER_NAME
    assert (path.parent / "旧名-匹配结果-2026-09-01.txt").read_text(encoding="utf-8") == "历史"
    assert not legacy.exists()                              # 旧目录搬走了（在同一块盘上是改名，不复制）


def test_legacy_export_folder_is_kept_when_the_new_one_already_exists(tmp_path) -> None:
    """新目录已经存在（用户两边都在用）→ **什么都不动**：合并两份历史不归程序替用户决定。"""
    home = tmp_path / "home"
    desktop = home / "Desktop"
    (desktop / pool.EXPORT_FOLDER_NAME).mkdir(parents=True)
    legacy = desktop / "财神助手"
    legacy.mkdir()
    (legacy / "旧文件.txt").write_text("历史", encoding="utf-8")

    pool.export_pick_file(_pool_rows(), dest_dir=None, home=home, day="2026-09-18")

    assert (legacy / "旧文件.txt").exists()                  # 旧目录原样留着
