# 验证报告

验证日期：2026-07-26；环境：Windows，Conda `feishu-api`，Python 3.11.15。

## 已执行基线

```powershell
conda run --no-capture-output -n feishu-api python -m unittest discover -s tests -v
```

在 v1 `wiki/` 中执行：136 tests，全部通过。v2 实施未修改 v1 文件。

## v2 验证

```powershell
conda run --no-capture-output -n feishu-api python -m pytest -q
```

结果：`43 passed, 2 skipped`，耗时 6.81 秒。两个 skipped 均为下述真实 Kimi 案例。验证范围包括纯规则、全部 Fixture 的 v1 source 基础字段和文件名兼容、原始飞书 Blocks/表格提取、PDF/Prompt/原子写入、五种业务结果、首轮相似性、复审、完整/部分多模态、长 PDF 分批、PDF 缺失/损坏/页数与大小超限、正文为空/超限、非法/空 JSON、API 失败、429/5xx 重试、认证预检、历史损坏隔离、敏感信息扫描、输出冲突和 checkpoint 恢复。

另外执行了 CLI Fake 验证：

- `basic_pass`：退出码 0，结果 `pass`，生成全部产物。
- `long_pdf`：退出码 0，结果 `pass`，执行 2 个视觉批次和 1 个最终审稿调用。
- `invalid_model_output`：退出码 1，错误分类 `model_non_json`，没有生成成功状态或通知。
- `basic_pass --real-model`（无 Key）：退出码 1，错误分类 `model_authentication_error`，生成安全失败 trace，不产生通知。
- `pip check`：`No broken requirements found.`
- 输出敏感信息扫描：未发现 `;base64,`、Authorization 或 Bearer 内容。

## 真实 Kimi

当前 `KIMI_API_KEY` 和 `MOONSHOT_API_KEY` 均未设置。两个 `real_kimi` 案例因缺少 Key 被显式跳过，未使用 Fake 冒充真实结果。这是唯一外部验证阻塞项。
