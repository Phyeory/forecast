import httpx, asyncio
async def fetch():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get('https://gamma-api.polymarket.com/events?limit=10')
            print(resp.status_code, resp.text[:200])
        except Exception as e:
            print("Error type:", type(e), "repr:", repr(e))
asyncio.run(fetch())
