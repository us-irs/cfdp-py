Change Log
=======

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

# [unreleased]

# [v0.5.0] 2025-01-17

## Added

- Added `RestrictedFilestore` to limit the file access of the `NativeFilestore` to a specific
  directory.
- `Filestore` creates a directory if it does not exist when creating a new file.

## Fixed

- Correction for `InvalidDestinationId` exception arguments in destination handler.
- Destination handler now only checks entity ID values when checking inserted packets.
- Source handler used an incorrect check if the file exists without the virtual filestore.
- Source handler opened files without the virtual filestore

# [v0.4.0] 2024-11-08

## Added

- `progress` property for both source and destination handler to track the progress of a
  transaction.
- `file_size` property for both destination and source handler.
- `get_pdu_request` getter function to retrieve the active Put Request for the source handler.

# [v0.3.0] 2024-10-15

## Changed

- Simplified state machine usage: Packets are now inserted using an optional `packet` argument
  of the `state_machine` call.
- Removed some of the visible intermedia transaction steps. For example, instead of remaining
  on `TransactionStep.SENDING_FINISHED_PDU`, the destination handler will still generate the
  Finished PDU but jump to the next step immediately without requiring another state machine call.
  For the source handler, the same was done for the `TransactionStep.SENDING_EOF` step.

## Removed

- `insert_packet` API of the source and destination handler. Packet insertion is now performed
  using the `state_machine` call.

## Fixed

- Fault location field of the Finished PDU is now set correctly for transfer cancellations.

# [v0.2.0] 2024-08-27

## Fixed

- The large file flag was not set properly in the source handler for large file transfers.
- The CRC algorithms will now be used for empty files as well instead of hardcoding the
  checksum type to the NULL checksum. This was a bug which did not show directly for
  checksums like CRC32 because those have an initial value of 0x0

## Changed

- Added `file_size` abstract method to `VirtualFilestore`
- Renamed `HostFilestore` to `NativeFilestore`, but keep old name alias for backwards compatibility.
- Added `calculate_checksum` and `verify_checksum` to `VirtualFilestore` interface.

# [v0.1.2] 2024-06-04

Updated documentation configuration to include a `spacepackets` docs mapping. This
should fix references to the `spacepackets` documentation.

# [v0.1.1] 2024-04-23

- Allow `spacepackets` range from v0.23 to < v0.25

# [v0.1.0]

Initial release of the `cfdp-py` library which was split off the
[tmtccmd library](https://github.com/robamu-org/tmtccmd).
