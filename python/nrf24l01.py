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
                 irq=None, irq_handler=None):

        assert payload_size <= 32

        self.spi = spi
        self.cs = cs
        self.ce = ce
        self.irq = irq
        self.irq_handler = irq_handler
        self.buf = bytearray(1)
        self.payload_size = payload_size
        self.pipe0_read_addr = None

        # Optional SPI init
        if init_spi:
            NRF24L01.init_spi_bus(spi)

        # Optional pin init
        if init_pins:
            NRF24L01.init_radio_pins(cs, ce)

        # Optional IRQ init
        if irq is not None:
            NRF24L01.init_irq_pin(irq)
            if irq_handler is not None:
                irq.irq(trigger=irq.IRQ_FALLING, handler=self._irq_wrapper)

        utime.sleep_ms(5)

        # Device presence check
        self.reg_write(SETUP_AW, 0b11)
        if self.reg_read(SETUP_AW) != 0b11:
            raise OSError("nRF24L01+ Hardware not responding")

        # Disable dynamic payloads
        self.reg_write(DYNPD, 0)

        # Auto retransmit: 1750us, count=8
        self.reg_write(SETUP_RETR, (6 << 4) | 8)

        # RF power + speed
        self.set_power_speed(POWER_3, SPEED_250K)

        # CRC
        self.set_crc(2)

        # Clear status flags
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        # Channel
        self.set_channel(channel)

        # Flush FIFOs
        self.flush_rx()
        self.flush_tx()

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

    # ------------------------------------------------------------
    # RX/TX
    # ------------------------------------------------------------
    def start_listening(self):
        self.reg_write(CONFIG, self.reg_read(CONFIG) | PWR_UP | PRIM_RX)
        self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)

        if self.pipe0_read_addr is not None:
            self.reg_write_bytes(RX_ADDR_P0, self.pipe0_read_addr)

        self.flush_rx()
        self.flush_tx()
        self.ce(1)
        utime.sleep_us(130)

    def stop_listening(self):
        self.ce(0)
        self.flush_tx()
        self.flush_rx()

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
            self.flush_tx()
            self.reg_write(CONFIG, self.reg_read(CONFIG) & ~PWR_UP)
            raise OSError("timed out")

        if result == 2:
            raise OSError("send failed")

    def send_start(self, buf):
        self.reg_write(CONFIG, (self.reg_read(CONFIG) | PWR_UP) & ~PRIM_RX)
        utime.sleep_us(1500)

        self.cs(0)
        self.spi.readinto(self.buf, W_TX_PAYLOAD)
        self.spi.write(buf)
        if len(buf) < self.payload_size:
            self.spi.write(b"\x00" * (self.payload_size - len(buf)))
        self.cs(1)

        self.ce(1)
        utime.sleep_us(15)
        self.ce(0)

    def send_done(self):
        status = self.read_status()
        if not (status & (TX_DS | MAX_RT)):
            return None

        status = self.reg_write(STATUS, RX_DR | TX_DS | MAX_RT)
        self.reg_write(CONFIG, self.reg_read(CONFIG) & ~PWR_UP)

        if status & TX_DS:
            return 1
        return 2

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
    # Convenience
    # ------------------------------------------------------------
    def disableAutoAck(self):
        EN_AA = const(0x01)
        self.reg_write(EN_AA, 0x00)

    def setNoRetries(self):
        self.reg_write(SETUP_RETR, 0)


