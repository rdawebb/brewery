# Install project
install:
    uv sync

# Install development dependencies
install-dev:
    uv sync --all-extras

# Run all tests except the slow brew-differential ones (deselected by addopts)
test:
    uv run pytest -n auto --dist loadfile --no-cov

# Run every test, slow included; a command-line -m replaces the addopts one
test-all:
    uv run pytest -m "" -v --no-cov

# Run only unit tests
test-unit:
    uv run pytest tests/unit -v --no-cov

# Run only integration tests
test-int:
    uv run pytest tests/integration -v --no-cov

# Run only CLI tests
test-cli:
    uv run pytest tests/cli -v --no-cov

# Run all tests with coverage
test-cov:
    uv run pytest --cov=src --cov-report=html --cov-report=term

# Lint code
lint:
    uv run ruff check src tests

# Format code
format:
    uv run ruff format src tests

# Type check code
type:
    uv run ty check src tests

# Check the one-way layering rule
layers:
    uv run lint-imports

# Check code quality
check: lint format type layers

# Run all pre-commit hooks
pre:
    uv run prek run --all-files

# Clean up temporary files
clean:
    @which python3 > /dev/null && uv run python3 src/brewery/scripts/clean.py || uv run python src/brewery/scripts/clean.py
