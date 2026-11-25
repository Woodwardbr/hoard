// --- Privacy mode ------------------------------------------------------------
// Set server-side from the hoard_privacy cookie (no flash of real numbers). Amounts
// are masked; percentages and chart shapes stay real so distributions still show.
const PRIVACY = document.documentElement.classList.contains("privacy");
const MASK = "$•••••";

const fmt = (n) =>
  PRIVACY ? MASK : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n || 0);
// Non-currency amounts (share quantities etc.).
const maskNum = (text) => (PRIVACY ? "•••" : text);

document.getElementById("quit-btn")?.addEventListener("click", async () => {
  try {
    await api("/api/shutdown", { method: "POST" });
  } catch {}
  document.body.innerHTML =
    '<main class="container" style="max-width:640px;margin:4rem auto;text-align:center">' +
    "<h2>Hoard has stopped</h2>" +
    '<p class="muted">Your data is saved. You can close this tab. To open Hoard again, use its shortcut.</p>' +
    "</main>";
});

document.getElementById("privacy-btn")?.addEventListener("click", () => {
  const next = PRIVACY ? "0" : "1";
  document.cookie = `hoard_privacy=${next}; path=/; max-age=31536000; SameSite=Lax`;
  location.reload();
});

if (PRIVACY && window.Chart) {
  // Every chart is created with `new Chart(...)`; wrap the constructor so each
  // one hides money axes and tooltips without touching the individual configs.
  const BaseChart = window.Chart;
  const isCircular = (type) => ["doughnut", "pie", "polarArea"].includes(type);
  const privatize = (cfg) => {
    const opts = (cfg.options ??= {});
    const plugins = (opts.plugins ??= {});
    const tooltip = (plugins.tooltip ??= {});
    const callbacks = (tooltip.callbacks ??= {});
    if (isCircular(cfg.type)) {
      callbacks.label = (c) => {
        const total = c.dataset.data.reduce((s, v) => s + (Number(v) || 0), 0);
        const pct = total ? ((Number(c.raw) || 0) / total) * 100 : 0;
        return `${c.label}: ${pct.toFixed(1)}%`;
      };
      return cfg;
    }
    callbacks.label = (c) => `${c.dataset.label ? c.dataset.label + ": " : ""}${MASK}`;
    const valueAxis = opts.indexAxis === "y" ? "x" : "y";
    const scales = (opts.scales ??= {});
    const axis = (scales[valueAxis] ??= {});
    axis.ticks = { ...(axis.ticks || {}), callback: () => "•••" };
    return cfg;
  };
  window.Chart = class extends BaseChart {
    constructor(item, cfg) {
      super(item, privatize(cfg));
    }
  };
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    // Server errors are JSON {detail, error_code}; show the readable part.
    let detail = text;
    try {
      const body = JSON.parse(text);
      if (typeof body.detail === "string") detail = body.detail;
    } catch {}
    const err = new Error(detail || `${path}: ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
}

// --- Plaid Link flow -------------------------------------------------------
// Opens Plaid Link for a new bank. Resolves to the institution name once the
// bank is linked and its first sync is done, or null if the user closed Link.
async function linkNewBank() {
  const { link_token } = await api("/api/link-token");
  return new Promise((resolve, reject) => {
    const handler = Plaid.create({
      token: link_token,
      onSuccess: async (publicToken, metadata) => {
        try {
          const institution = metadata?.institution?.name;
          await api("/api/exchange-public-token", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ public_token: publicToken, institution }),
          });
          resolve(institution || "your bank");
        } catch (err) {
          reject(err);
        }
      },
      onExit: (err) => (err ? reject(new Error(err.display_message || err.error_message || "Plaid Link closed")) : resolve(null)),
    });
    handler.open();
  });
}

document.getElementById("link-btn")?.addEventListener("click", async () => {
  try {
    if (await linkNewBank()) location.reload();
  } catch (err) {
    alert("Link failed: " + err.message);
  }
});

// Re-authenticate an existing item with Plaid Link update mode. The item keeps
// its item_id and access token, so there's no public-token exchange — just a
// re-sync once the user finishes logging in.
async function reconnectItem(itemId) {
  const { link_token } = await api(`/api/link-token?item_id=${encodeURIComponent(itemId)}`);
  return new Promise((resolve, reject) => {
    const handler = Plaid.create({
      token: link_token,
      onSuccess: async () => {
        try {
          await api(`/api/items/${encodeURIComponent(itemId)}/sync`, { method: "POST" });
          resolve(true);
        } catch (err) {
          reject(err);
        }
      },
      onExit: (err) => (err ? reject(new Error(err.display_message || err.error_message)) : resolve(false)),
    });
    handler.open();
  });
}

document.getElementById("sync-btn")?.addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Syncing…";
  try {
    const counts = await api("/api/sync", { method: "POST" });
    let msg = `Synced ${counts.accounts} accounts, ${counts.transactions} transactions.`;
    const errors = counts.errors || [];
    if (errors.length) {
      msg += "\n\nThese connections failed:\n" + errors
        .map((er) => `• ${er.institution || er.item_id}: ${er.message}`)
        .join("\n");
    }
    alert(msg);
    for (const er of errors.filter((x) => x.error_code === "ITEM_LOGIN_REQUIRED")) {
      if (!confirm(`${er.institution || "This connection"} needs you to log in again. Reconnect now?`)) continue;
      try {
        await reconnectItem(er.item_id);
      } catch (err) {
        alert(`Reconnect failed for ${er.institution || er.item_id}: ${err.message}`);
      }
    }
    location.reload();
  } catch (err) {
    alert("Sync failed: " + err.message);
  } finally {
    e.target.disabled = false;
    e.target.textContent = "Sync";
  }
});

// --- Dashboard charts ------------------------------------------------------
async function loadDashboard() {
  if (!document.getElementById("net-worth-chart")) return;

  const [netWorth, spending] = await Promise.all([api("/api/net-worth"), api("/api/spending")]);

  const latest = netWorth[netWorth.length - 1] || { assets: 0, liabilities: 0, credit_cards: 0, loans: 0, net: 0, potential_net: 0 };
  document.getElementById("net-worth-value").textContent = fmt(latest.net);
  document.getElementById("potential-net-worth-value").textContent = fmt(latest.potential_net);
  document.getElementById("assets-value").textContent = fmt(latest.assets);
  document.getElementById("liabilities-value").textContent = fmt(latest.liabilities);
  document.getElementById("credit-cards-value").textContent = fmt(latest.credit_cards);
  document.getElementById("loans-value").textContent = fmt(latest.loans);
  const monthSpend = spending.reduce((s, c) => s + c.total, 0);
  document.getElementById("month-spend-value").textContent = fmt(monthSpend);

  new Chart(document.getElementById("net-worth-chart"), {
    type: "line",
    data: {
      datasets: [
        { label: "Net", data: datedPoints(netWorth, "net"), borderColor: "#4f9eff", tension: 0.25, fill: true, backgroundColor: "rgba(79,158,255,0.15)" },
        { label: "Potential Net", data: datedPoints(netWorth, "potential_net"), borderColor: "#a371f7", borderDash: [6, 4], tension: 0.25 },
        { label: "Assets", data: datedPoints(netWorth, "assets"), borderColor: "#2ea043", tension: 0.25 },
        { label: "Liabilities", data: datedPoints(netWorth, "liabilities"), borderColor: "#f85149", tension: 0.25 },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e6edf3" } }, tooltip: { callbacks: { title: dateTooltipTitle } } }, scales: monthScales() },
  });

  new Chart(document.getElementById("spending-chart"), {
    type: "doughnut",
    data: {
      labels: spending.map((s) => s.category),
      datasets: [{ data: spending.map((s) => s.total), backgroundColor: palette(spending.length) }],
    },
    options: { responsive: true, plugins: { legend: { position: "right", labels: { color: "#e6edf3" } } } },
  });

  // Loans and credit cards differ in scale by ~40x, so each gets its own chart
  // and axis rather than sharing one where card changes would be invisible.
  liabilityChart("loans-chart", netWorth, "loans", "#d29922", "rgba(210,153,34,0.15)");
  liabilityChart("credit-cards-chart", netWorth, "credit_cards", "#f85149", "rgba(248,81,73,0.15)");

  new Chart(document.getElementById("net-worth-trend-chart"), {
    type: "line",
    data: {
      datasets: [
        { label: "Net Worth", data: datedPoints(netWorth, "net"), borderColor: "#4f9eff", tension: 0.25, fill: true, backgroundColor: "rgba(79,158,255,0.15)" },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e6edf3" } }, tooltip: { callbacks: { title: dateTooltipTitle } } }, scales: monthScales() },
  });
}

function gridScales() {
  const grid = { color: "rgba(255,255,255,0.08)" };
  const ticks = { color: "#8b98a5" };
  return { x: { grid, ticks }, y: { grid, ticks } };
}

function liabilityChart(id, netWorth, field, color, fill) {
  new Chart(document.getElementById(id), {
    type: "line",
    data: {
      datasets: [{ data: datedPoints(netWorth, field), borderColor: color, borderWidth: 2, pointRadius: 0, tension: 0.25, fill: true, backgroundColor: fill }],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: dateTooltipTitle, label: (c) => fmt(c.parsed.y) } } },
      scales: monthScales(),
    },
  });
}

// Dated line charts plot x as a UTC timestamp (ISO dates parse as UTC) so
// spacing follows the calendar, not how many snapshots fell in each month.
const datedPoints = (series, field) => series.map((p) => ({ x: Date.parse(p.date), y: p[field] }));
const fmtDate = (ms, opts) => new Date(ms).toLocaleDateString("en-US", { timeZone: "UTC", ...opts });
const dateTooltipTitle = (items) =>
  items.length ? fmtDate(items[0].parsed.x, { month: "short", day: "numeric", year: "numeric" }) : "";

// Grid scales for a datedPoints chart: one tick on the 1st of each month,
// labelled "Jul" (with the year when the data spans more than one year).
function monthScales() {
  const scales = gridScales();
  scales.x = {
    ...scales.x,
    type: "linear",
    afterBuildTicks: (axis) => {
      const ticks = [];
      const d = new Date(axis.min);
      let month = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
      while (month <= axis.max) {
        ticks.push({ value: month });
        const m = new Date(month);
        month = Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + 1, 1);
      }
      axis.ticks = ticks.length ? ticks : [{ value: axis.min }];
    },
    ticks: {
      ...scales.x.ticks,
      maxRotation: 0,
      callback: function (value) {
        const multiYear = new Date(this.min).getUTCFullYear() !== new Date(this.max).getUTCFullYear();
        return fmtDate(value, multiYear ? { month: "short", year: "numeric" } : { month: "short" });
      },
    },
  };
  return scales;
}

function palette(n) {
  const colors = ["#4f9eff", "#2ea043", "#f85149", "#d29922", "#a371f7", "#39c5cf", "#db6d28", "#58a6ff"];
  return Array.from({ length: n }, (_, i) => colors[i % colors.length]);
}

// --- Budgets page ----------------------------------------------------------
let spendingCategoriesChart = null;
let currentSpendingCategories = [];
let currentPeriod = "";

async function loadBudgets() {
  if (!document.getElementById("spending-categories-chart")) return;

  // Populate the period dropdown once (default = current month).
  await loadPeriodSelect();

  const [progress, categories] = await Promise.all([
    api(`/api/budgets${currentPeriod ? `?period=${encodeURIComponent(currentPeriod)}` : ""}`),
    api(`/api/spending-categories${currentPeriod ? `?period=${encodeURIComponent(currentPeriod)}` : ""}`),
  ]);
  currentSpendingCategories = categories;

  updateBudgetHeading();
  renderSpendingCategories(categories);
  renderBudgetProgress(progress);
}

async function loadPeriodSelect() {
  const sel = document.getElementById("budget-period-select");
  if (!sel) return;
  if (sel.dataset.loaded === "1") {
    sel.value = currentPeriod || sel.value;
    return;
  }
  const months = await api("/api/spending-months");
  sel.innerHTML =
    '<option value="avg">Average (all months)</option>' +
    months.map((m) => `<option value="${m.period}">${m.label}</option>`).join("");
  const now = new Date();
  const thisMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const hasThisMonth = months.some((m) => m.period === thisMonth);
  sel.value = currentPeriod || (hasThisMonth ? thisMonth : "avg");
  currentPeriod = sel.value;
  sel.dataset.loaded = "1";
  sel.addEventListener("change", async () => {
    currentPeriod = sel.value;
    await loadBudgets();
  });
}

function updateBudgetHeading() {
  const sel = document.getElementById("budget-period-select");
  const label = sel?.selectedOptions[0]?.textContent || "";
  const heading = document.getElementById("spending-heading");
  const totalLine = document.getElementById("spending-total-line");
  const isAvg = currentPeriod === "avg";
  if (heading) {
    heading.textContent = isAvg ? "Average Monthly Spending" : `${label} Spending`;
  }
  if (totalLine) {
    const total = currentSpendingCategories.reduce((s, c) => s + c.total, 0);
    totalLine.textContent = isAvg ? `Avg/month: ${fmt(total)}` : `Total: ${fmt(total)}`;
  }
}

function renderSpendingCategories(categories) {
  const canvas = document.getElementById("spending-categories-chart");
  const tbody = document.querySelector("#spending-categories-table tbody");
  if (!canvas || !tbody) return;

  if (spendingCategoriesChart) spendingCategoriesChart.destroy();
  spendingCategoriesChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: categories.map((c) => c.category),
      datasets: [{ data: categories.map((c) => c.total), backgroundColor: palette(categories.length) }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "right", labels: { color: "#e6edf3", boxWidth: 12, font: { size: 11 } } } },
    },
  });

  tbody.innerHTML = categories
    .map(
      (c, i) => `
      <tr data-category-index="${i}">
        <td>${escapeHtml(c.category)}</td>
        <td class="num">${fmt(c.total)}</td>
        <td class="num">${c.count}</td>
        <td><button class="btn btn-sm set-budget-btn" data-set-budget-category="${escapeHtml(c.category)}" data-set-budget-suggested="${Math.ceil(c.total / 50) * 50}">Set budget</button></td>
      </tr>`
    )
    .join("");

  // Click row → load transactions for that category
  document.querySelectorAll("#spending-categories-table tbody tr").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".set-budget-btn")) return;
      const idx = parseInt(tr.dataset.categoryIndex, 10);
      const cat = currentSpendingCategories[idx].category;
      document.querySelectorAll("#spending-categories-table tbody tr").forEach((r) => r.classList.remove("active"));
      tr.classList.add("active");
      loadCategoryTransactions(cat);
    });
  });

  // Set-budget buttons
  document.querySelectorAll("[data-set-budget-category]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const form = document.getElementById("budget-form");
      form.elements.category.value = btn.dataset.setBudgetCategory;
      form.elements.monthly_limit.value = btn.dataset.setBudgetSuggested;
      form.elements.monthly_limit.focus();
    });
  });
}

async function loadCategoryTransactions(category) {
  const panel = document.getElementById("category-transactions-panel");
  const title = document.getElementById("category-transactions-title");
  const body = document.getElementById("category-transactions-body");
  if (!panel || !body) return;

  title.textContent = `Transactions in "${category}"`;
  body.innerHTML = '<tr><td colspan="5" class="muted">Loading…</td></tr>';
  panel.hidden = false;
  if (currentPeriod === "avg") {
    // The row shows a monthly average; the list is every month, so say which.
    api("/api/spending-average-period")
      .then((info) => { title.textContent = `Transactions in "${category}" (${info.label})`; })
      .catch(() => {});
  }

  try {
    const txns = await api(
      `/api/spending-categories/${encodeURIComponent(category)}/transactions` +
        (currentPeriod ? `?period=${encodeURIComponent(currentPeriod)}` : "")
    );
    if (!txns.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">No transactions in this period.</td></tr>';
      return;
    }
    body.innerHTML = txns
      .map(
        (t) => `
        <tr data-txn-id="${t.transaction_id}" data-merchant="${escapeHtml(t.merchant || t.name)}">
          <td>${t.date}</td>
          <td>${escapeHtml(t.merchant || t.name)}</td>
          <td class="muted">${escapeHtml(t.account_name || "")}</td>
          <td class="num">${fmt(t.amount)}</td>
          <td><button class="btn btn-sm reclassify-btn">Reclassify</button></td>
        </tr>`
      )
      .join("");

    body.querySelectorAll(".reclassify-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const tr = btn.closest("tr");
        toggleReclassifyRow(tr, category);
      });
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="5" class="muted">Error: ${escapeHtml(err.message)}</td></tr>`;
  }
}

