import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

ORDERS_FILE = "orders.json"


# -------------------------
# Utility: Save raw Shopify order
# -------------------------
def save_order(order_data: dict):
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            orders = json.load(f)
    else:
        orders = []

    orders.append(order_data)

    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


# -------------------------
# Transform Shopify → Tally Voucher
# -------------------------
def shopify_to_tally_voucher(order: dict) -> dict:
    customer = order.get("customer") or {}

    items = []
    for item in order.get("line_items", []):
        items.append({
            "product_name": item.get("name"),
            "quantity": item.get("quantity"),
            "rate": float(item.get("price", 0))
        })

    return {
        "voucher_type": "Sales",
        "voucher_number": order.get("name"),  # e.g. #1006
        "voucher_date": order.get("created_at", "")[:10],
        "customer": {
            "name": f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip(),
            "email": order.get("email"),
            "phone": customer.get("phone")
        },
        "items": items,
        "tax": float(order.get("total_tax", 0)),
        "total_amount": float(order.get("total_price", 0))
    }


# -------------------------
# Shopify Webhook Endpoint
# -------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()

    print("🔥 Order received from Shopify")
    print(data)

    save_order(data)

    return {"status": "ok"}


# -------------------------
# Tally-ready Endpoint
# -------------------------
@app.get("/tally/vouchers")
async def get_vouchers_for_tally():
    if not os.path.exists(ORDERS_FILE):
        return JSONResponse(content={"vouchers": []}, status_code=200)

    with open(ORDERS_FILE, "r") as f:
        orders = json.load(f)

    vouchers = [shopify_to_tally_voucher(order) for order in orders]

    return JSONResponse(content={"vouchers": vouchers}, status_code=200)
