FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STATE_DIR=/app/state
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home worker
COPY lead_agent ./lead_agent
RUN mkdir /app/state && chown worker:worker /app/state
USER worker
CMD ["python", "-m", "lead_agent.worker"]
