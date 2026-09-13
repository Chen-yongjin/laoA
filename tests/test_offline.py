"""离线保证：本测试套件**绝不能联网**（真实接口 + 配额 + 限流会让结果不稳定）。

为什么要专门写一个测试文件
--------------------------
"记得注入假 client"是靠不住的：一次疏忽就会让整个套件打真实接口。
`conftest.py` 里的 `_block_network`（autouse）在 socket 层封死了 IPv4/IPv6 的
连接与域名解析 —— 这里再验证**这把锁真的锁上了**：

1. TCP 连接、域名解析、`create_connection` 全部被拒；
2. **即使用真实 `HithinkClient`（忘了注入假 session）也打不出去** —— 会抛
   `NetworkBlocked`，而不是静默联网；
3. 静态检查：测试文件里不许出现 `requests.get/post` 这类直连写法。

顺带说明一个容易误会的现象：日志里出现的
`WARNING ... 指数 399001.SZ 取数失败：[5001] 限流` 是 **假客户端合成的错误**
（见 `conftest.FakeClient`），用来验证"接口报错要转成结构化结果"，
并不是真的打到了同花顺 —— 真实请求在本文件面前根本发不出去。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tests.conftest import NetworkBlocked


def test_tcp_connection_is_blocked() -> None:
    """任何 TCP 连接都必须在测试里失败。"""
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("fuyao.aicubes.cn", 443), timeout=1)


def test_raw_socket_connect_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_dns_resolution_is_blocked() -> None:
    """连域名解析也要拦 —— DNS 查询本身就是网络流量（UDP 53）。"""
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("fuyao.aicubes.cn", 443)


def test_localhost_is_still_allowed() -> None:
    """放行本机回环：Qt / D-Bus 之类的本地 IPC 不算"联网"。"""
    assert socket.getaddrinfo("localhost", 80)


def test_real_hithink_client_cannot_escape_the_guard() -> None:
    """**关键一条**：忘了注入假 session 的真实客户端也打不出去。

    如果这条挂了，说明守卫被绕过 —— 那么"测试不联网"就只是一句口号。
    """
    from laoa_trader.data import hithink as hx

    client = hx.HithinkClient(api_key="dummy-key-not-real", retries=0, pace=0)
    with pytest.raises(NetworkBlocked):
        client.trading_days()
    # 顺带确认：真实端点地址确实是外网地址（不是 localhost 之类被放行的特例）
    assert hx.BASE_URL.startswith("https://")
    assert "aicubes.cn" in hx.BASE_URL


def test_real_requests_call_is_blocked() -> None:
    """裸 requests 也必须打不出去（防止有人绕过客户端直接写 HTTP）。"""
    import requests

    with pytest.raises(NetworkBlocked):
        requests.get("https://fuyao.aicubes.cn/api/a-share/calendar/trading-days",
                     timeout=3)


def test_feishu_endpoint_is_blocked_too() -> None:
    """飞书方向同样封死（通知测试必须注入假 session）。"""
    import requests

    with pytest.raises(NetworkBlocked):
        requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": "x", "app_secret": "y"}, timeout=3)


def test_no_test_file_calls_requests_directly() -> None:
    """静态检查：测试文件里不许出现直连写法（防止有人"临时试一下"）。

    允许的只有注入式的假会话（`FakeSession`）与它记录下来的调用。
    """
    tests_dir = Path(__file__).resolve().parent
    forbidden = (
        "requests.get(", "requests.post(", "requests.Session()",
        "urlopen(", "httpx.", "http.client.HTTPConnection(",
    )
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue          # 本文件正在验证"被封死"，会故意尝试连接
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"测试文件里出现了直连网络的写法：{offenders}"
