# ربات چت ناشناس تلگرام

ربات ناشناسِ تلگرام با **aiogram 3** + **SQLAlchemy 2 (async)** — چت ۱-۱ تصادفی،
نجوا (پیام مخفی)، اقتصاد سکه‌ای و پنل ادمین. دو حالت اجرا دارد:
`polling` (لوکال، پیش‌فرض) و `webhook` (سرور/استقرار).

---

## ۱. اجرای لوکال

```powershell
# پیش‌نیاز: Python 3.12
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# تنظیمات را از نمونه بسازید و ویرایش کنید
copy .env.example .env

# اجرا (RUN_MODE=polling در .env)
$env:PYTHONIOENCODING='utf-8'; venv\Scripts\python bot.py
```

برای تست حالت webhook **بدون هیچ SSL/دامنه‌ای** (تونل `cloudflared`) و مرجع
کلیدهای `WEBHOOK_*` و `WEBAPP_*` → **[WEBHOOK.md](WEBHOOK.md)**.

---

## ۲. بررسی سلامت پروژه

```powershell
venv\Scripts\python smoke\verify.py
```

کامپایل کل ریپو، ایمپورت `bot` و همهٔ smoke ها (۱۱ مرحله) باید سبز شوند —
بدون نیاز به تلگرام، دیتابیس اصلی یا اینترنت. هر feature جدید باید smoke
خودش را در `smoke/` داشته باشد وگرنه `verify.py` آن را اجرا نمی‌کند.

---

## ۳. استقرار روی Render (مسیر فعلی)

مسیر فعلی یک **Python Web Service معمولی** است (Render, Railway, Fly, VPS…).
Render فقط `requirements.txt` را نصب می‌کند و خودِ `bot.py` را اجرا می‌کند —
**نه Docker، نه `npx wrangler deploy`، نه Node**. سرور webhook روی
`0.0.0.0` و پورتی که پلتفرم با `PORT` می‌دهد گوش می‌دهد و مسیر `/health`
را برای health-check باز می‌گذارد.

فایل استقرار: **`render.yaml`** (Blueprint). هیچ کلید واقعی‌ای داخلش نیست؛
همهٔ secret ها با `sync: false` به داشبورد Render واگذار شده‌اند.

### مقادیر Render

| فیلد | مقدار |
| --- | --- |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python bot.py` |
| Health Check Path | `/health` |
| Python | `3.12` (از طریق `PYTHON_VERSION=3.12.6`) |

### متغیرهای محیطی لازم در Render

غیر‌حساس (در `render.yaml` از قبل ست شده‌اند): `RUN_MODE=webhook`،
`WEBHOOK_PATH=/telegram/webhook`، `WEBAPP_HOST=0.0.0.0`، `LOG_FILE=` (خالی،
فقط stdout)، `GROUP_KEYBOARD_CLEANUP=false`.

محرمانه / مخصوصِ نمونه (در داشبورد Render → Environment بگذارید):

| کلید | توضیح |
| --- | --- |
| `BOT_TOKEN` | از @BotFather |
| `DATABASE_URL` | PostgreSQL مدیریت‌شده — `postgresql+asyncpg://user:pass@host:5432/db` (فرم `postgres://` هم خودکار اصلاح می‌شود) |
| `REDIS_URL` | اختیاری ولی توصیه‌شده: `rediss://user:pass@host:6379` تا وضعیت FSM از خواب/ری‌استارت جان سالم به در ببرد |
| `WEBHOOK_SECRET` | فقط `A-Za-z0-9_-`؛ خالی = در هر بوت یکی تصادفی ساخته می‌شود |
| `WEBHOOK_BASE_URL` | بعد از اولین deploy: `https://<service>.onrender.com` (بدون اسلش انتهایی) |
| `ADMIN_IDS` | آیدی‌های عددی ادمین، جدا با کاما |
| `CHANNEL_ID` / `CHANNEL_USERNAME` | برای عضویت اجباری |
| `PROXY_URL` | فقط اگر `api.telegram.org` فیلتر است |

> **مهم:** `PORT` را دستی ست نکنید — Render خودش می‌دهد و ربات همان را
> می‌خواند (`config.py`). فقط `WEBHOOK_BASE_URL` احتیاج به پر شدن دستی دارد؛
> تا وقتی خالی باشد ربات عمداً بالا نمی‌آید تا وبهوکِ خراب ثبت نشود.

