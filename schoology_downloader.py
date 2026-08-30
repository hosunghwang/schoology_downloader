from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import io
import json
import os
import platform
import queue
import re
import subprocess
import threading
import traceback
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.parse import (
    parse_qsl,
    parse_qs,
    quote,
    unquote,
    urlencode,
    urldefrag,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
except Exception:  # Some development Pythons omit Tk; Windows python.org builds include it.
    tk = None
    filedialog = None
    messagebox = None
    scrolledtext = None

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
except Exception:  # The GUI gives an actionable installation error later.
    keyring = None
    KeyringError = Exception
    PasswordDeleteError = Exception

try:
    from playwright.async_api import (
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except Exception:  # Allows --self-test to run before dependencies are installed.
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


APP_NAME = "Schoology File Downloader"
APP_VERSION = "6.0"
# Keep the original vault service name so upgrades can reuse saved credentials.
CREDENTIAL_SERVICE_NAME = "Schoology PDF Downloader"
START_URL = "https://basised-tx.schoology.com/course/8442701217/materials?f=1022828767"
MAX_PAGES = 5000
MAX_CANDIDATES = 5000
MAX_WINDOWS_PATH = 235
SUPPORTED_EXTENSIONS = (".pdf", ".pptx", ".docx")
SUPPORTED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
COURSE_RE = re.compile(r"/course/(\d+)(?:/|$)", re.I)
RESOURCE_RE = re.compile(
    r"^/(?:assignment|page|discussion|assessment|course_assessment|link|"
    r"media_album|external_tool|scorm)/\d+(?:/|$)",
    re.I,
)
MATERIAL_DETAIL_RE = re.compile(r"^/course/\d+/materials/gp/\d+(?:/|$)", re.I)
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
VOLATILE_QUERY_KEYS = {
    "expires",
    "signature",
    "key-pair-id",
    "policy",
    "token",
    "auth",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
}


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "SchoologyPDFDownloader"
    return Path.home() / ".schoology_pdf_downloader"


SETTINGS_FILE = app_data_dir() / "settings.json"
PROFILE_ROOT = app_data_dir() / "BrowserProfile"


def shorten_component(value: str, limit: int, preserve_suffix: bool = False) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    marker = f"~{digest}"
    suffix = ""
    if preserve_suffix:
        suffix = Path(value).suffix
        if len(suffix) > 20:
            suffix = ""
    keep = max(1, limit - len(marker) - len(suffix))
    base = value[:keep].rstrip(" .") or "_"
    return f"{base}{marker}{suffix}"


def clean_component(value: object, limit: int = 90) -> str:
    """Return one Windows-safe path component without flattening hierarchy."""
    text = str(value or "")
    text = INVALID_WINDOWS_CHARS.sub("_", text)
    text = text.strip().rstrip(" .")
    if not text:
        text = "_"
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        text = f"_{text}"
    return shorten_component(text, limit)


def supported_filename_extension(value: object) -> str:
    text = unquote(str(value or "")).strip().strip('"')
    suffix = Path(text).suffix.lower()
    return suffix if suffix in SUPPORTED_EXTENSIONS else ""


def supported_url_extension(url: str) -> str:
    disposition_name = decode_disposition_filename(query_disposition(url))
    extension = supported_filename_extension(disposition_name)
    if extension:
        return extension
    return supported_filename_extension(Path(unquote(urlparse(url).path)).name)


def clean_filename(
    value: object,
    expected_extension: str = ".pdf",
    limit: int = 180,
) -> str:
    expected_extension = expected_extension.lower()
    if expected_extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension: {expected_extension}")

    text = unquote(str(value or "")).strip()
    text = INVALID_WINDOWS_CHARS.sub("_", text).rstrip(" .") or f"download{expected_extension}"
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        text = f"_{text}"

    suffix = Path(text).suffix
    if suffix.lower() in SUPPORTED_EXTENSIONS:
        if suffix.lower() != expected_extension:
            text = f"{text[:-len(suffix)]}{expected_extension}"
    else:
        text += expected_extension
    return shorten_component(text, limit, preserve_suffix=True)


def decode_disposition_filename(content_disposition: str) -> str:
    if not content_disposition:
        return ""

    match = re.search(r"filename\*\s*=\s*([^;]+)", content_disposition, re.I)
    if match:
        raw = match.group(1).strip().strip('"')
        if "''" in raw:
            encoding, raw = raw.split("''", 1)
            try:
                return unquote(raw, encoding=encoding or "utf-8", errors="replace")
            except LookupError:
                return unquote(raw)
        return unquote(raw)

    match = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, re.I)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", content_disposition, re.I)
    return unquote(match.group(1).strip().strip('"')) if match else ""


def query_disposition(url: str) -> str:
    for key, values in parse_qs(urlparse(url).query, keep_blank_values=True).items():
        if key.lower() in {"content-disposition", "response-content-disposition"} and values:
            # Schoology encodes spaces in these values as literal encoded plus signs.
            return values[0].replace("+", " ")
    return ""


def filename_from_response(
    source_url: str,
    final_url: str,
    headers: dict[str, str] | None,
    link_text: str = "",
    expected_extension: str = ".pdf",
) -> str:
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    choices = [
        decode_disposition_filename(headers.get("content-disposition", "")),
        decode_disposition_filename(query_disposition(final_url)),
        decode_disposition_filename(query_disposition(source_url)),
    ]

    for url in (final_url, source_url):
        query = parse_qs(urlparse(url).query, keep_blank_values=True)
        for key in ("filename", "file", "name"):
            if query.get(key):
                choices.append(query[key][0])

    for url in (final_url, source_url):
        name = Path(unquote(urlparse(url).path)).name
        if name and "." in name:
            choices.append(name)

    for line in str(link_text or "").splitlines():
        line = line.strip()
        if supported_filename_extension(line):
            choices.append(line)

    for choice in choices:
        if supported_filename_extension(choice) == expected_extension:
            return clean_filename(choice, expected_extension)
    for choice in choices:
        if choice:
            return clean_filename(choice, expected_extension)
    return f"download{expected_extension}"


def extract_course_id(url: str) -> str | None:
    match = COURSE_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def canonical_url(url: str) -> str:
    return urldefrag(str(url or ""))[0]


def stable_url(url: str) -> str:
    """Remove expiring signature parameters before hashing a source identity."""
    parts = urlsplit(canonical_url(url))
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in VOLATILE_QUERY_KEYS
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(kept), "")
    )


def log_safe_url(url: str) -> str:
    """Remove expiring authorization values before a URL reaches a log file."""
    return stable_url(url)


def scrub_log_secrets(value: object) -> str:
    text = str(value)
    for key in VOLATILE_QUERY_KEYS:
        text = re.sub(
            rf"(?i)([?&]{re.escape(key)}=)[^&\s]+",
            rf"\1<redacted>",
            text,
        )
    return text


