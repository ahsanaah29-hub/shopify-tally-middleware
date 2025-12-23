import json
import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01").strip()

USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "83.0"))
GST_PERCENT = float(os.getenv("GST_PERCENT", "18.0"))

# -------------------------------------------------
# Local storage (Webhook Orders)
# -------------------------------------------------
ORDERS_FILE = "orders.json"


def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return []


def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


def build_webhook_index():
    """Index webhook orders by Shopify order ID"""
    orders = load_orders()
    return {str(o.get("id")): o for o in orders}


# -------------------------------------------------
# Shopify → Middleware (Webhook)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()
    orders = load_orders()

    # prevent duplicates
    if not any(o.get("id") == data.get("id") for o in orders):
        orders.append(data)
        save_orders(orders)

    return {"status": "ok"}


# -------------------------------------------------
# GST helper
# -------------------------------------------------
def calculate_gst(amount_inr: float):
    gst_total = round((amount_inr * GST_PERCENT) / 100, 2)
    return {
        "cgst": round(gst_total / 2, 2),
        "sgst": round(gst_total / 2, 2),
        "igst": 0.0
    }


# -------------------------------------------------
# IST → UTC conversion
# -------------------------------------------------
def ist_date_to_utc_range(date_str: str):
    ist = timezone(timedelta(hours=5, minutes=30))

    start_ist = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=ist
    )
    end_ist = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=ist
    )

    return (
        start_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


# -------------------------------------------------
# Tally → Fetch Webhook Orders
# -------------------------------------------------
@app.post("/tally/orders")
async def tally_orders_post():
    orders = load_orders()
    tally_orders = []

    for order in orders:
        customer = order.get("customer") or {}

        customer_name = (
            f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()
            or "Unknown Customer"
        )

        items = []
        for li in order.get("line_items", []):
            qty = li.get("quantity", 0)
            rate_inr = round(float(li.get("price", 0)) * USD_TO_INR_RATE, 2)
            amount = round(qty * rate_inr, 2)

            items.append({
                "item_name": li.get("title"),
                "quantity": qty,
                "rate": rate_inr,
                "amount": amount,
                "gst": calculate_gst(amount)
            })

        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at", "")[:10],
            "customer": {
                "name": customer_name,
                "email": customer.get("email"),
                "phone": customer.get("phone")
            },
            "items": items,
            "total_amount": round(
                float(order.get("total_price", 0)) * USD_TO_INR_RATE, 2
            ),
            "currency": "INR",
            "source": "Shopify",
            "shopify_order_id": order.get("id")
        })

    return {"orders": tally_orders}


# -------------------------------------------------
# Shopify → Tally (Date Range, MERGED LOGIC)
# -------------------------------------------------
def build_shopify_orders_by_date(from_date: str, to_date: str):
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        raise HTTPException(500, "Shopify config missing")

    from_utc, _ = ist_date_to_utc_range(from_date)
    _, to_utc = ist_date_to_utc_range(to_date)

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
    )

    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    params = {
        "status": "any",
        "created_at_min": from_utc,
        "created_at_max": to_utc,
        "limit": 250
    }

    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        raise HTTPException(500, response.text)

    api_orders = response.json().get("orders", [])
    webhook_index = build_webhook_index()
    tally_orders = []

    for order in api_orders:
        order_id = str(order.get("id"))

        # 🔥 CUSTOMER FROM WEBHOOK (SOURCE OF TRUTH)
        if order_id in webhook_index:
            w = webhook_index[order_id]
            customer = w.get("customer") or {}
            billing = w.get("billing_address") or {}
            shipping = w.get("shipping_address") or {}

            customer_name = (
                f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()
                or billing.get("name")
                or shipping.get("name")
                or "Unknown Customer"
            )

            customer_email = (
                customer.get("email")
                or w.get("email")
            )

            customer_phone = (
                customer.get("phone")
                or billing.get("phone")
                or shipping.get("phone")
            )
        else:
            customer_name = "Unknown Customer"
            customer_email = None
            customer_phone = None

        # -------- ITEMS --------
        items = []
        for li in order.get("line_items", []):
            qty = li.get("quantity", 0)
            rate_inr = round(float(li.get("price", 0)) * USD_TO_INR_RATE, 2)
            amount = round(qty * rate_inr, 2)

            items.append({
                "item_name": li.get("title"),
                "quantity": qty,
                "rate": rate_inr,
                "amount": amount,
                "gst": calculate_gst(amount)
            })

        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at", "")[:10],
            "customer": {
                "name": customer_name,
                "email": customer_email,
                "phone": customer_phone
            },
            "items": items,
            "total_amount": round(
                float(order.get("total_price", 0)) * USD_TO_INR_RATE, 2
            ),
            "currency": "INR",
            "source": "Shopify",
            "shopify_order_id": order.get("id")
        })

    return {"orders": tally_orders}


@app.post("/tally/orders/shopify")
async def get_shopify_orders_post(request: Request):
    body = await request.json()
    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")

    return build_shopify_orders_by_date(from_date, to_date)


# -------------------------------------------------
# Tally → Shopify (Sales Push)
# -------------------------------------------------
@app.post("/tally/sales")
async def tally_sales(request: Request):
    data = await request.json()

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
    )

    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    line_items = []
    for item in data["items"]:
        price_usd = round(item["rate"] / USD_TO_INR_RATE, 2)
        line_items.append({
            "title": item["item_name"],
            "quantity": item["quantity"],
            "price": price_usd
        })

    payload = {
        "order": {
            "email": data["customer"].get("email"),
            "line_items": line_items,
            "financial_status": "paid"
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code not in (200, 201):
        raise HTTPException(500, response.text)

    return {
        "status": "success",
        "shopify_order_id": response.json()["order"]["id"]
    }
