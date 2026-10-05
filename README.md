# ElectroTrace：电子产品序列号识别与质量追溯平台

面向 PCB、电子控制板、智能硬件及小型电子产品生产线的 AI 质检与追溯系统。平台通过工业相机或图片读取产品序列号、型号、批次号、生产日期、二维码及条形码，对识别结果执行订单匹配、格式校验、重复码检查和标签一致性验证，并保存产品级质量档案。

本项目以“可运行的制造业应用”而不是单独的 OCR Notebook 为目标，重点展示计算机视觉、后端服务、业务规则、数据建模、人工复核和工程部署能力。当前版本已实现四级 RBAC、用户生命周期、登录锁定、可撤销会话、可验证审计哈希链、规则版本、订单状态机、复核任务认领、工位设备密钥、证据鉴权、幂等质检、流式上传门禁、Prometheus 指标、在线一致性备份和 CI/安全扫描。

## 快速运行

当前仓库已经包含可运行的企业 PoC。默认使用合成标签中嵌入的可验证元数据模拟 OCR，因此无需下载大模型即可演示完整质检流程；该模式不代表真实 OCR 效果。

```powershell
# 进入项目目录
cd electronics-traceability-platform

# 创建隔离环境并安装已验证依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

# 仓库已包含五张合成演示标签；需要重新生成时执行
python scripts/generate_demo_labels.py

# 启动应用
python -m uvicorn backend.app.main:app --reload
```

打开：

- Web 应用：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health/ready>
- Prometheus 指标：<http://127.0.0.1:8000/metrics>

默认启用身份认证，演示账号如下（生产环境必须通过环境变量修改管理员密码并替换为企业 SSO）：

| 账号 | 密码 | 权限 |
|---|---|---|
| `admin` | `ChangeMe!2026` | 系统管理、审计日志、全部业务功能 |
| `quality` | `Quality!2026` | 质量配置、人工复核、数据导出 |
| `operator` | `Operator!2026` | 产线质检与业务查询 |
| `viewer` | `Viewer!2026` | 只读追溯与看板 |

演示顺序建议：

1. 扫描 `01-pass.png`，结果为 PASS；
2. 扫描 `02-review-low-confidence.png`，进入 REVIEW；
3. 扫描 `03-duplicate-sn.png`，因重复序列号被 REJECT；
4. 扫描 `04-wrong-model.png`，因订单型号不匹配被 REJECT；
5. 扫描 `05-qr-conflict.png`，因 OCR 与码值冲突被 REJECT。

运行测试：

```powershell
pytest
```

当前回归套件包含 11 个端到端 API 测试，覆盖质量规则、幂等、重复序列号、人工复核、复核认领冲突、权限矩阵、登录锁定、恶意上传、工位密钥、证据鉴权、订单状态、规则版本、审计篡改检测、导出、生产安全边界、指标和备份完整性。

也可以使用 Docker Compose：

```powershell
docker compose up --build
```

容器默认运行 Demo OCR，并将 SQLite 数据及质检图片保存到命名卷 `electrotrace-data`。

### 接入 PaddleOCR

建议使用 Python 3.11 创建独立环境，安装与硬件兼容的 PaddlePaddle 和 PaddleOCR，然后设置：

```powershell
$env:ELECTROTRACE_OCR_PROVIDER = "paddle"
python -m uvicorn backend.app.main:app --reload
```

真实产品应为每个型号配置 ROI 和字段模板，并使用固定测试集重新评估准确率、误拦截率和延迟。

## 一、业务背景

电子制造企业通常通过标签、PCB 丝印、二维码或条形码标识产品。人工核对存在以下问题：

- 产线速度快，人工读取容易漏检或录错；
- 标签反光、倾斜、模糊，小字符和点阵字符识别困难；
- 标签可见文字与二维码内容可能不一致；
- 重复序列号、错误型号和错误批次难以及时发现；
- 质检图片、检测结果和产品序列号分散，发生问题时追溯困难；
- 单纯 OCR 只能返回文字，无法决定产品应当放行、拦截还是人工复核。

ElectroTrace 将 OCR、条码解析、业务规则和产品追溯整合为统一质检工位。

## 二、目标用户

- 生产线操作员：扫描产品并获得即时放行结果；
- 质量工程师：配置校验规则、处理人工复核和分析异常；
- 生产主管：查看工位效率、批次合格率和异常趋势；
- 售后与质量追溯人员：根据序列号查询生产及质检记录；
- 系统管理员：维护产品型号、订单、用户、设备和规则版本。

## 三、核心业务流程

