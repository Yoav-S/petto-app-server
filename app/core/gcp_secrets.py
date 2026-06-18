"""
gcp_secrets.py — Load secrets from env, with Secret Manager fallback on Cloud Run.

Cloud Build deploys sometimes omit secret mounts on new revisions; reading directly
from Secret Manager fixes that when secrets exist in GCP.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)


def _gcp_project_id() -> str:
    return (
        os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.environ.get("GCP_PROJECT", "").strip()
        or os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    )


@lru_cache(maxsize=16)
def fetch_gcp_secret(secret_id: str) -> str:
    project = _gcp_project_id()
    if not project:
        return ""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8").strip()
    except Exception as exc:
        logger.warning("Secret Manager fetch failed for %s: %s", secret_id, exc)
        return ""


@lru_cache(maxsize=1)
def resolve_resend_credentials() -> tuple[str, str, str]:
    """
    Return (api_key, from_email, source).
    source is 'env', 'secret-manager', or '' when not configured.
    """
    from app.core.config import settings

    api_key = settings.RESEND_API_KEY.strip()
    from_email = settings.RESEND_FROM_EMAIL.strip()
    source = "env"

    if not api_key:
        api_key = fetch_gcp_secret("RESEND_API_KEY")
        if api_key:
            source = "secret-manager"

    if not from_email:
        fetched_from = fetch_gcp_secret("RESEND_FROM_EMAIL")
        if fetched_from:
            from_email = fetched_from
            if source == "env":
                source = "secret-manager"

    if api_key and from_email and source == "secret-manager":
        logger.info("Resend credentials loaded from Secret Manager (project=%s)", _gcp_project_id())

    if not (api_key and from_email):
        return "", "", ""

    return api_key, from_email, source
