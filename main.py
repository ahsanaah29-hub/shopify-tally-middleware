import urllib.parse
from fastapi.responses import HTMLResponse, RedirectResponse
import os
import requests
from fastapi import FastAPI, Request, HTTPException
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

# -------------------------------------------------
# Helper Functions
# -------------------------------------------------
def determine_payment_method(order):
    """
    Determine if order is COD or Prepaid.
    Returns: 'COD' or 'Prepaid'
    """
    # Check payment gateway names
    gateway = order.get("gateway", "").lower()
    
    # Common COD indicators in Shopify
    if "cash on delivery" in gateway or "cod" in gateway:
        return "COD"
    
    # Check if payment is pending (usually COD)
    financial_status = order.get("financial_status", "").lower()
    if financial_status == "pending":
        return "COD"
    
    # Check payment method details
    payment_gateway_names = order.get("payment_gateway_names", [])
    for pg in payment_gateway_names:
        if "cash" in pg.lower() or "cod" in pg.lower():
            return "COD"
    
    # Default to Prepaid if paid or authorized
    if financial_status in ["paid", "authorized", "partially_paid"]:
        return "Prepaid"
    
    # Default fallback
    return "Prepaid"


def determine_delivery_channel(order):
    """
    Identify delivery channel from Shopify order data.
    
    PRIORITY ORDER (checks in sequence):
    1. Order tags (highest priority - manually tagged)
    2. Customer journey / conversion data (Instagram, Facebook sessions)
    3. Shipping company name (DTDC, BlueDart, etc.)
    4. Note attributes (custom fields)
    5. Source name and referring site
    
    Returns: 'Website', 'Marketplace', or 'Social-Media'
    """
    # Method 1: Check order tags (HIGHEST PRIORITY)
    tags = order.get("tags", "").lower()
    if "marketplace" in tags or "amazon" in tags or "flipkart" in tags or "meesho" in tags:
        return "Marketplace"
    if "instagram" in tags or "facebook" in tags or "whatsapp" in tags or "social" in tags:
        return "Social-Media"
    if "website" in tags:
        return "Website"
    
    # Method 2: Check customer journey data (NEW!)
    # This captures "1st session from Instagram" type data
    customer_journey = order.get("customer_journey_summary", {})
    if customer_journey:
        first_visit = customer_journey.get("first_visit", {})
        landing_page = first_visit.get("landing_page", "").lower()
        referrer = first_visit.get("referrer", "").lower()
        utm_source = first_visit.get("utm_source", "").lower()
        
        # Check for social media sources
        social_indicators = ["instagram", "facebook", "fb.com", "ig", "social"]
        if any(indicator in landing_page or indicator in referrer or indicator in utm_source 
               for indicator in social_indicators):
            return "Social-Media"
    
    # Method 3: Check client details (another way Shopify tracks sources)
    client_details = order.get("client_details", {})
    if client_details:
        user_agent = client_details.get("user_agent", "").lower()
        if "instagram" in user_agent or "fban" in user_agent or "fbav" in user_agent:
            return "Social-Media"
    
    # Method 4: Check landing site and referring site
    landing_site = order.get("landing_site", "").lower()
    referring_site = order.get("referring_site", "").lower()
    
    if referring_site:
        if any(x in referring_site for x in ["instagram", "facebook", "fb.com", "ig.me"]):
            return "Social-Media"
        if any(x in referring_site for x in ["amazon", "flipkart", "meesho"]):
            return "Marketplace"
    
    if landing_site:
        if any(x in landing_site for x in ["instagram", "facebook", "fbclid"]):
            return "Social-Media"
    
    # Method 5: Check shipping company name
    shipping_lines = order.get("shipping_lines", [])
    for shipping in shipping_lines:
        shipping_title = shipping.get("title", "").lower()
        shipping_code = shipping.get("code", "").lower()
        
        # If tracking number is present, you could map courier to channel
        tracking_number = shipping.get("tracking_number", "")
        if tracking_number:
            # Add custom logic here if certain couriers = certain channels
            pass
    
    # Method 6: Check note attributes (custom order fields)
    note_attributes = order.get("note_attributes", [])
    for attr in note_attributes:
        name = attr.get("name", "").lower()
        value = attr.get("value", "").lower()
        if name == "channel" or name == "source":
            if "marketplace" in value:
                return "Marketplace"
            if "instagram" in value or "facebook" in value or "social" in value:
                return "Social-Media"
            if "website" in value:
                return "Website"
    
    # Method 7: Check source name
    source_name = order.get("source_name", "").lower()
    if "web" in source_name or "online" in source_name:
        return "Website"
    if "pos" in source_name:
        return "Website"
    if "instagram" in source_name or "facebook" in source_name:
        return "Social-Media"
    
    # Method 8: Check customer note for channel hints
    note = order.get("note", "").lower()
    if "instagram" in note or "facebook" in note:
        return "Social-Media"
    if "amazon" in note or "marketplace" in note:
        return "Marketplace"
    
    # Default to Website
    return "Website"


