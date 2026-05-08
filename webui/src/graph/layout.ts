/**
 * Fixed-column layout for the layered call-graph.
 *
 * Three columns laid out left to right:
 *   col 0 — Pi-nodes      (one slot per node, fixed muscle-memory positions)
 *   col 1 — tools + memory stores (auto-arranged inside the column)
 *   col 2 — LLM providers (Claude, Ollama, …)
 *
 * Pi-node slot order is deterministic — assigned by name on first sight and
 * preserved for the lifetime of the page so the operator's eye builds muscle
 * memory for "pi-alpha is always top-left."
 */

import type { GraphNodeData, NodeRole } from "./types";

const COL_WIDTH = 320;
const ROW_HEIGHT = 110;
const COL_X: Record<NodeRole, number> = {
  pi: 0,
  tool: COL_WIDTH,
  memory: COL_WIDTH,
  llm: COL_WIDTH * 2,
};

export interface PositionedNode extends GraphNodeData {
  x: number;
  y: number;
}

/**
 * Assign (x, y) to every node. Pi-nodes use the persistent `slot` index for
 * their row; the other columns auto-stack in alphabetical order so renames
 * don't shuffle the visible order randomly.
 */
export function layoutNodes(
  nodes: Record<string, GraphNodeData>,
): PositionedNode[] {
  const byRole: Record<NodeRole, GraphNodeData[]> = {
    pi: [],
    tool: [],
    memory: [],
    llm: [],
  };
  for (const node of Object.values(nodes)) {
    byRole[node.role].push(node);
  }
  byRole.pi.sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0));
  byRole.tool.sort((a, b) => a.label.localeCompare(b.label));
  byRole.memory.sort((a, b) => a.label.localeCompare(b.label));
  byRole.llm.sort((a, b) => a.label.localeCompare(b.label));

  const positioned: PositionedNode[] = [];
  let toolRow = 0;
  let memoryRow = 0;
  for (const role of ["pi", "tool", "memory", "llm"] as NodeRole[]) {
    for (const node of byRole[role]) {
      let row: number;
      if (role === "pi") {
        row = node.slot ?? 0;
      } else if (role === "tool") {
        row = toolRow++;
      } else if (role === "memory") {
        // memory stores stack below tools in the middle column
        row = byRole.tool.length + memoryRow++;
      } else {
        // llm column — alphabetical, top-down
        row = positioned
          .filter((p) => p.role === "llm")
          .length;
      }
      positioned.push({
        ...node,
        x: COL_X[role],
        y: row * ROW_HEIGHT,
      });
    }
  }
  return positioned;
}
