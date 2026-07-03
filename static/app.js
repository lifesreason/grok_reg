const form = document.querySelector("#configForm");
const message = document.querySelector("#message");
const statusText = document.querySelector("#statusText");
const statsText = document.querySelector("#statsText");
const jobIdText = document.querySelector("#jobIdText");
const logBox = document.querySelector("#logBox");
const startBtn = document.querySelector("#startBtn");
const stopBtn = document.querySelector("#stopBtn");
const refreshAccountsBtn = document.querySelector("#refreshAccountsBtn");
const checkHealthBtn = document.querySelector("#checkHealthBtn");
const importGrok2apiBtn = document.querySelector("#importGrok2apiBtn");
const importSub2apiBtn = document.querySelector("#importSub2apiBtn");
const dashboardStatusText = document.querySelector("#dashboardStatusText");
const dashboardRunNote = document.querySelector("#dashboardRunNote");
const dashboardTotalAccounts = document.querySelector("#dashboardTotalAccounts");
const dashboardRefreshAccounts = document.querySelector("#dashboardRefreshAccounts");
const dashboardHealthyAccounts = document.querySelector("#dashboardHealthyAccounts");
const dashboardNeedActionAccounts = document.querySelector("#dashboardNeedActionAccounts");
const dashboardPipeline = document.querySelector("#dashboardPipeline");
const dashboardHealthMix = document.querySelector("#dashboardHealthMix");
const dashboardPushMix = document.querySelector("#dashboardPushMix");
const dashboardSources = document.querySelector("#dashboardSources");
const selectPageAccounts = document.querySelector("#selectPageAccounts");
const accountPageSize = document.querySelector("#accountPageSize");
const accountColumnOptions = document.querySelector("#accountColumnOptions");
const accountPagination = document.querySelector("#accountPagination");
const accountsHead = document.querySelector("#accountsHead");
const accountsBody = document.querySelector("#accountsBody");
const accountsSummary = document.querySelector("#accountsSummary");
const tabButtons = Array.from(document.querySelectorAll("[data-tab-target]"));
const tabPanels = Array.from(document.querySelectorAll("[data-tab-panel]"));

const ACCOUNT_TABLE_PREFS_KEY = "grok-reg.accounts.table";
const ACCOUNT_TABLE_PREFS_VERSION = 2;
const ACCOUNT_COLUMNS = [
  { key: "select", label: "选择", locked: true },
  { key: "email", label: "邮箱", className: "email-column" },
  { key: "sso", label: "SSO 摘要", className: "token-column" },
  { key: "refresh", label: "Refresh Token", className: "token-column" },
  { key: "source", label: "来源文件", className: "source-column" },
  { key: "index", label: "序号" },
  { key: "password", label: "密码" },
  { key: "health", label: "健康状态" },
  { key: "grok2api", label: "grok2api" },
  { key: "sub2api", label: "sub2api" },
];
const DEFAULT_ACCOUNT_TABLE_PREFS = {
  visibleColumns: ACCOUNT_COLUMNS.map((column) => column.key),
  pageSize: 20,
  version: ACCOUNT_TABLE_PREFS_VERSION,
};

let currentJobId = null;
let logOffset = 0;
let pollTimer = null;
let accounts = [];
let accountPage = 1;
let accountTablePrefs = loadAccountTablePrefs();
let selectedAccountIdsSet = new Set();
let accountHealthStatus = {};
let accountPushStatus = {};
let accountGrok2apiPushStatus = {};
let pushingToSub2api = false;
let pushingToGrok2api = false;

function setMessage(text) {
  message.textContent = text || "";
}

function activateTab(name) {
  tabButtons.forEach((button) => {
    const active = button.dataset.tabTarget === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  tabPanels.forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.tabPanel === name);
  });
  if (name === "accounts" || name === "dashboard") {
    loadAccounts().catch((error) => setMessage(error.message));
  }
}

