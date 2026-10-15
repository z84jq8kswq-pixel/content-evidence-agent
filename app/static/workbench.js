/* 内容风险研判工作台。
 *
 * 三条硬约束（写在最前面，改这个文件时不要破坏它们）：
 *
 * 1. **结果只来自本次运行**。点"开始研判"会真的 POST /api/run-content，服务端
 *    跑 EvidenceAgent；页面上没有一份预制的判定结果可以顶替（演示样例只是把
 *    真实任务集里的原文填进输入框，仍然要跑一次）。
 * 2. **不编造运行进度**。后端是一次同步调用，没有阶段回调，所以运行中只有
 *    "正在研判"这一种状态；三个步骤只在返回之后回填真实数字。
 * 3. **不冒充后台业务状态**。"标记复核"只写在本页内存里，刷新即失效，页面上
 *    写明这一点；任何"已提交审核""已完成处置"的措辞都不出现。
 */
"use strict";

const API = {
  status: "/api/workbench",
  runs: "/api/content-runs",
  run: "/api/content-run?run=",
  submit: "/api/run-content",
  references: "/api/web-references",
};

/** 数值容差：与 runtime/assessor.py 的 TOLERANCE_BY_METRIC 保持一致。
 *  这里只用于把"这条主张里的数值与证据取值差多少"显示给人看，
 *  不参与任何判定——判定在服务端。 */
const TOLERANCE_BY_METRIC = {
  续航: 0.2, 降噪续航: 0.2, 亮度: 0.2, 耗时: 0.2, 滤芯寿命: 0.2,
  容量: 0.05, 额定能量: 0.05, 快充功率: 0.05, 价格: 0.05, 通量: 0.05,
  烟酰胺浓度: 0.05, 含铁量: 0.05, 废水比: 0.1,
};
const DEFAULT_TOLERANCE = 0.1;

const TIER_TONE = {
  release: "green",
  downrank: "amber",
  label: "amber",
  notify_creator: "amber",
  violation: "red",
  manual: "blue",
};

const VERDICT_TONE = {
  SUPPORTED: "green",
  PARTIAL: "amber",
  CONTRADICTED: "red",
  INSUFFICIENT: "blue",
};

const TIER_NEXT_STEP = {
  release: "不需要动作，自动入库为判例样本。",
  downrank: "提交降级分发（不进推荐池／搜索降权），不通知读者。",
  label: "提交打标，在内容上提示读者该说法未经验证。",
  notify_creator: "给创作者发整改提示，修改后可恢复。",
  violation: "转违规流程（须走申诉链路），需要更高级别复核。",
  manual: "不做对外动作，交人工判断。",
};

const EVIDENCE_MEANING = {
  E3: "证据充分，≥2 个互相独立的可用来源",
  E2: "证据充分，但只有 1 个可用来源",
  E1: "证据不充分：低等级来源／来源冲突／来源过期／检索用尽",
  E0: "工具失败，本次核查不成立",
};

const HARM_MEANING = {
  H3: "高风险：可能伤到人身或金钱",
  H2: "中风险：影响消费决策",
  H1: "低风险：主观体验类表述",
  H0: "未命中危害词",
};

const state = {
  status: null,
  running: false,
  /** 本次运行的服务端载荷；null = 未运行 */
  payload: null,
  /** 前端本地的复核标记，只在本页内存里，不上传 */
  marked: new Set(),
  history: [],
  activeRunId: null,
  /** 联网参考材料（影子模式）：只展示，不参与任何判定 */
  references: null,
  webBusy: false,
};

const $ = (id) => document.getElementById(id);

/* ---------- 基础工具 ---------- */

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, kind = "") {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast" + (kind ? " toast-" + kind : "");
  node.hidden = false;
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => {
    node.hidden = true;
  }, 2600);
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message =
      (payload && payload.error && payload.error.message) ||
      (payload && payload.error) ||
      `请求失败（HTTP ${response.status}）`;
    const error = new Error(typeof message === "string" ? message : "请求失败");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/** 把一段文本按高亮区间渲染成 HTML。区间越界一律忽略，不让渲染崩掉。 */
function renderText(text, marks) {
  const source = String(text ?? "");
  if (!marks || !marks.length) return esc(source);
  const valid = marks
    .filter((m) => Number.isInteger(m.start) && Number.isInteger(m.end))
    .filter((m) => m.start >= 0 && m.end > m.start && m.end <= source.length)
    .sort((a, b) => a.start - b.start);
  if (!valid.length) return esc(source);
  let out = "";
  let cursor = 0;
  for (const mark of valid) {
    if (mark.start < cursor) continue; // 重叠片段跳过，不做嵌套
    out += esc(source.slice(cursor, mark.start));
    out += `<span class="hl hl-${esc(mark.kind)}" title="${esc(mark.note || "")}">${esc(
      source.slice(mark.start, mark.end)
    )}</span>`;
    cursor = mark.end;
  }
  out += esc(source.slice(cursor));
  return out;
}