def source_key(url: str, folders: Iterable[str]) -> str:
    raw = stable_url(url) + "\n" + "/".join(folders)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def folder_id(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("f", [])
    return values[0] if values else ""


def is_allowed_page_url(url: str, host: str, course_id: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host.lower():
        return False

    match = COURSE_RE.search(parsed.path)
    if match:
        if match.group(1) != course_id:
            return False
        remainder = parsed.path[match.end() :].lower()
        return remainder.startswith(
            (
                "materials",
                "assignment",
                "page",
                "discussion",
                "assessment",
                "course_assessment",
                "link",
                "media_album",
                "external_tool",
                "scorm",
            )
        )

    # Schoology frequently exposes material detail pages outside /course/<id>.
    # These links are accepted only when found inside an already-scoped course page.
    return bool(RESOURCE_RE.match(parsed.path))


def is_material_detail_url(url: str, host: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == host.lower()
        and bool(MATERIAL_DETAIL_RE.match(parsed.path))
    )


def looks_like_file_candidate(url: str, text: str = "", download_attr: str = "") -> bool:
    decoded = unquote(str(url or "")).lower()
    label = str(text or "").lower()
    path = urlparse(decoded).path
    has_supported_name = any(
        extension in decoded or extension in label
        for extension in SUPPORTED_EXTENSIONS
    )
    return any(
        (
            has_supported_name,
            "content-disposition=" in decoded,
            bool(download_attr),
            "/file/" in path,
            "/attachment" in path,
            "/download" in path,
            "download=" in decoded,
            any(label.strip().endswith(extension) for extension in SUPPORTED_EXTENSIONS),
            "download pdf" in label,
            "download powerpoint" in label,
            "download word" in label,
        )
    )


def looks_like_pdf(body: bytes, content_type: str = "") -> bool:
    del content_type
    return b"%PDF-" in body[:1024]


def content_type_extension(content_type: str) -> str:
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    return SUPPORTED_CONTENT_TYPES.get(mime, "")


def detect_supported_extension(body: bytes, content_type: str = "") -> str:
    if looks_like_pdf(body, content_type):
        return ".pdf"

    if not body.startswith(b"PK"):
        return ""

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                return ""
            content_types = archive.read("[Content_Types].xml").lower()
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return ""

    if (
        "word/document.xml" in names
        and b"wordprocessingml.document.main+xml" in content_types
    ):
        return ".docx"
    if (
        "ppt/presentation.xml" in names
        and b"presentationml.presentation.main+xml" in content_types
    ):
        return ".pptx"
    return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def fit_destination_parts(
    root: Path,
    folders: Iterable[str],
    filename: str,
    max_path: int = MAX_WINDOWS_PATH,
) -> tuple[list[str], str]:
    safe_folders = [clean_component(part) for part in folders if str(part or "").strip()]
    if not safe_folders:
        safe_folders = ["Materials"]
    safe_name = clean_filename(
        filename,
        supported_filename_extension(filename) or ".pdf",
    )

    def total_length() -> int:
        return len(str(root.joinpath(*safe_folders, safe_name)))

    while total_length() > max_path:
        candidates = [(len(value), index) for index, value in enumerate(safe_folders) if len(value) > 24]
        if not candidates:
            break
        _, index = max(candidates)
        safe_folders[index] = shorten_component(safe_folders[index], max(24, len(safe_folders[index]) - 12))

    while total_length() > max_path and len(safe_name) > 48:
        safe_name = shorten_component(safe_name, max(48, len(safe_name) - 12), preserve_suffix=True)

    if total_length() > max_path:
        raise OSError(
            f"The selected output folder is too long for a safe Windows path: {root}"
        )
    return safe_folders, safe_name


class DownloadState:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".schoology_pdf_downloader_state.json"
        self.entries: dict[str, dict[str, object]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                self.entries = raw["entries"]
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self.entries = {}

    def save(self) -> None:
        payload = {"version": 1, "entries": self.entries}
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)

    def resolve_recorded(self, key: str) -> Path | None:
        record = self.entries.get(key)
        relative = record.get("path") if isinstance(record, dict) else None
        if not isinstance(relative, str):
            return None
        candidate = self.root.joinpath(*PurePosixPath(relative).parts)
        try:
            candidate.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError):
            return None
        return candidate

    def record(self, key: str, path: Path, digest: str, size: int) -> None:
        self.entries[key] = {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": digest,
            "size": size,
        }
        self.save()


def select_destination(
    root: Path,
    folders: Iterable[str],
    filename: str,
    state: DownloadState,
    key: str,
    digest: str,
) -> tuple[Path, bool, str]:
    safe_folders, safe_name = fit_destination_parts(root, folders, filename)
    directory = root.joinpath(*safe_folders)
    directory.mkdir(parents=True, exist_ok=True)
    recorded = state.resolve_recorded(key)
    expected_suffix = Path(safe_name).suffix.lower()
    if (
        recorded
        and recorded.is_file()
        and recorded.suffix.lower() == expected_suffix
        and recorded.parent.resolve() == directory.resolve()
    ):
        try:
            if file_sha256(recorded) == digest:
                return recorded, True, "saved source"
        except OSError:
            pass

    desired = directory / safe_name
    stem, suffix = desired.stem, desired.suffix

    for index in range(1, 10000):
        candidate = desired if index == 1 else directory / f"{stem} [{index}]{suffix}"
        if not candidate.exists() or candidate.stat().st_size == 0:
            return candidate, False, "new file"
        try:
            if file_sha256(candidate) == digest:
                return candidate, True, "identical file"
        except OSError:
            pass
    raise OSError(f"Too many filename collisions for {desired}")


def atomic_write(path: Path, body: bytes) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.part")
    with temp.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def best_label(text: str, context: str = "", fallback: str = "") -> str:
    generic = {"", "folder", "open", "view", "download", "document", "file"}
    for source in (text, context):
        for line in str(source or "").splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line.lower() not in generic and len(line) <= 180:
                return line
    return fallback


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value and key.lower() in {"href", "src", "data-url", "data-href", "data-download-url"}:
                self.urls.append(value)


def extract_file_urls_from_text(text: str, base_url: str) -> list[str]:
    decoded_variants: list[str] = []
    decoded = html_unescape(str(text or ""))
    for _ in range(4):
        decoded = re.sub(r"\\u002[fF]", "/", decoded)
        decoded = re.sub(r"\\u003[aA]", ":", decoded)
        decoded = decoded.replace("\\/", "/")
        if decoded not in decoded_variants:
            decoded_variants.append(decoded)
        next_decoded = html_unescape(unquote(decoded))
        if next_decoded == decoded:
            break
        decoded = next_decoded

    parser = LinkParser()
    try:
        parser.feed(decoded_variants[0])
    except Exception:
        pass
    absolute: list[str] = []
    for variant in decoded_variants:
        absolute.extend(re.findall(r"https?://[^\s'\"<>]+", variant, flags=re.I))
    result: list[str] = []
    seen: set[str] = set()
    for value in [*parser.urls, *absolute]:
        url = canonical_url(urljoin(base_url, value.rstrip(",);")))
        if url not in seen and looks_like_file_candidate(url):
            seen.add(url)
            result.append(url)
    return result


def iter_json_file_links(value: object, base_url: str, inherited_name: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        local_name = inherited_name
        for key in ("filename", "file_name", "display_name", "title", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                local_name = candidate.strip()
                break
        for key, child in value.items():
            if isinstance(child, str) and (
                "url" in key.lower()
                or "path" in key.lower()
                or "download" in key.lower()
                or looks_like_file_candidate(child, local_name)
            ):
                url = canonical_url(urljoin(base_url, child))
                if looks_like_file_candidate(url, local_name):
                    yield url, local_name
            elif isinstance(child, (dict, list, tuple)):
                yield from iter_json_file_links(child, base_url, local_name)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from iter_json_file_links(child, base_url, inherited_name)


@dataclass
class Candidate:
    url: str
    folders: tuple[str, ...]
    text: str = ""
    referrer: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "page"

    @property
    def identity(self) -> tuple[str, tuple[str, ...]]:
        return stable_url(self.url), self.folders


class CandidateRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, tuple[str, ...]], Candidate] = {}

    def add(self, candidate: Candidate) -> bool:
        candidate.url = canonical_url(candidate.url)
        parsed = urlparse(candidate.url)
        if parsed.scheme not in {"http", "https"}:
            return False
        key = candidate.identity
        current = self._items.get(key)
        if current:
            if not current.text and candidate.text:
                current.text = candidate.text
            if not current.headers and candidate.headers:
                current.headers = candidate.headers
            return False
        self._items[key] = candidate
        return True

    def values(self) -> list[Candidate]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class NetworkCollector:
    def __init__(self, registry: CandidateRegistry):
        self.registry = registry
        self.folders: tuple[str, ...] = ("Materials",)
        self.referrer = ""
        self.tasks: set[asyncio.Task] = set()
        self.attached_pages: set[int] = set()

    def set_context(self, folders: tuple[str, ...], referrer: str) -> None:
        self.folders = folders
        self.referrer = referrer

    def attach(self, page) -> None:
        page_id = id(page)
        if page_id in self.attached_pages:
            return
        self.attached_pages.add(page_id)

        def on_response(response) -> None:
            task = asyncio.create_task(
                self._inspect_response(response, self.folders, self.referrer)
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        page.on("response", on_response)

    async def _inspect_response(self, response, folders: tuple[str, ...], referrer: str) -> None:
        try:
            headers = {str(k).lower(): str(v) for k, v in (await response.all_headers()).items()}
            content_type = headers.get("content-type", "").lower()
            disposition = headers.get("content-disposition", "")
            url = canonical_url(response.url)
            if content_type_extension(content_type) or looks_like_file_candidate(
                url, disposition
            ):
                self.registry.add(
                    Candidate(url, folders, referrer=referrer, headers=headers, method="network")
                )
                return

            content_length = int(headers.get("content-length", "0") or 0)
            if "json" in content_type and content_length <= 5 * 1024 * 1024:
                data = await response.json()
                for link, name in iter_json_file_links(data, url):
                    self.registry.add(
                        Candidate(link, folders, text=name, referrer=referrer, method="network-json")
                    )
        except Exception:
            # Network discovery is supplemental; DOM discovery remains available.
            return

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def get_dom_links(page) -> list[dict[str, object]]:
    return await page.eval_on_selector_all(
        "a[href], [data-url], [data-href], [data-download-url]",
        """els => els.map(e => {
          const raw = e.href || e.dataset.url || e.dataset.href || e.dataset.downloadUrl || '';
          const holder = e.closest(
            '[role="main"], main, #main-content, #main-content-wrapper, #center-top, '
            + '.material-row, .s-js-materials-item, .item-content, article, li, tr'
          );
          const inContent = !!e.closest(
            '[role="main"], main, #main-content, #main-content-wrapper, #center-top, '
            + '.material-row, .s-js-materials-item, .item-content'
          );
          return {
            href: raw,
            text: (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 500),
            context: holder ? (holder.innerText || holder.textContent || '').trim().slice(0, 1000) : '',
            download: e.getAttribute('download') || '',
            inContent
          };
        }).filter(x => x.href)""",
    )


async def infer_material_folders(page, page_url: str) -> tuple[str, ...]:
    """Read only Schoology breadcrumb and parent-folder elements."""
    parsed = urlparse(page_url)
    if not MATERIAL_DETAIL_RE.match(parsed.path):
        return ()

    selectors = [
        'nav[aria-label*="breadcrumb" i] a[href*="/materials?f="]',
        '.folder-title a[href*="/materials?f="]',
        'a.folder-title[href*="/materials?f="]',
    ]

    items: list[dict[str, str]] = []
    for selector in selectors:
        try:
            items.extend(
                await page.eval_on_selector_all(
                    selector,
                    """els => els.map(e => ({
                      href: e.href || '',
                      text: (e.innerText || e.textContent || '').trim()
                    }))""",
                )
            )
        except Exception:
            continue

    folders = ["Materials"]
    seen_folder_ids: set[str] = set()
    for item in items:
        href = canonical_url(urljoin(page_url, str(item.get("href") or "")))
        linked_folder = folder_id(href)
        if not linked_folder or linked_folder in seen_folder_ids:
            continue
        name = best_label(str(item.get("text") or ""))
        if not name:
            continue
        seen_folder_ids.add(linked_folder)
        folders.append(clean_component(name))
    return tuple(folders) if len(folders) > 1 else ()


async def scroll_lazy_content(page) -> None:
    stable = 0
    previous = 0
    for _ in range(18):
        height = await page.evaluate("document.documentElement.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await page.wait_for_timeout(350)
        new_height = await page.evaluate("document.documentElement.scrollHeight")
        stable = stable + 1 if new_height == previous == height else 0
        previous = new_height
        if stable >= 2:
            break


async def discover_rendered_file_links(
    context,
    url: str,
    referrer: str,
) -> tuple[list[str], str]:
    """Render a Schoology viewer so its JavaScript-created original-file URL exists."""
    page = await context.new_page()
    try:
        goto_options: dict[str, object] = {
            "wait_until": "domcontentloaded",
            "timeout": 60000,
        }
        if referrer:
            goto_options["referer"] = referrer
        await page.goto(url, **goto_options)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(750)

        documents = [page.url, await page.content()]
        documents.extend(frame.url for frame in page.frames)
        links: list[str] = []
        seen: set[str] = set()
        for document in documents:
            for link in extract_file_urls_from_text(document, page.url):
                if link not in seen:
                    seen.add(link)
                    links.append(link)
        return links, page.url
    finally:
        await page.close()


def credential_service(host: str) -> str:
    return f"{CREDENTIAL_SERVICE_NAME}:{host.lower()}"


class CredentialVaultError(RuntimeError):
    pass


def uses_native_macos_keychain() -> bool:
    return platform.system() == "Darwin" and Path("/usr/bin/security").is_file()


def credential_vault_available() -> bool:
    return uses_native_macos_keychain() or keyring is not None


def run_macos_security(arguments: list[str], password_input: str | None = None):
    try:
        stdin_value = None
        if password_input is not None:
            # A new Keychain item asks for the value twice; an update consumes
            # only the first line and safely ignores the second.
            stdin_value = f"{password_input}\n{password_input}\n"
        return subprocess.run(
            ["/usr/bin/security", *arguments],
            input=stdin_value,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialVaultError(f"macOS Keychain command failed: {exc}") from exc


def vault_get_password(host: str, username: str) -> str:
    if not host or not username:
        return ""
    service = credential_service(host)
    if uses_native_macos_keychain():
        result = run_macos_security(
            ["find-generic-password", "-a", username, "-s", service, "-w"]
        )
        if result.returncode == 0:
            return result.stdout.rstrip("\r\n")
        error = result.stderr.strip()
        if "could not be found" in error.lower():
            return ""
        raise CredentialVaultError(error or "Could not read the macOS Keychain.")
    if keyring is None:
        raise CredentialVaultError("No supported operating-system credential vault is available.")
    try:
        return keyring.get_password(service, username) or ""
    except KeyringError as exc:
        raise CredentialVaultError(str(exc)) from exc


def vault_set_password(host: str, username: str, password: str) -> None:
    if not host or not username or not password:
        raise CredentialVaultError("Host, username, and password are required.")
    service = credential_service(host)
    if uses_native_macos_keychain():
        # Supplying -w without a value makes `security` read the password from
        # stdin, keeping it out of the process list and project files.
        result = run_macos_security(
            [
                "add-generic-password",
                "-a",
                username,
                "-s",
                service,
                "-U",
                "-w",
            ],
            password_input=password,
        )
        if result.returncode != 0:
            raise CredentialVaultError(
                result.stderr.strip() or "Could not write to the macOS Keychain."
            )
        return
    if keyring is None:
        raise CredentialVaultError("No supported operating-system credential vault is available.")
    try:
        keyring.set_password(service, username, password)
    except KeyringError as exc:
        raise CredentialVaultError(str(exc)) from exc


def vault_delete_password(host: str, username: str) -> bool:
    if not host or not username:
        return False
    service = credential_service(host)
    if uses_native_macos_keychain():
        result = run_macos_security(
            ["delete-generic-password", "-a", username, "-s", service]
        )
        if result.returncode == 0:
            return True
        if "could not be found" in result.stderr.lower():
            return False
        raise CredentialVaultError(
            result.stderr.strip() or "Could not delete the macOS Keychain item."
        )
    if keyring is None:
        raise CredentialVaultError("No supported operating-system credential vault is available.")
    try:
        keyring.delete_password(service, username)
        return True
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise CredentialVaultError(str(exc)) from exc


def allowed_autofill_host(host: str, schoology_host: str) -> bool:
    host = host.lower().split(":", 1)[0]
    return host == schoology_host.lower() or host in {
        "login.microsoftonline.com",
        "login.microsoft.com",
        "login.live.com",
        "login.windows.net",
    }


async def first_visible(page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=700):
                return locator
        except Exception:
            continue
    return None


async def first_visible_text(page, patterns: Iterable[str]):
    for pattern in patterns:
        locator = page.get_by_text(re.compile(pattern, re.I), exact=False).first
        try:
            if await locator.is_visible(timeout=700):
                return locator
        except Exception:
            continue
    return None


async def course_page_ready(page, course_id: str) -> bool:
    if extract_course_id(page.url) != course_id:
        return False
    login_field = await first_visible(
        page,
        (
            "input[name='loginfmt']",
            "input[type='email']",
            "input[name='passwd']",
            "input[type='password']",
            "#edit-mail",
            "#edit-pass",
        ),
    )
    return login_field is None


async def find_course_page(context, course_id: str):
    for candidate in reversed(context.pages):
        try:
            if not candidate.is_closed() and await course_page_ready(candidate, course_id):
                return candidate
        except Exception:
            continue
    return None


async def choose_login_page(context, current_page, schoology_host: str, course_id: str):
    course_page = await find_course_page(context, course_id)
    if course_page is not None:
        return course_page
    for candidate in reversed(context.pages):
        try:
            host = urlparse(candidate.url).netloc.lower()
            if not candidate.is_closed() and allowed_autofill_host(host, schoology_host):
                return candidate
        except Exception:
            continue
    return current_page


async def click_submit(page) -> bool:
    submit = await first_visible(
        page,
        (
            "#idSIButton9",
            "button[name='login']",
            "button[type='submit']",
            "input[type='submit']",
        ),
    )
    if submit is None:
        return False
    await submit.click()
    return True


async def attempt_credential_login(
    page,
    schoology_host: str,
    course_id: str,
    username: str,
    password: str,
    log: Callable[[str], None],
) -> tuple[object, bool]:
    """Fill known Schoology/Microsoft forms. Never sends credentials to another host."""
    if not username or not password:
        log("[LOGIN] No reusable password was supplied; browser sign-in may be required.")
        return page, False

    context = page.context
    submitted = False
    completed_actions: set[tuple[str, str]] = set()
    idle_cycles = 0
    previous_host = ""
    deadline = asyncio.get_running_loop().time() + 60

    log("[LOGIN] Reusable credentials are available; attempting automatic sign-in.")
    while asyncio.get_running_loop().time() < deadline:
        page = await choose_login_page(context, page, schoology_host, course_id)
        if await course_page_ready(page, course_id):
            log("[LOGIN] Automatic sign-in reached the requested course.")
            return page, submitted

        host = urlparse(page.url).netloc.lower()
        if host and host != previous_host:
            log(f"[LOGIN] Sign-in page: {host}")
            previous_host = host
        if not allowed_autofill_host(host, schoology_host):
            log(f"[LOGIN] Automatic entry stopped on unapproved host: {host or '(unknown)'}")
            return page, submitted

        action_taken = False
        page_key = canonical_url(page.url)

        if submitted:
            explicit_error = await first_visible(
                page,
                (
                    "#usernameError:not(:empty)",
                    "#passwordError:not(:empty)",
                    ".messages.error",
                    ".alert-error",
                ),
            )
            if explicit_error:
                log(
                    "[LOGIN] The sign-in page reported an error. Automatic retries stopped "
                    "to avoid repeated password attempts."
                )
                return page, submitted

        email = await first_visible(
            page,
            (
                "input[name='loginfmt']",
                "input[type='email']",
                "input[name='mail']",
                "input[name='username']",
                "#edit-mail",
            ),
        )
        password_box = await first_visible(
            page,
            ("input[name='passwd']", "input[type='password']", "#edit-pass"),
        )

        # Microsoft now also uses a combined username/password form in some
        # tenants. Fill both fields before submitting whenever both are visible.
        combined_key = ("combined-credentials", page_key)
        if email and password_box and combined_key not in completed_actions:
            await email.fill(username)
            await password_box.fill(password)
            email_ok = await email.input_value() == username
            password_ok = await password_box.input_value() == password
            if not email_ok or not password_ok:
                log("[LOGIN] The sign-in form did not retain the supplied credentials.")
                return page, submitted
            if await click_submit(page):
                completed_actions.add(combined_key)
                submitted = True
                action_taken = True
                log("[LOGIN] Username and password submitted securely.")

        # District Schoology pages may expose SSO as a local redirect rather than
        # a direct microsoftonline.com link, so also match a narrowly scoped label.
        sso_key = ("sso", page_key)
        if host == schoology_host.lower() and not action_taken and sso_key not in completed_actions:
            sso_link = await first_visible(
                page,
                (
                    "a[href*='microsoftonline.com']",
                    "a[href*='login.microsoft']",
                    "a[href*='microsoft']",
                    "a[href*='/sso']",
                    "button[data-provider*='microsoft' i]",
                ),
            )
            if sso_link is None:
                sso_link = await first_visible_text(
                    page,
                    (
                        r"(?:log|sign)\s*in\s+(?:with|using)\s+Microsoft",
                        r"Microsoft\s+(?:log|sign)\s*in",
                        r"single\s+sign[ -]?on",
                    ),
                )
            if sso_link:
                await sso_link.click()
                completed_actions.add(sso_key)
                submitted = True
                action_taken = True
                log("[LOGIN] Opened the Microsoft sign-in page.")

        password_key = ("password", page_key)
        if password_box and not action_taken and password_key not in completed_actions:
            await password_box.fill(password)
            if await password_box.input_value() != password:
                log("[LOGIN] The password field did not retain the supplied value.")
                return page, submitted
            if await click_submit(page):
                completed_actions.add(password_key)
                submitted = True
                action_taken = True
                log("[LOGIN] Password submitted securely; approve MFA if requested.")

        email_key = ("email", page_key)
        if email and not action_taken and email_key not in completed_actions:
            await email.fill(username)
            if await email.input_value() != username:
                log("[LOGIN] The username field did not retain the supplied value.")
                return page, submitted
            if await click_submit(page):
                completed_actions.add(email_key)
                submitted = True
                action_taken = True
                log("[LOGIN] Username entered securely.")

        account_key = ("account", page_key)
        if host in {
            "login.microsoftonline.com",
            "login.microsoft.com",
            "login.live.com",
            "login.windows.net",
        } and not action_taken and account_key not in completed_actions:
            account = page.get_by_text(username, exact=True).first
            try:
                if await account.is_visible(timeout=700):
                    await account.click()
                    completed_actions.add(account_key)
                    submitted = True
                    action_taken = True
                    log("[LOGIN] Selected the saved Microsoft account.")
            except Exception:
                pass

        other_key = ("other-account", page_key)
        if host in {
            "login.microsoftonline.com",
            "login.microsoft.com",
            "login.live.com",
            "login.windows.net",
        } and not action_taken and other_key not in completed_actions:
            other_account = await first_visible(
                page,
                ("#otherTile", "#otherTileText", "[data-test-id='otherTile']"),
            )
            if other_account is None:
                other_account = await first_visible_text(page, (r"use another account",))
            if other_account:
                await other_account.click()
                completed_actions.add(other_key)
                submitted = True
                action_taken = True
                log("[LOGIN] Selected 'Use another account'.")

        stay_key = ("stay-signed-in", page_key)
        if host in {
            "login.microsoftonline.com",
            "login.microsoft.com",
            "login.live.com",
            "login.windows.net",
        } and not action_taken and stay_key not in completed_actions:
            stay_prompt = await first_visible_text(page, (r"stay signed in",))
            if stay_prompt:
                yes_button = await first_visible(page, ("#idSIButton9", "input[type='submit']"))
                if yes_button:
                    await yes_button.click()
                    completed_actions.add(stay_key)
                    submitted = True
                    action_taken = True
                    log("[LOGIN] Confirmed the reusable Microsoft session.")

        if action_taken:
            idle_cycles = 0
            await asyncio.sleep(1.2)
            continue

        idle_cycles += 1
        if submitted and idle_cycles >= 20:
            log("[LOGIN] Waiting for MFA or another Microsoft approval in the browser.")
            return page, submitted
        if not submitted and idle_cycles >= 20:
            log("[LOGIN] No recognized automatic sign-in control was found.")
            return page, submitted
        await asyncio.sleep(0.5)

    log("[LOGIN] Automatic sign-in timed out; finish the remaining step in the browser.")
    return page, submitted


class RunLogger:
    def __init__(self, root: Path, emit: Callable[[str], None]):
        self.emit = emit
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = root / f"schoology-downloader-{stamp}.log"

    def __call__(self, message: str = "") -> None:
        line = scrub_log_secrets(message)
        self.emit(line)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


async def fetch_and_save_candidate(
    context,
    candidate: Candidate,
    root: Path,
    state: DownloadState,
    log: Callable[[str], None],
    registry: CandidateRegistry,
) -> str:
    headers = {"referer": candidate.referrer} if candidate.referrer else None
    response = None
    try:
        candidate_extension = (
            supported_filename_extension(candidate.text)
            or supported_url_extension(candidate.url)
        )
        if candidate_extension in {".docx", ".pptx"}:
            log(
                f"[FETCH] Requesting {candidate_extension} candidate "
                f"via={candidate.method}: {log_safe_url(candidate.url)}"
            )
        response = await context.request.get(
            candidate.url,
            headers=headers,
            timeout=120000,
            max_redirects=20,
            fail_on_status_code=False,
        )
        response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        body = await response.body()
        final_url = canonical_url(response.url)
        content_type = response_headers.get("content-type", "")

        if response.status >= 400:
            log(f"[FAIL] HTTP {response.status}: {log_safe_url(candidate.url)}")
            return "failed"

        detected_extension = detect_supported_extension(body, content_type)
        if not detected_extension:
            if "html" in content_type.lower() or "json" in content_type.lower():
                links = extract_file_urls_from_text(
                    body.decode("utf-8", "replace"), final_url
                )
                discovered_links = [(link, final_url) for link in links]
                expected_extension = (
                    supported_filename_extension(candidate.text)
                    or supported_url_extension(candidate.url)
                )
                matching_links = [
                    item
                    for item in discovered_links
                    if supported_url_extension(item[0]) == expected_extension
                ]
                if (
                    expected_extension in {".docx", ".pptx"}
                    and supported_url_extension(candidate.url) != expected_extension
                ):
                    try:
                        rendered_links, rendered_referrer = (
                            await discover_rendered_file_links(
                                context,
                                candidate.url,
                                candidate.referrer,
                            )
                        )
                        discovered_links.extend(
                            (link, rendered_referrer or final_url)
                            for link in rendered_links
                        )
                        matching_links = [
                            item
                            for item in discovered_links
                            if supported_url_extension(item[0]) == expected_extension
                        ]
                        if matching_links:
                            log(
                                f"[DISCOVER] Found original {expected_extension} "
                                "download in the rendered Schoology viewer."
                            )
                    except Exception as exc:
                        log(f"[DISCOVER] Could not render the document viewer: {exc}")
                if expected_extension and matching_links:
                    discovered_links = matching_links
                for link, link_referrer in discovered_links:
                    added = registry.add(
                        Candidate(
                            link,
                            candidate.folders,
                            text=candidate.text,
                            referrer=link_referrer,
                            method="response-body",
                        )
                    )
                    link_extension = supported_url_extension(link)
                    if link_extension in {".docx", ".pptx"}:
                        status = "Queued" if added else "Already queued"
                        log(f"[DISCOVER] {status} original {link_extension} download.")
            filename_hint = (
                decode_disposition_filename(response_headers.get("content-disposition", ""))
                or decode_disposition_filename(query_disposition(final_url))
                or decode_disposition_filename(query_disposition(candidate.url))
                or candidate.text
            )
            filename_hint = re.sub(r"\s+", " ", filename_hint).strip()[:180] or "unknown"
            log(
                f"[UNSUPPORTED] type={content_type or 'unknown'} "
                f"name={filename_hint} via={candidate.method}: "
                f"{log_safe_url(candidate.url)}"
            )
            return "not_supported"

        combined_headers = {**candidate.headers, **response_headers}
        filename = filename_from_response(
            candidate.url,
            final_url,
            combined_headers,
            candidate.text,
            expected_extension=detected_extension,
        )
        digest = bytes_sha256(body)
        key = source_key(candidate.url, candidate.folders)
        destination, skip, reason = select_destination(
            root, candidate.folders, filename, state, key, digest
        )
        if skip:
            state.record(key, destination, digest, len(body))
            log(f"[SKIP] {destination} ({reason})")
            return "skipped"

        atomic_write(destination, body)
        state.record(key, destination, digest, len(body))
        log(f"[DONE] {destination} ({len(body):,} bytes)")
        return "downloaded"
    except Exception as exc:
        log(f"[FAIL] {log_safe_url(candidate.url)} -> {exc}")
        return "failed"
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass


@dataclass
class RunSummary:
    pages: int = 0
    candidates: int = 0
    files: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    rejected: int = 0
    login_confirmed: bool = False
    log_path: Path | None = None


class UserCancelled(Exception):
    pass


async def run_downloader(
    start_url: str,
    output: str,
    username: str,
    password: str,
    login_continue: threading.Event,
    cancel_event: threading.Event,
    emit: Callable[[str], None],
    crawl_all: bool = False,
) -> RunSummary:
    if async_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run the install_and_run launcher for your "
            "operating system, then try again."
        )

    start = canonical_url(start_url.strip())
    parsed = urlparse(start)
    course_id = extract_course_id(start)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not course_id:
        raise ValueError("Enter a Schoology URL containing /course/<course id>/materials.")

    root = Path(output).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(root, emit)
    log = logger
    log(f"{APP_NAME} v{APP_VERSION}")
    log(f"Course ID: {course_id}")
    log(
        "Scan mode: all linked course material pages"
        if crawl_all
        else "Scan mode: provided URL only"
    )
    log(f"Output: {root}")
    log("Passwords and signed CDN query strings are never written to this log.")

    summary = RunSummary(log_path=logger.path)
    state = DownloadState(root)
    registry = CandidateRegistry()
    processed_candidates: set[tuple[str, tuple[str, ...]]] = set()
    host = parsed.netloc.lower()
    profile = PROFILE_ROOT / clean_component(host, limit=70)
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                headless=False,
                accept_downloads=True,
                viewport={"width": 1440, "height": 950},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            collector = NetworkCollector(registry)
            collector.attach(page)
            collector.set_context(("Materials",), start)

            log("[LOGIN] Opening the reusable browser profile.")
            await page.goto(start, wait_until="domcontentloaded", timeout=90000)
            if not await course_page_ready(page, course_id):
                page, _ = await attempt_credential_login(
                    page, host, course_id, username, password, log
                )
                collector.attach(page)

            for _ in range(20):
                ready_page = await find_course_page(context, course_id)
                if ready_page is not None:
                    page = ready_page
                    collector.attach(page)
                    break
                await asyncio.sleep(0.5)

            if await find_course_page(context, course_id) is not None:
                login_continue.set()
                log("[LOGIN] Existing or automatic sign-in succeeded; continuing.")
            else:
                emit("@@LOGIN_READY@@")

            while not login_continue.is_set():
                if cancel_event.is_set():
                    raise UserCancelled("Cancelled by user.")
                await asyncio.sleep(0.2)

            ready_page = await find_course_page(context, course_id)
            if ready_page is not None:
                page = ready_page
                collector.attach(page)
            await page.goto(start, wait_until="domcontentloaded", timeout=90000)
            if await course_page_ready(page, course_id):
                summary.login_confirmed = True
                log("[LOGIN] Authenticated course page confirmed.")
            else:
                raise RuntimeError(
                    "The course page did not open after login. Complete Microsoft SSO/MFA, "
                    "then press Continue after login."
                )

            # Do not carry candidates observed during login or redirects into the scan.
            # The scan below reloads the supplied URL and captures its responses afresh.
            await collector.drain()
            registry.clear()

            initial_folders = await infer_material_folders(page, start)
            if not initial_folders:
                initial_folders = ("Materials",)
            collector.set_context(initial_folders, start)

            page_queue: deque[tuple[str, tuple[str, ...], bool]] = deque(
                [(start, initial_folders, crawl_all)]
            )
            queued = {start}
            seen: set[str] = set()

            async def process_pending() -> None:
                while len(processed_candidates) < MAX_CANDIDATES:
                    pending = [
                        item
                        for item in registry.values()
                        if item.identity not in processed_candidates
                    ]
                    if not pending:
                        return
                    for item in pending:
                        if cancel_event.is_set():
                            raise UserCancelled("Cancelled by user.")
                        processed_candidates.add(item.identity)
                        result = await fetch_and_save_candidate(
                            context, item, root, state, log, registry
                        )
                        if result == "downloaded":
                            summary.downloaded += 1
                            summary.files += 1
                        elif result == "skipped":
                            summary.skipped += 1
                            summary.files += 1
                        elif result == "failed":
                            summary.failed += 1
                        else:
                            summary.rejected += 1

            while page_queue and len(seen) < MAX_PAGES:
                if cancel_event.is_set():
                    raise UserCancelled("Cancelled by user.")
                url, folders, expand_links = page_queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                collector.set_context(folders, url)

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except PlaywrightTimeoutError:
                        pass
                    await scroll_lazy_content(page)
                    inferred_folders = await infer_material_folders(page, url)
                    if inferred_folders:
                        folders = inferred_folders
                        collector.set_context(folders, url)
                    await collector.drain()
                    links = await get_dom_links(page)
                except Exception as exc:
                    log(f"[ERROR] {url} -> {exc}")
                    continue

                content_links = [item for item in links if item.get("inContent")]
                if content_links:
                    links = content_links

                log(f"[SCAN {len(seen)}] {url}")
                log(f"    folder: {' / '.join(folders)}")

                current_folder = folder_id(url)
                for item in links:
                    href = canonical_url(urljoin(url, str(item.get("href") or "")))
                    text = str(item.get("text") or "")
                    context_text = str(item.get("context") or "")
                    download_attr = str(item.get("download") or "")
                    if not href:
                        continue

                    linked_material_page = is_material_detail_url(href, host)
                    fileish = looks_like_file_candidate(href, text, download_attr)
                    if fileish and not (crawl_all and linked_material_page):
                        registry.add(
                            Candidate(
                                href,
                                folders,
                                text=best_label(text, context_text),
                                referrer=url,
                                method="dom",
                            )
                        )

                    if not expand_links:
                        continue

                    same_course_page = is_allowed_page_url(href, host, course_id)
                    if not same_course_page and not linked_material_page:
                        continue

                    next_folders = folders
                    linked_folder = folder_id(href)
                    if linked_folder and linked_folder != current_folder:
                        folder_name = best_label(
                            text,
                            context_text,
                            fallback=f"Folder {linked_folder}",
                        )
                        next_folders = (*folders, clean_component(folder_name))

                    if href not in seen and href not in queued:
                        queued.add(href)
                        page_queue.append(
                            (href, tuple(next_folders), same_course_page)
                        )

                log(
                    f"    pages={len(seen)} candidates={len(registry)} "
                    f"queue={len(page_queue)}"
                )
                # Fetch once and save now; Schoology CDN signatures can expire quickly.
                await process_pending()

            await collector.drain()
            await process_pending()
            summary.pages = len(seen)
            summary.candidates = len(processed_candidates)

            log("")
            log("========== COMPLETE ==========")
            log(f"Pages scanned      : {summary.pages}")
            log(f"Candidates checked : {summary.candidates}")
            log(f"Files confirmed    : {summary.files}")
            log(f"Downloaded         : {summary.downloaded}")
            log(f"Skipped            : {summary.skipped}")
            log(f"Failed             : {summary.failed}")
            log(f"Unsupported links  : {summary.rejected}")
            log(f"Log file           : {logger.path}")
            return summary
        finally:
            if context is not None:
                await context.close()


def load_settings() -> dict[str, object]:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def save_settings(data: dict[str, object]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


class App:
    def __init__(self, root: tk.Tk, crawl_all: bool = False):
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.login_continue = threading.Event()
        self.cancel_event = threading.Event()
        self.pending_password = ""
        self.pending_username = ""
        self.pending_host = ""
        self.settings = load_settings()

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("980x790")
        root.minsize(800, 620)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        tk.Label(root, text="Schoology course/materials URL:").pack(
            anchor="w", padx=10, pady=(10, 2)
        )
        self.url = tk.Entry(root)
        self.url.insert(0, str(self.settings.get("url") or START_URL))
        self.url.pack(fill="x", padx=10)

        self.scan_all = tk.BooleanVar(value=crawl_all)
        tk.Checkbutton(
            root,
            text="Scan all linked folders/material pages in this course (--all)",
            variable=self.scan_all,
        ).pack(anchor="w", padx=10, pady=(4, 0))

        folder_row = tk.Frame(root)
        folder_row.pack(fill="x", padx=10, pady=8)
        tk.Label(folder_row, text="Download folder:").pack(side="left")
        self.folder = tk.Entry(folder_row)
        self.folder.insert(
            0,
            str(self.settings.get("output") or (Path.home() / "Schoology-Files")),
        )
        self.folder.pack(side="left", fill="x", expand=True, padx=8)
        tk.Button(folder_row, text="Browse...", command=self.browse).pack(side="right")

        credentials = tk.LabelFrame(
            root,
            text="Optional automatic sign-in (password is stored only in the OS credential vault)",
        )
        credentials.pack(fill="x", padx=10, pady=(0, 8))

        tk.Label(credentials, text="Microsoft/Schoology email:").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 4)
        )
        self.username = tk.Entry(credentials)
        self.username.insert(0, str(self.settings.get("username") or ""))
        self.username.grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 4))

        tk.Label(credentials, text="Password:").grid(
            row=1, column=0, sticky="w", padx=8, pady=4
        )
        self.password = tk.Entry(credentials, show="●")
        self.password.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        self.password.insert(0, "")

        self.remember = tk.BooleanVar(value=bool(self.settings.get("remember", False)))
        self.remember_box = tk.Checkbutton(
            credentials,
            text="Remember password securely and reuse it",
            variable=self.remember,
        )
        self.remember_box.grid(row=2, column=1, sticky="w", padx=8, pady=(2, 8))
        tk.Button(
            credentials,
            text="Forget saved password",
            command=self.forget_password,
        ).grid(row=2, column=2, padx=8, pady=(2, 8))
        credentials.columnconfigure(1, weight=1)

        note = (
            "The reusable browser profile normally keeps you signed in. If the session expires, "
            "automatic sign-in can fill the saved credentials; MFA still requires your approval."
        )
        tk.Label(root, text=note, justify="left", wraplength=930).pack(
            anchor="w", padx=10, pady=(0, 8)
        )

        tk.Label(root, text="Log:").pack(anchor="w", padx=10)
        self.logbox = scrolledtext.ScrolledText(root, height=26, wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=5)

        buttons = tk.Frame(root)
        buttons.pack(fill="x", padx=10, pady=10)
        self.start_button = tk.Button(
            buttons,
            text="OPEN BROWSER / SIGN IN / DOWNLOAD",
            command=self.start,
        )
        self.start_button.pack(side="left")
        self.continue_button = tk.Button(
            buttons,
            text="CONTINUE AFTER LOGIN",
            command=self.continue_login,
            state="disabled",
        )
        self.continue_button.pack(side="left", padx=8)
        self.cancel_button = tk.Button(
            buttons, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="right")

        self.root.after(100, self.poll_events)

    def browse(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.folder.delete(0, "end")
            self.folder.insert(0, selected)

    def append_log(self, message: str) -> None:
        self.logbox.insert("end", message + "\n")
        self.logbox.see("end")

    def forget_password(self) -> None:
        username = self.username.get().strip() or str(self.settings.get("username") or "")
        host = urlparse(self.url.get().strip()).netloc.lower()
        if not username or not host:
            messagebox.showinfo("Saved password", "No saved credential is selected.")
            return
        if not credential_vault_available():
            messagebox.showerror(
                "Missing dependency",
                "No supported operating-system credential vault is available.",
            )
            return
        try:
            vault_delete_password(host, username)
        except CredentialVaultError as exc:
            messagebox.showerror("Credential vault", str(exc))
            return
        self.password.delete(0, "end")
        self.remember.set(False)
        self.settings["remember"] = False
        save_settings(self.settings)
        messagebox.showinfo("Saved password", "The saved password was removed.")

    def resolve_password(self, host: str, username: str) -> str:
        typed = self.password.get()
        if typed:
            return typed
        if not self.remember.get() or not username:
            return ""
        try:
            return vault_get_password(host, username)
        except CredentialVaultError as exc:
            raise RuntimeError(f"Could not read the OS credential vault: {exc}") from exc

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        start_url = self.url.get().strip()
        output = self.folder.get().strip()
        username = self.username.get().strip()
        crawl_all = self.scan_all.get()
        host = urlparse(start_url).netloc.lower()
        typed_password = self.password.get()
        try:
            password = self.resolve_password(host, username)
        except Exception as exc:
            messagebox.showerror("Credentials", str(exc))
            return

        credential_message = ""
        if self.remember.get() and username and typed_password:
            if not credential_vault_available():
                messagebox.showerror(
                    "Credentials",
                    "Secure credential storage is unavailable. Run the installer again.",
                )
                return
            try:
                vault_set_password(host, username, typed_password)
                credential_message = "[LOGIN] Password saved in the OS credential vault."
            except CredentialVaultError as exc:
                messagebox.showerror("Credential vault", str(exc))
                return
        elif self.remember.get() and password:
            credential_message = "[LOGIN] Saved password loaded from the OS credential vault."

        self.settings = {
            "url": start_url,
            "output": output,
            "username": username,
            "remember": self.remember.get(),
        }
        save_settings(self.settings)

        self.pending_password = password
        self.pending_username = username
        self.pending_host = host
        self.login_continue.clear()
        self.cancel_event.clear()
        self.logbox.delete("1.0", "end")
        if credential_message:
            self.append_log(credential_message)
        self.start_button.config(state="disabled")
        self.continue_button.config(state="disabled")
        self.cancel_button.config(state="normal")

        def worker() -> None:
            try:
                summary = asyncio.run(
                    run_downloader(
                        start_url,
                        output,
                        username,
                        password,
                        self.login_continue,
                        self.cancel_event,
                        lambda line: self.events.put(("log", line)),
                        crawl_all=crawl_all,
                    )
                )
                self.events.put(("finished", summary))
            except UserCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("log", traceback.format_exc()))
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=worker, name="schoology-downloader", daemon=True)
        self.worker.start()

    def continue_login(self) -> None:
        self.login_continue.set()
        self.continue_button.config(state="disabled")

    def cancel(self) -> None:
        self.cancel_event.set()
        self.login_continue.set()
        self.cancel_button.config(state="disabled")
        self.append_log("Cancellation requested...")

    def reset_buttons(self) -> None:
        self.start_button.config(state="normal")
        self.continue_button.config(state="disabled")
        self.cancel_button.config(state="disabled")
        self.pending_password = ""

    def poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    line = str(payload)
                    if line == "@@LOGIN_READY@@":
                        kind = "login_ready"
                    else:
                        self.append_log(line)
                        continue

                if kind == "login_ready":
                    self.continue_button.config(state="normal")
                    messagebox.showinfo(
                        "Confirm login",
                        "A reusable browser is open.\n\n"
                        "If automatic sign-in did not finish, complete Microsoft SSO/MFA.\n"
                        "When the requested Schoology course is visible, return here and click "
                        "CONTINUE AFTER LOGIN.",
                    )
                elif kind == "finished":
                    summary = payload
                    if isinstance(summary, RunSummary):
                        if summary.login_confirmed:
                            self.password.delete(0, "end")
                        messagebox.showinfo(
                            "Finished",
                            f"Downloaded: {summary.downloaded}\n"
                            f"Skipped: {summary.skipped}\n"
                            f"Failed: {summary.failed}\n\n"
                            f"Log: {summary.log_path}",
                        )
                    self.reset_buttons()
                elif kind == "cancelled":
                    self.append_log(str(payload))
                    self.reset_buttons()
                elif kind == "error":
                    messagebox.showerror("Downloader error", str(payload))
                    self.reset_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Exit", "A scan is running. Cancel it and close the application?"
            ):
                return
            self.cancel_event.set()
            self.login_continue.set()
        self.pending_password = ""
        self.root.destroy()


