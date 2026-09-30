from __future__ import annotations

import time
from typing import Optional, Union
from pydantic import BaseModel, Field


class Ingredient(BaseModel):
    amount: Optional[float] = None
    unit: Optional[str] = None
    name: str
    note: Optional[str] = None
    group: Optional[str] = None  # e.g. "For the dough", "For the filling"
    step_index: Optional[int] = None  # 0-based index of the step that needs this ingredient
    tandoor_match: Optional[str] = None  # "exists" | "matched" | "new" - set by import_matching before review
    original_name: Optional[str] = None  # name before it was matched to an existing Tandoor ingredient
    unit_match: Optional[str] = None  # same for the unit: "exists" | "matched" | "new"
    original_unit: Optional[str] = None  # unit before it was matched to an existing Tandoor unit


class Step(BaseModel):
    instruction: str
    title: Optional[str] = None
    time_minutes: Optional[int] = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class ExtractedRecipe(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    servings: Optional[int] = None
    prep_time_minutes: Optional[int] = None
    cook_time_minutes: Optional[int] = None
    total_time_minutes: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    tag_status: dict[str, str] = Field(default_factory=dict)    # tag -> "exists" | "matched" | "new" (import_matching)
    tag_original: dict[str, str] = Field(default_factory=dict)  # matched tag -> the tag as the AI wrote it
    ingredients: list[Ingredient] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    source_page_start: int
    source_page_end: int
    candidate_image_ids: list[str] = Field(default_factory=list)
    selected_image_id: Optional[str] = None
    selected: bool = True
    import_status: str = "pending"  # pending | importing | imported | error
    import_error: Optional[str] = None
    tandoor_recipe_id: Optional[Union[int, str]] = None  # Tandoor id, or Mealie slug (RECIPE_MANAGER=mealie)
    duplicate_match: Optional[str] = None    # name of the probably-already-existing Tandoor recipe
    duplicate_exact: bool = False            # True = (near-)exact title match, False = only similar
    source_url: Optional[str] = None         # web page the recipe was imported from (URL import) -> Tandoor's "source_url"
    cookbooks: list[str] = Field(default_factory=list)  # moved from the other manager: the cookbooks it was in there


class Job(BaseModel):
    id: str
    filename: str
    status: str = "processing"  # processing | ready | error
    error: Optional[str] = None
    page_count: int = 0
    recipes: list[ExtractedRecipe] = Field(default_factory=list)
    images: dict[str, dict] = Field(default_factory=dict)  # image_id -> {page, path}
    suggested_cookbook_name: Optional[str] = None
    cookbook_name: Optional[str] = None  # name confirmed/edited by the user
    progress_current: int = 0
    progress_total: int = 0
    progress_label: Optional[str] = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: float = Field(default_factory=time.time)  # epoch seconds, used for cleanup
    notes: list[str] = Field(default_factory=list)  # e.g. links of a link list that were skipped, shown in the review
    source: Optional[str] = None  # "share" (phone share menu) or "folder" (watched folder) - shown in the review inbox


class ToolSuggestion(BaseModel):
    """One proposed change from a maintenance tool run (e.g. "merge these two
    ingredients") - shown in the UI for individual approve/skip, mirroring how
    the CLI scripts print each suggestion and ask before applying it."""
    id: str
    kind: str  # e.g. "rename" | "merge" | "set_plural" | "tag" | "season"
    summary: str  # human-readable one-liner shown in the list, e.g. "merge 'Onion' into 'Zwiebel'"
    detail: dict = Field(default_factory=dict)  # the raw action payload the apply step needs
    preview: Optional[str] = None  # optional multi-line before/after, shown expandable under the summary
    status: str = "pending"  # pending | applied | skipped | error | undone
    error: Optional[str] = None
    applied_at: Optional[float] = None
    undoable: bool = False  # an undo journal was saved when it was applied (see undo.py)


class ToolJob(BaseModel):
    """A maintenance-tool run against the user's live Tandoor data (ingredient/
    tag/unit cleanup etc.) - separate from Job (recipe extraction) above, since
    the two have very different shapes and lifecycles, but polled the same way
    (GET, then act on individual items) from the UI."""
    id: str
    tool: str  # "ingredients_review" | "ingredients_metadata" | "ingredients_nutrition" | "tags_cleanup" | "tags_simplify" | "tags_translate" | "tags_season" | "tags_suggest_more" | "units_review" | "recipes_translate" | "ingredients_enrich" | "new_recipes"
    status: str = "scanning"  # scanning | ready | applying | done | cancelled | error
    error: Optional[str] = None
    progress_current: int = 0
    progress_total: int = 0
    progress_label: Optional[str] = None
    cost_estimate: Optional[str] = None  # shown as soon as the item count is known, before the first AI call
    cancel_requested: bool = False  # set by POST .../cancel; the running scan checks this after each chunk/item
    suggestions: list[ToolSuggestion] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    meta: dict = Field(default_factory=dict)  # tool-specific state, e.g. which recipes the new-recipes run covers
    created_at: float = Field(default_factory=time.time)

