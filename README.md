# arXiv 智能文章更新器

一个面向科研个人与小组的轻量论文阅读网页。它把重点作者、SciRate 热门、arXiv 的 `quant-ph`/全部 `cond-mat` 更新，以及 Nature、Nature Physics、Physical Review Letters 集中到一个可个性化的英文论文流中。界面使用中文，论文内容和 AI 总结保持英文。

## 已实现

- Google Scholar 重点作者：通过 Scholar 主页 URL 关注，使用 SerpAPI 查询，不直接爬取 Google Scholar。
- arXiv：每天增量同步 `quant-ph` 与全部显式配置的 `cond-mat` 子分类。
- SciRate：按 arXiv ID 合并票数，标记每日前 10 或至少 5 票的热门论文。
- 期刊：默认 Nature、Nature Physics、PRL，管理员还可添加 HTTPS RSS/Atom 源。
- 个性化：重点作者、期刊、热度、新鲜度、关键词与个人行为共同排序；互不共享成员行为数据。
- AI 总结：按需调用 OpenAI-compatible API，默认 DeepSeek；只依据 abstract 生成英文结构化总结并全组缓存。
- 运维：SQLite 本地模式、PostgreSQL 组内模式、Alembic、Docker Compose、备份/恢复与 CI。

## Windows 本地快速开始

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater init-db
.venv\Scripts\arxiv-updater serve --with-scheduler
```

打开 <http://127.0.0.1:8000>。开发模式只在绑定回环地址时使用本地自动登录。SerpAPI 和 DeepSeek key 都是可选的；没有 key 时已有论文和其他来源仍可浏览。

常用诊断与同步命令：

```powershell
arxiv-updater doctor
arxiv-updater sync --source arxiv
arxiv-updater sync --source journals
arxiv-updater create-admin your@email.example
arxiv-updater create-invite
arxiv-updater migrate-db
```

## 文档

- [架构与扩展](docs/architecture.md)
- [数据源与调度](docs/sources.md)
- [Docker 组内部署、备份与迁移](docs/deployment.md)

## 安全边界

程序不保存或代理 PDF，只保存公开元数据与 abstract。API key 只从环境变量读取，不写入数据库或日志。当前 Docker 配置面向受控内网/VPN；若暴露到公网，必须增加 HTTPS 反向代理、正式域名、安全审计与网络访问控制。
