import time
import math
import vgamepad as vg

# Create virtual DS4 controller
gamepad = vg.VDS4Gamepad()

print("Virtual DS4 connected. Moving in circles...")

MAX = 100   # stick radius (-127..127)
SPEED = 2 * math.pi / 3  # radians per second

start = time.time()

try:
    while True:
        t = time.time() - start
        angle = t * SPEED

        x = int(MAX * math.cos(angle))
        y = int(MAX * math.sin(angle))

        gamepad.left_joystick(x_value=x, y_value=y)
        gamepad.update()

        time.sleep(0.01)

except KeyboardInterrupt:
    print("Stopping...")

finally:
    gamepad.left_joystick(0, 0)
    gamepad.update()
    print("Controller reset.")

