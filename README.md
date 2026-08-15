# arXiv Updater

[![CI](https://github.com/yuand17/arxiv-article-updater/actions/workflows/ci.yml/badge.svg)](https://github.com/yuand17/arxiv-article-updater/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/yuand17/arxiv-article-updater)](https://github.com/yuand17/arxiv-article-updater/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A privacy-first, Windows-local research-paper library that brings together arXiv, SciRate, selected Google Scholar authors, and eight built-in journals. It creates a configurable three-day reading list while keeping the database, preferences, credentials, and browsing history on the user's own computer.

一个隐私优先、仅在 Windows 本机运行的论文库，汇集 arXiv、SciRate、选定的 Google Scholar 作者和 8 本内置重点期刊。它会生成篇数可配置的三天精选，同时将数据库、偏好、凭据和浏览记录留在用户自己的电脑上。

**Latest release:** [Download arXiv Updater v0.1.0](https://github.com/yuand17/arxiv-article-updater/releases/tag/v0.1.0)

**最新版本：** [下载 arXiv Updater v0.1.0](https://github.com/yuand17/arxiv-article-updater/releases/tag/v0.1.0)

## Features / 主要功能

### English

- Selects the configured number of papers, 66 by default, strictly from the latest three-day candidate window. Older papers are never used to fill a short batch.
- Uses deterministic local BM25 and source signals for coarse ranking, then optionally asks DeepSeek to rerank each three-day batch. Oversized model responses are split into smaller batches; a genuinely unavailable model falls back to local ranking.
- Reads arXiv incrementally around its official announcement schedule, imports SciRate's top 50 three-day papers, synchronizes each tracked Scholar author's latest 10 works every seven days, and checks enabled journals daily.
- Includes Nature, Nature Physics, Nature Communications, Science, Science Advances, Physical Review Letters, Physical Review X, and PRX Quantum, each with an independent subscription switch.
- Records abstract views, full-text opens, saves, and dismissals as local preference signals. Papers with an interaction are permanently protected; untouched papers are retained for at least nine days.
- Enriches missing abstracts only through an explicitly linked arXiv identifier, publisher metadata, Crossref metadata, or an exact local DOI/arXiv match. It does not perform fuzzy title lookups through hidden replacement APIs.
- Bundles htmx, KaTeX, and fonts locally, so an existing library remains readable and interactive without an internet connection.

### 中文

- 严格从最近三天的候选中选择设置的篇数，默认 66 篇；候选不足时不会用更早论文补齐。
- 先使用确定性的本地 BM25 和来源信号粗排，再按三天批次选择性调用 DeepSeek 精排。模型响应过长时会拆成更小批次；只有模型真正不可用时才回退到本地排序。
- arXiv 按官方发布节奏增量读取；SciRate 导入三日榜前 50；Google Scholar 每 7 天同步每位重点作者最近发表的 10 篇并去重；已开启的期刊每天检查。
- 内置 Nature、Nature Physics、Nature Communications、Science、Science Advances、Physical Review Letters、Physical Review X 和 PRX Quantum，每本期刊都有独立订阅开关。
- Abstract 查看、全文打开、收藏和“不感兴趣”都会形成本地偏好信号。有互动的论文会永久保护；无互动论文至少保留 9 天。
- 缺少摘要时只通过明确关联的 arXiv ID、出版社元数据、Crossref 元数据或本地 DOI/arXiv ID 精确匹配补全，不通过隐藏的替代 API 做模糊标题搜索。
- htmx、KaTeX 和字体均随程序打包，断网时仍可阅读现有论文并使用本地操作。

## Architecture / 架构

### English

The application is a single-user FastAPI service bound only to loopback addresses. APScheduler runs independent source schedules, SQLAlchemy and Alembic manage the local SQLite database, and a Windows tray controller owns the normal desktop lifecycle. Recommendation generation combines local ranking with an optional DeepSeek reranker; optional API credentials are resolved from Windows Credential Manager at runtime.

Publisher RSS or Atom feeds define the journal article universe. Crossref may enrich matching DOI records but cannot expand that universe. Journal entries are normalized, deduplicated, filtered for original research, and then checked for physics relevance. A failure in one journal does not roll back successful journals or remove previously imported data.

### 中文

应用是一个只绑定本机回环地址的单用户 FastAPI 服务。APScheduler 独立调度各来源，SQLAlchemy 和 Alembic 管理本地 SQLite 数据库，Windows 托盘控制器负责日常桌面生命周期。推荐流程由本地排序和可选 DeepSeek 精排组成；可选 API 凭据在运行时从 Windows 凭据管理器读取。

期刊的文章集合由出版社官方 RSS 或 Atom 决定。Crossref 只能补充 DOI 已匹配条目的元数据，不能扩大文章集合。期刊条目会依次规范化、去重、筛选原创研究并判断物理相关性。单本期刊失败不会回滚其他成功期刊，也不会删除以前导入的数据。

## Requirements / 系统要求

### English

- Windows 10 or Windows 11.
- 64-bit Python 3.12.
- Network access for source updates.
- Google Chrome only when SciRate, Science, or Science Advances requires a Cloudflare human-verification step.

### 中文

- Windows 10 或 Windows 11。
- 64 位 Python 3.12。
- 更新论文来源时需要网络连接。
- 只有当 SciRate、Science 或 Science Advances 触发 Cloudflare 人工验证时才需要 Google Chrome。

## Installation / 安装

### English

Download and extract **Source code (zip)** from the [latest GitHub Release](https://github.com/yuand17/arxiv-article-updater/releases/latest), then open PowerShell in the extracted directory. Alternatively, clone the repository with Git:

```powershell
git clone https://github.com/yuand17/arxiv-article-updater.git
Set-Location arxiv-article-updater
```

The initialization creates a new local database; it does not download or restore another user's library.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater.exe init-db
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1
```

To omit automatic startup at Windows sign-in:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1 -NoStartup
```

Enable optional services from the local Settings page. DeepSeek is optional and falls back to local BM25 when disabled. SerpAPI is required only for Google Scholar author synchronization. Secret fields are never echoed back by the page; leaving a field empty preserves its current credential.

### 中文

从 [GitHub 最新 Release](https://github.com/yuand17/arxiv-article-updater/releases/latest) 下载并解压 **Source code (zip)**，然后在解压后的目录打开 PowerShell。也可以使用 Git clone 仓库：

```powershell
git clone https://github.com/yuand17/arxiv-article-updater.git
Set-Location arxiv-article-updater
```

初始化会创建一个新的本地数据库，不会下载或恢复其他用户的论文库。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater.exe init-db
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1
```

如果不希望登录 Windows 时自动启动：

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1 -NoStartup
```

可选服务在本机设置页中开启。DeepSeek 关闭时自动回退到本地 BM25；只有 Google Scholar 作者同步需要 SerpAPI。页面不会回显密钥；输入框留空会保留当前凭据。

## Daily use and diagnostics / 日常使用与诊断

### English

- The desktop **arXiv Updater** shortcut starts the tray controller and opens <http://127.0.0.1:8000>. If the application is already running, it only sends an open command.
- The startup **arXiv Updater Background** shortcut starts the service and tray without opening a browser.
- Double-click the tray icon to open the reader. Use the tray's **Quit** action to stop the scheduler and Uvicorn cleanly and release the local ports.

Development and diagnostic commands:

```powershell
.venv\Scripts\arxiv-updater.exe serve
.venv\Scripts\arxiv-updater.exe sync --source arxiv
.venv\Scripts\arxiv-updater.exe doctor
.venv\Scripts\arxiv-updater.exe migrate-db
```

Remove the desktop and startup shortcuts without deleting the database, backups, browser profiles, or credentials:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\uninstall_windows_shortcuts.ps1
```

### 中文

- 桌面 **arXiv Updater** 快捷方式会启动托盘控制器并打开 <http://127.0.0.1:8000>；如果应用已经运行，它只发送打开命令。
- 开机启动目录中的 **arXiv Updater Background** 只启动后台服务和托盘，不自动打开浏览器。
- 双击托盘图标打开阅读器；使用托盘中的“结束”会让调度器和 Uvicorn 正常退出并释放本地端口。

开发与诊断命令：

```powershell
.venv\Scripts\arxiv-updater.exe serve
.venv\Scripts\arxiv-updater.exe sync --source arxiv
.venv\Scripts\arxiv-updater.exe doctor
.venv\Scripts\arxiv-updater.exe migrate-db
```

删除桌面和开机启动快捷方式，但不删除数据库、备份、浏览器档案或凭据：

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\uninstall_windows_shortcuts.ps1
```

## Cloudflare human verification / Cloudflare 人工验证

### English

Manual updates from the Settings page may open a dedicated visible Chrome window when SciRate, Science, or Science Advances returns a Cloudflare challenge. Complete the verification in that window and leave it open; the application waits for the real paper page or feed, imports it, and then closes the dedicated window. The default timeout is five minutes.

SciRate and journal feeds use separate profiles under `data/chrome/scirate/` and `data/chrome/journals/`. These profiles persist the site's verification state locally and are excluded from Git. Scheduled updates never open a window: they preserve the previous successful data, record a safe error, and retry later. This workflow assists a real user verification and does not automate or bypass Cloudflare.

### 中文

从设置页手动更新时，如果 SciRate、Science 或 Science Advances 返回 Cloudflare challenge，程序会打开一个专用的可见 Chrome 窗口。请在该窗口中完成验证并保持窗口打开；程序会等待真正的论文页面或 feed，导入完成后自动关闭专用窗口。默认等待时间为 5 分钟。

SciRate 与期刊分别使用 `data/chrome/scirate/` 和 `data/chrome/journals/` 档案，在本机保留网站验证状态，且不会进入 Git。定时更新绝不会弹窗：它会保留上次成功数据、记录安全的错误信息并稍后重试。该流程只辅助真实用户完成人工验证，不会自动绕过 Cloudflare。

## Privacy and clean-clone guarantee / 隐私与干净克隆保证

### English

The repository contains application code, migrations, static assets, documentation, synthetic test fixtures, and the fixed eight-journal catalog. A fresh clone contains no API keys, paper database, saved papers, interaction history, research interests, preference profile, tracked Scholar authors, source cache, logs, backups, or Chrome verification profile.

Local state stays in `.env`, `data/`, `*.log`, and Windows Credential Manager. Those filesystem paths are excluded by `.gitignore`; credentials are never written to SQLite, logs, HTML, source code, or Git. Before publishing a change, inspect `git status` and never force-add ignored local-state paths.

### 中文

仓库只包含应用代码、迁移、静态资源、文档、合成测试样例和固定的 8 本期刊目录。全新 clone 不包含 API key、论文数据库、收藏论文、互动历史、研究兴趣、偏好画像、重点 Scholar 作者、来源缓存、日志、备份或 Chrome 验证档案。

本地状态只保存在 `.env`、`data/`、`*.log` 和 Windows 凭据管理器中。这些文件系统路径均由 `.gitignore` 排除；凭据不会写入 SQLite、日志、HTML、源码或 Git。发布改动前应检查 `git status`，不要强制添加被忽略的本地状态路径。

## Backup and recovery / 备份与恢复

### English

Before a database upgrade, the application creates and verifies a SQLite backup under `data/backups/`. To restore one, first quit the tray application, preserve a copy of the current database, and replace `data/arxiv_updater.db` with the selected `.db.bak` file. Reinstall the shortcuts and run `doctor` after moving the project, rebuilding the virtual environment, or restoring a backup.

### 中文

数据库升级前，程序会在 `data/backups/` 创建并校验 SQLite 备份。恢复时应先退出托盘程序，保留当前数据库副本，再用选定的 `.db.bak` 替换 `data/arxiv_updater.db`。移动项目、重建虚拟环境或恢复备份后，请重新安装快捷方式并运行 `doctor`。

## Development verification / 开发验证

### English

```powershell
.venv\Scripts\python.exe -m ruff check src tests alembic scripts
.venv\Scripts\python.exe -m mypy src\arxiv_updater
.venv\Scripts\python.exe -m pytest -m "not browser"
.venv\Scripts\python.exe -m pytest -m browser
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m build --wheel
git diff --check
```

CI covers Ubuntu unit and browser tests plus Windows wheel initialization, tray-controller, and shortcut smoke tests.

### 中文

```powershell
.venv\Scripts\python.exe -m ruff check src tests alembic scripts
.venv\Scripts\python.exe -m mypy src\arxiv_updater
.venv\Scripts\python.exe -m pytest -m "not browser"
.venv\Scripts\python.exe -m pytest -m browser
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m build --wheel
git diff --check
```

CI 同时覆盖 Ubuntu 单元与浏览器测试，以及 Windows wheel 初始化、托盘控制器和快捷方式 smoke test。

## Documentation / 文档

- [Architecture / 当前架构](docs/architecture.md)
- [Sources, classification, and scheduling / 来源、分类与调度](docs/sources.md)
- [Legacy single-user refactor status / 旧单用户改造方案状态](docs/single-user-refactor-plan.md)
- [v0.1.0 release notes / v0.1.0 发布说明](docs/releases/v0.1.0.md)
- [Third-party browser resources / 第三方浏览器资源说明](THIRD_PARTY_NOTICES.md)

## Contributing and citation / 贡献与引用

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request. If this software supports your research, GitHub can generate a citation from [CITATION.cff](CITATION.cff).

欢迎参与贡献。提交 Issue 或 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。如果本软件帮助了你的研究，可以使用 [CITATION.cff](CITATION.cff) 中的信息进行引用。

## Copyright and attribution / 版权与署名

### English

**Author:** Dong Yuan (袁冬)

**Affiliation:** Institute for Interdisciplinary Information Sciences, Tsinghua University (清华大学交叉信息研究院)

**Programming assistance:** Developed with programming assistance from ChatGPT Codex.

**License:** Released under the [MIT License](LICENSE).

Copyright © 2026 Dong Yuan (袁冬).

### 中文

**作者：** Dong Yuan 袁冬

**单位：** Institute for Interdisciplinary Information Sciences, Tsinghua University 清华大学交叉信息研究院

**编程辅助：** 本项目由 ChatGPT Codex 辅助编程。

**许可证：** 本项目按照 [MIT License](LICENSE) 发布。

版权所有 © 2026 Dong Yuan 袁冬。