function formPayload() {
  const data = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  data.enable_nsfw = form.elements.enable_nsfw.checked;
  data.grok2api_auto_add_local = form.elements.grok2api_auto_add_local.checked;
  data.grok2api_auto_add_remote = form.elements.grok2api_auto_add_remote.checked;
  data.sub2api_auto_import_remote = form.elements.sub2api_auto_import_remote.checked;
  data.register_count = Number(data.register_count || 1);
  data.register_threads = Number(data.register_threads || 1);
  data.sub2api_concurrency = Number(data.sub2api_concurrency || 3);
  data.sub2api_priority = Number(data.sub2api_priority || 50);
  return data;
}

function applyConfig(config) {
  for (const [key, value] of Object.entries(config)) {
    const field = form.elements[key];
    if (!field) continue;
    if (field.type === "checkbox") {
      field.checked = Boolean(value);
    } else {
      field.value = value ?? "";
    }
  }
  const paths = [
    config.cloudflare_path_domains,
    config.cloudflare_path_accounts,
    config.cloudflare_path_token,
    config.cloudflare_path_messages,
  ].filter(Boolean);
  if (paths.length === 4 && form.elements.cloudflare_paths) {
    form.elements.cloudflare_paths.value = paths.join(",");
  }
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

async function loadConfig() {
  const config = await requestJson("/api/config");
  applyConfig(config);
}

async function saveConfig() {
  const config = await requestJson("/api/config", {
    method: "PUT",
    body: JSON.stringify(formPayload()),
  });
  applyConfig(config);
  setMessage("配置已保存");
}

async function startJob() {
  const job = await requestJson("/api/jobs/start", {
    method: "POST",
    body: JSON.stringify(formPayload()),
  });
  currentJobId = job.job_id;
  logOffset = 0;
  logBox.textContent = "";
  jobIdText.textContent = currentJobId;
  setMessage("任务已启动");
  startPolling();
}

async function stopJob() {
  if (!currentJobId) return;
  await requestJson(`/api/jobs/${currentJobId}/stop`, { method: "POST" });
  setMessage("已请求停止任务");
}

async function pollJob() {
  if (!currentJobId) return;
  const status = await requestJson(`/api/jobs/${currentJobId}`);
  statusText.textContent = status.status;
  statsText.textContent = `成功 ${status.success_count} / 失败 ${status.fail_count}`;
  const running = ["pending", "running"].includes(status.status);
  startBtn.disabled = running;
  stopBtn.disabled = !running;

  const logs = await requestJson(`/api/jobs/${currentJobId}/logs?offset=${logOffset}`);
  if (logs.lines.length) {
    logBox.textContent += `${logs.lines.join("\n")}\n`;
    logBox.scrollTop = logBox.scrollHeight;
    logOffset = logs.next_offset;
  }

  if (!running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
    loadAccounts().catch((error) => setMessage(error.message));
  }
  renderDashboard();
}

function loadAccountTablePrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(ACCOUNT_TABLE_PREFS_KEY) || "{}");
    const allowedColumns = new Set(ACCOUNT_COLUMNS.map((column) => column.key));
    const visibleColumns = Array.isArray(saved.visibleColumns)
      ? saved.visibleColumns
          .map((key) => (key === "line" ? "index" : key))
          .filter((key) => allowedColumns.has(key))
      : DEFAULT_ACCOUNT_TABLE_PREFS.visibleColumns;
    if (Number(saved.version || 1) < ACCOUNT_TABLE_PREFS_VERSION && !visibleColumns.includes("health")) {
      visibleColumns.push("health");
    }
    const pageSize = [10, 20, 50, 100].includes(Number(saved.pageSize))
      ? Number(saved.pageSize)
      : DEFAULT_ACCOUNT_TABLE_PREFS.pageSize;
    return {
      visibleColumns: visibleColumns.includes("select") ? visibleColumns : ["select", ...visibleColumns],
      pageSize,
      version: ACCOUNT_TABLE_PREFS_VERSION,
    };
  } catch (error) {
    return { ...DEFAULT_ACCOUNT_TABLE_PREFS };
  }
}

function saveAccountTablePrefs() {
  localStorage.setItem(ACCOUNT_TABLE_PREFS_KEY, JSON.stringify(accountTablePrefs));
}

function visibleAccountColumns() {
  const visible = new Set(accountTablePrefs.visibleColumns);
  return ACCOUNT_COLUMNS.filter((column) => column.locked || visible.has(column.key));
}

