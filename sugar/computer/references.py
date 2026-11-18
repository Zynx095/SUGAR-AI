"""Finding a drawing reference on the web — freely licensed images only.

Searches Wikimedia Commons (public domain and Creative Commons media, with licence data for every file) with a
few phrasings ("cat line art", "cat silhouette", "cat clipart"), keeps files whose licence allows reuse, downloads
small PNG/JPEG renditions only (never other file types), scores how well each would trace into a drawing, and —
when the local vision model is running — checks the best few really show the subject. The chosen file's title,
author, licence and page are reported so the user can see the source.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SIDDrawingHelper/1.0 (+https://meta.wikimedia.org/wiki/User-Agent_policy) httpx"  # generic: no user details
FREE_LICENCES = re.compile(r"^(?:cc0|public domain|pd\b|pd-|cc[ -]by(?:[ -]sa)?(?:[ -]\d|\b))", re.I)
ALLOWED_TYPES = {"image/png", "image/jpeg"}
MAX_BYTES = 3_000_000


@dataclass
class Reference:
    title: str
    page_url: str
    image_url: str
    licence: str
    author: str
    image: Any = None  # PIL image
    score: float = 0.0
    confirmed: bool | None = None  # the vision model agreed it shows the subject

    def attribution(self) -> dict[str, str]:
        return {"title": self.title, "page": self.page_url, "licence": self.licence, "author": self.author}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def search(subject: str, *, client=None, per_query: int = 10) -> list[Reference]:
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    found: dict[str, Reference] = {}
    try:
        for phrasing in (f"{subject} line art", f"{subject} silhouette", f"{subject} clipart", f"{subject} drawing"):
            try:
                response = client.get(COMMONS_API, params={
                    "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                    "gsrsearch": f"{phrasing} filetype:bitmap|drawing", "gsrlimit": per_query,
                    "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 640})
                pages = response.json().get("query", {}).get("pages", {})
            except Exception as exc:
                log.warning("Commons search failed for %r: %s", phrasing, exc)
                continue
            for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
                info = (page.get("imageinfo") or [{}])[0]
                meta = info.get("extmetadata") or {}
                licence = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
                if not FREE_LICENCES.match(licence):
                    continue
                url = info.get("thumburl") or info.get("url")
                if not url or page["title"] in found:
                    continue
                found[page["title"]] = Reference(
                    title=page["title"].removeprefix("File:"), page_url=info.get("descriptionurl", ""),
                    image_url=url, licence=licence,
                    author=_strip_html((meta.get("Artist") or {}).get("value", ""))[:120] or "unknown")
    finally:
        if own:
            client.close()
    return list(found.values())


def download(reference: Reference, *, client=None):
    """The image at ``reference.image_url`` as a PIL image — PNG/JPEG only, size-capped."""
    import httpx
    from PIL import Image

    own = client is None
    client = client or httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        with client.stream("GET", reference.image_url) as response:
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code != 200 or kind not in ALLOWED_TYPES:
                return None
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
    except Exception as exc:
        log.warning("reference download failed (%s): %s", reference.image_url, exc)
        return None
    finally:
        if own:
            client.close()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return None
    if image.mode in ("RGBA", "LA", "P"):  # transparent line art: put it on white
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        white.alpha_composite(rgba)
        image = white
    return image.convert("RGB")


def find_reference(subject: str, *, vision=None, folder: Path | None = None, client=None,
                   candidates: int = 10) -> Reference | None:
    """The most drawable, freely licensed reference for ``subject``, confirmed by the vision model if possible."""
    from sugar.computer.drawing import drawability

    references = search(subject, client=client)[:candidates]
    scored: list[Reference] = []
    for reference in references:
        image = download(reference, client=client)
        if image is None:
            continue
        reference.image = image
        reference.score = drawability(image)
        if reference.score > 0.15:
            scored.append(reference)
    scored.sort(key=lambda r: -r.score)
    if vision is not None and scored:
        try:
            available = vision.available
        except Exception:
            available = False
        if available:
            for reference in scored[:3]:
                try:
                    answer = vision.ask(reference.image, f"Does this image show a {subject}? Answer only yes or no.",
                                        max_tokens=8)
                except Exception:
                    break
                reference.confirmed = answer.strip().lower().startswith("yes")
                if reference.confirmed:
                    scored.remove(reference)
                    scored.insert(0, reference)
                    break
    best = scored[0] if scored else None
    if best is not None and folder is not None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", best.title)[:80]
            best.image.save(folder / f"{safe}.png")
        except OSError:
            pass
    return best
