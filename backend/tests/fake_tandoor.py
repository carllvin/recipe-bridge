"""A small in-memory stand-in for Tandoor's REST API - enough for the
tests to apply and undo real suggestions: list/detail GET, POST, PATCH/PUT
and DELETE on any endpoint, nested recipe steps/ingredients with food/unit
references, and the ?foods= / ?keywords= recipe filters."""
from __future__ import annotations

import copy
import itertools
import json
import re

import httpx


class FakeTandoor:
    def __init__(self):
        self._ids = itertools.count(1000)
        self.db: dict[str, dict[int, dict]] = {}
        self.requests: list[tuple[str, str]] = []

    # ---- setup helpers ----
    def add(self, endpoint: str, obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        obj.setdefault("id", next(self._ids))
        self.db.setdefault(endpoint, {})[obj["id"]] = obj
        if endpoint == "recipe":
            self._normalize_recipe(obj)
        return obj

    def client(self):
        from app import tandoor_client
        return tandoor_client.RecordingClient(base_url="http://tandoor/api", transport=httpx.MockTransport(self.handler))

    # ---- internals ----
    def _ref(self, endpoint, ref):
        if ref is None:
            return None
        obj = self.db.get(endpoint, {}).get(ref.get("id"))
        if obj is None:
            raise KeyError(f"{endpoint} #{ref.get('id')} does not exist")
        return {"id": obj["id"], "name": obj["name"], "plural_name": obj.get("plural_name", "")}

    def _normalize_recipe(self, recipe):
        for step in recipe.get("steps", []):
            step.setdefault("id", next(self._ids))
            for ing in step.get("ingredients", []):
                ing.setdefault("id", next(self._ids))
                ing["food"] = self._ref("food", ing.get("food"))
                ing["unit"] = self._ref("unit", ing.get("unit"))
        recipe["keywords"] = [self._ref("keyword", k) for k in recipe.get("keywords", [])]

    def _uses(self, recipe, param, value):
        if param == "foods":
            return any((i.get("food") or {}).get("id") == value for s in recipe["steps"] for i in s["ingredients"])
        if param == "keywords":
            return any(k["id"] == value for k in recipe.get("keywords", []))
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.requests.append((request.method, path))
        match = re.match(r"^/([a-z\-]+)/(?:(\d+)/)?$", path)
        if not match:
            return httpx.Response(404, json={})
        endpoint, obj_id = match.group(1), int(match.group(2)) if match.group(2) else None
        store = self.db.setdefault(endpoint, {})
        body = json.loads(request.content) if request.content else None
        try:
            if request.method == "GET":
                if obj_id is None:
                    items = list(store.values())
                    for param in ("foods", "keywords"):
                        if param in request.url.params:
                            items = [r for r in items if self._uses(r, param, int(request.url.params[param]))]
                    return httpx.Response(200, json={"count": len(items), "results": items, "next": None})
                return httpx.Response(200, json=store[obj_id]) if obj_id in store else httpx.Response(404, json={})
            if request.method == "POST":
                if endpoint in ("food", "unit", "keyword") and any(o["name"] == body["name"] for o in store.values()):
                    return httpx.Response(400, json={"name": ["already exists"]})
                return httpx.Response(201, json=self.add(endpoint, body))
            if request.method in ("PATCH", "PUT"):
                if obj_id not in store:
                    return httpx.Response(404, json={})
                updated = copy.deepcopy(store[obj_id])
                updated.update(copy.deepcopy(body))
                if endpoint == "recipe":
                    self._normalize_recipe(updated)
                store[obj_id] = updated
                return httpx.Response(200, json=updated)
            if request.method == "DELETE":
                store.pop(obj_id, None)
                return httpx.Response(204)
        except KeyError as exc:
            return httpx.Response(400, json={"detail": str(exc)})
        return httpx.Response(405, json={})

    # ---- assertions ----
    def names(self, endpoint) -> list[str]:
        return sorted(o["name"] for o in self.db.get(endpoint, {}).values())
