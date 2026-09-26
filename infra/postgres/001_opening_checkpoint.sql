-- Coldline
-- File: infra/postgres/001_opening_checkpoint.sql
-- Component: Database initialization
-- Purpose: Create the durable exception table and state constraints.
-- Interacts With: PostgreSQL initializer and domain state contract
-- Sprint/Task: Sprint 1 — Project 1
-- Concepts: Idempotent schema setup
-- Tools: PostgreSQL SQL

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    reading JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RECEIVED', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')),
    accepted_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    summary TEXT,
    failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS exceptions_state_idx ON exceptions (state);
