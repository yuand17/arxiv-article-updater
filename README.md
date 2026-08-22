# arXiv Updater

[![CI](https://github.com/yuand17/arxiv-article-updater/actions/workflows/ci.yml/badge.svg)](https://github.com/yuand17/arxiv-article-updater/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/yuand17/arxiv-article-updater)](https://github.com/yuand17/arxiv-article-updater/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A privacy-first Windows and macOS research-paper library that brings together arXiv, SciRate, selected Google Scholar authors, and eight built-in journals. It creates a configurable three-day reading list while keeping the database, preferences, credentials, and browsing history on the user's own computer.

一个隐私优先、支持 Windows 与 macOS 本机运行的论文库，汇集 arXiv、SciRate、选定的 Google Scholar 作者和 8 本内置重点期刊。它会生成篇数可配置的三天精选，同时将数据库、偏好、凭据和浏览记录留在用户自己的电脑上。

## Downloads / 下载

| System / 系统 | Download / 下载 | Notes / 说明 |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows portable ZIP](https://github.com/yuand17/arxiv-article-updater/releases/latest/download/arXiv-Updater-Windows-x64.zip) | Extract the complete folder before running. / 使用前请完整解压。 |
| macOS 13+, Apple Silicon | [macOS DMG for M-series Macs](https://github.com/yuand17/arxiv-article-updater/releases/latest/download/arXiv-Updater-macOS-Apple-Silicon.dmg) | M1/M2/M3/M4/M5 and later. / 适用于 M 系列 Mac。 |
| macOS 13+, Intel | [macOS DMG for Intel Macs](https://github.com/yuand17/arxiv-article-updater/releases/latest/download/arXiv-Updater-macOS-Intel.dmg) | For older Intel-based Macs. / 适用于 Intel Mac。 |

All packaged applications contain Python and the required runtime; Python and Git are not required for ordinary use. Source archives and release notes remain available on the [latest GitHub Release](https://github.com/yuand17/arxiv-article-updater/releases/latest).

所有封装版本都已经包含 Python 和所需运行环境，日常使用无需另行安装 Python 或 Git。源码压缩包和版本说明仍可在 [GitHub 最新 Release](https://github.com/yuand17/arxiv-article-updater/releases/latest) 找到。

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

The application is a single-user FastAPI service bound only to loopback addresses. APScheduler runs independent source schedules, SQLAlchemy and Alembic manage the local SQLite database, and a Windows tray or macOS menu-bar controller owns the normal desktop lifecycle. Recommendation generation combines local ranking with an optional DeepSeek reranker; optional API credentials are resolved from Windows Credential Manager or macOS Keychain at runtime.

Publisher RSS or Atom feeds define the journal article universe. Crossref may enrich matching DOI records but cannot expand that universe. Journal entries are normalized, deduplicated, filtered for original research, and then checked for physics relevance. A failure in one journal does not roll back successful journals or remove previously imported data.

### 中文

应用是一个只绑定本机回环地址的单用户 FastAPI 服务。APScheduler 独立调度各来源，SQLAlchemy 和 Alembic 管理本地 SQLite 数据库，Windows 托盘或 macOS 菜单栏控制器负责日常桌面生命周期。推荐流程由本地排序和可选 DeepSeek 精排组成；可选 API 凭据在运行时从 Windows 凭据管理器或 macOS Keychain 读取。

期刊的文章集合由出版社官方 RSS 或 Atom 决定。Crossref 只能补充 DOI 已匹配条目的元数据，不能扩大文章集合。期刊条目会依次规范化、去重、筛选原创研究并判断物理相关性。单本期刊失败不会回滚其他成功期刊，也不会删除以前导入的数据。

## Requirements / 系统要求

### English

- Windows 10/11 x64, or macOS 13 or later on Apple Silicon or Intel.
- Network access for source updates.
- Google Chrome only when SciRate, Science, or Science Advances requires a Cloudflare human-verification step.
- Python 3.12 and Git are required only for source development, not packaged downloads.

### 中文

- Windows 10/11 x64，或 Apple Silicon/Intel 芯片且运行 macOS 13 及以上版本的 Mac。
- 更新论文来源时需要网络连接。
- 只有当 SciRate、Science 或 Science Advances 触发 Cloudflare 人工验证时才需要 Google Chrome。
- 只有从源码开发时才需要 Python 3.12 和 Git，封装下载版本不需要。

## Installation / 安装

### Windows

1. Download the Windows ZIP above and extract the complete folder to a stable location.
2. Double-click `arXiv Updater.exe`. To create desktop and login-startup shortcuts, right-click `Install arXiv Updater.ps1` and run it with PowerShell.
3. Use the tray icon to open or quit the reader. The first run creates a new local database; it never downloads another user's library.

1. 下载上面的 Windows ZIP，并将整个文件夹解压到一个不会随意移动的位置。
2. 双击 `arXiv Updater.exe`。如需创建桌面和登录自启快捷方式，请右键 `Install arXiv Updater.ps1` 并使用 PowerShell 运行。
3. 通过托盘图标打开或退出阅读器。首次运行只会新建空的本地数据库，不会下载其他用户的论文库。

### macOS

1. Select the Apple Silicon or Intel DMG above, open it, and drag **arXiv Updater.app** into **Applications**. Do not run the copy inside the mounted DMG if you want login startup.
2. This release is **not notarized by Apple**. On first launch, macOS may block it. Open **System Settings → Privacy & Security**, find the message that arXiv Updater was blocked, click **Open Anyway**, and confirm **Open**. You can also Control-click the app in Finder and choose **Open** first; the Privacy & Security approval may still be required.
3. The application then stays in the macOS menu bar. Choose **Open arXiv Updater** to open the reader, optionally enable **Launch at Login**, and choose **Quit** to stop both the scheduler and local web service.

1. 根据芯片选择上面的 Apple Silicon 或 Intel DMG，打开后把 **arXiv Updater.app** 拖进 **Applications（应用程序）**。如果需要登录自启，请不要直接运行 DMG 里的副本。
2. 当前版本**未经 Apple 公证**，用户首次运行时 macOS 可能会阻止启动。请打开 **系统设置 → 隐私与安全性**，找到 arXiv Updater 已被阻止的提示，点击 **仍要打开**，然后再次确认 **打开**。也可以先在 Finder 中按住 Control 点击应用并选择 **打开**；部分系统仍需要到“隐私与安全性”中手动允许。
3. 启动后程序常驻 macOS 菜单栏。选择 **打开 arXiv Updater** 进入阅读器；可选择 **登录时自动启动**；选择 **退出** 会同时停止调度器和本地 Web 服务。

Optional services are configured from the local Settings page. DeepSeek falls back to local BM25 when disabled; only Google Scholar author synchronization requires SerpAPI. Secret fields are never echoed, and leaving a field empty preserves the credential in Windows Credential Manager or macOS Keychain.

可选服务在本机设置页中配置。DeepSeek 关闭时自动回退到本地 BM25；只有 Google Scholar 作者同步需要 SerpAPI。页面不会回显密钥，输入框留空会保留 Windows 凭据管理器或 macOS Keychain 中的现有凭据。

## Daily use and diagnostics / 日常使用与诊断

### Packaged applications / 封装版本

- On Windows, the desktop shortcut starts the tray controller and opens <http://127.0.0.1:8000>. The background shortcut starts without opening a browser. Double-click the tray icon to open the reader.
- On macOS, the app stays in the menu bar without a Dock icon. Its menu opens the reader, enables or disables login startup, and quits the complete background service.
- Starting a second copy only asks the existing controller to open the page. Neither platform takes over an unknown process already using port 8000.
- Windows 本地版通过桌面快捷方式启动托盘并打开网页，通过后台快捷方式静默启动；双击托盘图标可打开阅读器。
- macOS 版本常驻菜单栏且不显示 Dock 图标；菜单可以打开阅读器、切换登录自启并完整退出后台服务。
- 重复启动只会通知现有控制器打开页面；两个平台都不会接管占用 8000 端口的未知进程。

### Source diagnostics / 源码诊断

Development and diagnostic commands:

```powershell
.venv\Scripts\arxiv-updater.exe serve
.venv\Scripts\arxiv-updater.exe sync --source arxiv
.venv\Scripts\arxiv-updater.exe doctor
.venv\Scripts\arxiv-updater.exe migrate-db
```

Windows source installations can remove their shortcuts without deleting the database, backups, browser profiles, or credentials:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\uninstall_windows_shortcuts.ps1
```

Windows 源码安装可以删除桌面和开机启动快捷方式，但不会删除数据库、备份、浏览器档案或凭据。

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

Source installations keep local state in `.env`, `data/`, `*.log`, and the operating-system credential store. The packaged macOS app uses `~/Library/Application Support/arXiv Updater/` plus macOS Keychain; the portable Windows app uses its extracted folder plus Windows Credential Manager. Credentials are never written to SQLite, logs, HTML, source code, or Git.

### 中文

仓库只包含应用代码、迁移、静态资源、文档、合成测试样例和固定的 8 本期刊目录。全新 clone 不包含 API key、论文数据库、收藏论文、互动历史、研究兴趣、偏好画像、重点 Scholar 作者、来源缓存、日志、备份或 Chrome 验证档案。

源码安装的本地状态保存在 `.env`、`data/`、`*.log` 和操作系统凭据存储中。macOS 封装版使用 `~/Library/Application Support/arXiv Updater/` 与 macOS Keychain；Windows 便携版使用解压目录与 Windows 凭据管理器。凭据绝不会写入 SQLite、日志、HTML、源码或 Git。

## Backup and recovery / 备份与恢复

### English

Before a database upgrade, the application creates and verifies a SQLite backup under `data/backups/`. To restore one, first quit the tray or menu-bar application, preserve a copy of the current database, and replace `data/arxiv_updater.db` with the selected `.db.bak` file. Reinstall lifecycle shortcuts or login startup after moving an installation.

### 中文

数据库升级前，程序会在 `data/backups/` 创建并校验 SQLite 备份。恢复时应先退出托盘或菜单栏程序，保留当前数据库副本，再用选定的 `.db.bak` 替换 `data/arxiv_updater.db`。移动安装位置后，请重新设置快捷方式或登录自启。

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

CI covers Ubuntu unit and browser tests, Windows wheel/tray/portable-app checks, and an actual Apple Silicon macOS app-bundle smoke test. Tagged releases additionally build Intel macOS and publish all three downloads.

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

CI 同时覆盖 Ubuntu 单元与浏览器测试、Windows wheel/托盘/便携版检查，以及真实 Apple Silicon macOS App Bundle 的 smoke test。创建版本标签后还会构建 Intel macOS 版本并发布三个下载文件。

## Documentation / 文档

- [Architecture / 当前架构](docs/architecture.md)
- [Sources, classification, and scheduling / 来源、分类与调度](docs/sources.md)
- [Legacy single-user refactor status / 旧单用户改造方案状态](docs/single-user-refactor-plan.md)
- [v0.2.0 release notes / v0.2.0 发布说明](docs/releases/v0.2.0.md)
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
