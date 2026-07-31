# 数据源与调度

| 来源 | 默认调度 | 说明 |
|---|---:|---|
| arXiv | 每天 08:00 | `quant-ph` 与全部配置的 `cond-mat` 子分类；从上次成功时间回退一天补抓 |
| SciRate | 每天 08:00 | 低频读取 quant-ph 页面，只作为 arXiv 热度增强层 |
| Nature / Nature Physics / PRL | 每天 08:00 | RSS/Atom；过滤新闻、社论、勘误等非研究内容 |
| Google Scholar 重点作者 | 每周一 06:00 | SerpAPI；最久未同步优先，默认每月最多 240 次 |

所有时间使用 `TIMEZONE`，默认 `Asia/Shanghai`。本地可用 `serve --with-scheduler`，服务器必须用独立 `worker`，避免多 Web 进程重复调度。

手动诊断单个来源：

```powershell
arxiv-updater sync --source arxiv
arxiv-updater sync --source scirate
arxiv-updater sync --source scholar
arxiv-updater sync --source journals
```

管理员页面显示每次同步的状态、读取/新增数量和错误。没有 API key、页面结构变化、请求超时或额度耗尽时，其他来源和已有数据仍可使用。

## 外部服务原则

- Google Scholar 只通过 SerpAPI，不自动爬取 Scholar 页面。
- arXiv 适配器保持单连接、请求间隔和增量窗口，不代理 PDF。
- SciRate HTML 解析器是可替换增强层；403 或结构变化不得阻断主同步。
- 自定义期刊只接受无凭据的公开 HTTPS RSS/Atom URL；同步前仍应用请求超时和内容过滤。
