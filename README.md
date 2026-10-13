# uv-wheel-download

A pip-free replacement for the `pip download` step in our Azure pipelines. It reads `uv.lock`, downloads every
pinned wheel (sdist only if no wheel fits) into a folder, and writes a `requirements.txt` that installs from that folder
only.

```yaml
# before: uv export + pip download + PowerShell "Adjust requirements.txt"
- script: uvx --from git+https://github.com/bmsuisse/uv-wheel-download uv-wheel-download -d _dependencies -r requirements.txt
  displayName: Download dependencies
  env:
    SYSTEM_ACCESSTOKEN: $(System.AccessToken) # for the private BMS feed; ~/.netrc works too
```

Result: `_dependencies/*.whl` and a `requirements.txt` starting with `--no-index` / `--find-links _dependencies`,
followed by the usual `uv export --no-hashes` content.

## How it works

1. `uv export --frozen --format pylock.toml` gives all files in `uv.lock` with URL and sha256 (`--frozen`: the lock is
   trusted as is, no re-resolve; pass `--locked` after `--` to have uv verify it instead).
2. The target interpreter (`--python`, default `uv python find` in the project) is run directly to get its markers and
   compatible wheel tags (no index or network access needed), and one file per package is picked (marker-excluded packages are skipped).
3. Files are fetched in parallel, sha256-verified, and kept in `<uv cache dir>/wheel-download/<sha256>/`. Whatever already
   persists `UV_CACHE_DIR` between runs also persists the wheels, and a hit costs no network. They are then hard-linked
   into the destination.

uv's own cache only holds unpacked archives, not `.whl` files, so it can't be reused directly; this store lives next to
it and follows `UV_CACHE_DIR`.

Auth: `SYSTEM_ACCESSTOKEN` is sent as `VssSessionToken:<token>` to `https://pkgs.dev.azure.com` only; any other host (or Azure
without the variable) uses `~/.netrc`.

Local/editable packages (workspace members, `-e .`) are not downloaded; they stay in `requirements.txt` as uv exports
them.

## Options

```
uv-wheel-download [-d DEST] [-r REQUIREMENTS | --no-requirements] [--project DIR] [--python PY] [-j N] [-- <uv export args>]
```

Everything after `--` goes to `uv export`, e.g. `-- --package mdmapp` or `-- --group build`. `--no-dev` is added unless
you choose groups yourself (`--dev`, `--only-dev`, `--all-groups`, ...).

## Development

`just install`, then `just test-all` (ruff, ty, pytest).
