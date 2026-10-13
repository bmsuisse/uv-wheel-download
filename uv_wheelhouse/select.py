"""Decide which files of a pylock.toml (``uv export --format pylock.toml``) to download for one interpreter."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.utils import parse_wheel_filename


@dataclass(frozen=True)
class Target:
    env: dict[str, str]  # PEP 508 marker environment of the interpreter that will install
    tags: list[Tag]  # its compatible wheel tags, best first (`packaging.tags.sys_tags()` order)


@dataclass(frozen=True)
class Download:
    package: str
    filename: str
    url: str
    sha256: str


def _as_download(package: str, entry: dict) -> Download:
    url = entry.get("url")
    if not url:
        raise ValueError(f"{package}: only url-based files are supported (got {sorted(entry)})")
    sha256 = entry.get("hashes", {}).get("sha256")
    if not sha256:
        raise ValueError(f"{package}: lock entry for {url} has no sha256")
    filename = entry.get("name") or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return Download(package, filename, url, sha256)


def _best_wheel(wheels: list[dict], rank: dict[Tag, int]) -> dict | None:
    best, best_rank = None, len(rank)
    for wheel in wheels:
        filename = wheel.get("name") or unquote(urlsplit(wheel["url"]).path.rsplit("/", 1)[-1])
        ranks = [rank[t] for t in parse_wheel_filename(filename)[3] if t in rank]
        if ranks and min(ranks) < best_rank:
            best, best_rank = wheel, min(ranks)
    return best


def select(lock: dict, target: Target) -> tuple[list[Download], list[str]]:
    """Return the files to fetch plus human-readable notes about packages that are skipped."""
    requires_python = lock.get("requires-python")
    if requires_python and not SpecifierSet(requires_python).contains(
        target.env["python_full_version"], prereleases=True
    ):
        raise ValueError(
            f"uv.lock requires Python {requires_python}, but the target interpreter is {target.env['python_full_version']}"
        )

    rank: dict[Tag, int] = {}
    for i, tag in enumerate(target.tags):
        rank.setdefault(tag, i)

    downloads: list[Download] = []
    notes: list[str] = []
    for pkg in lock["packages"]:
        name = pkg["name"]
        if "marker" in pkg and not Marker(pkg["marker"]).evaluate(target.env):
            continue
        wheel = _best_wheel(pkg.get("wheels", []), rank)
        if wheel is not None:
            downloads.append(_as_download(name, wheel))
        elif "sdist" in pkg:
            downloads.append(_as_download(name, pkg["sdist"]))
            if pkg.get("wheels"):
                notes.append(f"{name}: no compatible wheel, downloading sdist (needs a build at install time)")
        elif "archive" in pkg:
            downloads.append(_as_download(name, pkg["archive"]))
        elif "directory" in pkg or "vcs" in pkg:
            notes.append(f"{name}: local/VCS source, not downloaded (installed from source)")
        else:
            raise ValueError(f"{name}: no wheel compatible with the target interpreter and no sdist")
    return downloads, notes
