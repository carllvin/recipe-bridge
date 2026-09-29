"""The choice of existing cookbooks in the review."""
from fastapi.testclient import TestClient

from app import main, tandoor_client
from test_mealie import mealie  # noqa: F401 - fixture


def test_tandoor_cookbooks(tandoor):
    tandoor.add("recipe-book", {"name": "Omas Kochbuch"})
    tandoor.add("recipe-book", {"name": "backen"})
    assert TestClient(main.app).get("/api/cookbooks").json() == {"names": ["backen", "Omas Kochbuch"]}


def test_tandoor_with_the_older_endpoint(tandoor, monkeypatch):
    real = tandoor.handler

    def handler(request):  # a Tandoor that only knows /cookbook/
        if "/recipe-book/" in request.url.path:
            import httpx
            return httpx.Response(404, json={})
        return real(request)
    monkeypatch.setattr(tandoor, "handler", handler)
    tandoor.add("cookbook", {"name": "Alt"})
    with tandoor_client.get_client() as client:
        assert tandoor_client.list_cookbooks(client) == ["Alt"]


def test_mealie_cookbooks_are_categories(mealie):  # noqa: F811
    mealie.add("category", "Sommer")
    mealie.add("category", "Backen")
    assert TestClient(main.app).get("/api/cookbooks").json() == {"names": ["Backen", "Sommer"]}


def test_unreachable_gives_an_empty_list(monkeypatch):
    def broken():
        raise tandoor_client.TandoorError("down")
    monkeypatch.setattr(tandoor_client, "get_client", broken)
    data = TestClient(main.app).get("/api/cookbooks").json()
    assert data["names"] == [] and "down" in data["error"]
