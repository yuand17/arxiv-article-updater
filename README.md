# arXiv 智能文章更新器

一个面向科研小组的轻量论文阅读网页。它把重点作者、SciRate 热门、arXiv 的
`quant-ph`/`cond-mat` 更新以及 Nature、Nature Physics、PRL 集中到一个可个性化的
英文论文流中。界面使用中文，论文内容和 AI 总结保持英文。

## 本机快速开始

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater init-db
.venv\Scripts\arxiv-updater serve --with-scheduler
```

打开 <http://127.0.0.1:8000>。开发模式默认创建并自动登录本地管理员。
SerpAPI 和 DeepSeek key 都是可选的；没有 key 时其他数据源和阅读功能仍能使用。

## 常用命令

```powershell
arxiv-updater doctor
arxiv-updater sync --source arxiv
arxiv-updater create-admin your@email.example
arxiv-updater create-invite
```

详细的本地、数据源和服务器部署说明见 `docs/`。

