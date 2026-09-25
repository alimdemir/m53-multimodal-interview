FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-models.txt ./
ARG INSTALL_MODEL_RUNTIME=1
RUN if [ "$INSTALL_MODEL_RUNTIME" = "1" ]; then pip install --no-cache-dir -r requirements-models.txt; else pip install --no-cache-dir -r requirements.txt; fi

COPY . .
RUN chmod +x deploy/entrypoint.sh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
