# 2026-11-08存储清理记录

用户明确要求删除仅用于2012测试的数据，以及研究当前股市不会再调用的数据。本次实际删除 **4,107个文件，6,517,844,967字节（约6.07 GiB / 6.52 GB）**；不是预览、移动或另存压缩副本。数字按被删除文件的逻辑字节统计，不包含Git历史对象回收或OneDrive云端配额变化。

## 删除范围

| 类别 | 文件数 | 字节数 |
|---|---:|---:|
| `data/`、`reports/`中的2012研究及旧Shiller指数实验 | 93 | 102,229,909 |
| 2012模型输出与对应历史运行归档 | 271 | 413,549,313 |
| 被替代的2026运行中重复的raw/working归档副本 | 3,635 | 4,664,221,715 |
| 已被新结果替代的模型、主表、排除表和队列 | 67 | 928,688,063 |
| 被替代的归一化及首轮验证派生数据 | 38 | 353,190,106 |
| 旧v5输入快照及早期readiness大表 | 3 | 55,965,861 |

删除前逐文件核对绝对路径、工作区边界、类型、大小和SHA256；拒绝重解析点与保留依赖。使用PowerShell `Remove-Item -LiteralPath` 逐个删除清单中的文件，没有递归删除计算出的目录。首次直接调用`.ps1`被本机执行策略拒绝，未更改系统策略；随后在当前PowerShell会话执行已审阅命令。一次路径祖先检查错误在删除前终止，修正后全清单预检通过，删除日志无失败项。

## 当前研究保留范围

- [最新Forward PE<6.5报告](../outputs/current_sp500_2026-11-06/fpe65_ranking_2026-11-08/report.md)及其全部CSV/JSON、基准、事件、日志、原始归档和交付清单均保留。当前288份模型输入、205只机构排名、11只历史排名、72只排除和215只缺失的结果未变。
- `outputs/current_sp500_2026-11-06/evidence/`完整保留，包含暂未进入模型的股票的SEC、Yahoo及价格原始来源；不把“当前缺失”当作“未来不再需要”。
- 当前成员表、`normalized/prices.json`、`normalized_latest_available_reviewed/`、`eps_reconciliation/`、`network_retry_2026-11-07/`保留。`normalized/prices.json`仍被实际数据装配入口调用。
- `reusable_reconciliation_audit/validation_run_v2/`的`actuals/`、`consensus/`、`screen/`、`comparison/`保留。`validation_run/price_review.json`、通用季度补录和财历配置及其原文证据保留。
- `consensus_rerank_v5/price_review.json`、`ttm_refresh/`、`evidence/`、`reconciliation/`保留；9只新TTM未核验成功股票的刷新原文仍可继续研究。
- NVDA案例中当前补录依赖的`component_validation.json`、`reviewed_inputs.json`及`evidence/`保留。旧案例模型和重复归档已清理。
- 小型2026报告、配置、来源清单和审计日志保留作为研究记录。某些旧报告所链接的大型派生文件已删除，应以本次清理日志判断可用性。
- `.git/`、`.venv/`、模型源码和当前入口未删除，未重写Git历史、未自动提交。历史数据的Git跟踪文件删除仍在工作区变更中。

## 清理后的使用方式

当前研究读取：

```text
outputs/current_sp500_2026-11-06/reusable_reconciliation_audit/validation_run_v2/actuals/snapshots.json
outputs/current_sp500_2026-11-06/reusable_reconciliation_audit/validation_run_v2/consensus/snapshots.json
outputs/current_sp500_2026-11-06/input/memberships.csv
```

新研究使用通用`stocking.py screen`，显式选择已核验输入并写入新目录。更新日期时仍须先准备真实可用的TTM、历史、价格和共识；保留的快照仍是2026-11-07已观察批次，不能仅改日期作为新数据。

旧2012下载/复现命令与v3/v4/strict等旧固定包装入口需要已删除资料，不能继续声称可在本地完整复现。不要为了运行旧命令自动重新下载本次清理的内容。最新交付自身的完整归档仍保留，旧归档清单的`complete`描述当时完成状态，不表示清理后所有旧副本仍存在；删除情况另记于本次日志，未篡改旧清单。

## 验证与审计

- 删除后4,285个保留文件完成SHA256核对，最新交付未变；之后仅更新README和Agent指南的清理提示，旧文档字节仍保存在最新运行的原始归档中，版本变化另记文档修订清单。
- 288份当前模型及概率用原配置、原风险政策重新计算，全部与最新基准完全一致。503只证券的结果记录仍完整。
- 测试总计273项：**262项通过，11项明确跳过**。跳过的是已移除外部历史资料的5项2012价格集成测试和6项2012历史装配测试；不是全部273项通过。
- 两处通用测试已改用最小合成数据，不依赖真实2012样本；保留历史集成测试源码并明确检查资料是否安装。

审计文件位于[本次清理目录](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/)：

- [删除计划及原文件hash](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/deletion_plan.json)
- [逐文件删除结果](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/deletion_results.jsonl)
- [删除汇总](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/deletion_summary.json)
- [保留依赖清单](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/protected_files.json)
- [删除后核验](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/verification.json)
- [文档修订及旧文档归档](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/documentation_revisions.json)
- [测试日志](../outputs/current_sp500_2026-11-06/maintenance/cleanup_2026-11-08/test_results.log)
