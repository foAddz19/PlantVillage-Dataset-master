"""Render a bounded OSM raw-data extract into a small offline raster MBTiles.

No online tiles are fetched. Geometry and labels come only from the OSM extract.
Source geographic data remains under ODbL with attribution and bundled sources.
"""
import argparse
import collections
import hashlib
import io
import json
import math
import os
import pathlib
import sqlite3
import sys
import time

def prepare_cpu_budget():
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[key]='2'
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=ctypes.c_void_p
        handle=kernel.GetCurrentProcess()
        kernel.GetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
        kernel.SetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
        kernel.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        process_mask,system_mask=ctypes.c_size_t(),ctypes.c_size_t()
        if not kernel.GetProcessAffinityMask(handle,ctypes.byref(process_mask),ctypes.byref(system_mask)):
            raise ctypes.WinError(ctypes.get_last_error())
        cpus=[bit for bit in range(ctypes.sizeof(ctypes.c_size_t)*8) if process_mask.value&(1<<bit)]
        mask=sum(1<<bit for bit in cpus[:2])
        if not kernel.SetProcessAffinityMask(handle,mask):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetPriorityClass(handle,0x00004000):
            raise ctypes.WinError(ctypes.get_last_error())
    elif hasattr(os,'sched_getaffinity'):
        os.sched_setaffinity(0,set(sorted(os.sched_getaffinity(0))[:2]))
        os.nice(5)

prepare_cpu_budget()

from PIL import Image, ImageDraw, ImageFilter, ImageOps

PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from leaflab.receipt import ThaiFont
from benchmark_paddy import memory_mib

DIRECTORY = PROJECT / 'runtime' / 'offline-map-build'
FONT = PROJECT / 'assets/fonts/NotoSansThaiLooped.ttf'
TILE_SIZE = 256
SCALE = 2
PALETTE = {
    'background': '#f1f2e9', 'farmland': '#e8eddd', 'orchard': '#dbe6c9',
    'forest': '#d6e2c6', 'wood': '#d6e2c6', 'scrub': '#e1e8d3',
    'residential': '#e9e5df', 'commercial': '#eadfda', 'industrial': '#e5dfe5',
    'water': '#b4d7df', 'reservoir': '#b4d7df', 'wetland': '#d6e3da',
    'grass': '#e1e9d0', 'meadow': '#e3eacf', 'park': '#d9e7cd',
    'pitch': '#c7dfbf', 'garden': '#d9e7cd', 'cemetery': '#d5ded1',
    'building': '#d6cdc1',
}
HIGHWAYS = {
    'motorway': (7.0, '#e6bc7d'), 'motorway_link': (5.0, '#e6bc7d'),
    'trunk': (6.5, '#eac48a'), 'trunk_link': (4.5, '#eac48a'),
    'primary': (6.0, '#f0d39c'), 'primary_link': (4.0, '#f0d39c'),
    'secondary': (5.0, '#f7e4b5'), 'secondary_link': (4.0, '#f7e4b5'),
    'tertiary': (4.0, '#fffbed'), 'tertiary_link': (3.0, '#fffbed'),
    'unclassified': (3.0, '#ffffff'), 'residential': (3.0, '#ffffff'),
    'living_street': (2.5, '#ffffff'), 'service': (2.0, '#ffffff'),
    'track': (1.5, '#d0bd9b'), 'path': (1.0, '#c2b49a'),
    'footway': (1.0, '#b9b8a7'), 'cycleway': (1.0, '#b9b8a7'),
}

def xy(lon, lat, z):
    lat = max(-85.05112878, min(85.05112878, lat))
    n = TILE_SIZE * (2 ** z)
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n

def lonlat(x, y, z):
    n = TILE_SIZE * (2 ** z)
    return x / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1-2*y/n))))

def display_name(tags):
    name=tags.get('name:th') or tags.get('name')
    if name and any('\u0e80'<=letter<='\u0eff' or '\u1780'<=letter<='\u17ff' for letter in name):
        return tags.get('name:en') or tags.get('name:latin')
    return name

def simplify(points, tolerance):
    if len(points) < 3:
        return points
    ax, ay = points[0]
    bx, by = points[-1]
    dx, dy = bx-ax, by-ay
    den = dx*dx + dy*dy
    farthest, farthest_distance = 0, 0.0
    for index, (px,py) in enumerate(points[1:-1],1):
        t = max(0.0,min(1.0,((px-ax)*dx+(py-ay)*dy)/den)) if den else 0
        distance = (px-ax-t*dx)**2+(py-ay-t*dy)**2
        if distance > farthest_distance:
            farthest, farthest_distance = index, distance
    if farthest_distance <= tolerance*tolerance:
        return [points[0],points[-1]]
    return simplify(points[:farthest+1],tolerance)[:-1]+simplify(points[farthest:],tolerance)

