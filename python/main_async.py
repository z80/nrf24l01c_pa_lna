import asyncio

from transport_node import *


async def main():
    tr = TransportNode()

    await tr.process()


