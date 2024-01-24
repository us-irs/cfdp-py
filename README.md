cfdpy - High level library for CFDP components
======================

The `cfdpy` library offers some high-level CCSDS File Delivery Protocol (CFDP) components to
perform file transfers according to the [CCSDS Blue Book 727.0-B-5](https://public.ccsds.org/Pubs/727x0b5.pdf).
The underlying base packet library used to generate the packets to be sent is the
[spacepackets](https://github.com/us-irs/spacepackets-py) library.

# Install

You can install this package from PyPI

Linux:

```sh
python3 -m pip install cfdpy
```

Windows:

```sh
py -m pip install cfdpy 
```

# Examples

You can find all examples [inside the documentation](https://cfdpy.readthedocs.io/en/latest/examples.html) and the `examples` directory of this repository.

# Tests

If you want to run the tests, it is recommended to install `pytest` and `coverage` (optional)
first. You also have to install the package with the optional `test` feature:

```sh
pip install coverage pytest
pip install cfdpy[test]
```

Running tests regularly:

```sh
pytest .
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