def prompt_with_default(label: str, default: str, allow_clear: bool = False) -> str:
    shown = f" [{default}]" if default else ""
    value = input(f"{label}{shown}: ").strip()
    if allow_clear and value == "-":
        return ""
    return value or default


def prompt_yes_no(label: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(label + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def get_saved_password(host: str, username: str) -> str:
    if not host or not username:
        return ""
    try:
        return vault_get_password(host, username)
    except CredentialVaultError as exc:
        print(f"Credential vault unavailable: {exc}")
        return ""


def delete_saved_password(host: str, username: str) -> None:
    if not credential_vault_available():
        print("Secure credential storage is unavailable.")
        return
    try:
        removed = vault_delete_password(host, username)
        print("Saved password removed." if removed else "No saved password was found.")
    except CredentialVaultError as exc:
        print(f"Could not access the credential vault: {exc}")


def run_console(crawl_all: bool = False) -> int:
    """Terminal interface used on macOS when Tkinter is unavailable."""
    settings = load_settings()
    print(f"{APP_NAME} v{APP_VERSION}")
    if tk is None:
        print("Tkinter is unavailable, so the portable terminal interface is being used.")
    print()

    start_url = prompt_with_default(
        "Schoology course/materials URL",
        str(settings.get("url") or START_URL),
    )
    print(
        "Scan scope: all linked course material pages (--all)."
        if crawl_all
        else "Scan scope: only the exact URL provided (default)."
    )
    output = prompt_with_default(
        "Download folder",
        str(settings.get("output") or (Path.home() / "Schoology-Files")),
    )
    username = prompt_with_default(
        "Microsoft/Schoology email (enter - to clear and log in manually)",
        str(settings.get("username") or ""),
        allow_clear=True,
    )
    host = urlparse(start_url).netloc.lower()
    password = ""
    remember = False
    password_is_new = False

    saved = get_saved_password(host, username)
    if saved:
        answer = input("Use saved password? [Y/n/f=forget]: ").strip().lower()
        if answer in {"f", "forget"}:
            delete_saved_password(host, username)
        elif answer not in {"n", "no"}:
            password = saved
            remember = True
            print("Saved password loaded from the operating system credential vault.")
    elif username:
        print("No saved password was found for this account and Schoology host.")

    if username and not password:
        print("Password typing is hidden in Terminal—no letters, dots, or stars will appear.")
        while not password:
            password = getpass.getpass("Type the password, then press Return: ")
            if password:
                password_is_new = True
                print("Password received.")
                break
            if prompt_yes_no(
                "No password was entered. Continue with manual browser sign-in?",
                default=False,
            ):
                break
        if password:
            if not credential_vault_available():
                print("The password will be used once; secure credential storage is unavailable.")
            else:
                remember = prompt_yes_no(
                    "Save this password in the operating system credential vault?",
                    default=True,
                )

    if (
        password_is_new
        and remember
        and username
        and password
        and credential_vault_available()
    ):
        try:
            vault_set_password(host, username, password)
            print("Password saved in the operating system credential vault.")
        except CredentialVaultError as exc:
            print(f"Could not save the password securely: {exc}")
            remember = False

    save_settings(
        {
            "url": start_url,
            "output": output,
            "username": username,
            "remember": remember,
        }
    )

    login_continue = threading.Event()
    cancel_event = threading.Event()

    def emit(message: str) -> None:
        if message == "@@LOGIN_READY@@":
            print()
            print("Complete Schoology/Microsoft sign-in in the opened browser.")
            print("Approve MFA if requested and wait until the course page is visible.")
            input("Then return to this window and press Enter to continue...")
            login_continue.set()
            return
        print(message, flush=True)

    try:
        summary = asyncio.run(
            run_downloader(
                start_url,
                output,
                username,
                password,
                login_continue,
                cancel_event,
                emit,
                crawl_all=crawl_all,
            )
        )
    except KeyboardInterrupt:
        cancel_event.set()
        login_continue.set()
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}")
        return 1

    password = ""
    print(f"Log file: {summary.log_path}")
    return 0


def run_self_test() -> int:
    import tempfile

    def sample_ooxml(main_part: str, main_content_type: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                f'<Types><Override PartName="/{main_part}" '
                f'ContentType="{main_content_type}"/></Types>',
            )
            archive.writestr(main_part, "<root/>")
        return buffer.getvalue()

    sample = (
        "https://files-cdn.schoology.com/abc?content-type=application%2Fpdf&"
        "content-disposition=attachment%3B%2Bfilename%3D%22Proof%2BWorksheet%2B1.pdf%22&"
        "Expires=1788047203&Signature=secret"
    )
    docx_body = sample_ooxml(
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    )
    pptx_body = sample_ooxml(
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    )
    pptx_source = "https://example.test/attachment/1/source/Lesson.pptx"
    encoded_custom = quote(
        '{"downloadLink":"https:\\/\\/example.test\\/attachment\\/1'
        '\\/source\\/Lesson.pptx"}',
        safe="",
    )
    viewer_html = f'<iframe src="/viewer#custom={encoded_custom}"></iframe>'
    assert extract_course_id(START_URL) == "8442701217"
    assert filename_from_response(sample, sample, {}) == "Proof Worksheet 1.pdf"
    assert clean_filename("Lesson 1.2", ".docx") == "Lesson 1.2.docx"
    assert clean_filename("Slides.pdf", ".pptx") == "Slides.pptx"
    assert detect_supported_extension(b"%PDF-1.7\nexample") == ".pdf"
    assert detect_supported_extension(docx_body) == ".docx"
    assert detect_supported_extension(pptx_body) == ".pptx"
    assert not detect_supported_extension(b"not a supported document")
    assert looks_like_file_candidate("https://example.test/Lesson.docx")
    assert looks_like_file_candidate("https://example.test/Slides.pptx")
    assert pptx_source in extract_file_urls_from_text(
        viewer_html, "https://example.test/docviewer"
    )
    assert supported_url_extension(pptx_source) == ".pptx"
    assert clean_component("Current Menu Item\nMaterials Dropdown") == (
        "Current Menu Item_Materials Dropdown"
    )
    assert clean_component("CON") == "_CON"
    assert is_allowed_page_url(
        "https://basised-tx.schoology.com/course/8442701217/materials?f=1",
        "basised-tx.schoology.com",
        "8442701217",
    )
    assert not is_allowed_page_url(
        "https://basised-tx.schoology.com/course/8442701215/materials?f=1",
        "basised-tx.schoology.com",
        "8442701217",
    )
    assert is_material_detail_url(
        "https://basised-tx.schoology.com/course/8442701215/materials/gp/8490805601",
        "basised-tx.schoology.com",
    )
    assert not is_material_detail_url(
        "https://other.example/course/8442701215/materials/gp/8490805601",
        "basised-tx.schoology.com",
    )
    assert "Signature=" not in stable_url(sample)
    assert "secret" not in scrub_log_secrets(f"failed: {sample}")
    assert allowed_autofill_host("login.microsoftonline.com", "basised-tx.schoology.com")
    assert not allowed_autofill_host("example.com", "basised-tx.schoology.com")
    assert not parse_arguments([]).crawl_all
    assert parse_arguments(["--all"]).crawl_all

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state = DownloadState(root)
        body = b"%PDF-1.7\nexample"
        digest = bytes_sha256(body)
        key = source_key(sample, ("Materials", "Unit 1"))
        path, skip, _ = select_destination(
            root, ("Materials", "Unit 1"), "Review.pdf", state, key, digest
        )
        assert not skip
        atomic_write(path, body)
        state.record(key, path, digest, len(body))
        repeated, skip, _ = select_destination(
            root, ("Materials", "Unit 1"), "Review.pdf", state, key, digest
        )
        assert skip and repeated == path

        other = b"%PDF-1.7\ndifferent"
        collision, skip, _ = select_destination(
            root,
            ("Materials", "Unit 1"),
            "Review.pdf",
            state,
            source_key("https://example.test/other", ("Materials", "Unit 1")),
            bytes_sha256(other),
        )
        assert not skip and collision.name == "Review [2].pdf"

        docx_path = root / "Materials" / "Unit 1" / "Notes.docx"
        atomic_write(docx_path, docx_body)
        assert detect_supported_extension(docx_path.read_bytes()) == ".docx"

        mislabeled = root / "Materials" / "Legacy.pdf"
        atomic_write(mislabeled, docx_body)
        repair_key = source_key(
            "https://example.test/attachment/source/Notes.docx",
            ("Materials",),
        )
        state.record(repair_key, mislabeled, bytes_sha256(docx_body), len(docx_body))
        repaired, skip, _ = select_destination(
            root,
            ("Materials",),
            "Recovered Notes.docx",
            state,
            repair_key,
            bytes_sha256(docx_body),
        )
        assert not skip and repaired.suffix == ".docx"

    print("Self-test passed.")
    return 0


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--all",
        dest="crawl_all",
        action="store_true",
        help="scan all linked material pages in the selected course",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="use the terminal interface even when Tkinter is available",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run built-in tests and exit",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_arguments()

    if args.self_test:
        return run_self_test()
    if args.cli or tk is None:
        return run_console(crawl_all=args.crawl_all)
    try:
        root_window = tk.Tk()
    except Exception as exc:
        print(f"Graphical interface unavailable ({exc}); using the terminal interface.")
        return run_console(crawl_all=args.crawl_all)
    App(root_window, crawl_all=args.crawl_all)
    root_window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
