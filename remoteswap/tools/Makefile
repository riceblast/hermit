CC ?= gcc
CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Werror

.PHONY: all clean
all: rswap_hotness

rswap_hotness: rswap_hotness.c rswap_hotness.h ../rswap_vpn_heat_uapi.h
	$(CC) $(CFLAGS) $< $(LDFLAGS) $(LDLIBS) -o $@

clean:
	$(RM) rswap_hotness
