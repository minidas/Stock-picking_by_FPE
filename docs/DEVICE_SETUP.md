# 在另一台Windows上克隆与运行

按 **上传GitHub → 新机克隆 → 安装依赖 → 测试与运行** 即可。所有命令都在项目根目录的PowerShell中执行；不要求沿用原机的用户名、盘符或Qicomput路径。算法见[README](../README.md)，数据核验与AI事件处理见[Agent说明](AGENT_GUIDE.md)。

## 1. 原设备：提交项目和研究数据

把代码、文档、`requirements.txt`、`examples/`、`tests/`、`reports/`以及`data/`中的研究数据一并提交到GitHub，包含`.gitignore`和`.gitattributes`。提交前用`git status --short`检查新增文件没有遗漏。只提交README不能运行项目。

仓库已按下面的方式区分文件，不需要另外制定迁移流程：

| 内容 | Git中的处理 |
| --- | --- |
| 代码、研究JSON、来源清单、原始财报/PDF/ZIP | 一并提交，供新机离线重建当前样本 |
| `data/raw/sec_2012_bulk/range_blocks/*.bin` | 忽略约3.56 GiB的数据库下载缓存；本机保留，已有完整查询结果足够复现当前样本 |
| `duckdb_resolved_url*.txt` | 忽略临时下载地址，需要时脚本重新获取 |
| `outputs/` | 忽略；筛选结果、采集表和新运行归档在新机重新生成 |
| `.venv/`、`vendor/` | 忽略；在新机安装依赖 |
| `.env`、`data/private/` | 忽略；密钥及私有授权数据在新机另行配置 |

完整EPS查询结果、申报信息和其他研究原文仍在`data/raw/`中，排除缓存并不删除它们。缓存的来源身份和块hash记录也保留，但没有缓存字节的新克隆不能做旧缓存的逐块验证或直接离线重跑数据库SQL；这不影响下文复现。`.gitattributes`防止Git在Windows检出时改写原始数据的换行符，保留来源文件的hash。

旧研究归档留在原设备即可，**不是新机运行的前提**。当前测试尚未结束，不清理原机研究原文和缓存。

## 2. 新设备：克隆、安装、测试

先安装Git和64位Python 3.12。将下列仓库地址替换为自己的地址：

```powershell
git clone 'https://github.com/你的账号/你的仓库.git' stocking
Set-Location stocking
py -3.12 -m venv .venv
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
New-Item -ItemType Directory -Force outputs | Out-Null
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $py -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '测试失败' }
& $py scripts/stocking.py screen --input examples/snapshots.json --universe examples/memberships.csv --mode research --output outputs/demo
if ($LASTEXITCODE -ne 0) { throw '示例运行失败' }
```

无需激活虚拟环境、修改PowerShell执行策略或安装本地GPU。已有合适的Qicomput环境也可直接指定其Python路径。之后新开PowerShell时，在项目根目录重新设置`$py`；运行测试时再设置`PYTHONPATH`。

当前有100项测试；完整研究数据随Git提交后，应全部通过且无跳过。缺少持仓PDF会跳过5项价格测试，应补交原文件。示例输出在`outputs/demo/all_stocks.json`和`.csv`，使用的是合成数据，仅验证程序流程。

## 3. 继续已有2012年研究

不必重新下载数据库，也不必复制旧`outputs/`。使用仓库中的原始结果离线重建：

```powershell
& $py scripts/download_2012_sec_bulk.py --normalize-only
if ($LASTEXITCODE -ne 0) { throw 'SEC归一化失败' }
& $py scripts/extract_legacy_sec_eps.py
if ($LASTEXITCODE -ne 0) { throw '较早EPS提取失败' }
& $py scripts/build_2012_sample.py
if ($LASTEXITCODE -ne 0) { throw '样本整合失败' }
```

结果在`outputs/stocks_2012_full/`，包括全部个股JSON/CSV、逐股下载状态和`remaining_samples.csv`。截至2026-11-06，预期是500只价格证据、487只含EPS原始观察、478份年度序列、22只待处理。完整原始JSON存在时，`--normalize-only`不需要数据库缓存、DuckDB扩展或网络。

接着按Agent说明核对剩余样本、拆股和发行人身份，再补历史机构共识及六年结果。目前仅4只股票有单独核对过的模型输入；采集表不能直接当作`screen --input`，上述测试通过也不表示投资策略已经验证。

只有确实需要新的原始数据或重新执行数据库查询时才运行联网下载器；缺少的缓存会重新下载。`--offline-cache`需要缓存且可能先安装httpfs扩展，不适合用来替代上面的首次复现命令。

## 4. 实际使用

按[Agent说明](AGENT_GUIDE.md)准备真实的`snapshots.json`、成员表和原始响应。需要下载器时，在新机环境变量中配置`ALPHAVANTAGE_API_KEY`和真实身份/联系方式形式的`SEC_USER_AGENT`；程序不会自动读取`.env`。现有下载器保存原始响应，数据归一化和AI事件复核仍按Agent流程执行。

先用`--mode research`检查真实输入，通过后使用固定输出目录：

```powershell
# 以下文件须先准备好，不是仓库自带的实际投资输入。
& $py scripts/stocking.py screen --input data/private/snapshots.json --universe data/private/memberships.csv --raw-input data/private/provider_response.json --mode live --retention-days 7 --output outputs/latest
# Agent读取本轮队列，按当前baseline hash生成events.json，再运行调整。
& $py scripts/stocking.py adjust --baseline outputs/latest/baseline.json --events data/private/events.json --mode live --retention-days 7 --output outputs/latest
```

交付`outputs/latest/all_stocks.json/.csv`和排除名单。默认归档目录是`outputs/run_archive/`，新克隆可直接使用，无需另设“新设备”目录。成功live运行按7天规则清理旧实用归档中的原始/工作副本，保留最近成功运行和精简审计；research原文、`data/raw/`及用户提供的输入原件不会自动删除。详细输出和留存规则见README。

本项目已在同一Windows设备的另一源码目录通过100项测试及合成流程；另按Git实际会包含的文件建立干净副本，不带旧输出或数据库缓存，成功离线重建了上述2012年样本。另一台Windows克隆后运行第2节即可确认本机环境。
