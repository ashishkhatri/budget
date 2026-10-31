// Minimal service worker so the browser offers "Install app".
// It never caches: the app always talks to the server on this computer for fresh data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
// Only this app's own requests go through it; others (e.g. Plaid's bank-connection window) go
// straight to the browser.
self.addEventListener('fetch', e => {
  if (new URL(e.request.url).origin === self.location.origin) e.respondWith(fetch(e.request));
});
