import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import requests
from supabase import create_client

app = FastAPI()

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =================================================
# Helper Functions
# =================================================

def extract_order_number_from_note(note: str):
    """
    Extract order number from notes like:
    - "This is an exchange order against #184055"
    - "This is a redispatch order against #184055"
    """
    if not note:
        return None
    match = re.search(r'against\s+#?(\d+)', note, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def determine_order_type(order):
    """
    Determine order type: 'sales', 'cancelled', 'return', 'exchange', or 'redispatch'
    """
    cancel_reason = order.get("cancel_reason")
    cancelled_at = order.get("cancelled_at")
    if cancel_reason or cancelled_at:
        return "cancelled", None

    note = (order.get("note") or "").lower()
    if "exchange order against" in note:
        against_id = extract_order_number_from_note(order.get("note", ""))
        return "exchange", against_id

    if "redispatch order against" in note:
        against_id = extract_order_number_from_note(order.get("note", ""))
        return "redispatch", against_id

    tags = (order.get("tags") or "").lower()
    if "return" in tags or "returned" in tags:
        return "return", None

    fulfillments = order.get("fulfillments") or []
    for f in fulfillments:
        status = (f.get("status") or "").lower()
        if status == "returned" or "return" in status:
            return "return", None

    return "sales", None


def determine_payment_method(order):
    """
    Determine if order is COD or Prepaid.
    """
    gateway = (order.get("gateway") or "").lower()
    if "cash on delivery" in gateway or "cod" in gateway:
        return "COD"

    financial_status = (order.get("financial_status") or "").lower()
    if financial_status == "pending":
        return "COD"

    payment_gateway_names = order.get("payment_gateway_names", [])
    for pg in payment_gateway_names:
        if "cash" in pg.lower() or "cod" in pg.lower():
            return "COD"

    return "Prepaid"


def determine_delivery_channel(order):
    """
    Identify delivery channel from tags, fulfillments, shipping lines, or notes.
    """
    # 1. Order tags
    tags = (order.get("tags") or "").lower()
    if "dtdc" in tags or "carrier:dtdc" in tags:
        return "DTDC"
    if "delhivery" in tags or "carrier:delhivery" in tags:
        return "Delhivery"
    if "bluedart" in tags or "blue dart" in tags or "carrier:bluedart" in tags:
        return "BlueDart"

    # 2. Fulfillments
    fulfillments = order.get("fulfillments") or []
    for f in fulfillments:
        tracking_company = (f.get("tracking_company") or "").strip().lower()
        tracking_url = (f.get("tracking_url") or "").strip().lower()

        if "dtdc" in tracking_company or "dtdc" in tracking_url:
            return "DTDC"
        if "delhivery" in tracking_company or "delhivery" in tracking_url:
            return "Delhivery"
        if "bluedart" in tracking_company or "blue dart" in tracking_company or "bluedart" in tracking_url:
            return "BlueDart"

    # 3. Shipping lines
    shipping_lines = order.get("shipping_lines") or []
    for s in shipping_lines:
        code = (s.get("code") or "").lower()
        title = (s.get("title") or "").lower()
        if "dtdc" in code or "dtdc" in title:
            return "DTDC"
        if "delhivery" in code or "delhivery" in title:
            return "Delhivery"
        if "bluedart" in code or "blue dart" in code or "bluedart" in title:
            return "BlueDart"

    # 4. Note
    note = (order.get("note") or "").lower()
    if "dtdc" in note:
        return "DTDC"
    if "delhivery" in note:
        return "Delhivery"
    if "bluedart" in note or "blue dart" in note:
        return "BlueDart"

    return "Pending"


def get_voucher_date_ist(created_at_str: str) -> str:
    """
    Convert Shopify UTC timestamp to IST YYYY-MM-DD
    """
    if not created_at_str:
        return ""
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        dt_ist = dt.astimezone(ist_offset)
        return dt_ist.strftime("%Y-%m-%d")
    except Exception:
        return created_at_str[:10]


# =================================================
# Process and Save Order to Supabase
# =================================================

def save_shopify_order(order: dict):
    """
    Core function to process and upsert an order into Supabase.
    """
    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or {}

    first_name = customer.get("first_name")
    last_name = customer.get("last_name")

    if first_name or last_name:
        customer_name = f"{first_name or ''} {last_name or ''}".strip()
    else:
        customer_name = (
            billing.get("name")
            or shipping.get("name")
            or customer.get("email")
            or "Unknown Customer"
        )

    customer_email = customer.get("email") or order.get("email") or billing.get("email")
    customer_phone = customer.get("phone") or billing.get("phone") or shipping.get("phone")

    total_with_gst = float(order.get("total_price", 0))
    total_gst = float(order.get("total_tax", 0))
    total_ex_gst = round(total_with_gst - total_gst, 2)

    shipping_lines = order.get("shipping_lines", [])
    shipping_charge = sum(float(s["price"]) for s in shipping_lines)
    shipping_tax = sum(float(t["price"]) for s in shipping_lines for t in s.get("tax_lines", []))

    payment_method = determine_payment_method(order)
    delivery_channel = determine_delivery_channel(order)
    order_type, against_order_id = determine_order_type(order)

    # Check existing order to preserve carrier if already resolved
    existing_order = (
        supabase.table("orders")
        .select("id, delivery_channel")
        .eq("shopify_order_id", order.get("id"))
        .maybe_single()
        .execute()
    )
    if existing_order.data:
        existing_channel = existing_order.data.get("delivery_channel")
        if delivery_channel == "Pending" and existing_channel and existing_channel != "Pending":
            delivery_channel = existing_channel

    voucher_date = get_voucher_date_ist(order.get("created_at"))

    # Upsert order in Supabase
    res = supabase.table("orders").upsert(
        {
            "shopify_order_id": order.get("id"),
            "order_number": str(order.get("order_number")),
            "voucher_date": voucher_date,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "total_amount": total_with_gst,
            "total_amount_ex_gst": total_ex_gst,
            "shipping_charge": shipping_charge,
            "shipping_gst": shipping_tax,
            "payment_method": payment_method,
            "delivery_channel": delivery_channel,
            "type": order_type,
            "against_order_id": against_order_id,
            "currency": order.get("currency", "INR"),
            "source": "Shopify",
            "raw_order": order
        },
        on_conflict="shopify_order_id"
    ).execute()

    order_id = res.data[0]["id"]

    # Delete existing items and recreate
    supabase.table("order_items").delete().eq("order_id", order_id).execute()

    for li in order.get("line_items", []):
        qty = li.get("quantity", 0)
        price = float(li.get("price", 0))

        discount = sum(float(d["amount"]) for d in li.get("discount_allocations", []))
        amount_with_gst = round(price * qty, 2)

        tax_lines = li.get("tax_lines", [])
        gst_amount = sum(float(t["price"]) for t in tax_lines)
        amount_ex_gst = round(amount_with_gst - gst_amount, 2)

        cgst = sgst = igst = 0
        for t in tax_lines:
            if t.get("title") == "CGST":
                cgst = float(t.get("price", 0))
            elif t.get("title") == "SGST":
                sgst = float(t.get("price", 0))
            elif t.get("title") == "IGST":
                igst = float(t.get("price", 0))

        hs_code = li.get("hs_code") or li.get("tax_code") or None
        item_code = li.get("sku") or str(li.get("id"))
        item_size = None

        variant_title = li.get("variant_title") or ""
        size_keywords = ["XXXL", "XXL", "2XS", "2XL", "3XL", "4XL", "XL", "XS", "S", "M", "L"]

        if variant_title:
            for size in size_keywords:
                if size.upper() == variant_title.upper():
                    item_size = size
                    break
            if not item_size:
                for size in size_keywords:
                    if size.upper() in variant_title.upper():
                        item_size = size
                        break

        if not item_size:
            name = li.get("name") or ""
            if " - " in name:
                size_part = name.split(" - ")[-1].strip()
                for size in size_keywords:
                    if size.upper() == size_part.upper():
                        item_size = size
                        break
            if not item_size:
                for size in size_keywords:
                    if size.upper() in name.upper():
                        item_size = size
                        break

        if not item_size:
            for prop in li.get("properties") or []:
                if prop.get("name") and "size" in prop.get("name", "").lower():
                    item_size = prop.get("value")
                    break

        gst_percentage = round(
            sum(float(t.get("rate", 0)) * 100 for t in tax_lines), 2
        ) if amount_ex_gst > 0 else 0

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "item_code": item_code,
            "item_size": item_size,
            "quantity": qty,
            "variant_id": li.get("variant_id"),
            "hs_code": hs_code,
            "rate": round(price, 2),
            "amount": amount_with_gst,
            "amount_ex_gst": amount_ex_gst,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "gst_percentage": gst_percentage,
            "item_discount": round(discount, 2)
        }).execute()

    return {
        "status": "stored",
        "delivery_channel": delivery_channel,
        "order_type": order_type,
        "against_order_id": against_order_id,
        "order_number": order.get("order_number")
    }


