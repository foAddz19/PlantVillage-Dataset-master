from __future__ import annotations

import argparse
import io
import os
import secrets
import threading
import webbrowser
from urllib.parse import urlencode

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from leaflab.dataset import Catalog
from leaflab.model import ModelService, read_image


def create_app(catalog=None, service=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=9 * 1024 * 1024,
                      TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"])
    catalog = catalog if catalog is not None else Catalog()
    service = service if service is not None else ModelService(catalog)
    token = secrets.token_urlsafe(32)
    app.extensions["model_service"] = service

    @app.before_request
    def protect_local_actions():
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-App-Token", ""), token):
            return jsonify(error="กรุณารีเฟรชหน้าเว็บแล้วลองใหม่"), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; media-src 'self' blob:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if request.path.startswith("/api/") and request.path != "/api/image":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(LookupError)
    def missing_model(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = "ไฟล์ใหญ่เกินไป กรุณาใช้ภาพไม่เกิน 8 MB" if error.code == 413 else "ไม่พบข้อมูล หรือคำขอไม่ถูกต้อง"
        return jsonify(error=message), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception("Request failed")
        return jsonify(error="เกิดข้อผิดพลาด กรุณาลองอีกครั้ง หรือตรวจหน้าต่างโปรแกรม"), 500

    @app.get("/")
    def index():
        return render_template("index.html", token=token)

    @app.get("/api/health")
    def health():
        return jsonify(app="leaf-lab", status="ok", pid=os.getpid())

    @app.get("/api/catalog")
    def dataset_summary():
        return jsonify(catalog.summary())

    @app.get("/api/status")
    def status():
        return jsonify(service.status())

    def image_args(data):
        label = data.get("label", "")
        if not isinstance(label, str):
            raise ValueError("ชื่อกลุ่มภาพไม่ถูกต้อง")
        try:
            index = int(data.get("index", -1))
        except (ValueError, TypeError):
            raise ValueError("หมายเลขภาพไม่ถูกต้อง")
        return label, index, catalog.path(label, index)

    @app.get("/api/image")
    def dataset_image():
        _, _, path = image_args(request.args)
        image = read_image(path)
        image.thumbnail((384, 384))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86)
        buffer.seek(0)
        return send_file(buffer, mimetype="image/jpeg", max_age=3600)

    @app.get("/api/examples")
    def examples():
        from leaflab.dataset import label_info
        label = request.args.get("label", "")
        try:
            page = max(0, int(request.args.get("page", "0")))
        except ValueError:
            raise ValueError("หมายเลขหน้าไม่ถูกต้อง")
        choices = []
        if label:
            if label not in catalog.files:
                raise ValueError("ไม่พบกลุ่มภาพที่เลือก")
            total = len(catalog.files[label])
            for index in range(page*12, min((page+1)*12, total)):
                choices.append((label, index))
        else:
            labels = list(catalog.files)
            total = len(labels)
            choices = [(value, 0) for value in labels[page*12:(page+1)*12] if catalog.files[value]]
        with service.lock:
            artifact = service.artifact
        membership = {row["path"]: row["split"] for row in artifact["manifest"]} if artifact else {}
        rows = []
        for selected, index in choices:
            path = catalog.path(selected, index)
            split = membership.get(f"{selected}/{path.name}", "unused")
            rows.append({**label_info(selected), "index": index, "split": split,
                         "url": "/api/image?" + urlencode({"label": selected, "index": index})})
        return jsonify(items=rows, page=page, pages=max(1, (total+11)//12), total=total)

    @app.post("/api/predict")
    def predict():
        if not service.status()["ready"]:
            raise LookupError("กรุณาฝึกโมเดลก่อนเริ่มวิเคราะห์ภาพ")
        uploaded = request.files.get("image")
        if uploaded is None or not uploaded.filename:
            raise ValueError("กรุณาเลือกภาพก่อน")
        payload = uploaded.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            return jsonify(error="กรุณาใช้ภาพไม่เกิน 8 MB"), 413
        image = read_image(io.BytesIO(payload))
        return jsonify({**service.predict(image), "source": "upload"})

    @app.post("/api/predict-example")
    def predict_example():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("ข้อมูลภาพไม่ถูกต้อง")
        label, _, path = image_args(data)
        prediction = service.predict(read_image(path))
        with service.lock:
            artifact = service.artifact
        split = next((row["split"] for row in artifact["manifest"]
                      if row["path"] == f"{label}/{path.name}"), "unused")
        return jsonify({**prediction, "source": "dataset", "actual_label": label, "split": split})

    @app.post("/api/train")
    def train():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("กรุณาเลือกขนาดชุดฝึก")
        size = data.get("samples_per_class", 300)
        if type(size) is not int or size not in (150, 300, 600):
            raise ValueError("เลือกขนาดชุดฝึก 150, 300 หรือ 600 ภาพต่อกลุ่ม")
        service.start_training(size)
        return jsonify(service.status()), 202

    return app


def main():
    parser = argparse.ArgumentParser(description="Leaf Lab — local plant image classifier")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if args.open_browser:
        import json
        from urllib.request import urlopen
        try:
            with urlopen(url + "/api/health", timeout=1) as response:
                if json.load(response).get("app") == "leaf-lab":
                    webbrowser.open(url)
                    return
        except (OSError, ValueError):
            pass
    app = create_app()
    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Leaf Lab is ready at {url}\nPress Ctrl+C to stop.", flush=True)
    from waitress import serve
    serve(app, host="127.0.0.1", port=args.port, threads=4, max_request_body_size=9*1024*1024)


if __name__ == "__main__":
    main()
