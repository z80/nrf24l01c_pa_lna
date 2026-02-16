import utime
from pyb import Pin

from tests import get_nrf
from nrf24l01 import POWER_0, SPEED_250K

def tx_loop():

    led1 = Pin('A15', Pin.OUT)
    led2 = Pin('C10', Pin.OUT)

    nrf, cs, ce = get_nrf()
    radio = nrf

    # Open TX pipe with a known address
    addr = b"ABCDE"
    radio.open_tx_pipe(addr)

    nrf.set_power_speed(POWER_0, SPEED_250K)

    print("Starting TX loop...")

    counter = 0
    while True:
        payload = bytes([counter & 0xFF])  # 1-byte payload
        print("TX:", payload)

        try:
            radio.send(payload)
            led1.value( not led1.value() )
        except Exception as e:
            print("TX error:", e)

        counter += 1
        utime.sleep_ms(200)  # send every 200ms

