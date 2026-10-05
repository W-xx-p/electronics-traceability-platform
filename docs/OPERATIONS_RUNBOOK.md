# ElectroTrace 运维手册

## 上线前门禁

- 设置 `ELECTROTRACE_ENV=production` 和 `ELECTROTRACE_SEED_DEMO=false`；生产模式会拒绝携带演示数据启动。
- 使用内部域名配置 `ELECTROTRACE_ALLOWED_HOSTS`，由反向代理终止 TLS，并限制请求体大小为 10 MiB。
- 首次启动通过 `ELECTROTRACE_BOOTSTRAP_ADMIN_*` 创建管理员，成功后立即从部署密钥中移除；随后应接入 OIDC/LDAP，不保留演示账号。
- 挂载持久卷到 `/app/data`，确认数据库和上传目录权限只授予应用用户。
- Prometheus 抓取 `/metrics`，探针分别使用 `/health/live` 和 `/health/ready`。
- 设置稳定、高熵的 `ELECTROTRACE_AUDIT_HMAC_KEY` 并交由密钥管理系统保存。该密钥不可随意轮换，否则历史审计签名将无法通过校验。

启动生产覆盖配置：

```bash
cp .env.production.example .env.production
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

## 备份与恢复演练

在线备份使用 SQLite Backup API，不直接复制正在写入的数据库：

```bash
python scripts/backup_database.py --database data/electrotrace.db --target backups --keep 14
```

每次备份都会执行 `PRAGMA integrity_check` 并生成 SHA-256 文件。恢复时必须先停止写流量，核对校验和，将当前数据库另行归档，再把备份复制到配置路径。至少每季度在隔离环境完成一次恢复演练。

## 告警建议

| 告警 | 建议阈值 |
|---|---|
| HTTP 5xx 比率 | 5 分钟超过 1% |
| 就绪探针失败 | 连续 3 次 |
| REVIEW 积压 | 超过班组约定 SLA |
| REJECT 比率突增 | 高于近 7 日均值 3 倍标准差 |
| 磁盘空间 | 剩余低于 20% |
| 工位心跳 | 超过 2 个生产节拍未更新 |

## 事件处置

1. 使用响应头 `X-Request-ID` 关联结构化访问日志和审计日志。
2. 管理员调用 `/api/v1/audit-logs/verify` 检查审计哈希链；失败时立即保全数据库和日志副本，不得自动修复或覆盖。
3. OCR 故障时暂停自动放行，切换人工复核或离线缓冲，不绕过质量规则。
4. 数据库异常时停止新质检写入，保留现场图片和设备幂等键，恢复后安全重放。
5. 账号泄露时停用账号；系统会立即撤销该用户的全部会话。
