import utime
import os
import nrf24l01
from tests import get_nrf

def cc_hop(power=nrf24l01.POWER_0, dwell_ms=0):
    nrf, cs, ce = get_nrf()
    radio = nrf

    radio.set_power_speed( power, nrf24l01.SPEED_250K )
    radio.disableAutoAck()
    radio.setNoRetries()

    print("Starting constant carrier hopping test.")
    print("Power =", power, "Dwell =", dwell_ms, "ms")

    radio.enter_const_carrier( 0, power )

    try:
        while True:
            for ch in range(0, 126):
                radio.set_channel( ch )
                if dwell_ms > 0:
                    utime.sleep_ms(dwell_ms)

    except KeyboardInterrupt:
        print("Stopping constant carrier.")
        radio.leave_const_carrier()
