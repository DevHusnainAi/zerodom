#!/usr/bin/env bash
# ZeroDOM Demo Runner - Side-by-side failure vs success comparison
# Starts mock SPA server, runs baseline failure demo, then ZeroDOM runner
set -euo pipefail

RED='\033[91m'
GREEN='\033[92m'
BLUE='\033[94m'
YELLOW='\033[93m'
RESET='\033[0m'

echo -e "${BLUE}========================================${RESET}"
echo -e "${BLUE}ZeroDOM Demonstration Suite${RESET}"
echo -e "${BLUE}========================================${RESET}"

# Check dependencies
command -v python3 >/dev/null 2>&1 || { echo "python3 required"; exit 1; }

# Use uv if available, otherwise fallback to venv python
if command -v uv >/dev/null 2>&1; then
    PYTHON="uv run python"
    echo -e "${BLUE}Using uv for execution${RESET}"
    # Install demo dependencies into uv environment
    echo -e "${YELLOW}Installing demo dependencies...${RESET}"
    uv pip install -r requirements.txt >/dev/null 2>&1 || true
    # Ensure playwright browser is installed
    echo -e "${YELLOW}Ensuring Playwright Chromium is installed...${RESET}"
    uv run playwright install chromium >/dev/null 2>&1 || true
else
    # Try repo .venv
    if [ -f "/home/husnain/storage/Files/zerodom/.venv/bin/python" ]; then
        PYTHON="/home/husnain/storage/Files/zerodom/.venv/bin/python"
    else
        PYTHON="python3"
    fi
    echo -e "${YELLOW}Using $PYTHON${RESET}"
    # Install deps via pip if possible
    pip install -r requirements.txt >/dev/null 2>&1 || true
fi

# Kill any existing server on port 8080
fuser -k 8080/tcp 2>/dev/null || true
sleep 0.5

# Start server in background
echo -e "${BLUE}Starting mock SPA server on http://localhost:8080...${RESET}"
$PYTHON server.py &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Wait for server to be ready
echo -e "${YELLOW}Waiting for server to start...${RESET}"
for i in {1..30}; do
    if curl -s http://localhost:8080/health >/dev/null 2>&1; then
        echo -e "${GREEN}Server ready!${RESET}"
        break
    fi
    sleep 0.5
done

# Ensure server is actually up
if ! curl -s http://localhost:8080/health >/dev/null 2>&1; then
    echo -e "${RED}Server failed to start${RESET}"
    kill $SERVER_PID 2>/dev/null || true
    exit 1
fi

echo ""
echo -e "${BLUE}========================================${RESET}"
echo -e "${BLUE}PART 1: Baseline Failure Demo${RESET}"
echo -e "${BLUE}========================================${RESET}"
$PYTHON fail_baseline.py || true

echo ""
echo -e "${BLUE}========================================${RESET}"
echo -e "${BLUE}PART 2: ZeroDOM Runner${RESET}"
echo -e "${BLUE}========================================${RESET}"
$PYTHON zerodom_runner.py

# Cleanup
echo ""
echo -e "${YELLOW}Cleaning up server...${RESET}"
kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true

echo -e "${GREEN}Demo complete!${RESET}"
echo -e "${BLUE}========================================${RESET}"
