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
  /** Stream-prefixed edge id (`{node}::{stream}::{seq}`) to flash. */
  highlightedEdge?: string | null;
}

export default function CallGraphCanvas({ state, highlightedEdge }: Props) {
  const nodes = useMemo<Node[]>(() => {
    return layoutNodes(state.nodes).map((n) => ({
      id: n.id,
      type: n.role,
      position: { x: n.x, y: n.y },
      data: n,
    }));
  }, [state.nodes]);

  const edges = useMemo<Edge[]>(() => {
    return Object.values(state.edges).map((e) => {
      const highlighted = highlightedEdge === e.id;
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        animated: e.active || highlighted,
        label: e.active
          ? "…"
          : e.latencyMs !== undefined
            ? `${Math.round(e.latencyMs)} ms`
            : undefined,
        labelStyle: { fill: "#6b7280", fontFamily: "inherit", fontSize: 10 },
        labelBgStyle: { fill: "#0b0e10", fillOpacity: 0.9 },
        style: highlighted
          ? { stroke: "#22d3ee", strokeWidth: 3 }
          : e.active
            ? { stroke: "#22c55e", strokeWidth: 2 }
            : { stroke: "#374151", strokeWidth: 1 },
      };
    });
  }, [state.edges, highlightedEdge]);

  return (
    <div className="h-full w-full bg-term-bg">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1d2329" gap={24} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