function accountTotalPages() {
  return Math.max(1, Math.ceil(accounts.length / accountTablePrefs.pageSize));
}

function currentPageAccounts() {
  const start = (accountPage - 1) * accountTablePrefs.pageSize;
  return accounts.slice(start, start + accountTablePrefs.pageSize);
}

function clampAccountPage() {
  accountPage = Math.min(Math.max(1, accountPage), accountTotalPages());
}

function selectedAccountIds() {
  return Array.from(selectedAccountIdsSet).filter((id) => accounts.some((account) => account.id === id));
}

function renderAccountColumns() {
  if (!accountColumnOptions) return;
  accountColumnOptions.innerHTML = "";
  for (const column of ACCOUNT_COLUMNS.filter((item) => !item.locked)) {
    const label = document.createElement("label");
    label.className = "check compact";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("data-column-toggle", column.key);
    checkbox.checked = accountTablePrefs.visibleColumns.includes(column.key);
    label.appendChild(checkbox);
    label.append(document.createTextNode(column.label));
    accountColumnOptions.appendChild(label);
  }
}

function renderAccountsHead() {
  if (!accountsHead) return;
  const row = document.createElement("tr");
  for (const column of visibleAccountColumns()) {
    const cell = document.createElement("th");
    cell.textContent = column.label;
    if (column.className) cell.className = column.className;
    row.appendChild(cell);
  }
  accountsHead.innerHTML = "";
  accountsHead.appendChild(row);
}

function accountCellValue(account, key, rowNumber) {
  const refreshStatus = account.has_refresh_token
    ? `已保存 ${account.refresh_token_preview || ""}`.trim()
    : "缺少";
  const persistedGrok2apiStatus = account.grok2api_status_text || (account.grok2api_status === "pushed" ? "已推送" : "未推送");
  const grok2apiStatus = accountGrok2apiPushStatus[account.id] || persistedGrok2apiStatus;
  const persistedSub2apiStatus = account.sub2api_status_text || (account.sub2api_status === "pushed" ? "已推送" : "未推送");
  const sub2apiStatus = accountPushStatus[account.id] || persistedSub2apiStatus;
  const persistedHealthStatus = account.health_status_text || "未检查";
  const healthStatus = accountHealthStatus[account.id] || persistedHealthStatus;
  const values = {
    email: account.email,
    sso: account.sso_preview || "",
    refresh: refreshStatus,
    source: account.source_file || "",
    index: rowNumber,
    password: account.password ? "已保存" : "-",
    health: healthStatus,
    grok2api: grok2apiStatus,
    sub2api: sub2apiStatus,
  };
  return values[key] ?? "";
}

function dashboardMetricValue(element, value) {
  if (!element) return;
  element.textContent = String(value);
}

function isFailedStatus(account, prefix) {
  const status = String(account[`${prefix}_status`] || "").toLowerCase();
  const text = String(account[`${prefix}_status_text`] || "");
  return status === "failed" || text.startsWith("失败");
}

function accountDashboardStats() {
  const total = accounts.length;
  const refresh = accounts.filter((account) => account.has_refresh_token).length;
  const healthy = accounts.filter((account) => account.health_status === "healthy" || account.health_status_text === "可用").length;
  const unhealthy = accounts.filter((account) => account.health_status === "unhealthy" || account.health_status_text === "失效").length;
  const incomplete = accounts.filter((account) => account.health_status === "incomplete" || account.health_status_text === "资料不完整").length;
  const untested = Math.max(0, total - healthy - unhealthy - incomplete);
  const grok2api = accounts.filter((account) => account.grok2api_status === "pushed" || account.grok2api_status_text === "已推送").length;
  const sub2api = accounts.filter((account) => account.sub2api_status === "pushed" || account.sub2api_status_text === "已推送").length;
  const needAction = accounts.filter((account) => {
    return (
      !account.has_refresh_token ||
      account.health_status === "unhealthy" ||
      account.health_status === "incomplete" ||
      account.health_status_text === "失效" ||
      account.health_status_text === "资料不完整" ||
      isFailedStatus(account, "grok2api") ||
      isFailedStatus(account, "sub2api")
    );
  }).length;
  return {
    total,
    refresh,
    healthy,
    unhealthy,
    incomplete,
    untested,
    grok2api,
    sub2api,
    needAction,
  };
}

