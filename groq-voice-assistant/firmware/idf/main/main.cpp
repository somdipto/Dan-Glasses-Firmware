// ESP-IDF entry: compile the Arduino sketch unchanged. arduino-esp32 (autostart)
// provides app_main(), which calls setup() and loop() from the sketch.
#include "voice_assistant.ino"

static_assert(LED_BUILTIN == 21, "XIAO ESP32-S3 pin map not active (status LED is GPIO21)");
static_assert(ARDUINO_USB_MODE == 1 && ARDUINO_USB_CDC_ON_BOOT == 1,
              "Serial must be the USB-C hardware CDC port");
