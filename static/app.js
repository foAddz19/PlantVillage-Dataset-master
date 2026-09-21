"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { file: null, sample: null, previewUrl: null, busy: false, ready: false, result: null,
  catalog: null, view: "analyze", page: 0, pages: 1, lastModel: null, pollTimer: null, libraryRequest: 0,
  inputMode: "file", auto: false, autoTimer: null, requestId: 0, controller: null,
  requestSource: null, cameraResultUrl: null };
const token = $('meta[name="app-token"]').content;
const number = (value) => Number(value).toLocaleString("th-TH");
const percent = (value) => `${(value * 100).toFixed(1)}%`;
const splitNames = { train: "ภาพที่ใช้ฝึกโมเดล", test: "ภาพในชุดทดสอบ", unused: "ไม่ได้ใช้ฝึกโมเดลนี้" };
const camera = new LeafCamera({ onChange: updateAnalyzeButton, onStop: () => stopAutomatic(true) });

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-App-Token": token, ...options.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error("อ่านข้อมูลจากโปรแกรมไม่ได้ กรุณารีเฟรชหน้าเว็บ"); }
  if (!response.ok) throw new Error(data.error || "ดำเนินการไม่สำเร็จ");
  return data;
}
function toast(message) {
  $("#toast").textContent = message; $("#toast").hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { $("#toast").hidden = true; }, 6500);
}
function view(name) {
  if (!["analyze", "library", "model"].includes(name)) name = "analyze";
  if (name !== "analyze" && (camera.active || camera.opening)) camera.stop();
  state.view = name;
  document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== `view-${name}`; });
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
  $("#page-label").textContent = { analyze: "วิเคราะห์ใบพืช", library: "สำรวจชุดข้อมูล", model: "โมเดลของฉัน" }[name];
  history.replaceState(null, "", `#${name}`);
  if (name === "library") loadLibrary();
  window.scrollTo({ top: 0, behavior: "instant" });
}
document.querySelectorAll("[data-view]").forEach(el => el.addEventListener("click", () => view(el.dataset.view)));
$(".brand").addEventListener("click", event => { event.preventDefault(); view("analyze"); });

function updateAnalyzeButton() {
  const fromCamera = state.inputMode === "camera";
  const hasInput = fromCamera ? camera.active : (state.file || state.sample);
  $("#analyze-button").disabled = state.busy || !state.ready || !hasInput || (fromCamera && state.auto);
  $("#analyze-button span").textContent = state.busy ? "กำลังวิเคราะห์…" : fromCamera ? "วิเคราะห์ภาพจากกล้อง" : "วิเคราะห์ภาพ";
  $("#camera-auto").disabled = !state.auto && (!camera.active || !state.ready || state.busy);
  $("#camera-auto").textContent = state.auto ? "หยุดวิเคราะห์ต่อเนื่อง" : "เริ่มวิเคราะห์ต่อเนื่อง";
  $("#camera-auto").setAttribute("aria-pressed", String(state.auto));
  $("#camera-auto").classList.toggle("active", state.auto);
  $("#camera-interval").disabled = state.auto;
  $("#mode-camera").disabled = state.busy && state.requestSource !== "camera";
}
function resetResult() {
  state.result = null;
  releaseCameraResult();
  $("#result-empty").hidden = false; $("#result-content").hidden = true;
  $("#result-loading").hidden = true; $("#result-tag").textContent = "พร้อมวิเคราะห์";
}
function showPreview(url, name, detail) {
  $("#preview-image").src = url; $("#file-name").textContent = name;
  $("#file-detail").textContent = detail; $("#preview").hidden = false;
  $("#drop-zone").hidden = true; resetResult(); updateAnalyzeButton();
}
function releasePreview() {
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
}
function chooseFile(file) {
  if (!file || state.busy) return;
  if (!/\.(jpe?g|png|webp)$/i.test(file.name)) return toast("กรุณาเลือกภาพ JPG, PNG หรือ WebP");
  if (file.size > 8 * 1024 * 1024) return toast("กรุณาใช้ภาพขนาดไม่เกิน 8 MB");
  setInputMode("file");
  releasePreview(); state.file = file; state.sample = null;
  state.previewUrl = URL.createObjectURL(file);
  showPreview(state.previewUrl, file.name, `${(file.size / 1024 / 1024).toFixed(2)} MB · ภาพจากเครื่องของคุณ`);
}
function chooseSample(item) {
  if (state.busy && state.requestSource !== "camera") return toast("รอการวิเคราะห์ภาพปัจจุบันให้เสร็จก่อน");
  setInputMode("file");
  releasePreview(); state.file = null; state.sample = item;
  showPreview(item.url, `${item.crop_th} · ${item.disease_th}`, `ภาพจากชุดข้อมูล · ${splitNames[item.split]}`);
  view("analyze");
}
$("#image-input").addEventListener("change", event => chooseFile(event.target.files[0]));
$("#drop-zone").addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("#image-input").click(); } });
for (const type of ["dragenter", "dragover"]) $("#drop-zone").addEventListener(type, event => { event.preventDefault(); $("#drop-zone").classList.add("dragging"); });
for (const type of ["dragleave", "drop"]) $("#drop-zone").addEventListener(type, event => { event.preventDefault(); $("#drop-zone").classList.remove("dragging"); });
$("#drop-zone").addEventListener("drop", event => chooseFile(event.dataTransfer.files[0]));
$("#clear-image").addEventListener("click", () => {
  if (state.busy) return;
  releasePreview(); state.file = null; state.sample = null; $("#image-input").value = "";
  $("#preview-image").removeAttribute("src"); $("#preview").hidden = true; $("#drop-zone").hidden = false;
  resetResult(); $("#result-tag").textContent = "รอภาพของคุณ"; updateAnalyzeButton();
});

