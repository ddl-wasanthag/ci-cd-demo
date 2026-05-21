"""
app.py — Simple Streamlit demo app.

This is deployed to Domino via GitHub Actions CI/CD (see .github/workflows/deploy.yml).
It intentionally keeps things bare-bones so the CI/CD plumbing is the focus,
not the app itself.
"""

import streamlit as st
import platform
import os
from datetime import datetime, timezone

st.set_page_config(page_title="CI/CD Demo App", page_icon="🚀")

st.title("🚀 Domino CI/CD Demo")
st.caption("Deployed automatically from GitHub via GitHub Actions")

st.divider()

col1, col2 = st.columns(2)

with col1:
    st.subheader("Deployment Info")
    st.info(
        f"**Commit SHA:** `{os.getenv('GITHUB_SHA', 'local')}`\n\n"
        f"**Branch:** `{os.getenv('GITHUB_REF_NAME', 'unknown')}`\n\n"
        f"**Workflow run:** `{os.getenv('GITHUB_RUN_NUMBER', 'N/A')}`"
    )

with col2:
    st.subheader("Runtime Info")
    st.info(
        f"**Python:** `{platform.python_version()}`\n\n"
        f"**Hostname:** `{platform.node()}`\n\n"
        f"**Started:** `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`"
    )

st.divider()
st.subheader("How this app got here")
st.markdown("""
1. A developer pushes code to the `main` branch of the GitHub repo.
2. **GitHub Actions** runs `.github/workflows/deploy.yml`.
3. The workflow calls `deploy/deploy.py` which:
   - Finds or creates the **Domino project** (git-backed — no file uploads needed).
   - Ensures the **shared compute environment** exists with Streamlit pre-installed.
   - Publishes or restarts the **Domino App** so it runs the latest commit.
4. No manual steps. No duplicate projects or environments. Re-runs are always safe.
""")
