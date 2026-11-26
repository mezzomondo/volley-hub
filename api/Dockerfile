FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY volleyhub ./volleyhub
RUN uv pip install --system --no-cache .
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "volleyhub.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
