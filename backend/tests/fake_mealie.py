"""A small in-memory stand-in for the parts of Mealie's REST API the import
uses (foods, units, tags, categories, recipes, image, users/self)."""
from __future__ import annotations

import copy
import json
import re
import uuid

import httpx

LISTS = {"/foods": "food", "/units": "unit", "/organizers/tags": "tag", "/organizers/categories": "category"}


class FakeMealie:
    def __init__(self, token="secret"):
        self.token = token
        self.db = {"food": {}, "unit": {}, "tag": {}, "category": {}}
        self.recipes: dict[str, dict] = {}
        self.images: dict[str, tuple[str, bytes]] = {}
        self.requests: list[tuple[str, str]] = []

    def add(self, kind, name, **extra):
        item = {"id": str(uuid.uuid4()), "name": name, **extra}
        if kind in ("tag", "category"):
            item["slug"] = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self.db[kind][item["id"]] = item
        return item

    def client(self, token=None):
        return httpx.Client(base_url="https://mealie.example/api", transport=httpx.MockTransport(self.handler),
                            headers={"Authorization": f"Bearer {token or self.token}"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.requests.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if path == "/users/self":
            return httpx.Response(200, json={"username": "cook", "groupSlug": "home"})
        if path in LISTS:
            kind = LISTS[path]
            if request.method == "GET":
                return self._page(list(self.db[kind].values()), request)
            if request.method == "POST":
                body = json.loads(request.content)
                if any(i["name"].lower() == body["name"].lower() for i in self.db[kind].values()):
                    return httpx.Response(400, json={"detail": "exists"})
                return httpx.Response(201, json=self.add(kind, body["name"]))
        if path == "/recipes":
            if request.method == "GET":
                return self._page([{"name": r["name"], "slug": r["slug"]} for r in self.recipes.values()], request)
            if request.method == "POST":
                name = json.loads(request.content)["name"]
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                while slug in self.recipes:
                    slug += "-1"
                self.recipes[slug] = {"id": str(uuid.uuid4()), "name": name, "slug": slug,
                                      "recipeIngredient": [], "recipeInstructions": [], "tags": [], "recipeCategory": []}
                return httpx.Response(201, json=slug)
        m = re.match(r"^/recipes/([^/]+)(/image)?$", path)
        if m:
            slug = m.group(1)
            if slug not in self.recipes:
                return httpx.Response(404, json={"detail": "not found"})
            if m.group(2):
                content = request.content.decode("latin-1")
                ext = re.search(r'name="extension"\r\n\r\n([^\r]+)', content)
                self.images[slug] = (ext.group(1) if ext else "", request.content)
                return httpx.Response(200, json={"image": "1"})
            if request.method == "GET":
                return httpx.Response(200, json=copy.deepcopy(self.recipes[slug]))
            if request.method == "PATCH":
                body = json.loads(request.content)
                error = self._check_refs(body)
                if error:
                    return httpx.Response(422, json={"detail": error})
                self.recipes[slug].update(body)
                return httpx.Response(200, json=self.recipes[slug])
            if request.method == "DELETE":
                return httpx.Response(200, json=self.recipes.pop(slug))
        return httpx.Response(404, json={"detail": f"no route {request.method} {path}"})

    def _page(self, items, request):
        per_page = int(request.url.params.get("perPage", 50))
        page = int(request.url.params.get("page", 1))
        chunk = items[(page - 1) * per_page: page * per_page]
        pages = max(1, -(-len(items) // per_page))
        return httpx.Response(200, json={"page": page, "per_page": per_page, "total": len(items),
                                         "total_pages": pages, "items": chunk})

    def _check_refs(self, body):
        for ing in body.get("recipeIngredient", []):
            for kind in ("food", "unit"):
                ref = ing.get(kind)
                if ref and self.db[kind].get(ref["id"], {}).get("name") != ref["name"]:
                    return f"unknown {kind} {ref}"
        refs = {i["referenceId"] for i in body.get("recipeIngredient", [])}
        for step in body.get("recipeInstructions", []):
            for r in step.get("ingredientReferences", []):
                if r["referenceId"] not in refs:
                    return f"unknown ingredient reference {r}"
        for tag in body.get("tags", []):
            if tag["id"] not in self.db["tag"]:
                return f"unknown tag {tag}"
        return None