function fmtTime(iso) {
  if (!iso) return "时间未记录";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

/* ---------- 主张级数值比对（仅供展示） ---------- */

const NUMBER_TOKEN = /(\d+(?:\.\d+)?)\s*([A-Za-z]+|小时|个月|分钟|天|年|次|%|℃)/g;

function numberTokens(text) {
  const out = [];
  NUMBER_TOKEN.lastIndex = 0;
  let match;
  while ((match = NUMBER_TOKEN.exec(String(text ?? ""))) !== null) {
    out.push({ value: Number(match[1]), unit: match[2], text: match[0], start: match.index });
  }
  return out;
}

function toleranceFor(metric) {
  return Object.prototype.hasOwnProperty.call(TOLERANCE_BY_METRIC, metric)
    ? TOLERANCE_BY_METRIC[metric]
    : DEFAULT_TOLERANCE;
}

/**
 * 把主张里的数值与"承载结论的来源"里同一指标的取值做偏差比对。
 * 只在指标名对得上、单位一致时给出结论，否则返回 null 并说明原因——
 * 宁可不说，也不给人一个猜出来的"一致"。
 */
function compareClaimNumbers(claim, sourceById) {
  const facts = [];
  for (const sid of claim.decided_by && claim.decided_by.length ? claim.decided_by : claim.evidence) {
    const source = sourceById[sid];
    if (!source) continue;
    for (const fact of source.facts || []) {
      facts.push({ ...fact, source_id: sid, title: source.title, date: source.date, tier: source.tier });
    }
  }
  if (!facts.length) {
    return { kind: "none", note: "承载结论的来源在事实库里没有结构化取值，无法自动比对偏差。" };
  }
  const tokens = numberTokens(claim.claim);
  if (!tokens.length) {
    return { kind: "none", note: "这条主张里没有带单位的数值，偏差比对不适用。" };
  }
  const token = tokens[0];
  const sameUnit = facts.filter((f) => String(f.value).includes(token.unit));
  if (!sameUnit.length) {
    return {
      kind: "none",
      note: `证据里的取值没有与“${token.unit}”相同单位的记录（${facts
        .map((f) => `${f.metric} ${f.value}`)
        .join("、")}），偏差未比对。`,
    };
  }
  let best = null;
  for (const fact of sameUnit) {
    const ref = numberTokens(fact.value).find((t) => t.unit === token.unit);
    if (!ref || !ref.value) continue;
    const diff = Math.abs(token.value - ref.value) / ref.value;
    if (!best || diff < best.diff) best = { fact, ref, diff };
  }
  if (!best) {
    return { kind: "none", note: "证据里的取值解析不出可比的数字，偏差未比对。" };
  }
  const tolerance = toleranceFor(best.fact.metric);
  const pct = (best.diff * 100).toFixed(1);
  const within = best.diff <= tolerance;
  return {
    kind: within ? "ok" : "bad",
    metric: best.fact.metric,
    claimValue: token.text,
    evidenceValue: best.fact.value,
    sourceId: best.fact.source_id,
    sourceTitle: best.fact.title,
    tolerance,
    note: within
      ? `与最权威来源一致（相差 ${pct}%，在 ${(tolerance * 100).toFixed(0)}% 容差内；${best.fact.metric}，${best.fact.source_id}）`
      : `与最权威来源相差 ${pct}%（${best.fact.metric}，${best.fact.source_id}：${best.fact.value}）`,
  };
}

/* ---------- 渲染：顶栏与输入 ---------- */

function renderEnvStatus() {
  const node = $("env-status");
  const status = state.status;
  if (!status) {
    node.hidden = true;
    return;
  }
  const corpus = status.corpus || {};
  const parts = [
    `判定方式：${status.adapter_label}`,
    `语料 ${corpus.version || "—"}（${corpus.sources || 0} 份资料 / ${corpus.facts || 0} 条事实）`,
  ];
  if (status.adapter !== "rule-only" && !status.model_key_ready) {
    parts.push(`未检测到 ${status.model_key_env}，模型判定会失败`);
    node.className = "env-status warn";
  } else {
    node.className = "env-status";
  }
  node.textContent = parts.join(" · ");
  node.title =
    "判定方式由服务端环境变量 EVIDENCE_AGENT_ADAPTER 决定；语料为受控离线语料，" +
    "检索与判定都在本机完成，不访问网络。";
  node.hidden = false;
}

function renderSamples() {
  const holder = $("sample-buttons");
  const samples = (state.status && state.status.samples) || [];
  if (!samples.length) {
    holder.innerHTML = '<span class="wf-hint">任务集不可读，没有样例</span>';
    return;
  }
  holder.innerHTML = samples
    .map(
      (sample, index) =>
        `<button class="sample-btn" type="button" data-sample="${index}" title="来源：${esc(
          sample.origin
        )}">载入 ${esc(sample.label)}</button>`
    )
    .join("");
  holder.querySelectorAll("button[data-sample]").forEach((button) => {
    button.addEventListener("click", () => loadSample(Number(button.dataset.sample)));
  });
}

function loadSample(index) {
  const sample = (state.status.samples || [])[index];
  if (!sample) return;
  $("content-input").value = sample.content;
  updateCount();
  // 载入样例只是填输入框：结果仍然要跑一次真运行才会出现。
  setStatus("idle", "已载入演示样例", `来源 ${sample.origin}。点「开始研判」会真的跑一次 Agent。`);
  toast("已载入演示样例，点「开始研判」运行", "ok");
}

function updateCount() {
  const input = $("content-input");
  const limit = (state.status && state.status.limits.max_content_chars) || 4000;
  const length = input.value.length;
  const counter = $("char-count");
  counter.textContent = `${length} / ${limit} 字`;
  counter.className = "wf-count" + (length > limit ? " over" : "");
  const warn = $("input-warn");
  if (length > limit) {
    warn.textContent = `超出 ${length - limit} 字，请删减后再提交`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }
  $("run-btn").disabled = state.running || length === 0 || length > limit;
}

/* ---------- 渲染：状态区 ---------- */

const STEP_NAMES = ["内容解析", "主张识别", "证据核查", "风险与处置判断", "结果生成"];

function setStatus(kind, title, body, actions) {
  const node = $("run-status");
  if (kind === "hidden") {
    node.hidden = true;
    node.innerHTML = "";
    return;
  }
  node.hidden = false;
  node.className = "wf-status wf-panel" + (kind === "running" ? " running" : "") +
    (kind === "error" ? " error" : "");
  const steps =
    kind === "running"
      ? `<p class="wf-status-body" style="margin-top:8px">本次研判在本机同步完成，没有分阶段进度可展示。完成后会回填：${esc(
          STEP_NAMES.join(" → ")
        )}。</p>`
      : "";
  node.innerHTML = `
    <div class="wf-status-icon" aria-hidden="true"></div>
    <div>
      <p class="wf-status-title">${esc(title)}</p>
      <p class="wf-status-body">${esc(body || "")}</p>
      ${steps}
      ${actions || ""}
    </div>`;
}

/* ---------- 渲染：结果 ---------- */

function claimTone(claim) {
  if (claim.state === "TOOL_FAILED") return "gray";
  const verdict = claim.verdict;
  if (verdict === "CONTRADICTED") return "red";
  if (verdict === "PARTIAL") return "amber";
  if (verdict === "INSUFFICIENT") return "blue";
  if (verdict === "SUPPORTED") return "green";
  return "gray";
}

function evidencePackHtml(claim, tone) {
  const decided = claim.decided_by || [];
  const others = (claim.evidence || []).filter((sid) => !decided.includes(sid));
  const sourceById = {};
  for (const source of (state.payload?.cases?.[0]?.view?.sources) || []) {
    sourceById[source.id] = source;
  }
  const comparison = compareClaimNumbers(claim, sourceById);

  const renderSource = (source) => {
    if (!source) return "";
    const facts = (source.facts || [])
      .map(
        (fact) =>
          `<tr><td>${esc(fact.metric)}</td><td class="num">${esc(fact.value)}</td><td>${esc(
            fact.conditions || "—"
          )}</td><td>${esc(source.id)}</td></tr>`
      )
      .join("");
    const reasons = (source.grade_reasons || []).slice(-2).join("；");
    return `
      <div class="src-row">
        <div class="src-row-head">
          <div class="src-row-title">${esc(source.title || source.id)}</div>
          <div class="src-row-meta">${esc(source.kind || "类型未标注")} · ${esc(
      source.tier || "无等级"
    )} · ${esc(source.date || "日期未标注")}</div>
        </div>
        <div class="src-row-body">
          ${esc(source.publisher || "发布者未标注")}｜来源身份：${esc(source.identity || "未知")}${
      source.eligible === false ? "（不足以单独支撑结论）" : ""
    }
          ${reasons ? `<br>分级依据：${esc(reasons)}` : ""}
          ${source.excerpt ? `<br>原文片段：${esc(source.excerpt)}…` : ""}
        </div>
        ${
          facts
            ? `<table class="fact-table"><thead><tr><th>指标</th><th>取值</th><th>条件</th><th>来源</th></tr></thead><tbody>${facts}</tbody></table>`
            : ""
        }
      </div>`;
  };

  const decidedHtml = decided.length
    ? decided.map((sid) => renderSource(sourceById[sid])).join("")
    : `<p class="pack-hint">系统没有记录承载这条结论的来源。</p>`;

  const othersHtml = others.length
    ? `<p class="pack-hint">以下资料被检索到但未承载结论，仅作参考——检索到不等于已证明。</p>` +
      others.map((sid) => renderSource(sourceById[sid])).join("")
    : "";

  const compareLine =
    comparison.kind === "ok"
      ? `<p class="pack-hint" style="color:var(--green)">数值比对：${esc(comparison.note)}</p>`
      : comparison.kind === "bad"
      ? `<p class="pack-hint" style="color:var(--red)">数值比对：${esc(comparison.note)}</p>`
      : `<p class="pack-hint">数值比对：${esc(comparison.note || "不适用")}</p>`;

  return `
    <div class="pack">
      <div class="pack-head">
        <span class="pack-title">证据包</span>
        <span class="tag">${
          decided.length ? `承载结论 ${decided.length} 份` : "无承载来源"
        }${others.length ? ` · 另有 ${others.length} 份仅被检索到` : ""}</span>
      </div>
      <p class="pack-hint">「承载结论」= 运行时记录的、真正支撑这个结论的来源；其余只是被检索到的材料。</p>
      ${decidedHtml}
      ${othersHtml}
      ${compareLine}
      ${
        claim.facts && claim.facts.length
          ? `<p class="pack-hint">这条主张涉及的实体在事实库里的记录：${claim.facts
              .map((f) => `${esc(f.entity)} ${esc(f.metric)} ${esc(f.value)}（${esc(f.source_id)}）`)
              .join("；")}</p>`
          : ""
      }
      ${
        claim.evidence_override
          ? `<p class="pack-hint">证据强度被阈值判定覆盖为 ${esc(claim.evidence_override)}：小幅来源冲突被"限额判定"吸收，结论不受影响。</p>`
          : ""
      }
    </div>`;
}

function ruleBoxHtml(claim) {
  const d = claim.disposition;
  if (!d) {
    return `<div class="rule-box"><p class="rule-reason">这条主张没有处置档位（可能未进入判定）。</p></div>`;
  }
  const dims = [
    ["证据强度", d.evidence, EVIDENCE_MEANING[d.evidence]],
    ["违规构成", d.violation, d.violation === "V2" ? "被证伪且带营销特征" : d.violation === "V1" ? "被证伪但无营销特征，或部分支持" : "不构成违规（未被证伪）"],
    ["危害性质", d.harm, HARM_MEANING[d.harm]],
  ]
    .map(
      ([name, value, meaning]) =>
        `<span class="dim" title="${esc(meaning || "")}">${esc(name)} <b>${esc(value)}</b></span>`
    )
    .join("");
  return `
    <div class="rule-box">
      <div class="rule-line">
        <span class="rule-code">${esc(d.rule)}</span>
        <span class="tag solid-${TIER_TONE[d.tier] || "gray"}">建议：${esc(d.label)}</span>
        ${d.priority === "high" ? '<span class="tag solid-red">高优先级</span>' : ""}
        ${d.vendor_only ? '<span class="tag">仅厂商自述 → 动作被拦</span>' : ""}
      </div>
      <div class="rule-dims">${dims}</div>
      <p class="rule-reason">${esc(d.reason)}</p>
      <p class="rule-note">下一步：${esc(TIER_NEXT_STEP[d.tier] || "—")}</p>
      <p class="rule-note">规则按 E → V → H 顺序判定（先看证据够不够动，再看构成，最后看危害）；这里是门槛制，不是加权打分。完整规则表见「处置档位」标准文档。</p>
    </div>`;
}

function stepsHtml(claim) {
  if (!claim.steps || !claim.steps.length) {
    return `<p class="pack-hint">这次运行没有调用任何工具。</p>`;
  }
  return claim.steps
    .map((step, index) => {
      const params = Object.entries(step.params || {})
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .map(([key, value]) => `${key}=${value}`)
        .join(" · ");
      const flags = (step.flags_after || []).join("／");
      return `
        <div class="step-card${step.degraded ? " degraded" : ""}">
          <div class="step-line">
            <span class="rule-code">第 ${index + 1} 步</span>
            <span class="tag outline-accent">${esc(step.tool_label || step.tool)}</span>
            <span class="tag">${esc(step.strategy)}</span>
            <span class="tag status-${esc(step.status)}">${esc(step.status)}</span>
            ${step.degraded ? '<span class="tag solid-amber">已降级</span>' : ""}
          </div>
          <div class="step-line"><span class="lbl">为什么选它</span>${esc(step.selection_reason || "—")}</div>
          <div class="step-line"><span class="lbl">参数</span>${esc(params || "—")}</div>
          <div class="step-line"><span class="lbl">结果</span>${esc(step.digest || "—")}${
        step.sources && step.sources.length ? `（引用 ${esc(step.sources.join(" "))}）` : ""
      }</div>
          <div class="step-line"><span class="lbl">这一步之后</span>${esc(
        step.assess_reason || "—"
      )}${flags ? `（标志位 ${esc(flags)}）` : ""}</div>
        </div>`;
    })
    .join("");
}

function claimCardHtml(claim) {
  const tone = claimTone(claim);
  const d = claim.disposition;
  const key = String(claim.index);
  const marked = state.marked.has(key);
  const tags = [];
  tags.push(
    `<span class="tag solid-${tone === "gray" ? "gray" : tone}">${esc(claim.verdict_label)}</span>`
  );
  tags.push(`<span class="tag">${esc(claim.state_label)}</span>`);
  if (d) tags.push(`<span class="tag outline-accent">建议 ${esc(d.label)}</span>`);
  if (claim.needs_human) tags.push(`<span class="tag solid-blue">需人工复核</span>`);
  if (d && d.priority === "high") tags.push(`<span class="tag solid-red">高优先级</span>`);

  const flags = (claim.flags || [])
    .map(
      (flag, index) =>
        `<span class="tag" title="${esc(flag)}">${esc(
          (claim.flag_labels || [])[index] || flag
        )}</span>`
    )
    .join("");

  const textHtml = renderText(claim.claim, claim.highlights || []);

  return `
    <article class="claim-card tone-${tone}" data-claim="${esc(key)}">
      <div class="claim-top">
        <span class="claim-idx">主张 ${claim.index + 1} · ${esc(claim.claim_type || "类型未标注")}</span>
        <span class="claim-tags">${tags.join("")}</span>
      </div>
      <p class="claim-text">${textHtml}</p>
      <p class="claim-reason"><span class="lbl">判定理由</span>${esc(claim.reason)}</p>
      ${flags ? `<div class="claim-flags">${flags}</div>` : ""}
      ${evidencePackHtml(claim)}
      ${ruleBoxHtml(claim)}
      <details class="claim-details">
        <summary>查看检索过程（${(claim.steps || []).length} 步工具调用）</summary>
        ${stepsHtml(claim)}
      </details>
      <div class="wf-claim-actions">
        <button class="btn-ghost" type="button" data-act="copy-claim">复制这条依据</button>
        <button class="btn-ghost" type="button" data-act="toggle-mark">${
          marked ? "取消标记" : "标记需重点复核"
        }</button>
        ${marked ? '<span class="marked-note">已在本页标记（不写入后台，刷新即失效）</span>' : ""}
      </div>
    </article>`;
}

function buildSummaryText(payload) {
  const view = payload.cases[0].view;
  const summary = view.summary;
  const lines = [
    `内容研判摘要（运行 ${payload.run_id}）`,
    `判定方式：${payload.adapter}｜生成时间：${payload.meta.created_at || "未记录"}`,
    "",
    `内容：${view.content}`,
    "",
    `结论：${summary.triage}`,
    `- 拆出主张 ${summary.claims} 条；已给出结论 ${summary.judged} 条（不需人工 ${summary.auto_released} 条）`,
    `- 需人工判断 ${summary.needs_human} 条；系统不可用 ${summary.tool_failed} 条`,
    summary.top_tier ? `- 内容级建议（取各主张中最高档）：${summary.top_tier.label}` : "",
    "",
    "逐条主张：",
  ].filter(Boolean);
  for (const claim of view.claims) {
    const d = claim.disposition || {};
    lines.push(
      `${claim.index + 1}. ${claim.claim}｜${claim.verdict_label}｜建议 ${d.label || "—"}${
        d.rule ? `（${d.rule}）` : ""
      }`
    );
    lines.push(`   理由：${claim.reason}`);
    if (claim.decided_by && claim.decided_by.length) {
      lines.push(`   承载结论的来源：${claim.decided_by.join("、")}`);
    }
  }
  lines.push("");
  lines.push("说明：本摘要由规则路径生成，未经人工复核；处置动作不自动执行。");
  return lines.join("\n");
}

function buildClaimText(claim) {
  const d = claim.disposition || {};
  const lines = [
    `主张：${claim.claim}`,
    `状态：${claim.state_label}｜判定：${claim.verdict_label}｜建议：${d.label || "—"}${
      d.rule ? `（${d.rule}）` : ""
    }`,
    `理由：${claim.reason}`,
    (claim.flag_labels || []).length ? `标志位：${claim.flag_labels.join("、")}` : "",
    (claim.decided_by || []).length ? `承载结论的来源：${claim.decided_by.join("、")}` : "",
  ].filter(Boolean);
  if (claim.steps && claim.steps.length) {
    lines.push("检索过程：");
    claim.steps.forEach((step, index) => {
      lines.push(
        `  ${index + 1}. ${step.tool}｜${step.status}｜${step.selection_reason || ""}｜${
          step.digest || ""
        }`
      );
    });
  }
  return lines.join("\n");
}

async function copyText(text, okMessage) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
    toast(okMessage, "ok");
  } catch (error) {
    toast("复制失败，请手动选中文本", "error");
  }
}

