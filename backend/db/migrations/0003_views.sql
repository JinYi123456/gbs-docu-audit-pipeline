-- ============================================================================
-- 0003_views.sql —— 视图层
--
--   submission_view : **唯一提交出口**，输出必须与官方 sample_submission.json 逐字一致
--   dashboard_view  : 人审工作台读的宽表（7 字段 + 比对明细聚合）
--
-- ★ 为什么一定要有 submission_view：人审改判写在 emails.manual_* 上，
--   视图用 coalesce 把人工结论叠加在机器结论之上，于是「改判 → 提交」零额外代码。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 官方提交产物：恰好 5 个键
--   category / status / review_reason / defect_fields / has_defect
-- 硬约束（scoring.py 的实际消费方式）：
--   · 非 BL_COMPARISON  → status='OK'、review_reason=null、defect_fields=[]、has_defect=false
--   · NEEDS_REVIEW      → has_defect=false、defect_fields=[]、review_reason ∈ 4 值闭集
--   · MISMATCH          → has_defect=true、defect_fields 非空、review_reason=null
-- ---------------------------------------------------------------------------
create or replace view public.submission_view as
select
    e.email_id,
    coalesce(e.manual_category, e.category)::text as category,

    case
        when coalesce(e.manual_category, e.category) <> 'BL_COMPARISON' then 'OK'
        else coalesce(e.manual_status, e.verdict_status)::text
    end as status,

    case
        when coalesce(e.manual_category, e.category) <> 'BL_COMPARISON' then null
        when coalesce(e.manual_status, e.verdict_status) = 'NEEDS_REVIEW'
            then coalesce(e.manual_review_reason, e.review_reason)::text
        else null
    end as review_reason,

    case
        when coalesce(e.manual_category, e.category) <> 'BL_COMPARISON' then '{}'::text[]
        when coalesce(e.manual_status, e.verdict_status) = 'MISMATCH'
            then coalesce(e.manual_defect_fields, e.defect_fields)
        else '{}'::text[]
    end as defect_fields,

    (coalesce(e.manual_category, e.category) = 'BL_COMPARISON'
     and coalesce(e.manual_status, e.verdict_status) = 'MISMATCH') as has_defect,

    -- 诊断用（导出脚本可带 --with-diagnostics 一并写出，不计入提交）
    e.classified_by,
    e.pipeline_state,
    (e.manual_status is not null or e.manual_category is not null) as manually_reviewed
from public.emails e;

comment on view public.submission_view is
    '官方 5 键提交产物；人工改判自动生效，因此提交前只需读这张视图';

-- ---------------------------------------------------------------------------
-- 人审工作台宽表：把 7 个字段的 SI/BL 原文与判定拼成一行，前端一次查询画完
-- ---------------------------------------------------------------------------
create or replace view public.dashboard_view as
select
    e.email_id,
    e.subject,
    e.from_addr,
    e.body,
    e.attachment_paths,
    e.attachment_count,
    e.category,
    e.category_confidence,
    e.classified_by,
    e.body_hint,
    e.verdict_status,
    e.has_defect,
    e.defect_fields,
    e.review_reason,
    e.pipeline_state,
    e.manual_status,
    e.manual_defect_fields,
    e.manual_note,
    e.processed_at,
    jsonb_object_agg(
        c.field_name,
        jsonb_build_object(
            'si_raw', c.si_raw,
            'bl_raw', c.bl_raw,
            'si_normalized', c.si_normalized,
            'bl_normalized', c.bl_normalized,
            'is_match', c.is_match,
            'match_method', c.match_method,
            'similarity', c.similarity,
            'delta', c.delta,
            'needs_human', c.needs_human,
            'needs_human_reason', c.needs_human_reason,
            'highlight', c.highlight,
            'note', c.note
        )
        order by array_position(public.sdoc_compare_fields(), c.field_name)
    ) filter (where c.field_name is not null) as fields,
    r.reason       as review_queue_reason,
    r.priority     as review_priority,
    r.status       as review_status,
    r.assigned_to  as review_assignee
from public.emails e
left join public.comparisons  c on c.email_id = e.email_id
left join public.review_queue r on r.email_id = e.email_id
group by e.email_id, e.subject, e.from_addr, e.body, e.attachment_paths,
         e.attachment_count, e.category, e.category_confidence, e.classified_by,
         e.body_hint, e.verdict_status, e.has_defect, e.defect_fields,
         e.review_reason, e.pipeline_state, e.manual_status,
         e.manual_defect_fields, e.manual_note, e.processed_at,
         r.reason, r.priority, r.status, r.assigned_to;

-- ---------------------------------------------------------------------------
-- 跑批健康度：每条诊断指标一行，供前端 KPI 卡与演示脚本读取
-- ---------------------------------------------------------------------------
create or replace view public.pipeline_stats as
select
    count(*)                                                              as total_emails,
    count(*) filter (where category = 'BL_COMPARISON')                    as bl_comparison,
    count(*) filter (where verdict_status = 'MISMATCH')                   as mismatches,
    count(*) filter (where verdict_status = 'NEEDS_REVIEW')               as needs_review,
    count(*) filter (where verdict_status = 'OK')                         as ok_count,
    count(*) filter (where classified_by = 'rule')                        as rule_decided,
    count(*) filter (where classified_by = 'llm')                         as llm_decided,
    count(*) filter (where not coalesce(
        (select bool_and(f.is_readable) from public.extracted_fields f
          where f.email_id = e.email_id), true))                          as with_unreadable_doc,
    count(*) filter (where manual_status is not null)                     as human_overridden,
    round(100.0 * count(*) filter (where classified_by = 'rule')
          / greatest(count(*), 1), 2)                                     as rule_pct
from public.emails e;
