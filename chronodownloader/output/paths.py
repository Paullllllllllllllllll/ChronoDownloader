"""Work-folder names, final file names and the folder layout of a work.

Ported from v1.30.3 (``api/core/naming.py`` and the naming parts of
``api/core/download.py``); work-folder, page-image and rendering names are
the ones v1.30.3 produced for the same inputs.

Layout. A work folder ``<output>/<work stem>/`` holds ``objects/`` (page
images and renderings), ``metadata/`` (JSON documents and the preview
image), and the staging folder ``objects/.staging/<generation>/`` of the
lease generation that downloads into it.

Work stem. :func:`work_folder_name` is v1.30.3's
``build_work_directory_name``: ``<entry slug>_<title slug>``, optionally
followed by a creator slug and the year (:class:`~chronodownloader.settings.
Naming`). The title slug is cut to ``title_slug_max_len``; a title with no
Latin letters or digits slugs to ``t_<8 hex digits>``, the SHA-1 of its NFC
form. Windows device names (``con``, ``nul``, ``com1`` ...) are wrapped in
underscores.

Final names (:func:`object_path`), with ``<stem50>`` the work stem cut to 50
characters (trailing underscores of a cut stem dropped) and ``<slug>`` the
provider slug (:data:`PROVIDER_SLUGS`):

- page image ``p00005``: ``objects/<stem50>_<slug>_image_005<ext>``, the
  number taken from the item, so a missing page leaves a gap;
- rendering ``rendering``: ``objects/<stem50>_<slug><ext>``;
- multi-file renderings: ``rendering_00001`` (one PDF per page, SBB) takes
  the index, ``rendering_p00026_00050`` (BNE page ranges of
  :data:`~chronodownloader.providers.bne.PDF_PAGE_CHUNK` pages) its range
  number counted from the first page; number 1 has no suffix, number *k*
  becomes ``objects/<stem50>_<slug>_<k><ext>``. v1.30.3 numbered these
  files in the order they arrived, so the names are equal whenever no
  earlier file failed;
- metadata ``<item>`` (kind ``metadata``, the preview image included):
  ``metadata/<stem>_<slug>_<item><ext>``, where ``<stem>_<slug>`` is cut to
  100 characters as v1.30.3 cut its metadata names. This differs from
  v1.30.3, which numbered metadata documents in save order
  (``<stem>_<slug>.json``, ``<stem>_<slug>_2.json`` ...); old output
  folders are not migrated.

Provider keys. The slugs are keyed by the v1.0.0 provider keys. Gallica's
key is ``bnf_gallica`` (v1.30.3's settings key was ``gallica``); both
versions name its files with the slug ``gallica``, so nothing on disk
changes.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from pathlib import Path, PurePath, PurePosixPath
from types import MappingProxyType

from chronodownloader.providers.base import RENDERING_ITEM, StagedFile
from chronodownloader.providers.bne import PDF_PAGE_CHUNK
from chronodownloader.settings import Naming

__all__ = [
    "INSPECT_DIR",
    "METADATA_DIR",
    "NON_DECOMPOSABLE_TRANSLIT",
    "OBJECTS_DIR",
    "PROVIDER_SLUGS",
    "STAGING_DIR",
    "WINDOWS_PATH_WARN",
    "inspect_dir",
    "object_path",
    "path_at_risk",
    "provider_slug",
    "sanitize_filename",
    "staging_dir",
    "strip_accents",
    "to_snake_case",
    "work_folder_name",
]

OBJECTS_DIR = "objects"
"""Page images and renderings, relative to the work folder."""

METADATA_DIR = "metadata"
"""Metadata documents and the preview image, relative to the work folder."""

STAGING_DIR = f"{OBJECTS_DIR}/.staging"
"""The parent of every lease generation's staging folder."""

INSPECT_DIR = "_inspect"
"""The output root's scratch folder of ``inspect``, one subfolder per work."""

WINDOWS_PATH_WARN = 170
"""Work-folder path length above which files risk Windows' MAX_PATH (260).

Files land about 90 characters deeper than the work folder
(``objects/<stem50>_<slug>_image_000.jpg``), so a folder path past 170
characters leaves too little room."""

OBJECT_STEM_MAX_LEN = 50
METADATA_BASE_MAX_LEN = 100

PROVIDER_SLUGS = MappingProxyType(
    {
        "bnf_gallica": "gallica",
        "british_library": "bl",
        "mdz": "mdz",
        "europeana": "europeana",
        "wellcome": "wellcome",
        "loc": "loc",
        "ddb": "ddb",
        "polona": "polona",
        "bne": "bne",
        "dpla": "dpla",
        "internet_archive": "ia",
        "google_books": "gb",
        "hathitrust": "hathi",
        "annas_archive": "annas",
        "slub": "slub",
        "e_rara": "erara",
        "sbb_digital": "sbb",
    }
)
"""The short provider name in file names, per provider key."""

