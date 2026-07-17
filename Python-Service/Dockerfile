# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CACHE_DIR=./tmp/pip-cache

WORKDIR /app

COPY requirements.txt ./

RUN --mount=type=cache,target=./tmp/pip-cache \
    pip install --upgrade pip && \
    pip install -r requirements.txt

COPY . .

EXPOSE 9002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9002"]
