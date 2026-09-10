# V2 飞书同步与审稿

V2 独立提供普通同步、快照采集、V2 多模态审稿、表格回写、机器人通知和公示索引。运行时不导入 V1，不读取 V1 配置；`migrate` 只读取明确指定的历史目录。原本地 `wiki_review_v2.cli`、Fake 案例和本地测试索引继续保留。

## 配置与运行

在既有 `feishu-api` 环境中安装本项目：

```powershell
conda run --no-capture-output -n feishu-api python -m pip install -e ".[dev]"
Copy-Item online.example.json online.json
```

填写 `online.json` 中的飞书配置。密钥建议放在当前进程环境中：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`KIMI_API_KEY`（或 `MOONSHOT_API_KEY`）。配置文件不会自动加载 `.env`；同名配置以环境变量优先。飞书变量还包括 `FEISHU_DOMAIN`、`REVIEW_WIKI_NODE_TOKEN`、`CONFIG_WIKI_NODE_TOKEN`、`REVIEW_SHEET_NAME`、`CONFIG_SHEET_NAME`、`ADMIN_OPEN_ID`、`ENABLE_ADMIN_NOTIFY`、`FEISHU_HTTP_TIMEOUT`、`FEISHU_HTTP_MAX_RETRIES`、`PDF_EXPORT_TIMEOUT_SECONDS`、`MAX_AI_REVIEW_ROUNDS`。`WIKI_V2_ONLINE_ROOT` 覆盖生产数据目录，默认为项目下 `online_state`。相对目录相对于显式配置文件。

飞书应用需要相应知识库、文档 Blocks、文档导出、电子表格读写、联系人和机器人私信权限；嵌入多维表格还需要其读取权限。应用必须能访问配置表、投稿表和待审核目录。`--write` 开启线上修改，`--real-model` 单独授权模型。没有两者时不会投递真实模型审稿结果。

下列命令在 V2 目录运行，也可用 `scripts/online.ps1 <参数>` 或 `bash scripts/online.sh <参数>`。脚本统一使用已有 `feishu-api`，不创建环境。

| 操作 | 命令示例 | 行为 |
|---|---|---|
| 普通同步预览 | `python -m wiki_review_v2.online sync --config online.json` | 只读飞书，输出增改、状态和排序诊断 |
| 指定稿件采集 | `python -m wiki_review_v2.online sync --config online.json --extract-only --node-token NODE` | 只生成本地 Fixture，不登记生产任务、不写表或发消息 |
| 普通同步执行 | `python -m wiki_review_v2.online sync --config online.json --write` | 登记、更新状态、生成 AI 队列并处理已有交付 |
| 模型预演 | `python -m wiki_review_v2.online review --config online.json --real-model --node-token NODE` | 在独立 previews 目录审稿，不回写、发消息或修改生产历史 |
| 审一篇并交付 | `python -m wiki_review_v2.online review --config online.json --write --real-model --node-token NODE` | 消费已有快照；默认最多一篇 |
| 一键流程 | `python -m wiki_review_v2.online run --config online.json --write --real-model --limit 1` | 同步 → 公示索引 → 审稿 → 交付重试 |
| 只重试交付 | `python -m wiki_review_v2.online retry --config online.json --write` | 复用已有结果，不调用模型 |
| 公示索引初始化 | `python -m wiki_review_v2.online index --config online.json --write --real-model --full` | 扫描全部公示文章；已有有效概述跳过 |
| 公示索引增量 | `python -m wiki_review_v2.online index --config online.json --write --real-model --limit 10` | 最多处理十篇需更新的公示文章 |

`--node-token` 在 sync 中仅限定 Fixture 采集对象，普通同步仍维护整张表；在 review/retry 中限定审稿或该文档交付。`--limit` 限制正式审稿篇数；一键 run 的索引更新独立于此限制。run 不带 `--real-model` 时仍能同步、复用公示概述和重试交付，但跳过新审稿。`--extract-only` 不能与 `--write` 或 `--real-model` 合用。

