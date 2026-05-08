import { Handle, Position } from "reactflow";
import type { GraphNodeData } from "../types";

export default function ToolNode({ data }: { data: GraphNodeData }) {
  return (
    <div className="min-w-[160px] rounded-lg border border-blue-700 bg-neutral-900 px-3 py-2">
      <Handle type="source" position={Position.Right} className="!bg-blue-500" />
      <Handle type="target" position={Position.Left} className="!bg-blue-500" />
      <div className="text-sm font-semibold text-blue-300">{data.label}</div>
      <div className="text-[11px] text-neutral-400">tool</div>
    </div>
  );
}
