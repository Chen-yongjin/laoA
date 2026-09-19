"""公式库（`laoa_trader.formulas`）与「公式」组（`strategy/formula_group.py`）的离线测试。

覆盖六块：

1. **目录定位** —— 源码运行 / 打包后（exe 同级）/ 环境变量覆盖，三种形态都测；
2. **保存与读回** —— 名称必填、非法字符安全化、注释头、UTF-8/BOM 都能被
   `load_formula_files()` 原样读回（这一条把"界面存、引擎读"这条缝钉死）；
3. **参与选股名单** —— 找不到的 / 语法错的公式名**忽略并记日志**；
4. **连板()/涨停天数() 的历史坑** —— 用到就提醒，不用就不提醒；
5. **试算** —— 合成小库上命中集合是**确定的**（不是"跑通就算过"）；
6. **集成** —— 勾选的公式进池、来源标成「公式·名字」、推送行带公式名；
   以及**失败隔离**：公式在运行时抛错，内置策略照常出票、建池不失败。

全程不联网、只读本地合成库（`tests/conftest.py` 已在 socket 层封死网络）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from laoa_trader import formulas as lib
from laoa_trader import intraday, pool
from laoa_trader.config import Config, save_settings
from laoa_trader.data import storage
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group
from tests.conftest import messages, workdays_ending

#: 小库里的三只票：甲/丙 一路上涨、乙 一路下跌 —— 于是"今天高于 5 日均价"
#: 这条公式的命中集合是 `{600001, 600003}`，可以**精确断言**而不是只看数量。
RISING = ("600001", "600003")
FALLING = "600002"


def _write_formula(folder: Path, name: str, body: str, description: str = "") -> Path:
    """直接按引擎认的格式写一个公式文件（不经过界面）。"""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.txt"
    path.write_text(lib.formula_text(name, body, description), encoding="utf-8")
    return path


@pytest.fixture()
def formula_db(tmp_path: Path) -> str:
    """3 只票 × 30 个交易日的小库（只读用：试算/成绩单都在它上面跑）。"""
    path = storage.init_db(tmp_path / "trader.db")
    days = workdays_ending("2026-09-11", 30)
    plan = {
        "600001": ("甲样本", 10.0, 0.01),
        "600002": ("乙样本", 20.0, -0.01),
        "600003": ("丙样本", 5.0, 0.01),
    }
    with storage.connect(path) as conn:
        storage.write_stock_basic(conn, [(s, meta[0], "银行") for s, meta in plan.items()])
        rows = []
        for symbol, (_name, base, drift) in plan.items():
            price = base
            for day in days:
                price = price * (1 + drift)
                rows.append((symbol, day, price * 0.99, price * 1.01, price * 0.98,
                             price, 1_000_000.0, price * 1_000_000.0))
        storage.write_daily_raw(conn, rows)
        storage.write_calendar(conn, days)
    return str(path)


@pytest.fixture()
def formulas_cfg(cfg: Config) -> Config:
    """一份"带 config.toml 的配置"（勾选公式要写回文件，得有落点）。"""
    cfg.source_path = cfg.data_dir.parent / "config.toml"
    return cfg


# ══════════════════════════════════════════════════════════════════════════
# 1) 目录定位：源码运行 / 打包后 / 环境变量
# ══════════════════════════════════════════════════════════════════════════


def test_formula_dir_source_run_is_repo_formulas(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码运行：公式目录就是仓库根那份 `formulas/`（随包分发的也是它）。"""
    monkeypatch.delenv(lib.FORMULA_DIR_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)

    folder = lib.formula_dir()

    assert folder == lib.repo_root() / "formulas"
    assert folder.is_dir()
    # 仓库里那几条随包公式就在里面（"载入示例"靠它；`尾盘选股策略` 是 2026-09-18 内置的那条）
    assert {spec.name for spec in lib.formula_files(folder)} >= {
        "放量上攻", "均线多头排列", "尾盘选股策略",
    }


def test_formula_dir_frozen_uses_exe_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """打包后：公式目录在 **exe 同级**（用户双击 exe 就看得见、备份得到）。"""
    monkeypatch.delenv(lib.FORMULA_DIR_ENV, raising=False)
    exe = tmp_path / "dist" / "老A选股助手" / "老A选股助手.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    folder = lib.formula_dir()

    assert folder == exe.parent / "formulas"
    assert folder.is_dir()          # 不存在就创建
    # 空目录时会自动把随包公式复制进来（否则新用户打开是空列表，第一步就走不下去）
    assert {spec.name for spec in lib.formula_files(folder)} >= {"放量上攻", "尾盘选股策略"}


def test_formula_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先（换机器 / 放共享盘）。"""
    target = tmp_path / "我的公式"
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    assert lib.formula_dir() == target
    assert target.is_dir()


def test_formula_dir_does_not_overwrite_user_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户已经存过同名公式时**绝不覆盖**，但其它随包公式照样补齐。

    2026-09-18 起这条规则从"只在空目录复制一次"改成"**缺哪条补哪条**"：
    新版本多带的随包公式（例如内置的 `尾盘选股策略`）在**已经用过一段时间**的
    用户目录里也必须出现 —— 否则"内置"就只对全新安装的人有效。
    """
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    mine = target / "放量上攻.txt"
    mine.write_text("# 名称: 放量上攻\nC>MA(C,999)\n", encoding="utf-8")   # 用户自己改过的
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    lib.formula_dir()

    # 用户的文件**一个字都没被动过**
    assert mine.read_text(encoding="utf-8") == "# 名称: 放量上攻\nC>MA(C,999)\n"
    # 其它随包公式补进来了（内置那几条）
    names = {p.name for p in target.iterdir()}
    assert "均线多头排列.tvf" in names and "尾盘选股策略.txt" in names


