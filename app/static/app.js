const form = document.querySelector("#convert-form");
const folderInput = document.querySelector("#folder-input");
const zipInput = document.querySelector("#zip-input");
const animationInput = document.querySelector("#animation-input");
const submitButton = document.querySelector("#submit-button");
const formFeedback = document.querySelector("#form-feedback");
const selectionSummary = document.querySelector("#selection-summary");
const jobSummary = document.querySelector("#job-summary");
const defaultOutputRoot = document.querySelector("#default-output-root");
const activeJobList = document.querySelector("#active-job-list");
const jobHistoryList = document.querySelector("#job-history-list");
const detailTitle = document.querySelector("#detail-title");
const detailStatus = document.querySelector("#detail-status");
const detailJobId = document.querySelector("#detail-job-id");
const detailOutput = document.querySelector("#detail-output");
const jobLog = document.querySelector("#job-log");
const detailOpenOutput = document.querySelector("#detail-open-output");
const langToggle = document.querySelector("#lang-toggle");

const jobsById = new Map();
const activeStatuses = new Set(["queued", "running", "cancelling"]);
const completedStatuses = new Set(["completed", "completed_with_warnings"]);
let selectedJobId = null;
let pollTimer = null;
let refreshPromise = null;
let apiToken = null;
let configReady = null;
let lastJobsPayload = { jobs: [], active_count: 0, max_concurrent_jobs: 2 };
let currentLang = localStorage.getItem("ve2rbx-local-lang") || "ja";
const sessionId =
  window.crypto && typeof window.crypto.randomUUID === "function"
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

const statusCopy = {
  queued: ["待機中", "Queued"],
  running: ["変換中", "Running"],
  cancelling: ["停止中", "Cancelling"],
  cancelled: ["停止済み", "Cancelled"],
  completed: ["完了", "Completed"],
  completed_with_warnings: ["完了（警告あり）", "Completed with warnings"],
  failed: ["失敗", "Failed"],
};

function copy(ja, en) {
  return currentLang === "ja" ? ja : en;
}

function applyLang() {
  document.documentElement.lang = currentLang;
  document.body.dataset.lang = currentLang;
  langToggle.textContent = currentLang === "ja" ? "EN" : "JP";
}

function statusLabel(status) {
  const labels = statusCopy[status] || [status, status];
  return copy(labels[0], labels[1]);
}

function setFormFeedback(message, danger = false) {
  formFeedback.textContent = message;
  formFeedback.dataset.status = danger ? "danger" : "";
}

async function api(path, options = {}) {
  const requestOptions = { ...options };
  const method = (requestOptions.method || "GET").toUpperCase();
  if (method !== "GET") {
    if (configReady) await configReady.catch(() => {});
    requestOptions.headers = {
      ...(requestOptions.headers || {}),
      "X-VE2RBX-Token": apiToken || "",
    };
  }
  const response = await fetch(path, requestOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.error;
    throw new Error(message || `HTTP ${response.status}`);
  }
  return payload;
}

function selectedFiles() {
  const zipFile = zipInput.files?.[0];
  if (zipFile) return { mode: "zip", files: [zipFile] };
  return { mode: "folder", files: Array.from(folderInput.files || []) };
}

function appendFiles(formData, files) {
  for (const file of files) {
    formData.append("files", file, file.webkitRelativePath || file.name);
  }
}

function updateSelectionSummary() {
  const { mode, files } = selectedFiles();
  if (!files.length) {
    selectionSummary.textContent = copy("フォルダまたはZIPを選択してください。", "Choose a folder or ZIP.");
    return;
  }
  const name = mode === "zip" ? files[0].name : (files[0].webkitRelativePath || files[0].name).split("/")[0];
  selectionSummary.textContent = copy(
    `選択: ${name}（${files.length}ファイル）`,
    `Selected: ${name} (${files.length} files)`,
  );
}

function renderSummary(payload) {
  const jobs = payload.jobs || [];
  const running = jobs.filter((job) => job.status === "running" || job.status === "cancelling").length;
  const queued = jobs.filter((job) => job.status === "queued").length;
  const finished = jobs.filter((job) => !activeStatuses.has(job.status)).length;
  jobSummary.textContent = copy(
    `変換中 ${running} / ${payload.max_concurrent_jobs} ・ 待機 ${queued} ・ 完了 ${finished} ・ 出力先 ${defaultOutputRoot.textContent}`,
    `Running ${running} / ${payload.max_concurrent_jobs} · Queued ${queued} · Finished ${finished} · Output ${defaultOutputRoot.textContent}`,
  );
}

