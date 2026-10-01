# 方案一：自建 PERM 案件级数据流水线（GitHub Actions 抓取 + 我们的站点展示）

这个目录就是**要推到 GitHub 仓库的全部内容**（`.github/workflows/`、`scripts/`、`requirements.txt`）。
目标：绕开"官方季度披露表对脚本一律 403"的限制——用 GitHub runner 上的**真实 Chromium** 下载，
聚合成小 JSON 提交回仓库，再由我们的 Cloudflare Worker 读 `raw.githubusercontent.com` 拿进来。

做完能多出这些现在没有的能力：按**递交月**看队列进度（哪个月的案子裁到哪一天）、
按**雇主首字母**的排队估算器、每月/每周裁决量与结果构成、更长的历史月份覆盖。
做不到的是 permupdate 那种"今日新增/今日处理"——那需要对 FLAG 做每日逐案轮询，另说。

---

## 隐私与安全的硬边界（不要改）

1. **只提交聚合结果**，绝不提交披露表原件、案号清单、雇主名称。披露表是公开的，
   但把它们整理成"可按雇主检索"的库会给人造成实际伤害，也没必要。
2. 仓库设为 **public** 只是为了让 Worker 无需凭据就能读；里面只有统计数字。
3. 抓取失败**不覆盖上一次数据**（脚本只在成功时写 `perm_case_stats.json`），
   页面永远显示"数据生成时间"，不会把旧数据当新的。
4. 提交用的是 workflow 内置的 `GITHUB_TOKEN`（`permissions: contents: write`），
   **不需要**把你的个人令牌交给 Actions。你给我的那个令牌只用于"建仓 + 首次推文件"。

---

## 第 0 步：先花 10 分钟验证 runner 能不能过 Akamai

这一步决定后面要不要投入，别跳过。

1. 打开 https://github.com ，右上角 `+` → **New repository**。
2. Repository name 填 `perm-data-feed`；Visibility 选 **Public**；勾上 **Add a README file**；
   其他都不动 → 点 **Create repository**。
3. 仓库页 → **Add file → Upload files**，把这个目录里的文件拖进去（保持目录结构）：
   - `.github/workflows/probe-dol.yml`
   - `.github/workflows/sync-perm.yml`
   - `scripts/fetch_perm_disclosure.py`
   - `requirements.txt`
   → 底部 **Commit changes**。
   （如果 GitHub 不让一次建 `.github` 目录，就分两次：先 **Create new file**，
   在文件名框里**整行输入** `.github/workflows/probe-dol.yml` 再粘贴内容，GitHub 会自动建目录。）
4. 仓库顶部 **Actions** → 左边选 **Probe DOL reachability** → 右侧 **Run workflow → Run workflow**。
5. 等 2~4 分钟，点开那次运行 → 看 **探测 performance 页面与最新文件** 这一步的输出，结论只有三种：
   - `结论：可达 …… 下载试探：HTTP 200` → **方案一成立**，继续第 1 步。
   - `结论：被拦 —— Access Denied` → runner 的 IP 被 Akamai 拒了。先点 **Run workflow** 再试 1~2 次
     （每次换一台 runner、换出口 IP）；连续被拦就说明这条路要换出口（美国服务器或别人的网络），
     先别投入后面的开发，回来告诉我结果。
   - `页面上没找到 PERM_Disclosure_Data_FYxxxx_Qx.xlsx` → 官方改版了，把日志最后几行发我，我改选择器。

---

## 第 1 步：正式跑聚合流水线

1. Actions → 左边选 **Sync PERM disclosure data** → **Run workflow → Run workflow**。
2. 跑完（约 3~6 分钟，文件几十 MB）后，仓库 Code 页应该多出：
   - `data/perm_case_stats.json`（几百 KB 以内）
   - `data/perm_status.json`（`"ok": true`、来源文件名、行数、最新裁决月）
3. 打开 `data/perm_status.json` 看 `last_decision_month`：如果比 `2026-06` 更靠近今天，
   说明我们拿到了比现有镜像**更新**的官方数据（这是方案一的主要收益之一）。
4. 定时任务已内建：每周一 10:00 UTC 自动跑。季度文件没更新时提交会显示"数据无变化"。

---

## 第 2 步：接进我们的站点（这一步由我在项目里改）

数据契约（`perm_case_stats.json` 的字段，脚本已按此产出）：

| 字段 | 含义 |
| --- | --- |
| `source` / `generated_at` / `rows_total` / `rows_decided` | 来源文件名、生成时间、总行数、已裁决行数 |
| `by_decision_month[]` | 按**裁决月**：`total certified certifiedExpired denied withdrawn medianDays p25Days p75Days` |
| `by_submit_month[]` | 按**递交月**队列：`received decided certified denied withdrawn medianDays p75Days` |
| `letter_progress[]` | 每个递交月 × 雇主首字母：`cases decided lastDecidedReceived medianDays`（估算器的依据） |
| `daily.decided[]` | 最近 45 天每天裁决量（真实） |
| `daily.received_biased[]` | 最近 45 天收到日分布，**只含已裁决案件**，越靠近今天越不完整 → 不能当新增量 |

要改的地方：

