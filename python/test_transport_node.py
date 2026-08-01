import asyncio
import importlib
import json
import os
import struct
import sys
import tempfile
import types
import unittest


class FakeTicks(types.ModuleType):
    def __init__(self):
        super().__init__("utime")
        self.now = 0

    def ticks_ms(self):
        self.now += 1
        return self.now

    @staticmethod
    def ticks_diff(new, old):
        return new - old


class FakeEvent:
    def __init__(self, unused_size):
        self.record = None

    def type(self):
        return self.record["type"]

    def flags(self):
        return self.record.get("flags", 0)

    def object_id(self):
        return self.record.get("object_id", 0)

    def source(self):
        return self.record.get("source", 0)

    def transaction(self):
        return self.record.get("transaction", 0)

    def data_length(self):
        return len(self.record.get("data", b""))

    def value0(self):
        return self.record.get("value0", 0)

    def value1(self):
        return self.record.get("value1", 0)

    def data(self):
        return self.record.get("data", b"")


class FakeCore:
    def __init__(self):
        self.events = []
        self.registrations = []
        self.commands = []
        self.opened = []
        self.pipe_data = []
        self.closed_pipes = []
        self.node_ids = []
        self.started = False
        self.pipe_write_limit = 3

    def recommended_event_size(self):
        return 256

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def poll_into(self, event):
        if not self.events:
            return False
        event.record = self.events.pop(0)
        return True

    def push(self, event_type, **fields):
        fields["type"] = event_type
        self.events.append(fields)

    def send_registration(self, destination, message_type, message_id,
                          address, data):
        self.registrations.append((
            destination, message_type, message_id, bytes(address), bytes(data)
        ))
        return True

    def set_node_id(self, node_id):
        self.node_ids.append(node_id)

    def send_command(self, destination, message_type, message_id, data):
        self.commands.append((
            destination, message_type, message_id, bytes(data)
        ))
        return True

    def open_pipe(self, destination, stream_id):
        self.opened.append((destination, stream_id))
        return 0

    def pipe_write(self, slot, data):
        count = min(len(data), self.pipe_write_limit)
        self.pipe_data.append((slot, bytes(data[:count])))
        return count

    def close_pipe(self, slot):
        self.closed_pipes.append(slot)


def import_transport_module():
    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value
    sys.modules["micropython"] = micropython
    sys.modules["ujson"] = json
    sys.modules["utime"] = FakeTicks()

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.Lock = asyncio.Lock
    uasyncio.create_task = asyncio.create_task

    async def sleep_ms(unused_milliseconds):
        await asyncio.sleep(0)

    uasyncio.sleep_ms = sleep_ms
    sys.modules["uasyncio"] = uasyncio
    sys.modules["ustruct"] = struct

    native = types.ModuleType("transport_core")
    native.TYPE_COMMAND = 1
    native.TYPE_COMMAND_REPLY = 2
    native.TYPE_STREAM = 3
    native.TYPE_MANAGEMENT_REQUEST = 4
    native.TYPE_MANAGEMENT_REPLY = 5
    native.TYPE_ENUM_HELLO = 6
    native.TYPE_ENUM_ASSIGN = 7
    native.TYPE_ENUM_CONFIRM = 8
    native.PIPE_SLOTS = 2
    native.EVENT_FLAG_TX = 4
    native.EVENT_COMMAND_READY = 1
    native.EVENT_COMMAND_SENT = 2
    native.EVENT_COMMAND_FAILED = 3
    native.EVENT_PIPE_OPENED = 4
    native.EVENT_PIPE_RX_DATA = 5
    native.EVENT_PIPE_TX_SPACE = 6
    native.EVENT_PIPE_CLOSED = 7
    native.EVENT_PIPE_FAILED = 8
    native.EVENT_CORE_ERROR = 10
    native.EVENT_REGISTRATION_RECEIVED = 11
    native.EVENT_REGISTRATION_SENT = 12
    native.EVENT_REGISTRATION_FAILED = 13
    native.Event = FakeEvent
    native.Core = object
    sys.modules["transport_core"] = native

    sys.modules.pop("transport_node", None)
    return importlib.import_module("transport_node")


class TransportNodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.transport = import_transport_module()

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    @staticmethod
    def write_identity(uuid):
        with open("identity.json", "w", encoding="utf-8") as identity_file:
            json.dump({"uuid": uuid}, identity_file)

    async def dispatch_one(self, node):
        self.assertTrue(node.core.poll_into(node._event))
        await node._dispatch_event()
        await asyncio.sleep(0)

    async def test_registration_round_trip_uses_native_datagrams(self):
        self.write_identity("0011223344556677")
        master_core = FakeCore()
        master = self.transport.TransportNode(
            is_master=True, debug=False, network_id="01B237AA",
            core=master_core,
        )

        self.write_identity("8899aabbccddeeff")
        slave_core = FakeCore()
        slave = self.transport.TransportNode(
            debug=False, network_id="01B237AA", core=slave_core,
        )

        self.assertTrue(slave._send_enum_hello())
        destination, kind, unused_id, address, uuid = \
            slave_core.registrations.pop(0)
        self.assertEqual(destination, 0)
        self.assertEqual(kind, self.transport.ENUM_HELLO)
        self.assertEqual(address, master._registration_address())

        master_core.push(
            self.transport._core.EVENT_REGISTRATION_RECEIVED,
            object_id=kind, source=0xFF, value0=destination, data=uuid,
        )
        await self.dispatch_one(master)
        destination, kind, unused_id, address, assignment = \
            master_core.registrations.pop(0)
        self.assertEqual(kind, self.transport.ENUM_ASSIGN)
        self.assertEqual(address, slave._temporary_address(slave._uuid_bytes))

        slave_core.push(
            self.transport._core.EVENT_REGISTRATION_RECEIVED,
            object_id=kind, source=0, value0=destination, data=assignment,
        )
        await self.dispatch_one(slave)
        self.assertEqual(slave.node_id, 1)
        self.assertEqual(slave_core.node_ids, [1])

        destination, kind, unused_id, address, confirmation = \
            slave_core.registrations.pop(0)
        self.assertEqual(kind, self.transport.ENUM_CONFIRM)
        self.assertEqual(address, master._endpoint_address(0))
        master_core.push(
            self.transport._core.EVENT_REGISTRATION_RECEIVED,
            object_id=kind, source=1, value0=destination, data=confirmation,
        )
        await self.dispatch_one(master)
        self.assertEqual(await master.get_nodes_qty(), 2)
        self.assertEqual((await master.get_node_info(1))["id"], 1)

    async def test_command_always_waits_for_and_returns_reply(self):
        self.write_identity("0011223344556677")
        core = FakeCore()
        node = self.transport.TransportNode(
            is_master=True, debug=False, core=core
        )
        node._mark_online(3, bytes.fromhex("0303030303030303"))

        task = asyncio.create_task(node.send_command(3, {"value": 7}))
        while not core.commands:
            await asyncio.sleep(0)
        destination, kind, message_id, unused_data = core.commands[0]
        core.push(
            self.transport._core.EVENT_COMMAND_SENT,
            source=destination, transaction=message_id, value0=kind,
        )
        await self.dispatch_one(node)
        core.push(
            self.transport._core.EVENT_COMMAND_READY,
            source=destination, transaction=message_id,
            value0=self.transport.CMD_REPLY,
            data=b'{"answer":8}',
        )
        await self.dispatch_one(node)
        self.assertEqual(await task, {"answer": 8})

    async def test_incoming_command_always_sends_callback_result(self):
        self.write_identity("0011223344556677")
        core = FakeCore()
        node = self.transport.TransportNode(
            is_master=True, debug=False, core=core
        )
        node._mark_online(2, bytes.fromhex("0202020202020202"))

        async def on_command(source_id, command):
            return {"source": source_id, "answer": command["value"] + 1}

        node.on_command = on_command
        core.push(
            self.transport._core.EVENT_COMMAND_READY,
            source=2, transaction=19, value0=self.transport.CMD,
            data=b'{"value":4}',
        )
        await self.dispatch_one(node)
        while not core.commands:
            await asyncio.sleep(0)
        destination, kind, message_id, data = core.commands[0]
        self.assertEqual((destination, kind, message_id),
                         (2, self.transport.CMD_REPLY, 19))
        self.assertEqual(json.loads(data), {"source": 2, "answer": 5})
        core.push(
            self.transport._core.EVENT_COMMAND_SENT,
            source=2, transaction=19, value0=self.transport.CMD_REPLY,
        )
        await self.dispatch_one(node)

    async def test_pipe_open_partial_write_and_confirmed_close(self):
        self.write_identity("0011223344556677")
        core = FakeCore()
        node = self.transport.TransportNode(
            is_master=True, debug=False, core=core
        )

        opening = asyncio.create_task(node.open_pipe(4))
        while not core.opened:
            await asyncio.sleep(0)
        destination, stream_id = core.opened[0]
        core.push(
            self.transport._core.EVENT_PIPE_OPENED,
            flags=self.transport._core.EVENT_FLAG_TX,
            object_id=0, source=destination, transaction=stream_id,
        )
        await self.dispatch_one(node)
        self.assertEqual(await opening, stream_id)

        await node.send_pipe(stream_id, b"abcdefgh")
        self.assertEqual(b"".join(chunk for unused_slot, chunk in core.pipe_data),
                         b"abcdefgh")

        closing = asyncio.create_task(node.send_pipe(stream_id, b"", close=True))
        while not core.closed_pipes:
            await asyncio.sleep(0)
        core.push(
            self.transport._core.EVENT_PIPE_CLOSED,
            flags=self.transport._core.EVENT_FLAG_TX,
            object_id=0, source=destination, transaction=stream_id,
        )
        await self.dispatch_one(node)
        await closing

    async def test_local_command_and_packed_online_records(self):
        self.write_identity("0011223344556677")
        node = self.transport.TransportNode(
            is_master=True, debug=False, core=FakeCore()
        )

        async def on_command(source_id, command):
            return {"source": source_id, "value": command["value"] + 1}

        node.on_command = on_command
        self.assertEqual(
            await node.send_command(0, {"value": 4}),
            {"source": 0, "value": 5},
        )
        node._mark_online(7, bytes.fromhex("0707070707070707"))
        node._mark_online(2, bytes.fromhex("0202020202020202"))
        node._mark_online(5, bytes.fromhex("0505050505050505"))
        self.assertEqual((await node.get_node_info(1))["id"], 2)
        self.assertEqual((await node.get_node_info(2))["id"], 5)
        self.assertEqual((await node.get_node_info(3))["id"], 7)


if __name__ == "__main__":
    unittest.main()