# -------------------------------------------------
# Shopify → Middleware (Webhook → Supabase)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    order = await request.json()

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

    customer_email = (
        customer.get("email")
        or order.get("email")
        or billing.get("email")
    )

    customer_phone = (
        customer.get("phone")
        or billing.get("phone")
        or shipping.get("phone")
    )

    total_with_gst = float(order.get("total_price", 0))
    total_gst = float(order.get("total_tax", 0))
    total_ex_gst = round(total_with_gst - total_gst, 2)

    shipping_lines = order.get("shipping_lines", [])

    shipping_charge = sum(
        float(s["price"])
        for s in shipping_lines
    )

    shipping_tax = sum(
        float(t["price"])
        for s in shipping_lines
        for t in s.get("tax_lines", [])
    )

    # ✅ Determine payment method (COD or Prepaid)
    payment_method = determine_payment_method(order)
    
    # ✅ Determine delivery channel
    delivery_channel = determine_delivery_channel(order)

    res = supabase.table("orders").upsert(
        {
            "shopify_order_id": order.get("id"),
            "order_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at")[:10],
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "total_amount": total_with_gst,
            "total_amount_ex_gst": total_ex_gst,
            "shipping_charge": shipping_charge,
            "shipping_gst": shipping_tax,
            "payment_method": payment_method,
            "delivery_channel": delivery_channel,
            "currency": order.get("currency", "INR"),
            "source": "Shopify",
            "raw_order": order
        },
        on_conflict="shopify_order_id"
    ).execute()

    order_id = res.data[0]["id"]

    supabase.table("order_items").delete().eq("order_id", order_id).execute()

    for li in order.get("line_items", []):
        qty = li.get("quantity", 0)
        price = float(li.get("price", 0))

        # Sum all discounts applied to this item
        discount = sum(
            float(d["amount"])
            for d in li.get("discount_allocations", [])
        )

        gross = price * qty
        amount_with_gst = round(gross - discount, 2)

        # GST from Shopify
        tax_lines = li.get("tax_lines", [])
        gst_amount = sum(float(t["price"]) for t in tax_lines)

        amount_ex_gst = round(amount_with_gst - gst_amount, 2)

        cgst = sgst = igst = 0
        for t in tax_lines:
            if t["title"] == "CGST":
                cgst = float(t["price"])
            elif t["title"] == "SGST":
                sgst = float(t["price"])
            elif t["title"] == "IGST":
                igst = float(t["price"])

        # Use original Shopify price (with GST, before discount) as rate
        original_rate_with_gst = price

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "quantity": qty,
            "rate": round(original_rate_with_gst, 2),
            "amount": amount_with_gst,
            "amount_ex_gst": amount_ex_gst,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "item_discount": round(discount, 2)
        }).execute()

    return {"status": "stored"}


