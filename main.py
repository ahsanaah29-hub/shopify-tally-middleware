import json
import os
from fastapi import FastAPI, Request

app = FastAPI()

ORDERS_FILE = "orders.json"


def save_order(order_data: dict):
    # If file exists, load existing orders
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            orders = json.load(f)
    else:
        orders = []

    # Append new order
    orders.append(order_data)

    # Save back to file
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()

    print("🔥 Order received from Shopify")
    print(data)

    save_order(data)

    return {"status": "ok"}

from fastapi.responses import JSONResponse

@app.get("/tally/orders")
async def get_orders_for_tally():
    if not os.path.exists(ORDERS_FILE):
        return JSONResponse(content=[], status_code=200)

    with open(ORDERS_FILE, "r") as f:
        orders = json.load(f)

    return JSONResponse(content=orders, status_code=200)


