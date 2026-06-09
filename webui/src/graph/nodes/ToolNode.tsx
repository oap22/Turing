import { Handle, Position } from "reactflow";
import type { GraphNodeData } from "../types";

export default function ToolNode({ data }: { data: GraphNodeData }) {
  return (
    <div className="min-w-[160px] border border-cyan-700 bg-term-panel px-3 py-2 font-mono">
      <Handle type="source" position={Position.Right} className="!bg-cyan-500" />
      <Handle type="target" position={Position.Left} className="!bg-cyan-500" />
      <div className="text-sm font-semibold text-cyan-300">{data.label}</div>
      <div className="text-[10px] uppercase tracking-widest text-term-dim">tool</div>
    </div>
  );
}
