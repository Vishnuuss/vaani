"""Tests for the download route's reachability decision.

Measured on the Vaani deployment 2026-08-28: every recording redirected to
`http://localhost:9000/voice-audio/recordings/96.wav`, pointing the caller at
their own machine. No recording could be downloaded, in a browser or otherwise.

Publishing MinIO to fix that is not safe here -- `MinioFileSystem` grants
anonymous GetObject/PutObject/DeleteObject/ListBucket to Principal "*" -- so the
route streams instead when the signed URL is not externally reachable.
"""

from __future__ import annotations

import pytest

from api.routes.public_download import _is_externally_reachable


@pytest.mark.parametrize("url", [
    "http://localhost:9000/voice-audio/recordings/96.wav",   # the real failure
    "http://127.0.0.1:9000/voice-audio/x.wav",
    "http://0.0.0.0:9000/x.wav",
    "http://minio:9000/voice-audio/x.wav",                   # compose service name
    "http://host.docker.internal:9000/x.wav",
])
def test_internal_urls_are_not_externally_reachable(url):
    assert not _is_externally_reachable(url)


@pytest.mark.parametrize("url", [
    # Production is configured correctly and must keep redirecting: streaming
    # every recording through the API there would be pure wasted bandwidth.
    "https://storage.bswealthfinance.com/voice-audio/recordings/2086/user.wav",
    "https://s3.ap-south-1.amazonaws.com/bucket/key.wav",
])
def test_public_urls_stay_on_the_redirect_path(url):
    assert _is_externally_reachable(url)


def test_a_bare_hostname_is_treated_as_internal():
    """A name with no dot only resolves on the container network."""
    assert not _is_externally_reachable("http://storage:9000/x.wav")


@pytest.mark.parametrize("url", ["", "not a url", "://broken"])
def test_garbage_is_never_assumed_reachable(url):
    """Unparseable means we stream, which works, rather than redirect blindly."""
    assert not _is_externally_reachable(url)
