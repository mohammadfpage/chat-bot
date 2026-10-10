# وبهوک (Webhook) — راه‌اندازی، تست لوکال بدون SSL، و استقرار

این پروژه دو حالت اجرا دارد و با یک خط در `.env` بین آن‌ها جابه‌جا می‌شوید:

| `RUN_MODE` | چطور کار می‌کند | کی استفاده کن |
| --- | --- | --- |
| `polling` (پیش‌فرض) | long polling — ربات خودش از تلگرام آپدیت می‌کشد | تست لوکال، پشت VPN، بدون هیچ تنظیم شبکه |
| `webhook` | یک سرور HTTP محلی بالا می‌آید و آدرس آن با `setWebhook` در تلگرام ثبت می‌شود | سرور، تست واقعی مسیر وبهوک، آماده‌سازی برای استقرار |

هر دو حالت **در یک فرایند** هستند؛ هیچ کدی عوض نمی‌شود، فقط `.env`.

---

## ۱. تست لوکال بدون SSL

### چرا تلگرام به SSL نیاز دارد ولی شما نباید بسازیدش

تلگرام فقط آدرس‌های `https://` را برای وبهوک قبول می‌کند (خطای `400 bad
webhook: An HTTPS URL must be provided`) و فقط با پورت‌های **443، 80، 88 و
8443** وصل می‌شود. این یعنی خودِ وبهوک باید پشت یک TLS باشد — ولی **نیازی
نیست گواهی بسازید، نصب کنید یا پورتی را باز کنید**: کافی است جلوی سرور
محلیِ تماماً HTTP یک **تونل** بگذارید. تونل TLS را می‌سازد، تلگرام با آن
صحبت می‌کند و ترافیک را به سرور شما روی `127.0.0.1` می‌فرستد.

### مراحل (۲ دقیقه)

**قدم ۱ — تونل را روشن کنید** (یکی از این‌ها):

```powershell
# پیشنهادی: cloudflared — رایگان، بدون اکانت، آدرس https موقت می‌دهد
# دانلود: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://127.0.0.1:8080
```

خروجی چیزی شبیه این است:

```
https://random-words-123.trycloudflare.com
```

**قدم ۲ — آدرس را در `.env` بگذارید:**

```ini
RUN_MODE=webhook
WEBHOOK_BASE_URL=https://random-words-123.trycloudflare.com
```

`WEBHOOK_SECRET` را خالی بگذارید (در هر بار اجرا یک توکن تصادفی ساخته و با
تلگرام ثبت می‌شود) و پورت هم پیش‌فرض `8080` است که باید با تونل یکی باشد.

**قدم ۳ — ربات را استارت کنید:**

```powershell
$env:PYTHONIOENCODING='utf-8'; python bot.py
```

لاگ‌ها باید این‌ها را نشان دهند:

```
Webhook server listening on http://127.0.0.1:8080/telegram/webhook
Webhook registered: https://random-words-123.trycloudflare.com/telegram/webhook
```

الان می‌توانید در تلگرام برای ربات پیام بدهید و از سمت وبهوک جواب بگیرید.
آدرس `GET /health` هم برای تست اتصال باز است:

```powershell
curl http://127.0.0.1:8080/health     # → {"status":"ok"}
```

### نکات مهم

- **توکن امنیتی**: هر درخواستِ تلگرام هدر
  `X-Telegram-Bot-Api-Secret-Token` دارد و قبل از دست‌خوردن توسط هندلر‌ها
  بررسی می‌شود؛ درخواست بدون توکن/با توکن غلط → `401`.
- **هرگز `drop_pending_updates` روشن نمی‌شود.** آپدیت‌های جامانده (مثلاً
  `chosen_inline_result` که ریشهٔ کارت نجواست) در صف می‌مانند و بعد از
  استارت پخش می‌شوند.
- **جایگزین cloudflared**: `ngrok http 8080` هم کار می‌کند (اکانت لازم
  دارد). تونل را که بستید، URL عوض می‌شود — در `.env` آدرس جدید را بگذارید
  و ربات را ری‌استارت کنید تا `setWebhook` دوباره ثبت شود.
- **تست بدون تونل**: اگر فقط می‌خواهید مطمئن شوید سرور و مسیر درست بالا
  آمده، `RUN_MODE=webhook` بگذارید و بدون `WEBHOOK_BASE_URL` استارت کنید —
  ربات با پیام راهنما (EN/FA) خارج می‌شود. سرور هم بدون نصب هیچ گواهی‌ای
  بالاست.
- **برگشت به حالت عادی**: `RUN_MODE=polling` کنید؛ ربات در شروع، وبهوک
  قبلی را پاک می‌کند و مثل قبل long polling می‌کند.

---

## ۲. استقرار روی سرور

