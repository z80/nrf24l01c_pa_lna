from micropython import const
import ujson
import utime
import uasyncio

from nrf24l01 import NRF24L01, RX_DR, TX_DS, MAX_RT
from radio import get_nrf

# Event types
EVENT_RX       = const(1)
EVENT_TX_DONE  = const(2)
EVENT_TX_FAIL  = const(3)
EVENT_TICK     = const(4)   # optional periodic tick

# Message types
CMD           = const(1)
STREAM        = const(2)
ENUM_HELLO    = const(3)
ENUM_ASSIGN   = const(4)
MASTER_HELLO  = const(5)
MASTER_CHANGE = const(6)

# Header layout:
# Byte 0: msg_type
# Byte 1: src_id
# Byte 2: dst_id
# Byte 3: msg_id
# Byte 4: seq_hi
# Byte 5: seq_lo
# Byte 6: flags (bit0 = LAST_PACKET)
# Byte 7+: payload (<= 25 bytes for 32-byte radio payload)

HELLO_INTERVAL_MS = 5000
ADDRESS = b"ABCDE"

class TransportNode:
    def __init__(self, role="slave", debug=True):
        self.role = role  # "master_candidate" or "slave" or "master"
        self.debug = debug

        # Event ring buffer
        self._event_buf_len = 32
        self._event_buf = [0] * self._event_buf_len
        self._event_head = 0
        self._event_tail = 0

        # Radio instance: use your factory
        # payload_size=32 so header+payload fits nicely
        self.radio = get_nrf(payload_size=32)
        # Wire IRQ handler from driver to our handler
        if self.radio.irq is not None:
            # nrf24l01 driver calls irq_handler(status)
            self.radio.irq_handler = self._irq_status_handler

        # Runtime state
        self._pending_cmds = {}   # (src_id, msg_id) -> [chunks]
        self._streams = {}        # (src_id, stream_id) -> stream state
        self._awaiting_replies = {}   # msg_id -> Future
        
        # Need to inform master that it's shown up.
        # Master should assign it a node_id and acknowledge.
        self._master_acknowledged = False
        # Last time hello has been sent
        self._last_enum_hello = 0

        # Identity and registry
        self._load_identity()
        if self._is_master():
            self._load_registry()
            self.node_id = 0

        else:
            self.node_id = None

        # Start listening on some default RX pipe/address
        # (you'll want to set a real address here)
        self.radio.open_rx_pipe(0, ADDRESS)
        self.radio.open_tx_pipe(ADDRESS)
        self.radio.start_listening()

    # ---------- ring buffer ----------

    def _push_event(self, event_type):
        nxt = (self._event_head + 1) % self._event_buf_len
        if nxt == self._event_tail:
            # buffer full; you might want to count drops
            return
        self._event_buf[self._event_head] = event_type
        self._event_head = nxt

    def _pop_event(self):
        if self._event_tail == self._event_head:
            return None
        event = self._event_buf[self._event_tail]
        self._event_tail = (self._event_tail + 1) % self._event_buf_len
        return event

    # ---------- IRQ integration ----------

    def _irq_status_handler(self, status):
        # Called by nrf24l01._irq_wrapper(status)
        if status & RX_DR:
            self._push_event(EVENT_RX)
        if status & TX_DS:
            self._push_event(EVENT_TX_DONE)
        if status & MAX_RT:
            self._push_event(EVENT_TX_FAIL)

    # ---------- identity / registry ----------

    def _load_identity(self):
        try:
            with open('identity.json') as f:
                data = ujson.loads(f.read())
            self.uuid = data["uuid"]
        except OSError:
            import os
            raw = os.urandom(16)
            self.uuid = raw.hex()
            self._save_identity()

    def _save_identity(self):
        data = {
            "uuid": self.uuid,
        }
        with open('identity.json', 'w') as f:
            f.write(ujson.dumps(data))

    def _load_registry(self):
        self.registry = {}
        try:
            with open('registry.jsonl') as f:
                for line in f:
                    rec = ujson.loads(line)
                    self.registry[rec["uuid"]] = rec
        except OSError:
            self.registry = {}

    def _append_registry_record(self, rec):
        with open('registry.jsonl', 'a') as f:
            f.write(ujson.dumps(rec) + "\n")
        self.registry[rec["uuid"]] = rec

    def _is_master(self):
        return self.role == "master" or self.role == "master_candidate"

    def promote_to_master(self):
        self.role = "master"
        self._save_identity()
        self._load_registry()

    def demote_to_slave(self):
        self.role = "slave"
        self._save_identity()

    # ---------- main async loop ----------

    async def process(self):
        while True:
            # Handle pending events
            while True:
                event = self._pop_event()
                if event is None:
                    break

                if self.debug:
                    print("[process] Event:", 
                          "RX" if event == EVENT_RX else
                          "TX_DONE" if event == EVENT_TX_DONE else
                          "TX_FAIL" if event == EVENT_TX_FAIL else
                          event)

                if event == EVENT_RX:
                    pkt = self.radio.recv()
                    if self.debug:
                        print("[RX] Raw packet:", pkt)
                    await self._handle_rx_packet(pkt)

                elif event == EVENT_TX_DONE:
                    if self.debug:
                        print("[TX] TX_DONE")
                    await self._handle_tx_done()

                elif event == EVENT_TX_FAIL:
                    if self.debug:
                        print("[TX] TX_FAIL")
                    await self._handle_tx_fail()

            # Periodic tasks
            if self.debug:
                print("[process] Running periodic tasks")
            await self._run_periodic_tasks()

            # Yield to scheduler
            # debug slow-motion mode
            await uasyncio.sleep_ms(500 if self.debug else 0)

    # ---------- RX packet parsing ----------

    async def _handle_rx_packet(self, pkt):
        if len(pkt) < 7:
            return

        msg_type = pkt[0]
        src_id   = pkt[1]
        dst_id   = pkt[2]
        msg_id   = pkt[3]
        seq      = (pkt[4] << 8) | pkt[5]
        flags    = pkt[6]
        payload  = pkt[7:]

        if self.debug:
            print("[RX] type:", msg_type,
                  "src:", src_id,
                  "dst:", dst_id,
                  "msg_id:", msg_id,
                  "seq:", seq,
                  "flags:", flags,
                  "payload:", payload)
        
        # Filter out garbage packets.
        if msg_type not in (ENUM_HELLO, ENUM_ASSIGN, MASTER_HELLO, CMD, STREAM):
            return
        if msg_type in (CMD, STREAM) and src_id == 0:
            # unassigned nodes cannot send multi-packet commands
            return
        if msg_id == 0 and seq != 0:
            return
        if flags & ~0x01:
            return
        if len(payload) != 25:
            return

        if msg_type == ENUM_HELLO:
            await self._handle_enum_hello(payload)
        elif msg_type == ENUM_ASSIGN:
            await self._handle_enum_assign(payload)
        elif msg_type == MASTER_HELLO:
            await self._handle_master_hello(payload)
        elif msg_type == CMD:
            await self._handle_cmd_chunk(src_id, msg_id, seq, flags, payload)
        elif msg_type == STREAM:
            await self._handle_stream_chunk(src_id, msg_id, seq, flags, payload)

    # ---------- commands ----------

    async def _handle_cmd_chunk(self, src_id, msg_id, seq, flags, payload):
        if self.debug:
            print("[CMD] Chunk from src", src_id,
                  "msg_id", msg_id,
                  "seq", seq,
                  "flags", flags,
                  "payload", payload)

        key = (src_id, msg_id)
        buf = self._pending_cmds.get(key)
        if buf is None:
            if self.debug:
                print("[CMD] New command buffer for", key)
            buf = []
            self._pending_cmds[key] = buf

        buf.append(payload)

        if flags & 0x01:  # LAST_PACKET
            full = b"".join(buf)
            del self._pending_cmds[key]

            if self.debug:
                print("[CMD] Full command assembled:", full)

            try:
                cmd = ujson.loads(full)
            except ValueError:
                if self.debug:
                    print("[CMD] JSON decode failed")
                return

            if self.debug:
                print("[CMD] Dispatching command:", cmd)

            await self.on_command(src_id, cmd)

    async def _on_command(self, src_id, cmd ):
        if not self._is_master():
            self.on_command( src_id, cmd )
            return

        if cmd.get("cmd") == "get_nodes_qty":
            reply = {"nodes_qty": len(self.registry)}
            await self._send_cmd_reply(src_id, reply)

        elif cmd.get("cmd") == "get_node_info":
            idx = cmd.get("index", -1)
            uuids = list(self.registry.keys())
            if 0 <= idx < len(uuids):
                rec = self.registry[uuids[idx]]
                await self._send_cmd_reply(src_id, rec)
            else:
                await self._send_cmd_reply(src_id, {"error": "index_out_of_range"})

        else:
            self.on_command( src_id, cmd )

    async def on_command(self, src_id, cmd):
        # default: do nothing
        pass

    # ---------- streams ----------

    async def _handle_stream_chunk(self, src_id, stream_id, seq, flags, payload):
        if self.debug:
            print("[STREAM] Chunk src", src_id,
                  "stream", stream_id,
                  "seq", seq,
                  "flags", flags,
                  "payload", payload)

        key = (src_id, stream_id)
        stream = self._streams.get(key)
        if stream is None:
            if self.debug:
                print("[STREAM] Opening new stream", key)
            stream = {"open": True}
            self._streams[key] = stream
            await self.on_pipe_opened(stream_id, src_id)

        await self.on_pipe_data(stream_id, src_id, payload)

        if flags & 0x01:
            if self.debug:
                print("[STREAM] Closing stream", key)
            stream["open"] = False
            await self.on_pipe_closed(stream_id, src_id)
            del self._streams[key]
        

    async def on_pipe_opened(self, pipe_id, src_id):
        pass

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        pass

    async def on_pipe_closed(self, pipe_id, src_id):
        pass

    # ---------- periodic tasks ----------

    async def _run_periodic_tasks(self):
        if self.debug:
            print("[periodic] role:", self.role,
                  "node_id:", self.node_id)

        now = utime.ticks_ms()

        if self._is_master():
            if self.debug:
                print("[periodic] Master periodic tasks")
            await self._master_periodic()

        else:
            if not self._master_acknowledged:
                if utime.ticks_diff(now, self._last_enum_hello) > HELLO_INTERVAL_MS:
                    if self.debug:
                        print("[periodic] sending ENUM_HELLO")
                    self._send_enum_hello()
                    self._last_enum_hello = now


    def _safe_send(self, buf):
        # Leave RX mode
        self.radio.stop_listening()
        try:
            # Blocking send: waits for TX_DS or MAX_RT
            self.radio.send(buf)
        finally:
            # Always return to RX mode
            self.radio.start_listening()


    def _send_enum_hello(self):
        uuid_bytes = bytes.fromhex(self.uuid)
        hdr = bytes([ENUM_HELLO, 0, 0, 0, 0, 0, 0])
        pkt = hdr + uuid_bytes

        try:
            self._safe_send(pkt)
        except Exception as e:
            if self.debug:
                print("[ENUM_HELLO] send failed:", e)

    async def _handle_enum_hello(self, payload):
        if not self._is_master():
            return

        uuid_bytes = payload[:16]
        uuid = uuid_bytes.hex()

        rec = self.registry.get(uuid)
        if rec is None:
            new_id = self._allocate_node_id()
            rec = {"uuid": uuid, "node_id": new_id, "status": "online"}
            self._append_registry_record(rec)
        else:
            rec["status"] = "online"

        assign = {
            "uuid": uuid,
            "node_id": rec["node_id"],
        }
        payload_out = ujson.dumps(assign).encode()
        hdr = bytes([ENUM_ASSIGN, 0, 0, 0, 0, 0, 0])
        self._safe_send(hdr + payload_out)

    async def _handle_enum_assign(self, payload):
        try:
            data = ujson.loads(payload)
        except ValueError:
            return
        self.node_id = data["node_id"]
        self._save_identity()
        self._master_acknowledged = True
        # here you’d reconfigure RX pipe to self.rx_addr

    async def _master_periodic(self):
        # placeholder for MASTER_HELLO, timeouts, election, etc.
        pass

    def _allocate_node_id(self):
        # simple example: max existing + 1
        if not self.registry:
            return 1
        return max(rec["node_id"] for rec in self.registry.values()) + 1

    # ---------- TX events ----------

    async def _handle_tx_done(self):
        # resolve any pending send futures if you add them
        pass

    async def _handle_tx_fail(self):
        # handle retries / failures
        pass

    # ---------- public API stubs ----------

    async def get_nodes_qty(self):
        if self._is_master():
            return len(self.registry)

        # Ask master
        req = {"cmd": "get_nodes_qty"}
        reply = await self._send_cmd_to_master(req)
        return reply.get("nodes_qty", 0)

    async def get_node_info(self, node_index):
        if self._is_master():
            # direct lookup
            uuids = list(self.registry.keys())
            if node_index < 0 or node_index >= len(uuids):
                return None
            return self.registry[uuids[node_index]]

        req = {"cmd": "get_node_info", "index": node_index}
        reply = await self._send_cmd_to_master(req)
        return reply

    async def send_command(self, node_index, command_array):
        # Step 2: send actual command to target node
        payload = ujson.dumps(command_array).encode()
        await self._send_cmd_to_node(node_index, payload)
        return True    


    async def send_command_and_wait_reply(self, node_id, command_array, timeout_ms=2000):
        payload = ujson.dumps(command_array).encode()
        msg_id = self._next_msg_id()

        fut = uasyncio.Future()
        self._awaiting_replies[msg_id] = fut

        await self._send_packet_sequence(dst_id=node_id,
                                         msg_type=CMD,
                                         msg_id=msg_id,
                                         payload=payload)

        # Manual timeout
        start = utime.ticks_ms()
        while not fut.done():
            await uasyncio.sleep_ms(10)
            if utime.ticks_diff(utime.ticks_ms(), start) > timeout_ms:
                del self._awaiting_replies[msg_id]
                self._pending_cmds.pop((self.node_id, msg_id), None)
                raise TimeoutError("Node {} did not reply".format(node_id))

        return fut.result()

    async def open_pipe(self, node_index):
        # send CMD "open_stream", get stream_id
        pass

    async def send_pipe(self, pipe_id, data_chunk_array):
        # send STREAM packets
        pass



    async def _send_cmd_to_master(self, obj):
        payload = ujson.dumps(obj).encode()
        msg_id = self._next_msg_id()

        # master always has node_id = 0
        await self._send_packet_sequence(dst_id=0, msg_type=CMD, msg_id=msg_id, payload=payload)

        # wait for reply
        return await self._wait_for_cmd_reply(msg_id)


    async def _send_cmd_reply(self, dst_id, obj):
        payload = ujson.dumps(obj).encode()
        msg_id = self._next_msg_id()
        await self._send_packet_sequence(dst_id=dst_id, msg_type=CMD, msg_id=msg_id, payload=payload)


    async def _send_cmd_to_node(self, node_id, payload):
        msg_id = self._next_msg_id()

        await self._send_packet_sequence(dst_id=node_id, msg_type=CMD, msg_id=msg_id, payload=payload)


    async def _send_packet_sequence(self, dst_id, msg_type, msg_id, payload):
        CHUNK = 25
        seq = 0
        
        self.radio.stop_listening()
        try:
            for i in range(0, len(payload), CHUNK):
                chunk = payload[i:i+CHUNK]
                last = 1 if (i + CHUNK >= len(payload)) else 0

                hdr = bytes([
                    msg_type,
                    self.node_id or 0,
                    dst_id,
                    msg_id,
                    (seq >> 8) & 0xFF,
                    seq & 0xFF,
                    last
                ])

                self.radio.send(hdr + chunk)
                seq += 1

                await uasyncio.sleep_ms(2)
        finally:
            self.radio.start_listening()

    async def _wait_for_cmd_reply(self, msg_id):
        # your existing command reassembly already stores replies
        # so you just wait until on_command() receives a matching msg_id
        while True:
            await uasyncio.sleep_ms(10)
            if ("reply", msg_id) in self._pending_cmds:
                full = b"".join(self._pending_cmds[("reply", msg_id)])
                del self._pending_cmds[("reply", msg_id)]
                return ujson.loads(full)

