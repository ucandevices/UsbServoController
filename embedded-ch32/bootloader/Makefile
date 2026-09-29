# ------------------------------------------------------------------------------
# USB CDC bootloader for the CH32V203 servo controller -- first 12 KB of flash.
#
#   make            build build/ch32-bootloader.{elf,hex,bin}
#   make clean
#
# Shares HAL, USB stack, clock setup and startup file with the application in
# ../. Normally built from ../Makefile ("make full"), which also produces the
# combined bootloader + application image for a first flash over the ROM
# bootloader (JP2 jumper + wchisp).
# ------------------------------------------------------------------------------

TARGET = ch32-bootloader

GCC_PATH ?= C:/Users/LJ/AppData/Local/rvgcc/xpack-riscv-none-elf-gcc-15.2.0-1/bin
PREFIX = riscv-none-elf-
CC = $(GCC_PATH)/$(PREFIX)gcc
CP = $(GCC_PATH)/$(PREFIX)objcopy
SZ = $(GCC_PATH)/$(PREFIX)size

BUILD_DIR = build
ROOT = ..

C_SOURCES = \
  bl_early.c \
  bl_main.c \
  $(ROOT)/User/system_ch32v20x.c \
  $(ROOT)/User/cdc_glue.c \
  $(ROOT)/User/ch32v20x_it.c \
  $(wildcard $(ROOT)/HAL/Peripheral/src/*.c) \
  $(wildcard $(ROOT)/HAL/Core/*.c) \
  $(wildcard $(ROOT)/HAL/Debug/*.c) \
  $(wildcard $(ROOT)/USBLIB/CONFIG/*.c) \
  $(wildcard $(ROOT)/USBLIB/USB-Driver/src/*.c)

ASM_SOURCES = $(ROOT)/Startup/startup_ch32v20x_D6.S

CPU    = -march=rv32imac_zicsr_zifencei -mabi=ilp32
C_DEFS =
C_INCLUDES = \
  -I$(ROOT)/User \
  -I$(ROOT)/HAL/Peripheral/inc \
  -I$(ROOT)/HAL/Core \
  -I$(ROOT)/HAL/Debug \
  -I$(ROOT)/USBLIB/CONFIG \
  -I$(ROOT)/USBLIB/USB-Driver/inc

# -Os: this has to fit in 12 KB.
OPT = -Os
CSTD = -std=gnu11
CFLAGS  = $(CPU) $(CSTD) -msmall-data-limit=8 -mno-save-restore $(C_DEFS) $(C_INCLUDES) $(OPT) \
          -Wall -Wextra -fmessage-length=0 -fsigned-char \
          -ffunction-sections -fdata-sections -g -gdwarf-2 -MMD -MP
ASFLAGS = $(CPU) -x assembler-with-cpp $(C_INCLUDES) -g -gdwarf-2

LDSCRIPT = Link.ld
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

$(BUILD_DIR)/$(TARGET).elf: $(OBJECTS) Makefile $(LDSCRIPT)
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@
	$(SZ) $@

$(BUILD_DIR)/%.hex: $(BUILD_DIR)/%.elf
	$(CP) -O ihex $< $@

$(BUILD_DIR)/%.bin: $(BUILD_DIR)/%.elf
	$(CP) -O binary -S $< $@

$(BUILD_DIR):
	mkdir -p $@

# header dependencies (-MMD): a changed .h rebuilds what includes it
-include $(OBJECTS:.o=.d)

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean
