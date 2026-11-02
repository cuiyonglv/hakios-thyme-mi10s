# SHA-256 implementation

`sha256.c` and `sha256.h` are by Brad Conte, from
[B-Con/crypto-algorithms](https://github.com/B-Con/crypto-algorithms/tree/cfbde48414baacf51fc7c74f275190881f037d32),
revision `cfbde48414baacf51fc7c74f275190881f037d32`.
The author's README releases this code into the public domain and requests
acknowledgement. Original source notices have been retained.

Local adjustments remove unused hosted-library includes, declare the loader's
memset, cast bytes to unsigned words before shifts, and use size_t for the
update-loop index. Tests compare streaming hashes to Python hashlib, including
binary data, block boundaries and a million-byte input.

This digest detects damaged installation bytes; it is not a signed-boot or
publisher-authentication mechanism. Firmware execution timing remains untested.
