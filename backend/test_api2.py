import asyncio
import json
import logging
import sys

# setup simple logging
logging.basicConfig(level=logging.DEBUG)

from pumpfun_client import resolve_input

async def main():
    mint, info = await resolve_input("3jeoZ6KqPmyb9vD64SndFapYV1B9YfC2Y7UUSqDppump")
    with open("test_out.json", "w") as f:
        json.dump(info or {}, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
