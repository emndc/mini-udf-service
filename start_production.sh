#!/bin/bash
# ═════════════════════════════════════════════════════════════════════
# Start Mini UDF Service (Production)
# Usage: ./start_production.sh
# ═════════════════════════════════════════════════════════════════════

set -e  # Exit on error

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Color

echo -e "${GREEN}Mini UDF Service - Production Startup${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check .env file
if [ ! -f .env ]; then
    echo -e "${RED}✗ .env file not found!${NC}"
    echo "  Please run: cp .env.example .env"
    exit 1
fi

# Load environment variables
source .env

echo -e "${GREEN}✓ Configuration loaded${NC}"

# Check API key
if [ -z "$API_SECRET_KEY" ] || [ "$API_SECRET_KEY" = "your-super-secret-api-key-here-generate-strong-key" ]; then
    echo -e "${RED}✗ API_SECRET_KEY not configured!${NC}"
    echo "  Generate one: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    exit 1
fi

echo -e "${GREEN}✓ API key configured${NC}"

# Create logs directory
mkdir -p logs
echo -e "${GREEN}✓ Logs directory ready${NC}"

# Check for required packages
if ! python -c "import gunicorn" 2>/dev/null; then
    echo -e "${YELLOW}! Installing dependencies...${NC}"
    pip install -q -r requirements-prod.txt
fi

echo -e "${GREEN}✓ Dependencies ready${NC}"

# Start Gunicorn
WORKERS=${GUNICORN_WORKERS:-4}
THREADS=${GUNICORN_THREADS:-2}
TIMEOUT=${GUNICORN_TIMEOUT:-30}

echo ""
echo -e "${YELLOW}Starting Gunicorn with:${NC}"
echo "  Workers: $WORKERS"
echo "  Threads: $THREADS"
echo "  Timeout: ${TIMEOUT}s"
echo "  Host: ${HOST}:${PORT}"
echo ""

gunicorn \
    -w ${WORKERS} \
    --threads ${THREADS} \
    -b ${HOST}:${PORT} \
    --timeout ${TIMEOUT} \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    mini_udf_service_secure:app

echo -e "${GREEN}✓ Service started${NC}"
