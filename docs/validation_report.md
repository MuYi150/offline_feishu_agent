# 验证报告

验证日期：2026-08-08；环境：Windows，Conda `feishu-api`，Python 3.11.15。

## 已执行基线

```powershell
conda run --no-capture-output -n feishu-api python -m unittest discover -s tests -v
```

在 v1 `wiki/` 中执行：136 tests，全部通过。v2 实施未修改 v1 文件。

## v2 验证

```powershell
conda run --no-capture-output -n feishu-api python -m pytest -q
```

结果：`77 passed, 2 skipped`，耗时 11.35 秒。两个 skipped 均为下述真实 Kimi 案例。新增覆盖包括 Blocks 优先、原生 PDF 文字回退、PNG/图片忽略、可读摘要去重与长度限制、技术实体和参数、字符 2～4 gram TF-IDF、四项权重公式、SQLite 自动建表/UPSERT/自身排除、阈值与 Top-K、旧 Fixture 固定候选禁用、Prompt 只携带历史摘要、pass 入库规则和索引节点 checkpoint 恢复。

另外执行了 CLI Fake 验证：

- `basic_pass`：退出码 0，结果 `pass`，生成全部产物。
- `long_pdf`：退出码 0，结果 `pass`，执行 2 个视觉批次和 1 个最终审稿调用。
- `invalid_model_output`：退出码 1，错误分类 `model_non_json`，没有生成成功状态或通知。
- `basic_pass --real-model`（无 Key）：退出码 1，错误分类 `model_authentication_error`，生成安全失败 trace，不产生通知。
- `pip check`：`No broken requirements found.`
- 输出敏感信息扫描：未发现 `;base64,`、Authorization 或 Bearer 内容。

## 连续相似性 Fixture 验收

使用独立临时索引及输出目录执行：初始化 → `similarity_drone_source` → 查看索引 → `similarity_drone_candidate` → 查看索引。

- 初始化记录数：0。
- 第一篇结果：`pass`；画像来源：`blocks`；摘要 893 字符；运行后记录数：1。
- 第二篇结果：`pass`；召回 `test_doc_similarity_drone_source`；运行后记录数：2。
- 分项分数：摘要 TF-IDF `0.383671`、标题 `0.444444`、关键词 `0.4`、技术实体 `0.769231`。
- 最终分数：`0.413698`，超过默认阈值 `0.35`，且 `selected_for_prompt=true`。

## 本地历史自动复审验收

2026-08-12 使用独立的临时 state、output 和相似性索引目录，按顺序运行 `drone_hardware_rd_round1_need_revision` 与 `drone_hardware_rd_round2_pass`：

- round1 无本地历史，自动进入第 1 轮初审，结果为 `need_revision`。
- round2 的 Fixture 不包含 `previous_issues`，系统按相同 document_id 命中 round1 的本地历史并自动进入第 2 轮复审，结果为 `pass`。
- round2 Prompt 加载了 round1 实际产生的 `drone-major-mcu-selection`、`drone-major-power-design`、`drone-major-verification-criteria`，并包含位置、问题、建议和证据 ID。
- round2 的 resolutions 精确覆盖上述三个动态 issue_id；复审未重新召回相似候选。
- 本地历史最终包含 `history-round1` 和 `history-round2` 两条记录，轮次依次为 1、2。
- 完整测试结果为 `88 passed, 2 skipped`，最终复跑耗时 13.46 秒；两个 skipped 仍为未显式启用的真实 Kimi 测试。
- 第二篇 Prompt 已实际包含第一篇 document_id、标题、`similarity_score` 和完整本地摘要。
- 新增产物及 SQLite 扫描未发现 `;base64,`、Authorization、Bearer 或测试 Key。
- `pip check`：`No broken requirements found.`；`git diff --check` 通过。

## 真实 Kimi

当前 `KIMI_API_KEY` 和 `MOONSHOT_API_KEY` 均未设置。两个 `real_kimi` 案例因缺少 Key 被显式跳过，未使用 Fake 冒充真实结果。这是唯一外部验证阻塞项。

