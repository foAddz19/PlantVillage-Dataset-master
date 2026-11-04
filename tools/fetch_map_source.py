"""PC-only download of raw Thailand OSM data, never pre-rendered online tiles."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request

PROJECT=Path(__file__).resolve().parents[1]
URL='https://download.geofabrik.de/asia/thailand-latest.osm.pbf'
HEADERS={'User-Agent':'AIPlantOfflineMapBuilder/1.0 (local OpenStreetMap data rendering)'}
LIMIT=1024**3

def sha(path,algorithm):
    with path.open('rb') as source:return hashlib.file_digest(source,algorithm).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=PROJECT/'runtime/isan-detail-z16')
    args=parser.parse_args();directory=args.directory.resolve();directory.mkdir(parents=True,exist_ok=True)
    def request(url,extra=None):
        return urllib.request.urlopen(urllib.request.Request(url,headers={**HEADERS,**(extra or {})}),timeout=90)
    with request(URL+'.md5') as response:expected=response.read(1000).decode().split()[0].lower()
    if len(expected)!=32 or any(c not in '0123456789abcdef' for c in expected):raise ValueError('Invalid remote checksum')
    target=directory/'thailand.osm.pbf';partial=directory/'thailand.download'
    if target.exists():
        if sha(target,'md5')!=expected:raise ValueError('Existing source is another snapshot; preserve it and choose a new directory')
        print('Verified existing Geofabrik source');return
    marker=directory/'download-checksum.txt'
    if partial.exists() and marker.exists() and marker.read_text().strip()!=expected:
        raise ValueError('Partial download belongs to another snapshot; choose a new directory')
    marker.write_text(expected,encoding='ascii');started=time.monotonic();last_error=None
    for attempt in range(4):
        count=partial.stat().st_size if partial.exists() else 0
        try:
            with request(URL,{'Range':f'bytes={count}-'} if count else None) as response:
                mode='ab' if count and response.status==206 else 'wb'
                if mode=='wb':count=0
                if response.status==206 and not response.headers.get('Content-Range','').startswith(f'bytes {count}-'):
                    raise ValueError('Unexpected download range')
                expected_bytes=count+int(response.headers.get('Content-Length','0'))
                if expected_bytes>LIMIT:raise ValueError('Source exceeds 1 GiB preparation guard')
                last=count
                with partial.open(mode) as output:
                    while block:=response.read(1024*1024):
                        output.write(block);count+=len(block)
                        if count>LIMIT:raise ValueError('Source exceeds 1 GiB preparation guard')
                        if count-last>=16*1024**2:
                            print(json.dumps({'stage':'download','bytes':count,'total':expected_bytes,'seconds':round(time.monotonic()-started,1)}),flush=True);last=count
                if expected_bytes and count!=expected_bytes:raise OSError('Incomplete download')
                modified=response.headers.get('Last-Modified')
            break
        except (OSError,TimeoutError) as error:
            last_error=error
            if attempt==3:raise
            print(json.dumps({'stage':'retry','attempt':attempt+1,'error':str(error)}),flush=True)
            time.sleep(5*(attempt+1))
    if sha(partial,'md5')!=expected:raise ValueError('Source checksum mismatch; existing map unchanged')
    partial.replace(target)
    manifest={'url':URL,'bytes':target.stat().st_size,'md5':expected,'sha256':sha(target,'sha256'),
      'last_modified':modified,'downloaded_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    (directory/'download-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'stage':'download-complete',**manifest}),flush=True)

if __name__=='__main__':main()
