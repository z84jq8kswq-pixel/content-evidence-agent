# Content Evidence Agent（内容证据核查 Agent）

面向内容平台运营与社区治理人员的证据核查 Agent。用户给出一段消费决策类内容和一组可核查资料，Agent 拆出关键主张、逐条寻找证据、判定"支持／部分支持／冲突／证据不足"，并把高风险与不确定项交给人工复核，最终输出可定位到原文和来源的结构化报告。

## 快速开始

```powershell
python app/server.py --port 8788      # 打开 http://127.0.0.1:8788/workbench
python -m unittest discover -s tests  # 377 项单元测试，纯标准库，不需要联网
```

零第三方依赖，Python 3.10+。不需要 API Key 也能完整跑通：判定默认走规则路径（本地离线、确定性、可复现）。要用模型判定加 `--adapter deepseek` 并配置密钥。

## 三条必须写在最前面的说明

1. **证据来自受控离线语料，不是联网检索。** 语料是 `data/corpus/` 下 24 份资料 / 28 条事实，刻意包含过时、冲突与低等级材料。评测与默认演示都不访问网络，这是为了可复现。工作台另有「联网参考材料」区可以取真实网页，但那部分**只作人工参考，不参与任何自动判定**。
2. **指标来自自建任务集，不是生产准确率。** 缺口任务 19 条 / 26 条主张、评测任务 30 条 / 35 条主张。其中有 12 条标注由 AI 起草、**尚待人工复查**（见 `docs/20-gap-annotations-review.md`）；也没有 holdout 集。亮眼数字只说明"在这套任务集上成立"。
3. **处置动作不自动执行。** 系统只给建议档位（放行／降级分发／打标／通知创作者／转违规／转人工），没有任何"已提交审核""已完成处置"的落地状态，界面上也不冒充。判错了被投诉的是平台，最终处置由人工确认。

## 当前状态

已完成评测 harness、模型适配器、Web 工作台、两轮实验，以及 v3 自主检索版的前半段（工具层与运行时）。**内容风险研判工作台已可真实操作**：在页面上粘贴内容 → 服务端真的跑一次 `EvidenceAgent` → 页面按"内容级摘要 → 主张 → 证据 → 规则依据"四层展示结论与不确定性。当前状态：

