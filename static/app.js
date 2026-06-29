const form = document.querySelector("#configForm");
const message = document.querySelector("#message");
const statusText = document.querySelector("#statusText");
const statsText = document.querySelector("#statsText");
const jobIdText = document.querySelector("#jobIdText");
const logBox = document.querySelector("#logBox");
const startBtn = document.querySelector("#startBtn");
const stopBtn = document.querySelector("#stopBtn");

let currentJobId = null;
let logOffset = 0;
let pollTimer = null;

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

loadConfig().catch((error) => setMessage(error.message));
