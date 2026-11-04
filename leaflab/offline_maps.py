"""Small, read-only raster MBTiles service. No external map requests are made."""
from __future__ import annotations

import io
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request
from PIL import Image, UnidentifiedImageError

MAX_TILE_BYTES = 2 * 1024 * 1024
MAX_MAP_BYTES = 16 * 1024 ** 3
MAX_TILES = 2_000_000
MAX_ZOOM = 20
MERCATOR_LIMIT = 85.051129


class OfflineMapError(ValueError):
    """A local raster package cannot be used safely or consistently."""


def _text(value, limit=300):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()[:limit]


def _safe_url(value):
    candidate = _text(value, 1000)
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
            return candidate
    except ValueError:
        pass
    return None


class _AttributionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.url = None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self.hidden += 1
        if tag.lower() == "a" and not self.url:
            self.url = _safe_url(dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _attribution(metadata):
    parser = _AttributionParser()
    parser.feed(metadata.get("attribution", ""))
    return {"text": _text(" ".join(parser.parts), 600),
            "url": _safe_url(metadata.get("attribution_url", "")) or parser.url}


@contextmanager
def _connect(path, *, seconds=3):
    # immutable avoids journal creation and expensive recovery; imported packages
    # must be self-contained snapshots, never databases being edited in place.
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.25)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-512")
        connection.execute("PRAGMA mmap_size=0")
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_TILE_BYTES + 4096)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 10_000)
        deadline = time.monotonic() + seconds
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        yield connection
    finally:
        connection.close()


def _number_list(value, count, field):
    try:
        result = [float(part.strip()) for part in value.split(",")]
    except (AttributeError, TypeError, ValueError) as error:
        raise OfflineMapError(f"Map {field} metadata is invalid.") from error
    if len(result) != count or not all(math.isfinite(number) for number in result):
        raise OfflineMapError(f"Map {field} metadata is invalid.")
    return result


def _integer(value, field, minimum=0, maximum=MAX_ZOOM):
    try:
        result = int(value)
        if str(result) != str(value).strip() or not minimum <= result <= maximum:
            raise ValueError
        return result
    except (ValueError, TypeError) as error:
        raise OfflineMapError(f"Map {field} metadata is invalid.") from error


def _tile_info(payload):
    if not isinstance(payload, bytes) or not 8 <= len(payload) <= MAX_TILE_BYTES:
        raise OfflineMapError("Map tile is empty or exceeds the 2 MiB limit.")
    expected = "PNG" if payload.startswith(b"\x89PNG\r\n\x1a\n") else "JPEG" if payload.startswith(b"\xff\xd8\xff") else None
    if expected is None:
        raise OfflineMapError("Only PNG and JPEG raster tiles are supported.")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != expected or image.size not in {(256, 256), (512, 512)}:
                raise OfflineMapError("Map tiles must be square 256 or 512 pixel PNG/JPEG images.")
            size = image.width
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        if isinstance(error, OfflineMapError):
            raise
        raise OfflineMapError("Map contains a damaged raster tile.") from error
    return ("png" if expected == "PNG" else "jpg"), size


