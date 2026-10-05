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


def _fake_voices(monkeypatch: pytest.MonkeyPatch,
                 names: list[tuple]) -> None:
    """给一份假的音色名单（不启动任何进程）。

    每一项可以写 `(名字, 区域)` 或 `(名字, 区域, 性别)` —— 两元的自动补成"性别未知"
    （`""`），这样"这台机器报得出性别"与"报不出性别"两种情况都能一句话造出来。
    """
    normalized = [(v[0], v[1], v[2] if len(v) > 2 else "") for v in names]
    monkeypatch.setattr(voice, "_list_voices_raw", lambda: list(normalized))
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

    # 队列里存的是**一条完整参数**（文本 + 音色/音量/语速），所以取出来看 text
    left = [item["text"] for item in
            (voice._queue.get_nowait() for _ in range(voice._queue.qsize()))]
    assert "最新的那条" in left
    assert "第0条" not in left                 # 最旧的那条被挤掉了
    # 队满之后塞回去的仍然是**同一份参数**（早先这里塞的是纯文本，音色/音量/语速就丢了）
    assert all(isinstance(item, dict) for item in
               [voice._queue.get_nowait() for _ in range(voice._queue.qsize())])


# ══════════════════════════════════════════════════════════════════════════
# 数字逐位（用户 2026-09-18："播报代码可以设置成一个一个读数字吗？
# 现在直接是 6 万零 5 百一十九"）
# ══════════════════════════════════════════════════════════════════════════


def test_only_stock_codes_are_spelled_digit_by_digit() -> None:
    """**只有股票代码**逐位念；价格与各种"数量"一律整读。

    口径改过一次，两个日期都留着：2026-09-18 先把"代码 + 价格"都逐位（当时用户说
    "6 万零 5 百一十九"听着不对）；2026-09-21 用户反馈"代码播报正常了，但是价格却也
    变成了逐字播报了" —— 价格一位一位念听着累，所以价格回到整读。
    """
    # 代码：逐位（这是这次唯一要逐位的东西）
    assert voice.digits_for_speech("贵州茅台 600519") == "贵州茅台 六零零五一九"
    assert voice.digits_for_speech("宁德时代 300750") == "宁德时代 三零零七五零"
    # 价格：整读（带小数、带负号、带"元/现价"都一样）
    assert voice.digits_for_speech("现价 1234.56") == "现价 1234.56"
    assert voice.digits_for_speech("跌到 -3.2") == "跌到 -3.2"
    assert voice.digits_for_speech("价格 8.06 元") == "价格 8.06 元"
    # 成交量 / 家数 / 天数 / 百分号 / 倍数：整读（"五百零七家"比"五零七家"顺耳）
    assert voice.digits_for_speech("成交 500万股") == "成交 500万股"
    assert voice.digits_for_speech("共 37 家涨停") == "共 37 家涨停"
    assert voice.digits_for_speech("连板 2 天") == "连板 2 天"
    assert voice.digits_for_speech("涨跌幅 3.21%") == "涨跌幅 3.21%"
    assert voice.digits_for_speech("量比 2.5倍") == "量比 2.5倍"
    # 短整数（1~5 位）不动；纯整数 6 位及以上按代码逐位
    assert voice.digits_for_speech("池子 12 只") == "池子 12 只"
    assert voice.digits_for_speech("成交额 1234567 元") == "成交额 一二三四五六七 元"


def test_a_sentence_keeps_code_spelled_and_price_intact() -> None:
    """同一句里两件事都要对：代码逐位、价格原样（这是主人实报的那一句）。"""
    assert voice.digits_for_speech("贵州茅台 600519 现价 1234.56") == \
        "贵州茅台 六零零五一九 现价 1234.56"


def test_the_reading_rule_has_no_switch(cfg) -> None:
    """读法是**固定的、没有开关**（主人 2026-09-21："价格逐位不需要有选项，直接按我说的做就行了"）。

    以前有个 `notify_voice_digits` 勾选框能整体关掉逐位；现在：
    * 配置项与设置页勾选框都已删除（老配置里还写着这个键也不会报错）；
    * `prepare()` 一律按规则处理 —— 代码逐位、价格整读、数量整读。
    """
    assert not hasattr(cfg, "notify_voice_digits"), "配置项应当已经删掉"
    # 就算有人往配置对象上硬塞这个属性（老配置/外部脚本），读法也不受影响
    cfg.notify_voice_digits = False
    assert voice.prepare("贵州茅台 600519，现价 1234.56", cfg) == \
        "贵州茅台 六零零五一九，现价 1234.56"