1. `cf/worker.js`：加 `PERM_CASES_FEED`（指向 `raw.githubusercontent.com/<你的用户名>/perm-data-feed/main/data/perm_case_stats.json`）、
   `cleanPermCases()`（校验月份格式、字母限 A–Z、数字用 `permNum` 规整、`letter_progress` 上限 400 条）、
   KV 键 `permcases`、路由 `/api/perm/cases`。**沿用现有的 20 小时节流 + 失败保留旧值 + 30 分钟冷却**，
   并且复用 `refreshPerm()` 的结构，别开第三条独立链路。改完 `copy cf\worker.js visa-bulletin-app\web\_worker.js`。
2. `visa-bulletin-app/export_perm.py`：同时抓两个 feed，写出 `web/data/perm.json` 与 `web/data/perm_cases.json` 兜底。
3. `visa-bulletin-app/web/`：`app.js` 里 `loadPerm()` 改成并行取 `/api/perm` 与 `/api/perm/cases`
   （cases 失败不影响大盘），新增两张卡：**递交月队列**（`by_submit_month` 的 decided/received 进度条）
   和**字母进度估算器**（输入递交日期 + 雇主首字母 → 用 `letter_progress` 找同递交月同字母的
   `lastDecidedReceived` 与 `medianDays/p75Days`，给区间而不是单点）。
   `index.html` 加卡片与说明，`styles.css` 复用 `.kpi/.meter/.chart-svg`，`sw.js` 的 SHELL 加
   `data/perm_cases.json` 并把 `VER` 递增。
4. 测试：`cf/test_local.mjs` 加 cases 清洗与端点断言（含"官方数据比镜像更新时页面显示新月份"）；
   `node cf/test_local.mjs` 与 `python -X utf8 visa-bulletin-monitor/selftest.py` 都要全绿。
5. 部署与验证：
   ```bat
   npx --yes wrangler pages deploy visa-bulletin-app\web --project-name=visa-bulletin --branch=main
   npx --yes wrangler deploy --config wrangler.worker.toml
   ```
   然后线上核对 `/api/perm/cases` 有数据、页面渲染、**浏览器控制台无 CSP 报错**
   （CSP 是 `style-src 'self'`，innerHTML 里不能写内联 style 属性）。

验收标准：新卡片在 375px 宽度无横向溢出；现有四个标签页功能不变；cases 接口挂掉时页面照常显示大盘；
`generated_at` 与"本站取数时间"都显示在页面上；估算器给的是区间且带"不是承诺"的措辞。

---

## 助手脚本：`github_push.py`（在项目根目录，不在本仓库里）

只要 `github-secrets.txt` 填好，剩下的网页操作都可以由它代做（只走 api.github.com，本机实测这条链路 1~2 秒，而 github.com 网页这条很慢、经常十几秒）：

```bat
python -X utf8 github_push.py --check    :: 令牌可用？仓库存在？
python -X utf8 github_push.py --push     :: 把本目录 6 个文件推到仓库（可反复执行，会自动带 sha）
python -X utf8 github_push.py --probe    :: 触发可行性验证并等结果，直接把日志里的「结论」行打出来
python -X utf8 github_push.py --sync     :: 触发正式抓取聚合
python -X utf8 github_push.py --runs     :: 最近 8 次 Actions 运行
python -X utf8 github_push.py --status   :: 读仓库里的 data/perm_status.json
```

脚本不打印令牌，网络抖动会自动重试 4 次。

## 我需要你给的东西

只有一样：一枚 **GitHub Fine-grained PAT**，用于建仓和首次推送（第 0 步你也可以全手工做，那就完全不用给令牌）。

1. 右上角头像 → **Settings** → 左侧最底下 **Developer settings** → **Fine-grained tokens** → **Generate new token**。
2. Token name：`perm-data-feed-push`；Expiration：**30 days**（到期自动失效，比长期令牌安全）。
3. Repository access：选 **Only select repositories** → 勾选 `perm-data-feed`（只这一个）。
4. Permissions → **Contents** → 勾 **Read and write**。其它权限一律不给。
5. 底部 **Generate token**，复制后写进项目根目录的 `github-secrets.txt`（模板已放好），保存，别贴到对话里。

令牌泄露的后果与处置：它能改这个仓库（等于能改我们站点显示的数据），**不能**碰你其它仓库、
不能读你的私有内容、30 天自动过期。用完随时可以在同一个页面点 **Delete** 撤掉。

---

## 风险清单

- **runner 被 Akamai 拦**：第 0 步就是为此准备的。真发生时可以换出口（美国 VPS、朋友的网络手动跑一次
  `python scripts/fetch_perm_disclosure.py --file 下载好的.xlsx`），但不要指望绕过去硬撞。
- **官方改版**：列名或文件名格式变了，脚本会抛"列名没认出来 / 没找到 xlsx"，
  `perm_status.json` 里留错误信息，数据保持上一版，页面显示旧日期，不会静默出错。
- **仓库文件体积**：披露表原件几十 MB，**不要提交**（脚本只写聚合 JSON）。真要留档放 Actions 的 artifact。
- **配额**：public 仓库的 Actions 分钟数无限，每周一次绰绰有余。
- **依赖第三方镜像的退路**：现有 `perm.json`（季度聚合镜像）继续保留，新链路挂了站点照样有数据。
