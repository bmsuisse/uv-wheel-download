install:
    uv sync --all-groups

lint:
    uv run ruff check .

fmt:
    uv run ruff format .

check:
    uv run ty check uv_wheel_download

test:
    uv run -m pytest -q

test-all: lint check test

build:
    uv build
