# syntax=docker/dockerfile:1

# ---------- Stage 1: Builder ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a virtual environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create non-root user
RUN groupadd -r skyn3t && useradd -r -g skyn3t skyn3t

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY skyn3t/ ./skyn3t/
COPY scripts/ ./scripts/

# Create data directory with correct permissions
RUN mkdir -p /app/data && chown -R skyn3t:skyn3t /app

# Switch to non-root user
USER skyn3t

# Expose application port
EXPOSE 6660

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:6660/health')" || exit 1

# Default command runs in daemon mode
CMD ["python", "-m", "uvicorn", "skyn3t.web.app:app", "--host", "0.0.0.0", "--port", "6660"]
