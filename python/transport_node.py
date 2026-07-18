from micropython import const
import ujson
import utime
import uasyncio
import ustruct

from radio import get_nrf


# Public protocol message types.
CMD = const(1)
CMD_REPLY = const(2)
STREAM = const(3)
MGMT_REQUEST = const(4)
MGMT_REPLY = const(5)
ENUM_HELLO = const(6)
ENUM_ASSIGN = const(7)
ENUM_CONFIRM = const(8)

# Five-byte transport header followed by data and a software CRC-8:
#   protocol/type, source, destination, message id, flags | data length
HEADER_SIZE = const(5)
RADIO_PAYLOAD_SIZE = const(32)
SOFTWARE_CRC_SIZE = const(1)
MAX_CHUNK_SIZE = const(
    RADIO_PAYLOAD_SIZE - HEADER_SIZE - SOFTWARE_CRC_SIZE
)
PROTOCOL_ID = const(0xA0)
PROTOCOL_ID_MASK = const(0xF0)
MESSAGE_TYPE_MASK = const(0x0F)
LAST_PACKET = const(0x80)
PAYLOAD_LENGTH_MASK = const(0x1F)
RESERVED_FLAGS_MASK = const(0x60)

MASTER_NODE_ID = const(0)
MAX_SLAVE_NODE_ID = const(0xFD)
BROADCAST_NODE_ID = const(0xFE)       # reserved; not implemented initially
UNASSIGNED_NODE_ID = const(0xFF)

UUID_SIZE = const(8)
DEFAULT_NETWORK_ID = "D26AB53C"

# Fixed resource limits.
MAX_ONLINE_NODES = const(8)
MAX_COMMAND_SIZE = const(128)
COMMAND_REASSEMBLY_SLOTS = const(2)
MAX_PENDING_REQUESTS = const(2)
MAX_CONSECUTIVE_FAILURES = const(3)
MAX_OPEN_STREAMS = const(4)

RX_POLL_INTERVAL_MS = const(2)
RADIO_SEND_TIMEOUT_MS = const(100)
HELLO_INTERVAL_MS = const(2000)
HELLO_JITTER_MS = const(1000)
HEALTH_PROBE_INTERVAL_MS = const(5000)
REASSEMBLY_TIMEOUT_MS = const(2000)
STREAM_TIMEOUT_MS = const(10000)
REQUEST_TIMEOUT_MS = const(2000)

# Packed online-node record: UUID, node id, failures, last-seen ticks.
ONLINE_UUID = const(0)
ONLINE_NODE_ID = const(8)
ONLINE_FAILURES = const(9)
ONLINE_LAST_SEEN = const(10)
ONLINE_RECORD_SIZE = const(14)

# Packed command reassembly record.
REASM_ACTIVE = const(0)
REASM_TYPE = const(1)
REASM_SRC = const(2)
REASM_MSG_ID = const(3)
REASM_LENGTH = const(4)
REASM_LAST_SEEN = const(5)
REASM_DATA = const(9)
REASM_RECORD_SIZE = const(REASM_DATA + MAX_COMMAND_SIZE)

# Packed stream records: active, source/destination, stream id, last seen.
STREAM_ACTIVE = const(0)
STREAM_NODE_ID = const(1)
STREAM_ID = const(2)
STREAM_LAST_SEEN = const(3)
STREAM_RECORD_SIZE = const(7)

_NO_REPLY = object()


def _crc8_update(crc, values):
    """CRC-8/ATM update (polynomial 0x07), without allocating a buffer."""
    for value in values:
        crc ^= value
        for unused in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _decode_network_id(network_id):
    if isinstance(network_id, str):
        if len(network_id) != 8:
            raise ValueError(
                "network_id must contain exactly 8 hexadecimal digits"
            )
        try:
            network_id = bytes.fromhex(network_id)
        except ValueError:
            raise ValueError("network_id contains non-hexadecimal characters")
    elif isinstance(network_id, (bytes, bytearray)):
        network_id = bytes(network_id)
    else:
        raise ValueError(
            "network_id must be an 8-digit hexadecimal string or 4 bytes"
        )

    if len(network_id) != 4:
        raise ValueError("network_id must decode to exactly 4 bytes")
    return network_id


