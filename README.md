# 六年盈利概率选股工具：人类说明

本项目面向**个股**：机构一致预期为主生成盈利模型，程序初筛后由Agent分析候选公司的事件，再输出每只股票的结果。后续研究和实际使用不再运行指数代理分析。标普500仅用于确定个股名单，不作为一只股票建模。

交付文件包含名单内所有非排除个股，**不局限于买入候选**：观察、卖出复核和仅历史数据回退的股票均保留；高风险或亏损股票另列排除原因。缺少数据的成员保留占位记录，不能悄悄从覆盖数量中消失。完整输出契约见第五节。

**另一台Windows使用：[克隆与运行指南](docs/DEVICE_SETUP.md)**。把代码和研究数据提交到GitHub，新机克隆后创建Python环境、安装依赖即可测试和运行；旧输出可以重建，无需搬运旧虚拟环境或运行归档。

已扩展到2012年末**500只个股的数据采集表**，历史名单与500条基金持仓估值逐股对齐；逐股采集结果见 `outputs/stocks_2012_full/download_status.csv`。另按用户要求增加全名单历史回退概率研究入口 `scripts/analyze_2012_universe.py`：允许明确标注未经完整核验的股份口径假设，使用已有财报数字做敏感性测试，仅按高亏损概率作风险排除。独立核对的[原四只真实样本](reports/stocks_2012_diagnostic.md)仍单独标记，不把其余研究情景升级为已验证输入。四只 `DEMO_*` 全部是合成演示。**500行覆盖不等于500份已验证预测；模型概率仍需用个股历史六年误差校准。**

**当前盈利基准使用TTM，年度历史已支持跨申报补齐。** 默认读取 `data/research_2012_ttm_anchors.json` 与 `data/research_2012_history_panels.json`，输出到 `outputs/stocks_2012_ttm_history_expanded/`。本轮为245家公司新增496个年度观察，多数从3年扩至5年；CLF补至2001—2011年共11年。缺口和冲突保留，不插值或填零；来源、拆股桥接和重叠核对见[补样本报告](reports/annual_history_expansion.md)。当前TTM仍仅补齐CLF四个实际季度，合计6.33美元，其余缺TTM的成员保留数据缺失，不用旧年报替代。现金流、融资等变量未加入。旧年度实验用 `--eps-anchor annual --history-mode original` 显式复现。输入与公式见[模型逐步拆解](docs/MODEL_WALKTHROUGH.md)。

## 1. 两个概率的含义

`P0`是今日价格，`E6`是六年后截至目标日的完整12个月稀释每股盈利；当前研究的起点使用最新已公布TTM。预测对象是 `P0/E6`，不是六年后的交易市盈率 `P6/E6`，也不是六年累计盈利。TTM四季稀释EPS求和是明确的研究口径，因各季加权股数和稀释规则不同，不冒充重新计算的全年GAAP稀释EPS。

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

TTM装配在 `src/stocking/ttm.py`：核对四季连续、完整一年、可用日期、来源和会计/股份口径；只更新 `eps0`、TTM期间和审计信息。`history`不能加入重叠TTM来增加样本量。显式TTM期末时效上限200天，旧年度输入保持550天的兼容上限；未通过核验的数据不能靠修改标签变成TTM。

