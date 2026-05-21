#!/bin/bash
# app.sh — Domino app launcher for Streamlit
#
# Domino calls this script to start the app. It runs inside the compute
# environment container, so Streamlit is already installed via the environment's
# Dockerfile instructions.
#
# Domino maps port 8888 as the app port. Streamlit must listen on this port
# with server.address=0.0.0.0 and server.headless=true.

set -e

echo "Starting Streamlit app..."
streamlit run app.py \
    --server.port=8888 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
