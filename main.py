import urllib.parse
import re
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
def extract_order_number_from_note(note: str):
    """
    Extract order number from notes like:
    - "This is an exchange order against #184055"
    - "This is a redispatch order against #184055"
    
    Returns: "184055" or None if not found
    """
    if not note:
        return None
    
    # Pattern to match: "against #NUMBER" or "against ORDER_NUMBER"
    match = re.search(r'against\s+#?(\d+)', note, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def determine_order_type(order):
    """
    Determine order type: 'sales', 'cancelled', 'return', 'exchange', or 'redispatch'
    
    Priority order:
    1. Cancelled orders (cancel_reason or cancelled_at)
    2. Exchange orders (note contains "exchange order against")
    3. Redispatch orders (note contains "redispatch order against")
    4. Return orders (tag "return" or fulfillment status = "returned")
    5. Default: sales
    """
    # Check if order is cancelled
    cancel_reason = order.get("cancel_reason")
    cancelled_at = order.get("cancelled_at")
    
    if cancel_reason or cancelled_at:
        return "cancelled", None
    
    # Check order notes for exchange/redispatch
    note = (order.get("note") or "").lower()
    
    # Check for exchange order
    if "exchange order against" in note:
        against_id = extract_order_number_from_note(order.get("note", ""))
        return "exchange", against_id
    
    # Check for redispatch order
    if "redispatch order against" in note:
        against_id = extract_order_number_from_note(order.get("note", ""))
        return "redispatch", against_id
    
    # Check for return order (tag-based)
    tags = (order.get("tags") or "").lower()
    if "return" in tags or "returned" in tags:
        return "return", None
    
    # Check fulfillment status for returns
    fulfillments = order.get("fulfillments", [])
    for f in fulfillments:
        status = (f.get("status") or "").lower()
        if status == "returned" or "return" in status.lower():
            return "return", None
    
    # Default to sales
    return "sales", None


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
    tags = (order.get("tags") or "").lower()
    if "dtdc" in tags or "carrier:dtdc" in tags:
        return "DTDC"
    if "delhivery" in tags or "carrier:delhivery" in tags:
        return "Delhivery"
    if "bluedart" in tags or "blue dart" in tags or "carrier:bluedart" in tags:
        return "BlueDart"
    
    # Method 2: Check fulfillments (tracking companies)
    fulfillments = order.get("fulfillments") or []
    for f in fulfillments:
        tracking_company = (f.get("tracking_company") or "").lower()
        if "dtdc" in tracking_company:
            return "DTDC"
        if "delhivery" in tracking_company:
            return "Delhivery"
        if "bluedart" in tracking_company or "blue dart" in tracking_company:
            return "BlueDart"
    
    # Method 3: Check shipping lines
    shipping_lines = order.get("shipping_lines") or []
    for s in shipping_lines:
        carrier = (s.get("code") or "").lower()
        title = (s.get("title") or "").lower()
        
        if "dtdc" in carrier or "dtdc" in title:
            return "DTDC"
        if "delhivery" in carrier or "delhivery" in title:
            return "Delhivery"
        if "bluedart" in carrier or "blue dart" in carrier or "bluedart" in title or "blue dart" in title:
            return "BlueDart"
    
    # Method 4: Check order notes
    note = (order.get("note") or "").lower()
    if "dtdc" in note:
        return "DTDC"
    if "delhivery" in note:
        return "Delhivery"
    if "bluedart" in note or "blue dart" in note:
        return "BlueDart"
    
    # Method 5: Check note attributes (custom fields)
    note_attributes = order.get("note_attributes") or []
    for attr in note_attributes:
        value = str(attr.get("value") or "").lower()
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
    2. When order is updated (tags, fulfillment, cancelled, returned, notes changed, etc.)
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
    delivery_channel = determine_delivery_channel(order)
    
    # ✅ NEW: Now returns tuple (type, against_order_id)
    order_type, against_order_id = determine_order_type(order)

    # ✅ UPSERT: Creates new order OR updates existing one
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
            "type": order_type,  # ✅ sales, cancelled, return, exchange, redispatch
            "against_order_id": against_order_id,  # ✅ For exchange/redispatch
            "currency": order.get("currency", "INR"),
            "source": "Shopify",
            "raw_order": order
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

        # ✅ NEW: Extract item code and size from variant data
        item_code = li.get("sku") or li.get("id")  # Use SKU if available, else use product ID
        item_size = None
        
        # Try to extract size from variant title or properties
        variant_title = li.get("variant_title") or ""
        if variant_title:
            # Sort size keywords by length (longest first) to match "2XS", "2XL" before "XS", "XL"
            size_keywords = ["XXXL", "XXL", "XXS", "2XL", "3XL", "4XL", "XL", "XS", "S", "M", "L"]
            for size in size_keywords:
                if size.upper() in variant_title.upper():
                    item_size = size
                    break
        
        # If not found in variant title, check properties
        if not item_size:
            properties = li.get("properties") or []
            for prop in properties:
                if prop.get("name") and "size" in prop.get("name", "").lower():
                    item_size = prop.get("value")
                    break
        
        # ✅ NEW: Calculate GST percentage
        gst_percentage = round((gst_amount / amount_ex_gst * 100), 2) if amount_ex_gst > 0 else 0

        supabase.table("order_items").insert({
            "order_id": order_id,
            "item_name": li.get("title"),
            "item_code": item_code,  # ✅ NEW: SKU or Product ID
            "item_size": item_size,  # ✅ NEW: Size (XL, M, etc.)
            "quantity": qty,
            "rate": round(original_rate_with_gst, 2),
            "amount": amount_with_gst,
            "amount_ex_gst": amount_ex_gst,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "gst_percentage": gst_percentage,  # ✅ NEW: GST percentage (5, 12, 18, 28%)
            "item_discount": round(discount, 2)
        }).execute()

    return {
        "status": "stored", 
        "delivery_channel": delivery_channel,
        "order_type": order_type,
        "against_order_id": against_order_id,  # ✅ Return against_order_id
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
        order_type, against_order_id = determine_order_type(order)  # ✅ NEW: Check order type
        
        # Update database
        result = supabase.table("orders") \
            .update({
                "delivery_channel": delivery_channel,
                "type": order_type,
                "against_order_id": against_order_id,  # ✅ NEW: Update against_order_id
                "raw_order": order
            }) \
            .eq("shopify_order_id", order_id) \
            .execute()
        
        return {
            "status": "success",
            "order_id": order_id,
            "delivery_channel": delivery_channel,
            "order_type": order_type,
            "against_order_id": against_order_id,  # ✅ NEW: Return against_order_id
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
    Also updates order types and against_order_id.
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
        order_type, against_order_id = determine_order_type(order)  # ✅ NEW: Update type
        
        # Only update if no longer pending
        if delivery_channel != "Pending":
            supabase.table("orders") \
                .update({
                    "delivery_channel": delivery_channel,
                    "type": order_type,
                    "against_order_id": against_order_id,  # ✅ NEW: Update against_order_id
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
# NEW: Fix old orders with wrong channels
# -------------------------------------------------
@app.post("/fix/old-orders")
async def fix_old_orders():
    """
    Reset all orders that have incorrect delivery channels from old code.
    Changes "Website", "Marketplace", "Social-Media" → "Pending"
    Also sets order type and against_order_id based on current Shopify data.
    """
    # Get orders with old wrong channels
    wrong_channels = ["Website", "Marketplace", "Social-Media"]
    
    total_fixed = 0
    
    for wrong_channel in wrong_channels:
        res = supabase.table("orders") \
            .select("id, order_number, raw_order") \
            .eq("delivery_channel", wrong_channel) \
            .execute()
        
        for order_record in res.data:
            # Re-check the raw_order data with new logic
            raw_order = order_record.get("raw_order", {})
            
            if raw_order:
                # Use the new detection logic
                new_channel = determine_delivery_channel(raw_order)
                new_type, new_against_id = determine_order_type(raw_order)  # ✅ NEW
            else:
                # If no raw_order, default to Pending and sales
                new_channel = "Pending"
                new_type = "sales"
                new_against_id = None
            
            # Update the order
            supabase.table("orders") \
                .update({
                    "delivery_channel": new_channel,
                    "type": new_type,
                    "against_order_id": new_against_id  # ✅ NEW
                }) \
                .eq("id", order_record["id"]) \
                .execute()
            
            total_fixed += 1
    
    return {
        "status": "fix_complete",
        "total_orders_fixed": total_fixed,
        "message": "Old orders updated with new type detection logic."
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
        
        # ✅ NEW: For exchange/redispatch, get customer from original order if missing
        customer_name = o["customer_name"]
        customer_email = o["customer_email"]
        customer_phone = o["customer_phone"]
        
        # If customer info is missing and it's exchange/redispatch, try to get from original order
        if (not customer_name or customer_name == "Unknown Customer") and o.get("against_order_id"):
            try:
                # Fetch original order from database
                original_order_res = supabase.table("orders") \
                    .select("customer_name, customer_email, customer_phone") \
                    .eq("order_number", o.get("against_order_id")) \
                    .limit(1) \
                    .execute()
                
                if original_order_res.data:
                    orig = original_order_res.data[0]
                    customer_name = orig.get("customer_name") or customer_name
                    customer_email = orig.get("customer_email") or customer_email
                    customer_phone = orig.get("customer_phone") or customer_phone
            except:
                pass  # If lookup fails, use current order's info
        
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

            # ✅ FIXED: Extract item_size from variant_title (prefer longer matches like 2XS over XS)
            item_size = None
            variant_title = li.get("variant_title") or ""
            
            if variant_title:
                # Sort size keywords by length (longest first) to match "2XS" before "XS"
                size_keywords = ["XXXL", "XXL", "XXS", "2XL", "3XL", "4XL", "XL", "XS", "S", "M", "L"]
                
                # First try direct match
                for size in size_keywords:
                    if size.upper() == variant_title.upper():
                        item_size = size
                        break
                
                # If not found as direct match, search within the text (will match longest first due to sorting)
                if not item_size:
                    for size in size_keywords:
                        if size.upper() in variant_title.upper():
                            item_size = size
                            break

            items.append({
                "item_code": li.get("sku") or li.get("id"),
                "item_name": li["title"],
                "item_size": item_size,  # ✅ Now correctly extracts size
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
                    "total": round(gst, 2),
                    "percentage": round((gst / amount_ex_gst * 100), 2) if amount_ex_gst > 0 else 0
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
        order_type = o.get("type", "sales")
        against_order_id = o.get("against_order_id")  # ✅ NEW: Get against_order_id
        
        # ✅ Fixed voucher type format (now includes order type)
        voucher_type = f"{order_type.capitalize()}-{payment_method}-{delivery_channel}"
        # Examples: 
        # "Sales-COD-DTDC"
        # "Sales-Prepaid-Delhivery"
        # "Cancelled-COD-BlueDart"
        # "Return-Prepaid-DTDC"
        # "Exchange-Prepaid-Delhivery"
        # "Redispatch-COD-DTDC"

        order_data = {
            "voucher_type": voucher_type,
            "order_type": order_type,
            "payment_method": payment_method,
            "delivery_channel": delivery_channel,
            
            "voucher_number": o["order_number"],
            "voucher_date": o["voucher_date"],
            
            "customer": {
                "name": customer_name,  # ✅ Now uses original customer if exchange/redispatch
                "email": customer_email,
                "phone": customer_phone
            },
            
            "items": items,
            
            # ✅ FLATTENED EXPENSES INTO SUMMARY
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
        
        # ✅ NEW: Add against_order_id only for exchange and redispatch
        if order_type in ["exchange", "redispatch"] and against_order_id:
            order_data["against_order_id"] = against_order_id

        tally_orders.append(order_data)

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
            .exchange { background: #e3f2fd; border-left-color: #2196f3; }
            .redispatch { background: #f3e5f5; border-left-color: #9c27b0; }
            code { background: #e1e3e5; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
            ul { margin: 10px 0; }
            .steps { background: #fff; padding: 15px; border-radius: 5px; border: 1px solid #ddd; margin: 10px 0; }
            .day { font-weight: bold; color: #5c6ac4; margin-top: 15px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>👗 AINA Shopify-Tally Integration</h1>
            <p>Automated order type detection + delivery channel tracking (sales/cancelled/return/exchange/redispatch)</p>
            
            <div class="feature workflow">
                <h3>📅 Daily Workflow (How It Works)</h3>
                
                <div class="day">Day 1 - Order Created:</div>
                <div class="steps">
                    Customer places order → Webhook fires → Order saved as <code>type: "sales"</code>
                </div>
                
                <div class="day">Day 2+ - Order Status Changes:</div>
                <div class="steps">
                    <strong>Scenario 1 (Normal Sale):</strong> Staff adds carrier tag → <code>type: "sales"</code>, carrier assigned<br><br>
                    <strong>Scenario 2 (Cancellation):</strong> Order cancelled → Webhook fires → <code>type: "cancelled"</code> (automatic)<br><br>
                    <strong>Scenario 3 (Return/Refund):</strong> Staff adds tag <code>return</code> → <code>type: "return"</code> (automatic)<br><br>
                    <strong>Scenario 4 (Exchange):</strong> Staff adds note <code>"This is an exchange order against #184055"</code> → <code>type: "exchange"</code>, <code>against_order_id: "184055"</code><br><br>
                    <strong>Scenario 5 (Redispatch):</strong> Staff adds note <code>"This is a redispatch order against #184055"</code> → <code>type: "redispatch"</code>, <code>against_order_id: "184055"</code>
                </div>
            </div>
            
            <div class="feature exchange">
                <h3>🔄 Exchange Orders</h3>
                
                <p><strong>What is an exchange order?</strong></p>
                <p>A customer returns an item and receives a different/replacement item. Example: Customer bought size M shirt but exchanges for size L shirt.</p>
                
                <p><strong>How to mark in Shopify:</strong></p>
                <ol>
                    <li>Open the NEW order (the exchange order)</li>
                    <li>Scroll to "Notes" section</li>
                    <li>Add note: <code>This is an exchange order against #184055</code></li>
                    <li>Click "Save"</li>
                </ol>
                
                <p><strong>System automatically detects:</strong></p>
                <ul>
                    <li>✅ <code>type: "exchange"</code></li>
                    <li>✅ <code>against_order_id: "184055"</code> (extracted from note)</li>
                    <li>✅ Links to original order #184055</li>
                </ul>
                
                <p><strong>Tally Response:</strong></p>
                <pre>{ "order_type": "exchange", "against_order_id": "184055", "voucher_type": "Exchange-COD-DTDC" }</pre>
            </div>
            
            <div class="feature redispatch">
                <h3>📦 Redispatch Orders</h3>
                
                <p><strong>What is a redispatch order?</strong></p>
                <p>Original order delivery failed/was lost. A new shipment is sent to customer. Example: Package #184055 was lost, so new order #184060 is dispatched to same customer.</p>
                
                <p><strong>How to mark in Shopify:</strong></p>
                <ol>
                    <li>Open the NEW order (the redispatch order)</li>
                    <li>Scroll to "Notes" section</li>
                    <li>Add note: <code>This is a redispatch order against #184055</code></li>
                    <li>Click "Save"</li>
                </ol>
                
                <p><strong>System automatically detects:</strong></p>
                <ul>
                    <li>✅ <code>type: "redispatch"</code></li>
                    <li>✅ <code>against_order_id: "184055"</code> (extracted from note)</li>
                    <li>✅ Links to original order #184055</li>
                </ul>
                
                <p><strong>Tally Response:</strong></p>
                <pre>{ "order_type": "redispatch", "against_order_id": "184055", "voucher_type": "Redispatch-Prepaid-Delhivery" }</pre>
            </div>
            
            <div class="feature">
                <h3>🏷️ All Order Types & Detection</h3>
                
                <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
                    <tr style="background: #f5f5f5; border-bottom: 2px solid #5c6ac4;">
                        <th style="padding: 10px; text-align: left; border: 1px solid #ddd;">Type</th>
                        <th style="padding: 10px; text-align: left; border: 1px solid #ddd;">How Staff Marks It</th>
                        <th style="padding: 10px; text-align: left; border: 1px solid #ddd;">Detection Logic</th>
                    </tr>
                    <tr style="border-bottom: 1px solid #ddd;">
                        <td style="padding: 10px; border: 1px solid #ddd;"><strong>Sales</strong></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">No action needed (default)</td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Default type for all orders</td>
                    </tr>
                    <tr style="background: #fee; border-bottom: 1px solid #ddd;">
                        <td style="padding: 10px; border: 1px solid #ddd;"><strong>Cancelled</strong></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Shopify "Cancel Order" button</td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Automatic (cancel_reason or cancelled_at)</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #ddd;">
                        <td style="padding: 10px; border: 1px solid #ddd;"><strong>Return</strong></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Add tag <code>return</code></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Tag "return" OR fulfillment status = "returned"</td>
                    </tr>
                    <tr style="background: #e3f2fd; border-bottom: 1px solid #ddd;">
                        <td style="padding: 10px; border: 1px solid #ddd;"><strong>Exchange</strong></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Add note: <code>This is an exchange order against #ORDER</code></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Note contains "exchange order against"</td>
                    </tr>
                    <tr style="background: #f3e5f5; border-bottom: 1px solid #ddd;">
                        <td style="padding: 10px; border: 1px solid #ddd;"><strong>Redispatch</strong></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Add note: <code>This is a redispatch order against #ORDER</code></td>
                        <td style="padding: 10px; border: 1px solid #ddd;">Note contains "redispatch order against"</td>
                    </tr>
                </table>
            </div>
            
            <div class="feature">
                <h3>💡 Voucher Types & Examples</h3>
                <p><strong>Format:</strong> <code>{OrderType}-{PaymentMethod}-{DeliveryChannel}</code></p>
                
                <p><strong>Sales Examples:</strong></p>
                <ul>
                    <li><code>Sales-COD-DTDC</code></li>
                    <li><code>Sales-Prepaid-Delhivery</code></li>
                    <li><code>Sales-Prepaid-Pending</code> (carrier not assigned yet)</li>
                </ul>
                
                <p><strong>Special Order Examples:</strong></p>
                <ul>
                    <li><code>Cancelled-COD-BlueDart</code> (cancelled order)</li>
                    <li><code>Return-Prepaid-DTDC</code> (return/refund)</li>
                    <li><code>Exchange-COD-Delhivery</code> (exchange with carrier assigned)</li>
                    <li><code>Redispatch-Prepaid-DTDC</code> (redispatch with carrier assigned)</li>
                </ul>
            </div>
            
            <div class="feature warning">
                <h3>⚠️ Important Setup Instructions</h3>
                <p>Make sure you have BOTH webhooks configured:</p>
                <ol>
                    <li><strong>Order creation:</strong> <code>POST /shopify/order</code> (✅ Already set up)</li>
                    <li><strong>Order updated:</strong> <code>POST /shopify/order</code> (⭐ Must add this!)</li>
                </ol>
                <p>Both should point to: <code>https://shopify-tally-middleware.onrender.com/shopify/order</code></p>
                <p>This ensures all status changes (cancellations, returns, notes) are properly tracked!</p>
            </div>
            
            <div class="feature">
                <h3>📊 API Endpoints</h3>
                <ul>
                    <li><strong>POST /shopify/order</strong> - Webhook for all order changes</li>
                    <li><strong>POST /tally/orders</strong> - Fetch orders for Tally (date range)</li>
                    <li><strong>POST /tally/sales</strong> - Create order in Shopify</li>
                    <li><strong>POST /sync/delivery-channels</strong> - Sync pending deliveries</li>
                    <li><strong>POST /fix/old-orders</strong> - Fix old orders with wrong types</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """
