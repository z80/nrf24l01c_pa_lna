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


class FakeRadio:
    def __init__(self):
        self.rx = []
        self.sent = []
        self.rx_pipes = {}
        self.tx_address = None
        self.listening = False
        self._tx_pending = False

    def stop_listening(self):
        self.listening = False

    def start_listening(self):
        self.listening = True

    def open_rx_pipe(self, pipe_id, address):
        self.rx_pipes[pipe_id] = bytes(address)

    def close_rx_pipe(self, pipe_id):
        self.rx_pipes.pop(pipe_id, None)

    def open_tx_pipe(self, address):
        self.tx_address = bytes(address)

    def any(self):
        return bool(self.rx)

    def recv(self):
        return self.rx.pop(0)

    def send_start(self, packet):
        self.sent.append((self.tx_address, bytes(packet)))
        self._tx_pending = True

    def send_done(self):
        if self._tx_pending:
            self._tx_pending = False
            return 1
        return None

    def read_observe_tx(self):
        return 0

    def abort_send(self):
        self._tx_pending = False


def import_transport_module():
    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value
    sys.modules["micropython"] = micropython
    sys.modules["ujson"] = json
    sys.modules["utime"] = FakeTicks()

    uasyncio = types.ModuleType("uasyncio")
    uasyncio.Lock = asyncio.Lock

    async def sleep_ms(milliseconds):
        await asyncio.sleep(0)

    uasyncio.sleep_ms = sleep_ms
    sys.modules["uasyncio"] = uasyncio
    sys.modules["ustruct"] = struct

    radio = types.ModuleType("radio")
    radio.get_nrf = lambda **unused: FakeRadio()
    sys.modules["radio"] = radio

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

    async def test_addresses_and_registration_round_trip(self):
        self.write_identity("0011223344556677")
        master_radio = FakeRadio()
        master = self.transport.TransportNode(
            is_master=True, debug=False,
            network_id="01B237AA", radio=master_radio,
        )

        self.write_identity("8899aabbccddeeff")
        slave_radio = FakeRadio()
        slave = self.transport.TransportNode(
            is_master=False, debug=False,
            network_id="01b237aa", radio=slave_radio,
        )

        self.assertEqual(master.network_id, b"\x01\xb2\x37\xaa")
        self.assertEqual(
            master_radio.rx_pipes[1], b"\x00\x01\xb2\x37\xaa"
        )
        self.assertEqual(
            master_radio.rx_pipes[2], b"\xff\x01\xb2\x37\xaa"
        )

        await slave._send_enum_hello()
        hello_address, hello_packet = slave_radio.sent.pop(0)
        self.assertEqual(hello_address, master._registration_address())
        await master._handle_rx_packet(hello_packet)

        assignment_address, assignment_packet = master_radio.sent.pop(0)
        self.assertEqual(
            assignment_address,
            slave._temporary_address(slave._uuid_bytes),
        )
        await slave._handle_rx_packet(assignment_packet)

        confirm_address, confirm_packet = slave_radio.sent.pop(0)
        self.assertEqual(confirm_address, master._endpoint_address(0))
        await master._handle_rx_packet(confirm_packet)

        self.assertEqual(slave.node_id, 1)
        self.assertEqual(slave_radio.rx_pipes[1], master._endpoint_address(1))
        self.assertEqual(await master.get_nodes_qty(), 2)
        self.assertEqual(
            await master.get_node_info(0),
            {"uuid": master.uuid, "id": 0},
        )
        self.assertEqual(
            await master.get_node_info(1),
            {"uuid": slave.uuid, "id": 1},
        )

        # A duplicate hello reuses the allocation and does not append a record.
        await master._handle_rx_packet(hello_packet)
        with open("registry.jsonl", encoding="utf-8") as registry_file:
            self.assertEqual(len(registry_file.readlines()), 1)

    async def test_packed_online_records_are_sorted_and_removed(self):
        self.write_identity("0011223344556677")
        node = self.transport.TransportNode(
            is_master=True, debug=False, radio=FakeRadio()
        )
        node._mark_online(7, bytes.fromhex("0707070707070707"))
        node._mark_online(2, bytes.fromhex("0202020202020202"))
        node._mark_online(5, bytes.fromhex("0505050505050505"))

        self.assertEqual((await node.get_node_info(1))["id"], 2)
        self.assertEqual((await node.get_node_info(2))["id"], 5)
        self.assertEqual((await node.get_node_info(3))["id"], 7)

        for unused in range(self.transport.MAX_CONSECUTIVE_FAILURES):
            node._note_tx_failure(5)
        self.assertEqual(await node.get_nodes_qty(), 3)
        self.assertEqual((await node.get_node_info(2))["id"], 7)

    async def test_fragmented_command_uses_bounded_reassembly(self):
        self.write_identity("0011223344556677")
        slave = self.transport.TransportNode(
            is_master=False, debug=False, radio=FakeRadio()
        )
        slave.node_id = 3
        received = []

        async def on_command(src_id, command):
            received.append((src_id, command))

        slave.on_command = on_command
        command = {"command": "x" * 60}
        encoded = json.dumps(command).encode()
        await slave._append_reassembly(
            self.transport.CMD, 0, 9, encoded[:27], False
        )
        await slave._append_reassembly(
            self.transport.CMD, 0, 9, encoded[27:54], False
        )
        await slave._append_reassembly(
            self.transport.CMD, 0, 9, encoded[54:], True
        )

        self.assertEqual(received, [(0, command)])
        self.assertEqual(slave._reassembly[0], 0)

    async def test_local_master_command_has_same_endpoint_api(self):
        self.write_identity("0011223344556677")
        master = self.transport.TransportNode(
            is_master=True, debug=False, radio=FakeRadio()
        )

        async def on_command(src_id, command):
            return {"source": src_id, "value": command["value"] + 1}

        master.on_command = on_command
        reply = await master.send_command_and_wait_reply(0, {"value": 4})
        self.assertEqual(reply, {"source": 0, "value": 5})

    async def test_remote_replies_wait_for_radio_retry_window(self):
        self.write_identity("0011223344556677")
        radio = FakeRadio()
        node = self.transport.TransportNode(
            is_master=True, debug=False, radio=radio
        )
        waits = []

        async def sleep_ms(milliseconds):
            waits.append(milliseconds)

        async def on_command(src_id, command):
            return {"ok": True}

        self.transport.uasyncio.sleep_ms = sleep_ms
        node.on_command = on_command

        await node._handle_complete_message(
            self.transport.CMD, 1, 7, b'{"cmd":1}'
        )
        await node._handle_complete_message(
            self.transport.MGMT_REQUEST, 1, 8, b'{"op":1}'
        )

        self.assertEqual(
            waits,
            [self.transport._REPLY_TURNAROUND_MS] * 2,
        )
        self.assertEqual(len(radio.sent), 2)

    async def test_slave_management_request_uses_common_master_endpoint(self):
        self.write_identity("0011223344556677")
        master_radio = FakeRadio()
        master = self.transport.TransportNode(
            is_master=True, debug=False, radio=master_radio
        )
        master._mark_online(1, bytes.fromhex("8899aabbccddeeff"))

        self.write_identity("8899aabbccddeeff")
        slave_radio = FakeRadio()
        slave = self.transport.TransportNode(
            is_master=False, debug=False, radio=slave_radio
        )
        slave.node_id = 1
        slave._master_acknowledged = True

        request_task = asyncio.create_task(slave.get_nodes_qty())
        while not slave_radio.sent:
            await asyncio.sleep(0)
        request_address, request_packet = slave_radio.sent.pop(0)
        self.assertEqual(request_address, master._endpoint_address(0))

        await master._handle_rx_packet(request_packet)
        reply_address, reply_packet = master_radio.sent.pop(0)
        self.assertEqual(reply_address, slave._endpoint_address(1))
        await slave._handle_rx_packet(reply_packet)

        self.assertEqual(await request_task, 2)

    async def test_protocol_marker_and_crc_reject_foreign_packets(self):
        self.assertEqual(
            self.transport._crc8_update(0, b"123456789"), 0xF4
        )

        self.write_identity("0011223344556677")
        sender_radio = FakeRadio()
        sender = self.transport.TransportNode(
            is_master=True, debug=False, radio=sender_radio
        )

        self.write_identity("8899aabbccddeeff")
        receiver = self.transport.TransportNode(
            is_master=False, debug=False, radio=FakeRadio()
        )
        receiver.node_id = 3
        received = []

        async def on_command(src_id, command):
            received.append((src_id, command))

        receiver.on_command = on_command
        await sender.send_command(3, {"value": 7})
        unused_address, valid_packet = sender_radio.sent.pop(0)
        self.assertNotIn(0, sender_radio.rx_pipes)
        self.assertEqual(sender_radio.rx_pipes[1], sender._endpoint_address(0))

        self.assertEqual(
            valid_packet[0] & self.transport.PROTOCOL_ID_MASK,
            self.transport.PROTOCOL_ID,
        )
        self.assertLessEqual(
            valid_packet[4] & self.transport.PAYLOAD_LENGTH_MASK,
            self.transport.MAX_CHUNK_SIZE,
        )

        bad_marker = bytearray(valid_packet)
        bad_marker[0] ^= 0x10
        await receiver._handle_rx_packet(bad_marker)

        bad_crc = bytearray(valid_packet)
        bad_crc[self.transport.HEADER_SIZE] ^= 0x01
        await receiver._handle_rx_packet(bad_crc)

        self.assertEqual(received, [])
        await receiver._handle_rx_packet(valid_packet)
        self.assertEqual(received, [(0, {"value": 7})])

    async def test_periodic_service_waits_for_idle_transport(self):
        self.write_identity("8899aabbccddeeff")
        slave_radio = FakeRadio()
        slave = self.transport.TransportNode(
            is_master=False, debug=False, radio=slave_radio
        )
        slave.node_id = 1
        slave._master_acknowledged = True
        slave._confirm_delay_ms = 1
        slave._last_master_confirm = 0

        ticks = sys.modules["utime"]
        ticks.now = 1000
        slave._last_radio_activity = 0
        slave._awaiting_replies[7] = [0, self.transport.MGMT_REPLY, object()]
        await slave._run_periodic_tasks()
        self.assertEqual(slave_radio.sent, [])

        slave._awaiting_replies.clear()
        await slave._append_reassembly(
            self.transport.CMD, 0, 9, b"{", False
        )
        ticks.now += 1000
        slave._last_radio_activity = 0
        await slave._run_periodic_tasks()
        self.assertEqual(slave_radio.sent, [])

        slave._clear_reassembly(0)
        ticks.now += self.transport.BACKGROUND_QUIET_MS + 1
        slave._last_radio_activity = 0
        await slave._run_periodic_tasks()
        self.assertEqual(len(slave_radio.sent), 1)
        self.assertEqual(
            slave_radio.sent[0][1][0] & self.transport.MESSAGE_TYPE_MASK,
            self.transport.ENUM_CONFIRM,
        )
        self.assertGreaterEqual(
            slave._confirm_delay_ms,
            self.transport.SLAVE_CONFIRM_INTERVAL_MS,
        )
        self.assertLessEqual(
            slave._confirm_delay_ms,
            self.transport.SLAVE_CONFIRM_INTERVAL_MS
            + self.transport.SLAVE_CONFIRM_JITTER_MS,
        )

        # A background sender must re-check idleness after waiting for the
        # radio lock; otherwise it can act on a stale pre-lock decision.
        slave_radio.sent.clear()
        await slave._radio_lock.acquire()
        background_task = asyncio.create_task(
            slave._send_enum_confirm(background=True)
        )
        await asyncio.sleep(0)
        slave._last_radio_activity = ticks.ticks_ms()
        slave._radio_lock.release()
        self.assertFalse(await background_task)
        self.assertEqual(slave_radio.sent, [])


if __name__ == "__main__":
    unittest.main()
