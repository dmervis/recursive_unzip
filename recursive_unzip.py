#!/usr/bin/env python3
"""Recursively unpack .zip, .gz, .tar.gz, and .tgz files with no third-party packages."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
import threading
import zipfile

STATE_FILE = ".recursive_unpack_state.json"
PRINT_LOCK = threading.Lock()


def preflight(root: Path) -> None:
    """Check every dependency and filesystem requirement before extraction."""
    errors = []
    if sys.version_info < (3, 9):
        errors.append(f"Python 3.9+ required (found {sys.version.split()[0]})")
    for name in ("concurrent.futures", "gzip", "json", "shutil", "tarfile", "zipfile"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            errors.append(f"missing standard-library module {name}: {exc}")
    if not root.is_dir():
        errors.append(f"folder does not exist: {root}")
    else:
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".write_test_", delete=True):
                pass
        except OSError as exc:
            errors.append(f"folder is not writable: {exc}")
    if errors:
        raise RuntimeError("Preflight failed:\n- " + "\n- ".join(errors))


def kind_and_output(source: Path) -> tuple[str, Path] | None:
    name = source.name.lower()
    if name.endswith(".tar.gz"):
        return "tar", source.with_name(source.name[:-7])
    if name.endswith(".tgz"):
        return "tar", source.with_name(source.name[:-4])
    if name.endswith(".zip"):
        return "zip", source.with_name(source.name[:-4])
    if name.endswith(".gz"):
        return "gzip", source.with_name(source.name[:-3])
    return None


def safe_path(base: Path, name: str) -> Path:
    """Reject absolute paths, drive paths, and directory traversal."""
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"unsafe member path: {name!r}")
    if ":" in pure.parts[0]:
        raise ValueError(f"unsafe member drive: {name!r}")
    target = base.joinpath(*pure.parts)
    if os.path.commonpath((str(base.resolve()), str(target.resolve(strict=False)))) != str(base.resolve()):
        raise ValueError(f"member escapes output folder: {name!r}")
    return target


def merge_staging(staging: Path, output: Path) -> None:
    if output.exists():
        if not output.is_dir():
            raise FileExistsError(f"expected an output folder but found a file: {output}")
        shutil.copytree(staging, output, dirs_exist_ok=True)
        shutil.rmtree(staging)
    else:
        os.replace(staging, output)


def extract_zip(source: Path, output: Path) -> int:
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.unpacking_", dir=output.parent))
    count = 0
    try:
        with zipfile.ZipFile(source) as archive:
            corrupt = archive.testzip()
            if corrupt:
                raise zipfile.BadZipFile(f"CRC failure: {corrupt}")
            for member in archive.infolist():
                target = safe_path(staging, member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"symbolic link rejected: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    count += 1
        merge_staging(staging, output)
        return count
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def extract_tar(source: Path, output: Path) -> int:
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.unpacking_", dir=output.parent))
    count = 0
    try:
        with tarfile.open(source, "r:gz") as archive:
            for member in archive.getmembers():
                target = safe_path(staging, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = archive.extractfile(member)
                    if src is None:
                        raise tarfile.ExtractError(f"cannot read {member.name}")
                    with src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    count += 1
                else:
                    raise ValueError(f"link or special TAR entry rejected: {member.name}")
        merge_staging(staging, output)
        return count
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def extract_gzip(source: Path, output: Path) -> int:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        with gzip.open(source, "rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.replace(temporary, output)
        return 1
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def extract_one(source: Path, kind: str, output: Path) -> tuple[int, str | None]:
    try:
        count = {"zip": extract_zip, "tar": extract_tar, "gzip": extract_gzip}[kind](source, output)
        source.unlink()
        with PRINT_LOCK:
            print(f"OK   {source} -> {output} (archive removed)")
        return count, None
    except Exception as exc:
        with PRINT_LOCK:
            print(f"FAIL {source}: {exc}", file=sys.stderr)
        return 0, f"{type(exc).__name__}: {exc}"


def signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def discover(root: Path) -> list[tuple[Path, str, Path]]:
    result = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if ".unpacking_" not in name]
        for filename in filenames:
            source = Path(directory, filename)
            identified = kind_and_output(source)
            if identified:
                result.append((source, *identified))
    return result


def load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    default_root = Path.home() / "Downloads" / "ctxs"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=default_root)
    parser.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2) + 2))
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        preflight(root)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2

    print(f"Preflight passed (Python {sys.version.split()[0]}; no third-party dependencies)")
    print(f"Scanning {root}")
    state_path = root / STATE_FILE
    state = load_state(state_path)
    failures: dict[str, str] = {}
    archive_total = file_total = pass_number = 0

    while True:
        pass_number += 1
        pending = []
        for source, kind, output in discover(root):
            relative = source.relative_to(root).as_posix()
            sig = signature(source)
            previous = state.get(relative, {})
            done = previous.get("signature") == sig and output.exists()
            if not done and relative not in failures:
                pending.append((source, kind, output, relative, sig))
        if not pending:
            break

        print(f"Pass {pass_number}: {len(pending)} archive(s), {args.workers} worker(s)")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs = {
                pool.submit(extract_one, source, kind, output): (output, relative, sig)
                for source, kind, output, relative, sig in pending
            }
            for job in as_completed(jobs):
                output, relative, sig = jobs[job]
                count, error = job.result()
                if error:
                    failures[relative] = error
                else:
                    state[relative] = {
                        "signature": sig,
                        "output": output.relative_to(root).as_posix(),
                    }
                    archive_total += 1
                    file_total += count
        save_state(state_path, state)

    print(f"Done: {archive_total} archive(s), {file_total} file(s), {pass_number} scan pass(es)")
    if failures:
        print(f"Failures: {len(failures)}", file=sys.stderr)
        for name, error in sorted(failures.items()):
            print(f"- {name}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
