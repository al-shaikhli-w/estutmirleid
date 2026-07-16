import json
import os

import requests


def handler(request):
    token = os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GITHUB_OWNER")
    repo = os.environ.get("GITHUB_REPO")
    workflow = os.environ.get("GITHUB_WORKFLOW_FILE", "scrape.yml")
    ref = os.environ.get("GITHUB_REF", "main")

    missing = [
        name
        for name, value in {
            "GITHUB_TOKEN": token,
            "GITHUB_OWNER": owner,
            "GITHUB_REPO": repo,
        }.items()
        if not value
    ]

    if missing:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "ok": False,
                    "error": "Missing required environment variables",
                    "missing": missing,
                }
            ),
        }

    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.post(url, headers=headers, json={"ref": ref}, timeout=20)

    if response.status_code != 204:
        return {
            "statusCode": response.status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "ok": False,
                    "error": "Failed to trigger GitHub workflow",
                    "details": response.text[:500],
                }
            ),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "ok": True,
                "message": "Triggered GitHub workflow",
                "workflow": workflow,
                "ref": ref,
            }
        ),
    }
