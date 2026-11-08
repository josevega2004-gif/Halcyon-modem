CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Iinclude
BUILD := build
BIN := $(BUILD)/halcyon-modem
TUI := $(BUILD)/halcyon-tui
GUI := $(BUILD)/halcyon-gui
QT_CFLAGS := $(shell pkg-config --cflags Qt6Widgets Qt6Network 2>/dev/null)
QT_LIBS := $(shell pkg-config --libs Qt6Widgets Qt6Network 2>/dev/null)

.PHONY: all clean selftest tables tui gui

all: $(BIN) $(TUI)

$(BIN): src/halcyon.cpp src/main.cpp include/halcyon.hpp include/halcyon_tables.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/halcyon.cpp src/main.cpp

# A terminal front end for the Python console: no browser, no third-party
# dependency, just the loopback API.
$(TUI): src/tui.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ src/tui.cpp

tui: $(TUI)

# A Qt 6 window for the same console.  Qt is optional: only this target needs
# it, so `make` and `make selftest` still build on a box without Qt.
$(GUI): src/gui.cpp | $(BUILD)
	@test -n "$(QT_LIBS)" || { echo "Qt 6 Widgets/Network not found (install qt6-base-dev)" >&2; exit 1; }
	$(CXX) $(CXXFLAGS) $(QT_CFLAGS) -fPIC -o $@ src/gui.cpp $(QT_LIBS)

gui: $(GUI)

$(BUILD):
	mkdir -p $(BUILD)

selftest: $(BIN)
	./$(BIN) selftest

# Regenerate the shared tables and golden vectors from the Python modem.
tables:
	cd .. && PYTHONPATH=. python3 tools/dump_cpp_data.py

clean:
	rm -rf $(BUILD)
