PYTHON ?= python3.12

.PHONY: check
check:
	$(PYTHON) -m pytest -q
	$(PYTHON) -m ruff check .
