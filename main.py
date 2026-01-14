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
    gateway = order.get("gateway", "").lower()
    
    if "cash on delivery" in gateway or "cod" in gateway:
        return "COD"
    
    financial_status = order.get("financial_status", "").lower()
    if financial_status == "pending":
        return "COD"
    
    payment_gateway_names = order.get("payment_gateway_names", [])
    for pg in payment_gateway_names:
        if "cash" in pg.lower() or "cod" in pg.lower():
            return "COD"
    
    if financial_status in ["paid", "authorized", "partially_paid"]:
        return "Prepaid"
    
    return "Prepaid"


def determine_delivery_channel(order):
    """
    Identify delivery channel from shipping carrier.
    Returns: 'DTDC', 'Delhivery', 'BlueDart', or 'Pending'
    
    Checks multiple sources:
    1. Order tags (e.g., "carrier:DTDC")
    2. Fulfillment tracking company
    3. Shipping line details
    4. Order notes/attributes
    """
    # Method 1: Check order tags (EASIEST - no API needed!)
    tags = order.get("tags", "").lower()
    if "dtdc" in tags or "carrier:dtdc" in tags:
        return "DTDC"
    if "delhivery" in tags or "carrier:delhivery" in tags:
        return "Delhivery"
    if "bluedart" in tags or "blue dart" in tags or "carrier:bluedart" in tags:
        return "BlueDart"
    
    # Method 2: Check fulfillments (tracking companies)
    fulfillments = order.get("fulfillments", [])
    for f in fulfillments:
        tracking_company = f.get("tracking_company", "").lower()
        if "dtdc" in tracking_company:
            return "DTDC"
        if "delhivery" in tracking_company:
            return "Delhivery"
        if "bluedart" in tracking_company or "blue dart" in tracking_company:
            return "BlueDart"
    
    # Method 3: Check shipping lines
    shipping_lines = order.get("shipping_lines", [])
    for s in shipping_lines:
        carrier = s.get("code", "").lower()
        title = s.get("title", "").lower()
        
        if "dtdc" in carrier or "dtdc" in title:
            return "DTDC"
        if "delhivery" in carrier or "delhivery" in title:
            return "Delhivery"
        if "bluedart" in carrier or "blue dart" in carrier or "bluedart" in title or "blue dart" in title:
            return "BlueDart"
    
    # Method 4: Check order notes
    note = order.get("note", "").lower()
    if "dtdc" in note:
        return "DTDC"
    if "delhivery" in note:
        return "Delhivery"
    if "bluedart" in note or "blue dart" in note:
        return "BlueDart"
    
    # Method 5: Check note attributes (custom fields)
    note_attributes = order.get("note_attributes", [])
    for attr in note_attributes:
        value = str(attr.get("value", "")).lower()
        if "dtdc" in value:
            return "DTDC"
        if "delhivery" in value:
            return "Delhivery"
        if "bluedart" in value or "blue dart" in value:
            return "BlueDart"
    
    # Default to Pending if carrier not identified
    return "Pending"