# =================================================
# Shopify Webhook
# =================================================

@app.post("/shopify/order")
async def shopify_order(request: Request):
    try:
        order = await request.json()
        result = save_shopify_order(order)
        return result
    except Exception as e:
        print(f"Error processing order webhook: {str(e)}")
        raise HTTPException(500, detail=str(e))


@app.post("/shopify/fulfillment")
async def shopify_fulfillment(request: Request):
    try:
        fulfillment = await request.json()
        order_id = fulfillment.get("order_id")
        if not order_id:
            return {"status": "error", "message": "no_order_id in webhook payload"}

        if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
            raise HTTPException(500, "SHOPIFY_STORE or SHOPIFY_TOKEN not configured")

        url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            raise HTTPException(500, f"Failed to fetch order from Shopify: {response.text}")

        order = response.json().get("order")
        delivery_channel = determine_delivery_channel(order)
        order_type, against_order_id = determine_order_type(order)

        result = supabase.table("orders").update({
            "delivery_channel": delivery_channel,
            "type": order_type,
            "against_order_id": against_order_id,
            "raw_order": order
        }).eq("shopify_order_id", order_id).execute()

        return {
            "status": "success",
            "order_id": order_id,
            "delivery_channel": delivery_channel,
            "order_type": order_type,
            "against_order_id": against_order_id,
            "updated": len(result.data) > 0
        }
    except Exception as e:
        print(f"Error in fulfillment webhook: {str(e)}")
        return {"status": "error", "message": str(e)}


