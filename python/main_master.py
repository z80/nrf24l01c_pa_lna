import asyncio

from transport_node import TransportNode

class MasterNode(TransportNode):

    async def on_command( self, src_id, command ):
        print("RX", src_id, command)
        ret = { "master_response": "all good", "value": 123, "f_value": 12.34 }
        return ret


async def async_main():
    tr = MasterNode(is_master=True)

    await tr.process()


def main():
    asyncio.run(async_main())


