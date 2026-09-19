-- ============================================================================
-- 0002_tables.sql —— 四张核心表
--
--   emails           一份邮件任务 + 最终裁决（父表，一切外键指向它）
--   extracted_fields  AI 从 SI / BL 各自抽出的 7 字段（一行一个文档）
--   comparisons       字段级比对结果（一行一个字段，前端标红的唯一数据源）
--   review_queue      第 4 步 Ask for help 的人工工单
--
-- 幂等：全部 upsert 走主键 / unique 约束，重跑整批没有任何副作用。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1) emails —— 邮件任务与最终裁决
-- ---------------------------------------------------------------------------
create table if not exists public.emails (
    email_id             text primary key,
    subject              text not null default '',
    body                 text not null default '',
    from_addr            text not null default '',
    attachment_paths     text[] not null default '{}',
    -- 由附件数组派生，写入时**必须**从 payload 里剔除（GENERATED 列不可写）
    attachment_count     integer generated always as (
                             coalesce(array_length(attachment_paths, 1), 0)
                         ) stored,

    category             public.sdoc_category not null default 'GENERAL',
    category_confidence  numeric(4, 3) not null default 0.000
                         check (category_confidence >= 0 and category_confidence <= 1),
    classified_by        text not null default 'rule'
                         check (classified_by in ('rule', 'llm', 'human')),
    body_hint            public.sdoc_review_reason,

    verdict_status       public.sdoc_verdict_status not null default 'OK',
    has_defect           boolean not null default false,
    defect_fields        text[] not null default '{}'
                         check (public.sdoc_fields_valid(defect_fields)),
    review_reason        public.sdoc_review_reason,

    pipeline_state       public.sdoc_pipeline_state not null default 'RECEIVED',
    prompt_version       text not null default '',
    model_classify       text,
    latency_ms           integer check (latency_ms is null or latency_ms >= 0),
    processed_at         timestamptz,

    -- ---- 人审改判（第 4 步闭环）：非空即覆盖机器裁决，视图自动生效 ----
    manual_category      public.sdoc_category,
    manual_status        public.sdoc_verdict_status,
    manual_review_reason public.sdoc_review_reason,
    manual_defect_fields text[]
                         check (manual_defect_fields is null
                                or public.sdoc_fields_valid(manual_defect_fields)),
    manual_note          text,
    manual_updated_at    timestamptz,

    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),

    -- 铁律约束：MISMATCH ⇔ 有缺陷字段且无升级理由；NEEDS_REVIEW ⇔ 有理由且无缺陷
    constraint emails_verdict_ck check (
        (verdict_status = 'MISMATCH'
             and has_defect and cardinality(defect_fields) > 0 and review_reason is null)
        or (verdict_status = 'OK'
             and not has_defect and cardinality(defect_fields) = 0)
        or (verdict_status = 'NEEDS_REVIEW'
             and not has_defect and review_reason is not null)
    ),
    -- 非 BL_COMPARISON 的邮件不参与比对，永远是干净的 OK
    constraint emails_non_compare_ck check (
        category = 'BL_COMPARISON'
        or (verdict_status = 'OK' and not has_defect
            and cardinality(defect_fields) = 0 and review_reason is null)
    )
);

comment on column public.emails.defect_fields is
    '官方 end_to_end 指标要求**集合完全相等**：多一个字段也算错，务必只写真实差异';
comment on column public.emails.manual_defect_fields is
    '人审改判后的缺陷集合，优先级高于机器结果，submission_view 会 coalesce 它';

drop trigger if exists trg_emails_touch on public.emails;
create trigger trg_emails_touch before update on public.emails
    for each row execute function public.sdoc_touch_updated_at();

create index if not exists idx_emails_category      on public.emails (category);
create index if not exists idx_emails_verdict       on public.emails (verdict_status);
create index if not exists idx_emails_state         on public.emails (pipeline_state);
create index if not exists idx_emails_defect_fields on public.emails using gin (defect_fields);
create index if not exists idx_emails_review_reason on public.emails (review_reason)
    where review_reason is not null;

-- ---------------------------------------------------------------------------
-- 2) extracted_fields —— 一个文档一行，7 字段装在 canonical jsonb 里
-- ---------------------------------------------------------------------------
create table if not exists public.extracted_fields (
    email_id          text not null
                      references public.emails (email_id) on delete cascade,
    doc_type          public.sdoc_doc_type not null,
    source_path       text not null default '',
    -- 键名严格等于 7 个 canonical 字段名，值是该侧原始文本
    values            jsonb not null default '{}'::jsonb
                      check (values ?& public.sdoc_compare_fields()),
    field_confidence  jsonb not null default '{}'::jsonb,
    field_blank       text[] not null default '{}'
                      check (public.sdoc_fields_valid(field_blank)),
    doc_role_detected public.sdoc_doc_role not null default 'UNKNOWN',
    other_doc_kind    text,
    extractor_model   text not null default 'rule',
    prompt_version    text not null default '',
    is_readable       boolean not null default true,
    read_error        text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    primary key (email_id, doc_type)
);

