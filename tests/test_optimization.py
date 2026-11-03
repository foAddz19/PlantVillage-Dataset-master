"""Lightweight safeguards for frozen-split selection; no model or image loading."""
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

from evaluate_paddy_candidate import (
    classification_metrics,
    declared_accuracy_identity,
    manifest_identity,
    validate_reference,
)
from optimize_paddy import stratified_calibration


def frozen_config():
    """Small, uneven rice dataset with genuinely distinct image identities."""
    labels = ['bacterial_leaf_blight', 'blast', 'normal']
    sizes = {'train': (5, 9, 12), 'validation': (2, 3, 4), 'test': (2, 3, 4)}
    rows = []
    for split, counts in sizes.items():
        for label, count in zip(labels, counts):
            for index in range(count):
                path = f'{split}_images/{label}/{index:03d}.jpg'
                digest = hashlib.sha256(path.encode('utf-8')).hexdigest()
                rows.append(dict(path=path, sha256=digest, group='leaf:' + digest,
                                 label=label, split=split))
    counts = {split: dict(Counter(row['label'] for row in rows if row['split'] == split))
              for split in sizes}
    return dict(schema_version=1, labels=labels, manifest=rows,
                preprocessing='rgb_0_255_bilinear_stretch_rescaling_in_model',
                metadata=dict(classes=len(labels), accuracy=.75,
                    **{split + '_images': sum(counts[split].values()) for split in sizes},
                    split_info=dict(images=len(rows), labels=labels[:], counts=counts,
                        profile='test-fixture-rice', grouping='supplied leaf IDs plus exact-image hashes',
                        physical_leaf_ids_available=True, seed=42)))


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.config = frozen_config()
        self.train = [row for row in self.config['manifest'] if row['split'] == 'train']

    def test_calibration_is_reproducible_balanced_and_disjoint_from_held_out(self):
        before = deepcopy(self.train)
        selected = stratified_calibration(self.train, self.config['labels'], 12, 42)
        self.assertEqual(selected, stratified_calibration(self.train, self.config['labels'], 12, 42))
        self.assertNotEqual(selected, stratified_calibration(self.train, self.config['labels'], 12, 43))
        self.assertEqual(len(selected), 12)
        self.assertEqual(len({row['path'] for row in selected}), 12)
        self.assertEqual(Counter(row['label'] for row in selected),
                         Counter(dict.fromkeys(self.config['labels'], 4)))
        held_out = {row['sha256'] for row in self.config['manifest'] if row['split'] != 'train'}
        self.assertFalse(held_out & {row['sha256'] for row in selected})
        self.assertTrue(all(row['split'] == 'train' for row in selected))
        self.assertEqual(self.train, before, 'Calibration must not reorder/mutate the frozen source')

    def test_partial_round_balances_classes_and_oversized_request_exhausts_without_duplicates(self):
        selected = stratified_calibration(self.train, self.config['labels'], 8, 7)
        counts = Counter(row['label'] for row in selected)
        self.assertEqual(len(selected), 8)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        all_rows = stratified_calibration(self.train, self.config['labels'], 100, 7)
        self.assertEqual({row['path'] for row in all_rows}, {row['path'] for row in self.train})
        self.assertEqual(len(all_rows), len(self.train))

    def test_contaminated_calibration_cannot_include_validation_or_test_images(self):
        for split in ('validation', 'test'):
            held_out = next(row for row in self.config['manifest'] if row['split'] == split)
            contaminated = self.train + [held_out]
            with self.subTest(split=split), self.assertRaises(ValueError):
                stratified_calibration(contaminated, self.config['labels'], 12, 42)

    def test_unknown_classes_and_duplicate_image_paths_are_rejected(self):
        unknown = deepcopy(self.train)
        unknown[0]['label'] = 'disease-not-in-model'
        duplicate = self.train + [deepcopy(self.train[0])]
        for name, rows in [('unknown', unknown), ('duplicate', duplicate)]:
            with self.subTest(change=name), self.assertRaises(ValueError):
                stratified_calibration(rows, self.config['labels'], 12, 42)

    def test_calibration_requires_unique_labels_and_a_positive_integer_count(self):
        for count in (0, -1, 1.5, True, '3', None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                stratified_calibration(self.train, self.config['labels'], count, 42)
        for labels in ([], ['normal', 'normal']):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                stratified_calibration(self.train, labels, 12, 42)


class FrozenManifestTests(unittest.TestCase):
    def setUp(self):
        self.baseline = frozen_config()

    def test_row_order_and_new_accuracy_do_not_change_frozen_dataset_identity(self):
        candidate = deepcopy(self.baseline)
        candidate['manifest'].reverse()
        candidate['manifest'][0]['unrelated_note'] = 'No identity change'
        candidate['metadata'].update(accuracy=.84, evaluation_split='validation')
        rows, report = validate_reference(self.baseline, candidate)
        self.assertEqual(rows, manifest_identity(self.baseline))
        self.assertEqual(report['records'], len(self.baseline['manifest']))
        _, original = validate_reference(self.baseline, self.baseline)
        self.assertEqual(report['manifest_sha256'], original['manifest_sha256'])

    def test_candidate_cannot_silently_change_any_frozen_identity_field(self):
        changes = dict(path='train_images/blast/replaced.jpg', sha256='f' * 64,
                       group='a-different-physical-leaf', label='normal', split='validation')
        for field, value in changes.items():
            with self.subTest(field=field):
                candidate = deepcopy(self.baseline)
                candidate['manifest'][0][field] = value
                with self.assertRaisesRegex(ValueError, 'frozen manifest identities'):
                    validate_reference(self.baseline, candidate)

    def test_removed_or_extra_samples_and_reordered_labels_are_rejected(self):
        removed = deepcopy(self.baseline)
        removed['manifest'].pop()
        added = deepcopy(self.baseline)
        extra = deepcopy(added['manifest'][0])
        extra.update(path='train_images/bacterial_leaf_blight/extra.jpg',
                     sha256='e' * 64, group='leaf:extra')
        added['manifest'].append(extra)
        reordered = deepcopy(self.baseline)
        reordered['labels'].reverse()
        for name, candidate in [('removed', removed), ('added', added), ('labels', reordered)]:
            with self.subTest(change=name), self.assertRaises(ValueError):
                validate_reference(self.baseline, candidate)

    def test_unsafe_noncanonical_and_duplicate_paths_are_rejected(self):
        paths = ['../outside.jpg', '/absolute.jpg', 'D:/outside.jpg',
                 'train_images\\normal\\leaf.jpg', './train_images/normal/leaf.jpg',
                 'train_images//normal/leaf.jpg', self.baseline['manifest'][1]['path']]
        for path in paths:
            with self.subTest(path=path):
                candidate = deepcopy(self.baseline)
                candidate['manifest'][0]['path'] = path
                with self.assertRaises(ValueError):
                    manifest_identity(candidate)

    def test_hash_and_physical_leaf_cannot_cross_label_or_split(self):
        source = self.baseline['manifest'][0]
        for target_index in (5, 26):  # Another train class, then first validation image.
            for field in ('sha256', 'group'):
                with self.subTest(target=target_index, field=field):
                    candidate = deepcopy(self.baseline)
                    candidate['manifest'][target_index][field] = source[field]
                    with self.assertRaisesRegex(ValueError, 'Cross-label or cross-split'):
                        manifest_identity(candidate)
        for invalid in ('not-a-sha256', 'A' * 64, ''):
            candidate = deepcopy(self.baseline)
            candidate['manifest'][0]['sha256'] = invalid
            with self.subTest(hash=invalid), self.assertRaisesRegex(ValueError, 'Invalid SHA256'):
                manifest_identity(candidate)

    def test_bad_labels_splits_groups_and_missing_record_fields_are_rejected(self):
        for field, value in [('label', 'unrecognized_disease'), ('split', 'holdout'),
                             ('group', ''), ('group', None)]:
            candidate = deepcopy(self.baseline)
            candidate['manifest'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                manifest_identity(candidate)
        candidate = deepcopy(self.baseline)
        del candidate['manifest'][0]['sha256']
        with self.assertRaisesRegex(ValueError, 'Every manifest record'):
            manifest_identity(candidate)

    def test_declared_counts_are_checked_for_baseline_and_candidate(self):
        mutations = [
            ('train_images', lambda c: c['metadata'].__setitem__('train_images', 999)),
            ('validation_images', lambda c: c['metadata'].__setitem__('validation_images', 999)),
            ('test_images', lambda c: c['metadata'].__setitem__('test_images', 999)),
            ('classes', lambda c: c['metadata'].__setitem__('classes', 99)),
            ('images', lambda c: c['metadata']['split_info'].__setitem__('images', 999)),
            ('class_counts', lambda c: c['metadata']['split_info']['counts']['train'].__setitem__('normal', 999)),
            ('split_labels', lambda c: c['metadata']['split_info']['labels'].reverse()),
        ]
        for side in ('baseline', 'candidate'):
            for name, mutate in mutations:
                with self.subTest(side=side, change=name):
                    baseline, candidate = deepcopy(self.baseline), deepcopy(self.baseline)
                    mutate(baseline if side == 'baseline' else candidate)
                    with self.assertRaisesRegex(ValueError, 'conflicts'):
                        validate_reference(baseline, candidate)

    def test_preprocessing_and_declared_split_provenance_cannot_change(self):
        candidate = deepcopy(self.baseline)
        candidate['preprocessing'] = 'center_crop_instead_of_bilinear_stretch'
        with self.assertRaisesRegex(ValueError, 'preprocessing identity'):
            validate_reference(self.baseline, candidate)
        changes = dict(profile='another-dataset', grouping='random-filenames',
                       physical_leaf_ids_available=False, seed=7)
        for field, value in changes.items():
            candidate = deepcopy(self.baseline)
            candidate['metadata']['split_info'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'differs from baseline'):
                validate_reference(self.baseline, candidate)


class EvaluationMetricsTests(unittest.TestCase):
    def test_confusion_metrics_use_actual_rows_and_prediction_columns(self):
        labels = ['bacterial_leaf_blight', 'blast', 'normal']
        metrics = classification_metrics([[8, 2, 0], [1, 3, 1], [0, 2, 3]], labels, 20)
        self.assertEqual((metrics['images'], metrics['correct']), (20, 14))
        self.assertAlmostEqual(metrics['accuracy'], .7)
        self.assertEqual(metrics['top3_accuracy'], 1.0)
        self.assertAlmostEqual(metrics['balanced_accuracy'], 2 / 3)
        first = metrics['per_class'][labels[0]]
        self.assertEqual((first['support'], first['predicted'], first['correct']), (10, 9, 8))
        self.assertAlmostEqual(first['precision'], 8 / 9)
        self.assertAlmostEqual(first['recall'], .8)
        self.assertEqual(metrics['confusion_matrix']['rows'], 'actual')
        self.assertEqual(sum(pair['count'] for pair in metrics['confusion_pairs']), 6)

    def test_invalid_confusion_counts_and_top3_totals_are_rejected(self):
        cases = [([[1]], ['a', 'b'], 1), ([[0]], ['a'], 0),
                 ([[-1]], ['a'], 0), ([[True]], ['a'], 1),
                 ([[1.0]], ['a'], 1), ([[1]], ['a'], 2)]
        for matrix, labels, top3 in cases:
            with self.subTest(matrix=matrix, top3=top3), self.assertRaises(ValueError):
                classification_metrics(matrix, labels, top3)

    def test_validation_accuracy_is_never_reported_as_declared_test_accuracy(self):
        candidate = frozen_config()
        candidate['metadata'].update(accuracy=.84, evaluation_split='validation', deployment_ready=False)
        identity = declared_accuracy_identity(candidate)
        self.assertEqual(identity['declared_accuracy'], .84)
        self.assertEqual(identity['declared_evaluation_split'], 'validation')
        self.assertIsNone(identity['declared_test_accuracy'])
        legacy = declared_accuracy_identity(frozen_config())
        self.assertEqual(legacy['declared_evaluation_split'], 'test')
        self.assertEqual(legacy['declared_test_accuracy'], .75)

    def test_protocol_helpers_can_import_with_heavy_dependencies_blocked(self):
        code = '''
import importlib.abc
import sys
class BlockHeavy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'tensorflow', 'ai_edge_litert', 'sklearn', 'PIL'}:
            raise AssertionError('Unexpected heavy import: ' + fullname)
sys.meta_path.insert(0, BlockHeavy())
import optimize_paddy
import evaluate_paddy_candidate
assert not any(name in sys.modules for name in ('numpy', 'tensorflow', 'ai_edge_litert', 'sklearn', 'PIL'))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