function releaseCameraResult() {
  if (state.cameraResultUrl) URL.revokeObjectURL(state.cameraResultUrl);
  state.cameraResultUrl = null;
  $("#camera-result-image").removeAttribute("src");
  $("#camera-result-info").hidden = true;
}

function setInputMode(mode) {
  if (mode === state.inputMode) return;
  if (state.busy && state.requestSource !== "camera") return;
  if (state.inputMode === "camera") camera.stop();
  state.inputMode = mode;
  $("#file-input-panel").hidden = mode !== "file";
  $("#camera-input-panel").hidden = mode !== "camera";
  for (const name of ["file", "camera"]) {
    $(`#mode-${name}`).classList.toggle("active", name === mode);
    $(`#mode-${name}`).setAttribute("aria-pressed", String(name === mode));
  }
  resetResult();
  updateAnalyzeButton();
}
$("#mode-file").addEventListener("click", () => setInputMode("file"));
$("#mode-camera").addEventListener("click", () => setInputMode("camera"));

function cancelCameraRequest() {
  if (state.requestSource !== "camera") return;
  state.controller?.abort();
  state.requestId++;
  state.controller = null;
  state.requestSource = null;
  state.busy = false;
  $("#clear-image").disabled = false;
  $("#result-loading").hidden = true;
  $("#result-empty").hidden = Boolean(state.result);
  $("#result-content").hidden = !state.result;
  $("#result-tag").textContent = state.result ? "ผลจากภาพล่าสุด" : "พร้อมวิเคราะห์";
}

function stopAutomatic(cancel = true) {
  state.auto = false;
  clearTimeout(state.autoTimer);
  state.autoTimer = null;
  if (cancel) cancelCameraRequest();
  $("#camera-auto-status").textContent = "หยุดวิเคราะห์ต่อเนื่องแล้ว สามารถกดวิเคราะห์ภาพครั้งเดียวได้";
  updateAnalyzeButton();
}

function scheduleCameraFrame(delay) {
  clearTimeout(state.autoTimer);
  if (!state.auto || !camera.active || document.hidden) return;
  state.autoTimer = setTimeout(() => runAnalysis(true), delay);
}

