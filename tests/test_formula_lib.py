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

import sys
from pathlib import Path

import pytest

from laoa_trader import formulas as lib
from laoa_trader import intraday, pool
from laoa_trader.config import Config, save_settings
from laoa_trader.data import storage
from laoa_trader.strategy import formula as fm
from laoa_trader.strategy import formula_group, groups, rules
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
    # 仓库里那三条示例公式就在里面（"载入示例"靠它）
    assert {spec.name for spec in lib.formula_files(folder)} >= {"放量上攻", "均线多头排列"}


def test_formula_dir_frozen_uses_exe_sibling(tmp_path: Path, monkeypatch) -> None:
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
    # 空目录时会自动把随包示例复制进来（否则新用户打开是空列表，第一步就走不下去）
    assert {spec.name for spec in lib.formula_files(folder)} >= {"放量上攻"}


def test_formula_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    """环境变量优先（换机器 / 放共享盘）。"""
    target = tmp_path / "我的公式"
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    assert lib.formula_dir() == target
    assert target.is_dir()


def test_formula_dir_does_not_overwrite_user_files(tmp_path: Path, monkeypatch) -> None:
    """用户目录里已经有公式时，**绝不**再往里复制示例（不能覆盖用户的东西）。"""
    target = tmp_path / "formulas"
    target.mkdir(parents=True)
    mine = target / "我的.txt"
    mine.write_text("# 名称: 我的\nC>MA(C,5)\n", encoding="utf-8")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(target))

    lib.formula_dir()

    assert [p.name for p in target.iterdir()] == ["我的.txt"]


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
    """最多列 N 只（`limit`），并且带中文名（小白只认名字）。"""
    formula = fm.compile_formula("C>MA(C,5)")

    result = lib.preview_hits(formula, formula_db, limit=1)

    assert result["count"] == 2 and result["shown"] == 1
    assert result["hits"][0]["name"] == "甲样本"


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


def test_bad_formula_is_isolated_from_builtin_strategies(engine, formulas_cfg: Config,
                                                         tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """**失败隔离**：公式在运行期抛错 → 好公式照常进池、内置策略照常出票、建池不失败。

    做法：让两条公式在求值时分别抛 `FormulaError` 与 `FormulaDataError`
    （真实场景就是"连板() 用的本地涨停池还没攒够历史""某只票的字段长度对不上"）。
    如果没有 `formula_group` 里那层逐票兜住，异常会一路冒到 `build_pool` ——
    用户自己写坏的一条公式，会把整轮选股连同 5 条内置策略一起带走。
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

    selection = groups.resolve_from_config(formulas_cfg)
    picks, errors = rules.run_all(engine, formulas_cfg, top_n=200, selection=selection)
    report: dict = {}
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False, picks=picks,
                           selection=selection, report=report)

    assert rows, "内置策略必须照常出票"
    assert any(not groups.is_formula_strategy(row["strategy"]) for row in rows)
    # 好公式照常进池，两条坏公式不进池但**都有原因**
    assert "公式·好公式" in {row["strategy"] for row in rows}
    assert "公式·坏公式甲" not in {row["strategy"] for row in rows}
    assert "公式·坏公式乙" not in {row["strategy"] for row in rows}
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

    selection = groups.resolve_from_config(formulas_cfg)
    picks, _errors = rules.run_all(engine, formulas_cfg, top_n=200, selection=selection)
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False, picks=picks,
                           selection=selection)

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
    # 推送范围：公式标的照常推送（不是 open_only，不会因为 push_only_proven 被丢）
    formulas_cfg.push_only_proven = True
    keep, skipped = pool.split_push_rows(pool.pool_table_rows(formulas_cfg.db_path),
                                         formulas_cfg)
    assert any(row["strategy"] == "公式·收盘在5日线上" for row in keep)


def test_not_enabled_formula_does_not_change_pool(engine, formulas_cfg: Config, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """**默认不参与**：目录里有公式、但没勾 → 池子里一条公式标的都没有。"""
    folder = tmp_path / "formulas"
    _write_formula(folder, "收盘在5日线上", "C>MA(C,5)")
    monkeypatch.setenv(lib.FORMULA_DIR_ENV, str(folder))
    assert formulas_cfg.enabled_formulas == []

    selection = groups.resolve_from_config(formulas_cfg)
    picks, _errors = rules.run_all(engine, formulas_cfg, top_n=200, selection=selection)
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False, picks=picks,
                           selection=selection)

    assert all(not groups.is_formula_strategy(row["strategy"]) for row in rows)


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

    selection = groups.resolve_from_config(formulas_cfg)
    picks, _errors = rules.run_all(engine, formulas_cfg, top_n=200, selection=selection)
    rows = pool.build_pool(engine, formulas_cfg, size=10, hot_only=False, picks=picks,
                           selection=selection)
    symbol = next(row["symbol"] for row in rows if row["strategy"] == "公式·收盘在5日线上")

    targets, symbols = intraday.watch_targets(formulas_cfg.db_path, selection=selection,
                                              cfg=formulas_cfg)

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
    assert formulas_cfg.enabled_formulas == ["放量上攻"]


def test_groups_place_formula_in_its_own_group() -> None:
    """公式是一个**独立的组**（与 short 等并列），但不进 `GROUPS` 那张静态表。"""
    assert groups.group_of("公式·放量上攻") == groups.FORMULA_GROUP_KEY
    assert groups.group_label(groups.FORMULA_GROUP_KEY) == "公式"
    assert groups.group_horizon(groups.FORMULA_GROUP_KEY) == 0
    assert groups.formula_name_of("公式·放量上攻") == "放量上攻"
    assert groups.is_formula_strategy("LowPriceStrategy") is False
    assert groups.is_formula_strategy("公式·x") is True
    # 三张老表一个都没被改：内置策略的组顺序与权重保持不变（有既有用例钉住）
    assert groups.GROUP_ORDER == ("ultra", "short", "swing")
    assert "formula" not in groups.GROUPS
    assert pool.weight_of("公式·x") == formula_group.FORMULA_WEIGHT
    assert pool.weight_of("LowPriceStrategy") == groups.STRATEGY_WEIGHTS["LowPriceStrategy"]
