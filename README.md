# TriClean Supply — ordering portal

A B2B ordering site for TriClean Supply:

- **Public site** — homepage, product catalog (no prices), and a Contact us form.
  There's no public sign-up: accounts are invite-only, created by staff.
- **Customer portal** — each customer company has its own logins and sees only
  its own prices, including bulk tiers. Customers order on account (invoice) and
  can see past orders, print invoices, and reorder.
- **Backend** (`/admin`) — staff manage products, standard and per-customer
  prices, customers and their logins, warehouse stock, and orders. Shipping an
  order deducts stock from the warehouse chosen for each line.

## How pricing works

Each product has a **standard price**. On top of that you can add:

- **Standard bulk tiers** — e.g. 10+ for $480, for everyone.
- **A customer's own price** — e.g. Customer A pays $500, Customer B pays $450.
  Set it on the customer's **Prices** page, or per product.
- **A customer's own bulk tiers** — set on the product page.
- **A blanket discount %** for a customer — applies to every product they
  don't have their own price for.

A customer always pays the **lower** of their own price and the standard
price (after their discount) for the quantity they order.

## Getting his data in

His prices live in a spreadsheet. In Excel or Google Sheets, save each sheet
as **CSV** and upload it on **Backend → Import / export**:

1. **Products** — `sku, name, base_price` (+ optional `category, brand, unit, description, image_url, active`)
2. **Prices** — `customer, sku, min_qty, price` (blank customer = standard list)
3. **Inventory** — `warehouse, sku, quantity` (+ optional `reorder_point`)

Re-uploading updates rows in place (matched by SKU). If any row has a problem,
nothing from that file is applied, and the page lists what to fix. Exporting
first and editing the export is the easiest way to get the format right.

Add customers (Backend → Customers) and warehouses before importing prices or
inventory that refer to them.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python seed.py      # optional: demo data, empty database only
.venv/bin/python wsgi.py      # http://localhost:5000 (PORT=5055 to change)
.venv/bin/python -m pytest -q
```

Demo logins created by `seed.py` are listed at the top of that file.

## Deploy (Render + Neon, same as job-agent)

1. Push this folder to its own GitHub repo.
2. Create a free Postgres database on [neon.tech](https://neon.tech) and copy its connection string.
3. On Render: **New + → Blueprint**, pick the repo. Set `DATABASE_URL` (Neon),
   `PUBLIC_BASE_URL` (the Render URL, later the real domain), and `COMPANY_EMAIL`.
4. Create the first staff login from Render's **Shell** tab:
   ```bash
   flask --app wsgi create-admin owner@example.com --name "Owner Name"
   ```
   It prints a one-time link to set the password.
5. To use tricleansupply.com: add it under the Render service's **Custom
   domains**, then update the DNS records in GoDaddy as Render instructs.

Render's free plan sleeps after inactivity (the first visit takes ~30s to
wake up). For a site customers order from, the $7/month plan avoids that.

## Logins

Accounts are invite-only, and there's no automatic email yet. Staff add a
customer (Backend → Customers → Add & invite a customer), optionally entering
the first person's email right there, and get a one-time link to send them.
They open it and choose their own password. More logins can be added on the
customer's page; "Password reset link" works the same way.

Messages from the Contact us page land in Backend → Messages. **Create
customer** on a message pre-fills the account and invite from it.

## Not built yet

- Card payments (Stripe) — orders from card customers are recorded, and staff
  collect payment manually for now.
- Automatic emails (order confirmations, invites, low-stock alerts).
- Tax and delivery charges on invoices.
- QuickBooks sync.
