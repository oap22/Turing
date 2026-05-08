import { Handle, Position } from "reactflow";
import type { GraphNodeData } from "../types";

export default function MemoryNode({ data }: { data: GraphNodeData }) {
  return (
    <div className="min-w-[160px] rounded-lg border border-amber-700 bg-neutral-900 px-3 py-2">
      <Handle type="source" position={Position.Right} className="!bg-amber-500" />
      <Handle type="target" position={Position.Left} className="!bg-amber-500" />
      <div className="text-sm font-semibold text-amber-300">{data.label}</div>
      <div className="text-[11px] text-neutral-400">memory</div>
    </div>
  );
}
