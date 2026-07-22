"""
Hammertime | RPINerd, 07/22/26

Simulate sustained, fluctuating heavy file I/O load on a NAS.

Spawns a configurable number of concurrent worker threads that continuously open, write random data,
and close files to stress-test a filesystem or network-attached storage connection.
"""

import argparse
import logging
import os
import random
import signal
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Global stop event so all threads can exit cleanly on SIGINT/SIGTERM.
_stop_event = threading.Event()


def _install_signal_handlers() -> None:
    """Register SIGINT and SIGTERM to set the stop event."""

    def _handler(signum: int, frame) -> None:
        logger.info("Signal %d received — stopping workers…", signum)
        _stop_event.set()

    # Only register once; subsequent Ctrl+C presses are intentionally ignored
    # because we will force-exit via os._exit() after the grace period.
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _worker(
    worker_id: int,
    target_dir: Path,
    min_file_bytes: int,
    max_file_bytes: int,
    min_pause_ms: int,
    max_pause_ms: int,
    chunk_size: int,
) -> None:
    """
    Continuously write a unique file, then delete it, until stopped.

    Each iteration writes the file in chunks to keep memory usage bounded and
    to produce realistic, bursty I/O patterns.

    Args:
        worker_id: Unique integer identifier for this worker (used in filenames).
        target_dir: Directory on the target filesystem to write files into.
        min_file_bytes: Lower bound for each file's total size.
        max_file_bytes: Upper bound for each file's total size.
        min_pause_ms: Minimum pause in milliseconds between iterations.
        max_pause_ms: Maximum pause in milliseconds between iterations.
        chunk_size: Number of bytes written per write() call.
    """
    iteration = 0
    while not _stop_event.is_set():
        file_path = target_dir / f"ht_worker{worker_id:04d}_iter{iteration:08d}.tmp"
        total_bytes = random.randint(min_file_bytes, max_file_bytes)
        bytes_written = 0

        try:
            with file_path.open("wb") as fh:
                while bytes_written < total_bytes and not _stop_event.is_set():
                    remaining = total_bytes - bytes_written
                    write_size = min(chunk_size, remaining)
                    fh.write(os.urandom(write_size))
                    bytes_written += write_size
            logger.debug(
                "Worker %d wrote %d bytes → %s", worker_id, bytes_written, file_path.name
            )
        except OSError as exc:
            logger.error("Worker %d I/O error: %s", worker_id, exc)
        finally:
            try:
                file_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Worker %d could not delete %s: %s", worker_id, file_path, exc)

        pause_s = random.randint(min_pause_ms, max_pause_ms) / 1000.0
        _stop_event.wait(timeout=pause_s)
        iteration += 1

    logger.debug("Worker %d exiting after %d iteration(s).", worker_id, iteration)


def run(
    target_dir: Path,
    num_workers: int,
    min_file_kb: int,
    max_file_kb: int,
    min_pause_ms: int,
    max_pause_ms: int,
    chunk_kb: int,
    exit_timeout: int,
) -> None:
    """
    Launch all workers and block until a stop signal is received.

    Workers run as daemon threads so the process is never held hostage by
    threads stuck in a blocking NAS syscall. After the stop event is set,
    the main thread waits up to *exit_timeout* seconds for a clean shutdown
    before calling os._exit() to force-terminate.

    Args:
        target_dir: Destination directory for temporary files.
        num_workers: Number of concurrent file-writing threads.
        min_file_kb: Minimum file size in KiB.
        max_file_kb: Maximum file size in KiB.
        min_pause_ms: Minimum inter-iteration pause in milliseconds.
        max_pause_ms: Maximum inter-iteration pause in milliseconds.
        chunk_kb: Write chunk size in KiB.
        exit_timeout: Seconds to wait for workers after stop before force-exiting.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Starting %d workers → %s  (file size: %d–%d KiB, pause: %d–%d ms)",
        num_workers,
        target_dir,
        min_file_kb,
        max_file_kb,
        min_pause_ms,
        max_pause_ms,
    )

    min_bytes = min_file_kb * 1024
    max_bytes = max_file_kb * 1024
    chunk_bytes = chunk_kb * 1024

    threads = [
        threading.Thread(
            target=_worker,
            name=f"ht-{i}",
            daemon=True,  # won't block process exit if stuck in a kernel call
            kwargs=dict(
                worker_id=i,
                target_dir=target_dir,
                min_file_bytes=min_bytes,
                max_file_bytes=max_bytes,
                min_pause_ms=min_pause_ms,
                max_pause_ms=max_pause_ms,
                chunk_size=chunk_bytes,
            ),
        )
        for i in range(num_workers)
    ]
    for t in threads:
        t.start()

    # Block the main thread until a signal fires.
    _stop_event.wait()

    logger.info("Stop requested — waiting up to %ds for workers to finish…", exit_timeout)
    deadline = time.monotonic() + exit_timeout
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=remaining)

    still_alive = sum(1 for t in threads if t.is_alive())
    if still_alive:
        logger.warning("%d worker(s) still blocked on I/O — forcing exit.", still_alive)
        os._exit(0)

    logger.info("All workers stopped.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hammertime",
        description=(
            "Simulate sustained, fluctuating heavy file I/O load on a filesystem "
            "or NAS mount point."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "target_dir",
        type=Path,
        help="Directory to write temporary files into (must be on the target filesystem).",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="Number of concurrent writer threads.",
    )
    parser.add_argument(
        "--min-file-kb",
        type=int,
        default=512,
        metavar="KiB",
        help="Minimum size of each temporary file in KiB.",
    )
    parser.add_argument(
        "--max-file-kb",
        type=int,
        default=65536,
        metavar="KiB",
        help="Maximum size of each temporary file in KiB (default: 64 MiB).",
    )
    parser.add_argument(
        "--min-pause-ms",
        type=int,
        default=0,
        metavar="ms",
        help="Minimum pause between file write iterations per worker, in milliseconds.",
    )
    parser.add_argument(
        "--max-pause-ms",
        type=int,
        default=500,
        metavar="ms",
        help="Maximum pause between file write iterations per worker, in milliseconds.",
    )
    parser.add_argument(
        "--chunk-kb",
        type=int,
        default=256,
        metavar="KiB",
        help="Size of each individual write() call in KiB.",
    )
    parser.add_argument(
        "--exit-timeout",
        type=int,
        default=5,
        metavar="s",
        help="Seconds to wait for workers to finish after Ctrl+C before force-exiting.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (logs every file written).",
    )
    return parser


def main() -> None:
    """Entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.min_file_kb > args.max_file_kb:
        parser.error("--min-file-kb must be ≤ --max-file-kb")
    if args.min_pause_ms > args.max_pause_ms:
        parser.error("--min-pause-ms must be ≤ --max-pause-ms")
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    _install_signal_handlers()

    run(
        target_dir=args.target_dir,
        num_workers=args.workers,
        min_file_kb=args.min_file_kb,
        max_file_kb=args.max_file_kb,
        min_pause_ms=args.min_pause_ms,
        max_pause_ms=args.max_pause_ms,
        chunk_kb=args.chunk_kb,
        exit_timeout=args.exit_timeout,
    )


if __name__ == "__main__":
    main()
