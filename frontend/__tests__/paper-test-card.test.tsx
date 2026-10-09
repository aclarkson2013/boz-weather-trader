import { render, screen, within } from "@testing-library/react";

import PaperTestCard, {
  daysSince,
  shutOffProximity,
} from "@/components/paper-test-card";
import type { PaperStrategySummary } from "@/lib/types";

vi.mock("lucide-react", () => ({
  FlaskConical: () => <span data-testid="flask-icon" />,
}));

const base = (
  overrides?: Partial<PaperStrategySummary>,
): PaperStrategySummary => ({
  strategy_id: "B5",
  status: "active",
  started_at: "2026-10-01T00:00:00",
  stopped_at: null,
  stop_reason: null,
  decisions: 30,
  trades_open: 2,
  trades_settled: 20,
  traded_city_days: 15,
  contracts: 40,
  pnl_cents: 123,
  wins: 12,
  infeasible_fills: 0,
  sprt_llr: 0.4,
  sprt_stop: -1.558,
  sprt_edge: 2.773,
  cusum_cents: 100,
  cusum_alarm_cents: 1000,
  ...overrides,
});

const NOW = new Date("2026-10-11T00:00:00Z");

describe("PaperTestCard", () => {
  it("explains the test in plain language", () => {
    render(<PaperTestCard strategies={[base()]} now={NOW} />);
    expect(screen.getByText("Paper Test")).toBeInTheDocument();
    expect(screen.getByText(/no real money/i)).toBeInTheDocument();
  });

  it("shows pretend P&L, trades, win rate, days and verdict progress", () => {
    render(<PaperTestCard strategies={[base()]} now={NOW} />);
    const card = within(screen.getByTestId("paper-B5"));
    expect(card.getByText("Running")).toBeInTheDocument();
    expect(card.getByText("+$1.23")).toBeInTheDocument();
    expect(card.getByText("20 settled · 2 open")).toBeInTheDocument();
    expect(card.getByText("60%")).toBeInTheDocument();
    expect(card.getByText("10")).toBeInTheDocument(); // days running
    expect(card.getByText(/15 \/ 300 trading days/)).toBeInTheDocument();
    expect(card.getByText(/Shut-off check: safe/)).toBeInTheDocument();
  });

  it("shows a stopped strategy and its reason", () => {
    render(
      <PaperTestCard
        strategies={[
          base({
            status: "stopped",
            stop_reason: "SPRT stop boundary crossed (LLR -1.80)",
            pnl_cents: -900,
          }),
        ]}
        now={NOW}
      />,
    );
    const card = within(screen.getByTestId("paper-B5"));
    expect(card.getByText("Stopped")).toBeInTheDocument();
    expect(card.getByText(/Stopped: SPRT stop boundary/)).toBeInTheDocument();
    expect(card.getByText("-$9.00")).toBeInTheDocument();
  });

  it("shows not-started strategies without fake numbers", () => {
    render(
      <PaperTestCard
        strategies={[
          base({
            strategy_id: "L5",
            status: "not_started",
            started_at: null,
            trades_settled: 0,
            wins: 0,
            pnl_cents: 0,
          }),
        ]}
        now={NOW}
      />,
    );
    const card = within(screen.getByTestId("paper-L5"));
    expect(card.getByText("Not started")).toBeInTheDocument();
    expect(card.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("warns when pretend fills exceeded the order book", () => {
    render(
      <PaperTestCard strategies={[base({ infeasible_fills: 3 })]} now={NOW} />,
    );
    expect(
      screen.getByText(/3 pretend fill\(s\) were larger than the order book/),
    ).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("daysSince handles naive UTC timestamps and nulls", () => {
    expect(daysSince("2026-10-01T00:00:00", NOW)).toBe(10);
    expect(daysSince(null, NOW)).toBe(0);
  });

  it("shutOffProximity takes the closer of SPRT and CUSUM", () => {
    expect(
      shutOffProximity(base({ sprt_llr: -0.779, cusum_cents: 100 })),
    ).toBeCloseTo(0.5, 2);
    expect(
      shutOffProximity(base({ sprt_llr: 1.0, cusum_cents: 800 })),
    ).toBeCloseTo(0.8, 5);
    expect(shutOffProximity(base({ sprt_llr: null, cusum_cents: null }))).toBe(
      0,
    );
    expect(shutOffProximity(base({ cusum_cents: 5000 }))).toBe(1);
  });
});
