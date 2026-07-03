"""Hardware-in-the-loop (HIL) bench tests.

Everything in this package is tagged (via ``tests/hil/conftest.py``) with the
``hil`` marker registered in the repo-root ``conftest.py``. HIL tests talk to a
*real* bench -- an Arduino motor controller, a live GPS, a trolling motor on a
stand -- so they are **skipped by default** and only run when the operator sets
``VANCHOR_HIL=1`` to declare a bench is connected. See the module docstrings for
what a bench test asserts and how to wire one.
"""