async function toggleReclassifyRow(tr, currentCategory) {
  // Collapse any other open reclassify row
  document.querySelectorAll(".reclassify-row").forEach((r) => r.remove());
  if (tr.dataset.reclassifyOpen === "1") {
    tr.dataset.reclassifyOpen = "0";
    return;
  }
  tr.dataset.reclassifyOpen = "1";

  const merchant = tr.dataset.merchant;
  const options = await loadCategoryOptions();
  const optHTML = options
    .map((o) => `<option value="${escapeHtml(o)}" ${o === currentCategory ? "selected" : ""}>${escapeHtml(o)}</option>`)
    .join("");

  const newRow = document.createElement("tr");
  newRow.className = "reclassify-row";
  newRow.innerHTML = `
    <td colspan="5">
      <div class="reclassify-form">
        <select class="reclassify-select">${optHTML}</select>
        <button class="btn btn-sm" data-action="txn">Just this transaction</button>
        <button class="btn btn-sm" data-action="vendor">All from "${escapeHtml(merchant)}"</button>
        <button class="btn btn-sm btn-secondary" data-action="cancel">Cancel</button>
      </div>
    </td>`;
  tr.after(newRow);

  newRow.querySelector('[data-action="cancel"]').addEventListener("click", () => {
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
  });
  newRow.querySelector('[data-action="txn"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api(`/api/transactions/${encodeURIComponent(tr.dataset.txnId)}/category`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ override_category: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadBudgets();
  });
  newRow.querySelector('[data-action="vendor"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api("/api/merchant-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merchant_pattern: merchant.toLowerCase(), target_category: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadBudgets();
  });
}

