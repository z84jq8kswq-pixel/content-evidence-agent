/* 处理内容：任务流视图。
 *
 * 三列：队列（窄）／待复核信息（最宽，人的工作对象）／检索过程（最次，按需展开）。
 * 首屏不出现任何评测指标——运营关心的是"这几条怎么处理"（PRD 7.1）。
 */

const STATE_LABEL = {
  JUDGED: "已自动放行",
  ESCALATED: "待人工",
  TOOL_FAILED: "系统不可用",
};

const FLAG_LABEL = {
  STALE: "来源过期",
  SOURCE_CONFLICT: "来源冲突",
  NOT_COMPARABLE: "不可比",
  SCOPE_MISMATCH: "范围不符",
  LOW_SOURCE_QUALITY: "来源质量低",
};

//: 处置档位，按风险从低到高。取值与 runtime/disposition.py 一一对应。
const TIER_OPTIONS = [
  { key: "release", label: "放行", cls: "tier-release", hint: "不改动内容，仅入库为判例" },
  { key: "downrank", label: "降级分发", cls: "tier-downrank", hint: "不进推荐池／搜索降权，用户无感" },
  { key: "label", label: "打标提示读者", cls: "tier-label", hint: "在内容上标注该说法未经验证" },
  {
    key: "notify_creator",
    label: "通知创作者整改",
    cls: "tier-notify",
    hint: "给作者发提示，修改后可恢复",
  },
  {
    key: "violation",
    label: "转违规处置",
    cls: "tier-violation",
    hint: "下架／账号级，须走违规流程并支持申诉",
    confirm: true,
  },
  { key: "manual", label: "转人工", cls: "tier-manual", hint: "证据不足，不做任何对外动作" },
];

//: claim_type 是内部枚举，不该直接给使用者看
const CLAIM_TYPE_LABELS = {
  numeric: "数值类",
  certification: "认证类",
  exclusive: "排他类",
  timeline: "时效类",
  scope: "范围类",
  causal: "因果类",
  health_safety: "健康安全类",
  other: "其他",
};

//: 档位配色，键与 disposition 的 tier 一致
const TIER_STYLE = {
  release: "chip-green",
  downrank: "chip-amber",
  label: "chip-blue",
  notify_creator: "chip-amber",
  violation: "chip-red",
  manual: "chip-manual",
};

function tierChip(disposition) {
  if (!disposition || !disposition.tier) return "";
  const cls = TIER_STYLE[disposition.tier] || "chip";
  return `<span class="chip ${cls}">建议 ${esc(disposition.label)}</span>`;
}

function claimTypeLabel(value) {
  return CLAIM_TYPE_LABELS[value] || value || "";
}

const state = { runs: [], runId: null, data: null, selectedKey: null };

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function toast(message, kind) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = "toast" + (kind ? " toast-" + kind : "");
  node.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    node.hidden = true;
  }, 2600);
}

function statusClass(status) {
  if (status === "OK") return "ok";
  if (status === "EMPTY") return "amber";
  return "red";
}

function formatParams(params) {
  const entries = Object.entries(params || {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== ""
  );
  return entries.length ? entries.map(([k, v]) => `${k}=${v}`).join(" · ") : "无";
}

/** 按后端给的位置标注可疑片段。位置越界或重叠时跳过，宁可少标不能标错。 */
function renderMarked(text, highlights) {
  const plain = String(text || "");
  let html = "";
  let cursor = 0;
  for (const mark of (highlights || []).slice().sort((a, b) => a.start - b.start)) {
    if (mark.start < cursor || mark.end > plain.length) continue;
    html += esc(plain.slice(cursor, mark.start));
    html += `<mark class="hl hl-${esc(mark.kind)}" title="${esc(mark.note)}">${esc(
      plain.slice(mark.start, mark.end)
    )}</mark>`;
    cursor = mark.end;
  }
  return html + esc(plain.slice(cursor));
}

function sourceList(items) {
  if (!items || !items.length) return '<p class="muted">无</p>';
  return `<ul class="src-list">${items
    .map(
      (item) => `
      <li class="src-item">
        <div class="src-title">${esc(item.title)}</div>
        <div class="src-meta" title="${esc(
          [item.identity, item.sampling, item.accredited ? "有认可资质" : "", item.weight]
            .filter(Boolean)
            .join(" · ")
        )}">${esc(
          [item.kind, item.sampling === "不明" ? "" : item.sampling, item.date]
            .filter(Boolean)
            .join(" · ")
        )}</div>
      </li>`
    )
    .join("")}</ul>`;
}