خودِ ربات هیچ فرقی با حالت لوکال ندارد؛ فقط `WEBHOOK_BASE_URL` باید یک
آدرس ثابت `https://` باشد. دو مسیر ساده:

### گزینه الف — cloudflared روی همان سرور (بدون دامنه و بدون SSL)

```bash
# نصب + اجرا به صورت سرویس (بدون اکانت هم می‌شود، ولی سرویسِ پایدار
# اکانت رایگان Cloudflare می‌خواهد)
cloudflared service install <TOKEN>   # یا یک named tunnel بسازید
```

تونل را به `http://127.0.0.1:8080` بزنید، آدرس ثابتِ tunnel را در
`WEBHOOK_BASE_URL` بگذارید. TLS، دامنه و پورت را Cloudflare می‌دهد.

### گزینه ب — دامنه + ریورس‌پراکسی

با nginx/caddy جلوی `127.0.0.1:8080` را بگیرید و `WEBHOOK_BASE_URL` را
آدرس دامنه بگذارید. اگر تلگرام مستقیم وصل می‌شود (بدون پراکسی/تونل)،
پورتِ بیرونی باید یکی از 443/80/88/8443 باشد.

### سرویس systemd

فایل `pixel-bot.service` بدون تغییر کار می‌کند (`RUN_MODE` از همان `.env`
خوانده می‌شود):

```bash
sudo cp pixel-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pixel-bot
journalctl -u pixel-bot -f
```

### چک‌لیست انتقال

1. `.env` را روی سرور کپی کنید (بدون تغییر، جز `WEBHOOK_BASE_URL`).
2. `python test_connection.py` — مطمئن شو راهِ اتصال به `api.telegram.org`
   (پراکسی یا مستقیم) کار می‌کند.
3. تونل/پراکسی را بالا بیاور، آدرس https را در `.env` بگذار.
4. `RUN_MODE=webhook` کن و سرویس را استارت کن.
5. `curl https://<domain>/health` → `{"status":"ok"}` و بعد یک پیام آزمایشی
   در تلگرام.

---

## ۳. تحقیق: آیا این پروژه روی Cloudflare اجرا می‌شود؟

(بررسی شده تا اکتبر ۲۰۲۶.)

### گزینه ۱ — Cloudflare Workers با زبان پایتون: ❌ برای این پروژه مناسب نیست

پایتونِ Workers از مدتی پیش اضافه شده (beta، با فلگ `python_workers`) و
روی Pyodide/WebAssembly اجرا می‌شود. ظرفیت‌ها واقعی است (aiohttp، pydantic،
httpx و... در پکیج‌های Pyodide هستند)، ولی برای این ربات چند مانع
ساختاری دارد:

| مانع | توضیح |
| --- | --- |
| **دیسک پایدار ندارد** | Worker فقط حافظهٔ isolate دارد و بعد از هر استراحت پاک می‌شود؛ `database.db` (aiosqlite) عملاً از بین می‌رود. باید کل دیتابیس به D1/Postgres منتقل شود — و `asyncpg` پایهٔ WebAssembly ندارد. |
| **فرایندِ طولانی‌مدت ندارد** | Workers فقط request/response است. لوپ‌های پس‌زمینهٔ پروژه (پاکسازی روزانهٔ retention، همگام‌سازی اعضا، انقضای چت، پاکسازی کیبورد) اصلاً قابل اجرا نیستند. |
| **حالتِ long polling منتفی است** | فقط webhook ممکن است؛ یعنی معماری «همیشه‌روشن» فعلی باید کاملاً بازنویسی شود. |
| **حافظهٔ مشترک ندارد** | `pair_map`/`search_queue` تطبیق چت، `MemoryStorage` برای FSM و roster در حافظه — همه به یک فرایندِ زنده وابسته‌اند؛ Workerها isolateهای بی‌,state هستند (باید به Durable Objects مهاجرت کنند). |
| **خودِ aiogram پشتیبانی رسمی ندارد** | در فهرست پکیج‌های پشتیبانی‌شده نیست؛ حجم بسته هم محدود است (۳MB رایگان / ۱۰MB پولی؛ افزایش به ۶۴MB در دست توسعه) و هنوز beta است. |

**جمع‌بندی:** مهاجرت به Workers یعنی بازنویسی دیتابیس، حافظه و بخش بزرگی
از معماری — تقریباً هزینهٔ یک پروژهٔ جدید با همان قابلیت‌ها.

### گزینه ۲ — Cloudflare Containers: ⏸ گزینهٔ اختیاری (مسیر فعلی نیست)

