const form = document.querySelector("#convert-form");
const folderInput = document.querySelector("#folder-input");
const zipInput = document.querySelector("#zip-input");
const animationInput = document.querySelector("#animation-input");
const submitButton = document.querySelector("#submit-button");
const statusText = document.querySelector("#status-text");
const jobIdEl = document.querySelector("#job-id");
const outputPathEl = document.querySelector("#output-path");
const logBox = document.querySelector("#log-box");
const openOutputButton = document.querySelector("#open-output-button");
const langToggle = document.querySelector("#lang-toggle");

let currentLang = localStorage.getItem("ve2rbx-local-lang") || "ja";
let currentJobId = null;
let pollTimer = null;
let apiToken = null;

function applyLang() {
  document.documentElement.lang = currentLang;
  document.body.dataset.lang = currentLang;
  langToggle.textContent = currentLang === "ja" ? "EN" : "JP";
}

function copy(ja, en) {
  return currentLang === "ja" ? ja : en;
}

function setStatus(ja, en) {
  statusText.textContent = copy(ja, en);
}

async function api(path, options = {}) {
  const requestOptions = { ...options };
  const method = (requestOptions.method || "GET").toUpperCase();
  if (method !== "GET") {
    await configReady.catch(() => {});
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
  if (zipFile) {
    return { mode: "zip", files: [zipFile] };
  }
  const folderFiles = Array.from(folderInput.files || []);
  return { mode: "folder", files: folderFiles };
}

function appendFiles(formData, files) {
  for (const file of files) {
    const relativeName = file.webkitRelativePath || file.name;
    formData.append("files", file, relativeName);
  }
}

async function refreshJob() {
  if (!currentJobId) return;
  const [jobPayload, logText] = await Promise.all([
    api(`/api/local/jobs/${currentJobId}`),
    fetch(`/api/local/jobs/${currentJobId}/log`).then((response) => response.text()),
  ]);

  const job = jobPayload.job;
  jobIdEl.textContent = job.job_id;
  outputPathEl.textContent = job.output_dir || job.output_dir_hint || job.output_root || "-";
  logBox.textContent = logText || copy("ログはまだありません。", "No log yet.");
  logBox.scrollTop = logBox.scrollHeight;
  openOutputButton.disabled = !job.output_dir;

  if (job.status === "queued") setStatus("待機中", "Queued");
  if (job.status === "running") setStatus("変換中", "Running");
  if (job.status === "succeeded") setStatus("完了", "Completed");
  if (job.status === "failed") setStatus("失敗", "Failed");

  if (job.status === "succeeded" || job.status === "failed") {
    clearInterval(pollTimer);
    pollTimer = null;
    submitButton.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const { files } = selectedFiles();
  if (!files.length) {
    setStatus("フォルダまたはZIPを選択してください", "Choose a folder or ZIP");
    return;
  }

  const formData = new FormData();
  appendFiles(formData, files);
  formData.append("include_animation", animationInput.checked ? "true" : "false");

  submitButton.disabled = true;
  openOutputButton.disabled = true;
  setStatus("アップロード中", "Uploading");
  logBox.textContent = copy("アップロードを受け付けています...", "Uploading files...");

  try {
    const payload = await api("/api/local/convert", {
      method: "POST",
      body: formData,
    });
    currentJobId = payload.job.job_id;
    jobIdEl.textContent = currentJobId;
    setStatus("変換中", "Running");
    await refreshJob();
    pollTimer = setInterval(refreshJob, 1500);
  } catch (error) {
    submitButton.disabled = false;
    setStatus("開始できませんでした", "Could not start");
    logBox.textContent = error.message;
  }
});

openOutputButton.addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await api(`/api/local/jobs/${currentJobId}/open-output`, { method: "POST" });
  } catch (error) {
    logBox.textContent += `\n${error.message}`;
  }
});

folderInput.addEventListener("change", () => {
  if (folderInput.files?.length) zipInput.value = "";
});

zipInput.addEventListener("change", () => {
  if (zipInput.files?.length) folderInput.value = "";
});

langToggle.addEventListener("click", () => {
  currentLang = currentLang === "ja" ? "en" : "ja";
  localStorage.setItem("ve2rbx-local-lang", currentLang);
  applyLang();
});

applyLang();
const configReady = fetch("/api/local/config")
  .then((response) => response.json())
  .then((payload) => {
    if (payload?.output_root) outputPathEl.textContent = payload.output_root;
    if (payload?.api_token) apiToken = payload.api_token;
  })
  .catch(() => {});
