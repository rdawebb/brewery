# Install project & fix entry point
install:
  uv sync && uv run scripts/fix_entry_point.py

# Install development dependencies
install-dev:
  uv sync --all-extras && uv run scripts/fix_entry_point.py

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
check:
  uv run ruff format src tests
  uv run ruff check src tests
  uv run ty check src tests

# Run all pre-commit hooks
pre:
  uv run prek run --all-files

# Run the application
run:
  uv run python src/brewery/cli/main.py

# Clean up temporary files
clean:
  @which python3 > /dev/null && uv run python3 scripts/clean.py || uv run python scripts/clean.py
