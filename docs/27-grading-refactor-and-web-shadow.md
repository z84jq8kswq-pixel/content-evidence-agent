# 27 来源分级重构与联网影子模式（2026-10-15）

> 状态：**已完成**　|　两个阶段分别成文，便于回滚与复查。
> 阶段一：来源分级三层拆分（门槛／权威排序／展示权重）。
> 阶段二：联网影子模式（`fetch_web` + 快照 + 工作台参考材料区）。

## 零、可追溯基线（本仓库没有可用的 Git）

本机**没有 git.exe**，`.git` 里也没有任何提交（`refs/heads` 为空），所以改动前的
基线靠独立备份与快照建立，不依赖 Git 回滚：

| 手段 | 位置 | 内容 |
|---|---|---|
| 目录备份 | `%TEMP%\cea-baseline-20261015\` | `src/`、`app/`、`harness/`、`tests/`、`config/`、`data/`、`README.md`、`AGENTS.md`，附 `MANIFEST.sha256`（77 条，逐文件 SHA-256） |
| 对照快照 | `harness/baseline/before.json`、`after.json` | 24 份语料分级、三类排序探针、26 条缺口任务与 30 条评测任务的逐项指标与逐条主张 |
| 对照脚本 | `harness/snapshot_grading.py` | `--label before|after` 抓快照，`--diff before after` 逐项比对 |

对照用的评测运行写在 `runs/baseline-*` 下，只为拿数字，**跑完即删**（不占用既有产物编号）。

## 一、阶段一：来源分级重构

### 1.1 原来的问题（有实测证据）

`accredited` 既决定资格门槛、又被加进权重一次，同一个事实算两遍；而且权重是
**累加**的，跨维度可加会带来"数值巧合决定权威性"的路径。实测（改动前）：

| 项 | 实测 |
|---|---|
| 语料里真正被 `+0.10` 影响的资料 | 5 份（S-002／S-010／S-014 独立检测，0.95→1.00；S-005／S-006 名录，0.85→0.95） |
| 因 `+0.10` 从"不够格"变"够格"的 | 0 份 |
| 因 `+0.10` 翻转"最权威来源"归属的 | 0 份（监管库 1.00 > 独立第三方 0.95，侥幸没被超过） |
| 定性 | **不改变结论、但会改变排序的假精度**；报告里的 `0.85 + 0.10` 看起来像在算资格 |

另外三处"挑最权威来源"的代码（`assessor.best()`、`compare_numeric()`、
`highlight.authoritative_value()`）都用 `weight` 比大小，其中 `best()` 遇到
"平台规则 0.70 与厂商自述 0.70 同分"时会退化成比日期，与研究纪律相悖。

### 1.2 现在的三层

| 层 | 实现 | 规则 |
|---|---|---|
| 资格门槛 | `_eligibility()` → `strong` | 口径未变（监管／平台／厂商标称通过；厂商送检不通过；用户自述不通过） |
| 权威排序 | `rank_key()`／`rank_key_for_row()` | 有序元组（身份 → 取样 → 资质 → 日期），**元组越小越权威**，不经过任何分数 |
| 展示权重 | `_display_weight()` | 三档：过门槛且非厂商 0.90／过门槛且厂商自述 0.70／未过门槛 0.40 |

身份等级（`IDENTITY_RANK`）：监管机构 0 < 独立第三方 1 < 平台机构 2 < 厂商自述 3 < 未知 4 < 用户自述 5。
取值顺序来自 `文档库/02-判定规则/来源分级.md` 与处置档位的举证责任，没有新造规则。

删除的常量与逻辑：`IDENTITY_WEIGHT`、`SAMPLING_ADJUST`（+0.10／−0.15）、`ACCREDITED_BONUS`、
`SAMPLING_APPLIES_TO`、`ACCREDITED_APPLIES_TO`。`TIER_FALLBACK_WEIGHT` 保留（旧数据按等级折算）。

### 1.3 实现过程中真实踩到并修掉的两个缺陷

1. **排序键的日期段方向反了**。前三位是"越小越权威"，日期段却用日期字符串（字典序
   "越大越新"），放进同一个元组后 `min()` 会选到**最旧**的那条——"同权威取更新的那条"
   直接失效。修法：新增 `score_sort_key()`，把日期折成"距 2000-01-01 的天数取负"，
   越新越小；缺失日期用一个有限的哨兵值（`10**8`）排最后。
   用有限值而非 `float('inf')` 是因为排序键会进 `to_dict()`／接口，`Infinity` 不是合法 JSON。
2. **`grade_of()` 没读 `Evidence.date`**。日期只从 `meta` 取，而 `Evidence` 自己的 `date`
   字段才是这条证据的时间；meta 缺日期时排序键的日期段全空，`min()` 退化成"取第一条"。
   修法：`str(getattr(item, "date", "") or meta.get("date", "") or "")`。

两条都有回归测试锁定（`tests/test_ranking.py`）。

### 1.4 测试

新增 `tests/test_ranking.py`（排序与处置不受影响），重写 `tests/test_grading.py`
的分级用例——原来那条 `test_market_sampling_raises_and_accreditation_adds` 把取样与
资质混在一个断言里，拆成"资质不加权重""取样不加权重""厂商送检靠门槛而非负权重"等独立用例。
条数：改动前 **300** → 改动后 **377**（含阶段二新增的 44 条）。

### 1.5 全量回归对照（`--diff before after`）

| 对照项 | 结果 |
|---|---|
| 三类排序探针（`authoritative_value` 5 组、`compare_numeric` 26 条、`assessor_best` 6 组） | **完全一致** |
| 26 条缺口任务：全部指标 + 逐条主张（状态／判定／标志位／档位／规则／承载来源） | **完全一致** |
| 30 条评测任务：全部指标 + 逐条主张 | **完全一致** |
| 24 份语料分级 | 只有"展示权重与分级文案"变化（预期内），**`strong` 与身份一个没变** |

语料权重变化明细（前后）：S-002／S-010／S-014 `1.00 → 0.90`；S-005／S-006 `0.85 → 0.90`；
S-007（监管库）`1.00 → 0.90`；S-011（平台规则）`0.70 → 0.90`；S-001／S-008／S-013／S-015～S-024
（厂商自述）`0.70` 不变；S-003／S-004／S-009／S-012／S-018／S-020（用户内容）`0.30 → 0.40`。

**结论：指标未变化**，且这个结论是**逐项对照后**得出的，不是"测试通过"推断的。

## 二、阶段二：联网影子模式

### 2.1 支持范围（先说实话）

| 能力 | 输入 | 密钥 | 状态 |
|---|---|---|---|
| URL 获取 | 明确的公开地址 | **不需要** | 已实现并通过真实联网验证 |
| 结果发现 | 查询词 | **需要**（`BRAVE_SEARCH_API_KEY`） | 已实现；本机未配密钥，**真实搜索未验证** |

没有可用的搜索密钥时，发现模式返回 `NO_PROVIDER` 与明确指引，**不返回空列表、
不伪造结果**，也不去抓搜索引擎结果页。

### 2.2 `fetch_web` 协议

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mode` | string | 是 | `url`／`search` |
| `url` | string | mode=url 时 | 目标地址 |
| `query` | string | mode=search 时 | 查询词 |
| `limit` | int | 否 | 发现模式取回条数（默认 3） |

