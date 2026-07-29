"""NRF24L01 driver for MicroPython"""

from micropython import const
import utime

# nRF24L01+ registers
CONFIG = const(0x00)
EN_RXADDR = const(0x02)
SETUP_AW = const(0x03)
SETUP_RETR = const(0x04)
RF_CH = const(0x05)
RF_SETUP = const(0x06)
STATUS = const(0x07)
OBSERVE_TX = const(0x08)
RX_ADDR_P0 = const(0x0A)
TX_ADDR = const(0x10)
RX_PW_P0 = const(0x11)
FIFO_STATUS = const(0x17)
DYNPD = const(0x1C)

# CONFIG register
EN_CRC = const(0x08)
CRCO = const(0x04)
PWR_UP = const(0x02)
PRIM_RX = const(0x01)

# RF_SETUP register
POWER_0 = const(0x00)
POWER_1 = const(0x02)
POWER_2 = const(0x04)
POWER_3 = const(0x06)
SPEED_1M = const(0x00)
SPEED_2M = const(0x08)
SPEED_250K = const(0x20)

CONT_WAVE = const(0x80)
PLL_LOCK  = const(0x40)

# STATUS register
RX_DR = const(0x40)
TX_DS = const(0x20)
MAX_RT = const(0x10)

# FIFO_STATUS register
RX_EMPTY = const(0x01)

# commands
R_RX_PL_WID = const(0x60)
R_RX_PAYLOAD = const(0x61)
W_TX_PAYLOAD = const(0xA0)
FLUSH_TX = const(0xE1)
FLUSH_RX = const(0xE2)
NOP = const(0xFF)

EN_AA     = const(0x01)
FEATURE   = const(0x1D)
EN_DPL    = const(0x04)   # FEATURE bit
EN_ACK_PAY= const(0x02)
EN_DYN_ACK= const(0x01)