let _categoryOptionsCache = null;
async function loadCategoryOptions() {
  if (_categoryOptionsCache) return _categoryOptionsCache;
  _categoryOptionsCache = await api("/api/category-options");
  // Populate datalist for the vendor-rule form too
  const dl = document.getElementById("category-options-list");
  if (dl) dl.innerHTML = _categoryOptionsCache.map((o) => `<option value="${escapeHtml(o)}">`).join("");
  return _categoryOptionsCache;
}

function renderBudgetProgress(progress) {
  const list = document.getElementById("budget-progress-list");
  if (!list) return;
  if (!progress.length) {
    list.innerHTML = '<p class="muted">No budgets set yet. Click <strong>Set budget</strong> on a spend row, or fill in the form above.</p>';
    return;
  }
  list.innerHTML = progress
    .map((b) => {
      const pct = Math.min(b.pct_used, 100);
      const over = b.pct_used > 100;
      return `
        <div class="budget-bar ${over ? "over" : ""}">
          <div class="label">
            <span>${escapeHtml(b.category)} <button class="btn btn-sm btn-danger" data-delete-budget="${escapeHtml(b.category)}">Delete</button></span>
            <span>${fmt(b.spent_so_far)} / ${fmt(b.monthly_limit)} (${b.pct_used.toFixed(0)}%)</span>
          </div>
          <div class="track"><div class="fill" style="width:${pct}%"></div></div>
        </div>`;
    })
    .join("");

  document.querySelectorAll("[data-delete-budget]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/budgets/${encodeURIComponent(btn.dataset.deleteBudget)}`, { method: "DELETE" });
      loadBudgets();
    });
  });
}

// --- Merchant rules --------------------------------------------------------
async function loadMerchantRules() {
  const list = document.getElementById("merchant-rules-list");
  if (!list) return;
  const rules = await api("/api/merchant-rules");
  if (!rules.length) {
    list.innerHTML = '<p class="muted">No vendor rules yet.</p>';
    return;
  }
  list.innerHTML = `<table class="accounts-table"><thead><tr><th>Vendor</th><th>Category</th><th></th></tr></thead><tbody>${rules
    .map(
      (r) => `<tr>
        <td>${escapeHtml(r.merchant_pattern)}</td>
        <td>${escapeHtml(r.target_category)}</td>
        <td><button class="btn btn-sm btn-danger" data-delete-rule="${escapeHtml(r.merchant_pattern)}">Delete</button></td>
      </tr>`
    )
    .join("")}</tbody></table>`;

  list.querySelectorAll("[data-delete-rule]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/merchant-rules/${encodeURIComponent(btn.dataset.deleteRule)}`, { method: "DELETE" });
      loadBudgets();
      loadMerchantRules();
    });
  });
}

document.getElementById("budget-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/budgets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category: form.get("category"), monthly_limit: parseFloat(form.get("monthly_limit")) }),
  });
  e.target.reset();
  loadBudgets();
});

document.getElementById("merchant-rule-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/merchant-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      merchant_pattern: form.get("merchant_pattern"),
      target_category: form.get("target_category"),
    }),
  });
  e.target.reset();
  loadBudgets();
  loadMerchantRules();
});

// --- Subscription detection -----------------------------------------------
async function loadSubscriptions() {
  const list = document.getElementById("subscriptions-list");
  if (!list) return;
  const subs = await api("/api/subscriptions");
  if (!subs.length) {
    list.innerHTML = '<p class="muted">No recurring charges detected yet. Sync more transactions to improve detection.</p>';
    return;
  }
  list.innerHTML = `<table class="accounts-table"><thead><tr>
    <th>Merchant</th><th class="num">Amount</th><th class="num">Times</th><th>Last seen</th><th>Category</th>
  </tr></thead><tbody>${subs
    .map(
      (s) => `<tr>
        <td>${escapeHtml(s.label)}</td>
        <td class="num">${fmt(s.amount)}</td>
        <td class="num">${s.occurrences}</td>
        <td>${s.last_date}</td>
        <td class="muted">${escapeHtml(s.category)}</td>
      </tr>`
    )
    .join("")}</tbody></table>`;
}

loadDashboard();
loadBudgets();
loadMerchantRules();
loadCategoryOptions();
loadSubscriptions();

// --- Portfolio page --------------------------------------------------------
let portfolioChart = null;
let currentPortfolioSectors = [];

async function loadPortfolio() {
  const canvas = document.getElementById("portfolio-chart");
  if (!canvas) return;

  const [sectors, options] = await Promise.all([
    api("/api/portfolio"),
    api("/api/sector-options"),
  ]);
  currentPortfolioSectors = sectors;
  _sectorOptionsCache = options;

  const tbody = document.querySelector("#portfolio-sectors-table tbody");
  if (portfolioChart) portfolioChart.destroy();

  portfolioChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: sectors.map((s) => s.sector),
      datasets: [{ data: sectors.map((s) => s.total), backgroundColor: palette(sectors.length) }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "right", labels: { color: "#e6edf3", boxWidth: 12, font: { size: 11 } } } },
    },
  });

  tbody.innerHTML = sectors
    .map(
      (s, i) => `
      <tr data-sector-index="${i}">
        <td>${escapeHtml(s.sector)}</td>
        <td class="num">${fmt(s.total)}</td>
        <td class="num">${s.asset_count}</td>
        <td></td>
      </tr>`
    )
    .join("");

  document.querySelectorAll("#portfolio-sectors-table tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      const idx = parseInt(tr.dataset.sectorIndex, 10);
      const sec = currentPortfolioSectors[idx].sector;
      document.querySelectorAll("#portfolio-sectors-table tbody tr").forEach((r) => r.classList.remove("active"));
      tr.classList.add("active");
      loadSectorAssets(sec);
    });
  });
}

