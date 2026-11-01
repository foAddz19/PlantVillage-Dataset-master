"""Read real Paddy Doctor images without moving or rewriting their source files."""
from __future__ import annotations
import csv
import hashlib
import random
from collections import Counter, defaultdict
from pathlib import Path
from .dataset import ROOT, label_info
from .paddy_labels import LABELS, ALL_LABELS, PROFILES

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
SPLITS = {'train':'train', 'train_images':'train', 'test':'test', 'test_images':'test',
          'val':'validation', 'valid':'validation', 'validation':'validation'}

def _inside(root, path):
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('เส้นทางภาพออกนอกโฟลเดอร์ชุดข้อมูล')
    return path

def read_records(directory):
    root = Path(directory).resolve()
    rows = {}
    if not root.is_dir():
        return []
    def add(path, label, split=None, group=None):
        label = label.removeprefix('Rice___')
        if label not in ALL_LABELS:
            raise ValueError('ไม่รู้จักคลาสข้าว: '+label)
        path = _inside(root, path)
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise ValueError('ไม่พบไฟล์ภาพที่ระบุ: '+str(path))
        relative = path.relative_to(root).as_posix()
        previous = rows.get(relative)
        if previous and previous['label'] != label:
            raise ValueError('ภาพเดียวมีป้ายกำกับขัดกัน: '+relative)
        rows[relative] = dict(path=relative, label=label, split=split, group=group)
    containers = [(root,None)] + [(root/n,s) for n,s in SPLITS.items() if (root/n).is_dir()]
    for container, split in containers:
        for folder in sorted(container.iterdir()):
            label = folder.name.removeprefix('Rice___')
            if folder.is_dir() and label in ALL_LABELS:
                for path in sorted(folder.iterdir()):
                    if path.suffix.lower() in EXTENSIONS:add(path,label,split)
    csv_paths = [root/n for n in ('train.csv','test.csv','validation.csv','metadata.csv') if (root/n).is_file()]
    by_name=defaultdict(list)
    for record in rows.values():by_name[Path(record['path']).name].append(record)
    for csv_path in csv_paths:
        with csv_path.open(encoding='utf-8-sig',newline='') as source:
            for row in csv.DictReader(source):
                label = (row.get('label') or '').strip()
                if not label:continue  # Unlabelled competition test rows are not training data.
                name = next((row[k].strip() for k in ('path','filepath','filename','file_name','image_id') if row.get(k)),None)
                if not name:raise ValueError('CSV ต้องมีคอลัมน์ path/filename/image_id และ label')
                raw_split = (row.get('split') or csv_path.stem).lower()
                if row.get('split') and raw_split not in SPLITS:
                    raise ValueError('ชื่อ split ใน CSV ไม่ถูกต้อง: '+raw_split)
                split = SPLITS.get(raw_split)
                candidates = [root/name,root/'images'/name,root/label/name]
                for sub in SPLITS:
                    candidates.extend([root/sub/name,root/sub/label/name])
                known=[r for r in by_name[Path(name).name] if r['label']==label.removeprefix('Rice___')]
                found = root/known[0]['path'] if len(known)==1 else next((p for p in candidates if p.is_file()),None)
                if found is None:raise ValueError('ไม่พบภาพจาก CSV: '+name)
                group = next((row[k].strip() for k in ('leaf_id','plant_id','group') if row.get(k)),None)
                add(found,label,split,group)
    return sorted(rows.values(),key=lambda r:r['path'])

class PaddyCatalog:
    def __init__(self,directory=None):
        self.directory=Path(directory or ROOT/'datasets'/'paddy').resolve()
        self.records=read_records(self.directory)
        self.files={}
        for row in self.records:self.files.setdefault('Rice___'+row['label'],[]).append(row['path'])
        self.files=dict(sorted(self.files.items()))
    def summary(self):
        classes=[{**label_info(label),'count':len(paths)} for label,paths in self.files.items()]
        return dict(images=len(self.records),class_count=len(classes),crop_count=int(bool(classes)),classes=classes,
                    dataset='paddy',
                    message='' if classes else 'ยังไม่พบภาพใบข้าวที่ติดป้ายกำกับ โค้ด GitHub และภาพผลทดลองใช้แทนชุดฝึกไม่ได้')
    def path(self,label,index):
        if label not in self.files or not 0<=index<len(self.files[label]):raise ValueError('ไม่พบภาพที่เลือก')
        return _inside(self.directory,self.directory/self.files[label][index])