> **وضعیت فعلی:** استقرار فعلی یک Python Web Service معمولی (Render) است
> (`render.yaml` + `python bot.py`)؛ به Docker و Wrangler نیازی ندارد.
> فایل‌های Cloudflare Containers (`Dockerfile`، `wrangler.jsonc`،
> `worker/index.js`، `package.json`) برای مهاجرتِ آینده در
> `deploy/cloudflare/` نگه داشته شده‌اند — جزئیات در همان پوشه و بخش ۴
> فایل `README.md`. از همان ریشهٔ ریپو هم قابل اجراست:
> `npx wrangler deploy -c deploy/cloudflare/wrangler.jsonc`
> (`wrangler.jsonc` با `image_build_context: "../.."` از ریشهٔ ریپو بیلد می‌کند).
>
> ⚠️ **پلن رایگان کافی نیست:** طبق صفحهٔ قیمت‌گذاری Cloudflare (اوت ۲۰۲۶)،
> Containers فقط روی **Workers Paid (‏$5/ماه)** فعال است (`Free: N/A`). برای
> استقرار رایگان از مسیر Render یا «سرور + تونل» (گزینهٔ ۳ همین بخش) استفاده
> کنید. بقیهٔ این بخش همان تحلیل قبلی است که چرا SQLite به درد Container
> نمی‌خورد.

Containers (public beta) یک image داکر کاملِ پایتونی را روی شبکهٔ Cloudflare
اجرا می‌کند؛ یعنی همین کد، بدون تغییر، با همهٔ لوپ‌های پس‌زمینه. مانع
اصلی:

> **دیسک Container موقتی است.** هر بار که instance به خواب می‌رود، دیسک
> تازه می‌شود؛ `database.db` پاک خواهد شد. (Dokumenatsion Cloudflare:
> «All disk is ephemeral … Snapshots are coming soon».)

راه‌حل‌ها: انتقال به PostgreSQL/managed DB (که `requirements.txt` از قبل
`asyncpg` را دارد و طراحی برایش آماده است)، یا D1/R2. یعنی مهاجرتِ دیتابیس
همچنان لازم است، هرچند بقیهٔ کد دست‌نخورده می‌ماند. پیچیدگی استقرار هم
بیشتر از یک VPS ساده است.

### گزینه ۳ — توصیه: سرور معمولی + تونل/شبکهٔ Cloudflare ✅

سریع‌ترین و پایدارترین مسیر «بردن پروژه روی Cloudflare»:

1. ربات را هرجا که هست اجرا کن (همین لپ‌تاپ، یا یک VPS ارزان) — با همان
   `pixel-bot.service`.
2. وبهوک را از طریق **Cloudflare Tunnel** (cloudflared) بالا بیاورید: دامنه،
   TLS، CDN و محافظتِ DDoS را Cloudflare می‌دهد، بدون اینکه پورتی باز شود
   و بدون هیچ گواهی‌ای.
3. چون ترافیکِ خروجی هم از شبکهٔ Cloudflare (یا همان سرور) می‌رود، معمولاً
   به پراکسیِ جدا هم کمتر نیاز پیدا می‌کنید — همان `test_connection.py`
   تشخیص می‌دهد.

این مسیر همهٔ مزایای Cloudflare (HTTPS رایگان، بدون مدیریت گواهی، پایداری)
را دارد و **صفر تغییر کد** می‌خواهد. اگر بعداً Containers از مرحلهٔ beta
گذشت و دیتابیس را به PostgreSQL برده بودید، همان موقع می‌توانید کل
فرایند را به Container منتقل کنید — فایل‌های آن در `deploy/cloudflare/`
نگهداری می‌شوند (راهنما: `README.md` → بخش ۴ و
`deploy/cloudflare/README.md`).

---

## ۴. مرجع تنظیمات

| کلید | پیش‌فرض | توضیح |
| --- | --- | --- |
| `RUN_MODE` | `polling` | `polling` یا `webhook` (نام‌های اشتباه به polling برمی‌گردند) |
| `WEBHOOK_BASE_URL` | خالی | آدرس عمومی `https://` بدون اسلش انتهایی |
| `WEBHOOK_PATH` | `/telegram/webhook` | مسیر محلی؛ اسلش ابتدایی خودکار |
| `WEBHOOK_SECRET` | خالی | توکن امنیتی هدر (`A-Za-z0-9_-`)؛ خالی = تصادفی هر بار اجرا |
| `WEBAPP_HOST` | `127.0.0.1` | آدرس bind سرور محلی |
| `WEBAPP_PORT` | `8080` | پورت سرور محلی (تونل/پراکسی همین را می‌خواند) |

کدهای مربوطه در `bot.py`: `build_webhook_url`، `webhook_secret_token`،
`build_set_webhook_kwargs`، `build_webhook_app`، `run_webhook` — تست‌شده با
`smoke/smoke_webhook.py` (۳۵ بررسی، بدون نیاز به تلگرام).
