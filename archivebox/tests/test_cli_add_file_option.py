#!/usr/bin/env python3
"""
Tests for `archivebox add --file`: bookmark/URL export files are read explicitly
from disk, while positional local paths must still never be expanded.
"""

from pathlib import Path

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import (
    cli_env,
    run_archivebox_cmd,
    run_queued_crawls,
)
from archivebox.tests.test_orm_helpers import use_archivebox_db

from .test_cli_add_1 import pytestmark as pytestmark

FILE_IMPORT_ENV = {
    "USE_COLOR": "False",
    "SHOW_PROGRESS": "False",
    "PLUGINS": "parse_html_urls,parse_jsonl_urls,parse_netscape_urls,parse_txt_urls",
    "SAVE_WGET": "False",
    "SAVE_HEADERS": "False",
    "USE_CHROME": "False",
    "URL_ALLOWLIST": r"example\.com|example\.org|iana\.org|www\.iana\.org",
}

NETSCAPE_EXPORT = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
  <DT><A HREF="https://www.iana.org/domains/reserved" ADD_DATE="1704153600" TAGS="netscape-tag,metadata">IANA Reserved Domains</A>
</DL><p>
"""


def _crawls(archive: Path) -> list[Crawl]:
    with use_archivebox_db(archive):
        return list(Crawl.objects.order_by("created_at"))


def test_add_file_option_imports_netscape_export_with_metadata(initialized_archive):
    export = initialized_archive / "bookmarks_export.html"
    export.write_text(NETSCAPE_EXPORT, encoding="utf-8")
    env = cli_env(**FILE_IMPORT_ENV)

    result = run_archivebox_cmd(
        ["add", "--file", str(export), "--bg", "--depth=0", "--tag=cli-file-import"],
        cwd=initialized_archive,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    crawls = _crawls(initialized_archive)
    assert len(crawls) == 1
    assert crawls[0].urls == NETSCAPE_EXPORT

    run_queued_crawls(initialized_archive, env=env, timeout=240)
    with use_archivebox_db(initialized_archive):
        snapshot = Snapshot.objects.prefetch_related("tags").get(url="https://www.iana.org/domains/reserved")
        tags = set(snapshot.tags.values_list("name", flat=True))
    assert snapshot.title == "IANA Reserved Domains"
    assert {"netscape-tag", "metadata", "cli-file-import"} <= tags
    assert snapshot.bookmarked_at.isoformat() == "2024-01-02T00:00:00+00:00"


def test_add_multiple_file_options_create_one_crawl_with_joined_text(initialized_archive):
    export = initialized_archive / "bookmarks_export.html"
    export.write_text(NETSCAPE_EXPORT, encoding="utf-8")
    urls_txt = initialized_archive / "urls.txt"
    urls_txt.write_text("https://example.org/\n", encoding="utf-8")

    result = run_archivebox_cmd(
        ["add", "-f", str(export), "--file", str(urls_txt), "--bg", "--depth=0"],
        cwd=initialized_archive,
        env=cli_env(**FILE_IMPORT_ENV),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    crawls = _crawls(initialized_archive)
    assert len(crawls) == 1
    assert crawls[0].urls == NETSCAPE_EXPORT + "\n" + "https://example.org/\n"


def test_add_file_option_rejects_bad_inputs(initialized_archive):
    env = cli_env(**FILE_IMPORT_ENV)

    missing = initialized_archive / "nonexistent" / "bookmarks.html"
    result = run_archivebox_cmd(["add", "--file", str(missing), "--bg"], cwd=initialized_archive, env=env)
    assert result.returncode != 0
    assert str(missing) in result.stdout + result.stderr

    binary = initialized_archive / "bookmarks.jsonlz4"
    binary.write_bytes(b"\x89PNG\x00\xff")
    result = run_archivebox_cmd(["add", "--file", str(binary), "--bg"], cwd=initialized_archive, env=env)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not a text" in combined
    assert "jsonlz4" in combined

    html = initialized_archive / "x.html"
    html.write_text(NETSCAPE_EXPORT, encoding="utf-8")
    result = run_archivebox_cmd(
        ["add", "--file", str(html), "https://example.com/", "--bg"],
        cwd=initialized_archive,
        env=env,
    )
    assert result.returncode != 0
    assert "--file" in result.stdout + result.stderr

    assert _crawls(initialized_archive) == []


def test_add_positional_local_path_is_still_rejected(initialized_archive):
    export = initialized_archive / "bookmarks.html"
    export.write_text(NETSCAPE_EXPORT, encoding="utf-8")

    result = run_archivebox_cmd(
        ["add", str(export), "--bg"],
        cwd=initialized_archive,
        env=cli_env(**FILE_IMPORT_ENV),
    )
    assert result.returncode != 0
    assert "No URLs provided" in result.stdout + result.stderr
    assert _crawls(initialized_archive) == []
    with use_archivebox_db(initialized_archive):
        assert not Snapshot.objects.exists()
