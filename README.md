# 六年盈利概率选股工具：人类说明

本项目面向**个股**：机构一致预期为主生成盈利模型，程序初筛后由Agent分析候选公司的事件，再输出每只股票的结果。后续研究和实际使用不再运行指数代理分析。标普500仅用于确定个股名单，不作为一只股票建模。

**自2026-11-07用户调整要求起，新任务默认目标为六年后 `P0/E6 < 6.5`。** 买入估值门槛从6改为6.5，卖出估值门槛仍为6.5；买入/卖出概率门槛仍为70%/80%，风险政策不变。新排名和候选判断读取 `p_below_buy_threshold`；已有报告及固定指标 `p_below_6` 保留原定义，不能将旧概率改标签后当成6.5结果。

**新任务默认先筛风险，再计算估值概率。** `screen` 先检查最近3个连续完整年度盈利记录，并排除终点亏损先验≥10%或这3年中任一年EPS≤0的股票。年度不足或断档保留为数据不足、风险未评估，不进入估值排名或AI队列。这是用户要求的更严格筛选政策，亏损先验的计算公式没有改变，也不代表概率已校准。旧25%研究及已有基准继续冻结保留，不能当作新默认；政策继承与审计见第五节。

**年度EPS冲突默认采用最新可用披露。** `latest_available` 按同一财年选择决策日前最新公开的数值，保留被替代值、来源和逐项审计；不再因旧年报与新年报重叠值不同就一概截断历史。同日异值且无法确定可用时间先后时保留待核查，不猜测或退回旧值。未来披露不得进入输入。最新值选择不取消合并主体及股份口径边界；旧 `strict_overlap` 规则仅显式用于旧实验复现。

交付文件包含名单内所有非排除个股，**不局限于买入候选**：观察、卖出复核和仅历史数据回退的股票均保留；高风险或亏损股票另列排除原因。缺少数据的成员保留占位记录，不能悄悄从覆盖数量中消失。完整输出契约见第五节。

**另一台Windows使用：[克隆与运行指南](docs/DEVICE_SETUP.md)**。把代码和研究数据提交到GitHub，新机克隆后创建Python环境、安装依赖即可测试和运行；旧输出可以重建，无需搬运旧虚拟环境或运行归档。

旧研究已扩展到2012年末**500只个股的数据采集表**，历史名单与500条基金持仓估值逐股对齐；逐股采集结果见 `outputs/stocks_2012_full/download_status.csv`。当时增加的全名单历史回退概率研究入口 `scripts/analyze_2012_universe.py` 允许明确标注未经完整核验的股份口径假设，使用已有财报数字做敏感性测试，仅按旧高亏损门槛作风险排除。该实验冻结保留，不代表新任务默认政策。独立核对的[原四只真实样本](reports/stocks_2012_diagnostic.md)仍单独标记，不把其余研究情景升级为已验证输入。四只 `DEMO_*` 全部是合成演示。**500行覆盖不等于500份已验证预测；模型概率仍需用个股历史六年误差校准。**

**当前盈利基准使用TTM，年度历史已支持跨申报补齐。** 2012研究入口默认读取 `data/research_2012_ttm_anchors.json` 与 `data/research_2012_history_panels.json`，输出到 `outputs/stocks_2012_ttm_history_expanded/`。该历史研究为245家公司新增496个年度观察，多数从3年扩至5年；CLF补至2001—2011年共11年。缺口和冲突保留，不插值或填零；来源、拆股桥接和重叠核对见[补样本报告](reports/annual_history_expansion.md)。该2012样本TTM仍仅补齐CLF四个实际季度，合计6.33美元，其余缺TTM的成员保留数据缺失，不用旧年报替代。现金流、融资等变量未加入。旧年度实验用 `--eps-anchor annual --history-mode original` 显式复现。输入与公式见[模型逐步拆解](docs/MODEL_WALKTHROUGH.md)。

## 1. 两个概率的含义

