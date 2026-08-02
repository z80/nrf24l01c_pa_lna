from micropython import const
import uasyncio
import ujson
import ustruct
import utime

import transport_core as _core


CMD = _core.TYPE_COMMAND
CMD_REPLY = _core.TYPE_COMMAND_REPLY
STREAM = _core.TYPE_STREAM
MGMT_REQUEST = _core.TYPE_MANAGEMENT_REQUEST
MGMT_REPLY = _core.TYPE_MANAGEMENT_REPLY
ENUM_HELLO = _core.TYPE_ENUM_HELLO
ENUM_ASSIGN = _core.TYPE_ENUM_ASSIGN
ENUM_CONFIRM = _core.TYPE_ENUM_CONFIRM

MASTER_NODE_ID = const(0)
MAX_SLAVE_NODE_ID = const(0xFD)
UNASSIGNED_NODE_ID = const(0xFF)
UUID_SIZE = const(8)
DEFAULT_NETWORK_ID = "D26AB53C"

MAX_ONLINE_NODES = const(8)
MAX_PENDING_REQUESTS = const(2)
MAX_CONSECUTIVE_FAILURES = const(3)
MAX_OPEN_STREAMS = _core.PIPE_SLOTS
MAX_COMMAND_SIZE = const(128)
PIPE_BUFFER_SIZE = const(2048)
PIPE_EVENT_BYTES = const(512)

POLL_INTERVAL_MS = const(1)
MAX_EVENTS_PER_POLL = const(8)
REQUEST_TIMEOUT_MS = const(2000)
PIPE_OPERATION_TIMEOUT_MS = const(12000)
HELLO_INTERVAL_MS = const(2000)
HELLO_JITTER_MS = const(1000)
HEALTH_PROBE_INTERVAL_MS = const(7000)
HEALTH_PROBE_JITTER_MS = const(3000)
SLAVE_CONFIRM_INTERVAL_MS = const(20000)
SLAVE_CONFIRM_JITTER_MS = const(5000)
BACKGROUND_QUIET_MS = const(300)

_OP_PING = const(0)
_OP_GET_QTY = const(1)
_OP_GET_INFO = const(2)

_ONLINE_UUID = const(0)
_ONLINE_NODE_ID = const(8)
_ONLINE_FAILURES = const(9)
_ONLINE_LAST_SEEN = const(10)
_ONLINE_RECORD_SIZE = const(14)

_PIPE_ACTIVE = const(0)
_PIPE_NODE_ID = const(1)
_PIPE_STREAM_ID = const(2)
_PIPE_STATE = const(3)
_PIPE_RECORD_SIZE = const(4)
_PIPE_OPENING = const(1)
_PIPE_OPEN = const(2)
_PIPE_CLOSING = const(3)
_PIPE_CLOSED = const(4)
_PIPE_FAILED = const(5)

_NO_REPLY = object()
_TX_WAITING = object()


def _provide_buffer(unused_kind, unused_index, minimum_size):
    return bytearray(minimum_size)


def _decode_network_id(network_id):
    if isinstance(network_id, str):
        if len(network_id) != 8:
            raise ValueError("network_id must contain 8 hexadecimal digits")
        try:
            network_id = bytes.fromhex(network_id)
        except ValueError:
            raise ValueError("network_id contains non-hexadecimal characters")
    elif isinstance(network_id, (bytes, bytearray)):
        network_id = bytes(network_id)
    else:
        raise ValueError("network_id must be an 8-digit string or 4 bytes")
    if len(network_id) != 4:
        raise ValueError("network_id must decode to 4 bytes")
    return network_id


def _default_hardware(spi_id, spi_baud, cs_name, ce_name, irq_name):
    from pyb import SPI, Pin

    spi = SPI(spi_id)
    try:
        spi.init(spi.MASTER, baudrate=spi_baud, polarity=0, phase=0)
    except AttributeError:
        spi.init(baudrate=spi_baud, polarity=0, phase=0)
    cs = Pin(cs_name, Pin.OUT, value=1)
    ce = Pin(ce_name, Pin.OUT, value=0)
    irq = Pin(irq_name, Pin.IN, Pin.PULL_UP)
    return spi, cs, ce, irq