- `docs/01-scenario-definition.md`：场景定义、用户、痛点、边界、指标与埋点口径
- `docs/02-labeling-rules.md`：标注规则、证据等级、判定优先级与人工复核规则
- `data/eval_tasks.json`：30 条人工标注任务、35 条标注主张，已做过一轮标注复查
- `docs/03-task-overview.md`：30 条任务的速览表
- `docs/04-harness.md`：评测 harness 的口径与使用说明
- `docs/05-first-real-run.md`：第一次真实模型运行暴露的四个问题
- `docs/06-reasoning-budget-experiment.md`：推理预算 A/B 实验与可复现性验证
- `docs/07-annotation-review.md`：标注复查，以及被误判为模型错误的标注缺陷
- `docs/08-pipeline-comparison.md`：v1 单次调用与 v2 两段式的效果成本对比
- `docs/09-web-interface.md`：Web 工作台的三个视图与接口说明
- `docs/10-prd-agent-v3.md`：v3 自主检索版的 PRD（状态机、工具规格、充分性规则、下游动作与权限边界）
- `docs/11-scenario-validation.md`：场景验证，把定位从"逐条核查"修正为"内容条级分诊"
- `docs/12-tool-layer.md`：工具层（Tool Registry、三个工具、受控离线语料）
- `docs/13-runtime.md`：运行时（状态机、策略顺序、证据充分性判定与已知局限）
- `docs/14-gap-evaluation.md`：缺口任务评测（过程指标、v2 上界、基线结果与失败明细）
- `docs/15-workbench-v3.md`：工作台 v3 的三个入口（待复核队列、检索过程时间线、工具与语料）
- `docs/16-backlog.md`：待办与已确认的设计修正
- `docs/17-disposition.md`：处置档位（证据／违规／危害三维度 → 六档位，含规则表与评测结果）
- `docs/18-business-model.md`：商业可行性调研（品牌方付费意愿、三层价格带、市场规模与一手验证方案）
- `docs/19-review-fixes-20261013.md`：项目复查发现的问题与两批修正记录（静默缺陷、评测口径、还没修的清单）
- `src/evidence_agent/`：评测核心（数据模型、匹配、指标、提示词、Pipeline、Trace、报告）
- `src/evidence_agent/adapters/`：模型适配器（离线桩 + DeepSeek V4）
- `src/evidence_agent/tools/`：Tool Registry、受控离线语料与三个内置工具
- `src/evidence_agent/runtime/`：策略层、充分性判定与 Agent 状态机
- `src/evidence_agent/runtime/disposition.py`：处置档位（从结论到动作的映射）
- 许可型主张的阈值比对（法规阈值也存成事实），见 `docs/17-disposition.md`
- `src/evidence_agent/highlight.py`：标出主张里需要人盯住的片段
- `src/evidence_agent/grading.py`：来源分级（身份／取样方式／资质 → 强度权重）
- `harness/run_eval.py`：命令行入口
- `harness/rescore.py`：用已有结果重新打分，不重复调用模型
- `harness/compare_runs.py`：两次运行的版本对比报告
- `harness/run_agent_demo.py`：v3 单条内容的端到端演示（不需要 API Key）
- `harness/run_agent_eval.py`：缺口任务评测，输出指标、v2 上界与失败明细
- `data/gap_tasks.json`：19 条缺口任务（资料不完整，附最优工具集标注；六类形状分批扩样）
- `src/evidence_agent/gap_eval.py`：缺口任务的加载、过程打分与 v2 上界
- `src/evidence_agent/runtime/workbench.py`：内容研判工作台的服务层（跑一次运行、摊平结果、落盘与读回）
- `app/static/workbench.html`、`workbench.js`、`workbench.css`：内容研判页（提交内容、看结论与依据、看历史研判）
- `app/static/agent.html`、`agent.js`：待复核队列与检索过程时间线（读已有运行产物）
- `app/static/tools.html`、`tools.js`：工具与语料（Tool Registry 可视化，只读）
- `tests/test_harness.py`：匹配、规则、解析与指标的单元测试
- `tests/test_tools.py`、`tests/test_runtime.py`、`tests/test_gap_eval.py`、`tests/test_ui_api.py`、`tests/test_workbench.py`：工具层、运行时、评测、工作台接口与内容研判的单元测试
- `snapshots/v2-two-stage-20261003/`：v2 两段式管线的可还原快照

尚未开始：缺口任务扩样到 20 条以上、真实模型下的 v3 基线数据、处置档位的权限差异（打标／下架通常不是同一角色能点）。

## 默认模型

DeepSeek Flash，模型 id `deepseek-flash`，接口地址 `https://api.deepseek.com`，实际请求路径为 `https://api.deepseek.com/chat/completions`。评测时模型与解码参数固定，版本对比只改 Prompt 或工作流；换模型属于另一条对比轴，不与 Prompt 对比混在一起。

## 目录

