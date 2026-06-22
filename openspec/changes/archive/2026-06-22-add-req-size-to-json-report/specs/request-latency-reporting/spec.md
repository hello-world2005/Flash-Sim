## ADDED Requirements

### Requirement: JSON request reports include request size

Each JSON request latency record SHALL include a top-level `size` field whose value matches the original input `Request.size` for that request.

#### Scenario: Generated JSON preserves input request sizes

- **WHEN** an event-driven simulation completes for a trace containing requests with `size` values
- **THEN** each entry in the generated JSON report's `requests` array MUST include `size` equal to the corresponding input trace request size
