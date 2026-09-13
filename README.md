# 老A法师 · 交易终端（Windows 单机版）

一个**独立运行在 Windows 上**的 A 股选股 + 盯盘 + 提醒程序。
和 NAS 上那套（`选股/`）是**两个项目**：那套是服务器常驻版（Docker + 飞书），
这套是桌面版（双击运行、自带数据、Windows 原生通知）。

## 为什么要有这个版本

| 需求 | NAS 版 | 本版本 |
|---|---|---|
| 数据自己下 | 需要会 Docker | ✅ 首次运行自动下载并导入 5 年历史（进度条，可改 10 年） |
| 通知 | 只有飞书 | ✅ **飞书 + Windows 原生弹窗 + 托盘，并行三路** |
| 界面 | 浏览器 | ✅ 桌面窗口 + 托盘（池子/信号/持仓/条件单） |
| 未来接 QMT 自动交易 | 要另配 Windows 机 | ✅ 就在本机（QMT 是 Windows-only） |

## 每天几点自动跑？（分发后**每个人在界面里自己设**）

程序带一个默认节奏：**每天 16:00 自动跑一次**「数据增量 → 策略 → 建池 → 通知」，
**19:15 补跑一次**（只在主跑没成功时才补）。分发出去之后，每个人都可以按自己的作息改：

**界面** → 「设置」页 → 「自动运行」：

- `每天自动运行`（默认开）：关掉就只在你点【立即选股并建池】时跑；
- `主跑时间`：默认 `16:00` —— A 股 15:00 收盘，收盘后当天数据才齐；
- `补跑时间`：默认 `19:15` —— 主跑没成功时到这个点再试一次（必须晚于主跑）。

改完点【保存自动运行设置】**立即生效、不用重启**（调度线程下一轮就按新时间判断），
状态栏右侧会写着「下次自动运行：今天/明天 16:00」，一眼就知道设置生效了。

校验规则（界面与命令行一致）：

| 情况 | 行为 |
|---|---|
| 不是 `HH:MM`（如 `25:99`） | **拒绝保存**并提示格式，`config.toml` 不动 |
| 补跑 ≤ 主跑 | **拒绝保存**（否则补跑会先于主跑，逻辑矛盾） |
| 主跑早于 15:00 收盘 | 允许保存，但**明确提示**"当天数据可能还没出全，建议 16:00 之后" |

配置文件与命令行：

```toml
auto_run = true             # 关掉则只手动跑
run_at = "16:00"            # 每天主跑（收盘后）
run_at_fallback = "19:15"   # 主跑没成功时的补跑（留空 = 不补跑）
```

```bat
.venv\Scripts\python -m laoa_trader --cli --run-at 16:30        :: 临时改（不写配置，调试用）
.venv\Scripts\python -m laoa_trader --cli --run-at-fallback 20:00
.venv\Scripts\python -m laoa_trader --cli --no-auto-run         :: 临时关掉自动运行
.venv\Scripts\python -m laoa_trader --cli --doctor              :: 看当前生效的时间与"下次自动运行"
```

## 首次运行 / 数据自检（有库就不下载）

启动（GUI 或 CLI）时先跑一次**纯本地、秒级、不联网**的自检，结论是三态：

