"""Small, offline rice-care knowledge base; model scores never prescribe pesticides."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path

DATA_PATH = Path(__file__).with_name('care_knowledge.json')
DISCLAIMER = 'ผลภาพเป็นการประเมินเบื้องต้น ตรวจอาการจริงและสภาพแปลงประกอบ คะแนน AI ไม่ใช่โอกาสยืนยันโรค'
CHEMICAL_POLICY = 'ก่อนใช้สารใด ให้เจ้าหน้าที่ตรวจยืนยันสาเหตุและตรวจทะเบียนสำหรับข้าว ฉลาก อัตราใช้ อุปกรณ์ป้องกัน และระยะก่อนเก็บเกี่ยวของผลิตภัณฑ์จริง'


@lru_cache(maxsize=1)
def knowledge():
    return json.loads(DATA_PATH.read_text(encoding='utf-8'))


def library():
    data = knowledge()
    return dict(version=data['version'], checked_on=data['checked_on'],
                scope=data['scope'], items=[guide(key) for key in data['profiles']])


def guide(key):
    data = knowledge()
    if key not in data['profiles']:
        raise LookupError('ไม่พบคู่มือกลุ่มนี้')
    profile = deepcopy(data['profiles'][key])
    return dict(schema_version=1, knowledge_version=data['version'], checked_on=data['checked_on'],
                basis='library', status='reference', status_label='คู่มืออ้างอิง · ไม่ใช่ผลตรวจ',
                disease_key=key, title=profile.pop('name'), disclaimer=DISCLAIMER,
                chemical_policy=CHEMICAL_POLICY, scope=data['scope'], urgent=False,
                sources=[deepcopy(data['sources'][item]) for item in profile.pop('source_ids')], **profile)


def _general(status, basis):
    data = knowledge()
    unsupported = status == 'unsupported'
    return dict(schema_version=1, knowledge_version=data['version'], checked_on=data['checked_on'],
        basis=basis, status=status, status_label='ยังไม่มีคู่มือตรงกลุ่ม' if unsupported else 'ตรวจเพิ่มก่อนเลือกวิธีจัดการ',
        disease_key=None, title='คำแนะนำตรวจเพิ่มเติม', urgent=False, scope=data['scope'],
        category='ทั่วไป', summary='คู่มือนี้รองรับข้าว 10 กลุ่ม หากเป็นพืชหรืออาการอื่นให้ตรวจยืนยันกับเจ้าหน้าที่' if unsupported else
            'ผลยังไม่ชัด จึงแสดงแนวทางเก็บข้อมูลเพิ่มก่อนเลือกคำแนะนำเฉพาะโรคหรือแมลง',
        cause='ภาพเดียวอาจแยกโรค แมลง ธาตุอาหาร และความเสียหายจากสภาพแวดล้อมไม่ได้',
        checks=['ตรวจว่าเป็นต้นข้าวและเห็นส่วนที่มีอาการชัดเจน', 'ดูใบทั้งสองด้าน โคนกอ และรวง พร้อมสำรวจต้นข้างเคียง'],
        next_steps=['ถ่ายภาพใหม่ในแสงสม่ำเสมอ ให้เห็นใบที่มีอาการเต็มภาพ', 'เก็บภาพทั้งกอและบริเวณรอบจุดตรวจ พร้อมระยะข้าวและประวัติปุ๋ย/น้ำ'],
        prevention=['สำรวจหลายจุดในแปลงและบันทึกการเปลี่ยนแปลงของอาการ'],
        avoid=['อย่าเลือกสารเคมีหรือถอนทำลายต้นจากคะแนนภาพที่ยังไม่ชัด'],
        seek_help='หากกอแห้งตาย อาการลามเร็ว หรือกระทบรวง ให้ติดต่อเจ้าหน้าที่เกษตรหรือศูนย์วิจัยข้าวในพื้นที่โดยเร็ว',
        follow_up='ถ่ายซ้ำทันทีเมื่อภาพไม่ชัด และติดตามอาการแปลง; หากลามเร็วให้ขอคำแนะนำทันที',
        receipt_lines=['ผลยังไม่ยืนยัน ถ่ายใบใหม่ให้ชัด', 'เก็บภาพทั้งกอและสำรวจต้นข้างเคียง', 'ลามเร็ว/กอแห้ง/รวงเสีย ติดต่อเจ้าหน้าที่'],
        sources=[], disclaimer=DISCLAIMER, chemical_policy=CHEMICAL_POLICY)


def advice_for_prediction(prediction, basis='analysis_snapshot'):
    if not isinstance(prediction, dict):
        return _general('uncertain', basis)
    candidates = prediction.get('candidates')
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return _general('uncertain', basis)
    best = candidates[0]
    key = best.get('disease')
    if best.get('crop') != 'Rice' or not isinstance(key, str) or key not in knowledge()['profiles'] or best.get('label') != 'Rice___' + key:
        return _general('unsupported', basis)
    # Keep the same minimum score/margin as the rice model, even for legacy records.
    try:
        scores = [float(item['score']) for item in candidates]
        valid = all(not isinstance(item['score'], bool) for item in candidates)
        valid = valid and all(math.isfinite(score) and 0 <= score <= 1 for score in scores)
        valid = valid and scores[0] >= .6 and (len(scores) < 2 or scores[0] - max(scores[1:]) >= .15 - 1e-9)
    except (ValueError, TypeError, KeyError, OverflowError):
        valid = False
    if prediction.get('uncertain') is not False or not valid:
        return _general('uncertain', basis)
    advice = guide(key)
    advice.update(basis=basis, status='healthy' if key == 'normal' else 'provisional',
                  status_label='เฝ้าระวังและดูแลแปลง' if key == 'normal' else 'คำแนะนำตามผลเบื้องต้น · ตรวจอาการประกอบ')
    return advice


def advice_for_record(row):
    """Old rows get an explicitly labelled current guide; stored snapshots stay intact."""
    saved = row.get('advice')
    advice = deepcopy(saved) if isinstance(saved, dict) and saved.get('schema_version') == 1 else \
        advice_for_prediction(row.get('prediction'), basis='current_reference')
    advice['urgent'] = row.get('severity') == 'severe'
    return advice
