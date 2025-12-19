import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()


# -----------------------------
# Normalize input (single / multiple)
# -----------------------------
def normalize_vouchers(payload: dict):
    if "vouchers" in payload and isinstance(payload["vouchers"], list):
        return payload["vouchers"]
    else:
        return [payload]


# -----------------------------
# Validate one voucher
# -----------------------------
def validate_voucher(voucher: dict):
    required_fields = [
        "voucher_type",
        "voucher_number",
        "voucher_date",
        "party",
        "items",
        "total_amount"
    ]

    for field in required_fields:
        if field not in voucher:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field: {field}"
            )

    if not isinstance(voucher["items"], list) or len(voucher["items"]) == 0:
        raise HTTPException(
            status_code=400,
            detail="Items must be a non-empty array"
        )


# -----------------------------
# Transform one voucher to Shopify
# -----------------------------
def transform_to_shopify_order(voucher: dict):
    line_items = []

    for item in voucher["items"]:
        line_item = {
            "title": item.get("item_name"),
            "quantity": item.get("quantity"),
            "price": item.get("rate")
        }

        # SKU is optional
        if item.get("sku"):
            line_item["sku"] = item["sku"]

        # GST handling
        tax_lines = []

        tax = item.get("tax", {})
        if tax.get("igst", 0) > 0:
            tax_lines.append({
                "title": "IGST",
                "rate": tax["igst"] / 100
            })
        else:
            if tax.get("cgst", 0) > 0:
                tax_lines.append({
                    "title": "CGST",
                    "rate": tax["cgst"] / 100
                })
            if tax.get("sgst", 0) > 0:
                tax_lines.append({
                    "title": "SGST",
                    "rate": tax["sgst"] / 100
                })

        if tax_lines:
            line_item["tax_lines"] = tax_lines

        line_items.append(line_item)

    shopify_order = {
        "order": {
            "name": voucher["voucher_number"],
            "processed_at": voucher["voucher_date"],
            "email": voucher["party"].get("email"),
            "customer": {
                "first_name": voucher["party"].get("name")
            },
            "line_items": line_items,
            "total_price": voucher["total_amount"],
            "financial_status": "paid"
        }
    }

    return shopify_order


# -----------------------------
# API: Receive Tally Sales Data
# -----------------------------
@app.post("/tally/sales")
async def receive_tally_sales(payload: dict):
    vouchers = normalize_vouchers(payload)

    processed = []
    skipped = []

    for voucher in vouchers:
        validate_voucher(voucher)

        # Process only Sales vouchers
        if voucher["voucher_type"].lower() != "sales":
            skipped.append({
                "voucher_number": voucher["voucher_number"],
                "reason": "Unsupported voucher type"
            })
            continue

        shopify_payload = transform_to_shopify_order(voucher)

        print("🔄 Transformed Shopify Payload")
        print(json.dumps(shopify_payload, indent=2))

        processed.append({
            "voucher_number": voucher["voucher_number"],
            "shopify_payload": shopify_payload
        })

    return JSONResponse(
        content={
            "status": "completed",
            "processed_count": len(processed),
            "skipped_count": len(skipped),
            "processed": processed,
            "skipped": skipped
        },
        status_code=200
    )
