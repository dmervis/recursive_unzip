#!/usr/bin/env python3
"""Recursively unpack .zip, .7z, .gz, .tar.gz, and .tgz files, deleting each archive once it is unpacked.

Scans repeatedly until no archives are left (archives found inside unpacked archives are unpacked
too). No third-party Python packages are needed; .7z additionally needs the 7-Zip program (7z.exe),
found on PATH or in Program Files.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import functools
import gzip
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import zipfile

STATE_FILE = ".recursive_unpack_state.json"
EXTENDED_PREFIX = "\\\\?\\"
EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"
LABELS = {"zip": "zip", "7z": "7z", "tar": "tgz", "gzip": "gz"}


def extended(path: Path) -> Path:
    """On Windows, add the \\\\?\\ prefix so paths over 260 characters work (deeply nested archives)."""
    text = str(path)
    if os.name != "nt" or text.startswith(EXTENDED_PREFIX):
        return path
    if text.startswith("\\\\"):
        return Path(EXTENDED_UNC_PREFIX + text[2:])
    return Path(EXTENDED_PREFIX + text)


def plain(text: str) -> str:
    """Strip the extended-path prefix again for anything shown to the user."""
    escaped = EXTENDED_PREFIX.replace("\\", "\\\\")  # how repr() shows it inside exception messages
    return text.replace(EXTENDED_UNC_PREFIX, "\\\\").replace(escaped, "").replace(EXTENDED_PREFIX, "")


def human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def clock(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def enable_ansi() -> bool:
    """Turn on ANSI escape handling in the Windows console; other platforms already have it."""
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel32.SetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        for std_handle in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                return False
        return True
    except Exception:
        return False


class Console:
    """All terminal output: colours, symbols and a live status bar on a terminal, plain text otherwise."""

    STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
    FANCY = {"ok": "✔", "bad": "✘", "warn": "!", "pass": "▸", "title": "◆ ", "rule": "─", "sep": "·",
             "elbow": "└─", "fill": "█", "empty": "░"}
    ASCII = {"ok": "OK", "bad": "FAIL", "warn": "!", "pass": ">", "title": "", "rule": "-", "sep": "-",
             "elbow": "->", "fill": "#", "empty": "-"}
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    BAR_WIDTH = 20

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = self.color = False
        self.glyphs = self.ASCII
        self.mark_width = 4
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.ticker: threading.Thread | None = None
        self.active = False
        self.shown = self.frame = 0
        self.pass_number = self.total = self.done = 0
        self.extracted = self.files = self.failed = 0
        self.current = ""

    def setup(self, force_plain: bool) -> None:
        """Choose the output style once the arguments are known."""
        self.live = not force_plain and sys.stdout.isatty() and sys.stderr.isatty()
        self.color = (
            self.live and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and enable_ansi()
        )
        self.glyphs = self.FANCY if self.live else self.ASCII
        self.mark_width = 1 if self.live else 4

    # ---- styling helpers -------------------------------------------------------------------------

    def paint(self, text: str, *styles: str) -> str:
        if not (self.color and styles and text):
            return text
        return f"\x1b[{';'.join(self.STYLES[style] for style in styles)}m{text}\x1b[0m"

    def columns(self) -> int:
        # One column short of the terminal width: a line that fills the last column can wrap early.
        return min(shutil.get_terminal_size((100, 24)).columns - 1, 110) if self.live else 78

    def fit(self, text: str, room: int, keep_start: bool = False) -> str:
        """Shorten to `room` columns, keeping the end (paths) or the start (messages); never in plain mode."""
        if not self.live or len(text) <= room:
            return text
        room = max(room, 2)
        return text[: room - 1] + "…" if keep_start else "…" + text[-(room - 1):]

    def mark(self, name: str, *styles: str) -> str:
        return self.paint(self.glyphs[name].ljust(self.mark_width), *styles)

    def rule(self) -> str:
        return " " + self.paint(self.glyphs["rule"] * (self.columns() - 2), "dim")

    # ---- messages --------------------------------------------------------------------------------

    def emit(self, *lines: str, err: bool = False) -> None:
        with self.lock:
            self._clear()
            print("\n".join(plain(line) for line in lines), file=sys.stderr if err else sys.stdout, flush=True)
            self._draw()

    def banner(self, rows: list[tuple[str, str, str, bool]]) -> None:
        """Rows are (label, value, colour, value_is_a_path)."""
        columns = self.columns()
        lines = ["", " " + self.paint(f"{self.glyphs['title']}RECURSIVE UNZIP", "bold", "cyan"), self.rule()]
        for label, value, style, is_path in rows:
            shown = self.fit(value, columns - 14, keep_start=not is_path)
            lines.append("   " + self.paint(label.ljust(10), "dim") + self.paint(shown, *filter(None, [style])))
        lines.append(self.rule())
        self.emit(*lines)

    def pass_header(self, number: int, total: int) -> None:
        noun = "archive" if total == 1 else "archives"
        self.emit(
            "",
            " " + self.paint(f"{self.glyphs['pass']} Pass {number}", "bold", "cyan")
            + self.paint(f"  {self.glyphs['sep']}  {total} {noun}", "dim"),
        )

    def archive_ok(self, kind: str, size: int, files: int, relative: str) -> None:
        files_text = f"{files} file" + ("" if files == 1 else "s")
        stats = f"{human_size(size):>9}  {files_text:>11}  "
        room = self.columns() - (2 + self.mark_width + 1 + 3 + 2 + len(stats))
        self.emit(
            "  " + self.mark("ok", "green") + " " + self.paint(f"{LABELS[kind]:<3}", "cyan") + "  "
            + self.paint(stats, "dim") + self.fit(relative, room)
        )

    def archive_fail(self, kind: str, relative: str, error: str) -> None:
        columns = self.columns()
        head = 2 + self.mark_width + 1
        elbow = self.glyphs["elbow"]
        self.emit(
            "  " + self.mark("bad", "red", "bold") + " " + self.paint(f"{LABELS[kind]:<3}", "cyan") + "  "
            + self.fit(relative, columns - head - 5),
            " " * head + self.paint(elbow + " ", "dim")
            + self.paint(self.fit(plain(error), columns - head - len(elbow) - 1, keep_start=True), "red"),
            err=True,
        )

    def summary(self, archives: int, files: int, passes: int, failures: dict[str, str]) -> None:
        columns = self.columns()
        took = self.paint(f"  in {clock(time.monotonic() - self.started)}", "dim")
        if failures:
            noun = "archive" if len(failures) == 1 else "archives"
            headline = self.paint(f"{self.glyphs['warn']} Finished with {len(failures)} failed {noun}", "bold", "yellow")
        else:
            headline = self.paint(f"{self.glyphs['ok']} All archives unpacked", "bold", "green")
        lines = ["", self.rule(), "  " + headline + took, ""]
        stats = [("Archives unpacked", str(archives)), ("Files extracted", str(files)), ("Passes", str(passes))]
        if failures:
            stats.append(("Failed", f"{len(failures)}"))
        for label, value in stats:
            lines.append("    " + self.paint(label.ljust(20), "dim") + self.paint(value.rjust(6), "bold"))
        if failures:
            lines += ["", "  " + self.paint("Failed archives (left in place)", "bold")]
            for relative, error in sorted(failures.items()):
                lines.append("    " + self.mark("bad", "red", "bold") + " " + self.fit(relative.replace("/", os.sep), columns - 8))
                reason = textwrap.wrap(plain(error), width=columns - 10) if self.live else [plain(error)]
                lines += ["        " + self.paint(part, "red") for part in reason]
        lines.append(self.rule())
        self.emit(*lines)

    # ---- live status bar -------------------------------------------------------------------------

    def start_pass(self, number: int, total: int) -> None:
        with self.lock:
            self.pass_number, self.total, self.done, self.current = number, total, 0, ""
            self.active = True
            self._draw()
        if self.live and self.ticker is None:
            self.ticker = threading.Thread(target=self._tick, daemon=True)
            self.ticker.start()

    def begin(self, name: str) -> None:
        with self.lock:
            self.current = name
            self._draw()

    def finish(self, files: int, failed: bool) -> None:
        with self.lock:
            self.done += 1
            if failed:
                self.failed += 1
            else:
                self.extracted += 1
                self.files += files
            self._draw()

    def close(self) -> None:
        self.stop.set()
        if self.ticker:
            self.ticker.join()
        with self.lock:
            self.active = False
            self._clear()

    def _tick(self) -> None:
        while not self.stop.wait(0.1):
            with self.lock:
                self.frame += 1
                self._draw()

    def _clear(self) -> None:
        if self.shown:
            sys.stdout.write("\r" + " " * self.shown + "\r")
            sys.stdout.flush()
            self.shown = 0

    def _draw(self) -> None:
        if not (self.live and self.active):
            return
        filled = self.BAR_WIDTH * self.done // self.total
        segments = [
            ("  ", ()),
            (self.SPINNER[self.frame % len(self.SPINNER)] + " ", ("cyan", "bold")),
            (f"Pass {self.pass_number} ", ("bold",)),
            (self.glyphs["fill"] * filled, ("green",)),
            (self.glyphs["empty"] * (self.BAR_WIDTH - filled), ("dim",)),
            (f" {self.done}/{self.total} {100 * self.done // self.total:>3}%", ("bold",)),
            (f"  {clock(time.monotonic() - self.started)}", ("dim",)),
            (f"  {self.glyphs['ok']} {self.extracted}", ("green",)),
            (f"  {self.glyphs['bad']} {self.failed}", ("red",) if self.failed else ("dim",)),
            (f"  {self.files} files", ("dim",)),
        ]
        if self.current:
            segments.append((f"  {self.current}", ("cyan",)))
        room = max(shutil.get_terminal_size((100, 24)).columns - 1, 20)
        pieces, used = [], 0
        for text, styles in segments:
            text = text[: room - used]
            used += len(text)
            pieces.append(self.paint(text, *styles))
        sys.stdout.write("\r" + "".join(pieces) + " " * max(self.shown - used, 0))
        sys.stdout.flush()
        self.shown = used


UI = Console()


@functools.lru_cache(maxsize=None)
def find_7z() -> str | None:
    for name in ("7z", "7zz", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base and Path(base, "7-Zip", "7z.exe").is_file():
            return str(Path(base, "7-Zip", "7z.exe"))
    return None


def preflight(root: Path) -> None:
    """Check every dependency and filesystem requirement before extraction."""
    errors = []
    if sys.version_info < (3, 9):
        errors.append(f"Python 3.9+ required (found {sys.version.split()[0]})")
    for name in ("concurrent.futures", "gzip", "json", "shutil", "subprocess", "tarfile", "zipfile"):
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
    if name.endswith(".7z"):
        return "7z", source.with_name(source.name[:-3])
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


def run_7z(*args: str) -> str:
    executable = find_7z()
    if executable is None:
        raise RuntimeError("7-Zip not found; install it or put 7z on PATH to unpack .7z files")
    result = subprocess.run([executable, *args], stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if result.returncode != 0:
        if result.returncode == 255:  # 7z aborts like this when it prompts for a password and stdin is closed
            raise RuntimeError("7z needs a password (archive is encrypted)")
        detail = " ".join((result.stderr or result.stdout).decode("utf-8", "replace").split())
        if "Is not archive" in detail:
            raise RuntimeError("not a valid 7z archive (corrupt or wrong format)")
        raise RuntimeError(f"7z exited with code {result.returncode}: {detail[:300] or 'no output'}")
    return result.stdout.decode("utf-8", "replace")


def check_7z_listing(listing: str, staging: Path) -> None:
    """Vet every entry of `7z l -slt` output before anything is extracted."""
    for block in listing.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.partition(" = ")[::2] for line in block.splitlines() if " = " in line)
        name = fields.get("Path")
        if name is None:
            continue
        safe_path(staging, name)
        mode = f"{fields.get('Attributes', '')} {fields.get('Mode', '')}"
        if (
            fields.get("Symbolic Link")
            or fields.get("Hard Link")
            or "REPARSE" in mode
            or re.search(r"(?<![A-Za-z0-9])l[-rwxsStT]{9}(?![A-Za-z0-9])", mode)
        ):
            raise ValueError(f"link rejected: {name}")


def extract_7z(source: Path, output: Path) -> int:
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.unpacking_", dir=output.parent))
    try:
        check_7z_listing(run_7z("l", "-slt", "-ba", "-sccUTF-8", "--", str(source)), staging)
        run_7z("x", "-y", "-bso0", "-bsp0", f"-o{staging}", "--", str(source))
        count = 0
        for directory, dirnames, filenames in os.walk(staging):
            # Backstop for the listing check: no links may survive extraction.
            for name in (*dirnames, *filenames):
                if Path(directory, name).is_symlink():
                    raise ValueError(f"symbolic link rejected: {Path(directory, name).relative_to(staging)}")
            count += len(filenames)
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


def remove_archive(source: Path) -> None:
    try:
        source.unlink()
    except PermissionError:
        os.chmod(source, stat.S_IWRITE)  # Windows will not delete read-only files
        source.unlink()


def extract_one(source: Path, kind: str, output: Path) -> tuple[int, str | None]:
    UI.begin(source.name)
    try:
        count = {"zip": extract_zip, "7z": extract_7z, "tar": extract_tar, "gzip": extract_gzip}[kind](source, output)
        remove_archive(source)
        return count, None
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def signature(path: Path) -> dict[str, int]:
    stat_result = path.stat()
    return {"size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns}


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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", type=Path, default=default_root)
    parser.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2) + 2))
    parser.add_argument("-q", "--quiet", action="store_true", help="only show the status bar and failures")
    parser.add_argument("--plain", action="store_true", help="no colours, symbols or live status bar")
    args = parser.parse_args()
    # Archive member names are untrusted; never let an unprintable character kill the run.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    UI.setup(args.plain)
    root = extended(args.root.expanduser().resolve())
    try:
        preflight(root)
    except RuntimeError as exc:
        print(plain(str(exc)), file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2

    seven_zip = find_7z()
    UI.banner([
        ("Folder", plain(str(root)), "", True),
        ("Preflight", f"{UI.glyphs['ok'].strip()}  Python {sys.version.split()[0]}, standard library only", "green", False),
        ("7-Zip", seven_zip or "not found - .7z archives will fail (install 7-Zip)", "" if seven_zip else "yellow", bool(seven_zip)),
        ("Workers", str(args.workers), "", False),
        ("Formats", ".zip  .7z  .tar.gz  .tgz  .gz", "", False),
        ("Cleanup", "each archive is deleted once it unpacks; failed ones stay put", "", False),
    ])
    state_path = root / STATE_FILE
    state = load_state(state_path)
    failures: dict[str, str] = {}
    archive_total = file_total = pass_number = 0

    try:
        while True:
            pass_number += 1
            pending = []
            for source, kind, output in discover(root):
                relative = source.relative_to(root).as_posix()
                if relative in failures:
                    continue
                try:
                    sig = signature(source)
                except OSError as exc:  # unreadable file: report it, don't abort the whole run
                    failures[relative] = f"{type(exc).__name__}: {exc}"
                    UI.failed += 1
                    UI.archive_fail(kind, relative.replace("/", os.sep), failures[relative])
                    continue
                previous = state.get(relative, {})
                if not (previous.get("signature") == sig and output.exists()):
                    pending.append((source, kind, output, relative, sig))
            if not pending:
                break

            UI.pass_header(pass_number, len(pending))
            UI.start_pass(pass_number, len(pending))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                jobs = {
                    pool.submit(extract_one, source, kind, output): (source, kind, output, relative, sig)
                    for source, kind, output, relative, sig in pending
                }
                for job in as_completed(jobs):
                    source, kind, output, relative, sig = jobs[job]
                    count, error = job.result()
                    shown = relative.replace("/", os.sep)
                    if error:
                        failures[relative] = error
                        UI.archive_fail(kind, shown, error)
                    else:
                        state[relative] = {
                            "signature": sig,
                            "output": output.relative_to(root).as_posix(),
                        }
                        archive_total += 1
                        file_total += count
                        if not args.quiet:
                            UI.archive_ok(kind, sig["size"], count, shown)
                    UI.finish(count, failed=bool(error))
            save_state(state_path, state)
    finally:
        UI.close()

    UI.summary(archive_total, file_total, pass_number - 1, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
