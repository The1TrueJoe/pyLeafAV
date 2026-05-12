# Minimal setup.py so that ``pip install -e .`` works with pip < 21.3
# (older pip requires a setup.py or setup.cfg for editable installs).
from setuptools import setup

setup()
