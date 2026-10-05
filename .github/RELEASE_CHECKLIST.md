# Release checklist

- [ ] 所有 GitHub Actions 通过
- [ ] 数据库迁移已在旧版本副本上验证
- [ ] 依赖审计和 CodeQL 没有未处理的高危问题
- [ ] README、CHANGELOG 和 API 文档已更新
- [ ] 不包含 `.env`、数据库、上传证据、缓存或真实企业数据
- [ ] 演示账号仅用于 Demo，生产配置关闭演示数据
- [ ] Docker 镜像以非 root 用户运行并通过健康检查
- [ ] 已创建签名版本标签和发布说明
