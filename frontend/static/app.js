const API = "";

let state = {
  page: 1,
  pageSize: 25,
  filters: { status: "", system_label: "", discrepancy_type: "", search: "" },
  chatHistory: [],
};

let routingChart, exceptionChart;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
window.addEventListener("DOMContentLoaded", async () => {
  await checkHealth();
  await tryLoadDashboard();
  wireEvents();
});

async function checkHealth() {
  try {
    const res = await fetch(`${API}/api/health`);
    const data = await res.json();
    const badge = document.getElementById("llmBadge");
    if (data.llm_configured) {
      badge.textContent = `LLM: ${data.llm_model}`;
      badge.className = "badge badge-on";
    } else {
      badge.textContent = "LLM: offline (rule-based fallback)";
      badge.className = "badge badge-off";
    }
  } catch (e) {
    console.error("health check failed", e);
  }
}

async function tryLoadDashboard() {
  // Step 1 (critical): fetch the report. If this fails, we genuinely have no
  // data yet -> show the empty state. Nothing below this point should be able
  // to fall back into showEmptyState(): a cosmetic rendering hiccup (a chart
  // failing to resize, etc.) is not the same as "no results yet" and must
  // never hide data that's already successfully loaded.
  let report;
  try {
    const res = await fetch(`${API}/api/report`);
    if (!res.ok) {
      showEmptyState();
      return;
    }
    report = await res.json();
  } catch (e) {
    console.error("Failed to fetch report:", e);
    showEmptyState();
    return;
  }

  // Un-hide the dashboard BEFORE drawing charts. Chart.js measures the
  // canvas's rendered size at construction time; if the parent still has
  // display:none, it initializes against a 0x0 box and never recovers on
  // its own. Making the container visible first, then resizing after,
  // avoids blank/broken charts on the very first render.
  document.getElementById("dashboard").classList.remove("hidden");
  document.getElementById("emptyState").classList.add("hidden");

  // Step 2 (best-effort): render everything. Data is already visible/safe at
  // this point, so any error here is logged, not treated as "no data".
  try {
    renderReport(report);
    await loadTransactions();
    if (routingChart) routingChart.resize();
    if (exceptionChart) exceptionChart.resize();
  } catch (e) {
    console.error("Dashboard loaded but a rendering step failed:", e);
  }
}

