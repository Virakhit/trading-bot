FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app migrations scripts data README.md alembic.ini PHASE1_AUDIT.md PHASE2_AUDIT.md PHASE3_AUDIT.md FINAL_SYSTEM_AUDIT.md ./
RUN useradd --create-home --uid 10001 trader && mkdir -p /app/data /app/logs && chown -R trader:trader /app
USER trader
HEALTHCHECK --interval=30s --timeout=5s CMD python -m app.cli doctor
CMD ["python", "-m", "app.worker"]
