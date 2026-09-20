"""中文语音朗读（`notify/voice.py`）的测试。

用户 2026-09-18 的原话
----------------------
> 「可不可以编写个机器人，直接中文语音提醒……有消息时大声喊出消息内容。」

这一块最容易做错的是**环境相关**的部分（有没有 Windows、有没有中文音色、
是不是正在静音），而那些恰恰在这台开发机（Linux）上跑不出来。所以这里的做法是：

* **绝不真的发声**：真正起进程那一步是 `voice.run_command`，用例把它换掉；
  音色名单是 `voice._list_voices_raw`，用例直接喂一个假的名单。
  这样"有没有中文音色""静音期间念不念"这些判断全都能确定性地验。
* 钉住三条硬口径：**没有中文音色就不念**（英文音色念中文是怪腔怪调）、
  **念之前先清洗**（markdown 星号/emoji/URL 念出来是噪音）、
  **静音只影响朗读**（气泡与消息列表照常 —— 那是桌宠与消息窗口的事）。
"""

from __future__ import annotations

import pytest

from laoa_trader.config import Config
from laoa_trader.notify import voice


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    """每个用例都从"干净的语音状态"开始：不动真的系统语音，也不留缓存/静音。"""
    voice.reset_cache()
    voice.unmute()
    monkeypatch.setattr(voice, "_prompted_missing_voice", False, raising=False)
    # 清空队列：上一个用例排队的残留不该影响下一个
    while not voice._queue.empty():
        voice._queue.get_nowait()
    yield
    voice.reset_cache()
    voice.unmute()


@pytest.fixture()
def cfg(tmp_path) -> Config:
    config = Config(data_dir=tmp_path / "data")
    config.ensure_dirs()
    return config


def _fake_voices(monkeypatch: pytest.MonkeyPatch, names: list[tuple[str, str]]) -> None:
    """给一份假的音色名单（不启动任何进程）。"""
    monkeypatch.setattr(voice, "_list_voices_raw", lambda: list(names))
    voice.reset_cache()


def _record(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """把"真正发声"换成记录命令，返回那个列表。"""
    spoken: list[list[str]] = []
    monkeypatch.setattr(voice, "run_command", lambda command: spoken.append(list(command)))
    return spoken


# ══════════════════════════════════════════════════════════════════════════
# 1) 文本清洗：念出来要像人话
# ══════════════════════════════════════════════════════════════════════════


def test_sanitize_strips_marks_emoji_urls_and_keeps_the_words() -> None:
    """markdown 星号、emoji、URL、竖线都不能念出来，正文一个字不丢。"""
    raw = "🛑 触及止损 ｜ 贵州茅台(600519) 现价 1234.56 **已跌破** https://x/y?s=1"

    out = voice.sanitize(raw)

    assert "星号" not in out and "*" not in out
    assert "http" not in out and "🛑" not in out
    assert "｜" not in out                     # 竖线换成逗号（念出来就是停顿）
    for keep in ("触及止损", "贵州茅台(600519)", "现价 1234.56", "已跌破"):
        assert keep in out


def test_compose_puts_target_first_then_kind_then_numbers() -> None:
    """念的顺序按"听"的习惯：先说谁、再说发生什么、数字放最后。"""
    text = voice.compose("贵州茅台(600519)", "止损提醒", "已跌破成本价", "1234.56")

    assert text.startswith("贵州茅台(600519)")
    assert text.index("止损提醒") < text.index("1234.56")
    assert "已跌破成本价" in text


def test_compose_works_without_price_or_detail() -> None:
    """只有标的与类型时也得是一句能念的话（没有数字就不写数字）。"""
    assert voice.compose("甲样本(600001)", "放量突破") == "甲样本(600001)，放量突破"


# ══════════════════════════════════════════════════════════════════════════
# 2) PowerShell 命令：不弹黑窗、引号不出错
# ══════════════════════════════════════════════════════════════════════════


def test_speak_command_selects_voice_sets_volume_and_rate(monkeypatch) -> None:
    """命令里必须带上音色、音量、语速与要念的文本（一次进程说完一句）。"""
    _fake_voices(monkeypatch, [("Microsoft Huihui Desktop", "zh-CN")])

    command = voice._speak_command("贵州茅台，止损提醒", voice="Microsoft Huihui Desktop",
                                   volume=0.5, rate=2)

    joined = " ".join(command)
    assert command[0].lower().endswith((".exe", "powershell", "pwsh")) or "powershell" in command[0]
    assert "System.Speech" in joined
    assert "SelectVoice('Microsoft Huihui Desktop')" in joined
    assert "$s.Volume = 50" in joined
    assert "$s.Rate = 2" in joined
    assert "$s.Speak('贵州茅台，止损提醒')" in joined
    # 音量/语速越界要夹住（SAPI 的量纲：音量 0~100、语速 -10~10）
    clipped = " ".join(voice._speak_command("x", voice="v", volume=3.0, rate=99))
    assert "$s.Volume = 100" in clipped and "$s.Rate = 10" in clipped


def test_speak_command_escapes_single_quotes(monkeypatch) -> None:
    """股票名里带个单引号不能把 PowerShell 命令拼坏（`'` → `''`）。"""
    _fake_voices(monkeypatch, [("V", "zh-CN")])

    joined = " ".join(voice._speak_command("甲'乙"))

    assert "$s.Speak('甲''乙')" in joined


# ══════════════════════════════════════════════════════════════════════════
# 3) 音色挑选：没有中文音色就不念
# ══════════════════════════════════════════════════════════════════════════


def test_prefers_simplified_chinese_voice(monkeypatch) -> None:
    """zh-CN 优先，其次任何 zh*（zh-TW 念简体也比英文音色强得多）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("English David", "en-US"), ("Chinese Taiwan", "zh-TW"),
                               ("Chinese Huihui", "zh-CN")])
    assert voice.voice_name() == "Chinese Huihui"

    voice.reset_cache()
    _fake_voices(monkeypatch, [("English David", "en-US"), ("Chinese Taiwan", "zh-TW")])
    assert voice.voice_name() == "Chinese Taiwan"


def test_no_chinese_voice_means_silence(monkeypatch, cfg) -> None:
    """只有英文音色 → **不念**（怪腔怪调比不念更糟），而且不报错、不阻塞别的功能。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("English David", "en-US")])
    spoken = _record(monkeypatch)

    assert voice.voice_name() is None
    assert voice.can_speak(cfg=cfg) is False
    assert voice.speak("贵州茅台，止损提醒", cfg=cfg) is False
    assert voice.speak_now("贵州茅台，止损提醒", cfg=cfg) is False
    assert spoken == []                       # 一条命令都没起


