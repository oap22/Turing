// Pure formatting for the cold-path errors TermPane can surface. The pane
// itself owns xterm/Tauri objects and stays outside the jsdom component suite;
// this seam keeps the user-facing error contract testable.

export function formatTerminalStartupError(stage: string, error: unknown): string {
  let detail: string;
  if (error instanceof Error && error.message) detail = error.message;
  else if (typeof error === "string" && error) detail = error;
  else {
    try {
      detail = JSON.stringify(error) || String(error);
    } catch {
      detail = String(error);
    }
  }
  return `terminal ${stage} failed: ${detail}`;
}
