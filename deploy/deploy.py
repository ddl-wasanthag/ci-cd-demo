#!/usr/bin/env python3
"""
deploy.py — Idempotent CI/CD deployment script for Domino.

What it does (in order):
  1. ensure_project()       – find or create the Domino project, backed by this Git repo
  2. add_service_account()  – ensure the CI/CD service account is a project collaborator
  3. ensure_environment()   – find or create the shared (Global) Compute Environment
  4. deploy_app()           – publish the app if it doesn't exist, or start a new instance
                              so it picks up the latest commit

Idempotency guarantee:
  Every step checks first and only acts if action is needed, so re-running on
  every git push is safe — no duplicate projects, environments, or apps.

Two-token design:
  DOMINO_SA_TOKEN   – Service account Bearer token. Used for all steps including
                      project and app management. The SA must be a Practitioner.
  DOMINO_ADMIN_TOKEN – (Optional) Admin API key or Bearer token. Only needed for
                      environment creation, which requires EditEnvironment permission.
                      If omitted and the environment doesn't already exist, the script
                      exits with a helpful error. If the environment already exists,
                      this token is never needed.

  Typical first-time setup:  provide both tokens → creates project + env + app.
  Subsequent pushes:          only DOMINO_SA_TOKEN needed → finds env, restarts app.

Usage:
  python deploy/deploy.py \
      --domino-url    https://your-org.cs.domino.tech \
      --sa-token      <service-account-bearer-token> \
      --admin-token   <admin-api-key>                    # only needed first time \
      --repo-url      https://github.com/your-org/your-repo.git \
      --project-name  my-project \
      --env-name      my-streamlit-env \
      --app-name      my-app

All values can also be supplied via environment variables (see argparse defaults).
GitHub Actions passes them as env vars from secrets.

Exit codes:
  0  success
  1  fatal error — check stderr
"""

import argparse
import os
import sys
import time
import json
import requests
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Thin HTTP client wrapping Domino REST calls
# ---------------------------------------------------------------------------

class DominoClient:
    def __init__(self, base_url: str, token: str, use_api_key: bool = False):
        """
        use_api_key=True  → X-Domino-Api-Key header (legacy API keys)
        use_api_key=False → Authorization: Bearer (service account JWT tokens)
        """
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        if use_api_key:
            self.session.headers.update({"X-Domino-Api-Key": token})
        else:
            self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{self.base}{path}", **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.session.post(f"{self.base}{path}", **kwargs)

    def raise_for(self, resp: requests.Response, action: str):
        """Raise a clear error if the response is not 2xx."""
        if not resp.ok:
            print(f"  ✗  {action} failed — HTTP {resp.status_code}", file=sys.stderr)
            try:
                print(f"     {json.dumps(resp.json(), indent=2)}", file=sys.stderr)
            except Exception:
                print(f"     {resp.text[:400]}", file=sys.stderr)
            sys.exit(1)

    def get_self(self) -> dict:
        resp = self.get("/v4/users/self")
        self.raise_for(resp, "GET /v4/users/self")
        return resp.json()


# ---------------------------------------------------------------------------
# Step 1 — ensure_project
# ---------------------------------------------------------------------------

def resolve_owner(client: DominoClient, owner_username: str) -> tuple[str, str]:
    """
    Resolve a username to (username, user_id).

    If owner_username is provided, look it up via the API.
    Used to support creating the project under a human user's account rather than
    the service account's account.
    """
    resp = client.get("/v4/users", params={"userName": owner_username})
    client.raise_for(resp, f"GET /v4/users?userName={owner_username}")
    users = resp.json()
    if not users:
        print(f"  ✗  Project owner '{owner_username}' not found in Domino.", file=sys.stderr)
        sys.exit(1)
    return users[0]["userName"], users[0]["id"]