1. 从决策日前公开的最近最多11个年度EPS中，计算相邻正盈利年份的年化对数增长，截断至±1。增长中位数按 `n/(n+6)` 向长期对数增长4%收缩。亏损保留给风险分支，不制造对数增长。
2. 每个机构预测作为EPS水平锚点，机构权重 `clip(N/(N+2),0.70,0.95)`，在对数水平上与历史路径融合；锚点之间插值。最后锚点之后的增长以2年尺度回归长期4%对数增长。这是几何收缩后的**正盈利情景条件均值锚点**，不是无条件EPS均值，也不是机构正确率。
3. 历史年化对数增长标准差设0.20下限、0.80上限；机构分歧可进一步提高波动，绝不除以分析师人数平方根。年度误差用 `0.35^|i-j|` 相关，再加入0.08的持续增长误差标准差。[预测不确定性研究](https://www.sciencedirect.com/science/article/pii/S0165410111000899)说明分析师分歧没有包含所有共同预测误差。
4. 正盈利分支为相关正态对数增长：`x~N(m,C)`，区间长度为 `d`，则 `log(E6)=log(E0)+dᵀx`，方差 `dᵀCd`。设第i期目标EPS均值为 `M_i`、累计对数方差为 `V_i`，使用 `m_i=(Δlog M_i−0.5ΔV_i)/d_i`，保留跨年相关项。最终EPS为对数正态，通过正态CDF直接计算概率，不需要蒙特卡洛。
5. 年化普通增长期望 `exp(m_i+C_ii/2)-1`；方差 `(exp(C_ii)-1)*exp(2*m_i+C_ii)`。同时导出对数参数和完整协方差矩阵。最近财报期末通常早于今日，第一段包含桥接期，字段 `interval_years` 显式说明；六年终点仍严格为决策日加六个日历年。年度增长统计条件于正盈利情景。
6. 终点非正盈利概率暂用历史亏损频率向6%先验收缩，先验权重10个样本。这是未经校准的**终点状态先验**，不是六年内曾亏损或破产的概率。不建模亏损金额，因此不输出混合状态的无条件EPS期望。

模型初筛默认：便宜概率≥70%且亏损概率≤10%进入买入研究；高估概率≥80%或亏损概率≥25%进入卖出研究，其余观察。卖出队列是复核持仓/回避新买入，不表示做空。缺少机构预测仍计算历史回退概率，但默认不进入AI队列。文件导出另执行高风险/亏损排除，被排除股票不进入主表或AI队列。高估本身不是高风险排除理由；观察和历史回退股票仍在主表。REIT、强周期、金融公司和重大并购稀释需要专门审阅。

估值阈值也可配置为 `buy_fpe_threshold`、`sell_fpe_threshold`（默认6和6.5，必须前者小于后者）。候选判断采用 `p_below_buy_threshold` 和 `p_above_sell_threshold_positive`；旧的 `p_below_6` 等字段始终保留固定6/6.5定义，不能误读为新阈值概率。`examples/thresholds_research.json`中的8.5/10.5仅是研究假设，未验证为更好策略。后续仅用个股数据检验这些假设。

个股阈值敏感性可运行 `python scripts/analyze_stock_thresholds.py`，结果见[2012四股阈值敏感性](reports/stocks_2012_threshold_sensitivity.md)。它固定盈利分布，对比6、8.5、10、12、16，分别展示概率门槛、亏损门槛和机构共识门禁；不启用历史回退正式候选，也不拟合最优阈值。JSON保留输入快照、模型、配置和文件hash。它不读取2018实际结果，不是概率校准或收益回测。

阈值应先对应经济假设：忽略分红、税费和交易成本，若六年后退出市盈率为 `M`、要求年化价格收益为 `r`，则边界为 `K=M/(1+r)^6`。按本次选定的10%研究情景，假设 `M=15` 对应8.47，`M=18`对应10.16；8.5–10可作为待验证的研究区间。`M`是情景假设，不是对任何股票未来估值的保证；6仍可作为更严格的筛选线。不能因为调大阈值后出现候选就认定策略合理，默认6/6.5与70%/80%均未因此修改。

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

`data/research_2012_snapshots.json` 中四只股票继续作为已核对的旧年度流程样本。历史补齐以478份连贯财报、9份明确选定股类/共同申报人/特殊申报的补充资料和四只人工快照为基准，核对旧申报重叠年份后接入更早年度；冲突不自动解决，新增股份口径标志保持未核验。年报拆股倍率不自动作用于TTM组件。默认扩展历史的TTM/年度输出分别在 `outputs/stocks_2012_ttm_history_expanded/`、`outputs/stocks_2012_annual_history_expanded/`；`--history-mode original` 使用原历史和原输出目录。

重建历史并运行：

```powershell
& $py scripts/download_sec_history.py --normalize-only --zip-dir data/raw/sec_legacy_2010
& $py scripts/build_2012_history.py
& $py scripts/analyze_2012_universe.py
# 仅用于隔离年度历史扩展的影响，不是全市场TTM结果：
& $py scripts/analyze_2012_universe.py --eps-anchor annual
```

下载器还支持 `--download --quarters 2011q1 2011q2`，使用真实 `SEC_USER_AGENT` 从官方季度ZIP采集；也可通过重复 `--zip-dir` 或 `--local-archive` 导入已有文件。历史面板按原始history哈希绑定基准，变更源文件后须重新构建，不能套用旧面板。现有本地资料尚不能为全部公司补满11年。

保持TTM和模型参数不变，CLF补样本后阈值6/8.5的模型概率由57.08%/71.29%变为13.22%/16.65%，终点亏损先验由4.62%变为17.14%。见[历史补齐前后对照](reports/history_expansion_impact.md)；`python scripts/compare_2012_history.py` 可从保存的两版输出重建。该对照验证输入影响，不是收益回测或概率校准。

```powershell
& $py scripts/recover_2012_contexts.py
& $py scripts/analyze_2012_universe.py --supplemental-companies data/research_2012_supplemental_eps.json --adjustments data/research_2012_share_adjustments.json --raw-input data/raw/sec_2012_bulk/annual_eps_and_splits_2012.json
# 显式复现此前的年度EPS实验（TTM和年度输出目录相互独立）。
& $py scripts/analyze_2012_universe.py --eps-anchor annual --history-mode original --supplemental-companies data/research_2012_supplemental_eps.json --adjustments data/research_2012_share_adjustments.json
```

完整逐股概率与状态在 `all_stocks.csv/.json`，高亏损排除在 `excluded_stocks.csv/.json`，汇总和阈值数量在 `summary.json` 与 `report.md`。默认在建模前排除终点亏损先验≥25%的股票；缺共识继续测试；当前EPS非正且未达排除线者保留为模型弃权，缺EPS者保留为空值，不以0代替。原买入条件中的亏损≤10%只用于对比，不是本轮测试排除线。全部输入、配置与结果按research留存，不删除原始文件。扩展情景仍不能当作六年实际结果检验、已校准置信率或完整收益回测。

2026-11-06此前的**年度EPS实验**结果：500只名单中462只计算成功、8只高亏损先验排除、17只非正EPS模型弃权、13只缺所需EPS。修正6家年内拆股后，估值阈值6/8.5/10/12/16下达到70%概率的数量依次为2/4/7/12/41；这些不是TTM结果，也不是正式买入建议。459只存在稀疏盈利历史，概率明显依赖默认先验与波动下限。COP等已知企业分拆/并购的经济范围差异保留逐股提示，未通过删除这些股票美化结果。

TTM数据覆盖审计见 `reports/ttm_2012_data_audit.json`：本地1920条EPS原始事实全部为年度；季度申报目录不等于季度EPS已采集。新增CLF四季6.33美元使用2012-10-24前已公布数据，保留股份口径未完整核验标志。缺TTM仅是数据缺口，不是高风险判断；其余股票的旧年度概率不改标签冒充TTM概率。

早期指数实验脚本 `scripts/validate_history.py`、`scripts/analyze_thresholds.py` 及其报告、原始文件仅作为历史研究记录保留，已退出当前工作流程。以后不再自动运行，也不再用其结论评价个股策略。

当代AI可能知道历史结果，给它旧新闻不能消除知识泄漏。AI增益最好通过从现在开始冻结记录的前瞻纸面测试评估。即使准确预测EPS，也不能保证六年间价格回撤、终点估值或投资收益。当前合理用途是产生逐股研究数据、量化假设影响，并保存未来可以检验的预测。