| 状态 | 判据 | 动作 |
|---|---|---|
| **ready** | 表齐全 + 跨度 ≥4.5 年（`min_history_years`）+ 最新交易日 ≥4000 只 + 有复权事件 + 行业覆盖 ≥90% + 有交易日历 + 不落后 | **一次下载请求都不发**，直接可用 |
| **needs_incremental** | 上面都满足，但落后 **1~10** 个交易日 | 只跑一次 `daily-k-10d` 增量（1 次请求） |
| **needs_full** | 缺表/空库/跨度或股票数不足/**缺复权事件**/行业覆盖低/缺日历/**落后 >10 个交易日** | 重新下载并导入（默认 5 年） |

> 为什么"落后 >10 天"要判全量：`daily-k-10d` 只覆盖近 10 个交易日，落后更多天**增量补不回来**（官方文档明确写了窗口）。
> 为什么"缺复权事件"不能算就绪：后复权价由复权因子算出，事件表空了价格就是不复权的，跨除权日的收益率会凭空多一截。

各端行为：

- **GUI 启动**：状态栏先显示"正在检查本地数据…"，然后——
  - `ready` → 状态栏打结论（如"本地数据就绪：503 万行 / 最新 2026-09-11"）；
  - `needs_incremental` → 提示落后几天；`auto_download_on_start=true`（默认）时**后台自动**跑增量；
    关掉则只提示，点【只刷新数据】可手动补；
  - `needs_full` → 弹**首次向导**：显示原因 → 填/确认 API Key 与数据目录 → 【开始下载】
    （进度条、可【取消下载】、**可中断续传**）→ 下完自动跑一次选股建池。
- **CLI**：`--doctor` 打印三态结论与各项指标；`--once`/`--pool`/`--serve` 进正题前先自检——
  `needs_full` 且没给 `--auto-download` 时**明确报错退出**（不会静默跑出空结果）；
  `--download` 在就绪时直接跳过（要强制重下加 `--force-download`）。

```toml
history_years = 5               # 首次导入几年历史（改 10 就是 10 年库）
min_history_years = 4.5         # 跨度门槛（必须 < history_years）
auto_download_on_start = true   # 缺数据时是否自动下载（增量自动；全量始终需确认或 --auto-download）
min_symbols = 4000
max_stale_trading_days = 0      # ready 允许落后的交易日数（>0 则该范围内也算 ready）
```

> **为什么默认只导 5 年**：10 年全市场约 1028 万行、库与内存都偏大，分发给别人偏重。
> 同花顺 `daily-k` dump 本身是固定的 10 年数据集（**没法只下 5 年**），所以做法是
> **照常下载整个 dump，导入时只写最近 `history_years` 年的行**；
> 复权事件（5.7 万条）**永远全部保留**，否则窗口起点之前的除权算不进去，整段后复权价会差一个常数。
> 想要长样本回测：把 `history_years` 改成 `10`（下载文件一样，只是导入更多行）。

## 选哪几组策略（三组，可自选）

5 条策略按"持有期 + 证据强度"分三组，**只做你有时间执行的那一档**：

| 组 key | 显示名 | 持有期 | 成员 | 依据（10 年样本） |
|---|---|---|---|---|
| `ultra` | 超短·隔日 | T+2 | 连板回踩低吸 | T+2 α **+0.47%**(t=2.02)，唯一在可执行最短持有期上显著 |
| `short` | 短线·T+3 | T+3 | 短期反转、地量后放量变盘、首板缩量整理 | T+2/T+3 t≈1.7~2.1 |
| `swing` | 波段·T+10 | T+10 | 低价股 | T+3 +0.13%(t=3.50)、T+10 +0.32%(t=4.43)，四个持有期全显著 |

两种方式改（**都不用记语法**）：

1. **界面** → 「设置」页 → 勾选策略组 / 成员策略 → 【保存策略组设置】（写回 `config.toml`，你的注释不会丢）；
2. **config.toml**：

```toml
enabled_groups = ["ultra", "swing"]      # 只要隔日 + 波段
enabled_strategies = []                  # 空 = 该组全选
# enabled_strategies = ["低价股"]        # 也可以点名到策略（中文名或类名都认）
```

**两组都空 = 全选**（安全默认，配置没写不会一条都不跑）。选择会**贯穿全链路**：
跑策略 → 落 `signal` → 建池 → 盘中提醒的观察池，都只围绕启用的组；
池子表格里也有「组别」列，能一眼看出每只标的属于哪一组。

临时想换个组合跑一次（**不改配置**）：

```bat
.venv\Scripts\python -m laoa_trader --cli --list-groups              :: 看有哪些组/策略
.venv\Scripts\python -m laoa_trader --cli --once --groups ultra,short
.venv\Scripts\python -m laoa_trader --cli --once --strategies 低价股,连板回踩低吸
```

## 自选股：和策略标的并列进池，一起实时监控

池子成员现在有**两类**：策略标的 + 你自己加的自选股。自选股：

- **不受热门行业过滤**（你选的，就是要盯）；
- **不占策略名额**（池子大小只限制策略标的，自选另有上限 `watchlist_max`，默认 20）；
- 同一只**既是策略选中又是自选** → 池子里只出现一行，来源标成「策略+自选」；
- 停用（`enabled=0`）的不进池、不监控，但仍留在列表里，随时能再打开；
- **即使策略池为空也照常盯**（想只做自选：`enabled_groups = ["none"]`）；
- 盘中买卖点与通知频道完全沿用同一套：有持仓 → 用**成本**算止损止盈；
  没有 → 用**前一交易日收盘**，并适用「池内回踩买点 / 放量突破20日高」；
  提醒文案带备注，例如「自选（龙头，成本 12.40）」。

三种用法：

1. **界面** → 「自选股」页：填代码（**名称自动从本地库补**）+ 备注 → 【加自选】；
   选中列表某行后可【删除自选】/【启用】/【停用】；「是否已进池」列一眼看到状态。
2. **命令行**：

```bat
.venv\Scripts\python -m laoa_trader --cli --watchlist add 600519 --note 龙头
.venv\Scripts\python -m laoa_trader --cli --watchlist list
.venv\Scripts\python -m laoa_trader --cli --watchlist disable 600519
.venv\Scripts\python -m laoa_trader --cli --watchlist enable 600519
.venv\Scripts\python -m laoa_trader --cli --watchlist remove 600519
```

3. **config.toml**：

```toml
watchlist_max = 20        # 自选股上限（超限会提示，不静默丢弃）
watchlist_in_pool = true  # false = 只记录不监控
enabled_groups = ["none"] # 可选：只盯自选股，不跑策略
```

池子上会显示**来源**（`波段·T+10（T+10）` / `自选` / `波段·T+10（T+10） + 自选`）与**备注**；
`--cli --pool` 的输出同样能看出每只的来源与备注。

## 通知方式自己定（频道自选）

`config.toml` 里是一张**频道清单**，三选任意组合，**空列表 = 只入库不推送**：

```toml
notify_channels = ["windows", "feishu", "tray"]
notify_windows_sound = true        # 弹窗提示音
notify_windows_open_url = true     # 弹窗按钮点击打开雪球
notify_tray_duration_ms = 8000     # 托盘气泡时长
feishu_on = true                   # 飞书频道总开关（凭证没配会自动跳过）
```

- 发送是**并行且互不影响**的：飞书挂了弹窗照出，反之亦然；任何一路失败只记日志；
- 没配飞书凭证却勾了飞书：状态栏提示「未配置飞书凭证，已跳过」，**不报错、不影响其它频道**；
- 界面「设置」页可以直接勾频道、填飞书凭证、调参数、点【发送测试提醒】**实发一条**
  （用页面上当前勾选，**不要求先保存**）；
- 保存设置只就地改那几个键 —— `config.toml` 里你自己写的注释、备注、将来版本的键**都会保留**。

## 首次运行会做什么

1. 建本地库 `%LOCALAPPDATA%\LaoATrader\data\trader.db`（不复权原始价 + 复权事件 + 后复权视图）；
2. **下载历史数据**：同花顺全市场日K dump（10 年、约 1028 万行）+ 复权事件（5.7 万条）
   → **导入最近 5 年**（约 500 万行）→ 本地算后复权 → 入库
   （约 8~15 分钟，有进度条，可中断续传；想要 10 年把 `history_years` 改成 10）；
3. 同步交易日历、行业归属（90 个一级行业）、指数日线；
4. 之后每天：19:15 跑策略建池、盘后增量更新；交易时段每分钟盯池提醒。

「**重新下载历史数据**」按钮随时可以再跑一次（增量：只补缺失的日期）。

## 界面上随时可以跑

- **【立即选股并建池】**：可选的数据增量 → 策略 → 建池 → 按通知设置推送（后台线程 + 进度条）；
- **【只刷新数据】**：只跑增量同步（行情/涨停池/日历/行业/指数），不选股、不推送；
- **【下载/更新历史数据】**：首次建库/补历史（默认导入 5 年，可中断续传）；
- **【立即检查盘面】**、**【暂停盘中提醒】**：盘中提醒的手动触发与暂停；
- **「自选股」页**：加/删/启用/停用自选股（名称自动补全，可写备注），并显示是否已进池；
- **「设置」页 → 自动运行**：改每天主跑/补跑时间与开关，保存后立即生效，状态栏显示下次运行时间；
- 所有耗时操作都在后台线程，**失败不弹错误框、不崩界面**，状态栏给出中文原因。

## 通知（多频道并行）

- **飞书**：卡片（与 NAS 版同一套文案，含条件单参数）
- **Windows 通知**：`winotify` 原生弹窗，点击直接跳到对应标的（可关提示音）
- **托盘**：气泡 + 状态灯（时长可调）

频道在 `config.toml` 的 `notify_channels` 里自选，也可以全关（只入库不推送）；
各频道参数见上面「通知方式自己定」。

## 目录结构

> 本项目就是一个**独立文件夹 `laoA/`**，拷走它就能打包/分发（`.venv`、缓存与测试临时目录都已经排除在交付物之外）。

```
laoA/
├─ config.example.toml 配置样例（复制成 config.toml 用）
├─ src/laoa_trader/
│  ├─ __main__.py      入口：无参数进 GUI，--cli 走命令行
│  ├─ config.py        配置（config.toml + 环境变量覆盖）
│  ├─ log.py           日志（控制台 + 轮转文件，桌面版要能翻日志）
│  ├─ data/
│  │  ├─ hithink.py    同花顺 REST 客户端（凭据/信封校验/重试/dump）
│  │  ├─ sync.py       下载与更新：按 history_years 导入历史、日更增量、日历/行业/指数
│  │  ├─ preflight.py  运行时自检（ready / needs_incremental / needs_full，纯本地不联网）
│  │  ├─ storage.py    SQLite（原始价 + 复权因子 + 后复权视图、全部幂等 upsert）
│  │  └─ engine.py     读库门面（策略用的 db_path / get_active_symbols）
│  ├─ strategy/
│  │  ├─ groups.py     策略分组（超短·隔日 / 短线·T+3 / 波段·T+10）与自选解析
│  │  ├─ rules.py      入选策略（低价股 / 连板回踩 / 短期反转 / 地量放量 / 首板缩量）
│  │  └─ factors.py    量价因子
│  ├─ pool.py          股票池 = 策略标的（热门行业过滤+权重） + 自选股（豁免过滤、不占名额）
│  ├─ intraday.py      盘中买卖点规则 + 条件单参数（plan_buy / plan_sell）
│  ├─ notify/          通知频道（windows / feishu / tray，可任选，并行互不影响）
│  ├─ scheduler.py     每天定时日更（主跑 + 主跑失败时补跑）+ 盘中轮询（start/stop/status）
│  └─ ui/app.py        PySide6 主窗口（PySide6 缺失时自动降级为 CLI）
├─ build/build.bat     一键打包（Windows 上双击）
├─ build/laoa_trader.spec
└─ tests/              离线测试（不联网、不需要 Key 也能跑）
```

> **详细打包教程（含分发与踩坑）见 [`docs/打包教程.md`](docs/打包教程.md)**
> 一句话版本：`py -3.11 -m venv .venv` → `pip install -e ".[dev]"` → `pytest tests -q`（期望 445 passed）
> → `build\build.bat` → 产物 `dist\LaoATrader\LaoATrader.exe`。
> **只运行 exe 的人不需要装 Python**；打包的人才需要 Python 3.11。

## 在 Windows 上跑起来

```bat
:: 0) 进入项目目录（就是一个独立文件夹，方便打包分发）
cd laoA

:: 1) 开发模式（需要 Python 3.11）
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .

:: 2) 填配置：复制 config.example.toml 为 config.toml，填同花顺 API Key
copy config.example.toml config.toml

:: 3) 启动（无参数 = 图形界面）
.venv\Scripts\python -m laoa_trader
```

命令行模式（首次建库建议先在命令行跑一次，能看到下载进度）：

```bat
cd laoA
.venv\Scripts\python -m laoa_trader --cli --download   :: 下载并导入历史（默认 5 年，约 8~15 分钟，可续传）
.venv\Scripts\python -m laoa_trader --cli --once       :: 数据增量 + 选股 + 建池 + 推送
.venv\Scripts\python -m laoa_trader --cli --pool       :: 看当前股票池（含组别）
.venv\Scripts\python -m laoa_trader --cli --serve      :: 常驻：定时日更 + 盘中提醒
.venv\Scripts\python -m laoa_trader --cli --list-groups :: 列出策略组与成员
.venv\Scripts\python -m laoa_trader --cli --doctor      :: 自检：路径/依赖/凭据/数据概况 + 数据三态结论
.venv\Scripts\python -m laoa_trader --cli --once --auto-download :: 数据不可用时先下载再继续
```

`--groups` / `--strategies` 是**临时覆盖**（配置文件不动），随时可以换个组合跑一次。
`--once` 与 19:15 的定时任务走的是**同一个流程**：同一天重复跑不会产生重复行，
也不会重复推同一批卡片（按内容指纹去重）。

## 跑测试

离线单元测试（**不联网、不需要 API Key、不消耗同花顺配额**）：

```bat
:: 先进入项目目录
cd laoA

:: Windows：pip install -e . 已装好 pandas/pyarrow，只差 pytest
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest tests -q

:: Linux / macOS（交付包里不带 venv，先自己建一个临时环境）
python3 -m venv /tmp/laoa-venv
/tmp/laoa-venv/bin/pip install pandas numpy pyarrow pytest requests PySide6
PYTHONPATH=src /tmp/laoa-venv/bin/python -m pytest tests -q
```

> 装 `pyarrow` 不能省：dump 相关用例要么真跑（需要它读写 Parquet），要么整组跳过；
> 装 `PySide6` 则让 47 条界面用例真跑（离屏），否则那一整个文件跳过。
> 依赖装齐时是 **506 passed**；跑完记得把 `__pycache__` / `.pytest_cache` 删掉再打包
> （`.gitignore` 已列出，交付目录里不该出现这些）。

> 测试套件在 **socket 层封死了 IPv4/IPv6**（`tests/conftest.py` 的 `_block_network`，autouse）：
> 任何漏网的客户端都会**报错**而不是悄悄打真实接口。`tests/test_offline.py` 还会验证这把锁
> 真的锁上了（TCP、DNS、裸 requests、真实 `HithinkClient` 全部打不出去）。
> 因此日志里出现的 `[5001] 限流` 之类字样是**假客户端合成的错误**，不是真实请求。

覆盖：后复权公式与增量因子一致性、代码/时间转换、股票池合成与热门行业过滤、
**自动运行时间（界面保存/校验/改完立即生效/主跑与补跑/成功标记/CLI 临时覆盖）**、
**历史导入窗口（按 history_years 过滤 / 复权事件全留 / 门槛联动 / 配置矛盾提示）**、
**运行时自检（三态判据每个分支 / ready 时零下载 / needs_full 报错退出 / --auto-download 先下载再跑）**、
**自选股（豁免热门行业 / 不占名额 / 去重来源 / 策略池为空也盯 / 上限提示 / 停用恢复 / 自动补名称）**、
条件单参数（整手 / 止损止盈 / 买不起一手告警）、盘中提醒去重、
**策略分组与自选（组/策略过滤贯穿策略→信号→池子→观察池）**、
**手动跑与定时跑的幂等（不重复行、不重复推送）**、
**通知频道自选（空列表不发、单频道失败不影响其它）**、
**设置写回 config.toml 保留注释与未知键**、
CLI 入口（`--doctor` / `--pool` / `--once` / `--download` / `--groups` / `--strategies`
都不吐 traceback），以及界面冒烟测试（离屏跑主窗口；装了 PySide6 才执行，否则跳过）。


## 打成 exe 分发

> 分发包默认**只导入 5 年历史**（约 500 万行）：同花顺 dump 本身是 10 年数据集，
> 导入时按 `history_years=5` 过滤，库与首次下载都轻一半 —— 收到包的人想要 10 年，
> 改 `config.toml` 的 `history_years = 10` 重新下载即可。

```bat
cd laoA
build\build.bat
:: 产出 dist\LaoATrader\LaoATrader.exe（目录版，启动快）
:: 想做成单文件加 --onefile（启动慢、杀软误报多，不推荐）
```

首次运行 exe 时同样会走"下载历史数据"向导。

> 打包细节见 `build/laoa_trader.spec` 顶部注释：onedir（启动快、杀软误报少）、
> `--noconsole`（不弹黑框，日志写到 `<数据目录>\logs\laoa-trader.log`）、
> `config.example.toml` 随包分发。exe 旁边的 `config.toml` 就是它的配置。

## 与 NAS 版的关系

- **策略与规则完全同源**：都是 10 年样本检验过的同一批规则（低价股 T+10、连板回踩 T+2、
  短期反转、地量后放量、首板缩量），参数一致；
- **数据协议一致**：同花顺 REST（同一个 Key）+ 本地 SQLite + 自算后复权；
- 两套可以同时跑（互不干扰），也可以只用其中一套。