```
content-evidence-agent/
├── README.md
├── AGENTS.md                    # 项目约定（默认 skill、常用命令）
├── config/
│   └── default.json             # 模型、解码参数、定价
├── harness/                     # 命令行入口
│   ├── run_eval.py              # v1/v2 评测（30 条）
│   ├── run_agent_eval.py        # 缺口任务评测（v3 运行时）
│   ├── repeat_agent_eval.py     # 重复运行，出噪声带
│   ├── run_agent_demo.py        # 单条内容端到端演示（零成本）
│   ├── rescore.py               # 用已有结果重新打分
│   ├── compare_runs.py          # 两次运行对比
│   └── probe_model.py           # 模型鉴权探测
├── app/
│   ├── server.py                # 标准库 HTTP 服务
│   └── static/                  # 四个视图的前端（workbench / agent / 评测 / tools）
├── snapshots/
│   └── v2-two-stage-20261003/   # v2 两段式管线的可还原快照
├── data/
│   ├── eval_tasks.json          # 30 条标注任务（资料齐全）
│   ├── gap_tasks.json           # 19 条缺口任务（资料不足，附最优工具集）
│   └── corpus/                  # 受控离线语料（sources.json + facts.json）
├── src/evidence_agent/
│   ├── adapters/                # stub 与 deepseek
│   ├── tools/                   # Tool Registry、语料与三个内置工具
│   ├── runtime/                 # 状态机、策略层、充分性判定、处置档位、工作台服务层
│   ├── dataset.py               # 任务集加载与校验
│   ├── matching.py              # 中文主张匹配
│   ├── metrics.py               # 指标与 P0/P1 错误分级
│   ├── models.py                # 数据结构与复核队列规则
│   ├── pipeline.py              # v1 单次调用 / v2 抽取+判定
│   ├── prompts.py               # 提示词模板
│   ├── report.py                # Markdown 报告
│   └── trace.py                 # JSONL Trace
├── tests/                       # 单元测试（377 项）
├── runs/                        # 运行产物（已 gitignore）
├── docs/                        # 工作区：01–25，过程记录与实验数据
└── 文档库/                       # 标准文档库：定稿的规则、口径与依据
```

## 运行

### 内容风险研判工作台（推荐入口）

```powershell
cd 'D:\JOB\项目\content-evidence-agent'
python app/server.py --port 8788
```

打开 <http://127.0.0.1:8788/workbench>，粘贴一段内容（或点下方的演示样例），
点「开始研判」。页面会真实调用 `POST /api/run-content`，服务端跑一次
`EvidenceAgent`，结果按四层展示：内容级摘要 → 逐条主张 → 证据与来源 → 规则依据。

判定方式默认是 `rule-only`（只用规则判定，本地离线、零成本、可复现）。要用模型判定：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
python app/server.py --port 8788 --adapter deepseek
```

页面顶栏会显示当前的判定方式、语料版本与份数；没有配密钥时会在该处提示，并且
模型判定失败会明确报"运行条件不足"，不会静默降级成规则结论。

把某次结论发给别人复核可以直接带运行编号打开：`http://127.0.0.1:8788/workbench?run=content-demo-guide`（演示产物，见 `docs/26-workbench-live-run.md`）。

其他三个页面：`/workbench`（内容研判，可提交）、`/agent`（待复核队列，只读已有评测运行）、`/`（评测与版本对比）、`/tools`（工具与语料规格）。

### 评测与复现

只校验任务集（不调用模型）：

```powershell
python harness/run_eval.py --validate-only
```

跑单元测试：

```powershell
python -m unittest discover -s tests -v
```

用离线桩跑通完整评测（不需要 API key）：

```powershell
python harness/run_eval.py --adapter stub --pipeline v1
python harness/run_eval.py --adapter stub --pipeline v2
```

