"""PC-only checks for the detailed offline map builder (no survey/model data)."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

AVAILABLE=all(importlib.util.find_spec(name) for name in ('osmium','shapely','aggdraw'))
if AVAILABLE:
    from shapely.geometry import LineString, Point, Polygon
    from tools import build_detailed_map as builder

@unittest.skipUnless(AVAILABLE,'PC map-build dependencies are optional on Pi')
class DetailedMapBuilderTests(unittest.TestCase):
    def test_all_road_classes_include_minor_and_agricultural_access(self):
        for category in ('residential','unclassified','service','living_street','track','path','footway','cycleway'):
            self.assertIn(category,builder.HIGHWAYS)
            self.assertLessEqual(builder.HIGHWAYS[category][3],16)

    def test_full_region_has_complete_native_zoom_16_budget(self):
        bounds=[100.823265,14.1103894,105.646812,18.458611]
        self.assertEqual(builder.expected_tiles(bounds,16),725175)
        self.assertEqual(sum(builder.expected_tiles(bounds,z) for z in range(7,17)),968144)

    def test_projection_roundtrips_without_fabricating_geography(self):
        geometry=builder.project_geometry(Point(103.652,16.053))
        lon,lat=builder.lonlat(geometry.x,geometry.y,16)
        self.assertAlmostEqual(lon,103.652,places=10)
        self.assertAlmostEqual(lat,16.053,places=10)

    def test_farm_track_visible_at_native_16_and_not_as_motorway(self):
        mx,my=50000,30000
        geometry=LineString([(mx*256+20,my*256+100),(mx*256+235,my*256+100)])
        image=builder.render_metatile([('road','track','',geometry.wkb)],16,mx,my)
        self.assertNotEqual(image.getpixel((100,100)),Image.new('RGB',(1,1),builder.BG).getpixel((0,0)))
        r,g,b=image.getpixel((100,100));self.assertGreater(r,b)

    def test_service_road_visible_at_native_16_but_hidden_at_overview(self):
        mx,my=50000,30000
        geometry=LineString([(mx*256+20,my*256+100),(mx*256+235,my*256+100)])
        image=builder.render_metatile([('road','service','',geometry.wkb)],16,mx,my)
        self.assertNotEqual(image.getpixel((100,100)),Image.new('RGB',(1,1),builder.BG).getpixel((0,0)))
        hidden=builder.render_metatile([('road','service','',geometry.wkb)],12,mx//16,my//16)
        self.assertEqual(hidden.getextrema(),Image.new('RGB',hidden.size,builder.BG).getextrema())

    def test_rtree_retains_road_crossing_view_without_endpoint_inside(self):
        with tempfile.TemporaryDirectory() as temporary:
            db=builder.create_features(Path(temporary)/'features.sqlite3')
            db.execute('INSERT INTO spatial VALUES(1,0,1000,499,501)')
            self.assertEqual(db.execute('SELECT id FROM spatial WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=?',(600,400,600,400)).fetchall(),[(1,)])
            db.close()

    def test_reservoir_islands_stay_unfilled_after_agg_antialiasing(self):
        mx,my=50000,30000;ox,oy=mx*256,my*256
        outer=[(ox+20,oy+20),(ox+400,oy+20),(ox+400,oy+400),(ox+20,oy+400),(ox+20,oy+20)]
        hole=[(ox+180,oy+180),(ox+280,oy+180),(ox+280,oy+280),(ox+180,oy+280),(ox+180,oy+180)]
        geometry=Polygon(outer,[hole])
        image=builder.render_metatile([('water','reservoir','',geometry.wkb)],16,mx,my)
        self.assertEqual(image.getpixel((100,100)),(180,215,223))
        self.assertEqual(image.getpixel((230,230)),(241,242,233))

    def test_completed_map_cannot_be_modified_by_accidental_rebuild(self):
        from tests.test_offline_maps import make_map
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);target=make_map(directory/'completed.mbtiles')
            before=target.read_bytes()
            with self.assertRaises(FileExistsError):builder.render(directory,target,7,16)
            self.assertEqual(target.read_bytes(),before)

    def test_non_map_output_is_rejected_before_touching_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);target=directory/'scans.sqlite3'
            target.write_bytes(b'private survey data must remain untouched')
            before=target.read_bytes()
            with self.assertRaises(ValueError):builder.render(directory,target,7,16)
            self.assertEqual(target.read_bytes(),before)

    def test_uniform_tile_is_valid_lossless_native_size(self):
        payload=builder.uniform_png(builder.BG)
        image=Image.open(io.BytesIO(payload));image.load()
        self.assertEqual(image.format,'PNG');self.assertEqual(image.size,(256,256))

if __name__=='__main__':unittest.main()
