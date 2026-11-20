// ═══ EASY MONEY PRO — Service Worker (ب3: التثبيت الكامل والأوفلاين) ═══
// السياسة المتعمدة: الهيكل فقط (صفحة اللوحة) يُخزَّن، وكل البيانات الحية شبكة دائماً.
// التنقلات network-first: الاتصال يحدّث النسخة المحفوظة في الكاش، والانقطاع يخدم آخر نسخة سليمة
// — فلا يمكن أبداً أن يُبنى قرار من كاش قديم، والكاش مجرد غلاف عرض يعمل بلا شبكة.
"use strict";
const SHELL = "emp_shell_v1";

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    // تخزين مسبق بالاسم المعروف للطقم — حتى أول جلسة تصبح محمية لقطع الشبكة المفاجئ.
    // best-effort: أي فشل لا يمنع التفعيل، والتنقلات الفعلية تُخزن كبداية.
    try {
      const c = await caches.open(SHELL);
      await c.add(new URL("index.html", self.registration.scope).href);
    } catch (err) { /* لا تخزين مسبق — لا يمنع التفعيل */ }
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const ks = await caches.keys();
    await Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || req.mode !== "navigate") return; // API/WebSocket/بقية الموارد: تمر بلا مساس
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // Bybit/Binance/تيليجرام: شبكة حية دائماً، لا تخزين ولا اعتراض
  e.respondWith((async () => {
    const cache = await caches.open(SHELL);
    try {
      const res = await fetch(req);
      if (res && res.ok) cache.put(req, res.clone()); // كل فتح ناجح ينعش النسخة المحفوظة
      return res;
    } catch (err) {
      const hit = (await cache.match(req, { ignoreSearch: true }))
        // رابط الجذر "/": الاستضافة تخدم index.html له لكن الكاش يخزن بالاسم الكامل —
        // هذا التعيين يجعل الأوفلاين على "/" يجد نسخة index.html المحفوظة (GitHub Pages وغيرها)
        || (await cache.match(new URL("index.html", req.url).href, { ignoreSearch: true }))
        || (await cache.match(self.registration.scope, { ignoreSearch: true }));
      if (hit) return hit; // آخر نسخة سليمة من اللوحة — بقية الواجهة تعرض آخر حالة محفوظة محلياً
      return new Response(
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width, initial-scale=1">' +
        '<title>EASY MONEY PRO — أوفلاين</title></head>' +
        '<body style="background:#0b111e;color:#e8ecf4;font-family:Tahoma,Arial,sans-serif;text-align:center;padding-top:18vh;line-height:1.9">' +
        '<div style="font-size:1.4rem;color:#e3b64b;font-weight:800">أوفلاين — لا نسخة محفوظة بعد</div>' +
        '<div style="max-width:34rem;margin:12px auto">أول فتح للوحة يجب أن يكون بشبكة موجودة حتى تُحفظ نسخة العمل؛ أعد المحاولة عند عودة الاتصال.</div>' +
        '</body></html>',
        { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
  })());
});
