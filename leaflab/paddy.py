"""Paddy Doctor inference only: LiteRT CPU, one inference at a time, no sklearn/TF."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .dataset import ROOT
from .paddy_labels import validate_labels, rice_info


def tensor_spec(value, shape):
    if value.get('shape') != shape or value.get('dtype') not in ('uint8', 'int8', 'float32'):
        raise ValueError('รูปแบบ tensor ไม่ตรงกับโมเดล Paddy Doctor')
    dtype = np.dtype(value['dtype'])
    scale = float(value.get('scale', 0))
    zero = value.get('zero_point', 0)
    if dtype.kind != 'f' and (not np.isfinite(scale) or scale <= 0 or
                             type(zero) is not int or not np.iinfo(dtype).min <= zero <= np.iinfo(dtype).max):
        raise ValueError('ค่า quantization ของ tensor ไม่ถูกต้อง')
    return dtype, scale, zero


def encode_input(image, config):
    height, width = config['input']['shape'][1:3]
    pixels = np.asarray(image.convert('RGB').resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32)[None]
    dtype, scale, zero = tensor_spec(config['input'], [1, height, width, 3])
    if dtype.kind != 'f':
        pixels = np.clip(np.rint(pixels / scale + zero), np.iinfo(dtype).min, np.iinfo(dtype).max)
    return np.ascontiguousarray(pixels, dtype=dtype)


class PaddyService:
    def __init__(self, catalog=None, model_dir=None):
        self.model_dir = Path(model_dir or ROOT / 'models' / 'paddy').resolve()
        self.lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.artifact = None
        self.compiled = None
        self.message = 'ยังไม่มีโมเดลข้าวที่ฝึกแล้ว: ใช้ train_paddy.py บน PC แล้วนำ model.json และไฟล์ .tflite มาไว้ใน models/paddy'
        path = self.model_dir / 'model.json'
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
            if config.get('schema_version') != 1:
                raise ValueError('schema ไม่ถูกต้อง')
            self.labels = validate_labels(config.get('labels', []))
            if config.get('preprocessing') != 'rgb_0_255_bilinear_stretch_rescaling_in_model':
                raise ValueError('preprocessing ไม่รองรับ')
            shape = config['input']['shape']
            if len(shape) != 4 or shape[0] != 1 or shape[3] != 3 or not all(type(n) is int and 32 <= n <= 512 for n in shape[1:3]):
                raise ValueError('ขนาดภาพนำเข้าไม่ถูกต้อง')
            tensor_spec(config['input'], shape)
            tensor_spec(config['output'], [1, len(self.labels)])
            model_path = (self.model_dir / config['model_file']).resolve()
            if not model_path.is_relative_to(self.model_dir) or model_path.suffix != '.tflite':
                raise ValueError('เส้นทางโมเดลไม่ถูกต้อง')
            if hashlib.sha256(model_path.read_bytes()).hexdigest() != config['sha256']:
                raise ValueError('SHA256 ของโมเดลไม่ตรงกับ metadata')
            from ai_edge_litert.compiled_model import CompiledModel, HardwareAccelerator
            compiled = CompiledModel.from_file(str(model_path), hardware_accel=HardwareAccelerator.CPU)
            signatures = list(compiled.get_signature_list())
            if not signatures:
                raise ValueError('โมเดลต้องมี serving signature')
            for actual, expected in ((compiled.get_input_tensor_details(signatures[0]), config['input']),
                                     (compiled.get_output_tensor_details(signatures[0]), config['output'])):
                if len(actual) != 1:
                    raise ValueError('โมเดลต้องมี input/output อย่างละหนึ่ง tensor')
                detail = next(iter(actual.values()))
                if detail['shape'] != expected['shape'] or np.dtype(detail['dtype']).name != expected['dtype']:
                    raise ValueError('tensor ในไฟล์โมเดลไม่ตรงกับ metadata')
            inputs = compiled.create_input_buffers(0)
            outputs = compiled.create_output_buffers(0)
            if len(inputs) != 1 or len(outputs) != 1:
                raise ValueError('โมเดลต้องมี input/output อย่างละหนึ่ง tensor')
            self.config, self.compiled = config, compiled
            self.inputs, self.outputs = inputs, outputs
            # Validate the actual tensor contract and a real invocation before marking ready.
            self._scores(Image.new('RGB', (32, 32)))
            metadata = config.get('metadata', {})
            self.artifact = {'metadata': metadata, 'manifest': config.get('manifest', [])}
            self.message = f'โมเดลข้าว {len(self.labels)} กลุ่มพร้อมใช้งาน · ประมวลผลด้วย LiteRT CPU'
        except Exception as exc:
            logging.exception('Cannot load Paddy Doctor model')
            if 'compiled' in locals():
                compiled.close()
            self.compiled = None
            self.message = 'โหลดโมเดลข้าวไม่ได้: ' + str(exc)

    def close(self):
        with self.inference_lock:
            for buffer in getattr(self, 'inputs', []) + getattr(self, 'outputs', []):
                buffer.destroy()
            self.inputs, self.outputs = [], []
            if self.compiled is not None:
                self.compiled.close()
                self.compiled = None
            self.artifact = None

    def status(self):
        with self.lock:
            return dict(ready=self.artifact is not None, running=False, backend='paddy',
                        training_available=False, stage='ready' if self.artifact else 'missing_model',
                        percent=100 if self.artifact else 0, message=self.message,
                        metadata=self.artifact['metadata'] if self.artifact else None)

    def start_training(self, *args):
        raise ValueError('ฝึก Paddy Doctor ด้วย train_paddy.py ใน .venv-training บน PC; โหมด Raspberry Pi ใช้วิเคราะห์ภาพเท่านั้น')

    def _scores(self, image):
        with self.inference_lock:
            self.inputs[0].write(encode_input(image, self.config))
            self.compiled.run_by_index(0, self.inputs, self.outputs)
            dtype, scale, zero = tensor_spec(self.config['output'], [1, len(self.labels)])
            scores = self.outputs[0].read(len(self.labels), dtype).astype(np.float32).reshape(-1)
            if dtype.kind != 'f':
                scores = (scores - zero) * scale
        if scores.size != len(self.labels) or not np.isfinite(scores).all() or (scores < -.01).any() or (scores > 1.01).any() or abs(float(scores.sum()) - 1) > .12:
            raise ValueError('ผลลัพธ์โมเดลต้องเป็น softmax ตามจำนวนคลาส')
        return np.clip(scores, 0, 1)

    def predict(self, image):
        if self.artifact is None:
            raise LookupError(self.message)
        scores = self._scores(image)
        order = np.argsort(-scores, kind='stable')[:3]
        return dict(candidates=[{**rice_info(self.labels[i]), 'score': round(float(scores[i]), 5)} for i in order],
                    uncertain=bool(scores[order[0]] < .6 or scores[order[0]] - scores[order[1]] < .15),
                    model_created_at=self.artifact['metadata'].get('created_at'), model_id=self.config['sha256'],
                    note=f'คะแนนยังไม่ผ่านการปรับเทียบ โมเดลเลือกจากข้าว {len(self.labels)} กลุ่ม รวมอาการแมลง แม้ภาพจะไม่ใช่ข้าว ผลนี้ไม่ใช่การยืนยันโรค')
