from pyb import Pin
from tests import get_nrf
import utime

def rx_loop():
    led1 = Pin('A15', Pin.OUT)
    led2 = Pin('C10', Pin.OUT)

    nrf, cs, ce = get_nrf()
    radio = nrf

    addr = b"ABCDE"
    radio.open_rx_pipe(0, addr)

    radio.start_listening()
    print("Listening...")

    while True:
        if radio.any():
            payload = radio.recv()
            print("RX:", payload)
            value = payload[0]
            led1.value( value & 0x01 )
            led2.value( value & 0x02 )
        utime.sleep_ms(10)


