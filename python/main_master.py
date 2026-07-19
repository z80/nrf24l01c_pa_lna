import asyncio
import gc

from transport_node import TransportNode

class MasterNode(TransportNode):

    async def on_command( self, src_id, command ):
        print("RX", src_id, command)
        free_bytes = gc.mem_free()
        ret = { "master free mem": free_bytes }
        return ret


async def async_main():
    tr = MasterNode(is_master=True)

    await tr.process()


def main():
    asyncio.run(async_main())


main()