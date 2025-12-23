from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import requests
import os

app = FastAPI()

# ---------------- CONFIG ----------------
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")  # example: myshop.myshopify.com
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
SHOPIFY_API_VERSION = "2025-01"

SHOPIFY_ORDERS_URL = (
    f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
)

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

# ---------------- MODELS ----------------
class DateRange(BaseModel):
    from_date: str
    to_date: str


# ---------------- HELPERS ----------------
def get_customer_details(order: dict) -> dict:
    """
    Shopify-safe customer extraction
    """

    # 1️⃣ Billing address (BEST SOURCE)
    billing = order.get("billing_address")
    if billing:
        name = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
        if name:
            return {
                "name": name,
                "email": order.get("email"),
                "phone": billing.get("phone"),
            }

    # 2️⃣ Shipping address
    shipping = order.get("shipping_address")
    if shipping:
        name = f"{shipping.get('first_name', '')} {shipping.get('last_name', '')}".strip()
        if name:
            return {
                "name": name,
                "email": order.get("email"),
                "phone": shipping.get("phone"),
            }

    # 3️⃣ Customer object (NOT reliable alone)
    customer = order.get("customer")
    if customer:
        name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        if name:
            return {
                "name": name,
                "email": customer.get("email"),
                "phone": customer.get("phone"),
            }

    # 4️⃣ Email fallback
    if order.get("email"):
        return {
            "name": order["email"],
            "email": order["email"],
            "phone": None,
        }

    # 5️⃣ Final fallback
    return {
        "name": "Unknown Customer",
        "email": None,
        "phone": None,
    }


def fetch_shopify_orders(from_date: str, to_date: str) -> List[dict]:
    params = {
        "status": "any",
        "created_at_min": f"{from_date}T00:00:00",
        "created_at_max": f"{to_date}T23:59:59",
        "limit": 250,
    }

    response = requests.get(SHOPIFY_ORDERS_URL, headers=HEADERS, params=params)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Shopify API error: {response.text}",
        )

    return response.json().get("orders", [])


# ---------------- API ----------------
@app.post("/tally/orders/shopify")
def get_orders(payload: DateRange):
    orders = fetch_shopify_orders(payload.from_date, payload.to_date)

    result = []

    for order in orders:
        customer = get_customer_details(order)

        items = []
        for line in order.get("line_items", []):
            price = float(line.get("price", 0))
            gst = price * 0.18

            items.append({
                "item_name": line.get("name"),
                "quantity": line.get("quantity"),
                "rate": price,
                "amount": price,
                "gst": {
                    "cgst": gst / 2,
                    "sgst": gst / 2,
                    "igst": 0.0,
                },
            })

        result.append({
            "voucher_type": "Sales",
            "voucher_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at")[:10],
            "customer": customer,
            "items": items,
            "total_amount": float(order.get("total_price", 0)),
            "currency": order.get("currency"),
            "source": "Shopify",
            "shopify_order_id": order.get("id"),
        })

    return {"orders": result}
