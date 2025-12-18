import os
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/shopify/order")
async def shopify_order(request: Request):
    data = await request.json()
    print("🔥 Order received from Shopify")
    print(data)
    return {"status": "ok"}
