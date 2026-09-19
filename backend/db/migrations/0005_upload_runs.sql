-- ============================================================================
-- 0005_upload_runs.sql —— 「上传即核对」的云端留痕
--
-- ★ 为什么必须单独一张表，而不是把上传件写进 emails：
--   官方评测的终局产物恰好是 520 个 email_id 的 5 键集合，且 `missing`/`extra`
--   都会被判罚。`submission_view` 是从 emails 聚合出来的 —— 一旦把用户上传的
--   临时核对也写进 emails，视图立刻多出未知键，线上演示时一点「上传」就把
--   已拿到的 1.0000 打成不及格。所以上传走独立表，与评测数据物理隔离。
--
-- 这张表同时也是答辩素材：它记录了「每一次真实用户上传」的输入指纹、判定结果与
-- 耗时，前端可直接订阅 Realtime 显示"刚刚发生的一次核对"。
-- ============================================================================

create table if not exists public.upload_runs (
    run_id          text primary key,
    created_at      timestamptz not null default now(),

    subject         text not null default '',
    si_name         text,
    bl_name         text,
    si_sha256       text,
    bl_sha256       text,
    si_bytes        integer check (si_bytes is null or si_bytes >= 0),
    bl_bytes        integer check (bl_bytes is null or bl_bytes >= 0),

    category        text not null default 'BL_COMPARISON',
    status          public.sdoc_verdict_status not null default 'OK',
    has_defect      boolean not null default false,
    defect_fields   text[] not null default '{}'
                    check (public.sdoc_fields_valid(defect_fields)),
    review_reason   public.sdoc_review_reason,

    extractor       text not null default 'rule',   -- rule | llm:<model>
    mode            text not null default 'PAIR',
    llm_used        boolean not null default false,
    duration_ms     integer check (duration_ms is null or duration_ms >= 0),

    -- 完整快照（7 字段两侧原文 + 归一化值 + 判定矩阵逐行结果）
    payload         jsonb not null default '{}'::jsonb
);

comment on table public.upload_runs is
    '用户主动上传 SI/BL 触发的即时核对记录；与官方 520 封评测数据严格隔离，不进 submission_view';

create index if not exists idx_upload_runs_created on public.upload_runs (created_at desc);
create index if not exists idx_upload_runs_defect  on public.upload_runs (created_at desc)
    where has_defect;

-- 前端 Realtime 订阅：上传完成即刷新"最近核对"面板
do $$
begin
    if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        begin
            alter publication supabase_realtime add table public.upload_runs;
        exception when duplicate_object then null;
        end;
    end if;
end $$;

alter table public.upload_runs enable row level security;

do $$
begin
    execute 'drop policy if exists upload_runs_read_all on public.upload_runs';
    execute 'create policy upload_runs_read_all on public.upload_runs '
            'for select to anon, authenticated using (true)';
end $$;

-- 统计视图：给前端 KPI 卡一个"上传侧"的口径，与 pipeline_stats 并列
create or replace view public.upload_stats as
select
    count(*)                                        as total_uploads,
    count(*) filter (where has_defect)              as uploads_with_defect,
    count(*) filter (where llm_used)                as uploads_via_llm,
    count(*) filter (where not llm_used)            as uploads_via_rule,
    round(avg(duration_ms)::numeric, 1)             as avg_duration_ms,
    max(created_at)                                 as last_upload_at
from public.upload_runs;

comment on view public.upload_stats is
    '上传核对侧的健康度；与 pipeline_stats（评测批次）分开统计，口径不混';
