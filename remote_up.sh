#!/usr/bin/env bash
# remote_up.sh — One-command deploy for Vaani voice agent
# Usage: ./remote_up.sh [--build]
#
# This script is meant to be run ON THE VM (either via SSH or gcloud compute ssh).
# It installs Docker if missing, pulls the code, and starts the containers.
set -euo pipefail

REPO_URL="https://github.com/Vishnuuss/vaani.git"
APP_DIR="$HOME/vaani"
BUILD_FLAG=""

for arg in "$@"; do
  case "$arg" in
    --build) BUILD_FLAG="--build" ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

echo "═══════════════════════════════════════════════════════"
echo "  Vaani Voice Agent — Remote Deployment"
echo "═══════════════════════════════════════════════════════"

# ── 1. Install Docker if not present ─────────────────────
if ! command -v docker &>/dev/null; then
  echo "[1/5] Installing Docker..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "  ✅ Docker installed"
else
  echo "[1/5] Docker already installed ✅"
fi

# ── 2. Install Docker Compose plugin if not present ──────
if ! docker compose version &>/dev/null; then
  echo "[2/5] Installing Docker Compose plugin..."
  sudo apt-get install -y -qq docker-compose-plugin
  echo "  ✅ Docker Compose installed"
else
  echo "[2/5] Docker Compose already installed ✅"
fi

# ── 3. Clone or update the repo ─────────────────────────
if [ -d "$APP_DIR/.git" ]; then
  echo "[3/5] Updating existing repo..."
  cd "$APP_DIR"
  git pull --ff-only
else
  echo "[3/5] Cloning repo..."
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

# ── 4. Check .env.production exists ─────────────────────
if [ ! -f "$APP_DIR/.env.production" ]; then
  echo ""
  echo "⚠️  .env.production not found!"
  echo "   Copy your .env file to $APP_DIR/.env.production"
  echo "   Then run this script again."
  exit 1
fi
echo "[4/5] .env.production found ✅"

# ── 5. Build and start ──────────────────────────────────
echo "[5/5] Starting containers..."
cd "$APP_DIR"
sudo docker compose up -d $BUILD_FLAG

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅ Vaani deployed!"
echo ""
echo "  Health check:  http://$(curl -s ifconfig.me):8090/health"
echo "  Answer URL:    http://$(curl -s ifconfig.me):80/answer"
echo "  WebSocket:     ws://$(curl -s ifconfig.me):80/ws"
echo ""
echo "  Logs:          sudo docker compose logs -f gateway"
echo "═══════════════════════════════════════════════════════"