function renderStepsColumn(item) {
  const steps = item.steps || [];
  if (!steps.length) {
    return `<h3>Agent 检索过程</h3><p class="muted">这次没有调用任何工具。</p>`;
  }
  const body = steps
    .map((step, index) => {
      const previous = index > 0 ? steps[index - 1] : null;
      const retried = previous && previous.tool === step.tool;
      const swapped = previous && previous.tool !== step.tool;
      return `
        <details class="step-compact" open>
          <summary>
            <span class="step-no">第 ${index + 1} 步</span>
            <span class="chip chip-tool">${esc(step.tool)}</span>
            <span class="chip chip-${statusClass(step.status)}">${esc(step.status)}</span>
            ${retried ? '<span class="chip chip-amber">换了参数重试</span>' : ""}
            ${swapped ? '<span class="chip chip-blue">换了工具</span>' : ""}
            <span class="step-why-short">为什么选它：${esc(step.selection_reason)}</span>
          </summary>
          <div class="step-body">
            <p class="step-line"><b>参数</b>${esc(formatParams(step.params))}</p>
            <p class="step-line"><b>结果</b>${esc(step.digest)}${
              step.degraded ? '　<span class="chip chip-amber">已降级</span>' : ""
            }</p>
            <p class="step-line"><b>耗时</b>${step.latency_ms}ms</p>
            ${
              step.sources && step.sources.length
                ? `<p class="step-line"><b>引用</b>${step.sources
                    .map((s) => `<span class="chip chip-src">${esc(s)}</span>`)
                    .join("")}</p>`
                : ""
            }
          </div>
        </details>`;
    })
    .join("");
  return `
    <h3>Agent 检索过程</h3>
    <p class="steps-hint">平时不需要看。要核实"这个判断是怎么来的"，展开任意一步。</p>
    ${body}`;
}

function renderHead(data) {
  const counts = data.counts || {};
  const ceiling = data.v2_ceiling && data.v2_ceiling.ceiling;
  document.getElementById("task-head").innerHTML = `
    <div class="task-headline">
      <h1>待复核队列</h1>
      <p class="muted">
        本批 ${data.contents} 条内容、${data.claims} 条主张，其中
        <b>${counts.needs_human || 0}</b> 条需要你判断，已处理
        <b>${counts.reviewed || 0}</b> 条。
      </p>
      <p class="note-line">
        材料分三类来源：品牌官方声明、监管公开库、站内内容；证据由 Agent 检索补齐。
        本次演示用的是<b>离线语料</b>（不联网），真实部署会联网检索并为每条结论留存资料快照。
      </p>
      <p class="note-line">
        系统只给处置建议，对外动作不自动执行——判错了被投诉的是平台。每次处置都会入库为判例样本，
        同一商品下次出现同类说法时不必从零核查。
      </p>
      ${
        ceiling === 0
          ? '<p class="note-line">这一批是缺口任务：手头材料不足，关键证据全靠检索。同样的材料交给旧流程（资料直接注入），一条结论也给不出来。</p>'
          : ""
      }
    </div>`;
}

function renderList(data) {
  const items = data.items || [];
  const node = document.getElementById("queue-list");
  if (!items.length) {
    node.innerHTML = '<p class="muted">这次运行没有产生任何主张。</p>';
    return;
  }
  const pending = items.filter((item) => item.needs_human);
  const cleared = items.filter((item) => !item.needs_human);

  const row = (item) => {
    const flags = (item.flags || [])
      .map((f) => `<span class="chip chip-amber">${esc(FLAG_LABEL[f] || f)}</span>`)
      .join("");
    return `
      <button class="queue-row ${item.key === state.selectedKey ? "active" : ""}" data-key="${esc(item.key)}" type="button">
        <div class="queue-row-top">
          <span class="chip">${esc(item.category)}</span>
          ${flags}
          ${item.review ? '<span class="chip chip-green">已处置</span>' : ""}
        </div>
        <div class="queue-claim">${esc(item.claim)}</div>
        <div class="queue-reason muted">${esc(item.reason)}</div>
        <div class="queue-meta">${tierChip(item.disposition)}</div>
      </button>`;
  };

  node.innerHTML = `
    <h2 class="list-title">需要判断 <span class="count">${pending.length}</span></h2>
    ${pending.map(row).join("") || '<p class="muted">没有待处理项。</p>'}
    <h2 class="list-title muted-title">已自动处置（可抽查、可改判） <span class="count">${cleared.length}</span></h2>
    ${cleared.map(row).join("") || '<p class="muted">没有自动处置的项。</p>'}
  `;

  node.querySelectorAll(".queue-row").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedKey = button.dataset.key;
      render();
    });
  });
}

