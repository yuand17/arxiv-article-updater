# arXiv Updater

一个只在 Windows 本机运行的个人论文库。它把 arXiv、SciRate、Google Scholar 重点作者与重点期刊集中在同一页面；论文和互动历史只保存在本机 SQLite 数据库中。

## 功能

- 无账号、邀请、管理员或远程部署功能；服务只绑定本机回环地址。
- “全部更新”显示最近 30 天入库的论文，严格按入库时间倒序，可每次追加 100 篇。
- “本周精选”每三天生成一次：先由 DeepSeek 根据每周偏好画像匹配，再结合新鲜度、重点作者、SciRate 和期刊信号排序；没有 key 时仍有稳定的本地回退排序。
- 点击“查看 Abstract”会记录阅读兴趣，并显示原始摘要；Scholar 缺摘要会从本地、Semantic Scholar、arXiv 或公开元数据逐级补全，绝不让模型编造摘要。
- 来源默认频率：arXiv 每天、SciRate 每 3 天、Google Scholar 每 7 天、重点期刊每 7 天；设置页可调为 1–30 天。
- Windows 登录后静默自启；桌面图标用于启动或唤醒本地网页。

## 首次安装

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater init-db
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1
```

在 `.env` 中按需填写 API key。不要把真实 key 提交到 Git：

```dotenv
SERPAPI_API_KEY=
SEMANTIC_SCHOLAR_API_KEY=
DEEPSEEK_API_KEY=
```

`SEMANTIC_SCHOLAR_API_KEY` 可选；不填也会尝试补摘要，但速度和限流表现较差。设置页只显示“已配置/未配置”。

安装快捷方式后：

- 双击桌面的 **arXiv Updater**：如果服务未运行，会在后台启动、通过健康检查后打开网页；已经运行则直接打开。
- Windows 登录：**arXiv Updater Background** 在 Startup 文件夹中静默启动服务和定时更新，不弹浏览器。

如果移动项目目录，请重新运行 `scripts\install_windows_shortcuts.ps1` 更新两个绝对路径快捷方式。

## 手动运行与诊断

```powershell
.venv\Scripts\arxiv-updater serve
.venv\Scripts\arxiv-updater sync --source arxiv
.venv\Scripts\arxiv-updater doctor
.venv\Scripts\arxiv-updater migrate-db
```

网页地址固定为 <http://127.0.0.1:8000>。`serve` 会启动内嵌调度器；不需要也不存在独立 worker。

升级数据库前，程序会用 SQLite backup API 在 `data\backups\` 创建并校验带时间戳的副本。若迁移失败，启动会停止并报告备份路径。

## 开发验证

```powershell
.venv\Scripts\python -m pytest -m "not browser"
.venv\Scripts\python -m ruff check src tests alembic
.venv\Scripts\python -m mypy src\arxiv_updater
```

## 文档

- [架构](docs/architecture.md)
- [来源、摘要补全与调度](docs/sources.md)
- [重构工程计划](docs/single-user-refactor-plan.md)
