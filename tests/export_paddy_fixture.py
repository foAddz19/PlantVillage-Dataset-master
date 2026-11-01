"""Manual integration fixture: synthetic tensors, never a rice diagnostic model.

Run with .venv-training, with an explicit temporary output directory.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('TF_NUM_INTRAOP_THREADS', '2')
os.environ.setdefault('TF_NUM_INTEROP_THREADS', '2')
import numpy as np
import tensorflow as tf
from train_paddy import export_int8
from leaflab.paddy_labels import COMPETITION_LABELS

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
assert 'models' not in args.output.parts, 'Synthetic fixtures must never be published as real models'
tf.keras.utils.set_random_seed(42)
inp = tf.keras.Input((32, 32, 3))
x = tf.keras.layers.Rescaling(1 / 127.5, offset=-1)(inp)
x = tf.keras.layers.GlobalAveragePooling2D()(x)
model = tf.keras.Model(inp, tf.keras.layers.Dense(10, activation='softmax')(x))
samples = np.random.default_rng(42).uniform(0, 255, (20, 32, 32, 3)).astype(np.float32)
def representative():
    for sample in samples: yield [sample[None]]
export_int8(model, representative, args.output,
    {'algorithm':'SYNTHETIC TEST FIXTURE', 'created_at':'fixture', 'train_images':0, 'test_images':10},
    [], lambda: iter(samples[:10]), list(range(10)), COMPETITION_LABELS)
print('Synthetic fixture exported:', args.output)
