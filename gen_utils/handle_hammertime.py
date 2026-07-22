"""
Stress-test simultaneous open file handles on a NAS mount.

This script opens a configurable number of files simultaneously, then randomly
jumps between open handles and writes random bytes to each file until every
file reaches a configured byte target. Completed files are closed and deleted.

Designed for extreme handle-count testing (50K+), subject to OS file-descriptor
limits and filesystem behavior.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import resource
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_EXIT_GRACE_SECONDS = 5


@dataclass
class HammerConfig:

    """
    Tunable parameters for the handle hammer workload.

    Attributes:
        bytes_per_file: Total bytes to write to each file.
        min_chunk_bytes: Minimum write size per call.
        max_chunk_bytes: Maximum write size per call.
        fd_reserve: Extra descriptors reserved for runtime overhead.
        auto_raise_nofile: Try to raise RLIMIT_NOFILE when needed.
        progress_interval_s: Seconds between progress log messages.
    """

    bytes_per_file: int
    min_chunk_bytes: int
    max_chunk_bytes: int
    fd_reserve: int
    auto_raise_nofile: bool
    progress_interval_s: float


@dataclass
class _WorkerShared:

    """
    Per-run state shared among worker threads.

    Each index in written_totals and completed_counts is owned exclusively by the matching worker — no lock is required for writes.

    Attributes:
        stop_event: Set to request early termination across all workers.
        written_totals: Per-worker byte counters updated on exit.
        completed_counts: Per-worker file-completion counters.
        min_chunk_bytes: Minimum write size per call.
        max_chunk_bytes: Maximum write size per call.
    """

    stop_event: threading.Event
    written_totals: list[int]
    completed_counts: list[int]
    min_chunk_bytes: int
    max_chunk_bytes: int


def _build_parser() -> argparse.ArgumentParser:
    """
    Create CLI parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="handle_hammertime",
        description=(
            "Open many files simultaneously, write random data by hopping among "
            "open handles, then close and delete them."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "target_dir",
        type=Path,
        help="Directory on the NAS mount used for temporary test files.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel writer threads.",
    )
    parser.add_argument(
        "-n",
        "--num-files",
        type=int,
        default=50000,
        metavar="N",
        help="Number of files to keep open simultaneously.",
    )
    parser.add_argument(
        "-b",
        "--bytes-per-file",
        type=int,
        default=1_048_576,
        metavar="BYTES",
        help="Total bytes to write to each file before closing/deleting it.",
    )
    parser.add_argument(
        "--min-chunk-bytes",
        type=int,
        default=4096,
        metavar="BYTES",
        help="Minimum write size for each random write operation.",
    )
    parser.add_argument(
        "--max-chunk-bytes",
        type=int,
        default=65536,
        metavar="BYTES",
        help="Maximum write size for each random write operation.",
    )
    parser.add_argument(
        "--fd-reserve",
        type=int,
        default=128,
        metavar="N",
        help="Extra descriptors reserved for logs/runtime beyond open test files.",
    )
    parser.add_argument(
        "--no-auto-raise-nofile",
        action="store_true",
        help="Do not attempt to increase RLIMIT_NOFILE automatically.",
    )
    parser.add_argument(
        "--progress-interval-s",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Seconds between progress log messages.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="INT",
        help="Optional random seed for repeatable handle-hop patterns.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """
    Validate argument relationships.

    Args:
        args: Parsed CLI args.
        parser: Parser used to report validation errors.
    """
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.num_files < 1:
        parser.error("--num-files must be at least 1")
    if args.bytes_per_file < 0:
        parser.error("--bytes-per-file must be >= 0")
    if args.min_chunk_bytes < 1:
        parser.error("--min-chunk-bytes must be at least 1")
    if args.max_chunk_bytes < args.min_chunk_bytes:
        parser.error("--max-chunk-bytes must be >= --min-chunk-bytes")
    if args.fd_reserve < 0:
        parser.error("--fd-reserve must be >= 0")
    if args.progress_interval_s <= 0:
        parser.error("--progress-interval-s must be > 0")


def _ensure_fd_limit(required_fds: int, auto_raise: bool) -> None:
    """
    Ensure process soft RLIMIT_NOFILE can support the requested workload.

    Args:
        required_fds: Minimum soft descriptor limit needed.
        auto_raise: Whether to attempt raising the soft limit.

    Raises:
        RuntimeError: If descriptor limit remains insufficient.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    LOGGER.info("RLIMIT_NOFILE soft=%s hard=%s", soft, hard)

    if soft >= required_fds:
        return

    if auto_raise:
        target_soft = required_fds
        if hard != resource.RLIM_INFINITY:
            target_soft = min(required_fds, hard)

        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target_soft, hard))
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            LOGGER.info("Updated RLIMIT_NOFILE soft=%s hard=%s", soft, hard)
        except (ValueError, OSError) as exc:
            LOGGER.warning("Failed to raise RLIMIT_NOFILE: %s", exc)

    if soft < required_fds:
        raise RuntimeError(
            "Insufficient RLIMIT_NOFILE soft limit. "
            f"Need at least {required_fds}, have {soft}. "
            "Try: ulimit -n <higher>, run as a user permitted to raise limits, "
            "or reduce --num-files."
        )


