# Domino CI/CD Demo — GitHub Actions → Domino App

A minimal, fully working example of deploying a Streamlit app to Domino automatically on every push to `main`. The deployment script is **fully idempotent** — re-running on every push is safe and will never create duplicate projects, environments, or apps.

---

## How it works

```
git push → main
    └── GitHub Actions (deploy.yml)
            └── deploy/deploy.py
                    ├── [1] ensure_project   — find or create git-backed Domino project
                    ├── [2] add_service_account — ensure CI/CD SA is a collaborator
                    ├── [3] ensure_environment — find or create shared compute environment
                    └── [4] deploy_app        — publish app (first time) or restart it (subsequent pushes)
```

Each step checks whether the resource already exists before acting, so the pipeline is always safe to re-run.

---

## Repository structure

```
.
├── app.py                          # Streamlit app source
├── app.sh                          # Domino app launcher (called by Domino to start the app)
├── requirements.txt                # App Python dependencies
├── deploy/
│   └── deploy.py                   # Idempotent CI/CD deployment script
└── .github/
    └── workflows/
        └── deploy.yml              # GitHub Actions workflow
```

---

## Prerequisites

### 1. Domino service account

Domino Service Accounts (DSAs) have no UI — they must be created via the API by a Domino administrator. See the [official docs](https://docs.dominodatalab.com/en/6.1/admin_guide/6921e5/manage-domino-service-accounts/) for full details. The steps are:

**Step 1 — Create the service account** (admin only):

```bash
curl -X POST https://<domino-url>/v4/serviceAccounts \
  -H "X-Domino-Api-Key: <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"userName": "functional-sa", "email": "functional-sa@your-org.com"}'
```

The response includes an `idpId` — save it, you need it for the next step.

**Step 2 — Assign the Practitioner role** via **Admin Panel → User Management → functional-sa → Roles**.

**Step 3 — Generate a Bearer token** (admin only):

```bash
curl -X POST https://<domino-url>/v4/serviceAccounts/<idpId>/tokens \
  -H "X-Domino-Api-Key: <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "github-actions-token"}'
```

The `token` field in the response is `DOMINO_SA_TOKEN`.

> SA tokens expire after **4 months** by default. Rotate before expiry — see [Token rotation](#token-rotation) below.

### 2. User API key (first run only)

Creating a compute environment requires `EditEnvironment` permission, which the SA's Practitioner role does not have. You need an user API key **once** — for the very first pipeline run that creates the environment.

After the environment exists, `DOMINO_USER_API_KEY` is no longer needed and can be removed from secrets.

Get an user API key from: **Domino UI → (Admin user) → Account Settings → API Keys**

---

## GitHub setup

### Secrets

Go to **GitHub repo → Settings → Secrets and variables → Actions → Secrets** and add:

| Secret | Value |
|--------|-------|
| `DOMINO_URL` | Your Domino instance URL, e.g. `https://your-org.cs.domino.tech` |
| `DOMINO_SA_TOKEN` | Service account Bearer token (JWT) |
| `DOMINO_USER_API_KEY` | User API key *(only needed on first run — remove after environment is created)* |

### Variables (optional)

If not set, these default to the GitHub repository name:

| Variable | Default | Example |
|----------|---------|---------|
| `DOMINO_PROJECT_NAME` | `github.event.repository.name` | `my-streamlit-app` |
| `DOMINO_ENV_NAME` | `{repo-name}-env` | `my-streamlit-app-env` |
| `DOMINO_APP_NAME` | `github.event.repository.name` | `my-streamlit-app` |
| `DOMINO_PROJECT_OWNER` | SA's own account | `wasantha.gamage@dominodatalab.com` |

> **Important:** Set `DOMINO_PROJECT_OWNER` to the human user's Domino username. Without it, the project is created under the service account's account and won't appear in any human user's workspace.

---

## What the deploy script does

### Step 1 — ensure_project

Searches for a Domino project owned by the SA with the given name. If not found, creates a **git-backed project** pointing at your GitHub repo. No files are uploaded — Domino pulls directly from git on every app start.

### Step 2 — add_service_account

Checks whether `functional-sa` is already a Contributor on the project. Adds it if not. This is what allows the SA token to manage the app on subsequent runs.

### Step 3 — ensure_environment

Looks for a **Global** compute environment with the given name. If found, returns its ID. If not found:
- Requires `DOMINO_USER_API_KEY` (exits with a clear error if missing)
- Creates the environment based on **Domino Standard Environment (Python 3.10 / R 4.5)**
- Adds `RUN pip install streamlit>=1.35.0` to the Dockerfile

On all subsequent runs, the environment already exists so `DOMINO_USER_API_KEY` is never needed again.

### Step 4 — deploy_app

- **First run**: publishes the app with `app.sh` as the entrypoint
- **Every run**: starts a new app instance, which causes Domino to pull the latest commit from the git-backed project

The app is accessible at: `{DOMINO_URL}/apps/{DOMINO_APP_NAME}/`

---

## Running locally

```bash
pip install requests

export DOMINO_URL="https://your-org.cs.domino.tech"
export DOMINO_SA_TOKEN="<bearer-token>"
export DOMINO_USER_API_KEY="<admin-api-key>"   # only needed if environment doesn't exist yet
export DOMINO_PROJECT_NAME="my-project"
export DOMINO_ENV_NAME="my-streamlit-env"
export DOMINO_APP_NAME="my-app"
export GITHUB_REPO_URL="https://github.com/your-org/your-repo.git"

python deploy/deploy.py
```

All arguments can also be passed as flags — run `python deploy/deploy.py --help` for details.

---

## Streamlit port

Domino apps must listen on **port 8888**. The `app.sh` launcher passes `--server.port=8888 --server.address=0.0.0.0 --server.headless=true` to Streamlit. Do not change this port.

---

## Token rotation

SA tokens expire after 4 months by default. To rotate:

**Step 1 — Generate a new token** (admin, via API):

```bash
curl -X POST https://<domino-url>/v4/serviceAccounts/<idpId>/tokens \
  -H "X-Domino-Api-Key: <admin-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "github-actions-token-2"}'
```

**Step 2 — Update the GitHub secret**: **Settings → Secrets → DOMINO_SA_TOKEN → Update** with the new token value.

**Step 3 — Revoke the old token** once the new one is confirmed working:

```bash
curl -X POST https://<domino-url>/v4/serviceAccounts/<idpId>/tokens/<old-token-name>/invalidate \
  -H "X-Domino-Api-Key: <admin-api-key>"
```

The pipeline will pick up the new token on the next push.
