[![ci](https://github.com/us-irs/cfdp-py/actions/workflows/ci.yml/badge.svg)](https://github.com/us-irs/cfdp-py/actions/workflows/ci.yml)
[![Documentation Status](https://readthedocs.org/projects/cfdp-py/badge/?version=latest)](https://cfdp-py.readthedocs.io/en/latest/?badge=latest)
[![codecov](https://codecov.io/gh/us-irs/cfdp-py/graph/badge.svg?token=FBL1NR54BI)](https://codecov.io/gh/us-irs/cfdp-py)
[![PyPI version](https://badge.fury.io/py/cfdp-py.svg)](https://badge.fury.io/py/cfdp-py)

cfdp-py - High level Python library for CFDP components
======================

The `cfdp-py` library offers some high-level CCSDS File Delivery Protocol (CFDP) components to
perform file transfers according to the [CCSDS Blue Book 727.0-B-5](https://public.ccsds.org/Pubs/727x0b5.pdf).
The underlying base packet library used to generate the packets to be sent is the
[spacepackets](https://github.com/us-irs/spacepackets-py) library.

# Features

This library supports the following features:

- Unacknowledged (class 1) file transfers for both the sending and destination side
- Acknowledged (class 2) file transfers for both the sending and destination side

The following features have not been implemented yet. PRs or notifications for demand are welcome!

- Suspending transfers
- Inactivity handling
- Start and end of transmission and reception opportunity handling
- Keep Alive and Prompt PDU handling

# Install

You can install this package from PyPI

For example, using [`uv`](https://docs.astral.sh/uv/)

Setting up virtual environment:

```sh
uv venv
```

Regular install:

```sh
uv pip install -e .
```

Interactive install with testing support:

```sh
uv pip install -e ".[test]"
```

# Examples

You can find all examples [inside the documentation](https://cfdp-py.readthedocs.io/en/latest/examples.html)
and the `examples` directory of this repository.

# Tests

If you want to run the tests, it is recommended to install `pytest` and `coverage` (optional)
first. You also have to install the package with the optional `test` feature:

```sh
uv pip install -e ".[test]"
```

Running tests regularly:

```sh
pytest
```

Running tests with coverage:

```sh
coverage run -m pytest
```

# Documentation

The documentation is built with Sphinx

Install the required dependencies first:

```sh
pip install -r docs/requirements.txt
```

Then the documentation can be built with

```sh
cd docs
make html
```

You can run the doctests with

```sh
make doctest
```

# Formatting and Linting

Linting:

```sh
ruff check
```

Formatting:

```sh
ruff format
```
