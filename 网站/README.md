# luweik决策系统 · 产品网站（纯静态）

给客户看的落地页，**不需要后端、不需要构建**，把整个 `网站/` 目录放到 NAS 上任意一个能被浏览器访问的地方就能用。

## 目录结构

```
网站/
├── index.html          单页落地页（所有文案在这里）
├── style.css           样式（自包含：无外部字体 / 图标库 / CDN）
├── script.js           只做一件事：检查 downloads/ 里有没有 zip，决定下载按钮是否可用
├── 截图/               10 张界面截图（从 ../docs/截图/ 拷贝过来的，别在这儿单独改）
└── downloads/          放安装包的地方
    ├── LuweikDecision-<版本>.zip   ← 带版本号的包（用户看到的就是这个名字）
    ├── LuweikDecision.zip          ← 固定名的副本（老链接/兜底用）
    └── latest.json                ← 告诉页面当前指向哪个包（换版本只改这里）
```

> **截图里的行情全是演示数据**（编的股票、价格、盈亏），不是真实行情也不是推荐。
> `01`~`07` 是**桌面合成图**：主窗口贴左上、桌宠（牧童骑牛）站在右下角，看着就是"桌宠陪着你盯盘"。
> 要重新生成：在仓库根目录跑 `QT_QPA_PLATFORM=offscreen python build/make_screenshots.py`，
> 它会用一份虚构的行情喂给界面（不碰真实配置与授权状态），出图在 `docs/截图/`，
> 拷过来覆盖本目录 `截图/` 即可。加了 `LUWEIK_SHOTS_DEBUG=1` 会把三张表的每个单元格打到终端，方便核对。

页面板块：首屏（一句话 + 下载 + 版本号）→ 解决什么问题 → 特色一 策略编辑 → 特色二 盘中监控 →
其它功能（大盘概览 / 自选标的 / 持仓监控 / 筛选结果 / 系统设置）→ 怎么开始用 → 授权与价格 →
常见问题 → 免责声明。

文案以 `../docs/软件介绍.md` 为底（定位：**行情软件的辅助工具**；两个主打特色：策略编辑、盘中监控），
改动请两边一起改，别让网站和文档说两套话。

## 在 NAS 上发布（三步）

1. 把 `网站/` 整个目录放到你想对外提供的路径，例如 `/vol3/1000/www/laoniuxuangu/`
   （或挂到已有 nginx 的站点目录下，加一个 `location /` 指向它即可）。
2. 想先自己看效果，最省事是用 Python 自带的静态服务器（**只是给你本机预览，不需要常驻**）：
   ```bash
   cd 网站 && python3 -m http.server 8080
   # 浏览器打开 http://<NAS 的 IP>:8080
   ```
   要长期对外，用已有的 nginx：把 `网站/` 设为 root，`index index.html;` 即可；
   `.png` 与 `.zip` 建议开 gzip/缓存头（截图约 1.5 MB，zip 上百 MB，缓存一下体验更好）。
3. 若将来要映射到公网，注意：站点里**没有任何后端与数据库**，只有静态文件；但别人能
   直接下到 zip，所以**别把注册机（keygen）放进 downloads/**。

## 要做的一件事：放安装包

把打包好的 `LuweikDecision.zip` 放进 `网站/downloads/`（文件名必须一致）。

- 有它：页面上的【下载 Windows 版】按钮自动可用，并显示文件大小（`script.js` 用 HEAD 探测）。
- 没有它：按钮显示「下载准备中，请联系作者微信 q352162」，**不会**出现点了 404 的死按钮。
- 换版本：直接用新 zip 覆盖这个文件，链接不用改（页面上版本号在 `index.html` 里写死成 `v1.1.0`，
  发新版时记得改那一处）。

## 更新截图

1. 在仓库根目录重抓（会给所有页面生成统一大小的图）：
   ```bash
   QT_QPA_PLATFORM=offscreen python build/make_screenshots.py
   ```
2. 抓完把 `docs/截图/*.png` 重新拷进 `网站/截图/`（文件名保持一致，`index.html` 就不用手改）。
3. 若新增了页面，记得在 `index.html` 里加一段 `<figure><img src="截图/xx-xxx.png" ...></figure>`。

## 自检（改完自己跑一遍）

```bash
cd 网站
python3 - <<'PY'
import re, pathlib
html = pathlib.Path("index.html").read_text(encoding="utf-8")
refs = re.findall(r'(?:src|href)="([^"]+)"', html)
local = [p for p in refs if not p.startswith(("http", "mailto:", "#"))]
# downloads/ 里的 zip 是发布时才放的，缺了不算错（按钮会显示「下载准备中」）
missing = [p for p in local if not pathlib.Path(p).exists() and not p.startswith("downloads/")]
print("缺失的本地资源：", missing or "无")
print("外部资源引用：", sorted({p for p in refs if p.startswith("http")}))
print("安装包已就位：", pathlib.Path("downloads/LuweikDecision.zip").exists())
PY
```

预期结果：缺失「无」、外部引用只有同花顺那一个站点、安装包那一行在发布后变成 `True`。
（首屏那张大盘概览与「其它功能」里那张是**同一个文件**，不重复存两份。）
