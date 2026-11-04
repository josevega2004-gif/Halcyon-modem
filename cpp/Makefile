CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Iinclude
BUILD := build
BIN := $(BUILD)/halcyon-modem

.PHONY: all clean selftest tables

all: $(BIN)

$(BIN): src/halcyon.cpp src/main.cpp include/halcyon.hpp include/halcyon_tables.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/halcyon.cpp src/main.cpp

$(BUILD):
	mkdir -p $(BUILD)

selftest: $(BIN)
	./$(BIN) selftest

# Regenerate the shared tables and golden vectors from the Python modem.
tables:
	cd .. && PYTHONPATH=. python3 tools/dump_cpp_data.py

clean:
	rm -rf $(BUILD)