普通 dry-run 不创建本地任务。真实模型预演会在 `online_state/previews/<id>` 下复制所需历史与公示索引，并写入独立产物。预演不自动刷新生产索引；正式 review/run 会先核实生产公示资格。

## 表格与状态

保留原投稿表 A:N 表头：序号、文档名称、文档链接、文档所属知识库、投稿人、审稿方式、审稿人、当前文档状态、审稿轮次、文档更新时间、上次AI审稿时间、公示时间、node_token、obj_token。配置表 A:D 为知识库名称、是否启用同步、待审核目录链接、待审核目录 node_token。

启用目录只扫描直接子节点并分页读取。匹配依次使用节点 token、对象 token、链接及标题/作者/知识库兜底；多行冲突停止对应处理。更新按单元格进行，不删除记录，不新增列。链接保留对象形式，投稿人使用可读的 `@姓名`，open_id 保存在本地快照中。新增行修复 F/H 下拉；`repair-format` 可单独修复既有行。排序只输出 J 升序诊断，不重排富文本行。

人工和人工+AI 沿用人工分配状态；只有方式为 AI/人工+AI 且状态为 AI审稿中/已修改待复审的稿件可入队。公示时间非空或“已公示”优先于其他状态；待审核目录缺失检测只针对本次成功扫描的知识库。同步会保留本地待交付和人工接管标记。

Fixture 中 `review_round=0` 表示初审；大于零时必须存在相同 document_id、周期及轮次的已提交历史。复审只加载上一轮 blocking/major，不召回相似文章，但会为修改后的版本生成检索概述。旧 `previous_issues` 不替代正式历史。

| V2 结果 | H 当前文档状态 |
|---|---|
| pass | AI通过待确认 |
| need_revision | 需修改 |
| reject | 已拒稿 |
| recommend_human_review / incomplete_review | 待分配人工审稿 |
| 第三轮仍 need_revision | 待分配人工审稿，并设置本地人工接管 |

正式交付只回写 H/I/K。第三轮完整意见保留，自动审稿停止。拒稿后文档修改且仍采用 AI 方式时，普通同步重置表格轮次为零并开启新的本地周期。模型、API、JSON 和校验失败只记录技术诊断，不产生业务结论、不递增轮次。

## 快照和交付恢复

生产数据全部位于独立 `online_state`：

- `snapshots/<obj_token>-<版本摘要>/`：source_document.json、document_blocks.json、attachment_metadata.json、原始 source.pdf、fixture_options.json、raw_blocks.json、snapshot_manifest.json。
- `capture_errors/`：未成功采集的原始响应和错误，绝不作为待审快照。
- `outputs/<快照>/review/`：原 V2 审稿产物和 SQLite checkpoint。
- `online.sqlite`：任务、已提交历史、周期、人工接管、各段通知、公示资格和迁移审计。
  公示索引使用其中的 `similarity_articles` 表，沿用本地旧索引的列名。用 VS Code SQLite Viewer 打开数据库并选择该表，即可直接查看 `title`、`content`、`topics_json` 等列；`content` 是供相似检索使用的文章概述，不是原文全文。`eligible` 和 `publication_confirmed` 独立记录公示资格。
  旧版线上 `records.payload` 中的公示索引在下一次索引写入时自动迁移，原始 JSON 保留为 `publication_archive` 审计记录；只读操作兼容旧结构但不迁移。任务、历史及消息记录仍在 `records` 表中，原本地测试索引不受影响。
- `run.lock`：操作系统运行锁；进程退出后自动释放，run 内部子作业共用一把锁。

真实数字类型 Blocks、子节点、嵌套表格、代码、列表、图片和附件元数据会适配成本地内容。嵌入电子表格/多维表格在采集时读取；审稿节点不访问飞书。附件正文不打开，不走 OCR。原始 PDF 和关键结构读取失败会阻止发布。采集前后核对文档对象与更新时间，临时目录验证完成后才发布，文件摘要用于后续完整性检查。

