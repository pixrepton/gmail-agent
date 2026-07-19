FROM python:3.12-slim

ARG INSTALL_DOCLING=0
ARG INSTALL_PHP=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tesseract-ocr \
        tesseract-ocr-pol \
        libgl1 \
        libglib2.0-0 \
    && if [ "$INSTALL_PHP" = "1" ]; then apt-get install -y --no-install-recommends php-cli; fi \
    && rm -rf /var/lib/apt/lists/*

COPY tools/gmail_audit/requirements.txt /tmp/gmail-audit-requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/gmail-audit-requirements.txt \
    && if [ "$INSTALL_DOCLING" = "1" ]; then python -m pip install docling; fi

COPY . /app

CMD ["python", "tools/gmail_audit/gmail_intake.py", "doctor", "--skip-gmail", "--verbose"]
