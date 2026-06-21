FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached separately from source)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Persistent directories (mount as volumes at runtime)
VOLUME ["/config", "/data", "/media"]

# Default command: daemon mode, reading everything from mounted volumes.
# Override individual flags with:  docker run ... --watch --config /config/config.json
CMD ["python", "main.py", \
     "--config", "/config/config.json", \
     "--db",     "/data/watcher.db",    \
     "--output", "/media",              \
     "--watch"]
