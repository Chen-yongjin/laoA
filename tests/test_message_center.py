"""「消息」窗口（仿 QQ 的消息列表）+ 选股完成那条消息的测试。

用户 2026-09-18 的原话
----------------------
> 「我的想法是通知仿照 QQ 桌面端，有消息软件图标闪烁，可以点开查看消息列表。」

所以这一块要钉住的是**交互**（不是画得好不好看）：

* 未读怎么算（关着的时候来的算未读、开着的时候看过的算已读）；
* **打开即已读**（窗口一显示，未读清零、主窗口停闪）；
* 点一条看详情、【全部已读】【清空】的行为；
* 选股完成也会进同一个列表（`kind="pool"`，走既有的 `intraday_alert` 表）；
* 飞书那条路**没被改坏**（用户明确要求保留，见 `tests/test_notify.py`）。

这些只有把控件真的建出来才测得到，所以用 Qt 的 `offscreen` 平台在无显示器环境里跑。
"""

from __future__ import annotations

import os
import pathlib

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面测试")

from PySide6.QtWidgets import QApplication  # noqa: E402

from laoa_trader import intraday  # noqa: E402
from laoa_trader.data import storage  # noqa: E402
from laoa_trader.ui.message_center import (  # noqa: E402
    COLUMNS,
    MAX_ROWS,
    MessageCenter,
    READ_MARK,
    UNREAD_MARK,
)

DAY = "2026-01-05"


@pytest.fixture()
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def center(qapp):
    """一个独立的消息窗口（不接主窗口，专测它自己的行为）。"""
    widget = MessageCenter()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _item(symbol: str = "600000", name: str = "样本股", kind: str = "break_high",
          when: str = f"{DAY} 09:31:05", detail: str = "现价 12.34 突破 20 日高点",
          label: str = "🚀 放量突破20日高") -> dict:
    """消息列表里的一条（主窗口 `_alert_items` 转出来的形状）。"""
    return {
        "date": DAY,
        "symbol": symbol,
        "target": f"{name}({symbol})",
        "name": name,
        "kind": kind,
        "kind_label": label,
        "detail": detail,
        "time": when,
    }


# ── 列表本身 ──


def test_columns_and_row_rendering(center) -> None:
    """四列（时间/标的/类型/说明），内容按传入的字段铺开。"""
    assert [center.table.horizontalHeaderItem(i).text()
            for i in range(center.table.columnCount())] == list(COLUMNS)

    center.set_messages([_item()])

    assert center.table.rowCount() == 1
    assert "样本股(600000)" in center.table.item(0, 1).text()
    assert "🚀 放量突破20日高" in center.table.item(0, 2).text()
    assert "突破 20 日高点" in center.table.item(0, 3).text()


def test_history_seeded_as_read_new_messages_are_unread(center) -> None:
    """灌历史**不算未读**；之后来的新消息才算（否则一开机就是一屏红点）。"""
    center.set_messages([_item("600000")])                 # 启动时灌库里的历史
    assert center.unread_count() == 0
    assert center.table.item(0, 0).text().startswith(READ_MARK)

    added = center.add_messages([_item("000001", "平安银行")])

    assert added == 1
    assert center.unread_count() == 1
    assert center.unread_count() == 1 and center.is_unread(_item("000001", "平安银行"))
    assert center.table.item(0, 0).text().startswith(UNREAD_MARK)   # 新的在最上面
    assert center.table.item(0, 0).font().bold() is True            # 未读加粗
    assert center.table.item(1, 0).font().bold() is False           # 已读不加粗


def test_duplicate_messages_are_not_added_twice(center) -> None:
    """同一条（日期+标的+类型）只进一次 —— 与库里的去重键同一个口径。"""
    assert center.add_messages([_item()]) == 1
    assert center.add_messages([_item()]) == 0
    assert center.table.rowCount() == 1


def test_message_arriving_while_open_is_already_read(center, qapp) -> None:
    """窗口开着时来的消息**直接算已读**（用户正看着这一屏，标红点没有意义）。"""
    center.show()
    qapp.processEvents()

    center.add_messages([_item()])

    assert center.table.rowCount() == 1
    assert center.unread_count() == 0
    center.close()
    center.add_messages([_item("000001", "平安银行")])       # 关掉之后再来的算未读
    assert center.unread_count() == 1


def test_opening_the_window_marks_everything_read(center, qapp) -> None:
    """**打开即已读**：`show()` 之后未读清零（QQ 的逻辑）。"""
    center.add_messages([_item(), _item("000001", "平安银行")])
    assert center.unread_count() == 2

    center.show()
    qapp.processEvents()

    assert center.unread_count() == 0
    assert "未读" not in center.title_label.text()


