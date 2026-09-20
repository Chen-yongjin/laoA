"""**北京时间**（A 股市场的墙上时间）—— 全项目唯一的时钟来源。

为什么单独一个模块
------------------
"今天是哪天/现在几点"这件事在 A 股工具里到处都要用（交易时段、交易日历、提醒时间戳、
池子日期、数据新鲜度），而**机器时区不一定是北京时间**：CI 的 Windows runner 是 UTC、
NAS/Docker 常常也是 UTC。曾经踩过的两个真实事故：

* CI 上当地 13:45 被判成"交易时段中"（北京此刻 21:45，早已收盘）；
* 提醒写进库时 `date` 用北京日期、时间戳却用机器本地时间 → 在 UTC 机器上变成
  "日期是今天、时间是昨天 17:10"，界面上显示的时间与"今天"对不上，CI 因此红过一次。

所以：**凡是与日期/时间有关的取值，一律走这里**，不要用 `datetime.now()`。
返回值是**朴素**的（不带 tzinfo）—— 项目里所有比较都是墙上时间的朴素比较。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: 北京时间（UTC+8，中国不实行夏令时）
TZ_SHANGHAI = timezone(timedelta(hours=8))

#: 日期 / 时间戳的字符串格式（与库里的列宽一致）
DATE_FORMAT = "%Y-%m-%d"
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_cn(now: datetime | None = None) -> datetime:
    """现在（北京时间，朴素值）；传入 `now` 原样返回（测试注入确定时刻用）。"""
    if now is not None:
        return now
    return datetime.now(TZ_SHANGHAI).replace(tzinfo=None)


def today_cn(now: datetime | None = None) -> str:
    """今天（北京日期，`YYYY-MM-DD`）。"""
    return now_cn(now).strftime(DATE_FORMAT)


def stamp_cn(now: datetime | None = None) -> str:
    """时间戳（北京时间，`YYYY-MM-DD HH:MM:SS`）—— 库里 `pushed_at` 这类列用它。"""
    return now_cn(now).strftime(STAMP_FORMAT)


__all__ = ["DATE_FORMAT", "STAMP_FORMAT", "TZ_SHANGHAI", "now_cn", "stamp_cn", "today_cn"]
