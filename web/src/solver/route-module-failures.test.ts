/** Activity-only module shortfalls that keep every strict failure strict. */
import { describe, expect, it } from "vitest";
import failures from "./fixtures/route-module-failures.json";
import { calculateMinimumNewCredits } from "./route";
import {
  creditsOf,
  loadStructure,
  selectedGroupIds,
  type RouteFixtures,
} from "./route-test-support";

const fixtures = failures as unknown as RouteFixtures;
const prefix =
  "active_module_shortfall_is_activity_only_and_preserves_strict_failures";

describe("route module failures", () => {
  it("active module shortfall is activity only and preserves strict failures", () => {
    const allCourses = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.all_courses`),
      [],
      "5",
    );
    expect(allCourses.state).toBe("exact");
    expect(selectedGroupIds(allCourses)).toEqual(["active-a", "inactive-b"]);
    expect(creditsOf(allCourses.total_new_credits)).toBe(1200);
    expect(allCourses.shortfalls).toEqual([]);

    const ordinary = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.ordinary`),
      [],
      "5",
    );
    expect(ordinary.state).toBe("no_solution");
    expect(ordinary.shortfalls).toEqual([]);

    const impossible = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.impossible`),
      [],
      "5",
    );
    expect(impossible.state).toBe("no_solution");
    expect(impossible.shortfalls).toEqual([]);

    const tooHigh = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.too_high`),
      [],
      "5",
    );
    expect(tooHigh.state).toBe("no_solution");
    expect(tooHigh.shortfalls).toEqual([]);

    const withUnresolved = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.module_with_unresolved`),
      [],
      "5",
    );
    expect(withUnresolved.state).toBe("unresolved");
    expect(withUnresolved.shortfalls).toEqual([]);

    const cyclic = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.cyclic`),
      [],
      "5",
    );
    expect(cyclic.state).toBe("unresolved");
    expect(cyclic.shortfalls).toEqual([]);

    const nestedOrdinary = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.nested_ordinary`),
      [],
      "5",
    );
    expect(nestedOrdinary.state).toBe("no_solution");
    expect(nestedOrdinary.shortfalls).toEqual([]);

    const numeric = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.numeric_compilation`),
      [],
      "5",
    );
    expect(numeric.state).toBe("no_solution");
    expect(numeric.shortfalls).toEqual([]);

    const outerCredit = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.outer_credit`),
      [],
      "5",
    );
    expect(outerCredit.state).toBe("no_solution");
    expect(outerCredit.shortfalls).toEqual([]);

    const exactWins = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.exact_wins`),
      [],
      "5",
    );
    expect(exactWins.state).toBe("exact");
    expect(selectedGroupIds(exactWins)).toEqual(["active-a"]);
    expect(exactWins.shortfalls).toEqual([]);

    const excluded = calculateMinimumNewCredits(
      loadStructure(fixtures, `${prefix}.excluded`),
      [],
      "5",
    );
    expect(excluded.state).toBe("no_solution");
    expect(excluded.shortfalls).toEqual([]);
    expect(excluded.blocked_excluded_group_ids).toEqual(["module-unit"]);
  });
});