function percentText(value, total) {
  if (!total) return "0%";
  return `${Math.round((value / total) * 100)}%`;
}

function renderMixRow(parent, label, value, total, tone = "") {
  const row = document.createElement("div");
  row.className = `mix-row ${tone}`.trim();
  const meta = document.createElement("div");
  meta.className = "mix-meta";
  const title = document.createElement("span");
  title.textContent = label;
  const count = document.createElement("strong");
  count.textContent = `${value} / ${total}`;
  meta.append(title, count);
  const track = document.createElement("div");
  track.className = "mix-track";
  const bar = document.createElement("span");
  bar.style.width = percentText(value, total);
  track.appendChild(bar);
  row.append(meta, track);
  parent.appendChild(row);
}

function renderDashboard() {
  if (!dashboardTotalAccounts) return;
  const stats = accountDashboardStats();
  dashboardMetricValue(dashboardTotalAccounts, stats.total);
  dashboardMetricValue(dashboardRefreshAccounts, stats.refresh);
  dashboardMetricValue(dashboardHealthyAccounts, stats.healthy);
  dashboardMetricValue(dashboardNeedActionAccounts, stats.needAction);
  if (dashboardStatusText) dashboardStatusText.textContent = statusText.textContent || "就绪";
  if (dashboardRunNote) dashboardRunNote.textContent = statsText.textContent || "成功 0 / 失败 0";

  if (dashboardPipeline) {
    dashboardPipeline.innerHTML = "";
    const flow = [
      ["注册账号", stats.total, "账号池总量"],
      ["Refresh Token", stats.refresh, "可推送 sub2api"],
      ["健康可用", stats.healthy, "最近检查通过"],
      ["grok2api", stats.grok2api, "远端已入池"],
      ["sub2api", stats.sub2api, "远端已导入"],
    ];
    for (const [label, value, caption] of flow) {
      const step = document.createElement("div");
      step.className = "flow-step";
      step.style.setProperty("--flow-percent", percentText(value, stats.total));
      const name = document.createElement("span");
      name.textContent = label;
      const number = document.createElement("strong");
      number.textContent = String(value);
      const note = document.createElement("small");
      note.textContent = `${caption} · ${percentText(value, stats.total)}`;
      const line = document.createElement("i");
      step.append(name, number, note, line);
      dashboardPipeline.appendChild(step);
    }
  }

  if (dashboardHealthMix) {
    dashboardHealthMix.innerHTML = "";
    renderMixRow(dashboardHealthMix, "可用", stats.healthy, stats.total, "ok");
    renderMixRow(dashboardHealthMix, "未检查", stats.untested, stats.total);
    renderMixRow(dashboardHealthMix, "资料不完整", stats.incomplete, stats.total, "warn");
    renderMixRow(dashboardHealthMix, "失效", stats.unhealthy, stats.total, "bad");
  }

  if (dashboardPushMix) {
    dashboardPushMix.innerHTML = "";
    renderMixRow(dashboardPushMix, "grok2api 已推送", stats.grok2api, stats.total, "ok");
    renderMixRow(dashboardPushMix, "sub2api 已推送", stats.sub2api, stats.total, "ok");
    renderMixRow(dashboardPushMix, "Refresh Token 覆盖", stats.refresh, stats.total);
  }

  if (dashboardSources) {
    dashboardSources.innerHTML = "";
    const sourceCounts = new Map();
    for (const account of accounts) {
      const source = account.source_file || "未知来源";
      sourceCounts.set(source, (sourceCounts.get(source) || 0) + 1);
    }
    const sources = Array.from(sourceCounts.entries())
      .sort((a, b) => b[1] - a[1] || b[0].localeCompare(a[0]))
      .slice(0, 5);
    if (!sources.length) {
      const empty = document.createElement("p");
      empty.className = "dashboard-empty";
      empty.textContent = "暂无账号批次，注册成功后这里会显示来源文件。";
      dashboardSources.appendChild(empty);
    }
    for (const [source, count] of sources) {
      const item = document.createElement("div");
      item.className = "source-item";
      const name = document.createElement("span");
      name.textContent = source;
      const value = document.createElement("strong");
      value.textContent = `${count} 个`;
      item.append(name, value);
      dashboardSources.appendChild(item);
    }
  }
}

