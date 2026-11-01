# Standalone image for the bot (Option B). Most people should instead run the
# bot inside their existing claude-code-web container — see README.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git nodejs npm ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && claude --version || echo "claude CLI install skipped"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python3", "bot.py"]
