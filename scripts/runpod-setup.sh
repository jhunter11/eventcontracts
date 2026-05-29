#!/bin/bash
# scripts/runpod-setup.sh
# 
# Initialization script for an Ubuntu-based RunPod instance.
# Provisions the environment for the eventcontracts Python/Rust framework.

set -e # Exit immediately if a command fails
set -o pipefail

echo "===================================================="
echo "🚀 Starting eventcontracts RunPod Setup"
echo "===================================================="

# 1. Update system and install core dependencies
echo "📦 Installing system dependencies..."
apt-get update -y
apt-get install -y \
    build-essential \
    curl \
    wget \
    git \
    pkg-config \
    libssl-dev \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    htop \
    linux-tools-common \
    linux-tools-generic

# 2. CPU Tuning (Attempt to lock CPU to performance mode for 5GHz)
echo "⚡ Tuning CPU Governor for High Performance..."
if command -v cpupower &> /dev/null; then
    cpupower frequency-set -g performance || echo "⚠️ Could not set CPU governor (requires privileged container). Moving on."
else
    echo "⚠️ cpupower not found or not permitted in this container."
fi

# 3. Install Rust (for the ultra-low latency live runner)
if ! command -v cargo &> /dev/null; then
    echo "🦀 Installing Rust..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
else
    echo "🦀 Rust is already installed. Updating..."
    rustup update
fi

# 4. Setup Python Virtual Environment
echo "🐍 Setting up Python Virtual Environment..."
cd /workspace # Assuming repo is cloned into /workspace or adjust as needed
if [ ! -d "python/.venv" ]; then
    python3.12 -m venv python/.venv
fi

source python/.venv/bin/activate

# 5. Install Python dependencies
echo "📚 Installing Python dependencies..."
if [ -f "python/requirements.txt" ]; then
    pip install --upgrade pip
    pip install -r python/requirements.txt
else
    echo "⚠️ python/requirements.txt not found. Please run this script from the project root."
fi

# 6. Build the Rust Runner in Release Mode
echo "🏗️ Building Rust crates for production..."
if [ -d "rust" ]; then
    cd rust
    cargo build --release
    cd ..
else
    echo "⚠️ rust directory not found."
fi

# 7. Prepare Environment Variables
echo "🔐 Setting up environment templates..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "✅ Created .env from .env.example. PLEASE EDIT THIS FILE with your API keys."
    else
        echo "EVENTCONTRACTS_ENV=production" > .env
        echo "KALSHI_API_KEY_ID=your_key_here" >> .env
        echo "KALSHI_PRIVATE_KEY_PATH=/workspace/kalshi.key" >> .env
        echo "✅ Created boilerplate .env. PLEASE EDIT THIS FILE with your API keys."
    fi
fi

echo "===================================================="
echo "🎉 Setup Complete!"
echo "===================================================="
echo "Next steps:"
echo "1. Edit the .env file with your credentials: nano .env"
echo "2. Activate the python env: source python/.venv/bin/activate"
echo "3. Run the live paper simulation: python python/src/eventcontracts/cli/live_paper.py ..."
echo "   OR"
echo "4. Run the Rust live runner: cd rust && cargo run --release --bin live-runner"
echo "===================================================="
