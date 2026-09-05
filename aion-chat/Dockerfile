# 固定 Linux amd64 / Python 3.11.15，与后端依赖锁一致。
FROM python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 公开仓库不提交二进制依赖；按同一版本和散列从安装包源获取。
COPY aion-chat/requirements.txt aion-chat/requirements.lock ./
RUN pip install --only-binary=:all: --require-hashes -r requirements.lock

COPY aion-chat/ /app/
COPY public/ /public/
RUN mkdir -p /app/data

EXPOSE 18080

CMD ["python", "-u", "main.py"]
