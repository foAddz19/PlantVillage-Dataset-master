"""Build real zoom-16 offline Isan roads from a checksummed Geofabrik PBF.

PC-only preparation: osmium + shapely + aggdraw + existing Pillow/Thai font.
No standard online map tiles are requested. Runtime Pi dependencies unchanged.
Extract uses a disk-backed node-location cache and an SQLite RTree. Rendering
uses bounded metatiles with transactional resume, never a region-sized canvas.
"""
from __future__ import annotations
import argparse
import collections
import contextlib
import concurrent.futures
import functools
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from tools.render_offline_map import prepare_cpu_budget, xy, lonlat, water_rings, display_name
from leaflab.receipt import ThaiFont
from benchmark_paddy import memory_mib
from PIL import Image, ImageDraw, ImageFilter, ImageOps
import osmium
import shapely
import aggdraw
from shapely.geometry import Point, LineString, Polygon

BUILD = PROJECT / 'runtime' / 'isan-detail-z16'
REF_ZOOM=16
META=8
PAD=192
SCALE=2
BG='#f1f2e9'
WATER='#b4d7df'
HIGHWAYS={
 'motorway':(7.,'#e6bc7d',9,7),'motorway_link':(5.,'#e6bc7d',8,9),
 'trunk':(6.5,'#eac48a',8,7),'trunk_link':(4.5,'#eac48a',7,9),
 'primary':(6.,'#f0d39c',7,7),'primary_link':(4.,'#f0d39c',6,9),
 'secondary':(5.,'#f7e4b5',6,9),'secondary_link':(4.,'#f7e4b5',5,11),
 'tertiary':(4.,'#fffbed',5,11),'tertiary_link':(3.,'#fffbed',4,12),
 'unclassified':(3.,'#ffffff',4,12),'residential':(3.,'#ffffff',3,12),
 'living_street':(2.5,'#ffffff',3,13),'service':(2.,'#ffffff',2,14),
 'track':(1.7,'#b89e72',1,14),'path':(1.,'#9a9b85',0,15),
 'footway':(1.,'#9a9b85',0,15),'cycleway':(1.3,'#789d88',0,15),
 'pedestrian':(2.,'#ddd5c4',2,14),'steps':(1.,'#9a9b85',0,16),
 'bridleway':(1.,'#9a9b85',0,15),
}

def emit(**value):
    print(json.dumps(value,ensure_ascii=True),flush=True)

def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()

def safe_memory(directory=BUILD):
    memory=memory_mib()
    if memory and memory['current']>1800:
        raise RuntimeError('Map preparation exceeds 1800 MiB memory budget')
    if shutil.disk_usage(directory).free<2*1024**3:
        raise RuntimeError('Less than 2 GiB free on build volume; preserving resumable build')
    return memory

def tile_range(bounds,z):
    west,south,east,north=bounds
    a,b=xy(west,north,z);c,d=xy(east,south,z)
    return math.floor(a/256),math.floor(b/256),math.floor(c/256),math.floor(d/256)

def expected_tiles(bounds,z):
    x0,y0,x1,y1=tile_range(bounds,z)
    return (x1-x0+1)*(y1-y0+1)

