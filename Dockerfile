FROM python:3.12-slim

# openssh-client is needed for paramiko host-key / ssh sanity checks (optional but useful)
RUN apt-get update \
  && apt-get install -y --no-install-recommends openssh-client \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fan_controller.py watchdog.py ./

# Default config path inside container. Mount your real config at /config/config.yaml
ENV NASTEMP_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

# Default = controller. Watchdog service overrides CMD (see docker-compose.yml)
CMD ["python", "fan_controller.py"]
