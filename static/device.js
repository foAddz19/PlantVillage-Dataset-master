"use strict";
const d = selector => document.querySelector(selector);
const device = { ready:false, busy:false, capturing:false, saving:false, blob:null, source:"upload", preview:null,
  stream:null, opening:false, cameraGeneration:0, requestGeneration:0, controller:null, draft:null,
  records:[], total:0, examples:[], sampleIndex:0, map:null, markers:null, currentMarker:null, tiles:null,
  mapEnabled:false, mapInfo:null, mapGeneration:0, mapController:null, coverageLayer:null, mapTileState:"loading",
  printing:false, deleting:false, pendingDelete:null, printer:{configured:false}, printedIds:new Set(), tab:"home", led:false, historyGeneration:0, pollTimer:null, mapLoadTimer:null };
const deviceToken = d('meta[name="app-token"]').content;
const percent = value => `${(value*100).toFixed(1)}%`;
const locationName = source => source === "simulated" ? "พิกัดจำลอง" : source === "gps" ? "GPS จริง" : "พิกัดระบุเอง";
const dateText = value => new Date(value).toLocaleString("th-TH");
function message(text, error=false) { d("#device-message").textContent=text; d("#device-message").classList.toggle("error",error); d("#device-message").hidden=false; clearTimeout(message.timer); message.timer=setTimeout(()=>{d("#device-message").hidden=true;},6000); }
async function api(path, options={}) {
  const response=await fetch(path,{...options,headers:{"X-App-Token":deviceToken,...options.headers}});
  let result; try { result=await response.json(); } catch { throw new Error("อ่านข้อมูลไม่ได้ กรุณารีเฟรชหน้าเว็บ"); }
  if(!response.ok) throw new Error(result.error || "ดำเนินการไม่สำเร็จ");
  return result;
}
function buttons() {
  d("#analyze-scan").disabled=!device.ready || device.busy || device.capturing || device.opening || device.deleting || device.saving || !!device.stream || !device.blob;
  d("#analyze-scan").textContent=device.busy ? "กำลังวิเคราะห์…" : "วิเคราะห์โรค";
  d("#save-scan").disabled=!device.draft || !!device.draft.saved_at || device.busy || device.saving || device.deleting;
  d("#save-scan").textContent=device.saving ? "กำลังบันทึก…" : device.draft?.saved_at ? "✓ บันทึกแล้ว" : "บันทึกผล";
  d("#print-scan").disabled=!device.draft?.saved_at || !device.printer.configured || device.printing || device.busy || device.saving || device.deleting;
  d("#print-scan").textContent=device.printing ? "กำลังส่งพิมพ์…" : device.printedIds.has(device.draft?.id) ? "พิมพ์ซ้ำ" : "พิมพ์ผล";
  d("#preview-receipt").disabled=!device.draft?.saved_at || device.printing || device.deleting;
  document.querySelectorAll("[data-print-scan]").forEach(button=>{
    button.disabled=!device.printer.configured || device.printing || device.deleting;
    button.textContent=device.printedIds.has(button.dataset.printScan) ? "พิมพ์ซ้ำ" : "พิมพ์ผล";
  });
  d("#open-camera").disabled=device.busy || !!device.stream || device.opening;
  d("#open-camera").textContent=device.opening ? "กำลังเปิด…" : "เปิดกล้อง";
  d("#close-camera").disabled=!device.stream && !device.opening;
  for(const id of ["#sample-image","#device-file"]) d(id).disabled=device.busy || device.saving;
  for(const id of ["#observer-severity","#scan-note"]) d(id).disabled=!!device.draft?.saved_at || device.saving;
  document.querySelectorAll('[data-delete-scan]').forEach(button=>{button.disabled=device.deleting || device.printing || device.saving || device.busy;});
  d('#cancel-delete').disabled=device.deleting;
  d('#confirm-delete').disabled=device.deleting || !device.pendingDelete;
  d('#confirm-delete').textContent=device.deleting ? 'กำลังลบ…' : 'ยืนยันลบรายการนี้';
  DeviceUI.sync(device);
}
function locationSnapshot() {
  const latitude=Number(d("#latitude").value),longitude=Number(d("#longitude").value);
  if(!d("#latitude").value.trim() || !d("#longitude").value.trim() || !Number.isFinite(latitude) || !Number.isFinite(longitude) || Math.abs(latitude)>90 || Math.abs(longitude)>180) throw new Error("กรุณาระบุพิกัดให้ถูกต้องในหน้าตั้งค่า");
  return {latitude,longitude,location_source:d("#location-source").value};
}
function updateLocation() {
  try {
    const point=locationSnapshot(); d("#location-summary").textContent=`${locationName(point.location_source)} · ${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
    if(device.map) {
      if(device.currentMarker) device.currentMarker.remove();
      device.currentMarker=L.circleMarker([point.latitude,point.longitude],{radius:12,color:"#245c55",weight:2,dashArray:"4 4",fill:false}).addTo(device.map);
      device.currentMarker.bindTooltip(`${locationName(point.location_source)} · พิกัดสำหรับภาพถัดไป ยังไม่บันทึก`);
    }
  } catch { d("#location-summary").textContent="พิกัดไม่ถูกต้อง · ตรวจการตั้งค่า"; }
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
    d("#source-label").textContent="ภาพสดจากกล้อง"; updateGuide(); buttons(); message("กล้องพร้อมแล้ว วางใบข้าวในกรอบแล้วกดถ่ายภาพ");
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
  d("#source-label").textContent=source==="sample" ? "ภาพทดลองจากชุดข้อมูลที่เลือก" : "ไฟล์จากเครื่อง";
  buttons(); message("ภาพนิ่งพร้อมแล้ว ตรวจภาพก่อนกดวิเคราะห์โรค");
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
  d("#result-disease").textContent=result.uncertain ? "ยังไม่สามารถระบุโรคได้อย่างมั่นใจ" : best.disease_th;
  d("#result-crop").textContent=result.uncertain ? "ลองถ่ายใหม่ให้เห็นใบชัดเจน" : DeviceUI.english(best.disease);
  d("#result-time").textContent=dateText(row.captured_at);
  d("#uncertainty").textContent=result.uncertain ? "คะแนนยังไม่ชัดเจน · ผลด้านล่างเป็นเพียงกลุ่มที่ใกล้เคียง" : "ผลคาดการณ์เบื้องต้น · ตรวจอาการจริงประกอบ";
  d("#device-candidates").replaceChildren();
  for(const item of result.candidates.slice(0,3)) {
    const box=document.createElement("div");box.className="rank-row";
    const line=document.createElement("div"),label=document.createElement("span"),score=document.createElement("span"),bar=document.createElement("progress");
    label.textContent=item.disease_th;score.textContent=percent(item.score);bar.max=1;bar.value=item.score;
    line.append(label,score);box.append(line,bar);d("#device-candidates").append(box);
  }
  d("#result-location").textContent=`${locationName(row.location_source)} · ${row.latitude.toFixed(5)}, ${row.longitude.toFixed(5)}`;
  d("#result-state").textContent=row.saved_at ? "บันทึกแล้ว" : "ตรวจแล้ว · ยังไม่บันทึก";
  d("#observer-severity").value=row.severity;d("#scan-note").value=row.note;
  PlantCare.summary(d("#result-care"),row.advice);
  DeviceUI.result(row);
  buttons();
}
async function analyze() {
  if(device.busy || device.capturing || !device.ready || !device.blob || device.stream)return;
  let location;try{location=locationSnapshot();}catch(error){message(error.message,true);return;}
  const generation=++device.requestGeneration, controller=new AbortController();device.controller=controller;
  device.busy=true;buttons();message("กำลังตรวจด้วย AI บนเครื่อง…");
  const timeout=setTimeout(()=>controller.abort(),30000);
  try {
    const source=device.source, blob=device.blob;
    if(generation!==device.requestGeneration)return;
    if(!blob)throw new Error("กรุณาเลือกภาพก่อน");
    const form=new FormData();form.append("image",blob,"scan-image");form.append("source",source);
    Object.entries(location).forEach(([key,value])=>form.append(key,value));
    const row=await api("/api/device/analyze",{method:"POST",body:form,signal:controller.signal});
    if(generation!==device.requestGeneration)return;
    device.draft=row;renderResult(row);showTab("result",true);message("วิเคราะห์เสร็จแล้ว ตรวจผลก่อนบันทึกหรือพิมพ์");
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
function previewReceipt(identifier) {
  if(!identifier)return;
  d("#receipt-image").src=`/api/device/scans/${encodeURIComponent(identifier)}/receipt.png`;
  d("#receipt-dialog").showModal();
}
async function printScan(identifier) {
  if(device.printing || !device.printer.configured)return;
  device.printing=true;buttons();
  // Every deliberate click is a new job. No automatic retries after USB/network errors.
  const requestId=crypto.randomUUID();
  try {
    const result=await api(`/api/device/scans/${encodeURIComponent(identifier)}/print`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({request_id:requestId})});
    device.printedIds.add(identifier);message(result.message);
  }catch(error){
    device.printedIds.add(identifier);message(`${error.message} · ตรวจใบพิมพ์ก่อนกดพิมพ์ซ้ำ`,true);
  }finally{device.printing=false;buttons();}
}
async function openCare(identifier) {
  try { const row=await api(`/api/device/scans/${encodeURIComponent(identifier)}`); PlantCare.open(row.advice); }
  catch(error) { message(error.message,true); }
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
  const care=document.createElement("button");care.textContent="ดูคำแนะนำ";care.addEventListener("click",()=>openCare(row.id));
  box.append(image,title,detail,care);return box;
}
function initializeMap() {
  if(device.map)return;
  device.map=L.map("field-map",{zoomControl:true,zoomAnimation:false,fadeAnimation:false,markerZoomAnimation:false}).setView([16.053,103.652],17);
  L.control.scale({imperial:false}).addTo(device.map);device.markers=L.featureGroup().addTo(device.map);updateLocation();
  device.map.on("moveend",mapCoverageState);
}
function mapState(state, detailText="") {
  const name=device.mapInfo?.name || "ไฟล์แผนที่ในเครื่อง";
  const descriptions={
    loading:"กำลังอ่านแผนที่จากเครื่อง… ไม่ใช้อินเทอร์เน็ต",
    ready:`แผนที่ออฟไลน์พร้อมใช้งาน · ${name}`,
    partial:"แผนที่ในเครื่องบางช่องอ่านไม่ได้ · จุดตรวจยังแสดงได้",
    error:"อ่านไฟล์แผนที่ไม่ได้ · จุดตรวจและประวัติยังใช้ได้",
    missing:"ยังไม่มีไฟล์แผนที่ออฟไลน์ · แสดงเฉพาะพิกัดจุดตรวจ",
    grid:"ปิดพื้นแผนที่อยู่ · แสดงเฉพาะพิกัดจุดตรวจ",
    outside:"บริเวณนี้อยู่นอกพื้นที่แผนที่ที่เก็บไว้ · จุดตรวจยังแสดงได้"
  };
  d("#map-mode").textContent=descriptions[state] || descriptions.error;
  d("#map-mode").dataset.state=state;
  const notice=d("#map-notice"),detail=d("#map-notice-text"),retry=d("#retry-map");
  notice.hidden=state==="ready" || state==="loading" || state==="grid";
  detail.textContent=detailText || (state==="outside"
    ? "กดดูพื้นที่แผนที่เพื่อกลับเข้าขอบเขต หรือใส่ไฟล์แผนที่ที่ครอบคลุมพื้นที่สำรวจนี้"
    : state==="missing" ? "ติดตั้งไฟล์แผนที่ในเครื่องตาม OFFLINE_MAP_GUIDE_TH.md ไม่ต้องเชื่อมอินเทอร์เน็ตขณะสำรวจ"
    : "ตรวจไฟล์แผนที่ในเครื่องแล้วกดตรวจไฟล์อีกครั้ง ระบบจะไม่เรียกเว็บไซต์แผนที่ภายนอก");
  retry.textContent=state==="outside" ? "ดูพื้นที่แผนที่" : "ตรวจไฟล์อีกครั้ง";
  d("#offline-map").setAttribute("aria-pressed",String(device.mapEnabled));
  d("#offline-map").textContent=device.mapEnabled ? "ปิดพื้นแผนที่" : "เปิดแผนที่ออฟไลน์";
  d("#show-map-coverage").disabled=!device.mapInfo?.ready;
}
function mapCoverageState() {
  if(!device.mapEnabled || !device.mapInfo?.ready || !device.tiles)return;
  const [west,south,east,north]=device.mapInfo.bounds;
  const center=device.map.getCenter();
  const expanded=device.map.getZoom()>device.mapInfo.maxzoom;
  d("#map-pack").textContent=(device.mapCaption || "")+(expanded ? " · กำลังขยายภาพจากระดับซูมข้อมูล" : "");
  const outside=center.lng<west || center.lng>east || center.lat<south || center.lat>north;
  mapState(outside ? "outside" : device.mapTileState || "loading");
}
function showMapCoverage() {
  if(!device.mapInfo?.ready)return;
  const [west,south,east,north]=device.mapInfo.bounds;
  device.map.stop();device.map.invalidateSize({animate:false,pan:false});
  device.map.fitBounds([[south,west],[north,east]],{padding:[12,12],maxZoom:device.mapInfo.maxzoom,animate:false});
}
function mapAttribution(info) {
  const span=document.createElement("span");
  span.textContent=info.attribution?.text || "แผนที่ในเครื่อง";
  if(info.attribution?.url){
    const url=new URL(info.attribution.url,location.origin);
    if(["https:","http:"].includes(url.protocol)){
      const link=document.createElement("a");link.href=url.href;link.target="_blank";link.rel="noopener";
      link.textContent=span.textContent;span.replaceChildren(link);
    }
  }
  return span.outerHTML;
}
async function setRoadMapEnabled(enabled) {
  const generation=++device.mapGeneration;
  device.mapEnabled=enabled;device.mapController?.abort();
  clearTimeout(device.mapLoadTimer);
  if(device.tiles){const previous=device.tiles;device.tiles=null;previous.remove();previous.off();}
  if(device.coverageLayer){device.coverageLayer.remove();device.coverageLayer=null;}
  if(!enabled){mapState("grid");return;}
  initializeMap();mapState("loading");
  const controller=new AbortController();device.mapController=controller;
  const timeout=setTimeout(()=>controller.abort(),10000);
  try {
    const info=await api("/api/device/map/status",{signal:controller.signal});
    if(generation!==device.mapGeneration)return;
    const firstPackage=!device.mapInfo?.ready;device.mapInfo=info;
    if(!info.ready){
      d("#map-pack").textContent="ยังไม่มีพื้นแผนที่ที่พร้อมใช้งานในเครื่อง";
      mapState(info.code==="missing" ? "missing" : "error",info.error || "");return;
    }
    if(!Array.isArray(info.bounds) || info.bounds.length!==4 || !info.bounds.every(Number.isFinite))throw new Error("ขอบเขตไฟล์แผนที่ไม่ถูกต้อง");
    const [west,south,east,north]=info.bounds;
    const bounds=L.latLngBounds([[south,west],[north,east]]);
    device.map.setMinZoom(Math.max(1,info.minzoom-2));
    device.map.setMaxZoom(Math.min(20,info.maxzoom+2));
    if(firstPackage && device.map.getZoom()>info.maxzoom)device.map.setZoom(info.maxzoom);
    device.mapCaption=`${info.name} · ไฟล์ในเครื่อง · ซูมข้อมูล ${info.minzoom}–${info.maxzoom} · ${(info.size_bytes/1000000).toFixed(1)} MB`;
    d("#map-pack").textContent=device.mapCaption;
    device.coverageLayer=L.rectangle(bounds,{color:"#567b56",weight:1,dashArray:"6 5",fill:false,interactive:false}).addTo(device.map);
    const version=encodeURIComponent(info.version || info.revision || "local");
    const layer=L.tileLayer(`/api/device/map/tiles/{z}/{x}/{y}.png?v=${version}`,{
      minNativeZoom:info.minzoom,maxNativeZoom:info.maxzoom,maxZoom:Math.min(20,info.maxzoom+2),
      bounds,noWrap:true,tileSize:256,keepBuffer:1,updateWhenIdle:true,
      attribution:mapAttribution(info)
    });
    device.tiles=layer;device.mapTileState="loading";
    let loaded=0,failed=0;
    layer.on("loading",()=>{
      if(device.tiles!==layer)return;
      loaded=0;failed=0;device.mapTileState="loading";mapCoverageState();clearTimeout(device.mapLoadTimer);
      device.mapLoadTimer=setTimeout(()=>{
        if(device.tiles===layer){device.mapTileState=loaded ? "partial" : "error";mapCoverageState();}
      },10000);
    });
    layer.on("tileload",()=>{if(device.tiles===layer)loaded++;});
    layer.on("tileerror",()=>{if(device.tiles===layer)failed++;});
    layer.on("load",()=>{
      if(device.tiles!==layer)return;
      clearTimeout(device.mapLoadTimer);device.mapTileState=failed ? (loaded ? "partial" : "error") : "ready";
      mapCoverageState();
    });
    layer.addTo(device.map);mapCoverageState();
  }catch(error){
    if(generation===device.mapGeneration){device.mapInfo=null;mapState("error",error.name==="AbortError" ? "อ่านสถานะแผนที่ไม่สำเร็จ กรุณาตรวจว่าแอปยังเปิดอยู่แล้วลองอีกครั้ง" : error.message);}
  }finally{clearTimeout(timeout);if(generation===device.mapGeneration)device.mapController=null;}
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
    button.addEventListener("click",()=>{device.map.setView(marker.getLatLng(),17,{animate:false});marker.openPopup();});d("#map-list").append(button);
  }
}
function fitPoints() {if(device.markers?.getLayers().length)device.map.fitBounds(device.markers.getBounds(),{padding:[30,30],maxZoom:18,animate:false});else{try{const point=locationSnapshot();device.map.setView([point.latitude,point.longitude],17,{animate:false});}catch{}}}
function showTab(tab, keepAnalysis=false) {
  if(!["home","capture","result","map","history","care","settings"].includes(tab))tab="home";
  if(tab==="result" && !device.draft)tab="capture";
  if(tab!=="capture"){if(!keepAnalysis)cancelAnalysis();stopCamera();}
  device.tab=tab;
  for(const name of ["home","capture","result","map","history","care","settings"])d(`#screen-${name}`).hidden=name!==tab;
  document.querySelectorAll("[data-tab]").forEach(button=>{button.classList.toggle("active",button.dataset.tab===(tab==="result" ? "capture" : tab));button.setAttribute("aria-pressed",String(button.dataset.tab===(tab==="result" ? "capture" : tab)));});
  if(tab==="map"){
    const firstVisit=!device.map;
    initializeMap();device.map.invalidateSize();renderMap();fitPoints();
    // Read local map files on first visit; preserve a manual switch to the coordinate grid.
    if(firstVisit)setRoadMapEnabled(true);
  }
  if(tab==="history" || tab==="map")loadHistory().catch(error=>message(error.message,true));
  if(tab==="capture")updateGuide();
  if(tab==="care")PlantCare.library(d("#care-library"));
  DeviceUI.view(tab);
  d("#workspace").scrollTop=0;
}
function renderHistory() {
  d("#history-count").textContent=`ทั้งหมด ${device.total} จุด · แสดง ${device.records.length} รายการล่าสุด`;
  d("#saved-count").textContent=`${device.total} จุดบันทึก`;
  DeviceUI.historySummary(device.total);
  d("#history-list").replaceChildren();
  if(!device.records.length){const p=document.createElement("p");p.className="empty";p.textContent="ยังไม่มีจุดตรวจ ลองเลือกภาพ ตรวจโรค แล้วกดบันทึกจุดตรวจ";d("#history-list").append(p);return;}
  for(const row of device.records){
    const card=document.createElement("article");card.className="scan-card";
    const img=document.createElement("img");img.src=row.image_url;img.alt="ภาพใบพืชที่บันทึก";img.loading="lazy";
    const body=document.createElement("div"),title=document.createElement("strong"),details=document.createElement("small"),note=document.createElement("p"),tag=document.createElement("span"),button=document.createElement("button");
    title.textContent=recordTitle(row);details.textContent=`${dateText(row.captured_at)} · ${row.latitude.toFixed(5)}, ${row.longitude.toFixed(5)}`;
    note.textContent=row.note;tag.className="simulation-tag";tag.textContent=locationName(row.location_source);body.append(title,details,tag,note);
    button.textContent="ดูบนแผนที่ ↗";button.addEventListener("click",()=>{showTab("map");device.map.setView([row.latitude,row.longitude],18,{animate:false});L.popup().setLatLng([row.latitude,row.longitude]).setContent(popup(row)).openOn(device.map);});
    const actions=document.createElement("div");actions.className="scan-actions";
    const preview=document.createElement("button");preview.textContent="ดูใบผลตรวจ";preview.addEventListener("click",()=>previewReceipt(row.id));
    const print=document.createElement("button");print.dataset.printScan=row.id;print.textContent=device.printedIds.has(row.id) ? "พิมพ์ซ้ำ" : "พิมพ์ผล";
    print.disabled=!device.printer.configured || device.printing;print.addEventListener("click",()=>printScan(row.id));
    const care=document.createElement("button");care.textContent="ดูคำแนะนำ";care.dataset.careScan=row.id;care.addEventListener("click",()=>openCare(row.id));
    const remove=document.createElement('button');remove.type='button';remove.className='delete-scan';remove.textContent='ลบรายการนี้';remove.dataset.deleteScan=row.id;
    remove.disabled=device.deleting || device.printing || device.saving || device.busy;remove.addEventListener('click',()=>requestDelete(row));
    const detailsButton=document.createElement('button');detailsButton.textContent='เปิดผลตรวจ';detailsButton.className='history-open-result';detailsButton.dataset.viewScan=row.id;
    detailsButton.addEventListener('click',async()=>{try{const saved=await api(`/api/device/scans/${encodeURIComponent(row.id)}`);device.draft=saved;renderResult(saved);showTab('result');}catch(error){message(error.message,true);}});
    actions.append(detailsButton,button,care,preview,print,remove);
    card.append(img,body,actions);d("#history-list").append(card);
  }
}
async function loadHistory() {const generation=++device.historyGeneration;const data=await api("/api/device/scans");if(generation!==device.historyGeneration)return;device.records=data.items;device.total=data.total;renderHistory();renderMap();}
function requestDelete(row) {
  if(device.deleting || device.printing || device.saving || device.busy || !row.saved_at)return;
  device.pendingDelete=row;
  d('#delete-image').src=row.image_url;
  d('#delete-title').textContent=recordTitle(row);
  d('#delete-time').textContent=dateText(row.captured_at);
  d('#delete-location').textContent=`${locationName(row.location_source)} · ${row.latitude.toFixed(5)}, ${row.longitude.toFixed(5)}`;
  d('#delete-id').textContent=`รหัสรายการ ${row.id.slice(0,12)}`;
  d('#delete-error').hidden=true;buttons();
  d('#delete-dialog').showModal();d('#cancel-delete').focus();
}
async function confirmDelete() {
  if(device.deleting || !device.pendingDelete)return;
  const identifier=device.pendingDelete.id;
  device.deleting=true;d('#delete-error').hidden=true;buttons();
  try {
    const result=await api(`/api/device/scans/${encodeURIComponent(identifier)}/delete`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_id:identifier})});
    // Invalidate outstanding history requests before removing the exact saved item.
    device.historyGeneration++;
    const previous=device.records.length;
    device.records=device.records.filter(row=>row.id!==identifier);
    device.total=Math.max(0,device.total-(previous-device.records.length));
    device.printedIds.delete(identifier);
    if(device.draft?.id===identifier)clearResult();
    device.map?.closePopup();renderHistory();renderMap();
    device.pendingDelete=null;d('#delete-dialog').close();
    try { await loadHistory();message(result.message); }
    catch { message('ลบรายการแล้ว แต่รีเฟรชข้อมูลไม่สำเร็จ กรุณากดรีเฟรชประวัติ',true); }
  }catch(error) {
    d('#delete-error').textContent=error.message+' · ตรวจประวัติก่อนลองใหม่';d('#delete-error').hidden=false;
  }finally {device.deleting=false;buttons();}
}
async function status() {
  clearTimeout(device.pollTimer);
  try {const data=await api("/api/status");device.ready=!!data.ready;d("#ai-status").textContent=data.ready ? "AI พร้อมใช้งาน" : "โมเดลยังไม่พร้อม";d("#ai-status").classList.toggle("ready",device.ready);}
  catch{device.ready=false;d("#ai-status").textContent="เชื่อมต่อโปรแกรมไม่ได้";d("#ai-status").classList.remove("ready");}
  try{device.printer=await api("/api/device/printer");d("#printer-status").textContent=device.printer.message;}
  catch{device.printer={configured:false};d("#printer-status").textContent="ตรวจสถานะเครื่องพิมพ์ไม่ได้";}
  buttons();device.pollTimer=setTimeout(status,15000);
}
d("#open-camera").addEventListener("click",openCamera);
d("#close-camera").addEventListener("click",()=>{cancelAnalysis();stopCamera();message("ปิดกล้องแล้ว");});
d("#device-file").addEventListener("change",event=>{const file=event.target.files[0];if(file)chooseImage(file,"upload");});
d("#sample-image").addEventListener("click",async()=>{try{if(!device.examples.length)device.examples=(await api("/api/examples")).items;const item=device.examples[device.sampleIndex++%device.examples.length];if(!item)throw new Error("ยังไม่มีชุดภาพทดลอง เลือกไฟล์หรือเปิดกล้องได้");const response=await fetch(item.url);if(!response.ok)throw new Error("โหลดภาพทดลองไม่ได้");chooseImage(await response.blob(),"sample");}catch(error){message(error.message,true);}});
d("#print-scan").addEventListener("click",()=>printScan(device.draft?.id));
d("#preview-receipt").addEventListener("click",()=>previewReceipt(device.draft?.id));
d("#close-receipt").addEventListener("click",()=>d("#receipt-dialog").close());
d("#receipt-image").addEventListener("error",()=>{d("#receipt-dialog").close();message("เปิดใบผลตรวจไม่ได้ กรุณาตรวจการติดตั้งส่วนพิมพ์และฟอนต์ไทย",true);});
d("#analyze-scan").addEventListener("click",analyze);d("#save-scan").addEventListener("click",saveScan);
document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>showTab(button.dataset.tab)));
d("#go-map").addEventListener("click",()=>showTab("map"));d("#fit-points").addEventListener("click",fitPoints);
d("#map-filter").addEventListener("change",()=>{renderMap();fitPoints();});
d("#refresh-history").addEventListener("click",()=>loadHistory().catch(error=>message(error.message,true)));
d('#cancel-delete').addEventListener('click',()=>{if(!device.deleting)d('#delete-dialog').close();});
d('#confirm-delete').addEventListener('click',confirmDelete);
d('#delete-dialog').addEventListener('cancel',event=>{if(device.deleting)event.preventDefault();});
d('#delete-dialog').addEventListener('close',()=>{device.pendingDelete=null;buttons();});
d("#offline-map").addEventListener("click",()=>setRoadMapEnabled(!device.mapEnabled));
d("#show-map-coverage").addEventListener("click",showMapCoverage);
d("#retry-map").addEventListener("click",()=>d("#map-mode").dataset.state==="outside" ? showMapCoverage() : setRoadMapEnabled(true));
for(const id of ["#latitude","#longitude","#location-source"])d(id).addEventListener("change",updateLocation);
d("#move-location").addEventListener("click",()=>{try{const point=locationSnapshot();d("#location-source").value="simulated";d("#latitude").value=Math.min(89.99,point.latitude+.0003).toFixed(6);d("#longitude").value=Math.min(179.99,point.longitude+.0002).toFixed(6);updateLocation();message("เปลี่ยนพิกัดสำหรับภาพถัดไปแล้ว");}catch(error){message(error.message,true);}});
d("#toggle-led").addEventListener("click",()=>{device.led=!device.led;d("#led-status").textContent=`ไฟจำลอง: ${device.led ? "เปิด" : "ปิด"}`;d("#lamp").hidden=!device.led;d("#toggle-led").textContent=device.led ? "ปิด LED จำลอง" : "เปิด LED จำลอง";d("#toggle-led").setAttribute("aria-pressed",String(device.led));});

