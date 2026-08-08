// The task graph, laid out deterministically.
//
// This module is the *only* thing that imports `@xyflow/react` and
// `@dagrejs/dagre`, and it is loaded through `React.lazy` from `GroupingTab`,
// so the two land in their own chunk and never enter the main bundle. Every
// other route stays as cheap to load as it was before this tab existed.
//
// Layout is dagre's layered algorithm with a fixed rank direction and no
// randomness: two operators opening the same shared link must see the same
// picture, or "the node on the left" stops being a thing anyone can say. Node
// order is sorted before it reaches dagre for the same reason — object key
// order is stable in practice but not something to build a shared link on.

import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { GroupingView } from "../../types";
import type { Palette, StageFrame } from "./stages";
import { edgeKey } from "./stages";

const NODE_WIDTH = 210;
const NODE_HEIGHT = 46;

export interface GroupingGraphProps {
  view: GroupingView;
  frame: StageFrame;
  palette: Palette;
  paletteSize: number;
  selectedEdge: string | null;
  onSelectEdge: (key: string | null) => void;
  onSelectNode: (node: string | null) => void;
  selectedNode: string | null;
}

export default function GroupingGraph({
  view,
  frame,
  palette,
  paletteSize,
  selectedEdge,
  onSelectEdge,
  onSelectNode,
  selectedNode,
}: GroupingGraphProps) {
  const { nodes, edges } = useMemo(
    () => layout(view, frame, palette, paletteSize, selectedEdge, selectedNode),
    [view, frame, palette, paletteSize, selectedEdge, selectedNode],
  );

  return (
    <div className="grouping-graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        // Deterministic on purpose: no auto-arrange, no physics, no jitter.
        proOptions={{ hideAttribution: false }}
        onNodeClick={(_, node) => onSelectNode(node.id === selectedNode ? null : node.id)}
        onEdgeClick={(_, edge) => onSelectEdge(edge.id === selectedEdge ? null : edge.id)}
        onPaneClick={() => {
          onSelectEdge(null);
          onSelectNode(null);
        }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function layout(
  view: GroupingView,
  frame: StageFrame,
  palette: Palette,
  paletteSize: number,
  selectedEdge: string | null,
  selectedNode: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 26, ranksep: 90, marginx: 16, marginy: 16 });

  // Sorted, so the layout is a pure function of the trace's content rather than
  // of the order its keys happened to serialize in.
  const ids = Object.keys(frame.partition).sort();
  for (const id of ids) {
    graph.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }

  const dependencies = view.input_graph?.dependencies ?? [];
  const affinity = view.input_graph?.affinity ?? [];
  for (const [from, to] of dependencies) {
    if (frame.partition[from] !== undefined && frame.partition[to] !== undefined) {
      graph.setEdge(from, to);
    }
  }
  dagre.layout(graph);

  const nodes: Node[] = ids.map((id) => {
    const position = graph.node(id);
    const groupId = frame.partition[id];
    const hue = palette.get(groupId) ?? 0;
    return {
      id,
      position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 },
      data: { label: `${id}` },
      draggable: false,
      className: [
        "grouping-node",
        `grouping-node--hue-${hue % paletteSize}`,
        // The recolour set: what this stage actually changed. Marked rather
        // than recoloured into a warning hue — the node did not go wrong, it
        // moved.
        frame.moved.has(id) ? "grouping-node--moved" : "",
        selectedNode === id ? "grouping-node--selected" : "",
      ]
        .filter(Boolean)
        .join(" "),
      style: { width: NODE_WIDTH, height: NODE_HEIGHT },
    };
  });

  // Dependencies are drawn as arrows (they carry direction and are what the
  // layout ranks on); affinity edges are drawn thin and undirected, because
  // affinity is what Louvain clustered on and cross-group affinity is the
  // interesting failure — a heavy edge the partition cut.
  const dependencyEdges: Edge[] = dependencies
    .filter(([from, to]) => frame.partition[from] !== undefined && frame.partition[to] !== undefined)
    .map(([from, to, weight]) => ({
      id: edgeKey(from, to),
      source: from,
      target: to,
      animated: false,
      label: weight ? weight.toFixed(1) : undefined,
      className: `grouping-edge grouping-edge--dependency${
        selectedEdge === edgeKey(from, to) ? " grouping-edge--selected" : ""
      }`,
    }));

  const seen = new Set(dependencyEdges.map((edge) => edge.id));
  const affinityEdges: Edge[] = affinity
    .filter(([from, to]) => frame.partition[from] !== undefined && frame.partition[to] !== undefined)
    .filter(([from, to]) => !seen.has(edgeKey(from, to)))
    .map(([from, to, weight]) => {
      const cut = frame.partition[from] !== frame.partition[to];
      return {
        id: edgeKey(from, to),
        source: from,
        target: to,
        label: weight ? weight.toFixed(1) : undefined,
        className: [
          "grouping-edge",
          "grouping-edge--affinity",
          cut ? "grouping-edge--cut" : "",
          selectedEdge === edgeKey(from, to) ? "grouping-edge--selected" : "",
        ]
          .filter(Boolean)
          .join(" "),
      };
    });

  return { nodes, edges: [...dependencyEdges, ...affinityEdges] };
}