同一文档版本/周期/轮次只创建一个任务。审稿完成先保存结果，然后按 token 重新读取表格定位行，并核对当前目录成员、文档版本、审稿方式、状态和轮次。文档已改动、移出、公示或被管理员切换流程时，旧结果标记 obsolete，不覆盖当前状态、不发送旧修改意见。飞书未提供本地数据库与表格之间的事务；最后核对与远端写入之间仍存在短暂并发窗口，因此正式切换前必须停止 V1 同表定时写入。

表格提交意图先落 SQLite；重试能识别已成功写入的 H/I/K。确认表格后才提交历史、生成投稿人及管理员消息。每段消息有独立状态和稳定 uuid。失败只重试失败步骤；发送超时、缺少回执或发送中进程中断标记 uncertain，不能自动重发。代码不承诺跨系统严格恰好一次；也不能保证在模型响应已返回但 checkpoint 尚未落盘的极短窗口中绝不重复模型请求。

## 管理与迁移

```powershell
python -m wiki_review_v2.online diagnose --config online.json --node-token NODE
python -m wiki_review_v2.online read-document --config online.json --node-token NODE
python -m wiki_review_v2.online repair-format --config online.json --node-token NODE --write
python -m wiki_review_v2.online test-message --config online.json --recipient ou_SELECTED_USER --write
python -m wiki_review_v2.online deliveries --config online.json
python -m wiki_review_v2.online resolve-delivery --config online.json --delivery-id ID --resolution sent --write
```

`resolve-delivery` 的 sent 表示已在线确认送达；retry 表示已人工核实允许重发；cancelled 表示终止投递。必须先核实 uncertain 的真实发送情况。test-message 只发送本次测试消息，不顺便重试其他生产消息。所有不带 `--write` 的管理命令只预览。`deliveries` 保留失败和已结束的投递审计，不物理删除。

迁移示例：

```powershell
python -m wiki_review_v2.online migrate --config online.json --legacy-root D:\NEU\feishu\wiki
python -m wiki_review_v2.online migrate --config online.json --legacy-root D:\NEU\feishu\wiki --write
```

迁移读取 `data/ai_review_history.json`、`notify_state.json`、`ai_review_delivery_failures.json`；V1 原文件保持不变。只迁移实际保存的最近轮次与问题，为无 ID 问题生成稳定 ID，不补造更早轮次、结论或 V2 视觉证据。根据节点、当前对象、轮次和审稿时间核对历史。有效通知状态映射到去重记录；原始记录整体归档。

缺少历史、身份不匹配或旧投递无法确认的稿件进入迁移报告和管理员处理队列。核实并修复后可执行 `release --node-token NODE --write` 解除迁移暂停；此命令不会解除第三轮人工接管，也不会绕过缺失历史检查。V2 原 `local_state` 历史及测试索引不会自动导入。

生产相似索引只召回经当前飞书数据确认已公示的版本。AI 通过仅保存概述，不自动进入生产候选。公示后同版本可复用已提交审稿概述；历史公示文章或改版文章通过独立文字概述任务建索引，不重新审稿、不发送审稿通知。索引保存 `publication_confirmed` 和 `eligible`，不会将人工公示伪装为模型 pass。公示资格撤回、版本变化或读取无法确认时暂停召回。

## 验收与切换

本地测试：`conda run --no-capture-output -n feishu-api python -m pytest -q`。模拟客户端覆盖普通同步、快照、V2 Fake 审稿、回写与消息故障恢复、公示索引及迁移，测试目录与生产完全隔离。

线上按只读 sync → 指定文档 extract-only → real-model 预演 → 明确选定记录的 review --write --real-model 顺序验证。检查新稿通知、改后复审、人工接管与公示索引后再启用定时作业。切换前停止 V1 对同表的定时写入，保留 V1 目录与配置用于回退；备份 V2 online_state。不要让两版本同时维护同一张表。
