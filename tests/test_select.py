import pytest
from packaging.tags import parse_tag

from uv_wheelhouse.select import Target, select


def entry(name: str, h: str = "a" * 64) -> dict:
    return {"url": f"https://files.example/{name}", "hashes": {"sha256": h}}


def target(python="3.14.0", *tags: str) -> Target:
    """A Linux x86_64 CPython target; the whole marker environment is explicit so tests don't depend on the host."""
    env = {
        "implementation_name": "cpython",
        "implementation_version": python,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "6.0",
        "platform_system": "Linux",
        "platform_version": "1",
        "python_full_version": python,
        "python_version": python.rsplit(".", 1)[0],
        "sys_platform": "linux",
    }
    return Target(env=env, tags=[t for tag in tags for t in parse_tag(tag)])


LINUX = ("cp314-cp314-manylinux_2_28_x86_64", "cp314-abi3-manylinux_2_28_x86_64", "py3-none-any")


def lock(*packages: dict, requires_python: str = ">=3.14") -> dict:
    return {"requires-python": requires_python, "packages": list(packages)}


def test_picks_best_ranked_wheel_not_first_listed():
    pkg = {
        "name": "x",
        "wheels": [
            entry("x-1-py3-none-any.whl"),
            entry("x-1-cp314-cp314-win_amd64.whl"),
            entry("x-1-cp314-cp314-manylinux_2_28_x86_64.whl"),
        ],
        "sdist": entry("x-1.tar.gz"),
    }
    downloads, notes = select(lock(pkg), target("3.14.0", *LINUX))
    assert [d.filename for d in downloads] == ["x-1-cp314-cp314-manylinux_2_28_x86_64.whl"]
    assert notes == []


def test_marker_excludes_package():
    pkg = {"name": "winonly", "marker": "sys_platform == 'win32'", "wheels": [entry("w-1-py3-none-any.whl")]}
    assert select(lock(pkg), target("3.14.0", *LINUX))[0] == []


def test_falls_back_to_sdist_with_note():
    pkg = {"name": "x", "wheels": [entry("x-1-cp314-cp314-win_amd64.whl")], "sdist": entry("x-1.tar.gz")}
    downloads, notes = select(lock(pkg), target("3.14.0", *LINUX))
    assert [d.filename for d in downloads] == ["x-1.tar.gz"]
    assert "sdist" in notes[0]


def test_sdist_only_has_no_note():
    downloads, notes = select(lock({"name": "x", "sdist": entry("x-1.tar.gz")}), target("3.14.0", *LINUX))
    assert [d.filename for d in downloads] == ["x-1.tar.gz"] and notes == []


def test_local_directory_skipped_with_note():
    downloads, notes = select(lock({"name": "mine", "directory": {"path": "lib"}}), target("3.14.0", *LINUX))
    assert downloads == [] and "mine" in notes[0]


def test_no_compatible_file_is_an_error():
    pkg = {"name": "x", "wheels": [entry("x-1-cp314-cp314-win_amd64.whl")]}
    with pytest.raises(ValueError, match="x"):
        select(lock(pkg), target("3.14.0", *LINUX))


def test_requires_python_mismatch_is_an_error():
    with pytest.raises(ValueError, match="requires Python"):
        select(lock(requires_python=">=3.14"), target("3.13.2", *LINUX))


def test_missing_hash_is_an_error():
    pkg = {"name": "x", "wheels": [{"url": "https://files.example/x-1-py3-none-any.whl"}]}
    with pytest.raises(ValueError, match="sha256"):
        select(lock(pkg), target("3.14.0", *LINUX))


def test_filename_is_unquoted_from_url():
    pkg = {"name": "x", "sdist": entry("x%2B1.tar.gz")}
    assert select(lock(pkg), target("3.14.0", *LINUX))[0][0].filename == "x+1.tar.gz"


def test_prerelease_interpreter_satisfies_lower_bound():
    pkg = {"name": "x", "sdist": entry("x-1.tar.gz")}
    assert select(lock(pkg), target("3.14.0rc1", *LINUX))[0]


def test_package_requires_python_is_honoured():
    pkg = {"name": "old", "requires-python": "<3.14", "sdist": entry("old-1.tar.gz")}
    assert select(lock(pkg), target("3.14.0", *LINUX))[0] == []


@pytest.mark.parametrize("name", ["..%2F..%2Fevil.whl", "a%5Cb.whl", ".."])
def test_path_traversal_in_file_name_is_rejected(name):
    pkg = {"name": "x", "sdist": entry(name)}
    with pytest.raises(ValueError, match="file name"):
        select(lock(pkg), target("3.14.0", *LINUX))


@pytest.mark.parametrize("digest", ["../x", "A" * 64, "a" * 63])
def test_malformed_sha256_is_rejected(digest):
    pkg = {"name": "x", "sdist": entry("x-1.tar.gz", digest)}
    with pytest.raises(ValueError, match="sha256"):
        select(lock(pkg), target("3.14.0", *LINUX))


def test_wheel_without_url_is_a_value_error_not_key_error():
    pkg = {"name": "x", "wheels": [{"path": "x-1-py3-none-any.whl", "hashes": {"sha256": "a" * 64}}]}
    with pytest.raises(ValueError):
        select(lock(pkg), target("3.14.0", *LINUX))


def test_lock_without_packages_selects_nothing():
    assert select({"requires-python": ">=3.14"}, target("3.14.0", *LINUX)) == ([], [])