function renderResult(payload) {
  const root = $("result-root");
  if (!payload) {
    root.innerHTML = `
      <div class="wf-empty">
        <strong>还没有研判结果。</strong><br>
        粘贴一段内容或载入演示样例，点「开始研判」。
      </div>`;
    // 没有结果时联网参考区也收起（材料要挂在一次运行上）
    state.references = null;
    $("web-panel").hidden = true;
    $("web-status").innerHTML = "";
    $("web-list").innerHTML = "";
    return;
  }
  const view = payload.cases[0].view;
  const summary = view.summary;
  const top = summary.top_tier;
  const contentHighlights = view.highlights || [];
  const contentHtml = renderText(view.content, contentHighlights);

  const cells = [
    ["可核查主张", `${summary.claims} 条`, `规则抽取器拆出：${(view.claims_extracted || []).join("、") || "无"}`],
    ["已给出结论", `${summary.judged} 条`, `其中 ${summary.auto_released} 条不需要人工介入`],
    ["需人工判断", `${summary.needs_human} 条`, "证据不足／来源冲突／系统判不了"],
    [
      "系统不可用",
      `${summary.tool_failed} 条`,
      summary.tool_failed ? "工具连续失败：本次核查不成立" : "无",
    ],
    ["工具调用", `${summary.tool_calls} 次`, `耗时 ${summary.duration_ms} ms`],
    [
      "内容级建议",
      top ? top.label : "—",
      top ? `取各主张中最高档（规则 ${top.rule}），不是独立的内容级判定` : "无处置档位",
    ],
  ];

  const attention = [];
  if (summary.needs_human) {
    attention.push(`有 ${summary.needs_human} 条主张需要人工判断，建议先看这些条目。`);
  }
  if (summary.tool_failed) {
    attention.push(
      `有 ${summary.tool_failed} 条因为系统不可用没有结论——这是可用性问题，不等于"内容没问题"。`
    );
  }
  const vendorOnly = view.claims.filter((c) => (c.disposition || {}).vendor_only);
  if (vendorOnly.length) {
    attention.push(
      `${vendorOnly.length} 条的依据全部是厂商自述：结论照给，但降级分发／打标／转违规这三档被拦为转人工（R13，举证责任）。`
    );
  }
  const highPriority = view.claims.filter((c) => (c.disposition || {}).priority === "high");
  if (highPriority.length) {
    attention.push(`${highPriority.length} 条被标为高优先级（证据不足且涉及高风险主题）。`);
  }

  const notesHtml = (view.notes || []).map((note) => `<li>${esc(note)}</li>`).join("");

  root.innerHTML = `
    <section class="wf-summary-card">
      <div class="wf-summary-head">
        <h2>内容级研判摘要</h2>
        <span class="wf-summary-sub">运行 ${esc(payload.run_id)} · ${esc(
    payload.adapter
  )} 判定 · ${esc(fmtTime(payload.meta.created_at))}</span>
      </div>
      <p class="wf-triage">${esc(summary.triage)}</p>
      <div class="wf-summary-grid">
        ${cells
          .map(
            ([k, v, n]) =>
              `<div class="wf-cell"><div class="k">${esc(k)}</div><div class="v">${esc(
                v
              )}</div><div class="n">${esc(n)}</div></div>`
          )
          .join("")}
      </div>
      ${
        attention.length
          ? `<div class="wf-callout attention"><strong>需要优先关注</strong><ul>${attention
              .map((item) => `<li>${esc(item)}</li>`)
              .join("")}</ul></div>`
          : ""
      }
      <div class="wf-callout info">
        <strong>数据来源与口径</strong>
        <ul>
          <li>本次结论来自刚刚这次真实运行（${esc(payload.adapter)}），不是预制结果。</li>
          <li>证据来自受控离线语料 <code>${esc(
            (state.status && state.status.corpus.path) || "data/corpus"
          )}</code>，检索与判定都在本机完成，不联网。</li>
          ${notesHtml}
        </ul>
      </div>
      <div class="wf-summary-actions">
        <button class="btn-ghost" type="button" data-act="copy-summary">复制研判摘要</button>
        <button class="btn-ghost" type="button" data-act="open-report">查看本次运行目录</button>
      </div>
      <p class="wf-action-hint">
        处置动作不自动执行：系统只给建议。判错了被投诉的是平台，最终处置由人工确认。
      </p>
    </section>

    <section class="wf-summary-card">
      <div class="wf-summary-head">
        <h2>待核查内容原文</h2>
        <span class="wf-summary-sub">高亮只作提示，不改变结论</span>
      </div>
      <p class="content-quote">${contentHtml}</p>
      <div class="hl-legend">
        <span class="k-bad">与证据不一致的数值</span>
        <span class="k-ok">与证据一致的数值</span>
        <span class="k-assert">容易被省略条件的断言词</span>
      </div>
    </section>

    <div class="wf-claims-head">
      <h2>逐条主张与判断依据</h2>
      <span class="wf-summary-sub">按内容里的顺序列出；每条都能展开看检索过程与规则依据</span>
    </div>
    ${
      view.claims.length
        ? view.claims.map(claimCardHtml).join("")
        : `<div class="wf-empty">这段内容里没有拆出可核查的事实性主张（没有带单位的数值或资质类断言）。</div>`
    }
  `;
  bindResultActions(payload);
  renderWebPanel();
}

