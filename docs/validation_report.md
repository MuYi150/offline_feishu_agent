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
