# CarHub Marketplace (site + backend)

Худалдан авагчийн вэб болон CarHub Seller mobile app-д зориулсан FastAPI backend.

## Ажиллуулах

```sh
python3 -m pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --reload
```

## Brand (CarHub)

Default нэр/logo кодонд суусан. Override:

```env
BRAND_NAME=CarHub
BRAND_TAGLINE=дугуй · обуд
BRAND_TITLE=CarHub — Дугуй, обуд
```

Static logo: `static/logo.png`, favicon: `static/favicon.png`

## Production database

Local default: `sqlite:///./database.db`

Production жишээ:

```env
ENVIRONMENT=production
AUTO_CREATE_SCHEMA=0
DATABASE_URL=postgresql+psycopg://user:password@host:5432/carhub
SESSION_SECRET=...
CORS_ORIGINS=https://YOUR-SERVICE.onrender.com,http://localhost:8081
BRAND_NAME=CarHub
```

Migration:

```sh
alembic upgrade head
```

## Render дээр deploy

1. Repo-г GitHub руу push хийнэ.
2. [render.com](https://render.com) → **New Web Service** → repo сонгоно.
3. **Environment Variables** (жишээ):

```env
ENVIRONMENT=production
AUTO_CREATE_SCHEMA=0
SESSION_SECRET=<openssl rand -hex 32>
DATABASE_URL=<postgres-url>
BASE_URL=https://YOUR-SERVICE.onrender.com
QPAY_CALLBACK_BASE=https://YOUR-SERVICE.onrender.com
CORS_ORIGINS=https://YOUR-SERVICE.onrender.com,http://localhost:8081
BRAND_NAME=CarHub
BRAND_TAGLINE=дугуй · обуд
SMS_SENDER=CARHUB
```

4. Deploy дууссаны дараа:

```sh
curl -sS https://YOUR-SERVICE.onrender.com/api/categories
```

### carhub.mn domain (дараа)

Render → **Custom Domains** → `carhub.mn` нэмээд DNS заавар дагана. Дараа нь `BASE_URL`, `CORS_ORIGINS`, app `EXPO_PUBLIC_API_URL`-ийг `https://carhub.mn` болгоно.

### Seller app холбох

`zamch-app/.env`:

```env
EXPO_PUBLIC_API_URL=https://YOUR-SERVICE.onrender.com
```

## Integration урсгал

1. Seller app → `/api/mobile/*` bearer token
2. Бараа бүртгэх → hybrid moderation → `published`
3. Web marketplace → зөвхөн `published` бараа
4. Захиалга → нөөц `reserved` → seller батлах → `committed`
5. Хугацаа (`ORDER_CONFIRM_MINUTES`) дуусвал автоматаар цуцлагдаж нөөц буцна

## Test

```sh
python3 -m pytest -q
```
