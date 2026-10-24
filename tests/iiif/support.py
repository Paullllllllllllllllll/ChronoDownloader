"""Payloads and manifest builders shared by the IIIF and provider tests."""

from __future__ import annotations

import json
from typing import Any

from tests.net.support import Reply, ScriptedServer

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"y" * 64
PDF = b"%PDF-1.7\n" + b"z" * 64
HTML = b"<!DOCTYPE html><html><body>viewer</body></html>"

FULL = "/full/full/0/default.jpg"
"""The first speculative candidate of an uncapped service."""


def jpeg() -> Reply:
    return Reply(200, JPEG, {"Content-Type": "image/jpeg"})


def pdf(content_type: str = "application/pdf") -> Reply:
    return Reply(200, PDF, {"Content-Type": content_type})


def json_reply(document: Any) -> Reply:
    return Reply(
        200, json.dumps(document).encode("utf-8"), {"Content-Type": "application/json"}
    )


def service_canvas(service_id: str, label: str | None = None) -> dict[str, Any]:
    canvas: dict[str, Any] = {
        "images": [{"resource": {"service": {"@id": service_id}}}]
    }
    if label is not None:
        canvas["label"] = label
    return canvas


def direct_canvas(image_url: str) -> dict[str, Any]:
    return {"images": [{"resource": {"@id": image_url}}]}


def v2_manifest(
    *canvases: dict[str, Any], rendering: Any = None, label: str = "Kochbuch"
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "label": label,
        "sequences": [{"canvases": list(canvases)}],
    }
    if rendering is not None:
        manifest["rendering"] = rendering
    return manifest


def serve_pages(server: ScriptedServer, *names: str) -> list[str]:
    """Serve a full-size JPEG for each service *name*; return the service URLs."""
    bases = []
    for name in names:
        server.script(f"/iiif/{name}{FULL}", jpeg())
        bases.append(server.url(f"/iiif/{name}"))
    return bases


def service_urls(server: ScriptedServer, *names: str) -> list[str]:
    """Return service URLs for *names* without serving anything (all 404)."""
    return [server.url(f"/iiif/{name}") for name in names]
