"use strict";
// Presentation only. Predictions, guidance and device status come from existing APIs.
window.DeviceUI = (() => {
  const find = selector => document.querySelector(selector);
  const text = (selector, value) => { const target=find(selector); if(target)target.textContent=value; };
  const english = value => String(value || '').replace(/_/g,' ').replace(/^./,letter=>letter.toUpperCase());
  const pageNames = {home:'ภาพรวมอุปกรณ์',capture:'ตรวจโรคข้าว',result:'ผลวิเคราะห์ใบข้าว',history:'ประวัติการตรวจ',map:'แผนที่แปลง',care:'คลังความรู้โรคข้าว',settings:'ตั้งค่าอุปกรณ์'};
  function view(name) {
    document.body.dataset.screen=name;
    text('#page-title',pageNames[name] || pageNames.home);
    try { history.replaceState(null,'',name==='home' ? location.pathname : `#${name}`); } catch { /* Local state still works. */ }
  }
  function sync(state) {
    const working=state.busy || state.capturing;
    find('#capture-photo').disabled=!state.stream || state.opening || working || state.saving;
    find('#retake-photo').hidden=!state.blob;
    find('#retake-photo').disabled=working || state.saving || state.opening;
    text('#retake-photo',state.source==='camera' ? 'ถ่ายใหม่' : 'เปิดกล้อง');
    find('#new-scan').disabled=working || state.saving || state.printing || state.deleting;
    find('#analysis-overlay').hidden=!state.busy;
    find('#workspace').setAttribute('aria-busy',String(state.busy));
    text('#camera-status',state.opening ? 'กำลังขอใช้กล้อง' : state.stream ? 'รับภาพจากกล้องแล้ว' : 'ยังไม่เปิดกล้อง');
    find('#camera-dot').classList.toggle('neutral',!state.stream);
    find('#viewfinder-dot').classList.toggle('neutral',!state.stream && !state.blob);
    text('#capture-instruction',state.busy ? 'กำลังอ่านภาพนิ่งด้วย AI กรุณารอสักครู่' : state.stream ? 'วางใบข้าวให้ชัด แล้วกด “ถ่ายภาพ”' : state.blob ? 'ตรวจภาพนิ่งแล้ว กด “วิเคราะห์โรค” เมื่อพร้อม' : 'เลือกภาพใบข้าว หรือเปิดกล้องแล้วกดถ่ายภาพ');
    text('#home-printer',state.printer.configured ? 'ตั้งค่าแล้ว · ตรวจที่เครื่องก่อนพิมพ์' : 'ยังไม่พร้อม · ตรวจการตั้งค่า');
    text('#edit-observations',state.draft?.saved_at ? 'ดูบันทึกแปลง' : 'เพิ่มบันทึกแปลง');
    find('#result-detail').disabled=!state.draft?.advice;
    find('#edit-observations').disabled=!state.draft || state.busy || state.saving;
  }
  function result(row) {
    const prediction=row.prediction, best=prediction.candidates[0], advice=row.advice || {};
    find('#screen-result').classList.toggle('uncertain',!!prediction.uncertain);
    text('#result-confidence',`${(best.score*100).toFixed(1)}%`);
    text('#confidence-label',prediction.uncertain ? 'คะแนนกลุ่มใกล้เคียง' : 'ความมั่นใจของโมเดล');
    text('#result-crop',prediction.uncertain ? 'Inconclusive · ควรถ่ายภาพใหม่' : english(best.disease));
    text('#result-symptoms',(advice.checks || [])[0] || advice.summary || 'ตรวจอาการจริงประกอบภาพ');
    text('#result-cause',advice.cause || 'ยังไม่เลือกสาเหตุเฉพาะโรค');
    text('#result-management',(advice.next_steps || [])[0] || advice.summary || 'เก็บภาพเพิ่มเติม');
    text('#result-caution',advice.urgent ? 'ผู้ตรวจระบุอาการรุนแรง · ติดต่อเจ้าหน้าที่เกษตรหรือศูนย์วิจัยข้าวโดยเร็ว' : (advice.avoid || [])[0] || advice.disclaimer || 'ผลจากภาพไม่ใช่การยืนยันโรค');
    for(const id of ['#result-symptoms','#result-cause','#result-management','#result-caution'])find(id).title=find(id).textContent;
  }
  function historySummary(total) {
    text('#home-total',new Intl.NumberFormat('th-TH').format(total));
    text('#nav-count',String(total));find('#nav-count').hidden=!total;
  }
  let theme='light';
  try { theme=localStorage.getItem('ai-plant-theme')==='dark' ? 'dark' : 'light'; } catch { /* Private/kiosk storage can be disabled. */ }
  document.documentElement.dataset.theme=theme;
  find('#theme-toggle').setAttribute('aria-pressed',String(theme==='dark'));
  find('#theme-toggle').addEventListener('click',()=>{
    theme=theme==='light' ? 'dark' : 'light';
    document.documentElement.dataset.theme=theme;
    find('#theme-toggle').setAttribute('aria-pressed',String(theme==='dark'));
    try { localStorage.setItem('ai-plant-theme',theme); } catch { /* Theme remains usable without persistence. */ }
  });
  document.querySelectorAll('[data-go]').forEach(button=>button.addEventListener('click',()=>showTab(button.dataset.go)));
  find('#close-observations').addEventListener('click',()=>find('#observations-dialog').close());
  return {view,sync,result,historySummary,english};
})();
