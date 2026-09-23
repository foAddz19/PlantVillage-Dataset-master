"""Local SQLite store: immutable analyzed frame/location, explicit idempotent save."""
import json
import math
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path


def location_fields(data):
    mode = data.get("location_source", "simulated")
    if mode not in {"simulated", "manual"}:
        raise ValueError("เลือกพิกัดจำลองหรือระบุพิกัดเอง")
    coordinates = []
    for field, limit in (("latitude", 90), ("longitude", 180)):
        try:
            raw = data.get(field)
            if isinstance(raw, bool):
                raise ValueError()
            value = float(raw)
            if not math.isfinite(value) or not -limit <= value <= limit:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("พิกัดไม่ถูกต้อง: ละติจูด −90 ถึง 90 และลองจิจูด −180 ถึง 180")
        coordinates.append(value)
    return {**dict(zip(("latitude", "longitude"), coordinates)), "location_source": mode}


class ScanStore:
    COLUMNS = "id, captured_at, saved_at, source, latitude, longitude, location_source, prediction, severity, note"

    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "scans.sqlite3"
        with self.connection() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, saved_at TEXT,
                source TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                location_source TEXT NOT NULL, prediction TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'unassessed', note TEXT NOT NULL DEFAULT '',
                image BLOB NOT NULL, mime TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS scans_saved ON scans(saved_at)")

    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return closing(db)

    @staticmethod
    def decode(row):
        value = dict(row)
        value["prediction"] = json.loads(value["prediction"])
        value["image_url"] = f"/api/device/scans/{value['id']}/image"
        return value

    def draft(self, payload, mime, prediction, source, location):
        identifier = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db, db:
            # Only abandoned drafts expire; saved records are never automatically removed.
            db.execute("DELETE FROM scans WHERE saved_at IS NULL AND captured_at < ?",
                       ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
            db.execute("""INSERT INTO scans
                (id,captured_at,source,latitude,longitude,location_source,prediction,image,mime)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (identifier, now, source, location["latitude"], location["longitude"], location["location_source"],
                 json.dumps(prediction, ensure_ascii=False), payload, mime))
        return self.get(identifier)

    def get(self, identifier):
        with self.connection() as db:
            row = db.execute(f"SELECT {self.COLUMNS} FROM scans WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LookupError("ไม่พบรายการตรวจนี้")
        return self.decode(row)

    def image(self, identifier):
        with self.connection() as db:
            row = db.execute("SELECT image,mime FROM scans WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LookupError("ไม่พบภาพนี้")
        return row["image"], row["mime"]

    def save(self, identifier, severity="unassessed", note=""):
        if not isinstance(severity, str) or severity not in {"unassessed", "mild", "severe"}:
            raise ValueError("ระดับความรุนแรงไม่ถูกต้อง")
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("บันทึกเพิ่มเติมได้ไม่เกิน 500 ตัวอักษร")
        with self.connection() as db, db:
            db.execute("UPDATE scans SET saved_at=?,severity=?,note=? WHERE id=? AND saved_at IS NULL",
                       (datetime.now(timezone.utc).isoformat(), severity, note.strip(), identifier))
        return self.get(identifier)

    def list(self, limit=200):
        with self.connection() as db:
            query = f"SELECT {self.COLUMNS} FROM scans WHERE saved_at IS NOT NULL ORDER BY saved_at DESC,id"
            rows = db.execute(query + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
            total = db.execute("SELECT count(*) FROM scans WHERE saved_at IS NOT NULL").fetchone()[0]
        return {"items": [self.decode(row) for row in rows], "total": total}
