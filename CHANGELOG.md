# Changelog

## Unreleased

- Half price with **Gemini** too: imports go to its Batch Mode like with
  Claude and OpenAI
- A notification (ntfy, Telegram) also when a half-price import started in
  the app is ready
- **Pinterest pins** lead to the recipe page they were saved from, short
  links (bit.ly, pin.it …) are followed – the recipe keeps the page's
  address as its source
- Alternatives in an ingredient ("butter or margarine", "lemon, lime or
  orange"): only the first becomes the ingredient, the others go into its
  note ("or margarine")
- Collection overview (Tandoor) faster: every list is read once instead of
  up to three times, the lists at the same time, new photos checked six at
  a time; the log shows how long each part took
- After applying a change to ingredients, units or tags only the recipes
  using them are read again – not the whole collection
- Mealie: the overview reads only recipes changed since its last read, the
  lists at the same time
- Background checks (recipe manager reachable, review counter) pause while
  the app isn't visible
- Removed: the tile **Contradictions** (recipe doctor) – it flagged too
  many recipes that were fine
- Docs: a short `.env.example` with what's needed to start;
  `docs/configuration.md` lists every setting

## 1.2.0

Docker image `ghcr.io/carllvin/recipe-bridge:1.2.0` (or `:1.2`). No changes
to the configuration are needed. Worth knowing:

- Imports from the **watched folder** now go to the AI as a batch at half
  price and are ready within up to 24 hours (Claude, OpenAI) –
  `WATCH_BATCH=false` brings back the immediate import.
- Drag the **bookmark button** into your bookmark bar again once – the new
  one hands over the page and its photos.
- New, all optional: `TRANSLATE_RECIPES`, `STEP_PHOTOS_AI`, `SOURCE_TAG`,
  `WATCH_BATCH` (see `.env.example`).

**Costs**
- AI batches at half price (Claude, OpenAI; ready within up to 24 hours):
  on by default for the watched folder, a checkbox on the import page for
  everything else – files, photos, links, link lists, text. A large import
  (from about 50,000 tokens) started without it asks once more. An import
  waiting for its batch survives a restart and collects the answers
  afterwards – nothing is sent or paid twice
- Cookbooks with Claude: the extraction's instructions are cached across
  the book's chunks – after the first chunk they cost about a tenth (usage
  and budget count cache reads that way)
- Amounts in the steps are written while the recipe is read (Tandoor) – no
  second AI call per recipe after the import (roughly 2,000–4,000 tokens
  per recipe); recipes without them still get them afterwards

**Import**
- Recipe language: `TRANSLATE_RECIPES=false` keeps every recipe in the
  language it was written in – also the clean-up of *Process new recipes*,
  the reuse of existing tags and the amounts in the steps
- Photos in the steps: in the review every photo can be the main photo,
  go into a step or stay unused; **Place photos into the steps** places
  them – a recipe page's step photos by position (free), the others by the
  image AI (`STEP_PHOTOS_AI=false` leaves it out); own photos with the "+"
  tile. With Tandoor as step files, with Mealie as recipe assets
- A tag with the source on every imported recipe: the website
  (`nytcooking`), the video channel or the cookbook's author (`ottolenghi`)
  – `SOURCE_TAG=false` turns it off
- Several links in the import field – separated by commas, spaces or lines
  – are imported like a link list; links in an RTF or Word file or pasted
  as text too (instead of becoming empty recipes)
- A page imported before is flagged as a duplicate by its address, even
  when the title differs
- Sites with bot protection (403): the app asks like a browser and tries a
  second time; the bookmark button hands over the page and its photos as
  your browser shows them, so such sites import too
- Recipe pages read more reliably: Recipe entries without a recipe, HowTo
  guides, "recipe" in lower case, pages inside one big form (e.g. Tesco Real
  Food), the largest text block instead of a teaser
- A site that doesn't answer no longer keeps the import waiting, and a
  failed import goes back to the import page with the error
- Handwritten notes are kept (in the ingredient, the step or a final "Note"
  step)
- WebP photos and RTF files
- A shorter message after the import

**Review**
- **Preview | Edit**: a recipe opens in the preview – as Tandoor (every step
  with its ingredients and amounts) or Mealie will show it, with the step
  photos
- Editing no longer drops step titles, step times and ingredient groups
- A click into an ingredient field places the cursor again in Safari and
  Firefox (only the ⠿ handle starts a drag)

**Maintain**
- Tile **Recipes without source tag**: existing recipes get their source
  tag by address (the site's name) or cookbook (its author)
- Faster: recipes are read several at a time; the overview reads only
  recipes changed since its last read; a tile with a few entries reads only
  those; an applied suggestion takes its entries off the tiles instead of a
  new scan
- **Diagnosis** (collapsed at the bottom): what the running app uses –
  version, recipe manager, AI provider and models, budget, settings, OCR,
  free space – with an AI test and a copy button for bug reports
- Mealie: no more *amounts into the steps* (no templates – written-out
  amounts were wrong once the servings changed)
- Amounts in the steps: fixed for steps with several photos and for
  recipes with every ingredient in step 1 after an import; the log says why
  a recipe gets none

**Also**
- "Undo all imports" updates the review again
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