工具规格：`timeout_s=20`、`max_retries=0`、`fallback=None`、`cost_per_call=0`、只读。
**不注册进 Agent 工具集**（`build_tools()` 仍只有三个离线工具），工作台走
`build_shadow_tools()` 单独装配。

输出状态区分：`OK`、`EMPTY`、`BLOCKED`、`TIMEOUT`、`HTTP_ERROR`、`TOO_LARGE`、
`NO_TEXT`、`UNSUPPORTED_TYPE`、`UNSAFE_URL`、`BAD_URL`、`NO_PROVIDER`、`UPSTREAM_ERROR`。
失败**绝不**转成"未发现相关证据"。

### 2.3 快照

目录：`runs/<run_id>/snapshots/web/`。每个文件只写一次，同名自动加序号。

| 文件 | 内容 | 哈希字段 |
|---|---|---|
| `<name>.raw` | 原始响应字节（按上限截断时标记） | `raw_sha256`，`raw_scope` 写明"原始响应字节前 N 字节" |
| `<name>.txt` | 抽取并清洗后的正文（UTF-8） | `text_sha256`，`text_scope` 写明"UTF-8 编码" |
| `<name>.json` | 元数据：url／final_url／状态／HTTP 码／内容类型／编码／取回时间／重定向链／截断标记／失败原因／两个哈希及其对象 | — |