function bindResultActions(payload) {
  const view = payload.cases[0].view;
  $("result-root")
    .querySelectorAll("button[data-act]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        const act = button.dataset.act;
        if (act === "copy-summary") {
          copyText(buildSummaryText(payload), "已复制研判摘要");
          return;
        }
        if (act === "open-report") {
          const dir = payload.run_dir || `runs/${payload.run_id}`;
          copyText(dir, "已复制运行目录路径：" + dir);
          return;
        }
        const card = button.closest("[data-claim]");
        const claim = view.claims[Number(card.dataset.claim)];
        if (!claim) return;
        if (act === "copy-claim") {
          copyText(buildClaimText(claim), "已复制这条主张的依据");
        }
        if (act === "toggle-mark") {
          const key = String(claim.index);
          if (state.marked.has(key)) {
            state.marked.delete(key);
          } else {
            state.marked.add(key);
          }
          renderResult(payload);
        }
      });
    });
}

/* ---------- 联网参考材料（影子模式） ---------- */

function renderWebPanel() {
  const panel = $("web-panel");
  const web = state.status && state.status.web;
  const hasRun = Boolean(state.activeRunId);
  // 没有研判运行时不给取材料：材料要挂在一次运行上，才有可追溯的目录。
  panel.hidden = !web || !web.enabled || !hasRun;

  if (panel.hidden) return;

  $("web-warning").textContent = web.disclaimer;
  $("web-note").textContent =
    "这里取回的网页只作人工参考；它不会参与主张判定、来源分级与处置建议。快照写在本次运行目录下，可复查哈希。";

  const searchNote = web.search_mode.available
    ? `发现模式可用（${web.search_mode.provider}）。`
    : `发现模式不可用：${web.search_mode.note}`;
  $("web-hint").textContent =
    `获取模式不需要密钥，直接粘一个 https 地址即可。${searchNote} ` +
    `上限：每次最多 ${web.limits.max_urls_per_request} 个地址、超时 ${web.limits.timeout_s}s、最多跟随 ${web.limits.max_redirects} 跳重定向。`;

  $("web-search").disabled = state.webBusy || !web.search_mode.available;
  $("web-fetch").disabled = state.webBusy;
  $("web-url").disabled = state.webBusy;
  $("web-query").disabled = state.webBusy;

  const list = $("web-list");
  const payload = state.references;
  if (!payload || !payload.items || !payload.items.length) {
    list.innerHTML = `<p class="wf-hint">本次运行还没有取过联网材料。</p>`;
    return;
  }
  list.innerHTML = payload.items.map(webItemHtml).join("");
  list.querySelectorAll("button[data-web-act]").forEach((button) => {
    button.addEventListener("click", () => {
      const item = payload.items[Number(button.dataset.index)];
      if (!item) return;
      const act = button.dataset.webAct;
      if (act === "toggle") {
        const body = button.closest(".web-item").querySelector(".excerpt");
        body.classList.toggle("open");
        button.textContent = body.classList.contains("open") ? "收起正文" : "展开正文";
      }
      if (act === "copy") {
        copyText(
          [
            `联网参考材料（仅人工参考，不参与自动研判结论）`,
            `标题：${item.title}`,
            `地址：${item.final_url || item.url}`,
            `取回时间：${item.fetched_at}`,
            `状态：${item.status}${item.error ? "｜" + item.error : ""}`,
            `快照：${item.snapshot_meta || "未保存"}`,
            `哈希（原始字节）：${item.raw_sha256 || "无"}`,
            "",
            item.text || "（没有可用正文）",
          ].join("\n"),
          "已复制这条参考材料"
        );
      }
    });
  });
}

