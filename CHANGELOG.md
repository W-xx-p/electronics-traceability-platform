# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的基本格式。

## [Unreleased]

### Fixed

- 修复 Python 包发现配置，确保可编辑安装、CI 和 Docker 构建稳定可用。
- 使用锁定依赖提升构建可复现性，并校验生产依赖无已知漏洞。
- 生产模式关闭交互式 API 文档，并为 Prometheus 指标增加令牌保护。
- 串行化审计哈希链写入，避免并发写入形成分叉。
- 清理限流器中的过期客户端状态，避免长期运行时无界增长。

### Added

- 可重复、幂等的规模化制造质检合成数据，覆盖 14 天、多个订单和工位。
- 工位绩效、质量趋势、数据时效和检测档案总量展示。
- PaddleOCR 可插拔适配器和 OpenCV 图像质量检测。
- PASS、REVIEW、REJECT 质量闭环与人工复核。
- 四级 RBAC、会话撤销、登录锁定和工位设备密钥。
- 产品规则版本、订单状态机、复核认领和质检追溯。
- 证据鉴权、审计哈希链、Prometheus 指标与数据库备份。
- 事务型 Outbox、Docker 部署、CI、CodeQL 和依赖安全检查。

## [1.0.0] - 2026-10-05

### Added

- 首个公开企业 PoC 版本。
