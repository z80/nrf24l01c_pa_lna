from pyb import SPI
from pyb import Pin
import utime

from nrf24l01 import NRF24L01



def get_all(spi_baud=1000000):
    ret = init_nrfs( spi_baud=spi_baud, modules=[ ('C4', 'C5', 'A4'), ] )
    return ret


def init_nrfs(
    spi_id=1,
    spi_baud=4000000,
    modules=[
        # (cs_pin, ce_pin, irq_pin or None)
        ("C4", "C5", None),
        ("B0", "B1", None),
        ("A1", "A2", None),
    ],
    payload_size=8,
    init_spi=True
):
    # 1. Create SPI object
    spi = SPI(spi_id)
    if init_spi:
        NRF24L01.init_spi_bus(spi, spi_baud)

    # 2. Pre-initialize ALL CS/CE pins BEFORE creating any NRF instance
    cs_pins = []
    ce_pins = []
    irq_pins = []

    for cs_name, ce_name, irq_name in modules:
        cs = Pin(cs_name, Pin.OUT, value=1)   # deselect
        ce = Pin(ce_name, Pin.OUT, value=0)   # disable radio
        irq = Pin(irq_name, Pin.IN, Pin.PULL_UP) if irq_name else None

        cs_pins.append(cs)
        ce_pins.append(ce)
        irq_pins.append(irq)

    # 3. Now create NRF instances safely
    radios = []
    for i, (cs, ce, irq) in enumerate(zip(cs_pins, ce_pins, irq_pins)):
        nrf = NRF24L01(
            spi,
            cs,
            ce,
            payload_size=payload_size,
            init_spi=False,
            init_pins=False,
            irq=irq,
            irq_handler=None
        )
        radios.append(nrf)

    return radios

# ------------------------------------------------------------
# Factory: create an NRF24L01 instance with optional parameters
# ------------------------------------------------------------
def get_nrf(
    spi_id=1,
    spi_baud=4000000,
    cs_pin="C4",
    ce_pin="C5",
    irq_pin=None,
    init_spi=True,
    init_pins=True,
    payload_size=8
):
    """
    Create and return an NRF24L01 instance with optional configuration.

    Parameters:
        spi_id      – SPI bus number (default 1)
        spi_baud    – SPI baudrate (default 4 MHz)
        cs_pin      – chip select pin name
        ce_pin      – chip enable pin name
        irq_pin     – optional IRQ pin name (string) or None
        init_spi    – whether to initialize SPI inside the driver
        init_pins   – whether to initialize CS/CE inside the driver
        payload_size – radio payload size

    Returns:
        (nrf, cs, ce, irq)
    """

    spi = SPI(spi_id)

    if init_spi:
        NRF24L01.init_spi_bus(spi, spi_baud)

    cs = Pin(cs_pin, Pin.OUT, value=1)
    ce = Pin(ce_pin, Pin.OUT, value=0)

    irq = None
    if irq_pin is not None:
        irq = Pin(irq_pin, Pin.IN, Pin.PULL_UP)

    nrf = NRF24L01(
        spi,
        cs,
        ce,
        payload_size=payload_size,
        init_spi=False,
        init_pins=False,
        irq=irq,
        irq_handler=None
    )

    return nrf


# ------------------------------------------------------------
# Basic register tests
# ------------------------------------------------------------
def nrf_status(nrf):
    return nrf.reg_read(0x07)


def nrf_config(nrf):
    return nrf.reg_read(0x00)


def nrf_rw_test(nrf):
    original = nrf.reg_read(0x06)
    testval = original ^ 0x01
    nrf.reg_write(0x06, testval)
    verify = nrf.reg_read(0x06)
    nrf.reg_write(0x06, original)
    return original, testval, verify


def nrf_aw(nrf):
    return nrf.reg_read(0x03)


def nrf_fifo(nrf):
    return nrf.reg_read(0x17)


# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------
def nrf_health(nrf):
    print("STATUS      =", hex(nrf.reg_read(0x07)))
    print("CONFIG      =", hex(nrf.reg_read(0x00)))
    print("RF_SETUP    =", hex(nrf.reg_read(0x06)))
    print("SETUP_AW    =", hex(nrf.reg_read(0x03)))
    print("FIFO_STATUS =", hex(nrf.reg_read(0x17)))

    orig = nrf.reg_read(0x06)
    nrf.reg_write(0x06, orig ^ 1)
    verify = nrf.reg_read(0x06)
    nrf.reg_write(0x06, orig)
    print("RW test OK  =", verify == (orig ^ 1))


# ------------------------------------------------------------
# RX mode test
# ------------------------------------------------------------
def test_rx_mode(nrf):
    print("Initial STATUS =", hex(nrf_status(nrf)))

    print("\n=== Entering RX mode (start_listening) ===")
    nrf.start_listening()
    utime.sleep_ms(5)

    print("STATUS after start_listening =", hex(nrf_status(nrf)))

    print("\n=== Sampling STATUS in RX mode ===")
    for i in range(5):
        utime.sleep_ms(200)
        print("Sample", i + 1, ":", hex(nrf_status(nrf)))

    print("\n=== Stopping RX mode ===")
    nrf.stop_listening()
    utime.sleep_ms(2)

    print("STATUS after stop_listening =", hex(nrf_status(nrf)))
    print("CE pin =", nrf.ce.value())

    print("\n=== RX mode test complete ===")



