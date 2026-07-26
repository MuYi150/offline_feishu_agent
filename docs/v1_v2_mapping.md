# v1 → v2 能力映射

| v1 能力 | v2 第一阶段实现 | 说明 |
|---|---|---|
| Docx Blocks/raw_content 提取 | `DocumentExtractor` 读取 Fixture Blocks | 表格保留为 Markdown；`【表格】` 不再视为占位符 |
| PDF 导出与 OCR | Fixture PDF、正文生成 PDF、PyMuPDF 页面渲染 | Kimi 直接理解页面图，不以 OCR 为主流程 |
| 第一轮/复审判断 | `ReviewModePolicy` | review_round>1 或存在 previous_review 即复审 |
| 第一轮相似召回 | `SimilarityService` | 排除自身、无效状态、空内容和低分项，阈值 0.35、Top-5 |
| 复审历史 | `ReviewHistoryPolicy` | 只带入上一轮 blocking/major，不重复召回 |
| AI 审稿 | `KimiMultimodalModel` / `FakeReviewModel` | OpenAI-compatible K3 严格 Schema；Fake 默认无网络 |
| JSON 解析和后处理 | Parser → Normalizer → Validator | 重新计数、等级/类别归一、结论与覆盖强校验 |
| 状态映射 | `ReviewOutcomeMapper` | 保持五种结果和原中文状态语义 |
| 投稿人/管理员消息 | `NotificationRenderer` | 两类消息由确定性代码分别生成，不采用模型自由文本 |
| 审稿历史与审计 | 原子 JSON 历史、run trace、SQLite checkpoint | 每次运行唯一目录，不覆盖历史结果 |
| Fixture + Mock | 11 个 v2 Fixture 和 Fake Graph 测试 | Fake 响应与 expected 断言分离 |
| 在线飞书 source/sink | 第一阶段不实现 | 下一阶段通过公开 source/sink 接口接入 |

## 有意修复的历史问题

- 不再沿用 `sync_service.py` / `llm_client.py` 巨型文件或私有函数。
- Settings 在组合根加载一次，通过构造参数注入组件，不在函数内部重复读环境变量。
- 不使用全局模型客户端或固定生产路径。
- 本地 JSON 使用原子写入，不直接覆盖半成品。
- 已提取 Markdown 表格不触发 `content_placeholder` major。
- 输入覆盖改为 structured/visual/attachment/truncation 通用模型，不复用 OCR 专用字段。
- 页面缺失、PDF 损坏和输入超限不再静默处理；覆盖不足禁止 pass/reject。
- 排序、阈值、Top-N 和状态过滤由公开策略及测试明确固定。