def _open_files(target_dir: Path, num_files: int) -> tuple[list[int], list[Path], list[int]]:
    """
    Open num_files temporary files and return parallel state lists.

    Args:
        target_dir: Directory to place test files.
        num_files: Number of files to open.

    Returns:
        Tuple of (fds, paths, remaining-bytes list initialized later).

    Raises:
        OSError: If file creation/open fails.
    """
    fds: list[int] = []
    paths: list[Path] = []

    for index in range(num_files):
        file_path = target_dir / f"fh_{index:06d}.tmp"
        fd = os.open(file_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
        fds.append(fd)
        paths.append(file_path)

        if index > 0 and index % 10000 == 0:
            LOGGER.info("Opened %d/%d files", index, num_files)

    remaining = [0] * num_files
    return fds, paths, remaining


def _close_and_delete(fd: int, file_path: Path) -> None:
    """
    Close a file descriptor and remove its file.

    Args:
        fd: File descriptor.
        file_path: File path for unlink after close.
    """
    try:
        os.close(fd)
    except OSError as exc:
        LOGGER.debug("close failed for %s: %s", file_path, exc)

    try:
        file_path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.debug("unlink failed for %s: %s", file_path, exc)


def _evict(
    idx: int,
    active: int,
    fds: list[int],
    paths: list[Path],
    remaining: list[int],
) -> int:
    """
    Swap-remove the file at idx from the active lists and return new active count.

    Args:
        idx: Index of the file to remove.
        active: Current number of active files.
        fds: Live file-descriptor list (modified in place).
        paths: Parallel path list (modified in place).
        remaining: Parallel remaining-bytes list (modified in place).

    Returns:
        Updated active count (active - 1).
    """
    last = active - 1
    fds[idx], fds[last] = fds[last], fds[idx]
    paths[idx], paths[last] = paths[last], paths[idx]
    remaining[idx], remaining[last] = remaining[last], remaining[idx]
    fds.pop()
    paths.pop()
    remaining.pop()
    return active - 1


def _worker_loop(
    worker_id: int,
    fds: list[int],
    paths: list[Path],
    remaining: list[int],
    shared: _WorkerShared,
) -> None:
    """
    Execute the random-hop write loop for one worker's file slice.

    Args:
        worker_id: Zero-based index used for logging and shared counter slots.
        fds: Mutable list of file descriptors owned by this worker.
        paths: Mutable list of paths parallel to *fds*.
        remaining: Mutable per-file byte counters parallel to *fds*.
        shared: Shared configuration and result accumulators.
    """
    active = len(fds)
    local_written = 0
    try:
        while active > 0 and not shared.stop_event.is_set():
            idx = random.randrange(active)

            # Guard: file already filled (edge case on stop path).
            if remaining[idx] <= 0:
                _close_and_delete(fds[idx], paths[idx])
                shared.completed_counts[worker_id] += 1
                active = _evict(idx, active, fds, paths, remaining)
                continue

            write_upper = min(shared.max_chunk_bytes, remaining[idx])
            write_lower = min(shared.min_chunk_bytes, write_upper)
            write_size = random.randint(write_lower, write_upper)

            try:
                written = os.write(fds[idx], os.urandom(write_size))
                local_written += written
                remaining[idx] -= written
            except OSError as exc:
                LOGGER.error("Worker %d: write error on %s: %s", worker_id, paths[idx].name, exc)
                remaining[idx] = 0  # evict on error

            if remaining[idx] <= 0:
                _close_and_delete(fds[idx], paths[idx])
                shared.completed_counts[worker_id] += 1
                active = _evict(idx, active, fds, paths, remaining)
    finally:
        shared.written_totals[worker_id] = local_written
        # Clean up files not yet closed (stop_event or error path).
        for fd, file_path in zip(fds, paths):
            _close_and_delete(fd, file_path)
    LOGGER.debug("Worker %d finished.", worker_id)


def _partition_files(
    fds: list[int],
    paths: list[Path],
    remaining: list[int],
    num_workers: int,
) -> tuple[list[list[int]], list[list[Path]], list[list[int]]]:
    """
    Split file lists evenly across num_workers workers.

    Args:
        fds: All open file descriptors.
        paths: Parallel path list.
        remaining: Parallel remaining-bytes list.
        num_workers: Number of partitions to create.

    Returns:
        Three parallel lists of per-worker slices: (worker_fds, worker_paths, worker_remain).
    """
    n = len(fds)
    chunk = math.ceil(n / num_workers)
    worker_fds = [fds[i : i + chunk] for i in range(0, n, chunk)]
    worker_paths = [paths[i : i + chunk] for i in range(0, n, chunk)]
    worker_remain = [remaining[i : i + chunk] for i in range(0, n, chunk)]
    return worker_fds, worker_paths, worker_remain


def _run_progress_loop(
    threads: list[threading.Thread],
    num_files: int,
    shared: _WorkerShared,
    progress_interval_s: float,
) -> None:
    """
    Block until all worker threads finish or the stop event fires, logging progress.

    Args:
        threads: Worker threads to monitor.
        num_files: Total file count (for percentage display).
        shared: Shared counters and stop event.
        progress_interval_s: Seconds between log lines.
    """
    while any(t.is_alive() for t in threads):
        shared.stop_event.wait(timeout=progress_interval_s)
        total_written = sum(shared.written_totals)
        done_files = sum(shared.completed_counts)
        pct = (done_files / num_files) * 100.0
        LOGGER.info(
            "Progress: %.2f%% files complete (%d/%d), %.2f MiB written",
            pct,
            done_files,
            num_files,
            total_written / (1024 * 1024),
        )
        if shared.stop_event.is_set():
            break


def _shutdown_workers(threads: list[threading.Thread]) -> int:
    """
    Join worker threads with a grace period, force-exiting if any are still blocked.

    Args:
        threads: Worker threads to wait for.

    Returns:
        Number of threads still alive after the grace period (0 on clean shutdown).
    """
    deadline = time.monotonic() + _EXIT_GRACE_SECONDS
    for t in threads:
        remaining_time = deadline - time.monotonic()
        if remaining_time > 0:
            t.join(timeout=remaining_time)
    return sum(1 for t in threads if t.is_alive())


def run_handle_hammer(
    target_dir: Path,
    num_files: int,
    num_workers: int,
    config: HammerConfig,
) -> None:
    """
    Run the open-handle hammer workload across one or more parallel workers.

    Opens num_files handles simultaneously, partitions them across num_workers
    threads, and lets each worker independently hop among its handles until every
    file reaches config.bytes_per_file bytes. On SIGINT/SIGTERM a grace period
    of _EXIT_GRACE_SECONDS is given before os._exit() forces termination.

    Args:
        target_dir: Test directory on the NAS mount.
        num_files: Number of simultaneous open files.
        num_workers: Number of parallel writer threads (1 = single-threaded).
        config: Tunable workload parameters.
    """
    stop_event = threading.Event()

    def _signal_handler(signum: int, _frame: object) -> None:
        LOGGER.warning("Signal %d received: stopping and cleaning up.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    target_dir.mkdir(parents=True, exist_ok=True)
    _ensure_fd_limit(
        required_fds=num_files + config.fd_reserve,
        auto_raise=config.auto_raise_nofile,
    )

    num_workers = min(num_workers, num_files)
    LOGGER.info(
        "Opening %d files in %s (workers=%d, bytes/file=%d, chunk=%d-%d bytes)",
        num_files,
        target_dir,
        num_workers,
        config.bytes_per_file,
        config.min_chunk_bytes,
        config.max_chunk_bytes,
    )

    start_time = time.monotonic()
    # Cleared after partitioning so the finally block is a no-op on success.
    fds: list[int] = []
    paths: list[Path] = []

    try:
        fds, paths, remaining = _open_files(target_dir=target_dir, num_files=num_files)
        for index in range(num_files):
            remaining[index] = config.bytes_per_file

        worker_fds, worker_paths, worker_remain = _partition_files(
            fds, paths, remaining, num_workers
        )
        actual_workers = len(worker_fds)
        shared = _WorkerShared(
            stop_event=stop_event,
            written_totals=[0] * actual_workers,
            completed_counts=[0] * actual_workers,
            min_chunk_bytes=config.min_chunk_bytes,
            max_chunk_bytes=config.max_chunk_bytes,
        )

        # Workers own the handles from this point; clear so finally is a no-op.
        fds = []
        paths = []

        threads = [
            threading.Thread(
                target=_worker_loop,
                name=f"fh-worker-{i}",
                daemon=True,
                kwargs={
                    "worker_id": i,
                    "fds": worker_fds[i],
                    "paths": worker_paths[i],
                    "remaining": worker_remain[i],
                    "shared": shared,
                },
            )
            for i in range(actual_workers)
        ]
        for t in threads:
            t.start()

        _run_progress_loop(threads, num_files, shared, config.progress_interval_s)

        still_alive = _shutdown_workers(threads)
        elapsed_s = time.monotonic() - start_time
        total_written = sum(shared.written_totals)

        if still_alive:
            LOGGER.warning(
                "%d worker(s) still blocked on I/O — forcing exit. Wrote %.2f MiB.",
                still_alive,
                total_written / (1024 * 1024),
            )
            os._exit(0)

        _log_completion(stop_event.is_set(), total_written, num_files, elapsed_s)
    finally:
        for fd, file_path in zip(fds, paths):
            _close_and_delete(fd, file_path)


def _log_completion(
    stopped_early: bool,
    total_written: int,
    num_files: int,
    elapsed_s: float,
) -> None:
    """
    Log final run statistics.

    Args:
        stopped_early: True if the run was interrupted before completion.
        total_written: Total bytes written across all files.
        num_files: Total number of files in the run.
        elapsed_s: Wall-clock seconds elapsed.
    """
    mib = total_written / (1024 * 1024)
    if stopped_early:
        LOGGER.warning("Stopped early: wrote %.2f MiB in %.2fs", mib, elapsed_s)
        return
    throughput = mib / elapsed_s if elapsed_s > 0 else 0.0
    LOGGER.info(
        "Completed: wrote %.2f MiB across %d files in %.2fs (%.2f MiB/s)",
        mib,
        num_files,
        elapsed_s,
        throughput,
    )


def main() -> None:
    """Program entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(args=args, parser=parser)

    if args.seed is not None:
        random.seed(args.seed)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = HammerConfig(
        bytes_per_file=args.bytes_per_file,
        min_chunk_bytes=args.min_chunk_bytes,
        max_chunk_bytes=args.max_chunk_bytes,
        fd_reserve=args.fd_reserve,
        auto_raise_nofile=not args.no_auto_raise_nofile,
        progress_interval_s=args.progress_interval_s,
    )

    run_handle_hammer(
        target_dir=args.target_dir,
        num_files=args.num_files,
        num_workers=args.workers,
        config=config,
    )


if __name__ == "__main__":
    main()