function webItemHtml(item, index) {
  const tone = item.ok ? "solid-green" : "solid-red";
  const statusLabel = item.ok ? "已取回" : `未取回（${esc(item.status)}）`;
  const excerpt = item.text ? esc(item.text) : "（没有可展示的正文）";
  return `
    <article class="web-item${item.ok ? "" : " failed"}">
      <div class="web-item-head">
        <span class="web-item-title">${esc(item.title || item.url || "（无标题）")}</span>
        <span class="web-item-meta">${esc(item.domain || "域名未知")} · ${esc(
    fmtTime(item.fetched_at)
  )}</span>
      </div>
      <p class="web-item-url">
        <a href="${esc(item.final_url || item.url)}" target="_blank" rel="noopener noreferrer">${esc(
    item.final_url || item.url
  )}</a>
      </p>
      <div class="claim-tags" style="margin-top:7px">
        <span class="tag ${tone}">${statusLabel}</span>
        ${item.truncated ? '<span class="tag solid-amber">内容已截断</span>' : ""}
        <span class="tag">${esc(item.mode === "search" ? "搜索发现" : "指定地址")}</span>
        ${item.http_status ? `<span class="tag">HTTP ${item.http_status}</span>` : ""}
      </div>
      ${
        item.error
          ? `<p class="web-fail">失败原因：${esc(item.error)}</p>`
          : ""
      }
      <div class="web-item-body"><div class="excerpt">${excerpt}</div></div>
      <div class="web-hashes">
        快照：${esc(item.snapshot_meta || "未保存")}<br>
        SHA-256（原始响应字节）：${esc(item.raw_sha256 || "未记录")}<br>
        SHA-256（清洗后正文）：${esc(item.text_sha256 || "未记录")}
      </div>
      <div class="web-actions">
        ${
          item.text
            ? `<button class="btn-ghost" type="button" data-web-act="toggle" data-index="${index}">展开正文</button>`
            : ""
        }
        <button class="btn-ghost" type="button" data-web-act="copy" data-index="${index}">复制这条材料</button>
      </div>
    </article>`;
}

