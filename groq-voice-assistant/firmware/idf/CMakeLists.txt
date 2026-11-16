# ESP-IDF build of firmware/voice_assistant (original 3D-glasses firmware),
# using arduino-esp32 3.3.10 as an IDF component. The sketch compiles in place.
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# Arduino IDE board flags for XIAO_ESP32S3 with "USB CDC On Boot: Enabled":
# Serial is the USB-C port. Directory-level so the Arduino core inherits them.
add_compile_definitions(ARDUINO_USB_MODE=1 ARDUINO_USB_CDC_ON_BOOT=1 ARDUINO_XIAO_ESP32S3)

project(glasses_assistant)
