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

SHOPIFY_TOKEN = os.getenv(
    "SHOPIFY_ACCESS_TOKEN",
    ""
).strip()

SHOPIFY_STORE = os.getenv(
    "SHOPIFY_STORE_NAME",
    ""
).strip()

SHOPIFY_API_VERSION = os.getenv(
    "SHOPIFY_API_VERSION",
    "2025-01"
).strip()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# =================================================
# Helper Functions
# =================================================


def extract_order_number_from_note(note: str):
    """
    Extract order number from notes like:
    - "This is an exchange order against #184055"
    - "This is a redispatch order against #184055"

    Returns: "184055" or None if not found
    """

    if not note:
        return None

    match = re.search(
        r'against\s+#?(\d+)',
        note,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None


# -------------------------------------------------
# Determine Order Type
# -------------------------------------------------

def determine_order_type(order):
    """
    Determine order type:
    'sales', 'cancelled', 'return',
    'exchange', or 'redispatch'

    Priority:
    1. Cancelled
    2. Exchange
    3. Redispatch
    4. Return
    5. Sales
    """

    # Check if order is cancelled

    cancel_reason = order.get("cancel_reason")
    cancelled_at = order.get("cancelled_at")

    if cancel_reason or cancelled_at:
        return "cancelled", None

    # Check order notes

    note = (
        order.get("note") or ""
    ).lower()

    # Exchange

    if "exchange order against" in note:

        against_id = extract_order_number_from_note(
            order.get("note", "")
        )

        return "exchange", against_id

    # Redispatch

    if "redispatch order against" in note:

        against_id = extract_order_number_from_note(
            order.get("note", "")
        )

        return "redispatch", against_id

    # Return tag

    tags = (
        order.get("tags") or ""
    ).lower()

    if "return" in tags or "returned" in tags:
        return "return", None

    # Return fulfillment status

    fulfillments = (
        order.get("fulfillments") or []
    )

    for f in fulfillments:

        status = (
            f.get("status") or ""
        ).lower()

        if (
            status == "returned"
            or "return" in status
        ):
            return "return", None

    # Default

    return "sales", None


# -------------------------------------------------
# Determine Payment Method
# -------------------------------------------------

def determine_payment_method(order):
    """
    Determine if order is COD or Prepaid.
    Returns: 'COD' or 'Prepaid'
    """

    gateway = (
        order.get("gateway", "")
        .lower()
    )

    if (
        "cash on delivery" in gateway
        or "cod" in gateway
    ):
        return "COD"

    financial_status = (
        order.get("financial_status", "")
        .lower()
    )

    if financial_status == "pending":
        return "COD"

    payment_gateway_names = (
        order.get("payment_gateway_names", [])
    )

    for pg in payment_gateway_names:

        if (
            "cash" in pg.lower()
            or "cod" in pg.lower()
        ):
            return "COD"

    if financial_status in [
        "paid",
        "authorized",
        "partially_paid"
    ]:
        return "Prepaid"

    return "Prepaid"


# =================================================
# GoKwik Carrier Detection
# =================================================

def get_gokwik_carrier(tracking_number):
    """
    Fetch carrier from GoKwik tracking API
    using the tracking/AWB number.
    """

    if not tracking_number:
        return None

    try:

        url = (
            "https://api.gokwik.co/"
            "kwikship/track/v2/public"
        )

        params = {
            "order_code": tracking_number
        }

        response = requests.get(
            url,
            params=params,
            timeout=10
        )

        print(
            "GOKWIK RESPONSE STATUS:",
            response.status_code
        )

        if response.status_code != 200:

            print(
                "GOKWIK ERROR:",
                response.text
            )

            return None

        data = (
            response.json().get("data")
            or {}
        )

        shipper_info = (
            data.get("shipper_info")
            or {}
        )

        carrier = (
            shipper_info.get(
                "master_shipper_name"
            )
            or shipper_info.get(
                "shipper_name"
            )
            or ""
        ).strip()

        print(
            "GOKWIK CARRIER:",
            carrier
        )

        carrier_lower = carrier.lower()

        if "dtdc" in carrier_lower:
            return "DTDC"

        if "delhivery" in carrier_lower:
            return "Delhivery"

        if (
            "blue dart" in carrier_lower
            or "bluedart" in carrier_lower
        ):
            return "BlueDart"

        return carrier or None

    except Exception as e:

        print(
            "GOKWIK CARRIER ERROR:",
            str(e)
        )

        return None


# =================================================
# Determine Delivery Channel
# =================================================

def determine_delivery_channel(order):
    """
    Identify delivery channel from Shopify + GoKwik.

    Returns:
    DTDC
    Delhivery
    BlueDart
    Pending
    """

    # -------------------------------------------------
    # Method 1: Shopify order tags
    # -------------------------------------------------

    tags = (
        order.get("tags") or ""
    ).lower()

    if "dtdc" in tags:
        return "DTDC"

    if "delhivery" in tags:
        return "Delhivery"

    if (
        "bluedart" in tags
        or "blue dart" in tags
    ):
        return "BlueDart"

    # -------------------------------------------------
    # Method 2: Shopify fulfillments
    # -------------------------------------------------

    fulfillments = (
        order.get("fulfillments") or []
    )

    for f in fulfillments:

        tracking_company = (
            f.get("tracking_company") or ""
        ).strip().lower()

        tracking_number = (
            f.get("tracking_number") or ""
        ).strip()

        tracking_url = (
            f.get("tracking_url") or ""
        ).strip().lower()

        print(
            "Carrier check:",
            "tracking_number =",
            tracking_number,
            "tracking_company =",
            tracking_company,
            "tracking_url =",
            tracking_url
        )

        # Shopify carrier

        if "dtdc" in tracking_company:
            return "DTDC"

        if "delhivery" in tracking_company:
            return "Delhivery"

        if (
            "bluedart" in tracking_company
            or "blue dart" in tracking_company
        ):
            return "BlueDart"

        # Tracking URL

        if "dtdc" in tracking_url:
            return "DTDC"

        if "delhivery" in tracking_url:
            return "Delhivery"

        if (
            "bluedart" in tracking_url
            or "blue-dart" in tracking_url
        ):
            return "BlueDart"

        # -------------------------------------------------
        # Shopify carrier missing → Check GoKwik
        # -------------------------------------------------

        if tracking_number:

            print(
                "Shopify carrier missing. "
                "Checking GoKwik for:",
                tracking_number
            )

            gokwik_carrier = get_gokwik_carrier(
                tracking_number
            )

            print(
                "GoKwik carrier:",
                gokwik_carrier
            )

            if gokwik_carrier:
                return gokwik_carrier

    # -------------------------------------------------
    # Method 3: Shipping lines
    # -------------------------------------------------

    shipping_lines = (
        order.get("shipping_lines") or []
    )

    for s in shipping_lines:

        code = (
            s.get("code") or ""
        ).lower()

        title = (
            s.get("title") or ""
        ).lower()

        if (
            "dtdc" in code
            or "dtdc" in title
        ):
            return "DTDC"

        if (
            "delhivery" in code
            or "delhivery" in title
        ):
            return "Delhivery"

        if (
            "bluedart" in code
            or "blue dart" in code
            or "bluedart" in title
            or "blue dart" in title
        ):
            return "BlueDart"

    # -------------------------------------------------
    # Method 4: Order note
    # -------------------------------------------------

    note = (
        order.get("note") or ""
    ).lower()

    if "dtdc" in note:
        return "DTDC"

    if "delhivery" in note:
        return "Delhivery"

    if (
        "bluedart" in note
        or "blue dart" in note
    ):
        return "BlueDart"

    # -------------------------------------------------
    # Method 5: Note attributes
    # -------------------------------------------------

    note_attributes = (
        order.get("note_attributes") or []
    )

    for attr in note_attributes:

        value = str(
            attr.get("value") or ""
        ).lower()

        name = str(
            attr.get("name") or ""
        ).lower()

        combined = (
            f"{name} {value}"
        )

        if "dtdc" in combined:
            return "DTDC"

        if "delhivery" in combined:
            return "Delhivery"

        if (
            "bluedart" in combined
            or "blue dart" in combined
        ):
            return "BlueDart"

    # -------------------------------------------------
    # Nothing found
    # -------------------------------------------------

    return "Pending"




# =================================================
# Shopify → Middleware
# Webhook → Supabase
# =================================================



@app.post("/shopify/order")
async def shopify_order(request: Request):

    """
    Webhook for order creation AND updates.

    This fires multiple times:
    1. Order created
    2. Order updated
    3. Tags added
    4. Fulfillment added
    5. Cancelled
    6. Returned
    7. Notes changed
    """

    order = await request.json()

    customer = (
        order.get("customer") or {}
    )

    billing = (
        order.get("billing_address") or {}
    )

    shipping = (
        order.get("shipping_address") or {}
    )

    first_name = customer.get(
        "first_name"
    )

    last_name = customer.get(
        "last_name"
    )

    if first_name or last_name:

        customer_name = (
            f"{first_name or ''} "
            f"{last_name or ''}"
        ).strip()

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

    total_with_gst = float(
        order.get("total_price", 0)
    )

    total_gst = float(
        order.get("total_tax", 0)
    )

    total_ex_gst = round(
        total_with_gst - total_gst,
        2
    )

    shipping_lines = (
        order.get("shipping_lines", [])
    )

    shipping_charge = sum(
        float(s["price"])
        for s in shipping_lines
    )

    shipping_tax = sum(
        float(t["price"])
        for s in shipping_lines
        for t in s.get("tax_lines", [])
    )

    payment_method = determine_payment_method(
        order
    )

    # -------------------------------------------------
    # Determine delivery channel
    # -------------------------------------------------

    delivery_channel = (
        determine_delivery_channel(order)
    )

    print(
        "=============================================="
    )

    print(
        "FINAL DELIVERY CHANNEL:",
        delivery_channel
    )

    print(
        "ORDER NUMBER:",
        order.get("order_number")
    )

    print(
        "TRACKING INFORMATION:"
    )

    for f in (
        order.get("fulfillments") or []
    ):

        print(
            "  Tracking Number:",
            f.get("tracking_number")
        )

        print(
            "  Tracking Company:",
            f.get("tracking_company")
        )

    print(
        "=============================================="
    )

    # -------------------------------------------------
    # Determine order type
    # -------------------------------------------------

    order_type, against_order_id = (
        determine_order_type(order)
    )

    # -------------------------------------------------
    # IMPORTANT:
    # Do NOT overwrite an already detected carrier
    # with Pending.
    # -------------------------------------------------

    existing_order = (
        supabase.table("orders")
        .select(
            "id, delivery_channel"
        )
        .eq(
            "shopify_order_id",
            order.get("id")
        )
        .maybe_single()
        .execute()
    )

    existing_channel = None

    if existing_order.data:

        existing_channel = (
            existing_order.data.get(
                "delivery_channel"
            )
        )

        print(
            "EXISTING DELIVERY CHANNEL:",
            existing_channel
        )

    # If this webhook does not find a carrier,
    # but DB already has one, keep existing carrier.

    if (
        delivery_channel == "Pending"
        and existing_channel
        and existing_channel != "Pending"
    ):

        print(
            "Keeping existing delivery channel:",
            existing_channel
        )

        delivery_channel = existing_channel

    print(
        "FINAL CHANNEL TO SAVE:",
        delivery_channel
    )

    # -------------------------------------------------
    # UPSERT ORDER
    # -------------------------------------------------

    res = (
        supabase.table("orders")
        .upsert(
            {
                "shopify_order_id": order.get("id"),
                "order_number": str(
                    order.get("order_number")
                ),
                "voucher_date": (
                    order.get("created_at")[:10]
                ),
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
                "currency": order.get(
                    "currency",
                    "INR"
                ),
                "source": "Shopify",
                "notes": order.get("note") or "",
                "raw_order": order
            },
            on_conflict="shopify_order_id"
        )
        .execute()
    )

    order_id = res.data[0]["id"]

    # -------------------------------------------------
    # Delete and recreate items
    # -------------------------------------------------

    (
        supabase.table("order_items")
        .delete()
        .eq("order_id", order_id)
        .execute()
    )

    for li in order.get(
        "line_items",
        []
    ):

        qty = li.get(
            "quantity",
            0
        )

        price = float(
            li.get("price", 0)
        )

        discount = sum(
            float(d["amount"])
            for d in li.get(
                "discount_allocations",
                []
            )
        )

        gross = price * qty

        # Shopify price is already after discount
        amount_with_gst = round(
            gross,
            2
        )

        tax_lines = li.get(
            "tax_lines",
            []
        )

        gst_amount = sum(
            float(t["price"])
            for t in tax_lines
        )

        amount_ex_gst = round(
            amount_with_gst - gst_amount,
            2
        )

        cgst = 0
        sgst = 0
        igst = 0

        for t in tax_lines:

            if t["title"] == "CGST":

                cgst = float(
                    t["price"]
                )

            elif t["title"] == "SGST":

                sgst = float(
                    t["price"]
                )

            elif t["title"] == "IGST":

                igst = float(
                    t["price"]
                )

        original_rate_with_gst = price

        # Extract HS code

        hs_code = li.get(
            "hs_code"
        )

        # -------------------------------------------------
        # Extract item code and size
        # -------------------------------------------------

        item_code = (
            li.get("sku")
            or li.get("id")
        )

        item_size = None

        variant_title = (
            li.get("variant_title")
            or ""
        )

        size_keywords = [
            "XXXL",
            "XXL",
            "2XS",
            "2XL",
            "3XL",
            "4XL",
            "XL",
            "XS",
            "S",
            "M",
            "L"
        ]

        # Direct match

        if variant_title:

            for size in size_keywords:

                if (
                    size.upper()
                    == variant_title.upper()
                ):

                    item_size = size
                    break

            # Search inside variant title

            if not item_size:

                for size in size_keywords:

                    if (
                        size.upper()
                        in variant_title.upper()
                    ):

                        item_size = size
                        break

        # -------------------------------------------------
        # Try name field
        # -------------------------------------------------

        if not item_size:

            name = (
                li.get("name")
                or ""
            )

            if name:

                # Product - 2XS

                if " - " in name:

                    size_part = (
                        name.split(
                            " - "
                        )[-1].strip()
                    )

                    for size in size_keywords:

                        if (
                            size.upper()
                            == size_part.upper()
                        ):

                            item_size = size
                            break

                # Search anywhere in name

                if not item_size:

                    for size in size_keywords:

                        if (
                            size.upper()
                            in name.upper()
                        ):

                            item_size = size
                            break

        # -------------------------------------------------
        # Try properties
        # -------------------------------------------------

        if not item_size:

            properties = (
                li.get("properties")
                or []
            )

            for prop in properties:

                if (
                    prop.get("name")
                    and "size"
                    in prop.get(
                        "name",
                        ""
                    ).lower()
                ):

                    item_size = (
                        prop.get("value")
                    )

                    break

        # -------------------------------------------------
        # GST percentage
        # -------------------------------------------------

        gst_percentage = round(
            sum(
                float(
                    t.get(
                        "rate",
                        0
                    )
                ) * 100
                for t in tax_lines
            ),
            2
        ) if amount_ex_gst > 0 else 0

        # -------------------------------------------------
        # Insert item
        # -------------------------------------------------

        (
            supabase.table("order_items")
            .insert(
                {
                    "order_id": order_id,
                    "item_name": li.get(
                        "title"
                    ),
                    "item_code": item_code,
                    "item_size": item_size,
                    "quantity": qty,
                    "variant_id": li.get(
                        "variant_id"
                    ),
                    "hs_code": hs_code,
                    "rate": round(
                        original_rate_with_gst,
                        2
                    ),
                    "amount": amount_with_gst,
                    "amount_ex_gst": amount_ex_gst,
                    "cgst": cgst,
                    "sgst": sgst,
                    "igst": igst,
                    "gst_percentage": gst_percentage,
                    "item_discount": round(
                        discount,
                        2
                    )
                }
            )
            .execute()
        )

    return {
        "status": "stored",
        "delivery_channel": delivery_channel,
        "order_type": order_type,
        "against_order_id": against_order_id,
        "order_number": order.get(
            "order_number"
        )
    }


# =================================================
# Shopify Fulfillment Webhook
# =================================================

@app.post("/shopify/fulfillment")
async def shopify_fulfillment(
    request: Request
):

    """
    Webhook endpoint for when Shopify
    fulfillment is created/updated.
    """

    try:

        fulfillment = await request.json()

        # The fulfillment webhook sends order_id

        order_id = fulfillment.get(
            "order_id"
        )

        if not order_id:

            return {
                "status": "error",
                "message": "no_order_id in webhook payload"
            }

        # Check Shopify configuration

        if (
            not SHOPIFY_STORE
            or not SHOPIFY_TOKEN
        ):

            raise HTTPException(
                500,
                "SHOPIFY_STORE or SHOPIFY_TOKEN not configured"
            )

        # Fetch full Shopify order

        url = (
            f"https://{SHOPIFY_STORE}.myshopify.com"
            f"/admin/api/{SHOPIFY_API_VERSION}"
            f"/orders/{order_id}.json"
        )

        headers = {
            "X-Shopify-Access-Token":
                SHOPIFY_TOKEN
        }

        response = requests.get(
            url,
            headers=headers
        )

        if response.status_code != 200:

            raise HTTPException(
                500,
                "Failed to fetch order from Shopify: "
                f"{response.text}"
            )

        order = (
            response.json()["order"]
        )

        print(
            "=============================================="
        )

        print(
            "SHOPIFY FULFILLMENT DEBUG"
        )

        print(
            "ORDER NUMBER:",
            order.get("order_number")
        )

        print(
            "SHOPIFY ORDER ID:",
            order.get("id")
        )

        for f in (
            order.get("fulfillments") or []
        ):

            print(
                "TRACKING NUMBER:",
                f.get("tracking_number")
            )

            print(
                "TRACKING COMPANY:",
                f.get("tracking_company")
            )

            print(
                "TRACKING URL:",
                f.get("tracking_url")
            )

            print(
                "FULFILLMENT STATUS:",
                f.get("status")
            )

        print(
            "TAGS:",
            order.get("tags")
        )

        print(
            "SHIPPING LINES:",
            order.get("shipping_lines")
        )

        print(
            "NOTE:",
            order.get("note")
        )

        print(
            "NOTE ATTRIBUTES:",
            order.get("note_attributes")
        )

        print(
            "=============================================="
        )

        # -------------------------------------------------
        # Determine delivery channel
        # -------------------------------------------------

        delivery_channel = (
            determine_delivery_channel(order)
        )

        # If Shopify order does not contain carrier,
        # try the fulfillment webhook payload itself

        if delivery_channel == "Pending":

            tracking_company = (
                fulfillment.get(
                    "tracking_company"
                )
                or ""
            ).strip().lower()

            if "dtdc" in tracking_company:

                delivery_channel = "DTDC"

            elif "delhivery" in tracking_company:

                delivery_channel = "Delhivery"

            elif (
                "bluedart"
                in tracking_company
                or "blue dart"
                in tracking_company
            ):

                delivery_channel = "BlueDart"

        # -------------------------------------------------
        # IMPORTANT:
        # Never overwrite known carrier with Pending
        # -------------------------------------------------

        if delivery_channel == "Pending":

            existing_order = (
                supabase.table("orders")
                .select(
                    "delivery_channel"
                )
                .eq(
                    "shopify_order_id",
                    order_id
                )
                .maybe_single()
                .execute()
            )

            if existing_order.data:

                existing_channel = (
                    existing_order.data.get(
                        "delivery_channel"
                    )
                )

                print(
                    "EXISTING DB DELIVERY CHANNEL:",
                    existing_channel
                )

                if (
                    existing_channel
                    and existing_channel != "Pending"
                ):

                    delivery_channel = (
                        existing_channel
                    )

        print(
            "FINAL FULFILLMENT DELIVERY CHANNEL:",
            delivery_channel
        )

        # -------------------------------------------------
        # Determine order type
        # -------------------------------------------------

        order_type, against_order_id = (
            determine_order_type(order)
        )

        # -------------------------------------------------
        # Update database
        # -------------------------------------------------

        result = (
            supabase.table("orders")
            .update(
                {
                    "delivery_channel":
                        delivery_channel,
                    "type":
                        order_type,
                    "against_order_id":
                        against_order_id,
                    "notes":
                        order.get("note") or "",
                    "raw_order":
                        order
                }
            )
            .eq(
                "shopify_order_id",
                order_id
            )
            .execute()
        )

        return {
            "status": "success",
            "order_id": order_id,
            "delivery_channel":
                delivery_channel,
            "order_type":
                order_type,
            "against_order_id":
                against_order_id,
            "updated":
                len(result.data) > 0
        }

    except Exception as e:

        print(
            f"Error in fulfillment webhook: {str(e)}"
        )

        return {
            "status": "error",
            "message": str(e)
        }


# =================================================
# Manual Delivery Channel Sync
# =================================================

@app.post("/sync/delivery-channels")
async def sync_delivery_channels():

    """
    Manually sync delivery channels
    for orders with Pending status.
    """

    try:

        # -------------------------------------------------
        # Check Shopify configuration
        # -------------------------------------------------

        if not SHOPIFY_STORE:

            raise Exception(
                "SHOPIFY_STORE_NAME is missing"
            )

        if not SHOPIFY_TOKEN:

            raise Exception(
                "SHOPIFY_ACCESS_TOKEN is missing"
            )

        print(
            "=============================================="
        )

        print(
            "DELIVERY CHANNEL SYNC STARTED"
        )

        print(
            "SHOPIFY STORE:",
            SHOPIFY_STORE
        )

        print(
            "API VERSION:",
            SHOPIFY_API_VERSION
        )

        print(
            "=============================================="
        )

        # -------------------------------------------------
        # Get all Pending orders
        # -------------------------------------------------

        res = (
            supabase.table("orders")
            .select(
                "id, shopify_order_id, "
                "order_number, delivery_channel"
            )
            .eq(
                "delivery_channel",
                "Pending"
            )
            .execute()
        )

        print(
            "PENDING ORDERS FOUND:",
            len(res.data)
        )

        updated_count = 0

        # -------------------------------------------------
        # Process each pending order
        # -------------------------------------------------

        for order_record in res.data:

            shopify_order_id = (
                order_record.get(
                    "shopify_order_id"
                )
            )

            print(
                "----------------------------------------------"
            )

            print(
                "ORDER NUMBER:",
                order_record.get(
                    "order_number"
                )
            )

            print(
                "SHOPIFY ORDER ID:",
                shopify_order_id
            )

            if not shopify_order_id:

                print(
                    "SKIPPING: "
                    "Shopify order ID missing"
                )

                continue

            # -------------------------------------------------
            # Fetch fresh order from Shopify
            # -------------------------------------------------

            url = (
                f"https://{SHOPIFY_STORE}.myshopify.com"
                f"/admin/api/{SHOPIFY_API_VERSION}"
                f"/orders/{shopify_order_id}.json"
            )

            headers = {
                "X-Shopify-Access-Token":
                    SHOPIFY_TOKEN
            }

            response = requests.get(
                url,
                headers=headers,
                timeout=30
            )

            print(
                "SHOPIFY RESPONSE STATUS:",
                response.status_code
            )

            if response.status_code != 200:

                print(
                    "SHOPIFY ERROR:",
                    response.text
                )

                continue

            order = (
                response.json().get("order")
            )

            if not order:

                print(
                    "ERROR: Shopify response "
                    "does not contain order"
                )

                continue

            print(
                "=============================================="
            )

            print(
                "DELIVERY DEBUG"
            )

            print(
                "ORDER NUMBER:",
                order.get("order_number")
            )

            print(
                "SHOPIFY ID:",
                order.get("id")
            )

            for f in (
                order.get("fulfillments") or []
            ):

                print(
                    "TRACKING NUMBER:",
                    f.get("tracking_number")
                )

                print(
                    "TRACKING COMPANY:",
                    f.get("tracking_company")
                )

                print(
                    "TRACKING URL:",
                    f.get("tracking_url")
                )

                print(
                    "STATUS:",
                    f.get("status")
                )

            print(
                "TAGS:",
                order.get("tags")
            )

            print(
                "SHIPPING LINES:",
                order.get("shipping_lines")
            )

            print(
                "NOTE:",
                order.get("note")
            )

            print(
                "=============================================="
            )

            # -------------------------------------------------
            # Additional debug
            # -------------------------------------------------

            print(
                "ORDER TAGS:",
                order.get("tags")
            )

            print(
                "ORDER NOTE:",
                order.get("note")
            )

            print(
                "SHIPPING LINES:",
                order.get("shipping_lines")
            )

            for f in (
                order.get("fulfillments") or []
            ):

                print(
                    "FULFILLMENT:"
                )

                print(
                    "  TRACKING NUMBER:",
                    f.get("tracking_number")
                )

                print(
                    "  TRACKING COMPANY:",
                    f.get("tracking_company")
                )

                print(
                    "  TRACKING URL:",
                    f.get("tracking_url")
                )

                print(
                    "  STATUS:",
                    f.get("status")
                )

            # -------------------------------------------------
            # Determine delivery channel
            # -------------------------------------------------

            delivery_channel = (
                determine_delivery_channel(order)
            )

            print(
                "DETECTED DELIVERY CHANNEL:",
                delivery_channel
            )

            # -------------------------------------------------
            # Determine order type
            # -------------------------------------------------

            order_type, against_order_id = (
                determine_order_type(order)
            )

            print(
                "ORDER TYPE:",
                order_type
            )

            print(
                "AGAINST ORDER ID:",
                against_order_id
            )

            # -------------------------------------------------
            # Update only when carrier found
            # -------------------------------------------------

            if delivery_channel != "Pending":

                (
                    supabase.table("orders")
                    .update(
                        {
                            "delivery_channel":
                                delivery_channel,
                            "type":
                                order_type,
                            "against_order_id":
                                against_order_id,
                            "notes":
                                order.get("note") or "",
                            "raw_order":
                                order
                        }
                    )
                    .eq(
                        "shopify_order_id",
                        shopify_order_id
                    )
                    .execute()
                )

                updated_count += 1

                print(
                    "✅ UPDATED:",
                    order_record.get(
                        "order_number"
                    ),
                    "→",
                    delivery_channel
                )

            else:

                print(
                    "⚠️ STILL PENDING:",
                    order_record.get(
                        "order_number"
                    )
                )

        # -------------------------------------------------
        # Sync completed
        # -------------------------------------------------

        print(
            "=============================================="
        )

        print(
            "DELIVERY CHANNEL SYNC COMPLETED"
        )

        print(
            "UPDATED ORDERS:",
            updated_count
        )

        print(
            "=============================================="
        )

        return {
            "status": "sync_complete",
            "updated_orders":
                updated_count,
            "pending_orders_checked":
                len(res.data)
        }

    except Exception as e:

        print(
            "=============================================="
        )

        print(
            "❌ DELIVERY CHANNEL SYNC ERROR"
        )

        print(
            str(e)
        )

        print(
            "=============================================="
        )

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# =================================================
# Fix Old Orders
# =================================================

@app.post("/fix/old-orders")
async def fix_old_orders():

    """
    Reset all orders that have incorrect
    delivery channels from old code.

    Changes:
    Website
    Marketplace
    Social-Media

    → Pending

    Also sets order type and against_order_id.
    """

    wrong_channels = [
        "Website",
        "Marketplace",
        "Social-Media"
    ]

    total_fixed = 0

    for wrong_channel in wrong_channels:

        res = (
            supabase.table("orders")
            .select(
                "id, order_number, raw_order"
            )
            .eq(
                "delivery_channel",
                wrong_channel
            )
            .execute()
        )

        for order_record in res.data:

            raw_order = (
                order_record.get(
                    "raw_order",
                    {}
                )
            )

            if raw_order:

                new_channel = (
                    determine_delivery_channel(
                        raw_order
                    )
                )

                (
                    new_type,
                    new_against_id
                ) = determine_order_type(
                    raw_order
                )

            else:

                new_channel = "Pending"
                new_type = "sales"
                new_against_id = None

            (
                supabase.table("orders")
                .update(
                    {
                        "delivery_channel":
                            new_channel,
                        "type":
                            new_type,
                        "against_order_id":
                            new_against_id
                    }
                )
                .eq(
                    "id",
                    order_record["id"]
                )
                .execute()
            )

            total_fixed += 1

    return {
        "status": "fix_complete",
        "total_orders_fixed":
            total_fixed,
        "message":
            "Old orders updated with new type detection logic."
    }


# =================================================
# Tally → Fetch Orders
# =================================================

@app.post("/tally/orders")
async def tally_orders_post(
    request: Request
):

    try:

        body = await request.json()

        from_date = body.get(
            "from_date"
        )

        to_date = body.get(
            "to_date"
        )

        if not from_date or not to_date:

            raise HTTPException(
                400,
                "from_date and to_date required"
            )

        res = (
            supabase.table("orders")
            .select(
                "*, order_items(*)"
            )
            .gte(
                "voucher_date",
                from_date
            )
            .lte(
                "voucher_date",
                to_date
            )
            .order(
                "voucher_date"
            )
            .execute()
        )

        tally_orders = []

        for o in res.data:

            raw = o["raw_order"]

            shipping_address = (
                raw.get(
                    "shipping_address"
                ) or {}
            )

            billing_address = (
                raw.get(
                    "billing_address"
                ) or {}
            )

            state = (
                shipping_address.get(
                    "province"
                )
                or billing_address.get(
                    "province"
                )
                or ""
            )

            country = (
                shipping_address.get(
                    "country"
                )
                or billing_address.get(
                    "country"
                )
                or ""
            )

            # -------------------------------------------------
            # Customer information
            # -------------------------------------------------

            customer_name = (
                o["customer_name"]
            )

            customer_email = (
                o["customer_email"]
            )

            customer_phone = (
                o["customer_phone"]
            )

            # -------------------------------------------------
            # Exchange / Redispatch
            # -------------------------------------------------

            if (
                (
                    not customer_name
                    or customer_name
                    == "Unknown Customer"
                )
                and o.get(
                    "against_order_id"
                )
            ):

                shipping_address = (
                    raw.get(
                        "shipping_address"
                    ) or {}
                )

                billing_address = (
                    raw.get(
                        "billing_address"
                    ) or {}
                )

                customer_name = (
                    shipping_address.get(
                        "name"
                    )
                    or billing_address.get(
                        "name"
                    )
                    or customer_name
                )

                customer_phone = (
                    shipping_address.get(
                        "phone"
                    )
                    or billing_address.get(
                        "phone"
                    )
                    or customer_phone
                )

            # -------------------------------------------------
            # Gross / discount
            # -------------------------------------------------

            gross_item_amount = sum(
                float(li["price"])
                * li["quantity"]
                for li in raw.get(
                    "line_items",
                    []
                )
            )

            discount_amount = float(
                raw.get(
                    "total_discounts",
                    0
                )
            )

            net_item_amount = round(
                gross_item_amount
                - discount_amount,
                2
            )

            shopify_lines = (
                raw.get(
                    "line_items",
                    []
                )
            )

            items = []

            total_ex_gst = 0
            total_gst = 0
            total_with_gst = 0

            # -------------------------------------------------
            # Items
            # -------------------------------------------------

            for li in shopify_lines:

                qty = li["quantity"]

                price = float(
                    li["price"]
                )

                discount = sum(
                    float(d["amount"])
                    for d in li.get(
                        "discount_allocations",
                        []
                    )
                )

                # Shopify price already includes discount

                amount_with_gst = round(
                    price * qty,
                    2
                )

                gst = sum(
                    float(t["price"])
                    for t in li.get(
                        "tax_lines",
                        []
                    )
                )

                amount_ex_gst = round(
                    amount_with_gst - gst,
                    2
                )

                rate_with_gst = round(
                    price,
                    2
                )

                rate_ex_gst = round(
                    amount_ex_gst / qty,
                    2
                ) if qty > 0 else 0

                total_ex_gst += (
                    amount_ex_gst
                )

                total_gst += gst

                total_with_gst += (
                    amount_with_gst
                )

                # -------------------------------------------------
                # Extract item size
                # -------------------------------------------------

                item_size = None

                variant_title = (
                    li.get(
                        "variant_title"
                    ) or ""
                )

                size_keywords = [
                    "XXXL",
                    "XXL",
                    "2XS",
                    "2XL",
                    "3XL",
                    "4XL",
                    "XL",
                    "XS",
                    "S",
                    "M",
                    "L"
                ]

                if variant_title:

                    for size in size_keywords:

                        if (
                            size.upper()
                            == variant_title.upper()
                        ):

                            item_size = size
                            break

                    if not item_size:

                        for size in size_keywords:

                            if (
                                size.upper()
                                in variant_title.upper()
                            ):

                                item_size = size
                                break

                # -------------------------------------------------
                # Try name field
                # -------------------------------------------------

                if not item_size:

                    name = (
                        li.get("name")
                        or ""
                    )

                    if name:

                        if " - " in name:

                            size_part = (
                                name.split(
                                    " - "
                                )[-1].strip()
                            )

                            for size in size_keywords:

                                if (
                                    size.upper()
                                    == size_part.upper()
                                ):

                                    item_size = size
                                    break

                        if not item_size:

                            for size in size_keywords:

                                if (
                                    size.upper()
                                    in name.upper()
                                ):

                                    item_size = size
                                    break

                # -------------------------------------------------
                # Find DB item
                # -------------------------------------------------

                db_item = next(
                    (
                        oi
                        for oi in o.get(
                            "order_items",
                            []
                        )
                        if oi.get(
                            "item_code"
                        )
                        == (
                            li.get("sku")
                            or str(
                                li.get("id")
                            )
                        )
                    ),
                    {}
                )

                items.append(
                    {
                        "item_code":
                            li.get("sku")
                            or li.get("id"),

                        "item_name":
                            li["title"],

                        "item_size":
                            item_size,

                        "quantity":
                            qty,

                        "variant_id":
                            db_item.get(
                                "variant_id"
                            ),

                        "hs_code":
                            db_item.get(
                                "hs_code"
                            ),

                        "rate_with_gst":
                            round(
                                price,
                                2
                            ),

                        "rate_ex_gst":
                            rate_ex_gst,

                        "amount_ex_gst":
                            round(
                                amount_ex_gst,
                                2
                            ),

                        "amount_with_gst":
                            round(
                                amount_with_gst,
                                2
                            ),

                        "discount":
                            round(
                                discount,
                                2
                            ),

                        "gst": {

                            "cgst": next(
                                (
                                    float(
                                        t["price"]
                                    )
                                    for t in li[
                                        "tax_lines"
                                    ]
                                    if t[
                                        "title"
                                    ] == "CGST"
                                ),
                                0
                            ),

                            "sgst": next(
                                (
                                    float(
                                        t["price"]
                                    )
                                    for t in li[
                                        "tax_lines"
                                    ]
                                    if t[
                                        "title"
                                    ] == "SGST"
                                ),
                                0
                            ),

                            "igst": next(
                                (
                                    float(
                                        t["price"]
                                    )
                                    for t in li[
                                        "tax_lines"
                                    ]
                                    if t[
                                        "title"
                                    ] == "IGST"
                                ),
                                0
                            ),

                            "total":
                                round(
                                    gst,
                                    2
                                ),

                            "percentage":
                                round(
                                    sum(
                                        float(
                                            t.get(
                                                "rate",
                                                0
                                            )
                                        ) * 100
                                        for t in li.get(
                                            "tax_lines",
                                            []
                                        )
                                    ),
                                    2
                                )
                        }
                    }
                )

            # -------------------------------------------------
            # Shipping
            # -------------------------------------------------

            shipping = sum(
                float(s["price"])
                for s in raw.get(
                    "shipping_lines",
                    []
                )
            )

            shipping_gst = sum(
                float(t["price"])
                for s in raw.get(
                    "shipping_lines",
                    []
                )
                for t in s.get(
                    "tax_lines",
                    []
                )
            )

            shipping_ex_gst = round(
                shipping - shipping_gst,
                2
            )

            grand_total = float(
                raw["total_price"]
            )

            # -------------------------------------------------
            # Stored values
            # -------------------------------------------------

            payment_method = o.get(
                "payment_method",
                "Prepaid"
            )

            delivery_channel = o.get(
                "delivery_channel",
                "Pending"
            )

            order_type = o.get(
                "type",
                "sales"
            )

            against_order_id = o.get(
                "against_order_id"
            )

            # -------------------------------------------------
            # Voucher Type
            # -------------------------------------------------

            voucher_type = (
                f"{order_type.capitalize()}-"
                f"{payment_method}-"
                f"{delivery_channel}"
            )

            # -------------------------------------------------
            # Order Data
            # -------------------------------------------------

            order_data = {

                "voucher_type":
                    voucher_type,

                "order_type":
                    order_type,

                "payment_method":
                    payment_method,

                "delivery_channel":
                    delivery_channel,

                "voucher_number":
                    o["order_number"],

                "voucher_date":
                    o["voucher_date"],

                "notes":
                    o.get("notes") or "",

                "customer": {

                    "name":
                        customer_name,

                    "email":
                        customer_email,

                    "phone":
                        customer_phone,

                    "state":
                        state,

                    "country":
                        country
                },

                "items":
                    items,

                "gross_item_amount":
                    round(
                        gross_item_amount,
                        2
                    ),

                "discount_amount":
                    round(
                        discount_amount,
                        2
                    ),

                "net_item_amount":
                    round(
                        net_item_amount,
                        2
                    ),

                "shipping_ex_gst":
                    round(
                        shipping_ex_gst,
                        2
                    ),

                "shipping_gst":
                    round(
                        shipping_gst,
                        2
                    ),

                "shipping_with_gst":
                    round(
                        shipping,
                        2
                    ),

                "total_ex_gst":
                    round(
                        total_ex_gst,
                        2
                    ),

                "total_gst":
                    round(
                        total_gst
                        + shipping_gst,
                        2
                    ),

                "total_with_gst":
                    round(
                        grand_total,
                        2
                    ),

                "grand_total":
                    round(
                        grand_total,
                        2
                    ),

                "currency":
                    o["currency"],

                "source":
                    o["source"],

                "shopify_order_id":
                    o["shopify_order_id"]
            }

            # -------------------------------------------------
            # Add against_order_id only for
            # exchange / redispatch
            # -------------------------------------------------

            if (
                order_type
                in [
                    "exchange",
                    "redispatch"
                ]
                and against_order_id
            ):

                order_data[
                    "against_order_id"
                ] = against_order_id

            tally_orders.append(
                order_data
            )

        return {
            "orders":
                tally_orders
        }

    except Exception as e:

        raise HTTPException(
            500,
            str(e)
        )


# =================================================
# Tally → Push Sales to Shopify
# =================================================

@app.post("/tally/sales")
async def tally_sales(
    request: Request
):

    data = await request.json()

    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/"
        f"admin/api/{SHOPIFY_API_VERSION}/"
        f"orders.json"
    )

    headers = {
        "X-Shopify-Access-Token":
            SHOPIFY_TOKEN,
        "Content-Type":
            "application/json"
    }

    full_name = (
        data.get(
            "customer",
            {}
        )
        .get(
            "name",
            ""
        )
        .strip()
    )

    name_parts = full_name.split(
        " ",
        1
    )

    first_name = (
        name_parts[0]
        if name_parts
        else ""
    )

    last_name = (
        name_parts[1]
        if len(name_parts) > 1
        else ""
    )

    line_items = []

    for item in data.get(
        "items",
        []
    ):

        product_name = (
            item.get(
                "product_name"
            )
            or item.get(
                "item_name"
            )
        )

        line_items.append(
            {
                "title":
                    product_name,

                "quantity":
                    item["quantity"],

                "price":
                    round(
                        item["rate"],
                        2
                    )
            }
        )

    payload = {

        "order": {

            "email":
                data["customer"].get(
                    "email"
                ),

            "customer": {

                "first_name":
                    first_name,

                "last_name":
                    last_name,

                "email":
                    data["customer"].get(
                        "email"
                    )
            },

            "line_items":
                line_items,

            "financial_status":
                "paid",

            "currency":
                "INR"
        }
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload
    )

    if response.status_code not in (
        200,
        201
    ):

        raise HTTPException(
            status_code=500,
            detail=response.text
        )

    return {
        "status":
            "success",

        "shopify_order_id":
            response.json()[
                "order"
            ]["id"]
    }