$("#camera-auto").addEventListener("click", () => {
  if (state.auto) return stopAutomatic(true);
  if (!camera.active || !state.ready || state.busy) return;
  state.auto = true;
  $("#camera-auto-status").textContent = "กำลังวิเคราะห์ต่อเนื่อง ส่งภาพครั้งละหนึ่งภาพ";
  updateAnalyzeButton();
  scheduleCameraFrame(0);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden && (camera.active || camera.opening)) camera.stop("ปิดกล้องเมื่อออกจากแท็บแล้ว กดเปิดกล้องเพื่อเริ่มใหม่");
});
window.addEventListener("pagehide", () => { camera.stop(); releaseCameraResult(); releasePreview(); });

async function runAnalysis(automatic = false) {
  const fromCamera = state.inputMode === "camera";
  if (state.busy || !state.ready || (fromCamera ? !camera.active : !(state.file || state.sample))) return;
  if (automatic && !state.auto) return;
  const requestId = ++state.requestId;
  const controller = new AbortController();
  state.controller = controller;
  state.requestSource = fromCamera ? "camera" : "file";
  const timeout = setTimeout(() => controller.abort(), 20000);
  state.busy = true; updateAnalyzeButton(); $("#clear-image").disabled = true;
  const keepPrevious = automatic && Boolean(state.result);
  $("#result-empty").hidden = true; $("#result-content").hidden = !keepPrevious; $("#result-loading").hidden = keepPrevious;
  $("#result-tag").textContent = "กำลังวิเคราะห์";
  try {
    let result, frame;
    if (fromCamera) {
      frame = await camera.capture();
      if (requestId !== state.requestId) return;
      const form = new FormData(); form.append("image", frame.blob, "camera-frame.jpg");
      result = await api("/api/predict", { method: "POST", body: form, signal: controller.signal });
      result = { ...result, source: "camera", captured_at: frame.capturedAt, crop_size: frame.size };
    } else if (state.sample) {
      result = await api("/api/predict-example", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: state.sample.label, index: state.sample.index }), signal: controller.signal });
    } else {
      const form = new FormData(); form.append("image", state.file);
      result = await api("/api/predict", { method: "POST", body: form, signal: controller.signal });
    }
    if (requestId !== state.requestId) return;
    state.result = { ...result, filename: fromCamera ? "camera-frame.jpg" : state.file ? state.file.name : "dataset-example",
      analyzed_at: new Date().toISOString() };
    renderResult(state.result);
    releaseCameraResult();
    if (frame) {
      state.cameraResultUrl = URL.createObjectURL(frame.blob);
      $("#camera-result-image").src = state.cameraResultUrl;
      $("#camera-result-time").textContent = `${new Date(frame.capturedAt).toLocaleTimeString("th-TH")} · ${frame.size} × ${frame.size} พิกเซล`;
      $("#camera-result-info").hidden = false;
      if (state.auto) $("#camera-auto-status").textContent = `อัปเดตแล้ว · รอ ${Number($("#camera-interval").value) / 1000} วินาทีก่อนวิเคราะห์ภาพถัดไป`;
    }
  } catch (error) {
    if (requestId !== state.requestId) return;
    stopAutomatic(false);
    resetResult(); $("#result-tag").textContent = "วิเคราะห์ไม่สำเร็จ";
    toast(error.name === "AbortError" ? "วิเคราะห์นานเกินไป กรุณาตรวจว่าโปรแกรมยังเปิดอยู่แล้วลองใหม่" : error.message);
  } finally {
    clearTimeout(timeout);
    if (requestId === state.requestId) {
      state.busy = false; state.controller = null; state.requestSource = null;
      $("#clear-image").disabled = false; $("#result-loading").hidden = true; updateAnalyzeButton();
      if (automatic && state.auto) scheduleCameraFrame(Number($("#camera-interval").value));
    }
  }
}
$("#analyze-button").addEventListener("click", () => runAnalysis());

