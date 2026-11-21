cmake_minimum_required(VERSION 3.13)

# Build for the Pico 2 W:
#   export PICO_SDK_PATH=/path/to/pico-sdk      (version 2.1.0 or later, with lib/tinyusb)
#   cmake -S firmware -B build/firmware && cmake --build build/firmware
set(PICO_BOARD pico2_w CACHE STRING "Board")

if(NOT PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
  set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()
if(NOT PICO_SDK_PATH)
  message(FATAL_ERROR "Set PICO_SDK_PATH to a Pico SDK checkout (see firmware/README.md)")
endif()
include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(eol_pico C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

execute_process(
  COMMAND git rev-parse --short HEAD
  WORKING_DIRECTORY ${CMAKE_CURRENT_LIST_DIR}
  OUTPUT_VARIABLE EOL_BUILD_TAG
  OUTPUT_STRIP_TRAILING_WHITESPACE
  ERROR_QUIET)
if(NOT EOL_BUILD_TAG)
  set(EOL_BUILD_TAG dev)
endif()

add_executable(eol_pico
  src/main.c
  src/usb_descriptors.c
  src/core.c
  src/proto.c)

target_include_directories(eol_pico PRIVATE src)
target_compile_definitions(eol_pico PRIVATE EOL_BUILD_TAG="${EOL_BUILD_TAG}")
target_compile_options(eol_pico PRIVATE -Wall -Wextra)

target_link_libraries(eol_pico
  pico_stdlib
  pico_unique_id
  hardware_clocks
  hardware_watchdog
  tinyusb_device
  tinyusb_board)

# The USB port carries our own two serial ports, so debug text goes to the UART (GP0/GP1).
pico_enable_stdio_usb(eol_pico 0)
pico_enable_stdio_uart(eol_pico 1)

pico_add_extra_outputs(eol_pico)
