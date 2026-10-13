# Changelog

## Unreleased

**Import**
- A large import (a whole cookbook) first shows its estimated tokens and
  what is left of the monthly budget, and waits for **Start** or
  **Cancel** (`IMPORT_CONFIRM_TOKENS`, default 50,000; 0 = never ask)
- WebP photos (.webp) can be imported like JPEG, PNG and HEIC
- RTF files (.rtf, e.g. from TextEdit or WordPad) are read as recipe text
- Links in an RTF or Word file or pasted as text are imported as a link
  list (each page read on its own) instead of becoming empty recipes; a
  "recipe" with neither ingredients nor steps is left out
- Own photos can be added to a recipe in the review (the "+" tile)
- Photos in the steps: in the review every photo can be the main photo,
  go into a step or stay unused; nothing is placed until you ask with
  **Place photos into the steps** (per recipe or for all): a recipe page's
  step photos by position (free), the others by the image AI (tools model,
  only sure placements, hand-placed photos stay, its own line in *AI
  usage*; `STEP_PHOTOS_AI=false` leaves the AI out); with Tandoor as step
  files, with Mealie as recipe assets in the step text
- `TRANSLATE_RECIPES=false` keeps recipes in the language they were
  written in – also the ingredient/unit/tag clean-up of *Process new
  recipes*, the reuse of existing tags and the amounts in the steps; the
  prompts name the recipe's language ("written in English")
- Sites with bot protection (403): the app asks like a browser and tries a
  second time; the bookmark button now hands over the page your browser
  already shows, so such sites import too (drag the button in again once)
- The bookmark button also hands over the recipe's photos (main photo and
  step photos) as your browser loads them – for sites that refuse the app
  their pictures; photos the browser can't fetch are loaded as before
- A site that doesn't answer no longer keeps the import waiting: shorter
  waits with a clear hint, and a recipe's photos stop once their server
  hangs or refuses (at most 30 seconds for all of them)
- A failed import goes back to the import page with the error instead of
  leaving the loading screen turning
- Recipe pages read more reliably: a Recipe entry with only name and photo
  no longer hides the recipe on the page, step-by-step guides (schema.org
  HowTo) are read like recipes, and the page text comes from its largest
  text block instead of the first article (often a teaser)
- Recipe data written as "recipe" (lower case) or with a schema.org prefix
  is recognised (e.g. Tesco Real Food), and pages that put everything into
  one form (ASP.NET) keep their text
- Handwritten notes are kept: the transcription marks every note and
  correction, the extraction puts them into the ingredient, the step or a
  final "Note" step instead of dropping them

**Also**
- Mealie: no more *amounts into the steps* – Mealie has no templates,
  and amounts written into the text were wrong as soon as the servings
  changed (tile, tool and *Process new recipes*)
- Maintain → **Diagnosis** (collapsed at the bottom): what the running
  app actually uses – version, recipe manager reachable, AI provider and
  models, budget, language settings, OCR, free space – with a quick AI
  test and a copy button for bug reports; never shows a key
- "Undo all imports" updates the review again (it stopped with an error)
- The frontend is checked with ESLint on every pull request

## 1.1.0

Docker image `ghcr.io/carllvin/recipe-bridge:1.1.0` (or `:1.1`). No changes
to the configuration are needed; voice notes need an OpenAI or Gemini key or
a local transcription model (see `.env.example`).

**Import**
- Cooking videos from YouTube, Instagram and TikTok (description, caption,
  YouTube subtitles)
- Voice notes: dictate a recipe in the app or share/drop an audio file
- Move recipes from Tandoor to Mealie or back – without AI, with photos,
  tags and cookbooks, through the normal review

**Review**
- Renaming "gemahlene Mandeln" to "Mandeln" keeps "gemahlen" as the
  recipe lines' note
- Suggestions that need a look (⚠️) are left out of "select all"

**Maintain**
- Amounts into the steps (Tandoor templates that follow the servings,
  Mealie written out), with the ingredients' comments and a proofreading
  pass; also part of "process new recipes" and applied right after an import
  (unless marked ⚠️)
- Recipe doctor: contradictions found without AI, fixed with it
- Weak photos found without AI, improved with the image AI
- Changes by chat, from a fixed catalog of actions, with a warning first

**Plan**
- Household profile: persons, never / rather not, fixed weekdays,
  nutrition goals (max. kcal, min. protein per serving, more in words)
- Guest menu: one recipe per course from your collection, swap a course,
  into the meal plan for the guests, shopping list
- Meal-prep plan for the week and work plan for a guest menu (clock times
  back from "ready at"); the oven is planned first: similar temperatures
  share their mean with adjusted baking times, very different ones come one
  after another; a guest menu's courses can be served one after another
  (15/25/40 min apart) with the final steps planned between the courses
- The weekly plan uses up rests (cream, herbs …) across the week
- Shopping list to share, grouped by supermarket aisle

**Also** – `AI_PROVIDER=compatible` for local models (Ollama, LM Studio …)
and any OpenAI-compatible API

## 1.0.0-beta.1

The first release of **Recipe Bridge**, as a ready-made Docker image
(`ghcr.io/carllvin/recipe-bridge:1.0.0-beta.1`, amd64 and arm64). See the
[setup guide](docs/setup.md).

**Import** – into Tandoor or Mealie (`RECIPE_MANAGER`)
- Cookbooks (PDF, EPUB, Word), photos of cookbook pages and handwritten
  cards, single links, link lists, browser bookmarks, pasted text
- Website scan: finds the recipes of a food blog via its sitemap or links
  (1–4 levels deep), without AI
- Share from the phone, bookmark button, watched folder
- Translation into your language and metric units on the way
- Review before import: every field editable, ingredients assigned to steps,
  ingredients/units/tags matched against your collection, duplicates
  flagged, existing or new cookbook, photos generated or improved by an
  image AI

**Review** – one inbox for all suggestions, applied in the background;
failed ones can be retried, applied ones undone (Tandoor)

**Maintain**
- Overview of what's left to do (duplicates, missing nutrition, categories
  and conversions, untranslated or unstructured recipes, missing season,
  tags, servings or photo, unused entries, tags outside a group)
- A tool per tile, full-collection reviews, processing of new recipes
- Automatic maintenance on a schedule, monthly AI budget, notifications
  (ntfy, Telegram)

**Plan**
- Weekly plan starting on the shopping day: perishable dishes first,
  pantry dishes last, what's at home gets used up; change it in a chat
- What can I cook today? – by typed ingredients or a fridge photo
- How was it? – ratings feed back into the plan

**Also** – AI from Anthropic, OpenAI or Google; UI in German, English,
French, Italian and Spanish; optional password

**Known gaps** – tested against simulated Tandoor and Mealie APIs and
simulated AI answers; real-world use will show details to fix. With Mealie,
nutrition, supermarket categories, unit conversions and tag groups aren't
available and changes can't be undone from the app.
