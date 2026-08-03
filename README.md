# ЗАМЧ Marketplace (site + backend)

Худалдан авагчийн вэб болон seller mobile app-д зориулсан FastAPI backend.

## Ажиллуулах

```sh
python3 -m pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --reload
```

## Production database

Local default: `sqlite:///./database.db`

Production жишээ:

```env
ENVIRONMENT=production
AUTO_CREATE_SCHEMA=0
DATABASE_URL=postgresql+psycopg://user:password@host:5432/zamch
SESSION_SECRET=...
CORS_ORIGINS=https://YOUR-SERVICE.up.railway.app,http://localhost:8081
```

`postgres://` / `postgresql://` (Railway) автоматаар `postgresql+psycopg://` болно.

Migration:

```sh
alembic upgrade head
```

## Railway дээр public API гаргах

1. Repo-г GitHub руу push хийнэ (`zamch-site`).
2. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** → энэ repo.
3. **Postgres** plugin нэмээд service-тэй холбоно.
4. Service → **Variables**:

```env
ENVIRONMENT=production
AUTO_CREATE_SCHEMA=0
SESSION_SECRET=<урт-random>
DATABASE_URL=${{Postgres.DATABASE_URL}}
BASE_URL=https://YOUR-SERVICE.up.railway.app
QPAY_CALLBACK_BASE=https://YOUR-SERVICE.up.railway.app
CORS_ORIGINS=https://YOUR-SERVICE.up.railway.app,http://localhost:8081
```

`BASE_URL`-ийг Public Domain гарсаны дараа яг тэр URL-ээр шинэчилнэ.

5. **Settings → Networking → Public Networking** → Generate domain.
6. **Volumes** (зөвлөмж): mount path `/app/photos` — барааны зураг restart-д устахгүй.
7. Deploy дууссаны дараа шалгана:

```sh
curl -sS https://YOUR-SERVICE.up.railway.app/api/categories
```

Амжилттай бол JSON category жагсаалт ирнэ.

Docker entrypoint (`docker-entrypoint.sh`) автоматаар `alembic upgrade head` хийгээд `uvicorn` асаана (`PORT` Railway өгнө).

### Seller app холбох

`zamch-app/eas.json` → `build.preview.env.EXPO_PUBLIC_API_URL` болон `.env`:

```env
EXPO_PUBLIC_API_URL=https://YOUR-SERVICE.up.railway.app
```

Дараа нь:

```sh
cd ../zamch-app
npm run eas:build:apk
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

## Админ самбар (`/admin`)

### Нэвтрэх

1. `.env` дээр тохируулна:
   ```env
   ADMIN_PHONE=99112233
   ADMIN_PASSWORD=your-secure-password
   ADMIN_NAME=ЗАМЧ Админ
   ```
2. Сервер **restart** — `bootstrap_admin()` admin хэрэглэгч үүсгэнэ/шинэчилнэ.
3. Хөтчөөр [`/login?next=/admin`](http://127.0.0.1:8000/login?next=/admin) нээнэ.
4. `ADMIN_PHONE` + `ADMIN_PASSWORD`-аар нэвтэрнэ.

Шууд: [`http://127.0.0.1:8000/admin`](http://127.0.0.1:8000/admin) — нэвтрээгүй бол заавар + «Нэвтрэх» холбоос гарна.

### Admin panel-д юу хийх вэ

- **Checklist** — хяналт хүлээлт, батлах бэлэн дэлгүүр, шинэ захиалга
- **Дэлгүүр** — утас/байршил баталгаажуулалт, батлах/идэвхгүй болгох
- **Хяналт** — seller publish илгээсэн бараа шалгах
- **Захиалга / Хүргэлт** — end-to-end flow cross-check
- **Бараа** — аппгүйгээр туршилтын бараа нэмэх

Role `admin` биш бол `/admin` хаагдана. Одоогийн admin-аар **Хэрэглэгч** tab-аас role өөрчилж болно.
