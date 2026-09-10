# 科研团队飞书知识库审稿 Agent v2

这是一个使用 LangGraph 编排的多模态审稿系统。本地 CLI 保留 Fixture、离线 Fake 和真实 Kimi 模式；新增独立线上入口 `python -m wiki_review_v2.online`，支持飞书普通同步、自动 Fixture、审稿回写、通知重试和公示索引。线上配置、运行命令和切换步骤见 [ONLINE.md](ONLINE.md)，默认只读，线上修改必须显式 `--write`，模型另需 `--real-model`。

## 安装

项目必须使用既有 Conda 环境 `feishu-api`，不要创建新虚拟环境：

```powershell
conda run --no-capture-output -n feishu-api python -m pip install -e ".[dev]"
```

PDF 原生文字、表格和视觉区域提取使用 PyMuPDF，轻量感知去重使用 Pillow；如需单独安装：

```powershell
conda run --no-capture-output -n feishu-api python -m pip install "PyMuPDF>=1.24,<2" "Pillow>=10,<13"
```

## 配置 Kimi

复制 `.env.example` 中需要的变量到当前终端环境。程序不会自动读取或修改 `.env`，也不会把 Key 写入日志或产物。

```powershell
$env:KIMI_API_KEY = "your-key"
$env:KIMI_BASE_URL = "https://api.moonshot.cn/v1"
$env:KIMI_MODEL = "kimi-k3"
```

默认 `ALLOW_REAL_MODEL_CALL=0`。CLI 的 `--real-model` 本身是一次显式授权；程序仍会在没有 Key 时安全失败。模型、超时、重试、PDF DPI、图片尺寸、长短文分流、视觉预算、最大页数和输入上限均可通过 `.env.example` 中的变量覆盖。

## 运行

列出 Fixture：

```powershell
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --list-cases
```

默认安全模式使用 Fake 模型，不访问网络：

```powershell
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --case basic_pass --fake-model
```

显式调用真实 Kimi：

```powershell
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --case multimodal_pass --real-model
```

恢复中断的运行：

```powershell
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --resume outputs\basic_pass\<run_id>
```

恢复使用 `run_metadata.json` 中的 case、模式和 thread ID，从 SQLite checkpoint 中失败节点之前的最近快照继续。已完成的模型节点不会重复运行；输出写入采用摘要一致的幂等检查。

## 本地审稿历史与自动复审

本地跨运行历史存储于 `local_state/review_history/<document_id>.json`。Fixture 的 `review_round=0` 明确进入初审；大于零时必须加载对应轮次的真实历史，只把 blocking/major 问题交给模型，跳过相似召回。旧 `previous_issues` 只供兼容解析，不替代历史。线上使用独立 SQLite 历史，并在表格写入确认后提交有效记录；每次运行目录内的 checkpoint 用于恢复该次运行。

新历史记录包含 `run_id`、系统推导的 `review_round`、`completed_at` 和完整审稿结果。相同 `run_id` 重试采用幂等写入；旧记录没有时间字段时按 records 数组顺序，以最后一条为最新。历史文件损坏或 Schema 不合法会产生 `review_history_error` 并停止，不会静默降级为初审，也不会移动或覆盖原历史文件。

无人机两轮 Fake 验收可先只删除该测试文章的历史，再依次运行：

```powershell
$history = "D:\NEU\feishu\WIKI_V2\local_state\review_history\test_doc_drone_hardware_rd.json"
Remove-Item -LiteralPath $history -Force -ErrorAction SilentlyContinue
$stamp = Get-Date -Format "yyyyMMddTHHmmss"

& "E:\tools\conda\envs\feishu-api\python.exe" -m wiki_review_v2.cli --case drone_hardware_rd_round1_need_revision --run-id "history-round1-$stamp"
& "E:\tools\conda\envs\feishu-api\python.exe" -m wiki_review_v2.cli --case drone_hardware_rd_round2_pass --run-id "history-round2-$stamp"
```

每次运行的 `review_history_lookup.json` 记录是否命中历史及选中轮次；`review_history.json` 区分本次运行前加载的 `previous_review` 和本次产生的 `current_review`。