function showEmptyState() {
  document.getElementById("dashboard").classList.add("hidden");
  document.getElementById("emptyState").classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Pipeline run
// ---------------------------------------------------------------------------
function wireEvents() {
  document.getElementById("runBtn").addEventListener("click", runPipeline);
  document.getElementById("searchInput").addEventListener("input", debounce(onFilterChange, 350));
  document.getElementById("statusFilter").addEventListener("change", onFilterChange);
  document.getElementById("labelFilter").addEventListener("change", onFilterChange);
  document.getElementById("typeFilter").addEventListener("change", onFilterChange);
  document.getElementById("prevPage").addEventListener("click", () => changePage(-1));
  document.getElementById("nextPage").addEventListener("click", () => changePage(1));
  document.getElementById("closeModal").addEventListener("click", closeModal);
  document.getElementById("evidenceModal").addEventListener("click", (e) => {
    if (e.target.id === "evidenceModal") closeModal();
  });

  document.getElementById("chatToggle").addEventListener("click", () => {
    document.getElementById("chatPanel").classList.toggle("hidden");
  });
  document.getElementById("chatClose").addEventListener("click", () => {
    document.getElementById("chatPanel").classList.add("hidden");
  });
  document.getElementById("chatForm").addEventListener("submit", onChatSubmit);
}

async function runPipeline() {
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    await fetch(`${API}/api/pipeline/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ regenerate: false, n_records: 1000 }),
    });
    await pollStatus();
    await tryLoadDashboard();
  } catch (e) {
    alert("Pipeline run failed: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run pipeline";
  }
}

async function pollStatus() {
  while (true) {
    const res = await fetch(`${API}/api/pipeline/status`);
    const status = await res.json();
    if (status.error) throw new Error(status.error);
    document.getElementById("runBtn").textContent = status.running
      ? `Running… ${status.stage || ""} (${status.pct || 0}%)`
      : "Run pipeline";
    if (!status.running) return;
    await new Promise((r) => setTimeout(r, 500));
  }
}

// ---------------------------------------------------------------------------
// Report rendering
// ---------------------------------------------------------------------------
function renderReport(report) {
  const rl = report.record_level;
  const al = report.amount_level;
  const acc = report.accuracy;
  const tp = report.throughput;

  document.getElementById("mRecords").textContent = rl.records_processed;
  document.getElementById("mResolution").textContent = pct(rl.resolution_rate);
  document.getElementById("mAccuracy").textContent = pct(acc.accuracy);
  document.getElementById("mFalseRes").textContent = pct(acc.false_resolution_rate, 2);
  document.getElementById("mThroughput").textContent = `${tp.records_per_second ?? "–"} rec/s`;

  document.getElementById("mAmtProcessed").textContent = money(al.total_amount_processed);
  document.getElementById("mAmtReconciled").textContent = `${money(al.amount_reconciled)} (${pct(al.pct_amount_reconciled)})`;
  document.getElementById("mAmtUnresolved").textContent = money(al.amount_unresolved);

  // Each of these is independently wrapped: if Chart.js failed to load (blocked
  // CDN, offline preview, etc.) or one panel throws for any other reason, that
  // failure must not prevent the other two panels from rendering. Previously
  // these three calls were sequential and unguarded inside a single try/catch
  // in tryLoadDashboard(), so one throw silently skipped everything after it.
  safeRender("routing chart", () => renderRoutingChart(rl));
  safeRender("exception chart", () => renderExceptionChart(report.exception_breakdown || []));
  safeRender("risk table", () => renderRiskTable(report.merchant_risk_ranking || []));
  safeRender("type filter", () => populateTypeFilter(report.exception_breakdown || []));
}

function safeRender(label, fn) {
  try {
    fn();
  } catch (e) {
    console.error(`Failed to render ${label}:`, e);
    if (typeof Chart === "undefined" && (label === "routing chart" || label === "exception chart")) {
      const panelId = label === "routing chart" ? "routingChart" : "exceptionChart";
      const canvas = document.getElementById(panelId);
      if (canvas && canvas.parentElement) {
        canvas.parentElement.insertAdjacentHTML(
          "beforeend",
          `<p class="chart-error">Chart.js failed to load (check network/CDN access). Data itself is fine — see the Transactions table below.</p>`
        );
      }
    }
  }
}

function renderRoutingChart(rl) {
  const ctx = document.getElementById("routingChart");
  const data = {
    labels: ["Auto-resolved", "AI review", "Human review"],
    datasets: [{
      data: [rl.auto_resolved, rl.ai_review, rl.human_review],
      backgroundColor: ["#34d399", "#fbbf24", "#f87171"],
      borderRadius: 6,
    }],
  };
  if (routingChart) { routingChart.data = data; routingChart.update(); return; }
  routingChart = new Chart(ctx, {
    type: "bar",
    data,
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
  });
}

function renderExceptionChart(breakdown) {
  const ctx = document.getElementById("exceptionChart");
  const data = {
    labels: breakdown.map((b) => b.discrepancy_type),
    datasets: [{
      data: breakdown.map((b) => b.count),
      backgroundColor: "#5b8cff",
      borderRadius: 6,
    }],
  };
  if (exceptionChart) { exceptionChart.data = data; exceptionChart.update(); return; }
  exceptionChart = new Chart(ctx, {
    type: "bar",
    data,
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true } },
    },
  });
}

function renderRiskTable(risk) {
  const tbody = document.querySelector("#riskTable tbody");
  tbody.innerHTML = "";
  if (!risk.length) {
    tbody.innerHTML = `<tr><td colspan="3">No exceptions in this batch.</td></tr>`;
    return;
  }
  risk.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.merchant_id}</td><td>${r.exception_count}</td><td>${money(r.exception_amount)}</td>`;
    tbody.appendChild(tr);
  });
}

function populateTypeFilter(breakdown) {
  const sel = document.getElementById("typeFilter");
  const current = sel.value;
  sel.innerHTML = `<option value="">All discrepancy types</option>`;
  breakdown.forEach((b) => {
    const opt = document.createElement("option");
    opt.value = b.discrepancy_type;
    opt.textContent = `${b.discrepancy_type} (${b.count})`;
    sel.appendChild(opt);
  });
  sel.value = current;
}

// ---------------------------------------------------------------------------
// Transactions table
// ---------------------------------------------------------------------------
function onFilterChange() {
  state.filters.search = document.getElementById("searchInput").value.trim();
  state.filters.status = document.getElementById("statusFilter").value;
  state.filters.system_label = document.getElementById("labelFilter").value;
  state.filters.discrepancy_type = document.getElementById("typeFilter").value;
  state.page = 1;
  loadTransactions();
}

function changePage(delta) {
  state.page = Math.max(1, state.page + delta);
  loadTransactions();
}

async function loadTransactions() {
  const params = new URLSearchParams({
    page: state.page,
    page_size: state.pageSize,
  });
  if (state.filters.search) params.set("search", state.filters.search);
  if (state.filters.status) params.set("status", state.filters.status);
  if (state.filters.system_label) params.set("system_label", state.filters.system_label);
  if (state.filters.discrepancy_type) params.set("discrepancy_type", state.filters.discrepancy_type);

  const res = await fetch(`${API}/api/transactions?${params.toString()}`);
  const data = await res.json();
  renderTransactions(data);
}

