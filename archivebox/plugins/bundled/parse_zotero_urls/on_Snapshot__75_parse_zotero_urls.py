#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
#
# Parse Zotero library exports and extract item URLs with their metadata.
#
# This is a standalone extractor that can run without ArchiveBox.
# It reads an exported Zotero library from SNAP_DIR/staticfile/*.txt or an HTTP URL
# and understands three text export formats produced by Zotero's "Export Library..."
# dialog: Zotero RDF (richest: tags, notes, collections, attachments), Zotero CSV,
# and CSL JSON.
#
# Usage:
#     ./on_Snapshot__75_parse_zotero_urls.py --url=<url>
# Output: Appends discovered URLs to SNAP_DIR/parse_zotero_urls/urls.jsonl
#
import csv
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import rich_click as click
from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    emit_tag_record,
    load_config,
    read_snapshot_source_text,
    write_text_atomic,
)

PLUGIN_NAME = "parse_zotero_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"

MAX_NOTES_CHARS = 20000
GENERIC_ATTACHMENT_TITLES = {"snapshot", "full text pdf", "full text", "pdf", "attachment", "link"}

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "z": "http://www.zotero.org/namespaces/export#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "bib": "http://purl.org/net/biblio#",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "link": "http://purl.org/rss/1.0/modules/link/",
    "prism": "http://prismstandard.org/namespaces/1.2/basic/",
}
ZOTERO_NAMESPACE_MARKER = NS["z"]
# Zotero's CSV export always starts with these columns, in this order.
ZOTERO_CSV_HEADER_PREFIX = ("Key", "Item Type", "Publication Year", "Author", "Title")
DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")


def ns(prefix: str, local: str) -> str:
    return f"{{{NS[prefix]}}}{local}"


