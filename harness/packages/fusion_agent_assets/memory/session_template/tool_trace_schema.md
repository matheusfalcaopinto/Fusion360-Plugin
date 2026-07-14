# Tool Trace Schema

Implementation should save raw traces as JSONL. This Markdown describes the fields.

```json
{
  "timestamp": "2026-06-19T00:00:00Z",
  "session_id": "...",
  "transaction_id": "...",
  "facade_tool": "create_named_parameter",
  "native_tool": "native_tool_name_or_null",
  "arguments_redacted": {},
  "result_status": "ok",
  "duration_ms": 100,
  "error_code": null,
  "notes": ""
}
```
