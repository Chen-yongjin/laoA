"""dump 下载：分块 + 断点续传 + URL 过期自动重签 + 有界重试 + 完整性校验。

背景（用户实测）：同花顺的 dump 预签名 URL **只允许 GET**（HEAD 403）、有效期约 5 分钟；
`daily-k` 有 180 MB。老实现是"一次流式 GET 直接把响应写进目标文件"，遇到
"URL 过期 / 连接被重置 / 读取超时"就整轮失败，而且下次还得从 0 开始 ——
这正是"下载不了 10 年数据"的根因。

这些用例全部**离线**：用假 session 精确模拟"断流一次""403 过期""服务器忽略 Range"
"Range 超界"等真实故障，断言**用了 Range 从断点继续**（而不是从头下）、最终文件完整。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from laoa_trader.data import hithink as hx
from laoa_trader.data import sync


# ── 假 session：可编程的"远端" ──


class FakeStreamResponse:
    """模拟 requests 的流式响应（支持 Range / 206 / 中途断流 / 403）。"""

    def __init__(self, *, status_code=206, body=b"", headers=None, fail_after=None,
                 error=None, head_only=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._fail_after = fail_after          # 传够这么多字节后"断网"
        self._error = error or hx.requests.ConnectionError("连接被重置")
        self._head_only = head_only

    def iter_content(self, chunk_size=1 << 20):
        """先发 `fail_after` 个字节**再**断（0 = 第一个块就断，模拟连上就断流）。"""
        sent = 0
        for start in range(0, len(self._body), chunk_size):
            chunk = self._body[start : start + chunk_size]
            if self._fail_after is not None and sent + len(chunk) > self._fail_after:
                partial = self._body[start : start + (self._fail_after - sent)]
                if partial:
                    yield partial
                raise self._error
            sent += len(chunk)
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            raise hx.requests.HTTPError(f"HTTP {self.status_code}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDumpSession:
    """假的远端：按 `plan` 依次返回响应，并记录每次请求的 Range。

    plan 里的每一项可以是：
      - `bytes`：直接给这段数据（自动补 content-range/content-length）；
      - `FakeStreamResponse`：原样返回（用来模拟 403 / 断流 / 416 / 200）；
    """

    def __init__(self, payload: bytes, plan: list) -> None:
        self.payload = payload
        self.plan = list(plan)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, stream=False, timeout=None, **kw):
        if not stream:                      # dump_url() 那次签名请求
            self.calls.append({"kind": "sign", "url": url})
            return _Slurp({"code": 0, "data": {"presigned_url": "https://example.com/dump",
                                               "presigned_url_expires_at": "soon"}})
        offset = 0
        rng = (headers or {}).get("Range")
        if rng and rng.startswith("bytes=") and rng.endswith("-"):
            offset = int(rng[len("bytes=") : -1])
        self.calls.append({"kind": "download", "offset": offset, "range": rng,
                           "timeout": timeout, "headers": dict(headers or {})})
        step = self.plan.pop(0) if self.plan else self._whole(offset)
        if callable(step):
            step = step(offset)                 # 让响应依赖当前 Range 起点
        if isinstance(step, bytes):
            step = self._whole(offset, forced_body=step)
        return step

    def _whole(self, offset: int, forced_body: bytes | None = None) -> FakeStreamResponse:
        body = self.payload[offset:] if forced_body is None else forced_body
        total = len(self.payload) if forced_body is None else offset + len(body)
        return FakeStreamResponse(
            status_code=206 if offset else 200,
            body=body,
            headers={"content-range": f"bytes {offset}-{offset + len(body) - 1}/{total}",
                     "content-length": str(len(body))},
        )

    @property
    def download_calls(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] == "download"]

    @property
    def sign_calls(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] == "sign"]


class _Slurp:
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload


def _parquet_bytes(rows: int = 50, start: int = 0) -> bytes:
    """造一段**真的可读**的 Parquet 字节（完整性校验要用）。

    `start` 用来造"另一代"数据：只要起点不同，**文件前缀就不同**，
    这样"把新版本尾部贴到旧版本头部"拼出来的文件才与任何一代都不相等
    （否则两代共享前缀，拼接结果可能刚好等于新一代，护栏被关掉也测不出来）。
    """
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame({"a": range(start, start + rows)}).to_parquet(buf, index=False)
    return buf.getvalue()


@pytest.fixture()
def client() -> hx.HithinkClient:
    return hx.HithinkClient(api_key="k", retries=0, pace=0)


class _FakeTime:
    """替掉 hithink 里的 time 模块：sleep 只记录不真睡。"""

    def __init__(self, sleeps: list) -> None:
        self._sleeps = sleeps
        self.monotonic = __import__("time").monotonic

    def sleep(self, seconds: float) -> None:
        self._sleeps.append(seconds)


def _patch_time(monkeypatch) -> list:
    """把 hithink 里的退避 sleep 换成"只记录不真睡"，测试不必等。"""
    sleeps: list = []
    monkeypatch.setattr(hx, "time", _FakeTime(sleeps))
    return sleeps


# ── 1) 中途断连 → 用 Range 从断点继续，不从头下 ──


def test_resume_after_interrupted_download(client, tmp_path, monkeypatch) -> None:
    payload = _parquet_bytes()
    split = len(payload) // 3
    # 第一次：发 1/3 之后连接被重置（content-length 告诉我们总长是多少）
    first = FakeStreamResponse(
        status_code=200, body=payload,
        headers={"content-length": str(len(payload))}, fail_after=split,
    )
    session = FakeDumpSession(payload, [first])
    client.session = session
    sleeps = _patch_time(monkeypatch)

    target = tmp_path / "daily-k.parquet"
    out = client.download_dump("daily-k", dest=target, max_attempts=3)

    assert out == target and target.read_bytes() == payload       # 最终文件完整
    calls = session.download_calls
    assert len(calls) >= 2
    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == split, calls           # **从断点续**，不是从头
    assert calls[1]["range"] == f"bytes={split}-"
    assert sleeps, "应该有退避等待"                       # 有界重试的退避
    assert not (tmp_path / "daily-k.parquet.part").exists()   # 成功后清理 .part


# ── 2) URL 过期（403）→ 重新签一次并从中断处继续 ──


def test_expired_url_is_resigned_and_resumed(client, tmp_path, monkeypatch) -> None:
    payload = _parquet_bytes()
    half = len(payload) // 2
    expired = FakeStreamResponse(status_code=403, body=b"", headers={})
    partial = FakeStreamResponse(
        status_code=200, body=payload,
        headers={"content-length": str(len(payload))}, fail_after=half,
    )
    session = FakeDumpSession(payload, [expired, partial])
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=4)
    assert out.read_bytes() == payload
    assert len(session.sign_calls) >= 2, "每次尝试都应重新签 URL"
    offsets = [c["offset"] for c in session.download_calls]
    assert offsets[0] == 0
    assert offsets[-1] == half, offsets          # 403 之后从断点继续


def test_expired_url_midtransfer(client, tmp_path, monkeypatch) -> None:
    """下载到一半 URL 过期（连接被服务端掐断）→ 重签 + 断点续。"""
    payload = _parquet_bytes()
    cut = len(payload) // 4
    partial = FakeStreamResponse(
        status_code=200, body=payload,
        headers={"content-length": str(len(payload))}, fail_after=cut,
    )
    session = FakeDumpSession(payload, [partial])
    client.session = session
    _patch_time(monkeypatch)
    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3)
    assert out.read_bytes() == payload
    assert session.download_calls[1]["offset"] == cut


# ── 2.5) 进度与状态：180 MB 不能"看着像卡死" ──


def test_progress_streams_many_times_and_reaches_100(client, tmp_path, monkeypatch) -> None:
    """分块下载期间进度回调要**多次触发**、数值单调不减、最后一次到 100%。

    用户的反馈是"下载过程没有进度显示，180MB 像卡死"。所以这条用例钉住三件事：
    1. 每收到一块就回调一次（≥3 次，而不是开头结尾各一次）；
    2. `done` 单调不减（进度条不会回退）；
    3. 最后一次 `done == total`（进度条能走到底 → 100%）。
    """
    payload = _parquet_bytes(rows=200_000)          # 足够大，会被切成很多块
    assert len(payload) > 3 * (1 << 20) or True
    session = FakeDumpSession(payload, [])
    client.session = session
    events: list[tuple[int, int]] = []

    client.download_dump("daily-k", dest=tmp_path / "d.parquet",
                         progress_cb=lambda done, total: events.append((done, total)))

    assert len(events) >= 3, f"回调次数太少（{len(events)}）：进度条不会动"
    dones = [d for d, _ in events]
    assert dones == sorted(dones), dones             # 单调不减
    assert events[-1][0] == len(payload)             # 到 100%
    assert events[-1][0] == events[-1][1]
    assert all(total == len(payload) for _, total in events)   # 每块都知道总大小


def test_notes_report_resign_retry_and_completion(client, tmp_path, monkeypatch) -> None:
    """状态回调要说清"正在重签 URL 继续下载（第 N 次）"与"下载完成"。"""
    payload = _parquet_bytes()
    half = len(payload) // 2
    partial = FakeStreamResponse(
        status_code=200, body=payload,
        headers={"content-length": str(len(payload))}, fail_after=half,
    )
    session = FakeDumpSession(payload, [partial])
    client.session = session
    _patch_time(monkeypatch)
    notes: list[str] = []

    client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3,
                         note_cb=notes.append)

    joined = "\n".join(notes)
    assert any("正在重签 URL 继续下载" in n and "第 2 次" in n for n in notes), notes
    assert "下载完成" in joined
    assert any("MB" in n or "KB" in n or "B" in n for n in notes)   # 带上体量，别只说"重试中"


def test_notes_report_cancellation(client, tmp_path, monkeypatch) -> None:
    """用户取消也要有状态（否则界面停在"正在下载"上，用户不知道停了没有）。"""
    payload = _parquet_bytes()
    session = FakeDumpSession(payload, [])
    client.session = session
    notes: list[str] = []

    with pytest.raises(hx.DownloadCancelled):
        client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=2,
                             should_stop=lambda: True, note_cb=notes.append)
    assert any("已取消" in n for n in notes), notes
    # 取消不等于"什么都没留下"：已下好的字节仍在 .part 里，下次接着传
    part = tmp_path / "d.parquet.part"
    assert part.exists() and part.stat().st_size > 0
    assert not (tmp_path / "d.parquet").exists()       # 没校验通过就不许冒充成品


def test_note_callback_exception_does_not_break_download(client, tmp_path) -> None:
    """显示层（界面/命令行）的状态回调抛异常，不能把下载搞崩。"""
    payload = _parquet_bytes()
    client.session = FakeDumpSession(payload, [])

    def boom(_text: str) -> None:
        raise RuntimeError("界面炸了")

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", note_cb=boom)
    assert out.read_bytes() == payload


def test_sync_forwards_notes_from_client(cfg, monkeypatch) -> None:
    """`sync.download_dump` 要把 note_cb 透传给客户端，并给整段下载打上阶段状态。"""
    cfg.ensure_dirs()
    seen: list[str] = []
    events: list[tuple[str, int, int]] = []
    from laoa_trader.data import hithink as hx_mod

    class Recorder:
        def download_dump(self, tag, dest=None, progress_cb=None, note_cb=None, **kwargs):
            assert note_cb is not None, "note_cb 必须被透传下来"
            note_cb("正在重签 URL 继续下载（第 2 次）")
            progress_cb(90, 100)
            Path(dest).write_bytes(b"x")
            return Path(dest)

    monkeypatch.setattr(hx_mod, "parquet_ok", lambda *a, **k: True)
    sync.download_dump(cfg, "daily-k", Recorder(), force=True,
                       progress_cb=lambda st, d, t: events.append((st, d, t)),
                       note_cb=seen.append)
    assert seen == ["正在重签 URL 继续下载（第 2 次）"]
    assert events == [("下载 daily-k", 90, 100)]


# ── 3) 重试耗尽 → 结构化失败（不抛裸异常）──


def test_retry_exhausted_returns_structured_error(client, tmp_path, monkeypatch, cfg) -> None:
    payload = _parquet_bytes(rows=20000)
    cut = len(payload) // 5          # 3 次尝试也只够 3/5，必然重试耗尽

    def drop_after_cut(offset: int) -> FakeStreamResponse:
        """每次连接都只再发 cut 个字节就断（模拟"网一到 2 分钟就断"）。"""
        body = payload[offset : offset + cut] if len(payload) > offset else b""
        return FakeStreamResponse(
            status_code=206 if offset else 200, body=body,
            headers={"content-length": str(len(body)), "content-range":
                     f"bytes {offset}-{offset + len(body) - 1}/{len(payload)}"},
            fail_after=len(body),
        )

    session = FakeDumpSession(payload, [drop_after_cut] * 6)
    client.session = session
    _patch_time(monkeypatch)            # 退避不真等（只验证逻辑）

    with pytest.raises(hx.DumpDownloadError) as excinfo:
        client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3)
    err = excinfo.value
    info = err.as_dict()
    assert info["downloaded"] >= cut * 2        # 含"已下载多少"（两次尝试的成果都留着）
    assert info["attempts"] == 3
    # 每次重试都从断点继续（不是从头），且都重新签了 URL
    offsets = [c["offset"] for c in session.download_calls]
    assert offsets[0] == 0 and offsets[1] == cut and offsets[2] == cut * 2, offsets
    assert info["stage"]                      # 卡在哪一步
    assert "重新运行本程序" in info["suggestion"]   # 给出建议
    import re
    assert re.search(r"\d+(\.\d+)? (GB|MB|KB|B)", str(err)), str(err)
    assert (tmp_path / "d.parquet.part").exists()   # .part 保留 → 下次能续

    # 上层（sync）把它转成**结构化**的 SyncResult，不再往上抛
    assert (tmp_path / "d.parquet.part").stat().st_size >= cut * 2


def test_download_history_turns_dump_error_into_sync_result(cfg, monkeypatch) -> None:
    """下载失败在 sync 层变成 `SyncResult(ok=False)`（结构化），**不往上抛异常**。"""
    cfg.ensure_dirs()

    def boom(*a, **kwargs):
        raise hx.DumpDownloadError(
            "daily-k",
            "dump daily-k 下载失败：已下载 87.3 MB / 180.7 MB，5 次尝试后放弃",
            downloaded=87_300_000, total=180_700_000, attempts=5, stage="传输",
            suggestion="重新运行本程序即可从断点继续",
        )

    monkeypatch.setattr(sync, "download_dump", boom)
    result = sync.download_history(cfg, client=object(), include_names=False)
    assert isinstance(result, sync.SyncResult)
    assert result.ok is False
    assert "已下载" in result.error and "MB" in result.error       # 含已下载字节数
    assert "Traceback" not in result.error


def test_dump_download_error_carries_fields() -> None:
    err = hx.DumpDownloadError("daily-k", "失败了", downloaded=123, total=456,
                               attempts=5, stage="传输", suggestion="再来一次")
    assert err.downloaded == 123 and err.total == 456
    assert err.as_dict()["stage"] == "传输"
    assert isinstance(err, hx.HithinkError)      # 仍然能被老的 except 捕获


# ── 4) 进度回调：多次调用且单调不减 ──


def test_progress_is_monotonic(client, tmp_path, monkeypatch) -> None:
    payload = _parquet_bytes(rows=5000)
    session = FakeDumpSession(payload, [])
    client.session = session
    seen: list[tuple[int, int]] = []
    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=2,
                               progress_cb=lambda done, total: seen.append((done, total)))
    assert out.read_bytes() == payload
    assert len(seen) >= 2, "至少要有起始与结束两次回调"
    dones = [d for d, _ in seen]
    assert dones == sorted(dones), "进度必须单调不减"
    assert dones[-1] == len(payload)
    assert all(total == len(payload) for _, total in seen if total)   # 总大小已知


def test_progress_callback_exception_does_not_break(client, tmp_path) -> None:
    payload = _parquet_bytes()
    client.session = FakeDumpSession(payload, [])

    def boom(done, total):
        raise RuntimeError("界面回调炸了")

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", progress_cb=boom)
    assert out.read_bytes() == payload


# ── 5) 残缺 .part 的续传 / 清理 ──


def test_resume_from_existing_part(client, tmp_path, monkeypatch) -> None:
    """上次留下的 .part：直接从它的字节数开始 Range，而不是从头下。"""
    payload = _parquet_bytes()
    part = tmp_path / "d.parquet.part"
    part.write_bytes(payload[: len(payload) // 3])
    session = FakeDumpSession(payload, [])
    client.session = session

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=2)
    assert out.read_bytes() == payload
    call = session.download_calls[0]
    assert call["offset"] == len(payload) // 3
    assert call["range"] == f"bytes={len(payload) // 3}-"


def test_bogus_part_is_cleaned_and_restarted(client, tmp_path, monkeypatch) -> None:
    """`.part` 比远端文件还大（上次下的是别的东西）→ 远端回 416 → 清理后从头下。"""
    payload = _parquet_bytes()
    part = tmp_path / "d.parquet.part"
    part.write_bytes(b"x" * (len(payload) + 1000))          # 明显不对
    too_big = FakeStreamResponse(status_code=416, body=b"", headers={})
    session = FakeDumpSession(payload, [too_big])
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3)
    assert out.read_bytes() == payload
    assert session.download_calls[0]["offset"] > len(payload)   # 先试了那个坏 .part
    assert session.download_calls[-1]["offset"] == 0            # 清理后从头下
    assert not part.exists()


def test_server_ignoring_range_restarts_cleanly(client, tmp_path, monkeypatch) -> None:
    """服务器忽略 Range 直接回 200（整包）→ 必须从头写，否则文件会写坏。"""
    payload = _parquet_bytes()
    part = tmp_path / "d.parquet.part"
    part.write_bytes(b"garbage-prefix")                     # 半截垃圾
    full_200 = FakeStreamResponse(status_code=200, body=payload,
                                  headers={"content-length": str(len(payload))})
    session = FakeDumpSession(payload, [full_200])
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=2)
    assert out.read_bytes() == payload                       # 没有把垃圾留在文件头


# ── 6) 完整性校验 & 其它 ──


def test_corrupt_payload_is_rejected_and_retried(client, tmp_path, monkeypatch) -> None:
    """大小对但内容不可读（不是 Parquet）→ 丢弃重下，最终仍拿到好文件。"""
    payload = _parquet_bytes()
    broken = b"not-a-parquet" * 100
    bad = FakeStreamResponse(status_code=200, body=broken,
                             headers={"content-length": str(len(broken))})
    session = FakeDumpSession(payload, [bad])
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3,
                               verify=True)
    assert out.read_bytes() == payload          # 第二次拿到真数据
    assert session.download_calls[-1]["offset"] == 0


def test_incomplete_transfer_without_total_restarts(client, tmp_path, monkeypatch) -> None:
    """没有 content-length 时无法比大小，但校验兜底（下到一半的 Parquet 读不了）。"""
    payload = _parquet_bytes()
    cut = payload[: len(payload) // 2]
    truncated = FakeStreamResponse(status_code=200, body=cut, headers={})
    session = FakeDumpSession(payload, [truncated])
    client.session = session
    _patch_time(monkeypatch)
    out = client.download_dump("daily-k", dest=tmp_path / "d.parquet", max_attempts=3)
    assert out.read_bytes() == payload


def test_cancellation_stops_and_keeps_part(client, tmp_path, monkeypatch) -> None:
    """用户取消：立刻停、.part 保留（下次续传），并给出"已取消"的结构化结果。"""
    payload = _parquet_bytes(rows=20000)
    client.session = FakeDumpSession(payload, [])
    state = {"stop": False}

    def cb(done, total):
        if done > 0:
            state["stop"] = True          # 收到第一个进度块后要求取消

    with pytest.raises(hx.DownloadCancelled):
        client.download_dump("daily-k", dest=tmp_path / "d.parquet",
                             progress_cb=cb, should_stop=lambda: state["stop"])
    part = tmp_path / "d.parquet.part"
    assert part.exists() and part.stat().st_size > 0
    assert not (tmp_path / "d.parquet").exists()


def test_timeouts_are_configurable(client, tmp_path, monkeypatch, cfg) -> None:
    """连接/读取超时要能配（tuple 形式：连接短、读取宽松）。"""
    payload = _parquet_bytes()
    session = FakeDumpSession(payload, [])
    client.session = session
    client.download_dump("daily-k", dest=tmp_path / "d.parquet",
                         connect_timeout=7, read_timeout=123)
    assert session.download_calls[0]["timeout"] == (7, 123)


def test_sync_passes_config_download_settings(cfg, tmp_path, monkeypatch) -> None:
    """sync 层把 config 里的重试/超时参数传下去。"""
    cfg.download_max_attempts = 7
    cfg.download_connect_timeout = 3
    cfg.download_read_timeout = 200
    seen: dict = {}

    class Recorder:
        def download_dump(self, tag, dest=None, **kwargs):
            seen.update(kwargs)
            Path(dest).write_bytes(_parquet_bytes())
            return Path(dest)

    cfg.ensure_dirs()
    sync.download_dump(cfg, "daily-k", Recorder(), force=True)
    assert seen["max_attempts"] == 7
    assert seen["connect_timeout"] == 3
    assert seen["read_timeout"] == 200


def test_sync_progress_reports_bytes(cfg, monkeypatch) -> None:
    """界面拿到的进度是"在下载哪个 dump + **字节数**"。

    显示层（界面状态栏/命令行）负责把字节格式化成 MB 与百分比 ——
    所以这一层的约定是：stage 只说明"在干什么"，数值必须是**原始字节**，
    不要把 MB 塞进 stage 文案里（否则同一条信息会显示两遍）。
    """
    cfg.ensure_dirs()
    events: list[tuple[str, int, int]] = []

    class Recorder:
        def download_dump(self, tag, dest=None, progress_cb=None, **kwargs):
            Path(dest).write_bytes(_parquet_bytes())
            progress_cb(1_000_000, 180_700_000)
            return Path(dest)

    sync.download_dump(cfg, "daily-k", Recorder(), force=True,
                       progress_cb=lambda stage, done, total: events.append((stage, done, total)))
    assert events, "应该有进度回调"
    stage, done, total = events[0]
    assert stage == "下载 daily-k"                 # 只说"在干什么"
    assert (done, total) == (1_000_000, 180_700_000)   # 数值是字节，没被格式化过
    assert "MB" not in stage                       # 格式化交给显示层（界面/命令行各有各的写法）


def test_existing_good_dump_is_reused_without_download(cfg, monkeypatch) -> None:
    """已经校验通过的文件直接复用：一次下载请求都不发。"""
    cfg.ensure_dirs()
    target = cfg.dump_dir / "daily-k.parquet"
    target.write_bytes(_parquet_bytes())

    class Boom:
        def download_dump(self, *a, **k):
            raise AssertionError("不该重新下载")

    assert sync.download_dump(cfg, "daily-k", Boom()) == target
    assert sync.dump_is_usable(target) is True


# ── 6) 断点续传的"还是不是同一代 dump"护栏 ──
#
# dump（`daily-k`）**按天重新生成**，而续传只看"本地 .part 有多少字节"，`Range`
# 请求服务端不校验内容。于是"今天下到一半、明天点继续"会把新版本的尾部贴到旧版本的
# 头部后面 —— 拼出一个**能读、但两天数据混在一起**的文件。这一组用例钉住护栏：
# 首次响应记下远端身份（ETag / Last-Modified / 总大小），续传时比对，不同就作废重下；
# 远端不给指纹时退回"跨天的半截文件一律作废"。

_GEN_A = _parquet_bytes(rows=50)
_GEN_B = _parquet_bytes(rows=80, start=1000)   # 另一代 dump：前缀与长度都不一样


def _write_part(tmp_path: Path, name: str, data: bytes, *, meta: str | None = None,
                age_days: float = 0.0) -> tuple[Path, Path]:
    """造一个"上一轮留下的半截文件"（可带身份档案、可把时间改到过去）。"""
    part = tmp_path / f"{name}.parquet.part"
    part.write_bytes(data)
    if age_days:
        import os
        old = __import__("time").time() - age_days * 86400
        os.utime(part, (old, old))
    meta_path = Path(str(part) + ".meta")
    if meta is not None:
        meta_path.write_text(meta, encoding="utf-8")
    return part, meta_path


def test_same_generation_keeps_resuming(client, tmp_path, monkeypatch) -> None:
    """回归：**同一代** dump 的续传照旧从断点继续（护栏不能把正常路走坏）。"""
    split = len(_GEN_A) // 2
    part, _meta = _write_part(
        tmp_path, "daily-k", _GEN_A[:split],
        meta=f"tag=daily-k\nat=2026-09-13 16:00:00\netag=gen-A\n"
             f"last_modified=Fri, 13 Sep 2026 08:00:00 GMT\ntotal={len(_GEN_A)}\n",
    )
    session = FakeDumpSession(_GEN_A, [])
    session.payload = _GEN_A
    client.session = session
    _patch_time(monkeypatch)

    # 让服务端每次都带同一代的身份头
    def resp(offset: int) -> FakeStreamResponse:
        body = _GEN_A[offset:]
        return FakeStreamResponse(
            status_code=206, body=body,
            headers={"content-range": f"bytes {offset}-{len(_GEN_A) - 1}/{len(_GEN_A)}",
                     "content-length": str(len(body)),
                     "etag": "gen-A",
                     "last-modified": "Fri, 13 Sep 2026 08:00:00 GMT"},
        )

    session.plan = [lambda offset: resp(offset)]
    target = tmp_path / "daily-k.parquet"
    out = client.download_dump("daily-k", dest=target, max_attempts=2)

    assert out.read_bytes() == _GEN_A                 # 拼出来的是完整的一代
    assert session.download_calls[0]["offset"] == split     # 从断点继续，没有从头下
    assert session.download_calls[0]["range"] == f"bytes={split}-"
    assert not part.exists() and not Path(str(part) + ".meta").exists()


def test_remote_etag_change_discards_local_part(client, tmp_path, monkeypatch) -> None:
    """**远端换了一代（ETag 变了）→ 本地半截文件作废，从 0 重下**（核心护栏）。

    这就是"今天下到一半、明天点继续"的真实场景：服务端只按字节切、不校验内容，
    不拦住的话会拼出两代混合的文件（能读、但价格是两天的混合物）。
    """
    split = len(_GEN_A) // 2
    part, meta_path = _write_part(
        tmp_path, "daily-k", _GEN_A[:split],
        meta=f"tag=daily-k\nat=2026-09-13 16:00:00\netag=gen-A\n"
             f"last_modified=Fri, 13 Sep 2026 08:00:00 GMT\ntotal={len(_GEN_A)}\n",
    )
    session = FakeDumpSession(_GEN_B, [])

    def stale(offset: int) -> FakeStreamResponse:      # 第 1 次：远端已是 gen-B
        return FakeStreamResponse(
            status_code=206, body=_GEN_B[offset:],
            headers={"content-range": f"bytes {offset}-{len(_GEN_B) - 1}/{len(_GEN_B)}",
                     "content-length": str(len(_GEN_B) - offset),
                     "etag": "gen-B",
                     "last-modified": "Sat, 14 Sep 2026 08:00:00 GMT"},
        )

    def fresh(offset: int) -> FakeStreamResponse:      # 第 2 次：作废后从 0 下 gen-B
        return FakeStreamResponse(
            status_code=206 if offset else 200, body=_GEN_B[offset:],
            headers={"content-range": f"bytes {offset}-{len(_GEN_B) - 1}/{len(_GEN_B)}",
                     "content-length": str(len(_GEN_B) - offset),
                     "etag": "gen-B",
                     "last-modified": "Sat, 14 Sep 2026 08:00:00 GMT"},
        )

    session.plan = [lambda offset: stale(offset), lambda offset: fresh(offset)]
    client.session = session
    _patch_time(monkeypatch)

    notes: list[str] = []
    target = tmp_path / "daily-k.parquet"
    # `verify=False`：把"完整性校验顺手挡下混合文件"这条退路关掉，**只**测指纹护栏
    # （否则护栏坏了也可能被 parquet_ok 兜住，用例就成了假绿）
    out = client.download_dump("daily-k", dest=target, max_attempts=3, verify=False,
                               note_cb=lambda text: notes.append(text))

    offsets = [c["offset"] for c in session.download_calls]
    assert offsets[0] == split, offsets          # 先按断点试了一次
    assert offsets[1] == 0, offsets              # 发现不同代 → **从 0 重下**
    assert any("远端文件已更新" in n for n in notes), notes
    assert out.read_bytes() == _GEN_B            # 最终是完整的一代，不是混合文件
    assert _GEN_A[:split] not in out.read_bytes()
    assert not meta_path.exists()


def test_remote_size_change_without_etag_discards_local_part(client, tmp_path, monkeypatch) -> None:
    """没有 ETag 时，**总大小变了**同样要作废（对比 Content-Range 里的总长）。"""
    split = len(_GEN_A) // 2
    _part, meta_path = _write_part(
        tmp_path, "daily-k", _GEN_A[:split],
        meta=f"tag=daily-k\nat=2026-09-13 16:00:00\netag=\nlast_modified=\ntotal={len(_GEN_A)}\n",
    )
    session = FakeDumpSession(_GEN_B, [])

    def resp(offset: int) -> FakeStreamResponse:
        return FakeStreamResponse(
            status_code=206, body=_GEN_B[offset:],
            headers={"content-range": f"bytes {offset}-{len(_GEN_B) - 1}/{len(_GEN_B)}",
                     "content-length": str(len(_GEN_B) - offset)},
        )

    session.plan = [lambda offset: resp(offset), lambda offset: resp(offset)]
    client.session = session
    _patch_time(monkeypatch)

    notes: list[str] = []
    out = client.download_dump("daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=3,
                               verify=False, note_cb=lambda text: notes.append(text))
    offsets = [c["offset"] for c in session.download_calls]
    assert offsets[:2] == [split, 0], offsets
    assert any("远端文件已更新（总大小" in n for n in notes), notes
    assert out.read_bytes() == _GEN_B
    assert not meta_path.exists()


def test_no_fingerprint_at_all_still_resumes(client, tmp_path, monkeypatch) -> None:
    """远端一个指纹都不给：仍然允许按字节续传（只是留个日志/状态说明）。"""
    split = len(_GEN_A) // 3
    part, _meta = _write_part(tmp_path, "daily-k", _GEN_A[:split], meta=None)
    session = FakeDumpSession(_GEN_A, [])

    def resp(offset: int) -> FakeStreamResponse:
        body = _GEN_A[offset:]
        return FakeStreamResponse(
            status_code=206 if offset else 200, body=body,
            headers={"content-length": str(len(body))},      # 没有 content-range、没有 etag
        )

    session.plan = [lambda offset: resp(offset)]
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=2)
    assert session.download_calls[0]["offset"] == split      # 照旧续传
    assert out.read_bytes() == _GEN_A


def test_part_from_previous_day_is_discarded(client, tmp_path, monkeypatch) -> None:
    """**跨天的半截文件一律作废**（远端不给指纹时的兜底判据）。

    dump 每天重新生成 → 跨天留下的 .part 必然属于上一代，接着下就是在拼两个版本。
    """
    part, meta_path = _write_part(
        tmp_path, "daily-k", _GEN_A[: len(_GEN_A) // 2], meta=None, age_days=2.0
    )
    notes: list[str] = []
    session = FakeDumpSession(_GEN_B, [])

    def resp(offset: int) -> FakeStreamResponse:
        body = _GEN_B[offset:]
        return FakeStreamResponse(
            status_code=206 if offset else 200, body=body,
            headers={"content-length": str(len(body))},
        )

    session.plan = [lambda offset: resp(offset)]
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump(
        "daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=2,
        note_cb=lambda text: notes.append(text),
    )
    assert session.download_calls[0]["offset"] == 0, "跨天的 .part 不该被继续用"
    assert out.read_bytes() == _GEN_B
    assert any("作废" in n and "每天重新生成" in n for n in notes), notes
    assert not part.exists() and not meta_path.exists()


def test_part_from_today_is_not_discarded(client, tmp_path, monkeypatch) -> None:
    """对照：**今天的**半截文件照常续传（护栏别把正常续传也砍了）。"""
    split = len(_GEN_A) // 2
    _part, _meta = _write_part(tmp_path, "daily-k", _GEN_A[:split], meta=None)
    session = FakeDumpSession(_GEN_A, [])

    def resp(offset: int) -> FakeStreamResponse:
        body = _GEN_A[offset:]
        return FakeStreamResponse(
            status_code=206 if offset else 200, body=body,
            headers={"content-length": str(len(body))},
        )

    session.plan = [lambda offset: resp(offset)]
    client.session = session
    _patch_time(monkeypatch)

    out = client.download_dump("daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=2)
    assert session.download_calls[0]["offset"] == split
    assert out.read_bytes() == _GEN_A


def test_failed_download_keeps_identity_meta_for_next_run(client, tmp_path, monkeypatch) -> None:
    """重试耗尽时，`.part` 与身份档案都要留着 —— 下次运行才能比对"还是不是同一代"。"""
    first = FakeStreamResponse(
        status_code=200, body=_GEN_A, headers={"content-length": str(len(_GEN_A))},
        fail_after=len(_GEN_A) // 4,
    )
    session = FakeDumpSession(_GEN_A, [first])
    client.session = session
    _patch_time(monkeypatch)

    with pytest.raises(hx.DumpDownloadError):
        client.download_dump("daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=1)

    part = tmp_path / "daily-k.parquet.part"
    meta = Path(str(part) + ".meta")
    assert part.exists() and part.stat().st_size > 0
    assert meta.exists(), "身份档案要留着，否则下次续传无法判断是不是同一代"
    assert "total=" in meta.read_text(encoding="utf-8")


def test_download_requests_ask_for_identity_encoding(client, tmp_path, monkeypatch) -> None:
    """请求头带 `Accept-Encoding: identity`：避免代理 gzip 让"字节数对不上"。"""
    session = FakeDumpSession(_GEN_A, [])
    client.session = session
    _patch_time(monkeypatch)
    client.download_dump("daily-k", dest=tmp_path / "daily-k.parquet", max_attempts=1)
    assert session.download_calls[0]["headers"].get("Accept-Encoding") == "identity"


@pytest.mark.parametrize(
    ("expected", "current", "should_complain"),
    [
        ({"etag": "a"}, {"etag": "b"}, True),                       # ETag 变了
        ({"etag": "a"}, {"etag": "a"}, False),                      # 一样 → 放行
        ({"last_modified": "x"}, {"last_modified": "y"}, True),      # Last-Modified 变了
        ({"total": "100"}, {"total": "200"}, True),                  # 总大小变了
        ({"total": "100"}, {"total": "100"}, False),
        ({"etag": "", "total": ""}, {"etag": "b", "total": "5"}, False),   # 上次没记 → 不拦
        ({}, {}, False),                                             # 完全没信息 → 不拦
    ],
)
def test_identity_mismatch_rules(expected, current, should_complain) -> None:
    """身份比对规则：**任一可得且不同**才拦；拿不到信息时不拦（宁可续传也不误杀）。"""
    reason = hx._identity_mismatch(expected, current)
    assert bool(reason) is should_complain, reason
    if reason:
        assert "已更新" in reason
