import csv
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import create_app
from leaflab.care import advice_for_prediction, advice_for_record, guide, knowledge, library
from leaflab.dataset import Catalog
from leaflab.paddy_labels import COMPETITION_LABELS, rice_info
from leaflab.receipt import receipt_layout
from leaflab.scans import ScanStore


def prediction(key='blast', uncertain=False, score=.85, second=.10):
    return dict(candidates=[{**rice_info(key), 'score':score},
                           {**rice_info('brown_spot' if key != 'brown_spot' else 'normal'), 'score':second}],
                uncertain=uncertain, model_created_at='test', model_id='test')


class CareTests(unittest.TestCase):
    def test_all_ten_classes_have_independent_guides_and_primary_sources(self):
        self.assertEqual(set(knowledge()['profiles']), set(COMPETITION_LABELS))
        for key in COMPETITION_LABELS:
            advice=advice_for_prediction(prediction(key))
            with self.subTest(key=key):
                self.assertEqual(advice['disease_key'],key)
                self.assertEqual(advice['status'],'healthy' if key=='normal' else 'provisional')
                self.assertTrue(advice['next_steps'] and advice['checks'] and advice['seek_help'] and advice['sources'])
                self.assertTrue(all(source['url'].startswith('https://') for source in advice['sources']))
                self.assertEqual(guide(key)['status'],'reference')
        altered=guide('blast');altered['next_steps'].clear()
        self.assertTrue(guide('blast')['next_steps'])

    def test_uncertain_low_score_margin_and_invalid_outputs_never_select_disease_care(self):
        cases=[prediction(uncertain=True),prediction(score=.59),prediction(score=.65,second=.55),
               prediction(score=float('nan')),prediction(score=float('inf')),prediction(score=True),
               prediction(score='bad'),{},None,{'candidates':[]},prediction(uncertain=None)]
        for value in cases:
            with self.subTest(value=value):
                advice=advice_for_prediction(value)
                self.assertIsNone(advice['disease_key'])
                self.assertEqual(advice['status'],'uncertain')
                self.assertFalse(advice['sources'])
        self.assertEqual(advice_for_prediction(prediction(score=.6,second=.45))['disease_key'],'blast')

    def test_other_crops_unknown_rice_and_inconsistent_labels_are_not_matched(self):
        for fields in ({'crop':'Apple'},{'label':'Apple___blast'},{'disease':'unknown'}, {'disease':[]}):
            value=prediction();value['candidates'][0].update(fields)
            self.assertEqual(advice_for_prediction(value)['status'],'unsupported')

    def test_snapshot_is_unchanged_across_save_restart_and_knowledge_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ScanStore(temp)
            row=store.draft(b'original-image','image/png',prediction(),'upload',
                dict(latitude=16.,longitude=103.,location_source='simulated'))
            snapshot=row['advice']
            with patch('leaflab.care.advice_for_prediction',side_effect=AssertionError('Snapshot must not be regenerated')):
                saved=store.save(row['id'],'severe','original note')
                restarted=ScanStore(temp).get(row['id'])
                self.assertTrue(restarted['advice']['urgent'])
                self.assertEqual({**restarted['advice'],'urgent':False},snapshot)
                self.assertEqual(saved['prediction'],row['prediction'])
                self.assertEqual(store.image(row['id'])[0],b'original-image')

    def test_additive_legacy_migration_preserves_data_and_labels_current_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'scans.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE scans (id TEXT PRIMARY KEY,captured_at TEXT,saved_at TEXT,source TEXT,latitude REAL,longitude REAL,location_source TEXT,prediction TEXT,severity TEXT,note TEXT,image BLOB,mime TEXT)')
                db.execute('INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('legacy','2026-09-01T00:00:00+00:00','2026-09-01T00:01:00+00:00','upload',16,103,'manual',
                     json.dumps(prediction()),'mild','keep this',b'old-image','image/png'))
            store=ScanStore(temp);row=store.get('legacy')
            self.assertEqual(row['note'],'keep this');self.assertEqual(row['latitude'],16)
            self.assertEqual(store.image('legacy')[0],b'old-image')
            self.assertEqual(row['advice']['basis'],'current_reference')
            self.assertEqual(store.list()['total'],1)
            ScanStore(temp)  # Migration is idempotent.
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute('SELECT advice FROM scans').fetchone()[0])

    def test_receipt_uses_the_stored_guidance_and_never_names_a_cure_for_uncertain_output(self):
        base=dict(id='test',captured_at='2026-11-02T01:00:00+00:00',latitude=16,longitude=103,
                  location_source='simulated',severity='unassessed',note='',prediction=prediction())
        base['advice']=advice_for_prediction(base['prediction'])
        base['advice']['receipt_lines']=['ข้อความสำเนาคำแนะนำเดิม']
        _,layout,_=receipt_layout(base)
        texts=''.join(item.get('text','') for item in layout)
        self.assertIn('ข้อความสำเนาคำแนะนำเดิม',texts)
        base['prediction']=prediction(uncertain=True);base.pop('advice')
        _,layout,_=receipt_layout(base)
        texts=''.join(item.get('text','') for item in layout)
        self.assertIn('ยังไม่เลือกวิธีจัดการเฉพาะโรค',texts)
        self.assertNotIn('ตรวจแผลรูปกระสวย',texts)


class CareApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        class Service:
            result=prediction()
            def status(self):return dict(ready=True,running=False)
            def predict(self,image):return self.result
        self.service=Service()
        self.app=create_app(Catalog(self.root),self.service,data_dir=self.root/'data')
        self.client=self.app.test_client()
        token=re.search(r'name="app-token" content="([^"]+)"',self.client.get('/device').text).group(1)
        self.headers={'X-App-Token':token}
        payload=io.BytesIO();Image.new('RGB',(64,64),(25,90,35)).save(payload,'PNG');self.payload=payload.getvalue()

    def analyze(self):
        return self.client.post('/api/device/analyze',headers=self.headers,data={
            'image':(io.BytesIO(self.payload),'leaf.png'),'latitude':'16','longitude':'103','location_source':'simulated'})

    def test_library_lab_device_history_exports_and_preview(self):
        catalog=self.client.get('/api/care/library')
        self.assertEqual(len(catalog.json['items']),10)
        self.assertEqual(self.client.get('/api/care/blast').json['status'],'reference')
        self.assertEqual(self.client.get('/api/care/unknown').status_code,409)
        lab=self.client.post('/api/predict',headers=self.headers,data={'image':(io.BytesIO(self.payload),'leaf.png')})
        self.assertEqual(lab.json['advice']['disease_key'],'blast')
        row=self.analyze().json
        self.assertEqual(row['advice']['basis'],'analysis_snapshot')
        self.assertEqual(self.client.get('/api/device/scans').json['total'],0)
        saved=self.client.post(f'/api/device/scans/{row["id"]}/save',headers=self.headers,
            json={'severity':'severe','advice':{'fake':True}}).json
        self.assertTrue(saved['advice']['urgent']);self.assertEqual(saved['advice']['disease_key'],'blast')
        self.assertEqual(self.client.get(f'/api/device/scans/{row["id"]}').json['advice'],saved['advice'])
        self.assertEqual(self.client.get('/api/device/scans').json['items'][0]['advice'],saved['advice'])
        exported=self.client.get('/api/device/export/geojson').json
        self.assertEqual(exported['features'][0]['properties']['advice'],saved['advice'])
        rows=list(csv.DictReader(io.StringIO(self.client.get('/api/device/export/csv').text.lstrip('\ufeff'))))
        self.assertEqual(rows[0]['care_status'],'provisional');self.assertIn('ไนโตรเจน',rows[0]['care_next_steps'])
        preview=self.client.get(f'/api/device/scans/{row["id"]}/receipt.png')
        self.assertEqual(preview.status_code,200)
        self.assertEqual(Image.open(io.BytesIO(preview.data)).width,384)

    def test_uncertain_scan_stays_general_after_saving(self):
        self.service.result=prediction(uncertain=True)
        row=self.analyze().json
        saved=self.client.post(f'/api/device/scans/{row["id"]}/save',headers=self.headers,json={}).json
        self.assertEqual(saved['advice']['status'],'uncertain');self.assertIsNone(saved['advice']['disease_key'])


if __name__=='__main__':unittest.main()
