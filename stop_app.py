"""Stop only the Leaf Lab process identifying itself on the selected local port."""
import argparse
import json
import os
import signal
from urllib.request import urlopen

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        with urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=3) as response:
            info = json.load(response)
        if info.get("app") != "leaf-lab" or type(info.get("pid")) is not int:
            raise ValueError("This port is not running Leaf Lab.")
        with urlopen(f"http://127.0.0.1:{args.port}/api/status", timeout=3) as response:
            if json.load(response).get("running"):
                raise ValueError("Model training is still running. Wait for it to finish before stopping.")
        os.kill(info["pid"], signal.SIGTERM)
        print("Leaf Lab stopped.")
    except OSError:
        print("Leaf Lab is not running, or could not be stopped.")
    except ValueError as exc:
        print(exc)