## 2026-08-14 AI 文章概述索引升级

- 全量测试：`98 passed, 2 skipped`，耗时 15.34 秒；两个 skipped 仍为未显式授权的真实 Kimi 冒烟测试。
- 独立临时 SQLite 从 0 条开始连续运行 `similarity_drone_source` 和 `similarity_drone_candidate`，两篇均为 `pass`，最终 v2 索引为 2 条 `ai_article_overview`。
- source 查询正文 828 字符，模型概述 325 字符；数据库 `content` 与模型 `article_overview.content` 逐字一致，且与本地 `query_text` 不同。
- candidate 召回 source，最终分数 `0.434914`：TF-IDF `0.416504`、标题 `0.444444`、主题关键词 `0.384615`、实体 `0.909091`、参数 `0.3`，超过默认阈值 `0.35`。
- candidate Prompt 包含 source 的 document_id、标题、分数和完整 AI 概述，不包含历史 PDF、PNG、页面路径或 Base64。
- 同一临时环境运行无人机 round1/round2：round1=`need_revision` 后索引仍为 2 条，round2=`pass` 后新增/更新为 3 条，证明只有复审通过后才写入概述。
- 将开发环境现有 SQLite v1 复制到临时目录后原位迁移：记录数保持 1，Schema 升至 v2，旧记录标记为 `deterministic_legacy`；原始数据库 SHA-256 未变化。
- 新增产物和 Prompt 安全扫描未发现 `Authorization`、`Bearer`、`;base64,` 或测试 Key。

## 2026-08-17 PDF 文字优先与视觉区域选择

- 全量 Fake/单元/集成测试：`111 passed, 2 skipped`，最终复跑耗时 17.71 秒；两个 skipped 为未显式启用的真实 Kimi 冒烟测试。
- 16 页 `long_pdf` 改为 PDF-only 原生文字案例：`mode=selective_regions`，提取 16 页带页码正文，发送 0 张图片，仅调用 1 次 `final_review`；旧流程为 2 次 `visual_batch` 加 1 次最终审稿。
- 14 页 `long_pdf_mixed` 发送 4 个局部/兜底视觉项：内嵌测试图、复杂表格、矢量架构图、扫描页整页兜底；重复图片按 SHA-256 去重，没有发送 14 张整页图，也没有 `visual_batch`。
- 短篇 `multimodal_pass` 保持 `legacy_full_pages`、原 `page-N` evidence ID 和单次 `final_review`。
- 将视觉区域预算压缩到 1 且禁止整页兜底后，审计记录必要内容未提交，`visual_pages_complete=false`，Fake 的 pass 自动归一为 `incomplete_review`。
- 新增 `document_extraction.json` 和 `visual_selection.json`；请求摘要记录图片类型和 bbox。安全扫描未发现 Base64、Authorization、Bearer 或 API Key。

## 2026-08-19 两阶段 AI 检索概述

- 全量 Fake/单元/集成测试：`122 passed, 2 skipped`；两个 skipped 为未显式启用的真实 Kimi 测试。
- 使用独立临时 output、state 和 SQLite 连续运行 `similarity_drone_source`、`similarity_drone_candidate`，两篇均为 `pass`，调用顺序均为 `retrieval_overview → final_review`，概述阶段图片数为 0。
- source 的检索概述成功写入；candidate 从 1 条历史记录中召回 source，最终分数 `0.371560`，超过默认阈值 `0.35`。分项为：概述 TF-IDF `0.263456`、标题 `0.444444`、主题 `0.75`、实体 `0.727273`、参数 `0.125`、方法 `0.6`、场景与验证 `0.428571`。
- candidate Prompt 包含 source 的 document_id 和完整检索概述，不包含历史 `source.pdf`、页面图片路径或 Base64。临时 SQLite Schema 为 v3，最终两条记录均标记为 `ai_retrieval_overview`。
- 新增 Prompt、JSON、trace 和临时 SQLite 文本安全扫描未发现 Authorization、Bearer、Base64 或测试 Key。
