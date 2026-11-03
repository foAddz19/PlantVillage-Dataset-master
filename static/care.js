"use strict";
// Shared offline guidance UI. Text and source links are built as DOM nodes, never HTML.
window.PlantCare = (() => {
  let catalog = null, libraryPromise = null, dialog = null;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, action) => {
    const item = node("button", text, "care-button"); item.type = "button";
    item.addEventListener("click", action); return item;
  };
  function provenance(advice) {
    const basis = advice.basis === "analysis_snapshot" ? "ฉบับที่เก็บพร้อมผลตรวจ" :
      advice.basis === "current_reference" ? "รายการเดิม · ใช้คู่มือฉบับปัจจุบัน" : "คู่มือสำหรับอ่านอ้างอิง";
    return `${basis} · รุ่น ${advice.knowledge_version} · ตรวจเอกสาร ${advice.checked_on}`;
  }
  function urgent(advice) {
    return advice.urgent ? node("p", "ผู้ตรวจระบุอาการรุนแรง · ติดต่อเจ้าหน้าที่เกษตรหรือศูนย์วิจัยข้าวโดยเร็ว", "care-urgent") : null;
  }
  function summary(container, advice) {
    container.replaceChildren(); container.hidden = !advice;
    if (!advice) return;
    container.classList.add("care-card"); container.dataset.status = advice.status;
    const heading = node("div", undefined, "care-heading");
    heading.append(node("span", "แนวทางดูแลแปลง", "care-eyebrow"), node("span", advice.category, "care-tag"));
    container.append(heading, node("h3", advice.title), node("p", advice.status_label, "care-status"));
    const alert = urgent(advice); if (alert) container.append(alert);
    container.append(node("p", advice.summary));
    const list = node("ol");
    (advice.next_steps || []).slice(0, 2).forEach(text => list.append(node("li", text)));
    container.append(list, button("อ่านคำแนะนำทั้งหมด →", () => open(advice)), node("small", provenance(advice), "care-meta"));
  }
  function section(container, title, values) {
    if (!values?.length) return;
    const box = node("section", undefined, "care-section"); box.append(node("h3", title));
    const list = node("ul"); values.forEach(text => list.append(node("li", text)));
    box.append(list); container.append(box);
  }
  function open(advice) {
    if (!advice) return;
    if (!dialog) {
      dialog = node("dialog", undefined, "care-dialog"); dialog.id = "care-dialog";
      dialog.setAttribute("aria-labelledby", "care-dialog-title");
      document.body.append(dialog);
    }
    dialog.replaceChildren(); dialog.dataset.status = advice.status;
    const head = node("div", undefined, "care-dialog-heading");
    const title = node("h2", advice.title); title.id = "care-dialog-title";
    head.append(title, button("ปิด", () => dialog.close())); dialog.append(head);
    dialog.append(node("p", advice.status_label, "care-status"));
    const alert = urgent(advice); if (alert) dialog.append(alert);
    dialog.append(node("p", advice.summary, "care-intro"), node("p", advice.disclaimer, "care-notice"));
    section(dialog, "สาเหตุที่ควรตรวจยืนยัน", [advice.cause]);
    section(dialog, "ตรวจอาการประกอบ", advice.checks);
    section(dialog, "สิ่งที่ควรทำต่อ", advice.next_steps);
    section(dialog, "ป้องกันและดูแลในระยะต่อไป", advice.prevention);
    section(dialog, "สิ่งที่ควรหลีกเลี่ยง", advice.avoid);
    section(dialog, "เมื่อใดควรขอความช่วยเหลือ", [advice.seek_help]);
    section(dialog, "การติดตามอาการ", [advice.follow_up]);
    section(dialog, "ก่อนพิจารณาสารเคมี", [advice.chemical_policy]);
    const sources = node("section", undefined, "care-section care-sources");
    sources.append(node("h3", "แหล่งอ้างอิง"));
    for (const source of advice.sources || []) {
      try {
        const url = new URL(source.url);
        if (url.protocol !== "https:") continue;
        const link = node("a", `${source.publisher} · ${source.title} ↗`);
        link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; sources.append(link);
      } catch { /* Ignore invalid source URLs in a legacy snapshot. */ }
    }
    if (!sources.querySelector("a")) sources.append(node("p", "คำแนะนำเก็บข้อมูลทั่วไป ยังไม่ได้เลือกคู่มือเฉพาะโรค"));
    sources.append(node("small", "เนื้อหาอ่านได้ออฟไลน์ · เปิดเว็บไซต์อ้างอิงต้องใช้อินเทอร์เน็ต", "care-meta"));
    dialog.append(sources, node("p", provenance(advice), "care-meta"), node("p", advice.scope, "care-meta"));
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  async function library(container, query = "") {
    if (!catalog) {
      container.replaceChildren(node("p", "กำลังเปิดคู่มือในเครื่อง…"));
      try {
        libraryPromise ||= fetch("/api/care/library").then(async response => {
          if (!response.ok) throw new Error("โหลดคู่มือไม่ได้"); return response.json();
        });
        catalog = await libraryPromise;
      } catch {
        libraryPromise = null;
        container.replaceChildren(node("p", "เปิดคู่มือไม่ได้ ตรวจว่าโปรแกรมยังทำงานอยู่"), button("ลองเปิดใหม่", () => library(container, query)));
        return;
      }
    }
    // Use the latest search value if the user typed while the local request was loading.
    const search = document.querySelector("#care-search");
    query = (search ? search.value : query).trim().toLocaleLowerCase();
    const matches = catalog.items.filter(item => `${item.title} ${item.category} ${item.disease_key}`.toLocaleLowerCase().includes(query));
    container.replaceChildren();
    const count = document.querySelector("#care-library-count");
    if (count) count.textContent = `แสดง ${matches.length} จาก ${catalog.items.length} กลุ่ม · อ่านได้ออฟไลน์`;
    for (const advice of matches) {
      const card = node("article", undefined, "care-library-card");
      card.append(node("span", advice.category, "care-tag"), node("h3", advice.title), node("p", advice.summary),
        button("เปิดคู่มือ →", () => open(advice)));
      container.append(card);
    }
    if (!matches.length) container.append(node("p", "ไม่พบชื่อที่ค้นหา ลองใช้ชื่อโรคหรืออาการอื่น"));
  }
  return { summary, open, library };
})();