let _sectorOptionsCache = null;
async function loadSectorOptions() {
  if (_sectorOptionsCache) return _sectorOptionsCache;
  _sectorOptionsCache = await api("/api/sector-options");
  return _sectorOptionsCache;
}

async function loadSectorAssets(sector) {
  const panel = document.getElementById("sector-assets-panel");
  const title = document.getElementById("sector-assets-title");
  const body = document.getElementById("sector-assets-body");
  if (!panel || !body) return;

  title.textContent = `Assets in "${sector}"`;
  body.innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
  panel.hidden = false;

  try {
    const assets = await api(`/api/portfolio/${encodeURIComponent(sector)}/assets`);
    if (!assets.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No assets in this sector.</td></tr>';
      return;
    }
    body.innerHTML = assets
      .map(
        (a) => `
        <tr data-account-id="${escapeHtml(a.account_id)}" data-security-id="${escapeHtml(a.security_id || "")}" data-name="${escapeHtml(a.name || a.ticker || "")}">
          <td>${escapeHtml(a.ticker || "—")}</td>
          <td>${escapeHtml(a.name)}</td>
          <td class="muted">${escapeHtml(a.account_name || "")}${a.institution ? ` (${escapeHtml(a.institution)})` : ""}</td>
          <td class="num">${fmt(a.value)}</td>
          <td class="num">${a.quantity != null ? maskNum(a.quantity.toFixed(4)) : "—"}</td>
          <td><button class="btn btn-sm reclassify-sector-btn">Reclassify</button></td>
        </tr>`
      )
      .join("");

    body.querySelectorAll(".reclassify-sector-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const tr = btn.closest("tr");
        toggleReclassifySectorRow(tr, sector);
      });
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="6" class="muted">Error: ${escapeHtml(err.message)}</td></tr>`;
  }
}

async function toggleReclassifySectorRow(tr, currentSector) {
  document.querySelectorAll(".reclassify-sector-row").forEach((r) => r.remove());
  if (tr.dataset.reclassifyOpen === "1") {
    tr.dataset.reclassifyOpen = "0";
    return;
  }
  tr.dataset.reclassifyOpen = "1";

  const securityId = tr.dataset.securityId || null;
  const accountId = tr.dataset.accountId;
  const name = tr.dataset.name;

  if (!securityId) {
    alert(`"${name}" is a cash account and can't be reclassified to another sector.`);
    tr.dataset.reclassifyOpen = "0";
    return;
  }

  const options = await loadSectorOptions();
  const optHTML = options
    .map((o) => `<option value="${escapeHtml(o)}" ${o === currentSector ? "selected" : ""}>${escapeHtml(o)}</option>`)
    .join("");

  const newRow = document.createElement("tr");
  newRow.className = "reclassify-sector-row";
  newRow.innerHTML = `
    <td colspan="6">
      <div class="reclassify-form">
        <select class="reclassify-select">${optHTML}</select>
        <button class="btn btn-sm" data-action="save">Save</button>
        <button class="btn btn-sm btn-secondary" data-action="cancel">Cancel</button>
      </div>
    </td>`;
  tr.after(newRow);

  newRow.querySelector('[data-action="cancel"]').addEventListener("click", () => {
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
  });
  newRow.querySelector('[data-action="save"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api("/api/portfolio/assets/sector", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId, security_id: securityId, sector: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadPortfolio();
  });
}

// Portfolio-specific Plaid Link button (separate from the dashboard's)
document.getElementById("portfolio-link-btn")?.addEventListener("click", async () => {
  try {
    if (await linkNewBank()) location.reload();
  } catch (err) {
    alert("Link failed: " + err.message);
  }
});

// Portfolio item reconnect (Link update mode) buttons
document.querySelectorAll("[data-relink-item]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try {
      if (await reconnectItem(btn.dataset.relinkItem)) location.reload();
    } catch (err) {
      alert("Reconnect failed: " + err.message);
    }
  });
});

// Settings page: sync a single linked item
document.querySelectorAll("[data-sync-item]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
      await api(`/api/items/${encodeURIComponent(btn.dataset.syncItem)}/sync`, { method: "POST" });
      location.reload();
    } catch (err) {
      alert("Sync failed: " + err.message);
      btn.disabled = false;
      btn.textContent = "Sync";
    }
  });
});

// Portfolio item unlink buttons
document.querySelectorAll("[data-unlink-item]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!confirm("Remove this linked item and all its accounts/holdings?")) return;
    await api(`/api/items/${encodeURIComponent(btn.dataset.unlinkItem)}`, { method: "DELETE" });
    location.reload();
  });
});

// --- 401(k) plan allocations ----------------------------------------------
async function loadPlanAllocations() {
  const list = document.getElementById("plan-allocations-list");
  if (!list) return;

  const [allocs, sectors] = await Promise.all([
    api("/api/plan-allocations"),
    api("/api/sector-options"),
  ]);

  // Populate the sector datalist.
  const dl = document.getElementById("sector-options-list");
  if (dl) dl.innerHTML = sectors.map((s) => `<option value="${escapeHtml(s)}">`).join("");

  if (!allocs.length) {
    list.innerHTML = '<p class="muted">No 401(k) allocations yet. Add funds below.</p>';
    return;
  }

  // Group by account.
  const byAccount = {};
  allocs.forEach((a) => {
    if (!byAccount[a.account_id]) byAccount[a.account_id] = [];
    byAccount[a.account_id].push(a);
  });

  list.innerHTML = Object.entries(byAccount)
    .map(([aid, rows]) => {
      const bal = rows[0].balance || 0;
      const totalPct = rows.reduce((s, r) => s + r.allocation_pct, 0);
      return `
        <div class="plan-account">
          <div class="label">
            <strong>${escapeHtml(rows[0].account_name)}</strong>
            <span class="muted">(${escapeHtml(rows[0].institution)}) — Balance: ${fmt(bal)}</span>
          </div>
          <table class="accounts-table">
            <thead><tr><th>Fund</th><th>Ticker</th><th class="num">%</th><th>Sector</th><th class="num">Value</th><th></th></tr></thead>
            <tbody>
              ${rows
                .map(
                  (r) => `<tr data-account-id="${escapeHtml(r.account_id)}" data-label="${escapeHtml(r.label)}">
                    <td>${escapeHtml(r.label)}</td>
                    <td>${escapeHtml(r.ticker || "—")}</td>
                    <td class="num">${r.allocation_pct.toFixed(2)}%</td>
                    <td class="muted">${escapeHtml(r.sector)}</td>
                    <td class="num">${fmt(bal * r.allocation_pct / 100)}</td>
                    <td><button class="btn btn-sm btn-danger" data-delete-alloc='${escapeHtml(JSON.stringify({ account_id: r.account_id, label: r.label }))}'>Delete</button></td>
                  </tr>`
                )
                .join("")}
              <tr class="muted">
                <td colspan="2">Total</td>
                <td class="num">${totalPct.toFixed(2)}%</td>
                <td colspan="2">${totalPct < 100 ? `${(100 - totalPct).toFixed(2)}% unallocated` : ""}</td>
                <td></td>
              </tr>
            </tbody>
          </table>
        </div>`;
    })
    .join("");

  list.querySelectorAll("[data-delete-alloc]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { account_id, label } = JSON.parse(btn.dataset.deleteAlloc);
      await api(`/api/plan-allocations/${encodeURIComponent(account_id)}/${encodeURIComponent(label)}`, {
        method: "DELETE",
      });
      await loadPlanAllocations();
      await loadPortfolio();
    });
  });
}

