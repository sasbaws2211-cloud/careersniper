FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations migrations
COPY app app
COPY admin admin
COPY data/candidates.txt data/candidates.txt

# run as an unprivileged user
RUN useradd --system --uid 10001 --no-create-home careersniper && chown -R careersniper /srv
USER careersniper

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status == 200 else 1)"

# Apply database migrations, then serve. One worker: the scheduler and in-memory rate limits assume a single process
# (the scheduler is also protected by a database lock). --proxy-headers makes client IPs real behind the reverse proxy,
# so the app must NOT be published directly to the internet (docker-compose.prod.yml only exposes it to Caddy).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers 1"]
