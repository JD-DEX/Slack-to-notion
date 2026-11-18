#
# docker build . -t send-to-notion
# docker run --env-file .env send-to-notion
#
# Pinned by tag and digest. To update: pick a new python:3.13.x-slim-trixie tag
# and replace both the tag and the digest.
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged user with a fixed UID and no login shell.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Hash-checked, wheel-only install of the locked dependencies.
COPY requirements.txt .
RUN pip install --require-hashes --only-binary :all: --no-deps -r requirements.txt

# Application code stays owned by root and is read-only for the app user.
COPY main.py .
COPY app/ ./app/

USER 10001:10001

# Exec form, so Python receives SIGTERM directly from the platform.
ENTRYPOINT ["python", "main.py"]