```text
工业相机/图片上传
        ↓
图片质量检查（模糊、曝光、分辨率）
        ↓
透视校正、旋转矫正、ROI 裁剪、对比度增强
        ↓
PaddleOCR 文本识别 + QR/条形码解析
        ↓
字段提取（SN、型号、批次、日期、RoHS）
        ↓
规则引擎（格式、重复、订单、文本与码值一致性）
        ↓
 ┌────────────┬────────────┬────────────┐
 │   PASS     │   REVIEW   │   REJECT   │
 │ 自动放行   │ 人工复核   │ 自动拦截   │
 └────────────┴────────────┴────────────┘
        ↓
保存原图、识别框、结构化字段、规则结果和审核记录
        ↓
形成产品级、批次级和订单级质量追溯档案
```

## 四、核心功能

### 1. AI 识别

- 可插拔 PaddleOCR 适配器，用于 PCB 丝印、产品标签和包装文字识别；
- 序列号、型号、批次号、生产日期和 RoHS 标识提取；
- 当前真实模式支持 QR Code 解码；Data Matrix、Code 128 需要按现场码制增加解码器；
- PaddleOCR 文档方向和展平能力，以及 OpenCV 模糊、曝光、对比度质量检测；
- 返回文字位置、置信度、处理耗时和预处理质量分数。

### 2. 产品与订单管理

- 产品型号及标签模板管理；
- 生产订单、计划批次和目标数量管理；
- 每个型号独立配置字段规则及 ROI；
- 支持规则版本和生效时间；
- 支持同一工位切换不同产品型号。

### 3. 质量规则引擎

- 必填字段检查；
- 序列号、批次号和日期正则校验；
- 序列号全局唯一性检查；
- OCR 可见文字与二维码内容交叉校验；
- 产品型号与当前订单一致性检查；
- 日期范围、批次范围和 RoHS 状态检查；
- 按规则严重程度生成 PASS、REVIEW 或 REJECT。

示例规则：

```json
{
  "product_model": "ECU-A100",
  "serial_number": {
    "required": true,
    "pattern": "^A100-[0-9]{8}$",
    "unique": true
  },
  "batch_number": {
    "required": true,
    "pattern": "^B[0-9]{6}$"
  },
  "checks": {
    "ocr_qr_consistency": true,
    "order_model_match": true,
    "rohs_required": true
  }
}
```

### 4. 人工复核

- 低置信度结果进入复核队列；
- 原图、OCR 框、识别字段和失败规则并排展示；
- 审核员可以更正字段、确认放行或拒绝；
- 保存修改前后内容、审核人、时间和原因；
- 复核结果沉淀为后续评估和模型优化样本。

### 5. 质量追溯

- 按 SN、批次、订单、产品型号和时间查询；
- 保存每件产品的原图、标注图、OCR 结果和判定理由；
- 展示批次合格率、拒绝原因和人工复核率；
- 支持 CSV/Excel 导出；
- 为后续 PCB 异常检测模块预留统一质检结果接口。

### 6. 工位监控

- 当前产品型号和订单；
- 当班扫描数量、通过数量和拦截数量；
- 平均识别耗时和工位节拍；
- 低置信度、重复 SN、型号不匹配等实时告警；
- OCR 服务、数据库和对象存储健康状态。

当前版本已实现工位建档、启停状态、工位心跳、质检记录关联工位和单次处理耗时；相机在线状态与 PLC 节拍仍需现场协议接入。

### 7. 企业治理与运维

- 四级 RBAC、用户创建/停用、会话自动撤销和密码更新；
- 密码复杂度、连续登录失败锁定和账号停用后的全会话撤销；
- 产品质量规则不可覆盖式版本历史，保留修改人、原因和版本号；
- 审计事件自动关联操作者、客户端 IP 和请求 ID，并通过 SHA-256/HMAC 前向哈希链检测篡改；
- 订单状态机、复核任务原子认领和版本递增，避免关闭订单继续生产或多人同时复核；
- 事务型 Outbox 在质检、复核和订单状态变更时原子生成集成事件，供 MES/QMS 连接器可靠消费和确认；
- 工位 API Key 哈希保存、密钥轮换、设备心跳和软件版本上报；
- 原始证据使用不透明文件名和 SHA-256，必须通过鉴权接口访问，且访问行为写入审计；
- JSON 结构化访问日志、Trusted Host、安全响应头和单节点限流；
- 统一错误码与请求 ID、分页筛选质检记录、14 天质量趋势、质量率和 P50/P95/P99 延迟；
- SQLite WAL、忙等待、数据库结构迁移记录、在线一致性备份及 SHA-256 校验；
- 开发和生产 Compose 覆盖配置、只读容器文件系统、资源限制、日志轮转和健康检查。
- 非 root 容器、Ruff、覆盖率门禁、pip-audit、CodeQL、Dependabot 和 pre-commit。

