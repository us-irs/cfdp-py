=====================
Example applications
=====================

You can find an example application inside the
`example directory <https://github.com/us-irs/cfdp-py/tree/main/examples/cfdp-simple>`_
which shows an end-to-end file transfer on a host computer. This should give you a general idea of
how the source and destination handler work in practice.

There is also a
`test application <https://github.com/robamu-org/tmtccmd/tree/main/examples/cfdp-libre-cube-crosstest>`_
which cross-tests the `cfdp-py` CFDP implementation with the
`Libre Cube CFDP <https://gitlab.com/librecube/lib/python-cfdp>`_ implementation.

Finally, you can see a more complex example also featuring more features of the CFDP state machines
`here <https://github.com/us-irs/cfdp-py/tree/main/examples/cfdp-cli-udp>`_. This example
uses UDP servers for communication and explicitely separates the local and remote entity
application.
