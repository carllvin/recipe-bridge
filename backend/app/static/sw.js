// Service worker of the installable app. It is only here so the browser
// offers "Install app" and the app shows up in the phone's share menu
// (share_target in manifest.json) - it deliberately caches nothing, so the
// UI is always the current one from the server.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
