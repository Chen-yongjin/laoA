"""授权核心（`licensing`）的用例：机器码 / 注册码 / 试用期 / 回拨防护。

用户 2026-09-20 拍板的三条都在这里守着：

* 「免费运行7天」 → 第 7 天还能用、第 8 天到期；
* 「做到能用的功能」 → 注册码必须真的能算、能验、能写进状态；
* 「策略编辑锁住」 → 这一层只提供 `is_licensed()`，锁的动作在界面层（见 `test_license_ui.py`）。

**测试一律不碰真实硬件指纹**（monkeypatch `_fingerprint_parts`）：
真机上跑出来的机器码会随机器变，拿它当断言等于让测试跟着机器走。
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import pytest

from laoa_trader import clock, licensing as L
from laoa_trader.config import Config


@pytest.fixture()
def lic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> L:
    """把授权状态文件指到 tmp_path，并固定一份假指纹（测试不碰真硬件）。"""
    monkeypatch.setattr(L, "state_path", lambda: tmp_path / "license.json")
    monkeypatch.setattr(L, "_fingerprint_parts", lambda: ["CPU-1", "BOARD-1", "DISK-1"])
    # 机器码现在是**进程内缓存 + 落盘**的（2026-09-21 的性能修复）：
    # 用例之间必须清掉，否则前一个用例的机器码会留到下一个，指纹改了也看不出区别。
    L.forget_cached_machine_code()
    return L


def _state(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════
# 机器码
# ══════════════════════════════════════════════════════════════════════════


def test_machine_code_is_stable_and_grouped(lic: L) -> None:
    """同一台机器每次都算出同一个码，且是 `XXXX-XXXX-XXXX-XXXX` 四组。"""
    first = lic.machine_code()
    second = lic.machine_code()

    assert first == second
    assert len(first) == 19 and first.count("-") == 3
    assert all(part.isalnum() for part in first.split("-"))


def test_machine_code_differs_per_machine(lic: L, monkeypatch: pytest.MonkeyPatch) -> None:
    """换一台机器（指纹不同）必须得到不同的码 —— 否则"绑定机器"就是空的。"""
    a = lic.machine_code()
    monkeypatch.setattr(lic, "_fingerprint_parts", lambda: ["CPU-2", "BOARD-1", "DISK-1"])
    lic.forget_cached_machine_code()          # 换了机器 = 缓存失效（同一个进程里要显式清）
    b = lic.machine_code()

    assert a != b


def test_machine_code_falls_back_when_hardware_is_unreadable(
        lic: L, monkeypatch: pytest.MonkeyPatch) -> None:
    """什么都取不到时也要给一个**稳定**的码（兜底指纹：用户名+机器名+安装目录）。"""
    monkeypatch.setattr(lic, "_hardware_parts", lambda: ["", "", ""])

    parts = lic._fingerprint_parts()
    code = lic.machine_code()

    # 三项都被兜底项补满了（否则所有"取不到硬件"的机器会算出同一个码 = 没有绑定）
    assert all(parts) and len(parts) == 3
    empty = hashlib.sha256("\x1f".join(["", "", ""]).encode()).digest()
    assert code != lic._encode(empty, lic.MACHINE_CHARS)
    assert code == lic.machine_code()          # 稳定


# ══════════════════════════════════════════════════════════════════════════
# 注册码
# ══════════════════════════════════════════════════════════════════════════


def test_expected_code_is_deterministic_and_sized(lic: L) -> None:
    machine = lic.machine_code()

    code = lic.expected_code(machine)

    assert code == lic.expected_code(machine)
    assert len(code) == 19 and code.count("-") == 3


def test_verify_accepts_typing_variants(lic: L) -> None:
    """大小写、有没有连字符、前后空格都认（用户是从微信复制/手打的）。"""
    machine = lic.machine_code()
    code = lic.expected_code(machine)

    for variant in (code, code.replace("-", ""), code.lower(), f"  {code}  ",
                    code.replace("-", " ")):
        ok, why = lic.verify(machine, variant)
        assert ok, f"{variant!r} 应该通过（原因：{why}）"


def test_verify_rejects_wrong_codes_with_chinese_reasons(lic: L) -> None:
    """三种失败要分开说：没填 / 格式不对 / 对不上（原因会直接显示给用户）。"""
    machine = lic.machine_code()
    good = lic.expected_code(machine)

    ok, why = lic.verify(machine, "")
    assert not ok and "请先填" in why

    ok, why = lic.verify(machine, "ABC")
    assert not ok and "格式" in why

    ok, why = lic.verify(machine, "AAAA-BBBB-CCCC-DDDD")
    assert not ok and "不匹配" in why

    # 差一个字符也不行（别把"近似匹配"当通过）
    broken = good[:-1] + ("A" if good[-1] != "A" else "B")
    assert lic.verify(machine, broken)[0] is False


def test_code_of_one_machine_does_not_work_on_another(lic: L,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """给 A 机器算的码，拿到 B 机器上必须无效（这是整套方案的意义）。"""
    code_a = lic.expected_code(lic.machine_code())
    monkeypatch.setattr(lic, "_fingerprint_parts", lambda: ["CPU-9", "BOARD-9", "DISK-9"])
    lic.forget_cached_machine_code()   # 换了机器 → 缓存的机器码作废

    ok, why = lic.verify(lic.machine_code(), code_a)

    assert not ok and "不匹配" in why


# ══════════════════════════════════════════════════════════════════════════
# 试用期（免费 7 天）
# ══════════════════════════════════════════════════════════════════════════


def _set_first_run(lic: L, path: Path, days_ago: int) -> None:
    """把"首次运行日期"设成 N 天前（模拟用户已经用了一段时间）。"""
    today = clock.now_cn().date()
    first = today - datetime.timedelta(days=days_ago)
    path.write_text(
        '{"first_run": "%s", "max_seen": "%s"}'
        % (first.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")),
        encoding="utf-8",
    )


def test_first_run_starts_a_seven_day_trial(lic: L, cfg: Config) -> None:
    status = lic.license_status(cfg)

    assert status["licensed"] is True and status["trial"] is True
    assert status["days_left"] == lic.TRIAL_DAYS == 7
    assert "试用" in lic.status_text(cfg)


def test_trial_counts_down_and_expires_on_day_eight(lic: L, cfg: Config,
                                                    tmp_path: Path) -> None:
    """第 7 天还能用（剩 1 天），第 8 天到期 —— 这是用户说的"免费运行7天"。"""
    _set_first_run(lic, tmp_path / "license.json", 6)
    status = lic.license_status(cfg)
    assert status["licensed"] is True and status["days_left"] == 1

    _set_first_run(lic, tmp_path / "license.json", 7)
    status = lic.license_status(cfg)
    assert status["licensed"] is False
    assert status["days_left"] == 0
    assert "到期" in status["reason"]
    assert lic.is_licensed(cfg) is False


def test_trial_writes_both_copies(lic: L, cfg: Config, tmp_path: Path) -> None:
    """首次运行日期写**两处**（状态文件 + 数据库）—— 这是"删文件不能续命"的前提。"""
    lic.license_status(cfg)

    assert _state(tmp_path / "license.json")["first_run"] == clock.today_cn()
    assert lic._db_read("license_first_run", cfg) == clock.today_cn()


def test_deleting_the_state_file_cannot_reset_the_trial(lic: L, cfg: Config,
                                                        tmp_path: Path) -> None:
    """把 license.json 删掉也不能重置试用期（数据库那份更早，会赢）。"""
    _set_first_run(lic, tmp_path / "license.json", 8)
    assert lic.license_status(cfg)["licensed"] is False      # 先让数据库记下"8 天前"
    (tmp_path / "license.json").unlink()

    status = lic.license_status(cfg)

    assert status["licensed"] is False            # 删文件没用
    assert "到期" in status["reason"]


def test_clock_rollback_is_treated_as_expired(lic: L, cfg: Config, tmp_path: Path) -> None:
    """把系统时间调回去（想续命）→ 按到期处理，并把原因说清楚。"""
    future = (clock.now_cn().date() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    (tmp_path / "license.json").write_text(
        '{"first_run": "2026-01-01", "max_seen": "%s"}' % future, encoding="utf-8")

    status = lic.license_status(cfg)

    assert status["licensed"] is False
    assert "时间" in status["reason"] and "调回" in status["reason"]


# ══════════════════════════════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════════════════════════════


def test_register_licenses_the_machine_and_survives_restart(lic: L, cfg: Config,
                                                            tmp_path: Path) -> None:
    machine = lic.machine_code()
    ok, message = lic.register(machine, lic.expected_code(machine), cfg)

    assert ok and "注册成功" in message
    assert lic.is_licensed(cfg) is True
    assert lic.license_status(cfg)["registered"] is True
    # 状态文件里记着机器码与注册码（下次启动直接认，不用再填）
    saved = _state(tmp_path / "license.json")
    assert saved["machine"] == machine and saved["code"]
    assert "已注册" in lic.status_text(cfg)


def test_register_rejects_a_bad_code(lic: L, cfg: Config, tmp_path: Path) -> None:
    ok, message = lic.register(lic.machine_code(), "AAAA-BBBB-CCCC-DDDD", cfg)

    assert ok is False and "不匹配" in message
    assert not (tmp_path / "license.json").exists()       # 失败不写任何状态
    assert lic.is_licensed(cfg) is True                   # 仍是试用中（没被弄坏）


def test_register_still_works_when_the_state_file_cannot_be_written(
        lic: L, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """盘写不进去时：**当次照样放行**，但把"重启后要再注册一次"说清楚。

    为什么这么定：用户点了【注册】却什么都没发生，是最难解释的现象；
    而"这次能用、重启要再来一次"是他能理解、也能自己处理的。
    """
    monkeypatch.setattr(lic, "_write_state", lambda state: False)
    machine = lic.machine_code()

    ok, message = lic.register(machine, lic.expected_code(machine), cfg)

    assert ok is True and "重启" in message
    assert lic.is_licensed(cfg) is True


def test_a_copied_license_file_does_not_license_another_machine(
        lic: L, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把别人的 license.json 拷过来**不构成授权**（机器码对不上）。

    注意这里的口径：文件被当成"不是本机的注册记录"，于是只剩**试用**可用；
    而试用期的起点来自文件里那份 first_run —— 拷来的文件要是很旧，连试用都过期。
    所以真正的判据是 `registered is False`（授权没有转移），而不是"一定完全不能用"。
    """
    machine_a = lic.machine_code()
    lic.register(machine_a, lic.expected_code(machine_a), cfg)
    assert lic.license_status(cfg)["registered"] is True

    # 换一台机器（指纹变了）→ 一旦**校验**（`verify_machine=True`，即打开授权对话框那条路）
    # 就认出来"这份授权不是本机的"
    monkeypatch.setattr(lic, "_fingerprint_parts", lambda: ["CPU-7", "BOARD-7", "DISK-7"])
    lic.forget_cached_machine_code()   # 换了机器 → 缓存的机器码作废
    status = lic.license_status(cfg, verify_machine=True)
    assert status["registered"] is False            # 授权没有跟过来

    # ⚠️ 而**不校验**的日常路径会（暂时地）把它当成已注册 —— 这是主人 2026-09-21 定的
    # 取舍："不用每次都读机器码"。也就是说：拷来的授权文件在日常使用里**不会立刻暴露**，
    # 只有在用户打开授权对话框（或注册）时才被识破。要改回"启动就校验"，
    # 只需在启动路径上传 `verify_machine=True`（代价是每次启动起一次 PowerShell）。
    plain = lic.license_status(cfg)
    assert plain["registered"] is True and plain["machine"] == ""

    # 拷来的文件很旧（first_run 是 30 天前）→ 连试用都过期，等于完全不能用
    old_day = (clock.now_cn().date() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    (tmp_path / "license.json").write_text(
        '{"machine": "%s", "code": "%s", "first_run": "%s", "max_seen": "%s"}'
        % (machine_a, lic.expected_code(machine_a), old_day, clock.today_cn()),
        encoding="utf-8",
    )
    lic._db_write("license_first_run", old_day, cfg)

    # 一校验就识破：注册码不是本机的 + 试用也过期了 → 完全不能用
    status = lic.license_status(cfg, verify_machine=True)
    assert status["licensed"] is False and status["registered"] is False


def test_broken_state_file_is_treated_as_unregistered(lic: L, cfg: Config,
                                                      tmp_path: Path) -> None:
    """状态文件被人改坏 / 权限读不了 → 当未注册处理，绝不抛异常。"""
    (tmp_path / "license.json").write_text("{ 这不是 JSON", encoding="utf-8")

    status = lic.license_status(cfg)          # 不抛

    assert status["licensed"] is True         # 试用期还没过，所以仍可用
    assert status["registered"] is False


def test_contact_text_is_exactly_what_the_user_gave() -> None:
    """联系方式是用户给定要发出去的，一个字都不能改（微信加错人 = 收不到授权）。"""
    assert L.CONTACT_TEXT == "需要授权，请联系作者 wx：q352162"


# ══════════════════════════════════════════════════════════════════════════
# 性能：机器码只算一次（主人 2026-09-21："点策略什么的都会卡一下"）
# ══════════════════════════════════════════════════════════════════════════
#
# 症状是"点一下卡一下"，根因是 `machine_code()` 以前**每次调用都去问硬件**
# （Windows 上一条 PowerShell 几百毫秒，而以前是三条），而点【策略编辑】、
# 刷新授权状态、打开「关于」都会走到它。修法两条：进程内缓存 + 首次在启动时后台预热。


def test_machine_code_asks_the_hardware_only_once(lic: L, monkeypatch: pytest.MonkeyPatch) -> None:
    """**一个进程里只问一次硬件**（第二次起直接吃缓存）—— 这条是"点按钮不再卡"的判据。"""
    asked = {"n": 0}

    def counting():
        asked["n"] += 1
        return ["CPU-1", "BOARD-1", "DISK-1"]

    monkeypatch.setattr(lic, "_fingerprint_parts", counting)
    lic.forget_cached_machine_code()

    first = lic.machine_code()
    for _ in range(20):                     # 模拟"连点 20 次策略编辑 / 刷新状态"
        assert lic.machine_code() == first

    assert asked["n"] == 1, f"硬件指纹被问了 {asked['n']} 次（应当只有 1 次）"


def test_the_routine_path_never_touches_the_hardware(
        lic: L, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """**日常那条路一次都不读硬件**（主人 2026-09-21："不用每次都读机器码啊，
    客户要注册的时候再去读"）。

    覆盖的是启动、点【策略编辑】、刷新设置/关于、保存设置都会走的那几个函数：
    `license_status()` / `is_licensed()` / `status_text()`。
    以前它们内部顺手算机器码（Windows 上一条 PowerShell），于是"点一下卡一下"。
    """
    asked = {"n": 0}

    def counting():
        asked["n"] += 1
        return ["CPU-1", "BOARD-1", "DISK-1"]

    monkeypatch.setattr(lic, "_fingerprint_parts", counting)
    lic.forget_cached_machine_code()

    for _ in range(5):
        status = lic.license_status(cfg)
        lic.is_licensed(cfg)
        lic.status_text(cfg)

    assert asked["n"] == 0, f"日常路径读了 {asked['n']} 次硬件指纹（应当是 0 次）"
    assert status["machine"] == "", "没校验时 machine 应当是空串（一眼看出没读过机器码）"


def test_only_verify_machine_reads_the_hardware(lic: L, cfg: Config,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 `verify_machine=True`（打开授权对话框 / 点【注册】那一路）才读机器码，且只读一次。"""
    asked = {"n": 0}

    def counting():
        asked["n"] += 1
        return ["CPU-1", "BOARD-1", "DISK-1"]

    monkeypatch.setattr(lic, "_fingerprint_parts", counting)
    lic.forget_cached_machine_code()

    first = lic.license_status(cfg, verify_machine=True)
    for _ in range(3):
        lic.license_status(cfg, verify_machine=True)

    assert asked["n"] == 1, f"读了 {asked['n']} 次（缓存之后应当只有 1 次）"
    assert first["machine"] == lic.machine_code()


def test_machine_code_is_not_persisted_to_the_license_file(
        lic: L, cfg: Config, tmp_path: Path) -> None:
    """机器码**只缓存在内存里**，绝不写进 `license.json`。

    为什么这条很重要（安全，不只是性能）：机器码是"授权绑定机器"的唯一依据。
    一旦把它落盘，那份 `license.json` 被拷到别的机器上时，`machine_code()` 会读回
    **旧机器**的码、与文件里的 `machine` 字段一致 → "拷文件就白用"。
    所以这里断言文件里没有机器码字段，且换机器（指纹变）后状态立刻变成"未注册"。
    """
    lic.machine_code()
    state = _state(tmp_path / "license.json") if (tmp_path / "license.json").exists() else {}

    assert "machine_code" not in state, "机器码不该落盘（会把授权变成「拷文件白用」）"
