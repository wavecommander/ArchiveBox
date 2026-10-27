#!/usr/bin/env python3
"""
`archivebox add < bookmarks.html` must archive the bookmarks, not every URL-shaped
string (favicon ICON_URI, keyword templates, ...) that generic text parsers scrape.
"""

import pytest

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import (
    cli_env,
    run_archivebox_cmd,
    run_queued_crawls,
)
from archivebox.tests.test_orm_helpers import use_archivebox_db

from .test_cli_add_1 import pytestmark as pytestmark

BOOKMARK_URL = "https://www.iana.org/domains/reserved"
PLAIN_BOOKMARK_URL = "https://example.com/"
FAVICON_URL = "https://www.iana.org/_img/bookmark_icon.ico"

# parse_rss_urls is intentionally omitted: its feedparser dependency is irrelevant here.
BOOKMARK_IMPORT_ENV = {
    "USE_COLOR": "False",
    "SHOW_PROGRESS": "False",
    "PLUGINS": "parse_html_urls,parse_jsonl_urls,parse_netscape_urls,parse_txt_urls",
    "SAVE_WGET": "False",
    "SAVE_HEADERS": "False",
    "USE_CHROME": "False",
    # Both hosts are allowed so a missing favicon snapshot proves parser-level
    # suppression rather than allowlist filtering.
    "URL_ALLOWLIST": r"example\.com|example\.org|iana\.org|www\.iana\.org",
}

NETSCAPE_BOOKMARKS = f"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><A HREF="{BOOKMARK_URL}" ADD_DATE="1704153600" ICON_URI="{FAVICON_URL}" ICON="data:image/png;base64,iVBORw0KGgo=" TAGS="netscape-tag,metadata">IANA Reserved Domains</A>
    <DT><A HREF="{PLAIN_BOOKMARK_URL}">Example</A>
</DL><p>
"""

PLAIN_HTML = (
    '<!doctype html><html><body><a href="https://www.iana.org/help/example-domains">x</a> and https://example.org/ in prose</body></html>'
)


def add_via_stdin_and_run(cwd, source_text: str) -> tuple[Crawl, dict[str, Snapshot]]:
    env = cli_env(**BOOKMARK_IMPORT_ENV)
    result = run_archivebox_cmd(
        ["add", "--bg", "--depth=0"],
        cwd=cwd,
        env=env,
        stdin=source_text,
        timeout=360,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    with use_archivebox_db(cwd):
        crawl = Crawl.objects.order_by("-created_at").first()
        assert crawl is not None
        assert crawl.urls == source_text

    run_queued_crawls(cwd, env=env, timeout=240)
    with use_archivebox_db(cwd):
        crawl = Crawl.objects.get(pk=crawl.pk)
        snapshots = {snapshot.url: snapshot for snapshot in crawl.snapshot_set.prefetch_related("tags")}
        for snapshot in snapshots.values():
            snapshot.tag_names = set(snapshot.tags.values_list("name", flat=True))
    return crawl, snapshots


@pytest.mark.timeout(360)
def test_add_netscape_bookmarks_ignores_favicon_urls_found_by_generic_parsers(initialized_archive):
    _crawl, snapshots = add_via_stdin_and_run(initialized_archive, NETSCAPE_BOOKMARKS)

    assert set(snapshots) == {BOOKMARK_URL, PLAIN_BOOKMARK_URL}
    iana = snapshots[BOOKMARK_URL]
    assert iana.title == "IANA Reserved Domains"
    assert iana.tag_names == {"netscape-tag", "metadata"}
    assert iana.bookmarked_at.isoformat() == "2024-01-02T00:00:00+00:00"
    with use_archivebox_db(initialized_archive):
        assert not Snapshot.objects.filter(url=FAVICON_URL).exists()


@pytest.mark.timeout(360)
def test_add_plain_html_still_keeps_urls_found_by_generic_parsers(initialized_archive):
    _crawl, snapshots = add_via_stdin_and_run(initialized_archive, PLAIN_HTML)

    assert set(snapshots) == {"https://www.iana.org/help/example-domains", "https://example.org/"}
