import { Handle, Position } from "reactflow";
import type { GraphNodeData } from "../types";

export default function LlmNode({ data }: { data: GraphNodeData }) {
  return (
    <div className="min-w-[180px] border border-fuchsia-700 bg-term-panel px-3 py-2 font-mono">
      <Handle type="target" position={Position.Left} className="!bg-fuchsia-500" />
      <div className="text-sm font-semibold text-fuchsia-300">{data.label}</div>
      <div className="text-[11px] text-term-dim">
        avg {Math.round(data.rollingAvgMs ?? 0)} ms / 60s
      </div>
    </div>
  );
}
