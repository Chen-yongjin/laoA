"""下载进度的**显示层**：命令行按秒/按 10% 打一行、状态文本要说清"在干什么"。

用户反馈（分发前）：下载 180 MB 的过程**没有任何进度显示**，像卡死。
产品侧其实一直在回调（`hithink` → `sync` 把字节数报上来），问题在显示：

- 命令行原来每个 1 MB 块都打一行 → 180 行刷屏，有用信息全被冲走；
- 状态文本只有干巴巴的计数，没有 MB / 百分比。

这里钉住新的显示约定：**按秒或按 10% 新起一行**（其余在同名行上 `\\r` 覆盖刷新），
进度一定单调、最后一定收尾 100%，并且"正在重签 URL 继续下载（第 N 次）"这类状态
另起一行、不会被进度覆盖掉。
"""

from __future__ import annotations

import re

import pytest

from laoa_trader import __main__ as cli_mod
from tests._toml import p


@pytest.fixture(autouse=True)
def _clean_progress_state():
    """进度节流状态是进程级的：每个用例前后都清干净。"""
    cli_mod._PROGRESS_LAST.clear()
    yield
    cli_mod._PROGRESS_LAST.clear()


def test_progress_line_shows_mb_and_percent() -> None:
    """一行里必须有"在干什么 + 已下/总量（MB）+ 百分比"。"""
    line = cli_mod._progress_line("下载 daily-k", 81_000_000, 180_700_000)
    assert "下载 daily-k" in line
    assert "81.0 MB" in line and "180.7 MB" in line
    assert "44.8%" in line
    bar = line[line.index("[") + 1 : line.index("]")]      # 只看方括号里的进度条
    assert len(bar) == 20 and set(bar) <= {"#", "."}       # 有进度条，一眼看得出在动
    assert bar.count("#") == 8                             # 44.8% → 8/20 格


def test_progress_is_throttled_by_ten_percent(capsys) -> None:
    """100 次回调（每 1%）只应打出很少的**成行**输出，并收尾 100%。

    注意 `str.splitlines()` 会把 `\r`（原地刷新）也算成换行，所以这里按
    `\n` 切分：够证明"回调 100 次但只成行十几次"，否则会误判成没节流。
    """
    total = 180_700_000
    for i in range(1, 101):
        cli_mod._progress("下载 daily-k", total * i // 100, total)
    out = capsys.readouterr().out
    # `^`（MULTILINE）只匹配 `\n` 之后 → 恰好是"新起一行"的那些输出
    lines = re.findall(r"(?m)^下载 daily-k：[^\r\n]*", out)
    refreshes = [ln for ln in out.split("\r") if "下载 daily-k" in ln]
    assert len(refreshes) >= 90, "每次回调都应该有输出（只是大多在原地刷新）"
    assert 3 <= len(lines) <= 14, lines                     # 成行的很少（原来会打 100 行）
    assert any("10.0%" in ln for ln in lines)               # 跨过 10% 有记录
    assert lines[-1].count("100.0%") == 1                   # 最后一行是 100%


def test_progress_never_goes_backwards(capsys) -> None:
    """进度只增不减（进度条回退看起来就像"又重下了"）。"""
    total = 1000
    seen: list[int] = []
    for done in (10, 100, 250, 250, 700, 1000):
        cli_mod._progress("下载 daily-k", done, total)
        seen.append(done)
    assert seen == sorted(seen)
    out = capsys.readouterr().out
    assert "100.0%" in out


def test_progress_without_total_still_prints(capsys) -> None:
    """总大小未知（服务端没给 content-length）时也要有输出，不能静默。"""
    cli_mod._progress("下载 daily-k", 5_000_000, 0)
    out = capsys.readouterr().out
    assert "下载 daily-k" in out and "5.0 MB" in out


def test_progress_reaches_hundred_percent_for_small_download(capsys) -> None:
    """小文件（如复权事件 dump）同样要能收尾 100%（不能被节流吃掉最后一行）。"""
    cli_mod._progress("下载 adjustment-factors", 300_000, 300_000)
    out = capsys.readouterr().out
    assert "100.0%" in out


def test_note_prints_its_own_line(capsys) -> None:
    """"正在重签 URL 继续下载（第 2 次）"要**另起一行**，不会被进度覆盖掉。"""
    cli_mod._progress("下载 daily-k", 87_000_000, 180_700_000)
    cli_mod._note("正在重签 URL 继续下载（第 2 次，从 87.0 MB 处接着下）")
    out = capsys.readouterr().out
    assert "↻ 正在重签 URL 继续下载（第 2 次" in out


def test_progress_state_resets_between_cli_runs(capsys, tmp_path, cfg, monkeypatch) -> None:
    """同一阶段第二次跑也要有输出（节流状态必须每轮 CLI 清一次）。"""
    from laoa_trader.__main__ import cli
    from laoa_trader.data import sync as sync_mod

    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = "k"\n', encoding="utf-8")

    def fake_download(cfg_, progress_cb=None, note_cb=None, **kwargs):
        if note_cb:
            note_cb("正在重签 URL 继续下载（第 2 次）")
        if progress_cb:
            progress_cb("下载 daily-k", 90_000_000, 180_700_000)
            progress_cb("下载 daily-k", 180_700_000, 180_700_000)
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=10, detail="写入 10 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    for _ in range(2):
        assert cli(["--cli", "--download", "--config", str(config)]) == 0
        out = capsys.readouterr().out
        assert "下载 daily-k" in out, out
        assert "100.0%" in out, out
        assert "正在重签 URL 继续下载" in out, out


def test_cli_download_shows_progress_and_notes(capsys, tmp_path, cfg, monkeypatch) -> None:
    """`--download` 整条链路：进度（MB/百分比）+ 状态（重签）= 用户看得见。"""
    from laoa_trader.__main__ import cli
    from laoa_trader.data import sync as sync_mod

    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{p(cfg.data_dir)}"\nhithink_api_key = "k"\n', encoding="utf-8")

    def fake_download(cfg_, progress_cb=None, note_cb=None, **kwargs):
        assert note_cb is not None, "CLI 必须把 note_cb 传下去，否则用户看不到重签状态"
        note_cb("第 1/5 次未成功（传输：连接被中断），已下 87.0 MB；稍后自动重签 URL 继续")
        note_cb("正在重签 URL 继续下载（第 2 次，从 87.0 MB 处接着下）")
        for pct in range(50, 101, 10):
            progress_cb("下载 daily-k", 180_700_000 * pct // 100, 180_700_000)
        return sync_mod.SyncResult(stage="下载历史数据", ok=True, rows=5, detail="写入 5 行")

    monkeypatch.setattr(sync_mod, "download_history", fake_download)
    # needs_full 才会真下载：空库 + 默认门槛
    assert cli(["--cli", "--download", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "开始下载历史数据" in out
    assert "MB" in out and "%" in out
    assert "正在重签 URL 继续下载（第 2 次" in out
    assert "100.0%" in out
    assert "Traceback" not in out
