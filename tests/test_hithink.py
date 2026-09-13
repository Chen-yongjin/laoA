"""同花顺客户端：代码/时间转换、信封校验、错误分类、可重试码、dump 下载。"""

from __future__ import annotations

from pathlib import Path

import pytest

from laoa_trader.data import hithink as hx
from tests.conftest import FakeResponse, FakeSession


# ── 代码转换 ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("sh.600519", "600519.SH"),
        ("SH.600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("000001", "000001.SZ"),
        ("sz.000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688111", "688111.SH"),
        ("830799", "830799.BJ"),
        ("ti.886042", "886042.TI"),
    ],
)
def test_to_thscode(raw: str, expected: str) -> None:
    assert hx.to_thscode(raw) == expected


def test_to_thscode_rejects_garbage() -> None:
    # 注意 "12345" 是**合法**的：会补零成 012345（与服务器版行为一致）
    for bad in ("", "abc", "1234567", "60051X"):
        with pytest.raises(ValueError):
            hx.to_thscode(bad)
    assert hx.to_thscode("12345") == "012345.SH"


def test_to_local_symbol_stock_is_bare_6_digits() -> None:
    assert hx.to_local_symbol("600519.SH") == "600519"
    assert hx.to_local_symbol("830799.BJ") == "830799"


def test_to_local_symbol_index_keeps_prefix() -> None:
    assert hx.to_local_symbol("000300.SH", asset_type="index") == "sh.000300"
    assert hx.to_local_symbol("399006.SZ", asset_type="index") == "sz.399006"
    # 同花顺板块指数用 ti. 前缀，避免与真实指数混淆
    assert hx.to_local_symbol("886042.TI") == "ti.886042"


# ── 时间转换（北京时间零点）──


def test_date_ms_roundtrip() -> None:
    assert hx.ms_to_date(hx.date_to_ms("2026-09-11")) == "2026-09-11"
    # 支持 yyyyMMdd 写法
    assert hx.ms_to_date(hx.date_to_ms("20260911")) == "2026-09-11"
    assert hx.ms_to_date(None) == ""


def test_date_to_ms_is_shanghai_midnight() -> None:
    ms = hx.date_to_ms("2026-01-01")
    # 北京时间零点 = UTC 前一天 16:00
    from datetime import datetime, timezone

    utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert (utc.year, utc.month, utc.day, utc.hour) == (2025, 12, 31, 16)


# ── 凭据 ──


def test_api_key_prefers_env(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "from-env")
    assert hx.api_key() == "from-env"
    assert hx.available() is True


def test_api_key_falls_back_to_config(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    from laoa_trader import config as config_mod

    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    monkeypatch.setattr(config_mod, "_config", cfg)  # cfg.hithink_api_key = "test-key"
    assert hx.api_key() == "test-key"


def test_client_without_key_raises_auth_error(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    from laoa_trader import config as config_mod

    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    cfg.hithink_api_key = ""
    monkeypatch.setattr(config_mod, "_config", cfg)
    with pytest.raises(hx.HithinkAuthError) as excinfo:
        hx.HithinkClient()
    assert excinfo.value.code == 2003


# ── 信封与错误分类 ──


def _client(session: FakeSession, **kwargs) -> hx.HithinkClient:
    return hx.HithinkClient(api_key="k", session=session, retries=0, pace=0, **kwargs)


def test_request_returns_data_on_code_zero() -> None:
    session = FakeSession({"/a-share/calendar/trading-days": FakeResponse(
        {"code": 0, "data": {"item": [{"date": "20260911"}]}})})
    assert _client(session).trading_days() == ["2026-09-11"]


def test_http_200_with_business_error_still_raises() -> None:
    """HTTP 200 也可能是错误（2003 Missing X-api-key）—— 只认信封 code。"""
    session = FakeSession({"/meta/tickers/list": FakeResponse(
        {"code": 2003, "message": "Missing X-api-key"}, status_code=200)})
    with pytest.raises(hx.HithinkAuthError):
        _client(session).tickers()


def test_not_ready_code_maps_to_not_ready_error() -> None:
    session = FakeSession({"/a-share/prices/snapshot": FakeResponse(
        {"code": 4040, "message": "数据尚未就绪"})})
    with pytest.raises(hx.HithinkNotReadyError):
        _client(session).snapshot(["600519"])


def test_retry_codes_are_retried_then_raise_rate_limit() -> None:
    """可重试码 {4001,5001,5002,5003}：有界重试后抛限流错误（而不是无限重试）。"""
    session = FakeSession({"/a-share/prices/snapshot": FakeResponse(
        {"code": 4001, "message": "too many requests"})})
    client = hx.HithinkClient(api_key="k", session=session, retries=2, pace=0)
    with pytest.raises(hx.HithinkRateLimitError):
        client.snapshot(["600519"])
    # retries=2 → 总共 3 次尝试
    assert len([c for c in session.calls if "snapshot" in c[0]]) == 3


def test_snapshot_sends_thscodes_comma_separated() -> None:
    session = FakeSession()
    _client(session).snapshot(["600519", "000001"])
    url, params = session.calls[0]
    assert params["thscodes"] == "600519.SH,000001.SZ"


def test_limit_up_pool_paginates_by_page_size() -> None:
    """涨停池用 page/size 分页：不翻页会漏掉一半涨停股（服务器版踩过的坑）。"""
    pages = {
        1: FakeResponse({"code": 0, "data": {"item": [{"thscode": "600001.SH"}],
                                            "pagination": {"pages": 3}}}),
        2: FakeResponse({"code": 0, "data": {"item": [{"thscode": "600002.SH"}],
                                            "pagination": {"pages": 3}}}),
        3: FakeResponse({"code": 0, "data": {"item": [{"thscode": "600003.SH"}],
                                            "pagination": {"pages": 3}}}),
    }

    def route(params):
        return pages[int(params.get("page", 1))]

    session = FakeSession({"/special-data/limit-up-pool": route})
    rows = _client(session).limit_up_pool("2026-09-11")
    assert [r["thscode"] for r in rows] == ["600001.SH", "600002.SH", "600003.SH"]


def test_download_dump_uses_presigned_url_and_atomic_replace(tmp_path: Path) -> None:
    """dump 下载：先落 .part 再原子替换（半截文件不能骗过续传判断）。"""
    target = tmp_path / "daily-k.parquet"

    class StreamResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def iter_content(self, chunk_size: int):
            yield b"PAR1"
            yield b"DATA"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    session = FakeSession({
        "/dump/market-dumps/daily-k/download-url": FakeResponse(
            {"code": 0, "data": {"presigned_url": "https://example.com/x.parquet"}}),
        "example.com": StreamResponse(),
    })
    path = _client(session).download_dump("daily-k", dest=target)
    assert path == target
    assert target.read_bytes() == b"PAR1DATA"
    assert not (tmp_path / "daily-k.parquet.part").exists()


def test_download_dump_requires_presigned_url() -> None:
    session = FakeSession({"/download-url": FakeResponse({"code": 0, "data": {}})})
    with pytest.raises(hx.HithinkError):
        _client(session).download_dump("daily-k", dest=Path("/tmp/x.parquet"))


def test_dump_is_usable_rejects_truncated_file(tmp_path: Path) -> None:
    """残缺文件必须判为不可用 —— 否则"看起来成功、其实只有一半股票"。"""
    from laoa_trader.data import sync

    bad = tmp_path / "bad.parquet"
    bad.write_text("not a parquet")
    assert sync.dump_is_usable(bad) is False
    assert sync.dump_is_usable(tmp_path / "missing.parquet") is False
