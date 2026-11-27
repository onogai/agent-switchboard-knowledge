"""Bounded structured retrieval from Markdown frontmatter in a local subtree."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import re

import yaml

from .knowledge import (KnowledgeError, MAX_PAGE_BYTES, folder_pages,
                        page_path, _page_name)

MAX_HEADER_BYTES = 16_384


def _date(value) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    raise ValueError("Expected YYYY-MM-DD")


def _json_value(value, depth=0):
    if depth > 12:
        raise ValueError("Metadata is too deeply nested")
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(v, depth + 1) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: _json_value(v, depth + 1) for k, v in value.items()}
    raise ValueError("Unsupported metadata value")


def _size(value) -> int:
    # Conservative ASCII JSON size, including envelope and escape overhead.
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii"))


def _header(path: Path):
    """Read only a bounded YAML header; never parse executable Markdown."""
    with path.open("rb") as handle:
        if handle.readline(MAX_HEADER_BYTES + 1).rstrip(b"\r\n") != b"---":
            return None
        raw = bytearray()
        while len(raw) <= MAX_HEADER_BYTES:
            line = handle.readline(MAX_HEADER_BYTES - len(raw) + 1)
            if not line:
                return None
            if line.rstrip(b"\r\n") == b"---":
                text = raw.decode("utf-8")
                # Avoid alias expansion and recursive YAML structures.
                if any(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(text)):
                    return None
                metadata = yaml.safe_load(text)
                if not isinstance(metadata, dict):
                    return None
                metadata = _json_value(metadata)
                _size(metadata)
                return metadata, handle.tell()
            raw.extend(line)
    return None


def _matches(value, expected: str) -> bool:
    if isinstance(value, list):
        return any(_matches(v, expected) for v in value if not isinstance(v, (dict, list)))
    if isinstance(value, bool):
        return str(value).lower() == expected
    return value is not None and not isinstance(value, dict) and str(value) == expected


def query_pages(root: Path, prefix: str, date_from: str, date_to: str,
                date_field: str = "date", metadata_equals: dict[str, str] | None = None,
                fields: list[str] | None = None, include_content: bool = False,
                limit: int = 30, offset: int = 0, max_bytes: int = 60000) -> dict:
    prefix = page_path(prefix.rstrip("/"))[:-3]
    if prefix == "Library" or prefix.startswith("Library/"):
        raise KnowledgeError("Query a document folder outside Library/.")
    try:
        start, end = _date(date_from), _date(date_to)
    except (ValueError, TypeError) as exc:
        raise KnowledgeError("Dates must be YYYY-MM-DD.") from exc
    if start > end:
        raise KnowledgeError("date_from must not be later than date_to.")
    if not date_field.strip():
        raise KnowledgeError("date_field must not be empty.")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise KnowledgeError("Limit must be 1–100 and offset 0–100000.")
    if not 1000 <= max_bytes <= 150000:
        raise KnowledgeError("max_bytes must be 1000–150000.")
    filters = metadata_equals or {}
    matches = []
    skipped = 0
    for path in folder_pages(root, prefix):
        try:
            header = _header(path)
            if header is None:
                skipped += 1
                continue
            metadata, body_offset = header
            document_date = _date(metadata.get(date_field))
        except (OSError, ValueError, TypeError, OverflowError, RecursionError, yaml.YAMLError):
            skipped += 1
            continue
        if start <= document_date <= end and all(
                _matches(metadata.get(key), expected) for key, expected in filters.items()):
            matches.append((document_date, _page_name(path, root), path, metadata, body_offset))
    matches.sort(key=lambda row: (-row[0].toordinal(), row[1]))
    result = {"items": [], "total": len(matches), "offset": offset,
              "next_offset": None, "truncated": False, "skipped_invalid": skipped}
    for document_date, page, path, metadata, body_offset in matches[offset:offset + limit]:
        item = {"page": page, "date": document_date.isoformat(),
                "metadata": metadata if fields is None else
                {key: metadata[key] for key in fields if key in metadata}}
        if include_content:
            try:
                # Verify the header did not change between selection and reading.
                if _header(path) != (metadata, body_offset):
                    raise KnowledgeError(f"Page changed during query: {page}. Retry the query.")
                with path.open("rb") as handle:
                    handle.seek(body_offset)
                    body = handle.read(MAX_PAGE_BYTES + 1)
                if len(body) > MAX_PAGE_BYTES:
                    raise KnowledgeError(f"Page body too large: {page}.")
                item["content"] = body.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise KnowledgeError(f"Cannot read page body: {page}. Retry the query.") from exc
        candidate = {**result, "items": result["items"] + [item],
                     "next_offset": offset + len(result["items"]) + 1, "truncated": True}
        if _size(candidate) > max_bytes:
            if not result["items"]:
                raise KnowledgeError(f"Page {page} does not fit max_bytes. Select fewer fields, omit content, or raise max_bytes.")
            break
        result["items"].append(item)
    next_offset = offset + len(result["items"])
    result["truncated"] = next_offset < len(matches)
    result["next_offset"] = next_offset if result["truncated"] else None
    return result
