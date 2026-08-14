# arXiv Updater

一个只在 Windows 本机运行的个人论文库。它汇集 arXiv、SciRate、Google Scholar 重点作者和 8 本内置重点期刊，并每三天生成一次可配置篇数的精选。

## 主要功能

- “三天精选”严格从最近三天的候选中选取，默认 66 篇，可在设置中调整为 1–200；候选不足时不会用旧论文补齐。
- 推荐先用本地 BM25 和来源信号做确定性粗排，再按三天批次调用 DeepSeek 精排；模型不可用时仍会生成本地批次。
- arXiv 按官方发布节奏分页读取增量；SciRate 保持三日榜前 50；Scholar 默认每 7 天同步重点作者最近发表的 10 篇论文并跨作者去重；期刊每天同步。
- 内置 Nature、Nature Physics、Nature Communications、Science、Science Advances、Physical Review Letters、Physical Review X 和 PRX Quantum；设置页可逐刊开关。
- 查看 Abstract、打开全文、收藏和“不感兴趣”都会形成偏好信号并永久保护该论文；无互动论文至少保留 9 天。
- 缺少摘要时只检查已明确关联的 arXiv 或出版社元数据，不做模糊标题搜索，也不调用隐藏的替代 API。
- API key 只写入当前 Windows 用户的凭据管理器，不写入 SQLite、日志、页面、源码或 Git。
- 页面运行所需的 htmx、KaTeX 和字体均已随程序打包，断网时仍可浏览现有论文并使用本地操作。

## 系统要求

- Windows 10 或 Windows 11。
- 64 位 Python 3.12。
- 网络访问用于更新论文来源。
- Google Chrome 仅在 SciRate 触发 Cloudflare 真人验证时需要；其余本地阅读流程不依赖 Chrome。

## 安装

在项目根目录运行：

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

可选外部服务在本机设置页中开启。DeepSeek 未启用时使用本地 BM25；SerpAPI 未启用时 Google Scholar 来源会跳过。输入框留空会保留现有密钥，清除操作会同时关闭服务。

## 使用与诊断

- 桌面 **arXiv Updater**：未运行时启动托盘控制器并打开 <http://127.0.0.1:8000>；已运行时只发送打开命令。
- Startup **arXiv Updater Background**：登录后只启动后台和托盘，不自动打开浏览器。
- 托盘双击：打开网页；右键“结束”：等待调度器与 Uvicorn 退出并释放端口。

开发与诊断命令：

```powershell
.venv\Scripts\arxiv-updater.exe serve
.venv\Scripts\arxiv-updater.exe sync --source arxiv
.venv\Scripts\arxiv-updater.exe doctor
.venv\Scripts\arxiv-updater.exe migrate-db
```

数据库升级前会在 `data/backups/` 创建并校验 SQLite 备份。恢复时应先退出托盘程序，保留当前数据库副本，再用选定的 `.db.bak` 替换 `data/arxiv_updater.db`。移动项目目录、重建虚拟环境或恢复备份后，请重新运行快捷方式安装脚本和 `doctor`。

删除桌面与开机启动快捷方式：

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\uninstall_windows_shortcuts.ps1
```

该脚本只删除快捷方式，不删除论文数据库、备份或凭据。

## 验证

```powershell
.venv\Scripts\python.exe -m ruff check src tests alembic scripts
.venv\Scripts\python.exe -m mypy src\arxiv_updater
.venv\Scripts\python.exe -m pytest -m "not browser"
.venv\Scripts\python.exe -m pytest -m browser
.venv\Scripts\python.exe -m alembic check
.venv\Scripts\python.exe -m build --wheel
git diff --check
```

CI 同时覆盖 Ubuntu 单元/浏览器测试和 Windows wheel 初始化、托盘控制器及快捷方式 smoke test。

## 文档

- [当前架构](docs/architecture.md)
- [来源、分类与调度](docs/sources.md)
- [旧单用户改造方案状态](docs/single-user-refactor-plan.md)
- [第三方浏览器资源说明](THIRD_PARTY_NOTICES.md)