## 五、系统架构

```text
┌────────────────────────────────────────────────────────────┐
│ HTML/CSS/JavaScript Web（当前）                            │
│ 工位扫描 | 人工复核 | 产品规则 | 质量追溯 | 统计看板       │
└───────────────────────────┬────────────────────────────────┘
                            │ REST / WebSocket
┌───────────────────────────▼────────────────────────────────┐
│ FastAPI Application                                        │
│ API层 | 认证授权 | 扫描编排 | 规则引擎 | 追溯服务          │
└──────────────┬───────────────────────────────┬─────────────┘
               │                               │
┌──────────────▼──────────────┐  ┌────────────▼──────────────┐
│ OCR / QR Service            │  │ Current Infrastructure    │
│ PaddleOCR + OpenCV          │  │ SQLite WAL / 本地证据存储 │
│ 字段提取 / 图像质量         │  │ 审计日志 / 指标 / 备份    │
└─────────────────────────────┘  └───────────────────────────┘
```

### 当前实现与生产演进

| 层级 | 技术选择 |
|---|---|
| OCR | Demo Provider + 可选 PaddleOCR PP-OCR 适配器 |
| 图像处理 | OpenCV |
| 条码解析 | OpenCV QRCodeDetector；目标为 zxing-cpp 多码制 |
| API | FastAPI、Pydantic |
| 数据库 | 当前 SQLite WAL + 增量迁移；多实例目标为 PostgreSQL/Alembic |
| 缓存与队列 | 当前无外部依赖；多实例目标为 Redis/消息队列 |
| 图片存储 | 当前本地不可变文件名；生产目标为 MinIO/S3 |
| 前端 | 当前原生 HTML/CSS/JavaScript；复杂管理端可演进为 React + TypeScript |
| 部署 | Docker Compose |
| 测试 | Pytest、Playwright |
| 可观测性 | Prometheus 指标、结构化日志、请求 ID |

## 六、主要数据模型

| 实体 | 作用 |
|---|---|
| `ProductModel` | 产品型号、标签模板和有效规则版本 |
| `ProductionOrder` | 当前订单、产品型号、批次和计划数量 |
| `Workstation` | 工位、相机及工位状态 |
| `Inspection` | 每次扫描的总记录和最终判定 |
| `RecognizedField` | OCR/条码提取的字段、位置和置信度 |
| `RuleResult` | 每条校验规则的输入、结论和原因 |
| `ReviewTask` | 低置信度或规则冲突的复核任务 |
| `AuditLog` | 规则、结果和人工操作的审计记录 |
| `ProductRuleVersion` | 产品质量规则版本、修改人和修改原因 |
| `User` / `AuthSession` | 用户角色、密码摘要和可撤销会话 |
| `OutboxEvent` | 面向 MES/QMS/ERP 的待投递领域事件和消费状态 |

## 七、当前已实现范围

当前版本实现了最能证明业务闭环与工程能力的功能：

1. 创建产品型号并配置 SN、批次正则；
2. 创建生产订单；
3. 上传图片并调用 PaddleOCR；
4. 提取 SN、型号和批次字段；
5. 解析二维码并与 OCR 文字核对；
6. 检查必填、格式、重复 SN 和订单型号；
7. 输出 PASS、REVIEW 或 REJECT；
8. 保存检测记录和图片；
9. 提供人工复核页面；
10. 提供按 SN 查询的追溯页面。
11. 四级角色权限、登录会话、安全退出与接口级鉴权；
12. 请求 ID、内存限流、安全响应头、存活/就绪探针和 Prometheus 指标；
13. 审计日志、CSV 导出、GitHub Actions 持续集成及图片伪装/畸形文件防护。

MVP 不包含 PLC 控制、真实传送带、复杂微服务拆分或模型微调，避免在没有真实工厂条件时制造虚假复杂度。

## 八、迭代路线

### Phase 1：可演示 MVP

- 图片上传、OCR、二维码解析；
- 三个核心字段提取；
- 产品规则和订单匹配；
- 检测记录、人工复核和 SN 追溯；
- SQLite 开发模式和基础 Web 界面。

### Phase 2：工程化（已完成当前单机参考实现）

- PostgreSQL、Alembic 和对象存储；
- 用户权限、审计日志和规则版本；
- 批量扫描、缓存和幂等处理；
- Docker Compose、单元测试和 API 测试；
- 质量统计看板。

