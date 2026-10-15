# 03 任务速览

30 条任务按 5 类各 6 条分布，完整内容见 `data/eval_tasks.json`。

| ID | 类别 | 场景 | 期望判定 | 主要考核点 |
|---|---|---|---|---|
| EV-001 | 支持 | 耳机综合续航 40 小时 | SUPPORTED | 数值容差；降噪条件不构成冲突 |
| EV-002 | 支持 | 净化器 CADR 600 | SUPPORTED | 多源一致；忽略无关信息 |
| EV-003 | 支持 | 笔记本重量 1.19kg | SUPPORTED | 低等级来源与官方一致时的标注 |
| EV-004 | 支持 | 咖啡机 20Bar 泵压 | SUPPORTED + INSUFFICIENT | 拆出因果关系 |
| EV-005 | 支持 | 监护器本地存储 | SUPPORTED | 默认行为与可选行为的区别 |
| EV-006 | 支持 | 洗地机滚刷可拆洗 | SUPPORTED | 高低等级证据混合 |
| EV-007 | 冲突 | 手机续航 20 小时 | CONTRADICTED | 数量级差异 |
| EV-008 | 冲突 | 全系支持无线充电 | CONTRADICTED | 范围型主张 |
| EV-009 | 冲突 | 独家专利防水膜 | CONTRADICTED | 排他型主张 |
| EV-010 | 冲突 | 国际安全认证 | CONTRADICTED | 认证需查一手登记源 |
| EV-011 | 冲突 | 筋膜枪同价位最强 | INSUFFICIENT + 冲突标志 | 资料互相矛盾时不硬下结论 |
| EV-012 | 冲突 | 现在浓度还是 5% | CONTRADICTED + STALE | 识别旧版数据被当作当前数据 |
| EV-013 | 证据不足 | 90% 用户 4 周改善 | INSUFFICIENT | 健康类主张的证据要求 |
| EV-014 | 证据不足 | 三甲医院推荐 | INSUFFICIENT | 机构背书；检索无结果不等于反证 |
| EV-015 | 证据不足 | 行业第一 | INSUFFICIENT | 排他主张缺少比较基础 |
| EV-016 | 证据不足 | 完全降解 / 90 天降解 | INSUFFICIENT ×2 | 营销词与标准定义的差距 |
| EV-017 | 证据不足 | 复购率 60% | INSUFFICIENT | 不可验证的内部数据 |
| EV-018 | 证据不足 | 与大牌同产线 | INSUFFICIENT | 复核队列规则的边界 |
| EV-019 | 过期 | 相册备份功能 | CONTRADICTED | 有现版本反证时仍判冲突 |
| EV-020 | 过期 | 到手价 899 元 | CONTRADICTED | 价格时效 |
| EV-021 | 过期 | 精华不含酒精 | CONTRADICTED | 配方换代 |
| EV-022 | 过期 | 抗菌认证 | INSUFFICIENT | 过期不等于伪造 |
| EV-023 | 过期 | 多篇评测说噪音低 | INSUFFICIENT + 冲突标志 | 换代后评测数据不可沿用 |
| EV-024 | 过期 | R3 滤芯 12 个月 | INSUFFICIENT | 型号错配 |
| EV-025 | 条件省略 | 续航 40 小时 | PARTIAL | 真实数据加条件省略 |
| EV-026 | 条件省略 | 充电 10 分钟用 5 小时 | PARTIAL | 配件与场景依赖 |
| EV-027 | 条件省略 | 7 天瘦 5 斤 | PARTIAL | 效果归因与小样本 |
| EV-028 | 条件省略 | CADR 600 又安静 | SUPPORTED + PARTIAL | 同报告内指标不可拼接 |
| EV-029 | 条件省略 | 含 A 醇效果明显 | SUPPORTED + PARTIAL | 成分存在与功效成立的差距 |
| EV-030 | 条件省略 | IPX5 下雨天放心用 | SUPPORTED + PARTIAL | 认证等级与使用范围 |

## 分布与说明

- 单主张任务 25 条，多主张任务 5 条（EV-004、EV-016、EV-028、EV-029、EV-030），用来检验拆解能力。
- 需要人工复核的任务 23 条，不进入复核队列的 7 条（EV-001 至 EV-006、EV-018）用来检验队列规则不会把所有任务都拦下来。
- 主判定为 INSUFFICIENT 的 12 条是核心考察区，对应"无依据确定性结论率"这个首要指标。分母只算"已给出判定"的条目：没拆出来的、没给 verdict 字段的都单列，不混进分母。
- EV-019、EV-020、EV-021 容易和 EV-022 混淆：前者有现版本反证，后者只是证据失效，这组对照点是这套评测集里最有区分度的地方。
- 标注主张共 35 条：SUPPORTED 9、INSUFFICIENT 12、CONTRADICTED 8、PARTIAL 6。
