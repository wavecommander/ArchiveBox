"""Tests for persisting parser-provided `notes` on discovered snapshots."""

import pytest

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl

pytestmark = pytest.mark.django_db(transaction=True)


def _crawl_with_parent(admin_user):
    crawl = Crawl.objects.create(urls="https://example.com/root", max_depth=1, created_by=admin_user)
    parent = crawl.create_snapshots_from_urls()[0]
    return crawl, parent


def test_discovered_snapshot_persists_stripped_notes_with_other_metadata(admin_user):
    crawl, parent = _crawl_with_parent(admin_user)

    created = crawl.create_discovered_snapshots(
        parent,
        [
            {
                "url": "https://example.com/with-notes",
                "title": "With Notes",
                "tags": "alpha,beta",
                "bookmarked_at": "2024-03-04T05:06:07+00:00",
                "notes": "  A bookmark description.\nSecond line.  \n",
            },
        ],
        depth=1,
    )

    assert [snapshot.url for snapshot in created] == ["https://example.com/with-notes"]
    snapshot = Snapshot.objects.get(url="https://example.com/with-notes")
    assert snapshot.notes == "A bookmark description.\nSecond line."
    assert snapshot.title == "With Notes"
    assert sorted(snapshot.tags.values_list("name", flat=True)) == ["alpha", "beta"]
    assert snapshot.bookmarked_at.isoformat() == "2024-03-04T05:06:07+00:00"


def test_discovered_snapshot_without_notes_has_empty_notes(admin_user):
    crawl, parent = _crawl_with_parent(admin_user)

    crawl.create_discovered_snapshots(
        parent,
        [
            {"url": "https://example.com/no-notes", "title": "No Notes"},
            {"url": "https://example.com/null-notes", "notes": None},
        ],
        depth=1,
    )

    assert Snapshot.objects.get(url="https://example.com/no-notes").notes == ""
    assert Snapshot.objects.get(url="https://example.com/null-notes").notes == ""


def test_record_with_notes_wins_same_url_dedupe(admin_user):
    crawl, parent = _crawl_with_parent(admin_user)

    crawl.create_discovered_snapshots(
        parent,
        [
            {"url": "https://example.com/dupe", "title": "Plain Title"},
            {"url": "https://example.com/dupe", "title": "Notes Title", "notes": "Only this one has notes."},
        ],
        depth=1,
    )

    snapshots = Snapshot.objects.filter(url="https://example.com/dupe")
    assert snapshots.count() == 1
    snapshot = snapshots.get()
    assert snapshot.notes == "Only this one has notes."
    assert snapshot.title == "Notes Title"


def test_existing_crawl_snapshot_fills_empty_notes_but_never_overwrites(admin_user):
    crawl, parent = _crawl_with_parent(admin_user)
    url = "https://example.com/existing-notes"
    crawl.create_discovered_snapshots(parent, [{"url": url, "title": "Existing"}], depth=1)
    assert Snapshot.objects.get(url=url).notes == ""

    crawl.create_discovered_snapshots(parent, [{"url": url, "notes": "  First notes.  "}], depth=1)
    assert Snapshot.objects.get(url=url).notes == "First notes."

    crawl.create_discovered_snapshots(parent, [{"url": url, "notes": "Different notes."}], depth=1)
    assert Snapshot.objects.get(url=url).notes == "First notes."

    crawl.create_discovered_snapshots(parent, [{"url": url, "notes": "   "}], depth=1)
    assert Snapshot.objects.get(url=url).notes == "First notes."
    assert Snapshot.objects.filter(url=url).count() == 1