async function captureReferences(payload) {
  if (state.webBusy || !state.activeRunId) return;
  state.webBusy = true;
  renderWebPanel();
  $("web-status").innerHTML = `<p class="wf-status-body">正在取回网页并保存快照…</p>`;
  try {
    const response = await api(API.references, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: state.activeRunId, ...payload }),
    });
    if (response.error) {
      $("web-status").innerHTML = `<p class="web-fail">取回没有完成：${esc(
        response.error.message
      )}</p>`;
      return;
    }
    state.references = response;
    const counts = response.counts || {};
    $("web-status").innerHTML = `<p class="wf-status-body">本次请求 ${esc(
      String(response.requests)
    )} 次：成功 ${esc(String(counts.ok || 0))} 条，失败 ${esc(
      String(counts.failed || 0)
    )} 条。${response.saved === false ? "（材料已存在，本次未重复写入）" : ""}</p>`;
    renderWebPanel();
  } catch (error) {
    $("web-status").innerHTML = `<p class="web-fail">取回失败：${esc(error.message)}</p>`;
  } finally {
    state.webBusy = false;
    renderWebPanel();
  }
}

/* ---------- 历史 ---------- */

async function loadHistory() {
  const holder = $("history-list");
  try {
    const payload = await api(API.runs);
    state.history = payload.runs || [];
  } catch (error) {
    holder.innerHTML = `<p class="wf-hint">读取历史失败：${esc(error.message)}</p>`;
    return;
  }
  if (!state.history.length) {
    holder.innerHTML = `<p class="wf-hint">还没有内容研判记录。跑一次之后，产物会出现在这里。</p>`;
    return;
  }
  holder.innerHTML = state.history
    .map(
      (row) => `
      <button class="wf-history-item${
        row.run_id === state.activeRunId ? " active" : ""
      }" type="button" data-run="${esc(row.run_id)}">
        <div class="wf-history-top">
          <span class="wf-history-id">${esc(row.run_id)}</span>
          <span class="wf-history-time">${esc(fmtTime(row.created_at))}</span>
        </div>
        <div class="wf-history-excerpt">${esc(row.excerpt || "")}…</div>
        <div class="wf-history-meta">主张 ${row.claims} 条 · 需人工 ${row.needs_human} 条${
        row.tool_failed ? ` · 系统不可用 ${row.tool_failed} 条` : ""
      }${row.top_tier ? ` · 建议 ${esc(row.top_tier)}` : ""} · ${esc(row.adapter)}</div>
      </button>`
    )
    .join("");
  holder.querySelectorAll("button[data-run]").forEach((button) => {
    button.addEventListener("click", () => openRun(button.dataset.run));
  });
}