function actionButton(label, className, handler, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.disabled = disabled;
  if (!disabled) button.addEventListener("click", handler);
  return button;
}

function renderJobCard(job) {
  const card = document.createElement("article");
  card.className = `job-card status-${job.status}`;
  if (job.job_id === selectedJobId) card.classList.add("selected");

  const title = document.createElement("h3");
  title.textContent = job.asset_name;
  const status = document.createElement("p");
  status.className = "job-status";
  status.textContent = statusLabel(job.status);
  const meta = document.createElement("p");
  meta.className = "job-meta";
  const created = new Date(job.created_at * 1000).toLocaleString(currentLang === "ja" ? "ja-JP" : "en-US");
  meta.textContent = `${job.include_animation ? "FBX + Animation" : "FBX"} · ${created} · ${job.job_id}`;
  const output = document.createElement("p");
  output.className = "job-output";
  output.textContent = job.output_dir || job.output_dir_hint || job.output_root || "-";
  const actions = document.createElement("div");
  actions.className = "job-actions";
  actions.append(
    actionButton(copy("詳細を見る", "View details"), "secondary-action", () => selectJob(job.job_id)),
  );
  if (["queued", "running"].includes(job.status)) {
    actions.append(
      actionButton(copy("変換を停止", "Cancel conversion"), "danger-action", () => cancelJob(job.job_id)),
    );
  } else if (job.status === "cancelling") {
    actions.append(actionButton(copy("停止中…", "Stopping…"), "danger-action", () => {}, true));
  } else if (completedStatuses.has(job.status)) {
    actions.append(
      actionButton(copy("出力フォルダを開く", "Open output folder"), "secondary-action", () => openOutput(job.job_id)),
    );
  }
  card.append(title, status, meta, output, actions);
  return card;
}

function fillJobList(target, jobs, emptyJa, emptyEn) {
  target.replaceChildren();
  if (!jobs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = copy(emptyJa, emptyEn);
    target.append(empty);
    return;
  }
  for (const job of jobs) target.append(renderJobCard(job));
}

function renderJobLists(jobs) {
  fillJobList(
    activeJobList,
    jobs.filter((job) => activeStatuses.has(job.status)),
    "実行中または待機中の変換はありません。",
    "No active or queued conversions.",
  );
  fillJobList(
    jobHistoryList,
    jobs.filter((job) => !activeStatuses.has(job.status)),
    "このセッションの履歴はまだありません。",
    "No finished conversions in this session.",
  );
}

async function renderSelectedJob() {
  const job = jobsById.get(selectedJobId);
  if (!job) {
    detailTitle.textContent = copy("ジョブを選択してください", "Select a job");
    detailStatus.textContent = "-";
    detailJobId.textContent = "-";
    detailOutput.textContent = "-";
    detailOpenOutput.disabled = true;
    jobLog.textContent = "VE2RBX OSS ready.";
    return;
  }
  detailTitle.textContent = job.asset_name;
  detailStatus.textContent = statusLabel(job.status);
  detailStatus.dataset.status = job.status;
  detailJobId.textContent = job.job_id;
  detailOutput.textContent = job.output_dir || job.output_dir_hint || job.output_root || "-";
  detailOpenOutput.disabled = !completedStatuses.has(job.status) || !job.output_dir;
  const requestedJobId = job.job_id;
  const response = await fetch(`/api/local/jobs/${requestedJobId}/log`);
  const log = await response.text();
  if (selectedJobId === requestedJobId) {
    jobLog.textContent = log || copy("ログはまだありません。", "No log yet.");
    jobLog.scrollTop = jobLog.scrollHeight;
  }
}

function refreshJobs() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const payload = await api("/api/local/jobs");
    lastJobsPayload = payload;
    jobsById.clear();
    for (const job of payload.jobs) jobsById.set(job.job_id, job);
    if (selectedJobId && !jobsById.has(selectedJobId)) selectedJobId = null;
    if (!selectedJobId && payload.jobs.length) selectedJobId = payload.jobs[0].job_id;
    renderSummary(payload);
    renderJobLists(payload.jobs);
    await renderSelectedJob();
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

