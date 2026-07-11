import utime
from pyb import Pin

from nrf24l01 import *
from radio import *

def tx_loop():

    led1 = Pin('A15', Pin.OUT)
    led2 = Pin('C10', Pin.OUT)

    radio = get_nrf()

    # Open TX pipe with a known address
    addr = b"ABCDE"
    radio.open_tx_pipe(addr)

    radio.set_power_speed(POWER_0, SPEED_250K)

    print("Starting TX loop...")

    counter = 0
    while True:
        counter = counter &0xFF
        payload = bytes([counter])  # 1-byte payload
        print("TX:", payload)

        try:
            radio.send(payload)
            led1.value( not led1.value() )
        except Exception as e:
            print("TX error:", e)

        led1.value( counter & 0x01 )
        led2.value( counter & 0x02 )

        counter += 1
        utime.sleep_ms(200)  # send every 200ms

