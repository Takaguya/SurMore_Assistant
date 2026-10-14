# One container serves the whole app: the API plus the built frontend on the same origin.
#   docker build -t surmore-assistant .
#   docker run -p 8000:8000 --env-file backend/.env surmore-assistant

# ---- Build the frontend
FROM node:24-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Run the API (and serve the built frontend)
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# Run as an unprivileged user; data/ only matters with STORAGE_BACKEND=local.
RUN useradd --create-home --uid 10001 surmore && mkdir -p /app/backend/data && chown surmore /app/backend/data
USER surmore

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/api/health', timeout=4)"

# Hosts like Render, Railway and Cloud Run tell the app which port to use through $PORT.
# One worker on purpose: rate limits are kept in memory per process.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
