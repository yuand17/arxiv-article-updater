# arXiv Updater

一个只在 Windows 本机运行的个人论文库。它汇集 arXiv、SciRate、Google Scholar 重点作者和用户主动添加的期刊，并每三天生成一次可配置篇数的精选。

## 主要功能

- “三天精选”严格从最近三天的候选中选取，默认 66 篇，可在设置中调整为 1–200；候选不足时不会用旧论文补齐。
- 推荐先用本地 BM25 和来源信号做确定性粗排，再按三天批次调用 DeepSeek 精排；模型不可用时仍会生成本地批次。
- arXiv 按官方发布节奏分页读取增量，SciRate 保持三日榜前 50，Scholar 默认每 7 天同步全部重点作者，期刊默认每天同步。
- 新数据库没有默认期刊。添加期刊时只需填写名称和官网，程序会验证公开 HTTPS、发现官方端点并预览原创物理研究论文。
- 查看 Abstract、打开全文、收藏和“不感兴趣”都会形成偏好信号并永久保护该论文；无互动论文至少保留 9 天。
- 缺少摘要时只检查已明确关联的 arXiv 或出版社元数据，不做模糊标题搜索，也不调用隐藏的替代 API。
- 设置页使用右下角 Toast，最近同步和 API 用量在独立滚动面板中按时间游标连续加载。
- 登录后由系统托盘控制器常驻；桌面快捷方式启动或唤醒应用并打开网页，托盘“结束”会完整关闭调度器和服务。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\arxiv-updater init-db
powershell.exe -ExecutionPolicy Bypass -File scripts\install_windows_shortcuts.ps1
```

可选外部服务配置：

```dotenv
SERPAPI_API_KEY=
DEEPSEEK_API_KEY=
```

API key 只保存在未跟踪的 `.env` 中，设置页不会显示或保存密钥。

## 使用与诊断

- 桌面 **arXiv Updater**：未运行时启动托盘控制器并打开 <http://127.0.0.1:8000>；已运行时只发送打开命令。
- Startup **arXiv Updater Background**：登录后只启动后台和托盘，不自动打开浏览器。
- 托盘双击：打开网页；右键“结束”：等待调度器与 Uvicorn 退出并释放端口。

开发命令：

```powershell
.venv\Scripts\arxiv-updater serve
.venv\Scripts\arxiv-updater sync --source arxiv
.venv\Scripts\arxiv-updater doctor
.venv\Scripts\arxiv-updater migrate-db
```

数据库升级前会在 `data/backups/` 创建并校验 SQLite 备份。移动项目目录后，请重新运行快捷方式安装脚本。

## 验证

```powershell
.venv\Scripts\python -m ruff check src tests alembic scripts
.venv\Scripts\python -m mypy src\arxiv_updater
.venv\Scripts\python -m pytest -m "not browser"
.venv\Scripts\python -m alembic upgrade head
git diff --check
```

## 文档

- [架构](docs/architecture.md)
- [来源、分类与调度](docs/sources.md)
- [旧单用户改造方案状态](docs/single-user-refactor-plan.md)
