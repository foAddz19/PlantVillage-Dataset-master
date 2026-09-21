"use strict";

// Owns only the camera stream. Inference scheduling is handled by app.js.
class LeafCamera {
  constructor({ onChange, onStop }) {
    this.video = document.querySelector("#camera-video");
    this.stage = document.querySelector("#camera-stage");
    this.devices = document.querySelector("#camera-device");
    this.onChange = onChange;
    this.onStop = onStop;
    this.stream = null;
    this.active = false;
    this.opening = false;
    this.generation = 0;
    document.querySelector("#camera-open").addEventListener("click", () => this.start());
    document.querySelector("#camera-stop").addEventListener("click", () => this.stop());
    this.devices.addEventListener("change", () => {
      if (this.active || this.opening) this.start();
    });
    this.video.addEventListener("resize", () => this.updateGuide());
    this.observer = new ResizeObserver(() => this.updateGuide());
    this.observer.observe(this.stage);
  }

  message(text, error = false) {
    const element = document.querySelector("#camera-message");
    element.textContent = text;
    element.classList.toggle("error", error);
  }

  render() {
    document.querySelector("#camera-open").disabled = this.opening || this.active;
    document.querySelector("#camera-open").textContent = this.opening ? "กำลังเปิดกล้อง…" : "เปิดกล้อง";
    document.querySelector("#camera-stop").disabled = !this.opening && !this.active;
    document.querySelector("#camera-stop").textContent = this.opening ? "ยกเลิกเปิดกล้อง" : "ปิดกล้อง";
    document.querySelector("#camera-placeholder").hidden = this.active;
    document.querySelector("#camera-live").hidden = !this.active;
    document.querySelector("#camera-guide").hidden = !this.active;
    this.devices.disabled = this.opening || this.devices.options.length < 2;
    this.onChange();
  }

  async start() {
    const selected = this.devices.value;
    this.stop();
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      this.message("เบราว์เซอร์นี้เปิดกล้องไม่ได้ กรุณาเปิดผ่าน localhost หรือ HTTPS ใน Edge หรือ Chrome", true);
      return;
    }
    const generation = ++this.generation;
    this.opening = true;
    this.message("กำลังขอเปิดกล้อง กรุณาเลือกอนุญาตในเบราว์เซอร์ หรือกดยกเลิกเปิดกล้อง");
    this.render();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 15, max: 30 },
          ...(selected ? { deviceId: { exact: selected } } : { facingMode: { ideal: "environment" } }) },
      });
      // A permission dialog can outlive Stop, a mode switch, or a hidden page.
      if (generation !== this.generation || document.hidden) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream;
      for (const track of stream.getVideoTracks()) {
        track.addEventListener("ended", () => {
          if (this.stream === stream) this.stop("กล้องถูกตัดการเชื่อมต่อ กรุณาตรวจสายหรือสิทธิ์กล้องแล้วเปิดใหม่");
        });
      }
      this.video.srcObject = stream;
      let playbackTimeout;
      try {
        await Promise.race([
          this.video.play(),
          new Promise((_, reject) => {
            playbackTimeout = setTimeout(() => reject(new DOMException("No camera frames", "NotReadableError")), 12000);
          }),
        ]);
      } finally {
        clearTimeout(playbackTimeout);
      }
      if (generation !== this.generation) return;
      this.active = true;
      this.opening = false;
      this.message("กล้องพร้อมแล้ว วางใบพืชหนึ่งใบในกรอบกลางภาพ");
      this.render();
      this.updateGuide();
      await this.refreshDevices(generation);
    } catch (error) {
      if (generation !== this.generation) return;
      this.stop();
      const messages = {
        NotAllowedError: "ไม่ได้รับอนุญาตให้ใช้กล้อง เปิดสิทธิ์กล้องจากไอคอนข้างแถบที่อยู่ แล้วกดเปิดกล้องอีกครั้ง",
        NotFoundError: "ไม่พบกล้อง กรุณาเชื่อมต่อเว็บแคมหรือกล้อง USB แล้วลองใหม่",
        NotReadableError: "เปิดกล้องไม่ได้ ลองปิดโปรแกรมอื่นที่ใช้กล้อง และตรวจสิทธิ์กล้องใน Windows",
        OverconstrainedError: "กล้องที่เลือกไม่พร้อมใช้งาน กรุณาเลือกกล้องอัตโนมัติหรือกล้องอื่น",
        AbortError: "การเปิดกล้องถูกยกเลิก กดเปิดกล้องเพื่อลองใหม่",
      };
      this.message(messages[error.name] || "เปิดกล้องไม่สำเร็จ กรุณาตรวจการเชื่อมต่อและลองใหม่", true);
    }
  }

  async refreshDevices(generation) {
    try {
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === "videoinput");
      if (generation !== this.generation) return;
      const selected = this.stream?.getVideoTracks()[0]?.getSettings().deviceId || this.devices.value;
      this.devices.replaceChildren(new Option("เลือกกล้องอัตโนมัติ", ""));
      devices.forEach((device, index) => this.devices.add(new Option(device.label || `กล้อง ${index + 1}`, device.deviceId)));
      if (devices.some(device => device.deviceId === selected)) this.devices.value = selected;
      this.render();
    } catch {
      // The default camera still works when a browser restricts enumeration.
    }
  }

  updateGuide() {
    if (!this.active || !this.video.videoWidth || !this.video.videoHeight) return;
    const scale = Math.min(this.stage.clientWidth / this.video.videoWidth, this.stage.clientHeight / this.video.videoHeight);
    // Match the centered square used by ImageOps.fit on the server.
    const size = Math.min(this.video.videoWidth, this.video.videoHeight) * scale;
    document.querySelector("#camera-guide").style.width = `${size}px`;
    document.querySelector("#camera-guide").style.height = `${size}px`;
  }

  async capture() {
    if (!this.active || this.video.readyState < 2 || !this.video.videoWidth || !this.video.videoHeight) {
      throw new Error("กล้องยังไม่มีภาพ กรุณารอสักครู่แล้วลองอีกครั้ง");
    }
    const canvas = document.createElement("canvas");
    canvas.width = this.video.videoWidth;
    canvas.height = this.video.videoHeight;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("เบราว์เซอร์ไม่สามารถจับภาพได้ กรุณาเปิดโปรแกรมใหม่");
    // Preserve the full frame. Uploads and camera frames share server preprocessing.
    context.drawImage(this.video, 0, 0);
    const capturedAt = new Date().toISOString();
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("จับภาพจากกล้องไม่สำเร็จ กรุณาลองอีกครั้ง");
    if (blob.size > 8 * 1024 * 1024) throw new Error("ภาพจากกล้องใหญ่เกิน 8 MB กรุณาลดความละเอียดกล้องแล้วลองใหม่");
    return { blob, capturedAt, width: canvas.width, height: canvas.height,
      size: Math.min(canvas.width, canvas.height) };
  }

  stop(message = "ปิดกล้องแล้ว กดเปิดกล้องเมื่อต้องการใช้งาน") {
    this.generation++;
    const stream = this.stream;
    this.stream = null;
    this.active = false;
    this.opening = false;
    if (stream) stream.getTracks().forEach(track => track.stop());
    this.video.pause();
    this.video.srcObject = null;
    this.message(message);
    this.onStop();
    this.render();
  }
}
