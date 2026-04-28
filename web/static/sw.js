// Service Worker для PWA
const CACHE_NAME = 'crypto-bot-v1.0';
const urlsToCache = [
  '/',
  '/static/manifest.json',
  '/static/icons/icon-72x72.png',
  '/static/icons/icon-96x96.png',
  '/static/icons/icon-128x128.png',
  '/static/icons/icon-144x144.png',
  '/static/icons/icon-152x152.png',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-384x384.png',
  '/static/icons/icon-512x512.png'
];

// Встановлення Service Worker
self.addEventListener('install', event => {
  console.log('[SW] Installing...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('[SW] Caching app shell');
        return cache.addAll(urlsToCache);
      })
  );
});

// Активація - видалення старих кешів
self.addEventListener('activate', event => {
  console.log('[SW] Activating...');
  event.waitUntil(
    caches.keys().then(keyList => {
      return Promise.all(keyList.map(key => {
        if (key !== CACHE_NAME) {
          console.log('[SW] Removing old cache', key);
          return caches.delete(key);
        }
      }));
    })
  );
  return self.clients.claim();
});

// Перехоплення запитів - стратегія "Network First"
self.addEventListener('fetch', event => {
  // API запити не кешуємо
  if (event.request.url.includes('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Статичні файли - кеш з оновленням
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        if (response) {
          // Оновлюємо кеш в фоні
          fetch(event.request).then(networkResponse => {
            if (networkResponse && networkResponse.status === 200) {
              caches.open(CACHE_NAME).then(cache => {
                cache.put(event.request, networkResponse);
              });
            }
          });
          return response;
        }
        return fetch(event.request);
      })
      .catch(() => {
        // Офлайн режим - показуємо заглушку
        if (event.request.mode === 'navigate') {
          return caches.match('/offline.html');
        }
      })
  );
});

// Push сповіщення (опціонально)
self.addEventListener('push', event => {
  const data = event.data.json();
  const options = {
    body: data.body || 'Новий сигнал від бота',
    icon: '/static/icons/icon-192x192.png',
    badge: '/static/icons/icon-72x72.png',
    vibrate: [200, 100, 200],
    requireInteraction: true,
    actions: [
      { action: 'open', title: 'Відкрити бота' },
      { action: 'close', title: 'Закрити' }
    ]
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'Crypto Bot', options)
  );
});

// Обробка кліку на сповіщення
self.addEventListener('notificationclick', event => {
  event.notification.close();

  if (event.action === 'open') {
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(windowClients => {
        for (let client of windowClients) {
          if (client.url === '/' && 'focus' in client) {
            return client.focus();
          }
        }
        if (clients.openWindow) {
          return clients.openWindow('/');
        }
      })
    );
  }
});