function selectJob(jobId) {
  selectedJobId = jobId;
  renderJobLists(lastJobsPayload.jobs);
  renderSelectedJob().catch((error) => setFormFeedback(error.message, true));
}

async function cancelJob(jobId) {
  const warning = copy(
    "この変換を停止します。途中まで作成されたデータは削除されます。ほかの変換と完了済みデータには影響しません。",
    "This conversion will stop and its partial data will be deleted. Other conversions and completed data will not be affected.",
  );
  if (!window.confirm(warning)) return;
  const job = jobsById.get(jobId);
  if (job) {
    jobsById.set(jobId, { ...job, status: "cancelling" });
    renderJobLists(Array.from(jobsById.values()));
    await renderSelectedJob();
  }
  let requestFailed = false;
  try {
    const payload = await api(`/api/local/jobs/${jobId}/cancel`, { method: "POST" });
    jobsById.set(jobId, payload.job);
  } catch (error) {
    requestFailed = true;
    setFormFeedback(error.message, true);
  } finally {
    if (refreshPromise) {
      try {
        await refreshPromise;
      } catch (_error) {}
    }
    try {
      await refreshJobs();
    } catch (error) {
      if (!requestFailed) setFormFeedback(error.message, true);
    }
  }
}

async function openOutput(jobId) {
  try {
    await api(`/api/local/jobs/${jobId}/open-output`, { method: "POST" });
  } catch (error) {
    setFormFeedback(error.message, true);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const { files } = selectedFiles();
  if (!files.length) {
    setFormFeedback(copy("フォルダまたはZIPを選択してください。", "Choose a folder or ZIP."), true);
    return;
  }
  const formData = new FormData();
  appendFiles(formData, files);
  formData.append("include_animation", animationInput.checked ? "true" : "false");
  submitButton.disabled = true;
  setFormFeedback(copy("アップロード中…", "Uploading…"));
  try {
    const payload = await api("/api/local/convert", { method: "POST", body: formData });
    selectedJobId = payload.job.job_id;
    folderInput.value = "";
    zipInput.value = "";
    updateSelectionSummary();
    setFormFeedback(copy("変換キューに追加しました。", "Added to the conversion queue."));
    await refreshJobs();
  } catch (error) {
    setFormFeedback(error.message, true);
  } finally {
    submitButton.disabled = false;
  }
});

detailOpenOutput.addEventListener("click", () => {
  if (selectedJobId) openOutput(selectedJobId);
});

folderInput.addEventListener("change", () => {
  if (folderInput.files?.length) zipInput.value = "";
  updateSelectionSummary();
});

zipInput.addEventListener("change", () => {
  if (zipInput.files?.length) folderInput.value = "";
  updateSelectionSummary();
});

langToggle.addEventListener("click", () => {
  currentLang = currentLang === "ja" ? "en" : "ja";
  localStorage.setItem("ve2rbx-local-lang", currentLang);
  applyLang();
  updateSelectionSummary();
  renderSummary(lastJobsPayload);
  renderJobLists(lastJobsPayload.jobs);
  renderSelectedJob().catch(() => {});
});

function postSessionOpen() {
  if (!apiToken) return;
  fetch("/api/local/session/open", {
    method: "POST",
    headers: { "X-VE2RBX-Token": apiToken },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => {});
}

function notifySessionClosed() {
  if (!apiToken) return;
  const params = new URLSearchParams({ token: apiToken, session_id: sessionId });
  const url = `/api/local/session/close?${params.toString()}`;
  if (navigator.sendBeacon && navigator.sendBeacon(url, "")) return;
  fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}

window.addEventListener("pagehide", notifySessionClosed);
window.addEventListener("beforeunload", notifySessionClosed);

applyLang();
updateSelectionSummary();
configReady = fetch("/api/local/config")
  .then((response) => response.json())
  .then((payload) => {
    if (payload?.output_root) defaultOutputRoot.textContent = payload.output_root;
    if (payload?.api_token) {
      apiToken = payload.api_token;
      postSessionOpen();
    }
    return payload;
  });
configReady
  .then(refreshJobs)
  .then(() => {
    if (!pollTimer) pollTimer = setInterval(() => refreshJobs().catch(() => {}), 1500);
  })
  .catch((error) => setFormFeedback(error.message, true));
