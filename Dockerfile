# Dockerfile for DocIntel MCP Server
FROM python:3.11-slim

# Prevent Python from writing bytecode and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY server.py pyproject.toml LICENSE README.md ./

# Default entrypoint runs server via stdio MCP protocol
ENTRYPOINT ["python3", "server.py"]
