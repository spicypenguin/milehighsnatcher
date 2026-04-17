# MileHighSnatcher — JAL First Class JFK→HND award monitor
# Single-container image: runs scheduler.py as a daemon that fires monitor.py
# at 07:00 and 19:00 in the configured timezone.
#
# Build:
#   docker build -t milessnatcher .
#
# Run (one-shot manual check):
#   docker run --rm --env-file .env -e DATA_DIR=/data \
#     -v $(pwd)/data:/data milessnatcher python3 monitor.py
#
# Run as daemon (use docker-compose.yml for Portainer instead):
#   docker run -d --env-file .env -e DATA_DIR=/data \
#     -v $(pwd)/data:/data milessnatcher

FROM python:3.12-slim

# Install tzdata so TZ= env var works correctly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py scheduler.py ./

# /data is a volume mount for persistent state (seen_flights.json, logs/).
# Map a host directory here so data survives container restarts and rebuilds.
VOLUME ["/data"]

ENV SEATS_AERO_API_KEY=""
ENV DISABLE_MACOS_NOTIFICATIONS=true
ENV DATA_DIR=/data
ENV TZ=UTC

# Optional: override schedule via comma-separated 24h times (e.g. "08:00,20:00")
ENV MONITOR_RUN_TIMES="07:00,19:00"

CMD ["python3", "-u", "scheduler.py"]
