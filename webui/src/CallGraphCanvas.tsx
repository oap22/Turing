import ReactFlow, { Background, Controls } from "reactflow";
import "reactflow/dist/style.css";

/**
 * Placeholder canvas for slice 6 (layered call-graph). This component
 * proves React Flow imports without error and renders an empty graph;
 * slice 6 fills in nodes, edges, and the latency-pulsed renderer.
 */
export default function CallGraphCanvas() {
  return (
    <div className="h-full w-full">
      <ReactFlow nodes={[]} edges={[]} fitView>
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  );
}