# =================================================
# Shopify OAuth
# =================================================

SHOPIFY_API_KEY = os.getenv(
    "SHOPIFY_API_KEY",
    ""
).strip()

SHOPIFY_API_SECRET = os.getenv(
    "SHOPIFY_API_SECRET",
    ""
).strip()

SCOPES = (
    "read_orders,"
    "read_products,"
    "read_customers,"
    "write_orders"
)

REDIRECT_URI = (
    "https://shopify-tally-middleware.onrender.com"
    "/auth/callback"
)


# -------------------------------------------------
# OAuth Install
# -------------------------------------------------

@app.get("/auth/install")
def shopify_install(
    shop: str
):

    if not shop:

        raise HTTPException(
            400,
            "Missing shop parameter"
        )

    params = {

        "client_id":
            SHOPIFY_API_KEY,

        "scope":
            SCOPES,

        "redirect_uri":
            REDIRECT_URI
    }

    query = urllib.parse.urlencode(
        params
    )

    install_url = (
        f"https://{shop}"
        f"/admin/oauth/authorize?"
        f"{query}"
    )

    return RedirectResponse(
        install_url
    )


# -------------------------------------------------
# OAuth Callback
# -------------------------------------------------

@app.get("/auth/callback")
def shopify_callback(
    code: str,
    shop: str
):

    if not code or not shop:

        raise HTTPException(
            400,
            "Invalid OAuth response"
        )

    token_url = (
        f"https://{shop}"
        f"/admin/oauth/access_token"
    )

    payload = {

        "client_id":
            SHOPIFY_API_KEY,

        "client_secret":
            SHOPIFY_API_SECRET,

        "code":
            code
    }

    response = requests.post(
        token_url,
        json=payload
    )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=(
                "Token exchange failed: "
                f"{response.text}"
            )
        )

    data = response.json()

    access_token = data.get(
        "access_token"
    )

    # -------------------------------------------------
    # Save token to database
    # -------------------------------------------------

    if access_token:

        (
            supabase.table(
                "shopify_tokens"
            )
            .upsert(
                {
                    "shop":
                        shop,

                    "access_token":
                        access_token,

                    "created_at":
                        "now()"
                },
                on_conflict="shop"
            )
            .execute()
        )

    # -------------------------------------------------
    # Display token
    # -------------------------------------------------

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>
                App Installed Successfully!
            </title>

            <style>

                body {{
                    font-family: Arial;
                    padding: 40px;
                    background: #f0f0f0;
                }}

                .container {{
                    max-width: 800px;
                    margin: 0 auto;
                    background: white;
                    padding: 40px;
                    border-radius: 10px;
                }}

                .success {{
                    color: #00b300;
                    font-size: 24px;
                    margin-bottom: 20px;
                }}

                .token {{
                    background: #f5f5f5;
                    padding: 15px;
                    border-radius: 5px;
                    word-break: break-all;
                    font-family: monospace;
                }}

                .warning {{
                    background: #fff3cd;
                    padding: 15px;
                    border-radius: 5px;
                    margin-top: 20px;
                    border-left: 4px solid #ffc107;
                }}

            </style>
        </head>

        <body>

            <div class="container">

                <div class="success">
                    ✅ App Installed Successfully!
                </div>

                <p>
                    <strong>Shop:</strong>
                    {shop}
                </p>

                <p>
                    <strong>
                        Access Token (SAVE THIS!):
                    </strong>
                </p>

                <div class="token">
                    {access_token}
                </div>

                <div class="warning">

                    <strong>
                        ⚠️ IMPORTANT:
                    </strong>

                    <ol>

                        <li>
                            Copy the access token above
                        </li>

                        <li>
                            Go to your Render dashboard
                        </li>

                        <li>
                            Add environment variable:
                            <code>
                                SHOPIFY_ACCESS_TOKEN=
                                {access_token}
                            </code>
                        </li>

                        <li>
                            Extract store name from shop URL
                            and add:
                            <code>
                                SHOPIFY_STORE_NAME=
                                (store-name-only)
                            </code>
                        </li>

                        <li>
                            Save and redeploy
                        </li>

                    </ol>

                </div>

            </div>

        </body>
        </html>
        """
    )


# =================================================
# Root Page
# =================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def root():

    return """
    <!DOCTYPE html>
    <html>

    <head>

        <title>
            AINA - Shopify-Tally Integration
        </title>

        <style>

            body {
                font-family: Arial;
                padding: 40px;
                background: #f5f5f5;
            }

            .container {
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                padding: 40px;
                border-radius: 10px;
            }

            h1 {
                color: #5c6ac4;
            }

            .feature {
                background: #f9fafb;
                padding: 20px;
                margin: 20px 0;
                border-radius: 8px;
                border-left: 4px solid #5c6ac4;
            }

            .feature h3 {
                margin-top: 0;
                color: #202223;
            }

            .workflow {
                background: #e8f5e9;
                border-left-color: #4caf50;
            }

            .warning {
                background: #fff4e6;
                border-left-color: #ff9800;
            }

            .exchange {
                background: #e3f2fd;
                border-left-color: #2196f3;
            }

            .redispatch {
                background: #f3e5f5;
                border-left-color: #9c27b0;
            }

            code {
                background: #e1e3e5;
                padding: 2px 6px;
                border-radius: 3px;
                font-size: 0.9em;
            }

            ul {
                margin: 10px 0;
            }

            .steps {
                background: #fff;
                padding: 15px;
                border-radius: 5px;
                border: 1px solid #ddd;
                margin: 10px 0;
            }

            .day {
                font-weight: bold;
                color: #5c6ac4;
                margin-top: 15px;
            }

        </style>

    </head>

    <body>

        <div class="container">

            <h1>
                👗 AINA Shopify-Tally Integration
            </h1>

            <p>
                Automated order type detection +
                delivery channel tracking
                (sales/cancelled/return/exchange/redispatch)
            </p>

            <div class="feature workflow">

                <h3>
                    📅 Daily Workflow
                    (How It Works)
                </h3>

                <div class="day">
                    Day 1 - Order Created:
                </div>

                <div class="steps">

                    Customer places order →
                    Webhook fires →
                    Order saved as
                    <code>
                        type: "sales"
                    </code>

                </div>

                <div class="day">
                    Day 2+ - Order Status Changes:
                </div>

                <div class="steps">

                    <strong>
                        Scenario 1 (Normal Sale):
                    </strong>

                    Staff adds carrier tag →
                    <code>
                        type: "sales"
                    </code>,
                    carrier assigned

                    <br><br>

                    <strong>
                        Scenario 2 (Cancellation):
                    </strong>

                    Order cancelled →
                    Webhook fires →
                    <code>
                        type: "cancelled"
                    </code>

                    <br><br>

                    <strong>
                        Scenario 3 (Return/Refund):
                    </strong>

                    Staff adds tag
                    <code>
                        return
                    </code>
                    →
                    <code>
                        type: "return"
                    </code>

                    <br><br>

                    <strong>
                        Scenario 4 (Exchange):
                    </strong>

                    Staff adds note
                    <code>
                        This is an exchange order
                        against #184055
                    </code>
                    →
                    <code>
                        type: "exchange"
                    </code>

                    <br><br>

                    <strong>
                        Scenario 5 (Redispatch):
                    </strong>

                    Staff adds note
                    <code>
                        This is a redispatch order
                        against #184055
                    </code>
                    →
                    <code>
                        type: "redispatch"
                    </code>

                </div>

            </div>


            <div class="feature exchange">

                <h3>
                    🔄 Exchange Orders
                </h3>

                <p>
                    <strong>
                        What is an exchange order?
                    </strong>
                </p>

                <p>
                    A customer returns an item and
                    receives a different/replacement item.
                    Example: Customer bought size M shirt
                    but exchanges for size L shirt.
                </p>

                <p>
                    <strong>
                        How to mark in Shopify:
                    </strong>
                </p>

                <ol>

                    <li>
                        Open the NEW order
                        (the exchange order)
                    </li>

                    <li>
                        Scroll to "Notes" section
                    </li>

                    <li>
                        Add note:
                        <code>
                            This is an exchange order
                            against #184055
                        </code>
                    </li>

                    <li>
                        Click "Save"
                    </li>

                </ol>

                <p>
                    <strong>
                        System automatically detects:
                    </strong>
                </p>

                <ul>

                    <li>
                        ✅
                        <code>
                            type: "exchange"
                        </code>
                    </li>

                    <li>
                        ✅
                        <code>
                            against_order_id: "184055"
                        </code>
                    </li>

                    <li>
                        ✅ Links to original order #184055
                    </li>

                </ul>

                <p>
                    <strong>
                        Tally Response:
                    </strong>
                </p>

                <pre>
{
  "order_type": "exchange",
  "against_order_id": "184055",
  "voucher_type": "Exchange-COD-DTDC"
}
                </pre>

            </div>


            <div class="feature redispatch">

                <h3>
                    📦 Redispatch Orders
                </h3>

                <p>
                    <strong>
                        What is a redispatch order?
                    </strong>
                </p>

                <p>
                    Original order delivery failed/was lost.
                    A new shipment is sent to customer.
                </p>

                <p>
                    <strong>
                        How to mark in Shopify:
                    </strong>
                </p>

                <ol>

                    <li>
                        Open the NEW order
                    </li>

                    <li>
                        Scroll to "Notes" section
                    </li>

                    <li>
                        Add note:
                        <code>
                            This is a redispatch order
                            against #184055
                        </code>
                    </li>

                    <li>
                        Click "Save"
                    </li>

                </ol>

                <p>
                    <strong>
                        System automatically detects:
                    </strong>
                </p>

                <ul>

                    <li>
                        ✅
                        <code>
                            type: "redispatch"
                        </code>
                    </li>

                    <li>
                        ✅
                        <code>
                            against_order_id: "184055"
                        </code>
                    </li>

                    <li>
                        ✅ Links to original order #184055
                    </li>

                </ul>

                <p>
                    <strong>
                        Tally Response:
                    </strong>
                </p>

                <pre>
{
  "order_type": "redispatch",
  "against_order_id": "184055",
  "voucher_type": "Redispatch-Prepaid-Delhivery"
}
                </pre>

            </div>


            <div class="feature">

                <h3>
                    🏷️ All Order Types & Detection
                </h3>

                <table
                    style="
                        width: 100%;
                        border-collapse: collapse;
                        margin-top: 10px;
                    "
                >

                    <tr
                        style="
                            background: #f5f5f5;
                            border-bottom: 2px solid #5c6ac4;
                        "
                    >

                        <th
                            style="
                                padding: 10px;
                                text-align: left;
                                border: 1px solid #ddd;
                            "
                        >
                            Type
                        </th>

                        <th
                            style="
                                padding: 10px;
                                text-align: left;
                                border: 1px solid #ddd;
                            "
                        >
                            How Staff Marks It
                        </th>

                        <th
                            style="
                                padding: 10px;
                                text-align: left;
                                border: 1px solid #ddd;
                            "
                        >
                            Detection Logic
                        </th>

                    </tr>


                    <tr
                        style="
                            border-bottom: 1px solid #ddd;
                        "
                    >

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            <strong>
                                Sales
                            </strong>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            No action needed (default)
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Default type for all orders
                        </td>

                    </tr>


                    <tr
                        style="
                            background: #fee;
                            border-bottom: 1px solid #ddd;
                        "
                    >

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            <strong>
                                Cancelled
                            </strong>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Shopify "Cancel Order" button
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Automatic
                            (cancel_reason or cancelled_at)
                        </td>

                    </tr>


                    <tr
                        style="
                            border-bottom: 1px solid #ddd;
                        "
                    >

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            <strong>
                                Return
                            </strong>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Add tag
                            <code>
                                return
                            </code>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Tag "return" OR
                            fulfillment status = "returned"
                        </td>

                    </tr>


                    <tr
                        style="
                            background: #e3f2fd;
                            border-bottom: 1px solid #ddd;
                        "
                    >

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            <strong>
                                Exchange
                            </strong>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Add note:
                            <code>
                                This is an exchange order
                                against #ORDER
                            </code>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Note contains
                            "exchange order against"
                        </td>

                    </tr>


                    <tr
                        style="
                            background: #f3e5f5;
                            border-bottom: 1px solid #ddd;
                        "
                    >

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            <strong>
                                Redispatch
                            </strong>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Add note:
                            <code>
                                This is a redispatch order
                                against #ORDER
                            </code>
                        </td>

                        <td
                            style="
                                padding: 10px;
                                border: 1px solid #ddd;
                            "
                        >
                            Note contains
                            "redispatch order against"
                        </td>

                    </tr>

                </table>

            </div>


            <div class="feature">

                <h3>
                    💡 Voucher Types & Examples
                </h3>

                <p>
                    <strong>
                        Format:
                    </strong>

                    <code>
                        {OrderType}-{PaymentMethod}-{DeliveryChannel}
                    </code>
                </p>

                <p>
                    <strong>
                        Sales Examples:
                    </strong>
                </p>

                <ul>

                    <li>
                        <code>
                            Sales-COD-DTDC
                        </code>
                    </li>

                    <li>
                        <code>
                            Sales-Prepaid-Delhivery
                        </code>
                    </li>

                    <li>
                        <code>
                            Sales-Prepaid-Pending
                        </code>
                    </li>

                </ul>

                <p>
                    <strong>
                        Special Order Examples:
                    </strong>
                </p>

                <ul>

                    <li>
                        <code>
                            Cancelled-COD-BlueDart
                        </code>
                    </li>

                    <li>
                        <code>
                            Return-Prepaid-DTDC
                        </code>
                    </li>

                    <li>
                        <code>
                            Exchange-COD-Delhivery
                        </code>
                    </li>

                    <li>
                        <code>
                            Redispatch-Prepaid-DTDC
                        </code>
                    </li>

                </ul>

            </div>


            <div class="feature warning">

                <h3>
                    ⚠️ Important Setup Instructions
                </h3>

                <p>
                    Make sure you have BOTH webhooks configured:
                </p>

                <ol>

                    <li>
                        <strong>
                            Order creation:
                        </strong>

                        <code>
                            POST /shopify/order
                        </code>

                        (Already set up)
                    </li>

                    <li>
                        <strong>
                            Order updated:
                        </strong>

                        <code>
                            POST /shopify/order
                        </code>

                        (Must add this)
                    </li>

                </ol>

                <p>
                    Both should point to:
                </p>

                <p>
                    <code>
                        https://shopify-tally-middleware.onrender.com/shopify/order
                    </code>
                </p>

                <p>
                    This ensures all status changes
                    (cancellations, returns, notes)
                    are properly tracked.
                </p>

            </div>


            <div class="feature">

                <h3>
                    📊 API Endpoints
                </h3>

                <ul>

                    <li>
                        <strong>
                            POST /shopify/order
                        </strong>
                        -
                        Webhook for all order changes
                    </li>

                    <li>
                        <strong>
                            POST /tally/orders
                        </strong>
                        -
                        Fetch orders for Tally
                    </li>

                    <li>
                        <strong>
                            POST /tally/sales
                        </strong>
                        -
                        Create order in Shopify
                    </li>

                    <li>
                        <strong>
                            POST /sync/delivery-channels
                        </strong>
                        -
                        Sync pending deliveries
                    </li>

                    <li>
                        <strong>
                            POST /fix/old-orders
                        </strong>
                        -
                        Fix old orders
                    </li>

                </ul>

            </div>

        </div>

    </body>

    </html>
    """
