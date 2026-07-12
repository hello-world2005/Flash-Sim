# Design

- PCIe uses 4 B/ns, 128 B maximum payload, 28 B TLP overhead, explicit read payload DMA, and CQ delivery after payload completion.
- A finite CMT discards clean victims. Dirty victims remain in GMT only while their mapping-page write is outstanding.
- Host misses to an in-flight MVPN join one mapping read; physical mapping pages remain addressed by GTD.
- MQSim mapping writes must be selected by both out-of-order TSUs. Mapping read-modify-write dependencies are bidirectional so read completion makes the write ready.
- Validation uses identical 64 B sectors, separates warmup and measured requests, and archives one authoritative 20k Exchange read result.