class TransportNode:
    def __init__(self, role="slave", debug=True,
                 network_id=DEFAULT_NETWORK_ID, radio=None):
        if role not in ("master", "slave"):
            raise ValueError("role must be 'master' or 'slave'")

        self.role = role
        self.debug = debug
        self.network_id = _decode_network_id(network_id)

        self._load_identity()
        self._uuid_bytes = bytes.fromhex(self.uuid)

        self.node_id = MASTER_NODE_ID if self._is_master() else None
        self._master_acknowledged = self._is_master()
        self._master_failures = 0

        # One preallocated buffer contains every currently online slave record.
        self._online_records = bytearray(
            MAX_ONLINE_NODES * ONLINE_RECORD_SIZE
        )
        self._online_count = 0

        # Two bounded command/reply reassembly records.
        self._reassembly = bytearray(
            COMMAND_REASSEMBLY_SLOTS * REASM_RECORD_SIZE
        )

        # Incoming and outgoing stream metadata are also fixed-capacity.
        self._incoming_streams = bytearray(
            MAX_OPEN_STREAMS * STREAM_RECORD_SIZE
        )
        self._outgoing_streams = bytearray(
            MAX_OPEN_STREAMS * STREAM_RECORD_SIZE
        )

        # Only active local request waits are dynamic. Their number is bounded
        # by the number of calls the application has in flight.
        self._awaiting_replies = {}
        self._last_msg_id = 0
        self._last_stream_id = 0

        now = utime.ticks_ms()
        self._last_enum_hello = now
        self._hello_delay_ms = self._next_hello_delay(initial=True)
        self._last_master_confirm = now

        # TX completion is polled asynchronously. No radio IRQ is installed.
        self.radio = radio if radio is not None else get_nrf(
            payload_size=RADIO_PAYLOAD_SIZE,
            irq_pin=None,
        )
        self._radio_lock = uasyncio.Lock()

        self.radio.stop_listening()
        for pipe_id in range(6):
            self.radio.close_rx_pipe(pipe_id)

        if self._is_master():
            # Pipe 1 owns the full base address. Pipe 2 inherits the network-id
            # suffix and differs only by its first byte.
            self.radio.open_rx_pipe(1, self._endpoint_address(MASTER_NODE_ID))
            self.radio.open_rx_pipe(2, self._registration_address())
        else:
            self.radio.open_rx_pipe(
                1, self._temporary_address(self._uuid_bytes)
            )

        self.radio.start_listening()

    # ---------- identity and addresses ----------

    def _load_identity(self):
        try:
            with open("identity.json") as identity_file:
                data = ujson.loads(identity_file.read())
            uuid = data["uuid"]
            if len(uuid) != UUID_SIZE * 2:
                raise ValueError("invalid UUID length")
            bytes.fromhex(uuid)
            self.uuid = uuid.lower()
            return
        except (OSError, ValueError, KeyError):
            pass

        try:
            import os
            raw = os.urandom(UUID_SIZE)
        except (ImportError, AttributeError):
            import urandom
            raw = bytes(urandom.getrandbits(8) for _ in range(UUID_SIZE))

        self.uuid = raw.hex()
        self._save_identity()

    def _save_identity(self):
        try:
            with open("identity.json") as identity_file:
                data = ujson.loads(identity_file.read())
            if data.get("uuid") == self.uuid:
                return
        except (OSError, ValueError):
            pass

        with open("identity.json", "w") as identity_file:
            identity_file.write(ujson.dumps({"uuid": self.uuid}))

    def _is_master(self):
        return self.role == "master"

    def _endpoint_address(self, node_id):
        if not 0 <= node_id <= MAX_SLAVE_NODE_ID:
            raise ValueError("invalid endpoint node id")
        return bytes((node_id,)) + self.network_id

    def _registration_address(self):
        return bytes((UNASSIGNED_NODE_ID,)) + self.network_id

    def _temporary_address(self, uuid_bytes):
        # A network-specific, uniformly distributed 40-bit return address.
        # The complete UUID in ENUM_ASSIGN remains the authoritative match.
        salt = b"\xA7\x39\xD4\x6E\x91"
        return bytes((
            uuid_bytes[0] ^ uuid_bytes[3] ^ self.network_id[0] ^ salt[0],
            uuid_bytes[1] ^ uuid_bytes[4] ^ self.network_id[1] ^ salt[1],
            uuid_bytes[2] ^ uuid_bytes[5] ^ self.network_id[2] ^ salt[2],
            uuid_bytes[3] ^ uuid_bytes[6] ^ self.network_id[3] ^ salt[3],
            uuid_bytes[4] ^ uuid_bytes[7] ^ self.network_id[0] ^ salt[4],
        ))

    # ---------- packed online-node storage ----------

    def _online_start(self, index):
        return index * ONLINE_RECORD_SIZE

    def _find_online_by_id(self, node_id):
        for index in range(self._online_count):
            start = self._online_start(index)
            if self._online_records[start + ONLINE_NODE_ID] == node_id:
                return index
        return -1

    def _online_last_seen(self, index):
        start = self._online_start(index)
        return ustruct.unpack_from(
            "<I", self._online_records, start + ONLINE_LAST_SEEN
        )[0]

    def _set_online_last_seen(self, index, value):
        start = self._online_start(index)
        ustruct.pack_into(
            "<I", self._online_records, start + ONLINE_LAST_SEEN, value
        )

    def _copy_record(self, dst_index, src_index):
        dst = self._online_start(dst_index)
        src = self._online_start(src_index)
        if dst < src:
            for offset in range(ONLINE_RECORD_SIZE):
                self._online_records[dst + offset] = \
                    self._online_records[src + offset]
        else:
            for offset in range(ONLINE_RECORD_SIZE - 1, -1, -1):
                self._online_records[dst + offset] = \
                    self._online_records[src + offset]

    def _mark_online(self, node_id, uuid_bytes):
        if not self._is_master() or node_id == MASTER_NODE_ID:
            return True

        now = utime.ticks_ms()
        index = self._find_online_by_id(node_id)
        if index >= 0:
            start = self._online_start(index)
            for offset in range(UUID_SIZE):
                self._online_records[start + ONLINE_UUID + offset] = \
                    uuid_bytes[offset]
            self._online_records[start + ONLINE_FAILURES] = 0
            self._set_online_last_seen(index, now)
            return True

        if self._online_count >= MAX_ONLINE_NODES:
            return False

        insert_at = self._online_count
        for current in range(self._online_count):
            start = self._online_start(current)
            if self._online_records[start + ONLINE_NODE_ID] > node_id:
                insert_at = current
                break

        for current in range(self._online_count, insert_at, -1):
            self._copy_record(current, current - 1)

        start = self._online_start(insert_at)
        for offset in range(ONLINE_RECORD_SIZE):
            self._online_records[start + offset] = 0
        for offset in range(UUID_SIZE):
            self._online_records[start + ONLINE_UUID + offset] = \
                uuid_bytes[offset]
        self._online_records[start + ONLINE_NODE_ID] = node_id
        self._online_records[start + ONLINE_FAILURES] = 0
        self._online_count += 1
        self._set_online_last_seen(insert_at, now)
        return True

    def _remove_online(self, index):
        if not 0 <= index < self._online_count:
            return
        for current in range(index, self._online_count - 1):
            self._copy_record(current, current + 1)
        self._online_count -= 1
        start = self._online_start(self._online_count)
        for offset in range(ONLINE_RECORD_SIZE):
            self._online_records[start + offset] = 0

    def _note_node_seen(self, node_id):
        if not self._is_master() or node_id == MASTER_NODE_ID:
            return
        index = self._find_online_by_id(node_id)
        if index < 0:
            return
        start = self._online_start(index)
        self._online_records[start + ONLINE_FAILURES] = 0
        self._set_online_last_seen(index, utime.ticks_ms())

    def _note_tx_failure(self, node_id):
        if not self._is_master() or node_id == MASTER_NODE_ID:
            return
        index = self._find_online_by_id(node_id)
        if index < 0:
            return
        start = self._online_start(index)
        failures = self._online_records[start + ONLINE_FAILURES]
        failures = min(255, failures + 1)
        self._online_records[start + ONLINE_FAILURES] = failures
        # Space health attempts out rather than declaring a node offline after
        # three probes fired a few milliseconds apart.
        self._set_online_last_seen(index, utime.ticks_ms())
        if failures >= MAX_CONSECUTIVE_FAILURES:
            if self.debug:
                print("[health] removing node", node_id)
            self._remove_online(index)

    def _local_node_info(self, node_index):
        if node_index == 0:
            return {
                "uuid": self.uuid,
                "node_id": MASTER_NODE_ID,
                "role": "master",
            }

        index = node_index - 1
        if not 0 <= index < self._online_count:
            return None
        start = self._online_start(index)
        uuid_bytes = bytes(
            self._online_records[
                start + ONLINE_UUID:start + ONLINE_UUID + UUID_SIZE
            ]
        )
        return {
            "uuid": uuid_bytes.hex(),
            "node_id": self._online_records[start + ONLINE_NODE_ID],
            "role": "slave",
        }

    # ---------- flash registry (scanned, never retained in RAM) ----------

    def _find_registry_assignment(self, uuid_bytes):
        wanted = uuid_bytes.hex()
        found = None
        max_node_id = 0
        try:
            with open("registry.jsonl") as registry_file:
                for line in registry_file:
                    try:
                        record = ujson.loads(line)
                        node_id = int(record["node_id"])
                        if node_id > max_node_id:
                            max_node_id = node_id
                        if record["uuid"].lower() == wanted:
                            found = node_id
                    except (ValueError, KeyError, TypeError):
                        if self.debug:
                            print("[registry] ignoring invalid record")
        except OSError:
            pass
        return found, max_node_id

    def _get_or_create_assignment(self, uuid_bytes):
        node_id, max_node_id = self._find_registry_assignment(uuid_bytes)
        if node_id is not None:
            return node_id

        node_id = max_node_id + 1
        if node_id > MAX_SLAVE_NODE_ID:
            raise RuntimeError("node id space exhausted")

        record = {"uuid": uuid_bytes.hex(), "node_id": node_id}
        with open("registry.jsonl", "a") as registry_file:
            registry_file.write(ujson.dumps(record) + "\n")
        return node_id

    def _assignment_matches(self, uuid_bytes, node_id):
        stored_id, unused_max = self._find_registry_assignment(uuid_bytes)
        return stored_id == node_id

    # ---------- radio execution ----------

    async def _recv_one(self):
        await self._radio_lock.acquire()
        try:
            if not self.radio.any():
                return None
            return self.radio.recv()
        finally:
            self._radio_lock.release()

    async def _send_payload_locked(self, packet):
        self.radio.send_start(packet)
        started = utime.ticks_ms()
        while True:
            result = self.radio.send_done()
            if result == 1:
                return
            if result == 2:
                self.radio.abort_send()
                raise OSError("send failed")
            if utime.ticks_diff(utime.ticks_ms(), started) >= \
                    RADIO_SEND_TIMEOUT_MS:
                self.radio.abort_send()
                raise OSError("send timed out")
            await uasyncio.sleep_ms(1)

    async def _send_packet_sequence(self, dst_id, msg_type, msg_id, payload,
                                    tx_address=None, mark_last=True,
                                    track_health=True):

        if not 0 < msg_type <= MESSAGE_TYPE_MASK:
            raise ValueError("invalid message type")

        if not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)

        if tx_address is None:
            tx_address = self._endpoint_address(dst_id)

        await self._radio_lock.acquire()
        failed = False

        try:
            #
            # --- ENTER TX MODE ---
            #
            self.radio.stop_listening()
            self.radio.open_tx_pipe(tx_address)

            # ACK pipe must be enabled during PTX
            self.radio.open_rx_pipe(0, tx_address)

            payload_length = len(payload)
            offset = 0

            while offset < payload_length or (payload_length == 0 and offset == 0):
                chunk = payload[offset:offset + MAX_CHUNK_SIZE]
                is_final_chunk = offset + len(chunk) >= payload_length

                # Your existing flags logic
                flags = len(chunk)
                if mark_last and is_final_chunk:
                    flags |= LAST_PACKET

                # Your existing header logic
                header = bytes((
                    PROTOCOL_ID | msg_type,
                    self.node_id if self.node_id is not None else UNASSIGNED_NODE_ID,
                    dst_id,
                    msg_id,
                    flags,
                ))

                # Your existing CRC logic
                crc = _crc8_update(0, self.network_id)
                crc = _crc8_update(crc, header)
                crc = _crc8_update(crc, chunk)

                packet = header + chunk + bytes((crc,))

                #
                # --- SEND PACKET ---
                #
                await self._send_payload_locked(packet)

                # Advance offset
                if payload_length == 0:
                    offset = 1
                else:
                    offset += len(chunk)

        except Exception:
            failed = True
            raise

        finally:
            #
            # --- EXIT TX MODE → RETURN TO RX MODE ---
            #

            # ACK pipe must NOT remain enabled in PRX mode
            self.radio.close_rx_pipe(0)

            # Restore endpoint RX pipe (pipe 1)
            if self.node_id is not None:
                self.radio.open_rx_pipe(1, self._endpoint_address(self.node_id))

            # Return to PRX mode
            self.radio.start_listening()

            self._radio_lock.release()

            # Your existing health tracking
            if track_health and 0 <= dst_id <= MAX_SLAVE_NODE_ID:
                if failed:
                    self._note_tx_failure(dst_id)
                else:
                    self._note_node_seen(dst_id)

    # ---------- main loop and periodic work ----------

    async def process(self):
        while True:
            while True:
                packet = await self._recv_one()
                if packet is None:
                    break
                try:
                    await self._handle_rx_packet(packet)
                except Exception as error:
                    # A malformed application command or failed reply must not
                    # terminate the transport service permanently.
                    if self.debug:
                        print("[transport] packet handler failed:", error)

            try:
                await self._run_periodic_tasks()
            except Exception as error:
                if self.debug:
                    print("[transport] periodic task failed:", error)
            await uasyncio.sleep_ms(RX_POLL_INTERVAL_MS)

    async def _run_periodic_tasks(self):
        now = utime.ticks_ms()
        await self._expire_reassembly(now)
        await self._expire_streams(now)

        if self._is_master():
            await self._master_periodic(now)
            return

        if not self._master_acknowledged:
            if utime.ticks_diff(now, self._last_enum_hello) >= \
                    self._hello_delay_ms:
                await self._send_enum_hello()
                self._last_enum_hello = now
                self._hello_delay_ms = self._next_hello_delay()
            return

        if utime.ticks_diff(now, self._last_master_confirm) >= \
                HEALTH_PROBE_INTERVAL_MS:
            try:
                await self._send_enum_confirm()
                self._master_failures = 0
            except OSError:
                self._master_failures += 1
                if self._master_failures >= MAX_CONSECUTIVE_FAILURES:
                    await self._become_unassigned()
            self._last_master_confirm = now

    async def _master_periodic(self, now):
        # Probe at most one idle node per loop so periodic work stays bounded.
        for index in range(self._online_count):
            if utime.ticks_diff(now, self._online_last_seen(index)) < \
                    HEALTH_PROBE_INTERVAL_MS:
                continue
            start = self._online_start(index)
            node_id = self._online_records[start + ONLINE_NODE_ID]
            try:
                await self._send_json(
                    node_id,
                    MGMT_REQUEST,
                    0,
                    {"op": "ping", "reply": False},
                )
            except OSError:
                pass
            return

    def _next_hello_delay(self, initial=False):
        # UUID-derived state prevents nodes powered together from remaining in
        # lockstep without retaining a random-generator object.
        seed = getattr(self, "_hello_seed", 0)
        if seed == 0:
            seed = 1
            for value in self._uuid_bytes:
                seed = ((seed * 33) ^ value) & 0x7FFFFFFF
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        self._hello_seed = seed
        jitter = seed % (HELLO_JITTER_MS + 1)
        return jitter if initial else HELLO_INTERVAL_MS + jitter

    # ---------- RX parsing and bounded reassembly ----------

    async def _handle_rx_packet(self, packet):
        if len(packet) < HEADER_SIZE + SOFTWARE_CRC_SIZE:
            return

        wire_type = packet[0]
        if wire_type & PROTOCOL_ID_MASK != PROTOCOL_ID:
            return
        msg_type = wire_type & MESSAGE_TYPE_MASK
        src_id = packet[1]
        dst_id = packet[2]
        msg_id = packet[3]
        flags = packet[4]
        if flags & RESERVED_FLAGS_MASK:
            return
        payload_length = flags & PAYLOAD_LENGTH_MASK
        last_packet = bool(flags & LAST_PACKET)

        if payload_length > MAX_CHUNK_SIZE:
            return
        crc_position = HEADER_SIZE + payload_length
        if crc_position >= len(packet):
            return
        payload = packet[HEADER_SIZE:HEADER_SIZE + payload_length]
        if len(payload) != payload_length:
            return

        expected_crc = _crc8_update(0, self.network_id)
        expected_crc = _crc8_update(
            expected_crc, packet[:HEADER_SIZE]
        )
        expected_crc = _crc8_update(expected_crc, payload)
        if packet[crc_position] != expected_crc:
            return

        if msg_type == ENUM_HELLO:
            if self._is_master() and dst_id == MASTER_NODE_ID:
                await self._handle_enum_hello(payload)
            return

        if msg_type == ENUM_ASSIGN:
            if not self._is_master() and dst_id == UNASSIGNED_NODE_ID:
                await self._handle_enum_assign(payload)
            return

        if msg_type == ENUM_CONFIRM:
            if self._is_master() and dst_id == MASTER_NODE_ID:
                await self._handle_enum_confirm(src_id, payload)
            return

        if self.node_id is None or dst_id != self.node_id:
            return
        if msg_type not in (
                CMD, CMD_REPLY, STREAM, MGMT_REQUEST, MGMT_REPLY):
            return
        if src_id == UNASSIGNED_NODE_ID:
            return

        if self._is_master():
            # Assigned traffic becomes eligible only after ENUM_CONFIRM.
            if src_id != MASTER_NODE_ID and \
                    self._find_online_by_id(src_id) < 0:
                return
            self._note_node_seen(src_id)

        if msg_type == STREAM:
            await self._handle_stream_chunk(
                src_id, msg_id, payload, last_packet
            )
        else:
            await self._append_reassembly(
                msg_type, src_id, msg_id, payload, last_packet
            )

    def _reasm_start(self, slot):
        return slot * REASM_RECORD_SIZE

    def _clear_reassembly(self, slot):
        start = self._reasm_start(slot)
        self._reassembly[start + REASM_ACTIVE] = 0
        self._reassembly[start + REASM_LENGTH] = 0

    async def _append_reassembly(self, msg_type, src_id, msg_id,
                                 payload, last_packet):
        selected = -1
        free_slot = -1
        for slot in range(COMMAND_REASSEMBLY_SLOTS):
            start = self._reasm_start(slot)
            if not self._reassembly[start + REASM_ACTIVE]:
                if free_slot < 0:
                    free_slot = slot
                continue
            if self._reassembly[start + REASM_TYPE] == msg_type and \
                    self._reassembly[start + REASM_SRC] == src_id and \
                    self._reassembly[start + REASM_MSG_ID] == msg_id:
                selected = slot
                break

        if selected < 0:
            selected = free_slot
            if selected < 0:
                if self.debug:
                    print("[reassembly] no free slot")
                return
            start = self._reasm_start(selected)
            self._reassembly[start + REASM_ACTIVE] = 1
            self._reassembly[start + REASM_TYPE] = msg_type
            self._reassembly[start + REASM_SRC] = src_id
            self._reassembly[start + REASM_MSG_ID] = msg_id
            self._reassembly[start + REASM_LENGTH] = 0

        start = self._reasm_start(selected)
        current_length = self._reassembly[start + REASM_LENGTH]
        new_length = current_length + len(payload)
        if new_length > MAX_COMMAND_SIZE:
            self._clear_reassembly(selected)
            if self.debug:
                print("[reassembly] message too large")
            return

        data_start = start + REASM_DATA + current_length
        for offset in range(len(payload)):
            self._reassembly[data_start + offset] = payload[offset]
        self._reassembly[start + REASM_LENGTH] = new_length
        ustruct.pack_into(
            "<I", self._reassembly, start + REASM_LAST_SEEN,
            utime.ticks_ms()
        )

        if not last_packet:
            return

        complete = bytes(
            self._reassembly[start + REASM_DATA:
                             start + REASM_DATA + new_length]
        )
        self._clear_reassembly(selected)
        await self._handle_complete_message(
            msg_type, src_id, msg_id, complete
        )

    async def _expire_reassembly(self, now):
        for slot in range(COMMAND_REASSEMBLY_SLOTS):
            start = self._reasm_start(slot)
            if not self._reassembly[start + REASM_ACTIVE]:
                continue
            last_seen = ustruct.unpack_from(
                "<I", self._reassembly, start + REASM_LAST_SEEN
            )[0]
            if utime.ticks_diff(now, last_seen) >= REASSEMBLY_TIMEOUT_MS:
                self._clear_reassembly(slot)

    async def _handle_complete_message(self, msg_type, src_id, msg_id,
                                       payload):
        try:
            value = ujson.loads(payload)
        except (ValueError, TypeError):
            if self.debug:
                print("[transport] invalid JSON payload")
            return

        if msg_type == CMD:
            result = await self.on_command(src_id, value)
            if result is not None:
                await self._send_json(src_id, CMD_REPLY, msg_id, result)
        elif msg_type == CMD_REPLY:
            self._fulfill_reply(src_id, msg_type, msg_id, value)
        elif msg_type == MGMT_REQUEST:
            await self._handle_management_request(src_id, msg_id, value)
        elif msg_type == MGMT_REPLY:
            self._fulfill_reply(src_id, msg_type, msg_id, value)

    # ---------- registration ----------

    async def _send_enum_hello(self):
        try:
            await self._send_packet_sequence(
                MASTER_NODE_ID,
                ENUM_HELLO,
                0,
                self._uuid_bytes,
                tx_address=self._registration_address(),
                track_health=False,
            )
        except OSError:
            if self.debug:
                print("[registration] hello not acknowledged")

    async def _handle_enum_hello(self, payload):
        if len(payload) != UUID_SIZE:
            return
        uuid_bytes = bytes(payload)
        try:
            node_id = self._get_or_create_assignment(uuid_bytes)
        except (OSError, RuntimeError) as error:
            if self.debug:
                print("[registration] assignment failed:", error)
            return

        assignment = uuid_bytes + bytes((node_id,))
        try:
            await self._send_packet_sequence(
                UNASSIGNED_NODE_ID,
                ENUM_ASSIGN,
                0,
                assignment,
                tx_address=self._temporary_address(uuid_bytes),
                track_health=False,
            )
        except OSError:
            # The receiver may still have obtained the packet while its ACK was
            # lost. ENUM_CONFIRM or the next HELLO resolves the uncertainty.
            if self.debug:
                print("[registration] assignment not acknowledged")

    async def _handle_enum_assign(self, payload):
        if len(payload) != UUID_SIZE + 1:
            return
        if bytes(payload[:UUID_SIZE]) != self._uuid_bytes:
            return
        node_id = payload[UUID_SIZE]
        if not 1 <= node_id <= MAX_SLAVE_NODE_ID:
            return

        await self._radio_lock.acquire()
        try:
            self.radio.stop_listening()
            self.radio.close_rx_pipe(1)
            self.radio.open_rx_pipe(1, self._endpoint_address(node_id))
            self.radio.start_listening()
            self.node_id = node_id
            self._master_acknowledged = True
            self._master_failures = 0
        finally:
            self._radio_lock.release()

        try:
            await self._send_enum_confirm()
            self._master_failures = 0
        except OSError:
            # Stay assigned and let the periodic confirmation retry. A failed
            # application confirmation does not invalidate the allocation.
            self._master_failures = 1
        self._last_master_confirm = utime.ticks_ms()

    async def _send_enum_confirm(self):
        if self.node_id is None:
            return
        payload = self._uuid_bytes + bytes((self.node_id,))
        await self._send_packet_sequence(
            MASTER_NODE_ID,
            ENUM_CONFIRM,
            0,
            payload,
            track_health=False,
        )

    async def _handle_enum_confirm(self, src_id, payload):
        if len(payload) != UUID_SIZE + 1:
            return
        uuid_bytes = bytes(payload[:UUID_SIZE])
        node_id = payload[UUID_SIZE]
        if src_id != node_id or not 1 <= node_id <= MAX_SLAVE_NODE_ID:
            return
        if not self._assignment_matches(uuid_bytes, node_id):
            return
        if not self._mark_online(node_id, uuid_bytes) and self.debug:
            print("[registration] online-node capacity reached")

    async def _become_unassigned(self):
        await self._radio_lock.acquire()
        try:
            self.radio.stop_listening()
            self.radio.close_rx_pipe(1)
            self.radio.open_rx_pipe(
                1, self._temporary_address(self._uuid_bytes)
            )
            self.radio.start_listening()
            self.node_id = None
            self._master_acknowledged = False
            self._master_failures = 0
            self._last_enum_hello = utime.ticks_ms()
            self._hello_delay_ms = self._next_hello_delay(initial=True)
        finally:
            self._radio_lock.release()

    # ---------- management and public command API ----------

    async def _send_json(self, dst_id, msg_type, msg_id, value,
                         mark_last=True):
        payload = ujson.dumps(value).encode()
        if msg_type != STREAM and len(payload) > MAX_COMMAND_SIZE:
            raise ValueError("encoded message exceeds MAX_COMMAND_SIZE")
        await self._send_packet_sequence(
            dst_id, msg_type, msg_id, payload, mark_last=mark_last
        )

    async def _handle_management_request(self, src_id, msg_id, request):
        operation = request.get("op") if isinstance(request, dict) else None
        reply = None

        if operation == "ping":
            if request.get("reply", True):
                reply = {"ok": True}
        elif self._is_master() and operation == "get_nodes_qty":
            reply = {"nodes_qty": 1 + self._online_count}
        elif self._is_master() and operation == "get_node_info":
            info = self._local_node_info(request.get("index", -1))
            reply = {"node": info}
        elif self._is_master():
            reply = {"error": "unknown_management_operation"}
        else:
            reply = {"error": "not_master"}

        if reply is not None:
            await self._send_json(src_id, MGMT_REPLY, msg_id, reply)

    def _next_msg_id(self):
        for unused in range(255):
            self._last_msg_id = (self._last_msg_id % 255) + 1
            if self._last_msg_id not in self._awaiting_replies:
                return self._last_msg_id
        raise RuntimeError("no message ids available")

    def _fulfill_reply(self, src_id, reply_type, msg_id, value):
        pending = self._awaiting_replies.get(msg_id)
        if pending is None:
            return
        if pending[0] != src_id or pending[1] != reply_type:
            return
        pending[2] = value

    async def _request(self, dst_id, request_type, reply_type, value,
                       timeout_ms=REQUEST_TIMEOUT_MS):
        if len(self._awaiting_replies) >= MAX_PENDING_REQUESTS:
            raise RuntimeError("too many pending requests")
        msg_id = self._next_msg_id()
        pending = [dst_id, reply_type, _NO_REPLY]
        self._awaiting_replies[msg_id] = pending
        try:
            await self._send_json(dst_id, request_type, msg_id, value)
            started = utime.ticks_ms()
            while pending[2] is _NO_REPLY:
                if utime.ticks_diff(utime.ticks_ms(), started) >= timeout_ms:
                    raise RuntimeError("request timed out")
                await uasyncio.sleep_ms(5)
            return pending[2]
        finally:
            self._awaiting_replies.pop(msg_id, None)

    async def get_nodes_qty(self):
        if self._is_master():
            return 1 + self._online_count
        reply = await self._request(
            MASTER_NODE_ID,
            MGMT_REQUEST,
            MGMT_REPLY,
            {"op": "get_nodes_qty"},
        )
        return reply.get("nodes_qty", 0)

    async def get_node_info(self, node_index):
        if self._is_master():
            return self._local_node_info(node_index)
        reply = await self._request(
            MASTER_NODE_ID,
            MGMT_REQUEST,
            MGMT_REPLY,
            {"op": "get_node_info", "index": node_index},
        )
        return reply.get("node")

    async def send_command(self, node_id, command):
        if node_id == self.node_id:
            await self.on_command(self.node_id, command)
            return True
        await self._send_json(
            node_id, CMD, self._next_msg_id(), command
        )
        return True

    async def send_command_and_wait_reply(self, node_id, command,
                                          timeout_ms=REQUEST_TIMEOUT_MS):
        if node_id == self.node_id:
            return await self.on_command(self.node_id, command)
        return await self._request(
            node_id, CMD, CMD_REPLY, command, timeout_ms=timeout_ms
        )

    async def on_command(self, src_id, command):
        # Return a JSON-compatible value to answer CMD_REPLY, or None for a
        # fire-and-forget command.
        return None

    # ---------- streaming API ----------

    def _stream_start(self, slot):
        return slot * STREAM_RECORD_SIZE

    def _find_stream(self, records, node_id, stream_id):
        free_slot = -1
        for slot in range(MAX_OPEN_STREAMS):
            start = self._stream_start(slot)
            if not records[start + STREAM_ACTIVE]:
                if free_slot < 0:
                    free_slot = slot
                continue
            if records[start + STREAM_NODE_ID] == node_id and \
                    records[start + STREAM_ID] == stream_id:
                return slot, free_slot
        return -1, free_slot

    def _next_stream(self):
        for unused in range(255):
            self._last_stream_id = (self._last_stream_id % 255) + 1
            in_use = False
            for slot in range(MAX_OPEN_STREAMS):
                start = self._stream_start(slot)
                if self._outgoing_streams[start + STREAM_ACTIVE] and \
                        self._outgoing_streams[start + STREAM_ID] == \
                        self._last_stream_id:
                    in_use = True
                    break
            if not in_use:
                return self._last_stream_id
        raise RuntimeError("no stream ids available")

    async def open_pipe(self, node_id):
        stream_id = self._next_stream()
        unused_existing, free_slot = self._find_stream(
            self._outgoing_streams, node_id, stream_id
        )
        if free_slot < 0:
            raise RuntimeError("too many open outgoing streams")
        start = self._stream_start(free_slot)
        self._outgoing_streams[start + STREAM_ACTIVE] = 1
        self._outgoing_streams[start + STREAM_NODE_ID] = node_id
        self._outgoing_streams[start + STREAM_ID] = stream_id
        ustruct.pack_into(
            "<I", self._outgoing_streams, start + STREAM_LAST_SEEN,
            utime.ticks_ms()
        )
        return stream_id

    async def send_pipe(self, pipe_id, data, close=False):
        selected = -1
        for slot in range(MAX_OPEN_STREAMS):
            start = self._stream_start(slot)
            if self._outgoing_streams[start + STREAM_ACTIVE] and \
                    self._outgoing_streams[start + STREAM_ID] == pipe_id:
                selected = slot
                break
        if selected < 0:
            raise ValueError("unknown outgoing pipe")

        start = self._stream_start(selected)
        dst_id = self._outgoing_streams[start + STREAM_NODE_ID]
        payload = bytes(data)

        if dst_id == self.node_id:
            await self._handle_stream_chunk(
                self.node_id, pipe_id, payload, close
            )
        else:
            await self._send_packet_sequence(
                dst_id, STREAM, pipe_id, payload, mark_last=close
            )

        if close:
            self._outgoing_streams[start + STREAM_ACTIVE] = 0
        else:
            ustruct.pack_into(
                "<I", self._outgoing_streams, start + STREAM_LAST_SEEN,
                utime.ticks_ms()
            )

    async def _handle_stream_chunk(self, src_id, stream_id, payload,
                                   last_packet):
        selected, free_slot = self._find_stream(
            self._incoming_streams, src_id, stream_id
        )
        if selected < 0:
            if free_slot < 0:
                if self.debug:
                    print("[stream] no free incoming stream slot")
                return
            selected = free_slot
            start = self._stream_start(selected)
            self._incoming_streams[start + STREAM_ACTIVE] = 1
            self._incoming_streams[start + STREAM_NODE_ID] = src_id
            self._incoming_streams[start + STREAM_ID] = stream_id
            await self.on_pipe_opened(stream_id, src_id)

        start = self._stream_start(selected)
        ustruct.pack_into(
            "<I", self._incoming_streams, start + STREAM_LAST_SEEN,
            utime.ticks_ms()
        )
        if payload:
            await self.on_pipe_data(stream_id, src_id, payload)
        if last_packet:
            self._incoming_streams[start + STREAM_ACTIVE] = 0
            await self.on_pipe_closed(stream_id, src_id)

    async def _expire_streams(self, now):
        for slot in range(MAX_OPEN_STREAMS):
            start = self._stream_start(slot)
            if not self._incoming_streams[start + STREAM_ACTIVE]:
                continue
            last_seen = ustruct.unpack_from(
                "<I", self._incoming_streams, start + STREAM_LAST_SEEN
            )[0]
            if utime.ticks_diff(now, last_seen) >= STREAM_TIMEOUT_MS:
                stream_id = self._incoming_streams[start + STREAM_ID]
                src_id = self._incoming_streams[start + STREAM_NODE_ID]
                self._incoming_streams[start + STREAM_ACTIVE] = 0
                await self.on_pipe_closed(stream_id, src_id)

        for slot in range(MAX_OPEN_STREAMS):
            start = self._stream_start(slot)
            if not self._outgoing_streams[start + STREAM_ACTIVE]:
                continue
            last_seen = ustruct.unpack_from(
                "<I", self._outgoing_streams, start + STREAM_LAST_SEEN
            )[0]
            if utime.ticks_diff(now, last_seen) >= STREAM_TIMEOUT_MS:
                self._outgoing_streams[start + STREAM_ACTIVE] = 0

    async def on_pipe_opened(self, pipe_id, src_id):
        pass

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        pass

    async def on_pipe_closed(self, pipe_id, src_id):
        pass
