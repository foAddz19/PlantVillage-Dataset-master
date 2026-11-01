"""Train on a PC and export a self-describing, fully integer Paddy Doctor model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from leaflab.dataset import ROOT
from leaflab.images import read_image
from leaflab.paddy_data import prepare_splits
from leaflab.paddy_labels import LABELS, rice_info


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def evaluate_probabilities(probabilities, truth, labels=LABELS):
    import numpy as np
    probabilities = np.asarray(probabilities)
    truth = np.asarray(truth)
    predicted = probabilities.argmax(axis=1)
    report = {}
    for index, label in enumerate(labels):
        tp = int(((predicted == index) & (truth == index)).sum())
        precision = tp / max(1, int((predicted == index).sum()))
        recall = tp / max(1, int((truth == index).sum()))
        report['Rice___' + label] = dict(precision=precision, recall=recall,
            **{'f1-score': 2 * precision * recall / max(1e-12, precision + recall)},
            support=int((truth == index).sum()))
    return dict(accuracy=float((predicted == truth).mean()),
                top3_accuracy=float(np.any(np.argsort(-probabilities, axis=1, kind='stable')[:, :3] == truth[:, None], axis=1).mean()),
                balanced_accuracy=float(np.mean([r['recall'] for r in report.values()])), report=report)


def export_int8(model, representative, output, metadata, manifest, test_pixels, truth, labels=LABELS):
    import numpy as np
    import tensorflow as tf
    from ai_edge_litert.interpreter import Interpreter

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.uint8
    converter.inference_output_type = tf.uint8
    payload = converter.convert()
    digest = hashlib.sha256(payload).hexdigest()
    interpreter = Interpreter(model_content=payload, num_threads=2)
    interpreter.allocate_tensors()
    inp, out = interpreter.get_input_details()[0], interpreter.get_output_details()[0]
    def describe(detail):
        scale, zero = detail['quantization']
        return dict(shape=detail['shape'].tolist(), dtype=np.dtype(detail['dtype']).name,
                    scale=float(scale), zero_point=int(zero))
    probabilities = []
    scale, zero = inp['quantization']
    for pixels in test_pixels():
        quantized = np.clip(np.rint(pixels / scale + zero), 0, 255).astype(np.uint8)[None]
        interpreter.set_tensor(inp['index'], quantized)
        interpreter.invoke()
        values = interpreter.get_tensor(out['index'])[0].astype(np.float32)
        probabilities.append((values - out['quantization'][1]) * out['quantization'][0])
    metadata = {**metadata, **evaluate_probabilities(probabilities, truth, labels), 'evaluation_model': 'exported_int8',
                'model_bytes': len(payload), 'real_camera_validation': False, 'confidence_calibrated': False}
    config = dict(schema_version=1, model_file=f'paddy-{digest[:16]}.tflite', sha256=digest,
                  labels=list(labels), preprocessing='rgb_0_255_bilinear_stretch_rescaling_in_model',
                  input=describe(inp), output=describe(out), metadata=metadata, manifest=manifest)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Publish config last so an interrupted export cannot replace a working model.
    with tempfile.NamedTemporaryFile(dir=output, suffix='.tmp', delete=False) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output / config['model_file'])
    atomic_json(output / 'model.json', config)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'models' / 'paddy')
    parser.add_argument('--check-data', action='store_true', help='Validate real images and splits; do not train')
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--fine-tune-epochs', type=int, default=0)
    parser.add_argument('--cache-features', action=argparse.BooleanOptionalAction, default=True,
                        help='Compute frozen embeddings once for faster CPU training')
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--image-size', type=int, default=224, choices=(96, 128, 160, 192, 224))
    parser.add_argument('--alpha', type=float, default=.5, choices=(.35, .5, .75, 1.))
    parser.add_argument('--weights', choices=('imagenet', 'none'), default='imagenet')
    parser.add_argument('--representative-images', type=int, default=128)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.epochs < 1 or args.fine_tune_epochs < 0 or args.batch_size < 1 or args.representative_images < 1:
        parser.error('epochs/batch-size/representative-images ต้องเป็นบวก')
    try:
        records, info = prepare_splits(args.dataset, args.seed)
        # Fail before training/export, with the offending filename, rather than silently dropping data.
        for row in records:
            try:
                read_image(args.dataset / row['path'])
            except ValueError as exc:
                raise ValueError(row['path'] + ': ' + str(exc)) from exc
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    if args.check_data:
        return
    labels = tuple(info['labels'])

    os.environ.setdefault('TF_NUM_INTRAOP_THREADS', '4')
    os.environ.setdefault('TF_NUM_INTEROP_THREADS', '2')
    import numpy as np
    import tensorflow as tf
    from PIL import Image

    tf.keras.utils.set_random_seed(args.seed)
    started = time.time()
    subsets = {split: [r for r in records if r['split'] == split] for split in ('train', 'validation', 'test')}
    def pixels(row):
        return np.asarray(read_image(args.dataset / row['path']).resize((args.image_size, args.image_size), Image.Resampling.BILINEAR), dtype=np.float32)
    def dataset(rows, training=False):
        rng = np.random.default_rng(args.seed)
        def generate():
            order = np.arange(len(rows))
            if training:
                rng.shuffle(order)
            for index in order:
                row = rows[index]
                yield pixels(row), labels.index(row['label'])
        ds = tf.data.Dataset.from_generator(generate, output_signature=(
            tf.TensorSpec((args.image_size, args.image_size, 3), tf.float32), tf.TensorSpec((), tf.int32)))
        return ds.apply(tf.data.experimental.assert_cardinality(len(rows))).batch(args.batch_size).prefetch(1)
    train_ds, validation_ds, test_ds = dataset(subsets['train'], True), dataset(subsets['validation']), dataset(subsets['test'])
    backbone = tf.keras.applications.MobileNetV2(input_shape=(args.image_size, args.image_size, 3),
        alpha=args.alpha, include_top=False, weights=None if args.weights == 'none' else 'imagenet', pooling='avg')
    backbone.trainable = False
    inputs = tf.keras.Input((args.image_size, args.image_size, 3), name='rgb_pixels')
    # Augmentation lives in the training dataset; inference model has no random layers.
    augmentation = tf.keras.Sequential([tf.keras.layers.RandomFlip('horizontal'),
        tf.keras.layers.RandomRotation(.06, fill_mode='reflect'), tf.keras.layers.RandomContrast(.12)])
    augmented_train = train_ds.map(lambda x, y: (augmentation(x, training=True), y), num_parallel_calls=1).prefetch(1)
    x = tf.keras.layers.Rescaling(1 / 127.5, offset=-1)(inputs)
    x = backbone(x, training=False)
    encoder = tf.keras.Model(inputs, x)
    head = tf.keras.Sequential([tf.keras.Input((int(x.shape[-1]),)), tf.keras.layers.Dropout(.2),
                                tf.keras.layers.Dense(len(labels), activation='softmax')])
    outputs = head(x)
    model = tf.keras.Model(inputs, outputs)
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss='sparse_categorical_crossentropy', metrics=['accuracy'])
    args.output.mkdir(parents=True, exist_ok=True)
    # A unique training folder avoids overwriting an active model until all checks pass.
    with tempfile.TemporaryDirectory(prefix='training-', dir=args.output) as temporary:
        checkpoint = str(Path(temporary) / 'best.weights.h5')
        callbacks = [tf.keras.callbacks.ModelCheckpoint(checkpoint, monitor='val_loss', save_best_only=True, save_weights_only=True),
                     tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=3, restore_best_weights=True)]
        if args.cache_features:
            print('Computing frozen training embeddings (original + augmented view)...', flush=True)
            original = encoder.predict(dataset(subsets['train']).map(lambda x, y: x), verbose=2)
            augmented = encoder.predict(dataset(subsets['train']).map(lambda x, y: augmentation(x, training=True)), verbose=2)
            validation_vectors = encoder.predict(validation_ds.map(lambda x, y: x), verbose=2)
            y_train = np.asarray([labels.index(r['label']) for r in subsets['train']], dtype=np.int32)
            y_validation = np.asarray([labels.index(r['label']) for r in subsets['validation']], dtype=np.int32)
            head.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss='sparse_categorical_crossentropy', metrics=['accuracy'])
            head.fit(np.concatenate([original, augmented]), np.tile(y_train, 2), batch_size=64,
                     validation_data=(validation_vectors, y_validation), epochs=args.epochs, verbose=2,
                     callbacks=[tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=3, restore_best_weights=True)])
            del original, augmented, validation_vectors
            model.save_weights(checkpoint)
        else:
            model.fit(augmented_train, validation_data=validation_ds, epochs=args.epochs, callbacks=callbacks)
            model.load_weights(checkpoint)
        if args.fine_tune_epochs:
            backbone.trainable = True
            for layer in backbone.layers[:-20]: layer.trainable = False
            for layer in backbone.layers:
                if isinstance(layer, tf.keras.layers.BatchNormalization): layer.trainable = False
            model.compile(optimizer=tf.keras.optimizers.Adam(1e-5), loss='sparse_categorical_crossentropy', metrics=['accuracy'])
            baseline_loss = float(model.evaluate(validation_ds, verbose=0)[0])
            fine_callbacks = [tf.keras.callbacks.ModelCheckpoint(checkpoint, monitor='val_loss', save_best_only=True,
                save_weights_only=True, initial_value_threshold=baseline_loss),
                tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=3, restore_best_weights=True)]
            model.fit(augmented_train, validation_data=validation_ds, epochs=args.fine_tune_epochs, callbacks=fine_callbacks, verbose=2)
            model.load_weights(checkpoint)
        truth = [labels.index(r['label']) for r in subsets['test']]
        float_metrics = evaluate_probabilities(model.predict(test_ds, verbose=0), truth, labels)
        def representative():
            # Calibration uses training data only, never held-out test images.
            calibration = list(subsets['train'])
            np.random.default_rng(args.seed).shuffle(calibration)
            for row in calibration[:args.representative_images]:
                yield [pixels(row)[None]]
        metadata = dict(algorithm=f'MobileNetV2 alpha={args.alpha} + global average pooling · INT8',
            created_at=datetime.now(timezone.utc).isoformat(), seconds=round(time.time() - started, 1),
            train_images=len(subsets['train']), validation_images=len(subsets['validation']), test_images=len(truth),
            classes=len(labels), split_info=info, float_metrics=float_metrics, pretrained_weights=args.weights,
            group_overlap=0, seed=args.seed, parameter_count=model.count_params(),
            class_labels=[rice_info(label) for label in labels])
        config = export_int8(model, representative, args.output, metadata, records,
                             lambda: (pixels(r) for r in subsets['test']), truth, labels)
        model.save(args.output / f'paddy-{config["sha256"][:16]}.keras')
    print('Exported:', args.output / 'model.json', flush=True)
    print('INT8 held-out accuracy:', config['metadata']['accuracy'], flush=True)


if __name__ == '__main__':
    main()
