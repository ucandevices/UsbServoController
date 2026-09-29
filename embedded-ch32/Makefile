# ------------------------------------------------------------------------------
# USB-C 12-Channel Servo Controller -- CH32V203C8T6 firmware
#
# Feasibility port of ../embedded (STM32F072C8T6) onto WCH RISC-V silicon.
#
#   make            build ELF + HEX + BIN into build/ (application, linked at
#                   0x3000 above the USB bootloader -- see User/flash_layout.h)
#   make flash      build, then reflash over USB via the bootloader: no jumper
#   make full       bootloader + application as one image from 0x0000, for a
#                   blank chip over the ROM bootloader (JP2 jumper + wchisp)
#   make clean
#   make size
#   make hosttest   run sense.c's protection logic on the PC, against a stubbed
#                   ADC (needs a host C compiler: HOSTCC=gcc, clang, "zig cc")
#
# Toolchain: xPack riscv-none-elf-gcc. Override with:
#   make GCC_PATH=/path/to/riscv-none-elf/bin
# ------------------------------------------------------------------------------

TARGET = USBServoController-ch32

GCC_PATH ?= C:/Users/LJ/AppData/Local/rvgcc/xpack-riscv-none-elf-gcc-15.2.0-1/bin
PREFIX = riscv-none-elf-
CC = $(GCC_PATH)/$(PREFIX)gcc
CP = $(GCC_PATH)/$(PREFIX)objcopy
SZ = $(GCC_PATH)/$(PREFIX)size

BUILD_DIR = build

C_SOURCES = \
  $(wildcard User/*.c) \
  $(wildcard HAL/Peripheral/src/*.c) \
  $(wildcard HAL/Core/*.c) \
  $(wildcard HAL/Debug/*.c) \
  $(wildcard USBLIB/CONFIG/*.c) \
  $(wildcard USBLIB/USB-Driver/src/*.c)

ASM_SOURCES = Startup/startup_ch32v20x_D6.S

# CH32V203 = QingKe V4B core: RV32IMAC.
CPU    = -march=rv32imac_zicsr_zifencei -mabi=ilp32
C_DEFS =
C_INCLUDES = \
  -IUser \
  -IHAL/Peripheral/inc \
  -IHAL/Core \
  -IHAL/Debug \
  -IUSBLIB/CONFIG \
  -IUSBLIB/USB-Driver/inc

OPT = -Og
CSTD = -std=gnu11
CFLAGS  = $(CPU) $(CSTD) -msmall-data-limit=8 -mno-save-restore $(C_DEFS) $(C_INCLUDES) $(OPT) \
          -Wall -Wextra -fmessage-length=0 -fsigned-char \
          -ffunction-sections -fdata-sections -g -gdwarf-2 -MMD -MP
ASFLAGS = $(CPU) -x assembler-with-cpp $(C_INCLUDES) -g -gdwarf-2

LDSCRIPT = Ld/Link.ld
LDFLAGS  = $(CPU) -T$(LDSCRIPT) -nostartfiles --specs=nano.specs --specs=nosys.specs \
           -Wl,-Map=$(BUILD_DIR)/$(TARGET).map,--cref -Wl,--gc-sections

OBJECTS  = $(addprefix $(BUILD_DIR)/,$(notdir $(C_SOURCES:.c=.o)))
vpath %.c $(sort $(dir $(C_SOURCES)))
OBJECTS += $(addprefix $(BUILD_DIR)/,$(notdir $(ASM_SOURCES:.S=.o)))
vpath %.S $(sort $(dir $(ASM_SOURCES)))

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin

$(BUILD_DIR)/%.o: %.c Makefile | $(BUILD_DIR)
	$(CC) -c $(CFLAGS) $< -o $@

$(BUILD_DIR)/%.o: %.S Makefile | $(BUILD_DIR)
	$(CC) -c $(ASFLAGS) $< -o $@

$(BUILD_DIR)/$(TARGET).elf: $(OBJECTS) Makefile
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@
	$(SZ) $@

$(BUILD_DIR)/%.hex: $(BUILD_DIR)/%.elf
	$(CP) -O ihex $< $@

$(BUILD_DIR)/%.bin: $(BUILD_DIR)/%.elf
	$(CP) -O binary -S $< $@

$(BUILD_DIR):
	mkdir -p $@

size: $(BUILD_DIR)/$(TARGET).elf
	$(SZ) $<

# --- USB bootloader ----------------------------------------------------------
BL_BIN   = bootloader/build/ch32-bootloader.bin
APP_BASE = 0x3000
PYTHON  ?= python

bootloader:
	$(MAKE) -C bootloader GCC_PATH=$(GCC_PATH)

# Bootloader padded to APP_BASE with 0xFF, then the application.
full: all bootloader
	$(CP) -I binary -O binary --pad-to $(APP_BASE) --gap-fill 0xFF $(BL_BIN) $(BUILD_DIR)/bl-padded.bin
	cat $(BUILD_DIR)/bl-padded.bin $(BUILD_DIR)/$(TARGET).bin > $(BUILD_DIR)/$(TARGET)-full.bin
	@echo "first flash (JP2 fitted): wchisp flash $(BUILD_DIR)/$(TARGET)-full.bin"

flash: all
	$(PYTHON) ../tools/flash.py $(BUILD_DIR)/$(TARGET).bin

# header dependencies (-MMD): a changed .h rebuilds what includes it
-include $(OBJECTS:.o=.d)

clean:
	rm -rf $(BUILD_DIR)
	$(MAKE) -C bootloader clean

# --- host-side unit test of the rail protection logic ------------------------
# Compiles the real User/sense.c for the PC. Each scenario runs in its own
# process because sense.c keeps static state.
HOSTCC ?= gcc
HOSTTEST_SCENARIOS = 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 20 21 22 23 24

hosttest: | $(BUILD_DIR)
	$(HOSTCC) -std=gnu11 -Wall -Wextra -O1 -Itest/stub -IUser User/sense.c test/test_sense.c -o $(BUILD_DIR)/test_sense.exe
	@for s in $(HOSTTEST_SCENARIOS); do ./$(BUILD_DIR)/test_sense.exe $$s || { echo "scenario $$s FAILED"; exit 1; }; done
	@echo "hosttest: all scenarios passed"

.PHONY: all clean size hosttest bootloader full flash
