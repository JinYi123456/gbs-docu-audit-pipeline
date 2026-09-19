import Dashboard from "@/components/Dashboard";
import { loadDashboard } from "@/lib/data";

export const dynamic = "force-dynamic";

export default function Page() {
  const { dashboard, scoreboard } = loadDashboard();

  if (!dashboard) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-20">
        <h1 className="text-lg font-semibold text-slate-900">
          Verification snapshot not available yet
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          The console reads a single pre-computed verification snapshot, so it needs no backend
          process and no credentials. Generate the snapshot, then reload this page.
        </p>
        <pre className="mono-cell mt-4 rounded-lg bg-slate-900 p-3 text-emerald-300">
make bootstrap &amp;&amp; (cd backend &amp;&amp; python -m app.dashboard)
        </pre>
      </main>
    );
  }

  return (
    <>
      <Dashboard data={dashboard} scoreboard={scoreboard} />
      <footer className="mx-auto max-w-[1560px] px-5 pb-8 pt-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-200 pt-3 text-[11px] text-slate-400">
          <span className="font-semibold text-slate-500">
            SDOC · Intelligent SI vs BL Verification
          </span>
          <span>Classify → Extract → Deterministic Compare → Human Escalation</span>
          <span className="ml-auto">Averis × Monash Hackathon 2026</span>
        </div>
      </footer>
    </>
  );
}
