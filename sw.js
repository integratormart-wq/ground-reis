self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  var url = new URL(req.url);
  if (url.protocol !== 'https:' && url.hostname !== 'localhost') {
    return;
  }
  event.respondWith(
    fetch(req).catch(function () {
      return new Response('Offline', { status: 503, statusText: 'Offline' });
    })
  );
});
