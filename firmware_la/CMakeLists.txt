cmake_minimum_required(VERSION 3.13)

# Logic analyser firmware for the SECOND Pico (a plain Pico 2; a Pico 2 W also works).
#   export PICO_SDK_PATH=/path/to/pico-sdk
#   cmake -S firmware_la -B build/firmware_la && cmake --build build/firmware_la
set(PICO_BOARD pico2 CACHE STRING "Board")

if(NOT PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
  set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()
if(NOT PICO_SDK_PATH)
  message(FATAL_ERROR "Set PICO_SDK_PATH to a Pico SDK checkout (see firmware_la/README.md)")
endif()
include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(eol_la C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(eol_la src/main.c src/la_core.c)
pico_generate_pio_header(eol_la ${CMAKE_CURRENT_LIST_DIR}/src/la.pio)
target_include_directories(eol_la PRIVATE src)
target_compile_options(eol_la PRIVATE -Wall -Wextra)
# Its own USB name, so the CM5 tells it apart from the front-end Pico:
#   /dev/serial/by-id/usb-Zynith_EOL_logic_analyser_<serial>-if00
target_compile_definitions(eol_la PRIVATE USBD_MANUFACTURER="Zynith" USBD_PRODUCT="EOL logic analyser")
target_link_libraries(eol_la pico_stdlib hardware_pio hardware_dma hardware_pwm hardware_clocks)
pico_enable_stdio_usb(eol_la 1)
pico_enable_stdio_uart(eol_la 0)
pico_add_extra_outputs(eol_la)
