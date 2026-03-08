FROM python:3.13-slim

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Use system Python, don't download another version
ENV UV_PYTHON_PREFERENCE=only-system

WORKDIR /app

# Copy dependency files first for Docker layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies using uv (respects pyproject.toml platform markers)
RUN uv sync --frozen --no-dev --no-install-project

# Copy project source
COPY . .

CMD ["uv", "run", "python", "bot.py"]
