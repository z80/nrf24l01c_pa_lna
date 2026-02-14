from machine import SPI
from pyb import Pin
import utime

from nrf24l01 import NRF24L01

#spi = SPI(1, baudrate=4000000, polarity=0, phase=0)
#csn = Pin("X5", Pin.OUT, value=1)
#ce  = Pin("X4", Pin.OUT, value=0)

#nrf = NRF24L01(spi, csn, ce)

def get_nrf():
    spi = SPI(1)
    cs  = Pin( "C4", mode=Pin.OUT, value=1 )
    ce  = Pin( "C5", mode=Pin.OUT, value=0 )
    irq = Pin( "A4", Pin.IN, Pin.PULL_UP )
    nrf = NRF24L01( spi, cs, ce, payload_size=8 )

    return nrf, cs, ce


def nrf_status(nrf):
    # STATUS is always returned on every SPI command
    return nrf.reg_read(0x07)


def nrf_config(nrf):
    return nrf.reg_read(0x00)


def nrf_rw_test(nrf):
    original = nrf.reg_read(0x06)
    testval = original ^ 0x01  # flip a bit
    nrf.reg_write(0x06, testval)
    verify = nrf.reg_read(0x06)
    # restore
    nrf.reg_write(0x06, original)
    return original, testval, verify


def nrf_aw(nrf):
    return nrf.reg_read(0x03)


def nrf_fifo(nrf):
    return nrf.reg_read(0x17)


def nrf_health(nrf):
    print("STATUS      =", hex(nrf.reg_read(0x07)))
    print("CONFIG      =", hex(nrf.reg_read(0x00)))
    print("RF_SETUP    =", hex(nrf.reg_read(0x06)))
    print("SETUP_AW    =", hex(nrf.reg_read(0x03)))
    print("FIFO_STATUS =", hex(nrf.reg_read(0x17)))

    # Write/read test
    orig = nrf.reg_read(0x06)
    nrf.reg_write(0x06, orig ^ 1)
    verify = nrf.reg_read(0x06)
    nrf.reg_write(0x06, orig)
    print("RW test OK  =", verify == (orig ^ 1))



def test_rx_mode(nrf):
    radio = nrf

    print("Initial STATUS =", hex(nrf_status(radio)))

    print("\n=== Entering RX mode (start_listening) ===")
    radio.start_listening()
    utime.sleep_ms(5)

    print("STATUS after start_listening =", hex(nrf_status(radio)))

    print("\n=== Sampling STATUS in RX mode ===")
    for i in range(5):
        utime.sleep_ms(200)
        print(f"Sample {i+1}:", hex(nrf_status(radio)))

    print("\n=== Stopping RX mode ===")
    radio.stop_listening()
    utime.sleep_ms(2)

    print("STATUS after stop_listening =", hex(nrf_status(radio)))
    print("CE pin =", ce.value())

    print("\n=== RX mode test complete ===")



