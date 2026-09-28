"""Scanning a website for recipe pages (sitemap and link crawl), without AI."""
import json
import time

import httpx
import pytest

from app import site_scan


def recipe_page(name, minutes="PT45M"):
    data = {"@context": "https://schema.org", "@type": "Recipe", "name": name, "totalTime": minutes,
            "image": f"https://blog.example/img/{name}.jpg"}
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body>…</body></html>'


SITE = {
    "/robots.txt": "User-agent: *\nDisallow: /privat/\nSitemap: https://blog.example/sitemap_index.xml",
    "/sitemap_index.xml": '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                          '<sitemap><loc>https://blog.example/page-sitemap.xml</loc></sitemap>'
                          '<sitemap><loc>https://blog.example/rezept-sitemap.xml</loc></sitemap></sitemapindex>',
    "/rezept-sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                           '<url><loc>https://blog.example/rezept/kuerbissuppe/</loc></url>'
                           '<url><loc>https://blog.example/rezept/apfelkuchen/</loc></url>'
                           '<url><loc>https://blog.example/privat/geheim/</loc></url></urlset>',
    "/page-sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         '<url><loc>https://blog.example/ueber-mich/</loc></url>'
                         '<url><loc>https://other.example/fremd/</loc></url></urlset>',
    "/rezept/kuerbissuppe/": recipe_page("Kürbissuppe"),
    "/rezept/apfelkuchen/": recipe_page("Apfelkuchen", "PT1H10M"),
    "/privat/geheim/": recipe_page("Geheim"),
    "/ueber-mich/": "<html><body>Hallo</body></html>",
    "/kategorie/suppen/": '<html><body><a href="/rezept/kuerbissuppe/">Suppe</a> <a href="/tag/x/">tag</a>'
                          '<a href="https://other.example/x">fremd</a> <a href="/kategorie/suppen/page/2/">2</a></body></html>',
    "/kategorie/suppen/page/2/": '<html><body><a href="/rezept/linsensuppe/">Linsen</a></body></html>',
    "/rezept/linsensuppe/": recipe_page("Linsensuppe"),
}


@pytest.fixture
def web(monkeypatch):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        body = SITE.get(request.url.path)
        if body is None or request.url.host != "blog.example":
            return httpx.Response(404)
        ctype = "text/html" if body.startswith("<html") else "application/xml" if body.startswith("<") else "text/plain"
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    real_client = httpx.Client
    monkeypatch.setattr(site_scan.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(site_scan, "REQUEST_DELAY", 0)
    monkeypatch.setattr(site_scan, "_existing_titles", lambda: ["apfelkuchen"])
    return requested


def run(url, depth=site_scan.DEFAULT_DEPTH):
    scan_id = site_scan.start(url, depth)
    for _ in range(200):
        state = site_scan.get(scan_id)
        if state["status"] != "scanning":
            return state
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def test_homepage_uses_the_sitemap_and_respects_robots(web):
    state = run("https://blog.example/")
    assert state["status"] == "done" and state["source"] == "sitemap"
    found = {f["title"]: f for f in state["found"]}
    assert set(found) == {"Kürbissuppe", "Apfelkuchen"}  # not the page disallowed by robots.txt
    assert found["Apfelkuchen"]["minutes"] == 70 and found["Apfelkuchen"]["in_tandoor"]
    assert not found["Kürbissuppe"]["in_tandoor"]
    assert "/privat/geheim/" not in web and not any("other.example" in p for p in web)


def test_category_page_follows_links_and_pagination(web):
    state = run("https://blog.example/kategorie/suppen/")
    assert state["source"] == "links"
    assert sorted(f["title"] for f in state["found"]) == ["Kürbissuppe", "Linsensuppe"]
    assert "/tag/x/" not in web


def test_invalid_url_is_rejected():
    with pytest.raises(ValueError):
        site_scan.start("blog.example")


def test_depth_limits_how_far_links_are_followed(web):
    # Linsensuppe is only linked from page 2 of the category - two levels away.
    shallow = run("https://blog.example/kategorie/suppen/", depth=1)
    assert [f["title"] for f in shallow["found"]] == ["Kürbissuppe"]
    assert shallow["depth"] == 1 and shallow["max_check"] == site_scan.MAX_CHECK_BY_DEPTH[1]
    deep = run("https://blog.example/kategorie/suppen/", depth=3)
    assert sorted(f["title"] for f in deep["found"]) == ["Kürbissuppe", "Linsensuppe"]


def test_depth_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        site_scan.start("https://blog.example/", depth=9)
