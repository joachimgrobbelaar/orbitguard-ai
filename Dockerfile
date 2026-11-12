# Hugging Face Docker Space: https://huggingface.co/docs/hub/spaces-sdks-docker
# The container is started as uid 1000. Listen on 0.0.0.0:7860.
FROM python:3.11-slim

RUN useradd -m -u 1000 user

USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/home/user/app/src \
    PORT=7860 \
    ORBITGUARD_CACHE_DIR=/tmp/orbitguard \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR $HOME/app

# git is required to install the pinned NanoDynamics URL.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/orbitguard \
    && chown user:user /tmp/orbitguard
USER user

COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir --user --upgrade pip \
    && pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860

CMD ["python", "-m", "uvicorn", "orbitguard.main:app", "--host", "0.0.0.0", "--port", "7860"]