# Latin letters NFKD does not decompose; the same table v1.30.3's slugs and
# match scores used, so a title folds to the same letters in both
# (``matching/fuzzy.py`` imports it).
NON_DECOMPOSABLE_TRANSLIT = str.maketrans(
    {
        "ß": "ss",
        "ø": "o",
        "æ": "ae",
        "œ": "oe",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
    }
)

_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# A trailing segment counts as an extension only if it is a short run of
# letters and digits: ".pdf" and ".tar" qualify, "vol. 2" does not; at most
# two segments (".tar.gz").
_EXTENSION_SEGMENT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_MAX_EXTENSION_SEGMENTS = 2
_STAGED_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")

_PAGE_ITEM = re.compile(r"p(\d+)")
_RENDERING_INDEX = re.compile(rf"{RENDERING_ITEM}_(\d+)")
_RENDERING_RANGE = re.compile(rf"{RENDERING_ITEM}_p(\d+)_(\d+)")


def strip_accents(text: str) -> str:
    """Return *text* without combining marks (NFKD), base letters kept."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def to_snake_case(value: str | None) -> str:
    """Return *value* folded to lowercase ASCII letters, digits and ``_``.

    Accents are stripped and non-decomposable Latin letters transliterated
    first, so NFC and NFD forms of a title agree; letters and digits are
    split by an underscore (``E0001`` gives ``e_0001``).
    """
    if value is None:
        return ""
    s = strip_accents(value).lower().translate(NON_DECOMPOSABLE_TRANSLIT)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s)
    s = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", s)
    s = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").lower()


def _guard_reserved_name(base: str) -> str:
    """Wrap a Windows device name in underscores so it can be created."""
    if base.lower() in _WINDOWS_RESERVED_NAMES:
        return f"_{base}_"
    return base


def _split_name_and_suffixes(name: str) -> tuple[str, str]:
    """Split *name* into its base and at most two extension-like suffixes."""
    base = PurePath(name).name
    stem = base
    segments: list[str] = []
    while len(segments) < _MAX_EXTENSION_SEGMENTS:
        match = _EXTENSION_SEGMENT.search(stem)
        if match is None:
            break
        segments.append(match.group(0))
        stem = stem[: match.start()]
    if not stem:
        return base, ""
    return stem, "".join(reversed(segments))


def sanitize_filename(name: str, max_base_len: int = 100) -> str:
    """Return *name* as a file name safe on Windows, keeping its extension.

    Illegal characters are removed from base and extension, runs of
    whitespace, dots, underscores and hyphens in the base become one
    underscore, the base is cut to *max_base_len*, device names are guarded,
    and a trailing dot or space is dropped. An empty result is
    ``_untitled_``.
    """
    if not name:
        return "_untitled_"
    base, ext = _split_name_and_suffixes(name)
    base = _ILLEGAL_CHARS.sub("", base)
    base = re.sub(r"[\s._-]+", "_", base).strip("._-")
    if not base:
        base = "_untitled_"
    base = _guard_reserved_name(base[:max_base_len])
    ext = _ILLEGAL_CHARS.sub("", ext)
    return f"{base}{ext}".rstrip(". ") or "_untitled_"


def provider_slug(provider: str) -> str:
    """Return the short name of *provider* in file names.

    Raises:
        ValueError: *provider* is not a provider key.
    """
    try:
        return PROVIDER_SLUGS[provider]
    except KeyError:
        hint = " (the Gallica key is bnf_gallica)" if provider == "gallica" else ""
        raise ValueError(f"unknown provider {provider!r}{hint}") from None


def work_folder_name(
    entry_id: str | None,
    title: str | None,
    *,
    creator: str | None = None,
    year: str | int | None = None,
    naming: Naming | None = None,
) -> str:
    """Return the work folder's name (the work stem), as v1.30.3 built it.

    ``<entry slug>_<title slug>``, then the creator slug and the year when
    *naming* includes them and they are given. A missing title gives
    ``untitled``; a title without Latin letters or digits gives ``t_`` and
    the first eight hex digits of the SHA-1 of its NFC form.
    """
    naming = naming or Naming()
    max_len = naming.title_slug_max_len
    entry_slug = to_snake_case(entry_id) if entry_id else ""
    if title:
        title_slug = to_snake_case(title)[:max_len]
        if not title_slug:
            digest = hashlib.sha1(unicodedata.normalize("NFC", title).encode("utf-8"))
            title_slug = "t_" + digest.hexdigest()[:8]
    else:
        title_slug = "untitled"
    creator_slug = ""
    if naming.include_creator_in_work_dir and creator:
        creator_slug = to_snake_case(creator)[:max_len]
    year_slug = ""
    if naming.include_year_in_work_dir and year:
        year_slug = to_snake_case(str(year))
    parts = [p for p in (entry_slug, title_slug, creator_slug, year_slug) if p]
    return _guard_reserved_name("_".join(parts) if parts else "untitled")


def _object_stem(work_stem: str) -> str:
    if len(work_stem) > OBJECT_STEM_MAX_LEN:
        return work_stem[:OBJECT_STEM_MAX_LEN].rstrip("_")
    return work_stem


def _rendering_number(item: str) -> int | None:
    """Return the file number of a rendering item, or None if it is none."""
    if item == RENDERING_ITEM:
        return 1
    match = _RENDERING_INDEX.fullmatch(item)
    if match is not None:
        index = int(match.group(1))
        return index if index >= 1 else None
    match = _RENDERING_RANGE.fullmatch(item)
    if match is not None:
        start = int(match.group(1))
        return (start - 1) // PDF_PAGE_CHUNK + 1 if start >= 1 else None
    return None


def _staged_extension(file: StagedFile) -> str:
    ext = PurePosixPath(file.name).suffix.lower()
    if not _STAGED_EXTENSION.fullmatch(ext):
        raise ValueError(f"staged file {file.name!r} has no usable extension")
    return ext


def object_path(file: StagedFile, *, work_stem: str, provider: str) -> str:
    """Return the final path of a staged file, relative to its work folder.

    *work_stem* is the work folder's name (:func:`work_folder_name`); the
    rules are in the module docstring. The result uses forward slashes.

    Raises:
        ValueError: *provider* is not a provider key, or *file* has no
            extension, an item its kind does not name (a page image that is
            not ``p<digits>``, a rendering that is not a rendering item), or
            an empty metadata item.
    """
    slug = provider_slug(provider)
    ext = _staged_extension(file)
    if file.kind == "metadata":
        item = re.sub(r"[^0-9A-Za-z]+", "_", file.item).strip("_").lower()
        if not item:
            raise ValueError(f"metadata file {file.name!r} has no item name")
        base = sanitize_filename(f"{work_stem}_{slug}", METADATA_BASE_MAX_LEN)
        return f"{METADATA_DIR}/{base}_{item}{ext}"
    stem = _object_stem(work_stem)
    if file.kind == "image":
        page = _PAGE_ITEM.fullmatch(file.item)
        if page is None:
            raise ValueError(f"image {file.name!r} is not a page item: {file.item!r}")
        name = f"{stem}_{slug}_image_{int(page.group(1)):03d}{ext}"
    else:
        number = _rendering_number(file.item)
        if number is None:
            raise ValueError(f"{file.name!r} is not a rendering item: {file.item!r}")
        suffix = "" if number <= 1 else f"_{number}"
        name = f"{stem}_{slug}{suffix}{ext}"
    return f"{OBJECTS_DIR}/{sanitize_filename(name)}"


def staging_dir(work_dir: Path, generation: int) -> Path:
    """Return the staging folder of lease *generation* in *work_dir*.

    Raises:
        ValueError: *generation* is negative.
    """
    if generation < 0:
        raise ValueError(f"generation must not be negative, got {generation}")
    return work_dir.joinpath(*STAGING_DIR.split("/"), str(generation))


def inspect_dir(root: Path, entry_id: str) -> Path:
    """Return the ``inspect`` scratch folder of work *entry_id* under *root*.

    The entry id is made a safe file name (:func:`sanitize_filename`), so the
    folder always lies directly below ``<root>/_inspect``. An id the
    sanitizing changes, or one with capitals (folder names compare without
    case), gets the first eight hex digits of its SHA-1 appended, so that
    distinct ids (``E-1``, ``E_1``, ``e_1``) never share a folder.
    """
    name = sanitize_filename(entry_id)
    if name != entry_id or entry_id != entry_id.lower():
        digest = hashlib.sha1(entry_id.encode("utf-8")).hexdigest()[:8]
        name = f"{name}_{digest}"
    return root / INSPECT_DIR / name


def path_at_risk(path: str | PurePath, *, windows: bool | None = None) -> bool:
    """Return True when a work-folder *path* risks Windows' MAX_PATH limit.

    The check is advisory: on Windows (*windows* defaults to the running
    platform) a path longer than :data:`WINDOWS_PATH_WARN` characters is at
    risk. Pass an absolute path; the function does not resolve it.
    """
    on_windows = sys.platform == "win32" if windows is None else windows
    return on_windows and len(str(path)) > WINDOWS_PATH_WARN
