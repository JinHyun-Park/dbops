# Phase 3: Operations Automation + Approval Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** DBA 승인 기반 파라미터 변경, DDL 실행, 스키마 관리 + Cedar Policy 기반 안전 장치

**Architecture:** Operations MCP Server (8 tools) + Approval workflow (DynamoDB) + Cedar Policy + Approval Center UI + Cluster management UI

**Spec:** `docs/superpowers/specs/2026-05-08-dbops-design.md` (sections 5.4, 6)

---

## Task 1: Approval Schema + DynamoDB Table

- [ ] Create `data-pipeline/sql/schema_v3.sql` with approvals table
- [ ] Update Foundation CDK Stack to add approvals DynamoDB table
- [ ] Commit

## Task 2: Operations MCP Server (8 tools, TDD)

8 tools: get_schema_diff, get_schema_history, execute_sql, modify_parameter, modify_scaling, manage_maintenance, review_sql, audit_permissions

- [ ] Create tests for all 8 tools
- [ ] Implement all 8 tools
- [ ] Create handler
- [ ] Run tests
- [ ] Commit

## Task 3: Approval API Lambda

- [ ] Create `api/approvals/handler.py` (GET list, POST create, PUT approve/reject)
- [ ] Update Agent CDK Stack with approval routes
- [ ] Commit

## Task 4: Cedar Policy Definitions

- [ ] Create `cdk/policies/cedar/` with policy files
- [ ] Document Cedar policy configuration
- [ ] Commit

## Task 5: Frontend — Approval Center + Cluster Management

- [ ] Create Approval Center page
- [ ] Create Cluster management page (register/list)
- [ ] Update navigation
- [ ] Build verify
- [ ] Commit

## Task 6: Final Verification + Push
