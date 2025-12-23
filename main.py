import json
import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# -------------------------------------------------
# Environment variables
# -------------------------------------------------
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE_NAME", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01").strip()

USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "83.0"))
GST_PERCENT = float(os.getenv("GST_PERCENT", "18.0"))

# -------------------------------------------------
# Local storage (Webhook Orders)
# -------------------------------------------------
ORDERS_FILE = "orders.json"


def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            return json.load(f)
    return []


def save_orders(orders):
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f, indent=2)


# -------------------------------------------------
# Shopify → Middleware (Webhook)
# -------------------------------------------------
@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()
    orders = load_orders()
    orders.append(data)
    save_orders(orders)
    return {"status": "ok"}


# -------------------------------------------------
# GST Helper
# -------------------------------------------------
def calculate_gst(amount_inr: float):
    gst_total = round((amount_inr * GST_PERCENT) / 100, 2)
    return {
        "cgst": round(gst_total / 2, 2),
        "sgst": round(gst_total / 2, 2),
        "igst": 0.0
    }


# -------------------------------------------------
# IST → UTC conversion
# -------------------------------------------------
def ist_date_to_utc_range(date_str: str):
    ist = timezone(timedelta(hours=5, minutes=30))

    start_ist = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=ist
    )
    end_ist = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=ist
    )

    return (
        start_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end_ist.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


# -------------------------------------------------
# REST CUSTOMER EXTRACTION
# -------------------------------------------------
def extract_customer(order: dict):
    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or {}

    notes = {
        (n.get("name") or "").strip().lower(): (n.get("value") or "").strip()
        for n in order.get("note_attributes", [])
        if n.get("name") and n.get("value")
    }

    customer_name = "Cash Customer"
    customer_email = (
        customer.get("email")
        or order.get("email")
        or order.get("contact_email")
        or notes.get("email")
    )

    if billing:
        customer_name = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip() or customer_name
    elif shipping:
        customer_name = f"{shipping.get('first_name','')} {shipping.get('last_name','')}".strip() or customer_name
    elif notes.get("name"):
        customer_name = notes.get("name")

    return {
        "name": customer_name,
        "email": customer_email,
        "phone": None
    }


# -------------------------------------------------
# GRAPHQL HELPER
# -------------------------------------------------
def shopify_graphql(query: str, variables: dict):
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, json={
        "query": query,
        "variables": variables
    })

    if response.status_code != 200:
        raise HTTPException(500, response.text)

    data = response.json()
    if "errors" in data:
        raise HTTPException(500, data["errors"])

    return data["data"]


# -------------------------------------------------
# GRAPHQL FETCH ORDERS
# -------------------------------------------------
def fetch_orders_graphql(from_date: str, to_date: str):
    from_utc, _ = ist_date_to_utc_range(from_date)
    _, to_utc = ist_date_to_utc_range(to_date)

    query = """
    query ($query: String!) {
      orders(first: 250, query: $query) {
        edges {
          node {
            id
            name
            displayName
            createdAt
            email
            totalPriceSet {
              shopMoney {
                amount
              }
            }
            customer {
              displayName
              email
              phone
            }
            shippingAddress {
              firstName
              lastName
              phone
            }
            billingAddress {
              firstName
              lastName
              phone
            }
            lineItems(first: 50) {
              edges {
                node {
                  title
                  quantity
                  originalUnitPriceSet {
                    shopMoney {
                      amount
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    search = f"created_at:>={from_utc} created_at:<={to_utc}"
    data = shopify_graphql(query, {"query": search})
    return data["orders"]["edges"]


# -------------------------------------------------
# GRAPHQL CUSTOMER EXTRACTION (ADMIN-ACCURATE)
# -------------------------------------------------
def extract_customer_graphql(order: dict):
    name = (
        order.get("displayName")
        or (order.get("customer") or {}).get("displayName")
        or "Cash Customer"
    )

    email = (
        (order.get("customer") or {}).get("email")
        or order.get("email")
    )

    phone = (
        (order.get("customer") or {}).get("phone")
        or (order.get("billingAddress") or {}).get("phone")
        or (order.get("shippingAddress") or {}).get("phone")
    )

    return {
        "name": name,
        "email": email,
        "phone": phone
    }


# -------------------------------------------------
# GRAPHQL → TALLY
# -------------------------------------------------
@app.post("/tally/orders/shopify/graphql")
async def get_shopify_orders_graphql(request: Request):
    body = await request.json()
    from_date = body.get("from_date")
    to_date = body.get("to_date")

    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")

    edges = fetch_orders_graphql(from_date, to_date)
    tally_orders = []

    for edge in edges:
        order = edge["node"]
        customer = extract_customer_graphql(order)

        items = []
        for li in order["lineItems"]["edges"]:
            node = li["node"]
            qty = node["quantity"]
            rate_inr = round(float(node["originalUnitPriceSet"]["shopMoney"]["amount"]) * USD_TO_INR_RATE, 2)
            amount = round(rate_inr * qty, 2)

            items.append({
                "item_name": node["title"],
                "quantity": qty,
                "rate": rate_inr,
                "amount": amount,
                "gst": calculate_gst(amount)
            })

        total_inr = round(float(order["totalPriceSet"]["shopMoney"]["amount"]) * USD_TO_INR_RATE, 2)

        tally_orders.append({
            "voucher_type": "Sales",
            "voucher_number": order["name"].replace("#", ""),
            "voucher_date": order["createdAt"][:10],
            "customer": customer,
            "items": items,
            "total_amount": total_inr,
            "currency": "INR",
            "source": "Shopify (GraphQL)",
            "shopify_order_id": order["id"]
        })

    return {"orders": tally_orders}