## 本地文字相似性索引（线上公示索引见 ONLINE.md）

初审不再使用 Fixture 中预设的 `similarity_candidates.json` 分数。系统优先用有效 Blocks，Blocks 不可用时读取 Fixture 原始 `source.pdf` 文字层；第一次纯文字模型调用把当前正文转换为 `retrieval_article_overview`，再与 SQLite 中历史检索概述对称比较。达到阈值的历史概述进入第二次正式多模态审稿。第一次调用不发送图片、附件或候选，第二次调用才发送当前正文、选定视觉证据和候选概述；历史 PDF、图片和完整正文永不进入 Prompt。

正常初审因此有两次模型调用：`retrieval_overview` 和 `final_review`。复审仍跳过相似候选召回，但会生成修改后概述，复审 pass 后 UPSERT 同一 document_id。第一次概述是检索和索引的权威内容；正式审稿 JSON 中原有 `article_overview` 继续保留用于结果兼容，两者无需逐字相同。

默认配置如下，程序仍沿用现有环境变量读取方式，不会自动加载 `.env`：

```powershell
$env:SIMILARITY_INDEX_PATH = Join-Path $PWD "local_state\similarity_index\articles.sqlite"
$env:SIMILARITY_THRESHOLD = "0.35"
$env:SIMILARITY_TOP_K = "5"
$env:SIMILARITY_OVERVIEW_MAX_CHARS = "1200"
$env:SIMILARITY_QUERY_MAX_CHARS = "30000"
$env:RETRIEVAL_OVERVIEW_MIN_CHARS = "100"
$env:RETRIEVAL_OVERVIEW_MAX_CHARS = "1200"
$env:RETRIEVAL_OVERVIEW_PROMPT_VERSION = "retrieval_overview_v1"
$env:RETRIEVAL_OVERVIEW_MODEL = ""
```

当前检索概述与历史概述使用统一字符 2～4 gram TF-IDF 空间。概述正文、标题、主题、技术实体、参数、方法、应用场景与验证方式的权重分别为 `0.60/0.10/0.10/0.05/0.05/0.05/0.05`。正式结果为 `pass`、检索概述合法且正文哈希一致时 UPSERT；相同 `document_id` 更新原记录。SQLite v1/v2 会原位迁移到 v3，旧规则摘要和旧正式审稿概述保留并继续召回，新记录标记为 `ai_retrieval_overview`。

初始化和查看索引：

```powershell
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --init-similarity-index
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --similarity-index-info
```

连续运行两个无人机 Fixture。下面使用专门的演示索引和输出目录，不影响默认索引或已有 outputs：

```powershell
$env:SIMILARITY_INDEX_PATH = Join-Path $PWD "local_state\similarity_demo\articles.sqlite"
$env:WIKI_V2_OUTPUT_ROOT = Join-Path $PWD "local_state\similarity_demo_outputs"
Remove-Item -LiteralPath $env:SIMILARITY_INDEX_PATH -Force -ErrorAction SilentlyContinue
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --init-similarity-index
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --case similarity_drone_source --fake-model --run-id source
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --case similarity_drone_candidate --fake-model --run-id candidate
conda run --no-capture-output -n feishu-api python -m wiki_review_v2.cli --similarity-index-info
```

查看第二次召回审计和实际 Prompt：

```powershell
Get-Content -Raw -Encoding UTF8 "$env:WIKI_V2_OUTPUT_ROOT\similarity_drone_candidate\candidate\similarity_retrieval.json"
Get-Content -Raw -Encoding UTF8 "$env:WIKI_V2_OUTPUT_ROOT\similarity_drone_candidate\candidate\prompt.txt"
```

清空演示索引时只删除显式配置的测试文件，不要对 `local_state` 或项目目录做递归删除：

```powershell
Remove-Item -LiteralPath $env:SIMILARITY_INDEX_PATH -Force
```

## 多模态页面如何进入模型

