import json
import os
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "").strip()

USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "83.0"))
GST_PERCENT = float(os.getenv("GST_PERCENT", "18.0"))

# -------------------------------------------------
# Local storage
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


# -------------------------------------------------
# Shopify → Middleware (Webhook)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()
    orders = load_orders()
    orders.append(data)
    save_orders(orders)
    return {"status": "ok"}


# -------------------------------------------------
# Helper: GST calculation
# -------------------------------------------------
def calculate_gst(amount_inr: float):
    gst_total = round((amount_inr * GST_PERCENT) / 100, 2)
    return {
        "cgst": round(gst_total / 2, 2),
        "sgst": round(gst_total / 2, 2),
        "igst": 0.0
    }


# -------------------------------------------------
# Core logic reused by GET & POST (TDL SAFE)
# -------------------------------------------------
def build_tally_orders():
    orders = load_orders()
    tally_orders = []

    for order in orders:
        customer = order.get("customer") or {}
        name = (
            f"{customer.get('first_name','')} {customer.get('last_name','')}"
            .strip() or "Unknown Customer"
        )

        items = []
        for li in order.get("line_items", []):
            qty = li.get("quantity") or 0
            rate_usd = float(li.get("price") or 0)
            rate_inr = round(rate_usd * USD_TO_INR_RATE, 2)
            amount = round(qty * rate_inr, 2)

            items.append({
                "item_name": li.get("title"),
                "quantity": qty,
                "rate": rate_inr,
                "amount": amount,
                "gst": calculate_gst(amount)
            })

        total_inr = round(float(order.get("total_price") or 0) * USD_TO_INR_RATE, 2)

        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at", "")[:10],
            "customer": {
                "name": name,
                "email": customer.get("email"),
                "phone": customer.get("phone")
            },
            "items": items,
            "total_amount": total_inr,
            "currency": "INR",
            "source": "Shopify",
            "shopify_order_id": order.get("id")
        })

    return {"orders": tally_orders}


# -------------------------------------------------
# Tally → Fetch Orders (GET)
# -------------------------------------------------
@app.get("/tally/orders")
async def get_orders_for_tally():
    return build_tally_orders()


# -------------------------------------------------
# ✅ Tally → Fetch Orders (POST) [TDL FIX]
# -------------------------------------------------
@app.post("/tally/orders")
async def get_orders_for_tally_post():
    return build_tally_orders()


# -------------------------------------------------
# Helper: Push Order to Shopify (INR → USD)
# -------------------------------------------------
def create_shopify_order(tally_data: dict):
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        raise HTTPException(status_code=500, detail="Shopify configuration missing")

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
    )

    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    line_items = []
    for item in tally_data["items"]:
        price_usd = round(item["rate"] / USD_TO_INR_RATE, 2)
        line_items.append({
            "title": item["product_name"],
            "quantity": item["quantity"],
            "price": price_usd
        })

    payload = {
        "order": {
            "email": tally_data["customer"].get("email"),
            "line_items": line_items,
            "financial_status": "paid",
            "note": f"Created from Tally | {tally_data['voucher_number']}"
        }
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()


# -------------------------------------------------
# Tally → Shopify (POST)
# -------------------------------------------------
@app.post("/tally/sales")
async def tally_sales(request: Request):
    data = await request.json()
    shopify_response = create_shopify_order(data)

    return {
        "status": "success",
        "message": "Sales voucher pushed to Shopify",
        "received_items_count": len(data["items"]),
        "shopify_order_id": shopify_response["order"]["id"]
    }


# -------------------------------------------------
# Core logic: Shopify → Tally (Date Range)
# -------------------------------------------------
def build_shopify_orders_by_date(from_date: str, to_date: str):
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        raise HTTPException(status_code=500, detail="Shopify configuration missing")

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
    )

    params = {
        "status": "any",
        "created_at_min": f"{from_date}T00:00:00Z",
        "created_at_max": f"{to_date}T23:59:59Z",
        "limit": 250
    }

    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    orders = response.json().get("orders", [])
    tally_orders = []

    for order in orders:
        items = []
        for li in order.get("line_items", []):
            qty = li.get("quantity") or 0
            rate_inr = round(float(li.get("price") or 0) * USD_TO_INR_RATE, 2)
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
            "items": items,
            "total_amount": round(float(order.get("total_price") or 0) * USD_TO_INR_RATE, 2),
            "currency": "INR",
            "source": "Shopify",
            "shopify_order_id": order.get("id")
        })

    return {"orders": tally_orders}


# -------------------------------------------------
# Shopify → Tally (GET)
# -------------------------------------------------
@app.get("/tally/orders/shopify")
async def get_shopify_orders_by_date(from_date: str, to_date: str):
    return build_shopify_orders_by_date(from_date, to_date)


# -------------------------------------------------
# ✅ Shopify → Tally (POST) [TDL FIX]
# -------------------------------------------------
@app.post("/tally/orders/shopify")
async def get_shopify_orders_by_date_post(request: Request):
    body = await request.json()

    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(
            status_code=400,
            detail="from_date and to_date are required"
        )

    return build_shopify_orders_by_date(from_date, to_date)
