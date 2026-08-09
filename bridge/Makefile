SHELL := /bin/sh

MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PROJECT_ROOT := $(abspath $(MAKEFILE_DIR)/..)

DOCKER ?= docker
PLATFORM ?= linux/amd64
DIST_DIR ?= $(MAKEFILE_DIR)/dist
BINARY := $(DIST_DIR)/deerflow-acp-bridge
DOCKERFILE := $(MAKEFILE_DIR)/Dockerfile.musl

SOURCES := $(wildcard $(MAKEFILE_DIR)/src/*.rs) \
           $(MAKEFILE_DIR)/Cargo.toml \
           $(MAKEFILE_DIR)/Cargo.lock \
           $(DOCKERFILE)

.DEFAULT_GOAL := musl

.PHONY: musl build verify clean help

musl: build verify

build: $(BINARY)

$(BINARY): $(SOURCES)
	@mkdir -p "$(DIST_DIR)"
	$(DOCKER) buildx build \
		--platform "$(PLATFORM)" \
		--file "$(DOCKERFILE)" \
		--target artifact \
		--output "type=local,dest=$(DIST_DIR)" \
		"$(PROJECT_ROOT)"
	@chmod 0755 "$(BINARY)"

verify: $(BINARY)
	$(DOCKER) run --rm \
		--platform "$(PLATFORM)" \
		--volume "$(BINARY):/usr/local/bin/deerflow-acp-bridge:ro" \
		ubuntu:22.04 \
		/usr/local/bin/deerflow-acp-bridge --version

clean:
	rm -rf -- "$(DIST_DIR)"

help:
	@printf '%s\n' \
		'make -C bridge musl   Build the static musl binary and test it on Ubuntu 22.04' \
		'make -C bridge build  Build bridge/dist/deerflow-acp-bridge' \
		'make -C bridge verify Run the built binary in an Ubuntu 22.04 container' \
		'make -C bridge clean  Remove bridge/dist'