def create_features(path):
    connection=sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.execute('PRAGMA cache_size=-16384')
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS features(id INTEGER PRIMARY KEY, osm_type TEXT, osm_id INTEGER,
      kind TEXT, category TEXT, name TEXT, tags TEXT, wgs84 BLOB, pixels BLOB);
    CREATE VIRTUAL TABLE IF NOT EXISTS spatial USING rtree(id,minx,maxx,miny,maxy);
    ''')
    return connection

def project_geometry(geometry):
    def transform(coordinates):
        import numpy as np
        result=coordinates.copy()
        n=256*2**REF_ZOOM
        result[:,0]=(coordinates[:,0]+180)/360*n
        latitude=np.clip(coordinates[:,1],-85.05112878,85.05112878)
        result[:,1]=(1-np.arcsinh(np.tan(np.radians(latitude)))/math.pi)/2*n
        return result
    return shapely.transform(geometry,transform)

def extract(directory):
    source=directory/'thailand.osm.pbf'
    download=json.loads((directory/'download-manifest.json').read_text(encoding='utf-8'))
    if digest(source)!=download['sha256']:
        raise ValueError('Country extract SHA256 mismatch')
    old=json.loads((PROJECT/'maps/source-manifest.json').read_text(encoding='utf-8'))
    south,west,north,east=old['bounds']
    bounds=[west,south,east,north]
    features_path=directory/'features.sqlite3'
    db=create_features(features_path)
    metadata=dict(db.execute('SELECT name,value FROM metadata'))
    if metadata.get('complete')=='true':
        if metadata.get('source_sha256')!=download['sha256']:
            raise ValueError('Feature cache belongs to another source')
        emit(stage='extract-cached',features=db.execute('SELECT COUNT(*) FROM features').fetchone()[0])
        db.close();return
    if db.execute('SELECT COUNT(*) FROM features').fetchone()[0]:
        raise RuntimeError('Incomplete feature cache exists; use a new build directory')
    started=time.monotonic(); counts=collections.Counter(); skipped=collections.Counter();total=0
    region=shapely.box(west,south,east,north)
    def insert(osm_type,osm_id,kind,category,name,tags,geometry):
        nonlocal total
        if geometry.is_empty or not geometry.intersects(region):return
        pixels=project_geometry(geometry)
        xmin,ymin,xmax,ymax=pixels.bounds
        cursor=db.execute('INSERT INTO features(osm_type,osm_id,kind,category,name,tags,wgs84,pixels) VALUES(?,?,?,?,?,?,?,?)',
            (osm_type,osm_id,kind,category,name,json.dumps(tags,ensure_ascii=False),geometry.wkb,pixels.wkb))
        db.execute('INSERT INTO spatial VALUES(?,?,?,?,?)',(cursor.lastrowid,xmin,xmax,ymin,ymax))
        counts[f'{kind}:{category}']+=1;total+=1
        if total%10000==0:
            db.commit();emit(stage='extract',features=total,seconds=round(time.monotonic()-started,1),memory_mib=safe_memory(directory))
    index_path=directory/'node-locations.idx'
    processor=osmium.FileProcessor(str(source)).with_locations('sparse_file_array,'+str(index_path))
    processor=processor.with_filter(osmium.filter.KeyFilter('highway','waterway','natural','place'))
    for element in processor:
        tags=dict(element.tags)
        name=display_name(tags) or ''
        if element.is_node():
            place=tags.get('place')
            if place in ('city','town','village','hamlet','locality') and name and west<=element.lon<=east and south<=element.lat<=north:
                insert('node',element.id,'place',place,name,tags,Point(element.lon,element.lat))
        elif element.is_way():
            highway=tags.get('highway');waterway=tags.get('waterway')
            water=tags.get('natural')=='water'
            if highway not in HIGHWAYS and waterway not in ('river','stream','canal','drain') and not water:continue
            if any(not point.location.valid() for point in element.nodes):
                skipped['incomplete_way']+=1;continue
            coordinates=[(point.lon,point.lat) for point in element.nodes]
            if len(coordinates)<2:
                skipped['short_way']+=1;continue
            if highway in HIGHWAYS:
                insert('way',element.id,'road',highway,tags.get('ref') or name,tags,LineString(coordinates))
            elif waterway:
                insert('way',element.id,'waterway',waterway,name,tags,LineString(coordinates))
            elif len(coordinates)>=4 and coordinates[0]==coordinates[-1]:
                geometry=Polygon(coordinates)
                if not geometry.is_valid:geometry=shapely.make_valid(geometry)
                if geometry.geom_type in ('Polygon','MultiPolygon'):
                    insert('way',element.id,'water','water',name,tags,geometry)
    # Add the existing, validated large reservoir multipolygons. Their distinct
    # source timestamp is recorded; no fabricated land parcels or roads.
    water_path=directory/'water-relations.osm.json'
    if water_path.exists():
        for relation in json.loads(water_path.read_text(encoding='utf-8')).get('elements',[]):
            outers=water_rings(relation,'outer');inners=water_rings(relation,'inner')
            for ring in outers:
                polygon=Polygon(ring)
                holes=[inner for inner in inners if polygon.covers(Point(inner[0]))]
                geometry=Polygon(ring,holes)
                if not geometry.is_valid:geometry=shapely.make_valid(geometry)
                if geometry.geom_type in ('Polygon','MultiPolygon'):
                    tags=relation.get('tags',{})
                    insert('relation',relation['id'],'water','reservoir',display_name(tags) or '',tags,geometry)
    with osmium.io.Reader(str(source)) as reader:
        header=reader.header()
        timestamp=header.get('osmosis_replication_timestamp') or download.get('last_modified')
    manifest={'name':'AI Plant detailed Northeast Thailand roads zoom 7-16','region':'Northeast Thailand / Isan',
      'province_iso_codes':old['province_iso_codes'],'bounds':[south,west,north,east],
      'center':[(west+east)/2,(south+north)/2],'osm_timestamp':timestamp,'source_download':download,
      'source_sha256':download['sha256'],'copyright':'OpenStreetMap contributors','license':'ODbL-1.0',
      'license_url':'https://opendatacommons.org/licenses/odbl/1-0/',
      'attribution_url':'https://www.openstreetmap.org/copyright',
      'coverage':'All 20 Isan provinces within the existing bounding rectangle. Thailand extract; some adjoining foreign territory has no detailed data.',
      'detail':'Actual OSM major/minor roads, residential/service roads, farm tracks, paths, waterways and place names. No satellite imagery, invented farm entrances or routing.',
      'road_classes':sorted(HIGHWAYS),'counts':dict(counts),'skipped':dict(skipped),
      'water_relations_timestamp':old.get('water_relations_timestamp'),
      'estimated_tiles':{str(z):expected_tiles(bounds,z) for z in range(7,17)},
      'rebuild_source':'source.geojsonseq.gz','source_format':'RFC 8142 GeoJSON text sequence, WGS84, ODbL 1.0'}
    db.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)',
        [('complete','true'),('bounds',json.dumps(bounds)),('source_sha256',download['sha256'])])
    db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('PRAGMA journal_mode=DELETE');db.close()
    (directory/'source-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    emit(stage='extract-complete',features=total,counts=dict(counts),skipped=dict(skipped),seconds=round(time.monotonic()-started,1),memory_mib=memory_mib())

@functools.lru_cache(maxsize=12)
def font(size):
    return ThaiFont(size*SCALE,font_path=PROJECT/'assets/fonts/NotoSansThaiLooped.ttf',weight=500)

@functools.lru_cache(maxsize=512)
def sprite(text,size,color):
    shaped=font(size);width=math.ceil(shaped.width(text))+12*SCALE
    coverage=Image.new('L',(width,shaped.height+4*SCALE),255)
    shaped.draw(coverage,text,6*SCALE,2*SCALE)
    mask=ImageOps.invert(coverage);box=mask.getbbox()
    if box:
        mask=mask.crop((max(0,box[0]-3*SCALE),max(0,box[1]-3*SCALE),min(width,box[2]+3*SCALE),min(coverage.height,box[3]+3*SCALE)))
    halo=Image.new('RGBA',mask.size,BG);halo.putalpha(mask.filter(ImageFilter.MaxFilter(7)))
    ink=Image.new('RGBA',mask.size,color);ink.putalpha(mask);halo.alpha_composite(ink)
    return halo

@functools.lru_cache(maxsize=12)
def uniform_png(color):
    image=Image.new('RGB',(256,256),color);output=io.BytesIO();image.save(output,format='PNG',compress_level=3)
    return output.getvalue()

def parts(geometry):
    if geometry.geom_type.startswith('Multi') or geometry.geom_type=='GeometryCollection':
        for value in geometry.geoms:yield from parts(value)
    else:yield geometry

class AntialiasCanvas:
    """Native-resolution AGG paths; no expensive full-canvas supersampling."""
    def __init__(self,image):self.context=aggdraw.Draw(image)
    def line(self,coordinates,fill,width,joint=None):
        if len(coordinates)<2:return
        path=aggdraw.Path();path.moveto(*coordinates[0])
        for point in coordinates[1:]:path.lineto(*point)
        self.context.path(path,aggdraw.Pen(fill,width))
    def polygon(self,rings,fill):
        path=aggdraw.Path()
        for coordinates in rings:
            if len(coordinates)<3:continue
            path.moveto(*coordinates[0])
            for point in coordinates[1:]:path.lineto(*point)
            path.close()
        self.context.path(path,None,aggdraw.Brush(fill))
    def flush(self):self.context.flush()

@functools.lru_cache(maxsize=512)
def native_sprite(text,size,color):
    ink=sprite(text,size,color)
    return ink.resize((math.ceil(ink.width/SCALE),math.ceil(ink.height/SCALE)),Image.Resampling.LANCZOS)

def render_metatile(features,z,mx,my):
    factor=2**(z-REF_ZOOM)
    ox,oy=mx*256-PAD,my*256-PAD
    extent=META*256+2*PAD
    image=Image.new('RGB',(extent,extent),BG);draw=AntialiasCanvas(image)
    clip=(ox/factor,oy/factor,(ox+META*256+2*PAD)/factor,(oy+META*256+2*PAD)/factor)
    roads=[];labels=[];waters=[]
    def local(sequence):return [(x*factor-ox,y*factor-oy) for x,y in sequence]
    for kind,category,name,payload in features:
        geometry=shapely.from_wkb(payload)
        if kind=='place':
            if z<10 and category not in ('city','town'):continue
            if z<12 and category not in ('city','town','village'):continue
            size=16 if category in ('city','town') else (14 if category=='village' else 12)
            labels.append(({'city':0,'town':1,'village':3,'hamlet':4}.get(category,5),name,geometry.x*factor,geometry.y*factor,size,'#375950'))
            continue
        if kind=='road':
            width,color,rank,minzoom=HIGHWAYS[category]
            if z<minzoom:continue
            factor_width=.35 if z<10 else (.5 if z<12 else (.65 if z<14 else (1.25 if z>=16 else 1.)))
            width=max(.8,width*factor_width)
            geometry=geometry.simplify(.35/factor,preserve_topology=False)
            if name and z>=12 and category not in ('track','path','footway','cycleway','service','steps','bridleway') and geometry.length*factor>160:
                point=geometry.interpolate(.5,normalized=True)
                labels.append((2,name,point.x*factor,point.y*factor,11,'#605c4b'))
            clipped=shapely.clip_by_rect(geometry,*clip)
            roads.append((rank,category,width,color,clipped))
        elif kind=='water':
            if geometry.area*factor*factor<4:continue
            clipped=shapely.clip_by_rect(geometry.simplify(.3/factor,preserve_topology=True),*clip)
            waters.append(clipped)
        elif kind=='waterway':
            if z<13 and category!='river':continue
            clipped=shapely.clip_by_rect(geometry.simplify(.4/factor,preserve_topology=False),*clip)
            for part in parts(clipped):
                if part.geom_type=='LineString' and not part.is_empty:
                    draw.line(local(part.coords),fill='#a5ccd7',width=(2.3 if category=='river' else 1.),joint='curve')
    for geometry in waters:
        for polygon in parts(geometry):
            if polygon.geom_type!='Polygon' or polygon.is_empty:continue
            polygon=shapely.orient_polygons(polygon,exterior_cw=False)
            rings=[local(polygon.exterior.coords)]+[local(hole.coords) for hole in polygon.interiors]
            draw.polygon(rings,WATER)
    for rank,category,width,color,geometry in sorted(roads,key=lambda p:p[0]):
        for part in parts(geometry):
            if part.geom_type!='LineString' or part.is_empty:continue
            coordinates=local(part.coords)
            if category not in ('track','path','footway','cycleway','steps','bridleway'):
                draw.line(coordinates,fill='#c6c4b7',width=width+1.4,joint='curve')
            draw.line(coordinates,fill=color,width=width,joint='curve')
    draw.flush()
    occupied=collections.defaultdict(list)
    seen=set()
    for priority,text,px,py,size,color in sorted(labels,key=lambda item:(item[0],item[1],item[2],item[3])):
        text=text.replace('\n',' ').strip()
        if not text or len(text)>60 or (text,round(px/100),round(py/100)) in seen:continue
        seen.add((text,round(px/100),round(py/100)))
        ink=sprite(text,size,color);w,h=ink.width/SCALE,ink.height/SCALE
        bbox=(px-w/2-5,py-h/2-4,px+w/2+5,py+h/2+4)
        cells=[(x,y) for x in range(math.floor(bbox[0]/64),math.floor(bbox[2]/64)+1) for y in range(math.floor(bbox[1]/64),math.floor(bbox[3]/64)+1)]
        if any(bbox[0]<b[2] and bbox[2]>b[0] and bbox[1]<b[3] and bbox[3]>b[1] for cell in cells for b in occupied[cell]):continue
        for cell in cells:occupied[cell].append(bbox)
        native=native_sprite(text,size,color)
        image.paste(native,(round(px-ox-native.width/2),round(py-oy-native.height/2)),native)
    image=image.crop((PAD,PAD,PAD+META*256,PAD+META*256))
    return image

_WORKER_SOURCE=None

def render_batch(job):
    global _WORKER_SOURCE
    z,mx,my,x0,y0,x1,y1,source_path=job
    if _WORKER_SOURCE is None:
        _WORKER_SOURCE=sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        _WORKER_SOURCE.execute('PRAGMA cache_size=-8192')
    factor=2**(z-REF_ZOOM);uniform=0
    left,top=(mx*256-PAD)/factor,(my*256-PAD)/factor
    right,bottom=((mx+META)*256+PAD)/factor,((my+META)*256+PAD)/factor
    rows=_WORKER_SOURCE.execute('SELECT f.kind,f.category,f.name,f.pixels FROM spatial s JOIN features f ON f.id=s.id WHERE s.minx<=? AND s.maxx>=? AND s.miny<=? AND s.maxy>=?',(right,left,bottom,top)).fetchall()
    image=render_metatile(rows,z,mx,my) if rows else None
    batch=[]
    for ty in range(max(y0,my),min(y1,my+META-1)+1):
        for tx in range(max(x0,mx),min(x1,mx+META-1)+1):
            if image is None:blob=uniform_png(BG);uniform+=1
            else:
                tile=image.crop(((tx-mx)*256,(ty-my)*256,(tx-mx+1)*256,(ty-my+1)*256))
                extremes=tile.getextrema()
                if all(a==b for a,b in extremes):blob=uniform_png(tuple(a for a,b in extremes));uniform+=1
                else:
                    encoded=io.BytesIO();tile.save(encoded,format='PNG',compress_level=3);blob=encoded.getvalue()
            batch.append((z,tx,2**z-1-ty,blob))
    safe_memory(Path(source_path).parent)
    return z,mx,my,batch,uniform

def bounded_render(jobs,workers):
    """At most 2 jobs/worker queued; PNG buffers never grow with region size."""
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        pending=collections.deque();iterator=iter(jobs)
        for _ in range(workers*2):
            job=next(iterator,None)
            if job is None:break
            pending.append(pool.submit(render_batch,job))
        while pending:
            result=pending.popleft().result()
            job=next(iterator,None)
            if job is not None:pending.append(pool.submit(render_batch,job))
            yield result

def render(directory,output,minzoom,maxzoom,bounds=None,workers=2):
    output=Path(output)
    if output.suffix.lower()!='.mbtiles':
        raise ValueError('Output must be a new or resumable .mbtiles file')
    if output.exists():
        with contextlib.closing(sqlite3.connect(output.resolve().as_uri()+'?mode=ro',uri=True)) as existing_db:
            previous=dict(existing_db.execute('SELECT name,value FROM metadata'))
        if 'build_settings' not in previous:
            raise FileExistsError('Completed or unrelated output already exists; choose a new file')
    manifest=json.loads((directory/'source-manifest.json').read_text(encoding='utf-8'))
    s,w,n,e=manifest['bounds'];coverage=bounds or [w,s,e,n]
    if not (w<=coverage[0]<coverage[2]<=e and s<=coverage[1]<coverage[3]<=n):raise ValueError('Requested bounds exceed extracted coverage')
    started=time.monotonic();output.parent.mkdir(parents=True,exist_ok=True)
    source=sqlite3.connect((directory/'features.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    source.execute('PRAGMA cache_size=-16384')
    db=sqlite3.connect(output);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL');db.execute('PRAGMA cache_size=-16384')
    db.executescript('''CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tiles(zoom_level INTEGER NOT NULL,tile_column INTEGER NOT NULL,tile_row INTEGER NOT NULL,tile_data BLOB NOT NULL,PRIMARY KEY(zoom_level,tile_column,tile_row)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS build_progress(z INTEGER,mx INTEGER,my INTEGER,PRIMARY KEY(z,mx,my)) WITHOUT ROWID;''')
    settings={'bounds':coverage,'minzoom':minzoom,'maxzoom':maxzoom,'source_sha256':manifest['source_sha256'],'renderer':'detailed-3-agg-bounded-pool','renderer_sha256':digest(Path(__file__))}
    existing=dict(db.execute('SELECT name,value FROM metadata'))
    if existing.get('build_settings') and json.loads(existing['build_settings'])!=settings:raise ValueError('Refusing to resume with different build settings')
    metadata={'name':'AI Plant · ถนนละเอียด ภาคตะวันออกเฉียงเหนือ','type':'baselayer','version':'1.3','format':'png','scheme':'tms',
      'minzoom':str(minzoom),'maxzoom':str(maxzoom),'bounds':','.join(str(v) for v in coverage),
      'center':f'{(coverage[0]+coverage[2])/2},{(coverage[1]+coverage[3])/2},{min(14,maxzoom)}',
      'attribution':'© OpenStreetMap contributors','attribution_url':'https://www.openstreetmap.org/copyright',
      'license':'Underlying geographic data OpenStreetMap ODbL 1.0; bundled source.geojsonseq.gz',
      'description':manifest['detail'],'tile_size':'256','source_timestamp':manifest['osm_timestamp'],
      'source':'https://download.geofabrik.de/asia/thailand.html','source_sha256':manifest['source_sha256'],
      'build_settings':json.dumps(settings)}
    db.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)',metadata.items());db.commit()
    total=db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0];done=0;uniform=0
    for z in range(minzoom,maxzoom+1):
        x0,y0,x1,y1=tile_range(coverage,z);factor=2**(z-REF_ZOOM)
        completed=set(db.execute('SELECT mx,my FROM build_progress WHERE z=?',(z,)))
        emit(stage='zoom-start',zoom=z,expected=expected_tiles(coverage,z),resumed_metatiles=len(completed))
        def jobs():
            for my in range(y0//META*META,y1+1,META):
                for mx in range(x0//META*META,x1+1,META):
                    if (mx,my) not in completed:
                        yield z,mx,my,x0,y0,x1,y1,str(directory/'features.sqlite3')
        for _,mx,my,batch,uniform_count in bounded_render(jobs(),workers):
            uniform+=uniform_count
            with db:
                db.executemany('INSERT INTO tiles VALUES(?,?,?,?)',batch)
                db.execute('INSERT INTO build_progress VALUES(?,?,?)',(z,mx,my))
            total+=len(batch);done+=1
            if done%40==0:
                emit(stage='render',zoom=z,tiles=total,seconds=round(time.monotonic()-started,1),uniform_tiles=uniform,bytes=output.stat().st_size,memory_mib=safe_memory(directory))
            if done%200==0:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if output.stat().st_size>15*1024**3:raise RuntimeError('Detailed atlas exceeds 15 GiB build budget; resume after revising storage plan')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        actual=db.execute('SELECT COUNT(*) FROM tiles WHERE zoom_level=?',(z,)).fetchone()[0]
        if actual!=expected_tiles(coverage,z):raise ValueError(f'Incomplete zoom {z}: {actual}')
        emit(stage='zoom-complete',zoom=z,tiles=actual,total=total,seconds=round(time.monotonic()-started,1),bytes=output.stat().st_size)
    db.execute('DROP TABLE build_progress');db.execute("DELETE FROM metadata WHERE name='build_settings'");db.commit()
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('PRAGMA journal_mode=DELETE');db.close();source.close()
    result={'file':str(output),'bytes':output.stat().st_size,'tiles':total,'seconds':round(time.monotonic()-started,2),
      'sha256':digest(output),'metadata':{k:v for k,v in metadata.items() if k!='build_settings'},
      'tiles_by_zoom':{str(z):expected_tiles(coverage,z) for z in range(minzoom,maxzoom+1)},
      'memory_mib':memory_mib(),'memory_scope':'main process only; worker processes excluded','workers':workers,'worker_memory_limit_mib':1800,'method':'disk-backed feature RTree, 8x8 metatiles, native AGG antialiased paths and 2x shaped Thai labels, PNG lossless, resumable transactions, CPU affinity 2 below normal'}
    output.with_suffix('.render.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    emit(stage='render-complete',**result)

def export_source(directory):
    db=sqlite3.connect((directory/'features.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    target=directory/'source.geojsonseq.gz'
    temporary=target.with_suffix('.tmp')
    count=0
    with temporary.open('wb') as raw, gzip.GzipFile(filename='',fileobj=raw,mode='wb',compresslevel=6,mtime=0) as output:
        for osm_type,osm_id,kind,category,name,tags,payload in db.execute('SELECT osm_type,osm_id,kind,category,name,tags,wgs84 FROM features ORDER BY id'):
            feature={'type':'Feature','id':f'{osm_type}/{osm_id}','properties':{'kind':kind,'category':category,'label':name,'osm_tags':json.loads(tags)},'geometry':json.loads(shapely.to_geojson(shapely.from_wkb(payload)))}
            output.write(b'\x1e'+json.dumps(feature,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
    temporary.replace(target);db.close();emit(stage='source-export',features=count,bytes=target.stat().st_size,sha256=digest(target))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['extract','render','export-source'])
    parser.add_argument('--directory',type=Path,default=BUILD)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--minzoom',type=int,default=7);parser.add_argument('--maxzoom',type=int,default=16)
    parser.add_argument('--workers',type=int,choices=(1,2),default=2)
    parser.add_argument('--bounds',nargs=4,type=float,metavar=('WEST','SOUTH','EAST','NORTH'))
    args=parser.parse_args();args.directory.mkdir(parents=True,exist_ok=True)
    if args.action=='extract':extract(args.directory)
    elif args.action=='export-source':export_source(args.directory)
    else:
        if not 0<=args.minzoom<=args.maxzoom<=16:raise ValueError('Require 0<=minzoom<=maxzoom<=16')
        render(args.directory,args.output or args.directory/'current.mbtiles',args.minzoom,args.maxzoom,args.bounds,args.workers)

if __name__=='__main__':main()
