const VERDICT_LABELS = {
  SUPPORTED: "支持",
  PARTIAL: "部分支持",
  CONTRADICTED: "冲突",
  INSUFFICIENT: "证据不足",
};

const FLAG_LABELS = {
  STALE: "资料过期",
  SOURCE_CONFLICT: "资料冲突",
  SCOPE_MISMATCH: "范围错配",
  OMITTED_CONDITION: "条件省略",
  UNIT_MISMATCH: "单位不一致",
  LOW_SOURCE_QUALITY: "来源质量低",
};

const CATEGORY_LABELS = {
  supported: "证据支持",
  contradicted: "信息冲突",
  insufficient: "证据不足",
  stale: "资料过期",
  omitted_condition: "条件省略",
};

const DECISION_LABELS = {
  accept: "采纳判定",
  supported: "改判为支持",
  contradicted: "改判为冲突",
  need_more: "标记资料不足",
};

const state = {
  runs: [],
  onlyComparable: true,
  runId: "",
  detail: null,
  caseId: "",
  caseDetail: null,
  comparison: null,
  view: "review",
};

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined) return "n/a";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  }
  return String(value);
}

function verdictTag(verdict) {
  const key = VERDICT_LABELS[verdict] ? verdict : "INSUFFICIENT";
  return `<span class="verdict v-${key}"><span class="dot"></span>${VERDICT_LABELS[key]}</span>`;
}

