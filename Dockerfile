# ── Build stage ────────────────────────────────────────────────────────────────
# Use a slim image to keep final size small (~150 MB vs ~900 MB for full Python)
FROM python:3.12-slim AS builder

WORKDIR /app

# Copy and install dependencies first so this layer is cached between code-only
# changes — Docker won't re-run pip install unless requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create a non-root user — running as root in a container is a security risk.
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY --chown=appuser:appgroup app/ ./app/

# Switch to non-root user
USER appuser

# Document the port — Railway / Docker Compose will honour EXPOSE when mapping
EXPOSE 8000

# Health check so Docker and Railway can detect a crashed container quickly
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Railway injects $PORT at runtime; fall back to 8000 for local Docker runs.
# --workers 1 keeps the in-memory store consistent (scale horizontally via Redis).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]