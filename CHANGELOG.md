# Changelog

## Unreleased

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
  after another
- The weekly plan uses up rests (cream, herbs …) across the week
- Shopping list to share, grouped by supermarket aisle

**Also** – `AI_PROVIDER=compatible` for local models (Ollama, LM Studio …)
and any OpenAI-compatible API

## 1.0.0-beta.1

The first release of **Recipe Bridge**, as a ready-made Docker image
(`ghcr.io/carllvin/recipe-bridge:1.0.0-beta.1`, amd64 and arm64). See the
[README](README.md) for setup.

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
