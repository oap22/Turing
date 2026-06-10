// Observability view — the watching-the-fleet third of the tabbed console
// (issue #358). Composes the three read-only surfaces that belong together:
// the call graph (dominant), the message-trace log (side column, because
// selecting a trace event highlights the matching graph edge), and the fleet
// specs strip (compact, under the graph). All state lives in App.tsx so the
// view can unmount while hidden without dropping frames.

import { memo } from "react";
import CallGraphCanvas from "./CallGraphCanvas";
import type { GraphState } from "./graph/types";
import SpecsGrid from "./specs/SpecsGrid";
import type { PeerSpecsRow } from "./specs/types";
import TracePane from "./trace/TracePane";
import type { TraceEvent } from "./trace/types";

interface Props {
  graphState: GraphState;
  highlightedEdge: string | null;
  specsRows: PeerSpecsRow[];
  liveTrace: TraceEvent[];
  onTraceSelect: (e: TraceEvent) => void;
}

const MemoCallGraphCanvas = memo(CallGraphCanvas);
const MemoSpecsGrid = memo(SpecsGrid);
const MemoTracePane = memo(TracePane);

export default function ObservabilityView({
  graphState,
  highlightedEdge,
  specsRows,
  liveTrace,
  onTraceSelect,
}: Props) {
  return (
    <div data-testid="observability-view" className="flex h-full">
      <section
        aria-label="Fleet graph and specs"
        className="flex min-w-0 flex-1 flex-col border-r border-term-edge"
      >
        <div className="flex-1 overflow-hidden">
          <MemoCallGraphCanvas
            state={graphState}
            highlightedEdge={highlightedEdge}
          />
        </div>
        <MemoSpecsGrid rows={specsRows} />
      </section>
      <aside
        aria-label="Message trace"
        className="w-[420px] shrink-0 border-r border-term-edge"
      >
        <MemoTracePane liveEvents={liveTrace} onSelect={onTraceSelect} />
      </aside>
    </div>
  );
}