function syncSelectPageAccounts() {
  if (!selectPageAccounts) return;
  const pageAccounts = currentPageAccounts();
  const selectedCount = pageAccounts.filter((account) => selectedAccountIdsSet.has(account.id)).length;
  selectPageAccounts.checked = pageAccounts.length > 0 && selectedCount === pageAccounts.length;
  selectPageAccounts.indeterminate = selectedCount > 0 && selectedCount < pageAccounts.length;
  selectPageAccounts.disabled = pageAccounts.length === 0;
}

function renderPagination() {
  if (!accountPagination) return;
  accountPagination.innerHTML = "";
  const totalPages = accountTotalPages();
  const start = accounts.length ? (accountPage - 1) * accountTablePrefs.pageSize + 1 : 0;
  const end = Math.min(accounts.length, accountPage * accountTablePrefs.pageSize);
  const summary = document.createElement("span");
  summary.className = "pagination-summary";
  summary.textContent = `${start}-${end} / ${accounts.length}`;
  accountPagination.appendChild(summary);

  const prevButton = document.createElement("button");
  prevButton.type = "button";
  prevButton.className = "page-button";
  prevButton.textContent = "上一页";
  prevButton.disabled = accountPage <= 1;
  prevButton.addEventListener("click", () => {
    accountPage -= 1;
    renderAccounts();
  });
  accountPagination.appendChild(prevButton);

  const pageText = document.createElement("span");
  pageText.className = "page-current";
  pageText.textContent = `${accountPage} / ${totalPages}`;
  accountPagination.appendChild(pageText);

  const nextButton = document.createElement("button");
  nextButton.type = "button";
  nextButton.className = "page-button";
  nextButton.textContent = "下一页";
  nextButton.disabled = accountPage >= totalPages;
  nextButton.addEventListener("click", () => {
    accountPage += 1;
    renderAccounts();
  });
  accountPagination.appendChild(nextButton);
}

