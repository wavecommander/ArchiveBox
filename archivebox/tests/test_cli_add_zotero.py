"""Import Zotero library exports (RDF, CSV, CSL JSON) with their metadata via the CLI and the web Add page."""

import json
import re
from pathlib import Path

import pytest
import requests

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import (
    cli_env,
    create_admin_and_token,
    get_free_port,
    get_http_response,
    init_archive,
    run_archivebox_cmd,
    run_queued_crawls,
    start_archivebox_server,
    stop_server,
)
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "zotero"
ARTICLE_URL = "https://example.com/journal/reserved-domains"
ARTICLE_PDF_URL = "https://example.com/journal/reserved-domains.pdf"
WEBPAGE_URL = "https://www.iana.org/help/example-domains"
DOI_URL = "https://doi.org/10.1000/doi-only.2024"
# Namespace/schema URIs inside the RDF must never become snapshots.
JUNK_HOSTS = ("purl.org", "www.w3.org", "www.zotero.org", "xmlns.com", "prismstandard.org")

ZOTERO_ENV = {
    "USE_COLOR": "False",
    "SHOW_PROGRESS": "False",
    "PLUGINS": "parse_zotero_urls",
    "SAVE_WGET": "False",
    "SAVE_HEADERS": "False",
    "USE_CHROME": "False",
}


def snapshots_by_url(cwd: Path) -> dict[str, Snapshot]:
    with use_archivebox_db(cwd):
        snapshots = list(Snapshot.objects.prefetch_related("tags").all())
        for snapshot in snapshots:
            snapshot.tag_names = set(snapshot.tags.values_list("name", flat=True))
    return {snapshot.url: snapshot for snapshot in snapshots}


def assert_no_junk_snapshots(snapshots: dict[str, Snapshot]) -> None:
    junk = sorted(url for url in snapshots if any(host in url for host in JUNK_HOSTS))
    assert not junk, junk


