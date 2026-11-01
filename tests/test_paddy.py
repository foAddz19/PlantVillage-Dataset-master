import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image
import numpy as np

from app import create_app
from leaflab.dataset import Catalog, ROOT, label_info
from leaflab.paddy import PaddyService
from leaflab.paddy_data import PaddyCatalog, prepare_splits, read_records
from leaflab.paddy_labels import LABELS, COMPETITION_LABELS


class PaddyDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def fill(self, labels=COMPETITION_LABELS):
        for index, label in enumerate(labels):
            folder = self.root / 'train_images' / label
            folder.mkdir(parents=True)
            for item in range(5):
                Image.new('RGB', (32, 32), (index * 12, item * 30, 40)).save(folder / f'{index}-{item}.png')

    def test_both_profiles_split_before_training_and_are_reproducible(self):
        for labels in (COMPETITION_LABELS, LABELS):
            with self.subTest(count=len(labels)), tempfile.TemporaryDirectory() as directory:
                previous = self.root
                self.root = Path(directory)
                self.fill(labels)
                records, info = prepare_splits(self.root)
                self.assertEqual(records, prepare_splits(self.root)[0])
                self.assertEqual(info['labels'], list(labels))
                split_groups = [{r['group'] for r in records if r['split'] == s} for s in ('train', 'validation', 'test')]
                self.assertFalse(split_groups[0] & split_groups[1] | split_groups[0] & split_groups[2] | split_groups[1] & split_groups[2])
                for split in ('train', 'validation', 'test'):
                    self.assertEqual({r['label'] for r in records if r['split'] == split}, set(labels))
                self.root = previous

    def test_csv_links_existing_images_without_losing_labels(self):
        self.fill()
        (self.root / 'train.csv').write_text('image_id,label,variety,age\n0-0.png,bacterial_leaf_blight,ADT45,45\n', encoding='utf-8')
        catalog = PaddyCatalog(self.root)
        self.assertEqual(catalog.summary()['images'], 50)
        self.assertEqual(catalog.summary()['class_count'], 10)
        self.assertTrue(catalog.path('Rice___normal', 0).is_file())
        self.assertTrue(label_info('Rice___normal')['healthy'])
        self.assertIn('ยอดแห้ง', label_info('Rice___dead_heart')['disease_th'])

    def test_duplicate_images_across_classes_are_rejected(self):
        self.fill()
        source = self.root / 'train_images' / COMPETITION_LABELS[0] / '0-0.png'
        shutil.copy2(source, self.root / 'train_images' / COMPETITION_LABELS[1] / 'duplicate.png')
        with self.assertRaisesRegex(ValueError, 'ภาพซ้ำ'): prepare_splits(self.root)

    def test_duplicate_images_across_supplied_splits_are_rejected(self):
        self.fill()
        target = self.root / 'test' / COMPETITION_LABELS[0]
        target.mkdir(parents=True)
        shutil.copy2(self.root / 'train_images' / COMPETITION_LABELS[0] / '0-0.png', target / 'duplicate.png')
        with self.assertRaisesRegex(ValueError, 'ภาพซ้ำ'): prepare_splits(self.root)

    def test_explicit_leaf_groups_cannot_cross_supplied_splits(self):
        self.fill()
        (self.root / 'metadata.csv').write_text('image_id,label,leaf_id,split\n0-0.png,bacterial_leaf_blight,leaf-a,train\n0-1.png,bacterial_leaf_blight,leaf-a,test\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'กลุ่มใบซ้ำ'): prepare_splits(self.root)

    def test_csv_path_cannot_escape_dataset(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / 'outside.png'
            Image.new('RGB', (32, 32)).save(path)
            (self.root / 'metadata.csv').write_text(f'path,label\n{path.as_posix()},normal\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'ออกนอก'): read_records(self.root)

    def test_empty_code_repository_is_not_training_data(self):
        (self.root / 'MobileNet.ipynb').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'ไม่พบชุดภาพ'): prepare_splits(self.root)
        self.assertEqual(PaddyCatalog(self.root).summary()['images'], 0)

    def test_plantvillage_empty_placeholders_are_excluded(self):
        (self.root / 'Rice___Brown_spot').mkdir()
        catalog = Catalog(self.root)
        self.assertEqual(catalog.summary()['class_count'], 0)
        self.assertEqual(catalog.empty_classes, ['Rice___Brown_spot'])

    def test_quantized_ties_use_the_same_class_order_as_evaluation(self):
        from train_paddy import evaluate_probabilities
        service = object.__new__(PaddyService)
        service.labels = COMPETITION_LABELS
        service.artifact = {'metadata':{}}
        service.config = {'sha256':'test'}
        scores = np.full(10, .1, dtype=np.float32)
        service._scores = lambda image: scores
        prediction = service.predict(Image.new('RGB',(32,32)))
        self.assertEqual([r['label'] for r in prediction['candidates']], ['Rice___'+label for label in COMPETITION_LABELS[:3]])
        self.assertEqual(evaluate_probabilities([scores],[0],COMPETITION_LABELS)['accuracy'],1)

    def test_missing_model_blocks_prediction_and_web_training(self):
        app = create_app(model_backend='paddy', model_dir=self.root / 'missing', dataset_dir=self.root, data_dir=self.root / 'scans')
        client = app.test_client()
        token = re.search(r'name="app-token" content="([^"]+)"', client.get('/').text).group(1)
        headers = {'X-App-Token': token}
        status = client.get('/api/status').json
        self.assertFalse(status['ready'])
        self.assertFalse(status['training_available'])
        self.assertEqual(client.get('/api/examples').json['items'], [])
        self.assertEqual(client.post('/api/predict', headers=headers).status_code, 409)
        self.assertEqual(client.post('/api/train', headers=headers, json={'samples_per_class':150}).status_code, 400)

    def test_paddy_process_does_not_import_heavy_legacy_or_training_libraries(self):
        code = "import tempfile,sys; from app import create_app; create_app(model_backend='paddy',data_dir=tempfile.mkdtemp()); assert not any(m in sys.modules for m in ('sklearn','joblib','tensorflow'))"
        subprocess.run([sys.executable, '-c', code], cwd=ROOT, check=True, capture_output=True)

    def test_parallel_requests_are_rejected_before_decoding_another_image(self):
        entered, release = threading.Event(), threading.Event()
        class SlowService:
            def status(self): return {'ready':True, 'running':False}
            def predict(self, image):
                entered.set()
                if not release.wait(5): raise RuntimeError('Test timeout')
                return {'candidates':[]}
        app = create_app(service=SlowService(), model_backend='paddy', dataset_dir=self.root, data_dir=self.root/'scans')
        client = app.test_client()
        token = re.search(r'name="app-token" content="([^"]+)"',client.get('/').text).group(1)
        headers={'X-App-Token':token}
        buf=io.BytesIO();Image.new('RGB',(32,32)).save(buf,'PNG')
        results=[]
        def first():
            results.append(app.test_client().post('/api/predict',headers=headers,
                data={'image':(io.BytesIO(buf.getvalue()),'leaf.png')}).status_code)
        thread=threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(client.post('/api/predict',headers=headers).status_code,429)
        finally:
            release.set();thread.join(5)
        self.assertEqual(results,[200])
        self.assertEqual(client.post('/api/predict',headers=headers).status_code,400)


@unittest.skipUnless(os.environ.get('PADDY_TEST_MODEL_DIR'), 'Set PADDY_TEST_MODEL_DIR to a temporary INT8 fixture or trained model')
class PaddyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.model = self.root / 'model'
        shutil.copytree(os.environ['PADDY_TEST_MODEL_DIR'], self.model)

    def test_real_int8_inference_upload_examples_device_and_saved_history(self):
        service = PaddyService(model_dir=self.model)
        self.addCleanup(service.close)
        self.assertTrue(service.status()['ready'], service.message)
        dataset = self.root / 'data' / 'normal'
        dataset.mkdir(parents=True)
        image = Image.new('RGB', (96, 64), (30, 110, 40))
        image.save(dataset / 'leaf.png')
        app = create_app(service=service, model_backend='paddy', dataset_dir=dataset.parent, data_dir=self.root / 'scans')
        client = app.test_client()
        token = re.search(r'name="app-token" content="([^"]+)"', client.get('/device').text).group(1)
        headers = {'X-App-Token':token}
        buffer = io.BytesIO(); image.save(buffer, 'PNG')
        response = client.post('/api/predict', headers=headers, data={'image':(io.BytesIO(buffer.getvalue()),'leaf.png')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['candidates']), 3)
        self.assertEqual(response.json['model_id'], service.config['sha256'])
        self.assertTrue(all(c['crop'] == 'Rice' for c in response.json['candidates']))
        self.assertEqual(client.post('/api/predict-example', headers=headers, json={'label':'Rice___normal','index':0}).status_code, 200)
        row = client.post('/api/device/analyze', headers=headers, data={'image':(io.BytesIO(buffer.getvalue()),'leaf.png'),
            'source':'upload', 'latitude':'16.0', 'longitude':'103.0', 'location_source':'manual'}).json
        self.assertIn('id', row)
        saved = client.post(f'/api/device/scans/{row["id"]}/save',headers=headers,json={'note':'rice integration'})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(client.get('/api/device/scans').json['total'], 1)

    def corrupt(self, change):
        path = self.model / 'model.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        change(config)
        path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertLogs(level='ERROR'):
            service = PaddyService(model_dir=self.model)
        self.assertFalse(service.status()['ready'])
        return service

    def test_swapped_label_order_is_rejected(self):
        self.corrupt(lambda c: c['labels'].reverse())

    def test_hash_mismatch_is_rejected(self):
        self.corrupt(lambda c: c.update(sha256='0'*64))

    def test_tensor_shape_mismatch_is_rejected(self):
        service = self.corrupt(lambda c: c['input'].update(shape=[1,64,64,3]))
        self.assertIn('tensor', service.message)


if __name__ == '__main__':
    unittest.main()
