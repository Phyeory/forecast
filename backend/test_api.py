import asyncio
import aiohttp

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.get("https://frontend-api.pump.fun/coins/2rcag4mFqDeozcdn9gCtKAX87jCnwqGy31fRjg3upump") as resp:
            data = await resp.json()
            print("KEYS:", data.keys())
            print("usd_market_cap:", data.get("usd_market_cap"))
            print("market_cap (sol):", data.get("market_cap"))
            print("virtual_sol_reserves:", data.get("virtual_sol_reserves"))
            print("virtual_token_reserves:", data.get("virtual_token_reserves"))

if __name__ == "__main__":
    asyncio.run(main())