### مراحل deploy

1. ریپو را به Render وصل کنید؛ `render.yaml` خودکار شناسایی می‌شود (یا
   یک Web Service از نوع Python بسازید و Build/Start بالا را بگذارید).
2. یک PostgreSQL مدیریت‌شده بسازید (Render Postgres / Neon / Supabase…) و
   رشتهٔ آن را در `DATABASE_URL` بگذارید.
3. `BOT_TOKEN` (و در صورت تمایل `REDIS_URL` و بقیه) را در Environment بگذارید.
4. اولین deploy را بزنید، سپس آدرس سرویس را در `WEBHOOK_BASE_URL` بگذارید و
   دوباره deploy کنید.
5. سلامت: `curl https://<service>.onrender.com/health` → `{"status":"ok"}` و
   بعد یک پیام آزمایشی در تلگرام.

### محدودیت‌های پلن رایگان (دور نزنید)

* سرویس رایگان Render بعد از ~۱۵ دقیقه بی‌کاری می‌خوابد. هنگام خواب،
  وبهوک‌های تلگرام پاسخ نمی‌گیرند و تلگرام مدتی دوباره تلاش می‌کند؛ با
  بیدار شدن دوباره، `rebuild_chat_state` صف/جفت‌ها را از PostgreSQL و
  وضعیت FSM را از Redis برمی‌گرداند. برای همیشه‌روشن بودن، پلن پولی لازم است.
* پلن رایگان **دیسک پایدار ندارد**؛ پس `DATABASE_URL` باید PostgreSQL باشد
  (SQLite با هر deploy/ری‌استارت پاک می‌شود — ربات در این حالت فقط هشدار
  می‌دهد، ولی داده از دست می‌رود). برای دور زدن این محدودیت هیچ ترافیک
  مصنوعی نفرستید.

### انتقال دیتابیس

* جدول‌ها در هر بار بالا آمدن ساخته می‌شوند (`init_db`)؛ یک PostgreSQL تازه
  نیازی به migrate دستی ندارد. هیچ جدولی drop نمی‌شود.
* داده‌های `database.db` لوکال منتقل نمی‌شوند؛ اگر لازم است، دستی export/import
  کنید.
* بکاپ در PostgreSQL: دکمهٔ «پشتیبان‌گیری» پنل ادمین فقط SQLite است و در حالت
  PostgreSQL راهنمای `pg_dump` نشان می‌دهد.

### عیب‌یابی

| علامت | علت محتمل |
| --- | --- |
| ربات بالا نمی‌آید / crash loop | `BOT_TOKEN` یا `DATABASE_URL` ست نشده، یا `WEBHOOK_BASE_URL` خالی است |
| `404` روی وبهوک | `WEBHOOK_BASE_URL` با آدرس سرویس یکی نیست، یا `WEBHOOK_PATH` عوض شده |
| داده بعد از مدتی ریست می‌شود | `DATABASE_URL` روی SQLite است (پلن رایگان دیسک ندارد) |
| وضعیت کاربران بعد از خواب ریست شد | `REDIS_URL` خالی/خراب است یا rebuild در لاگ خطا داده |
| پنل ادمین نیست | `ADMIN_IDS` خالی است |

---

## ۴. گزینهٔ اختیاری: Cloudflare Containers

استقرار Cloudflare **مسیر فعلی نیست**. فایل‌های آن (image داکر + Worker +
`wrangler`) برای مهاجرتِ احتمالیِ آینده جدا شده‌اند و در پوشهٔ
**`deploy/cloudflare/`** نگهداری می‌شوند تا هیچ پلتفرم پایتونی‌ای آن‌ها را
به‌اشتباه به‌عنوان پروژهٔ Node/Worker شناسایی نکند و `npx wrangler deploy`
اجرا نشود.

⚠️ **هیچ‌کدام از این فایل‌ها نباید به ریشهٔ ریپو برگردند** — وجود
`package.json` یا `wrangler.jsonc` در ریشه همان چیزی است که باعث اجرای
ناخواستهٔ `npx wrangler deploy` می‌شود. با این حال، **از همان ریشهٔ ریپو
هم می‌توان مستقیماً اجرا/استقرار کرد**؛ کافی است مسیر کانفیگ را با `-c`
بدهید (همهٔ مسیرهای نسبی داخلی نسبت به همان فایل حل می‌شوند، نه cwd):