document.getElementById("plan-allocation-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/plan-allocations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: form.get("account_id"),
      label: form.get("label"),
      ticker: form.get("ticker") || null,
      allocation_pct: parseFloat(form.get("allocation_pct")),
      sector: form.get("sector"),
    }),
  });
  e.target.reset();
  await loadPlanAllocations();
  await loadPortfolio();
});

loadPortfolio();
loadPlanAllocations();

// --- Debt Tracker page -----------------------------------------------------
let debtChart = null;
let debtBalanceChart = null;
let currentLoans = [];

async function loadDebtSummary() {
  if (!document.getElementById("debt-total-balance")) return;
  const s = await api("/api/debt/summary");
  document.getElementById("debt-total-balance").textContent = fmt(s.total_balance);
  document.getElementById("debt-num-loans").textContent = `${s.num_loans} loan${s.num_loans === 1 ? "" : "s"}`;
  document.getElementById("debt-total-min").textContent = fmt(s.total_min_payment);
  document.getElementById("debt-payoff-date").textContent =
    s.projected_payoff_date || "—";
  const monthsNote = s.projected_payoff_months
    ? `${s.projected_payoff_months} months at minimums`
    : (s.unprojectable.length ? "enter rate + balance + min" : "at minimums only");
  document.getElementById("debt-payoff-months").textContent = monthsNote;
  document.getElementById("debt-projected-interest").textContent =
    s.projected_total_interest != null ? fmt(s.projected_total_interest) : "—";
}

function populateLoanSelects() {
  const selects = [
    document.querySelector('form#loan-payment-form select[name="loan_id"]'),
  ].filter(Boolean);
  for (const sel of selects) {
    const cur = sel.value;
    sel.innerHTML = '<option value="">Combined (all loans)</option>' +
      currentLoans
        .map((l) => `<option value="${escapeHtml(l.loan_id)}">${escapeHtml(l.name)}</option>`)
        .join("");
    if (cur) sel.value = cur;
  }
}

function renderLoansTable() {
  const tbody = document.querySelector("#debt-loans-table tbody");
  if (!tbody) return;
  tbody.innerHTML = currentLoans.map((l) => `
    <tr>
      <td>${escapeHtml(l.name)}${l.notes ? `<br><small class="muted">${escapeHtml(l.notes)}</small>` : ""}</td>
      <td>${escapeHtml(l.loan_type || "—")}</td>
      <td>${l.interest_rate != null ? `${l.interest_rate}%` : "—"}</td>
      <td class="num">${l.min_payment != null ? fmt(l.min_payment) : "—"}</td>
      <td class="num negative">${l.current_balance != null ? fmt(l.current_balance) : "—"}</td>
      <td>${l.next_due_date || "—"}</td>
      <td>${escapeHtml(l.status)}</td>
      <td>${l.auto_pay ? "Yes" : "No"}</td>
      <td>
        <button class="btn btn-sm btn-secondary" data-edit-loan="${l.loan_id}">Edit</button>
        <button class="btn btn-sm btn-danger" data-del-loan="${l.loan_id}">Del</button>
      </td>
    </tr>
  `).join("");
}

async function loadLoans() {
  if (!document.getElementById("debt-loans-table")) return;
  currentLoans = await api("/api/loans");
  renderLoansTable();
  populateLoanSelects();
}

async function loadLoanPayments() {
  const tbody = document.querySelector("#debt-payments-table tbody");
  if (!tbody) return;
  const payments = await api("/api/loan-payments");
  const loanName = (id) => (id ? (currentLoans.find((l) => l.loan_id === id)?.name || "—") : "Combined");
  tbody.innerHTML = payments.map((p) => `
    <tr>
      <td>${p.payment_date}</td>
      <td class="num">${fmt(p.amount)}</td>
      <td>${escapeHtml(loanName(p.loan_id))}</td>
      <td>${escapeHtml(p.status)}</td>
      <td>${escapeHtml(p.source || "—")}</td>
      <td><button class="btn btn-sm btn-danger" data-del-payment="${p.payment_id}">Del</button></td>
    </tr>
  `).join("");
}

async function loadDebtBalanceChart() {
  const canvas = document.getElementById("debt-balance-chart");
  if (!canvas) return;
  const history = await api("/api/debt/balance-history");
  const labels = history.map((h) => h.date);
  const data = history.map((h) => h.total);
  if (debtBalanceChart) debtBalanceChart.destroy();
  debtBalanceChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Total balance",
          data,
          borderColor: "#4f9eff",
          backgroundColor: "rgba(79,158,255,0.1)",
          fill: true,
          tension: 0.2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#e6edf3" } } },
      scales: gridScales(),
    },
  });
}

async function loadDebtChart(extraMonthly, extraOnetime, strategy) {
  const canvas = document.getElementById("debt-chart");
  if (!canvas) return;
  const [history, projection] = await Promise.all([
    api("/api/debt/balance-history"),
    api(
      `/api/debt/projection?extra_monthly=${encodeURIComponent(extraMonthly || 0)}` +
        `&extra_onetime=${encodeURIComponent(extraOnetime || 0)}` +
        `&strategy=${encodeURIComponent(strategy || "distributed")}`
    ),
  ]);

  const histLabels = history.map((h) => h.date);
  const histData = history.map((h) => h.total);

  // Project forward from the last historical point (or today if none).
  const projLabels = projection.schedule.map((p) => p.date);
  const projData = projection.schedule.map((p) => p.total_balance);

  // Bridge: prepend the last actual point so the dashed line connects.
  let bridgeLabel = null, bridgeVal = null;
  if (history.length && projLabels.length) {
    bridgeLabel = history[history.length - 1].date;
    bridgeVal = history[history.length - 1].total;
  } else if (projLabels.length) {
    bridgeLabel = projLabels[0];
    bridgeVal = projData[0];
  }

  const labels = histLabels.concat(projLabels);
  const histSeries = histData.concat(Array(projLabels.length).fill(null));
  const projSeries = Array(histLabels.length - (bridgeLabel ? 1 : 0))
    .fill(null)
    .concat(bridgeVal ? [bridgeVal] : [])
    .concat(projData);

  if (debtChart) debtChart.destroy();
  debtChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Actual balance",
          data: histSeries,
          borderColor: "#4f9eff",
          backgroundColor: "rgba(79,158,255,0.1)",
          fill: true,
          tension: 0.2,
        },
        {
          label: "Projected payoff",
          data: projSeries,
          borderColor: "#2ea043",
          borderDash: [6, 4],
          fill: false,
          tension: 0.2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#e6edf3" } } },
      scales: {
        x: {
          grid: { color: "rgba(255,255,255,0.08)" },
          ticks: {
            color: "#8b98a5",
            autoSkip: false,
            callback: function (value, index) {
              const lbl = this.getLabelForValue(value) || "";
              const yr = parseInt(lbl.slice(0, 4), 10);
              if (Number.isNaN(yr)) return "";
              if (index === 0) return String(yr);       // leftmost = this year
              return yr % 2 === 0 ? String(yr) : "";     // label even years only
            },
          },
          // Keep only one tick per year (the first month of that year present
          // in the data) so vertical hashes align to yearly boundaries and we
          // don't draw a hash for every monthly data point.
          afterBuildTicks: (axis) => {
            const all = axis.chart.data.labels || [];
            if (!all.length) return;
            const ticks = [];
            let lastYear = null;
            for (let i = 0; i < all.length; i++) {
              const yr = parseInt(String(all[i]).slice(0, 4), 10);
              if (Number.isNaN(yr)) continue;
              if (yr === lastYear) continue;
              lastYear = yr;
              ticks.push({ value: i, label: String(all[i]) });
            }
            axis.ticks = ticks;
          },
        },
        y: { grid: { color: "rgba(255,255,255,0.08)" }, ticks: { color: "#8b98a5" } },
      },
      parsing: { xAxisKey: "x", yAxisKey: "y" },
    },
  });

  const note = document.getElementById("debt-projection-note");
  if (note) {
    if (projection.months_to_payoff != null) {
      const yrs = (projection.months_to_payoff / 12).toFixed(1);
      note.textContent = `Paid off in ${projection.months_to_payoff} months (~${yrs} yrs) — ${projection.payoff_date}. Interest: ${fmt(projection.total_interest)}.`;
    } else if (projection.unprojectable.length) {
      note.textContent = `Cannot project — missing rate/balance/min on: ${projection.unprojectable.join(", ")}.`;
    } else {
      note.textContent = "Beyond 50-year horizon at current payment.";
    }
  }
}