索引文件 `runs/<run_id>/web_references.json` **累加**（按快照去重），因为一次运行可以分多次
取材料；早期的"已存在就拒绝写入"会让后取的材料与失败记录丢失，界面比落盘更全，不可追溯。
凭据不写入任何快照（有测试断言元数据里不出现 cookie／authorization／api_key／token）。

### 2.4 安全边界

| 边界 | 实现 | 测试 |
|---|---|---|
| 协议白名单 | 只允许 http／https | `file:`、`ftp:`、`gopher:` 被拒 |
| 内网与保留地址 | 12 个 IPv4／IPv6 网段 + 元数据主机名 | 127.0.0.1／10.x／172.16／192.168／169.254.169.254／`[::1]`／0.0.0.0 全部拒绝 |
| DNS 解析后校验 | 解析出的每个地址都过同一套判断 | 内网域名、解析失败（当作获取失败）都有用例 |
| 重定向 | 每一跳重新校验，默认最多 3 跳 | "公网跳内网"被拦且**不发第二个请求**；循环跳转被截断 |
| URL 凭据 | 含用户名／口令的 URL 直接拒绝 | — |
| 体积与长度 | `max_bytes=512KB`、`max_text_chars=6000` | 截断并标记 |
| 超时 | `timeout_s=8`，工具层 20s | `TIMEOUT` 状态 |
| 并发与次数 | 单次运行最多 6 次请求、每次请求最多 3 个 URL、进程内串行 | 上限用例 |
| 内容可执行性 | 不执行 JavaScript、去掉 script／style／iframe 后只做文本清洗 | 注入脚本内容不进正文 |
| 提示注入 | 网页内容当**不可信数据**：它只进参考材料区，不改变工具权限、系统指令与判定规则 | 隔离测试（见 2.6） |

不允许的访问方式：带凭据 URL、非 http(s)、内网与元数据地址、搜索引擎结果页抓取。

### 2.5 工作台集成

`/workbench` 结果区下方新增「联网参考材料」区，独立于自动研判结果：

- 醒目标注（取自接口字段，界面不自编措辞）：「联网参考材料仅供人工参考，不参与当前自动研判结论。」
- 两种入口：直接填 URL 取回；填查询词搜索（未配密钥时按钮禁用并说明原因）。
- 每条材料显示：标题、域名、取回时间、状态标签（已取回／未取回 + 状态码）、HTTP 状态、
  是否截断、失败原因、正文（可展开／收起）、快照文件名与两个 SHA-256。
- 取回条数为 0 或失败时显示**真实失败状态**，不显示成"该主张已被证实"。

### 2.6 隔离证明（可执行的）

`tests/test_web_shadow.py::ShadowIsolationTests` 三条：

1. `fetch_web` 不在 `build_tools()` 的结果里（名字集合逐字断言）；
2. `fetch_web` 的规格是只读、零重试、无降级、零成本；
3. **同一段内容，在"取过联网材料（且落盘成功）"与"没取过"两种情况下，
   逐条主张的状态／判定／标志位／档位／规则／承载来源完全一致**，
   转人工条数也一致。

另外：参考材料写 `web_references.json`，`results.json` 里不出现任何材料字段（有断言）。
原有评测默认不调用 `fetch_web`（它不在工具集里，运行时无从调用）。

## 三、真实联网验证（2026-10-15，环境可联网）

`example.com` 实测（经 `POST /api/web-references`）：

| 项 | 结果 |
|---|---|
| 状态 | `OK`，HTTP 200，标题 `Example Domain`，域名 `example.com` |
| 取回时间 | `2026-10-15T03:31:34+00:00`（UTC） |
| 快照 | `web/example.com.raw` 577 B、`.txt` 172 B、`.json` 783 B |
| 哈希一致性 | 磁盘重算 `.raw` 的 SHA-256 **等于**元数据记录值 |
| 敏感词扫描 | 元数据里没有 cookie／authorization／api_key／token |
| 内网地址 | `http://127.0.0.1:8788/api/workbench` → `UNSAFE_URL`，未发起请求 |
| 未配密钥的发现模式 | 单条 `NO_PROVIDER` + 指引，不是空列表 |
| 结论是否被改变 | 研判签名前后一致；材料在 `web_references.json`，不在 `results.json` |