class NRF24L01:

    # ------------------------------------------------------------
    # Static helpers for optional external initialization
    # ------------------------------------------------------------
    @staticmethod
    def init_spi_bus(spi, baudrate=4000000):
        """Initialize SPI bus in nRF24L01-compatible mode."""
        try:
            master = spi.MASTER
            spi.init(master, baudrate=baudrate, polarity=0, phase=0)
        except AttributeError:
            spi.init(baudrate=baudrate, polarity=0, phase=0)

    @staticmethod
    def init_radio_pins(cs, ce):
        """Initialize CS and CE pins."""
        cs.init(cs.OUT, value=1)
        ce.init(ce.OUT, value=0)

    @staticmethod
    def init_irq_pin(irq_pin):
        """Initialize IRQ pin as input with pull-up."""
        irq_pin.init(irq_pin.IN, pull=irq_pin.PULL_UP)

    # ------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------
    def __init__(self, spi, cs, ce, channel=46, payload_size=16,
                 init_spi=True, init_pins=True,
                 irq=None, irq_handler=None, 
                 speed=SPEED_2M,          # <-- 2 Mbps default
                 power=POWER_3,
                 ard_us=750,              # <-- short auto-retransmit delay
                 arc=15):

        assert payload_size <= 32

        self.spi = spi
        self.cs = cs
        self.ce = ce
        self.irq = irq
        self.irq_handler = irq_handler
        self.buf = bytearray(1)
        self.payload_size = payload_size
        self.pipe0_read_addr = None
        self._powered = False             # track power state

        if init_spi:
            NRF24L01.init_spi_bus(spi, baudrate=8_000_000)  # max reliable SPI
        if init_pins:
            NRF24L01.init_radio_pins(cs, ce)
        if irq is not None:
            NRF24L01.init_irq_pin(irq)
            if irq_handler is not None:
                irq.irq(trigger=irq.IRQ_FALLING, handler=self._irq_wrapper)

        utime.sleep_ms(5)

        self.reg_write(SETUP_AW, 0b11)
        if self.reg_read(SETUP_AW) != 0b11:
            raise OSError("nRF24L01+ Hardware not responding")

        # Dynamic payloads still off by default (your transport uses fixed 32)
        self.reg_write(DYNPD, 0)

        # Auto-retransmit: ARD in 250 µs steps (0 = 250 µs)
        ard_steps = max(0, min(15, (ard_us // 250) - 1))
        self.reg_write(SETUP_RETR, (ard_steps << 4) | (arc & 0x0F))

        self.set_power_speed(power, speed)
        self.set_crc(2)
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)
        self.set_channel(channel)
        self.flush_rx()
        self.flush_tx()

        # Leave radio powered in Standby-I after init
        self.power_up()

    # ------------------------------------------------------------
    # Power management – the key latency win
    # ------------------------------------------------------------
    def power_up(self):
        """Bring radio to Standby-I. 1.5 ms only if it was fully off."""
        if self._powered:
            return
        config = self.reg_read(CONFIG)
        self.reg_write(CONFIG, config | PWR_UP)
        utime.sleep_us(1500)          # only once
        self._powered = True

    def power_down(self):
        """Full power-down (use only for long idle periods)."""
        self.ce(0)
        config = self.reg_read(CONFIG)
        self.reg_write(CONFIG, config & ~PWR_UP)
        self._powered = False

    def is_powered(self):
        return self._powered

    # ------------------------------------------------------------
    # IRQ wrapper (safe ISR)
    # ------------------------------------------------------------
    def _irq_wrapper(self, pin):
        # Read + clear IRQ flags
        status = self.read_status()
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        # Call user callback outside SPI operations
        if self.irq_handler:
            self.irq_handler(status)

    # ------------------------------------------------------------
    # Register access
    # ------------------------------------------------------------
    def reg_read(self, reg):
        self.cs(0)
        self.spi.readinto(self.buf, reg)
        self.spi.readinto(self.buf)
        self.cs(1)
        return self.buf[0]

    def reg_write_bytes(self, reg, buf):
        self.cs(0)
        self.spi.readinto(self.buf, 0x20 | reg)
        self.spi.write(buf)
        self.cs(1)
        return self.buf[0]

    def reg_write(self, reg, value):
        self.cs(0)
        self.spi.readinto(self.buf, 0x20 | reg)
        ret = self.buf[0]
        self.spi.readinto(self.buf, value)
        self.cs(1)
        return ret

    def read_status(self):
        self.cs(0)
        self.spi.readinto(self.buf, NOP)
        self.cs(1)
        return self.buf[0]

    def read_observe_tx(self):
        """Return PLOS_CNT in bits 7:4 and ARC_CNT in bits 3:0."""
        return self.reg_read(OBSERVE_TX)

    def flush_rx(self):
        self.cs(0)
        self.spi.readinto(self.buf, FLUSH_RX)
        self.cs(1)

    def flush_tx(self):
        self.cs(0)
        self.spi.readinto(self.buf, FLUSH_TX)
        self.cs(1)

    # ------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------
    def set_power_speed(self, power, speed):
        setup = self.reg_read(RF_SETUP) & 0b11010001
        self.reg_write(RF_SETUP, setup | power | speed)

    def set_crc(self, length):
        config = self.reg_read(CONFIG) & ~(CRCO | EN_CRC)
        if length == 1:
            config |= EN_CRC
        elif length == 2:
            config |= EN_CRC | CRCO
        self.reg_write(CONFIG, config)

    def set_channel(self, channel):
        self.reg_write(RF_CH, min(channel, 125))

    # ------------------------------------------------------------
    # Pipes
    # ------------------------------------------------------------
    def open_tx_pipe(self, address):
        assert len(address) == 5
        self.reg_write_bytes(RX_ADDR_P0, address)
        self.reg_write_bytes(TX_ADDR, address)
        self.reg_write(RX_PW_P0, self.payload_size)
        # Pipe 0 is used by the PTX to receive automatic acknowledgements.
        self.reg_write(EN_RXADDR, self.reg_read(EN_RXADDR) | 0x01)

    def open_rx_pipe(self, pipe_id, address):
        assert len(address) == 5
        assert 0 <= pipe_id <= 5

        if pipe_id == 0:
            self.pipe0_read_addr = address

        if pipe_id < 2:
            self.reg_write_bytes(RX_ADDR_P0 + pipe_id, address)
        else:
            self.reg_write(RX_ADDR_P0 + pipe_id, address[0])

        self.reg_write(RX_PW_P0 + pipe_id, self.payload_size)
        self.reg_write(EN_RXADDR, self.reg_read(EN_RXADDR) | (1 << pipe_id))

    def close_rx_pipe(self, pipe_id):
        assert 0 <= pipe_id <= 5
        self.reg_write(EN_RXADDR,
                       self.reg_read(EN_RXADDR) & ~(1 << pipe_id))
        if pipe_id == 0:
            self.pipe0_read_addr = None

    # ------------------------------------------------------------
    # RX/TX
    # ------------------------------------------------------------
    def start_listening(self):
        self.power_up()
        config = self.reg_read(CONFIG)
        self.reg_write(CONFIG, config | PRIM_RX)
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        if self.pipe0_read_addr is not None:
            self.reg_write_bytes(RX_ADDR_P0, self.pipe0_read_addr)

        self.ce(1)
        # 130 µs is enough when already powered
        utime.sleep_us(130)

    def stop_listening(self):
        self.ce(0)

    def any(self):
        return not bool(self.reg_read(FIFO_STATUS) & RX_EMPTY)

    def recv(self):
        self.cs(0)
        self.spi.readinto(self.buf, R_RX_PAYLOAD)
        buf = self.spi.read(self.payload_size)
        self.cs(1)
        self.reg_write(STATUS, RX_DR)
        return buf

    def send(self, buf, timeout=500):
        self.send_start(buf)
        start = utime.ticks_ms()
        result = None

        while result is None and utime.ticks_diff(utime.ticks_ms(), start) < timeout:
            result = self.send_done()

        if result is None:
            self.abort_send()
            raise OSError("timed out")

        if result == 2:
            self.flush_tx()
            raise OSError("send failed")

    def send_start(self, buf):
        """Start a transmission. Radio must already be powered."""
        if len(buf) > self.payload_size:
            raise ValueError("payload exceeds configured payload size")

        self.power_up()                       # no-op if already up

        # Switch to PTX
        config = self.reg_read(CONFIG)
        self.reg_write(CONFIG, (config | PWR_UP) & ~PRIM_RX)

        # Clear any previous status
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        # Upload payload
        self.cs(0)
        self.spi.readinto(self.buf, W_TX_PAYLOAD)
        self.spi.write(buf)
        if len(buf) < self.payload_size:
            self.spi.write(b"\x00" * (self.payload_size - len(buf)))
        self.cs(1)

        # Pulse CE - 10 µs
        self.ce(1)
        utime.sleep_us(15)
        self.ce(0)

    def send_done(self):
        """
        Non-blocking check.
        Returns:
            None  - still in progress
            1     - success (TX_DS)
            2     - failure (MAX_RT)
        """
        status = self.read_status()
        if not (status & (TX_DS | MAX_RT)):
            return None

        # Clear flags
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        if status & TX_DS:
            return 1
        return 2

    def send(self, buf, timeout_ms=50):
        """Blocking convenience wrapper (still much faster than before)."""
        self.send_start(buf)
        start = utime.ticks_ms()
        while True:
            result = self.send_done()
            if result is not None:
                if result == 2:
                    self.flush_tx()
                    raise OSError("send failed")
                return
            if utime.ticks_diff(utime.ticks_ms(), start) >= timeout_ms:
                self.abort_send()
                raise OSError("timed out")
            # tight spin - a few µs is enough; avoid asyncio here
            utime.sleep_us(50)


    def abort_send(self):
        self.ce(0)
        self.flush_tx()
        self.reg_write(STATUS, TX_DS | MAX_RT)
        # stay powered

    # ------------------------------------------------------------
    # Constant carrier mode
    # ------------------------------------------------------------
    def enter_const_carrier(self, channel=None, power=POWER_3):
        if channel is not None:
            self.set_channel(channel)

        config = self.reg_read(CONFIG)
        config |= PWR_UP
        config &= ~PRIM_RX
        self.reg_write(CONFIG, config)
        utime.sleep_ms(2)

        setup = self.reg_read(RF_SETUP)
        setup &= 0b00110001
        setup |= power | CONT_WAVE | PLL_LOCK
        self.reg_write(RF_SETUP, setup)

        self.ce(1)

    def leave_const_carrier(self):
        self.ce(0)
        setup = self.reg_read(RF_SETUP)
        setup &= ~(CONT_WAVE | PLL_LOCK)
        self.reg_write(RF_SETUP, setup)

        config = self.reg_read(CONFIG)
        config &= ~PWR_UP
        self.reg_write(CONFIG, config)

    # ------------------------------------------------------------
    # Optional helpers for the transport layer
    # ------------------------------------------------------------
    def set_retries(self, ard_us=250, arc=3):
        ard_steps = max(0, min(15, (ard_us // 250) - 1))
        self.reg_write(SETUP_RETR, (ard_steps << 4) | (arc & 0x0F))

    def disable_auto_ack(self):
        self.reg_write(EN_AA, 0x00)

    def enable_auto_ack(self, pipes=0x3F):
        self.reg_write(EN_AA, pipes & 0x3F)

    def set_no_retries(self):
        self.reg_write(SETUP_RETR, 0)