def test_speech_path_applies_digit_spelling(monkeypatch, cfg) -> None:
    """真正念的那条路上（`speak` 与 `speak_now` 都要）代码已经是逐位形式。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Huihui", "zh-CN", "female")])
    spoken: list[list[str]] = []
    monkeypatch.setattr(voice, "run_command", spoken.append)

    assert voice.speak_now("贵州茅台 600519 触及止损", cfg=cfg, force=True) is True
    assert "$s.Speak('贵州茅台 六零零五一九 触及止损')" in " ".join(spoken[0])

    monkeypatch.setattr(voice, "_ensure_worker", lambda: None)
    voice.speak("宁德时代 300750 现价 200.5", cfg=cfg)
    item = voice._queue.get_nowait()
    # 代码逐位、价格原样（2026-09-21 起价格回到整读）
    assert item["text"] == "宁德时代 三零零七五零 现价 200.5"


# ══════════════════════════════════════════════════════════════════════════
# 语速：倍率 1 = 正常（用户 2026-09-18："语速默认改成 1 正常点"）
# ══════════════════════════════════════════════════════════════════════════


def test_rate_multiplier_maps_to_sapi_rate() -> None:
    """倍率 ↔ SAPI Rate 的换算：1.0 → 0（正常），两端贴近但不贴边。"""
    assert voice.rate_to_sapi(1.0) == 0
    assert voice.rate_to_sapi(2.0) == 9
    assert voice.rate_to_sapi(1.5) == 5
    assert voice.rate_to_sapi(1.2) == 2
    assert voice.rate_to_sapi(0.8) == -3
    assert voice.rate_to_sapi(0.5) == -9
    # 越界夹到区间；乱码按正常
    assert voice.rate_to_sapi(9) == 9 and voice.rate_to_sapi(-9) == -9
    assert voice.rate_to_sapi("快") == 0
    # 反算（老配置用）能回到同一个量级
    for rate in (-9, -5, 0, 5, 9):
        back = voice.sapi_to_rate(rate)
        assert voice.rate_to_sapi(back) == rate, rate


# ══════════════════════════════════════════════════════════════════════════
# 音色：按**男声 / 女声**选（用户 2026-09-20："音色改成让用户可选男声和女声，
# 而不是中英文"；再之前一句是"设置里桌宠声音可以自由改"）
# ══════════════════════════════════════════════════════════════════════════
#
# 这一段钉的是"用户能挑声音"这条链：枚举（含性别）→ 按性别挑 → 真进了 PowerShell 命令；
# 这台机器没有那个性别、或者音色压根不报性别时**回落到自动挑中文**（不会因此没声）；
# 没有中文音色时的行为不变（不念，并给出原因）。


def test_installed_voices_lists_name_culture_and_gender(monkeypatch, cfg) -> None:
    """枚举出来的是（名字, 区域, 性别）—— 性别是"按男/女挑"的判据。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Microsoft Huihui Desktop", "zh-CN", "Female"),
                               ("Microsoft Kangkang", "zh-CN", "Male"),
                               ("Microsoft Zira Desktop", "en-US", "Female")])

    assert voice.installed_voices() == [("Microsoft Huihui Desktop", "zh-CN", "female"),
                                        ("Microsoft Kangkang", "zh-CN", "male"),
                                        ("Microsoft Zira Desktop", "en-US", "female")]
    assert voice.voice_label("Microsoft Huihui Desktop", "zh-CN") == \
        "Microsoft Huihui Desktop（zh-CN）"
    assert voice.voice_label("没有区域的名字", "") == "没有区域的名字"


