import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException
from supabase import create_client

app = FastAPI()

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01").strip()

USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "83.0"))
GST_PERCENT = float(os.getenv("GST_PERCENT", "18.0"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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
# Shopify → Middleware (Webhook → Supabase)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    order = await request.json()

    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or {}

    customer_name = (
        f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()
        or billing.get("name")
        or shipping.get("name")
        or "Unknown Customer"
    )

    customer_email = (
        customer.get("email")
        or order.get("email")
        or billing.get("email")
    )

    customer_phone = (
        customer.get("phone")
        or billing.get("phone")
        or shipping.get("phone")
    )

    # ---- Insert Order (idempotent) ----
    res = supabase.table("orders").upsert({
        "shopify_order_id": order.get("id"),
        "order_number": str(order.get("order_number")),
        "voucher_date": order.get("created_at")[:10],
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "total_amount": float(order.get("total_price", 0)) * USD_TO_INR_RATE,
        "currency": "INR",
        "source": "Shopify",
        "raw_order": order
    }, on_conflict="shopify_order_id").execute()

    order_id = res.data[0]["id"]

    # ---- Replace Items ----
    supabase.table("order_items") \
        .delete() \
        .eq("order_id", order_id) \
        .execute()

    for li in order.get("line_items", []):
        qty = li.get("quantity", 0)
        rate = float(li.get("price", 0)) * USD_TO_INR_RATE
        amount = qty * rate
        gst = calculate_gst(amount)

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "quantity": qty,
            "rate": round(rate, 2),
            "amount": round(amount, 2),
            "cgst": gst["cgst"],
            "sgst": gst["sgst"],
            "igst": gst["igst"]
        }).execute()

    return {"status": "stored"}

# -------------------------------------------------
# Tally → Fetch Orders (DATE RANGE → Supabase)
# -------------------------------------------------
@app.post("/tally/orders")
async def tally_orders_post(request: Request):
    body = await request.json()
    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")

    res = supabase.table("orders") \
        .select("*, order_items(*)") \
        .gte("voucher_date", from_date) \
        .lte("voucher_date", to_date) \
        .order("voucher_date") \
        .execute()

    tally_orders = []

    for o in res.data:
        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            "customer": {
                "name": o["customer_name"],
                "email": o["customer_email"],
                "phone": o["customer_phone"]
            },
            "items": [{
                "item_name": i["item_name"],
                "quantity": i["quantity"],
                "rate": i["rate"],
                "amount": i["amount"],
                "gst": {
                    "cgst": i["cgst"],
                    "sgst": i["sgst"],
                    "igst": i["igst"]
                }
            } for i in o["order_items"]],
            "total_amount": o["total_amount"],
            "currency": o["currency"],
            "source": o["source"],
            "shopify_order_id": o["shopify_order_id"]
        })

    return {"orders": tally_orders}

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
    product_name = item.get("product_name") or item.get("item_name")

    price_usd = round(item["rate"] / USD_TO_INR_RATE, 2)

    line_items.append({
        "title": product_name,
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


