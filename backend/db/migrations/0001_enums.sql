-- ============================================================================
-- 0001_enums.sql —— 枚举与通用触发器
--
-- 口径全部来自官方 server/scoring.py 与 sample_submission.json（源码级核对过）：
--   category      : BL_COMPARISON | SI_REQUEST | INVOICE_QUERY | GENERAL | SPAM
--   status        : OK | MISMATCH | NEEDS_REVIEW
--   review_reason : wrong_doc_type | missing_attachment | unreadable | missing_value
-- 7 个 Canonical 字段名与官方 pools.COMPARE_FIELDS 逐字一致，前端与提交产物共用。
-- ============================================================================

create extension if not exists "pgcrypto";

-- ---------- 邮件分类（5 类闭集） ----------
do $$ begin
    create type public.sdoc_category as enum
        ('BL_COMPARISON', 'SI_REQUEST', 'INVOICE_QUERY', 'GENERAL', 'SPAM');
exception when duplicate_object then null;
end $$;

-- ---------- 裁决状态（3 态闭集） ----------
do $$ begin
    create type public.sdoc_verdict_status as enum ('OK', 'MISMATCH', 'NEEDS_REVIEW');
exception when duplicate_object then null;
end $$;

-- ---------- 升级理由（4 值闭集） ----------
do $$ begin
    create type public.sdoc_review_reason as enum
        ('wrong_doc_type', 'missing_attachment', 'unreadable', 'missing_value');
exception when duplicate_object then null;
end $$;

-- ---------- 文档类型 / 角色 ----------
do $$ begin
    create type public.sdoc_doc_type as enum ('SI', 'BL');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type public.sdoc_doc_role as enum ('SI', 'BL', 'OTHER', 'UNKNOWN');
exception when duplicate_object then null;
end $$;

-- ---------- 流水线状态（人审工作台按此驱动） ----------
do $$ begin
    create type public.sdoc_pipeline_state as enum
        ('RECEIVED', 'CLASSIFIED', 'EXTRACTED', 'COMPARED', 'ESCALATED', 'REVIEWED', 'SUBMITTED');
exception when duplicate_object then null;
end $$;

-- ---------- 人审工单状态 ----------
do $$ begin
    create type public.sdoc_review_status as enum
        ('OPEN', 'IN_REVIEW', 'RESOLVED', 'DISMISSED');
exception when duplicate_object then null;
end $$;

-- ---------- 7 个 Canonical 字段的常量表（约束与视图复用，避免各处硬编码漂移） ----------
create or replace function public.sdoc_compare_fields()
returns text[]
language sql immutable
as $$
    select array['shipper', 'consignee', 'notify_party',
                 'port_of_loading', 'port_of_discharge',
                 'container_count', 'gross_weight_kg']::text[];
$$;

-- 校验：数组必须是 7 字段的子集（防止脏字段名污染 defect_fields）
create or replace function public.sdoc_fields_valid(fields text[])
returns boolean
language sql immutable
as $$
    select coalesce(fields <@ public.sdoc_compare_fields(), true);
$$;

-- ---------- updated_at 自动维护 ----------
create or replace function public.sdoc_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;
