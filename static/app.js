const form = document.querySelector("#configForm");
const message = document.querySelector("#message");
const statusText = document.querySelector("#statusText");
const statsText = document.querySelector("#statsText");
const jobIdText = document.querySelector("#jobIdText");
const logBox = document.querySelector("#logBox");
const startBtn = document.querySelector("#startBtn");
const stopBtn = document.querySelector("#stopBtn");
const refreshAccountsBtn = document.querySelector("#refreshAccountsBtn");
const importSub2apiBtn = document.querySelector("#importSub2apiBtn");
const accountsBody = document.querySelector("#accountsBody");
const accountsSummary = document.querySelector("#accountsSummary");

let currentJobId = null;
let logOffset = 0;
let pollTimer = null;
let accounts = [];
let accountPushStatus = {};
let pushingToSub2api = false;

function setMessage(text) {
  message.textContent = text || "";
}

function formPayload() {
  const data = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  data.enable_nsfw = form.elements.enable_nsfw.checked;
  data.grok2api_auto_add_local = form.elements.grok2api_auto_add_local.checked;
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
}

function selectedAccountIds() {
  return Array.from(document.querySelectorAll(".account-check:checked")).map((input) => input.value);
}

function renderAccounts() {
  if (!accountsBody) return;
  accountsBody.innerHTML = "";
  accountsSummary.textContent = `共 ${accounts.length} 个账号`;
  if (!accounts.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="8" class="empty">暂无账号，注册成功后会出现在这里</td>';
    accountsBody.appendChild(row);
    return;
  }
  for (const account of accounts) {
    const row = document.createElement("tr");
    const checkCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.className = "account-check";
    checkbox.type = "checkbox";
    checkbox.value = account.id;
    checkbox.disabled = !account.has_refresh_token;
    if (!account.has_refresh_token) {
      checkbox.title = "缺少 Refresh Token，不能推送到 sub2api";
    }
    checkCell.appendChild(checkbox);
    row.appendChild(checkCell);
    const refreshStatus = account.has_refresh_token
      ? `已保存 ${account.refresh_token_preview || ""}`.trim()
      : "缺少";
    for (const value of [
      account.email,
      account.sso_preview || "",
      refreshStatus,
      account.source_file || "",
      account.line_no || "",
      account.password ? "已保存" : "-",
      accountPushStatus[account.id] || "未推送",
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "");
      if (value === "已推送") cell.className = "push-ok";
      if (value === "推送中") cell.className = "push-running";
      if (String(value).startsWith("失败")) cell.className = "push-failed";
      row.appendChild(cell);
    }
    accountsBody.appendChild(row);
  }
}

async function loadAccounts() {
  const payload = await requestJson("/api/accounts");
  accounts = payload.accounts || [];
  renderAccounts();
}

async function importSelectedToSub2api() {
  const accountIds = selectedAccountIds();
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
    accountIds.forEach((id) => {
      accountPushStatus[id] = "已推送";
    });
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

importSub2apiBtn.addEventListener("click", () => {
  importSelectedToSub2api().catch((error) => setMessage(error.message));
});

loadConfig().catch((error) => setMessage(error.message));
loadAccounts().catch((error) => setMessage(error.message));
