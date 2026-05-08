import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  type Edge,
  type Node,
} from "reactflow";
import "reactflow/dist/style.css";
import LlmNode from "./graph/nodes/LlmNode";
import MemoryNode from "./graph/nodes/MemoryNode";
import PiNode from "./graph/nodes/PiNode";
import ToolNode from "./graph/nodes/ToolNode";
import { layoutNodes } from "./graph/layout";
import type { GraphState } from "./graph/types";

const nodeTypes = {
  pi: PiNode,
  tool: ToolNode,
  memory: MemoryNode,
  llm: LlmNode,
};

interface Props {
  state: GraphState;
}

export default function CallGraphCanvas({ state }: Props) {
  const nodes = useMemo<Node[]>(() => {
    return layoutNodes(state.nodes).map((n) => ({
      id: n.id,
      type: n.role,
      position: { x: n.x, y: n.y },
      data: n,
    }));
  }, [state.nodes]);

  const edges = useMemo<Edge[]>(() => {
    return Object.values(state.edges).map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      animated: e.active,
      label: e.active
        ? "…"
        : e.latencyMs !== undefined
          ? `${Math.round(e.latencyMs)} ms`
          : undefined,
      style: e.active
        ? { stroke: "#22c55e", strokeWidth: 2 }
        : { stroke: "#525252", strokeWidth: 1 },
    }));
  }, [state.edges]);

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  );
}
