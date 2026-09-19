/**
 * 后端 API 客户端（人审工作台的"活"那一半）。
 *
 * 设计约束（都是演示现场踩出来的）：
 *  1. **必须能失败得体面**：FastAPI 没起时，界面要给"怎么把后端起起来"的指引，
 *     而不是一个转圈到天荒地老的按钮。工作台的只读部分不依赖后端，别被它拖垮。
 *  2. **必须有超时**：多模态抽取要走 Gemini（实测单次 2–14s，冷启动更久）。
 *     没有超时的 fetch 在断网时会挂到浏览器默认的 300s，演示时就是"界面卡死"。
 *  3. **绝不放密钥**：前端只与自己的 BFF 说话；service-role key 永远在后端进程里。
 */

/**
 * API 基准网址：构建时由 Next.js 内联注入 NEXT_PUBLIC_API_BASE（Vercel → Railway），
 * 本地开发缺省回落 localhost:8000。同一份代码兼容两种环境，绝无第二处硬编码。
 */
export const API_BASE =
  (process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000").replace(/\/$/, "");

export interface ApiFailure {
  ok: false;
  status: number;
  /** 面向人的一句话说明（可直接显示） */
  message: string;
  /** 面向开发者的原始 detail（可折叠） */
  detail?: string;
  kind: "timeout" | "offline" | "http" | "network";
}

export interface ApiSuccess<T> {
  ok: true;
  status: number;
  data: T;
}

export type ApiResult<T> = ApiSuccess<T> | ApiFailure;

// 只在**真的连不上**时才显示（正常演示看不到）：给运维一句可执行的指引，不暴露内部路径。
// 真连不上才显示：第一行带上前端实际尝试的 API 地址，现场排障一眼定位是"指错地址"还是"后端挂了"。
const OFFLINE_HINT =
  `Realtime services are unreachable — console could not reach ${API_BASE}.\n` +
  `Start the verification API and retry:\n` +
  `  cd backend && python -m uvicorn app.main:app --port 8000`;

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 120_000,
): Promise<ApiResult<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
    });
    const text = await response.text();
    let payload: unknown = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = text;
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? String((payload as { detail: unknown }).detail)
          : String(payload ?? "");
      return {
        ok: false,
        status: response.status,
        kind: "http",
        message:
          response.status === 422
            ? detail || "Upload rejected — check file type and size."
            : `Verification service returned HTTP ${response.status}`,
        detail,
      };
    }
    return { ok: true, status: response.status, data: payload as T };
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === "AbortError";
    return {
      ok: false,
      status: 0,
      kind: aborted ? "timeout" : "offline",
      message: aborted
        ? `No response within ${Math.round(timeoutMs / 1000)}s — multimodal extraction can be slow on a cold start, please retry once.`
        : OFFLINE_HINT,
      detail: error instanceof Error ? error.message : String(error),
    };
  } finally {
    clearTimeout(timer);
  }
}

export function getHealth(): Promise<ApiResult<Record<string, unknown>>> {
  return request("/health", {}, 8_000);
}

export function getAiStatus(): Promise<ApiResult<Record<string, unknown>>> {
  return request("/api/ai-status", {}, 10_000);
}

/**
 * 上传 SI/BL 触发即时核对（核心交互）。
 *
 * 返回类型是泛型：调用方传入自己声明的响应形状（UploadPanel 传 VerifyResponse），
 * 这样界面能拿到字段级类型提示，而 API 层不必知道业务类型。
 */
export function verifyUpload<T = Record<string, unknown>>(
  form: FormData,
  timeoutMs = 180_000,
): Promise<ApiResult<T>> {
  return request<T>("/api/verify", { method: "POST", body: form }, timeoutMs);
}

/**
 * 「实时拉取」下一批 GBS 邮件。
 *
 * 超时给 45s：确定性通道实测 <1s，但若有人把 `use_llm=true` 打开，
 * 单批 6 封要多模态串一遍（每封 2–14s），留够余量但不至于挂到浏览器默认 300s。
 */
export function pullStream(
  batchSize = 6,
  options: { reset?: boolean; useLlm?: boolean } = {},
): Promise<ApiResult<import("./types").StreamBatch>> {
  const params = new URLSearchParams({
    batch_size: String(batchSize),
    reset: options.reset ? "true" : "false",
    use_llm: options.useLlm ? "true" : "false",
  });
  return request(`/api/stream/pull?${params.toString()}`, { method: "POST" }, 45_000);
}

/**
 * 现算一封邮件的多 Agent 轨迹（不是读快照）。
 *
 * 用的就是 `/api/agents/trace/{id}`：真跑一遍编排器，返回**真实耗时**与
 * `matches_submission`（现场自证"展示的就是拿去打分的那个结论"）。
 */
export function getAgentTrace<T = Record<string, unknown>>(
  emailId: string,
  options: { useLlm?: boolean } = {},
): Promise<ApiResult<T>> {
  const query = options.useLlm ? "?use_llm=true" : "";
  return request<T>(`/api/agents/trace/${encodeURIComponent(emailId)}${query}`, {}, 90_000);
}