接真实模型前先设置密钥，然后小样本试跑：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
python harness/probe_model.py
python harness/run_eval.py --adapter deepseek --pipeline v2 --limit 3
python harness/run_eval.py --adapter deepseek --pipeline v2
```

密钥也可以放在项目外：新建 `~/.deepseek/evidence-agent.env`（Windows 为
`C:\\Users\\<你>\\.deepseek\\evidence-agent.env`），内容一行
`DEEPSEEK_API_KEY=...`。脚本按 项目内 .env.local → `EVIDENCE_AGENT_ENV_FILE` →
项目外 的顺序查找，先设好的变量优先；`.env.example` 是模板。**密钥一旦进过项目目录，
就当它已经泄露，去控制台轮换。**

每次运行会在 `runs/<run_id>/` 下产出 `report.md`、`results.json`、`trace.jsonl` 和 `run_meta.json`。

改动标注或指标定义后重新打分，不需要再调用模型：

```powershell
python harness/rescore.py --run runs/<run_id>
```

对比两次运行：

```powershell
python harness/compare_runs.py --base runs/<基准> --compare runs/<对照>
```

开跑前会先发一次极小的鉴权探测请求，密钥失效时立刻中止，不会白跑整个任务集。

跑缺口任务评测（离线、可复现；主张由系统自己拆，不读标注）：

```powershell
python harness/run_agent_eval.py --run-id gap-baseline
python harness/run_agent_eval.py --adapter deepseek --extract model --run-id gap-model
```

运行目录默认不覆盖：同名目录已存在时会报错退出，需要覆盖必须显式加 `--force`。

## 还原到某个版本

`snapshots/v2-two-stage-20261003/` 是一次可还原的快照，包含当时的代码、配置、任务集和三次基线运行的产物，外加一份记录了每个文件 SHA256 的清单。

它固定了四样东西：任务集指纹 `6000dcf4`、模型 `deepseek-flash`、调用参数（温度 0、关闭思考模式）、提示词。任何一样变了，指标就不能直接对比。

还原后先用离线方式验证，不需要密钥：

```powershell
python harness/run_eval.py --validate-only
python -m unittest discover -s tests -v
python harness/run_eval.py --adapter stub --pipeline v2
```

第三条跑的是离线桩，它是确定性的，指标必须与快照记录完全一致。真实模型那一步建议用 `compare_runs.py` 对照快照里的基线，注意模型侧本身有噪声，一到两条案例的偏差属于正常范围。

## 离线桩的用途

`--adapter stub` 是一个刻意做得很朴素的基线：它只按提示词做启发式处理，从不读标注答案，并且对任何主张都回答 SUPPORTED。它的作用有两个：让评测管线在没有网络时也能跑通；证明"无依据确定性结论率"这个指标确实能把过度自信的模型挑出来。桩的结果不能当作模型能力。

## 内容研判工作台的口径与边界

页面上的每个数字和标签都有出处，改这一页时不要破坏以下约定：

| 展示内容 | 来自哪里 |
|---|---|
| 主张、状态、判定、标志位、处置档位与规则号 | `EvidenceAgent` / `runtime.disposition` 的运行结果，**原样带出**，界面不做任何二次判定 |
| 「内容级建议」 | 取各主张里风险最高的那一档（`TIER_RISK` 的最大值），页面明确标注它是取最高档，不是独立的内容级判定 |
| 证据取值与来源分级 | `data/corpus`（受控离线语料）；实体与指标由 `runtime.planner` 的规则推断，可能认错主体 |
| 数值偏差提示（一致／相差 x%） | 页面用与 `assessor.TOLERANCE_BY_METRIC` **同一张容差表**算出来的展示值，只作提示，不参与判定 |

三条不做的：

1. **不编造运行进度**。服务端是一次同步调用，没有阶段回调，所以运行时只有"正在研判"这一种状态，不会假装每步都独立执行过；
2. **不冒充后台状态**。"标记需重点复核"只写在本页内存里，刷新即失效；页面上不出现"已提交审核""已完成处置"这类没落地的措辞；
3. **不把检索到当成已证明**。证据包分「承载结论的来源」与「仅被检索到的材料」两栏，后者只是参考。

新接口（都只读或只跑 Agent，不改业务逻辑）：

| 接口 | 说明 |
|---|---|
| `GET /api/workbench` | 当前判定方式、语料版本与份数、长度上限、演示样例、联网参考能力状态 |
| `POST /api/run-content` | 跑一次内容研判；入参 `{content, adapter?, run_id?}`，产物写 `runs/<run_id>/` |
| `GET /api/content-runs` | 最近的内容研判运行列表（只列内容级产物） |
| `GET /api/content-run?run=` | 读回一次内容研判的完整结果（含该运行的联网参考材料，如果取过） |
| `POST /api/web-references` | 取一次联网参考材料；入参 `{run, url?|urls?|query?}` |
| `GET /api/web-references?run=` | 读回某次运行已保存的联网参考材料 |

内容研判与评测运行是两类产物，接口层互相拒绝：`/api/agent-runs` 不列内容研判（没有标注、没有任务集，混进版本对比会误导），`/api/content-run` 也不接受缺口评测运行。判定方式由服务端环境变量 `EVIDENCE_AGENT_ADAPTER`（或 `--adapter`）决定，刻意不做成页面开关——换判定方式会改变结论，属于运行配置。

## 来源分级的三层（权重不改结论）

来源判断拆成三个互不代偿的职责，改代码时不要重新合成一个分数：

| 层 | 回答 | 实现 | 用在 |
|---|---|---|---|
| 资格门槛 | 这条来源能不能单独支撑结论 | `grading._eligibility` → `strong` | 充分性判定、`strong`、R13 对外动作闸门 |
| 权威排序 | 多条都够格时听谁的 | `grading.rank_key`（身份 → 取样 → 资质 → 日期，元组越小越权威） | `assessor.best()`、`compare_numeric()`、`highlight.authoritative_value()` |
| 展示权重 | 报告与界面显示什么强度 | 三档 0.90／0.70／0.40 | 报告、工作台、标记 |

资质与取样方式**只决定门槛**，不再参与加权：它们的加减分已经删除，报告里也不会再出现
「基准 0.85 + 0.10」这类算式。权威性排序由有序元组保证（监管 > 独立第三方 > 平台 > 厂商 > 用户），
不依赖某个来源恰好拿到 1.00 分。定稿口径见 `文档库/02-判定规则/来源分级.md`，
重构记录与逐项回归对照见 `docs/27-grading-refactor-and-web-shadow.md`。

## 联网参考材料（影子模式）

工作台里的「联网参考材料」区用于取回真实网页给人看，**它不参与任何自动判定**：
不在 Agent 的工具集里、不进 `results.json`、不改判定与处置档位。这条边界有测试锁定
（`tests/test_web_shadow.py` 的隔离测试）。

两种模式，语义不混：

| 模式 | 输入 | 需要密钥 | 说明 |
|---|---|---|---|
| URL 获取 | 明确的公开地址 | 不需要 | 直接粘贴一个 `https` 地址即可 |
| 结果发现 | 查询词 | 需要 `BRAVE_SEARCH_API_KEY` | 未配密钥时明确提示不可用，**不伪造搜索结果** |

```powershell
# 可选：启用发现模式
$env:BRAVE_SEARCH_API_KEY = "你的密钥"
python app/server.py --port 8788
```

配置在 `config/web_retrieval.json`（超时 8s、单次最多 6 次请求、响应体上限 512 KB、
正文上限 6000 字、最多跟随 3 跳重定向）。安全边界：只允许 http／https，拒绝 localhost／
私有地址／链路本地／云元数据地址，**每次重定向后重新校验目标**，不执行 JavaScript，
网页内容一律当不可信数据。快照写在 `runs/<run_id>/snapshots/web/`，分原始字节／正文／元数据三个文件，
各自记录 SHA-256 与哈希对象；索引 `web_references.json` 累加，不覆盖已有产物。

## 下一步

1. 用 DeepSeek V4 跑出 v2 的真实基线数据，记录失败类型分布。
2. 针对失败类型改出第二版 Prompt，用同一模型跑 v1/v2 与提示词版本对比。
3. 把工作台的本地复核标记落成有归属、有回滚的记录（现在是页内内存态，刻意不写 `reviews.jsonl` 以免冒充后台动作）。
4. 检索层改多路召回（依据与实测见 `docs/23-retrieval-research.md`），并补 10 条 holdout。

## 标准文档库

`文档库/` 存放已定稿、可直接引用的标准文档（规则、口径、依据），与 `docs/` 工作区分开维护；同步时机与提炼规则见 `docs/24-standard-docs-skill.md`。本项目默认使用 skill `content-evidence-standard-docs`。
