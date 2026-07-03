# Host-compiled unit test for the shared firmware command parser
# (firmware/common/vanchor_protocol.h -> vanchorParseCmd).
#
#   make        build + run the test
#   make build  compile only
#   make clean  remove the built binary
#
# The parser is dependency-free portable C, so the host compiler is enough;
# no AVR toolchain or hardware required.

CXX      ?= g++
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O2
BIN       = test_parse_cmd
SRC       = test_parse_cmd.cpp
HEADER    = ../../common/vanchor_protocol.h

.PHONY: all build run clean

all: run

$(BIN): $(SRC) $(HEADER)
	$(CXX) $(CXXFLAGS) -o $(BIN) $(SRC)

build: $(BIN)

run: $(BIN)
	./$(BIN)

clean:
	rm -f $(BIN)
