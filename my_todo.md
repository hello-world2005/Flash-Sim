# TODO

## Compute scheduling follow-up

- [ ] Support concurrent compute requests from different `source_req` values within one die. The initial implementation will conservatively require all compute transactions dispatched together on a die to share the same `source_req` and `selected_wl`. A future design may relax only the request constraint by keeping `selected_wl` uniform at die scope while isolating each request's accumulation/output within independent plane BL domains.

