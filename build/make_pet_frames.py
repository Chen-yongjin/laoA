#!/usr/bin/env python3
"""把主人给的桌宠素材（8 张 1024×1024 立绘）切成**程序认的序列帧**。

怎么跑（素材目录就是工作区里那个 `图标宠物素材/宠物素材`）：
    python build/make_pet_frames.py "/vol4/@apphome/dsh/选股/图标宠物素材/宠物素材"

产出（写进 `src/laoa_trader/assets/pet/`，随包分发）：
    idle-1.png            待机（robot_default）
    walk-1..3.png         走路循环（robot_walk_1..3）—— "平时在右下角活动"就靠它
    act-1.png / act-2.png 有提醒时的动作（robot_wave / robot_jump，按顺序播一遍）
    think-1.png           干活中（robot_think）
    sad-1.png             亏了/止损（robot_sad）

两个取舍：
* **缩到 512×512**：程序最大只显示 180 像素，1024 的立绘一张 1.2MB，8 张就是 9MB ——
  512 足够（180 显示时还有 2.8 倍余量），包体小一截；
* **名字即含义**：程序按前缀找帧（见 `assets.pet_frames`），
  所以"哪个文件是什么动作"写在文件上，将来换素材不用改代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                     # noqa: E402
from PySide6.QtGui import QImage                  # noqa: E402
from PySide6.QtWidgets import QApplication        # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO / "src" / "laoa_trader" / "assets" / "pet"
#: 目标边长（见模块说明的取舍）
SIDE = 512

#: 源文件名 → 目标帧名（顺序即播放顺序）
MAPPING: tuple[tuple[str, str], ...] = (
    ("robot_default.png", "idle-1.png"),
    ("robot_walk_1.png", "walk-1.png"),
    ("robot_walk_2.png", "walk-2.png"),
    ("robot_walk_3.png", "walk-3.png"),
    ("robot_wave.png", "act-1.png"),
    ("robot_jump.png", "act-2.png"),
    ("robot_think.png", "think-1.png"),
    ("robot_sad.png", "sad-1.png"),
)


def main(argv: list[str]) -> int:
    source_dir = Path(argv[1]).expanduser() if len(argv) > 1 else Path(
        "/vol4/@apphome/dsh/选股/图标宠物素材/宠物素材")
    if not source_dir.is_dir():
        print(f"素材目录不存在：{source_dir}")
        return 1
    QApplication.instance() or QApplication([])
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    missing: list[str] = []
    for source_name, target_name in MAPPING:
        source = source_dir / source_name
        if not source.is_file():
            missing.append(source_name)
            continue
        image = QImage(str(source))
        if image.isNull():
            missing.append(f"{source_name}（读不出来）")
            continue
        scaled = image.scaled(SIDE, SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        target = TARGET_DIR / target_name
        if not scaled.save(str(target), "PNG"):
            missing.append(f"{source_name}（写不出去）")
            continue
        written.append(f"{target_name} ({target.stat().st_size // 1024}KB)")
    for name in written:
        print(f"  {name}")
    if missing:
        print("缺这些（没生成）：" + "、".join(missing))
    print(f"共 {len(written)} 帧 → {TARGET_DIR.relative_to(REPO)}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
