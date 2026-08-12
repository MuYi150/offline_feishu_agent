# 科研团队飞书知识库审稿 Agent v2

这是一个完全本地的多模态审稿系统。第一阶段只读取仓库内的 Fixture，使用 LangGraph 编排流程，并可选择离线 Fake 模型或显式调用真实 Kimi；它不会认证飞书、读取线上 Wiki/Sheet/Docx、写表、发消息、移动或修改文档。

## 安装

项目必须使用既有 Conda 环境 `feishu-api`，不要创建新虚拟环境：

```powershell
conda run --no-capture-output -n feishu-api python -m pip install -e ".[dev]"
```

文字相似性画像从原生 PDF 提取文字时使用 PyMuPDF；如需单独安装：

```powershell
conda run --no-capture-output -n feishu-api python -m pip install "PyMuPDF>=1.24,<2"
```

## 配置 Kimi

复制 `.env.example` 中需要的变量到当前终端环境。程序不会自动读取或修改 `.env`，也不会把 Key 写入日志或产物。

```powershell
$env:KIMI_API_KEY = "your-key"
$env:KIMI_BASE_URL = "https://api.moonshot.cn/v1"
$env:KIMI_MODEL = "kimi-k3"
```

默认 `ALLOW_REAL_MODEL_CALL=0`。CLI 的 `--real-model` 本身是一次显式授权；程序仍会在没有 Key 时安全失败。模型、超时、重试、PDF DPI、图片尺寸、批次页数、最大页数和输入上限均可通过 `.env.example` 中的变量覆盖。

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

## 本地文字相似性索引

初审不再使用 Fixture 中预设的 `similarity_candidates.json` 分数。系统优先用有效 Blocks，Blocks 不可用时读取 Fixture 原始 `source.pdf` 文字层，再生成可读摘要并与本地 SQLite 历史摘要比较。图片、PNG、渲染页面和 OCR 完全不参与相似性计算；历史文章只把摘要和分数放入 Prompt，不发送历史 PDF、图片或完整正文。

默认配置如下，程序仍沿用现有环境变量读取方式，不会自动加载 `.env`：

```powershell
$env:SIMILARITY_INDEX_PATH = Join-Path $PWD "local_state\similarity_index\articles.sqlite"
$env:SIMILARITY_THRESHOLD = "0.35"
$env:SIMILARITY_TOP_K = "5"
$env:SIMILARITY_SUMMARY_MAX_CHARS = "1200"
```

摘要字符 2～4 gram TF-IDF、标题、关键词和技术实体的固定权重分别为 `0.70/0.15/0.10/0.05`。只有经过完整结果校验、最终为 `pass`、摘要来源是 Blocks/PDF、正文未截断且没有缺失来源的文章才会 UPSERT；相同 `document_id` 更新原记录。

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

正文 Block 首先被转换为 Markdown。PDF 由 PyMuPDF 逐页渲染，每页获得稳定的 `page-N` evidence ID、一基页码、尺寸、字节数和 SHA-256。Prompt Builder 只生成文字和视觉清单；Kimi 适配器在内存中把页面标签以及对应的 `image_url` 数据项加入 `message.content` 数组，Base64 不会拼进 Prompt 或写入产物。

不超过 12 页的文档一次提交全部页面。长 PDF 以默认 8 页一批提取 `VisualEvidenceBatch`，随后把结构化正文、批次证据和最多 8 个关键页交给最终审稿调用。任何超限、损坏或缺页都会显式进入 `input_coverage`；系统不会静默截断，也不会在覆盖不足时强行 pass/reject。

## 输出文件

每次新运行创建 `outputs/<case_id>/<run_id>/`，已存在目录不会覆盖：

- `source_document.json`：输入元数据快照。
- `extracted_content.json`：结构化 Markdown 及统计。
- `pages/`：实际提交视觉流程的页面图。
- `visual_manifest.json`：页面、evidence ID、尺寸、哈希和失败页。
- `visual_evidence.json`：长 PDF 分批视觉证据。
- `input_coverage.json`：本次实际可见范围及限制。
- `similarity_profile.json`：当前文章的文字来源、可读摘要、关键词、技术实体和字符数。
- `similarity_retrieval.json`：本地索引候选数、四项分数、阈值判断和最终 Prompt 候选。
- `prompt.txt`：纯文字 Prompt，不含图片 Base64。
- `model_request_summary.json`：模型、阶段、耗时、token 和图片摘要，不含认证信息。
- `raw_model_output.json`：仅保存最终 message content 和安全响应元数据，不保存推理内容或完整 SDK 响应。
- `parsed_review_result.json`：经过 Parser、Normalizer、Validator 的最终结果。
- `submitter_notification.txt` / `admin_notification.txt`：由代码确定性生成的两类消息草稿。
- `review_history.json`：本次运行的历史快照。
- `run_trace.json`：节点、分支、失败分类和诊断轨迹。
- `checkpoint.sqlite`：LangGraph 本地 checkpoint。

模型/API/JSON 失败只生成 `failure.json`、trace 和 checkpoint，不生成成功状态或通知。

## 添加 Fixture

每个案例放在 `fixtures/<case_id>/`。为方便后续从 v1 平移，v2 沿用 v1 的 Fixture 外部契约，不把简化 DTO 写进测试数据：

- `source_document.json` 保留 v1 字段：`case_id`、`document_id`、`node_token`、`title`、`wiki_name`、`author_id`、`author`、`link`、`review_method`、`status`、`review_round`、`updated_at`、`last_ai_review_at`；复审案例可像 v1 一样内嵌 `previous_issues`。其中 `review_round=0` 表示尚未完成首轮，本次审稿轮次为 1；大于 0 表示复审。
- `document_blocks.json` 保持 `{"blocks": [...]}` 包装，内部使用飞书风格的 `block_type`、`text.elements[].text_run.content`、表格子块等原始结构。
- `attachment_metadata.json` 保持 `{"document_id": "...", "attachments": [...]}`。
- `mock_llm_result.json` 保持 v1 的 `behavior=return/raw/raise` 包装；Fake 响应与 `expected_result.json` 始终分离，避免测试自证。
- `expected_result.json` 保持 v1 的预期摘要字段。相似候选是 v2 扩展，采用 `similarity_candidates.json` 的 `similar_documents` 包装。
- 页面素材继续使用约定文件 `source.pdf` 或 `pages/`。仅 v2 才需要的“固定 PDF、模拟缺页”等测试控制放入可选 `fixture_options.json`，不污染 v1 的 source 格式。

Loader 也兼容早期 v2 的简化 Blocks、列表式附件/相似候选、`fake_model_response.json` 和独立 `previous_review.json`，但新建 Fixture 应优先使用上面的 v1 格式。

仓库中的示例由下列命令可重复生成；命令默认拒绝覆盖，重新生成必须显式传 `--force`：

```powershell
conda run --no-capture-output -n feishu-api python scripts\generate_fixtures.py
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

1.前主要依赖视觉审稿，将文档blocks和文档照片一起传入kimi大模型，消耗量较大，可以尝试的地方，更改Prompt组合方式，利用deepseek审稿，将视觉图片传入Kimi返回图片信息，多模型协同。

2.摘要归为本地索引的时间需要改为已公式后，目前为了测试方便改为ai通过后。

3.为了测试方便，初审和复审都采用了给定信息的fixture形式，应该在本地记录初审情况，如果不是初审查找以前本地审查结果，把上次检查到的问题交给大模型。当前代码“会自动保存历史”，但“不会在新一次复审运行时自动读取本地历史”。