async function loadDebt() {
  if (!document.getElementById("debt-chart")) return;
  await loadLoans();
  const strategy = document.getElementById("debt-strategy")?.value || "distributed";
  await Promise.all([
    loadDebtSummary(),
    loadLoanPayments(),
    loadDebtChart(0, 0, strategy),
    loadDebtBalanceChart(),
  ]);
}

// Loan form submit (add / edit)
document.getElementById("debt-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const body = {
    loan_id: form.get("loan_id") || null,
    name: form.get("name"),
    loan_type: form.get("loan_type") || null,
    interest_rate: form.get("interest_rate") ? parseFloat(form.get("interest_rate")) : null,
    min_payment: form.get("min_payment") ? parseFloat(form.get("min_payment")) : null,
    current_balance: form.get("current_balance") ? parseFloat(form.get("current_balance")) : null,
    next_due_date: form.get("next_due_date") || null,
    auto_pay: form.get("auto_pay") === "on",
    status: form.get("status"),
    notes: form.get("notes") || null,
  };
  await api("/api/loans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  resetLoanForm();
  await loadDebt();
});

document.getElementById("debt-form-cancel")?.addEventListener("click", resetLoanForm);

function resetLoanForm() {
  const form = document.getElementById("debt-form");
  if (!form) return;
  form.reset();
  form.querySelector('[name="loan_id"]').value = "";
  document.getElementById("debt-form-title").textContent = "Add Loan";
  document.getElementById("debt-form-cancel").hidden = true;
}

document.querySelector("#debt-loans-table")?.addEventListener("click", async (e) => {
  const editId = e.target.getAttribute("data-edit-loan");
  const delId = e.target.getAttribute("data-del-loan");
  if (editId) {
    const l = currentLoans.find((x) => x.loan_id === editId);
    if (!l) return;
    const form = document.getElementById("debt-form");
    form.querySelector('[name="loan_id"]').value = l.loan_id;
    form.querySelector('[name="name"]').value = l.name;
    form.querySelector('[name="loan_type"]').value = l.loan_type || "";
    form.querySelector('[name="interest_rate"]').value = l.interest_rate ?? "";
    form.querySelector('[name="min_payment"]').value = l.min_payment ?? "";
    form.querySelector('[name="current_balance"]').value = l.current_balance ?? "";
    form.querySelector('[name="next_due_date"]').value = l.next_due_date || "";
    form.querySelector('[name="auto_pay"]').checked = !!l.auto_pay;
    form.querySelector('[name="status"]').value = l.status;
    form.querySelector('[name="notes"]').value = l.notes || "";
    document.getElementById("debt-form-title").textContent = "Edit Loan";
    document.getElementById("debt-form-cancel").hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (delId) {
    if (!confirm("Delete this loan and all its payments/snapshots?")) return;
    await api(`/api/loans/${delId}`, { method: "DELETE" });
    await loadDebt();
  }
});

// Payment form submit
document.getElementById("loan-payment-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const body = {
    loan_id: form.get("loan_id") || null,
    payment_date: form.get("payment_date"),
    amount: parseFloat(form.get("amount")),
    status: form.get("status"),
    source: form.get("source") || null,
    notes: null,
    new_balance: form.get("new_balance") ? parseFloat(form.get("new_balance")) : null,
  };
  await api("/api/loan-payments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  e.target.reset();
  await loadDebt();
});

document.querySelector("#debt-payments-table")?.addEventListener("click", async (e) => {
  const delId = e.target.getAttribute("data-del-payment");
  if (!delId) return;
  if (!confirm("Delete this payment record?")) return;
  await api(`/api/loan-payments/${delId}`, { method: "DELETE" });
  await loadDebt();
});

// Re-project button
document.getElementById("debt-reproject")?.addEventListener("click", async () => {
  const extra = parseFloat(document.getElementById("debt-extra").value || "0");
  const onetime = parseFloat(
    document.getElementById("debt-extra-onetime")?.value || "0"
  );
  const strategy = document.getElementById("debt-strategy")?.value || "distributed";
  await loadDebtChart(extra, onetime, strategy);
});

loadDebt();

// --- Splits ---------------------------------------------------------------
let splitGroupId = null;
let splitGroup = null; // GET /api/splits/groups/{id}
let splitSearchRows = [];
let splitSelected = new Set(); // transaction_ids
let splitTxAmounts = {}; // transaction_id -> amount, for the live preview
let splitMode = "even";
let splitChecked = new Set(["me"]); // participants in the split being built
let splitValues = {}; // participant -> typed value (exact $ or share weight)
let splitKnownMembers = new Set();

const splitJson = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function splitErrorMessage(err) {
  const m = String(err.message || err).match(/"detail":\s*"([^"]+)"/);
  return m ? m[1] : String(err.message || err);
}

async function splitGuard(fn) {
  try {
    await fn();
  } catch (err) {
    alert(splitErrorMessage(err));
  }
}

const splitIso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const splitParticipants = () => [
  { id: "me", name: "Me" },
  ...(splitGroup?.members || []).map((m) => ({ id: m.person_id, name: m.name })),
];

async function loadSplits() {
  if (!document.getElementById("splits-page")) return;
  await Promise.all([loadSplitGroups(), loadSplitPeople()]);
}

async function loadSplitPeople() {
  const people = await api("/api/splits/people");
  document.getElementById("split-people-list").innerHTML = people
    .map((p) => `<option value="${escapeHtml(p.name)}"></option>`)
    .join("");
}

async function loadSplitGroups() {
  const groups = await api("/api/splits/groups");
  const list = document.getElementById("split-groups-list");
  if (!groups.length) {
    list.innerHTML = `<li class="muted">No groups yet.</li>`;
    return;
  }
  list.innerHTML = groups
    .map(
      (g) => `
    <li data-group-id="${g.group_id}" class="${g.group_id === splitGroupId ? "active" : ""}">
      <strong>${escapeHtml(g.name)}</strong>
      <span class="muted">${g.start_date ? escapeHtml(g.start_date) : ""}${g.end_date ? " → " + escapeHtml(g.end_date) : ""}</span>
      <span class="split-owed">${fmt(g.total_outstanding)} owed</span>
    </li>`
    )
    .join("");
}

document.getElementById("split-groups-list")?.addEventListener("click", (e) => {
  const li = e.target.closest("li[data-group-id]");
  if (li) splitGuard(() => selectSplitGroup(li.dataset.groupId));
});

async function selectSplitGroup(id) {
  splitGroupId = id;
  splitKnownMembers = new Set();
  splitChecked = new Set(["me"]);
  splitValues = {};
  splitSelected = new Set();
  splitTxAmounts = {};
  splitMode = "even";
  await refreshSplitGroup();
  const form = document.getElementById("split-search-form");
  const today = new Date();
  const firstOfLastMonth = new Date(today.getFullYear(), today.getMonth() - 1, 1);
  form.elements.start.value = splitGroup.start_date || splitIso(firstOfLastMonth);
  form.elements.end.value = splitGroup.end_date || splitIso(today);
  form.elements.q.value = "";
  await runSplitSearch();
  await loadSplitGroups();
}

