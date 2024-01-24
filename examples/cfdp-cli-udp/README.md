CFDP CLI Example with UDP servers
=============

This is a more complex example which also shows more features of the CFDP state machines.

It has the following features:

- Uses UDP as the underlying communication protocol
- Remote and local entities are distinct application, which makes this a bit of a more realistic
  use case.
- The local application exposes a minimal CLI interface to start both normal Put Requests and
  CFDP proxy put request to request files from the remote side.

Here, the remote and local entities are distinct applications which both spawn UDP servers.
The local entity will use the port 5111 while the remote entity will use the port 5222.
This allows running both entities on the same computer.

For example, you can run both `remote.py` and `local.py`. This will not do much because the local
entity will not initiate a put request for this command.

If you want to sent a file from the local application to the remote application, you can use
the following commands:

```sh
echo "Hello World!" > files/local/hello.txt
./local.py files/local/hello.txt files/remote
```

You can see the different indication steps for both the remote and local entity in the terminal.
You can check that the file `files/remote/hello.txt` now exists with the correct content.

After that, you can try a proxy put request to instruct the remote entity to send the file
back to the local entity using the following command:

```sh
./local.py -p files/remote/hello.txt files/local/hello-sent-back.txt
```

You can also run the remote application on a different computer, for example a Raspberry Pi.
Assuming that both computers are in the same network and the Raspberry Pi has the example address
`192.168.55.55`, you can create a configuration file `local_cfg.json` with the following
content:

```sh
{
    "remote_addr": "192.168.55.55"
}
```

The local entity application will use this address instead of the localhost address.

## Notes on example structure

This application also shows a possible modular approach with re-usability in mind. Both the
source, destination entity and the UDP server are based on the same classes. The configuration
of the CFDP is also in large parts re-used and provided inside the `common` module.