def ensure_project(client: DominoClient, project_name: str, repo_url: str,
                   owner_username: str, owner_id: str,
                   admin_client: "DominoClient | None" = None) -> str:
    """
    Return the project ID, creating the git-backed project if needed.

    admin_client: used as a fallback for lookup (SA can't see other users' private
                  projects before it's a collaborator) and for creation
                  (CreateProjectForOtherUsers permission required).
                  On subsequent runs the SA is already a collaborator, so the lookup
                  succeeds with the SA token and admin_client is never needed.
    """
    print(f"\n[1] ensure_project: '{project_name}' (owner: {owner_username})")

    # Try the SA token first (works on all subsequent runs); fall back to admin on
    # first run before the SA has been added as a collaborator.
    resp = client.get("/v4/projects", params={"name": project_name,
                                               "ownerUsername": owner_username})
    client.raise_for(resp, "GET /v4/projects")
    matches = [p for p in resp.json()
               if p["name"] == project_name and p["ownerUsername"] == owner_username]

    if not matches and admin_client:
        resp = admin_client.get("/v4/projects", params={"name": project_name,
                                                         "ownerUsername": owner_username})
        admin_client.raise_for(resp, "GET /v4/projects (admin)")
        matches = [p for p in resp.json()
                   if p["name"] == project_name and p["ownerUsername"] == owner_username]

    if matches:
        project_id = matches[0]["id"]
        print(f"  ✓  Found existing project — id={project_id}")
        return project_id

    poster = admin_client or client
    if owner_id != client.session.headers.get("sub") and not admin_client:
        # Creating for another user requires admin — give a helpful error
        pass  # let the POST fail naturally with a clear 403 message
    print(f"  →  Project not found, creating with git backing: {repo_url}")
    body = {
        "name": project_name,
        "description": "Deployed via GitHub Actions CI/CD",
        "visibility": "Private",
        "ownerId": owner_id,
        "collaborators": [],
        "tags": {"tagNames": []},
        "mainRepository": {
            "uri": repo_url,
            "serviceProvider": "github",
            "defaultRef": {"type": "head"},
            "credentials": {"credentialType": "none"},
        },
    }
    resp = poster.post("/v4/projects", json=body)
    poster.raise_for(resp, "POST /v4/projects")
    project_id = resp.json()["id"]
    print(f"  ✓  Created project — id={project_id}")
    return project_id


# ---------------------------------------------------------------------------
# Step 2 — add_service_account collaborator
# ---------------------------------------------------------------------------

def add_service_account(client: DominoClient, project_id: str, sa_username: str,
                        manage_client: "DominoClient | None" = None):
    """
    Ensure the service account is a Contributor on the project.

    manage_client: if provided, used for GET/POST on the collaborators endpoint.
                   Needed when the project is owned by a different user and the SA
                   doesn't yet have access to read/write collaborators.
    """
    print(f"\n[2] add_service_account: '{sa_username}' → project {project_id}")

    manager = manage_client or client

    resp = manager.get(f"/v4/projects/{project_id}/collaborators")
    manager.raise_for(resp, f"GET /v4/projects/{project_id}/collaborators")
    if any(c["userName"] == sa_username for c in resp.json()):
        print(f"  ✓  '{sa_username}' is already a collaborator — skipping")
        return

    # Look up SA user ID
    resp = manager.get("/v4/users", params={"userName": sa_username})
    manager.raise_for(resp, f"GET /v4/users?userName={sa_username}")
    users = resp.json()
    if not users:
        print(f"  ✗  Service account '{sa_username}' not found. "
              "An admin must create it first.", file=sys.stderr)
        sys.exit(1)
    sa_id = users[0]["id"]

    resp = manager.post(f"/v4/projects/{project_id}/collaborators",
                        json={"collaboratorId": sa_id, "projectRole": "Contributor"})
    manager.raise_for(resp, f"POST /v4/projects/{project_id}/collaborators")
    print(f"  ✓  Added '{sa_username}' (id={sa_id}) as Contributor")


# ---------------------------------------------------------------------------
# Step 3 — ensure_environment
# ---------------------------------------------------------------------------