function renderAccounts() {
  if (!accountsBody) return;
  selectedAccountIdsSet = new Set(selectedAccountIds());
  clampAccountPage();
  renderAccountsHead();
  renderAccountColumns();
  accountsBody.innerHTML = "";
  accountsSummary.textContent = `共 ${accounts.length} 个账号，已选择 ${selectedAccountIdsSet.size} 个`;
  if (accountPageSize) accountPageSize.value = String(accountTablePrefs.pageSize);
  if (!accounts.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="${visibleAccountColumns().length}" class="empty">暂无账号，注册成功后会出现在这里</td>`;
    accountsBody.appendChild(row);
    syncSelectPageAccounts();
    renderPagination();
    return;
  }
  const pageAccounts = currentPageAccounts();
  for (const [pageIndex, account] of pageAccounts.entries()) {
    const rowNumber = (accountPage - 1) * accountTablePrefs.pageSize + pageIndex + 1;
    const row = document.createElement("tr");
    for (const column of visibleAccountColumns()) {
      const cell = document.createElement("td");
      if (column.key === "select") {
        const checkbox = document.createElement("input");
        checkbox.className = "account-check";
        checkbox.type = "checkbox";
        checkbox.value = account.id;
        checkbox.checked = selectedAccountIdsSet.has(account.id);
        checkbox.title = account.has_refresh_token ? "" : "可推送到 grok2api；缺少 Refresh Token 时不能推送到 sub2api";
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selectedAccountIdsSet.add(account.id);
          else selectedAccountIdsSet.delete(account.id);
          accountsSummary.textContent = `共 ${accounts.length} 个账号，已选择 ${selectedAccountIdsSet.size} 个`;
          syncSelectPageAccounts();
        });
        cell.appendChild(checkbox);
        row.appendChild(cell);
        continue;
      }
      const value = accountCellValue(account, column.key, rowNumber);
      cell.textContent = String(value ?? "");
      if (column.className) cell.classList.add(column.className);
      if (value === "已推送") cell.classList.add("push-ok");
      if (value === "可用") cell.classList.add("push-ok");
      if (value === "推送中") cell.classList.add("push-running");
      if (value === "检查中") cell.classList.add("push-running");
      if (String(value).startsWith("失败")) cell.classList.add("push-failed");
      if (value === "失效" || value === "资料不完整") cell.classList.add("push-failed");
      row.appendChild(cell);
    }
    accountsBody.appendChild(row);
  }
  syncSelectPageAccounts();
  renderPagination();
}

async function loadAccounts() {
  const payload = await requestJson("/api/accounts");
  accounts = payload.accounts || [];
  renderDashboard();
  renderAccounts();
}

async function checkSelectedAccountHealth() {
  const accountIds = selectedAccountIds();
  if (!accountIds.length) {
    setMessage("请选择账号再做健康检查");
    return;
  }
  checkHealthBtn.disabled = true;
  checkHealthBtn.textContent = `检查中 ${accountIds.length} 个...`;
  accountIds.forEach((id) => {
    accountHealthStatus[id] = "检查中";
  });
  renderAccounts();
  setMessage(`开始健康检查：${accountIds.length} 个账号`);
  try {
    const result = await requestJson("/api/accounts/check-health", {
      method: "POST",
      body: JSON.stringify({ ...formPayload(), account_ids: accountIds }),
    });
    if (Array.isArray(result.accounts)) {
      const returned = new Map(result.accounts.map((account) => [account.id, account]));
      accounts = accounts.map((account) => returned.get(account.id) || account);
      accountIds.forEach((id) => {
        const account = returned.get(id);
        accountHealthStatus[id] = account?.health_status_text || "未检查";
      });
    }
    setMessage(result.message || `健康检查完成：可用 ${result.healthy || 0} 个，异常 ${result.failed || 0} 个`);
  } catch (error) {
    accountIds.forEach((id) => {
      accountHealthStatus[id] = `失败：${error.message}`;
    });
    setMessage(`健康检查失败：${error.message}`);
  } finally {
    checkHealthBtn.disabled = false;
    checkHealthBtn.textContent = "健康检查";
    renderAccounts();
  }
}

async function importSelectedToSub2api() {
  const accountIds = selectedAccountIds().filter((id) => {
    const account = accounts.find((item) => item.id === id);
    return account?.has_refresh_token;
  });
  if (!accountIds.length) {
    setMessage("请选择带 Refresh Token 的账号再推送");
    return;
  }
  pushingToSub2api = true;
  importSub2apiBtn.disabled = true;
  importSub2apiBtn.textContent = `推送中 ${accountIds.length} 个...`;
  accountIds.forEach((id) => {
    accountPushStatus[id] = "推送中";
  });
  renderAccounts();
  setMessage(`开始推送到 sub2api：${accountIds.length} 个账号`);
  const payload = { ...formPayload(), account_ids: accountIds };
  try {
    const result = await requestJson("/api/accounts/import/sub2api", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (Array.isArray(result.accounts)) {
      const returned = new Map(result.accounts.map((account) => [account.id, account]));
      accounts = accounts.map((account) => returned.get(account.id) || account);
      accountIds.forEach((id) => {
        const account = returned.get(id);
        accountPushStatus[id] = account?.sub2api_status_text || (account?.sub2api_status === "pushed" ? "已推送" : "未推送");
      });
    } else {
      accountIds.forEach((id) => {
        accountPushStatus[id] = result.status === "partial_failed" ? "失败：请刷新查看详情" : "已推送";
      });
    }
    setMessage(`${result.message || `已推送到 sub2api：${result.total} 个账号`}。${result.warning || ""}`);
  } catch (error) {
    accountIds.forEach((id) => {
      accountPushStatus[id] = `失败：${error.message}`;
    });
    setMessage(`推送 sub2api 失败：${error.message}`);
  } finally {
    pushingToSub2api = false;
    importSub2apiBtn.disabled = false;
    importSub2apiBtn.textContent = "推送到 sub2api";
    renderAccounts();
  }
}

async function importSelectedToGrok2api() {
  const accountIds = selectedAccountIds();
  if (!accountIds.length) {
    setMessage("请选择账号再推送到 grok2api");
    return;
  }
  pushingToGrok2api = true;
  importGrok2apiBtn.disabled = true;
  importGrok2apiBtn.textContent = `推送中 ${accountIds.length} 个...`;
  accountIds.forEach((id) => {
    accountGrok2apiPushStatus[id] = "推送中";
  });
  renderAccounts();
  setMessage(`开始推送到 grok2api：${accountIds.length} 个账号`);
  const payload = { ...formPayload(), account_ids: accountIds };
  try {
    const result = await requestJson("/api/accounts/import/grok2api", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (Array.isArray(result.accounts)) {
      const returned = new Map(result.accounts.map((account) => [account.id, account]));
      accounts = accounts.map((account) => returned.get(account.id) || account);
      accountIds.forEach((id) => {
        const account = returned.get(id);
        accountGrok2apiPushStatus[id] = account?.grok2api_status_text || (account?.grok2api_status === "pushed" ? "已推送" : "未推送");
      });
    } else {
      accountIds.forEach((id) => {
        accountGrok2apiPushStatus[id] = result.status === "partial_failed" ? "失败：请刷新查看详情" : "已推送";
      });
    }
    setMessage(`${result.message || `已推送到 grok2api：${result.total} 个账号`}。${result.warning || ""}`);
  } catch (error) {
    accountIds.forEach((id) => {
      accountGrok2apiPushStatus[id] = `失败：${error.message}`;
    });
    setMessage(`推送 grok2api 失败：${error.message}`);
  } finally {
    pushingToGrok2api = false;
    importGrok2apiBtn.disabled = false;
    importGrok2apiBtn.textContent = "推送到 grok2api";
    renderAccounts();
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    pollJob().catch((error) => setMessage(error.message));
  }, 1200);
  pollJob().catch((error) => setMessage(error.message));
}

document.querySelector("#saveBtn").addEventListener("click", () => {
  saveConfig().catch((error) => setMessage(error.message));
});

startBtn.addEventListener("click", () => {
  startJob().catch((error) => setMessage(error.message));
});

stopBtn.addEventListener("click", () => {
  stopJob().catch((error) => setMessage(error.message));
});

refreshAccountsBtn.addEventListener("click", () => {
  loadAccounts().catch((error) => setMessage(error.message));
});

checkHealthBtn.addEventListener("click", () => {
  checkSelectedAccountHealth().catch((error) => setMessage(error.message));
});

selectPageAccounts.addEventListener("change", () => {
  for (const account of currentPageAccounts()) {
    if (selectPageAccounts.checked) selectedAccountIdsSet.add(account.id);
    else selectedAccountIdsSet.delete(account.id);
  }
  renderAccounts();
});

accountPageSize.addEventListener("change", () => {
  accountTablePrefs.pageSize = Number(accountPageSize.value) || DEFAULT_ACCOUNT_TABLE_PREFS.pageSize;
  accountPage = 1;
  saveAccountTablePrefs();
  renderAccounts();
});

accountColumnOptions.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-column-toggle]");
  if (!checkbox) return;
  const visible = new Set(accountTablePrefs.visibleColumns);
  if (checkbox.checked) visible.add(checkbox.dataset.columnToggle);
  else visible.delete(checkbox.dataset.columnToggle);
  visible.add("select");
  accountTablePrefs.visibleColumns = ACCOUNT_COLUMNS
    .map((column) => column.key)
    .filter((key) => visible.has(key));
  accountTablePrefs.version = ACCOUNT_TABLE_PREFS_VERSION;
  saveAccountTablePrefs();
  renderAccounts();
});

importSub2apiBtn.addEventListener("click", () => {
  importSelectedToSub2api().catch((error) => setMessage(error.message));
});

importGrok2apiBtn.addEventListener("click", () => {
  importSelectedToGrok2api().catch((error) => setMessage(error.message));
});

tabButtons.forEach((button) => {
  button.addEventListener("click", () => activateTab(button.dataset.tabTarget));
});

loadConfig().catch((error) => setMessage(error.message));
loadAccounts().catch((error) => setMessage(error.message));
