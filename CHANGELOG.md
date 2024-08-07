Change Log
=======

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

# [unreleased]

# [v0.2.0] 2024-06-28

## Fixed

- The large file flag was not set properly in the source handler for large file transfers.

## Changed

- Added `file_size` abstract method to `VirtualFilestore`
- Renamed `HostFilestore` to `NativeFilestore`, but keep old name alias for backwards compatibility.

# [v0.1.2] 2024-06-04

Updated documentation configuration to include a `spacepackets` docs mapping. This
should fix references to the `spacepackets` documentation.

# [v0.1.1] 2024-04-23

- Allow `spacepackets` range from v0.23 to < v0.25

# [v0.1.0]

Initial release of the `cfdp-py` library which was split off the
[tmtccmd library](https://github.com/robamu-org/tmtccmd).
