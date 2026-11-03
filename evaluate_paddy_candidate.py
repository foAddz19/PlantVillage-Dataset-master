"""Evaluate a Paddy LiteRT candidate against a frozen, hash-verified image split.

Use validation to select candidates; reserve test for the final selected model.
This helper never trains a model or imports TensorFlow/sklearn. Run it in a fresh
process for comparable process-memory measurements. Thread environment limits are
set before importing NumPy/LiteRT; native LiteRT scheduling can vary by build.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import statistics
import tempfile
import time


IDENTITY_FIELDS = ('path', 'sha256', 'group', 'label', 'split')
SPLITS = ('train', 'validation', 'test')


def manifest_identity(config):
    """Validate and canonicalize paths/hashes/groups/labels/splits, not row order."""
    labels = config.get('labels')
    if (not isinstance(labels, list) or not labels or
            any(not isinstance(label, str) or not label for label in labels) or
            len(labels) != len(set(labels))):
        raise ValueError('Model labels must be a nonempty, unique ordered list')
    manifest = config.get('manifest')
    if not isinstance(manifest, list) or not manifest:
        raise ValueError('A frozen, nonempty split manifest is required')
    rows, seen_paths, hash_groups, groups = [], set(), {}, {}
    for record in manifest:
        if not isinstance(record, dict) or any(key not in record for key in IDENTITY_FIELDS):
            raise ValueError('Every manifest record needs path/sha256/group/label/split')
        row = {key: record[key] for key in IDENTITY_FIELDS}
        path = row['path']
        if (not isinstance(path, str) or not path or '\\' in path or ':' in path or
                PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or
                path != PurePosixPath(path).as_posix()):
            raise ValueError(f'Unsafe or noncanonical manifest path: {path!r}')
        if path in seen_paths:
            raise ValueError(f'Duplicate manifest path: {path}')
        seen_paths.add(path)
        digest = row['sha256']
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError(f'Invalid SHA256 for {path}')
        if row['label'] not in labels or row['split'] not in SPLITS:
            raise ValueError(f'Unknown label/split for {path}')
        if not isinstance(row['group'], str) or not row['group']:
            raise ValueError(f'Missing image group for {path}')
        identity = (row['label'], row['split'])
        for collection, key in ((hash_groups, digest), (groups, row['group'])):
            if key in collection and collection[key] != identity:
                raise ValueError(f'Cross-label or cross-split hash/group: {path}')
            collection[key] = identity
        rows.append(row)
    rows.sort(key=lambda row: row['path'])
    for split in SPLITS:
        if not any(row['split'] == split for row in rows):
            raise ValueError(f'Frozen manifest has no {split} records')
    return rows


def manifest_sha256(rows):
    payload = json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def validate_reference(baseline, candidate):
    """Reject changed samples or declared dataset identities before inference."""
    baseline_rows = manifest_identity(baseline)
    candidate_rows = manifest_identity(candidate)
    if baseline['labels'] != candidate['labels']:
        raise ValueError('Candidate label order differs from the frozen baseline')
    if baseline.get('preprocessing') != candidate.get('preprocessing'):
        raise ValueError('Candidate preprocessing identity differs from baseline')
    if baseline_rows != candidate_rows:
        original = {row['path']: row for row in baseline_rows}
        proposed = {row['path']: row for row in candidate_rows}
        changed = sorted(path for path in original.keys() | proposed.keys()
                         if original.get(path) != proposed.get(path))
        raise ValueError('Candidate changed frozen manifest identities: ' + ', '.join(changed[:5]))
    counts = {split: dict(sorted(Counter(row['label'] for row in baseline_rows
                                        if row['split'] == split).items())) for split in SPLITS}
    reference_identity = {}
    for name, config in (('baseline', baseline), ('candidate', candidate)):
        metadata = config.get('metadata', {})
        if not isinstance(metadata, dict):
            raise ValueError(f'{name} metadata must be an object')
        info = metadata.get('split_info', {})
        if not isinstance(info, dict):
            raise ValueError(f'{name} split_info must be an object')
        expected = dict(images=len(baseline_rows), labels=baseline['labels'], counts=counts)
        for key, value in expected.items():
            if key in info and info[key] != value:
                raise ValueError(f'{name} split_info.{key} conflicts with frozen manifest')
        for split in SPLITS:
            key = split + '_images'
            if key in metadata and metadata[key] != sum(counts[split].values()):
                raise ValueError(f'{name} metadata.{key} conflicts with frozen manifest')
        if 'classes' in metadata and metadata['classes'] != len(baseline['labels']):
            raise ValueError(f'{name} metadata.classes conflicts with label order')
        for key in ('profile', 'grouping', 'physical_leaf_ids_available', 'seed'):
            if key in info:
                reference_identity.setdefault(key, {})[name] = info[key]
    for key, values in reference_identity.items():
        if 'baseline' in values and values.get('candidate') != values['baseline']:
            raise ValueError(f'Candidate split_info.{key} differs from baseline')
    return baseline_rows, dict(manifest_sha256=manifest_sha256(baseline_rows),
                              records=len(baseline_rows), counts=counts,
                              dataset_identity={key: values.get('baseline')
                                                for key, values in reference_identity.items()})


def classification_metrics(confusion, labels, top3_correct):
    """Rows are truth, columns predictions; fractions remain unrounded."""
    if len(confusion) != len(labels) or any(len(row) != len(labels) for row in confusion):
        raise ValueError('Confusion matrix shape does not match labels')
    if any(type(value) is not int or value < 0 for row in confusion for value in row):
        raise ValueError('Confusion matrix must contain nonnegative integer counts')
    total = sum(map(sum, confusion))
    if not total or not 0 <= top3_correct <= total:
        raise ValueError('Metrics require images and a valid top-3 count')
    report = {}
    pairs = []
    for index, label in enumerate(labels):
        tp = confusion[index][index]
        support = sum(confusion[index])
        predicted = sum(row[index] for row in confusion)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        report[label] = dict(precision=precision, recall=recall, f1=f1,
                             support=support, predicted=predicted, correct=tp)
        for other, count in enumerate(confusion[index]):
            if other != index and count:
                pairs.append(dict(actual=label, predicted=labels[other], count=count,
                                  fraction_of_actual=count / support))
    pairs.sort(key=lambda pair: (-pair['count'], pair['actual'], pair['predicted']))
    supported = [entry for entry in report.values() if entry['support']]
    return dict(images=total, correct=sum(confusion[i][i] for i in range(len(labels))),
                accuracy=sum(confusion[i][i] for i in range(len(labels))) / total,
                top3_accuracy=top3_correct / total,
                balanced_accuracy=statistics.mean(entry['recall'] for entry in supported),
                macro_f1=statistics.mean(entry['f1'] for entry in supported),
                supported_classes=len(supported), per_class=report,
                confusion_matrix=dict(labels=labels, rows='actual', columns='predicted', counts=confusion),
                confusion_pairs=pairs)


def _read_config(directory):
    path = directory / 'model.json'
    payload = path.read_bytes()
    value = json.loads(payload.decode('utf-8'))
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError(f'Unsupported model metadata: {path}')
    return value, hashlib.sha256(payload).hexdigest()


def declared_accuracy_identity(config):
    """Legacy metadata reports test; optimization candidates explicitly report validation."""
    metadata = config.get('metadata', {})
    split = metadata.get('evaluation_split', 'test')
    accuracy = metadata.get('accuracy')
    return dict(declared_accuracy=accuracy, declared_evaluation_split=split,
                declared_test_accuracy=accuracy if split == 'test' else None)


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path, required=True,
                        help='Frozen original model directory; never use a moving experiment as reference')
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--dataset-dir', type=Path, default=root / 'datasets' / 'paddy-competition')
    parser.add_argument('--split', choices=('validation', 'test'), default='validation')
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--threads', type=int, choices=(1, 2, 3, 4), default=2,
                        help='Native math thread environment limit, set before NumPy import')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    baseline_dir, model_dir = args.baseline_dir.resolve(), args.model_dir.resolve()
    dataset_dir, output = args.dataset_dir.resolve(), args.output.resolve()
    if not dataset_dir.is_dir():
        parser.error('--dataset-dir must be an existing directory')
    # Never let a report replace a model or any source image.
    if output.suffix.lower() != '.json' or output.name.lower() == 'model.json':
        parser.error('--output must be a JSON report path other than model.json')
    if output.is_relative_to(dataset_dir) or output.is_relative_to(baseline_dir):
        parser.error('--output must be outside the dataset and frozen baseline directories')
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS',
                'TF_NUM_INTRAOP_THREADS', 'TF_NUM_INTEROP_THREADS'):
        os.environ[key] = str(args.threads)

    # These imports can load NumPy/LiteRT, so keep them after the environment setup.
    from benchmark_paddy import memory_mib
    from leaflab.images import read_image
    from leaflab.paddy import PaddyService

    baseline, baseline_config_sha = _read_config(baseline_dir)
    candidate, candidate_config_sha = _read_config(model_dir)
    rows, frozen = validate_reference(baseline, candidate)
    selected = [row for row in rows if row['split'] == args.split]
    labels = candidate['labels']
    label_index = {label: index for index, label in enumerate(labels)}
    memory_before = memory_mib()
    started = time.perf_counter()
    service = PaddyService(model_dir=model_dir)
    if not service.status()['ready']:
        service.close()
        raise ValueError(service.message)
    try:
        if service.config != candidate:
            raise ValueError('Candidate model.json changed while it was being loaded')
        matrix = [[0 for _ in labels] for _ in labels]
        top3_correct = 0
        first_image = None
        for position, row in enumerate(selected, 1):
            image_path = (dataset_dir / row['path']).resolve()
            if not image_path.is_relative_to(dataset_dir):
                raise ValueError(f'Image resolves outside dataset: {row["path"]}')
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if digest != row['sha256']:
                raise ValueError(f'Image SHA256 differs from frozen manifest: {row["path"]}')
            image = read_image(image_path)
            if first_image is None:
                first_image = image.copy()
            prediction = service.predict(image)
            predicted = [item['label'].removeprefix('Rice___') for item in prediction['candidates']]
            matrix[label_index[row['label']]][label_index[predicted[0]]] += 1
            top3_correct += row['label'] in predicted
            if position % 250 == 0 or position == len(selected):
                print(f'{args.split}: {position}/{len(selected)}', flush=True)
        metrics = classification_metrics(matrix, labels, top3_correct)
        # Decode outside timing and warm the exact API used by the application.
        service.predict(first_image)
        timings = []
        for _ in range(args.iterations):
            tick = time.perf_counter()
            service.predict(first_image)
            timings.append((time.perf_counter() - tick) * 1000)
        sorted_timings = sorted(timings)
        metadata = candidate.get('metadata', {})
        report = dict(schema_version=1, evaluated_at=datetime.now(timezone.utc).isoformat(),
            split=args.split, platform=platform.platform(), python=platform.python_version(),
            baseline=dict(directory=str(baseline_dir), config_sha256=baseline_config_sha,
                          model_sha256=baseline.get('sha256'),
                          **declared_accuracy_identity(baseline)),
            candidate=dict(directory=str(model_dir), config_sha256=candidate_config_sha,
                           model_sha256=service.config['sha256'],
                           model_bytes=(model_dir / service.config['model_file']).stat().st_size,
                           input=service.config['input'], output=service.config['output'],
                           **declared_accuracy_identity(candidate)),
            frozen_reference={**frozen, 'selected_image_hashes_verified': len(selected)},
            metrics=metrics,
            standalone=dict(iterations=args.iterations, thread_environment_limit=args.threads,
                median_inference_ms=statistics.median(timings),
                p95_inference_ms=sorted_timings[max(0, math.ceil(.95 * len(timings)) - 1)],
                max_inference_ms=max(timings), process_memory_before_mib=memory_before,
                process_memory_mib=memory_mib(),
                scope='Fresh standalone process recommended. Latency includes RGB resize, quantization, '
                      'LiteRT inference and prediction formatting on one decoded image. Excludes image '
                      'decoding, camera, Flask, browser and OS. Process peak includes evaluation and manifest loading. '
                      'Thread environment limits do not guarantee LiteRT worker count.'),
            elapsed_seconds=time.perf_counter() - started,
            interpretation='Validation is for model selection. Test is for final confirmation; '
                           'repeated test-guided tuning invalidates an untouched-test claim. '
                           'Image-hash groups do not establish physical-leaf or field independence.')
        if (args.split == 'test' and report['candidate']['declared_evaluation_split'] == 'test'
                and isinstance(metadata.get('accuracy'), (float, int))
                and not isinstance(metadata.get('accuracy'), bool)):
            report['candidate']['declared_test_accuracy_delta'] = metrics['accuracy'] - metadata['accuracy']
        _atomic_json(output, report)
        print(json.dumps(dict(output=str(output), split=args.split, images=metrics['images'],
                              accuracy=metrics['accuracy'], balanced_accuracy=metrics['balanced_accuracy'],
                              macro_f1=metrics['macro_f1'],
                              median_inference_ms=report['standalone']['median_inference_ms'],
                              process_memory_mib=report['standalone']['process_memory_mib']),
                         ensure_ascii=False, indent=2), flush=True)
    finally:
        service.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f'Evaluation failed: {error}') from error
