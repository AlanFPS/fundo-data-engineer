COMPOSE ?= docker compose
RUN      = $(COMPOSE) run --rm app
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

.PHONY: help up demo seed load simulate check dedupe dedupe-apply dedupe-undo break repair stats test down

help:           ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-13s %s\n", $$1, $$2}'

up:             ## start SQL Server and wait until it is up
	$(COMPOSE) up -d --wait

demo: up        ## the whole story end to end: seed, load, simulate, crash, dedupe, break, repair
	$(RUN) demo

seed: up        ## (re)create the source database with synthetic data; resets the warehouse
	$(RUN) seed

load:           ## incremental load source -> warehouse (full snapshot on first run)
	$(RUN) load

simulate:       ## one business day of inserts/updates/deletes at the source (~1% of rows)
	$(RUN) simulate

check:          ## is the warehouse complete and correct right now? fails (non-zero) if not
	$(RUN) check

dedupe:         ## duplicate-customer plan, dry run (writes reports/)
	$(RUN) dedupe

dedupe-apply:   ## apply the plan: backup, one transaction, change log, unique index
	$(RUN) dedupe --apply

dedupe-undo:    ## reverse the last applied dedupe run
	$(RUN) dedupe --undo

break:          ## damage the warehouse four ways so the check has something to catch
	$(RUN) break

repair:         ## full refresh of every table the check fails on, then check again
	$(RUN) repair

stats:          ## source sizes, change tracking overhead, bytes moved per load
	$(RUN) stats

test:           ## unit tests for the identity rules (no database needed)
	$(COMPOSE) run --rm --no-deps --entrypoint pytest app -q tests

down:           ## stop everything and delete the source database volume, warehouse and reports
	$(COMPOSE) down -v
	rm -rf data reports
