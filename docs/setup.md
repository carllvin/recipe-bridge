# Setup and operation

[Installation](#installation) · [Updating](#updating) · [Data and backups](#data-and-backups) ·
[Command-line scripts](#command-line-scripts) · [The recipe managers' APIs](#a-note-on-the-recipe-managers-apis)

← [Back to the README](../README.md)

## Installation

You need Docker (with Compose), a running Tandoor or Mealie instance and an
API key for one AI provider. The app comes as a ready-made image
(`ghcr.io/carllvin/recipe-bridge`, for amd64 and arm64 – e.g. a Raspberry
Pi or a Synology), so there's nothing to build.

1. **Get the two files** into a new folder:

   ```bash
   mkdir recipe-bridge && cd recipe-bridge
   curl -O https://raw.githubusercontent.com/carllvin/recipe-bridge/main/docker-compose.yml
   curl -o .env https://raw.githubusercontent.com/carllvin/recipe-bridge/main/.env.example
   ```

2. **Fill in `.env`** – at least:
   - `AI_PROVIDER` – `anthropic` (Claude), `openai` (ChatGPT), `gemini`
     (Google) or `compatible` (a local model, see [Local AI](configuration.md#local-ai-ollama--co))
   - the matching API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or
     `GEMINI_API_KEY`), or `COMPATIBLE_BASE_URL` and `COMPATIBLE_MODEL`
   - **Tandoor:** `TANDOOR_URL` (**without** a trailing slash) and
     `TANDOOR_TOKEN` (in Tandoor: *user menu → Settings → API → create new
     token*)
   - **or Mealie:** `RECIPE_MANAGER=mealie`, `MEALIE_URL` and `MEALIE_TOKEN`
     (in Mealie: *user profile → API tokens*)
   - `OUTPUT_LANGUAGE` – e.g. `Deutsch` (recipes **and** the app's UI)
   - `TZ` – e.g. `Europe/Berlin`, so automatic runs happen at your local time

3. **Start it**

   ```bash
   docker compose up -d
   ```

4. Open **http://localhost:8420** (change the port in `docker-compose.yml`
   if you like). The badge in the top right shows whether your recipe
   manager is reachable.

With Portainer, Unraid, Synology Container Manager & co., paste
`docker-compose.yml` as a new stack and put the settings from `.env.example`
into its environment / `.env`.

`:latest` follows the main branch; for a fixed version use e.g.
`image: ghcr.io/carllvin/recipe-bridge:1.1` (see the
[packages page](https://github.com/carllvin/recipe-bridge/pkgs/container/recipe-bridge)
for the versions).

**Access from outside:** set `APP_PASSWORD` – each device then signs in once
and stays signed in. For the phone's share menu and app install the app
must be reachable via **HTTPS**, e.g. behind the same reverse proxy as your
recipe manager.

**Building it yourself** (e.g. to change the code or add Tesseract
languages): clone the repository, create `.env` as above and run

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## Updating

```bash
docker compose pull
docker compose up -d
```

Your data (settings, open suggestions, undo history …) lives in a Docker
volume and survives updates – see [Data and backups](#data-and-backups).
If you build it yourself: `git pull` and the build command above.

## Data and backups

Everything the app keeps lives in the Docker volume `recipe_bridge_data`
(mounted at `/app/data`):

- running and recent imports with their uploaded files (deleted after
  `JOB_RETENTION_HOURS`),
- tool runs and suggestions waiting for review, the background queue, the
  last weekly plan with its chat,
- the undo history (14 days), ignored entries, settings (incl. the
  household profile), AI usage, the ingredient index for *what can I cook
  today?*, the measured photo quality.

Your recipes themselves are always in Tandoor or Mealie – the app doesn't
have a recipe database of its own. To back up its state, back up the
volume.

## Command-line scripts

A few operations deliberately need a terminal – mostly destructive ones for
test setups. They work with **Tandoor** only, live in
[`backend/scripts/`](../backend/scripts/) (they're part of the image) and run
inside the container, e.g.:

```bash
docker compose exec recipe-bridge python scripts/manage_ingredients.py --help
```

| Script | What it does |
|---|---|
| `manage_ingredients.py` | Review ingredient names, fill in plural and category, estimate nutrition |
| `manage_tags.py` | Simplify and translate tags, add season tags, suggest more tags |
| `cleanup_units.py` | Merge units that mean the same ("TL" / "Teelöffel" / "tsp") |
| `retranslate_recipes.py` | Translate recipes already in Tandoor after changing `OUTPUT_LANGUAGE` |
| `purge_recipes.py`, `purge_ingredients.py`, `purge_ingredients_to_15.py` | Delete (almost) all recipes/ingredients – **for test instances only, no undo** |

All of them do a dry run unless told otherwise; read the header of each
script before using it.

## A note on the recipe managers' APIs

Both recipe managers keep evolving, and their APIs can differ slightly
between versions.

- **Tandoor** – the client (`backend/app/tandoor_client.py`) is written
  against the documented REST API and tries the known alternatives where
  Tandoor renamed endpoints (e.g. `/api/recipe-book/` vs. `/api/cookbook/`).
  Compare with your instance's schema at
  `https://YOUR-TANDOOR-URL/api/schema/swagger-ui/`.
- **Mealie** – the client (`backend/app/mealie_client.py`, planning in
  `mealie_plan.py`) follows Mealie's current API (households, shopping
  lists, ratings). Compare with `https://YOUR-MEALIE-URL/docs`.

If the recipe manager rejects something, the UI shows its error message
verbatim (e.g. *"Tandoor rejected the recipe (400): …"*) – adjust the payload
in the client, or paste the error into an AI assistant and let it adjust the
client.