def test_clicking_a_row_marks_it_read_and_emits_it(center, qapp) -> None:
    """点某一条：那条转已读 + 把整条交给主窗口（弹详情用）。"""
    seen: list[dict] = []
    center.item_clicked.connect(seen.append)
    center.add_messages([_item(), _item("000001", "平安银行")])
    center.show()                                   # 打开会全部标已读，所以先关一次再放一条
    center.close()
    center.add_messages([_item("600519", "贵州茅台", when=f"{DAY} 10:00:00")])

    center._on_cell_clicked(0, 0)                    # 第一行 = 刚来的那条

    assert len(seen) == 1 and seen[0]["symbol"] == "600519"
    assert center.unread_count() == 0


def test_mark_all_read_button(center) -> None:
    """【全部已读】：未读清零，消息一条不少。"""
    center.add_messages([_item(), _item("000001", "平安银行")])

    center.btn_read_all.click()

    assert center.unread_count() == 0
    assert center.table.rowCount() == 2
    assert "未读" not in center.title_label.text()


def test_clear_button_only_clears_the_view(center) -> None:
    """【清空】只清这一屏：之后来的消息照常显示（库里一条都不删，见模块注释）。"""
    center.add_messages([_item(when=f"{DAY} 09:31:05")])

    center.btn_clear.click()

    assert center.table.rowCount() == 0
    center.add_messages([_item("000001", "平安银行", when=f"{DAY} 10:30:00")])
    assert center.table.rowCount() == 1              # 新的那条照常显示


def test_settings_button_emits_signal(center) -> None:
    """【通知设置】只举手（主窗口去切页），窗口自己不跳。"""
    seen: list[int] = []
    center.settings_requested.connect(lambda: seen.append(1))

    center.btn_settings.click()

    assert seen == [1]


def test_row_cap_keeps_the_newest(center) -> None:
    """列表最多留 `MAX_ROWS` 条，且留下的是**最新**的那些（旧的进不来）。"""
    for index in range(MAX_ROWS + 5):
        center.add_messages([_item(f"{index:06d}", "样本", when=f"{DAY} 09:{index:02d}:00")])

    assert center.table.rowCount() == MAX_ROWS
    assert center.messages()[0]["symbol"] == f"{MAX_ROWS + 4:06d}"


# ── 选股完成那条消息（走既有的 intraday_alert 表）──


def test_pool_message_is_recorded_once_per_content(tmp_path: pathlib.Path) -> None:
    """选股完成写进 `intraday_alert`（`kind="pool"`）：同一批内容只留一条，换了内容才是第二条。"""
    from laoa_trader.config import Config
    from laoa_trader.scheduler import _record_pool_message

    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    storage.init_db(cfg.db_path)
    rows = [{"symbol": "600519", "name": "贵州茅台"}, {"symbol": "000001", "name": "平安银行"}]

    _record_pool_message(cfg, DAY, rows)
    _record_pool_message(cfg, DAY, rows)                    # 同样的内容再来一次

    with storage.connect(cfg.db_path) as conn:
        alerts = storage.load_alerts_of_day(conn, DAY)
    assert len(alerts) == 1                                 # 内容没变 → 只一条
    assert alerts[0]["kind"] == intraday.KIND_POOL
    assert "共 2 只" in alerts[0]["detail"]
    assert "贵州茅台(600519)" in alerts[0]["detail"]
    assert intraday.KIND_LABELS[intraday.KIND_POOL] == "📈 选股完成"

    _record_pool_message(cfg, DAY, rows + [{"symbol": "300750", "name": "宁德时代"}])
    with storage.connect(cfg.db_path) as conn:
        assert len(storage.load_alerts_of_day(conn, DAY)) == 2   # 内容变了 → 第二条


def test_pool_message_shows_as_selection_result(center) -> None:
    """「选股完成」在列表里显示成「选股结果」，不是 `pool-3f2a…` 那串内部键。"""
    from laoa_trader.ui import app as ui_app

    # 标的列的写法由主窗口那个转换函数决定（`symbol` 是内容指纹，不是股票代码）
    target = ui_app.MainWindow._alert_target_text(
        {"symbol": "pool-3f2ac91b02", "kind": intraday.KIND_POOL}, {}
    )
    assert target == "选股结果"

    center.set_messages([_item(symbol="pool-3f2ac91b02", kind=intraday.KIND_POOL,
                               when=f"{DAY} 15:05:00", detail="共 12 只：贵州茅台(600519) 等",
                               label=intraday.KIND_LABELS[intraday.KIND_POOL])])
    assert "📈 选股完成" in center.table.item(0, 2).text()
    assert "共 12 只" in center.table.item(0, 3).text()