# -------------------------------------------------
# Tally → Fetch Orders (Enhanced for Client Requirements)
# -------------------------------------------------
@app.post("/tally/orders")
async def tally_orders_post(request: Request):
    body = await request.json()

    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")

    res = supabase.table("orders") \
        .select("*, order_items(*)") \
        .gte("voucher_date", from_date) \
        .lte("voucher_date", to_date) \
        .order("voucher_date") \
        .execute()

    tally_orders = []

    for o in res.data:
        raw = o["raw_order"]
        
        # Calculate totals
        gross_item_amount = sum(
            float(li["price"]) * li["quantity"]
            for li in raw.get("line_items", [])
        )

        discount_amount = float(raw.get("total_discounts", 0))
        net_item_amount = round(gross_item_amount - discount_amount, 2)

        shopify_lines = raw.get("line_items", [])

        items = []
        total_ex_gst = 0
        total_gst = 0
        total_with_gst = 0
        
        for li in shopify_lines:
            qty = li["quantity"]
            price = float(li["price"])

            discount = sum(float(d["amount"]) for d in li.get("discount_allocations", []))
            gross = price * qty
            amount_with_gst = gross - discount

            gst = sum(float(t["price"]) for t in li.get("tax_lines", []))
            amount_ex_gst = amount_with_gst - gst

            total_ex_gst += amount_ex_gst
            total_gst += gst
            total_with_gst += amount_with_gst

            # ✅ Enhanced item structure for Tally
            items.append({
                "item_name": li["title"],
                "quantity": qty,
                "rate_with_gst": round(price, 2),  # ✅ Original Shopify price (with GST)
                "rate_ex_gst": round(amount_ex_gst / qty, 2),  # ✅ Per-unit price without GST
                "amount_ex_gst": round(amount_ex_gst, 2),  # ✅ Total without GST
                "amount_with_gst": round(amount_with_gst, 2),  # ✅ Total with GST
                "discount": round(discount, 2),  # ✅ Item-level discount
                "gst": {
                    "cgst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="CGST"), 0),
                    "sgst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="SGST"), 0),
                    "igst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="IGST"), 0),
                    "total": round(gst, 2)
                }
            })

        # Shipping calculation
        shipping = sum(
            float(s["price"])
            for s in raw.get("shipping_lines", [])
        )

        shipping_gst = sum(
            float(t["price"])
            for s in raw.get("shipping_lines", [])
            for t in s.get("tax_lines", [])
        )

        shipping_ex_gst = round(shipping - shipping_gst, 2)

        grand_total = float(raw["total_price"])

        # ✅ Get payment method and delivery channel from database columns
        payment_method = o.get("payment_method") or "Prepaid"
        delivery_channel = o.get("delivery_channel") or "Website"
        
        voucher_type = f"Sales-{payment_method}-{delivery_channel}"
        # Example: "Sales-COD-Website", "Sales-Prepaid-Marketplace", etc.

        tally_orders.append({
            # ✅ Enhanced voucher classification
            "voucher_type": voucher_type,
            "payment_method": payment_method,  # COD or Prepaid
            "delivery_channel": delivery_channel,  # Website, Marketplace, or Social-Media
            
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            
            "customer": {
                "name": o["customer_name"],
                "email": o["customer_email"],
                "phone": o["customer_phone"]
            },
            
            # ✅ Items with GST split
            "items": items,
            
            # ✅ Discount classified as Direct Expense
            "direct_expenses": {
                "sales_discount": {
                    "amount": round(discount_amount, 2),
                    "ledger": "Sales Discount"  # Map to Tally ledger
                }
            },
            
            # ✅ Shipping classified as Indirect Expense
            "indirect_expenses": {
                "shipping_charges": {
                    "amount_ex_gst": round(shipping_ex_gst, 2),
                    "gst_amount": round(shipping_gst, 2),
                    "amount_with_gst": round(shipping, 2),
                    "ledger": "Shipping & Handling Charges"  # Map to Tally ledger
                }
            },
            
            # Summary totals
            "summary": {
                "gross_item_amount": round(gross_item_amount, 2),
                "discount_amount": round(discount_amount, 2),
                "net_item_amount": round(net_item_amount, 2),
                "total_ex_gst": round(total_ex_gst, 2),
                "total_gst": round(total_gst + shipping_gst, 2),
                "total_with_gst": round(net_item_amount, 2),
                "shipping_ex_gst": round(shipping_ex_gst, 2),
                "shipping_gst": round(shipping_gst, 2),
                "grand_total": round(grand_total, 2)
            },
            
            "currency": o["currency"],
            "source": o["source"],
            "shopify_order_id": o["shopify_order_id"]
        })

    return {"orders": tally_orders}