function renderResult(result) {
  $("#result-content").hidden = false; $("#result-tag").textContent = "วิเคราะห์เสร็จแล้ว";
  const best = result.candidates[0];
  $("#pred-disease").textContent = best.disease_th; $("#pred-crop").textContent = best.crop_th;
  $("#pred-label").textContent = best.label;
  $("#result-warning").textContent = result.uncertain ? "ผลยังไม่ชัดเจน · ลองถ่ายใหม่ให้เห็นใบเต็มใบ และเปรียบเทียบหลายอาการ" : "ผลคาดการณ์เบื้องต้น · ควรตรวจอาการจริงประกอบ";
  $("#candidates").replaceChildren();
  result.candidates.forEach(item => {
    const row = document.createElement("div"); row.className = "candidate";
    const top = document.createElement("div"); top.className = "candidate-top";
    const label = document.createElement("span"); label.textContent = `${item.crop_th} · ${item.disease_th}`;
    const score = document.createElement("b"); score.textContent = percent(item.score); top.append(label, score);
    const track = document.createElement("div"); track.className = "candidate-track";
    const fill = document.createElement("div"); fill.style.width = percent(item.score); track.append(fill);
    row.append(top, track); $("#candidates").append(row);
  });
  $("#sample-note").hidden = result.source !== "dataset";
  if (result.source === "dataset") {
    const matched = state.catalog.classes.find(item => item.label === result.actual_label);
    $("#sample-note").textContent = `ป้ายกำกับจริง: ${matched ? matched.crop_th + " · " + matched.disease_th : result.actual_label} — ${splitNames[result.split]}${result.split === "train" ? " ผลภาพนี้ใช้ประเมินความแม่นยำไม่ได้" : ""}`;
  }
}
$("#download-result").addEventListener("click", () => {
  if (!state.result) return;
  const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob); const link = document.createElement("a");
  link.href = url; link.download = `leaflab-${Date.now()}.json`; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

function sampleCard(item, showSplit = false) {
  const button = document.createElement("button"); button.className = "sample-card";
  const img = document.createElement("img"); img.src = item.url; img.alt = `${item.crop_th} ${item.disease_th}`; img.loading = "lazy";
  const body = document.createElement("div"); body.className = "sample-card-body";
  const name = document.createElement("strong"); name.textContent = item.crop_th;
  const disease = document.createElement("small"); disease.textContent = item.disease_th; body.append(name, disease);
  if (showSplit) { const badge = document.createElement("span"); badge.className = "split-badge"; badge.textContent = splitNames[item.split]; body.append(badge); }
  button.append(img, body); button.addEventListener("click", () => chooseSample(item)); return button;
}
async function loadLibrary() {
  const requestId = ++state.libraryRequest;
  try {
    const data = await api(`/api/examples?${new URLSearchParams({ label: $("#class-filter").value, page: state.page })}`);
    if (requestId !== state.libraryRequest) return;
    state.pages = data.pages; $("#library-grid").replaceChildren(...data.items.map(item => sampleCard(item, true)));
    $("#library-count").textContent = `${number(data.total)} รายการ`;
    $("#page-number").textContent = `${state.page + 1} / ${state.pages}`;
    $("#prev-page").disabled = state.page === 0; $("#next-page").disabled = state.page + 1 >= state.pages;
  } catch (error) { toast(error.message); }
}
$("#class-filter").addEventListener("change", () => { state.page = 0; loadLibrary(); });
$("#prev-page").addEventListener("click", () => { if (state.page > 0) { state.page--; loadLibrary(); } });
$("#next-page").addEventListener("click", () => { if (state.page + 1 < state.pages) { state.page++; loadLibrary(); } });

function metric(value, title) { const div = document.createElement("div"); div.className = "metric"; const strong = document.createElement("strong"); strong.textContent = value; const span = document.createElement("span"); span.textContent = title; div.append(strong, span); return div; }
function renderMetadata(metadata) {
  if (!metadata || state.lastModel === metadata.created_at) return;
  state.lastModel = metadata.created_at;
  $("#model-metrics").replaceChildren(metric(percent(metadata.accuracy), "ทำนายอันดับ 1 ถูกต้อง"), metric(percent(metadata.top3_accuracy), "คำตอบจริงอยู่ใน 3 อันดับ"), metric(number(metadata.train_images), "ภาพที่ใช้ฝึก"), metric(number(metadata.test_images), "ภาพที่ใช้ทดสอบ"));
  const date = document.createElement("div"); date.className = "model-date";
  date.textContent = `${metadata.algorithm} · ฝึกเมื่อ ${new Date(metadata.created_at).toLocaleString("th-TH")} · ใช้เวลา ${Math.round(metadata.seconds)} วินาที`;
  $("#model-metrics").append(date); $("#class-report").hidden = false; $("#report-body").replaceChildren();
  (state.catalog?.classes || []).forEach(item => {
    const scores = metadata.report[item.label]; if (!scores) return;
    const row = document.createElement("tr"); const name = document.createElement("td"); name.textContent = `${item.crop_th} · ${item.disease_th}`;
    const original = document.createElement("small"); original.textContent = item.label; name.append(original); row.append(name);
    for (const value of [percent(scores.precision), percent(scores.recall), percent(scores["f1-score"]), number(scores.support)]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    $("#report-body").append(row);
  });
  if (state.view === "library") loadLibrary();
}
function renderStatus(data) {
  state.ready = data.ready;
  if (!data.ready && state.auto) stopAutomatic(true);
  $("#pill-text").textContent = data.running ? `กำลังฝึก ${data.percent}%` : data.ready ? "โมเดลพร้อมใช้งาน" : "ยังไม่มีโมเดล";
  $("#model-pill").classList.toggle("offline", !data.ready);
  $("#no-model").hidden = data.ready; $("#train-button").disabled = data.running;
  $("#training-size").disabled = data.running;
  $("#model-stage").textContent = data.running ? "กำลังฝึก" : data.ready ? "พร้อมใช้งาน" : "รอฝึกโมเดล";
  $("#training-progress").hidden = data.stage === "idle" || (data.ready && data.stage === "ready" && !data.running);
  $("#progress-fill").style.width = `${data.percent}%`; $("#progress-message").textContent = data.message;
  renderMetadata(data.metadata); updateAnalyzeButton();
}
async function pollStatus() {
  clearTimeout(state.pollTimer);
  let running = false;
  try { const data = await api("/api/status"); renderStatus(data); running = data.running; }
  catch { $("#pill-text").textContent = "เชื่อมต่อโปรแกรมไม่ได้"; $("#model-pill").classList.add("offline"); state.ready = false; stopAutomatic(true); updateAnalyzeButton(); }
  state.pollTimer = setTimeout(pollStatus, running ? 2000 : 8000);
}
$("#train-button").addEventListener("click", async () => {
  $("#train-button").disabled = true;
  try { const data = await api("/api/train", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ samples_per_class: Number($("#training-size").value) }) }); renderStatus(data); toast("เริ่มฝึกโมเดลแล้ว เปิดหน้าต่างโปรแกรมไว้จนเสร็จ"); pollStatus(); }
  catch (error) { toast(error.message); $("#train-button").disabled = false; }
});
async function initialize() {
  view(location.hash.slice(1) || "analyze");
  try {
    state.catalog = await api("/api/catalog");
    $("#stat-crops").textContent = number(state.catalog.crop_count); $("#stat-classes").textContent = number(state.catalog.class_count); $("#stat-images").textContent = number(state.catalog.images);
    state.catalog.classes.forEach(item => { const option = document.createElement("option"); option.value = item.label; option.textContent = `${item.crop_th} · ${item.disease_th} (${number(item.count)} ภาพ)`; $("#class-filter").append(option); });
    const featured = ["Apple___Apple_scab", "Grape___Black_rot", "Tomato___healthy", "Corn_(maize)___Common_rust_", "Potato___Early_blight", "Strawberry___Leaf_scorch"];
    const images = await Promise.all(featured.map(label => api(`/api/examples?${new URLSearchParams({ label })}`)));
    $("#quick-samples").replaceChildren(...images.filter(data => data.items.length).map(data => sampleCard(data.items[0])));
  } catch (error) { toast(error.message); }
  await pollStatus();
}
initialize();
