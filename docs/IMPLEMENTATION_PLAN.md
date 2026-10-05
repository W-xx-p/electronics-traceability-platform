# ElectroTrace 初始实施计划（历史基线）

> 本文保留项目启动时的 MVP 设计。当前实际实现已经扩展到企业 PoC，最新能力和生产边界以根目录 `README.md`、`ENTERPRISE_ARCHITECTURE.md` 和 `OPERATIONS_RUNBOOK.md` 为准。

## 1. 建议仓库结构

```text
electronics-traceability-platform/
├── backend/
│   ├── app/
│   │   ├── api/               # HTTP 路由
│   │   ├── core/              # 配置、日志、异常、鉴权
│   │   ├── domain/            # 领域实体与业务规则
│   │   ├── services/          # 扫描、OCR、规则、追溯用例
│   │   ├── providers/         # PaddleOCR、条码、存储适配器
│   │   ├── repositories/      # 数据访问接口与实现
│   │   ├── schemas/           # Pydantic 请求/响应模型
│   │   └── main.py
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   ├── api/
│   │   └── types/
│   └── package.json
├── sample-data/
│   ├── images/
│   ├── orders/
│   └── rules/
├── docs/
├── docker-compose.yml
└── README.md
```

## 2. 核心接口草案

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/product-models` | 创建产品型号及规则 |
| `POST` | `/api/v1/orders` | 创建生产订单 |
| `POST` | `/api/v1/inspections` | 上传图片并执行质检 |
| `GET` | `/api/v1/inspections/{id}` | 查询检测详情 |
| `GET` | `/api/v1/trace/{serial_number}` | 按 SN 追溯 |
| `GET` | `/api/v1/reviews` | 查询待复核任务 |
| `POST` | `/api/v1/reviews/{id}/resolve` | 完成人工复核 |
| `GET` | `/api/v1/dashboard/summary` | 获取质量统计 |
| `GET` | `/health/ready` | 就绪检查 |

## 3. 质检判定规则

### PASS

- 所有必填字段均识别成功；
- SN、型号和批次格式正确；
- SN 未重复；
- 产品型号与订单匹配；
- OCR 字段与二维码字段一致；
- 所有关键字段置信度达到阈值。

### REVIEW

- 关键字段置信度低但仍有候选值；
- OCR 与二维码只有非关键字符差异；
- 图片质量较差但可以人工辨认；
- 规则版本缺失或订单状态不确定。

### REJECT

- SN 重复；
- 产品型号与订单明确不匹配；
- 关键字段缺失；
- OCR 与二维码关键字段冲突；
- 产品日期或批次超出允许范围。

## 4. 两周 MVP 排期

### 第 1～2 天

- 初始化 FastAPI、数据库和测试框架；
- 定义产品、订单、质检、字段、规则和复核模型；
- 准备合成订单和标签图片。

### 第 3～4 天

- 封装 OCR Provider 接口；
- 接入 PaddleOCR；
- 实现图片校验、旋转矫正和对比度增强；
- 为测试注入 Fake OCR Provider，避免测试下载模型。

### 第 5～6 天

- 实现二维码/条码解析；
- 实现字段提取器；
- 实现正则、重复 SN、订单匹配和一致性规则。

### 第 7～8 天

- 实现质检编排接口；
- 保存原图、OCR 框和判定原因；
- 实现幂等请求，防止网络重试造成重复记录。

### 第 9～10 天

- 开发工位扫描、检测详情和 SN 追溯页面；
- 开发人工复核页面。

### 第 11～12 天

- 开发批次质量统计；
- 增加结构化日志、请求 ID、健康检查和错误处理；
- 增加 API 与规则单元测试。

### 第 13～14 天

- Docker Compose 部署；
- 整理测试集并计算指标；
- 录制演示视频；
- 完善 README、架构图、API 文档和简历描述。

## 5. 必须测试的场景

- 正常标签自动放行；
- 重复 SN 自动拒绝；
- 当前订单与标签型号不一致；
- OCR 与二维码内容冲突；
- 缺少必填批次号；
- 模糊图片进入人工复核；
- 非图片、超大图片和损坏图片被安全拒绝；
- OCR 服务超时或不可用；
- 同一请求重试不会生成重复检测记录；
- 审核修改保留完整审计记录。

## 6. 演示数据策略

为避免依赖企业数据，使用脚本生成合成标签：

- 3 个虚拟产品型号；
- 2 张生产订单；
- 每个型号生成 30～50 张标签；
- 随机字体、旋转、透视、模糊、反光和噪声；
- 注入重复 SN、错误型号、缺字段及二维码冲突；
- 保留生成参数作为 ground truth，用于自动评估。

合成数据只用于功能验证。项目文档中应明确区分“合成测试结果”和“真实工厂效果”。

## 7. 第一版非目标

- 不直接控制真实 PLC、机械臂或传送带；
- 不搭建不必要的 Kubernetes 集群；
- 不在缺少数据时训练自定义大型 OCR 模型；
- 不把 OCR 置信度直接当作最终质量分数；
- 不虚构企业客户、生产数据或上线收益。
