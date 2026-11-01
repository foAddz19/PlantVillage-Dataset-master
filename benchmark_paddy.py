"""Measure this machine's Paddy LiteRT latency/RAM; optionally verify held-out metrics."""
import argparse
import json
import os
import platform
import statistics
import time
from pathlib import Path

from leaflab.dataset import ROOT
from leaflab.images import read_image
from leaflab.paddy import PaddyService


def memory_mib():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage',
                'PeakPagefileUsage', 'PrivateUsage')]
        info = Counters()
        info.cb = ctypes.sizeof(info)
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(info), info.cb):
            return None
        return dict(current=round(info.WorkingSetSize / 2**20, 2), peak=round(info.PeakWorkingSetSize / 2**20, 2))
    if Path('/proc/self/status').exists():
        values = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        return dict(current=round(int(values['VmRSS'].split()[0]) / 1024, 2), peak=round(int(values['VmHWM'].split()[0]) / 1024, 2))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, default=ROOT / 'models' / 'paddy')
    parser.add_argument('--dataset-dir', type=Path, default=ROOT / 'datasets' / 'paddy-competition')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--evaluate', action='store_true')
    args = parser.parse_args()
    if args.iterations < 1: parser.error('--iterations must be positive')
    service = PaddyService(model_dir=args.model_dir)
    if not service.status()['ready']: parser.exit(2, service.message + '\n')
    try:
        records = [r for r in service.artifact['manifest'] if r['split'] == 'test']
        image_path = args.image or (args.dataset_dir / records[0]['path'] if records else None)
        if image_path is None: parser.error('Provide --image or a model with a held-out manifest')
        image = read_image(image_path)
        service.predict(image)
        timings = []
        for _ in range(args.iterations):
            start = time.perf_counter()
            service.predict(image)
            timings.append((time.perf_counter() - start) * 1000)
        result = dict(platform=platform.platform(), python=platform.python_version(), model_sha256=service.config['sha256'],
            model_bytes=service.artifact['metadata'].get('model_bytes'), input=service.config['input'],
            iterations=args.iterations, median_inference_ms=round(statistics.median(timings), 2),
            max_inference_ms=round(max(timings), 2), process_memory_mib=memory_mib(),
            scope='Standalone Python process; includes RGB resize/quantization and inference; excludes decoding, camera, Flask, browser and OS memory')
        if args.evaluate:
            if not records: parser.error('The model has no held-out test manifest')
            correct = top3 = 0
            for row in records:
                prediction = service.predict(read_image(args.dataset_dir / row['path']))
                labels = [c['label'].removeprefix('Rice___') for c in prediction['candidates']]
                correct += labels[0] == row['label']
                top3 += row['label'] in labels
            result['held_out'] = dict(images=len(records), accuracy=correct / len(records), top3_accuracy=top3 / len(records),
                                     exported_interpreter_accuracy=service.artifact['metadata'].get('accuracy'))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        service.close()


if __name__ == '__main__': main()