def prepare_splits(directory,seed=42):
    """Honor supplied test/validation sets; reject leakage and conflicting labels.

    Without leaf IDs, byte-identical images are grouped; physical-leaf independence
    cannot be guaranteed from unrelated filenames alone.
    """
    root=Path(directory).resolve();records=read_records(root)
    if not records:
        raise ValueError('ไม่พบชุดภาพ Paddy Doctor: ต้องมีโฟลเดอร์คลาสหรือ CSV พร้อมภาพจริง ไม่ใช่เฉพาะ repository โค้ด')
    present={r['label'] for r in records}
    profile=next((name for name,values in PROFILES.items() if set(values)==present),None)
    if profile is None:
        raise ValueError('ต้องมีคลาสครบตาม Paddy Doctor 13 หรือ Paddy Competition 10; พบ: '+', '.join(sorted(present)))
    labels=PROFILES[profile]
    digests={};groups=defaultdict(list);explicit_ids=False;duplicates=0
    parent={}
    def find(key):
        parent.setdefault(key,key)
        if parent[key]!=key:parent[key]=find(parent[key])
        return parent[key]
    hashed=[]
    for row in records:
        digest=hashlib.sha256(_inside(root,root/row['path']).read_bytes()).hexdigest()
        key='sha256:'+digest
        if row['group']:
            parent[find(key)]=find('leaf:'+str(row['group']))
        hashed.append((row,digest))
    for row,digest in hashed:
        old=digests.get(digest)
        if old:
            if old['label']!=row['label'] or old['split']!=row['split']:
                raise ValueError('ภาพซ้ำมีป้ายกำกับหรือชุดแบ่งขัดกัน: '+row['path'])
            duplicates+=1;continue
        digests[digest]=row
        explicit_ids |= bool(row['group'])
        group=find('sha256:'+digest)
        groups[group].append({**row,'group':group,'sha256':digest})
    by_label=defaultdict(list)
    for group,items in groups.items():
        if len({r['label'] for r in items})!=1 or len({r['split'] for r in items})!=1:
            raise ValueError('กลุ่มใบซ้ำข้ามคลาสหรือชุดแบ่ง: '+group)
        by_label[items[0]['label']].append(items)
    result=[]
    for label in labels:
        assigned={'train':[],'validation':[],'test':[]}
        pool=[]
        for group in by_label[label]:
            split=group[0]['split']
            (pool if split in (None,'train') else assigned[split]).append(group)
        rng=random.Random(f'{seed}:{label}');rng.shuffle(pool)
        for split,fraction in (('test',.15),('validation',.2)):
            if not assigned[split]:
                count=max(1,round(len(pool)*fraction));assigned[split],pool=pool[:count],pool[count:]
        assigned['train']=pool
        if any(not assigned[s] for s in assigned):
            raise ValueError('แต่ละคลาสต้องมีอย่างน้อย 3 กลุ่มภาพสำหรับ train/validation/test: '+label)
        for split,items in assigned.items():
            for group in items:
                result.extend({**r,'split':split} for r in group)
    counts={s:dict(Counter(r['label'] for r in result if r['split']==s)) for s in ('train','validation','test')}
    return result,dict(images=len(result),duplicates_skipped=duplicates,counts=counts,profile=profile,labels=list(labels),
        grouping='supplied leaf IDs plus exact-image hashes' if explicit_ids else 'exact-image SHA256; physical leaf IDs unavailable',
        physical_leaf_ids_available=explicit_ids,seed=seed)
