"""A small in-memory stand-in for the parts of Mealie's REST API the import
uses (foods, units, tags, categories, recipes, image, users/self) and the
planning (meal plans, shopping lists, ratings, last made)."""
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
        self.mealplans: list[dict] = []
        self.shopping_lists: list[dict] = []
        self.ratings: dict[str, int] = {}  # recipe id -> rating
        self.user_id = str(uuid.uuid4())

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
            return httpx.Response(200, json={"id": self.user_id, "username": "cook", "groupSlug": "home"})
        planning = self._planning(request, path)
        if planning is not None:
            return planning
        merge = re.match(r"^/(foods|units)/merge$", path)
        if merge and request.method == "PUT":
            kind = "food" if merge.group(1) == "foods" else "unit"
            body = json.loads(request.content)
            src, dst = (body["fromFood"], body["toFood"]) if kind == "food" else (body["fromUnit"], body["toUnit"])
            if src not in self.db[kind] or dst not in self.db[kind]:
                return httpx.Response(500, json={"detail": "Failed to merge"})
            for recipe in self.recipes.values():
                for ing in recipe.get("recipeIngredient", []):
                    if (ing.get(kind) or {}).get("id") == src:
                        ing[kind] = {"id": dst, "name": self.db[kind][dst]["name"]}
            del self.db[kind][src]
            return httpx.Response(200, json={"message": "merged"})
        item = re.match(r"^(/foods|/units|/organizers/tags)/([^/]+)$", path)
        if item and request.method == "DELETE":
            kind = LISTS[item.group(1)]
            if item.group(2) not in self.db[kind]:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=self.db[kind].pop(item.group(2)))
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
                return self._page([{k: r.get(k) for k in ("id", "name", "slug", "image", "recipeServings", "tags",
                                                         "description", "totalTime", "rating", "lastMade")}
                                   for r in self.recipes.values()], request)
            if request.method == "POST":
                name = json.loads(request.content)["name"]
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                while slug in self.recipes:
                    slug += "-1"
                self.recipes[slug] = {"id": str(uuid.uuid4()), "name": name, "slug": slug, "image": None,
                                      "recipeServings": 0, "recipeIngredient": [], "recipeInstructions": [],
                                      "tags": [], "recipeCategory": []}
                return httpx.Response(201, json=slug)
        m = re.match(r"^/recipes/([^/]+)(/image)?$", path)
        if m:
            slug = self._slug(m.group(1))
            if slug not in self.recipes:
                return httpx.Response(404, json={"detail": "not found"})
            if m.group(2):
                content = request.content.decode("latin-1")
                ext = re.search(r'name="extension"\r\n\r\n([^\r]+)', content)
                self.images[slug] = (ext.group(1) if ext else "", request.content)
                self.recipes[slug]["image"] = "1"
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

    def _slug(self, slug_or_id):
        return next((s for s, r in self.recipes.items() if r["id"] == slug_or_id), slug_or_id)

    def _planning(self, request, path):
        if not (path.startswith(("/households/", "/users/")) or path.endswith("/last-made")):
            return None
        body = json.loads(request.content) if request.content else None
        if path == "/households/mealplans":
            if request.method == "GET":
                start, end = request.url.params.get("start_date"), request.url.params.get("end_date")
                entries = [e for e in self.mealplans if (not start or e["date"] >= start) and (not end or e["date"] <= end)]
                return self._page(copy.deepcopy(entries), request)
            recipe = next((r for r in self.recipes.values() if r["id"] == body.get("recipeId")), None)
            if body.get("entryType") not in ("breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert") or not recipe:
                return httpx.Response(422, json={"detail": "invalid entry"})
            return httpx.Response(201, json=self.add_mealplan(body["date"], body["entryType"], recipe))
        if path == "/households/shopping/lists":
            if request.method == "GET":
                return self._page([{"id": l["id"], "name": l["name"]} for l in self.shopping_lists], request)
            self.shopping_lists.append({"id": str(uuid.uuid4()), "name": body.get("name"), "recipes": []})
            return httpx.Response(201, json=self.shopping_lists[-1])
        m = re.match(r"^/households/shopping/lists/([^/]+)/recipe$", path)
        if m and request.method == "POST":
            lst = next((l for l in self.shopping_lists if l["id"] == m.group(1)), None)
            if lst is None or not isinstance(body, list):
                return httpx.Response(404 if lst is None else 422, json={})
            lst["recipes"].extend(b["recipeId"] for b in body)
            return httpx.Response(200, json=lst)
        m = re.match(r"^/users/([^/]+)/ratings/([^/]+)$", path)
        if m and request.method == "POST":
            slug = self._slug(m.group(2))
            if m.group(1) != self.user_id or slug not in self.recipes:
                return httpx.Response(404, json={})
            self.ratings[self.recipes[slug]["id"]] = body["rating"]
            self.recipes[slug]["rating"] = body["rating"]
            return httpx.Response(200, json=None)
        m = re.match(r"^/recipes/([^/]+)/last-made$", path)
        if m and request.method == "PATCH":
            slug = self._slug(m.group(1))
            if slug not in self.recipes:
                return httpx.Response(404, json={})
            self.recipes[slug]["lastMade"] = body["timestamp"]
            return httpx.Response(200, json=self.recipes[slug])
        return None

    def add_mealplan(self, date, entry_type, recipe):
        entry = {"id": len(self.mealplans) + 1, "date": date, "entryType": entry_type, "title": "", "text": "",
                 "recipeId": recipe["id"], "recipe": {k: recipe.get(k) for k in ("id", "name", "slug", "recipeServings", "lastMade")}}
        self.mealplans.append(entry)
        return entry

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

    def add_recipe(self, name, ingredients=(), tags=(), servings=0, image=None, **extra):
        """ingredients: [(food item, unit item or None)]"""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self.recipes[slug] = {
            "id": str(uuid.uuid4()), "name": name, "slug": slug, "image": image, "recipeServings": servings,
            "recipeIngredient": [{"referenceId": str(uuid.uuid4()), "quantity": 1, "note": "",
                                  "food": {"id": f["id"], "name": f["name"]},
                                  "unit": {"id": u["id"], "name": u["name"]} if u else None} for f, u in ingredients],
            "recipeInstructions": [], "tags": [{"id": t["id"], "name": t["name"], "slug": t["slug"]} for t in tags],
            "recipeCategory": [], **extra,
        }
        return self.recipes[slug]
