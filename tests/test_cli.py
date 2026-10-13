import hashlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from packaging.tags import parse_tag

from uv_wheel_download import cli
from uv_wheel_download.select import Target

WHEEL = b"pretend wheel"
SHA = hashlib.sha256(WHEEL).hexdigest()


def test_default_export_args_exclude_dev_and_freeze():
    assert cli.build_export_args([]) == ["--no-dev", "--frozen"]


def test_group_selection_keeps_no_dev_default():
    # `--group build` alone must not pull in the dev group
    assert cli.build_export_args(["--", "--group", "build"]) == ["--no-dev", "--group", "build", "--frozen"]


@pytest.mark.parametrize("flag", ["--only-dev", "--all-groups", "--dev", "--no-default-groups", "--no-dev"])
def test_explicit_group_flags_are_left_alone(flag):
    assert cli.build_export_args([flag]) == [flag, "--frozen"]


def test_locked_replaces_frozen():
    assert cli.build_export_args(["--locked"]) == ["--no-dev", "--locked"]


def test_parse_args_splits_ours_from_uv_export():
    args = cli.parse_args(["-d", "deps", "--", "--package", "mdmapp"])
    assert args.dest == "deps" and args.export_args == ["--no-dev", "--package", "mdmapp", "--frozen"]


def test_jobs_must_be_positive():
    with pytest.raises(SystemExit):
        cli.parse_args(["-j", "0"])


def test_relative_python_is_made_absolute(tmp_path: Path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").touch()
    monkeypatch.chdir(tmp_path)
    assert cli.resolve_python("bin/python") == str(tmp_path / "bin" / "python")
    assert cli.resolve_python("3.14") == "3.14"  # a version request, not a path
    assert cli.resolve_python(None) is None


def test_token_only_sent_to_ado_over_https(monkeypatch):
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", "secret")
    assert cli.auth_for("https://pkgs.dev.azure.com/o/x.whl") == ("VssSessionToken", "secret")
    assert cli.auth_for("http://pkgs.dev.azure.com/o/x.whl") is None
    assert cli.auth_for("https://files.pythonhosted.org/x.whl") is None
    assert cli.auth_for("https://pkgs.dev.azure.com.evil.example/x.whl") is None
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN")
    assert cli.auth_for("https://pkgs.dev.azure.com/o/x.whl") is None


def test_probe_target_reports_the_given_interpreter(tmp_path: Path):
    target = cli.probe_target(sys.executable, tmp_path)
    assert target.env["python_full_version"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")
    assert target.tags


@pytest.fixture
def wheel_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path.endswith("good-1-py3-none-any.whl") else 404)
            self.send_header("Content-Length", str(len(WHEEL)))
            self.end_headers()
            self.wfile.write(WHEEL)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def run_main(monkeypatch, tmp_path: Path, base: str, filenames: list[str]) -> int:
    packages = "".join(
        f'[[packages]]\nname = "{n.split("-")[0]}"\nversion = "1"\n'
        f'wheels = [{{ url = "{base}/{n}", hashes = {{ sha256 = "{SHA}" }} }}]\n\n'
        for n in filenames
    )
    outputs = {
        "pylock.toml": f'lock-version = "1.0"\nrequires-python = ">=3.11"\n\n{packages}',
        "requirements.txt": "good==1\n",
    }

    def fake_uv(*args: str, cwd=None) -> str:
        if args[0] == "export":
            return outputs[args[args.index("--format") + 1]]
        assert args[:2] == ("cache", "dir")
        return str(tmp_path / "uvcache")

    target = Target(
        env={"python_full_version": "3.14.0"},
        tags=[t for tag in ["py3-none-any"] for t in parse_tag(tag)],
    )
    monkeypatch.setattr(cli, "run_uv", fake_uv)
    monkeypatch.setattr(cli, "probe_target", lambda python, project: target)
    monkeypatch.chdir(tmp_path)
    return cli.main(["-d", "deps", "-r", "out/requirements.txt"])


def test_main_downloads_and_writes_offline_requirements(monkeypatch, tmp_path: Path, wheel_server):
    (tmp_path / "out").mkdir()
    assert run_main(monkeypatch, tmp_path, wheel_server, ["good-1-py3-none-any.whl"]) == 0
    assert (tmp_path / "deps" / "good-1-py3-none-any.whl").read_bytes() == WHEEL
    assert (tmp_path / "out" / "requirements.txt").read_text() == "--no-index\n--find-links ../deps\ngood==1\n"
    assert (tmp_path / "uvcache" / "wheel-download" / SHA / "good-1-py3-none-any.whl").exists()


def test_main_reports_all_failures_and_writes_no_requirements(monkeypatch, tmp_path: Path, wheel_server, capsys):
    (tmp_path / "out").mkdir()
    names = ["good-1-py3-none-any.whl", "bad-1-py3-none-any.whl", "worse-1-py3-none-any.whl"]
    assert run_main(monkeypatch, tmp_path, wheel_server, names) == 1
    err = capsys.readouterr().err
    assert "bad-1-py3-none-any.whl" in err and "worse-1-py3-none-any.whl" in err
    assert not (tmp_path / "out" / "requirements.txt").exists()
    assert (tmp_path / "deps" / "good-1-py3-none-any.whl").exists()