async function refreshSplitGroup() {
  splitGroup = await api(`/api/splits/groups/${splitGroupId}`);
  // New members start out checked so a trip split includes everyone by default.
  for (const m of splitGroup.members) {
    if (!splitKnownMembers.has(m.person_id)) {
      splitKnownMembers.add(m.person_id);
      splitChecked.add(m.person_id);
    }
  }
  renderSplitGroup();
}

function renderSplitGroup() {
  const g = splitGroup;
  document.getElementById("split-group-panel").hidden = false;
  document.getElementById("split-group-title").textContent = g.name;

  document.querySelector("#split-balances-table tbody").innerHTML =
    g.balances.people
      .map(
        (p) => `
    <tr>
      <td>${escapeHtml(p.name)}</td>
      <td class="num">${fmt(p.owed)}</td>
      <td class="num">${fmt(p.repaid)}</td>
      <td class="num"><strong>${fmt(p.outstanding)}</strong></td>
    </tr>`
      )
      .join("") || `<tr><td colspan="4" class="muted">No people yet. Add some above.</td></tr>`;
  document.getElementById("split-my-share").textContent =
    `Your share: ${fmt(g.balances.my_share)} of ${fmt(g.balances.total_spent)} spent.`;

  const select = document.querySelector("#split-repayment-form select");
  select.innerHTML = g.members
    .map((m) => `<option value="${m.person_id}">${escapeHtml(m.name)}</option>`)
    .join("");

  document.querySelector("#split-expenses-table tbody").innerHTML =
    g.expenses
      .map(
        (e) => `
    <tr>
      <td>${escapeHtml(e.date)}</td>
      <td>${escapeHtml(e.name)}</td>
      <td class="num">${fmt(e.amount)}</td>
      <td>${e.shares.map((s) => `${escapeHtml(s.name)} ${fmt(s.amount)}`).join(" · ")}
        <small class="muted">(${e.split_mode})</small></td>
      <td>
        <button class="btn btn-sm btn-secondary" data-edit-tx="${e.transaction_id}">Edit</button>
        <button class="btn btn-sm btn-danger" data-remove-tx="${e.transaction_id}">Remove</button>
      </td>
    </tr>`
      )
      .join("") || `<tr><td colspan="5" class="muted">Nothing assigned yet.</td></tr>`;

  document.querySelector("#split-repayments-table tbody").innerHTML =
    g.repayments
      .map(
        (r) => `
    <tr>
      <td>${escapeHtml(r.paid_date)}</td>
      <td>${escapeHtml(r.name)}</td>
      <td class="num">${fmt(r.amount)}</td>
      <td><button class="btn btn-sm btn-danger" data-del-repayment="${r.repayment_id}">Del</button></td>
    </tr>`
      )
      .join("") || `<tr><td colspan="4" class="muted">No repayments yet.</td></tr>`;

  updateSplitSelection();
}

// --- Searching transactions ---
document.getElementById("split-search-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  splitGuard(runSplitSearch);
});

async function runSplitSearch() {
  const form = document.getElementById("split-search-form");
  const params = new URLSearchParams({
    start: form.elements.start.value,
    end: form.elements.end.value,
  });
  if (form.elements.q.value.trim()) params.set("q", form.elements.q.value.trim());
  splitSearchRows = await api(`/api/splits/transactions?${params}`);
  for (const r of splitSearchRows) splitTxAmounts[r.transaction_id] = r.amount;
  renderSplitSearch();
}

function splitRowSelectable(r) {
  return !r.group_id;
}