def test_gender_choice_picks_that_gender_and_prefers_chinese(monkeypatch, cfg) -> None:
    """选「女声/男声」→ 在该性别的音色里挑，**中文优先**（zh-CN → zh* → 其它语言）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("English Zira", "en-US", "female"),
                               ("Chinese Huihui", "zh-CN", "female"),
                               ("Kangkang", "zh-CN", "male")])
    cfg.notify_voice_name = "female"
    assert voice.chosen_voice(cfg) == "Chinese Huihui"

    cfg.notify_voice_name = "male"
    assert voice.chosen_voice(cfg) == "Kangkang"

    # 同一性别有多个中文音色时，取 zh-CN 那个（zh-TW 排在它后面）
    _fake_voices(monkeypatch, [("Taiwan Hanhan", "zh-TW", "female"),
                               ("Huihui", "zh-CN", "female")])
    cfg.notify_voice_name = "female"
    assert voice.chosen_voice(cfg) == "Huihui"


def test_gender_choice_falls_back_to_auto_when_that_gender_is_missing(monkeypatch, cfg) -> None:
    """这台机器没有那个性别的中文音色 → 自动回落到「自动挑中文」，而不是不念。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN", "female")])
    cfg.notify_voice_name = "male"

    assert voice.resolve_gender_voice("male") is None      # 没有男声
    assert voice.chosen_voice(cfg) == "Chinese Huihui"      # 回落成自动挑中文


def test_unknown_gender_is_guessed_from_the_voice_name(monkeypatch, cfg) -> None:
    """系统**不报性别**的语音（`Gender=NotSet`）→ 按音色名认一遍（用户 2026-09-21 实报）。

    他的原话是"两个声音都是女声，windows没有男声吗？"。原因之一就是这类语音不报性别：
    以前一律当"没这个性别"，于是"选了男声还是女声"。现在 `Huihui` 认成女声、
    `Kangkang` 认成男声，选男声就能真的挑到男声。
    """
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Huihui", "zh-CN", "NotSet"), ("Kangkang", "zh-CN", "")])

    assert voice.gender_from_name("Microsoft Huihui Desktop") == "female"
    assert voice.gender_from_name("Microsoft Kangkang Desktop") == "male"
    assert voice.voices_of_gender("female")[0][0] == "Huihui"
    assert voice.resolve_gender_voice("male") == "Kangkang"
    cfg.notify_voice_name = "male"
    assert voice.chosen_voice(cfg) == "Kangkang"


def test_gender_truly_unknown_still_falls_back_to_auto(monkeypatch, cfg) -> None:
    """名字也认不出来的语音（第三方/自造音色）→ 仍按"没这个性别"处理并回落自动。

    宁可回落，也不按名字瞎猜：猜错会让"男声"念出女声，比回落更难解释。
    """
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Some Custom Voice", "zh-CN", "NotSet"),
                               ("Chinese Huihui", "zh-CN", "")])
    cfg.notify_voice_name = "male"

    assert voice.gender_from_name("Some Custom Voice") == ""
    assert voice.voices_of_gender("male") == []
    assert voice.resolve_gender_voice("male") is None
    # 回落成"自动挑中文"：不写死具体名字（自动挑的是这台机器上最合适的中文音色，
    # 与假名单的先后有关）—— 判据是"最终用的就是自动那一个"
    assert voice.chosen_voice(cfg) == voice.voice_name()


def test_voices_summary_lists_what_this_machine_has(monkeypatch) -> None:
    """说明行要能把**实测到的**音色清单摆出来（用户据此判断"到底装没装男声"）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Huihui", "zh-CN", "Female"), ("Kangkang", "zh-CN", "Male")])

    summary = voice.voices_summary()

    assert "Huihui（女声 · zh-CN）" in summary and "Kangkang（男声 · zh-CN）" in summary
    assert voice.has_gender("male") is True and voice.has_gender("female") is True


def test_legacy_voice_name_means_auto(monkeypatch, cfg) -> None:
    """老配置里存的是"某个音色的完整名字"→ 一律当自动（界面不再列具体音色）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN", "female")])
    cfg.notify_voice_name = "Microsoft Zira Desktop"       # 上一版的写法

    assert voice.configured_gender(cfg) == ""
    assert voice.chosen_voice(cfg) == "Chinese Huihui"


