from fastapi import FastAPI, Request, HTTPException
import json

app = FastAPI()

@app.post("/tally/sales")
async def tally_sales(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Debug print (VERY IMPORTANT)
    print("📥 Received Tally Payload:")
    print(json.dumps(data, indent=2))

    # Basic validation (minimal)
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

    if not isinstance(data["items"], list) or len(data["items"]) == 0:
        raise HTTPException(
            status_code=400,
            detail="items must be a non-empty list"
        )

    return {
        "status": "success",
        "message": "Sales voucher received",
        "received_items_count": len(data["items"])
    }