# =================================================
# Sync Missing Orders Endpoint
# =================================================

@app.post("/sync/missing-orders")
async def sync_missing_orders():
    """
    Fetch all Shopify orders from last 5 days and sync them directly to Supabase.
    """
    try:
        if not SHOPIFY_STORE:
            raise HTTPException(500, "SHOPIFY_STORE_NAME is missing")
        if not SHOPIFY_TOKEN:
            raise HTTPException(500, "SHOPIFY_ACCESS_TOKEN is missing")

        now = datetime.now(timezone.utc)
        from_date = now - timedelta(days=5)
        created_at_min = from_date.isoformat()
        created_at_max = now.isoformat()

        headers = {
            "X-Shopify-Access-Token": SHOPIFY_TOKEN,
            "Content-Type": "application/json"
        }

        all_orders = []
        since_id = None

        while True:
            url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders.json"
            params = {
                "status": "any",
                "limit": 250,
                "created_at_min": created_at_min,
                "created_at_max": created_at_max,
                "order": "id asc"
            }
            if since_id:
                params["since_id"] = since_id

            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 200:
                raise HTTPException(500, f"Failed to fetch Shopify orders: {response.text}")

            data = response.json()
            orders = data.get("orders") or []
            if not orders:
                break

            all_orders.extend(orders)
            if len(orders) < 250:
                break

            last_id = orders[-1].get("id")
            if not last_id or since_id == last_id:
                break
            since_id = last_id

        synced_count = 0
        failed_count = 0

        for order in all_orders:
            try:
                save_shopify_order(order)
                synced_count += 1
            except Exception as ex:
                print(f"Failed to sync order {order.get('order_number')}: {ex}")
                failed_count += 1

        return {
            "status": "sync_complete",
            "days": 5,
            "total_orders_found": len(all_orders),
            "synced_orders": synced_count,
            "failed_orders": failed_count
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# =================================================
# Tally → Fetch Orders
# =================================================

@app.post("/tally/orders")
async def tally_orders_post(request: Request):
    try:
        body = await request.json()
        from_date = body.get("from_date")
        to_date = body.get("to_date")

        if not from_date or not to_date:
            raise HTTPException(400, "from_date and to_date required")

        res = (
            supabase.table("orders")
            .select("*, order_items(*)")
            .gte("voucher_date", from_date)
            .lte("voucher_date", to_date)
            .order("voucher_date")
            .execute()
        )

        tally_orders = []
        for o in res.data:
            raw = o.get("raw_order") or {}
            shipping_address = raw.get("shipping_address") or {}
            billing_address = raw.get("billing_address") or {}

            state = shipping_address.get("province") or billing_address.get("province") or ""
            country = shipping_address.get("country") or billing_address.get("country") or ""

            customer_name = o.get("customer_name")
            customer_email = o.get("customer_email")
            customer_phone = o.get("customer_phone")

            if (not customer_name or customer_name == "Unknown Customer") and o.get("against_order_id"):
                customer_name = shipping_address.get("name") or billing_address.get("name") or customer_name
                customer_phone = shipping_address.get("phone") or billing_address.get("phone") or customer_phone

            gross_item_amount = sum(float(li["price"]) * li["quantity"] for li in raw.get("line_items", []))
            discount_amount = float(raw.get("total_discounts", 0))
            net_item_amount = round(gross_item_amount - discount_amount, 2)

            items = []
            total_ex_gst = 0
            total_gst = 0
            total_with_gst = 0

            for li in raw.get("line_items", []):
                qty = li.get("quantity", 0)
                price = float(li.get("price", 0))
                discount = sum(float(d["amount"]) for d in li.get("discount_allocations", []))
                amount_with_gst = round(price * qty, 2)
                gst = sum(float(t["price"]) for t in li.get("tax_lines", []))
                amount_ex_gst = round(amount_with_gst - gst, 2)
                rate_ex_gst = round(amount_ex_gst / qty, 2) if qty > 0 else 0

                total_ex_gst += amount_ex_gst
                total_gst += gst
                total_with_gst += amount_with_gst

                db_item = next(
                    (oi for oi in o.get("order_items", []) if oi.get("item_code") == (li.get("sku") or str(li.get("id")))),
                    {}
                )

                items.append({
                    "item_code": li.get("sku") or li.get("id"),
                    "item_name": li.get("title"),
                    "item_size": db_item.get("item_size"),
                    "quantity": qty,
                    "variant_id": db_item.get("variant_id"),
                    "hs_code": db_item.get("hs_code"),
                    "rate_with_gst": round(price, 2),
                    "rate_ex_gst": rate_ex_gst,
                    "amount_ex_gst": round(amount_ex_gst, 2),
                    "amount_with_gst": round(amount_with_gst, 2),
                    "discount": round(discount, 2),
                    "gst": {
                        "cgst": next((float(t["price"]) for t in li.get("tax_lines", []) if t.get("title") == "CGST"), 0),
                        "sgst": next((float(t["price"]) for t in li.get("tax_lines", []) if t.get("title") == "SGST"), 0),
                        "igst": next((float(t["price"]) for t in li.get("tax_lines", []) if t.get("title") == "IGST"), 0),
                        "total": round(gst, 2),
                        "percentage": round(sum(float(t.get("rate", 0)) * 100 for t in li.get("tax_lines", [])), 2)
                    }
                })

            shipping = sum(float(s["price"]) for s in raw.get("shipping_lines", []))
            shipping_gst = sum(float(t["price"]) for s in raw.get("shipping_lines", []) for t in s.get("tax_lines", []))
            shipping_ex_gst = round(shipping - shipping_gst, 2)
            grand_total = float(raw.get("total_price", 0))

            payment_method = o.get("payment_method", "Prepaid")
            delivery_channel = o.get("delivery_channel", "Pending")
            order_type = o.get("type", "sales")
            against_order_id = o.get("against_order_id")

            voucher_type = f"{order_type.capitalize()}-{payment_method}-{delivery_channel}"

            order_data = {
                "voucher_type": voucher_type,
                "order_type": order_type,
                "payment_method": payment_method,
                "delivery_channel": delivery_channel,
                "voucher_number": o["order_number"],
                "voucher_date": o["voucher_date"],
                "customer": {
                    "name": customer_name,
                    "email": customer_email,
                    "phone": customer_phone,
                    "state": state,
                    "country": country
                },
                "items": items,
                "gross_item_amount": round(gross_item_amount, 2),
                "discount_amount": round(discount_amount, 2),
                "net_item_amount": round(net_item_amount, 2),
                "shipping_ex_gst": round(shipping_ex_gst, 2),
                "shipping_gst": round(shipping_gst, 2),
                "shipping_with_gst": round(shipping, 2),
                "total_ex_gst": round(total_ex_gst, 2),
                "total_gst": round(total_gst + shipping_gst, 2),
                "total_with_gst": round(grand_total, 2),
                "grand_total": round(grand_total, 2),
                "currency": o["currency"],
                "source": o["source"],
                "shopify_order_id": o["shopify_order_id"]
            }

            if order_type in ["exchange", "redispatch"] and against_order_id:
                order_data["against_order_id"] = against_order_id

            tally_orders.append(order_data)

        return {"orders": tally_orders}
    except Exception as e:
        raise HTTPException(500, str(e))


# =================================================
# Shopify OAuth Endpoints
# =================================================
SHOPIFY_API_KEY = os.getenv("SHOPIFY_API_KEY", "").strip()
SHOPIFY_API_SECRET = os.getenv("SHOPIFY_API_SECRET", "").strip()
SCOPES = "read_orders,read_products,read_customers,write_orders"
REDIRECT_URI = "https://shopify-tally-middleware.onrender.com/auth/callback"

@app.get("/auth/install")
def shopify_install(shop: str):
    if not shop:
        raise HTTPException(400, "Missing shop parameter")
    params = {
        "client_id": SHOPIFY_API_KEY,
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI
    }
    query = urllib.parse.urlencode(params)
    return RedirectResponse(f"https://{shop}/admin/oauth/authorize?{query}")


@app.get("/auth/callback")
def shopify_callback(code: str = "", shop: str = ""):
    if not code or not shop:
        return HTMLResponse("<h3>Error: Missing 'code' or 'shop' parameter in callback</h3>", status_code=400)

    token_url = f"https://{shop}/admin/oauth/access_token"
    payload = {
        "client_id": SHOPIFY_API_KEY,
        "client_secret": SHOPIFY_API_SECRET,
        "code": code
    }
    
    try:
        response = requests.post(token_url, json=payload, timeout=30)
    except Exception as net_err:
        return HTMLResponse(f"<h3>Network error connecting to Shopify: {str(net_err)}</h3>", status_code=500)

    if response.status_code != 200:
        return HTMLResponse(f"""
        <div style="font-family: Arial; padding: 40px; max-width: 600px; margin: auto;">
            <h2 style="color: red;">❌ Token Exchange Failed</h2>
            <p><strong>Status:</strong> {response.status_code}</p>
            <p><strong>Shopify Response:</strong></p>
            <pre style="background: #fee; padding: 15px; border-radius: 5px;">{response.text}</pre>
            <p>Please ensure <code>SHOPIFY_API_KEY</code> and <code>SHOPIFY_API_SECRET</code> on Render match your Shopify Partner app credentials.</p>
        </div>
        """, status_code=500)

    try:
        data = response.json()
        access_token = data.get("access_token")
    except Exception:
        access_token = None

    if not access_token:
        return HTMLResponse(f"<h3>Error: No access_token returned by Shopify: {response.text}</h3>", status_code=500)

    try:
        supabase.table("shopify_tokens").upsert({
            "shop": shop,
            "access_token": access_token,
            "created_at": "now()"
        }, on_conflict="shop").execute()
    except Exception as db_err:
        print(f"Could not save token to DB (non-fatal): {db_err}")

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>App Installed Successfully!</title>
        <style>
            body {{ font-family: Arial; padding: 40px; background: #f0f0f0; }}
            .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 40px; border-radius: 10px; }}
            .success {{ color: #00b300; font-size: 24px; margin-bottom: 20px; }}
            .token {{ background: #f5f5f5; padding: 15px; border-radius: 5px; word-break: break-all; font-family: monospace; }}
            .warning {{ background: #fff3cd; padding: 15px; border-radius: 5px; margin-top: 20px; border-left: 4px solid #ffc107; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success">✅ App Installed Successfully!</div>
            <p><strong>Shop:</strong> {shop}</p>
            <p><strong>Access Token (SAVE THIS!):</strong></p>
            <div class="token">{access_token}</div>
            <div class="warning">
                <strong>⚠️ IMPORTANT:</strong>
                <ol>
                    <li>Copy the access token above</li>
                    <li>Go to your Render dashboard</li>
                    <li>Set Environment Variable: <code>SHOPIFY_ACCESS_TOKEN={access_token}</code></li>
                    <li>Set Environment Variable: <code>SHOPIFY_STORE_NAME={shop.replace('.myshopify.com', '')}</code></li>
                    <li>Save and redeploy</li>
                </ol>
            </div>
        </div>
    </body>
    </html>
    """)


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>👗 AINA Shopify-Tally Integration API is Running</h1>"
