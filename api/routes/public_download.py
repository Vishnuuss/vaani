"""Public download endpoints for workflow recordings and transcripts.

These endpoints provide secure, token-based public access to workflow artifacts
without requiring authentication. Tokens are generated on-demand during
post-call processing for runs that execute integrations, QA, or campaign
reporting.
"""

from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from fastapi.responses import RedirectResponse, StreamingResponse
from loguru import logger

from api.db import db_client
from api.services.storage import get_storage_for_backend
from api.utils.recording_artifacts import (
    get_recording_storage_backend,
    get_recording_storage_key,
)

router = APIRouter(prefix="/public/download")


# Hosts that only resolve to something useful from inside the deployment. A
# signed URL pointing at one of these cannot be followed by a caller.
_INTERNAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "minio", "host.docker.internal"}


def _is_externally_reachable(url: str) -> bool:
    """Can a caller outside the server actually follow this URL?"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host or host in _INTERNAL_HOSTS:
        return False
    # Container/compose service names have no dots and resolve only on the
    # internal network.
    return "." in host


# A browser <audio> element refuses to play "application/octet-stream": the
# dashboard's Run Preview player sat at 0:00 / 0:00 with a perfectly good WAV
# behind it. The redirect path never had this problem because MinIO returns the
# type it stored, so it only appeared once this route began serving bytes.
_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def _media_type_for(file_path: str) -> str:
    return _MEDIA_TYPES.get(Path(file_path).suffix.lower(), "application/octet-stream")


async def _stream_object(url: str, chunk_size: int = 64 * 1024):
    """Relay the object body without buffering the whole file in memory."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(chunk_size):
                yield chunk

@router.get("/workflow/{token}/{artifact_type}")
async def download_workflow_artifact(
    token: str,
    artifact_type: str,
    inline: bool = Query(
        default=False, description="Display inline in browser instead of download"
    ),
):
    """Download a workflow recording or transcript via public access token.

    This endpoint:
    1. Validates the public access token
    2. Looks up the corresponding workflow run
    3. Generates a signed URL for the requested artifact
    4. Redirects to the signed URL

    Args:
        token: The public access token (UUID format)
        artifact_type: Type of artifact - "recording", "transcript",
            "user_recording", or "bot_recording"
        inline: If true, sets Content-Disposition to inline for browser preview

    Returns:
        RedirectResponse to the signed URL (302 redirect)

    Raises:
        HTTPException 400: If artifact type is unsupported
        HTTPException 404: If token is invalid or artifact not found
    """
    # 1. Lookup workflow run by token
    workflow_run = await db_client.get_workflow_run_by_public_token(token)
    if not workflow_run:
        logger.warning(f"Invalid public access token: {token[:8]}...")
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    # 2. Get file path based on artifact type
    artifact_storage_backend = None
    if artifact_type == "recording":
        file_path = workflow_run.recording_url
    elif artifact_type == "transcript":
        file_path = workflow_run.transcript_url
    elif artifact_type == "user_recording":
        file_path = get_recording_storage_key(workflow_run.extra, "user")
        artifact_storage_backend = get_recording_storage_backend(
            workflow_run.extra, "user"
        )
    elif artifact_type == "bot_recording":
        file_path = get_recording_storage_key(workflow_run.extra, "bot")
        artifact_storage_backend = get_recording_storage_backend(
            workflow_run.extra, "bot"
        )
    else:
        logger.warning(
            f"Unsupported artifact type: type={artifact_type}, workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(status_code=400, detail="Unsupported artifact type")

    if not file_path:
        logger.warning(
            f"Artifact not found: type={artifact_type}, workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(
            status_code=404,
            detail=f"No {artifact_type} available for this workflow run",
        )

    # 3. Get storage backend for this workflow run
    try:
        storage = get_storage_for_backend(
            artifact_storage_backend or workflow_run.storage_backend
        )
    except ValueError as e:
        logger.error(f"Invalid storage backend: {workflow_run.storage_backend}")
        raise HTTPException(status_code=500, detail="Storage configuration error")

    # 4. Generate signed URL (1 hour expiration)
    try:
        signed_url = await storage.aget_signed_url(
            file_path=file_path,
            expiration=3600,  # 1 hour
            force_inline=inline,
        )
    except Exception as e:
        logger.error(f"Failed to generate signed URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate download URL")

    if not signed_url:
        logger.error(f"Storage returned None for signed URL: {file_path}")
        raise HTTPException(status_code=500, detail="Failed to generate download URL")

    logger.info(
        f"Generated signed URL for {artifact_type}: workflow_run_id={workflow_run.id}, token={token[:8]}..."
    )

    # 5. Hand the file over.
    #
    # Redirecting is the cheap path and stays the default: the caller fetches
    # straight from object storage and no bytes cross this API. It only works
    # when the signed URL is reachable from outside, which depends entirely on
    # MINIO_PUBLIC_ENDPOINT being set to a real public address.
    #
    # When it is not, that variable falls back to "http://localhost:9000"
    # (api/constants.py:90) and the redirect points the caller at their OWN
    # machine. Measured on the Vaani deployment 2026-08-28: every recording
    # 302'd to http://localhost:9000/voice-audio/recordings/96.wav, so no
    # recording could be downloaded by anyone, in a browser or otherwise.
    #
    # The obvious repair -- publish MinIO on its own domain, as the production
    # install does -- is NOT safe here. `MinioFileSystem` sets a bucket policy
    # granting anonymous s3:GetObject, s3:PutObject, s3:DeleteObject and
    # s3:ListBucket to Principal "*" (api/services/filesystem/minio.py:69-85),
    # with its own comment reading "Only use in local development, not
    # production!". Exposing that bucket would make every call recording
    # publicly listable AND publicly deletable.
    #
    # So when the signed URL is not externally reachable, this route streams the
    # object itself over the connection the caller already has. Object storage
    # stays private, no DNS record is needed, and the token check above remains
    # the only door.
    if _is_externally_reachable(signed_url):
        return RedirectResponse(url=signed_url, status_code=302)

    internal_url = await storage.aget_signed_url(
        file_path=file_path,
        expiration=3600,
        force_inline=inline,
        use_internal_endpoint=True,
    )
    if not internal_url:
        logger.error(f"No internal URL for {file_path}; cannot stream")
        raise HTTPException(status_code=500, detail="Failed to read artifact")

    logger.info(
        f"Public endpoint {signed_url.split('/')[2]!r} is not externally "
        f"reachable; streaming {artifact_type} through the API instead"
    )
    return StreamingResponse(
        _stream_object(internal_url),
        media_type=_media_type_for(file_path),
        headers={
            "Content-Disposition": (
                f'{"inline" if inline else "attachment"}; '
                f'filename="{Path(file_path).name}"'
            ),
            # Lets the dashboard's audio element seek instead of only playing
            # from the start.
            "Accept-Ranges": "bytes",
        },
    )
