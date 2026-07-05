import asyncio
from emergency_agent.resource_client import query_availability
import logging
logging.basicConfig(level=logging.INFO)

async def test():
    res = await query_availability("fire", 2)
    print("RESULT:", res)

asyncio.run(test())