```powershell
# یک‌بار: نصب ابزار Node در همان پوشه (نه در ریشه)
npm install --prefix deploy/cloudflare

# a) دو حالت معادل برای همهٔ دستورات:
npx wrangler dev    -c deploy/cloudflare/wrangler.jsonc   # لوکال (نیاز به Docker)
npx wrangler deploy -c deploy/cloudflare/wrangler.jsonc   # استقرار
npx wrangler tail   -c deploy/cloudflare/wrangler.jsonc   # لاگ زنده
npx wrangler secret put BOT_TOKEN -c deploy/cloudflare/wrangler.jsonc

# b) یا اسکریپت‌های آمادهٔ npm (بدون نیاز به -c؛ cwd خودش همان پوشه است):
npm --prefix deploy/cloudflare run cf:dev
npm --prefix deploy/cloudflare run cf:deploy
npm --prefix deploy/cloudflare run cf:tail
npm --prefix deploy/cloudflare run cf:secret -- BOT_TOKEN
npm --prefix deploy/cloudflare run cf:check   # اعتبارسنجی offline (dry-run)
```

`wrangler.jsonc` در بخش `containers` مقدار `"image_build_context": "../.."`
دارد؛ یعنی Docker با **context = ریشهٔ ریپو** ساخته می‌شود و `Dockerfile`
به `requirements.txt` و سورس ربات دسترسی دارد — دیگر نیازی به کپی‌کردن فایل‌ها
به ریشه نیست. `.dockerignore` ریشه جلوی ورود `.env`/`venv`/`*.db`/`node_modules`
به image را می‌گیرد.

### متغیرها و secret ها

| کلید | محل |
| --- | --- |
| `RUN_MODE`, `WEBHOOK_PATH`, `WEBAPP_*`, `ADMIN_IDS`, `LOG_*` | `wrangler.jsonc` → `vars` (غیرحساس) |
| `BOT_TOKEN`, `DATABASE_URL`, `WEBHOOK_SECRET`, `REDIS_URL`, `PROXY_URL` | `npx wrangler secret put <KEY>` (هرگز در git) |

برای `wrangler dev` لوکال، همان کلیدها از فایل **`deploy/cloudflare/.dev.vars`**
خوانده می‌شوند (git-ignored؛ الگو: `.dev.vars.example`). `DATABASE_URL` لوکال
می‌تواند SQLite باشد، ولی **در container واقعی دیسک موقتی است** و ربات با
`sqlite` عمداً بالا نمی‌آید؛ پس `DATABASE_URL` استقرار باید PostgreSQL باشد.

### ثبت وبهوک و رهاسازی

بعد از اولین deploy، آدرس `https://<worker>.workers.dev` را گرفته و با
کمک‌اسکریپت داخل ریپو ثبت کنید (خودش `allowed_updates` را از dispatcher
می‌سازد تا `chat_member` جا نیفتد):

```powershell
python deploy/cloudflare/set_webhook.py --url https://<worker>.workers.dev
python deploy/cloudflare/set_webhook.py --print-curl   # فقط نمایش دستور curl
python deploy/cloudflare/set_webhook.py --remove        # بازگشت به polling
```

### ⚠️ پلن رایگان و هزینه

تیم Cloudflare از **اوت ۲۰۲۶** صریحاً می‌گوید Containers فقط روی
**Workers Paid (‏$5/ماه)** فعال است (`Free: N/A`). پس «Cloudflare Workers
رایگان» برای این معماری **وجود ندارد**. اگر پلن رایگان می‌خواهید، از مسیر
**Render** (بخش ۳) یا سرور+تونل (بخش ۳ فایل `WEBHOOK.md`) استفاده کنید —
منطقِ کامل در `WEBHOOK.md` §۳ آمده است.

خلاصهٔ معماری: کانتینر `python:3.12` کل ربات را اجرا می‌کند (بدون VPS)،
`worker/index.js` فقط `POST WEBHOOK_PATH` و `GET /health` را به آن forward
می‌کند، و `max_instances` باید ۱ بماند (حالت matching در حافظهٔ فرایند است).
دیسک کانتینر موقتی است، پس دیتابیس همان PostgreSQL و FSM همان Redis است.
جزئیات و چک‌لیست → [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md).
