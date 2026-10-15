/* 工具管理：Tool Registry 的可视化。
 *
 * 每个工具必须能回答六个问题——参数、返回、超时、重试、降级、成本。
 * 缺哪一个，研发就得回来问产品，所以这六个字段在这里全列出来。
 */

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path) {
  const response = await fetch(path);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function paramRow(param) {
  const marks = [param.type, param.required ? "必填" : "可选"];
  return `
    <li>
      <code>${esc(param.name)}</code>
      <span class="muted">${esc(marks.join(" · "))}</span>
      <span>${esc(param.desc)}</span>
    </li>`;
}

function toolCard(tool) {
  return `
    <article class="tool-card">
      <header>
        <h3>${esc(tool.name)}</h3>
        ${tool.read_only ? '<span class="chip chip-green">只读</span>' : '<span class="chip chip-red">可写</span>'}
      </header>
      <p class="muted">${esc(tool.purpose)}</p>
      <dl class="spec-list">
        <div><dt>超时</dt><dd>${tool.timeout_s}s</dd></div>
        <div><dt>重试</dt><dd>${tool.max_retries} 次</dd></div>
        <div><dt>失败降级</dt><dd>${tool.fallback ? esc(tool.fallback) : "无（转人工）"}</dd></div>
        <div><dt>单次成本</dt><dd>${tool.cost_per_call}</dd></div>
      </dl>
      <h4>参数</h4>
      <ul class="param-list">${tool.params.map(paramRow).join("")}</ul>
      <h4>返回</h4>
      <p class="muted">${esc(tool.returns)}</p>
    </article>`;
}

async function boot() {
  const head = document.getElementById("tools-head");
  const grid = document.getElementById("tool-grid");
  try {
    const data = await api("/api/tools");
    const corpus = data.corpus;
    head.innerHTML = `
      <div class="task-headline">
        <h1>工具管理</h1>
        <p class="muted">
          这一页是给 Agent 平台使用者看的：每个工具的参数、超时、重试、降级与成本都登记在案，
          运行时的每一步调用都要经过这里的校验。
        </p>
        <p class="muted">
          语料：<code>${esc(corpus.path)}</code>　版本 ${esc(corpus.version)}　
          ${corpus.sources} 份资料　${corpus.facts} 条事实　实体：${esc(corpus.entities.join("、"))}
        </p>
        <p class="note-line">语料是离线受控的，刻意包含过时资料、互相冲突的复测结果与低等级内容——
          否则"自主检索"会退化成固定查找。</p>
      </div>`;
    grid.innerHTML = data.tools.map(toolCard).join("");
  } catch (error) {
    head.innerHTML = '<p class="muted">读取工具清单失败：' + esc(error.message) + "</p>";
  }
}

boot();