function flagTags(flags) {
  return (flags || []).map((f) => `<span class="flag">${esc(FLAG_LABELS[f] || f)}</span>`).join(" ");
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => { node.hidden = true; }, 2600);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 ${response.status}`);
  return payload;
}

async function loadRuns() {
  const { runs } = await api("/api/runs");
  state.runs = runs.filter((r) => !r.error);
  const select = document.getElementById("run-select");
  const visible = state.runs.filter((r) => !state.onlyComparable || r.comparable);
  const shown = visible.length ? visible : state.runs;
  select.innerHTML = shown
    .map((r) => `<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.pipeline)} · ${esc(r.kind_label || "")}</option>`)
    .join("");
  const preferred = shown.find((r) => r.id === "final-judge-off") || shown[0];
  if (preferred) {
    state.runId = preferred.id;
    select.value = preferred.id;
  }
}

async function loadRunDetail() {
  if (!state.runId) return;
  state.detail = await api(`/api/run?run=${encodeURIComponent(state.runId)}`);
  const run = state.runs.find((r) => r.id === state.runId);
  const steps = run && run.steps ? Object.keys(run.steps) : [];
  document.getElementById("run-badges").innerHTML = [
    run ? `<span class="chip">${esc(run.kind_label || "")}</span>` : "",
    run ? run.model : "",
    run ? `管线 ${run.pipeline}` : "",
    run && run.eval_set_hash ? `任务集 ${run.eval_set_hash}` : "任务集版本未记录",
    run ? `成本 ${fmt(run.cost, 4)} 元` : "",
  ].filter(Boolean).map((text) => `<span class="chip">${esc(text)}</span>`).join("");
  if (!state.caseId || !state.detail.cases.some((c) => c.id === state.caseId)) {
    const needsReview = state.detail.cases.find((c) => c.needs_review) || state.detail.cases[0];
    state.caseId = needsReview ? needsReview.id : "";
  }
}

async function loadCase() {
  if (!state.runId || !state.caseId) return;
  state.caseDetail = await api(
    `/api/case?run=${encodeURIComponent(state.runId)}&case=${encodeURIComponent(state.caseId)}`
  );
}

function renderReview() {
  const detail = state.detail;
  if (!detail) return;
  const run = state.runs.find((r) => r.id === state.runId);
  const warning = run && !run.comparable
    ? `<div class="warn">这次运行属于「${esc(run.kind_label)}」，它的指标不能直接当质量结论读：
       ${esc(
         run.kind === "stub" ? "用的是离线桩，它被设计成故意答错，用来验证指标能不能分辨好坏。"
         : run.kind === "smoke" ? `只跑了 ${detail.cases.length} 条任务，样本太小。`
         : "跑在标注复查之前的任务集上，当时的正确答案本身有错。"
       )}
       要比较效果请选「完整评测」类型的运行。</div>`
    : "";
  const stats = [
    { label: "任务数", value: detail.cases.length, note: "本次运行" },
    { label: "P0 错误", value: detail.metrics.find((m) => m.key === "p0_errors")?.value ?? "n/a", note: "影响处置方向" },
    { label: "主判定准确率", value: fmt(detail.metrics.find((m) => m.key === "verdict_accuracy")?.value), note: "已匹配主张" },
    { label: "无依据确定性结论率", value: fmt(detail.metrics.find((m) => m.key === "unsupported_certainty_rate")?.value), note: "越低越好" },
  ];

  const rows = detail.cases.map((row) => `
    <tr class="clickable ${row.id === state.caseId ? "selected" : ""}" data-case="${esc(row.id)}">
      <td class="mono">${esc(row.id)}</td>
      <td>${esc(CATEGORY_LABELS[row.category] || row.category)}</td>
      <td>${row.verdicts.map((v) => verdictTag(v)).join(" ") || '<span class="muted">—</span>'}</td>
      <td class="num">${row.p0 ? `<span style="color:var(--red)">${row.p0}</span>` : "0"}</td>
    </tr>`).join("");

  document.getElementById("view-review").innerHTML = `
    ${warning}
    <div class="grid-stats">
      ${stats.map((s) => `
        <div class="stat">
          <div class="label">${esc(s.label)}</div>
          <div class="value">${esc(s.value)}</div>
          <div class="note">${esc(s.note)}</div>
        </div>`).join("")}
    </div>
    <div class="split">
      <div class="panel">
        <p class="section-title">任务列表 · ${detail.cases.length} 条</p>
        <table>
          <thead><tr><th>任务</th><th>类别</th><th>判定</th><th class="num">P0</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div id="case-panel"></div>
    </div>`;

  document.querySelectorAll("#view-review tr[data-case]").forEach((tr) => {
    tr.addEventListener("click", async () => {
      state.caseId = tr.dataset.case;
      await loadCase();
      renderReview();
      renderTrace();
    });
  });
  renderCasePanel();
}

function renderCasePanel() {
  const panel = document.getElementById("case-panel");
  if (!panel) return;
  const detail = state.caseDetail;
  if (!detail) {
    panel.innerHTML = '<p class="empty">选择左侧任意任务查看核查详情。</p>';
    return;
  }
  const c = detail.case;
  const sources = c.sources.map((s) => `
    <div class="source">
      <span class="meta">${esc(s.id)} · ${esc(s.tier)} · ${esc(s.date)}</span>
      <span>${esc(s.text)}</span>
    </div>`).join("");

  const claims = detail.predicted.length
    ? detail.predicted.map((claim) => {
      const reviewed = claim.review
        ? `<span class="reviewed">已复核：${esc(DECISION_LABELS[claim.review.decision] || claim.review.decision)}</span>`
        : "";
      return `
        <div class="claim">
          <div class="claim-head">
            ${verdictTag(claim.verdict)}
            <span>${flagTags(claim.flags)}${claim.in_review_queue ? ' <span class="flag">待复核</span>' : ""}</span>
          </div>
          <p class="claim-text">${esc(claim.text)}</p>
          <p class="small muted">证据 ${claim.evidence.length ? esc(claim.evidence.join("、")) : "无"} · 类型 ${esc(claim.claim_type)}</p>
          <p class="reason">${esc(claim.reasoning)}</p>
          <div class="actions">
            ${["accept", "supported", "contradicted", "need_more"].map((decision) => `
              <button type="button" class="${decision === "accept" ? "primary" : ""}"
                      data-review data-index="${claim.index}" data-decision="${decision}">
                ${DECISION_LABELS[decision]}
              </button>`).join("")}
            ${reviewed}
          </div>
        </div>`;
    }).join("")
    : `<p class="empty">本次运行没有产出可判定的主张。${detail.error ? esc(detail.error) : ""}</p>`;

  const expected = c.expected_claims.map((e) => `
    <tr>
      <td>${esc(e.text)}</td>
      <td>${verdictTag(e.verdict)}</td>
      <td>${flagTags(e.flags) || '<span class="muted">—</span>'}</td>
    </tr>`).join("");

  panel.innerHTML = `
    <div class="panel">
      <div class="block">
        <p class="section-title">${esc(c.id)} · ${esc(CATEGORY_LABELS[c.category] || c.category)} · ${esc(c.what_it_tests)}</p>
        <p class="content-text">${esc(c.content)}</p>
      </div>
      <div class="block">
        <p class="section-title">可用资料</p>
        ${sources}
      </div>
      <div class="block">
        <p class="section-title">模型判定</p>
        ${claims}
      </div>
      <details>
        <summary>标注答案（评测用，实际审核界面不会展示）</summary>
        <table>
          <thead><tr><th>主张</th><th>期望判定</th><th>期望标志</th></tr></thead>
          <tbody>${expected}</tbody>
        </table>
      </details>
    </div>`;

  panel.querySelectorAll("[data-review]").forEach((button) => {
    button.addEventListener("click", () => submitReview(button.dataset.index, button.dataset.decision));
  });
}

async function submitReview(index, decision) {
  try {
    await api("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run: state.runId,
        case_id: state.caseId,
        claim_index: Number(index),
        decision,
      }),
    });
    toast(`已记录：${DECISION_LABELS[decision]}`);
    await loadCase();
    renderCasePanel();
  } catch (error) {
    toast(`记录失败：${error.message}`);
  }
}

function renderTrace() {
  const container = document.getElementById("view-trace");
  const detail = state.caseDetail;
  const historyRows = state.runs.map((r) => `
    <tr>
      <td class="mono">${esc(r.id)}</td>
      <td>${esc(r.config)}</td>
      <td>${esc(r.pipeline)}</td>
      <td>${esc(r.kind_label || "")}</td>
      <td class="num">${fmt(r.verdict_accuracy)}</td>
      <td class="num">${r.p0_errors}</td>
      <td class="num">${fmt(r.cost, 4)}</td>
    </tr>`).join("");

  let callsBlock = '<p class="empty">先在核查视图选择一个任务。</p>';
  if (detail && detail.calls.length) {
    const rows = detail.calls.map((call) => `
      <tr>
        <td class="mono small">${esc(call.step)}</td>
        <td>${call.error ? `<span style="color:var(--red)">${esc(call.error_code || "错误")}</span>` : "完成"}</td>
        <td class="num">${call.latency_ms} ms</td>
        <td class="num">${call.prompt_tokens}</td>
        <td class="num">${call.completion_tokens}</td>
        <td class="num">${call.attempts}</td>
        <td>${esc(call.finish_reason || "—")}</td>
      </tr>`).join("");
    callsBlock = `
      <p class="section-title">${esc(detail.case.id)} 的调用记录 · 共 ${detail.calls.length} 次 · 总耗时 ${detail.duration_ms} ms</p>
      <table>
        <thead><tr><th>步骤</th><th>状态</th><th class="num">耗时</th><th class="num">输入</th><th class="num">输出</th><th class="num">重试</th><th>结束原因</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <details>
        <summary>展开模型原始返回</summary>
        <pre>${esc(detail.calls.map((c) => `[${c.step}]\n${c.output || "(空)"}`).join("\n\n"))}</pre>
      </details>`;
  }

  container.innerHTML = `
    <div class="panel" style="margin-bottom:16px">
      <p class="section-title">当前运行的调用记录</p>
      ${callsBlock}
    </div>
    <div class="panel">
      <p class="section-title">历史运行对比 · 共 ${state.runs.length} 次</p>
      <table>
        <thead><tr><th>运行</th><th>配置</th><th>管线</th><th>类型</th><th class="num">判定准确率</th><th class="num">P0</th><th class="num">成本(元)</th></tr></thead>
        <tbody>${historyRows}</tbody>
      </table>
    </div>`;
}

async function loadComparison() {
  const pool = state.runs.filter((r) => r.comparable);
  const base = pool.find((r) => r.pipeline === "v1") || pool[pool.length - 1] || state.runs[state.runs.length - 1];
  const target = pool.find((r) => r.id === "final-judge-off") || pool[0] || state.runs[0];
  if (!base || !target) return;
  state.comparison = await api(
    `/api/compare?base=${encodeURIComponent(base.id)}&compare=${encodeURIComponent(target.id)}`
  );
  renderCompare();
}

function renderCompare() {
  const container = document.getElementById("view-compare");
  const data = state.comparison;
  if (!data) {
    container.innerHTML = '<p class="empty">需要至少两次运行才能对比。</p>';
    return;
  }
  const rows = data.rows.map((row) => {
    const delta = row.delta === null ? "n/a"
      : (typeof row.delta === "number" && !Number.isInteger(row.delta)
        ? `${row.delta >= 0 ? "+" : ""}${row.delta.toFixed(4)}`
        : `${row.delta > 0 ? "+" : ""}${row.delta}`);
    const color = row.verdict === "改善" ? "var(--green)" : (row.verdict === "变差" ? "var(--red)" : "var(--muted)");
    return `<tr>
      <td>${esc(row.name)}</td>
      <td class="num">${fmt(row.base)}</td>
      <td class="num">${fmt(row.new)}</td>
      <td class="num">${esc(delta)}</td>
      <td style="color:${color}">${esc(row.verdict)}</td>
    </tr>`;
  }).join("");

  const changes = data.changes.length
    ? data.changes.map((change) => `
      <tr>
        <td class="mono">${esc(change.case_id)}#${change.claim_index}</td>
        <td>${verdictTag(change.expected)}</td>
        <td>${verdictTag(change.base)}</td>
        <td>${verdictTag(change.new)}</td>
        <td>${esc(change.note)}</td>
      </tr>`).join("")
    : '<tr><td colspan="5" class="muted">两次运行的判定完全一致。</td></tr>';

  container.innerHTML = `
    <div class="panel" style="margin-bottom:16px">
      <p class="section-title">基准 ${esc(data.base.run)}（${esc(data.base.pipeline)}） · 对照 ${esc(data.new.run)}（${esc(data.new.pipeline)}）</p>
      <table>
        <thead><tr><th>指标</th><th class="num">基准</th><th class="num">对照</th><th class="num">变化</th><th>方向</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="panel">
      <p class="section-title">判定发生变化的案例</p>
      <table>
        <thead><tr><th>任务</th><th>期望</th><th>基准</th><th>对照</th><th>结果</th></tr></thead>
        <tbody>${changes}</tbody>
      </table>
    </div>`;
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.getElementById("view-review").hidden = view !== "review";
  document.getElementById("view-trace").hidden = view !== "trace";
  document.getElementById("view-compare").hidden = view !== "compare";
}

async function refresh() {
  await loadRunDetail();
  await loadCase();
  renderReview();
  renderTrace();
  if (state.view === "compare") await loadComparison();
}

async function boot() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      switchView(tab.dataset.view);
      if (tab.dataset.view === "compare") await loadComparison();
    });
  });
  document.getElementById("run-select").addEventListener("change", async (event) => {
    state.runId = event.target.value;
    state.caseId = "";
    await refresh();
  });
  document.getElementById("only-comparable").addEventListener("change", async (event) => {
    state.onlyComparable = event.target.checked;
    state.caseId = "";
    await loadRuns();
    await refresh();
  });
  try {
    await loadRuns();
    await refresh();
  } catch (error) {
    document.getElementById("view-review").innerHTML =
      `<p class="empty">加载失败：${esc(error.message)}</p>`;
  }
}

boot();
