import json
import os
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# -------------------------------------------------
# Environment variables (Render / Local)
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "").strip()

# -------------------------------------------------
# Local storage (temporary)
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
# Shopify → Middleware (Webhook receiver)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()

    print("🔥 Order received from Shopify")
    print(json.dumps(data, indent=2))

    orders = load_orders()
    orders.append(data)
    save_orders(orders)

    return {"status": "ok"}


# -------------------------------------------------
# Tally → Fetch Shopify Orders
# -------------------------------------------------
@app.get("/tally/orders")
async def get_orders_for_tally():
    try:
        raw_orders = load_orders()
        tally_orders = []

        for order in raw_orders:
            # --- Safe total price extraction ---
            total_price = (
                order.get("total_price")
                or order.get("current_total_price")
                or 0
            )

            # --- Customer mapping ---
            customer = order.get("customer") or {}

            # --- Line items mapping ---
            items = []
            for li in order.get("line_items", []):
                qty = li.get("quantity", 0)
                price = float(li.get("price", 0))

                items.append({
                    "item_name": li.get("title"),
                    "quantity": qty,
                    "rate": price,
                    "amount": qty * price
                })

            tally_orders.append({
                "voucher_type": "Sales",
                "voucher_number": str(order.get("order_number")),
                "voucher_date": order.get("created_at", "")[:10],
                "customer": {
                    "name": customer.get("first_name", "") + " " + customer.get("last_name", ""),
                    "email": customer.get("email"),
                    "phone": customer.get("phone")
                },
                "items": items,
                "tax": {
                    "type": "GST",
                    "amount": float(order.get("total_tax", 0))
                },
                "total_amount": float(total_price),
                "currency": order.get("currency"),
                "source": "Shopify",
                "shopify_order_id": order.get("id")
            })

        # ✅ MAINSTREAM ARRAY WRAPPING
        return JSONResponse(
            content={"orders": tally_orders},
            status_code=200
        )

    except Exception as e:
        print("❌ Error building Tally orders:", str(e))
        raise HTTPException(status_code=500, detail="Failed to build Tally orders")




# -------------------------------------------------
# Helper: Push Order to Shopify
# -------------------------------------------------
def create_shopify_order(tally_data: dict):
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Shopify configuration missing"
        )

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
        line_items.append({
            "title": item["product_name"],
            "quantity": item["quantity"],
            "price": item["rate"]
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
        raise HTTPException(
            status_code=500,
            detail=f"Shopify error: {response.text}"
        )

    return response.json()


# -------------------------------------------------
# Tally → Middleware (Sales Voucher)
# -------------------------------------------------
@app.post("/tally/sales")
async def tally_sales(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    print("📥 Received Tally Sales Voucher:")
    print(json.dumps(data, indent=2))

    # Required fields validation
    required_fields = [
        "voucher_type",
        "voucher_number",
        "voucher_date",
        "customer",
        "items",
        "total_amount"
    ]

    for field in required_fields:
        if field not in data:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field: {field}"
            )

    # Validate items
    if not isinstance(data["items"], list) or len(data["items"]) == 0:
        raise HTTPException(
            status_code=400,
            detail="items must be a non-empty list"
        )

    # Push to Shopify
    shopify_response = create_shopify_order(data)

    return {
        "status": "success",
        "message": "Sales voucher pushed to Shopify",
        "received_items_count": len(data["items"]),
        "shopify_order_id": shopify_response["order"]["id"]
    }




