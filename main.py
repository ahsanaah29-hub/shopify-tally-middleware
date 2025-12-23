from fastapi import FastAPI, HTTPException, Request
import requests
from datetime import datetime
import os

app = FastAPI()

# =========================
# CONFIG
# =========================
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
SHOPIFY_API_VERSION = "2025-10"

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json"
}


# =========================
# HELPERS
# =========================
def parse_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").isoformat()


def fetch_customer(customer_id: int):
    if not customer_id:
        return {
            "name": "Unknown Customer",
            "email": None,
            "phone": None
        }

    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/customers/{customer_id}.json"
    resp = requests.get(url, headers=HEADERS)

    if resp.status_code != 200:
        return {
            "name": "Unknown Customer",
            "email": None,
            "phone": None
        }

    customer = resp.json().get("customer", {})
    name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()

    return {
        "name": name if name else "Unknown Customer",
        "email": customer.get("email"),
        "phone": customer.get("phone")
    }


# =========================
# API
# =========================
@app.post("/tally/orders/shopify")
async def fetch_shopify_orders(request: Request):
    body = await request.json()
    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(status_code=400, detail="from_date and to_date are required")

    params = {
        "status": "any",
        "created_at_min": parse_date(from_date),
        "created_at_max": parse_date(to_date),
        "limit": 250
    }

    orders_url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    resp = requests.get(orders_url, headers=HEADERS, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch orders from Shopify")

    orders = resp.json().get("orders", [])
    result = []

    for order in orders:
        customer_info = None

        if order.get("customer"):
            cust = order["customer"]
            customer_info = {
                "name": f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip(),
                "email": cust.get("email"),
                "phone": cust.get("phone")
            }
        else:
            customer_info = fetch_customer(order.get("customer_id"))

        items = []
        for item in order.get("line_items", []):
            price = float(item.get("price", 0))
            qty = int(item.get("quantity", 1))
            amount = price * qty

            cgst = round(amount * 0.09, 2)
            sgst = round(amount * 0.09, 2)

            items.append({
                "item_name": item.get("name"),
                "quantity": qty,
                "rate": price,
                "amount": amount,
                "gst": {
                    "cgst": cgst,
                    "sgst": sgst,
                    "igst": 0.0
                }
            })

        result.append({
            "voucher_type": "Sales",
            "voucher_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at")[:10],
            "customer": customer_info,
            "items": items,
            "total_amount": float(order.get("total_price", 0)),
            "currency": order.get("currency"),
            "source": "Shopify",
            "shopify_order_id": order.get("id")
        })

    return {"orders": result}
