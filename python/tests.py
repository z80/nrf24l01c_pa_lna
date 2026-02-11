from machine import SPI, Pin

#spi = SPI(1, baudrate=4000000, polarity=0, phase=0)
#csn = Pin("X5", Pin.OUT, value=1)
#ce  = Pin("X4", Pin.OUT, value=0)

#nrf = NRF24L01(spi, csn, ce)


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


