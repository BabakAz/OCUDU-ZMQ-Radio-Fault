# SPDX-License-Identifier: GPL-3.0-only
#
# Build the C broker with the reference toolchain, run the offline tests, and
# check the paper provenance. `make help` lists the targets.

# The paper's recorded C build identity was produced by Ubuntu clang 18.1.3
# with exactly these flags; other compilers build a working broker but a
# different binary hash. Override with `make CC=...` if needed.
ifeq ($(origin CC),default)
CC := clang-18
endif
CFLAGS := -std=c17 -O2 -Wall -Wextra -Wpedantic -Werror
LDLIBS := -lzmq -lm -pthread
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

BUILD := build
BROKER := $(BUILD)/zmq_channel_broker
BROKER_SOURCES := scripts/zmq_channel_broker.c scripts/radio_schedule_native.h \
                  scripts/radio_schedule_sha256.h scripts/radio_metrics_native.h

.PHONY: all broker test verify-provenance demo validate-local clean help

all: broker

broker: $(BROKER) ## Build the C broker into build/

$(BROKER): $(BROKER_SOURCES)
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) scripts/zmq_channel_broker.c -o $@ $(LDLIBS)

test: ## Run the complete offline test suite (starts only bounded local fixtures)
	$(PYTHON) -m pytest -q

verify-provenance: ## Check byte identity with the paper revisions and rebuild the recorded C binary
	$(PYTHON) scripts/verify_provenance.py

demo: $(BROKER) ## Broker-only end-to-end run of the B1 blank recipe with synthetic peers
	$(PYTHON) scripts/radio_fault.py demo ul_blank_50ms --c-binary $(BROKER)

validate-local: $(BROKER) ## Actual-process L2 identity/fixed/schedule/metrics validators (writes artifacts/)
	$(PYTHON) scripts/validate_radio_broker_identity.py --run-local --backend both --output artifacts/identity-$$(date +%Y%m%dT%H%M%S)
	$(PYTHON) scripts/validate_radio_broker_fixed.py --run-local --backend both --output artifacts/fixed-$$(date +%Y%m%dT%H%M%S)
	$(PYTHON) scripts/validate_radio_broker_schedule.py --run-local --backend both --output artifacts/schedule-$$(date +%Y%m%dT%H%M%S)
	$(PYTHON) scripts/validate_radio_broker_metrics.py --run-local --backend both --output artifacts/metrics-$$(date +%Y%m%dT%H%M%S)

clean: ## Remove build outputs
	rm -rf $(BUILD)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'
