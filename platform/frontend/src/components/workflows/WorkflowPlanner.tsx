// This file is part of Prometheus
// https://inl.gov/super-intelligence/prometheus/
//
// Copyright 2026 Battelle Energy Alliance, LLC
// All rights reserved, see NOTICE.txt for full restrictions
// https://github.com/idaholab/prometheus_core/blob/main/NOTICE.txt
//
// Licensed under the MIT License, please see LICENSE for details
// https://github.com/idaholab/prometheus_core/blob/main/LICENSE
//
// SPDX-License-Identifier: MIT

import { Button } from "../ui/Button";
import { SectionLabel } from "../ui/SectionLabel";
import { WorkflowGraph, type WorkflowGraphNode, type WorkflowGraphWire } from "./WorkflowGraph";

export interface WorkflowPlan {
  /** Breadcrumb above the graph, e.g. ["Chat", "Workflow Plan", "comprehensive_material_analysis"]. */
  path: string[];
  /** Orchestrator state shown opposite the path, e.g. "Orchestrator: Active | Mode: DAG". */
  status: string;
  nodes: WorkflowGraphNode[];
  wires: WorkflowGraphWire[];
  agents: number;
  /** Display string, e.g. "3hr 40s". */
  pipelineTime: string;
  /** Display string, e.g. "2 Active". */
  checkpoints: string;
  inputs: string[];
  outputs: string[];
}

/** Sizes go on each element: the base layer sets span and li font-size, so it isn't inherited. */
function FileList({ label, files }: { label: string; files: string[] }) {
  return (
    <div className="flex flex-col gap-2">
      <SectionLabel as="h3">{label}</SectionLabel>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {files.map((file) => (
          <li key={file} className="break-all text-[11px] leading-4 text-base-content">
            • {file}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Workflow Execution Planner (Figma D.1): the plan's graph on the left and its
 * summary and actions on the right. Side by side from 1024px; below that they
 * stack. Until the graph column is wide enough (xl), the graph keeps a readable
 * size and scrolls sideways in its own area.
 * Display only: the buttons have no handlers yet.
 */
export function WorkflowPlanner({ plan }: { plan: WorkflowPlan }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-base-content/10 bg-base-200 lg:flex-row">
      <div className="flex min-w-0 flex-1 flex-col gap-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 uppercase">
          {/* Segments wrap at the slashes, never mid-word. */}
          <span className="flex min-w-0 flex-wrap gap-x-1.5">
            {plan.path.map((segment, i) => (
              <span key={segment} className="break-all text-[10px] leading-3 tracking-[0.1em] text-primary">
                {i > 0 && "/ "}
                {segment}
              </span>
            ))}
          </span>
          <span className="text-[10px] leading-3 text-base-content/50">{plan.status}</span>
        </div>
        {/* Below xl the graph column is narrower than D.1's, so the graph keeps
            its size and scrolls sideways rather than shrinking to fit. */}
        <div className="overflow-x-auto">
          <WorkflowGraph
            nodes={plan.nodes}
            wires={plan.wires}
            className="h-[628px] w-full min-w-[772px] xl:min-w-0"
          />
        </div>
      </div>

      <aside className="flex shrink-0 flex-col gap-5 border-t border-base-content/10 p-6 lg:w-80 lg:border-l lg:border-t-0 xl:w-[340px]">
        <div className="flex flex-col gap-2.5">
          <SectionLabel as="h3">Plan Summary</SectionLabel>
          <dl className="m-0 flex flex-col gap-2 text-[13px]">
            <div className="flex justify-between gap-2">
              <dt className="text-base-content/60">Total Agents Involved</dt>
              <dd className="m-0 text-base-content">
                {plan.agents} {plan.agents === 1 ? "Agent" : "Agents"}
              </dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-base-content/60">Pipeline Time</dt>
              <dd className="m-0 text-primary">{plan.pipelineTime}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-base-content/60">Checkpoints</dt>
              <dd className="m-0 text-primary">{plan.checkpoints}</dd>
            </div>
          </dl>
        </div>
        <FileList label="Input Requirements" files={plan.inputs} />
        <FileList label="Expected Outputs" files={plan.outputs} />
        <div className="flex flex-col gap-2.5 pt-5">
          <Button label="Approve & Execute Pipeline" variant="primary" wide />
          {/* TODO(WB-1): opens the Builder. */}
          <Button label="Modify Plan" outline wide />
        </div>
      </aside>
    </div>
  );
}