async function openRun(runId) {
  try {
    const payload = await api(API.run + encodeURIComponent(runId));
    state.payload = payload;
    state.activeRunId = runId;
    state.marked.clear();
    state.running = false;
    state.references = payload.web_references || null;
    $("content-input").value = payload.cases[0].content || "";
    updateCount();
    setStatus(
      "idle",
      `已打开历史运行 ${runId}`,
      "这是当时那次运行的产物，不会重新执行；点「开始研判」才会用当前输入跑一次新的。"
    );
    renderResult(payload);
    renderHistoryActive();
    $("web-status").innerHTML = "";
  } catch (error) {
    toast("打开历史运行失败：" + error.message, "error");
  }
}

function renderHistoryActive() {
  document.querySelectorAll("#history-list button[data-run]").forEach((button) => {
    button.classList.toggle("active", button.dataset.run === state.activeRunId);
  });
}

/* ---------- 提交 ---------- */

async function submit() {
  if (state.running) return;
  const input = $("content-input");
  const content = input.value.trim();
  const limit = (state.status && state.status.limits.max_content_chars) || 4000;

  if (!content) {
    setStatus("error", "没有可研判的内容", "请先粘贴一段内容，或从下面的演示样例里载入一条。");
    input.focus();
    return;
  }
  if (content.length > limit) {
    setStatus("error", "内容过长", `当前 ${content.length} 字，上限 ${limit} 字，请删减后再提交。`);
    return;
  }

  state.running = true;
  $("run-btn").disabled = true;
  $("clear-btn").disabled = true;
  input.disabled = true;
  // 上一次的结果必须立刻清掉：否则运行失败时会残留旧结论，被当成这次的结果。
  state.payload = null;
  state.marked.clear();
  state.references = null;
  $("result-root").innerHTML = "";
  $("web-panel").hidden = true;
  $("web-status").innerHTML = "";
  $("web-list").innerHTML = "";
  setStatus(
    "running",
    "正在研判…",
    `已提交 ${content.length} 字，正在拆主张、检索证据并做风险与处置判定。`
  );

  try {
    const payload = await api(API.submit, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (payload.error) {
      // 运行条件不足（例如没配模型密钥）：不是服务器故障，给出明确指引。
      setStatus(
        "error",
        "本次研判没有完成",
        `${payload.error.message}${payload.error.hint ? " " + payload.error.hint : ""}`,
        `<div class="wf-status-actions"><button class="btn-ghost" type="button" id="retry-btn">重新提交</button></div>`
      );
      bindRetry();
      return;
    }
    state.payload = payload;
    state.activeRunId = payload.run_id;
    setStatus(
      "idle",
      `研判完成（运行 ${payload.run_id}）`,
      `拆出 ${payload.cases[0].view.summary.claims} 条主张，其中 ${payload.cases[0].view.summary.needs_human} 条需要人工判断。`
    );
    renderResult(payload);
    await loadHistory();
  } catch (error) {
    const status = error.status;
    const kind = (error.payload && error.payload.error && error.payload.error.kind) || "";
    if (status === 409) {
      setStatus(
        "error",
        "运行编号冲突",
        "同一秒内重复提交会撞上同一个运行目录。稍等一秒再提交即可。",
        `<div class="wf-status-actions"><button class="btn-ghost" type="button" id="retry-btn">重新提交</button></div>`
      );
    } else if (kind === "invalid_content") {
      setStatus("error", "输入不合法", error.message);
    } else if (status >= 500) {
      setStatus(
        "error",
        "本次研判失败（服务端错误）",
        "服务端没有返回可用的结果。可以重试一次；持续失败请查看服务端控制台。",
        `<div class="wf-status-actions"><button class="btn-ghost" type="button" id="retry-btn">重试</button></div>`
      );
    } else {
      setStatus(
        "error",
        "本次研判没有完成",
        error.message,
        `<div class="wf-status-actions"><button class="btn-ghost" type="button" id="retry-btn">重试</button></div>`
      );
    }
    bindRetry();
  } finally {
    state.running = false;
    input.disabled = false;
    $("clear-btn").disabled = false;
    updateCount();
  }
}

function bindRetry() {
  const button = $("retry-btn");
  if (button) button.addEventListener("click", submit);
}

function clearAll() {
  if (state.running) return;
  $("content-input").value = "";
  state.payload = null;
  state.marked.clear();
  state.activeRunId = null;
  state.references = null;
  $("web-url").value = "";
  $("web-query").value = "";
  $("web-status").innerHTML = "";
  $("web-list").innerHTML = "";
  updateCount();
  setStatus("hidden");
  renderResult(null);
  renderHistoryActive();
  toast("已清空输入与结果", "ok");
  $("content-input").focus();
}

/* ---------- 启动 ---------- */

function bindEvents() {
  $("run-btn").addEventListener("click", submit);
  $("clear-btn").addEventListener("click", clearAll);
  $("refresh-history").addEventListener("click", loadHistory);
  $("content-input").addEventListener("input", updateCount);
  $("content-input").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });

  // 联网参考材料（影子模式）：取回网页，只作人工参考
  $("web-fetch").addEventListener("click", () => {
    const url = $("web-url").value.trim();
    if (!url) {
      toast("请先填一个 https 地址", "error");
      return;
    }
    captureReferences({ urls: [url] });
  });
  $("web-search").addEventListener("click", () => {
    const query = $("web-query").value.trim();
    if (!query) {
      toast("请先填查询词", "error");
      return;
    }
    captureReferences({ query });
  });
  $("web-url").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $("web-fetch").click();
    }
  });
  $("web-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (!$("web-search").disabled) $("web-search").click();
    }
  });
}

async function boot() {
  bindEvents();
  renderResult(null);
  try {
    state.status = await api(API.status);
  } catch (error) {
    setStatus(
      "error",
      "无法读取工作台状态",
      `服务端 /api/workbench 不可用：${error.message}。研判功能可能不可用。`
    );
  }
  renderEnvStatus();
  renderSamples();
  updateCount();
  await loadHistory();

  // 支持 ?run=<run_id> 直接打开一次历史研判，便于把某次结论发给别人复核。
  const requested = new URLSearchParams(window.location.search).get("run");
  if (requested) {
    await openRun(requested);
  } else {
    setStatus(
      "idle",
      "等待提交",
      "粘贴一段内容或载入下方的演示样例，点「开始研判」。结果来自本次真实运行，不是预制数据。"
    );
  }
}

boot();
