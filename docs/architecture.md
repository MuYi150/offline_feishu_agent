# v2 架构说明

## 边界

系统只有一个核心 `ReviewAgent` 工作流。LangGraph 负责节点顺序、条件边和 checkpoint；规则、PDF、Prompt、模型、校验、通知和存储都是可以脱离 Graph 单测的普通 Python 组件。Fixture 是本阶段唯一文档来源，本地目录是唯一结果落点。Fixture adapter 在边界保留 v1 的 source 字段、原始飞书 Blocks 包装、附件包装和 mock behavior 文件，再统一转换为 v2 内部 DTO；业务组件不依赖测试文件的传输形态。

```mermaid
flowchart TD
    F["FixtureDocumentSource"] --> E["DocumentExtractor"]
    E --> T["SimilarityProfile：Blocks → 原生 PDF 文字"]
    T --> P["PDF 生成或读取"]
    P --> R["PdfPageRenderer"]
    R --> C["InputCoverage"]
    C --> M{"首轮或复审"}
    M -->|首轮| S["SQLite 历史摘要 + 本地 TF-IDF 召回"]
    M -->|复审| H["上一轮 blocking/major"]
    S --> B["Prompt + MultimodalInput"]
    H --> B
    B --> L{"长 PDF"}
    L -->|是| V["分批 VisualEvidence"]
    L -->|否| K["最终审稿"]
    V --> K
    K --> J["Parser → Normalizer → Validator"]
    J --> O["OutcomeMapper + Notifications"]
    O --> A["Atomic Artifacts + History"]
    A --> U["合格 pass 画像 UPSERT"]
    C -->|关键输入不可用| I["确定性 incomplete_review"]
    I --> J
    K -->|API/JSON 失败| X["Safe failure trace，无状态/通知"]
```

## 数据契约

`ReviewGraphState` 是 Pydantic 状态模型，checkpoint 内仅保存 JSON 可序列化值。文字召回增加 `SimilarityProfile`、`SimilarityScoreDetails` 和 `SimilarityRetrievalAudit`。Fixture Loader 继续兼容 `similarity_candidates.json`，但 Graph 不使用其中的数据，正式候选只来自本地索引。

模型原始输出必须匹配 `ModelReviewPayload` 的严格 JSON Schema。模型不负责生成通知和本地状态；最终结果必须依次经过 JSON Parser、类别/等级/计数归一化、覆盖和结论一致性校验，然后才能映射状态。

## 文字相似性数据流

画像构建位于正文提取之后。有效 Blocks 优先；否则只读取 Fixture 原始 PDF 文字层，不读取生成 PDF、PNG、渲染页面或内嵌图片，也不执行 OCR。摘要以标题、章节、可读关键词、技术实体、参数和原文代表句组成，最大长度由配置控制。

SQLite 查询在 SQL 层排除当前 document_id。正文摘要使用字符 2～4 gram TF-IDF 余弦，标题使用规范化字符相似度，关键词和技术实体使用 Jaccard；固定权重为 `0.70/0.15/0.10/0.05`。超过阈值的 Top-K 摘要进入 Prompt，模型仍负责判断主题独立、重复关系和修改必要性。

## 可恢复性与副作用

每个运行目录持有独立 `checkpoint.sqlite` 和 thread ID。节点边界形成 checkpoint；恢复时定位失败节点之前的最近快照并以相同 thread ID 继续。模型调用、产物落盘和相似画像持久化分属不同节点，因此索引 UPSERT 中断时不会重新调用模型。

所有正式 JSON 产物先写同目录临时文件、刷新并 `os.replace`。普通产物只允许首次写入或内容完全相同的幂等写入；持续更新的 trace 使用原子替换。历史文件保留 records 数组，损坏的历史会被隔离为带时间戳的 `.corrupt-*` 文件。

## 安全

- 真实模型必须由 `--real-model` 或程序级允许开关显式授权，并且必须提供 Key。
- SDK 自动重试关闭；适配器只对超时、429、连接错误和 5xx 做有上限的退避重试。
- Key 使用 `SecretStr`，安全配置快照只记录 Key 是否存在。
- Prompt、日志、trace 和请求摘要不保存 Base64、Authorization、完整请求头或 reasoning content。
- 认证错误、模型空响应、非 JSON 和 Schema 错误不会生成业务状态或通知。
