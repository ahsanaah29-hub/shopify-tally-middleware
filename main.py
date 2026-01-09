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



SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------------------------------------
# GST helper (INR)
# -------------------------------------------------

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
    shipping_charge = float(shipping_lines[0]["price"]) if shipping_lines else 0

    shipping_tax = 0
    if shipping_lines and shipping_lines[0].get("tax_lines"):
        shipping_tax = float(shipping_lines[0]["tax_lines"][0]["price"])

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

        price_with_gst = float(li.get("price", 0))
        amount_with_gst = round(price_with_gst * qty, 2)

        tax_lines = li.get("tax_lines", [])
        tax = tax_lines[0] if tax_lines else {}

        gst_amount = float(tax.get("price", 0))
        gst_type = tax.get("title")

        amount_ex_gst = round(amount_with_gst - gst_amount, 2)

        cgst = sgst = igst = 0
        if gst_type == "IGST":
            igst = gst_amount
        elif gst_type == "CGST":
            cgst = gst_amount
        elif gst_type == "SGST":
            sgst = gst_amount

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "quantity": qty,
            "rate": round(amount_ex_gst / qty, 2),   # Ex-GST rate
            "amount": amount_with_gst,              # With GST
            "amount_ex_gst": amount_ex_gst,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "shipping_charge": shipping_charge,
            "shipping_gst": shipping_tax
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

        items = []
        total_ex_gst = 0
        total_gst = 0
        total_with_gst = 0

        for i in o["order_items"]:
            amount_ex_gst = float(i["amount_ex_gst"] or 0)
            amount_with_gst = float(i["amount"] or 0)

            gst_value = float(i["cgst"] or 0) + float(i["sgst"] or 0) + float(i["igst"] or 0)

            total_ex_gst += amount_ex_gst
            total_gst += gst_value
            total_with_gst += amount_with_gst

            items.append({
                "item_name": i["item_name"],
                "quantity": i["quantity"],
                "rate": i["rate"],
                "amount": amount_ex_gst,
                "amount_with_gst": amount_with_gst,
                "gst": {
                    "cgst": i["cgst"],
                    "sgst": i["sgst"],
                    "igst": i["igst"]
                }
            })

        # ✅ Shipping must be calculated per order
        shipping = float(o.get("shipping_charge") or 0)
        shipping_gst = float(o.get("shipping_gst") or 0)

        grand_total = total_with_gst + shipping

        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            "customer": {
                "name": o["customer_name"],
                "email": o["customer_email"],
                "phone": o["customer_phone"]
            },
            "items": items,

            "total_gst": round(total_gst + shipping_gst, 2),

            "shipping_charge": round(shipping, 2),
            "shipping_gst": round(shipping_gst, 2),

            "total_amount": round(total_ex_gst, 2),
            "total_amount_with_gst": round(total_with_gst, 2),

            "grand_total": round(grand_total, 2),

            "currency": o["currency"],
            "source": o["source"],
            "shopify_order_id": o["shopify_order_id"]
        })

    return {"orders": tally_orders}


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
    shop = request.query_params.get("shop", "aina-india")
    
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AINA - Shopify-Tally Sync</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            .header {
                background: white;
                padding: 32px;
                border-radius: 16px;
                margin-bottom: 24px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.1);
                text-align: center;
            }
            .header h1 {
                font-size: 2.5rem;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
                margin-bottom: 8px;
            }
            .header p {
                color: #6d7175;
                font-size: 1.1rem;
            }
            .status-badge {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: #d1f7e5;
                color: #008060;
                padding: 8px 20px;
                border-radius: 24px;
                font-weight: 600;
                margin-top: 16px;
            }
            .sync-flow {
                display: grid;
                grid-template-columns: 1fr auto 1fr;
                gap: 24px;
                margin-bottom: 32px;
                align-items: center;
            }
            .sync-box {
                background: white;
                padding: 32px;
                border-radius: 16px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                text-align: center;
            }
            .sync-box h3 {
                color: #202223;
                font-size: 1.5rem;
                margin-bottom: 12px;
            }
            .sync-box .icon {
                font-size: 3rem;
                margin-bottom: 16px;
            }
            .sync-arrow {
                font-size: 3rem;
                color: white;
                animation: pulse 2s infinite;
            }
            @keyframes pulse {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.7; transform: scale(1.1); }
            }
            .tabs {
                display: flex;
                gap: 12px;
                margin-bottom: 24px;
                flex-wrap: wrap;
            }
            .tab {
                padding: 14px 28px;
                background: white;
                border: none;
                border-radius: 12px;
                cursor: pointer;
                font-size: 1rem;
                font-weight: 500;
                transition: all 0.3s;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            }
            .tab:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            }
            .tab.active {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .tab-content {
                display: none;
            }
            .tab-content.active {
                display: block;
                animation: fadeIn 0.3s;
            }
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .card {
                background: white;
                padding: 32px;
                border-radius: 16px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.08);
                margin-bottom: 24px;
            }
            .card h2 {
                color: #202223;
                font-size: 1.8rem;
                margin-bottom: 24px;
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .form-group {
                margin-bottom: 24px;
            }
            .form-group label {
                display: block;
                margin-bottom: 8px;
                color: #202223;
                font-weight: 600;
                font-size: 0.95rem;
            }
            .form-group input,
            .form-group select {
                width: 100%;
                padding: 14px;
                border: 2px solid #e1e3e5;
                border-radius: 8px;
                font-size: 1rem;
                transition: border 0.3s;
            }
            .form-group input:focus,
            .form-group select:focus {
                outline: none;
                border-color: #667eea;
            }
            .form-row {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
            }
            .btn {
                padding: 14px 32px;
                border: none;
                border-radius: 8px;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
            }
            .btn-primary {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .btn-primary:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(102, 126, 234, 0.4);
            }
            .btn-secondary {
                background: #f6f6f7;
                color: #202223;
            }
            .btn-secondary:hover {
                background: #e1e3e5;
            }
            .items-section {
                border: 2px dashed #c9cccf;
                padding: 24px;
                border-radius: 12px;
                margin: 24px 0;
                background: #fafbfb;
            }
            .item-row {
                display: grid;
                grid-template-columns: 2fr 1fr 1fr 60px;
                gap: 16px;
                margin-bottom: 16px;
                align-items: end;
            }
            .remove-btn {
                background: #d82c0d;
                color: white;
                border: none;
                padding: 14px;
                border-radius: 8px;
                cursor: pointer;
                font-weight: bold;
            }
            .remove-btn:hover {
                background: #bf2600;
            }
            .success-msg {
                background: linear-gradient(135deg, #d1f7e5 0%, #a7f3d0 100%);
                color: #065f46;
                padding: 16px;
                border-radius: 8px;
                margin-top: 16px;
                display: none;
                font-weight: 500;
            }
            .error-msg {
                background: linear-gradient(135deg, #fed3d1 0%, #fca5a1 100%);
                color: #991b1b;
                padding: 16px;
                border-radius: 8px;
                margin-top: 16px;
                display: none;
                font-weight: 500;
            }
            .feature-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 20px;
                margin-top: 24px;
            }
            .feature-card {
                background: linear-gradient(135deg, #f6f6f7 0%, #ffffff 100%);
                padding: 24px;
                border-radius: 12px;
                border-left: 4px solid #667eea;
            }
            .feature-card h4 {
                color: #202223;
                font-size: 1.2rem;
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .feature-card p {
                color: #6d7175;
                line-height: 1.6;
            }
            .webhook-box {
                background: #1f2937;
                color: #10b981;
                padding: 20px;
                border-radius: 8px;
                font-family: 'Courier New', monospace;
                font-size: 0.95rem;
                margin: 16px 0;
                overflow-x: auto;
            }
            .info-badge {
                display: inline-block;
                background: #eff6ff;
                color: #1e40af;
                padding: 6px 14px;
                border-radius: 6px;
                font-size: 0.9rem;
                font-weight: 600;
                margin: 4px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>👗 AINA Clothing Store</h1>
                <p>Seamless Order Synchronization</p>
                <div class="status-badge">
                    <span>●</span> Active & Syncing
                </div>
            </div>

            <div class="sync-flow">
                <div class="sync-box">
                    <div class="icon">🛍️</div>
                    <h3>Shopify</h3>
                    <p style="color: #6d7175;">Online Store Orders</p>
                </div>
                <div class="sync-arrow">⇄</div>
                <div class="sync-box">
                    <div class="icon">📊</div>
                    <h3>Tally ERP</h3>
                    <p style="color: #6d7175;">Accounting System</p>
                </div>
            </div>

            <div class="tabs">
                <button class="tab active" onclick="showTab('create')">📝 Create Order</button>
                <button class="tab" onclick="showTab('sync')">🔄 Sync Status</button>
                <button class="tab" onclick="showTab('features')">✨ Features</button>
                <button class="tab" onclick="showTab('settings')">⚙️ Settings</button>
            </div>

            <!-- CREATE ORDER TAB -->
            <div id="create" class="tab-content active">
                <div class="card">
                    <h2>📝 Create New Clothing Order</h2>
                    <form id="orderForm" onsubmit="createOrder(event)">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Customer Name *</label>
                                <input type="text" id="customerName" required placeholder="Enter customer name">
                            </div>
                            <div class="form-group">
                                <label>Customer Email *</label>
                                <input type="email" id="customerEmail" required placeholder="customer@example.com">
                            </div>
                        </div>

                        <div class="form-group">
                            <label>Phone Number</label>
                            <input type="tel" id="customerPhone" placeholder="+91 9876543210">
                        </div>

                        <div class="items-section">
                            <h3 style="margin-bottom: 20px; color: #202223;">🛒 Order Items</h3>
                            <div id="itemsList">
                                <div class="item-row">
                                    <div class="form-group" style="margin: 0;">
                                        <label>Product/Garment Name</label>
                                        <input type="text" class="item-name" required placeholder="e.g., Cotton T-Shirt, Denim Jeans">
                                    </div>
                                    <div class="form-group" style="margin: 0;">
                                        <label>Quantity</label>
                                        <input type="number" class="item-quantity" required min="1" value="1">
                                    </div>
                                    <div class="form-group" style="margin: 0;">
                                        <label>Price (₹)</label>
                                        <input type="number" class="item-price" required min="0" step="0.01" placeholder="0.00">
                                    </div>
                                    <button type="button" class="remove-btn" onclick="removeItem(this)">✕</button>
                                </div>
                            </div>
                            <button type="button" class="btn btn-secondary" onclick="addItem()" style="margin-top: 12px;">
                                + Add More Items
                            </button>
                        </div>

                        <button type="submit" class="btn btn-primary" style="width: 100%;">
                            🚀 Create Order & Sync to Shopify
                        </button>
                    </form>

                    <div id="successMsg" class="success-msg"></div>
                    <div id="errorMsg" class="error-msg"></div>
                </div>
            </div>

            <!-- SYNC STATUS TAB -->
            <div id="sync" class="tab-content">
                <div class="card">
                    <h2>🔄 Two-Way Synchronization</h2>
                    <div class="feature-grid">
                        <div class="feature-card">
                            <h4>→ Shopify to Tally</h4>
                            <p>When orders are placed on your Shopify store, they automatically sync to Tally ERP with GST calculations.</p>
                            <div style="margin-top: 12px;">
                                <span class="info-badge">Auto-sync enabled</span>
                            </div>
                        </div>
                        <div class="feature-card">
                            <h4>← Tally to Shopify</h4>
                            <p>Create orders from this interface and they'll be pushed to Shopify with proper customer and product details.</p>
                            <div style="margin-top: 12px;">
                                <span class="info-badge">Manual sync</span>
                            </div>
                        </div>
                    </div>

                    <div style="margin-top: 32px;">
                        <h3 style="margin-bottom: 16px;">Webhook Configuration</h3>
                        <p style="color: #6d7175; margin-bottom: 12px;">Your active webhook endpoint:</p>
                        <div class="webhook-box">POST https://shopify-tally-middleware.onrender.com/shopify/order</div>
                        <p style="color: #6d7175; margin-top: 12px;">
                            <strong>Event:</strong> orders/create | <strong>Format:</strong> JSON
                        </p>
                    </div>
                </div>
            </div>

            <!-- FEATURES TAB -->
            <div id="features" class="tab-content">
                <div class="card">
                    <h2>✨ Key Features</h2>
                    <div class="feature-grid">
                        <div class="feature-card">
                            <h4>🧾 Auto GST Calculation</h4>
                            <p>Automatically calculates CGST (""" + str(GST_PERCENT/2) + """%) and SGST (""" + str(GST_PERCENT/2) + """%) for all orders synced to Tally.</p>
                        </div>
                        <div class="feature-card">
                            <h4>💰 INR Currency</h4>
                            <p>All transactions are processed in Indian Rupees (₹) with proper currency handling.</p>
                        </div>
                        <div class="feature-card">
                            <h4>📦 Order Management</h4>
                            <p>Complete order details including customer info, line items, and pricing sync seamlessly.</p>
                        </div>
                        <div class="feature-card">
                            <h4>🗄️ Supabase Storage</h4>
                            <p>All order data is securely stored in Supabase database with full order history.</p>
                        </div>
                        <div class="feature-card">
                            <h4>⚡ Real-time Sync</h4>
                            <p>Orders sync instantly via webhooks when created in Shopify.</p>
                        </div>
                        <div class="feature-card">
                            <h4>👥 Customer Tracking</h4>
                            <p>Track customer names, emails, and phone numbers across both systems.</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- SETTINGS TAB -->
            <div id="settings" class="tab-content">
                <div class="card">
                    <h2>⚙️ Configuration</h2>
                    <div class="feature-card" style="margin-bottom: 20px;">
                        <h4>Store Information</h4>
                        <p><strong>Store:</strong> """ + shop + """.myshopify.com</p>
                        <p><strong>API Version:</strong> """ + SHOPIFY_API_VERSION + """</p>
                        <p><strong>GST Rate:</strong> """ + str(GST_PERCENT) + """%</p>
                    </div>

                    <div class="feature-card">
                        <h4>API Endpoints</h4>
                        <p><strong>Shopify Webhook:</strong> /shopify/order</p>
                        <p><strong>Fetch Tally Orders:</strong> /tally/orders</p>
                        <p><strong>Push to Shopify:</strong> /tally/sales</p>
                    </div>
                </div>
            </div>
        </div>

        <script>
            function showTab(tabName) {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                
                event.target.classList.add('active');
                document.getElementById(tabName).classList.add('active');
            }

            function addItem() {
                const itemsList = document.getElementById('itemsList');
                const newItem = document.createElement('div');
                newItem.className = 'item-row';
                newItem.innerHTML = `
                    <div class="form-group" style="margin: 0;">
                        <input type="text" class="item-name" required placeholder="e.g., Cotton T-Shirt, Denim Jeans">
                    </div>
                    <div class="form-group" style="margin: 0;">
                        <input type="number" class="item-quantity" required min="1" value="1">
                    </div>
                    <div class="form-group" style="margin: 0;">
                        <input type="number" class="item-price" required min="0" step="0.01" placeholder="0.00">
                    </div>
                    <button type="button" class="remove-btn" onclick="removeItem(this)">✕</button>
                `;
                itemsList.appendChild(newItem);
            }

            function removeItem(btn) {
                if (document.querySelectorAll('.item-row').length > 1) {
                    btn.closest('.item-row').remove();
                }
            }

            async function createOrder(event) {
                event.preventDefault();
                
                const successMsg = document.getElementById('successMsg');
                const errorMsg = document.getElementById('errorMsg');
                successMsg.style.display = 'none';
                errorMsg.style.display = 'none';

                const submitBtn = event.target.querySelector('button[type="submit"]');
                submitBtn.disabled = true;
                submitBtn.textContent = '⏳ Creating Order...';

                const customerName = document.getElementById('customerName').value;
                const customerEmail = document.getElementById('customerEmail').value;
                const customerPhone = document.getElementById('customerPhone').value;

                const items = [];
                document.querySelectorAll('.item-row').forEach(row => {
                    items.push({
                        product_name: row.querySelector('.item-name').value,
                        quantity: parseInt(row.querySelector('.item-quantity').value),
                        rate: parseFloat(row.querySelector('.item-price').value)
                    });
                });

                const payload = {
                    customer: {
                        name: customerName,
                        email: customerEmail,
                        phone: customerPhone
                    },
                    items: items
                };

                try {
                    const response = await fetch('/tally/sales', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });

                    const result = await response.json();

                    if (response.ok) {
                        successMsg.textContent = `✅ Success! Order created in Shopify with ID: ${result.shopify_order_id}`;
                        successMsg.style.display = 'block';
                        document.getElementById('orderForm').reset();
                        
                        // Scroll to success message
                        successMsg.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    } else {
                        throw new Error(result.detail || 'Failed to create order');
                    }
                } catch (error) {
                    errorMsg.textContent = `❌ Error: ${error.message}`;
                    errorMsg.style.display = 'block';
                    errorMsg.scrollIntoView({ behavior: 'smooth', block: 'center' });
                } finally {
                    submitBtn.disabled = false;
                    submitBtn.textContent = '🚀 Create Order & Sync to Shopify';
                }
            }
        </script>
    </body>
    </html>
    """




