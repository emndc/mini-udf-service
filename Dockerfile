# ═════════════════════════════════════════════════════════════════════
# Dockerfile for Mini UDF Service
# ═════════════════════════════════════════════════════════════════════
# Build: docker build -t mini-udf-service:latest .
# Run:   docker run -p 5055:5055 --env-file .env mini-udf-service:latest
# ═════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# Copy application
COPY mini_udf_service_secure.py .
COPY ../ /app/

# Create logs directory
RUN mkdir -p logs

# Expose port
EXPOSE 5055

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5055/health')"

# Start application
CMD ["gunicorn", \
     "-w", "4", \
     "--threads", "2", \
     "-b", "0.0.0.0:5055", \
     "--timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "mini_udf_service_secure:app"]
