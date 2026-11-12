#!/usr/bin/env bash
cd "$(dirname "$0")"
source venv/bin/activate
python main.py discover
python bot.py
