import asyncio
from transport_node import *

class SlaveNode(TransportNode):

    async def on_command(self, src_id, command):
        print("node", src_id, "has sent", command)
        ret = {
            "slave_response": "all good",
        }
        return ret

    async def periodic_task(self):
        # Wait until enumeration is complete
        while self.node_id is None:
            print("Waiting for enumeration...")
            await asyncio.sleep(2)

        print("Slave node ID:", self.node_id)
        print("Starting periodic broadcast loop")

        while True:
            try:
                # Ask master how many nodes exist
                qty = await self.get_nodes_qty()
                print("Master reports", qty, "nodes")

                # Query each node
                for idx in range(qty):
                    info = await self.get_node_info(idx)
                    print("Node", idx, "info:", info)

                    node_id = info.get("node_id")
                    if node_id is None:
                        print("Node", idx, "has no node_id, skipping")
                        continue
                    if node_id == self.node_id:
                        print( "Node", idx, "is the same node as this one with node_id =", node_id, "skipping" )
                        continue

                    cmd = {
                        "cmd": "ping",
                        "from": self.node_id,
                        "to": node_id,
                        "message": "Hello from slave {}".format(self.node_id)
                    }

                    print("Sending command to node", node_id, ":", cmd)

                    try:
                        reply = await self.send_command_and_wait_reply(node_id, cmd)
                        print("Reply from node", node_id, ":", reply)
                    except Exception as e:
                        print("Node", node_id, "did not reply:", e)

            except Exception as e:
                print("Error during periodic task:", e)

            await asyncio.sleep(5)  # repeat every 5 seconds


async def async_main():
    tr = SlaveNode(role="slave")

    # Start periodic task
    asyncio.create_task(tr.periodic_task())

    # Main processing loop
    await tr.process()


def main():
    asyncio.run(async_main())