`P0`是今日价格，`E6`是六年后截至目标日的完整12个月稀释每股盈利；当前研究的起点使用最新已公布TTM。预测对象是 `P0/E6`，不是六年后的交易市盈率 `P6/E6`，也不是六年累计盈利。TTM四季稀释EPS求和是明确的研究口径，因各季加权股数和稀释规则不同，不冒充重新计算的全年GAAP稀释EPS。

新任务的主要指标是 `p_below_buy_threshold = Pr(E6 > P0/6.5)`；以下固定字段保留供旧结果对照：

| 输出 | 定义 |
| --- | --- |
| `p_below_6` | `Pr(E6 > P0/6)`，未来盈利足够高的概率 |
| `p_above_6_5_positive` | `Pr(0 < E6 < P0/6.5)`，正盈利且该比率高于6.5的事件概率 |
| `p_loss` | `Pr(E6 <= 0)`，零盈利/亏损风险 |
| `p_between_6_6_5` | 正盈利且比率处于6至6.5之间 |
| `p_risk_or_above_6_5` | 高于6.5或亏损的合计概率，不冒充第二项 |

前四项总和为1。第二项没有除以正盈利概率；仅名为 `positive_conditional_*` 的EPS均值/区间条件于正盈利。负EPS不能因负市盈率低于估值门槛被当作便宜。当前EPS或有效机构预测非正时，本版本返回 `abstain`，留给专门的扭亏模型，不捏造概率。

这些是**依赖假设、尚未校准的模型预测概率**，不是经过验证的“置信率”。`P0/E[E6]`、`E[P0/E6]`和 `Pr(P0/E6<6.5)`并不相同。当前PE为20时，在恒定增长假设下，该比率六年后小于6.5需要约20.6%的年化EPS增长；6.5阈值不一定适合不同利率、行业和经济周期。

## 2. 数据来源与实际接入程度

