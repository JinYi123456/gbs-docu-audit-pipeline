-- ============================================================================
-- 0004_realtime_rls.sql —— Realtime、RLS、人审改判 RPC
--
-- 前端依赖：Next.js 订阅 emails / review_queue 的 Realtime 变更以刷新工作台。
-- 安全：service-role 走后端（可写），anon 只读视图（浏览），
--       人工改判必须走 apply_manual_verdict()，保证 verdict 自洽性不被破坏。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Realtime：把需要实时监听的表加入 supabase_realtime publication
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        begin
            alter publication supabase_realtime add table public.emails;
        exception when duplicate_object then null;
        end;
        begin
            alter publication supabase_realtime add table public.review_queue;
        exception when duplicate_object then null;
        end;
        begin
            alter publication supabase_realtime add table public.comparisons;
        exception when duplicate_object then null;
        end;
        -- UPDATE / DELETE 的 old record 也要发出来，否则前端拿不到改判前的值
        begin
            alter table public.emails replica identity full;
        exception when others then null;
        end;
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- RLS：后端 service-role 绕过 RLS；前端 anon 只允许读视图与只读表，
--      写入只能通过下面的 RPC（带自洽性校验）。
-- ---------------------------------------------------------------------------
alter table public.emails            enable row level security;
alter table public.extracted_fields  enable row level security;
alter table public.comparisons       enable row level security;
alter table public.review_queue      enable row level security;

do $$
declare
    t text;
begin
    foreach t in array array['emails', 'extracted_fields', 'comparisons', 'review_queue']
    loop
        execute format('drop policy if exists %I on public.%I', t || '_read_all', t);
        execute format(
            'create policy %I on public.%I for select to anon, authenticated using (true)',
            t || '_read_all', t);
    end loop;
end $$;

-- 仅允许人工在 review_queue 上推进工单状态（改判结果必须经 RPC 落到 emails）
drop policy if exists review_queue_update_workflow on public.review_queue;
create policy review_queue_update_workflow on public.review_queue
    for update to authenticated
    using (status in ('OPEN', 'IN_REVIEW'))
    with check (status in ('OPEN', 'IN_REVIEW', 'RESOLVED', 'DISMISSED'));

-- ---------------------------------------------------------------------------
-- 人审改判 RPC —— 第 4 步闭环的唯一写入口
--
-- 语义：把人工结论写进 emails.manual_*，同时把工单标记为 RESOLVED。
-- 自洽性由函数内部保证（与 emails 的 check 约束一致），不给前端留破坏机会。
-- ---------------------------------------------------------------------------
create or replace function public.apply_manual_verdict(
    p_email_id      text,
    p_status        public.sdoc_verdict_status,
    p_defect_fields text[] default '{}',
    p_review_reason public.sdoc_review_reason default null,
    p_note          text default null,
    p_reviewer      text default null
) returns public.emails
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row      public.emails;
    v_defects  text[];
    v_reason   public.sdoc_review_reason;
begin
    if not public.sdoc_fields_valid(p_defect_fields) then
        raise exception '非法字段名：% 只能取自 7 个 canonical 字段', p_defect_fields;
    end if;

    -- 与表约束同源的规范化：MISMATCH 必带缺陷且无理由；NEEDS_REVIEW 必带理由且无缺陷
    if p_status = 'MISMATCH' then
        v_defects := coalesce(nullif(p_defect_fields, '{}'), '{}'::text[]);
        if cardinality(v_defects) = 0 then
            raise exception 'MISMATCH 必须给出至少一个缺陷字段';
        end if;
        v_reason := null;
    elsif p_status = 'NEEDS_REVIEW' then
        v_defects := '{}'::text[];
        v_reason  := coalesce(p_review_reason, 'missing_value');
    else
        v_defects := '{}'::text[];
        v_reason  := null;
    end if;

    update public.emails e
       set manual_status        = p_status,
           manual_defect_fields = v_defects,
           manual_review_reason = v_reason,
           manual_note          = p_note,
           manual_updated_at    = now(),
           pipeline_state       = 'REVIEWED'
     where e.email_id = p_email_id
    returning * into v_row;

    if v_row.email_id is null then
        raise exception 'email_id 不存在：%', p_email_id;
    end if;

    update public.review_queue r
       set status                 = 'RESOLVED',
           resolution_note        = p_note,
           resolved_defect_fields = v_defects,
           reviewed_by            = p_reviewer,
           reviewed_at            = now()
     where r.email_id = p_email_id
       and r.status <> 'DISMISSED';

    return v_row;
end;
$$;

comment on function public.apply_manual_verdict is
    '人审改判唯一入口：写 emails.manual_* 并关单；submission_view 自动反映结果';

-- 撤销人工结论（回到机器裁决），演示时用于回滚
create or replace function public.revert_manual_verdict(
    p_email_id text,
    p_reviewer text default null
) returns public.emails
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.emails;
begin
    update public.emails e
       set manual_category      = null,
           manual_status        = null,
           manual_review_reason = null,
           manual_defect_fields = null,
           manual_note          = null,
           manual_updated_at    = null,
           pipeline_state       = case
               when e.verdict_status = 'NEEDS_REVIEW' then 'ESCALATED'::public.sdoc_pipeline_state
               else 'COMPARED'::public.sdoc_pipeline_state end
     where e.email_id = p_email_id
    returning * into v_row;

    update public.review_queue r
       set status = 'OPEN', reviewed_by = null, reviewed_at = null,
           resolution_note = null, resolved_defect_fields = null
     where r.email_id = p_email_id;

    return v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- 提交流水线状态：/submit 成功后把整批置为 SUBMITTED（演示时可见进度）
-- ---------------------------------------------------------------------------
create or replace function public.mark_submitted(p_email_ids text[] default null)
returns integer
language sql
security definer
set search_path = public
as $$
    with updated as (
        update public.emails e
           set pipeline_state = 'SUBMITTED'
         where (p_email_ids is null or e.email_id = any (p_email_ids))
           and e.pipeline_state <> 'SUBMITTED'
        returning 1
    )
    select count(*)::integer from updated;
$$;