def add_zotero_export(cwd: Path, path: Path, env: dict[str, str], *, tag: str) -> Crawl:
    source_text = path.read_text(encoding="utf-8")
    result = run_archivebox_cmd(
        ["add", "--bg", "--depth=0", "--parser=zotero", f"--tag={tag}"],
        cwd=cwd,
        env=env,
        stdin=source_text,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    with use_archivebox_db(cwd):
        crawl = Crawl.objects.order_by("-created_at").first()
    assert crawl is not None
    assert crawl.urls == source_text
    assert crawl.config["PARSER"] == "zotero"
    return crawl


@pytest.mark.timeout(360)
def test_add_parser_zotero_rdf_imports_items_attachments_and_metadata(tmp_path):
    """`archivebox add --parser=zotero < library.rdf` imports item/attachment/DOI URLs with Zotero metadata and no namespace junk."""
    init_archive(tmp_path)
    env = cli_env(**ZOTERO_ENV)
    crawl = add_zotero_export(tmp_path, FIXTURES_DIR / "library.rdf", env, tag="zotero-rdf")

    run_queued_crawls(tmp_path, env=env, timeout=240)

    snapshots = snapshots_by_url(tmp_path)
    assert_no_junk_snapshots(snapshots)
    # The book without URL/DOI is skipped; the "Snapshot" attachments share their parent's URL and are deduplicated.
    assert set(snapshots) == {ARTICLE_URL, ARTICLE_PDF_URL, WEBPAGE_URL, DOI_URL}
    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.get(id=crawl.id)
        assert crawl.snapshot_set.count() == 4
        assert crawl.status in {Crawl.StatusChoices.STARTED, Crawl.StatusChoices.SEALED}

    article = snapshots[ARTICLE_URL]
    assert article.title == "Reserved Domains in Practice"
    assert article.bookmarked_at.isoformat() == "2024-01-05T10:11:12+00:00"
    # Manual tag, automatic tag (comma stripped), both nested collection names, and the crawl tag.
    assert article.tag_names == {"reserved domains", "Internet standards", "Domains", "Research", "zotero-rdf"}
    assert "Jane Doe, Richard Roe · 2023-05-01 · Journal of Examples" in article.notes
    assert "DOI: https://doi.org/10.1000/example.2023.001" in article.notes
    assert "A study of example.com and friends." in article.notes
    assert "Read this before the workshop." in article.notes
    assert "<" not in article.notes
    assert "files/11/reserved-domains.pdf" in article.notes

    pdf = snapshots[ARTICLE_PDF_URL]
    assert pdf.title == "Reserved Domains in Practice"
    assert pdf.bookmarked_at.isoformat() == "2024-01-05T10:11:30+00:00"
    assert pdf.tag_names == article.tag_names
    assert pdf.notes.startswith(f"Zotero attachment of {ARTICLE_URL}")

    webpage = snapshots[WEBPAGE_URL]
    assert webpage.title == "IANA Example Domains"
    assert webpage.bookmarked_at.isoformat() == "2024-02-06T08:00:00+00:00"
    assert webpage.tag_names == {"iana", "Domains", "Research", "zotero-rdf"}
    assert webpage.notes.startswith("Zotero webpage · collections: Domains, Research")

    doi_only = snapshots[DOI_URL]
    assert doi_only.title == "DOI Only Article"
    assert doi_only.bookmarked_at.isoformat() == "2024-04-08T12:00:00+00:00"
    assert doi_only.tag_names == {"Research", "zotero-rdf"}
    assert "Zotero journalArticle · key item_41 · collections: Research" in doi_only.notes

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.get(id=crawl.id)
        input_dir = crawl.output_dir / "input"
    assert (input_dir / "staticfile" / "stdin.txt").read_text(encoding="utf-8") == crawl.urls
    records = [json.loads(line) for line in (input_dir / "parse_zotero_urls" / "urls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["url"] for record in records] == [ARTICLE_URL, ARTICLE_PDF_URL, WEBPAGE_URL, DOI_URL]
    assert all(record["plugin"] == "parse_zotero_urls" for record in records)
    article_record = records[0]
    assert article_record["item_type"] == "journalArticle"
    assert article_record["doi"] == "10.1000/example.2023.001"
    assert article_record["collections"] == ["Domains", "Research"]
    assert [attachment["url"] for attachment in article_record["attachments"]] == [ARTICLE_PDF_URL, ARTICLE_URL]
    assert records[3]["zotero_key"] == "item_41"


@pytest.mark.timeout(360)
def test_add_parser_zotero_csv_and_csl_json_exports(tmp_path):
    """Zotero CSV and CSL JSON exports import the same items as the RDF export, with the metadata each format carries."""
    csv_dir = tmp_path / "csv"
    json_dir = tmp_path / "csl_json"
    for data_dir in (csv_dir, json_dir):
        data_dir.mkdir()
        init_archive(data_dir)
    env = cli_env(**ZOTERO_ENV)

    add_zotero_export(csv_dir, FIXTURES_DIR / "library.csv", env, tag="zotero-csv")
    add_zotero_export(json_dir, FIXTURES_DIR / "library.json", env, tag="zotero-json")
    run_queued_crawls(csv_dir, env=env, timeout=240)
    run_queued_crawls(json_dir, env=env, timeout=240)

    csv_snapshots = snapshots_by_url(csv_dir)
    assert_no_junk_snapshots(csv_snapshots)
    assert set(csv_snapshots) == {ARTICLE_URL, ARTICLE_PDF_URL, WEBPAGE_URL, DOI_URL}
    csv_article = csv_snapshots[ARTICLE_URL]
    assert csv_article.title == "Reserved Domains in Practice"
    assert csv_article.bookmarked_at.isoformat() == "2024-01-05T10:11:12+00:00"
    assert csv_article.tag_names == {"reserved domains", "Internet standards", "zotero-csv"}
    assert "Doe, Jane; Roe, Richard · 2023-05-01 · Journal of Examples" in csv_article.notes
    assert "Zotero journalArticle · key ABCD1234" in csv_article.notes
    assert "A study of example.com and friends." in csv_article.notes
    assert "Read this before the workshop." in csv_article.notes
    assert "/home/user/Zotero/storage/XYZ/reserved-domains.pdf" in csv_article.notes
    assert csv_snapshots[ARTICLE_PDF_URL].tag_names == csv_article.tag_names
    assert csv_snapshots[WEBPAGE_URL].tag_names == {"iana", "zotero-csv"}
    assert csv_snapshots[DOI_URL].title == "DOI Only Article"

    json_snapshots = snapshots_by_url(json_dir)
    assert_no_junk_snapshots(json_snapshots)
    # CSL JSON carries no attachments or tags; "accessed" stands in for the date added.
    assert set(json_snapshots) == {ARTICLE_URL, WEBPAGE_URL, DOI_URL}
    json_article = json_snapshots[ARTICLE_URL]
    assert json_article.title == "Reserved Domains in Practice"
    assert json_article.bookmarked_at.date().isoformat() == "2024-01-05"
    assert json_article.tag_names == {"zotero-json"}
    assert "Jane Doe, Richard Roe · 2023-05-01 · Journal of Examples" in json_article.notes
    assert "DOI: https://doi.org/10.1000/example.2023.001" in json_article.notes
    assert "Zotero article-journal · key doe2023reserved" in json_article.notes
    assert json_snapshots[WEBPAGE_URL].bookmarked_at.date().isoformat() == "2024-02-06"
    assert json_snapshots[DOI_URL].title == "DOI Only Article"


@pytest.mark.timeout(360)
def test_add_view_input_format_zotero_over_real_server(tmp_path):
    """The /add/ page offers an Input format select; choosing Zotero runs only that parser on the pasted export."""
    init_archive(tmp_path)
    port = get_free_port()
    # The shared server env allowlists example.com only; the junk-URL check needs every host admitted.
    env = cli_env(port=port, server=True, **ZOTERO_ENV, URL_ALLOWLIST="")
    create_admin_and_token(tmp_path)
    source_text = (FIXTURES_DIR / "library.rdf").read_text(encoding="utf-8")

    try:
        start_archivebox_server(tmp_path, env=env, port=port)
        host = f"admin.archivebox.localhost:{port}"
        get_http_response(port, host=host, path="/admin/login/")
        session = requests.Session()
        login_page = session.get(f"http://127.0.0.1:{port}/admin/login/", headers={"Host": host}, timeout=10)
        assert login_page.status_code == 200
        csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
        assert csrf_match, login_page.text[:500]
        login_response = session.post(
            f"http://127.0.0.1:{port}/admin/login/",
            headers={"Host": host, "Referer": f"http://{host}/admin/login/"},
            data={
                "username": "apitestadmin",
                "password": "testpass123",
                "csrfmiddlewaretoken": csrf_match.group(1),
                "next": "/add/",
            },
            timeout=10,
            allow_redirects=False,
        )
        assert login_response.status_code in (302, 303), login_response.text[:500]

        add_page = session.get(f"http://127.0.0.1:{port}/add/", headers={"Host": host}, timeout=10)
        assert add_page.status_code == 200
        assert 'name="parser"' in add_page.text
        assert '<option value="auto" selected>' in add_page.text
        assert '<option value="zotero">Zotero only</option>' in add_page.text
        assert '<option value="netscape">Netscape only</option>' in add_page.text
        csrf_token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', add_page.text).group(1)

        response = session.post(
            f"http://127.0.0.1:{port}/add/",
            headers={"Host": host, "Referer": f"http://{host}/add/"},
            data={
                "url": source_text,
                "parser": "zotero",
                "depth": "0",
                "max_urls": "0",
                "crawl_max_size": "0",
                "snapshot_max_size": "0",
                "tag": "zotero-web",
                "url_filters_allowlist": "",
                "url_filters_denylist": "",
                "schedule": "",
                "notes": "zotero web import",
                "persona": "Default",
                "permissions": "public",
                "start_paused": "",
                "config": "{}",
                "csrfmiddlewaretoken": csrf_token,
            },
            timeout=10,
            allow_redirects=False,
        )
        assert response.status_code in (302, 303), response.text[:1000]
        with use_archivebox_db(tmp_path):
            crawl = Crawl.objects.order_by("-created_at").first()
            assert crawl is not None
            assert crawl.urls == source_text
            assert crawl.config["PARSER"] == "zotero"
            assert crawl.tags_str == "zotero-web"
            crawl_id = crawl.id

        # An unknown format name is rejected by the form rather than silently ignored.
        rejected = session.post(
            f"http://127.0.0.1:{port}/add/",
            headers={"Host": host, "Referer": f"http://{host}/add/"},
            data={
                "url": source_text,
                "parser": "not-a-parser",
                "depth": "0",
                "max_urls": "0",
                "crawl_max_size": "0",
                "snapshot_max_size": "0",
                "tag": "",
                "url_filters_allowlist": "",
                "url_filters_denylist": "",
                "schedule": "",
                "notes": "",
                "persona": "Default",
                "permissions": "public",
                "start_paused": "",
                "config": "{}",
                "csrfmiddlewaretoken": csrf_token,
            },
            timeout=10,
            allow_redirects=False,
        )
        assert rejected.status_code == 200
        assert "not-a-parser is not one of the available choices" in rejected.text
        with use_archivebox_db(tmp_path):
            assert Crawl.objects.count() == 1
    finally:
        stop_server(tmp_path)

    run_result = run_archivebox_cmd(["run", f"--crawl-id={crawl_id}"], cwd=tmp_path, timeout=240, env=env)
    assert run_result.returncode == 0, run_result.stderr or run_result.stdout

    snapshots = snapshots_by_url(tmp_path)
    assert_no_junk_snapshots(snapshots)
    assert set(snapshots) == {ARTICLE_URL, ARTICLE_PDF_URL, WEBPAGE_URL, DOI_URL}
    assert snapshots[ARTICLE_URL].tag_names == {"reserved domains", "Internet standards", "Domains", "Research", "zotero-web"}
    assert "A study of example.com and friends." in snapshots[ARTICLE_URL].notes