def ensure_environment(client: DominoClient, env_name: str,
                       admin_client: "DominoClient | None") -> str:
    """
    Return the ID of the Global environment named `env_name`.

    If not found and admin_client is provided, create it.
    If not found and no admin_client, exit with a helpful error.

    Creating environments requires EditEnvironment permission (typically admin).
    On subsequent runs the environment already exists so only the SA token is needed.
    """
    print(f"\n[3] ensure_environment: '{env_name}'")

    resp = client.get("/v1/environments")
    client.raise_for(resp, "GET /v1/environments")
    data = resp.json()
    envs = data.get("data", data) if isinstance(data, dict) else data
    matches = [e for e in envs if e["name"] == env_name]

    if matches:
        env_id = matches[0]["id"]
        print(f"  ✓  Found existing environment '{env_name}' — id={env_id}")
        return env_id

    if not admin_client:
        print(
            f"  ✗  Environment '{env_name}' not found and DOMINO_ADMIN_TOKEN is not set.\n"
            "     Options:\n"
            "     a) Create the environment manually in Domino with this exact name.\n"
            "     b) Provide DOMINO_ADMIN_TOKEN on the first run so this script can\n"
            "        create it automatically (requires EditEnvironment permission).",
            file=sys.stderr
        )
        sys.exit(1)

    # Resolve base environment revision ID (Domino Standard Env Py3.10 R4.5)
    BASE_ENV_ID = "686fd386d3759f30baf334bb"
    resp_base = admin_client.get(f"/v4/environments/{BASE_ENV_ID}")
    admin_client.raise_for(resp_base, f"GET /v4/environments/{BASE_ENV_ID}")
    base_rev_id = resp_base.json()["latestRevision"]["id"]

    print(f"  →  Environment not found — creating '{env_name}' (requires admin token)")
    body = {
        "name": env_name,
        "description": "Shared Streamlit environment — auto-created by deploy.py",
        "visibility": "Global",
        "supportedClusters": [],
        "dockerfileInstructions": "RUN pip install streamlit>=1.35.0",
        "base": {
            "type": "Environment",
            "environmentId": BASE_ENV_ID,
            "environmentRevisionId": base_rev_id,
        },
    }
    resp = admin_client.post("/v1/environments", json=body)
    admin_client.raise_for(resp, "POST /v1/environments")
    result = resp.json()
    # The v1 create API returns _id (not id) for the environment
    env_id = result.get("_id") or result.get("id")
    print(f"  ✓  Created environment — id={env_id}")
    time.sleep(2)  # Let the record commit before referencing it
    return env_id


# ---------------------------------------------------------------------------
# Step 4 — deploy_app
# ---------------------------------------------------------------------------

def deploy_app(client: DominoClient, project_id: str, app_name: str,
               env_id: str, hw_tier: str) -> str:
    """Publish the app if new, or start a fresh instance to pick up latest code."""
    print(f"\n[4] deploy_app: '{app_name}'")

    resp = client.get("/v4/modelProducts", params={"projectId": project_id})
    client.raise_for(resp, "GET /v4/modelProducts")
    matches = [a for a in resp.json() if a["name"] == app_name]

    if not matches:
        print(f"  →  App not found, publishing '{app_name}'")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "id": str(uuid.uuid4()).replace("-", "")[:24],  # Domino uses 24-char hex IDs
            "modelProductType": "APP",
            "projectId": project_id,
            "name": app_name,
            "description": "Deployed via GitHub Actions CI/CD",
            "entryPoint": "app.sh",
            "environmentId": env_id,
            "hardwareTierId": hw_tier,
            "renderIFrame": True,
            "mountDatasets": False,
            "created": now_iso,
            "lastUpdated": now_iso,
            "status": "Stopped",
            "media": [],
            "tags": [],
            "stats": {"usageCount": 0},
            "appExtension": {"appType": "Shiny"},
            "permissionsData": {
                "visibility": "GRANT_BASED",
                "accessRequestStatuses": {},
                "pendingInvitations": [],
                "discoverable": True,
                "appAccessStatus": "ALLOWED",
            },
        }
        resp = client.post("/v4/modelProducts", json=body)
        client.raise_for(resp, "POST /v4/modelProducts")
        app_data = resp.json()
        app_id = app_data.get("id") or app_data.get("modelProductId")
        print(f"  ✓  Published app — id={app_id}")
    else:
        app_id = matches[0]["id"]
        print(f"  ✓  Found existing app — id={app_id}, status={matches[0].get('status')}")

    # Start a new instance — this picks up the latest commit from the git-backed project
    print(f"  →  Starting new app instance (picks up latest git commit)…")
    resp = client.post(f"/v4/modelProducts/{app_id}/start", json={})
    client.raise_for(resp, f"POST /v4/modelProducts/{app_id}/start")
    instance_id = resp.json() if isinstance(resp.json(), str) else str(resp.json())
    print(f"  ✓  App instance started — instanceId={instance_id}")
    print(f"  ℹ  App URL: {client.base}/apps/{app_name}/")
    return app_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Idempotent Domino CI/CD deployer")
    parser.add_argument("--domino-url",    default=os.getenv("DOMINO_URL"))
    parser.add_argument("--sa-token",      default=os.getenv("DOMINO_SA_TOKEN"),
                        help="Service account Bearer token (env: DOMINO_SA_TOKEN)")
    parser.add_argument("--admin-token",   default=os.getenv("DOMINO_ADMIN_TOKEN"),
                        help="Admin API key for env creation, optional after first run "
                             "(env: DOMINO_ADMIN_TOKEN)")
    parser.add_argument("--repo-url",      default=os.getenv("GITHUB_REPO_URL"))
    parser.add_argument("--project-name",  default=os.getenv("DOMINO_PROJECT_NAME"))
    parser.add_argument("--env-name",      default=os.getenv("DOMINO_ENV_NAME"))
    parser.add_argument("--app-name",      default=os.getenv("DOMINO_APP_NAME"))
    parser.add_argument("--hw-tier",       default=os.getenv("DOMINO_HW_TIER", "small-k8s"))
    parser.add_argument("--sa-username",     default=os.getenv("DOMINO_SA_USERNAME",
                                                                "functional-sa"))
    parser.add_argument("--project-owner",  default=os.getenv("DOMINO_PROJECT_OWNER"),
                        help="Domino username who should own the project. "
                             "Defaults to the service account itself. Set this to a human "
                             "user's username so the project appears in their workspace. "
                             "(env: DOMINO_PROJECT_OWNER)")
    return parser.parse_args()


