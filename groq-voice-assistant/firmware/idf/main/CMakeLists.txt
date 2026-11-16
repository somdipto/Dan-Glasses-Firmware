set(SKETCH_DIR "${CMAKE_CURRENT_LIST_DIR}/../../voice_assistant")

idf_component_register(
    SRCS "main.cpp"
    # XIAO pin map first, so LED_BUILTIN is GPIO21 (not the generic S3 RGB pin).
    INCLUDE_DIRS "../variants/XIAO_ESP32S3" "${SKETCH_DIR}"
    REQUIRES arduino-esp32)

set_property(SOURCE main.cpp APPEND PROPERTY OBJECT_DEPENDS
    "${SKETCH_DIR}/voice_assistant.ino")
