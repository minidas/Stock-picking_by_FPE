# 2012年末四只个股：有限历史数据诊断

**这不是标普500全量筛选，也没有2012年机构共识快照。不能凭四只样本回答全市场有没有买入机会。**

决策日2012-12-31，六年目标2018-12-31。采用截止日前已公开年报中的年度稀释EPS；没有使用2013年才公布的2012全年盈利。IBM锚点为2011年年度盈利，其他为2012财政年度。没有重建更近期季度TTM，因此只是有限年度数据诊断。每家只整理了五个历史年度，短历史与年度桥接会影响结果。价格、EPS及历史股份口径一致；前三只价格来自FT年末表，仅精确到0.1美元。

由于机构共识为空，原程序严格门禁下四只均为none（数据不足以形成正式候选），**不能把这个none解释为估值不合格**。下表特意打开历史回退候选，只用于观察概率门槛：买入70%且亏损≤10%，卖出80%或亏损≥25%。

| 股票 | 年末价格 | 已知年度EPS | P(比率<6) | P(比率>6.5且盈利) | P(亏损) | 历史回退诊断 |
|---|---:|---:|---:|---:|---:|---|
| AAPL | 532.20 | 44.15 | 28.13% | 64.62% | 4.00% | none |
| IBM | 191.60 | 13.06 | 14.04% | 79.90% | 4.00% | none |
| COST | 98.70 | 3.89 | 3.93% | 91.20% | 4.00% | sell_review |
| STX | 30.42 | 6.49 | 49.70% | 36.36% | 10.67% | none |

本样本没有70%概率的买入条件，COST达到历史回退的卖出复核条件。卖出复核不是做空建议，更不表示已证实未来收益会差。STX当时静态PE低，但历史亏损和盈利波动阻止了简单的低PE买入推论。

## 8.5/10.5研究假设

保留同一模型和概率门槛，只改变估值阈值。这个参数来自退出PE和要求收益的假设，不是最优参数。

| 股票 | P(比率<8.5) | P(比率>10.5且盈利) | 历史回退诊断 |
|---|---:|---:|---|
| AAPL | 43.13% | 43.21% | none |
| IBM | 24.32% | 63.92% | none |
| COST | 8.87% | 82.45% | sell_review |
| STX | 63.28% | 19.05% | none |

## 来源与重建边界

历史成分关系以[Alex Chinco的2008–2012成员重建](https://gist.github.com/alexchinco/1ef9cb653334839f3c24)交叉确认；它是次级重建，不是官方全集审计。年末价格允许用之后发布的历史行情记录核对；盈利预测输入严格限于当时已公开财报。未输入任何2018年结果，未调用当代AI重构历史新闻观点。

- AAPL：[财务报表](https://www.sec.gov/Archives/edgar/data/320193/000119312512444068/d411355d10k.htm)；[历史价格](https://im.ft-static.com/content/images/2a53e388-569a-11e2-aa70-00144feab49a.pdf)；完整日期和限制见输入JSON。
- IBM：[财务报表](https://www.ibm.com/investor/att/pdf/IBM_Annual_Report_2011.pdf)；[历史价格](https://im.ft-static.com/content/images/2a53e388-569a-11e2-aa70-00144feab49a.pdf)；完整日期和限制见输入JSON。
- COST：[财务报表](https://www.sec.gov/Archives/edgar/data/909832/000119312512428890/d388097d10k.htm)；[历史价格](https://im.ft-static.com/content/images/2a53e388-569a-11e2-aa70-00144feab49a.pdf)；完整日期和限制见输入JSON。
- STX：[财务报表](https://www.annualreports.com/HostedData/AnnualReportArchive/s/NASDAQ_STX_2012.pdf)；[历史价格](https://www.crn.com/slide-shows/channel-programs/240145558/the-best-and-worst-technology-stocks-of-2012)；完整日期和限制见输入JSON。

没有历史机构共识、完整成分样本、最新TTM或退市收益，不能证明原策略是否在2012年有买入标的，也不能估计横截面收益。不要用2018年实现EPS倒推2012年当时的预测概率。

复现：`python scripts/analyze_2012_stocks.py`。阈值经济含义和指数误差模型问题见[阈值诊断](threshold_analysis.md)。
