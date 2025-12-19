import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# -----------------------------
# Utility: Validate input
# -----------------------------
def validate_tally_payload(payload: dict):
    required_fields = [
        "voucher_type",
        "voucher_number",
        "voucher_date",
        "party",
        "items",
        "total_amount"
    ]

    for field in required_fields:
        if field not in payload:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field: {field}"
            )

    if not isinstance(payload["items"], list) or len(payload["items"]) == 0:
        raise HTTPException(
            status_code=400,
            detail="Items must be a non-empty array"
        )


# -----------------------------
# Utility: Transform to Shopify
# -----------------------------
def transform_to_shopify_order(payload: dict):
    line_items = []

    for item in payload["items"]:
        line_items.append({
            "title": item.get("item_name"),
            "quantity": item.get("quantity"),
            "price": item.get("rate")
        })

    shopify_order = {
        "order": {
            "name": payload["voucher_number"],
            "processed_at": payload["voucher_date"],
            "email": payload["party"].get("email"),
            "customer": {
                "first_name": payload["party"].get("name")
            },
            "line_items": line_items,
            "total_price": payload["total_amount"],
            "financial_status": "paid"
        }
    }

    return shopify_order


# -----------------------------
# API: Receive Tally Sales Data
# -----------------------------
@app.post("/tally/sales")
async def receive_tally_sales(payload: dict):
    # 1️⃣ Validate
    validate_tally_payload(payload)

    # 2️⃣ Transform
    shopify_payload = transform_to_shopify_order(payload)

    # 3️⃣ Log (for testing & handover)
    print("✅ Tally payload received")
    print(json.dumps(payload, indent=2))

    print("🔄 Transformed Shopify payload")
    print(json.dumps(shopify_payload, indent=2))

    # 4️⃣ Return transformed data (for Postman test)
    return JSONResponse(
        content={
            "status": "success",
            "shopify_payload": shopify_payload
        },
        status_code=200
    )
