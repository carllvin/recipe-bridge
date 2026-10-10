# Features

Everything the app does, area by area. The app has four areas, reachable
from the top bar (on the phone: the bottom bar). Texts in the app name your
recipe manager – "Tandoor" or "Mealie".

[📥 Import](#-import) · [✅ Review](#-review) · [🔧 Maintain](#-maintain) ·
[📅 Plan](#-plan) · [On the phone](#on-the-phone) ·
[Tandoor or Mealie](#tandoor-or-mealie) · [Known limitations](#known-limitations)

← [Back to the README](../README.md)

## 📥 Import

**Files** – drop them on the import area or click to choose:

- **Cookbooks as PDF, EPUB or Word (.docx)** – the AI reads every page (in
  overlapping chunks, so books with hundreds of pages work), finds each
  recipe with its page range, ingredients, steps, times, servings, tags and
  photo. Pictures embedded in a Word document become photo candidates.
- **Photos of cookbook pages** – drop them in or, on the phone, take them one
  by one with the camera. They are collected first, can be reordered and
  removed, and are imported together; tick *"all photos show one single
  recipe"* when ingredients and method are on different pages. Scans and
  photos go through OCR (Tesseract) with deskewing, cropping and two-page
  spread detection.
- **Handwriting** – for recipe cards and notes, tick *handwriting*: the AI
  reads the photos itself instead of the OCR (costs a little more).
- **A `.txt` file** – either a **list of recipe links** (one per line, or any
  text containing links; up to 50) or **recipe text**; the app tells them
  apart by the content. Markdown files (`.md`) are read as text.
- **Browser bookmarks** – the HTML export of Chrome, Firefox, Safari or Edge
  opens a pick list with the bookmark folders as a filter.

**From the web and the clipboard** – one field below the import area; the
app recognizes what you paste:

- **A recipe page** – imported directly. **🌐 Scan the whole website**
  instead looks for more recipes on the same site: via its sitemap, or by
  following the links of the page you entered up to the chosen **depth**
  (1–4 levels, incl. "page 2, 3 …") – and recognizes them by the recipe
  data they embed (schema.org), **without AI**. Results show up while it
  runs, with photo, time and an *already in your collection* mark; you pick
  which ones to import. It respects the site's `robots.txt`, reads at most
  one page per second and checks up to 300 pages.
- **A cooking video** from **YouTube, Instagram or TikTok** – the AI reads
  the recipe from the description or caption, and for YouTube also from the
  subtitles (what is said in the video). If a site only shows a login page,
  the app says so – then paste the caption as text.
- **Recipe text** – from a message, an email or a note.

**🎙️ Dictate a recipe** – record it in the app (or share / drop a voice note:
m4a, mp3, ogg, opus, wav, webm). It is transcribed and then read like a
pasted text. Needs an OpenAI or Gemini key (also when `AI_PROVIDER` is
`anthropic`) or a local transcription model, see
[Local AI](configuration.md#local-ai-ollama--co).

<p align="center">
  <img src="../screenshots/scan.png" alt="Picking recipes found by the website scan" width="70%">
</p>

Links (single, from a list, a scan or bookmarks) are read one page at a
time – the page's schema.org recipe data is used when present – and each
recipe keeps its link as the source. Links that can't be loaded or contain
no recipe are skipped and listed in the review.

**More ways in** (under *More ways* on the import page):

- **📱 Share from the phone** – install the app on your phone (browser menu →
  *Install app* / *Add to home screen*; needs HTTPS). It then shows up in the
  share menu: links, text and photos land right in the import.
- **🔖 Bookmark button** – drag it to your browser's bookmark bar; one click
  on a recipe page sends it to the app.
- **📂 Watched folder** – files put into a folder (`WATCH_DIR`) are imported
  on their own and wait under ✅ Review → *New imports*.
- **🚚 Move from Tandoor / Mealie** – when both are configured, all recipes
  (or one cookbook) of the other recipe manager come over **without AI**:
  ingredient sections, steps, times, tags, photos and source links. They go
  through the normal review (recipes that exist already are deselected) and
  into the cookbooks they were in before.

Everything is **translated into your language** (`OUTPUT_LANGUAGE`) and
**converted to metric** on the way.

> **Recipes from other apps** (Paprika, Nextcloud Cookbook, RecipeSage, …):
> Tandoor and Mealie import their export files themselves. Afterwards run
> **Process new recipes** on the 🔧 Maintain page to translate them, match
> their ingredients to yours and add tags.

Before anything goes into your collection you review the result – and the
review covers everything, so nothing has to be checked again afterwards:

<p align="center">
  <img src="../screenshots/review_recipes.png" alt="Reviewing the recipes found in a cookbook" width="85%">
</p>

- Edit title, description, servings, times, tags, ingredients and steps;
  pick or deselect the photo.
- Each ingredient is assigned to the step that needs it (dropdown on the
  right), so the ingredients show up at the right step.
- Ingredients, units and tags are matched against your collection:
  ✓ exists · ↺ matched to an existing entry ("onions" → "Onion") · new. A
  matched entry shows what was read below it ("read as: onions").
- Recipes that probably exist already are flagged (exact matches are
  deselected). With an image AI, missing photos can be generated and the
  recipe's own photos improved (light, colors, sharpness – the dish stays
  as it is), one by one or for all recipes while importing.
- **Import** puts the selected recipes into a cookbook – pick an existing
  one or create a new one (with Mealie a category); failed ones can be retried on their own and an
  import can be undone. With Tandoor, the new ingredients then get their
  details (plural, nutrition, supermarket category, gram conversions) filled
  in automatically – shown under ✅ Review → *Recently applied*, with undo.

## ✅ Review

One inbox for every suggestion – from automatic runs, from tools you
started and imports that arrived without you (phone, watched folder) –
grouped by kind:

<p align="center">
  <img src="../screenshots/review_inbox.png" alt="The review inbox" width="85%">
</p>

- **Merge & rename** duplicates (ingredients, units, tags), **ingredient
  details** (plural, nutrition, supermarket category), **gram conversions**,
  **recipe revisions** (one block of text split into steps, ingredients
  assigned to steps – with a before/after preview), **translations**,
  **tags & season**, **servings**, **photos**, **amounts in the steps**,
  **contradictions** and **unused entries**. (The weekly plan is reviewed
  and added on the 📅 Plan page itself.)
- When an ingredient like *gemahlene Mandeln* is renamed to or merged into
  *Mandeln*, the preparation (*gemahlen*) goes into the note of every
  recipe line that used it.
- Suggestions marked **⚠️** need a look first (e.g. a sentence that may not
  read well, a change to many recipes) – *Select all* leaves them out.
- Tick what you want and **Apply** or **Skip**. Applying runs **in the
  background on the server**, strictly one after another – you can close the
  page; the queue even survives a restart.
- Suggestions that failed (e.g. the recipe manager was briefly unreachable)
  stay in a **Failed** group with the error: *Retry* or *Dismiss*.
- **Recently applied** lists what was changed in the last 14 days. With
  Tandoor any of it can be **undone**, including merges (the merged-away
  ingredient is recreated and the recipes point to it again).

## 🔧 Maintain

<p align="center">
  <img src="../screenshots/maintain.png" alt="Collection health, automation and budget" width="85%">
</p>

- **Process new recipes** handles recipes added since the last run directly
  in your recipe manager (URL import, app, by hand): translates them right
  away, then suggests revising their structure, matching their
  ingredients/units/tags to existing ones, filling in ingredient details and
  conversions (Tandoor) and adding tags. Recipes imported through this app
  were already covered by the review – they only get the *amounts into the
  steps* (see below), applied right away like the ingredient details;
  only ones marked ⚠️ wait for review.
- **State of your collection** counts what's left to do – without AI – in
  groups:
  - *Ingredients*: possible duplicates, without nutrition or supermarket
    category, missing conversions, unused;
  - *Units*: possible duplicates, unused;
  - *Recipes*: not in your language, one block of text / all ingredients in
    step 1, steps without amounts, contradictions, without a season, few
    tags, without servings, without a photo, weak photos;
  - *Tags*: not in a tag group, unused.

  **Fix** starts the tool for exactly that tile (e.g. only the likely
  duplicates go to the AI, not your whole ingredient list). **Entries** lists
  them; single entries can be **ignored** (e.g. *Water* without nutrition)
  and are never suggested again. Each group also has a tool for the whole
  collection (all ingredients, all units, tag translate & simplify). Tiles
  show when a fix is running or waiting for review, and the overview
  recounts on its own after you applied something.
- **Steps without amounts** – "Das Mehl mit der Milch verrühren" becomes
  "250 g Mehl mit 500 ml Milch verrühren", with an ingredient's comment in
  brackets. The AI only marks where an ingredient is mentioned – it never
  writes a number: with Tandoor the step gets Tandoor's templates
  (`{{ ingredients[0] }}`), so the amounts follow when you change the
  servings; with Mealie they are written out. A second AI pass proofreads
  each step as it will read; doubtful ones are marked ⚠️.
- **Contradictions** (recipe doctor) – found without AI: an ingredient no
  step mentions, a step naming an ingredient that isn't in the list, an
  amount that can't be right (400 g salt for 4 servings), a time that
  doesn't fit the method. Only for those, the AI proposes a fix; if it finds
  a false alarm, the recipe goes on the tile's ignore list.
- **Weak photos** – too dark, too bright, washed out, blurry or too small,
  measured from the pixels **without AI** (each photo once). Applying a
  suggestion improves the photo with the image AI (`IMAGE_GEN_ENABLED`).
- **💬 Changes by chat** – ask in plain words: "every recipe with salmon gets
  the tag Fisch", "rename Paprika rot to rote Paprika", "put all soups into
  the cookbook Winter", "which recipes have no photo?". The AI only picks
  from a fixed set of actions (rename/merge ingredients, units, tags;
  add/remove/delete tags; servings; cookbook; supermarket category; find)
  and describes which recipes are meant – the app finds them itself. Every
  change is shown with the recipes it touches and applied only on a click;
  changes to more than 50 recipes and deleting a tag need a second click. A
  warning has to be confirmed once before the chat can be used.

  <p align="center">
    <img src="../screenshots/chat.png" alt="Asking for changes in the maintenance chat" width="75%">
  </p>
- **Automation & budget** (stored in the app, no restart needed):
  - *Automatic maintenance* – at a chosen time every N days, prepare
    suggestions for the selected tiles.
  - *Monthly AI budget* – a token limit; once reached, automatic runs pause
    until the next month (optionally manual starts and imports too).
  - *Notifications* – a test message for the configured channels.
  - *AI usage* of the last 30 days per tool.

## 📅 Plan

<p align="center">
  <img src="../screenshots/plan.png" alt="How was it, what can I cook today and the weekly plan" width="85%">
</p>

- **🏠 Household** – how many people eat, what must **never** be in a dish
  (allergies, intolerances), what you'd **rather not** eat, and fixed wishes
  per weekday ("Friday: pizza"), plus **nutrition goals**: max. kcal and
  min. protein per serving and more in words ("2x fish a week, little red
  meat"). The weekly plan, its chat and *what can I cook today?* follow it:
  recipes with a "never" ingredient are left out, disliked ones come last,
  recipes known to be above the kcal limit don't get planned (energy and
  protein per serving come from Tandoor's computed nutrition or Mealie's
  nutrition), and planned days get the number of persons as servings (the
  shopping list scales the amounts).
- **How was it?** – rate the meals you planned in the last days. With
  Tandoor the rating goes in as a cook log, with Mealie as your rating plus
  the recipe's "last made" date – either way the weekly plan uses it.
- **What can I cook today?** – type what you have at home, or take a
  **photo of the fridge** and let the AI list what it sees, and get matching
  recipes from your collection (the matching needs no AI; "tomato" also
  finds "cherry tomatoes"). Salt, pepper, oil, sugar, flour, butter,
  vinegar, onions and garlic count as always at home. *Plan for today* puts
  a recipe on the meal plan.
- **Weekly plan** – the AI picks one of your recipes per day:
  - the week starts on your **shopping day** (the coming Saturday by
    default): recipes with quickly perishing ingredients – fresh fish, mince,
    leafy greens, herbs, berries – come first, pantry and frozen dishes
    towards the end;
  - it prefers recipes that use what's **already at home** (typed or from a
    fridge photo), and **uses up rests**: an ingredient that usually leaves
    a rest (cream, fresh herbs, feta …) is planned again a day or two later
    – each day shows it ("🔁 Feta: Tue");
  - it follows your wishes ("2x vegetarian, quick on weekdays"), the season,
    variety, your ratings and how long ago you cooked something;
  - days that are already planned are left out, any day can be re-rolled,
    and a **chat** below the week changes it on request ("something with
    beef on Thursday", "swap Monday and Wednesday");
  - the selected days go into the meal plan, optionally with the
    ingredients on the shopping list;
  - **🛒 Shopping list to share** – the ingredients of the planned days
    added up, scaled to the household and grouped by supermarket aisle
    (Tandoor's categories, Mealie's labels), as text for a messenger or to
    copy; staples and what's at home come under *check the pantry*;
  - **🔪 Meal-prep plan** – one work plan for the planned (or selected)
    days, to cook ahead in one session: shared preparation done once with
    the combined amounts ("dice 3 onions for the soup and the curry"), oven
    and hob in parallel, the longest things first, and how to keep what's
    prepared. The **oven** is planned before the AI sees the recipes: dishes
    within 20 °C bake together at their mean temperature with adjusted
    times ("check whether it's done"), very different temperatures one
    after another – never two temperatures at once (fan ovens counted as
    +20 °C).
- **🥂 Guest menu** – occasion, day, number of guests, courses (starter,
  soup, main, side, dessert), what the guests can't eat and wishes: the AI
  puts together a menu from your recipes – one per course, matching each
  other (no main ingredient twice, not three heavy courses), the
  household's and the guests' "never" ingredients left out. Swap any course
  on its own, put the whole menu into the meal plan with the guests as
  servings, get its **shopping list** and a **🔪 work plan** with clock
  times counting back from "ready at 19:30" – all courses at once, or
  served one after another (15/25/40 min apart, the side dish with the
  main) with only the final steps (searing, sauce, plating) between the
  courses and the oven planned course by course. (Tags like *Dessert* or
  *Starter* on your recipes help it pick the right ones.)

## On the phone

The whole app works on the phone – with a bottom navigation, the camera for
cookbook and fridge photos, the share menu for links, and the review inbox
for approving suggestions on the go. Optional push notifications (ntfy or
Telegram) tell you when an import is ready, automatic runs prepared
suggestions or the AI budget runs low.

<p align="center">
  <img src="../screenshots/mobile.png" alt="Review inbox and plan on a phone" width="60%">
</p>

## Tandoor or Mealie

`RECIPE_MANAGER` chooses which one the app works with (`tandoor` by
default).

| | Tandoor | Mealie |
|---|---|---|
| Import (all sources), review, matching | ✓ | ✓ |
| Weekly plan, what can I cook today, how was it, household, shopping list to share | ✓ | ✓ (rating + "last made" instead of a cook log) |
| Duplicates, unused entries, servings, photos | ✓ | ✓ |
| Tags (clean up, season, more tags), translate & revise recipes, process new recipes | ✓ | ✓ |
| Amounts into the steps | ✓ (templates – follow the servings) | ✓ (written out) |
| Contradictions, weak photos, changes by chat | ✓ | ✓ (chat without supermarket categories) |
| Moving recipes from the other one | ✓ | ✓ |
| Automatic maintenance | ✓ | ✓ (for the tiles above) |
| Nutrition, supermarket categories, unit conversions, tag groups | ✓ | – (no matching data model) |
| Undo applied changes | ✓ | – (every change is still reviewed first) |

With Mealie, the cookbook name of an import becomes a category (Mealie's
cookbooks are saved filters, e.g. on a category), and meal types are
Mealie's fixed ones (breakfast, lunch, dinner, …).

## Known limitations

- **Undo** (Tandoor) restores the state from before the change; edits you
  made in Tandoor to the same recipes or ingredients since then are
  overwritten. Side effects Tandoor does on its own (e.g. shopping-list
  entries created with a meal-plan entry) are not undone. Changes in Mealie
  can't be undone from the app.
- The **Mealie** support was built against Mealie's API as published on
  GitHub and a fake of it – details like time formats may need adjusting
  for your version (see [the note on the APIs](setup.md#a-note-on-the-recipe-managers-apis)).
- Ingredient-to-step assignment and image matching are done by the AI or
  heuristically – worth a glance in the review screen for complex recipes.
- Duplicate recipe detection compares titles only, not ingredients.
- **Allergies** in the household profile and the guest menu are matched by
  name (ingredients and title) and told to the AI as well – check the plan
  anyway when it really matters.
- **Nutrition goals** need nutrition data: with Tandoor the ingredients'
  properties (see *Without nutrition* under Maintain), with Mealie the
  recipe's nutrition. Recipes without it are planned as before.
- The **guest menu** recognizes courses only by recipe names and tags.
- **Video links**: YouTube, Instagram and TikTok change often and sometimes
  show bots a login or consent page – then only the caption (or nothing) is
  found.
- **Amounts in the steps** with Tandoor use its templates; the comment in
  brackets uses `{{ ingredients[n].note }}` – if your Tandoor version shows
  `()` instead, please report it.
- The **photo check** measures at most 200 new photos per overview refresh;
  large collections fill the tile over several refreshes.
- The freshness order of the weekly plan is based on ingredient names
  (German and English word lists) – unusual names may not be recognized.
- The website scan only finds pages that embed schema.org recipe data, and
  sites that load their content with JavaScript or block bots (e.g. behind
  Cloudflare) yield little or nothing. It is meant for picking recipes for
  your own collection – respect the site's terms.
- Imports that haven't been sent to your recipe manager yet are lost when
  the container restarts (open suggestions, the queue and the undo history
  are kept).
- `TOC_AWARE_CHUNKING` (aligning chunks to a PDF's table of contents) is
  experimental and off by default – it has made some PDFs yield no recipes.