# -------------------------------------------------
# Auto-Sync Yesterday's Orders - Fetch delivery channel info
# -------------------------------------------------
@app.post("/sync/yesterday-orders")
async def sync_yesterday_orders():
    """
    Fetches yesterday's orders from Shopify and updates delivery channels.
    This handles the delay in shipping/tracking info (like DTDC).
    
    Run this daily via cron job or Render's cron feature.
    """
    from datetime import datetime, timedelta
    
    # Get yesterday's date
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Fetch orders from Shopify
    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
        f"?status=any&created_at_min={yesterday}T00:00:00Z&created_at_max={yesterday}T23:59:59Z"
        f"&limit=250"
    )
    
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }
    
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        raise HTTPException(500, f"Shopify API error: {response.text}")
    
    shopify_orders = response.json().get("orders", [])
    updated_count = 0
    
    for order in shopify_orders:
        shopify_order_id = order.get("id")
        
        # Re-determine delivery channel with fresh data
        delivery_channel = determine_delivery_channel(order)
        payment_method = determine_payment_method(order)
        
        # Update in database
        result = supabase.table("orders") \
            .update({
                "delivery_channel": delivery_channel,
                "payment_method": payment_method,
                "raw_order": order  # Update with latest data
            }) \
            .eq("shopify_order_id", shopify_order_id) \
            .execute()
        
        if result.data:
            updated_count += 1
    
    return {
        "status": "completed",
        "date": yesterday,
        "orders_processed": len(shopify_orders),
        "orders_updated": updated_count
    }


# -------------------------------------------------
# Manual Update Endpoint - For DTDC delay issue
# -------------------------------------------------
@app.post("/update/delivery-channel")
async def update_delivery_channel(request: Request):
    """
    Manually update delivery channel for specific orders.
    Useful when shipping info arrives next day (like DTDC).
    
    Body: {
        "order_number": "181074",
        "delivery_channel": "Website"  // or "Marketplace" or "Social-Media"
    }
    OR
    Body: {
        "shopify_order_id": 7227929755785,
        "delivery_channel": "Marketplace"
    }
    """
    body = await request.json()
    
    order_number = body.get("order_number")
    shopify_order_id = body.get("shopify_order_id")
    delivery_channel = body.get("delivery_channel")
    
    if not delivery_channel:
        raise HTTPException(400, "delivery_channel is required")
    
    if delivery_channel not in ["Website", "Marketplace", "Social-Media"]:
        raise HTTPException(400, "delivery_channel must be Website, Marketplace, or Social-Media")
    
    if order_number:
        result = supabase.table("orders") \
            .update({"delivery_channel": delivery_channel}) \
            .eq("order_number", str(order_number)) \
            .execute()
    elif shopify_order_id:
        result = supabase.table("orders") \
            .update({"delivery_channel": delivery_channel}) \
            .eq("shopify_order_id", shopify_order_id) \
            .execute()
    else:
        raise HTTPException(400, "Either order_number or shopify_order_id is required")
    
    if not result.data:
        raise HTTPException(404, "Order not found")
    
    return {
        "status": "updated",
        "order": result.data[0]
    }


