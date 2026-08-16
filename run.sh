#!/bin/bash
# Starts the PDF split-table fixer locally and opens it in your browser.
set -e
cd "$(dirname "$0")"
source venv/bin/activate
( sleep 1 && open "http://127.0.0.1:5050" ) &
python app.py
