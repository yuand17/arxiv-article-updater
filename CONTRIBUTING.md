# Contributing to arXiv Updater / 为 arXiv Updater 做贡献

Thank you for helping improve arXiv Updater. Bug reports, documentation improvements, tests, source-adapter fixes, accessibility work, and focused feature proposals are all welcome.

感谢你帮助改进 arXiv Updater。我们欢迎错误报告、文档改进、测试、来源适配修复、无障碍改进和范围明确的功能建议。

## English

### Before you start

- Search the existing issues and pull requests before opening a duplicate.
- For a substantial architectural or data-model change, open an issue first and describe the motivation, user-visible behavior, migration impact, privacy impact, and proposed tests.
- Never include API keys, `.env`, local databases, saved papers, interaction history, research interests, tracked authors, logs, backups, caches, or Chrome verification profiles.
- Do not automate or bypass publisher security controls. Changes involving Cloudflare must preserve the explicit human-verification workflow.

### Development setup

Use Windows 10/11 or macOS 13+ with Python 3.12. The commands below show PowerShell; equivalent POSIX virtual-environment commands work on macOS:

```powershell
git clone https://github.com/yuand17/arxiv-article-updater.git
Set-Location arxiv-article-updater
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater.exe init-db
```

Run the local application with:

```powershell
.venv\Scripts\arxiv-updater.exe serve
```

### Making a change

1. Fork the repository and create a focused branch from `main`.
2. Keep the change small enough to review and avoid unrelated formatting rewrites.
3. Add or update tests for behavior changes.
4. Update the English and Chinese documentation together when user-visible behavior changes.
5. Run the verification commands below before opening a pull request.

```powershell
.venv\Scripts\python.exe -m ruff check src tests alembic scripts
.venv\Scripts\python.exe -m mypy src\arxiv_updater
.venv\Scripts\python.exe -m pytest -m "not browser"
.venv\Scripts\python.exe -m pytest -m browser
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m build --wheel
git diff --check
```

### Pull-request checklist

- Explain what changed and why.
- Describe the verification performed and any checks that could not be run.
- Include screenshots for visible interface changes, using only synthetic or empty local data.
- Call out database migrations, new network destinations, new dependencies, or credential handling.
- Confirm that `git status` contains no personal state or secrets.

By submitting a contribution, you agree that it may be distributed under the repository's [MIT License](LICENSE).

## 中文

### 开始之前

- 新建 Issue 或 Pull Request 前，请先搜索是否已有重复内容。
- 对架构或数据模型的较大改动，请先开 Issue，说明动机、用户可见行为、迁移影响、隐私影响和计划添加的测试。
- 绝不能提交 API key、`.env`、本地数据库、收藏论文、互动历史、研究兴趣、重点作者、日志、备份、缓存或 Chrome 验证档案。
- 不要自动化或绕过出版商的安全控制。涉及 Cloudflare 的改动必须保留明确的人工验证流程。

### 开发环境

请使用 Windows 10/11 或 macOS 13 及以上版本，以及 Python 3.12。下面以 PowerShell 为例；macOS 可使用对应的 POSIX 虚拟环境命令：

```powershell
git clone https://github.com/yuand17/arxiv-article-updater.git
Set-Location arxiv-article-updater
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater.exe init-db
```

运行本地应用：

```powershell
.venv\Scripts\arxiv-updater.exe serve
```

### 提交改动

1. Fork 仓库，并从 `main` 创建一个范围明确的分支。
2. 保持改动便于审查，不要混入无关的格式化重写。
3. 行为发生变化时添加或更新测试。
4. 用户可见行为发生变化时，同时更新英文和中文文档。
5. 提交 Pull Request 前运行下面的验证命令。

```powershell
.venv\Scripts\python.exe -m ruff check src tests alembic scripts
.venv\Scripts\python.exe -m mypy src\arxiv_updater
.venv\Scripts\python.exe -m pytest -m "not browser"
.venv\Scripts\python.exe -m pytest -m browser
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m build --wheel
git diff --check
```

### Pull Request 检查清单

- 说明改了什么以及为什么修改。
- 说明已经完成的验证，以及未能运行的检查。
- 可见界面发生变化时附上截图，并且只使用合成数据或空数据库。
- 明确指出数据库迁移、新增网络目标、新依赖或凭据处理变化。
- 确认 `git status` 中没有个人状态或密钥。

提交贡献即表示你同意该贡献可以按照仓库的 [MIT License](LICENSE) 分发。
