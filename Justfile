# Install project & fix entry point
install:
  uv sync

# Install development dependencies
install-dev:
  uv sync --all-extras

# Run all tests
test:
  uv run pytest -v --no-cov

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

# Check code quality
check: lint format type

# Run all pre-commit hooks
pre:
  uv run prek run --all-files

# Run the application
run:
  uv run python src/brewery/cli/main.py

# Clean up temporary files
clean:
  @which python3 > /dev/null && uv run python3 src/brewery/scripts/clean.py || uv run python src/brewery/scripts/clean.py
