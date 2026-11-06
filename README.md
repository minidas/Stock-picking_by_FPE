# 六年盈利概率选股工具：人类说明

本项目面向**个股**：机构一致预期为主生成盈利模型，程序初筛后由Agent分析候选公司的事件，再输出每只股票的结果。后续研究和实际使用不再运行指数代理分析。标普500仅用于确定个股名单，不作为一只股票建模。

交付文件包含名单内所有非排除个股，**不局限于买入候选**：观察、卖出复核和仅历史数据回退的股票均保留；高风险或亏损股票另列排除原因。缺少数据的成员保留占位记录，不能悄悄从覆盖数量中消失。完整输出契约见第五节。

**另一台Windows使用：[克隆与运行指南](docs/DEVICE_SETUP.md)**。把代码和研究数据提交到GitHub，新机克隆后创建Python环境、安装依赖即可测试和运行；旧输出可以重建，无需搬运旧虚拟环境或运行归档。

已扩展到2012年末**500只个股的数据采集表**，历史名单与500条基金持仓估值逐股对齐；逐股采集结果见 `outputs/stocks_2012_full/download_status.csv`，完整证据见 `all_stocks.json`。下载到的财报数字仍需核对发行人、拆股和EPS口径；当前单独核对过、可运行模型的仍是[原四只真实样本](reports/stocks_2012_diagnostic.md)。四只 `DEMO_*` 全部是合成演示。**500行采集记录不等于500份已验证预测；模型概率仍需用个股历史六年误差校准。**

## 1. 两个概率的含义

`P0`是今日价格，`E6`是六年后截至目标日的年度/TTM稀释每股收益。预测对象是 `P0/E6`，不是六年后的交易市盈率 `P6/E6`，也不是六年累计盈利。

| 输出 | 定义 |
| --- | --- |
| `p_below_6` | `Pr(E6 > P0/6)`，未来盈利足够高的概率 |
| `p_above_6_5_positive` | `Pr(0 < E6 < P0/6.5)`，正盈利且该比率高于6.5的事件概率 |
| `p_loss` | `Pr(E6 <= 0)`，零盈利/亏损风险 |
| `p_between_6_6_5` | 正盈利且比率处于6至6.5之间 |
| `p_risk_or_above_6_5` | 高于6.5或亏损的合计概率，不冒充第二项 |

前四项总和为1。第二项没有除以正盈利概率；仅名为 `positive_conditional_*` 的EPS均值/区间条件于正盈利。负EPS不能因负市盈率小于6被当作便宜。当前EPS或有效机构预测非正时，本版本返回 `abstain`，留给专门的扭亏模型，不捏造概率。

这些是**依赖假设、尚未校准的模型预测概率**，不是经过验证的“置信率”。`P0/E[E6]`、`E[P0/E6]`和 `Pr(P0/E6<6)`并不相同。当前PE为20时，在恒定增长假设下，该比率六年后小于6需要约22.2%的年化EPS增长；固定6/6.5阈值不一定适合不同利率、行业和经济周期。

## 2. 数据来源与实际接入程度