function renderSplitSearch() {
  const tbody = document.querySelector("#split-search-table tbody");
  if (!splitSearchRows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No charges in that range.</td></tr>`;
  } else {
    tbody.innerHTML = splitSearchRows
      .map((r) => {
        const inThis = r.group_id === splitGroupId;
        const selectable = splitRowSelectable(r);
        const label = inThis ? "✓ in this group" : r.group_name ? escapeHtml(r.group_name) : "";
        return `
    <tr class="${selectable ? "" : "disabled"}">
      <td><input type="checkbox" data-split-tx="${r.transaction_id}"
        ${splitSelected.has(r.transaction_id) ? "checked" : ""} ${selectable ? "" : "disabled"} /></td>
      <td>${escapeHtml(r.date)}</td>
      <td>${escapeHtml(r.merchant || r.name)}</td>
      <td>${escapeHtml(r.account_name || "")}</td>
      <td>${escapeHtml(r.category || "")}</td>
      <td class="num">${fmt(r.amount)}</td>
      <td>${label}</td>
    </tr>`;
      })
      .join("");
  }
  document.getElementById("split-select-all").checked = false;
  updateSplitSelection();
}

document.querySelector("#split-search-table")?.addEventListener("change", (e) => {
  if (e.target.id === "split-select-all") {
    for (const r of splitSearchRows.filter(splitRowSelectable)) {
      if (e.target.checked) splitSelected.add(r.transaction_id);
      else splitSelected.delete(r.transaction_id);
    }
    renderSplitSearch();
    return;
  }
  const id = e.target.getAttribute("data-split-tx");
  if (!id) return;
  if (e.target.checked) splitSelected.add(id);
  else splitSelected.delete(id);
  updateSplitSelection();
});

// --- Split panel ---
function splitSelectedTotal() {
  let total = 0;
  for (const id of splitSelected) total += splitTxAmounts[id] || 0;
  return total;
}

function updateSplitSelection() {
  const n = splitSelected.size;
  const line = document.getElementById("split-selection-line");
  if (!line) return;
  line.textContent = n ? `${n} selected · ${fmt(splitSelectedTotal())}` : "";
  document.getElementById("split-panel").hidden = n === 0;
  // Exact dollar amounts only make sense for a single transaction.
  if (n > 1 && splitMode === "exact") splitMode = "even";
  document.querySelector('[data-mode="exact"]').disabled = n > 1;
  renderSplitPanel();
}

function renderSplitPanel() {
  if (!splitGroup) return;
  document.getElementById("split-participants").innerHTML = splitParticipants()
    .map(
      (p) => `
    <label class="split-chip">
      <input type="checkbox" data-participant="${escapeHtml(p.id)}" ${splitChecked.has(p.id) ? "checked" : ""} />
      ${escapeHtml(p.name)}
    </label>`
    )
    .join("");

  document.querySelectorAll("[data-mode]").forEach((btn) => {
    const active = btn.dataset.mode === splitMode;
    btn.classList.toggle("btn-primary", active);
    btn.classList.toggle("btn-secondary", !active);
  });

  const valuesDiv = document.getElementById("split-values");
  if (splitMode === "even") {
    valuesDiv.innerHTML = "";
  } else {
    valuesDiv.innerHTML = splitParticipants()
      .filter((p) => splitChecked.has(p.id))
      .map((p) => {
        const v = splitValues[p.id] ?? (splitMode === "shares" ? 1 : "");
        return `
      <label class="split-chip">${escapeHtml(p.name)}
        <input type="number" step="0.01" min="0" data-value-for="${escapeHtml(p.id)}"
          value="${escapeHtml(v)}" placeholder="${splitMode === "exact" ? "$" : "weight"}" />
      </label>`;
      })
      .join("");
  }
  renderSplitPreview();
}

// Rough preview; the server computes the authoritative cent-exact amounts.
function renderSplitPreview() {
  const preview = document.getElementById("split-preview");
  const assignBtn = document.getElementById("split-assign-btn");
  if (!preview) return;
  const total = splitSelectedTotal();
  const people = splitParticipants().filter((p) => splitChecked.has(p.id));
  const weightOf = (p) => parseFloat(splitValues[p.id] ?? (splitMode === "shares" ? 1 : ""));
  let amounts = {};
  let ok = people.length > 0 && splitSelected.size > 0;
  let note = "";

  if (splitMode === "even") {
    people.forEach((p) => (amounts[p.id] = total / people.length));
  } else if (splitMode === "shares") {
    const weights = people.map(weightOf);
    const sum = weights.reduce((a, b) => a + b, 0);
    if (weights.some((w) => !(w > 0))) ok = false;
    else people.forEach((p, i) => (amounts[p.id] = (total * weights[i]) / sum));
  } else {
    let sum = 0;
    people.forEach((p) => {
      const v = weightOf(p) || 0;
      amounts[p.id] = v;
      sum += v;
    });
    const remaining = Math.round((total - sum) * 100) / 100;
    note = ` Remaining: ${fmt(remaining)}`;
    if (Math.abs(remaining) > 0.005 || splitSelected.size !== 1) ok = false;
  }

  preview.textContent =
    people.map((p) => `${p.name} ${fmt(amounts[p.id] || 0)}`).join(" · ") + note;
  assignBtn.disabled = !ok;
}

document.getElementById("split-participants")?.addEventListener("change", (e) => {
  const id = e.target.getAttribute("data-participant");
  if (!id) return;
  if (e.target.checked) splitChecked.add(id);
  else splitChecked.delete(id);
  renderSplitPanel();
});

document.getElementById("split-values")?.addEventListener("input", (e) => {
  const id = e.target.getAttribute("data-value-for");
  if (!id) return;
  splitValues[id] = e.target.value;
  renderSplitPreview();
});

document.querySelectorAll("[data-mode]").forEach((btn) =>
  btn.addEventListener("click", () => {
    splitMode = btn.dataset.mode;
    renderSplitPanel();
  })
);

document.getElementById("split-cancel-btn")?.addEventListener("click", () => {
  splitSelected = new Set();
  renderSplitSearch();
});

document.getElementById("split-assign-btn")?.addEventListener("click", () =>
  splitGuard(async () => {
    const shares = splitParticipants()
      .filter((p) => splitChecked.has(p.id))
      .map((p) => ({
        participant: p.id,
        value: splitMode === "even" ? 1 : parseFloat(splitValues[p.id] ?? 1),
      }));
    await api(
      `/api/splits/groups/${splitGroupId}/expenses`,
      splitJson("POST", {
        transaction_ids: [...splitSelected],
        split_mode: splitMode,
        shares,
      })
    );
    splitSelected = new Set();
    splitValues = {};
    await refreshSplitGroup();
    await runSplitSearch();
    await loadSplitGroups();
  })
);

// --- Assigned expenses: edit / remove ---
document.querySelector("#split-expenses-table")?.addEventListener("click", (e) => {
  const editId = e.target.getAttribute("data-edit-tx");
  const removeId = e.target.getAttribute("data-remove-tx");
  if (editId) {
    const exp = splitGroup.expenses.find((x) => x.transaction_id === editId);
    if (!exp) return;
    splitSelected = new Set([editId]);
    splitTxAmounts[editId] = exp.amount;
    splitMode = exp.split_mode;
    splitChecked = new Set(exp.shares.map((s) => s.participant));
    splitValues = {};
    if (exp.split_mode !== "even") exp.shares.forEach((s) => (splitValues[s.participant] = s.value));
    renderSplitSearch();
    document.getElementById("split-panel").scrollIntoView({ behavior: "smooth" });
  } else if (removeId) {
    if (!confirm("Remove this expense from the group?")) return;
    splitGuard(async () => {
      await api(
        `/api/splits/groups/${splitGroupId}/expenses/${removeId}`,
        { method: "DELETE" }
      );
      splitSelected.delete(removeId);
      await refreshSplitGroup();
      await runSplitSearch();
      await loadSplitGroups();
    });
  }
});

// --- Group / people / repayment forms ---
document.getElementById("split-group-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  splitGuard(async () => {
    const { group_id } = await api(
      "/api/splits/groups",
      splitJson("POST", {
        name: form.elements.name.value,
        start_date: form.elements.start_date.value || null,
        end_date: form.elements.end_date.value || null,
        members: form.elements.members.value.split(",").map((s) => s.trim()).filter(Boolean),
      })
    );
    form.reset();
    await loadSplitPeople();
    await selectSplitGroup(group_id);
  });
});

document.getElementById("split-add-member-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  splitGuard(async () => {
    await api(
      `/api/splits/groups/${splitGroupId}/members`,
      splitJson("POST", {
        names: form.elements.names.value.split(",").map((s) => s.trim()).filter(Boolean),
      })
    );
    form.reset();
    await loadSplitPeople();
    await refreshSplitGroup();
  });
});

document.getElementById("split-repayment-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  splitGuard(async () => {
    await api(
      `/api/splits/groups/${splitGroupId}/repayments`,
      splitJson("POST", {
        person_id: form.elements.person_id.value,
        amount: parseFloat(form.elements.amount.value),
        paid_date: form.elements.paid_date.value || null,
      })
    );
    form.elements.amount.value = "";
    await refreshSplitGroup();
    await loadSplitGroups();
  });
});

document.querySelector("#split-repayments-table")?.addEventListener("click", (e) => {
  const id = e.target.getAttribute("data-del-repayment");
  if (!id || !confirm("Delete this repayment?")) return;
  splitGuard(async () => {
    await api(`/api/splits/repayments/${id}`, { method: "DELETE" });
    await refreshSplitGroup();
    await loadSplitGroups();
  });
});

document.getElementById("split-delete-group")?.addEventListener("click", () => {
  if (!confirm("Delete this group, its assigned expenses and repayments?")) return;
  splitGuard(async () => {
    await api(`/api/splits/groups/${splitGroupId}`, { method: "DELETE" });
    splitGroupId = null;
    splitGroup = null;
    splitSelected = new Set();
    document.getElementById("split-group-panel").hidden = true;
    await loadSplitGroups();
  });
});

loadSplits();

// --- First-run setup wizard ---------------------------------------------------
(function setupWizard() {
  const root = document.getElementById("setup");
  if (!root) return;
  const steps = [...root.querySelectorAll("[data-step]")];
  const show = (name) => {
    steps.forEach((el) => (el.hidden = el.dataset.step !== name));
    root.querySelectorAll("[data-step-dot]").forEach((dot) =>
      dot.classList.toggle("active", dot.dataset.stepDot === name));
    window.scrollTo(0, 0);
  };
  root.querySelectorAll("[data-goto]").forEach((btn) =>
    btn.addEventListener("click", () => show(btn.dataset.goto)));
  show(root.dataset.start || "welcome");

  const form = document.getElementById("setup-keys-form");
  const keysError = document.getElementById("setup-keys-error");
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    keysError.hidden = true;
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Checking with Plaid…";
    try {
      const res = await api("/api/setup/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: form.elements.client_id.value.trim(),
          secret: form.elements.secret.value.trim(),
          environment: form.elements.environment.value,
        }),
      });
      document.getElementById("setup-saved-to").textContent = res.saved_to_label;
      show("bank");
    } catch (err) {
      keysError.textContent = err.message;
      keysError.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Save keys";
    }
  });

  const linked = document.getElementById("setup-linked");
  const bankError = document.getElementById("setup-bank-error");
  document.getElementById("setup-link-btn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    bankError.hidden = true;
    btn.disabled = true;
    btn.textContent = "Waiting for Plaid…";
    try {
      const name = await linkNewBank();
      if (name) {
        const li = document.createElement("li");
        li.textContent = `${name}: linked and synced`;
        linked.appendChild(li);
        linked.hidden = false;
        document.getElementById("setup-bank-next").disabled = false;
        btn.textContent = "Add another bank";
      } else {
        btn.textContent = linked.children.length ? "Add another bank" : "Add a bank";
      }
    } catch (err) {
      bankError.textContent = err.message;
      bankError.hidden = false;
      btn.textContent = "Try again";
    } finally {
      btn.disabled = false;
    }
  });
})();
