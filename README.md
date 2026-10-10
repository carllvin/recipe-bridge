<h1 align="center">🍳 Recipe Bridge</h1>

<p align="center">
  <b>An AI companion for your self-hosted
  <a href="https://tandoor.dev/">Tandoor</a> or <a href="https://mealie.io/">Mealie</a>.</b><br>
  Import recipes from anywhere · keep your collection tidy · plan meals from your own recipes
</p>

<p align="center">
  <a href="https://github.com/carllvin/recipe-bridge/releases"><img alt="Release" src="https://img.shields.io/github/v/release/carllvin/recipe-bridge?include_prereleases&label=release"></a>
  <a href="https://github.com/carllvin/recipe-bridge/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/carllvin/recipe-bridge/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/carllvin/recipe-bridge/pkgs/container/recipe-bridge"><img alt="Docker image" src="https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker&logoColor=white"></a>
  <img alt="amd64 · arm64" src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64-lightgrey">
</p>

<p align="center">
  <img src="screenshots/import.png" alt="Import page with the photo collector" width="85%">
</p>

Recipe Bridge runs next to your recipe manager and uses an AI – **Claude,
ChatGPT, Gemini or a local model** – for the tedious parts: reading a
400-page cookbook, translating it, merging "onions" into "Onion", adding
tags, planning the week. **It never changes anything behind your back:**
every change is shown as a suggestion first, and with Tandoor applied
changes can be undone.

> **Beta** – feature-complete, but it hasn't run on many installations yet
> (the Mealie support in particular is new). Please report problems as
> [issues](https://github.com/carllvin/recipe-bridge/issues).

## ✨ Highlights

### 📥 Import from almost anywhere

Cookbooks (**PDF, EPUB, Word**) · **photos** of cookbook pages and
**handwritten** cards · recipe pages and **whole recipe websites** ·
**YouTube, Instagram and TikTok** videos · **voice notes** · browser
bookmarks · pasted text · your phone's share menu · a watched folder ·
**moving from Tandoor to Mealie or back**.

Everything is translated into your language and converted to metric on the
way – and you review every recipe before it's saved: edit any field, see
ingredients, units and tags matched to the ones you already have, get
duplicates flagged.

<p align="center">
  <img src="screenshots/review_recipes.png" alt="Reviewing the recipes found in a cookbook" width="85%">
</p>

### 🥄 Amounts right in the steps

> *"Mix the flour with the milk"* → *"Mix **250 g** flour with **500 ml** milk (lukewarm)"*

No more scrolling back to the ingredient list while cooking. With Tandoor
the steps get Tandoor's templates, so **the amounts follow when you change
the servings**; with Mealie they're written out. The AI only marks where an
ingredient is mentioned – it never writes a number – and a second pass
proofreads every step. New imports get it automatically; one click does
your whole existing collection.

### 🔧 A tidy collection

<p align="center">
  <img src="screenshots/maintain.png" alt="Collection health, automation and budget" width="85%">
</p>

- **Collection health** at a glance – duplicates, missing nutrition, tags,
  seasons, servings, photos … each tile with its own *Fix* button.
- **Recipe doctor** – finds contradictions without AI: an ingredient no
  step uses, 400 g salt for 4 servings, a time that doesn't fit.
- **Weak photos** – dark, blurry or washed-out ones are found and can be
  improved by an image AI.
- **💬 Changes by chat** – *"every recipe with salmon gets the tag Fish"*,
  *"put all soups into the cookbook Winter"*. Shown first, applied on a
  click.
- **Automatic maintenance** on a schedule, a **monthly AI budget**, push
  notifications via ntfy or Telegram.

All suggestions land in **one review inbox**; applying runs in the
background, and with Tandoor anything can be **undone** – even merges.

### 📅 Meal planning for your household

<p align="center">
  <img src="screenshots/plan.png" alt="How was it, what can I cook today and the weekly plan" width="85%">
</p>

- **Household profile** – persons, allergies, dislikes, fixed days
  (*"Friday: pizza"*), nutrition goals.
- **What can I cook today?** – type what you have or snap a **photo of the
  fridge**.
- **Weekly plan** from your own recipes – perishable things first, uses up
  what's at home and the half cup of cream from Tuesday, follows your
  ratings and the season; adjust it by chat.
- **Shopping list to share**, grouped by supermarket aisle.
- **Meal-prep plan** – one work plan for the week's dishes, with shared
  prep done once and the oven planned sensibly.
- **Guest menu** – courses that go together, guests' allergies respected,
  shopping list and a timed work plan, courses served one after another.

➡️ **[All features in detail](docs/features.md)**

## 🚀 Quick start

You need **Docker** (with Compose), a running **Tandoor or Mealie** and an
**API key** for one AI provider (or a local model).

```bash
mkdir recipe-bridge && cd recipe-bridge
curl -O https://raw.githubusercontent.com/carllvin/recipe-bridge/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/carllvin/recipe-bridge/main/.env.example
```

Fill in `.env` – at least:

```bash
AI_PROVIDER=anthropic            # or openai, gemini, compatible (Ollama & co.)
ANTHROPIC_API_KEY=sk-ant-...     # the key for that provider

TANDOOR_URL=https://tandoor.example.com   # no trailing slash
TANDOOR_TOKEN=...                          # Tandoor: Settings → API → new token
# or: RECIPE_MANAGER=mealie, MEALIE_URL=..., MEALIE_TOKEN=...

OUTPUT_LANGUAGE=Deutsch          # recipes and interface
TZ=Europe/Berlin
```

```bash
docker compose up -d
```

Open **http://localhost:8420** – the badge in the top right shows whether
your recipe manager is reachable. Update with
`docker compose pull && docker compose up -d`.

Portainer, Unraid, Synology, HTTPS for the phone, a password for access
from outside, building it yourself: see **[Setup](docs/setup.md)**.

## Tandoor or Mealie?

Both work. Import, review, the maintenance tools, the amounts in the steps
and all of planning are available for either; a few things need Tandoor's
data model:

| Only with Tandoor | Why |
|---|---|
| Nutrition, supermarket categories, unit conversions, tag groups | Mealie has no matching data model |
| Undo applied changes | Mealie changes are still reviewed first, but can't be rolled back |

The full comparison is in [Features → Tandoor or Mealie](docs/features.md#tandoor-or-mealie).

## 💸 AI and costs

- Works with **Anthropic, OpenAI, Gemini** or any **OpenAI-compatible API**
  (Ollama, LM Studio, vLLM, OpenRouter …) – see
  [Local AI](docs/configuration.md#local-ai-ollama--co).
- The maintenance tools use a **cheaper model** and send only what's needed;
  the collection overview, duplicate detection, the website scan, moving
  recipes and the *what can I cook* matching need **no AI at all**.
- A **monthly budget** stops automatic runs when it's used up; the app
  shows the usage per tool.

## 📚 Documentation

| | |
|---|---|
| **[Features](docs/features.md)** | Import, review, maintain, plan – everything in detail, plus known limitations |
| **[Setup](docs/setup.md)** | Installation, updating, data and backups, command-line scripts |
| **[Configuration](docs/configuration.md)** | All `.env` options, custom instructions, languages, AI providers and costs |
| **[Development](docs/development.md)** | Architecture, tests, CI |
| **[Changelog](CHANGELOG.md)** | What's new in each version |

The interface is available in **Deutsch, English, Français, Italiano and
Español**; recipes can be written in any language.

## Contributing

Bug reports and ideas are welcome as
[issues](https://github.com/carllvin/recipe-bridge/issues). The test suite
runs without a recipe manager or an AI key – see
[Development](docs/development.md).