| 数据 | 来源与支持 | 限制 |
| --- | --- | --- |
| 机构一致预期 | [Alpha Vantage官方文档](https://www.alphavantage.co/documentation/)的 `EARNINGS_ESTIMATES`；提供带自有key的原始下载器 | 年度/季度EPS、分析师数等以账户响应为准；本次没有真实key，未下载个股共识，未猜测供应商字段 |
| 历史机构快照 | [FactSet Estimates Point-in-Time](https://www.factset.com/marketplace/catalog/product/factset-estimates-pointintime)等获许可的历史快照 | 需另行提供授权数据；今天的预测不可改日期冒充历史预测 |
| 历史已公布盈利 | [SEC Company Facts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)和公司财报；提供原始下载器 | 必须处理申报时间、重述、财政年度和拆股；当前API整包不自动等于无偏历史面板 |
| 行情与公司行动 | 授权的价格、拆股、股息、退市数据，归一化后导入 | 价格/EPS需同币种和股份口径；股息复权价格不能直接当作估值分子 |
| 成分证券 | 有来源和生效日期的成员CSV/JSON | [S&P说明](https://www.spglobal.com/spdji/en/research-insights/index-literacy/the-sp-500-and-the-dow/)指出成员可随公司事件变化；不能用今日成员回测历史；多股类使证券数未必恰好500 |
| 2012年末逐股采集 | `data/universe_2012_members.json`、`data/research_2012_prices.json`、下载到的SEC历史数据及原始文件 | 500只历史名单与基金持仓对应；基金估值不冒充独立核验的交易所收盘价，财报数据不冒充历史共识 |
| 已核对模型输入 | `data/research_2012_snapshots.json`内的公司年报、历史价格与逐项来源 | 目前仍四只，使用已公布年度EPS，缺少当时机构共识，不能充当全市场检验 |

项目采用供应商中立JSON，并提供显式字段映射函数；**未声称完成所有供应商的一键清洗和全市场实时抓取**。GAAP与调整后EPS、年度与季度、股份单位和拆股基准必须先统一。来源字符串一致不能证明经济口径正确。[Agent说明](docs/AGENT_GUIDE.md)列出输入契约和处理流程。

## 3. 算法概要

实现位于 `src/stocking/model.py`。默认参数集中于 `DEFAULT_CONFIG`，可通过JSON覆盖，都是研究起点，尚未校准。

1. 从决策日前公开的最近最多11个年度EPS中，计算相邻正盈利年份的年化对数增长，截断至±1。增长中位数按 `n/(n+6)` 向长期对数增长4%收缩。亏损保留给风险分支，不制造对数增长。
2. 每个机构预测作为EPS水平锚点，机构权重 `clip(N/(N+2),0.70,0.95)`，在对数水平上与历史路径融合；锚点之间插值。最后锚点之后的增长以2年尺度回归长期4%对数增长。这是几何收缩后的**正盈利情景条件均值锚点**，不是无条件EPS均值，也不是机构正确率。
3. 历史年化对数增长标准差设0.20下限、0.80上限；机构分歧可进一步提高波动，绝不除以分析师人数平方根。年度误差用 `0.35^|i-j|` 相关，再加入0.08的持续增长误差标准差。[预测不确定性研究](https://www.sciencedirect.com/science/article/pii/S0165410111000899)说明分析师分歧没有包含所有共同预测误差。
4. 正盈利分支为相关正态对数增长：`x~N(m,C)`，区间长度为 `d`，则 `log(E6)=log(E0)+dᵀx`，方差 `dᵀCd`。设第i期目标EPS均值为 `M_i`、累计对数方差为 `V_i`，使用 `m_i=(Δlog M_i−0.5ΔV_i)/d_i`，保留跨年相关项。最终EPS为对数正态，通过正态CDF直接计算概率，不需要蒙特卡洛。
5. 年化普通增长期望 `exp(m_i+C_ii/2)-1`；方差 `(exp(C_ii)-1)*exp(2*m_i+C_ii)`。同时导出对数参数和完整协方差矩阵。最近财报期末通常早于今日，第一段包含桥接期，字段 `interval_years` 显式说明；六年终点仍严格为决策日加六个日历年。年度增长统计条件于正盈利情景。
6. 终点非正盈利概率暂用历史亏损频率向6%先验收缩，先验权重10个样本。这是未经校准的**终点状态先验**，不是六年内曾亏损或破产的概率。不建模亏损金额，因此不输出混合状态的无条件EPS期望。

模型初筛默认：便宜概率≥70%且亏损概率≤10%进入买入研究；高估概率≥80%或亏损概率≥25%进入卖出研究，其余观察。卖出队列是复核持仓/回避新买入，不表示做空。缺少机构预测仍计算历史回退概率，但默认不进入AI队列。文件导出另执行高风险/亏损排除，被排除股票不进入主表或AI队列。高估本身不是高风险排除理由；观察和历史回退股票仍在主表。REIT、强周期、金融公司和重大并购稀释需要专门审阅。

估值阈值也可配置为 `buy_fpe_threshold`、`sell_fpe_threshold`（默认6和6.5，必须前者小于后者）。候选判断采用 `p_below_buy_threshold` 和 `p_above_sell_threshold_positive`；旧的 `p_below_6` 等字段始终保留固定6/6.5定义，不能误读为新阈值概率。`examples/thresholds_research.json`中的8.5/10.5仅是研究假设，未验证为更好策略。后续仅用个股数据检验这些假设。

[长期增长研究](https://www.nber.org/papers/w8282)记录了机构长期增长预期的乐观偏差和有限预测力；因此本工具不将共识视为真值，也不机械外推短期高增长六年。

## 4. AI调整与审计

Agent只分析未被风险规则排除的初筛候选，检索决策日前180日内的公司公告、财报指引、监管披露等重要事件。判断是否已经包含在共识；已包含者只能零调整。未包含的影响须解释营收、利润率、税费或股数如何改变EPS，再提供六个年度增长增量、方差倍数、亏损概率增量和证据权重。

程序检查日期、来源URL、重复ID及幅度。单事件年度对数增长增量≤±0.10，加权合计每年≤±0.20；方差只能增加、合计最多3倍。亏损增量也受限。方差增加不会机械抬高期望EPS；增长增量仅作用于未来六年，方差倍数调整整体预测不确定性。`confidence`只是收缩权重，不是校准过的AI可靠度。程序不能验证新闻真假或推理质量。

测试阶段保留原始输入副本、hash、基准模型、事件记录和调整前后概率；实际使用采用第五节的保留策略。旧基准hash会被拒绝。没有可靠新信息就提交空列表，不能强行制造观点。工具不自动下单。

## 5. 输出文件、测试留存与实际使用

```powershell
# 在项目根目录运行；新设备先按迁移手册创建.venv。
if (Test-Path '.venv\Scripts\python.exe') {
  $py = (Resolve-Path '.venv\Scripts\python.exe').Path
} elseif (Test-Path 'C:\VirtueEnvironment\Qicomput\Scripts\python.exe') {
  $py = 'C:\VirtueEnvironment\Qicomput\Scripts\python.exe'
} else {
  throw '请先按docs/DEVICE_SETUP.md配置Python环境'
}
# 缺少依赖时安装。
& $py -m pip install -r requirements.txt
New-Item -ItemType Directory -Force outputs | Out-Null
& $py scripts/stocking.py screen --input examples/snapshots.json --universe examples/memberships.csv --mode research --output outputs/demo
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $py -m unittest discover -s tests -v
```

每次 `screen` 或 `adjust` 都提供：

| 文件 | 用途 |
| --- | --- |
| `all_stocks.json` | 非排除股票的完整输入、历史/机构预测、模型、六年年度增长均值与方差、协方差及概率；包含观察和数据不足记录 |
| `all_stocks.csv` | 便于查看的逐股表，展开年度增长统计、价格、EPS、各概率和处理状态；完整嵌套历史以JSON为准 |
| `excluded_stocks.json` / `.csv` | 排除名单和理由，不把排除股票从审计记录中抹去 |
| `baseline.json` / `adjusted.json` | 调整前后模型及审计记录 |
| `agent_queue.json` | `screen`生成的初筛事件研究队列，不能代替完整个股文件 |
| `forecast_audit.csv` | 精简的全量预测审计，包含排除和缺失记录，不携带完整历史/共识原文 |
| `run_lifecycle.json` | 本轮归档路径、模式、原始资料留存及清理结果 |

主表默认排除当前EPS≤0、有效机构预测EPS≤0、模型终点亏损概率≥25%，或输入明确标记 `risk_status: "high"` 的股票。25%只是可调整的工程过滤标准，用 `--exclude-loss-probability 0.25` 配置；`adjust`默认继承基准的排除门槛，并不表示风险概率已校准。仅仅没有买入信号、估值偏高、缺机构共识，都不是排除理由。名单内缺失数据以 `data_unavailable` 占位，概率留空；没有风险数据不能被标成低风险。

默认 `--mode research`，每轮在 `outputs/run_archive/` 中建立独立归档，保留输入快照、成员表、配置、事件文件和指定原始资料的字节副本及hash。**归一化JSON不是供应商原始响应**；需通过可重复的 `--raw-input <文件路径>` 显式加入原始响应、财报等，缺少原始资料时不能称为完整可复现实验。测试模式不会自动清理。

现有四股样本的原始资料由 `scripts/archive_2012_sources.py` 保存到 `data/raw/stocks_2012/`，逐条清单记录来源、下载时间、SHA256、备用来源和失败状态。它验证并复用已有缓存，只重试未解决的来源；成功下载不代表获得了历史机构共识。运行筛选时应把清单和实际成功保存的原文逐个传入 `--raw-input`。

全名单采集使用 `scripts/download_2012_universe.py`、`scripts/download_2012_prices.py`、`scripts/download_2012_sec_bulk.py`，再运行 `scripts/extract_legacy_sec_eps.py` 与 `scripts/build_2012_sample.py` 整合。输出目录为 `outputs/stocks_2012_full/`，`coverage_summary.json`给出实际成功数量及缺失名单，`download_status.csv`逐股区分价格、年度EPS、较早历史与已核对模型输入。研究原文保存在 `data/raw/`，合并运行也建立研究归档。SEC官方接口本次返回403；通过公开镜像获得的行明确标记为镜像数据，不声称与官方ZIP字节相同。远程查询较慢，已保存的成功响应可离线复用。

SEC研究下载器使用的 `duckdb==2.0.0`已包含在 `requirements.txt`，无需另装到vendor目录。它采用单查询、512MB数据库内存上限、2线程、按需读取缓存，并逐批保存EPS；这些限制不包括Python自身和其他应用的内存。`--help`列出离线及只归一化模式。查询尚未结束时，`eps_retrieval_complete`为false；不要把部分结果的缺失项解释成公司没有财报。

名单按2012年12月31日交易收盘时点定义，包含FII；收盘后生效的ABBV替换另外记为边界事件。社区历史表已纠正遗漏、更名与部分错误CIK，并与当年IVV持仓交叉核对；这仍是事后重建，未取得S&P授权历史名单认证。基金报告2013年发布，仅用于重建2012年价格事实，不用于给2012年的盈利输入增加未来信息。

实际使用时显式选择 `--mode live`。成功运行后，按 `--retention-days`（默认7天）清理已完成、较早实用运行中登记的原始/临时副本，保留最近成功运行、精简预测结果、清单、hash和清理记录。研究运行及未完成运行不清理；使用者提供的原文件不删除，也不遍历删除项目文档。仅登记在受管归档中的文件适用该策略，`data/raw/` 原始来源和旧 `reports/` 不会因此自动被删。

```powershell
# 实际数据准备好后使用；下列路径需要替换成真实文件。
& $py scripts/stocking.py screen --input data/private/snapshots.json --universe data/private/memberships.csv --raw-input data/private/provider_response.json --mode live --retention-days 7 --output outputs/latest
# 查看到期清理计划；加 --apply 才执行独立清理命令。
& $py scripts/stocking.py cleanup --archive-root outputs/run_archive --retention-days 7
& $py scripts/stocking.py cleanup --archive-root outputs/run_archive --retention-days 7 --apply
```

实际使用建议固定 `--output outputs/latest` 更新主表，避免为每天或每只股票另建临时文档。归档中的完整输入/模型副本可按上述策略到期清理，精简的 `forecast_audit.csv` 和运行元数据保留用于后续核对；这不等于永久保存全部原始资料。两份说明文档和源代码长期保留。

当前测试尚未完成完整策略验证，因此已有研究原始资料保留。真实数据放 `data/private/`，密钥从环境变量提供；`outputs/` 和 `vendor/` 默认不纳入版本管理，必要时另行备份研究归档。按[Agent说明](docs/AGENT_GUIDE.md)填写事件及hash，再用 `adjust` 重新生成完整个股文件。

## 6. 后续只用个股验证

`score-history` 提供历史个股快照、历史成员和六年实际EPS的回放评分接口，报告Brier、对数损失、分桶及缺失成员。它强制使用研究留存，归档预测输入、结果标签、成员表与指定原始资料；支持 `--archive-root` 和重复的 `--raw-input`。它不是个股组合收益回测。完整策略仍需历史共识、退市/并购/分红数据、可比EPS口径，分别比较机构原始预测、程序、程序加AI。训练与验证按时间隔离六年标签重叠，并按公司/年份考虑依赖；同月500只股票不是500个独立宏观样本。

当前以 `data/research_2012_snapshots.json` 中的四只股票检查模型流程，以500行采集表跟踪其余样本的输入质量。新下载的EPS没有通过发行人和股份口径核对前，不自动生成概率。未核对的负EPS标为待复核，不能据此直接宣称该股票在2012年亏损；已确认的高风险/亏损股票才适用模型排除规则。不能把四只样本的概率、买入数量或表现解释为全成分结果。历史测试始终保留排除清单与原始资料，避免只保留成功公司的幸存者偏差。

早期指数实验脚本 `scripts/validate_history.py`、`scripts/analyze_thresholds.py` 及其报告、原始文件仅作为历史研究记录保留，已退出当前工作流程。以后不再自动运行，也不再用其结论评价个股策略。

当代AI可能知道历史结果，给它旧新闻不能消除知识泄漏。AI增益最好通过从现在开始冻结记录的前瞻纸面测试评估。即使准确预测EPS，也不能保证六年间价格回撤、终点估值或投资收益。当前合理用途是产生逐股研究数据、量化假设影响，并保存未来可以检验的预测。
