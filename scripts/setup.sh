#!/usr/bin/env bash
# =============================================================================
# SkyN3t First-Time Setup Script
# =============================================================================
# Installs dependencies, creates data directories, and prepares the environment.
# Usage:
#   ./scripts/setup.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_CMD="${PYTHON_CMD:-python3.11}"

echo "🤖 SkyN3t Setup"
echo "================"

# Check Python version
if ! command -v "$PYTHON_CMD" &>/dev/null; then
    echo "❌ Python 3.11 not found. Please install Python 3.11 or set PYTHON_CMD."
    exit 1
fi

PYTHON_VERSION=$($PYTHON_CMD --version 2>&1 | awk '{print $2}')
echo "✅ Found Python $PYTHON_VERSION"

# Create virtual environment
echo "📦 Creating virtual environment at $VENV_DIR ..."
if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# Upgrade pip
echo "⬆️  Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "📚 Installing dependencies..."
if [[ -f "${PROJECT_DIR}/requirements.txt" ]]; then
    pip install -r "${PROJECT_DIR}/requirements.txt"
else
    echo "⚠️  requirements.txt not found. Installing core packages..."
    pip install fastapi uvicorn redis
fi

# Create data directories
echo "📂 Creating data directories..."
mkdir -p "${PROJECT_DIR}/data/vector_store"
mkdir -p "${PROJECT_DIR}/data/logs"

# Copy .env.example if .env doesn't exist
if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    echo "📝 Creating .env from template..."
    cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
    echo "⚠️  Please edit ${PROJECT_DIR}/.env and add your API keys."
else
    echo "✅ .env already exists, skipping."
fi

# Make scripts executable
chmod +x "${PROJECT_DIR}/scripts/run.sh"

echo ""
echo "🎉 Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit ${PROJECT_DIR}/.env with your configuration."
echo "  2. Start SkyN3t: ./scripts/run.sh [web|cli|daemon]"
echo ""