def main():
    args = parse_args()

    missing = [k for k, v in {
        "DOMINO_URL":          args.domino_url,
        "DOMINO_SA_TOKEN":     args.sa_token,
        "GITHUB_REPO_URL":     args.repo_url,
        "DOMINO_PROJECT_NAME": args.project_name,
        "DOMINO_ENV_NAME":     args.env_name,
        "DOMINO_APP_NAME":     args.app_name,
    }.items() if not v]
    if missing:
        print("Missing required arguments:\n  •", "\n  • ".join(missing), file=sys.stderr)
        sys.exit(1)

    # SA client (Bearer JWT token)
    sa = DominoClient(args.domino_url, args.sa_token, use_api_key=False)

    # Admin client (API key) — optional, only needed for first-time env creation
    admin = (DominoClient(args.domino_url, args.admin_token, use_api_key=True)
             if args.admin_token else None)

    me = sa.get_self()
    sa_username = me["userName"]

    # Resolve project owner — defaults to the SA itself, but can be set to a human user
    # so the project appears in their workspace rather than the SA's account.
    if args.project_owner:
        owner_username, owner_id = resolve_owner(sa, args.project_owner)
    else:
        owner_username, owner_id = sa_username, me["id"]

    print(f"\n=== Domino CI/CD Deploy ===")
    print(f"  Authenticated as : {sa_username}")
    print(f"  Project owner    : {owner_username}")
    print(f"  Domino URL       : {args.domino_url}")
    print(f"  GitHub repo      : {args.repo_url}")
    print(f"  Project          : {args.project_name}")
    print(f"  Environment      : {args.env_name}")
    print(f"  App              : {args.app_name}")
    print(f"  Admin token      : {'provided' if admin else 'not provided (env must already exist)'}")

    # When creating a project for another user, the SA can't see their private projects
    # and lacks CreateProjectForOtherUsers permission — use admin for all project ops.
    project_id = ensure_project(sa, args.project_name, args.repo_url,
                                 owner_username, owner_id,
                                 admin_client=(admin if args.project_owner else None))
    # When the project is owned by another user, the SA has no access yet —
    # use admin to manage collaborators until the SA has been added.
    manage_client = admin if args.project_owner else None
    add_service_account(sa, project_id, args.sa_username, manage_client=manage_client)
    env_id = ensure_environment(sa, args.env_name, admin)
    deploy_app(sa, project_id, args.app_name, env_id, args.hw_tier)

    print(f"\n=== Deployment complete ===\n")


if __name__ == "__main__":
    main()