class TransportNode:
    def __init__(self, is_master=False, debug=True,
                 network_id=DEFAULT_NETWORK_ID, core=None,
                 spi=None, cs=None, ce=None, irq=None,
                 spi_id=1, spi_baud=4000000,
                 cs_pin="C4", ce_pin="C5", irq_pin="A4"):
        self.is_master = bool(is_master)
        self.debug = bool(debug)
        self.network_id = _decode_network_id(network_id)
        self._load_identity()
        self._uuid_bytes = bytes.fromhex(self.uuid)

        self.node_id = MASTER_NODE_ID if self.is_master else None
        self._master_acknowledged = self.is_master
        self._master_failures = 0

        self._online_records = bytearray(
            MAX_ONLINE_NODES * _ONLINE_RECORD_SIZE
        )
        self._online_count = 0
        self._awaiting_replies = {}
        self._tx_results = {}
        self._last_msg_id = 0
        self._last_stream_id = 0
        self._outgoing_pipes = bytearray(
            MAX_OPEN_STREAMS * _PIPE_RECORD_SIZE
        )
        self._pipe_failures = [0] * MAX_OPEN_STREAMS
        self._incoming_pipe_active = bytearray(MAX_OPEN_STREAMS)
        self._pipe_callback_queues = [[] for unused in range(MAX_OPEN_STREAMS)]
        self._pipe_callback_running = bytearray(MAX_OPEN_STREAMS)
        self._send_lock = uasyncio.Lock()
        self._background_task_active = False

        now = utime.ticks_ms()
        self._last_enum_hello = now
        self._hello_delay_ms = self._next_hello_delay(initial=True)
        self._last_master_confirm = now
        self._confirm_delay_ms = self._next_service_delay(
            SLAVE_CONFIRM_INTERVAL_MS, SLAVE_CONFIRM_JITTER_MS
        )
        self._last_radio_activity = now

        if core is None:
            if spi is None:
                spi, cs, ce, irq = _default_hardware(
                    spi_id, spi_baud, cs_pin, ce_pin, irq_pin
                )
            elif cs is None or ce is None or irq is None:
                raise ValueError("spi, cs, ce, and irq must be provided together")

            initial_id = MASTER_NODE_ID if self.is_master else \
                UNASSIGNED_NODE_ID
            if self.is_master:
                core = _core.Core(
                    spi=spi, cs=cs, ce=ce, irq=irq,
                    buffer_provider=_provide_buffer,
                    node_id=initial_id, network_id=self.network_id,
                    command_size=MAX_COMMAND_SIZE,
                    pipe_buffer_size=PIPE_BUFFER_SIZE,
                    pipe_event_bytes=PIPE_EVENT_BYTES,
                )
            else:
                core = _core.Core(
                    spi=spi, cs=cs, ce=ce, irq=irq,
                    buffer_provider=_provide_buffer,
                    node_id=initial_id, network_id=self.network_id,
                    service_address=self._temporary_address(self._uuid_bytes),
                    command_size=MAX_COMMAND_SIZE,
                    pipe_buffer_size=PIPE_BUFFER_SIZE,
                    pipe_event_bytes=PIPE_EVENT_BYTES,
                )
        self.core = core
        self._event = _core.Event(self.core.recommended_event_size())
        self.core.start()

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
            raw = bytes(urandom.getrandbits(8) for unused in range(UUID_SIZE))
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

    def _endpoint_address(self, node_id):
        if not 0 <= node_id <= MAX_SLAVE_NODE_ID:
            raise ValueError("invalid endpoint node id")
        return bytes((node_id,)) + self.network_id

    def _registration_address(self):
        return bytes((UNASSIGNED_NODE_ID,)) + self.network_id

    def _temporary_address(self, uuid_bytes):
        salt = b"\xA7\x39\xD4\x6E\x91"
        return bytes((
            uuid_bytes[0] ^ uuid_bytes[3] ^ self.network_id[0] ^ salt[0],
            uuid_bytes[1] ^ uuid_bytes[4] ^ self.network_id[1] ^ salt[1],
            uuid_bytes[2] ^ uuid_bytes[5] ^ self.network_id[2] ^ salt[2],
            uuid_bytes[3] ^ uuid_bytes[6] ^ self.network_id[3] ^ salt[3],
            uuid_bytes[4] ^ uuid_bytes[7] ^ self.network_id[0] ^ salt[4],
        ))

    # ---------- compact online registry ----------

    def _online_start(self, index):
        return index * _ONLINE_RECORD_SIZE

    def _find_online_by_id(self, node_id):
        for index in range(self._online_count):
            start = self._online_start(index)
            if self._online_records[start + _ONLINE_NODE_ID] == node_id:
                return index
        return -1

    def _online_last_seen(self, index):
        return ustruct.unpack_from(
            "<I", self._online_records,
            self._online_start(index) + _ONLINE_LAST_SEEN
        )[0]

    def _set_online_last_seen(self, index, value):
        ustruct.pack_into(
            "<I", self._online_records,
            self._online_start(index) + _ONLINE_LAST_SEEN, value
        )

    def _copy_record(self, destination, source):
        dst = self._online_start(destination)
        src = self._online_start(source)
        if dst < src:
            offsets = range(_ONLINE_RECORD_SIZE)
        else:
            offsets = range(_ONLINE_RECORD_SIZE - 1, -1, -1)
        for offset in offsets:
            self._online_records[dst + offset] = self._online_records[src + offset]

    def _mark_online(self, node_id, uuid_bytes):
        if not self.is_master or node_id == MASTER_NODE_ID:
            return True
        now = utime.ticks_ms()
        index = self._find_online_by_id(node_id)
        if index >= 0:
            start = self._online_start(index)
            self._online_records[start:start + UUID_SIZE] = uuid_bytes
            self._online_records[start + _ONLINE_FAILURES] = 0
            self._set_online_last_seen(index, now)
            return True
        if self._online_count >= MAX_ONLINE_NODES:
            return False
        insert_at = self._online_count
        for current in range(self._online_count):
            if self._online_records[
                    self._online_start(current) + _ONLINE_NODE_ID] > node_id:
                insert_at = current
                break
        for current in range(self._online_count, insert_at, -1):
            self._copy_record(current, current - 1)
        start = self._online_start(insert_at)
        for offset in range(_ONLINE_RECORD_SIZE):
            self._online_records[start + offset] = 0
        self._online_records[start:start + UUID_SIZE] = uuid_bytes
        self._online_records[start + _ONLINE_NODE_ID] = node_id
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
        for offset in range(_ONLINE_RECORD_SIZE):
            self._online_records[start + offset] = 0

    def _note_node_seen(self, node_id):
        if not self.is_master or node_id == MASTER_NODE_ID:
            return
        index = self._find_online_by_id(node_id)
        if index >= 0:
            start = self._online_start(index)
            self._online_records[start + _ONLINE_FAILURES] = 0
            self._set_online_last_seen(index, utime.ticks_ms())

    def _note_tx_failure(self, node_id):
        if not self.is_master or node_id == MASTER_NODE_ID:
            return
        index = self._find_online_by_id(node_id)
        if index < 0:
            return
        start = self._online_start(index)
        failures = min(255, self._online_records[
            start + _ONLINE_FAILURES] + 1)
        self._online_records[start + _ONLINE_FAILURES] = failures
        self._set_online_last_seen(index, utime.ticks_ms())
        if failures >= MAX_CONSECUTIVE_FAILURES:
            if self.debug:
                print("DROP", node_id)
            self._remove_online(index)

    def _record_tx_failure(self, node_id):
        if self.is_master:
            self._note_tx_failure(node_id)
        elif node_id == MASTER_NODE_ID and self.node_id is not None:
            self._master_failures = min(255, self._master_failures + 1)
            if self._master_failures >= MAX_CONSECUTIVE_FAILURES:
                self._start_task(self._become_unassigned())

    def _record_tx_success(self, node_id):
        if self.is_master:
            self._note_node_seen(node_id)
        elif node_id == MASTER_NODE_ID:
            self._master_failures = 0

    def _local_node_info(self, node_index):
        if node_index == 0:
            return {"uuid": self.uuid, "id": MASTER_NODE_ID}
        index = node_index - 1
        if not 0 <= index < self._online_count:
            return None
        start = self._online_start(index)
        uuid_bytes = bytes(self._online_records[
            start + _ONLINE_UUID:start + _ONLINE_UUID + UUID_SIZE
        ])
        return {
            "uuid": uuid_bytes.hex(),
            "id": self._online_records[start + _ONLINE_NODE_ID],
        }

    def _find_registry_assignment(self, uuid_bytes):
        wanted = uuid_bytes.hex()
        found = None
        maximum = 0
        try:
            with open("registry.jsonl") as registry_file:
                for line in registry_file:
                    try:
                        record = ujson.loads(line)
                        node_id = int(record.get("id", record.get("node_id")))
                        maximum = max(maximum, node_id)
                        if record["uuid"].lower() == wanted:
                            found = node_id
                    except (ValueError, KeyError, TypeError):
                        if self.debug:
                            print("REG bad")
        except OSError:
            pass
        return found, maximum

    def _get_or_create_assignment(self, uuid_bytes):
        node_id, maximum = self._find_registry_assignment(uuid_bytes)
        if node_id is not None:
            return node_id
        node_id = maximum + 1
        if node_id > MAX_SLAVE_NODE_ID:
            raise RuntimeError("node id space exhausted")
        with open("registry.jsonl", "a") as registry_file:
            registry_file.write(ujson.dumps({
                "uuid": uuid_bytes.hex(), "id": node_id
            }) + "\n")
        return node_id

    def _assignment_matches(self, uuid_bytes, node_id):
        stored, unused_maximum = self._find_registry_assignment(uuid_bytes)
        return stored == node_id

    # ---------- native event loop ----------

    async def process(self):
        try:
            while True:
                handled = False
                for unused in range(MAX_EVENTS_PER_POLL):
                    if not self.core.poll_into(self._event):
                        break
                    handled = True
                    await self._dispatch_event()
                self._run_periodic_tasks()
                await uasyncio.sleep_ms(0 if handled else POLL_INTERVAL_MS)
        finally:
            self.core.stop()

    async def _dispatch_event(self):
        event_type = self._event.type()
        flags = self._event.flags()
        object_id = self._event.object_id()
        source_id = self._event.source()
        transaction_id = self._event.transaction()
        value0 = self._event.value0()
        value1 = self._event.value1()
        data = bytes(self._event.data()) if self._event.data_length() else b""
        self._last_radio_activity = utime.ticks_ms()

        if event_type == _core.EVENT_COMMAND_READY:
            if self.is_master and source_id != MASTER_NODE_ID and \
                    self._find_online_by_id(source_id) < 0:
                return
            if self.is_master:
                self._note_node_seen(source_id)
            if value0 == CMD_REPLY or value0 == MGMT_REPLY:
                self._handle_reply_event(
                    value0, source_id, transaction_id, data
                )
            else:
                self._start_task(self._handle_command_event(
                    value0, source_id, transaction_id, data
                ))
        elif event_type == _core.EVENT_COMMAND_SENT or \
                event_type == _core.EVENT_COMMAND_FAILED:
            key = self._tx_key(value0, source_id, transaction_id)
            if key in self._tx_results:
                self._tx_results[key] = 0 if event_type == \
                    _core.EVENT_COMMAND_SENT else value1
            if event_type == _core.EVENT_COMMAND_FAILED:
                self._record_tx_failure(source_id)
            else:
                self._record_tx_success(source_id)
        elif event_type == _core.EVENT_REGISTRATION_RECEIVED:
            await self._handle_registration(
                object_id, source_id, value0, data
            )
        elif event_type == _core.EVENT_REGISTRATION_FAILED:
            if object_id == ENUM_CONFIRM:
                self._record_tx_failure(source_id)
            if self.debug:
                print("REG!", object_id, value1)
        elif event_type == _core.EVENT_REGISTRATION_SENT:
            if object_id == ENUM_CONFIRM:
                self._record_tx_success(source_id)
        elif event_type == _core.EVENT_PIPE_OPENED:
            if flags & _core.EVENT_FLAG_TX:
                self._set_out_pipe_state(object_id, _PIPE_OPEN)
            else:
                if self.is_master and source_id != MASTER_NODE_ID and \
                        self._find_online_by_id(source_id) < 0:
                    return
                self._incoming_pipe_active[object_id] = 1
                self._queue_pipe_callback(
                    object_id, 1, transaction_id, source_id, b""
                )
        elif event_type == _core.EVENT_PIPE_RX_DATA:
            self._queue_pipe_callback(
                object_id, 2, transaction_id, source_id, data
            )
        elif event_type == _core.EVENT_PIPE_CLOSED:
            if flags & _core.EVENT_FLAG_TX:
                self._set_out_pipe_state(object_id, _PIPE_CLOSED)
            else:
                self._incoming_pipe_active[object_id] = 0
                self._queue_pipe_callback(
                    object_id, 3, transaction_id, source_id, b""
                )
        elif event_type == _core.EVENT_PIPE_FAILED:
            if flags & _core.EVENT_FLAG_TX:
                self._pipe_failures[object_id] = value0
                self._set_out_pipe_state(object_id, _PIPE_FAILED)
                self._note_tx_failure(source_id)
            else:
                self._incoming_pipe_active[object_id] = 0
                self._queue_pipe_callback(
                    object_id, 3, transaction_id, source_id, b""
                )
        elif event_type == _core.EVENT_CORE_ERROR and self.debug:
            print("CORE!", value0, value1)

    def _start_task(self, coroutine):
        uasyncio.create_task(self._guard_task(coroutine))

    async def _guard_task(self, coroutine):
        try:
            await coroutine
        except Exception as error:
            if self.debug:
                print("TASK!", error)

    def _queue_pipe_callback(self, slot, kind, pipe_id, source_id, data):
        queue = self._pipe_callback_queues[slot]
        queue.append((kind, pipe_id, source_id, data))
        if not self._pipe_callback_running[slot]:
            self._pipe_callback_running[slot] = 1
            self._start_task(self._run_pipe_callbacks(slot))

    async def _run_pipe_callbacks(self, slot):
        queue = self._pipe_callback_queues[slot]
        try:
            while queue:
                kind, pipe_id, source_id, data = queue.pop(0)
                if kind == 1:
                    await self.on_pipe_opened(pipe_id, source_id)
                elif kind == 2:
                    await self.on_pipe_data(pipe_id, source_id, data)
                else:
                    await self.on_pipe_closed(pipe_id, source_id)
        finally:
            self._pipe_callback_running[slot] = 0

    # ---------- registration policy ----------

    def _queue_registration(self, destination_id, message_type,
                            address, payload):
        queued = self.core.send_registration(
            destination_id, message_type, 0, address, payload
        )
        if queued:
            self._last_radio_activity = utime.ticks_ms()
        return queued

    async def _handle_registration(self, message_type, source_id,
                                   destination_id, payload):
        if message_type == ENUM_HELLO:
            if not self.is_master or destination_id != MASTER_NODE_ID or \
                    len(payload) != UUID_SIZE:
                return
            try:
                node_id = self._get_or_create_assignment(payload)
            except (OSError, RuntimeError) as error:
                if self.debug:
                    print("ASSIGN!", error)
                return
            assignment = payload + bytes((node_id,))
            self._queue_registration(
                UNASSIGNED_NODE_ID, ENUM_ASSIGN,
                self._temporary_address(payload), assignment
            )
            return

        if message_type == ENUM_ASSIGN:
            if self.is_master or self.node_id is not None or \
                    destination_id != UNASSIGNED_NODE_ID or \
                    len(payload) != UUID_SIZE + 1 or \
                    payload[:UUID_SIZE] != self._uuid_bytes:
                return
            node_id = payload[UUID_SIZE]
            if not 1 <= node_id <= MAX_SLAVE_NODE_ID:
                return
            self.core.set_node_id(node_id)
            self.node_id = node_id
            self._master_acknowledged = True
            self._master_failures = 0
            self._send_enum_confirm()
            self._last_master_confirm = utime.ticks_ms()
            self._confirm_delay_ms = self._next_service_delay(
                SLAVE_CONFIRM_INTERVAL_MS, SLAVE_CONFIRM_JITTER_MS
            )
            return

        if message_type == ENUM_CONFIRM:
            if not self.is_master or destination_id != MASTER_NODE_ID or \
                    len(payload) != UUID_SIZE + 1:
                return
            uuid_bytes = payload[:UUID_SIZE]
            node_id = payload[UUID_SIZE]
            if source_id != node_id or not 1 <= node_id <= MAX_SLAVE_NODE_ID:
                return
            if self._assignment_matches(uuid_bytes, node_id):
                if not self._mark_online(node_id, uuid_bytes) and self.debug:
                    print("ONLINE full")

    def _send_enum_hello(self):
        return self._queue_registration(
            MASTER_NODE_ID, ENUM_HELLO,
            self._registration_address(), self._uuid_bytes
        )

    def _send_enum_confirm(self):
        if self.node_id is None:
            return False
        payload = self._uuid_bytes + bytes((self.node_id,))
        return self._queue_registration(
            MASTER_NODE_ID, ENUM_CONFIRM,
            self._endpoint_address(MASTER_NODE_ID), payload
        )

    async def _become_unassigned(self):
        if self.is_master or self.node_id is None:
            return
        self.core.set_node_id(UNASSIGNED_NODE_ID)
        self.node_id = None
        self._master_acknowledged = False
        self._master_failures = 0
        self._last_enum_hello = utime.ticks_ms()
        self._hello_delay_ms = self._next_hello_delay(initial=True)

    # ---------- commands and management ----------

    def _tx_key(self, message_type, destination_id, message_id):
        return (message_type << 16) | (destination_id << 8) | message_id

    def _next_msg_id(self):
        for unused in range(255):
            self._last_msg_id = (self._last_msg_id % 255) + 1
            if self._last_msg_id not in self._awaiting_replies:
                return self._last_msg_id
        raise RuntimeError("no message ids available")

    async def _send_native_command(self, destination_id, message_type,
                                   message_id, payload, timeout_ms):
        key = self._tx_key(message_type, destination_id, message_id)
        started = utime.ticks_ms()
        await self._send_lock.acquire()
        try:
            while True:
                self._tx_results[key] = _TX_WAITING
                if self.core.send_command(
                        destination_id, message_type, message_id, payload):
                    break
                self._tx_results.pop(key, None)
                if utime.ticks_diff(utime.ticks_ms(), started) >= timeout_ms:
                    raise RuntimeError("transport busy")
                await uasyncio.sleep_ms(POLL_INTERVAL_MS)
            while self._tx_results[key] is _TX_WAITING:
                if utime.ticks_diff(utime.ticks_ms(), started) >= timeout_ms:
                    self._record_tx_failure(destination_id)
                    raise RuntimeError("send timed out")
                await uasyncio.sleep_ms(POLL_INTERVAL_MS)
            failure = self._tx_results[key]
            if failure:
                raise RuntimeError("send failed {}".format(failure))
        finally:
            self._tx_results.pop(key, None)
            self._send_lock.release()

    async def _request(self, destination_id, request_type, reply_type,
                       value, timeout_ms=REQUEST_TIMEOUT_MS):
        if len(self._awaiting_replies) >= MAX_PENDING_REQUESTS:
            raise RuntimeError("too many pending requests")
        message_id = self._next_msg_id()
        pending = [destination_id, reply_type, _NO_REPLY]
        self._awaiting_replies[message_id] = pending
        started = utime.ticks_ms()
        try:
            payload = ujson.dumps(value).encode()
            if len(payload) > MAX_COMMAND_SIZE:
                raise ValueError("encoded command too large")
            await self._send_native_command(
                destination_id, request_type, message_id, payload, timeout_ms
            )
            while pending[2] is _NO_REPLY:
                if utime.ticks_diff(utime.ticks_ms(), started) >= timeout_ms:
                    self._record_tx_failure(destination_id)
                    raise RuntimeError("request timed out")
                await uasyncio.sleep_ms(POLL_INTERVAL_MS)
            self._note_node_seen(destination_id)
            return pending[2]
        finally:
            self._awaiting_replies.pop(message_id, None)

    async def _handle_command_event(self, message_type, source_id,
                                    message_id, payload):
        try:
            value = ujson.loads(payload)
        except (ValueError, TypeError):
            if self.debug:
                print("JSON bad")
            return
        if message_type == CMD:
            result = await self.on_command(source_id, value)
            reply_type = CMD_REPLY
        elif message_type == MGMT_REQUEST:
            result = self._management_result(value)
            reply_type = MGMT_REPLY
        else:
            return
        encoded = ujson.dumps(result).encode()
        if len(encoded) > MAX_COMMAND_SIZE:
            encoded = b'{"err":"large"}'
        await self._send_native_command(
            source_id, reply_type, message_id, encoded, REQUEST_TIMEOUT_MS
        )

    def _handle_reply_event(self, message_type, source_id,
                            message_id, payload):
        try:
            value = ujson.loads(payload)
        except (ValueError, TypeError):
            if self.debug:
                print("JSON bad")
            return
        pending = self._awaiting_replies.get(message_id)
        if pending is not None and pending[0] == source_id and \
                pending[1] == message_type:
            pending[2] = value

    def _management_result(self, request):
        operation = request.get("op") if isinstance(request, dict) else None
        if operation == _OP_PING:
            return {"ok": True}
        if self.is_master and operation == _OP_GET_QTY:
            return {"qty": 1 + self._online_count}
        if self.is_master and operation == _OP_GET_INFO:
            return {"node": self._local_node_info(request.get("i", -1))}
        return {"err": "op" if self.is_master else "master"}

    async def get_nodes_qty(self):
        if self.is_master:
            return 1 + self._online_count
        reply = await self._request(
            MASTER_NODE_ID, MGMT_REQUEST, MGMT_REPLY, {"op": _OP_GET_QTY}
        )
        return reply.get("qty", 0)

    async def get_node_info(self, node_index):
        if self.is_master:
            return self._local_node_info(node_index)
        reply = await self._request(
            MASTER_NODE_ID, MGMT_REQUEST, MGMT_REPLY,
            {"op": _OP_GET_INFO, "i": node_index}
        )
        return reply.get("node")

    async def send_command(self, node_id, command,
                           timeout_ms=REQUEST_TIMEOUT_MS):
        return await self.send_command_and_wait_reply(
            node_id, command, timeout_ms
        )

    async def send_command_and_wait_reply(self, node_id, command,
                                          timeout_ms=REQUEST_TIMEOUT_MS):
        if node_id == self.node_id:
            return await self.on_command(self.node_id, command)
        return await self._request(
            node_id, CMD, CMD_REPLY, command, timeout_ms=timeout_ms
        )

    async def on_command(self, src_id, command):
        return None

    # ---------- streaming ----------

    def _out_pipe_start(self, slot):
        return slot * _PIPE_RECORD_SIZE

    def _set_out_pipe_state(self, slot, state):
        self._outgoing_pipes[
            self._out_pipe_start(slot) + _PIPE_STATE] = state

    def _find_out_pipe(self, pipe_id):
        for slot in range(MAX_OPEN_STREAMS):
            start = self._out_pipe_start(slot)
            if self._outgoing_pipes[start + _PIPE_ACTIVE] and \
                    self._outgoing_pipes[start + _PIPE_STREAM_ID] == pipe_id:
                return slot
        return -1

    def _next_stream(self):
        for unused in range(255):
            self._last_stream_id = (self._last_stream_id % 255) + 1
            if self._find_out_pipe(self._last_stream_id) < 0:
                return self._last_stream_id
        raise RuntimeError("no stream ids available")

    async def open_pipe(self, node_id):
        stream_id = self._next_stream()
        slot = self.core.open_pipe(node_id, stream_id)
        if slot is None:
            raise RuntimeError("transport busy or no pipe slot")
        start = self._out_pipe_start(slot)
        self._outgoing_pipes[start + _PIPE_ACTIVE] = 1
        self._outgoing_pipes[start + _PIPE_NODE_ID] = node_id
        self._outgoing_pipes[start + _PIPE_STREAM_ID] = stream_id
        self._outgoing_pipes[start + _PIPE_STATE] = _PIPE_OPENING
        self._pipe_failures[slot] = 0
        started = utime.ticks_ms()
        while self._outgoing_pipes[start + _PIPE_STATE] == _PIPE_OPENING:
            if utime.ticks_diff(utime.ticks_ms(), started) >= \
                    PIPE_OPERATION_TIMEOUT_MS:
                raise RuntimeError("pipe open timed out")
            await uasyncio.sleep_ms(POLL_INTERVAL_MS)
        if self._outgoing_pipes[start + _PIPE_STATE] == _PIPE_FAILED:
            reason = self._pipe_failures[slot]
            self._outgoing_pipes[start + _PIPE_ACTIVE] = 0
            raise RuntimeError("pipe open failed {}".format(reason))
        return stream_id

    async def send_pipe(self, pipe_id, data, close=False):
        slot = self._find_out_pipe(pipe_id)
        if slot < 0:
            raise ValueError("unknown outgoing pipe")
        start = self._out_pipe_start(slot)
        state = self._outgoing_pipes[start + _PIPE_STATE]
        if state == _PIPE_FAILED:
            raise RuntimeError("pipe failed {}".format(
                self._pipe_failures[slot]))
        if state != _PIPE_OPEN:
            raise RuntimeError("pipe not open")
        data = bytes(data)
        offset = 0
        started = utime.ticks_ms()
        while offset < len(data):
            written = self.core.pipe_write(slot, memoryview(data)[offset:])
            offset += written
            if written:
                started = utime.ticks_ms()
                continue
            if self._outgoing_pipes[start + _PIPE_STATE] == _PIPE_FAILED:
                raise RuntimeError("pipe failed {}".format(
                    self._pipe_failures[slot]))
            if utime.ticks_diff(utime.ticks_ms(), started) >= \
                    PIPE_OPERATION_TIMEOUT_MS:
                raise RuntimeError("pipe write timed out")
            await uasyncio.sleep_ms(POLL_INTERVAL_MS)
        if not close:
            return
        self._outgoing_pipes[start + _PIPE_STATE] = _PIPE_CLOSING
        self.core.close_pipe(slot)
        started = utime.ticks_ms()
        while True:
            state = self._outgoing_pipes[start + _PIPE_STATE]
            if state == _PIPE_CLOSED:
                self._outgoing_pipes[start + _PIPE_ACTIVE] = 0
                return
            if state == _PIPE_FAILED:
                reason = self._pipe_failures[slot]
                self._outgoing_pipes[start + _PIPE_ACTIVE] = 0
                raise RuntimeError("pipe close failed {}".format(reason))
            if utime.ticks_diff(utime.ticks_ms(), started) >= \
                    PIPE_OPERATION_TIMEOUT_MS:
                raise RuntimeError("pipe close timed out")
            await uasyncio.sleep_ms(POLL_INTERVAL_MS)

    async def on_pipe_opened(self, pipe_id, src_id):
        pass

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        pass

    async def on_pipe_closed(self, pipe_id, src_id):
        pass

    # ---------- periodic policy ----------

    def _transport_busy(self):
        if self._awaiting_replies or self._tx_results:
            return True
        for slot in range(MAX_OPEN_STREAMS):
            if self._incoming_pipe_active[slot] or self._outgoing_pipes[
                    self._out_pipe_start(slot) + _PIPE_ACTIVE]:
                return True
        return False

    def _background_service_allowed(self, now):
        return not self._transport_busy() and \
            utime.ticks_diff(now, self._last_radio_activity) >= \
            BACKGROUND_QUIET_MS

    def _run_periodic_tasks(self):
        now = utime.ticks_ms()
        if not self.is_master and self.node_id is None:
            if utime.ticks_diff(now, self._last_enum_hello) >= \
                    self._hello_delay_ms and self._send_enum_hello():
                self._last_enum_hello = now
                self._hello_delay_ms = self._next_hello_delay()
            return
        if not self.is_master:
            if utime.ticks_diff(now, self._last_master_confirm) >= \
                    self._confirm_delay_ms and \
                    self._background_service_allowed(now) and \
                    self._send_enum_confirm():
                self._last_master_confirm = now
                self._confirm_delay_ms = self._next_service_delay(
                    SLAVE_CONFIRM_INTERVAL_MS, SLAVE_CONFIRM_JITTER_MS
                )
            return
        if self._background_task_active or \
                not self._background_service_allowed(now):
            return
        for index in range(self._online_count):
            if utime.ticks_diff(now, self._online_last_seen(index)) < \
                    self._health_probe_delay(index):
                continue
            node_id = self._online_records[
                self._online_start(index) + _ONLINE_NODE_ID]
            self._background_task_active = True
            self._start_task(self._probe_node(node_id))
            return

    async def _probe_node(self, node_id):
        try:
            await self._request(
                node_id, MGMT_REQUEST, MGMT_REPLY, {"op": _OP_PING}
            )
        except Exception:
            pass
        finally:
            self._background_task_active = False

    def _health_probe_delay(self, index):
        start = self._online_start(index)
        value = 0x5A
        for offset in range(UUID_SIZE):
            value = ((value * 33) ^ self._online_records[
                start + _ONLINE_UUID + offset]) & 0x7FFFFFFF
        return HEALTH_PROBE_INTERVAL_MS + \
            value % (HEALTH_PROBE_JITTER_MS + 1)

    def _next_service_delay(self, base_ms, jitter_ms):
        seed = getattr(self, "_service_seed", 0)
        if seed == 0:
            seed = 0x13579
            for value in self._uuid_bytes:
                seed = ((seed * 33) ^ value) & 0x7FFFFFFF
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        self._service_seed = seed
        return base_ms + seed % (jitter_ms + 1)

    def _next_hello_delay(self, initial=False):
        seed = getattr(self, "_hello_seed", 0)
        if seed == 0:
            seed = 1
            for value in self._uuid_bytes:
                seed = ((seed * 33) ^ value) & 0x7FFFFFFF
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        self._hello_seed = seed
        jitter = seed % (HELLO_JITTER_MS + 1)
        return jitter if initial else HELLO_INTERVAL_MS + jitter