def test_speak_always_uses_the_locked_rate(monkeypatch, cfg) -> None:
    """**语速恒为锁定值 1.0**（→ SAPI 的 0 = 正常），面板上的音色与音量照旧生效。

    主人 2026-09-21："把播报速度直接锁定 1.0 吧 不要给选择了 选错了感觉太怪了"。
    所以这里连"调用方硬塞一个别的 rate"也断言**不生效** —— 语速是锁死的。
    """
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN", "female")])
    spoken: list[list[str]] = []
    monkeypatch.setattr(voice, "run_command", spoken.append)

    ok = voice.speak_now("财神助手，语音提醒测试", cfg=cfg, force=True,
                         voice="Chinese Huihui", volume=0.5, rate=1.5)

    assert ok is True
    command = " ".join(spoken[0])
    assert "SelectVoice('Chinese Huihui')" in command
    assert "$s.Volume = 50" in command          # 音量仍然可调（主人没要求锁）
    assert "$s.Rate = 0" in command             # 语速锁定 1.0 → SAPI 0
    assert voice.RATE_LOCKED == 1.0


def test_the_spoken_command_rate_is_always_normal(monkeypatch, cfg) -> None:
    """实时提醒那条路也一样：不管配置里写过什么，念的时候语速都是 1.0 → SAPI 0。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Chinese Huihui", "zh-CN", "female")])
    spoken: list[list[str]] = []
    monkeypatch.setattr(voice, "run_command", spoken.append)
    monkeypatch.setattr(voice, "_ensure_worker", lambda: None)     # 不真起线程，直接看队列

    cfg.notify_voice_rate = 2.0            # 硬塞一个老键（配置里已经没有了）
    voice.speak("贵州茅台 600519，止损提醒", cfg=cfg)
    item = voice._queue.get_nowait()

    assert item["rate"] == voice.RATE_LOCKED == 1.0


def test_no_chinese_voice_still_means_no_speaking(monkeypatch, cfg) -> None:
    """一个中文音色都没有 → 不念（这条硬约束不因为"能挑音色"而放松）。"""
    monkeypatch.setattr(voice, "available", lambda: True)
    _fake_voices(monkeypatch, [("Microsoft Zira Desktop", "en-US")])
    spoken: list[list[str]] = []
    monkeypatch.setattr(voice, "run_command", spoken.append)

    assert voice.voice_name() is None
    assert voice.speak("贵州茅台 600519，止损提醒", cfg=cfg) is False
    assert voice.speak_now("试一条", cfg=cfg, force=True) is False
    assert spoken == []


# ══════════════════════════════════════════════════════════════════════════
# 音色枚举只发生一次（主人 2026-09-21："点策略什么的都会卡一下"）
# ══════════════════════════════════════════════════════════════════════════
#
# 枚举音色要起一个 PowerShell（几百毫秒）。设置页打开、保存、刷新说明行都会问
# "这台机器有哪些音色" —— 每次都重新枚举的话，设置页每点一下都顿一下。


def test_voices_are_enumerated_once_per_process(monkeypatch) -> None:
    """反复问"有哪些音色"只枚举一次（缓存住），失败结果也一样缓存。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return [("Huihui", "zh-CN", "Female"), ("Kangkang", "zh-CN", "Male")]

    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "_list_voices_raw", counting)
    monkeypatch.setattr(voice, "_voices", None, raising=False)

    for _ in range(10):
        voice.installed_voices()
        voice.voices_of_gender("male")
        voice.voices_summary()
        voice.has_gender("female")

    assert calls["n"] == 1, f"音色被枚举了 {calls['n']} 次（应当只有 1 次）"


def test_a_failed_enumeration_is_not_retried_every_time(monkeypatch) -> None:
    """枚举**失败**也要缓存（否则每次刷新设置页都再起一个进程、再失败一次）。"""
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        return []                      # 枚举失败时 `_list_voices_raw` 就是返回空

    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice, "_list_voices_raw", failing)
    monkeypatch.setattr(voice, "_voices", None, raising=False)

    for _ in range(5):
        assert voice.installed_voices() == []

    assert calls["n"] == 1, "枚举失败没有被缓存（每次问都重新起进程）"