def test_formula_dir_never_resurrects_a_deleted_bundled_formula(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户删掉的随包公式**不会**在下次启动时长回来（那是"他不要"，不是"他还没有"）。

    判据就是那条记录（`.laoa-seeded.json`）：播过种的名字记在里面，
    之后目录里没了也只当"用户删了"。否则每次开机都长回来，用户会以为程序坏了。
    """
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))
    lib.formula_dir()                      # 第一次：播种 + 记录
    gone = target / "尾盘选股策略.txt"
    assert gone.exists()

    gone.unlink()                          # 用户删掉它
    lib.formula_dir()                      # 再启动一次

    assert not gone.exists(), "删掉的随包公式又长回来了"


#: 老版本随包那份「涨停回踩低吸」的**原始内容**（退役清理的判据就是它的哈希）。
#:
#: 为什么把内容直接抄进测试、而不是运行时 `git show 24b1b96:...` 去取：
#: CI 是**浅克隆**，那个提交在 runner 上根本不存在 —— 第一次提交后 CI 立刻红在
#: `git ... returned non-zero exit status 128`。测试不该依赖 git 历史。
#: 这段内容与 `formulas.RETIRED_BUNDLED_FORMULAS` 里那个哈希一一对应（改了这里就会红）。
_RETIRED_TEXT = (
    "# 名称: 涨停回踩低吸\n"
    "# 说明: 近 10 日内出现过涨停或连板（用本地涨停池数据），今日缩量回踩 5 日线不破。\n"
    "#       涨停数据来自每天同步的 limit_up_pool，没下载到涨停池时这几条公式不会出信号。\n"
    "#       ⚠️ 这条只是写法示例，不是推荐：同样的条件跑过 10 年成绩单（1029 万行），\n"
    "#         开盘买 −0.19%（t=−0.10）、尾盘买 −1.21%（t=−5.36），两套口径都是负的。\n"
    "#         留着它是为了演示\"怎么用 涨停天数() / 连板() 写条件\"，别照抄去实盘。\n"
    "#       （内置策略里的「连板回踩低吸」就是它，证据见 README 的策略证据表。）\n"
    "ZT10:=涨停天数(10)\n"
    "LB:=连板()\n"
    "M5:=MA(C,5)\n"
    "V5:=MA(V,5)\n"
    "HAS_ZT:=ZT10>=1 OR LB>=1\n"
    "SHRINK:=V<V5*0.9\n"
    "PULLBACK:=C>=M5*0.98 AND C<REF(C,1)\n"
    "HAS_ZT AND SHRINK AND PULLBACK\n"
)


def _retired_bytes() -> bytes:
    """老版本随包那份「涨停回踩低吸」的**原始字节**（退役清理的判据就是它）。

    退役清理是"内容一致才删"，所以测试必须拿到真那份内容，不能自己现编一个 ——
    这里用的是抄进来的常量（见上面那段注释：CI 浅克隆里没有那个提交）。
    先断言它与 `formulas.RETIRED_BUNDLED_FORMULAS` 记的哈希对得上，两边不会各自漂移。
    """
    raw = _RETIRED_TEXT.encode("utf-8")
    import hashlib

    recorded = lib.RETIRED_BUNDLED_FORMULAS["涨停回踩低吸.txt"]
    assert hashlib.sha256(raw).hexdigest() == recorded, "测试里的老内容与退役名单的哈希不一致"
    return raw


def test_retired_bundled_formula_is_removed_for_existing_installs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**退役清理**：老用户目录里那份没改过的 `涨停回踩低吸.txt` 会被清掉。

    为什么必须有这一步（用户 2026-09-18 要求删掉「连板回踩低吸」这条策略）：
    只把文件从仓库里删掉的话，**已经装过老版本的人**升级后目录里那份还在，
    列表里照样留着这条 —— 等于没删。
    """
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    retired = target / "涨停回踩低吸.txt"
    retired.write_bytes(_retired_bytes())          # 模拟：老版本播种进来的那一份
    mine = target / "我的.txt"
    mine.write_text("# 名称: 我的\nC>MA(C,5)\n", encoding="utf-8")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    lib.formula_dir()

    assert not retired.exists(), "退役的随包公式没有被清掉"
    assert mine.exists(), "用户自己的公式被误删了"
    # 记录进状态文件 = 明确告诉补齐逻辑"这条处理过了"
    seeded = json.loads((target / lib.SEED_STATE_NAME).read_text(encoding="utf-8"))
    assert "涨停回踩低吸.txt" in seeded


def test_retired_bundled_formula_is_kept_when_the_user_edited_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户**改过**的那份绝不删：他改阈值/加条件之后，那就是他自己的公式了。

    判据是哈希逐字节一致，所以"同名但内容不同"必须原样留着 ——
    替用户做主删掉他的劳动成果，比"退役公式没清干净"糟糕得多。
    """
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    edited = target / "涨停回踩低吸.txt"
    edited.write_text(
        "# 名称: 涨停回踩低吸\n# 说明: 我自己改过的条件\nC>MA(C,5)\n", encoding="utf-8"
    )
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    lib.formula_dir()

    assert edited.exists(), "用户改过的公式被删了"
    assert "我自己改过的条件" in edited.read_text(encoding="utf-8")
    # 它还在列表里（随包的那几条也会照常补进来，所以只断言"这一条还在"）
    assert "涨停回踩低吸" in {spec.name for spec in lib.formula_files(target)}


def test_retired_bundled_formula_never_comes_back_from_the_bundled_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """退役的公式**不会被"缺哪条补哪条"再补回来**（哪怕随包目录里还留着老文件）。

    这条是退役清理最容易做错的地方：老版本解包目录（`_internal/formulas`）在用户
    原地升级时可能还在，而 `_seed_samples()` 的规则是"缺哪条补哪条" ——
    只要退役文件**还在随包目录里**、又没被记进名单，下一次启动就会把它原样复制回去。
    所以这里造一个"随包目录里仍留着退役文件"的场景，断言它不会被补回。
    """
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    (target / "涨停回踩低吸.txt").write_bytes(_retired_bytes())
    fake_bundled = tmp_path / "老解包目录" / "formulas"
    fake_bundled.mkdir(parents=True)
    (fake_bundled / "涨停回踩低吸.txt").write_bytes(_retired_bytes())   # 老版本还带着它
    (fake_bundled / "放量上攻.txt").write_text("# 名称: 放量上攻\nC>MA(C,5)\n", encoding="utf-8")
    monkeypatch.setattr(lib, "bundled_formula_dir", lambda: fake_bundled)
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    lib.formula_dir()                      # 第一次：清掉退役文件 + 记录
    assert not (target / "涨停回踩低吸.txt").exists()
    assert (target / "放量上攻.txt").exists()          # 别的随包公式照常补齐

    lib.formula_dir()                      # 再启动一次：绝不能把它补回来

    assert not (target / "涨停回踩低吸.txt").exists(), "退役公式被补齐逻辑复活了"
    assert {spec.name for spec in lib.formula_files(target)} == {"放量上攻"}


# ══════════════════════════════════════════════════════════════════════════
# 2) 名称安全化 / 保存 / 读回
# ══════════════════════════════════════════════════════════════════════════


def test_safe_name_replaces_illegal_chars() -> None:
    """路径分隔符、冒号、问号这些**建不出文件**的字符换成下划线。"""
    assert lib.safe_name(" 5日线/放量 ") == "5日线_放量"
    assert lib.safe_name('a:b*c?"d<e>f|g\\h') == "a_b_c__d_e_f_g_h"
    # 结尾的点与空格：Windows 会静默吃掉，先去掉，免得"名字和文件对不上"
    assert lib.safe_name("放量上攻. ") == "放量上攻"
    assert len(lib.safe_name("长" * 100)) == lib.MAX_NAME_CHARS


def test_name_error_rejects_empty() -> None:
    assert "请先填公式名称" in lib.name_error("   ")
    assert lib.name_error(None)
    assert lib.name_error("正常名字") == ""


def test_save_formula_rejects_empty_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="请先填公式名称"):
        lib.save_formula("  ", "C>MA(C,5)", directory=tmp_path)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """存进去 → 被 `load_formula_files()` 原样读回（名称、说明、正文一致）。"""
    body = "M5:=MA(C,5)\nV5:=MA(V,5)\nC>M5 AND V>V5*1.5"
    path = lib.save_formula("放量上攻", body, directory=tmp_path)

    assert path.name == "放量上攻.txt"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 名称: 放量上攻\n")
    assert "# 说明: " in text                       # 说明自动生成（界面上不用手填）
    assert "字段：C、VOL" in text

    specs = lib.formula_files(tmp_path)
    assert [spec.name for spec in specs] == ["放量上攻"]
    assert specs[0].ok
    assert specs[0].source.strip() == body
    assert "MA" in specs[0].description


def test_save_formula_sanitizes_name_at_library_level(tmp_path: Path) -> None:
    """**公式库这一层**也必须安全化：直接调 API（不经过界面）同样不能建出非法文件名。

    为什么单独测这一条：界面 `on_save` 自己也会安全化一次 —— 只测界面的话，
    把库里的安全化删掉照样全绿（实测就是这样漏过一次），而 CLI / 以后的新入口
    一旦直接用 `save_formula()`，`涨/跌` 这种名字会变成"往子目录里写"。
    """
    path = lib.save_formula("涨/跌:*?", "C>MA(C,5)", directory=tmp_path)

    assert path.parent == tmp_path                     # 没被当成子目录
    assert path.name == "涨_跌___.txt"
    specs = lib.formula_files(tmp_path)
    assert [spec.name for spec in specs] == ["涨_跌___"]
    assert specs[0].ok and specs[0].source.strip() == "C>MA(C,5)"


def test_save_formula_strips_trailing_dot_for_windows(tmp_path: Path) -> None:
    """结尾的点/空格：Windows 会静默吃掉，`save_formula` 必须先去掉。"""
    path = lib.save_formula("放量上攻. ", "C>MA(C,5)", directory=tmp_path)

    assert path.name == "放量上攻.txt"


def test_save_overwrites_same_name_without_extra_file(tmp_path: Path) -> None:
    lib.save_formula("同名", "C>MA(C,5)", directory=tmp_path)
    lib.save_formula("同名", "C<MA(C,5)", directory=tmp_path)

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["同名.txt"]                     # 没有留下 .tmp 之类的垃圾
    assert lib.formula_files(tmp_path)[0].source.strip() == "C<MA(C,5)"


def test_save_reads_back_bom_file(tmp_path: Path) -> None:
    """Windows 记事本存 UTF-8 会带 BOM —— 用户手改过的公式必须照样读得回来。"""
    path = tmp_path / "带BOM.txt"
    path.write_text("# 名称: 带BOM公式\nC>MA(C,5)\n", encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    specs = lib.formula_files(tmp_path)

    assert [spec.name for spec in specs] == ["带BOM公式"]
    assert specs[0].ok


def test_save_formula_with_chinese_name_keeps_body(tmp_path: Path) -> None:
    body = "ZT:=涨停天数(10)\nLB:=连板()\nZT>=1 OR LB>=2"
    lib.save_formula("涨停回踩·低吸", body, directory=tmp_path)

    spec = lib.formula_files(tmp_path)[0]
    assert spec.name == "涨停回踩·低吸"
    assert spec.ok
    assert spec.source.strip() == body


def test_delete_formula(tmp_path: Path) -> None:
    lib.save_formula("删我", "C>MA(C,5)", directory=tmp_path)

    assert lib.delete_formula("删我", tmp_path) is True
    assert lib.formula_files(tmp_path) == []
    assert lib.delete_formula("删我", tmp_path) is False    # 再删一次不抛异常


# ══════════════════════════════════════════════════════════════════════════
# 3) 参与选股名单（enabled_formulas）
# ══════════════════════════════════════════════════════════════════════════


def test_enabled_names_default_is_empty_and_touches_nothing(formulas_cfg: Config,
                                                            tmp_path: Path) -> None:
    """默认（没勾）→ 空名单，而且**连目录都不看**（不存在也不报错）。"""
    assert formulas_cfg.enabled_formulas == []
    assert lib.enabled_names(formulas_cfg, tmp_path / "根本没有这个目录") == []


def test_enabled_names_ignores_missing_and_broken(formulas_cfg: Config, tmp_path: Path,
                                                  log_records) -> None:
    """找不到的、语法错的公式名 → 忽略 + **记日志**（不静默失败）。"""
    _write_formula(tmp_path, "好公式", "C>MA(C,5)")
    _write_formula(tmp_path, "坏公式", "C>MAA(C,5)")
    formulas_cfg.enabled_formulas = ["好公式", "坏公式", "根本不存在的公式", "好公式"]

    picked = lib.enabled_names(formulas_cfg, tmp_path)

    assert picked == ["好公式"]              # 去重、只留能跑的那条
    log = messages(log_records)
    assert "根本不存在的公式" in log and "没有这条公式" in log
    assert "坏公式" in log and "语法有错" in log


# ══════════════════════════════════════════════════════════════════════════
# 4) 连板()/涨停天数() 的历史坑
# ══════════════════════════════════════════════════════════════════════════


def test_limit_up_hint_only_for_limit_up_functions() -> None:
    """用到才提醒：`连板()` 有提醒，普通公式**不能**被贴这条提醒。"""
    assert lib.limit_up_hint(fm.compile_formula("连板()>=2")) == lib.LIMIT_UP_HINT
    assert lib.limit_up_hint(fm.compile_formula("涨停天数(10)>=1")) == lib.LIMIT_UP_HINT
    assert lib.limit_up_hint(fm.compile_formula("C>MA(C,5)")) == ""
    assert lib.limit_up_hint(None) == ""
    assert "涨停池" in lib.LIMIT_UP_HINT and "早期日期" in lib.LIMIT_UP_HINT


# ══════════════════════════════════════════════════════════════════════════
# 5) 试算 / 成绩单
# ══════════════════════════════════════════════════════════════════════════


def test_preview_hits_known_symbols(formula_db: str) -> None:
    """试算：合成小库上命中集合是确定的（上涨的两只，不是下跌的那只）。"""
    formula = fm.compile_formula("C>MA(C,5)")

    result = lib.preview_hits(formula, formula_db)

    assert {hit["symbol"] for hit in result["hits"]} == set(RISING)
    assert result["count"] == 2
    assert result["date"] == "2026-09-11"
    assert result["scanned"] == 3
    assert result["errors"] == []


def test_preview_hits_respects_limit_and_names(formula_db: str) -> None:
    """`limit` 只截断**显示**（`shown`），`hits` 必须是**全量**。

    为什么要钉住"全量"：界面上的【导出选股结果】写的就是 `hits`
    （提示区只列前 20 只，文件里是全部命中）。哪天有人把 `hits[:limit]`
    改回来，用户导出的文件就会静默少票 —— 那种错在界面上完全看不出来。
    """
    formula = fm.compile_formula("C>MA(C,5)")

    result = lib.preview_hits(formula, formula_db, limit=1)

    assert result["count"] == 2 and result["shown"] == 1
    assert len(result["hits"]) == 2                    # 全量，不受 limit 影响
    assert {hit["symbol"] for hit in result["hits"]} == set(RISING)
    assert result["hits"][0]["name"] == "甲样本"        # 带中文名（小白只认名字）


def test_preview_hits_missing_db_is_chinese_error(tmp_path: Path) -> None:
    """库不存在：给中文提示（而不是一堆 traceback）。"""
    with pytest.raises(fm.FormulaDataError, match="本地数据库不存在"):
        lib.preview_hits(fm.compile_formula("C>MA(C,5)"), tmp_path / "没有.db")


def test_scorecard_reports_progress_and_numbers(formula_db: str) -> None:
    """成绩单：进度回调被调用（阶段 + 已完成 + 总数），并给出样本数与平均收益。"""
    seen: list[tuple[str, int, int]] = []
    formula = fm.compile_formula("C>MA(C,5)")

    result = lib.run_scorecard(formula, formula_db, progress_cb=lambda *a: seen.append(a))

    assert seen, "成绩单必须回报进度（界面的进度条靠它）"
    assert all(stage == "公式成绩单" and total >= 1 and done >= 0 for stage, done, total in seen)
    assert result["samples"] > 0
    assert result["days"] > 0
    assert isinstance(result["avg"], float)
    assert result["conv_key"] == lib.DEFAULT_CONVENTION_KEY
    assert "公式成绩单" in result["text"]
    assert "绝对收益" in result["text"]          # 口径写清楚（与策略成绩单的 α 不是一回事）


def test_scorecard_mentions_limit_up_history(formula_db: str) -> None:
    """成绩单里也要提醒 `连板()` 的历史坑（否则用户会以为公式有问题）。"""
    result = lib.run_scorecard(fm.compile_formula("连板()>=2"), formula_db)

    assert result["hint"] == lib.LIMIT_UP_HINT
    assert "涨停池" in result["text"]
    assert "早期日期" in result["text"]


def test_scorecard_without_limit_up_functions_has_no_hint(formula_db: str) -> None:
    result = lib.run_scorecard(fm.compile_formula("C>MA(C,5)"), formula_db)

    assert result["hint"] == ""
    assert "涨停池" not in result["text"]


# ══════════════════════════════════════════════════════════════════════════
# 6) 「公式」组：跑、进池、失败隔离
# ══════════════════════════════════════════════════════════════════════════


def test_run_enabled_formulas_default_reads_nothing(formulas_cfg: Config, tmp_path: Path) -> None:
    """没勾任何公式：一次库都不读（默认状态必须与"没这个功能"完全一致）。"""
    run = formula_group.run_enabled_formulas(tmp_path / "没有这个库.db", formulas_cfg)

    assert run.picks == {} and run.errors == [] and run.scanned == 0


def test_run_enabled_formulas_picks_expected_symbols(formula_db: str, formulas_cfg: Config,
                                                     tmp_path: Path) -> None:
    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    formulas_cfg.enabled_formulas = ["收盘在5日线上"]

    run = formula_group.run_enabled_formulas(formula_db, formulas_cfg, directory=folder)

    assert list(run.picks) == ["公式·收盘在5日线上"]
    assert {pick["symbol"] for pick in run.picks["公式·收盘在5日线上"]} == set(RISING)
    assert run.picks["公式·收盘在5日线上"][0]["reason"] == "公式：收盘在5日线上"
    assert run.status == {} and run.errors == []
    assert run.ran == ["收盘在5日线上"]


def test_run_enabled_formulas_reports_missing_db(formulas_cfg: Config, tmp_path: Path) -> None:
    """库不存在：说清楚原因就返回，**不抛异常**（否则整轮建池会挂）。"""
    folder = tmp_path / "formulas"
    _write_formula(folder, "随便", "C>MA(C,5)")
    formulas_cfg.enabled_formulas = ["随便"]

    run = formula_group.run_enabled_formulas(tmp_path / "没有.db", formulas_cfg, directory=folder)

    assert run.picks == {}
    assert run.errors and "本地数据库不存在" in run.errors[0]
    assert "本地数据库不存在" in run.status["随便"]


def test_bad_formula_is_isolated_from_the_rest_of_the_pool(engine, formulas_cfg: Config,
                                                          tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """**失败隔离**：一条公式在运行期抛错 → 好公式照常进池、建池不失败。

    做法：让两条公式在求值时分别抛 `FormulaError` 与 `FormulaDataError`
    （真实场景就是"连板() 用的本地涨停池还没攒够历史""某只票的字段长度对不上"）。
    如果没有 `formula_group` 里那层逐票兜住，异常会一路冒到 `build_pool` ——
    用户自己写坏的一条公式会把整轮选股带走。

    2026-09-18 起这条用例少了一半内容：它原来还要验"内置 5 条策略照常出票"，
    而内置策略已经改成随包公式、不再由 `rules.run_all()` 产出候选，
    所以现在验的是"**好公式照常进池、坏公式只留下原因**"。
    """
    folder = tmp_path / "formulas"
    _write_formula(folder, "好公式", "C>MA(C,5)")
    _write_formula(folder, "坏公式甲", "C>MA(C,5)")
    _write_formula(folder, "坏公式乙", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    formulas_cfg.enabled_formulas = ["好公式", "坏公式甲", "坏公式乙"]

    original = fm.Formula.eval

    def fake_eval(self, series):  # noqa: ANN001 - 与引擎同签名
        if self.label == "坏公式甲":
            raise fm.FormulaError("公式求值失败（类型不匹配）", code="eval")
        if self.label == "坏公式乙":
            raise fm.FormulaDataError("连板() 缺历史：本地涨停池还没攒够数据")
        return original(self, series)

    monkeypatch.setattr(fm.Formula, "eval", fake_eval)

    report: dict = {}
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False, report=report)

    # 候选只剩公式（随包的那几条 + 这几条），所以"照常出票"这句话现在只对公式成立
    assert rows, "好公式必须照常出票（一条坏公式不能把整轮选股带走）"
    assert {row["strategy"] for row in rows} == {"公式·好公式"}
    # 两条坏公式不进池，但**都有原因**（状态栏/日志/报告三处都能看到）
    status = report["formulas"]["status"]
    assert "坏公式甲" in status and "坏公式乙" in status
    assert any("坏公式甲" in msg for msg in report["errors"])
    assert any("坏公式乙" in msg for msg in report["errors"])


def test_formula_group_status_is_remembered_for_ui(formula_db: str, formulas_cfg: Config,
                                                  tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """运行期错误要留在进程里，界面刷新列表时才能把那一行标红。"""
    folder = tmp_path / "formulas"
    _write_formula(folder, "坏公式", "C>MA(C,5)")
    formulas_cfg.enabled_formulas = ["坏公式"]
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    monkeypatch.setattr(fm.Formula, "eval",
                        lambda self, series: (_ for _ in ()).throw(
                            fm.FormulaDataError("数据不足")))

    formula_group.run_enabled_formulas(formula_db, formulas_cfg, directory=folder)

    assert "坏公式" in formula_group.last_status()
    assert "数据不足" in formula_group.last_status()["坏公式"]
    formula_group.reset_status()
    assert formula_group.last_status() == {}


# ── 进池 / 来源标注 / 推送 ──


def test_enabled_formula_enters_pool_with_formula_source(engine, formulas_cfg: Config,
                                                        tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """勾选的公式进池，来源标成「公式·名字」（表格、卡片、推送行三处一致）。"""
    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    formulas_cfg.enabled_formulas = ["收盘在5日线上"]

    # 走**真实链路**：不给 picks（2026-09-18 起候选只来自勾选的公式，建池自己去跑）
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False)

    formula_rows = [row for row in rows if row["strategy"] == "公式·收盘在5日线上"]
    assert formula_rows, "勾了公式就必须能进池"

    table_rows = {row["symbol"]: row for row in pool.pool_table_rows(formulas_cfg.db_path)}
    row = table_rows[formula_rows[0]["symbol"]]
    assert row["label"] == "公式·收盘在5日线上"          # 「来源策略」列
    assert row["source_label"] == "公式·收盘在5日线上"   # 「来源」列
    assert row["source"] == "公式"
    assert row["is_formula"] is True
    # 自定义公式**不打**证据标记（那是内置策略的边际证据，不是用户公式的）
    assert row["evidence_text"] == ""

    lines = "\n".join(pool.format_pool_lines(rows))
    assert "公式·收盘在5日线上" in lines


def test_not_enabled_formula_does_not_change_pool(engine, formulas_cfg: Config, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """**默认不参与**：目录里有公式、但没勾 → 池子里一条公式标的都没有。"""
    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    assert formulas_cfg.enabled_formulas == []

    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False)

    assert all(
        not formula_group.is_formula_strategy(row["strategy"]) for row in rows
    ), "没勾公式就不该有公式标的进池"


def test_formula_pool_row_is_also_monitored_intraday(engine, formulas_cfg: Config,
                                                     tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """进池的公式标的也要进**盘中观察池**。

    为什么专门测这一条：盘中观察池是按"启用的内置策略"过滤池子的，
    公式标的的策略名（`公式·xxx`）不在那份名单里 —— 漏掉这一处就会表现成
    "股票池里有它，可它跌到止损了也不提醒"，而这在界面上极难察觉。
    """
    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    formulas_cfg.enabled_formulas = ["收盘在5日线上"]

    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False)
    symbol = next(row["symbol"] for row in rows if row["strategy"] == "公式·收盘在5日线上")

    targets, symbols = intraday.watch_targets(formulas_cfg.db_path, cfg=formulas_cfg)

    assert symbol in symbols
    assert targets[symbol]["strategy"] == "公式·收盘在5日线上"


def test_push_title_mentions_formula_group(engine, formulas_cfg: Config, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """推送**标题与正文**都要标明「公式」来源（用户不用翻到正文才知道是自己写的）。"""
    from laoa_trader import notify as notify_mod
    from laoa_trader import scheduler

    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    formulas_cfg.enabled_formulas = ["收盘在5日线上"]

    captured: dict = {}

    def fake_notify_all(title, lines, kinds=notify_mod.KINDS, cfg=None):
        captured["title"] = title
        captured["lines"] = lines
        return {"windows": {"ok": True}}

    monkeypatch.setattr(notify_mod, "notify_all", fake_notify_all)
    monkeypatch.setattr(scheduler.sync, "daily_update", lambda *a, **k: [])

    report = scheduler.run_daily(formulas_cfg, engine, notify=True)

    assert report["pool"]
    assert "公式：收盘在5日线上" in captured["title"]
    body = "\n".join(captured["lines"])
    assert "公式·收盘在5日线上" in body


def test_formula_opt_in_writes_config_and_keeps_comments(formulas_cfg: Config,
                                                         tmp_path: Path) -> None:
    """勾「参与选股」→ 写回 `enabled_formulas`，而且**用户自己的注释不许丢**。"""
    formulas_cfg.source_path = tmp_path / "config.toml"
    formulas_cfg.source_path.write_text(
        "# 我自己写的注释，别动\n"
        'enabled_groups = ["short"]\n'
        'my_own_key = "别动我"\n',
        encoding="utf-8",
    )

    save_settings(formulas_cfg, {"enabled_formulas": ["放量上攻"]})

    text = formulas_cfg.source_path.read_text(encoding="utf-8")
    assert 'enabled_formulas = ["放量上攻"]' in text
    assert "# 我自己写的注释，别动" in text
    assert 'my_own_key = "别动我"' in text
    # 老配置里的退役键（`enabled_groups`）**原样留着**：不报错、不丢内容、也不复活
    assert 'enabled_groups = ["short"]' in text
    assert formulas_cfg.enabled_formulas == ["放量上攻"]


def test_formula_name_helpers_live_in_formula_group() -> None:
    """公式合成名的三个 helper 住在 `formula_group`（策略组机制删掉之后搬过来的）。

    为什么它们必须留着：池子里的候选是按"策略名"组织的，公式靠 `公式·` 前缀
    借这个结构（来源列、排序、推送全都能复用）；老库里的行也还带着类名，
    靠 `legacy.strategy_label` 显示中文名。
    """
    assert formula_group.formula_strategy_name("放量上攻") == "公式·放量上攻"
    assert formula_group.formula_name_of("公式·放量上攻") == "放量上攻"
    assert formula_group.is_formula_strategy("公式·x") is True
    # 老策略类名**不是**公式（它们只会出现在历史数据里）
    assert formula_group.is_formula_strategy("LowPriceStrategy") is False
    assert formula_group.formula_name_of("LowPriceStrategy") == "LowPriceStrategy"
    assert pool.weight_of("公式·x") == formula_group.FORMULA_WEIGHT == 2
    assert pool.weight_of("LowPriceStrategy") == 1        # 非公式一律 1（老数据）


# ══════════════════════════════════════════════════════════════════════════
# 7) 备注（`# 说明:`）的读写 —— 界面「备注」框 ↔ 文件注释头
# ══════════════════════════════════════════════════════════════════════════
#
# 界面上的「备注」框与公式文件的 `# 说明:` 注释头是同一件事（`docs/改版方案.md`
# TAB 4："自定义公式追加在后（备注来自公式文件的 `# 说明:`）"），这一节把这条缝钉死：
# 写进去的是它、读回来也是它，中间不许有第二份真相。


def test_save_formula_stores_note_in_description_header(tmp_path: Path) -> None:
    """`save_formula(description=...)` → 文件里的 `# 说明:`；读回来还是同一句。"""
    path = lib.save_formula("放量上攻", "C>MA(C,5)", description="站上5日线并且放量",
                            directory=tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "# 名称: 放量上攻" in text
    assert "# 说明: 站上5日线并且放量" in text
    spec = lib.formula_files(tmp_path)[0]
    assert spec.description == "站上5日线并且放量"
    assert spec.ok and spec.source.strip() == "C>MA(C,5)"


def test_save_formula_note_none_keeps_auto_describe(tmp_path: Path) -> None:
    """备注留空（`description=None`）→ 仍然自动生成"用到的字段/函数"（小白不用手写）。"""
    lib.save_formula("放量上攻", "M5:=MA(C,5)\nC>M5", directory=tmp_path)

    text = (tmp_path / "放量上攻.txt").read_text(encoding="utf-8")
    assert "# 说明: " in text and "字段：C" in text


def test_multiline_description_breaks_the_header_so_ui_must_flatten(tmp_path: Path) -> None:
    """**换行会打断注释头** —— 这正是界面必须把它拍平的原因。

    `# 说明:` 是注释头的**一行**：写进去一个换行，后面那半行就不以 `#` 开头了，
    引擎按格式把它当**公式正文**读走。这里把这个事实钉住（不是"期望的行为"，
    而是文件格式的硬约束）：所以 `ui/formula_page.py` 的 `_note_text()` 会把换行
    拍成空格，`tests/test_formula_page.py::test_note_with_newline_is_flattened`
    反过来验界面确实拍了。
    """
    lib.save_formula("多行", "C>MA(C,5)", description="第一行\n第二行", directory=tmp_path)

    spec = lib.formula_files(tmp_path)[0]
    assert spec.ok is False                      # 正文里混进了"第二行" → 编译不过
    assert "第二行" in spec.source
    # 拍平之后（界面走的就是这条路）一切正常
    lib.save_formula("多行", "C>MA(C,5)", description="第一行 第二行", directory=tmp_path)
    spec = lib.formula_files(tmp_path)[0]
    assert spec.ok and spec.description == "第一行 第二行"


def test_scorecard_library_capability_survives_the_ui_removal(tmp_path: Path) -> None:
    """界面拿掉了成绩单入口，但**库能力还在**（CLI `--scorecard` 与长样本回测要用）。"""
    assert callable(lib.run_scorecard)
    assert lib.DEFAULT_CONVENTION_KEY == "B"


# ══════════════════════════════════════════════════════════════════════════
# 8) 统一策略列表的备注/行（纯函数，不需要 Qt）
# ══════════════════════════════════════════════════════════════════════════
#
# 这一节测的是「策略列表」里那两列的**数据来源**：内置策略的备注只能搬现有字段
# （`rules` 的 evidence/evidence_note、`groups` 的组结论与停用理由），
# 公式的备注只能来自文件的 `# 说明:` —— 界面一个数字都不许编。


def test_builtin_row_helpers_are_gone() -> None:
    """内置策略那套「说明/顺序」函数**整体删掉**了（防止老路径回流）。

    2026-09-18：用户把 5 条内置策略改成了随包公式（可改可删），于是
    `builtin_order()` / `builtin_enabled()` / `builtin_strategy_note()` /
    `builtin_strategy_tip()` / `builtin_strategy_detail()` 都没有调用方了 ——
    它们原来只服务"列表里那 5 行只读的内置策略"，那 5 行已经不存在。
    这条用例的价值就是：谁哪天顺手把内置策略行加回来，这里立刻红。
    """
    from laoa_trader.ui import formula_page as fp

    for name in ("builtin_order", "builtin_enabled", "builtin_strategy_note",
                 "builtin_strategy_tip", "builtin_strategy_detail",
                 "ROW_BUILTIN", "MENU_DELETE_BUILTIN", "OFF_GROUP_KEY"):
        assert not hasattr(fp, name), f"{name} 应该已经随内置策略行一起删掉"
    # 行类型只剩两种
    assert fp.ROW_FORMULA and fp.ROW_AUCTION


def test_build_strategy_rows_marks_auction_and_formula_rows(formulas_cfg: Config,
                                                           tmp_path: Path) -> None:
    """一次建表：**竞价策略那一行在最前面**，公式在后（说明来自文件）。

    2026-09-18（用户要求）：内置策略改成了随包公式，所以列表里不再有那种
    "只读的内置策略行"—— 固定行只剩竞价策略一条，其余全是公式（随包的几条也在里面）。
    """
    from laoa_trader.ui import formula_page as fp

    _write_formula(tmp_path, "放量上攻", "C>MA(C,5)", "站上5日线")
    formulas_cfg.enabled_formulas = ["放量上攻"]

    rows = fp.build_strategy_rows(formulas_cfg, lib.formula_files(tmp_path), {})

    assert [row.kind for row in rows] == [fp.ROW_AUCTION, fp.ROW_FORMULA]
    auction = rows[0]
    assert auction.is_auction and auction.read_only
    assert auction.key == fp.AUCTION_KEY and auction.name == fp.AUCTION_NAME
    assert auction.enabled is False                    # 竞价默认关（intraday_auction=false）
    assert "不参与选股" in auction.note_tip and "无法回测" in auction.note_tip
    formula = rows[-1]
    assert formula.key == "放量上攻" and formula.note == "站上5日线"
    assert formula.enabled is True                     # 勾了才为真（写回 enabled_formulas）
    assert formula.spec is not None and formula.detail == ""


def test_build_strategy_rows_keeps_bundled_formulas_in_the_same_list(
        formulas_cfg: Config, tmp_path: Path) -> None:
    """随包公式与用户自己写的公式**同一条待遇**（都在公式那一段里，可改可删）。

    这是"内置策略改成随包公式"这件事的验收点：用户在列表里看到的随包公式
    （例如 `短期反转`）与他自己存的公式没有区别 —— 都是 `ROW_FORMULA`、
    都能载入编辑器、都能删。所以这条用例把两类公式放一起建表，断言行类型完全一致。
    """
    from laoa_trader.ui import formula_page as fp

    _write_formula(tmp_path, "短期反转", "C>MA(C,5)", "随包的那条")
    _write_formula(tmp_path, "我的公式", "C<MA(C,5)", "我自己写的")

    rows = fp.build_strategy_rows(formulas_cfg, lib.formula_files(tmp_path), {})

    assert [row.kind for row in rows] == [fp.ROW_AUCTION, fp.ROW_FORMULA, fp.ROW_FORMULA]
    bundled, mine = rows[1], rows[2]
    assert {bundled.kind, mine.kind} == {fp.ROW_FORMULA}
    assert bundled.spec is not None and mine.spec is not None
    for row in (bundled, mine):
        assert row.read_only is False                  # 不是只读行 → 可删可改
        assert not row.is_auction


def test_build_strategy_rows_shows_broken_and_runtime_errors(formulas_cfg: Config,
                                                             tmp_path: Path) -> None:
    """坏公式的两类问题都要在列表里看得见：语法错（文件解析）与运行期出错（上一轮）。"""
    from laoa_trader.ui import formula_page as fp

    _write_formula(tmp_path, "坏公式", "C>MAA(C,5)")
    _write_formula(tmp_path, "跑崩的", "C>MA(C,5)")

    rows = fp.build_strategy_rows(formulas_cfg, lib.formula_files(tmp_path),
                                  {"跑崩的": "3 只票算不出来：本地涨停池还没攒够"})
    by_name = {row.name: row for row in rows}

    assert "⛔ 语法错" in by_name["坏公式"].note
    assert "未知函数" in by_name["坏公式"].note and "第 1 行" in by_name["坏公式"].note_tip
    assert by_name["坏公式"].spec.ok is False
    assert "⚠️ 运行时出错" in by_name["跑崩的"].note
    assert "涨停池" in by_name["跑崩的"].note_tip


# ══════════════════════════════════════════════════════════════════════════
# 快照字段（流通市值 / 换手率）的接线：谁来取、什么时候取、取不到怎么说
#
# 2026-09-18 用户给的那条策略要用「流通市值 10-300 亿 + 换手率 > 5%」，
# 而这两个数**日线里没有**（同花顺的快照端点也不返回），只能从实时快照取一趟。
# 这几条钉住三件事：取到了就真的参与选股、**不用它的公式一个请求都不发**、
# 取不到时给一句人话（否则"勾了却没出票"会被当成公式写错）。
# ══════════════════════════════════════════════════════════════════════════


def _fake_snapshot(monkeypatch: pytest.MonkeyPatch, quotes: dict) -> list[list[str]]:
    """把 `sources.snapshot_map` / `supplement_map` 换成假的，并记录调用。"""
    from laoa_trader.data import sources

    calls: list[list[str]] = []

    def fake_map(_cfg, symbols=None):
        calls.append(list(symbols or []))
        return {code: dict(row) for code, row in quotes.items() if code in set(symbols or [])}

    monkeypatch.setattr(sources, "snapshot_map", fake_map)
    monkeypatch.setattr(sources, "supplement_map", lambda *a, **k: None)
    return calls


def test_preview_hits_uses_snapshot_fields(formula_db: str, formulas_cfg: Config,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """试算：`流通市值` / `换手率` 由**一趟快照**喂进来，条件真的生效。"""
    calls = _fake_snapshot(monkeypatch, {
        "600001": {"circ_mktcap": 50.0, "turnover_rate": 8.0},    # 合格
        "600002": {"circ_mktcap": 5.0, "turnover_rate": 8.0},     # 市值太小
        "600003": {"circ_mktcap": 60.0, "turnover_rate": 3.0},    # 换手不够
    })
    formula = fm.compile_formula("流通市值>=10 AND 流通市值<=300 AND 换手率>5")

    result = lib.preview_hits(formula, formula_db, cfg=formulas_cfg)

    assert {hit["symbol"] for hit in result["hits"]} == {"600001"}
    assert calls == [["600001", "600002", "600003"]]      # 只取一趟，且只要库里的票


def test_preview_hits_without_snapshot_fields_sends_no_request(
    formula_db: str, formulas_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不用这两个字段的公式一个请求都不发**（与"没这个功能"完全一样）。"""
    calls = _fake_snapshot(monkeypatch, {})
    formula = fm.compile_formula("C>MA(C,5)")

    result = lib.preview_hits(formula, formula_db, cfg=formulas_cfg)

    assert result["count"] == 2
    assert calls == []


def test_preview_hits_says_so_when_the_snapshot_is_missing(
    formula_db: str, formulas_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取不到快照 → 命中 0 只，但**必须说清原因**（否则会被当成公式写错）。"""
    _fake_snapshot(monkeypatch, {})
    formula = fm.compile_formula("流通市值>=10")

    result = lib.preview_hits(formula, formula_db, cfg=formulas_cfg)

    assert result["count"] == 0
    # 这句是**全局提示**（影响整次试算），不是"某只票算不出来" —— 放在 `notes` 里，
    # 界面才会把它单独渲染一行，而不是说成"1 只票算不出来"（票数是假的）
    assert any("市值/换手" in note for note in result["notes"]), result["notes"]
    assert result["errors"] == []


def test_snapshot_extra_skips_symbols_without_values(formula_db: str,
                                                     formulas_cfg: Config,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """快照里没有那两个数的票**不进 extra**（缺值由引擎按"条件不成立"处理）。"""
    _fake_snapshot(monkeypatch, {
        "600001": {"circ_mktcap": 50.0, "turnover_rate": 8.0},
        "600002": {"circ_mktcap": None, "turnover_rate": None},
    })

    extra, note = lib.snapshot_extra(formulas_cfg, ["600001", "600002"])

    assert extra == {"600001": {"流通市值": 50.0, "换手率": 8.0}}
    assert note == ""


def test_run_enabled_formulas_uses_snapshot_fields(
    formula_db: str, formulas_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """选股链路同样把快照喂进公式（`enabled_formulas` 里勾了它才会跑）。"""
    from laoa_trader.strategy import formula_group

    fx = formulas_cfg.data_dir / "formulas"
    fx.mkdir(parents=True, exist_ok=True)
    # 三个条件都写上，才能证明**两个字段都被喂进来了**（只写市值的话，600003 的
    # 60 亿也合格 —— 那只证明了一半）
    _write_formula(fx, "市值适中", "流通市值>=10 AND 流通市值<=300 AND 换手率>5")
    formulas_cfg.enabled_formulas = ["市值适中"]
    _fake_snapshot(monkeypatch, {
        "600001": {"circ_mktcap": 50.0, "turnover_rate": 8.0},
        "600002": {"circ_mktcap": 5.0, "turnover_rate": 8.0},
        "600003": {"circ_mktcap": 60.0, "turnover_rate": 3.0},
    })

    run = formula_group.run_enabled_formulas(formula_db, formulas_cfg, directory=fx)

    assert run.ran == ["市值适中"]
    # 候选挂在 `picks` 上，键是**合成策略名**（`公式·<公式名>`，与内置策略同一套写法）
    assert {pick["symbol"] for pick in run.picks["公式·市值适中"]} == {"600001"}
    assert run.status == {} and run.errors == []


def test_preview_hits_uses_hot_industries(formula_db: str, formulas_cfg: Config,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """试算：`热门行业` 按**行业**查表填进去，条件真的生效（读库、不联网）。

    夹具库里三只票都是"银行"，所以只要把"银行"算成热门，三条都该入选；
    不算热门就一只都不选 —— 这样一次就把"字段通了没有"钉死了。
    """
    from laoa_trader import pool

    monkeypatch.setattr(lib, "hot_industry_counts",
                        lambda _db, **kw: {"银行": 2})
    formula = fm.compile_formula("热门行业>=1")

    hit = lib.preview_hits(formula, formula_db, cfg=formulas_cfg)
    assert {h["symbol"] for h in hit["hits"]} == {"600001", "600002", "600003"}

    monkeypatch.setattr(lib, "hot_industry_counts", lambda _db, **kw: {"煤炭": 3})
    miss = lib.preview_hits(formula, formula_db, cfg=formulas_cfg)
    assert miss["count"] == 0
    # 顺带确认它走的是 `hot_industries` 那一套（不是自己另发明一个口径）
    assert callable(pool.hot_industries)


def test_hot_industry_counts_is_the_union_over_the_window(formula_db: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """"最近 3 天上榜的" = **按天各算一次再并集**，并记下上过几次。

    用户 2026-09-18 定的口径（原话"最近3天上榜的"）：只看当天会把一天的脉冲当热门。
    """
    from laoa_trader import pool

    days = {"2026-09-11": {"银行", "煤炭"}, "2026-09-10": {"银行"}, "2026-09-09": {"券商"}}

    def fake_hot(_db, top=12, momentum_window=5, day=None):
        return {name: {} for name in days.get(day, set())}

    monkeypatch.setattr(pool, "hot_industries", fake_hot)
    # 让"最近 N 个交易日"查到夹具库里的那三天
    from laoa_trader.data import storage
    with storage.connect(formula_db) as conn:
        storage.write_limit_up_pool(conn, [
            ("2026-09-09", "600001", "甲样本", 1, None, None, None, None, None, None,
             None, None, None, None, None, None, None, None, 0, "test", "2026-09-09"),
            ("2026-09-10", "600001", "甲样本", 1, None, None, None, None, None, None,
             None, None, None, None, None, None, None, None, 0, "test", "2026-09-10"),
            ("2026-09-11", "600001", "甲样本", 1, None, None, None, None, None, None,
             None, None, None, None, None, None, None, None, 0, "test", "2026-09-11"),
        ])

    counts = lib.hot_industry_counts(formula_db, days=3)

    assert counts == {"银行": 2, "煤炭": 1, "券商": 1}
