
import utime
import os
import channels
import nrf24l01
from tests import get_nrf

def cc_hop(power=nrf24l01.POWER_3, dwell_ms=0):
    counter = 0
    last_report = utime.ticks_ms()

    led1 = Pin('A15', Pin.OUT)
    led2 = Pin('C10', Pin.OUT)

    led1.value( False )
    led2.value( False )

    cnls = channels.bluetooth_channels
    cnls_qty = len(cnls)

    nrf, cs, ce = get_nrf()
    radio = nrf

    index = 0

    radio.set_power_speed( power, nrf24l01.SPEED_250K )
    radio.disableAutoAck()
    radio.setNoRetries()

    print("Starting constant carrier hopping test.")
    print("Power =", power, "Dwell =", dwell_ms, "ms")

    radio.enter_const_carrier( 0, power )

    try:
        while True:
            r = int(os.urandom(1)[0])
            index = (r + index) % cnls_qty
            ch = cnls[index]
            radio.set_channel( ch )
            if dwell_ms > 0:
                utime.sleep_ms(dwell_ms)

            counter += 1
            now = utime.ticks_ms()
            diff_ms = utime.ticks_diff(now, last_report)
            if diff_ms >= 1000:
                rate = (counter * 1000) / diff_ms
                last_report = now
                counter = 0
                print( f"r: {r}, index: {index}, ch: {ch}" )
                print( "Count:", counter, "dt:", diff_ms, "[ms] Rate:", rate, "Hz" )
                # Blink. This is for measuring switching frequency.
                led1.value( not led1.value() )

    except KeyboardInterrupt:
        print("Stopping constant carrier.")
        radio.leave_const_carrier()