| 数据 | 来源与支持 | 限制 |
| --- | --- | --- |
| 当前公开机构预测 | Yahoo显式 `earningsTrendGaap`，通过 `scripts/retry_current_network.py` 保存原始响应；[yfinance官方口径说明](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html) | 2026-11-07主机网络重试已取得真实GAAP模块预测；覆盖数以本轮报告为准。EPS和人数取同一模块内部 `earningsEstimate`，标准差未知留空；还须核对真实财年日期、稀释及股份口径，取得数据不等于已接入模型 |
| 其他机构一致预期接口 | [Alpha Vantage官方文档](https://www.alphavantage.co/documentation/)的 `EARNINGS_ESTIMATES`；提供带自有key的原始下载器 | 年度/季度EPS、分析师数等以账户响应为准；本次没有该接口真实key，未从Alpha Vantage下载共识，未猜测供应商字段 |
| 历史机构快照 | [FactSet Estimates Point-in-Time](https://www.factset.com/marketplace/catalog/product/factset-estimates-pointintime)等获许可的历史快照 | 需另行提供授权数据；今天的预测不可改日期冒充历史预测 |
| 历史已公布盈利 | [SEC Company Facts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)和公司财报；提供原始下载器 | 必须处理申报时间、重述、财政年度和拆股；当前API整包不自动等于无偏历史面板 |
| 行情与公司行动 | 授权的价格、拆股、股息、退市数据，归一化后导入 | 价格/EPS需同币种和股份口径；股息复权价格不能直接当作估值分子 |
| 成分证券 | 有来源和生效日期的成员CSV/JSON | [S&P说明](https://www.spglobal.com/spdji/en/research-insights/index-literacy/the-sp-500-and-the-dow/)指出成员可随公司事件变化；不能用今日成员回测历史；多股类使证券数未必恰好500 |
| 2012年末逐股采集 | `data/universe_2012_members.json`、`data/research_2012_prices.json`、下载到的SEC历史数据及原始文件 | 500只历史名单与基金持仓对应；基金估值不冒充独立核验的交易所收盘价，财报数据不冒充历史共识 |
| 2012已核对模型输入 | `data/research_2012_snapshots.json`内的公司年报、历史价格与逐项来源 | 该旧样本为四只，使用已公布年度EPS，缺少当时机构共识，不能充当全市场检验；不代表当前v5研究覆盖数 |

项目采用供应商中立JSON，并提供显式字段映射函数；**未声称完成所有供应商的一键清洗和全市场实时抓取**。GAAP与调整后EPS、年度与季度、股份单位和拆股基准必须先统一。来源字符串一致不能证明经济口径正确。[Agent说明](docs/AGENT_GUIDE.md)列出输入契约和处理流程。

网络重试批次的取得数、字段完整数及待核查项，以 [重试报告](outputs/current_sp500_2026-11-06/network_retry_2026-11-07/report.md) 和 [覆盖汇总](outputs/current_sp500_2026-11-06/network_retry_2026-11-07/coverage_summary.json) 为准。`defaultMethodology` 是ticker默认口径元数据，不覆盖显式GAAP请求；顶层 `growth` 可能仍对应默认口径，不用于计算。该暂存批次保留原次一UTC日规则：11月7日取得的观察记为11月8日可用，当时模型接入数为0。**暂存批次的0接入不等于0取得，也不代表后续新研究未使用这些数据。** 原 `latest_available_v4/` 缺共识基准与网络重试暂存文件均保持冻结；后续按用户明确要求现在使用新数据，另建下述v5研究。

当前交付入口为 [共识重排v5报告](outputs/current_sp500_2026-11-06/consensus_rerank_v5/final/report.md)。新研究采用 `observed_by_decision_timestamp`：实际UTC数据截止为 `2026-11-07T02:18:46.385159+00:00`，仅在新输入副本中采用真实观察日2026-11-07，同时保存原暂存可用日2026-11-08。它是当时已取得信息上的新决策，不是回填旧基准或盘中历史回测。机构共识组与仅历史组分开，缺少可验证新TTM、盈利范围不一致或财政日期未映射的记录不能混入机构排名；完整流程及限制见 [v5重排说明](docs/AGENT_GUIDE.md#当前交付用新共识重排v5)。

## 3. 算法概要

实现位于 `src/stocking/model.py`。默认参数集中于 `DEFAULT_CONFIG`，可通过JSON覆盖，都是研究起点，尚未校准。

TTM装配在 `src/stocking/ttm.py`：核对四季连续、完整一年、可用日期、来源和会计/股份口径；只更新 `eps0`、TTM期间和审计信息。`history`不能加入重叠TTM来增加样本量。显式TTM期末时效上限200天，旧年度输入保持550天的兼容上限；未通过核验的数据不能靠修改标签变成TTM。

季度缺失和52/53周财政日期已有可复用的证据接口：`build_current_snapshots.py --quarter-supplements <JSON> --information-cutoff <带时区UTC截止>` 接收经审阅的官方单季GAAP稀释EPS；`build_consensus_rerank.py --actuals <新快照> --fiscal-calendars <JSON>` 接收独立的新实际输入与有证据的财历规则。季度补录仍需通过四季连续、股份口径及来源校验，不以全年减前三季倒算Q4；财历规则须有已归档官方原文、hash、审阅时间和至少3个连续完整实际年度支持，只映射预测标签，不修改实际年度或TTM日期。详细字段、参数和限制见 [通用季度与财历复核](docs/AGENT_GUIDE.md#通用季度补录与财政日历复核)。

年度历史装配默认调用 `stocking.history.extend_annual_history(..., conflict_policy="latest_available")`，审计保留 `revision_audit`、`pending_periods` 和来源申报。会计政策修订或前期错报更正时，未被后来年报重列的更早年度可以保留各自最新公开值，但必须标注混合披露版本的会计口径风险（mixed-vintage accounting risk），不能称为完全同口径。合并主体改变则另设有证据的 `entity_basis_start`；例如 ECHO 从2021年起才核实为 EchoStar＋DISH 合并实际年度，不能拼入更早旧单体EPS，也不能把备考报表当实际值。拆股调整必须有明确事件及股份映射，不能从EPS比值猜测倍率。

本轮已核实三种差异：ALL 因 LDTI 新会计准则追溯重列，FIS 因前期不重大错报更正，ECHO 因共同控制下合并改变历史报表主体。FIS 的这次2022年EPS修正不是 Worldpay 处置收益或终止经营重分类。进一步复核修正了 MKC、AMCR、PCAR 新接入历史中的旧股份单位。个股桥接及原文证据见 [EPS差异复核](outputs/current_sp500_2026-11-06/eps_reconciliation/report_reviewed.md)；ALL、FIS 的更早未重列年份保留版本可比性风险，ECHO 则保留明确主体边界。

官方年报的多年 GAAP 摘要若包含 Companyfacts 遗漏的实际年度，可用有原文与hash证据的 `annual_supplements` 补录，再参与同一 `latest_available` 选择。MKC 2016/2017 年最新披露值为1.85/1.86；旧2016值按股份单位乘0.5得到1.845，不能压过较新的1.85。股份映射解决单位问题，未改变“同财年采用最新披露”的规则；补录时间也不能冒充财报发布时间。

1. 从决策日前公开的最近最多11个年度EPS中，计算相邻正盈利年份的年化对数增长，截断至±1。增长中位数按 `n/(n+6)` 向长期对数增长4%收缩。亏损保留给风险分支，不制造对数增长。
2. 每个机构预测作为EPS水平锚点，机构权重 `clip(N/(N+2),0.70,0.95)`，在对数水平上与历史路径融合；锚点之间插值。最后锚点之后的增长以2年尺度回归长期4%对数增长。这是几何收缩后的**正盈利情景条件均值锚点**，不是无条件EPS均值，也不是机构正确率。
3. 历史年化对数增长标准差设0.20下限、0.80上限；机构分歧可进一步提高波动，绝不除以分析师人数平方根。年度误差用 `0.35^|i-j|` 相关，再加入0.08的持续增长误差标准差。[预测不确定性研究](https://www.sciencedirect.com/science/article/pii/S0165410111000899)说明分析师分歧没有包含所有共同预测误差。
4. 正盈利分支为相关正态对数增长：`x~N(m,C)`，区间长度为 `d`，则 `log(E6)=log(E0)+dᵀx`，方差 `dᵀCd`。设第i期目标EPS均值为 `M_i`、累计对数方差为 `V_i`，使用 `m_i=(Δlog M_i−0.5ΔV_i)/d_i`，保留跨年相关项。最终EPS为对数正态，通过正态CDF直接计算概率，不需要蒙特卡洛。
5. 年化普通增长期望 `exp(m_i+C_ii/2)-1`；方差 `(exp(C_ii)-1)*exp(2*m_i+C_ii)`。同时导出对数参数和完整协方差矩阵。最近财报期末通常早于今日，第一段包含桥接期，字段 `interval_years` 显式说明；六年终点仍严格为决策日加六个日历年。年度增长统计条件于正盈利情景。
6. 终点非正盈利概率暂用历史亏损频率向6%先验收缩，先验权重10个样本。这是未经校准的**终点状态先验**，不是六年内曾亏损或破产的概率。不建模亏损金额，因此不输出混合状态的无条件EPS期望。

CLI `screen` 在调用估值计算前应用 `src/stocking/risk.py` 的风险政策：默认终点亏损先验须低于10%，最近3个连续完整年度EPS均须为正；当前EPS≤0、有效机构预测EPS≤0或有明确高风险标记也会排除。未通过者不生成估值概率；年度不足或断档者保留风险未评估状态，不能因先验较低而放行。通过风险筛选后，便宜概率≥70%进入买入研究，高估概率≥80%进入卖出复核，其余观察；机构共识门禁保持不变。缺少机构预测但已通过风险筛选的股票仍可计算明确标记的历史回退概率，默认不进入AI队列。高估本身不是高风险排除理由。REIT、强周期、金融公司和重大并购稀释仍需专门审阅。

`build_baseline` 数学API保留兼容入口；不能仅凭直接调用该函数的结果宣称已执行新CLI风险政策。模型配置中的买入亏损上限10%和卖出亏损复核线25%没有改变；新CLI更早执行的10%排除门槛优先，因此默认流程不会把亏损先验恰为10%的股票送入买入复核，也不会等到25%才过滤。

估值阈值也可配置为 `buy_fpe_threshold`、`sell_fpe_threshold`（默认均为6.5，必须前者不大于后者）。候选判断采用 `p_below_buy_threshold` 和 `p_above_sell_threshold_positive`；共享6.5边界时，可配置的中间区间为空。旧的 `p_below_6` 等字段始终保留固定6/6.5定义，不能误读为新阈值概率。`examples/thresholds_research.json`中的8.5/10.5仅是研究假设，未验证为更好策略。后续仅用个股数据检验这些假设。

`examples/thresholds_legacy.json` 明确保存旧6/6.5门槛，供2012研究、旧2026批次和NVDA独立案例等固定入口复现使用；这些旧入口不是未来新分析的默认入口。新任务调用通用 `screen`，已有基准的 `adjust` 则继承该基准保存的配置。

个股阈值敏感性可运行 `python scripts/analyze_stock_thresholds.py`，结果见[2012四股阈值敏感性](reports/stocks_2012_threshold_sensitivity.md)。它固定盈利分布，对比6、8.5、10、12、16，分别展示概率门槛、亏损门槛和机构共识门禁；不启用历史回退正式候选，也不拟合最优阈值。JSON保留输入快照、模型、配置和文件hash。它不读取2018实际结果，不是概率校准或收益回测。

阈值应先对应经济假设：忽略分红、税费和交易成本，若六年后退出市盈率为 `M`、要求年化价格收益为 `r`，则边界为 `K=M/(1+r)^6`。按此前选定的10%研究情景，假设 `M=15` 对应8.47，`M=18`对应10.16；8.5–10可作为待验证的研究区间。`M`是情景假设，不是对任何股票未来估值的保证。当前默认6.5来自用户明确要求，并非上述敏感性实验的校准结果；70%/80%概率门槛保持不变。不能因为调大阈值后出现候选就认定策略合理。

[长期增长研究](https://www.nber.org/papers/w8282)记录了机构长期增长预期的乐观偏差和有限预测力；因此本工具不将共识视为真值，也不机械外推短期高增长六年。

## 4. AI调整与审计

Agent只分析未被风险规则排除的初筛候选，检索决策日前180日内的公司公告、财报指引、监管披露等重要事件。判断是否已经包含在共识；已包含者只能零调整。未包含的影响须解释营收、利润率、税费或股数如何改变EPS，再提供六个年度增长增量、方差倍数、亏损概率增量和证据权重。

程序检查日期、来源URL、重复ID及幅度。单事件年度对数增长增量≤±0.10，加权合计每年≤±0.20；方差只能增加、合计最多3倍。亏损增量也受限。方差增加不会机械抬高期望EPS；增长增量仅作用于未来六年，方差倍数调整整体预测不确定性。`confidence`只是收缩权重，不是校准过的AI可靠度。程序不能验证新闻真假或推理质量。

测试阶段保留原始输入副本、hash、基准模型、事件记录和调整前后概率；实际使用采用第五节的保留策略。旧基准hash会被拒绝。没有可靠新信息就提交空列表，不能强行制造观点。工具不自动下单。

每轮另留追加式 `ai_adjustment_log.jsonl`。用户要求改变筛选政策或年度历史版本规则时，记录要求来源、原新规则与完整参数、原新输入及基准hash，以及逐股调整前后的值、状态和理由；与时事引起的EPS情景调整分开记录。保留被替代披露的日期、来源及hash。新政策下不再计算的估值概率留空，不能沿用旧值参加排名。保留旧报告，规则变更后重新 `screen` 并输出到另一目录，不覆盖旧基准来伪装为同一轮结果。

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

新 `screen` 默认风险政策为 `{"version":2,"max_loss_probability":0.10,"recent_positive_years":3}`。终点亏损先验≥10%、最近3个连续完整年度中任一年EPS≤0、当前EPS≤0、有效机构预测EPS≤0，或输入明确标记 `risk_status: "high"` 时，股票先进入排除名单，再跳过估值计算。`screen --exclude-loss-probability` 默认0.10；显式改变该参数只改变概率门槛，不取消最近3年的正盈利要求。没有明确排除依据、但年度不足或断档者留在主表，`export_status` 为 `data_unavailable`、风险未评估，估值概率留空，不进入排名和AI队列；已能计算的终点亏损先验可单独保留。已证实触发排除条件者仍排除，并同时保留年度缺口。没有买入信号、估值偏高或缺机构共识本身均不是风险排除理由。

完整政策随 `baseline.json` 的 `export_policy.risk_policy` 冻结，顶层 `export_policy.max_loss_probability` 保留兼容字段，逐股 `model.risk_screen` 保留风险判断、原因和年度证据。风险未评估时，`model.status`/`prediction.status` 为 `risk_not_assessed`，`risk_screen.status` 和导出 `risk_assessment` 为 `not_assessed`。`adjust` 继承基准的政策；版本2拒绝传入不同的排除阈值，须重新 `screen` 并另存新目录。没有嵌套风险政策的旧基准按版本1处理：沿用其已存阈值（缺失才用旧默认0.25），不追加最近3年门槛；版本1保留显式阈值覆盖的旧兼容行为，但不扩大原AI队列。不得用新的默认值重解释旧基准。

`screen --risk-policy <政策JSON路径>` 可显式指定政策文件以复现旧研究；未指定时采用版本2。`--exclude-loss-probability` 显式覆盖本轮 `screen` 的概率阈值，最终完整政策一并冻结。最近年度须先在上游核实为完整年度；程序排除季度/TTM历史，有起始日时校验期间长度，并检查相邻年度期末间隔0.75—1.25年、最新年度在 `max_eps_age_days`（默认550日）内。10%和连续3年均为更严格的工程筛选条件，尚未通过概率校准或收益回测证明。

当前v5研究在已审核年度历史上加入通过门禁的新共识与11月6日Yahoo常规时段末报价，保留11月5日价格用于归因对照。首次流程为 `refresh_rerank_ttm.py fetch` / `normalize` → `build_consensus_rerank.py --ttm-updates .../ttm_refresh/updates.json` → `run_consensus_rerank.py screen` / `finalize`；准确命令见Agent说明。原风险规则不变。本次9只新TTM未通过核验，FTV、GE、PNR的持续经营与总GAAP EPS范围不同，未映射的52/53周财政日期也不强行接入。冻结交付覆盖503只：202只进入机构组排名、11只仅历史组单列、71只排除、219只数据缺失；机构组和仅历史组不混排。低PE买入复核候选为0，175只卖出复核保留 `pending`。本次数据接入与前列股票事实核验不等于完成全部卖出事件审阅或新闻盈利调整；最终概率排名见v5报告。

已冻结v4历史回退研究的复现入口为 `python scripts/run_latest_history_study.py`，读取 `normalized_latest_available_reviewed/`，采用 `latest_available` 年度选择、主体边界及版本2风险政策；该版本报告保留在 `outputs/current_sp500_2026-11-06/latest_available_v4/final/report.md`。通用归一化脚本 `scripts/build_current_snapshots.py` 仍默认输出 `normalized_latest_available/`，v4年度基础输入曾显式使用审核后的映射和独立目录：

```powershell
& $py scripts/build_current_snapshots.py --history-policy latest_available --entity-overrides outputs/current_sp500_2026-11-06/eps_reconciliation/entity_overrides_reviewed.json --output outputs/current_sp500_2026-11-06/normalized_latest_available_reviewed
& $py scripts/run_latest_history_study.py
```

以上归一化命令用于首次装配；已有非空输出拒绝覆盖，应核验后复用。`--entity-overrides` 支持带证据的主体边界、股份映射及缺失实际年度补录。本轮不读取 `data/` 测试数据。原 `latest_available_v3/` 因 MKC、AMCR、PCAR 的历史股份单位问题已被替代，保留其 `SUPERSEDED.md` 和原产物，不把v3当作最终结果。

通用模块与本次研究包装脚本需分开理解：风险筛选、最新披露选择、股份桥接、主体边界、年度/季度补录、TTM校验和财历映射可复用；新 `scripts/compare_reconciliation_runs.py` 可对同一完整证券集合的任意两轮输出生成逐股前后日志及hash清单，没有股票、日期或503数量硬编码。当前下载、特定案例及报告/旧日志运行脚本仍含本批次的目录、日期或名单约定，不能只换股票代码就宣称支持任意公司。旧修补的逐项边界见 [改动前复用性审计](outputs/current_sp500_2026-11-06/reusable_reconciliation_audit/pre_change_reusability_review.json)，本次最终验证位于 `reusable_reconciliation_audit/validation_run_v2/`，见 [通用修复与覆盖回归报告](outputs/current_sp500_2026-11-06/reusable_reconciliation_audit/final_report.md)。旧验证及修前比较脚本保留归档，不覆盖。

本次功能验证的实际快照由293增至297，新增AZO、COST、GIS、NVDA；原293只的盈利、年度历史和价格数值不变。共识装配后288只输入中275只接入共识、13只仅历史，另9只仍缺可验证新TTM。严格筛选得到216只可用、72只风险排除、215只缺失，合计503；GIS因负TTM及最近年度负EPS排除，AZO/COST/NVDA新增可用。4家公司8个预测锚点中7个期末完成有证据的映射。仍有48个近日期缺失案例及另7家周历公司待补季度或财历证据；这不是全部52只恢复，也不是新推荐榜。177只卖出复核仍未完成，冻结v5排名保留。

原 `normalized/`、`normalized_latest_available/`、`final/` 和 `strict_risk_v2/` 保持冻结。`scripts/rescreen_current_risk.py` 只复用原冻结输入核验旧交付及输入hash，保留仅变更风险政策的版本2对照；其报告仍为 `strict_risk_v2/final/report.md`。`scripts/run_current_study.py` 显式使用 `examples/risk_policy_legacy.json`，仅用于旧25%研究复现。历史选择规则和股份映射修订均记录原新输入、基准hash及逐股替代值和风险状态，不覆盖旧结果。

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

上述2012独立研究的完整逐股概率与状态在 `all_stocks.csv/.json`，高亏损排除在 `excluded_stocks.csv/.json`，汇总和阈值数量在 `summary.json` 与 `report.md`。它是冻结的旧25%实验：当时在建模前排除终点亏损先验≥25%的股票，缺共识继续测试；当前EPS非正且未达旧排除线者保留为模型弃权，缺EPS者留空。原买入条件中的亏损≤10%当时只用于对比。该规则不是新 `screen` 的默认政策；按新政策复核时须另建基准和输出目录，保留旧结果及差异日志。全部输入、配置与结果按research留存，不删除原始文件。扩展情景仍不能当作六年实际结果检验、已校准置信率或完整收益回测。

2026-11-06此前的**年度EPS实验**结果：500只名单中462只计算成功、8只高亏损先验排除、17只非正EPS模型弃权、13只缺所需EPS。修正6家年内拆股后，估值阈值6/8.5/10/12/16下达到70%概率的数量依次为2/4/7/12/41；这些不是TTM结果，也不是正式买入建议。459只存在稀疏盈利历史，概率明显依赖默认先验与波动下限。COP等已知企业分拆/并购的经济范围差异保留逐股提示，未通过删除这些股票美化结果。

TTM数据覆盖审计见 `reports/ttm_2012_data_audit.json`：本地1920条EPS原始事实全部为年度；季度申报目录不等于季度EPS已采集。新增CLF四季6.33美元使用2012-10-24前已公布数据，保留股份口径未完整核验标志。缺TTM仅是数据缺口，不是高风险判断；其余股票的旧年度概率不改标签冒充TTM概率。

早期指数实验脚本 `scripts/validate_history.py`、`scripts/analyze_thresholds.py` 及其报告、原始文件仅作为历史研究记录保留，已退出当前工作流程。以后不再自动运行，也不再用其结论评价个股策略。

当代AI可能知道历史结果，给它旧新闻不能消除知识泄漏。AI增益最好通过从现在开始冻结记录的前瞻纸面测试评估。即使准确预测EPS，也不能保证六年间价格回撤、终点估值或投资收益。当前合理用途是产生逐股研究数据、量化假设影响，并保存未来可以检验的预测。
