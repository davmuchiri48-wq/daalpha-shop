# DaveTech Shop — E-commerce website

Online shop built with Python (Flask). Customers browse, add to cart and pay with
**M-Pesa (STK Push)** or **Pay on Delivery**. You manage everything from `/admin`.

## Features
- Home page with hot deals, categories, search and price sorting
- Product pages with discount badges, stock levels, "Buy now"
- Cart, delivery fee (free above a set amount), checkout
- M-Pesa STK Push — customer gets the PIN prompt; order turns **paid** automatically
- Order page with live payment status + order tracking (code + phone)
- Admin: dashboard (revenue, new orders, low stock), products (photo upload), categories, orders (change status; cancelling returns stock)
- Works well on phones

## Run on your computer
```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 — admin at http://localhost:5000/admin
(default login `admin@example.com` / `admin123` — change it with env vars!).
10 demo products are added on first run. Set `SEED_DEMO=0` to skip them.

## Deploy on Render
1. Push this folder to a new GitHub repo.
2. On Render: **New → Blueprint**, pick the repo (it reads `render.yaml` and creates the web service + a free PostgreSQL database).
3. Fill in the environment variables it asks for:
   - `ADMIN_EMAIL`, `ADMIN_PASSWORD` — your admin login
   - `BASE_URL` — your site address, e.g. `https://davetech-shop.onrender.com` (needed for M-Pesa callbacks)
4. Optional: add your own domain in Render → Settings → Custom Domains.

> Use the PostgreSQL database on Render. Without `DATABASE_URL` the app uses SQLite, which Render wipes on every redeploy.

## Turn on M-Pesa
1. Create an app at https://developer.safaricom.co.ke (Lipa Na M-Pesa Online).
2. Set these env vars on Render:

| Variable | Value |
|---|---|
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | from your Daraja app |
| `MPESA_SHORTCODE` | Paybill number (sandbox: `174379`) |
| `MPESA_PASSKEY` | Lipa Na M-Pesa passkey |
| `MPESA_ENV` | `sandbox` for testing, `production` when live |
| `MPESA_TYPE` | `CustomerPayBillOnline` (Paybill) or `CustomerBuyGoodsOnline` (Till) |
| `MPESA_TILL` | your Till number, only for Buy Goods |

The M-Pesa option appears at checkout only when all keys and `BASE_URL` are set.
Until then customers see Pay on Delivery only.

## Other settings
`STORE_NAME`, `STORE_PHONE`, `CURRENCY` (default `KSh`), `DELIVERY_FEE` (default 200),
`FREE_DELIVERY_OVER` (default 5000).

## Connect Davetech Importer
Davetech Importer publishes AliExpress products here and reads orders back.
1. On Render, add `IMPORTER_API_KEY` — a long random secret (at least 32 characters).
2. In Davetech Importer → Integrations, enter this shop's address and the same key.

The importer talks to `/api/integration/v1/*` (health, categories, products, orders).
Requests without the key are refused. M-Pesa payments are now double-checked with
Safaricom (STK Query) and the paid amount before an order is marked paid.