# -------------------------------------------------
# Batch Update Endpoint - Update multiple orders at once
# -------------------------------------------------
@app.post("/update/delivery-channel-batch")
async def update_delivery_channel_batch(request: Request):
    """
    Batch update delivery channels for multiple orders.
    
    Body: {
        "updates": [
            {"order_number": "181074", "delivery_channel": "Marketplace"},
            {"order_number": "181075", "delivery_channel": "Social-Media"}
        ]
    }
    """
    body = await request.json()
    updates = body.get("updates", [])
    
    if not updates:
        raise HTTPException(400, "updates array is required")
    
    results = []
    for update in updates:
        order_number = update.get("order_number")
        delivery_channel = update.get("delivery_channel")
        
        if not order_number or not delivery_channel:
            continue
        
        result = supabase.table("orders") \
            .update({"delivery_channel": delivery_channel}) \
            .eq("order_number", str(order_number)) \
            .execute()
        
        if result.data:
            results.append({
                "order_number": order_number,
                "status": "updated"
            })
    
    return {
        "status": "completed",
        "updated_count": len(results),
        "results": results
    }


# -------------------------------------------------
# Tally → Push Sales to Shopify
# -------------------------------------------------
@app.post("/tally/sales")
async def tally_sales(request: Request):
    data = await request.json()

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/orders.json"
    )

    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    full_name = data.get("customer", {}).get("name", "").strip()
    name_parts = full_name.split(" ", 1)

    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    line_items = []
    for item in data.get("items", []):
        product_name = item.get("product_name") or item.get("item_name")

        line_items.append({
            "title": product_name,
            "quantity": item["quantity"],
            "price": round(item["rate"], 2)
        })

    payload = {
        "order": {
            "email": data["customer"].get("email"),
            "customer": {
                "first_name": first_name,
                "last_name": last_name,
                "email": data["customer"].get("email")
            },
            "line_items": line_items,
            "financial_status": "paid",
            "currency": "INR"
        }
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=response.text)

    return {
        "status": "success",
        "shopify_order_id": response.json()["order"]["id"]
    }


# -------------------------------------------------
# Shopify OAuth
# -------------------------------------------------
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
        "redirect_uri": REDIRECT_URI,
    }

    query = urllib.parse.urlencode(params)
    install_url = f"https://{shop}/admin/oauth/authorize?{query}"

    return RedirectResponse(install_url)