d("#fullscreen").addEventListener("click",async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch{message("กด F11 เพื่อใช้หน้าจอเต็มได้ครับ");}});
document.addEventListener("fullscreenchange",()=>{document.body.classList.toggle("kiosk",!!document.fullscreenElement);device.map?.invalidateSize();});
document.addEventListener("visibilitychange",()=>{if(document.hidden){cancelAnalysis();stopCamera();}});
window.addEventListener("pagehide",()=>{cancelAnalysis();stopCamera();releasePreview();clearTimeout(device.pollTimer);clearTimeout(device.mapLoadTimer);});
d("#care-search").addEventListener("input",()=>PlantCare.library(d("#care-library")));

async function capturePhoto() {
  if(device.capturing || device.busy || !device.stream || device.opening)return;
  const generation=device.cameraGeneration;device.capturing=true;buttons();
  try {const frame=await cameraFrame();if(generation!==device.cameraGeneration)return;chooseImage(frame,'camera');d('#source-label').textContent='ภาพนิ่งจากกล้อง · พร้อมวิเคราะห์';}
  catch(error){message(error.message,true);}finally{device.capturing=false;buttons();}
}
function newScan() {
  if(device.busy || device.saving || device.printing || device.deleting)return;
  cancelAnalysis();stopCamera();releasePreview();device.blob=null;device.source='upload';clearResult();
  d('#device-file').value='';d('#device-preview').hidden=true;d('#camera-empty').hidden=false;d('#capture-guide').hidden=true;
  d('#source-label').textContent='รอภาพใบข้าว';showTab('capture');buttons();
}
d('#capture-photo').addEventListener('click',capturePhoto);
d('#retake-photo').addEventListener('click',openCamera);
d('#new-scan').addEventListener('click',newScan);
d('#result-detail').addEventListener('click',()=>PlantCare.open(device.draft?.advice));
d('#edit-observations').addEventListener('click',()=>d('#observations-dialog').showModal());
d('#refresh-device-status').addEventListener('click',status);

updateLocation();status();loadHistory().catch(error=>message(error.message,true));
showTab(location.hash.slice(1) || "home");
