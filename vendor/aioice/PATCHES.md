# Local patches

- Upstream: <https://github.com/aiortc/aioice>, tag [`0.10.2`](https://github.com/aiortc/aioice/tree/0.10.2).
- `0.10.2+uutun1`: Guard the STUN transaction retry timer against an already-completed future to prevent retransmission and `InvalidStateError` races.
