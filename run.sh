set -e

echo "Installing host dependencies..."
python3 -m pip install -q --break-system-packages -r requirements.txt 2>/dev/null || python3 -m pip install -q -r requirements.txt

echo "Checking Docker daemon..."
docker info >/dev/null

echo "Warming Docker base image cache..."
docker pull python:3.11-slim >/dev/null

echo "Running Python import smoke checks..."
python3 -m compileall -q shared services

echo "Starter repository is ready. Pipeline authoring is the candidate task."
