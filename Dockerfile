FROM python:3.12-slim AS hr
WORKDIR /app
RUN groupadd -g 10000 avatar && useradd -u 10000 -g avatar avatar \
    && install -d -o avatar -g avatar /var/lib/hr \
    && touch /var/lib/hr/.volume
COPY requirements.txt .
RUN python -m venv /opt/avatar-venv && /opt/avatar-venv/bin/pip install --no-cache-dir -r requirements.txt
COPY hr_service ./hr_service
COPY avatar_contract ./avatar_contract
ENV HR_DATA_DIR=/var/lib/hr PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER avatar
CMD ["/opt/avatar-venv/bin/python", "-m", "uvicorn", "hr_service.app:app", "--host", "0.0.0.0", "--port", "8891"]

FROM nousresearch/hermes-agent:v0.23.1 AS avatar
USER root
WORKDIR /app
RUN install -d -o hermes -g hermes /var/lib/avatar \
    && touch /var/lib/avatar/.volume
COPY requirements.txt .
RUN python3 -m venv /opt/avatar-venv && /opt/avatar-venv/bin/pip install --no-cache-dir -r requirements.txt
COPY avatar_service ./avatar_service
COPY avatar_contract ./avatar_contract
COPY deploy/hr-contract.json ./deploy/hr-contract.json
COPY --chmod=755 deploy/avatar-hermes /usr/local/bin/avatar-hermes
RUN sed -i 's/\r$//' /usr/local/bin/avatar-hermes
COPY deploy/pair_hr.py ./deploy/pair_hr.py
ENV PY_AVATAR_DATA_DIR=/var/lib/avatar HOME=/var/lib/avatar HERMES_HOME=/var/lib/avatar/hermes PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER hermes
RUN HERMES_HOME=/tmp/avatar-acp-check /usr/local/bin/avatar-hermes acp --check
ENTRYPOINT []
CMD ["/opt/avatar-venv/bin/python", "-m", "uvicorn", "avatar_service.app:app", "--host", "0.0.0.0", "--port", "8788", "--timeout-graceful-shutdown", "5"]