正文 Block 首先被转换为 Markdown。短篇 PDF 保持逐页渲染，每页使用稳定的 `page-N` evidence ID。长篇 PDF 则先提取带 `[PDF第N页]` 标记的原生文字，并用本地规则提取内嵌图片、复杂表格、矢量图和扫描页兜底。Prompt Builder 只生成文字和视觉清单；Kimi 适配器在内存中把视觉标签以及对应的 `image_url` 加入 `message.content`，Base64 不会拼进 Prompt 或写入产物。

短篇只有在页数不超过 12、整页图片不超过 12 张且总字节不超过 15 MiB 时，才一次提交全部页面。否则进入 `selective_regions`：普通文字页不发送截图，简单表格转 Markdown，真正需要视觉理解的区域按优先级、SHA-256 和 dHash 去重后提交。默认长短流程都只调用一次 `final_review`，不再运行 `visual_batch`；只有显式设置 `LONG_PDF_LEGACY_BATCH_FALLBACK=true` 才恢复旧长文批次方案。

默认视觉配置：

```powershell
$env:DIRECT_PAGE_LIMIT = "12"
$env:DIRECT_IMAGE_COUNT_LIMIT = "12"
$env:DIRECT_IMAGE_BYTES_LIMIT = "15728640"
$env:MAX_VISUAL_REGIONS = "60"
$env:MAX_VISUAL_TOTAL_BYTES = "15728640"
$env:MAX_FULL_PAGE_FALLBACKS = "4"
$env:LONG_PDF_LEGACY_BATCH_FALLBACK = "false"
```

任何正文截断、PDF 损坏、提取失败或必要视觉区域因预算未提交，都会写入 `input_coverage`；覆盖不足时不能 pass/reject。

审稿正文：
可靠 Blocks 优先；没有可靠 Blocks 时使用带页码的 PDF 原生文字

审稿视觉：
PDF 优先，其次 pages，最后由 Blocks 自动生成 PDF

相似性文字：
有效 Blocks 优先，否则读取原始 PDF 文字层；
永远不读取 pages，不做 OCR

## 输出文件

每次新运行创建 `outputs/<case_id>/<run_id>/`，已存在目录不会覆盖：

- `source_document.json`：输入元数据快照。
- `extracted_content.json`：结构化 Markdown 及统计。
- `pages/`：短篇旧流程的整页图；长篇局部图片位于 `visual_regions/`。
- `document_extraction.json`：逐页文字来源、字符数、表格/图片/图表、扫描页和失败页。
- `visual_selection.json`：视觉候选、过滤/去重、优先级、预算、选中项和必要遗漏。
- `visual_manifest.json`：实际提交的整页或局部视觉项、类型、evidence ID、bbox、尺寸和哈希。
- `visual_evidence.json`：兼容旧批次流程的证据；默认长篇新流程不产生批次内容。
- `input_coverage.json`：本次实际可见范围及限制。
- `similarity_profile.json`：当前文章的文字来源、清洗查询正文、本地关键词/实体/参数、字符数、截断状态和内容哈希。
- `similarity_retrieval.json`：本地索引候选数、七项分数、概述来源/版本、阈值判断和最终 Prompt 候选。
- `retrieval_article_overview.json`：第一次纯文字调用生成的权威检索概述、正文哈希、质量校验和是否写入索引。
- `article_overview.json`：第二次正式审稿返回的兼容文章概述及质量审计，不作为新索引记录的权威内容。
- `prompt.txt`：纯文字 Prompt，不含图片 Base64。
- `model_request_summary.json`：模型、阶段、耗时、token 和图片摘要，不含认证信息。
- `raw_model_output.json`：仅保存最终 message content 和安全响应元数据，不保存推理内容或完整 SDK 响应。
- `parsed_review_result.json`：经过 Parser、Normalizer、Validator 的最终结果。
- `submitter_notification.txt` / `admin_notification.txt`：由代码确定性生成的两类消息草稿。
- `review_history.json`：本次运行的历史快照。
- `review_history_lookup.json`：按 document_id 查询本地历史的命中与选中记录审计。
- `run_trace.json`：节点、分支、失败分类和诊断轨迹。
- `checkpoint.sqlite`：LangGraph 本地 checkpoint。

模型/API/JSON 失败只生成 `failure.json`、trace 和 checkpoint，不生成成功状态或通知。

