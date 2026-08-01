"""Core file download primitives.

Provides the central `download_file` function used by every provider connector
and the IIIF strategies. Handles rate limiting, exponential backoff, budget
enforcement, content-type validation, magic-byte checks, HTML login-page
detection, and standardized naming.

Also provides `save_json` for metadata persistence under each work's
`metadata/` subdirectory.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from .atomic import atomic_write_json
from .budget import get_budget
from .config import (
    get_download_config,
    get_network_config,
    include_metadata,
    overwrite_existing,
)
from .context import (
    get_current_name_stem,
    get_current_provider,
    get_current_work,
    increment_counter,
    peek_counter,
    release_counter,
)
from .naming import get_provider_slug, sanitize_filename, to_snake_case
from .network import (
    get_circuit_breaker,
    get_provider_for_url,
    get_rate_limiter,
    get_session,
)

logger = logging.getLogger(__name__)

_BUDGET = get_budget()

# Number of leading bytes buffered from the stream for post-write validation
# (magic-byte + HTML login-page checks), so the file need not be reopened.
# Covers the larger of the two validators (HTML login check reads 2048).
_VALIDATION_HEAD_BYTES = 2048

# Directories created this run. os.makedirs(exist_ok=True) is a syscall even
# when the directory already exists; every page of a work re-creates the same
# objects/ and metadata/ directories. Caching known-created paths skips the
# redundant calls. A directory deleted externally after caching is still caught
# by the existing OSError handling on the subsequent write.
_CREATED_DIRS: set[str] = set()
_CREATED_DIRS_LOCK = threading.Lock()


def _ensure_dir(path: str) -> None:
    """Create ``path`` once, caching to skip redundant makedirs syscalls."""
    with _CREATED_DIRS_LOCK:
        if path in _CREATED_DIRS:
            return
        os.makedirs(path, exist_ok=True)
        _CREATED_DIRS.add(path)


_CONTENT_TYPE_EXT_MAP = {
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/jp2": ".jp2",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/json": ".json",
}

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".jp2",
    ".tif",
    ".tiff",
    ".gif",
    ".bmp",
    ".webp",
}

_ANNAS_LOGIN_MARKERS = (
    "<title>log in / register",
    "<title>login",
    "member login",
    "please log in",
    "__darkreader__",
)


def _safe_remove(path: str) -> None:
    """Remove a file, ignoring any error (best-effort cleanup)."""
    with contextlib.suppress(OSError):
        os.remove(path)


def _parse_content_length(cl_header: str | None) -> int | None:
    """Parse a Content-Length header, returning None on absence or malformation.

    A malformed header (e.g. non-numeric) must not raise; the download simply
    proceeds without a declared length.
    """
    if not cl_header:
        return None
    try:
        length = int(cl_header)
    except ValueError:
        return None
    # A negative declared length is malformed; treating it as unknown avoids
    # discarding a fully downloaded file over an impossible size mismatch.
    return length if length >= 0 else None


def _infer_extension_from_content_type(content_type: str) -> str:
    ct_lower = content_type.lower()
    for mime, ext in _CONTENT_TYPE_EXT_MAP.items():
        if mime in ct_lower:
            return ext
    return ""


def _should_reject_html_response(
    content_type: str,
    url: str,
    content_length: int | None = None,
) -> tuple[bool, str]:
    if "text/html" not in content_type.lower():
        return False, ""

    parsed = urlparse(url)
    url_lower = url.lower()
    path_lower = parsed.path.lower()

    suggests_pdf = (
        ".pdf" in path_lower or "output=pdf" in url_lower or "download" in url_lower
    )
    suggests_epub = ".epub" in path_lower or "output=epub" in url_lower

    if suggests_pdf or suggests_epub:
        return (
            True,
            "URL suggests PDF/EPUB but server returned HTML (likely error page)",
        )

    if (
        "annas-archive" in url_lower
        and content_length
        and 170000 < content_length < 185000
    ):
        return True, "Anna's Archive HTML page likely login/error page (~180KB)"

    return False, ""


def _validate_file_magic_bytes(
    filepath: str,
    ext: str,
    head: bytes | None = None,
    complete: bool = False,
) -> tuple[bool, str]:
    if ext not in (".pdf", ".epub"):
        return True, ""

    try:
        # Prefer the bytes already buffered during streaming; fall back to
        # reopening the file only if the buffered prefix is too short.
        if head is not None and (len(head) >= 512 or complete):
            first_bytes = head[:512]
        else:
            with open(filepath, "rb") as f:
                first_bytes = f.read(512)

        is_html = b"<!DOCTYPE" in first_bytes or b"<html" in first_bytes.lower()

        if ext == ".pdf":
            if not first_bytes.startswith(b"%PDF-"):
                if is_html:
                    return False, "File claims to be PDF but contains HTML"
                return False, "File claims to be PDF but has invalid magic bytes"
        elif ext == ".epub" and not first_bytes.startswith(b"PK\x03\x04"):
            if is_html:
                return False, "File claims to be EPUB but contains HTML"
            return False, "File claims to be EPUB but has invalid magic bytes"

        return True, ""
    except Exception as e:
        logger.warning("Error validating file %s: %s", filepath, e)
        return True, ""


def _validate_html_not_login_page(
    filepath: str,
    url: str,
    provider: str | None,
    head: bytes | None = None,
    complete: bool = False,
) -> tuple[bool, str]:
    url_lower = url.lower()
    provider_lower = (provider or "").lower()

    if "annas-archive" not in url_lower and "annas-archive" not in provider_lower:
        return True, ""

    try:
        # Decode the bytes buffered during streaming; only reopen the file when
        # that prefix does not cover the 2048 characters this check inspects
        # (e.g. a large multibyte file whose first 2048 bytes decode short).
        html_content: str | None = None
        if head is not None:
            decoded = head.decode("utf-8", errors="ignore").lower()
            if len(decoded) >= 2048 or complete:
                html_content = decoded[:2048]
        if html_content is None:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                html_content = f.read(2048).lower()

        if any(marker in html_content for marker in _ANNAS_LOGIN_MARKERS):
            return False, "Anna's Archive login/error page detected"

        return True, ""
    except Exception as e:
        logger.warning("Error validating HTML file %s: %s", filepath, e)
        return True, ""


def _determine_target_directory(
    folder_path: str,
    ext: str,
    allowed_exts: list[str] | None,
    save_disallowed_to_metadata: bool,
) -> tuple[str | None, str, bool]:
    if allowed_exts and ext not in allowed_exts:
        if save_disallowed_to_metadata:
            return (
                os.path.join(folder_path, "metadata"),
                f"Extension {ext} not in allowed list; saving to metadata folder",
                False,
            )
        return None, f"Extension {ext} not in allowed list; skipping download", False

    return os.path.join(folder_path, "objects"), "", True


def _counter_key(
    ext: str,
    stem: str,
    prov_slug: str,
    max_stem_len: int = 50,
) -> tuple[str, str, str]:
    """Build the per-work naming counter key for one (extension, stem) pair."""
    if len(stem) > max_stem_len:
        stem = stem[:max_stem_len].rstrip("_")

    type_key = "image" if ext in _IMAGE_EXTENSIONS else (ext.lstrip(".") or "bin")
    return (stem, prov_slug, type_key)


def _build_standardized_filename(
    ext: str,
    stem: str,
    prov_slug: str,
    max_stem_len: int = 50,
) -> str:
    key = _counter_key(ext, stem, prov_slug, max_stem_len)
    stem, prov_slug, type_key = key
    seq = increment_counter(key)

    if type_key == "image":
        safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
    else:
        safe_base = f"{stem}_{prov_slug}" if seq <= 1 else f"{stem}_{prov_slug}_{seq}"

    return sanitize_filename(f"{safe_base}{ext}")


def _filename_from_content_disposition(cd: str | None) -> str | None:
    if not cd:
        return None
    try:
        parts = [p.strip() for p in cd.split(";")]
        params: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"')
                params[k] = v
        fn = params.get("filename*") or params.get("filename")
        if fn:
            if "''" in fn:
                try:
                    charset, _, enc = fn.partition("''")
                    return unquote(enc)
                except Exception:
                    return fn
            return fn
    except Exception:
        return None
    return None


def _try_skip_existing(
    url: str, folder_path: str, filename: str, provider: str | None
) -> tuple[str | None, bool]:
    """Return ``(existing_path, counts_as_success)`` for an already-present file.

    The second element mirrors ``_determine_target_directory``: a file routed
    to ``metadata/`` is not a successful object download, and the skip path
    must report that exactly as the fresh-download path does.
    """
    if overwrite_existing():
        return None, True

    predicted_ext = (
        Path(urlparse(url).path).suffix.lower()
        or Path(filename or "").suffix.lower()
        or None
    )
    if not predicted_ext:
        return None, True

    dl_cfg = get_download_config()
    allowed_exts = dl_cfg.get("allowed_object_extensions", [])
    save_disallowed = dl_cfg.get("save_disallowed_to_metadata", True)
    target_dir, _, counts_as_success = _determine_target_directory(
        folder_path, predicted_ext, allowed_exts, save_disallowed
    )
    if target_dir is None:
        return None, True

    stem = get_current_name_stem() or to_snake_case(filename) or "object"
    prov_slug = get_provider_slug(get_current_provider(), provider)

    key = _counter_key(predicted_ext, stem, prov_slug)
    stem, prov_slug, type_key = key
    seq = peek_counter(key)

    if type_key == "image":
        safe_base = f"{stem}_{prov_slug}_image_{seq:03d}"
    else:
        safe_base = f"{stem}_{prov_slug}" if seq <= 1 else f"{stem}_{prov_slug}_{seq}"

    predicted_name = sanitize_filename(f"{safe_base}{predicted_ext}")
    predicted_path = os.path.join(target_dir, predicted_name)

    if os.path.exists(predicted_path):
        increment_counter(key)
        logger.info("File already exists (early check), skipping: %s", predicted_path)
        return predicted_path, counts_as_success

    return None, True


def download_file(url: str, folder_path: str, filename: str) -> str | None:
    """Download a file with centralized rate limiting, retries, and budget enforcement.

    Args:
        url: URL to download
        folder_path: Target directory (will create objects/ subdirectory)
        filename: Base filename (will be standardized with provider prefix and counter)

    Returns:
        Path to downloaded file or None on failure
    """
    _ensure_dir(folder_path)

    session = get_session()
    provider = get_provider_for_url(url)

    existing, existing_counts = _try_skip_existing(url, folder_path, filename, provider)
    if existing is not None:
        return existing if existing_counts else None

    work_id = get_current_work()

    # Budget guards run before the circuit-breaker check: a skipped download
    # makes no request, so it must not consume the breaker's single half-open
    # probe slot.
    if _BUDGET.exhausted():
        logger.warning("Download budget exhausted; skipping %s", url)
        return None

    # Consult the per-provider circuit breaker before downloading, mirroring
    # make_request: a provider tripped by 429/5xx storms is skipped until its
    # cooldown elapses.
    cb = get_circuit_breaker(provider)
    if cb and not cb.allow_request():
        logger.warning(
            "Circuit breaker OPEN for %s; skipping download (retry in %.0fs): %s",
            provider or "unknown",
            cb.time_until_retry(),
            url,
        )
        return None

    net = get_network_config(provider)

    max_attempts = int(net.get("max_attempts", 5) or 5)
    base_backoff = float(net.get("base_backoff_s", 1.5) or 1.5)
    backoff_mult = float(net.get("backoff_multiplier", 1.5) or 1.5)
    max_backoff = float(net.get("max_backoff_s", 60.0) or 60.0)
    timeout_s = net.get("timeout_s")
    timeout = float(timeout_s) if timeout_s is not None else 30.0
    verify_default = bool(net.get("verify_ssl", True))
    ssl_policy = str(net.get("ssl_error_policy", "fail") or "fail").lower()
    provider_headers = dict(net.get("headers", {}) or {})

    req_headers = (
        {str(k): str(v) for k, v in provider_headers.items() if v is not None}
        if provider_headers
        else {}
    )

    rl = get_rate_limiter(provider)

    def _process_response(
        response: requests.Response, is_insecure_retry: bool = False
    ) -> str | None:
        content_type = response.headers.get("Content-Type", "")

        content_len = _parse_content_length(response.headers.get("Content-Length"))
        should_reject, reject_reason = _should_reject_html_response(
            content_type, url, content_len
        )
        if should_reject:
            log_suffix = " (insecure retry)" if is_insecure_retry else ""
            logger.warning(
                "Rejecting download%s: %s: %s", log_suffix, reject_reason, url
            )
            return None

        cd_name = _filename_from_content_disposition(
            response.headers.get("Content-Disposition")
        )
        parsed = urlparse(url)
        # Prefer the server's declared Content-Type over the URL suffix: a URL
        # ending in ``.pdf`` may actually serve an HTML error page, whereas the
        # Content-Type reflects the real payload.
        inferred_ext = (
            _infer_extension_from_content_type(content_type)
            or Path(parsed.path).suffix
            or Path(cd_name or "").suffix
            or Path(filename or "").suffix
            or ".bin"
        ).lower()

        stem = get_current_name_stem() or to_snake_case(filename) or "object"
        prov_slug = get_provider_slug(get_current_provider(), provider)

        dl_cfg = get_download_config()
        allowed_exts = dl_cfg.get("allowed_object_extensions", [])
        save_disallowed = dl_cfg.get("save_disallowed_to_metadata", True)

        target_dir, log_msg, counts_as_success = _determine_target_directory(
            folder_path, inferred_ext, allowed_exts, save_disallowed
        )
        if target_dir is None:
            logger.info(log_msg)
            return None
        if log_msg:
            logger.info(log_msg)

        _ensure_dir(target_dir)

        name_key = _counter_key(inferred_ext, stem, prov_slug)
        safe_name = _build_standardized_filename(inferred_ext, stem, prov_slug)
        filepath = os.path.join(target_dir, safe_name)

        if not overwrite_existing() and os.path.exists(filepath):
            logger.info("File already exists, skipping: %s", filepath)
            return filepath if counts_as_success else None

        if not _BUDGET.allow_new_file(provider, work_id):
            logger.warning("Download budget stop-policy tripped; skipping %s", url)
            return None

        # Classify the payload into its budget bucket by extension so PDF and
        # metadata limits are actually enforced (previously everything was
        # booked as "images").
        if inferred_ext in (".pdf", ".epub"):
            budget_type = "pdfs"
        elif inferred_ext in (".json", ".xml", ".html", ".txt"):
            budget_type = "metadata"
        else:
            budget_type = "images"

        content_len_int = content_len or 0
        if content_len_int and not _BUDGET.allow_bytes(
            provider, work_id, content_len_int, content_type=budget_type
        ):
            logger.warning(
                "Download budget (bytes) would be exceeded by %s (%d bytes); skipping.",
                url,
                content_len_int,
            )
            return None

        # Stream into a temporary <name>.part file and only promote it to the
        # final path via os.replace() once the write is complete and validated.
        # A connection drop, disk-full error, or budget cutoff therefore never
        # leaves a partial file at the final path that a later resume run would
        # treat as a complete download.
        # Resolve the budget limits once for this streaming session; the
        # per-chunk atomic check-and-record still reads live counters against
        # this snapshot, so mid-file cutoff behavior is unchanged while the
        # config dict rebuild and GB/MB conversions no longer run per chunk.
        budget_limits = _BUDGET.resolve_limits(budget_type, work_id)

        part_path = filepath + ".part"
        truncated = False
        bytes_written = 0

        def _discard_partial() -> None:
            """Remove the .part file and refund any bytes already booked."""
            _safe_remove(part_path)
            _BUDGET.refund(budget_type, work_id, bytes_written)

        # Buffer the leading bytes so the post-write validators need not reopen
        # the just-written file (which triggers a Defender re-scan on Windows).
        head = bytearray()
        try:
            with open(part_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if not _BUDGET.add_bytes(
                        provider,
                        work_id,
                        len(chunk),
                        content_type=budget_type,
                        limits=budget_limits,
                    ):
                        logger.error(
                            "Download budget exceeded while writing %s; "
                            "discarding partial file.",
                            filepath,
                        )
                        truncated = True
                        break
                    # Count the chunk as soon as it is booked in the budget:
                    # if f.write below raises, _discard_partial must refund
                    # this chunk too, so bytes_written has to include it.
                    bytes_written += len(chunk)
                    f.write(chunk)
                    if len(head) < _VALIDATION_HEAD_BYTES:
                        head.extend(chunk[: _VALIDATION_HEAD_BYTES - len(head)])
        except requests.exceptions.RequestException as e:
            # A connection reset or read timeout partway through the body is
            # the normal failure mode for a large PDF, and it lands here --
            # after raise_for_status, inside _process_response -- rather than
            # on the initial GET. Returning None exited download_file's retry
            # loop immediately, so max_attempts never applied to it. Refund
            # the bytes, drop the .part file, and re-raise into the loop.
            logger.warning(
                "Error while streaming %s to %s: %s; discarding partial file "
                "and retrying.",
                url,
                part_path,
                e,
            )
            _discard_partial()
            # Hand back the sequence number this attempt reserved, so the
            # retry writes p00005_..._image_001 rather than _image_002.
            release_counter(name_key)
            raise
        except OSError as e:
            # A disk-side failure will not be cured by another attempt.
            logger.error(
                "Error while streaming %s to %s: %s; discarding partial file.",
                url,
                part_path,
                e,
            )
            _discard_partial()
            return None
        except BaseException:
            # Ctrl-C (KeyboardInterrupt) and SystemExit derive from
            # BaseException, so they bypass the handler above. Without this
            # clause the .part file survives inside objects/, and the shipped
            # resume mode "skip_if_has_objects" then skips the work forever on
            # the strength of a partial file holding no usable content.
            _discard_partial()
            raise

        if truncated:
            _discard_partial()
            return None

        # Reject zero-byte downloads outright.
        if bytes_written == 0:
            logger.warning("Downloaded 0 bytes for %s; discarding.", url)
            _discard_partial()
            return None

        # When the server declared a Content-Length, require the written byte
        # count to match; a short read means the stream was truncated. Skip the
        # check for content-encoded responses (gzip/deflate/br): iter_content
        # yields DECODED bytes while Content-Length counts the encoded wire
        # bytes, so the two legitimately differ and a complete download would
        # be discarded as "incomplete".
        content_encoding = (
            (response.headers.get("Content-Encoding") or "").strip().lower()
        )
        is_identity_encoding = content_encoding in ("", "identity")
        if (
            content_len is not None
            and is_identity_encoding
            and bytes_written != content_len
        ):
            logger.error(
                "Incomplete download for %s: wrote %d of %d declared bytes; "
                "discarding.",
                url,
                bytes_written,
                content_len,
            )
            _discard_partial()
            return None

        head_bytes = bytes(head)
        head_complete = bytes_written == len(head_bytes)

        is_valid, error_msg = _validate_file_magic_bytes(
            part_path, inferred_ext, head=head_bytes, complete=head_complete
        )
        if not is_valid:
            logger.warning("%s; discarding: %s", error_msg, url)
            _discard_partial()
            return None

        if inferred_ext == ".html":
            is_valid, error_msg = _validate_html_not_login_page(
                part_path, url, provider, head=head_bytes, complete=head_complete
            )
            if not is_valid:
                logger.warning("%s; discarding: %s", error_msg, url)
                _discard_partial()
                return None

        try:
            os.replace(part_path, filepath)
        except OSError as e:
            logger.error("Failed to finalize download %s: %s", filepath, e)
            _discard_partial()
            return None

        log_suffix = " (insecure)" if is_insecure_retry else ""
        logger.info("Downloaded %s -> %s%s", url, filepath, log_suffix)
        _BUDGET.add_file(provider, work_id)

        if not counts_as_success:
            logger.info(
                "File saved to metadata folder; not counting as successful "
                "download for work completion"
            )
            return None
        return filepath

    def _calculate_backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                # Clamp to [0, max_backoff]: a malformed negative numeric
                # Retry-After must not reach time.sleep (ValueError).
                return max(0.0, min(float(retry_after), max_backoff))
            except ValueError:
                try:
                    retry_dt = parsedate_to_datetime(retry_after)
                    return min(
                        max(
                            0.0,
                            (retry_dt - datetime.now(retry_dt.tzinfo)).total_seconds(),
                        ),
                        max_backoff,
                    )
                except Exception:
                    pass
        return min(base_backoff * (backoff_mult ** (attempt - 1)), max_backoff)

    verify = verify_default
    insecure_retry_used = False

    for attempt in range(1, max_attempts + 1):
        if rl:
            rl.wait()

        try:
            with session.get(
                url,
                stream=True,
                timeout=timeout,
                verify=verify,
                headers=req_headers or None,
            ) as response:
                if response.status_code == 429:
                    sleep_s = _calculate_backoff(
                        attempt, response.headers.get("Retry-After")
                    )
                    logger.warning(
                        "429 Too Many Requests for %s; sleeping %.1fs (attempt %d/%d)",
                        url,
                        sleep_s,
                        attempt,
                        max_attempts,
                    )
                    # No point sleeping out the backoff on the final
                    # attempt; the loop is about to give up anyway.
                    if attempt < max_attempts:
                        time.sleep(sleep_s)
                    continue

                if response.status_code in (500, 502, 503, 504):
                    sleep_s = _calculate_backoff(attempt, None)
                    logger.warning(
                        "%s for %s; sleeping %.1fs (attempt %d/%d)",
                        response.status_code,
                        url,
                        sleep_s,
                        attempt,
                        max_attempts,
                    )
                    # No point sleeping out the backoff on the final
                    # attempt; the loop is about to give up anyway.
                    if attempt < max_attempts:
                        time.sleep(sleep_s)
                    continue

                if response.status_code in (400, 401, 403, 404, 410, 422):
                    # The server answered, so the transport is healthy: record
                    # the outcome, or a half-open probe spent on a dead URL
                    # would leave the breaker half-open and throttle a working
                    # provider to one request per cooldown.
                    if cb:
                        cb.record_success()
                    logger.error(
                        "Non-retryable HTTP %s for %s; aborting download",
                        response.status_code,
                        url,
                    )
                    return None

                response.raise_for_status()
                if cb:
                    cb.record_success()
                return _process_response(
                    response, is_insecure_retry=insecure_retry_used
                )

        except requests.exceptions.SSLError as e:
            # Mirror make_request: retry once with verify=False when policy
            # allows, consuming this attempt; abort on any further SSL error.
            if ssl_policy == "retry_insecure_once" and verify:
                logger.warning(
                    "SSL verify failed for %s; retrying once with verify=False "
                    "due to policy.",
                    url,
                )
                verify = False
                insecure_retry_used = True
                continue
            logger.error("SSL error downloading %s: %s", url, e)
            return None

        except requests.exceptions.RequestException as e:
            # Transient network failures (timeouts, connection resets) are
            # retried with backoff like make_request; previously any such
            # error on the initial GET aborted the download outright, so the
            # configured max_attempts never applied to the download path.
            if attempt < max_attempts:
                sleep_s = _calculate_backoff(attempt, None)
                logger.warning(
                    "Request error for %s: %s; sleeping %.1fs (attempt %d/%d)",
                    url,
                    e,
                    sleep_s,
                    attempt,
                    max_attempts,
                )
                time.sleep(sleep_s)
                continue
            logger.error("Error downloading %s: %s", url, e)
            if cb:
                cb.record_failure(provider or "unknown")
            return None

        except OSError as e:
            logger.error("Error saving file to %s: %s", folder_path, e)
            return None

    logger.error(
        "Giving up after %d attempts for %s",
        max_attempts,
        url,
    )
    if cb:
        cb.record_failure(provider or "unknown")
    return None


def save_json(data: Any, folder_path: str, filename: str) -> str | None:
    """Save data as JSON file in metadata directory.

    Args:
        data: Data to serialize
        folder_path: Base directory (will create metadata/ subdirectory)
        filename: Base filename (will be standardized with provider prefix)

    Returns:
        Path to saved file or None if skipped/failed
    """
    if not include_metadata():
        logger.debug(
            "Config download.include_metadata=false; skipping metadata save for %s",
            filename,
        )
        return None

    os.makedirs(folder_path, exist_ok=True)

    stem = get_current_name_stem() or to_snake_case(filename) or "item"
    prov_slug = get_provider_slug(get_current_provider(), None)
    meta_dir = os.path.join(folder_path, "metadata")
    os.makedirs(meta_dir, exist_ok=True)

    key = (stem, prov_slug or "unknown", "metadata")
    idx = increment_counter(key)

    base = f"{stem}_{prov_slug}" if idx <= 1 else f"{stem}_{prov_slug}_{idx}"

    filepath = os.path.join(meta_dir, sanitize_filename(base) + ".json")

    # Counters are thread-local, so a fresh thread (e.g. a parallel-mode
    # worker after reset_counters()) can recompute a name already written by
    # another thread. Bump the sequence until the path is free so earlier
    # metadata (such as the search_result JSON) is never silently replaced.
    while os.path.exists(filepath):
        idx = increment_counter(key)
        base = f"{stem}_{prov_slug}_{idx}"
        filepath = os.path.join(meta_dir, sanitize_filename(base) + ".json")

    if _BUDGET.exhausted():
        logger.warning("Download budget exhausted; skipping metadata save %s", filename)
        return None

    try:
        atomic_write_json(filepath, data)
        # Book the bytes against the metadata bucket through the enforcing
        # check-and-record path. record_download() only incremented counters,
        # so neither the metadata ceilings nor the "stop" policy applied to
        # anything written here -- and manifests for large works run to
        # megabytes. The size is unknowable before serialization, so this
        # writes first and discards on refusal, mirroring the .part semantics
        # of download_file.
        try:
            size = os.path.getsize(filepath)
        except OSError:
            size = 0
        if size and not _BUDGET.add_bytes(
            None, get_current_work(), size, content_type="metadata"
        ):
            logger.warning(
                "Metadata budget would be exceeded by %s (%d bytes); discarding.",
                filepath,
                size,
            )
            _safe_remove(filepath)
            return None
        logger.info("Saved JSON: %s", filepath)
        return filepath
    except (OSError, TypeError, ValueError) as e:
        logger.error("Error saving JSON %s: %s", filepath, e)
        return None


__all__ = ["download_file", "save_json"]
