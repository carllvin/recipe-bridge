// Lint for the frontend (backend/app/static) - plain browser scripts, no
// build step. Mainly catches what tests can't: a function defined twice
// (the later one silently wins), undefined names, unreachable code.
// Run: npx eslint@10 backend/app/static (CI: .github/workflows/tests.yml)

const browser = Object.fromEntries([
  "window", "document", "navigator", "location", "history", "localStorage", "sessionStorage", "console",
  "fetch", "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
  "URL", "URLSearchParams", "FormData", "Blob", "File", "FileReader", "Image", "Notification", "alert",
  "confirm", "Event", "CustomEvent", "AbortController", "MediaRecorder", "crypto", "performance",
  "getComputedStyle", "matchMedia", "HTMLElement", "DOMParser", "btoa", "atob", "Intl", "TextEncoder",
  "self", "caches", "clients", "Response", "Request", "Headers", "structuredClone", "IntersectionObserver",
  "ResizeObserver", "MutationObserver", "Audio", "MediaStream", "screen", "innerWidth", "innerHeight",
  "scrollTo", "open", "prompt", "queueMicrotask",
].map((name) => [name, "readonly"]));

// Defined in i18n.js, used by app.js (both plain scripts, one page).
const fromI18n = Object.fromEntries(["TRANSLATIONS", "LANG_CODE", "T", "APP_CONFIG", "t", "tf",
  "applyStaticTranslations", "initI18n"].map((name) => [name, "writable"]));

const rules = {
  "no-redeclare": "error",
  "no-undef": "error",
  "no-dupe-keys": "error",
  "no-dupe-args": "error",
  "no-func-assign": "error",
  "no-const-assign": "error",
  "no-unreachable": "error",
  "no-self-assign": "error",
  "no-unsafe-negation": "error",
  "valid-typeof": "error",
  "use-isnan": "error",
  "no-unused-vars": ["warn", { vars: "local", args: "none", caughtErrors: "none" }],
};

export default [
  {
    files: ["backend/app/static/**/*.js"],
    languageOptions: { ecmaVersion: "latest", sourceType: "script", globals: browser },
    rules,
  },
  {
    files: ["backend/app/static/app.js"],
    languageOptions: { globals: { ...browser, ...fromI18n } },
  },
];
