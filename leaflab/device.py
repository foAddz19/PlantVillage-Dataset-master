import csv
import io
import json

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from PIL import Image

from .model import read_image
from .scans import ScanStore, location_fields


def device_routes(service, token, directory):
    routes = Blueprint("device", __name__)
    store = ScanStore(directory)

    @routes.get("/device")
    def screen():
        return render_template("device.html", token=token)

    @routes.post("/api/device/analyze")
    def analyze():
        location = location_fields(request.form)
        source = request.form.get("source", "upload")
        if source not in {"upload", "camera", "sample"}:
            raise ValueError("แหล่งภาพไม่ถูกต้อง")
        uploaded = request.files.get("image")
        if uploaded is None:
            raise ValueError("กรุณาถ่ายภาพหรือเลือกภาพ")
        payload = uploaded.read(8*1024*1024+1)
        if len(payload) > 8*1024*1024:
            return jsonify(error="ภาพใหญ่เกิน 8 MB"), 413
        image = read_image(io.BytesIO(payload))
        with Image.open(io.BytesIO(payload)) as original:
            mime = Image.MIME[original.format]
        prediction = service.predict(image)
        return jsonify(store.draft(payload, mime, prediction, source, location)), 201

    @routes.post("/api/device/scans/<identifier>/save")
    def save(identifier):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("ข้อมูลบันทึกไม่ถูกต้อง")
        return jsonify(store.save(identifier, data.get("severity", "unassessed"), data.get("note", "")))

    @routes.get("/api/device/scans")
    def history():
        return jsonify(store.list())

    @routes.get("/api/device/scans/<identifier>")
    def detail(identifier):
        return jsonify(store.get(identifier))

    @routes.get("/api/device/scans/<identifier>/image")
    def image(identifier):
        payload, mime = store.image(identifier)
        return send_file(io.BytesIO(payload), mimetype=mime)

    @routes.get("/api/device/export/<kind>")
    def export(kind):
        records = store.list(limit=None)["items"]
        if kind == "geojson":
            features = []
            for row in records:
                properties = {k: v for k, v in row.items() if k not in {"latitude", "longitude", "image_url"}}
                features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]}, "properties": properties})
            return Response(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
                            mimetype="application/geo+json", headers={"Content-Disposition": 'attachment; filename="plant-scans.geojson"'})
        if kind != "csv":
            raise ValueError("รองรับ CSV และ GeoJSON")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("id", "captured_at", "crop", "disease", "score", "uncertain", "latitude", "longitude", "location_source", "severity_by_observer", "note", "model_created_at"))
        for row in records:
            best = row["prediction"]["candidates"][0]
            note = row["note"]
            if note.lstrip().startswith(("=", "+", "-", "@")) or note.startswith(("\t", "\r", "\n")):
                note = "'"+note
            writer.writerow((row["id"], row["captured_at"], best["crop_th"], best["disease_th"], best["score"],
                             row["prediction"]["uncertain"], row["latitude"], row["longitude"], row["location_source"], row["severity"], note, row["prediction"]["model_created_at"]))
        return Response("\ufeff"+output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="plant-scans.csv"'})

    return routes
