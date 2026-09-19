import fs from "node:fs";
import path from "node:path";

import type { Dashboard } from "./types";

/**
 * 服务端读取跑批快照。
 *
 * 刻意不做「没有数据就报错」：演示环境、CI、队友的干净克隆都可能在跑批之前
 * 打开前端，此时应当给出可读的引导页，而不是 500。
 */
const CANDIDATES = [
  process.env.DASHBOARD_PATH,
  path.join(process.cwd(), "..", "eval", "report", "dashboard.json"),
  path.join(process.cwd(), "eval", "report", "dashboard.json"),
  path.join(process.cwd(), "public", "dashboard.json"),
].filter(Boolean) as string[];

export interface LoadResult {
  dashboard: Dashboard | null;
  source: string | null;
  scoreboard: Scoreboard | null;
  scoreSource: string | null;
}

export interface Scoreboard {
  final_score?: number;
  stage1_macro_f1?: number;
  stage3_defect_f1?: number;
  end_to_end?: { success?: number; total?: number; rate?: number };
  saved_at?: string;
}

function firstExisting(paths: string[]): string | null {
  for (const candidate of paths) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      /* 不存在就继续找 */
    }
  }
  return null;
}

export function loadDashboard(): LoadResult {
  const dashboardPath = firstExisting(CANDIDATES);
  let dashboard: Dashboard | null = null;
  if (dashboardPath) {
    try {
      dashboard = JSON.parse(fs.readFileSync(dashboardPath, "utf-8")) as Dashboard;
    } catch {
      dashboard = null;
    }
  }

  const scoreCandidates = [
    process.env.BEST_SCORE_PATH,
    path.join(process.cwd(), "..", "eval", "report", "best.json"),
    path.join(process.cwd(), "eval", "report", "best.json"),
  ].filter(Boolean) as string[];
  const scorePath = firstExisting(scoreCandidates);
  let scoreboard: Scoreboard | null = null;
  if (scorePath) {
    try {
      scoreboard = JSON.parse(fs.readFileSync(scorePath, "utf-8")) as Scoreboard;
    } catch {
      scoreboard = null;
    }
  }

  return {
    dashboard,
    source: dashboardPath,
    scoreboard,
    scoreSource: scorePath,
  };
}