# -------------------------------------------------
# Shopify → Middleware (Webhook → Supabase)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    """
    Webhook for order creation AND updates (including when tags are added).
    This fires multiple times:
    1. When order is created
    2. When order is updated (tags, fulfillment, etc.)
    """
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

    payment_method = determine_payment_method(order)
    
    # ✅ This will now detect tags like "carrier:DTDC"
    delivery_channel = determine_delivery_channel(order)

    # ✅ UPSERT: Creates new order OR updates existing one (when tags are added)
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
            "delivery_channel": delivery_channel,  # ✅ Updates when tags change!
            "currency": order.get("currency", "INR"),
            "source": "Shopify",
            "raw_order": order  # ✅ Stores latest order data with tags
        },
        on_conflict="shopify_order_id"
    ).execute()

    order_id = res.data[0]["id"]

    # Delete and recreate items (in case quantities changed)
    supabase.table("order_items").delete().eq("order_id", order_id).execute()

    for li in order.get("line_items", []):
        qty = li.get("quantity", 0)
        price = float(li.get("price", 0))

        discount = sum(
            float(d["amount"])
            for d in li.get("discount_allocations", [])
        )

        gross = price * qty
        amount_with_gst = round(gross - discount, 2)

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

    return {
        "status": "stored", 
        "delivery_channel": delivery_channel,
        "order_number": order.get("order_number")
    }


# -------------------------------------------------
# NEW: Update Delivery Channel (Call this webhook for fulfillments)
# -------------------------------------------------
@app.post("/shopify/fulfillment")
async def shopify_fulfillment(request: Request):
    """
    Webhook endpoint for when Shopify fulfillment is created/updated.
    This will be called the next day when carrier is assigned.
    """
    try:
        fulfillment = await request.json()
        
        # The fulfillment webhook sends order_id
        order_id = fulfillment.get("order_id")
        
        if not order_id:
            return {"status": "error", "message": "no_order_id in webhook payload"}
        
        # Check if required env vars are set
        if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
            raise HTTPException(500, "SHOPIFY_STORE or SHOPIFY_TOKEN not configured")
        
        # Fetch the full order from Shopify to get updated carrier info
        url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            raise HTTPException(500, f"Failed to fetch order from Shopify: {response.text}")
        
        order = response.json()["order"]
        
        # Determine delivery channel from updated order
        delivery_channel = determine_delivery_channel(order)
        
        # Update database
        result = supabase.table("orders") \
            .update({
                "delivery_channel": delivery_channel,
                "raw_order": order
            }) \
            .eq("shopify_order_id", order_id) \
            .execute()
        
        return {
            "status": "success",
            "order_id": order_id,
            "delivery_channel": delivery_channel,
            "updated": len(result.data) > 0
        }
    
    except Exception as e:
        # Log the error but don't crash
        print(f"Error in fulfillment webhook: {str(e)}")
        return {
            "status": "error",
            "message": str(e)
        }


# -------------------------------------------------
# NEW: Manual sync endpoint to update pending channels
# -------------------------------------------------
@app.post("/sync/delivery-channels")
async def sync_delivery_channels():
    """
    Manually sync delivery channels for orders with 'Pending' status.
    Run this daily or on-demand.
    """
    # Get all orders with Pending delivery channel
    res = supabase.table("orders") \
        .select("shopify_order_id") \
        .eq("delivery_channel", "Pending") \
        .execute()
    
    updated_count = 0
    
    for order_record in res.data:
        shopify_order_id = order_record["shopify_order_id"]
        
        # Fetch fresh data from Shopify
        url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders/{shopify_order_id}.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            continue
        
        order = response.json()["order"]
        delivery_channel = determine_delivery_channel(order)
        
        # Only update if no longer pending
        if delivery_channel != "Pending":
            supabase.table("orders") \
                .update({
                    "delivery_channel": delivery_channel,
                    "raw_order": order
                }) \
                .eq("shopify_order_id", shopify_order_id) \
                .execute()
            
            updated_count += 1
    
    return {
        "status": "sync_complete",
        "updated_orders": updated_count
    }


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

            items.append({
                "item_name": li["title"],
                "quantity": qty,
                "rate_with_gst": round(price, 2),
                "rate_ex_gst": round(amount_ex_gst / qty, 2),
                "amount_ex_gst": round(amount_ex_gst, 2),
                "amount_with_gst": round(amount_with_gst, 2),
                "discount": round(discount, 2),
                "gst": {
                    "cgst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="CGST"), 0),
                    "sgst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="SGST"), 0),
                    "igst": next((float(t["price"]) for t in li["tax_lines"] if t["title"]=="IGST"), 0),
                    "total": round(gst, 2)
                }
            })

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

        payment_method = o.get("payment_method", "Prepaid")
        delivery_channel = o.get("delivery_channel", "Pending")
        
        # ✅ Fixed voucher type format
        voucher_type = f"Sales-{payment_method}-{delivery_channel}"
        # Examples: 
        # "Sales-COD-DTDC"
        # "Sales-Prepaid-Delhivery" 
        # "Sales-COD-BlueDart"
        # "Sales-Prepaid-Pending" (if not yet assigned)

        tally_orders.append({
            "voucher_type": voucher_type,
            "payment_method": payment_method,
            "delivery_channel": delivery_channel,
            
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            
            "customer": {
                "name": o["customer_name"],
                "email": o["customer_email"],
                "phone": o["customer_phone"]
            },
            
            "items": items,
            
            "direct_expenses": {
                "sales_discount": {
                    "amount": round(discount_amount, 2),
                    "ledger": "Sales Discount"
                }
            },
            
            "indirect_expenses": {
                "shipping_charges": {
                    "amount_ex_gst": round(shipping_ex_gst, 2),
                    "gst_amount": round(shipping_gst, 2),
                    "amount_with_gst": round(shipping, 2),
                    "ledger": "Shipping & Handling Charges"
                }
            },
            
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
    
    # ✅ SAVE TOKEN TO DATABASE
    if access_token:
        supabase.table("shopify_tokens").upsert({
            "shop": shop,
            "access_token": access_token,
            "created_at": "now()"
        }, on_conflict="shop").execute()

    # ✅ DISPLAY TOKEN SO YOU CAN COPY IT
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
                    <li>Add environment variable: <code>SHOPIFY_ACCESS_TOKEN={access_token}</code></li>
                    <li>Extract store name from shop URL and add: <code>SHOPIFY_STORE_NAME=(store-name-only)</code></li>
                    <li>Save and redeploy</li>
                </ol>
            </div>
        </div>
    </body>
    </html>
    """)


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AINA - Shopify-Tally Integration</title>
        <style>
            body { font-family: Arial; padding: 40px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 40px; border-radius: 10px; }
            h1 { color: #5c6ac4; }
            .feature { background: #f9fafb; padding: 20px; margin: 20px 0; border-radius: 8px; border-left: 4px solid #5c6ac4; }
            .feature h3 { margin-top: 0; color: #202223; }
            .workflow { background: #e8f5e9; border-left-color: #4caf50; }
            .warning { background: #fff4e6; border-left-color: #ff9800; }
            code { background: #e1e3e5; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
            ul { margin: 10px 0; }
            .steps { background: #fff; padding: 15px; border-radius: 5px; border: 1px solid #ddd; margin: 10px 0; }
            .day { font-weight: bold; color: #5c6ac4; margin-top: 15px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>👗 AINA Shopify-Tally Integration</h1>
            <p>Automated delivery channel detection via order tags</p>
            
            <div class="feature workflow">
                <h3>📅 Daily Workflow (How It Works)</h3>
                
                <div class="day">Day 1 (Jan 14) - Order Placed:</div>
                <div class="steps">
                    1. Customer places order<br>
                    2. Webhook fires → Order saved with <code>delivery_channel: "Pending"</code><br>
                    3. Order fulfilled but carrier not assigned yet
                </div>
                
                <div class="day">Day 2 (Jan 15) - Carrier Assigned:</div>
                <div class="steps">
                    1. Staff assigns carrier (DTDC/Delhivery/BlueDart)<br>
                    2. <strong>Staff adds tag to order</strong> (see instructions below)<br>
                    3. Webhook fires → Database automatically updates with correct carrier!
                </div>
                
                <div class="day">Day 3 (Jan 16) - Tally Sync:</div>
                <div class="steps">
                    1. Tally calls API for yesterday's orders (Jan 14)<br>
                    2. ✅ <strong>Response includes correct carrier</strong> (tags were added on Jan 15)<br>
                    3. Data syncs to Tally with proper voucher types
                </div>
            </div>
            
            <div class="feature">
                <h3>🏷️ How to Add Carrier Tags (For Staff)</h3>
                
                <p><strong>Method 1: Single Order</strong></p>
                <ol>
                    <li>Open the order in Shopify Admin</li>
                    <li>Scroll down to "Tags" section (right sidebar)</li>
                    <li>Type one of these exact tags:
                        <ul>
                            <li><code>carrier:DTDC</code></li>
                            <li><code>carrier:Delhivery</code></li>
                            <li><code>carrier:BlueDart</code></li>
                        </ul>
                    </li>
                    <li>Press Enter</li>
                    <li>Click "Save" (top right corner)</li>
                </ol>
                
                <p><strong>Method 2: Bulk Tagging (Faster for Multiple Orders)</strong></p>
                <ol>
                    <li>Go to Orders page</li>
                    <li>Select multiple orders using checkboxes</li>
                    <li>Click "More actions" dropdown</li>
                    <li>Select "Add tags"</li>
                    <li>Enter carrier tag (e.g., <code>carrier:DTDC</code>)</li>
                    <li>Apply to all selected orders</li>
                </ol>
            </div>
            
            <div class="feature warning">
                <h3>⚠️ Important: Webhook Setup Required</h3>
                <p>Make sure you have BOTH webhooks configured:</p>
                <ol>
                    <li><strong>Order creation:</strong> <code>POST /shopify/order</code> (✅ Already set up)</li>
                    <li><strong>Order updated:</strong> <code>POST /shopify/order</code> (⭐ Must add this!)</li>
                </ol>
                <p>Both should point to: <code>https://shopify-tally-middleware.onrender.com/shopify/order</code></p>
            </div>
            
            <div class="feature">
                <h3>✅ Voucher Classification</h3>
                <p><strong>Format:</strong> <code>Sales-{PaymentMethod}-{Carrier}</code></p>
                <p><strong>Examples:</strong></p>
                <ul>
                    <li><code>Sales-COD-DTDC</code></li>
                    <li><code>Sales-Prepaid-Delhivery</code></li>
                    <li><code>Sales-COD-BlueDart</code></li>
                    <li><code>Sales-Prepaid-Pending</code> (if tag not added yet)</li>
                </ul>
            </div>
            
            <div class="feature">
                <h3>✅ Client Requirements Met</h3>
                <ul>
                    <li>✅ COD vs Prepaid classification</li>
                    <li>✅ Sales discounts under Direct Expenses</li>
                    <li>✅ Shipping charges under Indirect Expenses</li>
                    <li>✅ GST split (rate with GST + rate without GST)</li>
                    <li>✅ CGST, SGST, IGST breakdown</li>
                    <li>✅ Three delivery channels for debtor tracking</li>
                </ul>
            </div>
            
            <div class="feature">
                <h3>📊 API Endpoints</h3>
                <p><strong>Order Webhook:</strong> POST /shopify/order (handles create AND update)</p>
                <p><strong>Fetch Orders for Tally:</strong> POST /tally/orders</p>
                <p><strong>Create Order in Shopify:</strong> POST /tally/sales</p>
            </div>
            
            <div class="feature">
                <h3>💡 Tips for Best Results</h3>
                <ul>
                    <li>Add tags <strong>every morning</strong> for yesterday's fulfilled orders</li>
                    <li>Use exact tag format: <code>carrier:DTDC</code> (case-sensitive)</li>
                    <li>Tags can be added immediately after fulfillment or next day</li>
                    <li>If tag is missed, add it anytime - it will update in database automatically</li>
                    <li>Tally should sync orders from yesterday (not today) to get tagged orders</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """
