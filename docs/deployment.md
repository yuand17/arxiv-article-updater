# 组内 Linux 部署

当前 Compose 面向受控内网或 VPN。服务器需要 Docker Engine 与 Compose v2。

## 首次启动

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

把生成值分别用于 `.env` 的 `APP_SECRET_KEY` 和 `POSTGRES_PASSWORD`。数据库密码应只含 URL-safe 字符。设置服务器实际 `BASE_URL`，按需填写 SerpAPI/DeepSeek key，然后：

```bash
docker compose up -d --build
docker compose exec web arxiv-updater create-admin you@example.org
docker compose ps
```

`migrate` 服务会在 web/worker 启动前运行 Alembic。升级代码时执行：

```bash
git pull --ff-only
docker compose up -d --build
```

## 备份与恢复

```bash
sh scripts/backup.sh
sh scripts/restore.sh backups/arxiv_updater_YYYYMMDDTHHMMSSZ.sql.gz
```

恢复脚本会要求输入 `RESTORE`，然后停止 web/worker、重建指定数据库、导入备份、应用迁移并重新启动。备份文件应复制到服务器之外并按小组策略加密保管。

## SQLite 迁移到 PostgreSQL

先备份本地 SQLite 文件。把文件复制到装有本项目和 PostgreSQL 网络访问权限的机器，并确保目标数据库为空：

```bash
export TARGET_DATABASE_URL='postgresql+psycopg://arxiv:URL_SAFE_PASSWORD@host/arxiv_updater'
arxiv-updater migrate-sqlite-to-postgres --sqlite-path data/arxiv_updater.db
```

命令只向空表写入，并逐表比较源与目标行数；发现非空目标或计数不同会停止。完成后再让 Compose web/worker 指向该数据库。

## 生产检查

- `APP_ENV=production`、`LOCAL_DEV_AUTO_LOGIN=false`。
- `APP_SECRET_KEY` 使用独立高熵值，API key 不进入 Git 或镜像。
- 仅通过内网/VPN 暴露 8000 端口，并配置主机防火墙。
- 定期执行备份并实际演练恢复。
- 公网部署前增加 HTTPS 反向代理、域名、安全头、速率限制和安全审计。
