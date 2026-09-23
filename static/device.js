"use strict";
const d = selector => document.querySelector(selector);
const device = { ready:false, busy:false, saving:false, blob:null, source:"upload", preview:null,
  stream:null, opening:false, cameraGeneration:0, requestGeneration:0, controller:null, draft:null,
  records:[], total:0, examples:[], sampleIndex:0, map:null, markers:null, currentMarker:null, tiles:null,
  tab:"capture", led:false, historyGeneration:0, pollTimer:null };
const deviceToken = d('meta[name="app-token"]').content;
const percent = value => `${(value*100).toFixed(1)}%`;
const locationName = source => source === "simulated" ? "พิกัดจำลอง" : "พิกัดระบุเอง";
const dateText = value => new Date(value).toLocaleString("th-TH");
function message(text, error=false) { d("#device-message").textContent=text; d("#device-message").classList.toggle("error",error); d("#device-message").hidden=false; clearTimeout(message.timer); message.timer=setTimeout(()=>{d("#device-message").hidden=true;},6000); }
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{"X-App-Token":deviceToken,...options.headers}});
  let result; try { result=await response.json(); } catch { throw new Error("อ่านข้อมูลไม่ได้ กรุณารีเฟรชหน้าเว็บ"); }
  if(!response.ok) throw new Error(result.error || "ดำเนินการไม่สำเร็จ");
  return result;
}
function buttons() {
  d("#analyze-scan").disabled=!device.ready || device.busy || device.opening || (!device.stream && !device.blob);
  d("#analyze-scan").textContent=device.busy ? "กำลังตรวจ…" : "◉ ถ่าย / ตรวจโรค";
  d("#save-scan").disabled=!device.draft || !!device.draft.saved_at || device.busy || device.saving;
  d("#save-scan").textContent=device.saving ? "กำลังบันทึก…" : device.draft?.saved_at ? "✓ บันทึกแล้ว" : "▣ บันทึกจุดตรวจ";
  d("#open-camera").disabled=device.busy || !!device.stream || device.opening;
  d("#open-camera").textContent=device.opening ? "กำลังเปิด…" : "เปิดกล้อง";
  d("#close-camera").disabled=!device.stream && !device.opening;
  for(const id of ["#sample-image","#device-file"]) d(id).disabled=device.busy || device.saving;
  for(const id of ["#observer-severity","#scan-note"]) d(id).disabled=!!device.draft?.saved_at || device.saving;
}
function locationSnapshot() {
  const latitude=Number(d("#latitude").value),longitude=Number(d("#longitude").value);
  if(!d("#latitude").value.trim() || !d("#longitude").value.trim() || !Number.isFinite(latitude) || !Number.isFinite(longitude) || Math.abs(latitude)>90 || Math.abs(longitude)>180) throw new Error("กรุณาระบุพิกัดให้ถูกต้องในหน้าตั้งค่าจำลอง");
  return {latitude,longitude,location_source:d("#location-source").value};
}
function updateLocation() {
  try {
    const point=locationSnapshot(); d("#gps-status").textContent=`${locationName(point.location_source)} · ${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
    if(device.map) {
      if(device.currentMarker) device.currentMarker.remove();
      device.currentMarker=L.circleMarker([point.latitude,point.longitude],{radius:12,color:"#245c55",weight:2,dashArray:"4 4",fill:false}).addTo(device.map);
      device.currentMarker.bindTooltip("ตำแหน่งปัจจุบัน · ยังไม่บันทึก");
    }
  } catch { d("#gps-status").textContent="พิกัดไม่ถูกต้อง"; }
}
function releasePreview() { if(device.preview) URL.revokeObjectURL(device.preview); device.preview=null; }
function clearResult() { device.draft=null; d("#device-result").hidden=true; d("#device-result-empty").hidden=false; d("#result-state").textContent="ยังไม่ได้ตรวจ"; d("#observer-severity").value="unassessed"; d("#scan-note").value=""; buttons(); }
function cancelAnalysis() { device.requestGeneration++; device.controller?.abort(); device.controller=null; device.busy=false; buttons(); }
function stopCamera() {
  device.cameraGeneration++; device.opening=false;
  device.stream?.getTracks().forEach(track=>track.stop()); device.stream=null;
  d("#device-video").pause(); d("#device-video").srcObject=null; d("#device-video").hidden=true;
  d("#capture-guide").hidden=true;
  d("#camera-empty").hidden=!!device.blob;
  d("#device-preview").hidden=!device.blob;
  if(device.blob) updateGuide();
  buttons();
}
function updateGuide() {
  const media=device.stream ? d("#device-video") : d("#device-preview");
  const width=media.videoWidth || media.naturalWidth, height=media.videoHeight || media.naturalHeight;
  if(!width || !height || media.hidden) return;
  const stage=d("#viewfinder"), size=Math.min(width,height)*Math.min(stage.clientWidth/width,stage.clientHeight/height);
  d("#capture-guide").style.width=`${size}px`; d("#capture-guide").style.height=`${size}px`; d("#capture-guide").hidden=false;
}
new ResizeObserver(updateGuide).observe(d("#viewfinder"));
d("#device-video").addEventListener("resize",updateGuide); d("#device-preview").addEventListener("load",updateGuide);
async function openCamera() {
  cancelAnalysis(); stopCamera();
  if(!navigator.mediaDevices?.getUserMedia) return message("เปิดกล้องผ่าน localhost ใน Edge หรือ Chrome",true);
  const generation=++device.cameraGeneration; device.opening=true; buttons(); message("กรุณาอนุญาตการใช้กล้อง");
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{width:{ideal:1280},height:{ideal:720},facingMode:{ideal:"environment"}}});
    if(generation!==device.cameraGeneration || document.hidden) {stream.getTracks().forEach(track=>track.stop());return;}
    device.stream=stream; d("#device-video").srcObject=stream;
    let timeout;
    try { await Promise.race([d("#device-video").play(),new Promise((_,reject)=>{timeout=setTimeout(()=>reject(new Error("กล้องไม่ส่งภาพ กรุณาปิดโปรแกรมอื่นที่ใช้กล้องแล้วลองใหม่")),12000);})]); }
    finally { clearTimeout(timeout); }
    if(generation!==device.cameraGeneration) return;
    stream.getVideoTracks().forEach(track=>track.addEventListener("ended",()=>{if(device.stream===stream){cancelAnalysis();stopCamera();message("กล้องถูกตัดการเชื่อมต่อ",true);}}));
    releasePreview(); device.blob=null; device.source="camera"; clearResult(); device.opening=false;
    d("#device-video").hidden=false; d("#device-preview").hidden=true; d("#camera-empty").hidden=true;
    d("#source-label").textContent="ภาพสดจากกล้อง"; updateGuide(); buttons(); message("กล้องพร้อมแล้ว วางใบพืชในกรอบแล้วกดตรวจโรค");
  } catch(error) {
    if(generation!==device.cameraGeneration)return;
    stopCamera(); message(({NotAllowedError:"ไม่ได้รับอนุญาตให้ใช้กล้อง",NotFoundError:"ไม่พบกล้อง ใช้ไฟล์หรือภาพทดลองก่อนได้",NotReadableError:"เปิดกล้องไม่ได้ กรุณาตรวจสายและปิดโปรแกรมอื่นที่ใช้กล้อง"})[error.name] || error.message,true);
  }
}
function chooseImage(blob,source) {
  if(device.busy || device.saving)return;
  if(blob.size>8*1024*1024)return message("เลือกภาพขนาดไม่เกิน 8 MB",true);
  stopCamera(); releasePreview(); clearResult(); device.blob=blob; device.source=source;
  device.preview=URL.createObjectURL(blob); d("#device-preview").src=device.preview;
  d("#device-preview").hidden=false; d("#camera-empty").hidden=true;
  d("#source-label").textContent=source==="sample" ? "ภาพทดลองจาก PlantVillage" : "ไฟล์จากเครื่อง";
  buttons(); message("เลือกภาพแล้ว กดถ่าย / ตรวจโรคเพื่อวิเคราะห์");
}
async function cameraFrame() {
  const video=d("#device-video");
  if(video.readyState<2 || !video.videoWidth)throw new Error("กล้องยังไม่มีภาพ กรุณารอสักครู่");
  const canvas=document.createElement("canvas");canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext("2d").drawImage(video,0,0);
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/png"));
  if(!blob || blob.size>8*1024*1024)throw new Error("ภาพจากกล้องใหญ่เกินไปหรือจับภาพไม่ได้ กรุณาลดความละเอียดกล้อง");
  return blob;
}
function renderResult(row) {
  const result=row.prediction,best=result.candidates[0];
  d("#device-result-empty").hidden=true; d("#device-result").hidden=false;
  d("#analyzed-frame").src=row.image_url;
  d("#result-disease").textContent=result.uncertain ? "ยังระบุไม่ได้" : best.disease_th;
  d("#result-crop").textContent=result.uncertain ? "ลองถ่ายใหม่ให้เห็นใบชัดเจน" : best.crop_th;
  d("#result-time").textContent=dateText(row.captured_at);
  d("#uncertainty").textContent=result.uncertain ? "คะแนนยังไม่ชัดเจน · ผลด้านล่างเป็นเพียงกลุ่มที่ใกล้เคียง" : "ผลคาดการณ์เบื้องต้น · ตรวจอาการจริงประกอบ";
  d("#device-candidates").replaceChildren();
  for(const item of result.candidates) {
    const box=document.createElement("div");box.className="rank-row";
    const line=document.createElement("div"),label=document.createElement("span"),score=document.createElement("span"),bar=document.createElement("progress");
    label.textContent=`${item.crop_th} · ${item.disease_th}`;score.textContent=percent(item.score);bar.max=1;bar.value=item.score;
    line.append(label,score);box.append(line,bar);d("#device-candidates").append(box);
  }
  d("#result-location").textContent=`${locationName(row.location_source)} · ${row.latitude.toFixed(5)}, ${row.longitude.toFixed(5)}`;
  d("#result-state").textContent=row.saved_at ? "บันทึกแล้ว" : "ตรวจแล้ว · ยังไม่บันทึก";
  d("#observer-severity").value=row.severity;d("#scan-note").value=row.note;
  buttons();
}
async function analyze() {
  if(device.busy || !device.ready)return;
  let location;try{location=locationSnapshot();}catch(error){message(error.message,true);return;}
  const generation=++device.requestGeneration, controller=new AbortController();device.controller=controller;
  device.busy=true;buttons();message("กำลังตรวจด้วย AI บนเครื่อง…");
  const timeout=setTimeout(()=>controller.abort(),30000);
  try {
    const source=device.stream ? "camera" : device.source, blob=device.stream ? await cameraFrame() : device.blob;
    if(generation!==device.requestGeneration)return;
    if(!blob)throw new Error("กรุณาเลือกภาพก่อน");
    const form=new FormData();form.append("image",blob,"scan-image");form.append("source",source);
    Object.entries(location).forEach(([key,value])=>form.append(key,value));
    const row=await api("/api/device/analyze",{method:"POST",body:form,signal:controller.signal});
    if(generation!==device.requestGeneration)return;
    device.draft=row;renderResult(row);message("ตรวจเสร็จแล้ว กดบันทึกจุดตรวจเพื่อเก็บลงสมุดและแผนที่");
  } catch(error) { if(generation===device.requestGeneration)message(error.name==="AbortError" ? "การตรวจใช้เวลานานเกินไป กรุณาลองใหม่" : error.message,true); }
  finally {clearTimeout(timeout);if(generation===device.requestGeneration){device.busy=false;device.controller=null;buttons();}}
}
async function saveScan() {
  if(!device.draft || device.draft.saved_at || device.saving)return;
  const identifier=device.draft.id;device.saving=true;buttons();
  try {
    const row=await api(`/api/device/scans/${identifier}/save`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({severity:d("#observer-severity").value,note:d("#scan-note").value})});
    if(device.draft?.id===identifier){device.draft=row;renderResult(row);}
    await loadHistory();message("บันทึกภาพ ผลตรวจ และพิกัดลงฐานข้อมูลแล้ว");
  }catch(error){message(error.message,true);}finally{device.saving=false;buttons();}
}
function markerColor(row) {
  if(row.severity==="severe")return "#d04f46";
  if(row.severity==="mild")return "#d4a21a";
  if(!row.prediction.uncertain && row.prediction.candidates[0].healthy)return "#438b52";
  return "#81908d";
}
function recordTitle(row) {const best=row.prediction.candidates[0];return row.prediction.uncertain ? "ยังระบุไม่ได้" : `${best.crop_th} · ${best.disease_th}`;}
function popup(row) {
  const box=document.createElement("div"),image=document.createElement("img"),title=document.createElement("strong"),detail=document.createElement("p");
  image.src=row.image_url;image.alt="ภาพใบพืชที่บันทึก";title.textContent=recordTitle(row);
  detail.textContent=`${locationName(row.location_source)} · ${dateText(row.captured_at)}${row.note ? " · "+row.note : ""}`;
  box.append(image,title,detail);return box;
}
function initializeMap() {
  if(device.map)return;
  device.map=L.map("field-map",{zoomControl:true}).setView([16.053,103.652],17);
  L.control.scale({imperial:false}).addTo(device.map);device.markers=L.featureGroup().addTo(device.map);updateLocation();
}
function renderMap() {
  if(!device.map)return;
  device.markers.clearLayers();d("#map-list").replaceChildren();
  const filter=d("#map-filter").value, rows=device.records.filter(row=>filter==="all" || row.location_source===filter);
  d("#map-count").textContent=`แสดง ${rows.length} จุดบันทึก`;
  for(const row of rows) {
    const marker=L.circleMarker([row.latitude,row.longitude],{radius:9,color:"white",weight:2,fillColor:markerColor(row),fillOpacity:.95}).addTo(device.markers).bindPopup(popup(row));
    const button=document.createElement("button");button.className="map-record";button.textContent=recordTitle(row);
    const detail=document.createElement("small");detail.textContent=`${locationName(row.location_source)} · ${dateText(row.captured_at)}`;button.append(detail);
    button.addEventListener("click",()=>{device.map.setView(marker.getLatLng(),17);marker.openPopup();});d("#map-list").append(button);
  }
}
function fitPoints() {if(device.markers?.getLayers().length)device.map.fitBounds(device.markers.getBounds(),{padding:[30,30],maxZoom:18});else{try{const point=locationSnapshot();device.map.setView([point.latitude,point.longitude],17);}catch{}}}
function showTab(tab) {
  if(tab!=="capture"){cancelAnalysis();stopCamera();}
  device.tab=tab;
  for(const name of ["capture","map","history","settings"])d(`#screen-${name}`).hidden=name!==tab;
  document.querySelectorAll("[data-tab]").forEach(button=>{button.classList.toggle("active",button.dataset.tab===tab);button.setAttribute("aria-pressed",String(button.dataset.tab===tab));});
  if(tab==="map"){initializeMap();device.map.invalidateSize();renderMap();fitPoints();}
  if(tab==="history" || tab==="map")loadHistory().catch(error=>message(error.message,true));
  if(tab==="capture")updateGuide();
}
function renderHistory() {
  d("#history-count").textContent=`ทั้งหมด ${device.total} จุด · แสดง ${device.records.length} รายการล่าสุด`;
  d("#saved-count").textContent=`${device.total} จุดบันทึก`;
  d("#history-list").replaceChildren();
  if(!device.records.length){const p=document.createElement("p");p.className="empty";p.textContent="ยังไม่มีจุดตรวจ ลองเลือกภาพ ตรวจโรค แล้วกดบันทึกจุดตรวจ";d("#history-list").append(p);return;}
  for(const row of device.records){
    const card=document.createElement("article");card.className="scan-card";
    const img=document.createElement("img");img.src=row.image_url;img.alt="ภาพใบพืชที่บันทึก";img.loading="lazy";
    const body=document.createElement("div"),title=document.createElement("strong"),details=document.createElement("small"),note=document.createElement("p"),tag=document.createElement("span"),button=document.createElement("button");
    title.textContent=recordTitle(row);details.textContent=`${dateText(row.captured_at)} · ${row.latitude.toFixed(5)}, ${row.longitude.toFixed(5)}`;
    note.textContent=row.note;tag.className="simulation-tag";tag.textContent=locationName(row.location_source);body.append(title,details,tag,note);
    button.textContent="ดูบนแผนที่ ↗";button.addEventListener("click",()=>{showTab("map");device.map.setView([row.latitude,row.longitude],18);L.popup().setLatLng([row.latitude,row.longitude]).setContent(popup(row)).openOn(device.map);});
    card.append(img,body,button);d("#history-list").append(card);
  }
}
async function loadHistory() {const generation=++device.historyGeneration;const data=await api("/api/device/scans");if(generation!==device.historyGeneration)return;device.records=data.items;device.total=data.total;renderHistory();renderMap();}
async function status() {
  clearTimeout(device.pollTimer);
  try {const data=await api("/api/status");device.ready=data.ready;d("#ai-status").textContent=data.ready ? "● AI พร้อมตรวจ" : "● ยังไม่มีโมเดล AI";}
  catch{device.ready=false;d("#ai-status").textContent="● เชื่อมต่อโปรแกรมไม่ได้";}
  buttons();device.pollTimer=setTimeout(status,15000);
}
d("#open-camera").addEventListener("click",openCamera);
d("#close-camera").addEventListener("click",()=>{cancelAnalysis();stopCamera();message("ปิดกล้องแล้ว");});
d("#device-file").addEventListener("change",event=>{const file=event.target.files[0];if(file)chooseImage(file,"upload");});
d("#sample-image").addEventListener("click",async()=>{try{if(!device.examples.length)device.examples=(await api("/api/examples")).items;const item=device.examples[device.sampleIndex++%device.examples.length];if(!item)throw new Error("ยังไม่มีชุดภาพทดลอง เลือกไฟล์หรือเปิดกล้องได้");const response=await fetch(item.url);if(!response.ok)throw new Error("โหลดภาพทดลองไม่ได้");chooseImage(await response.blob(),"sample");}catch(error){message(error.message,true);}});
d("#analyze-scan").addEventListener("click",analyze);d("#save-scan").addEventListener("click",saveScan);
document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>showTab(button.dataset.tab)));
d("#go-map").addEventListener("click",()=>showTab("map"));d("#fit-points").addEventListener("click",fitPoints);
d("#map-filter").addEventListener("change",()=>{renderMap();fitPoints();});
d("#refresh-history").addEventListener("click",()=>loadHistory().catch(error=>message(error.message,true)));
d("#online-map").addEventListener("click",()=>{
  if(device.tiles){device.tiles.remove();device.tiles=null;d("#map-mode").textContent="พิกัดออฟไลน์ · ยังไม่โหลดแผนที่ถนน";}
  else{device.tiles=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',referrerPolicy:"strict-origin-when-cross-origin"}).addTo(device.map);device.tiles.on("tileerror",()=>{d("#map-mode").textContent="โหลดแผนที่ถนนไม่ได้ · จุดตรวจยังดูได้";});d("#map-mode").textContent="แผนที่ถนนจาก OpenStreetMap · ใช้อินเทอร์เน็ต";}
  d("#online-map").setAttribute("aria-pressed",String(!!device.tiles));d("#online-map").textContent=device.tiles ? "ปิดแผนที่ถนน" : "เปิดแผนที่ถนน (อินเทอร์เน็ต)";
});
for(const id of ["#latitude","#longitude","#location-source"])d(id).addEventListener("change",updateLocation);
d("#move-location").addEventListener("click",()=>{try{const point=locationSnapshot();d("#location-source").value="simulated";d("#latitude").value=Math.min(89.99,point.latitude+.0003).toFixed(6);d("#longitude").value=Math.min(179.99,point.longitude+.0002).toFixed(6);updateLocation();message("เปลี่ยนพิกัดสำหรับภาพถัดไปแล้ว");}catch(error){message(error.message,true);}});
d("#toggle-led").addEventListener("click",()=>{device.led=!device.led;d("#led-status").textContent=`ไฟจำลอง: ${device.led ? "เปิด" : "ปิด"}`;d("#lamp").hidden=!device.led;d("#toggle-led").textContent=device.led ? "ปิด LED จำลอง" : "เปิด LED จำลอง";d("#toggle-led").setAttribute("aria-pressed",String(device.led));});
d("#battery").addEventListener("input",()=>{d("#battery-status").textContent=`แบตจำลอง ${d("#battery").value}%`;});
d("#fullscreen").addEventListener("click",async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch{message("กด F11 เพื่อใช้หน้าจอเต็มได้ครับ");}});
document.addEventListener("fullscreenchange",()=>{document.body.classList.toggle("kiosk",!!document.fullscreenElement);device.map?.invalidateSize();});
document.addEventListener("visibilitychange",()=>{if(document.hidden){cancelAnalysis();stopCamera();}});
window.addEventListener("pagehide",()=>{cancelAnalysis();stopCamera();releasePreview();clearTimeout(device.pollTimer);});
updateLocation();status();loadHistory().catch(error=>message(error.message,true));