**未验证**：真实搜索 API 的发现模式（无密钥）；真实网络下的重定向、超时、体积上限
（这些路径由假 opener 的离线用例覆盖，不是真实网络验证）。

## 四、变更文件清单

| 文件 | 阶段 | 变更 |
|---|---|---|
| `src/evidence_agent/grading.py` | 一 | 三层拆分：`rank_key`／`rank_key_for_row`／`score_sort_key`／`identity_rank_of`／`sampling_rank`／`_display_weight`／`WEIGHT_*`；删除累加常量；`grade_of` 读证据日期 |
| `src/evidence_agent/runtime/assessor.py` | 一 | `Assessment.best()`、`assess()` 内的 `best`、`compare_numeric()` 改用 `rank_key` |
| `src/evidence_agent/highlight.py` | 一 | `authoritative_value()` 改用 `rank_key_for_row` |
| `src/evidence_agent/runtime/workbench.py` | 一/二 | 来源视图补 `rank`／`eligible`／日期；新增 `shadow_status`／`capture_references`／`list_references` |
| `app/server.py` | 一/二 | `/api/workbench` 增加 `web` 段；新增 `GET/POST /api/web-references`；`source_detail` 补字段 |
| `src/evidence_agent/tools/websearch.py` | 二 | 新增：SSRF 闸门、获取器、正文抽取、搜索提供方、快照存储、配置装配 |
| `src/evidence_agent/tools/shadow.py` | 二 | 新增：影子检索装配、`WebReference`、参考材料读写（累加索引） |
| `src/evidence_agent/tools/builtin.py` | 二 | 新增 `FetchWebTool` 与 `build_shadow_tools()`；`build_tools()` 明确不含 `fetch_web` |
| `src/evidence_agent/tools/__init__.py` | 二 | 导出新符号 |
| `config/web_retrieval.json` | 二 | 新增：超时、上限、重定向、搜索提供方与密钥环境变量、快照目录 |
| `app/static/workbench.html/.js/.css` | 二 | 新增联网参考材料区与样式；渲染、取回、复制、展开 |
| `tests/test_grading.py` | 一 | 拆分与新增断言（单变量隔离） |
| `tests/test_ranking.py` | 一 | 新增：排序键、日期方向、逐步降级、R13 不受影响、语料一致性 |
| `tests/test_web_shadow.py` | 二 | 新增 44 条：SSRF、获取行为、快照、影子隔离、接口 |
| `harness/snapshot_grading.py` | 一 | 新增：前后对照快照与 diff |
| `文档库/02-判定规则/来源分级.md` | 一 | 定稿口径重写：门槛／权威排序／展示权重三节 |
| `docs/13-runtime.md` | 一 | 三层职责与三个已踩的坑 |
| `README.md` | 二 | 联网参考材料的启动与配置说明 |
| `docs/27-*.md` | — | 本文件 |

## 五、已知风险与后续建议

1. **平台规则的展示权重从 0.70 升到 0.90**：语义上正确（它过门槛且不是厂商自述），但如果有
   外部脚本按 0.70 判断过这条，需要同步。仓库内没有这样的调用（已搜索确认）。
2. **联网材料目前是死数据**：取回来只给人看。下一阶段若要让它参与判定，必须单独设计评审：
   要解决"网页没有 `kind`／`sampling`／`accredited`"导致来源分级无从落地的问题，
   以及"网页原文没有结构化事实"导致 `compare_numeric` 失效的问题。
3. **搜索密钥与配额**：发现模式依赖 `BRAVE_SEARCH_API_KEY`。真实产品要考虑频率限制、
   缓存与失败重试策略；当前每次点击都真发请求。
4. **快照体积**：每次取回最多 512 KB 原始字节，`runs/` 会随之增长。若长期使用需要清理策略
   （当前按"不删既有产物"的约定不自动清理）。
5. **基线脚本的位置**：`harness/baseline/*.json` 是本次对照用的快照，可长期保留作为回归基准；
   但它抓的是当时的真实语料，语料变化后需要重新抓 `before` 才能再对照。
