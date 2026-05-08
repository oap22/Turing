import { Handle, Position } from "reactflow";
import type { GraphNodeData } from "../types";

/** Custom React Flow node: a Pi in the cluster. */
export default function PiNode({ data }: { data: GraphNodeData }) {
  const stale = data.stale ? "opacity-40 grayscale" : "";
  return (
    <div
      className={`min-w-[200px] rounded-lg border border-emerald-700 bg-neutral-900 px-3 py-2 ${stale}`}
    >
      <Handle type="source" position={Position.Right} className="!bg-emerald-500" />
      <Handle type="target" position={Position.Left} className="!bg-emerald-500" />
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-emerald-400">{data.label}</div>
        {data.droppedCount && data.droppedCount > 0 ? (
          <span
            className="rounded-full border border-amber-700 bg-amber-950 px-2 py-0.5 text-[10px] text-amber-300"
            title="events reported missing in the visible window"
          >
            ⚠ {data.droppedCount} dropped
          </span>
        ) : null}
      </div>
      <div className="mt-1 flex justify-between gap-4 text-[11px] text-neutral-400">
        <span>in-flight: {data.inFlight ?? 0}</span>
        <span>{data.currentProvider ?? "—"}</span>
      </div>
    </div>
  );
}
