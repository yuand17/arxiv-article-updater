# 旧单用户改造方案状态

这份历史方案已经完成并被当前三日更新器架构取代。当前实现仍保持“Windows 本机、单用户、Python + FastAPI + SQLite”的边界，但推荐、期刊、摘要和后台生命周期已按新的工程设计更新。

请以以下文档和代码为准：

- [当前架构](architecture.md)
- [来源、分类与调度](sources.md)
- Alembic 当前 head 及 `src/arxiv_updater/` 下的实现

历史方案不再作为配置、接口或验收依据。