function renderTransactions(data) {
  const tbody = document.querySelector("#txnTable tbody");
  tbody.innerHTML = "";
  data.results.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.transaction_id}</td>
      <td>${row.merchant_id ?? "–"}</td>
      <td>${money(row.amount)}</td>
      <td>${row.discrepancy_type}</td>
      <td><span class="label-pill ${row.system_label === "MATCH" ? "label-match" : "label-exception"}">${row.system_label}</span></td>
      <td><span class="status-pill status-${row.status}">${row.status.replace("_", " ")}</span></td>
      <td>${row.confidence?.toFixed ? row.confidence.toFixed(1) : row.confidence}%</td>
      <td><button class="btn btn-secondary why-btn" data-txn="${row.transaction_id}">Why?</button></td>
    `;
    tbody.appendChild(tr);
  });
  document.getElementById("pageInfo").textContent =
    `Page ${data.page} of ${Math.max(1, Math.ceil(data.total / data.page_size))} (${data.total} total)`;

  document.querySelectorAll(".why-btn").forEach((btn) => {
    btn.addEventListener("click", () => openEvidence(btn.dataset.txn));
  });
}

// ---------------------------------------------------------------------------
// Evidence modal ("Why?")
// ---------------------------------------------------------------------------
async function openEvidence(txnId) {
  const res = await fetch(`${API}/api/transactions/${encodeURIComponent(txnId)}`);
  if (!res.ok) return;
  const data = await res.json();
  const { decision, audit } = data;
  const ev = audit ? audit.evidence : {};

  document.getElementById("evTitle").textContent =
    `${decision.transaction_id} — ${decision.system_label} / ${decision.status}`;

  const badgeClass = decision.status === "AUTO_RESOLVED" ? "label-match" :
    decision.status === "HUMAN_REVIEW" ? "label-exception" : "label-match";

  document.getElementById("evBody").innerHTML = `
    <div class="ev-section">
      <h4>Decision</h4>
      <div class="ev-grid">
        <div>Discrepancy type</div><div>${decision.discrepancy_type}</div>
        <div>Confidence</div><div>${decision.confidence}%</div>
        <div>Merchant</div><div>${decision.merchant_id ?? "–"}</div>
        <div>Amount</div><div>${money(decision.amount)}</div>
      </div>
    </div>
    <div class="ev-section">
      <h4>Reason</h4>
      <div class="ev-reason">${decision.reason ?? "–"}</div>
    </div>
    ${ev.payment ? evSection("Payment", ev.payment) : ""}
    ${ev.settlement ? evSection("Settlement", ev.settlement) : ""}
    ${ev.bank ? evSection("Bank statement", ev.bank) : ""}
    ${ev.ledger ? evSection("Merchant ledger", ev.ledger) : ""}
    ${ev.expected ? evSection("Expected (recomputed)", ev.expected) : ""}
    <div class="ev-section">
      <h4>Decided by</h4>
      <div class="ev-reason">${audit ? audit.agent : "–"} at ${audit ? audit.timestamp : "–"}</div>
      ${audit && audit.investigation_method ? `
        <div class="ev-grid">
          <div>Investigation method</div><div>${audit.investigation_method}</div>
          <div>Reason</div><div>${audit.investigation_reason ?? "–"}</div>
        </div>
      ` : ""}
    </div>
  `;

  document.getElementById("evidenceModal").classList.remove("hidden");
}

function evSection(title, obj) {
  const rows = Object.entries(obj)
    .map(([k, v]) => `<div>${k}</div><div>${v ?? "–"}</div>`)
    .join("");
  return `<div class="ev-section"><h4>${title}</h4><div class="ev-grid">${rows}</div></div>`;
}

function closeModal() {
  document.getElementById("evidenceModal").classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Chatbot
// ---------------------------------------------------------------------------
async function onChatSubmit(e) {
  e.preventDefault();
  const input = document.getElementById("chatInput");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendChatMessage(message, "user");

  const thinkingEl = appendChatMessage("Thinking…", "bot");
  try {
    const res = await fetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: state.chatHistory.slice(-8) }),
    });
    const data = await res.json();
    renderChatContent(thinkingEl, data.reply);
    state.chatHistory.push({ role: "user", content: message });
    state.chatHistory.push({ role: "assistant", content: data.reply });
  } catch (err) {
    renderChatContent(thinkingEl, "Sorry, something went wrong reaching the assistant.");
  }
}

function appendChatMessage(text, who) {
  const wrap = document.getElementById("chatMessages");
  const div = document.createElement("div");
  div.className = `chat-msg chat-msg-${who}`;
  renderChatContent(div, text);
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  return div;
}

// Renders bot replies as formatted HTML (bold, italics, bullet lists)
// instead of dumping raw "**"/"-" characters, so it reads like a normal
// chat UI. No external library/CDN dependency -- works fully offline.
function renderChatContent(div, text) {
  div._rawText = text;
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  const lines = escaped.split("\n");
  let html = "";
  let inList = false;
  for (const rawLine of lines) {
    const line = rawLine.trim();
    const isBullet = /^[-*]\s+/.test(line);
    if (isBullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inlineMd(line.replace(/^[-*]\s+/, ""))}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line === "") continue;
      html += `<p>${inlineMd(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  div.innerHTML = html || escaped;
}

function inlineMd(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function pct(x, digits = 1) {
  if (x === null || x === undefined) return "–";
  return `${(x * 100).toFixed(digits)}%`;
}

function money(x) {
  if (x === null || x === undefined) return "–";
  return `Rs. ${Number(x).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}