## 添加 Fixture

每个案例放在 `fixtures/<case_id>/`。为方便后续从 v1 平移，v2 沿用 v1 的 Fixture 外部契约，不把简化 DTO 写进测试数据：

- `source_document.json` 保留 v1 字段：`case_id`、`document_id`、`node_token`、`title`、`wiki_name`、`author_id`、`author`、`link`、`review_method`、`status`、`review_round`、`updated_at`、`last_ai_review_at`。旧 Fixture 可继续携带 `previous_issues` 供 Loader 兼容解析，但 Graph 不使用它；模式由 review_round 决定，复审需匹配已提交历史。
- `document_blocks.json` 保持 `{"blocks": [...]}` 包装，内部使用飞书风格的 `block_type`、`text.elements[].text_run.content`、表格子块等原始结构。
- `attachment_metadata.json` 保持 `{"document_id": "...", "attachments": [...]}`。
- `mock_overview_result.json` 提供第一次检索概述 Fake 响应，`mock_llm_result.json` 继续提供正式审稿响应；两者都与 `expected_result.json` 分离，避免测试自证。
- `expected_result.json` 保持 v1 的预期摘要字段。相似候选是 v2 扩展，采用 `similarity_candidates.json` 的 `similar_documents` 包装。
- 页面素材继续使用约定文件 `source.pdf` 或 `pages/`。仅 v2 才需要的“固定 PDF、模拟缺页”等测试控制放入可选 `fixture_options.json`，不污染 v1 的 source 格式。
- `fixture_options.real_model_only=true` 表示案例只用于真实模型手动运行；Fake CLI 会明确拒绝，而不会伪造模型结果。

Loader 也兼容早期 v2 的简化 Blocks、列表式附件/相似候选、`fake_model_response.json` 和独立 `previous_review.json`，但新建 Fixture 应优先使用上面的 v1 格式。

仓库中的示例由下列命令可重复生成；命令默认拒绝覆盖，重新生成必须显式传 `--force`：

```powershell
conda run --no-capture-output -n feishu-api python scripts\generate_fixtures.py
conda run --no-capture-output -n feishu-api python fixtures\generate_pdf_fixtures.py
```

短篇、长篇纯文字和长篇图文的 Fake 验收：

```powershell
& "E:\tools\conda\envs\feishu-api\python.exe" -m wiki_review_v2.cli --case multimodal_pass
& "E:\tools\conda\envs\feishu-api\python.exe" -m wiki_review_v2.cli --case long_pdf
& "E:\tools\conda\envs\feishu-api\python.exe" -m wiki_review_v2.cli --case long_pdf_mixed
```

从命令返回的 `output_dir` 查看新审计和实际请求：

```powershell
Get-Content -Raw -Encoding UTF8 "<output_dir>\document_extraction.json"
Get-Content -Raw -Encoding UTF8 "<output_dir>\visual_selection.json"
Get-Content -Raw -Encoding UTF8 "<output_dir>\model_request_summary.json"
```

## 测试

```powershell
conda run --no-capture-output -n feishu-api python -m pytest -q
```

真实冒烟测试默认跳过。只有同时提供 Key 和显式开关时才会访问 Kimi：

```powershell
$env:RUN_REAL_KIMI_TESTS = "1"
conda run --no-capture-output -n feishu-api python -m pytest -q -m real_kimi
```

更多说明见 [架构](docs/architecture.md)、[v1→v2 能力映射](docs/v1_v2_mapping.md)、[已知限制](docs/known_limitations.md) 和 [验证报告](docs/validation_report.md)。


## 可以优化的地方

1. 长篇 PDF 已使用本地文字优先和视觉区域筛选；后续可用真实语料继续校准表格、矢量图和视觉优先级规则。

2. 本地 Fake 测试继续在 AI 通过后写入测试索引；线上生产索引仅在公示资格和版本确认后写入，二者隔离。

3. 本地案例历史仍采用按 document_id 分文件的 JSON records 数组；线上历史与交付使用 SQLite 事务和操作系统运行锁，防止定时作业重叠。
