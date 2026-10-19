"""Read-only CPU background-removal benchmark against an existing frame.

Run from backend/ using the service virtualenv. Stop production processing first;
run inside a memory-limited cgroup to contain an out-of-memory benchmark.
The script never saves output images or changes production session metadata.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_OUTPUT_LOCK = threading.Lock()


def emit(event: str, **fields) -> None:
    with _OUTPUT_LOCK:
        print(json.dumps({"event": event, "time": time.time(), **fields}), flush=True)


def memory_kib() -> dict:
    result = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "VmHWM", "VmSwap"}:
            result[key] = int(value.split()[0])
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            result["available"] = int(line.split()[1])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model", default="bria-rmbg-2.0")
    parser.add_argument("--workers", default="1,2")
    parser.add_argument("--samples-per-worker", type=int, default=2)
    args = parser.parse_args()
    workers = [int(value) for value in args.workers.split(",")]
    if not workers or min(workers) < 1 or args.samples_per_worker < 1:
        parser.error("workers and samples-per-worker must be positive")
    if not args.image.is_file():
        parser.error("image must be an existing file")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    os.environ["SPRITE_FORCE_CPU"] = "true"
    os.environ["SPRITE_WARMUP_MODEL"] = "false"

    from PIL import Image
    import numpy as np
    from app.core.background_remover import BackgroundRemover

    image = np.array(Image.open(args.image).convert("RGB"))
    emit("start", image_shape=image.shape, model=args.model,
         cpus=os.cpu_count(), memory_kib=memory_kib())
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(5):
            emit("memory", memory_kib=memory_kib())

    threading.Thread(target=monitor, daemon=True).start()
    try:
        remover = BackgroundRemover()
        started = time.perf_counter()
        emit("load_start")
        remover._init_rembg(args.model, force_cpu=True)
        emit("load_done", seconds=time.perf_counter() - started,
             memory_kib=memory_kib())

        # Keep the cached model shared across workers, matching production.
        for count in workers:
            barrier = threading.Barrier(count)

            def work(worker: int) -> list[float]:
                local = BackgroundRemover()
                durations = []
                barrier.wait()
                for index in range(args.samples_per_worker):
                    began = time.perf_counter()
                    result = local._remove_ai(image, {"model": args.model,
                                                     "force_cpu": True})
                    assert result.shape == (*image.shape[:2], 4)
                    assert result.dtype == np.uint8
                    duration = time.perf_counter() - began
                    durations.append(duration)
                    del result
                    emit("frame", workers=count, worker=worker, index=index,
                         seconds=duration, memory_kib=memory_kib())
                return durations

            emit("stage_start", workers=count, memory_kib=memory_kib())
            before = time.process_time()
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(work, worker) for worker in range(count)]
                durations = [value for future in futures for value in future.result()]
            elapsed = time.perf_counter() - started
            emit("stage_done", workers=count, frames=len(durations),
                 wall_seconds=elapsed, cpu_seconds=time.process_time() - before,
                 frames_per_minute=len(durations) * 60 / elapsed,
                 frame_seconds=durations, memory_kib=memory_kib(),
                 process_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