@app.get("/auth/callback")
def shopify_callback(code: str, shop: str):
    if not code or not shop:
        raise HTTPException(400, "Invalid OAuth response")

    token_url = f"https://{shop}/admin/oauth/access_token"

    payload = {
        "client_id": SHOPIFY_API_KEY,
        "client_secret": SHOPIFY_API_SECRET,
        "code": code
    }

    response = requests.post(token_url, json=payload)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Token exchange failed: {response.text}"
        )

    data = response.json()
    access_token = data.get("access_token")

    return {
        "status": "app_installed",
        "shop": shop,
        "access_token_received": bool(access_token)
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AINA - Shopify-Tally Integration</title>
        <style>
            body { font-family: Arial; padding: 40px; background: #f5f5f5; }
            .container { max-width: 1000px; margin: 0 auto; background: white; padding: 40px; border-radius: 10px; }
            h1 { color: #5c6ac4; }
            .feature { background: #f9fafb; padding: 20px; margin: 20px 0; border-radius: 8px; border-left: 4px solid #5c6ac4; }
            .feature h3 { margin-top: 0; color: #202223; }
            code { background: #e1e3e5; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
            .btn { 
                background: #5c6ac4; 
                color: white; 
                padding: 12px 24px; 
                border: none; 
                border-radius: 6px; 
                cursor: pointer;
                font-size: 14px;
                margin: 10px 5px;
            }
            .btn:hover { background: #4c5db8; }
            .btn-secondary { background: #637381; }
            .btn-secondary:hover { background: #525f6f; }
            #syncResult { 
                margin-top: 20px; 
                padding: 15px; 
                border-radius: 6px; 
                display: none;
            }
            .success { background: #d4edda; color: #155724; }
            .error { background: #f8d7da; color: #721c24; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>👗 AINA Shopify-Tally Integration</h1>
            <p>Advanced integration with automated delivery channel detection</p>
            
            <div class="feature">
                <h3>🔄 Auto-Sync Yesterday's Orders</h3>
                <p>Click the button below to fetch yesterday's orders from Shopify and update delivery channels based on the latest tracking and conversion data.</p>
                <button class="btn" onclick="syncYesterdayOrders()">Sync Yesterday's Orders</button>
                <div id="syncResult"></div>
            </div>
            
            <div class="feature">
                <h3>✅ Voucher Classification</h3>
                <p><strong>COD Orders:</strong> <code>Sales-COD-{Channel}</code></p>
                <p><strong>Prepaid Orders:</strong> <code>Sales-Prepaid-{Channel}</code></p>
                <p><strong>Channels:</strong> Website, Marketplace, Social-Media</p>
            </div>
            
            <div class="feature">
                <h3>🎯 Automatic Channel Detection</h3>
                <p>The system automatically detects delivery channels from:</p>
                <ul>
                    <li><strong>Instagram/Facebook:</strong> Detected from "1st session from Instagram" and conversion data</li>
                    <li><strong>Marketplace:</strong> Detected from referring sites (Amazon, Flipkart, etc.)</li>
                    <li><strong>Website:</strong> Direct website orders</li>
                </ul>
            </div>
            
            <div class="feature">
                <h3>✅ GST Split</h3>
                <p>All items show:</p>
                <ul>
                    <li>Rate with GST (as displayed in Shopify)</li>
                    <li>Rate without GST (calculated)</li>
                    <li>CGST, SGST, IGST breakup</li>
                </ul>
            </div>
            
            <div class="feature">
                <h3>✅ Expense Classification</h3>
                <p><strong>Direct Expenses:</strong> Sales Discounts</p>
                <p><strong>Indirect Expenses:</strong> Shipping & Handling Charges (with GST split)</p>
            </div>
            
            <div class="feature">
                <h3>📊 API Endpoints</h3>
                <p><strong>Webhook:</strong> POST /shopify/order</p>
                <p><strong>Fetch Orders:</strong> POST /tally/orders</p>
                <p><strong>Create Order:</strong> POST /tally/sales</p>
                <p><strong>Sync Yesterday:</strong> POST /sync/yesterday-orders</p>
                <p><strong>Update Channel:</strong> POST /update/delivery-channel</p>
                <p><strong>Batch Update:</strong> POST /update/delivery-channel-batch</p>
            </div>
            
            <div class="feature">
                <h3>⏰ Automated Daily Sync</h3>
                <p>Set up a cron job to run daily at 2 AM:</p>
                <code>0 2 * * * curl -X POST https://your-app.onrender.com/sync/yesterday-orders</code>
                <p style="margin-top: 10px;"><em>This will automatically update delivery channels for yesterday's orders after tracking info becomes available.</em></p>
            </div>
        </div>
        
        <script>
            async function syncYesterdayOrders() {
                const btn = event.target;
                const resultDiv = document.getElementById('syncResult');
                
                btn.disabled = true;
                btn.textContent = '⏳ Syncing...';
                resultDiv.style.display = 'none';
                
                try {
                    const response = await fetch('/sync/yesterday-orders', {
                        method: 'POST'
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        resultDiv.className = 'success';
                        resultDiv.innerHTML = `
                            ✅ <strong>Success!</strong><br>
                            Date: ${result.date}<br>
                            Orders Processed: ${result.orders_processed}<br>
                            Orders Updated: ${result.orders_updated}
                        `;
                    } else {
                        throw new Error(result.detail || 'Sync failed');
                    }
                } catch (error) {
                    resultDiv.className = 'error';
                    resultDiv.innerHTML = `❌ <strong>Error:</strong> ${error.message}`;
                } finally {
                    resultDiv.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Sync Yesterday\'s Orders';
                }
            }
        </script>
    </body>
    </html>
    """
