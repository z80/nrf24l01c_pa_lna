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
    irq_pin="A4",
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


