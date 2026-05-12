#!/usr/bin/env bash
# One-time setup for the isolated Ubuntu VM.
# Installs Python 3.13, Chrome/Chromium, Xvfb, and base Python deps.
set -e

sudo apt-get update
sudo apt-get install -y \
    software-properties-common \
    git curl wget unzip \
    xvfb x11-utils \
    libnss3 libxss1 libasound2t64 libgbm1 libxshmfence1 \
    fonts-liberation

# Python 3.13 (deadsnakes)
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.13 python3.13-venv python3.13-dev

# Chrome (selenium will auto-fetch the matching chromedriver via Selenium Manager)
if ! command -v google-chrome >/dev/null 2>&1; then
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y /tmp/chrome.deb
fi

# Master venv for the runner
python3.13 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
# Pre-install common deps so most projects don't need to fetch them again
.venv/bin/pip install \
    selenium \
    undetected-chromedriver \
    webdriver-manager \
    pyautogui \
    requests \
    beautifulsoup4 \
    pynput \
    pillow

echo "Setup done. Run with: .venv/bin/python run_all.py"
