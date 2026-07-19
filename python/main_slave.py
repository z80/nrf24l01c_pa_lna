import asyncio
import gc
from transport_node import TransportNode

class SlaveNode(TransportNode):

    async def on_command(self, src_id, command):
        print("RX", src_id, command)
        ret = {
            "slave_response": "all good",
        }
        return ret

    async def periodic_task(self):
        # Wait until enumeration is complete
        while self.node_id is None:
            print("WAIT enum")
            await asyncio.sleep(2)

        print("ID", self.node_id)
        print("LOOP start")

        while True:
            try:
                # Ask master how many nodes exist
                qty = await self.get_nodes_qty()
                print("NODES", qty)

                # Query each node
                for idx in range(qty):
                    info = await self.get_node_info(idx)
                    print("INFO", idx, info)

                    node_id = info.get("id")
                    if node_id is None:
                        print("SKIP no-id", idx)
                        continue
                    if node_id == self.node_id:
                        print("SKIP self", node_id)
                        continue
                    
                    free_bytes = gc.mem_free()
                    cmd = {
                        "cmd": "ping",
                        "from": self.node_id,
                        "to": node_id,
                        "message": "Hello from slave {}".format(self.node_id), 
                        "slave free bytes": free_bytes
                    }

                    print("TX", node_id, cmd)

                    try:
                        reply = await self.send_command_and_wait_reply(node_id, cmd)
                        print("REPLY", node_id, reply)
                    except Exception as e:
                        print("NOREPLY", node_id, e)

            except Exception as e:
                print("LOOP!", e)

            await asyncio.sleep(5)  # repeat every 5 seconds


async def async_main():
    tr = SlaveNode()

    # Start periodic task
    asyncio.create_task(tr.periodic_task())

    # Main processing loop
    await tr.process()


def main():
    asyncio.run(async_main())


main()