其中 RBAC、审计、规则版本、幂等、Docker、测试、质量看板和监控指标已经实现；PostgreSQL、Redis 和 MinIO 属于下一阶段的多实例基础设施替换。

### Phase 3：工业化增强

- USB/IP 相机接入和 WebSocket 实时状态；
- OpenVINO/ONNX Runtime 加速；
- 多工位并发和离线缓冲；
- PLC/MES 模拟接口；
- 模糊、曝光和标签缺失的图像质量门禁；
- 与 PCB 少样本异常检测模块整合。

## 九、验收指标

指标必须通过固定测试集和真实运行日志计算，不在项目开始时虚构准确率。

| 类别 | 指标 |
|---|---|
| OCR | 字符准确率、完整 SN 识别率、字段提取 F1 |
| 条码 | 解码成功率、OCR/码值一致性准确率 |
| 规则 | 重复 SN、型号错误、缺字段检出率 |
| 系统 | P50/P95 延迟、吞吐量、失败率 |
| 业务 | 自动放行率、人工复核率、误拦截率 |
| 工程 | 单元测试覆盖率、API 错误处理、可复现部署 |

建议将测试集按场景分组：正常、倾斜、模糊、反光、低照度、小字符和遮挡，以展示系统边界，而不是只报告一个平均准确率。

## 十、求职展示重点

### 面试演示脚本

1. 创建产品型号和校验规则；
2. 创建一张生产订单；
3. 扫描正常标签，系统自动放行；
4. 扫描重复 SN，系统拒绝并给出原因；
5. 扫描 OCR 与二维码不一致的标签，进入人工复核；
6. 审核员修正识别结果并记录原因；
7. 使用 SN 查询完整质量档案；
8. 打开批次看板查看合格率与主要异常原因。

### 简历项目标题

**ElectroTrace——电子产品序列号识别与质量追溯平台**

### 简历描述模板

> 面向电子制造质检场景，使用 FastAPI、Pillow、SQLite 和可插拔 PaddleOCR 适配器开发产品序列号识别与质量追溯平台；设计重复 SN、订单型号、批次及 OCR/码值一致性规则，构建 PASS/REVIEW/REJECT、人工复核与产品级追溯闭环；实现四级 RBAC、审计日志、幂等请求、恶意图片校验、Prometheus 指标、CSV 导出、Docker 部署与 API 自动化测试。

完成系统后，应追加真实数据，例如：

> 在自建的 XX 张多场景标签测试集上，完整 SN 识别率为 XX%，P95 处理延迟为 XX ms，人工复核率为 XX%。

## 十一、参考项目

本项目借鉴以下仓库的能力和工程思路，不直接复制其业务实现：

- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)：OCR 预训练模型、本地推理和多后端部署；
- [IntelliTrace](https://github.com/shreyakarmakar04/IntelliTrace-Smart_Traceability_and_Labelling_Framework)：电子产品标签、条码、质量判定和追溯流程；
- [goods-in-label-ocr](https://github.com/gunaprakshkethineni/goods-in-label-ocr)：OCR/条码与产品目录交叉校验，以及接受、暂扣、拒绝的业务决策；
- [flexbone-ocr](https://github.com/therealahnaf/flexbone-ocr)：OCR Provider 抽象、FastAPI 服务、上传校验、限流、缓存和测试结构。

## 十二、合规与边界

- PaddleOCR 主项目使用 Apache-2.0 许可证，但部署时仍需核对具体模型、字体和第三方依赖许可；
- 示例产品、SN 和订单必须使用合成数据，不提交真实企业敏感信息；
- OCR 置信度不能直接等价于业务正确率；关键放行规则应支持人工复核；
- 项目是求职和工程演示系统，不宣称未经现场验证即可直接控制真实生产线。

更完整的生产化边界和 PostgreSQL、MinIO、OIDC、MES/QMS 集成路径见 [`docs/ENTERPRISE_ARCHITECTURE.md`](docs/ENTERPRISE_ARCHITECTURE.md)，漏洞处理要求见 [`SECURITY.md`](SECURITY.md)。

生产部署、告警、备份与故障处置见 [`docs/OPERATIONS_RUNBOOK.md`](docs/OPERATIONS_RUNBOOK.md)。

逐项生产就绪状态和仍需企业现场提供的外部条件见 [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md)。

## 十三、开源与贡献

本项目以 [Apache License 2.0](LICENSE) 发布。提交问题前请阅读 [安全策略](SECURITY.md)，参与开发请参考 [贡献指南](CONTRIBUTING.md)，版本变化记录在 [CHANGELOG](CHANGELOG.md)。仓库中的账号、订单、序列号和标签图片均为合成演示数据，不包含真实企业生产信息。
