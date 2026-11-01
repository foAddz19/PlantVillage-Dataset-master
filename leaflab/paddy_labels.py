"""Paddy Doctor 13-class vocabulary; order exported with each trained model."""
LABELS = (
    'bacterial_leaf_blight', 'bacterial_leaf_streak', 'bacterial_panicle_blight',
    'black_stem_borer', 'blast', 'brown_spot', 'downy_mildew', 'hispa',
    'leaf_roller', 'normal', 'tungro', 'white_stem_borer', 'yellow_stem_borer',
)
THAI = {
    'dead_heart': 'อาการยอดแห้ง (Dead heart)',
    'bacterial_leaf_blight': 'โรคขอบใบแห้ง',
    'bacterial_leaf_streak': 'โรคใบขีดโปร่งแสง',
    'bacterial_panicle_blight': 'โรครวงไหม้จากแบคทีเรีย',
    'black_stem_borer': 'ความเสียหายจากหนอนกอ (Black stem borer)',
    'blast': 'โรคไหม้', 'brown_spot': 'โรคใบจุดสีน้ำตาล',
    'downy_mildew': 'โรคราน้ำค้าง', 'hispa': 'ความเสียหายจากแมลง Hispa',
    'leaf_roller': 'ความเสียหายจากหนอนห่อใบ', 'normal': 'ใบปกติ',
    'tungro': 'โรคใบสีส้ม (ทังโกร)',
    'white_stem_borer': 'ความเสียหายจากหนอนกอ (White stem borer)',
    'yellow_stem_borer': 'ความเสียหายจากหนอนกอ (Yellow stem borer)',
}
COMPETITION_LABELS = (
    'bacterial_leaf_blight', 'bacterial_leaf_streak', 'bacterial_panicle_blight',
    'blast', 'brown_spot', 'dead_heart', 'downy_mildew', 'hispa', 'normal', 'tungro',
)
PROFILES = {'paddy-doctor-13': LABELS, 'paddy-competition-10': COMPETITION_LABELS}
ALL_LABELS = set(LABELS) | set(COMPETITION_LABELS)

def validate_labels(labels):
    if not any(list(labels) == list(profile) for profile in PROFILES.values()):
        raise ValueError('รายชื่อและลำดับคลาสไม่ตรงกับ Paddy Doctor 13 หรือ Paddy Competition 10')
    return tuple(labels)

def rice_info(label):
    raw = label.removeprefix('Rice___')
    if raw not in ALL_LABELS:
        raise ValueError('ไม่รู้จักคลาส Paddy Doctor: ' + raw)
    return dict(label='Rice___'+raw, crop='Rice', crop_th='ข้าว', disease=raw,
                disease_th=THAI[raw], healthy=raw == 'normal')
