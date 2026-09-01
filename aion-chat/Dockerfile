FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY aion-chat/requirements.txt ./
RUN pip install -r requirements.txt

COPY aion-chat/ /app/
COPY public/ /public/
RUN mkdir -p /app/data

EXPOSE 18080

CMD ["python", "-u", "main.py"]