def option_enabled(name: str, default: bool) -> bool:
    """Read a boolean plugin option from the resolved config."""
    value = getattr(CONFIG, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def html_to_text(value: str) -> str:
    """Flatten Zotero note HTML into readable plain text."""
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", value or "")
    text = TAG_PATTERN.sub(" ", text)
    text = unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clean_tag(name: str) -> str:
    # The host splits emitted tags on commas, so a tag can never contain one.
    return " ".join(str(name or "").replace(",", " ").split())[:100]


def parse_zotero_datetime(value: str) -> str | None:
    """Normalize Zotero timestamps (UTC "YYYY-MM-DD HH:MM:SS", ISO 8601, or bare dates) to ISO 8601."""
    text = str(value or "").strip()
    if not text:
        return None
    candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return None


def doi_url(doi: str) -> str:
    doi = str(doi or "").strip()
    doi = re.sub(r"(?i)^(doi:|https?://(dx\.)?doi\.org/)", "", doi).strip()
    return f"https://doi.org/{doi}" if doi else ""


def find_doi(*texts: str) -> str:
    for text in texts:
        match = DOI_PATTERN.search(str(text or ""))
        if match:
            return match.group(1).rstrip(".,;)")
    return ""


def detect_format(content: str) -> str | None:
    head = content[:65536]
    if ZOTERO_NAMESPACE_MARKER in head and "<rdf:RDF" in head:
        return "rdf"
    stripped = head.lstrip("﻿ \t\r\n")
    if stripped.startswith("["):
        try:
            data = json.loads(content)
        except ValueError:
            return None
        if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
            if any("URL" in item or "DOI" in item or "title" in item for item in data) and all("type" in item for item in data):
                return "csl_json"
        return None
    first_line = stripped.splitlines()[0] if stripped else ""
    if first_line:
        try:
            header = next(csv.reader(io.StringIO(first_line)))
        except (csv.Error, StopIteration):
            header = []
        if tuple(column.strip() for column in header[: len(ZOTERO_CSV_HEADER_PREFIX)]) == ZOTERO_CSV_HEADER_PREFIX:
            return "csv"
    return None


# ---------------------------------------------------------------------------
# Zotero RDF
# ---------------------------------------------------------------------------


def rdf_about(element: ET.Element) -> str:
    return element.get(ns("rdf", "about"), "")


def rdf_resource(element: ET.Element | None) -> str:
    return element.get(ns("rdf", "resource"), "") if element is not None else ""


def rdf_text(element: ET.Element | None, *path: str) -> str:
    node = element
    for step in path:
        if node is None:
            return ""
        node = node.find(step)
    if node is None:
        return ""
    return " ".join((node.text or "").split())


def rdf_item_uri(element: ET.Element) -> str:
    """Return the item's URL field (dc:identifier/dcterms:URI/rdf:value)."""
    for identifier in element.findall(ns("dc", "identifier")):
        uri = identifier.find(ns("dcterms", "URI"))
        if uri is not None:
            value = rdf_text(uri, ns("rdf", "value"))
            if value:
                return value
    return ""


def rdf_item_doi(element: ET.Element) -> str:
    for identifier in element.findall(ns("dc", "identifier")):
        text = " ".join((identifier.text or "").split())
        if text.upper().startswith("DOI"):
            return find_doi(text)
    return find_doi(rdf_text(element, ns("dc", "description")))


def rdf_item_tags(element: ET.Element) -> list[str]:
    tags = []
    for subject in element.findall(ns("dc", "subject")):
        automatic = subject.find(ns("z", "AutomaticTag"))
        value = rdf_text(automatic, ns("rdf", "value")) if automatic is not None else " ".join((subject.text or "").split())
        if value:
            tags.append(value)
    return tags


def rdf_item_creators(element: ET.Element) -> str:
    names = []
    for role in (ns("bib", "authors"), ns("bib", "editors"), ns("z", "contributors")):
        for person in element.findall(f"{role}/{ns('rdf', 'Seq')}/{ns('rdf', 'li')}/*"):
            surname = rdf_text(person, ns("foaf", "surname"))
            given = rdf_text(person, ns("foaf", "givenName")) or rdf_text(person, ns("foaf", "givenname"))
            full = " ".join(part for part in (given, surname) if part) or rdf_text(person, ns("foaf", "name"))
            if full:
                names.append(full)
        if names:
            break
    return ", ".join(names)


def rdf_item_publication(element: ET.Element, containers: dict[str, ET.Element]) -> str:
    part_of = element.find(ns("dcterms", "isPartOf"))
    if part_of is None:
        return ""
    container = containers.get(rdf_resource(part_of))
    if container is None:
        container = next(iter(part_of), None)
    return rdf_text(container, ns("dc", "title")) if container is not None else ""


def parse_zotero_rdf(content: str) -> list[dict]:
    root = ET.fromstring(content)
    top_level = list(root)
    attachments: dict[str, ET.Element] = {}
    memos: dict[str, ET.Element] = {}
    collections: dict[str, ET.Element] = {}
    by_about: dict[str, ET.Element] = {}
    referenced_containers: set[str] = set()
    for element in top_level:
        about = rdf_about(element)
        if about:
            by_about[about] = element
        if element.tag == ns("z", "Attachment"):
            attachments[about] = element
        elif element.tag == ns("bib", "Memo"):
            memos[about] = element
        elif element.tag == ns("z", "Collection"):
            collections[about] = element
        for part_of in element.iter(ns("dcterms", "isPartOf")):
            if rdf_resource(part_of):
                referenced_containers.add(rdf_resource(part_of))

    # Collection membership: a collection lists its items and sub-collections in dcterms:hasPart.
    collection_parent: dict[str, str] = {}
    item_collections: dict[str, list[str]] = {}
    for about, collection in collections.items():
        for part in collection.findall(ns("dcterms", "hasPart")):
            resource = rdf_resource(part)
            if resource in collections:
                collection_parent[resource] = about
            elif resource:
                item_collections.setdefault(resource, []).append(about)

    def collection_names(about: str) -> list[str]:
        names = []
        seen = set()
        for collection_about in item_collections.get(about, []):
            current = collection_about
            while current and current not in seen:
                seen.add(current)
                title = rdf_text(collections[current], ns("dc", "title"))
                if title:
                    names.append(title)
                current = collection_parent.get(current, "")
        return names

    items = []
    linked_attachments: set[str] = set()
    for element in top_level:
        about = rdf_about(element)
        if element.tag in {ns("z", "Attachment"), ns("bib", "Memo"), ns("z", "Collection")}:
            continue
        if element.tag.startswith(f"{{{NS['foaf']}}}") or about in referenced_containers:
            continue
        item_type = rdf_text(element, ns("z", "itemType")) or element.tag.rsplit("}", 1)[-1]
        if item_type.lower() in {"journal", "series", "note", "collection"}:
            continue
        attachment_records = []
        for link in element.findall(ns("link", "link")):
            attachment = attachments.get(rdf_resource(link))
            if attachment is None:
                continue
            linked_attachments.add(rdf_resource(link))
            attachment_records.append(
                {
                    "title": rdf_text(attachment, ns("dc", "title")),
                    "url": rdf_item_uri(attachment),
                    "path": rdf_resource(attachment.find(ns("rdf", "resource"))),
                    "mimetype": rdf_text(attachment, ns("link", "type")),
                    "bookmarked_at": parse_zotero_datetime(rdf_text(attachment, ns("dcterms", "dateSubmitted"))),
                },
            )
        notes = []
        for reference in element.findall(ns("dcterms", "isReferencedBy")):
            memo = memos.get(rdf_resource(reference))
            if memo is not None:
                note_text = html_to_text(rdf_text(memo, ns("rdf", "value")) or (memo.text or ""))
                if note_text:
                    notes.append(note_text)
        url = rdf_item_uri(element) or (about if about.lower().startswith(("http://", "https://")) else "")
        items.append(
            {
                "zotero_key": about[1:] if about.startswith("#") else "",
                "item_type": item_type,
                "url": url,
                "title": rdf_text(element, ns("dc", "title")),
                "doi": rdf_item_doi(element),
                "date": rdf_text(element, ns("dc", "date")),
                "bookmarked_at": parse_zotero_datetime(rdf_text(element, ns("dcterms", "dateSubmitted"))),
                "tags": rdf_item_tags(element),
                "abstract": " ".join((rdf_text(element, ns("dcterms", "abstract")) or "").split()),
                "creators": rdf_item_creators(element),
                "publication": rdf_item_publication(element, by_about),
                "collections": collection_names(about),
                "notes": notes,
                "attachments": attachment_records,
            },
        )

    # Standalone attachments (e.g. pages saved straight from the browser connector)
    # are items in their own right.
    for about, attachment in attachments.items():
        if about in linked_attachments:
            continue
        url = rdf_item_uri(attachment)
        if not url:
            continue
        items.append(
            {
                "zotero_key": about[1:] if about.startswith("#") else "",
                "item_type": "attachment",
                "url": url,
                "title": rdf_text(attachment, ns("dc", "title")),
                "doi": "",
                "date": "",
                "bookmarked_at": parse_zotero_datetime(rdf_text(attachment, ns("dcterms", "dateSubmitted"))),
                "tags": rdf_item_tags(attachment),
                "abstract": "",
                "creators": "",
                "publication": "",
                "collections": collection_names(about),
                "notes": [],
                "attachments": [],
            },
        )
    return items


# ---------------------------------------------------------------------------
# Zotero CSV
# ---------------------------------------------------------------------------


def split_csv_list(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def parse_zotero_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    items = []
    for row in reader:
        get = lambda column: " ".join(str(row.get(column) or "").split())  # noqa: E731
        attachments = [
            {"title": "", "url": link, "path": "", "mimetype": "", "bookmarked_at": None}
            for link in split_csv_list(row.get("Link Attachments") or "")
            if link.lower().startswith(("http://", "https://"))
        ]
        attachments += [
            {"title": "", "url": "", "path": path, "mimetype": "", "bookmarked_at": None}
            for path in split_csv_list(row.get("File Attachments") or "")
        ]
        notes = html_to_text(str(row.get("Notes") or ""))
        items.append(
            {
                "zotero_key": get("Key"),
                "item_type": get("Item Type"),
                "url": get("Url"),
                "title": get("Title"),
                "doi": find_doi(get("DOI"), get("Extra")),
                "date": get("Date") or get("Publication Year"),
                "bookmarked_at": parse_zotero_datetime(get("Date Added")) or parse_zotero_datetime(get("Access Date")),
                "tags": split_csv_list(row.get("Manual Tags") or "") + split_csv_list(row.get("Automatic Tags") or ""),
                "abstract": get("Abstract Note"),
                "creators": "; ".join(split_csv_list(row.get("Author") or "")),
                "publication": get("Publication Title"),
                "collections": [],
                "notes": [notes] if notes else [],
                "attachments": attachments,
            },
        )
    return items


# ---------------------------------------------------------------------------
# CSL JSON
# ---------------------------------------------------------------------------


def csl_date(value) -> str:
    if not isinstance(value, dict):
        return ""
    parts = value.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        return "-".join(f"{int(part):02d}" if index else str(part) for index, part in enumerate(parts[0]) if part not in (None, ""))
    return str(value.get("raw") or value.get("literal") or "")


def parse_csl_json(content: str) -> list[dict]:
    items = []
    for entry in json.loads(content):
        if not isinstance(entry, dict):
            continue
        creators = []
        for person in entry.get("author") or []:
            if isinstance(person, dict):
                full = " ".join(str(person.get(key) or "") for key in ("given", "family")).strip() or str(person.get("literal") or "")
                if full:
                    creators.append(full)
        items.append(
            {
                "zotero_key": str(entry.get("id") or ""),
                "item_type": str(entry.get("type") or ""),
                "url": str(entry.get("URL") or ""),
                "title": " ".join(str(entry.get("title") or "").split()),
                "doi": find_doi(str(entry.get("DOI") or ""), str(entry.get("note") or "")),
                "date": csl_date(entry.get("issued")),
                "bookmarked_at": parse_zotero_datetime(csl_date(entry.get("accessed"))),
                "tags": [str(keyword) for keyword in (entry.get("keyword") or "").split(",") if str(keyword).strip()]
                if isinstance(entry.get("keyword"), str)
                else [],
                "abstract": " ".join(str(entry.get("abstract") or "").split()),
                "creators": ", ".join(creators),
                "publication": str(entry.get("container-title") or ""),
                "collections": [],
                "notes": [],
                "attachments": [],
            },
        )
    return items


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def build_notes(item: dict) -> str:
    """Render the Zotero metadata that has no dedicated ArchiveBox column as readable notes."""
    lines = []
    citation = " · ".join(part for part in (item["creators"], item["date"], item["publication"]) if part)
    if citation:
        lines.append(citation)
    if item["doi"]:
        lines.append(f"DOI: {doi_url(item['doi'])}")
    provenance = [f"Zotero {item['item_type']}" if item["item_type"] else "Zotero item"]
    if item["zotero_key"]:
        provenance.append(f"key {item['zotero_key']}")
    if item["collections"]:
        provenance.append("collections: " + ", ".join(item["collections"]))
    lines.append(" · ".join(provenance))
    local_files = [attachment["path"] for attachment in item["attachments"] if attachment.get("path")]
    if local_files:
        lines.append("Attached files: " + ", ".join(local_files))
    sections = ["\n".join(lines)]
    if item["abstract"]:
        sections.append(item["abstract"])
    sections.extend(note for note in item["notes"] if note)
    return "\n\n".join(sections)[:MAX_NOTES_CHARS].strip()


def item_tags(item: dict, *, collection_tags: bool, itemtype_tags: bool) -> list[str]:
    tags = [clean_tag(tag) for tag in item["tags"]]
    if collection_tags:
        tags += [clean_tag(name) for name in item["collections"]]
    if itemtype_tags and item["item_type"]:
        tags.append(clean_tag(f"zotero:{item['item_type']}"))
    return list(dict.fromkeys(tag for tag in tags if tag))


def build_records(items: list[dict], *, source_url: str, depth: int) -> list[dict]:
    include_attachments = option_enabled("PARSE_ZOTERO_URLS_INCLUDE_ATTACHMENTS", True)
    doi_fallback = option_enabled("PARSE_ZOTERO_URLS_DOI_FALLBACK", True)
    collection_tags = option_enabled("PARSE_ZOTERO_URLS_COLLECTION_TAGS", True)
    itemtype_tags = option_enabled("PARSE_ZOTERO_URLS_ITEMTYPE_TAGS", False)

    records: dict[str, dict] = {}

    def add_record(url: str, record: dict, tags: list[str]) -> None:
        url = sanitize_extracted_url(url)
        if not url or not url.lower().startswith(("http://", "https://")) or url == source_url:
            return
        existing = records.get(url)
        if existing is None:
            record = {**record, "url": url, "type": "Snapshot", "plugin": PLUGIN_NAME, "depth": depth + 1}
            if tags:
                record["tags"] = ",".join(tags)
            records[url] = {key: value for key, value in record.items() if value not in (None, "", [])}
            return
        # The same URL can be shared by several items: keep the first record and merge tags.
        merged = [tag for tag in existing.get("tags", "").split(",") if tag]
        merged += [tag for tag in tags if tag not in merged]
        if merged:
            existing["tags"] = ",".join(merged)

    for item in items:
        tags = item_tags(item, collection_tags=collection_tags, itemtype_tags=itemtype_tags)
        url = item["url"] or (doi_url(item["doi"]) if doi_fallback and item["doi"] else "")
        base = {
            "title": item["title"][:512],
            "bookmarked_at": item["bookmarked_at"],
            "notes": build_notes(item),
            "zotero_key": item["zotero_key"],
            "item_type": item["item_type"],
            "doi": item["doi"],
            "creators": item["creators"],
            "date": item["date"],
            "publication": item["publication"],
            "collections": item["collections"],
            "attachments": item["attachments"],
        }
        if url:
            add_record(url, base, tags)
        if not include_attachments:
            continue
        for attachment in item["attachments"]:
            attachment_url = attachment.get("url") or ""
            if not attachment_url or sanitize_extracted_url(attachment_url) == sanitize_extracted_url(url):
                continue
            attachment_title = attachment.get("title") or ""
            title = item["title"]
            if attachment_title and attachment_title.lower() not in GENERIC_ATTACHMENT_TITLES:
                title = f"{item['title']} ({attachment_title})" if item["title"] else attachment_title
            add_record(
                attachment_url,
                {
                    **base,
                    "title": title[:512],
                    "bookmarked_at": attachment.get("bookmarked_at") or item["bookmarked_at"],
                    "notes": ("\n\n".join(part for part in (f"Zotero attachment of {url}" if url else "", base["notes"]) if part))[
                        :MAX_NOTES_CHARS
                    ],
                    "attachments": [],
                },
                tags,
            )
    return list(records.values())


def emit_result(status: str, output_str: str) -> None:
    """Emit final ArchiveResult JSONL plus a short stderr summary."""
    emit_archive_result_record(status, output_str)
    if output_str:
        click.echo(output_str, err=True)


def persist_records(records: list[dict]) -> tuple[str, str]:
    """Write extracted URLs when present, otherwise clear stale output after success."""
    if records:
        write_text_atomic(
            URLS_FILE,
            "\n".join(json.dumps(record) for record in records) + "\n",
        )
        return "succeeded", f"{len(records)} URLs parsed"

    URLS_FILE.unlink(missing_ok=True)
    return "noresults", NORESULTS_OUTPUT


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="Zotero export file URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse a Zotero library export and extract item URLs."""
    print("parsing 1 files for urls...")
    try:
        content = read_snapshot_source_text(
            url,
            snap_dir=SNAP_DIR,
            timeout=CONFIG.TIMEOUT,
            user_agent=CONFIG.USER_AGENT,
        )
    except Exception as e:
        if url.startswith(("http://", "https://")):
            # Ordinary webpages and transient fetch errors are not a broken parser.
            status, output_str = persist_records([])
            print(output_str)
            emit_result(status, output_str)
            sys.exit(0)
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    export_format = detect_format(content)
    if export_format is None:
        status, output_str = persist_records([])
        print(output_str)
        emit_result(status, output_str)
        sys.exit(0)

    try:
        if export_format == "rdf":
            items = parse_zotero_rdf(content)
        elif export_format == "csv":
            items = parse_zotero_csv(content)
        else:
            items = parse_csl_json(content)
    except (ET.ParseError, ValueError, csv.Error) as e:
        emit_result("failed", f"Failed to parse Zotero {export_format} export: {e}")
        sys.exit(1)

    records = build_records(items, source_url=url, depth=depth)

    all_tags = sorted({tag for record in records for tag in record.get("tags", "").split(",") if tag})
    for tag_name in all_tags:
        emit_tag_record(tag_name)
    for record in records:
        emit_snapshot_record(record)

    status, output_str = persist_records(records)
    print(f"{export_format}: {len(items)} items, {output_str}")
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