function renderMain(item) {
  const node = document.getElementById("task-main");
  if (!item) {
    node.innerHTML = '<p class="muted">从左侧选一条主张，这里会显示它的判断、证据与处置。</p>';
    return;
  }
  const flags = (item.flags || [])
    .map((f) => `<span class="chip chip-amber">${esc(FLAG_LABEL[f] || f)}</span>`)
    .join("");
  const review = item.review
    ? `<p class="reviewed">已处置：<b>${esc(
        (TIER_OPTIONS.find((d) => d.key === item.review.decision) || {}).label ||
          item.review.decision
      )}</b>　<span class="muted">${esc(item.review.reviewed_at)}</span></p>`
    : "";

  // 阅读顺序 = 人的推理顺序：先看原文，再看被拎出来的那句话，最后看判断。
  const hasHighlights = (item.highlights || []).length > 0;
  node.innerHTML = `
    <section class="detail-block">
      <h3 class="block-title">1 · 待核查内容</h3>
      <blockquote class="quote">${renderMarked(item.content, item.content_highlights)}</blockquote>
      <p class="src-caption">手头已有的材料</p>
      ${sourceList(item.given_sources_detail)}
    </section>

    <section class="detail-block">
      <h3 class="block-title">2 · 需要判断的说法</h3>
      <div class="claim-row">
        <p class="claim-big">${renderMarked(item.claim, item.highlights)}</p>
        <div class="claim-meta">
          <span class="chip chip-${item.needs_human ? "amber" : "green"}">${esc(
            STATE_LABEL[item.state] || item.state
          )}</span>
          ${item.state === "JUDGED" ? `<span class="chip chip-green">判定 ${esc(item.verdict)}</span>` : ""}
          <span class="chip">${esc(claimTypeLabel(item.claim_type))}</span>
          ${flags}
        </div>
      </div>
      ${
        hasHighlights
          ? `<div class="hl-legend">
               <span class="k-bad">与证据不一致</span>
               <span class="k-ok">与证据一致</span>
               <span class="k-conflict">可信来源不一致</span>
               <span class="k-assert">断言／许可类表述</span>
             </div>`
          : ""
      }
    </section>

    <section class="detail-block">
      <h3 class="block-title">3 · 判断结果</h3>
      <div class="result-card ${item.needs_human ? "pending" : "ok"}">
        <p class="result-reason">${esc(item.reason)}</p>
        ${
          item.disposition
            ? `<div class="tier-line">
                 ${tierChip(item.disposition)}
                 <span class="muted">${esc(item.disposition.reason)}</span>
               </div>
               <p class="tier-basis muted">依据：证据 ${esc(item.disposition.evidence)}　危害 ${esc(
                item.disposition.harm
              )}　违规 ${esc(item.disposition.violation)}　规则 ${esc(item.disposition.rule)}</p>`
            : ""
        }
      </div>
      ${
        item.evidence_detail && item.evidence_detail.length
          ? `<p class="src-caption">依据的材料</p>${sourceList(item.evidence_detail)}`
          : ""
      }
    </section>

    <section class="detail-block">
      <h3 class="block-title">4 · 你的处置（可改档）</h3>
      ${review}
      <div class="actions">
        ${TIER_OPTIONS.map(
          (tier) =>
            `<button class="action ${tier.cls}" type="button" data-decision="${tier.key}" title="${esc(
              tier.hint
            )}">${esc(tier.label)}</button>`
        ).join("")}
      </div>
      <p class="muted small">按风险从低到高排列。转违规处置会二次确认。</p>
    </section>`;

  node.querySelectorAll("[data-decision]").forEach((button) => {
    button.addEventListener("click", () => submitReview(item, button.dataset.decision));
  });
}

function render() {
  const items = (state.data && state.data.items) || [];
  const item = items.find((row) => row.key === state.selectedKey) || null;
  renderList(state.data);
  renderMain(item);
  document.getElementById("task-steps").innerHTML = item
    ? renderStepsColumn(item)
    : '<p class="muted">选中一条主张后，这里显示它的检索过程。</p>';
}

async function submitReview(item, decision) {
  const option = TIER_OPTIONS.find((tier) => tier.key === decision);
  if (option && option.confirm) {
    const ok = window.confirm(
      `确认要「${option.label}」？\n\n这个动作对创作者有实际影响，且需要能拿出证据链应对申诉。`
    );
    if (!ok) return;
  }
  try {
    await api("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run: state.runId,
        case_id: item.case_id,
        claim_index: item.claim_index,
        decision,
      }),
    });
    toast("已记录处置：" + decision, "ok");
    await load(state.runId);
  } catch (error) {
    toast("处置失败：" + error.message, "error");
  }
}

async function load(runId) {
  state.runId = runId;
  state.data = await api("/api/queue?run=" + encodeURIComponent(runId));
  const items = state.data.items || [];
  if (!state.selectedKey || !items.some((item) => item.key === state.selectedKey)) {
    const first = items.find((item) => item.needs_human) || items[0];
    state.selectedKey = first ? first.key : null;
  }
  renderHead(state.data);
  render();
}

async function boot() {
  let payload;
  try {
    payload = await api("/api/agent-runs");
  } catch (error) {
    document.getElementById("queue-list").innerHTML =
      '<p class="muted">读取运行列表失败：' + esc(error.message) + "</p>";
    return;
  }
  state.runs = payload.runs || [];
  const select = document.getElementById("run-select");
  if (!state.runs.length) {
    document.getElementById("task-head").innerHTML =
      '<p class="muted">还没有 v3 Agent 运行。先执行：<code>python harness/run_agent_eval.py</code></p>';
    return;
  }
  select.innerHTML = state.runs
    .map((run) => `<option value="${esc(run.id)}">${esc(run.id)}</option>`)
    .join("");
  state.runId = state.runs[state.runs.length - 1].id;
  select.value = state.runId;
  select.addEventListener("change", () => load(select.value));
  document.getElementById("run-badges").innerHTML = `<span class="chip">${esc(
    state.runs.find((run) => run.id === state.runId).adapter
  )}</span>`;
  await load(state.runId);
}

boot();
