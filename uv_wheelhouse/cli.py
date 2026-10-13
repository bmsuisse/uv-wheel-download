"""uv-wheelhouse: fill a folder with the wheels pinned in uv.lock, for `pip install --no-index --find-links`."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import packaging
import requests
from packaging.tags import parse_tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from uv_wheelhouse.select import Download, Target, select

# Azure Artifacts accepts the pipeline's System.AccessToken as the password of any user name.
ADO_HOST = "pkgs.dev.azure.com"
TOKEN_ENV = "SYSTEM_ACCESSTOKEN"

# Run inside the *target* interpreter, which is what pip would have looked at.
_PROBE = """
import json
from packaging.markers import default_environment
from packaging.tags import sys_tags
print(json.dumps({"env": default_environment(), "tags": [str(t) for t in sys_tags()]}))
"""

# Options of `uv export` that choose dependency groups; without any of them we add --no-dev (deploy set).
_GROUP_FLAGS = {"--no-dev", "--dev", "--only-dev", "--only-group", "--all-groups", "--no-default-groups"}


def run(cmd: list[str], cwd: str | Path | None = None, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(cmd, check=False, cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(f"error: {cmd[0]!r} not found; uv-wheelhouse needs uv on PATH")
    if result.returncode:
        sys.exit(f"error: {' '.join(cmd[:3])} failed:\n{result.stderr.strip()}")
    return result.stdout


def run_uv(*args: str, cwd: str | Path | None = None) -> str:
    return run(["uv", *args], cwd=cwd)


def resolve_python(python: str | None) -> str | None:
    """Make an interpreter path absolute: uv runs in the project dir, so a relative path would point elsewhere."""
    return str(Path(python).resolve()) if python and Path(python).exists() else python


def probe_target(python: str | None, project: Path) -> Target:
    request = resolve_python(python)
    interpreter = run_uv("python", "find", *([request] if request else []), cwd=project).strip()
    # Run the interpreter itself with a copy of our (pure Python) `packaging`: no index or network access needed.
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(
            Path(packaging.__file__).parent, Path(tmp) / "packaging", ignore=shutil.ignore_patterns("__pycache__")
        )
        out = run([interpreter, "-c", _PROBE], env={**os.environ, "PYTHONPATH": tmp})
    probe = json.loads(out)
    return Target(env=probe["env"], tags=[t for tag in probe["tags"] for t in parse_tag(tag)])


def build_export_args(user_args: list[str]) -> list[str]:
    args = [a for a in user_args if a != "--"]
    if not _GROUP_FLAGS & set(args):
        args.insert(0, "--no-dev")
    if not {"--locked", "--frozen"} & set(args):
        args.append("--frozen")  # uv.lock is the source of truth; don't re-resolve or touch the network
    return args


def auth_for(url: str) -> tuple[str, str] | None:
    """Explicit credentials only for Azure Artifacts over https; everything else uses ~/.netrc via requests."""
    token = os.environ.get(TOKEN_ENV)
    parts = urlsplit(url)
    if token and parts.scheme == "https" and parts.hostname == ADO_HOST:
        return ("VssSessionToken", token)
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch(session: requests.Session, item: Download, dest: Path, store: Path) -> str:
    """Make `dest/<filename>` exist with the locked hash. Returns where it came from."""
    target = dest / item.filename
    if target.exists() and sha256_of(target) == item.sha256:
        return "present"

    stored = store / item.sha256 / item.filename
    source = "cache"
    if not stored.exists():
        source = "download"
        stored.parent.mkdir(parents=True, exist_ok=True)
        partial = stored.with_name(f"{stored.name}.{os.getpid()}.{id(item)}.part")
        auth = auth_for(item.url)
        digest = hashlib.sha256()
        try:
            with session.get(item.url, auth=auth, stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                with partial.open("wb") as f:
                    for chunk in response.iter_content(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != item.sha256:
                raise ValueError(f"sha256 mismatch for {item.url}: expected {item.sha256}, got {digest.hexdigest()}")
            os.replace(partial, stored)
        finally:
            partial.unlink(missing_ok=True)

    target.unlink(missing_ok=True)
    try:
        os.link(stored, target)
    except OSError:  # e.g. cache and destination on different filesystems
        shutil.copyfile(stored, target)
    return source


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="uv-wheelhouse",
        allow_abbrev=False,
        description="Download all packages pinned in uv.lock into a folder and write an offline requirements.txt. "
        "Arguments after `--` go to `uv export`; --no-dev is added unless you pick dependency groups yourself.",
    )
    parser.add_argument("-d", "--dest", default="_dependencies", help="folder for the wheels (default: _dependencies)")
    parser.add_argument("-r", "--requirements", default="requirements.txt", help="requirements file to write")
    parser.add_argument("--no-requirements", action="store_true", help="only download, do not write requirements")
    parser.add_argument("--project", default=".", help="directory containing uv.lock (default: .)")
    parser.add_argument("--python", help="interpreter that will install the packages (default: `uv python find`)")
    parser.add_argument("-j", "--jobs", type=positive_int, default=8, help="parallel downloads (default: 8)")
    args, export_args = parser.parse_known_args(argv)
    args.export_args = build_export_args(export_args)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project)
    dest = Path(args.dest)

    export = args.export_args

    lock = tomllib.loads(run_uv("export", "--format", "pylock.toml", "--no-emit-project", *export, cwd=project))
    requirements = run_uv("export", "--format", "requirements.txt", "--no-hashes", *export, cwd=project)

    target = probe_target(args.python, project)
    try:
        downloads, notes = select(lock, target)
    except ValueError as e:
        sys.exit(f"error: {e}")
    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    store = Path(run_uv("cache", "dir").strip()) / "wheelhouse"
    dest.mkdir(parents=True, exist_ok=True)
    session = make_session()

    def work(item: Download) -> tuple[Download, str, str | None]:
        try:
            return item, fetch(session, item, dest, store), None
        except Exception as e:  # noqa: BLE001 - collect, so one bad file does not hide the rest
            return item, "", str(e) or type(e).__name__

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(work, downloads))

    sources = [source for _, source, error in results if error is None]
    print(
        f"{len(downloads)} files in {dest}: {sources.count('download')} downloaded, "
        f"{sources.count('cache')} from uv cache dir, {sources.count('present')} already present",
        file=sys.stderr,
    )
    failures = sorted(f"{item.filename}: {error}" for item, _, error in results if error is not None)
    if failures:
        print("failed:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1

    if not args.no_requirements:
        find_links = Path(os.path.relpath(dest, Path(args.requirements).resolve().parent)).as_posix()
        Path(args.requirements).write_text(f"--no-index\n--find-links {find_links}\n{requirements}", encoding="utf-8")
        print(f"wrote {args.requirements}", file=sys.stderr)
    return 0
