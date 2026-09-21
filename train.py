"""Train the same model used by the web application."""
import argparse
import json

from leaflab.dataset import Catalog
from leaflab.model import train_model

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples-per-class", type=int, choices=(150, 300, 600), default=300)
    args = parser.parse_args()
    artifact = train_model(Catalog(), max_per_class=args.samples_per_class,
                           progress=lambda state: print(json.dumps(state, ensure_ascii=True), flush=True))
    metrics = artifact["metadata"]
    print(json.dumps({key: metrics[key] for key in ("accuracy", "top3_accuracy", "train_images", "test_images", "seconds")}, indent=2))
