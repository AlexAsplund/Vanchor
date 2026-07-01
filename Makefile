# Vanchor-NG developer tasks. `make <target>`; run inside the project venv.
.PHONY: help docs docs-serve test

help:
	@echo "make docs        Generate the API reference from docstrings -> docs/api/"
	@echo "make docs-serve  Live, auto-reloading API docs server (browse while editing)"
	@echo "make test        Run the test suite"

# Auto-generated API reference for the whole `vanchor` package, built from the
# code's docstrings with pdoc (needs the docs extra: pip install -e '.[docs]').
# Output lives in docs/api/ and is gitignored — regenerate any time with `make docs`.
docs:
	pdoc vanchor -o docs/api --docformat google
	@echo "API reference -> docs/api/index.html"

# Same, but served live with auto-reload — handy while writing docstrings.
docs-serve:
	pdoc vanchor --docformat google

test:
	pytest -q
