import urllib.parse
from fastapi.responses import HTMLResponse

from fastapi.responses import RedirectResponse
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

GST_PERCENT = float(os.getenv("GST_PERCENT", "18.0"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------------------------------------
# GST helper (INR)
# -------------------------------------------------
def calculate_gst(amount: float):
    gst_total = round((amount * GST_PERCENT) / 100, 2)
    return {
        "cgst": round(gst_total / 2, 2),
        "sgst": round(gst_total / 2, 2),
        "igst": 0.0
    }

# -------------------------------------------------
# Shopify → Middleware (Webhook → Supabase)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    order = await request.json()

    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or {}

    # ✅ SAFE CUSTOMER NAME LOGIC
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

    # ✅ PRESENTMENT MONEY (INR)
    presentment_total = (
        order.get("total_price_set", {})
             .get("presentment_money", {})
    )

    res = supabase.table("orders").upsert(
        {
            "shopify_order_id": order.get("id"),
            "order_number": str(order.get("order_number")),
            "voucher_date": order.get("created_at")[:10],
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "total_amount": float(presentment_total.get("amount", 0)),
            "currency": presentment_total.get("currency_code", "INR"),
            "source": "Shopify",
            "raw_order": order
        },
        on_conflict="shopify_order_id"
    ).execute()

    order_id = res.data[0]["id"]

    supabase.table("order_items") \
        .delete() \
        .eq("order_id", order_id) \
        .execute()

    # ✅ LINE ITEMS – PRESENTMENT PRICE (INR)
    for li in order.get("line_items", []):
        qty = li.get("quantity", 0)

        price_set = (
            li.get("price_set", {})
              .get("presentment_money", {})
        )

        rate = float(price_set.get("amount", 0))
        amount = qty * rate
        gst = calculate_gst(amount)

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "quantity": qty,
            "rate": round(rate, 2),
            "amount": round(amount, 2),
            "cgst": gst["cgst"],
            "sgst": gst["sgst"],
            "igst": gst["igst"]
        }).execute()

    return {"status": "stored"}

# -------------------------------------------------
# Tally → Fetch Orders
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
        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            "customer": {
                "name": o["customer_name"],
                "email": o["customer_email"],
                "phone": o["customer_phone"]
            },
            "items": [
                {
                    "item_name": i["item_name"],
                    "quantity": i["quantity"],
                    "rate": i["rate"],
                    "amount": i["amount"],
                    "gst": {
                        "cgst": i["cgst"],
                        "sgst": i["sgst"],
                        "igst": i["igst"]
                    }
                }
                for i in o["order_items"]
            ],
            "total_amount": o["total_amount"],
            "currency": o["currency"],
            "source": o["source"],
            "shopify_order_id": o["shopify_order_id"]
        })

    return {"orders": tally_orders}

# -------------------------------------------------
# Tally → Shopify (Sales Push)
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

    # ✅ CUSTOMER NAME HANDLING
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
            "price": round(item["rate"], 2)  # INR
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
# Shopify OAuth – Install App
# -------------------------------------------------
SHOPIFY_API_KEY = os.getenv("SHOPIFY_API_KEY", "").strip()
SHOPIFY_API_SECRET = os.getenv("SHOPIFY_API_SECRET", "").strip()

SCOPES = "read_orders,read_products,read_customers,write_orders"

REDIRECT_URI = (
    "https://shopify-tally-middleware.onrender.com/auth/callback"
)

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


# -------------------------------------------------
# Shopify OAuth – Callback
# -------------------------------------------------
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

    # 👉 For now just return it (later we store in Supabase)
    return {
        "status": "app_installed",
        "shop": shop,
        "access_token_received": bool(access_token)
    }

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # Get shop parameter if embedded
    shop = request.query_params.get("shop", "your-store")
    
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shopify-Tally Integration</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 40px 20px;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
            }
            .header {
                background: linear-gradient(135deg, #5e72e4 0%, #825ee4 100%);
                padding: 40px;
                color: white;
                text-align: center;
            }
            .header h1 {
                font-size: 2.5rem;
                margin-bottom: 10px;
            }
            .status-badge {
                display: inline-block;
                background: rgba(255,255,255,0.2);
                padding: 8px 16px;
                border-radius: 20px;
                font-size: 0.9rem;
                margin-top: 10px;
            }
            .content {
                padding: 40px;
            }
            .card {
                background: #f8f9fa;
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 20px;
                border-left: 4px solid #5e72e4;
            }
            .card h3 {
                color: #2d3748;
                margin-bottom: 12px;
                font-size: 1.3rem;
            }
            .card p {
                color: #4a5568;
                line-height: 1.6;
                margin-bottom: 8px;
            }
            .endpoint {
                background: #2d3748;
                color: #48bb78;
                padding: 12px;
                border-radius: 6px;
                font-family: 'Courier New', monospace;
                margin-top: 10px;
                font-size: 0.9rem;
            }
            .success { color: #48bb78; font-weight: 600; }
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 16px;
                margin-top: 20px;
            }
            .stat {
                background: white;
                padding: 20px;
                border-radius: 8px;
                text-align: center;
                border: 2px solid #e2e8f0;
            }
            .stat-value {
                font-size: 1.8rem;
                font-weight: bold;
                color: #5e72e4;
            }
            .stat-label {
                color: #718096;
                margin-top: 4px;
                font-size: 0.9rem;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🔗 Shopify-Tally Integration</h1>
                <div class="status-badge">✅ Active & Running</div>
            </div>
            
            <div class="content">
                <div class="card">
                    <h3>📊 Integration Status</h3>
                    <p class="success">Your Shopify-Tally middleware is successfully connected!</p>
                    <p>This app automatically syncs orders between Shopify and Tally ERP.</p>
                </div>

                <div class="card">
                    <h3>⚙️ Configuration</h3>
                    <p><strong>Store:</strong> """ + shop + """</p>
                    <p><strong>API Version:</strong> """ + SHOPIFY_API_VERSION + """</p>
                    <p><strong>GST Rate:</strong> """ + str(GST_PERCENT) + """%</p>
                </div>

                <div class="card">
                    <h3>🔔 Webhook Setup</h3>
                    <p>Configure the following webhook in your Shopify admin:</p>
                    <div class="endpoint">POST https://shopify-tally-middleware.onrender.com/shopify/order</div>
                    <p style="margin-top:12px;"><strong>Event:</strong> Order creation (orders/create)</p>
                    <p><strong>Format:</strong> JSON</p>
                </div>

                <div class="card">
                    <h3>📡 Available Endpoints</h3>
                    <div class="grid">
                        <div class="stat">
                            <div class="stat-value">POST</div>
                            <div class="stat-label">/shopify/order</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value">POST</div>
                            <div class="stat-label">/tally/orders</div>
                        </div>
                        <div class="stat">
                            <div class="stat-value">POST</div>
                            <div class="stat-label">/tally/sales</div>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <h3>📚 Next Steps</h3>
                    <p>1. Configure webhook in Shopify Settings → Notifications</p>
                    <p>2. Test with a sample order</p>
                    <p>3. Verify data sync in Supabase database</p>
                    <p>4. Connect Tally ERP for bidirectional sync</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """




