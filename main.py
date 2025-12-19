import json
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# -------------------------
# Storage (temporary JSON file)
# -------------------------
ORDERS_FILE = "orders.json"


def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return []


def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


# -------------------------
# Shopify → Middleware
# -------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()

    print("🔥 Order received from Shopify")
    print(json.dumps(data, indent=2))

    orders = load_orders()
    orders.append(data)
    save_orders(orders)

    return {"status": "ok"}


# -------------------------
# Tally → Fetch Orders
# -------------------------
@app.get("/tally/orders")
async def get_orders_for_tally():
    orders = load_orders()
    return JSONResponse(content=orders, status_code=200)


# -------------------------
# Tally → Middleware (Sales Voucher)
# -------------------------
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

    # (Optional) Save Tally data separately later
    # For now we just acknowledge receipt

    return {
        "status": "success",
        "message": "Sales voucher received",
        "received_items_count": len(data["items"])
    }