def water_rings(relation, role):
    segments = []
    for member in relation.get('members', []):
        if member.get('type')!='way' or member.get('role','outer')!=role:
            continue
        points=[(point['lon'],point['lat']) for point in member.get('geometry',[]) if 'lon' in point and 'lat' in point]
        if len(points)>=2:
            segments.append(points)
    rings=[]
    while segments:
        ring=segments.pop()
        while ring[0]!=ring[-1]:
            found=False
            for index,segment in enumerate(segments):
                if ring[-1]==segment[0]:
                    ring.extend(segment[1:])
                elif ring[-1]==segment[-1]:
                    ring.extend(reversed(segment[:-1]))
                elif ring[0]==segment[-1]:
                    ring=segment[:-1]+ring
                elif ring[0]==segment[0]:
                    ring=list(reversed(segment[1:]))+ring
                else:
                    continue
                segments.pop(index)
                found=True
                break
            if not found:
                raise ValueError(f'Unclosed water relation {relation["id"]}')
        if len(ring)>=4:
            rings.append(ring)
    return rings

def main():
    global DIRECTORY
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=pathlib.Path,default=DIRECTORY)
    parser.add_argument('--minzoom',type=int,default=12)
    parser.add_argument('--maxzoom',type=int,default=17)
    parser.add_argument('--name',default='AI Plant · แผนที่เริ่มต้น ร้อยเอ็ด')
    parser.add_argument('--default-zoom',type=int,default=16)
    args=parser.parse_args()
    if not 0<=args.minzoom<=args.default_zoom<=args.maxzoom<=18:
        raise ValueError('Require 0<=minzoom<=defaultzoom<=maxzoom<=18')
    DIRECTORY=args.directory.resolve()
    started = time.monotonic()
    source_path = DIRECTORY / 'starter.osm.json'
    water_path = DIRECTORY / 'water-relations.osm.json'
    if source_path.stat().st_size+water_path.stat().st_size>128*1024*1024:
        raise ValueError('Combined OSM raw extracts exceed bounded 128 MiB preparation limit')
    source = json.loads(source_path.read_text(encoding='utf-8'))
    water = json.loads(water_path.read_text(encoding='utf-8'))
    manifest = json.loads((DIRECTORY/'source-manifest.json').read_text(encoding='utf-8'))
    south,west,north,east = manifest['bounds']
    output = DIRECTORY / 'current.mbtiles'
    if output.exists():
        raise RuntimeError('Refusing to overwrite existing atlas')
    db = sqlite3.connect(output)
    db.execute('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE tiles (zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL, tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL, PRIMARY KEY(zoom_level,tile_column,tile_row)) WITHOUT ROWID')
    metadata = {
        'name': args.name, 'type': 'baselayer',
        'version': '1.3', 'description': manifest.get('detail') or 'Bounded offline starter map rendered locally from OpenStreetMap data. Coverage follows the example device coordinates; not satellite imagery.',
        'format': 'png', 'scheme': 'tms', 'minzoom': str(args.minzoom), 'maxzoom': str(args.maxzoom),
        'bounds': f'{west},{south},{east},{north}', 'center': f'{manifest["center"][0]},{manifest["center"][1]},{args.default_zoom}',
        'attribution': '© OpenStreetMap contributors',
        'attribution_url': 'https://www.openstreetmap.org/copyright',
        'license': 'Underlying geographic data: OpenStreetMap ODbL 1.0; see bundled sources and attribution',
        'source': 'https://overpass-api.de/api/interpreter',
        'source_timestamp': manifest.get('osm_timestamp') or '', 'tile_size': '256',
        'source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
        'water_source_sha256': hashlib.sha256(water_path.read_bytes()).hexdigest(),
    }
    db.executemany('INSERT INTO metadata VALUES(?,?)',metadata.items())
    feature_counts = collections.Counter()
    total = 0
    for z in range(args.minzoom,args.maxzoom+1):
        minpx,minpy=xy(west,north,z)
        maxpx,maxpy=xy(east,south,z)
        minx,maxx=math.floor(minpx/256),math.floor(maxpx/256)
        miny,maxy=math.floor(minpy/256),math.floor(maxpy/256)
        buckets = collections.defaultdict(list)
        labels = []
        occupied = []
        fonts = {}
        def font(size):
            if size not in fonts:
                fonts[size] = ThaiFont(size*SCALE, font_path=FONT, weight=500)
            return fonts[size]
        def addlabel(text,point,size,color,priority):
            if not text:
                return
            text=text.replace('\n',' ').strip()
            if len(text)>52:
                return
            width=font(size).width(text)/SCALE
            height=font(size).height/SCALE
            labels.append((priority,text,point,size,color,width,height))
        sprites = {}
        def text_sprite(text,size,color):
            key=(text,size,color)
            if key not in sprites:
                shaped=font(size)
                width=math.ceil(shaped.width(text))+8*SCALE
                coverage=Image.new('L',(width,shaped.height),255)
                shaped.draw(coverage,text,4*SCALE,0)
                mask=ImageOps.invert(coverage)
                box=mask.getbbox()
                if box:
                    mask=mask.crop((max(0,box[0]-3*SCALE),max(0,box[1]-3*SCALE),min(width,box[2]+3*SCALE),min(shaped.height,box[3]+3*SCALE)))
                halo=Image.new('RGBA',mask.size,PALETTE['background'])
                halo.putalpha(mask.filter(ImageFilter.MaxFilter(7)))
                ink=Image.new('RGBA',mask.size,color)
                ink.putalpha(mask)
                halo.alpha_composite(ink)
                sprites[key]=halo
            return sprites[key]
        for element in source['elements']:
            tags=element.get('tags',{})
            if element['type']=='node':
                if 'place' in tags:
                    size=17 if tags['place'] in ('city','town') else 14
                    place=tags['place']
                    if z<11 and place not in ('city','town'):
                        continue
                    priority={'city':0,'town':1,'village':5,'hamlet':6}.get(place,7)
                    addlabel(display_name(tags),xy(element['lon'],element['lat'],z),size,'#375950',priority)
                elif z>=16 and 'amenity' in tags:
                    addlabel(display_name(tags),xy(element['lon'],element['lat'],z),11,'#5b6760',8)
                continue
            geometry=element.get('geometry') or []
            points=[xy(p['lon'],p['lat'],z) for p in geometry if 'lon' in p and 'lat' in p]
            if len(points)<2:
                continue
            highway=tags.get('highway')
            closed = points[0]==points[-1]
            if highway:
                if highway not in HIGHWAYS or (z<14 and highway in ('track','path','footway','cycleway','service')):
                    continue
                if z<=8 and highway not in ('motorway','motorway_link','trunk','trunk_link','primary','primary_link'):
                    continue
                if z<=10 and highway in ('tertiary','tertiary_link','unclassified','residential','living_street'):
                    continue
                width,color=HIGHWAYS[highway]
                factor=0.35 if z<10 else (0.5 if z<12 else (0.65 if z<14 else (1.35 if z>=16 else 1)))
                width=max(0.8,width*factor)
                layer=3
                kind='road'
                name=tags.get('ref') or display_name(tags)
                if z>=11 and name and (z>=13 or tags.get('ref')) and highway not in ('track','path','footway','cycleway','service'):
                    if len(points)>2:
                        mid=points[len(points)//2]
                    else:
                        mid=((points[0][0]+points[-1][0])/2,(points[0][1]+points[-1][1])/2)
                    if math.dist(points[0],points[-1])>80:
                        addlabel(name,mid,11,'#6d6558',2 if tags.get('ref') else 3)
            elif tags.get('waterway'):
                kind,layer,width,color='waterway',2,(2.2 if tags['waterway']=='river' else 1.0),'#a5ccd7'
            else:
                if tags.get('building') and z<16:
                    continue
                kind='polygon'
                layer=1 if tags.get('building') else 0
                width=0
                value = 'building' if tags.get('building') else (tags.get('natural') or tags.get('landuse') or tags.get('leisure'))
                if value in ('water','reservoir'):
                    layer=0.5
                color=PALETTE.get(value)
                if not color or not closed or len(points)<4:
                    continue
            points=simplify(points,0.7)
            if kind=='polygon' and len(points)<4:
                continue
            xmin=min(p[0] for p in points); xmax=max(p[0] for p in points)
            ymin=min(p[1] for p in points); ymax=max(p[1] for p in points)
            feature=(layer,kind,points,width,color)
            for tx in range(max(minx,math.floor(xmin/256)),min(maxx,math.floor(xmax/256))+1):
                for ty in range(max(miny,math.floor(ymin/256)),min(maxy,math.floor(ymax/256))+1):
                    buckets[tx,ty].append(feature)
            feature_counts[z,kind]+=1
        for relation in water.get('elements',[]):
            outers=[simplify([xy(lon,lat,z) for lon,lat in ring],0.7) for ring in water_rings(relation,'outer')]
            inners=[simplify([xy(lon,lat,z) for lon,lat in ring],0.7) for ring in water_rings(relation,'inner')]
            outers=[ring for ring in outers if len(ring)>=4]
            inners=[ring for ring in inners if len(ring)>=4]
            if not outers:
                continue
            flattened=[point for ring in outers for point in ring]
            xmin=min(p[0] for p in flattened); xmax=max(p[0] for p in flattened)
            ymin=min(p[1] for p in flattened); ymax=max(p[1] for p in flattened)
            feature=(0.5,'waterpolygon',(outers,inners),0,PALETTE['water'])
            for tx in range(max(minx,math.floor(xmin/256)),min(maxx,math.floor(xmax/256))+1):
                for ty in range(max(miny,math.floor(ymin/256)),min(maxy,math.floor(ymax/256))+1):
                    buckets[tx,ty].append(feature)
            if z>=14 and xmax-xmin>100:
                tags=relation.get('tags',{})
                addlabel(display_name(tags),((xmin+xmax)/2,(ymin+ymax)/2),12,'#3c7180',4)
            feature_counts[z,'waterpolygon']+=1
        occupied_grid=collections.defaultdict(list)
        for priority,text,(px,py),size,color,width,height in sorted(labels,key=lambda p:p[0]):
            bbox=(px-width/2-4,py-height/2-4,px+width/2+4,py+height/2+4)
            cells=[(gx,gy) for gx in range(math.floor(bbox[0]/64),math.floor(bbox[2]/64)+1) for gy in range(math.floor(bbox[1]/64),math.floor(bbox[3]/64)+1)]
            if any(bbox[0]<b[2] and bbox[2]>b[0] and bbox[1]<b[3] and bbox[3]>b[1] for cell in cells for b in occupied_grid[cell]):
                continue
            occupied.append(bbox)
            for cell in cells:
                occupied_grid[cell].append(bbox)
            feature=(6,'label',(text,px,py,size),0,color)
            for tx in range(max(minx,math.floor(bbox[0]/256)),min(maxx,math.floor(bbox[2]/256))+1):
                for ty in range(max(miny,math.floor(bbox[1]/256)),min(maxy,math.floor(bbox[3]/256))+1):
                    buckets[tx,ty].append(feature)
        for tx in range(minx,maxx+1):
            for ty in range(miny,maxy+1):
                image=Image.new('RGB',(256*SCALE,256*SCALE),PALETTE['background'])
                draw=ImageDraw.Draw(image)
                for layer,kind,points,width,color in sorted(buckets[tx,ty],key=lambda f:f[0]):
                    if kind=='label':
                        text,px,py,size=points
                        location=((px-tx*256)*SCALE,(py-ty*256)*SCALE)
                        sprite=text_sprite(text,size,color)
                        image.paste(sprite,(round(location[0]-sprite.width/2),round(location[1]-sprite.height/2)),sprite)
                        continue
                    if kind=='waterpolygon':
                        outers,inners=points
                        mask=Image.new('L',image.size,0)
                        stencil=ImageDraw.Draw(mask)
                        for value,rings in ((255,outers),(0,inners)):
                            for ring in rings:
                                stencil.polygon([((x-tx*256)*SCALE,(y-ty*256)*SCALE) for x,y in ring],fill=value)
                        image.paste(color,(0,0,image.width,image.height),mask)
                        continue
                    local=[((x-tx*256)*SCALE,(y-ty*256)*SCALE) for x,y in points]
                    if kind=='polygon':
                        draw.polygon(local,fill=color)
                        if layer==1:
                            draw.line(local,fill='#c7bdaf',width=SCALE)
                    elif kind=='road':
                        draw.line(local,fill='#c9c7ba',width=max(2,round((width+1.5)*SCALE)),joint='curve')
                        draw.line(local,fill=color,width=max(1,round(width*SCALE)),joint='curve')
                    else:
                        draw.line(local,fill=color,width=max(1,round(width*SCALE)),joint='curve')
                image=image.resize((256,256),Image.Resampling.LANCZOS)
                blob=io.BytesIO()
                image.save(blob,format='PNG',optimize=True)
                db.execute('INSERT INTO tiles VALUES(?,?,?,?)',(z,tx,(2**z)-1-ty,blob.getvalue()))
                total+=1
                if total%200==0:
                    memory=memory_mib()
                    if memory and memory['current']>1800:
                        raise RuntimeError('Preparation process exceeds 1800 MiB memory budget')
                    print(json.dumps({'stage':'render','zoom':z,'tiles':total,'seconds':round(time.monotonic()-started,1),'memory_mib':memory}),flush=True)
        db.commit()
        if output.stat().st_size>220*1024*1024:
            raise ValueError('Prepared atlas exceeds 220 MiB soft storage budget')
        print(json.dumps({'zoom':z,'tiles_total':total,'seconds':round(time.monotonic()-started,1)}),flush=True)
    db.execute('PRAGMA optimize')
    db.close()
    result={'file':str(output),'bytes':output.stat().st_size,'tiles':total,'seconds':round(time.monotonic()-started,2),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'metadata':metadata,'features':{str(key):value for key,value in feature_counts.items()}}
    (DIRECTORY/'render-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))

if __name__=='__main__':
    main()
