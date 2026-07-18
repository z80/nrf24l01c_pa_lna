import asyncio

from transport_node import *

class MasterNode(TransportNode):

    async def on_command( self, src_id, command ):
        print( "node", src_id, "has sent", command )
        ret = { "master_response": "all good", "value": 123, "f_value": 12.34 }
        return ret


async def async_main():
    tr = MasterNode( role="master" )

    await tr.process()


def main():
    asyncio.run(async_main())


