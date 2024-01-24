CFDP Simple File Copy Example
===========

This example shows an end-to-end file transfer on a host computer. This should give you a general
idea of how the source and destination handler work in practice. Simply running the script with


```sh
./file-copy-example.py
```

will perform an acknowledged transfer of a file on the same host system.
You can also perform an unacknowledged transfer using

```sh
./file-copy-example.py -t nak
```

It is also possible to supply the ``-v`` verbosity argument to the application to print all
packets being exchanged between the source and destination handler.
