# Vanchor-NG developer tasks. `make <target>`; run inside the project venv.
.PHONY: help docs test

help:
	@echo "make docs   Generate the Markdown API reference from docstrings -> docs/api/"
	@echo "make test   Run the test suite"

# Auto-generated Markdown API reference for the whole `vanchor` package, built
# from the code's docstrings with pydoc-markdown (needs the docs extra:
# pip install -e '.[docs]'). One file per top-level package under docs/api/.
docs:
	python scripts/gen_api_docs.py

test:
	pytest -q
