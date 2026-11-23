"""Independent SilverBullet MCP knowledge adapter. No Switchboard Core imports."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import quote
from uuid import uuid4

import httpx


MAX_PAGE_BYTES = 500_000
COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
AGENT_MENTION = re.compile(r"(?<![\w@])@agent\b", re.IGNORECASE)
EXECUTABLE_MARKDOWN = re.compile(r"(?im)^\s*(?:`{3,}|~{3,})\s*(?:space-lua|space-style)\b|\$\{|<\s*script\b")


class KnowledgeError(ValueError):
    """A user-actionable knowledge operation error."""


def page_path(page: str, *, writable: bool = False) -> str:
    if not isinstance(page, str):
        raise KnowledgeError("Page must be a string.")
    page = page.strip()
    if page.endswith(".md"):
        page = page[:-3]
    parts = page.split("/")
    if (not page or len(page) > 240 or any(
        not part or part in {".", ".."} or part.startswith(".") or
        "\\" in part or "\x00" in part or "?" in part or "#" in part
        for part in parts
    )):
        raise KnowledgeError("Invalid page name.")
    if writable and (len(parts) < 2 or parts[0] not in {"Inbox", "Wiki"}):
        raise KnowledgeError("Agent writes are limited to Inbox/ and Wiki/ pages.")
    return "/".join(parts) + ".md"


def _page_name(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()[:-3]


def _pages(root: Path):
    if not root.is_dir():
        raise KnowledgeError("Knowledge space is not mounted. Check SilverBullet setup.")
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not Path(directory, d).is_symlink())
        for filename in sorted(files):
            path = Path(directory, filename)
            if filename.endswith(".md") and not filename.startswith(".") and not path.is_symlink():
                yield path


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_PAGE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _validate_content(content: str) -> None:
    if not content.strip() or len(content.encode("utf-8")) > MAX_PAGE_BYTES:
        raise KnowledgeError("Content must be nonempty and at most 500 KB.")
    if EXECUTABLE_MARKDOWN.search(content):
        raise KnowledgeError("Executable SilverBullet markup is not allowed through MCP writes.")


def search_pages(root: Path, query: str, limit: int = 10) -> dict:
    query = query.strip()
    if not 2 <= len(query) <= 200:
        raise KnowledgeError("Search query must be 2–200 characters.")
    if not 1 <= limit <= 30:
        raise KnowledgeError("Limit must be 1–30.")
    words = [word.casefold() for word in query.split()]
    matches = []
    for path in _pages(root):
        page = _page_name(path, root)
        if page == "CONFIG" or page.startswith("Library/"):
            continue
        content = _read_text(path)
        if content is None:
            continue
        lines = content.splitlines()
        for number, line in enumerate(lines, 1):
            folded = line.casefold()
            if all(word in folded for word in words):
                matches.append({"page": page, "line": number, "excerpt": line.strip()[:350]})
                break
        else:
            if all(word in content.casefold() for word in words):
                matches.append({"page": page, "line": None, "excerpt": "Terms occur in different lines."})
    matches.sort(key=lambda item: (not item["page"].casefold().startswith(query.casefold()), item["page"]))
    return {"results": matches[:limit], "total": len(matches), "truncated": len(matches) > limit}


def _feedback_in_page(page: str, content: str) -> list[dict]:
    results = []
    for match in COMMENT.finditer(content):
        comment = match.group(0)
        if not AGENT_MENTION.search(match.group(1)):
            continue
        preceding = content[:match.start()].rstrip()
        blocks = re.split(r"\n\s*\n", preceding)
        context = blocks[-1].strip()[-600:] if preceding else ""
        results.append({
            "page": page,
            "line": content.count("\n", 0, match.start()) + 1,
            "id": hashlib.sha256(comment.encode("utf-8")).hexdigest()[:16],
            "comment": match.group(1).strip()[:1500],
            "context_before": context,
        })
    return results


def list_feedback(root: Path, limit: int = 20) -> dict:
    if not 1 <= limit <= 100:
        raise KnowledgeError("Limit must be 1–100.")
    results = []
    total = 0
    for path in _pages(root):
        page = _page_name(path, root)
        if page == "CONFIG" or page.startswith("Library/"):
            continue
        content = _read_text(path)
        if content is None:
            continue
        found = _feedback_in_page(page, content)
        total += len(found)
        results.extend(found[:max(0, limit - len(results))])
    return {"results": results, "total": total, "truncated": total > limit}


class KnowledgeClient:
    def __init__(self, base_url: str, token_file: Path, root: Path):
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.root = root

    @classmethod
    def from_environment(cls) -> "KnowledgeClient":
        return cls(
            os.environ.get("KNOWLEDGE_BASE_URL", "http://silverbullet:3000"),
            Path(os.environ.get("KNOWLEDGE_TOKEN_FILE", "/home/ubuntu/silverbullet-api-token")),
            Path(os.environ.get("KNOWLEDGE_SPACE_DIR", "/knowledge")),
        )

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise KnowledgeError("SilverBullet API token is not configured on this host.") from error
        if not token:
            raise KnowledgeError("SilverBullet API token is empty.")
        return token

    def _request(self, method: str, path: str, *, content: str | None = None,
                 etag: str | None = None, create: bool = False) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token()}", "X-Sync-Mode": "true"}
        # SilverBullet 3 uses representation-specific ETags. Ordinary text GET
        # returns sha256-typed (browser cache), but Accept: octet-stream returns
        # sha256, the content revision accepted by If-Match on PUT.
        if method == "GET":
            headers["Accept"] = "application/octet-stream"
        if etag is not None:
            headers["If-Match"] = etag
        if create:
            headers["If-None-Match"] = "*"
        try:
            with httpx.Client(base_url=self.base_url, timeout=15, follow_redirects=False,
                              trust_env=False) as client:
                response = client.request(method, "/.fs/" + quote(path, safe="/"),
                                          headers=headers, content=content.encode("utf-8") if content is not None else None)
        except httpx.RequestError as error:
            raise KnowledgeError("SilverBullet is unreachable from the gateway.") from error
        if response.status_code == 412:
            raise KnowledgeError("Page changed since it was read. Read it again before editing.")
        if response.status_code == 404:
            raise KnowledgeError("Page does not exist.")
        if response.status_code in {401, 403}:
            raise KnowledgeError("SilverBullet API token is invalid or lacks access.")
        if 300 <= response.status_code < 400:
            raise KnowledgeError("SilverBullet redirected the API request. Check its space URL and authentication.")
        if response.status_code >= 400:
            raise KnowledgeError(f"SilverBullet returned HTTP {response.status_code}.")
        return response

    def read_page(self, page: str) -> dict:
        path = page_path(page)
        response = self._request("GET", path)
        if len(response.content) > MAX_PAGE_BYTES:
            raise KnowledgeError("Page is too large for the MCP tool.")
        return {"page": path[:-3], "content": response.text,
                "etag": response.headers.get("etag")}

    def create_note(self, title: str, content: str) -> dict:
        title = title.strip()
        if not title or len(title) > 200 or "\n" in title or "\r" in title:
            raise KnowledgeError("Title must be one line of 1–200 characters.")
        _validate_content(content)
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:50] or "note"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"Inbox/{stamp}-{slug}-{uuid4().hex[:8]}.md"
        body = f"# {title}\n\n{content.rstrip()}\n"
        response = self._request("PUT", path, content=body, create=True)
        return {"page": path[:-3], "etag": response.headers.get("etag")}

    def create_page(self, page: str, content: str) -> dict:
        path = page_path(page, writable=True)
        if not path.startswith("Wiki/"):
            raise KnowledgeError("New wiki pages must be under Wiki/.")
        _validate_content(content)
        response = self._request("PUT", path, content=content, create=True)
        return {"page": path[:-3], "etag": response.headers.get("etag")}

    def update_page(self, page: str, content: str, etag: str) -> dict:
        path = page_path(page, writable=True)
        _validate_content(content)
        if not re.fullmatch(r'"sha256:[0-9a-fA-F]{64}"', etag or ""):
            raise KnowledgeError("A current SilverBullet ETag from read_page is required.")
        response = self._request("PUT", path, content=content, etag=etag)
        return {"page": path[:-3], "etag": response.headers.get("etag")}

    def resolve_feedback(self, page: str, feedback_id: str, etag: str) -> dict:
        path = page_path(page, writable=True)
        if not re.fullmatch(r"[0-9a-f]{16}", feedback_id):
            raise KnowledgeError("Invalid feedback ID.")
        current = self.read_page(page)
        if current["etag"] != etag:
            raise KnowledgeError("Page changed since it was read. Read it again before resolving.")
        content = current["content"]
        matches = [match for match in COMMENT.finditer(content)
                   if AGENT_MENTION.search(match.group(1)) and
                   hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:16] == feedback_id]
        if len(matches) != 1:
            raise KnowledgeError("Feedback is missing or ambiguous. List feedback again.")
        match = matches[0]
        updated = content[:match.start()] + content[match.end():]
        response = self._request("PUT", path, content=updated, etag=etag)
        return {"page": path[:-3], "resolved": feedback_id,
                "etag": response.headers.get("etag")}