def _tile_coordinate(z, x, y):
    return all(type(value) is int for value in (z, x, y)) and 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def inspect_mbtiles(path, *, validate_tiles=False):
    """Inspect a package; full validation streams every tile with bounded memory."""
    path = Path(path)
    if not path.is_file():
        raise OfflineMapError("Offline map file was not found.")
    if not 100 <= path.stat().st_size <= MAX_MAP_BYTES:
        raise OfflineMapError("Offline map file is empty or exceeds the 16 GiB limit.")
    try:
        with _connect(path, seconds=300 if validate_tiles else 3) as connection:
            lookup_plan = connection.execute("EXPLAIN QUERY PLAN SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1", (0, 0, 0)).fetchall()
            if not any("search " in str(row[-1]).lower() and all(field in str(row[-1]).lower() for field in ("zoom_level", "tile_column", "tile_row")) for row in lookup_plan):
                raise OfflineMapError("Map needs an index on zoom_level, tile_column and tile_row for fast local access.")
            rows = connection.execute("SELECT name, value FROM metadata LIMIT 257").fetchall()
            if len(rows) > 256 or not rows:
                raise OfflineMapError("Map metadata is missing or too large.")
            metadata = {}
            for name, value in rows:
                if not isinstance(name, str) or not isinstance(value, str) or len(name) > 100 or len(value) > 4096 or name in metadata:
                    raise OfflineMapError("Map metadata keys and values are invalid or duplicated.")
                metadata[name] = value
            if metadata.get("scheme", "tms").lower() != "tms":
                raise OfflineMapError("MBTiles must use the standard TMS tile row convention.")
            declared_format = metadata.get("format", "").lower()
            if declared_format not in {"png", "jpg", "jpeg"}:
                raise OfflineMapError("Only PNG/JPEG MBTiles are supported; vector PBF/WebP maps cannot be displayed.")
            bounds = _number_list(metadata.get("bounds"), 4, "bounds")
            west, south, east, north = bounds
            if not (-180 <= west < east <= 180 and -MERCATOR_LIMIT <= south < north <= MERCATOR_LIMIT):
                raise OfflineMapError("Map bounds are outside the Web Mercator range or cross the date line.")
            if "minzoom" in metadata and "maxzoom" in metadata:
                minimum = _integer(metadata["minzoom"], "minzoom")
                maximum = _integer(metadata["maxzoom"], "maxzoom")
            else:
                minimum, maximum = connection.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
                minimum = _integer(minimum, "minzoom")
                maximum = _integer(maximum, "maxzoom")
            if minimum > maximum:
                raise OfflineMapError("Map minzoom exceeds maxzoom.")
            first = connection.execute("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles LIMIT 1").fetchone()
            if not first or not _tile_coordinate(*first[:3]) or not minimum <= first[0] <= maximum:
                raise OfflineMapError("Map has no valid raster tiles.")
            tile_format, tile_size = _tile_info(first[3])
            if tile_format != ("jpg" if declared_format == "jpeg" else declared_format):
                raise OfflineMapError("Tile image format does not match map metadata.")
            if "tile_size" in metadata and _integer(metadata["tile_size"], "tile_size", 256, 512) != tile_size:
                raise OfflineMapError("Tile image dimensions do not match map metadata.")
            center = _number_list(metadata["center"], 3, "center") if metadata.get("center") else [(west + east) / 2, (south + north) / 2, minimum]
            if not (west <= center[0] <= east and south <= center[1] <= north and minimum <= center[2] <= maximum):
                raise OfflineMapError("Map center is outside its bounds or zoom range.")
            result = dict(ready=True, name=_text(metadata.get("name", "Offline map"), 160), bounds=bounds,
                          minzoom=minimum, maxzoom=maximum, center=center, tile_size=tile_size,
                          format=tile_format, attribution=_attribution(metadata), size_bytes=path.stat().st_size)
            # Force schema/type validation even when the first tile happened to
            # be readable. Full validation is used before import, not per request.
            if validate_tiles:
                count = connection.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                if not 1 <= count <= MAX_TILES:
                    raise OfflineMapError("Map must contain between 1 and 2,000,000 tiles.")
                if connection.execute("SELECT zoom_level,tile_column,tile_row FROM tiles GROUP BY zoom_level,tile_column,tile_row HAVING COUNT(*)>1 LIMIT 1").fetchone():
                    raise OfflineMapError("Map contains duplicate tile coordinates.")
                actual_min, actual_max = MAX_ZOOM, 0
                for z, x, y, payload in connection.execute("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
                    if not _tile_coordinate(z, x, y) or not minimum <= z <= maximum:
                        raise OfflineMapError("Map contains an invalid or out-of-range tile coordinate.")
                    if _tile_info(payload) != (tile_format, tile_size):
                        raise OfflineMapError("Map tile image formats or dimensions are inconsistent.")
                    actual_min, actual_max = min(actual_min, z), max(actual_max, z)
                if (actual_min, actual_max) != (minimum, maximum):
                    raise OfflineMapError("Map zoom metadata does not match its stored tiles.")
                result["tile_count"] = count
            return result
    except (sqlite3.Error, OSError) as error:
        raise OfflineMapError("Offline map database is unreadable, unsupported, or exceeds validation limits.") from error


def import_mbtiles(source, destination, *, replace=False):
    """Validate and atomically install a self-contained local raster package."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise OfflineMapError("Source and destination must be different files.")
    if source.suffix.lower() != ".mbtiles" or destination.suffix.lower() != ".mbtiles":
        raise OfflineMapError("Source and destination must use the .mbtiles extension.")
    if destination.exists() and not replace:
        raise OfflineMapError("Destination already exists; use --replace to replace this map only.")
    if Path(str(source) + "-wal").exists() or Path(str(source) + "-journal").exists():
        raise OfflineMapError("Close the map editor and export a self-contained MBTiles file before importing.")
    result = inspect_mbtiles(source, validate_tiles=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".offline-map-", suffix=".mbtiles", dir=destination.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        # Recheck the copy as well: an editor could have changed the source
        # during validation/copy. No existing installation has changed yet.
        copied = inspect_mbtiles(temporary, validate_tiles=True)
        if copied != result:
            raise OfflineMapError("Source map changed while it was being copied; import a closed snapshot.")
        if replace:
            os.replace(temporary, destination)
        else:
            # Atomic no-clobber publication, including a competing import.
            os.link(temporary, destination)
            temporary.unlink()
        return result
    except FileExistsError as error:
        raise OfflineMapError("Destination already exists; use --replace to replace this map only.") from error
    except OSError as error:
        raise OfflineMapError("Offline map could not be installed; check disk space and directory permissions.") from error
    finally:
        temporary.unlink(missing_ok=True)


def offline_map_routes(directory):
    """Create routes for directory/current.mbtiles without touching survey data."""
    routes = Blueprint("offline_maps", __name__)
    path = Path(directory) / "current.mbtiles"
    cached = {"identity": None, "status": None}
    lock = threading.Lock()

    def status():
        try:
            stat = path.stat()
            identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            return dict(ready=False, code="missing", error="No offline map is installed.")
        with lock:
            if cached["identity"] != identity:
                try:
                    value = inspect_mbtiles(path)
                    value["revision"] = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
                    value["version"] = value["revision"]
                except (OfflineMapError, OSError) as error:
                    value = dict(ready=False, code="invalid", error=str(error) if isinstance(error, OfflineMapError) else "Offline map is unavailable.")
                cached.update(identity=identity, status=value)
            return dict(cached["status"])

    @routes.get("/api/device/map/status")
    def map_status():
        response = jsonify(status())
        response.headers["Cache-Control"] = "no-store"
        return response

    @routes.get("/api/device/map/tiles/<int:z>/<int:x>/<int:y>.png")
    def map_tile(z, x, y):
        info = status()
        if not _tile_coordinate(z, x, y) or not info.get("ready") or not info["minzoom"] <= z <= info["maxzoom"]:
            return Response(status=404, headers={"Cache-Control": "no-store"})
        try:
            with _connect(path) as connection:
                row = connection.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
                                         (z, x, (1 << z) - 1 - y)).fetchone()
            if not row or _tile_info(row[0]) != (info["format"], info["tile_size"]):
                return Response(status=404, headers={"Cache-Control": "no-store"})
            response = Response(row[0], mimetype="image/png" if info["format"] == "png" else "image/jpeg")
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.set_etag(f"{info['revision']}-{z}-{x}-{y}")
            return response.make_conditional(request)
        except (OfflineMapError, sqlite3.Error, OSError):
            return Response(status=404, headers={"Cache-Control": "no-store"})

    return routes
