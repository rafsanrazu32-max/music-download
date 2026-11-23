#!/usr/bin/env bash
set -e

# Render Web Service এর জন্য HTTP পোর্ট ও Keepalive চালু রাখা
python3 keepalive.py &

# মূল টেলিগ্রাম বট চালু করা
exec python3 bot.py
