-- 0006_security_hardening.sql
-- 目的：固化 2026-09-24 手动执行的 Supabase Security Advisor 修复，
--       让云端 schema 与 git 迁移历史重新一字不差。
-- 内容：① 视图 security_invoker  ② 函数 search_path 固定  ③ 收回 RPC 执行权

-- ① 4 个视图切到 SECURITY INVOKER：底表 RLS 重新生效，清除 4 条 SECURITY DEFINER ERROR
ALTER VIEW public.submission_view SET (security_invoker = on);
ALTER VIEW public.dashboard_view  SET (security_invoker = on);
ALTER VIEW public.pipeline_stats  SET (security_invoker = on);
ALTER VIEW public.upload_stats    SET (security_invoker = on);

-- ② 3 个 helper 函数固定 search_path（防 search_path 劫持；3 个 RPC 函数定义里已内建，无需处理）
ALTER FUNCTION public.sdoc_compare_fields()       SET search_path = public, pg_temp;
ALTER FUNCTION public.sdoc_fields_valid(text[])   SET search_path = public, pg_temp;
ALTER FUNCTION public.sdoc_touch_updated_at()     SET search_path = public, pg_temp;

-- ③ 收回 3 个 RPC 的 anon/authenticated/PUBLIC 执行权（后端 service_role 走显式授权，不受影响）
REVOKE EXECUTE ON FUNCTION public.apply_manual_verdict(text, public.sdoc_verdict_status, text[], public.sdoc_review_reason, text, text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.revert_manual_verdict(text, text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.mark_submitted(text[]) FROM PUBLIC, anon, authenticated;

-- service_role 显式授权（幂等保险：即使未来 PUBLIC 默认授权语义变化，后端通道也永不中断）
GRANT EXECUTE ON FUNCTION public.apply_manual_verdict(text, public.sdoc_verdict_status, text[], public.sdoc_review_reason, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.revert_manual_verdict(text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.mark_submitted(text[]) TO service_role;
