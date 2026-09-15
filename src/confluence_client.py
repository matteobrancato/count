"""Read-only Confluence Cloud client — pages as context for AI Test Design.

Same Atlassian account and token as the Jira client (one API token covers the
whole site), so no new credential is needed.  Never writes.

Secrets (Streamlit Cloud):
    CONFLUENCE_URL     e.g. "https://elab-aswatson.atlassian.net/wiki"
                       optional — derived from JIRA_URL's host when absent
    ATLASSIAN_USER     the account e-mail           (shared with Jira)
    ATLASSIAN_API_KEY  the API token                (shared with Jira)
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def _conf() -> tuple[str, str, str] | None:
    """(wiki_base, user, token), wiki_base always ending in "/wiki"."""
    try:
        user  = str(st.secrets["ATLASSIAN_USER"])
        token = str(st.secrets["ATLASSIAN_API_KEY"])
    except Exception:                                                   # noqa: BLE001
        return None
    base = ""
    try:
        base = str(st.secrets.get("CONFLUENCE_URL") or "")
    except Exception:                                                   # noqa: BLE001
        base = ""
    if not base:
        try:
            host = urlparse(str(st.secrets["JIRA_URL"]))
            base = f"{host.scheme}://{host.netloc}"
        except Exception:                                               # noqa: BLE001
            return None
    base = base.rstrip("/")
    if not base.endswith("/wiki"):
        base = base + "/wiki"
    if not (user and token):
        return None
    return base, user, token


def available() -> bool:
    return _conf() is not None


_PAGE_ID_RES = (
    re.compile(r"/pages/(\d+)"),
    re.compile(r"[?&]pageId=(\d+)"),
)
_TINY_RE = re.compile(r"/wiki/x/([A-Za-z0-9_\-]+)")


def extract_page_refs(text: str) -> list[str]:
    """Page references found in free text, de-duplicated, in written order.

    Accepts full page URLs (`…/pages/123456/Title`), old `?pageId=123456`
    links, bare numeric ids, and short links (`…/wiki/x/AbCd`) — the last
    returned as `x:AbCd` and resolved when fetched.
    """
    seen: dict[str, None] = {}
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        ref = None
        for pattern in _PAGE_ID_RES:
            if m := pattern.search(token):
                ref = m.group(1)
                break
        if ref is None and (m := _TINY_RE.search(token)):
            ref = f"x:{m.group(1)}"
        if ref is None and token.isdigit():
            ref = token
        if ref:
            seen.setdefault(ref, None)
    return list(seen)


class _StorageText(HTMLParser):
    """Confluence storage format (XHTML + `ac:` macros) → readable text."""

    _BLOCK = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "br",
                        "tr", "pre", "blockquote", "table", "ul", "ol"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._cells_in_row = 0

    def handle_starttag(self, tag, attrs):
        if tag == "ac:parameter":          # macro settings, not page content
            self._skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "tr":
            self.out.append("\n")
            self._cells_in_row = 0
        elif tag in ("td", "th"):
            if self._cells_in_row:
                self.out.append(" | ")
            self._cells_in_row += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "ac:parameter":
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def unknown_decl(self, data):          # <![CDATA[ … ]]> in code macros
        if data.startswith("CDATA[") and not self._skip:
            self.out.append(data[len("CDATA["):])


def storage_to_text(html: str) -> str:
    parser = _StorageText()
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_tiny(base: str, code: str, auth: HTTPBasicAuth) -> str | None:
    """A `/wiki/x/CODE` short link → its page id, by following the redirect."""
    try:
        resp = requests.get(f"{base}/x/{code}", auth=auth, timeout=_TIMEOUT,
                            allow_redirects=True)
        for pattern in _PAGE_ID_RES:
            if m := pattern.search(resp.url):
                return m.group(1)
    except Exception:
        logger.exception("Confluence short link %s failed", code)
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_page(ref: str) -> dict:
    """One page's title and text.  Always a dict; `error` set instead of raising."""
    out = {"ref": ref, "id": "", "title": "", "text": "", "error": None}
    conf = _conf()
    if not conf:
        out["error"] = "Confluence is not configured"
        return out
    base, user, token = conf
    auth = HTTPBasicAuth(user, token)

    page_id = ref
    if ref.startswith("x:"):
        page_id = _resolve_tiny(base, ref[2:], auth) or ""
        if not page_id:
            out["error"] = "short link could not be resolved"
            return out
    out["id"] = page_id

    try:
        # v2 first; v1 as a fallback for sites or permissions where v2 is off.
        resp = requests.get(f"{base}/api/v2/pages/{page_id}",
                            params={"body-format": "storage"},
                            auth=auth, timeout=_TIMEOUT)
        if resp.ok:
            data = resp.json()
            out["title"] = data.get("title") or ""
            html = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        else:
            resp = requests.get(f"{base}/rest/api/content/{page_id}",
                                params={"expand": "body.storage"},
                                auth=auth, timeout=_TIMEOUT)
            if resp.status_code in (401, 403):
                out["error"] = "no permission to read it"
                return out
            if resp.status_code == 404:
                out["error"] = "not found (or no permission)"
                return out
            if not resp.ok:
                out["error"] = f"Confluence answered {resp.status_code}"
                return out
            data = resp.json()
            out["title"] = data.get("title") or ""
            html = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
    except Exception as exc:                                            # noqa: BLE001
        out["error"] = f"could not reach Confluence ({type(exc).__name__})"
        return out

    out["text"] = storage_to_text(html)
    if not out["text"]:
        out["error"] = "the page is empty"
    return out
