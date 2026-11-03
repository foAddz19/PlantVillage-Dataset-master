import csv
import io
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from app import create_app
from leaflab.dataset import Catalog
from leaflab.scans import ScanStore
from tests.test_device import StubService


class SingleDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=create_app(Catalog(self.root),StubService(),data_dir=self.root/'data')
        self.client=self.app.test_client()
        self.headers={'X-App-Token':re.search(r'name="app-token" content="([^"]+)"',self.client.get('/device').text).group(1)}
        self.store=ScanStore(self.root/'data')
        self.rows=[]
        for index in range(3):
            payload=io.BytesIO();Image.new('RGB',(64,64),(30+index*30,90,40)).save(payload,'PNG')
            row=self.client.post('/api/device/analyze',headers=self.headers,data={
                'image':(io.BytesIO(payload.getvalue()),'leaf.png'),'latitude':str(16+index*.01),
                'longitude':'103','location_source':'simulated'}).json
            if index<2:
                row=self.client.post(f'/api/device/scans/{row["id"]}/save',headers=self.headers,
                    json={'note':f'keep note {index}','severity':'mild'}).json
            self.rows.append(row)
        self.path=f'/api/device/scans/{self.rows[0]["id"]}/delete'

    def test_deletes_one_image_result_point_and_export_record_but_preserves_all_other_data(self):
        keep=self.rows[1];before=self.store.image(keep['id'])
        response=self.client.post(self.path,headers=self.headers,json={'confirm_id':self.rows[0]['id']})
        self.assertEqual(response.status_code,200);self.assertTrue(response.json['deleted'])
        history=self.client.get('/api/device/scans').json
        self.assertEqual(history['total'],1);self.assertEqual(history['items'],[keep])
        self.assertEqual(self.store.image(keep['id']),before)
        self.assertEqual(self.store.get(self.rows[2]['id']),self.rows[2])
        for suffix in ('','/image','/receipt.png'):
            self.assertEqual(self.client.get(f'/api/device/scans/{self.rows[0]["id"]}'+suffix).status_code,409)
        features=self.client.get('/api/device/export/geojson').json['features']
        self.assertEqual([feature['properties']['id'] for feature in features],[keep['id']])
        exported=list(csv.DictReader(io.StringIO(self.client.get('/api/device/export/csv').text.lstrip('\ufeff'))))
        self.assertEqual([item['id'] for item in exported],[keep['id']])
        self.assertEqual(ScanStore(self.root/'data').list()['total'],1)

    def test_requires_post_token_and_confirmation_of_exact_id(self):
        self.assertEqual(self.client.get(self.path).status_code,405)
        self.assertEqual(self.client.post(self.path,json={'confirm_id':self.rows[0]['id']}).status_code,403)
        for body in (None,{},[],{'confirm_id':self.rows[1]['id']},{'confirm_id':self.rows[0]['id'],'all':True}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.path,headers=self.headers,json=body).status_code,400)
        self.assertEqual(self.store.list()['total'],2)

    def test_retries_are_idempotent_and_never_delete_the_next_item(self):
        for _ in range(2):
            response=self.client.post(self.path,headers=self.headers,json={'confirm_id':self.rows[0]['id']})
            self.assertEqual(response.status_code,200)
        self.assertFalse(response.json['deleted'])
        self.assertEqual(self.store.list()['items'],[self.rows[1]])

    def test_cannot_delete_a_draft_or_use_wildcards_lists_or_bulk_routes(self):
        draft=self.rows[2]
        response=self.client.post(f'/api/device/scans/{draft["id"]}/delete',headers=self.headers,json={'confirm_id':draft['id']})
        self.assertFalse(response.json['deleted']);self.assertEqual(self.store.get(draft['id']),draft)
        for identifier in ('all','*',"' OR 1=1 --",'a'*31,self.rows[0]['id']+','+self.rows[1]['id']):
            with self.subTest(identifier=identifier),self.assertRaises(ValueError):self.store.delete_saved(identifier)
        self.assertEqual(self.client.post('/api/device/scans/delete-all',headers=self.headers,json={}).status_code,405)
        self.assertEqual(self.client.post('/api/device/scans/delete',headers=self.headers,json={'ids':[row['id'] for row in self.rows]}).status_code,405)
        self.assertEqual(self.store.list()['total'],2)


if __name__=='__main__':unittest.main()
