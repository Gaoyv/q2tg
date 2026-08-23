FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 efb \
    && install -d -o efb -g efb /home/efb/.ehforwarderbot /home/efb/.ehforwarderbot/modules

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY efb_qq_napcat /app/efb_qq_napcat

RUN pip install --no-cache-dir "efb-telegram-master==2.3.1" .

USER efb
CMD ["ehforwarderbot", "--profile", "default"]
