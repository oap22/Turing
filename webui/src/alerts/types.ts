// Alert types for the hardware-safety SPA banner (#229 / #230 / #232).
//
// Schema mirrors the Python `Alert` dataclass; the golden frame fixture at
// `tests/test_coordinator/fixtures/alert_frames_golden.json` is consumed by
// both sides so drift between them is caught on either Py or TS CI.

export type AlertField = "temp_celsius" | "disk_pct";
export type AlertSeverity = "warn" | "danger";
export type AlertEventState = "alerting" | "cleared";

export interface Alert {
  node_id: string;
  node_name: string;
  field: AlertField;
  severity: AlertSeverity;
  value: number;
  threshold: number;
  state: AlertEventState;
  fired_at_ms: number;
  // Epoch-ms expiry of an operator snooze, or null when not snoozed.
  // Set on a snooze `update` frame; null on a normal alerting/cleared edge.
  snoozed_until_ms: number | null;
}

export interface AlertFrame extends Alert {
  type: "alert";
}