def test_voice_switch_off_means_silence(monkeypatch, cfg) -> None:
    """设置里关掉朗读 → 不念（哪怕机器上有中文音色）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN")])
    spoken = _record(monkeypatch)
    cfg.notify_voice = False

    assert voice.can_speak(cfg=cfg) is False
    assert voice.speak_now("你好", cfg=cfg) is False
    assert spoken == []


def test_non_windows_is_silent(monkeypatch, cfg) -> None:
    """非 Windows（开发机/CI）：`available()` 为假 → 什么都不做。"""
    monkeypatch.setattr(voice, "available", lambda: False)
    spoken = _record(monkeypatch)

    assert voice.speak("你好", cfg=cfg) is False
    assert voice.speak_now("你好", cfg=cfg) is False
    assert spoken == []


# ══════════════════════════════════════════════════════════════════════════
# 4) 静音：只影响朗读
# ══════════════════════════════════════════════════════════════════════════


def test_mute_blocks_speaking_until_it_expires(monkeypatch, cfg) -> None:
    """静音期间不出声；取消静音（或到点）之后又能念。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN")])
    spoken = _record(monkeypatch)

    voice.mute_for(3600)
    assert voice.muted() is True
    assert voice.mute_remaining() > 3500
    assert voice.speak("你好", cfg=cfg) is False
    assert spoken == []

    voice.unmute()
    assert voice.muted() is False
    assert voice.speak_now("你好", cfg=cfg) is True
    assert len(spoken) == 1


def test_forced_speak_ignores_mute(monkeypatch, cfg) -> None:
    """【试喊一条】是用户主动点的 → 静音期间也照念（静音是"别打扰"，不是"点了也不许响"）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN")])
    spoken = _record(monkeypatch)
    voice.mute_for(3600)

    assert voice.can_speak(cfg=cfg, force=True) is True
    assert voice.speak_now("试喊一条", cfg=cfg, force=True) is True
    assert len(spoken) == 1


# ══════════════════════════════════════════════════════════════════════════
# 5) 队列：排队不重叠、只念最新
# ══════════════════════════════════════════════════════════════════════════


def test_speak_queues_and_never_blocks(monkeypatch, cfg) -> None:
    """`speak()` 只排队、立刻返回（界面绝不阻塞）；真正念由后台线程做。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN")])
    # 把"起后台线程"也替掉：这里只验"排队成功、内容对"
    monkeypatch.setattr(voice, "_ensure_worker", lambda: None)

    assert voice.speak("第一条", cfg=cfg) is True
    assert voice.speak("第二条", cfg=cfg) is True
    assert voice._queue.qsize() == 2

    while not voice._queue.empty():
        voice._queue.get_nowait()


def test_queue_full_drops_the_oldest(monkeypatch, cfg) -> None:
    """队满时丢掉**最旧**的一条：新消息永远比旧消息值得念。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN")])
    monkeypatch.setattr(voice, "_ensure_worker", lambda: None)

    for index in range(voice.MAX_QUEUE):
        assert voice.speak(f"第{index}条", cfg=cfg) is True
    assert voice.speak("最新的那条", cfg=cfg) is True

    left = [voice._queue.get_nowait() for _ in range(voice._queue.qsize())]
    assert "最新的那条" in left
    assert "第0条" not in left                 # 最旧的那条被挤掉了