comment on table public.extracted_fields is
    'SI 与 BL **独立**抽取：对同一封邮件写两行（doc_type=SI / BL），绝不交叉污染';

drop trigger if exists trg_extracted_touch on public.extracted_fields;
create trigger trg_extracted_touch before update on public.extracted_fields
    for each row execute function public.sdoc_touch_updated_at();

create index if not exists idx_extracted_readable on public.extracted_fields (is_readable)
    where not is_readable;

-- ---------------------------------------------------------------------------
-- 3) comparisons —— 字段级差异（前端红框高亮 + 人审界面都读这里）
-- ---------------------------------------------------------------------------
create table if not exists public.comparisons (
    email_id           text not null
                       references public.emails (email_id) on delete cascade,
    field_name         text not null check (field_name = any (public.sdoc_compare_fields())),
    si_raw             text,
    bl_raw             text,
    si_normalized      text,
    bl_normalized      text,
    si_parsed          jsonb not null default '{}'::jsonb,
    bl_parsed          jsonb not null default '{}'::jsonb,
    is_match           boolean not null,
    match_method       text not null check (match_method in (
                           'exact', 'normalized', 'alias', 'numeric_tolerance',
                           'fuzzy', 'missing', 'not_compared', 'blank_one_side')),
    similarity         numeric(5, 4) check (similarity is null
                           or (similarity >= 0 and similarity <= 1)),
    delta              numeric(14, 3),
    needs_human        boolean not null default false,
    needs_human_reason public.sdoc_review_reason,
    -- 供前端直接渲染的 [start,end] 区间；留空则整格标红
    highlight          jsonb not null default '[]'::jsonb,
    decided_by         text not null default 'rule',
    note               text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    primary key (email_id, field_name),
    -- 自洽性：判为「不一致」的行不允许用 missing 占位（missing 只能配 needs_human）
    constraint comparisons_mismatch_ck check (
        is_match or needs_human or match_method <> 'missing'),
    -- 不一致必须能说清理由（delta / similarity / note 至少有一个）
    constraint comparisons_evidence_ck check (
        is_match or needs_human or note is not null or delta is not null
        or similarity is not null)
);

comment on column public.comparisons.match_method is
    'blank_one_side 表示一侧有值另一侧被留空（值在传递中丢失），属实质不一致';

drop trigger if exists trg_comparisons_touch on public.comparisons;
create trigger trg_comparisons_touch before update on public.comparisons
    for each row execute function public.sdoc_touch_updated_at();

create index if not exists idx_comparisons_mismatch on public.comparisons (email_id)
    where (not is_match and not needs_human);
create index if not exists idx_comparisons_needs_human on public.comparisons (email_id)
    where needs_human;

-- ---------------------------------------------------------------------------
-- 4) review_queue —— 第 4 步 Ask for help 的人工工单
-- ---------------------------------------------------------------------------
create table if not exists public.review_queue (
    id                      bigint generated always as identity primary key,
    email_id                text not null
                            references public.emails (email_id) on delete cascade,
    reason                  public.sdoc_review_reason not null,
    priority                smallint not null default 50
                            check (priority between 0 and 100),
    field_names             text[] not null default '{}'
                            check (public.sdoc_fields_valid(field_names)),
    status                  public.sdoc_review_status not null default 'OPEN',
    assigned_to             text,
    -- 人审人给出的最终结论（写回 emails.manual_* 后本表标记 RESOLVED）
    resolution_note         text,
    resolved_defect_fields  text[]
                            check (resolved_defect_fields is null
                                   or public.sdoc_fields_valid(resolved_defect_fields)),
    reviewed_by             text,
    reviewed_at             timestamptz,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    -- 同一封邮件同一理由只保留一条工单：重跑幂等
    unique (email_id, reason)
);

comment on table public.review_queue is
    '低置信度/不一致的待人工审核任务；priority 越小越急（missing_attachment 最优先）';

drop trigger if exists trg_review_touch on public.review_queue;
create trigger trg_review_touch before update on public.review_queue
    for each row execute function public.sdoc_touch_updated_at();

create index if not exists idx_review_open on public.review_queue (status, priority, created_at)
    where status in ('OPEN', 'IN_REVIEW');
create index if not exists idx_review_email on public.review_queue (email_id);
