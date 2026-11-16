/**
 * Evaluate the drawn box states from the held graph report and the live profile inputs.
 *
 * The cached report is never mutated: only the three live calculation sets are replaced on a
 * shallow copy, and every native table is shared by reference.
 */
import type {
  PrerequisiteGraphReport,
  PrerequisitesRequest,
} from "../../../api/types";
import {
  evaluateNodeStates,
  type NodeStateEvaluation,
} from "../../../solver/satisfaction";
import { withLiveInputs } from "../../../solver/structure";

/** Return the drawn box states and assumed-credit flag of one report under the live inputs. */
export function evaluateGraphNodeStates(
  report: PrerequisiteGraphReport,
  request: PrerequisitesRequest,
): NodeStateEvaluation {
  const display = report.display;
  if (display === null) return { states: [], uses_assumed_credits: false };
  const { structure, basis_group_ids: basis } = withLiveInputs(
    report.solver,
    request,
  );
  return evaluateNodeStates(
    structure,
    basis,
    display.nodes,
    request.completed_codes,
    report.assumed_course_credits,
  );
}
